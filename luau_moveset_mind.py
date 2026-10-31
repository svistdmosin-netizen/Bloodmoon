from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import hub_moveset_converter as core

@dataclass
class Role:
    name: str
    line: int
    role: str
    confidence: float
    evidence: list[str]

ROLE_RULES = {
    "primary_attack": ("attack", "primary", "m1", "slash", "swing", "punch", "shoot"),
    "ability": ("ability", "skill", "move", "special", "cast", "blast", "beam", "rush", "spin"),
    "equip": ("equip", "unequip", "draw", "holster"),
    "weapon_builder": ("sword", "knife", "katana", "gun", "weapon", "blade"),
    "animation": ("animate", "animation", "pose", "lerp", "clerp", "swait"),
    "effect": ("effect", "vfx", "particle", "trail", "beam", "sphere", "wave", "ring", "burst"),
    "damage": ("damage", "hitbox", "kill", "fling", "attack"),
    "input": ("input", "keydown", "button", "bind", "mouse"),
    "cleanup": ("destroy", "cleanup", "remove", "disconnect", "stop"),
    "configuration": ("config", "save", "loadconfig"),
    "ragdoll": ("ragdoll", "stun", "unstun"),
    "music": ("music", "theme", "soundtrack"),
}

def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")

def function_spans(text: str, inv: core.Inventory) -> dict[str, str]:
    lines = text.splitlines()
    starts = sorted((line, name) for name, _, line in inv.functions)
    result = {}
    for index, (line, name) in enumerate(starts):
        end = starts[index + 1][0] - 1 if index + 1 < len(starts) else min(len(lines), line + 300)
        result[name] = "\n".join(lines[line - 1:end])
    return result

def classify_roles(text: str, inv: core.Inventory) -> list[Role]:
    spans = function_spans(text, inv)
    roles = []
    for name, _, line in inv.functions:
        body = spans.get(name, "")
        haystack = (name + "\n" + body[:5000]).lower()
        best_role = "helper"
        best_score = 0.15
        evidence = []
        for role, words in ROLE_RULES.items():
            matches = [word for word in words if word in haystack]
            score = min(0.35 + len(matches) * 0.12, 0.95) if matches else 0
            if role == "damage" and re.search(r"\.Health\s*(?:=|-=)|TakeDamage|ReanimateFling", body):
                score = max(score, 0.9)
                matches.append("health/fling operation")
            if role == "effect" and re.search(r"ParticleEmitter|Trail|Beam|TweenService", body):
                score = max(score, 0.85)
                matches.append("VFX instance")
            if role == "weapon_builder" and re.search(r"MeshId|SpecialMesh|Weld|Motor6D", body):
                score = max(score, 0.7)
                matches.append("mesh/weld construction")
            if score > best_score:
                best_role = role
                best_score = score
                evidence = matches[:8]
        roles.append(Role(name, line, best_role, round(best_score, 2), evidence))
    return roles

def architecture(text: str, inv: core.Inventory) -> tuple[str, float, list[str]]:
    evidence = []
    if core.is_existing_hub_module(text):
        return "hub_module", 1.0, ["existing MOVESET contract"]
    score_reanimate = len(re.findall(r"reanim|netless|permadeath|simulationradius", text, re.I))
    score_tool = len(re.findall(r"\bTool\b|\.Equipped|\.Activated", text))
    score_classic = len(re.findall(r"Right Shoulder|Left Shoulder|RootJoint|ArtificialHB|swait|clerp", text, re.I))
    score_executor = len(inv.executor_apis)
    values = {
        "executor_reanimate": score_reanimate + score_executor,
        "tool_script": score_tool,
        "classic_character_script": score_classic,
        "generic_local_script": 1,
    }
    kind = max(values, key=values.get)
    top = values[kind]
    confidence = min(0.45 + top * 0.04, 0.96)
    if score_reanimate: evidence.append(f"reanimation markers: {score_reanimate}")
    if score_tool: evidence.append(f"tool markers: {score_tool}")
    if score_classic: evidence.append(f"classic animation markers: {score_classic}")
    if score_executor: evidence.append(f"executor APIs: {score_executor}")
    return kind, round(confidence, 2), evidence

def blockers(text: str, inv: core.Inventory) -> list[dict]:
    result = []
    if inv.executor_apis:
        result.append({"type": "executor_api", "severity": "high", "items": inv.executor_apis})
    if inv.loops:
        result.append({"type": "unbounded_loop", "severity": "high", "lines": inv.loops})
    if re.search(r"loadstring\s*\(|HttpGet\s*\(", text):
        result.append({"type": "dynamic_external_code", "severity": "critical", "message": "Behavior depends on code unavailable to static analysis."})
    if inv.damage_lines:
        result.append({"type": "damage_mapping", "severity": "high", "lines": inv.damage_lines})
    if inv.joint_lines:
        result.append({"type": "joint_restore", "severity": "high", "lines": inv.joint_lines})
    if inv.connections:
        result.append({"type": "connection_cleanup", "severity": "medium", "lines": inv.connections})
    return result

def confidence_score(inv: core.Inventory, architecture_confidence: float, issues: list[dict]) -> float:
    penalty = 0
    for issue in issues:
        penalty += {"critical": 0.22, "high": 0.08, "medium": 0.03, "low": 0.01}.get(issue["severity"], 0)
    if not inv.functions:
        penalty += 0.12
    return round(max(0.05, min(0.98, architecture_confidence - penalty)), 2)

def run_converter(source: Path, output: Path, name: str, description: str, mode: str) -> int:
    command = [
        sys.executable, str(Path(__file__).with_name("hub_moveset_converter.py")), str(source),
        "--output", str(output), "--name", name, "--description", description, "--mode", mode,
    ]
    return subprocess.run(command).returncode

def write_overview(path: Path, manifest: dict):
    lines = [
        "# Luau Moveset Mind project", "",
        f"- Source: `{manifest['source']}`",
        f"- Architecture: **{manifest['architecture']['kind']}**",
        f"- Architecture confidence: **{manifest['architecture']['confidence']}**",
        f"- Automatic readiness: **{manifest['readiness']}**",
        f"- Functions: **{len(manifest['roles'])}**",
        f"- Keys: **{', '.join(manifest['keys']) or 'none detected'}**",
        "", "## Generated files", "",
        "- `moveset.lua` — safe canonical module scaffold.",
        "- `moveset.lua.report.md` — static conversion report.",
        "- `legacy_candidate.lua` — maximum-preservation mechanical candidate; unsafe until reviewed.",
        "- `legacy_candidate.lua.report.md` — legacy candidate report.",
        "- `manifest.json` — machine-readable architecture map.",
        "- `source.lua` — normalized source snapshot.",
        "", "## Function roles", "",
    ]
    for role in manifest["roles"]:
        lines.append(f"- `{role['name']}` line {role['line']}: **{role['role']}** ({role['confidence']}) — {', '.join(role['evidence']) or 'no strong evidence'}")
    lines += ["", "## Blockers", ""]
    if manifest["blockers"]:
        for issue in manifest["blockers"]:
            lines.append(f"- **{issue['severity']} / {issue['type']}** — `{json.dumps(issue, ensure_ascii=False)}`")
    else:
        lines.append("- No static blockers detected.")
    lines += [
        "", "## Meaning of readiness", "",
        "Readiness is a static confidence estimate. It is not proof that the moveset works in Roblox.",
        "The safe scaffold initializes cleanly but requires ability bodies to be ported.",
        "The legacy candidate preserves more source code but can hang or fail at runtime.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def main() -> int:
    parser = argparse.ArgumentParser(description="Static Luau-to-moveset project generator without AI or source execution.")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--description", default="Generated moveset migration project")
    args = parser.parse_args()
    if not args.input.exists():
        parser.error(f"input does not exist: {args.input}")

    text = normalize(args.input.read_text(encoding="utf-8", errors="ignore"))
    inv = core.inventory(text)
    roles = classify_roles(text, inv)
    kind, arch_confidence, evidence = architecture(text, inv)
    issues = blockers(text, inv)
    readiness = confidence_score(inv, arch_confidence, issues)
    name = args.name or args.input.stem
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "MovesetProject"
    output = args.output_dir or Path(slug + "_moveset_project")
    output.mkdir(parents=True, exist_ok=True)
    (output / "source.lua").write_text(text, encoding="utf-8")

    safe_status = run_converter(output / "source.lua", output / "moveset.lua", name, args.description, "scaffold")
    legacy_status = run_converter(output / "source.lua", output / "legacy_candidate.lua", name + " Legacy Candidate", args.description, "legacy-wrap")

    manifest = {
        "version": 1,
        "source": str(args.input),
        "source_md5": hashlib.md5(args.input.read_bytes()).hexdigest(),
        "architecture": {"kind": kind, "confidence": arch_confidence, "evidence": evidence},
        "readiness": readiness,
        "keys": inv.keys,
        "assets": {"sounds": inv.sounds, "meshes": inv.meshes, "textures": inv.textures},
        "roles": [asdict(role) for role in roles],
        "blockers": issues,
        "outputs": {"safe_status": safe_status, "legacy_status": legacy_status},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_overview(output / "README.md", manifest)

    print(f"Project: {output}")
    print(f"Architecture: {kind} ({arch_confidence})")
    print(f"Readiness: {readiness}")
    print(f"Functions: {len(roles)} | Keys: {len(inv.keys)} | Blockers: {len(issues)}")
    print(f"Safe converter status: {safe_status} | Legacy converter status: {legacy_status}")
    return 0 if safe_status == 0 else 2

if __name__ == "__main__":
    raise SystemExit(main())
