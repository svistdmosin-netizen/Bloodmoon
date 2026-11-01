from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
ENGINE_ROOT = ROOT if (ROOT / "luau_moveset_mind.py").exists() else ROOT.parent
JOBS = Path("/tmp/bloodmoon-jobs")
                                                                           
                                                    
STATIC = ROOT / "static" if (ROOT / "static").is_dir() else ROOT
MAX_UPLOAD = 4 * 1024 * 1024
ALLOWED = {".lua", ".luau", ".txt"}
AI_ENDPOINT = "https://api.kilo.ai/api/gateway/chat/completions"
AI_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
AI_PROVIDER = "Kilo Gateway anonymous"
AI_SOURCE_LIMIT = 650_000
JOBS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Luau Moveset Mind", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

def safe_name(value: str, fallback: str = "Moveset") -> str:
    value = re.sub(r"[^A-Za-z0-9 _.-]+", "", value).strip()
    return value[:80] or fallback

def read_limited(path: Path, limit: int = 300_000) -> str:
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8", errors="replace")
    return data if len(data) <= limit else data[:limit] + "\n-- Preview truncated"

def zip_project(project: Path, destination: Path):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in project.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(project.parent))

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")

@app.get("/health")
def health():
    return {"ok": True, "engine": (ENGINE_ROOT / "luau_moveset_mind.py").exists()}

@app.post("/api/convert")
async def convert(
    file: UploadFile = File(...),
    name: str = Form(""),
    description: str = Form("Generated moveset migration project"),
):
    suffix = Path(file.filename or "script.lua").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, "Only .lua, .luau and .txt files are accepted")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File is larger than 4 MB")
    if not data.strip():
        raise HTTPException(400, "Uploaded file is empty")

    job_id = uuid.uuid4().hex
    job = JOBS / job_id
    project = job / "project"
    job.mkdir(parents=True)
    source_name = safe_name(Path(file.filename or "script.lua").name, "script.lua")
    source = job / source_name
    source.write_bytes(data)
    display_name = safe_name(name, Path(source_name).stem)
    description = description.strip()[:400] or "Generated moveset migration project"

    command = [
        sys.executable,
        str(ENGINE_ROOT / "luau_moveset_mind.py"),
        str(source),
        "--output-dir", str(project),
        "--name", display_name,
        "--description", description,
    ]
    try:
        process = subprocess.run(command, cwd=ENGINE_ROOT, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Analysis timed out")
    if not (project / "manifest.json").exists():
        raise HTTPException(500, "Converter failed: " + (process.stderr or process.stdout)[-1200:])

    manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
    archive = job / "moveset-project.zip"
    zip_project(project, archive)
    response = {
        "job_id": job_id,
        "manifest": manifest,
        "console": (process.stdout + process.stderr)[-5000:],
        "safe_code": read_limited(project / "moveset.lua"),
        "legacy_code": read_limited(project / "legacy_candidate.lua"),
        "overview": read_limited(project / "README.md", 100_000),
        "safe_report": read_limited(project / "moveset.lua.report.md", 100_000),
        "legacy_report": read_limited(project / "legacy_candidate.lua.report.md", 100_000),
        "zip_base64": base64.b64encode(archive.read_bytes()).decode("ascii"),
        "downloads": {
            "zip": f"/api/download/{job_id}/zip",
            "safe": f"/api/download/{job_id}/safe",
            "legacy": f"/api/download/{job_id}/legacy",
            "manifest": f"/api/download/{job_id}/manifest",
        },
    }
    return JSONResponse(response)

def extract_luau(text: str) -> str:
    match = re.search(r"```(?:lua|luau)?\s*(.*?)```", text, re.I | re.S)
    return (match.group(1) if match else text).strip()

def ai_prompt(source: str, filename: str, name: str) -> str:
    return f'''You are migrating a legacy Roblox Luau character script into a Uhhhhhh Reanimate Hub module.
Return only the complete compact Luau source. Do not use Markdown fences and do not explain anything.
The complete output must fit within 45000 tokens. Consolidate repeated effects and pose operations into reusable helpers. Do not reproduce large repetitive effect constructors or every animation frame literally. Prefer a complete concise implementation over duplicated legacy code, and reserve enough output budget to close every function and return the module.

The output contract is mandatory:
local modules = {{}}
table.insert(modules, function()
    local m = {{}}
    m.ModuleType = "MOVESET"
    m.Name = "{name}"
    m.Description = "AI-migrated moveset"
    m.InternalName = "AI_Migrated_Moveset"
    m.Assets = {{}}
    m.Config = function(parent) end
    m.SaveConfig = function() return {{}} end
    m.LoadConfig = function(save) end
    m.Init = function(figure) end
    m.Update = function(dt, figure) end
    m.Destroy = function(figure) end
    return m
end)
return modules

Hub rules:
- figure is the reanimated R6 character. Never use workspace.non or LocalPlayer.Character as the controlled rig.
- Init creates state, binds ContextActions, registers connections and creates temporary instances.
- Update performs idle, walk, jump, fall and attack pose updates. Move legacy animation loops into Update instead of starting an infinite loop in Init.
- Locate Humanoid, HumanoidRootPart, Torso, RootJoint, Neck, shoulders and hips defensively.
- Prefer the developer contract helper SetC0C1Joint when applying C0/C1 poses. Its semantic target is: t = c0 * c1:Inverse(); scale its translation; then j.Transform = j.C0:Inverse() * t * j.C1. Preserve the original pose mathematics and timing.
- Keep a module-scope state table such as state.figure, state.joints, state.created, state.connections, state.actions, state.pose, state.attack, state.sine, and state.effects. Init fills it, Update consumes it every frame, abilities mutate it, and Destroy clears it.
- ContextActions:BindAction registers keyboard and touch controls. SetTitle and SetPosition for every action. Unbind every action in Destroy.
- Use ReanimateFling only during intentional attack hit phases and never target figure, LocalPlayer.Character, their descendants, or _G.Uhhhhhh.Character.
- HatReanimator.HatCFrameOverride entries added in Init must be removed in Destroy.
- Track and disconnect every RBXScriptConnection. Destroy temporary instances and restore modified Humanoid, camera and joint state.
- Preserve abilities, animation modes, sounds, effects and key mappings whenever their behavior exists in the source.
- Replace old wait/spawn/connect APIs with task.wait/task.spawn/Connect where appropriate.
- Never execute or preserve executable external loading. The output must not contain loadstring, HttpGet, game:GetObjects, require(assetId), or a compatibility adapter that runs the original script. Record unavailable external behavior only in a non-executable comment.
- Do not return a scaffold with empty Update when visible animation logic exists. Update must contain executable pose/state logic, not placeholder comments.
- Do not create Heartbeat, RenderStepped, Stepped, PreRender, PreSimulation, or PostSimulation connections in the module; the hub calls Update itself. Replace frame waits inside abilities with task.wait or dt-driven attack phases, and keep every recurring per-frame operation in Update.
- Keep shared runtime state in locals outside Init so Init, Update, abilities, and Destroy use the same joints, figure, attack state, clocks, instances, and connections.
- Resolve and cache R6 RootJoint, Neck, Right Shoulder, Left Shoulder, Right Hip, and Left Hip from figure. Never animate anchored decorative copies while leaving the controlled figure unchanged.
- For Motor6D animation, preserve each joint's base C0/C1 and apply poses through SetC0C1Joint when available; otherwise use Motor6D.Transform or carefully interpolated C0. Convert degree values with math.rad. Do not assign world CFrames to body parts as a substitute for joint animation.
- Reconstruct the original idle, run/walk, jump, fall, sit, and attack pose phases from visible CFrame/Angles/clerp/Lerp logic. Advance a persistent sine/phase value from dt. Detect grounded state with a raycast excluding figure and derive local forward/side movement from HumanoidRootPart velocity.
- Treat the legacy main RenderStepped/Heartbeat body as the primary specification for Update. Move its per-frame state selection, effect stepping, pose call, music volume, particles, and sine advancement into m.Update instead of omitting that loop.
- When the source defines animation(token), preserve every visible token branch and its exact joint C0/C1 targets. Adapt custom body-part Welds to the cached R6 joints or recreate and track equivalent welds; do not discard the animation function merely because it does not use standard Motor6D names.
- Preserve source interpolation semantics. A source that animates both C0 and C1 needs both sides represented through SetC0C1Joint or an equivalent transform calculation; copying only one side changes the pose.
- Convert blocking attack loops into task-spawned ability state machines or bounded tasks that set and clear attack/debounce flags. They must not block m.Update forever, and ordinary locomotion poses must pause or blend correctly while an attack owns the joints.
- Convert every mouse click and key handler into named abilities and ContextActions. The output must not retain Mouse.KeyDown, Mouse.KeyUp, Mouse.Button1Down, UserInputService.InputBegan, or InputEnded as the primary control wiring. For aim-dependent attacks, read the local mouse only for its Hit/world target; never use LocalPlayer.Character as the controlled rig.
- Use the actual hub control object ContextActions. For each action call ContextActions:BindAction(uniqueName, callback, true, keyOrInput), run only on Enum.UserInputState.Begin unless it is an intentional hold action, then call ContextActions:SetTitle and ContextActions:SetPosition. Store every uniqueName and call ContextActions:UnbindAction in Destroy.
- Migrate generated effect systems (effect parts, meshes, lightning, projectiles, particles, sounds, camera shake) together with the abilities that call them. Per-frame effect advancement belongs in Update and all effect instances/connections belong to tracked cleanup state.
- Legacy RemoteEvents created locally do not replicate by themselves. Do not replace them with BindableEvent and do not preserve fake FireServer/OnClientEvent synchronization as if it worked; remove the fake network layer and call the local visible effect/ability functions directly.
- Executor filesystem APIs such as getgenv, isfolder, makefolder, isfile, writefile, getcustomasset, and getsynasset are unavailable hub dependencies. Replace optional music with declared m.Assets/configurable Roblox asset IDs and keep the module functional when an external audio file cannot be migrated.
- Detect bundled reanimation bootstrap code separately from the moveset payload. Large sections that create/replace a character, align accessories, force simulation radius, manage permanent death, camera subject, refit, or hat-drop are infrastructure and must not be copied into the module; the supplied figure already represents the reanimated rig.
- A common payload boundary is a later title/credit block, game:GetObjects model reference, animation function, effects helpers, input handlers, and main frame loop. Analyze everything after that boundary as moveset behavior while replacing the bootstrap's Character/Rig with figure.
- If game:GetObjects only supplies an opaque model whose descendants are not present in source, do not invent its geometry and do not execute the asset. Preserve its numeric ID in m.Assets and add a clear non-executable missing-asset note, while still migrating all visible animation/effect behavior around it.
- Never replace the reanimator with invented dependencies such as KadeAPI, OpenFE, getgenv globals, or another external framework. The only controlled rig is the Init/Update figure argument and the documented Uhhhhhh globals.
- Abilities must be real callable functions using the same cached state and joints. Bind every recovered key through ContextActions:BindAction in Init, call the matching ability on Begin, add touch titles/positions, debounce attack state, and unbind every action in Destroy.
- Preserve visible models, crowns, weapons, meshes, welds, trails, particles, sounds, textures, colors, sizes, offsets, and attachment relationships. Parent created visual models to figure or the correct world container, weld or update them to the intended body joint, and track every created Instance for cleanup.
- If the source creates a visual replacement rig, map its parts to figure joints and animate that rig from the recovered pose data. Hiding the original character and merely copying unchanged body-part CFrames is not a migration.
- Never destroy source accessories that cannot be recreated. For temporary hiding, store and restore properties instead.
- Save every RBXScriptConnection in a Lua table; never put Instances or connections in Attributes. Disconnect all of them in Destroy.
- Output must parse as Luau before packaging. Never use ... inside a function unless that exact function declaration contains a vararg parameter, and prefer explicit callback parameters over select(...). Do not reference an undeclared local, malformed Enum member, or method that the created Instance class does not provide.
- Destroy all generated models/effects and restore joint transforms, transparency, collision, Humanoid properties, camera state, and hat overrides in Destroy.
- Do not paste the entire original program inside Init. Restructure it into the hub lifecycle.
- Keep attribution comments from the source.
- Produce a self-contained module and do not claim runtime guarantees.

Source filename: {filename}
Source begins:
{source}
Source ends.'''

def call_anonymous_ai(prompt: str) -> str:
    payload = json.dumps({
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 60000,
        "include_reasoning": False,
        "reasoning_effort": "low",
    }).encode("utf-8")
    request = urllib.request.Request(
        AI_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Bloodmoon-Converter/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=295) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-1000:]
        if exc.code == 429:
            raise HTTPException(429, "The free anonymous AI limit is busy. Wait at least one minute and try again.")
        raise HTTPException(502, f"Anonymous AI request failed ({exc.code}): {detail}")
    except Exception as exc:
        raise HTTPException(502, f"Anonymous AI is unavailable: {exc}")
    choices = result.get("choices") or []
    if not choices:
        raise HTTPException(502, "Anonymous AI returned no result")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if not content.strip():
        raise HTTPException(502, "Anonymous AI exhausted its response budget before producing code")
    if choice.get("finish_reason") == "length":
        raise HTTPException(502, "The anonymous AI used its full 60,000-token response budget without completing the Luau module. No truncated file was packaged.")
    return content

@app.post("/api/ai-convert")
async def ai_convert(file: UploadFile = File(...), name: str = Form(""), description: str = Form("")):
    suffix = Path(file.filename or "script.lua").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, "Only .lua, .luau and .txt files are accepted")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File is larger than 4 MB")
    if not data.strip():
        raise HTTPException(400, "Uploaded file is empty")
    source = data.decode("utf-8", errors="replace")
    if len(source) > AI_SOURCE_LIMIT:
        raise HTTPException(413, "This source is too large for the anonymous AI context. Use a smaller script or the static converter.")
    display_name = safe_name(name, Path(file.filename or "script.lua").stem)
    response_text = await __import__("asyncio").to_thread(call_anonymous_ai, ai_prompt(source, file.filename or "script.lua", display_name))
    code = extract_luau(response_text)
    required = ["ModuleType", "MOVESET", ".Init", ".Update", ".Destroy", "return modules"]
    missing = [item for item in required if item not in code]
    if missing:
        raise HTTPException(502, "AI returned an incomplete hub module. Missing: " + ", ".join(missing))
    forbidden = [item for item in ("loadstring", ":HttpGet", "game:GetObjects") if item in code]
    if forbidden:
        raise HTTPException(502, "AI tried to preserve executable external loading instead of migrating the visible behavior. Rejected: " + ", ".join(forbidden))
    update_match = re.search(r"(?:m\.)?Update\s*=\s*function\s*\([^)]*\)(.*?)\n\s*end", code, re.S)
    if not update_match:
        raise HTTPException(502, "AI returned no readable Update lifecycle body")
    update_body = re.sub(r"--[^\n]*", "", update_match.group(1)).strip()
    if len(update_body) < 80:
        raise HTTPException(502, "AI returned an empty or placeholder Update. The file was rejected because its animation lifecycle would do nothing.")
    source_has_controls = any(token in source for token in ("KeyDown", "Button1Down", "InputBegan", "BindAction"))
    if source_has_controls and "ContextActions:BindAction" not in code:
        raise HTTPException(502, "AI preserved legacy input listeners instead of wiring abilities through ContextActions")
    rejected_patterns = {
        "LocalPlayer.Character ownership": r"(?:LocalPlayer|lplr|plr)\.Character",
        "legacy mouse controls": r"\.KeyDown\s*:\s*Connect|\.Button1Down\s*:\s*Connect",
        "extra frame connection": r"(?:RenderStepped|Heartbeat|Stepped|PreRender|PreSimulation|PostSimulation)\s*:\s*Connect",
        "fake local network layer": r"Instance\.new\s*\(\s*[\"'](?:RemoteEvent|BindableEvent)[\"']",
    }
    violations = [name for name, pattern in rejected_patterns.items() if re.search(pattern, code)]
    if violations:
        raise HTTPException(502, "AI returned code that initializes but violates the runtime contract: " + ", ".join(violations))
    job_id = uuid.uuid4().hex
    job = JOBS / job_id
    project = job / "project"
    project.mkdir(parents=True)
    output = project / "ai_moveset.lua"
    output.write_text(code + "\n", encoding="utf-8")
    (project / "source.lua").write_text(source, encoding="utf-8")
    report = {
        "provider": AI_PROVIDER,
        "model": AI_MODEL,
        "source_executed": False,
        "runtime_guarantee": False,
        "contract_fields_present": True,
        "source_bytes": len(data),
        "output_bytes": len(code.encode("utf-8")),
    }
    (project / "ai_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    manifest = {
        "source": file.filename or "script.lua",
        "architecture": {"kind": "ai_contract_migration", "confidence": 0.7},
        "readiness": 0.7,
        "keys": [],
        "assets": {"sounds": [], "meshes": [], "textures": []},
        "roles": [],
        "blockers": [],
    }
    return JSONResponse({
        "job_id": job_id,
        "manifest": manifest,
        "console": f"AI migration completed with {AI_MODEL}. Source was not executed.",
        "safe_code": code,
        "legacy_code": code,
        "overview": json.dumps(report, indent=2),
        "safe_report": "AI-generated migration. Manual Roblox runtime testing is required.",
        "legacy_report": "",
        "downloads": {
            "safe": f"/api/download/{job_id}/ai",
            "legacy": f"/api/download/{job_id}/ai",
            "manifest": f"/api/download/{job_id}/ai-report",
        },
    })

@app.get("/api/download/{job_id}/{kind}")
def download(job_id: str, kind: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(404)
    job = JOBS / job_id
    mapping = {
        "zip": (job / "moveset-project.zip", "moveset-project.zip", "application/zip"),
        "safe": (job / "project/moveset.lua", "moveset.lua", "text/plain"),
        "legacy": (job / "project/legacy_candidate.lua", "legacy_candidate.lua", "text/plain"),
        "manifest": (job / "project/manifest.json", "manifest.json", "application/json"),
        "ai-zip": (job / "ai-moveset-project.zip", "ai-moveset-project.zip", "application/zip"),
        "ai": (job / "project/ai_moveset.lua", "ai_moveset.lua", "text/plain"),
        "ai-report": (job / "project/ai_report.json", "ai_report.json", "application/json"),
    }
    if kind not in mapping:
        raise HTTPException(404)
    path, filename, media = mapping[kind]
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type=media, filename=filename)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
