"""Loss-aware source splitting and plan evidence checks. Never establishes equivalence."""
from __future__ import annotations
import re

MAX_CHUNK = 28000
MAX_CHUNKS = 24

def split_source(source: str, limit: int = MAX_CHUNK):
    if limit < 100: raise ValueError('chunk size too small')
    lines = source.splitlines(keepends=True)
    chunks, current, size, first_line = [], [], 0, 1
    for lineno, line in enumerate(lines, 1):
        # Never silently split a line or omit source.
        if len(line) > limit:
            raise ValueError(f'Line {lineno} exceeds the supported chunk size; manual review is required')
        if current and size + len(line) > limit:
            chunks.append({'start_line': first_line, 'end_line': lineno - 1, 'source': ''.join(current)})
            current, size, first_line = [], 0, lineno
        current.append(line); size += len(line)
    if current: chunks.append({'start_line': first_line, 'end_line': len(lines), 'source': ''.join(current)})
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
