/* Single native editor serializes two separate model roles; no browser/model calls. */
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const title='{{article_title}}',excerpt='{{article_excerpt}}',divider='--- ARTICLE CONTEXT ---';
const defaultArticle=`Article data:\n{"title": ${title}, "excerpt": ${excerpt}}`;
const template=fs.readFileSync(path.join(__dirname,'../app/pulsefeed/templates/prompt_settings.html'),'utf8');
class Element{
 constructor(tag='div'){this.tagName=tag.toUpperCase();this._value='';this.children=[];this.listeners={};this.attributes={};this.selectionStart=0;this.selectionEnd=0;this.validity='';this.text='';this.hidden=false;this.classList={toggle(){}};}
 set value(value){this._value=this.tagName==='TEXTAREA'?String(value).replace(/\r\n?/g,'\n'):String(value);}get value(){return this._value;}
 set innerHTML(value){throw Error('Raw HTML must never be interpreted');}set textContent(value){this.text=String(value);this.children=[];}get textContent(){return this.text+this.children.map(child=>child.textContent).join('');}
 append(...children){this.children.push(...children);}replaceChildren(...children){this.text='';this.children=children;}
 addEventListener(type,callback){(this.listeners[type]||=[]).push(callback);}setCustomValidity(value){this.validity=value;}setAttribute(key,value){this.attributes[key]=String(value);}
 setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}focus(){this.focused=true;}closest(){return this.section;}checkValidity(){return !this.validity;}reportValidity(){this.reported=true;this.openAtReport=this.section?.open;}
}
function setup(article=defaultArticle,system='SYSTEM INSTRUCTIONS'){
 const elements=new Map();for(const match of template.matchAll(/<([a-z]+)\b[^>]*\bid="([\w-]+)"[^>]*>/g))elements.set(match[2],new Element(match[1]));
 const field=elements.get('qwen-prompt-editor');field.section=elements.get('qwen-settings-section');
 elements.get('qwen-editor-data').textContent=JSON.stringify({system,article});elements.get('qwen-template').value=system;elements.get('qwen-article-template').value=article;
 elements.get('flux-prefix').value='Style {{qwen_prompt}}';elements.get('flux-prefix').section=elements.get('flux-settings-section');
 const context={document:{getElementById:id=>elements.get(id)||null,createElement:tag=>new Element(tag)},window:{},console};vm.createContext(context);
 const script=fs.readFileSync(path.join(__dirname,'../app/pulsefeed/static/settings.js'),'utf8').replace('  render();\n})();','  render(); window.hooks={joinQwen,splitQwen};\n})();');vm.runInContext(script,context);
 function dispatch(id,type,extra={}){const event={target:elements.get(id),prevented:false,stopped:false,preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...extra};for(const fn of elements.get(id).listeners[type]||[]){fn(event);if(event.stopped)break;}return event;}
 function edit(value){field.value=value;dispatch('qwen-prompt-editor','input');}
 function add(which,cursor=field.value.length){field.selectionStart=cursor;dispatch(`add-article-${which}`,'click');}
 function drop(which,cursor){const marker=which==='title'?title:excerpt;dispatch('qwen-prompt-editor','drop',{dataTransfer:{getData:()=>`article-${which}`}});field.value=field.value.slice(0,cursor)+marker+field.value.slice(cursor);field.selectionEnd=cursor+marker.length;dispatch('qwen-prompt-editor','input');}
 const canonical=()=>({system:elements.get('qwen-template').value,article:elements.get('qwen-article-template').value});
 return{field,elements,dispatch,edit,add,drop,canonical,hooks:context.window.hooks};
}
let count=0;function test(name,fn){fn();count++;console.log('PASS '+name);}const occurrences=(value,token)=>value.split(token).length-1;
test('actual Qwen form has one editable textarea and two hidden canonical API fields',()=>{
 const qwen=template.match(/<form\b[^>]*id="qwen-settings-form"[\s\S]*?<\/form>/)[0];assert.equal([...qwen.matchAll(/<textarea\b/g)].length,1);assert(qwen.includes('for="qwen-prompt-editor">Qwen prompt'));
 for(const name of ['qwen_template','qwen_article_template'])assert(new RegExp(`<input[^>]*name="${name}"[^>]*type="hidden"`).test(qwen));assert(!qwen.includes('move-article-'));
});
test('default combined editor keeps roles separate and previews JSON-quoted field labels',()=>{
 const h=setup();assert.equal(h.field.validity,'');assert.equal(h.field.value,`SYSTEM INSTRUCTIONS\n${divider}\n${defaultArticle}`);assert.deepEqual(h.canonical(),{system:'SYSTEM INSTRUCTIONS',article:defaultArticle});
 assert.equal(h.elements.get('qwen-article-preview').textContent,'Article data:\n{"title": "Article title", "excerpt": "Article excerpt"}');assert.equal(h.elements.get('flux-prefix').value,'Style {{qwen_prompt}}');
 assert(h.elements.get('add-article-title').hidden);assert(h.elements.get('add-article-excerpt').hidden);
});
test('split and join preserve framing-adjacent whitespace, CRLF and Unicode without trimming',()=>{
 const h=setup();for(const system of ['  system\r\n\t','\n🌱\n','system\r'])for(const article of [`\r\n ${title}\r\n${excerpt} \t`,'\n'+title+excerpt+'\n']){
  const parts=h.hooks.splitQwen(h.hooks.joinQwen(system,article));assert.equal(parts.system,system);assert.equal(parts.article,article);
 }
});
test('native LF display preserves original saved CRLF independently until that role is edited',()=>{
 const system='  System\r\n\t',article=`\r\n${title}\r\n${excerpt} \r\n`,h=setup(article,system);assert.equal(h.field.validity,'');assert.deepEqual(h.canonical(),{system,article});
 h.edit(h.field.value);assert.deepEqual(h.canonical(),{system,article});h.edit(h.field.value.replace('System','Changed'));assert.equal(h.canonical().system,'  Changed\n\t');assert.equal(h.canonical().article,article);
 h.edit(h.field.value+' edited');assert.equal(h.canonical().article,article.replace(/\r\n/g,'\n')+' edited');
});
test('missing, duplicate and existing role divider collisions block save without guessing a split',()=>{
 for(const system of ['Instructions\n'+divider+'\nextra','Instructions']){
  const h=setup(defaultArticle,system);if(system==='Instructions')h.edit(h.field.value.replace(divider,''));assert(h.field.validity.includes('exactly one divider'));assert.equal(h.canonical().system,system);
  const event=h.dispatch('qwen-settings-form','submit');assert(event.prevented&&event.stopped);assert(h.field.reported&&h.field.openAtReport);
 }
 const h=setup(defaultArticle+'\n'+divider);assert(h.field.validity);assert.equal(h.canonical().article,defaultArticle+'\n'+divider);
});
test('article tokens in the system region block serialization and keep the prior valid canonical values',()=>{
 const h=setup();h.edit(title+'\n'+h.field.value);assert(h.field.validity.includes('below the divider'));assert.equal(h.canonical().system,'SYSTEM INSTRUCTIONS');assert.equal(h.field.attributes['aria-invalid'],'true');
 h.edit(`Instructions ${excerpt}\n${divider}\n${title}`);assert(h.field.validity.includes('below the divider'));assert(h.elements.get('add-article-excerpt').hidden);
});
test('missing-only Add controls insert at article caret or article end when caret is in system',()=>{
 for(const which of ['title','excerpt'])for(const position of ['system','article']){
  const marker=which==='title'?title:excerpt,article=defaultArticle.replace(marker,''),h=setup(article);assert(!h.elements.get(`add-article-${which}`).hidden);
  const start=h.hooks.splitQwen(h.field.value).articleStart;h.add(which,position==='system'?2:start+1);assert.equal(h.field.validity,'');assert(h.elements.get(`add-article-${which}`).hidden);assert.equal(occurrences(h.field.value,marker),1);
  assert.equal(h.canonical().system,'SYSTEM INSTRUCTIONS');if(position==='system')assert(h.canonical().article.endsWith(marker));else assert(h.canonical().article.startsWith('A'+marker));
  const before=h.field.value;h.add(which,0);assert.equal(h.field.value,before);
 }
});
test('duplicate tokens show validation without an Add button or silent duplicate repair',()=>{
 const h=setup(title+title+excerpt);assert(h.field.validity.includes('exactly one'));assert(h.elements.get('add-article-title').hidden);const before=h.field.value;h.add('title',0);assert.equal(h.field.value,before);
});
test('Add snaps outside another marker and a Unicode surrogate pair',()=>{
 const h=setup('🌱'+excerpt),start=h.hooks.splitQwen(h.field.value).articleStart;h.add('title',start+1);assert.equal(h.canonical().article,'🌱'+title+excerpt);
 const other=setup('A'+excerpt+'B'),offset=other.hooks.splitQwen(other.field.value).articleStart;other.add('title',offset+5);assert.equal(other.canonical().article,'A'+excerpt+title+'B');
});
test('role budgets count Unicode code points and preserved CRLF wire characters',()=>{
 const h=setup();h.edit('🌱'.repeat(4000)+'\n'+divider+'\n'+'🌱'.repeat(2000)+title+excerpt);assert.equal(h.field.validity,'');
 h.edit('🌱'.repeat(4001)+'\n'+divider+'\n'+title+excerpt);assert(h.field.validity.includes('4,000'));
 h.edit('System\n'+divider+'\n'+'🌱'.repeat(2001)+title+excerpt);assert(h.field.validity.includes('2,000'));
 const wire=setup('x'.repeat(1999)+'\r\n'+title+excerpt);assert(wire.field.validity.includes('2,000'));
});
test('article controls and lone surrogates fail while ordinary whitespace and unknown markers remain literal',()=>{
 const h=setup();for(const invalid of ['\u0000','\u001f','\u007f','\u0085','\ud800','\udfff']){h.edit(`System\n${divider}\n${invalid}${title}${excerpt}`);assert(h.field.validity.includes('control characters'));}
 h.edit(`System\n${divider}\n{{literal}}🌱\t\n${title}${excerpt}`);assert.equal(h.field.validity,'');assert(h.elements.get('qwen-article-preview').textContent.startsWith('{{literal}}'));
});
test('preview chip drops move tokens safely inside article text and other markers',()=>{
 for(const which of ['title','excerpt']){const h=setup(),other=which==='title'?excerpt:title;h.drop(which,h.field.value.indexOf(other)+5);assert.equal(h.field.validity,'');assert.equal(occurrences(h.field.value,title),1);assert.equal(occurrences(h.field.value,excerpt),1);assert(h.canonical().article.includes(other+(which==='title'?title:excerpt)));}
});
test('cross-boundary and divider-splitting token drops are rejected without changing either role',()=>{
 for(const position of [0,'divider']){const h=setup(),before=h.field.value;h.drop('title',position==='divider'?h.field.value.indexOf(divider)+5:position);assert.equal(h.field.value,before);assert.equal(h.field.validity,'');assert(h.elements.get('qwen-article-status').textContent.includes('not moved'));assert.deepEqual(h.canonical(),{system:'SYSTEM INSTRUCTIONS',article:defaultArticle});}
});
test('native cut/paste supports token movement and undo without changing the system role',()=>{
 const h=setup();h.edit(h.field.value.replace(title,''));assert(!h.elements.get('add-article-title').hidden);assert(h.field.validity);
 h.edit(h.field.value+title);assert.equal(h.field.validity,'');assert(h.canonical().article.endsWith(title));assert.equal(h.canonical().system,'SYSTEM INSTRUCTIONS');
});
test('preview chips retain scoped plain drag payloads and select the marker in the combined editor',()=>{
 const h=setup(),values={};h.dispatch('article-title-token','dragstart',{dataTransfer:{setData(key,value){values[key]=value;}}});assert.equal(values['text/plain'],title);assert.equal(values['application/x-pulsefeed-token'],'article-title');
 h.dispatch('article-excerpt-token','click');assert.equal(h.field.selectionStart,h.field.value.indexOf(excerpt));assert.equal(h.field.selectionEnd-h.field.selectionStart,excerpt.length);
});
test('validation targets the visible editor and reveals only its own collapsed section',()=>{
 const h=setup('No tokens');h.field.section.open=false;const event=h.dispatch('qwen-settings-form','submit');assert(event.prevented&&event.stopped&&h.field.reported&&h.field.openAtReport);assert(!h.elements.get('flux-settings-section').open);
 h.field.section.open=false;h.dispatch('qwen-settings-form','invalid',{target:h.field});assert(h.field.section.open);assert.equal(h.field.value,`SYSTEM INSTRUCTIONS\n${divider}\nNo tokens`);
});
test('preview interprets no markup and never puts article values in system instructions',()=>{
 const h=setup();h.edit(`System\n${divider}\n<img src=x onerror=alert(1)> ${title} & ${excerpt}`);assert.equal(h.field.validity,'');assert.equal(h.elements.get('qwen-article-preview').textContent,'<img src=x onerror=alert(1)> "Article title" & "Article excerpt"');assert.equal(h.canonical().system,'System');
});
console.log(JSON.stringify({result:'PASS',tests:count}));
