import os

"""
Vercel entrypoint for ProjectBrain.

- Cold start: seeds the demo project into a /tmp SQLite database (serverless
  filesystems are read-only except /tmp) and optionally indexes it into Moss.
- Warm start: reuses the existing /tmp database for the lifetime of the
  instance.

Enable auto-seeding by setting SEED_ON_BOOT=1 in Vercel environment variables.
The auto-seed skips Moss indexing unless MOSS_AUTOSYNC=1 is also set, so a
Moss outage or credit limit never breaks a deploy.
"""

_BOOTSTRAPPED = False


def bootstrap() -> None:
    """Runs once per serverless instance, before the first request."""
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
        import asyncio

        from backend import seed

        asyncio.run(seed.run_seed("aura-smart-home", index_into_moss=os.environ.get("MOSS_AUTOSYNC", "").strip() == "1"))
        print(f"Seeded demo data into {DB_PATH}")
    except Exception as e:  # never block a deploy on seeding
        print(f"Seed on boot skipped: {e}")
