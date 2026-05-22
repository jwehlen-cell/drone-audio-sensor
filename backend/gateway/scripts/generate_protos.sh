#!/usr/bin/env bash
set -euo pipefail

# Generate Python proto code into src-local proto_gen/ for IDE / local dev.
# In the container build, this happens inside the Dockerfile instead.

cd "$(dirname "$0")/.."
ROOT="$(cd ../.. && pwd)"

rm -rf proto_gen
mkdir -p proto_gen
python -m grpc_tools.protoc \
    -I"${ROOT}/proto" \
    --python_out=proto_gen \
    --pyi_out=proto_gen \
    --grpc_python_out=proto_gen \
    "${ROOT}/proto/drone_audio.proto"

echo "Generated proto_gen/ contents:"
ls -1 proto_gen
