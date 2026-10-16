"""Multi-market completion and explicit stop diagnostics, using local SQLite only."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from .test_inventory_gpsr_workflow import harness, items, execute
from .test_inventory_compliance import dual_run, add_site, DualAdapter
from ziniao_automation.models import Run, RunEvent, SiteRun
from ziniao_automation.workflows.amazon_inventory_gpsr.page import (
    AmazonInventoryGpsrPage, InventoryContractError, ItemLoadError,
)
from ziniao_automation.workflows.types import RunMode, SiteStatus

CODES = ("ES", "FR", "IT", "PL")


def four_site_run(h, mode=RunMode.AUTO):
    run = dual_run(h, mode)
    for code in CODES:
        run = add_site(h, run, code)
    config = {**run.workflow_config, "marketplace_codes": list(CODES)}
    with h.factory() as session:
        session.get(Run, run.id).workflow_config = config
        session.commit()
    return replace(run, workflow_config=config, marketplaces=run.marketplaces[1:])


class MultiAdapter(DualAdapter):
    def __init__(self, failure=None):
        super().__init__()
        self.items = items(2)
        self.opened = []
        self.failure = failure

    async def open_inventory(self, page, run, market):
        self.opened.append(market.code)
        return page

    async def scan_candidates(self, page, run, market):
        return self.items

    async def inspect_item(self, page, run, market, item):
        if market.code == "ES" and item.sku == "sku-0" and item.requirement_type == "safety_attestation":
            if self.failure == "contract":
                # Keep real validation frame, but never persist raw unknown DOM text.
                AmazonInventoryGpsrPage._validate_form({"heading": "TOKEN_COOKIE_SECRET"})
            if self.failure == "load":
                raise ItemLoadError("TOKEN_COOKIE_SECRET")
            if self.failure == "runtime":
                raise RuntimeError("TOKEN_COOKIE_SECRET https://fixture/?signed=SECRET")
        return await super().inspect_item(page, run, market, item)


async def test_all_four_sites_execute_after_all_four_preflights(harness):
    run = four_site_run(harness)
    adapter = MultiAdapter()
    await execute(harness, adapter, run)
    assert adapter.opened == list(CODES) * 2
    assert [a[0] for a in adapter.actions] == [c for c in CODES for _ in range(4)]
    assert len(set(adapter.actions)) == 16
    for code in CODES:
        rows = await harness.ledger.list_items(run.id, code)
        assert len(rows) == 4 and all(r.state == "CONFIRMED" for r in rows)
        assert await harness.repo.get_site_status(run.id, code) == SiteStatus.CONFIRMED


async def test_marketplace_diagnostic_is_whitelisted_and_persisted(harness):
    run = four_site_run(harness)
    workflow = harness.workflow(MultiAdapter())
    await workflow._marketplace_event(run, run.marketplaces[1], 'reopened', {
        'state': 'closed', 'target_count': 0, 'item_count': 0, 'reopens': 1,
        'cookie': 'SECRET', 'seller_id': 'SECRET', 'url': 'https://private.invalid/?token=SECRET',
    })
    with harness.factory() as session:
        event = session.scalar(select(RunEvent).where(RunEvent.run_id == run.id,
                                                    RunEvent.event_type == 'inventory_marketplace_reopened'))
        assert event.details == {'state': 'closed', 'target_count': 0, 'item_count': 0, 'reopens': 1}
        assert 'SECRET' not in json.dumps(event.details)


async def test_single_read_failure_continues_other_requirement_and_later_sites(harness):
    run = four_site_run(harness)
    adapter = MultiAdapter("load")
    await execute(harness, adapter, run)
    assert adapter.opened == list(CODES) * 2
    assert len(adapter.actions) == 15
    rows = await harness.ledger.list_items(run.id)
    failed = [r for r in rows if r.state == "FAILED"]
    assert len(failed) == 1 and failed[0].intent_at is None
    assert await harness.repo.get_site_status(run.id, "ES") == SiteStatus.FAILED
    for code in CODES[1:]:
        assert await harness.repo.get_site_status(run.id, code) == SiteStatus.CONFIRMED


@pytest.mark.parametrize("phase", ["plan", "execute"])
@pytest.mark.parametrize("failure", ["contract", "runtime"])
async def test_fatal_stop_preserves_results_and_records_sanitized_diagnostic(harness, phase, failure):
    run = four_site_run(harness, RunMode.DRY_RUN if phase == "plan" else RunMode.AUTO)
    adapter = MultiAdapter(failure)
    if phase == "plan":
        with pytest.raises(InventoryContractError if failure == "contract" else RuntimeError):
            await execute(harness, adapter, run)
        assert not adapter.actions
    else:
        # The first product succeeded before the last product's contract failed.
        await execute(harness, adapter, run)
        assert len(adapter.actions) == 2 and all(a[0] == "ES" for a in adapter.actions)
        for code in CODES[1:]:
            rows = await harness.ledger.list_items(run.id, code)
            assert len(rows) == 4 and all(r.state == "PENDING" and r.intent_at is None for r in rows)
    with harness.factory() as session:
        events = list(session.scalars(select(RunEvent).where(
            RunEvent.run_id == run.id, RunEvent.event_type == "inventory_gpsr_stopped")))
        assert len(events) == 1
        event = events[0]
        assert event.details["phase"] == phase
        assert event.details["remaining_marketplaces"] == ["FR", "IT", "PL"]
        assert event.details["exception_frames"]
        if failure == "contract":
            assert event.details["stage"] == "attestation_form"
            assert "安全证明表单核验" in event.message
        assert "已停止后续提交" in event.message
        assert "FR、IT、PL" in event.message
        assert "TOKEN_COOKIE_SECRET" not in json.dumps(event.details)
        assert "TOKEN_COOKIE_SECRET" not in event.message
        site = session.scalar(select(SiteRun).where(SiteRun.run_id == run.id, SiteRun.marketplace_code == "ES"))
        assert site.error == event.message


@pytest.mark.parametrize("historical_state,selected,disabled,expected", [
    ("CONFIRMED", True, True, "ALREADY_SUBMITTED"),
    ("CONFIRMED", False, True, "BLOCKED"),
    ("CONFIRMED", True, False, "BLOCKED"),
    ("UNCERTAIN", True, True, "BLOCKED"),
])
async def test_only_confirmed_history_can_accept_retained_checked_declaration(
    harness, historical_state, selected, disabled, expected,
):
    from types import SimpleNamespace
    from ziniao_automation.workflows.amazon_inventory_gpsr.store import InventorySubmissionBlocked
    old = harness.new_run("old-attestation")
    old_flow = harness.workflow(MultiAdapter())
    await old_flow.plan(old, object())
    previous = (await harness.ledger.list_items(old.id))[0]
    await harness.ledger.mark(previous.id, "ELIGIBLE", eligible=True)
    await harness.ledger.arm(previous.id)
    await harness.ledger.mark(previous.id, historical_state, reason="fixture_prior_result")
    previous = await harness.ledger.get(previous.id)
    new = harness.new_run("new-attestation")
    await harness.repo.set_site_status(new.id, "DE", SiteStatus.PREFLIGHT)
    await harness.ledger.save_snapshot(run_id=new.id, store_id=int(new.store.id),
                                      marketplace_code="DE", items=[previous.item])
    current = (await harness.ledger.list_items(new.id))[0]
    class Retained(MultiAdapter):
        async def confirmed_attestation_retained(self, *args):
            return selected and disabled
        async def reconcile_item(self, *args):
            return False  # A checked form is not a review receipt.
    adapter = Retained()
    flow = harness.workflow(adapter)
    await flow._inspect_and_act(new, object(), new.marketplaces[0], current, submit=True)
    assert (await harness.ledger.get(current.id)).state == expected
    assert (await harness.ledger.get(current.id)).intent_at is None
    assert (await harness.ledger.get(previous.id)).state == historical_state
    assert adapter.actions == []
