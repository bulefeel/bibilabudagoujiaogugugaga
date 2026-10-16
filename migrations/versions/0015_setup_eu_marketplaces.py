"""Allow read-only EU inventory onboarding; keep previous probe history intact."""
from contextlib import contextmanager

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

OLD = "marketplace_code IN ('CA','UK','AU')"
NEW = "marketplace_code IN ('CA','UK','AU','DE','IT','FR','ES','PL')"


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
            raise RuntimeError("Onboarding migration foreign-key validation failed")
        bind.commit()
    except BaseException:
        bind.rollback()
        raise
    finally:
        bind.exec_driver_sql(f"PRAGMA foreign_keys={original}")
        bind.commit()


def rewrite(condition):
    bind = op.get_bind()
    table = sa.Table("store_setup_site_results", sa.MetaData(), autoload_with=bind)
    checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)
              and "marketplace_code" in str(c.sqltext)]
    if len(checks) != 1:
        raise RuntimeError("Unexpected onboarding marketplace constraint")
    table.constraints.remove(checks[0])
    table.append_constraint(sa.CheckConstraint(condition))
    triggers = bind.execute(sa.text("SELECT sql FROM sqlite_master WHERE type='trigger' "
                                   "AND tbl_name='store_setup_site_results'")).scalars().all()
    with op.batch_alter_table(table.name, recreate="always", copy_from=table):
        pass
    for sql in triggers:
        bind.exec_driver_sql(sql)


def upgrade():
    with rebuild():
        rewrite(NEW)


def downgrade():
    with rebuild() as bind:
        if bind.scalar(sa.text("SELECT 1 FROM store_setup_site_results "
                               "WHERE marketplace_code NOT IN ('CA','UK','AU') LIMIT 1")):
            raise RuntimeError("已有欧洲站建档记录，请保留数据及备份，不执行有损降级")
        rewrite(OLD)
