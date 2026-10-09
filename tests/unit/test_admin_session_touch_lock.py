"""Password-free page sessions must never touch administrator rows or hold a writer lock."""
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from ziniao_automation.auth import LocalAccess
from ziniao_automation.config import Settings
from ziniao_automation.models import AdminCredential, AdminSession
from ziniao_automation.web import create_app


def test_page_requests_never_write_admin_sessions(tmp_path: Path):
    database = tmp_path / "local.db"
    settings = Settings(project_root=tmp_path, database_url=f"sqlite:///{database.as_posix()}", testing=True)
    app = create_app(settings)
    with TestClient(app) as client:
        statements = []
        def record(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(app.state.engine, "before_cursor_execute", record)
        try:
            assert client.get("/").status_code == 200
            assert client.get("/stores").status_code == 200
            assert client.get("/api/stores").status_code == 200
        finally:
            event.remove(app.state.engine, "before_cursor_execute", record)
        assert not any("admin_sessions" in s.lower() or "admin_credentials" in s.lower() for s in statements)
        with app.state.sessions() as db:
            assert db.scalar(select(func.count(AdminCredential.id))) == 0
            assert db.scalar(select(func.count(AdminSession.token_hash))) == 0
        with sqlite3.connect(database, timeout=1) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()


def test_tokens_are_validated_without_database():
    access = LocalAccess()
    token, csrf = access.page_tokens()
    for _ in range(3):
        assert access.valid_session(token)
        assert access.validate_csrf(token, csrf)
        assert access.page_tokens(token) == (token, csrf)


def test_failed_csrf_does_not_invalidate_other_tabs():
    access = LocalAccess()
    token, csrf = access.page_tokens()
    assert not access.validate_csrf(token, "wrong")
    assert access.page_tokens(token) == (token, csrf)
    assert access.validate_csrf(token, csrf)
