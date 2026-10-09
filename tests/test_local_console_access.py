"""Real HTTP coverage for password-free, loopback-only console access."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ziniao_automation.auth import LocalAccess, is_loopback_client, is_same_origin_request
from ziniao_automation.cli import build_parser
from ziniao_automation.config import Settings
from ziniao_automation.models import AdminCredential
from ziniao_automation.web import create_app


@pytest.fixture()
def app(tmp_path: Path):
    return create_app(Settings(project_root=tmp_path, database_url=f"sqlite:///{(tmp_path / 'console.db').as_posix()}", testing=True))


@pytest.mark.parametrize("path", ["/", "/stores", "/schedules", "/approvals", "/runs", "/feedback", "/diagnostics"])
def test_all_pages_open_without_account(app, path):
    with TestClient(app) as client:
        page = client.get(path)
        assert page.status_code == 200, page.text
        assert not page.history
        assert "无需登录" in page.text
        assert 'data-action="logout"' not in page.text
        assert "管理员登录" not in page.text
        assert "ziniao_local_session" in client.cookies
        assert "ziniao_csrf" in client.cookies


def test_old_bookmarks_redirect_and_old_credential_posts_are_gone(app):
    with TestClient(app) as client:
        for path in ("/login", "/setup"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/"
        for path in ("/auth/login", "/auth/bootstrap", "/auth/logout"):
            assert client.post(path, json={}).status_code == 404


def test_existing_admin_is_not_required_modified_or_reset(app):
    with TestClient(app) as client:
        with app.state.sessions() as db:
            db.add(AdminCredential(id=1, username="old-operator", password_hash="opaque-legacy-hash", session_epoch=7))
            db.commit()
        client.cookies.set("ziniao_session", "legacy-session", domain="testserver.local", path="/")
        page = client.get("/")
        assert page.status_code == 200
        assert "old-operator" not in page.text
        assert "ziniao_session" not in client.cookies
        with app.state.sessions() as db:
            old = db.scalar(select(AdminCredential))
            assert old.username == "old-operator"
            assert old.password_hash == "opaque-legacy-hash"
            assert old.session_epoch == 7


def test_cookie_flags_and_no_store_headers(app):
    with TestClient(app) as client:
        page = client.get("/")
        cookies = page.headers.get_list("set-cookie")
        session_cookie = next(c for c in cookies if c.startswith("ziniao_local_session="))
        csrf_cookie = next(c for c in cookies if c.startswith("ziniao_csrf="))
        assert "HttpOnly" in session_cookie
        assert "HttpOnly" not in csrf_cookie
        assert all("SameSite=strict" in c and "Path=/" in c for c in cookies)
        assert page.headers["Cache-Control"] == "no-store"
        assert page.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("path", ["/api/stores", "/api/schedules", "/api/workflows"])
def test_read_api_needs_no_admin_cookie(app, path):
    with TestClient(app) as client:
        assert client.get(path).status_code == 200


def test_writes_require_a_signed_page_pair_and_header(app):
    with TestClient(app) as client:
        payload = {"name": "local store", "selector_type": "id", "selector_value": "test-profile"}
        assert client.post("/api/stores", json=payload).status_code == 403
        page = client.get("/")
        assert page.status_code == 200
        csrf = client.cookies["ziniao_csrf"]
        assert client.post("/api/stores", json=payload).status_code == 403
        assert client.post("/api/stores", json=payload, headers={"X-CSRF-Token": "x"}).status_code == 403
        assert client.post("/api/stores", json=payload, headers={"X-CSRF-Token": csrf}).status_code == 201
        assert client.get("/api/stores").json()[0]["name"] == "local store"


@pytest.mark.parametrize("headers", [
    {"Origin": "https://example.com"}, {"Origin": "null"},
    {"Origin": "http://testserver:9876"}, {"Origin": "http://testserver@evil.test"},
    {"Origin": "http://testserver/path"}, {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_other_websites_cannot_read_or_write_even_with_valid_pair(app, headers):
    with TestClient(app) as client:
        client.get("/")
        csrf = client.cookies["ziniao_csrf"]
        assert client.get("/api/stores", headers=headers).status_code == 403
        response = client.post("/api/stores", json={"name": "foreign", "selector_type": "id", "selector_value": "bad"}, headers={**headers, "X-CSRF-Token": csrf})
        assert response.status_code == 403
        assert client.get("/api/stores").json() == []


def test_same_origin_write_works(app):
    with TestClient(app) as client:
        client.get("/")
        result = client.post("/api/stores", json={"name": "local", "selector_type": "id", "selector_value": "ok"}, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin", "X-CSRF-Token": client.cookies["ziniao_csrf"]})
        assert result.status_code == 201


def test_remote_client_is_rejected_even_when_forwarded_host_is_local(app):
    with TestClient(app, client=("192.168.1.100", 43210)) as client:
        assert client.get("/", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        assert client.get("/api/stores").status_code == 403


def test_host_allowlist_rejects_rebinding(app):
    with TestClient(app) as client:
        assert client.get("/", headers={"Host": "evil.test"}).status_code == 400


def test_production_does_not_accept_testserver_or_synthetic_peer(tmp_path):
    application = create_app(Settings(project_root=tmp_path, testing=False))
    with TestClient(application, client=("127.0.0.1", 43210), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/").status_code == 200
        assert client.get("/", headers={"Host": "testserver"}).status_code == 400
    with TestClient(application, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/").status_code == 403


def test_restart_invalidates_old_pair_refresh_restores_writes(tmp_path):
    settings = Settings(project_root=tmp_path, testing=True)
    first_app = create_app(settings)
    with TestClient(first_app) as first:
        first.get("/")
        old_cookies = first.cookies
    second_app = create_app(settings)
    with TestClient(second_app, cookies=old_cookies) as second:
        payload = {"name": "after restart", "selector_type": "id", "selector_value": "restart"}
        assert second.post("/api/stores", json=payload, headers={"X-CSRF-Token": old_cookies["ziniao_csrf"]}).status_code == 403
        assert second.get("/").status_code == 200
        assert second.post("/api/stores", json=payload, headers={"X-CSRF-Token": second.cookies["ziniao_csrf"]}).status_code == 201


def test_tokens_reject_expiration_forgery_and_other_browser_pair(monkeypatch):
    monkeypatch.setattr("ziniao_automation.auth.time.time", lambda: 100000)
    access = LocalAccess(session_hours=1)
    token, csrf = access.page_tokens()
    other, other_csrf = access.page_tokens()
    assert access.validate_csrf(token, csrf)
    assert not access.validate_csrf(token, other_csrf)
    assert not access.validate_csrf(other, csrf)
    assert not LocalAccess().validate_csrf(token, csrf)
    body, sig = token.rsplit(".", 1)
    assert not access.valid_session(body + "." + ("0" if sig[0] != "0" else "1") + sig[1:])
    for invalid in (None, "", "x", "非ASCII", token + "x", "9" * 10000):
        assert not access.valid_session(invalid)
        assert not access.validate_csrf(invalid, csrf)
    assert not access.validate_csrf(token, "非ASCII")
    monkeypatch.setattr("ziniao_automation.auth.time.time", lambda: 99999)
    assert not access.valid_session(token)
    monkeypatch.setattr("ziniao_automation.auth.time.time", lambda: 103600)
    assert not access.valid_session(token)
    renewed, new_csrf = access.page_tokens(token)
    assert renewed != token
    assert access.validate_csrf(renewed, new_csrf)


def test_cli_retains_platform_credentials_but_not_admin():
    parser = build_parser()
    assert parser.parse_args(["configure", "ziniao"]).kind == "ziniao"
    assert parser.parse_args(["configure", "feishu"]).kind == "feishu"
    with pytest.raises(SystemExit):
        parser.parse_args(["configure", "admin"])


def test_local_helpers_reject_missing_peer_and_bad_origins():
    assert is_loopback_client("127.0.0.1")
    assert is_loopback_client("::1")
    assert not is_loopback_client(None)
    assert not is_loopback_client("testclient")
    assert is_loopback_client("testclient", testing=True)
    assert not is_same_origin_request("http://[", None, "http://127.0.0.1:8765/")
    assert not is_same_origin_request("http://127.0.0.1:bad", None, "http://127.0.0.1:8765/")
    assert is_same_origin_request("http://127.0.0.1:8765", "same-origin", "http://127.0.0.1:8765/stores")
