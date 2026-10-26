"""Inventory-specific sidebars; all writes are scoped to one verified SKU/ASIN."""

from __future__ import annotations

import asyncio
import json
import math
import logging
import re
import time
from typing import Any, Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from weakref import WeakKeyDictionary, ref
from uuid import uuid4

from ..amazon_landing import AmazonLanding
from ..amazon_announcements import AnnouncementBlocked, dismiss_inventory_announcements
from ..amazon_login import AmazonLoginAdvancer
from ..errors import DomContractError, HumanAuthRequired, PageRecoveryRequired, PreflightRejected, WorkflowError
from .http_diagnostic import is_csrf_header_loss
from .store import InventoryRunCancelled
from .marketplace_menu import HEADER_ROOT, HEADER, ITEM, OWNER_XPATH, MENU_BODY, ERRORS as MENU_ERRORS
from .config import (
    DECLARATION, INVENTORY_DOMAIN, INVENTORY_PATH, INVENTORY_URL, PANEL_SELECTOR,
    SUBMISSION_RECEIPT, Inspection, InventoryItem, InventoryPageSnapshot, InventoryTiming,
    InventoryGpsrPolicy,
    INVENTORY_MARKETPLACES, MARKETPLACE_LABELS, ENTRY_LABELS, VIOLATION_LABELS,
    SAFETY_ATTESTATION, RESPONSIBLE_PERSON, MANUFACTURER, PARTY_TYPES, REQUIREMENT_LABELS, DECLARATIONS, SUBMISSION_RECEIPTS,
    RESPONSIBLE_PERSON_RECEIPTS, ATTESTATION_SAVE_LABELS, ATTESTATION_REVIEW_RECEIPTS,
)

logger = logging.getLogger(__name__)


class InventoryCsrfHeaderLoss(PageRecoveryRequired):
    """Known page request corruption; only read stages may rebuild the page."""

    def __init__(self) -> None:
        super().__init__("库存页面请求丢失校验头并返回跨站请求校验失败，需恢复页面", http_status=403)


class ItemLoadError(RuntimeError):
    """A bounded pre-submit load failed; caller may continue with the next item."""

    reason_code = "item_load_timeout"


class ItemEntryError(ItemLoadError):
    """This verified row has no unique supported entry; do not retry its click."""

    def __init__(self, *, ambiguous: bool = False) -> None:
        self.reason_code = "item_entry_ambiguous" if ambiguous else "item_entry_unrecognized"
        super().__init__("商品处理入口不唯一，未点击" if ambiguous else "商品处理入口未识别，未点击")


class ItemClickError(ItemLoadError):
    """A UI action was not completed; this is not evidence of an HTTP failure."""

    reason_code = "item_click_timeout"


class ItemPanelLoadError(ItemLoadError):
    reason_code = "item_panel_load_timeout"


class ItemSearchLoadError(ItemLoadError):
    reason_code = "item_search_load_timeout"


class ItemPanelResetError(ItemClickError):
    reason_code = "item_sidebar_not_reset"


class PersonSelectionError(ItemLoadError):
    """Selection failed before any persistent intent or submit dispatch."""

    reason_code = "responsible_person_selection_failed"


class ManufacturerSelectionError(PersonSelectionError):
    reason_code = "manufacturer_selection_failed"


class InventoryItemHttpError(ItemLoadError):
    """The current item stayed unreadable after bounded UI-only 403 recovery."""

    http_status = 403
    reason_code = "item_http_403_retries_exhausted"


class InventoryContractError(DomContractError):
    """Identity, inventory completeness, or an unknown form requires stopping."""


class InventoryScanChanged(InventoryContractError):
    """A count-only change can rebuild a pre-submit snapshot, not an identity error."""

    def __init__(self, expected: int, actual: int, page_number: int, size: int) -> None:
        super().__init__("扫描期间库存数量变化，候选清单不完整")
        self.details = dict(expected_total=expected, actual_total=actual, page=page_number, size=size)


class InventoryMarketplaceError(InventoryContractError):
    """Fixed menu reason; safe for persisted reports without raw exception text."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(MENU_ERRORS[reason])


class _AuthenticationResumed(RuntimeError):
    """Rebind one pre-submit item after successful authentication."""


class _AuthenticationNeeded(RuntimeError):
    """Leave the DOM deadline before using the shared login helper's own budget."""

    def __init__(self, page: Any, run: Any, marketplace: Any) -> None:
        self.page, self.run, self.marketplace = page, run, marketplace


_DOM_HELPERS = r"""
const visible = e => !!e && e.getClientRects().length > 0 &&
  getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
const deep = (root, selector) => {
  let out = [...root.querySelectorAll(selector)];
  if (root.shadowRoot) out = out.concat(deep(root.shadowRoot, selector));
  for (const e of root.querySelectorAll('*')) if (e.shadowRoot)
    out = out.concat(deep(e.shadowRoot, selector));
  return out;
};
const canonical = s => String(s||'').replaceAll('修正刊登資訊問題','解决商品信息问题')
  .replaceAll('處理違規商品刊登資訊','解决商品信息违规问题').replaceAll('負責人資訊','负责人信息')
  .replaceAll('製造商資訊','制造商信息').replaceAll('安全證明','安全证明')
  .replaceAll('警告和安全資訊','警告和安全信息').replaceAll('警告與安全資訊','警告和安全信息')
  .replaceAll('警告和安全圖片','警告和安全图片').replaceAll('GPSR 安全認證','GPSR 安全认证')
  .replaceAll('無需採取任何操作','无需采取任何操作').replaceAll('無需採取任何動作','无需采取任何操作');
const text = e => canonical(e.innerText || e.textContent || '').trim();
// Exact legal declarations must not pass through partial heading translation.
const rawLabel = e => String(e.getAttribute('label') || e.getAttribute('aria-label') ||
  e.innerText || e.textContent || '').trim();
const label = e => canonical(rawLabel(e));
"""

_IDENTITY_HELPERS = r"""
const headers=[...document.querySelectorAll('#dropdown-account-switcher-container')].filter(visible);
const normal=s=>String(s||'').normalize('NFKC').trim().replace(/\s+/g,' ').toLocaleLowerCase();
let headerValid=false, headerSeller='';
if(headers.length===1) {
  const globals=[...headers[0].querySelectorAll('.dropdown-account-switcher-header-label-global')].filter(visible);
  const regions=[...headers[0].querySelectorAll('.dropdown-account-switcher-header-label-regional')].filter(visible);
  headerValid=globals.length===1 && regions.length===1 && (p.marketLabels || ['德国','德國','Germany','Deutschland']).includes(text(regions[0]));
  if(headerValid) headerSeller=text(globals[0]);
}
const machine=[...document.querySelectorAll('meta[name="merchantId"],meta[name="sellerId"],[data-merchant-id],[data-seller-id]')]
  .flatMap(e=>[e.getAttribute('content'),e.getAttribute('data-merchant-id'),e.getAttribute('data-seller-id')]).filter(Boolean);
const machineUnique=[...new Set(machine.map(normal))];
const identityOK=headerValid && (normal(headerSeller)===normal(p.expected) ||
  (machineUnique.length===1 && machineUnique[0]===normal(p.expected)));
"""

_IDENTITY = "(p) => { /* inventory:identity */\n" + _DOM_HELPERS + _IDENTITY_HELPERS + "\nreturn {ok:identityOK}; }"
_MARKETPLACE_MENU = "(p) => { /* inventory:marketplace-menu */\n" + _DOM_HELPERS + _IDENTITY_HELPERS + MENU_BODY + "}"

_SEARCH = """() => { /* inventory:search */
%s
const groups=[...document.querySelectorAll('kat-input-group')].filter(visible)
  .filter(group=>deep(group,'kat-dropdown').some(dropdown=>
    deep(dropdown,'kat-option[value="SKU"]').length===1));
if(groups.length!==1) return {recognized:false,active:false,values:[]};
const group=groups[0];
const hosts=deep(group,'kat-input[type="text"][maxlength="1000"]').filter(visible);
if(hosts.length!==1) return {recognized:false,active:false,values:[]};
const host=hosts[0];
const native=deep(host,'input[type="text"],input:not([type])').filter(visible);
const values=native.length ? native.map(e=>String(e.value||'').trim()) :
  [String(host.value ?? host.getAttribute('value') ?? '').trim()];
return {recognized:true,active:values.some(Boolean),values,
  mode:String(deep(group,'kat-dropdown')[0].value || deep(group,'kat-dropdown')[0].getAttribute('value') || '')};
}""" % _DOM_HELPERS

_INSTALL_SEARCH_TRACE = """(token) => { /* inventory:install-search-trace */
window.__ziniaoInventorySearchTrace?.dispose();
const trace={token,started:false,fresh:false};
const containsRow=node=>node?.nodeType===1 &&
  (node.matches('[data-sku]') || !!node.querySelector('[data-sku]'));
const loader=node=>node?.nodeType===1 &&
  (node.matches('kat-spinner,[aria-busy="true"]') || !!node.querySelector('kat-spinner,[aria-busy="true"]'));
const changedList=record=>{
  const target=record.target;
  if(record.type==='childList')
    return (target?.nodeType===1 && !!target.closest('[data-sku]')) ||
      [...record.addedNodes,...record.removedNodes].some(n=>containsRow(n)||loader(n));
  if(record.type!=='attributes' || target?.nodeType!==1 ||
    record.oldValue===target.getAttribute(record.attributeName)) return false;
  if(target.matches('kat-pagination'))
    return ['total-items','page','items-per-page'].includes(record.attributeName);
  return !!target.closest('[data-sku]') && ['data-sku','href','hidden'].includes(record.attributeName);
};
const observer=new MutationObserver(records=>{
  if(!trace.started) return;
  if(records.some(changedList)) trace.fresh=true;
});
const keyed=event=>{
  if(event.key==='Enter' && event.isTrusted &&
    event.composedPath().some(e=>e?.nodeType===1 && e.matches('kat-input-group'))){
    observer.takeRecords();trace.started=true;trace.fresh=false;
  }
};
const clicked=event=>{
  if(event.isTrusted && event.composedPath().some(e=>e?.nodeType===1 &&
     e.matches('kat-option') && !e.closest('kat-input-group'))){
    observer.takeRecords();trace.started=true;trace.fresh=false;
  }
};
trace.dispose=()=>{observer.disconnect();document.removeEventListener('keydown',keyed,true);
  document.removeEventListener('click',clicked,true);};
observer.observe(document,{subtree:true,childList:true,attributes:true,attributeOldValue:true});
document.addEventListener('keydown',keyed,true);
document.addEventListener('click',clicked,true);
window.__ziniaoInventorySearchTrace=trace;
}"""

_CLEAR_SEARCH_TRACE = """(token) => {
const trace=window.__ziniaoInventorySearchTrace;
if(trace && trace.token===token){trace.dispose();delete window.__ziniaoInventorySearchTrace;}
}"""

_LIST = """() => { /* inventory:list */
%s
const ps = [...document.querySelectorAll('kat-pagination')].filter(visible);
const filters = [...document.querySelectorAll('kat-dropdown')].filter(visible)
  .map(e => String(e.value || e.getAttribute('value') || ''));
const statusControls=[...document.querySelectorAll('kat-dropdown')].filter(visible)
  .filter(e=>deep(e,'kat-option[value="ApprovalRequired"]').length===1);
const statusFilter=statusControls.length===1 ?
  String(statusControls[0].value ?? statusControls[0].getAttribute('value') ?? '') : null;
const searchTrace=window.__ziniaoInventorySearchTrace;
if (ps.length !== 1) {
  const empty=ps.length===0 && !document.querySelector('[data-sku]') &&
    /0\\s*[-–]\\s*0\\s*(?:共|of)\\s*0(?:\\s|個|个|件|$)/i.test(document.body.innerText);
  return {ready:empty,paginationCount:ps.length,filters,statusFilter,page:1,total:0,size:100,items:[],
    loading:deep(document,'kat-spinner,[aria-busy="true"]').some(visible),
    searchToken:searchTrace?.token||'',searchFresh:!!searchTrace?.fresh};
}
const p = ps[0];
const page = Number(p.getAttribute('page'));
const rows = [...document.querySelectorAll('[data-sku]')].filter(visible);
return {ready:true, filters, statusFilter, page,
  searchToken:searchTrace?.token || '', searchFresh:!!searchTrace?.fresh,
  total:Number(p.getAttribute('total-items')),
  size:Number(p.getAttribute('items-per-page')),
  loading:deep(document, 'kat-spinner,[aria-busy="true"]').some(visible),
  items:rows.map((row,position) => {
    const links = [...row.querySelectorAll('a[href]')];
    const dp = links.find(a => /\\/dp\\/[A-Z0-9]{10}(?:[/?#]|$)/.test(a.getAttribute('href') || ''));
    return {sku:row.getAttribute('data-sku'),
      asin:dp ? (dp.getAttribute('href').match(/\\/dp\\/([A-Z0-9]{10})(?:[/?#]|$)/) || [])[1] : '',
      title:dp ? text(dp) : '', position};
  })};
}""" % _DOM_HELPERS

_INSTALL_OPEN_TRACE = """(p) => { /* inventory:install-open-trace */
%s
const rows=[...document.querySelectorAll('[data-sku]')].filter(e=>visible(e) && e.getAttribute('data-sku')===p.sku);
if(!rows.length) return {ok:false,reason:'row_missing'};
if(rows.length!==1) return {ok:false,reason:'row_ambiguous'};
const row=rows[0];
const rowAsins=()=>[...new Set([...row.querySelectorAll('a[href]')]
  .map(e=>(e.getAttribute('href').match(/\\/dp\\/([A-Z0-9]{10})(?:[/?#]|$)/)||[])[1]).filter(Boolean))];
// Identity failures stay fatal, including when the entry is also unsupported.
if(rowAsins().length!==1 || rowAsins()[0]!==p.asin) return {ok:false,reason:'asin_changed'};
const entries=[...row.querySelectorAll('kat-link')].filter(e=>visible(e)&&%s.includes(rawLabel(e)));
if(!entries.length) return {ok:false,reason:'entry_unrecognized'};
if(entries.length!==1) return {ok:false,reason:'entry_ambiguous'};
// Inspect without touching an existing sidebar/trace, before trial clicks.
if(p.checkOnly) return {ok:true};
window.__ziniaoInventoryOpenTrace?.dispose();
const entry=entries[0];
const trace={token:p.token,sku:p.sku,asin:p.asin,clicks:0,invalid:false,fresh:false};
const containsPanel=node=>node?.nodeType===1 && (node.matches(p.panel) || !!node.querySelector(p.panel));
const containsContent=node=>node?.nodeType===1 &&
  (node.matches('#ActionPanelView,[data-f1-component="myp_actions_website/ActionPanelContent"]') ||
   !!node.querySelector('#ActionPanelView,[data-f1-component="myp_actions_website/ActionPanelContent"]'));
const isContent=node=>node?.nodeType===1 && !!node.closest(p.panel) &&
  !!node.closest('#ActionPanelView,[data-f1-component="myp_actions_website/ActionPanelContent"]');
const observer=new MutationObserver(records=>{
  if(trace.invalid || trace.clicks!==1) return;
  // Ignore hover, header changes, and unrelated inventory updates. A first-layer
  // content replacement or replacement of the whole panel proves a new render.
  if(records.some(record=>record.type==='childList' &&
    (isContent(record.target) || [...record.addedNodes,...record.removedNodes].some(containsPanel) ||
     (record.target?.nodeType===1 && record.target.matches(p.panel) &&
      [...record.addedNodes,...record.removedNodes].some(containsContent)))))
    trace.fresh=true;
});
const clicked=event=>{
  const path=event.composedPath();
  const selected=path.find(e=>e?.nodeType===1 && e.hasAttribute('data-sku'));
  if(!selected) return;
  observer.takeRecords(); // Do not attribute a pre-click render to this click.
  if(selected!==row || !path.includes(entry) || !event.isTrusted ||
      rowAsins().length!==1 || rowAsins()[0]!==p.asin || trace.clicks!==0){
    trace.invalid=true;return;
  }
  trace.clicks=1;trace.fresh=false;
};
trace.dispose=()=>{observer.disconnect();document.removeEventListener('click',clicked,true);};
observer.observe(document,{subtree:true,childList:true});
document.addEventListener('click',clicked,true);
window.__ziniaoInventoryOpenTrace=trace;
return {ok:true};
}""" % (_DOM_HELPERS, json.dumps(ENTRY_LABELS, ensure_ascii=False))

_CLEAR_OPEN_TRACE = """(token) => {
  const trace=window.__ziniaoInventoryOpenTrace;
  if(trace && trace.token===token){trace.dispose();delete window.__ziniaoInventoryOpenTrace;}
}"""

_PANEL = """(p) => { /* inventory:panel */
%s
const panels = [...document.querySelectorAll(p.panel)].filter(visible);
if (panels.length !== 1) return {ready:false, count:panels.length};
const panel = panels[0];
const content = text(panel);
const skus = [...content.matchAll(/(?:^|\\n)\\s*SKU\\s*[：:]\\s*([^\\n]+)/g)].map(m => m[1].trim());
const asins = [...content.matchAll(/ASIN\\s*[：:]\\s*([A-Z0-9]{10})(?![A-Z0-9])/g)].map(m=>m[1]);
const cards = deep(panel, '[role="button"]').filter(e=>visible(e)&&label(e)==='安全证明');
const boxes = deep(panel, 'kat-checkbox[name="value"]').filter(visible);
const saves = deep(panel, 'kat-button[data-testid="primary-button"]').filter(visible);
const forms = deep(panel, 'form[data-testid="form"]').filter(visible);
const requirementHeadings = deep(panel, 'h3').filter(visible);
const partyTitle = requirementHeadings.length===1 ? text(requirementHeadings[0]) : '';
const partyPage = ['负责人信息','制造商信息'].includes(partyTitle) &&
  !cards.length && !forms.length && !boxes.length &&
  saves.every(e=>['关闭','關閉','Close'].includes(label(e)));
const responsiblePerson=partyPage && partyTitle==='负责人信息';
const manufacturerInformation=partyPage && partyTitle==='制造商信息';
const personCards=partyPage ? deep(panel,'kat-card').filter(visible) : [];
const personRecords=personCards.map(e=>{
  const radios=deep(e,'input[type="radio"]').filter(visible);
  const badges=deep(e,'kat-badge').filter(visible).map(label);
  return {key:text(e),radioCount:radios.length,selected:radios.some(r=>r.checked),
    disabled:radios.length!==1||radios[0].disabled,
    invalid:badges.some(b=>/失效|无效|無效|过期|過期|expired|invalid/i.test(b))};
});
const personSaves=partyPage ? deep(panel,'kat-button').filter(e=>visible(e)&&['送出','提交','Submit'].includes(label(e))) : [];
const partyReceipt = partyPage && !personSaves.length &&
  !deep(panel, 'input,kat-radiobutton,kat-checkbox,form').some(visible) &&
  deep(panel, '*').some(e=>visible(e) && (p.personReceipts||[]).some(r=>
    text(e).replace(/\\s+/g,'')===r.replace(/\\s+/g,'')));

const personReceipt = responsiblePerson && partyReceipt;
const manufacturerReceipt = manufacturerInformation && partyReceipt;

// The current safety receipt uses a shorter seven-day message and renders the
// submitted declaration as read-only true. It is distinct from a checked form
// and from the responsible-person/manufacturer receipt with similar wording.
const safetyHeadings=deep(panel,'h1,h2,h3,h4').filter(e=>visible(e)&&text(e)==='GPSR 安全认证');
const safetyReceipt = safetyHeadings.length===1 && !forms.length && !boxes.length &&
  !deep(panel,'input,kat-radiobutton,kat-checkbox,form').some(visible) &&
  saves.length===1 && ['关闭','關閉','Close'].includes(label(saves[0])) &&
  deep(panel,'*').some(e=>visible(e) && (p.attestationReceipts||[]).some(r=>{
    const value=text(e).replace(/\\s+/g,'');
    const message=r.replace(/\\s+/g,'');
    return value===message+'GPSR安全认证:true' || value===message+'GPSR安全认证：true';
  }));

const boundRows=[...document.querySelectorAll('[data-sku]')].filter(e=>visible(e)&&skus.includes(e.getAttribute('data-sku')));
const rowAsins=boundRows.length===1 ? [...new Set([...boundRows[0].querySelectorAll('a[href]')]
  .map(e=>(e.getAttribute('href').match(/\\/dp\\/([A-Z0-9]{10})(?:[/?#]|$)/)||[])[1]).filter(Boolean))] : [];
const violation = deep(panel, '*').filter(e => visible(e) && text(e) === '解决商品信息违规问题'
  && ![...e.children].some(c => text(c) === '解决商品信息违规问题'));
const noActions=deep(panel,'#ActionPanelView kat-alert').filter(e=>visible(e) &&
  e.getAttribute('variant')==='info' && e.getAttribute('size')==='large' &&
  e.getAttribute('persistent')==='true' &&
  e.getAttribute('description')==='如果您之前已提交更改，则这些更改当前正在处理中。' &&
  [...e.querySelectorAll('span[slot="header"]')].filter(visible)
    .filter(h=>text(h)==='无需采取任何操作').length===1);
// KAT keeps this named-slot heading in the active panel's light DOM without
// assigning it to a visible slot. It is structural evidence, not the alert.
const headers=deep(panel,'h4#actionPanelHeader');
const noAction=noActions.length===1 && headers.length===1 && text(headers[0])==='解决商品信息问题' &&
  !skus.length && !asins.length && !cards.length && !boxes.length && !saves.length && !violation.length;
const trace=window.__ziniaoInventoryOpenTrace;
return {ready:true, skus:[...new Set(skus)], asins:[...new Set(asins)],
  loading:deep(panel, 'kat-spinner,[aria-busy="true"],[role="progressbar"]').some(visible),
  firstLayer:!!panel.querySelector('[data-testid="action-panel-body"]'),
  noAction,
  freshOpen:!!(trace && trace.token===p.traceToken && trace.clicks===1 && !trace.invalid && trace.fresh),
  freshNoAction:!!(noAction && trace && trace.token===p.traceToken && trace.clicks===1 && !trace.invalid && trace.fresh),
  openedSku:trace && trace.token===p.traceToken ? trace.sku : '',
  openedAsin:trace && trace.token===p.traceToken ? trace.asin : '',
  violationCount:violation.length, cardCount:cards.length,
  cardSubmitted:cards.length===1 && /审核中|正在审核中/.test(text(cards[0])),
  receipt:safetyReceipt || (p.receipts||[p.receipt]).some(r=>r && content.replace(/\\s+/g,'').includes(r.replace(/\\s+/g,''))),
  safetyReceipt,
  responsiblePerson,manufacturerInformation,manufacturerReceipt,personRecords,rowAsins,personReceipt,
  personSaves:personSaves.map(e=>({disabled:e.hasAttribute('disabled')&&e.getAttribute('disabled')!=='false'})),
  knownOther:responsiblePerson || content.includes('制造商信息') || /WEEE|EPR/.test(content),
  resolution:content.includes('警告和安全信息') &&
    (content.includes('商品文件') || content.includes('警告和安全图片')),
  formCount:forms.length,
  boxes:boxes.map(e=>({label:rawLabel(e), value:e.getAttribute('value'),
    checked:deep(e,'input[type="checkbox"]').some(i=>i.checked),
    disabled:e.hasAttribute('disabled') && e.getAttribute('disabled')!=='false'})),
  saves:saves.map(e=>({label:label(e),disabled:e.hasAttribute('disabled') && e.getAttribute('disabled')!=='false'})),
  heading:content.includes('GPSR 安全认证')};
}""" % _DOM_HELPERS

# This final same-tick check protects against a sidebar/route swap during a click.
# Browser-side marking precedes dispatch; a thrown/uncertain evaluation is never retried.
_SAVE = """(p) => { /* inventory:save */
%s
%s
if (!identityOK) return {ok:false,reason:'seller_changed'};
const u = new URL(location.href);
if (u.protocol!=='https:' || u.hostname!==p.domain || u.pathname!==p.path || u.port)
  return {ok:false,reason:'route_changed'};
const activeFilters=[...document.querySelectorAll('kat-dropdown')].filter(visible)
  .filter(e=>String(e.value || e.getAttribute('value') || '')==='ApprovalRequired');
if(activeFilters.length!==1 || u.searchParams.get('status')!=='approval_required')
  return {ok:false,reason:'filter_changed'};
const panels = [...document.querySelectorAll(p.panel)].filter(visible);
if(panels.length!==1) return {ok:false,reason:'panel_not_unique'};
const panel=panels[0], content=text(panel);
const skus=[...content.matchAll(/(?:^|\\n)\\s*SKU\\s*[：:]\\s*([^\\n]+)/g)].map(m=>m[1].trim());
const asins=[...content.matchAll(/ASIN\\s*[：:]\\s*([A-Z0-9]{10})(?![A-Z0-9])/g)].map(m=>m[1]);
if(new Set(skus).size!==1 || skus[0]!==p.sku || new Set(asins).size!==1 || asins[0]!==p.asin)
  return {ok:false,reason:'item_changed'};
const boxes=deep(panel,'kat-checkbox[name="value"]').filter(visible);
const forms=deep(panel,'form[data-testid="form"]').filter(visible);
const saves=deep(panel,'kat-button[data-testid="primary-button"]').filter(visible);
if(forms.length!==1 || boxes.length!==1 || rawLabel(boxes[0])!==p.declaration ||
  !(boxes[0].getAttribute('value')==='true' || deep(boxes[0],'input[type="checkbox"]').some(e=>e.checked)))
  return {ok:false,reason:'declaration_changed'};
if(saves.length!==1 || !%s.includes(label(saves[0])) ||
  (saves[0].hasAttribute('disabled') && saves[0].getAttribute('disabled')!=='false'))
  return {ok:false,reason:'save_not_ready'};
const key=p.run+'|'+p.marketplace+'|'+p.sku+'|'+(p.requirementType||'safety_attestation');
window.__ziniaoInventoryGpsrConsumed ||= new Set();
if(window.__ziniaoInventoryGpsrConsumed.has(key)) return {ok:false,reason:'already_consumed'};
window.__ziniaoInventoryGpsrConsumed.add(key);
saves[0].click();
return {ok:true};
}""" % (_DOM_HELPERS, _IDENTITY_HELPERS, json.dumps(ATTESTATION_SAVE_LABELS, ensure_ascii=False))

_PERSON_SAVE = """(p) => { /* inventory:responsible-save */
%s
%s
const u=new URL(location.href);
if(!identityOK || u.protocol!=='https:' || u.hostname!==p.domain || u.port || u.pathname!==p.path ||
   u.searchParams.get('status')!=='approval_required')return {ok:false,reason:'identity_or_route_changed'};
if([...document.querySelectorAll('kat-dropdown')].filter(e=>visible(e)&&String(e.value||e.getAttribute('value'))==='ApprovalRequired').length!==1)
  return {ok:false,reason:'filter_changed'};
const panels=[...document.querySelectorAll(p.panel)].filter(visible);
if(panels.length!==1)return {ok:false,reason:'panel_changed'};
const panel=panels[0],content=text(panel);
const headings=deep(panel,'h3').filter(visible);
const skus=[...content.matchAll(/(?:^|\\n)\\s*SKU\\s*[：:]\\s*([^\\n]+)/g)].map(m=>m[1].trim());
const rows=[...document.querySelectorAll('[data-sku]')].filter(e=>visible(e)&&e.getAttribute('data-sku')===p.sku);
const titles={responsible_person:'负责人信息',manufacturer:'制造商信息'};
if(!Object.prototype.hasOwnProperty.call(titles,p.requirementType))return {ok:false,reason:'requirement_changed'};
if(headings.length!==1 || text(headings[0])!==titles[p.requirementType] || new Set(skus).size!==1 || skus[0]!==p.sku || rows.length!==1)
  return {ok:false,reason:'item_changed'};
const asins=[...new Set([...rows[0].querySelectorAll('a[href]')].map(e=>(e.getAttribute('href').match(/\\/dp\\/([A-Z0-9]{10})(?:[/?#]|$)/)||[])[1]).filter(Boolean))];
if(asins.length!==1 || asins[0]!==p.asin)return {ok:false,reason:'asin_changed'};
const cards=deep(panel,'kat-card').filter(visible);
const selectedCards=cards.filter(e=>deep(e,'input[type="radio"]').some(r=>visible(r)&&r.checked));
if(selectedCards.length!==1 || text(selectedCards[0])!==p.record ||
   cards.filter(e=>text(e)===p.record).length!==1)return {ok:false,reason:'record_changed'};
const radios=deep(selectedCards[0],'input[type="radio"]').filter(visible);
if(radios.length!==1 || !radios[0].checked || radios[0].disabled ||
   deep(selectedCards[0],'kat-badge').some(e=>/失效|无效|無效|过期|過期|invalid|expired/i.test(label(e))))
  return {ok:false,reason:'record_not_ready'};
const saves=deep(panel,'kat-button').filter(e=>visible(e)&&['送出','提交','Submit'].includes(label(e)));
if(saves.length!==1 || (saves[0].hasAttribute('disabled')&&saves[0].getAttribute('disabled')!=='false'))
  return {ok:false,reason:'save_not_ready'};
const key=p.run+'|'+p.marketplace+'|'+p.sku+'|'+p.requirementType;
window.__ziniaoInventoryGpsrConsumed ||= new Set();
if(window.__ziniaoInventoryGpsrConsumed.has(key))return {ok:false,reason:'already_consumed'};
window.__ziniaoInventoryGpsrConsumed.add(key);
saves[0].click();return {ok:true};
}""" % (_DOM_HELPERS, _IDENTITY_HELPERS)


class AmazonInventoryGpsrPage:
    def __init__(self, *, timing: InventoryTiming | None = None,
                 policy: InventoryGpsrPolicy | None = None,
                 policy_path: Path | None = None,
                 landing: AmazonLanding | None = None,
                 login_advancer: AmazonLoginAdvancer | None = None) -> None:
        if sum(x is not None for x in (timing, policy, policy_path)) > 1:
            raise ValueError("Specify only one inventory policy source")
        self._timing = policy or timing
        self._policy_path = policy_path
        if self._timing is None and policy_path is None:
            self._timing = InventoryGpsrPolicy()
        self.login = login_advancer or AmazonLoginAdvancer()
        self.landing = landing or AmazonLanding(login_advancer=self.login)
        self._aliases: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
        self._contexts: WeakKeyDictionary[Any, tuple[Any, Any]] = WeakKeyDictionary()
        self._open_tokens: WeakKeyDictionary[Any, str] = WeakKeyDictionary()
        self._person_selection: WeakKeyDictionary[Any, tuple[str, str, str, str]] = WeakKeyDictionary()
        self._person_prepared: WeakKeyDictionary[Any, tuple[str, str, str, str]] = WeakKeyDictionary()
        self._panel_requirement: WeakKeyDictionary[Any, str] = WeakKeyDictionary()
        self._inspected_items: WeakKeyDictionary[Any, tuple[str, str, str]] = WeakKeyDictionary()
        self._active_marketplace: Any = None
        self._consumed: set[tuple[str, str, str, str]] = set()
        self.cancel_check: Callable[[Any], Awaitable[None]] | None = None
        self._active_run: Any = None
        self._history_readonly = False
        self._network_run_id: str | None = None
        self._network_attempt = 0
        self._network_failure: int | None = None
        self._csrf_header_loss = False
        self._csrf_reloads = 0
        self._pending_diagnostic: Any = None
        self._diagnostic_generation = 0
        self._http_retries: dict[tuple[str, str, str, str, str], int] = {}
        self.on_http_event: Callable[[Any, Any, str, int], Awaitable[None]] | None = None
        self.on_item_event: Callable[[Any, Any, str, dict[str, Any]], Awaitable[None]] | None = None
        self.on_marketplace_event: Callable[[Any, Any, str, dict[str, Any]], Awaitable[None]] | None = None
        self.on_scan_event: Callable[[Any, Any, str, dict[str, Any]], Awaitable[None]] | None = None
        self._item_stage = "entry"
        self._last_panel: dict[str, Any] = {}
        self._response_observers: WeakKeyDictionary[Any, tuple[Any, Any]] = WeakKeyDictionary()

    def begin_attempt(self, run_id: Any) -> None:
        """Reset HTTP observation only at a newly dequeued execution attempt.

        The workflow engine calls this once after QUEUED -> RUNNING, including
        an explicitly resumed run. Authentication recovery and listener cleanup
        never reset failures. Verified DOM and bounded item recovery handle 403
        responses separately; consumed saves remain consumed.
        """
        self.detach_response_observers()
        self._network_attempt += 1
        self._network_run_id = str(run_id)
        self._clear_forbidden_response()
        self._network_failure = None
        self._csrf_reloads = 0
        self._http_retries.clear()

    def detach_response_observers(self) -> None:
        """Release listeners without clearing this run's latched HTTP failure."""
        self._diagnostic_generation += 1
        self._pending_diagnostic = None
        for page, (response_handler, close_handler) in list(self._response_observers.items()):
            self._response_observers.pop(page, None)
            self._remove_response_observers(page, response_handler, close_handler)

    @staticmethod
    def _remove_response_observers(page: Any, response_handler: Any, close_handler: Any) -> None:
        for event, handler in (('response', response_handler), ('close', close_handler)):
            try:
                page.remove_listener(event, handler)
            except Exception as exc:
                # Playwright's closed transport can raise while unregistering a
                # listener. Cleanup is not a new business/reporting failure.
                if type(exc).__name__ != 'TargetClosedError':
                    raise
                logger.debug('inventory_observer_already_closed event=%s', event)

    def _watch_inventory_responses(self, page: Any, run: Any) -> None:
        run_id = str(run.id)
        if self._network_run_id != run_id:
            self.begin_attempt(run_id)
        if page not in self._response_observers:
            attempt = self._network_attempt
            weak_page = ref(page)
            def active() -> bool:
                current = weak_page()
                handlers = self._response_observers.get(current) if current is not None else None
                return (self._network_run_id == run_id and self._network_attempt == attempt
                        and handlers is not None and handlers[0] is responded)

            def responded(response: Any) -> None:
                if not active():
                    return
                # Only retain the first relevant 403 for a bounded, passive
                # diagnosis at the next awaited page read. No background tasks.
                try:
                    address = urlparse(str(response.url))
                    status = int(response.status)
                    relevant = (address.scheme == 'https' and address.hostname in INVENTORY_MARKETPLACES.values()
                                and address.port is None and address.path == '/myinventory/gql'
                                and not address.params and not address.username and not address.password)
                except (ValueError, TypeError):
                    return
                if relevant and (status in {403, 429} or 500 <= status <= 599):
                    if self._network_failure is None or (self._network_failure == 403 and status != 403):
                        self._network_failure = status
                        self._diagnostic_generation += 1
                        self._pending_diagnostic = response if status == 403 else None
                        logger.warning('inventory_http_failure status=%s', status)

            def closed(*_args: Any) -> None:
                if not active():
                    return
                self._diagnostic_generation += 1
                self._pending_diagnostic = None
                current = weak_page()
                if current is not None:
                    handlers = self._response_observers.pop(current, None)
                    if handlers:
                        self._remove_response_observers(current, *handlers)

            page.on('response', responded)
            page.on('close', closed)
            self._response_observers[page] = (responded, closed)
        self._require_inventory_http_ok(allow_forbidden_read=True)

    async def _diagnose_forbidden_response(self) -> None:
        response = self._pending_diagnostic
        if response is None:
            return
        generation = self._diagnostic_generation
        self._pending_diagnostic = None
        try:
            confirmed = await is_csrf_header_loss(
                response, timeout=self.timing.http_diagnostic_timeout_seconds,
            )
        except Exception as exc:
            # A closed/unavailable response is not evidence of header loss.
            # Never persist browser exceptions: they can contain signed URLs.
            logger.debug("inventory_http_diagnostic_unavailable type=%s", type(exc).__name__)
            return
        if generation == self._diagnostic_generation and self._network_failure == 403 and confirmed:
            self._csrf_header_loss = True
            logger.warning("inventory_http_diagnostic category=csrf_header_loss")

    def _clear_forbidden_response(self) -> None:
        self._diagnostic_generation += 1
        self._pending_diagnostic = None
        self._csrf_header_loss = False
        if self._network_failure == 403:
            self._network_failure = None

    def _require_inventory_http_ok(self, *, allow_forbidden_read: bool = False) -> None:
        if self._network_failure == 403 and self._csrf_header_loss:
            raise InventoryCsrfHeaderLoss()
        if allow_forbidden_read and self._network_failure == 403:
            return
        if self._network_failure is not None:
            raise PageRecoveryRequired(
                f"德国站库存页面请求返回 HTTP {self._network_failure}，本次流程已暂停并保留进度；"
                "请在紫鸟确认页面恢复后继续当前任务",
                http_status=self._network_failure,
            )

    async def _http_event(self, run: Any, item: InventoryItem | None, action: str) -> None:
        retry = self._http_retries.get(self._retry_key(run, item), 0) if item else 0
        if self.on_http_event is not None:
            await self.on_http_event(run, item, action, retry)
        logger.info("inventory_http_403 action=%s retry=%s", action, retry)

    async def _accept_forbidden_dom(self, run: Any, item: InventoryItem | None) -> None:
        await self._diagnose_forbidden_response()
        self._require_inventory_http_ok(allow_forbidden_read=True)
        if self._network_failure == 403:
            self._clear_forbidden_response()
            await self._http_event(run, item, "dom_verified")

    async def _recover_csrf_page(self, page: Any, run: Any, marketplace: Any,
                                 item: InventoryItem) -> Any:
        """Rebuild this tab once per execution attempt, without changing the ledger."""
        await self._check_cancelled()
        if self._network_failure != 403 or not self._csrf_header_loss:
            self._require_inventory_http_ok()
            raise InventoryContractError("未确认页面校验头丢失，未刷新页面")
        await self._http_event(run, item, "csrf_detected")
        if self._csrf_reloads >= self.timing.csrf_reload_attempts:
            await self._http_event(run, item, "csrf_recovery_exhausted")
            raise PageRecoveryRequired("库存页面校验故障恢复次数已用完，已保留剩余商品进度", http_status=403)
        page = self.resolved_page_for(page)
        self._require_route(page)
        self._csrf_reloads += 1
        await self._http_event(run, item, "csrf_reload")
        try:
            async with asyncio.timeout(self.timing.csrf_recovery_timeout_seconds):
                await asyncio.sleep(self.timing.http_retry_delay_seconds)
                await self._check_cancelled()
                # A rate limit arriving during the delay still stops recovery.
                if self._network_failure != 403:
                    self._require_inventory_http_ok()
                self._require_route(page)
                # Ignore completions of the old document during navigation.
                self.detach_response_observers()
                self._clear_forbidden_response()
                self._open_tokens.pop(page, None)
                await page.reload(wait_until="domcontentloaded",
                                  timeout=int(self.timing.list_timeout_seconds * 1000))
                self._watch_inventory_responses(page, run)
                page = await self._session(page, run, marketplace)
                await self._wait_list(page)
                await self._identity(page, run)
                await self._check_cancelled()
                # The list reader checks the rendered ApprovalRequired filter.
                # Keep an exact SKU search if one was active; locate_item will
                # restore/search through normal UI using the original snapshot.
                if parse_qs(urlparse(str(page.url)).query).get('status') != ['approval_required']:
                    raise InventoryContractError("恢复后库存筛选已变化，未继续商品操作")
                await self._diagnose_forbidden_response()
                self._require_inventory_http_ok()
        except WorkflowError:
            raise
        except Exception:
            await self._http_event(run, item, "csrf_recovery_failed")
            raise PageRecoveryRequired("库存页面恢复未完成，已保留当前及剩余商品进度", http_status=403) from None
        await self._http_event(run, item, "csrf_restored")
        return page

    async def _item_event(self, run: Any, item: InventoryItem, action: str,
                          error: ItemLoadError | None = None) -> None:
        # Fixed scalars only: no DOM text, URL, seller identity or raw exceptions.
        details = {"stage": self._item_stage,
                   "retry": self._http_retries.get(self._retry_key(run, item), 0),
                   "http_status": self._network_failure,
                   "reason": error.reason_code if error else None,
                   "panel_ready": bool(self._last_panel.get("ready")),
                   "panel_loading": bool(self._last_panel.get("loading")),
                   "panel_bound": self._bound(self._last_panel, item),
                   "fresh_open": bool(self._last_panel.get("freshOpen")),
                   "no_action": bool(self._last_panel.get("noAction")),
                   "first_layer": bool(self._last_panel.get("firstLayer")),
                   "known_other": bool(self._last_panel.get("knownOther")),
                   "person_form": bool(self._last_panel.get("responsiblePerson")),
                   "manufacturer_form": bool(self._last_panel.get("manufacturerInformation")),
                   "typed_receipt": bool(self._last_panel.get("personReceipt") or self._last_panel.get("manufacturerReceipt") or self._last_panel.get("safetyReceipt")),
                   "close_count": self._last_panel.get("closeCount"),
                   "back_count": self._last_panel.get("backCount")}
        if self.on_item_event is not None:
            await self.on_item_event(run, item, action, details)
        logger.info("inventory_item_ui action=%s details=%s", action, details)

    @staticmethod
    def _positive_panel_content(state: dict[str, Any]) -> bool:
        # An anonymous no-action fallback can result from a 403. Require
        # explicit, item-bound content before accepting this response anomaly.
        return bool(state.get('receipt') or state.get('cardSubmitted') or
                    state.get('cardCount') or state.get('formCount') or
                    state.get('knownOther') or state.get('violationCount'))

    async def _check_cancelled(self) -> None:
        if self.cancel_check is not None and self._active_run is not None:
            await self.cancel_check(self._active_run)

    @property
    def timing(self) -> InventoryGpsrPolicy:
        if self._timing is None:
            assert self._policy_path is not None
            self._timing = InventoryGpsrPolicy.from_file(self._policy_path)
        return self._timing

    def resolved_page_for(self, page: Any) -> Any:
        while page in self._aliases and self._aliases[page] is not page:
            page = self._aliases[page]
        return page

    def _remember(self, old: Any, page: Any, run: Any, marketplace: Any) -> Any:
        if old is not page:
            self._aliases[old] = page
            # Requirement context is needed to bind a generic receipt after a
            # successful reauthentication/new-tab handoff; no click is replayed.
            if old in self._panel_requirement:
                self._panel_requirement[page] = self._panel_requirement[old]
        self._contexts[page] = (run, marketplace)
        return page

    @staticmethod
    def _require_route(page: Any) -> None:
        u = urlparse(str(page.url))
        if (u.scheme != 'https' or u.hostname not in INVENTORY_MARKETPLACES.values() or u.port is not None
                or u.path != INVENTORY_PATH or u.params or u.username or u.password):
            raise InventoryContractError("当前页面不是允许商城的准确库存路径")

    @staticmethod
    def _trusted_host(page: Any) -> str:
        u = urlparse(str(page.url))
        if (u.scheme != 'https' or u.hostname not in INVENTORY_MARKETPLACES.values()
                or u.port or u.username or u.password or u.params):
            raise InventoryContractError("库存页面域名不在准确白名单中")
        return u.hostname

    async def _switch_marketplace(self, page: Any, run: Any, marketplace: Any) -> Any:
        """Host is a security boundary, never proof of the selected EU store."""
        host = urlparse(str(page.url)).hostname
        if host not in INVENTORY_MARKETPLACES.values():
            # Bootstrap only once to a known business page. It does NOT select
            # the marketplace; the header/menu check below is still mandatory.
            outcome = await self.landing.land_on(page,
                url=INVENTORY_URL.replace(INVENTORY_DOMAIN, marketplace.domain),
                expected_path=INVENTORY_PATH, domain=marketplace.domain)
            page = outcome.page or page
            if not outcome.ok:
                raise HumanAuthRequired("库存流程等待登录和商城选择", kind="signin")
        host = self._trusted_host(page)
        if AmazonLoginAdvancer.is_supported_login_url(str(page.url), expected_host=host):
            page = await self.login.advance_or_raise(page, expected_host=host)
        expected = {'expected': str(run.store.expected_seller_id), 'marketLabels': MARKETPLACE_LABELS[marketplace.code]}
        if (await self._evaluate(page, _IDENTITY, expected)).get('ok'):
            return page
        any_market = {'expected': str(run.store.expected_seller_id),
                      'marketLabels': [label for labels in MARKETPLACE_LABELS.values() for label in labels]}
        await self._poll(lambda: self._evaluate(page, _IDENTITY, any_market), lambda s: s.get('ok'),
                         self.timing.list_timeout_seconds, message="切站前卖家身份或商城页头未识别", contract=True)
        reopens = await self._choose_marketplace(page, run, marketplace, any_market)
        # A switch can invoke the shared login flow; its own deadline is used.
        async def settled():
            nonlocal page
            host = self._trusted_host(page)
            if AmazonLoginAdvancer.is_supported_login_url(str(page.url), expected_host=host):
                return {'auth': True}
            return await self._evaluate(page, _IDENTITY, expected)
        result = await self._poll(settled, lambda s: s.get('ok') or s.get('auth'), self.timing.list_timeout_seconds,
                                 message="商城切换未确认", contract=True)
        if result.get('auth'):
            page = await self.login.advance_or_raise(page, expected_host=self._trusted_host(page))
            await self._poll(lambda: self._evaluate(page, _IDENTITY, expected), lambda s: s.get('ok'),
                             self.timing.list_timeout_seconds, message="登录后商城切换未确认", contract=True)
        self._panel_requirement.pop(page, None)
        self._person_selection.pop(page, None)
        self._person_prepared.pop(page, None)
        self._inspected_items.pop(page, None)
        await self._marketplace_event(run, marketplace, 'confirmed', {'state': 'selected'}, reopens)
        return page

    async def _marketplace_event(self, run: Any, marketplace: Any, action: str,
                                 state: dict[str, Any], reopens: int) -> None:
        details = {"state": state['state'], "target_count": state.get('targets', 0),
                   "item_count": state.get('items', 0), "reopens": reopens}
        logger.info("inventory_marketplace action=%s target=%s details=%s", action, marketplace.code, details)
        if self.on_marketplace_event is not None:
            await self.on_marketplace_event(run, marketplace, action, details)

    async def _choose_marketplace(self, page: Any, run: Any, marketplace: Any,
                                  identity: dict[str, Any]) -> int:
        """Bounded menu recovery, with a single target click and no page reload.

        Respect the ordinary interval *between* clicks; do not add another
        three-second sleep after a transient menu has already become ready.
        """
        payload = {**identity, 'targetLabels': MARKETPLACE_LABELS[marketplace.code]}
        state: dict[str, Any] = {'state': 'header_missing'}
        opens = reopens = 0
        last_action = time.monotonic()
        closed_since = None
        stable_since = None
        stable_key = None
        logged_key = None
        header = page.locator(HEADER_ROOT).filter(visible=True).locator(HEADER)
        fatal = {'header_ambiguous', 'identity_mismatch', 'target_ambiguous',
                 'target_disabled', 'account_mismatch'}
        try:
            # Includes DOM reads, pacing, retries, and click actionability waits.
            async with asyncio.timeout(self.timing.list_timeout_seconds):
                while True:
                    await self._check_cancelled()
                    self._trusted_host(page)
                    state = await self._evaluate(page, _MARKETPLACE_MENU, payload)
                    now = time.monotonic()
                    key = (state['state'], state.get('scope'), state.get('index'))
                    if key != logged_key:
                        logger.info("inventory_marketplace_state target=%s state=%s targets=%s reopens=%s",
                                    marketplace.code, state['state'], state.get('targets', 0), reopens)
                        logged_key = key
                    if state['state'] in fatal:
                        raise InventoryMarketplaceError(state['state'])
                    if state['state'] == 'selected':
                        return reopens
                    if not opens and state['state'] in {'loading', 'ready', 'target_pending'}:
                        # A menu left open by the user counts as the initial open;
                        # it must not receive an extra recovery budget.
                        opens = 1
                    if state['state'] == 'closed':
                        stable_since = None
                        if closed_since is None:
                            closed_since = now
                        if opens and reopens >= self.timing.marketplace_menu_reopen_attempts:
                            # Allow a DOM redraw to restore an open menu before
                            # classifying a second collapse as terminal.
                            if now - closed_since >= self.timing.marketplace_menu_reopen_delay_seconds:
                                raise InventoryMarketplaceError('closed')
                        elif (now - last_action >= self.timing.action_interval_seconds and
                              (not opens or now - closed_since >= self.timing.marketplace_menu_reopen_delay_seconds)):
                            # Announcements may have closed it while being dismissed.
                            # Read once again so a now-open menu is never toggled off.
                            fresh = await self._evaluate(page, _MARKETPLACE_MENU, payload)
                            if fresh['state'] != 'closed':
                                continue
                            action = 'opened' if not opens else 'reopened'
                            await header.click(timeout=int(self.timing.click_timeout_seconds * 1000))
                            opens += 1
                            reopens = opens - 1
                            last_action = time.monotonic()
                            closed_since = last_action
                            await self._marketplace_event(run, marketplace, action, state, reopens)
                    else:
                        closed_since = None
                        if state['state'] == 'ready':
                            if stable_since is None or key != stable_key:
                                stable_since, stable_key = now, key
                            if (now - stable_since >= self.timing.marketplace_menu_stable_seconds and
                                    now - last_action >= self.timing.action_interval_seconds):
                                root = page.locator(HEADER_ROOT).filter(visible=True)
                                scope = root.locator(OWNER_XPATH) if state['scope'] == 'owner' else root
                                labels = MARKETPLACE_LABELS[marketplace.code]
                                options = scope.locator(ITEM).filter(visible=True).filter(
                                    has_text=re.compile(r'^\s*(?:' + '|'.join(re.escape(s) for s in labels) + r')\s*$', re.I))
                                if await options.count() != 1:
                                    state = {**state, 'state': 'target_ambiguous'}
                                    raise InventoryMarketplaceError('target_ambiguous')
                                fresh = await self._evaluate(page, _MARKETPLACE_MENU, payload)
                                if fresh != state:
                                    stable_since = None
                                    continue
                                state = {'state': 'click_pending', 'targets': 1}
                                await options.click(timeout=int(self.timing.click_timeout_seconds * 1000))
                                await self._marketplace_event(run, marketplace, 'clicked', state, reopens)
                                return reopens  # Never re-enter the menu after dispatch.
                        else:
                            stable_since = None
                    await asyncio.sleep(self.timing.poll_seconds)
        except Exception as exc:
            if type(exc).__name__ == 'TimeoutError':
                await self._marketplace_event(run, marketplace, 'failed', state, reopens)
                raise InventoryMarketplaceError(state['state']) from exc
            if isinstance(exc, InventoryContractError):
                await self._marketplace_event(run, marketplace, 'failed', state, reopens)
            raise

    async def _identity(self, page: Any, run: Any) -> None:
        expected = str(run.store.expected_seller_id).strip()
        if not expected:
            raise InventoryContractError("店铺尚未绑定卖家身份")
        try:
            # The inventory header is a live page proof; never reuse a payment cache.
            market = self._contexts.get(page, (None, self._active_marketplace))[1]
            proof = await self._evaluate(page, _IDENTITY, {'expected': expected,
                'marketLabels': MARKETPLACE_LABELS[market.code if market else 'DE']})
            if not proof.get('ok'):
                raise PreflightRejected("库存卖家或站点身份不匹配")
        except (PreflightRejected, TimeoutError) as exc:
            raise InventoryContractError("库存页卖家身份未能与店铺建档匹配") from exc

    async def _session(self, page: Any, run: Any, marketplace: Any) -> Any:
        self._active_run = run
        self._active_marketplace = marketplace
        await self._check_cancelled()
        source = page
        page = self.resolved_page_for(page)
        self._watch_inventory_responses(page, run)
        if INVENTORY_MARKETPLACES.get(marketplace.code) != marketplace.domain:
            raise InventoryContractError("库存流程站点配置不在白名单中")
        host = self._trusted_host(page)
        if AmazonLoginAdvancer.is_supported_login_url(str(page.url), expected_host=host):
            page = await self.login.advance_or_raise(page, expected_host=host)
            if page is None:
                raise HumanAuthRequired("库存流程等待当前商城登录完成", kind="signin")
            self._watch_inventory_responses(page, run)
            self._require_route(page)
            self._remember(source, page, run, marketplace)
            await self._identity(page, run)
        self._require_route(page)
        return self._remember(source, page, run, marketplace)

    async def open_inventory(self, page: Any, run: Any, marketplace: Any) -> Any:
        self._active_run = run
        await self._check_cancelled()
        if INVENTORY_MARKETPLACES.get(marketplace.code) != marketplace.domain:
            raise InventoryContractError("库存流程站点配置不在白名单中")
        source = page
        page = self.resolved_page_for(page)
        self._active_marketplace = marketplace
        page = await self._switch_marketplace(page, run, marketplace)
        self._remember(source, page, run, marketplace)
        # Install before the first navigation so initial-list failures also latch.
        self._watch_inventory_responses(page, run)
        navigated = False
        if not self._has_full_inventory_query(page):
            page = await self._land_inventory(page, run, marketplace)
            navigated = True
        page = await self._session(page, run, marketplace)
        await self._wait_list(page)
        search = await self._read_search_state(page)
        if not search.get('recognized'):
            raise InventoryContractError("库存搜索控件结构未识别，未开始全量扫描")
        if search['active']:
            if navigated:
                raise InventoryContractError("库存搜索条件重置后仍然生效，未开始全量扫描")
            # A DOM-only search can persist even while the address bar looks normal.
            # Use the one user-provided URL once, not guessed query parameter names.
            page = await self._land_inventory(page, run, marketplace)
            await self._wait_list(page)
            await self._require_empty_search(page)
        if not self._has_full_inventory_query(page):
            raise InventoryContractError("库存筛选条件重置后仍不符合全量扫描要求")
        await self._identity(page, run)
        await self._accept_forbidden_dom(run, None)
        return self._remember(source, page, run, marketplace)

    @staticmethod
    def _has_full_inventory_query(page: Any) -> bool:
        u = urlparse(str(page.url))
        query = parse_qs(u.query)
        return (u.scheme == 'https' and u.hostname in INVENTORY_MARKETPLACES.values()
                    and not u.port and not u.params and not u.username and not u.password
                   and u.path == INVENTORY_PATH and query.get('status') == ['approval_required']
                   and query.get('fulfilledBy') == ['all'] and query.get('pageSize') == ['100']
                   and query.get('sort') == ['date_created_desc']
                   and set(query) <= {'fulfilledBy','page','pageSize','sort','status'})

    async def _land_inventory(self, page: Any, run: Any, marketplace: Any) -> Any:
        source = page
        self._active_run = run
        await self._check_cancelled()
        host = self._trusted_host(page)
        outcome = await self.landing.land_on(
            page, url=INVENTORY_URL.replace(INVENTORY_DOMAIN, host), expected_path=INVENTORY_PATH, domain=host,
        )
        page = outcome.page or page
        self._remember(source, page, run, marketplace)
        if not outcome.ok:
            if outcome.reason == 'auth_required':
                raise HumanAuthRequired("库存流程等待当前商城登录完成", kind="signin")
            raise InventoryContractError("库存页未完成登录或选站")
        page = await self._session(page, run, marketplace)
        return self._remember(source, page, run, marketplace)

    async def _read_search_state(self, page: Any) -> dict[str, Any]:
        return await self._evaluate(self.resolved_page_for(page), _SEARCH)

    async def _require_empty_search(self, page: Any) -> None:
        search = await self._read_search_state(page)
        if not search.get('recognized') or search.get('active'):
            raise InventoryContractError("库存搜索条件未清空，不能把筛选子集作为全量候选")

    async def _evaluate(self, page: Any, script: str, arg: Any = None) -> Any:
        async with asyncio.timeout(self.timing.panel_timeout_seconds):
            await self._dismiss_announcements(page)
            return await page.evaluate(script, arg)

    async def _dismiss_announcements(self, page: Any) -> None:
        # Only the inventory route opts in. Do not touch login, payment or
        # compliance submission destinations, even when their title overlaps.
        u = urlparse(str(page.url))
        if (u.scheme != 'https' or u.hostname not in INVENTORY_MARKETPLACES.values()
                or u.port or u.params or u.username or u.password or u.path != INVENTORY_PATH):
            return
        try:
            await dismiss_inventory_announcements(page, self.timing, cancel_check=self._check_cancelled)
        except AnnouncementBlocked as exc:
            raise InventoryContractError(str(exc)) from None

    async def _bounded_ui(self, operation: Awaitable[Any]) -> Any:
        """Locator reads do not accept native timeouts; bound their CDP request."""
        try:
            async with asyncio.timeout(self.timing.panel_timeout_seconds):
                return await operation
        except TimeoutError as exc:
            raise ItemLoadError("商品页面控件读取或输入超时") from exc

    async def _poll(self, read: Callable[[], Awaitable[Any]], accept: Callable[[Any], bool],
                    timeout: float, *, message: str, contract: bool = False,
                    resume_after_auth: bool = False) -> Any:
        last_key = None
        stable_since = 0.0
        remaining = timeout
        auth_recoveries = 0
        while remaining > 0:
            started = time.monotonic()
            try:
                async with asyncio.timeout(remaining):
                    while True:
                        value = await read()
                        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                        now = time.monotonic()
                        if accept(value):
                            if key != last_key:
                                last_key, stable_since = key, now
                            if now - stable_since >= self.timing.stable_seconds:
                                return value
                        else:
                            last_key = None
                        await asyncio.sleep(self.timing.poll_seconds)
            except _AuthenticationNeeded as auth:
                remaining -= time.monotonic() - started
                if auth_recoveries:
                    raise HumanAuthRequired("当前商城登录完成后再次要求验证", kind="signin")
                auth_recoveries += 1
                # The existing login helper may wait 60s for managed OTP input.
                # Do not cancel it at the unrelated 45s sidebar/list deadline.
                await self._session(auth.page, auth.run, auth.marketplace)
                last_key = None
                if not resume_after_auth:
                    raise _AuthenticationResumed()
            except TimeoutError as exc:
                raise (InventoryContractError if contract else ItemLoadError)(message) from exc
        raise (InventoryContractError if contract else ItemLoadError)(message)

    async def _read_list(self, page: Any) -> dict[str, Any]:
        await self._diagnose_forbidden_response()
        self._require_inventory_http_ok(allow_forbidden_read=True)
        page = self.resolved_page_for(page)
        if page in self._contexts:
            if AmazonLoginAdvancer.is_supported_login_url(str(page.url), expected_host=self._trusted_host(page)):
                raise _AuthenticationNeeded(page, *self._contexts[page])
            page = await self._session(page, *self._contexts[page])
        state = await self._evaluate(page, _LIST)
        self._require_inventory_http_ok(allow_forbidden_read=True)
        return state

    @staticmethod
    def _matches_list_filter(state: dict[str, Any], expected: str) -> bool:
        if expected == 'ApprovalRequired':
            return state.get('filters', []).count(expected) == 1
        # All is common to several dropdowns. Read the identified status control.
        return state.get('statusFilter') == expected

    async def _wait_list(self, page: Any, *, expected_filter: str = "ApprovalRequired") -> InventoryPageSnapshot:
        started = time.monotonic()
        progress_at, last_count, expected_shape = started, -1, None
        async def load():
            nonlocal progress_at, last_count, expected_shape
            state = await self._read_list(page)
            if not state.get('ready') or state.get('loading'):
                return state
            if not self._matches_list_filter(state, expected_filter):
                raise InventoryContractError("当前页面不是需要批准筛选" if expected_filter == "ApprovalRequired" else "历史核对商品状态筛选不符")
            try:
                shape = tuple(int(state[k]) for k in ('page', 'total', 'size'))
            except (KeyError, ValueError, TypeError):
                raise InventoryContractError("库存分页数据未识别") from None
            if expected_shape is None:
                expected_shape = shape
            elif shape != expected_shape:
                if shape[0] == expected_shape[0] and shape[2] == expected_shape[2]:
                    raise InventoryScanChanged(expected_shape[1], shape[1], shape[0], shape[2])
                raise InventoryContractError("懒加载期间分页发生变化，扫描不完整")
            count = len(state.get('items', []))
            if count < last_count:
                raise InventoryContractError("懒加载期间商品行被移除，扫描不完整")
            if count > last_count:
                last_count, progress_at = count, time.monotonic()
            number, total, size = shape
            expected = max(0, min(size, total - (number - 1) * size))
            if count > expected:
                raise InventoryContractError("库存行数超过分页声明数量")
            if count < expected:
                if time.monotonic() - progress_at >= self.timing.lazy_no_progress_seconds:
                    raise InventoryContractError("库存滚动加载无进展，候选扫描不完整")
                if count:
                    await self._bounded_ui(page.locator('[data-sku]').last.scroll_into_view_if_needed(
                        timeout=int(self.timing.click_timeout_seconds * 1000)))
            return state
        def ready(s: dict[str, Any]) -> bool:
            if not s.get('ready') or s.get('loading') or not self._matches_list_filter(s, expected_filter):
                return False
            try:
                p, total, size = (int(s[k]) for k in ('page', 'total', 'size'))
            except (ValueError, TypeError, KeyError):
                return False
            return (p >= 1 and total >= 0 and size > 0 and p <= max(1, math.ceil(total / size)) and
                    len(s.get('items', [])) == max(0, min(size, total - (p - 1) * size)))
        raw = await self._poll(
            load,
            ready,
            self.timing.lazy_page_timeout_seconds,
            message="需要批准列表未完成加载或实际筛选不符", contract=True, resume_after_auth=True,
        )
        page_number, total, size = (int(raw[x]) for x in ('page', 'total', 'size'))
        if page_number < 1 or total < 0 or size <= 0:
            raise InventoryContractError("库存分页结构不符合预期")
        items = tuple(InventoryItem(str(r['sku']), str(r['asin']), page_number,
                                    int(r['position']), str(r.get('title') or '')[:300])
                      for r in raw['items'])
        if any(not i.sku or not re.fullmatch(r'[A-Z0-9]{10}', i.asin) for i in items):
            raise InventoryContractError("库存商品缺少稳定 SKU 或 ASIN")
        if len({i.sku for i in items}) != len(items):
            raise InventoryContractError("库存当前页存在重复 SKU")
        expected = max(0, min(size, total - (page_number - 1) * size))
        if len(items) != expected:
            raise InventoryContractError("库存总数与完整加载后的商品行数不一致")
        return InventoryPageSnapshot(page_number, total, size, items)

    async def _turn_page(self, page: Any, current: InventoryPageSnapshot,
                         target: int) -> InventoryPageSnapshot:
        page = self.resolved_page_for(page)
        target = min(max(1, target), max(1, math.ceil(current.total_items / current.items_per_page)))
        attempts = abs(current.page_number - target)
        for _ in range(attempts):
            if current.page_number == target:
                break
            forward = current.page_number < target
            nav = page.locator('kat-pagination').locator(
                '[part="pagination-nav-right"]' if forward else '[part="pagination-nav-left"]'
            )
            if (await self._bounded_ui(nav.count()) != 1 or
                    await self._bounded_ui(nav.get_attribute('tabindex')) == '-1'):
                raise InventoryContractError("库存分页按钮缺失或无法前进")
            await self._click_unique(nav)
            before, before_total = current.page_number, current.total_items
            await self._poll(lambda: self._read_list(page),
                lambda s: bool(s.get('ready') and not s.get('loading') and s.get('page') != before),
                self.timing.list_timeout_seconds, message="库存翻页停滞", contract=True, resume_after_auth=True)
            current = await self._wait_list(page)
            if current.page_number != before + (1 if forward else -1):
                if current.total_items != before_total:
                    raise InventoryScanChanged(before_total, current.total_items, current.page_number, current.items_per_page)
                raise InventoryContractError("库存分页发生非预期跳转")
            target = min(target, max(1, math.ceil(current.total_items / current.items_per_page)))
        return current

    async def scan_candidates(self, page: Any, run: Any, marketplace: Any) -> tuple[InventoryItem, ...]:
        for attempt in range(self.timing.scan_restart_attempts + 1):
            await self._check_cancelled()
            try:
                # Each attempt has its own list and seen set. No partial merge.
                return await self._scan_candidates_once(self.resolved_page_for(page), run, marketplace)
            except InventoryScanChanged as exc:
                exhausted = attempt >= self.timing.scan_restart_attempts
                if self.on_scan_event is not None:
                    await self.on_scan_event(run, marketplace, "exhausted" if exhausted else "restart",
                                             {**exc.details, "restart": attempt + (not exhausted)})
                if exhausted:
                    raise PageRecoveryRequired("候选数量持续变化，已保留其他站点进度",
                                               reason_code="inventory_scan_unstable") from exc
                await asyncio.sleep(self.timing.scan_restart_delay_seconds)
                await self._check_cancelled()
        raise AssertionError("unreachable")

    async def _scan_candidates_once(self, page: Any, run: Any, marketplace: Any) -> tuple[InventoryItem, ...]:
        page = await self._session(page, run, marketplace)
        search = await self._read_search_state(page)
        if not self._has_full_inventory_query(page) or search.get('active'):
            page = await self.open_inventory(page, run, marketplace)
        else:
            await self._require_empty_search(page)
        current = await self._wait_list(page)
        current = await self._turn_page(page, current, 1)
        total, size = current.total_items, current.items_per_page
        all_items: list[InventoryItem] = []
        seen: set[str] = set()
        for number in range(1, max(1, math.ceil(total / size)) + 1):
            await self._require_empty_search(page)
            if current.items_per_page != size:
                raise InventoryContractError("扫描期间每页大小变化，候选清单不完整")
            if current.total_items != total:
                raise InventoryScanChanged(total, current.total_items, current.page_number, size)
            if current.page_number != number:
                raise InventoryContractError("扫描期间页码变化，候选清单不完整")
            for item in current.items:
                if item.sku in seen:
                    raise InventoryContractError("扫描出现重复页或重复 SKU")
                seen.add(item.sku)
                all_items.append(item)
            if number * size < total:
                current = await self._turn_page(page, current, number + 1)
        if len(all_items) != total:
            raise InventoryContractError("库存候选扫描不完整")
        return tuple(all_items)

    async def locate_item(self, page: Any, item: InventoryItem, *, search: bool = False) -> bool:
        try:
            return await self._locate_item(page, item, search=search)
        except InventoryCsrfHeaderLoss:
            page = self.resolved_page_for(page)
            if page not in self._contexts:
                raise
            page = await self._recover_csrf_page(page, *self._contexts[page], item)
            return await self._locate_item(page, item, search=search)

    async def _search_inventory(self, page: Any, sku: str, *,
                                expected_filter: str = "ApprovalRequired") -> InventoryPageSnapshot:
        # Explicit SKU search only for the residual pass, never guessed business URLs.
        group = page.locator('kat-input-group').filter(visible=True).filter(
            has=page.locator('kat-option[value="SKU"]'))
        dropdown = group.locator('kat-dropdown').filter(has=page.locator('kat-option[value="SKU"]'))
        if await self._bounded_ui(dropdown.count()) != 1:
            raise InventoryContractError("SKU 搜索控件结构未识别")
        query = await self._read_search_state(page)
        if query.get('mode') != 'SKU':
            await self._click_unique(dropdown)
            option = dropdown.locator('kat-option[value="SKU"]')
            if await self._bounded_ui(option.count()) != 1:
                raise InventoryContractError("SKU 搜索选项不唯一")
            await self._click_unique(option)
        # KAT mirrors the placeholder on its host as well as the native input.
        # Filling get_by_placeholder can therefore select a non-editable host.
        entry = group.locator('kat-input[type="text"][maxlength="1000"] input').filter(visible=True)
        if await self._bounded_ui(entry.count()) != 1:
            raise InventoryContractError("SKU 搜索输入框不唯一")
        await self._check_cancelled()
        await self._bounded_ui(entry.fill(sku, timeout=int(self.timing.panel_timeout_seconds * 1000)))
        token = uuid4().hex
        await self._evaluate(page, _INSTALL_SEARCH_TRACE, token)
        try:
            await self._bounded_ui(entry.press('Enter', timeout=int(self.timing.panel_timeout_seconds * 1000)))
            async def search_result() -> dict[str, Any]:
                query = await self._read_search_state(page)
                if (not query.get('recognized') or query.get('mode') != 'SKU'
                        or query.get('values') != [sku]):
                    raise InventoryContractError("当前 SKU 搜索条件未与候选商品绑定")
                return await self._read_list(page)
            # Filling the field does not prove that Enter's asynchronous
            # filtering finished. Require a fresh list render, never old rows.
            try:
                await self._poll(search_result,
                    lambda s: bool(s.get('ready') and not s.get('loading') and
                        s.get('searchToken') == token and s.get('searchFresh') and
                        s.get('page') == 1 and (not sku or s.get('total') == len(s.get('items', []))) and
                        (not sku or all(r.get('sku') == sku for r in s.get('items', [])))),
                    self.timing.list_timeout_seconds, message="SKU 搜索结果尚未完成更新",
                    resume_after_auth=True)
            except ItemLoadError as exc:
                # A stale render is not proof of absence or of identity mismatch.
                # Keep explicit route/filter/SKU/ASIN contract errors fatal.
                raise ItemSearchLoadError("SKU 搜索结果未在期限内更新，未打开或提交该商品") from exc
            current = await self._wait_list(page, expected_filter=expected_filter)
            if sku and any(i.sku != sku for i in current.items):
                raise InventoryContractError("SKU 搜索结果与候选商品不一致")
        finally:
            try:
                async with asyncio.timeout(min(1.0, self.timing.panel_timeout_seconds)):
                    await page.evaluate(_CLEAR_SEARCH_TRACE, token)
            except Exception as exc:
                logger.warning('inventory_search_trace_cleanup_failed type=%s', type(exc).__name__)
        return current

    async def _locate_item(self, page: Any, item: InventoryItem, *, search: bool = False) -> bool:
        page = self.resolved_page_for(page)
        if search:
            current = await self._search_inventory(page, item.sku)
        else:
            if page in self._contexts:
                # A preceding readback may have left a SKU search active. It must
                # not restrict the next snapshot item or its original page lookup.
                search_state = await self._read_search_state(page)
                if not self._has_full_inventory_query(page) or search_state.get('active'):
                    page = await self.open_inventory(page, *self._contexts[page])
                else:
                    await self._require_empty_search(page)
            current = await self._wait_list(page)
            if not any(i.sku == item.sku for i in current.items):
                current = await self._turn_page(page, current, item.page_number)
        match = [i for i in current.items if i.sku == item.sku]
        if match and match[0].asin != item.asin:
            raise InventoryContractError("同一 SKU 的 ASIN 已变化")
        return len(match) == 1

    async def _panel_snapshot(self, page: Any, run: Any, marketplace: Any) -> dict[str, Any]:
        await self._diagnose_forbidden_response()
        self._require_inventory_http_ok(allow_forbidden_read=True)
        old = self.resolved_page_for(page)
        if AmazonLoginAdvancer.is_supported_login_url(str(old.url), expected_host=self._trusted_host(old)):
            raise _AuthenticationNeeded(old, run, marketplace)
        page = await self._session(page, run, marketplace)
        state = await self._evaluate(page, _PANEL, {
            'panel': PANEL_SELECTOR, 'receipt': SUBMISSION_RECEIPT, 'receipts': SUBMISSION_RECEIPTS,
            'personReceipts': RESPONSIBLE_PERSON_RECEIPTS,
            'attestationReceipts': ATTESTATION_REVIEW_RECEIPTS,
            'traceToken': self._open_tokens.get(page, ''),
        })
        self._require_inventory_http_ok(allow_forbidden_read=True)
        return state

    @staticmethod
    def _bound(state: dict[str, Any], item: InventoryItem) -> bool:
        return state.get('skus') == [item.sku] and (state.get('asins') == [item.asin] or
            ((state.get('responsiblePerson') or state.get('manufacturerInformation')) and not state.get('asins') and state.get('rowAsins') == [item.asin]))

    async def _wait_panel(self, page: Any, run: Any, marketplace: Any, item: InventoryItem,
                          accept: Callable[[dict[str, Any]], bool], *, allow_sku_only_skip: bool = False,
                          allow_no_action_skip: bool = False) -> dict[str, Any]:
        last: dict[str, Any] = {}
        async def read() -> dict[str, Any]:
            nonlocal last
            last = await self._panel_snapshot(page, run, marketplace)
            self._last_panel = last
            return last
        try:
            result = await self._poll(read,
                lambda s: bool(s.get('ready') and not s.get('loading') and
                    (self._network_failure != 403 or self._positive_panel_content(s)) and
                    (self._bound(s, item) or (allow_sku_only_skip and s.get('knownOther')
                     and s.get('skus') == [item.sku] and not s.get('asins')) or
                     (allow_no_action_skip and s.get('freshNoAction') and
                      s.get('openedSku')==item.sku and s.get('openedAsin')==item.asin)) and accept(s)),
                self.timing.panel_timeout_seconds, message="商品侧栏加载超时或仍为上一商品")
            if self._network_failure == 403:
                await self._identity(self.resolved_page_for(page), run)
                await self._accept_forbidden_dom(run, item)
            return result
        except ItemLoadError as exc:
            if last.get('skus') and last.get('asins') and not self._bound(last, item):
                raise InventoryContractError("侧栏仍属于其他商品，已停止后续提交") from exc
            if self._network_failure == 403:
                raise InventoryItemHttpError("HTTP 403 后当前商品侧栏仍未取得有效内容") from exc
            raise ItemPanelLoadError("商品侧栏加载超时，未取得当前商品的稳定内容") from exc

    def _panel(self, page: Any) -> Any:
        return self.resolved_page_for(page).locator(PANEL_SELECTOR).filter(visible=True)

    async def _click_unique(self, locator: Any) -> None:
        try:
            async with asyncio.timeout(self.timing.panel_timeout_seconds):
                if await locator.count() != 1:
                    raise InventoryContractError("商品操作入口不唯一")
                await asyncio.sleep(self.timing.action_interval_seconds)
                await self._check_cancelled()
                # Announcements may appear during the action interval after a
                # successful DOM read. Recheck just before the ordinary click.
                if getattr(locator, 'page', None) is not None:
                    await self._dismiss_announcements(locator.page)
                await locator.click(timeout=int(min(self.timing.click_timeout_seconds,
                                                   self.timing.panel_timeout_seconds) * 1000))
        except Exception as exc:
            if type(exc).__name__ == 'TimeoutError':
                raise ItemClickError("商品操作入口点击未完成，可能被侧栏遮挡或尚不可点击") from exc
            raise

    async def _prepare_item_entry(self, page: Any, run: Any, marketplace: Any,
                                  item: InventoryItem, entry: Any) -> None:
        """Recover an open overlay through its real UI; never force a row click."""
        self._item_stage = "entry"
        try:
            state = await self._panel_snapshot(page, run, marketplace)
        except _AuthenticationNeeded as auth:
            await self._session(auth.page, auth.run, auth.marketplace)
            raise _AuthenticationResumed() from None
        except TimeoutError as exc:
            raise ItemPanelLoadError("打开商品前侧栏状态读取超时") from exc
        self._last_panel = state
        if not state.get("ready"):
            if state.get("count", 0) > 1:
                raise InventoryContractError("存在多个商品侧栏，未点击商品入口")
            return
        reset = bool(self._http_retries.get(self._retry_key(run, item), 0) or self._bound(state, item))
        if not reset:
            try:
                await self._bounded_ui(entry.click(trial=True, timeout=int(min(
                    self.timing.click_timeout_seconds, self.timing.panel_timeout_seconds) * 1000)))
            except Exception as exc:
                if isinstance(exc, ItemLoadError) or type(exc).__name__ == 'TimeoutError':
                    reset = True
                else:
                    raise
        if not reset:
            return
        self._item_stage = "close_sidebar"
        close = self._panel(page).locator('button[part="panel-close-button"]').filter(visible=True)
        if (state.get('personReceipt') or state.get('manufacturerReceipt') or state.get('safetyReceipt')) and await self._bounded_ui(close.count()) == 0:
            # Completed typed receipts use a footer Close instead of the X.
            close = self._panel(page).locator('kat-button').filter(visible=True).filter(
                has_text=re.compile(r'^(?:关闭|關閉|Close)$'))

        # Observed path: form back-button -> requirements primary 返回 ->
        # first-layer close. Two distinct transitions, never an unbounded back loop.
        for _ in range(2):
            if await self._bounded_ui(close.count()) != 0:
                break
            from_form = bool(state.get('formCount'))
            if from_form:
                self._validate_form(state)
                selector = 'kat-button[data-testid="back-button"][label="返回"]'
            elif state.get('responsiblePerson') or state.get('knownOther'):
                selector = 'button[aria-label="back"],kat-button[data-testid="primary-button"][label="返回"]'
            elif state.get('resolution') or state.get('receipt'):
                selector = 'kat-button[data-testid="primary-button"][label="返回"]'
            else:
                break
            back = self._panel(page).locator(selector).filter(visible=True)
            back_count = await self._bounded_ui(back.count())
            self._last_panel = {**state, "backCount": back_count}
            if back_count != 1:
                break
            self._item_stage = "return_sidebar"
            await self._click_unique(back)
            try:
                state = await self._poll(
                    lambda: self._panel_snapshot(page, run, marketplace),
                    lambda s: (not s.get('ready') and s.get('count') == 0) or
                        bool(s.get('ready') and not s.get('loading') and
                             (s.get('firstLayer') or s.get('noAction') or
                              (from_form and s.get('resolution') and not s.get('formCount')))),
                    self.timing.panel_timeout_seconds, message="侧栏返回后上一级内容未恢复")
                self._last_panel = state
            except ItemLoadError as exc:
                raise ItemPanelResetError("侧栏返回未完成，未重复点击商品入口") from exc
            if not state.get('ready'):
                await self._item_event(run, item, "sidebar_closed")
                self._item_stage = "entry"
                return
            self._item_stage = "close_sidebar"
        close_count = await self._bounded_ui(close.count())
        self._last_panel = {**self._last_panel, "closeCount": close_count}
        if close_count != 1:
            raise ItemPanelResetError("当前侧栏遮挡入口或需要重新打开，但未找到唯一可见的关闭按钮")
        await self._click_unique(close)
        try:
            self._last_panel = await self._poll(
                lambda: self._panel_snapshot(page, run, marketplace),
                lambda s: not s.get('ready') and s.get('count') == 0,
                self.timing.panel_timeout_seconds, message="侧栏关闭后仍遮挡商品列表")
        except ItemLoadError as exc:
            raise ItemPanelResetError("侧栏关闭未完成，未重复点击商品入口") from exc
        await self._item_event(run, item, "sidebar_closed")
        self._item_stage = "entry"

    def _retry_key(self, run: Any, item: InventoryItem) -> tuple[str, str, str, str, str]:
        return (str(run.id), getattr(self._active_marketplace, "code", ""),
                item.sku, item.asin, item.requirement_type)

    async def inspect_item(self, page: Any, run: Any, marketplace: Any, item: InventoryItem) -> Inspection:
        self._active_marketplace = marketplace
        key = self._retry_key(run, item)
        while True:
            self._item_stage, self._last_panel = "entry", {}
            try:
                await self._diagnose_forbidden_response()
                self._require_inventory_http_ok(allow_forbidden_read=True)
                result = await self._inspect_with_auth(page, run, marketplace, item)
                self._inspected_items[self.resolved_page_for(page)] = (item.sku, item.asin, item.requirement_type)
                return result
            except InventoryCsrfHeaderLoss:
                page = await self._recover_csrf_item(page, run, marketplace, item)
            except ItemEntryError:
                # A settled row with no unique entry is not a transient HTTP
                # failure. Preserve its reason and continue via the item-local exit.
                raise
            except ItemLoadError as exc:
                # Normal timeouts retain per-item handling. Contract/identity
                # failures and save() never enter this read-only retry loop.
                await self._diagnose_forbidden_response()
                if self._network_failure == 403 and self._csrf_header_loss:
                    page = await self._recover_csrf_item(page, run, marketplace, item)
                    continue
                self._require_inventory_http_ok(allow_forbidden_read=True)
                used = self._http_retries.get(key, 0)
                forbidden = not isinstance(exc, ItemClickError) and self._network_failure == 403
                if forbidden and not isinstance(exc, InventoryItemHttpError):
                    exc = InventoryItemHttpError("HTTP 403 后当前商品未取得有效内容")
                await self._item_event(run, item, "read_failed", exc)
                if not forbidden and not used and not isinstance(exc, ItemClickError):
                    raise
                if used >= self.timing.http_retry_attempts:
                    await self._item_event(run, item, "exhausted", exc)
                    if forbidden:
                        await self._http_event(run, item, "exhausted")
                    if self._network_failure == 403:
                        self._clear_forbidden_response()
                    raise exc
                self._http_retries[key] = used + 1
                await self._item_event(run, item, "retry", exc)
                if forbidden:
                    await self._http_event(run, item, "retry")
                await self._check_cancelled()
                await asyncio.sleep(self.timing.http_retry_delay_seconds * (used + 1))
                await self._check_cancelled()
                # Keep a subsequent rate-limit/server failure while waiting.
                self._require_inventory_http_ok(allow_forbidden_read=True)
                page = self.resolved_page_for(page)
                self._require_route(page)
                await self._identity(page, run)
                if self._network_failure == 403:
                    self._clear_forbidden_response()
                # Only re-open this row's sidebar on the same page.

    async def _recover_csrf_item(self, page: Any, run: Any, marketplace: Any, item: InventoryItem) -> Any:
        page = await self._recover_csrf_page(page, run, marketplace, item)
        if (not await self.locate_item(page, item)
                and not await self.locate_item(page, item, search=True)):
            raise ItemLoadError("恢复页面后当前商品已不在列表，未执行保存")
        return self.resolved_page_for(page)

    async def _inspect_with_auth(self, page: Any, run: Any, marketplace: Any, item: InventoryItem) -> Inspection:
        # A login redirect can happen between any two sidebar stages. One successful
        # reauthentication permits re-opening that same item, not submitting it again.
        for attempt in range(2):
            try:
                return await self._inspect_item(page, run, marketplace, item)
            except _AuthenticationResumed:
                page = self.resolved_page_for(page)
                if attempt or not await self.locate_item(page, item):
                    raise ItemLoadError("重新登录后当前商品侧栏未恢复")
        raise ItemLoadError("重新登录后当前商品侧栏未恢复")

    @staticmethod
    def _require_item_entry(state: dict[str, Any]) -> None:
        if state.get('ok'):
            return
        reason = state.get('reason')
        if reason in {'entry_unrecognized', 'entry_ambiguous'}:
            raise ItemEntryError(ambiguous=reason == 'entry_ambiguous')
        if reason == 'row_missing':
            raise ItemLoadError("候选商品当前未在列表中")
        if reason == 'row_ambiguous':
            raise InventoryContractError("侧栏打开前商品行不唯一")
        if reason == 'asin_changed':
            raise InventoryContractError("侧栏打开前商品 ASIN 不唯一或已变化")
        raise InventoryContractError("商品入口核验返回未知状态")

    async def _inspect_item(self, page: Any, run: Any, marketplace: Any, item: InventoryItem) -> Inspection:
        page = await self._session(page, run, marketplace)
        await self._identity(page, run)
        state = await self._panel_snapshot(page, run, marketplace)
        previous = self._inspected_items.get(page)
        if (previous is not None and previous[:2] == (item.sku, item.asin) and previous[2] != item.requirement_type
                and self._bound(state, item) and not state.get('loading')):
            state = await self._wait_panel(page, run, marketplace, item,
                lambda s: self._positive_panel_content(s))
            if (state.get('firstLayer') or
                self._party_matches(state, item.requirement_type) or
                ((state.get('formCount') or state.get('cardCount') or state.get('resolution')) and item.requirement_type == SAFETY_ATTESTATION)):
                return await self._inspect_loaded_panel(page, run, marketplace, item, state)
            if not (state.get('personReceipt') or state.get('manufacturerReceipt') or state.get('safetyReceipt')):
                state = await self._return_to_requirements(page, run, marketplace, item)
                return await self._inspect_loaded_panel(page, run, marketplace, item, state)
            # The previous requirement's receipt has Close, not Back. Reopen
            # this row once to inspect its other requirement, without saving again.
        # Use the same exact entry contract for initial classification and the
        # final trace. Missing entries must not spend time resetting a valid
        # previous sidebar, or masquerade as an ASIN/seller identity mismatch.
        token = uuid4().hex
        payload = {'panel': PANEL_SELECTOR, 'sku': item.sku, 'asin': item.asin, 'token': token}
        self._require_item_entry(await self._evaluate(
            page, _INSTALL_OPEN_TRACE, {**payload, 'checkOnly': True}))
        # JSON quoting also safely handles SKUs containing quotes/backslashes in CSS strings.
        row = page.locator(f'[data-sku={json.dumps(item.sku, ensure_ascii=False)}]').filter(visible=True)
        entry = row.locator(','.join(f'kat-link[label={json.dumps(label, ensure_ascii=False)}]' for label in ENTRY_LABELS)).filter(visible=True)
        await self._prepare_item_entry(page, run, marketplace, item, entry)
        self._require_item_entry(await self._evaluate(page, _INSTALL_OPEN_TRACE, payload))
        self._open_tokens[page] = token
        try:
            await self._click_unique(entry)
            await self._item_event(run, item, "entry_clicked")
            self._item_stage = "first_layer"
            first = await self._wait_panel(page, run, marketplace, item,
                lambda s: bool(s.get('freshOpen') and (s.get('firstLayer') or s.get('freshNoAction'))),
                allow_no_action_skip=True)
            await self._item_event(run, item, "panel_loaded")
        finally:
            self._open_tokens.pop(page, None)
            try:
                async with asyncio.timeout(min(1.0, self.timing.panel_timeout_seconds)):
                    await page.evaluate(_CLEAR_OPEN_TRACE, token)
            except Exception as exc:
                # Cleanup failure is bounded and not a reason to repeat a click.
                logger.warning("inventory_open_trace_cleanup_failed type=%s", type(exc).__name__)
        if first.get('freshNoAction'):
            return Inspection('no_violation_entry',
                '平台表示无需采取任何操作，此前更改可能仍在处理中', self.resolved_page_for(page), requirements_complete=True)
        return await self._inspect_loaded_panel(page, run, marketplace, item, first)

    async def _inspect_loaded_panel(self, page: Any, run: Any, marketplace: Any,
                                    item: InventoryItem, second: dict[str, Any]) -> Inspection:
        """Continue an identity-bound, stable sidebar without reopening its row."""
        wanted = item.requirement_type
        if self._receipt_matches(page, item, second):
            return Inspection('already_submitted', '平台显示审核中', self.resolved_page_for(page))
        if self._party_matches(second, wanted):
            return self._inspect_person(page, item, second)
        if second.get('formCount'):
            if wanted == SAFETY_ATTESTATION:
                self._validate_form(second)
                self._panel_requirement[page] = wanted
                return Inspection('eligible', '已核对安全声明表单', self.resolved_page_for(page),
                                  bool(second['boxes'][0]['checked'] or second['boxes'][0]['value'] == 'true'))
            second = await self._return_to_requirements(page, run, marketplace, item)
        if second.get('firstLayer'):
            if second['violationCount'] == 0:
                return Inspection('no_violation_entry', '没有商品信息违规处理入口', self.resolved_page_for(page), requirements_complete=True)
            count = second['violationCount']
            for index in range(count):
                entries = self._panel(page).get_by_text(re.compile('^(?:' + '|'.join(VIOLATION_LABELS) + ')$')).filter(visible=True)
                if await self._bounded_ui(entries.count()) != count:
                    raise InventoryContractError("商品违规要求在检查期间发生变化")
                self._panel_requirement.pop(page, None)
                self._item_stage = "violation_entry"
                await self._click_unique(entries.nth(index))
                self._item_stage = "violation_content"
                second = await self._wait_panel(page, run, marketplace, item,
                    lambda s: bool(s.get('cardCount') or s.get('knownOther') or s.get('resolution') or s.get('receipt')),
                    allow_sku_only_skip=True)
                if second.get('manufacturerInformation') and MANUFACTURER not in getattr(run, 'workflow_config', {}).get('processing_types', (SAFETY_ATTESTATION,)):
                    await self._item_event(run, item, 'manufacturer_skipped')
                if self._party_matches(second, wanted):
                    return self._inspect_person(page, item, second)
                if wanted == SAFETY_ATTESTATION and (second.get('cardCount') or second.get('resolution')):
                    break
                if index + 1 < count:
                    second = await self._return_to_requirements(page, run, marketplace, item)
            else:
                return Inspection('no_attestation' if wanted == SAFETY_ATTESTATION else 'not_applicable',
                                  '已检查全部违规入口，没有本项处理要求', page, requirements_complete=True)
        if wanted in PARTY_TYPES:
            return Inspection('not_applicable', '当前要求不是' + REQUIREMENT_LABELS[wanted], page)
        self._panel_requirement[page] = SAFETY_ATTESTATION
        if self._receipt_matches(page, item, second):
            return Inspection('already_submitted', '平台显示审核中', self.resolved_page_for(page))
        if second.get('cardCount', 0) == 0:
            return Inspection('no_attestation', '当前违规要求没有安全证明', self.resolved_page_for(page))
        if second['cardCount'] != 1:
            raise InventoryContractError("安全证明入口不唯一")
        self._item_stage = "attestation_entry"
        await self._click_unique(self._panel(page).get_by_role('button', name=re.compile('^(安全证明|安全證明)$')))
        self._item_stage = "attestation_form"
        form = await self._wait_panel(page, run, marketplace, item,
            lambda s: bool(s.get('receipt') or s.get('formCount')))
        if form.get('receipt'):
            return Inspection('already_submitted', '平台显示审核中', self.resolved_page_for(page))
        self._validate_form(form)
        return Inspection('eligible', '已核对安全声明表单', self.resolved_page_for(page),
                          bool(form['boxes'][0]['checked'] or form['boxes'][0]['value'] == 'true'))

    @staticmethod
    def _party_matches(state: dict[str, Any], requirement_type: str) -> bool:
        field = {RESPONSIBLE_PERSON: 'responsiblePerson', MANUFACTURER: 'manufacturerInformation'}.get(requirement_type)
        return bool(field and state.get(field))

    def _inspect_person(self, page: Any, item: InventoryItem, state: dict[str, Any]) -> Inspection:
        if not self._party_matches(state, item.requirement_type):
            raise InventoryContractError("关联要求类型不匹配")
        if self._bound(state, item) and self._receipt_matches(page, item, state):
            return Inspection('already_submitted', REQUIREMENT_LABELS[item.requirement_type] + '提交正在审核中', page)
        records = state.get('personRecords', [])
        self._panel_requirement[page] = item.requirement_type
        self._person_selection.pop(page, None)
        self._person_prepared.pop(page, None)
        if not self._bound(state, item):
            raise InventoryContractError("关联信息侧栏商品身份未绑定")
        if any(r.get('selected') for r in records):
            return Inspection('already_linked', '进入页面时已有选中项，本次不送出', page, True)
        if not records:
            return Inspection('no_records', REQUIREMENT_LABELS[item.requirement_type] + '没有记录，不自动创建', page, False)
        selectable = [r for r in records if not r.get('invalid') and not r.get('disabled') and r.get('radioCount') == 1]
        if not selectable:
            return Inspection('invalid_record', REQUIREMENT_LABELS[item.requirement_type] + '记录失效或控件不可操作', page, False)
        record = selectable[0]
        if (len(state.get('personSaves', [])) != 1 or not record.get('key')
                or sum(r.get('key') == record['key'] for r in records) != 1):
            raise InventoryContractError("关联信息送出表单结构未识别")
        self._person_selection[page] = (item.sku, item.asin, record['key'], item.requirement_type)
        return Inspection('eligible', '将选择页面第一条可操作且未失效的' + REQUIREMENT_LABELS[item.requirement_type] + '记录', page, False)

    def _receipt_matches(self, page: Any, item: InventoryItem, state: dict[str, Any]) -> bool:
        if state.get('safetyReceipt'):
            return item.requirement_type == SAFETY_ATTESTATION
        if state.get('personReceipt'):
            return item.requirement_type == RESPONSIBLE_PERSON
        if state.get('manufacturerReceipt'):
            return item.requirement_type == MANUFACTURER
        if (state.get('responsiblePerson') or state.get('manufacturerInformation')) and not self._party_matches(state, item.requirement_type):
            return False
        if item.requirement_type == SAFETY_ATTESTATION and state.get('cardSubmitted'):
            return True
        return bool(state.get('receipt') and self._panel_requirement.get(page) == item.requirement_type)

    async def _return_to_requirements(self, page: Any, run: Any, marketplace: Any, item: InventoryItem) -> dict[str, Any]:
        for _ in range(3):
            state = await self._panel_snapshot(page, run, marketplace)
            if state.get('firstLayer') and self._bound(state, item) and not state.get('loading'):
                return state
            back = self._panel(page).locator('button[aria-label="back"],kat-button[data-testid="back-button"],'
                'kat-button[data-testid="primary-button"][label="返回"]').filter(visible=True)
            await self._click_unique(back)
            previous = json.dumps(state, sort_keys=True)
            state = await self._wait_panel(page, run, marketplace, item,
                lambda s: not s.get('loading') and json.dumps(s, sort_keys=True) != previous,
                allow_sku_only_skip=True)
            if state.get('firstLayer'):
                return state
        raise ItemPanelResetError("侧栏未返回当前商品违规要求列表")

    @staticmethod
    def _validate_form(state: dict[str, Any], *, checked: bool = False) -> None:
        boxes, saves = state.get('boxes', []), state.get('saves', [])
        if (not state.get('heading') or state.get('formCount') != 1 or len(boxes) != 1 or len(saves) != 1
                or boxes[0]['label'] not in DECLARATIONS or boxes[0]['disabled']
                or saves[0]['label'] not in ATTESTATION_SAVE_LABELS):
            raise InventoryContractError("安全证明表单结构或完整声明文案已变化")
        if checked and not (boxes[0]['value'] == 'true' or boxes[0]['checked']):
            raise InventoryContractError("安全声明勾选未生效")
        if checked and saves[0]['disabled']:
            raise InventoryContractError("安全证明保存按钮尚未启用")

    async def submit_once(self, page: Any, run: Any, marketplace: Any, item: InventoryItem) -> None:
        if self._history_readonly:
            raise InventoryContractError("历史全部商品核对仅允许读取")
        if item.requirement_type in PARTY_TYPES:
            await self._submit_person_once(page, run, marketplace, item)
            return
        key = (str(run.id), marketplace.code, item.sku, item.requirement_type)
        if key in self._consumed:
            raise InventoryContractError("本次商品保存已使用，只允许回读")
        page = await self._session(page, run, marketplace)
        await self._identity(page, run)
        try:
            state = await self._panel_snapshot(page, run, marketplace)
        except _AuthenticationNeeded as auth:
            await self._session(auth.page, auth.run, auth.marketplace)
            raise InventoryContractError("保存前发生重新登录，保留提交意图并只回读")
        if not self._bound(state, item):
            raise InventoryContractError("保存前商品身份已变化")
        self._validate_form(state)
        if self._network_failure == 403:
            state = await self._wait_panel(page, run, marketplace, item,
                lambda s: bool(s.get('formCount')))
            self._validate_form(state)
        if not (state['boxes'][0]['checked'] or state['boxes'][0]['value'] == 'true'):
            await self._click_unique(self._panel(page).locator('kat-checkbox[name="value"]'))
        state = await self._wait_panel(page, run, marketplace, item,
            lambda s: bool(s.get('boxes') and (s['boxes'][0]['value'] == 'true' or s['boxes'][0]['checked'])
                          and s.get('saves') and not s['saves'][0]['disabled']))
        self._validate_form(state, checked=True)
        page = self.resolved_page_for(page)
        await self._identity(page, run)
        await self._check_cancelled()
        self._require_inventory_http_ok()
        self._consumed.add(key)
        result = await self._evaluate(page, _SAVE, {
            'domain': self._trusted_host(page), 'path': INVENTORY_PATH, 'panel': PANEL_SELECTOR,
            'sku': item.sku, 'asin': item.asin, 'run': str(run.id), 'declaration': state['boxes'][0]['label'],
            'marketplace': marketplace.code, 'requirementType': item.requirement_type,
            'marketLabels': MARKETPLACE_LABELS[marketplace.code],
            'expected': str(run.store.expected_seller_id),
        })
        if not result.get('ok'):
            raise InventoryContractError("安全证明保存未派发：" + str(result.get('reason', 'unknown')))

    async def prepare_submission(self, page, run, marketplace, item):
        """Reversible form preparation, before the SQLite submit intent.

        Selection is not submission. A failure here can be recorded per item
        and must never create a historical UNCERTAIN submission barrier.
        """
        if self._history_readonly:
            raise InventoryContractError("历史全部商品核对仅允许读取")
        if item.requirement_type not in PARTY_TYPES:
            return
        page = await self._session(page, run, marketplace)
        if (str(run.id), marketplace.code, item.sku, item.requirement_type) in self._consumed:
            raise InventoryContractError("关联信息送出已使用，只允许回读")
        choice = self._person_selection.get(page)
        if choice is None or choice[:2] != (item.sku, item.asin) or choice[3] != item.requirement_type:
            raise InventoryContractError("缺少本次关联信息初始未选中证明")
        self._person_prepared.pop(page, None)
        state = await self._wait_panel(page, run, marketplace, item, lambda s: self._party_matches(s, item.requirement_type))
        records = state.get('personRecords', [])
        candidates = [r for r in records if not r['invalid'] and not r['disabled'] and r['radioCount'] == 1]
        if (not candidates or candidates[0]['key'] != choice[2] or any(r['selected'] for r in records)
                or sum(r['key'] == choice[2] for r in records) != 1):
            raise InventoryContractError("关联信息选择依据已变化，未送出")
        await self._identity(page, run)
        self._item_stage = 'person_selection'
        # Real KAT DOM: a SPAN[part=radiobutton-icon] covers the native input.
        # Click its visible custom control, not the covered input or the card
        # (which also contains Edit and detail links). No force or JS check.
        index = next(i for i, r in enumerate(records) if r['key'] == choice[2])
        card = self._panel(page).locator('kat-card').filter(visible=True).nth(index)
        radio = card.locator('input[type="radio"]').filter(visible=True)
        hosts = card.locator('kat-radiobutton').filter(visible=True).filter(
            has=page.locator('input[type="radio"]'))
        count = await self._bounded_ui(hosts.count())
        if count > 1:
            raise InventoryContractError("关联信息单选控件不唯一")
        target = hosts if count == 1 else radio
        try:
            await self._click_unique(target)
            await self._wait_panel(page, run, marketplace, item,
                lambda s: self._person_ready(s, choice[2], item.requirement_type))
        except ItemLoadError as exc:
            await self._item_event(run, item, 'person_selection_failed')
            error = ManufacturerSelectionError if item.requirement_type == MANUFACTURER else PersonSelectionError
            raise error("关联信息勾选或送出按钮启用未确认，尚未点击送出") from exc
        self._person_prepared[page] = choice
        self._item_stage = 'person_ready'
        await self._item_event(run, item, 'person_selected')

    @classmethod
    def _person_ready(cls, state, record_key, requirement_type=RESPONSIBLE_PERSON):
        records = state.get('personRecords', [])
        selected = [r for r in records if r.get('selected')]
        return bool(cls._party_matches(state, requirement_type) and len(selected) == 1
            and selected[0]['key'] == record_key and not selected[0]['invalid'] and not selected[0]['disabled']
            and selected[0]['radioCount'] == 1 and sum(r['key'] == record_key for r in records) == 1
            and len(state.get('personSaves', [])) == 1 and not state['personSaves'][0]['disabled'])

    async def _submit_person_once(self, page, run, marketplace, item):
        page = await self._session(page, run, marketplace)
        key = (str(run.id), marketplace.code, item.sku, item.requirement_type)
        if key in self._consumed:
            raise InventoryContractError("关联信息送出已使用，只允许回读")
        choice = self._person_prepared.get(page)
        if choice is None or choice[:2] != (item.sku, item.asin) or choice[3] != item.requirement_type:
            raise InventoryContractError("缺少关联信息提交前勾选完成证明")
        state = await self._panel_snapshot(page, run, marketplace)
        if not self._bound(state, item) or not self._person_ready(state, choice[2], item.requirement_type):
            raise InventoryContractError("关联信息送出前商品或选择依据已变化")
        await self._identity(page, run)
        await self._check_cancelled()
        self._require_inventory_http_ok()
        self._consumed.add(key)
        result = await self._evaluate(page, _PERSON_SAVE, {
            'domain': self._trusted_host(page), 'path': INVENTORY_PATH, 'panel': PANEL_SELECTOR,
            'expected': str(run.store.expected_seller_id), 'marketLabels': MARKETPLACE_LABELS[marketplace.code],
            'marketplace': marketplace.code, 'run': str(run.id), 'sku': item.sku, 'asin': item.asin, 'record': choice[2], 'requirementType': item.requirement_type,
        })
        if not result.get('ok'):
            raise InventoryContractError("关联信息送出未派发")
        self._person_prepared.pop(page, None)
        self._item_stage = 'person_submitted'
        await self._item_event(run, item, 'person_dispatched')

    async def confirm_submission(self, page: Any, run: Any, marketplace: Any, item: InventoryItem) -> bool:
        async def read() -> dict[str, Any]:
            try:
                return await self._panel_snapshot(page, run, marketplace)
            except _AuthenticationResumed:
                # Login already succeeded and the alias was retained. Continue
                # read-only polling on that page within the original deadline.
                return {}
        started = time.monotonic()
        try:
            await self._poll(read,
                lambda s: bool(self._bound(s, item) and self._receipt_matches(self.resolved_page_for(page), item, s) and not s.get('loading')),
                self.timing.confirmation_timeout_seconds, message="未读到商品审核中回执", resume_after_auth=True)
        except InventoryRunCancelled:
            # Do not disable the cancellation guard globally. This path performs
            # raw DOM reads only: no login, announcement clicks, refresh or reopen.
            remaining = self.timing.confirmation_timeout_seconds - (time.monotonic() - started)
            return await self._drain_cancelled_receipt(page, run, marketplace, item, remaining)
        except (ItemLoadError, TimeoutError):
            return False
        if self._network_failure == 403:
            await self._identity(self.resolved_page_for(page), run)
            await self._accept_forbidden_dom(run, item)
        return True

    async def _drain_cancelled_receipt(self, page: Any, run: Any, marketplace: Any,
                                      item: InventoryItem, remaining: float) -> bool:
        if remaining <= 0:
            return False
        async def read():
            current = self.resolved_page_for(page)
            self._require_route(current)  # A login redirect stops readback, never clicks Login.
            proof = await current.evaluate(_IDENTITY, {
                'expected': str(run.store.expected_seller_id),
                'marketLabels': MARKETPLACE_LABELS[marketplace.code],
            })
            if not proof.get('ok'):
                raise InventoryContractError("取消后回执卖家或商城身份不匹配")
            return await current.evaluate(_PANEL, {
                'panel': PANEL_SELECTOR, 'receipt': SUBMISSION_RECEIPT,
                'receipts': SUBMISSION_RECEIPTS, 'personReceipts': RESPONSIBLE_PERSON_RECEIPTS,
                'attestationReceipts': ATTESTATION_REVIEW_RECEIPTS, 'traceToken': '',
            })
        try:
            await self._poll(read,
                lambda s: bool(self._bound(s, item) and not s.get('loading') and
                    self._receipt_matches(self.resolved_page_for(page), item, s)),
                remaining, message="取消后回执读取到期")
        except ItemLoadError:
            return False
        return True

    async def confirmed_attestation_retained(self, page: Any, run: Any, marketplace: Any, item: InventoryItem) -> bool:
        """Consistency check for independently CONFIRMED history, never a receipt.

        Reconcile already opened the current form. Read it in place; no new
        navigation or checkbox action, and no promotion of UNCERTAIN history.
        """
        if item.requirement_type != SAFETY_ATTESTATION:
            return False
        page = self.resolved_page_for(page)
        state = await self._panel_snapshot(page, run, marketplace)
        await self._identity(page, run)
        if not self._bound(state, item) or state.get('loading') or not state.get('formCount'):
            return False
        self._validate_form(state)
        box = state['boxes'][0]
        return bool((box['checked'] or box['value'] == 'true') and state['saves'][0]['disabled'])

    async def reconcile_item(self, page: Any, run: Any, marketplace: Any, item: InventoryItem) -> bool:
        for attempt in range(2):
            try:
                return await self._reconcile_item(page, run, marketplace, item)
            except InventoryCsrfHeaderLoss:
                page = await self._recover_csrf_page(page, run, marketplace, item)
                # This remains receipt-only even after rebuilding the document.
                return await self._reconcile_item(page, run, marketplace, item)
            except _AuthenticationResumed:
                if attempt:
                    raise ItemLoadError("重新登录后商品历史提交回读未恢复")
                page = self.resolved_page_for(page)
        raise ItemLoadError("重新登录后商品历史提交回读未恢复")

    async def _reconcile_item(self, page: Any, run: Any, marketplace: Any, item: InventoryItem) -> bool:
        try:
            state = await self._panel_snapshot(page, run, marketplace)
        except _AuthenticationNeeded as auth:
            await self._session(auth.page, auth.run, auth.marketplace)
            page = self.resolved_page_for(page)
            state = await self._panel_snapshot(page, run, marketplace)
        except _AuthenticationResumed:
            page = self.resolved_page_for(page)
            state = {}
        if self._bound(state, item):
            # An existing receipt, pending-review card, or known sidebar stage is
            # already bound to this SKU/ASIN. Keep the current page and continue it.
            stable = await self._wait_panel(page, run, marketplace, item,
                lambda s: bool(s.get('receipt') or s.get('cardSubmitted') or s.get('firstLayer')
                               or s.get('cardCount') or s.get('formCount') or s.get('knownOther')
                               or s.get('resolution')))
            result = await self._inspect_loaded_panel(page, run, marketplace, item, stable)
            return result.status == 'already_submitted'
        # Most historical items are still in the scanned list. Searching first
        # needlessly changed the view and could fail before opening any sidebar.
        if (not await self.locate_item(page, item)
                and not await self.locate_item(page, item, search=True)):
            # Leaving ApprovalRequired is not a receipt; never expand to All.
            return False
        result = await self.inspect_item(page, run, marketplace, item)
        return result.status == 'already_submitted'

    def _status_dropdown(self, page: Any) -> Any:
        # Resolve the real status control by its known ApprovalRequired option,
        # not by position (the search and fulfillment dropdowns also contain All).
        return page.locator('kat-dropdown').filter(visible=True).filter(
            has=page.locator('kat-option[value="ApprovalRequired"]'))

    async def _select_inventory_status(self, page: Any, *, all_items: bool) -> str:
        dropdown = self._status_dropdown(page)
        if await self._bounded_ui(dropdown.count()) != 1:
            raise InventoryContractError("商品状态筛选控件不唯一，历史回读未扩大范围")
        await self._click_unique(dropdown)
        if all_items:
            option = dropdown.locator('kat-option').filter(visible=True).filter(
                has_text=re.compile(r'^\s*(?:全部|所有商品|全部商品|所有|All|All listings|All products)(?:\s*[（(]\s*[\d,]+\s*[)）])?\s*$', re.I))
        else:
            option = dropdown.locator('kat-option[value="ApprovalRequired"]').filter(visible=True)
        if await self._bounded_ui(option.count()) != 1:
            raise InventoryContractError("商品状态目标选项不唯一，未猜测筛选值")
        value = await self._bounded_ui(option.get_attribute('value'))
        if value is None or (all_items and value == 'ApprovalRequired'):
            raise InventoryContractError("商品状态选项值未识别")
        token = uuid4().hex
        await self._evaluate(page, _INSTALL_SEARCH_TRACE, token)
        try:
            await self._click_unique(option)
            await self._poll(lambda: self._read_list(page),
                lambda s: bool(s.get('ready') and not s.get('loading') and
                    s.get('statusFilter') == value and
                    s.get('searchToken') == token and s.get('searchFresh')),
                self.timing.list_timeout_seconds, message="商品状态切换后列表未完成更新")
        finally:
            try:
                async with asyncio.timeout(min(1.0, self.timing.panel_timeout_seconds)):
                    await page.evaluate(_CLEAR_SEARCH_TRACE, token)
            except Exception as exc:
                logger.warning('inventory_filter_trace_cleanup_failed type=%s', type(exc).__name__)
        return value

    async def _reconcile_all_inventory(self, page: Any, run: Any, marketplace: Any,
                                       item: InventoryItem) -> bool:
        """Only historical missing items may enter this read-only scope.

        Disappearance, an active listing and a selected radio are not receipts.
        The normal scan/write path still strictly requires ApprovalRequired.
        """
        page = self.resolved_page_for(page)
        await self._identity(page, run)
        query = await self._read_search_state(page)
        if not query.get('recognized') or query.get('mode') != 'SKU' or query.get('values') != [item.sku]:
            raise InventoryContractError("历史商品搜索条件已变化，未切换全部商品")
        self._history_readonly = True
        try:
            self._item_stage = "history_all_inventory"
            await self._item_event(run, item, "history_all_inventory")
            # Close an old sidebar only if it obstructs the real filter control.
            await self._prepare_item_entry(page, run, marketplace, item, self._status_dropdown(page))
            value = await self._select_inventory_status(page, all_items=True)
            snapshot = await self._wait_list(page, expected_filter=value)
            if (await self._read_search_state(page)).get('values') != [item.sku]:
                raise InventoryContractError("切换全部商品后 SKU 搜索条件丢失")
            if any(candidate.sku != item.sku or candidate.asin != item.asin for candidate in snapshot.items):
                raise InventoryContractError("全部商品搜索结果与历史 SKU / ASIN 不符")
            confirmed = False
            if snapshot.items:
                # Do not enter inspect_item's automatic reload/retry path in All.
                # Only read-only sidebar traversal is allowed here.
                result = await self._inspect_item(page, run, marketplace, item)
                confirmed = result.status == 'already_submitted'
            await self._prepare_item_entry(page, run, marketplace, item, self._status_dropdown(page))
            await self._select_inventory_status(page, all_items=False)
            await self._search_inventory(page, "")
            await self._require_empty_search(page)
            await self._identity(page, run)
            return confirmed
        finally:
            # Failure stops this run; a later run's open_inventory resets scope.
            self._history_readonly = False
