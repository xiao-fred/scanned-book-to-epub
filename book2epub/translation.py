"""Provider adapter; one request is made per cached segment."""
import base64, json, os, time
from threading import Lock, Semaphore
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Hosted correction/translation calls must not be bursty, even if a caller
# invokes the pipeline from multiple threads.  A single gate also keeps the
# correction and translation models from competing with one another.
_REQUEST_LOCK = Lock()
_REQUEST_SEMAPHORE = Semaphore(3)
_LOG_LOCK = Lock()
_LAST_REQUEST_AT = 0.0
_RETRY_HOOK = None

def set_retry_hook(hook):
    """Install an observer called for failed attempts and retry success."""
    global _RETRY_HOOK
    _RETRY_HOOK = hook

def _request_log_path(env_file=None):
    """Return the project-local JSONL request log path."""
    if env_file:
        env_path = Path(env_file)
        return env_path.parent / 'logs' / 'model_requests.jsonl'
    return Path.cwd() / 'logs' / 'model_requests.jsonl'

def _append_request_log(env_file, entry):
    """Append one diagnostic event, best-effort so logging never breaks a run."""
    try:
        path = _request_log_path(env_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass

def _request_delay():
    """Return the minimum gap between hosted model request starts."""
    raw = os.environ.get('BOOK2EPUB_REQUEST_DELAY', '1.0')
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0

def _retry_settings():
    try:
        attempts = max(1, int(os.environ.get('BOOK2EPUB_LLM_MAX_ATTEMPTS', '10')))
    except ValueError:
        attempts = 10
    try:
        delay = max(0.0, float(os.environ.get('BOOK2EPUB_RETRY_DELAY', '5.0')))
    except ValueError:
        delay = 5.0
    return attempts, delay

def _retry_sleep(base, attempt):
    """Exponential backoff between attempts, capped to avoid runaway waits."""
    time.sleep(min(120.0, base * (2 ** (attempt - 1))))

def _post_json(request, timeout=120):
    """Execute one serialized, rate-limited model request."""
    global _LAST_REQUEST_AT
    with _REQUEST_SEMAPHORE:
      with _REQUEST_LOCK:
        wait = _request_delay() - (time.monotonic() - _LAST_REQUEST_AT)
        if wait > 0: time.sleep(wait)
        _LAST_REQUEST_AT = time.monotonic()
      with urlopen(request, timeout=timeout) as response:
        data = json.load(response)
        trace_id = response.headers.get('x-turing-trace-id')
    return data, trace_id

def trace_billing(url, key, trace_id):
    """Retrieve authoritative Turing billing when inline service usage is unavailable."""
    if not trace_id or 'coding.efficient.center' not in url: return []
    root=url.split('/api/v1/',1)[0]+'/api/v1'
    req=Request(f'{root}/trace/{trace_id}/billing',headers={'Authorization':'Bearer '+key} if key else {})
    try:
        time.sleep(2)
        with urlopen(req,timeout=30) as response:
            data=json.load(response) or []
            return data if isinstance(data,list) else data.get('service_usages',data.get('data',[]))
    except Exception:
        return []

def load_dotenv(*paths):
    """Load simple KEY=VALUE files without overriding the process environment."""
    for path in paths:
        if not path: continue
        path=Path(path)
        if not path.is_file(): continue
        for raw in path.read_text(encoding='utf-8').splitlines():
            line=raw.strip()
            if not line or line.startswith('#'): continue
            if line.startswith('export '): line=line[7:].lstrip()
            if '=' not in line: continue
            key,value=line.split('=',1); key=key.strip(); value=value.strip()
            if len(value)>=2 and value[0]==value[-1] and value[0] in "'\"": value=value[1:-1]
            if key and key not in os.environ: os.environ[key]=value

def translate_segment(text, source, target, provider='mock', env_file=None, image_path=None, system_prompt=None, model=None):
    """Run one correction, translation, or assembly LLM operation.

    All hosted operations use the same serialized request/retry/logging path.
    """
    # Configuration is shared at the invocation root; env_file is retained
    # only as the project location for request logs.
    load_dotenv(Path.cwd()/'.env')
    if provider == 'mock': return text, {'input_tokens':0,'cached_input_tokens':0,'output_tokens':0,'cost':0.0,'model':'mock'}
    url=os.environ.get('BOOK2EPUB_TRANSLATION_URL')
    if not url: raise RuntimeError('Set BOOK2EPUB_TRANSLATION_URL in the environment or a .env file')
    # Efficiency Center coding clients are restricted to Responses/Messages.
    if 'coding.efficient.center' in url and url.rstrip('/').endswith('/chat/completions'):
        url=url.rsplit('/chat/completions',1)[0]+'/responses'
    user_content=text
    if image_path and Path(image_path).is_file():
        data=base64.b64encode(Path(image_path).read_bytes()).decode(); mime='image/jpeg' if str(image_path).lower().endswith(('.jpg','.jpeg')) else 'image/png'
        user_content=[{'type':'text','text':text},{'type':'image_url','image_url':{'url':f'data:{mime};base64,{data}'}}]
    chosen_model=model or os.environ.get('BOOK2EPUB_TRANSLATION_MODEL')
    if not chosen_model:
        raise RuntimeError('Set BOOK2EPUB_TRANSLATION_MODEL in the project .env or pass an explicit model')
    use_turing_options='coding.efficient.center' not in url
    if url.rstrip('/').endswith('/responses'):
        system=system_prompt or f'Translate faithfully from {source} to {target}. Preserve formatting.'
        user=[]
        if isinstance(user_content,list):
            user=[{'type':'input_text','text':user_content[0]['text']},{'type':'input_image','image_url':user_content[1]['image_url']['url']}]
        else: user=[{'type':'input_text','text':user_content}]
        payload={'model':chosen_model,'input':[{'role':'system','content':[{'type':'input_text','text':system}]},{'role':'user','content':user}]}
    else:
        payload={'model':chosen_model,'messages':[{'role':'system','content':system_prompt or f'Translate faithfully from {source} to {target}. Preserve formatting.'},{'role':'user','content':user_content}]}
    if use_turing_options: payload['turing_options']={'include_service_usages':True}
    headers={'Content-Type':'application/json','User-Agent':os.environ.get('BOOK2EPUB_USER_AGENT','book2epub/0.1')}; key=os.environ.get('BOOK2EPUB_TRANSLATION_KEY')
    if key: headers['Authorization']='Bearer '+key
    log_entry={'timestamp':time.time(),'provider':provider,'url':url,'model':chosen_model,
               'source':source,'target':target,'input_text':text,
               'system_prompt':system_prompt or f'Translate faithfully from {source} to {target}. Preserve formatting.',
               'image_path':str(image_path) if image_path else None,
               'image_attached':bool(image_path and Path(image_path).is_file())}
    max_attempts, retry_delay = _retry_settings()
    successful_attempt_log = None
    for attempt in range(1, max_attempts + 1):
        attempt_log = dict(log_entry, attempt=attempt, max_attempts=max_attempts,
                           started_at=time.time())
        try:
            data, trace_id = _post_json(Request(url,data=json.dumps(payload).encode(),headers=headers,method='POST'))
            attempt_log.update({'success':True, 'trace_id':trace_id or data.get('trace_id')})
            log_entry['trace_id'] = trace_id or data.get('trace_id')
            successful_attempt_log = attempt_log
        except HTTPError as exc:
            detail=exc.read().decode('utf-8','replace')[:1000]
            attempt_log.update({'success':False, 'trace_id':exc.headers.get('x-turing-trace-id') if exc.headers else None,
                                'output':None,'service_usages':[],'cost':0.0,
                                'error':f'HTTP {exc.code}: {detail}'})
            _append_request_log(env_file, attempt_log)
            print(f'[llm] attempt {attempt}/{max_attempts} failed (HTTP {exc.code})', flush=True)
            if _RETRY_HOOK:
                try: _RETRY_HOOK('failure', attempt, max_attempts, f'HTTP {exc.code}')
                except Exception: pass
            if attempt == max_attempts:
                raise RuntimeError(f'LLM provider rejected the request (HTTP {exc.code}) at {url}. Check API key, endpoint, and model access. Response: {detail}') from exc
            _retry_sleep(retry_delay, attempt)
        except (URLError, TimeoutError, ConnectionError, OSError, ValueError) as exc:
            reason = getattr(exc, 'reason', None) or str(exc) or exc.__class__.__name__
            attempt_log.update({'success':False, 'trace_id':None, 'output':None,
                                'service_usages':[],'cost':0.0,
                                'error':f'{exc.__class__.__name__}: {reason}'})
            _append_request_log(env_file, attempt_log)
            print(f'[llm] attempt {attempt}/{max_attempts} failed ({exc.__class__.__name__})', flush=True)
            if _RETRY_HOOK:
                try: _RETRY_HOOK('failure', attempt, max_attempts, exc.__class__.__name__)
                except Exception: pass
            if attempt == max_attempts:
                raise RuntimeError(f'Could not complete LLM request at {url}: {reason}') from exc
            _retry_sleep(retry_delay, attempt)
        else:
            if attempt > 1:
                print(f'[llm] retry succeeded on attempt {attempt}/{max_attempts}', flush=True)
                if _RETRY_HOOK:
                    try: _RETRY_HOOK('success', attempt, max_attempts, '')
                    except Exception: pass
            break
    usage=data.get('usage',{}); services=data.get('service_usages') or []
    if not services:
        header_trace=trace_id or data.get('trace_id')
        services=trace_billing(url,key,header_trace)
    cost=sum(float(s.get('bill_amount',0) or 0) for s in services); currency=(services[0].get('bill_currency','USD') if services else 'USD')
    if data.get('output_text') is not None: content=data['output_text']
    elif data.get('choices'): content=data['choices'][0]['message']['content']
    else:
        content=''.join(part.get('text','') for item in data.get('output',[]) for part in item.get('content',[]) if part.get('type') in ('output_text','text'))
    log_entry.update({'output':content,'service_usages':services,'cost':cost,'currency':currency,
                      'usage':usage,'response_keys':sorted(data.keys()),'success':True})
    successful_attempt_log.update(log_entry)
    _append_request_log(env_file, successful_attempt_log)
    return content, {'input_tokens':usage.get('prompt_tokens',usage.get('input_tokens',0)),'cached_input_tokens':usage.get('prompt_tokens_details',{}).get('cached_tokens',0),'output_tokens':usage.get('completion_tokens',usage.get('output_tokens',0)),'cost':cost,'currency':currency,'model':payload['model'],'service_usages':services,'trace_id':trace_id or data.get('trace_id')}
