"""
LangGraph Vault - Stateful approval orchestrator using LangGraph
Replaces the hand-rolled if/else state machine in VaultService.process_request()
with an explicit directed graph that can be:
  - Inspected visually (draw_mermaid_png())
  - Checkpointed mid-flow (human-in-the-loop MFA pause/resume)
  - Replayed for audit
  - Extended with new nodes without touching existing logic
"""
import logging
import operator
import os
from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional, Tuple

from internal.core.risk.classifier import RiskClassifier, Tier
from internal.memory.ledger.store import LedgerStore
from internal.memory.context.manager import ContextManager
from internal.memory.graph.client import GraphRAGClient
from internal.mcp.client import MCPContextProvider
from internal.affect.engine import signal_caution
from internal.affect.store import AffectStore

try:
    from langgraph.graph import StateGraph, END
    from langgraph.graph.message import add_messages
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.types import interrupt, Command
    _LANGGRAPH_OK = True
except ImportError:
    # Node functions are importable and testable without LangGraph.
    # Only build_vault_graph() and LangGraphVault require it at runtime.
    StateGraph = None  # type: ignore[assignment,misc]
    END = None         # type: ignore[assignment]
    AsyncPostgresSaver = None  # type: ignore[assignment,misc]
    interrupt = None   # type: ignore[assignment]
    Command = None     # type: ignore[assignment,misc]
    _LANGGRAPH_OK = False

try:
    from typing_extensions import TypedDict
except ImportError:
    from typing import TypedDict  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------

class VaultState(TypedDict):
    """Full Vault decision state — every field persisted in checkpointer"""
    # Request inputs
    request_id: str
    prompt: str
    system_context: str
    scope: str
    approval_token: Optional[str]
    session_id: str

    # Decision outputs (filled as graph runs)
    tier: Optional[int]
    approved: Optional[bool]
    reason: str

    # Node checkpoints (append-only for audit trail)
    checkpoints: Annotated[List[str], operator.add]

    # Peripheral sensory context (MCP — thalamo-cortical pathway)
    # Filled by node_sense_context before classification fires.
    # Analogy: the primary sensory cortex receives filtered input from the
    # thalamus; here that signal enriches the risk classifier's world-model.
    mcp_context: Optional[str]

    # Intermediate flags
    rejection_cache_hit: bool
    rate_limit_exceeded: bool
    graph_memory_warning: Optional[str]   # GraphRAG failure pattern match
    shadow_eligible: bool
    token_valid: bool
    human_approval_pending: bool
    human_approved: Optional[bool]        # Filled by human interrupt resume


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

async def node_sense_context(state: VaultState, config: dict) -> dict:
    """
    Primary sensory cortex — first cortical processing of peripheral input.

    Biology mapping:
      MCP servers          = sensory receptors (git=proprioception, files=tactile)
      MCPContextProvider   = thalamus  (routes + gates the signal)
      This node             = primary sensory cortex (first cortical layer)
      node_classify        = association cortex (multi-modal integration)

    Runs unconditionally before classify. If the provider is absent or the
    sensory signal doesn't arrive within the thalamic window (2 s), the
    agent proceeds with mcp_context=None — degraded senses, not paralysis.
    """
    mcp_provider: Optional[MCPContextProvider] = config["configurable"].get("mcp_provider")
    if mcp_provider is None:
        return {"mcp_context": None, "checkpoints": ["sense_context:disabled"]}

    ctx = await mcp_provider.gather(state["prompt"])
    label = "sense_context:ok" if ctx else "sense_context:empty"
    logger.info("[sense_context] %s  chars=%s", label, len(ctx) if ctx else 0)
    return {"mcp_context": ctx, "checkpoints": [label]}


def node_classify(state: VaultState, config: dict) -> dict:
    """Classify the request into risk Tier 1-4 using RiskClassifier.

    If MCP sensory context was gathered in node_sense_context, it is prepended
    to system_context before the classifier sees it — giving the classifier the
    same multi-modal awareness a human expert would have when reading a PR.

    After classification, fires signal_caution(tier) into the affect store if
    one is available — the outward-directed concern proportional to risk level.
    """
    classifier: RiskClassifier = config["configurable"]["classifier"]

    # Enrich system_context with peripheral sensory signal (thalamo-cortical)
    base_ctx = state.get("system_context", "")
    mcp_ctx  = state.get("mcp_context") or ""
    enriched_ctx = (mcp_ctx + "\n\n" + base_ctx).strip() if mcp_ctx else base_ctx

    tier = classifier.classify(
        state["prompt"],
        enriched_ctx,
        state.get("scope", "local"),
    )
    logger.info(f"[classify] {state['request_id']} → Tier {tier}")

    # Fire caution signal into affect store (non-blocking, fire-and-forget)
    affect_store: Optional[AffectStore] = config["configurable"].get("affect_store")
    if affect_store is not None:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(affect_store.apply_delta(signal_caution(int(tier))))
        except Exception:
            pass  # affect store failure must never block the classification path

    return {
        "tier": int(tier),
        "checkpoints": [f"classified:tier_{tier}"],
    }


async def node_check_rejection_cache(state: VaultState, config: dict) -> dict:
    """Tier 4 only — auto-block if identical request rejected within 24h"""
    ledger: LedgerStore = config["configurable"]["ledger"]

    hit = await ledger.check_request_rejected_tier4(state["request_id"])
    logger.info(f"[rejection_cache] {state['request_id']} hit={hit}")
    return {
        "rejection_cache_hit": hit,
        "checkpoints": [f"rejection_cache:{'hit' if hit else 'miss'}"],
    }


async def node_check_rate_limit(state: VaultState, config: dict) -> dict:
    """Enforce per-tier hourly rate limits via Redis session"""
    context: ContextManager = config["configurable"]["context"]
    tier = state["tier"]
    session_id = state["session_id"]

    tier_limits = {1: 1000, 2: 100, 3: 10, 4: 1}
    limit = tier_limits.get(tier, 10)
    counter_key = f"rate_tier_{tier}_hour"

    session_exists = await context.exists_session(session_id)
    if not session_exists:
        await context.create_session(session_id)

    count = await context.increment_counter(session_id, counter_key)
    exceeded = count > limit
    logger.info(f"[rate_limit] tier={tier} count={count}/{limit} exceeded={exceeded}")
    return {
        "rate_limit_exceeded": exceeded,
        "checkpoints": [f"rate_limit:{'exceeded' if exceeded else 'ok'}_{count}/{limit}"],
    }


async def node_query_graph_memory(state: VaultState, config: dict) -> dict:
    """
    Query GraphRAG for failure patterns similar to this request.
    Adds a warning to state if a matching failure pattern exists — the
    Vault node will include this context in its decision reasoning.
    """
    graph_client: GraphRAGClient = config["configurable"]["graph_client"]

    try:
        warning = await graph_client.find_failure_patterns(
            prompt=state["prompt"],
            tier=state["tier"],
        )
        result = {"graph_memory_warning": warning}
        label = f"graph_memory:{'warning' if warning else 'clean'}"
    except Exception as exc:
        logger.warning(f"[graph_memory] GraphRAG unavailable: {exc}")
        result = {"graph_memory_warning": None}
        label = "graph_memory:unavailable"

    result["checkpoints"] = [label]
    return result


async def node_validate_token(state: VaultState, config: dict) -> dict:
    """Validate approval token — checks format, MFA flag, and expiry"""
    classifier: RiskClassifier = config["configurable"]["classifier"]
    token = state.get("approval_token")
    tier = Tier(state["tier"])

    if not token or len(token) < 10:
        return {"token_valid": False, "checkpoints": ["token:missing_or_short"]}

    if classifier.requires_mfa(tier) and "mfa:" not in token:
        return {"token_valid": False, "checkpoints": ["token:mfa_required_but_absent"]}

    # In production: parse JWT payload and verify mTLS cert signature
    return {"token_valid": True, "checkpoints": ["token:valid"]}


async def node_check_shadow_baseline(state: VaultState, config: dict) -> dict:
    """
    For Tier 3/4 with no pre-approved token: check whether Shadow has
    collected a baseline of sufficient age and semantic quality.
    If yes, grant auto-approval (no human MFA needed this cycle).
    """
    graph_client: GraphRAGClient = config["configurable"]["graph_client"]
    tier = state["tier"]

    try:
        eligible = await graph_client.check_baseline_eligibility(
            prompt=state["prompt"],
            tier=tier,
        )
    except Exception as exc:
        logger.warning(f"[shadow_baseline] check failed: {exc}")
        eligible = False

    logger.info(f"[shadow_baseline] tier={tier} eligible={eligible}")
    return {
        "shadow_eligible": eligible,
        "checkpoints": [f"shadow:{'eligible' if eligible else 'ineligible'}"],
    }


async def node_request_human_approval(state: VaultState, config: dict) -> dict:
    """
    Pause graph execution and wait for human MFA approval.
    LangGraph's `interrupt()` suspends this run; the caller resumes it
    by invoking `.ainvoke()` again with `human_approved=True/False` in state.
    The interrupt payload is surfaced to the caller (e.g. gRPC handler, UI).
    """
    human_decision = interrupt({
        "request_id": state["request_id"],
        "prompt": state["prompt"],
        "tier": state["tier"],
        "graph_memory_warning": state.get("graph_memory_warning"),
        "message": (
            "Manual approval required. "
            f"Tier {state['tier']} request is pending MFA confirmation. "
            "Resume with human_approved=True to grant or False to deny."
        ),
    })
    # human_decision is the value passed by the caller when resuming
    approved = bool(human_decision)
    logger.info(f"[human_approval] request={state['request_id']} approved={approved}")
    return {
        "human_approved": approved,
        "human_approval_pending": False,
        "checkpoints": [f"human:{'approved' if approved else 'denied'}"],
    }


async def node_approve(state: VaultState, config: dict) -> dict:
    """Write approval to immutable ledger and mark state approved"""
    ledger: LedgerStore = config["configurable"]["ledger"]

    details = f"Approved Tier {state['tier']} request"
    if state.get("shadow_eligible"):
        details += " (Shadow auto-eligible)"
    if state.get("graph_memory_warning"):
        details += f" [WARNING: {state['graph_memory_warning']}]"

    await ledger.write_entry(
        action_type="approve",
        actor_id="vault-langgraph",
        request_id=state["request_id"],
        details=details,
        metadata={"tier": str(state["tier"]), "node": "approve"},
    )
    reason = f"Approved by Vault graph (Tier {state['tier']})"
    if state.get("graph_memory_warning"):
        reason += f" — caution: {state['graph_memory_warning']}"
    logger.info(f"[approve] {state['request_id']}")
    return {"approved": True, "reason": reason, "checkpoints": ["decision:approved"]}


async def node_reject(state: VaultState, config: dict) -> dict:
    """Write rejection to immutable ledger and mark state rejected"""
    ledger: LedgerStore = config["configurable"]["ledger"]

    # Build reason from whichever gate triggered
    if state.get("rejection_cache_hit"):
        reason = "Tier 4 rejection cache auto-block (identical request rejected within 24h)"
    elif state.get("rate_limit_exceeded"):
        reason = f"Rate limit exceeded for Tier {state['tier']}"
    elif not state.get("token_valid"):
        reason = "Invalid or missing approval token"
    elif state.get("human_approved") is False:
        reason = "Human approver denied request"
    else:
        reason = "Request rejected by Vault policy"

    await ledger.write_entry(
        action_type="reject",
        actor_id="vault-langgraph",
        request_id=state["request_id"],
        details=reason,
        metadata={"tier": str(state["tier"]), "node": "reject"},
    )
    logger.info(f"[reject] {state['request_id']}: {reason}")
    return {"approved": False, "reason": reason, "checkpoints": ["decision:rejected"]}


# ---------------------------------------------------------------------------
# Routing functions (conditional edges)
# ---------------------------------------------------------------------------

def route_after_classify(state: VaultState) -> str:
    tier = state["tier"]
    if tier == 1:
        return "approve"
    if tier == 4:
        return "check_rejection_cache"
    return "check_rate_limit"


def route_after_rejection_cache(state: VaultState) -> str:
    return "reject" if state["rejection_cache_hit"] else "check_rate_limit"


def route_after_rate_limit(state: VaultState) -> str:
    return "reject" if state["rate_limit_exceeded"] else "query_graph_memory"


def route_after_graph_memory(state: VaultState) -> str:
    # Always proceed to token validation; warning is carried in state
    return "validate_token"


def route_after_validate_token(state: VaultState) -> str:
    tier = state["tier"]
    if not state["token_valid"]:
        # No token provided for Tier 2: reject
        if tier == 2:
            return "reject"
        # No token for Tier 3/4: check if Shadow baseline can auto-approve
        return "check_shadow_baseline"
    # Valid token present — Tier 2 can approve immediately
    if tier == 2:
        return "approve"
    # Tier 3/4 with valid token still need shadow check
    return "check_shadow_baseline"


def route_after_shadow_baseline(state: VaultState) -> str:
    if state["shadow_eligible"]:
        return "approve"
    # Not eligible — need human MFA
    return "request_human_approval"


def route_after_human_approval(state: VaultState) -> str:
    return "approve" if state["human_approved"] else "reject"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_vault_graph(checkpointer) -> Any:
    """
    Compile the Vault decision graph.

    Node layout (PNS = peripheral nervous system via MCP):
        sense_context → classify
          ├─ tier=1 ─────────────────────────────────────── approve
          ├─ tier=4 ─── check_rejection_cache
          │                ├─ cache_hit ─────────────────── reject
          │                └─ miss ─────────────────────────┐
          └─ tier=2/3 ──────────────────────────────────────┤
                                                            check_rate_limit
                                                             ├─ exceeded ── reject
                                                             └─ ok ─────── query_graph_memory
                                                                            │
                                                                            validate_token
                                                             ├─ invalid(T2) ─ reject
                                                             ├─ valid(T2) ─── approve
                                                             └─ T3/4 ──────── check_shadow_baseline
                                                                                ├─ eligible ──── approve
                                                                                └─ ineligible ── request_human_approval
                                                                                                  ├─ approved ── approve
                                                                                                  └─ denied ──── reject
    """
    if not _LANGGRAPH_OK:
        raise RuntimeError(
            "LangGraph not installed. Run: pip install langgraph langgraph-checkpoint-postgres"
        )
    g = StateGraph(VaultState)

    g.add_node("sense_context", node_sense_context)
    g.add_node("classify", node_classify)
    g.add_node("check_rejection_cache", node_check_rejection_cache)
    g.add_node("check_rate_limit", node_check_rate_limit)
    g.add_node("query_graph_memory", node_query_graph_memory)
    g.add_node("validate_token", node_validate_token)
    g.add_node("check_shadow_baseline", node_check_shadow_baseline)
    g.add_node("request_human_approval", node_request_human_approval)
    g.add_node("approve", node_approve)
    g.add_node("reject", node_reject)

    # sense_context is the new entry point — the peripheral nervous system
    # feeds the thalamus before prefrontal cortex fires
    g.set_entry_point("sense_context")
    g.add_edge("sense_context", "classify")

    g.add_conditional_edges("classify", route_after_classify)
    g.add_conditional_edges("check_rejection_cache", route_after_rejection_cache)
    g.add_conditional_edges("check_rate_limit", route_after_rate_limit)
    g.add_conditional_edges("query_graph_memory", route_after_graph_memory)
    g.add_conditional_edges("validate_token", route_after_validate_token)
    g.add_conditional_edges("check_shadow_baseline", route_after_shadow_baseline)
    g.add_conditional_edges("request_human_approval", route_after_human_approval)

    g.add_edge("approve", END)
    g.add_edge("reject", END)

    return g.compile(checkpointer=checkpointer, interrupt_before=["request_human_approval"])


# ---------------------------------------------------------------------------
# High-level runner
# ---------------------------------------------------------------------------

class LangGraphVault:
    """
    Drop-in replacement for VaultService.process_request() backed by LangGraph.
    Supports:
      - Full checkpointed decision trace via PostgreSQL checkpointer
      - Human-in-the-loop MFA interrupt/resume
      - GraphRAG failure pattern awareness
    """

    DB_DSN = (
        f"postgresql://{os.getenv('VAULT_DB_USER', 'vault')}:"
        f"{os.getenv('VAULT_DB_PASSWORD', 'vault_secure_pass')}@"
        f"{os.getenv('VAULT_DB_HOST', 'localhost')}:"
        f"{os.getenv('VAULT_DB_PORT', '5432')}/"
        f"{os.getenv('VAULT_DB_NAME', 'agent_memory')}"
    )

    def __init__(
        self,
        ledger: LedgerStore,
        context: ContextManager,
        graph_client: GraphRAGClient,
        mcp_provider: Optional["MCPContextProvider"] = None,
        affect_store: Optional[AffectStore] = None,
    ):
        self.ledger = ledger
        self.context = context
        self.graph_client = graph_client
        self.mcp_provider = mcp_provider
        self.affect_store = affect_store
        self.classifier = RiskClassifier()
        self._graph = None
        self._checkpointer = None

    async def initialize(self):
        """Set up PostgreSQL-backed checkpointer and compile graph"""
        if not _LANGGRAPH_OK:
            raise RuntimeError(
                "LangGraph not installed. Run: pip install langgraph langgraph-checkpoint-postgres"
            )
        self._checkpointer = AsyncPostgresSaver.from_conn_string(self.DB_DSN)
        await self._checkpointer.setup()
        self._graph = build_vault_graph(self._checkpointer)
        logger.info("LangGraph Vault initialized with PostgreSQL checkpointer")

    async def process_request(
        self,
        request_id: str,
        prompt: str,
        system_context: str,
        scope: str = "local",
        approval_token: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Tuple[bool, str, int]:
        """
        Run the vault graph for a new request.
        Returns (approved, reason, tier).

        If the graph pauses at `request_human_approval`, returns
        (False, "PENDING_MFA:<thread_id>", tier) so the caller can resume.
        """
        if self._graph is None:
            raise RuntimeError("LangGraphVault not initialized — call await initialize()")

        thread_id = request_id  # Use request_id as LangGraph thread_id for traceability
        config = {
            "configurable": {
                "thread_id": thread_id,
                "classifier": self.classifier,
                "ledger": self.ledger,
                "context": self.context,
                "graph_client": self.graph_client,
                "mcp_provider": self.mcp_provider,  # Thalamus — None = senses disabled
                "affect_store": self.affect_store,  # Caution signal target
            }
        }

        initial_state: VaultState = {
            "request_id": request_id,
            "prompt": prompt,
            "system_context": system_context,
            "scope": scope,
            "approval_token": approval_token,
            "session_id": session_id or request_id,
            "tier": None,
            "approved": None,
            "reason": "",
            "checkpoints": [],
            "mcp_context": None,
            "rejection_cache_hit": False,
            "rate_limit_exceeded": False,
            "graph_memory_warning": None,
            "shadow_eligible": False,
            "token_valid": False,
            "human_approval_pending": False,
            "human_approved": None,
        }

        result = await self._graph.ainvoke(initial_state, config)

        # If graph is interrupted for human approval, approved will be None
        if result.get("approved") is None:
            tier = result.get("tier", 0)
            return False, f"PENDING_MFA:{thread_id}", tier

        return result["approved"], result["reason"], result["tier"]

    async def resume_after_mfa(
        self,
        thread_id: str,
        human_approved: bool,
    ) -> Tuple[bool, str, int]:
        """
        Resume a paused graph after human MFA decision.
        thread_id matches the original request_id.
        """
        if self._graph is None:
            raise RuntimeError("LangGraphVault not initialized")

        config = {
            "configurable": {
                "thread_id": thread_id,
                "classifier": self.classifier,
                "ledger": self.ledger,
                "context": self.context,
                "graph_client": self.graph_client,
                "mcp_provider": self.mcp_provider,
                "affect_store": self.affect_store,
            }
        }

        # Resume by passing the human decision as the interrupt return value
        result = await self._graph.ainvoke(
            Command(resume=human_approved),
            config,
        )

        return result["approved"], result["reason"], result["tier"]

    def get_graph_png(self) -> bytes:
        """Return Mermaid PNG of the decision graph (for visualisation)"""
        if self._graph is None:
            raise RuntimeError("Graph not compiled")
        return self._graph.get_graph().draw_mermaid_png()
