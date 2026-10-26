"""Entry failures are finite item-local outcomes, not page/identity failures."""
import pytest

from .test_inventory_gpsr_workflow import harness, items, execute
from .test_inventory_multisite_completion import four_site_run, MultiAdapter
from ziniao_automation.workflows.amazon_inventory_gpsr.page import ItemEntryError
from ziniao_automation.workflows.types import RunMode, SiteStatus


@pytest.mark.parametrize("residual", [False, True])
@pytest.mark.parametrize("ambiguous", [False, True])
@pytest.mark.parametrize("mode", [RunMode.DRY_RUN, RunMode.AUTO])
async def test_many_entry_failures_remain_reported_but_do_not_block_following_sites(
    harness, residual, ambiguous, mode,
):
    class UnsupportedEntries(MultiAdapter):
        def __init__(self):
            super().__init__()
            self.items = items(4)
            self.inspections = []
        async def locate_item(self, page, item, *, search=False):
            return search or not residual
        async def inspect_item(self, page, run, market, item):
            self.inspections.append((market.code, item.sku, item.requirement_type))
            if market.code == "FR":
                raise ItemEntryError(ambiguous=ambiguous)
            return await super().inspect_item(page, run, market, item)

    run = four_site_run(harness, mode)
    adapter = UnsupportedEntries()
    await execute(harness, adapter, run)
    records = await harness.ledger.list_items(run.id)
    assert len(adapter.inspections) == len(records) == 32
    assert not any(r.state in {"PENDING", "SUBMITTING", "UNCERTAIN"} for r in records)
    failed = [r for r in records if r.state == "FAILED"]
    assert len(failed) == 8 and all(r.intent_at is None for r in failed)
    assert {r.reason for r in failed} == {
        "item_entry_ambiguous" if ambiguous else "item_entry_unrecognized"}
    assert await harness.repo.get_site_status(run.id, "FR") is SiteStatus.FAILED
    for code in ("IT", "PL"):
        assert await harness.repo.get_site_status(run.id, code) is (
            SiteStatus.CONFIRMED if mode is RunMode.AUTO else SiteStatus.DRY_RUN_COMPLETE)
    assert not any(a[0] == "FR" for a in adapter.actions)
