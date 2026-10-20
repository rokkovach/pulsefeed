"""Build hand-laid-out SVG diagrams; render with bundled Node + sharp (see README)."""
from pathlib import Path
from html import escape
ROOT = Path(__file__).resolve().parent
INK='#25283D'; PAPER='#FAF9F6'; LAV='#DED8F8'; MINT='#CEF0DE'; PEACH='#FFDCC7'; MUTED='#606579'; LINE='#B8B8C5'
class SVG:
 def __init__(self,w,h,title,desc):
  self.w,self.h=w,h; self.s=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-labelledby="title desc"><title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="{INK}"/></marker></defs><rect width="{w}" height="{h}" fill="{PAPER}"/>']
 def rect(self,x,y,w,h,fill=PAPER,stroke=INK,dash=False):
  self.s.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="2"'+(' stroke-dasharray="8 7"' if dash else '')+'/>')
 def text(self,x,y,lines,size=20,weight=400,color=INK,anchor='start',lh=None):
  if isinstance(lines,str):lines=[lines]
  lh=lh or size*1.4
  for i,l in enumerate(lines):self.s.append(f'<text x="{x}" y="{y+i*lh}" font-family="Inter, -apple-system, BlinkMacSystemFont, Segoe UI, Arial, sans-serif" font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{escape(l)}</text>')
 def line(self,pts,arrow=True,both=False,dash=False,color=INK):
  self.s.append(f'<polyline points="{" ".join(f"{x},{y}" for x,y in pts)}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round"'+(' marker-end="url(#arrow)"' if arrow else '')+(' marker-start="url(#arrow)"' if both else '')+(' stroke-dasharray="6 6"' if dash else '')+'/>')
 def node(self,x,y,w,h,title,body=(),fill=PAPER):
  self.rect(x+4,y+4,w,h,'#EAE7E1',stroke='none');self.rect(x,y,w,h,fill);self.text(x+20,y+33,title,23,650);self.text(x+20,y+65,body,19,color=MUTED,lh=27)
 def header(self,title,subtitle,kicker='PULSEFEED · TECHNICAL REFERENCE'):
  self.rect(50,38,12,12,LAV,stroke='none');self.rect(65,38,12,12,MINT,stroke='none');self.rect(50,53,12,12,PEACH,stroke='none');self.text(92,55,kicker,16,650,color=MUTED);self.text(50,105,title,38,700);self.text(50,140,subtitle,21,color=MUTED)
 def footer(self,text):self.text(50,self.h-24,text,16,color=MUTED)
 def save(self,name): (ROOT/f'{name}.svg').write_text(''.join(self.s)+'</svg>')

s=SVG(1600,1000,'PulseFeed architecture and trust boundaries','A single FastAPI app orchestrates two separate Runpod queue workers. Browser-local images expire with the one-hour view; SQLite stores metadata, not an image archive.')
s.header('One app orchestrates two independent workers','Railway and Runpod verified together · Separate model workers and persistent application state')
for x,w,label in [(50,330,'VISITOR BROWSER'),(420,650,'RAILWAY · WEB APP'),(1110,440,'RUNPOD · LIVE ENDPOINTS')]:
 s.rect(x,175,w,540,stroke=LINE,dash=True);s.text(x+20,210,label,18,700)
s.node(75,245,280,110,'Reader UI',['HTML + local JavaScript','Owner-scoped requests'],LAV)
s.node(75,405,280,115,'sessionStorage',['Submission key + recovery','10-minute recovery record'])
s.node(75,555,280,135,'IndexedDB',['Validated image Blobs','Purge at original view expiry','One-hour reader lifetime'],MINT)
s.line([(215,355),(215,405)],both=True);s.line([(355,320),(390,320),(390,615),(355,615)])
s.node(455,245,275,125,'Routes + authorization',['Cookie, CSRF, ownership','Response / image checks'],LAV)
s.node(765,245,270,125,'Admission + policy',['Bounded source snapshot','Atomic limits + reservations'],MINT)
s.node(455,465,275,190,'Persistent SQLite',['Sessions, policy, snapshots','Runs, invocations, usage','Reservations + audit history','Output metadata only'],MINT)
s.node(765,465,270,190,'One coordinator',['Persist intent before submit','Save IDs; poll existing jobs','Compose + filter prompt','One ASGI process / replica'],LAV)
s.line([(355,285),(455,285)],both=True);s.text(405,269,'HTTPS',16,600,anchor='middle')
s.line([(730,307),(765,307)]);s.line([(594,370),(594,465)],both=True);s.text(607,420,'owner + state',17,color=MUTED)
s.line([(900,370),(900,465)]);s.text(913,420,'admitted work',17,color=MUTED)
s.line([(765,560),(730,560)],both=True)
s.node(1140,265,380,130,'Qwen3-8B · NVFP4',['Prompt + measured token usage','Separate queue endpoint','App slots: 1–3 · GPU / worker: 1'],LAV)
s.node(1140,475,380,130,'FLUX.2-klein-4B · BF16',['512 × 512 image + usage','Separate queue endpoint','App slots: 1–3 · GPU / worker: 1'],PEACH)
s.line([(1035,505),(1087,505),(1087,330),(1140,330)],both=True)
s.line([(1035,575),(1140,575)],both=True)
s.text(1138,649,['/run → save ID → /status/{id}','Server-held credentials; fixed jobs'],19,color=MUTED,lh=28)
s.node(455,775,580,120,'Untrusted public RSS / Atom',['Public DNS pinned to socket; redirects revalidated','Safe XML · 2 MiB cap · bounded 10-second fetch'],PEACH)
s.line([(455,340),(438,340),(438,835),(455,835)],both=True)
s.node(50,775,330,120,'Protected admin',['Aggregate usage + policy','Visitor feeds stay owner-scoped'],LAV)
s.text(1110,780,'Temporary provider output',23,650)
s.text(1110,816,['Runpod: 30 min after completion','Weight cache stores models only','No permanent image archive'],19,color=MUTED,lh=29)
s.text(50,943,'Trust boundaries are dashed. Every model exchange passes through the app; workers never call each other.',21,600)
s.footer('Evidence: docs/architecture.md · docs/deployment.md · docs/dependency-remediation-2026-10-20.md | Status: 20 Oct 2026')
s.save('architecture')

s=SVG(1600,1210,'PulseFeed request lifecycle','The browser waits for RSS validation and atomic admission before navigation. The app persists intent before each separate Qwen and FLUX submission; unknown outcomes hold reservations without automatic replay.')
s.header('From “View RSS” to a saved browser image','A fresh request admits work once; read-only page and image retrieval never start model jobs')
# Swim lanes: the app remains the only orchestrator.
xs=[200,565,900,1195,1430]; labels=['Browser','App / coordinator','SQLite','Qwen worker','FLUX worker']
for x,label in zip(xs,labels):
 s.rect(x-110,172,220,48,LAV if label in ['Browser','App / coordinator'] else MINT if label=='SQLite' else PEACH)
 s.text(x,203,label,21,650,anchor='middle');s.line([(x,220),(x,985)],arrow=False,dash=True,color=LINE)
def msg(a,b,y,label,fill=None):
 if fill:s.rect(min(xs[a],xs[b])+10,y-30,abs(xs[b]-xs[a])-20,27,fill,stroke='none')
 s.text((xs[a]+xs[b])/2,y-10,label,18,500,anchor='middle');s.line([(xs[a],y),(xs[b],y)])
def selfnote(lane,y,label,w=300,fill=PAPER):
 x=xs[lane];s.rect(x-w/2,y-20,w,42,fill,stroke=LINE);s.text(x,y+7,label,18,550,anchor='middle')
selfnote(0,251,'Persist request key in sessionStorage',330)
msg(0,1,309,'POST URL + key · cookie + CSRF')
msg(1,2,356,'Check owner + exact-key replay')
selfnote(1,405,'Fresh key: validate + fetch RSS',310,PEACH)
msg(1,2,459,'Snapshot + quotas + reservations')
msg(1,0,512,'Validated view · immutable 1-hour expiry')
selfnote(0,554,'Save response, then navigate',310,MINT)
msg(1,2,599,'Persist Qwen intent / submitting')
msg(1,3,644,'Submit /run · persist ID · poll /status/{id}')
msg(3,1,689,'Verified prompt + measured usage')
msg(1,2,734,'Save final prompt + persist FLUX intent')
msg(1,4,779,'Submit /run · persist ID · poll /status/{id}')
msg(4,1,824,'Image + measured usage')
msg(1,2,869,'Verify, settle, save metadata only')
msg(0,1,914,'Poll owned batch; request existing image')
msg(1,0,958,'Validated bytes → decode → IndexedDB Blob')
s.node(50,1020,470,135,'Exact replay / reload',['Same key: original selection + expiry','No new generation; uncertain POST','requires an explicit same-key retry'],MINT)
s.node(550,1020,500,135,'Unknown provider outcome',['Hold reservations; freeze new dispatch','Keep polling known job IDs when possible','Operator reconciliation; no automatic replay'],PEACH)
s.node(1080,1020,470,135,'Finite image lifetime',['Browser Blob: original one-hour expiry','Provider result: temporary retrieval','SQLite: metadata, never image bytes'],LAV)
s.footer('RSS rejection precedes navigation and inference. Qwen → app → FLUX; no direct model-to-model call. | Status: 20 Oct 2026')
s.save('request-lifecycle')

s=SVG(1600,1000,'PulseFeed local and cloud validation','Spark GPU execution, laptop-to-Runpod queues and the full Railway generation flow are verified.')
s.header('Verified local and cloud model paths','The same app contracts span native Spark and the verified Railway-to-Runpod deployment')
s.rect(50,180,720,630,stroke=LINE);s.rect(830,180,720,630,stroke=LINE,dash=True)
s.rect(50,180,720,52,MINT);s.text(74,214,'ACTUAL · LAPTOP + DGX SPARK',22,700)
s.rect(830,180,720,52,PEACH);s.text(854,214,'VERIFIED · RAILWAY + RUNPOD',22,700)
s.node(80,260,660,95,'Laptop · FastAPI + SQLite',['One coordinator · authenticated local_http provider'],LAV)
s.node(860,260,660,95,'Railway · FastAPI + persistent /data',['One replica, one ASGI process, one coordinator'],LAV)
s.line([(410,355),(410,407)],both=True);s.text(430,389,'SSH tunnel · loopback gateways',19,color=MUTED)
s.line([(1190,355),(1190,407)],both=True);s.text(1210,389,'Native queue API',19,color=MUTED)
s.rect(80,417,660,255,fill='#F0F6EF',stroke=LINE);s.text(102,452,'Spark · GB10 / ARM64 / SM121',23,650)
s.node(102,478,292,110,'Qwen · NVFP4',['Authenticated gateway','One active invocation'],LAV)
s.node(426,478,292,110,'FLUX · BF16',['Authenticated gateway','One active invocation'],PEACH)
s.rect(102,610,616,40,MINT,stroke='none');s.text(410,637,'Both resident · shared lock serializes actual GPU calls',19,600,anchor='middle')
s.rect(860,417,660,255,fill='#FBF0E7',stroke=LINE,dash=True);s.text(882,452,'Runpod · AMD64 / Blackwell SM120',23,650)
s.node(882,478,292,110,'Qwen endpoint',['App permits 1–3 jobs / model','1 GPU · concurrency 1'],LAV)
s.node(1206,478,292,110,'FLUX endpoint',['App permits 1–3 jobs / model','1 GPU · concurrency 1'],PEACH)
s.text(1190,636,'Operator GPU selection: RTX 5090 · 32 GB',20,600,anchor='middle')
s.text(90,711,'Verified on native Spark GPU',23,650)
s.text(90,746,['27 images · 54 successful model calls · zero retries','Transport/status work may overlap; GPU inference serializes'],20,color=MUTED,lh=31)
s.text(870,711,'Native Runpod queue / GPU execution verified',22,650)
s.text(870,746,['Local app → Runpod: 10 images / 20 calls, no retries','Railway → Runpod: 9 images / 18 calls, no retries'],20,color=MUTED,lh=31)
s.rect(50,850,1500,88,LAV,stroke='none');s.text(76,882,'Shared invariants',22,650);s.text(310,881,'Qwen → app → FLUX · fixed models / presets · app owns admission and accounting',21)
s.text(310,913,'Persistent SQLite metadata · browser Blobs until view expiry · temporary provider output · no image archive',20,color=MUTED)
s.footer('Separate evidence: Spark / ARM64; Railway → Runpod / AMD64. Timings separate queue and execution. | 20 Oct 2026')
s.save('local-vs-cloud')
