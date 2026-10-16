"""EU inventory sites and per-requirement submission ledger; preserve old intents."""
from contextlib import contextmanager

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


@contextmanager
def rebuild():
    bind = op.get_bind()
    if bind.in_transaction():
        bind.commit()
    original = int(bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    bind.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    bind.commit()
    try:
        bind.exec_driver_sql("BEGIN IMMEDIATE")
        yield bind
        if bind.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Inventory migration foreign-key validation failed")
        bind.commit()
    except BaseException:
        bind.rollback()
        raise
    finally:
        bind.exec_driver_sql(f"PRAGMA foreign_keys={original}")
        bind.commit()


def rewrite(table, name, condition):
    bind = op.get_bind()
    # SQLite drops table triggers on rebuild; history mirrors must survive it.
    triggers = bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=:table"
    ), {"table": table}).scalars().all()
    ddl = bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=:table"
    ), {"table": table}).scalar_one()
    autoincrement = "AUTOINCREMENT" in ddl.upper()
    sequence = bind.execute(sa.text(
        "SELECT seq FROM sqlite_sequence WHERE name=:table"
    ), {"table": table}).scalar() if autoincrement else None
    with op.batch_alter_table(table, recreate="always", table_kwargs={
        "sqlite_autoincrement": autoincrement,
    }) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, condition)
    for trigger in triggers:
        bind.exec_driver_sql(trigger)
    if sequence is not None:
        bind.execute(sa.text(
            "UPDATE sqlite_sequence SET seq=MAX(seq,:seq) WHERE name=:table"
        ), {"table": table, "seq": sequence})


def upgrade():
    with rebuild() as bind:
        rewrite("store_marketplaces", "ck_v1_marketplace_code", "code IN ('CA','UK','AU','DE','IT','FR','ES','PL')")
        rewrite("inventory_gpsr_items", "ck_inventory_gpsr_marketplace", "marketplace_code IN ('DE','IT','FR','ES','PL')")
        if "inventory_gpsr_requirements" not in sa.inspect(bind).get_table_names():
            op.create_table(
                "inventory_gpsr_requirements",
                sa.Column("id", sa.String(36), primary_key=True),
                sa.Column("item_id", sa.String(36), sa.ForeignKey("inventory_gpsr_items.id", ondelete="RESTRICT"), nullable=False),
                sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="SET NULL")),
                sa.Column("store_id", sa.Integer, sa.ForeignKey("store_history.id", ondelete="RESTRICT"), nullable=False),
                sa.Column("marketplace_code", sa.String(2), nullable=False),
                sa.Column("sku", sa.String(255), nullable=False),
                sa.Column("asin", sa.String(10), nullable=False),
                sa.Column("requirement_type", sa.String(32), nullable=False),
                sa.Column("state", sa.String(32), nullable=False),
                sa.Column("reason", sa.String(120), nullable=False),
                sa.Column("eligible", sa.Boolean, nullable=False),
                sa.Column("initial_selected", sa.Boolean),
                *[sa.Column(c, sa.DateTime(timezone=True), nullable=c not in {"created_at", "updated_at"})
                  for c in ("intent_at", "submitted_at", "confirmed_at", "created_at", "updated_at")],
                sa.UniqueConstraint("item_id", "requirement_type", name="uq_inventory_requirement_item_type"),
                sa.CheckConstraint("marketplace_code IN ('DE','IT','FR','ES','PL')", name="ck_inventory_requirement_marketplace"),
                sa.CheckConstraint("requirement_type IN ('safety_attestation','responsible_person')", name="ck_inventory_requirement_type"),
                sa.CheckConstraint("state IN ('PENDING','ELIGIBLE','NO_VIOLATION_ENTRY','NO_ATTESTATION',"
                                   "'ALREADY_SUBMITTED','ALREADY_LINKED','MULTIPLE_RECORDS','NO_RECORDS','INVALID_RECORD',"
                                   "'NOT_APPLICABLE','MISSING','FAILED','SUBMITTING','CONFIRMED','UNCERTAIN','BLOCKED')",
                                   name="ck_inventory_requirement_state"),
                sa.CheckConstraint("state NOT IN ('SUBMITTING','UNCERTAIN','CONFIRMED') OR intent_at IS NOT NULL",
                                   name="ck_inventory_requirement_intent"),
            )
        indexes = {i["name"] for i in sa.inspect(bind).get_indexes("inventory_gpsr_requirements")}
        if "ix_inventory_requirement_run" not in indexes:
            op.create_index("ix_inventory_requirement_run", "inventory_gpsr_requirements", ["run_id", "marketplace_code"])
        if "uq_inventory_requirement_submission" not in indexes:
            op.create_index("uq_inventory_requirement_submission", "inventory_gpsr_requirements",
                            ["store_id", "marketplace_code", "sku", "asin", "requirement_type"], unique=True,
                            sqlite_where=sa.text("state IN ('SUBMITTING','UNCERTAIN','CONFIRMED')"))
        bind.execute(sa.text("""
            INSERT INTO inventory_gpsr_requirements
            (id,item_id,run_id,store_id,marketplace_code,sku,asin,requirement_type,state,reason,eligible,
             intent_at,submitted_at,confirmed_at,created_at,updated_at)
            SELECT id,id,run_id,store_id,marketplace_code,sku,asin,'safety_attestation',state,reason,eligible,
                   intent_at,submitted_at,confirmed_at,created_at,updated_at
            FROM inventory_gpsr_items i WHERE NOT EXISTS
              (SELECT 1 FROM inventory_gpsr_requirements r WHERE r.item_id=i.id AND r.requirement_type='safety_attestation')
        """))
        # Offer sites in store editing without silently enabling any schedule/site.
        for code, currency in (("IT", "EUR"), ("FR", "EUR"), ("ES", "EUR"), ("PL", "PLN")):
            bind.execute(sa.text("""INSERT INTO store_marketplaces
                (store_id,code,domain,currency,enabled,created_at,updated_at)
                SELECT s.id,:code,:domain,:currency,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM stores s
                WHERE NOT EXISTS (SELECT 1 FROM store_marketplaces m WHERE m.store_id=s.id AND m.code=:code)
            """), {"code": code, "domain": "sellercentral.amazon." + code.lower(), "currency": currency})


def downgrade():
    with rebuild() as bind:
        if bind.scalar(sa.text("SELECT 1 FROM inventory_gpsr_requirements WHERE requirement_type='responsible_person' LIMIT 1")) or \
           bind.scalar(sa.text("SELECT 1 FROM inventory_gpsr_items WHERE marketplace_code!='DE' LIMIT 1")) or \
           bind.scalar(sa.text("SELECT 1 FROM store_marketplaces WHERE code IN ('IT','FR','ES','PL') AND enabled=1 LIMIT 1")):
            raise RuntimeError("新增站点或负责人记录已使用，请保留数据库及备份，不执行有损降级")
        op.drop_table("inventory_gpsr_requirements")
        bind.execute(sa.text("DELETE FROM store_marketplaces WHERE code IN ('IT','FR','ES','PL')"))
        rewrite("inventory_gpsr_items", "ck_inventory_gpsr_marketplace", "marketplace_code = 'DE'")
        rewrite("store_marketplaces", "ck_v1_marketplace_code", "code IN ('CA','UK','AU','DE')")
