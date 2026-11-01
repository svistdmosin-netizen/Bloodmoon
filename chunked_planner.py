"""Loss-aware source splitting and plan evidence checks. Never establishes equivalence."""
from __future__ import annotations
import re

MAX_CHUNK = 28000
MAX_CHUNKS = 24

def split_source(source: str, limit: int = MAX_CHUNK, ast=None):
    """Preserve every byte-equivalent character; prefer AST function boundaries.

    Oversize functions are split and explicitly reported. This does not resolve
    scopes, loops, state branches, or cross-function dependencies.
    """
    if limit < 100: raise ValueError('chunk size too small')
    lines = source.splitlines(keepends=True)
    for i, line in enumerate(lines, 1):
        if len(line) > limit: raise ValueError(f'Line {i} exceeds the supported chunk size; manual review is required')
    delta = [0] * (len(lines) + 2)
    stack = [ast] if ast else []
    while stack:
        node = stack.pop()
        if isinstance(node, list): stack.extend(node); continue
        if not isinstance(node, dict): continue
        if node.get('type') == 'AstExprFunction':
            loc = re.fullmatch(r'(\d+),(\d+) - (\d+),(\d+)', node.get('location', ''))
            if loc:
                first, _, last, _ = map(int,loc.groups())
                if 0 <= first < last < len(lines):
                    delta[first+1] += 1; delta[last+1] -= 1
        stack.extend(node.values())
    protected=[]; active=0
    for i in range(len(lines)+1):
        active += delta[i]; protected.append(active > 0)
    chunks=[]; start=0
    while start < len(lines):
        end=start; size=0; safe=None
        while end < len(lines) and size + len(lines[end]) <= limit:
            size += len(lines[end]);end += 1
            if not protected[end]: safe=end
        if end == len(lines): chosen=end
        elif safe is not None and safe > start: chosen=safe
        else: chosen=end  # Oversize function: forced split, explicitly reported.
        chunks.append({'start_line':start+1,'end_line':chosen,'source':''.join(lines[start:chosen]), 'starts_inside_function':protected[start], 'ends_inside_function':protected[chosen]})
        start=chosen
    if len(chunks) > MAX_CHUNKS: raise ValueError('Source requires too many AI requests; split manually or use static analysis')
    assert ''.join(x['source'] for x in chunks) == source
    return chunks

def has_evidence(item, source):
    quote = item.get('original_quote') if isinstance(item, dict) else None
    return isinstance(quote, str) and len(quote.strip()) >= 20 and quote in source

def looks_like_zero_pose(body):
    s = re.sub(r'\s+', '', body or '').lower()
    return bool(s) and bool(re.fullmatch(r'pose\(["\']root["\'],cframe\.new\(0,0,0\),cframe\.new\(0,0,0\)\);?', s))

def merge_plans(pieces):
    merged = {'states': {}, 'abilities': [], 'warnings': [], 'assets': {}}
    seen = set()
    for chunk, plan in pieces:
        tag = f"lines {chunk['start_line']}-{chunk['end_line']}"
        for name, item in plan.get('states', {}).items():
            if not has_evidence(item, chunk['source']):
                merged['warnings'].append(f'{tag}: omitted state {name}: original_quote missing or not exact')
                continue
            if looks_like_zero_pose(item.get('body', '')):
                merged['warnings'].append(f'{tag}: omitted state {name}: zero-pose placeholder')
                continue
            if name in merged['states']:
                merged['warnings'].append(f'{tag}: conflicting state {name} omitted; manual merge required')
                continue
            merged['states'][name] = item
        for item in plan.get('abilities', []):
            if not isinstance(item, dict): continue
            name = str(item.get('name') or '')
            if not has_evidence(item, chunk['source']):
                merged['warnings'].append(f'{tag}: omitted ability {name}: original_quote missing or not exact')
                continue
            if name in seen:
                merged['warnings'].append(f'{tag}: duplicate ability {name} omitted; manual merge required')
                continue
            seen.add(name); merged['abilities'].append(item)
        for warning in plan.get('warnings', []):
            if isinstance(warning, str): merged['warnings'].append(f'{tag}: {warning[:300]}')
    return merged

# Best-effort supplemental context. Only uniquely named global functions and
# top-level local functions are indexed; no claim of scope/dataflow resolution.
def build_function_index(source, ast):
    from collections import defaultdict
    lines = source.encode('utf-8').splitlines(keepends=True)
    found = defaultdict(list)
    stack = [(ast, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, list):
            stack.extend((x, depth) for x in reversed(node)); continue
        if not isinstance(node, dict): continue
        kind = node.get('type')
        if kind in ('AstStatFunction', 'AstStatLocalFunction'):
            n = node.get('name') or {}
            name = n.get('global') if n.get('type') == 'AstExprGlobal' else (n.get('name') if kind == 'AstStatLocalFunction' and depth == 0 else None)
            loc = re.fullmatch(r'(\d+),(\d+) - (\d+),(\d+)', node.get('location', ''))
            if name and loc:
                a,b,c,d=map(int,loc.groups())
                if a <= c < len(lines):
                    raw = lines[a][b:d] if a == c else lines[a][b:] + b''.join(lines[a+1:c]) + lines[c][:d]
                    try: text=raw.decode('utf-8','strict')
                    except UnicodeDecodeError: text=''
                    if text: found[name].append({'name':name,'source':text,'start_line':a+1,'end_line':c+1})
        next_depth=depth + (kind == 'AstExprFunction')
        stack.extend((v,next_depth) for v in reversed(list(node.values())))
    return {name: values[0] for name, values in found.items() if len(values) == 1}

def dependency_context(chunk, index, limit=18000, max_functions=6):
    """Rank direct function-call names only; bounded, incomplete and untrusted."""
    target=chunk['source']; selected=[]; omitted=[]; used=0
    candidates=[]
    for name, item in index.items():
        # Only attach the full definition if a call is visible and it is not
        # already fully contained in this fragment.
        if item['source'] not in target and re.search(r'\b'+re.escape(name)+r'\s*\(',target):
            candidates.append(item)
    candidates.sort(key=lambda x:(len(x['source']),x['start_line'],x['name']))
    for item in candidates:
        size=len(item['source'])
        if len(selected)>=max_functions or used+size>limit:
            omitted.append(item['name']); continue
        selected.append(item);used+=size
    return {'functions':selected,'omitted':omitted,'characters':used,'resolution':'heuristic direct calls only; not scope- or dataflow-resolved'}
