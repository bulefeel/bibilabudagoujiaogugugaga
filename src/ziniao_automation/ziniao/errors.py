"""Exceptions raised by the Ziniao adapter."""

from __future__ import annotations

from typing import Any


class ZiniaoError(RuntimeError):
    """Base error for the local Ziniao integration."""


class ZiniaoConnectionError(ZiniaoError):
    """The local WebDriver HTTP service could not be reached."""


class ZiniaoCredentialError(ZiniaoError):
    """The current production credential snapshot is incomplete or unreadable."""


class ZiniaoApiError(ZiniaoError):
    """Ziniao returned a non-success status code."""

    def __init__(self, action: str, status_code: str, message: str = "") -> None:
        self.action = action
        self.status_code = status_code
        self.api_message = message
        suffix = f": {message}" if message else ""
        super().__init__(f"Ziniao action {action!r} failed ({status_code}){suffix}")


class ZiniaoLaunchError(ZiniaoError):
    """A single store environment did not become healthy."""


class CdpHealthError(ZiniaoLaunchError):
    """Chrome CDP exists but failed the required health checks."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.details = details or {}
        super().__init__(message)


class AuthWaitExpired(ZiniaoError):
    """The 30-minute human-auth lease expired."""


class AuthWaitCancelled(ZiniaoError):
    """A human-auth lease was cancelled from the administration UI."""
