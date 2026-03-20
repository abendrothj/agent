"""
Sandbox Service - Ephemeral execution environment for dry-runs and testing
Runs on Pi, communicates with Muscle (Win11) for compute
"""
import logging
import asyncio
from typing import Optional, Tuple
from datetime import datetime
import os
import sys
from pathlib import Path

from internal.affect import engine as affect_engine
from internal.affect.store import AffectStore
from internal.memory.ledger.store import LedgerStore

# Proto stubs
_PROTO_DIR = Path(__file__).resolve().parent.parent.parent / "internal" / "api"
if str(_PROTO_DIR) not in sys.path:
    sys.path.insert(0, str(_PROTO_DIR))

import muscle_pb2        # noqa: E402
import muscle_pb2_grpc  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Static base temperature — affect state modulates around this
_BASE_TEMPERATURE = float(os.getenv("BASE_TEMPERATURE", "0.7"))
_BASE_TOP_P       = float(os.getenv("BASE_TOP_P",       "0.9"))


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
    
    def __init__(self, affect_store: Optional[AffectStore] = None):
        self.ledger: Optional[LedgerStore] = None
        self.muscle_client = None  # gRPC stub to Muscle (set during initialize)
        self._affect = affect_store
    
    async def initialize(self):
        """Initialize Sandbox services"""
        logger.info("Initializing Sandbox Service...")
        
        self.ledger = LedgerStore(
            db_host=self.DB_HOST,
            db_port=self.DB_PORT,
            db_name=self.DB_NAME,
            db_user=self.DB_USER,
            db_password=self.DB_PASSWORD,
        )
        await self.ledger.connect()
        
        logger.info(f"Sandbox connecting to Muscle at {self.MUSCLE_HOST}:{self.MUSCLE_PORT}")
        # mTLS channel to Muscle — stubs init here in production
        # self.muscle_client = muscle_pb2_grpc.MuscleStub(channel)
        
        logger.info("Sandbox Service initialized successfully")
    
    async def shutdown(self):
        """Graceful shutdown"""
        logger.info("Shutting down Sandbox Service...")
        if self.ledger:
            await self.ledger.disconnect()
        logger.info("Sandbox Service shut down")

    # ── Affect-aware InferenceConfig builder ──────────────────────────────────

    async def _build_inference_config(
        self,
        base_temperature: float = _BASE_TEMPERATURE,
        base_top_p: float = _BASE_TOP_P,
        max_tokens: int = 1024,
    ) -> muscle_pb2.InferenceConfig:
        """
        Build the InferenceConfig for a Muscle call, with temperature and
        top_p derived from the agent's current affective state.

        If the affect store is unavailable, falls back to static base values
        so calls always succeed.
        """
        state = await self._affect.read_state() if self._affect else None

        if state:
            temperature = affect_engine.compute_temperature(state, base_temperature)
            top_p       = affect_engine.compute_top_p(state, base_top_p)
            params      = affect_engine.summarise_inference_params(state, base_temperature)
            logger.info(f"[sandbox] inference params: {params['reasoning']}")
        else:
            temperature = base_temperature
            top_p       = base_top_p

        return muscle_pb2.InferenceConfig(
            temperature=temperature,
            top_p=top_p,
            top_k=40,
            max_tokens=max_tokens,
            stream=True,
        )

    # ── Muscle call ───────────────────────────────────────────────────────────

    async def run_dry_op(
        self,
        request_id: str,
        prompt: str,
        system_context: str,
        max_tokens: int = 1024,
    ) -> Tuple[str, dict]:
        """
        Execute prompt in sandbox with affect-derived inference parameters.
        Returns: (output, metrics)
        """
        start_ms = int(datetime.utcnow().timestamp() * 1000)
        
        try:
            config = await self._build_inference_config(max_tokens=max_tokens)

            if self.muscle_client:
                # Real Muscle call — stream tokens and collect
                muscle_request = muscle_pb2.PromptRequest(
                    session_id=request_id,
                    prompt=prompt,
                    system_context=system_context,
                    config=config,
                    action_intent="code_gen",
                )
                output_tokens = []
                async for token_resp in self.muscle_client.GenerateResponse(muscle_request):
                    if token_resp.status == "error":
                        raise RuntimeError(token_resp.error_msg)
                    if token_resp.token:
                        output_tokens.append(token_resp.token)
                output = "".join(output_tokens)
            else:
                # Stub path (muscle client not yet connected)
                output = f"[DRY-RUN] Response to: {prompt[:50]}..."

            end_ms = int(datetime.utcnow().timestamp() * 1000)
            duration_ms = end_ms - start_ms
            
            metrics = {
                "duration_ms": duration_ms,
                "tokens_generated": int(len(output.split()) * 1.3),
                "gpu_memory_mb": 2048,
                "success": True,
                "temperature_used": config.temperature,
                "top_p_used": config.top_p,
            }
            
            await self.ledger.write_entry(
                action_type="execute",
                actor_id="sandbox",
                request_id=request_id,
                details=f"Dry-run executed: {duration_ms}ms, temp={config.temperature}",
                metadata=metrics,
            )
            
            logger.info(
                f"Dry-run completed for {request_id} — "
                f"temp={config.temperature} top_p={config.top_p}"
            )
            return output, metrics
        
        except Exception as e:
            logger.error(f"Dry-run failed: {e}")
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
