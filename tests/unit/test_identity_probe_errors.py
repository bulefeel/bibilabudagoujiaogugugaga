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
        store = Store(name="店铺 A", selector_type="id", selector_value="profile-a")
        db.add(store)
        db.commit()
        store_id = store.id

    class Controller:
        def __init__(self) -> None:
            self.opens = 0

        @asynccontextmanager
        async def session(self, selector, store_key=None):
            self.opens += 1
            yield type("Handle", (), {"page": object()})()

    async def target_closed(self, page, marketplace):
        del self, page, marketplace
        raise Exception("Target page, context or browser has been closed")

    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonPaymentsPage.detect_identity",
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
    assert "对应紫鸟店铺窗口尚未稳定" in result["message"]
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
        store = Store(name="店铺 B", selector_type="id", selector_value="profile-b")
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
        self.handle = type("Handle", (), {"page": object()})()
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
        "ziniao_automation.workflows.runtime.AmazonPaymentsPage.detect_identity",
        challenge_twice,
    )
    first = await service.detect_store_identity(store_id)
    assert first["status"] == "WAITING_AUTH"
    assert "邮箱 Continue" in first["message"]
    assert "紫鸟托管 Passkey" in first["message"]
    assert "已填密码登录" in first["message"]
    assert "已填好的 6 位 OTP" in first["message"]
    assert "再次尝试自动登录并继续" in first["message"]
    assert "每个页面动作最多自动点击 3 次" in first["message"]
    assert "每个站点最多进行 3 轮完整验证" in first["message"]
    assert "CA、UK、AU 分别计数" in first["message"]
    assert "未填内容、候选不唯一、CAPTCHA、非托管 Passkey" in first["message"]
    probe_id = first["probe_id"]
    one = await service.continue_identity_probe(store_id, probe_id)
    assert one["status"] == "CHECKING"
    for _ in range(20):
        again = await service.get_identity_probe(store_id, probe_id)
        if again["status"] == "WAITING_AUTH":
            break
        await asyncio.sleep(0)
    assert again["status"] == "WAITING_AUTH"
    assert "再次尝试自动登录并继续" in again["message"]
    assert "每个页面动作最多自动点击 3 次" in again["message"]
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
        "ziniao_automation.workflows.runtime.AmazonPaymentsPage.detect_identity", challenge
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
        "ziniao_automation.workflows.runtime.AmazonPaymentsPage.detect_identity", challenge
    )
    waiting = await service.detect_store_identity(store_id)
    for _ in range(30):
        await asyncio.sleep(0.01)
        result = await service.get_identity_probe(store_id, waiting["probe_id"])
        if result["status"] == "EXPIRED":
            break
    assert result["status"] == "EXPIRED"
    assert controller.closed.is_set()
