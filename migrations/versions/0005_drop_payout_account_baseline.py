"""Stop storing a payout-account baseline; record the observed tail instead.

Amazon owns where a disbursement goes.  This automation only presses Request
disbursement — it can neither select nor edit a destination — so a locally
stored "expected" tail could only ever refuse a payout, never redirect one.  In
practice it refused three legitimate payouts and caught nothing, so the two
columns that held it are removed from ``store_marketplaces``.

The tail Amazon actually displayed is still worth having, as evidence rather
than as a gate, so ``operation_guards`` gains ``payout_account_tail``.  It lives
on the money record because that is what a disputed transfer is traced through,
and as a column rather than inside ``metadata`` because the notification builder
deliberately never reads guard metadata.

Plain ``ALTER TABLE ... DROP COLUMN`` is used rather than Alembic's batch
rebuild.  SQLite has supported it since 3.35 and the runtime here ships 3.47.
The rebuild path is actively worse for this table: ``site_runs.marketplace_id``
references it, so dropping the old copy trips the foreign key, and a rebuild
only recreates the constraints and indexes it is explicitly told about.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _columns(bind: sa.engine.Connection, table: str) -> set[str] | None:
    """Columns of ``table``, or ``None`` when the table does not exist.

    ``None`` and "no such column" are different answers: an installation that
    predates a table must be left alone, not have a column bolted onto nothing.
    """

    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return None
    return {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    guards = _columns(bind, "operation_guards")
    if guards is not None and "payout_account_tail" not in guards:
        op.add_column(
            "operation_guards",
            sa.Column("payout_account_tail", sa.String(8), nullable=True),
        )

    existing = _columns(bind, "store_marketplaces") or set()
    for column in ("expected_payment_account", "verified_at"):
        if column in existing:
            op.drop_column("store_marketplaces", column)


def downgrade() -> None:
    bind = op.get_bind()

    existing = _columns(bind, "store_marketplaces")
    if existing is not None and "expected_payment_account" not in existing:
        op.add_column(
            "store_marketplaces",
            sa.Column("expected_payment_account", sa.String(180), nullable=True),
        )
        op.add_column(
            "store_marketplaces",
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "payout_account_tail" in (_columns(bind, "operation_guards") or set()):
        op.drop_column("operation_guards", "payout_account_tail")
