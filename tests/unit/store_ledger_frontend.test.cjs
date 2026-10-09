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
    $: selector=>nodes[selector]||null, $$: selector=>selector==="select"?selects:cards,
    setupOperationMode:()=>"fill_missing",
    validPositiveId:value=>Number.isSafeInteger(Number(value))&&Number(value)>0, bulkStoreSetup,
  });
  vm.runInContext(helpers + "\n" + filter,context);
  return {cards,context,form,start,count,note,empty,reset,state:bulkStoreSetup,
    apply(){form.listeners.change();}, visible(){return cards.filter(c=>!c.hidden).map(c=>c.dataset.storeId);},
    selection(){return context.collectBulkStoreCandidates();}};
}
test("default shows Amazon, keeps other and unknown cards available",()=>{
  const x=setup([card(1),card(2,{platformLabel:"TikTok Shop",platformAmazon:"false",setupState:"n/a"}),card(3,{platformLabel:"平台未知",platformAmazon:""})]);
  assert.deepEqual(x.visible(),["1"]); assert.equal(x.start.textContent,"批量建档当前筛选（1）");
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
test("DE-only pending store is skipped explicitly, never called complete or defaulted",()=>{
  const x=setup([card(1,{storeSetupMarketplaces:'["DE"]'})]);
  assert.equal(x.selection().eligible.length,0);assert.equal(x.selection().unsupported.length,1);
  assert.equal(x.start.disabled,true);assert.match(x.note.textContent,/DE/);assert.doesNotMatch(x.note.textContent,/已配置|已完成/);
});
test("only genuinely empty site selection defaults to CA UK AU",()=>{
  const x=setup([card(1,{storeSetupMarketplaces:'[]'}),card(2,{storeSetupMarketplaces:'["DE","CA"]'}),card(3,{storeSetupMarketplaces:'["DE"]'})]);
  const selected=JSON.parse(JSON.stringify(x.selection().eligible));
  assert.deepEqual(selected.map(s=>s.marketplaceCodes),[["CA","UK","AU"],["CA"]]);
  assert.deepEqual(selected.map(s=>s.defaulted),[true,false]);
});
test("configured cards stay visible but do not enter a new setup queue",()=>{
  const x=setup([card(1,{setupState:"done",storeSetupNeeded:"false",identityState:"CONFIRMED"}),card(2)]);
  assert.equal(x.visible().length,2);assert.equal(x.selection().eligible.length,1);assert.match(x.note.textContent,/1 家已配置/);
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
  Object.assign(x.context,{previewSetupSelection:async()=>{message="Store 1; DE excluded";return JSON.parse(JSON.stringify(x.selection().eligible));},toast:()=>assert.fail("unexpected toast"),sessionStorage:{removeItem(){}},bulkSetupSummaryStorageKey:"summary",persistBulkStoreSetup(){},bulkSetupUi:()=>({panel:element(),actions:element(),dismiss:element()}),updateBulkSetupCounts(){},runNextBulkStoreSetup:async()=>{started++;}});
  const invoke=vm.runInContext("(async function(action,target){"+action+"})",x.context);
  await invoke("detect-all-store-setups",x.start);
  assert.equal(started,1);assert.deepEqual(Array.from(x.state.queue,s=>s.id),[1]);assert.match(message,/Store 1/);assert.doesNotMatch(message,/Hidden/);assert.match(message,/DE/);
});
test("canceling the confirmation never changes the queue or launches work",async()=>{
  const x=setup([card(1)]);Object.assign(x.context,{previewSetupSelection:async()=>null,toast:()=>{},sessionStorage:{removeItem(){assert.fail("must not write");}}});
  const invoke=vm.runInContext("(async function(action,target){"+action+"})",x.context);await invoke("detect-all-store-setups",x.start);
  assert.equal(x.state.queue.length,0);assert.equal(x.state.active,false);
});
test("filter initialization is a no-op on other pages",()=>{
  assert.doesNotThrow(()=>vm.runInNewContext(filter,{$:()=>null}));
});
