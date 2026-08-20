from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import ApprovalRequest, OperationGuard, Run, RunQueueEntry, SiteRun, Store, StoreMarketplace
from ziniao_automation.queue import DurableRunQueue, PersistentQueueWorker


@pytest.fixture()
def queue_env(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'queue.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        store = Store(
            name="Queue Store", selector_type="id", selector_value="profile",
            expected_seller_id="seller", identity_confirmed=True, enabled=True,
        )
        db.add(store); db.flush()
        market = StoreMarketplace(
            store_id=store.id, code="CA", domain="sellercentral.amazon.ca",
            currency="CAD", enabled=True,
        )
        db.add(market); db.commit()
        keys = (store.id, market.id)
    yield factory, keys
    engine.dispose()


def add_run(factory, store_id, *, trigger="manual", due=None, status="QUEUED"):
    with factory() as db:
        row = Run(
            store_id=store_id, workflow="amazon_disbursement", mode="dry_run",
            trigger=trigger, status=status, requested_by="test", scheduled_for_at=due,
        )
        db.add(row); db.commit()
        return row.id


@pytest.mark.asyncio
async def test_five_nine_then_nine_ten_and_late_items_are_not_dropped(queue_env):
    factory, (store_id, _) = queue_env
    queue = DurableRunQueue(factory)
    nine = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
    ids = [add_run(factory, store_id, trigger="schedule", due=nine) for _ in range(5)]
    ids.append(add_run(factory, store_id, trigger="schedule", due=nine + timedelta(minutes=10)))
    for run_id in ids:
        queue.enqueue(run_id)
    seen = []
    async def execute(entry):
        seen.append(entry.run_id)
        await asyncio.sleep(0)
    worker = PersistentQueueWorker(queue, execute, poll_seconds=.01)
    await worker.start(); await worker.wait_idle(); await worker.close()
    assert seen == ids
    with factory() as db:
        assert {x.state for x in db.query(RunQueueEntry).all()} == {"DONE"}


@pytest.mark.asyncio
async def test_manual_priority_becomes_next_but_never_interrupts_claimed(queue_env):
    factory, (store_id, _) = queue_env
    queue = DurableRunQueue(factory)
    scheduled = [add_run(factory, store_id, trigger="schedule") for _ in range(2)]
    for run_id in scheduled:
        queue.enqueue(run_id)
    first = queue.claim_next()
    assert first and first.run_id == scheduled[0]
    manual = add_run(factory, store_id)
    queue.enqueue(manual)
    queue.finish(first.id)
    assert queue.claim_next().run_id == manual


def test_claimed_restart_keeps_row_and_financial_becomes_reconcile(queue_env):
    factory, (store_id, market_id) = queue_env
    queue = DurableRunQueue(factory)
    ordinary = add_run(factory, store_id, status="RUNNING")
    financial = add_run(factory, store_id, status="RUNNING")
    ordinary_entry, _ = queue.enqueue(ordinary)
    financial_entry, _ = queue.enqueue(financial)
    assert queue.claim_next() is not None
    # Claim the second as well to model a legacy/crash fixture; production has one worker.
    assert queue.claim_next() is not None
    with factory() as db:
        site = SiteRun(run_id=financial, marketplace_id=market_id, marketplace_code="CA", status="ARMED")
        db.add(site); db.flush()
        db.add(OperationGuard(
            guard_key="guard", run_id=financial, site_run_id=site.id, store_id=store_id,
            workflow="amazon_disbursement", marketplace_code="CA", settlement_key="cycle",
            state="ARMED", amount=1, currency="CAD", plan_hash="a"*64, snapshot_hash="b"*64,
        )); db.commit()
    queue.restore_claimed({financial})
    with factory() as db:
        a = db.get(RunQueueEntry, ordinary_entry); b = db.get(RunQueueEntry, financial_entry)
        assert a.state == b.state == "READY"
        assert a.id == ordinary_entry and b.id == financial_entry
        assert b.action == "RECONCILE" and b.priority == 0


def test_approval_is_atomic_and_worker_action_is_persisted(queue_env):
    factory, (store_id, _) = queue_env
    queue = DurableRunQueue(factory)
    run_id = add_run(factory, store_id, status="WAITING_APPROVAL")
    with factory() as db:
        approval = ApprovalRequest(
            run_id=run_id, plan_hash="a"*64, snapshot_hash="b"*64,
            plan_json={}, status="PENDING", expires_at=datetime.now(timezone.utc)+timedelta(hours=1),
        )
        db.add(approval); db.commit(); approval_id = approval.id
    entry_id = queue.approve_and_enqueue(run_id, approval_id, actor="operator")
    with factory() as db:
        assert db.get(Run, run_id).status == "QUEUED"
        assert db.get(ApprovalRequest, approval_id).status == "APPROVED"
        entry = db.get(RunQueueEntry, entry_id)
        assert (entry.action, entry.priority, entry.state) == ("APPROVE", 10, "READY")
