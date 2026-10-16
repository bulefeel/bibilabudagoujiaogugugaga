"""Known page-level CSRF corruption, bounded recovery and durable save guards."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from ziniao_automation.models import RunEvent
from ziniao_automation.workflows.amazon_inventory_gpsr.config import (
    INVENTORY_URL, Inspection, InventoryGpsrPolicy, InventoryPageSnapshot,
)
from ziniao_automation.workflows.amazon_inventory_gpsr.http_diagnostic import is_csrf_header_loss
from ziniao_automation.workflows.amazon_inventory_gpsr.page import (
    AmazonInventoryGpsrPage, InventoryContractError, InventoryCsrfHeaderLoss, ItemPanelLoadError,
)
from ziniao_automation.workflows.amazon_inventory_gpsr.store import InventoryRunCancelled
from ziniao_automation.workflows.errors import HumanAuthRequired, PageRecoveryRequired
from ziniao_automation.workflows.types import RunMode, RunStatus
from .test_inventory_gpsr_page import Page, RUN, DE, ITEM, FAST
from .test_inventory_gpsr_workflow import Adapter, harness, items


class Response:
    url = 'https://sellercentral.amazon.de/myinventory/gql'
    status = 403

    def __init__(self, *, request_headers=None, body=None, method='POST', content_type='text/html'):
        self.request = SimpleNamespace(method=method, headers=request_headers or {})
        self.headers = {'content-type': content_type}
        self.text = AsyncMock(return_value=body if body is not None else
                              '<html><title>Amazon</title><h1>Unsafe cross site request.</h1></html>')


@pytest.mark.parametrize('options, expected', [
    ({}, True),
    ({'body': '<title> Unsafe cross site request. </title>'}, True),
    ({'request_headers': {'Cookie': 'SECRET_NOT_LOGGED'}}, True),
    ({'request_headers': {'Anti-CSRFToken-A2Z': 'TOKEN_NOT_LOGGED'}}, False),
    ({'request_headers': {'Content-Type': 'application/json'}}, False),
    ({'method': 'GET'}, False),
    ({'content_type': 'application/json'}, False),
    ({'body': '<h1>Access denied</h1>'}, False),
    ({'body': '<script>"Unsafe cross site request."</script>'}, False),
    ({'body': '<h1>Unsafe cross site request. Other problem.</h1>'}, False),
])
async def test_diagnostic_requires_exact_error_and_missing_headers(options, expected):
    assert await is_csrf_header_loss(Response(**options), timeout=.1) is expected


@pytest.mark.parametrize('trigger', ['new_attempt', 'detach', 'close'])
async def test_late_response_diagnostic_cannot_pollute_another_attempt_or_page(trigger):
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    page = Page()
    adapter._watch_inventory_responses(page, RUN)
    old_response_callback = page.listeners['response'][0]
    response = Response()
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_body():
        started.set()
        await release.wait()
        return '<h1>Unsafe cross site request.</h1>'

    response.text = delayed_body
    page.emit('response', response)
    task = asyncio.create_task(adapter._diagnose_forbidden_response())
    await started.wait()
    if trigger == 'new_attempt':
        adapter.begin_attempt(RUN.id)
    elif trigger == 'detach':
        adapter.detach_response_observers()
    else:
        page.emit('close')
    adapter._watch_inventory_responses(page, RUN)
    old_response_callback(Response())
    release.set()
    await task
    assert not adapter._csrf_header_loss
    assert adapter._pending_diagnostic is None
    assert adapter._network_failure == (None if trigger == 'new_attempt' else 403)


async def test_unreadable_or_hanging_body_stays_unknown_403_and_never_leaks(caplog):
    adapter = AmazonInventoryGpsrPage(policy=replace(FAST, http_diagnostic_timeout_seconds=.02))
    page = Page()
    adapter._watch_inventory_responses(page, RUN)
    response = Response()

    async def hangs():
        await asyncio.Event().wait()

    response.text = hangs
    page.emit('response', response)
    await asyncio.wait_for(adapter._diagnose_forbidden_response(), .2)
    assert adapter._network_failure == 403 and not adapter._csrf_header_loss
    adapter._clear_forbidden_response()
    response.text = AsyncMock(side_effect=RuntimeError('Cookie=SECRET_NOT_LOGGED'))
    page.emit('response', response)
    await adapter._diagnose_forbidden_response()
    assert 'SECRET_NOT_LOGGED' not in caplog.text
    assert not adapter._csrf_header_loss


class RecoveryPage(Page):
    def __init__(self):
        super().__init__()
        self.reloads = 0
        self.corrupt = True

    async def reload(self, **kwargs):
        assert kwargs['wait_until'] == 'domcontentloaded' and kwargs['timeout'] > 0
        self.reloads += 1
        self.corrupt = False


class RecoveryAdapter(AmazonInventoryGpsrPage):
    def __init__(self):
        super().__init__(policy=replace(FAST, http_retry_delay_seconds=0,
                                       csrf_recovery_timeout_seconds=.2))
        self._identity = AsyncMock()
        self._wait_list = AsyncMock(return_value=InventoryPageSnapshot(1, 1, 100, (ITEM,)))
        self.locate_item = AsyncMock(return_value=True)
        self.visits = []
        self.on_http_event = AsyncMock()

    async def _inspect_with_auth(self, page, run, marketplace, item):
        self._active_run = run
        self._watch_inventory_responses(page, run)
        self.visits.append(item.sku)
        if page.corrupt:
            page.emit('response', Response())
            raise ItemPanelLoadError('anonymous no-action overlay')
        return Inspection('eligible', page=page)


async def test_same_tab_recovery_revalidates_and_relocates_original_sku_without_resetting_save_marker():
    adapter, page = RecoveryAdapter(), RecoveryPage()
    consumed = (str(RUN.id), DE.code, ITEM.sku, ITEM.requirement_type)
    adapter._consumed.add(consumed)
    result = await adapter.inspect_item(page, RUN, DE, ITEM)
    assert result.status == 'eligible' and result.page is page
    assert page.reloads == 1 and adapter.visits == [ITEM.sku, ITEM.sku]
    adapter._identity.assert_awaited_once_with(page, RUN)
    adapter.locate_item.assert_awaited_once_with(page, ITEM)
    assert adapter._consumed == {consumed}
    assert adapter._csrf_reloads == 1
    with pytest.raises(InventoryContractError, match='只允许回读'):
        await adapter.submit_once(page, RUN, DE, ITEM)
    actions = [call.args[2] for call in adapter.on_http_event.await_args_list]
    assert actions == ['csrf_detected', 'csrf_reload', 'csrf_restored']


async def test_budget_is_shared_across_items_and_only_new_attempt_resets_it():
    adapter, page = RecoveryAdapter(), RecoveryPage()
    await adapter.inspect_item(page, RUN, DE, ITEM)
    page.corrupt = True
    with pytest.raises(PageRecoveryRequired, match='次数已用完'):
        await adapter.inspect_item(page, RUN, DE, items(2)[1])
    assert page.reloads == 1
    adapter.detach_response_observers()
    assert adapter._csrf_reloads == 1
    adapter.begin_attempt(RUN.id)
    assert adapter._csrf_reloads == 0


@pytest.mark.parametrize('fault', ['timeout', 'closed', 'cancel', 'wrong_domain', 'wrong_filter', 'identity', 'auth'])
async def test_recovery_failure_has_bounded_exit_without_reopening_or_saving(fault):
    adapter, page = RecoveryAdapter(), RecoveryPage()
    async def reload(**kwargs):
        page.reloads += 1
        if fault == 'timeout':
            await asyncio.Event().wait()
        elif fault == 'closed':
            raise RuntimeError('Target closed https://example.test/?TOKEN')
        elif fault == 'cancel':
            raise InventoryRunCancelled('cancelled')
        elif fault == 'wrong_domain':
            page.url = INVENTORY_URL.replace('.de', '.co.uk')
        elif fault == 'wrong_filter':
            page.url = INVENTORY_URL.replace('approval_required', 'all')
        elif fault == 'identity':
            adapter._identity.side_effect = InventoryContractError('identity changed')
        elif fault == 'auth':
            page.url = 'https://sellercentral.amazon.de/ap/signin'
            adapter.login.advance_or_raise = AsyncMock(side_effect=HumanAuthRequired('login', kind='signin'))
    page.reload = reload
    error = {'cancel': InventoryRunCancelled, 'wrong_domain': InventoryContractError,
             'wrong_filter': InventoryContractError, 'identity': InventoryContractError,
             'auth': HumanAuthRequired}.get(fault, PageRecoveryRequired)
    with pytest.raises(error):
        await asyncio.wait_for(adapter.inspect_item(page, RUN, DE, ITEM), .6)
    assert page.reloads == 1 and adapter.visits == [ITEM.sku]
    assert not adapter.locate_item.await_count


@pytest.mark.parametrize('status', [429, 503])
async def test_new_rate_limit_during_recovery_delay_prevents_reload(monkeypatch, status):
    adapter, page = RecoveryAdapter(), RecoveryPage()
    async def delay(seconds):
        adapter._network_failure = status
    monkeypatch.setattr('ziniao_automation.workflows.amazon_inventory_gpsr.page.asyncio.sleep', delay)
    with pytest.raises(PageRecoveryRequired) as error:
        await adapter.inspect_item(page, RUN, DE, ITEM)
    assert error.value.http_status == status and page.reloads == 0


class LedgerRecoveryAdapter(Adapter, AmazonInventoryGpsrPage):
    inspect_item = AmazonInventoryGpsrPage.inspect_item

    def __init__(self, *, repeated=False):
        Adapter.__init__(self, items(13))
        AmazonInventoryGpsrPage.__init__(self, policy=replace(FAST, http_retry_delay_seconds=0))
        self._identity = AsyncMock()
        self._wait_list = AsyncMock(return_value=InventoryPageSnapshot(1, 13, 100, self.items))
        self.repeated = repeated

    async def _inspect_with_auth(self, page, run, marketplace, item):
        self._active_run = run
        self._watch_inventory_responses(page, run)
        if (item.sku == 'sku-8' and page.corrupt) or (self.repeated and item.sku == 'sku-7'):
            page.emit('response', Response())
            raise ItemPanelLoadError('fixture broken document')
        return await Adapter.inspect_item(self, page, run, marketplace, item)


@pytest.mark.parametrize('mode', [RunMode.DRY_RUN, RunMode.AUTO])
async def test_fifth_of_thirteen_recovers_all_remaining_using_one_snapshot(harness, mode):
    adapter, page = LedgerRecoveryAdapter(), RecoveryPage()
    run = harness.new_run(mode=mode)
    flow = harness.workflow(adapter)
    plan = await flow.plan(run, page)
    if plan.lines:
        await flow.execute(run, page, plan)
    counts = await harness.ledger.summary(run.id)
    assert counts['scanned'] == counts['eligible'] == 13
    assert counts['failed'] == counts['pending'] == counts['uncertain'] == 0
    assert counts['confirmed'] == (13 if mode is RunMode.AUTO else 0)
    assert adapter.inspected == [f'sku-{i}' for i in reversed(range(13))]
    assert len(adapter.saved) == len(set(adapter.saved))
    assert page.reloads == adapter.scans == 1
    with harness.factory() as session:
        events = session.scalars(select(RunEvent).where(RunEvent.event_type.like('inventory_gpsr_http_csrf%'))).all()
        assert [event.event_type for event in events] == [
            'inventory_gpsr_http_csrf_detected', 'inventory_gpsr_http_csrf_reload',
            'inventory_gpsr_http_csrf_restored',
        ]
        assert all(set(event.details) == {'http_status', 'retry', 'sku', 'asin'} for event in events)


async def test_repeated_corruption_keeps_unvisited_records_pending_for_resume(harness):
    adapter, page = LedgerRecoveryAdapter(repeated=True), RecoveryPage()
    run = harness.new_run(mode=RunMode.DRY_RUN)
    flow = harness.workflow(adapter)
    with pytest.raises(PageRecoveryRequired, match='次数已用完'):
        await flow.plan(run, page)
    counts = await harness.ledger.summary(run.id)
    assert counts['scanned'] == 13 and counts['eligible'] == 5 and counts['pending'] == 8
    assert counts['failed'] == 0 and page.reloads == 1
    rows = await harness.ledger.list_items(run.id)
    before = [(row.id, row.item) for row in rows]
    assert next(row for row in rows if row.item.sku == 'sku-7').state == 'PENDING'
    # Explicit resumed execution: original candidates, no rescanning or redoing
    # five already inspected records even when today's listing has changed.
    adapter.repeated = False
    adapter.begin_attempt(run.id)
    await harness.repo.set_run_status(run.id, RunStatus.RUNNING)
    adapter.items = items(15)
    await flow.plan(run, page)
    assert adapter.scans == 1 and len(adapter.inspected) == 13
    assert before == [(row.id, row.item) for row in await harness.ledger.list_items(run.id)]


async def test_header_loss_after_persisted_intent_never_enters_save_recovery(harness):
    adapter, page = LedgerRecoveryAdapter(), RecoveryPage()
    run = harness.new_run()
    flow = harness.workflow(adapter)
    plan = await flow.plan(run, page)
    async def lost_save_response(item):
        adapter._network_failure = 403
        adapter._csrf_header_loss = True
        raise InventoryCsrfHeaderLoss()
    adapter.on_save = lost_save_response
    with pytest.raises(PageRecoveryRequired):
        await flow.execute(run, page, plan)
    rows = await harness.ledger.list_items(run.id)
    assert rows[0].state == 'UNCERTAIN' and rows[0].intent_at is not None
    assert all(row.state == 'PENDING' for row in rows[1:])
    assert not adapter.saved and page.reloads == 0
    # A new execution resolves this intent read-only. No second save is allowed.
    adapter.begin_attempt(run.id)
    adapter.known_saved.add(rows[0].item.sku)
    await harness.repo.set_run_status(run.id, RunStatus.RECONCILING)
    await flow.reconcile(run, page)
    assert (await harness.ledger.get(rows[0].id)).state == 'CONFIRMED'
    assert not adapter.saved and page.reloads == 0


@pytest.mark.parametrize('budget', [-1, 2, True, 1.5])
def test_reload_budget_is_small_explicit_and_shared(budget):
    with pytest.raises(ValueError):
        InventoryGpsrPolicy(csrf_reload_attempts=budget)


@pytest.mark.parametrize('search', [True, False])
async def test_location_recovery_keeps_original_item_and_search_mode(search):
    adapter, page = RecoveryAdapter(), RecoveryPage()
    # Exercise the real public location wrapper, including multi-page candidates.
    del adapter.locate_item
    adapter._remember(page, page, RUN, DE)
    adapter._watch_inventory_responses(page, RUN)
    page.emit('response', Response())
    await adapter._diagnose_forbidden_response()
    candidate = items(201)[200]
    adapter._locate_item = AsyncMock(side_effect=[InventoryCsrfHeaderLoss(), True])
    assert await adapter.locate_item(page, candidate, search=search)
    assert adapter._locate_item.await_count == 2 and page.reloads == 1
    assert all(call.args == (page, candidate) and call.kwargs == {'search': search}
               for call in adapter._locate_item.await_args_list)


@pytest.mark.parametrize('confirmed', [True, False])
async def test_reconciliation_recovery_returns_only_receipt_result(confirmed):
    adapter, page = RecoveryAdapter(), RecoveryPage()
    adapter._watch_inventory_responses(page, RUN)
    page.emit('response', Response())
    await adapter._diagnose_forbidden_response()
    adapter._reconcile_item = AsyncMock(side_effect=[InventoryCsrfHeaderLoss(), confirmed])
    adapter.submit_once = AsyncMock(side_effect=AssertionError('must not save during reconciliation'))
    assert await adapter.reconcile_item(page, RUN, DE, ITEM) is confirmed
    assert page.reloads == 1 and adapter._reconcile_item.await_count == 2
    adapter.submit_once.assert_not_awaited()
