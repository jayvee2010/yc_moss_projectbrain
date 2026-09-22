# ProjectBrain — run doc

FastAPI backend + static frontend (single-page app served by the backend). Data: SQLite (`projectbrain.db`), retrieval: Moss cloud index.

## Reproduce artifacts

Nothing to build — no bundler. Required non-committed files:

1. **Python venv**: the main checkout has `.venv/` (Python 3.12, deps: fastapi, uvicorn, moss, google-genai, dotenv, httpx). A fresh checkout can reuse it via `cp -R` from the main checkout or recreate: `python3 -m venv .venv && .venv/bin/pip install fastapi "uvicorn[standard]" moss google-genai python-dotenv httpx`.
2. **`.env` from the main checkout** (`cp .env /path/to/other/worktree/.env` — never copy secrets into git). Keys: `MOSS_PROJECT_ID`, `MOSS_PROJECT_KEY`, `GEMINI_API_KEY`, optional `GITHUB_TOKEN`. The server BOOTS WITHOUT them (SQLite-only mode); `/ask`, `/check`, AI extraction and Moss indexing need real keys.
3. **Demo data (optional)**: `projectbrain.db` already holds seeded projects (`aura-smart-home` 52 memories, `webscraping` 51, `fastapi-demo` 17). Recreate from scratch: `.venv/bin/python -m backend.seed --project-id aura-smart-home`.

## Run the server

```bash
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000 — the UI is served at `/` (plus `/styles.css`, `/app.js`). Opening `frontend/index.html` directly from disk also works (relative assets + file://→127.0.0.1:8000 API fallback).

Startup: pre-warms all Moss indexes (skips gracefully if Moss credits are exhausted — see note). Verify: `curl -s localhost:8000/health` → `{"status":"ok"}`.

Detached (macOS, survives the shell): run inside `screen -dmS pb /bin/sh -c "cd <root> && exec .venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 >> .freebuff/preview.log 2>&1"` (launchctl submit has cwd `/` and needs a wrapper; nohup gets reaped by the Freebuff runner).

## Known operational notes

- **Moss credits**: cloud load/index calls consume account credits. The app syncs indexes IN PLACE (`add_docs` upsert + `delete_docs`, load once per process) to minimize usage; `HTTP 429 USAGE_LIMIT_EXCEEDED` means the account window is drained — UI keeps working from SQLite; wait for the window to reset.
- **Gemini 503s**: `/ask` uses a fallback model chain (flash-lite → flash-lite-latest → 2.5-flash).
- Port 8000 busy? Use any free port; the UI targets `http://127.0.0.1:8000` by default — change it in Settings → Engine URL when served from disk, or same-origin when served by the backend.
