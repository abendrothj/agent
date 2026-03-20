"""
Autonomous Repo Selector — decides what to work on next.

This is the core of the agent's genuine autonomy. There are NO hardcoded
repos here. Every decision is driven by:

  1. A dynamic profile of the agent's own capabilities and history, read
     from Neo4j (languages it has worked in, domains where PRs merged,
     patterns that caused failures — things to avoid).

  2. A live GitHub search built from that profile at runtime.

  3. Self-evaluation: the agent's own codebase is always evaluated as a
     candidate — if Neo4j shows a recurring failure pattern (e.g. repeated
     rate-limit rejections that turned out to be safe) the agent proposes
     a self-modification PR.

  4. Affective state: the agent's current curiosity, boredom, and fulfillment
     modulate candidate scores.  Bored + curious → seek unexplored territory.
     High fulfillment → be slightly more selective.

The result is a `ContributionTarget` — a specific repo + issue + framed
prompt that gets injected back into the Vault's LangGraph pipeline for
risk classification and execution, just like any human prompt would.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from internal.affect import engine as affect_engine
from internal.affect.store import AffectStore
from internal.git.github_client import GitHubClient, IssueInfo, RepoInfo
from internal.memory.graph.client import GraphRAGClient

logger = logging.getLogger(__name__)

# How many GitHub candidates to evaluate per cycle (keeps API usage low)
CANDIDATE_POOL_SIZE = int(os.getenv("AUTONOMY_CANDIDATE_POOL", "8"))

# Minimum score a candidate needs to be selected.
# Lowered when agent is bored (boredom_override) to attempt harder targets.
_MIN_SCORE_NORMAL  = 0.30
_MIN_SCORE_BORED   = 0.15  # boredom forces the agent out of its comfort zone


@dataclass
class ContributionTarget:
    """A fully-described contribution opportunity the agent has chosen."""
    repo_full_name: str         # "owner/repo"  or  OWN_REPO marker
    issue_number: Optional[int]
    issue_title: str
    issue_body: str
    proposed_prompt: str        # what gets sent into the Vault pipeline
    language: str
    score: float                # 0-1; higher = more confident it can help
    domain: str = ""            # topic area (for affect tracking)
    is_self_modification: bool = False
    clone_url: str = ""

    # Populated after submission
    pr_url: Optional[str] = None
    branch_name: Optional[str] = None


class RepoSelector:
    """
    Decides autonomously where the agent should direct its next contribution.

    Nothing about this class requires external configuration of which repos
    are "allowed". The agent reasons from its own history, live GitHub data,
    and its current affective state.
    """

    def __init__(
        self,
        neo4j_client: GraphRAGClient,
        github_client: GitHubClient,
        affect_store: Optional[AffectStore] = None,
    ):
        self._graph  = neo4j_client
        self._github = github_client
        self._affect = affect_store

    # ── Public API ──────────────────────────────────────────────────────────

    async def find_next_target(self) -> Optional[ContributionTarget]:
        """
        Core decision: what should the agent work on next?

        Returns None if nothing suitable is found this cycle (agent will
        sleep and try again later rather than forcing a bad contribution).
        """
        profile      = await self._build_agent_profile()
        affect_state = await self._affect.read_state() if self._affect else None
        logger.info(
            f"[selector] agent profile: {profile}  "
            f"affect: {affect_state.as_dict() if affect_state else 'unavailable'}"
        )

        candidates: list[ContributionTarget] = []

        # 1. Evaluate own repo first (self-modification always a candidate)
        self_target = await self._evaluate_self_repo(profile)
        if self_target:
            candidates.append(self_target)

        # 2. Search GitHub dynamically from learned profile
        external = await self._search_external(profile, affect_state)
        candidates.extend(external)

        if not candidates:
            logger.info("[selector] No suitable candidates this cycle — will retry later")
            return None

        # Filter by minimum score (relaxed when bored)
        boredom_override = affect_state.boredom > 0.70 if affect_state else False
        min_score = _MIN_SCORE_BORED if boredom_override else _MIN_SCORE_NORMAL
        viable = [c for c in candidates if c.score >= min_score]

        if not viable:
            if boredom_override and candidates:
                # Boredom override: take our best shot even below normal threshold
                best = max(candidates, key=lambda c: c.score)
                logger.info(
                    f"[selector] Boredom override: accepting below-threshold candidate "
                    f"{best.repo_full_name} (score={best.score:.2f})"
                )
                return best
            return None

        best = max(viable, key=lambda c: c.score)
        logger.info(
            f"[selector] Chose: {best.repo_full_name} "
            f"({'self-mod' if best.is_self_modification else 'external'}, "
            f"score={best.score:.2f}, domain={best.domain})"
        )
        return best

    # ── Profile building (from Neo4j) ────────────────────────────────────────

    async def _build_agent_profile(self) -> dict:
        """
        Ask Neo4j what the agent knows about itself.

        Returns a profile dict:
          languages:    ["python", "go"]        — languages in merged PRs
          domains:      ["cli", "testing"]      — topic areas with positive outcomes
          avoid:        ["database migration"]  — patterns that caused failures
          self_issues:  [str]                   — recurring agent failure patterns
        """
        profile: dict = {
            "languages": [],
            "domains":   [],
            "avoid":     [],
            "self_issues": [],
        }

        if not self._graph._neo4j_available:
            # No memory yet — start with Python (agent's own language)
            profile["languages"] = ["python"]
            return profile

        # Merged PR languages
        lang_rows = await self._graph.neo4j_query(
            """
            MATCH (p:PR {outcome: 'merged'})
            WHERE p.language IS NOT NULL
            RETURN p.language AS lang, count(p) AS n
            ORDER BY n DESC LIMIT 5
            """
        )
        profile["languages"] = [r["lang"] for r in lang_rows if r.get("lang")]

        # Domains/topics with positive outcomes
        domain_rows = await self._graph.neo4j_query(
            """
            MATCH (p:PR {outcome: 'merged'})-[:RELATES_TO]->(t:Topic)
            RETURN t.name AS domain, count(p) AS n
            ORDER BY n DESC LIMIT 5
            """
        )
        profile["domains"] = [r["domain"] for r in domain_rows if r.get("domain")]

        # Recurring failure patterns (to avoid repeating)
        fail_rows = await self._graph.neo4j_query(
            """
            MATCH (d:Document)-[:CAUSED|TRIGGERED]->(f:Entity {type: 'failure'})
            RETURN f.description AS reason, count(d) AS n
            ORDER BY n DESC LIMIT 3
            """
        )
        profile["avoid"] = [r["reason"] for r in fail_rows if r.get("reason")]

        # Self-issues: vault decisions that were overridden or reversed
        self_rows = await self._graph.neo4j_query(
            """
            MATCH (d:Document)-[:APPROVED]->(e:Entity)
            WHERE d.source_type = 'vault_rejection'
              AND e.type = 'override'
            RETURN d.text AS text LIMIT 3
            """
        )
        profile["self_issues"] = [r["text"] for r in self_rows if r.get("text")]

        if not profile["languages"]:
            profile["languages"] = ["python"]

        return profile

    # ── Self-modification evaluation ────────────────────────────────────────

    async def _evaluate_self_repo(self, profile: dict) -> Optional[ContributionTarget]:
        """
        Evaluate whether the agent's own codebase needs a modification.

        This is genuine self-modification: the agent reads its own failure
        history from Neo4j and decides if it should change its own behaviour.
        Not triggered by a human prompt — triggered by its own observed patterns.
        """
        own = self._github.own_repo()
        if not own:
            return None

        owner, repo = own
        issues = profile.get("self_issues", [])

        if not issues:
            # No patterns pressing enough to warrant self-modification this cycle
            return None

        # Synthesise a self-modification prompt from the top recurring issue
        issue_desc = issues[0]
        prompt = (
            f"Self-modification: I have observed the recurring pattern '{issue_desc}' "
            f"in my own decision logs. Propose and implement a targeted code change to "
            f"address this, submit as a PR to {owner}/{repo} for human review."
        )

        return ContributionTarget(
            repo_full_name=f"{owner}/{repo}",
            issue_number=None,
            issue_title=f"[self-mod] {issue_desc[:80]}",
            issue_body=issue_desc,
            proposed_prompt=prompt,
            language="python",
            domain="self-improvement",
            score=0.85,    # Self-modification always high-priority when triggered
            is_self_modification=True,
            clone_url=f"git@github.com:{owner}/{repo}.git",
        )

    # ── External repo search ─────────────────────────────────────────────────

    async def _search_external(
        self,
        profile: dict,
        affect_state=None,
    ) -> list[ContributionTarget]:
        """
        Dynamically build GitHub search queries from agent profile and
        evaluate each candidate repo/issue pair.

        Crucially: the search query is NOT hardcoded. It is generated from
        the agent's live Neo4j profile, so it evolves as the agent grows.

        When the agent is highly bored (boredom > 0.6), it broadens the search
        to include languages/domains it has NOT worked in before — the drive
        to push past knowledge limits.
        """
        languages  = profile.get("languages", ["python"])
        domains    = profile.get("domains",   [])
        avoid_kws  = profile.get("avoid",     [])

        # When bored: add completely unknown languages to the search pool
        bored = affect_state and affect_state.boredom > 0.60
        if bored:
            all_langs  = ["python", "go", "rust", "typescript", "ruby", "kotlin"]
            novel_langs = [l for l in all_langs if l not in languages]
            languages  = languages[:1] + novel_langs[:2]   # focus on the unknown
            logger.info(
                f"[selector] Boredom={affect_state.boredom:.2f} → broadening to {languages}"
            )

        results: list[ContributionTarget] = []

        for language in languages[:2]:      # top 2 languages
            topic_clause = " ".join(f"topic:{d}" for d in domains[:3]) if domains else ""
            query = f"is:public {topic_clause} good-first-issues:>0"

            try:
                repos = await self._github.search_repositories(
                    query=query,
                    language=language,
                    min_stars=20,
                    max_stars=10000,
                    limit=CANDIDATE_POOL_SIZE // 2,
                )
            except Exception as exc:
                logger.warning(f"[selector] GitHub search failed: {exc}")
                continue

            for repo in repos:
                target = await self._evaluate_repo(repo, avoid_kws, affect_state)
                if target:
                    results.append(target)

        return results

    async def _evaluate_repo(
        self, repo: RepoInfo, avoid_kws: list[str], affect_state=None
    ) -> Optional[ContributionTarget]:
        """
        For a candidate repo: find a concrete issue the agent can work on,
        score it, and apply affect-driven adjustments.
        """
        owner, name = repo.full_name.split("/", 1)

        try:
            issues = await self._github.get_issues(
                owner, name,
                labels=["good first issue", "help wanted"],
                limit=3,
            )
        except Exception:
            return None

        if not issues:
            return None

        issue = max(issues, key=lambda i: len(i.body))

        # Derive a domain label from repo topics or language
        domain = repo.topics[0] if repo.topics else repo.language.lower() if repo.language else "general"

        # Avoidance
        overlap = any(kw.lower() in issue.title.lower() for kw in avoid_kws)
        raw_score = self._raw_score(repo, issue, overlap)

        # Apply affect adjustments
        if affect_state and self._affect:
            visits, merges = await self._affect.get_domain_familiarity(
                domain, repo.language.lower() if repo.language else ""
            )
            pref_w = await self._affect.get_preference_weight(
                domain, repo.language.lower() if repo.language else ""
            )
            influence = affect_engine.score_boost(
                state=affect_state,
                domain=domain,
                language=repo.language.lower() if repo.language else "",
                preference_weight=pref_w,
                visit_count=visits,
                merged_count=merges,
            )
            final_score = raw_score + influence.novelty_bonus + influence.preference_bonus
            logger.debug(
                f"[selector] {repo.full_name}: raw={raw_score:.2f} "
                f"affect=[{influence.description}] final={final_score:.2f}"
            )
        else:
            final_score = raw_score
            domain = domain

        if final_score < 0.01:
            return None

        return ContributionTarget(
            repo_full_name=repo.full_name,
            issue_number=issue.number,
            issue_title=issue.title,
            issue_body=issue.body,
            proposed_prompt=self._frame_prompt(repo, issue),
            language=repo.language,
            domain=domain,
            score=min(final_score, 1.0),
            clone_url=repo.clone_url,
        )

    # ── Scoring ──────────────────────────────────────────────────────────────

    def _raw_score(self, repo: RepoInfo, issue: IssueInfo, avoidance_overlap: bool) -> float:
        """Base score before affect adjustments (0-1)."""
        score = 0.4
        if len(issue.body) > 100:  score += 0.15
        if len(issue.body) > 400:  score += 0.10
        if repo.has_good_first_issues: score += 0.10
        if "good first issue" in issue.labels or "help wanted" in issue.labels: score += 0.15
        if avoidance_overlap:      score -= 0.30
        if repo.stars > 500:       score += 0.05
        return min(max(score, 0.0), 1.0)

    def _frame_prompt(self, repo: RepoInfo, issue: IssueInfo) -> str:
        return (
            f"Contribute to {repo.full_name} (GitHub issue #{issue.number}): "
            f"{issue.title}\n\n"
            f"Issue description:\n{issue.body[:1000]}\n\n"
            f"Propose and implement a fix, commit to a new branch, and open a PR "
            f"against the upstream repo's default branch."
        )
