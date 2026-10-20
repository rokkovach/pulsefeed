/** Editable PulseFeed case-study deck. Requires the supplied @oai/artifact-tool runtime. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const workspaceDir = path.resolve(process.env.WORKSPACE_DIR ?? process.cwd());
const modules = process.env.RUNTIME_NODE_MODULES;
const SKILL_DIR = process.env.PRESENTATIONS_SKILL_DIR;
const RUNTIME_PYTHON = process.env.RUNTIME_PYTHON;
if (!modules || !SKILL_DIR || !RUNTIME_PYTHON) throw new Error('Set RUNTIME_NODE_MODULES, PRESENTATIONS_SKILL_DIR and RUNTIME_PYTHON.');
const require = createRequire(path.join(modules, 'runtime.cjs'));
const sharp = require('sharp');
const { Presentation, PresentationFile, FileBlob } = await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const { finalizePresentation } = await import(pathToFileURL(path.join(SKILL_DIR, 'container_tools/artifact_tool_utils.mjs')).href);
const availableFonts = require('@napi-rs/canvas').GlobalFonts.families.map(x => x.family);
if (!availableFonts.includes('Arial')) throw new Error('The selected design font Arial is unavailable.');
const buildDir = path.join(workspaceDir, 'integration/artifacts/documentation-2026-10-20/presentation');
const revision = process.env.BUILD_REVISION ?? 'initial';
const finalPath = path.resolve(process.env.FINAL_PPTX ?? path.join(workspaceDir, 'output/presentation/pulsefeed-case-study.pptx'));
await fs.mkdir(buildDir, { recursive: true });
await fs.mkdir(path.dirname(finalPath), { recursive: true });
const c = { bg:'#FAF9F6', ink:'#25283D', lavender:'#DED8F8', mint:'#CEF0DE', peach:'#FFDCC7', muted:'#656577', line:'#CAC8D0' };
const p = Presentation.create({ slideSize:{width:1280,height:720} });
function txt(s, text, x,y,w,h, size=26, bold=false, color=c.ink, align='left') {
  const o=s.shapes.add({geometry:'textbox',name:text.slice(0,52),position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
  o.text=text; o.text.style={fontSize:size,typeface:'Arial',bold,color,alignment:align,verticalAlignment:'top',autoFit:'none',insets:0};
  return o;
}
function line(s,x,y,w,color=c.line,width=1){return s.shapes.add({geometry:'line',position:{left:x,top:y,width:w,height:0},fill:'none',line:{fill:color,width}});}
function box(s,text,x,y,w,h,fill=c.lavender,size=25){
  const o=s.shapes.add({geometry:'rect',name:text.replaceAll('\n',' '),position:{left:x,top:y,width:w,height:h},fill,line:{fill:c.ink,width:1.2}});
  o.text=text;o.text.style={fontSize:size,typeface:'Arial',color:c.ink,bold:false,alignment:'center',verticalAlignment:'middle',autoFit:'none',insets:14};return o;
}
function arrow(s,a,b,from='right',to='left',dashed=false){
  return s.shapes.connect(a,b,{kind:'straight',fromSide:from,toSide:to,line:{style:dashed?'dashed':'solid',fill:c.ink,width:2},tail:{type:'triangle',width:'sm',length:'sm'}});
}
function slide(title,notes){const s=p.slides.add();s.background.fill=c.bg;txt(s,title,64,46,1130,76,44,true);txt(s,String(p.slides.items.length).padStart(2,'0'),1194,660,36,26,18,false,c.muted,'right');s.speakerNotes.text=notes;return s;}
function table(s,values,x,y,w,h,widths,size=24){
  const t=s.tables.add({rows:values.length,columns:values[0].length,left:x,top:y,width:w,height:h,columnWidths:widths,values});
  t.borders.assign({fill:c.line,width:1,style:'solid'});
  for(let r=0;r<values.length;r++)for(let col=0;col<values[0].length;col++){
    const cell=t.getCell(r,col);cell.fill=r===0?c.lavender:c.bg;
    cell.text.style={fontSize:size,typeface:'Arial',color:c.ink,bold:r===0,verticalAlignment:'middle',insets:14};
  }return t;
}

// 1. Minimal product brief, using the existing repository brand asset.
{
 const s=slide('PulseFeed','Sources: README.md, Product and safety boundaries and Current status. Brand asset: app/pulsefeed/static/pixel-garden.svg. Technical case study as of 20 October 2026.');
 txt(s,'An RSS reader with\nAI illustrations',64,165,700,155,57,true);
 txt(s,'A public feed becomes a temporary reader page.\nQwen writes a visual prompt. FLUX renders the image.',66,360,675,100,28);
 txt(s,'Runpod technical case study\n20 October 2026',66,570,620,65,22,false,c.muted);
 const garden=await sharp(await fs.readFile(path.join(workspaceDir,'app/pulsefeed/static/pixel-garden.svg'))).resize({width:780}).png().toBuffer();
 s.images.add({blob:garden,contentType:'image/png',alt:'PulseFeed repository pixel garden illustration',fit:'contain',position:{left:800,top:205,width:390,height:280}});
 txt(s,'Real GPU flow validated on Spark\nCloud deployment checks pending',800,535,400,120,25,true);
}
// 2. User lifecycle as an editable process diagram.
{
 const s=slide('The reader experience','Sources: README.md, Product and safety boundaries. docs/architecture.md, One explicit View RSS request and Storage and lifetimes. Diagram describes implemented behavior, not a screenshot of a deployed service.');
 txt(s,'One explicit request creates one private view',64,135,1120,45,29);
 const a=box(s,'Paste a public\nRSS or Atom URL',64,235,250,124,c.lavender);
 const b=box(s,'Click View RSS\nValidate and admit',370,235,250,124,c.mint);
 const d=box(s,'Read articles with\nlabeled AI images',676,235,250,124,c.peach);
 const e=box(s,'View expires\nafter one hour',982,235,234,124,c.lavender);
 arrow(s,a,b);arrow(s,b,d);arrow(s,d,e);
 txt(s,'Original article links stay visible. No signup is required.',64,430,1090,45,28);
 line(s,64,512,1152);
 txt(s,'Refresh restores saved browser images. Read-only requests never start model jobs.',64,545,1120,90,27);
}
// 3. Target architecture with explicit trust boundaries.
{
 const s=slide('Cloud target architecture','Sources: README.md, target deployment and Current status. docs/architecture.md, Runtime topology and Trust boundaries. Railway and Runpod diagram is the intended deployment and is not live. Workers never call each other. API credentials remain server-held.');
 txt(s,'Target deployment. Railway and target Runpod GPU remain unverified.',64,128,1130,40,25,false,c.muted);
 const browser=box(s,'Visitor browser\nHTML + JavaScript',64,240,230,112,c.bg,24);
 const bd=s.shapes.add({geometry:'rect',name:'Railway trust boundary',position:{left:350,top:198,width:440,height:380},fill:'none',line:{fill:c.muted,width:1.5,style:'dashed'}});
 txt(s,'Railway · one app process',374,216,392,32,24,true);
 const api=box(s,'FastAPI\nAuth + admission + prompt policy',374,277,392,92,c.lavender,23);
 const coord=box(s,'Background coordinator',374,401,392,64,c.mint,23);
 const db=box(s,'Persistent SQLite volume',374,495,392,56,c.bg,23);
 const rp=s.shapes.add({geometry:'rect',name:'Runpod trust boundary',position:{left:866,top:198,width:350,height:380},fill:'none',line:{fill:c.muted,width:1.5,style:'dashed'}});
 txt(s,'Runpod · separate queues',890,216,310,34,24,true);
 const q=box(s,'Qwen3-8B NVFP4\nPrompt worker',890,285,302,96,c.lavender,24);
 const f=box(s,'FLUX.2 klein 4B\nImage worker',890,435,302,96,c.peach,24);
 arrow(s,browser,api);arrow(s,api,coord,'bottom','top');arrow(s,coord,db,'bottom','top');arrow(s,coord,q);arrow(s,coord,f);
 txt(s,'HTTPS +\nvisitor cookie',92,371,200,65,21,false,c.muted);
 const rss=box(s,'Untrusted RSS\nBounded fetch',64,485,230,84,c.bg,22);
 s.shapes.connect(rss,api,{kind:'elbow',fromSide:'right',toSide:'left',line:{fill:c.ink,width:1.5},tail:{type:'triangle',width:'sm',length:'sm'}});
 txt(s,'Server-held credentials cross the provider boundary. Each worker validates a fixed job schema.',64,617,1110,45,23);
}
// 4. Request pipeline and lifetimes.
{
 const s=slide('One request, two model stages','Sources: docs/architecture.md, One explicit View RSS request, Admission and state, and Storage and lifetimes. Runpod async result retention is documented there with the provider source link. SQLite saves metadata, not image bytes. There is no S3/R2 or permanent server image archive.');
 const labels=[['Save admission\nand invocation',c.mint],['Qwen creates\na visual prompt',c.lavender],['App validates\nand filters',c.bg],['FLUX renders\n1024 × 1024',c.peach],['Browser saves\nimage Blob',c.mint]];
 const bs=labels.map(([v,col],i)=>box(s,v,64+i*234,167,216,102,col,23));
 for(let i=0;i<bs.length-1;i++)arrow(s,bs[i],bs[i+1]);
 table(s,[['Store','What it keeps','Lifetime'],['SQLite volume','Snapshots, usage, invocations and history','Persistent metadata'],['Runpod queue output','Temporary prompt or image result','30 minutes after completion'],['Browser IndexedDB','Visitor/view image Blobs','Original one-hour view expiry']],64,333,1152,260,[270,550,332],23);
 txt(s,'A late return without a saved browser copy can leave an image unavailable.\nThe app reports this without generating it again.',64,620,1130,65,24);
}
// 5. Model separation, editable comparison table.
{
 const s=slide('Separate models, separate deployments','Sources: README.md, model presets and Current status. docs/architecture.md, Deliberate constraints. docs/dependency-remediation-2026-10-20.md, Final maintained images and Release handoff.');
 table(s,[['Decision','Qwen worker','FLUX worker'],['Job','Article context to visual prompt','Prompt to one illustration'],['Pinned model','Qwen3-8B NVFP4','FLUX.2 klein 4B, BF16'],['Runtime boundary','Independent image and dependency lock','Independent image and dependency lock'],['Execution preset','Fixed prompt job contract','1024 × 1024, 4 steps, guidance 1']],64,164,1152,342,[250,451,451],23);
 txt(s,'Separate stacks isolate incompatible dependencies and allow independent scaling.',64,547,1130,65,27);
 txt(s,'The cost is two cold starts and potentially two billed GPU workers. Cloud cost and latency still need measurement.',64,625,1120,60,24,false,c.muted);
}
// 6. Flat safeguard narrative.
{
 const s=slide('Limits and failure behavior','Sources: README.md, Product and safety boundaries. docs/architecture.md, Admission and state and Trust boundaries. Default policy: 100 global daily FLUX attempts, 30 global starts per rolling hour, 10 maximum stories per new reader page. Each selected story reserves two FLUX attempts. Operator test override: nine stories. Authentication and prompt checks mitigate specific boundaries and do not imply complete vulnerability clearance.');
 txt(s,'Cost and retry accounting',64,163,535,42,30,true);
 txt(s,'Fresh-database defaults',64,207,520,28,21,false,c.muted);
 txt(s,'100 FLUX attempts per day\n30 starts per rolling hour\nUp to 10 stories per new page',64,246,520,145,29);
 txt(s,'Each story reserves two attempts for the bounded retry budget.',64,411,500,90,26);
 txt(s,'Unknown provider acceptance holds reservations and freezes new dispatch. A saved job ID is polled, never blindly resubmitted.',64,535,500,115,25);
 txt(s,'Trust and abuse controls',684,163,530,42,30,true);
 txt(s,'Visitor ownership + CSRF\nAdmin password exchange\nBounded public-feed fetch\nValidated model results',684,230,500,180,28);
 txt(s,'Global quotas bound shared inference. A new anonymous browser can create a new visitor identity.',684,448,500,92,26);
 txt(s,'Proxy-derived IP controls require verified ingress configuration.',684,579,500,74,25,false,c.muted);
}
// 7. Measured final-maintained native result.
{
 const s=slide('The final Spark run completed 27 images','Sources: docs/dependency-remediation-2026-10-20.md, Test evidence, final maintained-image run. Qwen e0b3… and FLUX 310c… on native ARM64 DGX Spark/GB10 through laptop FastAPI and authenticated HTTP provider, using a fresh isolated SQLite database. Final evidence is the maintained-image run, not earlier checkpoints. Qwen 13,140 input + 2,016 output = 15,156 tokens. Operator limit nine stories per page. These observations do not establish AMD64 Runpod behavior or a latency benchmark.');
 txt(s,'Native ARM64 DGX Spark / GB10 · final maintained worker images',64,131,1130,42,25,false,c.muted);
 txt(s,'Operator override: nine stories per page',64,176,1130,30,22,false,c.muted);
 txt(s,'27',64,206,310,130,100,true);txt(s,'images',68,342,310,44,28);
 txt(s,'54',465,206,310,130,100,true);txt(s,'successful model calls',469,342,360,44,28);
 txt(s,'0',920,206,290,130,100,true);txt(s,'retries',924,342,280,44,28);
 table(s,[['Source','Completed images'],['Hacker News','9'],['BBC England','9'],['NYTimes','9']],64,423,565,211,[390,175],24);
 txt(s,'Qwen usage\n13,140 input tokens\n2,016 output tokens',706,435,490,135,29);
 txt(s,'Replay and refresh checks passed.\nQuota accounting and decoding passed.\nNo latency benchmark.',706,579,490,96,24,false,c.muted);
}
// 8. Concrete dependency lesson.
{
 const s=slide('The engine import caught a real mismatch','Sources: docs/dependency-remediation-2026-10-20.md, Final maintained images and Security findings that remain. Rejected AMD64 checkpoint passed basic imports but actual from vllm import LLM exposed TorchCodec requiring libnvrtc.so.13 in a CUDA 12.9 profile. Corrected image enforces the actual decoder/LLM/SamplingParams/EngineArgs gate. Unused Mooncake removal resolved 37 Go advisory keys, including the rejected image’s three Critical and 27 additional High matches. The retained 22 High matches concern bundled FFmpeg. Full-image OS/native findings remain.');
 txt(s,'Basic imports passed. Loading the actual engine failed.',64,137,1135,45,30);
 const a=box(s,'Rejected AMD64 checkpoint\nTorchCodec expected\nlibnvrtc.so.13',64,226,440,151,c.peach,27);
 const b=box(s,'Corrected AMD64 image\nActual engine import gate\n87 CPU / SDK tests passed',710,226,506,151,c.mint,27);
 arrow(s,a,b);
 txt(s,'CUDA 12.9 profile\nexposed the mismatch',64,404,440,77,25,false,c.muted);
 txt(s,'Unused Mooncake removed\n37 Go advisory keys removed',710,404,506,77,25);
 line(s,64,527,1152);
 txt(s,'Full-image scans still retain upstream OS and bundled FFmpeg findings.\nPassing imports and tests do not establish vulnerability clearance.',64,565,1120,100,28);
}
// 9. Concrete release gates and tradeoffs.
{
 const s=slide('The remaining deployment work','Sources: README.md, Current status. docs/dependency-remediation-2026-10-20.md, Release handoff. docs/architecture.md, Storage and lifetimes and Deliberate constraints. docs/case-study.md, Fill in after cloud validation. New immutable publication, actual RTX PRO 4500 Blackwell queue/GPU tests, Railway HTTPS/proxy/persistent volume/secrets checks, and measured cost/latency remain pending.');
 txt(s,'Release gates',64,166,540,43,30,true);
 const rows=[['01','Publish rebuilt worker images with immutable versions'],['02','Run both AMD64 images on the actual RTX PRO 4500 Blackwell queue'],['03','Verify Railway HTTPS, proxy trust, volume and secrets'],['04','Measure cold/warm latency and provider billing']];
 rows.forEach(([n,v],i)=>{txt(s,n,64,240+i*91,55,44,27,true);txt(s,v,142,237+i*91,475,78,26);});
 txt(s,'Current tradeoffs',720,166,495,43,30,true);
 txt(s,'One app process and SQLite\nkeep coordinator ownership simple.',720,239,490,90,27);
 txt(s,'Two workers can scale independently,\nwith two cold-start paths.',720,361,490,90,27);
 txt(s,'Temporary images limit storage,\nwith a late-retrieval gap.',720,483,490,90,27);
 txt(s,'Cloud deployment is the next evidence gate.',720,615,490,58,25,true);
}

const candidatePath=path.join(buildDir,'candidate.pptx');
await (await PresentationFile.exportPptx(p)).save(candidatePath);
const result=await finalizePresentation({
 workspaceDir,candidatePath,finalPath,pythonExecutable:RUNTIME_PYTHON,
 integrityValidatorPath:path.join(SKILL_DIR,'container_tools/inspect_presentation_package_integrity.py'),
 layoutValidatorPath:path.join(SKILL_DIR,'container_tools/inspect_presentation_layout_geometry.py'),
 explicitTotalSlideCount:9,requiredNativeTableOwnerSlides:[4,5,7],requiredNativeChartOwnerSlides:[],
 layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit',...([4,5,7].flatMap(n=>['--require-native-table-slide',String(n)]))],
 fontPolicy:{basis:'design',families:['Arial']},verifyArtifactToolImport:true,
 receiptPath:path.join(buildDir,`validation-${revision}.json`),
});
// Review the actual final PPTX after importing it, not just the authoring state.
const final=await PresentationFile.importPptx(await FileBlob.load(finalPath));
const renderDir=path.join(buildDir,'final-render');await fs.mkdir(renderDir,{recursive:true});
for(let i=0;i<final.slides.items.length;i++){
 const blob=await final.export({slide:final.slides.items[i],format:'png',scale:1});
 await fs.writeFile(path.join(renderDir,`slide-${i+1}.png`),new Uint8Array(await blob.arrayBuffer()));
}
const pdfPath=path.join(workspaceDir,'output/pdf/pulsefeed-case-study.pdf');
await fs.mkdir(path.dirname(pdfPath),{recursive:true});
try {const pdf=await final.export({format:'pdf'});await fs.writeFile(pdfPath,new Uint8Array(await pdf.arrayBuffer()));console.log(`PDF: ${pdfPath}`);}catch(error){
 console.error(`Native PDF export unavailable: ${error.message}`);
 if(process.env.SOFFICE_BIN){
   const out=path.join(buildDir,'pdf-fallback');await fs.mkdir(out,{recursive:true});
   const profile=pathToFileURL(path.join(buildDir,`lo-profile-${process.pid}`)).href;
   const converted=spawnSync(process.env.SOFFICE_BIN,[`-env:UserInstallation=${profile}`,'--headless','--convert-to','pdf','--outdir',out,finalPath],{encoding:'utf8'});
   if(converted.status!==0)throw new Error(`Bundled LibreOffice PDF fallback failed: ${converted.stderr}`);
   await fs.copyFile(path.join(out,path.basename(finalPath,'.pptx')+'.pdf'),pdfPath);
   console.log(`PDF via bundled LibreOffice: ${pdfPath}`);
 } else console.error('PDF_EXPORT_UNAVAILABLE: Configure SOFFICE_BIN with the supplied bundled headless executable.');
}
console.log(JSON.stringify({slideCount:final.slides.items.length,finalPath,renderDir,result},null,2));
