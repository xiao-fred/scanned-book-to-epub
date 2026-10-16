from __future__ import annotations
import hashlib, html, json, os, re, shutil, subprocess, uuid, zipfile, random, threading, time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from .db import connect, meta_get, meta_set, cache_key, now, record_usage
from .refinement import make_batches, make_request, make_global_request, request_bytes, REQUEST_LIMIT
from .toc import detect_toc_candidates, parse_toc_entries, match_toc_to_body, toc_quality_gate, image_confirmation_prompt

# Bump this whenever the assembly prompt or materialization semantics change.
# Assembly responses are cached by content, so without an explicit algorithm
# version a reset can replay results produced by an older implementation.
ASSEMBLY_CACHE_VERSION = 'assembly-v7-sentence-bounded-paragraphs'
CORRECTION_CACHE_VERSION = 'correction-v3-evidence'
EVIDENCE_VERSION = 'evidence-v1'
DEFAULT_STRUCTURE_MAX_ROUNDS = 10
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

def _structure_max_rounds():
    try: return min(DEFAULT_STRUCTURE_MAX_ROUNDS, max(2, int(os.environ.get('BOOK2EPUB_STRUCTURE_MAX_ROUNDS', str(DEFAULT_STRUCTURE_MAX_ROUNDS)))))
    except (TypeError, ValueError): return DEFAULT_STRUCTURE_MAX_ROUNDS
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

def _pdf_text_pages(source):
    """Extract layout-preserving embedded text, returning one entry per PDF page."""
    if not shutil.which('pdftotext'):
        return None
    try:
        raw=subprocess.check_output(['pdftotext','-layout',str(source),'-'],text=True,stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return None
    return raw.split('\f')

def _embedded_text_quality(text):
    value=str(text or '').strip()
    if len(value)<20: return 0.0
    printable=sum(ch.isprintable() or ch in '\n\t' for ch in value)/max(1,len(value))
    words=len(re.findall(r"\b\w+\b",value,flags=re.UNICODE))
    return min(1.0, printable * min(1.0, words/8))

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
    for table in ('blocks','pages','events','cache','usage_events','assembly_events','assembly_windows','manuscript_units','assembly_runs','structure_nodes','structure_chunks','structure_runs'):
        db.execute(f'DELETE FROM {table}')
    db.execute('DELETE FROM metadata')
    meta_set(db,'source',str(source_path)); meta_set(db,'source_sha256',hashlib.sha256(source_path.read_bytes()).hexdigest())
    if source_lang: meta_set(db,'source_lang',source_lang)
    if target_lang: meta_set(db,'target_lang',target_lang)
    db.close(); print(f'[reset] retained source PDF and language configuration in {project}',flush=True)

def reset_stage(project, stage, output=None):
    """Reset one stage only; this function never starts processing."""
    db=connect(project)
    # Usage is an immutable billing/audit ledger. Reset derived stage output,
    # but never erase costs already incurred by prior generations.
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
        db.execute('DELETE FROM structure_nodes'); db.execute('DELETE FROM structure_chunks'); db.execute('DELETE FROM structure_versions'); db.execute('DELETE FROM structure_runs')
    elif stage=='structure-discovery':
        db.execute('DELETE FROM structure_nodes'); db.execute('DELETE FROM structure_chunks'); db.execute('DELETE FROM structure_versions'); db.execute('DELETE FROM structure_runs')
    elif stage=='structure-refinement':
        run=db.execute('SELECT id FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone()
        if run:
            db.execute("DELETE FROM structure_chunks WHERE run_id=? AND chunk_key NOT LIKE 'v5:r1:%'",(run['id'],))
            discovery=db.execute("SELECT * FROM structure_versions WHERE run_id=? AND phase='discovery' ORDER BY version LIMIT 1",(run['id'],)).fetchone()
            db.execute("DELETE FROM structure_versions WHERE run_id=? AND phase!='discovery'",(run['id'],))
            db.execute('DELETE FROM structure_nodes WHERE run_id=?',(run['id'],))
            if discovery:
                ids={}
                for ordinal,node in enumerate(json.loads(discovery['tree_json']),1):
                    parent_id=ids.get(node.get('parent_start_unit'))
                    cur=db.execute('INSERT INTO structure_nodes(run_id,ordinal,parent_id,node_type,title,number,number_source,number_format,source_derived,heading_level,start_unit,end_unit,confidence,evidence_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(run['id'],ordinal,parent_id,node.get('node_type'),node.get('title'),node.get('number'),node.get('number_source','none'),node.get('number_format'),1,node.get('heading_level'),node.get('start_unit'),node.get('end_unit'),node.get('confidence'),node.get('evidence_json'),'needs_review')).lastrowid
                    ids[node.get('start_unit')]=cur
            db.execute("UPDATE structure_runs SET round=1,phase='discovery',status='needs_review',convergence=0,fingerprint=NULL,quality_score=0,validation_issues_json='[]',content_start_unit=NULL,backmatter_start_unit=NULL,completed=NULL WHERE id=?",(run['id'],))
    elif stage=='build':
        target=Path(output) if output else project/'book.epub'
        if target.exists(): target.unlink()
    else:
        db.close(); raise ValueError(f'Unknown reset stage: {stage}')
    db.commit(); db.close(); print(f'[reset] {stage} reset complete; processing was not started',flush=True)
def ocr(project,lang='eng',start=1,end=None,debug=False,sample_pages=2,seed=None,groups='beginning,middle,end',reset=False,middle_pages=2):
    for c in ('pdfinfo','pdftoppm'): need(c)
    db=connect(project); lang=lang or meta_get(db,'source_lang','eng'); source=Path(meta_get(db,'source',str(project/'source.pdf'))); pages=project/'pages'; pages.mkdir(exist_ok=True); texts=project/'ocr'; texts.mkdir(exist_ok=True)
    info=subprocess.check_output(['pdfinfo',str(source)],text=True); n=int(re.search(r'^Pages:\s+(\d+)',info,re.M).group(1)); meta_set(db,'page_count',n); end=end or n
    embedded_pages=_pdf_text_pages(source)
    ocr_mode=os.environ.get('BOOK2EPUB_OCR_MODE','adaptive').strip().lower()
    text_mode=os.environ.get('BOOK2EPUB_PDF_TEXT_MODE','auto').strip().lower()
    if text_mode not in ('auto','always','never'): text_mode='auto'
    adaptive=ocr_mode not in ('full','always')
    meta_set(db,'evidence_version',EVIDENCE_VERSION)
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
        pdf_text=(embedded_pages[i-1] if embedded_pages and i-1<len(embedded_pages) else '') or ''
        quality=_embedded_text_quality(pdf_text)
        use_embedded=bool(text_mode!='never' and quality>=0.55 and (text_mode=='always' or adaptive))
        if not use_embedded:
            need('tesseract')
            if not txt.exists(): subprocess.run(['tesseract',str(image),str(txt.with_suffix('')),'-l',lang,'--psm','3'],check=True)
            raw=txt.read_text(encoding='utf8',errors='replace'); source_kind='ocr'
        else:
            raw=pdf_text.strip(); source_kind='pdf_text'
        evidence={'version':EVIDENCE_VERSION,'pdf_text_quality':round(quality,4),'text_source':source_kind,'ocr_ran':source_kind=='ocr','pdf_text_available':bool(pdf_text.strip())}
        row=db.execute('SELECT id FROM pages WHERE number=?',(i,)).fetchone(); page=row[0] if row else db.execute('INSERT INTO pages(number,image,text,status,pdf_text,pdf_layout_text,evidence_json) VALUES(?,?,?,?,?,?,?)',(i,str(image.relative_to(project)),raw,'needs_review',pdf_text,pdf_text,json.dumps(evidence,ensure_ascii=False))).lastrowid
        db.execute('UPDATE pages SET image=?,text=?,pdf_text=?,pdf_layout_text=?,evidence_json=? WHERE id=?',(str(image.relative_to(project)),raw,pdf_text,pdf_text,json.dumps(evidence,ensure_ascii=False),page))
        if not db.execute('SELECT 1 FROM blocks WHERE page_id=?',(page,)).fetchone():
            for j,b in enumerate(re.split(r'\n\s*\n',raw)):
                if b.strip(): db.execute('INSERT INTO blocks(page_id,ordinal,text,approved_text,evidence_source,evidence_json) VALUES(?,?,?,?,?,?)',(page,j,b.strip(),b.strip(),source_kind,json.dumps(evidence,ensure_ascii=False)))
        else:
            db.execute('UPDATE blocks SET evidence_source=?,evidence_json=? WHERE page_id=?',(source_kind,json.dumps(evidence,ensure_ascii=False),page))
        db.execute("UPDATE pages SET status='ocr_done' WHERE id=?",(page,)); db.commit()
        _progress('ocr', number, total, f'page {i}')
    meta_set(db,'source_lang',lang); db.close()
def clean(text): return re.sub(r'\n{3,}','\n\n',re.sub(r'[ \t]+\n','\n',re.sub(r'(?<=\w)-\n(?=\w)','',text))).strip()
def _split_long(text, limit=None):
    """Split prose into sentence-bounded paragraphs near *limit* words."""
    if limit is None:
        try: limit=max(1, int(os.environ.get('BOOK2EPUB_ASSEMBLY_MAX_WORDS', '100')))
        except (TypeError, ValueError): limit=100
    else:
        limit=max(1, int(limit))
    normalized=re.sub(r'\s+', ' ', str(text or '')).strip()
    if not normalized: return []
    # Include common closing quotes/brackets in the sentence so they are never
    # stranded at the start of the next paragraph.
    pattern=r'.*?(?:[.!?]+(?:["»”’\'\)\]]+)?(?=\s|$)|$)'
    sentences=[m.group(0).strip() for m in re.finditer(pattern, normalized) if m.group(0).strip()]
    if not sentences: return [normalized]
    out=[]; current=[]; words=0
    for sentence in sentences:
        n=len(sentence.split())
        if current and words+n>limit:
            out.append(' '.join(current)); current=[]; words=0
        current.append(sentence); words+=n
        if words>limit:
            out.append(' '.join(current)); current=[]; words=0
    if current: out.append(' '.join(current))
    return out

def _provenance_refs(value):
    """Normalize model provenance to canonical, globally unique block refs."""
    try: raw=json.loads(value) if isinstance(value,str) else (value or [])
    except Exception: raw=[]
    if isinstance(raw,(str,int)): raw=[raw]
    out=[]
    for item in raw:
        if isinstance(item,dict) and item.get('block') is not None:
            try: page=int(item['page']) if item.get('page') is not None else None; block=int(item['block'])
            except (TypeError,ValueError): continue
            out.append({'page':page,'block':block,'key':f'block:{block}'})
        elif isinstance(item,int) and not isinstance(item,bool):
            out.append({'page':None,'block':item,'key':f'block:{item}'})
        elif isinstance(item,str):
            m=re.fullmatch(r'\s*(?:(\d+)\s*:\s*(\d+)|page\s*=\s*(\d+)\s+block\s*=\s*(\d+))\s*',item,re.I)
            if m:
                page,block=(m.group(1),m.group(2)) if m.group(1) is not None else (m.group(3),m.group(4))
                out.append({'page':int(page),'block':int(block),'key':f'block:{int(block)}'})
    return out

def _assembly_text_key(text):
    return re.sub(r'\s+',' ',(text or '').strip().casefold())

def _chapter_number(title):
    m=re.match(r'^\s*(?:chapter|chapitre)?\s*(\d{1,3})[.)]?(?:\s+|$)',str(title or ''),re.I)
    return int(m.group(1)) if m else None

def _part_number(title):
    m=re.match(r'^\s*(?:part|partie)\s+([0-9]+|[ivxlcdm]+)\b',str(title or ''),re.I)
    if not m: return None
    raw=m.group(1)
    if raw.isdigit(): return int(raw),'arabic'
    vals={'i':1,'v':5,'x':10,'l':50,'c':100,'d':500,'m':1000}; total=prev=0
    for ch in raw.casefold()[::-1]:
        v=vals.get(ch,0); total += -v if v<prev else v; prev=max(prev,v)
    return total,'roman'

def _structure_title_key(title):
    value=re.sub(r'^\s*(?:chapitre\s*)?\d{1,3}[.)]?\s*','',str(title or ''),flags=re.I)
    return re.sub(r'\W+',' ',value.casefold()).strip()

def _reconcile_structure_candidates(units, candidates):
    """Normalize anchors/types without making language-specific book decisions."""
    unit_by_ordinal={int(u['ordinal']):u for u in units}
    normalized=[]
    for raw in candidates:
        item=dict(raw); unit=item.get('unit',item.get('start_unit'))
        try: unit=int(unit)
        except (TypeError,ValueError): continue
        source_unit=unit_by_ordinal.get(unit)
        title=re.sub(r'\s+',' ',str(item.get('title') or (source_unit['text'] if source_unit else '')).strip())
        if not title: continue
        item['unit']=unit; item['title']=title
        if item.get('node_type') not in ('part','chapter','section','subsection'): continue
        if item['node_type'] in ('part','chapter'): item['heading_level']=1
        normalized.append(item)
    # Prefer explicit numbered/part candidates and collapse duplicate model +
    # corrected candidates at one anchor.
    normalized.sort(key=lambda x:(x['unit'],0 if _chapter_number(x['title']) is not None or x['node_type']=='part' else 1,-float(x.get('confidence') or 0)))
    unique=[]; seen_units=set()
    for item in normalized:
        if item['unit'] in seen_units: continue
        seen_units.add(item['unit']); unique.append(item)
    # Remove the common OCR pair "17. TITLE" followed by an unnumbered copy
    # of TITLE.  Same-anchor and adjacent-unit variants are both covered.
    out=[]
    for item in unique:
        if out and item['unit']-out[-1]['unit']<=1 and _chapter_number(out[-1]['title']) is not None and _structure_title_key(item['title'])==_structure_title_key(out[-1]['title']):
            continue
        out.append(item)
    # Retain a proven number-only chapter as an explicit boundary.  It is a
    # warning, not a structural error; no title is invented.
    warnings=[]
    for item in out:
        if item['node_type']=='chapter' and re.fullmatch(r'\s*(?:chapter|chapitre)?\s*\d+[.)]?\s*',item['title'],re.I):
            warnings.append({'severity':'warning','issue':'chapter_title_missing','start_unit':item['unit'],'number':_chapter_number(item['title'])})
    return out,warnings

def _validate_structure_tree(nodes, content_start=None, backmatter_start=None, allow_backmatter=False):
    errors=[]; warnings=[]; seen_titles=set(); seen_starts=set(); chapter_numbers=[]
    chapter_anchors={int(n.get('unit',n.get('start_unit')) or 0) for n in nodes if n.get('node_type')=='chapter'}
    for node in sorted(nodes,key=lambda n:int(n.get('unit',n.get('start_unit')) or 0)):
        title=str(node.get('title') or '').strip(); ntype=node.get('node_type'); start=int(node.get('unit',node.get('start_unit')) or 0)
        norm=_structure_title_key(title)
        if start<=0 or start in seen_starts: errors.append({'severity':'error','issue':'duplicate_or_invalid_anchor','start_unit':start,'title':title})
        if content_start and start<content_start: errors.append({'severity':'error','issue':'frontmatter_navigation_node','start_unit':start,'title':title})
        in_backmatter=bool(backmatter_start and start>=backmatter_start)
        # Back matter headings are real TOC entries when the TOC is
        # authoritative. They must remain navigable in the EPUB, so they are
        # excluded from the body-only navigation check.
        if in_backmatter and not allow_backmatter:
            errors.append({'severity':'error','issue':'backmatter_or_toc_navigation_node','start_unit':start,'title':title})
        if norm and norm in seen_titles: errors.append({'severity':'error','issue':'duplicate_title','start_unit':start,'title':title})
        if ntype not in ('part','chapter','section','subsection'): errors.append({'severity':'error','issue':'invalid_node_type','start_unit':start,'node_type':ntype})
        if ntype in ('part','chapter') and node.get('parent_start_unit') is not None: errors.append({'severity':'error','issue':'top_level_has_parent','start_unit':start})
        if ntype in ('section','subsection') and not (in_backmatter and allow_backmatter):
            parent=node.get('parent_start_unit')
            try: parent=int(parent)
            except (TypeError,ValueError): parent=None
            if parent not in chapter_anchors or parent>=start: errors.append({'severity':'error','issue':'invalid_section_parent','start_unit':start,'parent_start_unit':parent,'title':title})
        number=node.get('number') if ntype=='chapter' else None
        try: number=int(number) if number is not None else _chapter_number(title)
        except (TypeError,ValueError): number=_chapter_number(title)
        if number is not None:
            if number in [x[0] for x in chapter_numbers]: errors.append({'severity':'error','issue':'duplicate_chapter_number','number':number,'start_unit':start})
            chapter_numbers.append((number,start))
        if ntype=='chapter' and (not title or re.fullmatch(r'\s*(?:chapter|chapitre)?\s*\d+[.)]?\s*',title,re.I)): warnings.append({'severity':'warning','issue':'chapter_title_missing','number':number,'start_unit':start})
        seen_starts.add(start)
        if norm: seen_titles.add(norm)
    for (a,au),(b,bu) in zip(chapter_numbers,chapter_numbers[1:]):
        if b<=a: errors.append({'severity':'error','issue':'non_monotonic_chapter_numbers','previous':a,'current':b,'start_unit':bu})
        elif b>a+1: warnings.append({'severity':'warning','issue':'chapter_number_gap','previous':a,'current':b,'start_unit':bu})
    return errors,warnings

def _structure_snapshot(db,run_id):
    return [dict(r) for r in db.execute('''SELECT n.node_type,n.title,n.number,n.number_source,n.number_format,
                    p.start_unit parent_start_unit,n.heading_level,n.start_unit,n.end_unit,n.confidence,n.evidence_json
                FROM structure_nodes n LEFT JOIN structure_nodes p ON p.id=n.parent_id
                WHERE n.run_id=? ORDER BY n.start_unit,n.ordinal''',(run_id,)).fetchall()]

def _unit_source_evidence(db, unit):
    """Return page/block evidence for an assembled unit.

    This deliberately uses the authoritative block coordinates/order rather
    than trusting the LLM's heading classification.  It lets structure
    detection distinguish a chapter opening from a running page header.
    """
    refs = _provenance_refs(unit['provenance_json'])
    out=[]
    for ref in refs:
        row=db.execute('''SELECT b.id,b.page_id,b.ordinal,b.corrected_kind,p.number
                          FROM blocks b JOIN pages p ON p.id=b.page_id
                          WHERE b.id=?''',(ref['block'],)).fetchone()
        if row: out.append(dict(row))
    return out

def _is_running_header(db, unit, evidence):
    """Conservatively reject headings at the top of a continuing page.

    A real chapter opening may also be the first block on a page, so the
    decisive signal is that the same page already contains body blocks after
    the candidate and the preceding assembled unit belongs to that page.  The
    source block must also be at the page edge or marked as page furniture.
    """
    if not evidence: return False
    for item in evidence:
        if item.get('ordinal') != 0 and item.get('corrected_kind') != 'page_furniture':
            continue
        page_id=item['page_id']
        later=db.execute("SELECT COUNT(*) FROM blocks WHERE page_id=? AND ordinal>?",(page_id,item['ordinal'])).fetchone()[0]
        if not later: continue
        prior=db.execute('''SELECT 1 FROM manuscript_units u
                            WHERE u.run_id=? AND u.ordinal<?
                              AND EXISTS (SELECT 1 FROM json_each(u.provenance_json) j
                                          WHERE CAST(json_extract(j.value,'$.block') AS INTEGER) IN
                                          (SELECT id FROM blocks WHERE page_id=?))
                            LIMIT 1''',(unit['run_id'],unit['ordinal'],page_id)).fetchone()
        if prior: return True
    return False
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
    page_groups=[]
    try: max_words=max(1,int(os.environ.get('BOOK2EPUB_CORRECTION_MAX_WORDS_PER_PAGE','5000')))
    except (TypeError,ValueError): max_words=5000
    for p in work:
        blocks=db.execute('SELECT * FROM blocks WHERE page_id=? ORDER BY ordinal',(p['id'],)).fetchall(); group=[]; words=0
        for b in blocks:
            n=len((b['text'] or '').split())
            if group and words+n>max_words: page_groups.append((p,group)); group=[]; words=0
            group.append(b); words+=n
        if group: page_groups.append((p,group))
    _progress('correction', 0, len(page_groups), 'pages')
    prompt='Return JSON {"blocks":[{"block_id":number,"corrected_text":string,"kind":"paragraph|heading|subheading|blockquote|list_item|caption|footnote|table|page_furniture|unknown","heading_level":number|null,"confidence":number,"source":"pdf_text|ocr|vision|merged","source_spans":array,"attributes":object,"changes":array,"unresolved":array}],"unresolved":array}. Reconcile the embedded PDF text, layout-preserving PDF text, OCR text, neighboring blocks, and the complete page image. The image is authoritative for visual structure and disputed wording; embedded text is preferred for exact characters only when it agrees with layout and image evidence. Preserve block IDs and order, do not invent illegible text, and report disagreements in unresolved. A line at the very top or bottom may be a repeated running header/footer, not a chapter. Detect standalone printed chapter/part numbers immediately above titles and report attributes.chapter_number and attributes.number_source="printed".'
    def request_page(item):
        p,blocks=item; payload='\n\n'.join(f'[BLOCK {b["id"]}]\n{b["text"] or ""}' for b in blocks)
        payload='[EMBEDDED_PDF_TEXT]\n'+(p['pdf_text'] or '')+'\n\n[LAYOUT_PDF_TEXT]\n'+(p['pdf_layout_text'] or '')+'\n\n[OCR_TEXT]\n'+(p['text'] or '')+'\n\n'+payload
        key=cache_key(CORRECTION_CACHE_VERSION,'page',payload,source,provider,model,f'v{generation}',str(p['image']))
        c=connect(project); cached=c.execute('SELECT value FROM cache WHERE key=?',(key,)).fetchone(); c.close()
        if cached: return p,blocks,json.loads(cached[0]),{'input_tokens':0,'output_tokens':0,'cost':0,'currency':'USD','model':'cached','service_usages':[]},1,key
        _worker_start_jitter('BOOK2EPUB_CORRECTION_START_JITTER')
        out,usage=translate_segment(payload,source,source,provider,project/'.env',project/p['image'],prompt,model)
        try: result=json.loads(out)
        except json.JSONDecodeError: result={'blocks':[],'unresolved':['model returned non-JSON']}
        return p,blocks,result,usage,0,key
    with ThreadPoolExecutor(max_workers=_workers('BOOK2EPUB_CORRECTION_WORKERS',3)) as pool:
      futures=[pool.submit(request_page,g) for g in page_groups]
      for number,future in enumerate(futures,1):
        _check_cancelled(); p,blocks,result,usage,hit,key=future.result()
        if key: db.execute('INSERT OR REPLACE INTO cache(key,value,created) VALUES(?,?,?)',(key,json.dumps(result,ensure_ascii=False),now()))
        returned={int(x['block_id']):x for x in (result.get('blocks') or []) if isinstance(x,dict) and str(x.get('block_id','')).isdigit()}
        for b in blocks:
            x=returned.get(b['id'])
            if x is None:
                fallback_prompt='Return JSON with corrected_text, kind, heading_level, confidence, attributes, changes, and unresolved. Correct only this OCR block using the page image. Preserve meaning; do not omit or invent text.'
                fallback_key=cache_key(CORRECTION_CACHE_VERSION,'paragraph-fallback',b['text'] or '',source,provider,model,str(p['image']),str(b['id']))
                cached_fallback=db.execute('SELECT value FROM cache WHERE key=?',(fallback_key,)).fetchone()
                if cached_fallback:
                    x=json.loads(cached_fallback[0]); fallback_usage={'model':'cached'}; fallback_hit=1
                else:
                    fallback_payload='[EMBEDDED_PDF_TEXT]\n'+(p['pdf_text'] or '')+'\n\n[LAYOUT_PDF_TEXT]\n'+(p['pdf_layout_text'] or '')+'\n\n[OCR_BLOCK]\n'+(b['text'] or '')
                    out,fallback_usage=translate_segment(fallback_payload,source,source,provider,project/'.env',project/p['image'],fallback_prompt,model)
                    try: x=json.loads(out)
                    except json.JSONDecodeError: x={'corrected_text':b['text'] or '','kind':b['kind'] or 'paragraph','confidence':0.2,'unresolved':['paragraph fallback returned non-JSON']}
                    db.execute('INSERT OR REPLACE INTO cache(key,value,created) VALUES(?,?,?)',(fallback_key,json.dumps(x,ensure_ascii=False),now())); fallback_hit=0
                record_usage(db,run_id='correction',stage='correction',page_id=p['id'],block_id=b['id'],request_key=fallback_key,provider=provider,model=fallback_usage.get('model',model),input_tokens=fallback_usage.get('input_tokens',0),cached_input_tokens=fallback_usage.get('cached_input_tokens',0),output_tokens=fallback_usage.get('output_tokens',0),total_cost=fallback_usage.get('cost',0),currency=fallback_usage.get('currency','USD'),cache_hit=fallback_hit,details_json=json.dumps(fallback_usage.get('service_usages',[]),ensure_ascii=False),trace_id=fallback_usage.get('trace_id'),created=now())
            db.execute("UPDATE blocks SET corrected_text=?,correction_json=?,corrected_kind=?,corrected_heading_level=?,corrected_confidence=?,evidence_source=?,evidence_json=?,status='needs_review',revision=revision+1 WHERE id=?",(x.get('corrected_text',b['text'] or ''),json.dumps(x,ensure_ascii=False),x.get('kind','paragraph'),x.get('heading_level'),x.get('confidence'),x.get('source','merged'),json.dumps({'source_spans':x.get('source_spans') or [],'page_evidence':json.loads(p['evidence_json'] or '{}') if p['evidence_json'] else {}},ensure_ascii=False),b['id']))
        db.commit(); record_usage(db,run_id='correction',stage='correction',page_id=p['id'],request_key=key,provider=provider,model=usage.get('model',model),input_tokens=usage.get('input_tokens',0),cached_input_tokens=usage.get('cached_input_tokens',0),output_tokens=usage.get('output_tokens',0),total_cost=usage.get('cost',0),currency=usage.get('currency','USD'),cache_hit=hit,details_json=json.dumps(usage.get('service_usages',[]),ensure_ascii=False),trace_id=usage.get('trace_id'),created=now())
        _progress('correction', number, len(page_groups), f'page {p["number"]}' + (' (cached)' if hit else ''))
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

def _assemble_generative_legacy(project,provider='hosted',model=None,window_size=None,reset=False):
    """Assemble only consecutive page windows into reviewable manuscript units."""
    load_dotenv(Path.cwd()/'.env')
    if window_size is None:
        try: window_size=max(2,int(os.environ.get('BOOK2EPUB_ASSEMBLY_WINDOW_SIZE','6')))
        except (TypeError,ValueError): window_size=6
    db=connect(project); source=meta_get(db,'source_lang','eng')
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
        source_text='\n\n'.join(parts); window_key=','.join(str(p['number']) for p in window); key=cache_key(ASSEMBLY_CACHE_VERSION,source_text,source,provider,model,str(generation),window_key)
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
    # Keep only a small bounded set of requests in flight.  ``workers`` is the
    # actual parallelism; this queue limit controls how far ahead we submit
    # work and prevents hundreds of requests being queued at once.
    # A context manager here would wait for *all* 206 requests on exit before
    # the first result is integrated, making the UI appear stuck at 0/206.
    workers=_workers('BOOK2EPUB_ASSEMBLY_WORKERS',2)
    try: max_inflight=max(workers, min(workers*8, int(os.environ.get('BOOK2EPUB_ASSEMBLY_MAX_INFLIGHT', workers*2))))
    except (TypeError, ValueError): max_inflight=workers*2
    pool=ThreadPoolExecutor(max_workers=workers)
    pending=iter(payloads.items())
    def fill_queue():
        while len(assembly_futures) < max_inflight:
            try: wk,data=next(pending)
            except StopIteration: break
            assembly_futures[wk]=pool.submit(request_window,(wk,data))
    fill_queue()
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
        key=cache_key(ASSEMBLY_CACHE_VERSION,source_text,source,provider,model,str(generation),','.join(str(p['number']) for p in window))
        window_key=','.join(str(p['number']) for p in window)
        if db.execute('SELECT 1 FROM assembly_windows WHERE run_id=? AND window_key=?',(run,window_key)).fetchone():
            _progress('assembly',n,len(windows),f'pages {window[0]["number"]}-{window[-1]["number"]} (resumed)'); continue
        if window_key not in assembly_futures:
            _progress('assembly',n,len(windows),f'pages {window[0]["number"]}-{window[-1]["number"]} (resumed)'); continue
        # Poll rather than blocking indefinitely on a model request, so Stop
        # can be observed even when a provider is retrying or slow.
        future=assembly_futures[window_key]
        while True:
            try:
                _check_cancelled()
            except Exception:
                for pending_future in assembly_futures.values(): pending_future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            try:
                _,result,usage,hit=future.result(timeout=0.5)
                break
            except FutureTimeout:
                continue
        del assembly_futures[window_key]
        fill_queue()
        # Persist usage for every assembly window so the dashboard's stage
        # cost/token totals include both live and cached model requests.
        record_usage(db, run_id=run, stage='assembly',
                     page_id=window[0]['id'] if window else None,
                     request_key=key, provider=provider,
                     model=usage.get('model', model),
                     input_tokens=usage.get('input_tokens', 0),
                     cached_input_tokens=usage.get('cached_input_tokens', 0),
                     output_tokens=usage.get('output_tokens', 0),
                     total_cost=usage.get('cost', 0),
                     currency=usage.get('currency', 'USD'),
                     cache_hit=hit, created=now())
        result['removed']=removed + result.get('removed',[])
        if not hit: db.execute('INSERT OR REPLACE INTO cache(key,value,created) VALUES(?,?,?)',(key,json.dumps(result,ensure_ascii=False),now()))
        if 'removed' not in result: result['removed']=removed
        for unit in result.get('units',[]):
            refs=_provenance_refs(unit.get('source_blocks',provenance))
            ref_ids={r.get('block') if isinstance(r,dict) else r for r in refs}
            if ref_ids and ref_ids <= seen_blocks: continue
            kind=unit.get('kind','paragraph'); chunks=_split_long(unit.get('text','')) if kind=='paragraph' else [unit.get('text','')]
            for chunk in chunks:
                ordinal+=1; db.execute('INSERT INTO manuscript_units(run_id,ordinal,kind,text,provenance_json,decisions_json,confidence,status) VALUES(?,?,?,?,?,?,?,?)',(run,ordinal,kind,chunk,json.dumps(refs,ensure_ascii=False),json.dumps({'removed':result.get('removed',[]),'joins':result.get('joins',[]),'unresolved':result.get('unresolved',[]),'split_from_long_paragraph':len(chunks)>1,'heading_level':unit.get('heading_level')},ensure_ascii=False),unit.get('confidence'), 'needs_review'))
            seen_blocks.update(ref_ids)
        db.execute('INSERT OR REPLACE INTO assembly_windows(run_id,window_key,result_json,status) VALUES(?,?,?,?)',(run,window_key,json.dumps(result,ensure_ascii=False),'completed'))
        db.commit(); _progress('assembly',n,len(windows),f'pages {window[0]["number"]}-{window[-1]["number"]}' + (' (cached)' if hit else ''))
    pool.shutdown(wait=True)
    # Re-materialize the manuscript stream from window results in global page
    # order.  The previous implementation inserted units while processing and
    # reset ``ordinal`` to zero on resume; completed windows could therefore
    # receive duplicate ordinals or be appended after later windows.  Window
    # results are the durable source of truth, so rebuild the stream once all
    # available windows have been processed.
    db.execute('DELETE FROM manuscript_units WHERE run_id=?',(run,))
    db.execute('DELETE FROM assembly_reconciliation WHERE run_id=?',(run,))
    materialized=[]; owners=set()
    completed_windows=db.execute('SELECT window_key,result_json FROM assembly_windows WHERE run_id=? AND status=\'completed\'',(run,)).fetchall()
    def window_start(key):
        try: return int(str(key).split(',',1)[0])
        except (TypeError,ValueError): return 10**9
    for window in sorted(completed_windows,key=lambda r: window_start(r['window_key'])):
        try: result=json.loads(window['result_json'] or '{}')
        except Exception: result={}
        # Build an exact-text fallback for model responses that omit or use an
        # unsupported provenance shape.  Only unique matches are accepted.
        try: page_nums=[int(x) for x in str(window['window_key']).split(',') if x]
        except ValueError: page_nums=[]
        block_rows=[]
        for page_num in page_nums:
            page_row=db.execute('SELECT id FROM pages WHERE number=?',(page_num,)).fetchone()
            if page_row:
                block_rows.extend(db.execute('SELECT id,page_id,text,approved_text,corrected_text FROM blocks WHERE page_id=? ORDER BY ordinal',(page_row['id'],)).fetchall())
        exact_blocks={}
        for block in block_rows:
            text=_assembly_text_key(block['corrected_text'] or block['approved_text'] or block['text'])
            if text: exact_blocks.setdefault(text,[]).append(block)
        for unit in result.get('units',[]) or []:
            refs=_provenance_refs(unit.get('source_blocks',[])); ref_keys={r['key'] for r in refs}
            if not refs:
                matches=exact_blocks.get(_assembly_text_key(unit.get('text')),[])
                if len(matches)==1:
                    block=matches[0]; page_num=db.execute('SELECT number FROM pages WHERE id=?',(block['page_id'],)).fetchone()['number']
                    refs=[{'page':page_num,'block':block['id'],'key':f'block:{block["id"]}'}]; ref_keys={refs[0]['key']}
            # Empty provenance is never allowed to duplicate a prior unit; it
            # is retained only when the model supplied no provenance at all.
            if ref_keys and ref_keys <= owners: continue
            chunks=_split_long(unit.get('text','')) if unit.get('kind','paragraph')=='paragraph' else [unit.get('text','')]
            for chunk in chunks:
                materialized.append((unit,chunk,refs,result))
            owners.update(ref_keys)
    for ordinal,(unit,chunk,refs,result) in enumerate(materialized,1):
        split_from_long=unit.get('kind','paragraph')=='paragraph' and len(_split_long(unit.get('text',''))) > 1
        db.execute('INSERT INTO manuscript_units(run_id,ordinal,kind,text,provenance_json,decisions_json,confidence,status) VALUES(?,?,?,?,?,?,?,?)',(run,ordinal,unit.get('kind','paragraph'),chunk,json.dumps(refs,ensure_ascii=False),json.dumps({'removed':result.get('removed',[]),'joins':result.get('joins',[]),'unresolved':result.get('unresolved',[]),'split_from_long_paragraph':split_from_long,'heading_level':unit.get('heading_level')},ensure_ascii=False),unit.get('confidence'),'needs_review'))
    db.commit()
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
        try: unit_decisions=json.loads(unit['decisions_json'] or '{}'); existing_decisions=json.loads(existing['decisions_json'] or '{}')
        except Exception: unit_decisions=existing_decisions={}
        # A long paragraph is intentionally represented by several ordered
        # chunks sharing the same source refs. Keep those siblings; discard
        # only an exact duplicate emitted by an overlapping window.
        if unit_decisions.get('split_from_long_paragraph') and existing_decisions.get('split_from_long_paragraph') and unit['text'] != existing['text']:
            continue
        old_refs=_provenance_refs(existing['provenance_json']); old_keys={r['key'] for r in old_refs}
        score=(len(keys),len(unit['text'] or ''),float(unit['confidence'] or 0)); old_score=(len(old_keys),len(existing['text'] or ''),float(existing['confidence'] or 0))
        if score>old_score:
            db.execute("UPDATE manuscript_units SET status='needs_review' WHERE id=?",(unit['id'],))
            for oid in existing_ids:
                db.execute("UPDATE manuscript_units SET status='discarded' WHERE id=?",(oid,)); db.execute('INSERT INTO assembly_reconciliation(run_id,candidate_unit_id,final_unit_id,action,reason,evidence_json,created) VALUES(?,?,?,?,?,?,?)',(run,oid,unit['id'],'discarded_duplicate','less complete provenance',json.dumps({'kept_score':score,'discarded_score':old_score}),now()))
            for k in old_keys: owners[k]=unit['id']
        else:
            db.execute("UPDATE manuscript_units SET status='discarded' WHERE id=?",(unit['id'],)); db.execute('INSERT INTO assembly_reconciliation(run_id,candidate_unit_id,final_unit_id,action,reason,evidence_json,created) VALUES(?,?,?,?,?,?,?)',(run,unit['id'],existing['id'],'discarded_duplicate','less complete provenance',json.dumps({'kept_score':old_score,'discarded_score':score}),now()))
    # Discarded overlap candidates leave holes in the provisional sequence;
    # compact only surviving units so Structure receives a unique monotonic
    # global ordinal stream.
    active_units=db.execute("SELECT id FROM manuscript_units WHERE run_id=? AND status!='discarded' ORDER BY ordinal,id",(run,)).fetchall()
    for ordinal,unit in enumerate(active_units,1):
        db.execute('UPDATE manuscript_units SET ordinal=? WHERE id=?',(ordinal,unit['id']))
    db.execute('UPDATE assembly_runs SET status=?,completed=? WHERE id=?',('needs_review',now(),run)); db.commit(); db.close()

def assemble(project,provider='lossless',model=None,window_size=None,reset=False):
    """Build a complete manuscript without allowing an LLM to rewrite text."""
    load_dotenv(Path.cwd()/'.env')
    if window_size is None:
        try: window_size=max(2,int(os.environ.get('BOOK2EPUB_ASSEMBLY_WINDOW_SIZE','6')))
        except (TypeError,ValueError): window_size=6
    db=connect(project); source=meta_get(db,'source_lang','eng')
    join_mode=os.environ.get('BOOK2EPUB_ASSEMBLY_JOIN_MODE','hybrid').strip().lower()
    if join_mode not in ('heuristic','hybrid','llm'): join_mode='hybrid'
    join_model=model or os.environ.get('BOOK2EPUB_ASSEMBLY_JOIN_MODEL') or os.environ.get('BOOK2EPUB_ASSEMBLY_MODEL') or os.environ.get('BOOK2EPUB_CORRECTION_MODEL')
    try: join_threshold=float(os.environ.get('BOOK2EPUB_ASSEMBLY_JOIN_CONFIDENCE','0.75'))
    except (TypeError,ValueError): join_threshold=.75
    try: join_batch=max(1,int(os.environ.get('BOOK2EPUB_ASSEMBLY_JOIN_BATCH_SIZE','40')))
    except (TypeError,ValueError): join_batch=40
    join_fallback=os.environ.get('BOOK2EPUB_ASSEMBLY_JOIN_FALLBACK','heuristic').strip().lower()
    if join_fallback not in ('heuristic','fail'): join_fallback='heuristic'
    join_images=os.environ.get('BOOK2EPUB_ASSEMBLY_JOIN_IMAGES','false').strip().lower() in ('1','true','yes','on')
    total_blocks=db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]
    corrected=db.execute('SELECT COUNT(*) FROM blocks WHERE corrected_text IS NOT NULL').fetchone()[0]
    if total_blocks and corrected < total_blocks:
        db.close(); raise RuntimeError(f'Complete correction required before assembly ({corrected}/{total_blocks} blocks corrected)')
    generation=db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM assembly_runs').fetchone()[0]
    run=db.execute('INSERT INTO assembly_runs(generation,source_lang,provider,model,window_size,status,created) VALUES(?,?,?,?,?,?,?)',
                   (generation,source,'lossless',join_model or 'deterministic',window_size,'running',now())).lastrowid
    pages=db.execute('SELECT * FROM pages ORDER BY number').fetchall()
    page_blocks={p['id']:db.execute('SELECT * FROM blocks WHERE page_id=? ORDER BY ordinal,id',(p['id'],)).fetchall() for p in pages}

    def norm(value): return re.sub(r'\s+',' ',str(value or '').strip()).casefold()
    edge_counts={}
    for p in pages:
        bs=page_blocks[p['id']]
        for b in ([bs[0],bs[-1]] if bs else []):
            key=norm(b['corrected_text'] or b['approved_text'] or b['text'])
            if key: edge_counts[key]=edge_counts.get(key,0)+1
    repeated={key for key,count in edge_counts.items() if count>=3}
    removed=[]; kept=[]
    for p in pages:
        bs=page_blocks[p['id']]
        for index,b in enumerate(bs):
            text=b['corrected_text'] if b['corrected_text'] is not None else (b['approved_text'] or b['text'] or '')
            edge=index in (0,len(bs)-1); key=norm(text); reason=None
            if b['corrected_kind']=='page_furniture': reason='classified page furniture'
            elif edge and key in repeated: reason='repeated page-edge text'
            elif edge and b['corrected_kind'] not in ('heading','subheading') and re.fullmatch(r'\s*\d{1,4}\s*',text or ''): reason='numeric page-edge page number'
            if reason:
                removed.append({'page':p['number'],'block':b['id'],'text':text,'reason':reason})
            else:
                if not str(text or '').strip(): continue
                kept.append({'page':p['number'],'page_id':p['id'],'block':b['id'],'ordinal':b['ordinal'],
                             'kind':b['corrected_kind'] or b['kind'] or 'paragraph','text':text,
                             'confidence':b['corrected_confidence'] or b['confidence']})

    def output_kind(kind):
        if kind in ('heading','subheading'): return 'heading'
        if kind in ('blockquote','caption','footnote','list_item','table'): return kind
        return 'paragraph'
    # Decide only paragraph boundaries; prose remains immutable.  A boundary
    # is ambiguous when punctuation heuristics cannot safely classify it.
    def heuristic_join(left,right):
        before=(left['text'] or '').rstrip(); after=(right['text'] or '').lstrip()
        if not before or not after: return False,False
        if re.search(r'[.!?…:;»”\]\)]$',before): return False,True
        continuation=bool(re.match(r'^[a-zà-öø-ÿ«“(]',after))
        return continuation,continuation
    candidates=[]; decisions={}; fallback_count=0
    for left,right in zip(kept,kept[1:]):
        if left['page']+1!=right['page'] or left['kind']!='paragraph' or right['kind']!='paragraph': continue
        hj,classified=heuristic_join(left,right)
        if join_mode=='heuristic' or (join_mode=='hybrid' and classified):
            decisions[(left['block'],right['block'])]={'decision':'join' if hj else 'split','confidence':1.0 if classified else 0.0,'source':'heuristic','reason':'deterministic boundary rule'}
        else:
            candidates.append((left,right))
    if join_mode in ('hybrid','llm') and candidates:
        if not join_model:
            if join_mode=='llm' or join_fallback=='fail':
                db.execute('UPDATE assembly_runs SET status=?,completed=? WHERE id=?',('failed',now(),run)); db.commit(); db.close()
                raise RuntimeError('Set BOOK2EPUB_ASSEMBLY_JOIN_MODEL for LLM boundary decisions')
            for left,right in candidates:
                hj,_=heuristic_join(left,right); decisions[(left['block'],right['block'])]={'decision':'join' if hj else 'split','confidence':0.0,'source':'heuristic','reason':'LLM model unavailable'}; fallback_count+=1
        else:
            effective_batch=1 if join_images else join_batch
            for start in range(0,len(candidates),effective_batch):
                batch=candidates[start:start+effective_batch]; payload='\n\n'.join(f'[{i}] LEFT block={a["block"]} page={a["page"]}: {a["text"]}\nRIGHT block={b["block"]} page={b["page"]}: {b["text"]}' for i,(a,b) in enumerate(batch))
                prompt='Decide paragraph boundaries only. Do not rewrite text. Return JSON {"decisions":[{"index":number,"decision":"join|split","confidence":number,"reason":string}],"unresolved":[]}. Join only when RIGHT is a continuation of LEFT across a page break; preserve separate paragraphs otherwise.'
                image_paths=[]
                if join_images:
                    for item in batch[0]:
                        image=next((p['image'] for p in pages if p['id']==item['page_id']),None)
                        if image and (project/image).is_file(): image_paths.append(project/image)
                image_key='|'.join(str(path) for path in image_paths)
                key=cache_key(ASSEMBLY_CACHE_VERSION,'join',source,join_model,payload,prompt,image_key)
                cached=db.execute('SELECT value FROM cache WHERE key=?',(key,)).fetchone(); hit=bool(cached)
                try:
                    if cached:
                        result=json.loads(cached[0]); usage={'input_tokens':0,'output_tokens':0,'cached_input_tokens':0,'cost':0,'currency':'USD','model':join_model}
                    else:
                        out,usage=translate_segment(payload,source,source,'hosted',project/'.env',image_paths or None,prompt,join_model)
                        result=json.loads(out)
                    if not isinstance(result,dict): raise ValueError('boundary response must be a JSON object')
                except Exception:
                    if join_fallback=='fail':
                        db.execute('UPDATE assembly_runs SET status=?,completed=? WHERE id=?',('failed',now(),run)); db.commit(); db.close(); raise
                    result={'decisions':[],'unresolved':['LLM request or JSON parsing failed; heuristic fallback used']}; usage={'input_tokens':0,'output_tokens':0,'cached_input_tokens':0,'cost':0,'currency':'USD','model':join_model}
                else:
                    if not cached: db.execute('INSERT OR REPLACE INTO cache(key,value,created) VALUES(?,?,?)',(key,json.dumps(result,ensure_ascii=False),now()))
                record_usage(db,run_id=f'assembly-{run}',stage='assembly-join',request_key=key,provider='hosted',model=usage.get('model',join_model),input_tokens=usage.get('input_tokens',0),cached_input_tokens=usage.get('cached_input_tokens',0),output_tokens=usage.get('output_tokens',0),total_cost=usage.get('cost',0),currency=usage.get('currency','USD'),cache_hit=int(hit),details_json=json.dumps(usage.get('service_usages',[]),ensure_ascii=False),trace_id=usage.get('trace_id'),created=now())
                returned={int(x.get('index')):x for x in (result.get('decisions') or []) if isinstance(x,dict) and str(x.get('index','')).isdigit()}
                for i,(left,right) in enumerate(batch):
                    d=returned.get(i); hj,_=heuristic_join(left,right)
                    try: confidence=float(d.get('confidence') or 0) if d else 0.0
                    except (TypeError,ValueError): confidence=0.0
                    if not d or d.get('decision') not in ('join','split') or confidence<join_threshold:
                        decisions[(left['block'],right['block'])]={'decision':'join' if hj else 'split','confidence':confidence,'source':'heuristic','reason':'invalid or low-confidence LLM decision'}; fallback_count+=1
                    else: decisions[(left['block'],right['block'])]={'decision':d['decision'],'confidence':confidence,'source':'llm','reason':str(d.get('reason') or '')}
                _progress('assembly',start,len(candidates),f'join decisions {start+len(batch)}/{len(candidates)}'+(' (cached)' if hit else ''))
    for (left_id,right_id),decision in decisions.items():
        db.execute('INSERT INTO assembly_reconciliation(run_id,action,reason,evidence_json,created) VALUES(?,?,?,?,?)',(run,'join_decision',decision['reason'],json.dumps({'left_block':left_id,'right_block':right_id,'decision':decision['decision'],'confidence':decision['confidence'],'source':decision['source']},ensure_ascii=False),now()))
    db.commit()
    units=[]
    for item in kept:
        kind=output_kind(item['kind']); joined=False
        if units and kind=='paragraph' and units[-1]['kind']=='paragraph' and item['page']==units[-1]['last_page']+1:
            before=units[-1]['text'].rstrip(); after=item['text'].lstrip()
            # Only a source-page continuation is joined. Block order remains
            # authoritative and no prose is generated or paraphrased.
            decision=decisions.get((units[-1]['refs'][-1]['block'],item['block']),{'decision':'split'})
            if decision.get('decision')=='join':
                units[-1]['text']=(before[:-1]+after if before.endswith('-') else before+' '+after)
                units[-1]['refs'].append({'page':item['page'],'block':item['block'],'key':f'block:{item["block"]}'})
                units[-1]['last_page']=item['page']; joined=True
        if not joined:
            units.append({'kind':kind,'text':item['text'],'refs':[{'page':item['page'],'block':item['block'],'key':f'block:{item["block"]}'}],
                          'confidence':item['confidence'],'last_page':item['page']})

    db.execute('DELETE FROM manuscript_units WHERE run_id=?',(run,)); db.execute("DELETE FROM assembly_reconciliation WHERE run_id=? AND action!='join_decision'",(run,))
    ordinal=1
    for unit in units:
        chunks=_split_long(unit['text']) if unit['kind']=='paragraph' else [re.sub(r'\s+',' ',unit['text'] or '').strip()]
        for offset,chunk in enumerate(chunks):
            decisions={'algorithm':ASSEMBLY_CACHE_VERSION,'lossless':True,'joined_blocks':len(unit['refs'])>1,
                       'split_from_long_paragraph':len(chunks)>1}
            db.execute('INSERT INTO manuscript_units(run_id,ordinal,kind,text,provenance_json,decisions_json,confidence,status) VALUES(?,?,?,?,?,?,?,?)',
                       (run,ordinal+offset,unit['kind'],chunk,json.dumps(unit['refs'],ensure_ascii=False),json.dumps(decisions),unit['confidence'],'needs_review'))
        ordinal += len(chunks)
    for item in removed:
        db.execute('INSERT INTO assembly_reconciliation(run_id,action,reason,evidence_json,created) VALUES(?,?,?,?,?)',
                   (run,'removed_furniture',item['reason'],json.dumps(item,ensure_ascii=False),now()))

    groups=[]
    for p in pages:
        if not groups or p['number']!=groups[-1][-1]['number']+1: groups.append([p])
        else: groups[-1].append(p)
    stride=max(1,window_size-1); windows=[g[i:i+window_size] for g in groups for i in range(0,len(g),stride)]
    _progress('assembly',0,len(windows),'lossless windows')
    for n,window in enumerate(windows,1):
        nums=[p['number'] for p in window]; result={'algorithm':ASSEMBLY_CACHE_VERSION,'pages':nums,'lossless':True,
            'removed':[x for x in removed if x['page'] in nums]}
        db.execute('INSERT INTO assembly_windows(run_id,window_key,result_json,status) VALUES(?,?,?,?)',
                   (run,','.join(map(str,nums)),json.dumps(result,ensure_ascii=False),'completed'))
        _progress('assembly',n,len(windows),f'pages {nums[0]}-{nums[-1]} · lossless')

    expected=[x['block'] for x in kept]; actual=[]; markers=[]
    previous_refs=None
    for u in db.execute('SELECT ordinal,text,provenance_json FROM manuscript_units WHERE run_id=? ORDER BY ordinal',(run,)):
        refs=_provenance_refs(u['provenance_json'])
        if refs != previous_refs: actual.extend(r['block'] for r in refs)
        previous_refs=refs
        if re.search(r'\[\[\s*page\s*=',u['text'] or '',re.I): markers.append(u['ordinal'])
    duplicate_ids=sorted(k for k,v in __import__('collections').Counter(actual).items() if v>1)
    missing_ids=sorted(set(expected)-set(actual)); unexpected_ids=sorted(set(actual)-set(expected))
    order_ok=actual==expected
    report={'algorithm':ASSEMBLY_CACHE_VERSION,'run_id':run,'join_mode':join_mode,'join_model':join_model,'join_images':join_images,'fallback_join_decisions':fallback_count,'meaningful_blocks':len(expected),
            'represented_blocks':len(set(actual)),'removed_furniture':removed,'missing_blocks':missing_ids,
            'duplicate_blocks':duplicate_ids,'unexpected_blocks':unexpected_ids,'source_order_ok':order_ok,
            'raw_marker_units':markers,'unit_count':len(units)}
    errors=[]
    if missing_ids: errors.append(f'{len(missing_ids)} corrected blocks missing')
    if duplicate_ids: errors.append(f'{len(duplicate_ids)} corrected blocks duplicated')
    if unexpected_ids: errors.append(f'{len(unexpected_ids)} unexpected blocks represented')
    if not order_ok: errors.append('source block order changed')
    if markers: errors.append(f'raw provenance markers in units {markers}')
    report['errors']=errors; report['result']='fail' if errors else 'pass'
    meta_set(db,'assembly_validation_report',report)
    status='failed_validation' if errors else 'completed'
    db.execute('UPDATE assembly_runs SET status=?,completed=? WHERE id=?',(status,now(),run)); db.commit(); db.close()
    if errors: raise RuntimeError('Lossless assembly validation failed: '+'; '.join(errors))

def _toc_authoritative_structure(db, run_id, units, project=None, model=None):
    """Build an authoritative hierarchy when the assembled text contains a reliable TOC."""
    candidates=detect_toc_candidates(units)
    if not candidates:
        db.execute("UPDATE structure_runs SET toc_status='unavailable',structure_source='agentic' WHERE id=?",(run_id,)); db.commit()
        return False
    candidate=candidates[0]
    selected=list(candidate['units'])
    index=selected[-1]['ordinal']
    while index < units[-1]['ordinal']:
        unit=next((x for x in units if x['ordinal']==index+1),None)
        if not unit or unit['kind'] not in ('list_item','paragraph'): break
        if unit['kind']=='paragraph' and not re.search(r'\d{1,4}\s*$',unit['text'] or ''): break
        selected.append(unit); index+=1
    toc_text='\n'.join(str(x['text'] or '') for x in selected)
    entries=parse_toc_entries(toc_text)
    if project and model and os.environ.get('BOOK2EPUB_TOC_IMAGE_CONFIRM','true').lower() not in ('0','false','no'):
        image_paths=[]
        for unit in selected:
            for ref in _provenance_refs(unit['provenance_json']):
                page=db.execute('SELECT image FROM pages WHERE number=?',(ref.get('page'),)).fetchone() if ref.get('page') is not None else None
                if page and page['image']:
                    image=project / page['image']
                    if image.is_file() and image not in image_paths: image_paths.append(image)
        if image_paths:
            try:
                out,_usage=translate_segment(toc_text,'eng','eng','hosted',project/'.env',image_paths,image_confirmation_prompt(toc_text),model,max_attempts=3)
                confirmed=json.loads(out)
                if confirmed.get('is_toc') is False:
                    db.execute("UPDATE structure_runs SET toc_status='rejected',toc_issues_json=? WHERE id=?",(json.dumps(['image_confirmation_rejected']),run_id)); db.commit(); return False
                if isinstance(confirmed.get('entries'),list) and confirmed['entries']:
                    entries=confirmed['entries']
            except Exception:
                # Image confirmation is advisory; OCR-backed deterministic
                # evidence remains available as the fallback path.
                pass
    body=[u for u in units if u['ordinal']>selected[-1]['ordinal']]
    report=match_toc_to_body(entries,body)
    decision=toc_quality_gate(report)
    pages=[]
    for unit in selected:
        for ref in _provenance_refs(unit['provenance_json']):
            if ref.get('page') is not None: pages.append(ref['page'])
    issues=decision['issues']
    db.execute("UPDATE structure_runs SET toc_status=?,toc_confidence=?,toc_pages_json=?,toc_entries_json=?,toc_issues_json=?,structure_source=? WHERE id=?",
               ('authoritative' if decision['authoritative'] else 'candidate',decision['confidence'],json.dumps(sorted(set(pages))),json.dumps(entries,ensure_ascii=False),json.dumps(issues,ensure_ascii=False),'toc' if decision['authoritative'] else 'hybrid',run_id))
    if not decision['authoritative']: db.commit(); return False
    db.execute('DELETE FROM structure_nodes WHERE run_id=?',(run_id,)); ids={}; matches={m['toc_index']:m for m in report['matches']}
    content_start=None; backmatter_start=None
    for i,entry in enumerate(entries):
        match=matches.get(i)
        if not match: continue
        etype=entry['entry_type']; node_type='chapter' if entry['number'] is not None or etype in ('introduction','appendix','notes','references','acknowledgments','acknowledgements','index') else 'section'
        if content_start is None and etype in ('introduction','chapter'): content_start=match['body_unit']
        if backmatter_start is None and etype in ('appendix','notes','references','acknowledgments','acknowledgements','index'): backmatter_start=match['body_unit']
        cur=db.execute('INSERT INTO structure_nodes(run_id,ordinal,parent_id,node_type,title,number,number_source,number_format,source_derived,heading_level,start_unit,end_unit,confidence,evidence_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (run_id,i+1,None,node_type,entry['title'],entry['number'],'printed' if entry['number'] is not None else 'toc', 'arabic' if entry['number'] is not None else None,1,1,match['body_unit'],match['body_unit'],decision['confidence'],json.dumps({'source':'toc','toc_index':i,'printed_page':entry['printed_page']},ensure_ascii=False),'needs_review')).lastrowid
        ids[match['body_unit']]=cur
    nodes=db.execute('SELECT id,start_unit FROM structure_nodes WHERE run_id=? ORDER BY start_unit,ordinal',(run_id,)).fetchall()
    for i,node in enumerate(nodes):
        end=(nodes[i+1]['start_unit']-1) if i+1<len(nodes) else units[-1]['ordinal']
        db.execute('UPDATE structure_nodes SET end_unit=? WHERE id=?',(max(node['start_unit'],end),node['id']))
    snapshot=_structure_snapshot(db,run_id); fp=hashlib.sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    ver=db.execute('SELECT COALESCE(MAX(version),0)+1 FROM structure_versions WHERE run_id=?',(run_id,)).fetchone()[0]
    db.execute('INSERT INTO structure_versions(run_id,version,phase,tree_json,issues_json,fingerprint,quality_score,accepted,created) VALUES(?,?,?,?,?,?,?,?,?)',(run_id,ver,'toc_authoritative',json.dumps(snapshot,ensure_ascii=False),json.dumps(issues,ensure_ascii=False),fp,decision['confidence'],1,now()))
    db.execute("UPDATE structure_runs SET status='completed',completed=?,round=1,max_rounds=?,convergence=1,fingerprint=?,quality_score=?,candidate_count=?,phase='toc_authoritative',structure_source='toc',content_start_unit=?,backmatter_start_unit=?,unresolved_json=?,validation_issues_json=? WHERE id=?",
               (now(),_structure_max_rounds(),fp,decision['confidence'],len(snapshot),content_start,backmatter_start,json.dumps(issues),json.dumps([]),run_id)); db.commit(); return True


def _structure_once(project, model=None, reset=False, chunk_size=80, debug=False, round_no=1, prior_tree=None, prior_issues=None):
    """Iteratively infer and validate a global chapter/section tree (max five rounds)."""
    load_dotenv(Path.cwd()/'.env'); db=connect(project)
    model=model or os.environ.get('BOOK2EPUB_STRUCTURE_MODEL') or os.environ.get('BOOK2EPUB_ASSEMBLY_MODEL') or os.environ.get('BOOK2EPUB_CORRECTION_MODEL')
    if not model: raise RuntimeError('Set BOOK2EPUB_STRUCTURE_MODEL, BOOK2EPUB_ASSEMBLY_MODEL, or BOOK2EPUB_CORRECTION_MODEL')
    assembly=db.execute("SELECT * FROM assembly_runs ORDER BY generation DESC LIMIT 1").fetchone()
    units=db.execute("SELECT * FROM manuscript_units WHERE run_id=? ORDER BY ordinal",(assembly['id'],)).fetchall() if assembly else []
    if not units: raise RuntimeError('Run assemble before structure')
    if assembly['status']!='completed': raise RuntimeError(f'Assembly validation must pass before structure (status: {assembly["status"]})')
    # Structure is a global, late-stage decision.  Do not infer hierarchy from
    # a debug/partial correction run: every source block must have corrected
    # text before the model sees the assembled book.
    total_blocks=db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]
    corrected_blocks=db.execute('SELECT COUNT(*) FROM blocks WHERE corrected_text IS NOT NULL').fetchone()[0]
    if total_blocks and corrected_blocks < total_blocks:
        raise RuntimeError(f'Complete correction required before structure ({corrected_blocks}/{total_blocks} blocks corrected)')
    prior=db.execute('SELECT * FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone()
    if prior and not reset and prior['model']==model and prior['assembly_run_id']==assembly['id'] and prior['status']!='completed': run_id,generation=prior['id'],prior['generation']; print(f'[structure] resuming generation {generation}',flush=True)
    else:
        generation=(db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM structure_runs').fetchone()[0]); run_id=db.execute('INSERT INTO structure_runs(generation,source_lang,target_lang,model,status,assembly_run_id,created) VALUES(?,?,?,?,?,?,?)',(generation,meta_get(db,'source_lang','eng'),meta_get(db,'target_lang'),model,'running',assembly['id'],now())).lastrowid
    if round_no==1 and _toc_authoritative_structure(db,run_id,[dict(u) for u in units],project,model):
        db.close(); print('[structure] accepted authoritative TOC structure',flush=True); return
    phase='discovery' if round_no == 1 else 'repair'
    db.execute('UPDATE structure_runs SET round=?,phase=? WHERE id=?',(round_no,phase,run_id)); db.commit()
    chunks=[units[i:i+chunk_size] for i in range(0,len(units),chunk_size)]
    _progress('structure',0,len(chunks),f'round {round_no} · {phase} · chunks')
    summaries=[]; unresolved=[]
    for n,chunk in enumerate(chunks,1):
        _check_cancelled()
        key=f'v5:r{round_no}:{chunk[0]["ordinal"]}-{chunk[-1]["ordinal"]}'; cached=db.execute('SELECT result_json FROM structure_chunks WHERE run_id=? AND chunk_key=?',(run_id,key)).fetchone()
        if cached: result=json.loads(cached[0]); hit=True
        else:
            text='\n'.join(f'[{u["ordinal"]}] ({u["kind"]}) {u["text"]}' for u in chunk)
            prompt=('Return JSON {"headings":[{"title":...,"node_type":"part|chapter|section|subsection", "unit": number, "heading_level": number, "confidence": number, "evidence":...}], "toc_candidates": [...], "unresolved": [...]}. '
                    f'The unit field MUST be an absolute global assembled-unit ordinal from {chunk[0]["ordinal"]} through {chunk[-1]["ordinal"]}; never use a local 1-based position. '
                    'Identify only headings supported by the supplied corrected/assembled text. Exclude running headers, footers, captions, page furniture, and page numbers. '
                    'Prefer explicit numbered chapters; report ambiguity in unresolved and do not invent missing text.')
            if round_no > 1:
                prompt += ' This is a repair round. Preserve the existing structure unless an issue requires a change. Existing tree: '+json.dumps(prior_tree or [],ensure_ascii=False)+' Validation issues: '+json.dumps(prior_issues or [],ensure_ascii=False)
            out,usage=translate_segment(text,meta_get(db,'source_lang','eng'),meta_get(db,'source_lang','eng'),'hosted',project/'.env',None,prompt,model)
            record_usage(db,run_id=f'structure-{run_id}',stage='structure-discovery',request_key=cache_key('structure',key),provider='hosted',model=usage.get('model',model),input_tokens=usage.get('input_tokens',0),cached_input_tokens=usage.get('cached_input_tokens',0),output_tokens=usage.get('output_tokens',0),total_cost=usage.get('cost',0),currency=usage.get('currency','USD'),cache_hit=0,details_json=json.dumps(usage.get('service_usages',[]),ensure_ascii=False),trace_id=usage.get('trace_id'),created=now())
            try: result=json.loads(out)
            except json.JSONDecodeError: result={'headings':[],'toc_candidates':[],'unresolved':['model returned non-JSON']}
            db.execute('INSERT OR REPLACE INTO structure_chunks(run_id,chunk_key,result_json,status) VALUES(?,?,?,?)',(run_id,key,json.dumps(result,ensure_ascii=False),'completed')); db.commit(); hit=False
        # Validate cached and fresh responses alike. Cached rows may predate
        # stricter response validation.
        if not isinstance(result,dict):
            result={'headings':[],'toc_candidates':[],'unresolved':['model returned a non-object JSON response']}
        if not isinstance(result.get('headings'),list):
            result['unresolved']=(result.get('unresolved') if isinstance(result.get('unresolved'),list) else [])+['model headings field was not a list']; result['headings']=[]
        # Add corrected headings as deterministic candidates; the model can
        # enrich hierarchy, but it cannot erase strong source evidence.
        for u in chunk:
            if u['kind'] in ('heading','subheading'):
                evidence_rows=_unit_source_evidence(db,u)
                running=_is_running_header(db,u,evidence_rows)
                is_chapter = u['kind']=='heading' and (u['ordinal']==1 or str(u['text']).strip().isupper() or re.match(r'(?i)^\s*(chapter|chapitre)\b', str(u['text'])))
                if running:
                    unresolved.append({'issue':'running_header_demoted','unit':u['ordinal'],'title':u['text']})
                    continue
                ev={'source':'corrected_unit','source_blocks':[x['id'] for x in evidence_rows],
                    'source_pages':[x['number'] for x in evidence_rows]}
                summaries.append({'title':u['text'],'node_type':'chapter' if is_chapter else 'section','unit':u['ordinal'],'heading_level':1 if is_chapter else (2 if u['kind']=='subheading' else 1),'confidence':u['confidence'] or 0.8,'evidence':ev})
        # Model suggestions are advisory: only accept them when they point to
        # a global unit that is itself a corrected heading.  This prevents
        # local 1-based positions (and duplicate competing nodes) from
        # corrupting the hierarchy.
        corrected_ordinals={u['ordinal'] for u in chunk if u['kind'] in ('heading','subheading') and not _is_running_header(db,u,_unit_source_evidence(db,u))}
        for candidate in result.get('headings',[]):
            if not isinstance(candidate,dict):
                unresolved.append({'issue':'invalid_candidate','candidate':candidate}); continue
            try: cu=int(candidate.get('unit'))
            except (TypeError,ValueError): unresolved.append({'issue':'invalid_unit','candidate':candidate}); continue
            if not (chunk[0]['ordinal'] <= cu <= chunk[-1]['ordinal']): unresolved.append({'issue':'out_of_range_unit','candidate':candidate}); continue
            if cu in corrected_ordinals:
                candidate['unit']=cu; summaries.append(candidate)
        unresolved.extend(result.get('unresolved',[]) or [])
        _progress('structure',n,len(chunks),f'round {round_no} · {phase} · units {key}' + (' (cached)' if hit else ''))
    if round_no > 1:
        # Preserve unchanged nodes after applying repair candidates.  The
        # sorted/dedup pass below gives current-round edits precedence.
        summaries.extend({'title':old.get('title'),'node_type':old.get('node_type','section'),'unit':old.get('start_unit'),'heading_level':old.get('heading_level'),'confidence':old.get('confidence'),'evidence':{'source':'previous_round'}} for old in (prior_tree or []))
    # Deterministically repair a common OCR split: a chapter number followed
    # immediately by its title.  Keep the number's unit as the chapter anchor.
    summaries.sort(key=lambda x: int(x.get('unit') or 10**9))
    merged=[]; i=0
    while i < len(summaries):
        cur=summaries[i]
        if i+1 < len(summaries) and re.fullmatch(r'\s*(?:chapter|chapitre)?\s*\d+[.)]?\s*', str(cur.get('title','')), re.I):
            nxt=summaries[i+1]
            try: adjacent=int(nxt.get('unit')) == int(cur.get('unit')) + 1
            except (TypeError,ValueError): adjacent=False
            if adjacent:
                nxt=dict(nxt); nxt['title']=f'{cur.get("title")} {nxt.get("title")}'.strip(); nxt['unit']=cur.get('unit'); nxt['node_type']='chapter'; nxt['heading_level']=1
                merged.append(nxt); i+=2; continue
        merged.append(cur); i+=1
    summaries=merged
    summaries,reconcile_warnings=_reconcile_structure_candidates(units,summaries)
    content_start=None; backmatter_start=None
    unresolved.extend(reconcile_warnings)
    # Global reconciliation is deterministic over candidates; this keeps the
    # stage resumable and avoids an unbounded full-book request.
    db.execute('DELETE FROM structure_nodes WHERE run_id=?',(run_id,))
    # Models may emit ``unit: null`` (or a non-numeric value).  Sorting those
    # directly against integer unit numbers raises ``NoneType < int`` and
    # aborts the whole structure stage.  Normalize only valid unit numbers and
    # leave invalid candidates to be skipped below.
    def heading_unit(item):
        try:
            value=item.get('unit')
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    summaries=sorted(summaries,key=lambda x:(heading_unit(x) is None, heading_unit(x) or 0)); seen=set(); ordinal=0; last_chapter_id=None
    for h in summaries:
        title=re.sub(r'\s+',' ',str(h.get('title','')).strip()); unit=heading_unit(h) or 0
        if heading_unit(h) is None or heading_unit(h) <= 0: continue
        unit=heading_unit(h)
        norm=title.casefold()
        if not title or norm in seen or h.get('node_type') not in ('part','chapter','section','subsection'): continue
        seen.add(norm); ordinal+=1; level=min(6,max(1,int(h.get('heading_level') or (1 if h.get('node_type')=='chapter' else 2))))
        # A chapter is always top-level.  Prevent malformed model levels from
        # nesting a chapter below another chapter.
        if h.get('node_type') in ('part','chapter'): level=1
        # Sections always belong to the nearest preceding top-level chapter.
        # This remains true even when correction supplied a generic heading
        # classification or the model returned an invalid level.
        if h.get('node_type') in ('section','subsection'):
            parent_id=last_chapter_id
            level=max(2,level)
        else:
            parent_id=None
        num=_chapter_number(title) if h.get('node_type')=='chapter' else (_part_number(title)[0] if h.get('node_type')=='part' and _part_number(title) else None)
        evidence=h.get('evidence') if isinstance(h.get('evidence'),dict) else {'raw':h.get('evidence')}
        nsrc='printed' if num is not None else str(evidence.get('number_source','none'))
        nformat='arabic' if num is not None else None
        cur=db.execute('INSERT INTO structure_nodes(run_id,ordinal,parent_id,node_type,title,number,number_source,number_format,source_derived,heading_level,start_unit,end_unit,confidence,evidence_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(run_id,ordinal,parent_id,h.get('node_type','section'),title,num,nsrc,nformat,1,level,unit,None,h.get('confidence'),json.dumps(evidence,ensure_ascii=False),'needs_review')).lastrowid
        if h.get('node_type')=='chapter': last_chapter_id=cur
    nodes=db.execute('SELECT id,ordinal,start_unit FROM structure_nodes WHERE run_id=? ORDER BY start_unit,ordinal',(run_id,)).fetchall()
    for i,node in enumerate(nodes):
        end=(nodes[i+1]['start_unit']-1) if i+1 < len(nodes) else units[-1]['ordinal']
        db.execute('UPDATE structure_nodes SET end_unit=? WHERE id=?',(max(node['start_unit'],end),node['id']))
    db.execute('UPDATE structure_runs SET status=?,completed=?,unresolved_json=?,candidate_count=?,content_start_unit=?,backmatter_start_unit=? WHERE id=?',('needs_review',now(),json.dumps(unresolved,ensure_ascii=False),len(summaries),content_start,backmatter_start,run_id))
    snapshot=_structure_snapshot(db,run_id)
    fp=hashlib.sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    ver=(db.execute('SELECT COALESCE(MAX(version),0)+1 FROM structure_versions WHERE run_id=?',(run_id,)).fetchone()[0])
    db.execute('INSERT INTO structure_versions(run_id,version,phase,tree_json,issues_json,fingerprint,quality_score,accepted,created) VALUES(?,?,?,?,?,?,?,?,?)',(run_id,ver,phase,json.dumps(snapshot,ensure_ascii=False),json.dumps(unresolved,ensure_ascii=False),fp,max(0,1-len(unresolved)/max(1,len(snapshot))),1,now()))
    db.commit(); db.close(); print(f'[structure] generated {ordinal} nodes; saved version {ver} ({phase})',flush=True)

def structure_discovery(project, model=None, chunk_size=80, debug=False):
    """Run only the first, chunked discovery pass."""
    _structure_once(project, model=model, reset=False, chunk_size=chunk_size, debug=debug, round_no=1)

def _refinement_call(db, run_id, project, model, step, system, user):
    """Execute and durably cache one bounded refinement step."""
    size=request_bytes(system,user)
    if size > REQUEST_LIMIT - 512:
        raise ValueError(f'{step} request is {size} bytes, above the refinement request limit')
    key=cache_key('structure-refinement-v2',str(run_id),model,step,system,user)
    cached=db.execute('SELECT result_json FROM structure_chunks WHERE run_id=? AND chunk_key=?',(run_id,key)).fetchone()
    if cached:
        return json.loads(cached['result_json']),True
    out,usage=translate_segment(user,meta_get(db,'source_lang','eng'),meta_get(db,'source_lang','eng'),
                                'hosted',project/'.env',None,system,model,max_attempts=3)
    try: result=json.loads(out)
    except json.JSONDecodeError as exc: raise ValueError(f'{step} returned invalid JSON') from exc
    if not isinstance(result,dict): raise ValueError(f'{step} returned non-object JSON')
    if ':batch:' in step:
        if result.get('action')=='inspect':
            if not result.get('inspect_units'): raise ValueError(f'{step} requested inspection without units')
        else:
            nodes=result.get('nodes')
            if not isinstance(nodes,list): raise ValueError(f'{step} returned no node list')
            anchors={h['ordinal'] for h in json.loads(user)['headings']}
            try:
                proposed={int(n.get('unit',n.get('start_unit'))) for n in nodes if isinstance(n,dict)}
                excluded={int(x.get('ordinal',x.get('unit')) if isinstance(x,dict) else x)
                          for x in result.get('excluded_units') or []}
            except (TypeError,ValueError) as exc: raise ValueError(f'{step} returned invalid anchors') from exc
            if proposed|excluded!=anchors:
                raise ValueError(f'{step} left {len(anchors-(proposed|excluded))} headings unaccounted for')
    db.execute('INSERT INTO structure_chunks(run_id,chunk_key,result_json,status) VALUES(?,?,?,?)',
               (run_id,key,json.dumps(result,ensure_ascii=False),'completed'))
    record_usage(db,run_id=f'structure-{run_id}',stage='structure-refinement',request_key=key,
                 provider='hosted',model=usage.get('model',model),input_tokens=usage.get('input_tokens',0),
                 cached_input_tokens=usage.get('cached_input_tokens',0),output_tokens=usage.get('output_tokens',0),
                 total_cost=usage.get('cost',0),currency=usage.get('currency','USD'),cache_hit=0,
                 details_json=json.dumps(usage.get('service_usages',[]),ensure_ascii=False),
                 trace_id=usage.get('trace_id'),created=now())
    return result,False

def structure_refinement(project, model=None, chunk_size=80, debug=False):
    """Refine the entire persisted candidate tree in one call per round."""
    load_dotenv(Path.cwd()/'.env'); db=connect(project)
    run=db.execute('SELECT * FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone()
    if not run: db.close(); raise RuntimeError('Run structure-discovery before structure-refinement')
    if run['status'] in ('completed','completed_with_warnings') and run['phase']=='toc_authoritative':
        db.close(); print('[structure] authoritative TOC already accepted; refinement not required',flush=True); return
    model=model or os.environ.get('BOOK2EPUB_STRUCTURE_MODEL') or os.environ.get('BOOK2EPUB_ASSEMBLY_MODEL') or os.environ.get('BOOK2EPUB_CORRECTION_MODEL')
    max_rounds=_structure_max_rounds()
    start=max(2,int(run['round'] or 1)+1); previous=run['fingerprint']
    for round_no in range(start,max_rounds+1):
        _check_cancelled()
        tree=_structure_snapshot(db,run['id'])
        all_units=[dict(row) for row in db.execute('SELECT * FROM manuscript_units WHERE run_id=? ORDER BY ordinal',(run['assembly_run_id'],)).fetchall()]
        unit_map={u['ordinal']:u for u in all_units}
        for unit in all_units:
            if unit['kind'] not in ('heading','subheading'): continue
            source=_unit_source_evidence(db,unit)
            unit['source_pages']=sorted({item['number'] for item in source})
            unit['running_header']=_is_running_header(db,unit,source)
        batches=make_batches(all_units,tree)
        if not batches: raise ValueError('No source headings available for refinement')
        step_count=len(batches)+1
        total_steps=(max_rounds-1)*step_count
        base=(round_no-2)*step_count
        proposed=[]; batch_issues=[]; excluded=set()
        for index,batch in enumerate(batches,1):
            _check_cancelled()
            _progress('structure-refinement',base+index-1,total_steps,
                      f'round {round_no}/{max_rounds} · batch {index}/{len(batches)}')
            inspected=[]
            for inspection_no in range(3):
                system,user=make_request(batch,inspected)
                result,hit=_refinement_call(db,run['id'],project,model,
                                            f'round:{round_no}:batch:{index}:inspection:{inspection_no}',system,user)
                if result.get('action')!='inspect': break
                requested=[]
                for raw in (result.get('inspect_units') or [])[:12]:
                    try: center=int(raw)
                    except (TypeError,ValueError): continue
                    for ordinal in range(max(1,center-2),min(all_units[-1]['ordinal'],center+2)+1):
                        unit=unit_map.get(ordinal)
                        if unit: requested.append({'ordinal':ordinal,'kind':unit['kind'],
                                                   'text':str(unit['text'] or '')[:600]})
                inspected=list({item['ordinal']:item for item in requested}.values())
                while inspected and request_bytes(*make_request(batch,inspected))>REQUEST_LIMIT-512:
                    inspected.pop()
                if not inspected: raise ValueError(f'batch {index} requested inspection without usable bounded evidence')
            else:
                raise ValueError(f'batch {index} requested inspection three times without a proposal')
            nodes=result.get('nodes')
            if not isinstance(nodes,list): raise ValueError(f'batch {index} returned no node list')
            anchors={h['ordinal'] for h in batch['headings']}
            local=[]
            for node in nodes:
                if not isinstance(node,dict): raise ValueError(f'batch {index} returned a malformed node')
                item=dict(node)
                try: item['unit']=int(item.get('unit',item.get('start_unit')))
                except (TypeError,ValueError): raise ValueError(f'batch {index} returned an invalid node anchor')
                if item['unit'] not in anchors: raise ValueError(f'batch {index} returned an out-of-batch anchor')
                local.append(item)
            omitted=set()
            for item in result.get('excluded_units') or []:
                try: omitted.add(int(item.get('ordinal',item.get('unit')) if isinstance(item,dict) else item))
                except (TypeError,ValueError): continue
            if not omitted.issubset(anchors): raise ValueError(f'batch {index} excluded an out-of-batch anchor')
            covered={item['unit'] for item in local}|omitted
            if covered!=anchors: raise ValueError(f'batch {index} left {len(anchors-covered)} headings unaccounted for')
            proposed.extend(local); excluded.update(omitted)
            batch_issues.extend(result.get('unresolved') or [])
            _progress('structure-refinement',base+index,total_steps,
                      f'round {round_no}/{max_rounds} · batch {index}/{len(batches)} complete'+(' (cached)' if hit else ''))
        excluded_headings=[{'ordinal':unit['ordinal'],'text':unit['text']}
                           for unit in all_units if unit['ordinal'] in excluded]
        global_system,global_user=make_global_request(proposed,batch_issues,
                                                       excluded_headings,all_units[-1]['ordinal'])
        global_result,hit=_refinement_call(db,run['id'],project,model,
                                           f'round:{round_no}:global',global_system,global_user)
        for update in global_result.get('updates') or []:
            if not isinstance(update,dict): raise ValueError('global refinement returned a malformed update')
            try: anchor=int(update['start_unit'])
            except (KeyError,TypeError,ValueError): raise ValueError('global refinement returned an invalid update anchor')
            target=next((n for n in proposed if n['unit']==anchor),None)
            if target is None: raise ValueError('global refinement updated an unknown anchor')
            for field in ('title','node_type','number','number_source','parent_start_unit','heading_level','confidence'):
                if field in update: target[field]=update[field]
        remove={int(x) for x in global_result.get('remove_units') or []}
        if not remove.issubset({n['unit'] for n in proposed}):
            raise ValueError('global refinement removed an unknown anchor')
        proposed=[node for node in proposed if node['unit'] not in remove]
        result={'content_start_unit':global_result.get('content_start_unit'),
                'backmatter_start_unit':global_result.get('backmatter_start_unit'),
                'unresolved':batch_issues+(global_result.get('unresolved') or []),
                'changes':global_result.get('changes') or []}
        issues=json.loads(run['validation_issues_json'] or '[]')
        _progress('structure-refinement',base+step_count,total_steps,
                  f'round {round_no}/{max_rounds} · global check complete'+(' (cached)' if hit else ''))
        # Running-header candidates are evidence for the agent, not a semantic
        # deletion rule. The validator only enforces structural safety.
        proposed,reconcile_warnings=_reconcile_structure_candidates(all_units,proposed)
        try: content_start=int(result.get('content_start_unit')); backmatter_start=int(result['backmatter_start_unit']) if result.get('backmatter_start_unit') is not None else all_units[-1]['ordinal']+1
        except (TypeError,ValueError): content_start=0; backmatter_start=0
        boundary_errors=[]
        if not (1<=content_start<backmatter_start<=all_units[-1]['ordinal']+1): boundary_errors.append({'severity':'error','issue':'invalid_content_boundaries','content_start_unit':content_start,'backmatter_start_unit':backmatter_start})
        validation,warnings=_validate_structure_tree(proposed,content_start,backmatter_start); validation=boundary_errors+validation
        # A malformed proposal must never erase the accepted tree from the
        # preceding round.  Keep the prior nodes and record the proposal as a
        # rejected version for diagnostics.
        accepted=bool(proposed and not validation)
        if accepted:
            db.execute('DELETE FROM structure_nodes WHERE run_id=?',(run['id'],)); ids={}
            for ordinal,node in enumerate(sorted(proposed,key=lambda n:int(n['unit'])),1):
                start=node['unit']; parent_id=ids.get(node.get('parent_start_unit')); ntype=node.get('node_type'); parsed=_chapter_number(node.get('title')) if ntype=='chapter' else (_part_number(node.get('title'))[0] if ntype=='part' and _part_number(node.get('title')) else None); number=node.get('number') if node.get('number') is not None else parsed; nsrc=node.get('number_source','printed' if number is not None else 'none')
                cur=db.execute('INSERT INTO structure_nodes(run_id,ordinal,parent_id,node_type,title,number,number_source,source_derived,heading_level,start_unit,end_unit,confidence,evidence_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(run['id'],ordinal,parent_id,ntype,node.get('title'),number,nsrc,1,node.get('heading_level') or (1 if ntype in ('part','chapter') else 2),start,start,node.get('confidence'),json.dumps(node.get('evidence',{}),ensure_ascii=False),'needs_review')).lastrowid; ids[start]=cur
            ordered=db.execute('SELECT id,start_unit FROM structure_nodes WHERE run_id=? ORDER BY start_unit,ordinal',(run['id'],)).fetchall()
            for i,node in enumerate(ordered):
                end=(ordered[i+1]['start_unit']-1) if i+1<len(ordered) else (backmatter_start-1 if backmatter_start else all_units[-1]['ordinal'])
                db.execute('UPDATE structure_nodes SET end_unit=? WHERE id=?',(max(node['start_unit'],end),node['id']))
            db.execute('UPDATE structure_runs SET content_start_unit=?,backmatter_start_unit=? WHERE id=?',(content_start,backmatter_start,run['id']))
        current=_structure_snapshot(db,run['id'])
        fp=hashlib.sha256(json.dumps(current,ensure_ascii=False,sort_keys=True).encode()).hexdigest(); unresolved=(result.get('unresolved') or [])+validation+warnings+reconcile_warnings; stable=accepted and fp==previous; quality=max(0,1-len(unresolved)/max(1,len(current)))
        ver=db.execute('SELECT COALESCE(MAX(version),0)+1 FROM structure_versions WHERE run_id=?',(run['id'],)).fetchone()[0]
        db.execute('INSERT INTO structure_versions(run_id,version,phase,tree_json,issues_json,fingerprint,quality_score,accepted,changes_json,created) VALUES(?,?,?,?,?,?,?,?,?,?)',(run['id'],ver,'agent_proposal',json.dumps(current,ensure_ascii=False),json.dumps(unresolved,ensure_ascii=False),fp,quality,1 if accepted else 0,json.dumps(result.get('changes') or [],ensure_ascii=False),now()))
        hard_errors=[x for x in unresolved if isinstance(x,dict) and x.get('severity')=='error']
        final=stable and accepted and not hard_errors; status=('completed_with_warnings' if final and unresolved else 'completed') if final else ('failed_validation' if round_no==max_rounds else 'needs_review')
        db.execute('UPDATE structure_runs SET round=?,max_rounds=?,phase=?,fingerprint=?,quality_score=?,convergence=?,validation_issues_json=?,unresolved_json=?,last_repair_summary=?,status=?,completed=? WHERE id=?',(round_no,max_rounds,'completed' if final else 'refinement',fp,quality,1 if final else 0,json.dumps(unresolved,ensure_ascii=False),json.dumps(result.get('unresolved') or [],ensure_ascii=False),json.dumps(result.get('changes') or [],ensure_ascii=False),status,now() if status!='needs_review' else None,run['id']))
        db.commit(); previous=fp; run=db.execute('SELECT * FROM structure_runs WHERE id=?',(run['id'],)).fetchone()
        _progress('structure-refinement',total_steps if final else base+step_count,total_steps,
                  f'round {round_no} · {"accepted" if accepted else "rejected"} · {len(unresolved)} issues'+(' (cached)' if hit else ''))
        if final: break
    final_status=run['status']
    db.close()
    if final_status=='failed_validation':
        raise RuntimeError(f'Structure refinement did not converge within {max_rounds} rounds; inspect saved issues and versions')

def structure(project, model=None, reset=False, chunk_size=80, debug=False):
    """Run candidate discovery once, then whole-tree refinement rounds."""
    if reset: reset_stage(project,'structure')
    probe=connect(project); prior=probe.execute('SELECT id FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone(); probe.close()
    if not prior: structure_discovery(project,model=model,chunk_size=chunk_size,debug=debug)
    structure_refinement(project,model=model,chunk_size=chunk_size,debug=debug)

def build(project,out,reset=False):
    db=connect(project); lang=meta_get(db,'target_lang',meta_get(db,'source_lang','eng'))
    assembly_check=db.execute('SELECT id,status FROM assembly_runs ORDER BY generation DESC LIMIT 1').fetchone()
    if assembly_check and assembly_check['status']!='completed':
        db.close(); raise RuntimeError(f'Build blocked: assembly validation status is {assembly_check["status"]}')
    structure_check=db.execute('SELECT status,validation_issues_json,assembly_run_id,content_start_unit,backmatter_start_unit FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone()
    if structure_check and structure_check['status'] not in ('completed','completed_with_warnings'):
        db.close(); raise RuntimeError(f'Build blocked: structure status is {structure_check["status"]}')
    if structure_check and assembly_check and structure_check['assembly_run_id'] != assembly_check['id']:
        db.close(); raise RuntimeError('Build blocked: structure belongs to an older assembly; rerun structure')
    if structure_check:
        persisted=_structure_snapshot(db,db.execute('SELECT id FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone()['id'])
        candidate=[dict(n,unit=n['start_unit']) for n in persisted]
        hard,_warnings=_validate_structure_tree(
            candidate, structure_check['content_start_unit'],
            structure_check['backmatter_start_unit'],
            allow_backmatter=structure_check['status'] in ('completed', 'completed_with_warnings')
            and db.execute("SELECT phase FROM structure_runs ORDER BY generation DESC LIMIT 1").fetchone()['phase'] == 'toc_authoritative')
        if hard:
            db.close(); raise RuntimeError(f'Build blocked: structure has {len(hard)} hard validation errors; rerun structure refinement')
    title=meta_get(db,'title',None); author=meta_get(db,'author',None)
    # Prefer explicit metadata, then recover a useful title/author from the
    # first OCR page (common for scanned title pages).
    first=db.execute('SELECT text,image FROM pages ORDER BY number LIMIT 1').fetchone()
    if first and (not title or title in ('Scanned book', project.name)):
        lines=[re.sub(r'\s+',' ',x).strip(' -–—') for x in (first['text'] or '').splitlines() if x.strip()]
        author_end=0
        if lines and not author:
            author_parts=[]
            for line in lines[:2]:
                if line.isupper() and len(line)>2: author_parts.append(line)
                else: break
            if author_parts:
                author=' '.join(author_parts).title(); author_end=len(author_parts)
        upper=[x for x in lines[author_end:] if x.isupper() and len(x)>2]
        if upper: title=' '.join(upper[:3]).title()
    title=title or project.name.replace('-',' ').title(); author=author or 'Unknown author'
    # Review is intentionally non-blocking: users can inspect the generated
    # assembly without having to approve thousands of units one by one.  Build
    # the latest assembly run using every non-discarded unit; approval remains
    # available as metadata for future editorial workflows.
    run=db.execute("SELECT id FROM assembly_runs ORDER BY generation DESC LIMIT 1").fetchone()
    if run:
        rows=db.execute("SELECT ordinal,kind,text,decisions_json FROM manuscript_units WHERE run_id=? AND status!='discarded' ORDER BY ordinal",(run['id'],)).fetchall()
        nodes=db.execute("SELECT id,parent_id,start_unit,title,number,number_source,heading_level,node_type FROM structure_nodes WHERE run_id=(SELECT id FROM structure_runs ORDER BY generation DESC LIMIT 1) AND start_unit>0 ORDER BY start_unit,ordinal",()).fetchall()
        node_by_unit={n['start_unit']:n for n in nodes}
        body_parts=[]
        for r in rows:
            text_value=html.escape(r['text'] or '').replace(chr(10),' '); node=node_by_unit.get(r['ordinal'])
            # Structure anchors describe chapter/section ranges for navigation;
            # they do not change the semantic kind of the assembled unit. A
            # paragraph at a range start must remain prose rather than being
            # promoted to a large bold heading.
            if node and r['kind'] in ('heading','subheading'):
                # Structure controls hierarchy and navigation only.  The
                # assembled/corrected unit remains the authoritative prose;
                # never replace it with an LLM-normalized structure title.
                level=min(6,max(1,int(node['heading_level'] or 2))); body_parts.append(f'<h{level} id="u{r["ordinal"]}">{text_value}</h{level}>')
            elif r['kind']=='blockquote': body_parts.append(f'<blockquote id="u{r["ordinal"]}">{text_value}</blockquote>')
            else: body_parts.append(f'<p id="u{r["ordinal"]}">{text_value}</p>')
        body=''.join(body_parts)
        toc_nodes=sorted(nodes,key=lambda n:n['start_unit'])
        all_top_nodes=[n for n in toc_nodes if n['parent_id'] is None and n['node_type'] in ('part','chapter')]
        content_start=int(structure_check['content_start_unit'] or (all_top_nodes[0]['start_unit'] if all_top_nodes else rows[0]['ordinal']))
        backmatter_start=int(structure_check['backmatter_start_unit']) if structure_check['backmatter_start_unit'] else rows[-1]['ordinal']+1
        top_nodes=[n for n in all_top_nodes if content_start<=n['start_unit']<backmatter_start]
        file_for_unit={}; chapter_documents=[]; assigned={}
        markup_by_unit={r['ordinal']:m for r,m in zip(rows,body_parts)}
        def document(filename,doc_title,start,end):
            ordinals=[r['ordinal'] for r in rows if start<=r['ordinal']<end]
            for ordinal in ordinals:
                if ordinal in assigned: raise RuntimeError(f'Build partition duplicates assembly unit {ordinal}')
                assigned[ordinal]=filename; file_for_unit[ordinal]=filename
            if ordinals: chapter_documents.append((filename,doc_title,''.join(markup_by_unit[x] for x in ordinals)))
        if top_nodes:
            document('frontmatter.xhtml','Front matter',rows[0]['ordinal'],content_start)
            boundaries=[(n['start_unit'],top_nodes[i+1]['start_unit'] if i+1<len(top_nodes) else backmatter_start,i+1,n) for i,n in enumerate(top_nodes)]
            for start,end,idx,node in boundaries:
                stem='part' if node['node_type']=='part' else 'chapter'
                fallback=(f'Part {node["number"]}' if node['node_type']=='part' else (f'Chapitre {node["number"]}' if str(lang).startswith('fra') else f'Chapter {node["number"]}'))
                document(f'{stem}-{idx:03d}.xhtml',node['title'] or fallback,start,min(end,backmatter_start))
            document('backmatter.xhtml','Back matter',backmatter_start,rows[-1]['ordinal']+1)
            expected={r['ordinal'] for r in rows}
            if set(assigned)!=expected:
                missing=sorted(expected-set(assigned)); raise RuntimeError(f'Build partition omits assembly units {missing[:20]}')
        else:
            chapter_documents=[('chapter-001.xhtml',title,body)]
            file_for_unit={r['ordinal']:'chapter-001.xhtml' for r in rows}
        def link(n):
            prefix=('Part ' if n['node_type']=='part' else 'Chapter ') if n['number'] is not None else ''
            bare=not n['title'] or bool(re.fullmatch(r'\s*(?:chapter|chapitre)?\s*\d+[.)]?\s*',n['title'] or '',re.I))
            already_numbered=(_chapter_number(n['title']) is not None if n['node_type']=='chapter' else _part_number(n['title']) is not None)
            label=(f'Chapitre {n["number"]}' if bare and str(lang).startswith('fra') else f'Chapter {n["number"]}') if bare else (n['title'] if already_numbered else (f'{prefix}{n["number"]} — {n["title"]}' if n['number'] is not None else n['title']))
            return f'<li><a href="{file_for_unit.get(n["start_unit"],"chapter-001.xhtml")}#u{n["start_unit"]}">{html.escape(label)}</a></li>'
        visible=[n for n in toc_nodes if content_start<=n['start_unit']<backmatter_start]
        toc_parts=[]
        if any(r['ordinal']<content_start for r in rows): toc_parts.append('<li><a href="frontmatter.xhtml">Front matter</a></li>')
        for n in visible:
            if n['node_type'] in ('part','chapter') and n['parent_id'] is None:
                children=[x for x in visible if x['parent_id']==n['id']]
                # Parts and chapters are siblings; only sections are nested.
                children=[x for x in children if x['node_type'] in ('section','subsection')]
                toc_parts.append(link(n)[:-5]+(('<ul>'+''.join(link(x) for x in children)+'</ul>') if children else '')+'</li>')
        if any(r['ordinal']>=backmatter_start for r in rows): toc_parts.append('<li><a href="backmatter.xhtml">Back matter</a></li>')
        toc=''.join(toc_parts)
    else:
        rows=db.execute("SELECT b.approved_text,b.corrected_text,b.text,b.translation FROM blocks b ORDER BY b.page_id,b.ordinal").fetchall(); body=''.join(f'<p>{html.escape((r["translation"] or r["corrected_text"] or r["approved_text"] or r["text"] or "").replace(chr(10)," "))}</p>' for r in rows)
        toc=''
    toc_markup = toc or '<li><a href="chapter-001.xhtml">Text</a></li>'
    if not run:
        chapter_documents=[('chapter-001.xhtml',title,body)]
    chapter_documents=chapter_documents or [('chapter-001.xhtml',title,body)]
    cover_path=(project/first['image']) if first and first['image'] else None
    if cover_path and cover_path.is_file():
        cover_ext=cover_path.suffix.lower()
        cover_media={'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp'}.get(cover_ext.lstrip('.'))
    else:
        cover_path=None; cover_ext=''; cover_media=None
    cover_name='cover'+cover_ext if cover_path and cover_media else None
    cover_manifest=(f'<item id="cover-image" href="images/{cover_name}" media-type="{cover_media}" properties="cover-image"/>'
                    '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>') if cover_name else ''
    cover_metadata='<meta name="cover" content="cover-image"/>' if cover_name else ''
    cover_spine='<itemref idref="cover" linear="yes"/>' if cover_name else ''
    with zipfile.ZipFile(out,'w') as z:
        container = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                     '<rootfiles><rootfile full-path="OEBPS/package.opf" '
                     'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr('mimetype',b'application/epub+zip',compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml',container)
        z.writestr('OEBPS/style.css','body{font-family:serif;line-height:1.5;margin:6%;}p{text-align:justify;}')
        if cover_name:
            z.writestr(f'OEBPS/images/{cover_name}',cover_path.read_bytes())
            z.writestr('OEBPS/cover.xhtml',f'<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{html.escape(lang)}"><head><title>Cover</title><meta name="viewport" content="width=device-width,height=device-height"/><style>html,body{{margin:0;padding:0;width:100%;height:100%;}}body{{display:flex;align-items:center;justify-content:center;}}img{{display:block;max-width:100%;max-height:100%;object-fit:contain;}}</style></head><body epub:type="cover"><img src="images/{cover_name}" alt="Cover of {html.escape(title)}"/></body></html>')
        for filename, doc_title, doc_body in chapter_documents:
            lead = f'<h1>{html.escape(title)}</h1><p class="author">{html.escape(author)}</p>' if filename == chapter_documents[0][0] else ''
            z.writestr(f'OEBPS/{filename}',f'<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="{html.escape(lang)}"><head><title>{html.escape(doc_title)}</title><link rel="stylesheet" href="style.css"/></head><body>{lead}{doc_body}</body></html>')
        z.writestr('OEBPS/nav.xhtml',f'<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc" id="toc"><ol>{toc_markup}</ol></nav></body></html>')
        manifest=''.join(f'<item id="c{i}" href="{html.escape(fn)}" media-type="application/xhtml+xml"/>' for i,(fn,_,_) in enumerate(chapter_documents))
        spine=''.join(f'<itemref idref="c{i}"/>' for i in range(len(chapter_documents)))
        z.writestr('OEBPS/package.opf',f'<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:uuid:{uuid.uuid4()}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:creator>{html.escape(author)}</dc:creator><dc:language>{html.escape(lang)}</dc:language>{cover_metadata}</metadata><manifest>{manifest}<item id="n" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="s" href="style.css" media-type="text/css"/>{cover_manifest}</manifest><spine>{cover_spine}{spine}</spine></package>')
    db.close()
    print(f'[build] wrote {out}', flush=True)
