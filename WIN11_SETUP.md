# Win11 Muscle Service: Setup Instructions

## Overview
This guide sets up your Win11 3060Ti machine as the **untrusted Muscle** service. It will:
1. Run Ollama locally (inference engine)
2. Host a Python gRPC server that streams tokens
3. Accept connections **only** from your Pi (mTLS tunnel)
4. Write nothing to disk except inference cache

---

## Prerequisites

- Windows 11 (Build 22000+)
- NVIDIA 3060Ti GPU
- ~50GB free SSD space (for Ollama models)
- Administrator access
- ~30 min of setup time

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

## Step 2: Install Ollama

### 2.1 Download & Install
1. Go to: https://ollama.ai (or https://ollama.com)
2. Download Windows installer
3. Run installer, install to default location: `C:\Users\<username>\AppData\Local\Programs\Ollama`

### 2.2 Verify Installation
```cmd
ollama --version
```

### 2.3 Configure GPU
Ollama auto-detects CUDA. Verify:
```cmd
ollama pull deepseek-r1:8b
```

This will download the Deepseek R1 8B model (~8GB).

**First run may take 10-15 min.** Monitor:
- Ollama window shows download progress
- GPU usage: Watch with `nvidia-smi -l 1` in separate terminal

### 2.4 Test Inference
```cmd
ollama run deepseek-r1:8b "What is 2+2?"
```

Should respond immediately with GPU accelerated inference.

---

## Step 3: Set Up Python Environment

### 3.1 Install Python 3.11+
1. Download from: https://www.python.org/downloads/
2. **Important:** Check "Add Python to PATH" during install
3. Verify:
   ```cmd
   python --version
   ```

### 3.2 Create Project Directory
```cmd
cd C:\Users\<username>
mkdir teammate-muscle
cd teammate-muscle
```

### 3.3 Create Virtual Environment
```cmd
python -m venv venv
venv\Scripts\activate
```

You should see `(venv)` in your prompt.

### 3.4 Install Dependencies
```cmd
pip install --upgrade pip
pip install grpcio grpcio-tools ollama pydantic cryptography loguru python-dotenv
```

Verify:
```cmd
python -c "import grpc; print(grpc.__version__)"
```

---

## Step 4: Generate mTLS Certificates (Muscle ↔ Pi Tunnel)

### 4.1 Generate Self-Signed Certs

Pi admin does this once, then shares with Win11. For now, **placeholder** using `openssl` (if not available, install from https://slproweb.com/products/Win32OpenSSL.html):

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

### 4.2 Share Certificates
- **Pi gets:** `client.crt` (Muscle's public cert)
- **Win11 keeps:** `muscle.key`, `muscle.crt`, `client.crt`

For now: create dummy files in `C:\teammate-muscle\certs\` (to be replaced later).

---

## Step 5: Firewall Configuration

### 5.1 Allow gRPC Port
1. Open Windows Defender Firewall → Advanced Settings
2. Inbound Rules → New Rule
3. Port: **50051** (gRPC default)
4. Protocol: TCP
5. Action: Allow
6. **IMPORTANT:** Limit to Pi IP only (if known)
   - If Pi IP is `192.168.1.50`:
     - Add condition: "Remote IP Address" = `192.168.1.50`

### 5.2 Disable Outbound (Optional but Recommended)
1. Create Rule: Outbound, Deny, all ports EXCEPT:
   - **443** (HTTPS, for Ollama model downloads)
   - **50051** (to Pi Vault)
   - **53** (DNS)

This prevents Muscle from exfiltrating data.

---

## Step 6: AppContainer Sandbox (Optional but Recommended)

**Why:** Restricts Muscle process to minimal permissions (filesystem read-only, network to Pi only).

### 6.1 Create AppContainer
1. Download WAIL (Windows Application Isolation Layer) from: https://github.com/trailofbits/wail (or similar AppContainer tool)
2. Or: Run Muscle in Docker (see Step 7 alternative)

### 6.2 Run in Sandbox
```cmd
# Using WAIL or similar
run-isolated cmd /c "cd C:\teammate-muscle && python muscle.py"
```

**For now:** Skip this. Add later if needed.

---

## Step 7: Alternative - Docker on Win11

**If you prefer containerization:**

### 7.1 Install Docker Desktop
1. Download from: https://www.docker.com/products/docker-desktop
2. Install, enable WSL 2

### 7.2 Build Container
See `Dockerfile` in Phase 8 (Deployment). For now, use native Python.

---

## Step 8: Environment Configuration

Create `C:\teammate-muscle\.env`:

```env
# Ollama Configuration
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=deepseek-r1:8b
OLLAMA_MAX_TOKENS=1024
OLLAMA_TEMPERATURE=0.7

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
```

---

## Step 9: Network Connectivity Test

### 9.1 Find Your Win11 IP
```cmd
ipconfig
```
Look for "IPv4 Address" on your LAN (usually `192.168.x.x`).

### 9.2 Test from Pi (Later)
From Raspberry Pi:
```bash
ping <win11-ip>
```

Should respond. If not, check firewall rules.

---

## Step 10: Verify Setup Complete

### 10.1 Checklist
- [ ] NVIDIA GPU recognized (`nvidia-smi` shows 3060Ti)
- [ ] Ollama installed and test model downloaded
- [ ] Python 3.11+ venv created with dependencies
- [ ] Certificates (dummy or real) in `./certs/`
- [ ] Firewall rule allows port 50051
- [ ] `.env` file configured
- [ ] Win11 IP address noted (for Pi config)

### 10.2 Quick Test
```cmd
cd C:\teammate-muscle
venv\Scripts\activate
ollama serve & # Start Ollama in background
```

Then in new terminal:
```cmd
ollama run deepseek-r1:8b "Test" --stream
```

Should stream tokens live.

---

## Logs & Monitoring

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
| Ollama won't start | Check Windows Defender disabled for Ollama app; restart Ollama service |
| Model download fails | Check internet; Ollama may need firewall rule for HTTPS (443) |
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

- ✅ **Ollama is localhost-only** by default (not exposed to network)
- ✅ **mTLS certificates** secure Muscle ↔ Pi tunnel
- ✅ **Firewall** restricts inbound to port 50051 + Pi IP
- ✅ **No persistent state** — Muscle restarts cleanly each day
- ✅ **Logs don't contain secrets** (no prompts, no outputs, just metadata)

**Do NOT:**
- ❌ Open port 50051 to the internet
- ❌ Store credentials on Win11
- ❌ Run Ollama.exe in unsafe container
- ❌ Trust any connection from non-Pi sources

