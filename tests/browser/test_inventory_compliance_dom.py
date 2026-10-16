"""Offline-only browser contracts for EU routing and separate requirements."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_inventory_gpsr_dom import page, renderer, BROWSER, RUN, DE, POLICY, opened
from ziniao_automation.workflows.amazon_inventory_gpsr.page import (
    AmazonInventoryGpsrPage, InventoryContractError, _PERSON_SAVE,
)
from ziniao_automation.workflows.amazon_inventory_gpsr.config import RESPONSIBLE_PERSON, INVENTORY_URL

pytestmark = [pytest.mark.asyncio(loop_scope='module'),
              pytest.mark.skipif(not BROWSER, reason='Requires offline fixture renderer')]
JS = (Path(__file__).parents[1] / 'fixtures/amazon_inventory_gpsr/compliance.js').read_text(encoding='utf-8')


async def install(page, **options):
    await page.evaluate(JS, options)
    original = page.evaluate
    async def evaluate(script, arg=None):
        if script == _PERSON_SAVE:
            return await page.raw.evaluate('(p)=>{const location=new URL(p.url);return ('+script+')(p.arg)}',
                                          {'url': page.url, 'arg': arg})
        return await original(script, arg)
    page.evaluate = evaluate
    return await opened(page)


@pytest.mark.parametrize('traditional', [False, True])
async def test_second_violation_is_safety_and_both_requirements_share_one_row_open(page, traditional):
    adapter, items = await install(page, traditional=traditional)
    assert (await adapter.inspect_item(page, RUN, DE, items[0])).status == 'eligible'
    person = replace(items[0], requirement_type=RESPONSIBLE_PERSON)
    assert (await adapter.inspect_item(page, RUN, DE, person)).status == 'eligible'
    actions = await page.evaluate('window.complianceClicks')
    assert actions.count('open:SKU-GPSR') == 1
    assert 'violation:1' in actions
    assert not any(a.startswith(('select-', 'send-', 'save-')) for a in actions)


@pytest.mark.parametrize('count,selected,invalid,status', [
    (1, True, False, 'already_linked'), (2, False, False, 'eligible'),
    (0, False, False, 'no_records'), (1, False, True, 'invalid_record'),
    (1, False, False, 'eligible'), (2, True, False, 'already_linked'),
])
async def test_person_initial_state_not_badge_or_enabled_submit(page, count, selected, invalid, status):
    adapter, items = await install(page, count=count, selected=selected, invalid=invalid)
    item = replace(items[0], requirement_type=RESPONSIBLE_PERSON)
    result = await adapter.inspect_item(page, RUN, DE, item)
    assert result.status == status
    assert result.initial_selected is selected
    assert not any(a.startswith(('select-', 'send-')) for a in await page.evaluate('window.complianceClicks'))


@pytest.mark.parametrize('receipt', [False, True])
async def test_person_sends_once_checked_state_alone_is_not_receipt(page, receipt):
    adapter, items = await install(page)
    item = replace(items[0], requirement_type=RESPONSIBLE_PERSON)
    assert (await adapter.inspect_item(page, RUN, DE, item)).status == 'eligible'
    await page.evaluate('window.noPersonReceipt='+str(not receipt).lower())
    await adapter.prepare_submission(page, RUN, DE, item)
    await adapter.submit_once(page, RUN, DE, item)
    assert await adapter.confirm_submission(page, RUN, DE, item) is receipt
    with pytest.raises(InventoryContractError):
        await adapter.submit_once(page, RUN, DE, item)
    assert (await page.evaluate('window.complianceClicks')).count('send-person') == 1
    if not receipt:
        assert await adapter.reconcile_item(page, RUN, DE, item) is False


@pytest.mark.parametrize('code,label', [('IT','義大利'),('FR','法國'),('ES','西班牙'),('PL','波蘭')])
async def test_marketplace_switch_uses_header_not_it_domain(page, code, label):
    page.url = INVENTORY_URL.replace('amazon.de','amazon.it')
    await page.evaluate('''({label})=>{
      const root=document.querySelector('#dropdown-account-switcher-container');
      root.insertAdjacentHTML('beforeend','<button class="dropdown-account-switcher-header">切换商城</button>');
      root.querySelector('button').onclick=()=>{
        const option=document.createElement('button');option.className='dropdown-account-switcher-list-item';
        option.title=label;option.textContent=label;root.append(option);
        option.onclick=()=>{root.querySelector('.dropdown-account-switcher-header-label-regional').textContent=label;option.remove();};
      };
    }''', {'label':label})
    adapter=AmazonInventoryGpsrPage(policy=POLICY)
    market=SimpleNamespace(code=code, domain='sellercentral.amazon.'+code.lower())
    await adapter.open_inventory(page, RUN, market)
    assert 'amazon.it' in page.url
    await adapter._identity(page, RUN)


async def test_zero_results_without_pagination_is_complete_empty_list(page):
    await page.evaluate("document.querySelector('#rows').innerHTML='';document.querySelector('kat-pagination').remove();document.body.insertAdjacentHTML('beforeend','<p>0 - 0 共 0 個</p>')")
    adapter, items = await opened(page)
    assert items == ()


@pytest.mark.parametrize('total',[225,230])
async def test_lazy_25_rows_per_batch_across_three_pages(page,total):
    await page.evaluate('''total=>{
      const rows=document.querySelector('#rows'),p=document.querySelector('kat-pagination');let number=1,loaded=0;
      window.lazyAdvance=()=>{const count=Math.min(100,total-(number-1)*100);const end=Math.min(count,loaded+25);
        for(let i=loaded;i<end;i++){const n=(number-1)*100+i;rows.insertAdjacentHTML('beforeend','<div data-sku="SKU-'+n+'"><a href="https://www.amazon.it/dp/B'+String(n).padStart(9,'0')+'">item</a></div>');}loaded=end;};
      function render(){rows.innerHTML='';loaded=0;p.setAttribute('page',number);p.setAttribute('total-items',total);window.lazyAdvance();}
      p.innerHTML='<button part="pagination-nav-right">next</button><button part="pagination-nav-left">previous</button>';
      p.querySelector('[part="pagination-nav-right"]').onclick=()=>{number++;render();};
      p.querySelector('[part="pagination-nav-left"]').onclick=()=>{number--;render();};render();
    }''',total)
    original=page.locator
    class Last:
        async def scroll_into_view_if_needed(self,**kwargs):await page.evaluate('window.lazyAdvance()')
    class Rows:
        last=Last()
    page.locator=lambda selector,*a,**kw:Rows() if selector=='[data-sku]' else original(selector,*a,**kw)
    adapter,items=await opened(page)
    assert len(items)==total and len({i.sku for i in items})==total
    assert [sum(i.page_number==n for i in items) for n in (1,2,3)]==[100,100,total-200]


async def test_cancel_after_person_selection_never_sends_or_counts_a_receipt(page):
    from ziniao_automation.workflows.amazon_inventory_gpsr.store import InventoryRunCancelled
    adapter, items = await install(page)
    item = replace(items[0], requirement_type=RESPONSIBLE_PERSON)
    assert (await adapter.inspect_item(page, RUN, DE, item)).status == 'eligible'
    async def cancel_after_select(run):
        if await page.evaluate("Array.from(document.querySelectorAll('input[type=radio]')).some(r=>r.checked)"):
            raise InventoryRunCancelled('fixture cancellation after selection')
    adapter.cancel_check = cancel_after_select
    with pytest.raises(InventoryRunCancelled):
        await adapter.prepare_submission(page, RUN, DE, item)
    assert 'send-person' not in await page.evaluate('window.complianceClicks')
    adapter.cancel_check = None
    assert not await adapter.confirm_submission(page, RUN, DE, item)
    with pytest.raises(InventoryContractError):
        await adapter.submit_once(page, RUN, DE, item)
    assert 'send-person' not in await page.evaluate('window.complianceClicks')


async def test_lazy_loading_without_progress_exits_instead_of_accepting_partial_page(page):
    await page.evaluate("document.querySelector('kat-pagination').setAttribute('total-items','100')")
    adapter = AmazonInventoryGpsrPage(policy=replace(POLICY, lazy_no_progress_seconds=.2,
                                                   lazy_page_timeout_seconds=1))
    with pytest.raises(InventoryContractError, match='不完整'):
        await adapter.open_inventory(page, RUN, DE)


@pytest.mark.parametrize('count,disabled,invalid,chosen', [(1,False,False,0), (3,False,False,0),
                                                        (3,True,False,1), (3,False,True,1)])
async def test_real_kat_radio_icon_selected_before_single_send(page,count,disabled,invalid,chosen):
    adapter, items = await install(page,traditional=True,katRadio=True,count=count,
                                   disabledFirst=disabled,invalidFirst=invalid)
    item=replace(items[0],requirement_type=RESPONSIBLE_PERSON)
    assert (await adapter.inspect_item(page,RUN,DE,item)).status=='eligible'
    # Regression reproduction: the native input is covered by the KAT icon.
    from playwright.async_api import TimeoutError as PwTimeout
    with pytest.raises(PwTimeout):
        await adapter._panel(page).locator('input[type="radio"]').nth(chosen).click(trial=True,timeout=80)
    await adapter.prepare_submission(page,RUN,DE,item)
    selected=await adapter._panel(page).locator('input[type="radio"]').evaluate_all('inputs=>inputs.map(x=>x.checked)')
    assert selected==[i==chosen for i in range(count)]
    actions=await page.evaluate('window.complianceClicks')
    assert actions.count('select-person')==1 and 'send-person' not in actions
    await adapter.submit_once(page,RUN,DE,item)
    assert await adapter.confirm_submission(page,RUN,DE,item)
    assert (await page.evaluate('window.complianceClicks')).count('send-person')==1


async def test_unselectable_record_does_not_create_prepared_submission(page):
    from ziniao_automation.workflows.amazon_inventory_gpsr.page import PersonSelectionError
    adapter,items=await install(page,katRadio=True,blockedSelection=True)
    adapter._timing=replace(POLICY,panel_timeout_seconds=.2,stable_seconds=.02)
    item=replace(items[0],requirement_type=RESPONSIBLE_PERSON)
    assert (await adapter.inspect_item(page,RUN,DE,item)).status=='eligible'
    with pytest.raises(PersonSelectionError):await adapter.prepare_submission(page,RUN,DE,item)
    with pytest.raises(InventoryContractError):await adapter.submit_once(page,RUN,DE,item)
    assert 'send-person' not in await page.evaluate('window.complianceClicks')


async def test_record_selection_change_after_preparation_never_sends(page):
    adapter,items=await install(page,katRadio=True,count=2)
    item=replace(items[0],requirement_type=RESPONSIBLE_PERSON)
    await adapter.inspect_item(page,RUN,DE,item)
    await adapter.prepare_submission(page,RUN,DE,item)
    await page.evaluate("document.querySelectorAll('kat-card input')[1].checked=true")
    with pytest.raises(InventoryContractError):await adapter.submit_once(page,RUN,DE,item)
    assert 'send-person' not in await page.evaluate('window.complianceClicks')


@pytest.mark.parametrize('traditional,primary_close', [(False,False),(True,False),(True,True)])
async def test_person_real_short_receipt_confirms_and_next_item_opens(page, traditional, primary_close):
    adapter, items = await install(page, traditional=traditional, personReceiptStyle='responsible')
    item = replace(items[0], requirement_type=RESPONSIBLE_PERSON)
    assert (await adapter.inspect_item(page, RUN, DE, item)).status == 'eligible'
    await adapter.prepare_submission(page, RUN, DE, item)
    await adapter.submit_once(page, RUN, DE, item)
    if primary_close:
        await page.evaluate("document.querySelector('kat-panel kat-button').setAttribute('data-testid','primary-button')")
    assert await adapter.confirm_submission(page, RUN, DE, item)
    assert await adapter.reconcile_item(page, RUN, DE, item)
    # A fresh adapter after restart must recognize the typed receipt as well.
    recovered = AmazonInventoryGpsrPage(policy=POLICY)
    recovered._remember(page, page, RUN, DE)
    assert await recovered.reconcile_item(page, RUN, DE, item)
    # Force an overlay to verify the footer Close recovery path.
    await page.evaluate("document.querySelector('kat-panel').style.cssText='position:fixed;inset:0;z-index:1000;background:white'")
    assert (await recovered.inspect_item(page, RUN, DE,
        replace(items[1], requirement_type=RESPONSIBLE_PERSON))).status == 'eligible'
    actions = await page.evaluate('window.complianceClicks')
    assert actions.count('send-person') == 1
    assert actions.count('close-person-receipt') == 1
    assert actions[-1].startswith('violation:')


async def test_person_receipt_is_not_safety_confirmation(page):
    adapter, items = await install(page, personInitiallyPending=True)
    person = replace(items[0], requirement_type=RESPONSIBLE_PERSON)
    assert (await adapter.inspect_item(page, RUN, DE, person)).status == 'already_submitted'
    snapshot = await adapter._panel_snapshot(page, RUN, DE)
    assert adapter._receipt_matches(page, person, snapshot)
    assert not adapter._receipt_matches(page, items[0], snapshot)


@pytest.mark.parametrize('change', ['wrong_sku', 'wrong_asin', 'manufacturer', 'hidden', 'notice', 'still_editable'])
async def test_person_short_receipt_requires_current_item_and_completed_person_view(page, change):
    adapter, items = await install(page, personInitiallyPending=True)
    person = replace(items[0], requirement_type=RESPONSIBLE_PERSON)
    assert (await adapter.inspect_item(page, RUN, DE, person)).status == 'already_submitted'
    adapter._timing = replace(POLICY, confirmation_timeout_seconds=.15, stable_seconds=.01)
    await page.evaluate('''change=>{
        const panel=document.querySelector('kat-panel');
        if(change==='wrong_sku')panel.innerHTML=panel.innerHTML.replace('SKU：SKU-GPSR','SKU：OTHER');
        if(change==='wrong_asin')document.querySelector('[data-sku="SKU-GPSR"] a').href='https://www.amazon.de/dp/B999999999';
        if(change==='manufacturer')panel.querySelector('h3').textContent='制造商信息';
        if(change==='hidden')panel.querySelector('p').style.display='none';
        if(change==='notice')panel.querySelector('p').textContent='提示：您的提交正在审核中。示例说明';
        if(change==='still_editable')panel.insertAdjacentHTML('beforeend','<kat-button>送出</kat-button>');
    }''',change)
    assert not await adapter.confirm_submission(page, RUN, DE, person)
    assert 'send-person' not in await page.evaluate('window.complianceClicks')


@pytest.mark.parametrize("first_type", ["safety_attestation", "responsible_person"])
async def test_completed_receipt_reopens_same_product_for_other_requirement(page, first_type):
    from test_inventory_gpsr_dom import render_observed_attestation_receipt
    adapter, items = await install(page, personReceiptStyle="responsible")
    first = replace(items[0], requirement_type=first_type)
    second_type = "responsible_person" if first_type == "safety_attestation" else "safety_attestation"
    second = replace(items[0], requirement_type=second_type)
    assert (await adapter.inspect_item(page, RUN, DE, first)).status == "eligible"
    await adapter.prepare_submission(page, RUN, DE, first)
    await adapter.submit_once(page, RUN, DE, first)
    if first_type == "safety_attestation":
        await render_observed_attestation_receipt(page)
    assert await adapter.confirm_submission(page, RUN, DE, first)
    assert (await adapter.inspect_item(page, RUN, DE, second)).status == "eligible"
    actions = await page.evaluate("window.complianceClicks")
    assert actions.count("open:SKU-GPSR") == 2
    assert actions.count("save-safety") + actions.count("send-person") == 1


async def test_two_similar_entries_person_and_manufacturer_are_not_two_people(page):
    adapter, items = await install(page, selected=True, traditional=True, manufacturerOnly=True)
    events = []
    async def record(run, item, action, details):
        events.append(action)
    adapter.on_item_event = record
    assert (await adapter.inspect_item(page, RUN, DE, items[0])).status == "no_attestation"
    person = replace(items[0], requirement_type=RESPONSIBLE_PERSON)
    assert (await adapter.inspect_item(page, RUN, DE, person)).status == "already_linked"
    actions = await page.evaluate("window.complianceClicks")
    assert "violation:0" in actions and "violation:1" in actions
    assert "manufacturer_skipped" in events
    assert "send-person" not in actions and "select-person" not in actions
