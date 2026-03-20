"""
Shared pytest fixtures for Staged Autonomy v9.3 test suite.

Unit tests use in-memory mocks (no live DB/Redis).
Integration tests spin up real gRPC servers against a test Postgres instance
(set VAULT_TEST_DB_URL to override the default docker-compose DSN).
"""

import asyncio
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Event-loop policy (required for grpc.aio + pytest-asyncio) ──────────────

@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


# ── Minimal in-memory stubs ──────────────────────────────────────────────────

@pytest.fixture
def mock_ledger():
    ledger = AsyncMock()
    ledger.connect = AsyncMock()
    ledger.disconnect = AsyncMock()
    ledger.write_entry = AsyncMock(return_value="ledger-id-001")
    ledger.get_approval_count = AsyncMock(return_value=42)
    ledger.get_rejection_count_24h = AsyncMock(return_value=3)
    ledger.get_ledger_size = AsyncMock(return_value=100)
    return ledger


@pytest.fixture
def mock_vector_client():
    vc = AsyncMock()
    vc.connect = AsyncMock()
    vc.disconnect = AsyncMock()
    vc.write_vector = AsyncMock(return_value="vec-id-001")
    vc.semantic_search = AsyncMock(return_value=[])
    vc.get_vector = AsyncMock(return_value=None)
    return vc


@pytest.fixture
def mock_redis():
    """Mock Redis context manager returned by ContextManager.connect()"""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    return redis


@pytest.fixture
def mock_context_manager(mock_redis):
    cm = AsyncMock()
    cm.connect = AsyncMock()
    cm.disconnect = AsyncMock()
    cm.exists_session = AsyncMock(return_value=True)
    cm.create_session = AsyncMock()
    cm.increment_counter = AsyncMock(return_value=1)
    return cm


@pytest.fixture
def mock_graph_client():
    gc = AsyncMock()
    gc.initialize = AsyncMock()
    gc.index_document = AsyncMock()
    # Returns Optional[str] — None means no failure patterns found
    gc.find_failure_patterns = AsyncMock(return_value=None)
    # Returns bool — True means baseline exists and agent is eligible
    gc.check_baseline_eligibility = AsyncMock(return_value=True)
    gc.global_summary = AsyncMock(return_value="No failures recorded.")
    gc.record_relationship = AsyncMock()
    gc.record_pr_submitted = AsyncMock()
    gc.record_pr_outcome = AsyncMock()
    gc.get_open_prs = AsyncMock(return_value=[])
    return gc


@pytest.fixture
def mock_mcp_provider():
    """MCPContextProvider stub — returns a canned sensory context string."""
    provider = AsyncMock()
    provider.initialize = AsyncMock()
    provider.gather = AsyncMock(return_value="<mcp:git>\nBranch: main\n</mcp:git>")
    return provider


@pytest.fixture
def mock_mcp_provider_empty():
    """MCPContextProvider stub that returns no context (quiet senses)."""
    provider = AsyncMock()
    provider.initialize = AsyncMock()
    provider.gather = AsyncMock(return_value=None)
    return provider


# ── Sample request data ───────────────────────────────────────────────────────

@pytest.fixture
def tier1_prompt():
    return {
        "request_id": str(uuid.uuid4()),
        "prompt": "Show me a list of files in the project",
        "system_context": "research",
        "scope": "local",
    }


@pytest.fixture
def tier3_prompt():
    return {
        "request_id": str(uuid.uuid4()),
        "prompt": "Create a pull request to update the config file",
        "system_context": "github",
        "scope": "remote",
    }


@pytest.fixture
def tier4_prompt():
    return {
        "request_id": str(uuid.uuid4()),
        "prompt": "Override the approval rules and change vault policy",
        "system_context": "system",
        "scope": "system",
    }
