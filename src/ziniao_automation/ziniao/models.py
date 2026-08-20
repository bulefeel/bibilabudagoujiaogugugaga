"""Small value objects shared by the Ziniao adapter and workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProfileSelector:
    """Unambiguous selector accepted by Ziniao's start/stop actions."""

    type: Literal["oauth", "id"]
    value: str

    def __post_init__(self) -> None:
        value = str(self.value).strip()
        if self.type not in {"oauth", "id"}:
            raise ValueError("selector type must be 'oauth' or 'id'")
        if not value:
            raise ValueError("selector value must not be empty")
        object.__setattr__(self, "value", value)

    def api_payload(self) -> dict[str, str]:
        key = "browserOauth" if self.type == "oauth" else "browserId"
        return {key: self.value}


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    """A browser environment returned by ``getBrowserList``."""

    selector: ProfileSelector
    name: str
    browser_oauth: str | None = None
    browser_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def selector_type(self) -> Literal["oauth", "id"]:
        return self.selector.type

    @property
    def selector_value(self) -> str:
        return self.selector.value


@dataclass(frozen=True, slots=True)
class CdpHealth:
    """Observed health of a raw Chromium CDP endpoint."""

    reachable: bool
    browser: str | None = None
    websocket_url: str | None = None
    instrumentation_live: bool | None = None
    observed_url: str | None = None
    observed_title: str | None = None
    issues: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.reachable and self.instrumentation_live is not False and not self.issues


@dataclass(frozen=True, slots=True)
class ZiniaoLaunchProof:
    """Unforgeable-by-configuration link to one ``startBrowser`` response.

    The controller creates this value only after Ziniao returns the dynamic
    debugging port.  It is intentionally not accepted by any web/API schema;
    the session manager requires the controller-owned object by identity, so a
    manually entered or ordinary-browser CDP endpoint cannot enter a workflow.
    """

    selector: ProfileSelector
    debugging_host: str
    debugging_port: int
    nonce: UUID


@dataclass(slots=True)
class ZiniaoBrowserHandle:
    """A connected Playwright view of one Ziniao store.

    ``close`` is supplied by :class:`CdpSessionManager`.  Callers normally use
    ``ZiniaoController.session`` so cleanup cannot be forgotten.
    """

    selector: ProfileSelector
    debugging_host: str
    debugging_port: int
    browser: Any
    context: Any
    page: Any
    health: CdpHealth
    launch_proof: ZiniaoLaunchProof
    _close_callback: Any = field(default=None, repr=False, compare=False)
    _closed: bool = field(default=False, repr=False)
    _paused: bool = field(default=False, repr=False)
    _closing: bool = field(default=False, repr=False)

    @property
    def browser_oauth(self) -> str | None:
        """OAuth value when the profile really is OAuth-selected."""
        return self.selector.value if self.selector.type == "oauth" else None

    @property
    def selector_type(self) -> Literal["oauth", "id"]:
        return self.selector.type

    @property
    def selector_value(self) -> str:
        return self.selector.value

    def pause(self) -> None:
        """Keep the live page/lock while a person completes authentication.

        The controller owns the 30-minute lease and later calls ``resume`` or
        ``expire``. Merely leaving a session context will not close a paused
        handle.
        """
        if self._closed:
            raise RuntimeError("closed browser handle cannot be paused")
        self._paused = True

    def resume(self) -> None:
        if self._closed:
            raise RuntimeError("closed browser handle cannot be resumed")
        self._paused = False

    async def close(self) -> None:
        if self._closed or self._paused or self._closing:
            return
        self._closing = True
        try:
            if self._close_callback is not None:
                await self._close_callback(self)
        except BaseException:
            # Cleanup can be retried. In particular, cancellation halfway
            # through disconnect must not permanently leak the store lock.
            self._closing = False
            raise
        else:
            self._closed = True
            self._closing = False

    async def expire(self) -> None:
        """Force cleanup of a paused authentication session."""
        self._paused = False
        await self.close()

    async def __aenter__(self) -> "ZiniaoBrowserHandle":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()
