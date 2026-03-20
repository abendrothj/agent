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
    
    # Ollama Settings
    ollama_host: str
    ollama_model: str
    ollama_max_tokens: int
    ollama_temperature: float
    
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
    
    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from .env file and environment."""
        
        # Load .env file if it exists
        env_file = Path(__file__).parent.parent.parent / ".env"
        if env_file.exists():
            load_dotenv(dotenv_path=env_file)
        
        return cls(
            ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "deepseek-r1:8b"),
            ollama_max_tokens=int(os.getenv("OLLAMA_MAX_TOKENS", "1024")),
            ollama_temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0.7")),
            
            grpc_host=os.getenv("GRPC_HOST", "0.0.0.0"),
            grpc_port=int(os.getenv("GRPC_PORT", "50051")),
            
            cert_file=os.getenv("CERT_FILE", "./certs/muscle.crt"),
            key_file=os.getenv("KEY_FILE", "./certs/muscle.key"),
            ca_cert=os.getenv("CA_CERT", "./certs/client.crt"),
            
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_file=os.getenv("LOG_FILE", "./logs/muscle.log"),
        )
    
    def __str__(self):
        """String representation (no secrets exposed)."""
        return (
            f"Config("
            f"ollama_model={self.ollama_model}, "
            f"grpc_port={self.grpc_port}, "
            f"log_level={self.log_level}"
            f")"
        )
