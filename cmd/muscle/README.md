# Teammate Muscle Service (Win11)

## Overview

This is the Win11 stateless inference service. It:
- runs Hugging Face Transformers locally (GPU-first)
- serves a Python gRPC server for PromptRequest from Pi Vault
- streams tokens back via TokenResponse
- stores no decision state or credentials
- communicates only with Pi over mTLS

## Architecture

Pi (Vault) -> gRPC over mTLS -> Win11 (Muscle)

Muscle components:
- HFModel (transformers + torch inference)
- gRPC server (port 50051)
- Activity monitor (GPU + input based queueing)

## Quick Start

1. Complete system setup in ../../WIN11_SETUP.md
2. Create venv and install requirements
3. Configure ../../.env with HF_* and gRPC values
4. Run service with run.bat (Windows) or python main.py

Windows example:

```cmd
cd cmd\muscle
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python download_model.py --model-id NousResearch/Hermes-2.5-Mistral-7B
python main.py
```

## Required .env Settings

```env
HF_MODEL_ID=NousResearch/Hermes-2.5-Mistral-7B
HF_DEVICE=cuda
HF_DTYPE=float16
HF_MAX_TOKENS=1024
HF_TEMPERATURE=0.7
HF_TOP_K=50
HF_TOP_P=0.9

GRPC_PORT=50051
GRPC_HOST=0.0.0.0

CERT_FILE=./certs/muscle.crt
KEY_FILE=./certs/muscle.key
CA_CERT=./certs/client.crt

LOG_LEVEL=INFO
LOG_FILE=./logs/muscle.log

ACTIVITY_MONITORING_ENABLED=true
GPU_THRESHOLD_PERCENT=30
IDLE_THRESHOLD_SEC=300
ACTIVITY_CHECK_INTERVAL_SEC=5
```

## gRPC RPCs

- GenerateResponse: streams tokens for a prompt
- Health: reports liveness and model status
- GetActivityStatus: reports idle state, queue depth, and GPU usage

## Activity Detection

When the machine is active, requests are queued (not dropped). When idle for the configured threshold, queued requests are processed in FIFO order.

States:
- active
- transitioning
- idle

## Security Properties

- mTLS required for every request
- no GitHub tokens or secrets on Win11
- no persistent decision state on Muscle
- firewall should allow 50051 only from Pi

## Troubleshooting

- GPU unavailable:
  - run nvidia-smi
  - verify torch CUDA build
- Slow inference:
  - check VRAM pressure in nvidia-smi
  - try lower HF_MAX_TOKENS or quantized loading
- gRPC timeout:
  - verify firewall on 50051
  - verify cert paths and CA_CERT value

## File Map

- main.py: startup and lifecycle
- config.py: env config
- hf_model.py: local inference wrapper
- grpc_server.py: RPC implementation
- activity_monitor.py: active-use detection and queueing
- download_model.py: prefetch and cache model weights
- muscle_pb2.py: generated protobuf messages
- muscle_pb2_grpc.py: generated gRPC bindings
- run.bat: Windows launcher
- run.sh: Linux/macOS launcher
