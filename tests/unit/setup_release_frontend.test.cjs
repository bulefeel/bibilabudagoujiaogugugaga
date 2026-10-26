const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../src/ziniao_automation/static/app.js'),'utf8');
const recover=source.slice(source.indexOf('  function markBulkSetupReleaseConfirmed'),source.indexOf('  async function pollBulkStoreSetup'));
const handler=source.slice(source.indexOf('  async function handleBulkStoreSetupResponse'),source.indexOf('  async function runNextBulkStoreSetup'));
function fixture(cleanup='CONFIRMED'){
  const ui=Object.fromEntries(['panel','title','message','start','actions','release','dismiss'].map(k=>[k,{hidden:true,disabled:false,textContent:''}]));
  const buttons=[{disabled:false},{disabled:false}];
  const state={queue:[{id:6,name:'Fixture'}],index:0,probeId:'probe-1',storeId:6,probeStatus:'RESOURCE_UNCONFIRMED',active:false,resumeAvailable:true};
  const calls=[];
  let next=0;
  const context=vm.createContext({bulkStoreSetup:state,bulkSetupUi:()=>ui,$:()=>null,$$:()=>buttons,
    confirmAction:async()=>true,storeSetupProbePath:(id,pid)=>`/api/stores/${id}/store-setup-probes/${pid}`,
    api:async(url)=>{calls.push(url);return {cleanup_state:cleanup,status:'FAILED'};},
    persistBulkStoreSetup(){},refreshPersistedSetups:async()=>{},toast(){},
    AbortController,setTimeout,clearTimeout,
    storeSetupTerminalStatuses:new Set(['FAILED','SUCCEEDED','CANCELLED']),summarizeBulkStoreResult(){},
    runNextBulkStoreSetup:async()=>{next++;},
  });
  vm.runInContext(recover+'\n'+handler,context);
  context.bulkSetupTrackingPolicy=()=>({batch_poll_request_timeout_ms:10000});
  return {ui,state,calls,buttons,context,next:()=>next};
}
test('release pause exposes both direct controls and queue dismissal',async()=>{
  const x=fixture();
  await x.context.handleBulkStoreSetupResponse(x.state.queue[0],{status:'FAILED',cleanup_state:'UNCONFIRMED'},200);
  assert.equal(x.ui.release.hidden,false);assert.equal(x.ui.dismiss.hidden,false);
  assert.equal(x.ui.actions.hidden,true);assert.equal(x.state.probeStatus,'RESOURCE_UNCONFIRMED');
  assert.equal(x.next(),0);assert.equal(x.state.index,0);
});
test('database-confirmed cleanup clears cached pause without launching work',()=>{
  const x=fixture();x.context.markBulkSetupReleaseConfirmed(x.state.queue[0]);
  assert.equal(x.state.probeStatus,'RESOURCE_RELEASED');assert.equal(x.next(),0);assert.equal(x.state.index,0);
  assert.equal(x.state.active,false);assert.match(x.ui.title.textContent,/已解除阻塞/);
  assert.equal(x.ui.dismiss.hidden,false);assert.equal(x.ui.release.hidden,true);
});
test('checking release calls the actual release check and does not resume automatically',async()=>{
  const x=fixture();await x.context.recoverBulkSetupRelease(false);
  assert.deepEqual(x.calls,['/api/stores/6/store-setup-probes/probe-1/check-release']);
  assert.equal(x.state.probeStatus,'RESOURCE_RELEASED');assert.equal(x.state.index,0);assert.equal(x.next(),0);
  assert.equal(x.state.active,false);assert.equal(x.ui.release.hidden,true);assert.equal(x.ui.dismiss.hidden,false);
});
test('explicit release stops only the current probe environment without starting another',async()=>{
  const x=fixture();await x.context.recoverBulkSetupRelease(true);
  assert.deepEqual(x.calls,['/api/stores/6/store-setup-probes/probe-1/release-environment']);
  assert.equal(x.next(),0);assert.equal(x.state.probeStatus,'RESOURCE_RELEASED');
  assert.equal(x.ui.start.disabled,false);assert.equal(x.buttons.some(b=>b.disabled),false);
});
test('no acknowledgement keeps the block and retry controls',async()=>{
  const x=fixture('UNCONFIRMED');await x.context.recoverBulkSetupRelease(true);
  assert.equal(x.state.probeStatus,'RESOURCE_UNCONFIRMED');assert.equal(x.ui.release.hidden,false);
  assert.equal(x.next(),0);assert.equal(x.state.index,0);
});
test('cancelled confirmation sends no release command',async()=>{
  const x=fixture();x.context.confirmAction=async()=>false;
  await x.context.recoverBulkSetupRelease(true);assert.deepEqual(x.calls,[]);
});
test('network error preserves probe identity and enables recovery buttons',async()=>{
  const x=fixture();x.context.api=async()=>{throw new Error('offline');};
  await x.context.recoverBulkSetupRelease(true);
  assert.equal(x.state.probeId,'probe-1');assert.equal(x.state.index,0);assert.equal(x.next(),0);
  assert.equal(x.ui.start.disabled,false);assert.match(x.ui.message.textContent,/offline/);
});
test('toolbar recheck dispatches release check, never plain status polling',async()=>{
  const action=source.slice(source.indexOf('    if (action === "detect-all-store-setups")'),source.indexOf('    if (action === "continue-bulk-store-setup")'));
  const x=fixture();vm.runInContext('(async function(action,target){'+action+'})("detect-all-store-setups",{})',x.context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(x.calls[0],'/api/stores/6/store-setup-probes/probe-1/check-release');
});
