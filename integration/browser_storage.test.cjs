/* Execute the real IndexedDB boundary with transaction, clock and cross-tab doubles. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = fs.readFileSync(path.join(__dirname, '../app/pulsefeed/static/image-store.js'), 'utf8');
async function until(predicate) { for (let i = 0; i < 40 && !predicate(); i++) await Promise.resolve(); assert(predicate(), 'Expected asynchronous boundary was not reached'); }
function fixture(seed = [], {holdWrites = false, holdReads = false, upgrade = false} = {}) {
  const records = new Map(seed.map(record => [record.key, {...record}]));
  const transactions = [], reads = [], requests = [];
  let indexExists = !upgrade, upgradeDone;
  function cursorRequest(transaction, sorted, onDone) {
    const request = {}; let index = 0;
    const next = () => queueMicrotask(() => {
      const record = sorted[index++]; let continued = false;
      request.result = record ? {value: {...record}, delete() {transaction.staged.set(record.key, null);}, update(value) {transaction.staged.set(value.key, {...value});}, continue() {continued = true; next();}} : null;
      request.onsuccess?.();
      if (!continued) {transaction.pending--; transaction.schedule(); onDone?.();}
    });
    next(); return request;
  }
  function transaction(mode = 'readwrite', upgrading = false) {
    const tx = {mode, pending: 0, staged: new Map(), settled: false, hasPut: false, scheduled: false,
      abort() {if (this.settled) return; this.settled = true; this.onabort?.();},
      commit() {if (this.settled) return; this.settled = true; for (const [key, value] of this.staged) value === null ? records.delete(key) : records.set(key, value); this.oncomplete?.();},
      schedule() {if (this.scheduled) return; this.scheduled = true; queueMicrotask(() => {this.scheduled = false; if (!this.pending && !(holdWrites && this.hasPut)) this.commit();});},
      objectStore() {return {
        indexNames: {contains: () => indexExists}, createIndex() {indexExists = true;},
        put(value) {tx.hasPut = true; tx.staged.set(value.key, {...value}); tx.schedule();},
        delete(key) {tx.staged.set(key, null); tx.schedule();},
        get(key) {const request = {}; tx.pending++; const done = () => {request.result = records.get(key); request.onsuccess?.(); tx.pending--; tx.schedule();}; reads.push({key, done}); if (!holdReads) queueMicrotask(done); return request;},
        openCursor() {tx.pending++; return cursorRequest(tx, [...records.values()], upgrading ? () => {tx.commit(); upgradeDone?.();} : null);},
        index() {return {openCursor() {
          tx.pending++; const sorted = [...records.values()].filter(record => Number.isFinite(record.expiresAt)).sort((a, b) => a.expiresAt - b.expiresAt);
          const request = {}; let index = 0, continued;
          const next = () => queueMicrotask(() => {
            const record = sorted[index++]; continued = false;
            request.result = record ? {value: {...record}, delete() {tx.staged.set(record.key, null);}, continue() {continued = true; next();}} : null;
            request.onsuccess?.(); if (!continued) {tx.pending--; tx.schedule();}
          }); next(); return request;
        }};}
      };}
    }; transactions.push(tx); return tx;
  }
  const db = {closed: 0, objectStoreNames: {contains: () => true}, transaction(_name, mode) {return transaction(mode);}, close() {this.closed++;}};
  const indexedDB = {open(name, version) {
    assert.equal(name, 'pulsefeed-images'); assert.equal(version, 2);
    const request = {result: db}; requests.push(request);
    queueMicrotask(() => {
      if (upgrade && requests.length === 1) {request.transaction = transaction('readwrite', true); upgradeDone = () => request.onsuccess(); request.onupgradeneeded();}
      else request.onsuccess();
    }); return request;
  }};
  return {records, db, indexedDB, transactions, reads, requests};
}
function setup(database = fixture(), timing = null) {
  let wall = 0, serial = 0; const timers = new Map(), channels = [];
  class Clock extends Date {static now() {return wall;}}
  class Channel {constructor() {channels.push(this);} postMessage(data) {for (const peer of channels) if (peer !== this) peer.onmessage?.({data});}}
  const context = {document: {getElementById: () => null}, window: {indexedDB: database.indexedDB, BroadcastChannel: Channel}, Date: Clock, console,
    setTimeout(callback, delay) {timers.set(++serial, {callback, delay}); return serial;}, clearTimeout(id) {timers.delete(id);}};
  if (timing) context.window.performance = {now: () => timing.scriptNow + wall, getEntriesByType: () => [timing]};
  vm.createContext(context); vm.runInContext(script, context);
  const create = (serverNow = 1000) => new context.window.PulseImageStore(serverNow);
  return {store: create(), create, context, timers, channels, database, advance(ms) {wall += ms;}};
}
const view = (id = 'view-a', expires_at = 4600) => ({id, created_at: 1000, expires_at});
let tests = 0;
async function check(name, run) {await run(); tests++; console.log(`PASS ${name}`);}
(async () => {
  await check('committed copies use account:view:run keys, contain no credentials, and saves never extend the fixed expiry', async () => {
    const h = setup(); const blob = new Blob(['image']); const a = view();
    await h.store.save('account-a', a, 'run', blob); h.advance(30000); await h.store.save('account-a', a, 'run', blob);
    const record = h.database.records.get('account-a:view-a:run'); assert(record); assert.equal(record.expiresAt, 4600000); assert.equal(record.blob, blob);
    assert.deepEqual(Object.keys(record).sort(), ['accountId', 'blob', 'expiresAt', 'key', 'runId', 'savedAt', 'viewId']);
  });
  await check('account and view partitions isolate the same run without deleting another live view', async () => {
    const h = setup(); const a = new Blob(['a']), b = new Blob(['b']);
    await h.store.save('account-a', view('a', 1010), 'same', a); await h.store.save('account-a', view('b'), 'same', b);
    assert.equal(await h.store.read('account-b', view('b'), 'same'), null);
    assert.equal(await h.store.read('account-a', view('b'), 'same'), b);
    h.advance(11000); await h.store.maintain(); assert.equal(h.database.records.has('account-a:a:same'), false);
    assert.equal(await h.store.read('account-a', view('b'), 'same'), b);
  });
  await check('global expiry sweep deletes expired copies across accounts and feeds while retaining still-live data', async () => {
    const h = setup(fixture([{key: 'old-a', expiresAt: 999000}, {key: 'old-b', expiresAt: 1000000}, {key: 'live', expiresAt: 1300000}]));
    await h.store.sweep(); assert.deepEqual([...h.database.records.keys()], ['live']); assert.equal(h.store.nextRecordExpiry, 1300000);
  });
  await check('v1 migration keeps valid legacy data only until its original savedAt + one hour, without making it readable in new views', async () => {
    const h = setup(fixture([{key: 'a:old', savedAt: 900000, blob: new Blob(['legacy'])}, {key: 'a:expired', savedAt: -3000000}, {key: 'a:undated'}], {upgrade: true}));
    await h.store.sweep(); assert.equal(h.database.records.get('a:old').expiresAt, 4500000);
    assert.equal(h.database.records.has('a:expired'), false); assert.equal(h.database.records.has('a:undated'), false);
    assert.equal(await h.store.read('a', view(), 'old'), null);
  });
  await check('refresh/restart uses the absolute server deadline rather than renewing a browser copy’s lifetime', async () => {
    const h = setup(); await h.store.save('a', view('short', 1020), 'run', new Blob(['image']));
    const reloaded = h.create(1021); await reloaded.maintain(); assert.equal(h.database.records.size, 0);
    await assert.rejects(reloaded.read('a', view('short', 1020), 'run'), /one-hour/);
  });
  await check('a delayed document or deferred script cannot revive already-expired cached images, even with a skewed wall clock', async () => {
    const db = fixture([{key: 'a:short:run', accountId: 'a', viewId: 'short', expiresAt: 1100000, blob: new Blob(['expired'])}]);
    const h = setup(db, {requestStart: 0, responseStart: 1000, scriptNow: 120000});
    assert.equal(h.store.now(), 1120000); assert.equal(h.store.live(view('short', 1100)), false);
    await assert.rejects(h.store.read('a', view('short', 1100), 'run'), /one-hour/); assert.equal(db.reads.length, 0);
    await h.store.maintain(); assert.equal(db.records.size, 0);
    h.advance(10000); assert.equal(h.store.now(), 1130000);
  });
  await check('navigation timing fallbacks stay conservative, while pages without a server snapshot use their current wall clock', async () => {
    const h = setup(fixture(), {responseStart: 2000, scriptNow: 7000}); assert.equal(h.store.now(), 1005000);
    h.context.window.performance.getEntriesByType = () => [{requestStart: 8000, responseStart: 2000}];
    assert.equal(h.create().now(), 1005000);
    h.context.window.performance.getEntriesByType = () => [];
    assert.equal(h.create().now(), 1007000);
    const local = new h.context.window.PulseImageStore(null); assert.equal(local.now(), 0);
  });
  await check('nearest-expiry timer fires watchers once even when no image has been saved', async () => {
    const h = setup(); let expired = 0, live = 0;
    h.store.watch(view('short', 1005), () => expired++); h.store.watch(view('long'), () => live++);
    const timer = [...h.timers.values()][0]; assert.equal(timer.delay, 5000); h.advance(5000); await timer.callback();
    h.store.checkExpired(); assert.equal(expired, 1); assert.equal(live, 0);
  });
  await check('a read completing after expiry cannot return its blob', async () => {
    const db = fixture([{key: 'a:short:run', accountId: 'a', viewId: 'short', expiresAt: 1005000, blob: new Blob(['image'])}], {holdReads: true});
    const h = setup(db); const pending = h.store.read('a', view('short', 1005), 'run');
    await until(() => db.reads.length === 1); h.advance(6000); db.reads[0].done(); assert.equal(await pending, null);
  });
  await check('a save committed after expiry is deleted and rejected rather than resurrecting an expired copy', async () => {
    const db = fixture([], {holdWrites: true}), h = setup(db);
    const pending = h.store.save('a', view('short', 1005), 'run', new Blob(['image']));
    await until(() => db.transactions.some(tx => tx.hasPut)); h.advance(6000); db.transactions.find(tx => tx.hasPut).commit();
    await assert.rejects(pending, /one-hour/); assert.equal(db.records.size, 0);
  });
  await check('expiry aborts a pending write and a later commit cannot restore it', async () => {
    const db = fixture([], {holdWrites: true}), h = setup(db); const a = view('short', 1005);
    const pending = h.store.save('a', a, 'run', new Blob(['image'])); await until(() => db.transactions.some(tx => tx.hasPut));
    h.advance(6000); h.store.checkExpired(); await assert.rejects(pending, /expired/);
    db.transactions.find(tx => tx.hasPut).commit(); assert.equal(db.records.size, 0);
  });
  await check('cross-tab cleanup expires the same view in another tab while preserving its unrelated live copy', async () => {
    const h = setup(); const other = h.create(); let expired = 0;
    other.watch(view('short', 1005), () => expired++);
    await h.store.save('a', view('short', 1005), 'run', new Blob(['a'])); await other.save('a', view('long'), 'run', new Blob(['b']));
    h.advance(6000); await h.store.maintain(); await until(() => expired === 1);
    assert(h.database.records.has('a:long:run')); assert.equal(h.database.records.has('a:short:run'), false);
  });
  await check('a blocked upgrade rejects cleanly and closes a late connection without losing expiry enforcement', async () => {
    const h = setup(); const request = {}, db = {closed: false, close() {this.closed = true;}};
    h.context.window.indexedDB = {open() {return request;}};
    const pending = h.store.open(); request.onblocked(); await assert.rejects(pending, /older PulseFeed tabs/);
    request.result = db; request.onsuccess(); assert.equal(db.closed, true); assert.equal(h.store.db, null);
    let expired = false; h.store.watch(view('short', 1005), () => {expired = true;}); h.advance(6000); h.store.checkExpired(); assert(expired);
  });
  await check('shared cleanup removes obsolete feed preferences only, preserving theme, auth, and unrelated entries', async () => {
    const h = setup(); const values = new Map([['pulsefeed:selected-feed:a', 'old'], ['pulsefeed:selected-feed:b', 'old2'], ['pulsefeed-theme', 'dark'], ['session', 'preserve'], ['other', 'preserve']]);
    h.context.window.localStorage = {get length() {return values.size;}, key: index => [...values.keys()][index], removeItem: key => values.delete(key)};
    await h.store.maintain(); assert.deepEqual([...values.keys()], ['pulsefeed-theme', 'session', 'other']);
  });
  console.log(JSON.stringify({result: 'PASS', tests}));
})().catch(error => {console.error(error); process.exitCode = 1;});
