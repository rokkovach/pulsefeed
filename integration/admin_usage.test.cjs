/* Execute the real admin callbacks with DOM/network doubles; no cloud operations. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(root, 'app/pulsefeed/static/admin.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'app/pulsefeed/templates/admin.html'), 'utf8');
class Element {
  constructor(tag='div') { this.tagName=tag.toUpperCase(); this.children = []; this.hidden = false; this.value = ''; this.listeners = {}; this.attributes = {}; this._text = ''; }
  set innerHTML(value) { throw Error('Usage content must remain text-only'); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent || '').join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  focus() { this.focused = true; }
}
const quotas = (limit = 100, articles = 10) => ({ global: { limit, used: 3, reserved: 2, uncertain_charged: 1, charged: 6, remaining: Math.max(0, limit - 6), window_start: 1791936000, reset_at: 1792022400 }, page: { article_limit: articles, max_articles: 10 }, hourly: {limit: 30, used: 4, remaining: 26, window_seconds: 3600, next_slot_at: 1791972000} });
const metric = (total, reported = 1, missing = 0) => ({ total, reported, missing });
function model(stage = 'qwen') {
  return { stage, model_id: stage === 'qwen' ? 'nvidia/Qwen3-8B-NVFP4' : 'black-forest-labs/FLUX.2-klein-4B', execution_mode: 'synthetic', invocations: 2, succeeded: 1, failed: 1, pending: 0, unknown: 0,
    tokens_applicable: stage === 'qwen', images_applicable: stage === 'flux', input_tokens: metric(20, 1, 1), output_tokens: metric(null, 0, 2), total_tokens: metric(null, 0, 2), image_count: metric(0, 1, 1), inference_attempts: metric(2, 1, 1), inference_retries: metric(1, 1, 1) };
}
const report = (window = 'today', limit = 100, articles = 10) => ({ window, generated_at: 1791972000, models: [model(), model('flux')], quotas: quotas(limit, articles) });
function timelineReport(){
 const data=report(),start=Date.UTC(2026,9,15);data.generated_at=new Date(start+2.5*3600000).toISOString();
 data.timeline={bucket_seconds:3600,start:new Date(start).toISOString(),end:new Date(start+86400000).toISOString(),unbucketed_invocations:0,buckets:Array.from({length:24},(_,index)=>({start:new Date(start+index*3600000).toISOString(),end:new Date(start+(index+1)*3600000).toISOString(),models:[]}))};return data;
}
const response = (data, status = 200, headers = {}) => ({ ok: status >= 200 && status < 300, status, json: async () => data, headers: new Map(Object.entries(headers)) });
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
function setup(fetcher = () => response(report()), bootstrap = {}) {
  const elements = new Map([...template.matchAll(/\bid="([\w-]+)"/g)].map(match => [match[1], new Element()]));
  const boot = { account: { id: 'admin-a', role: 'admin' }, csrf: 'test-csrf', fake_mode: true, quotas: quotas(), ...bootstrap };
  elements.get('admin-bootstrap').textContent = JSON.stringify(boot);
  const requests = [], timers = [], events = {};
  let now=Date.now();class Clock extends Date {static now(){return now;}}
  const schedule=(fn,delay,type)=>{timers.push({fn,delay,at:now+delay,type});return timers.length;};
  const cancel=id=>{if(timers[id-1])timers[id-1].cancelled=true;};
  elements.get('usage-rows').append(elements.get('qwen-usage-rows'),elements.get('flux-usage-rows'));
  const document = { getElementById: id => elements.get(id), createElement: tag => new Element(tag), createElementNS: (_namespace,tag) => new Element(tag), querySelectorAll: () => [...elements.values()] };
  const context = { document, console, URL, Date:Clock, Number, String, Array, Math, JSON, Error, Promise, AbortController,
    window: { location: { href: 'http://localhost/admin', reload() {} }, history: { replaceState() {} }, addEventListener: (type, fn) => { events[type] = fn; } },
    setInterval: (fn, delay) => schedule(fn,delay,'interval'), clearInterval:cancel,
    setTimeout: (fn,delay)=>schedule(fn,delay,'timeout'),clearTimeout:cancel,
    fetch: async (url, options) => { const request = { url, options, body: options.body ? JSON.parse(options.body) : null }; requests.push(request); return fetcher(request); } };
  assert.match(script, /^  start\(\);$/m);
  const exposed = script.replace(/^  start\(\);$/m, '  window.testHooks = {state, api, renderQuotas, renderUsage, refreshUsage, saveLimits, armReset, cancelReset, confirmReset, start, validTimeline, bucketMetric, renderReports};');
  vm.createContext(context); vm.runInContext(exposed, context);
  async function advance(ms) {
    const target=now+ms;
    for(let count=0;count<10000;count++){
      const timer=timers.filter(timer=>!timer.cancelled&&!timer.fired&&timer.at<=target).sort((a,b)=>a.at-b.at)[0];
      if(!timer)break;now=timer.at;
      if(timer.type==='interval')timer.at+=timer.delay;else timer.fired=true;
      timer.fn();await Promise.resolve();
    }
    now=target;
  }
  return { hooks: context.window.testHooks, elements, requests, timers, events, context,advance };
}
const posts = h => h.requests.filter(request => request.options.method === 'POST');
const event = { preventDefault() {} };
let count = 0;
async function check(name, run) { await run(); count++; console.log(`PASS ${name}`); }
(async () => {
  await check('admin reads and uncertain writes time out across headers and body without automatic replay',async()=>{
    for(const body of [false,true]){
      const pending=deferred(),h=setup(()=>body?{...response({}),json:()=>pending.promise}:pending.promise);
      h.elements.get('global-daily-limit').value='100';h.elements.get('feed-article-limit').value='10';
      const write=h.hooks.saveLimits(event);for(let i=0;i<10;i++)await Promise.resolve();
      await h.advance(30000);await write;
      assert.equal(h.requests[0].options.signal.aborted,true);assert.equal(h.hooks.state.writing,false);
      assert.equal(h.hooks.state.requests.size,0);assert.equal(posts(h).length,1);
      assert(h.elements.get('admin-feedback').textContent.includes('before submitting another change'));
      pending.resolve(body?{quotas:quotas()}:response({quotas:quotas()}));await Promise.resolve();
      assert.equal(h.requests.length,1);assert(!h.timers.some(timer=>!timer.cancelled&&!timer.fired));
    }
    const pending=deferred(),h=setup(()=>pending.promise),read=h.hooks.refreshUsage();
    await h.advance(30000);assert.equal(await read,false);assert.equal(h.hooks.state.reading,false);
  });
  await check('admin BFCache restore preserves limit drafts, aborts stale reads and resumes the absolute cooldown',async()=>{
    for(const elapsed of [2000,6000]){
      let mode='limited';const pending=deferred(),h=setup(()=>mode==='limited'?response({error:{message:'Wait'}},429,{'Retry-After':'5'}):mode==='pending'?pending.promise:response(report()));
      h.elements.get('global-daily-limit').value='250';h.elements.get('feed-article-limit').value='7';
      await h.hooks.saveLimits(event);mode='pending';const read=h.hooks.refreshUsage();
      h.events.pagehide({persisted:true});assert.equal(await read,false);assert.equal(h.requests.at(-1).options.signal.aborted,true);
      await h.advance(elapsed);mode='ready';h.events.pageshow({persisted:true});
      for(let i=0;i<20;i++)await Promise.resolve();
      assert.equal(h.elements.get('global-daily-limit').value,'250');assert.equal(h.elements.get('feed-article-limit').value,'7');
      assert.equal(posts(h).length,1);assert.equal(h.hooks.state.reading,false);
      assert.equal(h.elements.get('arm-quota-reset').disabled,elapsed<5000);
      if(elapsed<5000){await h.advance(2999);assert(h.elements.get('arm-quota-reset').disabled);await h.advance(1);assert.equal(h.elements.get('arm-quota-reset').disabled,false);}
      pending.resolve(response(report('today',999)));await Promise.resolve();assert.equal(h.hooks.state.quotas.global.limit,100);
    }
  });
  await check('bucket sliders are absent while accessible chart data tables remain',async()=>{
    assert(!template.includes('Inspect time bucket'));assert(!template.includes('type="range"'));
    for(const stage of ['qwen','flux']){assert(!template.includes(`${stage}-chart-position`));assert(!template.includes(`${stage}-chart-inspection`));assert(template.includes(`${stage}-bucket-rows`));}
    assert(!script.includes('chart-position'));assert(!script.includes('chart-inspection'));assert(!script.includes('slider.oninput'));
    const h=setup();h.hooks.renderUsage(timelineReport());assert.equal(h.elements.get('qwen-bucket-rows').children.length,24);assert.equal(h.elements.get('flux-bucket-rows').children.length,24);
  });
  await check('valid matched SSR report avoids a duplicate initial read; explicit refresh still reads', async () => {
    const h = setup(undefined, {period: 'all', usage: report('all')}); await h.hooks.start();
    assert.equal(h.requests.length, 0); assert.equal(h.hooks.state.report.window, 'all');
    await h.hooks.refreshUsage(); assert.equal(h.requests.length, 1);
  });
  await check('invalid or mismatched SSR report falls back to a read of the selected period', async () => {
    for (const usage of [{}, report('all')]) {
      const h = setup(undefined, {period: 'today', usage}); await h.hooks.start();
      assert.equal(h.requests.length, 1); assert.equal(h.requests[0].url, '/api/admin/usage?window=today');
    }
  });
  await check('startup and report refresh are GET-only; unknown and inapplicable metrics stay distinct', async () => {
    const h = setup(); await h.hooks.start();
    assert.equal(posts(h).length, 0); assert.equal(h.requests[0].url, '/api/admin/usage?window=today');
    const text = h.elements.get('usage-rows').textContent;
    for (const value of ['Synthetic demo', 'Unavailable', 'Known subtotal']) assert(text.includes(value), value);
    assert(!text.includes('calls reported'));
    assert.equal(h.elements.get('quota-remaining').textContent, '94');
  });
  await check('usage cells omit coverage ratios while preserving complete zero, partial subtotal and unavailable distinctions', async () => {
    const data=report(),qwen=data.models[0];qwen.input_tokens=metric(35,35,0);qwen.output_tokens=metric(0,1,1);qwen.total_tokens=metric(null,0,2);qwen.inference_retries=metric(0,2,0);
    const h=setup();h.hooks.renderUsage(data);const rows=h.elements.get('usage-rows'),cells=h.elements.get('qwen-usage-rows').children[0].children;
    assert.equal(cells[2].textContent,'35');assert.equal(cells[2].children.length,1);
    assert.equal(cells[3].textContent,'0 Known subtotal');assert.equal(cells[3].children[1].className,'partial-coverage');
    assert.equal(cells[4].textContent,'Unavailable');assert.equal(cells[4].children.length,1);
    const flux=h.elements.get('flux-usage-rows').children[0];assert.equal(flux.children.length,5);assert(!flux.textContent.includes('N/A'));
    assert.equal(cells[6].textContent,'0');
    assert(!rows.textContent.includes('calls reported'));assert(!/\d+ \/ \d+/.test(rows.textContent));
  });
  await check('split reports default to live and preserve explicit synthetic/unknown separation without another request',async()=>{
    const data=report();data.models=[{...model(),execution_mode:'synthetic',model_id:'synthetic-only'}, {...model(),execution_mode:'live',model_id:'live-only'}, {...model('flux'),execution_mode:'live'}, {...model(),execution_mode:'unknown',model_id:null}];
    const h=setup();h.hooks.renderUsage(data);assert.equal(h.hooks.state.mode,'live');assert(h.elements.get('qwen-usage-rows').textContent.includes('live-only'));assert(!h.elements.get('qwen-usage-rows').textContent.includes('synthetic-only'));assert.equal(h.elements.get('flux-usage-rows').children[0].children.length,5);
    h.elements.get('usage-mode').value='synthetic';h.elements.get('usage-mode').listeners.change();assert(h.elements.get('qwen-usage-rows').textContent.includes('synthetic-only'));assert(!h.elements.get('qwen-usage-rows').textContent.includes('live-only'));
    assert(h.elements.get('flux-usage-rows').textContent.includes('No FLUX calls'));h.elements.get('usage-mode').value='unknown';h.elements.get('usage-mode').listeners.change();assert(h.elements.get('qwen-usage-rows').textContent.includes('Unverified model'));
    h.hooks.renderUsage(data);assert.equal(h.hooks.state.mode,'unknown');assert.equal(h.requests.length,0);
  });
  await check('mode defaults prefer synthetic then unknown when live measurements are absent',async()=>{
    for(const mode of ['synthetic','unknown']){const data=report();data.models=data.models.map(model=>({...model,execution_mode:mode}));const h=setup();h.hooks.renderUsage(data);assert.equal(h.hooks.state.mode,mode);assert.equal(h.elements.get('usage-mode').value,mode);}
  });
  await check('hourly charts overlay one shared scale, break missing measurements and keep future hours as gaps',async()=>{
    const data=timelineReport();const first={...model(),input_tokens:metric(100),output_tokens:metric(20)},gap={...model(),input_tokens:metric(null,0,2),output_tokens:metric(null,0,2)},partial={...model(),input_tokens:metric(0,1,1),output_tokens:metric(10)};
    data.timeline.buckets[0].models=[first];data.timeline.buckets[1].models=[gap];data.timeline.buckets[2].models=[partial];data.timeline.buckets[3].models=[{...first,input_tokens:metric(900)}];
    const h=setup();h.hooks.renderUsage(data);const svg=h.elements.get('qwen-usage-chart').children[0],paths=svg.children.filter(node=>node.tagName==='PATH'),primary=paths.find(node=>node.attributes.class.includes('series-0'));
    assert.equal(paths.length,2);assert.equal((primary.attributes.d.match(/M/g)||[]).length,2);assert(!primary.attributes.d.includes('NaN'));assert(!primary.attributes.d.includes('Infinity'));
    const dots=svg.children.filter(node=>node.tagName==='CIRCLE');assert.equal(dots.length,4);assert(dots.some(node=>node.attributes.class.includes('partial')));
    const outputFirst=dots.find(node=>node.attributes.class==='usage-chart-dot series-1');assert.equal(Number(outputFirst.attributes.cy),140); // 20/100 on the same140px scale.
    const rows=h.elements.get('qwen-bucket-rows').children;assert(rows[1].textContent.includes('Unavailable'));assert(rows[2].textContent.includes('0 · Known subtotal'));assert(rows[3].textContent.includes('Future-dated measurement'));assert(rows[3].textContent.includes('900'));assert(rows[4].textContent.includes('Not yet elapsed'));
    assert.equal(h.requests.length,0);
  });
  await check('elapsed empty buckets represent zero calls while pending rows with missing measurements remain gaps',async()=>{
    const data=timelineReport();data.timeline.buckets[1].models=[{...model(),pending:2,succeeded:0,failed:0,input_tokens:metric(null,0,2),output_tokens:metric(null,0,2)}];
    const h=setup();h.hooks.renderUsage(data);const rows=h.elements.get('qwen-bucket-rows').children;assert.equal(rows[0].children[1].textContent,'0');assert.equal(rows[1].children[1].textContent,'Unavailable');assert.equal(rows[23].children[1].textContent,'Not yet elapsed');
  });
  await check('mobile tick labels fit a reserved gutter while exact data-table values remain unchanged',async()=>{
    for(const total of [1301,1999000,1e308]){
      const data=timelineReport();data.timeline.buckets[0].models=[{...model(),input_tokens:metric(total),output_tokens:metric(0)}];const h=setup();h.hooks.renderUsage(data);
      const svg=h.elements.get('qwen-usage-chart').children[0],ticks=svg.children.filter(node=>node.tagName==='TEXT'&&node.attributes['text-anchor']==='end');
      assert.equal(ticks.length,3);for(const tick of ticks){assert.equal(tick.attributes.x,'76');assert(tick.textContent.length<=7);assert(Number(tick.attributes.x)-tick.textContent.length*17*.6>=0);}
      const grid=svg.children.find(node=>node.tagName==='LINE');assert.equal(grid.attributes.x1,'84');
      const path=svg.children.find(node=>node.tagName==='PATH');assert(path.attributes.d.startsWith('M84.00,'));
      assert(h.elements.get('qwen-bucket-rows').children[0].textContent.includes(total.toLocaleString()));
      if(total===1301)assert.equal(ticks[1].textContent,'650.5');
    }
  });
  await check('FLUX charts plot images and attempts only without multiplying either metric',async()=>{
    const data=timelineReport();data.timeline.buckets[0].models=[{...model('flux'),image_count:metric(2),inference_attempts:metric(5)}];const h=setup();h.hooks.renderUsage(data);
    const row=h.elements.get('flux-bucket-rows').children[0];assert.equal(row.children[1].textContent,'2');assert.equal(row.children[2].textContent,'5');const svg=h.elements.get('flux-usage-chart').children[0];assert(svg.attributes['aria-label'].includes('Images and Attempts'));assert(!svg.attributes['aria-label'].includes('token'));
  });
  await check('all-time multiday charts accept a short final bucket and explain undated totals',async()=>{
    const data=report('all'),start=Date.UTC(2024,0,1),end=start+801*86400000,width=7*86400000;data.generated_at=new Date(end).toISOString();data.timeline={start:new Date(start).toISOString(),end:new Date(end).toISOString(),bucket_seconds:7*86400,unbucketed_invocations:2,buckets:Array.from({length:115},(_,index)=>({start:new Date(start+index*width).toISOString(),end:new Date(Math.min(end,start+(index+1)*width)).toISOString(),models:[]}))};
    data.timeline.buckets[0].models=[model()];const h=setup();assert.equal(h.hooks.validTimeline(data),true);h.hooks.renderUsage(data);assert.equal(h.elements.get('usage-undated-note').hidden,false);assert(h.elements.get('qwen-chart-note').textContent.includes('7-day buckets'));assert.equal(h.elements.get('qwen-bucket-rows').children.length,115);
  });
  await check('missing or malformed timelines leave honest chart placeholders while preserving aggregate totals',async()=>{
    const valid=timelineReport();for(const mutate of [data=>delete data.timeline,data=>{data.timeline.buckets[0].end='bad';},data=>{data.timeline.buckets[0].models[0]={...model(),input_tokens:metric(Infinity)};},data=>{data.timeline.buckets=Array(121).fill(data.timeline.buckets[0]);},data=>{data.timeline.buckets[1].start=data.timeline.buckets[0].start;}]){
      const data=JSON.parse(JSON.stringify(valid));mutate(data);const h=setup();h.hooks.renderUsage(data);assert(h.elements.get('qwen-usage-chart').textContent.includes('Timeline unavailable'));assert(h.elements.get('qwen-usage-rows').textContent.includes('Qwen3'));assert.equal(h.requests.length,0);
    }
  });
  await check('empty modes and missing metrics never fabricate a synthetic/live merged graph',async()=>{
    const data=timelineReport();data.models=[];const h=setup();h.hooks.renderUsage(data);assert.equal(h.hooks.state.mode,'unknown');assert(h.elements.get('qwen-usage-chart').textContent.includes('No calls recorded'));
    const partial=h.hooks.bucketMetric([{...model(),input_tokens:metric(0,1,1)},{...model(),input_tokens:metric(null,0,2)}],'input_tokens');assert.equal(partial.total,0);assert.equal(partial.partial,true);
  });
  await check('hostile model labels remain plain text and expiry clears both tables and graphs before stale reads settle',async()=>{
    const pending=deferred(),data=timelineReport();data.models[0].model_id='<img src=x onerror=alert(1)>';const h=setup(()=>pending.promise);h.hooks.renderUsage(data);assert(h.elements.get('qwen-usage-rows').textContent.includes('<img src=x'));
    const read=h.hooks.refreshUsage();h.context.fetch=async()=>response({error:{code:'FORBIDDEN'}},401);await h.hooks.api('/api/session').catch(()=>{});pending.resolve(response(data));await read;
    for(const stage of ['qwen','flux'])for(const suffix of ['usage-rows','usage-chart','bucket-rows'])assert.equal(h.elements.get(`${stage}-${suffix}`).children.length,0);assert.equal(h.hooks.state.expired,true);
  });
  await check('current-hour boundary is open and old all-time ranges never claim a current bucket',async()=>{
    const data=timelineReport();data.generated_at=data.timeline.buckets[2].start;const h=setup();h.hooks.renderUsage(data);const rows=h.elements.get('qwen-bucket-rows').children;
    assert(rows[2].textContent.includes('In progress'));assert(!rows[2].textContent.includes('Not yet elapsed'));assert(rows[3].textContent.includes('Not yet elapsed'));
    data.window='all';data.timeline.bucket_seconds=86400;data.timeline.start='2024-01-01T00:00:00.000Z';data.timeline.end='2024-01-02T00:00:00.000Z';data.timeline.buckets=[{start:data.timeline.start,end:data.timeline.end,models:[model()]}];h.hooks.renderUsage(data);assert(!h.elements.get('qwen-chart-note').textContent.includes('Current bucket'));
  });
  await check('period mismatch cannot replace a valid graph and a mode edit during a read remains selected',async()=>{
    const pending=deferred(),data=timelineReport(),h=setup(()=>pending.promise);h.hooks.renderUsage(data);const old=h.elements.get('qwen-usage-chart').children[0];const read=h.hooks.refreshUsage();
    h.elements.get('usage-mode').value='unknown';h.elements.get('usage-mode').listeners.change();const changed=h.elements.get('qwen-usage-chart').children[0];assert.notEqual(old,changed);
    pending.resolve(response({...data,window:'all'}));await read;assert.equal(h.hooks.state.report.window,'today');assert.equal(h.hooks.state.mode,'unknown');assert.equal(h.elements.get('qwen-usage-chart').children[0],changed);assert(h.elements.get('admin-feedback').textContent.includes('period did not match'));
  });
  await check('SSR separates model columns and chooses the same initial mode as client hydration',async()=>{
    const code=`import json,sys
from html.parser import HTMLParser
from jinja2 import Environment,FileSystemLoader,StrictUndefined
from pulsefeed.db import DEFAULT_SETTINGS
report=json.load(sys.stdin)
env=Environment(loader=FileSystemLoader('app/pulsefeed/templates'),undefined=StrictUndefined,autoescape=True)
env.globals.update(asset_url=lambda name:'/static/'+name,utc_timestamp=lambda value:'00:00 UTC')
html=env.get_template('admin.html').render(account={'id':'admin','role':'admin'},csrf='test',fake_mode=False,settings=DEFAULT_SETTINGS,quotas=report['quotas'],usage=report,period=report['window'])
class Parser(HTMLParser):
 def __init__(self):super().__init__();self.ids=[];self.body=None;self.text={'qwen':'','flux':''};self.mode=None;self.inmode=False
 def handle_starttag(self,tag,attrs):
  attrs=dict(attrs)
  if attrs.get('id'):self.ids.append(attrs['id'])
  if tag=='tbody' and attrs.get('id') in ('qwen-usage-rows','flux-usage-rows'):self.body=attrs['id'].split('-')[0]
  if tag=='select':self.inmode=attrs.get('id')=='usage-mode'
  if tag=='option' and self.inmode and 'selected' in attrs:self.mode=attrs['value']
 def handle_endtag(self,tag):
  if tag=='tbody':self.body=None
  if tag=='select':self.inmode=False
 def handle_data(self,text):
  if self.body:self.text[self.body]+=text
p=Parser();p.feed(html)
assert len(p.ids)==len(set(p.ids))
assert 'Input tokens' not in html.split('flux-usage-table',1)[1].split('</table>',1)[0]
assert 'Output tokens' not in html.split('flux-usage-table',1)[1].split('</table>',1)[0]
assert 'calls reported' not in html
assert html.index('id="overview-heading"')<html.index('id="prompt-settings-heading"')<html.index('id="limits-section"')<html.index('id="model-usage-section"')
print(json.dumps({'mode':p.mode,'text':p.text}))`;
    const {execFileSync}=require('node:child_process');
    for(const modes of [['synthetic','live','unknown'],['synthetic'],['unknown']]){
      const data=report();data.models=modes.flatMap(mode=>[{...model(),execution_mode:mode,model_id:`${mode}-qwen`},{...model('flux'),execution_mode:mode,model_id:`${mode}-flux`}]);const h=setup();h.hooks.renderUsage(data);
      const rendered=JSON.parse(execFileSync(process.env.PULSEFEED_PYTHON || path.join(root,'app/.venv/bin/python'),['-c',code],{cwd:root,input:JSON.stringify(data),encoding:'utf8',env:{...process.env,PYTHONPATH:path.join(root,'app')}}));
      assert.equal(rendered.mode,h.hooks.state.mode);for(const stage of ['qwen','flux']){assert(rendered.text[stage].includes(`${rendered.mode}-${stage}`));for(const other of modes.filter(mode=>mode!==rendered.mode))assert(!rendered.text[stage].includes(`${other}-${stage}`));}
    }
  });
  await check('hourly starts stay a read-only summary independent of daily mutation fields', async () => {
    const h = setup(); h.hooks.renderQuotas(quotas());
    assert.equal(h.elements.get('hourly-used').textContent, '4'); assert.equal(h.elements.get('hourly-remaining').textContent, '26');
    assert.equal(h.elements.get('hourly-limit').textContent, '30'); assert(h.elements.get('hourly-next-slot').textContent.includes('UTC'));
    const unknown = quotas(); delete unknown.hourly; h.hooks.renderQuotas(unknown);
    assert.equal(h.elements.get('hourly-used').textContent, 'Unavailable'); assert.equal(posts(h).length, 0);
  });
  await check('save validates independent bounded daily attempt and page article integers before POST', async () => {
    const h = setup();
    for (const [global, feed] of [[0, 1], [2.5, 1], [10, 11], [10001, 10], [100, 0], [100, 2.5]]) {
      h.elements.get('global-daily-limit').value = global; h.elements.get('feed-article-limit').value = feed;
      await h.hooks.saveLimits(event);
    }
    assert.equal(posts(h).length, 0); assert(h.elements.get('admin-feedback').textContent.includes('whole-number'));
  });
  await check('save sends only two limit fields with same-origin CSRF and refreshes the confirmed report', async () => {
    const h = setup(request => request.options.method === 'POST' ? response({ quotas: quotas(120) }) : response(report('today', 120)));
    h.elements.get('global-daily-limit').value = '120'; h.elements.get('feed-article-limit').value = '10';
    await h.hooks.saveLimits(event);
    const request = posts(h)[0]; assert.equal(request.url, '/api/admin/limits');
    assert.deepEqual(request.body, { global_daily_limit: 120, feed_article_limit: 10 });
    assert.equal(request.options.headers['X-CSRF-Token'], 'test-csrf'); assert.equal(request.options.credentials, 'same-origin');
    assert.equal(h.hooks.state.quotas.global.limit, 120); assert.equal(posts(h).length, 1);
  });
  await check('page cap is independent of global attempts and summary contains no shared-feed daily bucket', async () => {
    const h=setup(request=>request.options.method==='POST'?response({quotas:quotas(5,10)}):response(report('today',5,10)));
    h.elements.get('global-daily-limit').value='5';h.elements.get('feed-article-limit').value='10';await h.hooks.saveLimits(event);
    assert.equal(posts(h).length,1);assert.deepEqual(posts(h)[0].body,{global_daily_limit:5,feed_article_limit:10});assert.equal(h.elements.get('quota-page-summary').textContent,'Up to 10 illustrated stories per new page');
    assert.equal(h.elements.has('quota-feed-summary'),false);assert.equal(h.hooks.state.quotas.per_feed,undefined);
    h.hooks.renderQuotas(quotas(5,1));assert.equal(h.elements.get('quota-page-summary').textContent,'Up to 1 illustrated story per new page');
  });
  await check('incomplete or out-of-range page quota responses never claim a successful save',async()=>{
    for(const page of [undefined,{article_limit:0,max_articles:10},{article_limit:11,max_articles:10},{article_limit:5,max_articles:100}]){
      const bad=quotas();bad.page=page;const h=setup(()=>response({quotas:bad}));h.elements.get('global-daily-limit').value='100';h.elements.get('feed-article-limit').value='5';await h.hooks.saveLimits(event);
      assert.equal(posts(h).length,1);assert(!h.elements.get('admin-feedback').textContent.includes('Limits saved.'));assert(h.elements.get('admin-feedback').textContent.includes('incomplete'));
    }
  });
  await check('reset requires explicit confirmation; opening/canceling the prompt never posts', async () => {
    const h = setup(); await h.hooks.confirmReset(); h.hooks.armReset(); h.hooks.cancelReset(); await h.hooks.confirmReset();
    assert.equal(posts(h).length, 0); assert.equal(h.hooks.state.armed, false);
    assert.equal(h.elements.get('quota-reset-confirmation').hidden,true);assert.equal(h.elements.get('arm-quota-reset').hidden,false);assert.equal(h.elements.get('arm-quota-reset').focused,true);
  });
  await check('native invalid capture reveals limits without replacing an unsaved draft', async () => {
    const h=setup();h.elements.get('limits-section').open=false;h.elements.get('global-daily-limit').value='';
    h.elements.get('admin-limits-form').listeners.invalid();
    assert.equal(h.elements.get('limits-section').open,true);assert.equal(h.elements.get('global-daily-limit').value,'');assert.equal(posts(h).length,0);
  });
  await check('confirmed reset clears daily and hourly display once while preserving usage history', async () => {
    const cleared=quotas();Object.assign(cleared.global,{used:0,reserved:0,uncertain_charged:0,charged:0,remaining:100});Object.assign(cleared.hourly,{used:0,remaining:30,next_slot_at:null});
    const refreshed=report();refreshed.quotas=cleared;
    const pending = deferred(); const h = setup(request => request.options.method === 'POST' ? pending.promise : response(refreshed));
    const exhausted=quotas();Object.assign(exhausted.hourly,{used:30,remaining:0});h.hooks.renderQuotas(exhausted);assert.equal(h.elements.get('hourly-used').textContent,'30');
    h.hooks.armReset(); const first = h.hooks.confirmReset(); await h.hooks.confirmReset();
    assert.equal(posts(h).length, 1); assert.deepEqual(posts(h)[0].body, { confirm: true });
    assert.equal(posts(h)[0].url, '/api/admin/limits/reset'); pending.resolve(response({ quotas: cleared })); await first;
    assert.equal(h.hooks.state.armed, false); assert(h.elements.get('admin-feedback').textContent.includes('history is retained'));
    assert(h.elements.get('usage-rows').textContent.includes('Qwen3'));
    assert.equal(h.elements.get('hourly-used').textContent,'0');assert.equal(h.elements.get('hourly-remaining').textContent,'30');assert.equal(h.elements.get('quota-used').textContent,'0');
    assert(h.elements.get('admin-feedback').textContent.includes('Daily and hourly allowances were reset'));
  });
  await check('an incomplete write response does not announce a successful change or retry automatically', async () => {
    const h = setup(() => response({})); h.hooks.armReset(); await h.hooks.confirmReset();
    assert.equal(posts(h).length, 1); assert(!h.elements.get('admin-feedback').textContent.includes('allowances were reset'));
    assert(h.elements.get('admin-feedback').textContent.includes('incomplete')); assert.equal(h.hooks.state.quotas.global.charged, 6);
  });
  await check('connection failure and invalid JSON require a read refresh rather than automatic mutation replay', async () => {
    for (const broken of [() => { throw new Error('offline'); }, () => ({ ok: true, status: 200, json: async () => { throw new Error('truncated'); } })]) {
      const h = setup(broken); h.hooks.armReset(); await h.hooks.confirmReset();
      assert.equal(posts(h).length, 1); assert(h.elements.get('admin-feedback').textContent.includes('Refresh usage'));
    }
  });
  await check('401 or lost admin authority clears the table and hides the protected dashboard', async () => {
    for (const code of [401, 403]) {
      const h = setup(() => response({ error: { code: 'FORBIDDEN' } }, code)); h.hooks.renderUsage(report()); await h.hooks.refreshUsage();
      assert.equal(h.hooks.state.expired, true); assert.equal(h.elements.get('admin-dashboard').hidden, true);
      assert.equal(h.elements.get('usage-rows').children.length, 0);
    }
  });
  await check('429 honors Retry-After and prevents further limit/reset mutations during cooldown', async () => {
    const h = setup(() => response({ error: { message: 'Slow down' } }, 429, { 'Retry-After': '9' }));
    h.hooks.armReset(); await h.hooks.confirmReset(); const before = posts(h).length;
    await h.hooks.confirmReset(); assert.equal(posts(h).length, before); assert(h.hooks.state.cooldownUntil > Date.now());
    assert(h.elements.get('admin-feedback').textContent.includes('9 seconds'));
  });
  await check('a stale usage read cannot overwrite a later period response', async () => {
    const old = deferred(); let requests = 0;
    const h = setup(() => ++requests === 1 ? old.promise : response(report('all', 120)));
    const first = h.hooks.refreshUsage(); h.hooks.state.window = 'all'; await h.hooks.refreshUsage();
    old.resolve(response(report('today', 100))); await first;
    assert.equal(h.hooks.state.report.window, 'all'); assert.equal(h.hooks.state.quotas.global.limit, 120); assert.equal(posts(h).length, 0);
  });
  await check('usage refresh preserves unsaved limits while updating the allowance meter', async () => {
    const pending = deferred(); const h = setup(() => pending.promise); const startup = h.hooks.start();
    for (const [id, value] of [['global-daily-limit', '120'], ['feed-article-limit', '7']]) {
      const input = h.elements.get(id); input.value = value; input.listeners.input?.();
    }
    pending.resolve(response(report('today', 100))); await startup;
    assert.equal(h.elements.get('global-daily-limit').value, '120');
    assert.equal(h.elements.get('feed-article-limit').value, '7');
    assert.equal(h.elements.get('quota-limit').textContent, '100');
    assert.equal(posts(h).length, 0);
  });
  await check('confirmed saves preserve newer edits made while the mutation was pending', async () => {
    const pending = deferred(); const h = setup(request => request.options.method === 'POST' ? pending.promise : response(report('today', 120, 7)));
    await h.hooks.start();
    const global = h.elements.get('global-daily-limit'); global.value = '120'; global.listeners.input?.();
    const articles = h.elements.get('feed-article-limit'); articles.value = '7'; articles.listeners.input?.();
    const saving = h.hooks.saveLimits(event);
    global.value = '150'; global.listeners.input?.();articles.value='9';articles.listeners.input?.();
    pending.resolve(response({ quotas: quotas(120,7) })); await saving;
    assert.equal(posts(h).length, 1); assert.equal(posts(h)[0].body.global_daily_limit, 120);
    assert.equal(global.value, '150'); assert.equal(h.elements.get('quota-limit').textContent, '120');
    assert.equal(h.elements.get('feed-article-limit').value, '9');
    assert.equal(posts(h)[0].body.feed_article_limit,7);assert(h.elements.get('quota-page-summary').textContent.includes('7 illustrated stories'));
  });
  await check('confirmed resets preserve unsaved limit drafts', async () => {
    const h = setup(request => request.options.method === 'POST' ? response({ quotas: quotas() }) : response(report()));
    await h.hooks.start();
    const global = h.elements.get('global-daily-limit'); global.value = '150'; global.listeners.input?.();
    const articles=h.elements.get('feed-article-limit');articles.value='6';articles.listeners.input?.();
    h.hooks.armReset(); await h.hooks.confirmReset();
    assert.equal(posts(h).length, 1); assert.equal(global.value, '150');assert.equal(articles.value,'6');assert.equal(h.hooks.state.quotas.page.article_limit,10);
    assert.equal(h.elements.get('quota-limit').textContent, '100');
  });
  console.log(JSON.stringify({ result: 'PASS', tests: count }));
})().catch(error => { console.error(error); process.exitCode = 1; });
