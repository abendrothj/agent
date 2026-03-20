#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: ./decrypt.sh <password>"
  exit 1
fi

PASSWORD="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENC_FILE="$SCRIPT_DIR/muscle-certs.zip.enc"
ZIP_FILE="$SCRIPT_DIR/muscle-certs.zip"
OUT_DIR="$SCRIPT_DIR/decrypted-certs"

if [[ ! -f "$ENC_FILE" ]]; then
  echo "Encrypted bundle not found: $ENC_FILE"
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required but not installed"
  exit 1
fi

if ! command -v unzip >/dev/null 2>&1; then
  echo "unzip is required but not installed"
  exit 1
fi

openssl enc -d -aes-256-cbc -pbkdf2 \
  -in "$ENC_FILE" \
  -out "$ZIP_FILE" \
  -pass "pass:$PASSWORD"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
unzip -q "$ZIP_FILE" -d "$OUT_DIR"
rm -f "$ZIP_FILE"

echo "Decrypted certs written to: $OUT_DIR"
