from __future__ import annotations

from logging.config import fileConfig
import os
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

from ziniao_automation.db import Base
from ziniao_automation import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    # ``disable_existing_loggers`` defaults to True, which switches off every
    # logger that already exists — including this package's module loggers when
    # a migration runs in the same process as the service.  That silently
    # deletes diagnostics rather than changing behaviour, so it is never what
    # we want here.
    fileConfig(config.config_file_name, disable_existing_loggers=False)
if os.getenv("ZINIAO_DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["ZINIAO_DATABASE_URL"].replace("%", "%%"))
else:
    # SQLite does not create missing parent directories.  Resolve the default
    # relative to the project instead of the caller's current directory.
    project_root = Path(__file__).resolve().parents[1]
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config.set_main_option(
        "sqlalchemy.url", f"sqlite:///{(data_dir / 'ziniao-automation.db').as_posix()}"
    )
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")

    def migrate(connection) -> None:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        # SQLAlchemy 2 autobegins a transaction for Alembic's revision stamp.
        # SQLite DDL itself is non-transactional, so explicitly commit the
        # stamp rather than letting Connection.__exit__ roll it back.
        connection.commit()

    if supplied_connection is not None:
        migrate(supplied_connection)
        return

    connectable = create_engine(config.get_main_option("sqlalchemy.url"), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        migrate(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
