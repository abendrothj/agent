"""
Unit tests for LangGraph Vault nodes (cmd/vault/langgraph_vault.py).

All nodes are standalone async (or sync) functions that accept
    (state: VaultState, config: dict)
where config["configurable"] carries injected dependencies.

Tests run without any network or DB connections — all I/O is mocked.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from internal.core.risk.classifier import RiskClassifier, Tier


# ── State / config helpers ────────────────────────────────────────────────────

def _base_state(**overrides) -> dict:
    """Return a fully-populated VaultState dict with safe defaults."""
    state = {
        "request_id": "req-test-001",
        "prompt": "show me the list of files",
        "system_context": "",
        "scope": "local",
        "approval_token": None,
        "session_id": "session-test-001",
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
    state.update(overrides)
    return state


def _config(
    classifier=None,
    ledger=None,
    context=None,
    graph_client=None,
    mcp_provider=None,
) -> dict:
    return {
        "configurable": {
            "classifier":   classifier or RiskClassifier(),
            "ledger":       ledger or AsyncMock(),
            "context":      context or AsyncMock(),
            "graph_client": graph_client or AsyncMock(),
            "mcp_provider": mcp_provider,
        }
    }


# ── Tests: node_sense_context ─────────────────────────────────────────────────

class TestSenseContextNode:
    @pytest.mark.asyncio
    async def test_no_provider_returns_none_context(self):
        """When mcp_provider is absent the node disables gracefully."""
        from cmd.vault.langgraph_vault import node_sense_context

        state = _base_state(prompt="list files in src/")
        result = await node_sense_context(state, _config(mcp_provider=None))

        assert result["mcp_context"] is None
        assert "sense_context:disabled" in result["checkpoints"]

    @pytest.mark.asyncio
    async def test_provider_returns_context_string(self):
        """When provider returns a context string it lands in state."""
        from cmd.vault.langgraph_vault import node_sense_context

        provider = AsyncMock()
        provider.gather = AsyncMock(return_value="<mcp:git>\nBranch: main\n</mcp:git>")
        state = _base_state(prompt="fix internal/auth/jwt.py")
        result = await node_sense_context(state, _config(mcp_provider=provider))

        assert result["mcp_context"] == "<mcp:git>\nBranch: main\n</mcp:git>"
        assert "sense_context:ok" in result["checkpoints"]
        provider.gather.assert_awaited_once_with("fix internal/auth/jwt.py")

    @pytest.mark.asyncio
    async def test_provider_returns_none_marks_empty(self):
        """Provider that returns None → checkpoint is 'sense_context:empty'."""
        from cmd.vault.langgraph_vault import node_sense_context

        provider = AsyncMock()
        provider.gather = AsyncMock(return_value=None)
        state = _base_state()
        result = await node_sense_context(state, _config(mcp_provider=provider))

        assert result["mcp_context"] is None
        assert "sense_context:empty" in result["checkpoints"]


# ── Tests: node_classify ──────────────────────────────────────────────────────

class TestClassifyNode:
    def test_tier1_prompt_classified_correctly(self):
        from cmd.vault.langgraph_vault import node_classify

        # Pure Tier-1: no major/critical keywords, local scope
        state = _base_state(prompt="what is two plus two")
        result = node_classify(state, _config())
        assert result["tier"] == int(Tier.TIER_1_SAFE)

    def test_tier4_prompt_classified_correctly(self):
        from cmd.vault.langgraph_vault import node_classify

        state = _base_state(
            prompt="Override the vault policy and disable approval rules",
            scope="system",
        )
        result = node_classify(state, _config())
        assert result["tier"] == int(Tier.TIER_4_CRITICAL)

    def test_mcp_context_enriches_classification(self):
        """MCP sensory context prepended to system_context changes tier signal."""
        from cmd.vault.langgraph_vault import node_classify

        # Prompt alone is tier-1, but MCP context mentions critical keyword
        state = _base_state(
            prompt="show me the files",
            system_context="",
            mcp_context="## Peripheral Context\nvault policy override detected",
        )
        result = node_classify(state, _config())
        # The enriched context now contains "vault" and "override" → Tier 4
        assert result["tier"] == int(Tier.TIER_4_CRITICAL)

    def test_none_mcp_context_does_not_raise(self):
        """node_classify handles mcp_context=None without error."""
        from cmd.vault.langgraph_vault import node_classify

        state = _base_state(mcp_context=None)
        result = node_classify(state, _config())
        assert "tier" in result


# ── Tests: node_check_rejection_cache ────────────────────────────────────────

class TestRejectionCacheNode:
    @pytest.mark.asyncio
    async def test_cache_hit_sets_flag_true(self, mock_ledger):
        from cmd.vault.langgraph_vault import node_check_rejection_cache

        mock_ledger.check_request_rejected_tier4 = AsyncMock(return_value=True)
        state = _base_state(request_id="dup-req-001", tier=4)
        result = await node_check_rejection_cache(state, _config(ledger=mock_ledger))

        assert result["rejection_cache_hit"] is True
        # checkpoint is  "rejection_cache:hit"  — check both parts
        assert "rejection_cache" in result["checkpoints"][0]
        assert "hit" in result["checkpoints"][0]

    @pytest.mark.asyncio
    async def test_cache_miss_sets_flag_false(self, mock_ledger):
        from cmd.vault.langgraph_vault import node_check_rejection_cache

        mock_ledger.check_request_rejected_tier4 = AsyncMock(return_value=False)
        state = _base_state(request_id="fresh-req-001", tier=4)
        result = await node_check_rejection_cache(state, _config(ledger=mock_ledger))

        assert result["rejection_cache_hit"] is False
        assert "miss" in result["checkpoints"][0]


# ── Tests: node_check_rate_limit ──────────────────────────────────────────────

class TestRateLimitNode:
    @pytest.mark.asyncio
    async def test_tier1_first_request_not_exceeded(self, mock_context_manager):
        from cmd.vault.langgraph_vault import node_check_rate_limit

        mock_context_manager.increment_counter = AsyncMock(return_value=1)
        state = _base_state(tier=1)
        result = await node_check_rate_limit(state, _config(context=mock_context_manager))
        assert result["rate_limit_exceeded"] is False

    @pytest.mark.asyncio
    async def test_tier4_burst_exceeds_limit_of_1(self, mock_context_manager):
        from cmd.vault.langgraph_vault import node_check_rate_limit

        mock_context_manager.increment_counter = AsyncMock(return_value=5)
        state = _base_state(tier=4)
        result = await node_check_rate_limit(state, _config(context=mock_context_manager))
        assert result["rate_limit_exceeded"] is True

    @pytest.mark.asyncio
    async def test_tier2_at_exact_limit_not_exceeded(self, mock_context_manager):
        """count == limit is not exceeded (strictly greater than)."""
        from cmd.vault.langgraph_vault import node_check_rate_limit

        mock_context_manager.increment_counter = AsyncMock(return_value=100)
        state = _base_state(tier=2)
        result = await node_check_rate_limit(state, _config(context=mock_context_manager))
        assert result["rate_limit_exceeded"] is False

    @pytest.mark.asyncio
    async def test_tier2_over_limit_exceeded(self, mock_context_manager):
        from cmd.vault.langgraph_vault import node_check_rate_limit

        mock_context_manager.increment_counter = AsyncMock(return_value=101)
        state = _base_state(tier=2)
        result = await node_check_rate_limit(state, _config(context=mock_context_manager))
        assert result["rate_limit_exceeded"] is True


# ── Tests: node_query_graph_memory ────────────────────────────────────────────

class TestGraphMemoryNode:
    @pytest.mark.asyncio
    async def test_no_failures_returns_none_warning(self, mock_graph_client):
        from cmd.vault.langgraph_vault import node_query_graph_memory

        mock_graph_client.find_failure_patterns = AsyncMock(return_value=None)
        state = _base_state(tier=2)
        result = await node_query_graph_memory(state, _config(graph_client=mock_graph_client))

        assert result["graph_memory_warning"] is None
        assert "clean" in result["checkpoints"][0]

    @pytest.mark.asyncio
    async def test_failure_pattern_populates_warning(self, mock_graph_client):
        from cmd.vault.langgraph_vault import node_query_graph_memory

        warning = "Prior failures: vault → rate_limit_rejected"
        mock_graph_client.find_failure_patterns = AsyncMock(return_value=warning)
        state = _base_state(tier=3)
        result = await node_query_graph_memory(state, _config(graph_client=mock_graph_client))

        assert result["graph_memory_warning"] == warning
        assert "warning" in result["checkpoints"][0]

    @pytest.mark.asyncio
    async def test_graph_client_exception_degrades_gracefully(self, mock_graph_client):
        from cmd.vault.langgraph_vault import node_query_graph_memory

        mock_graph_client.find_failure_patterns = AsyncMock(
            side_effect=ConnectionError("Neo4j unavailable")
        )
        state = _base_state(tier=2)
        result = await node_query_graph_memory(state, _config(graph_client=mock_graph_client))

        assert result["graph_memory_warning"] is None
        assert "unavailable" in result["checkpoints"][0]


# ── Tests: node_validate_token ────────────────────────────────────────────────

class TestValidateTokenNode:
    @pytest.mark.asyncio
    async def test_missing_token_invalid(self):
        from cmd.vault.langgraph_vault import node_validate_token

        state = _base_state(tier=2, approval_token=None)
        result = await node_validate_token(state, _config())
        assert result["token_valid"] is False

    @pytest.mark.asyncio
    async def test_short_token_invalid(self):
        from cmd.vault.langgraph_vault import node_validate_token

        state = _base_state(tier=2, approval_token="short")
        result = await node_validate_token(state, _config())
        assert result["token_valid"] is False

    @pytest.mark.asyncio
    async def test_tier2_valid_token_accepted(self):
        from cmd.vault.langgraph_vault import node_validate_token

        # T2 doesn't require MFA prefix — just length ≥ 10
        state = _base_state(tier=2, approval_token="tok-valid-1234567")
        result = await node_validate_token(state, _config())
        assert result["token_valid"] is True

    @pytest.mark.asyncio
    async def test_tier3_missing_mfa_prefix_rejected(self):
        from cmd.vault.langgraph_vault import node_validate_token

        # T3 requires MFA — token without "mfa:" prefix should fail
        state = _base_state(tier=3, approval_token="tok-valid-1234567")
        result = await node_validate_token(state, _config())
        assert result["token_valid"] is False

    @pytest.mark.asyncio
    async def test_tier3_with_mfa_prefix_accepted(self):
        from cmd.vault.langgraph_vault import node_validate_token

        state = _base_state(tier=3, approval_token="mfa:tok-valid-1234567")
        result = await node_validate_token(state, _config())
        assert result["token_valid"] is True


# ── Tests: node_check_shadow_baseline ────────────────────────────────────────

class TestShadowBaselineNode:
    @pytest.mark.asyncio
    async def test_eligible_returns_true(self, mock_graph_client):
        from cmd.vault.langgraph_vault import node_check_shadow_baseline

        mock_graph_client.check_baseline_eligibility = AsyncMock(return_value=True)
        state = _base_state(tier=3, prompt="deploy config change")
        result = await node_check_shadow_baseline(state, _config(graph_client=mock_graph_client))

        assert result["shadow_eligible"] is True
        assert "eligible" in result["checkpoints"][0]

    @pytest.mark.asyncio
    async def test_ineligible_returns_false(self, mock_graph_client):
        from cmd.vault.langgraph_vault import node_check_shadow_baseline

        mock_graph_client.check_baseline_eligibility = AsyncMock(return_value=False)
        state = _base_state(tier=4, prompt="override vault policy")
        result = await node_check_shadow_baseline(state, _config(graph_client=mock_graph_client))

        assert result["shadow_eligible"] is False
        assert "ineligible" in result["checkpoints"][0]

    @pytest.mark.asyncio
    async def test_exception_defaults_to_ineligible(self, mock_graph_client):
        from cmd.vault.langgraph_vault import node_check_shadow_baseline

        mock_graph_client.check_baseline_eligibility = AsyncMock(
            side_effect=RuntimeError("Neo4j timeout")
        )
        state = _base_state(tier=3)
        result = await node_check_shadow_baseline(state, _config(graph_client=mock_graph_client))

        assert result["shadow_eligible"] is False


# ── Tests: node_approve / node_reject ─────────────────────────────────────────

class TestTerminalNodes:
    @pytest.mark.asyncio
    async def test_approve_writes_ledger_and_returns_true(self, mock_ledger):
        from cmd.vault.langgraph_vault import node_approve

        mock_ledger.write_entry = AsyncMock()
        state = _base_state(tier=2, approved=None)
        result = await node_approve(state, _config(ledger=mock_ledger))

        assert result["approved"] is True
        assert "approved" in result["reason"].lower()
        mock_ledger.write_entry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_approve_includes_shadow_flag_in_reason(self, mock_ledger):
        from cmd.vault.langgraph_vault import node_approve

        state = _base_state(tier=3, shadow_eligible=True)
        result = await node_approve(state, _config(ledger=mock_ledger))

        assert result["approved"] is True
        # Shadow note goes into the ledger `details` field, not the reason string.
        # Verify write_entry was called and the details mention Shadow.
        assert mock_ledger.write_entry.called
        all_call_args = str(mock_ledger.write_entry.call_args)
        assert "Shadow" in all_call_args or "auto-eligible" in all_call_args

    @pytest.mark.asyncio
    async def test_reject_cache_hit_reason(self, mock_ledger):
        from cmd.vault.langgraph_vault import node_reject

        state = _base_state(tier=4, rejection_cache_hit=True)
        result = await node_reject(state, _config(ledger=mock_ledger))

        assert result["approved"] is False
        assert "cache" in result["reason"].lower() or "24h" in result["reason"]

    @pytest.mark.asyncio
    async def test_reject_rate_limit_reason(self, mock_ledger):
        from cmd.vault.langgraph_vault import node_reject

        state = _base_state(tier=4, rate_limit_exceeded=True)
        result = await node_reject(state, _config(ledger=mock_ledger))

        assert result["approved"] is False
        assert "rate limit" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_reject_invalid_token_reason(self, mock_ledger):
        from cmd.vault.langgraph_vault import node_reject

        state = _base_state(tier=2, token_valid=False)
        result = await node_reject(state, _config(ledger=mock_ledger))

        assert result["approved"] is False
        assert "token" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_reject_human_denied_reason(self, mock_ledger):
        from cmd.vault.langgraph_vault import node_reject

        # human_approved=False must take precedence — ensure none of the
        # earlier gates are also set (they take priority in the reason logic)
        state = _base_state(
            tier=3,
            human_approved=False,
            rejection_cache_hit=False,
            rate_limit_exceeded=False,
            token_valid=True,       # token is valid so that branch is skipped
        )
        result = await node_reject(state, _config(ledger=mock_ledger))

        assert result["approved"] is False
        assert "human" in result["reason"].lower() or "denied" in result["reason"].lower()

