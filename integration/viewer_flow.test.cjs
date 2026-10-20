/* Behavioral tests execute the real viewer callbacks with DOM/network doubles. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

const root = path.resolve(__dirname, '..');
const scriptPath = path.join(root, 'app/pulsefeed/static/app.js');
const templatePath = path.join(root, 'app/pulsefeed/templates/index.html');
const checked = [];
const failed = [];

class Element {
  constructor(tag = 'div', id = '') {
    this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.listeners = {};
    this.dataset = {}; this.attributes = {}; this.className = ''; this.hidden = false;
    this.disabled = false; this.value = ''; this.open = false; this.checked = false;
    this.isConnected = true; this._text = '';
    this.style = {values:{},setProperty(name,value){this.values[name]=value;}};
    this.classList = {contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => { const names = new Set(this.className.split(/\s+/).filter(Boolean));
        const enabled = force === undefined ? !names.has(name) : force;
        if (enabled) names.add(name); else names.delete(name); this.className = [...names].join(' '); return enabled; }};
  }
  set textContent(value) { this._text = String(value ?? ''); this.children = []; }
  get textContent() { return this._text + this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
  append(...children) { for (const child of children) { if (typeof child !== 'string') child.parentNode = this; this.children.push(child); } }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  removeAttribute(name) { delete this.attributes[name]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  scrollIntoView(options) { this.scrollOptions = options; }
  focus(options) { this.focused = true; this.focusOptions = options; }
  close() { this.open = false; }
  showModal() { this.open = true; }
  querySelectorAll(selector) { return descendants(this).filter(element => matches(element, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function descendants(element) {
  return element.children.flatMap(child => typeof child === 'string' ? [] : [child, ...descendants(child)]);
}
function matches(element, selector) {
  if (selector.includes(' ')) {
    const pieces = selector.split(/\s+/); const last = pieces.pop();
    if (!matches(element, last)) return false;
    let parent = element.parentNode;
    while (parent) { if (matches(parent, pieces.join(' '))) return true; parent = parent.parentNode; }
    return false;
  }
  const tag = selector.match(/^[a-z]+/i)?.[0];
  if (tag && element.tagName.toLowerCase() !== tag.toLowerCase()) return false;
  for (const [, name] of selector.matchAll(/\.([\w-]+)/g)) if (!element.classList.contains(name)) return false;
  for (const [, name, quote, value] of selector.matchAll(/\[([\w-]+)(?:=(["']?)([^\]"']*)\2)?\]/g)) {
    let actual;
    if (name.startsWith('data-')) actual = element.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())];
    else if (name === 'open') actual = element.open ? '' : undefined;
    else actual = element[name] ?? element.attributes[name];
    if (actual === undefined || (value !== undefined && String(actual) !== value)) return false;
  }
  return true;
}
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }
async function until(predicate, message) {
  for (let tick = 0; tick < 30 && !predicate(); tick++) await Promise.resolve();
  assert(predicate(), message);
}
function response(payload, status = 200, headers = {}) {
  return {ok: status >= 200 && status < 300, status,
    json: async () => payload, headers: new Map(Object.entries(headers)),
    blob: async () => new Blob(['synthetic-image'], {type: 'image/webp'})};
}
function plain(value) { return JSON.parse(JSON.stringify(value)); }
const feedA = {id: 'feed-a', title: 'Source A', url: 'https://example.org/a.xml'};
const feedB = {id: 'feed-b', title: 'Source B', url: 'https://example.org/b.xml'};
function item(id, title, published = '2026-10-14') {
  return {id, title, excerpt: `Original excerpt for ${title}`, link: `https://example.org/${id}`, published};
}
const itemsA = [item('a-one', 'First in source order', '2020-01-01'), item('a-two', 'Second in source order', '2030-01-01')];
const itemsB = [item('b-one', 'Only source B story')];
function run(id, story = itemsA[0], created = 1, state = 'queued') {
  const {title, excerpt, link, published} = story;
  return {id, item_id: story.id, created, state, original: {title, excerpt, link, published},
    qwen_prompt: `prompt-${id}`, final_prompt: `final-${id}`, invocations: [],
    error: null, availability: state === 'completed' ? 'unavailable' : null};
}
function feedData(feed = feedA, items = itemsA, runs = [], generation = null) {
  return {feed, items, runs, active_batch_id: null, ...(generation ? {generation} : {})};
}
function startedData(feed = feedA, items = itemsA) {
  const runs = [run('queued-one', items[0])];
  return {...feedData(feed, items, runs, {status: 'started', batch: {id: 'batch-a', state: 'active', runs}, error: null, retry_after: null}), active_batch_id: 'batch-a'};
}

const viewA = {id: 'view-a', url: '/feeds/view-a', created_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 3600};
function accepted(view = viewA) { return {...startedData(), view, server_now: Date.now() / 1000}; }
function setup(options = {}) {
  const template = fs.readFileSync(templatePath, 'utf8') + fs.readFileSync(path.join(root, 'app/pulsefeed/templates/fun_loader.html'), 'utf8');
  const elements = new Map();
  for (const match of template.matchAll(/<([\w-]+)\b[^>]*\bid="([\w-]+)"[^>]*>/g)) {
    const element = new Element(match[1], match[2]);
    element.disabled = /\bdisabled\b/.test(match[0]);
    if (/\bdata-initializing\b/.test(match[0])) element.attributes['data-initializing'] = '';
    if (/\bdata-mutation\b/.test(match[0])) element.dataset.mutation = '';
    elements.set(match[2], element);
  }
  if (!options.landing && !options.pending) { elements.delete('fun-loader'); elements.delete('fun-message'); }
  const view = options.landing ? {...viewA, ...options.view} : null;
  const boot = {account: {id: 'account-a', role: options.role || 'evaluator'}, csrf: 'test-csrf',
    fake_mode: true, admin_mode: options.adminMode || false, pending_feed: Boolean(options.pending), view, server_now: Date.now() / 1000,
    initial_feed: options.landing ? options.initialFeed || feedData() : null};
  elements.get('bootstrap').textContent = JSON.stringify(boot);
  const body = new Element('body'), documentEvents = {};
  const document = {body, title: '', hidden: false, addEventListener(type, callback) {(documentEvents[type] ||= []).push(callback);},
    getElementById: id => elements.get(id) || null, createElement: tag => {
      const element = new Element(tag);
      if (tag === "img") {
        if (options.decodeSupported === false) { element.complete = false; options.imageLoad?.(element); }
        else element.decode = () => options.decode ? options.decode(element) : Promise.resolve();
      }
      return element;
    },
    querySelectorAll(selector) { const all = new Set([...elements.values(), body]);
      for (const element of [...all]) for (const child of descendants(element)) all.add(child);
      return [...all].filter(element => matches(element, selector)); }};
  let now = Date.now();
  class Clock extends Date { static now() { return now; } }
  const requests = [], timers = [], windowEvents = {}, navigations = [], localReads = [], localWrites = [], watchers = [];
  const media = {matches: options.reducedMotion || false, addEventListener(type, fn) {this.change = fn;}};
  const location = {href: options.href || `http://localhost${view ? view.url : '/'}`, reload() {navigations.push('reload');},
    assign(url) {navigations.push(url);}, replace(url) {navigations.push(url);}};
  const store = {now: () => Date.now(), live: value => value && Date.now() < value.expires_at * 1000,
    open: async () => {}, sweep: async () => {}, maintain: async () => {},
    watch(value, callback) {watchers.push({value, callback});}, checkExpired() {for (const watcher of watchers) if (!this.live(watcher.value)) watcher.callback();},
    read: async (...args) => {localReads.push(args); return options.read ? options.read(...args) : null;},
    save: async (...args) => {localWrites.push(args); if (options.save) return options.save(...args);}};
  const sessionRecords=options.sessionRecords || new Map();
  const sessionStorage=options.sessionStorage || {getItem:key=>sessionRecords.get(key)??null,setItem:(key,value)=>sessionRecords.set(key,value),removeItem:key=>sessionRecords.delete(key)};
  const revoked = [];
  class ImageURLs extends URL { static revokeObjectURL(url) { revoked.push(url); super.revokeObjectURL(url); } }
  const context = {document, window: {sessionStorage, pulseImages: store, matchMedia: () => media, addEventListener(type, callback) {(windowEvents[type] ||= []).push(callback);}, location,
    history: {replaceState() {throw Error('Reader must not rewrite history');}}},
    console, AbortController, Blob, URL: ImageURLs, Set, Map, Date: Clock, JSON, Promise, Number, String, Array, Error, Math, crypto: crypto.webcrypto,
    setTimeout(callback, delay) {timers.push({callback, delay, at: now + delay, type: 'timeout'}); return timers.length;}, clearTimeout(id) {if (timers[id - 1]) timers[id - 1].cancelled = true;},
    setInterval(callback, delay) {timers.push({callback, delay, at: now + delay, type: 'interval'}); return timers.length;}, clearInterval(id) {if (timers[id - 1]) timers[id - 1].cancelled = true;},
    fetch: async (url, requestOptions = {}) => {const request = {url, options: requestOptions, body: requestOptions.body ? JSON.parse(requestOptions.body) : null};
      requests.push(request); return options.fetch ? options.fetch(request) : response({});}};
  const original = fs.readFileSync(scriptPath, 'utf8');
  const source = original.replace(/^  start\(\);$/m,
    '  window.testHooks = {state, start, api, showFeed, renderRuns, acceptRuns, acceptBatch, pollBatch, viewFeed, retryRun, recoverImage, expireView, updateControls, updateLoader, stopLoader, readerDate, readerExcerpt, safeSource, openPending, readPending, writePending, funLines};');
  vm.createContext(context); vm.runInContext(source, context, {filename: scriptPath});
  const hooks = context.window.testHooks;
  if (!options.unstarted) { hooks.state.account = boot.account; hooks.state.restoring = false; }
  async function dispatch(id, type, overrides = {}) {const element = elements.get(id); assert(element, `Missing DOM element ${id}`);
    const evt = Object.assign({target: element, prevented: false, preventDefault() {this.prevented = true;}}, overrides);
    for (const callback of element.listeners[type] || []) await callback(evt); return evt;}
  const submit = async url => {elements.get('feed-url').value = url; return dispatch('feed-form', 'submit');};
  async function advance(ms) {
    const target = now + ms;
    for (let count = 0; count < 1000; count++) {
      const timer = timers.filter(timer => !timer.cancelled && !timer.fired && timer.at <= target).sort((a,b) => a.at - b.at)[0];
      if (!timer) break;
      now = timer.at;
      if (timer.type === 'interval') timer.at += timer.delay;
      else timer.fired = true;
      timer.callback(); await Promise.resolve();
    }
    now = target;
  }
  return {hooks, context, document, elements, requests, timers, windowEvents, documentEvents, submit, dispatch, boot, navigations, store, localReads, localWrites, media, advance, sessionRecords, revoked};
}
const posts = harness => harness.requests.filter(request => request.options.method === 'POST');
const cards = harness => harness.elements.get('item-list').children.filter(child => child.tagName === 'ARTICLE');
const text = harness => harness.elements.get('item-list').textContent;
async function test(name, callback) {try {await callback(); checked.push(name); console.log(`PASS ${name}`);} catch(error) {failed.push(name); console.error(`FAIL ${name}\n${error.stack}`);}}

(async () => {
  await test('Request deadlines cover stalled headers and JSON without retaining their owners or timers', async () => {
    for (const body of [false, true]) {
      const pending=deferred(),h=setup({fetch:()=>body?{...response({}),json:()=>pending.promise}:pending.promise});
      const outcome=h.hooks.api('/api/session').then(()=>null,error=>error);
      await until(()=>h.requests.length===1,'request starts');
      if(body) await Promise.resolve();
      await h.advance(29999);assert.equal(h.requests[0].options.signal.aborted,false);
      await h.advance(1);const error=await outcome;
      assert(error.ambiguous);assert.equal(h.requests[0].options.signal.aborted,true);assert.equal(h.hooks.state.requests.size,0);
      pending.resolve(body?{}:response({}));await Promise.resolve();
      assert.equal(h.requests.length,1);assert(!h.timers.some(timer=>!timer.cancelled&&!timer.fired));
    }
  });
  await test('A stalled batch poll times out, releases its lock and backs off without accepting its late result', async () => {
    const pending=deferred(),h=setup({landing:true,fetch:()=>pending.promise});h.hooks.state.batchId='batch-a';
    const poll=h.hooks.pollBatch();await until(()=>h.requests.length===1,'poll starts');
    await h.advance(30000);await poll;
    assert.equal(h.hooks.state.polling,false);assert.equal(h.hooks.state.pollFailures,1);
    assert.equal(h.timers[h.hooks.state.pollTimer-1].delay,8000);assert.equal(h.requests[0].options.signal.aborted,true);
    pending.resolve(response({id:'batch-a',state:'finished',runs:[]}));await Promise.resolve();
    assert.equal(h.hooks.state.batchId,'batch-a');assert.equal(posts(h).length,0);
  });
  await test('Image retrieval bounds a stalled blob body and offers only explicit GET recovery', async () => {
    const pending=deferred(),h=setup({landing:true,fetch:()=>({...response({},200,{'Content-Type':'image/webp'}),blob:()=>pending.promise})});
    const complete=run('image-body',itemsA[0],1,'completed');complete.availability='available_to_retrieve';
    h.hooks.showFeed(feedData(feedA,itemsA,[complete]));await until(()=>h.requests.length===1,'image fetch starts');
    await h.advance(30000);await until(()=>h.hooks.state.loadingImages.size===0,'recovery times out');
    assert.equal(h.requests[0].options.signal.aborted,true);assert(h.hooks.state.runs.get(complete.id).retrievalError);
    assert.equal(h.hooks.state.images.size,0);assert.equal(posts(h).length,0);assert.equal(h.hooks.state.requests.size,0);
    pending.resolve(new Blob(['late']));await Promise.resolve();assert.equal(h.localWrites.length,0);
  });
  await test('Expiry and pagehide abort reads, cancel queued polls and prevent wakeups from restarting disposed work', async () => {
    for(const reason of ['expiry','pagehide']) {
      const pending=deferred(),h=setup({landing:true,fetch:()=>pending.promise});h.hooks.state.batchId='batch-a';
      const poll=h.hooks.pollBatch();await until(()=>h.requests.length===1,'poll starts');
      for(const callback of h.windowEvents.online||[])callback();assert(h.hooks.state.pollTimer);
      if(reason==='expiry')h.hooks.expireView();else for(const callback of h.windowEvents.pagehide||[])callback({persisted:true});
      await poll;assert.equal(h.requests[0].options.signal.aborted,true);assert.equal(h.hooks.state.polling,false);
      for(const callback of h.windowEvents.online||[])callback();
      for(const callback of h.documentEvents.visibilitychange||[])callback();
      await h.advance(60000);assert.equal(h.requests.length,1);assert.equal(h.hooks.state.requests.size,0);
      assert(!h.timers.some(timer=>!timer.cancelled&&!timer.fired));
      if(reason==='pagehide') {for(const callback of h.windowEvents.pageshow||[])callback({persisted:true});assert.deepEqual(h.navigations,['reload']);}
      pending.resolve(response({id:'batch-a',state:'finished',runs:[]}));await Promise.resolve();
    }
  });
  await test('Timed-out feed submission remains posting and reload never resubmits before a same-key explicit retry', async () => {
    const pending=deferred(),h=setup({fetch:()=>pending.promise});const submit=h.submit(feedA.url);
    await until(()=>posts(h).length===1,'feed POST starts');const key=posts(h)[0].body.idempotency_key;
    await h.advance(30000);await submit;assert.equal(h.hooks.state.submitting,false);assert.equal(h.navigations.length,0);
    assert.equal(h.hooks.readPending().status,'posting');assert.equal(h.hooks.readPending().key,key);
    const reload=setup({unstarted:true,sessionRecords:h.sessionRecords,fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response(accepted())});
    await reload.hooks.start();assert.equal(posts(reload).length,0);
    await reload.submit(feedA.url);assert.equal(posts(reload).length,1);assert.equal(posts(reload)[0].body.idempotency_key,key);
    pending.resolve(response(accepted()));await Promise.resolve();assert.equal(h.navigations.length,0);
  });
  await test('Interrupted image retry preserves its durable key through BFCache reload and never automatically resubmits', async () => {
    const pending=deferred(),h=setup({landing:true,fetch:()=>pending.promise}),failedRun=run('failed-retry',itemsA[0],1,'failed');
    h.hooks.showFeed(feedData(feedA,itemsA,[failedRun]));
    const retry=h.hooks.retryRun(failedRun).catch(error=>error);await until(()=>posts(h).length===1,'retry POST starts');
    const key=posts(h)[0].body.idempotency_key;
    for(const callback of h.windowEvents.pagehide||[])callback({persisted:true});
    assert((await retry).ambiguous);assert.equal(failedRun.retryKey,key);
    for(const callback of h.windowEvents.pageshow||[])callback({persisted:true});assert.deepEqual(h.navigations,['reload']);
    const reload=setup({landing:true,sessionRecords:h.sessionRecords,fetch:()=>response({id:'retry-batch',state:'active',runs:[run('new-attempt')]})});
    const restored={...failedRun};delete restored.retryKey;reload.hooks.showFeed(feedData(feedA,itemsA,[restored]));
    assert.equal(posts(reload).length,0);await reload.hooks.retryRun(restored);
    assert.equal(posts(reload)[0].body.idempotency_key,key);assert.equal(restored.retryKey,undefined);
    assert(![...h.sessionRecords.keys()].some(value=>value.startsWith('pulsefeed:retry:')));
    pending.resolve(response({id:'old-result',state:'active',runs:[run('late')]}));await Promise.resolve();
    assert.equal(h.hooks.state.batchId,null);
  });
  const requestKey='6bbce7a8-5ca7-4bfb-b7c8-9fb75300b1a1';
  const recordKey='pulsefeed:pending-feed:account-a';
  const requestRecord=(updates={})=>({version:1,account_id:'account-a',url:feedA.url,key:requestKey,created_at:Date.now(),status:'ready',...updates});
  const pendingSetup=(options={})=>setup({pending:true,unstarted:true,sessionRecords:new Map([[recordKey,JSON.stringify(requestRecord())]]),
    fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response(accepted()),...options});
  await test('Persisted admission errors explain the specific allowance or busy cause with SSR parity and no stale countdown', async () => {
    const source=fs.readFileSync(templatePath,'utf8');
    const copies=JSON.parse(source.match(/\{% set admission_messages = ([\s\S]*?) %\}/)[1]);
    for(const adminMode of [false,true])for(const code of ['FEED_DAILY_LIMIT','GLOBAL_DAILY_LIMIT','GLOBAL_HOURLY_LIMIT','BUSY','PROVIDER_SECRET','__proto__']){
      const h=setup({landing:true,adminMode});h.hooks.showFeed(feedData(feedA,itemsA,[],{status:'blocked',error:{code,message:'RAW_PRIVATE_MESSAGE'},retry_after:21544}));
      const expected=copies[code]&&Object.hasOwn(copies,code)?copies[code]:copies.OTHER;
      assert.equal(h.elements.get('generation-notice').hidden,false);
      assert.equal(h.elements.get('generation-notice-title').textContent,expected[0]);assert.equal(h.elements.get('generation-notice-message').textContent,expected[1]);
      const copy=h.elements.get('generation-notice-message').textContent;
      assert(!copy.includes('RAW_PRIVATE_MESSAGE'));assert(!copy.includes('21544'));assert(!copy.includes('seconds'));
      if(code==='GLOBAL_DAILY_LIMIT'){assert(copy.includes('00:00 UTC'));assert(copy.includes('not enough'));}
      if(code==='FEED_DAILY_LIMIT'){assert.equal(copy,'This page was opened under the previous feed allowance. Load a new feed to use the current limits.');assert(!copy.includes('00:00 UTC'));}
      if(code==='GLOBAL_HOURLY_LIMIT'){assert(copy.includes('rolling one-hour window'));assert(!copy.includes('00:00 UTC'));}
      assert.equal(posts(h).length,0);assert.equal(h.elements.get('fun-loader').dataset.active,'false');
      h.hooks.showFeed(feedData());assert.equal(h.elements.get('generation-notice').hidden,true);
    }
  });
  await test('Home validates with one durable CSRF POST before navigation and locks editing during the wait', async () => {
    const pending=deferred();let h;
    h=setup({adminMode:true,fetch:()=>{
      assert.equal(JSON.parse(h.sessionRecords.get(recordKey)).status,'posting');return pending.promise;
    }});
    const submitted=h.submit(`  ${feedA.url}  `);await until(()=>posts(h).length===1,'home POST starts');
    assert.equal(h.navigations.length,0);assert.equal(h.elements.get('feed-url').disabled,true);
    assert.equal(h.elements.get('view-feed').disabled,true);assert.equal(h.elements.get('view-feed').textContent,'View RSS ↗');
    assert.equal(h.elements.get('reader-status').textContent,'Checking feed…');assert.equal(h.elements.has('fun-loader'),false);
    const request=posts(h)[0];assert.deepEqual(plain(request.body),{url:feedA.url,idempotency_key:JSON.parse(h.sessionRecords.get(recordKey)).key});
    assert.equal(request.options.headers['X-CSRF-Token'],'test-csrf');assert.equal(request.options.credentials,'same-origin');
    assert(!h.sessionRecords.get(recordKey).includes('test-csrf'));
    await h.dispatch('feed-form','submit');assert.equal(posts(h).length,1);assert.equal(h.navigations.length,0);
    pending.resolve(response(accepted()));await submitted;
    assert.deepEqual(h.navigations,['/feeds/view-a?admin']);assert.equal(JSON.parse(h.sessionRecords.get(recordKey)).status,'done');
    assert.equal(h.elements.get('feed-url').disabled,true);assert.equal(h.hooks.state.navigating,true);
  });
  await test('Invalid feeds stay under the home input without any navigation and corrected input gets a fresh key', async () => {
    for(const code of ['INVALID_FEED','EMPTY_FEED','UNSAFE_FEED','FEED_TOO_LARGE']) {
      let valid=false;const h=setup({fetch:()=>valid?response(accepted()):response({error:{code,message:'RAW_PRIVATE_SERVER_MESSAGE'}},400)});
      await h.submit(feedA.url);assert.equal(h.navigations.length,0);assert.equal(posts(h).length,1);
      const rejected=JSON.parse(h.sessionRecords.get(recordKey));assert.equal(rejected.status,'rejected');assert.equal(rejected.error_code,code);
      assert.equal(h.elements.get('feed-url').value,feedA.url);assert.equal(h.elements.get('feed-url').disabled,false);
      assert.equal(h.elements.get('feed-url').getAttribute('aria-invalid'),'true');assert(h.elements.get('reader-status').textContent);
      assert(!h.elements.get('reader-status').textContent.includes('RAW_PRIVATE'));assert.equal(h.elements.get('feedback').textContent,'');
      h.elements.get('feed-url').value=feedB.url;await h.dispatch('feed-url','input');
      assert.equal(h.elements.get('reader-status').textContent,'');assert.equal(h.elements.get('feed-url').getAttribute('aria-invalid'),null);
      valid=true;await h.submit(feedB.url);assert.equal(posts(h).length,2);assert.notEqual(posts(h)[1].body.idempotency_key,rejected.key);
      assert.equal(posts(h)[1].body.url,feedB.url);assert.deepEqual(h.navigations,['/feeds/view-a']);
    }
  });
  await test('Active-page capacity rejection stays inline, honors cooldown and never replays on reload', async () => {
    let full=true;
    const h=setup({fetch:()=>full?response({error:{code:'VIEW_LIMIT_REACHED',message:'RAW_PRIVATE'}},429,{'Retry-After':'12'}):response(accepted())});
    await h.submit(feedA.url);assert.equal(posts(h).length,1);assert.equal(h.navigations.length,0);
    const record=JSON.parse(h.sessionRecords.get(recordKey));assert.equal(record.status,'rejected');assert.equal(record.error_code,'VIEW_LIMIT_REACHED');
    assert(h.elements.get('reader-status').textContent.includes('earlier page expires'));assert(!h.elements.get('reader-status').textContent.includes('RAW_PRIVATE'));
    assert.equal(h.elements.get('feed-url').getAttribute('aria-invalid'),null);assert.equal(h.elements.get('view-feed').disabled,true);
    await h.submit(feedB.url);assert.equal(posts(h).length,1);
    const reload=setup({unstarted:true,sessionRecords:h.sessionRecords,fetch:()=>response({account:{id:'account-a'},csrf:'test-csrf'})});
    await reload.hooks.start();assert.equal(posts(reload).length,0);assert(reload.elements.get('reader-status').textContent.includes('earlier page expires'));
    await h.advance(12000);full=false;await h.submit(feedB.url);
    assert.equal(posts(h).length,2);assert.notEqual(posts(h)[1].body.idempotency_key,record.key);assert.deepEqual(h.navigations,['/feeds/view-a']);
    const pending=pendingSetup({fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response({error:{code:'VIEW_LIMIT_REACHED'}},429,{'Retry-After':'12'})});
    await pending.hooks.start();assert.deepEqual(pending.navigations,['/']);assert.equal(JSON.parse(pending.sessionRecords.get(recordKey)).status,'rejected');
  });
  await test('Feed-fetch capacity rejection stays inline with one-second cooldown and no automatic replay', async () => {
    let busy=true;
    const h=setup({fetch:()=>busy?response({error:{code:'FEED_BUSY',message:'RAW_PRIVATE'}},429,{'Retry-After':'1'}):response(accepted())});
    await h.submit(feedA.url);assert.equal(posts(h).length,1);assert.equal(h.navigations.length,0);
    const record=JSON.parse(h.sessionRecords.get(recordKey));assert.equal(record.status,'rejected');assert.equal(record.error_code,'FEED_BUSY');
    assert(h.elements.get('reader-status').textContent.includes('busy right now'));assert(!h.elements.get('reader-status').textContent.includes('RAW_PRIVATE'));
    assert.equal(h.elements.get('feed-url').getAttribute('aria-invalid'),null);assert.equal(h.elements.get('view-feed').disabled,true);
    await h.submit(feedA.url);assert.equal(posts(h).length,1);
    const reload=setup({unstarted:true,sessionRecords:h.sessionRecords,fetch:()=>response({account:{id:'account-a'},csrf:'test-csrf'})});
    await reload.hooks.start();assert.equal(posts(reload).length,0);assert(reload.elements.get('reader-status').textContent.includes('busy right now'));
    assert.equal(reload.elements.get('feed-url').getAttribute('aria-invalid'),null);
    await h.advance(1000);assert.equal(posts(h).length,1);busy=false;await h.submit(feedA.url);
    assert.equal(posts(h).length,2);assert.notEqual(posts(h)[1].body.idempotency_key,record.key);assert.deepEqual(h.navigations,['/feeds/view-a']);
    const pending=pendingSetup({fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response({error:{code:'FEED_BUSY'}},429,{'Retry-After':'1'})});
    await pending.hooks.start();assert.deepEqual(pending.navigations,['/']);assert.equal(JSON.parse(pending.sessionRecords.get(recordKey)).status,'rejected');
    const pendingReload=pendingSetup({sessionRecords:pending.sessionRecords});await pendingReload.hooks.start();assert.equal(posts(pendingReload).length,0);
  });
  await test('Home reload never replays uncertain submissions and explicit retry preserves the original key', async () => {
    for(const failure of [()=>{throw Error('offline');},()=>response({},503),()=>response({}),
      ()=>({ok:true,status:200,json:async()=>{throw Error('truncated');}})]) {
      const h=setup({fetch:failure});await h.submit(feedA.url);
      assert.equal(posts(h).length,1);assert.equal(h.navigations.length,0);assert.equal(h.elements.get('feed-url').disabled,false);
      const stored=JSON.parse(h.sessionRecords.get(recordKey));assert.equal(stored.status,'posting');
      const reload=setup({unstarted:true,sessionRecords:h.sessionRecords,fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response(accepted())});
      await reload.hooks.start();assert.equal(posts(reload).length,0);assert.equal(reload.navigations.length,0);
      assert.equal(reload.elements.get('feed-url').value,feedA.url);assert(reload.elements.get('reader-status').textContent.includes('same request'));
      await reload.submit(feedA.url);assert.equal(posts(reload).length,1);assert.equal(posts(reload)[0].body.idempotency_key,stored.key);
      assert.deepEqual(reload.navigations,['/feeds/view-a']);
    }
  });
  await test('Home same-page retry and failed done write retain a posting key until durable confirmation', async () => {
    const records=new Map();let writes=0;
    const storage={getItem:key=>records.get(key)??null,setItem(key,value){writes++;if(writes===2)throw Error('disk full');records.set(key,value);},removeItem:key=>records.delete(key)};
    const h=setup({sessionRecords:records,sessionStorage:storage,fetch:()=>response(accepted())});
    await h.submit(feedA.url);assert.equal(posts(h).length,1);assert.equal(h.navigations.length,0);
    const stored=JSON.parse(records.get(recordKey));assert.equal(stored.status,'posting');assert.equal(h.elements.get('feed-url').disabled,false);
    await h.submit(feedA.url);assert.equal(posts(h).length,2);assert.equal(posts(h)[1].body.idempotency_key,stored.key);
    assert.equal(JSON.parse(records.get(recordKey)).status,'done');assert.deepEqual(h.navigations,['/feeds/view-a']);
  });
  await test('Home startup leaves ready and done records idle; only an explicit done click opens the saved view', async () => {
    for(const status of ['ready','done']) {
      const record=requestRecord(status==='done'?{status,view:viewA}:{status});
      const h=setup({unstarted:true,sessionRecords:new Map([[recordKey,JSON.stringify(record)]]),fetch:request=>{
        assert.equal(request.url,'/api/session');return response({account:{id:'account-a'},csrf:'test-csrf'});
      }});
      await h.hooks.start();assert.equal(posts(h).length,0);assert.equal(h.navigations.length,0);assert.equal(h.elements.get('feed-url').value,feedA.url);
      if(status==='done') {await h.submit(feedA.url);assert.equal(posts(h).length,0);assert.deepEqual(h.navigations,['/feeds/view-a']);}
    }
  });
  await test('Verified reader arrival consumes its matching done request so the next home is blank and the same RSS gets a fresh key', async () => {
    const records=new Map([[recordKey,JSON.stringify(requestRecord({status:'done',view:viewA}))]]);
    const reader=setup({landing:true,unstarted:true,initialFeed:startedData(),sessionRecords:records,
      fetch:request=>{assert.equal(request.url,'/api/session');return response({account:{id:'account-a'},csrf:'test-csrf'});}});
    await reader.hooks.start();assert.equal(records.has(recordKey),false);assert.equal(posts(reader).length,0);
    const home=setup({unstarted:true,sessionRecords:records,fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response(accepted())});
    await home.hooks.start();assert.equal(home.elements.get('feed-url').value,'');assert.equal(home.elements.get('reader-status').textContent,'');
    assert.equal(home.navigations.length,0);assert.equal(posts(home).length,0);
    await home.submit(feedA.url);assert.equal(posts(home).length,1);assert.notEqual(posts(home)[0].body.idempotency_key,requestKey);
  });
  await test('Reader arrival clears neither a newer pending request nor another view or account and never consumes before authentication', async () => {
    const anotherView={...viewA,id:'view-b',url:'/feeds/view-b'};
    for(const record of [requestRecord({status:'posting'}),requestRecord({status:'ready'}),
      requestRecord({status:'done',view:anotherView}),requestRecord({status:'done',view:viewA,account_id:'other-account'})]) {
      const raw=JSON.stringify(record), records=new Map([[recordKey,raw],['pulsefeed:pending-feed:other-account','private-other-record']]);
      const h=setup({landing:true,unstarted:true,initialFeed:startedData(),sessionRecords:records,
        fetch:()=>response({account:{id:'account-a'},csrf:'test-csrf'})});
      await h.hooks.start();assert.equal(records.get(recordKey),raw);assert.equal(records.get('pulsefeed:pending-feed:other-account'),'private-other-record');
    }
    const raw=JSON.stringify(requestRecord({status:'done',view:viewA}));
    const session=deferred(), records=new Map([[recordKey,raw]]);
    const h=setup({landing:true,unstarted:true,initialFeed:startedData(),sessionRecords:records,fetch:()=>session.promise});
    const start=h.hooks.start();assert.equal(records.get(recordKey),raw);
    session.resolve(response({account:{id:'different'},csrf:'different'}));await start;
    assert.equal(records.get(recordKey),raw);assert(h.hooks.state.expired);
  });
  await test('Home storage read/write failures prevent POST and malformed successes never navigate', async () => {
    for(const storage of [{getItem(){throw Error('blocked');}}, {getItem:()=>null,setItem(){throw Error('blocked');}},
      {getItem:()=>null,setItem(){},removeItem(){}}]) {
      const h=setup({sessionStorage:storage});await h.submit(feedA.url);assert.equal(posts(h).length,0);assert.equal(h.navigations.length,0);
      assert(h.elements.get('reader-status').textContent.includes('storage'));assert.equal(h.elements.get('feed-url').disabled,false);
    }
    for(const payload of [{},accepted({...viewA,url:'https://evil.example'}),accepted({...viewA,id:'new',url:'/feeds/new'}),
      {...accepted(),server_now:null},{...accepted(),server_now:viewA.expires_at},accepted({...viewA,expires_at:viewA.created_at+3601})]) {
      const h=setup({fetch:()=>response(payload)});await h.submit(feedA.url);
      assert.equal(h.navigations.length,0);assert.equal(JSON.parse(h.sessionRecords.get(recordKey)).status,'posting');
    }
  });
  await test('Home authentication failure preserves the uncertain key and never navigates or replays', async () => {
    const h=setup({fetch:()=>response({},401)});await h.submit(feedA.url);
    assert(h.hooks.state.expired);assert.equal(posts(h).length,1);assert.equal(h.navigations.length,0);
    assert.equal(JSON.parse(h.sessionRecords.get(recordKey)).status,'posting');
    await h.submit(feedA.url);assert.equal(posts(h).length,1);
  });
  await test('Pending validates session then durably marks posting before its one automatic CSRF POST', async () => {
    let h;h=pendingSetup({adminMode:true,fetch:request=>{
      if(request.url==='/api/session')return response({account:{id:'account-a'},csrf:'test-csrf'});
      assert.equal(JSON.parse(h.sessionRecords.get(recordKey)).status,'posting');return response(accepted());
    }});
    await h.hooks.start();assert.equal(posts(h).length,1);const request=posts(h)[0];
    assert.deepEqual(plain(request.body),{url:feedA.url,idempotency_key:requestKey});assert.equal(request.options.headers['X-CSRF-Token'],'test-csrf');
    assert.equal(request.options.credentials,'same-origin');assert.deepEqual(h.navigations,['/feeds/view-a?admin']);
    assert.equal(JSON.parse(h.sessionRecords.get(recordKey)).status,'done');await h.hooks.openPending();assert.equal(posts(h).length,1);
  });
  await test('Ambiguous refresh never automatically replays; explicit retry validates session and reuses its key', async () => {
    const h=pendingSetup({fetch:request=>{if(request.url==='/api/session')return response({account:{id:'account-a'},csrf:'test-csrf'});throw Error('offline');}});
    await h.hooks.start();assert.equal(posts(h).length,1);assert.equal(JSON.parse(h.sessionRecords.get(recordKey)).status,'posting');
    const reload=pendingSetup({sessionRecords:h.sessionRecords});await reload.hooks.start();assert.equal(posts(reload).length,0);
    assert.equal(reload.elements.get('pending-retry').hidden,false);assert.equal(reload.elements.get('pending-retry').disabled,false);assert.equal(reload.navigations.length,0);
    await reload.dispatch('pending-retry','click');assert.equal(posts(reload).length,1);assert.equal(posts(reload)[0].body.idempotency_key,requestKey);
    assert.deepEqual(reload.navigations,['/feeds/view-a']);
  });
  await test('Malformed success and server failures preserve a posting key without automatic retries', async () => {
    for(const reply of [response({}),response({...accepted(),view:{...viewA,url:'https://example.org'}}),response({},503),
      {ok:true,status:200,json:async()=>{throw Error('truncated');}}]) {
      const h=pendingSetup({fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):reply});
      await h.hooks.start();assert.equal(posts(h).length,1);assert.equal(JSON.parse(h.sessionRecords.get(recordKey)).status,'posting');
      await h.hooks.openPending();assert.equal(posts(h).length,1);assert.equal(h.navigations.length,0);
    }
  });
  await test('Duplicate pending starts and manual clicks cannot overlap a POST', async () => {
    const pending=deferred();const h=pendingSetup({fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):pending.promise});
    const first=h.hooks.start();await until(()=>posts(h).length===1,'pending POST starts');await h.hooks.start(true);await h.dispatch('pending-retry','click');
    assert.equal(posts(h).length,1);pending.resolve(response(accepted()));await first;
  });
  await test('Transient session validation failure exposes enabled retry that revalidates before consuming the original key', async () => {
    let offline=true;const h=pendingSetup({fetch:request=>{
      if(offline)throw Error('offline');
      return request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response(accepted());
    }});
    await h.hooks.start();assert.equal(posts(h).length,0);assert.equal(h.hooks.state.account,null);
    assert.equal(h.elements.get('pending-retry').hidden,false);assert.equal(h.elements.get('pending-retry').disabled,false);
    assert.equal(JSON.parse(h.sessionRecords.get(recordKey)).status,'ready');
    offline=false;await h.dispatch('pending-retry','click');assert.equal(posts(h).length,1);
    assert.equal(posts(h)[0].body.idempotency_key,requestKey);assert.deepEqual(h.navigations,['/feeds/view-a']);
  });
  await test('Storage denial fails closed before POST; a failed done write preserves same-key manual recovery', async () => {
    const blocked=setup({sessionStorage:{setItem(){throw Error('blocked');}}});await blocked.submit(feedA.url);
    assert.equal(blocked.requests.length,0);assert.equal(blocked.navigations.length,0);assert(blocked.elements.get('reader-status').textContent.includes('session storage'));
    const records=new Map([[recordKey,JSON.stringify(requestRecord())]]);let writes=0;
    const store={getItem:key=>records.get(key)??null,setItem(key,value){writes++;if(writes===2)throw Error('quota');records.set(key,value);},removeItem:key=>records.delete(key)};
    const h=pendingSetup({sessionRecords:records,sessionStorage:store});await h.hooks.start();
    assert.equal(posts(h).length,1);assert.equal(h.navigations.length,0);assert.equal(JSON.parse(records.get(recordKey)).status,'posting');
    await h.dispatch('pending-retry','click');assert.equal(posts(h).length,2);assert.equal(posts(h)[1].body.idempotency_key,requestKey);assert.equal(h.navigations.length,1);
  });
  await test('Invalid, expired, foreign and unsafe done manifests never allocate or navigate; valid done reload only redirects', async () => {
    for(const updates of [{account_id:'foreign'},{created_at:Date.now()-600001},{created_at:Date.now()+60000},{url:'https://u:p@example.org'},
      {key:'bad'},{status:'unexpected'},{view:viewA},{url:'https:\\example.org/feed'},{status:'done',view:{...viewA,url:'https://evil.example'}},{status:'done',view:{...viewA,id:'new',url:'/feeds/new'}}, {csrf:'secret'}]) {
      const h=pendingSetup({sessionRecords:new Map([[recordKey,JSON.stringify(requestRecord(updates))]])});await h.hooks.start();
      assert.equal(posts(h).length,0);assert.equal(h.navigations.length,0);
    }
    const done=pendingSetup({sessionRecords:new Map([[recordKey,JSON.stringify(requestRecord({status:'done',view:viewA}))]])});
    await done.hooks.start();assert.equal(posts(done).length,0);assert.deepEqual(done.navigations,['/feeds/view-a']);
    const other=pendingSetup({fetch:()=>response({account:{id:'different'},csrf:'different'})});await other.hooks.start();
    assert.equal(posts(other).length,0);assert(other.hooks.state.expired);assert.equal(JSON.parse(other.sessionRecords.get(recordKey)).status,'ready');
  });
  await test('Definitive feed rejections return fixed inline home guidance with preserved URL and zero refresh allocation', async () => {
    for(const code of ['INVALID_FEED','EMPTY_FEED','UNSAFE_FEED','FEED_TOO_LARGE']) {
      const h=pendingSetup({adminMode:true,fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response({error:{code,message:'RAW_PRIVATE_SERVER_MESSAGE'}},400)});
      await h.hooks.start();assert.equal(posts(h).length,1);assert.deepEqual(h.navigations,['/?admin']);
      const record=JSON.parse(h.sessionRecords.get(recordKey));assert.equal(record.status,'rejected');assert.equal(record.error_code,code);assert.equal(record.url,feedA.url);
      assert(!h.sessionRecords.get(recordKey).includes('RAW_PRIVATE_SERVER_MESSAGE'));
      const home=setup({unstarted:true,adminMode:true,sessionRecords:h.sessionRecords,fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response(accepted())});
      await home.hooks.start();assert.equal(posts(home).length,0);assert.equal(home.elements.get('feed-url').value,feedA.url);
      assert.equal(home.elements.get('feed-url').getAttribute('aria-invalid'),'true');
      assert(home.elements.get('reader-status').textContent.includes(code==='EMPTY_FEED'?'no readable articles':code==='UNSAFE_FEED'?'public RSS':code==='FEED_TOO_LARGE'?'too large':'readable RSS or Atom'));
      assert(!home.elements.get('feedback').textContent);
      const reload=pendingSetup({sessionRecords:h.sessionRecords});await reload.hooks.start();assert.equal(posts(reload).length,0);assert.deepEqual(reload.navigations,['/']);
      await home.dispatch('feed-url','input');assert.equal(home.elements.get('reader-status').textContent,'');assert.equal(home.elements.get('feed-url').getAttribute('aria-invalid'),null);
      await home.submit(feedB.url);const fresh=JSON.parse(home.sessionRecords.get(recordKey));assert.equal(fresh.status,'done');assert.equal(fresh.url,feedB.url);assert.notEqual(fresh.key,requestKey);assert(!Object.hasOwn(fresh,'error_code'));assert.equal(posts(home).length,1);assert.deepEqual(home.navigations,['/feeds/view-a?admin']);
    }
  });
  await test('Transient feed errors and5xx remain posting with explicit same-key retries rather than rejected handback', async () => {
    for(const [code,status] of [['FEED_TIMEOUT',400],['FEED_UNREACHABLE',400],['INVALID_FEED',503],['INVALID_INPUT',400]]) {
      const h=pendingSetup({fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response({error:{code,message:'Try later'}},status)});
      await h.hooks.start();assert.equal(JSON.parse(h.sessionRecords.get(recordKey)).status,'posting');assert.equal(h.navigations.length,0);
      await h.dispatch('pending-retry','click');assert.equal(posts(h).length,2);assert.equal(posts(h)[1].body.idempotency_key,requestKey);
    }
  });
  await test('Rejected storage failure stays visibly inline on pending page and never loses its posting key', async () => {
    const records=new Map([[recordKey,JSON.stringify(requestRecord())]]);let writes=0;
    const storage={getItem:key=>records.get(key)??null,setItem(key,value){if(++writes===2)throw Error('blocked');records.set(key,value);},removeItem:key=>records.delete(key)};
    const h=pendingSetup({sessionRecords:records,sessionStorage:storage,fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):response({error:{code:'INVALID_FEED',message:'RAW_PRIVATE'}},400)});
    await h.hooks.start();assert.equal(h.navigations.length,0);assert.equal(JSON.parse(records.get(recordKey)).status,'posting');
    assert(h.elements.get('pending-status').textContent.includes('readable RSS or Atom'));assert(!h.elements.get('pending-status').textContent.includes('RAW_PRIVATE'));assert.equal(h.elements.get('pending-retry').hidden,true);
  });
  await test('Rejected manifests are restored only after authentication; unsafe, expired or malformed records are ignored', async () => {
    for(const updates of [{error_code:'ARBITRARY'},{error_code:['INVALID_FEED']},{error_code:'INVALID_FEED',error_message:'unsafe'},
      {account_id:'foreign',error_code:'INVALID_FEED'},{url:'javascript:alert(1)',error_code:'INVALID_FEED'},
      {created_at:Date.now()-600001,error_code:'INVALID_FEED'}]) {
      const h=setup({unstarted:true,sessionRecords:new Map([[recordKey,JSON.stringify(requestRecord({status:'rejected',...updates}))]]),fetch:()=>response({account:{id:'account-a'},csrf:'test-csrf'})});
      await h.hooks.start();assert.equal(h.elements.get('feed-url').value,'');assert.equal(h.elements.get('reader-status').textContent,'');assert.equal(posts(h).length,0);
    }
    const session=deferred();const h=setup({unstarted:true,sessionRecords:new Map([[recordKey,JSON.stringify(requestRecord({status:'rejected',error_code:'INVALID_FEED'}))]]),fetch:()=>session.promise});
    const start=h.hooks.start();assert.equal(h.elements.get('feed-url').value,'');session.resolve(response({account:{id:'foreign'},csrf:'other'}));await start;
    assert(h.hooks.state.expired);assert.equal(h.elements.get('feed-url').value,'');assert.equal(posts(h).length,0);
    const malformed=setup();await malformed.submit('not a URL');assert.equal(malformed.elements.get('feed-url').getAttribute('aria-invalid'),'true');
    assert(malformed.elements.get('reader-status').textContent.includes('HTTP(S)'));assert.equal(malformed.requests.length,0);assert.equal(malformed.navigations.length,0);
  });
  await test('Home starts SSR-disabled with stable initializing markers until a matching session is verified', async () => {
    const session=deferred();const h=setup({unstarted:true,fetch:()=>session.promise});
    const button=h.elements.get('view-feed'),input=h.elements.get('feed-url');
    assert.equal(button.disabled,true);assert.equal(input.disabled,true);assert.equal(button.getAttribute('data-initializing'),'');
    await h.submit(feedA.url);assert.equal(h.requests.length,0);assert.equal(h.navigations.length,0);
    const started=h.hooks.start();assert.equal(button.disabled,true);assert.equal(input.disabled,true);assert.equal(button.getAttribute('data-initializing'),'');
    session.resolve(response({account:{id:'account-a'},csrf:'test-csrf'}));await started;
    assert.equal(button.disabled,false);assert.equal(input.disabled,false);assert.equal(button.getAttribute('data-initializing'),null);assert.equal(input.getAttribute('data-initializing'),null);
    const failed=setup({unstarted:true,fetch:()=>response({},503)});await failed.hooks.start();assert.equal(failed.elements.get('view-feed').disabled,true);assert.equal(failed.elements.get('feed-url').disabled,true);
  });
  await test('Unverified home session failures give actionable refresh guidance without unlocking or submitting', async () => {
    for (const fetch of [() => {throw new Error('offline');}, () => response({}), () => ({...response({}), json: async () => {throw new SyntaxError('truncated');}})]) {
      const h=setup({unstarted:true,fetch});await h.hooks.start();
      assert.equal(h.elements.get('reader-status').textContent,'Couldn’t verify your session. Refresh this page to try again.');
      assert.equal(h.elements.get('view-feed').disabled,true);assert.equal(h.elements.get('feed-url').disabled,true);
      await h.submit(feedA.url);assert.equal(posts(h).length,0);assert.equal(h.navigations.length,0);
    }
  });
  await test('Home startup only validates session and never restores history or rewrites its URL', async () => {
    const h = setup({unstarted: true, fetch: request => {assert.equal(request.url, '/api/session'); return response({account: {id: 'account-a'}, csrf: 'test-csrf'});}});
    await h.hooks.start(); assert.equal(h.requests.length, 1); assert.equal(posts(h).length, 0);
    assert.equal(h.hooks.state.currentFeed, null); assert.equal(h.localReads.length, 0); assert.equal(h.navigations.length, 0);
  });
  await test('Landing SSR hydration preserves its fixed expiry and performs no new inference or source fetch', async () => {
    const h = setup({landing: true, unstarted: true, initialFeed: startedData(), fetch: request => {
      assert.equal(request.url, '/api/session'); return response({account: {id: 'account-a'}, csrf: 'test-csrf'});}});
    const expiry = h.boot.view.expires_at; await h.hooks.start();
    assert.equal(h.hooks.state.items[0].id, itemsA[0].id); assert.equal(h.hooks.state.view.expires_at, expiry);
    assert.equal(h.requests.length, 1); assert.equal(posts(h).length, 0); assert.equal(h.navigations.length, 0);
  });
  await test('Account mismatch never opens locally saved images', async () => {
    const h = setup({landing: true, unstarted: true, initialFeed: feedData(feedA, itemsA, [run('complete', itemsA[0], 1, 'completed')]),
      fetch: () => response({account: {id: 'different'}, csrf: 'different'})});
    await h.hooks.start(); assert.equal(h.localReads.length, 0); assert(h.hooks.state.expired); assert(h.navigations.includes('reload'));
  });
  await test('Reader stays in source order with latest per-item runs and never shows another view’s unrelated stories', async () => {
    const h = setup({landing: true}); h.hooks.showFeed(feedData());
    const latest = run('new-first', itemsA[0], 20); latest.original.title = 'Old snapshot';
    h.hooks.acceptRuns([latest, run('older-first', itemsA[0], 10), run('second-run', itemsA[1], 30), run('unrelated', itemsB[0], 100)]);
    assert.equal(cards(h).length, 2); assert(cards(h)[0].textContent.includes(itemsA[0].title));
    assert(cards(h)[1].textContent.includes(itemsA[1].title)); assert(!text(h).includes('Old snapshot')); assert(!text(h).includes(itemsB[0].title));
  });
  await test('Technical prompts and telemetry require explicit admin mode', async () => {
    for (const [role, adminMode] of [['evaluator', false], ['admin', false], ['admin', true]]) {
      const h = setup({landing: true, role, adminMode}); h.hooks.showFeed(feedData(feedA, itemsA, [run('detail')]));
      assert.equal(h.elements.get('item-list').querySelectorAll('details').length, adminMode ? 1 : 0);
      assert.equal(text(h).includes('prompt-detail'), adminMode);
    }
  });
  await test('Batch polling and explicit retries include their view membership and do not extend its expiry', async () => {
    const h = setup({landing: true, fetch: request => response({id: 'batch', state: 'finished', runs: [run('retried')]} )});
    h.hooks.showFeed(feedData()); h.hooks.state.batchId = 'batch'; await h.hooks.pollBatch();
    assert.equal(h.requests[0].url, '/api/batches/batch?view_id=view-a');
    h.hooks.state.batchId = null; const expiry = h.hooks.state.view.expires_at; await h.hooks.retryRun(run('failed'));
    assert.equal(posts(h)[0].url, '/api/runs/failed/retry?view_id=view-a'); assert.equal(h.hooks.state.view.expires_at, expiry);
  });
  await test('Malformed successful retry retains its key and does not claim another attempt started', async () => {
    let valid = false; const h = setup({landing: true, fetch: () => response(valid ? {id: 'batch', state: 'active', runs: [run('linked')]} : {})});
    h.hooks.showFeed(feedData()); const original = run('failed'); await assert.rejects(h.hooks.retryRun(original));
    const key = original.retryKey; assert(key); assert.equal(h.hooks.state.batchId, null); valid = true;
    await h.hooks.retryRun(original); assert.equal(posts(h)[1].body.idempotency_key, key); assert.equal(original.retryKey, undefined);
  });
  await test('Retrieval failure survives current-run replacement and offers GET-only recovery', async () => {
    const pending = deferred(); let first = true;
    const h = setup({landing: true, fetch: () => {if (first) {first = false; return pending.promise;} return response({}, 410);}});
    const original = run('image', itemsA[0], 1, 'completed'); original.availability = 'available_to_retrieve';
    h.hooks.showFeed(feedData(feedA, itemsA, [])); h.hooks.state.runs.set(original.id, original);
    const recovery = h.hooks.recoverImage(original); await until(() => h.requests.length === 1, 'Image GET must start');
    h.hooks.acceptRuns([{...original}]); pending.resolve(response({}, 503)); await recovery;
    const current = h.hooks.state.runs.get(original.id); assert(current.retrievalError); assert(text(h).includes('Load image again'));
    const button = h.elements.get('item-list').querySelectorAll('button').find(node => node.textContent.includes('Load image again'));
    for (const callback of button.listeners.click || []) await callback();
    assert.equal(h.requests.length, 2); assert(h.requests.every(request => request.url === '/api/runs/image/image?view_id=view-a'));
    assert.equal(posts(h).length, 0); assert(h.hooks.state.unavailable.has('image'));
  });
  await test('View expiry clears unsaved and saved memory, stops polling, and rejects late image reads', async () => {
    const pending = deferred(); const h = setup({landing: true, fetch: () => pending.promise});
    h.hooks.showFeed(feedData()); const original = run('late', itemsA[0], 1, 'completed'); original.availability = 'available_to_retrieve';
    const recovery = h.hooks.recoverImage(original); await until(() => h.requests.length === 1, 'Image GET must start');
    h.hooks.state.images.set('unsaved', {url: URL.createObjectURL(new Blob(['unsaved'])), saved: false});
    h.hooks.state.images.set('saved', {url: URL.createObjectURL(new Blob(['saved'])), saved: true});
    h.hooks.expireView(); pending.resolve(response({}, 200, {'Content-Type': 'image/webp'})); await recovery;
    assert.equal(h.hooks.state.images.size, 0); assert.equal(h.localWrites.length, 0); assert.equal(h.hooks.state.batchId, null);
    assert.equal(h.elements.get('view-expired').hidden, false); assert.equal(h.elements.get('studio').hidden, true);
  });
  await test('A scoped missing/expired response redirects to the same private generic404 without POST replay', async () => {
    const h = setup({landing: true, adminMode: true, fetch: () => response({error: {code: 'VIEW_NOT_FOUND'}}, 404)});
    h.hooks.state.batchId = 'batch'; await h.hooks.pollBatch();
    assert.deepEqual(h.navigations, ['/feeds/view-a?admin']); assert(h.hooks.state.viewExpired); assert.equal(posts(h).length, 0);
  });
  await test('Playful feed-to-pixels phrases type at42ms, remain readable for3200ms and pause hidden/reduced-motion', async () => {
    const h = setup({landing: true}); h.hooks.state.batchId = 'batch'; h.hooks.updateControls();
    const first = h.hooks.state.funTimer; assert(first); h.hooks.updateControls(); assert.equal(h.hooks.state.funTimer, first);
    const lines=plain(h.hooks.funLines);assert(lines.every(line=>line.endsWith('...')&&line.length<70));
    assert(lines[0].includes('RSS'));assert(lines[1].includes('tokens'));assert(lines[2].includes('Jensen'));assert(lines[3].includes('GPUs'));
    assert(lines[4].includes('tensor'));assert(lines[5].includes('transformer'));assert(lines[6].includes('attention'));assert(lines[7].includes('parallelism'));
    const loaderTemplate=fs.readFileSync(path.join(root,'app/pulsefeed/templates/fun_loader.html'),'utf8');assert(loaderTemplate.includes(`'${lines[0]}' if`));
    assert(lines.includes('Asking Jensen for more compute...'));assert(lines.includes('Securing more GPUs...'));assert(lines.includes('Melting some GPUs...'));
    assert(lines.findIndex(line=>line.includes('GPUs'))<lines.findIndex(line=>line.includes('pixels')));
    for(let index=0;index<lines.length;index++){
      assert.equal(h.hooks.state.funIndex,index);assert.equal(h.elements.get('fun-message').textContent,lines[index][0]);
      assert.equal(h.timers[h.hooks.state.funTimer-1].delay,42);
      await h.advance((lines[index].length-1)*42);assert.equal(h.elements.get('fun-message').textContent,lines[index]);
      assert.equal(h.timers[h.hooks.state.funTimer-1].delay,3200);
      await h.advance(3199);assert.equal(h.hooks.state.funIndex,index);assert.equal(h.elements.get('fun-message').textContent,lines[index]);
      await h.advance(1);
    }
    assert.equal(h.hooks.state.funIndex,0);
    h.media.matches = true; h.media.change(); assert.equal(h.hooks.state.funTimer, null);
    assert.equal(h.elements.get('fun-message').textContent, lines[0]);
    h.document.hidden = true; h.hooks.updateLoader(); assert.equal(h.hooks.state.funTimer, null);
    h.document.hidden = false; h.media.matches = false; h.media.change(); assert(h.hooks.state.funTimer);
    h.hooks.expireView(); assert.equal(h.hooks.state.funTimer, null); assert.equal(h.hooks.state.loaderTimer, null);
  });
  await test('Fast cached restore fades immediately for 350 ms once and repeated updates preserve its deadline', async () => {
    const h=setup({landing:true,read:async()=>new Blob(['saved'],{type:'image/webp'})});
    const complete=run('cached',itemsA[0],1,'completed');
    h.hooks.showFeed(feedData(feedA,itemsA,[complete]));
    await until(()=>h.hooks.state.loadingImages.size===0,'cached restore settles');
    const list=h.elements.get('item-list'), loader=h.elements.get('fun-loader');
    assert.equal(list.dataset.reveal,'revealing');assert.equal(list.inert,false);
    assert.equal(loader.dataset.phase,'settling');assert.equal(loader.dataset.active,'true');
    assert.equal(list.style.values['--reveal-duration'],'350ms');assert.equal(list.style.values['--reveal-stagger'],'35ms');
    assert(cards(h)[0].querySelector('img'));assert.equal(h.requests.length,0);
    const deadline=h.hooks.state.revealReadyAt+h.hooks.state.revealDuration;
    await h.advance(175);h.hooks.acceptRuns([{...complete}]);h.hooks.updateControls();
    assert.equal(h.hooks.state.revealReadyAt+h.hooks.state.revealDuration,deadline);
    await h.advance(174);assert.equal(list.dataset.reveal,'revealing');
    await h.advance(1);assert.equal(list.dataset.reveal,'ready');assert.equal(loader.dataset.active,'false');
    assert.equal(h.hooks.state.funTimer,null);
    h.hooks.acceptRuns([{...complete}]);await h.advance(5000);
    assert.equal(list.dataset.reveal,'ready');assert.equal(h.localReads.length,1);assert.equal(h.requests.length,0);
  });
  for(const [wait,duration] of [[5000,600],[30000,1600]]) {
    await test(`Generation lasting ${wait} ms stays held until its terminal image then uses ${duration} ms fade`, async () => {
      const queued=run('generation');
      const h=setup({landing:true,read:async()=>new Blob(['saved']),fetch:()=>response({id:'batch-a',state:'active',runs:[queued]})});
      h.hooks.showFeed({...feedData(feedA,itemsA,[queued]),active_batch_id:'batch-a'});
      await h.advance(wait);h.hooks.acceptBatch({id:'batch-a',state:'active',runs:[{...queued}]});
      assert.equal(h.elements.get('item-list').dataset.reveal,'holding');assert.equal(h.elements.get('item-list').inert,true);
      const complete={...queued,state:'completed',availability:'unavailable'};
      h.hooks.acceptBatch({id:'batch-a',state:'finished',runs:[complete]});
      // Recovery begins before rendering the terminal batch, so no idle frame can start an early fade.
      assert(h.hooks.state.loadingImages.has(complete.id));assert.equal(h.elements.get('item-list').dataset.reveal,'holding');
      await until(()=>h.hooks.state.loadingImages.size===0,'terminal cached image settles');
      const list=h.elements.get('item-list');assert.equal(list.dataset.reveal,'revealing');
      assert.equal(list.style.values['--reveal-duration'],`${duration}ms`);
      assert.equal(list.style.values['--reveal-stagger'],`${duration*.1}ms`);
      const readyAt=h.hooks.state.revealReadyAt;
      await h.advance(duration-1);h.hooks.acceptBatch({id:'batch-a',state:'finished',runs:[{...complete}]});
      assert.equal(h.hooks.state.revealReadyAt,readyAt);assert.equal(list.dataset.reveal,'revealing');
      await h.advance(1);assert.equal(list.dataset.reveal,'ready');assert.equal(h.elements.get('fun-loader').dataset.active,'false');
      assert.equal(posts(h).length,0);
    });
  }
  for(const [wait,duration] of [[6000,720],[25000,1600]]) {
    await test(`Image network wait ${wait} ms holds its slot and settles with bounded ${duration} ms fade`, async () => {
      const pending=deferred();const h=setup({landing:true,fetch:()=>pending.promise});
      const complete=run('retrieval',itemsA[0],1,'completed');complete.availability='available_to_retrieve';
      h.hooks.showFeed(feedData(feedA,itemsA,[complete]));await until(()=>h.requests.length===1,'image GET starts');
      await h.advance(wait);assert.equal(h.elements.get('item-list').dataset.reveal,'holding');
      assert.equal(h.elements.get('item-list').inert,true);assert.equal(h.elements.get('fun-loader').dataset.phase,'working');
      pending.resolve(response({},200,{'Content-Type':'image/webp'}));await until(()=>h.hooks.state.loadingImages.size===0,'image settles');
      assert.equal(h.elements.get('item-list').dataset.reveal,'revealing');assert(cards(h)[0].querySelector('img'));
      assert.equal(h.hooks.state.revealDuration,duration);assert.equal(posts(h).length,0);
      await h.advance(duration-1);assert.equal(h.elements.get('item-list').dataset.reveal,'revealing');
      await h.advance(1);assert.equal(h.elements.get('item-list').dataset.reveal,'ready');
      assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,'/api/runs/retrieval/image?view_id=view-a');
    });
  }
  await test('Recovery errors bypass cosmetic fades and retain GET-only recovery plus the original story link', async () => {
    const complete=run('retrieval',itemsA[0],1,'completed');complete.availability='available_to_retrieve';
    const bad=setup({landing:true,fetch:()=>response({},503)});bad.hooks.showFeed(feedData(feedA,itemsA,[complete]));
    await until(()=>bad.hooks.state.loadingImages.size===0,'failure settles');
    assert.equal(bad.elements.get('item-list').dataset.reveal,'ready');assert.equal(bad.elements.get('item-list').inert,false);
    assert.equal(bad.elements.get('fun-loader').dataset.active,'false');assert(text(bad).includes('Load image again'));assert(text(bad).includes('Read original story'));
    assert.equal(posts(bad).length,0);assert.equal(bad.hooks.state.loaderTimer,null);
  });
  await test('Ten selected cards reveal after three decoded images while the batch and remaining placeholders continue', async () => {
    const stories=Array.from({length:10},(_,index)=>item(`story-${index}`,`Story ${index}`));
    const queued=stories.map((story,index)=>run(`run-${index}`,story));
    let batch={id:'batch-a',state:'active',runs:queued};const decodes=[];
    const h=setup({landing:true,read:async()=>new Blob(['saved']),decode:()=>{const pending=deferred();decodes.push(pending);return pending.promise;},fetch:()=>response(batch)});
    h.hooks.showFeed({...feedData(feedA,stories,queued),active_batch_id:'batch-a'});await h.advance(5000);
    batch={...batch,runs:queued.map((value,index)=>index<3?{...value,state:'completed'}:value)};h.hooks.acceptBatch(batch);
    await until(()=>decodes.length===3,'three complete API jobs reach decoding');assert.equal(h.hooks.state.images.size,0);assert.equal(h.elements.get('item-list').dataset.reveal,'holding');
    decodes[0].resolve();decodes[1].resolve();await until(()=>h.hooks.state.images.size===2,'two images decode');assert.equal(h.elements.get('item-list').dataset.reveal,'holding');
    decodes[2].resolve();await until(()=>h.elements.get('item-list').dataset.reveal==='revealing','third image starts the one-shot reveal');
    assert.equal(h.hooks.state.batchId,'batch-a');assert.equal(h.hooks.state.revealDuration,600);assert.equal(cards(h).length,10);assert.equal(cards(h).filter(card=>card.querySelector('img')).length,3);
    assert.equal(cards(h).filter(card=>card.querySelector('.image-state')).length,7);
    const readyAt=h.hooks.state.revealReadyAt;await h.advance(600);assert.equal(h.elements.get('item-list').dataset.reveal,'ready');assert.equal(h.elements.get('fun-loader').dataset.active,'false');
    batch={...batch,runs:batch.runs.map((value,index)=>index===3?{...value,state:'completed'}:value)};h.hooks.acceptBatch(batch);await until(()=>decodes.length===4,'remaining image continues independently');
    assert.equal(h.elements.get('item-list').dataset.reveal,'ready');assert.equal(h.elements.get('fun-loader').dataset.active,'false');decodes[3].resolve();await until(()=>h.hooks.state.images.size===4,'fourth image is ready');
    h.hooks.acceptBatch(batch);assert.equal(h.hooks.state.revealReadyAt,readyAt);assert.equal(h.elements.get('fun-loader').dataset.active,'false');assert.equal(posts(h).length,0);
  });
  await test('Fewer than three selected cards wait for their own decoded threshold, not unselected feed items', async () => {
    for(const selectedCount of [1,2]){
      const stories=Array.from({length:10},(_,index)=>item(`short-${index}`,`Story ${index}`)),selected=stories.slice(0,selectedCount).map((story,index)=>run(`selected-${index}`,story,1,'completed'));
      const decodes=[],h=setup({landing:true,read:async()=>new Blob(['saved']),decode:()=>{const pending=deferred();decodes.push(pending);return pending.promise;},fetch:()=>response({id:'batch-a',state:'active',runs:selected})});
      h.hooks.showFeed({...feedData(feedA,stories,selected),active_batch_id:'batch-a'});await until(()=>decodes.length===selectedCount,'selected images reach decode');
      for(let index=0;index<selectedCount;index++){decodes[index].resolve();await until(()=>h.hooks.state.images.size===index+1,'selected image decode settles');if(index+1<selectedCount)assert.equal(h.elements.get('item-list').dataset.reveal,'holding');}
      assert.equal(h.elements.get('item-list').dataset.reveal,'revealing');assert.equal(cards(h).length,selectedCount);assert.equal(h.hooks.state.batchId,'batch-a');await h.advance(350);assert.equal(h.elements.get('fun-loader').dataset.active,'false');assert.equal(posts(h).length,0);
    }
  });
  await test('Expired or hidden-navigation decode work releases its object URL and cannot publish a late image', async () => {
    for(const end of ['expiry','pagehide']){
      const pending=deferred(),h=setup({landing:true,read:async()=>new Blob(['saved']),decode:()=>pending.promise}),complete=run('decode-late',itemsA[0],1,'completed');
      h.hooks.showFeed(feedData(feedA,itemsA,[complete]));await until(()=>h.hooks.state.imageDecodes.size===1,'decode is pending');const url=[...h.hooks.state.imageDecodes.keys()][0];
      if(end==='expiry')h.hooks.expireView();else for(const callback of h.windowEvents.pagehide||[])callback();
      await until(()=>h.hooks.state.loadingImages.size===0,'canceled decode settles');pending.resolve();await Promise.resolve();
      assert.equal(h.hooks.state.images.size,0);assert.equal(h.hooks.state.imageDecodes.size,0);assert.equal(h.revoked.filter(value=>value===url).length,1);assert.equal(h.localWrites.length,0);assert.equal(h.requests.length,0);assert.equal(h.elements.get('fun-loader').dataset.active,'false');
    }
  });
  await test('Image decode failures and the bounded decode timeout expose recovery without counting an API completion', async () => {
    for(const timeout of [false,true]){
      const pending=deferred(),h=setup({landing:true,decode:()=>timeout?pending.promise:Promise.reject(Error('invalid image')),fetch:()=>response({},200,{'Content-Type':'image/webp'})});
      const complete=run('decode-error',itemsA[0],1,'completed');complete.availability='available_to_retrieve';h.hooks.showFeed(feedData(feedA,itemsA,[complete]));
      if(timeout){await until(()=>h.hooks.state.imageDecodes.size===1,'decode timeout starts');await h.advance(15000);}
      await until(()=>h.hooks.state.loadingImages.size===0,'decode failure settles');assert.equal(h.hooks.state.images.size,0);assert.equal(h.hooks.state.imageDecodes.size,0);assert.equal(h.localWrites.length,0);
      assert(text(h).includes('Load image again'));assert.equal(h.elements.get('fun-loader').dataset.active,'false');assert.equal(posts(h).length,0);
    }
  });
  await test('Browsers without decode wait for image load before satisfying the reveal threshold', async () => {
    const images=[],h=setup({landing:true,decodeSupported:false,imageLoad:image=>images.push(image),read:async()=>new Blob(['saved'])});
    h.hooks.showFeed(feedData(feedA,itemsA,[run('load-fallback',itemsA[0],1,'completed')]));await until(()=>images.some(image=>typeof image.onload==='function'),'fallback listener is ready');
    assert.equal(h.hooks.state.images.size,0);assert.equal(h.elements.get('item-list').dataset.reveal,'holding');const image=images.find(image=>typeof image.onload==='function');image.naturalWidth=1024;image.complete=true;image.onload();
    await until(()=>h.hooks.state.images.size===1,'fallback load satisfies readiness');assert.equal(h.elements.get('item-list').dataset.reveal,'revealing');assert.equal(h.requests.length,0);assert.equal(image.onload,null);assert.equal(image.onerror,null);
  });
  await test('Reduced motion keeps the overlay off after its three-image threshold even with a running batch', async () => {
    const stories=Array.from({length:4},(_,index)=>item(`reduced-${index}`,`Story ${index}`)),queued=stories.map((story,index)=>run(`reduced-${index}`,story));
    const h=setup({landing:true,reducedMotion:true,read:async()=>new Blob(['saved'])});h.hooks.showFeed({...feedData(feedA,stories,queued),active_batch_id:'batch-a'});
    assert.equal(h.elements.get('item-list').dataset.reveal,'ready');h.hooks.acceptBatch({id:'batch-a',state:'active',runs:queued.map((value,index)=>index<3?{...value,state:'completed'}:value)});
    await until(()=>h.hooks.state.images.size===3,'three reduced-motion images decode');assert.equal(h.elements.get('fun-loader').dataset.active,'false');assert.equal(h.hooks.state.revealReadyAt,null);
    h.hooks.updateControls();assert.equal(h.elements.get('fun-loader').dataset.active,'false');assert.equal(h.elements.get('item-list').inert,false);assert.equal(h.hooks.state.funTimer,null);assert.equal(posts(h).length,0);
  });
  await test('Successful polling after a connection error keeps content accessible without restoring its overlay or fade', async () => {
    let healthy=false;
    const queued=run('poll-recovery');
    const h=setup({landing:true,fetch:()=>healthy?response({id:'batch-a',state:'active',runs:[{...queued}]}):response({},503)});
    h.hooks.showFeed({...feedData(feedA,itemsA,[queued]),active_batch_id:'batch-a'});
    const list=h.elements.get('item-list'), loader=h.elements.get('fun-loader');
    assert.equal(list.dataset.reveal,'holding');assert.equal(loader.dataset.active,'true');
    await h.hooks.pollBatch();
    assert.equal(list.dataset.reveal,'ready');assert.equal(list.inert,false);assert.equal(loader.dataset.active,'false');
    assert.equal(h.hooks.state.batchId,'batch-a');assert.equal(h.hooks.state.revealReadyAt,null);
    healthy=true;await h.hooks.pollBatch();
    assert.equal(list.dataset.reveal,'ready');assert.equal(list.inert,false);
    assert.equal(loader.dataset.active,'false');assert.equal(loader.dataset.phase,'idle');
    await h.advance(500);await h.hooks.pollBatch();
    assert.equal(list.dataset.reveal,'ready');assert.equal(list.inert,false);
    assert.equal(h.hooks.state.revealReadyAt,null);assert.equal(h.hooks.state.loaderTimer,null);
    assert.equal(loader.dataset.active,'false');assert.equal(posts(h).length,0);
  });
  await test('Hidden reveal resumes from its absolute deadline and never restarts or prolongs the fade', async () => {
    for(const hiddenFor of [200,1000]) {
      const h=setup({landing:true,read:async()=>new Blob(['saved'])});const complete=run('cached',itemsA[0],1,'completed');
      h.hooks.showFeed(feedData(feedA,itemsA,[complete]));await until(()=>h.hooks.state.loadingImages.size===0,'cached restore settles');
      const readyAt=h.hooks.state.revealReadyAt;
      h.document.hidden=true;for(const callback of h.documentEvents.visibilitychange||[])callback();
      assert.equal(h.hooks.state.funTimer,null);assert.equal(h.hooks.state.loaderTimer,null);
      await h.advance(hiddenFor);h.document.hidden=false;for(const callback of h.documentEvents.visibilitychange||[])callback();
      assert.equal(h.hooks.state.revealReadyAt,readyAt);
      if(hiddenFor<350) {
        assert.equal(h.elements.get('item-list').dataset.reveal,'revealing');
        assert.equal(h.timers[h.hooks.state.loaderTimer-1].at,readyAt+350);
        await h.advance(149);assert.equal(h.elements.get('item-list').dataset.reveal,'revealing');
        await h.advance(1);
      }
      assert.equal(h.elements.get('item-list').dataset.reveal,'ready');assert.equal(h.elements.get('fun-loader').dataset.active,'false');
      for(const callback of h.windowEvents.pagehide||[])callback();assert.equal(h.hooks.state.funTimer,null);assert.equal(h.hooks.state.loaderTimer,null);
    }
  });
  await test('Expiry during image recovery clears decorative timers and rejects a late local blob', async () => {
    const pending=deferred();const late=setup({landing:true,read:()=>pending.promise});
    const complete=run('late-cache',itemsA[0],1,'completed');
    late.hooks.showFeed(feedData(feedA,itemsA,[complete]));await until(()=>late.localReads.length===1,'read starts');
    late.hooks.expireView();pending.resolve(new Blob(['late']));await until(()=>late.hooks.state.loadingImages.size===0,'late read settles');
    assert.equal(late.hooks.state.images.size,0);assert.equal(late.hooks.state.funTimer,null);assert.equal(late.hooks.state.loaderTimer,null);
    assert.equal(late.elements.get('fun-loader').dataset.active,'false');assert.equal(late.localWrites.length,0);
  });
  await test('Pending success and restored done requests keep their spinner active through navigation handoff', async () => {
    const pending=deferred();const h=pendingSetup({fetch:request=>request.url==='/api/session'?response({account:{id:'account-a'},csrf:'test-csrf'}):pending.promise});
    const start=h.hooks.start();await until(()=>posts(h).length===1,'pending POST starts');
    assert.equal(h.elements.get('fun-loader').dataset.active,'true');assert.equal(h.elements.get('fun-loader').dataset.phase,'working');
    pending.resolve(response(accepted()));await start;
    assert.equal(h.hooks.state.submitting,false);assert.equal(h.hooks.state.navigating,true);
    assert.equal(h.elements.get('fun-loader').dataset.active,'true');assert.equal(h.elements.get('fun-loader').dataset.phase,'working');
    await h.advance(1000);assert.equal(h.elements.get('fun-loader').dataset.active,'true');assert.equal(posts(h).length,1);
    const done=pendingSetup({sessionRecords:h.sessionRecords});await done.hooks.start();
    assert.equal(posts(done).length,0);assert.deepEqual(done.navigations,['/feeds/view-a']);
    assert.equal(done.elements.get('fun-loader').dataset.active,'true');assert.equal(done.elements.get('fun-loader').dataset.phase,'working');
  });
  await test('Downloaded blob is visible and revealed while a slow save remains pending; failed save retains explicit recovery', async () => {
    const save=deferred();let saves=0;
    const h=setup({landing:true,save:()=>++saves===1?save.promise:Promise.resolve(),fetch:()=>response({},200,{'Content-Type':'image/webp'})});
    const complete=run('downloaded',itemsA[0],1,'completed');complete.availability='available_to_retrieve';
    h.hooks.showFeed(feedData(feedA,itemsA,[complete]));await until(()=>h.localWrites.length===1,'download reaches local save');
    const image=h.hooks.state.images.get(complete.id);assert(image);assert.equal(image.saved,false);
    assert.equal(h.hooks.state.loadingImages.has(complete.id),true);
    assert(cards(h)[0].querySelector('img'));assert.equal(cards(h)[0].querySelector('img').src,image.url);
    assert.equal(h.elements.get('item-list').dataset.reveal,'revealing');await h.advance(350);
    assert.equal(h.elements.get('item-list').dataset.reveal,'ready');assert.equal(h.elements.get('fun-loader').dataset.active,'false');
    save.reject(Error('local disk unavailable'));await until(()=>h.hooks.state.loadingImages.size===0,'failed save settles');
    assert.equal(h.hooks.state.images.get(complete.id),image);assert.equal(image.saved,false);assert(image.saveError);
    assert(cards(h)[0].querySelector('img'));assert(text(h).includes('Retry save'));
    const button=h.elements.get('item-list').querySelectorAll('button').find(element=>element.textContent.includes('Retry save'));
    for(const callback of button.listeners.click||[])await callback();
    assert.equal(image.saved,true);assert.equal(h.localWrites.length,2);assert.equal(h.requests.length,1);assert.equal(posts(h).length,0);
  });
  await test('Off-view and superseded image work never blocks the currently visible illustration reveal', async () => {
    const oldRead=deferred();
    const h=setup({landing:true,read:(_account,_view,id)=>id==='old-image'?oldRead.promise:Promise.resolve(new Blob(['current']))});
    const old=run('old-image',itemsA[0],1,'completed');
    h.hooks.showFeed(feedData(feedA,itemsA,[old]));await until(()=>h.localReads.length===1,'old image restore starts');
    h.hooks.state.loadingImages.add('other-view-image');
    const current=run('current-image',itemsA[0],2,'completed');h.hooks.acceptRuns([current]);
    await until(()=>h.hooks.state.images.has(current.id),'current image is visible');
    assert(h.hooks.state.loadingImages.has(old.id));assert(h.hooks.state.loadingImages.has('other-view-image'));
    assert.equal(h.elements.get('item-list').dataset.reveal,'revealing');await h.advance(350);
    assert.equal(h.elements.get('item-list').dataset.reveal,'ready');assert.equal(h.elements.get('fun-loader').dataset.active,'false');
    assert.equal(cards(h)[0].dataset.runId,current.id);const url=h.hooks.state.images.get(current.id).url;
    oldRead.resolve(new Blob(['stale']));await until(()=>!h.hooks.state.loadingImages.has(old.id),'old image read settles');
    assert.equal(cards(h)[0].dataset.runId,current.id);assert.equal(h.hooks.state.images.get(current.id).url,url);
    assert.equal(h.hooks.state.images.has(old.id),false);assert.equal(h.requests.length,0);
  });
  await test('A failed older recovery cannot bypass the hold for the latest still-pending illustration', async () => {
    const oldRequest=deferred(), currentRequest=deferred();
    const h=setup({landing:true,fetch:request=>request.url.includes('/old-image/')?oldRequest.promise:currentRequest.promise});
    const old=run('old-image',itemsA[0],1,'completed');old.availability='available_to_retrieve';
    h.hooks.showFeed(feedData(feedA,itemsA,[old]));await until(()=>h.requests.length===1,'old image GET starts');
    const current=run('current-image',itemsA[0],2,'completed');current.availability='available_to_retrieve';
    h.hooks.acceptRuns([current]);await until(()=>h.requests.length===2,'latest image GET starts');
    await h.advance(4000);oldRequest.resolve(response({},503));
    await until(()=>!h.hooks.state.loadingImages.has(old.id),'older failed recovery settles');
    assert(h.hooks.state.loadingImages.has(current.id));assert.equal(h.hooks.state.revealBypass,false);
    assert.equal(h.elements.get('item-list').dataset.reveal,'holding');assert.equal(h.elements.get('item-list').inert,true);
    assert.equal(h.elements.get('fun-loader').dataset.active,'true');assert.equal(h.elements.get('fun-loader').dataset.phase,'working');
    assert.equal(cards(h)[0].dataset.runId,current.id);assert(!text(h).includes('Load image again'));
    currentRequest.resolve(response({},200,{'Content-Type':'image/webp'}));
    await until(()=>!h.hooks.state.loadingImages.has(current.id),'latest successful recovery settles');
    assert.equal(h.elements.get('item-list').dataset.reveal,'revealing');assert.equal(h.hooks.state.revealDuration,480);
    await h.advance(480);assert.equal(h.elements.get('item-list').dataset.reveal,'ready');
    assert.equal(posts(h).length,0);assert.equal(h.requests.length,2);
  });
  await test('Reduced motion bypasses cached cosmetic delay and failed/blocked/unknown runs keep original links accessible', async () => {
    const h=setup({landing:true,reducedMotion:true,read:async()=>new Blob(['saved'])});
    h.hooks.showFeed(feedData(feedA,itemsA,[run('saved',itemsA[0],1,'completed')]));await until(()=>h.hooks.state.loadingImages.size===0,'cached restore settles');
    assert.equal(h.elements.get('item-list').dataset.reveal,'ready');assert.equal(h.elements.get('item-list').inert,false);
    assert.equal(h.hooks.state.funTimer,null);assert.equal(h.hooks.state.loaderTimer,null);
    for(const state of ['failed','submission_unknown']) {
      const broken=setup({landing:true});broken.hooks.showFeed({...feedData(feedA,itemsA,[run('broken',itemsA[0],1,state)]),active_batch_id:'batch-broken'});
      assert.equal(broken.elements.get('item-list').dataset.reveal,'ready');assert.equal(broken.elements.get('item-list').inert,false);assert(text(broken).includes('Read original story'));
      assert.equal(broken.elements.get('fun-loader').dataset.active,'false');
    }
    const blocked=setup({landing:true});blocked.hooks.showFeed(feedData(feedA,itemsA,[],{status:'blocked'}));
    assert.equal(blocked.elements.get('item-list').dataset.reveal,'ready');assert.equal(blocked.elements.get('fun-loader').dataset.active,'false');
  });
  await test('Only selected slots render; failed/unavailable readers get compact recovery notices while admins retain details', async () => {
    const h=setup({landing:true});h.hooks.showFeed(feedData(feedA,itemsA,[run('selected')]));
    assert.equal(cards(h).length,1);assert(!text(h).includes(itemsA[1].title));
    const failed=run('selected',itemsA[0],1,'failed');failed.error={retryable:true,message:'internal',code:'MODEL_FAILED'};
    h.hooks.acceptRuns([failed]);assert.equal(cards(h).length,0);assert(text(h).includes('Retry illustration'));assert(!text(h).includes('MODEL_FAILED'));
    const admin=setup({landing:true,adminMode:true});admin.hooks.showFeed(feedData(feedA,itemsA,[failed]));
    assert.equal(cards(admin).length,1);assert.equal(admin.elements.get('item-list').querySelectorAll('details').length,1);
  });
  await test('Article excerpts remove only standalone terminal Comments boilerplate and preserve legitimate prose', async () => {
    const h=setup({landing:true});
    for(const [input,expected] of [['Comments',''],[' COMMENTS https://example.org/comments',''],['A story. Comments','A story.'],
      ['A story! comments https://example.org/comments','A story!'],['A story\nComments','A story'],['We welcome comments.','We welcome comments.'],
      ['Officials declined Comments','Officials declined Comments'],['Comments explain this essay','Comments explain this essay'],['A story. Comments continue here','A story. Comments continue here']]) {
      assert.equal(h.hooks.readerExcerpt(input),expected);
      const story={...itemsA[0],excerpt:input};h.hooks.showFeed(feedData(feedA,[story],[run('selected',story)]));
      const paragraph = cards(h)[0].querySelector('.article-excerpt');
      if (expected) assert.equal(paragraph.textContent,expected);
      else assert.equal(paragraph,null);
    }
  });
  await test('Missing descriptions omit empty paragraphs while long descriptions remain literal and complete', async () => {
    const h=setup({landing:true});
    for(const excerpt of ['', '   ', undefined, null, 'Comments https://example.org/comments']) {
      const story={...itemsA[0],excerpt};h.hooks.showFeed(feedData(feedA,[story],[run('selected',story)]));
      const card=cards(h)[0];assert.equal(card.querySelector('.article-excerpt'),null);
      assert(card.querySelector('a.reader-story-cta'));assert(card.querySelector('.image-area'));
    }
    const excerpt='<img src=x onerror=alert(1)> '+ 'A detailed source description. '.repeat(120);
    const story={...itemsA[0],excerpt};h.hooks.showFeed(feedData(feedA,[story],[run('selected',story)]));
    const paragraph=cards(h)[0].querySelector('.article-excerpt');
    assert.equal(paragraph.textContent,excerpt.trimEnd());assert.equal(paragraph.children.length,0);
    assert.equal(posts(h).length,0);
  });
  await test('Reader date/source CTA are deterministic and safe; image cards expose no download action', async () => {
    const h=setup({landing:true});const story=item('safe','Readable story','2026-10-15T00:30:00+02:00');
    const complete=run('saved',story,1,'completed');h.hooks.state.images.set('saved',{url:'blob:saved',saved:true});
    h.hooks.showFeed(feedData(feedA,[story],[complete]));
    const card=cards(h)[0];assert.equal(card.querySelector('time').textContent,'14 Oct 2026');
    const source=card.querySelector('a.article-source');assert.equal(source.href,'https://example.org');assert.equal(source.rel,'noopener noreferrer');
    assert(card.querySelector('a.reader-story-cta').classList.contains('button-dark'));
    assert.equal(card.querySelectorAll('a').some(a=>a.download || /download/i.test(a.textContent)),false);
    assert.equal(h.hooks.readerDate('2026-10-15T00:30:00'),'15 Oct 2026');assert.equal(h.hooks.readerDate('bad date'),'');assert.equal(h.hooks.readerDate('2026-02-30'),'');
    assert.equal(h.hooks.readerDate('Wed, 14 Oct 2026 23:30:00 EST'),'15 Oct 2026');
    assert.equal(h.hooks.readerDate('Wed, 14 Oct 2026 23:30:00 PST'),'15 Oct 2026');
    assert.equal(h.hooks.safeSource('https://user:pass@example.org/story'),null);assert.equal(h.hooks.safeSource('https://example.org/\nsecret'),null);
  });
  console.log(JSON.stringify({result: failed.length ? 'FAIL' : 'PASS', tests: checked.length + failed.length, passed: checked.length, failed: failed.length, failures: failed}, null, 2));
  if (failed.length) process.exitCode = 1;
})().catch(error => {console.error(error); process.exitCode = 1;});
