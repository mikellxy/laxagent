#!/usr/bin/env bash
# Build all file tools for Linux; GOARCH may select a cross-compilation target.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
for tool in read_file write_file edit_file; do
  bash "$ROOT/scripts/build_${tool}.sh"
done
