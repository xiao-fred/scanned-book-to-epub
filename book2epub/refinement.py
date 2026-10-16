"""Bounded evidence and request construction for structure refinement."""
import json

REQUEST_LIMIT = 40000
SYSTEM_PROMPT = (
    'Refine only the supplied heading anchors. Return JSON with action="propose" and '
    'nodes containing one entry per retained heading: title, node_type '
    '(part|chapter|section|subsection), number, number_source, start_unit, '
    'heading_level, parent_start_unit, confidence, and evidence. You may instead '
    'return action="inspect" and inspect_units (at most 12) when evidence is '
    'insufficient. Keep source ordinals absolute. Do not invent headings or book '
    'prose. Preserve justified existing decisions; list unresolved ambiguities. '
    'Account for every supplied heading with either a node or an excluded_units '
    'entry containing its ordinal and a reason.'
)
GLOBAL_PROMPT = (
    'Check the proposed hierarchy across the whole book. Return JSON with '
    'content_start_unit, backmatter_start_unit, updates (each identified by '
    'start_unit and containing only fields to change), remove_units, unresolved, '
    'and done. Resolve chapter order, numbering, duplicate anchors, and parent '
    'links. Keep decisions grounded in supplied headings. Do not invent titles.'
)


def request_bytes(system, user):
    """Conservatively measure the UTF-8 JSON sent to a Responses endpoint."""
    payload = {'model': 'placeholder', 'input': [
        {'role': 'system', 'content': [{'type': 'input_text', 'text': system}]},
        {'role': 'user', 'content': [{'type': 'input_text', 'text': user}]},
    ]}
    return len(json.dumps(payload).encode('utf-8'))


def make_request(batch, inspected=None):
    data = {'headings': batch['headings'], 'existing_nodes': batch['existing_nodes'],
            'context': batch['context'], 'inspected': inspected or []}
    return SYSTEM_PROMPT, json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def make_global_request(nodes, issues, excluded_headings=None, last_unit=None):
    """Use all compact nodes when possible, otherwise chapter summaries."""
    brief = [{key: n.get(key) for key in ('unit', 'title', 'node_type', 'number',
              'parent_start_unit')} for n in nodes]
    chapters = [n for n in brief if n['node_type'] in ('part', 'chapter')]
    data = {'node_count': len(nodes), 'chapters': chapters, 'nodes': brief,
            'excluded_headings': excluded_headings or [], 'last_unit': last_unit,
            'issues': issues}
    user = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    if request_bytes(GLOBAL_PROMPT, user) > REQUEST_LIMIT:
        data['nodes'] = [{'unit': n['unit'], 'node_type': n['node_type'],
                          'parent_start_unit': n['parent_start_unit']}
                         for n in brief]
        user = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    if request_bytes(GLOBAL_PROMPT, user) > REQUEST_LIMIT:
        data.pop('nodes')
        user = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    if request_bytes(GLOBAL_PROMPT, user) > REQUEST_LIMIT:
        raise ValueError('global structure summary exceeds refinement request limit')
    return GLOBAL_PROMPT, user


def _brief_node(node):
    return {key: node.get(key) for key in ('start_unit', 'title', 'node_type', 'number',
            'number_source', 'heading_level', 'parent_start_unit', 'confidence')}


def make_batches(units, tree, max_headings=12):
    """Partition source headings into source-order requests below REQUEST_LIMIT."""
    ordered = sorted((dict(u) for u in units), key=lambda u: int(u['ordinal']))
    by_ordinal = {int(u['ordinal']): u for u in ordered}
    headings = [u for u in ordered if u['kind'] in ('heading', 'subheading')]
    existing = {int(n['start_unit']): _brief_node(n) for n in tree}

    def make(group):
        anchors = [int(u['ordinal']) for u in group]
        context = []
        for anchor in anchors:
            for neighbor in (anchor - 1, anchor + 1):
                u = by_ordinal.get(neighbor)
                if u and u['kind'] not in ('heading', 'subheading'):
                    context.append({'ordinal': neighbor, 'text': str(u['text'] or '')[:180]})
        return {'headings': [{'ordinal': int(u['ordinal']), 'kind': u['kind'],
                              'text': u['text'], 'source_pages': u.get('source_pages',[]),
                              'running_header': bool(u.get('running_header'))} for u in group],
                'existing_nodes': [existing[a] for a in anchors if a in existing],
                'context': context}

    batches = []
    group = []
    for heading in headings:
        anchor = int(heading['ordinal'])
        if group and len(group) >= max(2, max_headings // 2) and existing.get(anchor, {}).get('node_type') == 'chapter':
            batches.append(make(group))
            group = []
        candidate = group + [heading]
        batch = make(candidate)
        system, user = make_request(batch)
        if len(candidate) > max_headings or request_bytes(system, user) > REQUEST_LIMIT - 512:
            if not group:
                raise ValueError(f'heading {heading["ordinal"]} exceeds refinement request limit')
            batches.append(make(group))
            group = [heading]
            batch = make(group)
            system, user = make_request(batch)
            if request_bytes(system, user) > REQUEST_LIMIT - 512:
                raise ValueError(f'heading {heading["ordinal"]} exceeds refinement request limit')
        else:
            group = candidate
    if group:
        batches.append(make(group))
    return batches
