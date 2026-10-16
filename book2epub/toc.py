"""Deterministic table-of-contents detection and reconciliation."""
import re
import unicodedata


_TOC_MARKERS = re.compile(r'\b(?:table\s+of\s+contents|contents|sommaire|table\s+des\s+mati[eè]res)\b', re.I)
_ENTRY = re.compile(r'^\s*(?:(?:chapter|chapitre)\s*)?(?P<number>\d{1,3})\s*[:.)-]\s*(?P<title>.+?)\s+(?P<page>\d{1,4})\s*$', re.I)
_PLAIN_ENTRY = re.compile(r'^\s*(?P<title>[A-Za-z][^\d]{2,100}?)\s+(?P<page>\d{1,4})\s*$')


def _norm(value):
    value = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode()
    value = re.sub(r'\b(?:chapter|chapitre)\s*\d+\b', ' ', value, flags=re.I)
    value = re.sub(r'^\s*\d{1,3}\s*[:.)-]\s*', ' ', value)
    value = re.sub(r'[^a-z0-9]+', ' ', value.casefold())
    return re.sub(r'\s+', ' ', value).strip()


def _entry_type(title):
    key = _norm(title)
    if key.startswith('appendix') or key.startswith('annex'): return 'appendix'
    for name in ('notes', 'references', 'acknowledgments', 'acknowledgements', 'index'):
        if key == name or key.startswith(name + ' '): return name
    if key == 'introduction': return 'introduction'
    return 'chapter' if key else 'unknown'


def detect_toc_candidates(units):
    """Score assembled units that look like a contents page or spread."""
    ordered = sorted(units, key=lambda x: int(x.get('ordinal', 0)))
    candidates = []
    for index, unit in enumerate(ordered):
        text = str(unit.get('text') or '')
        if _TOC_MARKERS.search(text):
            text += '\n' + '\n'.join(str(x.get('text') or '') for x in ordered[index + 1:index + 4])
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        joined = '\n'.join(lines)
        marker = bool(_TOC_MARKERS.search(joined))
        entry_count = sum(bool(_ENTRY.match(line) or _PLAIN_ENTRY.match(line)) for line in lines)
        leader_count = sum(bool(re.search(r'\.{2,}\s*\d+\s*$', line)) for line in lines)
        score = (0.65 if marker else 0) + min(0.4, entry_count * 0.08) + min(0.15, leader_count * 0.05)
        if score < 0.55: continue
        end = index
        while end + 1 < len(ordered) and end - index < 2:
            nxt = str(ordered[end + 1].get('text') or '')
            if sum(bool(_ENTRY.match(line.strip()) or _PLAIN_ENTRY.match(line.strip())) for line in nxt.splitlines()) == 0: break
            end += 1
        candidates.append({'start_unit': int(unit['ordinal']), 'end_unit': int(ordered[end]['ordinal']),
                           'score': min(1.0, score), 'entry_count': entry_count,
                           'units': ordered[index:end + 1]})
    return sorted(candidates, key=lambda x: (-x['score'], x['start_unit']))


def parse_toc_entries(text):
    entries = []
    for line in str(text or '').splitlines():
        line = re.sub(r'\.{2,}', ' ', line).strip()
        if not line or _TOC_MARKERS.fullmatch(line): continue
        match = _ENTRY.match(line) or _PLAIN_ENTRY.match(line)
        if not match: continue
        title = re.sub(r'\s+', ' ', match.group('title')).strip(' .')
        number = int(match.group('number')) if match.groupdict().get('number') else None
        entries.append({'title': title, 'number': number,
                        'printed_page': int(match.group('page')), 'entry_type': _entry_type(title),
                        'source_text': line})
    return entries


def match_toc_to_body(entries, body_units):
    headings = [u for u in sorted(body_units, key=lambda x: int(x.get('ordinal', 0)))
                if u.get('kind') in ('heading', 'subheading')]
    matches = []; cursor = 0; unmatched = []
    substantive = [e for e in entries if e['entry_type'] not in ('unknown',)]
    for index, entry in enumerate(entries):
        target = _norm(entry['title'])
        found = None
        for pos in range(cursor, len(headings)):
            candidate = _norm(headings[pos].get('text'))
            if candidate == target or (len(target) >= 8 and (target in candidate or candidate in target)):
                found = (pos, headings[pos]); break
        if found:
            pos, unit = found; cursor = pos + 1
            matches.append({'toc_index': index, 'body_unit': int(unit['ordinal']),
                            'title': entry['title'], 'printed_page': entry['printed_page']})
        else:
            if entry in substantive: unmatched.append({'toc_index': index, 'title': entry['title']})
    numbered = [i for i, e in enumerate(entries) if e['number'] is not None]
    matched_numbered = sum(1 for m in matches if entries[m['toc_index']]['number'] is not None)
    issues = [] if not unmatched else ['unmatched_toc_entries']
    pages = [m['printed_page'] for m in matches if m.get('printed_page') is not None]
    if any(b < a for a, b in zip(pages, pages[1:])): issues.append('non_monotonic_printed_pages')
    if len(pages) != len(set(pages)): issues.append('duplicate_printed_pages')
    return {'entries': entries, 'matches': matches, 'unmatched': unmatched,
            'matched_substantive': len(matches), 'substantive_entries': len(substantive),
            'numbered_entries': len(numbered), 'matched_numbered': matched_numbered,
            'issues': issues}


def toc_quality_gate(report):
    substantive = max(1, report.get('substantive_entries', 0))
    coverage = report.get('matched_substantive', 0) / substantive
    numbered = report.get('numbered_entries', 0)
    numbered_coverage = report.get('matched_numbered', 0) / max(1, numbered)
    authoritative = coverage >= 0.8 and numbered_coverage == 1.0 and not report.get('issues')
    confidence = min(1.0, 0.95 + 0.05 * coverage) if authoritative else min(0.94, 0.5 * coverage + 0.4 * numbered_coverage)
    return {'authoritative': authoritative, 'confidence': confidence,
            'coverage': coverage, 'numbered_coverage': numbered_coverage,
            'issues': list(report.get('issues') or [])}


def image_confirmation_prompt(toc_text):
    return ('Confirm whether the attached page image is part of a printed table of contents. '
            'Return JSON {"is_toc":boolean,"entries":[{"title":string,"number":number|null,'
            '"printed_page":number|null,"entry_type":string}],"issues":[]}. '
            'Use the image to correct OCR line breaks and punctuation. Do not invent entries. '
            'OCR candidate text:\n' + str(toc_text or ''))
