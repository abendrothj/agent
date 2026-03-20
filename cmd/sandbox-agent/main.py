"""
Sandbox Service - Ephemeral execution environment for dry-runs and testing
Runs on Pi, communicates with Muscle (Win11) for compute
"""
import logging
import asyncio
from typing import Optional, Tuple
from datetime import datetime
import os

from internal.memory.ledger.store import LedgerStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SandboxService:
    """Sandbox: executes Muscle in isolated environment for testing and validation"""
    
    DB_HOST = os.getenv("SANDBOX_DB_HOST", "localhost")
    DB_PORT = int(os.getenv("SANDBOX_DB_PORT", "5432"))
    DB_NAME = os.getenv("SANDBOX_DB_NAME", "agent_memory")
    DB_USER = os.getenv("SANDBOX_DB_USER", "sandbox")
    DB_PASSWORD = os.getenv("SANDBOX_DB_PASSWORD", "sandbox_secure_pass")
    
    MUSCLE_HOST = os.getenv("MUSCLE_HOST", "192.168.1.100")
    MUSCLE_PORT = int(os.getenv("MUSCLE_PORT", "50051"))
    
    GRPC_PORT = int(os.getenv("SANDBOX_GRPC_PORT", "50055"))
    GRPC_HOST = os.getenv("SANDBOX_GRPC_HOST", "0.0.0.0")
    
    def __init__(self):
        self.ledger: Optional[LedgerStore] = None
        self.muscle_client = None  # Will be gRPC client to Muscle
    
    async def initialize(self):
        """Initialize Sandbox services"""
        logger.info("Initializing Sandbox Service...")
        
        # Connect to ledger for logging
        self.ledger = LedgerStore(
            db_host=self.DB_HOST,
            db_port=self.DB_PORT,
            db_name=self.DB_NAME,
            db_user=self.DB_USER,
            db_password=self.DB_PASSWORD,
        )
        await self.ledger.connect()
        
        logger.info(f"Sandbox connecting to Muscle at {self.MUSCLE_HOST}:{self.MUSCLE_PORT}")
        # In production: initialize gRPC client to Muscle with mTLS
        
        logger.info("Sandbox Service initialized successfully")
    
    async def shutdown(self):
        """Graceful shutdown"""
        logger.info("Shutting down Sandbox Service...")
        if self.ledger:
            await self.ledger.disconnect()
        logger.info("Sandbox Service shut down")
    
    async def run_dry_op(
        self,
        request_id: str,
        prompt: str,
        system_context: str,
        max_tokens: int = 1024,
    ) -> Tuple[str, dict]:
        """
        Execute prompt in sandbox (unmetered, no approval needed)
        
        Returns: (output, metrics)
        """
        
        start_ms = int(datetime.utcnow().timestamp() * 1000)
        
        try:
            # Call Muscle service via gRPC (placeholder)
            output = f"[DRY-RUN] Response to: {prompt[:50]}..."
            
            end_ms = int(datetime.utcnow().timestamp() * 1000)
            duration_ms = end_ms - start_ms
            
            metrics = {
                "duration_ms": duration_ms,
                "tokens_generated": int(len(output.split()) * 1.3),  # Estimate
                "gpu_memory_mb": 2048,  # Placeholder
                "success": True,
            }
            
            # Log execution
            await self.ledger.write_entry(
                action_type="execute",
                actor_id="sandbox",
                request_id=request_id,
                details=f"Dry-run executed: {duration_ms}ms",
                metadata=metrics,
            )
            
            logger.info(f"Dry-run completed for {request_id}")
            return output, metrics
        
        except Exception as e:
            logger.error(f"Dry-run failed: {e}")
            
            # Log failure
            await self.ledger.write_entry(
                action_type="rollback",
                actor_id="sandbox",
                request_id=request_id,
                details=f"Dry-run failed: {str(e)}",
                metadata={"error": str(e)},
            )
            
            raise


async def main():
    """Run Sandbox service"""
    sandbox = SandboxService()
    
    try:
        await sandbox.initialize()
        
        logger.info(f"Sandbox listening on {sandbox.GRPC_HOST}:{sandbox.GRPC_PORT}")
        
        # Keep running
        await asyncio.Event().wait()
    
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    
    finally:
        await sandbox.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
