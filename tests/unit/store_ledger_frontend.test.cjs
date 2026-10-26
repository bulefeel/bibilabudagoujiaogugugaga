"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const script = fs.readFileSync(path.join(__dirname, "../../src/ziniao_automation/static/app.js"), "utf8");
const helpers = script.slice(script.indexOf("function collectBulkStoreCandidates()"), script.indexOf("function discardBulkStoreSetup()"));
const filter = script.slice(script.indexOf("(function storeFilter()"), script.indexOf("// A page-specific block that throws"));
const action = script.slice(script.indexOf('if (action === "detect-all-store-setups")'), script.indexOf('if (action === "continue-bulk-store-setup")'));
function element(extra={}) {
  return {value:"", textContent:"", hidden:false, disabled:false, listeners:{},
    addEventListener(name, callback) { this.listeners[name] = callback; }, ...extra};
}
function card(id, overrides={}) {
  return element({dataset:{storeId:String(id), storeName:"Store " + id, platformLabel:"亚马逊", platformAmazon:"true", setupState:"needed", storeSetupNeeded:"true", storeSetupMarketplaces:'["CA"]', identityState:"MISSING", storeEnabled:"false", ...overrides}});
}
function setup(cards, search="", state={}) {
  const selects = [
    element({name:"platform", value:"亚马逊", options:["", "亚马逊", "TikTok Shop", "平台未知"].map(value => ({value}))}),
    element({name:"setup", options:["", "needed", "done"].map(value => ({value}))}),
    element({name:"enabled", options:["", "true", "false"].map(value => ({value}))}),
  ];
  const q = element();
  const form = element({elements:{...Object.fromEntries(selects.map(s => [s.name,s])), q}, reset() {selects.forEach(s=>s.value=s.name==="platform"?"亚马逊":""); q.value="";}});
  const start=element(), count=element(), note=element(), empty=element(), reset=element();
  const bulkStoreSetup={queue:[],index:0,active:false, ...state};
  const nodes={"[data-store-filter]":form,"[data-store-filter-count]":count,"[data-store-bulk-scope]":note,"[data-store-filter-empty]":empty,'[data-action="reset-store-filters"]':reset,'[data-action="detect-all-store-setups"]':start};
  const context=vm.createContext({
    URLSearchParams, location:{search,pathname:"/stores",hash:""},
    history:{replaceState(_state,_title,url){context.location.search=url.slice(url.indexOf("?"));}},
    $: (selector,root)=>selector==="[data-setup-select]"?{checked:!!root?.checked}:nodes[selector]||null, $$: selector=>selector==="select"?selects:cards,
    setupOperationMode:()=>"fill_missing", selectedBatchSetupCodes:()=>[],
    setupMarketplaceCodes:["CA","UK","AU","US","DE","IT","FR","ES","PL"],
    validPositiveId:value=>Number.isSafeInteger(Number(value))&&Number(value)>0, bulkStoreSetup,
  });
  vm.runInContext(helpers + "\n" + filter,context);
  return {cards,context,form,start,count,note,empty,reset,state:bulkStoreSetup,
    apply(){form.listeners.change();}, visible(){return cards.filter(c=>!c.hidden).map(c=>c.dataset.storeId);},
    selection(){return context.collectBulkStoreCandidates();}};
}
test("default shows Amazon, keeps other and unknown cards available",()=>{
  const x=setup([card(1),card(2,{platformLabel:"TikTok Shop",platformAmazon:"false",setupState:"n/a"}),card(3,{platformLabel:"平台未知",platformAmazon:""})]);
  assert.deepEqual(x.visible(),["1"]); assert.equal(x.start.textContent,"预览批量补充建档（1）");
  x.form.elements.platform.value="";x.apply();assert.deepEqual(x.visible(),["1","2","3"]);
  assert.equal(x.selection().eligible.length,2); // 保留老记录无平台字段时的既有处理方式。
  assert.match(x.note.textContent,/1 家其他平台不参与/);
});
test("explicit all-platform selection survives reload",()=>{
  const cards=[card(1),card(2,{platformLabel:"TikTok Shop",platformAmazon:"false"})];
  const x=setup(cards,"?platform=");assert.deepEqual(x.visible(),["1","2"]);
  assert.ok(x.context.location.search.includes("platform="));
  const y=setup(cards,x.context.location.search);assert.equal(y.form.elements.platform.value,"");assert.equal(y.visible().length,2);
});
test("name, setup and task switch combine without mutating store data",()=>{
  const cards=[card(1,{storeName:"Alpha & 一店"}),card(2,{storeName:"Alpha",storeEnabled:"true"}),card(3,{storeName:"Beta"})];
  const before=JSON.stringify(cards.map(c=>c.dataset));
  const x=setup(cards,"?q=ALPHA&enabled=false&setup=needed");assert.deepEqual(x.visible(),["1"]);
  x.form.elements.q.value="no match";x.form.elements.q.listeners.input();assert.equal(x.empty.hidden,false);assert.equal(x.start.disabled,true);
  x.reset.listeners.click();assert.deepEqual(x.visible(),["1","2","3"]);assert.equal(x.empty.hidden,true);
  assert.equal(JSON.stringify(cards.map(c=>c.dataset)),before);
});
test("encoded search and hash-unrelated query survive URL serialization",()=>{
  const x=setup([card(1,{storeName:"A & 一店"})],"?other=value&q=A+%26+%E4%B8%80%E5%BA%97");
  assert.equal(x.visible().length,1);const p=new URLSearchParams(x.context.location.search);
  assert.equal(p.get("q"),"A & 一店");assert.equal(p.get("other"),"value");
});
test("DE-only pending store is eligible without widening to payout sites",()=>{
  const x=setup([card(1,{storeSetupMarketplaces:'["DE"]'})]);
  assert.equal(x.selection().eligible.length,1);assert.equal(x.selection().unsupported.length,0);
  assert.equal(x.start.disabled,false);assert.equal(x.selection().eligible[0].defaulted,false);
  assert.deepEqual(Array.from(x.selection().eligible[0].marketplaceCodes),["DE"]);
});
test("only genuinely empty site selection defaults to CA UK AU",()=>{
  const x=setup([card(1,{storeSetupMarketplaces:'[]'}),card(2,{storeSetupMarketplaces:'["DE","CA"]'}),card(3,{storeSetupMarketplaces:'["DE"]'})]);
  const selected=JSON.parse(JSON.stringify(x.selection().eligible));
  assert.deepEqual(selected.map(s=>s.marketplaceCodes),[["CA","UK","AU"],["DE","CA"],["DE"]]);
  assert.deepEqual(selected.map(s=>s.defaulted),[true,false,false]);
});
test("configured cards can preview additions without resetting their identity",()=>{
  const x=setup([card(1,{setupState:"done",storeSetupNeeded:"false",identityState:"CONFIRMED"}),card(2)]);
  assert.equal(x.visible().length,2);assert.equal(x.selection().eligible.length,2);assert.match(x.note.textContent,/1 家已配置/);
  assert.match(x.note.textContent,/无需删除/);assert.equal(x.start.disabled,false);
});

test("a selected configured store adds only the requested sites and excludes hidden selections",()=>{
  const first=card(1,{setupState:"done",storeSetupNeeded:"false",identityState:"CONFIRMED"});first.checked=true;
  const hidden=card(3,{storeName:"Hidden"});hidden.checked=true;
  const x=setup([first,card(2),hidden],"?q=Store");
  x.context.selectedBatchSetupCodes=()=>["US","DE"];
  x.context.refreshBulkStoreScope();
  assert.equal(x.start.disabled,false);
  assert.deepEqual(Array.from(x.selection().eligible,s=>s.id),[1]);
  assert.deepEqual(Array.from(x.selection().eligible[0].marketplaceCodes),["US","DE"]);
  assert.equal(first.dataset.storeSetupMarketplaces,'["CA"]');
});

test("batch preview sends the selected additions and queues the server's incremental scope",async()=>{
  const a=card(1,{setupState:"done",storeSetupNeeded:"false"});a.checked=true;
  const hidden=card(3,{storeName:"Hidden"});hidden.checked=true;
  const x=setup([a,card(2),hidden],"?q=Store");
  const rows=element({children:[],replaceChildren(){this.children=[];},append(row){this.children.push(row);}});
  const yes=element(),no=element(),count=element();
  for(const node of [yes,no]) node.removeEventListener=()=>{};
  const dialog=element({removeEventListener(){},close(){},showModal(){queueMicrotask(()=>yes.listeners.click());}});
  const originalSelect=x.context.$;
  const nodes={"#setup-batch-preview":dialog,"[data-setup-preview-rows]":rows,"[data-setup-preview-count]":count,"[data-setup-preview-confirm]":yes,"[data-setup-preview-cancel]":no};
  Object.assign(x.context,{
    $:(selector,root)=>nodes[selector]||originalSelect(selector,root),
    selectedBatchSetupCodes:()=>["CA","US","DE"],
    crypto:{randomUUID:()=>"batch-request"},
    document:{createElement:()=>({textContent:"",children:[],append(child){this.children.push(child);}})},
    api:async(url,request)=>{
      assert.equal(url,"/api/stores/setup-batch/preview");
      assert.deepEqual(JSON.parse(request.body),{store_ids:[1],mode:"fill_missing",selections:{1:["CA","US","DE"]}});
      return {eligible_count:1,targets:[{store_id:1,name:"Store 1",eligible:true,marketplace_codes:["US","DE"],scope:"full",setup_revision:7,added_marketplace_codes:["US","DE"],preserved_marketplace_codes:["CA"],already_passed_marketplace_codes:["CA"]}]};
    },
  });
  const preview=script.slice(script.indexOf("async function previewSetupSelection()"),script.indexOf("const setupOutcomeLabels="));
  vm.runInContext(preview,x.context);
  const queue=await x.context.previewSetupSelection();
  assert.deepEqual(Array.from(queue[0].marketplaceCodes),["US","DE"]);
  assert.equal(queue[0].setupRevision,7);assert.equal(queue[0].requestId,"batch-request");
  assert.match(rows.children[0].children[3].textContent,/新增或启用：US \/ DE/);
  assert.match(rows.children[0].children[3].textContent,/保留原站点：CA/);
});
test("filters never replace the saved queue or its resume button",()=>{
  const queue=[{id:2,name:"original queue"}];const x=setup([card(1),card(2)],"",{queue,index:0,resumeAvailable:true});
  x.start.textContent="继续未完成队列";x.form.elements.q.value="absent";x.apply();
  assert.equal(x.start.textContent,"继续未完成队列");assert.equal(x.start.disabled,false);assert.strictEqual(x.state.queue,queue);
  assert.match(x.note.textContent,/原店铺清单/);
});
test("the exact confirmed visible selection is queued, not all cards",async()=>{
  const x=setup([card(1),card(2,{storeName:"Hidden"}),card(3,{storeName:"Store 3",storeSetupMarketplaces:'["DE"]'})],"?q=Store");
  let message="",started=0;
  Object.assign(x.context,{previewSetupSelection:async()=>{message="Store 1; DE included";return JSON.parse(JSON.stringify(x.selection().eligible));},toast:()=>assert.fail("unexpected toast"),sessionStorage:{removeItem(){}},bulkSetupSummaryStorageKey:"summary",persistBulkStoreSetup(){},bulkSetupUi:()=>({panel:element(),actions:element(),dismiss:element()}),updateBulkSetupCounts(){},runNextBulkStoreSetup:async()=>{started++;}});
  const invoke=vm.runInContext("(async function(action,target){"+action+"})",x.context);
  await invoke("detect-all-store-setups",x.start);
  assert.equal(started,1);assert.deepEqual(Array.from(x.state.queue,s=>s.id),[1,3]);assert.match(message,/Store 1/);assert.doesNotMatch(message,/Hidden/);assert.match(message,/DE/);
});
test("canceling the confirmation never changes the queue or launches work",async()=>{
  const x=setup([card(1)]);Object.assign(x.context,{previewSetupSelection:async()=>null,toast:()=>{},sessionStorage:{removeItem(){assert.fail("must not write");}}});
  const invoke=vm.runInContext("(async function(action,target){"+action+"})",x.context);await invoke("detect-all-store-setups",x.start);
  assert.equal(x.state.queue.length,0);assert.equal(x.state.active,false);
});
test("filter initialization is a no-op on other pages",()=>{
  assert.doesNotThrow(()=>vm.runInNewContext(filter,{$:()=>null}));
});

test("explicit EU batch selection allows extending an already configured store",()=>{
  const x=setup([card(1,{setupState:"done",storeSetupNeeded:"false",identityState:"CONFIRMED"})]);
  x.context.selectedBatchSetupCodes=()=>["IT","FR","ES","PL"];
  x.context.refreshBulkStoreScope();
  assert.equal(x.start.disabled,false);
  assert.match(x.start.textContent,/所选站点/);
});

test("restored queue keeps the exact EU site snapshot and request identity",()=>{
  const saved={queue:[{id:1,name:"Fixture",marketplaceCodes:["IT","FR","ES","PL"],requestId:"request-1",scope:"full",setupRevision:5}],index:0};
  const ui=Object.fromEntries(["panel","actions","resume","skip","title","message","start","dismiss"].map(k=>[k,element()]));
  const state={};
  const context=vm.createContext({sessionStorage:{getItem:()=>JSON.stringify(saved),removeItem:()=>assert.fail("must preserve queue")},
    bulkSetupStorageKey:"fixture",setupMarketplaceCodes:["CA","UK","AU","DE","IT","FR","ES","PL"],
    validPositiveId:n=>Number.isSafeInteger(Number(n))&&Number(n)>0,bulkQueueArchiveIsStale:()=>false,
    bulkStoreSetup:state,bulkSetupUi:()=>ui,ziniaoCredentialErrorCode:"CREDENTIAL",updateBulkSetupCounts(){}});
  const restore=script.slice(script.indexOf("function restoreBulkStoreSetup()"),script.indexOf("function bulkSetupUi()"));
  vm.runInContext(restore+";restoreBulkStoreSetup();",context);
  assert.deepEqual(Array.from(state.queue[0].marketplaceCodes),["IT","FR","ES","PL"]);
  assert.equal(state.queue[0].requestId,"request-1");assert.equal(state.queue[0].setupRevision,5);
  assert.equal(state.active,false);assert.equal(state.resumeAvailable,true);
});
