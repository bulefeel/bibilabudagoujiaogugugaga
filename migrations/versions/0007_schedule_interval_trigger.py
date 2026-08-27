"""Replace the weekly cron schedule with an absolute anchor plus a fixed period.

Amazon caps on-demand disbursement at once per ROLLING 24 hours, counted from
the previous request rather than from midnight.  A cron at 09:00 daily fires a
few seconds short of that window every single day, so Seller Central refused it
and the schedule only ever succeeded every other day.  No wall-clock schedule
can avoid this; the period has to exceed 24 hours, which means the firing time
must be allowed to walk forward.

So ``local_time`` and ``days_of_week`` go away and ``schedules`` gains
``first_run_at`` (an absolute instant) and ``interval_minutes``.  Weekday-only
scheduling is lost with them — deliberately, on the operator's instruction.

``timezone`` stays.  It is no longer a trigger input (an absolute anchor is
immune to DST) but notifications render each run's times in its schedule's zone.

Backfill keeps every existing rule pointing at the same wall-clock time it
already used: the first occurrence of its old ``local_time`` strictly after this
migration runs, in its own timezone.  The period becomes 1440 minutes, i.e. the
behaviour operators already have — changing it to something that clears the
24-hour window is a deliberate per-schedule decision, not something a migration
should make on their behalf.

``next_run_at`` is cleared for every row.  Startup projection recomputes it, and
wiping it also retires the pre-0003 rows that stored a local wall clock in a UTC
column — the heuristic that used to detect them keyed on ``local_time``, which
no longer exists.

Plain ``ALTER TABLE ... DROP COLUMN`` as in 0005: SQLite has supported it since
3.35 and the runtime here ships 3.47.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from alembic import op


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_DEFAULT_INTERVAL_MINUTES = 1440
_FALLBACK_ZONE = "Asia/Singapore"


def _columns(bind: sa.engine.Connection, table: str) -> set[str] | None:
    """Columns of ``table``, or ``None`` when the table does not exist."""

    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return None
    return {item["name"] for item in inspector.get_columns(table)}


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or _FALLBACK_ZONE))
    except (ZoneInfoNotFoundError, ValueError):
        # A rule with an unreadable timezone must still migrate; it would
        # otherwise block every other schedule on this installation.
        return ZoneInfo(_FALLBACK_ZONE)


def _next_wall_clock(local_time: str | None, zone_name: str | None, now: datetime) -> datetime:
    """First occurrence of ``HH:MM`` strictly after ``now``, returned as UTC."""

    try:
        hour_text, minute_text = str(local_time or "09:00").split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (AttributeError, ValueError):
        hour, minute = 9, 0
    zone = _zone(zone_name)
    local_now = now.astimezone(zone)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "schedules")
    if columns is None:
        return

    if "interval_minutes" not in columns:
        op.add_column(
            "schedules",
            sa.Column(
                "interval_minutes",
                sa.Integer(),
                nullable=False,
                server_default=sa.text(str(_DEFAULT_INTERVAL_MINUTES)),
            ),
        )
    # Nullable first: the anchor is computed per row below, and a NOT NULL
    # column cannot be added to a populated table without inventing one value
    # for every rule.
    if "first_run_at" not in columns:
        op.add_column(
            "schedules", sa.Column("first_run_at", sa.DateTime(timezone=True), nullable=True)
        )

    now = datetime.now(timezone.utc)
    # Read only what this table actually has. An installation stamped at an
    # older revision may predate either cron column or even ``timezone``, and a
    # migration must not assume the shape of columns it does not own.
    selected_time = "local_time" if "local_time" in columns else "'09:00' AS local_time"
    selected_zone = (
        "timezone" if "timezone" in columns else f"'{_FALLBACK_ZONE}' AS timezone"
    )
    rows = bind.execute(
        sa.text(f"SELECT id, {selected_time}, {selected_zone} FROM schedules")
    ).fetchall()
    for row in rows:
        bind.execute(
            sa.text(
                "UPDATE schedules SET first_run_at = :anchor"
                + (", next_run_at = NULL" if "next_run_at" in columns else "")
                + " WHERE id = :id"
            ),
            {"anchor": _next_wall_clock(row.local_time, row.timezone, now), "id": row.id},
        )
    # Rows with no cron columns to read from still need an anchor.
    bind.execute(
        sa.text(
            "UPDATE schedules SET first_run_at = :anchor WHERE first_run_at IS NULL"
        ),
        {"anchor": now},
    )

    # first_run_at stays nullable on upgraded databases for the same reason the
    # CHECK is absent: enforcing it means rebuilding a table that runs.schedule_id
    # references. Every row has just been given a value above, and both the ORM
    # and the API require one, so a NULL can only arrive by hand-editing SQLite.
    for column in ("local_time", "days_of_week"):
        if column in columns:
            op.drop_column("schedules", column)


def downgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "schedules")
    if columns is None:
        return

    if "local_time" not in columns:
        op.add_column(
            "schedules",
            sa.Column(
                "local_time", sa.String(length=5), nullable=False, server_default="09:00"
            ),
        )
    if "days_of_week" not in columns:
        # Every day, not the old mon-fri default: an interval rule has been
        # firing on weekends too, and narrowing it silently would skip runs.
        op.add_column(
            "schedules",
            sa.Column("days_of_week", sa.String(length=32), nullable=True, server_default="*"),
        )

    for row in bind.execute(
        sa.text("SELECT id, first_run_at, timezone FROM schedules")
    ).fetchall():
        anchor = row.first_run_at
        if isinstance(anchor, str):
            anchor = datetime.fromisoformat(anchor)
        if anchor is None:
            continue
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        local = anchor.astimezone(_zone(row.timezone))
        bind.execute(
            sa.text(
                "UPDATE schedules SET local_time = :value, days_of_week = '*', "
                "next_run_at = NULL WHERE id = :id"
            ),
            {"value": f"{local.hour:02d}:{local.minute:02d}", "id": row.id},
        )

    for column in ("first_run_at", "interval_minutes"):
        if column in columns:
            op.drop_column("schedules", column)
