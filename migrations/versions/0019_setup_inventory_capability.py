"""Keep optional inventory readiness separate from basic site verification.

Historical passes remain valid; no historical inventory capability is invented.
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    # The initial migrations create some tables from current ORM metadata.
    # Fresh databases therefore already contain this column; existing 0018
    # databases do not. Validate the pre-existing shape rather than add twice.
    columns = {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("store_setup_site_results")}
    if "inventory_capability" in columns:
        column = columns["inventory_capability"]
        if (not isinstance(column["type"], sa.String) or column["type"].length != 24
                or column["nullable"] or str(column["default"]).strip("'\"") != "NOT_CHECKED"):
            raise RuntimeError("Unexpected inventory_capability schema")
        return
    op.add_column("store_setup_site_results", sa.Column(
        "inventory_capability", sa.String(24), nullable=False, server_default="NOT_CHECKED"))


def downgrade():
    op.drop_column("store_setup_site_results", "inventory_capability")
