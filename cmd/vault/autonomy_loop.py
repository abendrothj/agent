"""
Autonomy Loop — the agent's unsupervised contribution drive.

Runs as a background asyncio task inside the Vault container.
It is NOT triggered by human prompts — it wakes on its own schedule,
driven by its affect state (boredom, curiosity, fulfillment).

What makes this genuinely self-directed:
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

Task queue and sleep/wake model (endocrine analog):
  The loop runs a priority queue of AutonomyTask objects.  When the queue
  drains it enters a hormonally-gated sleep: the duration is computed from
  the current affect state (boredom + curiosity = wake pressure; fulfillment
  = rest signal).  External callers (API, Slack, Watchdog) can push tasks
  and wake the loop immediately via enqueue().
"""
import asyncio
import heapq
import logging
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from internal.affect import engine as affect_engine
from internal.affect.store import AffectStore

try:
    from internal.git.github_client import GitHubClient, PRInfo
    from internal.git.identity import GitIdentity
    from internal.git.repo_selector import ContributionTarget, RepoSelector
    from internal.memory.graph.client import GraphRAGClient
    _GIT_OK = True
except ImportError:
    _GIT_OK = False
    # Stub types so the class definition compiles without the optional deps.
    class GitHubClient:  # type: ignore[no-redef]
        ...
    class PRInfo:  # type: ignore[no-redef]
        ...
    class GitIdentity:  # type: ignore[no-redef]
        ...
    class ContributionTarget:  # type: ignore[no-redef]
        ...
    class RepoSelector:  # type: ignore[no-redef]
        def __init__(self, *a, **kw): ...
    class GraphRAGClient:  # type: ignore[no-redef]
        ...

logger = logging.getLogger(__name__)

# Minimum interval between contributions (prevents spam PRs).
MIN_CONTRIBUTION_GAP  = int(os.getenv("AUTONOMY_MIN_GAP_SECONDS", str(30 * 60)))

# How often to run affect decay (seconds — 30 min default).
DECAY_INTERVAL_SECONDS = int(os.getenv("AFFECT_DECAY_INTERVAL_SECONDS", str(30 * 60)))

# Fallback sleep duration when the affect store is unavailable.
_FALLBACK_SLEEP_SECONDS = int(os.getenv("AUTONOMY_INTERVAL_SECONDS", str(2 * 60 * 60)))


# ── Task queue types ──────────────────────────────────────────────────────────

class TaskKind(str, Enum):
    POLL_OUTCOMES  = "poll_outcomes"   # check open PRs against GitHub
    CONTRIBUTE     = "contribute"      # find a target and submit a PR
    EXTERNAL       = "external"        # task injected by API / Slack / Watchdog


@dataclass(order=True)
class AutonomyTask:
    """
    A unit of scheduled autonomy work.

    Priority: lower runs first.
      0  — urgent (human-injected, Watchdog alert)
      5  — normal (standard cycle tasks)
      10 — background (deferred housekeeping)
    """
    priority:   int      = field(default=5)
    kind:       TaskKind = field(compare=False, default=TaskKind.CONTRIBUTE)
    payload:    dict     = field(compare=False, default_factory=dict)
    created_at: datetime = field(compare=False, default_factory=datetime.utcnow)


class AutonomyLoop:
    """
    Background task that drives the agent's autonomous contribution cycle.

    Lifecycle:
      1. start()     — launches the queue loop and affect decay as asyncio tasks
      2. enqueue()   — push an external task and wake the loop immediately
      3. Every sleep/wake cycle:
           a. Drain the queue (poll PR outcomes, find target, contribute)
           b. Compute affect-driven sleep duration
           c. Sleep (interruptible by enqueue())
           d. Reseed the queue for the next cycle
      4. stop()      — graceful shutdown
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

        self._queue: list[AutonomyTask] = []              # min-heap
        self._wake_event: Optional[asyncio.Event] = None  # created in start()

        self._loop_task:  Optional[asyncio.Task] = None
        self._decay_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_contribution: Optional[datetime] = None
        self._last_decay:        Optional[datetime] = None
        self._had_novel_activity_since_decay = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        """Launch the autonomy loop and affect decay as background asyncio tasks."""
        if self._loop_task and not self._loop_task.done():
            return
        self._wake_event = asyncio.Event()
        self._running    = True
        self._reseed_queue()  # seed initial cycle tasks
        self._loop_task  = asyncio.create_task(self._loop(),        name="autonomy_loop")
        self._decay_task = asyncio.create_task(self._decay_loop(),  name="affect_decay")
        logger.info(
            "[autonomy] Loop started — task queue active, "
            f"decay every {DECAY_INTERVAL_SECONDS}s"
        )

    async def stop(self):
        """Graceful shutdown — cancels background tasks and waits."""
        self._running = False
        if self._wake_event:
            self._wake_event.set()   # unblock any sleeping wait
        for t in (self._loop_task, self._decay_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        logger.info("[autonomy] Loop stopped")

    def enqueue(
        self,
        kind: TaskKind = TaskKind.EXTERNAL,
        payload: Optional[dict] = None,
        priority: int = 0,
    ) -> None:
        """
        Push a task into the queue and wake the loop immediately if sleeping.

        Used by: API handlers, Slack command callbacks, Watchdog alerts.
        External tasks default to priority=0 (urgent) so they run before the
        next scheduled cycle.
        """
        heapq.heappush(self._queue, AutonomyTask(
            priority=priority,
            kind=kind,
            payload=payload or {},
        ))
        if self._wake_event:
            self._wake_event.set()
        logger.info(f"[autonomy] Task enqueued: kind={kind} priority={priority}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _loop(self):
        """
        Task queue executor with affect-driven sleep.

        Wakes when:
          - A task is in the queue (including freshly enqueued external tasks)
          - The affect-computed sleep interval expires

        Sleeps when:
          - The queue is empty; duration = sleep_duration(affect_state)
        """
        while self._running:
            if self._queue:
                task = heapq.heappop(self._queue)
                try:
                    await self._run_task(task)
                except Exception as exc:
                    logger.error(
                        f"[autonomy] Task {task.kind} failed: {exc}", exc_info=True
                    )
            else:
                # Queue empty — sleep until affect says it's time to act
                sleep_secs = await self._compute_sleep_duration()
                logger.info(
                    f"[autonomy] Queue empty — sleeping {sleep_secs / 60:.1f} min "
                    f"(affect-driven)"
                )
                await self._sleep_or_wake(sleep_secs)
                if self._running:
                    self._reseed_queue()

    async def _decay_loop(self):
        """Separate loop that runs affect decay on its own cadence."""
        await asyncio.sleep(DECAY_INTERVAL_SECONDS)
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
                    state = await self._affect.read_state()
                    if state:
                        next_wake = await self._compute_sleep_duration()
                        logger.info(
                            f"[affect] decay — "
                            f"curiosity={state.curiosity:.3f}  "
                            f"boredom={state.boredom:.3f}  "
                            f"fulfillment={state.fulfillment:.3f}  "
                            f"caution={state.caution:.3f}  "
                            f"→ next wake in {next_wake / 60:.1f} min"
                        )
            except Exception as exc:
                logger.warning(f"[affect] decay error (non-fatal): {exc}")
            await asyncio.sleep(DECAY_INTERVAL_SECONDS)

    # ── Task dispatcher ────────────────────────────────────────────────────────

    async def _run_task(self, task: AutonomyTask) -> None:
        """Dispatch a task to the appropriate handler."""
        logger.info(f"[autonomy] Running task: {task.kind}")
        if task.kind == TaskKind.POLL_OUTCOMES:
            await self._poll_pr_outcomes()
        elif task.kind == TaskKind.CONTRIBUTE:
            await self._cycle_contribute()
        elif task.kind == TaskKind.EXTERNAL:
            await self._handle_external(task.payload)
        else:
            logger.warning(f"[autonomy] Unknown task kind: {task.kind}")

    async def _handle_external(self, payload: dict) -> None:
        """
        Handle a task injected by an external caller (API, Slack, Watchdog).

        Payload fields:
          prompt      — the request text (required)
          request_id  — idempotency key (generated if absent)
          tier_hint   — suggested risk tier (default 2)
        """
        prompt = payload.get("prompt", "")
        if not prompt:
            logger.warning("[autonomy] External task had no prompt — skipping")
            return
        request_id = payload.get("request_id", str(uuid.uuid4()))
        try:
            result = await self._vault.process_autonomous_request(
                request_id=request_id,
                prompt=prompt,
                tier_hint=payload.get("tier_hint", 2),
            )
            logger.info(
                f"[autonomy] External task {request_id} "
                f"→ approved={result.get('approved')} reason={result.get('reason')}"
            )
        except Exception as exc:
            logger.warning(f"[autonomy] External task {request_id} failed: {exc}")

    # ── Sleep / wake ──────────────────────────────────────────────────────────

    async def _sleep_or_wake(self, seconds: float) -> None:
        """
        Sleep for `seconds`, but wake immediately if a task is enqueued.

        This is the hormonal gate: instead of a fixed cron interval the loop
        waits a duration shaped by the affect state.  Any call to enqueue()
        sets the wake event and interrupts the sleep.
        """
        if not self._wake_event:
            await asyncio.sleep(seconds)
            return
        self._wake_event.clear()
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=seconds)
            logger.info("[autonomy] Woke early — external stimulus")
        except asyncio.TimeoutError:
            pass  # natural wake after interval

    async def _compute_sleep_duration(self) -> float:
        """
        Read affect state and compute the next sleep duration via the engine.
        Falls back to _FALLBACK_SLEEP_SECONDS when the store is unavailable.
        """
        if not self._affect:
            return float(_FALLBACK_SLEEP_SECONDS)
        state = await self._affect.read_state()
        if state is None:
            return float(_FALLBACK_SLEEP_SECONDS)
        return affect_engine.sleep_duration(state)

    def _reseed_queue(self) -> None:
        """Schedule the standard next-cycle tasks after waking."""
        heapq.heappush(self._queue, AutonomyTask(priority=5, kind=TaskKind.POLL_OUTCOMES))
        heapq.heappush(self._queue, AutonomyTask(priority=5, kind=TaskKind.CONTRIBUTE))

    # ── Core cycle ────────────────────────────────────────────────────────────

    async def _cycle_contribute(self):
        """Find a target and contribute — the core autonomous work task."""
        logger.info("[autonomy] Starting contribution cycle")

        # Enforce minimum gap between contributions
        if self._last_contribution:
            elapsed = (datetime.utcnow() - self._last_contribution).total_seconds()
            if elapsed < MIN_CONTRIBUTION_GAP:
                logger.info(
                    f"[autonomy] Skipping contribution — last one was "
                    f"{elapsed:.0f}s ago (min gap {MIN_CONTRIBUTION_GAP}s)"
                )
                return

        # Decide what to work on
        target = await self._selector.find_next_target()
        if not target:
            logger.info("[autonomy] No suitable target found this cycle")
            if self._affect:
                await self._affect.apply_delta(affect_engine.cycle_no_target())
            return

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
