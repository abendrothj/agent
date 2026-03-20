# Raspberry Pi Setup (Vault Side)

This guide documents the Pi-side certificate setup for this repo.

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
