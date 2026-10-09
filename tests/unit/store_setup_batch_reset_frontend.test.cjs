const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const script = fs.readFileSync(path.join(__dirname, "../../src/ziniao_automation/static/app.js"), "utf8");
const helpers = script.slice(script.indexOf("  let batchSetupResetBusy"), script.indexOf("  function setupOperationMode"));
function element(extra = {}) {
  return {textContent:"", disabled:false, hidden:false, children:[], listeners:new Map(),
    addEventListener(type, cb) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(cb); },
    removeEventListener(type, cb) { this.listeners.get(type)?.delete(cb); },
    async fire(type) { await Promise.all([...this.listeners.get(type)||[]].map(cb=>cb({preventDefault(){}}))); },
    replaceChildren(){this.children=[];}, append(child){this.children.push(child);}, ...extra};
}
function card(id, extra = {}) {
  return {hidden:false, checked:true, dataset:{storeId:String(id), storeName:`测试店 ${id}`, setupRevision:"7", platformAmazon:"true"}, ...extra};
}
function setup(cards=[card(1)], extra={}) {
  const nodes = Object.fromEntries(["#setup-reset-batch-dialog", "[data-setup-reset-list]", "[data-setup-reset-confirm]",
    "[data-setup-reset-cancel]", "[data-setup-reset-result]", "[data-setup-reset-summary]", "[data-setup-reset-queue-note]",
    '[data-action="batch-reset-store-setups"]', "[data-setup-reset-count]"].map(key=>[key,element()]));
  const dialog=nodes["#setup-reset-batch-dialog"];
  dialog.showModal=()=>{dialog.open=true;};dialog.close=()=>{dialog.open=false;};
  const state={calls:[],toasts:[],reloads:0,discards:0,removed:[]};
  const bulkStoreSetup={active:false,queue:[],...extra.bulk};
  const context=vm.createContext({
    $:(selector, root)=>selector==="[data-setup-select]" ? root : nodes[selector]||null,
    $$:()=>cards,
    document:{createElement:()=>element()},
    location:{reload(){state.reloads++;}},
    validPositiveId:value=>Number.isSafeInteger(value)&&value>0,
    toast:(message)=>state.toasts.push(message),bulkStoreSetup,
    discardBulkStoreSetup(){state.discards++;bulkStoreSetup.queue=[];},
    bulkSetupSummaryStorageKey:"summary",
    sessionStorage:{removeItem(key){if(extra.storageFailure)throw new Error("storage disabled");state.removed.push(key);}},
    api:async (url, request)=>{state.calls.push({url,request});if(extra.api)return extra.api(url,request);
      const ids=JSON.parse(request.body).stores.map(s=>s.store_id);
      return {reset_store_ids:ids,reset_count:ids.length,disabled_schedules:ids.length*2};},
  });
  vm.runInContext(helpers, context);
  return {context,nodes,cards,state,bulkStoreSetup,dialog,
    yes:nodes["[data-setup-reset-confirm]"],no:nodes["[data-setup-reset-cancel]"],
    result:nodes["[data-setup-reset-result]"],list:nodes["[data-setup-reset-list]"],
    button:nodes['[data-action="batch-reset-store-setups"]']};
}
const plain=value=>JSON.parse(JSON.stringify(value));
test("no checked stores never falls back to all stores",async()=>{
  const x=setup([card(1,{checked:false})]);await x.context.resetSelectedStoreSetups();
  assert.equal(x.state.calls.length,0);assert.equal(x.dialog.open,undefined);
  x.context.refreshSetupResetSelection();assert.equal(x.button.disabled,true);
});
test("only visible explicitly checked Amazon stores and current revisions",()=>{
  const x=setup([card(1),card(2,{hidden:true}),card(3,{checked:false}),card(4,{dataset:{storeId:"4",platformAmazon:"false"}})]);
  assert.deepEqual(plain(x.context.selectedSetupResetTargets()),[{store_id:1,name:"测试店 1",expected_setup_revision:7}]);
  x.context.refreshSetupResetSelection();assert.equal(x.button.textContent,"批量删除建档（1）");
  x.cards[0].hidden=true;x.context.refreshSetupResetSelection();assert.equal(x.button.disabled,true);
  x.cards[0].hidden=false;x.cards[1].hidden=false;x.context.refreshSetupResetSelection();assert.equal(x.button.textContent,"批量删除建档（2）");
});
test("missing revision cannot be guessed and resets are not attempted",async()=>{
  const a=card(1);delete a.dataset.setupRevision;const x=setup([a]);await x.context.resetSelectedStoreSetups();
  assert.equal(x.state.calls.length,0);assert.match(x.state.toasts[0],/版本/);
});
test("cancel and escape never call reset API",async()=>{
  for(const how of ["cancel","escape"]){const x=setup();const pending=x.context.resetSelectedStoreSetups();
    await(how==="cancel"?x.no.fire("click"):x.dialog.fire("cancel"));await pending;
    assert.equal(x.state.calls.length,0);assert.equal(x.button.disabled,false);}
});
test("confirmation freezes scope, uses textContent and posts no names",async()=>{
  const malicious='<img src=x onerror=alert(1)>';const a=card(1);a.dataset.storeName=malicious;
  const x=setup([a,card(2)]);const pending=x.context.resetSelectedStoreSetups();
  assert.equal(x.list.children[0].textContent,malicious);assert.equal(x.list.children[0].innerHTML,undefined);
  x.cards[1].checked=false;x.cards[0].dataset.setupRevision="99";
  await x.yes.fire("click");assert.equal(x.state.calls.length,1);
  assert.deepEqual(JSON.parse(x.state.calls[0].request.body),{stores:[{store_id:1,expected_setup_revision:7},{store_id:2,expected_setup_revision:7}]});
  await x.no.fire("click");await pending;assert.equal(x.state.reloads,1);
});
test("double click, repeated action and escape while submitting cannot duplicate",async()=>{
  let release;const gate=new Promise(resolve=>{release=resolve;});
  const x=setup(undefined,{api:()=>gate});const pending=x.context.resetSelectedStoreSetups();
  await x.context.resetSelectedStoreSetups();const clicked=x.yes.fire("click");
  await x.yes.fire("click");await x.dialog.fire("cancel");assert.equal(x.dialog.open,true);
  assert.equal(x.state.calls.length,1);assert.equal(x.no.disabled,true);
  release({reset_store_ids:[1],reset_count:1,disabled_schedules:2});await clicked;
  await x.yes.fire("click");assert.equal(x.state.calls.length,1);
  await x.no.fire("click");await pending;
});
test("blocked response explains each store without changing paused queue",async()=>{
  const error=Object.assign(new Error("blocked"),{statusCode:409,detail:{targets:[{store_name:"测试店 2",reason:"等待验证"}]}});
  const x=setup(undefined,{bulk:{queue:[{id:1}]},api:()=>{throw error;}});
  const pending=x.context.resetSelectedStoreSetups();await x.yes.fire("click");
  assert.match(x.result.textContent,/整批未修改.*测试店 2.*等待验证/);
  assert.equal(x.state.discards,0);assert.equal(x.state.removed.length,0);
  await x.no.fire("click");await pending;assert.equal(x.state.reloads,0);
});
test("only successful resets discard a related local queue",async()=>{
  const x=setup(undefined,{bulk:{queue:[{id:1},{id:3}]}});const pending=x.context.resetSelectedStoreSetups();
  assert.equal(x.nodes["[data-setup-reset-queue-note]"].hidden,false);
  await x.yes.fire("click");assert.equal(x.state.discards,1);assert.deepEqual(x.state.removed,["summary"]);
  await x.no.fire("click");await pending;
  const y=setup(undefined,{bulk:{queue:[{id:3}]}});
  await y.context.submitSetupResetBatch([{store_id:1,expected_setup_revision:7}]);assert.equal(y.state.discards,0);
});
test("storage and scheduler warnings do not misreport committed result",async()=>{
  const x=setup(undefined,{storageFailure:true,api:async()=>({reset_store_ids:[1],reset_count:1,disabled_schedules:2,warning:"定时器未刷新。"})});
  const pending=x.context.resetSelectedStoreSetups();await x.yes.fire("click");
  assert.match(x.result.textContent,/已删除 1 家/);assert.match(x.result.textContent,/定时器未刷新/);assert.match(x.result.textContent,/缓存清理未完成/);
  assert.equal(x.no.textContent,"完成并刷新列表");await x.no.fire("click");await pending;assert.equal(x.state.reloads,1);
});
test("active bulk workflow cannot be silently cancelled by reset",async()=>{
  const x=setup(undefined,{bulk:{active:true}});await x.context.resetSelectedStoreSetups();
  assert.equal(x.state.calls.length,0);assert.match(x.state.toasts[0],/仍在运行/);
});
test("404 and network errors do not silently resend the same confirmation",async()=>{
  for(const statusCode of [404,0]){
    const x=setup(undefined,{api:()=>{throw Object.assign(new Error("request failed"),{statusCode});}});
    const pending=x.context.resetSelectedStoreSetups();await x.yes.fire("click");assert.equal(x.yes.hidden,true);
    assert.match(x.result.textContent,statusCode===404?/尚未加载/:/刷新列表核对/);
    await x.no.fire("click");await pending;assert.equal(x.state.calls.length,1);
  }
});
test("failed dialog opening resets busy flag; other pages need no nodes",async()=>{
  const x=setup();x.dialog.showModal=()=>{throw new Error("fixture failure");};
  await x.context.resetSelectedStoreSetups();assert.equal(x.button.disabled,false);
  x.dialog.showModal=()=>{x.dialog.open=true;};const p=x.context.resetSelectedStoreSetups();assert.equal(x.dialog.open,true);
  await x.no.fire("click");await p;
  delete x.nodes['[data-action="batch-reset-store-setups"]'];assert.doesNotThrow(()=>x.context.refreshSetupResetSelection());
});
