"""
GitHub Client — the agent's authenticated interface to GitHub.

Uses the agent's own Personal Access Token (GITHUB_TOKEN env var).
All network calls go through httpx — no hardcoded repo lists anywhere.

The agent's GitHub identity is separate from any human developer account.
It opens PRs under its own username so the contribution history is real
and attributable.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN", "")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "teammate-agent")
GITHUB_API      = "https://api.github.com"

# The agent's own repo — read from env so there's no hardcoded path.
# Set to "owner/repo" format, e.g. "ja/agent"
AGENT_REPO = os.getenv("AGENT_REPO", "")


@dataclass
class RepoInfo:
    full_name: str          # "owner/repo"
    clone_url: str          # git@github.com:owner/repo.git
    description: str
    language: str
    stars: int
    open_issues: int
    topics: list[str]
    default_branch: str = "main"
    has_good_first_issues: bool = False


@dataclass
class IssueInfo:
    number: int
    title: str
    body: str
    labels: list[str]
    url: str


@dataclass
class PRInfo:
    number: int
    url: str
    state: str              # "open" | "closed" | "merged"
    merged: bool
    title: str
    repo_full_name: str


class GitHubClient:
    """
    Authenticated GitHub API wrapper for the agent's own account.

    Intentionally has no concept of a "permitted repo list" — the agent
    discovers what to work on at runtime based on its own capabilities
    and memory, not a static config.
    """

    def __init__(self):
        if not GITHUB_TOKEN:
            logger.warning("GITHUB_TOKEN not set — GitHub operations will be rate-limited or fail")
        self._headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ── Repo discovery ──────────────────────────────────────────────────────

    async def search_repositories(
        self,
        query: str,
        language: Optional[str] = None,
        min_stars: int = 10,
        max_stars: int = 5000,
        limit: int = 10,
    ) -> list[RepoInfo]:
        """
        Search GitHub for repos matching a naturally-generated query.
        The query is built by the caller (RepoSelector) from the agent's
        own Neo4j profile — not hardcoded here.
        """
        q = f"{query} stars:{min_stars}..{max_stars}"
        if language:
            q += f" language:{language}"

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API}/search/repositories",
                headers=self._headers,
                params={"q": q, "sort": "updated", "per_page": limit},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])

        results = []
        for item in items:
            topics = item.get("topics", [])
            results.append(RepoInfo(
                full_name=item["full_name"],
                clone_url=f"git@github.com:{item['full_name']}.git",
                description=item.get("description") or "",
                language=item.get("language") or "",
                stars=item.get("stargazers_count", 0),
                open_issues=item.get("open_issues_count", 0),
                topics=topics,
                default_branch=item.get("default_branch", "main"),
                has_good_first_issues="good-first-issue" in topics,
            ))
        return results

    async def get_issues(
        self,
        owner: str,
        repo: str,
        labels: list[str] | None = None,
        limit: int = 5,
    ) -> list[IssueInfo]:
        """Fetch open issues for a repo, optionally filtered by label."""
        params: dict[str, Any] = {"state": "open", "per_page": limit}
        if labels:
            params["labels"] = ",".join(labels)

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/issues",
                headers=self._headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json()

        return [
            IssueInfo(
                number=i["number"],
                title=i["title"],
                body=(i.get("body") or "")[:2000],
                labels=[l["name"] for l in i.get("labels", [])],
                url=i["html_url"],
            )
            for i in items
            if "pull_request" not in i   # exclude PRs from issue list
        ]

    # ── Fork + PR ───────────────────────────────────────────────────────────

    async def fork_repo(self, owner: str, repo: str) -> str:
        """Fork a repo to the agent's account. Returns the fork's clone URL."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GITHUB_API}/repos/{owner}/{repo}/forks",
                headers=self._headers,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        clone_url = f"git@github.com:{data['full_name']}.git"
        logger.info(f"Forked {owner}/{repo} → {data['full_name']}")
        return clone_url

    async def create_pr(
        self,
        owner: str,
        repo: str,
        head: str,          # "agent-username:branch-name"
        base: str,
        title: str,
        body: str,
    ) -> PRInfo:
        """Open a PR from the agent's fork/branch into the upstream repo."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
                headers=self._headers,
                json={"title": title, "head": head, "base": base, "body": body},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

        pr = PRInfo(
            number=data["number"],
            url=data["html_url"],
            state=data["state"],
            merged=data.get("merged", False),
            title=data["title"],
            repo_full_name=f"{owner}/{repo}",
        )
        logger.info(f"PR opened: {pr.url}")
        return pr

    async def get_pr_status(self, owner: str, repo: str, pr_number: int) -> PRInfo:
        """Check the current state of one of the agent's PRs."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=self._headers,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        return PRInfo(
            number=data["number"],
            url=data["html_url"],
            state=data["state"],
            merged=data.get("merged", False),
            title=data["title"],
            repo_full_name=f"{owner}/{repo}",
        )

    async def list_agent_prs(self, state: str = "open") -> list[PRInfo]:
        """List all PRs opened BY the agent across all repos."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API}/search/issues",
                headers=self._headers,
                params={
                    "q": f"type:pr author:{GITHUB_USERNAME} state:{state}",
                    "per_page": 50,
                },
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])

        prs = []
        for i in items:
            repo_url   = i.get("repository_url", "")
            repo_name  = "/".join(repo_url.split("/")[-2:]) if repo_url else ""
            prs.append(PRInfo(
                number=i["number"],
                url=i["html_url"],
                state=i["state"],
                merged=i.get("pull_request", {}).get("merged_at") is not None,
                title=i["title"],
                repo_full_name=repo_name,
            ))
        return prs

    # ── Own-repo helpers ────────────────────────────────────────────────────

    def own_repo(self) -> tuple[str, str] | None:
        """Return (owner, repo) for the agent's own repo, or None if not configured."""
        if not AGENT_REPO or "/" not in AGENT_REPO:
            return None
        owner, repo = AGENT_REPO.split("/", 1)
        return owner, repo
