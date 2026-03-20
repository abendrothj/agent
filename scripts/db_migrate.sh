#!/bin/bash
# Database migration script
# Applies schema and creates necessary tables/indexes

set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(dirname "$SCRIPT_DIR")

DB_HOST="${VAULT_DB_HOST:-localhost}"
DB_PORT="${VAULT_DB_PORT:-5432}"
DB_NAME="${VAULT_DB_NAME:-agent_memory}"
DB_USER="${VAULT_DB_USER:-vault}"

echo "=== Database Migration ==="
echo "Target: $DB_HOST:$DB_PORT/$DB_NAME"

# Wait for database
echo "Waiting for database..."
for i in {1..30}; do
    if pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" 2>/dev/null; then
        echo "✓ Database ready"
        break
    fi
    echo "  Attempt $i/30..."
    sleep 2
done

# Apply schema
echo "Applying schema..."
psql -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -U "$DB_USER" \
    -f "$PROJECT_ROOT/internal/memory/db_schema.sql" \
    2>&1 | head -20

echo "✓ Schema applied"

# Verify tables
echo "Verifying tables..."
psql -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -U "$DB_USER" \
    -c "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;" \
    2>/dev/null | tail -20

echo "=== Migration Complete ==="
