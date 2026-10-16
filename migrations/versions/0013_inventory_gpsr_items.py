"""Inventory GPSR candidate snapshots and durable non-financial submission intent.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    # The original baseline creates current ORM metadata for fresh installations.
    if "inventory_gpsr_items" in sa.inspect(op.get_bind()).get_table_names():
        _ensure_indexes()
        return
    op.create_table(
        "inventory_gpsr_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="SET NULL")),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("store_history.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("marketplace_code", sa.String(2), nullable=False),
        sa.Column("sku", sa.String(255), nullable=False),
        sa.Column("asin", sa.String(10), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(120), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("intent_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "marketplace_code", "sku", name="uq_inventory_gpsr_run_sku"),
        sa.CheckConstraint("marketplace_code = 'DE'", name="ck_inventory_gpsr_marketplace"),
        sa.CheckConstraint("page_number >= 1 AND position >= 0", name="ck_inventory_gpsr_position"),
        sa.CheckConstraint(
            "state IN ('PENDING','ELIGIBLE','NO_VIOLATION_ENTRY','NO_ATTESTATION',"
            "'ALREADY_SUBMITTED','MISSING','FAILED','SUBMITTING','CONFIRMED','UNCERTAIN','BLOCKED')",
            name="ck_inventory_gpsr_state",
        ),
        sa.CheckConstraint(
            "state NOT IN ('SUBMITTING','UNCERTAIN','CONFIRMED') OR intent_at IS NOT NULL",
            name="ck_inventory_gpsr_intent",
        ),
    )
    _ensure_indexes()


def _ensure_indexes():
    # SQLite DDL may survive an interrupted migration. Retrying must restore
    # the submission barrier even when CREATE TABLE already completed.
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("inventory_gpsr_items")}
    for name, columns, unique in (
        ("ix_inventory_gpsr_run_order", ["run_id", "marketplace_code", "page_number", "position"], False),
        ("ix_inventory_gpsr_history", ["store_id", "marketplace_code", "sku", "asin"], False),
        ("uq_inventory_gpsr_submission", ["store_id", "marketplace_code", "sku", "asin"], True),
    ):
        if name not in existing:
            kwargs = {"sqlite_where": sa.text("state IN ('SUBMITTING','UNCERTAIN','CONFIRMED')")} if unique else {}
            op.create_index(name, "inventory_gpsr_items", columns, unique=unique, **kwargs)


def downgrade():
    has_intent = op.get_bind().scalar(sa.text(
        "SELECT 1 FROM inventory_gpsr_items "
        "WHERE state IN ('SUBMITTING','UNCERTAIN','CONFIRMED') LIMIT 1"
    ))
    if has_intent:
        raise RuntimeError("存在库存安全证明提交记录，降级会丢失防重复依据；请保留当前数据库和备份")
    op.drop_table("inventory_gpsr_items")
