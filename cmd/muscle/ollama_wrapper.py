# ⚠️  DEPRECATED — Ollama support removed in v9.3
#
# The Muscle service switched from Ollama to HuggingFace Transformers
# in the release that introduced cmd/muscle/hf_model.py.
# All code paths that previously referenced OllamaClient now go through
# HFModel.  Delete this file after confirming no external imports remain.

raise ImportError(
    "OllamaClient is deprecated and removed. "
    "Use HFModel from cmd/muscle/hf_model.py instead."
)

# ---- original code preserved below for audit trail only ----

"""
Ollama Client Wrapper (ARCHIVED)
"""

import httpx
import json
import asyncio
from typing import AsyncGenerator
from loguru import logger


class OllamaClient:
    """Async wrapper for Ollama inference."""
    
    def __init__(self, host: str, model: str, max_tokens: int, temperature: float):
        self.host = host
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.client = httpx.AsyncClient(timeout=60.0)
    
    async def health_check(self) -> bool:
        """Check if Ollama is running and model is available."""
        try:
            resp = await self.client.get(f"{self.host}/api/tags")
            if resp.status_code != 200:
                raise Exception(f"Ollama returned {resp.status_code}")
            
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            
            if self.model not in models:
                logger.warning(
                    f"Model {self.model} not found. Available: {models}. "
                    f"Will attempt to pull on first use."
                )
            
            return True
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            raise
    
    async def generate_stream(
        self,
        prompt: str,
        system_context: str = None
    ) -> AsyncGenerator[str, None]:
        """
        Stream tokens from Ollama.
        
        Yields individual tokens as they are generated.
        """
        
        # Build request body
        request_body = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "temperature": self.temperature,
            "num_predict": self.max_tokens,
            "top_k": 40,
            "top_p": 0.9,
        }
        
        if system_context:
            request_body["system"] = system_context
        
        logger.info(
            f"Starting inference: model={self.model}, "
            f"prompt_len={len(prompt)}, temp={self.temperature}"
        )
        
        try:
            async with self.client.stream(
                "POST",
                f"{self.host}/api/generate",
                json=request_body
            ) as response:
                
                if response.status_code != 200:
                    error_text = await response.aread()
                    logger.error(f"Ollama error {response.status_code}: {error_text}")
                    raise Exception(f"Ollama returned {response.status_code}")
                
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    
                    try:
                        data = json.loads(line)
                        token = data.get("response", "")
                        if token:
                            yield token
                        
                        # Check if generation is complete
                        if data.get("done", False):
                            logger.info("Inference completed")
                            break
                    
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse JSON line: {line}")
                        continue
        
        except asyncio.TimeoutError:
            logger.error("Ollama inference timeout")
            raise
        except Exception as e:
            logger.error(f"Inference error: {e}", exc_info=True)
            raise
        
        finally:
            logger.info("Stream generation finished")
    
    async def health_status(self) -> dict:
        """Get healthiness status for diagnostics."""
        try:
            resp = await self.client.get(f"{self.host}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return {
                "healthy": True,
                "model_loaded": self.model,
                "available_models": len(data.get("models", [])),
            }
        except Exception as e:
            return {
                "healthy": False,
                "error": str(e),
            }
    
    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
