"""
GraphRAG Memory Client
Uses Microsoft's graphrag package to build and query a knowledge graph
over PR history, failure retrospectives, and code change summaries.

Two-layer architecture:
  Layer 1 — Neo4j (bolt://memory_store:7687):
    Real-time writes via the neo4j Bolt driver.  Every failure retrospective,
    PR description, and approval decision is written here immediately so the
    graph is always current without waiting for a full graphrag index rebuild.
    Nodes: Document, Entity (file/function/PR/failure/deployment)
    Relationships: CAUSED, FIXED, MODIFIED, INTRODUCED, APPROVED, REJECTED

  Layer 2 — graphrag (parquet/DuckDB, optional):
    Periodic index rebuild using LLM entity extraction for richer community
    summaries and semantic search.  Falls back gracefully if unavailable.
    Vault queries this layer before every T3/T4 approval (find_failure_patterns).
"""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class GraphEntity:
    id: str
    name: str
    type: str              # "file", "function", "pr", "failure", "developer"
    description: str
    community_id: Optional[str] = None


@dataclass
class GraphRelationship:
    source: str            # entity id
    target: str            # entity id
    relation: str          # "modified", "caused", "fixed", "introduced", "related_to"
    weight: float = 1.0
    description: str = ""


@dataclass
class GraphQueryResult:
    answer: str
    entities: List[GraphEntity] = field(default_factory=list)
    relationships: List[GraphRelationship] = field(default_factory=list)
    confidence: float = 0.0


class GraphRAGClient:
    """
    Hybrid GraphRAG + Neo4j memory client.

    Neo4j (Layer 1) is always active when NEO4J_URI is set:
      - Real-time writes on every retrospective/PR/decision
      - Direct Cypher queries for low-latency lookups
      - Deployed as `memory_store` container in docker-compose

    graphrag parquet index (Layer 2) is loaded when the index exists:
      - LLM-extracted entity summaries and community reports
      - Used for Vault's find_failure_patterns() and global_summary()
      - Rebuild nightly: graphrag index --root graphrag_index
    """

    # Neo4j env — populated by docker-compose from `memory_store` container
    NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "super_secure_password")

    def __init__(
        self,
        index_dir: Optional[str] = None,
        llm_model: str = "gpt-4o-mini",
        embedding_model: str = "text-embedding-3-small",
        community_level: int = 2,
    ):
        self.index_dir = Path(index_dir or os.getenv("GRAPHRAG_INDEX_DIR", "./graphrag_index"))
        self.llm_model = llm_model
        self.embedding_model = embedding_model
        self.community_level = community_level
        self._search_engine = None
        self._global_search_engine = None
        self._available = False          # graphrag parquet index
        self._neo4j_driver = None        # neo4j Bolt driver
        self._neo4j_available = False

    async def initialize(self):
        """Connect to Neo4j (Layer 1) and load graphrag index (Layer 2, optional)"""
        await self._init_neo4j()
        await self._init_graphrag()

    async def _init_neo4j(self):
        """Connect to the Neo4j Bolt endpoint on the Pi memory_store container"""
        try:
            from neo4j import AsyncGraphDatabase

            self._neo4j_driver = AsyncGraphDatabase.driver(
                self.NEO4J_URI,
                auth=(self.NEO4J_USER, self.NEO4J_PASSWORD),
            )
            # Verify connectivity
            async with self._neo4j_driver.session() as session:
                await session.run("RETURN 1")

            # Ensure indexes exist for fast lookups
            async with self._neo4j_driver.session() as session:
                await session.run(
                    "CREATE INDEX doc_id_idx IF NOT EXISTS "
                    "FOR (d:Document) ON (d.doc_id)"
                )
                await session.run(
                    "CREATE INDEX entity_name_idx IF NOT EXISTS "
                    "FOR (e:Entity) ON (e.name, e.type)"
                )

            self._neo4j_available = True
            logger.info(f"Neo4j connected at {self.NEO4J_URI}")

        except ImportError:
            logger.warning(
                "neo4j package not installed. Run: pip install neo4j. "
                "Real-time graph writes disabled."
            )
        except Exception as exc:
            logger.warning(
                f"Neo4j connection failed ({self.NEO4J_URI}): {exc}. "
                "Continuing without real-time graph writes."
            )

    async def _init_graphrag(self):
        """Load graphrag parquet index for LLM-enhanced search (optional Layer 2)"""
        if not self.index_dir.exists():
            logger.warning(
                f"GraphRAG index directory not found at {self.index_dir}. "
                "Build with: graphrag index --root graphrag_index"
            )
            return

        try:
            from graphrag.query.context_builder.entity_extraction import EntityVectorStoreKey
            from graphrag.query.input.loaders.dfs import store_entity_semantic_embeddings
            from graphrag.query.llm.oai.chat_openai import ChatOpenAI
            from graphrag.query.llm.oai.embedding import OpenAIEmbedding
            from graphrag.query.llm.oai.typing import OpenaiApiType
            from graphrag.query.structured_search.local_search.mixed_context import (
                LocalSearchMixedContext,
            )
            from graphrag.query.structured_search.local_search.search import LocalSearch
            from graphrag.query.structured_search.global_search.community_context import (
                GlobalCommunityContext,
            )
            from graphrag.query.structured_search.global_search.search import GlobalSearch
            import pandas as pd

            output_dir = self.index_dir / "output"
            parquet_files = list(output_dir.glob("**/*.parquet"))
            if not parquet_files:
                logger.warning("GraphRAG index exists but no parquet output found. Re-run indexing.")
                return

            # Load entity and relationship tables
            entities_df = pd.read_parquet(output_dir / "create_final_entities.parquet")
            relationships_df = pd.read_parquet(output_dir / "create_final_relationships.parquet")
            communities_df = pd.read_parquet(output_dir / "create_final_communities.parquet")
            community_reports_df = pd.read_parquet(output_dir / "create_final_community_reports.parquet")
            text_units_df = pd.read_parquet(output_dir / "create_final_text_units.parquet")

            # Set up LLM (uses same model as Muscle or cheaper proxy)
            llm = ChatOpenAI(
                api_key=os.getenv("OPENAI_API_KEY", ""),
                model=self.llm_model,
                api_type=OpenaiApiType.OpenAI,
                max_retries=3,
            )

            # Build local search (entity neighbourhood)
            local_context = LocalSearchMixedContext(
                entities=entities_df,
                entity_text_embeddings=None,
                relationships=relationships_df,
                text_units=text_units_df,
                community_reports=community_reports_df,
                embedding_vectorstore_key=EntityVectorStoreKey.TITLE,
            )
            self._search_engine = LocalSearch(
                llm=llm,
                context_builder=local_context,
                token_encoder=None,
                llm_params={"max_tokens": 1024, "temperature": 0.0},
            )

            # Build global search (community-level summaries)
            global_context = GlobalCommunityContext(
                community_reports=community_reports_df,
                communities=communities_df,
                entities=entities_df,
            )
            self._global_search_engine = GlobalSearch(
                llm=llm,
                context_builder=global_context,
                token_encoder=None,
                llm_params={"max_tokens": 1024, "temperature": 0.0},
                context_builder_params={"use_community_summary": True, "community_level": self.community_level},
            )

            self._available = True
            logger.info(f"GraphRAG parquet index loaded from {self.index_dir} ({len(entities_df)} entities)")

        except ImportError:
            logger.warning(
                "graphrag package not installed. Run: pip install graphrag. "
                "LLM-enhanced queries disabled (Neo4j still available)."
            )
        except Exception as exc:
            logger.error(f"GraphRAG parquet init failed: {exc}", exc_info=True)

    async def _neo4j_write(self, cypher: str, params: Optional[Dict[str, Any]] = None):
        """
        Execute a write Cypher statement against the Neo4j memory_store.
        Non-fatal: logs warning and continues if Neo4j is unavailable.
        """
        if not self._neo4j_available or self._neo4j_driver is None:
            return
        try:
            async with self._neo4j_driver.session() as session:
                await session.run(cypher, parameters=params or {})
        except Exception as exc:
            logger.warning(f"Neo4j write failed (non-fatal): {exc}")

    async def neo4j_query(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[Dict]:
        """
        Run a read Cypher query against Neo4j and return raw rows.
        Useful for direct graph traversals not covered by the graphrag search API.
        Example:
            rows = await client.neo4j_query(
                "MATCH (d:Document)-[:CAUSED]->(f:Entity {type: 'failure'}) "
                "WHERE d.doc_id CONTAINS $kw RETURN d, f",
                {"kw": "auth"}
            )
        """
        if not self._neo4j_available or self._neo4j_driver is None:
            return []
        try:
            async with self._neo4j_driver.session() as session:
                result = await session.run(cypher, parameters=params or {})
                return [dict(record) async for record in result]
        except Exception as exc:
            logger.warning(f"Neo4j query failed: {exc}")
            return []

    async def find_failure_patterns(
        self,
        prompt: str,
        tier: int,
    ) -> Optional[str]:
        """
        Query for past failures related to this request.
        Checks Neo4j first (real-time, always current), falls back to graphrag
        parquet index (richer LLM summaries but requires index rebuild to pick
        up recent data).

        Returns a warning string if risky patterns found, None if clean.
        Used by the Vault graph's query_graph_memory node.
        """
        # --- Layer 1: Neo4j real-time query ---
        if self._neo4j_available:
            rows = await self.neo4j_query(
                """
                MATCH (d:Document)-[:CAUSED|TRIGGERED|RELATED_TO]->(f:Entity)
                WHERE f.type IN ['failure', 'rollback', 'rejected']
                  AND (toLower(d.text) CONTAINS toLower($kw)
                       OR toLower(f.description) CONTAINS toLower($kw))
                RETURN d.doc_id AS id, d.source_type AS src,
                       f.name AS failure_name, f.description AS desc
                LIMIT 5
                """,
                {"kw": prompt[:200]},
            )
            if rows:
                summary = "; ".join(
                    f"{r.get('src','unknown')} → {r.get('failure_name','?')}"
                    for r in rows[:3]
                )
                logger.info(f"[graph_memory] Neo4j found {len(rows)} failure pattern(s)")
                return f"Prior failures in graph: {summary}"

        # --- Layer 2: graphrag parquet search ---
        if not self._available or self._search_engine is None:
            return None

        query = (
            f"Are there any recorded failures, rollbacks, or rejected changes "
            f"related to: {prompt}? "
            f"Focus on Tier {tier} operations and their outcomes."
        )

        try:
            result = await self._search_engine.asearch(query)
            answer = result.response.strip()

            # Treat as a warning if the LLM found relevant failure history
            has_failure = any(
                kw in answer.lower()
                for kw in ["failure", "rollback", "rejected", "error", "broke", "caused"]
            )
            if has_failure:
                # Return a concise summary (first 200 chars)
                return answer[:200]
            return None

        except Exception as exc:
            logger.warning(f"GraphRAG local search failed: {exc}")
            return None

    async def check_baseline_eligibility(
        self,
        prompt: str,
        tier: int,
    ) -> bool:
        """
        Query GraphRAG: has this type of change been safely baselined before?
        Supplements Shadow's timestamp-based check with relationship context.
        A change is considered eligible if GraphRAG finds ≥1 successful
        similar operation with no associated failure nodes.
        """
        if not self._available or self._search_engine is None:
            return False

        query = (
            f"Has a change like the following been successfully deployed before "
            f"with no associated failures or rollbacks? "
            f"Change description: {prompt}"
        )

        try:
            result = await self._search_engine.asearch(query)
            answer = result.response.lower()
            success_indicators = ["successfully", "deployed", "approved", "merged", "completed"]
            failure_indicators = ["failure", "rollback", "rejected", "error"]

            success_count = sum(1 for kw in success_indicators if kw in answer)
            failure_count = sum(1 for kw in failure_indicators if kw in answer)

            eligible = success_count > failure_count and success_count >= 2
            logger.info(
                f"[graph_baseline] tier={tier} "
                f"success_signals={success_count} failure_signals={failure_count} "
                f"eligible={eligible}"
            )
            return eligible

        except Exception as exc:
            logger.warning(f"GraphRAG baseline check failed: {exc}")
            return False

    async def global_summary(self, query: str) -> str:
        """
        Run a global GraphRAG query over community summaries.
        Good for questions like: "What are the most frequent failure causes?"
        Uses community-level aggregation which handles very large codebases.
        """
        if not self._available or self._global_search_engine is None:
            return "GraphRAG index not available."

        try:
            result = await self._global_search_engine.asearch(query)
            return result.response
        except Exception as exc:
            logger.warning(f"GraphRAG global search failed: {exc}")
            return f"Search failed: {exc}"

    async def index_document(self, text: str, doc_id: str, source_type: str):
        """
        Persist a new document to both storage layers:

        1. Neo4j (immediate): writes a :Document node with metadata so the
           Vault can query it in real-time on the very next T3/T4 request.

        2. graphrag input dir (staged): writes a .txt file picked up by the
           next scheduled `graphrag index --root graphrag_index` rebuild,
           which extracts entities and enriches community summaries.

        source_type: "pr_description", "failure_retrospective", "code_change_summary"
        """
        # Layer 1: write to Neo4j immediately
        await self._neo4j_write(
            """
            MERGE (d:Document {doc_id: $doc_id})
            SET d.text       = $text,
                d.source_type = $source_type,
                d.indexed_at  = timestamp()
            """,
            {"doc_id": doc_id, "text": text[:4000], "source_type": source_type},
        )
        if self._neo4j_available:
            logger.info(f"Neo4j: upserted Document {doc_id} ({source_type})")

        # Layer 2: stage for next graphrag index rebuild
        input_dir = self.index_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        safe_id = doc_id.replace("/", "_").replace(":", "_")
        filepath = input_dir / f"{source_type}_{safe_id}.txt"
        filepath.write_text(text, encoding="utf-8")
        logger.info(f"GraphRAG: staged {filepath.name} for next index rebuild")

    async def record_relationship(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Write a directed relationship between two documents or entities in Neo4j.
        Used to record causal chains, e.g.:
            record_relationship("pr-42", "failure-17", "CAUSED")
            record_relationship("pr-51", "failure-17", "FIXED")
        """
        meta = metadata or {}
        await self._neo4j_write(
            f"""
            MATCH (a {{doc_id: $src}}), (b {{doc_id: $tgt}})
            MERGE (a)-[r:{relation.upper()}]->(b)
            SET r += $meta
            """,
            {"src": source_id, "tgt": target_id, "meta": meta},
        )

    async def rebuild_index(self):
        """
        Trigger a full GraphRAG index rebuild.
        This is I/O and LLM-call intensive — run nightly or on major memory writes.
        """
        import subprocess
        import sys

        logger.info("Starting GraphRAG index rebuild...")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "graphrag", "index", "--root", str(self.index_dir)],
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if result.returncode == 0:
                logger.info("GraphRAG index rebuild complete")
                await self.initialize()  # Reload search engines
            else:
                logger.error(f"GraphRAG index rebuild failed:\n{result.stderr}")
        except subprocess.TimeoutExpired:
            logger.error("GraphRAG index rebuild timed out after 1h")
        except Exception as exc:
            logger.error(f"GraphRAG index rebuild error: {exc}")
