"""Normalize legacy schedule next-run values to UTC.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from alembic import op


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _parse(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _zone(value: object):
    try:
        return ZoneInfo(str(value or "Asia/Singapore"))
    except ZoneInfoNotFoundError:
        return timezone.utc


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "schedules" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("schedules")}
    if not {"id", "timezone", "next_run_at"}.issubset(columns):
        return
    rows = bind.execute(
        sa.text("SELECT id, timezone, next_run_at FROM schedules WHERE next_run_at IS NOT NULL")
    ).all()
    for schedule_id, timezone_name, raw_value in rows:
        value = _parse(raw_value)
        if value is None:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=_zone(timezone_name))
        utc_value = value.astimezone(timezone.utc).replace(tzinfo=None)
        bind.execute(
            sa.text("UPDATE schedules SET next_run_at = :value WHERE id = :id"),
            {"value": utc_value, "id": schedule_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "schedules" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("schedules")}
    if not {"id", "timezone", "next_run_at"}.issubset(columns):
        return
    rows = bind.execute(
        sa.text("SELECT id, timezone, next_run_at FROM schedules WHERE next_run_at IS NOT NULL")
    ).all()
    for schedule_id, timezone_name, raw_value in rows:
        value = _parse(raw_value)
        if value is None:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        local_value = value.astimezone(_zone(timezone_name)).replace(tzinfo=None)
        bind.execute(
            sa.text("UPDATE schedules SET next_run_at = :value WHERE id = :id"),
            {"value": local_value, "id": schedule_id},
        )
