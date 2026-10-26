const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/ziniao_automation/static/app.js'), 'utf8');
const tracking = source.slice(source.indexOf('  let bulkSetupPollGeneration'), source.indexOf('  function formData'));
const restore = source.slice(source.indexOf('  function restoreBulkStoreSetup'), source.indexOf('  function bulkSetupUi'));
const actions = source.slice(source.indexOf('    if (action === "continue-bulk-store-setup")'), source.indexOf('    if (action === "reset-store-setup")'));
const response = (status, id = 1, extra = {}) => ({statusCode: 200, data: {
  status, store_id:id, probe_id:`p${id}`, expires_in_seconds:1800, cleanup_state:'CONFIRMED', ...extra,
}});
function fixture(options = {}) {
  const ui = Object.fromEntries(['panel','title','message','start','actions','release','dismiss','resume','skip'].map(k=>[k,{hidden:true,disabled:false,textContent:''}]));
  ui.panel.dataset = {trackingPolicy:JSON.stringify({batch_poll_interval_ms:2000,
    batch_poll_request_timeout_ms:10000,batch_poll_error_retries:3,
    batch_poll_finalization_grace_ms:30000,total_deadline_seconds:1800})};
  const queue = Array.from({length:options.stores || 1}, (_,i)=>({id:i+1,name:`fixture ${i+1}`,marketplaceCodes:['DE'],requestId:`r${i+1}`}));
  const state = {queue,index:0,active:true,probeId:'p1',storeId:1,probeStatus:'CHECKING',success:0,failed:0,auth:0};
  const calls=[], snapshots=[], timers=new Map(), saved=new Map();
  let now=0, timerId=0, counted=0, refreshes=0;
  const context=vm.createContext({
    bulkStoreSetup:state,bulkSetupUi:()=>ui,$:()=>null,Date:{now:()=>now},AbortController,
    setTimeout(fn,ms){const id=++timerId;timers.set(id,fn);if(options.fireTimeouts && ms===10000)queueMicrotask(fn);return id;},
    clearTimeout:id=>timers.delete(id),sleep:async ms=>{now+=ms;},
    storeSetupProbePath:(id,pid)=>`/stores/${id}/probes/${pid}`,
    apiResult:async(url,opts={})=>{calls.push({url,method:opts.method||'GET'});return options.request?options.request(url,opts,calls):response('SUCCEEDED');},
    api:async(url,opts)=>{const result=await context.apiResult(url,opts);return result.data;},
    refreshPersistedSetups:async(id,opts,current)=>{refreshes++;if(options.refresh)await options.refresh(id,opts,current);},
    summarizeBulkStoreResult(){counted++;state.success++;},updateBulkSetupCounts(){},
    persistBulkStoreSetup(){snapshots.push({...state,queue:undefined});},persistBulkSetupCompletionSummary(){},
    storeSetupTerminalStatuses:new Set(['SUCCEEDED','FAILED','CANCELLED','PARTIAL','NEEDS_REVIEW']),
    validPositiveId:id=>Number.isSafeInteger(Number(id))&&Number(id)>0,
    toast(){},location:{reload(){}},isZiniaoCredentialError:()=>false,
    sessionStorage:{setItem:(k,v)=>saved.set(k,v),getItem:k=>saved.get(k)||null,removeItem:k=>saved.delete(k)},
    bulkSetupStorageKey:'queue',bulkSetupSummaryStorageKey:'summary',setupMarketplaceCodes:['DE'],
    bulkQueueArchiveIsStale:()=>false,ziniaoCredentialErrorCode:'ZINIAO_CREDENTIALS_INVALID',
  });
  vm.runInContext(tracking+'\n'+restore+'\nasync function action(action,target){'+actions+'}',context);
  return {context,state,ui,calls,snapshots,timers,saved,counted:()=>counted,refreshes:()=>refreshes,now:()=>now,
    poll:()=>context.pollBulkStoreSetup(queue[state.index],state.probeId)};
}
const turn=()=>new Promise(resolve=>setImmediate(resolve));

test('batch counts only this probe identity, never historical saved bindings',()=>{
  const counts={success:0,failed:0,siteIncomplete:0};
  const code=source.slice(source.indexOf('  function identitySetupSucceeded'), source.indexOf('  function persistBulkSetupCompletionSummary'));
  const ctx=vm.createContext({bulkStoreSetup:counts,updateBulkSetupCounts(){}});
  vm.runInContext(code,ctx);
  const failed={status:'FAILED',identity:{status:'FAILED',identity_confirmed:true},marketplaces:[]};
  const original=JSON.stringify(failed);
  ctx.summarizeBulkStoreResult(failed);
  assert.deepEqual(counts,{success:0,failed:1,siteIncomplete:0});
  assert.equal(JSON.stringify(failed),original);
  ctx.summarizeBulkStoreResult({status:'PARTIAL',identity:{status:'SUCCEEDED'},marketplaces:[{status:'FAILED'}]});
  assert.deepEqual(counts,{success:1,failed:1,siteIncomplete:1});
});

test('WAITING_AUTH keeps GET tracking, never posts continue, and finishes once',async()=>{
  const sequence=['WAITING_AUTH','WAITING_AUTH','CHECKING','SUCCEEDED'];
  const x=fixture({request:async()=>response(sequence.shift())});
  await x.poll();
  assert.equal(x.calls.length,4);assert.ok(x.calls.every(c=>c.method==='GET'));
  assert.equal(x.state.auth,1);assert.equal(x.state.index,1);assert.equal(x.counted(),1);
  assert.equal(x.ui.actions.hidden,true);assert.equal(x.timers.size,1); // final UI reload only
});
test('checking beyond the old 180 polls continues until server terminal',async()=>{
  let n=0;const x=fixture({request:async()=>response(++n<=185?'CHECKING':'SUCCEEDED')});
  await x.poll();assert.equal(n,186);assert.ok(x.now()>360000);assert.equal(x.counted(),1);
});
test('eight stores advance serially, including auth waiting and terminal failure',async()=>{
  const reads=new Map(),posts=[];
  const x=fixture({stores:8,request:async(url,opts)=>{
    const id=Number(url.match(/stores\/(\d+)/)[1]);
    if(opts.method==='POST'){posts.push(id);assert.equal(x.state.index,id-1);return response('CHECKING',id);}
    const n=(reads.get(id)||0)+1;reads.set(id,n);
    return response(n<=2?'WAITING_AUTH':id===4?'FAILED':'SUCCEEDED',id);
  }});
  await x.context.runNextBulkStoreSetup();
  assert.deepEqual(posts,[1,2,3,4,5,6,7,8]);assert.equal(x.counted(),8);
  assert.equal(x.state.index,8);assert.equal(x.state.active,false);assert.equal(x.state.auth,8);
  assert.ok([...reads.values()].every(n=>n===3));
});
test('network and 5xx failures retry only the original GET with retained queue',async()=>{
  let n=0;const x=fixture({request:async()=>{if(++n<=3)throw Object.assign(new Error('temporary'),{statusCode:503});return response('SUCCEEDED');}});
  await x.poll();assert.equal(n,4);assert.equal(x.counted(),1);assert.ok(x.calls.every(c=>c.method==='GET'));
});
test('exhausted retries retain original probe and index, no invented completion',async()=>{
  const x=fixture({request:async()=>{throw new Error('offline');}});
  await x.poll();assert.equal(x.calls.length,4);assert.equal(x.state.index,0);assert.equal(x.counted(),0);
  assert.equal(x.state.probeId,'p1');assert.equal(x.state.active,false);assert.equal(x.state.resumeAvailable,true);
  assert.match(x.ui.start.textContent,/继续追踪原任务/);
});
test('404 and authentication failure do not infer that the window was released',async()=>{
  for(const code of [404,401]){
    const x=fixture({request:async()=>{throw Object.assign(new Error('missing'),{statusCode:code});}});
    await x.poll();assert.equal(x.calls.length,1);assert.equal(x.state.index,0);assert.equal(x.counted(),0);
  }
});
test('hung status request aborts within budget and retries are bounded',async()=>{
  const x=fixture({fireTimeouts:true,request:async(url,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new Error('timeout'))))});
  await x.poll();assert.equal(x.calls.length,4);assert.equal(x.state.active,false);assert.equal(x.counted(),0);
  assert.equal(x.timers.size,0);
});
test('server deadline plus grace pauses observation, not releases or skips',async()=>{
  const x=fixture({request:async()=>response('WAITING_AUTH',1,{expires_in_seconds:0})});
  await x.poll();assert.equal(x.now(),30000);assert.equal(x.state.index,0);assert.equal(x.counted(),0);
  assert.equal(x.state.probeId,'p1');assert.match(x.ui.message.textContent,/截止时间/);
});
test('terminal without cleanup confirmation holds the queue and exposes release controls',async()=>{
  const x=fixture({request:async()=>response('FAILED',1,{cleanup_state:'UNCONFIRMED'})});
  await x.poll();assert.equal(x.state.index,0);assert.equal(x.counted(),0);
  assert.equal(x.state.probeStatus,'RESOURCE_UNCONFIRMED');assert.equal(x.ui.release.hidden,false);
});
test('late in-flight GET cannot overwrite manual continue or double-count terminal',async()=>{
  let resolveGet;
  const x=fixture({request:async(url,opts)=>opts.method==='POST'?response('SUCCEEDED'):new Promise(resolve=>{resolveGet=resolve;})});
  const old=x.poll();await turn();
  await x.context.action('continue-bulk-store-setup',x.ui.resume);
  resolveGet(response('WAITING_AUTH'));await old;
  assert.equal(x.counted(),1);assert.equal(x.state.index,1);assert.equal(x.ui.actions.hidden,true);
  assert.equal(x.calls.filter(c=>c.method==='POST').length,1);
});
test('late GET after skip cannot double-advance queue',async()=>{
  let resolveGet;
  const x=fixture({request:async(url,opts)=>opts.method==='POST'?response('CANCELLED'):new Promise(resolve=>{resolveGet=resolve;})});
  const old=x.poll();await turn();await x.context.action('skip-bulk-store-setup',x.ui.skip);
  resolveGet(response('SUCCEEDED'));await old;
  assert.equal(x.counted(),1);assert.equal(x.state.index,1);
});
test('cancel acknowledgement lost: resume GET, do not repost cancellation',async()=>{
  const x=fixture({request:async(url,opts)=>{if(opts.method==='POST')throw new Error('lost ack');return response('CANCELLED');}});
  await x.context.action('skip-bulk-store-setup',x.ui.skip);await turn();
  assert.deepEqual(x.calls.map(c=>c.method),['POST','GET']);assert.equal(x.counted(),1);
});
test('refresh restores original probe by GET, without opening the current store again',async()=>{
  const x=fixture();x.saved.set('queue',JSON.stringify({...x.state,probeStatus:'WAITING_AUTH'}));
  x.context.restoreBulkStoreSetup();await turn();
  assert.equal(x.calls.length,1);assert.equal(x.calls[0].method,'GET');assert.match(x.calls[0].url,/p1$/);
  assert.equal(x.counted(),1);
});
test('slow card refresh is bounded and stale response cannot advance a new generation',async()=>{
  let resolveRefresh;
  const x=fixture({refresh:async()=>new Promise(resolve=>{resolveRefresh=resolve;})});
  const old=x.poll();await turn();x.context.stopBulkSetupPolling();resolveRefresh();await old;
  assert.equal(x.counted(),0);assert.equal(x.state.index,0);assert.equal(x.timers.size,0);
});
test('capability labels keep unknown and unchecked independent of basic verification',()=>{
  const block=source.slice(source.indexOf('  const setupPaymentLabels'),source.indexOf('  function applyPersistedStoreFacts'));
  const c=vm.createContext({});vm.runInContext(block,c);
  const text=c.setupCapabilityText({identity_check:'PASSED',access_check:'PASSED',payment_capability:'UNKNOWN',inventory_capability:'NOT_CHECKED'});
  assert.match(text,/基础身份\/站点：已核验/);assert.match(text,/付款页面：未识别/);assert.match(text,/库存列表：未检查/);
});
