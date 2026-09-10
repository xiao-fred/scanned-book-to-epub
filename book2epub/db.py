from __future__ import annotations
import hashlib, json, sqlite3, time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(id INTEGER PRIMARY KEY, number INTEGER UNIQUE, image TEXT, text TEXT, status TEXT DEFAULT 'ocr_pending', confidence REAL);
CREATE TABLE IF NOT EXISTS blocks(id INTEGER PRIMARY KEY, page_id INTEGER, ordinal INTEGER, kind TEXT DEFAULT 'paragraph', text TEXT, approved_text TEXT, translation TEXT, status TEXT DEFAULT 'needs_review', confidence REAL, x REAL, y REAL, w REAL, h REAL, revision INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, block_id INTEGER, action TEXT, old_value TEXT, new_value TEXT, created REAL);
CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT, created REAL);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS usage_events(id INTEGER PRIMARY KEY, run_id TEXT, stage TEXT, page_id INTEGER, block_id INTEGER, request_key TEXT, provider TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, cached_input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, total_cost REAL DEFAULT 0, currency TEXT DEFAULT 'USD', cache_hit INTEGER DEFAULT 0, details_json TEXT, trace_id TEXT, created REAL);
CREATE TABLE IF NOT EXISTS assembly_runs(id INTEGER PRIMARY KEY, generation INTEGER NOT NULL, source_lang TEXT, provider TEXT, model TEXT, window_size INTEGER DEFAULT 3, status TEXT DEFAULT 'needs_review', created REAL, completed REAL);
CREATE TABLE IF NOT EXISTS manuscript_units(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, kind TEXT DEFAULT 'paragraph', text TEXT, provenance_json TEXT, decisions_json TEXT, confidence REAL, status TEXT DEFAULT 'needs_review', FOREIGN KEY(run_id) REFERENCES assembly_runs(id));
CREATE TABLE IF NOT EXISTS assembly_events(id INTEGER PRIMARY KEY, unit_id INTEGER, action TEXT, old_value TEXT, new_value TEXT, created REAL);
CREATE TABLE IF NOT EXISTS assembly_windows(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, window_key TEXT NOT NULL, result_json TEXT, status TEXT DEFAULT 'completed', UNIQUE(run_id,window_key));
CREATE TABLE IF NOT EXISTS assembly_reconciliation(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, candidate_unit_id INTEGER, final_unit_id INTEGER, action TEXT, reason TEXT, evidence_json TEXT, created REAL);
CREATE TABLE IF NOT EXISTS structure_runs(id INTEGER PRIMARY KEY, generation INTEGER NOT NULL, source_lang TEXT, target_lang TEXT, model TEXT, status TEXT DEFAULT 'running', assembly_run_id INTEGER, created REAL, completed REAL, error TEXT);
CREATE TABLE IF NOT EXISTS structure_chunks(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, chunk_key TEXT NOT NULL, result_json TEXT, status TEXT DEFAULT 'completed', UNIQUE(run_id,chunk_key));
CREATE TABLE IF NOT EXISTS structure_nodes(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, parent_id INTEGER, node_type TEXT, title TEXT, number INTEGER, number_source TEXT DEFAULT 'none', number_format TEXT, source_derived INTEGER DEFAULT 1, heading_level INTEGER, start_unit INTEGER, end_unit INTEGER, confidence REAL, evidence_json TEXT, status TEXT DEFAULT 'needs_review');
CREATE TABLE IF NOT EXISTS structure_versions(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, version INTEGER NOT NULL, phase TEXT, tree_json TEXT NOT NULL, issues_json TEXT DEFAULT '[]', fingerprint TEXT, quality_score REAL DEFAULT 0, accepted INTEGER DEFAULT 0, created REAL, UNIQUE(run_id,version));
CREATE TABLE IF NOT EXISTS stage_jobs(id INTEGER PRIMARY KEY, stage TEXT NOT NULL, status TEXT NOT NULL, current TEXT, completed INTEGER DEFAULT 0, total INTEGER DEFAULT 0, error TEXT, log TEXT, reset INTEGER DEFAULT 0, created REAL, updated REAL, completed_at REAL);
"""
def connect(project: Path):
    project.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(project/'book.db',check_same_thread=False,timeout=10.0); c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=10000')
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript(SCHEMA)
    for sql in ("ALTER TABLE blocks ADD COLUMN corrected_text TEXT", "ALTER TABLE blocks ADD COLUMN correction_json TEXT", "ALTER TABLE blocks ADD COLUMN corrected_kind TEXT", "ALTER TABLE blocks ADD COLUMN corrected_heading_level INTEGER", "ALTER TABLE blocks ADD COLUMN corrected_confidence REAL", "ALTER TABLE usage_events ADD COLUMN details_json TEXT", "ALTER TABLE usage_events ADD COLUMN trace_id TEXT", "ALTER TABLE stage_jobs ADD COLUMN retry_count INTEGER DEFAULT 0", "ALTER TABLE stage_jobs ADD COLUMN failed_attempts INTEGER DEFAULT 0", "ALTER TABLE stage_jobs ADD COLUMN cancel_requested INTEGER DEFAULT 0", "ALTER TABLE stage_jobs ADD COLUMN last_progress_at REAL", "ALTER TABLE stage_jobs ADD COLUMN timing_samples_json TEXT", "ALTER TABLE stage_jobs ADD COLUMN uncached_total INTEGER DEFAULT 0", "ALTER TABLE stage_jobs ADD COLUMN uncached_completed INTEGER DEFAULT 0", "ALTER TABLE stage_jobs ADD COLUMN eta_seconds REAL", "ALTER TABLE structure_runs ADD COLUMN round INTEGER DEFAULT 0", "ALTER TABLE structure_runs ADD COLUMN max_rounds INTEGER DEFAULT 5", "ALTER TABLE structure_runs ADD COLUMN convergence INTEGER DEFAULT 0", "ALTER TABLE structure_runs ADD COLUMN fingerprint TEXT", "ALTER TABLE structure_runs ADD COLUMN candidate_count INTEGER DEFAULT 0", "ALTER TABLE structure_runs ADD COLUMN repairs_json TEXT", "ALTER TABLE structure_runs ADD COLUMN unresolved_json TEXT", "ALTER TABLE structure_runs ADD COLUMN phase TEXT DEFAULT 'discovery'", "ALTER TABLE structure_runs ADD COLUMN validation_issues_json TEXT DEFAULT '[]'", "ALTER TABLE structure_runs ADD COLUMN quality_score REAL DEFAULT 0", "ALTER TABLE structure_runs ADD COLUMN last_repair_summary TEXT", "ALTER TABLE structure_runs ADD COLUMN content_start_unit INTEGER", "ALTER TABLE structure_runs ADD COLUMN backmatter_start_unit INTEGER", "ALTER TABLE structure_versions ADD COLUMN changes_json TEXT DEFAULT '[]'", "ALTER TABLE structure_nodes ADD COLUMN number INTEGER", "ALTER TABLE structure_nodes ADD COLUMN number_source TEXT DEFAULT 'none'", "ALTER TABLE structure_nodes ADD COLUMN number_format TEXT"):
        try: c.execute(sql)
        except sqlite3.OperationalError: pass
    # Do not perform data migrations on every connection: the dashboard polls
    # this function concurrently with workers, and a write here can make every
    # status request fail with SQLITE_BUSY.  Legacy usage rows are handled by
    # the read-side accounting alias in server.py.
    c.commit(); return c
def cache_key(*parts): return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
def meta_get(db,key,default=None):
    r=db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone(); return json.loads(r[0]) if r else default
def meta_set(db,key,value):
    db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False))); db.commit()
def now(): return time.time()
def record_usage(db, **kw):
    fields=['run_id','stage','page_id','block_id','request_key','provider','model','input_tokens','cached_input_tokens','output_tokens','total_cost','currency','cache_hit','details_json','trace_id','created']
    vals=[kw.get(f,0 if f.endswith('tokens') or f in ('total_cost','cache_hit') else None) for f in fields]
    db.execute('INSERT INTO usage_events('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',vals); db.commit()
