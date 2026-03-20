"""
Unit tests for GraphRAGClient (internal/memory/graph/client.py).

All Neo4j I/O is mocked — tests run without a running Neo4j instance.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


def _make_client(neo4j_connected: bool = False):
    """Build a GraphRAGClient with all external dependencies patched."""
    from internal.memory.graph.client import GraphRAGClient

    client = GraphRAGClient()
    client._neo4j_driver = MagicMock() if neo4j_connected else None
    client._neo4j_available = neo4j_connected
    return client


class TestGraphRAGClientInit:
    @pytest.mark.asyncio
    async def test_initialize_raises_when_neo4j_unreachable(self):
        """_init_neo4j() raises — initialize() propagates it (no swallowing)."""
        from internal.memory.graph.client import GraphRAGClient

        with patch.object(GraphRAGClient, "_init_neo4j", new_callable=AsyncMock) as mock_neo4j:
            mock_neo4j.side_effect = Exception("Neo4j unreachable")

            client = GraphRAGClient()
            with pytest.raises(Exception, match="Neo4j unreachable"):
                await client.initialize()

    @pytest.mark.asyncio
    async def test_initialize_succeeds_when_neo4j_available(self):
        from internal.memory.graph.client import GraphRAGClient

        with patch.object(GraphRAGClient, "_init_neo4j", new_callable=AsyncMock):
            client = GraphRAGClient()
            await client.initialize()   # should not raise


class TestIndexDocument:
    @pytest.mark.asyncio
    async def test_index_document_writes_to_neo4j(self):
        client = _make_client(neo4j_connected=True)
        client._neo4j_write = AsyncMock(return_value=None)

        await client.index_document(
            text="Failure in deploy step: timeout",
            doc_id="retro-001",
            source_type="failure_retrospective",
        )

        client._neo4j_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_index_document_handles_neo4j_down(self):
        """Should not raise when Neo4j is unavailable."""
        client = _make_client(neo4j_connected=False)
        # _neo4j_write is a no-op when unavailable
        await client.index_document(
            text="Another failure",
            doc_id="retro-002",
            source_type="failure_retrospective",
        )


class TestFindFailurePatterns:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_data(self):
        client = _make_client(neo4j_connected=True)
        client.neo4j_query = AsyncMock(return_value=[])

        result = await client.find_failure_patterns("timeout in deploy", tier=3)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_warning_string_on_hit(self):
        client = _make_client(neo4j_connected=True)
        client.neo4j_query = AsyncMock(return_value=[
            {"src": "failure_retrospective", "failure_name": "deploy timeout"}
        ])

        result = await client.find_failure_patterns("deploy timeout", tier=3)
        assert result is not None
        assert "Prior failures" in result
        client.neo4j_query.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_neo4j_unavailable(self):
        client = _make_client(neo4j_connected=False)
        result = await client.find_failure_patterns("anything", tier=2)
        assert result is None


class TestCheckBaselineEligibility:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_successes(self):
        client = _make_client(neo4j_connected=True)
        client.neo4j_query = AsyncMock(return_value=[{"successes": 0}])

        result = await client.check_baseline_eligibility("build test prompt", tier=2)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_when_successes_found(self):
        client = _make_client(neo4j_connected=True)
        client.neo4j_query = AsyncMock(return_value=[{"successes": 3}])

        result = await client.check_baseline_eligibility("build test prompt", tier=2)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_neo4j_unavailable(self):
        client = _make_client(neo4j_connected=False)
        result = await client.check_baseline_eligibility("anything", tier=1)
        assert result is False


class TestRecordRelationship:
    @pytest.mark.asyncio
    async def test_record_relationship_calls_neo4j_write(self):
        client = _make_client(neo4j_connected=True)
        client._neo4j_write = AsyncMock()

        await client.record_relationship(
            source_id="req-001",
            target_id="retro-001",
            relation="CAUSED",
        )

        client._neo4j_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_relationship_tolerates_missing_neo4j(self):
        client = _make_client(neo4j_connected=False)
        # _neo4j_write is a no-op when unavailable — should not raise
        await client.record_relationship("a", "b", "X")


