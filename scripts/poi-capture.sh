#!/bin/bash
# One POI capture: stop the collector, read once, restart the collector.
#
#     sudo ./scripts/poi-capture.sh
#
# This is the V8 procedure in `docs/VALIDATION.md`, which needs the detector's
# link free because only one BLE central may hold it at a time. Doing it by hand
# is three commands with a stopped collector in the middle, and a collector left
# stopped is a drive not captured -- so this always restarts it, including when
# the read fails.
#
# It reads as the owning user rather than as root. The private store is 0700 and
# owned by that account, and a root-written capture inside it would be
# unreadable to every other command in this project.
#
# Two details are deliberate, and both are here because the first version of
# this script got them wrong:
#
#   * **It prints everything `inspect` says.** The first version filtered the
#     output down to the POI section, so a failed read showed the operator an
#     empty result and no reason. Hiding a command's own error output has cost
#     real time on this project more than once.
#   * **It waits, then retries.** After the collector disconnects, BlueZ needs a
#     few seconds before the detector is connectable by a new central. Four
#     seconds was not enough: the read failed with "device not found" twelve
#     seconds after the collector stopped.
set -uo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo: sudo bash $0" >&2; exit 1; }

OWNER="${SUDO_USER:-jeremy}"
# Derived from this script's own location, so the file is the same one whether
# it is run from a checkout or from the deployed tree.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BEFORE=$(ls -1 "$ROOT"/.private/inspect-*.json 2>/dev/null | wc -l)

echo "== stopping the collector so nothing else holds the detector"
systemctl stop unidenr8-collector
echo "   waiting for BlueZ to release the link"
sleep 12

read_once() {
    sudo -u "$OWNER" env HOME="/home/$OWNER" \
        "$ROOT/.venv/bin/python" -m uniden_r8.cli inspect --confirm 2>&1
}

echo "== reading the POI characteristic (attempt 1)"
OUT="$(read_once)"
echo "$OUT" | sed 's/^/   /'

# A bash pattern match, not `echo "$OUT" | grep -q`. Under `set -o pipefail`
# that pipeline reports failure *on a match* -- grep exits at the first hit and
# echo dies of SIGPIPE -- so the retry below would never have run. Caught by
# `test_no_script_pipes_into_an_early_exiting_reader_under_pipefail`, which
# exists because this same mistake shipped twice before.
lower="${OUT,,}"
if [[ "$lower" == *"did not connect"* || "$lower" == *"not found"* ]]; then
    echo "== attempt 1 could not reach the detector; waiting and retrying once"
    sleep 15
    echo "== reading the POI characteristic (attempt 2)"
    read_once | sed 's/^/   /'
fi

echo "== restarting the collector"
systemctl reset-failed unidenr8-collector 2>/dev/null
systemctl start unidenr8-collector
sleep 8
AFTER=$(ls -1 "$ROOT"/.private/inspect-*.json 2>/dev/null | wc -l)
printf "   unit: %s   captures: %s -> %s\n" \
    "$(systemctl is-active unidenr8-collector)" "$BEFORE" "$AFTER"
[[ "$AFTER" -gt "$BEFORE" ]] \
    && echo "== a new capture was written" \
    || echo "== NO new capture was written -- the read did not succeed"
