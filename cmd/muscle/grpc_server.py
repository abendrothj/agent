"""
gRPC Servicer Implementation for Muscle
Implements the GenerateResponse and Health RPCs.
"""

import asyncio
import time
from loguru import logger
import muscle_pb2
import muscle_pb2_grpc
from hf_model import HFModel
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from activity_monitor import ActivityMonitor


class MuscleServicer(muscle_pb2_grpc.MuscleServicer):
    """Implementation of the Muscle gRPC service."""
    
    def __init__(self, hf_model: HFModel, activity_monitor: Optional['ActivityMonitor'] = None):
        self.hf_model = hf_model
        self.activity_monitor = activity_monitor
        self.request_count = 0
        self.total_tokens_generated = 0
        self.start_time = time.time()
    
    async def GenerateResponse(self, request: muscle_pb2.PromptRequest, context):
        """
        Main RPC: Stream tokens from HuggingFace Transformers in response to PromptRequest.
        
        Activity-aware: queues requests if user is active.
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
        
        # Check user activity - queue if not idle
        if self.activity_monitor and not self.activity_monitor.is_idle():
            queued = self.activity_monitor.queue_request(session_id, {
                "prompt": prompt,
                "system_context": system_context,
                "action_intent": action_intent
            })
            
            if queued:
                logger.info(f"[{session_id}] User active - request queued")
                yield muscle_pb2.TokenResponse(
                    token="",
                    status="queued",
                    error_msg=f"System is in use. Request queued. Queue depth: {len(self.activity_monitor.request_queue)}"
                )
                return
            else:
                logger.warning(f"[{session_id}] Queue full - request rejected")
                yield muscle_pb2.TokenResponse(
                    token="",
                    status="error",
                    error_msg="Request queue is full. Try again later."
                )
                return
        
        start_time = time.time()
        token_index = 0
        
        # Extract InferenceConfig — sent by the Pi with affect-derived values.
        # Fall back to model defaults if absent (e.g. Health checks, old callers).
        cfg = request.config if request.HasField("config") else None
        temperature = cfg.temperature if cfg and cfg.temperature > 0.0 else None
        top_k       = cfg.top_k       if cfg and cfg.top_k       > 0   else None
        top_p       = cfg.top_p       if cfg and cfg.top_p       > 0.0 else None
        max_tokens  = cfg.max_tokens  if cfg and cfg.max_tokens  > 0   else None

        logger.info(
            f"[{session_id}] InferenceConfig: "
            f"temperature={temperature or 'default'} "
            f"top_p={top_p or 'default'} "
            f"top_k={top_k or 'default'} "
            f"max_tokens={max_tokens or 'default'}"
        )
        
        try:
            # Stream tokens from HuggingFace model, using affect-derived sampling params
            async for token in self.hf_model.generate_stream(
                prompt,
                system_context,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                max_tokens=max_tokens,
            ):
                token_index += 1
                self.total_tokens_generated += 1
                
                response = muscle_pb2.TokenResponse(
                    token=token,
                    token_index=token_index,
                    is_complete=False,
                    metadata=muscle_pb2.ResponseMetadata(
                        confidence=0.9,  # Placeholder
                        tokens_used=token_index,
                        model_name=self.hf_model.model_id,
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
                    model_name=self.hf_model.model_id,
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
        
        try:
            await self.hf_model.health_check()
            status_info = self.hf_model.get_status()
            
            return muscle_pb2.HealthResponse(
                healthy=True,
                status="ready",
                uptime_seconds=int(uptime),
                model_loaded=self.hf_model.model_id
            )
        except Exception as e:
            logger.warning(f"Health check: Model not healthy. {e}")
            return muscle_pb2.HealthResponse(
                healthy=False,
                status="degraded",
                uptime_seconds=int(uptime),
                model_loaded=""
            )
    
    async def GetActivityStatus(self, request: muscle_pb2.ActivityStatusRequest, context):
        """
        Get system activity status (queue depth, idle status, GPU utilization).
        Called by Pi to decide whether to send requests to Muscle.
        """
        
        if not self.activity_monitor:
            # Activity monitoring disabled
            return muscle_pb2.ActivityStatusResponse(
                idle_status="unknown",
                queue_depth=0,
                queue_capacity=0,
                accepting_requests=True,
                gpu_utilization_percent=0,
                idle_duration_seconds=0,
            )
        
        status_dict = self.activity_monitor.get_status()
        
        return muscle_pb2.ActivityStatusResponse(
            idle_status=status_dict.get("idle_status", "unknown"),
            queue_depth=status_dict.get("queue_depth", 0),
            queue_capacity=status_dict.get("queue_capacity", 0),
            accepting_requests=status_dict.get("accepting_requests", True),
            gpu_utilization_percent=status_dict.get("gpu_utilization_percent", 0),
            idle_duration_seconds=int(status_dict.get("idle_duration_seconds", 0)),
            user_active=status_dict.get("user_active", False),
        )
