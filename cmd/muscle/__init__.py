"""
Teammate Muscle Service
Python module structure for Win11 gRPC server
"""

# Main files (implement Muscle service)
# ├── main.py              - Entry point, gRPC server startup
# ├── config.py            - Configuration from .env
# ├── ollama_wrapper.py    - Ollama async client wrapper
# ├── grpc_server.py       - MuscleServicer implementation
# │
# └── Generated files (from protoc)
#     ├── muscle_pb2.py          - Protocol buffer messages
#     └── muscle_pb2_grpc.py     - gRPC servicer interface
