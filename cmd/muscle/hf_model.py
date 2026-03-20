"""
Hugging Face Model Loader for Win11 Muscle Service
Direct inference using transformers + torch (no Ollama)

Provides streaming token generation from open-source LLMs.
"""

import asyncio
import torch
from typing import AsyncGenerator, Optional
from loguru import logger

try:
    from transformers import AutoModelForCausalLM, AutoTokenizer
except ImportError:
    logger.error("transformers not installed. Run: pip install transformers torch")
    raise


class HFModel:
    """Hugging Face model wrapper for async token streaming."""
    
    def __init__(
        self,
        model_id: str,
        device: str = "cuda",
        dtype: torch.dtype = torch.float16,
        max_memory: Optional[dict] = None,
    ):
        """
        Initialize HF model.
        
        Args:
            model_id: HuggingFace model ID (e.g., "NousResearch/Hermes-2.5-Mistral-7B")
            device: "cuda" or "cpu"
            dtype: torch.float16 (recommended) or torch.float32
            max_memory: GPU memory allocation {0: "24GB"} for multi-GPU, or None for auto
        """
        self.model_id = model_id
        self.device = device
        self.dtype = dtype
        
        self.model = None
        self.tokenizer = None
        self.load_time = None
        
        logger.info(f"HFModel initialized. Model: {model_id}, Device: {device}, DType: {dtype}")
    
    async def load_model(self):
        """Load model and tokenizer (async wrapper around sync load)."""
        logger.info(f"Loading model: {self.model_id}")
        
        loop = asyncio.get_event_loop()
        
        # Run in thread pool to avoid blocking
        self.tokenizer = await loop.run_in_executor(
            None,
            self._load_tokenizer
        )
        
        self.model = await loop.run_in_executor(
            None,
            self._load_model
        )
        
        logger.info(f"✓ Model loaded. Params: {self.model.num_parameters():,}")
    
    def _load_tokenizer(self):
        """Sync tokenizer load."""
        tokenizer = AutoTokenizer.from_pretrained(
            self.model_id,
            trust_remote_code=True
        )
        
        # Add pad token if missing
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        
        return tokenizer
    
    def _load_model(self):
        """Sync model load."""
        model = AutoModelForCausalLM.from_pretrained(
            self.model_id,
            torch_dtype=self.dtype,
            device_map=self.device,
            trust_remote_code=True,
        )
        
        model.eval()
        return model
    
    async def generate_stream(
        self,
        prompt: str,
        system_context: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        top_k: int = 40,
        top_p: float = 0.9,
    ) -> AsyncGenerator[str, None]:
        """
        Stream tokens from HF model.
        
        Yields tokens one by one as they're generated.
        """
        
        if not self.model or not self.tokenizer:
            raise RuntimeError("Model not loaded. Call load_model() first.")
        
        # Build full prompt
        if system_context:
            full_prompt = f"{system_context}\n\n{prompt}"
        else:
            full_prompt = prompt
        
        logger.info(f"Generating: {len(full_prompt)} chars input, max {max_tokens} tokens")
        
        # Tokenize
        inputs = self.tokenizer(
            full_prompt,
            return_tensors="pt",
            truncation=True,
            max_length=2048
        ).to(self.device)
        
        input_ids = inputs["input_ids"]
        batch_size = input_ids.shape[0]
        
        # Generate with streaming
        output_tokens = []
        
        with torch.no_grad():
            for i in range(max_tokens):
                # Forward pass
                logits = self.model(input_ids).logits[:, -1, :]
                
                # Apply temperature
                logits = logits / temperature
                
                # Top-k + Top-p sampling
                sorted_logits, sorted_indices = torch.sort(logits, descending=True)
                cumsum_probs = torch.cumsum(
                    torch.softmax(sorted_logits, dim=-1),
                    dim=-1
                )
                
                # Remove tokens with cumsum > top_p
                sorted_indices_to_remove = cumsum_probs > top_p
                sorted_indices_to_remove[..., :1] = False  # Keep at least top 1
                
                indices_to_remove = sorted_indices[sorted_indices_to_remove]
                logits[:, indices_to_remove] = float('-inf')
                
                # Sample
                probs = torch.softmax(logits, dim=-1)
                next_token = torch.multinomial(probs, num_samples=1)
                
                # Check for EOS
                if next_token.item() == self.tokenizer.eos_token_id:
                    break
                
                # Append to sequence
                input_ids = torch.cat([input_ids, next_token.unsqueeze(0)], dim=-1)
                output_tokens.append(next_token.item())
                
                # Decode and yield
                token_text = self.tokenizer.decode(
                    output_tokens,
                    skip_special_tokens=True,
                    clean_up_tokenization_spaces=False
                )
                
                # Only yield new tokens
                if i == 0:
                    prev_text = ""
                else:
                    prev_text = self.tokenizer.decode(
                        output_tokens[:-1],
                        skip_special_tokens=True,
                        clean_up_tokenization_spaces=False
                    )
                
                new_text = token_text[len(prev_text):]
                if new_text:
                    yield new_text
                
                # Allow async context switching
                if i % 10 == 0:
                    await asyncio.sleep(0)
    
    async def health_check(self) -> bool:
        """Check if model is loaded and GPU available."""
        if not self.model:
            return False
        
        try:
            # Try a tiny forward pass
            dummy_input = self.tokenizer("test", return_tensors="pt").to(self.device)
            with torch.no_grad():
                _ = self.model(**dummy_input)
            return True
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False
    
    def get_status(self) -> dict:
        """Get model status for diagnostics."""
        if not self.model:
            return {"loaded": False}
        
        try:
            gpu_memory = torch.cuda.memory_allocated() / 1e9 if torch.cuda.is_available() else 0
            return {
                "loaded": True,
                "model_id": self.model_id,
                "device": self.device,
                "dtype": str(self.dtype),
                "gpu_memory_gb": f"{gpu_memory:.2f}",
                "parameters": f"{self.model.num_parameters():,}",
            }
        except Exception as e:
            return {"loaded": True, "error": str(e)}
