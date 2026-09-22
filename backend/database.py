import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


# Location of the SQLite database.
# Serverless hosts (e.g. Vercel) have a read-only filesystem except /tmp,
# so when PROJECTBRAIN_DB_PATH points at /tmp the seed runs on cold start.
_default_db_path = Path(__file__).resolve().parent.parent / "projectbrain.db"
DB_PATH = Path(os.environ.get("PROJECTBRAIN_DB_PATH", str(_default_db_path)))


def get_connection(db_path: Union[Path, str] = DB_PATH) -> sqlite3.Connection:
    """Creates a connection to the SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Union[Path, str] = DB_PATH) -> None:
    """Initializes the database schema and creates tables if they do not exist."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT 'unknown',
                    entities TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_project_id
                ON memories (project_id);
                """
            )
            # Lightweight migration for databases created before author existed
            cursor.execute("PRAGMA table_info(memories);")
            existing_columns = {row[1] for row in cursor.fetchall()}
            if "author" not in existing_columns:
                cursor.execute(
                    "ALTER TABLE memories ADD COLUMN author TEXT NOT NULL DEFAULT 'unknown';"
                )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL,
                    priority TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_project_id
                ON tasks (project_id);
                """
            )
    except sqlite3.Error as e:
        raise RuntimeError(f"Failed to initialize database: {e}") from e


def insert_memory(memory_data: Dict[str, Any], db_path: Union[Path, str] = DB_PATH) -> None:
    """Inserts a single memory record into the memories table."""
    entities_val = memory_data.get("entities")
    if isinstance(entities_val, (list, dict)):
        entities_json = json.dumps(entities_val)
    elif entities_val is None:
        entities_json = "[]"
    else:
        entities_json = str(entities_val)

    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO memories (
                    id, project_id, type, title, content, status, author, entities, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    memory_data["id"],
                    memory_data["project_id"],
                    memory_data["type"],
                    memory_data["title"],
                    memory_data["content"],
                    memory_data["status"],
                    memory_data.get("author", "unknown"),
                    entities_json,
                    memory_data["created_at"],
                ),
            )
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error while inserting memory: {e}") from e


def update_memory_status(
    memory_id: str, new_status: str, db_path: Union[Path, str] = DB_PATH
) -> None:
    """Updates a memory's status (e.g. marking a decision superseded)."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE memories SET status = ? WHERE id = ?;",
                (new_status, memory_id),
            )
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error while updating memory status: {e}") from e


def get_memories_by_project(
    project_id: str, db_path: Union[Path, str] = DB_PATH
) -> List[Dict[str, Any]]:
    """Retrieves all memories associated with a specific project_id."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, project_id, type, title, content, status, author, entities, created_at
                FROM memories
                WHERE project_id = ?
                ORDER BY created_at DESC;
                """,
                (project_id,),
            )
            rows = cursor.fetchall()

        results = []
        for row in rows:
            raw_entities = row["entities"]
            entities_list = []
            if raw_entities:
                try:
                    entities_list = json.loads(raw_entities)
                except (json.JSONDecodeError, TypeError):
                    entities_list = [raw_entities]

            results.append(
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "type": row["type"],
                    "title": row["title"],
                    "content": row["content"],
                    "status": row["status"],
                    "author": row["author"],
                    "entities": entities_list,
                    "created_at": row["created_at"],
                }
            )
        return results
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error while retrieving memories: {e}") from e


def get_memories_by_ids(
    memory_ids: List[str], db_path: Union[Path, str] = DB_PATH
) -> List[Dict[str, Any]]:
    """Retrieves complete memory records by a list of IDs, preserving the requested ID order."""
    if not memory_ids:
        return []

    placeholders = ",".join("?" for _ in memory_ids)
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT id, project_id, type, title, content, status, author, entities, created_at
                FROM memories
                WHERE id IN ({placeholders});
                """,
                memory_ids,
            )
            rows = cursor.fetchall()

        by_id = {}
        for row in rows:
            raw_entities = row["entities"]
            entities_list = []
            if raw_entities:
                try:
                    entities_list = json.loads(raw_entities)
                except (json.JSONDecodeError, TypeError):
                    entities_list = [raw_entities]

            by_id[row["id"]] = {
                "id": row["id"],
                "project_id": row["project_id"],
                "type": row["type"],
                "title": row["title"],
                "content": row["content"],
                "status": row["status"],
                "author": row["author"],
                "entities": entities_list,
                "created_at": row["created_at"],
            }

        # Preserve the original order of memory_ids (e.g. ranked by relevance)
        return [by_id[mid] for mid in memory_ids if mid in by_id]
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error while retrieving memories by IDs: {e}") from e


def create_task(
    task_data: Dict[str, Any], db_path: Union[Path, str] = DB_PATH
) -> Dict[str, Any]:
    """Creates a new task in the tasks table."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO tasks (
                    id, project_id, title, description, status, priority, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    task_data["id"],
                    task_data["project_id"],
                    task_data["title"],
                    task_data.get("description"),
                    task_data["status"],
                    task_data.get("priority"),
                    task_data["created_at"],
                    task_data["updated_at"],
                ),
            )
        return task_data
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error while creating task: {e}") from e


def get_tasks_for_project(
    project_id: str, db_path: Union[Path, str] = DB_PATH
) -> List[Dict[str, Any]]:
    """Retrieves all tasks for a project ordered by created_at DESC."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, project_id, title, description, status, priority, created_at, updated_at
                FROM tasks
                WHERE project_id = ?
                ORDER BY created_at DESC;
                """,
                (project_id,),
            )
            rows = cursor.fetchall()

        return [dict(row) for row in rows]
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error while retrieving tasks: {e}") from e


def update_task_status(
    task_id: str,
    status: str,
    updated_at: Optional[str] = None,
    db_path: Union[Path, str] = DB_PATH,
) -> Optional[Dict[str, Any]]:
    """Updates the status and updated_at timestamp of a task."""
    from datetime import datetime, timezone

    if updated_at is None:
        updated_at = datetime.now(timezone.utc).isoformat()

    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?
                WHERE id = ?;
                """,
                (status, updated_at, task_id),
            )
            if cursor.rowcount == 0:
                return None
            cursor.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error while updating task status: {e}") from e


def get_recent_of_type(
    project_id: str,
    memory_type: str,
    limit: int = 10,
    db_path: Union[Path, str] = DB_PATH,
) -> List[Dict[str, Any]]:
    """Retrieves recent memories of a given type (e.g. 'change') for a project."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, project_id, type, title, content, status, author, entities, created_at
                FROM memories
                WHERE project_id = ? AND lower(type) = ?
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                (project_id, memory_type.lower(), limit),
            )
            rows = cursor.fetchall()

        results = []
        for row in rows:
            raw_entities = row["entities"]
            entities_list = []
            if raw_entities:
                try:
                    entities_list = json.loads(raw_entities)
                except (json.JSONDecodeError, TypeError):
                    entities_list = [raw_entities]
            results.append(
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "type": row["type"],
                    "title": row["title"],
                    "content": row["content"],
                    "status": row["status"],
                    "author": row["author"],
                    "entities": entities_list,
                    "created_at": row["created_at"],
                }
            )
        return results
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error while retrieving memories of type: {e}") from e


def get_recent_memories_feed(
    project_id: str,
    types: Optional[List[str]] = None,
    author_contains: Optional[str] = None,
    limit: int = 40,
    db_path: Union[Path, str] = DB_PATH,
) -> List[Dict[str, Any]]:
    """Unified recent-activity feed across memory types (newest first)."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            where = "project_id = ?"
            params: List[Any] = [project_id]
            if types:
                placeholders = ",".join("?" for _ in types)
                where += f" AND lower(type) IN ({placeholders})"
                params.extend(t.lower() for t in types)
            if author_contains:
                where += " AND lower(author) LIKE ?"
                params.append(f"%{author_contains.lower()}%")
            params.append(limit)
            cursor.execute(
                f"""
                SELECT id, project_id, type, title, content, status, author, entities, created_at
                FROM memories
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                tuple(params),
            )
            rows = cursor.fetchall()

        results = []
        for row in rows:
            raw_entities = row["entities"]
            entities_list = []
            if raw_entities:
                try:
                    entities_list = json.loads(raw_entities)
                except (json.JSONDecodeError, TypeError):
                    entities_list = [raw_entities]
            results.append(
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "type": row["type"],
                    "title": row["title"],
                    "content": row["content"],
                    "status": row["status"],
                    "author": row["author"],
                    "entities": entities_list,
                    "created_at": row["created_at"],
                    "source": "memory",
                }
            )
        return results
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error while retrieving activity feed: {e}") from e


def get_project_stats(
    project_id: str, db_path: Union[Path, str] = DB_PATH
) -> Dict[str, Any]:
    """Aggregate counts for the overview dashboard (memories by type + tasks)."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT type, COUNT(*) AS n
                FROM memories
                WHERE project_id = ?
                GROUP BY type;
                """,
                (project_id,),
            )
            by_type = {row["type"]: row["n"] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE project_id = ?;", (project_id,)
            )
            total = cursor.fetchone()["n"]
            cursor.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND status = 'blocked';",
                (project_id,),
            )
            blocked = cursor.fetchone()["n"]
        return {"total_memories": total, "by_type": by_type, "blocked_tasks": blocked}
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error while computing project stats: {e}") from e


def get_recent_decisions_for_project(
    project_id: str, limit: int = 10, db_path: Union[Path, str] = DB_PATH
) -> List[Dict[str, Any]]:
    """Retrieves recent decision memories for a project from the memories table."""
    try:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, project_id, type, title, content, status, author, entities, created_at
                FROM memories
                WHERE project_id = ? AND lower(type) = 'decision'
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                (project_id, limit),
            )
            rows = cursor.fetchall()

        results = []
        for row in rows:
            raw_entities = row["entities"]
            entities_list = []
            if raw_entities:
                try:
                    entities_list = json.loads(raw_entities)
                except (json.JSONDecodeError, TypeError):
                    entities_list = [raw_entities]
            results.append(
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "type": row["type"],
                    "title": row["title"],
                    "content": row["content"],
                    "status": row["status"],
                    "author": row["author"],
                    "entities": entities_list,
                    "created_at": row["created_at"],
                }
            )
        return results
    except sqlite3.Error as e:
        raise RuntimeError(
            f"Database error while retrieving recent decisions: {e}"
        ) from e


