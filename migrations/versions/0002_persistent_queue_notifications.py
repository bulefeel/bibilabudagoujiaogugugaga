"""Add durable FIFO queue and notification delivery receipts.

Revision ID: 0002
Revises: 0001

``0001`` intentionally creates tables from the application's live metadata.
Consequently a brand-new database may already contain this revision's schema
by the time Alembic reaches 0002, while an installed 0001 database does not.
Every DDL operation below is therefore guarded by SQLite introspection.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {item["name"] for item in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "scheduled_for_at" not in _columns("runs"):
        op.add_column(
            "runs",
            sa.Column("scheduled_for_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "run_queue_entries" not in tables:
        op.create_table(
            "run_queue_entries",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("action", sa.String(length=24), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("scheduled_for_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "enqueued_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "available_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column("claim_token", sa.String(length=64), nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "cross_day_notified_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "action IN ('START','APPROVE','CONTINUE_AUTH','RECONCILE')",
                name="ck_run_queue_action",
            ),
            sa.CheckConstraint(
                "priority IN (0,10,100)", name="ck_run_queue_priority"
            ),
            sa.CheckConstraint(
                "state IN ('READY','CLAIMED','DONE','CANCELLED')",
                name="ck_run_queue_state",
            ),
            sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    if "notification_deliveries" not in tables:
        op.create_table(
            "notification_deliveries",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("site_run_id", sa.String(length=36), nullable=True),
            sa.Column("dedupe_key", sa.String(length=255), nullable=False),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('PENDING','SENT','FAILED')",
                name="ck_notification_delivery_status",
            ),
            sa.CheckConstraint(
                "attempts >= 0", name="ck_notification_delivery_attempts"
            ),
            sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["site_run_id"], ["site_runs.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("dedupe_key", name="uq_notification_deliveries_dedupe_key"),
        )

    # Re-inspect because one or both tables may have just been created.
    if "uq_runs_schedule_occurrence" not in _indexes("runs"):
        op.create_index(
            "uq_runs_schedule_occurrence",
            "runs",
            ["schedule_id", "scheduled_for_at"],
            unique=True,
            sqlite_where=sa.text(
                "schedule_id IS NOT NULL AND scheduled_for_at IS NOT NULL"
            ),
        )
    if "uq_run_queue_active_run" not in _indexes("run_queue_entries"):
        op.create_index(
            "uq_run_queue_active_run",
            "run_queue_entries",
            ["run_id"],
            unique=True,
            sqlite_where=sa.text("state IN ('READY','CLAIMED')"),
        )
    if "ix_run_queue_ready_order" not in _indexes("run_queue_entries"):
        op.create_index(
            "ix_run_queue_ready_order",
            "run_queue_entries",
            ["state", "priority", "scheduled_for_at", "enqueued_at", "id"],
            unique=False,
        )
    if "ix_notification_deliveries_run_id" not in _indexes("notification_deliveries"):
        op.create_index(
            "ix_notification_deliveries_run_id",
            "notification_deliveries",
            ["run_id"],
            unique=False,
        )
    if "ix_notification_deliveries_site_run_id" not in _indexes(
        "notification_deliveries"
    ):
        op.create_index(
            "ix_notification_deliveries_site_run_id",
            "notification_deliveries",
            ["site_run_id"],
            unique=False,
        )
    if "ix_notification_delivery_status" not in _indexes("notification_deliveries"):
        op.create_index(
            "ix_notification_delivery_status",
            "notification_deliveries",
            ["status", "created_at"],
            unique=False,
        )

    # Only pre-existing QUEUED runs are safe to replay.  Approval waits and all
    # terminal states deliberately remain outside the queue.  created_at is the
    # closest durable approximation of enqueue time for legacy records.
    bind.execute(
        sa.text(
            """
            INSERT INTO run_queue_entries (
                run_id, action, priority, state, scheduled_for_at,
                enqueued_at, available_at, created_at, updated_at
            )
            SELECT
                r.id,
                'START',
                CASE
                    WHEN r.trigger = 'recovery' THEN 0
                    WHEN r.trigger = 'schedule' THEN 100
                    ELSE 10
                END,
                'READY',
                r.scheduled_for_at,
                r.created_at,
                r.created_at,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM runs AS r
            WHERE r.status = 'QUEUED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM run_queue_entries AS q
                  WHERE q.run_id = r.id
                    AND q.state IN ('READY','CLAIMED')
              )
            """
        )
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "notification_deliveries" in tables:
        op.drop_table("notification_deliveries")
    if "run_queue_entries" in tables:
        op.drop_table("run_queue_entries")

    if "runs" in tables:
        if "uq_runs_schedule_occurrence" in _indexes("runs"):
            op.drop_index("uq_runs_schedule_occurrence", table_name="runs")
        if "scheduled_for_at" in _columns("runs"):
            with op.batch_alter_table("runs") as batch_op:
                batch_op.drop_column("scheduled_for_at")
