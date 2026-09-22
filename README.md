# ProjectBrain

**Your entire team. One living project context.**

ProjectBrain is a shared live project-state layer for humans and AI agents. Instead of every AI conversation starting from zero — and every teammate digging through old chats to find out *why* something was decided — ProjectBrain keeps one structured, searchable record of a project: its decisions (and rejections), tasks, blockers, changes, experiments, and who did what.

When a human or an AI asks a question, [Moss](https://usemoss.dev) retrieves only the few relevant facts — in single-digit milliseconds — and Gemini answers grounded in those facts, with **citations back to the original events**.

```
                 PROJECT EVENTS  (text, GitHub repos, conversations)
                              │
                       ┌──────▼──────┐
                       │ STATE ENGINE │   typed memories: decision · task ·
                       │  (SQLite)    │   blocker · change · experiment · fact
                       └──────┬──────┘
                              │  every memory carries author + type + entities
                       ┌──────▼──────┐
                       │    MOSS     │   sub-10ms semantic retrieval,
                       │   INDEX     │   per-project index, in-process query path
                       └──────┬──────┘
                              │  top-K relevant memories + measured latency
                     ┌────────┴─────────┐
                     ▼                  ▼
                HUMAN  (UI)        AI  (Gemini, grounded + cited)
```

Moss handles retrieval. ProjectBrain handles **meaning and state**.

## Why it exists

AI tools are single-player. ChatGPT doesn't know what Claude concluded yesterday. Cursor doesn't know the team rejected Firebase. A teammate returning after a week has no diff of what changed. ProjectBrain is the layer between:

- **Shared context** — humans and agents read and write the same project state.
- **Provenance** — every memory records its author (a person, or `"inferred (AI)"`), so "Who decided to use ESP32?" has a real answer.
- **Rejections matter** — "we tried Firebase and rejected it because…" is exactly the context that prevents an agent from repeating a mistake.
- **Conflict detection** — propose MongoDB when the team approved PostgreSQL and ProjectBrain flags `DIRECT_CONFLICT` and cites the original decision.

## The Moss retrieval story — our measured numbers

All numbers below are **measured on this machine** (localhost, warm index, Sept 21 2026) — not Moss's advertised benchmarks. Retrieval latency includes the full Moss query round-trip as called from `query_memories()`.

| Scenario | Measured retrieval |
|---|---|
| `aura-smart-home` index (52 docs), 6 different questions | **4.4 – 8.7 ms** |
| `webscraping` index (51 docs, real repo ingest) | **27 – 30 ms** warm |
| Brand-new index, first-ever query (index creation + cold load just happened) | ~2.6 s once, then warm numbers above |

What the demo shows:

- **Ask** — "Why did we reject Firebase?" returns a grounded answer citing 5 provenanced memories; the UI displays the *actual* Moss latency next to the retrieval count (never hardcoded).
- **Who-questions** — "Who decided to use ESP32?" → "Jayvee decided to use the ESP32-C6", because author flows from SQLite through the prompt.
- **Conflict check** — proposing MongoDB against the Supabase decision returns `DIRECT_CONFLICT` citing `seed-…-decision-001` with its original rationale.

Operational notes we built for this: indexes are loaded **once per process** (startup pre-warm) and synced **in place** (upsert + delete-stale) so reconnecting a repo never re-creates an index or re-burns cloud load credits; a live query path never sits behind a network load. When Moss returns `429 USAGE_LIMIT_EXCEEDED` (account credit window drained), the UI keeps working from SQLite; `/ask` resumes when the window resets. Gemini 503s are absorbed by a fallback model chain.

## Features

- **Feed Context** — paste conversations/notes, or connect **any public GitHub repo**: last ~25 commits become `change` memories with real git authors, open issues become `task` memories (bug-labeled → blockers), the README feeds an AI insight pass that infers higher-level decisions.
- **AI extraction** — Gemini turns raw pasted text into typed memories with entities and authors.
- **Ask Project** — grounded answers with source cards (memory id, author, timestamp, relevance score) and a live retrieval-latency chip.
- **Conflict Check** — deterministic entity-overlap detection plus an LLM judge, with a CURRENT vs PROPOSAL verdict view.
- **Overview / Activity / Decisions / Tasks** — derived project-health meters (labeled as derived), a live activity ticker, the "living thread" timeline with day separators and per-type nodes, and decision expansion into WHY / WHO / WHEN.
- **Keyboard-first** — `1–9` navigate, `/` ask, `N` what's-new.
- **Works offline-of-keys** — no `.env` credentials? The server still boots and serves everything except AI features, with graceful error states.

## Run it

Requirements: Python 3.12+, a Moss account (project id + key), a Gemini API key (free tier works). GitHub token is optional (raises rate limits from 60 to 5,000 req/hr).

```bash
# 1. Environment
python3 -m venv .venv
.venv/bin/pip install fastapi "uvicorn[standard]" moss google-genai python-dotenv httpx

# 2. Credentials — .env in the project root (never commit it)
cat > .env <<'EOF'
MOSS_PROJECT_ID=your-moss-project-id
MOSS_PROJECT_KEY=your-moss-project-key
GEMINI_API_KEY=your-gemini-key
GITHUB_TOKEN=
EOF

# 3. Demo data (seeds the aura-smart-home story: decisions, rejections, blockers)
.venv/bin/python -m backend.seed --project-id aura-smart-home

# 4. Run
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000**. (Opening `frontend/index.html` straight from disk also works — assets are relative and the page targets `127.0.0.1:8000` automatically.)

### The 60-second demo

1. **Overview** — project health, LIVE NOW ticker, decision feed.
2. **Ask Project** (`/`) — "Why did we reject Firebase?" → grounded answer, source cards, the retrieval chip showing real measured Moss ms.
3. **Conflict Check** — propose MongoDB → `DIRECT_CONFLICT` verdict citing the original decision.
4. **Feed Context** — paste `https://github.com/<any>/repo` → commits/issues/README become project memories in seconds.
5. Switch projects in **Settings** — each project is a fully separate context (and its own Moss index).

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness |
| `/state?project_id=` | GET | Full project state (tasks, blockers, counts, latest activity) |
| `/timeline?project_id=&type=&author=&limit=` | GET | Filterable activity feed |
| `/ingest` | POST | Ingest text → AI extraction → SQLite + Moss |
| `/ingest/github` | POST | Connect a public repo (commits, issues, README) |
| `/ask` | POST | Moss retrieval + grounded Gemini answer with citations and measured latency |
| `/check` | POST | Conflict detection against current project state |

## Layout

```
backend/
  main.py           FastAPI routes, static UI serving
  database.py       SQLite schema + queries (source of truth)
  moss_service.py   Moss index lifecycle: per-project indexes, pre-warm,
                    in-place sync, measured query path
  llm.py            Gemini client + extraction prompt + model fallback chain
  github_ingest.py  GitHub API connector
  seed.py           Demo data seeder
frontend/
  index.html        App shell (9 views)
  styles.css        Design system (graphite / electric-mint, editorial type)
  app.js            Views, router, keyboard, offline states — real data only
```

## Honest status

Built for a hackathon; the core loop (ingest → index → retrieve → grounded cited answer → conflict flag → resolve) is verified end-to-end with real credentials, including the conflict-resolution endpoint (supersede/keep). Not done yet: the 10,000-event scale benchmark and automated tests. Retrieval latency at this demo scale is single-digit-to-tens of milliseconds; the at-volume claim is the next thing to measure.

### Deployment note (Vercel/serverless)

The app deploys to Vercel (`vercel.json` included; set `SEED_ON_BOOT=1` so the demo project seeds into `/tmp` on cold start). One platform limitation: the Moss SDK ships native wheels and the current 0.25.x series has no `manylinux_2_34` wheel, so on Vercel's build image the wheel is skipped (environment marker in `requirements.txt`) and the app runs SQLite-only — browsing, ingestion and conflict recording work; `/ask` and `/check` return a clear "Moss SDK not available on this platform" error until InferEdge publishes a compatible wheel. Full retrieval (measured 4.4–8.7 ms) runs locally on macOS today. Data written on serverless instances lives only for the instance lifetime; point `PROJECTBRAIN_DB_PATH` at a hosted DB for persistence.
