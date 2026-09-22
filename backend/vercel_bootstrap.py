import os
import threading

"""
Serverless (Vercel) bootstrapping for ProjectBrain.

- Cold start: initializes a /tmp SQLite database (serverless filesystems are
  read-only except /tmp) and, with SEED_ON_BOOT=1, seeds the demo project.
- Warm start: no-op for the lifetime of the instance (_BOOTSTRAPPED flag).
- MOSS_AUTOSYNC=1 additionally indexes the seed into Moss in a background
  thread (async work cannot run on the request's event loop during boot).

Everything here is deliberately synchronous and event-loop-safe: serverless
platforms invoke the app inside a running loop where asyncio.run() fails.
"""

_BOOTSTRAPPED = False


def bootstrap() -> None:
    """Runs once per serverless instance, before the first response."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True

    from backend.database import DB_PATH, get_connection, init_db

    init_db()

    if os.environ.get("SEED_ON_BOOT", "").strip() != "1":
        return

    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE project_id = ?",
            ("aura-smart-home",),
        ).fetchone()

    if row and row["n"] > 0:
        return  # instance already seeded (warm start)

    try:
        from backend.seed import seed_sqlite

        seed_sqlite("aura-smart-home")
        print(f"Seeded demo data into {DB_PATH}")
    except Exception as e:  # never block a deploy on seeding
        print(f"Seed on boot skipped: {e}")
        return

    if os.environ.get("MOSS_AUTOSYNC", "").strip() == "1":
        # Moss calls are async; run them off the request loop in a throwaway
        # thread so a credit outage or slow network can't hang the response.
        def _index():
            try:
                asyncio_run(seed_index("aura-smart-home"))
            except Exception as e:
                print(f"Moss autosync skipped: {e}")

        def asyncio_run(coro):
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(coro)
            finally:
                loop.close()

        def seed_index(project_id):
            from backend.seed import run_seed
            return run_seed(project_id, index_into_moss=True)

        threading.Thread(target=_index, daemon=True).start()
