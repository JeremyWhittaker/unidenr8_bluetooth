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

if ! systemctl list-unit-files 2>/dev/null | grep -q "^${UNIT_NAME}"; then
    echo "not installed; nothing to do"
    exit 0
fi

echo "== stopping and removing ${UNIT_NAME} (sudo)"
sudo systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
sudo rm -f "$UNIT_DEST"
sudo systemctl daemon-reload
sudo systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true

echo "== removed. The state directory and history database were left in place."
