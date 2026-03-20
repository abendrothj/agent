#!/usr/bin/env bash
set -euo pipefail

WIN11_MAC="${WIN11_MAC:?Set WIN11_MAC, e.g. AA:BB:CC:DD:EE:FF}"
WIN11_IP="${WIN11_IP:?Set WIN11_IP, e.g. 10.0.0.50}"
MUSCLE_PORT="${MUSCLE_PORT:-50051}"

wakeonlan "$WIN11_MAC"

echo "Sent WoL packet to $WIN11_MAC"
echo "Waiting for host ping..."

for i in $(seq 1 60); do
  if ping -c 1 -W 1 "$WIN11_IP" >/dev/null 2>&1; then
    echo "Host is reachable"
    exit 0
  fi
  sleep 2
done

echo "Timed out waiting for Win11 host ($WIN11_IP)" >&2
exit 1
