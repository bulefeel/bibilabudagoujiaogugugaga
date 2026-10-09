"""Password-free local console access and signed, database-free CSRF sessions.

The historical administrator tables are retained for database compatibility.
They are not read, created or updated by the console anymore.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import hmac
from ipaddress import ip_address
import re
import secrets
import time
from urllib.parse import urlsplit

_TOKEN_PATTERN = re.compile(r"[0-9]{1,12}\.[A-Za-z0-9_-]{43}\.[a-f0-9]{64}")


@dataclass(frozen=True, slots=True)
class LocalOperator:
    username: str = "本机操作员"


class LocalAccess:
    """Tokens prove a write originated from a page opened on this backend.

    This is not user authentication. A restart rotates the process-local key;
    opening or refreshing a page supplies fresh tokens without a login screen.
    """

    def __init__(self, *, session_hours: int = 12) -> None:
        if session_hours < 1:
            raise ValueError("本地页面会话有效期至少为 1 小时")
        self._key = secrets.token_bytes(32)
        self._max_age = session_hours * 3600

    def _sign(self, purpose: str, value: str) -> str:
        return hmac.new(self._key, f"{purpose}:{value}".encode("utf-8"), sha256).hexdigest()

    def valid_session(self, token: str | None) -> bool:
        if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
            return False
        issued, nonce, signature = token.split(".")
        age = int(time.time()) - int(issued)
        return (
            0 <= age < self._max_age
            and hmac.compare_digest(signature, self._sign("session", f"{issued}.{nonce}"))
        )

    def page_tokens(self, existing: str | None = None) -> tuple[str, str]:
        if self.valid_session(existing):
            token = str(existing)
        else:
            body = f"{int(time.time())}.{secrets.token_urlsafe(32)}"
            token = f"{body}.{self._sign('session', body)}"
        return token, self._sign("csrf", token)

    def validate_csrf(self, token: str | None, csrf: str | None) -> bool:
        return (
            self.valid_session(token)
            and isinstance(csrf, str)
            and bool(re.fullmatch(r"[a-f0-9]{64}", csrf))
            and hmac.compare_digest(csrf, self._sign("csrf", str(token)))
        )


def is_loopback_client(host: str | None, *, testing: bool = False) -> bool:
    # TestClient has a synthetic peer; never accept it in a deployed app.
    if testing and host == "testclient":
        return True
    try:
        return ip_address(host or "").is_loopback
    except ValueError:
        return False


def is_same_origin_request(origin: str | None, fetch_site: str | None, url: str) -> bool:
    """Reject foreign websites (including another localhost port), not API tools."""
    if fetch_site is not None and fetch_site.lower() not in {"same-origin", "none"}:
        return False
    if origin is None:
        return True  # Non-browser clients still need the signed CSRF pair for writes.
    try:
        source, target = urlsplit(origin), urlsplit(url)
        def authority(value):
            return value.scheme, value.hostname, value.port if value.port is not None else (443 if value.scheme == "https" else 80)
        return (
            source.scheme in {"http", "https"}
            and not source.username and not source.password
            and not source.path and not source.query and not source.fragment
            and authority(source) == authority(target)
        )
    except ValueError:
        return False
