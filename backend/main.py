from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.database import (
    get_memories_by_ids,
    get_project_stats,
    get_recent_decisions_for_project,
    get_recent_memories_feed,
    get_recent_of_type,
    get_tasks_for_project,
    init_db,
    insert_memory,
    update_memory_status,
)
from backend.github_ingest import GitHubError, ingest_github_repo
from backend.llm import generate, extract_memories
from backend.schemas import ResolveRequest
from backend.moss_service import (
    index_memories,
    prewarm_all_indexes,
    query_memories,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Local/uvicorn path: initialize here. On Vercel, ASGI lifespan does not
    # run, so bootstrap also happens per-request via the middleware below.
    if os.environ.get("VERCEL") == "1":
        from backend.vercel_bootstrap import bootstrap
        bootstrap()

    # Initialize SQLite database and tables on startup
    init_db()

    # Pre-warm every existing Moss index so the first query reports true latency
    try:
        loaded = await prewarm_all_indexes()
        print(f"Pre-warmed {loaded} Moss index(es)")
    except RuntimeError as e:
        print(f"Moss pre-warm skipped: {e}")

    yield


app = FastAPI(title="ProjectBrain API", lifespan=lifespan)


@app.middleware("http")
async def serverless_bootstrap(request, call_next):
    """Serverless safety net: ASGI lifespan doesn't run on Vercel, so the
    /tmp database init + demo seeding must happen on the first request.
    bootstrap() is idempotent (guarded by a module flag), so this is a
    no-op after the first call on any instance."""
    if os.environ.get("VERCEL") == "1":
        from backend.vercel_bootstrap import bootstrap
        bootstrap()
    return await call_next(request)

# Allow the demo frontend (any origin for the hackathon) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class IngestRequest(BaseModel):
    project_id: str
    text: str
    author: str = "unknown"


class GitHubIngestRequest(BaseModel):
    project_id: str
    repo: str  # "owner/name" or a full GitHub URL
    max_commits: int = 25
    max_issues: int = 15
    use_llm: bool = True


class AskRequest(BaseModel):
    project_id: str
    question: str


class CheckRequest(BaseModel):
    project_id: str
    action: str


def check_deterministic_conflict(
    action: str, memories: List[Dict[str, Any]]
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Deterministically checks if a proposed action directly conflicts with a rejected/superseded approach."""
    action_lower = action.lower()
    positive_indicators = [
        "use", "using", "adopt", "adopting", "implement", "implementing",
        "primary", "select", "selecting", "choose", "choosing", "switch to",
        "deploy", "deploying", "standardize"
    ]
    is_positive_proposal = any(
        re.search(rf"\b{re.escape(w)}\b", action_lower) for w in positive_indicators
    )

    for mem in memories:
        status = str(mem.get("status", "")).lower()
        m_type = str(mem.get("type", "")).lower()
        is_rejected = (
            status in ("rejected", "superseded")
            or "rejected" in m_type
            or "superseded" in m_type
        )
        if not is_rejected:
            continue

        entities = [
            str(e).lower().strip()
            for e in mem.get("entities", [])
            if e and len(str(e).strip()) > 1
        ]
        matched_term = None
        for entity in entities:
            if entity in ("ocr", "database", "api", "ai", "backend", "frontend", "pipeline"):
                continue
            if re.search(rf"\b{re.escape(entity)}\b", action_lower):
                matched_term = entity
                break

        if not matched_term:
            title_words = [
                w.lower()
                for w in re.findall(r"\b[A-Za-z0-9_-]{3,}\b", mem.get("title", ""))
                if w.lower() not in (
                    "rejection", "rejected", "approach", "choice",
                    "decision", "for", "the", "and", "ocr", "with"
                )
            ]
            for tw in title_words:
                if re.search(rf"\b{re.escape(tw)}\b", action_lower):
                    matched_term = tw
                    break

        if matched_term and is_positive_proposal:
            reason = (
                f"The proposed action directly contradicts a previously rejected approach. "
                f"'{matched_term.capitalize()}' was rejected in memory '{mem['title']}': {mem['content']}"
            )
            return reason, mem

    return None



FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
def home():
    """Serves the demo UI."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/styles.css")
def styles():
    return FileResponse(FRONTEND_DIR / "styles.css", media_type="text/css")


@app.get("/app.js")
def app_js():
    return FileResponse(FRONTEND_DIR / "app.js", media_type="application/javascript")


@app.get("/health")
def health():
    return {"message": "ProjectBrain backend is running!"}


@app.get("/timeline")
def timeline(
    project_id: str,
    types: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = 40,
):
    """Unified activity feed across memory types, newest first.

    Optional filters: types=decision,change,blocker (comma-separated), author=substring.
    """
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id parameter is required")
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
    try:
        events = get_recent_memories_feed(
            project_id, types=type_list, author_contains=author, limit=min(limit, 200)
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"project_id": project_id, "events": events}


@app.post("/ingest")
async def ingest(request: IngestRequest):
    start_total = time.perf_counter()

    # Ask Gemini to extract important project memories (async: never blocks the event loop)
    llm_start = time.perf_counter()
    extracted = await extract_memories(request.text)
    llm_ms = (time.perf_counter() - llm_start) * 1000.0

    memories = []

    # Add ProjectBrain-specific information to each memory
    for memory in extracted.memories:

        memory_data = {
            "id": f"mem_{uuid.uuid4().hex[:8]}",
            "project_id": request.project_id,
            "type": memory.type,
            "title": memory.title,
            "content": memory.content,
            "status": memory.status,
            "author": request.author,
            "entities": memory.entities,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            insert_memory(memory_data)
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

        memories.append(memory_data)

    # Index saved memories into Moss — best-effort: SQLite is the source of
    # truth, so a Moss outage/credit limit/platform gap must not lose data
    # (mirrors /resolve and /ingest/github behavior).
    moss_warning: Optional[str] = None
    if memories:
        try:
            await index_memories(request.project_id, memories)
        except RuntimeError as e:
            moss_warning = str(e)

    total_ms = (time.perf_counter() - start_total) * 1000.0

    return {
        "success": True,
        "memories": memories,
        "moss_warning": moss_warning,
        "timings": {
            "llm_ms": round(llm_ms, 2),
            "total_ms": round(total_ms, 2),
        },
    }


@app.post("/ingest/github")
async def ingest_github(request: GitHubIngestRequest):
    """Connects a public GitHub repository to a project: ingests commits,
    open issues and README, then infers higher-level insights with Gemini."""
    try:
        summary = await ingest_github_repo(
            request.project_id,
            request.repo,
            max_commits=request.max_commits,
            max_issues=request.max_issues,
            use_llm=request.use_llm,
        )
    except GitHubError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"success": True, **summary}


@app.post("/ask")
async def ask(request: AskRequest):
    start_total = time.perf_counter()

    # 1. Query Moss for the top 5 relevant memories
    moss_start = time.perf_counter()
    try:
        relevance_info, _ = await query_memories(
            request.project_id, request.question, top_k=5
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    moss_ms = (time.perf_counter() - moss_start) * 1000.0

    # 2. Use returned memory IDs to retrieve complete memory objects from SQLite
    context_start = time.perf_counter()
    memory_ids = [item["id"] for item in relevance_info]
    try:
        sources = get_memories_by_ids(memory_ids)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # 3. Build a concise context containing those memories
    context_blocks = []
    for idx, mem in enumerate(sources, 1):
        entities_str = (
            ", ".join(mem.get("entities", [])) if mem.get("entities") else "none"
        )
        context_blocks.append(
            f"Memory {idx} [{mem['type'].upper()}] - {mem['title']}\n"
            f"Status: {mem['status']}\n"
            f"Recorded by: {mem.get('author', 'unknown')}\n"
            f"Content: {mem['content']}\n"
            f"Entities: {entities_str}"
        )

    context = (
        "\n\n".join(context_blocks)
        if context_blocks
        else "No relevant project memories found."
    )
    context_ms = (time.perf_counter() - context_start) * 1000.0

    # 4. Send context + user question to Gemini
    llm_start = time.perf_counter()
    prompt = f"""You are the ProjectBrain assistant. Answer the user's question using ONLY the provided project memories as context. If the answer cannot be found in the memories, state that clearly.

Project Memories:
{context}

Question: {request.question}

Provide a concise, accurate answer based on the project memories above."""

    try:
        response = await generate(prompt)
        answer = response.text
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM generation failed: {e}")
    llm_ms = (time.perf_counter() - llm_start) * 1000.0

    total_ms = (time.perf_counter() - start_total) * 1000.0

    # 5. Return answer, sources, and timings
    return {
        "answer": answer,
        "sources": sources,
        "timings": {
            "moss_ms": round(moss_ms, 2),
            "context_ms": round(context_ms, 2),
            "llm_ms": round(llm_ms, 2),
            "total_ms": round(total_ms, 2),
        },
    }


@app.get("/state")
def get_state(project_id: str):
    """Returns the live project state including tasks, blockers, and recent decisions from SQLite."""
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id parameter is required")

    try:
        tasks = get_tasks_for_project(project_id)
        blockers = [t for t in tasks if t.get("status") == "blocked"]
        recent_decisions = get_recent_decisions_for_project(project_id)
        recent_changes = get_recent_of_type(project_id, "change", limit=12)
        stats = get_project_stats(project_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "project_id": project_id,
        "tasks": tasks,
        "blockers": blockers,
        "recent_decisions": recent_decisions,
        "recent_changes": recent_changes,
        "stats": stats,
    }


@app.post("/check")
async def check_action(request: CheckRequest):
    """Evaluates whether a proposed action conflicts with existing project memories.

    Uses Moss for semantic retrieval, SQLite for hydration, deterministic checks first,
    and Gemini as a judge if ambiguous.
    """
    start_total = time.perf_counter()

    # 1. Query Moss for the top 5 relevant memories
    moss_start = time.perf_counter()
    try:
        relevance_info, _ = await query_memories(
            request.project_id, request.action, top_k=5
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    moss_ms = (time.perf_counter() - moss_start) * 1000.0

    # 2. Hydrate the memories from SQLite
    check_start = time.perf_counter()
    memory_ids = [item["id"] for item in relevance_info]
    try:
        sources = get_memories_by_ids(memory_ids)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # 3. Deterministic conflict checks
    deterministic_match = check_deterministic_conflict(request.action, sources)
    if deterministic_match:
        reason, conflicting_mem = deterministic_match
        check_ms = (time.perf_counter() - check_start) * 1000.0
        total_ms = (time.perf_counter() - start_total) * 1000.0
        return {
            "conflict": True,
            "level": "DIRECT_CONFLICT",
            "reason": reason,
            "conflicting_memory": conflicting_mem,
            "timings": {
                "moss_ms": round(moss_ms, 2),
                "check_ms": round(check_ms, 2),
                "total_ms": round(total_ms, 2),
            },
        }

    # If no memories exist for project
    if not sources:
        check_ms = (time.perf_counter() - check_start) * 1000.0
        total_ms = (time.perf_counter() - start_total) * 1000.0
        return {
            "conflict": False,
            "level": "NO_CONFLICT",
            "reason": "No relevant project memories found to evaluate.",
            "conflicting_memory": None,
            "timings": {
                "moss_ms": round(moss_ms, 2),
                "check_ms": round(check_ms, 2),
                "total_ms": round(total_ms, 2),
            },
        }

    # 4. If not deterministic, use Gemini as a judge
    context_blocks = []
    for idx, mem in enumerate(sources, 1):
        entities_str = (
            ", ".join(mem.get("entities", [])) if mem.get("entities") else "none"
        )
        context_blocks.append(
            f"[{mem['id']}] [{mem['type'].upper()}] {mem['title']} (Status: {mem['status']})\n"
            f"Content: {mem['content']}\n"
            f"Entities: {entities_str}"
        )
    context = "\n\n".join(context_blocks)

    judge_prompt = f"""You are a conflict detection judge for the software project '{request.project_id}'.
Evaluate whether the proposed action contradicts or creates a conflict with previously established project memories, decisions, facts, or architecture choices.

Proposed Action:
"{request.action}"

Relevant Project Memories:
{context}

Analyze the relationship between the proposed action and these memories carefully:
- Return level "NO_CONFLICT" if the action aligns with or does not contradict any project memories.
- Return level "POTENTIAL_CONFLICT" if the action might conflict, is partially inconsistent, or reopens a settled decision with ambiguity.
- Return level "DIRECT_CONFLICT" if the action directly contradicts an active decision, architecture choice, or previously rejected approach.
- Do not make unsupported assumptions.

Return valid JSON with this exact schema:
{{
  "level": "NO_CONFLICT" | "POTENTIAL_CONFLICT" | "DIRECT_CONFLICT",
  "reason": "Clear explanation of the evaluation",
  "conflicting_memory_id": "memory ID if conflicting or null"
}}"""

    try:
        response = await generate(
            judge_prompt,
            config={
                "response_mime_type": "application/json",
            },
        )
        data = json.loads(response.text)
        level = data.get("level", "NO_CONFLICT")
        if level not in ("NO_CONFLICT", "POTENTIAL_CONFLICT", "DIRECT_CONFLICT"):
            level = "POTENTIAL_CONFLICT" if data.get("conflict") else "NO_CONFLICT"

        conflict = level != "NO_CONFLICT"
        reason = data.get("reason", "Evaluation completed.")
        conflicting_id = data.get("conflicting_memory_id")
        conflicting_mem = next((m for m in sources if m["id"] == conflicting_id), None)
        if conflict and not conflicting_mem and sources:
            conflicting_mem = sources[0]
        elif not conflict:
            conflicting_mem = None

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Conflict evaluation failed: {e}")

    check_ms = (time.perf_counter() - check_start) * 1000.0
    total_ms = (time.perf_counter() - start_total) * 1000.0

    return {
        "conflict": conflict,
        "level": level,
        "reason": reason,
        "conflicting_memory": conflicting_mem,
        "timings": {
            "moss_ms": round(moss_ms, 2),
            "check_ms": round(check_ms, 2),
            "total_ms": round(total_ms, 2),
        },
    }


@app.post("/resolve")
async def resolve_conflict(request: ResolveRequest):
    """Resolves a conflict detected by /check — the backend half of the
    Conflicts page's actions.

    resolution = "supersede": the current decision is marked 'superseded'
      and the proposal is recorded as the new active 'decision' (with an
      audit 'change' event linking back to the replaced memory).
    resolution = "keep": the proposal is recorded as a REJECTED decision
      so future identical proposals hit deterministic conflict detection —
      the system learns from the team's answer.

    SQLite is the source of truth: a Moss sync failure degrades to a
    warning instead of losing the resolution.
    """
    resolution = request.resolution.strip().lower()
    if resolution not in ("supersede", "keep"):
        raise HTTPException(status_code=422, detail="resolution must be 'supersede' or 'keep'")
    if not request.action.strip():
        raise HTTPException(status_code=422, detail="action must not be empty")

    start_total = time.perf_counter()
    now = datetime.now(timezone.utc).isoformat()
    author = request.author.strip() or "unknown"

    # Optional current decision being replaced (validates project ownership)
    replaced: Optional[Dict[str, Any]] = None
    if request.conflicting_memory_id:
        matches = get_memories_by_ids([request.conflicting_memory_id])
        replaced = next((m for m in matches if m["project_id"] == request.project_id), None)
        if replaced is None:
            raise HTTPException(status_code=404, detail="conflicting_memory_id not found in this project")

    rationale = request.rationale.strip()

    if resolution == "supersede":
        if replaced is not None:
            try:
                update_memory_status(replaced["id"], "superseded")
            except RuntimeError as e:
                raise HTTPException(status_code=500, detail=str(e))

        new_decision = {
            "id": f"decision-{uuid.uuid4().hex[:8]}",
            "project_id": request.project_id,
            "type": "decision",
            "title": (rationale[:60] if rationale else request.action.strip()),
            "content": (
                f"{request.action.strip()}"
                + (f" Rationale: {rationale}" if rationale else "")
                + (f" (replaces: {replaced['title']})" if replaced else "")
            ),
            "status": "active",
            "author": author,
            "entities": [],
            "created_at": now,
        }
        audit_event = {
            "id": f"change-{uuid.uuid4().hex[:8]}",
            "project_id": request.project_id,
            "type": "change",
            "title": f"Decision superseded: {replaced['title']}" if replaced else "New decision recorded",
            "content": (
                f"{author} superseded '{replaced['title']}' with: {request.action.strip()}"
                if replaced
                else f"{author} recorded a new decision: {request.action.strip()}"
            ),
            "status": "completed",
            "author": author,
            "entities": [],
            "created_at": now,
        }
        memories = [new_decision, audit_event]
    else:  # keep
        rejected_decision = {
            "id": f"rejected-{uuid.uuid4().hex[:8]}",
            "project_id": request.project_id,
            "type": "rejected_approach",
            "title": f"Rejected: {request.action.strip()[:60]}",
            "content": (
                f"Proposed '{request.action.strip()}' was rejected during conflict check."
                + (f" Reason: {rationale}" if rationale else "")
                + (f" Team kept the existing decision: {replaced['title']}." if replaced else "")
            ),
            "status": "rejected",
            "author": author,
            "entities": [],
            "created_at": now,
        }
        memories = [rejected_decision]

    # Persist to SQLite (source of truth)
    try:
        for m in memories:
            insert_memory(m)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Best-effort Moss sync — never lose a resolution over retrieval-layer credits
    moss_warning: Optional[str] = None
    try:
        await index_memories(request.project_id, memories)
    except RuntimeError as e:
        moss_warning = str(e)

    total_ms = (time.perf_counter() - start_total) * 1000.0
    return {
        "success": True,
        "resolution": resolution,
        "recorded": memories,
        "superseded_memory_id": replaced["id"] if replaced else None,
        "moss_warning": moss_warning,
        "timings": {"total_ms": round(total_ms, 2)},
    }

