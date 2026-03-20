"""
gRPC Servicer Implementation for Muscle
Implements the GenerateResponse and Health RPCs.
"""

import asyncio
import time
from loguru import logger
import muscle_pb2
import muscle_pb2_grpc
from ollama_wrapper import OllamaClient


class MuscleServicer(muscle_pb2_grpc.MuscleServicer):
    """Implementation of the Muscle gRPC service."""
    
    def __init__(self, ollama_client: OllamaClient):
        self.ollama = ollama_client
        self.request_count = 0
        self.total_tokens_generated = 0
        self.start_time = time.time()
    
    async def GenerateResponse(self, request: muscle_pb2.PromptRequest, context):
        """
        Main RPC: Stream tokens from Ollama in response to PromptRequest.
        
        Security properties:
        - No state persistence (each call is independent)
        - No filesystem access (only inference)
        - No network access except back to Pi
        - Input validation on prompt size
        """
        
        session_id = request.session_id
        prompt = request.prompt
        system_context = request.system_context
        action_intent = request.action_intent
        
        # Input validation
        if len(prompt) > 50000:
            logger.warning(f"Prompt too large: {len(prompt)} chars, rejecting")
            yield muscle_pb2.TokenResponse(
                token="",
                status="error",
                error_msg="Prompt exceeds maximum size (50KB)"
            )
            return
        
        self.request_count += 1
        logger.info(
            f"[{session_id}] GenerateResponse started. "
            f"Intent: {action_intent}, Prompt len: {len(prompt)}"
        )
        
        start_time = time.time()
        token_index = 0
        
        try:
            # Stream tokens from Ollama
            async for token in self.ollama.generate_stream(prompt, system_context):
                token_index += 1
                self.total_tokens_generated += 1
                
                response = muscle_pb2.TokenResponse(
                    token=token,
                    token_index=token_index,
                    is_complete=False,
                    metadata=muscle_pb2.ResponseMetadata(
                        confidence=0.9,  # Placeholder
                        tokens_used=token_index,
                        model_name=self.ollama.model,
                    ),
                    status="ok"
                )
                
                yield response
                
                # Allow other tasks to run
                await asyncio.sleep(0)
            
            # Send completion marker
            inference_time = time.time() - start_time
            yield muscle_pb2.TokenResponse(
                token="",
                token_index=token_index,
                is_complete=True,
                metadata=muscle_pb2.ResponseMetadata(
                    confidence=0.95,
                    tokens_used=token_index,
                    inference_time_ms=inference_time * 1000,
                    model_name=self.ollama.model,
                ),
                status="ok"
            )
            
            logger.info(
                f"[{session_id}] GenerateResponse completed. "
                f"Tokens: {token_index}, Time: {inference_time:.2f}s, "
                f"Speed: {token_index / max(inference_time, 0.1):.1f} tok/s"
            )
        
        except Exception as e:
            logger.error(f"[{session_id}] GenerateResponse error: {e}", exc_info=True)
            yield muscle_pb2.TokenResponse(
                token="",
                status="error",
                error_msg=str(e)
            )
    
    async def Health(self, request: muscle_pb2.HealthRequest, context):
        """
        Health check RPC (called by Watchdog from Pi).
        Returns liveness status but NO inference details.
        """
        
        uptime = time.time() - self.start_time
        status = await self.ollama.health_status()
        
        if status.get("healthy"):
            return muscle_pb2.HealthResponse(
                healthy=True,
                status="ready",
                uptime_seconds=int(uptime),
                model_loaded=self.ollama.model
            )
        else:
            logger.warning(f"Health check: Ollama not healthy. {status}")
            return muscle_pb2.HealthResponse(
                healthy=False,
                status="degraded",
                uptime_seconds=int(uptime),
                model_loaded=""
            )
