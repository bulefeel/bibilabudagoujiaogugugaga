"""Sync removes live profiles, never historical operations or evidence files."""
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from tests.test_db_api import FakeAutomation, bootstrap
from ziniao_automation.config import Settings
from ziniao_automation.db import utc_now
from ziniao_automation.models import (
    Store, StoreHistory, StoreMarketplace, Schedule, Run, SiteRun, OperationGuard,
    Evidence, ApprovalRequest, RunQueueEntry, FeedbackReview, ZiniaoAccount,
)
from ziniao_automation.web import create_app
from ziniao_automation.workflows.runtime import DatabaseRunLoader


def profile(key):
    return dict(name=f"Fixture {key}", selector_type="oauth", selector_value=key,
                browser_oauth=key, raw={"platform_id": 1, "platform_name": "Amazon-CA"})


class SyncFake(FakeAutomation):
    def __init__(self):
        super().__init__()
        self.profiles = [profile("one"), profile("two")]
        self.complete = True
        self.error = False
    async def sync_ziniao(self):
        if self.error:
            raise RuntimeError("fixture sync error")
        return {"profiles": self.profiles, "complete": self.complete}


@pytest.fixture
def catalog(tmp_path):
    fake = SyncFake()
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", testing=True)
    app = create_app(settings, automation_service=fake)
    with TestClient(app) as client:
        headers = {"X-CSRF-Token": bootstrap(client)}
        response = client.post("/api/ziniao/sync", json={}, headers=headers)
        assert response.status_code == 200, response.text
        stores = {s["selector_value"]: s["id"] for s in client.get("/api/stores").json()}
        yield app, client, headers, fake, stores, tmp_path


def sync(catalog):
    _, client, headers, *_ = catalog
    return client.post("/api/ziniao/sync", json={}, headers=headers)


def seed_history(catalog, *, state="SUCCEEDED", queue_state="DONE", legacy=False):
    app, _, _, _, ids, tmp = catalog
    screenshot = tmp / "data" / "evidence" / "fixture.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"fixture evidence, do not remove")
    with app.state.sessions.begin() as db:
        from sqlalchemy.orm import defer
        store = db.get(Store, ids["two"], options=[defer(Store.setup_revision)] if legacy else None)
        store.expected_seller_id = "FIXTURE-SELLER"
        store.identity_confirmed = True
        store.enabled = True
        market = StoreMarketplace(store_id=store.id, code="CA", domain="sellercentral.amazon.ca", currency="CAD", enabled=True)
        db.add(market); db.flush()
        schedule = Schedule(store_id=store.id, name="Fixture schedule", first_run_at=utc_now(), enabled=True)
        db.add(schedule); db.flush()
        run = Run(store_id=store.id, schedule_id=schedule.id, workflow="amazon_disbursement", mode="approval", status=state)
        db.add(run); db.flush()
        site = SiteRun(run_id=run.id, marketplace_id=market.id, marketplace_code="CA", currency="CAD", status="SUCCEEDED")
        db.add(site); db.flush()
        guard = OperationGuard(run_id=run.id, site_run_id=site.id, store_id=store.id,
            guard_key="fixture-per-day-guard", workflow="amazon_disbursement", marketplace_code="CA",
            settlement_key="fixture-cycle", state="SUBMITTED", amount="12.34", currency="CAD",
            plan_hash="fixture-plan", snapshot_hash="fixture-snapshot", payout_account_tail="1234", submitted_at=utc_now())
        db.add(guard)
        evidence = Evidence(run_id=run.id, site_run_id=site.id, kind="screenshot", file_path=str(screenshot), sha256="0"*64)
        db.add(evidence)
        approval = ApprovalRequest(run_id=run.id, plan_hash="fixture", snapshot_hash="fixture", expires_at=utc_now()+timedelta(hours=1))
        db.add(approval)
        queue = RunQueueEntry(run_id=run.id, state=queue_state)
        db.add(queue)
        review = FeedbackReview(store_id=store.id, run_id=run.id, marketplace_code="CA", order_id="FIXTURE-ORDER", rating=1)
        db.add(review); db.flush()
        return dict(run=run.id, site=site.id, guard=guard.id, queue=queue.id, approval=approval.id,
                    evidence=evidence.id, review=review.id, schedule=schedule.id, screenshot=screenshot)


def test_full_sync_hard_deletes_store_sites_schedules_but_preserves_history(catalog):
    app, client, _, fake, ids, tmp = catalog
    saved = seed_history(catalog)
    fake.profiles = [profile("one")]
    response = sync(catalog)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1
    assert response.json()["deleted_schedule_ids"] == [saved["schedule"]]
    assert [s["id"] for s in client.get("/api/stores").json()] == [ids["one"]]
    with app.state.sessions() as db:
        assert db.get(Store, ids["two"]) is None
        assert not db.scalars(select(StoreMarketplace).where(StoreMarketplace.store_id == ids["two"])).all()
        assert db.get(Schedule, saved["schedule"]) is None
        assert db.get(StoreHistory, ids["two"]).name == "Fixture two"
        run = db.get(Run, saved["run"])
        assert run.status == "SUCCEEDED" and run.schedule_id is None
        assert run.store.name == "Fixture two"
        assert db.get(SiteRun, saved["site"]).marketplace.code == "CA"
        guard = db.get(OperationGuard, saved["guard"])
        assert str(guard.amount) == "12.34" and guard.payout_account_tail == "1234"
        assert guard.state == "SUBMITTED" and guard.guard_key == "fixture-per-day-guard"
        assert db.get(Evidence, saved["evidence"]) is not None
        assert db.get(FeedbackReview, saved["review"]) is not None
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    assert saved["screenshot"].read_bytes() == b"fixture evidence, do not remove"
    for url in ("/runs", f"/runs/{saved['run']}", "/feedback", "/schedules", "/stores", "/"):
        page = client.get(url)
        assert page.status_code == 200, (url, page.text)
    assert "Fixture two" in client.get(f"/runs/{saved['run']}").text
    assert list((tmp / "data" / "backups").glob("*.db"))
    assert sync(catalog).json()["deleted"] == 0


def test_pending_queue_and_approval_are_cancelled_without_altering_guard(catalog):
    app, _, _, fake, *_ = catalog
    saved = seed_history(catalog, state="QUEUED", queue_state="READY")
    fake.profiles = [profile("one")]
    assert sync(catalog).status_code == 200
    with app.state.sessions() as db:
        assert db.get(RunQueueEntry, saved["queue"]).state == "CANCELLED"
        assert db.get(ApprovalRequest, saved["approval"]).status == "CANCELLED"
        assert db.get(Run, saved["run"]).status == "UNCERTAIN_FINANCIAL"
        assert db.get(OperationGuard, saved["guard"]).state == "SUBMITTED"


@pytest.mark.parametrize("state,queue", [("RUNNING","DONE"), ("WAITING_AUTH","DONE"), ("QUEUED","CLAIMED")])
def test_sync_is_atomic_and_rejects_an_in_use_store(catalog, state, queue):
    app, _, _, fake, ids, _ = catalog
    saved = seed_history(catalog, state=state, queue_state=queue)
    fake.profiles = [dict(profile("one"), name="must rollback")]
    response = sync(catalog)
    assert response.status_code == 409, response.text
    with app.state.sessions() as db:
        assert db.get(Store, ids["two"]) is not None
        assert db.get(Store, ids["one"]).name == "Fixture one"
        assert db.get(Schedule, saved["schedule"]) is not None


def test_active_setup_probe_prevents_deletion(catalog):
    app, _, _, fake, ids, _ = catalog
    fake.active_store_setup = True
    fake.profiles = [profile("one")]
    assert sync(catalog).status_code == 409
    with app.state.sessions() as db:
        assert db.get(Store, ids["two"]) is not None


@pytest.mark.parametrize("kind", ["incomplete", "error", "duplicate", "invalid"])
def test_bad_or_unproven_snapshot_never_deletes(catalog, kind):
    app, _, _, fake, ids, _ = catalog
    fake.profiles = []
    if kind == "incomplete": fake.complete = False
    elif kind == "error": fake.error = True
    elif kind == "duplicate": fake.profiles = [profile("one"), profile("one")]
    else: fake.profiles = [{}]
    response = sync(catalog)
    assert response.status_code == (200 if kind == "incomplete" else 503 if kind == "error" else 502)
    with app.state.sessions() as db:
        assert db.get(Store, ids["two"]) is not None


def test_explicit_empty_complete_list_deletes_synced_only(catalog):
    app, client, _, fake, ids, _ = catalog
    with app.state.sessions.begin() as db:
        manual = Store(name="Manual fixture", selector_type="id", selector_value="manual")
        account = ZiniaoAccount(display_name="Other account")
        db.add(account); db.flush()
        other = Store(name="Other fixture", selector_type="id", selector_value="other", account_id=account.id, last_seen_at=utc_now())
        db.add_all([manual, other]); db.flush()
        preserved = {manual.id, other.id}
    fake.profiles = []
    response = sync(catalog)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 2
    assert {s["id"] for s in client.get("/api/stores").json()} == preserved


@pytest.mark.asyncio
async def test_regrant_uses_same_identity_without_replaying_historical_runs(catalog):
    app, client, _, fake, ids, _ = catalog
    saved = seed_history(catalog)
    fake.profiles = [profile("one")]
    assert sync(catalog).json()["deleted"] == 1
    fake.profiles = [profile("one"), profile("three"), profile("two")]
    response = sync(catalog)
    assert response.status_code == 200, response.text
    stores = {s["selector_value"]: s for s in client.get("/api/stores").json()}
    assert stores["two"]["id"] == ids["two"]
    assert stores["three"]["id"] not in ids.values()
    assert stores["two"]["enabled"] is False
    loader = DatabaseRunLoader(app.state.sessions)
    with pytest.raises(ValueError, match="仅供查看"):
        await loader(saved["run"])
    plan = await loader.startup_recovery_plan()
    assert not plan.financial and not plan.queued_ids and not plan.reconciling_ids


def test_scheduler_refresh_failure_does_not_roll_back_deletion(catalog):
    app, _, _, fake, ids, _ = catalog
    saved = seed_history(catalog)
    class BrokenManager:
        async def refresh(self): raise RuntimeError("fixture scheduler failure")
    app.state.schedule_manager = BrokenManager()
    fake.profiles = [profile("one")]
    response = sync(catalog)
    assert response.status_code == 200 and response.json()["deleted"] == 1
    assert response.json()["scheduler_refreshed"] is False
    with app.state.sessions() as db:
        assert db.get(Store, ids["two"]) is None
        assert db.get(Run, saved["run"]) is not None


def test_ordinary_waiting_run_is_cancelled_and_never_claimed(catalog):
    from ziniao_automation.queue import DurableRunQueue
    from sqlalchemy import delete
    app, _, _, fake, _, _=catalog
    saved=seed_history(catalog,state="WAITING_APPROVAL",queue_state="READY")
    with app.state.sessions.begin() as db:
        db.execute(delete(OperationGuard).where(OperationGuard.id==saved["guard"]))
    fake.profiles=[profile("one")]
    assert sync(catalog).status_code==200
    assert DurableRunQueue(app.state.sessions).claim_next() is None
    with app.state.sessions() as db:
        assert db.get(Run,saved["run"]).status=="CANCELLED"


def test_worker_waits_for_sync_transaction_and_sees_the_cancelled_queue(catalog):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from ziniao_automation.queue import DurableRunQueue
    from ziniao_automation.repositories import StoreRepository
    app, _, _, _, _, _=catalog
    seed_history(catalog,state="QUEUED",queue_state="READY")
    ready=Event()
    def claim():
        ready.set()
        return DurableRunQueue(app.state.sessions).claim_next()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with app.state.sessions() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            account=db.scalar(select(ZiniaoAccount))
            future=executor.submit(claim)
            assert ready.wait(2)
            result=StoreRepository(db).delete_missing_profiles([profile("one")],account_id=account.id)
            assert result["deleted"]==1
            db.commit()
        assert future.result(timeout=10) is None


def test_complete_flag_without_a_list_never_deletes(catalog):
    app, _, _, fake, ids, _=catalog
    async def malformed(): return {"complete":True}
    fake.sync_ziniao=malformed
    assert sync(catalog).status_code==502
    with app.state.sessions() as db:
        assert db.get(Store,ids["two"]) is not None
