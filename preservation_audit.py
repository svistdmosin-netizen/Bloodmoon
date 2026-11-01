"""Conservative, AST-backed evidence of exact source retention; never proof of behavior."""
from __future__ import annotations
import hashlib
import re
from collections import Counter
from typing import Any


def _walk(value: Any):
    stack = [value]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if 'type' in cur:
                yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _fragment(source: str, location: str) -> str:
    match = re.fullmatch(r'(\d+),(\d+) - (\d+),(\d+)', location or '')
    if not match:
        return ''
    a, b, c, d = map(int, match.groups())
    lines = source.encode('utf-8').splitlines(keepends=True)
    if a >= len(lines) or c >= len(lines) or a > c:
        return ''
    if a == c:
        raw = lines[a][b:d]
    else:
        raw = lines[a][b:] + b''.join(lines[a+1:c]) + lines[c][:d]
    return raw.decode('utf-8', errors='replace')


def analyze(source: str, ast: dict[str, Any], output: str | None = None) -> dict[str, Any]:
    nodes = list(_walk(ast))
    counts = Counter(n.get('type', '') for n in nodes)
    functions = []
    external = []
    assets = set()
    for n in nodes:
        typ = n.get('type')
        if typ in ('AstStatLocalFunction', 'AstStatFunction'):
            name_data = n.get('name') or {}
            name = (name_data.get('name') or name_data.get('global') or name_data.get('index') or '<anonymous>') if isinstance(name_data, dict) else str(name_data)
            location = n.get('location', '')
            frag = _fragment(source, location)
            if frag:
                functions.append({'name': name, 'location': location, 'sha256': hashlib.sha256(frag.encode('utf-8')).hexdigest(), 'exact_source_found_in_output': (frag in output) if output is not None else None})
        if typ == 'AstExprCall':
            fn = n.get('func') or {}
            name = fn.get('global') or fn.get('index') or '' if isinstance(fn, dict) else ''
            if name.lower() in ('loadstring', 'getobjects', 'httpget', 'request', 'require', 'readfile', 'writefile'):
                external.append({'call': name, 'location': n.get('location', '')})
        if typ == 'AstExprConstantString':
            value = n.get('value')
            if isinstance(value, str):
                assets.update(re.findall(r'(?:rbxassetid://|[?&]id=)(\d{5,})', value))
    functions.sort(key=lambda x: tuple(int(v) for v in re.findall(r'\d+', x['location'])[:2]))
    matched = sum(x['exact_source_found_in_output'] is True for x in functions)
    same = output is not None and source == output
    return {
        'method': 'Luau AST named-function source fragments, exact text matching only',
        'source_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
        'source_equals_output': same if output is not None else None,
        'named_functions_total': len(functions),
        'named_functions_exact_text_found': matched if output is not None else None,
        'functions': functions[:500], 'functions_truncated': len(functions) > 500,
        'ast_counts': {'functions': counts['AstExprFunction'], 'loops': sum(counts[t] for t in ('AstStatWhile','AstStatRepeat','AstStatFor','AstStatForIn')), 'calls': counts['AstExprCall']},
        'external_calls': external[:200], 'external_calls_truncated': len(external) > 200,
        'asset_ids_literal': sorted(assets)[:200],
        'limitations': ['Exact text matching does not prove behavior; modified equivalent functions are counted as not exact.', 'Anonymous callbacks, dynamically loaded code, computed assets and runtime behavior are not fully tracked.', 'This is evidence for manual review, not semantic coverage or a runtime safety guarantee.'],
    }
