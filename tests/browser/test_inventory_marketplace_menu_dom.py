"""Offline regression for the account switcher's sibling flyout, not live Amazon."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock

import pytest

from test_inventory_gpsr_dom import page, renderer, BROWSER, RUN, POLICY
from ziniao_automation.workflows.amazon_inventory_gpsr.page import AmazonInventoryGpsrPage, InventoryContractError

pytestmark = [pytest.mark.asyncio(loop_scope='module'),
              pytest.mark.skipif(not BROWSER, reason='Requires offline fixture renderer')]
FR = SimpleNamespace(code='FR', domain='sellercentral.amazon.fr')
FAST = replace(POLICY, marketplace_menu_stable_seconds=.06,
               marketplace_menu_reopen_delay_seconds=.08)

MENU = r'''(p) => {
 const root=document.querySelector('#dropdown-account-switcher-container');
 const owner=document.createElement('div');owner.className='partner-switcher-container';
 owner.dataset.testTag='partner-switcher-container';root.before(owner);
 const wrap=document.createElement('div');wrap.className='flyoutTriggerWrap';owner.append(wrap);wrap.append(root);
 const header=document.createElement('button');header.className='dropdown-account-switcher-header';
 header.textContent='country';root.append(header);
 const menu=document.createElement('div');menu.className='flyoutFlyout';
 menu.style.display='none';(p.inline?root:owner).append(menu);
 window.menuClicks=[];let opens=0;
 const fill=()=>{
   menu.innerHTML='';
   if(p.group){const g=document.createElement('div');g.className='dropdown-account-switcher-list-item';
     g.title=p.wrongGroup?'another seller':'fixture seller';g.textContent=g.title;menu.append(g);}
   for(let i=0;i<(p.missing?0:(p.duplicate?2:1));i++){
     const opt=document.createElement('button');opt.className='dropdown-account-switcher-list-item';
     if(p.group)opt.classList.add('dropdown-account-switcher-list-item-indented');
     if(!p.textOnly)opt.title=p.label||'法国';opt.innerHTML='<span>'+(p.label||'法国')+'</span>';
     opt.disabled=!!p.disabled;menu.append(opt);
     opt.onclick=()=>{window.menuClicks.push('country');menu.style.display='none';
       setTimeout(()=>{if(!p.noChange)root.querySelector('.dropdown-account-switcher-header-label-regional').textContent=p.label||'法国';},p.switchDelay||0);};
   }
   if(p.missing)menu.innerHTML='<div class="dropdown-account-switcher-list-item" title="意大利">意大利</div>';
 };
 const show=()=>{
   opens++;menu.style.display='block';menu.innerHTML='<div role="progressbar">loading</div>';
   if(!p.neverLoad)setTimeout(fill,p.delay||0);
   if(p.closeAlways || (p.closeOnce && opens===1))setTimeout(()=>menu.style.display='none',p.closeAfter||20);
 };
 header.onclick=()=>{window.menuClicks.push('header');if(menu.style.display==='none')show();else menu.style.display='none';};
 if(p.open)show();
 if(p.unrelated){const other=document.createElement('div');other.className='partner-switcher-container';
   other.innerHTML='<button class="dropdown-account-switcher-list-item" title="法国">法国</button>';document.body.append(other);}
}'''


async def test_sibling_flyout_target_is_not_a_descendant_of_header(page):
    await page.evaluate(MENU, {})
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    await adapter._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['header', 'country']
    assert await page.locator('#dropdown-account-switcher-container .dropdown-account-switcher-list-item').count() == 0


async def test_captured_real_menu_groups_are_bound_to_the_current_seller(page):
    html = (Path(__file__).parents[1] / 'fixtures/amazon_inventory_gpsr/marketplace-menu.html').read_text(encoding='utf8')
    await page.evaluate('''html=>{
      document.querySelector('#dropdown-account-switcher-container').innerHTML=html;
      window.menuClicks=[];
      document.querySelector('.dropdown-account-switcher-list-item[title="意大利"]').onclick=()=>{
        window.menuClicks.push('country');
        document.querySelector('.dropdown-account-switcher-header-label-regional').textContent='意大利';
        document.querySelector('.dropdown-account-switcher-list-pointer-area').remove();
      };
    }''', html)
    await AmazonInventoryGpsrPage(policy=FAST)._switch_marketplace(
        page, RUN, SimpleNamespace(code='IT', domain='sellercentral.amazon.it'))
    assert await page.evaluate('window.menuClicks') == ['country']


@pytest.mark.parametrize('label', ['法国', '法國', 'France'])
async def test_loaded_menu_collapses_before_stability_then_recovers_once(page, label):
    # Recording: a real target is briefly visible, then the menu collapses.
    await page.evaluate(MENU, {'inline': True, 'closeOnce': True, 'label': label, 'group': True})
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    events = []
    async def event(run, market, action, details): events.append((action, details))
    adapter.on_marketplace_event = event
    await adapter._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['header', 'header', 'country']
    assert [a for a, _ in events] == ['opened', 'reopened', 'clicked', 'confirmed']
    assert events[1][1]['reopens'] == 1


async def test_already_open_menu_loading_is_not_toggled_closed(page):
    await page.evaluate(MENU, {'open': True, 'inline': True, 'delay': 150, 'group': True})
    await AmazonInventoryGpsrPage(policy=FAST)._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['country']


async def test_preopened_collapsing_menu_gets_only_one_reopen(page):
    await page.evaluate(MENU, {'open': True, 'closeAlways': True, 'closeAfter': 80})
    with pytest.raises(InventoryContractError, match='收起'):
        await AmazonInventoryGpsrPage(policy=FAST)._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['header']


async def test_already_target_country_never_opens_menu(page):
    await page.evaluate(MENU, {})
    await page.evaluate("document.querySelector('.dropdown-account-switcher-header-label-regional').textContent='法国'")
    await AmazonInventoryGpsrPage(policy=FAST)._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == []


async def test_exact_translated_label_and_unrelated_owner_cannot_duplicate_target(page):
    await page.evaluate(MENU, {'textOnly': True, 'label': '法國', 'unrelated': True})
    await AmazonInventoryGpsrPage(policy=FAST)._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['header', 'country']


@pytest.mark.parametrize('options,reason,opens', [
    ({'closeAlways': True}, '收起', 2),
    ({'duplicate': True}, '不唯一', 1),
    ({'disabled': True}, '不可操作', 1),
    ({'missing': True}, '未取得', 1),
    ({'neverLoad': True}, '加载超时', 1),
    ({'group': True, 'wrongGroup': True}, '账号组', 1),
])
async def test_menu_failures_are_classified_and_never_select(page, options, reason, opens):
    await page.evaluate(MENU, options)
    policy = replace(FAST, list_timeout_seconds=.7)
    with pytest.raises(InventoryContractError, match=reason):
        await AmazonInventoryGpsrPage(policy=policy)._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['header'] * opens


async def test_delayed_country_switch_is_not_clicked_again(page):
    await page.evaluate(MENU, {'switchDelay': 160})
    await AmazonInventoryGpsrPage(policy=FAST)._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['header', 'country']


async def test_no_country_change_after_click_never_reopens_or_reclicks(page):
    await page.evaluate(MENU, {'noChange': True})
    with pytest.raises(InventoryContractError, match='商城切换未确认'):
        await AmazonInventoryGpsrPage(policy=replace(FAST, list_timeout_seconds=.7))._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['header', 'country']


async def test_cancellation_after_menu_open_does_not_select(page):
    from ziniao_automation.workflows.amazon_inventory_gpsr.store import InventoryRunCancelled
    await page.evaluate(MENU, {})
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    async def cancelled():
        if await page.evaluate('window.menuClicks.length'): raise InventoryRunCancelled('cancelled')
    adapter._check_cancelled = cancelled
    with pytest.raises(InventoryRunCancelled): await adapter._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['header']


async def test_hung_menu_dom_read_ends_at_shared_deadline(page):
    await page.evaluate(MENU, {})
    adapter = AmazonInventoryGpsrPage(policy=replace(FAST, list_timeout_seconds=.3))
    original = adapter._evaluate
    async def evaluate(page, script, arg=None):
        if 'inventory:marketplace-menu' in script: await asyncio.Event().wait()
        return await original(page, script, arg)
    adapter._evaluate = evaluate
    with pytest.raises(InventoryContractError, match='页头尚未就绪'):
        await asyncio.wait_for(adapter._switch_marketplace(page, RUN, FR), 1.5)
    assert await page.evaluate('window.menuClicks') == []


async def test_reopening_can_be_disabled_by_policy(page):
    await page.evaluate(MENU, {'closeAlways': True})
    with pytest.raises(InventoryContractError, match='收起'):
        await AmazonInventoryGpsrPage(policy=replace(FAST, marketplace_menu_reopen_attempts=0))._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['header']


async def test_identity_change_after_open_stops_before_country_click(page):
    await page.evaluate(MENU, {})
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    async def event(run, market, action, details):
        if action == 'opened':
            await page.evaluate("document.querySelector('.dropdown-account-switcher-header-label-global').textContent='different seller'")
    adapter.on_marketplace_event = event
    with pytest.raises(InventoryContractError, match='身份'):
        await adapter._switch_marketplace(page, RUN, FR)
    assert await page.evaluate('window.menuClicks') == ['header']


async def test_switch_invokes_existing_login_once_then_rechecks_country(page):
    await page.evaluate(MENU, {'noChange': True})
    adapter = AmazonInventoryGpsrPage(policy=FAST)
    original = adapter._choose_marketplace
    async def choose(*args):
        reopens = await original(*args)
        page.url = 'https://sellercentral.amazon.de/ap/signin'
        return reopens
    adapter._choose_marketplace = choose
    async def login(current, **kwargs):
        await page.evaluate("document.querySelector('.dropdown-account-switcher-header-label-regional').textContent='法国'")
        page.url = 'https://sellercentral.amazon.de/myinventory/inventory'
        return page
    adapter.login.advance_or_raise = AsyncMock(side_effect=login)
    await adapter._switch_marketplace(page, RUN, FR)
    assert adapter.login.advance_or_raise.await_count == 1
    assert await page.evaluate('window.menuClicks') == ['header', 'country']
