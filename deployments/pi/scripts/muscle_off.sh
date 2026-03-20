#!/usr/bin/env bash
set -euo pipefail

WIN11_HOST="${WIN11_HOST:?Set WIN11_HOST, e.g. jakea@10.0.0.50}"
WIN11_SCRIPT_PATH="${WIN11_SCRIPT_PATH:-C:/Users/jakea/Desktop/agent/deployments/win11/power/sleep_muscle.ps1}"
MODE="${MODE:-sleep}"

ssh -o BatchMode=yes "$WIN11_HOST" \
  "powershell -NoProfile -ExecutionPolicy Bypass -File '$WIN11_SCRIPT_PATH' -Mode $MODE"

echo "Requested Win11 Muscle $MODE"
