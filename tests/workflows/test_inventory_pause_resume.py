"""Offline inventory pause/resume: original snapshot, explicit durable restart."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from ziniao_automation.models import InventoryGpsrItem, Run, RunEvent, RunQueueEntry, SiteRun, Store
from ziniao_automation.queue import DurableRunQueue, PRIORITY_HUMAN
from ziniao_automation.repositories import ConflictError, WorkflowRepository
from ziniao_automation.workflows.amazon_inventory_gpsr.store import (
    CONFIRMED, ELIGIBLE, FAILED, PENDING, UNCERTAIN, can_resume_inventory_run,
)
from ziniao_automation.workflows.errors import PageRecoveryRequired
from ziniao_automation.workflows.types import RunMode, RunStatus, SiteStatus

from .test_inventory_gpsr_workflow import Adapter, _real_runtime, execute, harness, items


def unavailable(status=403):
    return PageRecoveryRequired("fixture HTTP unavailable", http_status=status)


def quiet_worker(service):
    service._worker = SimpleNamespace(start=AsyncMock(), wake=Mock())


async def start(harness, engine, run):
    await harness.repo.set_run_status(run.id, RunStatus.QUEUED)
    return await engine.start(run)


@pytest.mark.parametrize("mode", [RunMode.AUTO, RunMode.DRY_RUN])
async def test_thirteen_pause_on_fifth_resume_only_remaining_nine(harness, mode):
    adapter = Adapter(items(13))
    adapter.results["sku-8"] = unavailable()
    adapter.begin_attempt = Mock()
    adapter.detach_response_observers = Mock()
    run = harness.new_run(mode=mode)
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    notices = []
    engine.notifier = SimpleNamespace(send=AsyncMock(side_effect=lambda report: notices.append(report)))
    result = await start(harness, engine, run)
    assert result.status is RunStatus.PAUSED
    assert await harness.repo.get_site_status(run.id, "DE") is SiteStatus.PAUSED
    rows = await harness.ledger.list_items(run.id)
    first_four = [(row.id, row.state, row.intent_at) for row in rows[:4]]
    assert sum(row.state == PENDING for row in rows) == 9
    assert not any(row.state == FAILED for row in rows)
    assert notices[-1].status is RunStatus.PAUSED
    adapter.detach_response_observers.assert_called_once()
    with harness.factory() as session:
        stored = session.get(Run, run.id)
        assert stored.finished_at is None and can_resume_inventory_run(session, stored)
        event = session.scalar(select(RunEvent).where(RunEvent.event_type == "inventory_gpsr_paused"))
        assert event.details == {"reason_code": "inventory_page_unavailable", "http_status": 403,
                                 "remaining": 9, "uncertain": 0}
    adapter.results.clear()
    adapter.items = items(20)  # Resume must never enlarge the old candidate set.
    inspected_before = list(adapter.inspected)
    await service.resume_inventory_run(run.id)
    await service.resume_inventory_run(run.id)  # double click is idempotent
    with harness.factory() as session:
        entries = session.scalars(select(RunQueueEntry)).all()
        assert len(entries) == 1 and entries[0].priority == PRIORITY_HUMAN
        assert entries[0].action == "START"
    claimed = service._queue.claim_next()
    result = await engine.start(run)
    service._queue.finish(claimed.id)
    assert result.status is RunStatus.SUCCEEDED
    assert adapter.inspected[len(inspected_before):] == [f"sku-{i}" for i in reversed(range(9))]
    assert adapter.scans == 1
    assert adapter.begin_attempt.call_count == 2
    rows = await harness.ledger.list_items(run.id)
    assert [(row.id, row.state, row.intent_at) for row in rows[:4]] == first_four
    assert len(rows) == 13 and not any(row.state == PENDING for row in rows)
    assert adapter.saved == ([] if mode is RunMode.DRY_RUN else [f"sku-{i}" for i in reversed(range(13))])


async def test_pause_after_intent_resumes_readback_never_clicks_twice(harness):
    adapter = Adapter(items(3))
    adapter.on_confirm = lambda item: _raise_unavailable()
    run = harness.new_run()
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    assert (await start(harness, engine, run)).status is RunStatus.PAUSED
    assert (await harness.ledger.list_items(run.id))[0].state == UNCERTAIN
    assert adapter.saved == ["sku-2"]
    adapter.on_confirm = None
    await service.resume_inventory_run(run.id)
    result = await engine.start(run)
    assert result.status is RunStatus.SUCCEEDED
    assert adapter.reconciled == ["sku-2"]
    assert adapter.saved == ["sku-2", "sku-1", "sku-0"]


async def _raise_unavailable():
    raise unavailable(503)


async def test_current_candidate_read_pause_keeps_item_pending_not_blocked(harness):
    adapter = Adapter(items(1))
    previous = harness.new_run("previous")
    await execute(harness, adapter, previous)
    await harness.repo.set_run_status(previous.id, RunStatus.SUCCEEDED)
    adapter.items = items(3)
    normal = adapter.inspect_item
    adapter.inspect_item = AsyncMock(side_effect=unavailable(429))
    run = harness.new_run()
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    assert (await start(harness, engine, run)).status is RunStatus.PAUSED
    assert not any(row.state in {FAILED, "BLOCKED"} for row in await harness.ledger.list_items(run.id))
    assert (await harness.ledger.list_items(previous.id))[0].state == CONFIRMED
    adapter.inspect_item = normal
    adapter.results["sku-0"] = "already_submitted"
    await service.resume_inventory_run(run.id)
    assert (await engine.start(run)).status is RunStatus.SUCCEEDED
    assert adapter.saved.count("sku-0") == 1 and not adapter.reconciled


async def test_paused_cancel_preserves_item_results_and_rejects_resume(harness):
    adapter = Adapter(items(3))
    adapter.results["sku-1"] = unavailable()
    run = harness.new_run()
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    await start(harness, engine, run)
    before = await harness.ledger.list_items(run.id)
    await service.cancel_run(run.id)
    assert await harness.repo.get_run_status(run.id) is RunStatus.CANCELLED
    assert await harness.repo.get_site_status(run.id, "DE") is SiteStatus.CANCELLED
    assert await harness.ledger.list_items(run.id) == before
    with pytest.raises(RuntimeError):
        await service.resume_inventory_run(run.id)


async def test_restart_does_not_restore_paused_claim_or_enqueue_paused_run(harness):
    adapter = Adapter(items(1))
    adapter.results["sku-0"] = unavailable()
    run = harness.new_run()
    service, engine, _ = _real_runtime(harness, adapter)
    await harness.repo.set_run_status(run.id, RunStatus.QUEUED)
    service._queue.enqueue(run.id)
    claimed = service._queue.claim_next()
    assert (await engine.start(run)).status is RunStatus.PAUSED
    # Crash here, before old worker.finish(claimed.id).
    restored, _, _ = _real_runtime(harness, adapter)
    quiet_worker(restored)
    await restored.recover_startup()
    assert await harness.repo.get_run_status(run.id) is RunStatus.PAUSED
    assert restored._queue.claim_next() is None
    with harness.factory() as session:
        assert session.get(RunQueueEntry, claimed.id).state == "DONE"
    await restored.resume_inventory_run(run.id)
    assert restored._queue.claim_next().run_id == run.id


async def test_resume_waits_for_old_claim_then_is_atomic_on_queue_failure(harness, monkeypatch):
    adapter = Adapter(items(1))
    adapter.results["sku-0"] = unavailable()
    run = harness.new_run()
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    await harness.repo.set_run_status(run.id, RunStatus.QUEUED)
    service._queue.enqueue(run.id)
    claimed = service._queue.claim_next()
    await engine.start(run)
    with pytest.raises(RuntimeError, match="收尾"):
        await service.resume_inventory_run(run.id)
    assert await harness.repo.get_run_status(run.id) is RunStatus.PAUSED
    service._queue.finish(claimed.id)
    monkeypatch.setattr(service._queue, "_business_priority", Mock(side_effect=RuntimeError("fixture queue failure")))
    with pytest.raises(RuntimeError, match="queue failure"):
        await service.resume_inventory_run(run.id)
    assert await harness.repo.get_run_status(run.id) is RunStatus.PAUSED
    with harness.factory() as session:
        assert session.scalar(select(RunQueueEntry.id).where(RunQueueEntry.state == "READY")) is None
        assert session.scalar(select(RunEvent.id).where(RunEvent.event_type == "inventory_gpsr_resumed")) is None


async def test_atomic_resume_survives_restart_before_worker_wakeup(harness):
    adapter = Adapter(items(1))
    adapter.results["sku-0"] = unavailable()
    run = harness.new_run()
    service, engine, _ = _real_runtime(harness, adapter)
    await start(harness, engine, run)
    service._queue.resume_inventory_run(run.id)
    restored, _, _ = _real_runtime(harness, adapter)
    quiet_worker(restored)
    await restored.recover_startup()
    with harness.factory() as session:
        ready = session.scalars(select(RunQueueEntry).where(RunQueueEntry.state == "READY")).all()
        assert len(ready) == 1 and ready[0].priority == PRIORITY_HUMAN
    assert restored._queue.claim_next().run_id == run.id


async def test_concurrent_resume_creates_one_durable_queue_entry(harness):
    adapter = Adapter(items(1))
    adapter.results["sku-0"] = unavailable()
    run = harness.new_run()
    service, engine, _ = _real_runtime(harness, adapter)
    await start(harness, engine, run)
    q1, q2 = DurableRunQueue(harness.factory), DurableRunQueue(harness.factory)
    results = await asyncio.gather(asyncio.to_thread(q1.resume_inventory_run, run.id),
                                   asyncio.to_thread(q2.resume_inventory_run, run.id))
    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    with harness.factory() as session:
        assert len(session.scalars(select(RunQueueEntry)).all()) == 1


async def legacy_failure(harness, status=403, *, baseline="binding", mode=RunMode.DRY_RUN):
    adapter = Adapter(items(3))
    run = harness.new_run(mode=mode)
    flow = harness.workflow(adapter)
    await harness.repo.set_site_status(run.id, "DE", SiteStatus.PREFLIGHT)
    await harness.ledger.save_snapshot(run_id=run.id, store_id=int(run.store.id), marketplace_code="DE", items=items(3))
    # Simulate the old writer, which did not persist identity at scan time.
    with harness.factory() as session:
        session.get(Run, run.id).result_summary = {}
        session.commit()
    if baseline == "binding":
        with harness.factory() as session:
            stored = session.get(Run, run.id)
            stored.result_summary = {"inventory_gpsr_binding": {
                "seller_id": run.store.expected_seller_id, "selector_type": run.store.selector_type,
                "selector_value": run.store.selector_value,
            }}
            session.commit()
    elif baseline == "plan":
        from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import _snapshot
        from ziniao_automation.workflows.types import PlanLine, WorkflowPlan
        await harness.repo.save_plan(WorkflowPlan(
            run.workflow, run.id, run.store.id,
            (PlanLine(run.marketplaces[0], _snapshot(run, run.marketplaces[0])),),
        ))
    rows = await harness.ledger.list_items(run.id)
    await flow._mark_item(run, run.marketplaces[0], rows[0], ELIGIBLE, "eligible", eligible=True)
    await flow._mark_item(run, run.marketplaces[0], rows[1], FAILED, "page_contract_or_identity_error")
    await harness.repo.set_run_status(run.id, RunStatus.FAILED, error=(
        f"InventoryContractError: 德国站库存接口返回 HTTP {status}，本次流程已停止；"
        "请在紫鸟确认页面恢复后再创建新任务"))
    return adapter, run, rows


@pytest.mark.parametrize("status", [403, 429, 500, 503, 599])
async def test_exact_legacy_http_failure_resets_only_failed_row(harness, status):
    adapter, run, original = await legacy_failure(harness, status)
    service, _, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    with harness.factory() as session:
        assert can_resume_inventory_run(session, session.get(Run, run.id))
    await service.resume_inventory_run(run.id)
    rows = await harness.ledger.list_items(run.id)
    assert [row.state for row in rows] == [ELIGIBLE, PENDING, PENDING]
    assert rows[0].id == original[0].id and rows[0].eligible
    assert rows[1].reason == "legacy_http_failure_resumed"


@pytest.mark.parametrize("damage", ["generic_error", "other_workflow", "no_snapshot", "unsettled", "wrong_failed_reason"])
async def test_legacy_resume_rejects_unrelated_or_unproven_failure(harness, damage):
    adapter, run, _ = await legacy_failure(harness)
    with harness.factory() as session:
        stored = session.get(Run, run.id)
        if damage == "generic_error":
            stored.error = "RuntimeError: HTTP 403"
        elif damage == "other_workflow":
            stored.workflow = "unknown_workflow"
        elif damage == "no_snapshot":
            for event in session.scalars(select(RunEvent).where(RunEvent.event_type == "inventory_gpsr_scan_complete")):
                session.delete(event)
        elif damage == "unsettled":
            from ziniao_automation.workflows.types import utc_now
            row = session.scalar(select(InventoryGpsrItem).where(InventoryGpsrItem.state == ELIGIBLE))
            row.state, row.intent_at = UNCERTAIN, utc_now()
        else:
            row = session.scalar(select(InventoryGpsrItem).where(InventoryGpsrItem.state == FAILED))
            row.reason = "item_load_timeout"
        session.commit()
        assert not can_resume_inventory_run(session, stored)
    with pytest.raises(RuntimeError):
        DurableRunQueue(harness.factory).resume_inventory_run(run.id)
    assert await harness.repo.get_run_status(run.id) is RunStatus.FAILED


async def test_paused_inventory_blocks_only_duplicate_workflow_creation(harness):
    adapter = Adapter(items(1))
    adapter.results["sku-0"] = unavailable()
    run = harness.new_run()
    _, engine, registry = _real_runtime(harness, adapter)
    await start(harness, engine, run)
    with harness.factory() as session:
        repo = WorkflowRepository(session, workflow_registry=registry)
        with pytest.raises(ConflictError, match="库存"):
            repo.create_run(store_id=int(run.store.id), workflow=run.workflow, mode="auto",
                            workflow_config={"marketplace_codes": ["DE"]})


async def test_crash_unwinding_pause_has_atomic_run_site_and_event(harness):
    class ProcessDied(BaseException):
        pass

    adapter = Adapter(items(3))
    adapter.results["sku-1"] = unavailable()
    run = harness.new_run(mode=RunMode.DRY_RUN)
    service, engine, _ = _real_runtime(harness, adapter)
    flow = engine.registry.get(run.workflow)
    original_summary = flow._summary_event

    async def crash_summary(current, marketplace):
        if await harness.repo.get_site_status(run.id, "DE") is SiteStatus.PAUSED:
            raise ProcessDied()
        await original_summary(current, marketplace)

    flow._summary_event = crash_summary
    await harness.repo.set_run_status(run.id, RunStatus.QUEUED)
    service._queue.enqueue(run.id)
    service._queue.claim_next()
    with pytest.raises(ProcessDied):
        await engine.start(run)
    with harness.factory() as session:
        assert session.get(Run, run.id).status == "PAUSED"
        assert session.scalar(select(SiteRun.status).where(SiteRun.run_id == run.id)) == "PAUSED"
        pause = session.scalar(select(RunEvent).where(RunEvent.event_type == "inventory_gpsr_paused"))
        assert pause.to_status == "PAUSED" and pause.details["http_status"] == 403
    restored, _, _ = _real_runtime(harness, adapter)
    quiet_worker(restored)
    await restored.recover_startup()
    assert restored._queue.claim_next() is None
    assert await harness.repo.get_run_status(run.id) is RunStatus.PAUSED


@pytest.mark.parametrize("change", ["disabled", "unconfirmed", "seller", "selector"])
async def test_resume_checks_current_store_and_saved_attempt_identity(harness, change):
    adapter = Adapter(items(1))
    adapter.results["sku-0"] = unavailable()
    run = harness.new_run()
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    await start(harness, engine, run)
    with harness.factory() as session:
        store = session.get(Store, int(run.store.id))
        if change == "disabled":
            store.enabled = False
        elif change == "unconfirmed":
            store.identity_confirmed = False
        elif change == "seller":
            store.expected_seller_id = "changed-seller"
        else:
            store.selector_value = "changed-profile"
        session.commit()
    with pytest.raises(RuntimeError):
        await service.resume_inventory_run(run.id)
    assert await harness.repo.get_run_status(run.id) is RunStatus.PAUSED
    assert service._queue.claim_next() is None


async def test_no_receipt_after_resuming_uncertain_item_stops_remaining(harness):
    adapter = Adapter(items(3))
    adapter.on_confirm = lambda item: _raise_unavailable()
    run = harness.new_run()
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    await start(harness, engine, run)
    adapter.on_confirm = None
    adapter.known_saved.clear()  # Readback has no matching platform receipt.
    await service.resume_inventory_run(run.id)
    from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import InventoryResultUncertain
    with pytest.raises(InventoryResultUncertain):
        await engine.start(run)
    assert adapter.saved == ["sku-2"]
    assert adapter.inspected == ["sku-2"]
    assert (await harness.ledger.summary(run.id))["pending"] == 2


async def test_legacy_resume_executes_remaining_without_rescan(harness):
    adapter, run, _ = await legacy_failure(harness)
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    await service.resume_inventory_run(run.id)
    assert (await engine.start(run)).status is RunStatus.SUCCEEDED
    assert adapter.inspected == ["sku-1", "sku-0"]
    assert adapter.scans == 0 and not adapter.saved


async def test_auth_resume_within_attempt_does_not_reset_http_latch(harness):
    from ziniao_automation.workflows.errors import HumanAuthRequired

    adapter = Adapter(items(3))
    adapter.begin_attempt = Mock()
    run = harness.new_run()
    _, engine, _ = _real_runtime(harness, adapter)
    first = True

    async def require_auth(item):
        nonlocal first
        if item.sku == "sku-1" and first:
            first = False
            raise HumanAuthRequired("fixture signin", kind="signin")

    async def wait(handle, *, auth_key, timeout_seconds, on_waiting):
        await on_waiting()

    adapter.on_inspect = require_auth
    engine.browser_sessions.wait_for_auth = wait
    assert (await start(harness, engine, run)).status is RunStatus.SUCCEEDED
    adapter.begin_attempt.assert_called_once_with(run.id)
    assert adapter.scans == 1 and adapter.saved == ["sku-2", "sku-1", "sku-0"]


async def test_store_removal_cancels_pause_but_keeps_submission_ledger(harness, monkeypatch):
    from ziniao_automation.repositories import StoreRepository

    adapter = Adapter(items(2))
    adapter.results["sku-0"] = unavailable()
    run = harness.new_run()
    _, engine, _ = _real_runtime(harness, adapter)
    await start(harness, engine, run)
    before = await harness.ledger.list_items(run.id)
    with harness.factory() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        repo = StoreRepository(session)
        monkeypatch.setattr(repo, "missing_profile_ids", lambda profiles, account_id: [int(run.store.id)])
        result = repo.delete_missing_profiles([], account_id=1)
        session.commit()
        assert result["cancelled_runs"] == 1
        assert session.get(Run, run.id).status == "CANCELLED"
        assert session.scalar(select(SiteRun.status).where(SiteRun.run_id == run.id)) == "CANCELLED"
    assert await harness.ledger.list_items(run.id) == before


@pytest.mark.parametrize("change_identity", [False, True])
async def test_legacy_without_original_identity_baseline_never_resumes(harness, change_identity):
    adapter, run, _ = await legacy_failure(harness, baseline=None)
    service, _, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    before = await harness.ledger.list_items(run.id)
    with harness.factory() as session:
        store = session.get(Store, int(run.store.id))
        if change_identity:
            store.expected_seller_id, store.selector_value = "new-seller", "new-profile"
            session.commit()
        assert not can_resume_inventory_run(session, session.get(Run, run.id))
    with pytest.raises(RuntimeError):
        await service.resume_inventory_run(run.id)
    assert await harness.repo.get_run_status(run.id) is RunStatus.FAILED
    assert await harness.ledger.list_items(run.id) == before
    assert service._queue.claim_next() is None


@pytest.mark.parametrize("damage", [None, "empty", "wrong_run", "wrong_store", "wrong_site", "missing_seller", "changed_seller"])
async def test_legacy_original_plan_seller_baseline_is_required_and_preserved(harness, damage):
    from copy import deepcopy

    adapter, run, _ = await legacy_failure(harness, baseline="plan", mode=RunMode.AUTO)
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    with harness.factory() as session:
        stored = session.get(Run, run.id)
        summary = deepcopy(stored.result_summary)
        plan = summary["workflow_plan_v1"]
        if damage == "empty":
            plan["lines"] = []
        elif damage == "wrong_run":
            plan["run_id"] = "unrelated-run"
        elif damage == "wrong_store":
            plan["store_id"] = "unrelated-store"
        elif damage == "wrong_site":
            plan["lines"][0]["snapshot"]["domain"] = "sellercentral.amazon.com"
        elif damage == "missing_seller":
            plan["lines"][0]["snapshot"].pop("seller_id")
        elif damage == "changed_seller":
            session.get(Store, int(run.store.id)).expected_seller_id = "new-seller"
        stored.result_summary = summary
        session.commit()
        assert can_resume_inventory_run(session, stored) is (damage is None)
    if damage is not None:
        with pytest.raises(RuntimeError):
            await service.resume_inventory_run(run.id)
        assert await harness.repo.get_run_status(run.id) is RunStatus.FAILED
        assert service._queue.claim_next() is None
    else:
        await service.resume_inventory_run(run.id)
        assert (await engine.start(run)).status is RunStatus.SUCCEEDED
        assert adapter.scans == 0
        assert adapter.saved == ["sku-2", "sku-1", "sku-0"]
