"""
Autonomy Loop — the agent's unsupervised contribution drive.

Runs as a background asyncio task inside the Vault container.
It is NOT triggered by human prompts. It wakes on its own schedule,
decides what to work on (via RepoSelector), executes the work through
git operations, submits PRs, and learns from outcomes.

This is what makes the system genuinely self-directed:
  - The agent chooses its own targets
  - The agent modifies its own codebase when it detects improvement opportunities
  - Every PR outcome (merged/closed) feeds back into Neo4j, improving future decisions
  - The Vault's own LangGraph risk pipeline governs what actually executes

Architecture note:
  The loop does NOT contain its own code-generation logic. It builds a
  prompt from the ContributionTarget and sends it through the Vault's
  existing LangGraph pipeline — so the same risk classification, rate
  limiting, and human-interrupt nodes that govern human prompts also
  govern the agent's autonomous work. The agent cannot bypass its own
  governance system.
"""
import asyncio
import logging
import os
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from internal.affect import engine as affect_engine
from internal.affect.store import AffectStore
from internal.git.github_client import GitHubClient, PRInfo
from internal.git.identity import GitIdentity
from internal.git.repo_selector import ContributionTarget, RepoSelector
from internal.memory.graph.client import GraphRAGClient

logger = logging.getLogger(__name__)

# How often the autonomy loop wakes up (seconds).  2h default.
LOOP_INTERVAL_SECONDS = int(os.getenv("AUTONOMY_INTERVAL_SECONDS", str(2 * 60 * 60)))

# Minimum interval between contributions (prevents spam PRs)
MIN_CONTRIBUTION_GAP  = int(os.getenv("AUTONOMY_MIN_GAP_SECONDS", str(30 * 60)))

# How often to run affect decay (seconds).  30min default.
DECAY_INTERVAL_SECONDS = int(os.getenv("AFFECT_DECAY_INTERVAL_SECONDS", str(30 * 60)))


class AutonomyLoop:
    """
    Background task that drives the agent's autonomous contribution cycle.

    Lifecycle:
      1. start() — launches the loop as a detached asyncio task
      2. Every LOOP_INTERVAL_SECONDS:
         a. poll_pr_outcomes() — check outstanding PRs, record results to Neo4j
         b. find() target via RepoSelector
         c. execute() — clone, branch, send prompt through Vault pipeline, push
         d. submit_pr() — open PR on GitHub, record to Neo4j
      3. stop() — graceful shutdown (waits for current cycle)
    """

    def __init__(
        self,
        graph_client: GraphRAGClient,
        github_client: GitHubClient,
        identity: GitIdentity,
        vault_service,
        affect_store: Optional[AffectStore] = None,
    ):
        self._graph    = graph_client
        self._github   = github_client
        self._identity = identity
        self._vault    = vault_service
        self._affect   = affect_store
        self._selector = RepoSelector(graph_client, github_client, affect_store)

        self._task: Optional[asyncio.Task] = None
        self._decay_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_contribution: Optional[datetime] = None
        self._last_decay: Optional[datetime] = None
        self._had_novel_activity_since_decay = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        """Launch the autonomy loop and decay loop as background asyncio tasks."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="autonomy_loop")
        self._decay_task = asyncio.create_task(self._decay_loop(), name="affect_decay")
        logger.info(
            f"[autonomy] Loop started — interval={LOOP_INTERVAL_SECONDS}s, "
            f"decay every {DECAY_INTERVAL_SECONDS}s"
        )

    async def stop(self):
        self._running = False
        for t in (self._task, self._decay_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        logger.info("[autonomy] Loop stopped")

    # ── Main loop ────────────────────────────────────────────────────────────

    async def _loop(self):
        while self._running:
            try:
                await self._cycle()
            except Exception as exc:
                logger.error(f"[autonomy] Unhandled error in cycle: {exc}", exc_info=True)
            await asyncio.sleep(LOOP_INTERVAL_SECONDS)

    async def _decay_loop(self):
        """Separate loop that runs affect decay on its own cadence."""
        await asyncio.sleep(DECAY_INTERVAL_SECONDS)   # first decay after one interval
        while self._running:
            try:
                if self._affect:
                    now     = datetime.utcnow()
                    elapsed = int(
                        (now - self._last_decay).total_seconds()
                        if self._last_decay else DECAY_INTERVAL_SECONDS
                    )
                    await self._affect.apply_decay(
                        elapsed_seconds=elapsed,
                        had_novel_activity=self._had_novel_activity_since_decay,
                    )
                    self._last_decay = now
                    self._had_novel_activity_since_decay = False
                    # Log current state
                    state = await self._affect.read_state()
                    if state:
                        logger.info(
                            f"[affect] state after decay — "
                            f"curiosity={state.curiosity:.3f}  "
                            f"boredom={state.boredom:.3f}  "
                            f"fulfillment={state.fulfillment:.3f}"
                        )
            except Exception as exc:
                logger.warning(f"[affect] decay error (non-fatal): {exc}")
            await asyncio.sleep(DECAY_INTERVAL_SECONDS)

    async def _cycle(self):
        logger.info("[autonomy] Starting contribution cycle")

        # Step 1 — learn from outstanding PR outcomes
        await self._poll_pr_outcomes()

        # Step 2 — enforce minimum gap between contributions
        if self._last_contribution:
            elapsed = (datetime.utcnow() - self._last_contribution).total_seconds()
            if elapsed < MIN_CONTRIBUTION_GAP:
                logger.info(
                    f"[autonomy] Skipping contribution — last one was {elapsed:.0f}s ago "
                    f"(min gap {MIN_CONTRIBUTION_GAP}s)"
                )
                return

        # Step 3 — decide what to work on
        target = await self._selector.find_next_target()
        if not target:
            logger.info("[autonomy] No suitable target found this cycle")
            if self._affect:
                await self._affect.apply_delta(affect_engine.cycle_no_target())
            return

        # Step 4 — execute and submit PR
        await self._contribute(target)

    # ── PR outcome polling (learning signal) ────────────────────────────────

    async def _poll_pr_outcomes(self):
        """
        Check all open PRs in Neo4j against GitHub's current state.
        Record any transitions (merged/closed) so Neo4j reflects reality,
        and fire the corresponding affect signals.
        """
        open_prs = await self._graph.get_open_prs()
        if not open_prs:
            return

        logger.info(f"[autonomy] Polling {len(open_prs)} outstanding PR(s)")

        for record in open_prs:
            pr_id  = record.get("pr_id", "")
            repo   = record.get("repo", "")
            number = record.get("pr_number")
            self_mod = record.get("self_mod", False)

            if not repo or not number:
                continue

            owner, repo_name = repo.split("/", 1)
            try:
                pr_info = await self._github.get_pr_status(owner, repo_name, number)
            except Exception as exc:
                logger.warning(f"[autonomy] Could not check PR {pr_id}: {exc}")
                continue

            if pr_info.state == "closed":
                outcome  = "merged" if pr_info.merged else "closed"
                feedback = f"PR #{number} {outcome} in {repo}"
                await self._graph.record_pr_outcome(pr_id, outcome, feedback)

                # Derive domain/language from pr_id context (best effort)
                domain   = repo.split("/")[1] if "/" in repo else ""
                language = ""

                # Fire affect signal
                if self._affect:
                    await self._affect.record_pr_signal(
                        pr_id=pr_id, event_type=outcome,
                        repo_full_name=repo, domain=domain,
                        language=language, is_self_mod=bool(self_mod),
                    )
                    if outcome == "merged":
                        delta = affect_engine.pr_merged(
                            pr_id, domain, language, is_self_mod=bool(self_mod)
                        )
                        await self._affect.apply_delta(delta)
                        await self._affect.update_preference(
                            domain, language, "pr_merged", positive=True
                        )
                        self._had_novel_activity_since_decay = True
                    else:
                        delta = affect_engine.pr_rejected(pr_id, domain, language)
                        await self._affect.apply_delta(delta)
                        await self._affect.update_preference(
                            domain, language, "pr_rejected", positive=False
                        )

                logger.info(f"[autonomy] PR {pr_id} → {outcome}")

    # ── Contribution execution ───────────────────────────────────────────────

    async def _contribute(self, target: ContributionTarget):
        """
        Send the contribution target's prompt through the Vault pipeline.
        If approved, perform the git operations and open a PR.
        """
        logger.info(
            f"[autonomy] Contributing to {target.repo_full_name} — "
            f"{'self-mod' if target.is_self_modification else f'issue #{target.issue_number}'}"
        )

        # Send through Vault's risk pipeline — same path as human prompts
        request_id = str(uuid.uuid4())
        try:
            result = await self._vault.process_autonomous_request(
                request_id=request_id,
                prompt=target.proposed_prompt,
                tier_hint=3 if target.is_self_modification else 2,
            )
        except Exception as exc:
            logger.warning(f"[autonomy] Vault rejected or errored: {exc}")
            return

        if not result.get("approved"):
            logger.info(f"[autonomy] Vault did not approve — {result.get('reason')}")
            return

        # Git operations: clone, branch, apply the generated code, push
        code_patch = result.get("code_patch", "")
        if not code_patch:
            logger.info("[autonomy] Vault approved but returned no code patch — skipping git ops")
            return

        pr_info = await self._git_submit(target, code_patch)
        if not pr_info:
            return

        # Record in Neo4j
        pr_id = f"{target.repo_full_name}#{pr_info.number}"
        await self._graph.record_pr_submitted(
            pr_id=pr_id,
            repo_full_name=target.repo_full_name,
            pr_number=pr_info.number,
            pr_url=pr_info.url,
            title=pr_info.title,
            language=target.language,
            branch=target.branch_name or "",
            is_self_modification=target.is_self_modification,
            issue_title=target.issue_title,
        )

        # Record affect signals: was this domain novel or familiar?
        if self._affect:
            domain   = target.domain
            language = target.language.lower() if target.language else ""

            visits, _ = await self._affect.get_domain_familiarity(domain, language)
            if visits == 0:
                await self._affect.apply_delta(
                    affect_engine.novel_domain_explored(domain, language)
                )
                self._had_novel_activity_since_decay = True
            else:
                await self._affect.apply_delta(
                    affect_engine.familiar_domain_again(domain, language, visits)
                )

            await self._affect.apply_delta(
                affect_engine.cycle_contributed(domain, language)
            )
            await self._affect.record_pr_signal(
                pr_id=pr_id, event_type="submitted",
                repo_full_name=target.repo_full_name,
                domain=domain, language=language,
                is_self_mod=target.is_self_modification,
                issue_title=target.issue_title,
            )
            # Mark domain visited (for future familiarity lookups)
            await self._affect.mark_domain_visited(domain, language, outcome="open")

        self._last_contribution = datetime.utcnow()
        logger.info(f"[autonomy] PR submitted: {pr_info.url}")

    async def _git_submit(
        self, target: ContributionTarget, code_patch: str
    ) -> Optional[PRInfo]:
        """
        Clone the repo, apply the patch to a new branch, push, open PR.
        All operations happen in a temp directory that is cleaned up afterwards.
        """
        if not target.clone_url:
            return None

        owner, repo_name = target.repo_full_name.split("/", 1)
        branch = f"agent/{uuid.uuid4().hex[:8]}"
        target.branch_name = branch

        with tempfile.TemporaryDirectory() as workdir:
            try:
                # For external repos: fork first so we own the branch
                if not target.is_self_modification:
                    fork_url = await self._github.fork_repo(owner, repo_name)
                    clone_url = fork_url
                    # Give GitHub a moment to create the fork
                    await asyncio.sleep(5)
                else:
                    clone_url = target.clone_url

                _git(["clone", "--depth", "1", clone_url, workdir])
                self._identity.configure_repo(workdir)
                _git(["-C", workdir, "checkout", "-b", branch])

                # Apply the patch produced by the Vault pipeline
                patch_file = Path(workdir) / "_agent.patch"
                patch_file.write_text(code_patch)
                try:
                    _git(["-C", workdir, "apply", str(patch_file)])
                except subprocess.CalledProcessError:
                    # Patch didn't apply cleanly — skip rather than force a broken PR
                    logger.warning("[autonomy] Patch did not apply cleanly — skipping")
                    return None
                finally:
                    patch_file.unlink(missing_ok=True)

                _git(["-C", workdir, "add", "-A"])
                commit_msg = f"agent: {target.issue_title[:72]}"
                _git(["-C", workdir, "commit", "-m", commit_msg])
                _git(["-C", workdir, "push", "origin", branch])

            except subprocess.CalledProcessError as exc:
                logger.error(f"[autonomy] Git operation failed: {exc.stderr}")
                return None

        # Open PR on upstream
        from internal.git.github_client import GITHUB_USERNAME
        head = f"{GITHUB_USERNAME}:{branch}"
        title = f"[agent] {target.issue_title[:80]}"
        body = _pr_body(target)

        try:
            pr = await self._github.create_pr(
                owner=owner,
                repo=repo_name,
                head=head,
                base="main",
                title=title,
                body=body,
            )
            return pr
        except Exception as exc:
            logger.error(f"[autonomy] Failed to open PR: {exc}")
            return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git"] + args, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _pr_body(target: ContributionTarget) -> str:
    lines = [
        "<!-- opened by teammate-agent autonomy loop -->",
        "",
        f"**Issue:** {target.issue_title}",
        "",
    ]
    if target.issue_body:
        lines += [
            "**Context from issue:**",
            "> " + target.issue_body[:600].replace("\n", "\n> "),
            "",
        ]
    if target.is_self_modification:
        lines += [
            "_This PR is a self-modification proposed by the agent based on recurring_",
            "_patterns observed in its own decision log. Please review carefully._",
        ]
    else:
        lines.append(
            "_This PR was opened autonomously by [teammate-agent](https://github.com/"
            + (target.repo_full_name.split("/")[0]) + "). "
            "Please review and merge or close as appropriate._"
        )
    return "\n".join(lines)
