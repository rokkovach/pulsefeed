/* Exercise the browser reconciliation branch with DOM-shaped nodes, not its fallback. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Text {
  constructor(value) { this.nodeType = 3; this.nodeName = '#text'; this.nodeValue = value; this.parentNode = null; }
  get textContent() { return this.nodeValue; }
}
class Element {
  constructor(tag = 'div') {
    this.nodeType = 1; this.nodeName = this.tagName = tag.toUpperCase();
    this.childNodes = []; this.attrs = new Map(); this.listeners = {}; this.writes = []; this.parentNode = null;
    this.dataset = new Proxy({}, {
      get: (_, key) => this.getAttribute('data-' + String(key).replace(/[A-Z]/g, c => '-' + c.toLowerCase())) ?? undefined,
      set: (_, key, value) => { this.setAttribute('data-' + String(key).replace(/[A-Z]/g, c => '-' + c.toLowerCase()), value); return true; }
    });
  }
  get attributes() { return [...this.attrs].map(([name, value]) => ({name, value})); }
  get children() { return this.childNodes.filter(child => child.nodeType === 1); }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get textContent() { return this.childNodes.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(new Text(String(value))); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(value) { if (value) this.setAttribute('disabled', ''); else this.removeAttribute('disabled'); }
  get isConnected() { return Boolean(this.parentNode); }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  hasAttribute(name) { return this.attrs.has(name); }
  setAttribute(name, value) { this.writes.push([name, String(value)]); this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  append(...children) { for (const child of children) this.insertBefore(typeof child === 'string' ? new Text(child) : child, null); }
  insertBefore(child, before) {
    if (child === before) return child;
    if (child.parentNode) child.parentNode.removeChild(child);
    const index = before ? this.childNodes.indexOf(before) : this.childNodes.length;
    assert(index >= 0); this.childNodes.splice(index, 0, child); child.parentNode = this; return child;
  }
  removeChild(child) { const index = this.childNodes.indexOf(child); assert(index >= 0); this.childNodes.splice(index, 1); child.parentNode = null; return child; }
  replaceChildren(...children) { for (const child of [...this.childNodes]) this.removeChild(child); this.append(...children); }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  close() {}
}
const elements = new Map();
const document = {getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  createElement: tag => new Element(tag), querySelectorAll: () => [], body: new Element('body')};
document.getElementById('bootstrap').textContent = JSON.stringify({account: {id: 'a'}, csrf: 'csrf', feeds: []});
const context = {document, window: {addEventListener() {}}, console, AbortController, URL, Blob, setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {}};
const source = fs.readFileSync(path.join(__dirname, '../app/pulsefeed/static/app.js'), 'utf8')
  .replace(/^  start\(\);$/m, '  window.hooks = {reconcileChildren, bindAction, state};');
vm.createContext(context); vm.runInContext(source, context);
const hooks = context.window.hooks; hooks.state.account = {id: 'a'};
function element(tag, className, text) { const node = new Element(tag); if (className) node.className = className; if (text) node.textContent = text; return node; }
function card(id, callback) {
  const article = element('article', 'reader-card'); article.dataset.itemId = id;
  const area = element('div', 'image-area'); const image = element('img'); image.setAttribute('src', 'blob:saved-' + id); area.append(image);
  const body = element('div', 'card-body'); const button = element('button', 'button', 'Retry save'); hooks.bindAction(button, callback); body.append(button);
  article.append(area, body); return {article, image, button, body};
}
let tests = 0;
async function check(name, callback) { await callback(); tests++; console.log('PASS ' + name); }
(async () => {
  await check('reconciliation preserves existing article, decoded image and interactive node identities', async () => {
    const root = element('div'); const old = card('a', () => {}); root.append(old.article);
    const beforeWrites = old.image.writes.length;
    hooks.reconcileChildren(root, [card('a', () => {}).article]);
    assert.equal(root.children[0], old.article); assert.equal(old.article.children[0].children[0], old.image);
    assert.equal(old.body.children[0], old.button); assert.equal(old.image.writes.length, beforeWrites, 'Unchanged image src must not be reassigned');
  });
  await check('a retained action invokes the latest callback exactly once after repeated updates', async () => {
    const calls = []; const root = element('div'); const old = card('a', () => calls.push('old')); root.append(old.article);
    hooks.reconcileChildren(root, [card('a', () => calls.push('middle')).article]);
    hooks.reconcileChildren(root, [card('a', () => calls.push('latest')).article]);
    assert.equal(old.button.listeners.click.length, 1);
    for (const callback of old.button.listeners.click) await callback();
    assert.deepEqual(calls, ['latest']); assert.equal(old.button.disabled, false);
  });
  await check('source reordering keeps keyed articles and removes stale cards and whitespace', async () => {
    const root = element('div'); const a = card('a', () => {}), b = card('b', () => {}), gone = card('gone', () => {});
    root.append('\n', a.article, '\n', b.article, '\n', gone.article);
    hooks.reconcileChildren(root, [card('b', () => {}).article, card('a', () => {}).article]);
    assert.deepEqual(root.children, [b.article, a.article]); assert.equal(root.childNodes.length, 2); assert.equal(gone.article.parentNode, null);
  });
  await check('adding or omitting a description keeps the decoded image and source action mounted', async () => {
    const root = element('div'), old = card('a', () => {}); root.append(old.article);
    const described = card('a', () => {});
    described.body.insertBefore(element('p', 'article-excerpt', 'A source description.'), described.button);
    hooks.reconcileChildren(root, [described.article]);
    assert.equal(old.article.children[0].children[0], old.image); assert.equal(old.body.children[1], old.button);
    hooks.reconcileChildren(root, [card('a', () => {}).article]);
    assert.deepEqual(old.body.children, [old.button]); assert.equal(old.article.children[0].children[0], old.image);
  });
  await check('changed text and status attributes update while stable ancestors remain mounted', async () => {
    const root = element('div'); const old = element('p', 'save-status warning', 'Saving'); old.setAttribute('aria-busy', 'true'); root.append(old);
    const fresh = element('p', 'save-status', 'Saved in this browser'); hooks.reconcileChildren(root, [fresh]);
    assert.equal(root.children[0], old); assert.equal(old.textContent, 'Saved in this browser');
    assert.equal(old.className, 'save-status'); assert.equal(old.hasAttribute('aria-busy'), false);
  });
  console.log(JSON.stringify({result: 'PASS', tests}));
})().catch(error => {console.error(error); process.exitCode = 1;});
