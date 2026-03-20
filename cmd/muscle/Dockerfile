# Muscle — Win11 RTX 3060Ti HuggingFace inference service
# Runs on Windows 11, connects back to Pi Vault via mTLS gRPC
FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    python3.11 python3.11-pip python3.11-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Make python3 point to 3.11
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/pip3 pip3 /usr/bin/pip3.11 1

WORKDIR /app

# Install Python dependencies first (cache layer)
COPY cmd/muscle/requirements.txt ./requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy generated proto stubs
COPY internal/api/muscle_pb2.py ./muscle_pb2.py
COPY internal/api/muscle_pb2_grpc.py ./muscle_pb2_grpc.py
COPY internal/api/vault_pb2.py ./vault_pb2.py
COPY internal/api/vault_pb2_grpc.py ./vault_pb2_grpc.py

# Copy service source
COPY cmd/muscle/ .

# HuggingFace model cache volume mount point
VOLUME ["/root/.cache/huggingface"]

EXPOSE 50051

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import grpc; ch = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(ch).result(timeout=5)" \
    || exit 1

CMD ["python3", "main.py"]
