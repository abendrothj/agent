"""
Shadow Service - Prediction validator and 24h baseline tracker
Runs on Pi
"""
import logging
import asyncio
from typing import Optional, List, Tuple
from datetime import datetime
import os

from internal.memory.vector.client import VectorClient
from internal.memory.ledger.store import LedgerStore
from internal.core.metrics.evaluator import MetricsEvaluator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ShadowService:
    """Shadow: collects 24h baseline predictions for canary validation"""
    
    DB_HOST = os.getenv("SHADOW_DB_HOST", "localhost")
    DB_PORT = int(os.getenv("SHADOW_DB_PORT", "5432"))
    DB_NAME = os.getenv("SHADOW_DB_NAME", "agent_memory")
    DB_USER = os.getenv("SHADOW_DB_USER", "shadow")
    DB_PASSWORD = os.getenv("SHADOW_DB_PASSWORD", "shadow_secure_pass")
    
    GRPC_PORT = int(os.getenv("SHADOW_GRPC_PORT", "50053"))
    GRPC_HOST = os.getenv("SHADOW_GRPC_HOST", "0.0.0.0")
    
    def __init__(self):
        self.vector_client: Optional[VectorClient] = None
        self.ledger: Optional[LedgerStore] = None
        self.metrics = MetricsEvaluator()
        self.baselines = {}  # In-memory cache of recent baselines
    
    async def initialize(self):
        """Initialize Shadow services"""
        logger.info("Initializing Shadow Service...")
        
        # Connect to vector memory for semantic search
        self.vector_client = VectorClient(
            db_host=self.DB_HOST,
            db_port=self.DB_PORT,
            db_name=self.DB_NAME,
            db_user=self.DB_USER,
            db_password=self.DB_PASSWORD,
        )
        await self.vector_client.connect()
        
        # Connect to ledger for logging
        self.ledger = LedgerStore(
            db_host=self.DB_HOST,
            db_port=self.DB_PORT,
            db_name=self.DB_NAME,
            db_user=self.DB_USER,
            db_password=self.DB_PASSWORD,
        )
        await self.ledger.connect()
        
        logger.info("Shadow Service initialized successfully")
    
    async def shutdown(self):
        """Graceful shutdown"""
        logger.info("Shutting down Shadow Service...")
        if self.vector_client:
            await self.vector_client.disconnect()
        if self.ledger:
            await self.ledger.disconnect()
        logger.info("Shadow Service shut down")
    
    async def record_baseline(
        self,
        request_id: str,
        prompt: str,
        response: str,
        embedding: List[float],
        tier: int,
    ) -> str:
        """Record baseline prediction for 24h observation"""
        
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        
        try:
            # Store as vector memory for semantic search
            vector_id = await self.vector_client.write_vector(
                text=f"BASELINE: {prompt}\n\nRESPONSE: {response}",
                embedding=embedding,
                source_type="baseline_prediction",
                metadata={
                    "request_id": request_id,
                    "tier": str(tier),
                    "recorded_at": str(now_ms),
                },
            )
            
            # Log in ledger
            await self.ledger.write_entry(
                action_type="execute",
                actor_id="shadow",
                request_id=request_id,
                details=f"Recorded Tier {tier} baseline prediction (24h observation)",
                metadata={"vector_id": vector_id},
            )
            
            logger.info(f"Baseline recorded for {request_id} (Tier {tier})")
            return vector_id
        
        except Exception as e:
            logger.error(f"Failed to record baseline: {e}")
            raise
    
    async def check_canary_eligibility(
        self,
        request_id: str,
        prompt: str,
        query_embedding: List[float],
        tier: int,
    ) -> Tuple[bool, str, float]:
        """
        Check if request is eligible for canary promotion
        
        Returns: (eligible, reason, semantic_similarity)
        """
        
        try:
            # Search for baseline with same prompt
            similar = await self.vector_client.semantic_search(
                query_embedding=query_embedding,
                limit=5,
                similarity_threshold=0.70,
                source_type_filter="baseline_prediction",
            )
            
            if not similar:
                return False, "No baseline predictions found for semantic matching", 0.0
            
            # Get best match and compute cosine similarity against query
            best_match = similar[0]
            similarity = self._cosine_similarity(query_embedding, best_match.embedding)
            
            # Check if meets canary eligibility criteria
            min_similarity = 0.90 if tier == 4 else 0.85 if tier == 3 else 0.80
            
            if similarity < min_similarity:
                return False, f"Semantic similarity {similarity:.2%} below {min_similarity:.2%}", similarity
            
            logger.info(f"Request {request_id} eligible for Tier {tier} canary (similarity: {similarity:.2%})")
            return True, "Eligible for canary promotion", similarity
        
        except Exception as e:
            logger.error(f"Canary eligibility check failed: {e}")
            return False, f"Check failed: {str(e)}", 0.0
    
    async def verify_baseline_age(
        self,
        baseline_vector_id: str,
        tier: int,
    ) -> Tuple[bool, str, float]:
        """
        Verify baseline prediction has aged appropriately
        
        Returns: (aged_enough, reason, age_hours)
        """
        
        try:
            baseline = await self.vector_client.get_vector(baseline_vector_id)
            
            if not baseline:
                return False, "Baseline not found", 0.0
            
            # Calculate age
            now_ms = int(datetime.utcnow().timestamp() * 1000)
            age_ms = now_ms - baseline.created_at_ms
            age_hours = age_ms / (3600 * 1000)
            
            # Check minimum age
            min_age_hours = 48 if tier == 4 else 24 if tier == 3 else 1
            
            if age_hours < min_age_hours:
                return False, f"Baseline too young: {age_hours:.1f}h < {min_age_hours}h", age_hours
            
            logger.info(f"Baseline aged {age_hours:.1f}h (minimum {min_age_hours}h)")
            return True, "Baseline aged sufficiently", age_hours
        
        except Exception as e:
            logger.error(f"Baseline age check failed: {e}")
            return False, f"Check failed: {str(e)}", 0.0

    @staticmethod
    def _cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
        """Compute cosine similarity between two embeddings (0.0-1.0)"""
        if not vec1 or not vec2 or len(vec1) != len(vec2):
            return 0.0
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = sum(a * a for a in vec1) ** 0.5
        norm2 = sum(b * b for b in vec2) ** 0.5
        if norm1 == 0.0 or norm2 == 0.0:
            return 0.0
        return dot / (norm1 * norm2)


async def main():
    """Run Shadow service"""
    from cmd.shadow.grpc_server import start_grpc_server

    shadow = ShadowService()

    try:
        await shadow.initialize()
        server = await start_grpc_server(shadow, shadow.GRPC_HOST, shadow.GRPC_PORT)
        logger.info(f"Shadow gRPC ready on {shadow.GRPC_HOST}:{shadow.GRPC_PORT}")
        await server.wait_for_termination()

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise

    finally:
        await shadow.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
