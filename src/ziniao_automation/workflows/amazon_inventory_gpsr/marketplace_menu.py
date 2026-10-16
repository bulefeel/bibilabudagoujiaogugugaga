"""Read-only contracts for the header menu (not the full-page account selector)."""

HEADER_ROOT = '#dropdown-account-switcher-container'
HEADER = '.dropdown-account-switcher-header'
ITEM = '.dropdown-account-switcher-list-item'
OWNER_XPATH = ('xpath=ancestor::*[@data-test-tag="partner-switcher-container" or '
               'contains(concat(" ", normalize-space(@class), " "), " partner-switcher-container ")][1]')

# Appended to the existing identity helpers: identityOK/headerSeller have already
# been computed from this exact header. No account names, URLs or IDs are returned.
MENU_BODY = r'''
if(headers.length!==1) return {state:'header_missing',targets:0};
if(!identityOK) return {state:'identity_mismatch',targets:0};
const header=headers[0];
if(p.targetLabels.includes(text(header.querySelector('.dropdown-account-switcher-header-label-regional'))))
  return {state:'selected',targets:0};
const triggers=[...header.querySelectorAll('.dropdown-account-switcher-header')].filter(visible);
if(triggers.length!==1) return {state:'header_ambiguous',targets:0};
const owner=header.closest('[data-test-tag="partner-switcher-container"],.partner-switcher-container');
const scope=owner || header;
const items=[...scope.querySelectorAll('.dropdown-account-switcher-list-item')];
const shown=items.filter(visible);
const indented='dropdown-account-switcher-list-item-indented';
const names=p.targetLabels.map(normal);
const caption=e=>normal(e.querySelector('.dropdown-account-switcher-list-item-label')?.innerText || e.innerText);
const matches=e=>{
  const title=normal(e.title);
  return names.includes(caption(e)) && (!title || names.includes(title));
};
const allTargets=shown.filter(matches);
const bound=e=>{
  if(!e.classList.contains(indented)) return !scope.querySelector('.dropdown-account-switcher-list-scrollable');
  let group=e.previousElementSibling;
  while(group && group.classList.contains(indented))group=group.previousElementSibling;
  return !!group && group.classList.contains('dropdown-account-switcher-list-item') &&
    normal(group.title || group.innerText)===normal(headerSeller);
};
const targets=allTargets.filter(bound);
const menus=[...scope.querySelectorAll('.dropdown-account-switcher-list,.flyoutFlyout')].filter(visible);
const expanded=triggers[0].getAttribute('aria-expanded')==='true';
const loading=[...scope.querySelectorAll('[role="progressbar"],[aria-busy="true"],.dropdown-account-switcher-spinner,kat-spinner')].some(visible);
const open=menus.length>0 || expanded || shown.length>0;
const target=targets.length===1?targets[0]:null;
const disabled=target && (target.hasAttribute('disabled') || target.getAttribute('aria-disabled')==='true');
const state=allTargets.length>1?'target_ambiguous':loading?'loading':
  target?(disabled?'target_disabled':'ready'):
  allTargets.length?'account_mismatch':
  !open?'closed':'target_pending';
return {state,scope:owner?'owner':'header',targets:targets.length,items:shown.length,
        index:target?items.indexOf(target):-1};
'''

ERRORS = {
    'header_missing': '商城切换页头尚未就绪',
    'header_ambiguous': '商城切换入口不唯一',
    'identity_mismatch': '切站前卖家身份或商城页头发生变化',
    'target_ambiguous': '当前账号的目标商城入口不唯一',
    'target_disabled': '目标商城入口不可操作',
    'account_mismatch': '目标商城入口不属于当前卖家账号组',
    'closed': '商城菜单意外收起，有限重开后仍未保持展开',
    'loading': '商城菜单加载超时',
    'target_pending': '商城菜单已展开，但未取得当前账号的目标入口（可能账号组未展开）',
    'ready': '商城目标入口未保持稳定，尚未点击',
    'click_pending': '商城入口点击结果未确认，未重复点击',
}
