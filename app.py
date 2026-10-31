from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
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
