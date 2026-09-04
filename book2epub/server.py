from __future__ import annotations
import json, mimetypes, threading, time, io, contextlib, re, os, json as _json, statistics
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from .db import connect
from .pipeline import ocr, correct, assemble, structure, translate, build, reset_project, reset_stage, set_progress_hook, set_cancel_hook
from .translation import set_retry_hook

STAGES=('ocr','correction','assembly','structure','translation','build')
DEPS={'ocr':None,'correction':'ocr','assembly':'correction','structure':'assembly','translation':'structure','build':'structure'}
_CANCEL_EVENTS={}

UI='''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>book2epub dashboard</title><style>:root{color-scheme:light}body{font:14px system-ui,-apple-system,sans-serif;margin:0;background:#f4f6fa;color:#172033}header{padding:24px 5%;background:#172033;color:white}main{max-width:1200px;margin:24px auto;padding:0 20px}.summary,.card{background:white;border-radius:12px;padding:18px;box-shadow:0 2px 10px #17203312;margin-bottom:16px}.totals{display:flex;gap:24px;flex-wrap:wrap}.metric{min-width:130px}.metric b{display:block;font-size:20px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}.card h2{margin:0 0 8px;text-transform:capitalize}.state{font-weight:600}.bar{height:9px;background:#e5e9f2;border-radius:8px;overflow:hidden}.bar i{display:block;height:100%;background:#4969d8}.muted{color:#667085}.err{color:#b42318;background:#fff1f0;padding:8px;border-radius:6px;margin-top:8px;white-space:pre-wrap}.actions{margin-top:12px}button,a{margin-right:8px;padding:7px 10px;border:0;border-radius:6px;background:#e8edf8;color:#172033;text-decoration:none;cursor:pointer}button.primary{background:#4969d8;color:#fff}button:disabled{opacity:.45;cursor:not-allowed}pre{white-space:pre-wrap}</style><header><h1>book2epub pipeline</h1><div id="summary">Loading…</div></header><main><div class="summary"><h2>Overall usage</h2><div id="totals" class="totals"></div></div><nav class="summary"><a href="/ocr">OCR</a><a href="/correction">Correction</a><a href="/assembly">Assembly</a><a href="/structure">Structure</a><a href="/translation">Translation</a><a href="/costs">Costs/logs</a></nav><div id="stages" class="cards"></div><div id="warning"></div></main><script>const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function run(stage,reset){if(reset&&!confirm('Reset '+stage+'? This deletes derived '+stage+' results.'))return;let r=await fetch('/api/stages/'+stage+'/'+(reset?'reset':'run'),{method:'POST'});let x=await r.json();if(!r.ok)alert((x.reason||'Stage could not be started')+(x.blocked_by?' — blocked by '+x.blocked_by:''));load()}function metrics(m){let c=Object.entries(m.costs||{}).map(([k,v])=>esc(k)+' '+Number(v).toFixed(4)).join(', ')||'—';return '<div class="muted">input '+m.input_tokens+' · cached '+m.cached_input_tokens+' · output '+m.output_tokens+' · total '+m.total_tokens+'<br>cost '+c+' · requests '+m.requests+' · retries '+m.retries+' · failures '+m.failures+'</div>'}async function load(){try{let x=await(await fetch('/api/pipeline/status')).json();document.getElementById('summary').innerHTML='<b>'+esc(x.source_lang||'?')+' → '+esc(x.target_lang||'source build')+'</b> · '+esc(x.source||'');let t=x.overall;document.getElementById('totals').innerHTML='<div class="metric"><span>Tokens</span><b>'+t.total_tokens+'</b></div><div class="metric"><span>Input / cached / output</span><b>'+t.input_tokens+' / '+t.cached_input_tokens+' / '+t.output_tokens+'</b></div><div class="metric"><span>Cost</span><b>'+Object.entries(t.costs||{}).map(([k,v])=>esc(k)+' '+Number(v).toFixed(4)).join(', ')||'—'+'</b></div><div class="metric"><span>LLM requests</span><b>'+t.requests+' · retries '+t.retries+' · failures '+t.failures+'</b></div>';document.getElementById('stages').innerHTML=x.stages.map(s=>{let pct=s.total?Math.min(100,100*s.completed/s.total):0;return '<section class="card"><h2>'+esc(s.name)+'</h2><div class="state">'+esc(s.status)+(s.blocked_by?' · blocked by '+esc(s.blocked_by):'')+'</div><p><b>'+s.completed+' of '+(s.total||'?')+' '+esc(s.unit||'items')+'</b> · '+pct.toFixed(0)+'%</p><div class="bar"><i style="width:'+pct+'%"></i></div>'+metrics(s.metrics)+(s.current?'<p class="muted">'+esc(s.current)+'</p>':'')+(s.error?'<div class="err">'+esc(s.error)+'</div>':'')+'<div class="actions"><button class="primary" '+(!s.runnable?'disabled':'')+' onclick="run(\''+s.name+'\',false)">Run / Resume</button><button onclick="run(\''+s.name+'\',true)">Reset</button><a href="/'+s.name+'">Inspect</a></div></section>'}).join('')}catch(e){document.getElementById('warning').innerHTML='<div class="err">Could not load live status: '+esc(e)+'</div>'}}load();setInterval(load,1500)</script>'''

INSPECT_UI='''<!doctype html><meta charset="utf-8"><title>OCR inspection</title><style>body{margin:0;font:14px system-ui;display:grid;grid-template-columns:180px 1fr 420px;height:100vh}aside,section{overflow:auto;padding:12px;border-right:1px solid #ddd}#page{max-width:100%}.block{border:1px solid #ddd;margin:8px 0;padding:8px}.block.approved{border-color:#2a6}textarea{width:100%;min-height:100px}a,button{margin:4px;padding:6px 10px}</style><aside><p><a href="/">← dashboard</a></p><h3>Pages</h3><div id="pageList"></div></aside><section><img id="page"><pre id="ocr"></pre></section><aside><h3>Blocks</h3><div id="blocks"></div></aside><script>let current=1;const listEl=document.getElementById('pageList'),pageEl=document.getElementById('page'),ocrEl=document.getElementById('ocr'),blocksEl=document.getElementById('blocks');async function load(n){current=n;let p=await(await fetch('/api/pages/'+n)).json();pageEl.src='/files/'+p.image;ocrEl.textContent=p.text;blocksEl.innerHTML=p.blocks.map(b=>'<div class="block '+(b.status==='approved'?'approved':'')+'"><small>#'+b.id+' '+(b.corrected_kind||b.kind)+(b.corrected_heading_level?' level '+b.corrected_heading_level:'')+'</small><textarea id="t'+b.id+'">'+(b.corrected_text||b.approved_text||b.text||'')+'</textarea><button onclick="save('+b.id+')">Save</button><button onclick="approve('+b.id+')">Approve</button></div>').join('')}async function save(id){await fetch('/api/blocks/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:document.getElementById('t'+id).value})});load(current)}async function approve(id){await fetch('/api/blocks/'+id+'/approve',{method:'POST'});load(current)}(async()=>{let x=await(await fetch('/api/pages')).json();listEl.innerHTML=x.map(p=>'<button onclick="load('+p.number+')">'+p.number+'</button>').join('');if(x.length)load(x[0].number)})()</script>'''

ASSEMBLY_UI='''<!doctype html><meta charset="utf-8"><title>Assembly inspection</title><style>body{font:14px system-ui;margin:16px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.grid img{width:100%;max-height:300px;object-fit:contain;border:1px solid #ddd}article{border:1px solid #ddd;padding:10px;margin:8px 0}textarea{width:95%;min-height:100px}</style><p><a href="/">← dashboard</a></p><h2>Assembly inspection</h2><select id="windowSelect" onchange="loadWindow()"></select><div id="snapshots" class="grid"></div><div id="units"></div><script>let data;const windowSelect=document.getElementById('windowSelect'),snapshots=document.getElementById('snapshots'),units=document.getElementById('units');async function load(){data=await(await fetch('/api/assembly/windows')).json();windowSelect.innerHTML=data.windows.map(w=>'<option value="'+w.window_key+'">pages '+w.window_key+' ('+w.status+')</option>').join('');if(data.windows.length)loadWindow()}async function loadWindow(){let w=await(await fetch('/api/assembly/windows/'+encodeURIComponent(windowSelect.value))).json();snapshots.innerHTML=w.pages.map(p=>'<div><b>page '+p.number+'</b><img src="/files/'+p.image+'"><pre>'+((p.text||'').slice(0,1200))+'</pre></div>').join('');units.innerHTML=w.units.map(u=>'<article><b>#'+u.ordinal+' '+u.kind+' · '+u.status+'</b><p>provenance: '+u.provenance_json+'</p>'+ (u.status==='discarded'?'<em>Discarded duplicate</em>':'<textarea id="t'+u.id+'">'+u.text+'</textarea><br><button onclick="save('+u.id+')">Save</button><button onclick="approve('+u.id+')">Approve</button>')+'</article>').join('')}async function save(id){await fetch('/api/assembly/units/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:document.getElementById('t'+id).value})});loadWindow()}async function approve(id){await fetch('/api/assembly/units/'+id+'/approve',{method:'POST'});loadWindow()}load()</script>'''

STRUCTURE_UI='''<!doctype html><meta charset="utf-8"><title>Structure inspection</title><style>body{font:14px system-ui;margin:24px}li{margin:8px}.meta{color:#666}</style><p><a href="/">← dashboard</a></p><h2>Structure inspection (read-only)</h2><div id="meta"></div><div id="tree"></div><pre id="evidence"></pre><script>async function load(){let x=await (await fetch('/api/structure/tree')).json();meta.textContent=x.run?('Generation '+x.run.generation+' · '+x.run.status+' · '+x.nodes.length+' nodes'):'No structure run';let children=(pid)=>x.nodes.filter(n=>(n.parent_id||null)===(pid||null));let render=(pid)=>children(pid).map(n=>'<li><button onclick="show('+n.id+')">'+n.node_type+': '+n.title+'</button> <span class="meta">units '+n.start_unit+'–'+(n.end_unit||'?')+' · confidence '+(n.confidence||'')+' · '+(n.source_derived?'source':'synthetic')+(n.status==='needs_review'?' · review':'')+'</span>'+((children(n.id).length)?'<ul>'+render(n.id)+'</ul>':'')+'</li>').join('');tree.innerHTML='<ul>'+render(null)+'</ul>';window.nodes=x.nodes}function show(id){let n=nodes.find(x=>x.id===id);evidence.textContent=JSON.stringify(n,null,2)}load()</script>'''

UI=UI.replace("onclick=\"run(''+s.name+'',false)\"", "onclick=\"run(&quot;'+s.name+'&quot;,false)\"").replace("onclick=\"run(''+s.name+'',true)\"", "onclick=\"run(&quot;'+s.name+'&quot;,true)\"")
UI=UI.replace('function metrics(m)', 'async function stop(stage){if(!confirm("Stop "+stage+"?"))return;await fetch("/api/stages/"+stage+"/stop",{method:"POST"});load()}function metrics(m)')
UI=UI.replace("+'%</p>", "+'% · ETA '+(s.eta_seconds==null?'—':Math.ceil(s.eta_seconds/60)+' min')+'</p>")
UI=UI.replace("+'<a href=\"/'+s.name+'\"", "+'<button onclick=\"stop(&quot;'+s.name+'&quot;)\">Stop</button><a href=\"/'+s.name+'\"")
UI=UI.replace('>Reset</button><a href="/', ">Reset</button><button onclick=\"stop(&quot;'+s.name+'&quot;)\">Stop</button><a href=\"/")

def serve(project: Path, host='127.0.0.1', port=8765):
    db=connect(project)
    def stage_status(c, stage):
        if stage=='ocr':
            total=meta(c,'page_count') or 0; done=c.execute('SELECT COUNT(*) FROM pages').fetchone()[0]; return ('completed' if total and done>=total else ('running' if done else 'not_started'),done,total)
        if stage=='correction': total=c.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]; done=c.execute('SELECT COUNT(*) FROM blocks WHERE corrected_text IS NOT NULL').fetchone()[0]
        elif stage=='assembly': total=c.execute('SELECT COUNT(*) FROM assembly_windows').fetchone()[0]; done=total
        elif stage=='structure': total=c.execute('SELECT COUNT(*) FROM structure_chunks').fetchone()[0]; done=total
        elif stage=='translation': total=c.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]; done=c.execute('SELECT COUNT(*) FROM blocks WHERE translation IS NOT NULL').fetchone()[0]
        elif stage=='build':
            done=1 if (project/'book.epub').exists() else 0; total=1
        else: return ('not_started',0,0)
        return ('completed' if total and done>=total else ('running' if done else 'not_started'),done,total)
    def meta(c,key):
        r=c.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        try:return json.loads(r[0]) if r else None
        except:return None
    def usage_summary(c, stage=None):
        where='WHERE stage=?' if stage else ''
        args=(stage,) if stage else ()
        rows=c.execute(f'SELECT COALESCE(SUM(input_tokens),0) i,COALESCE(SUM(cached_input_tokens),0) ci,COALESCE(SUM(output_tokens),0) o,COALESCE(SUM(total_cost),0) cost,COUNT(*) requests FROM usage_events {where}',args).fetchone()
        failures=c.execute(f"SELECT COALESCE(SUM(failed_attempts),0) FROM stage_jobs {'WHERE stage=?' if stage else ''}",args).fetchone()[0]
        retries=c.execute(f'SELECT COALESCE(SUM(retry_count),0) FROM stage_jobs {"WHERE stage=?" if stage else ""}',args).fetchone()[0]
        costs={r['currency'] or 'USD':r['cost'] for r in c.execute(f'SELECT COALESCE(currency,\'USD\') currency,COALESCE(SUM(total_cost),0) cost FROM usage_events {where} GROUP BY currency',args)}
        d={'input_tokens':rows['i'],'cached_input_tokens':rows['ci'],'output_tokens':rows['o'],'total_tokens':rows['i']+rows['o'],'costs':costs,'requests':rows['requests'],'retries':retries,'failures':failures}; return d
    def run_stage(job_id,stage,reset=False):
        out=io.StringIO()
        cancel_event=_CANCEL_EVENTS.setdefault(job_id,threading.Event())
        def progress(s,current,total,detail):
            c=connect(project); row=c.execute('SELECT * FROM stage_jobs WHERE id=?',(job_id,)).fetchone(); now_t=time.time(); samples=[]
            try: samples=_json.loads(row['timing_samples_json'] or '[]')
            except Exception: samples=[]
            # Older jobs stored durations rather than completion timestamps.
            # Discard that legacy shape so it cannot corrupt the new ETA.
            if samples and (not all(isinstance(x,(int,float)) for x in samples) or max(samples) < 1000000): samples=[]
            uncached_total=row['uncached_total'] or (total if current==0 else total)
            uncached_completed=row['uncached_completed'] or 0; last=row['last_progress_at']
            if current==0:
                uncached_total=max(0,total); last=None; samples=[]; uncached_completed=0
            elif '(cached)' in detail or '(resumed)' in detail:
                uncached_total=max(0,uncached_total-1)
            elif last is not None:
                # Store completion timestamps; adjacent intervals represent
                # aggregate throughput and therefore already include worker
                # parallelism, throttling, retries, and jitter.
                samples=(samples+[now_t])[-50:]; uncached_completed+=1; last=now_t
            elif '(cached)' not in detail and '(resumed)' not in detail:
                samples=[now_t]; uncached_completed+=1; last=now_t
            intervals=[b-a for a,b in zip(samples,samples[1:]) if b>a]
            eta=None
            remaining=max(0,uncached_total-uncached_completed)
            if remaining==0: eta=0.0
            elif intervals: eta=statistics.median(intervals)*remaining
            c.execute('UPDATE stage_jobs SET current=?,completed=?,total=?,log=?,updated=?,last_progress_at=?,timing_samples_json=?,uncached_total=?,uncached_completed=?,eta_seconds=? WHERE id=?',(detail,current,total,out.getvalue(),now_t,last,_json.dumps(samples),uncached_total,uncached_completed,eta,job_id)); c.commit(); c.close()
        def retry(kind,attempt,max_attempts,detail):
            c=connect(project); c.execute('UPDATE stage_jobs SET retry_count=retry_count+?,failed_attempts=failed_attempts+?,updated=? WHERE id=?',(1 if kind=='failure' and attempt>1 else 0,1 if kind=='failure' else 0,time.time(),job_id)); c.commit(); c.close()
        try:
            c=connect(project); c.execute("UPDATE stage_jobs SET status='running',updated=? WHERE id=?",(time.time(),job_id)); c.commit(); c.close()
            set_progress_hook(progress); set_retry_hook(retry); set_cancel_hook(cancel_event.is_set)
            with contextlib.redirect_stdout(out):
                if reset: reset_stage(project,stage,project/'book.epub')
                elif stage=='ocr': ocr(project)
                elif stage=='correction': correct(project)
                elif stage=='assembly': assemble(project)
                elif stage=='structure': structure(project)
                elif stage=='translation': translate(project)
                elif stage=='build': build(project,project/'book.epub')
            status='completed'; error=None
        except Exception as exc:
            status='failed'; error=f'{exc.__class__.__name__}: {exc}'
        finally:
            set_progress_hook(None); set_retry_hook(None); set_cancel_hook(None); _CANCEL_EVENTS.pop(job_id,None)
        if error and 'stopped by user' in error: status='stopped'
        c=connect(project); c.execute('UPDATE stage_jobs SET status=?,log=?,error=?,updated=?,completed_at=? WHERE id=?',(status,out.getvalue(),error,time.time(),time.time(),job_id)); c.commit(); c.close()
    def launch(stage,reset=False):
        c=connect(project); active=c.execute("SELECT * FROM stage_jobs WHERE status IN ('queued','running') ORDER BY id DESC LIMIT 1").fetchone()
        if active:
            if not reset: return None,('active',active['stage'])
            ev=_CANCEL_EVENTS.get(active['id'])
            if ev: ev.set()
            c.execute("UPDATE stage_jobs SET cancel_requested=1,status='stopping',updated=? WHERE id=?",(time.time(),active['id'])); c.commit()
            deadline=time.time()+15
            while time.time()<deadline:
                time.sleep(.1)
                check=c.execute("SELECT status FROM stage_jobs WHERE id=?",(active['id'],)).fetchone()
                if not check or check[0] not in ('queued','running','stopping'): break
            check=c.execute("SELECT status FROM stage_jobs WHERE id=?",(active['id'],)).fetchone()
            if check and check[0] in ('queued','running','stopping'): return None,('active',active['stage'])
        dep=None if reset else DEPS[stage]
        if dep:
            s,done,total=stage_status(c,dep)
            if s!='completed': return None,('blocked',dep)
        c.execute('INSERT INTO stage_jobs(stage,status,reset,created,updated) VALUES(?,?,?,?,?)',(stage,'queued',int(reset),time.time(),time.time())); jid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit(); c.close()
        _CANCEL_EVENTS[jid]=threading.Event(); threading.Thread(target=run_stage,args=(jid,stage,reset),daemon=True).start(); return jid,None
    class H(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path=='/': return self._send(UI,'text/html')
            if self.path in ('/ocr','/correction','/translation'): return self._send(INSPECT_UI,'text/html')
            if self.path=='/costs': return self._send(UI,'text/html')
            if self.path=='/assembly': return self._send(ASSEMBLY_UI,'text/html')
            if self.path=='/structure': return self._send(STRUCTURE_UI,'text/html')
            if self.path=='/api/pages': return self._json([dict(x) for x in db.execute('SELECT number,image,status,confidence FROM pages ORDER BY number')])
            if self.path=='/api/overview':
                def count(sql): return db.execute(sql).fetchone()[0]
                return self._json({'source':str(project/'source.pdf'),'source_lang':self._meta('source_lang'),'target_lang':self._meta('target_lang'),'stages':[{'name':'ocr','status':'ready' if count('SELECT COUNT(*) FROM pages') else 'pending','progress':f'{count("SELECT COUNT(*) FROM pages")}/{self._meta("page_count") or "?"}','details':'page OCR'}, {'name':'correction','status':'ready' if count('SELECT COUNT(*) FROM blocks WHERE corrected_text IS NOT NULL') else 'pending','progress':f'{count("SELECT COUNT(*) FROM blocks WHERE corrected_text IS NOT NULL")}/{count("SELECT COUNT(*) FROM blocks")}', 'details':'paragraph corrections'}, {'name':'assembly','status':(db.execute('SELECT status FROM assembly_runs ORDER BY generation DESC LIMIT 1').fetchone() or ['pending'])[0],'progress':f'{count("SELECT COUNT(*) FROM assembly_windows")}', 'details':'assembly windows'}, {'name':'structure','status':(db.execute('SELECT status FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone() or ['pending'])[0],'progress':f'{count("SELECT COUNT(*) FROM structure_nodes")}', 'details':'hierarchy nodes'}, {'name':'translation','status':'ready' if count('SELECT COUNT(*) FROM blocks WHERE translation IS NOT NULL') else 'pending','progress':f'{count("SELECT COUNT(*) FROM blocks WHERE translation IS NOT NULL")}/{count("SELECT COUNT(*) FROM blocks")}', 'details':'translated blocks'}]})
            if self.path=='/api/pipeline/status':
                c=connect(project); active=c.execute("SELECT * FROM stage_jobs WHERE status IN ('queued','running') ORDER BY id DESC LIMIT 1").fetchone(); result=[]
                units={'ocr':'pages','correction':'paragraphs','assembly':'windows','structure':'chunks','translation':'blocks','build':'artifacts'}
                for stage in STAGES:
                    status,done,total=stage_status(c,stage); blocked=DEPS[stage] and stage_status(c,DEPS[stage])[0]!='completed'; job=c.execute('SELECT * FROM stage_jobs WHERE stage=? ORDER BY id DESC LIMIT 1',(stage,)).fetchone()
                    if job:
                        if job['status'] in ('queued','running','failed'): status=job['status']
                        if job['total']: done,total=job['completed'],job['total']
                    elapsed=max(0,time.time()-(job['created'] or time.time())) if job else 0
                    eta=None
                    if job and done and total and done<total: eta=max(0,elapsed/done*(total-done))
                    workers=(int(os.environ.get('BOOK2EPUB_CORRECTION_WORKERS','3')) if stage=='correction' else int(os.environ.get('BOOK2EPUB_ASSEMBLY_WORKERS','2')) if stage=='assembly' else 1)
                    timing_count=max(0,len(_json.loads(job['timing_samples_json'] or '[]'))-1) if job and 'timing_samples_json' in job.keys() and job['timing_samples_json'] else 0
                    result.append({'name':stage,'status':'blocked' if blocked and status=='not_started' else status,'completed':done,'total':total,'unit':units[stage],'workers':max(1,min(3,workers)),'current':job['current'] if job else None,'elapsed_seconds':round(elapsed,1),'eta_seconds':round(job['eta_seconds'],1) if job and job['eta_seconds'] is not None else None,'uncached_total':job['uncached_total'] if job and 'uncached_total' in job.keys() else None,'uncached_completed':job['uncached_completed'] if job and 'uncached_completed' in job.keys() else None,'timing_sample_count':timing_count,'runnable':not blocked and not active,'blocked_by':DEPS[stage] if blocked else None,'error':job['error'] if job else None,'retry_count':job['retry_count'] if job and 'retry_count' in job.keys() else 0,'metrics':usage_summary(c,stage)})
                return self._json({'source':str(project/'source.pdf'),'source_lang':meta(c,'source_lang'),'target_lang':meta(c,'target_lang'),'stages':result,'overall':usage_summary(c),'active':dict(active) if active else None})
            if self.path.startswith('/api/jobs/'):
                jid=int(self.path.rsplit('/',1)[1]); j=db.execute('SELECT * FROM stage_jobs WHERE id=?',(jid,)).fetchone(); return self._json(dict(j) if j else {})
            if self.path.startswith('/api/pages/'):
                n=int(self.path.rsplit('/',1)[1]); p=db.execute('SELECT * FROM pages WHERE number=?',(n,)).fetchone();
                if not p:return self.send_error(404)
                out=dict(p); out['blocks']=[dict(x) for x in db.execute('SELECT * FROM blocks WHERE page_id=? ORDER BY ordinal',(p['id'],))]; return self._json(out)
            if self.path.startswith('/files/'):
                f=(project/self.path.removeprefix('/files/')).resolve()
                if not f.is_relative_to(project.resolve()) or not f.exists():return self.send_error(404)
                self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(str(f))[0] or 'application/octet-stream'); self.end_headers(); self.wfile.write(f.read_bytes()); return
            if self.path=='/api/assembly/latest':
                run=db.execute('SELECT * FROM assembly_runs ORDER BY generation DESC LIMIT 1').fetchone()
                return self._json({'run':dict(run) if run else None,'units':[dict(x) for x in db.execute('SELECT * FROM manuscript_units WHERE run_id=? AND status != \'discarded\' ORDER BY ordinal',(run['id'],)).fetchall()] if run else [],'reconciliation':[dict(x) for x in db.execute('SELECT * FROM assembly_reconciliation WHERE run_id=? ORDER BY id',(run['id'],)).fetchall()] if run else []})
            if self.path=='/api/assembly/windows':
                run=db.execute('SELECT * FROM assembly_runs ORDER BY generation DESC LIMIT 1').fetchone(); ws=db.execute('SELECT window_key,status FROM assembly_windows WHERE run_id=? ORDER BY id',(run['id'],)).fetchall() if run else []
                return self._json({'run':dict(run) if run else None,'windows':[dict(x) for x in ws]})
            if self.path.startswith('/api/assembly/windows/'):
                key=self.path.rsplit('/',1)[1]; from urllib.parse import unquote
                key=unquote(key)
                try: nums=[int(x) for x in key.split(',') if x]
                except ValueError: return self.send_error(400,'Invalid assembly window key')
                if not nums: return self.send_error(400,'Empty assembly window key')
                run=db.execute('SELECT * FROM assembly_runs ORDER BY generation DESC LIMIT 1').fetchone(); pages=[dict(db.execute('SELECT number,image,text FROM pages WHERE number=?',(n,)).fetchone()) for n in nums if db.execute('SELECT 1 FROM pages WHERE number=?',(n,)).fetchone()]; units=[dict(x) for x in db.execute('SELECT * FROM manuscript_units WHERE run_id=? AND status != \'discarded\' ORDER BY ordinal',(run['id'],)).fetchall()] if run else []
                return self._json({'window_key':key,'pages':pages,'units':units})
            if self.path=='/api/structure/latest':
                run=db.execute('SELECT * FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone()
                return self._json({'run':dict(run) if run else None,'nodes':[dict(x) for x in db.execute('SELECT * FROM structure_nodes WHERE run_id=? ORDER BY ordinal',(run['id'],)).fetchall()] if run else []})
            if self.path=='/api/structure/tree':
                run=db.execute('SELECT * FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone(); return self._json({'run':dict(run) if run else None,'nodes':[dict(x) for x in db.execute('SELECT * FROM structure_nodes WHERE run_id=? ORDER BY ordinal',(run['id'],)).fetchall()] if run else []})
            self.send_error(404)
        def do_PATCH(self):
            if self.path.startswith('/api/assembly/units/'):
                uid=int(self.path.rsplit('/',1)[1]); data=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0)))); old=db.execute('SELECT text FROM manuscript_units WHERE id=?',(uid,)).fetchone()[0]; db.execute("UPDATE manuscript_units SET text=?,status='needs_review' WHERE id=?",(data.get('text',''),uid)); db.execute('INSERT INTO assembly_events(unit_id,action,old_value,new_value,created) VALUES(?,?,?,?,strftime(\'%s\',\'now\'))',(uid,'edit',old,data.get('text',''))); db.commit(); return self._json({'ok':True})
            if not self.path.startswith('/api/blocks/'):return self.send_error(404)
            bid=int(self.path.rsplit('/',1)[1]); data=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0)))); old=db.execute('SELECT approved_text FROM blocks WHERE id=?',(bid,)).fetchone()[0]; val=data.get('text',''); db.execute("UPDATE blocks SET approved_text=?,status='needs_review',revision=revision+1 WHERE id=?",(val,bid)); db.execute('INSERT INTO events(block_id,action,old_value,new_value,created) VALUES(?,?,?,?,strftime(\'%s\',\'now\'))',(bid,'edit',old,val)); db.commit(); return self._json({'ok':True})
        def do_POST(self):
            m=re.match(r'^/api/stages/([^/]+)/(run|reset)$',self.path)
            if m:
                stage,action=m.groups()
                if stage not in STAGES:return self.send_error(404)
                jid,err=launch(stage,action=='reset')
                if err:
                    self.send_response(409); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(json.dumps({'ok':False,'reason':err[0],'blocked_by':err[1]}).encode()); return
                return self._json({'ok':True,'job_id':jid})
            m=re.match(r'^/api/stages/([^/]+)/stop$',self.path)
            if m:
                stage=m.group(1)
                if stage not in STAGES:return self.send_error(404)
                j=db.execute("SELECT * FROM stage_jobs WHERE stage=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",(stage,)).fetchone()
                if not j:return self._json({'ok':False,'reason':'no active job'})
                ev=_CANCEL_EVENTS.get(j['id'])
                if ev: ev.set()
                db.execute('UPDATE stage_jobs SET cancel_requested=1,status=CASE WHEN status=\'queued\' THEN \'stopped\' ELSE \'stopping\' END,updated=? WHERE id=?',(time.time(),j['id'])); db.commit()
                return self._json({'ok':True,'job_id':j['id']})
            if self.path.startswith('/api/assembly/units/') and self.path.endswith('/approve'):
                uid=int(self.path.split('/')[4]); db.execute("UPDATE manuscript_units SET status='approved' WHERE id=?",(uid,)); db.commit(); return self._json({'ok':True})
            if self.path.startswith('/api/blocks/') and self.path.endswith('/approve'):
                bid=int(self.path.split('/')[3]); db.execute("UPDATE blocks SET status='approved' WHERE id=?",(bid,)); db.commit(); return self._json({'ok':True})
            self.send_error(404)
        def _json(self,x):self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(x,ensure_ascii=False).encode())
        def _meta(self,key):
            r=db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
            try:return json.loads(r[0]) if r else None
            except:return None
        def _send(self,x,t):self.send_response(200);self.send_header('Content-Type',t);self.end_headers();self.wfile.write(x.encode())
    print(f'http://{host}:{port}/'); ThreadingHTTPServer((host,port),H).serve_forever()
