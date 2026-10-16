# book2epub

Local-first, resumable scanned-PDF-to-reviewed-EPUB pipeline.

```bash
python3 -m book2epub init my-book --pdf /path/to/book.pdf
python3 -m book2epub ocr my-book --lang eng
python3 -m book2epub serve my-book
python3 -m book2epub build my-book --out my-book.epub
```

## Installation

The current MVP uses Python’s standard library, so there is no `requirements.txt` yet. A virtual environment is recommended:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m book2epub --help
```

On macOS install system OCR tools with `brew install python poppler tesseract tesseract-lang`. On Debian/Ubuntu use `sudo apt install python3 python3-venv poppler-utils tesseract-ocr` plus the required `tesseract-ocr-<lang>` packages. Verify with `command -v pdfinfo pdftoppm tesseract` and `tesseract --list-langs`.

## Create a project

`my-book` is only an example name. Choose any project directory. The PDF is copied into it and the original input is not modified:

```bash
python -m book2epub init /path/to/my-book-project --pdf /path/to/book.pdf --source-lang eng --target-lang fra
python -m book2epub reset /path/to/my-book-project  # reset the whole project; keep PDF and language configuration only
python -m book2epub reset /path/to/my-book-project --stage correction  # reset one stage only

All reset operations require a confirmation keypress. At the prompt, press
`y` or `n`; when run from a terminal the key is read immediately without
pressing Enter. Resets from non-interactive input are cancelled.
```

## Optional translation configuration

Translation is optional. Hosted translation settings are loaded from `.env` in the directory where you run the command:

```dotenv
BOOK2EPUB_TRANSLATION_URL=https://your-compatible-endpoint/v1/chat/completions
BOOK2EPUB_TRANSLATION_KEY=your-secret
BOOK2EPUB_TRANSLATION_MODEL=gpt-4o-mini
BOOK2EPUB_CORRECTION_MODEL=turing/gpt-5.3-codex
```

Existing shell environment variables take precedence over `.env` values. Do not commit `.env` files containing secrets. Project directories do not need their own `.env`.

Use the repository template to get started:

```bash
cp .env-template .env
```

## OCR, debug, review, and build

```bash
python -m book2epub ocr /path/to/my-book-project --lang fra
python -m book2epub reset /path/to/my-book-project --stage ocr  # reset OCR only, without running it
python -m book2epub ocr /path/to/my-book-project --lang fra --debug --sample-pages 2 --middle-pages 6 --sample-groups beginning,middle,end
python -m book2epub correct /path/to/my-book-project
python -m book2epub correct /path/to/my-book-project --source-lang fra
python -m book2epub assemble /path/to/my-book-project
python -m book2epub serve /path/to/my-book-project
python -m book2epub translate /path/to/my-book-project  # uses stored target_lang
python -m book2epub reset /path/to/my-book-project --stage translation
python -m book2epub costs /path/to/my-book-project
python -m book2epub build /path/to/my-book-project --out /path/to/my-book-project/book.epub
python -m book2epub reset /path/to/my-book-project --stage build --out /path/to/my-book-project/book.epub
```

OCR requires `pdfinfo`, `pdftoppm`, and `tesseract`. Existing artifacts are reused, so interrupted runs can be restarted. The browser UI shows PDF pages and OCR blocks side by side for editing and approval. Translation is cached by content, languages, provider, model, and prompt version; `costs` reports token totals and debug projections.

Keep `.venv/`, `.env`, SQLite databases, OCR output, rendered pages, caches, and generated EPUBs out of version control unless intentionally archived.

Hosted correction and translation requests are rate-limited. Correction uses
up to three workers by default, while assembly uses up to two; set
`BOOK2EPUB_CORRECTION_WORKERS` and `BOOK2EPUB_ASSEMBLY_WORKERS` in `.env` to
adjust either value (each is bounded to 1–3). The default gap is one second
between request starts. Assembly requests can run in parallel, but overlapping
window results are integrated and deduplicated in deterministic page order.
Startup requests are randomly staggered by up to two seconds per worker by
default; tune `BOOK2EPUB_CORRECTION_START_JITTER` and
`BOOK2EPUB_ASSEMBLY_START_JITTER` (or set them to `0` to disable). ETA uses
only the most recent 50 non-cached completions, so cached work does not distort
the estimate.
request starts; set `BOOK2EPUB_REQUEST_DELAY` in `.env` to tune it (for
example, `2.0` for a two-second gap). OCR, correction, translation, and build
commands print live `current/total` progress and can be safely resumed.

After correction, run `assemble` to process only consecutive page windows,
remove repeated page furniture, and join text split across page boundaries.
The default assembly window is six pages with a one-page overlap; configure it
with `BOOK2EPUB_ASSEMBLY_WINDOW_SIZE` or override it per CLI run with
`--window-size N`. Values around four to six balance context, quality, and
request time.
Assembly keeps corrected blocks as the authoritative source text; only a
deterministic boundary join may add a separating space or remove a line-wrap
hyphen. In `hybrid` join mode, the LLM is used only for ambiguous paragraph
boundaries across page breaks and may return only `join`/`split` decisions; it
cannot rewrite manuscript text. Configure
`BOOK2EPUB_ASSEMBLY_JOIN_MODE`, `BOOK2EPUB_ASSEMBLY_JOIN_MODEL`, and
`BOOK2EPUB_ASSEMBLY_JOIN_CONFIDENCE`. `heuristic`, `hybrid`, and `llm` modes
are supported. Invalid or low-confidence answers use the deterministic
heuristic unless `BOOK2EPUB_ASSEMBLY_JOIN_FALLBACK=fail`. Set
`BOOK2EPUB_ASSEMBLY_JOIN_IMAGES=true` only when page-image evidence is worth
one request per boundary; the default keeps decisions text-only and batched.
Review assembled units at `http://127.0.0.1:8765/assembly`, then approve them
before building the EPUB.

Correction also classifies semantic formatting (headings, quotes, captions,
footnotes, lists, tables, and page furniture). Assembled body paragraphs over
200 words are split at sentence boundaries; override this with
`BOOK2EPUB_ASSEMBLY_MAX_WORDS` in the root `.env`.
OCR renders every page and extracts any embedded PDF text separately. In
`adaptive` OCR mode (the default), Tesseract runs only on pages whose embedded
text is missing or too weak; set `BOOK2EPUB_OCR_MODE=full` to run both sources
on every page. Correction receives the embedded text, layout text, OCR, and
page image as separate evidence and records which source it selected.
Structure first detects and parses a likely table of contents from assembled
text, confirming candidate pages with their images. When at least 80% of entries
and all numbered chapters match body headings in order, the TOC becomes the
authoritative hierarchy and broad refinement is skipped. Otherwise the stage
uses bounded consecutive-heading batches; each request is capped at 40,000
serialized bytes and may request focused unit evidence. A compact global pass
checks chapter order, parent links, and front/main/back boundaries. Completed
batch responses are reused on Resume; invalid proposals cannot replace the
accepted tree. Refinement calls make at most three attempts before reporting a
failure. Ten total rounds are the default; `BOOK2EPUB_STRUCTURE_MAX_ROUNDS` may
be set lower, up to ten.

Each hosted model attempt is also appended to
`PROJECT/logs/model_requests.jsonl`. Entries include the input text, output
(or error), model, URL, trace ID, usage, service usage, cost, and image
metadata. API keys are never written to this log.

Run `python -m book2epub structure PROJECT` after assembly to infer a
chapter/section hierarchy. This compatibility command runs discovery and then
batched refinement. The phases can also be run independently with
`structure-discovery` and `structure-refinement`; inspect saved versions at
`http://127.0.0.1:8765/structure`.

Network and HTTP failures are retried for every hosted LLM operation
(correction, translation, and assembly), up to ten total attempts by
default, with exponential backoff between attempts (capped at two minutes).
Connection resets, timeouts, HTTP failures, and malformed responses are
retryable. Tune `BOOK2EPUB_LLM_MAX_ATTEMPTS`
and `BOOK2EPUB_RETRY_DELAY` in `.env` if needed.
