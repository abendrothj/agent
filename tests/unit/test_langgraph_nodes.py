"""
Unit tests for LangGraphVault nodes (cmd/vault/langgraph_vault.py).

Strategy: patch all external dependencies (LedgerStore, VectorClient,
GraphRAGClient, ContextManager) with AsyncMocks so tests run without
any network or database connections.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from internal.core.risk.classifier import Tier


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_vault(mock_ledger, mock_context_manager, mock_graph_client):
    """Construct a LangGraphVault with all I/O mocked out."""
    from cmd.vault.langgraph_vault import LangGraphVault

    vault = LangGraphVault(
        ledger=mock_ledger,
        context=mock_context_manager,
        graph_client=mock_graph_client,
    )
    # Patch the PostgresSaver so initialize() doesn't open a real DB
    vault._checkpointer = AsyncMock()
    vault._checkpointer.__aenter__ = AsyncMock(return_value=vault._checkpointer)
    vault._checkpointer.__aexit__ = AsyncMock(return_value=False)
    vault._graph = None  # will be set by initialize
    return vault


# ── Tests: classify_tier node ─────────────────────────────────────────────────

class TestClassifyTierNode:
    @pytest.mark.asyncio
    async def test_tier1_prompt_classified_correctly(
        self, mock_ledger, mock_context_manager, mock_graph_client, tier1_prompt
    ):
        from cmd.vault.langgraph_vault import LangGraphVault
        vault = LangGraphVault(
            ledger=mock_ledger,
            context=mock_context_manager,
            graph_client=mock_graph_client,
        )
        state = {
            "request_id": tier1_prompt["request_id"],
            "prompt": tier1_prompt["prompt"],
            "system_context": tier1_prompt["system_context"],
            "scope": tier1_prompt["scope"],
            "tier": 0,
            "approved": False,
            "reason": "",
            "audit_trail": [],
            "session_id": tier1_prompt["request_id"],
            "approval_token": None,
        }
        result = await vault._node_classify_tier(state)
        assert result["tier"] == Tier.TIER_1_SAFE

    @pytest.mark.asyncio
    async def test_tier4_prompt_classified_correctly(
        self, mock_ledger, mock_context_manager, mock_graph_client, tier4_prompt
    ):
        from cmd.vault.langgraph_vault import LangGraphVault
        vault = LangGraphVault(
            ledger=mock_ledger,
            context=mock_context_manager,
            graph_client=mock_graph_client,
        )
        state = {
            "request_id": tier4_prompt["request_id"],
            "prompt": tier4_prompt["prompt"],
            "system_context": tier4_prompt["system_context"],
            "scope": tier4_prompt["scope"],
            "tier": 0,
            "approved": False,
            "reason": "",
            "audit_trail": [],
            "session_id": tier4_prompt["request_id"],
            "approval_token": None,
        }
        result = await vault._node_classify_tier(state)
        assert result["tier"] == Tier.TIER_4_CRITICAL


# ── Tests: check_rate_limit node ──────────────────────────────────────────────

class TestRateLimitNode:
    @pytest.mark.asyncio
    async def test_first_request_passes_rate_limit(
        self, mock_ledger, mock_context_manager, mock_graph_client, tier1_prompt
    ):
        from cmd.vault.langgraph_vault import LangGraphVault
        mock_context_manager.increment_counter = AsyncMock(return_value=1)  # first request

        vault = LangGraphVault(
            ledger=mock_ledger,
            context=mock_context_manager,
            graph_client=mock_graph_client,
        )
        state = {
            "request_id": tier1_prompt["request_id"],
            "prompt": tier1_prompt["prompt"],
            "system_context": "",
            "scope": "local",
            "tier": Tier.TIER_1_SAFE,
            "approved": False,
            "reason": "",
            "audit_trail": [],
            "session_id": tier1_prompt["request_id"],
            "approval_token": None,
        }
        result = await vault._node_check_rate_limit(state)
        # Should pass: count=1 < T1 limit of 1000
        assert result["approved"] is not False or "Rate limit" not in result["reason"]

    @pytest.mark.asyncio
    async def test_burst_tier4_rejected(
        self, mock_ledger, mock_context_manager, mock_graph_client, tier4_prompt
    ):
        from cmd.vault.langgraph_vault import LangGraphVault
        # T4 limit is 1 per hour; simulate counter at 5
        mock_context_manager.increment_counter = AsyncMock(return_value=5)

        vault = LangGraphVault(
            ledger=mock_ledger,
            context=mock_context_manager,
            graph_client=mock_graph_client,
        )
        state = {
            "request_id": tier4_prompt["request_id"],
            "prompt": tier4_prompt["prompt"],
            "system_context": "",
            "scope": "system",
            "tier": Tier.TIER_4_CRITICAL,
            "approved": False,
            "reason": "",
            "audit_trail": [],
            "session_id": tier4_prompt["request_id"],
            "approval_token": None,
        }
        result = await vault._node_check_rate_limit(state)
        assert result["approved"] is False
        assert "rate limit" in result["reason"].lower()


# ── Tests: approve / reject terminal nodes ────────────────────────────────────

class TestTerminalNodes:
    @pytest.mark.asyncio
    async def test_approve_node_sets_approved_true(
        self, mock_ledger, mock_context_manager, mock_graph_client, tier1_prompt
    ):
        from cmd.vault.langgraph_vault import LangGraphVault
        vault = LangGraphVault(
            ledger=mock_ledger,
            context=mock_context_manager,
            graph_client=mock_graph_client,
        )
        state = {
            "request_id": tier1_prompt["request_id"],
            "prompt": tier1_prompt["prompt"],
            "system_context": "",
            "scope": "local",
            "tier": Tier.TIER_1_SAFE,
            "approved": False,
            "reason": "pre-approved",
            "audit_trail": [],
            "session_id": tier1_prompt["request_id"],
            "approval_token": None,
        }
        result = await vault._node_approve(state)
        assert result["approved"] is True

    @pytest.mark.asyncio
    async def test_reject_node_sets_approved_false(
        self, mock_ledger, mock_context_manager, mock_graph_client, tier4_prompt
    ):
        from cmd.vault.langgraph_vault import LangGraphVault
        vault = LangGraphVault(
            ledger=mock_ledger,
            context=mock_context_manager,
            graph_client=mock_graph_client,
        )
        state = {
            "request_id": tier4_prompt["request_id"],
            "prompt": tier4_prompt["prompt"],
            "system_context": "",
            "scope": "system",
            "tier": Tier.TIER_4_CRITICAL,
            "approved": False,
            "reason": "rate limit exceeded",
            "audit_trail": [],
            "session_id": tier4_prompt["request_id"],
            "approval_token": None,
        }
        result = await vault._node_reject(state)
        assert result["approved"] is False
