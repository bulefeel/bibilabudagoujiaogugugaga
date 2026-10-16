"""Offline DOM only: announcements cannot trigger auth or business actions."""
import asyncio
from dataclasses import replace

import pytest
import pytest_asyncio

from test_inventory_gpsr_dom import page, renderer, BROWSER, POLICY, RUN, DE
from test_setup_inventory_dom import subject, site, STORE
from ziniao_automation.workflows.amazon_announcements import (
    AnnouncementBlocked, dismiss_inventory_announcements,
)
from ziniao_automation.workflows.amazon_inventory_gpsr.page import AmazonInventoryGpsrPage

pytestmark = [pytest.mark.asyncio(loop_scope='module'),
              pytest.mark.skipif(not BROWSER, reason='Requires offline fixture renderer')]

FAST = replace(POLICY, announcement_timeout_seconds=.6, announcement_stable_seconds=.03)


@pytest_asyncio.fixture(autouse=True, loop_scope='module')
async def reset_document_ledger(page):
    # set_content reuses about:blank's JS context; a real navigation does not.
    await page.evaluate("delete window[Symbol.for('ziniao.announcements.v1')]")


async def install(page, title='有什麼新功能？', *, body='Manage All Inventory 新功能介绍',
                  close='×', shadow=False, remove=True, extra='', delay=0):
    await page.evaluate(r'''p=>{
      window.announcementClicks=[];window.otherClicks=[];
      document.body.insertAdjacentHTML('beforeend',
        '<button id="background-x" aria-label="Close">×</button>');
      document.querySelector('#background-x').onclick=()=>window.otherClicks.push('background');
      const create=()=>{
        const modal=document.createElement(p.shadow?'kat-modal':'div');
        modal.id='notice';modal.setAttribute('role','dialog');
        modal.style.cssText='position:fixed;inset:30px;background:white;z-index:99999';
        const root=p.shadow?modal.attachShadow({mode:'open'}):modal;
        root.innerHTML='<h2></h2><p></p><button id="dismiss"></button>'+p.extra;
        root.querySelector('h2').textContent=p.title;
        root.querySelector('p').textContent=p.body;
        root.querySelector('#dismiss').textContent=p.close;
        root.querySelector('#dismiss').onclick=()=>{
          window.announcementClicks.push(p.title);
          if(p.remove)modal.remove();
        };
        for(const b of root.querySelectorAll('button:not(#dismiss)'))
          b.onclick=()=>window.otherClicks.push('business');
        document.body.append(modal);
      };
      if(p.delay)setTimeout(create,p.delay);else create();
    }''', dict(title=title, body=body, close=close, shadow=shadow, remove=remove, extra=extra, delay=delay))


@pytest.mark.parametrize('title', ['有什麼新功能？','有什么新功能?','What’s new','功能更新','推廣活動','最新内容有哪些？','最新內容有哪些？'])
@pytest.mark.parametrize('shadow', [False, True])
async def test_known_announcement_closes_once_without_background_or_business_click(page,title,shadow):
    await install(page,title,shadow=shadow)
    assert await dismiss_inventory_announcements(page,FAST)==1
    assert await dismiss_inventory_announcements(page,FAST)==0
    assert await page.evaluate('window.announcementClicks')==[title]
    assert await page.evaluate('window.otherClicks')==[]


async def test_kat_header_attribute_and_close_icon(page):
    await page.evaluate('''()=>{
      window.noticeClosedCount=0;
      const m=document.createElement('kat-modal');m.setAttribute('header','有什麼新功能？');
      document.body.append(m);
      m.attachShadow({mode:'open'}).innerHTML='<div role="dialog"><kat-icon name="close" style="display:block">×</kat-icon><p>功能介绍</p></div>';
      m.shadowRoot.querySelector('kat-icon').onclick=()=>{window.noticeClosedCount++;m.remove();};
    }''')
    assert await dismiss_inventory_announcements(page,FAST)==1
    assert await page.evaluate('window.noticeClosedCount')==1


async def test_contents_shadow_host_and_zero_sized_inactive_modal(page):
    await install(page,shadow=True)
    await page.evaluate('''()=>{
      const m=document.querySelector('#notice');m.style.display='contents';
      m.shadowRoot.querySelector('h2').style.display='block';
      document.body.insertAdjacentHTML('beforeend',
        '<kat-modal header="有什麼新功能？" style="display:block;width:0;height:0"></kat-modal>');
    }''')
    assert await dismiss_inventory_announcements(page,FAST)==1


@pytest.mark.parametrize('title', ['登录','请求付款','安全证明','未知弹窗'])
async def test_unregistered_dialog_is_not_closed(page,title):
    await install(page,title)
    assert await dismiss_inventory_announcements(page,FAST)==0
    assert await page.evaluate('window.announcementClicks')==[]


@pytest.mark.parametrize('body,extra', [
    ('请输入验证码',''), ('请确认付款',''), ('安全证明声明',''),
    ('News','<input type="password">'), ('News','<input type="checkbox">'),
    ('News','<div role="dialog"><h3>提交声明</h3><button>保存</button></div>'),
    ('News','<button>Agree</button>'),
])
async def test_known_title_does_not_override_authentication_or_form_controls(page,body,extra):
    await install(page,body=body,extra=extra)
    with pytest.raises(AnnouncementBlocked):
        await dismiss_inventory_announcements(page,FAST)
    assert await page.evaluate('window.announcementClicks')==[]
    assert await page.evaluate('window.otherClicks')==[]


@pytest.mark.parametrize('extra', ['<button aria-label="Close">×</button>',''])
async def test_ambiguous_close_or_dialog_never_clicks(page,extra):
    await install(page,extra=extra)
    if not extra:
        await page.evaluate("document.body.append(document.querySelector('#notice').cloneNode(true))")
    with pytest.raises(AnnouncementBlocked):
        await dismiss_inventory_announcements(page,FAST)
    assert await page.evaluate('window.announcementClicks')==[]


async def test_hidden_announcement_not_closed(page):
    await install(page)
    await page.evaluate("document.querySelector('#notice').style.display='none'")
    assert await dismiss_inventory_announcements(page,FAST)==0
    assert await page.evaluate('window.announcementClicks')==[]


async def test_close_failure_is_bounded_and_never_reclicked_on_later_probe(page):
    await install(page,remove=False)
    with pytest.raises(AnnouncementBlocked):
        await dismiss_inventory_announcements(page,FAST)
    with pytest.raises(AnnouncementBlocked):
        await dismiss_inventory_announcements(page,FAST)
    assert len(await page.evaluate('window.announcementClicks'))==1


async def test_document_close_budget_survives_multiple_checks(page):
    for _ in range(FAST.announcement_max_closes):
        await install(page)
        assert await dismiss_inventory_announcements(page,FAST)==1
    await install(page)
    with pytest.raises(AnnouncementBlocked):
        await dismiss_inventory_announcements(page,FAST)
    assert await page.evaluate('window.announcementClicks')==[]


async def test_delayed_close_control_waits_for_recognized_dialog(page):
    await install(page)
    await page.evaluate("const b=document.querySelector('#dismiss');b.hidden=true;setTimeout(()=>b.hidden=false,50)")
    assert await dismiss_inventory_announcements(page,FAST)==1


async def test_inventory_dry_run_closes_late_announcement_without_business_submit(page):
    adapter=AmazonInventoryGpsrPage(policy=FAST)
    await adapter.open_inventory(page,RUN,DE)
    await install(page,delay=20)
    await asyncio.sleep(.06)
    items=await adapter.scan_candidates(page,RUN,DE)
    assert len(items)==3
    result=await adapter.inspect_item(page,RUN,DE,items[0])
    assert result.status=='eligible'
    assert len(await page.evaluate('window.announcementClicks'))==1
    assert not any(x.startswith(('checkbox:','save:')) for x in await page.evaluate('window.fixtureClicks'))


async def test_setup_reuses_shared_guard_and_reads_identity_without_scanning(page):
    adapter=subject(page)
    adapter.inventory._timing=FAST
    await install(page)
    identity,_=await adapter.detect_identity(page,site('DE'))
    assert identity=='fixture seller'
    assert (await adapter.check_site(page,STORE,site('DE'))).payment_capability=='NOT_CHECKED'
    assert len(await page.evaluate('window.announcementClicks'))==1
    assert await page.evaluate('window.fixtureClicks')==[]


@pytest.mark.parametrize('url', [
    'https://sellercentral.amazon.de/ap/signin',
    'https://sellercentral.amazon.de/payments/disburse/details',
    'https://sellercentral.amazon.de.evil.test/myinventory/inventory',
    'https://sellercentral.amazon.de/myinventory/inventory/',
])
async def test_integration_does_not_enable_closer_on_other_routes(page,url):
    await install(page)
    page.url=url
    await AmazonInventoryGpsrPage(policy=FAST)._dismiss_announcements(page)
    assert await page.evaluate('window.announcementClicks')==[]


async def test_before_click_rechecks_popup_arriving_during_pacing(page):
    adapter=AmazonInventoryGpsrPage(policy=replace(FAST,action_interval_seconds=.08))
    await install(page,delay=20)
    class Locator:
        def __init__(self):self.page=page
        async def count(self):return 1
        async def click(self,**kwargs):
            assert await page.raw.locator('#notice').count()==0
    await adapter._click_unique(Locator())
    assert len(await page.evaluate('window.announcementClicks'))==1


async def test_cancel_after_arm_does_not_replay_close(page):
    await install(page,remove=False)
    task=asyncio.create_task(dismiss_inventory_announcements(page,FAST))
    try:
        await page.raw.wait_for_function('window.announcementClicks.length===1',timeout=2000)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        with pytest.raises(AnnouncementBlocked):await dismiss_inventory_announcements(page,FAST)
        assert len(await page.evaluate('window.announcementClicks'))==1
    finally:
        if not task.done():task.cancel()


async def test_hung_dom_request_has_deadline_and_no_click():
    class HungPage:
        async def evaluate(self,*args):await asyncio.Future()
        def locator(self,*args):raise AssertionError('No click after hung probe')
    with pytest.raises(AnnouncementBlocked):
        await dismiss_inventory_announcements(HungPage(),replace(FAST,announcement_timeout_seconds=.06))


@pytest.mark.parametrize('title', ['最新内容有哪些？', '最新內容有哪些？'])
async def test_zero_sized_inline_kat_modal_uses_rendered_shadow_dialog(page, title):
    # Captured KAT contract: inline host is 0x0, but its fixed shadow dialog
    # and slotted heading are visible. Closing hides, rather than removes, it.
    await page.evaluate(r'''p => {
      window.announcementClicks=[];window.otherClicks=[];
      const m=document.createElement('kat-modal');
      m.id='tour';m.setAttribute('role','dialog');m.setAttribute('visible','true');
      m.style.cssText='display:inline';
      m.innerHTML='<span slot="title"></span><p>商品发布、亚马逊物流和定价工具链接已移动位置</p>';
      m.querySelector('span').textContent=p;
      m.attachShadow({mode:'open'}).innerHTML=`
        <style>:host(:not([visible="true"])){visibility:hidden}</style>
        <div style="position:fixed;inset:35px;background:white;z-index:9999">
          <div part="modal-header"><div id="title"><slot name="title"></slot></div>
          <button part="modal-close-button" aria-label="关闭"><kat-icon name="exit"></kat-icon></button></div>
          <slot></slot>
        </div>`;
      m.shadowRoot.querySelector('button').onclick=()=>{
        window.announcementClicks.push(p);m.removeAttribute('visible');
      };
      document.body.append(m);
      const background=document.createElement('button');
      background.textContent='保存';background.onclick=()=>window.otherClicks.push('save');
      document.body.append(background);
    }''', title)
    assert await page.evaluate("""() => {
      const r=document.querySelector('#tour').getBoundingClientRect();
      return r.width===0 || r.height===0;
    }""")
    assert await dismiss_inventory_announcements(page, FAST)==1
    assert await dismiss_inventory_announcements(page, FAST)==0
    assert await page.evaluate('window.announcementClicks')==[title]
    assert await page.evaluate('window.otherClicks')==[]


async def test_hidden_inline_kat_host_with_shadow_box_is_not_closed(page):
    await install(page, shadow=True)
    await page.evaluate("""() => {
      const m=document.querySelector('#notice');
      m.style.cssText='display:inline;visibility:hidden';
      m.shadowRoot.querySelector('h2').style.cssText='position:fixed;inset:30px';
    }""")
    assert await dismiss_inventory_announcements(page, FAST)==0
    assert await page.evaluate('window.announcementClicks')==[]
