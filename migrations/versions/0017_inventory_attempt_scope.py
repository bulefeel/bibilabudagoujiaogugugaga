"""Keep historical attempts; submission uniqueness is per run, not forever."""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

INDEXES = (
    ("inventory_gpsr_items", "uq_inventory_gpsr_submission",
     ["store_id", "marketplace_code", "sku", "asin"]),
    ("inventory_gpsr_requirements", "uq_inventory_requirement_submission",
     ["store_id", "marketplace_code", "sku", "asin", "requirement_type"]),
)
INTENTS = "state IN ('SUBMITTING','UNCERTAIN','CONFIRMED')"


def rewrite(*, per_run):
    for table, name, columns in INDEXES:
        op.drop_index(name, table_name=table)
        op.create_index(name, table, (["run_id"] if per_run else []) + columns,
                        unique=True, sqlite_where=sa.text(INTENTS))


def upgrade():
    # Only index DDL; no row, timestamp, foreign key or historical status changes.
    bind = op.get_bind()
    if not bind.connection.driver_connection.in_transaction:
        bind.exec_driver_sql("BEGIN IMMEDIATE")
    rewrite(per_run=True)


def downgrade():
    bind = op.get_bind()
    for table, name, columns in INDEXES:
        if bind.exec_driver_sql(
            f"SELECT 1 FROM {table} WHERE {INTENTS} GROUP BY {','.join(columns)} HAVING count(*) > 1 LIMIT 1"
        ).first():
            raise RuntimeError("已有跨任务提交尝试，请保留历史记录及备份，不执行有损降级")
    if not bind.connection.driver_connection.in_transaction:
        bind.exec_driver_sql("BEGIN IMMEDIATE")
    rewrite(per_run=False)
