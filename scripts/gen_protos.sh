#!/bin/bash
# Generate Python gRPC stub files from all .proto definitions.
# Run from the project root:
#   bash scripts/gen_protos.sh
#
# Output: internal/api/<name>_pb2.py and <name>_pb2_grpc.py
# The generated files import each other using bare names
# (e.g. `import vault_pb2`), so add internal/api to sys.path
# when importing them from outside that directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PROTO_SRC="$PROJECT_ROOT/internal/api"
PROTO_OUT="$PROJECT_ROOT/internal/api"

echo "=== Generating protobuf stubs ==="
echo "  Source:  $PROTO_SRC"
echo "  Output:  $PROTO_OUT"

# Verify grpcio-tools is available
if ! python3 -m grpc_tools.protoc --version >/dev/null 2>&1; then
    echo "ERROR: grpcio-tools not found. Install it:"
    echo "  pip install grpcio-tools>=1.60.0"
    exit 1
fi

PROTOS=(
    vault.proto
    shadow.proto
    sandbox.proto
    memory.proto
    watchdog.proto
)

for proto in "${PROTOS[@]}"; do
    echo "  Generating $proto ..."
    python3 -m grpc_tools.protoc \
        -I "$PROTO_SRC" \
        --python_out="$PROTO_OUT" \
        --grpc_python_out="$PROTO_OUT" \
        "$PROTO_SRC/$proto"
done

echo ""
echo "✓ Generated stubs in $PROTO_OUT:"
ls -1 "$PROTO_OUT"/*_pb2*.py 2>/dev/null || echo "  (none found — check for errors above)"

# Fix relative imports in _grpc files so they work when internal/api
# is on sys.path.  grpcio-tools emits  `import vault_pb2 as vault__pb2`
# which is correct for the path-append import strategy used by all services.
echo ""
echo "✓ Proto codegen complete."
