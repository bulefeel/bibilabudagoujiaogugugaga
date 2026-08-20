"""Drop the cycle-wide unique key on operation_guards.

``uq_financial_operation`` made (store, workflow, marketplace, settlement_key)
unique.  ``guard_key`` — already UNIQUE — hashes those same four values PLUS the
operator-local disbursement day, and it is the invariant this system wants: at
most one payout per site per day.  The cycle-wide key enforced something
stricter that nobody intended, and it deadlocked: an Amazon settlement cycle
only rolls over once a payout succeeds, so any leftover guard blocked the very
payout that would have cleared it and the site became permanently unpayable.

SQLite cannot drop a constraint, so the table is rebuilt.  ``copy_from`` spells
out every column, CHECK and foreign key explicitly rather than reflecting them,
because the SQLite dialect does not reflect CHECK constraints — a reflected
rebuild would silently drop ``ck_guard_state`` and ``ck_guard_amount`` from a
table that holds money.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _guard_table(*, with_cycle_unique: bool) -> sa.Table:
    """The exact shape of operation_guards, parameterised on the one change."""

    constraints: list[sa.schema.SchemaItem] = [
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guard_key"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["site_run_id"], ["site_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "state IN ('ARMED','SUBMITTED','CONFIRMED','UNCERTAIN','CANCELLED')",
            name="ck_guard_state",
        ),
        sa.CheckConstraint("amount >= 0", name="ck_guard_amount"),
    ]
    if with_cycle_unique:
        constraints.append(
            sa.UniqueConstraint(
                "store_id",
                "workflow",
                "marketplace_code",
                "settlement_key",
                name="uq_financial_operation",
            )
        )
    return sa.Table(
        "operation_guards",
        sa.MetaData(),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("guard_key", sa.String(255), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("site_run_id", sa.String(36), nullable=False),
        sa.Column("store_id", sa.Integer, nullable=False),
        sa.Column("workflow", sa.String(80), nullable=False),
        sa.Column("marketplace_code", sa.String(2), nullable=False),
        sa.Column("settlement_key", sa.String(180), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("armed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("failure_reason", sa.Text),
        sa.Column("metadata", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        *constraints,
        # Declared here because a batch rebuild recreates only what ``copy_from``
        # describes; omitting them silently drops the indexes that
        # ``list_operations`` and the startup recovery scan rely on.
        sa.Index("ix_operation_guards_run_id", "run_id"),
        sa.Index("ix_operation_guards_site_run_id", "site_run_id"),
        sa.Index("ix_operation_guards_state", "state"),
    )


def _has_cycle_unique(bind: sa.engine.Connection) -> bool:
    """Read the stored DDL rather than reflecting.

    SQLAlchemy's SQLite reflection does not reliably report a *named*
    table-level UNIQUE constraint — on a hand-written CREATE TABLE it returns
    only the inline ``UNIQUE (guard_key)`` — so a reflection-based check made
    this migration a silent no-op on exactly the databases that need it.
    ``sqlite_master.sql`` is the source of truth and is unambiguous.
    """

    ddl = bind.scalar(
        sa.text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'operation_guards'"
        )
    )
    return bool(ddl) and "uq_financial_operation" in ddl


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_cycle_unique(bind):
        return
    with op.batch_alter_table(
        "operation_guards", copy_from=_guard_table(with_cycle_unique=True)
    ) as batch:
        batch.drop_constraint("uq_financial_operation", type_="unique")


def downgrade() -> None:
    bind = op.get_bind()
    if "operation_guards" not in sa.inspect(bind).get_table_names():
        return
    if _has_cycle_unique(bind):
        return
    with op.batch_alter_table(
        "operation_guards", copy_from=_guard_table(with_cycle_unique=False)
    ) as batch:
        batch.create_unique_constraint(
            "uq_financial_operation",
            ["store_id", "workflow", "marketplace_code", "settlement_key"],
        )
