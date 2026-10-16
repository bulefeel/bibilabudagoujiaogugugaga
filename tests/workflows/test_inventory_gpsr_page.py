"""Bounded inventory progression and completeness, without business navigation."""
from dataclasses import replace
import asyncio
from types import SimpleNamespace
import ast
from pathlib import Path
import re

import pytest

from ziniao_automation.workflows.amazon_inventory_gpsr.config import (
    DECLARATION, INVENTORY_URL, InventoryGpsrPolicy, InventoryItem, InventoryPageSnapshot,
)
from ziniao_automation.workflows.amazon_inventory_gpsr.page import (
    AmazonInventoryGpsrPage, InventoryContractError, ItemLoadError,
    _AuthenticationResumed,
    _AuthenticationNeeded,
)
from ziniao_automation.workflows.errors import PageRecoveryRequired


FAST = InventoryGpsrPolicy(.3, .3, .3, .002, .005, 0,
                          marketplace_menu_stable_seconds=.005, marketplace_menu_reopen_delay_seconds=.02,
                          scan_restart_delay_seconds=.005)
DE = SimpleNamespace(code='DE', domain='sellercentral.amazon.de')
RUN = SimpleNamespace(id='fixture-run', store=SimpleNamespace(expected_seller_id='fixture seller'))
ITEM = InventoryItem('SKU-1', 'B000000001', 1, 0)


class Page:
    url = INVENTORY_URL

    def __init__(self): self.listeners={}
    def on(self,event,callback): self.listeners.setdefault(event,[]).append(callback)
    def remove_listener(self,event,callback):
        callbacks=self.listeners.get(event,[])
        if callback in callbacks:callbacks.remove(callback)
    def emit(self,event,value=None):
        for callback in list(self.listeners.get(event,[])):callback(value)


def state(**kwargs):
    return dict(ready=True, loading=False, skus=[ITEM.sku], asins=[ITEM.asin], **kwargs)


def form(**kwargs):
    return state(heading=True, formCount=1,
        boxes=[dict(label=DECLARATION, value='false', checked=False, disabled=False)],
        saves=[dict(label='保存', disabled=True)], **kwargs)


class PanelAdapter(AmazonInventoryGpsrPage):
    def __init__(self, states):
        super().__init__(policy=FAST)
        self.states = list(states)
        self.reads = 0

    async def _panel_snapshot(self, page, run, marketplace):
        self.reads += 1
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]


async def test_transient_expired_or_previous_item_does_not_finish_form_wait():
    adapter = PanelAdapter([
        dict(state(formCount=0), skus=['OLD-SKU']),
        state(formCount=0, expired=True),
        form(),
    ])
    result = await adapter._wait_panel(Page(), RUN, DE, ITEM, lambda s: bool(s.get('formCount')))
    adapter._validate_form(result)
    assert adapter.reads >= 4


async def test_permanent_old_item_is_contract_failure():
    adapter = PanelAdapter([dict(form(), skus=['OLD-SKU'])])
    with pytest.raises(InventoryContractError, match='其他商品'):
        await adapter._wait_panel(Page(), RUN, DE, ITEM, lambda s: True)


async def test_never_loaded_panel_has_bounded_item_failure():
    adapter = PanelAdapter([dict(ready=False)])
    with pytest.raises(ItemLoadError):
        await adapter._wait_panel(Page(), RUN, DE, ITEM, lambda s: True)


@pytest.mark.parametrize('mutation', ['wording', 'duplicate', 'unknown_heading', 'disabled'])
def test_unknown_form_and_checkbox_never_qualify(mutation):
    s = form()
    if mutation == 'wording': s['boxes'][0]['label'] = '其他声明'
    if mutation == 'duplicate': s['boxes'].append(dict(s['boxes'][0]))
    if mutation == 'unknown_heading': s['heading'] = False
    if mutation == 'disabled': s['boxes'][0]['disabled'] = True
    with pytest.raises(InventoryContractError):
        AmazonInventoryGpsrPage._validate_form(s)


async def test_receipt_for_other_item_is_not_success():
    adapter = PanelAdapter([dict(state(receipt=True), skus=['OTHER'])])
    assert await adapter.confirm_submission(Page(), RUN, DE, ITEM) is False


class ScanAdapter(AmazonInventoryGpsrPage):
    def __init__(self, rows, drift=None):
        super().__init__(policy=FAST)
        self.rows = rows
        self.number = 1
        self.turns = []
        self.drift = drift

    async def _session(self, page, run, marketplace):
        return page

    async def _read_search_state(self, page):
        return dict(recognized=True,active=False,values=[''])

    async def _wait_list(self, page):
        size = 100
        rows = self.rows[(self.number - 1)*size:self.number*size]
        items = tuple(InventoryItem(sku, asin, self.number, i) for i, (sku, asin) in enumerate(rows))
        total = len(self.rows) + (1 if self.drift == self.number else 0)
        return InventoryPageSnapshot(self.number, total, size, items)

    async def _turn_page(self, page, current, target):
        self.number = min(max(1, target), max(1, (len(self.rows) + 99)//100))
        self.turns.append(self.number)
        return await self._wait_list(page)


def rows(n):
    return [(f'SKU-{i}', f'B{i:09d}') for i in range(n)]


async def test_201_items_and_same_asin_different_skus_scan_all_pages():
    data = rows(201)
    data[1] = (data[1][0], data[0][1])
    adapter = ScanAdapter(data)
    result = await adapter.scan_candidates(Page(), RUN, DE)
    assert [i.sku for i in result] == [r[0] for r in data]
    assert adapter.turns == [1, 2, 3]
    assert result[0].asin == result[1].asin


async def test_scan_count_change_stops_before_any_write():
    with pytest.raises(PageRecoveryRequired) as error:
        await ScanAdapter(rows(201), drift=2).scan_candidates(Page(), RUN, DE)
    assert error.value.reason_code == "inventory_scan_unstable"


async def test_scan_duplicate_sku_on_later_page_is_not_silently_deduplicated():
    data = rows(201)
    data[200] = data[0]
    with pytest.raises(InventoryContractError, match='重复'):
        await ScanAdapter(data).scan_candidates(Page(), RUN, DE)


async def test_locate_reuses_current_page_and_clamps_original_page_after_shrink():
    adapter = ScanAdapter(rows(99))
    item = InventoryItem('SKU-98', 'B000000098', 3, 0)
    assert await adapter.locate_item(Page(), item)
    assert not adapter.turns
    assert not await adapter.locate_item(Page(), replace(item, sku='missing'))
    assert adapter.turns == [1]


async def test_changed_asin_for_same_sku_stops():
    with pytest.raises(InventoryContractError, match='ASIN'):
        await ScanAdapter(rows(1)).locate_item(Page(), replace(ITEM, sku='SKU-0'))


@pytest.mark.parametrize('url', [
    'https://sellercentral.amazon.de.evil.test/myinventory/inventory',
    'https://sellercentral.amazon.de/myinventory/inventory/',
    'http://sellercentral.amazon.de/myinventory/inventory',
    'https://sellercentral.amazon.de/myinventory/inventory;other',
    'https://sellercentral.amazon.de:444/myinventory/inventory',
])
def test_exact_route_only(url):
    with pytest.raises(InventoryContractError):
        AmazonInventoryGpsrPage._require_route(SimpleNamespace(url=url))


def test_policy_file_requires_complete_fields(tmp_path):
    p = tmp_path/'policy.toml'
    p.write_text('[timing]\npoll_seconds=1\n')
    adapter = AmazonInventoryGpsrPage(policy_path=p)
    with pytest.raises(ValueError, match='exactly'):
        _ = adapter.timing


async def test_reauthentication_page_alias_retained():
    old, new = Page(), Page()
    old.url = 'https://sellercentral.amazon.de/ap/signin'
    class Login:
        async def advance_or_raise(self, *args, **kwargs): return new
    class IdentityAdapter(AmazonInventoryGpsrPage):
        async def _identity(self, page, run): assert page is new
    adapter = IdentityAdapter(policy=FAST, login_advancer=Login())
    assert await adapter._session(old, RUN, DE) is new
    assert adapter.resolved_page_for(old) is new


async def test_hung_dom_read_is_cancelled_at_stage_deadline():
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    cancelled=[]
    async def forever():
        try: await asyncio.Future()
        finally: cancelled.append(True)
    with pytest.raises(ItemLoadError):
        await adapter._poll(forever,lambda s:True,.05,message='deadline')
    assert cancelled == [True]


async def test_cancel_callback_stops_before_inventory_action():
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    calls=[]
    async def cancelled(run):
        calls.append(run.id)
        raise RuntimeError('cancelled')
    adapter.cancel_check=cancelled
    with pytest.raises(RuntimeError,match='cancelled'):
        await adapter.inspect_item(Page(),RUN,DE,ITEM)
    assert calls == [RUN.id]


async def test_reauth_during_sidebar_relocates_only_current_item_once():
    from ziniao_automation.workflows.amazon_inventory_gpsr.config import Inspection
    old,new=Page(),Page()
    class Adapter(AmazonInventoryGpsrPage):
        calls=0
        located=[]
        async def _inspect_item(self,page,*args):
            self.calls+=1
            if self.calls==1:
                self._remember(page,new,RUN,DE)
                raise _AuthenticationResumed()
            assert page is new
            return Inspection('eligible',page=page)
        async def locate_item(self,page,item,**kwargs):
            self.located.append((page,item.sku))
            return True
    adapter=Adapter(policy=FAST)
    outcome=await adapter.inspect_item(old,RUN,DE,ITEM)
    assert outcome.page is new
    assert adapter.located == [(new,ITEM.sku)]
    assert adapter.calls==2


@pytest.mark.parametrize('new_tab',[False,True])
async def test_confirm_keeps_reading_after_successful_reauth(new_tab):
    old,new=Page(),Page()
    class Adapter(AmazonInventoryGpsrPage):
        reads=0
        async def _panel_snapshot(self,page,*args):
            self.reads+=1
            if self.reads==1:
                if new_tab: self._remember(page,new,RUN,DE)
                raise _AuthenticationResumed()
            assert self.resolved_page_for(page) is (new if new_tab else old)
            return state(receipt=True)
    adapter=Adapter(policy=FAST)
    adapter._panel_requirement[old] = ITEM.requirement_type
    assert await adapter.confirm_submission(old,RUN,DE,ITEM)
    assert adapter.reads>1


@pytest.mark.parametrize('read_kind',['list','panel','confirmation'])
async def test_login_budget_not_cut_short_by_dom_deadline(read_kind):
    page=Page()
    page.url='https://sellercentral.amazon.de/ap/signin'
    completed=[]
    class Login:
        async def advance_or_raise(self,p,**kwargs):
            # Longer than the DOM budget, with enough room for Windows' coarse
            # event-loop timer while the complete suite loads the machine.
            await asyncio.sleep(.75)
            p.url=INVENTORY_URL
            completed.append(True)
            return p
    class Adapter(AmazonInventoryGpsrPage):
        async def _identity(self,*args): pass
        async def _evaluate(self,p,script,arg=None):
            if 'inventory:list' in script:
                return dict(ready=True,loading=False,filters=['ApprovalRequired'],page=1,total=0,size=100,items=[])
            return state(receipt=True)
    short=replace(FAST,list_timeout_seconds=.5,panel_timeout_seconds=.5,
                  confirmation_timeout_seconds=.5)
    adapter=Adapter(policy=short,login_advancer=Login())
    adapter._remember(page,page,RUN,DE)
    adapter._panel_requirement[page] = ITEM.requirement_type
    if read_kind=='list':
        assert (await adapter._wait_list(page)).total_items==0
    elif read_kind=='panel':
        with pytest.raises(_AuthenticationResumed):
            await adapter._wait_panel(page,RUN,DE,ITEM,lambda s:True)
    else:
        assert await adapter.confirm_submission(page,RUN,DE,ITEM)
    assert completed==[True]


async def test_hung_locator_count_is_cancelled_without_click():
    adapter=AmazonInventoryGpsrPage(policy=replace(FAST,panel_timeout_seconds=.06))
    cancelled=[]
    class Locator:
        async def count(self):
            try: await asyncio.Future()
            finally: cancelled.append(True)
        async def click(self,**kwargs):
            raise AssertionError('must not click after count hang')
    with pytest.raises(ItemLoadError):
        await adapter._click_unique(Locator())
    assert cancelled==[True]


async def test_bounded_ui_cancels_hung_attribute_read():
    adapter=AmazonInventoryGpsrPage(policy=replace(FAST,panel_timeout_seconds=.06))
    cancelled=[]
    async def read_attribute():
        try: await asyncio.Future()
        finally: cancelled.append(True)
    with pytest.raises(ItemLoadError):
        await adapter._bounded_ui(read_attribute())
    assert cancelled==[True]


@pytest.mark.parametrize('status', [403, 429, 500, 503, 599])
def test_inventory_http_failure_latches_same_run_and_new_run_detaches_old_listeners(status):
    adapter=AmazonInventoryGpsrPage(policy=FAST)
    page,other=Page(),Page()
    adapter._watch_inventory_responses(page,RUN)
    assert len(page.listeners['response'])==1
    page.emit('response',SimpleNamespace(url='https://sellercentral.amazon.de/myinventory/gql',status=status))
    for current in (page,other):
        if status == 403:
            adapter._watch_inventory_responses(current,RUN)
        else:
            with pytest.raises(PageRecoveryRequired,match=f'HTTP {status}') as error:
                adapter._watch_inventory_responses(current,RUN)
            assert error.value.http_status == status
    page.emit('response',SimpleNamespace(url='https://sellercentral.amazon.de/myinventory/gql',status=200))
    with pytest.raises(PageRecoveryRequired):adapter._require_inventory_http_ok()
    next_run=SimpleNamespace(id='next-run')
    adapter._watch_inventory_responses(other,next_run)
    assert not page.listeners['response']
    assert len(other.listeners['response'])==1
    adapter._require_inventory_http_ok()
    other.emit('close')
    assert not other.listeners['response']
    assert not adapter._response_observers


@pytest.mark.parametrize('url',[
    'https://sellercentral.amazon.de.evil.test/myinventory/gql',
    'https://sellercentral.amazon.de/myinventory/gql/other',
    'https://sellercentral.amazon.de/payments/gql',
    'https://sellercentral.amazon.co.uk/myinventory/gql',
])
def test_other_responses_do_not_trip_inventory_guard(url):
    adapter=AmazonInventoryGpsrPage(policy=FAST)
    page=Page();adapter._watch_inventory_responses(page,RUN)
    class Response:
        status=403
        @property
        def headers(self):raise AssertionError('must never read headers')
        @property
        def request(self):raise AssertionError('must never read request')
        def body(self):raise AssertionError('must never read body')
    response=Response();response.url=url
    page.emit('response',response)
    adapter._require_inventory_http_ok()


def test_relevant_refusal_observer_reads_status_only_without_request_or_body():
    class Response:
        url = 'https://sellercentral.amazon.de/myinventory/gql'
        status = 429
        @property
        def request(self):
            raise AssertionError('must not inspect request credentials or payload')
        @property
        def headers(self):
            raise AssertionError('must not inspect response headers')
        def body(self):
            raise AssertionError('must not inspect response body')
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    page = Page()
    adapter._watch_inventory_responses(page, RUN)
    page.emit('response', Response())
    with pytest.raises(PageRecoveryRequired) as error:
        adapter._require_inventory_http_ok()
    assert error.value.http_status == 429


@pytest.mark.parametrize('trigger', ['detach', 'close', 'new_run'])
def test_closed_transport_listener_cleanup_does_not_break_report_or_next_run(trigger):
    class TargetClosedError(Exception):
        pass
    class ClosingPage(Page):
        closed = False
        removed = []
        def remove_listener(self, event, callback):
            self.removed.append(event)
            if self.closed:
                raise TargetClosedError('Target page, context or browser has been closed')
            super().remove_listener(event, callback)
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    page = ClosingPage()
    adapter._watch_inventory_responses(page, RUN)
    page.emit('response', SimpleNamespace(url='https://sellercentral.amazon.de/myinventory/gql', status=403))
    page.closed = True
    if trigger == 'new_run':
        new_page = Page()
        adapter._watch_inventory_responses(new_page, SimpleNamespace(id='new-run'))
        assert list(adapter._response_observers) == [new_page]
        adapter._require_inventory_http_ok()
    else:
        if trigger == 'close': page.emit('close')
        else: adapter.detach_response_observers()
        assert not adapter._response_observers
        with pytest.raises(PageRecoveryRequired, match='HTTP 403'):
            adapter._require_inventory_http_ok()
    assert page.removed == ['response', 'close']


def test_listener_cleanup_does_not_hide_unrelated_programming_error():
    class BrokenPage(Page):
        def remove_listener(self, event, callback):
            raise ValueError('unexpected error')
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    page = BrokenPage()
    adapter._watch_inventory_responses(page, RUN)
    with pytest.raises(ValueError, match='unexpected error'):
        adapter.detach_response_observers()


def test_only_new_execution_attempt_clears_same_run_refusal_and_old_callbacks_stay_detached():
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    page = Page()
    adapter.begin_attempt(RUN.id)
    adapter._watch_inventory_responses(page, RUN)
    old_response = page.listeners['response'][0]
    old_close = page.listeners['close'][0]
    failure = SimpleNamespace(url='https://sellercentral.amazon.de/myinventory/gql', status=403)
    old_response(failure)
    consumed = (str(RUN.id), DE.code, ITEM.sku)
    adapter._consumed.add(consumed)
    adapter.detach_response_observers()
    adapter._watch_inventory_responses(page, RUN)
    with pytest.raises(PageRecoveryRequired):
        adapter._require_inventory_http_ok()

    adapter.begin_attempt(RUN.id)
    adapter._watch_inventory_responses(page, RUN)
    old_response(failure)
    old_close()
    adapter._require_inventory_http_ok()
    assert consumed in adapter._consumed
    assert len(page.listeners['response']) == len(page.listeners['close']) == 1
    # A fresh failure is retained until valid DOM evidence or another attempt.
    page.emit('response', failure)
    with pytest.raises(PageRecoveryRequired) as error:
        adapter._require_inventory_http_ok()
    assert error.value.http_status == 403


@pytest.mark.parametrize('stage', ['count', 'click', 'read'])
async def test_closed_page_is_not_downgraded_to_single_item_load_error(stage):
    class TargetClosedError(Exception):
        pass
    class Locator:
        async def count(self):
            if stage == 'count':
                raise TargetClosedError('closed fixture')
            return 1
        async def click(self, **kwargs):
            raise TargetClosedError('closed fixture')
    async def read():
        raise TargetClosedError('closed fixture')
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    with pytest.raises(TargetClosedError):
        if stage == 'read':
            await adapter._bounded_ui(read())
        else:
            await adapter._click_unique(Locator())


def test_inventory_adapter_has_no_active_http_client_or_browser_network_primitives():
    source = (Path(__file__).parents[2] / 'src/ziniao_automation/workflows/amazon_inventory_gpsr/page.py').read_text(encoding='utf8')
    tree = ast.parse(source)
    forbidden_imports = {'requests', 'httpx', 'aiohttp', 'socket', 'http.client', 'urllib.request'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name in forbidden_imports for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden_imports
        # This also catches page.context.request and arbitrary aliases of page.
        if isinstance(node, ast.Attribute):
            assert node.attr not in {'request', 'new_cdp_session'}
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not re.search(r'\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\s*\(', node.value)


@pytest.mark.parametrize('present', [True, False])
async def test_reconcile_searches_only_when_candidate_missing_from_current_and_original_page(present):
    from ziniao_automation.workflows.amazon_inventory_gpsr.config import Inspection
    class Adapter(PanelAdapter):
        calls = []
        async def locate_item(self, page, item, *, search=False):
            self.calls.append(('locate', search))
            return present or search
        async def inspect_item(self, page, run, marketplace, item):
            self.calls.append(('inspect', item.sku))
            return Inspection('already_submitted', page=page)
    adapter = Adapter([dict(ready=False)])
    adapter.calls = []
    assert await adapter.reconcile_item(Page(), RUN, DE, ITEM)
    assert adapter.calls == ([('locate', False)] +
        ([] if present else [('locate', True)]) + [('inspect', ITEM.sku)])


@pytest.mark.parametrize('repeat', [False, True])
async def test_reconcile_resumes_same_item_once_after_sidebar_authentication(repeat):
    old, new = Page(), Page()
    class Adapter(AmazonInventoryGpsrPage):
        calls = 0
        async def _reconcile_item(self, page, run, marketplace, item):
            self.calls += 1
            assert item is ITEM
            if self.calls == 1 or repeat:
                self._remember(old, new, RUN, DE)
                raise _AuthenticationResumed()
            assert page is new
            return True
    adapter = Adapter(policy=FAST)
    if repeat:
        with pytest.raises(ItemLoadError, match='回读未恢复'):
            await adapter.reconcile_item(old, RUN, DE, ITEM)
    else:
        assert await adapter.reconcile_item(old, RUN, DE, ITEM)
    assert adapter.calls == 2
