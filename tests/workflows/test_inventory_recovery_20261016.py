"""Regression fixtures for confirmed-history absence and bounded recovery; no live site."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from .test_inventory_gpsr_workflow import harness, Adapter, items, execute
from .test_inventory_compliance import dual_run, add_site
from ziniao_automation.workflows.amazon_inventory_gpsr.config import Inspection, RESPONSIBLE_PERSON
from ziniao_automation.workflows.amazon_inventory_gpsr.store import InventorySubmissionBlocked
from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import InventoryResultUncertain


class HistoryAdapter(Adapter):
    result = Inspection("eligible")
    async def inspect_item(self, page, run, marketplace, item):
        if item.requirement_type == RESPONSIBLE_PERSON:
            return self.result
        return Inspection("eligible")
    async def reconcile_item(self, page, run, marketplace, item):
        self.reconciled.append(item.requirement_type)
        return item.requirement_type != RESPONSIBLE_PERSON


@pytest.mark.parametrize("status", ["not_applicable", "no_violation_entry"])
async def test_confirmed_requirement_absent_preserves_history_and_reaches_later_site(harness, status):
    adapter = HistoryAdapter(items(1))
    previous = dual_run(harness, run_id="previous")
    await execute(harness, adapter, previous)
    before = await harness.ledger.list_items(previous.id)
    adapter.result = Inspection(status, requirements_complete=True)
    current = add_site(harness, dual_run(harness, run_id="current"), "IT")
    await execute(harness, adapter, current)
    rows = await harness.ledger.list_items(current.id, "DE")
    person = next(row for row in rows if row.item.requirement_type == RESPONSIBLE_PERSON)
    assert person.state == status.upper()
    assert person.reason == status
    assert person.intent_at is None
    assert await harness.ledger.list_items(previous.id) == before
    assert adapter.saved == ["sku-0", "sku-0", "sku-0"]  # original two + IT safety only
    assert all(row.state != "PENDING" for row in await harness.ledger.list_items(current.id, "IT"))


@pytest.mark.parametrize("status,complete", [
    ("not_applicable", False), ("eligible", True), ("no_records", True),
    ("invalid_record", True), ("no_violation_entry", False),
])
async def test_absence_without_complete_check_or_reopened_requirement_still_blocks(harness, status, complete):
    adapter = HistoryAdapter(items(1))
    await execute(harness, adapter, dual_run(harness, run_id="previous"))
    adapter.result = Inspection(status, requirements_complete=complete)
    current = dual_run(harness, run_id="current")
    await execute(harness, adapter, current)
    row = next(r for r in await harness.ledger.list_items(current.id) if r.item.requirement_type == RESPONSIBLE_PERSON)
    assert row.state == ("BLOCKED" if status == "eligible" else status.upper())
    assert row.intent_at is None
    assert len(adapter.saved) == 2


async def test_unsettled_intent_is_never_resolved_by_absence(harness):
    adapter = HistoryAdapter(items(1))
    previous = dual_run(harness, run_id="previous")
    flow = harness.workflow(adapter)
    await flow.plan(previous, object())
    row = next(r for r in await harness.ledger.list_items(previous.id) if r.item.requirement_type == RESPONSIBLE_PERSON)
    await harness.ledger.mark(row.id, "ELIGIBLE", eligible=True)
    await harness.ledger.arm(row.id)
    adapter.result = Inspection("not_applicable", requirements_complete=True)
    await execute(harness, adapter, dual_run(harness, run_id="current"))
    assert (await harness.ledger.get(row.id)).state == "SUBMITTING"
    assert adapter.saved == ["sku-0"]  # unrelated safety requirement proceeds
    assert not adapter.reconciled


from .test_inventory_gpsr_page import ScanAdapter, rows, Page, RUN, DE
from .test_inventory_gpsr_workflow import _real_runtime
from .test_inventory_pause_resume import start, quiet_worker
from ziniao_automation.workflows.amazon_inventory_gpsr.config import InventoryGpsrPolicy
from ziniao_automation.workflows.amazon_inventory_gpsr.page import ItemPanelLoadError, InventoryContractError
from ziniao_automation.workflows.amazon_inventory_gpsr.progress import ReadProgress
from ziniao_automation.workflows.errors import PageRecoveryRequired
from ziniao_automation.workflows.types import RunMode, RunStatus


async def test_109_to_108_restarts_once_without_merging_or_navigation():
    class Shrink(ScanAdapter):
        changed = False
        async def _turn_page(self, page, current, target):
            if target == 2 and not self.changed:
                self.rows.pop(0)  # Earlier page shifts; old rows MUST be discarded.
                self.changed = True
            return await super()._turn_page(page, current, target)
    adapter = Shrink(rows(109))
    events = []
    async def event(run, market, action, details): events.append((action, details))
    adapter.on_scan_event = event
    result = await adapter.scan_candidates(Page(), RUN, DE)
    assert len(result) == 108
    assert [i.sku for i in result] == [sku for sku, asin in adapter.rows]
    assert adapter.turns == [1, 2, 1, 2]
    assert events == [("restart", dict(expected_total=109, actual_total=108, page=2, size=100, restart=1))]


async def test_persistent_drift_pauses_without_partial_snapshot():
    adapter = ScanAdapter(rows(109), drift=2)
    events = []
    async def event(run, market, action, details): events.append(action)
    adapter.on_scan_event = event
    with pytest.raises(PageRecoveryRequired) as error:
        await adapter.scan_candidates(Page(), RUN, DE)
    assert error.value.reason_code == "inventory_scan_unstable"
    assert events == ["restart", "exhausted"]
    assert adapter.turns == [1, 2, 1, 2]


async def test_unknown_page_shape_is_not_retried():
    class WrongSize(ScanAdapter):
        async def _wait_list(self, page):
            current = await super()._wait_list(page)
            return replace(current, items_per_page=50) if self.number == 2 else current
    adapter = WrongSize(rows(109))
    with pytest.raises(InventoryContractError):
        await adapter.scan_candidates(Page(), RUN, DE)
    assert adapter.turns == [1, 2]


@pytest.mark.parametrize("mode", [RunMode.AUTO, RunMode.DRY_RUN])
async def test_six_consecutive_failures_pause_without_consuming_remaining(harness, mode):
    adapter = Adapter(items(13))
    adapter.results = {item.sku: ItemPanelLoadError("fixture") for item in adapter.items}
    run = harness.new_run(mode=mode)
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    assert (await start(harness, engine, run)).status == RunStatus.PAUSED
    records = await harness.ledger.list_items(run.id)
    assert len(adapter.inspected) == 6
    assert sum(r.state == "FAILED" for r in records) == 6
    assert sum(r.state == "PENDING" for r in records) == 7
    assert all(r.intent_at is None for r in records)
    adapter.results.clear()
    await service.resume_inventory_run(run.id)
    await engine.start(run)
    assert adapter.scans == 1
    assert adapter.inspected[6:] == [f"sku-{i}" for i in reversed(range(7))]
    assert not adapter.saved if mode == RunMode.DRY_RUN else len(adapter.saved) == 7


async def test_successful_skip_resets_failure_budget(harness):
    adapter = Adapter(items(13))
    adapter.results = {item.sku: ItemPanelLoadError("fixture") for item in adapter.items}
    for sku in ("sku-8", "sku-3"):
        adapter.results[sku] = "no_attestation"
    run = harness.new_run(mode=RunMode.DRY_RUN)
    await execute(harness, adapter, run)
    assert len(adapter.inspected) == 13
    assert not adapter.saved
    assert (await harness.ledger.summary(run.id))["no_attestation"] == 2


def test_no_progress_deadline_is_bounded_and_real_reads_reset_it(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("ziniao_automation.workflows.amazon_inventory_gpsr.progress.time.monotonic", lambda: now[0])
    progress = ReadProgress(InventoryGpsrPolicy(no_progress_timeout_seconds=600))
    now[0] = 599
    progress.check()
    progress.completed()
    now[0] = 1199
    with pytest.raises(PageRecoveryRequired) as error: progress.check()
    assert error.value.reason_code == "inventory_no_progress"


async def test_single_residual_search_timeout_is_failed_not_missing_and_next_item_runs(harness):
    from ziniao_automation.workflows.amazon_inventory_gpsr.page import ItemSearchLoadError
    class SearchTimeout(Adapter):
        async def locate_item(self, page, item, *, search=False):
            if not search:
                return False
            if item.sku == "sku-2":
                raise ItemSearchLoadError("fixture search still stale")
            return True
    adapter = SearchTimeout(items(3))
    run = harness.new_run()
    await execute(harness, adapter, run)
    rows = await harness.ledger.list_items(run.id)
    assert rows[0].state == "FAILED" and rows[0].reason == "item_search_load_timeout"
    assert rows[0].intent_at is None
    assert adapter.saved == ["sku-1", "sku-0"]


async def test_repeated_scan_instability_preserves_other_site_snapshot_and_resumes(harness):
    class Unstable(Adapter):
        unstable = True
        async def scan_candidates(self, page, run, market):
            if market.code == "IT" and self.unstable:
                raise PageRecoveryRequired("fixture", reason_code="inventory_scan_unstable")
            return await super().scan_candidates(page, run, market)
    adapter = Unstable(items(1))
    run = add_site(harness, dual_run(harness), "IT")
    service, engine, _ = _real_runtime(harness, adapter)
    quiet_worker(service)
    assert (await start(harness, engine, run)).status == RunStatus.PAUSED
    assert await harness.ledger.snapshot_exists(run.id, "DE")
    assert not await harness.ledger.snapshot_exists(run.id, "IT")
    assert adapter.saved == []
    adapter.unstable = False
    await service.resume_inventory_run(run.id)
    await engine.start(run)
    assert adapter.scans == 2  # original DE and later IT; DE not rescanned
    assert len(adapter.saved) == 4
