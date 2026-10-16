"""UI-only 403 recovery against the real workflow and SQLite item ledger."""
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from ziniao_automation.models import RunEvent
from ziniao_automation.workflows.amazon_inventory_gpsr.config import InventoryGpsrPolicy, INVENTORY_URL
from ziniao_automation.workflows.amazon_inventory_gpsr.page import AmazonInventoryGpsrPage, ItemLoadError
from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import InventoryResultUncertain
from ziniao_automation.workflows.types import RunMode, RunStatus, SiteStatus
from .test_inventory_gpsr_workflow import Adapter, harness, items, execute


class RetryAdapter(Adapter, AmazonInventoryGpsrPage):
    inspect_item = AmazonInventoryGpsrPage.inspect_item

    def __init__(self, candidates, failures):
        Adapter.__init__(self, candidates)
        AmazonInventoryGpsrPage.__init__(self, policy=InventoryGpsrPolicy(http_retry_delay_seconds=0))
        self.failures = failures
        self.hit = 0

    async def _identity(self, page, run):
        pass

    async def _inspect_with_auth(self, page, run, marketplace, item):
        self._active_run = run
        await self._check_cancelled()
        if item.sku == "sku-8":
            self.hit += 1
            if self.hit <= self.failures:
                self.inspected.append(item.sku)
                self._network_failure = 403
                raise ItemLoadError("fixture sidebar blank")
        await self._accept_forbidden_dom(run, item)
        return await Adapter.inspect_item(self, page, run, marketplace, item)


@pytest.mark.parametrize("mode", [RunMode.DRY_RUN, RunMode.AUTO])
@pytest.mark.parametrize("failures", [1, 2, 3])
async def test_thirteen_items_fifth_403_retries_then_continues_all_remaining(harness, mode, failures):
    adapter = RetryAdapter(items(13), failures)
    run = harness.new_run(mode=mode)
    flow = harness.workflow(adapter)
    page = FixturePage()
    await flow.preflight(run, page)
    plan = await flow.plan(run, page)
    if plan.lines:
        await flow.execute(run, page, plan)
    counts = await harness.ledger.summary(run.id)
    assert counts["scanned"] == 13 and counts["pending"] == 0
    assert adapter.hit == min(failures + 1, 3)
    assert set(adapter.inspected) == {f"sku-{i}" for i in range(13)}
    assert counts["failed"] == (failures == 3)
    assert len(adapter.saved) == (13 - (failures == 3) if mode is RunMode.AUTO else 0)
    assert len(adapter.saved) == len(set(adapter.saved))
    with harness.factory() as session:
        events = session.scalars(select(RunEvent).where(RunEvent.event_type == "inventory_gpsr_http_retry")).all()
        assert [event.details["retry"] for event in events] == list(range(1, min(failures, 2) + 1))
    if failures == 3:
        record = next(r for r in await harness.ledger.list_items(run.id) if r.item.sku == "sku-8")
        assert record.reason == "item_http_403_retries_exhausted"
        assert await harness.repo.get_site_status(run.id, "DE") is SiteStatus.FAILED


async def test_ui_retry_never_replays_a_save_with_uncertain_result(harness):
    adapter = RetryAdapter(items(13), 0)
    run = harness.new_run()
    async def broken_save(item):
        adapter._network_failure = 403
        raise ItemLoadError("save return lost")
    adapter.on_save = broken_save
    with pytest.raises(InventoryResultUncertain, match="尚未确认"):
        flow, page = harness.workflow(adapter), FixturePage()
        await flow.preflight(run, page)
        plan = await flow.plan(run, page)
        await flow.execute(run, page, plan)
    rows = await harness.ledger.list_items(run.id)
    assert sum(r.state == "UNCERTAIN" for r in rows) == 1
    assert sum(r.state == "PENDING" for r in rows) == 12
    assert adapter.hit == 0
    assert not adapter.saved


@pytest.mark.parametrize("mode", [RunMode.DRY_RUN, RunMode.AUTO])
async def test_exhausted_item_is_reported_as_partial_after_remaining_items_finish(harness, mode):
    from ziniao_automation.workflows.engine import WorkflowEngine
    from ziniao_automation.workflows.registry import WorkflowRegistry
    from ziniao_automation.workflows.amazon_inventory_gpsr.definition import build_amazon_inventory_gpsr_definition

    class Sessions:
        @asynccontextmanager
        async def session(self, selector, store_key=None):
            yield SimpleNamespace(page=FixturePage())

    adapter = RetryAdapter(items(13), 3)
    run = harness.new_run(mode=mode)
    await harness.repo.set_run_status(run.id, RunStatus.QUEUED)
    flow = harness.workflow(adapter)
    registry = WorkflowRegistry((build_amazon_inventory_gpsr_definition(flow),))
    engine = WorkflowEngine(registry=registry, repository=harness.repo, browser_sessions=Sessions())
    result = await engine.start(run)
    assert result.status is RunStatus.PARTIAL
    report = await flow.report(run, result.status)
    assert report.fields["scanned"] == 13
    assert report.fields["failed"] == 1 and report.fields["pending"] == 0
    assert report.fields["confirmed"] == (12 if mode is RunMode.AUTO else 0)


@pytest.mark.parametrize("interruption", ["cancel", "rate_limit", "wrong_route"])
async def test_retry_wait_rechecks_cancellation_response_and_route(harness, monkeypatch, interruption):
    import ziniao_automation.workflows.amazon_inventory_gpsr.page as page_module
    from ziniao_automation.workflows.amazon_inventory_gpsr.store import InventoryRunCancelled
    from ziniao_automation.workflows.errors import PageRecoveryRequired
    from ziniao_automation.workflows.amazon_inventory_gpsr.page import InventoryContractError

    adapter = RetryAdapter(items(13), 3)
    adapter._timing = replace(adapter.timing, http_retry_delay_seconds=5)
    run = harness.new_run()
    harness.workflow(adapter)  # Wire durable cancellation and events.
    page = FixturePage()
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if interruption == "cancel":
            await harness.repo.set_run_status(run.id, RunStatus.CANCELLED)
        elif interruption == "rate_limit":
            adapter._network_failure = 429
        else:
            page.url = "https://sellercentral.amazon.co.uk/myinventory/inventory"

    monkeypatch.setattr(page_module.asyncio, "sleep", sleep)
    expected = {"cancel": InventoryRunCancelled, "rate_limit": PageRecoveryRequired,
                "wrong_route": InventoryContractError}[interruption]
    with pytest.raises(expected):
        await adapter.inspect_item(page, run, run.marketplaces[0], items(13)[8])
    assert delays == [5] and adapter.hit == 1
    assert not adapter.saved


@pytest.mark.parametrize("bad", [-1, 3, True, 1.5])
def test_retry_count_has_small_explicit_integer_budget(bad):
    with pytest.raises(ValueError):
        InventoryGpsrPolicy(http_retry_attempts=bad)


async def test_retry_budget_covers_followup_timeout_without_new_403(harness, monkeypatch):
    import ziniao_automation.workflows.amazon_inventory_gpsr.page as page_module
    from ziniao_automation.workflows.amazon_inventory_gpsr.page import InventoryItemHttpError

    class FirstResponseOnly(RetryAdapter):
        async def _inspect_with_auth(self, page, run, marketplace, item):
            try:
                return await super()._inspect_with_auth(page, run, marketplace, item)
            except ItemLoadError:
                if self.hit > 1:
                    self._network_failure = None
                raise

    adapter = FirstResponseOnly(items(13), 3)
    adapter._timing = replace(adapter.timing, http_retry_delay_seconds=5)
    run = harness.new_run()
    harness.workflow(adapter)
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr(page_module.asyncio, "sleep", sleep)
    with pytest.raises(ItemLoadError) as error:
        await adapter.inspect_item(FixturePage(), run, run.marketplaces[0], items(13)[8])
    assert not isinstance(error.value, InventoryItemHttpError)
    assert error.value.reason_code == "item_load_timeout"
    assert adapter.hit == 3 and delays == [5, 10]
    assert not adapter.saved


@pytest.mark.parametrize('residual', [False, True])
async def test_click_failure_after_403_keeps_actual_reason_and_finishes_other_items(harness, residual):
    from ziniao_automation.workflows.amazon_inventory_gpsr.page import ItemClickError
    class BlockedAfter403(RetryAdapter):
        async def locate_item(self, page, item, *, search=False):
            return not (residual and item.sku == 'sku-8' and not search)

        async def _inspect_with_auth(self, page, run, marketplace, item):
            if item.sku == 'sku-8' and self.hit:
                self.hit += 1
                self._item_stage = 'close_sidebar'
                raise ItemClickError('opaque browser message with TOKEN')
            return await super()._inspect_with_auth(page, run, marketplace, item)

    adapter = BlockedAfter403(items(13), 3)
    run = harness.new_run(mode=RunMode.DRY_RUN)
    flow = harness.workflow(adapter)
    await flow.preflight(run, FixturePage())
    await flow.plan(run, FixturePage())
    rows = await harness.ledger.list_items(run.id)
    assert next(row for row in rows if row.item.sku == 'sku-8').reason == 'item_click_timeout'
    counts = await harness.ledger.summary(run.id)
    assert counts['failed'] == 1 and counts['pending'] == 0
    assert adapter.hit == 3 and not adapter.saved
    with harness.factory() as session:
        events = session.scalars(select(RunEvent).where(RunEvent.run_id == run.id)).all()
        end = [e for e in events if e.event_type == 'inventory_gpsr_ui_exhausted']
        assert len(end) == 1 and end[0].details['stage'] == 'close_sidebar'
        assert end[0].details['reason'] == 'item_click_timeout'
        assert 'TOKEN' not in str([(e.message, e.details) for e in events])


async def test_ui_diagnostics_discard_extra_fields(harness):
    adapter = RetryAdapter(items(1), 0)
    run = harness.new_run()
    flow = harness.workflow(adapter)
    await flow._item_event(run, items(1)[0], 'read_failed', {
        'stage': 'first_layer', 'retry': 0, 'reason': 'item_panel_load_timeout',
        'html': '<input value="TOKEN">', 'cookie': 'SECRET', 'url': 'https://example.test/?TOKEN',
    })
    with harness.factory() as session:
        event = session.scalar(select(RunEvent).where(RunEvent.event_type == 'inventory_gpsr_ui_read_failed'))
        assert set(event.details) == {'sku', 'asin', 'stage', 'retry', 'reason', 'requirement_type'}
    assert event.details['requirement_type'] == 'safety_attestation'


class FixturePage:
    url = INVENTORY_URL
