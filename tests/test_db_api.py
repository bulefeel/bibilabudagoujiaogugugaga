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

from ziniao_automation.config import Settings
from ziniao_automation.models import (
    ApprovalRequest,
    OperationGuard,
    Run,
    RunEvent,
    RunQueueEntry,
    Schedule,
    StoreMarketplace,
    SystemSetting,
    ZiniaoAccount,
)
from ziniao_automation.repositories import StoreRepository, WorkflowRepository
from ziniao_automation.web import create_app
from ziniao_automation.ziniao.errors import ZiniaoCredentialError


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
    response = client.post(
        "/auth/bootstrap",
        json={
            "username": "operator",
            "password": "Local-Ledger-2026!",
            "confirm_password": "Local-Ledger-2026!",
        },
    )
    assert response.status_code == 200, response.text
    assert "ziniao_session" in client.cookies
    return client.cookies["ziniao_csrf"]


def test_one_time_setup_login_and_csrf(app_client):
    _, client, _ = app_client
    assert client.get("/").history[-1].headers["location"] == "/setup"
    csrf = bootstrap(client)

    duplicate = client.post(
        "/auth/bootstrap",
        json={
            "username": "another",
            "password": "Another-Strong-2026!",
            "confirm_password": "Another-Strong-2026!",
        },
    )
    assert duplicate.status_code == 409

    no_header = client.post(
        "/api/stores",
        json={"name": "店铺 A", "selector_type": "oauth", "selector_value": "oauth-a"},
    )
    assert no_header.status_code == 403

    denied = client.post(
        "/api/stores",
        json={"name": "店铺 A", "selector_type": "oauth", "selector_value": "oauth-a"},
        headers={"X-CSRF-Token": "incorrect"},
    )
    assert denied.status_code == 403

    logout = client.post("/auth/logout", json={}, headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 200
    login = client.post(
        "/auth/login", json={"username": "operator", "password": "Local-Ledger-2026!"}
    )
    assert login.status_code == 200














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
        assert repo.has_financial_guard(run1.id)
        assert sorted(g.id for g in repo.recovery_guards()) == sorted(
            [guard1.id, next_day.id]
        )
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
        assert "系统本身并未回读到该记录" in events[0].message


def _pin_run_uncertain(sessions, run_id: str) -> None:
    with sessions() as db:
        WorkflowRepository(db).set_run_status(
            run_id, "UNCERTAIN_FINANCIAL", allowed_from=("QUEUED",)
        )
        db.commit()


def test_settle_refuses_while_any_guard_is_still_undecided(app_client):
    """Closing a run must never be a way to skip the money questions.

    The whole point of the block is that a recorded payout has not been read
    back. Ending the run while a guard is still ARMED/SUBMITTED/UNCERTAIN would
    let an operator clear the schedule without ever deciding what happened to
    the transfer.
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
    released = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/release",
        headers={"X-CSRF-Token": csrf},
    )
    assert released.status_code == 200, released.text
    _pin_run_uncertain(sessions, run_id)

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


def test_acknowledge_endpoint_refuses_a_guard_that_never_dispatched(app_client):
    """Never let the two exits blur: this one must not invent a payout.

    A guard with no dispatch recorded is the release case.  Acknowledging it
    would assert that money moved on no evidence at all — the exact claim this
    whole workflow exists to get right.
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

    response = client.post(
        f"/api/runs/{run_id}/guards/{guard_key}/acknowledge",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409
    assert "确认从未发出" in response.json()["detail"]

    with sessions() as db:
        assert db.get(OperationGuard, guard_id).state == "UNCERTAIN"




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
    assert "/static/app.js?v=20260828-no-funds-block" in page.text
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
    assert "CA、UK、AU 分别计数" in page.text
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
    assert "0.4.1" in page.text
    assert "数据库迁移版本" in page.text
    assert "0007" in page.text


def test_webdriver_switch_is_refused_while_a_payout_holds_the_browser(app_client):
    """Killing Ziniao mid-run could strand a payout between ARM and the click."""

    _, client, fake = app_client
    csrf = bootstrap(client)

    async def busy() -> dict[str, object]:
        return {
            "ok": False,
            "status": "busy",
            "message": "现在切换会关掉正在使用的紫鸟窗口，因此已阻止：\n有提现任务正在执行（资金锁被占用）",
            "closed_processes": 0,
            "details": {},
        }

    fake.start_webdriver_mode = busy

    response = client.post(
        "/api/ziniao/webdriver/start", headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 409
    assert "资金锁" in response.json()["detail"]


def test_webdriver_switch_reports_success_with_what_it_closed(app_client):
    _, client, fake = app_client
    csrf = bootstrap(client)

    async def ready() -> dict[str, object]:
        return {
            "ok": True,
            "status": "ready",
            "message": "紫鸟 WebDriver 模式已就绪（127.0.0.1:16851）。",
            "closed_processes": 2,
            "details": {},
        }

    fake.start_webdriver_mode = ready

    response = client.post(
        "/api/ziniao/webdriver/start", headers={"X-CSRF-Token": csrf}
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
