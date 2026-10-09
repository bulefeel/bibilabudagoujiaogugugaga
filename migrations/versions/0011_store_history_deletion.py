"""Delete live stores without deleting their execution and financial history.

Revision ID: 0011
Revises: 0010
"""
from __future__ import annotations
from contextlib import contextmanager
from typing import Iterator
import sqlalchemy as sa
from alembic import op
from ziniao_automation.models import (
    StoreHistory, MarketplaceHistory, HISTORY_TRIGGERS, history_trigger_sql,
)
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

@contextmanager
def _sqlite_rebuild_transaction() -> Iterator[None]:
    """0006 的配方，一字不改。重建期间必须关掉外键强制。"""

    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return

    if bind.in_transaction():
        bind.commit()
    original_foreign_keys = int(
        bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    )
    bind.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    disabled = int(bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    bind.commit()
    if disabled != 0:
        raise RuntimeError("SQLite foreign-key enforcement could not be suspended")

    try:
        bind.exec_driver_sql("BEGIN IMMEDIATE")
        yield
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            sample = ", ".join(str(tuple(row)) for row in violations[:3])
            raise RuntimeError(
                "Migration would leave invalid foreign-key references: " + sample
            )
        bind.commit()
    except BaseException:
        if bind.in_transaction():
            bind.rollback()
        raise
    finally:
        if bind.in_transaction():
            bind.rollback()
        restore = "ON" if original_foreign_keys else "OFF"
        bind.exec_driver_sql(f"PRAGMA foreign_keys={restore}")
        restored = int(bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
        bind.commit()
        if restored != original_foreign_keys:
            raise RuntimeError("SQLite foreign-key enforcement was not restored")


_LINKS = (
    ("runs", "store_id", "stores", "store_history", "RESTRICT"),
    ("operation_guards", "store_id", "stores", "store_history", "RESTRICT"),
    ("feedback_reviews", "store_id", "stores", "store_history", "CASCADE"),
    ("site_runs", "marketplace_id", "store_marketplaces", "marketplace_history", "RESTRICT"),
)
_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _retarget(table: str, column: str, old: str, new: str, ondelete: str) -> None:
    inspector = sa.inspect(op.get_bind())
    keys = [fk for fk in inspector.get_foreign_keys(table)
            if fk["constrained_columns"] == [column] and fk["referred_table"] == old]
    if not keys:
        return
    with op.batch_alter_table(table, recreate="always", naming_convention=_CONVENTION) as batch:
        for key in keys:
            batch.drop_constraint(key["name"] or f"fk_{table}_{column}_{old}", type_="foreignkey")
        batch.create_foreign_key(f"fk_{table}_{column}_{new}", new, [column], ["id"], ondelete=ondelete)


def upgrade() -> None:
    bind = op.get_bind()
    with _sqlite_rebuild_transaction():
        StoreHistory.__table__.create(bind, checkfirst=True)
        MarketplaceHistory.__table__.create(bind, checkfirst=True)
        bind.exec_driver_sql("INSERT OR IGNORE INTO store_history (id,name,selector_type,selector_value) SELECT id,name,selector_type,selector_value FROM stores")
        bind.exec_driver_sql("INSERT OR IGNORE INTO marketplace_history (id,store_id,code,domain,currency) SELECT id,store_id,code,domain,currency FROM store_marketplaces")
        for table, column, old, new, _ in _LINKS:
            _retarget(table, column, old, new, "RESTRICT")
        # IDs of deleted stores/sites must never silently be reused by another
        # environment: historical identity IDs outlive the active records.
        for table in ("stores", "store_marketplaces"):
            ddl = bind.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).scalar_one()
            if "AUTOINCREMENT" not in ddl.upper():
                with op.batch_alter_table(table, recreate="always", table_kwargs={"sqlite_autoincrement": True}):
                    pass
        for table, name, action in HISTORY_TRIGGERS:
            bind.exec_driver_sql(history_trigger_sql(table, name, action))


def downgrade() -> None:
    bind = op.get_bind()
    with _sqlite_rebuild_transaction():
        # An older schema cannot represent history without a live store. Fail
        # before touching anything instead of losing history or resurrecting shops.
        for table, column, active, _, _ in _LINKS:
            orphan = bind.exec_driver_sql(
                f"SELECT 1 FROM {table} h LEFT JOIN {active} a ON a.id=h.{column} WHERE a.id IS NULL LIMIT 1"
            ).first()
            if orphan:
                raise RuntimeError("Archived history exists after store deletion; restore the pre-upgrade backup to use the older schema")
        for _, name, _ in HISTORY_TRIGGERS:
            bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
        for table, column, active, history, ondelete in _LINKS:
            _retarget(table, column, history, active, ondelete)
        op.drop_table("marketplace_history")
        op.drop_table("store_history")
