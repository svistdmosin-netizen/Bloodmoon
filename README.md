---
title: Bloodmoon
emoji: 🌘
colorFrom: red
colorTo: black
sdk: docker
app_port: 7860
pinned: false
---

# Bloodmoon

Bloodmoon is a personal Luau-to-moveset conversion project by Restlessman.

It statically inspects uploaded `.lua`, `.luau`, and `.txt` source files, then produces:

- a safe moveset scaffold;
- a legacy migration candidate;
- architecture and function-role analysis;
- detected controls and assets;
- blocker reports;
- a project manifest;
- a downloadable ZIP.

Uploaded Luau is treated as text and is never executed.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8000`.

## Docker

```bash
docker build -t bloodmoon .
docker run --rm -p 8000:8000 bloodmoon
```

## API

- `GET /health`
- `POST /api/convert`
- `GET /api/download/{job_id}/zip`
- `GET /api/download/{job_id}/safe`
- `GET /api/download/{job_id}/legacy`
- `GET /api/download/{job_id}/manifest`

## Important

Static readiness and compile/AST results are not guarantees of Roblox runtime compatibility. Legacy output must be reviewed before use.

## Creator

Restlessman — [Roblox profile](https://www.roblox.com/users/3539425154/profile)
