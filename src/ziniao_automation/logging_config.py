"""Secret-safe structured logging and artifact retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import re
import traceback
from typing import Any, Mapping


SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "access_token",
        "tenant_access_token",
        "refresh_token",
        "cookie",
        "cookies",
        "authorization",
        "secret",
        "app_secret",
        "webhook",
        "license_blob",
        "email",
        "username",
        "otp",
        "otp_code",
        "verification_code",
    }
)
_INLINE_SECRET = re.compile(
    r"(?i)(password|passwd|token|access_token|tenant_access_token|"
    r"refresh_token|cookie|secret|app_secret|webhook|email|username|"
    r"otp|otp_code|verification_code)"
    r"(\s*[=:]\s*)([^,;\s&]+)"
)
_AUTHORIZATION = re.compile(
    r"(?i)(authorization\s*[=:]\s*)(?:bearer\s+)?[^,;\s&]+"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_FEISHU_HOOK = re.compile(
    r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
_URL_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|access_token|secret|app_secret|key)=)[^&#\s]+"
)


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secrets from log-friendly values."""
    if key and key.lower() in SECRET_KEYS:
        return "***"
    if isinstance(value, Mapping):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        text = _AUTHORIZATION.sub(lambda match: f"{match.group(1)}***", value)
        text = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}***", text)
        text = _BEARER.sub("Bearer ***", text)
        text = _URL_QUERY_SECRET.sub(lambda match: f"{match.group(1)}***", text)
        return _FEISHU_HOOK.sub("https://open.feishu.cn/open-apis/bot/v2/hook/***", text)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        if isinstance(record.args, Mapping):
            record.args = redact(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(redact(item) for item in record.args)
        if record.exc_info:
            # Drop the rendered traceback from the production log: third-party
            # exceptions can embed request bodies/URLs.  Dropping *everything*
            # went too far, though — a production failure reached the log as a
            # bare type name, so an OperationalError meaning "database is
            # locked" was indistinguishable from any other database fault and
            # nothing could be diagnosed without reproducing it.
            #
            # Keep a structure-only summary instead: the first line of the
            # message (drivers put the cause there and append the data-bearing
            # "[SQL: ...] / [parameters: ...]" tail afterwards) plus
            # file:line:function per frame.  No source text, no locals, no
            # arguments, and everything still passes through ``redact``.
            exc_type, exc_value, exc_tb = record.exc_info
            record.safe_exception_type = exc_type.__name__
            if exc_value is not None:
                rendered = str(exc_value)
                first_line = rendered.splitlines()[0] if rendered else ""
                record.safe_exception_message = first_line[:300]
            frames = traceback.extract_tb(exc_tb)
            if frames:
                record.safe_exception_frames = " <- ".join(
                    f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                    for frame in frames[-8:]
                )
            record.exc_info = None
            record.exc_text = None
        return True


class JsonLineFormatter(logging.Formatter):
    """One valid JSON object per line; Unicode stays readable."""

    def format(self, record: logging.LogRecord) -> str:
        message = redact(record.getMessage())
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        for field in ("run_id", "store_id", "marketplace", "event"):
            if hasattr(record, field):
                payload[field] = redact(getattr(record, field), key=field)
        if hasattr(record, "safe_exception_type"):
            payload["exception_type"] = redact(record.safe_exception_type)
            if hasattr(record, "safe_exception_message"):
                payload["exception_message"] = redact(record.safe_exception_message)
            if hasattr(record, "safe_exception_frames"):
                payload["exception_frames"] = redact(record.safe_exception_frames)
        elif record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(
    log_dir: Path,
    *,
    level: int | str = logging.INFO,
    retention_days: int = 90,
    console: bool = True,
) -> Path:
    """Configure root JSONL logging with midnight rotation and retention."""
    directory = Path(log_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "ziniao-automation.jsonl"
    handler = TimedRotatingFileHandler(
        target,
        when="midnight",
        interval=1,
        backupCount=max(1, retention_days),
        encoding="utf-8",
        utc=False,
        delay=True,
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(JsonLineFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(JsonLineFormatter())
        stream.addFilter(RedactionFilter())
        root.addHandler(stream)
    return target


def purge_expired_artifacts(
    roots: list[Path] | tuple[Path, ...],
    *,
    retention_days: int = 90,
    now: datetime | None = None,
) -> list[Path]:
    """Delete old files only under explicit roots, then empty directories."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    instant = now or datetime.now(UTC)
    cutoff = instant - timedelta(days=retention_days)
    removed: list[Path] = []
    for root_value in roots:
        root = Path(root_value).resolve()
        if not root.is_dir():
            continue
        files = [item for item in root.rglob("*") if item.is_file()]
        for item in files:
            resolved = item.resolve()
            if root not in resolved.parents:
                continue
            modified = datetime.fromtimestamp(item.stat().st_mtime, UTC)
            if modified < cutoff:
                item.unlink(missing_ok=True)
                removed.append(item)
        directories = sorted(
            (item for item in root.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass
    return removed
