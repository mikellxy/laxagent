#!/usr/bin/env bash
# Upload the laxcode working tree into the lima VM at ~/code/laxcode.
#
# - Source is mirrored with rsync --delete (excluding .git, .venv, caches).
# - config/config.json is NOT copied verbatim: database.host and the NATS url
#   are rewritten to the Lima host gateway so the guest reaches
#   PostgreSQL/NATS running on the macOS host.
# - Builds the frontend on the host; finally runs `uv sync` inside the VM.
#
# Usage:  scripts/deploy_lima.sh          (from anywhere)
# Env:    VM_NAME=laxcode  GATEWAY=192.0.2.1

set -euo pipefail

VM_NAME="${VM_NAME:-laxcode}"
# From inside the guest, this name reaches the macOS host; lima writes it into
# the guest /etc/hosts (192.168.5.2 in the default user-v2 network).
GATEWAY="${GATEWAY:-host.lima.internal}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="code/laxcode"

SSH_CONFIG="$HOME/.lima/$VM_NAME/ssh.config"
SSH_HOST="lima-$VM_NAME"
if [[ ! -f "$SSH_CONFIG" ]]; then
  echo "error: lima VM '$VM_NAME' not found (missing $SSH_CONFIG)" >&2
  exit 1
fi
SSH_OPTS=(-F "$SSH_CONFIG" -o ControlPath=none)
ssh_with() { ssh "${SSH_OPTS[@]}" "$SSH_HOST" "$@"; }

npm --prefix "$SRC/frontend" ci
npm --prefix "$SRC/frontend" run build

ssh_with "mkdir -p '$DEST' '$DEST/config'"

rsync -az --delete \
  --exclude .git --exclude .venv --exclude __pycache__ \
  --exclude '*.pyc' --exclude '*.pyo' --exclude .pytest_cache \
  --exclude '*.swp' --exclude .DS_Store \
  --exclude node_modules --exclude test-results --exclude playwright-report \
  --exclude config/config.json \
  -e "ssh -F $SSH_CONFIG -o ControlPath=none" \
  "$SRC/" "$SSH_HOST:$DEST/"

# Patch config.json for guest->host networking, then upload it.
if [[ -f "$SRC/config/config.json" ]]; then
  PATCHED="$(mktemp)"
  GATEWAY="$GATEWAY" python3 - "$SRC/config/config.json" "$PATCHED" <<'PY'
import json, os, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as f:
    cfg = json.load(f)
cfg["database"]["host"] = os.environ["GATEWAY"]
nats = cfg.setdefault("nats", {})
nats["url"] = nats.get("url", "nats://127.0.0.1:4222").replace("127.0.0.1", os.environ["GATEWAY"])
with open(dst, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PY
  rsync -az -e "ssh -F $SSH_CONFIG -o ControlPath=none" \
    "$PATCHED" "$SSH_HOST:$DEST/config/config.json"
  rm -f "$PATCHED"
else
  echo "warning: config/config.json not found, VM keeps its existing config" >&2
fi

ssh_with "cd '$DEST' && ~/.local/bin/uv sync"

echo
echo "deployed to VM '$VM_NAME':~/code/laxcode"
echo "start the service with:"
echo "  limactl shell $VM_NAME"
echo "  cd ~/code/laxcode && uv run python -m laxcode.main --config config/config.json"
