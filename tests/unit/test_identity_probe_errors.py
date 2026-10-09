from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio

import pytest

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import Store
from ziniao_automation.workflows.runtime import AutomationService
from ziniao_automation.workflows.errors import HumanAuthRequired
from ziniao_automation.ziniao.errors import AuthWaitCancelled, AuthWaitExpired


@pytest.mark.asyncio
async def test_identity_probe_wraps_transient_target_error_without_retry(tmp_path, monkeypatch):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'identity.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        store = Store(name="店铺 A", selector_type="id", selector_value="profile-a", raw_profile={"platform_id":"1"})
        db.add(store)
        db.commit()
        store_id = store.id

    class Controller:
        def __init__(self) -> None:
            self.opens = 0

        @asynccontextmanager
        async def session(self, selector, store_key=None):
            self.opens += 1
            yield type("Handle", (), {"page": object(), "cleanup_confirmed": True})()

    async def target_closed(self, page, marketplace):
        del self, page, marketplace
        raise Exception("Target page, context or browser has been closed")

    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity",
        target_closed,
    )
    controller = Controller()
    service = AutomationService(
        engine=object(),
        run_loader=lambda _: None,
        ziniao_controller=controller,
        session_factory=factory,
    )

    result = await service.detect_store_identity(store_id)
    assert result["status"] == "FAILED"
    assert result["reason_code"] == "SETUP_FAILED"
    assert controller.opens == 1


def _identity_service(tmp_path, controller, *, timeout=1.0):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'probe.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        store = Store(name="店铺 B", selector_type="id", selector_value="profile-b", raw_profile={"platform_id":"1"})
        db.add(store)
        db.commit()
        store_id = store.id
    return AutomationService(
        engine=object(),
        run_loader=lambda _: None,
        ziniao_controller=controller,
        session_factory=factory,
        identity_auth_timeout_seconds=timeout,
    ), store_id


class AuthController:
    def __init__(self):
        self.opens = 0
        self.handle = None
        self.leases = {}
        self.closed = asyncio.Event()

    @asynccontextmanager
    async def session(self, selector, store_key=None):
        self.opens += 1
        self.handle = type("Handle", (), {"page": object(), "cleanup_confirmed": True})()
        try:
            yield self.handle
        finally:
            self.closed.set()

    async def wait_for_auth(self, handle, key, *, timeout_seconds, on_waiting=None):
        assert handle is self.handle
        event = asyncio.get_running_loop().create_future()
        self.leases[key] = event
        if on_waiting:
            await on_waiting()
        try:
            decision = await asyncio.wait_for(event, timeout_seconds)
        except TimeoutError as exc:
            raise AuthWaitExpired("expired") from exc
        finally:
            self.leases.pop(key, None)
        if not decision:
            raise AuthWaitCancelled("cancelled")
        return handle

    async def continue_auth(self, key):
        future = self.leases.get(key)
        if future is None:
            return False
        future.set_result(True)
        return True

    async def cancel_auth(self, key):
        future = self.leases.get(key)
        if future is None:
            return False
        future.set_result(False)
        return True


@pytest.mark.asyncio
async def test_identity_auth_continues_twice_on_same_handle_without_relaunch(tmp_path, monkeypatch):
    controller = AuthController()
    service, store_id = _identity_service(tmp_path, controller)
    calls = 0

    async def challenge_twice(self, page, marketplace):
        nonlocal calls
        del self, page, marketplace
        calls += 1
        if calls <= 2:
            raise HumanAuthRequired("challenge")
        return "SELLER-B", "dom:test"

    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity",
        challenge_twice,
    )
    first = await service.detect_store_identity(store_id)
    assert first["status"] == "WAITING_AUTH"
    assert first["last_diagnostic"]["reason_code"] in {"AUTH_UNKNOWN", "AUTH_INPUT_NOT_READY"}
    probe_id = first["probe_id"]
    one = await service.continue_identity_probe(store_id, probe_id)
    assert one["status"] == "CHECKING"
    for _ in range(20):
        again = await service.get_identity_probe(store_id, probe_id)
        if again["status"] == "WAITING_AUTH":
            break
        await asyncio.sleep(0)
    assert again["status"] == "WAITING_AUTH"
    await service.continue_identity_probe(store_id, probe_id)
    for _ in range(20):
        result = await service.get_identity_probe(store_id, probe_id)
        if result["status"] == "SUCCEEDED":
            break
        await asyncio.sleep(0)
    assert result["seller_id"] == "SELLER-B"
    assert controller.opens == 1
    assert calls == 3


@pytest.mark.asyncio
async def test_identity_probe_rejects_duplicate_and_cancel_joins_cleanup(tmp_path, monkeypatch):
    controller = AuthController()
    service, store_id = _identity_service(tmp_path, controller)

    async def challenge(self, page, marketplace):
        raise HumanAuthRequired("challenge")

    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", challenge
    )
    first = await service.detect_store_identity(store_id)
    attached = await service.detect_store_identity(store_id)
    assert attached["status"] == "WAITING_AUTH"
    assert attached["probe_id"] == first["probe_id"]
    cancelled = await service.cancel_identity_probe(store_id, first["probe_id"])
    assert cancelled["status"] == "CANCELLED"
    assert controller.closed.is_set()
    assert controller.opens == 1


@pytest.mark.asyncio
async def test_identity_probe_total_deadline_expires_and_closes(tmp_path, monkeypatch):
    controller = AuthController()
    service, store_id = _identity_service(tmp_path, controller, timeout=0.02)

    async def challenge(self, page, marketplace):
        raise HumanAuthRequired("challenge")

    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", challenge
    )
    waiting = await service.detect_store_identity(store_id)
    for _ in range(30):
        await asyncio.sleep(0.01)
        result = await service.get_identity_probe(store_id, waiting["probe_id"])
        if result["outcome"] == "TIMED_OUT":
            break
    assert result["outcome"] == "TIMED_OUT"
    assert controller.closed.is_set()


@pytest.mark.asyncio
async def test_legacy_identity_wait_preserves_safe_specific_cause(tmp_path, monkeypatch, caplog):
    import json
    caplog.set_level("INFO", logger="ziniao_automation.workflows.runtime")
    controller = AuthController()
    service, store_id = _identity_service(tmp_path, controller)

    async def challenge(self, page, marketplace):
        raise HumanAuthRequired(
            "SENTINEL_SECRET_PASSWORD", kind="sign_in",
            reason_code="AUTH_RETRY_EXHAUSTED", stage="password_signin",
            action="password_signin", attempts=3, page_category="LOGIN",
        )

    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", challenge)
    first = await service.detect_store_identity(store_id)
    try:
        assert first["status"] == "WAITING_AUTH"
        assert first["last_diagnostic"]["reason_code"] == "AUTH_RETRY_EXHAUSTED"
        assert first["last_diagnostic"]["attempts"] == 3
        assert "SENTINEL_SECRET_PASSWORD" not in json.dumps(first) + caplog.text
    finally:
        final = await service.cancel_identity_probe(store_id, first["probe_id"])
    assert final["status"] == "CANCELLED"
    assert final["last_diagnostic"]["reason_code"] == "AUTH_RETRY_EXHAUSTED"
    assert controller.closed.is_set()


@pytest.mark.asyncio
async def test_legacy_identity_hung_read_has_whole_probe_deadline(tmp_path, monkeypatch):
    controller = AuthController()
    service, store_id = _identity_service(tmp_path, controller, timeout=0.02)
    cancelled = asyncio.Event()
    async def hung(self, page, marketplace):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", hung)
    result = await asyncio.wait_for(service.detect_store_identity(store_id), timeout=1)
    assert result["status"] == "FAILED"
    assert cancelled.is_set()
    assert controller.closed.is_set()
