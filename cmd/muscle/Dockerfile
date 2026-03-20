# Muscle — Win11 RTX 3060Ti HuggingFace inference service
# Runs on Windows 11, connects back to Pi Vault via mTLS gRPC
FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y \
    python3.11 python3.11-distutils python3.11-dev \
    libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Install pip for Python 3.11
RUN curl https://bootstrap.pypa.io/get-pip.py | python3.11

# Make python3 and pip3 point to 3.11
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && python3.11 -m pip install --upgrade pip

WORKDIR /app

# Install Python dependencies first (cache layer)
COPY cmd/muscle/requirements.txt ./requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy service source
COPY cmd/muscle/ .

# HuggingFace model cache volume mount point
VOLUME ["/root/.cache/huggingface"]

EXPOSE 50051

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import socket; s=socket.create_connection(('127.0.0.1', 50051), timeout=5); s.close()" \
    || exit 1

CMD ["python3", "main.py"]
