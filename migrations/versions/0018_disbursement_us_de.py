"""Allow US onboarding and disbursement; preserve existing site/task scope."""
from contextlib import contextmanager

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

OLD_CODES = "'CA','UK','AU','DE','IT','FR','ES','PL'"
NEW_CODES = "'CA','UK','AU','US','DE','IT','FR','ES','PL'"


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
            raise RuntimeError("US marketplace migration foreign-key validation failed")
        bind.commit()
    except BaseException:
        bind.rollback()
        raise
    finally:
        bind.exec_driver_sql(f"PRAGMA foreign_keys={original}")
        bind.commit()


def rewrite(table_name, column, codes):
    bind = op.get_bind()
    table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)
              and str(c.sqltext).startswith(f"{column} IN (")]
    if len(checks) != 1:
        raise RuntimeError(f"Unexpected marketplace constraint on {table_name}")
    old = checks[0]
    table.constraints.remove(old)
    table.append_constraint(sa.CheckConstraint(f"{column} IN ({codes})", name=old.name))
    triggers = bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=:name"
    ), {"name": table_name}).scalars().all()
    ddl = bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"
    ), {"name": table_name}).scalar_one()
    autoincrement = "AUTOINCREMENT" in ddl.upper()
    sequence = bind.execute(sa.text(
        "SELECT seq FROM sqlite_sequence WHERE name=:name"
    ), {"name": table_name}).scalar() if autoincrement else None
    with op.batch_alter_table(table_name, recreate="always", copy_from=table,
                              table_kwargs={"sqlite_autoincrement": autoincrement}):
        pass
    for sql in triggers:
        bind.exec_driver_sql(sql)
    if sequence is not None:
        # An empty live table can still have deleted historical IDs above zero.
        bind.execute(sa.text("INSERT INTO sqlite_sequence(name,seq) SELECT :name,:seq "
                             "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name=:name)"),
                     {"name": table_name, "seq": sequence})
        bind.execute(sa.text("UPDATE sqlite_sequence SET seq=MAX(seq,:seq) WHERE name=:name"),
                     {"name": table_name, "seq": sequence})


def upgrade():
    with rebuild():
        rewrite("store_marketplaces", "code", NEW_CODES)
        rewrite("store_setup_site_results", "marketplace_code", NEW_CODES)


def downgrade():
    with rebuild() as bind:
        for table, column in (("store_marketplaces", "code"), ("marketplace_history", "code"),
                              ("store_setup_site_results", "marketplace_code")):
            if bind.exec_driver_sql(f"SELECT 1 FROM {table} WHERE {column}='US' LIMIT 1").fetchone():
                raise RuntimeError("已有美国站配置或历史记录，请保留数据及备份，不执行有损降级")
        rewrite("store_marketplaces", "code", OLD_CODES)
        rewrite("store_setup_site_results", "marketplace_code", OLD_CODES)
