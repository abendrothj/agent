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

# ── GraphRAG index directory setup ────────────────────────────────────────────
echo ""
echo "=== GraphRAG Setup ==="
GRAPHRAG_DIR="${GRAPHRAG_INDEX_DIR:-$PROJECT_ROOT/graphrag_index}"
INPUT_DIR="$GRAPHRAG_DIR/input"

if [ ! -d "$INPUT_DIR" ]; then
    echo "Creating GraphRAG input directory: $INPUT_DIR"
    mkdir -p "$INPUT_DIR"
    echo "✓ Created $INPUT_DIR"
else
    echo "✓ GraphRAG input dir exists: $INPUT_DIR"
fi

# Verify prompt files are present (required for graphrag index command)
REQUIRED_PROMPTS=(
    "entity_extraction.txt"
    "summarize_descriptions.txt"
    "community_report.txt"
    "claim_extraction.txt"
)

PROMPTS_DIR="$GRAPHRAG_DIR/prompts"
MISSING_PROMPTS=0
for prompt in "${REQUIRED_PROMPTS[@]}"; do
    if [ ! -f "$PROMPTS_DIR/$prompt" ]; then
        echo "  MISSING: $PROMPTS_DIR/$prompt"
        MISSING_PROMPTS=$((MISSING_PROMPTS + 1))
    fi
done

if [ "$MISSING_PROMPTS" -gt 0 ]; then
    echo "WARNING: $MISSING_PROMPTS GraphRAG prompt file(s) missing."
    echo "  Run from project root: ls graphrag_index/prompts/"
else
    echo "✓ All GraphRAG prompt files present"
fi

# ── Proto stub reminder ───────────────────────────────────────────────────────
echo ""
echo "=== Proto Stubs ==="
PROTO_OUT="$PROJECT_ROOT/internal/api"
STUBS_FOUND=$(ls "$PROTO_OUT"/*_pb2.py 2>/dev/null | wc -l | tr -d ' ')
if [ "$STUBS_FOUND" -eq 0 ]; then
    echo "WARNING: No generated pb2 stubs found in $PROTO_OUT"
    echo "  Run: bash scripts/gen_protos.sh"
else
    echo "✓ Found $STUBS_FOUND generated proto stub file(s)"
fi

echo ""
echo "=== Setup Complete ==="
