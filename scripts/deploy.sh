#!/bin/bash
# Deploy this project to the Pi as an ordinary user-owned tree.
#
# Copies source, tests, docs and packaging to /home/jeremy/unidenr8 and
# nothing else.  It does not touch /home/jeremy/hummer-obd, does not install
# or enable a systemd unit, does not run sudo, and does not start a scan.
#
# HOST is deliberately not defaulted to a name or address: the node is
# reachable by a private VPN name that this repository must not record.
set -euo pipefail

: "${HOST:?set HOST=user@node (never commit the value)}"
DEST="${DEST:-/home/jeremy/unidenr8}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXCLUDES=(--exclude '__pycache__' --exclude '*.pyc' --exclude '*.egg-info'
          --exclude '.private' --exclude '.foreman' --exclude '.git'
          --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.venv')

echo "# deploying -> \$HOST:$DEST"
ssh "$HOST" "mkdir -p '$DEST' '$DEST/.state' && chmod 700 '$DEST/.state'"

rsync -a --delete "${EXCLUDES[@]}" "$SRC_DIR/src/"    "$HOST:$DEST/src/"
rsync -a --delete "${EXCLUDES[@]}" "$SRC_DIR/tests/"  "$HOST:$DEST/tests/"
rsync -a          "${EXCLUDES[@]}" "$SRC_DIR/docs/"   "$HOST:$DEST/docs/"
rsync -a          "${EXCLUDES[@]}" "$SRC_DIR/scripts/" "$HOST:$DEST/scripts/"
rsync -a          "${EXCLUDES[@]}" "$SRC_DIR/systemd/" "$HOST:$DEST/systemd/"
rsync -a "$SRC_DIR/pyproject.toml" "$SRC_DIR/pytest.ini" "$SRC_DIR/README.md" \
         "$SRC_DIR/.gitignore" "$HOST:$DEST/"

echo "# deployed.  The systemd template is copied but NOT installed, NOT"
echo "# enabled and NOT started; installing it needs a deliberate manual step."
