"""
Watchdog Service - Health monitor and failure retrospective writer
Runs on Pi
"""
import logging
import asyncio
from typing import Optional, Dict, List, Tuple
from datetime import datetime
import os

from internal.memory.ledger.store import LedgerStore
from internal.memory.vector.client import VectorClient
from internal.memory.graph.client import GraphRAGClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class WatchdogService:
    """Watchdog: monitors health, writes retrospectives, triggers rollbacks"""
    
    DB_HOST = os.getenv("WATCHDOG_DB_HOST", "localhost")
    DB_PORT = int(os.getenv("WATCHDOG_DB_PORT", "5432"))
    DB_NAME = os.getenv("WATCHDOG_DB_NAME", "agent_memory")
    DB_USER = os.getenv("WATCHDOG_DB_USER", "watchdog")
    DB_PASSWORD = os.getenv("WATCHDOG_DB_PASSWORD", "watchdog_secure_pass")
    
    GRPC_PORT = int(os.getenv("WATCHDOG_GRPC_PORT", "50054"))
    GRPC_HOST = os.getenv("WATCHDOG_GRPC_HOST", "0.0.0.0")
    
    # Alert thresholds
    ERROR_RATE_THRESHOLD = 0.10  # 10%
    LATENCY_SPIKE_THRESHOLD_MS = 5000
    GPU_THERMAL_CRITICAL = 85  # Celsius
    GPU_MEMORY_CRITICAL_MB = 512

    def __init__(self):
        self.ledger: Optional[LedgerStore] = None
        self.vector_client: Optional[VectorClient] = None
        self.graph_client: Optional[GraphRAGClient] = None
        self.running = False
        self.metrics_buffer: List[Dict] = []
    
    async def initialize(self):
        """Initialize Watchdog services"""
        logger.info("Initializing Watchdog Service...")
        
        # Connect to ledger for logging
        self.ledger = LedgerStore(
            db_host=self.DB_HOST,
            db_port=self.DB_PORT,
            db_name=self.DB_NAME,
            db_user=self.DB_USER,
            db_password=self.DB_PASSWORD,
        )
        await self.ledger.connect()
        
        # Connect to vector memory for retrospective storage
        self.vector_client = VectorClient(
            db_host=self.DB_HOST,
            db_port=self.DB_PORT,
            db_name=self.DB_NAME,
            db_user=self.DB_USER,
            db_password=self.DB_PASSWORD,
        )
        await self.vector_client.connect()

        # Neo4j graph memory — failures become traversable graph entities
        self.graph_client = GraphRAGClient()
        await self.graph_client.initialize()

        self.running = True
        logger.info("Watchdog Service initialized successfully")
    
    async def shutdown(self):
        """Graceful shutdown"""
        logger.info("Shutting down Watchdog Service...")
        self.running = False
        if self.ledger:
            await self.ledger.disconnect()
        if self.vector_client:
            await self.vector_client.disconnect()
        logger.info("Watchdog Service shut down")

    async def _index_retrospective_in_graph(self, request_id: str, text: str, metadata: dict):
        """Stage retrospective in GraphRAG input dir for next index rebuild"""
        if self.graph_client is None:
            return
        try:
            await self.graph_client.index_document(
                text=text,
                doc_id=request_id,
                source_type="failure_retrospective",
            )
        except Exception as e:
            logger.warning(f"GraphRAG index_document failed (non-fatal): {e}")
    
    async def monitor_metrics(
        self,
        request_id: str,
        error_rate: float,
        latency_p99_ms: int,
        gpu_temp_c: float,
        gpu_memory_available_mb: float,
    ) -> Tuple[bool, str]:
        """
        Monitor request metrics and determine if rollback needed
        
        Returns: (should_rollback, reason)
        """
        
        # Check error rate
        if error_rate > self.ERROR_RATE_THRESHOLD:
            reason = f"Error rate {error_rate:.2%} > {self.ERROR_RATE_THRESHOLD:.2%}"
            await self._trigger_rollback(request_id, reason)
            return True, reason
        
        # Check latency spike
        if latency_p99_ms > self.LATENCY_SPIKE_THRESHOLD_MS:
            reason = f"P99 latency {latency_p99_ms}ms > {self.LATENCY_SPIKE_THRESHOLD_MS}ms"
            await self._trigger_rollback(request_id, reason)
            return True, reason
        
        # Check GPU thermal
        if gpu_temp_c > self.GPU_THERMAL_CRITICAL:
            reason = f"GPU temperature {gpu_temp_c}°C critical (>{self.GPU_THERMAL_CRITICAL}°C)"
            await self._trigger_rollback(request_id, reason)
            return True, reason
        
        # Check GPU memory
        if gpu_memory_available_mb < self.GPU_MEMORY_CRITICAL_MB:
            reason = f"GPU memory {gpu_memory_available_mb}MB < {self.GPU_MEMORY_CRITICAL_MB}MB critical"
            # Throttle instead of rollback
            await self._alert("throttle", request_id, reason)
            return False, reason
        
        return False, "All metrics healthy"
    
    async def _trigger_rollback(self, request_id: str, reason: str):
        """Trigger rollback action"""
        logger.warning(f"ROLLBACK TRIGGERED for {request_id}: {reason}")
        
        try:
            # Write rollback event to ledger
            await self.ledger.write_entry(
                action_type="rollback",
                actor_id="watchdog",
                request_id=request_id,
                details=f"Automatic rollback triggered: {reason}",
                metadata={"trigger": reason},
            )
            
            # Write retrospective
            await self.write_retrospective(
                request_id=request_id,
                failure_reason=reason,
                recovery_action="rollback",
            )
        
        except Exception as e:
            logger.error(f"Failed to trigger rollback: {e}")
    
    async def _alert(self, alert_type: str, request_id: str, message: str):
        """Send alert to Vault and user"""
        logger.warning(f"ALERT [{alert_type}] {request_id}: {message}")
        
        try:
            await self.ledger.write_entry(
                action_type="execute",
                actor_id="watchdog",
                request_id=request_id,
                details=f"Alert: {message}",
                metadata={"alert_type": alert_type},
            )
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
    
    async def write_retrospective(
        self,
        request_id: str,
        failure_reason: str,
        recovery_action: str,
        root_cause: Optional[str] = None,
    ):
        """Write post-mortem analysis and store as semantic memory"""
        
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        
        try:
            # Format retrospective text
            retrospective_text = f"""
FAILURE RETROSPECTIVE: {request_id}

Root Cause: {root_cause or 'See details below'}
Failure Reason: {failure_reason}
Recovery Action: {recovery_action}
Timestamp: {now_ms}

This incident has been logged for learning and pattern analysis.
            """.strip()
            
            # Store as vector memory for semantic analysis of failure patterns
            vector_id = await self.vector_client.write_vector(
                text=retrospective_text,
                embedding=self._text_to_embedding(retrospective_text),
                source_type="failure_retrospective",
                metadata={
                    "request_id": request_id,
                    "failure_reason": failure_reason,
                    "recovery_action": recovery_action,
                    "root_cause": root_cause or "unknown",
                    "recorded_at": str(now_ms),
                },
            )
            
            # Stage in GraphRAG so failures are queryable as graph entities
            await self._index_retrospective_in_graph(
                request_id=request_id,
                text=retrospective_text,
                metadata={
                    "failure_reason": failure_reason,
                    "recovery_action": recovery_action,
                    "root_cause": root_cause or "unknown",
                },
            )

            # Log in ledger
            await self.ledger.write_entry(
                action_type="retrospective",
                actor_id="watchdog",
                request_id=request_id,
                details=f"Retrospective written for {request_id}: {failure_reason}",
                metadata={
                    "vector_id": vector_id,
                    "root_cause": root_cause or "unknown",
                },
            )

            logger.info(f"Retrospective written for {request_id}")
        
        except Exception as e:
            logger.error(f"Failed to write retrospective: {e}")
    
    async def check_system_health(self) -> Dict[str, any]:
        """Get overall system health status"""
        
        try:
            approvals_count = await self.ledger.get_approval_count()
            rejections_24h = await self.ledger.get_rejection_count_24h()
            ledger_size = await self.ledger.get_ledger_size()
            
            return {
                "status": "healthy",
                "approvals_total": approvals_count,
                "rejections_24h": rejections_24h,
                "ledger_entries": ledger_size,
                "timestamp_ms": int(datetime.utcnow().timestamp() * 1000),
            }
        
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return {
                "status": "error",
                "error": str(e),
            }

    @staticmethod
    def _text_to_embedding(text: str, dim: int = 1024) -> List[float]:
        """
        Produce a deterministic sparse embedding from text using character n-gram hashing.
        Not semantically rich, but differentiates retrospectives by content until an
        embedding model is integrated.
        """
        import hashlib
        vec = [0.0] * dim
        words = text.lower().split()
        for i, word in enumerate(words):
            h = int(hashlib.md5(f"{i}:{word}".encode()).hexdigest(), 16)
            idx = h % dim
            vec[idx] += 1.0
        # L2-normalize
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0.0:
            vec = [v / norm for v in vec]
        return vec


async def main():
    """Run Watchdog service"""
    from cmd.watchdog.grpc_server import start_grpc_server

    watchdog = WatchdogService()

    try:
        await watchdog.initialize()
        server = await start_grpc_server(watchdog, watchdog.GRPC_HOST, watchdog.GRPC_PORT)
        logger.info(f"Watchdog gRPC ready on {watchdog.GRPC_HOST}:{watchdog.GRPC_PORT}")
        await server.wait_for_termination()

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise

    finally:
        await watchdog.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
