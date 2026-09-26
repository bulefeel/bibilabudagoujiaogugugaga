"""Add the German marketplace and give every existing store a DE row.

Revision ID: 0010
Revises: 0009

德国站是为 GPSR 安全证明流程加的。两件事必须一起做，否则会出现一种很难查的症状：

1. 放宽 ``ck_v1_marketplace_code``。SQLite 不能 ALTER 一个 CHECK，只能整表重建，
   所以这里照抄 0006 的 ``_sqlite_rebuild_transaction`` 配方（关外键、显式事务、
   收尾 ``PRAGMA foreign_key_check``）。全新安装的库由 0001 按 live metadata 建表，
   本身就带着新的 CHECK，所以这一段只对升级库有意义。

2. 给**每一个已存在的店铺**补一行 DE。运营明确要求德国站适用于当前所有账号。
   不补的话，排期创建时会被 ``_validate_marketplace_codes`` 挡下并报
   「排期包含尚未在该店铺建档的站点」——那条消息根本不会提 DE，操作员只会看到
   一个说不通的错误。

⚠️ 补出来的行是 ``enabled=1``。没有真正开通德国站的店铺不会因此出错：流程打开
德国站 URL 会被亚马逊弹到账户切换页，按既有约定那是**正常跳过**，不是故障。

⚠️ DE **不进** ``amazon_disbursement.config.ALLOWED_MARKETPLACES``。统一建档要读
付款页才能核验身份，德国站没有那条链路；DE 只能在店铺弹窗里勾选。两个白名单分工
不同，不要"顺手统一"。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import sqlalchemy as sa
from alembic import op


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


_OLD_CODES = "code IN ('CA', 'UK', 'AU')"
_NEW_CODES = "code IN ('CA', 'UK', 'AU', 'DE')"
_CHECK_NAME = "ck_v1_marketplace_code"
_TABLE = "store_marketplaces"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _checks(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    inspector = sa.inspect(op.get_bind())
    return {
        str(item["name"])
        for item in inspector.get_check_constraints(table_name)
        if item.get("name")
    }


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


def _rewrite_check(condition: str) -> None:
    if _CHECK_NAME not in _checks(_TABLE):
        return
    with op.batch_alter_table(_TABLE, recreate="always") as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.create_check_constraint(_CHECK_NAME, condition)


def _backfill_de() -> None:
    """给每个还没有 DE 行的店铺补一行。重复执行安全。"""

    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO store_marketplaces
                (store_id, code, domain, currency, enabled, created_at, updated_at)
            SELECT s.id, 'DE', 'sellercentral.amazon.de', 'EUR', 1,
                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM stores AS s
            WHERE NOT EXISTS (
                SELECT 1 FROM store_marketplaces AS m
                WHERE m.store_id = s.id AND m.code = 'DE'
            )
            """
        )
    )


def upgrade() -> None:
    if _TABLE not in _tables():
        return
    with _sqlite_rebuild_transaction():
        _rewrite_check(_NEW_CODES)
        _backfill_de()


def downgrade() -> None:
    if _TABLE not in _tables():
        return
    with _sqlite_rebuild_transaction():
        # 先删数据再收窄约束，否则重建时旧 CHECK 会被现存的 DE 行违反。
        op.get_bind().execute(
            sa.text("DELETE FROM store_marketplaces WHERE code = 'DE'")
        )
        _rewrite_check(_OLD_CODES)
