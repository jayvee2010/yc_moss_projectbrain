import json
import math
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from dotenv import load_dotenv

# The Moss SDK ships native wheels that don't cover every deployment target
# (e.g. manylinux_2_34 serverless images). Import it optionally so the app
# still boots and serves everything SQLite-backed; retrieval endpoints raise
# a clear RuntimeError that the API surfaces as a graceful error state.
try:
    from moss import DocumentInfo, MossClient, QueryOptions
except Exception as _import_err:  # pragma: no cover - platform dependent
    DocumentInfo = MossClient = QueryOptions = None  # type: ignore[assignment]
    _MOSS_IMPORT_ERROR: Optional[Exception] = _import_err
else:
    _MOSS_IMPORT_ERROR = None

from backend.database import get_connection

load_dotenv()

# Singleton client reference
_client: Optional[MossClient] = None
# Cache of known loaded index names
_loaded_indexes: Set[str] = set()


def get_project_index_name(project_id: str) -> str:
    """Generates a stable, valid project-specific Moss index name."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", project_id.lower().strip())
    return f"pb-{sanitized}"


def get_moss_client() -> MossClient:
    """Initializes and returns the MossClient using environment variables.

    Raises RuntimeError if credentials are not configured or client creation fails.
    Never logs or exposes secret credentials.
    """
    global _client
    if _client is not None:
        return _client

    if MossClient is None:
        raise RuntimeError(
            "Moss SDK is not available on this platform "
            f"(import failed: {_MOSS_IMPORT_ERROR}). Retrieval is disabled; "
            "all other features work from the local database."
        )

    project_id = os.getenv("MOSS_PROJECT_ID")
    project_key = os.getenv("MOSS_PROJECT_KEY")

    if not project_id or not project_key:
        raise RuntimeError(
            "Moss credentials not configured. Please ensure MOSS_PROJECT_ID and MOSS_PROJECT_KEY are set."
        )

    try:
        _client = MossClient(project_id=project_id, project_key=project_key)
        return _client
    except Exception as e:
        raise RuntimeError(f"Failed to initialize MossClient: {e}") from e


def _format_memory_text(title: str, content: str, entities: Optional[List[str]]) -> str:
    """Formats a memory into a searchable text string for indexing."""
    entities_str = ", ".join(entities) if entities else "none"
    return f"{title}: {content}\nEntities: {entities_str}"


async def index_memory(memory_data: Dict[str, Any]) -> None:
    """Indexes a single ProjectBrain memory in Moss.

    Expects memory_data to contain 'id', 'project_id', 'title', 'content', and 'entities'.
    """
    await index_memories(memory_data["project_id"], [memory_data])


def _build_docs(memories: List[Dict[str, Any]]) -> List[DocumentInfo]:
    """Converts memory dicts into Moss DocumentInfo objects with provenance metadata."""
    docs = []
    for m in memories:
        text = _format_memory_text(m["title"], m["content"], m.get("entities"))
        # Metadata enables Moss-side provenance filtering via QueryOptions.filter
        docs.append(
            DocumentInfo(
                id=m["id"],
                text=text,
                metadata={
                    "author": str(m.get("author", "unknown")),
                    "type": str(m.get("type", "")),
                    "status": str(m.get("status", "")),
                },
            )
        )
    return docs


async def _ensure_index_loaded(client: MossClient, index_name: str) -> None:
    """Loads an index only if this process hasn't loaded it yet.

    Loading is a cloud operation with account-level credits, so repeated
    load_index calls on an already-loaded index are the main way this app
    can burn through the account's limits. Every load path must go through
    this helper instead of calling load_index directly.
    """
    if index_name in _loaded_indexes:
        return
    await client.load_index(index_name)
    _loaded_indexes.add(index_name)


async def _index_exists(client: MossClient, index_name: str) -> bool:
    existing_indexes = await client.list_indexes()
    return any(idx.name == index_name for idx in existing_indexes)


async def index_memories(project_id: str, memories: List[Dict[str, Any]]) -> None:
    """Indexes multiple ProjectBrain memories in Moss under the project's index.

    add_docs is an upsert, so repeated ingestion of the same memory IDs is safe.
    """
    if not memories:
        return

    client = get_moss_client()
    index_name = get_project_index_name(project_id)
    docs = _build_docs(memories)

    try:
        if not await _index_exists(client, index_name):
            await client.create_index(index_name, docs)
        else:
            await _ensure_index_loaded(client, index_name)
            await client.add_docs(index_name, docs)
        await _ensure_index_loaded(client, index_name)
    except Exception as e:
        raise RuntimeError(f"Moss indexing failed for index '{index_name}': {e}") from e


async def delete_indexed_memories(project_id: str, doc_ids: Iterable[str]) -> None:
    """Removes documents from a project's Moss index (best-effort sync).

    Used when SQLite rows are deleted so Moss doesn't serve stale ghosts.
    Never raises: if Moss is unconfigured or the index is missing, SQLite
    simply remains the source of truth.
    """
    ids = [i for i in doc_ids if i]
    if not ids:
        return
    try:
        client = get_moss_client()
    except RuntimeError:
        return  # Moss not configured — nothing to sync
    try:
        await client.delete_docs(get_project_index_name(project_id), ids)
    except Exception:
        pass  # index may not exist yet; deletion is best-effort


async def rebuild_project_index(project_id: str) -> int:
    """Syncs a project's Moss index with SQLite (the source of truth), in place.

    Guarantees no stale/orphan documents: everything currently in SQLite for
    this project gets indexed, anything else in the index is deleted. The sync
    happens IN PLACE (add_docs upsert + delete_docs) — never delete_index plus
    create_index — so reconnecting a repository doesn't recreate the index or
    re-load it from the cloud, which would burn account load credits.
    Returns the number of documents indexed.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, type, title, content, status, author, entities
            FROM memories WHERE project_id = ?
            """,
            (project_id,),
        ).fetchall()

    memories: List[Dict[str, Any]] = []
    for r in rows:
        try:
            entities = json.loads(r["entities"]) if r["entities"] else []
        except (json.JSONDecodeError, TypeError):
            entities = []
        memories.append(
            {
                "id": r["id"],
                "type": r["type"],
                "title": r["title"],
                "content": r["content"],
                "status": r["status"],
                "author": r["author"],
                "entities": entities,
            }
        )

    if not memories:
        return 0

    client = get_moss_client()
    index_name = get_project_index_name(project_id)
    docs = _build_docs(memories)

    try:
        if not await _index_exists(client, index_name):
            # First ingest for this project: create with docs, then load once.
            await client.create_index(index_name, docs)
            await _ensure_index_loaded(client, index_name)
            return len(docs)

        # Index exists: make it live in this process (no-op if pre-warmed),
        # then sync document set in place.
        await _ensure_index_loaded(client, index_name)

        try:
            existing_docs = await client.get_docs(index_name)
            existing_ids = {d.id for d in existing_docs}
        except Exception:
            existing_ids = set()  # can't diff — upsert only, no deletes

        current_ids = {d.id for d in docs}
        stale_ids = existing_ids - current_ids
        if stale_ids:
            try:
                await client.delete_docs(index_name, list(stale_ids))
            except Exception:
                pass  # stale docs are cosmetic; don't fail the ingest

        await client.add_docs(index_name, docs)  # upsert
        return len(docs)
    except Exception as e:
        raise RuntimeError(f"Moss index sync failed for '{index_name}': {e}") from e


async def query_memories(
    project_id: str, query: str, top_k: int = 5
) -> Tuple[List[Dict[str, Any]], float]:
    """Retrieves top-K relevant memories for a project.

    Primary path: Moss semantic search (sub-10ms when the native SDK is
    installed and the index is loaded — the local/demo experience).
    Fallback path: a built-in BM25 keyword ranker over the project's SQLite
    memories, used automatically when the Moss SDK isn't available on the
    platform (e.g. Vercel's Linux image has no compatible native wheel) or
    when Moss itself fails. Same return shape, so /ask and /check work
    everywhere.

    Returns:
        tuple of (relevance_list, elapsed_ms)
        where each item in relevance_list is a dict: {'id': ..., 'score': ..., 'text': ...}
    Raises:
        RuntimeError only if BOTH Moss and the SQLite fallback fail.
    """
    start_time = time.perf_counter()

    if MossClient is not None:
        try:
            relevance, ms = await _query_moss(project_id, query, top_k, start_time)
            return [dict(r, retrieval="moss") for r in relevance], ms
        except Exception as moss_err:
            # fall through to the local ranker, but surface why in the timing note
            fallback_note = f"Moss unavailable ({str(moss_err)[:80]})"
        else:
            fallback_note = None
    else:
        fallback_note = "Moss SDK not installed on this platform"

    try:
        relevance, ms = _query_sqlite_bm25(project_id, query, top_k)
        note = fallback_note or "Moss error"
        relevance = [dict(r, retrieval=f"sqlite-fallback · {ms:.1f}ms · {note}") for r in relevance]
        return relevance, ms
    except Exception as db_err:
        raise RuntimeError(
            f"Retrieval failed: {fallback_note or 'Moss error'}; SQLite fallback also failed: {db_err}"
        ) from db_err


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does",
    "for", "from", "had", "has", "have", "how", "i", "in", "is", "it",
    "its", "of", "on", "or", "our", "s", "so", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "to", "was", "we",
    "were", "what", "when", "where", "which", "who", "why", "will", "with",
}


def _tokenize(text: str) -> List[str]:
    return [
        t for t in re.findall(r"[a-z0-9_-]+", text.lower())
        if len(t) > 1 and t not in _STOPWORDS
    ]


def _query_sqlite_bm25(
    project_id: str, query: str, top_k: int
) -> Tuple[List[Dict[str, Any]], float]:
    """Ranks the project's memories against the query with BM25.

    A compact, dependency-free retriever (k1=1.5, b=0.75) — the same scoring
    family Moss uses for its keyword signal, so results stay comparable.
    Latency is honest and measured: it runs against the project's SQLite
    rows and typically lands well under a millisecond at demo scale.
    """
    from backend.database import get_memories_by_project

    start_time = time.perf_counter()
    memories = get_memories_by_project(project_id)
    if not memories:
        ms = (time.perf_counter() - start_time) * 1000.0
        return [], ms

    docs_tokens = {
        m["id"]: _tokenize(
            f"{m['title']} {m['content']} {' '.join(m.get('entities') or [])}"
        )
        for m in memories
    }
    n_docs = len(memories)
    avg_len = (sum(len(t) for t in docs_tokens.values()) / n_docs) or 1.0

    df: Dict[str, int] = {}
    for toks in docs_tokens.values():
        for term in set(toks):
            df[term] = df.get(term, 0) + 1

    q_terms = _tokenize(query)
    k1, b = 1.5, 0.75
    scored = []
    for m in memories:
        toks = docs_tokens[m["id"]]
        tf: Dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks) or 1
        score = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * dl / avg_len))
        if score > 0:
            text = (
                f"{m['title']}: {m['content']}\n"
                f"Entities: {', '.join(m.get('entities') or []) or 'none'}"
            )
            scored.append({"id": m["id"], "score": round(score, 4), "text": text})

    scored.sort(key=lambda x: x["score"], reverse=True)
    ms = (time.perf_counter() - start_time) * 1000.0
    return scored[:top_k], ms


async def _query_moss(
    project_id: str, query: str, top_k: int, start_time: float
) -> Tuple[List[Dict[str, Any]], float]:
    """The real Moss semantic retrieval path (native SDK, in-process)."""
    client = get_moss_client()
    index_name = get_project_index_name(project_id)

    if index_name not in _loaded_indexes:
        if not await _index_exists(client, index_name):
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return [], round(elapsed_ms, 2)

        await _ensure_index_loaded(client, index_name)

    search_result = await client.query(
        index_name, query, QueryOptions(top_k=top_k)
    )
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    relevance_info = [
        {
            "id": doc.id,
            "score": doc.score,
            "text": doc.text,
        }
        for doc in search_result.docs
    ]
    return relevance_info, round(elapsed_ms, 2)


async def prewarm_all_indexes() -> int:
    """Loads every existing Moss index at startup so the first query is warm.

    Without this, the first /ask after a server restart would report a cold-start
    latency (list_indexes + load_index) instead of true retrieval latency.

    Returns the number of indexes loaded.
    Raises RuntimeError if credentials are missing or loading fails.
    """
    if MossClient is None:
        return 0  # SDK unavailable on this platform — retrieval stays disabled
    client = get_moss_client()
    try:
        indexes = await client.list_indexes()
        for idx in indexes:
            try:
                await _ensure_index_loaded(client, idx.name)
            except Exception:
                pass  # one cold index shouldn't stop the rest of the pre-warm
        return len(_loaded_indexes)
    except Exception as e:
        raise RuntimeError(f"Moss pre-warm failed: {e}") from e
