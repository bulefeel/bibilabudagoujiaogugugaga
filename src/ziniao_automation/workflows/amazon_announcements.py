"""Dismiss identified informational overlays, never business/authentication dialogs.

Only the inventory adapter enables this helper. Detection is read-only except
for a document-local click budget and locator markers; closing uses Playwright's
normal click (no force, Escape, DOM removal, navigation or network requests).
"""
from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .errors import DomContractError

logger = logging.getLogger(__name__)

# Exact headings, not arbitrary mentions in body text. Add new contracts here
# with a DOM test; never generalize this to every visible X/Cancel button.
TITLES = {
    "whats_new": ["有什么新功能", "有什麼新功能", "What's new", "What’s new", "新增功能",
                  "最新内容有哪些", "最新內容有哪些"],
    "product_updates": ["功能更新", "新功能介绍", "新功能介紹", "Product updates"],
    "promotion": ["推广活动", "推廣活動", "促销活动", "促銷活動", "Promotions"],
}

_PROBE = r"""(p) => { /* amazon:announcements */
const deep=(root,sel)=>{
  let out=[...root.querySelectorAll(sel)];
  if(root.shadowRoot) out.push(...deep(root.shadowRoot,sel));
  for(const e of root.querySelectorAll('*')) if(e.shadowRoot) out.push(...deep(e.shadowRoot,sel));
  return out;
};
const parent=e=>e.parentElement || e.getRootNode().host;
const visible=e=>{
  if(!e || !e.isConnected) return false;
  for(let n=e;n;n=parent(n)) {
    const s=getComputedStyle(n);
    if(n.hidden || n.getAttribute('aria-hidden')==='true' || s.display==='none' || s.visibility==='hidden') return false;
  }
  if([...e.getClientRects()].some(r=>r.width>0 && r.height>0)) return true;
  // KAT also uses zero-area inline hosts with fixed-position shadow dialogs.
  // A missing host box is not proof the overlay is hidden. The ancestor
  // hidden/display/visibility guards above still apply to every child.
  const renderedChildren=getComputedStyle(e).display==='contents' ||
    (e.matches('kat-modal,kat-dialog') && e.shadowRoot);
  return !!renderedChildren && [...(e.shadowRoot||e).children].some(visible);
};
const within=(a,b)=>{for(let n=b;n;n=parent(n)) if(n===a)return true;return false;};
const norm=s=>String(s||'').normalize('NFKC').trim().replace(/[?？!！:：]+$/,'').trim().toLowerCase();
const text=e=>(e.innerText||e.textContent||'').trim();
const key=Symbol.for('ziniao.announcements.v1');
const ledger=window[key] || (window[key]={count:0,pending:null});
const marked=token=>deep(document,'[data-ziniao-announcement]').filter(e=>
  e.getAttribute('data-ziniao-announcement')===token && visible(e));
if(p.mode==='closed') return {closed:marked(p.token).length===0};
if(ledger.pending) {
  if(marked(ledger.pending).length) return {status:'blocked',reason:'previous_close_unconfirmed'};
  ledger.pending=null;
}
const containers=deep(document,'kat-modal,kat-dialog,dialog,[role="dialog"],[aria-modal="true"],.a-popover-wrapper').filter(visible);
const headingSelector='h1,h2,h3,h4,h5,h6,[role="heading"],[slot="header"],[slot="title"],.a-popover-header';
const identify=e=>{
  const heads=deep(e,headingSelector).filter(visible).map(text);
  for(const attr of ['header','heading','title','aria-label']) if(e.hasAttribute(attr)) heads.push(e.getAttribute(attr));
  for(const id of (e.getAttribute('aria-labelledby')||'').split(/\s+/)) {
    const h=e.getRootNode().getElementById?.(id);
    if(h && visible(h) && within(e,h)) heads.push(text(h));
  }
  return Object.entries(p.titles).filter(([_,labels])=>heads.some(h=>labels.some(l=>norm(l)===norm(h)))).map(([k])=>k);
};
let matches=containers.map(e=>({e,kinds:identify(e)})).filter(x=>x.kinds.length);
// A custom-element host and its shadow dialog are the same overlay, not two.
matches=matches.filter(x=>!matches.some(y=>y!==x && within(y.e,x.e)));
if(!matches.length) return {status:'none'};
if(matches.length!==1 || matches[0].kinds.length!==1) return {status:'blocked',reason:'ambiguous_dialog'};
const {e,kinds}=matches[0];
const body=[text(e),...deep(e,'*').filter(visible).map(n=>n.shadowRoot?text(n.shadowRoot):'')].join(' ');
const protectedText=/验证码|驗證碼|一次性密[码碼]|密码|密碼|passkey|captcha|password|sign[ -]?in|log[ -]?in|安全证[明書]|安全證[明書]|负责人|負責人|请求付款|請求付款|确认付款|確認付款|disburse|attestation|responsible person|confirm payment|withdraw|身份验证|身分驗證/i;
const controls=deep(e,'form,input,textarea,select,kat-input,kat-checkbox,kat-radiobutton,[role="checkbox"],[role="radio"],[role="textbox"]').filter(visible);
const actionNames=new Set(['save','submit','confirm','accept','agree','保存','提交','送出','确认','確認','确定','確定','同意','接受']);
const businessAction=deep(e,'button,[role="button"],kat-button').filter(visible).some(n=>
  [n.getAttribute('label'),n.getAttribute('aria-label'),text(n)].some(s=>actionNames.has(norm(s))));
if(controls.length || protectedText.test(body) || businessAction) return {status:'blocked',reason:'business_or_auth_dialog'};
const closeNames=new Set(['close','dismiss','关闭','關閉','×','✕','✖','x']);
const isClose=n=>(n.localName==='kat-icon' && n.getAttribute('name')==='close') ||
  [n.getAttribute('aria-label'),n.getAttribute('title'),n.getAttribute('label'),text(n)]
    .some(s=>closeNames.has(norm(s)));
let buttons=deep(e,'button,[role="button"],kat-button,kat-icon[name="close"]').filter(visible).filter(isClose);
// The native button inside kat-button is one control, not an ambiguous pair.
buttons=buttons.filter(x=>!buttons.some(y=>y!==x && within(y,x)));
if(buttons.length>1) return {status:'blocked',reason:'ambiguous_close'};
if(!buttons.length) return {status:'loading',kind:kinds[0]};
const b=buttons[0];
if(b.disabled || b.getAttribute('disabled')==='true' || b.hasAttribute('disabled') || b.getAttribute('aria-disabled')==='true')
  return {status:'loading',kind:kinds[0]};
// Do not accidentally close a parent while a nested business dialog is active.
if(containers.some(other=>other!==e && within(e,other) && identify(other).length===0 &&
    deep(other,headingSelector).filter(visible).some(h=>text(h))))
  return {status:'blocked',reason:'nested_unknown_dialog'};
if(ledger.count>=p.maxCloses) return {status:'blocked',reason:'close_budget_exhausted'};
const signature=JSON.stringify([kinds[0],e.localName,b.localName,b.getAttribute('aria-label'),b.getAttribute('title')]);
if(p.mode==='arm') {
  if(signature!==p.signature) return {status:'changed'};
  ledger.count++;ledger.pending=p.token;
  e.setAttribute('data-ziniao-announcement',p.token);
  b.setAttribute('data-ziniao-announcement-close',p.token);
  return {status:'armed',kind:kinds[0]};
}
return {status:'ready',kind:kinds[0],signature};
}"""


class AnnouncementBlocked(DomContractError):
    code = "announcement_blocked"


async def dismiss_inventory_announcements(page, policy, *, cancel_check=None) -> int:
    """No overlay: immediate return. Known overlay: bounded, single-click close.

    The caller must validate the inventory origin/path first. A pending click
    stays latched in the document even if cancellation interrupts this function.
    """
    count, stable_key, stable_at = 0, None, 0.0

    async def probe(mode="inspect", **extra):
        return await page.evaluate(_PROBE, {
            "mode": mode, "titles": TITLES, "maxCloses": policy.announcement_max_closes, **extra,
        })

    try:
        async with asyncio.timeout(policy.announcement_timeout_seconds):
            while True:
                if cancel_check:
                    await cancel_check()
                state = await probe()
                if state["status"] == "none":
                    return count
                if state["status"] == "blocked":
                    logger.warning("amazon_announcement action=blocked reason=%s", state["reason"])
                    raise AnnouncementBlocked("公告弹窗结构不明确或关闭未完成，已保留页面；请手动关闭公告后重试")
                now = time.monotonic()
                signature = state.get("signature") if state["status"] == "ready" else None
                if signature != stable_key or signature is None:
                    stable_key, stable_at = signature, now
                if signature and now - stable_at >= policy.announcement_stable_seconds:
                    token = uuid4().hex
                    armed = await probe("arm", token=token, signature=signature)
                    if armed["status"] != "armed":
                        stable_key = None
                        continue
                    logger.info("amazon_announcement action=identified kind=%s", armed["kind"])
                    close = page.locator(f'[data-ziniao-announcement-close="{token}"]')
                    # No automatic second click if dispatch outcome is unknown.
                    await close.click(timeout=int(policy.click_timeout_seconds * 1000))
                    while not (await probe("closed", token=token))["closed"]:
                        if cancel_check:
                            await cancel_check()
                        await asyncio.sleep(policy.poll_seconds)
                    count += 1
                    logger.info("amazon_announcement action=closed kind=%s", armed["kind"])
                    stable_key = None
                await asyncio.sleep(policy.poll_seconds)
    except (TimeoutError, PlaywrightTimeoutError):
        logger.warning("amazon_announcement action=timeout")
        raise AnnouncementBlocked("公告弹窗识别或关闭超时，已保留页面，未连续重复点击") from None
