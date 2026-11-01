#!/usr/bin/env python3
"""Best-effort Luau script -> Reanimation Hub moveset converter.

The converter does not claim that arbitrary Luau can be made semantically correct
without testing. It creates the hub contract, embeds the original source without
minifying it, and emits a JSON conversion report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ConversionReport:
    input_file: str
    output_file: str
    mode: str
    module_name: str
    input_lines: int
    output_lines: int = 0
    external_code_preserved: bool = True
    detected: dict[str, int] = field(default_factory=dict)
    rewrites: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


PATTERNS = {
    "loadstring": r"\bloadstring\s*\(",
    "http_get": r"(?:\bHttpGet\s*\(|:\s*HttpGet\s*\()",
    "request": r"\b(?:request|http_request)\s*\(",
    "get_objects": r":\s*GetObjects\s*\(",
    "character_added_wait": r"CharacterAdded\s*:\s*Wait\s*\(",
    "local_player_character": r"LocalPlayer\s*\.\s*Character",
    "script_reference": r"\bscript\b",
    "input_connection": r"(?:InputBegan|InputEnded|KeyDown|Button1Down)\s*:\s*Connect\s*\(",
    "generic_connection": r"\bConnect\s*\(",
    "while_loop": r"\bwhile\b.+\bdo\b",
    "repeat_loop": r"\brepeat\b",
    "break_joints": r"\bBreakJoints\s*\(",
    "destroy_character": r"(?:Character|character|char|CharacterModel)\s*:\s*Destroy\s*\(",
    "return_statement": r"(?m)^\s*return\b",
}


REWRITE_RULES = [
    (
        "game_players_character",
        re.compile(r"\bgame\s*\.\s*Players\s*\.\s*LocalPlayer\s*\.\s*Character\b"),
        "Character",
    ),
    (
        "players_character",
        re.compile(r"\bPlayers\s*\.\s*LocalPlayer\s*\.\s*Character\b"),
        "Character",
    ),
    (
        "player_character",
        re.compile(r"\bLocalPlayer\s*\.\s*Character\b"),
        "Character",
    ),
    (
        "getservice_players_character",
        re.compile(
            r"\bgame\s*:\s*GetService\s*\(\s*([\"'])Players\1\s*\)"
            r"\s*\.\s*LocalPlayer\s*\.\s*Character\b"
        ),
        "Character",
    ),
    (
        "character_added_wait",
        re.compile(
            r"(?:\bgame\s*\.\s*Players\s*\.\s*LocalPlayer|"
            r"\bPlayers\s*\.\s*LocalPlayer|\bLocalPlayer)"
            r"\s*\.\s*CharacterAdded\s*:\s*Wait\s*\(\s*\)"
        ),
        "Character",
    ),
    (
        "script_parent",
        re.compile(r"\bscript\s*\.\s*Parent\b"),
        "Character",
    ),
    (
        "breakjoints_suppressed",
        re.compile(r"\b(?:Character|character|char|chr|model)\s*:\s*BreakJoints\s*\(\s*\)"),
        'warn("[Converted Moveset] BreakJoints was suppressed")',
    ),
    (
        "character_destroy_suppressed",
        re.compile(r"\b(?:Character|character|char|chr|model)\s*:\s*Destroy\s*\(\s*\)"),
        'warn("[Converted Moveset] Character destruction was suppressed")',
    ),
    (
        "infinite_loop_guard",
        re.compile(r"\bwhile\s+true\s+do\b"),
        "while IsActive() do",
    ),
    (
        "boolean_loop_guard",
        re.compile(r"\bwhile\s+([A-Za-z_][A-Za-z0-9_]*)\s+do\b"),
        r"while IsActive() and (\1) do",
    ),
]


def split_luau_segments(source: str) -> list[tuple[str, bool]]:
    """Split source into code and protected string/comment segments.

    The second tuple item is True for code. Rewrites only touch code segments.
    Supports line comments, long comments/strings with equals delimiters, quoted
    strings, and backtick strings. It is intentionally conservative.
    """

    result: list[tuple[str, bool]] = []
    code_start = 0
    index = 0
    length = len(source)

    def flush_code(end: int) -> None:
        nonlocal code_start
        if end > code_start:
            result.append((source[code_start:end], True))

    while index < length:
        if source.startswith("--", index):
            long_match = re.match(r"--\[(=*)\[", source[index:])
            flush_code(index)
            if long_match:
                equals = long_match.group(1)
                close = "]" + equals + "]"
                end = source.find(close, index + long_match.end())
                end = length if end < 0 else end + len(close)
            else:
                end = source.find("\n", index)
                end = length if end < 0 else end + 1
            result.append((source[index:end], False))
            index = end
            code_start = index
            continue

        long_match = re.match(r"\[(=*)\[", source[index:])
        if long_match:
            flush_code(index)
            equals = long_match.group(1)
            close = "]" + equals + "]"
            end = source.find(close, index + long_match.end())
            end = length if end < 0 else end + len(close)
            result.append((source[index:end], False))
            index = end
            code_start = index
            continue

        character = source[index]
        if character in ('"', "'", "`"):
            flush_code(index)
            quote = character
            cursor = index + 1
            escaped = False
            while cursor < length:
                current = source[cursor]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    cursor += 1
                    break
                cursor += 1
            result.append((source[index:cursor], False))
            index = cursor
            code_start = index
            continue

        index += 1

    flush_code(length)
    return result


def wrap_statement_connections(source: str) -> tuple[str, int]:
    """Wrap line-start `signal:Connect(...)` statements with TrackConnection.

    This handles the dominant legacy pattern, including multiline anonymous
    callbacks. Assignments and nested expressions are deliberately left alone
    and reported for manual review rather than being rewritten unsafely.
    """

    code_mask = [False] * len(source)
    cursor = 0
    for segment, is_code in split_luau_segments(source):
        if is_code:
            for index in range(cursor, cursor + len(segment)):
                code_mask[index] = True
        cursor += len(segment)

    insertions: list[tuple[int, str]] = []
    occupied_until = -1
    connection_pattern = re.compile(r":\s*connect\s*\(", re.IGNORECASE)

    for match in connection_pattern.finditer(source):
        if match.start() < occupied_until:
            continue
        if not all(code_mask[index] for index in range(match.start(), match.end())):
            continue

        line_start = source.rfind("\n", 0, match.start()) + 1
        expression_start = line_start
        while expression_start < match.start() and source[expression_start] in " \t":
            expression_start += 1
        prefix = source[expression_start:match.start()]
        if not prefix or "=" in prefix or prefix.lstrip().startswith(("return ", "TrackConnection")):
            continue

        open_parenthesis = source.find("(", match.start(), match.end() + 1)
        if open_parenthesis < 0:
            continue
        depth = 1
        index = open_parenthesis + 1
        while index < len(source) and depth:
            if code_mask[index]:
                if source[index] == "(":
                    depth += 1
                elif source[index] == ")":
                    depth -= 1
            index += 1
        if depth != 0:
            continue

        close_parenthesis = index - 1
        insertions.append((expression_start, "TrackConnection("))
        insertions.append((close_parenthesis + 1, ")"))
        occupied_until = close_parenthesis + 1

    for position, text in sorted(insertions, reverse=True):
        source = source[:position] + text + source[position:]
    return source, len(insertions) // 2


def aggressive_rewrite(source: str) -> tuple[str, dict[str, int]]:
    rewrites: dict[str, int] = {}
    transformed: list[str] = []

    for segment, is_code in split_luau_segments(source):
        if not is_code:
            transformed.append(segment)
            continue

        for name, pattern, replacement in REWRITE_RULES:
            segment, count = pattern.subn(replacement, segment)
            if count:
                rewrites[name] = rewrites.get(name, 0) + count
        transformed.append(segment)

    result = "".join(transformed)

    # Prevent the legacy source from shadowing the hub-supplied Character with
    # a workspace lookup such as PlayerName_Fake or PlayerName.
    character_redeclaration = re.compile(
        r"(?mi)^\s*local\s+Character\s*=\s*workspace\s*:\s*FindFirstChild\s*\([^\n]+\)"
        r"\s*or\s*workspace\s*:\s*FindFirstChild\s*\([^\n]+\)\s*$"
    )
    result, count = character_redeclaration.subn(
        "        Character = CharacterModel -- hub-supplied model retained",
        result,
    )
    if count:
        rewrites["character_workspace_shadow_suppressed"] = count

    # A common morph template assumes Torso.roblox exists. Preserve the intent
    # when it does, but do not terminate the entire initialization when absent.
    torso_roblox_assignment = re.compile(
        r"(?mi)^(\s*)([^\n]+)\.roblox\.Transparency\s*=\s*([^\n]+)$"
    )
    result, count = torso_roblox_assignment.subn(
        r'\1local __roblox = \2:FindFirstChild("roblox"); if __roblox then __roblox.Transparency = \3 end',
        result,
    )
    if count:
        rewrites["optional_roblox_member_guarded"] = count

    # Route legacy keyboard callbacks through a generated BindableEvent. The
    # wrapper binds the discovered keys through ContextActionService, producing
    # mobile buttons only on touch devices.
    result, count = re.subn(
        r"\b(?:mouse|Mouse)\s*\.\s*KeyDown\b",
        "LegacyKeySignal.Event",
        result,
        flags=re.IGNORECASE,
    )
    if count:
        rewrites["legacy_keydown_routed"] = count

    # Replace common CloneCharacter aliases with the model supplied by Init.
    # These patterns include quoted property access and therefore run after the
    # protected-segment rewrite pass.
    clone_rules = [
        re.compile(
            r"game\s*:\s*GetService\s*\(\s*([\"'])Workspace\1\s*\)"
            r"\s*\.\s*CloneCharacter\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bworkspace\s*\.\s*CloneCharacter\b", re.IGNORECASE),
        re.compile(
            r"\bworkspace\s*\[\s*([\"'])CloneCharacter\1\s*\]",
            re.IGNORECASE,
        ),
    ]
    clone_count = 0
    for pattern in clone_rules:
        result, count = pattern.subn("Character", result)
        clone_count += count
    if clone_count:
        rewrites["clone_character_aliases"] = clone_count

    # If the script explicitly depends on CloneCharacter, a GetObjects/loadstring
    # immediately surrounding that dependency is a reanimation bootstrap. The
    # hub already supplies the model, so suppress it without asking an LLM.
    if re.search(r"CloneCharacter", source, re.IGNORECASE):
        generic_reanimation_loader = re.compile(
            r"loadstring\s*\(\s*game\s*:\s*GetObjects\s*\(\s*"
            r"([\"'])rbxassetid://[0-9]+\1\s*\)\s*\[\s*1\s*\]"
            r"\s*\.\s*Source\s*\)\s*\(\s*\)",
            re.IGNORECASE,
        )
        result, count = generic_reanimation_loader.subn(
            'warn("[Converted Moveset] CloneCharacter reanimation bootstrap suppressed")',
            result,
        )
        if count:
            rewrites["clone_character_loaders_suppressed"] = count

    # Suppress explicitly named fling BodyVelocity scaffolds and their direct
    # property assignments. This is deterministic and intentionally narrow so
    # ordinary projectile BodyVelocity objects remain untouched.
    fling_variables = set(
        re.findall(
            r"(?mi)^\s*(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*fling[A-Za-z0-9_]*)"
            r"\s*=\s*Instance\.new\s*\(\s*([\"'])BodyVelocity\2",
            result,
        )
    )
    # re.findall includes the quote capture; retain variable names only.
    fling_names = {item[0] if isinstance(item, tuple) else item for item in fling_variables}
    fling_count = 0
    for variable in fling_names:
        creation = re.compile(
            rf"(?mi)^(\s*)(?:local\s+)?{re.escape(variable)}\s*=\s*"
            r"Instance\.new\s*\([^\n]+\)\s*$"
        )
        result, count = creation.subn(
            rf'\1local {variable} = nil -- fling BodyVelocity suppressed',
            result,
        )
        fling_count += count
        assignment = re.compile(
            rf"(?mi)^\s*{re.escape(variable)}\s*\.\s*"
            r"(?:Velocity|MaxForce|P)\s*=\s*[^\n]+$"
        )
        result, count = assignment.subn(
            '-- fling BodyVelocity assignment suppressed',
            result,
        )
        fling_count += count
    if fling_count:
        rewrites["fling_scaffold_suppressed"] = fling_count

    # These two public assets are a widely copied legacy reanimation/death-bypass
    # bootstrap. A hub already supplies the reanimated model, so running them is
    # both redundant and destructive to the hub lifecycle.
    known_loader = re.compile(
        r"loadstring\s*\(\s*game\s*:\s*GetObjects\s*\(\s*"
        r"([\"'])rbxassetid://(?:5209815302|5325226148)\1\s*\)"
        r"\s*\[\s*1\s*\]\s*\.\s*Source\s*\)\s*\(\s*\)",
        re.IGNORECASE,
    )
    result, loader_count = known_loader.subn(
        'warn("[Converted Moveset] legacy reanimation bootstrap suppressed")',
        result,
    )
    if loader_count:
        rewrites["legacy_reanimation_loaders_suppressed"] = loader_count

    result, library_count = re.subn(
        r"(?m)^(\s*)FELOADLIBRARY\s*=\s*\{\s*\}\s*$",
        r"\1FELOADLIBRARY = ConversionScope.FELOADLIBRARY",
        result,
    )
    if library_count:
        rewrites["feloadlibrary_compatibility"] = library_count

    result, connection_count = wrap_statement_connections(result)
    if connection_count:
        rewrites["statement_connections_tracked"] = connection_count
    return result, rewrites


def count_patterns(source: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, pattern in PATTERNS.items():
        value = len(re.findall(pattern, source, flags=re.IGNORECASE | re.MULTILINE))
        if value:
            counts[name] = value
    return counts


def indent_source(source: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else "" for line in source.splitlines())


def luau_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def create_wrapper(source: str, name: str, internal_name: str, description: str, mode: str) -> str:
    embedded = indent_source(source.rstrip() + "\n", 12)

    discovered_keys = sorted(
        {
            match.lower()
            for match in re.findall(
                r"\b(?:key|k)\s*==\s*[\"']([A-Za-z0-9])[\"']",
                source,
                flags=re.IGNORECASE,
            )
        }
    )
    keycode_names = {
        "0": "Zero",
        "1": "One",
        "2": "Two",
        "3": "Three",
        "4": "Four",
        "5": "Five",
        "6": "Six",
        "7": "Seven",
        "8": "Eight",
        "9": "Nine",
    }
    key_rows = []
    for key in discovered_keys:
        enum_name = keycode_names.get(key, key.upper())
        key_rows.append(f'        {{ key = {luau_quote(key)}, code = Enum.KeyCode.{enum_name} }},')
    legacy_key_rows = "\n".join(key_rows)

    return f'''-- Generated by luau_moveset_converter.py
-- Mode: {mode}
-- The original source is embedded below without minification.
-- Runtime behavior still requires testing in the target Reanimation Hub.

local modules = {{}}

local function AddModule(factory)
    table.insert(modules, factory)
end

AddModule(function()
    local module = {{}}

    module.ModuleType = "MOVESET"
    module.Name = {luau_quote(name)}
    module.InternalName = {luau_quote(internal_name)}
    module.Description = {luau_quote(description)}
    module.Assets = {{}}
    module.ConversionMode = {luau_quote(mode)}
    module._Active = false

    local Players = game:GetService("Players")
    local RunService = game:GetService("RunService")
    local ContextActionService = game:GetService("ContextActionService")
    local UserInputService = game:GetService("UserInputService")
    local SoundService = game:GetService("SoundService")

    local player = Players.LocalPlayer
    local actions = ContextActions or ContextActionService

    local state = {{}}
    local LEGACY_KEYS = {{
{legacy_key_rows}
    }}
    local trackedConnections = {{}}
    local trackedInstances = {{}}
    local trackedActions = {{}}
    local originalJoints = {{}}

    local function trackConnection(connection)
        if typeof(connection) == "RBXScriptConnection" then
            table.insert(trackedConnections, connection)
        end
        return connection
    end

    local function trackInstance(instance)
        if typeof(instance) == "Instance" then
            table.insert(trackedInstances, instance)
        end
        return instance
    end

    local function bindAction(name, callback, createTouchButton, ...)
        actions:BindAction(name, callback, createTouchButton, ...)
        table.insert(trackedActions, name)
    end

    local function saveJoint(joint)
        if joint and joint:IsA("Motor6D") and not originalJoints[joint] then
            originalJoints[joint] = {{
                Name = joint.Name,
                Parent = joint.Parent,
                Part0 = joint.Part0,
                Part1 = joint.Part1,
                C0 = joint.C0,
                C1 = joint.C1,
                Transform = joint.Transform,
            }}
        end
        return joint
    end

    local function legacyCreateFactory(className)
        return function(properties)
            properties = properties or {{}}
            local object = nil

            if className == "Motor" or className == "Motor6D" then
                local part0 = properties.Part0
                local part1 = properties.Part1
                if typeof(part0) == "Instance" and typeof(part1) == "Instance" then
                    for _, candidate in ipairs(part0:GetChildren()) do
                        if candidate:IsA("Motor6D")
                            and candidate.Part0 == part0
                            and candidate.Part1 == part1
                        then
                            object = candidate
                            break
                        end
                    end
                end
            end

            if not object then
                object = Instance.new(className == "Motor" and "Motor6D" or className)
            elseif object:IsA("Motor6D") then
                saveJoint(object)
            end

            for key, value in pairs(properties) do
                if type(key) == "number" then
                    value.Parent = object
                else
                    object[key] = value
                end
            end
            return object
        end
    end

    local compatibilityLibrary = {{
        Create = legacyCreateFactory,
    }}

    local function restoreJoints()
        for joint, saved in pairs(originalJoints) do
            if joint and joint.Parent then
                pcall(function()
                    joint.Name = saved.Name
                    joint.Parent = saved.Parent
                    joint.Part0 = saved.Part0
                    joint.Part1 = saved.Part1
                    joint.C0 = saved.C0
                    joint.C1 = saved.C1
                    joint.Transform = saved.Transform
                end)
            end
        end
        table.clear(originalJoints)
    end

    local function disconnectTracked()
        for _, connection in ipairs(trackedConnections) do
            pcall(function()
                connection:Disconnect()
            end)
        end
        table.clear(trackedConnections)
    end

    local function destroyTracked()
        for index = #trackedInstances, 1, -1 do
            local instance = trackedInstances[index]
            if instance and instance.Parent then
                pcall(function()
                    instance:Destroy()
                end)
            end
        end
        table.clear(trackedInstances)
    end

    local function unbindTracked()
        for _, actionName in ipairs(trackedActions) do
            pcall(function()
                actions:UnbindAction(actionName)
            end)
        end
        table.clear(trackedActions)
    end

    local function legacyMain(CharacterModel, ConversionScope, ...)
        local Character = CharacterModel
        local character = CharacterModel
        local char = CharacterModel
        local chr = CharacterModel
        local model = CharacterModel

        local Humanoid = CharacterModel:FindFirstChildOfClass("Humanoid")
        local humanoid = Humanoid
        local RootPart = CharacterModel:FindFirstChild("HumanoidRootPart")
        local HumanoidRootPart = RootPart
        local Torso = CharacterModel:FindFirstChild("Torso")
        local Head = CharacterModel:FindFirstChild("Head")
        local RightArm = CharacterModel:FindFirstChild("Right Arm")
        local LeftArm = CharacterModel:FindFirstChild("Left Arm")
        local RightLeg = CharacterModel:FindFirstChild("Right Leg")
        local LeftLeg = CharacterModel:FindFirstChild("Left Leg")
        local Mouse = player:GetMouse()
        local mouse = Mouse

        local TrackConnection = ConversionScope.TrackConnection
        local TrackInstance = ConversionScope.TrackInstance
        local BindAction = ConversionScope.BindAction
        local SaveJoint = ConversionScope.SaveJoint
        local RuntimeFolder = ConversionScope.RuntimeFolder
        local IsActive = ConversionScope.IsActive
        local FELOADLIBRARY = ConversionScope.FELOADLIBRARY
        local LegacyKeySignal = ConversionScope.LegacyKeySignal

        -- Legacy scripts commonly create BindableEvents under script and call
        -- script:WaitForChild(). Keep those objects isolated from the hub.
        local script = RuntimeFolder

        -- BEGIN ORIGINAL SOURCE
{embedded}        -- END ORIGINAL SOURCE
    end

    module.Config = function(parent: GuiBase2d)
        if type(Util_CreateLabel) == "function" then
            Util_CreateLabel(parent, "Converted legacy script ({mode} mode)")
        end
    end

    module.LoadConfig = function(_save)
        -- Generated wrapper has no script-specific settings.
    end

    module.SaveConfig = function()
        return {{
            ConversionMode = module.ConversionMode,
        }}
    end

    module.Destroy = function()
        module._Active = false

        if state.legacyThread then
            local thread = state.legacyThread
            state.legacyThread = nil
            pcall(function()
                task.cancel(thread)
            end)
        end

        unbindTracked()
        disconnectTracked()
        restoreJoints()
        destroyTracked()

        if state.runtimeFolder then
            state.runtimeFolder:Destroy()
        end
        state = {{}}
    end

    module.Init = function(model)
        module.Destroy()

        if typeof(model) ~= "Instance" or not model:IsA("Model") then
            warn("[Converted Moveset] Init expected a Model")
            return false
        end

        local humanoid = model:FindFirstChildOfClass("Humanoid")
        local root = model:FindFirstChild("HumanoidRootPart")
        if not humanoid or not root then
            warn("[Converted Moveset] Required Humanoid or HumanoidRootPart is missing")
            return false
        end

        state.model = model
        state.humanoid = humanoid
        state.root = root
        state.runtimeFolder = Instance.new("Folder")
        state.runtimeFolder.Name = "ConvertedMovesetRuntime"
        state.runtimeFolder.Parent = model
        state.legacyKeySignal = Instance.new("BindableEvent")
        state.legacyKeySignal.Name = "LegacyKeySignal"
        state.legacyKeySignal.Parent = state.runtimeFolder
        module._Active = true

        for _, keyDefinition in ipairs(LEGACY_KEYS) do
            local actionName = "ConvertedLegacy_" .. string.upper(keyDefinition.key)
            bindAction(
                actionName,
                function(_, inputState)
                    if inputState == Enum.UserInputState.Begin and module._Active then
                        state.legacyKeySignal:Fire(keyDefinition.key)
                    end
                    return Enum.ContextActionResult.Sink
                end,
                UserInputService.TouchEnabled,
                keyDefinition.code
            )
            if UserInputService.TouchEnabled then
                pcall(function()
                    actions:SetTitle(actionName, string.upper(keyDefinition.key))
                end)
            end
        end

        for _, descendant in ipairs(model:GetDescendants()) do
            if descendant:IsA("Motor6D") then
                saveJoint(descendant)
            end
        end

        local scope = {{
            RuntimeFolder = state.runtimeFolder,
            TrackConnection = trackConnection,
            TrackInstance = trackInstance,
            BindAction = bindAction,
            SaveJoint = saveJoint,
            IsActive = function()
                return module._Active
            end,
            FELOADLIBRARY = compatibilityLibrary,
            LegacyKeySignal = state.legacyKeySignal,
        }}

        state.legacyThread = task.spawn(function()
            local success, message = xpcall(function()
                legacyMain(model, scope)
            end, debug.traceback)

            if not success and module._Active then
                module._Active = false
                warn("[Converted Moveset] Legacy runtime error: " .. tostring(message))
            end
        end)

        -- Init must return even when the legacy source owns a permanent
        -- animation loop. Destroy cancels state.legacyThread.
        return true
    end

    module.Update = function(_deltaTime)
        -- Legacy source remains event-driven unless manually adapted.
    end

    return module
end)

return modules
'''


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a Luau script into a Reanimation Hub moveset wrapper."
    )
    parser.add_argument("input", type=Path, help="Input .lua/.luau/.txt file")
    parser.add_argument("output", type=Path, help="Output moveset .lua file")
    parser.add_argument(
        "--mode",
        choices=("safe", "aggressive"),
        default="safe",
        help="safe preserves source; aggressive rewrites common character references",
    )
    parser.add_argument("--name", default=None, help="Visible moveset name")
    parser.add_argument("--internal-name", default=None, help="Hub internal name")
    parser.add_argument("--description", default="Automatically converted legacy Luau script")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="JSON report path (default: OUTPUT.report.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    source = args.input.read_text(encoding="utf-8-sig")

    visible_name = args.name or args.input.stem
    internal_name = args.internal_name or re.sub(r"[^A-Za-z0-9_]+", "_", visible_name).upper()
    internal_name = internal_name.strip("_") or "CONVERTED_MOVESET"

    report_path = args.report or args.output.with_suffix(args.output.suffix + ".report.json")
    report = ConversionReport(
        input_file=str(args.input),
        output_file=str(args.output),
        mode=args.mode,
        module_name=visible_name,
        input_lines=len(source.splitlines()),
        detected=count_patterns(source),
    )

    converted_source = source
    if args.mode == "aggressive":
        converted_source, report.rewrites = aggressive_rewrite(source)

    if report.detected.get("loadstring") or report.detected.get("http_get"):
        report.warnings.append(
            "External executable/downloaded code was preserved as requested; inspect it manually."
        )
    if report.detected.get("script_reference") and args.mode == "safe":
        report.warnings.append(
            "The source references script; safe mode does not rewrite those references."
        )
    if report.detected.get("generic_connection"):
        tracked = report.rewrites.get("statement_connections_tracked", 0)
        total = report.detected.get("generic_connection", 0)
        if tracked < total:
            report.warnings.append(
                f"Tracked {tracked}/{total} connection statements; review nested or assigned :Connect calls manually."
            )
    if report.detected.get("while_loop") or report.detected.get("repeat_loop"):
        report.warnings.append(
            "Long-running loops may block Init or survive Destroy unless they check IsActive()."
        )
    if report.detected.get("break_joints") or report.detected.get("destroy_character"):
        report.warnings.append(
            "Potentially destructive character operation detected; review before running."
        )
    report.warnings.append(
        "Automatic conversion cannot guarantee runtime correctness, animation compatibility, cleanup, or ability behavior."
    )

    output = create_wrapper(
        converted_source,
        visible_name,
        internal_name,
        args.description,
        args.mode,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")

    report.output_lines = len(output.splitlines())
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Converted: {args.input} -> {args.output}")
    print(f"Mode: {args.mode}")
    print(f"Report: {report_path}")
    print(f"Warnings: {len(report.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
