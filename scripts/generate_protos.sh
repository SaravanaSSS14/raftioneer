#!/usr/bin/env bash
# Regenerates Python gRPC stubs from proto/ into generated/.
# generated/ is gitignored — proto/*.proto is the single source of truth.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p generated
touch generated/__init__.py

python -m grpc_tools.protoc \
  -I proto \
  --python_out=generated \
  --grpc_python_out=generated \
  proto/auction.proto proto/raft.proto proto/llm.proto

echo "Generated stubs in generated/"
