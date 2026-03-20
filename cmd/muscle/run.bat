@echo off
REM Teammate Muscle Service - Win11 Launch Script
REM This script starts the Muscle gRPC server

setlocal enabledelayedexpansion

REM Ensure we're in the correct directory
cd /d "%~dp0"

REM Check if venv exists
if not exist "venv\" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Failed to create venv. Ensure Python 3.11+ is installed.
        exit /b 1
    )
)

REM Activate venv
call venv\Scripts\activate.bat

REM Install/upgrade dependencies
echo Installing dependencies...
pip install -q --upgrade pip
pip install -q -r requirements.txt

REM Check if .env exists
if not exist "..\..\\.env" (
    echo WARNING: .env file not found!
    echo Creating template at ...\.env
    echo.
    echo HF_MODEL_ID=NousResearch/Hermes-2.5-Mistral-7B > ..\..\\.env
    echo HF_DEVICE=cuda >> ..\..\\.env
    echo HF_DTYPE=float16 >> ..\..\\.env
    echo HF_MAX_TOKENS=1024 >> ..\..\\.env
    echo HF_TEMPERATURE=0.7 >> ..\..\\.env
    echo HF_TOP_K=50 >> ..\..\\.env
    echo HF_TOP_P=0.9 >> ..\..\\.env
    echo GRPC_PORT=50051 >> ..\..\\.env
    echo GRPC_HOST=0.0.0.0 >> ..\..\\.env
    echo CERT_FILE=./certs/muscle.crt >> ..\..\\.env
    echo KEY_FILE=./certs/muscle.key >> ..\..\\.env
    echo CA_CERT=./certs/client.crt >> ..\..\\.env
    echo LOG_LEVEL=INFO >> ..\..\\.env
    echo LOG_FILE=./logs/muscle.log >> ..\..\\.env
    echo ACTIVITY_MONITORING_ENABLED=true >> ..\..\\.env
    echo GPU_THRESHOLD_PERCENT=30 >> ..\..\\.env
    echo IDLE_THRESHOLD_SEC=300 >> ..\..\\.env
    echo ACTIVITY_CHECK_INTERVAL_SEC=5 >> ..\..\\.env
    echo.
    echo Please edit .env file with correct paths, then run this script again.
    pause
    exit /b 1
)

REM Check if certificates exist
if not exist "certs\muscle.crt" (
    echo ERROR: Certificate files not found!
    echo Please run the setup from WIN11_SETUP.md first.
    pause
    exit /b 1
)

REM Create logs directory if needed
if not exist "logs" mkdir logs

REM Start the Muscle service
echo.
echo ======================================
echo Teammate Muscle Service (Win11)
echo ======================================
echo.
echo Starting gRPC server on port 50051...
echo Press Ctrl+C to stop.
echo.

python main.py

REM Deactivate venv on exit
deactivate

pause
