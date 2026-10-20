const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '../app/pulsefeed/static/app.js'), 'utf8').replace('  start();', '  window.testHooks = {state, saveImage, readImage, recoverImage, api, acceptBatch, expireSession, safeSource, metric, store};');
class Element {
  constructor() { this.children=[]; this.dataset={}; this.hidden=false; this.disabled=false; this.textContent=''; this.className=''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  addEventListener() {}
  setAttribute() {}
  removeAttribute() {}
  close() { this.closed=true; }
}
function setup() {
  const elements = new Map();
  const document = {getElementById(id) { if(id==='storage-bootstrap')return null;if(!elements.has(id)) elements.set(id,new Element()); return elements.get(id); },createElement(){const element=new Element();element.decode=async()=>{};return element;},querySelectorAll(){return [];},body:new Element()};
  document.getElementById('bootstrap').textContent=JSON.stringify({account:{id:'account-A'},csrf:'session-csrf',view:{id:'view-A',url:'/feeds/view-A',created_at:Date.now()/1000,expires_at:Date.now()/1000+3600},fake_mode:true});
  const requests=[];
  const context={document,window:{addEventListener(){}},console,AbortController,Blob,URL,Set,Map,Date,JSON,Promise,Number,String,Array,Error,Math,crypto:require('crypto').webcrypto,
    setInterval(){return 1;},clearInterval(){},setTimeout(){return 1;},clearTimeout(){},fetch:async(path,options)=>{requests.push({path,options});return {ok:true,status:200,json:async()=>({ok:true}),headers:new Map(),blob:async()=>new Blob(['test'],{type:'image/webp'})};}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../app/pulsefeed/static/image-store.js'),'utf8'),context);context.window.pulseImages=new context.window.PulseImageStore(Date.now()/1000);vm.runInContext(source,context);
  const hooks=context.window.testHooks;hooks.state.account={id:'account-A'};
  return {hooks,context,elements,requests};
}
function transactionDB(options={}) {
  const records=[]; const reads=[]; let current;
  const db={transaction(store,mode){
    current={objectStore(){return {put(record){records.push(record);return {};},get(key){reads.push(key);const request={};queueMicrotask(()=>{request.result=options.savedBlob?{blob:options.savedBlob,accountId:key.split(':')[0],viewId:'view-A',expiresAt:Date.now()+3600000}:null;request.onsuccess();});return request;}};}};
    if(mode==='readwrite'&&options.autoCommit)queueMicrotask(()=>current.oncomplete());
    if(mode==='readwrite'&&options.fail)queueMicrotask(()=>{current.error=new Error('Synthetic quota failure');current.onabort();});
    return current;
  }};
  return {db,records,reads,get transaction(){return current;}};
}
(async()=>{
  const tests=[];
  {
    const {hooks}=setup();const database=transactionDB();hooks.store.db=database.db;
    const blob=new Blob(['image'],{type:'image/webp'});let settled=false;
    const pending=hooks.saveImage('run-1',blob).then(()=>{settled=true;});
    for(let i=0;i<8&&!database.records.length;i++)await Promise.resolve();assert.equal(settled,false);
    assert.equal(database.records[0].key,'account-A:view-A:run-1');assert.equal(database.records[0].blob,blob);
    assert(!database.records[0].csrf);
    database.transaction.oncomplete();await pending;assert.equal(settled,true);
    tests.push('save resolves only after transaction commit; account/run key stores Blob without credentials');
  }
  {
    const {hooks}=setup();const database=transactionDB({fail:true});hooks.store.db=database.db;
    await assert.rejects(hooks.saveImage('run-2',new Blob(['image'])),/write failed/);
    tests.push('aborted browser write reports failure');
  }
  {
    const {hooks,requests}=setup();const database=transactionDB({savedBlob:new Blob(['image'],{type:'image/webp'})});hooks.store.db=database.db;
    const run={id:'run-3',item_id:'item-3',state:'completed',availability:'unavailable'};
    hooks.state.items=[{id:'item-3'}];hooks.state.runs.set(run.id,run);
    await hooks.recoverImage(run);
    assert.deepEqual(database.reads,['account-A:view-A:run-3']);assert.equal(requests.length,0);assert.equal(hooks.state.images.get('run-3').saved,true);
    hooks.state.account={id:'account-B'};await hooks.readImage('run-3');assert.equal(database.reads[1],'account-B:view-A:run-3');
    tests.push('local restoration precedes provider expiry and reads are partitioned by authenticated account');
  }
  {
    const {hooks,context,requests}=setup();const database=transactionDB({fail:true});hooks.store.db=database.db;
    context.fetch=async(path,options)=>{requests.push({path,options});return {ok:true,status:200,headers:new Map([['Content-Type','image/webp']]),blob:async()=>new Blob(['image'],{type:'image/webp'})};};
    await hooks.recoverImage({id:'run-4',state:'completed',availability:'available_to_retrieve'});
    const image=hooks.state.images.get('run-4');assert.equal(image.saved,false);assert(image.saveError.includes('not saved'));assert.equal(image.blob.size,5);
    assert.equal(requests.length,1);assert.equal(requests[0].path,'/api/runs/run-4/image?view_id=view-A');assert.equal(requests[0].options.credentials,'same-origin');
    tests.push('write failure retains viewable image for retry save and only retrieves an existing result');
  }
  {
    const {hooks,context,requests}=setup();hooks.store.db=transactionDB().db;
    context.fetch=async(path,options)=>{requests.push({path,options});return {status:410};};
    await hooks.recoverImage({id:'run-5',state:'completed',availability:'available_to_retrieve'});
    assert(hooks.state.unavailable.has('run-5'));assert.equal(hooks.state.images.size,0);assert.equal(requests.length,1);
    tests.push('expired result becomes unavailable with no inference submission');
  }
  {
    const {hooks,context,requests,elements}=setup();
    await hooks.api('/api/batches',{method:'POST',body:{item_ids:['item-1'],idempotency_key:'key'}});
    assert.equal(requests[0].options.headers['X-CSRF-Token'],'session-csrf');assert.equal(requests[0].options.credentials,'same-origin');
    assert.deepEqual(JSON.parse(requests[0].options.body).item_ids,['item-1']);
    context.fetch=async()=>({status:401});await assert.rejects(hooks.api('/api/runs'),/expired/);
    assert.equal(hooks.state.expired,true);assert.equal(elements.get('studio').hidden,true);assert.equal(elements.get('session-expired').hidden,false);assert.equal(hooks.state.images.size,0);
    tests.push('mutation sends same-origin CSRF and expired auth clears protected display');
  }
  {
    const {hooks,context}=setup();context.fetch=async()=>({status:429,ok:false,json:async()=>({error:{message:'Slow down'}}),headers:new Map([['Retry-After','7']])});
    const before=Date.now();await assert.rejects(hooks.api('/api/batches',{method:'POST',body:{}}),/7 seconds/);assert(hooks.state.cooldownUntil>=before+7000);
    hooks.acceptBatch({id:'held-batch',state:'active',runs:[]});assert.equal(hooks.state.batchId,'held-batch');
    assert.equal(hooks.safeSource('javascript:alert(1)'),null);assert.equal(hooks.safeSource('https://example.org/story'),'https://example.org/story');
    const list=new Element();hooks.metric(list,'Tokens',null);hooks.metric(list,'Attempts',0);assert.equal(list.children[1].textContent,'Unavailable');assert.equal(list.children[3].textContent,'0');
    tests.push('rate-limit cooldown, held capacity, safe links, and unavailable versus zero telemetry');
  }
  console.log(JSON.stringify({result:'PASS',tests:tests.length,checks:tests},null,2));
})().catch(error=>{console.error(error);process.exit(1);});
