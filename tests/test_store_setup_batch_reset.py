"""Batch reset is atomic, revision-checked and never deletes business history."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from ziniao_automation.config import Settings
from ziniao_automation.db import utc_now
from ziniao_automation.models import (Store, StoreMarketplace, Schedule, Run, SiteRun,
    OperationGuard, RunQueueEntry, StoreSetupProbe, Evidence, RunEvent)
from ziniao_automation.repositories import StoreRepository
from ziniao_automation.setup_repository import SetupRepository
from ziniao_automation.web import create_app

URL = "/api/stores/setup/reset-batch"


class FakeAutomation:
    def __init__(self):
        self.active = set()

    async def has_active_store_setup(self, store_id):
        return store_id in self.active


class FakeScheduler:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.report = None

    async def refresh_schedule(self, schedule_id):
        self.calls.append(schedule_id)
        if self.fail:
            raise RuntimeError("fixture refresh failed")
        return self.report


@pytest.fixture
def console(tmp_path):
    fake = FakeAutomation()
    app = create_app(Settings(project_root=tmp_path, data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'batch.db').as_posix()}", testing=True),
        automation_service=fake)
    with TestClient(app) as client:
        client.get("/")
        client.headers["X-CSRF-Token"] = client.cookies["ziniao_csrf"]
        scheduler = FakeScheduler()
        app.state.schedule_manager = scheduler
        yield SimpleNamespace(app=app, client=client, sessions=app.state.sessions,
                              fake=fake, scheduler=scheduler)


def seed(c, count=3):
    ids = []
    with c.sessions.begin() as db:
        for i in range(count):
            store = StoreRepository(db).create(name=f"批量测试店 {i}", selector_type="oauth",
                selector_value=f"fixture-{i}", expected_seller_id=f"SELLER-{i}")
            store.raw_profile = {"platform_id": "1"}
            store.identity_confirmed = True
            store.enabled = True
            for code, domain, currency in [("CA", "sellercentral.amazon.ca", "CAD"),
                ("UK", "sellercentral.amazon.co.uk", "GBP"),
                ("AU", "sellercentral.amazon.com.au", "AUD"),
                ("DE", "sellercentral.amazon.de", "EUR")]:
                db.add(StoreMarketplace(store_id=store.id, code=code, domain=domain,
                    currency=currency, enabled=True))
            for n in range(2):
                db.add(Schedule(store_id=store.id, name=f"排期 {i}-{n}",
                    first_run_at=utc_now(), next_run_at=utc_now(), enabled=True))
            ids.append(store.id)
    return ids


def payload(c, ids):
    with c.sessions() as db:
        return {"stores": [{"store_id": sid, "expected_setup_revision": db.get(Store, sid).setup_revision}
                           for sid in ids]}


def tables(c, names):
    with c.sessions() as db:
        return {name: [tuple(row) for row in db.execute(text(f"SELECT * FROM {name} ORDER BY 1"))]
                for name in names}


def probe(c, sid, finish=True):
    repo = SetupRepository(c.sessions)
    p = repo.begin(sid, ["CA"], kind="unified", seconds=1800, policy_version="fixture")
    repo.progress(p["probe_id"], sites=[{"code":"CA", "status":"CHECKING"}], lifecycle="CHECKING")
    repo.progress(p["probe_id"], sites=[{"code":"CA", "status":"SUCCEEDED",
        "identity_check":"PASSED", "access_check":"PASSED"}])
    if finish:
        repo.finish(p["probe_id"], outcome="SUCCEEDED", cleanup="CONFIRMED")
    return p["probe_id"]


def run(c, sid, status="SUCCEEDED"):
    with c.sessions.begin() as db:
        row = Run(store_id=sid, workflow="amazon_disbursement", mode="dry_run",
            trigger="manual", status=status, requested_by="fixture")
        db.add(row); db.flush()
        return row.id


def test_reset_selected_five_keeps_sites_history_guards_and_unselected_store(console):
    c = console; ids = seed(c, 6)
    probe(c, ids[0])
    rid = run(c, ids[0])
    with c.sessions.begin() as db:
        market = db.scalar(select(StoreMarketplace).where(StoreMarketplace.store_id == ids[0], StoreMarketplace.code == "CA"))
        site = SiteRun(run_id=rid, marketplace_id=market.id, marketplace_code="CA", status="SUBMITTED")
        db.add(site); db.flush()
        db.add(Evidence(run_id=rid, site_run_id=site.id, kind="screenshot",
                        file_path="fixture/evidence.png", sha256="e"*64, size_bytes=12))
        db.add(RunEvent(run_id=rid, site_run_id=site.id, event_type="FIXTURE",
                       message="retained audit event"))
        for state in ("ARMED", "SUBMITTED", "CONFIRMED", "UNCERTAIN"):
            db.add(OperationGuard(guard_key=f"guard-{state}", run_id=rid, site_run_id=site.id,
                store_id=ids[0], workflow="amazon_disbursement", marketplace_code="CA",
                settlement_key=state, state=state, amount=Decimal("1.00"), currency="CAD",
                plan_hash="p"*64, snapshot_hash="s"*64))
    preserved = ["store_marketplaces", "runs", "site_runs", "operation_guards", "evidence",
                 "store_history", "marketplace_history", "run_events", "store_setup_probes", "store_setup_site_results"]
    before = tables(c, preserved)
    body = payload(c, ids[:5])
    result = c.client.post(URL, json=body)
    assert result.status_code == 200, result.text
    assert result.json() == {"status":"reset", "reset_store_ids":ids[:5], "reset_count":5,
        "disabled_schedules":10, "scheduler_refreshed":True}
    assert len(c.scheduler.calls) == 1
    assert tables(c, preserved) == before
    with c.sessions() as db:
        for item in body["stores"]:
            store = db.get(Store, item["store_id"])
            assert store.expected_seller_id is None and not store.identity_confirmed and not store.enabled
            assert store.setup_revision > item["expected_setup_revision"]
            assert all(not s.enabled and s.next_run_at is None for s in store.schedules)
        untouched = db.get(Store, ids[-1])
        assert untouched.expected_seller_id == "SELLER-5" and untouched.enabled
        assert all(s.enabled and s.next_run_at for s in untouched.schedules)
    summary = SetupRepository(c.sessions).summary(ids[0])
    assert summary["configured"] is False
    assert summary["verification_outcome"] == "NEEDS_REVIEW"
    assert summary["marketplaces"] == {}
    assert probe(c, ids[0])  # reset does not make re-onboarding impossible


@pytest.mark.parametrize("blocker", ["active_memory", "QUEUED", "CHECKING", "WAITING_AUTH", "UNCONFIRMED",
    "run_RUNNING", "run_WAITING_APPROVAL", "run_WAITING_AUTH", "run_RECONCILING", "run_QUEUED", "queue_READY", "queue_CLAIMED"])
def test_one_blocked_store_means_zero_writes(console, blocker):
    c = console; ids = seed(c); sid = ids[1]
    if blocker == "active_memory":
        c.fake.active.add(sid)
    elif blocker.startswith("run_"):
        run(c, sid, blocker[4:])
    elif blocker.startswith("queue_"):
        rid = run(c, sid)
        with c.sessions.begin() as db:
            db.add(RunQueueEntry(run_id=rid, action="START", priority=10, state=blocker[6:],
                enqueued_at=utc_now()))
    else:
        pid = probe(c, sid, finish=blocker == "UNCONFIRMED")
        with c.sessions.begin() as db:
            row = db.get(StoreSetupProbe, pid)
            if blocker == "UNCONFIRMED": row.cleanup_state = blocker
            else: row.lifecycle = blocker
    before = tables(c, ["stores", "schedules"])
    result = c.client.post(URL, json=payload(c, ids))
    assert result.status_code == 409, result.text
    details = result.json()["detail"]
    assert details["code"] == "BATCH_SETUP_RESET_BLOCKED"
    assert details["targets"][0]["store_id"] == sid
    assert details["targets"][0]["store_name"] == "批量测试店 1"
    assert tables(c, before) == before
    assert c.scheduler.calls == []


@pytest.mark.parametrize("blocker", ["missing", "tiktok", "revision"])
def test_invalid_target_blocks_whole_batch(console, blocker):
    c = console; ids = seed(c); body = payload(c, ids)
    if blocker == "missing": body["stores"][1]["store_id"] = 999999
    elif blocker == "tiktok":
        with c.sessions.begin() as db: db.get(Store, ids[1]).raw_profile = {"platform_name":"TikTok Shop"}
    else:
        with c.sessions.begin() as db: db.get(Store, ids[1]).expected_seller_id = "CHANGED"
    before = tables(c, ["stores", "schedules"])
    assert c.client.post(URL, json=body).status_code == 409
    assert tables(c, before) == before


@pytest.mark.parametrize("stores", [[], [{"store_id":1,"expected_setup_revision":0}]*2,
    [{"store_id":-1,"expected_setup_revision":0}], [{"store_id":True,"expected_setup_revision":0}],
    [{"store_id":"1","expected_setup_revision":0}], [{"store_id":1,"expected_setup_revision":False}],
    [{"store_id":1,"expected_setup_revision":-1}], [{"store_id":1,"expected_setup_revision":0,"extra":1}]])
def test_strict_request_validation(console, stores):
    assert console.client.post(URL, json={"stores": stores}).status_code == 422


def test_csrf_and_top_level_extra_field(console):
    c = console; body = payload(c, seed(c))
    assert c.client.post(URL, json=body, headers={"X-CSRF-Token":"wrong"}).status_code == 403
    assert c.client.post(URL, json={**body,"force":True}).status_code == 422


def test_replay_is_blocked_after_first_commit(console):
    c = console; body = payload(c, seed(c))
    assert c.client.post(URL, json=body).status_code == 200
    before = tables(c, ["stores", "schedules"])
    assert c.client.post(URL, json=body).status_code == 409
    assert tables(c, before) == before
    assert len(c.scheduler.calls) == 1


@pytest.mark.parametrize("failure", ["error", "missing", "stale_projection"])
def test_refresh_failure_never_reports_committed_reset_as_failure(console, failure):
    c = console; ids = seed(c)
    if failure == "missing": c.app.state.schedule_manager = None
    elif failure == "error": c.scheduler.fail = True
    else: c.scheduler.report = SimpleNamespace(projected_schedule_ids=[1], failed_schedule_ids=[])
    result = c.client.post(URL, json=payload(c, ids))
    assert result.status_code == 200
    assert result.json()["scheduler_refreshed"] is False
    assert "建档已删除" in result.json()["warning"]
    with c.sessions() as db:
        assert all(db.get(Store, sid).expected_seller_id is None for sid in ids)
        assert not any(s.enabled for s in db.scalars(select(Schedule)))


def test_unexpected_second_store_failure_rolls_back_first(console, monkeypatch):
    c = console; ids = seed(c); body = payload(c, ids)
    original = StoreRepository.reset_setup
    def failing(self, sid):
        if sid == ids[1]: raise RuntimeError("fixture abort")
        return original(self, sid)
    monkeypatch.setattr(StoreRepository, "reset_setup", failing)
    before = tables(c, ["stores", "schedules"])
    with pytest.raises(RuntimeError, match="fixture abort"):
        c.client.post(URL, json=body)
    assert tables(c, before) == before
    assert c.scheduler.calls == []


def test_parallel_requests_only_one_reset_commits(console):
    c = console; body = payload(c, seed(c))
    with ThreadPoolExecutor(2) as pool:
        codes = list(pool.map(lambda _: c.client.post(URL, json=body).status_code, range(2)))
    assert sorted(codes) == [200, 409]
    assert len(c.scheduler.calls) == 1


def test_page_exposes_revision_selection_and_confirmation(console):
    c = console; seed(c)
    result = c.client.get("/stores")
    assert result.status_code == 200
    for marker in ("batch-reset-store-setups", "clear-setup-selection", "setup-reset-batch-dialog",
                   "data-setup-revision=", "data-setup-reset-confirm", "资金防重复记录"):
        assert marker in result.text
