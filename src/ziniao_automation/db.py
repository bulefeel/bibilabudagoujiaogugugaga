"""SQLite engine, sessions, and durability settings."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
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


def init_database(engine: Engine) -> None:
    """Bring a fresh or installed database to the current Alembic revision.

    The same entry point is used by the web lifespan, installer and CLI, so an
    existing 0001 database cannot start newer ORM code before its migration is
    applied.  Passing the already configured Engine also keeps in-memory test
    databases and SQLite durability pragmas on the same connection.
    """

    from alembic import command
    from alembic.config import Config

    project_root = Path(__file__).resolve().parents[2]
    script_location = project_root / "migrations"
    if not script_location.is_dir():
        raise RuntimeError(f"Alembic migration directory is missing: {script_location}")

    config = Config()
    config.set_main_option("script_location", str(script_location))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


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
