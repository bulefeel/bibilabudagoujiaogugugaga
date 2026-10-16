"""Current candidates first; historical facts remain immutable (offline SQLite)."""
from dataclasses import replace
import pytest
from sqlalchemy import select
from tests.workflows.test_inventory_gpsr_workflow import harness, Adapter, items, execute
from tests.workflows.test_inventory_compliance import add_site
from ziniao_automation.models import Run, RunEvent, InventoryGpsrRequirement
from ziniao_automation.notifications.database import read_inventory_gpsr_counts
from ziniao_automation.workflows.types import RunMode, RunStatus
from ziniao_automation.workflows.amazon_inventory_gpsr.store import (
    ELIGIBLE, SUBMITTING, UNCERTAIN, CONFIRMED, InventorySubmissionBlocked,
)
from ziniao_automation.workflows.amazon_inventory_gpsr.page import ItemLoadError

class CurrentAdapter(Adapter):
    async def inspect_item(self, *args):
        result = await super().inspect_item(*args)
        result.initial_selected = False if result.status == "eligible" else None
        return result


def typed(h, run, kind):
    config = {**run.workflow_config, "processing_types": [kind]}
    with h.factory() as db:
        db.get(Run, run.id).workflow_config = config
        db.commit()
    return replace(run, workflow_config=config)


async def previous(h, kind="safety_attestation", state=UNCERTAIN, candidate=None):
    run = typed(h, h.new_run("old"), kind)
    await h.workflow(Adapter((candidate or items(1)[0],))).plan(run, object())
    row = (await h.ledger.list_items(run.id))[0]
    await h.ledger.mark(row.id, ELIGIBLE, eligible=True)
    await h.ledger.arm(row.id)
    if state != SUBMITTING:
        await h.ledger.mark(row.id, state, reason="original_fact")
    await h.repo.set_run_status(run.id, RunStatus.CANCELLED)
    return row


def original_fact(h, row):
    with h.factory() as db:
        obj = db.get(InventoryGpsrRequirement, row.id)
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


@pytest.mark.parametrize("kind", ["safety_attestation", "responsible_person", "manufacturer"])
@pytest.mark.parametrize("state", [SUBMITTING, UNCERTAIN])
async def test_current_unprocessed_retries_once_preserving_history(harness, kind, state):
    old = await previous(harness, kind, state)
    before = original_fact(harness, old)
    run = typed(harness, harness.new_run("new"), kind)
    adapter = CurrentAdapter(items(2))
    flow, _ = await execute(harness, adapter, run)
    assert adapter.saved == ["sku-1", "sku-0"]
    assert not adapter.reconciled
    assert original_fact(harness, old) == before
    report = await flow.report(run, RunStatus.SUCCEEDED)
    assert report.fields["uncertain"] == 0 and report.fields["confirmed"] == 2
    with harness.factory() as db:
        event = db.scalar(select(RunEvent).where(RunEvent.run_id == run.id,
            RunEvent.event_type == "inventory_gpsr_current_unprocessed_retry"))
        assert event.details["previous_intents"] == [{"id": old.id, "state": state}]
        assert event.details["initial_selected"] is False
        assert read_inventory_gpsr_counts(db, run.id).uncertain == 0
    await execute(harness, adapter, run)
    assert adapter.saved == ["sku-1", "sku-0"]


async def test_missing_historical_item_never_looked_up_or_counted(harness):
    old = await previous(harness)
    before = original_fact(harness, old)
    run = harness.new_run("new")
    adapter = CurrentAdapter(items(2)[1:])
    flow, _ = await execute(harness, adapter, run)
    assert adapter.inspected == adapter.saved == ["sku-1"]
    assert adapter.located == [("sku-1", False)] and not adapter.reconciled
    assert (await flow.report(run, RunStatus.SUCCEEDED)).fields["uncertain"] == 0
    assert original_fact(harness, old) == before


@pytest.mark.parametrize("outcome", ["already_linked", "already_submitted", "not_applicable", "no_records"])
async def test_current_processed_or_nonactionable_does_not_rewrite_old_attempt(harness, outcome):
    old = await previous(harness, "manufacturer")
    before = original_fact(harness, old)
    run = typed(harness, harness.new_run("new"), "manufacturer")
    adapter = CurrentAdapter(items(2)); adapter.results["sku-0"] = outcome
    await execute(harness, adapter, run)
    assert adapter.saved == ["sku-1"] and not adapter.reconciled
    assert original_fact(harness, old) == before


@pytest.mark.parametrize("initial", [None, True])
async def test_no_explicit_unprocessed_proof_blocks_only_this_item(harness, initial):
    old = await previous(harness)
    class Ambiguous(CurrentAdapter):
        async def inspect_item(self, *args):
            out = await super().inspect_item(*args)
            out.initial_selected = initial
            return out
    adapter = Ambiguous(items(2)); run = harness.new_run("new")
    await execute(harness, adapter, run)
    assert adapter.saved == ["sku-1"]
    assert (await harness.ledger.summary(run.id))["blocked"] == 1
    assert (await harness.ledger.get(old.id)).state == UNCERTAIN


async def test_read_timeout_not_unprocessed_and_other_items_continue(harness):
    await previous(harness, candidate=items(2)[1])
    adapter = CurrentAdapter(items(2))
    adapter.results["sku-1"] = ItemLoadError("fixture unreadable")
    await execute(harness, adapter, harness.new_run("new"))
    assert adapter.saved == ["sku-0"] and not adapter.reconciled


async def test_dry_run_never_creates_retry_or_changes_history(harness):
    old = await previous(harness)
    before = original_fact(harness, old)
    adapter = CurrentAdapter(items(1)); run = harness.new_run("new", RunMode.DRY_RUN)
    await execute(harness, adapter, run)
    assert not adapter.saved and not adapter.reconciled
    assert original_fact(harness, old) == before
    assert (await harness.ledger.list_items(run.id))[0].intent_at is None


async def test_confirmed_history_conflict_does_not_replay_or_block_other_sites(harness):
    old = await previous(harness, state=CONFIRMED)
    run = add_site(harness, harness.new_run("new"), "IT")
    adapter = CurrentAdapter(items(1))
    await execute(harness, adapter, run)
    assert adapter.saved == ["sku-0"]  # IT only; DE is a known-confirmed conflict
    assert (await harness.ledger.list_items(run.id, "DE"))[0].state == "BLOCKED"
    assert (await harness.ledger.list_items(run.id, "IT"))[0].state == CONFIRMED


async def test_new_history_after_page_inspection_is_not_overridden(harness):
    old = await previous(harness)
    run = harness.new_run("new")
    adapter = CurrentAdapter(items(2))
    async def change_history(page, run, market, item):
        if item.sku == "sku-0":
            await harness.ledger.mark(old.id, CONFIRMED, reason="later_receipt")
    adapter.prepare_submission = change_history
    await execute(harness, adapter, run)
    assert adapter.saved == ["sku-1"]
    assert (await harness.ledger.summary(run.id))["blocked"] == 1


async def test_live_old_attempt_does_not_get_concurrent_retry(harness):
    await previous(harness)
    with harness.factory() as db:
        db.get(Run, "old").status = "RUNNING"
        db.commit()
    adapter = CurrentAdapter(items(2))
    await execute(harness, adapter, harness.new_run("new"))
    assert adapter.saved == ["sku-1"]


async def test_atomic_arm_rejects_same_proof_after_competing_attempt(harness):
    await previous(harness)
    rows = []
    for name in ("new-a", "new-b"):
        run = harness.new_run(name)
        await harness.workflow(Adapter(items(1))).plan(run, object())
        row = (await harness.ledger.list_items(run.id))[0]
        await harness.ledger.mark(row.id, ELIGIBLE, eligible=True, initial_selected=False)
        rows.append(row)
    first, second = rows
    proof_a = await harness.ledger.history_revisions(first)
    proof_b = await harness.ledger.history_revisions(second)
    await harness.ledger.arm(first.id, rechecked_history=proof_a)
    with pytest.raises(InventorySubmissionBlocked):
        await harness.ledger.arm(second.id, rechecked_history=proof_b)
    with pytest.raises(InventorySubmissionBlocked):
        await harness.ledger.arm(first.id, rechecked_history=proof_a)


async def test_plain_arm_still_rejects_unexamined_history(harness):
    await previous(harness)
    run = harness.new_run("new")
    await harness.workflow(Adapter(items(1))).plan(run, object())
    row = (await harness.ledger.list_items(run.id))[0]
    await harness.ledger.mark(row.id, ELIGIBLE, eligible=True, initial_selected=False)
    with pytest.raises(InventorySubmissionBlocked):
        await harness.ledger.arm(row.id)


@pytest.mark.parametrize("change", ["asin", "requirement"])
async def test_old_intent_does_not_cross_product_or_requirement_identity(harness, change):
    old = await previous(harness, "manufacturer")
    before = original_fact(harness, old)
    run = typed(harness, harness.new_run("new"), "responsible_person" if change == "requirement" else "manufacturer")
    item = replace(items(1)[0], asin="B999999999") if change == "asin" else items(1)[0]
    adapter = CurrentAdapter((item,))
    await execute(harness, adapter, run)
    assert adapter.saved == ["sku-0"] and not adapter.reconciled
    assert original_fact(harness, old) == before
    with harness.factory() as db:
        assert db.scalar(select(RunEvent).where(RunEvent.run_id == run.id,
            RunEvent.event_type == "inventory_gpsr_current_unprocessed_retry")) is None


async def test_empty_current_list_does_not_report_old_unresolved(harness):
    old = await previous(harness)
    adapter = CurrentAdapter(())
    run = harness.new_run("new")
    flow, _ = await execute(harness, adapter, run)
    report = await flow.report(run, RunStatus.SKIPPED)
    with harness.factory() as db:
        counts = read_inventory_gpsr_counts(db, run.id)
        assert counts.scanned == counts.uncertain == 0
    assert report.fields["scanned"] == report.fields["uncertain"] == 0
    assert not adapter.located and not adapter.reconciled and not adapter.saved
    assert (await harness.ledger.get(old.id)).state == UNCERTAIN
