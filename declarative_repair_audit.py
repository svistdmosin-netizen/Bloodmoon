from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.kilo.ai/api/gateway/chat/completions"
MODELS = ("reflection/beam-free", "nvidia/nemotron-3.5-super-vl:free", "kilo-auto/free")
ALLOWED = {
    "track_connections": "Resource ownership for event connections is unknown",
    "cancel_async_loops": "Async loops are not proven cancellable",
    "classify_destructive_operation": "Destructive operations require semantic classification",
    "replace_external_resource_with_asset_config": "EXECUTABLE_HTTP_GET",
    "prove_motor6d_cframe_origins": "UNPROVEN_JOINT_PROXY_TYPES",
    "complete_r6_joint_coverage": "INCOMPLETE_R6_JOINT_COVERAGE",
    "emit_mobile_context_actions": "MOBILE_ABILITY_UI_REQUIRED",
}


def _line_from_blocker(text: str) -> int | None:
    match = re.search(r"\bat\s+(\d+),\d+", text)
    return int(match.group(1)) + 1 if match else None


def _evidence(source: str, blockers: list[str]) -> list[dict[str, Any]]:
    lines = source.splitlines()
    result = []
    for blocker in blockers:
        line = _line_from_blocker(blocker)
        if line and 1 <= line <= len(lines):
            lo, hi = max(1, line - 2), min(len(lines), line + 2)
            excerpt = "\n".join(f"{n}:{lines[n-1][:500]}" for n in range(lo, hi + 1))
        else:
            excerpt = ""
        result.append({"blocker": blocker, "line": line, "excerpt": excerpt})
    return result


def _prompt(source: str, report: dict[str, Any]) -> str:
    blockers = [str(item) for item in report.get("blockers") or []]
    schema = {
        "operations": [{
            "type": "one allowed operation",
            "blocker": "exact blocker from input",
            "evidence_lines": [1],
            "reason": "short factual explanation",
            "confidence": 0.0,
        }],
        "remaining_blockers": ["exact blocker"],
    }
    return (
        "Audit a deterministic Luau conversion. Return ONLY JSON, never Luau or Python. "
        "Do not claim a blocker is fixed. Propose only declarative operations supported by the allowlist. "
        "Every operation must quote one exact input blocker and evidence line present in the supplied excerpt. "
        f"Schema: {json.dumps(schema, separators=(',', ':'))}\n"
        f"Allowlist: {json.dumps(sorted(ALLOWED), separators=(',', ':'))}\n"
        f"Evidence: {json.dumps(_evidence(source, blockers), ensure_ascii=False, separators=(',', ':'))}\n"
        f"Semantic family: {json.dumps((report.get('semantic_ir') or {}).get('family', 'unknown'))}\n"
    )


def _parse(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI response contains no JSON object")
    return json.loads(text[start:end + 1])


def _call(prompt: str, model: str, timeout: int) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 2500,
        "include_reasoning": False,
        "reasoning_effort": "low",
    }
    request = urllib.request.Request(
        API,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Bloodmoon-Repair-Audit/1.0"},
        method="POST",
    )
    try:
        payload = json.loads(urllib.request.urlopen(request, timeout=timeout).read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[-400:]}") from exc
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("model returned no choice")
    return _parse(((choices[0].get("message") or {}).get("content") or ""))


def _validate(raw: dict[str, Any], source: str, report: dict[str, Any]) -> dict[str, Any]:
    blockers = [str(item) for item in report.get("blockers") or []]
    line_count = len(source.splitlines())
    operations = []
    for item in raw.get("operations") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        blocker = str(item.get("blocker") or "")
        if kind not in ALLOWED or blocker not in blockers:
            continue
        expected = ALLOWED[kind]
        if expected not in blocker:
            continue
        evidence_lines = sorted({int(x) for x in item.get("evidence_lines") or [] if str(x).isdigit() and 1 <= int(x) <= line_count})
        blocker_line = _line_from_blocker(blocker)
        if blocker_line and blocker_line not in evidence_lines:
            continue
        operations.append({
            "type": kind,
            "blocker": blocker,
            "evidence_lines": evidence_lines,
            "reason": str(item.get("reason") or "")[:300],
            "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
            "validated": True,
            "applied": False,
        })
    return {
        "schema_version": "1.0",
        "mode": "declarative_audit_only",
        "validated": True,
        "operations": operations,
        "applied_operation_count": 0,
        "remaining_blockers": blockers,
        "note": "AI suggestions cannot remove blockers until a deterministic allowlisted rewriter applies and revalidates them.",
    }


def audit(source_path: Path, report: dict[str, Any], timeout: int = 35) -> dict[str, Any]:
    source = source_path.read_text(encoding="utf-8-sig", errors="replace")
    prompt = _prompt(source, report)
    errors = []
    for model in MODELS:
        try:
            result = _validate(_call(prompt, model, timeout), source, report)
            result["model"] = model
            return result
        except Exception as exc:
            errors.append(f"{model}: {exc}")
    return {
        "schema_version": "1.0",
        "mode": "declarative_audit_only",
        "validated": False,
        "operations": [],
        "applied_operation_count": 0,
        "remaining_blockers": [str(item) for item in report.get("blockers") or []],
        "errors": errors,
        "note": "Free repair audit was unavailable; deterministic conversion result is unchanged.",
    }
