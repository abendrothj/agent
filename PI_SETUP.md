# Raspberry Pi Setup (Vault Side)

This guide documents the Pi-side certificate setup for this repo.

It now also includes the lowest-friction Wake -> Task -> Sleep control flow for Win11 Muscle.

## Paths Used

- Repo path on Pi: `~/agent`
- Certificate directory on Pi: `/opt/teammate-vault/certs`

## Prerequisites

Install required tools:

```bash
sudo apt update
sudo apt install -y openssl unzip
```

## Decrypt the Transferred Certificate Bundle

From the repo on Pi:

```bash
cd ~/agent/secure_transfer
chmod +x decrypt.sh
./decrypt.sh <bundle_password>
```

Decrypted files are written to:

```bash
~/agent/secure_transfer/decrypted-certs
```

## Install Certificates to Vault Path

```bash
sudo mkdir -p /opt/teammate-vault/certs

sudo install -m 600 ~/agent/secure_transfer/decrypted-certs/client.crt /opt/teammate-vault/certs/client.crt
sudo install -m 600 ~/agent/secure_transfer/decrypted-certs/client.key /opt/teammate-vault/certs/client.key
sudo install -m 600 ~/agent/secure_transfer/decrypted-certs/muscle.crt /opt/teammate-vault/certs/muscle.crt
```

## Important

- Do not copy `muscle.key` to the Pi.
- Pi should hold only:
  - `client.crt`
  - `client.key`
  - `muscle.crt`

## Permissions

```bash
sudo chmod 700 /opt/teammate-vault/certs
sudo chmod 600 /opt/teammate-vault/certs/client.key
sudo chmod 600 /opt/teammate-vault/certs/client.crt
sudo chmod 600 /opt/teammate-vault/certs/muscle.crt
```

## Pi Service mTLS Env Mapping

Use these values in the Pi Vault service environment:

```env
CERT_FILE=/opt/teammate-vault/certs/client.crt
KEY_FILE=/opt/teammate-vault/certs/client.key
CA_CERT=/opt/teammate-vault/certs/muscle.crt
```

## Quick Verification

```bash
openssl x509 -in /opt/teammate-vault/certs/client.crt -noout -subject -dates
openssl x509 -in /opt/teammate-vault/certs/muscle.crt -noout -subject -dates
openssl rsa -in /opt/teammate-vault/certs/client.key -check -noout
```

## Wake-Run-Sleep Workflow (Lowest Friction)

Use this model for day-to-day operation:

1. Pi wakes Win11 via Wake-on-LAN.
2. Pi waits until Muscle gRPC Health is ready over mTLS.
3. Pi sends inference tasks.
4. Pi asks Win11 to sleep when done.

This keeps the Win11 box mostly asleep while still fully automated.

## Win11 One-Time Setup (Run on Win11 as Administrator)

From the repo root on Win11:

```powershell
powershell -ExecutionPolicy Bypass -File deployments/win11/power/setup_remote_power.ps1 -PiIp 10.0.0.104
```

What it configures:

- Installs/enables OpenSSH Server.
- Sets sshd to Automatic.
- Adds inbound firewall rules restricted to Pi IP:
  - TCP 22 (SSH)
  - TCP 50051 (Muscle gRPC)
- Registers startup task to run Muscle Docker automatically:
  - Task: Teammate-Muscle-Docker-On-Startup

## Pi One-Time Setup

Install tools:

```bash
sudo apt update
sudo apt install -y wakeonlan openssh-client python3 python3-pip
python3 -m pip install --user grpcio
```

Make scripts executable:

```bash
cd ~/agent
chmod +x deployments/pi/scripts/muscle_on.sh
chmod +x deployments/pi/scripts/muscle_wait_ready.sh
chmod +x deployments/pi/scripts/muscle_off.sh
```

Set environment values in your shell/profile:

```bash
export WIN11_MAC="AA:BB:CC:DD:EE:FF"
export WIN11_IP="10.0.0.50"
export WIN11_HOST="jakea@10.0.0.50"
export TLS_SERVER_NAME="win11-muscle"

export CERT_FILE="/opt/teammate-vault/certs/client.crt"
export KEY_FILE="/opt/teammate-vault/certs/client.key"
export CA_CERT="/opt/teammate-vault/certs/muscle.crt"
```

## Daily Operation

Wake + wait ready:

```bash
cd ~/agent
./deployments/pi/scripts/muscle_on.sh
./deployments/pi/scripts/muscle_wait_ready.sh
```

Sleep after tasks:

```bash
cd ~/agent
./deployments/pi/scripts/muscle_off.sh
```

Shutdown instead of sleep:

```bash
MODE=shutdown ./deployments/pi/scripts/muscle_off.sh
```

## Script Locations

- Win11:
  - deployments/win11/power/setup_remote_power.ps1
  - deployments/win11/power/start_muscle.ps1
  - deployments/win11/power/sleep_muscle.ps1
- Pi:
  - deployments/pi/scripts/muscle_on.sh
  - deployments/pi/scripts/muscle_wait_ready.sh
  - deployments/pi/scripts/muscle_off.sh

## Manual CD for Pi Brain Services

A dedicated manual workflow deploys only Pi brain services in this monorepo.

Workflow file:

- .github/workflows/pi-brain-manual-cd.yml

Deploy script used by the workflow:

- deployments/pi/scripts/deploy_brain_update.sh

Default deployed services:

- vault
- api
- shadow
- watchdog
- sandbox-agent
- cloudflared

Infrastructure services (postgres/redis/neo4j/prometheus/grafana) are intentionally not in the default list.

### Trigger in GitHub Actions

1. Open `Pi Brain Manual CD` in Actions.
2. Click `Run workflow`.
3. Set inputs:
  - `ref`: branch/tag/SHA to deploy
  - `allow_dirty`: keep `false` unless you intentionally deploy from a dirty runner checkout
  - `services`: space-separated service names (defaults are safe)

### Required Runner Labels

Use a self-hosted runner on the Pi with labels:

- self-hosted
- linux
- pi
- brain


