# Win11 Muscle Service: Setup Instructions

## Overview
This guide sets up your Win11 3060Ti machine as the **untrusted Muscle** service. It will:
1. Run Python with PyTorch & Hugging Face Transformers (direct inference)
2. Host a Python gRPC server that streams tokens
3. Accept connections **only** from your Pi (mTLS tunnel)
4. Write nothing to disk except model cache

---

## Prerequisites

- Windows 11 (Build 22000+)
- NVIDIA 3060Ti GPU (8GB VRAM)
- ~60GB free SSD space (for models: ~5GB base, up to 15GB with quantized variants)
- Administrator access
- ~45 min of setup time

---

## Step 1: Install NVIDIA CUDA & cuDNN

### 1.1 NVIDIA CUDA Toolkit
1. Download from: https://developer.nvidia.com/cuda-downloads?target_os=Windows&target_arch=x86_64&target_version=11
2. Choose CUDA 11.8 or 12.x (supports RTX 30 series)
3. Run installer, accept defaults
4. Verify installation:
   ```
   nvidia-smi
   ```
   Should show your 3060Ti listed with CUDA Compute Capability 8.6

### 1.2 Verify GPU Recognition
```cmd
nvidia-smi -L
```
Should output:
```
GPU 0: NVIDIA GeForce RTX 3060 Ti
```

**Troubleshooting:**
- If GPU not detected: update NVIDIA driver via GeForce Experience
- If CUDA not found: restart after installer completes

---

## Step 2: Install Python 3.11+ & PyTorch

### 2.1 Install Python 3.11+
1. Download from: https://www.python.org/downloads/
2. **Important:** Check "Add Python to PATH" during install
3. Verify:
   ```cmd
   python --version
   ```

### 2.2 Create Project Directory
```cmd
cd C:\Users\<username>
mkdir teammate-muscle
cd teammate-muscle
```

### 2.3 Create Virtual Environment
```cmd
python -m venv venv
venv\Scripts\activate
```

You should see `(venv)` in your prompt.

### 2.4 Install PyTorch (CUDA-Enabled)

This is the critical step — PyTorch with CUDA support for your RTX 3060Ti:

```cmd
pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
```

Verify GPU is recognized:
```cmd
python -c "import torch; print(f'GPU Available: {torch.cuda.is_available()}'); print(f'GPU: {torch.cuda.get_device_name(0)}')"
```

Should output:
```
GPU Available: True
GPU: NVIDIA GeForce RTX 3060 Ti
```

**Troubleshooting:**
- If `GPU Available: False`: Check NVIDIA driver and CUDA install
- Installation size: ~4GB base, may take 10-15 min on first download

### 2.5 Install Transformer Libraries
```cmd
pip install transformers>=4.35.0 accelerate>=0.24.0 bitsandbytes>=0.41.0
```

Verify:
```cmd
python -c "from transformers import AutoTokenizer; print('Transformers OK')"
```

---

## Step 3: Download Model (Pre-Cache)

### 3.1 Download OpenHermes 2.5 to Cache

Before starting the Muscle service, pre-download the model to avoid delays:

**Full Precision (Recommended for accuracy):**
```cmd
python -c "import torch; from transformers import AutoModelForCausalLM, AutoTokenizer; tok = AutoTokenizer.from_pretrained('NousResearch/Hermes-2.5-Mistral-7B'); model = AutoModelForCausalLM.from_pretrained('NousResearch/Hermes-2.5-Mistral-7B', torch_dtype=torch.float16, device_map='auto'); print('Model cached successfully')"
```

**ALTERNATIVE - Quantized (Faster, Less VRAM):**
If you want the model to run in less VRAM or faster:
```cmd
pip install bitsandbytes transformers
python -c "import torch; from transformers import BitsAndBytesConfig, AutoModelForCausalLM; config = BitsAndBytesConfig(load_in_8bit=True); model = AutoModelForCausalLM.from_pretrained('NousResearch/Hermes-2.5-Mistral-7B', quantization_config=config, device_map='auto'); print('Model cached successfully')"
```

**Storage Requirements:**
- Float16 (FP16): **4.1 GB**
- Float32 (FP32): **7.8 GB**  
- 8-bit quantized: **2.1 GB**
- 4-bit quantized (GPTQ): **1.5 GB**

First download takes **5-20 min** depending on connection. Model is cached locally in `~\.cache\huggingface\`.

---

## Step 4: Set Up Python Environment

### 4.1 Install Dependencies (gRPC & Utils)
```cmd
pip install grpcio grpcio-tools pydantic cryptography loguru python-dotenv
```

Verify:
```cmd
python -c "import grpc; print(grpc.__version__)"
```

### 4.2 Download Muscle Code
Clone or copy the Muscle service code to `C:\teammate-muscle\cmd\muscle\`:
- `main.py`
- `config.py`
- `grpc_server.py`
- `hf_model.py`
- `activity_monitor.py`
- `muscle_pb2.py`
- `muscle_pb2_grpc.py`
- `requirements.txt`

Then:
```cmd
pip install -r requirements.txt
```

---

## Step 5: Generate mTLS Certificates (Muscle ↔ Pi Tunnel)

### 5.1 Generate Self-Signed Certificates

```cmd
# In C:\teammate-muscle\certs\ directory

# Generate Muscle private key
openssl genrsa -out muscle.key 2048

# Generate Muscle certificate (self-signed)
openssl req -new -x509 -key muscle.key -out muscle.crt -days 365 -subj "/CN=win11-muscle"

# Generate client-side cert (Pi will use this to connect)
openssl genrsa -out client.key 2048
openssl req -new -x509 -key client.key -out client.crt -days 365 -subj "/CN=pi-vault"
```

### 5.2 Share Certificates
- **Pi gets:** `client.crt` (Muscle's public cert)
- **Win11 keeps:** `muscle.key`, `muscle.crt`, `client.crt`

For now: create dummy files in `C:\teammate-muscle\certs\` (to be replaced later).

---

## Step 6: Firewall Configuration

### 6.1 Allow gRPC Port
1. Open Windows Defender Firewall → Advanced Settings
2. Inbound Rules → New Rule
3. Port: **50051** (gRPC default)
4. Protocol: TCP
5. Action: Allow
6. **IMPORTANT:** Limit to Pi IP only (if known)
   - If Pi IP is `192.168.1.50`:
     - Add condition: "Remote IP Address" = `192.168.1.50`

### 6.2 Disable Outbound (Optional but Recommended)
1. Create Rule: Outbound, Deny, all ports EXCEPT:
   - **443** (HTTPS, for Hugging Face model downloads)
   - **50051** (to Pi Vault)
   - **53** (DNS)

This prevents Muscle from exfiltrating data.

---

## Step 7: AppContainer Sandbox (Optional but Recommended)

**Why:** Restricts Muscle process to minimal permissions (filesystem read-only, network to Pi only).

### 7.1 Create AppContainer
1. Download WAIL (Windows Application Isolation Layer) from: https://github.com/trailofbits/wail (or similar AppContainer tool)
2. Or: Run Muscle in Docker (see Step 8 alternative)

### 7.2 Run in Sandbox
```cmd
# Using WAIL or similar
run-isolated cmd /c "cd C:\teammate-muscle && python muscle.py"
```

**For now:** Skip this. Add later if needed.

---

## Step 8: Alternative - Docker on Win11

**If you prefer containerization:**

### 8.1 Install Docker Desktop
1. Download from: https://www.docker.com/products/docker-desktop
2. Install, enable WSL 2

### 8.2 Build Container
See `Dockerfile` in Phase 8 (Deployment). For now, use native Python.

---

## Step 9: Environment Configuration

Create `C:\teammate-muscle\.env`:

```env
# Hugging Face Model Configuration
HF_MODEL_ID=NousResearch/Hermes-2.5-Mistral-7B
HF_DEVICE=cuda
HF_DTYPE=float16              # Options: float16, float32
HF_MAX_TOKENS=1024
HF_TEMPERATURE=0.7            # Creativity (0.1=deterministic, 1.0=creative)
HF_TOP_K=50                   # Diversity in sampling
HF_TOP_P=0.9                  # Nucleus sampling

# gRPC Server
GRPC_PORT=50051
GRPC_HOST=0.0.0.0

# mTLS Certificates (paths)
CERT_FILE=./certs/muscle.crt
KEY_FILE=./certs/muscle.key
CA_CERT=./certs/client.crt

# Logging
LOG_LEVEL=INFO
LOG_FILE=./logs/muscle.log

# Activity Detection (Win11 only) - OPTIONAL but RECOMMENDED
ACTIVITY_MONITORING_ENABLED=true
GPU_THRESHOLD_PERCENT=30          # If GPU >30% in use, mark as active
IDLE_THRESHOLD_SEC=300            # 5 minutes idle before processing queued requests
ACTIVITY_CHECK_INTERVAL_SEC=5     # Monitor every 5 seconds
```

**Model Configuration Notes:**
- `HF_MODEL_ID`: Options: `NousResearch/Hermes-2.5-Mistral-7B` (7B, recommended), `meta-llama/Llama-2-7b-hf`, etc.
- `HF_DTYPE`: `float16` recommended for 3060Ti (8GB VRAM). Use `float32` for more accuracy but needs 16GB+
- `HF_TEMPERATURE`: Lower = more focused (0.3-0.5 for coding), Higher = more creative (0.7-1.0 for brainstorming)

---

## Step 10: Activity Detection (Optional but Recommended)

The Muscle service can detect when you're actively using your PC (gaming, working) and automatically queue inference requests until you're idle. This prevents the agent from consuming GPU resources while you're busy.

### 10.1 What It Does

**Before Activity Detection:**
- Agent requests arrive at any time
- GPU might be busy with your game → poor gaming performance
- Requests blocked or experience high latency

**With Activity Detection:**
- Incoming requests from Pi are **automatically queued**
- GPU utilization is monitored (nvidia-smi polls)
- Keyboard/mouse activity is tracked (Windows GetLastInputInfo)
- Once idle for 5+ minutes: queued requests are processed
- No lost requests, just delayed until you're done

**States:**
- `🎮 Active` — You're using the machine (requests queued)
- `⏳ Transitioning` — Recently became idle (< 5 min yet)
- `💤 Idle` — Idle for 5+ min (requests accepted & processed)

### 10.2 Configuration

The `.env` settings are already added above. Adjust if needed:

| Setting | Default | Description |
|---------|---------|-------------|
| `ACTIVITY_MONITORING_ENABLED` | `true` | Enable activity detection |
| `GPU_THRESHOLD_PERCENT` | 30 | GPU >30% → mark as active |
| `IDLE_THRESHOLD_SEC` | 300 | 5 min idle before processing queued requests |
| `ACTIVITY_CHECK_INTERVAL_SEC` | 5 | Poll every 5 seconds |

### 10.3 Tuning

**If agent works too much during light usage:**
```env
GPU_THRESHOLD_PERCENT=20  # Lower threshold (more sensitive)
```

**If it interferes with your gaming:**
```env
GPU_THRESHOLD_PERCENT=50  # Higher threshold (less sensitive)
```

**If you want requests processed faster:**
```env
IDLE_THRESHOLD_SEC=120    # 2 minutes instead of 5
```

**If you're running Muscle on a separate machine (not your main PC):**
```env
ACTIVITY_MONITORING_ENABLED=false  # Disable queue stalling
```

### 10.4 Logs

Monitor the queue and activity in real-time:

```
2026-03-20 15:30:45 | DEBUG | 🎮 Status: active | GPU: 42.1% | Idle: 0s | Queue: 0
2026-03-20 15:30:50 | DEBUG | 🎮 Status: active | GPU: 38.5% | Idle: 5s | Queue: 1
2026-03-20 15:30:55 | DEBUG | ⏳ Status: transitioning | GPU: 5.2% | Idle: 10s | Queue: 2
2026-03-20 15:36:00 | DEBUG | 💤 Status: idle | GPU: 0.5% | Idle: 305s | Queue: 2
2026-03-20 15:36:01 | INFO | Processing queued request req_001 (waited 330.2s)
```

---

## Step 11: Network Connectivity Test

### Find Your Win11 IP
```cmd
ipconfig
```
Look for "IPv4 Address" on your LAN (usually `192.168.x.x`).

### Test from Pi (Later)
From Raspberry Pi:
```bash
ping <win11-ip>
```

Should respond. If not, check firewall rules.

---

## Step 12: Verify Setup Complete

### Checklist
- [ ] NVIDIA GPU recognized (`nvidia-smi` shows 3060Ti)
- [ ] PyTorch installed and GPU recognized (`python -c "import torch; print(torch.cuda.is_available())"` returns True)
- [ ] Transformers library installed and working
- [ ] Model pre-downloaded and cached
- [ ] Python venv created with all dependencies from requirements.txt
- [ ] Certificates (dummy or real) in `./certs/`
- [ ] Firewall rule allows port 50051
- [ ] `.env` file configured (with HF model settings and activity detection)
- [ ] Muscle code copied to directory structure
- [ ] Win11 IP address noted (for Pi config)

### Quick Test
```cmd
cd C:\teammate-muscle
venv\Scripts\activate
python main.py
```

Expected output:
```
Configuration loaded: Config(hf_model_id=NousResearch/Hermes-2.5-Mistral-7B, grpc_port=50051, ...)
Loading Hugging Face model: NousResearch/Hermes-2.5-Mistral-7B...
✓ Model loaded successfully from HuggingFace Hub
  Device: cuda, Params: 7,000,000,000, VRAM: 4.1GB
✓ Model health check passed
✓ Activity monitoring enabled
✓ Muscle service ready to accept requests from Pi
```

If you see errors about modules not found, re-run:
```cmd
pip install -r requirements.txt
```

**First startup takes 2-5 min** for model download + cache. Subsequent runs are ~10 seconds.

---

## Step 13: Logs & Monitoring

### Logs Location
```
C:\teammate-muscle\logs\muscle.log
```

### Monitor GPU Usage (During Inference)
Open separate terminal:
```cmd
# Update every 1 second
nvidia-smi -l 1
```

Watch for:
- GPU Util: Should spike to 70-90% during inference
- Memory: Should use 4-6GB VRAM (depends on model)
- Temp: Should stay < 75°C

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `nvidia-smi: command not found` | CUDA toolkit not in PATH; restart after install |
| HF model load fails | Check internet, Hugging Face availability, and disk space in cache directory |
| Model download fails | Check internet; allow HTTPS (443) for Hugging Face downloads |
| gRPC connection timeout | Check firewall port 50051 open; verify Pi IP can resolve Win11 |
| Low GPU utilization (< 20%) | Model may be running on CPU; check CUDA support for your model |
| Inference very slow (> 10s/token) | 3060Ti should do ~10 tokens/sec; check thermal throttling (temps > 80°C) |

---

## Next Steps

Once Win11 is ready:
1. Move to `cmd/muscle/` Python implementation
2. Start Python gRPC server
3. Implement `GenerateResponse()` RPC streaming
4. Test from local machine first
5. Then test mTLS tunnel from Pi

---

## Security Reminders

- ✅ **Muscle service is LAN-local only** (not exposed to internet)
- ✅ **mTLS certificates** secure Muscle ↔ Pi tunnel
- ✅ **Firewall** restricts inbound to port 50051 + Pi IP
- ✅ **No persistent state** — Muscle restarts cleanly each day
- ✅ **Logs don't contain secrets** (no prompts, no outputs, just metadata)

**Do NOT:**
- ❌ Open port 50051 to the internet
- ❌ Store credentials on Win11
- ❌ Expose Hugging Face tokens or credentials on Win11
- ❌ Trust any connection from non-Pi sources

