# Teammate Muscle Service (Win11)

## Overview

This is the **Win11 stateless inference service**. It:
- Runs Ollama (local LLM inference)
- Serves a Python gRPC server accepting PromptRequests from Pi
- Streams tokens back via TokenResponse (no state persistence)
- Communicates ONLY with Pi (via mTLS)
- Refuses filesystem writes (except /tmp inference cache)

## Architecture

```
Pi (Vault)
    ↓
[gRPC over mTLS]
    ↓
Win11 (Muscle) ← ← ← THIS SERVICE
    ├─ Ollama (inference)
    ├─ gRPC Server (port 50051)
    └─ Token streamer
```

## Quick Start

### 1. System Setup (One-time)

Follow [WIN11_SETUP.md](../../WIN11_SETUP.md) to:
- Install NVIDIA CUDA
- Install Ollama
- Install Python 3.11+
- Create certificates
- Configure firewall

### 2. Install Dependencies

```cmd
cd cmd\muscle
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Environment

Edit `..\..\\.env`:

```env
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=openhermes:7b
GRPC_PORT=50051
GRPC_HOST=0.0.0.0
CERT_FILE=./certs/muscle.crt
KEY_FILE=./certs/muscle.key
CA_CERT=./certs/client.crt
LOG_LEVEL=INFO
LOG_FILE=./logs/muscle.log
```

### 4. Run the Service

```cmd
cd cmd\muscle
run.bat
```

Or manually:

```cmd
python main.py
```

Expected output:

```
2026-03-19 14:30:45 | INFO | Teammate Muscle Service Starting
2026-03-19 14:30:45 | INFO | Configuration loaded: Config(...)
2026-03-19 14:30:46 | INFO | Connection to Ollama at http://localhost:11434...
2026-03-19 14:30:47 | INFO | ✓ Ollama healthy. Model: openhermes:7b
2026-03-19 14:30:48 | INFO | Starting gRPC server on 0.0.0.0:50051 with mTLS
2026-03-19 14:30:48 | INFO | ✓ Muscle service ready to accept requests from Pi
```

## API (gRPC Only)

### RPC: `GenerateResponse`

**Request:** `PromptRequest`
- `session_id` — Trace ID from Pi
- `prompt` — The query to generate response for
- `system_context` — System role (optional)
- `history` — Previous turns (optional, ignored by Muscle)
- `config` — Inference parameters (temperature, max_tokens, etc.)
- `action_intent` — "reasoning", "code_gen", "analysis" (advisory only)
- `constraints` — Governance hints (advisory only)

**Response Stream:** `TokenResponse[]`

Each response contains:
- `token` — Single token or partial text chunk
- `token_index` — Position in response
- `is_complete` — True when generation finished
- `metadata` — Inference time, model name, confidence
- `status` — "ok" or "error"

**Constraints:**
- Max prompt size: 50KB (enforced by Muscle)
- Max response tokens: 1024 (configurable)
- Timeout: 60s per RPC
- All requests are stateless (new connection = blank slate)

### RPC: `Health`

**Request:** `HealthRequest`
- `session_id` — Trace ID

**Response:** `HealthResponse`
- `healthy` — Boolean health status
- `status` — "ready", "loading", "degraded", "error"
- `uptime_seconds` — How long Muscle has been running
- `model_loaded` — Which model is in memory

**Called by:** Watchdog (Pi) every 30s to confirm Muscle is alive.

### RPC: `GetActivityStatus`

**Request:** `ActivityStatusRequest`
- `session_id` — Trace ID

**Response:** `ActivityStatusResponse`

Returns real-time activity status:
- `idle_status` — "active", "transitioning", or "idle"
- `queue_depth` — Number of pending requests
- `queue_capacity` — Max requests allowed in queue (100)
- `accepting_requests` — Boolean (true if idle and will process)
- `gpu_utilization_percent` — Current GPU usage (%)
- `idle_duration_seconds` — Seconds idle since last activity
- `user_active` — Boolean (keyboard/mouse activity detected)

**Called by:** Pi Vault to check if Win11 Muscle is available before sending requests.

---

## Activity Detection (Request Queuing)

The Muscle service automatically detects user activity and queues requests when you're actively using your PC.

### How It Works

**When you're active (gaming, working):**
1. Incoming `GenerateResponse()` requests are **queued** (not rejected)
2. Response: status="queued" with queue depth
3. Request waits for idle period

**When you become idle:**
1. Machine must be idle for 5+ minutes (configurable)
2. Queued requests are processed in order (FIFO)
3. GPU freed up for agent work

**Detection Methods:**
- **GPU Utilization:** Polls NVIDIA GPU every 5s. If >30% in use → active
- **Input Activity:** Checks Windows `GetLastInputInfo`. If keyboard/mouse used → active

### Configuration

In `.env`:

```env
ACTIVITY_MONITORING_ENABLED=true          # Enable/disable feature
GPU_THRESHOLD_PERCENT=30                  # GPU >30% = mark as active
IDLE_THRESHOLD_SEC=300                    # 5 min idle before processing
ACTIVITY_CHECK_INTERVAL_SEC=5             # Check every 5 sec
```

### Status in Logs

```
DEBUG | 🎮 Status: active | GPU: 42.1% | Idle: 0s | Queue: 0
DEBUG | 🎮 Status: active | GPU: 38.5% | Idle: 5s | Queue: 1
DEBUG | ⏳ Status: transitioning | GPU: 5.2% | Idle: 10s | Queue: 2
DEBUG | 💤 Status: idle | GPU: 0.5% | Idle: 305s | Queue: 2
INFO | Processing queued request req_001 (waited 330.2s)
```

### Disable If Not Needed

If running Muscle on a separate non-gaming machine:

```env
ACTIVITY_MONITORING_ENABLED=false
```

---

## Security Properties

| Property | How Enforced |
|----------|-------------|
| No filesystem writes | Only `/tmp` allowed; other paths rejected by OS permissions |
| No credentials on Win11 | GitHub PAT, API keys stored only on Pi |
| Stateless inference | Each request fresh; no session state |
| Network isolation | Firewall: port 50051 → Pi IP only |
| mTLS authentication | Client cert required (Pi provides client.crt) |
| Input validation | Prompts 50KB max, requests 10s timeout |
| No external network | Blocked to internet (except model downloads from Ollama) |

## Logging

Logs are written to `./logs/muscle.log` (rotating daily, 100MB per file, 30 day retention).

Also streams to console (configurable via LOG_LEVEL).

**What's logged:**
- Service startup/shutdown
- Request metadata (session, prompt length, intent)
- Inference metrics (tokens generated, speed)
- Errors and warnings

**What's NOT logged:**
- Prompts (privacy)
- Responses (privacy)
- Credentials (security)

## Troubleshooting

### Symptom: "ollama: command not found"

**Fix:** Ollama not in PATH. Restart terminal after Ollama install.

### Symptom: "CUDA not detected" or "GPU usage 0%"

**Fix:** 
1. Verify with `nvidia-smi`
2. Reinstall CUDA drivers from NVIDIA website
3. Restart machine

### Symptom: "gRPC server bind failed: port 50051 in use"

**Fix:**
```cmd
netstat -ano | findstr :50051
taskkill /PID <pid> /F
```

Then restart.

### Symptom: "mTLS handshake failed"

**Fix:**
- Verify certificates exist in `./certs/`
- Verify Pi certificate matches `CA_CERT` path in .env
- Check certificate expiry: `openssl x509 -in certs/muscle.crt -noout -text`

### Symptom: "Ollama inference timeout"

**Fix:**
- Model loading on first run is *slow* (~30s). Patience.
- Check GPU with `nvidia-smi -l 1` during inference
- If GPU memory full: reduce model size or restart Ollama

## Performance Tuning

### Want faster inference?

1. Use smaller model: `OLLAMA_MODEL=mistral:7b` (faster, less accurate)
2. Lower context: `OLLAMA_MAX_TOKENS=512` (shorter responses)
3. Higher temperature: `OLLAMA_TEMPERATURE=0.5` (faster, less creative)

### Want higher quality?

1. Use larger model: `OLLAMA_MODEL=llama2:13b` (slower but more accurate)
2. Increase context: `OLLAMA_MAX_TOKENS=2048` (longer responses)
3. Lower temperature: `OLLAMA_TEMPERATURE=0.7` (more consistent)

## Development

### Regenerate gRPC code (if muscle.proto changes)

```cmd
protoc --python_out=. --grpc_python_out=. ../api/muscle.proto
```

(Note: You'll need `protoc` installed. See WIN11_SETUP.md.)

### Run with debug logging

```env
LOG_LEVEL=DEBUG
```

Then check `logs/muscle.log` for verbose output.

## Next Steps

Once Muscle is working:
1. Share `certs/muscle.crt` with Pi (for mTLS)
2. Configure Pi firewall for Win11 outbound connections
3. Build Pi Vault service (Phase 3)
4. Test end-to-end: Mac → Pi → Win11 → Ollama

## Files

```
cmd/muscle/
├── main.py              - Entry point
├── config.py            - Config loader
├── grpc_server.py       - MuscleServicer implementation
├── ollama_wrapper.py    - Ollama async client
├── muscle_pb2.py        - Generated: protobuf messages
├── muscle_pb2_grpc.py   - Generated: gRPC servicer
├── requirements.txt     - Python dependencies
├── run.bat              - Windows launch script
├── run.sh               - Linux/macOS launch script
└── README.md            - This file
```

## License & Notes

This is part of the Teammate v9.3 personal agent system. See [DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md) for full architecture.

**Security:** Treat Win11 Muscle as untrusted compute. All decisions and execution logic lives on Pi.
