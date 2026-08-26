"""Asynchronous HTTP client for Ziniao's local WebDriver service.

The API always stays on loopback by default.  Credentials are added only to
request bodies and response/error messages are sanitised before logging.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Mapping
from uuid import uuid4

import httpx

from .errors import ZiniaoApiError, ZiniaoConnectionError, ZiniaoCredentialError
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
        credential_resolver: "Callable[[], Mapping[str, str]] | None" = None,
        credential_resolver_authoritative: bool = False,
    ) -> None:
        self.config = config
        # Resolved per call rather than captured at construction.  The account
        # is configured from the console *after* the service is already up, so
        # a value baked in at startup is empty on exactly the run that matters:
        # Ziniao answers -10003 「参数不能为空（登录状态错误）」 and the operator
        # is told to check a login that is in fact configured correctly.
        # Observed 2026-08-24 right after the credentials page shipped.
        self._credential_resolver = credential_resolver
        # Generic/library callers keep the backwards-compatible per-field
        # fallback.  Production opts into authoritative mode so replacing or
        # disabling the database account can never combine its metadata with a
        # password captured for the previous account at process startup.
        self._credential_resolver_authoritative = bool(
            credential_resolver_authoritative
        )
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
        credential_resolver: "Callable[[], Mapping[str, str]] | None" = None,
        credential_resolver_authoritative: bool = False,
    ) -> "ZiniaoClient":
        """Build a client while keeping the database free of secret values."""
        from .credentials import read_generic_credential

        secret = read_generic_credential(credential_ref)
        resolved = ZiniaoClientConfig(
            host=config.host,
            port=config.port,
            # Credential Manager records written by older builds may contain
            # only a password (or only the account metadata).  Keep the
            # caller's startup values for each field that is absent/empty;
            # otherwise a partial record silently turns a valid request into
            # Ziniao's -10003 "parameter cannot be empty" response.
            company=_credential_value(secret, "company", config.company),
            username=_credential_value(secret, "username", config.username),
            password=_credential_value(secret, "password", config.password),
            connect_timeout=config.connect_timeout,
            request_timeout=config.request_timeout,
        )
        return cls(
            resolved,
            transport=transport,
            credential_resolver=credential_resolver,
            credential_resolver_authoritative=credential_resolver_authoritative,
        )

    async def __aenter__(self) -> "ZiniaoClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._http.aclose()

    def preflight_credentials(self) -> None:
        """Validate the current credential snapshot without making a request.

        The resolved values are deliberately discarded.  Callers learn only
        whether the configured database/vault reference is usable; company,
        username and password never leave this adapter or appear in a return
        value.  In particular this method does not probe port 16851.
        """

        if self._closed:
            raise RuntimeError("ZiniaoClient has been closed")
        snapshot = self._credentials()
        if (
            not str(snapshot.get("company") or "").strip()
            or not str(snapshot.get("username") or "").strip()
            or not str(snapshot.get("password") or "")
        ):
            raise ZiniaoCredentialError(
                "紫鸟凭据不完整，请在系统设置中重新保存公司、账号和密码"
            )

    def _credentials(self) -> dict[str, str]:
        startup = {
            "company": self.config.company,
            "username": self.config.username,
            "password": self.config.password,
        }
        if self._credential_resolver is not None:
            try:
                resolved = self._credential_resolver()
            except Exception as exc:
                if self._credential_resolver_authoritative:
                    if isinstance(exc, ZiniaoCredentialError):
                        raise
                    raise ZiniaoCredentialError(
                        "紫鸟凭据暂时不可读取，请在系统设置中重新保存后重试"
                    ) from exc
                # A resolver failure must not hide the static config, which is
                # what a developer-supplied client and the tests rely on.
                logger.warning(
                    "Could not resolve Ziniao credentials; falling back to the "
                    "values captured at startup (error=%s)",
                    type(exc).__name__,
                )
                resolved = None
            if isinstance(resolved, Mapping):
                if self._credential_resolver_authoritative:
                    snapshot = {
                        "company": str(resolved.get("company") or "").strip(),
                        "username": str(resolved.get("username") or "").strip(),
                        # Passwords may legally start or end with whitespace.
                        # Preserve the exact vault value sent by the operator.
                        "password": str(resolved.get("password") or ""),
                    }
                    missing = [key for key, value in snapshot.items() if not value]
                    if missing:
                        raise ZiniaoCredentialError(
                            "紫鸟凭据不完整，请在系统设置中重新保存公司、账号和密码"
                        )
                    return snapshot
                # Merge field-by-field rather than treating the resolver's
                # mapping as a replacement.  The web console stores metadata
                # and the secret separately, and a read can legitimately
                # return a partial mapping while one side is being updated.
                return {
                    key: _credential_value(resolved, key, startup[key])
                    for key in startup
                }
            if resolved is not None:
                if self._credential_resolver_authoritative:
                    raise ZiniaoCredentialError(
                        "紫鸟凭据读取结果无效，请在系统设置中重新保存后重试"
                    )
                logger.warning(
                    "Ziniao credential resolver returned a non-mapping; "
                    "falling back to startup values"
                )
        if self._credential_resolver_authoritative:
            raise ZiniaoCredentialError(
                "紫鸟账号尚未配置，请先在系统设置中保存紫鸟账号"
            )
        return startup

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
        # Resolve exactly once.  Besides avoiding an extra Credential Manager
        # read, retaining this immutable snapshot ensures error sanitisation
        # uses the same values that were sent in this request.
        credentials = self._credentials()
        body: dict[str, Any] = {
            "action": action,
            "requestId": str(uuid4()),
            **credentials,
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
                secrets=tuple(credentials.values())
                + (
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


def _credential_value(
    values: Mapping[str, Any],
    key: str,
    fallback: str,
) -> str:
    """Return one credential field, falling back when it is absent/empty.

    Credential records are assembled from two stores (SQLite metadata and
    Windows Credential Manager), so a mapping can be valid while still
    omitting one field.  Keeping this rule in one helper makes construction
    from a reference and per-call live resolution behave identically.
    """

    value = values.get(key)
    if value is None:
        return str(fallback or "")
    text = str(value)
    return text if text else str(fallback or "")


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
