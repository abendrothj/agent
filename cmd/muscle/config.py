"""
Configuration loader for Muscle service.
Reads from .env file and environment variables.
"""

from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
import os


@dataclass
class Config:
    """Muscle service configuration."""
    
    # Hugging Face Model Settings
    hf_model_id: str
    hf_device: str
    hf_dtype: str       # "float16" or "float32"
    hf_quantize: str    # "none", "4bit", or "8bit"
    hf_max_tokens: int
    hf_temperature: float
    hf_top_k: int
    hf_top_p: float
    
    # gRPC Settings
    grpc_host: str
    grpc_port: int
    
    # mTLS Certificates
    cert_file: str
    key_file: str
    ca_cert: str
    
    # Logging
    log_level: str
    log_file: str
    
    # Activity Monitoring (Win11 only)
    activity_monitoring_enabled: bool
    gpu_threshold_percent: float
    idle_threshold_sec: float
    activity_check_interval_sec: float
    
    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from .env file and environment."""
        
        # Load .env file if it exists
        env_file = Path(__file__).parent.parent.parent / ".env"
        if env_file.exists():
            load_dotenv(dotenv_path=env_file)
        
        return cls(
            # Hugging Face settings
            hf_model_id=os.getenv("HF_MODEL_ID", "Qwen/Qwen2.5-Coder-7B-Instruct"),
            hf_device=os.getenv("HF_DEVICE", "cuda"),
            hf_dtype=os.getenv("HF_DTYPE", "float16"),
            hf_quantize=os.getenv("HF_QUANTIZE", "none").lower().strip(),
            hf_max_tokens=int(os.getenv("HF_MAX_TOKENS", "1024")),
            hf_temperature=float(os.getenv("HF_TEMPERATURE", "0.7")),
            hf_top_k=int(os.getenv("HF_TOP_K", "50")),
            hf_top_p=float(os.getenv("HF_TOP_P", "0.9")),
            
            grpc_host=os.getenv("GRPC_HOST", "0.0.0.0"),
            grpc_port=int(os.getenv("GRPC_PORT", "50051")),
            
            cert_file=os.getenv("CERT_FILE", "./certs/muscle.crt"),
            key_file=os.getenv("KEY_FILE", "./certs/muscle.key"),
            ca_cert=os.getenv("CA_CERT", "./certs/client.crt"),
            
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_file=os.getenv("LOG_FILE", "./logs/muscle.log"),
            
            activity_monitoring_enabled=os.getenv("ACTIVITY_MONITORING_ENABLED", "true").lower() == "true",
            gpu_threshold_percent=float(os.getenv("GPU_THRESHOLD_PERCENT", "30.0")),
            idle_threshold_sec=float(os.getenv("IDLE_THRESHOLD_SEC", "300")),
            activity_check_interval_sec=float(os.getenv("ACTIVITY_CHECK_INTERVAL_SEC", "5")),
        )
    
    def __str__(self):
        """String representation (no secrets exposed)."""
        return (
            f"Config("
            f"hf_model_id={self.hf_model_id}, "
            f"grpc_port={self.grpc_port}, "
            f"log_level={self.log_level}, "
            f"activity_monitoring={self.activity_monitoring_enabled}"
            f")"
        )
