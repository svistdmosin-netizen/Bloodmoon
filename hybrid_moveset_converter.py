from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

FORBIDDEN = (
    "loadstring", ":HttpGet", "game:GetObjects", "getgenv", "setfenv", "getfenv",
    "writefile", "readfile", "makefolder", "isfolder", "isfile",
)
FRAME_SIGNALS = ("RenderStepped", "Heartbeat", "Stepped", "PreRender", "PreSimulation", "PostSimulation")
STATE_NAMES = ("idle", "walk", "run", "jump", "fall", "sit", "climb")


@dataclass
class FunctionInfo:
    name: str
    start: int
    end: int
    source: str


@dataclass
class Inventory:
    filename: str
    source_bytes: int
    functions: list[FunctionInfo] = field(default_factory=list)
    controls: list[dict[str, str]] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    animation_states: list[str] = field(default_factory=list)
    frame_signals: list[str] = field(default_factory=list)
    bootstrap_end: int = 0
    opaque_assets: list[str] = field(default_factory=list)
    payload_pose_writes: int = 0
    payload_waits: int = 0
    payload_effect_calls: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "source_bytes": self.source_bytes,
            "functions": [{"name": x.name, "start": x.start, "end": x.end} for x in self.functions],
            "controls": self.controls,
            "assets": self.assets,
            "animation_states": self.animation_states,
            "frame_signals": self.frame_signals,
            "bootstrap_end": self.bootstrap_end,
            "opaque_assets": self.opaque_assets,
            "payload_pose_writes": self.payload_pose_writes,
            "payload_waits": self.payload_waits,
            "payload_effect_calls": self.payload_effect_calls,
        }


def _line_at(source: str, index: int) -> int:
    return source.count("\n", 0, index) + 1


def _function_blocks(source: str) -> list[FunctionInfo]:
    starts = list(re.finditer(r"(?m)^\s*(?:local\s+)?function\s+([A-Za-z_][\w.:]*)\s*\(", source))
    result: list[FunctionInfo] = []
    token_re = re.compile(r"\b(function|if|for|while|repeat|do|end|until)\b")
    for match in starts:
        depth = 0
        finish = None
        for token in token_re.finditer(source, match.start()):
            word = token.group(1)
            if word in {"function", "if", "for", "while", "repeat", "do"}:
                depth += 1
            elif word == "until":
                depth = max(0, depth - 1)
            elif word == "end":
                depth -= 1
                if depth <= 0:
                    finish = token.end()
                    break
        if finish:
            result.append(FunctionInfo(match.group(1), _line_at(source, match.start()), _line_at(source, finish), source[match.start():finish]))
    return result


def analyze(source: str, filename: str) -> Inventory:
    inv = Inventory(filename=filename, source_bytes=len(source.encode("utf-8")))
    inv.functions = _function_blocks(source)
    ids = re.findall(r"(?:rbxassetid://|rbxasset://[^\"']*?|(?<![\w]))(\d{6,15})", source)
    inv.assets = sorted(set(x for x in ids if x))[:500]
    inv.opaque_assets = sorted(set(re.findall(r"game:GetObjects\s*\(\s*[\"']rbxassetid://(\d+)", source)))
    inv.frame_signals = [name for name in FRAME_SIGNALS if name in source]
    inv.animation_states = sorted(set(x.lower() for x in re.findall(r"(?:token|pose)\s*==\s*[\"']([A-Za-z]+)", source) if x.lower() in STATE_NAMES))

    for pattern, kind in (
        (r"\.KeyDown\s*:\s*[Cc]onnect\s*\(\s*function\s*\([^)]*\).*?key\s*==\s*[\"']([^\"']+)", "key"),
        (r"\.Button1Down\s*:\s*[Cc]onnect", "mouse1"),
        (r"Enum\.KeyCode\.([A-Za-z0-9_]+)", "keycode"),
    ):
        for value in re.findall(pattern, source, re.S):
            inv.controls.append({"kind": kind, "value": value if isinstance(value, str) else "MouseButton1"})
    if re.search(r"\.Button1Down\s*:\s*[Cc]onnect", source):
        inv.controls.append({"kind": "mouse1", "value": "MouseButton1"})

    markers = [source.find(x) for x in ("title =", "function animation(", "local script = game:GetObjects") if source.find(x) >= 0]
    if markers:
        inv.bootstrap_end = _line_at(source, min(markers)) - 1
    payload = "\n".join(source.splitlines()[inv.bootstrap_end:])
    inv.payload_pose_writes = len(re.findall(r"\.Weld\.(?:C0|C1)\s*=|\b(?:Lerp|clerp)\s*\(", payload, re.I))
    inv.payload_waits = len(re.findall(r"\b(?:wait|task\.wait|[A-Za-z_]\w*:wait)\s*\(", payload, re.I))
    inv.payload_effect_calls = len(re.findall(r"\bEffect\s*\(", payload, re.I))
    return inv


def plan_prompt(source: str, inventory: Inventory, contract: str) -> str:
    return f'''You are the semantic planner in a Roblox Luau to Uhhhhhh hybrid compiler.
You do NOT write the final module. Return one JSON object only, with no Markdown.
The deterministic builder owns wrapper, Init, Update, Destroy, ContextActions, joints, cleanup, and self-protection.
Never execute source and never emit external loaders.

JSON schema:
{{
  "name": "string",
  "description": "string",
  "states": {{
    "idle": {{"source": "function/branch", "body": "Luau statements using ctx and pose(joint,c0,c1)"}},
    "walk": {{"source": "...", "body": "..."}},
    "run": {{"source": "...", "body": "..."}},
    "jump": {{"source": "...", "body": "..."}},
    "fall": {{"source": "...", "body": "..."}},
    "sit": {{"source": "...", "body": "..."}}
  }},
  "abilities": [
    {{"name":"identifier","title":"string","input":"Enum.KeyCode.X or Enum.UserInputType.MouseButton1","body":"bounded Luau statements using ctx"}}
  ],
  "assets": {{"sounds":[],"meshes":[],"textures":[],"opaque":[]}},
  "warnings": []
}}

Planner rules:
- state and ability bodies may only use: ctx, pose, effect, sound, fling, task, math, CFrame, Vector3, Color3, Enum.
- ctx contains figure, humanoid, root, torso, head, joints, sine, dt, attacking, created.
- joints are root, neck, rightShoulder, leftShoulder, rightHip, leftHip.
- pose(name,c0,c1) uses the builder-local joint transformation helper, with figure scale.
- sound(id, parentOrWorldPosition, volume, speed, distance, startAt, lifetime) is available.
- effect(mesh, size, transparency, material, color, position, rotation, positionChange, sizeChange, rotationChange, transparencyChange, acceleration) is available.
- findAllNearestTorso(position,radius), shake(amount), and fling(targetModel) are available.
- Do not include ctx.attacking guards or assignments in ability bodies; the deterministic wrapper owns that lock.
- Every identifier must be defined locally, on ctx, or in this allowed helper list. Never emit undefined shorthand such as ws.
- Preserve complete multi-phase procedural attacks: pose sequences, task.wait timing, combo branches, movement, sounds, effects, target scans, and fling calls. Short summary abilities are invalid.
- Preserve visible CFrame mathematics. Do not invent APIs, globals, models, or assets.
- Every loop in an ability must be statically bounded. No frame-signal connections.
- fling(targetModel) is self-protected by the builder.
- Omit a state or ability rather than returning placeholder comments.

Inventory:
{json.dumps(inventory.public(), ensure_ascii=False)}

Hub contract notes:
{contract}

SOURCE:
{source}
END SOURCE'''


def parse_plan(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI planner returned no JSON object")
    plan = json.loads(text[start:end + 1])
    if not isinstance(plan, dict):
        raise ValueError("migration plan is not an object")
    return plan


def _safe_body(value: Any, label: str) -> str:
    if not isinstance(value, str):
        return ""
    body = value.strip()
    lowered = body.lower()
    for token in FORBIDDEN:
        if token.lower() in lowered:
            raise ValueError(f"{label} contains forbidden token {token}")
    if any(re.search(rf"\b{x}\s*:\s*Connect", body) for x in FRAME_SIGNALS):
        raise ValueError(f"{label} creates a frame connection")
    if re.search(r"\b(?:game|workspace|script|LocalPlayer|Players)\b", body):
        raise ValueError(f"{label} escaped the constrained runtime")
    return body


def validate_plan(plan: dict[str, Any], inventory: Inventory) -> dict[str, Any]:
    states = plan.get("states") or {}
    abilities = plan.get("abilities") or []
    if not isinstance(states, dict) or not isinstance(abilities, list):
        raise ValueError("states/abilities have invalid types")
    clean_states: dict[str, dict[str, str]] = {}
    for name in STATE_NAMES:
        item = states.get(name)
        if not isinstance(item, dict):
            continue
        body = _safe_body(item.get("body"), f"state {name}")
        body = re.sub(r"\bws\b", "(ctx.humanoid.WalkSpeed)", body)
        if body:
            clean_states[name] = {"source": str(item.get("source") or ""), "body": body}
    if inventory.animation_states and not clean_states:
        raise ValueError("planner dropped every visible animation state")
    clean_abilities = []
    used = set()
    for raw in abilities:
        if not isinstance(raw, dict):
            continue
        name = re.sub(r"\W+", "_", str(raw.get("name") or "")).strip("_")[:60]
        body = _safe_body(raw.get("body"), f"ability {name}")
        # The deterministic wrapper owns the attack lock. Remove duplicated planner guards
        # that would otherwise observe the wrapper's lock and return immediately.
        body = re.sub(r"(?m)^\s*if\s+ctx\.attacking\s+then\s+return\s+end\s*$", "", body)
        body = re.sub(r"(?m)^\s*ctx\.attacking\s*=\s*(?:true|false)\s*$", "", body).strip()
        inp = str(raw.get("input") or "")
        if not name or not body or name in used:
            continue
        if not re.fullmatch(r"Enum\.(?:KeyCode\.[A-Za-z0-9_]+|UserInputType\.MouseButton1)", inp):
            raise ValueError(f"ability {name} has invalid input {inp}")
        used.add(name)
        clean_abilities.append({"name": name, "title": str(raw.get("title") or name)[:80], "input": inp, "body": body})
    if inventory.controls and not clean_abilities:
        raise ValueError("planner dropped every visible control")
    ability_text = "\n".join(x["body"] for x in clean_abilities)
    planned_poses = len(re.findall(r"\bpose\s*\(", ability_text))
    planned_waits = len(re.findall(r"\btask\.wait\s*\(", ability_text))
    planned_effects = len(re.findall(r"\beffect\s*\(", ability_text))
    min_poses = min(18, max(0, inventory.payload_pose_writes // 8))
    min_waits = min(3, max(0, inventory.payload_waits // 5))
    min_effects = min(4, max(0, inventory.payload_effect_calls // 4))
    missing_fidelity = []
    if planned_poses < min_poses: missing_fidelity.append(f"procedural attack poses {planned_poses}/{min_poses}")
    if planned_waits < min_waits: missing_fidelity.append(f"attack timing phases {planned_waits}/{min_waits}")
    if planned_effects < min_effects: missing_fidelity.append(f"attack effects {planned_effects}/{min_effects}")
    if missing_fidelity:
        raise ValueError("planner compressed the payload instead of migrating it: " + ", ".join(missing_fidelity))
    return {
        "name": str(plan.get("name") or inventory.filename.rsplit(".", 1)[0])[:80],
        "description": str(plan.get("description") or "Hybrid migration")[:400],
        "states": clean_states,
        "abilities": clean_abilities,
        "assets": plan.get("assets") if isinstance(plan.get("assets"), dict) else {},
        "warnings": [str(x)[:300] for x in (plan.get("warnings") or []) if isinstance(x, (str, int, float))],
    }


def _lua_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_module(plan: dict[str, Any]) -> str:
    state_blocks = []
    for name, item in plan["states"].items():
        state_blocks.append(f"        {name} = function(ctx)\n{_indent(item['body'], 12)}\n        end,")
    ability_blocks, bind_blocks = [], []
    for item in plan["abilities"]:
        name = item["name"]
        ability_blocks.append(f"    local function ability_{name}(ctx)\n        if ctx.attacking then return end\n        ctx.attacking = true\n        local ok, err = pcall(function()\n{_indent(item['body'], 12)}\n        end)\n        ctx.attacking = false\n        if not ok then warn(\"[Bloodmoon/{name}] \" .. tostring(err)) end\n    end")
        bind_blocks.append(f"        bind(\"Bloodmoon_{name}\", {_lua_string(item['title'])}, UDim2.new(1,-70,1,-{70 + len(bind_blocks)*70}), {item['input']}, function() ability_{name}(state) end)")
    states = "\n".join(state_blocks) or "        idle = function(ctx) end,"
    abilities = "\n\n".join(ability_blocks)
    binds = "\n".join(bind_blocks)
    return f'''local modules = {{}}
table.insert(modules, function()
    local m = {{}}
    m.ModuleType = "MOVESET"
    m.Name = {_lua_string(plan['name'])}
    m.Description = {_lua_string(plan['description'])}
    m.InternalName = "BLOODMOON_HYBRID"
    m.Assets = {{}}
    m.Config = function(parent) end
    m.SaveConfig = function() return {{}} end
    m.LoadConfig = function(save) end

    local state = {{figure=nil, humanoid=nil, root=nil, torso=nil, head=nil, joints={{}}, created={{}}, connections={{}}, actions={{}}, sine=0, dt=0, attacking=false}}
    local original = {{}}
    local function track(x) table.insert(state.created, x) return x end
    -- Uhhhhhh does not inject SetC0C1Joint into user modules. Implement it locally.
    local function SetC0C1Joint(j, c0, c1, scale)
        local t = c0 * c1:Inverse()
        t += t.Position * (scale - 1)
        t = j.C0:Inverse() * t * j.C1
        j.Transform = t
    end
    local function pose(name, c0, c1)
        local joint = state.joints[name]
        if joint then SetC0C1Joint(joint, c0, c1 or joint.C1, state.figure and state.figure:GetScale() or 1) end
    end
    local function sound(id, parentOrPosition, volume, speed, distance, startAt, lifetime)
        if id==nil or tostring(id)=="Stop" then return nil end
        local parent=parentOrPosition
        if typeof(parentOrPosition)=="Vector3" then
            local point=track(Instance.new("Part")); point.Anchored=true; point.CanCollide=false; point.CanQuery=false; point.CanTouch=false; point.Transparency=1; point.Size=Vector3.new(0.05,0.05,0.05); point.Position=parentOrPosition; point.Parent=workspace
            parent=point
        end
        if typeof(parent)~="Instance" then parent=state.root end
        local s=track(Instance.new("Sound")); s.SoundId=tostring(id):find("rbxasset") and tostring(id) or "rbxassetid://"..tostring(id)
        s.Volume=volume or 1; s.PlaybackSpeed=speed or 1; s.EmitterSize=distance or 10; s.TimePosition=startAt or 0; s.Parent=parent; s:Play()
        if lifetime then task.delay(math.max(0.05,lifetime),function() if s.Parent then s:Destroy() end end) end
        return s
    end
    local function effect(mesh,size,transparency,material,color,position,rotation,positionChange,sizeChange,rotationChange,transparencyChange,acceleration)
        local part=track(Instance.new("Part")); part.Name="BLOODMOON_EFFECT"; part.Anchored=true; part.CanCollide=false; part.CanQuery=false; part.CanTouch=false
        part.Size=Vector3.one; part.Transparency=tonumber(transparency) or 0; part.Material=typeof(material)=="EnumItem" and material or Enum.Material.Neon; part.Color=typeof(color)=="Color3" and color or Color3.new(1,1,1)
        part.CFrame=CFrame.new(typeof(position)=="Vector3" and position or state.root.Position); part.Parent=state.figure
        local sm=Instance.new("SpecialMesh"); sm.Parent=part; sm.Scale=typeof(size)=="Vector3" and size or Vector3.one
        if typeof(mesh)=="EnumItem" then sm.MeshType=mesh elseif tonumber(mesh) then sm.MeshId="rbxassetid://"..tostring(mesh) elseif type(mesh)=="string" and mesh:find("rbxasset") then sm.MeshId=mesh else sm.MeshType=Enum.MeshType.Brick end
        part:SetAttribute("BMPosX",(typeof(positionChange)=="Vector3" and positionChange.X or 0)); part:SetAttribute("BMPosY",(typeof(positionChange)=="Vector3" and positionChange.Y or 0)); part:SetAttribute("BMPosZ",(typeof(positionChange)=="Vector3" and positionChange.Z or 0))
        part:SetAttribute("BMSizeX",(typeof(sizeChange)=="Vector3" and sizeChange.X or 0)); part:SetAttribute("BMSizeY",(typeof(sizeChange)=="Vector3" and sizeChange.Y or 0)); part:SetAttribute("BMSizeZ",(typeof(sizeChange)=="Vector3" and sizeChange.Z or 0)); part:SetAttribute("BMTransparency",tonumber(transparencyChange) or 0)
        return part
    end
    local function findAllNearestTorso(position,radius)
        local found={{}}; for _,part in ipairs(workspace:GetPartBoundsInRadius(position,radius)) do local model=part:FindFirstAncestorOfClass("Model"); local hum=model and model:FindFirstChildOfClass("Humanoid"); if hum and hum.Health>0 and model~=state.figure and not table.find(found,model) then table.insert(found,model) end end
        local heads={{}}; for _,model in ipairs(found) do table.insert(heads,model:FindFirstChild("Head") or model:FindFirstChild("HumanoidRootPart")) end; return heads
    end
    local function shake(amount) state.shake=math.max(state.shake or 0,tonumber(amount) or 0) end
    local function fling(target)
        if not target or target==state.figure or target==game:GetService("Players").LocalPlayer.Character then return false end
        if target:IsDescendantOf(state.figure) then return false end
        return ReanimateFling(target)
    end
    local poses = {{
{states}
    }}
{abilities}
    local function bind(name,title,position,input,callback)
        ContextActions:BindAction(name,function(_,inputState) if inputState==Enum.UserInputState.Begin then task.spawn(callback) end end,true,input)
        ContextActions:SetTitle(name,title); ContextActions:SetPosition(name,position); table.insert(state.actions,name)
    end
    m.Init = function(figure)
        state.figure=figure; state.humanoid=figure:FindFirstChildOfClass("Humanoid"); state.root=figure:FindFirstChild("HumanoidRootPart"); state.torso=figure:FindFirstChild("Torso"); state.head=figure:FindFirstChild("Head")
        if not (state.humanoid and state.root and state.torso and state.head) then error("Bloodmoon hybrid requires an R6 figure") end
        state.joints.root=state.root:FindFirstChild("RootJoint"); state.joints.neck=state.torso:FindFirstChild("Neck")
        state.joints.rightShoulder=state.torso:FindFirstChild("Right Shoulder"); state.joints.leftShoulder=state.torso:FindFirstChild("Left Shoulder")
        state.joints.rightHip=state.torso:FindFirstChild("Right Hip"); state.joints.leftHip=state.torso:FindFirstChild("Left Hip")
        original.walkSpeed=state.humanoid.WalkSpeed; original.jumpPower=state.humanoid.JumpPower
{binds}
    end
    m.Update = function(dt,figure)
        if figure~=state.figure then return end; state.dt=math.clamp(tonumber(dt) or 0,0,0.1); state.sine+=state.dt*60
        if state.attacking then return end
        local hs=state.humanoid:GetState(); local velocity=state.root.AssemblyLinearVelocity; local speed=Vector3.new(velocity.X,0,velocity.Z).Magnitude
        local wanted = hs==Enum.HumanoidStateType.Seated and "sit" or hs==Enum.HumanoidStateType.Climbing and "climb" or hs==Enum.HumanoidStateType.Jumping and "jump" or (hs==Enum.HumanoidStateType.Freefall or hs==Enum.HumanoidStateType.FallingDown) and "fall" or speed>16 and "run" or speed>0.1 and "walk" or "idle"
        for i=#state.created,1,-1 do local part=state.created[i]; if part and part.Parent and part.Name=="BLOODMOON_EFFECT" then local sm=part:FindFirstChildOfClass("SpecialMesh"); if sm then sm.Scale+=Vector3.new(part:GetAttribute("BMSizeX") or 0,part:GetAttribute("BMSizeY") or 0,part:GetAttribute("BMSizeZ") or 0) end; part.Position+=Vector3.new(part:GetAttribute("BMPosX") or 0,part:GetAttribute("BMPosY") or 0,part:GetAttribute("BMPosZ") or 0); part.Transparency+=part:GetAttribute("BMTransparency") or 0; if part.Transparency>=1 or (sm and sm.Scale.Magnitude<=0.01) then part:Destroy(); table.remove(state.created,i) end end end
        local fn=poses[wanted] or (wanted=="walk" and poses.run) or (wanted=="fall" and poses.jump) or poses.walk or poses.idle; if fn then fn(state) end
    end
    m.Destroy = function(figure)
        for _,name in ipairs(state.actions) do ContextActions:UnbindAction(name) end
        for _,c in ipairs(state.connections) do pcall(function() c:Disconnect() end) end
        for i=#state.created,1,-1 do pcall(function() state.created[i]:Destroy() end) end
        if state.humanoid then state.humanoid.WalkSpeed=original.walkSpeed or 16; state.humanoid.JumpPower=original.jumpPower or 50 end
        for _,j in pairs(state.joints) do if j then j.Transform=CFrame.identity end end
        table.clear(state.actions); table.clear(state.connections); table.clear(state.created); state.figure=nil
    end
    return m
end)
return modules
'''


def _indent(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line.strip() else line for line in text.splitlines())
