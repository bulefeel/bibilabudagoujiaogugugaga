"""History/dispatch regressions using real SQLite, no marketplace requests."""
import pytest
from sqlalchemy import select
from tests.workflows.test_inventory_gpsr_workflow import harness, Adapter, items, execute
from ziniao_automation.models import RunEvent, InventoryGpsrRequirement
from ziniao_automation.workflows.types import RunStatus
from ziniao_automation.workflows.amazon_inventory_gpsr.store import (
    CONFIRMED, UNCERTAIN, ELIGIBLE, InventoryRunCancelled, InventorySubmissionBlocked,
)

@pytest.mark.parametrize("receipt", [True, False, "error"])
async def test_cancel_after_dispatch_finishes_readback_without_next_submit(harness, receipt):
    run = harness.new_run()
    adapter = Adapter(items(2))
    original = adapter.submit_once
    async def dispatch_then_cancel(*args):
        await original(*args)
        await harness.repo.set_run_status(run.id, RunStatus.CANCELLED)
    adapter.submit_once = dispatch_then_cancel
    reads = []
    async def read(*args):
        reads.append(True)
        if receipt == "error":
            raise TimeoutError("fixture")
        return receipt
    adapter.confirm_submission = read
    with pytest.raises(InventoryRunCancelled):
        await execute(harness, adapter, run)
    assert reads == [True]
    assert adapter.saved == ["sku-1"]
    row = (await harness.ledger.list_items(run.id))[0]
    assert row.state == (CONFIRMED if receipt is True else UNCERTAIN)
    with harness.factory() as db:
        assert db.get(InventoryGpsrRequirement, row.id).submitted_at is not None
    assert await harness.repo.get_run_status(run.id) is RunStatus.CANCELLED

async def test_old_unresolved_does_not_prevent_current_candidate_scan(harness):
    old = harness.new_run("old")
    await harness.workflow(Adapter(items(1))).plan(old, object())
    row = (await harness.ledger.list_items(old.id))[0]
    await harness.ledger.mark(row.id, ELIGIBLE, eligible=True)
    await harness.ledger.arm(row.id)
    await harness.repo.set_run_status(old.id, RunStatus.CANCELLED)
    run = harness.new_run("next")
    adapter = Adapter(items(2))
    await execute(harness, adapter, run)
    assert adapter.scans == 1 and adapter.saved == ["sku-1"]
    assert not adapter.reconciled
    with harness.factory() as db:
        events = db.scalars(select(RunEvent).where(RunEvent.run_id == run.id)).all()
        assert not any(e.event_type == "inventory_gpsr_history_checking" for e in events)
        summaries = [e for e in events if e.event_type == "inventory_gpsr_summary"]
        assert summaries[-1].details["scanned"] == 2
