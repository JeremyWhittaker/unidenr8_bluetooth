#!/bin/bash
# Remove the Uniden R8 collector service.  Leaves the tree, the venv, the
# configuration, the state directory and the history database alone -- this
# stops a service, it does not delete anybody's data.
#
# Touches exactly one unit.  It does not stop, restart or disable
# `hummer-rfcomm`, `bluetooth` or the display service.
set -euo pipefail

UNIT_NAME="unidenr8-collector.service"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}"

# `grep -c`, not `grep -q`, and that is the whole point.  Under
# `set -o pipefail` a pipe into `grep -q` reports *failure on a match*: grep
# exits at its first hit, the writer dies of SIGPIPE with 141, and the pipeline
# takes that status.  This script read that as "not installed" and exited 0
# without uninstalling anything -- on exactly the nodes where the unit was
# present.  `grep -c` reads to end of input, so nothing is ever sent SIGPIPE.
# Asking systemd about the one unit by name also keeps the match exact.
if ! systemctl list-unit-files "$UNIT_NAME" --no-legend 2>/dev/null | grep -c . >/dev/null; then
    echo "not installed; nothing to do"
    exit 0
fi

echo "== stopping and removing ${UNIT_NAME} (sudo)"
sudo systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
sudo rm -f "$UNIT_DEST"
sudo systemctl daemon-reload
sudo systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true

echo "== removed. The state directory and history database were left in place."
