#!/bin/bash
# Emergency rollback script
# Triggered manually or by Watchdog on fatal errors

set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(dirname "$SCRIPT_DIR")

echo "=== EMERGENCY ROLLBACK INITIATED ==="
echo "Timestamp: $(date)"

# Stop all services
echo "Stopping services..."
cd "$PROJECT_ROOT"
docker-compose down 2>/dev/null || true
docker stop agent_vault agent_shadow agent_watchdog agent_sandbox 2>/dev/null || true

# Clear caches
echo "Clearing caches..."
docker-compose exec -T redis redis-cli FLUSHALL 2>/dev/null || true

# Check Muscle service health
echo "Checking Muscle service health..."
MUSCLE_HOST="${MUSCLE_HOST:-192.168.1.100}"
MUSCLE_PORT="${MUSCLE_PORT:-50051}"

if timeout 5 nc -z "$MUSCLE_HOST" "$MUSCLE_PORT" 2>/dev/null; then
    echo "✓ Muscle service responsive"
else
    echo "✗ Muscle service unresponsive; manual intervention required"
fi

# Restart services in order
echo "Restarting services..."
cd "$PROJECT_ROOT"
docker-compose up -d postgres redis
sleep 10
docker-compose up -d vault shadow watchdog sandbox-agent

# Verify health
echo "Verifying service health..."
sleep 5
docker-compose ps

echo "=== EMERGENCY ROLLBACK COMPLETE ==="
echo "All services have been restarted. Manual verification recommended."
