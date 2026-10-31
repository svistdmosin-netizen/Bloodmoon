from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class Finding:
    level: str
    category: str
    message: str
    lines: list[int] = field(default_factory=list)

@dataclass
class Inventory:
    functions: list[tuple[str, str, int]]
    keys: list[str]
    sounds: list[str]
    meshes: list[str]
    textures: list[str]
    executor_apis: list[tuple[str, int]]
    character_refs: list[int]
    connections: list[int]
    loops: list[int]
    damage_lines: list[int]
    joint_lines: list[int]
    gui_lines: list[int]

def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1

def unique(values):
    result = []
    seen = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result

def matching_lines(text: str, pattern: str, flags=0) -> list[int]:
    return [line_number(text, m.start()) for m in re.finditer(pattern, text, flags)]

def inventory(text: str) -> Inventory:
    functions = []
    function_seen = set()
    patterns = [
        r"^\s*(?:local\s+)?function\s+([A-Za-z_]\w*)\s*\(([^)]*)\)",
        r"^\s*(?:local\s+)?([A-Za-z_]\w*)\s*=\s*function\s*\(([^)]*)\)",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.M):
            item = (m.group(1), m.group(2), line_number(text, m.start()))
            if (item[0], item[2]) not in function_seen:
                functions.append(item)
                function_seen.add((item[0], item[2]))
    functions.sort(key=lambda item: item[2])
    keys = unique(re.findall(r"(?:key|kkk)\s*==\s*['\"]([^'\"]+)['\"]", text, re.I))
    keys += [k for k in unique(re.findall(r"Enum\.KeyCode\.([A-Za-z0-9_]+)", text)) if k not in keys]
    assets = unique(re.findall(r"rbxassetid://(\d+)", text, re.I))
    sounds = unique(re.findall(r"SoundId\s*=\s*['\"][^'\"]*?(\d+)", text, re.I))
    meshes = unique(re.findall(r"MeshId\s*=\s*['\"][^'\"]*?(\d+)", text, re.I))
    textures = unique(re.findall(r"Texture(?:Id|ID)?\s*=\s*['\"][^'\"]*?(\d+)", text, re.I))
    for asset in assets:
        if asset not in sounds and asset not in meshes and asset not in textures:
            textures.append(asset)
    executor_names = [
        "gethui", "getgenv", "getrenv", "getcustomasset", "getsynasset", "readfile",
        "writefile", "isfile", "isfolder", "makefolder", "request", "http_request",
        "loadstring", "hookmetamethod", "sethiddenproperty", "firetouchinterest",
    ]
    executor_apis = []
    for name in executor_names:
        for ln in matching_lines(text, rf"\b{re.escape(name)}\b"):
            executor_apis.append((name, ln))
    return Inventory(
        functions=functions,
        keys=keys,
        sounds=sounds,
        meshes=meshes,
        textures=textures,
        executor_apis=sorted(executor_apis, key=lambda x: x[1]),
        character_refs=matching_lines(text, r"(?:LocalPlayer|player|plr|p)\.Character\b", re.I),
        connections=matching_lines(text, r":(?:connect|Connect)\s*\("),
        loops=matching_lines(text, r"^\s*(?:while\s+true\s+do|repeat\s*$)", re.M),
        damage_lines=matching_lines(text, r"(?:\.Health\s*(?:=|-=)|TakeDamage\s*\(|BreakJoints\s*\()"),
        joint_lines=matching_lines(text, r"(?:Motor6D|RootJoint|Right Shoulder|Left Shoulder|Right Hip|Left Hip)"),
        gui_lines=matching_lines(text, r"Instance\.new\s*\(\s*['\"](?:ScreenGui|TextButton|ImageButton|Frame|TextLabel|TextBox)"),
    )

def remove_simple_character_added(text: str) -> tuple[str, int]:
    patterns = [
        r"\n\s*(?:game\.Players\.LocalPlayer|Players\.LocalPlayer|player|plr|p)\.CharacterAdded\s*:\s*(?:connect|Connect)\s*\(function\([^)]*\).*?\n\s*end\s*\)",
        r"\n\s*(?:game\.Players\.LocalPlayer|Players\.LocalPlayer|player|plr|p)\.CharacterAdded\s*:\s*Wait\s*\(\s*\)",
    ]
    count = 0
    for pattern in patterns:
        text, n = re.subn(pattern, "", text, flags=re.S)
        count += n
    return text, count

def make_functions_forward_safe(text: str) -> tuple[str, list[str]]:
    names = []
    for m in re.finditer(r"^\s*(?:local\s+)?function\s+([A-Za-z_]\w*)\s*\(", text, re.M):
        name = m.group(1)
        if name not in names:
            names.append(name)
    transformed = re.sub(
        r"^(\s*)(?:local\s+)?function\s+([A-Za-z_]\w*)\s*\(",
        r"\1\2 = function(",
        text,
        flags=re.M,
    )
    return transformed, names

def protect_self_targets(text: str) -> tuple[str, int]:
    replacements = [
        (r"(?<![.A-Za-z0-9_])([A-Za-z_]\w*(?:\.Parent)?)\s*~=\s*(?:char|character|figure)(?![.A-Za-z0-9_])", r"not isSelfModel(\1)"),
        (r"(?<![.A-Za-z0-9_])(?:char|character|figure)\s*~=\s*([A-Za-z_]\w*(?:\.Parent)?)(?![.A-Za-z0-9_])", r"not isSelfModel(\1)"),
    ]
    count = 0
    for pattern, replacement in replacements:
        text, n = re.subn(pattern, replacement, text)
        count += n
    return text, count

def transform_source(text: str) -> tuple[str, list[Finding]]:
    findings = []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^\s*(?:workspace|game\.Workspace)\.FilteringEnabled\s*=.*$", "", text, flags=re.M)
    text = re.sub(r"^\s*(?:fe|FilteringEnabled)\s*=\s*(?:workspace|game\.Workspace)\.FilteringEnabled.*$", "", text, flags=re.M)
    text, removed_respawn = remove_simple_character_added(text)
    if removed_respawn:
        findings.append(Finding("info", "respawn", f"Removed {removed_respawn} simple CharacterAdded handler(s)."))
    character_patterns = [
        r"game:GetService\(['\"]Players['\"]\)\.LocalPlayer\.Character",
        r"game\.Players\.LocalPlayer\.Character",
        r"Players\.LocalPlayer\.Character",
        r"LocalPlayer\.Character",
        r"player\.Character",
        r"plr\.Character",
        r"p\.Character",
        r"(?:workspace|game\.Workspace):WaitForChild\s*\(\s*['\"]non['\"]\s*\)",
        r"(?:workspace|game\.Workspace)\.non\b",
    ]
    replaced = 0
    for pattern in character_patterns:
        text, n = re.subn(pattern, "figure", text, flags=re.I)
        replaced += n
    if replaced:
        findings.append(Finding("info", "character", f"Replaced {replaced} direct character reference(s) with figure."))
    text = text.replace(":connect(", ":Connect(")
    text = re.sub(r"(?<![:.A-Za-z0-9_])spawn\s*\(", "task.spawn(", text)
    text = re.sub(r"(?<![:.A-Za-z0-9_])wait\s*\(", "task.wait(", text)
    text, cancellable_loops = re.subn(r"\bwhile\s+true\s+do\b", "while active do", text, flags=re.I)
    if cancellable_loops:
        findings.append(Finding("info", "lifecycle", f"Converted {cancellable_loops} infinite loop(s) to stop when Destroy disables the module."))
    text, external_loads = re.subn(
        r"loadstring\s*\(\s*game:GetObjects\s*\(([^\n]+?)\)\s*\[\s*1\s*\]\.Source\s*\)\s*\(\s*\)",
        r"runExternalSource(\1)",
        text,
        flags=re.I,
    )
    if external_loads:
        findings.append(Finding("warning", "external-source", f"Wrapped {external_loads} external source loader(s) in a guarded compatibility adapter."))
    text, guards = protect_self_targets(text)
    findings.append(Finding("info", "self-protection", f"Rewrote {guards} simple self-target comparison(s)."))
    text, function_names = make_functions_forward_safe(text)
    if function_names:
        findings.append(Finding("info", "functions", f"Predeclared {len(function_names)} function name(s) for forward references."))
    predeclare = "\t\tlocal " + ", ".join(function_names) + "\n" if function_names else ""
    indented = "\n".join("\t\t" + line for line in text.splitlines())
    return predeclare + indented, findings

def lua_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)

def keycode_expression(key: str) -> str | None:
    aliases = {
        " ": "Space", "space": "Space", "ctrl": "LeftControl", "control": "LeftControl",
        "shift": "LeftShift", "alt": "LeftAlt", "enter": "Return", "return": "Return",
    }
    raw = aliases.get(key.lower(), key)
    if len(raw) == 1 and raw.isalpha():
        raw = raw.upper()
    elif len(raw) == 1 and raw.isdigit():
        raw = "One Two Three Four Five Six Seven Eight Nine Zero".split()[int(raw) - 1] if raw != "0" else "Zero"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", raw):
        return None
    return "Enum.KeyCode." + raw

def make_scaffold(name: str, description: str, internal_name: str, inv: Inventory) -> tuple[str, list[Finding]]:
    bindings = []
    action_names = []
    positions = [
        (-130, -130), (-190, -130), (-250, -130), (-310, -130), (-370, -130),
        (-130, -190), (-190, -190), (-250, -190), (-310, -190), (-370, -190),
        (-430, -130), (-430, -190),
    ]
    for index, key in enumerate(inv.keys):
        expression = keycode_expression(key)
        if not expression:
            continue
        safe = re.sub(r"[^A-Za-z0-9_]", "_", key)
        action = internal_name + "_" + safe
        x, y = positions[index % len(positions)]
        action_names.append(action)
        bindings.append(
            f'\t\tbind({lua_quote(action)}, {lua_quote(key.upper())}, UDim2.new(1, {x}, 1, {y}), {expression}, function() dispatch({lua_quote(key.lower())}) end)'
        )
    function_map = "\n".join(
        f"\t\t[{lua_quote(fn)}] = {line}," for fn, _, line in inv.functions
    )
    if not function_map:
        function_map = "\t\t"
    action_table = ", ".join(lua_quote(value) for value in action_names)
    binding_code = "\n".join(bindings)
    result = f'''local modules = {{}}

table.insert(modules, function()
\tlocal m = {{}}
\tm.ModuleType = "MOVESET"
\tm.Name = {lua_quote(name)}
\tm.Description = {lua_quote(description)}
\tm.InternalName = {lua_quote(internal_name)}
\tm.Assets = {{}}
\tm.SourceFunctionLines = {{
{function_map}
\t}}

\tm.Config = function(parent)
\tend

\tlocal figure
\tlocal runtime
\tlocal active = false
\tlocal connections = {{}}
\tlocal hatOverrides = {{}}
\tlocal actions = {{ {action_table} }}
\tlocal handlers = {{}}

\tlocal function keep(connection)
\t\tconnections[#connections + 1] = connection
\t\treturn connection
\tend

\tlocal function bind(name, title, position, key, callback)
\t\tContextActions:BindAction(name, function(_, state, input)
\t\t\tif active and state == Enum.UserInputState.Begin then
\t\t\t\tcallback(input)
\t\t\tend
\t\tend, true, key)
\t\tContextActions:SetTitle(name, title)
\t\tContextActions:SetPosition(name, position)
\tend

\tlocal function dispatch(key)
\t\tlocal handler = handlers[key]
\t\tif handler then handler() end
\tend

\tlocal function registerHatOverride(data)
\t\ttable.insert(HatReanimator.HatCFrameOverride, data)
\t\thatOverrides[#hatOverrides + 1] = data
\t\treturn data
\tend

\tlocal function isSelfModel(model)
\t\tif not model then return true end
\t\tlocal player = game:GetService("Players").LocalPlayer
\t\tif model == figure or model == player.Character then return true end
\t\tif figure and model:IsDescendantOf(figure) then return true end
\t\tif player.Character and model:IsDescendantOf(player.Character) then return true end
\t\tlocal state = _G.Uhhhhhh
\t\tif type(state) == "table" and state.Character then
\t\t\tif model == state.Character or model:IsDescendantOf(state.Character) then return true end
\t\tend
\t\treturn false
\tend

\tm.Init = function(character)
\t\tfigure = character
\t\tactive = true
\t\truntime = Instance.new("Folder")
\t\truntime.Name = {lua_quote(internal_name + "_Runtime")}
\t\truntime.Parent = figure
{binding_code}
\tend

\tm.Update = function(dt, character)
\tend

\tm.Destroy = function(character)
\t\tactive = false
\t\tfor _, name in ipairs(actions) do
\t\t\tContextActions:UnbindAction(name)
\t\tend
\t\tfor _, connection in ipairs(connections) do
\t\t\tpcall(function() connection:Disconnect() end)
\t\tend
\t\ttable.clear(connections)
\t\tif HatReanimator and type(HatReanimator.HatCFrameOverride) == "table" then
\t\t\tfor _, ref in ipairs(hatOverrides) do
\t\t\t\tfor index = #HatReanimator.HatCFrameOverride, 1, -1 do
\t\t\t\t\tif HatReanimator.HatCFrameOverride[index] == ref then
\t\t\t\t\t\ttable.remove(HatReanimator.HatCFrameOverride, index)
\t\t\t\t\tend
\t\t\t\tend
\t\t\tend
\t\tend
\t\ttable.clear(hatOverrides)
\t\tif runtime then runtime:Destroy() end
\t\truntime = nil
\t\tfigure = nil
\tend

\treturn m
end)

return modules
'''
    findings = [
        Finding("info", "scaffold", "Generated a safe canonical hub module without executing the standalone source."),
        Finding("warning", "manual-port", f"Detected {len(inv.functions)} source function(s); their bodies are intentionally not inserted automatically."),
        Finding("info", "input", f"Generated {len(bindings)} ContextActions binding(s) from statically detected keys."),
    ]
    return result, findings

def is_existing_hub_module(source: str) -> bool:
    has_type = re.search(r"\.ModuleType\s*=\s*['\"]MOVESET['\"]", source) is not None
    has_return = re.search(r"\breturn\s+modules\b", source) is not None
    has_contract = all(re.search(rf"\.(?:{name})\s*=\s*function", source) for name in ("Init", "Update", "Destroy"))
    return has_type and has_return and has_contract

def make_module(source: str, name: str, description: str, internal_name: str) -> tuple[str, list[Finding]]:
    if is_existing_hub_module(source):
        normalized = source.replace("\r\n", "\n").replace("\r", "\n")
        return normalized, [Finding("info", "module-detection", "Input already implements the hub moveset contract; preserved it instead of nesting it inside another module.")]
    transformed, findings = transform_source(source)
    background = "\n".join("\t" + line for line in transformed.splitlines())
    result = f'''local modules = {{}}

table.insert(modules, function()
\tlocal m = {{}}
\tm.ModuleType = "MOVESET"
\tm.Name = {lua_quote(name)}
\tm.Description = {lua_quote(description)}
\tm.InternalName = {lua_quote(internal_name)}
\tm.Assets = {{}}

\tm.Config = function(parent)
\tend

\tlocal Players = game:GetService("Players")
\tlocal Player = Players.LocalPlayer
\tlocal figureRef
\tlocal runtime
\tlocal tracked = {{}}
\tlocal swordRefs = {{}}
\tlocal active = false

\tlocal function track(connection)
\t\ttracked[#tracked + 1] = connection
\t\treturn connection
\tend

\tlocal function isSelfModel(model)
\t\tif not model then return true end
\t\tif model == figureRef or model == Player.Character then return true end
\t\tif figureRef and model:IsDescendantOf(figureRef) then return true end
\t\tif Player.Character and model:IsDescendantOf(Player.Character) then return true end
\t\tlocal state = _G.Uhhhhhh
\t\tif type(state) == "table" and state.Character then
\t\t\tif model == state.Character or model:IsDescendantOf(state.Character) then return true end
\t\tend
\t\treturn false
\tend

\tlocal function runExternalSource(asset)
\t\tif type(loadstring) ~= "function" then
\t\t\twarn("Bloodmoon: external source requires loadstring", asset)
\t\t\treturn false
\t\tend
\t\tlocal ok, result = pcall(function()
\t\t\tlocal objects = game:GetObjects(asset)
\t\t\tlocal object = objects and objects[1]
\t\t\tif not object or type(object.Source) ~= "string" then error("source asset unavailable") end
\t\t\tlocal chunk, compileError = loadstring(object.Source)
\t\t\tif not chunk then error(compileError or "external source compile failed") end
\t\t\treturn chunk()
\t\tend)
\t\tif not ok then warn("Bloodmoon external dependency failed:", result) end
\t\treturn ok
\tend

\tm.Init = function(figure)
\t\tfigureRef = figure
\t\tactive = true
\t\truntime = Instance.new("Folder")
\t\truntime.Name = {lua_quote(internal_name + "_Runtime")}
\t\truntime.Parent = figure
\t\ttask.spawn(function()
{background}
\t\tend)
\tend

\tm.Update = function(dt, figure)
\tend

\tm.Destroy = function(figure)
\t\tactive = false
\t\tfor _, connection in ipairs(tracked) do
\t\t\tpcall(function() connection:Disconnect() end)
\t\tend
\t\ttable.clear(tracked)
\t\tif HatReanimator and type(HatReanimator.HatCFrameOverride) == "table" then
\t\t\tfor _, ref in ipairs(swordRefs) do
\t\t\t\tfor index = #HatReanimator.HatCFrameOverride, 1, -1 do
\t\t\t\t\tif HatReanimator.HatCFrameOverride[index] == ref then
\t\t\t\t\t\ttable.remove(HatReanimator.HatCFrameOverride, index)
\t\t\t\t\tend
\t\t\t\tend
\t\t\tend
\t\tend
\t\ttable.clear(swordRefs)
\t\tif runtime then runtime:Destroy() end
\t\truntime = nil
\t\tfigureRef = nil
\tend

\treturn m
end)

return modules
'''
    return result, findings

def find_tool(name: str, explicit: str | None, roots: list[Path]) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    for root in roots:
        candidate = root / name
        if candidate.exists():
            return candidate
    return None

def run_tool(path: Path | None, source: Path, args: list[str]) -> tuple[str, str]:
    if not path:
        return "not-run", "tool not found"
    try:
        proc = subprocess.run([str(path), *args, str(source)], capture_output=True, timeout=120)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        detail = (stdout + stderr).strip()
        if proc.returncode == 0:
            return "pass", ""
        return "fail", detail
    except Exception as exc:
        return "error", str(exc)

def make_report(source_path: Path, output_path: Path, inv: Inventory, findings: list[Finding], checks: dict) -> str:
    lines = [
        "# Moveset conversion report", "",
        f"- Source: `{source_path}`",
        f"- Output: `{output_path}`",
        f"- Source MD5: `{hashlib.md5(source_path.read_bytes()).hexdigest()}`",
        f"- Output MD5: `{hashlib.md5(output_path.read_bytes()).hexdigest()}`",
        "",
        "## Checks", "",
    ]
    for name, value in checks.items():
        lines.append(f"- {name}: **{value[0]}**" + (f" — `{value[1][:300]}`" if value[1] else ""))
    lines += ["", "## Inventory", "", f"### Functions ({len(inv.functions)})", ""]
    lines += [f"- `{name}({args})` — line {line}" for name, args, line in inv.functions] or ["- None detected"]
    lines += ["", "### Input", "", "- " + (", ".join(inv.keys) if inv.keys else "No static keys detected")]
    lines += ["", "### Assets", "", f"- Sounds: {', '.join(inv.sounds) or 'none detected'}", f"- Meshes: {', '.join(inv.meshes) or 'none detected'}", f"- Textures/other IDs: {', '.join(inv.textures) or 'none detected'}"]
    lines += ["", "### Risk locations", "", f"- Direct character references: {inv.character_refs}", f"- Connections: {inv.connections}", f"- Infinite/repeat loops: {inv.loops}", f"- Damage/destruction: {inv.damage_lines}", f"- Joint manipulation: {inv.joint_lines}", f"- GUI construction: {inv.gui_lines}"]
    lines += ["", "### Executor dependencies", ""]
    lines += [f"- `{name}` — line {line}" for name, line in inv.executor_apis] or ["- None detected"]
    lines += ["", "## Automatic transformations", ""]
    lines += [f"- **{f.level} / {f.category}:** {f.message}" for f in findings] or ["- None"]
    lines += [
        "", "## Manual review required", "",
        "- Verify every ability in the actual hub; compile/AST checks do not validate runtime behavior.",
        "- Replace original mouse/key handlers with `ContextActions` where mobile controls are required.",
        "- Route only intentional hit phases to `ReanimateFling`.",
        "- Register connections through `track(...)` or disconnect them manually in `Destroy`.",
        "- Parent temporary instances to `runtime` or destroy them explicitly.",
        "- Restore every modified Motor6D, Humanoid property, camera property and Animate state.",
        "- Configure Sword/Gun/Bullet `HatCFrameOverride` entries manually.",
        "- Inspect all damage lines to ensure `isSelfModel` protects both real and reanimated characters.",
        "- Remove or rewrite executor-only APIs listed above.",
    ]
    return "\n".join(lines) + "\n"

def main() -> int:
    parser = argparse.ArgumentParser(description="Create a conservative BlaaBlaa Reanimate Hub moveset draft from a standalone Luau script.")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--description", default="Converted standalone moveset")
    parser.add_argument("--internal-name")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--compile-tool")
    parser.add_argument("--ast-tool")
    parser.add_argument("--mode", choices=("scaffold", "legacy-wrap"), default="scaffold")
    args = parser.parse_args()

    if not args.input.exists():
        parser.error(f"input does not exist: {args.input}")
    output = args.output or args.input.with_name(args.input.stem + ".moveset.lua")
    report = args.report or output.with_suffix(output.suffix + ".report.md")
    name = args.name or args.input.stem
    internal_name = args.internal_name or re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "ConvertedMoveset"
    source = args.input.read_text(encoding="utf-8", errors="ignore")
    inv = inventory(source)
    if is_existing_hub_module(source):
        converted, findings = make_module(source, name, args.description, internal_name)
    elif args.mode == "legacy-wrap":
        converted, findings = make_module(source, name, args.description, internal_name)
        findings.append(Finding("warning", "legacy-wrap", "Standalone source was mechanically wrapped inside Init; runtime safety is not guaranteed."))
    else:
        converted, findings = make_scaffold(name, args.description, internal_name, inv)

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        backup = output.with_suffix(output.suffix + ".bak")
        shutil.copy2(output, backup)
    output.write_text(converted, encoding="utf-8")

    roots = [Path.cwd() / ".tools", Path(__file__).resolve().parent / ".tools"]
    compile_tool = find_tool("luau-compile", args.compile_tool, roots)
    ast_tool = find_tool("luau-ast", args.ast_tool, roots)
    checks = {
        "luau-compile": run_tool(compile_tool, output, ["--binary"]),
        "luau-ast": run_tool(ast_tool, output, []),
    }
    report.write_text(make_report(args.input, output, inv, findings, checks), encoding="utf-8")

    print(f"Output: {output}")
    print(f"Report: {report}")
    print(f"Functions: {len(inv.functions)} | Keys: {len(inv.keys)} | Assets: {len(set(inv.sounds + inv.meshes + inv.textures))}")
    for check, result in checks.items():
        print(f"{check}: {result[0]}")
    print("This is a conversion draft. Runtime and manual-review items are listed in the report.")
    return 0 if all(result[0] in {"pass", "not-run"} for result in checks.values()) else 2

if __name__ == "__main__":
    raise SystemExit(main())
