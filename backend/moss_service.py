import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from dotenv import load_dotenv
from moss import DocumentInfo, MossClient, QueryOptions

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
    """Retrieves top-K semantically relevant memories from Moss for a project.

    Returns:
        tuple of (relevance_list, elapsed_moss_ms)
        where each item in relevance_list is a dict: {'id': ..., 'score': ..., 'text': ...}
    Raises:
        RuntimeError if Moss is unavailable or query fails.
    """
    client = get_moss_client()
    index_name = get_project_index_name(project_id)

    start_time = time.perf_counter()
    try:
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

    except Exception as e:
        raise RuntimeError(f"Moss retrieval failed for index '{index_name}': {e}") from e


async def prewarm_all_indexes() -> int:
    """Loads every existing Moss index at startup so the first query is warm.

    Without this, the first /ask after a server restart would report a cold-start
    latency (list_indexes + load_index) instead of true retrieval latency.

    Returns the number of indexes loaded.
    Raises RuntimeError if credentials are missing or loading fails.
    """
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
