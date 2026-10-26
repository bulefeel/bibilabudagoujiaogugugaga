from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ziniao_automation import __version__
from ziniao_automation.config import Settings
from ziniao_automation.models import (
    ApprovalRequest,
    OperationGuard,
    Run,
    RunEvent,
    RunQueueEntry,
    Schedule,
    SiteRun,
    StoreMarketplace,
    SystemSetting,
    ZiniaoAccount,
)
from ziniao_automation.repositories import StoreRepository, WorkflowRepository
from ziniao_automation.web import create_app
from ziniao_automation.ziniao.errors import ZiniaoCredentialError


def _cache_key() -> str:
    """当前的静态资源缓存键，从 base.html 读出来而不是写死。"""

    import re

    base = (
        Path(__file__).resolve().parents[1]
        / "src/ziniao_automation/templates/base.html"
    ).read_text(encoding="utf-8")
    keys = set(re.findall(r"app\.(?:js|css)'\) }}\?v=([\w.-]+)", base))
    assert len(keys) == 1, f"app.js 和 app.css 的缓存键必须一致，读到：{keys}"
    return keys.pop()


def _head_revision() -> str:
    """迁移链当前的 head，从脚本里读而不是写死。"""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


class FakeAutomation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.active_store_setup = False
        self.active_store_setup_probe: dict | None = None

    async def sync_ziniao(self):
        return {"created": 0, "updated": 0}

    async def detect_store_identity(self, store_id: int):
        self.calls.append(("detect_identity", str(store_id)))
        return {
            "seller_id": "MERCHANT-DETECTED",
            "source": "dom:content",
            "marketplace_code": "CA",
        }

    async def enqueue_run(self, run_id: str):
        self.calls.append(("enqueue", run_id))

    async def approve_run(self, run_id: str, approval_id: str | None = None):
        self.calls.append(("approve", run_id))

    async def continue_auth(self, run_id: str):
        self.calls.append(("continue", run_id))

    async def cancel_run(self, run_id: str):
        self.calls.append(("cancel", run_id))

    async def reconcile_run(self, run_id: str):
        self.calls.append(("reconcile", run_id))

    async def has_active_store_setup(self, store_id: int) -> bool:
        self.calls.append(("has_active_store_setup", str(store_id)))
        return self.active_store_setup

    async def get_active_store_setup_probe(self, store_id: int):
        self.calls.append(("get_active_store_setup_probe", str(store_id)))
        return self.active_store_setup_probe


class SyncAutomation(FakeAutomation):
    async def sync_ziniao(self):
        return {
            "created": 0,
            "updated": 0,
            "profiles": [
                {
                    "name": "同步店铺",
                    "selector_type": "id",
                    "selector_value": "000123",
                    "browser_oauth": None,
                    "browser_id": "000123",
                }
            ],
        }


class MarketplaceSetupAutomation(FakeAutomation):
    def __init__(self) -> None:
        super().__init__()
        self.probe_id = "setup-probe-1"

    async def detect_marketplace_setup(self, store_id: int, marketplace_codes):
        self.calls.append(("detect_marketplace_setup", str(store_id)))
        return {
            "status": "CHECKING",
            "probe_id": self.probe_id,
            "store_id": store_id,
            "marketplaces": [
                {"code": code, "status": "PENDING"}
                for code in marketplace_codes
            ],
        }

    async def get_marketplace_setup_probe(self, store_id: int, probe_id: str):
        assert probe_id == self.probe_id
        return {
            "status": "PARTIAL",
            "probe_id": probe_id,
            "store_id": store_id,
            "marketplaces": [
                {
                    "code": "CA",
                    "status": "SUCCEEDED",
                    "observed_payment_account": "493",
                    "payment_account_source": "payments_details:test",
                },
                {"code": "UK", "status": "UNAVAILABLE", "message": "余额为 0"},
            ],
        }

    async def continue_marketplace_setup_probe(self, store_id: int, probe_id: str):
        return await self.get_marketplace_setup_probe(store_id, probe_id)

    async def cancel_marketplace_setup_probe(self, store_id: int, probe_id: str):
        return {"status": "CANCELLED", "probe_id": probe_id, "store_id": store_id}


class UnifiedSetupAutomation(FakeAutomation):
    probe_id = "unified-store-setup-1"

    async def detect_store_setup(self, store_id: int, marketplace_codes):
        self.calls.append(("detect_store_setup", str(store_id)))
        return {
            "status": "CHECKING",
            "probe_id": self.probe_id,
            "store_id": store_id,
            "identity": {"status": "CHECKING", "seller_id": None},
            "marketplaces": [
                {"code": code, "status": "PENDING"} for code in marketplace_codes
            ],
        }

    async def get_store_setup_probe(self, store_id: int, probe_id: str):
        assert probe_id == self.probe_id
        return {
            "status": "SUCCEEDED",
            "probe_id": probe_id,
            "store_id": store_id,
            "identity": {"status": "SUCCEEDED", "seller_id": "SELLER"},
            "marketplaces": [{"code": "CA", "status": "SUCCEEDED"}],
        }

    async def continue_store_setup_probe(self, store_id: int, probe_id: str):
        return await self.get_store_setup_probe(store_id, probe_id)

    async def cancel_store_setup_probe(self, store_id: int, probe_id: str):
        return {"status": "CANCELLED", "probe_id": probe_id, "store_id": store_id}


class CredentialFailureSetupAutomation(FakeAutomation):
    async def detect_store_setup(self, store_id: int, marketplace_codes):
        self.calls.append(("credential_preflight", str(store_id)))
        del marketplace_codes
        raise ZiniaoCredentialError("紫鸟凭据元数据不一致，请重新保存")


class FakeScheduleManager:
    def __init__(self) -> None:
        self.refreshed: list[int] = []

    async def refresh_schedule(self, schedule_id: int) -> None:
        self.refreshed.append(schedule_id)


@pytest.fixture()
def app_client(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        testing=True,
    )
    fake = FakeAutomation()
    app = create_app(settings, automation_service=fake)
    with TestClient(app) as client:
        yield app, client, fake


def bootstrap(client: TestClient) -> str:
    response = client.get("/")
    assert response.status_code == 200, response.text
    assert "ziniao_local_session" in client.cookies
    return client.cookies["ziniao_csrf"]


def test_passwordless_page_and_csrf(app_client):
    _, client, _ = app_client
    page = client.get("/")
    assert page.status_code == 200
    assert not page.history
    csrf = bootstrap(client)
    payload = {"name": "店铺 A", "selector_type": "oauth", "selector_value": "oauth-a"}
    assert client.post("/api/stores", json=payload).status_code == 403
    assert client.post("/api/stores", json=payload, headers={"X-CSRF-Token": "incorrect"}).status_code == 403
    assert client.post("/api/stores", json=payload, headers={"X-CSRF-Token": csrf}).status_code == 201
    for path in ("/auth/bootstrap", "/auth/login", "/auth/logout"):
        assert client.post(path, json={}).status_code == 404


def test_operation_guard_is_unique_and_never_rearmed(app_client):
    app, client, _ = app_client
    bootstrap(client)
    sessions = app.state.sessions
    with sessions() as db:
        store = StoreRepository(db).create(
            name="Guard Store",
            selector_type="id",
            selector_value="profile-101",
            browser_id="profile-101",
            expected_seller_id="SELLER-GUARD",
        )
        store.identity_confirmed = True
        store.enabled = True
        market = StoreMarketplace(
            store_id=store.id,
            code="UK",
            domain="sellercentral.amazon.co.uk",
            currency="GBP",
            enabled=True,
        )
        db.add(market)
        db.flush()
        repo = WorkflowRepository(db)
        run1 = repo.create_run(store_id=store.id, workflow="amazon_disbursement", mode="approval")
        site1 = repo.save_site_plan(
            run_id=run1.id,
            marketplace_id=market.id,
            marketplace_code="UK",
            currency="GBP",
            payable_amount=Decimal("22.10"),
            delayed_amount=Decimal("3.00"),
            settlement_key="period-2026-08",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
        guard1, is_new1 = repo.arm_operation(
            guard_key="guard-uk-2026-08",
            run_id=run1.id,
            site_run_id=site1.id,
            store_id=store.id,
            workflow="amazon_disbursement",
            marketplace_code="UK",
            settlement_key="period-2026-08",
            amount=Decimal("22.10"),
            currency="GBP",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
        db.commit()  # must be durable before the external click

        # Same guard_key = the same operation.  Never re-armed, never reset.
        same, is_new_same = repo.arm_operation(
            guard_key="guard-uk-2026-08",
            run_id=run1.id,
            site_run_id=site1.id,
            store_id=store.id,
            workflow="amazon_disbursement",
            marketplace_code="UK",
            settlement_key="period-2026-08",
            amount=Decimal("99.99"),
            currency="GBP",
            plan_hash="c" * 64,
            snapshot_hash="d" * 64,
        )
        assert is_new1 is True
        assert is_new_same is False
        assert same.id == guard1.id
        assert same.state == "ARMED"
        assert same.amount == Decimal("22.10")  # the first arming stands

        # A different guard_key sharing the settlement cycle is a DIFFERENT day
        # and must be allowed.  A cycle-wide unique key used to refuse this, and
        # because an Amazon cycle only rolls over once a payout succeeds, one
        # leftover guard then blocked that site forever.  Removed in 0004.
        next_day, is_new_next_day = repo.arm_operation(
            guard_key="guard-uk-2026-08-next-day",
            run_id=run1.id,
            site_run_id=site1.id,
            store_id=store.id,
            workflow="amazon_disbursement",
            marketplace_code="UK",
            settlement_key="period-2026-08",
            amount=Decimal("22.10"),
            currency="GBP",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
        assert is_new_next_day is True
        assert next_day.id != guard1.id
        assert repo.transition_guard(
            guard1.id,
            expected_states=("ARMED",),
            to_state="SUBMITTED",
            metadata={"receipt": "platform-receipt-1"},
        )
        db.commit()
        refreshed = repo.get_guard(guard1.id)
        assert refreshed.state == "SUBMITTED"
        assert refreshed.metadata_json["receipt"] == "platform-receipt-1"


def _guard_store_with_one_armed_guard(sessions, *, marketplace_code="UK"):
    with sessions() as db:
        store = StoreRepository(db).create(
            name="Release Store",
            selector_type="id",
            selector_value="profile-release",
            browser_id="profile-release",
            expected_seller_id="SELLER-RELEASE",
        )
        store.identity_confirmed = True
        store.enabled = True
        market = StoreMarketplace(
            store_id=store.id,
            code=marketplace_code,
            domain="sellercentral.amazon.co.uk",
            currency="GBP",
            enabled=True,
        )
        db.add(market)
        db.flush()
        repo = WorkflowRepository(db)
        run = repo.create_run(
            store_id=store.id, workflow="amazon_disbursement", mode="auto"
        )
        site = repo.save_site_plan(
            run_id=run.id,
            marketplace_id=market.id,
            marketplace_code=marketplace_code,
            currency="GBP",
            payable_amount=Decimal("610.03"),
            delayed_amount=Decimal("0.00"),
            settlement_key="2026/8/10 - 至今",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
        guard, _ = repo.arm_operation(
            guard_key="release-endpoint-guard",
            run_id=run.id,
            site_run_id=site.id,
            store_id=store.id,
            workflow="amazon_disbursement",
            marketplace_code=marketplace_code,
            settlement_key="2026/8/10 - 至今",
            amount=Decimal("610.03"),
            currency="GBP",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
        db.commit()
        return run.id, guard.id, guard.guard_key


def test_release_endpoint_clears_a_guard_that_never_dispatched(app_client):
    """The operator's exit from a money record for a payout never requested."""

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, guard_id, guard_key = _guard_store_with_one_armed_guard(sessions)
    # 释放只在任务结束之后:运行还在跑时,ARMED 可能正处在点击的那十几秒里。
    _pin_run_uncertain(sessions, run_id)

    response = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/release",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json()["marketplace_code"] == "UK"

    with sessions() as db:
        assert db.get(OperationGuard, guard_id) is None
        released = db.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "operation_released",
            )
        ).all()
        assert len(released) == 1
        assert "从未发出" in released[0].message
        assert released[0].details["released_by"] == "operator"


def test_release_endpoint_refuses_a_guard_that_was_dispatched(app_client):
    """``submitted_at`` is the hard boundary — no UI may cross it."""

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, guard_id, guard_key = _guard_store_with_one_armed_guard(sessions)
    with sessions() as db:
        WorkflowRepository(db).transition_guard(
            guard_id, expected_states=("ARMED",), to_state="SUBMITTED"
        )
        db.commit()

    response = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/release",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409
    assert "已派发" in response.json()["detail"]

    with sessions() as db:
        assert db.get(OperationGuard, guard_id) is not None


def test_acknowledge_endpoint_settles_a_dispatch_amazon_never_published(app_client):
    """The other half of release: the click happened, the platform stayed quiet.

    Amazon frequently does not show a disbursement in the statements page until
    the next day, so the automatic read-back — bounded to about a minute — can
    never close such a row.  It stayed UNCERTAIN for ever, pinned its run in
    UNCERTAIN_FINANCIAL, and no operator action existed to end it.
    """

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, guard_id, guard_key = _guard_store_with_one_armed_guard(sessions)
    with sessions() as db:
        repo = WorkflowRepository(db)
        repo.transition_guard(
            guard_id, expected_states=("ARMED",), to_state="SUBMITTED"
        )
        repo.transition_guard(
            guard_id,
            expected_states=("SUBMITTED",),
            to_state="UNCERTAIN",
            metadata={"reason": "已提交提现请求，回读时亚马逊尚未显示结果"},
        )
        db.commit()

    response = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/acknowledge",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text

    with sessions() as db:
        guard = db.get(OperationGuard, guard_id)
        # A transition, never a delete: the ``submitted_at`` boundary holds.
        assert guard is not None
        assert guard.state == "CONFIRMED"
        assert guard.submitted_at is not None
        # No fabricated platform reference — the operator's eyes are the source.
        assert guard.metadata_json["closed_by"] == "operator"
        # The read-back's own account of why it gave up must survive.
        assert "回读时亚马逊尚未显示结果" in guard.metadata_json["reason"]
        events = db.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "operation_acknowledged",
            )
        ).all()
        assert len(events) == 1
        assert "人工核对" in events[0].message
        assert "回读" not in events[0].message


def _pin_run_uncertain(sessions, run_id: str) -> None:
    with sessions() as db:
        WorkflowRepository(db).set_run_status(
            run_id, "UNCERTAIN_FINANCIAL", allowed_from=("QUEUED",)
        )
        db.commit()


def test_settle_refuses_while_any_guard_is_still_undecided(app_client):
    """Closing a run must never be a way to skip the money questions.

    Ending the run while a guard is still ARMED/UNCERTAIN (nobody knows whether
    the click went out) would let an operator clear the schedule without ever
    deciding what happened to the transfer.  SUBMITTED is *not* in that set any
    more: since the read-back was removed on 2026-09-23 it is the success state.
    """

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, guard_id, _ = _guard_store_with_one_armed_guard(sessions)
    with sessions() as db:
        repo = WorkflowRepository(db)
        repo.transition_guard(guard_id, expected_states=("ARMED",), to_state="SUBMITTED")
        repo.transition_guard(
            guard_id, expected_states=("SUBMITTED",), to_state="UNCERTAIN"
        )
        db.commit()
    _pin_run_uncertain(sessions, run_id)

    response = client.post(f"/api/runs/{run_id}/settle", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 409, response.text
    assert "还有资金记录没有裁定" in response.text
    assert "UK" in response.text, "要点名是哪个站点，否则操作员不知道去哪一条上处理"
    with sessions() as db:
        assert db.get(Run, run_id).status == "UNCERTAIN_FINANCIAL"
        assert db.get(OperationGuard, guard_id).state == "UNCERTAIN"


def test_settle_ends_a_run_once_every_guard_has_been_decided(app_client):
    """The exit that was missing entirely.

    Release and acknowledge each settle one guard row and neither writes
    ``Run.status``, so an operator could answer every funds question by hand and
    still be left with a run pinned in UNCERTAIN_FINANCIAL — which blocks its
    schedule from creating any new run, on the timer and via 「立即执行」 alike.
    """

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, guard_id, guard_key = _guard_store_with_one_armed_guard(sessions)
    with sessions() as db:
        repo = WorkflowRepository(db)
        repo.transition_guard(guard_id, expected_states=("ARMED",), to_state="SUBMITTED")
        repo.transition_guard(
            guard_id, expected_states=("SUBMITTED",), to_state="UNCERTAIN"
        )
        db.commit()
    _pin_run_uncertain(sessions, run_id)
    acknowledged = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/acknowledge",
        headers={"X-CSRF-Token": csrf},
    )
    assert acknowledged.status_code == 200, acknowledged.text
    # Settling the guard alone leaves the run — and therefore its schedule — stuck.
    with sessions() as db:
        assert db.get(Run, run_id).status == "UNCERTAIN_FINANCIAL"

    response = client.post(f"/api/runs/{run_id}/settle", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200, response.text
    # One site, one confirmed guard: every site of this run did get its money.
    assert response.json()["status"] == "SUCCEEDED"
    with sessions() as db:
        run = db.get(Run, run_id)
        assert run.status == "SUCCEEDED"
        assert run.finished_at is not None
        # Never a delete, never a resubmit: the guard is exactly as acknowledged.
        assert db.get(OperationGuard, guard_id).state == "CONFIRMED"
        events = db.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run_settled_by_operator",
            )
        ).all()
        assert len(events) == 1
        assert "没有重新提交任何转账" in events[0].message


def test_settle_reports_failed_when_every_guard_was_released(app_client):
    """Released means the click never happened, so no money moved.

    Reporting 「已完成」 in green here would tell an operator a payout succeeded
    on a site that still has its full balance sitting there.
    """

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, _, guard_key = _guard_store_with_one_armed_guard(sessions)
    _pin_run_uncertain(sessions, run_id)
    released = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/release",
        headers={"X-CSRF-Token": csrf},
    )
    assert released.status_code == 200, released.text

    response = client.post(f"/api/runs/{run_id}/settle", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "FAILED"


def test_settle_only_applies_to_a_run_waiting_on_a_funds_verdict(app_client):
    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, _, _ = _guard_store_with_one_armed_guard(sessions)

    response = client.post(f"/api/runs/{run_id}/settle", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 409, response.text
    assert "只有「资金结果待确认」的任务需要人工收尾" in response.text
    with sessions() as db:
        assert db.get(Run, run_id).status == "QUEUED"


def test_acknowledge_accepts_an_uncertain_guard_with_no_recorded_dispatch(app_client):
    """「系统没记录到派发」≠「没有派发」:点击之后、写 SUBMITTED 之前服务被停掉,
    就是 UNCERTAIN + ``submitted_at`` 为空,而钱可能已经发出去了。

    ⚠️ 这里原来拒绝结案、叫人去「释放」—— 可释放会删掉记录、**重新放开当天的
    提现**:操作员在卖家后台明明看到了这笔,页面却只给一个会引来第二笔的选项
    (2026-09-23 自审复核)。结案是操作员凭卖家后台做的裁定,只改状态、不删行、
    不补写 ``submitted_at``。
    """

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, guard_id, guard_key = _guard_store_with_one_armed_guard(sessions)
    with sessions() as db:
        WorkflowRepository(db).transition_guard(
            guard_id, expected_states=("ARMED",), to_state="UNCERTAIN"
        )
        db.commit()
    _pin_run_uncertain(sessions, run_id)

    response = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/acknowledge",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text

    with sessions() as db:
        guard = db.get(OperationGuard, guard_id)
        assert guard.state == "CONFIRMED"
        assert guard.submitted_at is None, "不伪造派发时间"

    # 结案之后不能再被释放:释放只接受 ARMED/UNCERTAIN。
    released = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/release",
        headers={"X-CSRF-Token": csrf},
    )
    assert released.status_code == 409


def test_acknowledge_refuses_a_click_that_has_no_verdict_yet(app_client):
    """ARMED 属于还在跑(或还没收口)的任务:这一下还没有结论,不许人工替它下结论。"""

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, guard_id, guard_key = _guard_store_with_one_armed_guard(sessions)

    response = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/acknowledge",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409
    with sessions() as db:
        assert db.get(OperationGuard, guard_id).state == "ARMED"


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "RECONCILING", "WAITING_AUTH"])
def test_release_refuses_while_the_run_is_still_going(app_client, status):
    """⚠️ 任务还在跑时,ARMED 可能正处在「已锁定、点击还没返回」的那十几秒里。
    这时删掉记录,点击照样发出去,而它唯一的记录已经没了(09-23 自审复核)。"""

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, guard_id, guard_key = _guard_store_with_one_armed_guard(sessions)
    with sessions() as db:
        db.get(Run, run_id).status = status
        db.commit()

    response = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/release",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409, response.text
    with sessions() as db:
        assert db.get(OperationGuard, guard_id) is not None
    page = client.get(f"/runs/{run_id}")
    assert 'data-run-action="release-guard"' not in page.text, "按钮也不该出现"




def test_ziniao_sync_persists_explicit_selector(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'sync.db').as_posix()}",
        testing=True,
    )
    app = create_app(settings, automation_service=SyncAutomation())
    with TestClient(app) as client:
        csrf = bootstrap(client)
        result = client.post("/api/ziniao/sync", json={}, headers={"X-CSRF-Token": csrf})
        assert result.status_code == 200, result.text
        assert result.json()["created"] == 1
        stores = client.get("/api/stores").json()
        assert stores[0]["selector_type"] == "id"
        # Leading zeroes prove the selector was not inferred with isdigit().
        assert stores[0]["selector_value"] == "000123"
        assert stores[0]["identity_confirmed"] is False
        assert stores[0]["enabled"] is False


def test_identity_probe_api_returns_candidate_without_confirming(app_client):
    _, client, fake = app_client
    csrf = bootstrap(client)
    headers = {"X-CSRF-Token": csrf}
    store = client.post(
        "/api/stores",
        json={"name": "Probe Store", "selector_type": "id", "selector_value": "probe-1"},
        headers=headers,
    ).json()
    response = client.post(
        f"/api/stores/{store['id']}/detect-identity", json={}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["seller_id"] == "MERCHANT-DETECTED"
    persisted = client.get("/api/stores").json()[0]
    assert persisted["expected_seller_id"] is None
    assert persisted["identity_confirmed"] is False
    assert persisted["enabled"] is False
    assert fake.calls == [("detect_identity", str(store["id"]))]




def test_unified_store_setup_api_runs_for_store_without_saved_identity(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'unified-setup-api.db').as_posix()}",
        testing=True,
    )
    fake = UnifiedSetupAutomation()
    app = create_app(settings, automation_service=fake)
    with TestClient(app) as client:
        csrf = bootstrap(client)
        headers = {"X-CSRF-Token": csrf}
        store = client.post(
            "/api/stores",
            json={
                "name": "Blank Unified Store",
                "selector_type": "id",
                "selector_value": "blank-unified-store",
            },
            headers=headers,
        ).json()
        denied = client.post(
            f"/api/stores/{store['id']}/detect-store-setup",
            json={"marketplace_codes": ["CA"]},
        )
        assert denied.status_code == 403
        started = client.post(
            f"/api/stores/{store['id']}/detect-store-setup",
            json={"marketplace_codes": ["CA"]},
            headers=headers,
        )
        assert started.status_code == 202, started.text
        assert started.json()["identity"]["status"] == "CHECKING"
        finished = client.get(
            f"/api/stores/{store['id']}/store-setup-probes/{fake.probe_id}"
        )
        assert finished.status_code == 200, finished.text
        assert finished.json()["identity"]["seller_id"] == "SELLER"


def test_unified_store_setup_returns_structured_credential_conflict(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'credential-preflight-api.db').as_posix()}",
        testing=True,
    )
    fake = CredentialFailureSetupAutomation()
    app = create_app(settings, automation_service=fake)
    with TestClient(app) as client:
        csrf = bootstrap(client)
        headers = {"X-CSRF-Token": csrf}
        store = client.post(
            "/api/stores",
            json={
                "name": "Credential Fixture Store",
                "selector_type": "id",
                "selector_value": "credential-fixture-store",
            },
            headers=headers,
        ).json()

        response = client.post(
            f"/api/stores/{store['id']}/detect-store-setup",
            json={"marketplace_codes": ["CA", "UK"]},
            headers=headers,
        )

        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "ZINIAO_CREDENTIALS_INVALID",
            "message": (
                "紫鸟凭据预检未通过：紫鸟凭据元数据不一致，请重新保存。"
                "请到“系统诊断”重新保存紫鸟公司、账号和密码后重试。"
            ),
        }
        assert fake.calls == [("credential_preflight", str(store["id"]))]


def test_identity_probe_api_returns_actionable_error_for_browser_failure(app_client):
    """A Playwright-style non-RuntimeError must never leak as an opaque 500."""
    app, client, fake = app_client
    csrf = bootstrap(client)
    headers = {"X-CSRF-Token": csrf}
    store = client.post(
        "/api/stores",
        json={"name": "Probe Store", "selector_type": "id", "selector_value": "probe-fail"},
        headers=headers,
    ).json()

    async def fail_once(store_id: int):
        fake.calls.append(("detect_identity_failed", str(store_id)))
        raise Exception("Target page, context or browser has been closed")

    app.state.automation_service.detect_store_identity = fail_once
    response = client.post(
        f"/api/stores/{store['id']}/detect-identity", json={}, headers=headers
    )

    assert response.status_code == 503
    assert "紫鸟店铺页面连接意外中断" in response.json()["detail"]
    assert fake.calls == [("detect_identity_failed", str(store["id"]))]


def test_stores_page_unified_setup_has_canonical_integer_store_id(app_client):
    """The unified setup dialog receives an explicit ID and a fresh asset."""
    _, client, _ = app_client
    csrf = bootstrap(client)
    store = client.post(
        "/api/stores",
        json={"name": "Probe Store", "selector_type": "id", "selector_value": "probe-1"},
        headers={"X-CSRF-Token": csrf},
    ).json()

    page = client.get("/stores")

    assert page.status_code == 200
    assert f'data-store-id="{store["id"]}"' in page.text
    assert 'type="button" data-action="detect-store-setup"' in page.text
    assert f"/static/app.js?v={_cache_key()}" in page.text
    assert "detect-identity" not in page.text
    assert 'data-store-setup-auth-panel hidden' in page.text
    assert 'data-action="continue-store-setup"' in page.text
    assert 'data-action="cancel-store-setup"' in page.text
    assert "自动登录未通过" in page.text
    assert "再次尝试自动登录并继续" in page.text
    assert "邮箱 Continue" in page.text
    assert "紫鸟托管 Passkey" in page.text
    assert "已填密码登录" in page.text
    assert "6 位 OTP" in page.text
    assert "程序会先等待紫鸟填充" in page.text
    assert "每个页面动作最多自动点击 3 次" in page.text
    assert "每个站点最多进行 3 轮完整验证" in page.text
    assert "每个站点分别计数" in page.text
    assert "未填内容、候选不唯一、CAPTCHA、非托管 Passkey" in page.text
    assert "取消并关闭该店铺窗口" in page.text
    assert "统一建档成功后自动勾选" in page.text
    assert "data-bulk-store-setup-site-incomplete" in page.text


def test_every_html_page_renders_for_a_store_that_has_marketplaces(app_client):
    """Render every page against a store with sites, not just a bare one.

    A store with no ``store_marketplaces`` rows never enters the per-site loop
    in ``stores.html``, so a template that reads a dropped column still renders
    fine.  That is exactly how a live 500 slipped past the suite: the page only
    fails once a real store has sites attached.  Assert on the rendered JSON so
    the check survives a template rewrite rather than pinning today's markup.
    """
    _, client, _ = app_client
    csrf = bootstrap(client)
    headers = {"X-CSRF-Token": csrf}
    store = client.post(
        "/api/stores",
        json={
            "name": "Sited Store",
            "selector_type": "id",
            "selector_value": "sited-1",
            "expected_seller_id": "A1SELLER",
        },
        headers=headers,
    ).json()
    patched = client.patch(
        f"/api/stores/{store['id']}",
        json={
            "identity_confirmed": True,
            "enabled": True,
            "marketplaces": [
                {"code": "CA", "enabled": True},
                {"code": "UK", "enabled": True},
                {"code": "AU", "enabled": False},
            ],
        },
        headers=headers,
    )
    assert patched.status_code == 200, patched.text

    for path in ("/", "/stores", "/schedules", "/approvals", "/runs", "/diagnostics"):
        page = client.get(path)
        assert page.status_code == 200, f"{path} -> {page.status_code}"

    page = client.get("/stores")
    card = re.search(r"data-store='(.*?)'>", page.text)
    assert card, page.text
    payload = json.loads(card.group(1))
    assert [(site["code"], site["enabled"]) for site in payload["marketplaces"]] == [
        ("CA", True),
        ("UK", True),
        ("AU", False),
    ]
    assert 'data-store-setup-marketplaces=\'["CA", "UK"]\'' in page.text


class PlatformSyncAutomation(FakeAutomation):
    """紫鸟返回三家店：一家亚马逊、一家 TikTok、一条没有平台字段的老记录。"""

    async def sync_ziniao(self):
        def profile(name: str, value: str, raw: dict) -> dict:
            return {
                "name": name,
                "selector_type": "oauth",
                "selector_value": value,
                "browser_oauth": value,
                "browser_id": None,
                "raw": raw,
            }

        return {
            "created": 0,
            "updated": 0,
            "profiles": [
                profile(
                    "Amazon CA Store",
                    "oauth-amz",
                    {"browserOauth": "oauth-amz", "platform_id": 1, "platform_name": "亚马逊-加拿大", "siteId": 2},
                ),
                profile(
                    "TikTok Store",
                    "oauth-tt",
                    {
                        "browserOauth": "oauth-tt",
                        "platform_id": 92,
                        "platform_name": "TikTok Shop-全球跨境(自运营)(新)",
                        "siteId": 458,
                    },
                ),
                profile("Legacy Store", "oauth-old", {"browserOauth": "oauth-old"}),
            ],
        }


def test_stores_page_tags_each_card_with_its_ziniao_platform_and_lists_a_filter(tmp_path: Path):
    """紫鸟公司账号下混着亚马逊、沃尔玛、TikTok 店铺，而流程只对亚马逊生效。

    页面原来只显示店铺名，操作员分不清哪家是亚马逊，建档就在 TikTok 环境里去找
    亚马逊后台。平台来自同步时存下的 raw_profile；这里断言的是渲染结果，不是模板
    字面。
    """
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'platform.db').as_posix()}",
        testing=True,
    )
    app = create_app(settings, automation_service=PlatformSyncAutomation())
    with TestClient(app) as client:
        csrf = bootstrap(client)
        synced = client.post("/api/ziniao/sync", json={}, headers={"X-CSRF-Token": csrf})
        assert synced.status_code == 200, synced.text
        assert synced.json()["created"] == 3
        # 线上那家 TikTok 店被早先的一键建档默认勾上了 CA/UK/AU。有站点时卡片也必须
        # 说清楚它不在范围内 —— 否则它和一家待建档的亚马逊店长得一模一样。
        tiktok = next(s for s in client.get("/api/stores").json() if s["name"] == "TikTok Store")
        ticked = client.patch(
            f"/api/stores/{tiktok['id']}",
            json={"marketplaces": [{"code": code, "enabled": True} for code in ("CA", "UK", "AU")]},
            headers={"X-CSRF-Token": csrf},
        )
        assert ticked.status_code == 200, ticked.text

        page = client.get("/stores")

    assert page.status_code == 200
    text = page.text
    # 每张卡片带上平台三态：true / false / 空（紫鸟没给字段，不下结论）。
    assert 'data-platform-label="亚马逊" data-platform-amazon="true"' in text
    assert 'data-platform-label="TikTok Shop" data-platform-amazon="false"' in text
    assert 'data-platform-label="平台未知" data-platform-amazon=""' in text
    # 标签文字：平台家族 + 紫鸟登记的站点/业务线。
    assert "亚马逊<em>加拿大</em>" in text
    assert "TikTok Shop<em>全球跨境(自运营)(新)</em>" in text
    assert text.count('class="platform-tag is-amazon"') == 1
    assert text.count('class="platform-tag is-other"') == 1
    assert text.count('class="platform-tag is-unknown"') == 1
    # 筛选项只列账册里真有的平台，亚马逊固定排第一。
    platform_select = text[text.index('name="platform"') : text.index('name="setup"')]
    assert re.findall(r'<option value="([^"]*)"[^>]*>', platform_select) == [
        "",
        "亚马逊",
        "TikTok Shop",
        "平台未知",
    ]
    assert "共 3 家" in text
    # 非亚马逊的卡片直说流程不适用 —— 即使它勾着亚马逊站点。
    cards = re.findall(r"<article class=\"store-card\".*?</article>", text, flags=re.S)
    by_label = {re.search(r'data-platform-label="([^"]*)"', card).group(1): card for card in cards}
    assert set(by_label) == {"亚马逊", "TikTok Shop", "平台未知"}
    tiktok_card = by_label["TikTok Shop"]
    assert "sellercentral.amazon.ca" in tiktok_card  # 前提：确实带着站点
    assert "TikTok Shop 店铺不在自动化范围内" in tiktok_card
    assert "不参与建档" in tiktok_card and "待自动建档" not in tiktok_card
    assert "尚未选择任务站点" not in tiktok_card
    for label in ("亚马逊", "平台未知"):
        assert "不在自动化范围内" not in by_label[label]
        assert "待自动建档" in by_label[label]
        assert "尚未选择任务站点" in by_label[label]
    # 建档筛选三态：非亚马逊店既不「待建档」也不「已建档」；平台未知的不下结论，照旧待建档。
    assert 'data-setup-state="n/a"' in tiktok_card
    assert 'data-setup-state="needed"' in by_label["亚马逊"]
    assert 'data-setup-state="needed"' in by_label["平台未知"]








def test_reset_store_setup_rejects_live_process_probe_without_editing_db(app_client):
    _, client, fake = app_client
    csrf = bootstrap(client)
    headers = {"X-CSRF-Token": csrf}
    store = client.post(
        "/api/stores",
        json={
            "name": "Live Probe Store",
            "selector_type": "id",
            "selector_value": "live-probe-store",
            "expected_seller_id": "SELLER-LIVE",
        },
        headers=headers,
    ).json()
    fake.active_store_setup = True

    reset = client.delete(f"/api/stores/{store['id']}/setup", headers=headers)

    assert reset.status_code == 409
    persisted = next(
        item for item in client.get("/api/stores").json() if item["id"] == store["id"]
    )
    assert persisted["expected_seller_id"] == "SELLER-LIVE"


def test_saving_a_store_is_refused_while_a_probe_holds_it(app_client):
    """「保存建档」 must obey the same gate as 「删除 / 重置建档」.

    Only reset was ever checked, which made the gate theatre: the save button
    sits directly above it, carries the same identity fields, and additionally
    rewrites ``store_marketplaces`` — rows the reset deliberately leaves alone.
    The blocked path was strictly gentler than the open one.
    """

    _, client, fake = app_client
    csrf = bootstrap(client)
    headers = {"X-CSRF-Token": csrf}
    store = client.post(
        "/api/stores",
        json={
            "name": "Save During Probe",
            "selector_type": "id",
            "selector_value": "save-during-probe",
            "expected_seller_id": "SELLER-KEEP",
        },
        headers=headers,
    ).json()
    fake.active_store_setup = True

    saved = client.patch(
        f"/api/stores/{store['id']}",
        json={
            "expected_seller_id": "SELLER-STOMPED",
            "enabled": False,
            "marketplaces": [{"code": "CA", "enabled": False}],
        },
        headers=headers,
    )

    assert saved.status_code == 409, saved.text
    assert "取消并关闭该店铺窗口" in saved.text
    fake.active_store_setup = False
    persisted = next(
        item for item in client.get("/api/stores").json() if item["id"] == store["id"]
    )
    assert persisted["expected_seller_id"] == "SELLER-KEEP"
    assert persisted["marketplaces"] == []


def test_editor_can_find_a_running_probe_without_knowing_its_id(app_client):
    """The server answers "what is running on this store", id not required.

    ``probe_id`` only ever lived in the browser's DOM, so a reload or a
    reopened editor lost the one handle that reaches the cancel endpoint while
    the backend kept holding the store's Ziniao window.
    """

    _, client, fake = app_client
    csrf = bootstrap(client)
    headers = {"X-CSRF-Token": csrf}
    store = client.post(
        "/api/stores",
        json={
            "name": "Probe Lookup",
            "selector_type": "id",
            "selector_value": "probe-lookup",
        },
        headers=headers,
    ).json()

    idle = client.get(f"/api/stores/{store['id']}/store-setup-probe")
    assert idle.status_code == 204
    assert idle.content == b""

    fake.active_store_setup_probe = {
        "status": "CHECKING",
        "probe_id": "unified-probe-9",
        "store_id": store["id"],
        "marketplaces": [{"code": "CA", "status": "CHECKING"}],
    }
    live = client.get(f"/api/stores/{store['id']}/store-setup-probe")

    assert live.status_code == 200
    assert live.json()["probe_id"] == "unified-probe-9"
    assert live.json()["status"] == "CHECKING"


def test_changing_seller_id_revokes_confirmation_but_keeps_store_available(app_client):
    _, client, _ = app_client
    csrf = bootstrap(client)
    headers = {"X-CSRF-Token": csrf}
    store = client.post(
        "/api/stores",
        json={
            "name": "Identity Store",
            "selector_type": "id",
            "selector_value": "identity-1",
            "expected_seller_id": "SELLER-OLD",
        },
        headers=headers,
    ).json()
    confirmed = client.patch(
        f"/api/stores/{store['id']}",
        json={"identity_confirmed": True, "enabled": True},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    changed = client.patch(
        f"/api/stores/{store['id']}",
        json={"expected_seller_id": "SELLER-NEW"},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["identity_confirmed"] is False
    assert changed.json()["enabled"] is True


def test_production_sync_persists_each_new_profile_only_once(tmp_path: Path):
    """Regression: runtime and web must not both insert the same profile."""
    from types import SimpleNamespace

    from ziniao_automation.workflows.runtime import AutomationService

    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'production-sync.db').as_posix()}",
        testing=True,
    )

    class Controller:
        async def sync_profiles(self):
            return [
                SimpleNamespace(
                    name="真实装配店铺",
                    selector_type="oauth",
                    selector_value="oauth-production-one",
                    browser_oauth="oauth-production-one",
                    browser_id="88",
                    raw={},
                )
            ]

    service = AutomationService(
        engine=object(),
        run_loader=lambda _: None,
        ziniao_controller=Controller(),
        session_factory=object(),
    )
    app = create_app(settings, automation_service=service)
    with TestClient(app) as client:
        csrf = bootstrap(client)
        response = client.post(
            "/api/ziniao/sync", json={}, headers={"X-CSRF-Token": csrf}
        )
        assert response.status_code == 200, response.text
        assert response.json()["created"] == 1
        stores = client.get("/api/stores").json()
        assert [item["selector_value"] for item in stores] == [
            "oauth-production-one"
        ]


def _settings_client(app_client, monkeypatch):
    """Point the credential helpers at an in-memory store.

    The real ones talk to Windows Credential Manager; a test that wrote there
    would clobber the operator's actual Ziniao and Feishu logins.
    """

    from ziniao_automation import web as web_module

    vault: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(
        web_module,
        "write_generic_credential",
        lambda target, secret, username="": vault.__setitem__(target, dict(secret)),
    )
    monkeypatch.setattr(
        web_module, "credential_exists", lambda target: target in vault
    )
    monkeypatch.setattr(
        web_module,
        "credential_matches",
        lambda target, expected: target in vault
        and vault[target] == {str(key): str(value) for key, value in expected.items()},
    )
    monkeypatch.setattr(
        web_module, "read_generic_credential", lambda target: dict(vault[target])
    )
    return vault


def test_saving_feishu_from_the_console_keeps_the_secret_out_of_sqlite(
    app_client, monkeypatch
):
    """The whole point of Credential Manager is that the DB never sees the secret.

    Moving configuration into the web console must not quietly become a reason
    to persist it somewhere easier to read.
    """

    app, client, _ = app_client
    csrf = bootstrap(client)
    vault = _settings_client(app_client, monkeypatch)

    response = client.post(
        "/api/settings/feishu",
        json={"app_id": "cli_app", "app_secret": "TOP_SECRET", "chat_id": "oc_room"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200, response.text
    assert "TOP_SECRET" not in response.text, "响应体不得带回密钥"
    assert vault["ziniao-automation/feishu/main"]["app_secret"] == "TOP_SECRET"
    with app.state.sessions() as db:
        stored = db.get(SystemSetting, "feishu").value
    assert stored["app_id"] == "cli_app"
    assert stored["chat_id"] == "oc_room"
    assert "TOP_SECRET" not in json.dumps(stored, ensure_ascii=False)


def test_a_credential_write_that_did_not_stick_is_reported_not_assumed(
    app_client, monkeypatch
):
    """Security software has been seen silently dropping credential writes.

    Without the read-back the operator would leave the page believing Feishu was
    configured, and only find out when a payout failed to notify anyone.
    """

    _, client, _ = app_client
    csrf = bootstrap(client)
    _settings_client(app_client, monkeypatch)
    from ziniao_automation import web as web_module

    monkeypatch.setattr(web_module, "credential_matches", lambda target, expected: False)

    response = client.post(
        "/api/settings/feishu",
        json={"app_id": "cli_app", "app_secret": "s", "chat_id": "oc_room"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 502
    assert "安全软件" in response.json()["detail"]


def test_an_existing_old_credential_is_not_mistaken_for_a_successful_overwrite(
    app_client, monkeypatch
):
    """A silent write block must not validate merely because the old record exists."""

    app, client, _ = app_client
    csrf = bootstrap(client)
    vault = _settings_client(app_client, monkeypatch)
    target = "ziniao-automation/ziniao/main"
    vault[target] = {
        "company": "same-company",
        "username": "same-user",
        "password": "OLD_PASSWORD",
    }
    from ziniao_automation import web as web_module

    # Simulate endpoint security reporting a successful CredWriteW call while
    # preserving the old record.
    monkeypatch.setattr(
        web_module, "write_generic_credential", lambda *args, **kwargs: None
    )
    response = client.post(
        "/api/settings/ziniao",
        json={
            "company": "same-company",
            "username": "same-user",
            "password": "NEW_PASSWORD",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 502
    assert vault[target]["password"] == "OLD_PASSWORD"
    with app.state.sessions() as db:
        assert db.scalar(select(ZiniaoAccount)) is None


def test_saving_ziniao_from_the_console_updates_the_account_row(app_client, monkeypatch):
    _, client, _ = app_client
    csrf = bootstrap(client)
    vault = _settings_client(app_client, monkeypatch)

    response = client.post(
        "/api/settings/ziniao",
        json={"company": "某公司", "username": "u1", "password": "PW"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200, response.text
    assert "PW" not in response.text
    assert vault["ziniao-automation/ziniao/main"]["password"] == "PW"


def test_the_diagnostics_page_never_renders_a_stored_secret(app_client, monkeypatch):
    """Not even masked: a mask still tells the reader how long the value is."""

    _, client, _ = app_client
    csrf = bootstrap(client)
    _settings_client(app_client, monkeypatch)
    client.post(
        "/api/settings/feishu",
        json={"app_id": "cli_app", "app_secret": "TOP_SECRET", "chat_id": "oc_room"},
        headers={"X-CSRF-Token": csrf},
    )
    client.post(
        "/api/settings/ziniao",
        json={"company": "某公司", "username": "u1", "password": "TOP_PASSWORD"},
        headers={"X-CSRF-Token": csrf},
    )

    page = client.get("/diagnostics")

    assert page.status_code == 200
    assert "TOP_SECRET" not in page.text
    assert "TOP_PASSWORD" not in page.text
    # Non-secret fields are pre-filled so the operator does not retype them.
    assert 'value="cli_app"' in page.text
    assert 'value="oc_room"' in page.text
    for field in ("password", "app_secret"):
        tag = re.search(rf'<input name="{field}"[^>]*>', page.text)
        assert tag and " value=" not in tag.group(0), f"{field} 不得带 value 属性"


def test_diagnostics_identifies_the_release_and_database_revision(app_client):
    _, client, _ = app_client
    bootstrap(client)

    page = client.get("/diagnostics")

    assert page.status_code == 200
    assert "应用版本 / 构建" in page.text
    assert __version__ in page.text
    assert "数据库迁移版本" in page.text
    # 从迁移脚本读 head，而不是写死版本号。写死的话每加一条迁移都会多出一条
    # 无关的红，久而久之就训练成「看到红就改断言」——这条断言的意思是
    # 「诊断页显示的是数据库当前真实的迁移版本」，不是「版本号等于某个字面量」。
    assert _head_revision() in page.text


def test_webdriver_switch_is_refused_while_a_payout_holds_the_browser(app_client):
    """Killing Ziniao mid-run could strand a payout between ARM and the click."""

    _, client, fake = app_client
    csrf = bootstrap(client)

    async def busy(*, force: bool = False) -> dict[str, object]:
        assert force is False, "默认不该强制"
        return {
            "ok": False,
            "status": "busy",
            "message": "现在切换会关掉正在使用的紫鸟窗口，因此已阻止：\n有提现任务正在执行（资金锁被占用）",
            "closed_processes": 0,
            "details": {},
        }

    fake.start_webdriver_mode = busy

    response = client.post(
        "/api/ziniao/webdriver/start", json={}, headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 409
    assert "资金锁" in response.json()["detail"]


def test_webdriver_switch_passes_the_operators_force_through(app_client):
    """强制开关必须真的到达运行服务，而且默认是关的。

    前两类阻止是服务进程内的 asyncio 锁，手动关掉紫鸟窗口清不掉。锁被卡住时
    （持锁的协程在等一个已经被关掉的浏览器），除了重启服务没有别的出路 ——
    运营 2026-09-22 明确要了这个出口。
    """

    _, client, fake = app_client
    csrf = bootstrap(client)
    seen: list[bool] = []

    async def starter(*, force: bool = False) -> dict[str, object]:
        seen.append(force)
        return {
            "ok": True,
            "status": "ready",
            "message": "已就绪",
            "closed_processes": 1,
            "details": {},
        }

    fake.start_webdriver_mode = starter

    client.post("/api/ziniao/webdriver/start", json={}, headers={"X-CSRF-Token": csrf})
    client.post(
        "/api/ziniao/webdriver/start",
        headers={"X-CSRF-Token": csrf},
        json={"force": True},
    )

    assert seen == [False, True]


def test_webdriver_switch_reports_success_with_what_it_closed(app_client):
    _, client, fake = app_client
    csrf = bootstrap(client)

    async def ready(*, force: bool = False) -> dict[str, object]:
        return {
            "ok": True,
            "status": "ready",
            "message": "紫鸟 WebDriver 模式已就绪（127.0.0.1:16851）。",
            "closed_processes": 2,
            "details": {},
        }

    fake.start_webdriver_mode = ready

    response = client.post(
        "/api/ziniao/webdriver/start", json={}, headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 200, response.text
    assert response.json()["closed_processes"] == 2


def test_every_console_api_the_diagnostics_page_calls_actually_exists(app_client):
    """Guard against the page shipping buttons that hit 404.

    Found the hard way on the test machine: an upgrade replaced the templates
    on disk while the old service kept serving from memory, so the new UI
    appeared but every new endpoint answered "Not Found".  Inspecting
    ``app.routes`` does not catch this either — this FastAPI version stores an
    ``_IncludedRouter`` placeholder rather than copying the sub-routes, so the
    list looks empty even when routing works.  Only a real request proves it.
    """

    _, client, _ = app_client
    csrf = bootstrap(client)
    page = client.get("/diagnostics")
    assert page.status_code == 200

    # Every endpoint app.js posts to from this page.
    endpoints = [
        "/api/settings/ziniao",
        "/api/settings/feishu",
        "/api/settings/feishu/test",
        "/api/ziniao/webdriver/start",
    ]
    for endpoint in endpoints:
        assert endpoint in _diagnostics_script(), f"{endpoint} 不在前端代码里"
        response = client.post(endpoint, json={}, headers={"X-CSRF-Token": csrf})
        # 422 (bad body) / 409 / 502 / 503 all prove the route is mounted.
        # 404 means the button is wired to nothing.
        assert response.status_code != 404, f"{endpoint} 返回 404"


def _diagnostics_script() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "src/ziniao_automation/static/app.js"
    ).read_text(encoding="utf-8")


def test_the_new_diagnostics_sections_have_styles(app_client):
    """A class the stylesheet never heard of renders as an unstyled form.

    That is exactly how the credential cards first shipped.
    """

    root = Path(__file__).resolve().parents[1] / "src/ziniao_automation"
    css = (root / "static/app.css").read_text(encoding="utf-8")
    for klass in (
        "diagnostic-actions",
        "diagnostic-actions-state",
        "diagnostic-settings",
        "settings-card",
        "settings-card-head",
        "settings-card-actions",
    ):
        assert f".{klass}" in css, f"{klass} 没有样式"


def test_schedules_page_renders_a_real_row_not_just_an_empty_list(app_client):
    """A page smoke test with no rows never runs the per-row template code.

    That is exactly how /stores once shipped a 500: the template read columns a
    migration had dropped, and the test that "covered" it rendered an empty
    list. The schedule card now calls two filters per row — ``interval_label``
    on the period and ``local_datetime`` on the anchor — so there has to be a
    row on the page for either of them to be exercised.
    """

    _, client, _ = app_client
    csrf = bootstrap(client)
    headers = {"X-CSRF-Token": csrf}
    store = client.post(
        "/api/stores",
        json={
            "name": "Schedule Render Store",
            "selector_type": "id",
            "selector_value": "schedule-render",
            "expected_seller_id": "SELLER-RENDER",
        },
        headers=headers,
    ).json()
    client.patch(
        f"/api/stores/{store['id']}",
        json={
            "identity_confirmed": True,
            "enabled": True,
            "marketplaces": [{"code": "CA", "enabled": True}],
        },
        headers=headers,
    )
    created = client.post(
        "/api/schedules",
        json={
            "store_id": store["id"],
            "name": "每 25 小时提现",
            "first_run_at": "2026-01-01T01:00:00+00:00",
            "interval_minutes": 1500,
            "marketplace_codes": ["CA"],
            "workflow_config": {"marketplace_codes": ["CA"]},
            "enabled": False,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["interval_minutes"] == 1500

    page = client.get("/schedules")

    assert page.status_code == 200, page.text
    assert "每 25 小时" in page.text, "间隔要按操作员的说法渲染，而不是 1500 分钟"
    assert "每 25 小时 0 分" not in page.text
    assert "首次" in page.text
    # The edit button carries the row back to the form; datetime-local needs the
    # 'T' form, and the period must survive the round trip.
    assert "2026-01-01T09:00" in page.text, "编辑按钮里的首次时间要按 UTC+8 回填"
    assert '"interval_minutes": 1500' in page.text



@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        ("amazon_disbursement", ["CA", "DE"]),
        ("amazon_feedback_removal", ["CA"]),
        ("amazon_gpsr_attestation", ["DE"]),
    ],
)
def test_start_check_defaults_to_sites_the_workflow_can_run(tmp_path, workflow, expected):
    """「发起检查」不带站点时，取「已启用 ∩ 这个流程能跑」的站点。

    ⚠️ 2026-09-23 实测：迁移 0010 给线上每家店补了 enabled 的 DE，而默认值只取
    「已启用」，于是提现的「发起检查」对**所有**店一律 422「工作流参数不符合当前
    版本的固定定义」。
    """

    from ziniao_automation.workflows.amazon_disbursement import (
        build_amazon_disbursement_definition,
    )
    from ziniao_automation.workflows.amazon_feedback import (
        build_amazon_feedback_definition,
    )
    from ziniao_automation.workflows.amazon_gpsr import build_amazon_gpsr_definition
    from ziniao_automation.workflows.registry import WorkflowRegistry

    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        testing=True,
    )
    fake = FakeAutomation()
    registry = WorkflowRegistry(
        (
            build_amazon_disbursement_definition(),
            build_amazon_feedback_definition(),
            build_amazon_gpsr_definition(),
        )
    )
    app = create_app(settings, automation_service=fake, workflow_registry=registry)
    with TestClient(app) as client:
        headers = {"X-CSRF-Token": bootstrap(client)}
        store = client.post(
            "/api/stores",
            json={
                "name": "Two-Site Store",
                "selector_type": "id",
                "selector_value": "two-site-1",
                "expected_seller_id": "A1SELLER",
            },
            headers=headers,
        ).json()
        patched = client.patch(
            f"/api/stores/{store['id']}",
            json={
                "identity_confirmed": True,
                "enabled": True,
                "marketplaces": [
                    {"code": "CA", "enabled": True},
                    {"code": "DE", "enabled": True},
                ],
            },
            headers=headers,
        )
        assert patched.status_code == 200, patched.text

        response = client.post(
            "/api/runs",
            json={"store_id": store["id"], "workflow": workflow, "mode": "dry_run"},
            headers=headers,
        )

    assert response.status_code == 202, response.text
    run = response.json()
    assert run["workflow_config"]["marketplace_codes"] == expected
    assert ("enqueue", run["id"]) in fake.calls


def _add_site(sessions, run_id: str, code: str, status: str) -> None:
    with sessions() as db:
        run = db.get(Run, run_id)
        market = StoreMarketplace(
            store_id=run.store_id,
            code=code,
            domain=f"sellercentral.amazon.{code.lower()}",
            currency="CAD",
            enabled=True,
        )
        db.add(market)
        db.flush()
        db.add(
            SiteRun(
                run_id=run_id,
                marketplace_id=market.id,
                marketplace_code=code,
                status=status,
                currency="CAD",
            )
        )
        db.commit()


@pytest.mark.parametrize(
    ("other_site", "expected"),
    [
        # 余额为零被跳过不是失败:全额付清的运行就是成功。
        ("SKIPPED", "SUCCEEDED"),
        # 另一站真的没做完(失败 / 被释放后留在 UNCERTAIN_FINANCIAL)才是部分完成。
        ("FAILED", "PARTIAL"),
        ("UNCERTAIN_FINANCIAL", "PARTIAL"),
    ],
)
def test_manual_settle_reaches_the_same_verdict_as_the_engine(
    app_client, other_site, expected
):
    """人工收尾与引擎收口用**同一个**判据(``engine.run_status_from_facts``)。

    ⚠️ 原来这里按「已派发条数 == 站点行数」算:一个因余额为零被跳过的站点就让
    全额付清的运行变成 PARTIAL,而引擎对同一组事实报 SUCCEEDED(09-23 自审复核)。
    同时钉住:只剩 SUBMITTED 的记录算已裁定,可以收尾。
    """

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, guard_id, _ = _guard_store_with_one_armed_guard(sessions)
    with sessions() as db:
        WorkflowRepository(db).transition_guard(
            guard_id, expected_states=("ARMED",), to_state="SUBMITTED"
        )
        db.commit()
    _add_site(sessions, run_id, "CA", other_site)
    _pin_run_uncertain(sessions, run_id)

    response = client.post(f"/api/runs/{run_id}/settle", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected


def test_start_check_on_a_store_with_no_supported_site_says_so(app_client):
    """只开了 FR 的店点「发起检查」(默认是提现流程):对话框里没有站点选择框,
    「请至少选择一个站点」让人无从下手。要直说这家店没启用这个流程能跑的站点。"""

    app, client, fake = app_client
    headers = {"X-CSRF-Token": bootstrap(client)}
    store = client.post(
        "/api/stores",
        json={
            "name": "FR Only",
            "selector_type": "id",
            "selector_value": "fr-only-1",
            "expected_seller_id": "A1SELLER",
        },
        headers=headers,
    ).json()
    patched = client.patch(
        f"/api/stores/{store['id']}",
        json={
            "identity_confirmed": True,
            "enabled": True,
            "marketplaces": [{"code": "FR", "enabled": True}],
        },
        headers=headers,
    )
    assert patched.status_code == 200, patched.text

    response = client.post(
        "/api/runs", json={"store_id": store["id"], "mode": "dry_run"}, headers=headers
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "没有启用这个流程支持的站点" in detail
    assert "CA" in detail and "UK" in detail and "AU" in detail
    assert not [call for call in fake.calls if call[0] == "enqueue"]


def test_manual_settle_closes_sites_that_were_still_waiting(app_client):
    """人工收尾也一样:还停在「等待验证 / 等待审核」的站点是本次没做的站点。"""

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, guard_id, _ = _guard_store_with_one_armed_guard(sessions)
    with sessions() as db:
        WorkflowRepository(db).transition_guard(
            guard_id, expected_states=("ARMED",), to_state="SUBMITTED"
        )
        db.commit()
    _add_site(sessions, run_id, "CA", "WAITING_AUTH")
    _pin_run_uncertain(sessions, run_id)

    response = client.post(f"/api/runs/{run_id}/settle", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PARTIAL"
    with sessions() as db:
        ca = db.scalar(
            select(SiteRun).where(SiteRun.run_id == run_id, SiteRun.marketplace_code == "CA")
        )
        assert ca.status == "CANCELLED"


def test_an_expired_approval_does_not_leave_a_zombie_run(app_client):
    """审批一超时,卡片(和那颗唯一的「取消任务」按钮)就消失了。运行还停在
    WAITING_APPROVAL —— 一个活跃状态 —— 于是永远否决这条排期的「立即执行」、
    删除排期和店铺建档重置,只能重启(2026-09-26 自审)。"""

    from datetime import timedelta

    from ziniao_automation.db import utc_now

    app, client, _ = app_client
    csrf = bootstrap(client)
    sessions = app.state.sessions
    run_id, _, _ = _guard_store_with_one_armed_guard(sessions)
    with sessions() as db:
        db.query(OperationGuard).delete()
        run = db.get(Run, run_id)
        run.status = "WAITING_APPROVAL"
        db.add(
            ApprovalRequest(
                run_id=run_id,
                status="PENDING",
                plan_hash="c" * 64,
                snapshot_hash="d" * 64,
                expires_at=utc_now() - timedelta(hours=1),
            )
        )
        site = db.scalar(select(SiteRun).where(SiteRun.run_id == run_id))
        site.status = "WAITING_APPROVAL"
        db.commit()

    assert client.get("/approvals").status_code == 200  # 这一下会把过期审批收掉

    with sessions() as db:
        run = db.get(Run, run_id)
        assert run.status == "CANCELLED"
        assert "审批已超时" in run.error
        assert run.finished_at is not None
        site = db.scalar(select(SiteRun).where(SiteRun.run_id == run_id))
        assert site.status == "CANCELLED"
    # 排期不再被它挡住
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    del csrf


def test_started_at_is_stamped_once_not_on_every_claim(app_client):
    """审批通过、人工验证恢复、收口认领都会再进一次 RUNNING。每次重盖的话,
    卡片上的「实际开始」「排队等待」会把两小时的审批时间算成排队(2026-09-26 自审)。"""

    app, client, _ = app_client
    bootstrap(client)
    sessions = app.state.sessions
    run_id, _, _ = _guard_store_with_one_armed_guard(sessions)
    with sessions() as db:
        repo = WorkflowRepository(db)
        assert repo.set_run_status(run_id, "RUNNING", allowed_from=("QUEUED",))
        db.commit()
        first = db.get(Run, run_id).started_at
        assert first is not None
        assert repo.set_run_status(run_id, "WAITING_AUTH", allowed_from=("RUNNING",))
        assert repo.set_run_status(run_id, "RUNNING", allowed_from=("WAITING_AUTH",))
        db.commit()
        assert db.get(Run, run_id).started_at == first
