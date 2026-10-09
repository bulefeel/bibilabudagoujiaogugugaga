"""Durable, non-financial onboarding records and configuration revisions.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

STATEMENTS = (
    "\nCREATE TABLE store_setup_probes (\n\tid VARCHAR(36) NOT NULL, \n\tstore_id INTEGER NOT NULL, \n\tkind VARCHAR(16) NOT NULL, \n\trequest_id VARCHAR(36), \n\trequest_fingerprint VARCHAR(64) NOT NULL, \n\trequest_aliases JSON NOT NULL, \n\tresource_snapshot JSON NOT NULL, \n\tsetup_revision INTEGER NOT NULL, \n\tconfig_snapshot JSON NOT NULL, \n\trequested_marketplace_codes JSON NOT NULL, \n\tscope VARCHAR(24) NOT NULL, \n\tadapter_version VARCHAR(64) NOT NULL, \n\tpolicy_version VARCHAR(64) NOT NULL, \n\tlifecycle VARCHAR(20) NOT NULL, \n\tphase VARCHAR(24) NOT NULL, \n\tresume_phase VARCHAR(24), \n\tcurrent_marketplace VARCHAR(2), \n\toutcome VARCHAR(24), \n\tidentity_result JSON NOT NULL, \n\treason_code VARCHAR(64), \n\tmessage VARCHAR(400) NOT NULL, \n\tdiagnostics JSON NOT NULL, \n\tcleanup_state VARCHAR(16) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tstarted_at DATETIME, \n\tdeadline_at DATETIME NOT NULL, \n\tupdated_at DATETIME NOT NULL, \n\tfinished_at DATETIME, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_setup_request UNIQUE (store_id, request_id), \n\tCHECK (kind IN ('unified','identity','marketplace')), \n\tCHECK (scope IN ('full','retry_unverified')), \n\tCHECK (outcome IS NULL OR outcome IN ('SUCCEEDED','PARTIAL','FAILED','NEEDS_REVIEW','CANCELLED','TIMED_OUT','INTERRUPTED')), \n\tCHECK ((lifecycle = 'FINISHED' AND outcome IS NOT NULL) OR (lifecycle != 'FINISHED' AND outcome IS NULL)), \n\tCHECK (lifecycle IN ('QUEUED','CHECKING','WAITING_AUTH','FINISHED')), \n\tCHECK (cleanup_state IN ('PENDING','CONFIRMED','UNCONFIRMED')), \n\tCHECK (setup_revision >= 0), \n\tFOREIGN KEY(store_id) REFERENCES stores (id) ON DELETE CASCADE\n)\n\n",
    'CREATE INDEX ix_setup_history ON store_setup_probes (store_id, created_at)',
    "CREATE UNIQUE INDEX uq_setup_active_store ON store_setup_probes (store_id) WHERE lifecycle != 'FINISHED'",
    "\nCREATE TABLE store_setup_site_results (\n\tid INTEGER NOT NULL, \n\tprobe_id VARCHAR(36) NOT NULL, \n\tmarketplace_code VARCHAR(2) NOT NULL, \n\tattempted BOOLEAN NOT NULL, \n\toutcome VARCHAR(16), \n\tidentity_check VARCHAR(16) NOT NULL, \n\taccess_check VARCHAR(16) NOT NULL, \n\tpayment_capability VARCHAR(24) NOT NULL, \n\treason_code VARCHAR(64), \n\tmessage VARCHAR(400) NOT NULL, \n\tpage_category VARCHAR(32) NOT NULL, \n\tdomain VARCHAR(255) NOT NULL, \n\tadapter_version VARCHAR(64) NOT NULL, \n\tevidence_state VARCHAR(16) NOT NULL, \n\tstarted_at DATETIME, \n\tfinished_at DATETIME, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_setup_site UNIQUE (probe_id, marketplace_code), \n\tCHECK (marketplace_code IN ('CA','UK','AU')), \n\tCHECK (outcome IS NULL OR outcome IN ('PASSED','BLOCKED','FAILED','NOT_RUN','CANCELLED')), \n\tFOREIGN KEY(probe_id) REFERENCES store_setup_probes (id) ON DELETE CASCADE\n)\n\n",
    'CREATE TRIGGER IF NOT EXISTS setup_store_revision AFTER UPDATE OF\n    selector_type, selector_value, expected_seller_id, identity_confirmed ON stores\n    WHEN OLD.selector_type IS NOT NEW.selector_type OR OLD.selector_value IS NOT NEW.selector_value\n      OR OLD.expected_seller_id IS NOT NEW.expected_seller_id OR OLD.identity_confirmed IS NOT NEW.identity_confirmed\n    BEGIN UPDATE stores SET setup_revision = setup_revision + 1 WHERE id = NEW.id; END',
    'CREATE TRIGGER IF NOT EXISTS setup_site_insert AFTER INSERT ON store_marketplaces\n    BEGIN UPDATE stores SET setup_revision = setup_revision + 1 WHERE id = NEW.store_id; END',
    'CREATE TRIGGER IF NOT EXISTS setup_site_delete AFTER DELETE ON store_marketplaces\n    BEGIN UPDATE stores SET setup_revision = setup_revision + 1 WHERE id = OLD.store_id; END',
    'CREATE TRIGGER IF NOT EXISTS setup_site_update AFTER UPDATE ON store_marketplaces\n    WHEN OLD.code IS NOT NEW.code OR OLD.domain IS NOT NEW.domain OR OLD.currency IS NOT NEW.currency\n      OR OLD.enabled IS NOT NEW.enabled OR OLD.store_id IS NOT NEW.store_id\n    BEGIN UPDATE stores SET setup_revision = setup_revision + 1 WHERE id IN (OLD.store_id, NEW.store_id); END',
)

def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("stores")}
    if "setup_revision" not in columns:
        op.add_column("stores", sa.Column("setup_revision", sa.Integer(), nullable=False, server_default="0"))
    for statement in STATEMENTS:
        statement = statement.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ").replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ").replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
        op.execute(statement)

def downgrade():
    for name in ("setup_store_revision", "setup_site_insert", "setup_site_delete", "setup_site_update"):
        op.execute("DROP TRIGGER IF EXISTS " + name)
    op.drop_table("store_setup_site_results")
    op.drop_table("store_setup_probes")
    op.drop_column("stores", "setup_revision")
