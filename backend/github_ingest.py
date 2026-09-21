"""GitHub repository ingestion — the "enter any project" path.

Fetches a public repository's recent activity from the GitHub REST API and
converts it into ProjectBrain memories:

- one "fact" memory for the repo overview          (author: "github")
- one "change" memory per recent commit            (author: the git commit author — real provenance)
- one "task"/"blocker" memory per open issue       (author: the issue reporter — bug-labeled issues become blockers)
- one LLM pass that infers higher-level decisions / facts / experiments
  from the README + commit history                 (author: "inferred (AI)" — honest, distinguishable provenance)

Everything lands in SQLite and is indexed into Moss, so /ask, /check and /state
immediately work for any public repository.

Optional: set GITHUB_TOKEN in .env to raise the GitHub API rate limit
(unauthenticated: 60 requests/hour) and to access private repos.
"""

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

from backend.database import get_connection, insert_memory
from backend.llm import extract_memories
from backend.moss_service import rebuild_project_index

load_dotenv()

GITHUB_API = "https://api.github.com"
BUG_LABELS = {"bug", "blocked", "critical", "regression", "breaking"}


class GitHubError(RuntimeError):
    """User-facing GitHub ingestion error (bad repo name, rate limit, ...)."""


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ProjectBrain-demo"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def repo_slug(repo: str) -> str:
    """Accepts 'owner/name', full GitHub URLs, and optional '.git' suffix."""
    slug = repo.strip()
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if slug.startswith(prefix):
            slug = slug[len(prefix):]
    slug = slug.removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        raise GitHubError("Repository must be in the form 'owner/name' (a public GitHub repo).")
    return slug


def _iso(ts: Optional[str]) -> str:
    """Normalizes GitHub ISO timestamps ('...Z') to '+00:00' ISO for SQLite ordering."""
    if not ts:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


async def _get(client: httpx.AsyncClient, url: str, params: Optional[Dict[str, Any]] = None):
    resp = await client.get(url, params=params, headers=_headers(), timeout=30.0)
    if resp.status_code == 404:
        raise GitHubError(f"Not found on GitHub: {url} — is the repo public and the name correct?")
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        raise GitHubError("GitHub API rate limit exceeded. Add GITHUB_TOKEN to .env to raise it.")
    if resp.status_code != 200:
        raise GitHubError(f"GitHub API returned {resp.status_code} for {url}")
    return resp


def _delete_existing(prefix: str, project_id: str) -> None:
    """Makes re-connecting the same repo idempotent (replaces its previous rows)."""
    like = prefix.replace("\\", "\\\\").replace("%", "").replace("_", "\\_") + "-%"
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM memories WHERE project_id = ? AND id LIKE ? ESCAPE '\\'",
            (project_id, like),
        )


async def ingest_github_repo(
    project_id: str,
    repo: str,
    max_commits: int = 25,
    max_issues: int = 15,
    use_llm: bool = True,
) -> Dict[str, Any]:
    start = time.perf_counter()
    slug = repo_slug(repo)
    owner, name = slug.split("/")
    # Project-scope the id prefix: memory ids are globally unique, so the same
    # repo must be able to live in two projects without colliding or stealing rows.
    # Deletion uses the repo-only base prefix so it also clears rows written by
    # the older, non-scoped id scheme.
    base_prefix = f"gh-{owner}-{name}".lower().replace("_", "-")
    safe_project = re.sub(r"[^a-zA-Z0-9_-]", "-", project_id.lower().strip())
    prefix = f"{base_prefix}-{safe_project}"[:100]

    # follow_redirects: renamed/moved repos answer with 301 and a Location header
    async with httpx.AsyncClient(follow_redirects=True) as client:
        meta = (await _get(client, f"{GITHUB_API}/repos/{slug}")).json()
        commits = (await _get(client, f"{GITHUB_API}/repos/{slug}/commits", {"per_page": max_commits})).json()
        # The issues endpoint mixes in PRs and sorts newest-first; on busy repos the
        # newest items are almost all PRs, so page until we have enough real issues.
        issues_raw: List[Dict[str, Any]] = []
        page = 1
        while page <= 2:
            batch = (
                await _get(
                    client,
                    f"{GITHUB_API}/repos/{slug}/issues",
                    {"state": "open", "per_page": 100, "page": page},
                )
            ).json()
            issues_raw.extend(batch)
            real_so_far = [i for i in issues_raw if "pull_request" not in i]
            if len(real_so_far) >= max_issues or len(batch) < 100:
                break
            page += 1
        readme_text = ""
        try:
            resp = await client.get(
                f"{GITHUB_API}/repos/{slug}/readme",
                headers={**_headers(), "Accept": "application/vnd.github.raw"},
                timeout=30.0,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                readme_text = resp.text[:2000]
        except Exception:
            pass  # README is optional

    # The issues endpoint also returns PRs; keep genuine issues only.
    issues = [i for i in issues_raw if "pull_request" not in i]

    _delete_existing(base_prefix, project_id)

    memories: List[Dict[str, Any]] = []

    # 1) Repository overview
    topics = ", ".join(meta.get("topics", [])[:8]) or "none"
    memories.append(
        {
            "id": f"{prefix}-overview",
            "project_id": project_id,
            "type": "fact",
            "title": f"Repository overview: {meta.get('full_name', slug)}",
            "content": (
                f"{meta.get('description') or 'No description.'} "
                f"Primary language: {meta.get('language') or 'unknown'}. "
                f"Topics: {topics}. Stars: {meta.get('stargazers_count', 0)}."
            ),
            "status": "active",
            "author": "github",
            "entities": [name] + ([meta["language"]] if meta.get("language") else []),
            "created_at": _iso(meta.get("updated_at")),
        }
    )

    # 2) Commits -> change memories with real git-author provenance
    for c in commits:
        sha = (c.get("sha") or "")[:10]
        commit = c.get("commit", {})
        message = (commit.get("message") or "").strip()
        first_line = message.splitlines()[0][:200] if message else "(no message)"
        git_author = (commit.get("author") or {}).get("name") or "unknown"
        memories.append(
            {
                "id": f"{prefix}-commit-{sha}",
                "project_id": project_id,
                "type": "change",
                "title": first_line,
                "content": f"Commit {sha} on {slug}: {first_line}",
                "status": "completed",
                "author": git_author,
                "entities": [name],
                "created_at": _iso((commit.get("author") or {}).get("date")),
            }
        )

    # 3) Open issues -> tasks, bug-labeled ones become blockers
    for issue in issues:
        labels = {l.get("name", "").lower() for l in issue.get("labels", [])}
        is_blocker = bool(labels & BUG_LABELS)
        body = (issue.get("body") or "").strip()[:300]
        memories.append(
            {
                "id": f"{prefix}-issue-{issue.get('number')}",
                "project_id": project_id,
                "type": "blocker" if is_blocker else "task",
                "title": issue.get("title", "(untitled issue)")[:200],
                "content": f"Issue #{issue.get('number')}: {body}" if body else f"Issue #{issue.get('number')}",
                "status": "blocked" if is_blocker else "active",
                "author": (issue.get("user") or {}).get("login") or "unknown",
                "entities": [name] + sorted(labels)[:4],
                "created_at": _iso(issue.get("created_at")),
            }
        )

    for m in memories:
        insert_memory(m)

    # 4) LLM insight pass: infer decisions/facts from README + commit history.
    #    Provenance is explicitly marked as AI-inferred so it stays honest.
    insights: List[Dict[str, Any]] = []
    insight_error: Optional[str] = None
    if use_llm:
        commit_lines = "\n".join(
            f"- {((c.get('commit') or {}).get('author') or {}).get('name', '?')}: "
            f"{((c.get('commit') or {}).get('message') or '').strip().splitlines()[0][:120]}"
            for c in commits[:20]
        )
        issue_lines = "\n".join(f"- {i.get('title', '')}" for i in issues[:10]) or "- none"
        digest = (
            f"README excerpt:\n{readme_text[:1500] or '(none)'}\n\n"
            f"Recent commits (author: message):\n{commit_lines or '- none'}\n\n"
            f"Open issues:\n{issue_lines}"
        )
        try:
            extracted = await extract_memories(digest)
            for i, m in enumerate(extracted.memories[:10]):
                memory_data = {
                    "id": f"{prefix}-insight-{i:02d}",
                    "project_id": project_id,
                    "type": m.type,
                    "title": m.title[:200],
                    "content": m.content,
                    "status": m.status,
                    "author": "inferred (AI)",
                    "entities": m.entities[:6],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                insert_memory(memory_data)
                insights.append(memory_data)
        except Exception as e:  # LLM is optional — deterministic events still stand
            insight_error = str(e)

    # 5) Re-index the project from SQLite (source of truth) into Moss so no
    #    stale docs linger (e.g. commits that dropped out of the recent window).
    moss_error: Optional[str] = None
    try:
        await rebuild_project_index(project_id)
    except RuntimeError as e:
        moss_error = str(e)

    return {
        "repo": slug,
        "commits": len(commits),
        "issues": len(issues),
        "blockers": sum(1 for m in memories if m["type"] == "blocker"),
        "insights": len(insights),
        "insight_error": insight_error,
        "moss_error": moss_error,
        "total_ms": round((time.perf_counter() - start) * 1000.0, 2),
    }
