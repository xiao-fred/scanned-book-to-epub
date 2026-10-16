#!/usr/bin/env python3
import argparse, collections, html, json, re, sqlite3, sys, zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

XHTML='http://www.w3.org/1999/xhtml'
def norm(value): return re.sub(r'\s+',' ',html.unescape(value or '')).strip()
def finding(items,severity,code,message,**where): items.append({'severity':severity,'code':code,'message':message,'location':where})

def audit(epub,project=None):
    findings=[]; texts=[]; ids={}; names=set(); unit_locations=collections.defaultdict(set)
    try:
        with zipfile.ZipFile(epub) as z:
            names=set(z.namelist()); bad=z.testzip()
            if bad: finding(findings,'fatal','bad_zip_entry',f'CRC failure in {bad}',path=bad)
            info=z.infolist()
            if not info or info[0].filename!='mimetype' or info[0].compress_type!=zipfile.ZIP_STORED:
                finding(findings,'error','mimetype_layout','mimetype must be the first uncompressed ZIP entry')
            if 'META-INF/container.xml' not in names: finding(findings,'fatal','missing_container','META-INF/container.xml is missing')
            roots=[]
            if 'META-INF/container.xml' in names:
                try:
                    c=ET.fromstring(z.read('META-INF/container.xml')); roots=[x.attrib.get('full-path') for x in c.iter() if x.tag.endswith('rootfile')]
                except Exception as exc: finding(findings,'fatal','invalid_container',str(exc),path='META-INF/container.xml')
            for root in roots:
                if root not in names: finding(findings,'fatal','missing_package',f'Package document {root} is missing',path=root)
                else:
                    try:
                        opf=ET.fromstring(z.read(root)); base=str(Path(root).parent); manifest={x.attrib.get('id'):str(Path(base)/x.attrib.get('href','')) for x in opf.iter() if x.tag.endswith('item')}
                        for x in opf.iter():
                            if x.tag.endswith('itemref') and manifest.get(x.attrib.get('idref')) not in names: finding(findings,'error','broken_spine','Spine target is missing',idref=x.attrib.get('idref'))
                    except Exception as exc: finding(findings,'fatal','invalid_package',str(exc),path=root)
            for name in sorted(n for n in names if n.lower().endswith(('.xhtml','.html'))):
                try: root=ET.fromstring(z.read(name))
                except Exception as exc: finding(findings,'fatal','invalid_xhtml',str(exc),path=name); continue
                local={}
                for element in root.iter():
                    if element.attrib.get('id'):
                        value=element.attrib['id']; local[value]=local.get(value,0)+1
                    if Path(name).name not in ('nav.xhtml','cover.xhtml') and element.tag.endswith(('p','blockquote','li','h1','h2','h3','h4','h5','h6')):
                        value=norm(''.join(element.itertext()))
                        if value: texts.append((name,value))
                ids[name]=set(local)
                for value in local:
                    if re.fullmatch(r'u\d+',value): unit_locations[int(value[1:])].add(name)
                for value,count in local.items():
                    if count>1: finding(findings,'error','duplicate_id',f'Duplicate XHTML id {value}',path=name,id=value)
            for name,value in texts:
                if re.search(r'\[\[\s*page\s*=.*?block\s*=',value,re.I): finding(findings,'error','raw_provenance_marker','Readable provenance marker found',path=name,text=value[:160])
            seen=collections.defaultdict(list)
            for name,value in texts:
                if len(value)>=80: seen[value.casefold()].append(name)
            for value,locations in seen.items():
                if len(locations)>1: finding(findings,'warning','exact_duplicate_text','Exact long text appears more than once',paths=locations,text=value[:160])
            for name in sorted(n for n in names if n.lower().endswith(('.xhtml','.html'))):
                try: root=ET.fromstring(z.read(name))
                except Exception: continue
                for a in root.iter():
                    href=a.attrib.get('href')
                    if not href: continue
                    target,_,frag=href.partition('#'); target=str(Path(name).parent/target) if target else name
                    if target not in names: finding(findings,'error','broken_link','Navigation/content target is missing',path=name,href=href)
                    elif frag and frag not in ids.get(target,set()): finding(findings,'error','broken_fragment','Fragment target is missing',path=name,href=href)
    except (OSError,zipfile.BadZipFile) as exc: finding(findings,'fatal','invalid_epub',str(exc),path=str(epub))

    if project:
        dbpath=Path(project)/'book.db'
        if not dbpath.exists(): finding(findings,'error','missing_project_db','book.db not found',path=str(dbpath))
        else:
            db=sqlite3.connect(dbpath); db.row_factory=sqlite3.Row
            run=db.execute('SELECT id,status FROM assembly_runs ORDER BY generation DESC LIMIT 1').fetchone()
            if not run: finding(findings,'error','missing_assembly','No assembly run exists')
            else:
                units=db.execute("SELECT ordinal,text,kind,provenance_json FROM manuscript_units WHERE run_id=? AND status!='discarded' ORDER BY ordinal,id",(run['id'],)).fetchall()
                epub_text=' '.join(v for _,v in texts).casefold()
                # Compare assembled units to the actual EPUB, not merely to
                # their provenance declarations.  Sampling both ends catches
                # truncated units while tolerating whitespace/tag boundaries.
                for unit in units:
                    utext=norm(unit['text']).casefold()
                    if len(utext) >= 80:
                        samples=[utext[:60],utext[-60:]]
                        if any(s and s not in epub_text for s in samples):
                            finding(findings,'error','unit_not_in_epub','Assembled unit text is absent or truncated in EPUB',unit=unit['ordinal'],text=norm(unit['text'])[:160])
                owners=collections.defaultdict(list); ordered=[]
                for unit in units:
                    try: refs=json.loads(unit['provenance_json'] or '[]')
                    except Exception: refs=[]
                    unit_text=norm(unit['text']).casefold()
                    for ref in refs:
                        if not isinstance(ref,dict) or not ref.get('block'): continue
                        bid=int(ref['block']); owners[bid].append(unit['ordinal'])
                        block=db.execute('SELECT b.corrected_text,b.approved_text,b.text,b.ordinal,p.number FROM blocks b JOIN pages p ON p.id=b.page_id WHERE b.id=?',(bid,)).fetchone()
                        if block:
                            ordered.append((block['number'],block['ordinal'],bid,unit['ordinal']))
                            source=norm(block['corrected_text'] or block['approved_text'] or block['text']).casefold()
                            probes=[source[i:i+80] for i in range(0,len(source),80) if len(source[i:i+80])>=30]
                            if probes and sum(p in unit_text for p in probes)/len(probes)<.5:
                                finding(findings,'error','false_provenance_coverage','Unit claims a block whose text is mostly absent',page=block['number'],block=bid,unit=unit['ordinal'])
                removed={}
                for r in db.execute("SELECT evidence_json FROM assembly_reconciliation WHERE run_id=? AND action='removed_furniture'",(run['id'],)):
                    try: item=json.loads(r[0]); removed[int(item['block'])]=item
                    except Exception: pass
                expected={r['id'] for r in db.execute("SELECT id FROM blocks WHERE trim(coalesce(corrected_text,approved_text,text,''))<>''")} - set(removed)
                missing=sorted(expected-set(owners)); duplicate=sorted(b for b,v in owners.items() if len(v)>1)
                if missing: finding(findings,'error','missing_source_blocks',f'{len(missing)} source blocks have no active unit',blocks=missing[:100])
                if duplicate: finding(findings,'error','duplicate_block_ownership',f'{len(duplicate)} source blocks have multiple active units',blocks=duplicate[:100])
                for prior,current in zip(ordered,ordered[1:]):
                    if current[:2]<prior[:2]: finding(findings,'error','source_order_reversal','Source block order reverses',previous=prior,current=current)
                if run['status']!='completed': finding(findings,'error','assembly_not_validated',f'Latest assembly status is {run["status"]}')
                for ordinal,locations in unit_locations.items():
                    if len(locations)>1: finding(findings,'error','unit_in_multiple_resources','Assembly unit is emitted into multiple XHTML resources',unit=ordinal,paths=sorted(locations))
            structure=db.execute('SELECT * FROM structure_runs ORDER BY generation DESC LIMIT 1').fetchone()
            if structure:
                if structure['status']=='failed_validation': finding(findings,'error','structure_failed_validation','Latest structure has hard validation errors')
                rejected=db.execute("SELECT COUNT(*) FROM structure_versions WHERE run_id=? AND accepted=0 AND phase='agent_proposal'",(structure['id'],)).fetchone()[0]
                if rejected: finding(findings,'warning','rejected_structure_rounds',f'{rejected} structure agent proposals were rejected',rounds=rejected)
                nodes=db.execute('SELECT node_type,title,number,parent_id,start_unit FROM structure_nodes WHERE run_id=? ORDER BY start_unit,ordinal',(structure['id'],)).fetchall(); starts=collections.Counter(n['start_unit'] for n in nodes)
                for start,count in starts.items():
                    if count>1: finding(findings,'error','duplicate_structure_anchor','Multiple structure nodes use one anchor',start_unit=start,count=count)
                keys=collections.defaultdict(list)
                for n in nodes:
                    title=re.sub(r'^\s*(?:chapter|chapitre)?\s*\d+[.)]?\s*','',n['title'] or '',flags=re.I); key=re.sub(r'\W+',' ',title.casefold()).strip()
                    if key: keys[key].append(dict(n))
                for key,items in keys.items():
                    if len(items)>1 and any(x['node_type']=='chapter' for x in items): finding(findings,'error','duplicate_chapter_title','Numbered/unnumbered or repeated chapter titles exist',title=key,nodes=items)
                for name in names:
                    if re.search(r'chapter-\d+\.xhtml$',name):
                        own=[o for o,paths in unit_locations.items() if name in paths]
                        if len(own)<=1: finding(findings,'warning','placeholder_content_file','Chapter/part resource contains no reading content beyond its heading',path=name,units=own)
            db.close()
    rank={'info':0,'warning':1,'error':2,'fatal':3}; worst=max((rank[x['severity']] for x in findings),default=0)
    recommendation='fail' if worst>=2 else ('pass_with_warnings' if worst==1 else 'pass')
    return {'epub':str(epub),'project_dir':str(project) if project else None,'recommendation':recommendation,
            'counts':dict(collections.Counter(x['severity'] for x in findings)),'findings':findings}

def markdown(report):
    lines=[f'# EPUB quality audit',f'',f'**Recommendation:** `{report["recommendation"]}`',f'',f'EPUB: `{report["epub"]}`',f'']
    for x in report['findings']:
        lines += [f'- **{x["severity"].upper()} · {x["code"]}** — {x["message"]}  ',f'  `{json.dumps(x["location"],ensure_ascii=False)}`']
    if not report['findings']: lines.append('- No findings.')
    return '\n'.join(lines)+'\n'

def main():
    p=argparse.ArgumentParser();p.add_argument('epub',type=Path);p.add_argument('--project-dir',type=Path);p.add_argument('--json-output',type=Path);p.add_argument('--markdown-output',type=Path);a=p.parse_args()
    report=audit(a.epub,a.project_dir); data=json.dumps(report,ensure_ascii=False,indent=2)
    if a.json_output:a.json_output.write_text(data+'\n',encoding='utf8')
    if a.markdown_output:a.markdown_output.write_text(markdown(report),encoding='utf8')
    print(data); return 2 if report['recommendation']=='fail' else 0
if __name__=='__main__':sys.exit(main())
