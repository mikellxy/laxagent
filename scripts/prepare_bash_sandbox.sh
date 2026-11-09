#!/usr/bin/env bash
# Run once as root on Ubuntu 24.04, then run the Python probe as the service user.
set -euo pipefail
if [[ "$(id -u)" != 0 ]]; then
  echo "Run with sudo bash scripts/prepare_bash_sandbox.sh" >&2
  exit 1
fi
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  bubblewrap apparmor bash coreutils procps util-linux ca-certificates \
  python3 python3-venv golang-go build-essential git curl

# Do not replace administrator profiles or disable the global userns restriction.
python3 - <<'PY'
from pathlib import Path
import subprocess

root = Path('/etc/apparmor.d')
target = root / 'laxcode-bwrap'
profile = '''abi <abi/4.0>,
include <tunables/global>

profile laxcode-bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
'''
others = []
for path in root.rglob('*'):
    if path == target or not path.is_file():
        continue
    if 'bwrap' in path.read_text(errors='replace'):
        others.append(str(path))
if others:
    print('Existing bwrap-related AppArmor configuration retained: ' + ', '.join(others))
    print('Run the ordinary-user sandbox probe; resolve existing policy if it fails.')
elif target.is_symlink() or (target.exists() and target.read_text() != profile):
    raise SystemExit('Refusing to overwrite modified ' + str(target))
else:
    target.write_text(profile)
    subprocess.run(['apparmor_parser', '-r', str(target)], check=True)
PY
echo "Dependencies prepared. As the service user run:"
echo "python3 src/laxcode/sandbox/bubblewrap.py --probe"
