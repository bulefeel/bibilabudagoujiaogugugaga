"""Add the per-feedback removal ledger.

Revision ID: 0008
Revises: 0007

``0001`` builds tables from live ORM metadata, so a fresh install already has
``feedback_reviews``; this revision only has to bring an installed database
stamped at ``0007`` up to the same shape.  Nothing existing is rebuilt — the
new table's foreign keys point outward at ``runs`` and ``stores``, so no
audit-link delete actions can fire and the 0006-style rebuild transaction is
not needed.

The unique constraint is the point of the table: Amazon accepts a removal
request for a given feedback exactly once, and the workflow must never submit
a second one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_indexes(table_name)
        if item.get("name")
    }


def upgrade() -> None:
    if "feedback_reviews" not in _tables():
        op.create_table(
            "feedback_reviews",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=True),
            sa.Column("store_id", sa.Integer(), nullable=False),
            sa.Column("marketplace_code", sa.String(length=2), nullable=False),
            sa.Column("order_id", sa.String(length=40), nullable=False),
            sa.Column("rating", sa.Integer(), nullable=False),
            sa.Column("order_date", sa.String(length=20), nullable=True),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("category", sa.String(length=40), nullable=True),
            sa.Column("reason_code", sa.String(length=8), nullable=True),
            sa.Column("decision_source", sa.String(length=16), nullable=True),
            sa.Column("decision_note", sa.Text(), nullable=True),
            sa.Column("state", sa.String(length=24), nullable=False),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_feedback_review_rating"),
            sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "store_id",
                "marketplace_code",
                "order_id",
                name="uq_feedback_review_order",
            ),
        )

    existing = _indexes("feedback_reviews")
    for name, column in (
        ("ix_feedback_reviews_run_id", "run_id"),
        ("ix_feedback_reviews_store_id", "store_id"),
        ("ix_feedback_reviews_state", "state"),
    ):
        if name not in existing:
            op.create_index(name, "feedback_reviews", [column])


def downgrade() -> None:
    if "feedback_reviews" in _tables():
        op.drop_table("feedback_reviews")
