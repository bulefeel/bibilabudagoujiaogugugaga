from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path

from ziniao_automation.logging_config import (
    JsonLineFormatter,
    RedactionFilter,
    purge_expired_artifacts,
    redact,
)


def test_recursive_redaction_removes_secret_values() -> None:
    raw = {
        "username": "seller@example.com",
        "password": "p@ss",
        "otp_code": "394046",
        "nested": {"app_secret": "secret", "authorization": "Bearer abc"},
        "message": "token=abc cookie=xyz email=seller@example.com verification_code=394046",
    }
    cleaned = redact(raw)
    rendered = json.dumps(cleaned)
    assert "p@ss" not in rendered
    assert '"secret"' not in rendered
    assert "abc" not in rendered
    assert "xyz" not in rendered
    assert "seller@example.com" not in rendered
    assert "394046" not in rendered
    assert cleaned["username"] == "***"
    assert cleaned["otp_code"] == "***"


def test_jsonl_formatter_is_valid_and_redacted() -> None:
    record = logging.LogRecord(
        "unit",
        logging.INFO,
        __file__,
        1,
        "Authorization: Bearer TOP_SECRET",
        (),
        None,
    )
    assert RedactionFilter().filter(record)
    decoded = json.loads(JsonLineFormatter().format(record))
    assert decoded["message"] == "Authorization: ***"


def test_retention_removes_old_file_but_keeps_new(tmp_path: Path) -> None:
    old = tmp_path / "old.png"
    fresh = tmp_path / "fresh.png"
    old.write_bytes(b"old")
    fresh.write_bytes(b"new")
    now = datetime(2026, 8, 12, tzinfo=UTC)
    old_time = datetime(2026, 4, 1, tzinfo=UTC).timestamp()
    fresh_time = datetime(2026, 8, 11, tzinfo=UTC).timestamp()
    os.utime(old, (old_time, old_time))
    os.utime(fresh, (fresh_time, fresh_time))
    removed = purge_expired_artifacts((tmp_path,), retention_days=90, now=now)
    assert old in removed
    assert not old.exists()
    assert fresh.exists()
