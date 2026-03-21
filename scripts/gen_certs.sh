#!/usr/bin/env bash
# gen_certs.sh — Generate mTLS certificates for Pi Vault ↔ Win11 Muscle gRPC
#
# Produces a self-signed CA and two leaf certs:
#   vault  — server cert (Pi), SAN includes LAN IP
#   muscle — client cert (Win11)
#
# Usage:
#   VAULT_HOST=192.168.1.100 bash scripts/gen_certs.sh
#
# Output: ./certs/  (relative to where script is called from)
#
# After running, copy:
#   Pi:    ca.crt vault.crt vault.key  →  /opt/teammate-vault/certs/
#   Win11: ca.crt muscle.crt muscle.key → deployments/win11/certs/

set -euo pipefail

VAULT_HOST="${VAULT_HOST:-192.168.1.100}"
OUT_DIR="${OUT_DIR:-$(dirname "$0")/../certs}"
DAYS=365
BITS=2048

mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

echo "==> Generating certs in $(pwd)"
echo "    VAULT_HOST = $VAULT_HOST"
echo "    Validity   = ${DAYS} days"
echo ""

# ── 1. CA ──────────────────────────────────────────────────────────────────
echo "[1/5] Generating CA key and self-signed cert..."
openssl genrsa -out ca.key "$BITS" 2>/dev/null
openssl req -new -x509 -days "$DAYS" -key ca.key -out ca.crt \
  -subj "/CN=teammate-agent-ca/O=teammate-agent"

# ── 2. Vault server cert (Pi) ──────────────────────────────────────────────
echo "[2/5] Generating Vault (Pi server) key and CSR..."
openssl genrsa -out vault.key "$BITS" 2>/dev/null
openssl req -new -key vault.key -out vault.csr \
  -subj "/CN=vault/O=teammate-agent"

echo "[3/5] Signing Vault cert with CA (SAN: localhost + $VAULT_HOST)..."
cat > vault.ext <<EOF
subjectAltName=DNS:localhost,DNS:vault,IP:127.0.0.1,IP:${VAULT_HOST}
extendedKeyUsage=serverAuth
EOF
openssl x509 -req -days "$DAYS" \
  -in vault.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out vault.crt -extfile vault.ext 2>/dev/null
rm vault.csr vault.ext

# ── 3. Muscle client cert (Win11) ──────────────────────────────────────────
echo "[4/5] Generating Muscle (Win11 client) key and CSR..."
openssl genrsa -out muscle.key "$BITS" 2>/dev/null
openssl req -new -key muscle.key -out muscle.csr \
  -subj "/CN=muscle/O=teammate-agent"

echo "[5/5] Signing Muscle cert with CA..."
cat > muscle.ext <<EOF
extendedKeyUsage=clientAuth
EOF
openssl x509 -req -days "$DAYS" \
  -in muscle.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out muscle.crt -extfile muscle.ext 2>/dev/null
rm muscle.csr muscle.ext ca.srl 2>/dev/null || true

# ── Verify ─────────────────────────────────────────────────────────────────
echo ""
echo "==> Verifying certs..."
openssl verify -CAfile ca.crt vault.crt   && echo "    vault.crt  OK"
openssl verify -CAfile ca.crt muscle.crt  && echo "    muscle.crt OK"

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "==> Generated files in $(pwd):"
ls -1 ./*.crt ./*.key 2>/dev/null | sed 's/^/    /'

echo ""
echo "==> Next steps:"
echo ""
echo "    Pi (Raspberry Pi — Vault service):"
echo "      sudo mkdir -p /opt/teammate-vault/certs"
echo "      sudo cp ca.crt vault.crt vault.key /opt/teammate-vault/certs/"
echo "      sudo chmod 600 /opt/teammate-vault/certs/vault.key"
echo ""
echo "    Win11 (Muscle service):"
echo "      Copy ca.crt muscle.crt muscle.key to deployments/win11/certs/"
echo ""
echo "    Rotate before expiry (${DAYS} days):"
echo "      VAULT_HOST=$VAULT_HOST bash scripts/gen_certs.sh"
echo ""
