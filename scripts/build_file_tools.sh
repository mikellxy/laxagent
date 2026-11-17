#!/usr/bin/env bash
# Build Linux execution tools; GOARCH selects the target container architecture.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT="${LAXAGENT_TOOLS_OUTPUT:-$ROOT/src/laxagent/tools/bin}"
mkdir -p "$OUTPUT"
cd "$ROOT/src/laxagent/tools/go"
for tool in read_file write_file edit_file sandbox_exec; do
  CGO_ENABLED=0 GOOS=linux go build -trimpath -o "$OUTPUT/$tool" "./cmd/$tool"
done
