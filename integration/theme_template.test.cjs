const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const path = require('path').resolve(__dirname, '../app/pulsefeed/static') + require('path').sep;
let passed = 0;
function test(name, fn) { fn(); passed++; console.log(`PASS ${name}`); }
function themeHarness({saved=null,dark=false,blocked=false}={}) {
  const attrs={};const icon={};const handlers={};const events={};const writes=[];
  const button={setAttribute(k,v){attrs[k]=v;},querySelector(){return icon;},addEventListener(k,v){handlers[k]=v;}};
  const media={matches:dark,addEventListener(k,v){events.system=v;}};
  const document={documentElement:{dataset:{}},readyState:'loading',querySelectorAll(){return [button];},addEventListener(k,v){events[k]=v;}};
  const context={document,window:{matchMedia:()=>media,addEventListener(k,v){events[k]=v;}},localStorage:{getItem(){if(blocked)throw Error('blocked');return saved;},setItem(k,v){if(blocked)throw Error('blocked');writes.push([k,v]);}}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path+'theme.js','utf8'),context);
  return {document,events,handlers,attrs,media,writes};
}
test('system dark is applied synchronously before DOM readiness',()=>{const h=themeHarness({dark:true});assert.equal(h.document.documentElement.dataset.theme,'dark');assert.equal(h.attrs['aria-pressed'],'true');});
test('saved light preference overrides dark system and survives system changes',()=>{const h=themeHarness({saved:'light',dark:true});h.media.matches=false;h.events.system();h.media.matches=true;h.events.system();assert.equal(h.document.documentElement.dataset.theme,'light');});
test('accessible toggle persists explicit preference and updates all controls',()=>{const h=themeHarness();h.events.DOMContentLoaded();h.handlers.click();assert.equal(h.document.documentElement.dataset.theme,'dark');assert.equal(h.attrs['aria-pressed'],'true');assert.deepEqual(h.writes,[['pulsefeed-theme','dark']]);h.handlers.click();assert.equal(h.attrs['aria-pressed'],'false');});
test('blocked storage leaves functional per-page theme toggle',()=>{const h=themeHarness({blocked:true});h.events.DOMContentLoaded();h.handlers.click();assert.equal(h.document.documentElement.dataset.theme,'dark');h.media.matches=false;h.events.system();assert.equal(h.document.documentElement.dataset.theme,'dark');});
test('system changes apply only without explicit preference',()=>{const h=themeHarness({saved:'invalid'});h.media.matches=true;h.events.system();assert.equal(h.document.documentElement.dataset.theme,'dark');});
test('other tab theme updates synchronize and clear resets system choice',()=>{const h=themeHarness({saved:'light'});h.events.storage({key:'pulsefeed-theme',newValue:'dark'});assert.equal(h.document.documentElement.dataset.theme,'dark');h.events.storage({key:'pulsefeed-theme',newValue:null});assert.equal(h.document.documentElement.dataset.theme,'light');});
function declarations(css,selector) {
  const escaped=selector.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  return [...css.matchAll(new RegExp('(?:^|[{}])\\s*'+escaped+'\\s*\\{([^}]+)\\}','g'))]
    .map(match=>Object.fromEntries(match[1].split(';').filter(part=>part.includes(':')).map(part=>{
      const colon=part.indexOf(':');return [part.slice(0,colon).trim(),part.slice(colon+1).trim()];
    })));
}
function color(css,selector,property='color') {
  const raw=declarations(css,selector).map(rule=>rule[property]).filter(Boolean).at(-1);
  const variable=raw?.match(/^var\((--[^)]+)\)$/);
  return variable?color(css,':root',variable[1]):raw;
}
function rgb(hex) { assert.match(hex,/^#[0-9a-f]{6}$/i);return hex.slice(1).match(/../g).map(value=>parseInt(value,16)); }
function contrast(foreground,background) {
  const luminance=channels=>channels.map(value=>{
    const channel=value/255;return channel<=.04045?channel/12.92:((channel+.055)/1.055)**2.4;
  }).reduce((sum,channel,index)=>sum+channel*[.2126,.7152,.0722][index],0);
  const a=luminance(foreground),b=luminance(background);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
}
const stylesheets=['tailwind.input.css','app.css'].map(name=>[name,fs.readFileSync(path+name,'utf8')]);
test('small reader and usage labels meet text contrast in source and shipped CSS',()=>{
  for(const [name,css] of stylesheets) {
    const background=rgb(color(css,':root','--tile'));
    for(const selector of ['.article-date','.article-source','.usage-outcomes dt']) {
      const ratio=contrast(rgb(color(css,selector)),background);
      assert(ratio>=4.5,`${name} ${selector}: ${ratio.toFixed(2)}:1`);
    }
  }
});
test('footer text keeps contrast over paper, static grid and the strongest ambient glow',()=>{
  const blend=(background,foreground,opacity)=>background.map((channel,index)=>channel*(1-opacity)+foreground[index]*opacity);
  for(const [name,css] of stylesheets) {
    const foreground=rgb(color(css,'.page-footer'));
    const paper=rgb(color(css,':root','--paper'));
    const pseudo=css.includes('body::before')?'::':':';
    const opacity=selector=>Math.max(...declarations(css,selector).map(rule=>parseFloat(rule.opacity)||0));
    const grid=blend(paper,rgb(color(css,':root','--grid-ink')),opacity(`body${pseudo}before`));
    const backgrounds=[paper,grid,...['--grid-glow','--grid-glow-warm'].map(variable=>
      blend(grid,rgb(color(css,':root',variable)),opacity(`body${pseudo}after`)))];
    for(const background of backgrounds) {
      const ratio=contrast(foreground,background);
      assert(ratio>=4.5,`${name} footer: ${ratio.toFixed(2)}:1`);
    }
  }
});
class Element {
  constructor(){this.value='';this.listeners={};this.attributes={};this.classList={toggle(){}};this.selectionStart=0;this.selectionEnd=0;}
  addEventListener(k,v){this.listeners[k]=v;}
  setCustomValidity(v){this.validity=v;}
  setAttribute(k,v){this.attributes[k]=v;}
  setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}
  focus(){}
  closest(){return this.section||null;}
  checkValidity(){return !this.validity;}
  reportValidity(){this.reported=true;this.openAtReport=this.section?.open;}
}
const elements=new Map();const document={getElementById(id){if(id==='qwen-prompt-editor')return null;if(!elements.has(id))elements.set(id,new Element());return elements.get(id);}};
const field=document.getElementById('flux-prefix');field.value='before {{qwen_prompt}} after';
field.section=document.getElementById('flux-settings-section');
const context={document,window:{},console};vm.createContext(context);
const source=fs.readFileSync(path+'settings.js','utf8').replace('  render();\n})();','  render(); window.hooks={moveToken,tokenCount,staticLength};\n})();');
vm.runInContext(source,context);const hooks=context.window.hooks;const marker='{{qwen_prompt}}';
test('token moves to beginning, middle and end preserving style text',()=>{
  assert.equal(hooks.moveToken('A '+marker+' Z',0).value,marker+'A  Z');
  assert.equal(hooks.moveToken('A '+marker+' Z',('A '+marker+' Z').length).value,'A  Z'+marker);
  assert.equal(hooks.moveToken('ABCD'+marker,2).value,'AB'+marker+'CD');
});
test('missing and duplicate markers repair to exactly one; cursor inside marker is safe',()=>{
  assert.equal(hooks.tokenCount(hooks.moveToken('A'+marker+'B'+marker+'C',3).value),1);
  assert.equal(hooks.moveToken('AB',1).value,'A'+marker+'B');
  assert.equal(hooks.moveToken('A'+marker+'B',4).value,'A'+marker+'B');
});
test('static character budget excludes marker and counts Unicode code points',()=>{assert.equal(hooks.staticLength('🌱'+marker+'🍑'),2);});
test('composition preview renders literal before and after text safely',()=>{
  field.value='<b>'+marker+'</b>';field.listeners.input();
  assert.equal(document.getElementById('flux-preview-before').textContent,'<b>');
  assert.equal(document.getElementById('flux-preview-after').textContent,'</b>');assert.equal(field.validity,'');
});
test('native textarea token drop removes old marker at new caret position',()=>{
  field.value='before '+marker+' after';
  field.listeners.drop({dataTransfer:{getData:()=> 'qwen'}});
  field.value+=marker;field.selectionEnd=field.value.length;field.listeners.input();
  assert.equal(field.value,'before  after'+marker);assert.equal(hooks.tokenCount(field.value),1);
});
test('invalid templates block save and expose corrective validation',()=>{
  field.value='No token here';field.section.open=false;let prevented=false;
  document.getElementById('flux-settings-form').listeners.submit({preventDefault(){prevented=true;},stopImmediatePropagation(){}});
  assert(prevented);assert(field.reported);assert(field.openAtReport);assert.equal(field.attributes['aria-invalid'],'true');
  field.value='🌱'.repeat(2000)+marker;field.listeners.input();assert.equal(field.validity,'');
  field.value='x'.repeat(2001)+marker;field.listeners.input();assert(field.validity.includes('2,000'));
});
test('native invalid capture reveals only the owning prompt disclosure without changing its draft',()=>{
  const qwen=document.getElementById('qwen-template');qwen.section=document.getElementById('qwen-settings-section');qwen.value='';qwen.section.open=false;field.section.open=false;
  document.getElementById('qwen-settings-form').listeners.invalid({target:qwen});
  assert(qwen.section.open);assert.equal(field.section.open,false);assert.equal(qwen.value,'');
});
console.log(JSON.stringify({result:'PASS',tests:passed}));
