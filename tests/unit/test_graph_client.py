"""
Unit tests for GraphRAGClient (internal/memory/graph/client.py).

All Neo4j and graphrag I/O is mocked.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest
import pytest_asyncio


def _make_client(neo4j_connected: bool = False, graphrag_loaded: bool = False):
    """Build a GraphRAGClient with all external dependencies patched."""
    from internal.memory.graph.client import GraphRAGClient

    client = GraphRAGClient(index_dir="./fake_index")
    # Skip real neo4j / graphrag initialisation
    client._neo4j_driver = MagicMock() if neo4j_connected else None
    client._graphrag_loaded = graphrag_loaded
    client._node_table = None
    client._edge_table = None
    client._community_table = None
    return client


class TestGraphRAGClientInit:
    @pytest.mark.asyncio
    async def test_initialize_gracefully_degrades_without_neo4j(self):
        """initialize() should not raise even when Neo4j/index are absent."""
        from internal.memory.graph.client import GraphRAGClient

        with patch.object(GraphRAGClient, "_init_neo4j", new_callable=AsyncMock) as mock_neo4j, \
             patch.object(GraphRAGClient, "_init_graphrag", new_callable=AsyncMock) as mock_gr:
            mock_neo4j.side_effect = Exception("Neo4j unreachable")
            mock_gr.side_effect = FileNotFoundError("index not found")

            client = GraphRAGClient(index_dir="./nonexistent")
            # Should not raise
            await client.initialize()


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

        # Should complete without raising
        await client.index_document(
            text="Another failure",
            doc_id="retro-002",
            source_type="failure_retrospective",
        )


class TestFindFailurePatterns:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_data(self):
        client = _make_client()
        client.neo4j_query = AsyncMock(return_value=[])

        results = await client.find_failure_patterns("timeout in deploy")
        assert results == []

    @pytest.mark.asyncio
    async def test_queries_neo4j_first(self):
        client = _make_client(neo4j_connected=True)
        expected = [{"id": "retro-001", "text": "deploy timeout", "source_type": "failure_retrospective"}]
        client.neo4j_query = AsyncMock(return_value=expected)

        results = await client.find_failure_patterns("deploy timeout")
        assert len(results) >= 1
        client.neo4j_query.assert_called_once()


class TestCheckBaselineEligibility:
    @pytest.mark.asyncio
    async def test_returns_tuple_of_three(self):
        client = _make_client()
        client.neo4j_query = AsyncMock(return_value=[])

        result = await client.check_baseline_eligibility("req-001", "build test prompt")
        assert isinstance(result, tuple)
        assert len(result) == 3
        eligible, reason, score = result
        assert isinstance(eligible, bool)
        assert isinstance(reason, str)
        assert isinstance(score, float)


class TestRecordRelationship:
    @pytest.mark.asyncio
    async def test_record_relationship_calls_neo4j_write(self):
        client = _make_client(neo4j_connected=True)
        client._neo4j_write = AsyncMock()

        await client.record_relationship(
            from_id="req-001",
            to_id="retro-001",
            rel_type="CAUSED_FAILURE",
        )

        client._neo4j_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_relationship_tolerates_missing_neo4j(self):
        client = _make_client(neo4j_connected=False)
        # Should complete silently
        await client.record_relationship("a", "b", "X")
