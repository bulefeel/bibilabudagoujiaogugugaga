const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../../src/ziniao_automation/static/app.js'),'utf8');
const fn=script.slice(script.indexOf('  function applyPersistedStoreFacts'),script.indexOf('  async function refreshPersistedSetups'));
function element(){return {textContent:'',dataset:{},disabled:true,children:[],classList:{values:new Set(),toggle(name,on){if(on)this.values.add(name);else this.values.delete(name);},add(name){this.values.add(name);}},replaceChildren(){this.children=[];},append(...nodes){this.children.push(...nodes);}};}
function setup(){
 const card={dataset:{platformAmazon:'true',identityState:'MISSING'}};
 const keys=['[data-store-identity-value]','[data-store-identity-state]','[data-store-task-state]','[data-store-config-state]','[data-store-markets]','[data-action="edit-store"]','[data-action="new-run"]'];
 const nodes=Object.fromEntries(keys.map(key=>[key,element()]));
 nodes['[data-action="edit-store"]'].dataset.store=JSON.stringify({id:1,name:'Fixture',expected_seller_id:null,marketplaces:[]});
 const context=vm.createContext({$:selector=>nodes[selector],document:{createElement:()=>element()}});vm.runInContext(fn,context);
 return {card,nodes,apply:summary=>context.applyPersistedStoreFacts(card,summary)};
}
function facts(extra={}){return {expected_seller_id:'Current Seller',identity_confirmed:true,store_enabled:true,configured:true,selected_marketplace_codes:['CA','DE'],marketplace_config:[{code:'CA',domain:'sellercentral.amazon.ca',currency:'CAD',enabled:true},{code:'DE',domain:'sellercentral.amazon.de',currency:'EUR',enabled:true}],...extra};}
test('saved identity renders immediately even when site verification failed',()=>{
 const x=setup();x.apply(facts({verification_outcome:'FAILED'}));
 assert.equal(x.nodes['[data-store-identity-value]'].textContent,'Current Seller');
 assert.equal(x.card.dataset.identityState,'CONFIRMED');assert.equal(x.card.dataset.setupState,'done');
 assert.match(x.nodes['[data-store-config-state]'].textContent,/建档已配置/);
 assert.equal(x.nodes['[data-action="new-run"]'].disabled,false);
 assert.equal(x.nodes['[data-store-markets]'].children.length,2);
 const saved=JSON.parse(x.nodes['[data-action="edit-store"]'].dataset.store);
 assert.equal(saved.expected_seller_id,'Current Seller');assert.equal(saved.marketplaces[1].code,'DE');
});
test('reset facts override historical successful identity and disable task',()=>{
 const x=setup();x.apply(facts());x.apply(facts({expected_seller_id:null,identity_confirmed:false,configured:false,store_enabled:false,latest_probe:{identity:{status:'SUCCEEDED',seller_id:'Old Seller'}}}));
 assert.equal(x.nodes['[data-store-identity-value]'].textContent,'尚未绑定');
 assert.equal(x.card.dataset.identityState,'MISSING');assert.equal(x.card.dataset.setupState,'needed');
 assert.equal(x.nodes['[data-action="new-run"]'].disabled,true);
 assert.equal(JSON.parse(x.nodes['[data-action="edit-store"]'].dataset.store).expected_seller_id,null);
});
test('unconfirmed identity and disabled task remain distinct',()=>{
 const x=setup();x.apply(facts({identity_confirmed:false,configured:false}));
 assert.equal(x.card.dataset.identityState,'UNCONFIRMED');assert.equal(x.nodes['[data-action="new-run"]'].disabled,true);
 x.apply(facts({store_enabled:false}));assert.match(x.nodes['[data-store-config-state]'].textContent,/建档仍保留/);
 assert.equal(x.card.dataset.setupState,'done');assert.equal(x.nodes['[data-action="new-run"]'].disabled,true);
});
test('seller names are text not HTML, and unrelated pages tolerate absent nodes',()=>{
 const x=setup();x.apply(facts({expected_seller_id:'<img src=x onerror=alert(1)>'}));
 assert.equal(x.nodes['[data-store-identity-value]'].innerHTML,undefined);
 for(const key of Object.keys(x.nodes))delete x.nodes[key];assert.doesNotThrow(()=>x.apply(facts()));
});
test('non Amazon sites stay outside onboarding and DE-only keeps payout disabled',()=>{
 const x=setup();x.card.dataset.platformAmazon='false';x.apply(facts());
 assert.equal(x.card.dataset.setupState,'n/a');assert.equal(x.nodes['[data-action="new-run"]'].disabled,true);
 x.card.dataset.platformAmazon='true';x.apply(facts({selected_marketplace_codes:['DE']}));assert.equal(x.nodes['[data-action="new-run"]'].disabled,true);
});
// Execute the production renderer-snapshot JS, not a separately copied parser.
const py=fs.readFileSync(path.join(__dirname,'../../src/ziniao_automation/workflows/store_setup/identity.py'),'utf8');
const snapshot=vm.runInNewContext(py.split('DASHBOARD_IDENTITY_SNAPSHOT = r"""')[1].split('"""')[0],{getComputedStyle:node=>node.style});
function dom(attrs={},text='',visible=true){return {style:{display:visible?'block':'none',visibility:'visible'},getClientRects:()=>visible?[{}]:[],getAttribute:key=>attrs[key]||null,innerText:text};}
test('identity DOM snapshot preserves meta and visible current text only',()=>{
 const output=snapshot([dom({content:'MACHINE'},'',false),dom({},'Old Hidden Seller',false),dom({label:'Display | Canada'},'Display | Canada')],50);
 assert.equal(output.items[0].machine[0][0],'MACHINE');assert.equal(output.items[1].visible.length,0);
 assert.equal(output.items[2].visible[0][0],'Display | Canada');
});
test('identity DOM snapshot enforces limit without returning a partial identity',()=>{
 assert.equal(snapshot([dom(),dom()],1).truncated,true);
 assert.equal(snapshot([dom(),dom()],1).items.length,0);
});
