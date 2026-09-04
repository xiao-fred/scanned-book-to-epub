import argparse
import os, sys
from pathlib import Path
from .pipeline import init_project, reset_project, reset_stage, ocr, translate, correct, assemble, structure, build
from .db import connect
from .server import serve

def confirm_reset(scope):
    """Require a single-key y/n confirmation before destructive reset work."""
    prompt=f'Reset {scope}? This deletes derived processing data. [y/N] '
    try:
        if not sys.stdin.isatty():
            print(prompt + 'No interactive terminal; reset cancelled.', flush=True)
            return False
        print(prompt, end='', flush=True)
        import termios, tty
        fd=sys.stdin.fileno(); old=termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd); answer=os.read(fd,1).decode('utf-8','ignore').lower()
        finally:
            termios.tcsetattr(fd,termios.TCSADRAIN,old)
        print(answer or 'n', flush=True)
        return answer=='y'
    except (OSError, ImportError):
        try: return input(prompt).strip().lower()=='y'
        except EOFError: return False

def main():
    p=argparse.ArgumentParser(prog='book2epub'); s=p.add_subparsers(dest='cmd',required=True)
    a=s.add_parser('init'); a.add_argument('project',type=Path); a.add_argument('--pdf',type=Path,required=True); a.add_argument('--source-lang'); a.add_argument('--target-lang')
    a=s.add_parser('reset'); a.add_argument('project',type=Path); a.add_argument('--stage',choices=('ocr','correction','assembly','structure','translation','build')) ; a.add_argument('--out',type=Path,help='EPUB path when resetting build')
    a=s.add_parser('ocr'); a.add_argument('project',type=Path); a.add_argument('--lang'); a.add_argument('--start',type=int,default=1); a.add_argument('--end',type=int); a.add_argument('--debug',action='store_true'); a.add_argument('--sample-pages',type=int,default=2,help='pages at beginning/end'); a.add_argument('--middle-pages',type=int,default=2,help='consecutive pages centered in the book'); a.add_argument('--sample-seed',type=int); a.add_argument('--sample-groups',default='beginning,middle,end')
    a=s.add_parser('translate'); a.add_argument('project',type=Path); a.add_argument('--target')
    a=s.add_parser('correct'); a.add_argument('project',type=Path); a.add_argument('--model'); a.add_argument('--source-lang'); a.add_argument('--debug',action='store_true'); a.add_argument('--sample-pages',type=int,default=2,help='pages at beginning/end'); a.add_argument('--middle-pages',type=int,default=2,help='consecutive pages centered in the book'); a.add_argument('--sample-seed',type=int); a.add_argument('--sample-groups',default='beginning,middle,end')
    a=s.add_parser('assemble'); a.add_argument('project',type=Path); a.add_argument('--model'); a.add_argument('--window-size',type=int,default=3)
    a=s.add_parser('structure'); a.add_argument('project',type=Path); a.add_argument('--model'); a.add_argument('--chunk-size',type=int,default=80); a.add_argument('--debug',action='store_true')
    a=s.add_parser('serve'); a.add_argument('project',type=Path); a.add_argument('--host',default='127.0.0.1'); a.add_argument('--port',type=int,default=8765)
    a=s.add_parser('build'); a.add_argument('project',type=Path); a.add_argument('--out',type=Path,required=True)
    a=s.add_parser('costs'); a.add_argument('project',type=Path)
    x=p.parse_args()
    if x.cmd=='init':init_project(x.project,x.pdf,x.source_lang,x.target_lang)
    elif x.cmd=='reset':
        if confirm_reset('the entire project' if not x.stage else x.stage):
            reset_project(x.project) if not x.stage else reset_stage(x.project,x.stage,x.out)
    elif x.cmd=='ocr':
        ocr(x.project,x.lang,x.start,x.end,x.debug,x.sample_pages,x.sample_seed,x.sample_groups,middle_pages=x.middle_pages)
    elif x.cmd=='translate':
        translate(x.project,x.target)
    elif x.cmd=='correct':
        correct(x.project,model=x.model,debug=x.debug,sample_pages=x.sample_pages,seed=x.sample_seed,groups=x.sample_groups,source_lang=x.source_lang,middle_pages=x.middle_pages)
    elif x.cmd=='assemble':
        assemble(x.project,model=x.model,window_size=x.window_size)
    elif x.cmd=='structure':
        structure(x.project,model=x.model,chunk_size=x.chunk_size,debug=x.debug)
    elif x.cmd=='serve':serve(x.project,x.host,x.port)
    elif x.cmd=='build':
        build(x.project,x.out)
    elif x.cmd=='costs':
        db=connect(x.project); rows=db.execute('SELECT COUNT(*) n,COALESCE(SUM(input_tokens),0) input,COALESCE(SUM(cached_input_tokens),0) cached,COALESCE(SUM(output_tokens),0) output,COALESCE(SUM(total_cost),0) cost FROM usage_events').fetchone(); result=dict(rows); scope=db.execute("SELECT value FROM metadata WHERE key='debug_scope'").fetchone(); result['debug_scope']=scope[0] if scope else None
        if scope:
            import json
            sample=len(json.loads(scope[0]).get('pages',[])); total=json.loads(db.execute("SELECT value FROM metadata WHERE key='page_count'").fetchone()[0]) if db.execute("SELECT value FROM metadata WHERE key='page_count'").fetchone() else sample
            if sample: result['projected_full_book_cost']=result['cost']*total/sample; result['projection_note']='simple sampled-page extrapolation; increase sample for confidence'
        print(result)
