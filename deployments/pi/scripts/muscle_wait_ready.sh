#!/usr/bin/env bash
set -euo pipefail

WIN11_IP="${WIN11_IP:?Set WIN11_IP}"
MUSCLE_PORT="${MUSCLE_PORT:-50051}"
TLS_SERVER_NAME="${TLS_SERVER_NAME:-win11-muscle}"
CERT_FILE="${CERT_FILE:-/opt/teammate-vault/certs/client.crt}"
KEY_FILE="${KEY_FILE:-/opt/teammate-vault/certs/client.key}"
CA_CERT="${CA_CERT:-/opt/teammate-vault/certs/muscle.crt}"

for i in $(seq 1 90); do
  python3 - <<'PY' && exit 0 || true
import os
import grpc

win11_ip = os.environ["WIN11_IP"]
port = int(os.environ["MUSCLE_PORT"])
server_name = os.environ["TLS_SERVER_NAME"]

with open(os.environ["CA_CERT"], "rb") as f:
    root = f.read()
with open(os.environ["KEY_FILE"], "rb") as f:
    key = f.read()
with open(os.environ["CERT_FILE"], "rb") as f:
    cert = f.read()

creds = grpc.ssl_channel_credentials(root_certificates=root, private_key=key, certificate_chain=cert)
ch = grpc.secure_channel(
    f"{win11_ip}:{port}",
    creds,
    options=(("grpc.ssl_target_name_override", server_name), ("grpc.default_authority", server_name)),
)
health = ch.unary_unary('/muscle.api.Muscle/Health', request_serializer=lambda _: b'', response_deserializer=lambda b: b)
resp = health(object(), timeout=4)
print("ready", len(resp))
PY
  sleep 2
done

echo "Muscle did not become ready in time" >&2
exit 1
