"""
Vault Service - Main orchestrator and approval gating
Runs on Pi as central decision maker.

Control flow is implemented as a LangGraph StateGraph (see langgraph_vault.py)
for explicit routing, human-in-the-loop MFA interrupts, and PostgreSQL checkpointing.
Long-term memory uses GraphRAG (see internal/memory/graph/client.py) for
relationship-aware retrieval over PR history and failure retrospectives.
"""
import logging
import asyncio
from typing import Optional, Tuple
from datetime import datetime
import os

from internal.core.risk.classifier import RiskClassifier, Tier
from internal.memory.ledger.store import LedgerStore
from internal.memory.context.manager import ContextManager
from internal.memory.graph.client import GraphRAGClient
from internal.memory.vector.client import VectorClient
from internal.mcp.client import MCPContextProvider
from internal.git.identity import GitIdentity
from internal.git.github_client import GitHubClient
from cmd.vault.langgraph_vault import LangGraphVault
from cmd.vault.autonomy_loop import AutonomyLoop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class VaultService:
    """
    Vault orchestrator — delegates all approval logic to LangGraphVault.
    Public API is unchanged so the gRPC server layer needs no modification.
    """

    # Configuration from environment
    DB_HOST = os.getenv("VAULT_DB_HOST", "localhost")
    DB_PORT = int(os.getenv("VAULT_DB_PORT", "5432"))
    DB_NAME = os.getenv("VAULT_DB_NAME", "agent_memory")
    DB_USER = os.getenv("VAULT_DB_USER", "vault")
    DB_PASSWORD = os.getenv("VAULT_DB_PASSWORD", "vault_secure_pass")

    REDIS_HOST = os.getenv("VAULT_REDIS_HOST", "localhost")
    REDIS_PORT = int(os.getenv("VAULT_REDIS_PORT", "6379"))

    GRPC_PORT = int(os.getenv("VAULT_GRPC_PORT", "50051"))  # LAN port Win11 Muscle connects to
    GRPC_HOST = os.getenv("VAULT_GRPC_HOST", "0.0.0.0")

    MFA_TIMEOUT_SECONDS = int(os.getenv("VAULT_MFA_TIMEOUT", "600"))
    TOKEN_TTL_HOURS = int(os.getenv("VAULT_TOKEN_TTL", "24"))

    def __init__(self):
        self.classifier = RiskClassifier()
        self.ledger: Optional[LedgerStore] = None
        self.context: Optional[ContextManager] = None
        self.vector_client: Optional[VectorClient] = None
        self.graph_client: Optional[GraphRAGClient] = None
        self._mcp_provider: Optional[MCPContextProvider] = None
        self._lg_vault: Optional[LangGraphVault] = None
        self._git_identity: GitIdentity = GitIdentity()
        self._github_client: GitHubClient = GitHubClient()
        self._autonomy_loop: Optional[AutonomyLoop] = None

    async def initialize(self):
        """Initialize Vault services"""
        logger.info("Initializing Vault Service (LangGraph mode)...")

        self.ledger = LedgerStore(
            db_host=self.DB_HOST,
            db_port=self.DB_PORT,
            db_name=self.DB_NAME,
            db_user=self.DB_USER,
            db_password=self.DB_PASSWORD,
        )
        await self.ledger.connect()

        self.context = ContextManager(
            redis_host=self.REDIS_HOST,
            redis_port=self.REDIS_PORT,
        )
        await self.context.connect()

        # Vector memory — also injected into GraphRAGClient for hybrid semantic lookup
        self.vector_client = VectorClient(
            db_host=self.DB_HOST,
            db_port=self.DB_PORT,
            db_name=self.DB_NAME,
            db_user=self.DB_USER,
            db_password=self.DB_PASSWORD,
        )
        await self.vector_client.connect()

        # Neo4j graph memory
        self.graph_client = GraphRAGClient(vector_client=self.vector_client)
        await self.graph_client.initialize()

        # MCP context provider — the Thalamus (peripheral sensory routing)
        # Feeds git state + relevant file context into node_sense_context before
        # the risk classifier fires.  Disabled = senses offline, agent still works.
        mcp_enabled = os.getenv("MCP_ENABLED", "true").lower() == "true"
        workspace   = os.getenv("MCP_WORKSPACE_PATH", "/app")
        self._mcp_provider = MCPContextProvider(
            workspace_path=workspace,
            enabled=mcp_enabled,
            timeout_seconds=float(os.getenv("MCP_TIMEOUT_SECONDS", "2.0")),
        )
        await self._mcp_provider.initialize()

        # LangGraph vault — wires together all dependencies
        self._lg_vault = LangGraphVault(
            ledger=self.ledger,
            context=self.context,
            graph_client=self.graph_client,
            mcp_provider=self._mcp_provider,
        )
        await self._lg_vault.initialize()

        # Git identity — generates SSH keypair on first boot, prints pub key to logs
        await self._git_identity.initialize()

        # Autonomy loop — self-directed contribution drive (affect-gated)
        self._autonomy_loop = AutonomyLoop(
            graph_client=self.graph_client,
            github_client=self._github_client,
            identity=self._git_identity,
            vault_service=self,
            affect_store=None,   # wired in after affect store is available
        )
        self._autonomy_loop.start()

        logger.info("Vault Service initialized successfully")
    
    async def shutdown(self):
        """Graceful shutdown"""
        logger.info("Shutting down Vault Service...")
        if self._autonomy_loop:
            await self._autonomy_loop.stop()
        if self._lg_vault:
            await self._lg_vault.teardown()
        if self.ledger:
            await self.ledger.disconnect()
        if self.context:
            await self.context.disconnect()
        if self.vector_client:
            await self.vector_client.disconnect()
        logger.info("Vault Service shut down")

    async def process_request(
        self,
        request_id: str,
        prompt: str,
        system_context: str,
        scope: str = "local",
        approval_token: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Tuple[bool, str, Tier]:
        """
        Main approval gating — delegated to LangGraph state machine.

        Returns: (approved, reason, tier)

        If the graph pauses for human MFA approval, reason will be
        "PENDING_MFA:<request_id>" and approved will be False.
        The caller should invoke resume_after_mfa() once the human decides.
        """
        if self._lg_vault is None:
            raise RuntimeError("VaultService not initialized")

        approved, reason, tier_int = await self._lg_vault.process_request(
            request_id=request_id,
            prompt=prompt,
            system_context=system_context,
            scope=scope,
            approval_token=approval_token,
            session_id=session_id or request_id,
        )
        return approved, reason, Tier(tier_int)

    async def process_autonomous_request(
        self,
        request_id: str,
        prompt: str,
        tier_hint: int = 2,
    ) -> dict:
        """
        Entry point for the AutonomyLoop.

        Wraps process_request() with autonomy-appropriate defaults and returns
        a dict so callers can do result.get("approved") / result.get("code_patch").
        tier_hint is embedded in system_context for classifier context; the
        LangGraph classifier makes the final tier determination.
        """
        system_context = f"autonomous_contribution tier_hint={tier_hint}"
        approved, reason, _tier = await self.process_request(
            request_id=request_id,
            prompt=prompt,
            system_context=system_context,
            scope="external",
            session_id=request_id,
        )
        return {"approved": approved, "reason": reason, "code_patch": None}

    async def resume_after_mfa(
        self,
        request_id: str,
        human_approved: bool,
    ) -> Tuple[bool, str, Tier]:
        """
        Resume a LangGraph run that paused for human MFA approval.
        Call this after the human confirms/denies via the MFA channel.
        """
        if self._lg_vault is None:
            raise RuntimeError("VaultService not initialized")

        approved, reason, tier_int = await self._lg_vault.resume_after_mfa(
            thread_id=request_id,
            human_approved=human_approved,
        )
        return approved, reason, Tier(tier_int)

    async def get_state(self) -> dict:
        """Get Vault ledger state info"""
        if not self.ledger:
            raise RuntimeError("Ledger not initialized")

        approvals_count = await self.ledger.get_approval_count()
        rejections_24h = await self.ledger.get_rejection_count_24h()
        ledger_size = await self.ledger.get_ledger_size()

        return {
            "approvals_total": approvals_count,
            "rejections_24h": rejections_24h,
            "ledger_entries": ledger_size,
            "timestamp_ms": int(datetime.utcnow().timestamp() * 1000),
        }

    async def enforce_rate_limit(
        self,
        session_id: str,
        tier: Tier,
    ) -> Tuple[bool, str]:
        """
        Enforce per-tier rate limits (also handled inside LangGraph graph,
        kept here for direct callers that need a pre-flight check).
        """
        if not self.context:
            raise RuntimeError("Context manager not initialized")

        session_exists = await self.context.exists_session(session_id)
        if not session_exists:
            await self.context.create_session(session_id)

        tier_limits = {
            Tier.TIER_1_SAFE: 1000,
            Tier.TIER_2_MINOR: 100,
            Tier.TIER_3_MAJOR: 10,
            Tier.TIER_4_CRITICAL: 1,
        }
        limit_per_hour = tier_limits.get(tier, 10)
        counter_key = f"requests_tier_{tier}_hour"
        count = await self.context.increment_counter(session_id, counter_key)

        if count > limit_per_hour:
            return False, f"Rate limit exceeded: {count}/{limit_per_hour} per hour for Tier {tier}"
        return True, f"Rate limit OK: {count}/{limit_per_hour}"


async def main():
    """Run Vault service"""
    from cmd.vault.grpc_server import start_grpc_server

    vault = VaultService()

    try:
        await vault.initialize()
        server = await start_grpc_server(vault, vault.GRPC_HOST, vault.GRPC_PORT)
        logger.info(f"Vault gRPC ready on {vault.GRPC_HOST}:{vault.GRPC_PORT}")
        await server.wait_for_termination()

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise

    finally:
        await vault.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
