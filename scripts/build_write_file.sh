#!/usr/bin/env bash
# Build only write_file. GOARCH may select a different Linux deployment target.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/src/laxagent/tools/bin"
cd "$ROOT/src/laxagent/tools/go"
CGO_ENABLED=0 GOOS=linux go build -trimpath -o "$ROOT/src/laxagent/tools/bin/write_file" ./cmd/write_file
