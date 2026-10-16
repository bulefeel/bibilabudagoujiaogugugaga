"""Offline UI-only history lookup and cancellation receipt drain."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from tests.browser.test_inventory_gpsr_dom import page, renderer, opened, RUN, DE, POLICY, pytestmark
from ziniao_automation.workflows.amazon_inventory_gpsr.config import INVENTORY_URL
from ziniao_automation.workflows.amazon_inventory_gpsr.page import InventoryContractError, ItemLoadError
from ziniao_automation.workflows.amazon_inventory_gpsr.store import InventoryRunCancelled

async def historical_listing(page, *, review=True, missing=False, stale=False):
    adapter, items = await opened(page)
    await page.evaluate("""({review,missing,stale})=>{
      const status=document.querySelector('kat-dropdown');
      status.id='status';
      status.innerHTML='<span>商品状态</span><kat-option value="ApprovalRequired">需要批准</kat-option><kat-option value="All">全部</kat-option>';
      for(const o of status.querySelectorAll('kat-option'))o.hidden=true;
      status.onclick=e=>{if(e.target.closest('kat-option'))return;for(const o of status.querySelectorAll('kat-option'))o.hidden=false;};
      const input=document.querySelector('kat-input-group kat-input').shadowRoot.querySelector('input');
      const render=()=>{
        let total=0;
        for(const row of document.querySelectorAll('[data-sku]')){
          row.hidden=(!!input.value && row.dataset.sku!==input.value) ||
            (row.dataset.sku==='SKU-GPSR' && (status.getAttribute('value')==='ApprovalRequired'||missing));
          if(!row.hidden)total++;
        }
        document.querySelector('kat-pagination').setAttribute('total-items',total);
      };
      input.onkeydown=e=>{if(e.key==='Enter')render();};
      for(const option of status.querySelectorAll('kat-option'))option.onclick=()=>{
        status.setAttribute('value',option.getAttribute('value'));
        for(const o of status.querySelectorAll('kat-option'))o.hidden=true;
        window.fixtureClicks.push('filter:'+option.getAttribute('value'));
        if(!stale){
          const spinner=document.createElement('kat-spinner');document.body.append(spinner);
          setTimeout(()=>{render();spinner.remove();},80);
        }
      };
      const row=document.querySelector('[data-sku="SKU-GPSR"]');
      const link=row.querySelector('kat-link'), original=link.onclick;
      link.onclick=()=>{
        original();
        if(review){
          const violation=document.querySelector('#violation'), click=violation.onclick;
          violation.onclick=()=>{
            click();document.querySelector('#card').textContent='安全证明 审核中';
          };
        }
      };
      render();
    }""", dict(review=review, missing=missing, stale=stale))
    return adapter, items[0]

@pytest.mark.parametrize("review", [True, False])
async def test_missing_approval_item_never_expands_to_all_inventory(page, review):
    adapter, item = await historical_listing(page, review=review)
    assert not await adapter.reconcile_item(page, RUN, DE, item)
    actions = await page.evaluate('window.fixtureClicks')
    assert not any(x.startswith(('filter:', 'open:', 'checkbox:', 'save:')) for x in actions)
    assert not adapter._history_readonly

async def test_all_listing_absence_is_not_a_receipt(page):
    adapter, item = await historical_listing(page, missing=True)
    assert not await adapter.reconcile_item(page, RUN, DE, item)
    assert not any(x.startswith(('open:', 'save:')) for x in await page.evaluate('window.fixtureClicks'))

async def test_filter_helper_does_not_accept_stale_results(page):
    adapter, item = await historical_listing(page, stale=True)
    adapter._timing = replace(POLICY, list_timeout_seconds=.3)
    with pytest.raises(ItemLoadError):
        await adapter._select_inventory_status(page, all_items=True)
    assert not any(x.startswith(('open:', 'save:')) for x in await page.evaluate('window.fixtureClicks'))

async def test_current_lookup_does_not_reconcile_another_asin(page):
    adapter, candidates = await opened(page)
    await page.evaluate("""()=>{
      document.querySelector('[data-sku="SKU-GPSR"] a').href='https://www.amazon.de/dp/B000000099';
    }""")
    with pytest.raises(InventoryContractError, match='ASIN'):
        await adapter.reconcile_item(page, RUN, DE, candidates[0])
    assert not any(x.startswith(('open:', 'save:')) for x in await page.evaluate('window.fixtureClicks'))

@pytest.mark.parametrize("method", ["prepare_submission", "submit_once"])
async def test_history_scope_rejects_all_write_entrypoints(page, method):
    adapter, items = await opened(page)
    await adapter.inspect_item(page, RUN, DE, items[0])
    before = await page.evaluate('window.fixtureClicks')
    adapter._history_readonly = True
    with pytest.raises(InventoryContractError, match='仅允许读取'):
        await getattr(adapter, method)(page, RUN, DE, items[0])
    assert await page.evaluate('window.fixtureClicks') == before

@pytest.mark.parametrize("receipt", [True, False])
async def test_cancel_during_confirmation_only_reads_remaining_receipt(page, receipt):
    adapter, items = await opened(page)
    item = items[0]
    await adapter.inspect_item(page, RUN, DE, item)
    if receipt:
        await adapter.submit_once(page, RUN, DE, item)
    before = await page.evaluate('window.fixtureClicks')
    async def cancelled(run):
        raise InventoryRunCancelled('fixture cancelled')
    adapter.cancel_check = cancelled
    adapter._timing = replace(POLICY, confirmation_timeout_seconds=.3)
    assert await adapter.confirm_submission(page, RUN, DE, item) is receipt
    assert await page.evaluate('window.fixtureClicks') == before
    with pytest.raises(InventoryRunCancelled):
        await adapter._check_cancelled()

@pytest.mark.parametrize("change", ["login", "seller", "marketplace", "closed", "hanging"])
async def test_cancelled_readback_never_logs_in_or_changes_page(page, change):
    adapter, items = await opened(page)
    await adapter.inspect_item(page, RUN, DE, items[0])
    await adapter.submit_once(page, RUN, DE, items[0])
    before = await page.evaluate('window.fixtureClicks')
    async def cancelled(run):
        raise InventoryRunCancelled('fixture cancelled')
    adapter.cancel_check = cancelled
    adapter._timing = replace(POLICY, confirmation_timeout_seconds=.3)
    if change == 'login':
        page.url = 'https://sellercentral.amazon.de/ap/signin'
    elif change == 'seller':
        await page.evaluate("document.querySelector('.dropdown-account-switcher-header-label-global').textContent='other'")
    elif change == 'marketplace':
        await page.evaluate("document.querySelector('.dropdown-account-switcher-header-label-regional').textContent='法国'")
    elif change in {'hanging','closed'}:
        original = page.evaluate
        async def broken(*args):
            if change == 'closed': raise RuntimeError('Target closed')
            await asyncio.sleep(10)
        page.evaluate = broken
    adapter.login.advance_or_raise = AsyncMock(side_effect=AssertionError('no login after cancellation'))
    try:
        if change == 'hanging':
            assert await asyncio.wait_for(adapter.confirm_submission(page, RUN, DE, items[0]),1) is False
        else:
            with pytest.raises((InventoryContractError, RuntimeError)):
                await adapter.confirm_submission(page, RUN, DE, items[0])
    finally:
        if change in {'hanging','closed'}: page.evaluate = original
        page.url = INVENTORY_URL
    assert await page.evaluate('window.fixtureClicks') == before
    adapter.login.advance_or_raise.assert_not_called()

async def test_all_read_filter_is_independent_of_fulfillment_all_and_normal_scan_stays_strict(page):
    adapter, item = await historical_listing(page)
    await page.evaluate("""()=>{
      const other=document.createElement('kat-dropdown');other.setAttribute('value','All');
      other.textContent='全部配送渠道';document.body.append(other);
    }""")
    assert not await adapter.reconcile_item(page, RUN, DE, item)
    await adapter._select_inventory_status(page, all_items=True)
    with pytest.raises(InventoryContractError, match='需要批准'):
        await adapter._wait_list(page)

async def test_missing_all_option_does_not_guess_or_open_another_page(page):
    adapter, item = await historical_listing(page)
    await page.evaluate("document.querySelector('#status [value=All]').textContent='未知筛选'")
    with pytest.raises(InventoryContractError, match='选项'):
        await adapter._select_inventory_status(page, all_items=True)
    assert not any(x.startswith(('open:', 'save:', 'filter:All')) for x in await page.evaluate('window.fixtureClicks'))

async def test_cancel_readback_accepts_delayed_receipt_but_no_additional_clicks(page):
    adapter, items = await opened(page)
    item = items[0]
    await adapter.inspect_item(page, RUN, DE, item)
    await adapter.submit_once(page, RUN, DE, item)
    await page.evaluate("""()=>{
      const p=document.querySelector('kat-panel'), html=p.innerHTML;
      p.innerHTML='<div>SKU：SKU-GPSR</div><div>ASIN：B000000001</div><kat-spinner></kat-spinner>';
      setTimeout(()=>p.innerHTML=html,150);
    }""")
    before = await page.evaluate('window.fixtureClicks')
    async def cancelled(run): raise InventoryRunCancelled('cancelled')
    adapter.cancel_check = cancelled
    adapter._timing = replace(POLICY, confirmation_timeout_seconds=.5)
    assert await adapter.confirm_submission(page, RUN, DE, item)
    assert await page.evaluate('window.fixtureClicks') == before

async def test_cancel_readback_does_not_accept_other_product_receipt(page):
    adapter, items = await opened(page)
    await adapter.inspect_item(page, RUN, DE, items[0])
    await adapter.submit_once(page, RUN, DE, items[0])
    async def cancelled(run): raise InventoryRunCancelled('cancelled')
    adapter.cancel_check = cancelled
    adapter._timing = replace(POLICY, confirmation_timeout_seconds=.3)
    assert not await adapter.confirm_submission(page, RUN, DE, items[1])


@pytest.mark.parametrize("selected", [True, False])
async def test_attestation_inspection_preserves_initial_checkbox_state(page, selected):
    adapter, candidates = await opened(page)
    result = await adapter.inspect_item(page, RUN, DE, candidates[0])
    assert result.initial_selected is False
    await page.evaluate("""selected=>{
      const box=document.querySelector('kat-panel kat-checkbox');
      box.setAttribute('value',selected?'true':'false');
      box.shadowRoot.querySelector('input').checked=selected;
    }""", selected)
    state = await adapter._panel_snapshot(page, RUN, DE)
    before = await page.evaluate('window.fixtureClicks')
    result = await adapter._inspect_loaded_panel(page, RUN, DE, candidates[0], state)
    assert result.status == "eligible" and result.initial_selected is selected
    assert await page.evaluate('window.fixtureClicks') == before
