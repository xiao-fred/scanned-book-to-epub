from __future__ import annotations
import hashlib, html, json, os, re, shutil, subprocess, uuid, zipfile, random, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .db import connect, meta_get, meta_set, cache_key, now, record_usage
from .translation import translate_segment, load_dotenv

_PROGRESS_HOOK = None
_CANCEL_HOOK = None
_WORKER_LOCAL = threading.local()

def set_progress_hook(hook):
    """Install a short-lived observer for live UI job progress."""
    global _PROGRESS_HOOK
    _PROGRESS_HOOK = hook

def set_cancel_hook(hook):
    global _CANCEL_HOOK
    _CANCEL_HOOK = hook

def _check_cancelled():
    if _CANCEL_HOOK and _CANCEL_HOOK():
        raise RuntimeError('stage stopped by user')

def _progress(stage, current, total, detail=''):
    suffix = f' — {detail}' if detail else ''
    print(f'[{stage}] {current}/{total}{suffix}', flush=True)
    if _PROGRESS_HOOK:
        try: _PROGRESS_HOOK(stage, current, total, detail)
        except Exception: pass

def need(cmd):
    if not shutil.which(cmd): raise RuntimeError(f'Required command not found: {cmd}')
def _workers(name, default):
    try: return max(1, min(3, int(os.environ.get(name, default))))
    except (TypeError, ValueError): return default
def _start_jitter(name, default=2.0):
    try: return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError): return default
def _worker_start_jitter(name):
    key='jitter_'+name
    if not getattr(_WORKER_LOCAL, key, False):
        delay=random.uniform(0, _start_jitter(name));
        if delay: time.sleep(delay)
        setattr(_WORKER_LOCAL, key, True)
def init_project(project,pdf,source_lang=None,target_lang=None):
    project.mkdir(parents=True,exist_ok=True); src=project/'source.pdf'
    if not src.exists(): shutil.copy2(pdf,src)
    db=connect(project); meta_set(db,'title',pdf.stem); meta_set(db,'source',str(src)); meta_set(db,'source_sha256',hashlib.sha256(src.read_bytes()).hexdigest())
    if source_lang: meta_set(db,'source_lang',source_lang)
    if target_lang: meta_set(db,'target_lang',target_lang)
    db.close()
    print(f'[init] project ready: {project}', flush=True)

def reset_project(project):
    """Reset all derived processing while retaining source PDF and languages."""
    db=connect(project); source=meta_get(db,'source',str(project/'source.pdf')); source_lang=meta_get(db,'source_lang',None); target_lang=meta_get(db,'target_lang',None)
    source_path=Path(source)
    if not source_path.is_absolute(): source_path=project/source_path.name
    if not source_path.is_file() and (project/'source.pdf').is_file(): source_path=project/'source.pdf'
    db.close()
    for child in list(project.iterdir()):
        if child == source_path or child == project/'source.pdf': continue
        if child.is_dir(): shutil.rmtree(child)
        else: child.unlink()
    db=connect(project)
    for table in ('blocks','pages','events','cache','usage_events','assembly_events','assembly_windows','manuscript_units','assembly_runs'):
        db.execute(f'DELETE FROM {table}')
    db.execute('DELETE FROM metadata')
    meta_set(db,'source',str(source_path)); meta_set(db,'source_sha256',hashlib.sha256(source_path.read_bytes()).hexdigest())
    if source_lang: meta_set(db,'source_lang',source_lang)
    if target_lang: meta_set(db,'target_lang',target_lang)
    db.close(); print(f'[reset] retained source PDF and language configuration in {project}',flush=True)

def reset_stage(project, stage, output=None):
    """Reset one stage only; this function never starts processing."""
    db=connect(project)
    db.execute('DELETE FROM usage_events WHERE stage=?',(stage,))
    db.execute('UPDATE stage_jobs SET completed=0,total=0,current=NULL,retry_count=0,failed_attempts=0,error=NULL,cancel_requested=0,last_progress_at=NULL,timing_samples_json=NULL,uncached_total=0,uncached_completed=0,eta_seconds=NULL WHERE stage=?',(stage,))
    if stage=='ocr':
        db.execute('DELETE FROM blocks'); db.execute('DELETE FROM pages'); db.execute("DELETE FROM metadata WHERE key IN ('page_count','debug_scope')")
        for folder in (project/'pages', project/'ocr'):
            if folder.is_dir():
                for item in folder.glob('page-*'):
                    if item.is_file(): item.unlink()
    elif stage=='correction':
        meta_set(db,'correction_generation',int(meta_get(db,'correction_generation','0'))+1)
        db.execute("UPDATE blocks SET corrected_text=NULL, correction_json=NULL, corrected_kind=NULL, corrected_heading_level=NULL, corrected_confidence=NULL, status='needs_review'")
        db.execute("DELETE FROM metadata WHERE key='correction_debug_scope'")
    elif stage=='translation':
        db.execute("UPDATE blocks SET translation=NULL, status='needs_review'")
    elif stage=='assembly':
        db.execute('DELETE FROM assembly_reconciliation'); db.execute('DELETE FROM assembly_events'); db.execute('DELETE FROM manuscript_units'); db.execute('DELETE FROM assembly_windows'); db.execute('DELETE FROM assembly_runs')
    elif stage=='structure':
        db.execute('DELETE FROM structure_nodes'); db.execute('DELETE FROM structure_chunks'); db.execute('DELETE FROM structure_runs')
    elif stage=='build':
        target=Path(output) if output else project/'book.epub'
        if target.exists(): target.unlink()
    else:
        db.close(); raise ValueError(f'Unknown reset stage: {stage}')
    db.commit(); db.close(); print(f'[reset] {stage} reset complete; processing was not started',flush=True)
def ocr(project,lang='eng',start=1,end=None,debug=False,sample_pages=2,seed=None,groups='beginning,middle,end',reset=False,middle_pages=2):
    for c in ('pdfinfo','pdftoppm','tesseract'): need(c)
    db=connect(project); lang=lang or meta_get(db,'source_lang','eng'); source=Path(meta_get(db,'source',str(project/'source.pdf'))); pages=project/'pages'; pages.mkdir(exist_ok=True); texts=project/'ocr'; texts.mkdir(exist_ok=True)
    info=subprocess.check_output(['pdfinfo',str(source)],text=True); n=int(re.search(r'^Pages:\s+(\d+)',info,re.M).group(1)); meta_set(db,'page_count',n); end=end or n
    selected=list(range(start,end+1))
    if debug:
        import random
        rng=random.Random(seed or int(meta_get(db,'source_sha256','0')[:8],16)); selected=[]; gs=set(groups.split(','))
        if 'beginning' in gs: selected += list(range(start,min(end,start+sample_pages-1)+1))
        if 'end' in gs: selected += list(range(max(start,end-sample_pages+1),end+1))
        if 'middle' in gs:
            count=min(max(1,middle_pages),end-start+1); center=(start+end)//2; first=max(start,min(center-count//2,end-count+1)); selected += list(range(first,first+count))
        if 'random' in gs: selected += rng.sample(list(range(start,end+1)),min(sample_pages,end-start+1))
        selected=sorted(set(selected)); meta_set(db,'debug_scope',{'pages':selected,'seed':seed,'groups':groups,'middle_pages':middle_pages})
    total = len(selected)
    _progress('ocr', 0, total, 'pages selected')
    for number, i in enumerate(selected, 1):
        _check_cancelled()
        image=pages/f'page-{i:05d}.png'; txt=texts/f'page-{i:05d}.txt'
        if not image.exists():
            subprocess.run(['pdftoppm','-f',str(i),'-l',str(i),'-png','-r','180',str(source),str(pages/'page')],check=True)
            # Poppler's zero-padding varies by version (page-1.png, page-01.png,
            # page-001.png, ...). Normalize whichever file it generated.
            candidates=[p for p in pages.glob('page-*.png') if p.stem.rsplit('-',1)[-1].isdigit() and int(p.stem.rsplit('-',1)[-1])==i]
            if candidates:
                candidates[0].rename(image)
        if not image.exists():
            raise RuntimeError(f'pdftoppm did not produce an image for page {i}; inspect {pages}')
        if not txt.exists(): subprocess.run(['tesseract',str(image),str(txt.with_suffix('')),'-l',lang,'--psm','3'],check=True)
        raw=txt.read_text(encoding='utf8',errors='replace'); row=db.execute('SELECT id FROM pages WHERE number=?',(i,)).fetchone(); page=row[0] if row else db.execute('INSERT INTO pages(number,image,text,status) VALUES(?,?,?,?)',(i,str(image.relative_to(project)),raw,'needs_review')).lastrowid
        if not db.execute('SELECT 1 FROM blocks WHERE page_id=?',(page,)).fetchone():
            for j,b in enumerate(re.split(r'\n\s*\n',raw)):
                if b.strip(): db.execute('INSERT INTO blocks(page_id,ordinal,text,approved_text) VALUES(?,?,?,?)',(page,j,b.strip(),b.strip()))
        db.execute("UPDATE pages SET status='ocr_done' WHERE id=?",(page,)); db.commit()
        _progress('ocr', number, total, f'page {i}')
    meta_set(db,'source_lang',lang); db.close()
def clean(text): return re.sub(r'\n{3,}','\n\n',re.sub(r'[ \t]+\n','\n',re.sub(r'(?<=\w)-\n(?=\w)','',text))).strip()
def _split_long(text, limit=200):
    words=text.split(); out=[]
    while len(words)>limit:
        cut=limit
        while cut < len(words) and not re.search(r'[.!?]["»”\']?$', words[cut-1]): cut+=1
        out.append(' '.join(words[:cut])); words=words[cut:]
    if words: out.append(' '.join(words))
    return out

def _provenance_refs(value):
    """Normalize legacy and structured provenance to page/block references."""
    try: raw=json.loads(value) if isinstance(value,str) else (value or [])
    except Exception: raw=[]
    if isinstance(raw,(str,int)): raw=[raw]
    out=[]
    for item in raw:
        if isinstance(item,dict) and item.get('page') is not None and item.get('block') is not None:
            out.append({'page':int(item['page']),'block':int(item['block']),'key':f"{item['page']}:{item['block']}"})
        elif isinstance(item,str) and ':' in item:
            p,b=item.split(':',1)
            if p.isdigit() and b.isdigit(): out.append({'page':int(p),'block':int(b),'key':item})
    return out
def correct(project,provider='efficiency-center',model=None,debug=False,sample_pages=2,seed=None,groups='beginning,middle,end',batch_pages=1,source_lang=None,reset=False,middle_pages=2):
    load_dotenv(Path.cwd()/'.env')
    db=connect(project); source=source_lang or meta_get(db,'source_lang','eng')
    if source_lang: meta_set(db,'source_lang',source_lang)
    generation=int(meta_get(db,'correction_generation','0'))
    pages=db.execute("SELECT * FROM pages WHERE text IS NOT NULL ORDER BY number").fetchall(); selected={p['number'] for p in pages}
    if debug:
        import random
        n=int(meta_get(db,'page_count',len(pages))); rng=random.Random(seed or int(meta_get(db,'source_sha256','0')[:8],16)); selected=set(); gs=set(groups.split(','))
        if 'beginning' in gs:selected.update(range(1,min(n,sample_pages)+1))
        if 'end' in gs:selected.update(range(max(1,n-sample_pages+1),n+1))
        if 'middle' in gs:
            count=min(max(1,middle_pages),n); center=(1+n)//2; first=max(1,min(center-count//2,n-count+1)); selected.update(range(first,first+count))
        if 'random' in gs:selected.update(rng.sample(range(1,n+1),min(sample_pages,n)))
        meta_set(db,'correction_debug_scope',{'pages':sorted(selected),'seed':seed,'sample_pages':sample_pages,'middle_pages':middle_pages,'groups':groups})
    model=model or os.environ.get('BOOK2EPUB_CORRECTION_MODEL')
    if not model:
        raise RuntimeError('Set BOOK2EPUB_CORRECTION_MODEL in the project .env or pass --model')
    work = [p for p in pages if p['number'] in selected]
    targets=[(p,b) for p in work for b in db.execute('SELECT * FROM blocks WHERE page_id=? ORDER BY ordinal',(p['id'],)).fetchall()]
    _progress('correction', 0, len(targets), 'paragraphs')
    prompt='Return JSON with corrected_text, kind (paragraph, heading, subheading, blockquote, list_item, caption, footnote, table, page_furniture, unknown), heading_level, confidence, attributes, changes, unresolved. Correct and classify only this paragraph using the page image for visual context. Preserve meaning and never invent illegible text.'
    def correct_one(item):
        p,b=item; raw=b['text'] or ''
        if not raw: return p,b,{'corrected_text':'','kind':'paragraph','confidence':1}, {'input_tokens':0,'output_tokens':0,'cost':0,'currency':'USD','model':'empty'}, 1, None
        image=project/p['image']; key=cache_key('correction',raw,source,provider,model,f'v{generation}',str(image),str(b['id']))
        c=connect(project); cached=c.execute('SELECT value FROM cache WHERE key=?',(key,)).fetchone(); c.close()
        if cached: return p,b,json.loads(cached[0]),{'input_tokens':0,'cached_input_tokens':0,'output_tokens':0,'cost':0,'currency':'USD','model':'cached','service_usages':[]},1,key
        _worker_start_jitter('BOOK2EPUB_CORRECTION_START_JITTER')
        out,usage=translate_segment(raw,source,source,provider,project/'.env',image,prompt,model)
        try: result=json.loads(out)
        except json.JSONDecodeError: result={'corrected_text':out,'kind':'paragraph','confidence':0.5,'changes':[],'unresolved':['model returned non-JSON']}
        return p,b,result,usage,0,key
    with ThreadPoolExecutor(max_workers=_workers('BOOK2EPUB_CORRECTION_WORKERS',3)) as pool:
      futures=[pool.submit(correct_one,t) for t in targets]
      for number,future in enumerate(futures,1):
        _check_cancelled(); p,b,result,usage,hit,key=future.result()
        if key: db.execute('INSERT OR REPLACE INTO cache(key,value,created) VALUES(?,?,?)',(key,json.dumps(result,ensure_ascii=False),now()))
        raw=b['text'] or ''
        corrected=result.get('corrected_text',raw)
        db.execute("UPDATE blocks SET corrected_text=?,correction_json=?,corrected_kind=?,corrected_heading_level=?,corrected_confidence=?,status='needs_review',revision=revision+1 WHERE id=?",(corrected,json.dumps(result,ensure_ascii=False),result.get('kind','paragraph'),result.get('heading_level'),result.get('confidence'),b['id']))
        db.commit(); record_usage(db,run_id='correction',stage='correction',page_id=p['id'],block_id=b['id'],request_key=key,provider=provider,model=usage.get('model',model),input_tokens=usage.get('input_tokens',0),cached_input_tokens=usage.get('cached_input_tokens',0),output_tokens=usage.get('output_tokens',0),total_cost=usage.get('cost',0),currency=usage.get('currency','USD'),cache_hit=hit,details_json=json.dumps(usage.get('service_usages',[]),ensure_ascii=False),trace_id=usage.get('trace_id'),created=now())
        _progress('correction', number, len(targets), f'paragraph {b["id"]}' + (' (cached)' if hit else ''))
    db.close()
def translate(project,target=None,provider='hosted',run_id='translate',reset=False):
    load_dotenv(Path.cwd()/'.env')
    db=connect(project); source=meta_get(db,'source_lang','eng')
    target=target or meta_get(db,'target_lang',None)
    if not target: raise RuntimeError('Set a target language with --target or store target_lang in the project')
    rows = db.execute("SELECT id,text,approved_text FROM blocks WHERE translation IS NULL").fetchall()
    _progress('translation', 0, len(rows), 'blocks')
    for number, r in enumerate(rows, 1):
        _check_cancelled()
        text=clean(r['approved_text'] or r['text']); key=cache_key(text,source,target,provider,'v1'); c=db.execute('SELECT value FROM cache WHERE key=?',(key,)).fetchone()
        if c: out=c[0]; usage={'input_tokens':0,'cached_input_tokens':0,'output_tokens':0,'cost':0.0,'model':'cached'}; hit=1
        else: out,usage=translate_segment(text,source,target,provider,project/'.env'); hit=0
        db.execute('INSERT OR REPLACE INTO cache(key,value,created) VALUES(?,?,?)',(key,out,now())); db.execute("UPDATE blocks SET translation=?,status='needs_review',revision=revision+1 WHERE id=?",(out,r['id'])); db.commit()
        record_usage(db,run_id=run_id,stage='translation',block_id=r['id'],request_key=key,provider=provider,model=usage.get('model','unknown'),input_tokens=usage.get('input_tokens',0),cached_input_tokens=usage.get('cached_input_tokens',0),output_tokens=usage.get('output_tokens',0),total_cost=usage.get('cost',0),currency=usage.get('currency','USD'),cache_hit=hit,created=now())
        _progress('translation', number, len(rows), f'block {r["id"]}' + (' (cached)' if hit else ''))
    meta_set(db,'target_lang',target); db.close()

def assemble(project,provider='hosted',model=None,window_size=3,reset=False):
    """Assemble only consecutive page windows into reviewable manuscript units."""
    load_dotenv(Path.cwd()/'.env'); db=connect(project); source=meta_get(db,'source_lang','eng')
    model=model or os.environ.get('BOOK2EPUB_ASSEMBLY_MODEL') or os.environ.get('BOOK2EPUB_CORRECTION_MODEL')
    if not model: raise RuntimeError('Set BOOK2EPUB_ASSEMBLY_MODEL or BOOK2EPUB_CORRECTION_MODEL')
    prior=db.execute('SELECT * FROM assembly_runs ORDER BY generation DESC LIMIT 1').fetchone()
    if prior and not reset and prior['source_lang']==source and prior['model']==model and prior['window_size']==window_size and prior['status']!='completed':
        generation,run=prior['generation'],prior['id']; print(f'[assembly] resuming generation {generation}',flush=True)
    else:
        generation=(db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM assembly_runs').fetchone()[0])
        run=db.execute('INSERT INTO assembly_runs(generation,source_lang,provider,model,window_size,status,created) VALUES(?,?,?,?,?,?,?)',(generation,source,provider,model,window_size,'needs_review',now())).lastrowid
    pages=db.execute('SELECT * FROM pages WHERE text IS NOT NULL ORDER BY number').fetchall()
    furniture_counts={}
    for p in pages:
        bs=db.execute('SELECT * FROM blocks WHERE page_id=? ORDER BY ordinal',(p['id'],)).fetchall()
        edge=[bs[0],bs[-1]] if bs else []
        for b in edge:
            norm=re.sub(r'\s+',' ',(b['corrected_text'] or b['approved_text'] or b['text'] or '').strip().lower())
            if norm: furniture_counts[norm]=furniture_counts.get(norm,0)+1
    repeated_furniture={k for k,v in furniture_counts.items() if v >= 3}
    groups=[]
    for p in pages:
        if not groups or p['number'] != groups[-1][-1]['number']+1: groups.append([p])
        else: groups[-1].append(p)
    stride=max(1,window_size-1)
    windows=[group[i:i+window_size] for group in groups for i in range(0,len(group),stride)]
    _progress('assembly',0,len(windows),'consecutive page windows')
    def window_payload(window):
        parts=[]; provenance=[]; removed=[]
        for p in window:
            page_blocks=db.execute('SELECT * FROM blocks WHERE page_id=? ORDER BY ordinal',(p['id'],)).fetchall()
            for b in page_blocks:
                txt=b['corrected_text'] or b['approved_text'] or b['text'] or ''; norm=re.sub(r'\s+',' ',txt.strip().lower())
                if norm in repeated_furniture and (b['ordinal']==page_blocks[0]['ordinal'] or b['ordinal']==page_blocks[-1]['ordinal']): removed.append({'page':p['number'],'block':b['id'],'text':txt,'reason':'repeated edge text'}); continue
                parts.append(f'[[page={p["number"]} block={b["id"]}]]\n{txt}'); provenance.append({'page':p['number'],'block':b['id']})
        source_text='\n\n'.join(parts); window_key=','.join(str(p['number']) for p in window); key=cache_key('assembly',source_text,source,provider,model,str(generation),window_key)
        prompt=('Return JSON {"units":[{"text":...,"kind":"paragraph|heading|caption|footnote","source_blocks":[...],"confidence":...}],"removed":[...],"joins":[...],"unresolved":[...]}. Assemble only this consecutive page window. Remove repeated headers/footers, join page-boundary words/sentences, and preserve order. Do not invent text. Deterministic furniture candidates are: '+json.dumps(removed,ensure_ascii=False))
        return source_text, provenance, removed, key, prompt
    payloads={','.join(str(p['number']) for p in w):window_payload(w) for w in windows
              if not db.execute('SELECT 1 FROM assembly_windows WHERE run_id=? AND window_key=?',(run,','.join(str(p['number']) for p in w))).fetchone()}
    def request_window(item):
        wk, data=item; source_text, provenance, removed, key, prompt=data
        if not source_text: return wk, {'units':[],'removed':removed,'joins':[],'unresolved':[]}, {'input_tokens':0,'output_tokens':0,'cost':0,'currency':'USD','model':'empty'}, 1
        c=connect(project); cached=c.execute('SELECT value FROM cache WHERE key=?',(key,)).fetchone(); c.close()
        if cached: return wk,json.loads(cached[0]),{'input_tokens':0,'output_tokens':0,'cost':0,'currency':'USD','model':'cached'},1
        _worker_start_jitter('BOOK2EPUB_ASSEMBLY_START_JITTER')
        out,usage=translate_segment(source_text,source,source,provider,project/'.env',None,prompt,model)
        try: result=json.loads(out)
        except json.JSONDecodeError: result={'units':[{'text':source_text,'kind':'paragraph','source_blocks':provenance,'confidence':0.4}],'removed':removed,'joins':[],'unresolved':['model returned non-JSON']}
        return wk,result,usage,0
    assembly_futures={}
    with ThreadPoolExecutor(max_workers=_workers('BOOK2EPUB_ASSEMBLY_WORKERS',2)) as pool:
        for wk,data in payloads.items(): assembly_futures[wk]=pool.submit(request_window,(wk,data))
        # Futures run while the deterministic integration loop below consumes them.
    ordinal=0; seen_blocks=set()
    for n,window in enumerate(windows,1):
        _check_cancelled()
        parts=[]; provenance=[]
        removed=[]
        for p in window:
            page_blocks=db.execute('SELECT * FROM blocks WHERE page_id=? ORDER BY ordinal',(p['id'],)).fetchall()
            for b in page_blocks:
                txt=b['corrected_text'] or b['approved_text'] or b['text'] or ''
                norm=re.sub(r'\s+',' ',txt.strip().lower())
                if norm in repeated_furniture and (b['ordinal']==page_blocks[0]['ordinal'] or b['ordinal']==page_blocks[-1]['ordinal']):
                    removed.append({'page':p['number'],'block':b['id'],'text':txt,'reason':'repeated edge text'}); continue
                parts.append(f'[[page={p["number"]} block={b["id"]}]]\n{txt}')
                provenance.append({'page':p['number'],'block':b['id']})
        source_text='\n\n'.join(parts)
        prompt=('Return JSON {"units":[{"text":...,"kind":"paragraph|heading|caption|footnote",'
                '"source_blocks":[...],"confidence":...}],"removed":[...],"joins":[...],"unresolved":[...]}. '
                'Assemble only this consecutive page window. Remove repeated headers/footers, join page-boundary words/sentences, and preserve order. '
                'Do not invent text; report ambiguity in unresolved. Deterministic furniture candidates are: '+json.dumps(removed,ensure_ascii=False))
        key=cache_key('assembly',source_text,source,provider,model,str(generation),','.join(str(p['number']) for p in window))
        window_key=','.join(str(p['number']) for p in window)
        if db.execute('SELECT 1 FROM assembly_windows WHERE run_id=? AND window_key=?',(run,window_key)).fetchone():
            _progress('assembly',n,len(windows),f'pages {window[0]["number"]}-{window[-1]["number"]} (resumed)'); continue
        if window_key not in assembly_futures:
            _progress('assembly',n,len(windows),f'pages {window[0]["number"]}-{window[-1]["number"]} (resumed)'); continue
        _,result,usage,hit=assembly_futures[window_key].result()
        result['removed']=removed + result.get('removed',[])
        if not hit: db.execute('INSERT OR REPLACE INTO cache(key,value,created) VALUES(?,?,?)',(key,json.dumps(result,ensure_ascii=False),now()))
        if 'removed' not in result: result['removed']=removed
        for unit in result.get('units',[]):
            refs=_provenance_refs(unit.get('source_blocks',provenance))
            ref_ids={r.get('block') if isinstance(r,dict) else r for r in refs}
            if ref_ids and ref_ids <= seen_blocks: continue
            kind=unit.get('kind','paragraph'); chunks=_split_long(unit.get('text',''),int(os.environ.get('BOOK2EPUB_ASSEMBLY_MAX_WORDS','200'))) if kind=='paragraph' else [unit.get('text','')]
            for chunk in chunks:
                ordinal+=1; db.execute('INSERT INTO manuscript_units(run_id,ordinal,kind,text,provenance_json,decisions_json,confidence,status) VALUES(?,?,?,?,?,?,?,?)',(run,ordinal,kind,chunk,json.dumps(refs,ensure_ascii=False),json.dumps({'removed':result.get('removed',[]),'joins':result.get('joins',[]),'unresolved':result.get('unresolved',[]),'split_from_long_paragraph':len(chunks)>1,'heading_level':unit.get('heading_level')},ensure_ascii=False),unit.get('confidence'), 'needs_review'))
            seen_blocks.update(ref_ids)
        db.execute('INSERT OR REPLACE INTO assembly_windows(run_id,window_key,result_json,status) VALUES(?,?,?,?)',(run,window_key,json.dumps(result,ensure_ascii=False),'completed'))
        db.commit(); _progress('assembly',n,len(windows),f'pages {window[0]["number"]}-{window[-1]["number"]}' + (' (cached)' if hit else ''))
    # Global reconciliation across overlapping windows: keep the candidate
    # with the most complete provenance coverage and record every discard.
    candidates=db.execute('SELECT * FROM manuscript_units WHERE run_id=? ORDER BY ordinal',(run,)).fetchall(); owners={}; discard=[]
    for unit in candidates:
        refs=_provenance_refs(unit['provenance_json']); keys={r['key'] for r in refs}
        conflicts=[owners[k] for k in keys if k in owners]
        if not conflicts:
            for k in keys: owners[k]=unit['id']
            db.execute('UPDATE manuscript_units SET provenance_json=? WHERE id=?',(json.dumps(refs,ensure_ascii=False),unit['id'])); continue
        existing_ids=set(conflicts); existing=db.execute('SELECT * FROM manuscript_units WHERE id=?',(next(iter(existing_ids)),)).fetchone()
        old_refs=_provenance_refs(existing['provenance_json']); old_keys={r['key'] for r in old_refs}
        score=(len(keys),len(unit['text'] or ''),float(unit['confidence'] or 0)); old_score=(len(old_keys),len(existing['text'] or ''),float(existing['confidence'] or 0))
        if score>old_score:
            db.execute("UPDATE manuscript_units SET status='needs_review' WHERE id=?",(unit['id'],))
            for oid in existing_ids:
                db.execute("UPDATE manuscript_units SET status='discarded' WHERE id=?",(oid,)); db.execute('INSERT INTO assembly_reconciliation(run_id,candidate_unit_id,final_unit_id,action,reason,evidence_json,created) VALUES(?,?,?,?,?,?,?)',(run,oid,unit['id'],'discarded_duplicate','less complete provenance',json.dumps({'kept_score':score,'discarded_score':old_score}),now()))
            for k in old_keys: owners[k]=unit['id']
        else:
            db.execute("UPDATE manuscript_units SET status='discarded' WHERE id=?",(unit['id'],)); db.execute('INSERT INTO assembly_reconciliation(run_id,candidate_unit_id,final_unit_id,action,reason,evidence_json,created) VALUES(?,?,?,?,?,?,?)',(run,unit['id'],existing['id'],'discarded_duplicate','less complete provenance',json.dumps({'kept_score':old_score,'discarded_score':score}),now()))
    db.execute('UPDATE assembly_runs SET status=?,completed=? WHERE id=?',('needs_review',now(),run)); db.commit(); db.close()

def structure(project, model=None, reset=False, chunk_size=80, debug=False):
    """Infer a compact chapter/section hierarchy from assembled units."""
    load_dotenv(Path.cwd()/'.env'); db=connect(project)
    model=model or os.environ.get('BOOK2EPUB_STRUCTURE_MODEL') or os.environ.get('BOOK2EPUB_ASSEMBLY_MODEL') or os.environ.get('BOOK2EPUB_CORRECTION_MODEL')
    if not model: raise RuntimeError('Set BOOK2EPUB_STRUCTURE_MODEL, BOOK2EPUB_ASSEMBLY_MODEL, or BOOK2EPUB_CORRECTION_MODEL')
    assembly=db.execute("SELECT * FROM assembly_runs ORDER BY generation DESC LIMIT 1").fetchone()
    units=db.execute("SELECT * FROM manuscript_units WHERE run_id=? ORDER BY ordinal",(assembly['id'],)).fetchall() if assembly else []
    if not units: raise RuntimeError('Run assemble before structure')
    prior=db.execute('SELECT * FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone()
    if prior and not reset and prior['model']==model and prior['assembly_run_id']==assembly['id'] and prior['status']!='completed': run_id,generation=prior['id'],prior['generation']; print(f'[structure] resuming generation {generation}',flush=True)
    else:
        generation=(db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM structure_runs').fetchone()[0]); run_id=db.execute('INSERT INTO structure_runs(generation,source_lang,target_lang,model,status,assembly_run_id,created) VALUES(?,?,?,?,?,?,?)',(generation,meta_get(db,'source_lang','eng'),meta_get(db,'target_lang'),model,'running',assembly['id'],now())).lastrowid
    chunks=[units[i:i+chunk_size] for i in range(0,len(units),chunk_size)]
    _progress('structure',0,len(chunks),'chunks')
    summaries=[]
    for n,chunk in enumerate(chunks,1):
        _check_cancelled()
        key=f'{chunk[0]["ordinal"]}-{chunk[-1]["ordinal"]}'; cached=db.execute('SELECT result_json FROM structure_chunks WHERE run_id=? AND chunk_key=?',(run_id,key)).fetchone()
        if cached: result=json.loads(cached[0]); hit=True
        else:
            text='\n'.join(f'[{u["ordinal"]}] ({u["kind"]}) {u["text"]}' for u in chunk)
            prompt=('Return JSON {"headings":[{"title":...,"node_type":"chapter|section", "unit": number, "heading_level": number, "confidence": number, "evidence":...}], "toc_candidates": [...], "unresolved": [...]}. '
                    'Identify high-level chapters and lower-level sections. Exclude running headers, footers, captions, and page furniture. Prefer source headings and meaningful boundaries.')
            out,usage=translate_segment(text,meta_get(db,'source_lang','eng'),meta_get(db,'source_lang','eng'),'hosted',project/'.env',None,prompt,model)
            record_usage(db,run_id=f'structure-{run_id}',stage='structure',request_key=cache_key('structure',key),provider='hosted',model=usage.get('model',model),input_tokens=usage.get('input_tokens',0),cached_input_tokens=usage.get('cached_input_tokens',0),output_tokens=usage.get('output_tokens',0),total_cost=usage.get('cost',0),currency=usage.get('currency','USD'),cache_hit=0,details_json=json.dumps(usage.get('service_usages',[]),ensure_ascii=False),trace_id=usage.get('trace_id'),created=now())
            try: result=json.loads(out)
            except json.JSONDecodeError: result={'headings':[],'toc_candidates':[],'unresolved':['model returned non-JSON']}
            db.execute('INSERT OR REPLACE INTO structure_chunks(run_id,chunk_key,result_json,status) VALUES(?,?,?,?)',(run_id,key,json.dumps(result,ensure_ascii=False),'completed')); db.commit(); hit=False
        summaries.extend(result.get('headings',[])); _progress('structure',n,len(chunks),f'units {key}' + (' (cached)' if hit else ''))
    # Global reconciliation is deterministic over candidates; this keeps the
    # stage resumable and avoids an unbounded full-book request.
    db.execute('DELETE FROM structure_nodes WHERE run_id=?',(run_id,)); summaries=sorted(summaries,key=lambda x:x.get('unit',0)); seen=set(); ordinal=0; stack=[]
    for h in summaries:
        title=re.sub(r'\s+',' ',str(h.get('title','')).strip()); unit=int(h.get('unit',0) or 0)
        norm=title.casefold()
        if not title or norm in seen or h.get('node_type') not in ('chapter','section'): continue
        seen.add(norm); ordinal+=1; level=int(h.get('heading_level') or (1 if h.get('node_type')=='chapter' else 2)); parent_id=stack[level-2][0] if level>1 and len(stack)>=level-1 else None
        cur=db.execute('INSERT INTO structure_nodes(run_id,ordinal,parent_id,node_type,title,source_derived,heading_level,start_unit,end_unit,confidence,evidence_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(run_id,ordinal,parent_id,h.get('node_type','section'),title,1,level,unit,None,h.get('confidence'),json.dumps(h.get('evidence',{}),ensure_ascii=False),'needs_review')).lastrowid
        stack=stack[:level-1]; stack.append((cur,level))
    db.execute('UPDATE structure_runs SET status=?,completed=? WHERE id=?',('needs_review',now(),run_id)); db.commit(); db.close(); print(f'[structure] generated {ordinal} nodes; inspect with /structure',flush=True)

def build(project,out,reset=False):
    db=connect(project); title=meta_get(db,'title','Scanned book'); lang=meta_get(db,'target_lang',meta_get(db,'source_lang','eng')); run=db.execute("SELECT id FROM assembly_runs WHERE status='approved' OR (status='needs_review' AND NOT EXISTS (SELECT 1 FROM manuscript_units u WHERE u.run_id=assembly_runs.id AND u.status!='approved')) ORDER BY generation DESC LIMIT 1").fetchone()
    if run:
        rows=db.execute("SELECT kind,text,decisions_json FROM manuscript_units WHERE run_id=? AND status='approved' ORDER BY ordinal",(run['id'],)).fetchall(); body=''.join((f'<h{min(6,max(1,(json.loads(r["decisions_json"] or "{}").get("heading_level") or 2)))}>{html.escape(r["text"])}</h{min(6,max(1,(json.loads(r["decisions_json"] or "{}").get("heading_level") or 2)))}>' if r['kind'] in ('heading','subheading') else f'<blockquote>{html.escape(r["text"])}</blockquote>' if r['kind']=='blockquote' else f'<p>{html.escape(r["text"]).replace(chr(10)," ")}</p>') for r in rows)
    else:
        rows=db.execute("SELECT b.approved_text,b.corrected_text,b.translation FROM blocks b WHERE b.status='approved' ORDER BY b.page_id,b.ordinal").fetchall(); body=''.join(f'<p>{html.escape((r["translation"] or r["corrected_text"] or r["approved_text"] or "").replace(chr(10)," "))}</p>' for r in rows)
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('mimetype',b'application/epub+zip',compress_type=zipfile.ZIP_STORED); z.writestr('META-INF/container.xml','<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'); z.writestr('OEBPS/style.css','body{font-family:serif;line-height:1.5;margin:6%;}p{text-align:justify;}'); z.writestr('OEBPS/chapter-001.xhtml',f'<html xmlns="http://www.w3.org/1999/xhtml" lang="{html.escape(lang)}"><head><title>{html.escape(title)}</title><link rel="stylesheet" href="style.css"/></head><body><h1>{html.escape(title)}</h1>{body}</body></html>'); z.writestr('OEBPS/nav.xhtml','<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol><li><a href="chapter-001.xhtml">Text</a></li></ol></nav></body></html>'); z.writestr('OEBPS/package.opf',f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:uuid:{uuid.uuid4()}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>{html.escape(lang)}</dc:language></metadata><manifest><item id="c" href="chapter-001.xhtml" media-type="application/xhtml+xml"/><item id="n" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="s" href="style.css" media-type="text/css"/></manifest><spine><itemref idref="c"/></spine></package>')
    db.close()
    print(f'[build] wrote {out}', flush=True)
