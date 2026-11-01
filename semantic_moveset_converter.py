#!/usr/bin/env python3
"""Fail-closed, AST-backed legacy Luau -> Reanimation Hub converter.

This is the safe replacement entry point for the old regex converter.  It never
uses compile success as proof of runtime readiness.  Existing hub modules are
passed through; legacy sources are classified and represented as semantic IR.
The first adapter family delegates code emission to the existing converter only
when explicitly enabled, and always returns RUNTIME_TEST_REQUIRED.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
DEFAULT_TOOLS = ROOT / "tools" / "luau"


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class SemanticIR:
    family: str = "unknown"
    existing_hub_module: bool = False
    character_bindings: list[str] = field(default_factory=list)
    joint_operations: dict[str, int] = field(default_factory=dict)
    animation_drivers: dict[str, int] = field(default_factory=dict)
    abilities: list[str] = field(default_factory=list)
    pc_inputs: dict[str, int] = field(default_factory=dict)
    mobile_inputs: dict[str, int] = field(default_factory=dict)
    gui: dict[str, int] = field(default_factory=dict)
    resources: dict[str, int] = field(default_factory=dict)
    destructive_operations: dict[str, int] = field(default_factory=dict)
    external_dependencies: list[str] = field(default_factory=list)
    hub_contract: dict[str, bool] = field(default_factory=dict)


@dataclass
class Report:
    schema_version: str
    input_file: str
    output_file: str | None
    input_sha256: str
    status: str
    status_reason: str
    frontend: dict[str, Any]
    semantic_ir: SemanticIR
    findings: list[Finding]
    blockers: list[str]
    validations: dict[str, Any]
    runtime_acceptance: dict[str, str]
    emitted: bool = False
    output_sha256: str | None = None


def occurrences(source: str, pattern: str, flags: int = 0) -> int:
    return len(re.findall(pattern, source, flags))


def lines_for(source: str, pattern: str, limit: int = 8) -> list[str]:
    rx = re.compile(pattern, re.I)
    return [f"{i}: {line.strip()[:180]}" for i, line in enumerate(source.splitlines(), 1)
            if rx.search(line)][:limit]


def walk(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        if "type" in node:
            yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def run_tool(command: list[str], timeout: int = 45) -> dict[str, Any]:
    try:
        # The compiler's textual disassembly may contain arbitrary string bytes.
        # Preserve strict UTF-8 for AST JSON; replacement is appropriate only
        # for human-readable compiler/analyzer diagnostics.
        decode_errors = "strict" if Path(command[0]).name == "luau-ast" else "replace"
        proc = subprocess.run(command, text=True, encoding="utf-8", errors=decode_errors, capture_output=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "stdout": proc.stdout, "stderr": proc.stderr}
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": str(exc)}


def parse_ast(path: Path, tool_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    exe = tool_dir / "luau-ast"
    result = run_tool([str(exe), str(path)])
    meta = {"tool": str(exe), "ok": result["ok"], "exit_code": result["exit_code"]}
    if not result["ok"]:
        meta["error"] = result["stderr"][-2000:]
        return None, meta
    try:
        ast = json.loads(result["stdout"])
        nodes = list(walk(ast))
        meta.update({"node_count": len(nodes), "node_types": sorted({n["type"] for n in nodes})})
        return ast, meta
    except json.JSONDecodeError as exc:
        meta.update({"ok": False, "error": f"Invalid AST JSON: {exc}"})
        return None, meta


def _location_offsets(source: str, location: str) -> tuple[int, int]:
    """Translate luau-ast's zero-based line/column range to Python offsets."""
    match = re.fullmatch(r"(\d+),(\d+) - (\d+),(\d+)", location)
    if not match:
        raise ValueError(f"Unsupported AST location: {location}")
    sl, sc, el, ec = map(int, match.groups())
    lines = source.splitlines(keepends=True)
    starts, total = [], 0
    for line in lines:
        starts.append(total)
        total += len(line)
    if not lines:
        return 0, 0
    def character_offset(line: str, byte_column: int) -> int:
        encoded = line.encode("utf-8")
        if byte_column > len(encoded):
            raise ValueError(f"AST byte column {byte_column} outside source line")
        # Fail closed rather than cut through a UTF-8 code point.
        return len(encoded[:byte_column].decode("utf-8", errors="strict"))
    if sl >= len(lines) or el >= len(lines):
        raise ValueError(f"AST location outside source: {location}")
    return starts[sl] + character_offset(lines[sl], sc), starts[el] + character_offset(lines[el], ec)


def semantic_rewrite_r6(source: str, ast: dict[str, Any], provenance: Any) -> tuple[str, dict[str, Any]]:
    """Apply narrow AST-addressed rewrites for the FELOADLIBRARY/R6 family.

    Only complete top-level AST statements are replaced. Unknown constructs are
    preserved; this pass never searches/replaces arbitrary text inside strings
    or nested scopes.
    """
    edits: list[tuple[int, int, str, str]] = []
    body = ast.get("root", {}).get("body", [])

    # Infer character and R6 part roles from binding provenance. Workspace
    # lookup candidates require downstream rig evidence before rewriting.
    candidate_origins: dict[str, str] = {}
    local_alias_edges: list[tuple[str, str]] = []
    local_declarations: list[tuple[dict[str, Any], dict[str, Any]]] = []
    character_assignment_ids: set[int] = set()

    def is_workspace_lookup(value: dict[str, Any]) -> bool:
        # Accept the assigned expression itself, or a transparent `or` chain of
        # direct workspace lookups. Never infer provenance from call arguments.
        if value.get("type") == "AstExprCall":
            func = value.get("func", {})
            return (_constant_member_name(func) == "FindFirstChild"
                    and func.get("expr", {}).get("type") == "AstExprGlobal"
                    and func.get("expr", {}).get("global") in {"workspace", "Workspace"})
        if value.get("type") == "AstExprBinary" and value.get("op") == "Or":
            return is_workspace_lookup(value.get("left", {})) and is_workspace_lookup(value.get("right", {}))
        return False

    players_service_ids: set[str] = set()
    for root_statement in body:
        if root_statement.get("type") != "AstStatLocal":
            continue
        for service_var, service_value in zip(root_statement.get("vars", []), root_statement.get("values", [])):
            if service_value.get("type") != "AstExprCall":
                continue
            service_func = service_value.get("func", {})
            service_args = service_value.get("args", [])
            if (_constant_member_name(service_func) == "GetService"
                    and service_func.get("expr", {}).get("type") == "AstExprGlobal"
                    and service_func.get("expr", {}).get("global") == "game"
                    and service_args and service_args[0].get("type") == "AstExprConstantString"
                    and service_args[0].get("value") == "Players"):
                players_service_ids.add(_decl_id(service_var))

    # Service provenance is immutable-only for now. Any later assignment in
    # any branch/function invalidates the binding conservatively.
    reassigned_local_ids: set[str] = set()
    for candidate in walk(ast.get("root", {})):
        if candidate.get("type") != "AstStatAssign":
            continue
        for assigned in candidate.get("vars", []):
            assigned_id = _expr_local_id(assigned)
            if assigned_id:
                reassigned_local_ids.add(assigned_id)
    players_service_ids.difference_update(reassigned_local_ids)

    def is_localplayer_character(value: dict[str, Any]) -> bool:
        # Require the exact Roblox chain rooted at global game. Matching only
        # the tail `LocalPlayer.Character` is insufficient (spoofable objects).
        current = value
        for member in ["Character", "LocalPlayer"]:
            if current.get("type") not in {"AstExprIndexName", "AstExprIndexExpr"} or _constant_member_name(current) != member:
                return False
            current = current.get("expr", {})
        service_id = _expr_local_id(current)
        if service_id in players_service_ids:
            return True
        if current.get("type") not in {"AstExprIndexName", "AstExprIndexExpr"} or _constant_member_name(current) != "Players":
            return False
        current = current.get("expr", {})
        return current.get("type") == "AstExprGlobal" and current.get("global") == "game"

    # Process direct root declarations and assignments. Assignments are definite
    # at root level; branch-local assignments are intentionally excluded.
    for statement in body:
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if statement.get("type") == "AstStatLocal":
            pairs = list(zip(statement.get("vars", []), statement.get("values", [])))
            local_declarations.extend(pairs)
        elif statement.get("type") == "AstStatAssign":
            pairs = list(zip(statement.get("vars", []), statement.get("values", [])))
        for var, value in pairs:
            var_id = (_decl_id(var) if var.get("type") == "AstLocal" else _expr_local_id(var))
            if not var_id:
                continue
            source_id = _expr_local_id(value)
            if source_id:
                local_alias_edges.append((var_id, source_id))
            if is_workspace_lookup(value):
                candidate_origins[var_id] = "workspace_lookup"
                if statement.get("type") == "AstStatAssign": character_assignment_ids.add(id(statement))
            elif is_localplayer_character(value):
                candidate_origins[var_id] = "localplayer_character"
                if statement.get("type") == "AstStatAssign": character_assignment_ids.add(id(statement))
            elif value.get("type") == "AstExprLocal" and value.get("local", {}).get("name") == "__HubCharacter":
                candidate_origins[var_id] = "hub_character"
                if statement.get("type") == "AstStatAssign": character_assignment_ids.add(id(statement))

    # Propagate aliases to candidates before collecting downstream evidence.
    candidate_ids = set(candidate_origins)
    changed = True
    while changed:
        changed = False
        for target, source_id in local_alias_edges:
            if source_id in candidate_ids and target not in candidate_ids:
                candidate_ids.add(target)
                candidate_origins[target] = "alias"
                changed = True

    rig_evidence: dict[str, set[str]] = {identity: set() for identity in candidate_ids}
    for node in walk(ast.get("root", {})):
        if node.get("type") in {"AstExprIndexName", "AstExprIndexExpr"}:
            owner_id = _expr_local_id(node.get("expr", {}))
            member = _constant_member_name(node)
            if owner_id in rig_evidence and member in {"Torso", "HumanoidRootPart", "Head", "Left Arm", "Right Arm", "Left Leg", "Right Leg"}:
                rig_evidence[owner_id].add(member)
        elif node.get("type") == "AstExprCall":
            func = node.get("func", {})
            owner_id = _expr_local_id(func.get("expr", {})) if func.get("type") in {"AstExprIndexName", "AstExprIndexExpr"} else None
            if owner_id in rig_evidence and _constant_member_name(func) == "FindFirstChildOfClass":
                args = node.get("args", [])
                if args and args[0].get("type") == "AstExprConstantString" and args[0].get("value") == "Humanoid":
                    rig_evidence[owner_id].add("Humanoid")

    # Evidence observed through an alias also proves its source candidate.
    changed = True
    while changed:
        changed = False
        for target, source_id in local_alias_edges:
            if target in rig_evidence and source_id in rig_evidence:
                merged = rig_evidence[source_id] | rig_evidence[target]
                if merged != rig_evidence[source_id]:
                    rig_evidence[source_id] = merged
                    changed = True

    character_ids: set[str] = set()
    for identity in candidate_ids:
        origin = candidate_origins.get(identity)
        evidence = rig_evidence.get(identity, set())
        if origin in {"localplayer_character", "hub_character"}:
            character_ids.add(identity)
        elif origin in {"workspace_lookup", "alias"} and (
            {"Torso", "HumanoidRootPart"}.issubset(evidence)
            or ("Humanoid" in evidence and bool(evidence.intersection({"Torso", "HumanoidRootPart", "Head"})))
        ):
            character_ids.add(identity)

    # Propagate only validated character identities through aliases.
    changed = True
    while changed:
        changed = False
        for target, source_id in local_alias_edges:
            if source_id in character_ids and target not in character_ids:
                character_ids.add(target)
                changed = True

    def is_direct_game_localplayer(value: dict[str, Any]) -> bool:
        current = value
        if current.get("type") not in {"AstExprIndexName", "AstExprIndexExpr"} or _constant_member_name(current) != "LocalPlayer":
            return False
        current = current.get("expr", {})
        if current.get("type") in {"AstExprIndexName", "AstExprIndexExpr"} and _constant_member_name(current) == "Players":
            root = current.get("expr", {})
            return root.get("type") == "AstExprGlobal" and root.get("global") == "game"
        if current.get("type") == "AstExprCall":
            func = current.get("func", {})
            args = current.get("args", [])
            return (_constant_member_name(func) == "GetService"
                    and func.get("expr", {}).get("type") == "AstExprGlobal"
                    and func.get("expr", {}).get("global") == "game"
                    and args and args[0].get("type") == "AstExprConstantString"
                    and args[0].get("value") == "Players")
        return False

    player_global_names: set[str] = set()
    mouse_global_names: set[str] = set()
    for root_statement in body:
        if root_statement.get("type") != "AstStatAssign":
            continue
        for var, value in zip(root_statement.get("vars", []), root_statement.get("values", [])):
            if var.get("type") != "AstExprGlobal":
                continue
            name = var.get("global")
            if is_direct_game_localplayer(value):
                player_global_names.add(name)
            elif name in player_global_names:
                player_global_names.discard(name)
            if (value.get("type") == "AstExprCall"
                    and _constant_member_name(value.get("func", {})) == "GetMouse"
                    and value.get("func", {}).get("expr", {}).get("type") == "AstExprGlobal"
                    and value.get("func", {}).get("expr", {}).get("global") in player_global_names):
                mouse_global_names.add(name)
            elif name in mouse_global_names:
                mouse_global_names.discard(name)

    part_roles: dict[str, str] = {}
    neck_proxy_by_torso: dict[str, str] = {}
    for var, value in local_declarations:
        owner = value.get("expr", {}) if value.get("type") in {"AstExprIndexName", "AstExprIndexExpr"} else {}
        if _expr_local_id(owner) not in character_ids:
            continue
        role = {
            "Torso": "torso", "HumanoidRootPart": "rootpart",
            "Left Arm": "left_arm", "Right Arm": "right_arm",
            "Left Leg": "left_leg", "Right Leg": "right_leg",
        }.get(_constant_member_name(value))
        if role:
            var_id = _decl_id(var)
            part_roles[var_id] = role
            if role == "torso":
                safe_name = re.sub(r"\W+", "_", var.get("name") or "Torso")
                neck_proxy_by_torso[var_id] = "__NeckProxy_" + safe_name

    for statement in body:
        location = statement.get("location")
        if not location:
            continue
        start, end = _location_offsets(source, location)
        text = source[start:end]

        # Remove the legacy external reanimation bootstrap. The hub owns the
        # character passed to Init; executing downloaded reanimation is forbidden.
        if statement.get("type") == "AstStatExpr" and re.search(
            r"\bloadstring\s*\(.*\bGetObjects\s*\(", text, re.S
        ):
            edits.append((start, end, '-- removed external reanimation bootstrap', "remove_reanimation_loader"))
            continue

        # Replace only the top-level binding whose declared local is Character.
        # Shadowed locals in functions are untouched because we inspect root.body.
        if statement.get("type") == "AstStatLocal":
            names = [v.get("name") for v in statement.get("vars", [])]
            if len(statement.get("vars", [])) == 1 and statement.get("values") and _decl_id(statement["vars"][0]) in character_ids:
                character_name = statement["vars"][0].get("name")
                edits.append((start, end, "local " + character_name + " = __HubCharacter", "bind_init_character_by_role"))
                continue
            if statement.get("values") and len(statement.get("vars", [])) == 1:
                var = statement["vars"][0]
                value = statement["values"][0]
                value_owner_role = (part_roles.get(_expr_local_id(value.get("expr", {})))
                                    if value.get("type") in {"AstExprIndexName", "AstExprIndexExpr"} else None)
                if value.get("type") in {"AstExprIndexName", "AstExprIndexExpr"} and _constant_member_name(value) == "RootJoint" and value_owner_role == "rootpart":
                    value_start, value_end = _location_offsets(source, value["location"])
                    edits.append((start, end, "local " + var.get("name") + " = __WrapJoint(" + source[value_start:value_end] + ")", "wrap_root_joint_by_role"))
                    continue
                var_id = _decl_id(var)
                if part_roles.get(var_id) == "torso":
                    proxy_name = neck_proxy_by_torso[var_id]
                    edits.append((start, end, text + "\nlocal " + proxy_name + " = __WrapJoint(" + var.get("name") + ".Neck)", "wrap_neck_joint_by_role"))
                    continue

        if statement.get("type") == "AstStatAssign" and len(statement.get("vars", [])) == 1 and len(statement.get("values", [])) == 1:
            target = statement["vars"][0]
            value = statement["values"][0]
            target_id = _expr_local_id(target)
            source_id = _expr_local_id(value)
            if target_id in character_ids and (source_id in character_ids or id(statement) in character_assignment_ids):
                target_start, target_end = _location_offsets(source, target["location"])
                edits.append((start, end, source[target_start:target_end] + " = __HubCharacter", "bind_assigned_character_by_role"))
                continue

        # Preserve the hub's animation driver. Legacy scripts commonly detach
        # Animator/Animate before creating replacement joints; doing that inside
        # a reusable moveset breaks the host after Destroy.
        if statement.get("type") == "AstStatAssign" and re.match(
            r"\s*(?:Humanoid\s*\.\s*Animator|Character\s*\.\s*Animate)\s*\.\s*Parent\s*=\s*nil\s*$",
            text,
        ):
            edits.append((start, end, "-- host Animator/Animate preserved by semantic adapter", "preserve_animation_driver"))
            continue

        # Replace the common local newMotor factory with existing-joint reuse.
        # This is based on the declared binding, not on a textual function body.
        if statement.get("type") == "AstStatLocal":
            names = [v.get("name") for v in statement.get("vars", [])]
            if "newMotor" in names and statement.get("values") and statement["values"][0].get("type") == "AstExprFunction":
                replacement = '''local newMotor = function(part0, part1, c0, c1)
    local existing = nil
    for _, candidate in ipairs(part0:GetChildren()) do
        if candidate:IsA("Motor6D") and candidate.Part0 == part0 and candidate.Part1 == part1 then
            existing = candidate
            break
        end
    end
    local joint = existing
    if joint then
        SaveJoint(joint)
    else
        joint = TrackInstance(Instance.new("Motor6D"))
        joint.Name = part1.Name .. "Joint"
        joint.Parent = part0
        joint.Part0 = part0
        joint.Part1 = part1
    end
    joint.C0 = c0
    joint.C1 = c1
    return __WrapJoint(joint)
end'''
                edits.append((start, end, replacement, "reuse_motor6d"))
                continue

        # FELOADLIBRARY's downloaded Create helper is replaced locally rather
        # than invented as an external dependency.
        if statement.get("type") == "AstStatAssign" and re.match(r"\s*FELOADLIBRARY\s*=\s*\{", text):
            replacement = '''FELOADLIBRARY = {}
FELOADLIBRARY.Create = function(className)
    return function(properties)
        local object = TrackInstance(Instance.new(className))
        for key, value in pairs(properties or {}) do
            if key == "Parent" then
                object.Parent = value
            else
                object[key] = value
            end
        end
        return object
    end
end'''
            edits.append((start, end, replacement, "install_create_compat"))

    # Redirect direct Torso.Neck.C0/C1 accesses to the generated neck proxy.
    for node in walk(ast.get("root", {})):
        if node.get("type") not in {"AstExprIndexName", "AstExprIndexExpr"} or _constant_member_name(node) not in {"C0", "C1"}:
            continue
        neck = node.get("expr", {})
        torso = neck.get("expr", {}) if neck.get("type") in {"AstExprIndexName", "AstExprIndexExpr"} and _constant_member_name(neck) == "Neck" else {}
        torso_id = _expr_local_id(torso)
        proxy_name = neck_proxy_by_torso.get(torso_id)
        if proxy_name:
            expr_start, expr_end = _location_offsets(source, node["location"])
            edits.append((expr_start, expr_end, proxy_name + "." + _constant_member_name(node), "redirect_neck_pose_by_role"))

    # Route legacy mouse events through host-owned signals. This extracts input
    # ownership from the player mouse while preserving callback bodies.
    for node in walk(ast.get("root", {})):
        if node.get("type") not in {"AstExprIndexName", "AstExprIndexExpr"}:
            continue
        member = _constant_member_name(node)
        receiver = node.get("expr", {})
        receiver_id = _expr_local_id(receiver)
        receiver_state = (provenance.expression_before.get(receiver.get("location", ""))
                          if provenance is not None else None)
        receiver_is_mouse = bool(
            (receiver_id and receiver_state and receiver_state.get(receiver_id) == frozenset({"player_mouse"}))
            or (receiver.get("type") == "AstExprGlobal" and receiver.get("global") in mouse_global_names)
        )
        receiver_is_uis = bool(receiver_id and receiver_state and receiver_state.get(receiver_id) == frozenset({"user_input_service"}))
        if member in {"KeyDown", "Button1Down"} and receiver_is_mouse:
            replacement = "LegacyKeySignal.Event" if member == "KeyDown" else "LegacyPrimarySignal.Event"
        elif member == "InputBegan" and receiver_is_uis:
            replacement = "LegacyInputBeganSignal.Event"
        else:
            continue
        expr_start, expr_end = _location_offsets(source, node["location"])
        edits.append((expr_start, expr_end, replacement, "route_" + member.lower()))

    input_began_bindings: set[tuple[str, str]] = set()
    input_began_callbacks_analyzed = 0
    input_began_callbacks_without_proven_bindings = 0
    input_began_callbacks_with_unsupported_branches = 0
    processed_parameter_used = False

    def enum_item(expr: dict[str, Any]) -> tuple[str, str] | None:
        if expr.get("type") not in {"AstExprIndexName", "AstExprIndexExpr"}:
            return None
        item = _constant_member_name(expr)
        category_expr = expr.get("expr", {})
        category = _constant_member_name(category_expr)
        root = category_expr.get("expr", {}) if category_expr.get("type") in {"AstExprIndexName", "AstExprIndexExpr"} else {}
        if root.get("type") == "AstExprGlobal" and root.get("global") == "Enum" and category in {"KeyCode", "UserInputType"}:
            return category, item
        return None

    for call in walk(ast.get("root", {})):
        if call.get("type") != "AstExprCall" or _constant_member_name(call.get("func", {})) not in {"Connect", "connect"}:
            continue
        signal = call.get("func", {}).get("expr", {})
        if _constant_member_name(signal) != "InputBegan":
            continue
        receiver = signal.get("expr", {})
        receiver_id = _expr_local_id(receiver)
        receiver_state = provenance.expression_before.get(receiver.get("location", ""))
        if not (receiver_id and receiver_state and receiver_state.get(receiver_id) == frozenset({"user_input_service"})):
            continue
        callback = next((arg for arg in call.get("args", []) if arg.get("type") == "AstExprFunction"), None)
        if not callback or not callback.get("args"):
            continue
        input_id = _decl_id(callback["args"][0])
        processed_id = _decl_id(callback["args"][1]) if len(callback.get("args", [])) > 1 else None
        input_began_callbacks_analyzed += 1
        callback_bindings: set[tuple[str, str]] = set()
        callback_has_unsupported_branch = False
        input_reference_locations: set[str] = set()
        covered_input_reference_locations: set[str] = set()
        body = callback.get("body", {})
        reassigned_ids: set[str] = set()
        assignment_locations: dict[str, list[tuple[int, int]]] = {}
        assignment_target_nodes: set[int] = set()
        for statement in walk(body):
            if statement.get("type") == "AstStatAssign":
                for var in statement.get("vars", []):
                    identity = _expr_local_id(var)
                    if identity:
                        reassigned_ids.add(identity)
                        assignment_locations.setdefault(identity, []).append(_location_key(var))
                        assignment_target_nodes.add(id(var))
        reference_locations: dict[str, list[tuple[int, int]]] = {}
        for expression in walk(body):
            if expression.get("type") != "AstExprLocal" or id(expression) in assignment_target_nodes:
                continue
            identity = _expr_local_id(expression)
            if identity:
                reference_locations.setdefault(identity, []).append(_location_key(expression))

        def immutable_until_last_use(identity: str) -> bool:
            assignments = assignment_locations.get(identity, [])
            uses = reference_locations.get(identity, [])
            return not assignments or (bool(uses) and min(assignments) > max(uses))

        input_aliases: set[str] = {input_id}
        property_aliases: dict[str, str] = {}
        alias_initializer_locations: set[str] = set()
        for statement in walk(body):
            if statement.get("type") != "AstStatLocal":
                continue
            for declaration, value in zip(statement.get("vars", []), statement.get("values", [])):
                declaration_id = _decl_id(declaration)
                if declaration_id in reassigned_ids and not immutable_until_last_use(declaration_id):
                    continue
                value_id = _expr_local_id(value)
                if value_id in input_aliases:
                    input_aliases.add(declaration_id)
                    if value.get("location"):
                        alias_initializer_locations.add(value["location"])
                elif value_id in property_aliases:
                    property_aliases[declaration_id] = property_aliases[value_id]
                    if value.get("location"):
                        alias_initializer_locations.add(value["location"])
                elif value.get("type") in {"AstExprIndexName", "AstExprIndexExpr"} and _expr_local_id(value.get("expr", {})) in input_aliases:
                    category = _constant_member_name(value)
                    if category in {"KeyCode", "UserInputType"}:
                        property_aliases[declaration_id] = category
                        receiver_location = value.get("expr", {}).get("location")
                        if receiver_location:
                            alias_initializer_locations.add(receiver_location)

        tracked_input_ids = input_aliases | set(property_aliases)

        def contains_input(expr: Any) -> bool:
            return any(node.get("type") == "AstExprLocal" and _expr_local_id(node) in tracked_input_ids for node in walk(expr))

        def exact_input_binding(expr: dict[str, Any]) -> tuple[str, str] | None:
            if expr.get("type") != "AstExprBinary" or expr.get("op") != "CompareEq":
                return None
            left, right = expr.get("left", {}), expr.get("right", {})
            for property_expr, enum_expr in ((left, right), (right, left)):
                expected_category = None
                local_reference = None
                if property_expr.get("type") in {"AstExprIndexName", "AstExprIndexExpr"}:
                    receiver = property_expr.get("expr", {})
                    if _expr_local_id(receiver) in input_aliases:
                        expected_category = _constant_member_name(property_expr)
                        local_reference = receiver
                elif property_expr.get("type") == "AstExprLocal":
                    expected_category = property_aliases.get(_expr_local_id(property_expr))
                    local_reference = property_expr
                parsed = enum_item(enum_expr)
                if expected_category and parsed and parsed[0] == expected_category:
                    local_location = local_reference.get("location") if local_reference else None
                    if local_location:
                        covered_input_reference_locations.add(local_location)
                    return parsed
            return None

        def analyze_control_predicate(expr: dict[str, Any]) -> tuple[set[tuple[str, str]], bool]:
            if not contains_input(expr):
                return set(), True
            exact = exact_input_binding(expr)
            if exact:
                return {exact}, True
            if expr.get("type") == "AstExprBinary" and expr.get("op") in {"And", "Or", "LogicalAnd", "LogicalOr"}:
                left_bindings, left_complete = analyze_control_predicate(expr.get("left", {}))
                right_bindings, right_complete = analyze_control_predicate(expr.get("right", {}))
                return left_bindings | right_bindings, left_complete and right_complete
            return set(), False

        covered_input_reference_locations.update(alias_initializer_locations)
        for candidate in walk(body):
            if (candidate.get("type") == "AstExprLocal"
                    and id(candidate) not in assignment_target_nodes
                    and _expr_local_id(candidate) in tracked_input_ids):
                input_reference_locations.add(candidate.get("location", ""))
            if processed_id and candidate.get("type") == "AstExprLocal" and _expr_local_id(candidate) == processed_id:
                processed_parameter_used = True
            if candidate.get("type") == "AstStatIf":
                branch_bindings, branch_complete = analyze_control_predicate(candidate.get("condition", {}))
                callback_bindings.update(branch_bindings)
                if contains_input(candidate.get("condition", {})) and not branch_complete:
                    callback_has_unsupported_branch = True

        if not callback_bindings:
            input_began_callbacks_without_proven_bindings += 1
        uncovered_input_references = input_reference_locations - covered_input_reference_locations
        if callback_has_unsupported_branch or uncovered_input_references:
            input_began_callbacks_with_unsupported_branches += 1
        input_began_bindings.update(callback_bindings)

    rewritten = source
    for start, end, replacement, _ in sorted(edits, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    summary: dict[str, Any] = {"engine": "ast_root_statement_rewriter", "edits": []}
    for start, end, _, kind in sorted(edits):
        summary["edits"].append({"kind": kind, "start": start, "end": end})
    summary["counts"] = {kind: sum(1 for *_, k in edits if k == kind) for kind in {e[3] for e in edits}}
    summary["input_began_bindings"] = [
        {"enum": category, "item": item}
        for category, item in sorted(input_began_bindings)
    ]
    summary["input_began_callbacks_analyzed"] = input_began_callbacks_analyzed
    summary["input_began_callbacks_without_proven_bindings"] = input_began_callbacks_without_proven_bindings
    summary["input_began_callbacks_with_unsupported_branches"] = input_began_callbacks_with_unsupported_branches
    summary["game_processed_parameter_used"] = processed_parameter_used
    return rewritten, summary


def _decl_id(decl: dict[str, Any]) -> str:
    return f"{decl.get('name')}@{decl.get('location')}"


def _expr_local_id(expr: dict[str, Any]) -> str | None:
    if expr.get("type") != "AstExprLocal":
        return None
    return _decl_id(expr.get("local", {}))


def _walk_without_nested_functions(node: Any, root: bool = True):
    """Yield structural nodes while treating every nested function as opaque."""
    if isinstance(node, dict):
        node_type = node.get("type")
        if node_type:
            yield node
        if not root and node_type in {"AstExprFunction", "AstStatLocalFunction", "AstStatFunction"}:
            return
        for value in node.values():
            yield from _walk_without_nested_functions(value, False)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_without_nested_functions(value, False)


def _is_table_insert(call: dict[str, Any]) -> bool:
    if call.get("type") != "AstExprCall":
        return False
    func = call.get("func", {})
    return (func.get("type") == "AstExprIndexName" and func.get("index") == "insert"
            and func.get("expr", {}).get("type") == "AstExprGlobal"
            and func.get("expr", {}).get("global") == "table")


def identify_contract(source: str, ast: dict[str, Any] | None = None) -> dict[str, bool]:
    """Strict proof of a registered Reanimation Hub moveset factory."""
    lifecycle = ("Config", "LoadConfig", "SaveConfig", "Init", "Update", "Destroy")
    required_lifecycle = ("Config", "Init", "Update", "Destroy")
    result = {**{name: False for name in lifecycle}, "return_modules": False,
              "ModuleType_MOVESET": False, "registry_identity_proven": False,
              "registration_proven": False, "lifecycle_function_types_proven": False,
              "direct_factory_return_proven": False, "ast_factory_proven": False,
              "registered_factory_count": 0, "valid_moveset_factory_count": 0,
              "invalid_factory_locations": []}
    if not ast:
        return result
    root_body = ast.get("root", {}).get("body", [])

    # The root registry must be a root local table and the sole value of a
    # direct root return statement.
    root_table_ids: set[str] = set()
    for statement in root_body:
        if statement.get("type") == "AstStatLocal":
            for var, value in zip(statement.get("vars", []), statement.get("values", [])):
                if value.get("type") == "AstExprTable":
                    root_table_ids.add(_decl_id(var))
    root_return_ids: list[str] = []
    for statement in root_body:
        if statement.get("type") == "AstStatReturn" and len(statement.get("list", [])) == 1:
            identity = _expr_local_id(statement["list"][0])
            if identity:
                root_return_ids.append(identity)
    registries = root_table_ids.intersection(root_return_ids)
    if len(registries) != 1:
        return result
    registry_id = next(iter(registries))
    result["return_modules"] = True

    # Resolve only local AddModule helpers that provably insert their first
    # parameter into the exact root-returned registry.
    proven_add_helpers: set[str] = set()
    for statement in root_body:
        if statement.get("type") != "AstStatLocalFunction":
            continue
        func = statement.get("func", {})
        params = func.get("args", [])
        if not params:
            continue
        parameter_id = _decl_id(params[0])
        helper_ok = False
        # Registration must execute unconditionally as a direct helper-body
        # statement. Calls hidden in if/loops/callbacks are not proof.
        for helper_statement in func.get("body", {}).get("body", []):
            if helper_statement.get("type") != "AstStatExpr":
                continue
            node = helper_statement.get("expr", {})
            if not _is_table_insert(node):
                continue
            args = node.get("args", [])
            if len(args) >= 2 and _expr_local_id(args[0]) == registry_id and _expr_local_id(args[1]) == parameter_id:
                helper_ok = True
        if helper_ok:
            proven_add_helpers.add(_decl_id(statement.get("name", {})))

    # Collect only factories whose registration targets that registry directly
    # or through a proven AddModule helper.
    factories: list[dict[str, Any]] = []
    for statement in root_body:
        if statement.get("type") != "AstStatExpr":
            continue
        call = statement.get("expr", {})
        if call.get("type") != "AstExprCall":
            continue
        args = call.get("args", [])
        if _is_table_insert(call):
            if len(args) >= 2 and _expr_local_id(args[0]) == registry_id and args[1].get("type") == "AstExprFunction":
                factories.append(args[1])
            continue
        if _expr_local_id(call.get("func", {})) in proven_add_helpers:
            if len(args) == 1 and args[0].get("type") == "AstExprFunction":
                factories.append(args[0])

    result["registry_identity_proven"] = True
    result["registration_proven"] = bool(factories)
    if not factories:
        return result

    result["registered_factory_count"] = len(factories)
    valid_count = 0
    invalid_locations: list[str] = []
    bundle_field_intersection: set[str] = set(lifecycle)
    for factory in factories:
        body = factory.get("body", {}).get("body", [])
        table_ids: set[str] = set()
        for statement in body:
            if statement.get("type") == "AstStatLocal":
                for var, value in zip(statement.get("vars", []), statement.get("values", [])):
                    if value.get("type") == "AstExprTable":
                        table_ids.add(_decl_id(var))
        direct_returns = {
            _expr_local_id(statement["list"][0])
            for statement in body
            if statement.get("type") == "AstStatReturn" and len(statement.get("list", [])) == 1
            and _expr_local_id(statement["list"][0])
        }
        owners = table_ids.intersection(direct_returns)
        factory_valid = False
        for owner in owners:
            functional_fields: set[str] = set()
            assigned_lifecycle_fields: set[str] = set()
            module_type_ok = False
            for node in body:
                if node.get("type") != "AstStatAssign":
                    continue
                for var, value in zip(node.get("vars", []), node.get("values", [])):
                    if var.get("type") != "AstExprIndexName" or _expr_local_id(var.get("expr", {})) != owner:
                        continue
                    field = var.get("index")
                    if field in lifecycle:
                        assigned_lifecycle_fields.add(field)
                        if value.get("type") == "AstExprFunction":
                            functional_fields.add(field)
                    elif field == "ModuleType":
                        module_type_ok = (value.get("type") == "AstExprConstantString"
                                          and value.get("value") == "MOVESET")
            optional_types_ok = assigned_lifecycle_fields.issubset(functional_fields)
            if all(name in functional_fields for name in required_lifecycle) and optional_types_ok and module_type_ok:
                factory_valid = True
                break
        if factory_valid:
            valid_count += 1
            bundle_field_intersection.intersection_update(functional_fields)
        else:
            invalid_locations.append(factory.get("location", "unknown"))

    result["valid_moveset_factory_count"] = valid_count
    result["invalid_factory_locations"] = invalid_locations
    all_factories_valid = bool(factories) and valid_count == len(factories)
    if all_factories_valid:
        for name in lifecycle:
            result[name] = name in bundle_field_intersection
        result["ModuleType_MOVESET"] = True
        result["lifecycle_function_types_proven"] = True
        result["direct_factory_return_proven"] = True
        result["ast_factory_proven"] = True
    return result

def extract_ir(source: str, ast: dict[str, Any]) -> SemanticIR:
    contract = identify_contract(source, ast)
    existing = bool(contract.get("ast_factory_proven"))

    char_patterns = [
        r"(?:Players\.)?LocalPlayer\.Character", r"CharacterAdded\s*:\s*Wait",
        r"workspace\.(?:non|Character)", r"GetCharacter\s*\(", r"Init\s*=\s*function\s*\([^)]*model",
    ]
    bindings = [p for p in char_patterns if re.search(p, source, re.I)]
    assets = sorted(set(re.findall(r"(?:rbxassetid://|[?&]id=)(\d{5,})", source)))
    urls = sorted(set(re.findall(r"https?://[^\s'\"]+", source)))

    legacy_markers = sum(bool(re.search(p, source, re.I)) for p in (
        r"FELOADLIBRARY", r"LoadLibrary", r"Mouse\.KeyDown", r"ArtificialHB",
        r"swait\s*\(", r"CreateSound\s*\(", r"Motor6D|\bMotor\.new",
    ))
    if existing:
        family = "existing_hub_module"
    elif legacy_markers >= 2:
        family = "legacy_feloadlibrary_r6_combat"
    else:
        family = "unknown_legacy"

    return SemanticIR(
        family=family,
        existing_hub_module=existing,
        character_bindings=bindings,
        joint_operations={
            "Motor6D": occurrences(source, r"\bMotor6D\b"),
            "Motor.new": occurrences(source, r"\bMotor\s*\.\s*new\b"),
            "C0_writes": occurrences(source, r"\.\s*C0\s*="),
            "C1_writes": occurrences(source, r"\.\s*C1\s*="),
            "Transform_writes": occurrences(source, r"\.\s*Transform\s*="),
            "Weld": occurrences(source, r"\bWeld\b"),
        },
        animation_drivers={
            "RunService": occurrences(source, r"\bRunService\b"),
            "Heartbeat": occurrences(source, r"\bHeartbeat\b"),
            "RenderStepped": occurrences(source, r"\bRenderStepped\b"),
            "Stepped": occurrences(source, r"\bStepped\b"),
            "while_loops": occurrences(source, r"\bwhile\b[^\n]*\bdo\b"),
            "repeat_loops": occurrences(source, r"\brepeat\b"),
        },
        abilities=sorted(set(re.findall(r"(?:KeyCode\.|key\s*==\s*['\"])([A-Za-z0-9]+)", source, re.I))),
        pc_inputs={
            "Mouse.KeyDown": occurrences(source, r"Mouse\s*\.\s*KeyDown"),
            "UserInputService": occurrences(source, r"\bUserInputService\b"),
            "ContextActionService": occurrences(source, r"\bContextActionService\b"),
        },
        mobile_inputs={
            "TouchEnabled": occurrences(source, r"TouchEnabled"),
            "TouchTap": occurrences(source, r"TouchTap"),
            "mobile_buttons": occurrences(source, r"(?:TextButton|ImageButton)"),
        },
        gui={
            "ScreenGui": occurrences(source, r"\bScreenGui\b"),
            "BillboardGui": occurrences(source, r"\bBillboardGui\b"),
            "buttons": occurrences(source, r"(?:TextButton|ImageButton)"),
        },
        resources={
            "Connect": occurrences(source, r":\s*Connect\s*\("),
            "task.spawn": occurrences(source, r"\btask\s*\.\s*spawn\s*\("),
            "coroutine": occurrences(source, r"\bcoroutine\b"),
            "Instance.new": occurrences(source, r"\bInstance\s*\.\s*new\s*\("),
            "Debris.AddItem": occurrences(source, r"Debris\s*:\s*AddItem\s*\("),
        },
        destructive_operations={
            "BreakJoints": occurrences(source, r":\s*BreakJoints\s*\("),
            "character_Destroy": occurrences(source, r"(?:Character|char|model)\s*:\s*Destroy\s*\(", re.I),
            "BodyVelocity": occurrences(source, r"\bBodyVelocity\b"),
            "BodyPosition": occurrences(source, r"\bBodyPosition\b"),
            "Humanoid_Health_write": occurrences(source, r"\.\s*Health\s*="),
        },
        external_dependencies=[*(f"asset:{x}" for x in assets), *(f"url:{x}" for x in urls)],
        hub_contract=contract,
    )


def findings_for(source: str, ir: SemanticIR) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    blockers: list[str] = []
    def add(code: str, severity: str, message: str, pattern: str = "") -> None:
        findings.append(Finding(code, severity, message, lines_for(source, pattern) if pattern else []))

    if ir.resources["Connect"] and not ir.hub_contract.get("Destroy"):
        add("UNOWNED_CONNECTIONS", "blocker", "Connections exist without a verified hub Destroy contract.", r":\s*Connect")
        blockers.append("Resource ownership for event connections is unknown")
    if ir.animation_drivers["while_loops"] or ir.animation_drivers["repeat_loops"]:
        severity = "warning" if ir.existing_hub_module else "blocker"
        add("UNBOUNDED_TASKS", severity, "Long-running loops require lifecycle/runtime verification.", r"\b(?:while|repeat)\b")
        if not ir.existing_hub_module:
            blockers.append("Async loops are not proven cancellable")
    if any(ir.destructive_operations.values()):
        severity = "warning" if ir.existing_hub_module else "blocker"
        add("DESTRUCTIVE_CHARACTER_LOGIC", severity, "Potential death, fling, or character destruction semantics detected.", r"BreakJoints|BodyVelocity|BodyPosition|\.Health\s*=|:Destroy")
        if not ir.existing_hub_module:
            blockers.append("Destructive operations require semantic classification")
    if ir.external_dependencies:
        add("EXTERNAL_DEPENDENCIES", "warning", "External assets/code are recorded, not fabricated.")
    if ir.family == "unknown_legacy":
        blockers.append("No deterministic adapter family matches this source")
    return findings, list(dict.fromkeys(blockers))


def _constant_member_name(expr: dict[str, Any]) -> str | None:
    if expr.get("type") == "AstExprIndexName":
        return expr.get("index")
    if expr.get("type") == "AstExprIndexExpr":
        index = expr.get("index", {})
        if index.get("type") == "AstExprConstantString":
            return index.get("value")
    return None


def _call_method_name(call: dict[str, Any]) -> str | None:
    if call.get("type") != "AstExprCall":
        return None
    func = call.get("func", {})
    if func.get("type") == "AstExprGlobal":
        return func.get("global")
    return _constant_member_name(func)


def _location_key(node: dict[str, Any]) -> tuple[int, int]:
    match = re.match(r"(\d+),(\d+)", node.get("location", ""))
    return (int(match.group(1)), int(match.group(2))) if match else (10**9, 10**9)


def _is_getgenv_result(expr: dict[str, Any]) -> bool:
    return (expr.get("type") == "AstExprCall"
            and expr.get("func", {}).get("type") == "AstExprGlobal"
            and expr.get("func", {}).get("global") == "getgenv")


def _is_global_env(expr: dict[str, Any]) -> bool:
    return ((expr.get("type") == "AstExprGlobal" and expr.get("global") == "_G")
            or _is_getgenv_result(expr))


def executable_external_code_blockers(source: str, ast: dict[str, Any], provenance: Any | None = None) -> list[str]:
    """Binding-aware detection of downloaded/executed code.

    Events are processed in lexical order. Reassignment invalidates trusted
    aliases before subsequent calls are classified.
    """
    dangerous_callables: set[str] = set()
    game_receivers: set[str] = set()
    blockers: list[str] = []

    # Summarize local functions that invoke captured locals as callees. At each
    # invocation, those captured bindings are evaluated in call-site state.
    function_callee_summaries: dict[str, set[str]] = {}
    for statement in walk(ast.get("root", {})):
        function_id = None
        function_expr = None
        if statement.get("type") == "AstStatLocalFunction":
            function_id = _decl_id(statement.get("name", {}))
            function_expr = statement.get("func", {})
        elif statement.get("type") in {"AstStatLocal", "AstStatAssign"}:
            for var, value in zip(statement.get("vars", []), statement.get("values", [])):
                if value.get("type") == "AstExprFunction":
                    assigned_function_id = (_decl_id(var) if var.get("type") == "AstLocal" else _expr_local_id(var))
                    if assigned_function_id:
                        function_callee_summaries[assigned_function_id] = {
                            _expr_local_id(node.get("func", {}))
                            for node in walk(value.get("body", {}))
                            if node.get("type") == "AstExprCall" and _expr_local_id(node.get("func", {}))
                        }
        if function_id and function_expr:
            function_callee_summaries[function_id] = {
                _expr_local_id(node.get("func", {}))
                for node in walk(function_expr.get("body", {}))
                if node.get("type") == "AstExprCall" and _expr_local_id(node.get("func", {}))
            }

    function_ids = set(function_callee_summaries)
    function_alias_targets: dict[str, set[str]] = {identity: {identity} for identity in function_ids}
    alias_edges: list[tuple[str, str]] = []
    for node in walk(ast.get("root", {})):
        if node.get("type") not in {"AstStatLocal", "AstStatAssign"}:
            continue
        for var, value in zip(node.get("vars", []), node.get("values", [])):
            target = _decl_id(var) if var.get("type") == "AstLocal" else _expr_local_id(var)
            source_id = _expr_local_id(value)
            if target and source_id:
                alias_edges.append((target, source_id))
    changed = True
    while changed:
        changed = False
        for target, source_id in alias_edges:
            resolved = function_alias_targets.get(source_id, set())
            if resolved and not resolved.issubset(function_alias_targets.setdefault(target, set())):
                function_alias_targets[target].update(resolved)
                changed = True

    # Compute captured dangerous-call dependencies transitively. Recursive
    # cycles converge because summaries only grow over a finite binding set.
    transitive_captured: dict[str, set[str]] = {identity: set() for identity in function_ids}
    changed = True
    while changed:
        changed = False
        for function_id, direct_refs in function_callee_summaries.items():
            expanded = set(transitive_captured[function_id])
            for reference in direct_refs:
                # Always retain the mutable binding as a call-site dependency.
                # Known function identities add transitive summaries but never
                # replace checking the binding's current origin.
                expanded.add(reference)
                for target in function_alias_targets.get(reference, set()):
                    expanded.update(transitive_captured.get(target, set()))
            if expanded != transitive_captured[function_id]:
                transitive_captured[function_id] = expanded
                changed = True

    def local_identity(expr: dict[str, Any]) -> str | None:
        return _expr_local_id(expr)

    def is_dangerous_callable(expr: dict[str, Any]) -> bool:
        if expr.get("type") == "AstExprGlobal" and expr.get("global") == "loadstring":
            return True
        identity = local_identity(expr)
        if identity and provenance is not None:
            state = provenance.expression_before.get(expr.get("location", ""))
            at_expression = state.get(identity) if state else frozenset()
            final_origins = provenance.final_state.get(identity)
            # Closure bodies may be declared before a captured binding receives
            # its dangerous value. Union declaration-time and final origins as
            # a conservative upvalue summary until invocation-sensitive analysis.
            if "dangerous_callable" in (at_expression | final_origins):
                return True
        if identity and identity in dangerous_callables:
            return True
        return _constant_member_name(expr) == "loadstring" and _is_global_env(expr.get("expr", {}))

    def is_game_receiver(expr: dict[str, Any]) -> bool:
        if expr.get("type") == "AstExprGlobal" and expr.get("global") == "game":
            return True
        identity = local_identity(expr)
        if identity and provenance is not None:
            state = provenance.expression_before.get(expr.get("location", ""))
            at_expression = state.get(identity) if state else frozenset()
            if "game" in (at_expression | provenance.final_state.get(identity)):
                return True
        return bool(identity and identity in game_receivers)

    def contains_member(node: Any, name: str) -> bool:
        return any(_constant_member_name(candidate) == name for candidate in walk(node))

    def contains_getobjects(node: Any) -> bool:
        for candidate in walk(node):
            if candidate.get("type") != "AstExprCall" or _call_method_name(candidate) != "GetObjects":
                continue
            func = candidate.get("func", {})
            if is_game_receiver(func.get("expr", {})):
                return True
        return False

    root_body = ast.get("root", {}).get("body", [])
    unconditional_root_assignment_ids = {
        id(statement) for statement in root_body if statement.get("type") == "AstStatAssign"
    }
    events = [node for node in walk(ast.get("root", {}))
              if node.get("type") in {"AstStatLocal", "AstStatAssign", "AstExprCall"}]
    # Assignments precede calls at the same source start only for alias state;
    # calls inside RHS are still independently inspected as call events.
    events.sort(key=lambda node: (_location_key(node), 0 if node.get("type") != "AstExprCall" else 1))

    higher_order = {"pcall", "xpcall", "spawn", "defer", "wrap", "create"}
    for node in events:
        if node.get("type") in {"AstStatLocal", "AstStatAssign"}:
            variables = node.get("vars", [])
            values = node.get("values", [])
            for index, variable in enumerate(variables):
                identity = (_decl_id(variable) if variable.get("type") == "AstLocal"
                            else local_identity(variable))
                if not identity:
                    continue
                value = values[index] if index < len(values) else {}
                dangerous_rhs = is_dangerous_callable(value)
                game_rhs = is_game_receiver(value)
                # A pre-existing dangerous alias may be cleared only by a
                # direct, unconditional root assignment. Assignments inside
                # if/loops/functions are not definite execution and therefore
                # cannot remove trust conservatively. Local declarations create
                # a fresh binding and may initialize it normally.
                fresh_local = node.get("type") == "AstStatLocal"
                definite_clear = fresh_local or id(node) in unconditional_root_assignment_ids
                if definite_clear:
                    dangerous_callables.discard(identity)
                    game_receivers.discard(identity)
                if dangerous_rhs:
                    dangerous_callables.add(identity)
                if game_rhs:
                    game_receivers.add(identity)
            continue

        call = node
        func = call.get("func", {})
        method = _call_method_name(call)
        location = call.get("location", "unknown")
        args = call.get("args", [])
        dangerous_callee = is_dangerous_callable(func)
        if dangerous_callee:
            code = "EXECUTABLE_LOADSTRING" if func.get("type") == "AstExprGlobal" else "EXECUTABLE_LOADSTRING_ALIAS"
            blockers.append(f"{code} at {location}")
            if any(contains_getobjects(arg) and contains_member(arg, "Source") for arg in args):
                blockers.append(f"GETOBJECTS_SOURCE_EXECUTION at {location}")

        invoked_function_id = local_identity(func)
        invoked_targets = function_alias_targets.get(invoked_function_id or "", set())
        captured_callees: set[str] = set()
        for target in invoked_targets:
            captured_callees.update(transitive_captured.get(target, set()))
        if captured_callees and provenance is not None:
            call_state = provenance.expression_before.get(call.get("location", ""))
            if call_state and any("dangerous_callable" in call_state.get(identity) for identity in captured_callees):
                blockers.append(f"DANGEROUS_FUNCTION_SUMMARY_CALL at {location}")

        receiver = func.get("expr", {}) if func.get("type") in {"AstExprIndexName", "AstExprIndexExpr"} else {}
        if method == "HttpGet" and is_game_receiver(receiver):
            blockers.append(f"EXECUTABLE_HTTP_GET at {location}")
        elif method == "GetObjects" and is_game_receiver(receiver):
            blockers.append(f"EXTERNAL_GETOBJECTS at {location}")

        if method in higher_order and any(is_dangerous_callable(arg) for arg in args):
            blockers.append(f"HIGHER_ORDER_LOADSTRING_EXECUTION at {location}")

    return list(dict.fromkeys(blockers))


def validate(path: Path, tool_dir: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name in ("luau-compile", "luau-ast", "luau-analyze"):
        result = run_tool([str(tool_dir / name), str(path)])
        results[name] = {"ok": result["ok"], "exit_code": result["exit_code"],
                         "diagnostic": (result["stderr"] or result["stdout"])[-3000:]}
    return results


def acceptance() -> dict[str, str]:
    return {
        "init_returns": "NOT_RUN", "character_bound_to_init_model": "NOT_RUN",
        "animation_tick_progresses": "NOT_RUN", "pc_ability_routing": "NOT_RUN",
        "mobile_ability_routing": "NOT_RUN", "all_abilities_reachable": "NOT_RUN",
        "destroy_disconnects_resources": "NOT_RUN", "rig_restored_after_destroy": "NOT_RUN",
        "second_init_matches_first": "NOT_RUN", "no_reanimation_bootstrap": "NOT_RUN",
        "no_forbidden_death_or_fling": "NOT_RUN",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AST-backed, fail-closed Reanimation Hub moveset converter")
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path, nargs="?")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--tool-dir", type=Path, default=DEFAULT_TOOLS)
    ap.add_argument("--experimental-adapter", action="store_true",
                    help="Emit the legacy family wrapper, but never mark it runtime-ready")
    ns = ap.parse_args(argv or sys.argv[1:])
    source = ns.input.read_text(encoding="utf-8-sig")
    ast, frontend = parse_ast(ns.input, ns.tool_dir)
    report_path = ns.report or (ns.output.with_suffix(ns.output.suffix + ".report.json") if ns.output
                                else ns.input.with_suffix(ns.input.suffix + ".semantic-report.json"))
    if ast is None:
        report = Report("3.0", str(ns.input), str(ns.output) if ns.output else None,
                        hashlib.sha256(source.encode()).hexdigest(), "VALIDATION_FAILED",
                        "Luau AST parsing failed", frontend, SemanticIR(), [],
                        ["Source cannot be parsed by the configured Luau frontend"], {}, acceptance())
        report_path.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False), encoding="utf-8")
        print("VALIDATION_FAILED: Luau AST parsing failed")
        return 2

    try:
        from luau_provenance import PROVENANCE_API_VERSION, analyze as analyze_provenance
        provenance_source = "luau_provenance.py"
    except ImportError as exc:
        # Browsers may append download suffixes such as `-3`. Permit exactly
        # one API-compatible sibling while still preferring the canonical name.
        candidates = sorted(Path(__file__).resolve().parent.glob("luau_provenance*.py"))
        compatible = []
        for candidate in candidates:
            try:
                spec = importlib.util.spec_from_file_location("_moveset_provenance_candidate", candidate)
                if not spec or not spec.loader:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                if getattr(module, "PROVENANCE_API_VERSION", None) == "2.0" and callable(getattr(module, "analyze", None)):
                    compatible.append((candidate, module))
            except Exception:
                continue
        if len(compatible) != 1:
            print("VALIDATION_FAILED: required dependency luau_provenance.py is missing or ambiguous", file=sys.stderr)
            print(f"Dependency error: {exc}; compatible sibling candidates: {len(compatible)}", file=sys.stderr)
            return 2
        candidate, module = compatible[0]
        PROVENANCE_API_VERSION = module.PROVENANCE_API_VERSION
        analyze_provenance = module.analyze
        provenance_source = candidate.name
    if PROVENANCE_API_VERSION != "2.0":
        print(f"VALIDATION_FAILED: luau_provenance API 2.0 required, found {PROVENANCE_API_VERSION}", file=sys.stderr)
        return 2
    provenance = analyze_provenance(ast)
    ambiguous_bindings = {
        identity: sorted(origins)
        for identity, origins in provenance.final_state.values.items()
        if len(origins) > 1
    }
    frontend["provenance"] = {
        "engine": "scoped_may_origin_dataflow_v1",
        "dependency_file": provenance_source,
        "api_version": PROVENANCE_API_VERSION,
        "binding_count": len(provenance.final_state.values),
        "ambiguous_binding_count": len(ambiguous_bindings),
        "ambiguous_bindings": ambiguous_bindings,
        "branch_merge": "union",
        "loop_merge": "pre_state_union_body_state",
    }

    ir = extract_ir(source, ast)
    findings, blockers = findings_for(source, ir)
    blockers.extend(executable_external_code_blockers(source, ast, provenance))
    contract = ir.hub_contract
    if contract.get("registered_factory_count", 0) > contract.get("valid_moveset_factory_count", 0):
        blockers.append(
            "INVALID_REGISTERED_FACTORIES: "
            f"{contract.get('valid_moveset_factory_count', 0)}/"
            f"{contract.get('registered_factory_count', 0)} registered factories satisfy the moveset contract"
        )
    blockers = list(dict.fromkeys(blockers))
    status, reason = "MANUAL_DEPENDENCY_REQUIRED", "No safe deterministic emission path"
    emitted = False
    validations: dict[str, Any] = {}
    if ir.existing_hub_module:
        if not ns.output:
            status, reason = "DETERMINISTIC_ONLY", "Existing hub module recognized; analysis only"
        else:
            ns.output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ns.input, ns.output)
            emitted = True
            validations = validate(ns.output, ns.tool_dir)
            # luau-analyze cannot resolve executor/hub globals without the real
            # runtime type environment, so its diagnostics are advisory. Parse
            # and bytecode compilation are the hard static gates.
            hard_static_ok = validations["luau-compile"]["ok"] and validations["luau-ast"]["ok"]
            if not hard_static_ok:
                status, reason = "VALIDATION_FAILED", "Pass-through module failed compile or AST validation"
            elif blockers:
                status = "MANUAL_DEPENDENCY_REQUIRED"
                reason = "Existing module is statically valid but security/semantic blockers prohibit runtime candidacy"
            else:
                status, reason = "RUNTIME_TEST_REQUIRED", "Pass-through module compiles and parses; runtime behavior is unverified"
    elif ir.family == "legacy_feloadlibrary_r6_combat" and ns.experimental_adapter and ns.output:
        # The semantic pass operates on root AST statements before lifecycle
        # emission. It does not read or copy any golden output.
        rewritten, rewrite_audit = semantic_rewrite_r6(source, ast, provenance)
        proxy_prelude = '''
local function __WrapJoint(reference)
    if not reference then return nil end
    SaveJoint(reference)
    local proxy = {
        Reference = reference,
        C0 = reference.C0,
        C1 = reference.C1,
    }
    table.insert(state.jointProxies, proxy)
    return proxy
end
'''
        rewritten = proxy_prelude + rewritten
        try:
            from luau_moveset_converter import create_wrapper
            visible_name = ns.input.stem
            internal_name = re.sub(r"[^A-Za-z0-9_]+", "_", visible_name).strip("_").upper() or "LEGACY_R6_MOVESET"
            generated = create_wrapper(rewritten, visible_name, internal_name,
                                       "AST-adapted legacy FELOADLIBRARY/R6 moveset", "safe")
            # create_wrapper exposes Character; the AST pass binds through this
            # explicit alias so source-level character acquisition stays removed.
            generated = generated.replace("local state = {}", "local state = {}\n    state.jointProxies = {}", 1)
            generated = generated.replace("local Character = state.character", "local Character = state.character\n        local __HubCharacter = Character", 1)
            generated = generated.replace("        local LegacyKeySignal = ConversionScope.LegacyKeySignal", "        local LegacyKeySignal = ConversionScope.LegacyKeySignal\n        local LegacyPrimarySignal = ConversionScope.LegacyPrimarySignal\n        local LegacyInputBeganSignal = ConversionScope.LegacyInputBeganSignal", 1)
            generated = generated.replace(
                "        state.legacyKeySignal.Parent = state.runtimeFolder\n        module._Active = true",
                "        state.legacyKeySignal.Parent = state.runtimeFolder\n        state.legacyPrimarySignal = Instance.new(\"BindableEvent\")\n        state.legacyPrimarySignal.Name = \"LegacyPrimarySignal\"\n        state.legacyPrimarySignal.Parent = state.runtimeFolder\n        state.legacyInputBeganSignal = Instance.new(\"BindableEvent\")\n        state.legacyInputBeganSignal.Name = \"LegacyInputBeganSignal\"\n        state.legacyInputBeganSignal.Parent = state.runtimeFolder\n        module._Active = true",
                1,
            )
            generated = generated.replace(
                "            LegacyKeySignal = state.legacyKeySignal,",
                "            LegacyKeySignal = state.legacyKeySignal,\n            LegacyPrimarySignal = state.legacyPrimarySignal,\n            LegacyInputBeganSignal = state.legacyInputBeganSignal,",
                1,
            )
            generated = generated.replace(
                "function(_, inputState)\n                    if inputState == Enum.UserInputState.Begin and module._Active then\n                        state.legacyKeySignal:Fire(keyDefinition.key)\n                    end",
                "function(_, inputState, inputObject)\n                    if inputState == Enum.UserInputState.Begin and module._Active then\n                        state.legacyKeySignal:Fire(keyDefinition.key)\n                        state.legacyInputBeganSignal:Fire(inputObject, false)\n                    end",
            )
            primary_action_name = "ConvertedLegacy_" + internal_name + "_Primary"
            primary_binding = '''        bindAction(
            "''' + primary_action_name + '''",
            function(_, inputState, inputObject)
                if inputState == Enum.UserInputState.Begin and module._Active then
                    state.legacyPrimarySignal:Fire()
                    state.legacyInputBeganSignal:Fire(inputObject, false)
                end
                return Enum.ContextActionResult.Sink
            end,
            false,
            Enum.UserInputType.MouseButton1
        )

'''
            input_binding_blocks = []
            for binding in rewrite_audit.get("input_began_bindings", []):
                category, item = binding["enum"], binding["item"]
                if category == "UserInputType" and item == "MouseButton1":
                    continue
                action_name = f"ConvertedLegacy_{internal_name}_Input_{category}_{item}"
                input_binding_blocks.append(f'''        bindAction(
            "{action_name}",
            function(_, inputState, inputObject)
                if inputState == Enum.UserInputState.Begin and module._Active then
                    state.legacyInputBeganSignal:Fire(inputObject, false)
                end
                return Enum.ContextActionResult.Sink
            end,
            false,
            Enum.{category}.{item}
        )
''')
            input_bindings_lua = "\n".join(input_binding_blocks)
            generated = generated.replace("        for _, descendant in ipairs(model:GetDescendants()) do", primary_binding + input_bindings_lua + "\n        for _, descendant in ipairs(model:GetDescendants()) do", 1)
            generated = generated.replace(
                'local actionName = "ConvertedLegacy_" .. string.upper(keyDefinition.key)',
                'local actionName = "ConvertedLegacy_" .. module.InternalName .. "_" .. string.upper(keyDefinition.key)',
            )
            generated = generated.replace("                UserInputService.TouchEnabled,\n                keyDefinition.code", "                false,\n                keyDefinition.code")
            backend = '''
    local function SetC0C1Joint(joint, c0, c1, scale)
        if not joint then return end
        local transform = c0 * c1:Inverse()
        transform += transform.Position * (scale - 1)
        transform = joint.C0:Inverse() * transform * joint.C1
        joint.Transform = transform
    end

'''
            generated = generated.replace("    local function legacyMain", backend + "    local function legacyMain", 1)
            old_update = '''    module.Update = function(_deltaTime)
        -- Legacy source remains event-driven unless manually adapted.
    end'''
            new_update = '''    module.Update = function(_deltaTime, figure)
        if not figure then return end
        local scale = figure:GetScale()
        for _, joint in ipairs(state.jointProxies or {}) do
            SetC0C1Joint(joint.Reference, joint.C0, joint.C1, scale)
        end
    end'''
            generated = generated.replace(old_update, new_update, 1)
            generated = generated.replace("    module.Destroy = function()", "    module.Destroy = function()\n        state.jointProxies = {}", 1)
            ns.output.parent.mkdir(parents=True, exist_ok=True)
            ns.output.write_text(generated, encoding="utf-8")
            emitted = True
            validations = validate(ns.output, ns.tool_dir)
            validations["semantic_emitter"] = {"ok": True, **rewrite_audit, "golden_output_used": False}
        except Exception as exc:
            validations = {"semantic_emitter": {"ok": False, "diagnostic": str(exc), "golden_output_used": False}}
        hard_static_ok = emitted and validations.get("luau-compile", {}).get("ok") and validations.get("luau-ast", {}).get("ok")
        if hard_static_ok:
            generated_text = ns.output.read_text(encoding="utf-8")
            source_pose_writes = ir.joint_operations.get("C0_writes", 0) + ir.joint_operations.get("C1_writes", 0)
            source_procedural_pose_writes = len(re.findall(
                r"\.\s*C[01]\s*=\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*:\s*)?(?:clerp|lerp)\s*\(",
                source,
                re.I,
            ))
            source_initial_joint_writes = max(0, source_pose_writes - source_procedural_pose_writes)
            all_flush_occurrences = len(re.findall(r"\bSetC0C1Joint\s*\(", generated_text))
            flush_definitions = len(re.findall(
                r"\b(?:local\s+)?function\s+SetC0C1Joint\s*\(", generated_text
            ))
            output_transform_call_sites = max(0, all_flush_occurrences - flush_definitions)
            # Resolve Update call sites and local scale bindings through AST;
            # helper definitions are structurally distinct from call nodes.
            from luau_animation_ast_validator import validate as validate_animation_ast
            output_ast, output_ast_meta = parse_ast(ns.output, ns.tool_dir)
            ast_animation = validate_animation_ast(output_ast) if output_ast else {
                "ok": False, "update_flush_call_sites": 0,
                "valid_scale_flush_call_sites": 0, "calls": [],
            }
            output_transform_call_sites = ast_animation["update_flush_call_sites"]
            scale_flush_call_sites = ast_animation["valid_scale_flush_call_sites"]
            update_match = re.search(
                r"module\s*\.\s*Update\s*=\s*function\s*\([^)]*\)(.*?)\n\s*end",
                generated_text,
                re.S,
            )
            update_body = update_match.group(1) if update_match else ""
            update_effective = re.sub(r"--[^\n]*|\s+", "", update_body)
            update_nonempty = bool(update_effective)
            animation_required = source_procedural_pose_writes > 0
            coverage_ok = (not animation_required) or (
                output_transform_call_sites > 0 and scale_flush_call_sites > 0 and update_nonempty
            )
            validations["animation_backend_coverage"] = {
                "ok": coverage_ok,
                "source_pose_writes_total": source_pose_writes,
                "source_initial_joint_configuration_writes": source_initial_joint_writes,
                "source_procedural_pose_writes": source_procedural_pose_writes,
                "output_flush_occurrences_total": all_flush_occurrences,
                "output_flush_definitions": flush_definitions,
                "output_transform_call_sites": output_transform_call_sites,
                "output_scale_flush_call_sites": scale_flush_call_sites,
                "module_update_nonempty": update_nonempty,
                "ast_origin_validation": {
                    "status": "PARTIAL",
                    "direct_update_containment": ast_animation.get("direct_update_containment", False),
                    "transitive_update_reachability": ast_animation.get("transitive_update_reachability", False),
                    "scale_binding_resolution": True,
                    "scale_receiver_bound_to_update_figure": ast_animation.get("receiver_bound_to_update_figure", False),
                    "argument_arity": True,
                    "joint_and_cframe_type_inference": ast_animation.get("joint_and_cframe_type_inference", False),
                    "strict_origin_ok": ast_animation.get("strict_origin_ok", False),
                    "details": ast_animation,
                },
            }
            if animation_required and output_transform_call_sites == 0:
                blockers.append("NO_UHHHHHH_ANIMATION_BACKEND_COVERAGE: procedural joint poses exist but output has no SetC0C1Joint call sites")
            if animation_required and not update_nonempty:
                blockers.append("EMPTY_UPDATE_WITH_SOURCE_ANIMATION_DRIVER: procedural animation exists but module.Update has no executable body")
            if animation_required and output_transform_call_sites > 0 and scale_flush_call_sites == 0:
                blockers.append("INVALID_UHHHHHH_SCALE_FLUSH: SetC0C1Joint calls are not proven to use figure:GetScale()")
            if animation_required and not ast_animation.get("strict_origin_ok", False):
                blockers.append("UNPROVEN_JOINT_PROXY_TYPES: Reference is not proven Motor6D and C0/C1 are not proven CFrame values")
            if animation_required and not ast_animation.get("r6_joint_coverage_ok", False):
                blockers.append("INCOMPLETE_R6_JOINT_COVERAGE: RootJoint, Neck, shoulders, and hips are not all proven flushed")
            emitter_validation = validations.get("semantic_emitter", {})
            emitted_counts = emitter_validation.get("counts", {})
            extracted_inputs = emitter_validation.get("input_began_bindings", [])
            inputbegan_count = emitted_counts.get("route_inputbegan", 0)
            validations["input_routing_coverage"] = {
                "inputbegan_callbacks_routed": inputbegan_count,
                "enum_branches_extracted": len(extracted_inputs),
                "emitted_context_actions": len([
                    item for item in extracted_inputs
                    if not (item["enum"] == "UserInputType" and item["item"] == "MouseButton1")
                ]),
                "input_began_callbacks_analyzed": emitter_validation.get("input_began_callbacks_analyzed", 0),
                "input_began_callbacks_without_proven_bindings": emitter_validation.get("input_began_callbacks_without_proven_bindings", 0),
                "input_began_callbacks_with_unsupported_branches": emitter_validation.get("input_began_callbacks_with_unsupported_branches", 0),
                "game_processed_parameter_used": emitter_validation.get("game_processed_parameter_used", False),
                "pc_binding_coverage_proven": (
                    inputbegan_count == 0
                    or (emitter_validation.get("input_began_callbacks_analyzed", 0) == inputbegan_count
                        and emitter_validation.get("input_began_callbacks_without_proven_bindings", 0) == 0
                        and emitter_validation.get("input_began_callbacks_with_unsupported_branches", 0) == 0)
                ),
            }
            if inputbegan_count and emitter_validation.get("input_began_callbacks_without_proven_bindings", 0):
                blockers.append("INCOMPLETE_INPUTBEGAN_BINDINGS: At least one routed InputBegan callback has no proven KeyCode/UserInputType ContextAction binding")
            if inputbegan_count and emitter_validation.get("input_began_callbacks_with_unsupported_branches", 0):
                blockers.append("UNPROVEN_INPUTBEGAN_BRANCH_COVERAGE: At least one routed callback contains an input-dependent predicate not covered by exact positive enum bindings")
            if inputbegan_count and emitter_validation.get("game_processed_parameter_used", False):
                blockers.append("UNPRESERVED_GAMEPROCESSED_SEMANTICS: ContextAction routing currently emits gameProcessedEvent=false")
            if (emitted_counts.get("route_keydown", 0) or emitted_counts.get("route_button1down", 0)
                    or inputbegan_count):
                blockers.append("MOBILE_ABILITY_UI_REQUIRED: PC ContextActions were emitted, but a separate mobile ability interface is not yet generated")
            if blockers:
                status = "MANUAL_DEPENDENCY_REQUIRED"
                reason = "Output is diagnostic only; unresolved semantic blockers prohibit runtime candidacy"
            else:
                status, reason = "RUNTIME_TEST_REQUIRED", "AST-addressed R6 adapter emitted code that compiles and parses; runtime acceptance remains unverified"
        else:
            status, reason = "VALIDATION_FAILED", "Semantic adapter emission, compile, or AST validation failed"
    elif ir.family == "legacy_feloadlibrary_r6_combat":
        status, reason = "DETERMINISTIC_ONLY", "Family classified and semantic IR produced; emission requires --experimental-adapter"

    out_hash = hashlib.sha256(ns.output.read_bytes()).hexdigest() if emitted and ns.output else None
    report = Report("3.0", str(ns.input), str(ns.output) if ns.output else None,
                    hashlib.sha256(source.encode()).hexdigest(), status, reason, frontend, ir,
                    findings, blockers, validations, acceptance(), emitted, out_hash)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{status}: {reason}")
    print(f"Family: {ir.family}")
    print(f"Report: {report_path}")
    if emitted:
        print(f"Output: {ns.output}")
    return 2 if status == "VALIDATION_FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
