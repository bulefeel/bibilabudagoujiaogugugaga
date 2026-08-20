"""Timezone-safe helpers for human-facing console timestamps.

Persistence stays in UTC.  SQLite commonly returns those values without a
``tzinfo`` even when the SQLAlchemy column declares ``timezone=True``; such
values must therefore be interpreted as UTC rather than as the Windows local
timezone before they are formatted for the operator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_DISPLAY_TIMEZONE = "Asia/Singapore"


def display_timezone(name: str | None = None) -> ZoneInfo:
    """Resolve a configured IANA timezone, falling back to the V1 UTC+8 zone."""

    try:
        return ZoneInfo(name or DEFAULT_DISPLAY_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(DEFAULT_DISPLAY_TIMEZONE)


def to_display_datetime(
    value: datetime | None,
    timezone_name: str | None = None,
) -> datetime | None:
    """Convert a persisted UTC timestamp into the operator display timezone."""

    if value is None:
        return None
    # SQLite drops timezone metadata.  Every persisted application timestamp
    # is UTC, so attaching UTC here restores its meaning without changing the
    # instant stored in the database.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(display_timezone(timezone_name))


def format_display_datetime(
    value: datetime | None,
    fmt: str = "%Y-%m-%d %H:%M:%S",
    timezone_name: str | None = None,
    empty: str = "—",
) -> str:
    """Format a UTC timestamp for HTML pages without mutating the source."""

    converted = to_display_datetime(value, timezone_name)
    return empty if converted is None else converted.strftime(fmt)
