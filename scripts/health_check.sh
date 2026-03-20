#!/bin/bash
# Service health check script
# Verifies all components are running and responsive

SERVICES=(
    "vault:50051"
    "shadow:50053"
    "watchdog:50054"
    "sandbox-agent:50055"
)

MUSCLE_HOST="${MUSCLE_HOST:-192.168.1.100}"
MUSCLE_PORT="${MUSCLE_PORT:-50051}"

echo "=== Agent System Health Check ==="
echo "Timestamp: $(date)"
echo ""

# Check Pi services
echo "✓ Pi Services (gRPC):"
for service_info in "${SERVICES[@]}"; do
    IFS=':' read -r service port <<< "$service_info"
    if timeout 2 nc -z localhost "$port" 2>/dev/null; then
        echo "  ✓ $service (port $port)"
    else
        echo "  ✗ $service (port $port) - NOT RESPONDING"
    fi
done

echo ""

# Check Muscle (Win11)
echo "✓ Win11 Services (gRPC):"
if timeout 2 nc -z "$MUSCLE_HOST" "$MUSCLE_PORT" 2>/dev/null; then
    echo "  ✓ Muscle ($MUSCLE_HOST:$MUSCLE_PORT)"
else
    echo "  ✗ Muscle ($MUSCLE_HOST:$MUSCLE_PORT) - NOT RESPONDING"
fi

echo ""

# Check database
echo "✓ Database:"
if timeout 2 nc -z localhost 5432 2>/dev/null; then
    echo "  ✓ PostgreSQL (localhost:5432)"
else
    echo "  ✗ PostgreSQL - NOT RESPONDING"
fi

echo ""

# Check Redis
echo "✓ Cache:"
if timeout 2 redis-cli -p 6379 ping 2>/dev/null | grep -q PONG; then
    echo "  ✓ Redis (localhost:6379)"
else
    echo "  ✗ Redis - NOT RESPONDING"
fi

echo ""
echo "=== Health Check Complete ==="
