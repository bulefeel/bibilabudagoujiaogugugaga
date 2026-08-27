from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import (
    ApprovalRequest,
    NotificationDelivery,
    OperationGuard,
    Run,
    RunQueueEntry,
    Schedule,
    ScheduleBatch,
    SiteRun,
    Store,
    StoreMarketplace,
)
from ziniao_automation.notifications import NotificationDeliveryService
from ziniao_automation.queue import DurableRunQueue, PersistentQueueWorker


# 09:00 Asia/Singapore. Amazon counts its 24-hour payout cap from the
# previous request, so schedules are an absolute anchor plus a period now.
SCHEDULE_ANCHOR = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
SCHEDULE_ANCHOR_ISO = "2026-01-01T01:00:00+00:00"


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


def _add_batch(factory, store_id: int, *, count: int, created_at: datetime):
    """Create scheduled runs whose child order is owned by one batch."""

    with factory() as db:
        batch = ScheduleBatch(
            request_id=str(uuid4()),
            definition_hash="a" * 64,
            definition_json={},
            schedule_count=count,
            created_at=created_at,
            updated_at=created_at,
        )
        db.add(batch)
        db.flush()
        rows: list[str] = []
        due = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
        for order in range(1, count + 1):
            schedule = Schedule(
                store_id=store_id,
                batch_id=batch.id,
                batch_order=order,
                name=f"batch-{count}-{order}",
                workflow="amazon_disbursement",
                mode="dry_run",
                first_run_at=SCHEDULE_ANCHOR,
                interval_minutes=1440,
                timezone="Asia/Singapore",
                marketplace_codes=["CA"],
                workflow_config={"marketplace_codes": ["CA"]},
                enabled=True,
            )
            db.add(schedule)
            db.flush()
            run = Run(
                store_id=store_id,
                schedule_id=schedule.id,
                workflow="amazon_disbursement",
                mode="dry_run",
                trigger="schedule",
                status="QUEUED",
                requested_by="test",
                scheduled_for_at=due,
            )
            db.add(run)
            db.flush()
            rows.append(run.id)
        db.commit()
        return rows


def test_enqueue_many_groups_batch_before_target_order(queue_env):
    """Two batches with orders 1/2 must run A1,A2,B1,B2, never 1,1,2,2."""

    factory, (store_id, _) = queue_env
    queue = DurableRunQueue(factory)
    first = _add_batch(
        factory,
        store_id,
        count=2,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    second = _add_batch(
        factory,
        store_id,
        count=2,
        # Deliberate timestamp collision: batch_scope_id, not clock precision,
        # must keep A1,A2 before B1,B2.
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    queue.enqueue_many(first)
    queue.enqueue_many(second)

    seen: list[str] = []
    while True:
        entry = queue.claim_next()
        if entry is None:
            break
        seen.append(entry.run_id)
        queue.finish(entry.id)
    assert seen == [*first, *second]


def test_batch_anchor_survives_schedule_edit(queue_env):
    """Changing child orders after enqueue cannot reorder immutable queue work."""

    factory, (store_id, _) = queue_env
    queue = DurableRunQueue(factory)
    ids = _add_batch(
        factory,
        store_id,
        count=2,
        created_at=datetime(2026, 8, 12, 0, 2, tzinfo=timezone.utc),
    )
    queue.enqueue_many(ids)
    with factory() as db:
        schedules = list(db.query(Schedule).order_by(Schedule.batch_order))
        # A legal edit (the unique batch-order constraint prevents swapping
        # both rows in one UPDATE); making child 1 sort after child 2 would
        # expose any accidental reread of mutable Schedule metadata.
        schedules[0].batch_order = 99
        db.commit()

    first = queue.claim_next()
    assert first is not None and first.run_id == ids[0]
    queue.finish(first.id)
    second = queue.claim_next()
    assert second is not None and second.run_id == ids[1]


def test_legacy_single_enqueue_uses_first_actual_batch_enqueue_time(queue_env):
    """An old batch joins today's FIFO and its later children stay together."""

    factory, (store_id, _) = queue_env
    queue = DurableRunQueue(factory)
    historical_created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ids = _add_batch(
        factory,
        store_id,
        count=2,
        created_at=historical_created_at,
    )

    queue.enqueue(ids[0])
    queue.enqueue(ids[1])

    with factory() as db:
        entries = list(db.query(RunQueueEntry).order_by(RunQueueEntry.id))
        assert len(entries) == 2
        assert entries[0].enqueued_at == entries[1].enqueued_at
        # SQLite loads timezone-aware columns as naive values, so compare the
        # calendar value rather than mixing aware/naive datetime instances.
        assert entries[0].enqueued_at.year != historical_created_at.year
        assert entries[0].batch_scope_id == entries[1].batch_scope_id


def test_run_batch_snapshot_wins_if_schedule_changes_before_enqueue(queue_env):
    """Creating a Run freezes routing even before its queue row is written."""

    factory, (store_id, _) = queue_env
    queue = DurableRunQueue(factory)
    run_id = _add_batch(
        factory,
        store_id,
        count=1,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )[0]
    with factory() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.schedule is not None
        original_batch_id = run.schedule.batch_id
        run.result_summary = {
            **(run.result_summary or {}),
            "schedule_batch_id": original_batch_id,
            "schedule_batch_order": 1,
        }
        # Simulate an edit between Run creation and enqueue.  The queue must
        # consume the immutable Run snapshot, not these mutable values.
        replacement = ScheduleBatch(
            request_id=str(uuid4()),
            definition_hash="b" * 64,
            definition_json={},
            schedule_count=1,
        )
        db.add(replacement)
        db.flush()
        run.schedule.batch_id = replacement.id
        run.schedule.batch_order = 99
        db.commit()

    entry_id, created = queue.enqueue(run_id)
    assert created is True
    with factory() as db:
        entry = db.get(RunQueueEntry, entry_id)
        assert entry is not None
        assert entry.batch_scope_id == original_batch_id
        assert entry.target_order == 1


@pytest.mark.asyncio
async def test_worker_cancellation_leaves_claimed_entry_for_startup_recovery(
    queue_env,
):
    factory, (store_id, _) = queue_env
    queue = DurableRunQueue(factory)
    run_id = add_run(factory, store_id)
    entry_id, _ = queue.enqueue(run_id)
    execution_started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def execute(_entry):
        execution_started.set()
        await never_finishes.wait()

    worker = PersistentQueueWorker(queue, execute, poll_seconds=0.01)
    await worker.start()
    await asyncio.wait_for(execution_started.wait(), timeout=1)
    await worker.close()

    with factory() as db:
        entry = db.get(RunQueueEntry, entry_id)
        assert entry is not None
        assert entry.state == "CLAIMED"
        assert entry.finished_at is None

    queue.restore_claimed(set())
    recovered = queue.claim_next()
    assert recovered is not None
    assert recovered.id == entry_id
    assert recovered.run_id == run_id


def _add_cross_day_run(factory, store_id: int) -> str:
    with factory() as db:
        schedule = Schedule(
            store_id=store_id,
            name="cross-day",
            workflow="amazon_disbursement",
            mode="dry_run",
            first_run_at=SCHEDULE_ANCHOR,
            interval_minutes=1440,
            timezone="UTC",
            marketplace_codes=["CA"],
            workflow_config={"marketplace_codes": ["CA"]},
            enabled=True,
        )
        db.add(schedule)
        db.flush()
        run = Run(
            store_id=store_id,
            schedule_id=schedule.id,
            workflow="amazon_disbursement",
            mode="dry_run",
            trigger="schedule",
            status="QUEUED",
            requested_by="test",
            scheduled_for_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        db.add(run)
        db.commit()
        return run.id


class _FlakyNoticeSender:
    def __init__(self, *, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def send(self, _notice) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("fixture transient notification failure")


@pytest.mark.asyncio
async def test_cross_day_notice_marks_after_persistent_retry_succeeds(queue_env):
    factory, (store_id, _) = queue_env
    queue = DurableRunQueue(factory)
    run_id = _add_cross_day_run(factory, store_id)
    entry_id, _ = queue.enqueue(run_id)
    sender = _FlakyNoticeSender(failures=1)
    notifications = NotificationDeliveryService(factory, sender)
    executed: list[str] = []

    async def execute(entry):
        executed.append(entry.run_id)

    worker = PersistentQueueWorker(
        queue,
        execute,
        notifications=notifications,
        poll_seconds=0.01,
    )
    await worker.start()
    await worker.wait_idle()
    await worker.close()

    assert executed == [run_id]
    assert sender.calls == 2
    with factory() as db:
        entry = db.get(RunQueueEntry, entry_id)
        delivery = db.query(NotificationDelivery).one()
        assert entry is not None and entry.cross_day_notified_at is not None
        assert (delivery.status, delivery.attempts) == ("SENT", 2)

    # The delivery receipt, not the queue marker, is the final duplicate
    # guard.  Repeating the event after recovery cannot send a second card.
    assert await notifications.notify_cross_day_started(run_id) is False
    assert sender.calls == 2


@pytest.mark.asyncio
async def test_cross_day_notice_failure_never_skips_business_execution(queue_env):
    factory, (store_id, _) = queue_env
    queue = DurableRunQueue(factory)
    run_id = _add_cross_day_run(factory, store_id)
    entry_id, _ = queue.enqueue(run_id)
    sender = _FlakyNoticeSender(failures=99)
    notifications = NotificationDeliveryService(factory, sender)
    executed: list[str] = []

    async def execute(entry):
        executed.append(entry.run_id)

    worker = PersistentQueueWorker(
        queue,
        execute,
        notifications=notifications,
        poll_seconds=0.01,
    )
    await worker.start()
    await worker.wait_idle()
    await worker.close()

    assert executed == [run_id]
    assert sender.calls == 2
    with factory() as db:
        entry = db.get(RunQueueEntry, entry_id)
        delivery = db.query(NotificationDelivery).one()
        assert entry is not None and entry.cross_day_notified_at is None
        assert entry.state == "DONE"
        assert (delivery.status, delivery.attempts) == ("FAILED", 2)
