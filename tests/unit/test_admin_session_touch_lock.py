"""The per-request admin-session touch must not hold SQLite's write lock.

Every authenticated request updates ``AdminSession.last_seen_at``.  While that
UPDATE sits uncommitted in the request session, the endpoint's next query
autoflushes it and the request owns SQLite's single writer slot.  The endpoints
that go on to call the automation service write through a *different*
connection, so the second writer blocked on the first, the blocking sqlite call
stalled the event loop so the first could never commit, and both failed after
``busy_timeout`` with "database is locked" — surfaced to the operator as a bare
500 on approve/cancel while no run was executing at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import select

from ziniao_automation.auth import AuthService
from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import AdminSession


@pytest.fixture()
def factory(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'auth.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    yield make_session_factory(engine), tmp_path / "auth.db"
    engine.dispose()


def _write_lock_is_free(database: Path) -> bool:
    """Ask a genuinely separate connection for SQLite's writer slot."""

    connection = sqlite3.connect(database, timeout=1)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def test_validate_does_not_leave_the_write_lock_held(factory) -> None:
    session_factory, database = factory
    with session_factory() as db:
        token = AuthService(db).bootstrap("admin", "correct horse battery").session_token
        db.commit()

    with session_factory() as db:
        auth = AuthService(db)
        assert auth.validate(token) is not None
        # Model what the real endpoints do next: one more query, which is what
        # autoflushes a still-pending touch into a held write transaction.
        db.scalar(select(AdminSession))
        assert _write_lock_is_free(database), (
            "the admin-session touch is still holding SQLite's write lock; a "
            "delegating endpoint would deadlock against the automation service"
        )


def _backdate_touch(session_factory) -> datetime:
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with session_factory() as db:
        db.query(AdminSession).update({AdminSession.last_seen_at: stale})
        db.commit()
    return stale


def test_validate_still_records_the_touch(factory) -> None:
    """Committing the touch early must not turn it into a no-op."""

    session_factory, _ = factory
    with session_factory() as db:
        token = AuthService(db).bootstrap("admin", "correct horse battery").session_token
        db.commit()
    stale = _backdate_touch(session_factory)

    with session_factory() as db:
        assert AuthService(db).validate(token) is not None

    with session_factory() as db:
        assert db.scalar(select(AdminSession.last_seen_at)) > stale.replace(tzinfo=None)


def test_a_rolled_back_request_keeps_the_touch(factory) -> None:
    """The touch belongs to the session, not to the request's outcome."""

    session_factory, _ = factory
    with session_factory() as db:
        token = AuthService(db).bootstrap("admin", "correct horse battery").session_token
        db.commit()
    stale = _backdate_touch(session_factory)

    with session_factory() as db:
        assert AuthService(db).validate(token) is not None
        db.rollback()

    with session_factory() as db:
        assert db.scalar(select(AdminSession.last_seen_at)) > stale.replace(tzinfo=None)
