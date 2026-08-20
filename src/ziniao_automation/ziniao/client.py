"""Asynchronous HTTP client for Ziniao's local WebDriver service.

The API always stays on loopback by default.  Credentials are added only to
request bodies and response/error messages are sanitised before logging.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Mapping
from uuid import uuid4

import httpx

from .errors import ZiniaoApiError, ZiniaoConnectionError
from .models import BrowserProfile, ProfileSelector

logger = logging.getLogger(__name__)

_SECRET_KEYS = frozenset({"password", "token", "cookie", "secret", "authorization"})


@dataclass(frozen=True, slots=True)
class ZiniaoClientConfig:
    host: str = "127.0.0.1"
    port: int = 16851
    company: str = ""
    username: str = ""
    password: str = ""
    connect_timeout: float = 3.0
    request_timeout: float = 60.0

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __post_init__(self) -> None:
        if self.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("Ziniao API host must be local loopback")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("Ziniao API port is invalid")
        if self.connect_timeout <= 0 or self.request_timeout <= 0:
            raise ValueError("Ziniao timeouts must be positive")


@dataclass(frozen=True, slots=True)
class ZiniaoApiResponse:
    action: str
    status_code: str
    payload: dict[str, Any]


class ZiniaoClient:
    """Typed façade over the small subset of Ziniao actions used by V1."""

    def __init__(
        self,
        config: ZiniaoClientConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        timeout = httpx.Timeout(
            config.request_timeout,
            connect=config.connect_timeout,
        )
        self._http = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )
        self._closed = False

    @classmethod
    def from_credential_reference(
        cls,
        config: ZiniaoClientConfig,
        credential_ref: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "ZiniaoClient":
        """Build a client while keeping the database free of secret values."""
        from .credentials import read_generic_credential

        secret = read_generic_credential(credential_ref)
        resolved = ZiniaoClientConfig(
            host=config.host,
            port=config.port,
            company=secret.get("company", config.company),
            username=secret.get("username", config.username),
            password=secret.get("password", ""),
            connect_timeout=config.connect_timeout,
            request_timeout=config.request_timeout,
        )
        return cls(resolved, transport=transport)

    async def __aenter__(self) -> "ZiniaoClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._http.aclose()

    def _credentials(self) -> dict[str, str]:
        return {
            "company": self.config.company,
            "username": self.config.username,
            "password": self.config.password,
        }

    async def call(
        self,
        action: str,
        /,
        *,
        timeout: float | None = None,
        require_success: bool = True,
        **parameters: Any,
    ) -> ZiniaoApiResponse:
        if self._closed:
            raise RuntimeError("ZiniaoClient has been closed")
        body: dict[str, Any] = {
            "action": action,
            "requestId": str(uuid4()),
            **self._credentials(),
            **parameters,
        }
        safe_parameters = _redact(parameters)
        logger.debug("Calling Ziniao action=%s parameters=%s", action, safe_parameters)
        decoded: Any
        try:
            response = await self._http.post(
                self.config.endpoint,
                json=body,
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ZiniaoConnectionError(
                f"Ziniao local API at {self.config.endpoint} did not return a valid response"
            ) from exc
        try:
            decoded = response.json()
        except ValueError as exc:
            raise ZiniaoConnectionError(
                f"Ziniao local API at {self.config.endpoint} returned invalid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise ZiniaoConnectionError("Ziniao returned a non-object JSON response")
        status_code = str(decoded.get("statusCode", ""))
        if require_success and status_code != "0":
            message = _safe_api_message(
                str(decoded.get("err") or decoded.get("message") or ""),
                secrets=(
                    self.config.password,
                    self.config.username,
                    self.config.company,
                ),
            )
            raise ZiniaoApiError(action, status_code or "missing", message)
        return ZiniaoApiResponse(action=action, status_code=status_code, payload=decoded)

    async def probe(self) -> ZiniaoApiResponse:
        """Return the raw status without turning permission errors into exceptions."""
        return await self.call("getBrowserList", timeout=5.0, require_success=False)

    async def get_browser_list(self) -> list[BrowserProfile]:
        response = await self.call("getBrowserList")
        records = _extract_browser_records(response.payload)
        profiles: list[BrowserProfile] = []
        for record in records:
            oauth = _first_string(record, "browserOauth", "browserOAuth", "oauth") or None
            browser_id = _first_string(record, "browserId", "id") or None
            if oauth:
                selector = ProfileSelector("oauth", oauth)
            elif browser_id:
                selector = ProfileSelector("id", browser_id)
            else:
                logger.warning("Ignoring Ziniao browser record without an identifier")
                continue
            name = _first_string(
                record,
                "browserName",
                "name",
                "envName",
                "storeName",
            ) or selector.value
            profiles.append(
                BrowserProfile(
                    selector=selector,
                    browser_oauth=oauth,
                    browser_id=browser_id,
                    name=name,
                    raw=dict(record),
                )
            )
        return profiles

    async def update_core(self) -> ZiniaoApiResponse:
        return await self.call("updateCore", timeout=15.0, require_success=False)

    async def start_browser(self, selector: ProfileSelector) -> dict[str, Any]:
        response = await self.call(
            "startBrowser",
            timeout=60.0,
            isWaitPluginUpdate=0,
            isHeadless=0,
            isWebDriverReadOnlyMode=0,
            cookieTypeLoad=0,
            cookieTypeSave=0,
            runMode="1",
            isLoadUserPlugin=False,
            pluginIdType=1,
            privacyMode=0,
            notPromptForDownload=1,
            **selector.api_payload(),
        )
        return response.payload

    async def stop_browser(
        self,
        selector: ProfileSelector,
        *,
        ignore_errors: bool = False,
    ) -> bool:
        try:
            await self.call("stopBrowser", timeout=10.0, **selector.api_payload())
            return True
        except (ZiniaoApiError, ZiniaoConnectionError):
            if ignore_errors:
                logger.info("Best-effort stopBrowser did not complete for %s", selector.value)
                return False
            raise


def _extract_browser_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for key in ("browserList", "data", "list", "rows"):
        candidates.append(payload.get(key))
    while candidates:
        candidate = candidates.pop(0)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            # Some client versions return a single browser object in data.
            if any(
                key in candidate
                for key in ("browserOauth", "browserOAuth", "browserId", "oauth")
            ):
                return [dict(candidate)]
            candidates.extend(candidate.get(key) for key in ("browserList", "list", "rows", "data"))
    return []


def _first_string(record: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "***" if str(key).lower() in _SECRET_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _safe_api_message(message: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Prevent an old client from echoing configured secrets in errors."""
    text = message
    # Error strings are user-visible; redact common inline secret formats.
    import re

    for pattern in (
        r'(?i)(password\s*[=:]\s*)[^,;\s]+',
        r'(?i)(token\s*[=:]\s*)[^,;\s]+',
        r'(?i)(cookie\s*[=:]\s*)[^,;\s]+',
        r'(?i)(secret\s*[=:]\s*)[^,;\s]+',
    ):
        text = re.sub(pattern, r'\1***', text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text[:1000]
