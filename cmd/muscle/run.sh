#!/bin/bash
# Teammate Muscle Service - macOS/Linux Launch Script
# This script starts the Muscle gRPC server

set -e

# Get the script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Teammate Muscle Service (macOS/Linux)"
echo "======================================"
echo ""

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate venv
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Check for .env file
if [ ! -f "../../.env" ]; then
    echo "WARNING: .env file not found!"
    echo "Creating template..."
    cat > ../../.env << EOF
HF_MODEL_ID=NousResearch/Hermes-2.5-Mistral-7B
HF_DEVICE=cuda
HF_DTYPE=float16
HF_MAX_TOKENS=1024
HF_TEMPERATURE=0.7
HF_TOP_K=50
HF_TOP_P=0.9
GRPC_PORT=50051
GRPC_HOST=0.0.0.0
CERT_FILE=./certs/muscle.crt
KEY_FILE=./certs/muscle.key
CA_CERT=./certs/client.crt
LOG_LEVEL=INFO
LOG_FILE=./logs/muscle.log
ACTIVITY_MONITORING_ENABLED=true
GPU_THRESHOLD_PERCENT=30
IDLE_THRESHOLD_SEC=300
ACTIVITY_CHECK_INTERVAL_SEC=5
EOF
    echo "Please edit .env file with correct paths, then run this script again."
    exit 1
fi

# Check for certificates
if [ ! -f "certs/muscle.crt" ]; then
    echo "ERROR: Certificate files not found!"
    echo "Please run the setup from WIN11_SETUP.md first."
    exit 1
fi

# Create logs directory
mkdir -p logs

# Start the Muscle service
echo ""
echo "Starting gRPC server on port 50051..."
echo "Press Ctrl+C to stop."
echo ""

python main.py
