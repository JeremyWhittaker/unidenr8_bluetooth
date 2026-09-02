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
# .state is excluded as deliberately as .private: it holds the schema-2
# document, which carries the detector's own heading, speed and altitude, and
# the history database, which can carry coordinates. Copying either between
# machines would move a rough trace of a drive without anybody deciding to.
EXCLUDES=(--exclude '__pycache__' --exclude '*.pyc' --exclude '*.egg-info'
          --exclude '.private' --exclude '.state' --exclude '.foreman'
          --exclude '.git' --exclude '.pytest_cache' --exclude '.ruff_cache'
          --exclude '.venv' --exclude '*.db' --exclude '*.db-wal'
          --exclude '*.db-shm')

echo "# deploying -> \$HOST:$DEST"
ssh "$HOST" "mkdir -p '$DEST' '$DEST/.state' && chmod 700 '$DEST/.state'"

rsync -a --delete "${EXCLUDES[@]}" "$SRC_DIR/src/"    "$HOST:$DEST/src/"
rsync -a --delete "${EXCLUDES[@]}" "$SRC_DIR/tests/"  "$HOST:$DEST/tests/"
rsync -a          "${EXCLUDES[@]}" "$SRC_DIR/docs/"   "$HOST:$DEST/docs/"
rsync -a          "${EXCLUDES[@]}" "$SRC_DIR/scripts/" "$HOST:$DEST/scripts/"
rsync -a          "${EXCLUDES[@]}" "$SRC_DIR/systemd/" "$HOST:$DEST/systemd/"
rsync -a "$SRC_DIR/pyproject.toml" "$SRC_DIR/pytest.ini" "$SRC_DIR/README.md" \
         "$SRC_DIR/.gitignore" "$HOST:$DEST/"

# The node's own unidenr8.toml is deliberately NOT overwritten. It names that
# node's OBD unit, its state directory and which feeds are switched on; a deploy
# that replaced it would silently reconfigure a running vehicle.
echo "# deployed.  The systemd template is copied but NOT installed, NOT"
echo "# enabled and NOT started; installing it needs a deliberate manual step."
echo "# unidenr8.toml on the node was left alone.  If this is a first deploy,"
echo "#   ssh \$HOST '$DEST/.venv/bin/python -m uniden_r8.cli config --example' \\"
echo "#     > /tmp/unidenr8.toml   # then edit and copy it into place."
