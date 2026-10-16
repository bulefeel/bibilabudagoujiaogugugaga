"""Offline DOM contracts; no seller navigation, CDP, or network is available."""
from pathlib import Path
from dataclasses import replace
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from ziniao_automation.workflows.amazon_inventory_gpsr.config import INVENTORY_URL, InventoryGpsrPolicy
from ziniao_automation.workflows.amazon_inventory_gpsr.page import (
    AmazonInventoryGpsrPage, InventoryContractError, ItemLoadError, InventoryItemHttpError,
    _SAVE, _PANEL, _INSTALL_OPEN_TRACE, _CLEAR_OPEN_TRACE,
    ItemClickError, ItemPanelLoadError, ItemPanelResetError, ItemSearchLoadError,
)
from ziniao_automation.workflows.errors import PageRecoveryRequired

BROWSER = os.getenv('ZINIAO_LOGIN_FIXTURE_BROWSER', '')
pytestmark = [pytest.mark.asyncio(loop_scope='module'),
              pytest.mark.skipif(not BROWSER, reason='Requires local offline fixture renderer')]
HTML = (Path(__file__).parents[1]/'fixtures/amazon_inventory_gpsr/inventory.html').read_text(encoding='utf8')
RUN = SimpleNamespace(id='fixture-run', store=SimpleNamespace(expected_seller_id='fixture seller'))
DE = SimpleNamespace(code='DE', domain='sellercentral.amazon.de')
POLICY = InventoryGpsrPolicy(2, 2, 2, .01, .03, 0, http_retry_delay_seconds=0,
                            marketplace_menu_stable_seconds=.03, marketplace_menu_reopen_delay_seconds=.08)


class OfflinePage:
    url = INVENTORY_URL
    def __init__(self, raw):
        self.raw = raw
        self.handlers=[]
    @property
    def request(self):
        raise AssertionError('Inventory must use UI, not page.request')
    @property
    def context(self):
        class UiOnlyContext:
            @property
            def request(self):
                raise AssertionError('Inventory must use UI, not context.request')
        return UiOnlyContext()
    def locator(self, *args, **kwargs): return self.raw.locator(*args, **kwargs)
    def get_by_placeholder(self,*args,**kwargs): return self.raw.get_by_placeholder(*args,**kwargs)
    def on(self,event,callback):
        self.handlers.append((event,callback))
        return self.raw.on(event,callback)
    def remove_listener(self,event,callback):
        if (event,callback) in self.handlers:self.handlers.remove((event,callback))
        return self.raw.remove_listener(event,callback)
    def response(self,status,url='https://sellercentral.amazon.de/myinventory/gql'):
        for event,callback in list(self.handlers):
            if event=='response':callback(SimpleNamespace(status=status,url=url))
    async def evaluate(self, expr, arg=None):
        if expr == _SAVE:
            return await self.raw.evaluate('(p)=>{const location=new URL(p.url);return ('+expr+')(p.arg)}',
                                           {'url':self.url,'arg':arg})
        return await self.raw.evaluate(expr,arg)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def renderer():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=BROWSER, headless=True,
            args=['--disable-background-networking', '--no-first-run'])
        context = await browser.new_context(offline=True, service_workers='block')
        await context.route('**/*',lambda r:r.abort())
        raw = await context.new_page()
        try: yield raw
        finally: await browser.close()


@pytest_asyncio.fixture(loop_scope='module')
async def page(renderer):
    assert renderer.url == 'about:blank'
    await renderer.set_content(HTML)
    wrapped=OfflinePage(renderer)
    try: yield wrapped
    finally:
        for event,callback in list(wrapped.handlers):wrapped.remove_listener(event,callback)


async def opened(page):
    adapter = AmazonInventoryGpsrPage(policy=POLICY)
    await adapter.open_inventory(page,RUN,DE)
    items = await adapter.scan_candidates(page,RUN,DE)
    return adapter,items


async def test_full_dry_run_reaches_form_and_skips_epr_without_checkbox_or_save(page):
    adapter,items = await opened(page)
    results=[]
    for item in items:
        results.append((await adapter.inspect_item(page,RUN,DE,item)).status)
    assert results == ['eligible','no_attestation','no_violation_entry']
    actions = await page.evaluate('window.fixtureClicks')
    assert not any(a.startswith(('checkbox:','save:')) for a in actions)


@pytest.mark.parametrize('changed', ['none', 'seller', 'asin'])
async def test_csrf_empty_sidebar_recovers_same_tab_and_rechecks_real_dom(page, changed):
    from unittest.mock import AsyncMock
    adapter, items = await opened(page)
    target = items[0]
    await page.evaluate("window.fixtureNoActionSkus=['SKU-GPSR']")
    response = SimpleNamespace(
        url='https://sellercentral.amazon.de/myinventory/gql', status=403,
        request=SimpleNamespace(method='POST', headers={}),
        headers={'content-type': 'text/html'},
        text=AsyncMock(return_value='<title>Amazon</title><h1>Unsafe cross site request.</h1>'),
    )
    original_click = adapter._click_unique
    clicked, reloads = [], []

    async def click(locator):
        await original_click(locator)
        clicked.append(True)
        if len(clicked) == 1:
            for event, handler in list(page.handlers):
                if event == 'response':
                    handler(response)

    async def reload(**kwargs):
        reloads.append(kwargs)
        content = HTML
        if changed == 'seller':
            content = content.replace('fixture seller', 'another seller')
        elif changed == 'asin':
            content = content.replace('B000000001', 'B000000009')
        await page.raw.set_content(content)

    adapter._click_unique = click
    page.reload = reload
    if changed == 'none':
        result = await adapter.inspect_item(page, RUN, DE, target)
        assert result.status == 'eligible' and result.page is page
    else:
        with pytest.raises(InventoryContractError):
            await adapter.inspect_item(page, RUN, DE, target)
    assert len(reloads) == 1 and len(clicked) == (4 if changed == 'none' else 1)
    actions = await page.evaluate('window.fixtureClicks')
    assert not any(a.startswith(('checkbox:', 'save:')) for a in actions)


@pytest.mark.parametrize('hidden_before', [False, True])
async def test_multiple_real_compliance_sections_open_only_first_visible_entry(page, hidden_before):
    captured = (Path(__file__).parents[1]/'fixtures/amazon_inventory_gpsr/multiple_violations.html').read_text(encoding='utf8')
    await page.evaluate("""({html, hiddenBefore}) => {
      const row=document.querySelector('[data-sku="SKU-GPSR"] kat-link');
      const open=row.onclick;
      window.fixtureSectionClicks=[];
      row.onclick=()=>{
        open();
        const proceed=document.querySelector('#violation').onclick;
        const template=document.createElement('template');template.innerHTML=html;
        const panel=document.querySelector('kat-panel');
        panel.innerHTML=template.content.querySelector('kat-panel').innerHTML;
        const sections=panel.querySelectorAll('[data-testid="compliance-requirement-section"]');
        sections.forEach((section,index)=>{
          section.querySelector('kat-box').onclick=()=>{
            window.fixtureSectionClicks.push(index);
            if(index===0)proceed();
          };
        });
        if(hiddenBefore){
          const hidden=sections[0].cloneNode(true);hidden.hidden=true;
          sections[0].before(hidden);
        }
      };
    }""", {'html':captured,'hiddenBefore':hidden_before})
    adapter,items=await opened(page)
    assert (await adapter.inspect_item(page,RUN,DE,items[0])).status=='eligible'
    assert await page.evaluate('window.fixtureSectionClicks')==[0]
    actions=await page.evaluate('window.fixtureClicks')
    assert actions.count('violation:SKU-GPSR')==1
    assert actions.count('card:SKU-GPSR')==1
    assert not any(a.startswith(('checkbox:','save:')) for a in actions)


async def test_panel_snapshot_without_trace_or_token_has_empty_open_identity(page):
    await page.raw.locator('[data-sku="SKU-GPSR"] kat-link').click()
    snapshot=await page.evaluate(_PANEL, {
        'panel':'kat-panel[data-testid="kat-panel-ActionPanelContent"]',
        'receipt':'您提交的内容正在审核中。将在七天内做出决定',
    })
    assert snapshot['openedSku']==snapshot['openedAsin']==''
    assert snapshot['violationCount']==1
    await page.raw.evaluate('('+_CLEAR_OPEN_TRACE+')(undefined)')


async def test_responsible_person_requirement_with_matching_sku_is_not_attestation(page):
    await page.evaluate("""() => {
      const row=document.querySelector('[data-sku="SKU-GPSR"] kat-link');
      const open=row.onclick;
      row.onclick=()=>{
        open();
        document.querySelector('#violation').onclick=()=>{
          window.fixtureClicks.push('responsible:SKU-GPSR');
          document.querySelector('kat-panel').innerHTML=
            '<div>SKU：SKU-GPSR</div><h3>负责人信息</h3><p>Fixture contact information</p>';
        };
      };
    }""")
    adapter,items=await opened(page)
    result=await adapter.inspect_item(page,RUN,DE,items[0])
    assert result.status=='no_attestation'
    snapshot=await adapter._panel_snapshot(page,RUN,DE)
    assert snapshot['knownOther'] is True
    assert snapshot['skus']==['SKU-GPSR'] and snapshot['asins']==[]
    actions=await page.evaluate('window.fixtureClicks')
    assert actions==['open:SKU-GPSR','responsible:SKU-GPSR']


@pytest.mark.parametrize('content', [
    '<p>请提供负责人信息</p>',
    '<h3 hidden>负责人信息</h3>',
    '<h3>负责人信息及其他信息</h3>',
    '<h3>负责人信息</h3><h3>负责人信息</h3>',
    '<h3>负责人信息</h3><div role="button" aria-label="安全证明">安全证明</div>',
    '<h3>负责人信息</h3><form data-testid="form">GPSR 安全认证</form>',
    '<h3>负责人信息</h3><kat-checkbox name="value">声明</kat-checkbox>',
    '<h3>负责人信息</h3><kat-button data-testid="primary-button">保存</kat-button>',
])
async def test_responsible_person_text_or_unknown_form_never_counts_as_known_other(page,content):
    adapter,_=await opened(page)
    await page.evaluate("""html=>{
      const panel=document.querySelector('kat-panel');panel.hidden=false;
      panel.innerHTML='<div>SKU：SKU-GPSR</div>'+html;
    }""",content)
    snapshot=await adapter._panel_snapshot(page,RUN,DE)
    assert snapshot['knownOther'] is False


async def test_responsible_person_requirement_for_different_sku_never_skips(page):
    adapter,items=await opened(page)
    adapter._timing=replace(POLICY,panel_timeout_seconds=.2)
    await page.evaluate("""()=>{
      const panel=document.querySelector('kat-panel');panel.hidden=false;
      panel.innerHTML='<div>SKU：OTHER-SKU</div><h3>负责人信息</h3>';
    }""")
    with pytest.raises(ItemLoadError):
        await adapter._wait_panel(page,RUN,DE,items[0],lambda s:s.get('knownOther'),
                                  allow_sku_only_skip=True)


async def test_transient_expired_waits_then_saves_exactly_once_and_confirms(page):
    await page.evaluate('window.fixtureDelay=120')
    adapter,items = await opened(page)
    item=items[0]
    assert (await adapter.inspect_item(page,RUN,DE,item)).status == 'eligible'
    await adapter.submit_once(page,RUN,DE,item)
    assert await adapter.confirm_submission(page,RUN,DE,item)
    with pytest.raises(InventoryContractError,match='只允许回读'):
        await adapter.submit_once(page,RUN,DE,item)
    actions=await page.evaluate('window.fixtureClicks')
    assert actions.count('save:SKU-GPSR') == 1
    assert actions.count('checkbox:SKU-GPSR') == 1


@pytest.mark.parametrize('mutation', [
    "document.querySelector('.dropdown-account-switcher-header-label-global').textContent='OTHER'",
    "document.querySelector('.dropdown-account-switcher-header-label-regional').textContent='英国'",
    "const h=document.querySelector('#dropdown-account-switcher-container');h.after(h.cloneNode(true))",
    "document.querySelector('kat-checkbox[name=value]').setAttribute('label','different statement')",
    "document.querySelector('kat-panel').querySelector('div:nth-child(2)').textContent='SKU：OTHER'",
])
async def test_form_identity_or_wording_drift_never_saves(page,mutation):
    adapter,items=await opened(page)
    await adapter.inspect_item(page,RUN,DE,items[0])
    await page.evaluate(mutation)
    with pytest.raises(InventoryContractError):
        await adapter.submit_once(page,RUN,DE,items[0])
    assert not any(a.startswith('save:') for a in await page.evaluate('window.fixtureClicks'))


async def test_same_tick_save_rejects_sidebar_swap(page):
    adapter,items=await opened(page)
    await adapter.inspect_item(page,RUN,DE,items[0])
    # Swap after identity preflight but before the atomic browser-side final check.
    calls=0
    original=adapter._identity
    async def identity(*args):
        nonlocal calls
        await original(*args)
        calls += 1
        if calls==2:
            await page.evaluate("document.querySelector('kat-panel').querySelector('div:nth-child(2)').textContent='SKU：OTHER'")
    adapter._identity=identity
    with pytest.raises(InventoryContractError,match='item_changed'):
        await adapter.submit_once(page,RUN,DE,items[0])
    assert not any(a.startswith('save:') for a in await page.evaluate('window.fixtureClicks'))


async def test_real_shadow_pagination_scans_201_then_locates_after_shrink(page):
    await page.evaluate("""() => {
      const pager=document.querySelector('kat-pagination');
      pager.attachShadow({mode:'open'}).innerHTML='<span part="pagination-nav-left" tabindex="-1">Previous</span><span part="pagination-nav-right" tabindex="0">Next</span>';
      window.fixtureRows=Array.from({length:201},(_,i)=>({sku:'SKU-'+i,asin:'B'+String(i).padStart(9,'0')}));
      window.fixtureRender=(number)=>{
        const page=Math.min(number,Math.max(1,Math.ceil(window.fixtureRows.length/100)));
        pager.setAttribute('page',page);pager.setAttribute('total-items',window.fixtureRows.length);
        pager.shadowRoot.querySelector('[part=pagination-nav-left]').setAttribute('tabindex',page===1?'-1':'0');
        pager.shadowRoot.querySelector('[part=pagination-nav-right]').setAttribute('tabindex',page*100>=window.fixtureRows.length?'-1':'0');
        document.querySelector('#rows').innerHTML=window.fixtureRows.slice((page-1)*100,page*100).map(r=>`<div data-sku="${r.sku}"><a href="https://www.amazon.de/dp/${r.asin}">fixture</a><kat-link label="解决商品信息问题">解决商品信息问题</kat-link></div>`).join('');
      };
      pager.shadowRoot.querySelector('[part=pagination-nav-left]').onclick=()=>window.fixtureRender(Number(pager.getAttribute('page'))-1);
      pager.shadowRoot.querySelector('[part=pagination-nav-right]').onclick=()=>window.fixtureRender(Number(pager.getAttribute('page'))+1);
      window.fixtureRender(1);
    }""")
    adapter,items=await opened(page)
    assert len(items)==201
    assert [i.sku for i in items] == [f'SKU-{i}' for i in range(201)]
    # Last page disappears. Remaining original page 2 is already current.
    await page.evaluate('window.fixtureRows.pop();window.fixtureRender(3)')
    assert await adapter.locate_item(page,items[199])
    assert await adapter.locate_item(page,items[0])


class ResetLanding:
    def __init__(self, *, clear=True):
        self.calls=[]
        self.clear=clear
    async def land_on(self,page,**kwargs):
        self.calls.append(kwargs['url'])
        assert kwargs['url']==INVENTORY_URL
        page.url=INVENTORY_URL
        if self.clear:
            await page.raw.set_content(HTML)
        return SimpleNamespace(ok=True,page=page)


@pytest.mark.parametrize('change', [
    ('fulfilledBy=all','fulfilledBy=amazon'),
    ('pageSize=100','pageSize=10'),
    ('sort=date_created_desc','sort=sku_asc'),
    ('status=approval_required','status=approval_required&searchTerm=SKU-GPSR'),
])
async def test_filtered_url_is_normalized_once_before_scan(page,change):
    page.url=INVENTORY_URL.replace(*change)
    landing=ResetLanding()
    adapter=AmazonInventoryGpsrPage(policy=POLICY,landing=landing)
    await adapter.open_inventory(page,RUN,DE)
    assert len(await adapter.scan_candidates(page,RUN,DE))==3
    assert landing.calls==[INVENTORY_URL]


async def test_correct_url_and_empty_search_reuses_page(page):
    landing=ResetLanding()
    adapter=AmazonInventoryGpsrPage(policy=POLICY,landing=landing)
    await adapter.open_inventory(page,RUN,DE)
    assert len(await adapter.scan_candidates(page,RUN,DE))==3
    assert not landing.calls


async def set_search(page,sku):
    field=page.raw.locator('kat-input-group input')
    await field.fill(sku)
    await field.press('Enter')


@pytest.mark.parametrize('mode',['ALL','SKU','TITLE_KEYWORD'])
async def test_dom_only_search_is_reset_before_fresh_scan(page,mode):
    await page.evaluate("m=>document.querySelector('kat-input-group kat-dropdown').setAttribute('value',m)",mode)
    await set_search(page,'SKU-GPSR')
    landing=ResetLanding()
    adapter=AmazonInventoryGpsrPage(policy=POLICY,landing=landing)
    # Direct scan still enforces full inventory; no caller-order assumption.
    assert len(await adapter.scan_candidates(page,RUN,DE))==3
    assert landing.calls==[INVENTORY_URL]


async def test_reset_that_keeps_search_must_not_report_subset_as_full_scan(page):
    await set_search(page,'SKU-GPSR')
    landing=ResetLanding(clear=False)
    adapter=AmazonInventoryGpsrPage(policy=POLICY,landing=landing)
    with pytest.raises(InventoryContractError,match='搜索条件'):
        await adapter.scan_candidates(page,RUN,DE)
    assert landing.calls==[INVENTORY_URL]
    assert not await page.evaluate('window.fixtureClicks')


async def test_search_from_readback_cannot_filter_next_pending_item(page):
    landing=ResetLanding()
    adapter=AmazonInventoryGpsrPage(policy=POLICY,landing=landing)
    await adapter.open_inventory(page,RUN,DE)
    items=await adapter.scan_candidates(page,RUN,DE)
    assert await adapter.locate_item(page,items[1],search=True)
    assert len((await adapter._wait_list(page)).items)==1
    assert await adapter.locate_item(page,items[0])
    assert landing.calls==[INVENTORY_URL]
    assert (await adapter.inspect_item(page,RUN,DE,items[0])).status=='eligible'


async def test_host_own_shadow_root_search_and_checkbox_are_read_once(page):
    adapter,items=await opened(page)
    search=await adapter._read_search_state(page)
    assert search==dict(recognized=True,active=False,values=[''],mode='ALL')
    await adapter.inspect_item(page,RUN,DE,items[0])
    await page.evaluate("""() => {
      const host=document.querySelector('kat-panel kat-checkbox');
      host.shadowRoot.querySelector('input').checked=true;
      host.setAttribute('value','false');
    }""")
    panel=await adapter._panel_snapshot(page,RUN,DE)
    assert len(panel['boxes'])==1
    assert panel['boxes'][0]['checked'] is True
    assert panel['boxes'][0]['value']=='false'


async def test_receipt_split_across_heading_and_paragraph_is_confirmed(page):
    adapter,items=await opened(page)
    await page.evaluate('window.fixtureSplitReceipt=true')
    await adapter.inspect_item(page,RUN,DE,items[0])
    await adapter.submit_once(page,RUN,DE,items[0])
    assert await adapter.confirm_submission(page,RUN,DE,items[0])
    # Whitespace is presentation only; altered wording/punctuation remains a miss.
    await page.evaluate("document.querySelector('kat-panel h2').textContent='您提交的内容正在审核中，'")
    assert (await adapter._panel_snapshot(page,RUN,DE))['receipt'] is False


async def test_identity_free_no_action_requires_current_row_click_and_new_render(page):
    adapter,items=await opened(page)
    await page.evaluate("window.fixtureNoActionSkus=['SKU-EPR','SKU-NONE']")
    for item in items[1:]:
        outcome=await adapter.inspect_item(page,RUN,DE,item)
        assert outcome.status=='no_violation_entry'
        assert '无需采取任何操作' in outcome.reason
        assert await page.evaluate('window.__ziniaoInventoryOpenTrace===undefined')
        assert await page.evaluate("document.querySelector('kat-panel h4#actionPanelHeader').getClientRects().length") == 0
        assert await page.evaluate("document.querySelector('kat-panel kat-alert').getClientRects().length") > 0
    assert not any(a.startswith(('card:','checkbox:','save:')) for a in await page.evaluate('window.fixtureClicks'))


async def test_previous_no_action_without_new_render_never_skips_next_item(page):
    adapter,items=await opened(page)
    await page.evaluate("window.fixtureNoActionSkus=['SKU-NONE']")
    assert (await adapter.inspect_item(page,RUN,DE,items[2])).status=='no_violation_entry'
    adapter._timing=replace(POLICY,panel_timeout_seconds=.3)
    await page.evaluate('window.fixtureKeepPanel=true')
    with pytest.raises(ItemLoadError):
        await adapter.inspect_item(page,RUN,DE,items[0])
    assert await page.evaluate('window.__ziniaoInventoryOpenTrace===undefined')


async def test_clicking_another_row_invalidates_no_action_trace(page):
    adapter,items=await opened(page)
    await page.evaluate("window.fixtureNoActionSkus=['SKU-EPR','SKU-NONE']")
    token='fixture-open-token'
    item=items[1]
    payload=dict(panel='kat-panel[data-testid="kat-panel-ActionPanelContent"]',
                 sku=item.sku,asin=item.asin,token=token)
    assert (await page.evaluate(_INSTALL_OPEN_TRACE,payload))['ok']
    adapter._open_tokens[page]=token
    try:
        await page.raw.locator('[data-sku="SKU-EPR"] kat-link').click()
        assert (await adapter._panel_snapshot(page,RUN,DE))['freshNoAction']
        await page.raw.locator('[data-sku="SKU-NONE"] kat-link').click()
        snapshot=await adapter._panel_snapshot(page,RUN,DE)
        assert snapshot['noAction'] is True
        assert snapshot['freshNoAction'] is False
        assert snapshot['receipt'] is False
    finally:
        await page.evaluate(_CLEAR_OPEN_TRACE,token)
        adapter._open_tokens.pop(page,None)


async def test_no_action_description_drift_is_not_a_skip_or_receipt(page):
    adapter,items=await opened(page)
    await page.evaluate("window.fixtureNoActionSkus=['SKU-NONE']")
    await adapter.inspect_item(page,RUN,DE,items[2])
    await page.evaluate("document.querySelector('kat-alert').setAttribute('description','正在发生其他错误')")
    snapshot=await adapter._panel_snapshot(page,RUN,DE)
    assert snapshot['noAction'] is False
    assert snapshot['receipt'] is False


@pytest.mark.parametrize('status',[429,503])
async def test_http_failure_with_no_action_alert_stops_instead_of_skipping(page,status):
    adapter,items=await opened(page)
    await page.evaluate("window.fixtureNoActionSkus=['SKU-NONE']")
    original=adapter._click_unique
    async def click(locator):
        await original(locator)
        page.response(status)
    adapter._click_unique=click
    with pytest.raises(PageRecoveryRequired,match=f'HTTP {status}') as error:
        await adapter.inspect_item(page,RUN,DE,items[2])
    assert error.value.http_status == status
    assert not any(a.startswith(('card:','checkbox:','save:')) for a in await page.evaluate('window.fixtureClicks'))
    assert await page.evaluate('window.__ziniaoInventoryOpenTrace===undefined')


async def test_http_403_with_verified_form_allows_single_save(page):
    adapter,items=await opened(page)
    await adapter.inspect_item(page,RUN,DE,items[0])
    page.response(403)
    await adapter.submit_once(page,RUN,DE,items[0])
    assert await adapter.confirm_submission(page,RUN,DE,items[0])
    assert (await page.evaluate('window.fixtureClicks')).count('save:SKU-GPSR') == 1


async def test_initial_navigation_has_observer_before_list_response(page):
    class FailingLanding(ResetLanding):
        async def land_on(self,page,**kwargs):
            assert any(event=='response' for event,callback in page.handlers)
            page.response(403)
            return await super().land_on(page,**kwargs)
    page.url=INVENTORY_URL.replace('fulfilledBy=all','fulfilledBy=amazon')
    landing=FailingLanding()
    adapter=AmazonInventoryGpsrPage(policy=POLICY,landing=landing)
    await adapter.open_inventory(page,RUN,DE)
    assert len(await adapter.scan_candidates(page,RUN,DE)) == 3
    assert landing.calls==[INVENTORY_URL]


async def test_ui_only_scan_search_inspect_submit_and_readback_without_http_apis(page):
    await page.raw.evaluate("""() => {
      window.fixtureNetworkAttempts=[];
      for(const name of ['fetch','XMLHttpRequest','WebSocket','EventSource'])
        Object.defineProperty(window,name,{configurable:true,writable:true,value:function(){
          window.fixtureNetworkAttempts.push(name);throw new Error('UI-only fixture: '+name);
        }});
      Object.defineProperty(navigator,'sendBeacon',{configurable:true,value:function(){
        window.fixtureNetworkAttempts.push('sendBeacon');throw new Error('UI-only fixture: sendBeacon');
      }});
    }""")
    adapter, items = await opened(page)
    item = items[0]
    assert await adapter.locate_item(page, item, search=True)
    assert (await adapter.inspect_item(page, RUN, DE, item)).status == 'eligible'
    await adapter.submit_once(page, RUN, DE, item)
    assert await adapter.confirm_submission(page, RUN, DE, item)
    assert await adapter.reconcile_item(page, RUN, DE, item)
    assert await page.raw.evaluate('window.fixtureNetworkAttempts') == []
    actions = await page.raw.evaluate('window.fixtureClicks')
    assert actions.count('save:SKU-GPSR') == 1
    assert actions.count('checkbox:SKU-GPSR') == 1


async def test_explicit_resume_reuses_page_and_keeps_already_consumed_save(page):
    adapter, items = await opened(page)
    item = items[0]
    await adapter.inspect_item(page, RUN, DE, item)
    await adapter.submit_once(page, RUN, DE, item)
    assert await adapter.confirm_submission(page, RUN, DE, item)
    page.response(429)
    with pytest.raises(PageRecoveryRequired):
        await adapter.reconcile_item(page, RUN, DE, item)
    before = await page.raw.evaluate('window.fixtureClicks')
    adapter.begin_attempt(RUN.id)
    assert await adapter.reconcile_item(page, RUN, DE, item)
    assert await page.raw.evaluate('window.fixtureClicks') == before
    with pytest.raises(InventoryContractError, match='只允许回读'):
        await adapter.submit_once(page, RUN, DE, item)
    assert (await page.raw.evaluate('window.fixtureClicks')).count('save:SKU-GPSR') == 1


@pytest.mark.parametrize('view', ['receipt', 'review_card', 'first_layer', 'form'])
async def test_reconcile_reuses_current_bound_panel_without_search_or_reopening(page, view):
    adapter, items = await opened(page)
    item = items[0]
    if view == 'form':
        await adapter.inspect_item(page, RUN, DE, item)
    else:
        await page.raw.locator('[data-sku="SKU-GPSR"] kat-link').click()
        if view != 'first_layer':
            await page.evaluate("""view => {
              const panel=document.querySelector('kat-panel');
              panel.innerHTML='<div>SKU：SKU-GPSR</div><div>ASIN：B000000001</div>'+(
                view==='receipt' ? '<h2>您提交的内容正在审核中。</h2><p>将在七天内做出决定</p>' :
                '<div role="button" aria-label="安全证明">安全证明 审核中</div>');
            }""", view)
    before = await page.evaluate('window.fixtureClicks')
    assert await adapter.reconcile_item(page, RUN, DE, item) is (view in {'receipt', 'review_card'})
    after = await page.evaluate('window.fixtureClicks')
    if view == 'first_layer':
        assert after[len(before):] == ['violation:SKU-GPSR', 'card:SKU-GPSR']
    else:
        assert after == before
    assert not any(a.startswith(('checkbox:', 'save:')) for a in after)
    assert (await adapter._read_search_state(page))['values'] == ['']


async def test_reconcile_item_in_current_list_opens_row_without_search(page):
    adapter, items = await opened(page)
    assert await adapter.reconcile_item(page, RUN, DE, items[0]) is False
    assert await page.evaluate('window.fixtureClicks') == [
        'open:SKU-GPSR', 'violation:SKU-GPSR', 'card:SKU-GPSR']
    assert (await adapter._read_search_state(page))['values'] == ['']


async def delayed_search(page, delay=120, *, update=True):
    await page.evaluate("""({delay,update}) => {
      window.fixtureSearches=0;
      const group=document.querySelector('kat-input-group');
      const host=group.querySelector('kat-input');
      const input=host.shadowRoot.querySelector('input');
      const option=group.querySelector('kat-dropdown').shadowRoot.querySelector('kat-option');
      const select=option.onclick;
      option.onclick=()=>{select();host.setAttribute('placeholder','SKU1, SKU2');};
      const search=input.onkeydown;
      input.onkeydown=event=>{
        if(event.key!=='Enter')return;
        window.fixtureSearches=(window.fixtureSearches||0)+1;
        const captured={key:event.key,target:{value:input.value}};
        if(update)setTimeout(()=>search(captured),delay);
      };
    }""", dict(delay=delay, update=update))


async def test_shadow_search_fills_native_input_and_waits_for_delayed_new_list(page):
    adapter, items = await opened(page)
    await delayed_search(page)
    assert await adapter.locate_item(page, items[1], search=True)
    assert await page.evaluate('window.fixtureSearches') == 1
    assert (await adapter._wait_list(page)).items == (replace(items[1], position=0),)
    assert await page.evaluate('window.__ziniaoInventorySearchTrace===undefined')


async def test_search_stale_list_never_counts_as_completed_lookup(page):
    adapter, items = await opened(page)
    adapter._timing = replace(POLICY, list_timeout_seconds=.25)
    await delayed_search(page, update=False)
    with pytest.raises(ItemSearchLoadError, match='未在期限内更新'):
        await adapter.locate_item(page, items[0], search=True)
    assert await page.evaluate('window.fixtureSearches') == 1
    assert await page.evaluate('window.__ziniaoInventorySearchTrace===undefined')
    assert not await page.evaluate('window.fixtureClicks')


async def test_search_waits_for_new_empty_result_before_missing_item(page):
    adapter, items = await opened(page)
    await delayed_search(page)
    missing = replace(items[0], sku='MISSING-SKU')
    assert await adapter.locate_item(page, missing, search=True) is False
    assert (await adapter._wait_list(page)).total_items == 0
    assert await page.evaluate('window.fixtureSearches') == 1


async def test_reconcile_rejects_changed_asin_without_any_side_panel_action(page):
    adapter, items = await opened(page)
    await page.evaluate("document.querySelector('[data-sku=SKU-GPSR] a').href='https://www.amazon.de/dp/B000000009'")
    with pytest.raises(InventoryContractError, match='ASIN'):
        await adapter.reconcile_item(page, RUN, DE, items[0])
    assert not await page.evaluate('window.fixtureClicks')


async def test_stale_empty_result_and_unrelated_panel_render_are_not_new_search_results(page):
    adapter, items = await opened(page)
    await set_search(page, 'PREVIOUS-MISSING-SKU')
    assert (await adapter._wait_list(page)).total_items == 0
    adapter._timing = replace(POLICY, list_timeout_seconds=.25)
    await delayed_search(page, update=False)
    await page.evaluate("""() => {
      const input=document.querySelector('kat-input').shadowRoot.querySelector('input');
      const previous=input.onkeydown;
      input.onkeydown=event=>{
        previous(event);
        const panel=document.querySelector('kat-panel');panel.hidden=false;
        panel.innerHTML='<p>Unrelated sidebar update</p>';
      };
    }""")
    with pytest.raises(ItemSearchLoadError, match='未在期限内更新'):
        await adapter.locate_item(page, replace(items[0], sku='NEW-MISSING-SKU'), search=True)
    assert await page.evaluate('window.fixtureSearches') == 1


async def test_search_existing_sku_mode_is_not_selected_again(page):
    adapter, items = await opened(page)
    await page.evaluate("""() => {
      const dropdown=document.querySelector('kat-input-group kat-dropdown');
      dropdown.setAttribute('value','SKU');
      dropdown.onclick=()=>{throw new Error('must not reopen SKU selector');};
      dropdown.shadowRoot.querySelector('kat-option').onclick=()=>{throw new Error('must not select SKU twice');};
    }""")
    assert await adapter.locate_item(page, items[1], search=True)
    assert (await adapter._wait_list(page)).items == (replace(items[1], position=0),)


@pytest.mark.parametrize('mutation', [
    "pager.setAttribute('page','1')",
    "pager.setAttribute('total-items','0')",
    "pager.setAttribute('items-per-page','100')",
    "pager.setAttribute('disabled','true')",
    "pager.setAttribute('aria-busy','false')",
])
async def test_early_pager_attribute_reset_does_not_confirm_stale_empty_search(page, mutation):
    adapter, items = await opened(page)
    await set_search(page, 'PREVIOUS-MISSING-SKU')
    assert (await adapter._wait_list(page)).total_items == 0
    adapter._timing = replace(POLICY, list_timeout_seconds=.3)
    await delayed_search(page, update=False)
    await page.evaluate("""mutation => {
      const input=document.querySelector('kat-input').shadowRoot.querySelector('input');
      const previous=input.onkeydown;
      input.onkeydown=event=>{
        previous(event);
        const pager=document.querySelector('kat-pagination');
        new Function('pager', mutation)(pager);
      };
    }""", mutation)
    with pytest.raises(ItemSearchLoadError, match='未在期限内更新'):
        await adapter.locate_item(page, items[0], search=True)
    assert await page.evaluate('window.fixtureSearches') == 1
    assert await page.evaluate('window.__ziniaoInventorySearchTrace===undefined')


async def test_same_page_reset_waits_for_delayed_search_instead_of_old_empty_result(page):
    adapter, items = await opened(page)
    await set_search(page, 'PREVIOUS-MISSING-SKU')
    await delayed_search(page, delay=180)
    await page.evaluate("""() => {
      const input=document.querySelector('kat-input').shadowRoot.querySelector('input');
      const previous=input.onkeydown;
      input.onkeydown=event=>{
        previous(event);
        document.querySelector('kat-pagination').setAttribute('page','1');
      };
    }""")
    assert await adapter.locate_item(page, items[0], search=True)
    assert (await adapter._wait_list(page)).items == (items[0],)
    assert await page.evaluate('window.fixtureSearches') == 1


async def test_403_with_normal_item_dom_does_not_retry_or_pause(page):
    adapter, items = await opened(page)
    events = []
    async def event(run, item, action, retry):
        events.append((action, retry))
    adapter.on_http_event = event
    click = adapter._click_unique
    async def observed_click(locator):
        await click(locator)
        page.response(403)
    adapter._click_unique = observed_click
    assert (await adapter.inspect_item(page, RUN, DE, items[0])).status == 'eligible'
    assert not any(action == 'retry' for action, _ in events)
    assert ('dom_verified', 0) in events
    assert (await page.evaluate('window.fixtureClicks')).count('open:SKU-GPSR') == 1
    await adapter.submit_once(page, RUN, DE, items[0])
    page.response(403)
    assert await adapter.confirm_submission(page, RUN, DE, items[0])
    assert (await page.evaluate('window.fixtureClicks')).count('save:SKU-GPSR') == 1
    with pytest.raises(InventoryContractError, match='只允许回读'):
        await adapter.submit_once(page, RUN, DE, items[0])


@pytest.mark.parametrize('failures', [1, 2, 3])
async def test_403_reopens_only_current_sidebar_at_most_twice_then_next_item(page, failures):
    adapter, items = await opened(page)
    adapter._timing = replace(POLICY, panel_timeout_seconds=.20)
    events, attempts = [], []
    async def event(run, item, action, retry):
        events.append((action, retry))
    adapter.on_http_event = event
    click = adapter._click_unique
    async def observed_click(locator):
        is_row = await locator.get_attribute('label') == '解决商品信息问题'
        if is_row:
            attempts.append(True)
            await page.evaluate("(bad)=>window.fixtureNoActionSkus=bad?['SKU-GPSR']:[]",
                                len(attempts) <= failures)
        await click(locator)
        if is_row and len(attempts) <= failures:
            page.response(403)
    adapter._click_unique = observed_click
    if failures == 3:
        with pytest.raises(InventoryItemHttpError):
            await adapter.inspect_item(page, RUN, DE, items[0])
        assert events[-1] == ('exhausted', 2)
    else:
        assert (await adapter.inspect_item(page, RUN, DE, items[0])).status == 'eligible'
    assert len(attempts) == min(3, failures + 1)
    assert [retry for action, retry in events if action == 'retry'] == list(range(1, min(2, failures) + 1))
    assert not any(a.startswith(('checkbox:', 'save:')) for a in await page.evaluate('window.fixtureClicks'))
    adapter._click_unique = click
    assert (await adapter.inspect_item(page, RUN, DE, items[1])).status == 'no_attestation'


@pytest.mark.parametrize('mutation', ['seller', 'form'])
async def test_403_cannot_mask_wrong_seller_or_form(page, mutation):
    adapter, items = await opened(page)
    click = adapter._click_unique
    async def observed_click(locator):
        is_card = await locator.get_attribute('aria-label') == '安全证明'
        await click(locator)
        page.response(403)
        if mutation == 'seller':
            await page.evaluate("document.querySelector('.dropdown-account-switcher-header-label-global').textContent='wrong seller'")
        elif is_card:
            await page.raw.locator('kat-panel kat-checkbox[name=value]').wait_for(state='visible')
            await page.evaluate("document.querySelector('kat-panel kat-checkbox[name=value]').setAttribute('label','unrecognized declaration')")
    # Alter identity at the moment side-panel loading is observed.
    adapter._click_unique = observed_click
    with pytest.raises(InventoryContractError):
        await adapter.inspect_item(page, RUN, DE, items[0])
    assert not any(a.startswith(('checkbox:', 'save:')) for a in await page.evaluate('window.fixtureClicks'))


async def test_429_after_403_is_not_silenced_by_ui_recovery(page):
    adapter, items = await opened(page)
    page.response(403)
    page.response(429)
    with pytest.raises(PageRecoveryRequired) as error:
        await adapter.inspect_item(page, RUN, DE, items[0])
    assert error.value.http_status == 429
    assert not await page.evaluate('window.fixtureClicks')


async def test_retry_closes_toggle_sidebar_before_reopening_same_sku(page):
    adapter, items = await opened(page)
    adapter._timing = replace(POLICY, panel_timeout_seconds=.30)
    await page.evaluate("""() => {
      const link=document.querySelector('[data-sku="SKU-GPSR"] kat-link');
      const open=link.onclick;
      link.onclick=()=>{
        const panel=document.querySelector('kat-panel');
        if(!panel.hidden && window.fixtureItem?.sku==='SKU-GPSR'){
          window.fixtureClicks.push('toggle-close');panel.hidden=true;return;
        }
        open();
      };
    }""")
    click = adapter._click_unique
    attempts = []
    async def observed(locator):
        is_entry = await locator.get_attribute('label') == '解决商品信息问题'
        if is_entry:
            attempts.append(True)
            await page.evaluate("bad=>window.fixtureNoActionSkus=bad?['SKU-GPSR']:[]", len(attempts)==1)
        await click(locator)
        if is_entry and len(attempts)==1:
            page.response(403)
    adapter._click_unique = observed
    assert (await adapter.inspect_item(page, RUN, DE, items[0])).status == 'eligible'
    clicks = await page.evaluate('window.fixtureClicks')
    assert clicks[:3] == ['open:SKU-GPSR', 'close:SKU-GPSR', 'open:SKU-GPSR']
    assert 'toggle-close' not in clicks
    assert not any(a.startswith(('checkbox:', 'save:')) for a in clicks)


async def test_overlay_is_closed_before_switching_to_next_item(page):
    adapter, items = await opened(page)
    adapter._timing = replace(POLICY, click_timeout_seconds=.15)
    assert (await adapter.inspect_item(page, RUN, DE, items[0])).status == 'eligible'
    await page.evaluate("document.querySelector('kat-panel').style='position:fixed;inset:0;z-index:999;background:white'")
    assert (await adapter.inspect_item(page, RUN, DE, items[1])).status == 'no_attestation'
    clicks = await page.evaluate('window.fixtureClicks')
    assert clicks.count('open:SKU-EPR') == 1
    assert clicks.index('close:SKU-GPSR') < clicks.index('open:SKU-EPR')
    assert not any(a.startswith(('checkbox:', 'save:')) for a in clicks)


async def test_unobstructed_different_item_does_not_close_sidebar(page):
    adapter, items = await opened(page)
    await adapter.inspect_item(page, RUN, DE, items[0])
    await adapter.inspect_item(page, RUN, DE, items[1])
    clicks = await page.evaluate('window.fixtureClicks')
    assert not any(a.startswith('close:') for a in clicks)


async def test_old_403_does_not_hide_later_blocked_click(page):
    adapter, items = await opened(page)
    adapter._timing = replace(POLICY, panel_timeout_seconds=.30, click_timeout_seconds=.10)
    await page.evaluate("window.fixtureNoActionSkus=['SKU-GPSR']")
    click = adapter._click_unique
    first = True
    async def observed(locator):
        nonlocal first
        await click(locator)
        if first:
            first = False
            page.response(403)
            await page.evaluate("""() => {
              const block=document.createElement('div');block.id='blocking-overlay';
              block.style='position:fixed;inset:0;z-index:9999;background:white';
              document.body.append(block);
            }""")
    adapter._click_unique = observed
    events = []
    async def event(run, item, action, details):
        events.append((action, details))
    adapter.on_item_event = event
    with pytest.raises(ItemClickError) as error:
        await adapter.inspect_item(page, RUN, DE, items[0])
    assert not isinstance(error.value, InventoryItemHttpError)
    assert events[-1][0] == 'exhausted'
    assert events[-1][1]['stage'] == 'close_sidebar'
    assert events[-1][1]['reason'] == 'item_click_timeout'
    assert events[-1][1]['retry'] == 2
    assert (await page.evaluate('window.fixtureClicks')) == ['open:SKU-GPSR']
    await page.evaluate("document.getElementById('blocking-overlay').remove()")
    adapter._click_unique = click
    assert (await adapter.inspect_item(page, RUN, DE, items[1])).status == 'no_attestation'


@pytest.mark.parametrize('broken', ['hidden_close', 'close_does_nothing'])
async def test_sidebar_reset_requires_visible_control_and_confirmed_close(page, broken):
    adapter, items = await opened(page)
    adapter._timing = replace(POLICY, panel_timeout_seconds=.25, click_timeout_seconds=.10)
    await adapter.inspect_item(page, RUN, DE, items[0])
    await page.evaluate("""broken=>{
      const close=document.querySelector('kat-panel').shadowRoot.querySelector('button');
      if(broken==='hidden_close')close.hidden=true;
      else close.onclick=()=>{};
    }""", broken)
    with pytest.raises(ItemPanelResetError):
        await adapter.inspect_item(page, RUN, DE, items[0])
    assert (await page.evaluate('window.fixtureClicks')).count('open:SKU-GPSR') == 1
    assert not await page.evaluate("window.fixtureClicks.some(x=>x.startsWith('save:'))")


async def test_current_sku_stale_content_without_new_render_is_not_accepted(page):
    adapter, items = await opened(page)
    adapter._timing = replace(POLICY, panel_timeout_seconds=.25)
    await page.raw.locator('[data-sku="SKU-GPSR"] kat-link').click()
    await page.evaluate("""() => {
      const panel=document.querySelector('kat-panel');panel.hidden=true;
      document.querySelector('[data-sku="SKU-GPSR"] kat-link').onclick=()=>{
        window.fixtureClicks.push('stale-open');panel.hidden=false;
      };
    }""")
    with pytest.raises(ItemPanelLoadError):
        await adapter.inspect_item(page, RUN, DE, items[0])
    assert await page.evaluate("window.fixtureClicks.includes('stale-open')")
    assert not await page.evaluate("window.fixtureClicks.some(x=>x.startsWith('violation:'))")


@pytest.mark.parametrize('stuck', [False, True])
async def test_details_hidden_close_returns_to_first_layer_then_closes(page, stuck):
    adapter, items = await opened(page)
    adapter._timing = replace(POLICY, panel_timeout_seconds=.35, click_timeout_seconds=.10)
    await page.raw.locator('[data-sku="SKU-GPSR"] kat-link').click()
    await page.raw.locator('#violation').click()
    await page.evaluate("""stuck=>{
      const panel=document.querySelector('kat-panel');
      const close=panel.shadowRoot.querySelector('button');close.hidden=true;
      const back=document.createElement('kat-button');
      back.setAttribute('data-testid','primary-button');back.setAttribute('label','返回');
      back.textContent='返回';panel.append(back);
      back.onclick=()=>{
        window.fixtureClicks.push('return:SKU-GPSR');
        if(stuck)return;
        setTimeout(()=>{
          document.querySelector('[data-sku="SKU-GPSR"] kat-link').onclick();
          close.hidden=false;
        },40);
      };
    }""", stuck)
    if stuck:
        with pytest.raises(ItemPanelResetError):
            await adapter.inspect_item(page, RUN, DE, items[0])
        assert (await page.evaluate('window.fixtureClicks')).count('open:SKU-GPSR') == 1
    else:
        assert (await adapter.inspect_item(page, RUN, DE, items[0])).status == 'eligible'
        clicks = await page.evaluate('window.fixtureClicks')
        assert clicks.index('return:SKU-GPSR') < clicks.index('close:SKU-GPSR')
    assert not await page.evaluate("window.fixtureClicks.some(x=>x.startsWith('save:'))")


async def test_unchecked_form_returns_two_levels_without_saving(page):
    adapter, items = await opened(page)
    await adapter.inspect_item(page, RUN, DE, items[0])
    await page.evaluate("""() => {
      const panel=document.querySelector('kat-panel');
      const close=panel.shadowRoot.querySelector('button');close.hidden=true;
      const back=document.createElement('kat-button');
      back.setAttribute('data-testid','back-button');back.setAttribute('label','返回');
      back.textContent='返回';panel.append(back);
      back.onclick=()=>{
        window.fixtureClicks.push('form-back');
        panel.innerHTML='<div>ASIN：B000000001</div><div>SKU：SKU-GPSR</div>'+
          '<h2>警告和安全信息</h2><div>商品文件</div>'+
          '<div role="button" aria-label="安全证明">安全证明 未开始</div>'+
          '<kat-button data-testid="primary-button" label="返回">返回</kat-button>';
        panel.querySelector('kat-button').onclick=()=>{
          window.fixtureClicks.push('requirements-back');
          document.querySelector('[data-sku="SKU-GPSR"] kat-link').onclick();
          close.hidden=false;
        };
      };
    }""")
    await adapter._prepare_item_entry(page, RUN, DE, items[0],
        page.locator('[data-sku="SKU-GPSR"] kat-link'))
    assert not await page.raw.locator('kat-panel').is_visible()
    clicks = await page.evaluate('window.fixtureClicks')
    assert clicks.index('form-back') < clicks.index('requirements-back') < clicks.index('close:SKU-GPSR')
    assert not any(a.startswith(('checkbox:', 'save:')) for a in clicks)


@pytest.mark.parametrize("save_label", ["保存", "儲存"])
@pytest.mark.parametrize("traditional_declaration", [False, True])
async def test_localized_attestation_form_and_atomic_save(page, save_label, traditional_declaration):
    from ziniao_automation.workflows.amazon_inventory_gpsr.config import DECLARATIONS
    html = HTML.replace('label="保存" disabled="true">保存',
                        f'label="{save_label}" disabled="true">{save_label}')
    if traditional_declaration:
        html = html.replace(DECLARATIONS[0], DECLARATIONS[1])
    await page.raw.set_content(html)
    adapter, candidates = await opened(page)
    item = candidates[0]
    assert (await adapter.inspect_item(page, RUN, DE, item)).status == "eligible"
    # A dry read, even for a mixed Simplified/Traditional page, does not select.
    before = await page.evaluate("window.fixtureClicks")
    assert not any(a.startswith(("save:", "checkbox:")) for a in before)
    await adapter.submit_once(page, RUN, DE, item)
    assert await adapter.confirm_submission(page, RUN, DE, item)
    with pytest.raises(InventoryContractError, match="只允许回读"):
        await adapter.submit_once(page, RUN, DE, item)
    actions = await page.evaluate("window.fixtureClicks")
    assert actions.count("save:SKU-GPSR") == 1
    assert actions.count("checkbox:SKU-GPSR") == 1


@pytest.mark.parametrize("save_label", ["保存更改", "删除", "送出", "未知"])
async def test_unknown_attestation_save_label_never_submits(page, save_label):
    await page.raw.set_content(HTML.replace('label="保存" disabled="true">保存',
                                           f'label="{save_label}" disabled="true">{save_label}'))
    adapter, candidates = await opened(page)
    with pytest.raises(InventoryContractError):
        await adapter.inspect_item(page, RUN, DE, candidates[0])
    assert not any(a.startswith(("save:", "checkbox:")) for a in await page.evaluate("window.fixtureClicks"))


async def test_same_tick_save_rejects_unrecognized_button_label(page):
    adapter, candidates = await opened(page)
    await adapter.inspect_item(page, RUN, DE, candidates[0])
    original = adapter._identity
    calls = 0
    async def changed_button(*args):
        nonlocal calls
        await original(*args)
        calls += 1
        if calls == 2:
            await page.evaluate("""document.querySelector('kat-panel kat-button[data-testid="primary-button"]')
                .setAttribute('label', '删除')""")
    adapter._identity = changed_button
    with pytest.raises(InventoryContractError, match="save_not_ready"):
        await adapter.submit_once(page, RUN, DE, candidates[0])
    assert not any(a.startswith("save:") for a in await page.evaluate("window.fixtureClicks"))


# Observed in the 2026-10-15 11:57 recording after the real save:
# completed GPSR view, short seven-day wording, declaration true, Close only.
async def render_observed_attestation_receipt(page, *, traditional=False):
    message = "您的提交正在審核中。七日內作出決定" if traditional else "您的提交正在审核中。七日内作出决定"
    heading = "GPSR 安全認證" if traditional else "GPSR 安全认证"
    await page.evaluate("""({message,heading})=>{
      const panel=document.querySelector('kat-panel');
      panel.innerHTML='<h2>'+heading+'</h2><div>ASIN：B000000001</div><div>SKU：SKU-GPSR</div>'+
        '<kat-card><div class="receipt"><p>'+message+'</p><b>'+heading+':</b><div>true</div></div></kat-card>'+
        '<kat-button data-testid="primary-button" label="關閉">關閉</kat-button>';
      panel.shadowRoot.querySelector('button').hidden=true;
      panel.querySelector('kat-button').onclick=()=>{
        window.fixtureClicks.push('close-attestation-receipt');panel.hidden=true;
      };
    }""", {"message":message,"heading":heading})


@pytest.mark.parametrize("traditional", [False, True])
async def test_observed_attestation_receipt_confirms_restarts_and_next_item_runs(page, traditional):
    adapter, candidates = await opened(page)
    item = candidates[0]
    await adapter.inspect_item(page, RUN, DE, item)
    await adapter.submit_once(page, RUN, DE, item)
    await render_observed_attestation_receipt(page, traditional=traditional)
    assert await adapter.confirm_submission(page, RUN, DE, item)
    recovered = AmazonInventoryGpsrPage(policy=POLICY)
    recovered._remember(page, page, RUN, DE)
    before = await page.evaluate("window.fixtureClicks")
    assert await recovered.reconcile_item(page, RUN, DE, item)
    assert await page.evaluate("window.fixtureClicks") == before
    state = await recovered._panel_snapshot(page, RUN, DE)
    assert not recovered._receipt_matches(page, replace(item, requirement_type="responsible_person"), state)
    await page.evaluate("document.querySelector('kat-panel').style.cssText='position:fixed;inset:0;z-index:1000;background:white'")
    assert (await recovered.inspect_item(page, RUN, DE, candidates[1])).status == "no_attestation"
    actions = await page.evaluate("window.fixtureClicks")
    assert actions.count("save:SKU-GPSR") == 1
    assert actions.count("close-attestation-receipt") == 1


@pytest.mark.parametrize("change", ["wrong_sku", "wrong_asin", "manufacturer", "person", "hidden",
                                    "example", "editing", "save_button", "false_declaration"])
async def test_observed_receipt_needs_current_completed_attestation_view(page, change):
    adapter, candidates = await opened(page)
    item = candidates[0]
    await adapter.inspect_item(page, RUN, DE, item)
    await render_observed_attestation_receipt(page)
    await page.evaluate("""change=>{
      const panel=document.querySelector('kat-panel');
      if(change==='wrong_sku')panel.innerHTML=panel.innerHTML.replace('SKU：SKU-GPSR','SKU：OTHER');
      if(change==='wrong_asin')panel.innerHTML=panel.innerHTML.replace('ASIN：B000000001','ASIN：B999999999');
      if(change==='manufacturer')panel.querySelector('h2').textContent='制造商信息';
      if(change==='person')panel.querySelector('h2').textContent='负责人信息';
      if(change==='hidden')panel.querySelector('kat-card').hidden=true;
      if(change==='example')panel.querySelector('p').textContent='提示：您的提交正在审核中。七日内作出决定（示例）';
      if(change==='editing')panel.insertAdjacentHTML('beforeend','<form><input type="checkbox" checked></form>');
      if(change==='save_button')panel.querySelector('kat-button').setAttribute('label','儲存');
      if(change==='false_declaration')panel.querySelector('.receipt>div').textContent='false';
    }""",change)
    adapter._timing = replace(POLICY, confirmation_timeout_seconds=.15, stable_seconds=.01)
    assert not await adapter.confirm_submission(page, RUN, DE, item)
    assert not any(a.startswith(("save:", "checkbox:")) for a in await page.evaluate("window.fixtureClicks"))


@pytest.mark.parametrize("selected,disabled", [(True,True),(True,False),(False,True)])
async def test_retained_declaration_check_is_read_only_and_not_a_receipt(page, selected, disabled):
    adapter, candidates = await opened(page)
    item = candidates[0]
    await adapter.inspect_item(page, RUN, DE, item)
    await page.evaluate("""({selected,disabled})=>{
      const panel=document.querySelector('kat-panel');
      const box=panel.querySelector('kat-checkbox');
      box.setAttribute('value',String(selected));
      box.shadowRoot.querySelector('input').checked=selected;
      panel.querySelector('kat-button').toggleAttribute('disabled',disabled);
    }""",{"selected":selected,"disabled":disabled})
    before = await page.evaluate("window.fixtureClicks")
    assert await adapter.confirmed_attestation_retained(page, RUN, DE, item) is (selected and disabled)
    assert await adapter.reconcile_item(page, RUN, DE, item) is False
    assert await page.evaluate("window.fixtureClicks") == before
