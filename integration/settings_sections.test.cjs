/* Exercise real independently scoped dashboard forms and settings callbacks. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const template=fs.readFileSync(path.join(__dirname,'../app/pulsefeed/templates/prompt_settings.html'),'utf8');
const script=fs.readFileSync(path.join(__dirname,'../app/pulsefeed/static/settings.js'),'utf8');
const initial={qwen_template:'System instructions',qwen_article_template:'Article data:\n{"title": {{article_title}}, "excerpt": {{article_excerpt}}}',flux_prefix:'Style {{qwen_prompt}}',fallback_prompt:'A newspaper',blocked_terms:'restricted',flux_template_version:1};
class Element {
 constructor(tag='div'){this.tagName=tag.toUpperCase();this._value='';this.children=[];this.listeners={};this.attributes={};this.dataset={};this.disabled=false;this.hidden=false;this._text='';this.classList={toggle(){}};}
 set value(value){this._value=this.tagName==='TEXTAREA'?String(value).replace(/\r\n?/g,'\n'):String(value);}get value(){return this._value;}
 set innerHTML(value){throw Error('No HTML interpretation');}set textContent(value){this._text=String(value);this.children=[];}get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
 addEventListener(type,fn){(this.listeners[type]||=[]).push(fn);}setAttribute(name,value){this.attributes[name]=String(value);}append(...children){this.children.push(...children);}replaceChildren(...children){this._text='';this.children=children;}
 closest(){return this.section;}focus(){this.focused=true;}setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}setCustomValidity(value){this.validity=value;}
 querySelectorAll(){return this.controls||[];}checkValidity(){return this.controls?this.controls.every(control=>!control.validity&&(!control.required||control.value.trim())):!this.validity;}reportValidity(){this.reported=true;}
}
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return{promise,resolve};};
const response=(data,status=200,headers={})=>({ok:status>=200&&status<300,status,headers:new Map(Object.entries(headers)),json:async()=>data});
function setup(fetcher=()=>response(initial),settings=initial) {
 const elements=new Map(),forms={};
 for(const match of template.matchAll(/<([a-z]+)\b[^>]*\bid="([\w-]+)"[^>]*>/g))elements.set(match[2],new Element(match[1]));
 for(const match of template.matchAll(/<form\b[^>]*id="([\w-]+)"[^>]*data-settings-section="([a-z]+)"[^>]*>([\s\S]*?)<\/form>/g)) {
  const [all,id,key,html]=match,form=elements.get(id);form.controls=[];form.section=elements.get(`${key==='content'?'content':key}-settings-section`);forms[key]=form;
  for(const control of html.matchAll(/<(textarea|input|button)\b([^>]*)>/g)) {
   const attrs=Object.fromEntries([...control[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(attr=>[attr[1],attr[2]]));
   const element=attrs.id?elements.get(attrs.id):new Element(control[1]);element.name=attrs.name;element.section=form.section;element.form=form;element.required=/\brequired\b/.test(control[2]);
   if(attrs.name)element.value=settings[attrs.name];form.controls.push(element);
  }
 }
 for(const id of ['admin-bootstrap','admin-dashboard','admin-access-expired','usage-rows'])elements.set(id,new Element());
 elements.get('qwen-editor-data').textContent=JSON.stringify({system:settings.qwen_template,article:settings.qwen_article_template});
 elements.get('admin-bootstrap').textContent=JSON.stringify({account:{id:'admin',role:'admin'},csrf:'csrf-token'});
 elements.get('admin-access-expired').hidden=true;
 let now=1000;class Clock extends Date{static now(){return now;}}
 const requests=[],timers=[],events={};
 function schedule(fn,delay,interval=false){const timer={fn,delay,at:now+delay,interval,cancelled:false};timers.push(timer);return timer;}
 function cancel(timer){if(timer)timer.cancelled=true;}
 const window={addEventListener(type,fn){(events[type]||=[]).push(fn);},dispatchEvent(event){for(const fn of events[event.type]||[])fn(event);}};
 const context={document:{getElementById:id=>elements.get(id)||null,createElement:tag=>new Element(tag)},window,console,Date:Clock,Event,AbortController,
  setTimeout:(fn,delay)=>schedule(fn,delay),clearTimeout:cancel,setInterval:(fn,delay)=>schedule(fn,delay,true),clearInterval:cancel,
  fetch:async(url,options)=>{const request={url,options,body:JSON.parse(options.body)};requests.push(request);return fetcher(request);}};
 vm.createContext(context);vm.runInContext(script,context);
 async function submit(key){const form=forms[key],event={prevented:false,stopped:false,preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;}};
  for(const fn of form.listeners.submit||[]){await fn(event);if(event.stopped)break;}return event;}
 return{elements,forms,requests,timers,events,window,submit,activeTimers:()=>timers.filter(timer=>!timer.cancelled),advance(ms){
  const target=now+ms;
  for(let n=0;n<1000;n++){
   const timer=timers.filter(timer=>!timer.cancelled&&timer.at<=target).sort((a,b)=>a.at-b.at)[0];if(!timer)break;
   now=timer.at;if(timer.interval)timer.at+=timer.delay;else timer.cancelled=true;timer.fn();
  }
  now=target;
 }};
}
async function until(predicate){for(let n=0;n<50&&!predicate();n++)await Promise.resolve();assert(predicate(),'Expected pending callback to settle');}
let tests=0;async function test(name,fn){await fn();tests++;console.log('PASS '+name);}
(async()=>{
 await test('forms read from actual partial contain exactly their own named fields',async()=>{
  const h=setup();assert.deepEqual(h.forms.qwen.controls.filter(c=>c.name).map(c=>c.name),['qwen_template','qwen_article_template']);
  assert.deepEqual(h.forms.flux.controls.filter(c=>c.name).map(c=>c.name),['flux_prefix']);assert.deepEqual(h.forms.content.controls.filter(c=>c.name).map(c=>c.name),['fallback_prompt','blocked_terms']);
  assert.equal(h.requests.length,0);
 });
 await test('Qwen saves only its fields despite invalid unsaved FLUX and preserves every other draft',async()=>{
  const h=setup(request=>response({...initial,...request.body}));h.elements.get('flux-prefix').value='UNSAVED INVALID FLUX';h.elements.get('flux-prefix').listeners.input[0]();
  h.elements.get('blocked-terms').value='UNSAVED CONTENT';await h.submit('qwen');assert.equal(h.requests.length,1);
  assert.equal(h.requests[0].url,'/api/admin/settings/qwen');assert.deepEqual(Object.keys(h.requests[0].body).sort(),['qwen_article_template','qwen_template']);
  assert.equal(h.elements.get('flux-prefix').value,'UNSAVED INVALID FLUX');assert.equal(h.elements.get('blocked-terms').value,'UNSAVED CONTENT');assert(h.elements.get('qwen-settings-feedback').textContent.startsWith('Saved.'));
 });
 await test('section lock prevents duplicate writes while other forms can save concurrently',async()=>{
  const pending=deferred();const h=setup(request=>request.url.endsWith('/qwen')?pending.promise:response({...initial,...request.body}));
  const first=h.submit('qwen');for(let n=0;n<20&&!h.requests.length;n++)await Promise.resolve();assert(h.forms.qwen.controls.every(c=>c.disabled));assert(h.forms.flux.controls.every(c=>!c.disabled));
  await h.submit('qwen');await h.submit('content');assert.equal(h.requests.length,2);
  pending.resolve(response(initial));await first;assert(h.forms.qwen.controls.every(c=>!c.disabled));
 });
 await test('save POST uses session CSRF, same-origin credentials and only content fields',async()=>{
  const h=setup();await h.submit('content');const request=h.requests[0];assert.equal(request.options.headers['X-CSRF-Token'],'csrf-token');assert.equal(request.options.credentials,'same-origin');assert.equal(request.options.cache,'no-store');
  assert.deepEqual(request.body,{fallback_prompt:'A newspaper',blocked_terms:'restricted'});
 });
 await test('invalid Qwen stops only Qwen save and native validation reveals its section',async()=>{
  const h=setup();h.elements.get('qwen-prompt-editor').value='missing';h.elements.get('qwen-prompt-editor').listeners.input[0]();await h.submit('qwen');assert.equal(h.requests.length,0);
  assert(h.forms.qwen.section.open);await h.submit('flux');assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,'/api/admin/settings/flux');
 });
 await test('unknown network/JSON/incomplete success never claims Saved and keeps draft values',async()=>{
  for(const fetcher of [()=>{throw Error('offline');},()=>({ok:true,status:200,json:async()=>{throw Error('truncated');}}),()=>response({}),()=>response({...initial,qwen_template:'wrong'})]) {
   const h=setup(fetcher);await h.submit('qwen');const text=h.elements.get('qwen-settings-feedback').textContent;assert(!text.startsWith('Saved.'));assert(text.includes('confirm'));assert.equal(h.elements.get('qwen-template').value,initial.qwen_template);
  }
 });
 await test('429 cools down only the rejected section and blocks duplicate manual writes until reset',async()=>{
  let limited=true;const h=setup(request=>request.url.endsWith('/flux')&&limited?response({error:{message:'Wait'}},429,{'Retry-After':'5'}):response({...initial,...request.body}));
  await h.submit('flux');assert(h.forms.flux.controls.every(c=>c.disabled));assert(h.forms.qwen.controls.every(c=>!c.disabled));assert(h.elements.get('flux-settings-feedback').textContent.includes('5 seconds'));
  await h.submit('flux');assert.equal(h.requests.length,1);await h.submit('content');assert.equal(h.requests.length,2);
  limited=false;h.advance(5000);await h.submit('flux');assert.equal(h.requests.length,3);
 });
 await test('BFCache restoration after a cooldown deadline unlocks the section without replacing drafts',async()=>{
  const h=setup(()=>response({error:{message:'Wait'}},429,{'Retry-After':'5'}));
  h.elements.get('flux-prefix').value='Draft {{qwen_prompt}}';h.elements.get('blocked-terms').value='Other unsaved draft';
  await h.submit('flux');h.window.dispatchEvent(new Event('pagehide'));assert.equal(h.activeTimers().length,0);
  h.advance(6000);assert(h.forms.flux.controls.every(c=>c.disabled));h.window.dispatchEvent(new Event('pageshow'));
  assert(h.forms.flux.controls.every(c=>!c.disabled));assert.equal(h.elements.get('flux-prefix').value,'Draft {{qwen_prompt}}');
  assert.equal(h.elements.get('blocked-terms').value,'Other unsaved draft');assert.equal(h.requests.length,1);assert.equal(h.activeTimers().length,0);
 });
 await test('BFCache restoration before a cooldown deadline keeps its absolute remaining time',async()=>{
  const h=setup(()=>response({error:{message:'Wait'}},429,{'Retry-After':'5'}));
  await h.submit('flux');h.advance(1200);h.window.dispatchEvent(new Event('pagehide'));h.advance(1300);
  h.window.dispatchEvent(new Event('pageshow'));assert(h.forms.flux.controls.every(c=>c.disabled));assert(h.forms.qwen.controls.every(c=>!c.disabled));
  assert.equal(h.activeTimers().length,1);await h.submit('flux');assert.equal(h.requests.length,1);
  h.advance(2499);assert(h.forms.flux.controls.every(c=>c.disabled));h.advance(1);
  assert(h.forms.flux.controls.every(c=>!c.disabled));assert.equal(h.activeTimers().length,0);assert.equal(h.requests.length,1);
 });
 await test('stalled save headers abort at the deadline, release controls and never replay the POST',async()=>{
  const pending=deferred(),h=setup(()=>pending.promise);const save=h.submit('flux');await until(()=>h.requests.length===1);
  const signal=h.requests[0].options.signal;assert(signal);h.advance(29999);assert(!signal.aborted);assert(h.forms.flux.controls.every(c=>c.disabled));
  h.advance(1);await save;assert(signal.aborted);assert(h.forms.flux.controls.every(c=>!c.disabled));assert.equal(h.activeTimers().length,0);
  assert(h.elements.get('flux-settings-feedback').textContent.includes('check the stored settings'));assert.equal(h.elements.get('flux-prefix').value,initial.flux_prefix);
  pending.resolve(response(initial));await Promise.resolve();await Promise.resolve();h.advance(60000);
  assert.equal(h.requests.length,1);assert(!h.elements.get('flux-settings-feedback').textContent.startsWith('Saved.'));
 });
 await test('the same save deadline covers stalled JSON after slow response headers',async()=>{
  const headers=deferred(),body=deferred();let readingBody=false;const h=setup(()=>headers.promise),save=h.submit('qwen');await until(()=>h.requests.length===1);
  h.advance(20000);headers.resolve({...response(initial),json:()=>{readingBody=true;return body.promise;}});await until(()=>readingBody);
  h.advance(9999);assert(!h.requests[0].options.signal.aborted);h.advance(1);await save;
  assert(h.requests[0].options.signal.aborted);assert(h.forms.qwen.controls.every(c=>!c.disabled));assert.equal(h.activeTimers().length,0);
  body.resolve(initial);await Promise.resolve();await Promise.resolve();assert(!h.elements.get('qwen-settings-feedback').textContent.startsWith('Saved.'));assert.equal(h.requests.length,1);
 });
 await test('pagehide aborts an unconfirmed save and restoration preserves drafts without automatic retry',async()=>{
  const pending=deferred(),h=setup(()=>pending.promise);
  const drafts={'qwen-prompt-editor':h.elements.get('qwen-prompt-editor').value+' draft','flux-prefix':'Changed {{qwen_prompt}}','blocked-terms':'Other draft'};
  for(const [id,value] of Object.entries(drafts)){h.elements.get(id).value=value;for(const fn of h.elements.get(id).listeners.input||[])fn();}
  const save=h.submit('flux');await until(()=>h.requests.length===1);h.window.dispatchEvent(new Event('pagehide'));
  assert(h.requests[0].options.signal.aborted);assert.equal(h.activeTimers().length,0);await save;
  assert(Object.values(h.forms).every(form=>form.controls.every(c=>c.disabled)));
  h.window.dispatchEvent(new Event('pageshow'));assert(Object.values(h.forms).every(form=>form.controls.every(c=>!c.disabled)));
  for(const [id,value] of Object.entries(drafts))assert.equal(h.elements.get(id).value,value);
  assert(h.elements.get('flux-settings-feedback').textContent.includes('check the stored settings'));assert.equal(h.requests.length,1);
  pending.resolve(response({...initial,flux_prefix:drafts['flux-prefix']}));await Promise.resolve();await Promise.resolve();
  assert(!h.elements.get('flux-settings-feedback').textContent.startsWith('Saved.'));
 });
 await test('a response from before pagehide cannot finish or unlock a newer manual save',async()=>{
  const first=deferred(),second=deferred(),h=setup(()=>h.requests.length===1?first.promise:second.promise);
  const oldSave=h.submit('flux');await until(()=>h.requests.length===1);h.window.dispatchEvent(new Event('pagehide'));h.window.dispatchEvent(new Event('pageshow'));
  h.elements.get('flux-prefix').value='New {{qwen_prompt}}';const newSave=h.submit('flux');await until(()=>h.requests.length===2);
  first.resolve(response(initial));await oldSave;assert(h.forms.flux.controls.every(c=>c.disabled));assert.equal(h.elements.get('flux-settings-feedback').textContent,'Saving…');
  second.resolve(response({...initial,...h.requests[1].body}));await newSave;assert(h.forms.flux.controls.every(c=>!c.disabled));
  assert(h.elements.get('flux-settings-feedback').textContent.startsWith('Saved.'));assert.equal(h.requests.length,2);assert.equal(h.activeTimers().length,0);
 });
 await test('401 and lost admin403 hide dashboard and disable every settings section',async()=>{
  for(const status of [401,403]){const h=setup(()=>response({error:{code:'FORBIDDEN'}},status));await h.submit('flux');assert(h.elements.get('admin-dashboard').hidden);assert(!h.elements.get('admin-access-expired').hidden);assert(Object.values(h.forms).every(form=>form.controls.every(c=>c.disabled)));}
 });
 await test('CSRF failure remains inline without exposing a false session expiry',async()=>{
  const h=setup(()=>response({error:{code:'CSRF_FAILED',message:'Reload to verify this request'}},403));await h.submit('content');assert(!h.elements.get('admin-dashboard').hidden);assert(h.elements.get('content-settings-feedback').textContent.includes('Reload'));
 });
 await test('external admin expiry stops a late settings success and cannot announce Saved',async()=>{
  const pending=deferred();const h=setup(()=>pending.promise);const save=h.submit('qwen');for(let n=0;n<20&&!h.requests.length;n++)await Promise.resolve();
  h.window.dispatchEvent(new Event('pulsefeed:admin-expired'));assert(h.requests[0].options.signal.aborted);assert.equal(h.activeTimers().length,0);await save;
  h.window.dispatchEvent(new Event('pageshow'));assert(Object.values(h.forms).every(form=>form.controls.every(c=>c.disabled)));
  pending.resolve(response(initial));await Promise.resolve();await Promise.resolve();assert(h.elements.get('admin-dashboard').hidden);assert(!h.elements.get('qwen-settings-feedback').textContent.startsWith('Saved.'));assert.equal(h.requests.length,1);
 });
 await test('editing a saved field marks only its own section unsaved and a confirmed save clears that status',async()=>{
  const h=setup(request=>response({...initial,...request.body}));
  for(const key of ['qwen','flux','content'])await h.submit(key);
  for(const [key,id] of [['qwen','qwen-prompt-editor'],['flux','flux-prefix'],['content','blocked-terms']]){
   h.elements.get(id).value+=' draft';for(const fn of h.elements.get(id).listeners.input||[])fn();
   assert.equal(h.elements.get(`${key}-settings-feedback`).textContent,'Unsaved changes.');
   for(const other of ['qwen','flux','content'].filter(other=>other!==key))assert(h.elements.get(`${other}-settings-feedback`).textContent.startsWith('Saved.'));
   await h.submit(key);assert(h.elements.get(`${key}-settings-feedback`).textContent.startsWith('Saved.'));
  }
 });
 await test('combined editor saves exactly two canonical roles without the divider and preserves original CRLF',async()=>{
  const settings={...initial,qwen_template:'  System\r\n',qwen_article_template:'\r\n'+initial.qwen_article_template+' \t'};
  const h=setup(request=>response({...settings,...request.body}),settings);await h.submit('qwen');assert.deepEqual(h.requests[0].body,{qwen_template:settings.qwen_template,qwen_article_template:settings.qwen_article_template});
  const editor=h.elements.get('qwen-prompt-editor');editor.value=editor.value.replace('System','Changed');for(const fn of editor.listeners.input||[])fn();await h.submit('qwen');
  assert.deepEqual(h.requests[1].body,{qwen_template:'  Changed\n',qwen_article_template:settings.qwen_article_template});assert(!h.requests[1].body.qwen_template.includes('--- ARTICLE CONTEXT ---'));
 });
 await test('invalid combined drafts remain dirty, block Qwen only and never serialize stale hidden values',async()=>{
  const h=setup(request=>response({...initial,...request.body}));await h.submit('qwen');const editor=h.elements.get('qwen-prompt-editor');
  editor.value=editor.value.replace('--- ARTICLE CONTEXT ---','');for(const fn of editor.listeners.input||[])fn();assert.equal(h.elements.get('qwen-settings-feedback').textContent,'Unsaved changes.');
  await h.submit('qwen');assert.equal(h.requests.length,1);assert(h.forms.qwen.section.open&&editor.reported);await h.submit('flux');assert.equal(h.requests[1].url,'/api/admin/settings/flux');
 });
 await test('Qwen Add controls and preview drag edits mark only their saved section unsaved',async()=>{
  const h=setup(request=>response({...initial,...request.body}));for(const key of ['qwen','flux','content'])await h.submit(key);
  const editor=h.elements.get('qwen-prompt-editor'),marker='{{article_title}}';editor.value=editor.value.replace(marker,'');for(const fn of editor.listeners.input||[])fn();
  assert.equal(h.elements.get('qwen-settings-feedback').textContent,'Unsaved changes.');editor.selectionStart=0;for(const fn of h.elements.get('add-article-title').listeners.click||[])fn();await h.submit('qwen');
  assert(h.elements.get('qwen-settings-feedback').textContent.startsWith('Saved.'));
  for(const fn of editor.listeners.drop||[])fn({dataTransfer:{getData:()=> 'article-title'}});const start=editor.value.indexOf('--- ARTICLE CONTEXT ---')+'--- ARTICLE CONTEXT ---'.length+1;
  editor.value=editor.value.slice(0,start)+marker+editor.value.slice(start);editor.selectionEnd=start+marker.length;for(const fn of editor.listeners.input||[])fn();
  assert.equal(h.elements.get('qwen-settings-feedback').textContent,'Unsaved changes.');assert(h.elements.get('flux-settings-feedback').textContent.startsWith('Saved.'));assert(h.elements.get('content-settings-feedback').textContent.startsWith('Saved.'));
 });
 await test('FLUX manual token edits and native drops still mark only FLUX feedback unsaved',async()=>{
  const h=setup(request=>response({...initial,...request.body}));for(const key of ['qwen','flux','content'])await h.submit(key);
  const field=h.elements.get('flux-prefix'),marker='{{qwen_prompt}}';field.value=marker+' Style';for(const fn of field.listeners.input||[])fn();
  assert.equal(h.elements.get('flux-settings-feedback').textContent,'Unsaved changes.');await h.submit('flux');
  for(const fn of field.listeners.drop||[])fn({dataTransfer:{getData:type=>type==='application/x-pulsefeed-token'?'qwen':''}});field.value+=' '+marker;field.selectionEnd=field.value.length;for(const fn of field.listeners.input||[])fn();
  assert.equal(h.elements.get('flux-settings-feedback').textContent,'Unsaved changes.');assert(h.elements.get('qwen-settings-feedback').textContent.startsWith('Saved.'));assert(h.elements.get('content-settings-feedback').textContent.startsWith('Saved.'));
 });
 console.log(JSON.stringify({result:'PASS',tests}));
})().catch(error=>{console.error(error);process.exitCode=1;});
