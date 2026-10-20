/* Focused real-script checks for polling admission and retained image ownership. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
class Element {
  constructor() { this.children = []; this.dataset = {}; this.listeners = {}; this.hidden = false; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  setAttribute() {} removeAttribute() {} close() {}
}
const response = (payload, status = 200, headers = {}) => ({ok: status >= 200 && status < 300, status, headers: new Map(Object.entries(headers)), json: async () => payload});
function setup(fetcher = () => response({id: 'batch', state: 'active', runs: []})) {
  const elements = new Map();
  const document = {hidden: false, events: {}, body: new Element(),
    addEventListener(type, callback) { this.events[type] = callback; },
    getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    createElement: () => {const element=new Element();element.decode=async()=>{};return element;}, querySelectorAll: () => []};
  document.getElementById('bootstrap').textContent = JSON.stringify({account: {id: 'a'}, csrf: 'csrf', view: {id: 'view-a', url: '/feeds/view-a', created_at: 1000, expires_at: 4600}});
  let now = 1000000, serial = 0;
  const timers = new Map(), requests = [], revoked = [], events = {};
  class Clock extends Date { static now() { return now; } }
  class URLs extends URL { static createObjectURL() { return `blob:test-${++serial}`; } static revokeObjectURL(url) { revoked.push(url); } }
  const context = {document, AbortController, Blob, URL: URLs, Date: Clock, console,
    window: {pulseImages: {live: view => view && now < view.expires_at * 1000, read: async () => null, save: async () => {}, checkExpired() {}, maintain: async () => {}}, navigator: {onLine: true}, addEventListener(type, callback) { events[type] = callback; }},
    setTimeout(callback, delay) { const id = ++serial; timers.set(id, {callback, delay}); return id; }, clearTimeout(id) { timers.delete(id); },
    setInterval() { return 1; }, clearInterval() {},
    fetch: async (url, options) => { requests.push({url, options}); return fetcher(url, options); }};
  const source = fs.readFileSync(path.join(__dirname, '../app/pulsefeed/static/app.js'), 'utf8')
    .replace(/^  start\(\);$/m, '  window.hooks = {state, schedulePoll, pollBatch, acceptBatch, acceptRuns, cacheImage, pruneSavedImages, recoverImage, detailsFor};');
  vm.createContext(context); vm.runInContext(source, context);
  const hooks = context.window.hooks; hooks.state.account = {id: 'a'}; hooks.state.batchId = 'batch';
  return {hooks, context, document, requests, timers, revoked, events, advance(ms) { now += ms; }};
}
const latestTimer = h => [...h.timers.values()].filter(timer => timer.callback.name === "pollBatch").at(-1);
const pollTimers = h => [...h.timers.values()].filter(timer => timer.callback.name === "pollBatch");
let count = 0;
async function check(name, run) { await run(); count++; console.log(`PASS ${name}`); }
(async () => {
  await check('polling honors Retry-After seconds and HTTP dates, including online/manual wakeups', async () => {
    for (const header of ['45', new Date(1045000).toUTCString()]) {
      const h = setup(() => response({error: {message: 'Wait'}}, 429, {'Retry-After': header}));
      await h.hooks.pollBatch(); assert.equal(h.requests.length, 1); assert.equal(latestTimer(h).delay, 45000);
      h.events.online(); assert.equal(latestTimer(h).delay, 45000);
      await h.hooks.pollBatch(); assert.equal(h.requests.length, 1);
      h.advance(45000); await h.hooks.pollBatch(); assert.equal(h.requests.length, 2);
      assert(h.requests.every(request => request.options.method === 'GET'));
    }
  });
  await check('hidden/offline tabs pause timers and returning tabs make a read-only terminal check', async () => {
    const h = setup(() => response({id: 'batch', state: 'finished', runs: []}));
    h.hooks.schedulePoll(); h.document.hidden = true; h.document.events.visibilitychange(); assert.equal(h.timers.size, 0);
    await h.hooks.pollBatch(); assert.equal(h.requests.length, 0);
    h.document.hidden = false; h.context.window.navigator.onLine = false; h.document.events.visibilitychange(); assert.equal(pollTimers(h).length, 0);
    h.context.window.navigator.onLine = true; h.events.online(); assert.equal(latestTimer(h).delay, 100);
    await latestTimer(h).callback(); assert.equal(h.requests.length, 1); assert.equal(h.hooks.state.batchId, null);
    h.document.events.visibilitychange(); assert.equal(pollTimers(h).length, 0);
  });
  await check('connection failures back off to 60 seconds; success restores normal cadence', async () => {
    let failed = true;
    const h = setup(() => { if (failed) throw Error('offline'); return response({id: 'batch', state: 'active', runs: []}); });
    for (const delay of [8000, 16000, 32000, 60000, 60000]) { await h.hooks.pollBatch(); assert.equal(latestTimer(h).delay, delay); }
    failed = false; await h.hooks.pollBatch(); assert.equal(latestTimer(h).delay, 2500); assert.equal(h.hooks.state.pollFailures, 0);
  });
  await check('overlapping wakeups do not overlap requests; in-flight terminal response is accepted while hidden', async () => {
    let finish; const pending = new Promise(resolve => { finish = resolve; }); const h = setup(() => pending);
    const first = h.hooks.pollBatch(); await h.hooks.pollBatch(); assert.equal(h.requests.length, 1);
    h.document.hidden = true; h.document.events.visibilitychange();
    finish(response({id: 'batch', state: 'finished', runs: []})); await first;
    assert.equal(h.hooks.state.batchId, null); assert.equal(h.timers.size, 0);
  });
  await check('only saved off-feed or superseded image URLs are revoked; visible and unsaved copies remain', async () => {
    const h = setup(); h.hooks.state.items = [{id: 'article'}];
    h.hooks.state.runs.set('old', {id: 'old', item_id: 'article', created: 1});
    h.hooks.state.runs.set('new', {id: 'new', item_id: 'article', created: 2});
    const blob = new Blob(['image'], {type: 'image/webp'});
    for (const [id, saved] of [['old', true], ['new', true], ['off-feed', true], ['unsaved', false]]) h.hooks.cacheImage(id, blob, saved);
    const oldURL = h.hooks.state.images.get('old').url, offURL = h.hooks.state.images.get('off-feed').url;
    h.hooks.pruneSavedImages(); assert.deepEqual([...h.hooks.state.images.keys()], ['new', 'unsaved']); assert.deepEqual(h.revoked, [oldURL, offURL]);
    h.hooks.state.items = []; h.hooks.pruneSavedImages(); assert.deepEqual([...h.hooks.state.images.keys()], ['unsaved']);
    h.hooks.state.images.get('unsaved').saved = true; h.hooks.pruneSavedImages(); assert.equal(h.hooks.state.images.size, 0);
  });
  await check('late local restoration after feed navigation releases its saved blob without inference', async () => {
    let complete;
    const h = setup(); h.context.window.pulseImages.read = () => new Promise(resolve => {complete = () => resolve(new Blob(['saved']));});
    const pending = h.hooks.recoverImage({id: 'old-feed-run', state: 'completed'});
    complete(); await pending; assert.equal(h.hooks.state.images.size, 0); assert.equal(h.revoked.length, 1); assert.equal(h.requests.length, 0);
  });
  await check('SSR admin metrics match hydrated values and expiry; bundled asset URLs share content hashes', async () => {
    const run = {id: 'run', qwen_prompt: '<prompt>', final_prompt: 'image', used_fallback: false, matched_terms: [], seed: 0, width: 1024, height: 1024, result_expires_at: 1791972000, expiry_estimated: true,
      invocations: [{stage: 'qwen', model: {id: 'verified/model', revision: null, precision: 'fp4', runtime: 'torch'}, usage: {input_tokens: 0, output_tokens: 12, total_tokens: 12}, timings: {inference_ms: 0}, delay_ms: null, execution_ms: 500, counters: {inference_attempts: 1, inference_retries: 0, dependency_retries: null}},
        {stage: 'flux', model: null, usage: {image_count: 0}, timings: null, counters: null, delay_ms: 0, execution_ms: null}]};
    const code = `import json,sys
from html.parser import HTMLParser
from pathlib import Path
from jinja2 import Environment,FileSystemLoader,StrictUndefined
from pulsefeed.assets import asset_url,utc_timestamp,static_cache_control
env=Environment(loader=FileSystemLoader('app/pulsefeed/templates'),undefined=StrictUndefined,autoescape=True)
env.globals['utc_timestamp']=utc_timestamp
html=env.get_template('run_details.html').render(run=json.load(sys.stdin))
class Parser(HTMLParser):
 def __init__(self): super().__init__();self.tag=None;self.values=[]
 def handle_starttag(self,tag,attrs): self.tag=tag if tag in ('dt','dd','h4','p') else None
 def handle_data(self,data):
  if self.tag:self.values.append([self.tag,data])
 def handle_endtag(self,tag):self.tag=None
p=Parser();p.feed(html)
for name in ('Silkscreen-Regular.ttf','pixel-garden.svg'):
 assert asset_url(name) in Path('app/pulsefeed/static/app.css').read_text()
for name in ('app.js','app.css','theme.js'):
 path,version=asset_url(name).split('?v=')
 assert 'immutable' in static_cache_control(path,version)
 assert 'must-revalidate' in static_cache_control(path,'bad')
assert static_cache_control('/static/../config.py',None)=='no-store'
assert static_cache_control('/api/session',None)=='no-store'
print(json.dumps(p.values))`;
    const root = path.resolve(__dirname, '..');
    const python = process.env.PULSEFEED_PYTHON || (fs.existsSync(path.join(root, 'app/.venv/bin/python')) ? path.join(root, 'app/.venv/bin/python') : 'python3');
    const server = JSON.parse(execFileSync(python, ['-c', code], {cwd: root, input: JSON.stringify(run), env: {...process.env, PYTHONPATH: path.join(root, 'app')}, encoding: 'utf8'}));
    const client = [];
    function visit(element) { const tag = element.tagName?.toLowerCase(); if (['dt', 'dd', 'h4', 'p'].includes(tag)) client.push([tag, element.textContent]); for (const child of element.children) visit(child); }
    const h = setup();
    // The small DOM double only needs tag names here; production uses actual browser elements.
    h.document.createElement = tag => {const element = new Element(); element.tagName = tag; return element;};
    visit(h.hooks.detailsFor(run)); assert.deepEqual(client, server);
  });
  console.log(JSON.stringify({result: 'PASS', tests: count}));
})().catch(error => {console.error(error); process.exitCode = 1;});
