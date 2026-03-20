"""
Teammate Muscle Service: Entry Point
Win11 Stateless Inference gRPC Server

This service:
1. Accepts PromptRequest from Pi (Vault)
2. Streams tokens from Ollama inference
3. Returns TokenResponse (no state persistence)
4. Communicates ONLY with Pi via mTLS
"""

import asyncio
import sys
import signal
from pathlib import Path
from loguru import logger

# Add cmd/muscle to path for local imports
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from ollama_wrapper import OllamaClient
from grpc_server import MuscleServicer
import grpc
from concurrent import futures
import muscle_pb2_grpc


def setup_logging(config: Config):
    """Configure logging with rotation and filtering."""
    log_file = Path(config.log_file)
    log_file.parent.mkdir(exist_ok=True)
    
    logger.remove()  # Remove default handler
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="<level>{level: <8}</level> | {name}:{function}:{line} - {message}"
    )
    logger.add(
        str(log_file),
        level=config.log_level,
        rotation="100 MB",
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}"
    )
    logger.info(f"Logging initialized. Level: {config.log_level}, File: {log_file}")


async def start_grpc_server(config: Config, ollama_client: OllamaClient):
    """Start the gRPC server with mTLS."""
    
    # Load certificates
    with open(config.key_file, "rb") as f:
        private_key = f.read()
    with open(config.cert_file, "rb") as f:
        certificate_chain = f.read()
    with open(config.ca_cert, "rb") as f:
        ca_cert = f.read()
    
    # Create server credentials
    server_credentials = grpc.ssl_server_credentials(
        [
            (
                private_key,
                certificate_chain
            )
        ],
        root_certificates=ca_cert,
        require_client_auth=True  # Mandatory mTLS
    )
    
    # Create server
    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=10)
    )
    
    # Register servicer
    servicer = MuscleServicer(ollama_client)
    muscle_pb2_grpc.add_MuscleServicer_to_server(servicer, server)
    
    # Add secure port
    port = f"{config.grpc_host}:{config.grpc_port}"
    server.add_secure_port(port, server_credentials)
    
    logger.info(f"Starting gRPC server on {port} with mTLS")
    await server.start()
    
    return server


async def main():
    """Main async entry point."""
    try:
        # Load configuration
        config = Config.from_env()
        logger.info(f"Configuration loaded: {config}")
        
        setup_logging(config)
        
        # Initialize Ollama client
        logger.info(f"Connecting to Ollama at {config.ollama_host}...")
        ollama_client = OllamaClient(
            host=config.ollama_host,
            model=config.ollama_model,
            max_tokens=config.ollama_max_tokens,
            temperature=config.ollama_temperature
        )
        
        # Verify Ollama is reachable
        try:
            await ollama_client.health_check()
            logger.info(f"✓ Ollama healthy. Model: {config.ollama_model}")
        except Exception as e:
            logger.error(f"✗ Ollama health check failed: {e}")
            logger.error("Is Ollama running? (ollama serve)")
            sys.exit(1)
        
        # Start gRPC server
        server = await start_grpc_server(config, ollama_client)
        logger.info("✓ Muscle service ready to accept requests from Pi")
        
        # Handle graceful shutdown
        def signal_handler(sig, frame):
            logger.info(f"Received signal {sig}, shutting down...")
            asyncio.create_task(server.stop(grace=5))
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Keep running
        await server.wait_for_termination()
        
    except FileNotFoundError as e:
        logger.error(f"Configuration or certificate file not found: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Teammate Muscle Service Starting")
    logger.info("=" * 60)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
