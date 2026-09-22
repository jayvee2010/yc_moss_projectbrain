"""Seed a demo project with a realistic AURA Smart Home story.

Inserts memories and tasks directly into SQLite (no Gemini needed) and indexes
them into Moss so /ask, /check and /state work immediately for the demo.

Usage (from the project root):
    .venv/bin/python -m backend.seed                 # project "aura-smart-home"
    .venv/bin/python -m backend.seed --project-id my-demo

The script is idempotent: seed-* rows are replaced on every run, and the same
document IDs are pushed to Moss so existing docs are overwritten, not duplicated.
"""

import argparse
import re
from datetime import datetime, timedelta, timezone

from backend.database import create_task, get_connection, init_db, insert_memory
from backend.moss_service import rebuild_project_index

# (id suffix, hours_ago, type, title, content, status, entities, author)
SEED_MEMORIES = [
    (
        "decision-001",
        54.0,
        "decision",
        "Supabase selected for authentication and database",
        "The team decided to use Supabase for authentication and data storage. "
        "PostgreSQL compatibility, row-level security and a managed OAuth flow were the deciding factors.",
        "active",
        ["supabase", "postgresql", "oauth"],
        "Jayvee",
    ),
    (
        "decision-002",
        56.0,
        "decision",
        "Firebase rejected as backend platform",
        "Firebase was rejected because the project requires direct PostgreSQL compatibility "
        "and the team wants full control over the backend. Vendor lock-in was also a concern.",
        "rejected",
        ["firebase"],
        "Jayvee",
    ),
    (
        "decision-003",
        50.0,
        "decision",
        "React + Vite chosen for the frontend",
        "The frontend will be React with Vite: fast HMR, a simple build pipeline and no server-side "
        "rendering requirements for the dashboard prototype.",
        "active",
        ["react", "vite"],
        "Teammate",
    ),
    (
        "decision-004",
        51.0,
        "decision",
        "Next.js rejected for the frontend",
        "Next.js was rejected because server-side rendering is unnecessary for the hardware "
        "dashboard prototype, and it would slow down iteration on the demo.",
        "rejected",
        ["nextjs"],
        "Teammate",
    ),
    (
        "decision-005",
        48.0,
        "decision",
        "ESP32-C6 chosen as the hardware controller",
        "ESP32-C6 is the main controller for the security sensors: built-in WiFi 6 and BLE are "
        "required for the wireless door/window sensors.",
        "active",
        ["esp32"],
        "Jayvee",
    ),
    (
        "decision-006",
        47.0,
        "decision",
        "Arduino Uno rejected for hardware",
        "Arduino Uno was rejected because it has no native WiFi or Bluetooth, which are mandatory "
        "for the wireless sensor design.",
        "rejected",
        ["arduino"],
        "GPT",
    ),
    (
        "experiment-001",
        30.0,
        "experiment",
        "Local JWT fallback experiment",
        "Claude experimented with self-hosted JWT authentication as a fallback. It works, but the "
        "team confirmed Supabase OAuth remains the primary approach once the redirect bug is fixed.",
        "completed",
        ["jwt", "supabase", "oauth"],
        "Claude",
    ),
    (
        "fact-001",
        24.0,
        "fact",
        "Sensor polling interval fixed at 2 seconds",
        "Motion and door sensors report every 2 seconds. Faster polling caused ESP32 watchdog "
        "resets during load testing.",
        "active",
        ["esp32"],
        "GPT",
    ),
    (
        "decision-007",
        8.0,
        "decision",
        "JWT adopted for API sessions",
        "API sessions use short-lived JWTs issued after the Supabase OAuth exchange; refresh is "
        "handled by the Supabase SDK.",
        "active",
        ["jwt", "supabase"],
        "Claude",
    ),
    (
        "blocker-001",
        3.0,
        "blocker",
        "Supabase OAuth callback returns wrong redirect URL",
        "The Supabase OAuth callback is redirecting to http://localhost:3000 instead of the "
        "configured production URL. Authentication implementation is blocked on this.",
        "blocked",
        ["supabase", "oauth"],
        "Claude",
    ),
]

# (id suffix, hours_ago, title, description, status, priority)
SEED_TASKS = [
    (
        "task-001",
        30.0,
        "Implement Supabase OAuth login flow",
        "Blocked by the OAuth callback redirect URL bug (see blocker-001).",
        "blocked",
        "high",
    ),
    (
        "task-002",
        40.0,
        "Build real-time sensor dashboard",
        "React + Vite dashboard showing live door/window/motion sensor states.",
        "in_progress",
        "medium",
    ),
    (
        "task-003",
        20.0,
        "ESP32 firmware for door sensor",
        "Baseline firmware: WiFi provisioning, 2s polling, tamper alert.",
        "todo",
        "medium",
    ),
    (
        "task-004",
        10.0,
        "Set up automated deployment",
        "CI pipeline for the FastAPI backend and Vite frontend.",
        "todo",
        "low",
    ),
]


def hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


async def run_seed(project_id: str, index_into_moss: bool = True) -> None:
    init_db()

    with get_connection() as conn:
        conn.execute(
            "DELETE FROM memories WHERE id LIKE 'seed-%' AND project_id = ?", (project_id,)
        )
        conn.execute("DELETE FROM tasks WHERE id LIKE 'seed-%' AND project_id = ?", (project_id,))

    memories = []
    # Project-scope ids: memory ids are globally unique, so seeding the demo
    # story into a second project must not collide with the first one's rows.
    safe_project = re.sub(r"[^a-zA-Z0-9_-]", "-", project_id.lower().strip())
    for i, (mem_id, h, m_type, title, content, status, entities, author) in enumerate(
        SEED_MEMORIES, 1
    ):
        memory_data = {
            "id": f"seed-{safe_project}-{mem_id}",
            "project_id": project_id,
            "type": m_type,
            "title": title,
            "content": content,
            "status": status,
            "author": author,
            "entities": entities,
            "created_at": hours_ago(h),
        }
        insert_memory(memory_data)
        memories.append(memory_data)

    for task_id, h, title, description, status, priority in SEED_TASKS:
        created_at = hours_ago(h)
        create_task(
            {
                "id": f"seed-{safe_project}-{task_id}",
                "project_id": project_id,
                "title": title,
                "description": description,
                "status": status,
                "priority": priority,
                "created_at": created_at,
                "updated_at": created_at,
            }
        )

    print(f"Seeded {len(memories)} memories and {len(SEED_TASKS)} tasks into '{project_id}'.")

    if not index_into_moss:
        print("Skipping Moss indexing (index_into_moss=False) — data is SQLite-only.")
        return

    try:
        count = await rebuild_project_index(project_id)
        print(f"Indexed {count} memories into Moss (index rebuilt and warm).")
    except RuntimeError as e:
        print(f"WARNING: Moss indexing failed: {e}")
        print("Data is in SQLite (/state works), but /ask and /check need Moss indexing to succeed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the ProjectBrain demo project.")
    parser.add_argument(
        "--project-id", default="aura-smart-home", help="Project id to seed (default: aura-smart-home)"
    )
    args = parser.parse_args()

    import asyncio

    asyncio.run(run_seed(args.project_id))


if __name__ == "__main__":
    main()
