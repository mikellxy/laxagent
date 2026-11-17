#!/usr/bin/env bash
# Build only read_file. GOARCH may select a different Linux deployment target.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/src/laxagent/tools/bin"
cd "$ROOT/src/laxagent/tools/go"
CGO_ENABLED=0 GOOS=linux go build -trimpath -o "$ROOT/src/laxagent/tools/bin/read_file" ./cmd/read_file
