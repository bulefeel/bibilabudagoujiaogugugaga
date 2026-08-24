"""SQLite engine, sessions, and durability settings."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Iterator

from sqlalchemy import Engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy import create_engine

from .config import Settings


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


def create_sqlite_engine(settings: Settings, *, echo: bool = False) -> Engine:
    settings.ensure_directories()
    if not str(settings.database_url).startswith("sqlite:///"):
        raise ValueError("V1 仅支持 SQLite 数据库")
    engine = create_engine(
        settings.database_url,
        echo=echo,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _migration_config():
    from alembic.config import Config

    project_root = Path(__file__).resolve().parents[2]
    script_location = project_root / "migrations"
    if not script_location.is_dir():
        raise RuntimeError(f"Alembic migration directory is missing: {script_location}")
    config = Config()
    config.set_main_option("script_location", str(script_location))
    return config


def _current_revision(engine: Engine) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def database_revision(engine: Engine) -> str:
    """Return the revision currently stamped in SQLite for diagnostics."""

    return _current_revision(engine) or "unversioned"


def _safe_revision(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", value or "unversioned")[:40]


def backup_sqlite_database(
    database: Path,
    backup_dir: Path,
    *,
    from_revision: str | None,
    to_revision: str,
) -> Path:
    """Create an internally consistent SQLite snapshot using its Backup API.

    Copying only the ``.db`` file can omit committed rows that are still in the
    WAL.  SQLite's own backup operation reads the complete logical database and
    leaves a standalone file that remains useful even when the migration that
    follows fails.
    """

    database = Path(database).resolve()
    backup_dir = Path(backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = (
        f"pre-migration-{stamp}-"
        f"{_safe_revision(from_revision)}-to-{_safe_revision(to_revision)}.db"
    )
    destination = backup_dir / name
    partial = destination.with_suffix(".db.partial")

    source_connection: sqlite3.Connection | None = None
    backup_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro", uri=True, timeout=30
        )
        backup_connection = sqlite3.connect(str(partial), timeout=30)
        source_connection.backup(backup_connection)
        result = backup_connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("SQLite migration backup failed integrity_check")
        backup_connection.close()
        backup_connection = None
        partial.replace(destination)
        return destination
    except Exception:
        # A partial file is not a usable rollback point.  Completed backups are
        # never removed here, especially if the following migration fails.
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if backup_connection is not None:
            backup_connection.close()
        if source_connection is not None:
            source_connection.close()


def init_database(engine: Engine, *, backup_dir: Path | None = None) -> Path | None:
    """Bring a fresh or installed database to the current Alembic revision.

    The same entry point is used by the web lifespan, installer and CLI, so an
    existing 0001 database cannot start newer ORM code before its migration is
    applied.  Passing the already configured Engine also keeps in-memory test
    databases and SQLite durability pragmas on the same connection.
    """

    from alembic import command
    from alembic.script import ScriptDirectory

    if engine.dialect.name != "sqlite":
        raise ValueError("V1 仅支持 SQLite 数据库")

    config = _migration_config()
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("Alembic migration head is missing")

    database_name = engine.url.database
    database = (
        Path(database_name).resolve()
        if database_name and database_name != ":memory:"
        else None
    )
    existed_with_data = bool(
        database is not None and database.is_file() and database.stat().st_size > 0
    )
    current = _current_revision(engine)
    if current == head:
        return None

    backup: Path | None = None
    if existed_with_data and database is not None:
        backup = backup_sqlite_database(
            database,
            backup_dir or database.parent / "backups",
            from_revision=current,
            to_revision=head,
        )

    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    return backup


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
