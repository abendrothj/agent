"""
Graph Memory Client — Neo4j only.

Writes every failure retrospective, PR description, and approval decision
to Neo4j (bolt://memory_store:7687) in real time so Vault can query past
failure patterns before every T3/T4 approval.

Nodes: Document, Entity (file/function/PR/failure/deployment)
Relationships: CAUSED, FIXED, MODIFIED, INTRODUCED, APPROVED, REJECTED
"""
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from internal.memory.vector.client import VectorClient
except ImportError:
    VectorClient = None  # type: ignore

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
    Neo4j graph memory client.

    Real-time writes on every retrospective/PR/decision via Bolt driver.
    Direct Cypher queries for low-latency failure-pattern lookups.
    Deployed as `memory_store` container in docker-compose.
    """

    # Neo4j env — populated by docker-compose from `memory_store` container
    NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "super_secure_password")

    def __init__(
        self,
        index_dir: Optional[str] = None,
        vector_client: Optional["VectorClient"] = None,
    ):
        self._neo4j_driver = None
        self._neo4j_available = False
        self._vector_client = vector_client

    async def initialize(self):
        """Connect to Neo4j."""
        await self._init_neo4j()

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
                await session.run(
                    "CREATE INDEX pr_id_idx IF NOT EXISTS "
                    "FOR (p:PR) ON (p.pr_id)"
                )
                await session.run(
                    "CREATE INDEX pr_outcome_idx IF NOT EXISTS "
                    "FOR (p:PR) ON (p.outcome)"
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
        Useful for direct graph traversals.
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

    @staticmethod
    def _text_to_embedding(text: str, dim: int = 1024) -> List[float]:
        """
        Deterministic sparse embedding via character n-gram hashing.
        Matches the same function in WatchdogService so vectors written there
        are comparable to queries generated here.
        """
        vec = [0.0] * dim
        words = text.lower().split()
        for i, word in enumerate(words):
            h = int(hashlib.md5(f"{i}:{word}".encode()).hexdigest(), 16)
            idx = h % dim
            vec[idx] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0.0:
            vec = [v / norm for v in vec]
        return vec

    async def _semantic_doc_ids(self, prompt: str, top_k: int = 20) -> List[str]:
        """
        Phase-1 of hybrid lookup: find the top-K most similar retrospective
        doc_ids in pgvector, using the same hash embedding as Watchdog writes.

        Returns a list of request_id strings that can be used as doc_id filters
        in subsequent Neo4j Cypher queries.  Falls back to [] if VectorClient
        is unavailable.
        """
        if self._vector_client is None:
            return []
        try:
            embedding = self._text_to_embedding(prompt)
            entries = await self._vector_client.semantic_search(
                query_embedding=embedding,
                limit=top_k,
                source_type_filter="failure_retrospective",
            )
            doc_ids = [
                e.metadata.get("request_id")
                for e in entries
                if e.metadata.get("request_id")
            ]
            logger.debug(f"[graph_semantic] pgvector returned {len(doc_ids)} candidate doc_ids")
            return doc_ids
        except Exception as exc:
            logger.warning(f"[graph_semantic] pgvector lookup failed (non-fatal): {exc}")
            return []

    async def find_failure_patterns(
        self,
        prompt: str,
        tier: int,
    ) -> Optional[str]:
        """
        Query Neo4j for past failures related to this request.

        Two-phase hybrid lookup:
          1. pgvector cosine search → candidate doc_ids (retrospective entries
             written by Watchdog using the same hash embedding)
          2. Neo4j graph traversal filtered to those doc_ids

        Falls back to keyword CONTAINS if VectorClient is not wired in.
        Returns a warning string if risky patterns found, None if clean.
        """
        if not self._neo4j_available:
            return None

        doc_ids = await self._semantic_doc_ids(prompt)

        if doc_ids:
            rows = await self.neo4j_query(
                """
                MATCH (d:Document)-[:CAUSED|TRIGGERED|RELATED_TO]->(f:Entity)
                WHERE d.doc_id IN $doc_ids
                  AND f.type IN ['failure', 'rollback', 'rejected']
                RETURN d.doc_id AS id, d.source_type AS src,
                       f.name AS failure_name, f.description AS desc
                LIMIT 5
                """,
                {"doc_ids": doc_ids},
            )
        else:
            # Fallback: keyword scan when VectorClient is unavailable
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

        return None

    async def check_baseline_eligibility(
        self,
        prompt: str,
        tier: int,
    ) -> bool:
        """
        Query Neo4j: has this type of change been safely baselined before?

        Two-phase hybrid lookup:
          1. pgvector cosine search → candidate doc_ids
          2. Neo4j check that ≥1 of those docs has an APPROVED edge and no
             failure edges

        Falls back to keyword CONTAINS if VectorClient is not wired in.
        Returns True if ≥1 successful similar operation found.
        """
        if not self._neo4j_available:
            return False

        doc_ids = await self._semantic_doc_ids(prompt)

        if doc_ids:
            rows = await self.neo4j_query(
                """
                MATCH (d:Document)-[:APPROVED]->(e:Entity)
                WHERE d.doc_id IN $doc_ids
                  AND NOT (d)-[:CAUSED|TRIGGERED]->(:Entity {type: 'failure'})
                RETURN count(d) AS successes
                """,
                {"doc_ids": doc_ids},
            )
        else:
            rows = await self.neo4j_query(
                """
                MATCH (d:Document)-[:APPROVED]->(e:Entity)
                WHERE NOT (d)-[:CAUSED|TRIGGERED]->(:Entity {type: 'failure'})
                  AND (toLower(d.text) CONTAINS toLower($kw)
                       OR toLower(e.description) CONTAINS toLower($kw))
                RETURN count(d) AS successes
                """,
                {"kw": prompt[:200]},
            )

        successes = rows[0].get("successes", 0) if rows else 0
        eligible = successes >= 1
        logger.info(f"[graph_baseline] tier={tier} successes={successes} eligible={eligible}")
        return eligible

    async def index_document(self, text: str, doc_id: str, source_type: str):
        """
        Persist a document to Neo4j as a :Document node.
        source_type: "pr_description", "failure_retrospective", "code_change_summary"
        """
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

    # ── PR lifecycle tracking ────────────────────────────────────────────────
    # These methods form the agent's contribution memory.  Every PR it
    # submits is written as a :PR node; every outcome (merged/rejected/commented)
    # updates that node.  RepoSelector reads this to improve future targeting.

    async def record_pr_submitted(
        self,
        pr_id: str,                 # unique key, e.g. "owner/repo#42"
        repo_full_name: str,
        pr_number: int,
        pr_url: str,
        title: str,
        language: str,
        branch: str,
        is_self_modification: bool,
        issue_title: str = "",
        topics: Optional[List[str]] = None,
    ):
        """Write a :PR node the moment the pull request is opened."""
        await self._neo4j_write(
            """
            MERGE (p:PR {pr_id: $pr_id})
            SET p.repo           = $repo,
                p.pr_number      = $pr_number,
                p.url            = $url,
                p.title          = $title,
                p.language       = $language,
                p.branch         = $branch,
                p.self_mod       = $self_mod,
                p.issue_title    = $issue_title,
                p.outcome        = 'open',
                p.submitted_at   = timestamp()
            WITH p
            UNWIND $topics AS topic
            MERGE (t:Topic {name: topic})
            MERGE (p)-[:RELATES_TO]->(t)
            """,
            {
                "pr_id":      pr_id,
                "repo":       repo_full_name,
                "pr_number":  pr_number,
                "url":        pr_url,
                "title":      title,
                "language":   language,
                "branch":     branch,
                "self_mod":   is_self_modification,
                "issue_title": issue_title,
                "topics":     topics or [],
            },
        )
        logger.info(f"[graph] PR submitted recorded: {pr_id}")

    async def record_pr_outcome(
        self,
        pr_id: str,
        outcome: str,           # "merged" | "closed" | "commented"
        feedback: str = "",
    ):
        """
        Update the :PR node with the final outcome.
        Called by the AutonomyLoop when it polls outstanding PRs.
        This is the core learning signal: merged = agent got something right.
        """
        await self._neo4j_write(
            """
            MATCH (p:PR {pr_id: $pr_id})
            SET p.outcome      = $outcome,
                p.feedback     = $feedback,
                p.resolved_at  = timestamp()
            """,
            {"pr_id": pr_id, "outcome": outcome, "feedback": feedback},
        )
        logger.info(f"[graph] PR outcome recorded: {pr_id} → {outcome}")

        # If rejected, create a failure entity so find_failure_patterns picks it up
        if outcome == "closed":
            await self._neo4j_write(
                """
                MATCH (p:PR {pr_id: $pr_id})
                MERGE (f:Entity {
                    id:   $pr_id + '_rejection',
                    type: 'rejected',
                    name: 'PR rejected: ' + p.title
                })
                SET f.description = $feedback
                MERGE (p)-[:CAUSED]->(f)
                """,
                {"pr_id": pr_id, "feedback": feedback},
            )

    async def get_open_prs(self) -> List[Dict[str, Any]]:
        """Return all :PR nodes the agent has submitted that are still open."""
        return await self.neo4j_query(
            """
            MATCH (p:PR {outcome: 'open'})
            RETURN p.pr_id     AS pr_id,
                   p.repo      AS repo,
                   p.pr_number AS pr_number,
                   p.self_mod  AS self_mod
            ORDER BY p.submitted_at ASC
            """
        )
