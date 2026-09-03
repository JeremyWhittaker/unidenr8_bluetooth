#!/bin/bash
# Install the Uniden R8 collector as a systemd system service.
#
# Run this ON the node, from the deployed tree.  It needs `sudo` for exactly
# three things -- writing one unit file, reloading the manager, and starting one
# unit -- and it touches no other unit.  In particular it never restarts, edits,
# enables or disables `hummer-rfcomm`, `bluetooth`, or the display service: the
# vehicle's OBD-II link is somebody else's, and a script that can restart it is
# a script that can break a drive.
#
#     ./scripts/install-service.sh                 # install, enable at boot, start
#     ./scripts/install-service.sh --no-enable     # install and start, not at boot
#     ./scripts/install-service.sh --dry-run       # print the unit, change nothing
#
# It is idempotent: running it again re-templates the unit, reloads and
# restarts.  Re-running after a `git pull` + deploy is the supported upgrade.
#
# Why a script rather than the four commands in the runbook: those commands
# require the operator to hand-edit four paths in the unit first, and a
# hand-edited path that is subtly wrong produces a service that starts, does
# nothing useful, and reports `active (running)` while it does it.  Everything
# here is derived from the tree it is run out of, and then verified against a
# live counter rather than against systemd's opinion of the process.
set -euo pipefail

UNIT_NAME="unidenr8-collector.service"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}"

ENABLE=1
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --no-enable) ENABLE=0 ;;
        --dry-run)   DRY_RUN=1 ;;
        -h|--help)   sed -n '2,25p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT/systemd/${UNIT_NAME}"
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"
PYTHON="$ROOT/.venv/bin/python"
CONFIG="$ROOT/unidenr8.toml"

say()  { printf '%s\n' "$*"; }
fail() { printf 'install-service: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Preflight.  Each of these is a way the service can come up "active" and be
# useless, so each is checked before anything is written rather than after.
# ---------------------------------------------------------------------------

say "== preflight"

[[ -f "$TEMPLATE" ]] || fail "no unit template at $TEMPLATE (deploy the tree first)"
[[ -x "$PYTHON"   ]] || fail "no interpreter at $PYTHON -- create the venv first:
    python3 -m venv $ROOT/.venv && $ROOT/.venv/bin/pip install -e '$ROOT[ble]'"
[[ -f "$CONFIG"   ]] || fail "no configuration at $CONFIG -- generate one with:
    $PYTHON -m uniden_r8.cli config --example > $CONFIG   # then edit it"

command -v systemctl >/dev/null 2>&1 || fail "no systemctl on this host"

"$PYTHON" -c 'import uniden_r8' 2>/dev/null \
    || fail "the venv cannot import uniden_r8 -- reinstall it with pip install -e ."

# The state directory is where every writable file lives, and it is the only
# path the hardened unit is allowed to write.  Read it out of the config rather
# than assuming, because a unit whose ReadWritePaths does not match the
# configured state_dir starts cleanly and then fails on its first write.
#
# A relative `state_dir` must be resolved against the tree, not against
# whoever's directory this script was invoked from.  The unit sets
# `WorkingDirectory=$ROOT`, so that is where the collector will resolve it --
# and an installer run from ~ would otherwise render
# `ReadWritePaths=/home/jeremy/.state` for a service that writes in
# `/home/jeremy/unidenr8/.state`, then report success.
STATE_DIR="$(
    "$PYTHON" - "$CONFIG" "$ROOT" <<'PY'
import pathlib, sys
try:
    import tomllib
except ModuleNotFoundError:                      # pragma: no cover - py<3.11
    import tomli as tomllib
doc = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())
value = (doc.get("collector") or {}).get("state_dir") or ".state"
path = pathlib.Path(value).expanduser()
if not path.is_absolute():
    path = pathlib.Path(sys.argv[2]) / path
print(path.resolve())
PY
)" || fail "could not read state_dir out of $CONFIG"

[[ -n "$STATE_DIR" ]] || fail "state_dir resolved empty"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

# `[history] enabled` defaults to OFF, which is the right default for a library
# and the wrong one for a service somebody installed in a vehicle to record
# drives.  A collector with history off runs perfectly, publishes a healthy
# state document with a rising packet counter, drives all day and writes not one
# row to SQLite -- and the operator finds out afterwards, which is the one time
# it cannot be fixed.
HISTORY_ON="$(
    "$PYTHON" - "$CONFIG" <<'PY' 2>/dev/null || true
import pathlib, sys
try:
    import tomllib
except ModuleNotFoundError:                      # pragma: no cover - py<3.11
    import tomli as tomllib
history = tomllib.loads(pathlib.Path(sys.argv[1]).read_text()).get("history") or {}
print("yes" if history.get("enabled") else "no")
PY
)"
if [[ "$HISTORY_ON" != "yes" ]]; then
    say "   warning: [history] enabled is off. The service will run and publish"
    say "            state, but record nothing to SQLite -- no drive can be"
    say "            reviewed afterwards. Set enabled = true in $CONFIG."
else
    say "   history: recording to SQLite"
fi

# The read-only property audit.  If this does not hold, nothing else matters and
# the service must not be installed: it is the control that proves the package
# still has no write path to the detector.
say "== selftest"
"$PYTHON" -m uniden_r8.cli selftest >/dev/null \
    || fail "selftest failed -- refusing to install. Run it directly and read the output:
    $PYTHON -m uniden_r8.cli selftest"
say "   read-only properties hold"

# A second instance would fight the first for the radio, and the loser is
# whichever one the vehicle needed.
if pgrep -f 'uniden_r8.cli collect' >/dev/null 2>&1; then
    say "   note: a collector is already running by hand; it will be replaced"
fi

# The OBD guard is advisory here, not fatal: the collector re-checks it every
# pass and simply waits while the link is unhealthy.  But an operator who has
# `guard = true` on a node with no OBDLink gets a service that never connects,
# and saying so now is cheaper than reading `obd-blocked` out of a state file
# later.
#
# Read the unit name out of the configuration rather than hardcoding it, and
# ask systemd about that one unit directly.  Deliberately no pipe: under
# `set -o pipefail`, `systemctl ... | grep -q` reports failure on a *match*,
# because grep exits at the first hit and systemctl dies of SIGPIPE with 141.
# That reads as "the unit is missing" on exactly the nodes where it is present.
OBD_GUARD="$(
    "$PYTHON" - "$CONFIG" <<'PY' 2>/dev/null || true
import pathlib, sys
try:
    import tomllib
except ModuleNotFoundError:                      # pragma: no cover - py<3.11
    import tomli as tomllib
obd = tomllib.loads(pathlib.Path(sys.argv[1]).read_text()).get("obd") or {}
if obd.get("guard", True):
    print(obd.get("unit", "hummer-rfcomm"))
PY
)"

if [[ -n "$OBD_GUARD" ]]; then
    obd_state="$(systemctl is-active "$OBD_GUARD" 2>/dev/null || true)"
    case "$obd_state" in
        active)
            say "   OBD guard: $OBD_GUARD is active"
            ;;
        inactive|failed|activating|deactivating)
            say "   warning: [obd] guard names '$OBD_GUARD', which is $obd_state."
            say "            The collector will wait in 'obd-blocked' until it is active."
            ;;
        *)
            say "   warning: [obd] guard names '$OBD_GUARD', which this host does not have."
            say "            The collector will sit in 'obd-blocked' and never connect."
            say "            Set guard = false in $CONFIG if this node has no OBDLink."
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# Template.  Every path in the shipped unit is a placeholder for this node.
# ---------------------------------------------------------------------------

say "== templating"
say "   user:       $RUN_USER"
say "   tree:       $ROOT"
say "   state dir:  $STATE_DIR"

RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT

sed \
    -e "s|^User=.*|User=${RUN_USER}|" \
    -e "s|^Group=.*|Group=${RUN_GROUP}|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=${ROOT}|" \
    -e "s|^Environment=PYTHONPATH=.*|Environment=PYTHONPATH=${ROOT}/src|" \
    -e "s|^Environment=UNIDEN_R8_CONFIG=.*|Environment=UNIDEN_R8_CONFIG=${CONFIG}|" \
    -e "s|^ExecStart=.*|ExecStart=${PYTHON} -m uniden_r8.cli collect|" \
    -e "s|^ReadWritePaths=.*|ReadWritePaths=${STATE_DIR}|" \
    -e "s|^Documentation=.*|Documentation=file:${ROOT}/docs/RUNBOOK.md|" \
    "$TEMPLATE" > "$RENDERED"

# Prove the substitutions actually landed.  `sed` reports success when it
# matches nothing, so an upstream rename of a key would silently ship the
# template's own paths -- which point at somebody else's home directory.
for required in \
    "User=${RUN_USER}" \
    "WorkingDirectory=${ROOT}" \
    "ExecStart=${PYTHON} -m uniden_r8.cli collect" \
    "ReadWritePaths=${STATE_DIR}"
do
    grep -qxF "$required" "$RENDERED" \
        || fail "templating failed: '$required' is not in the rendered unit.
    The unit template's key names have changed; update $0."
done

if [[ "$DRY_RUN" == 1 ]]; then
    say "== dry run: the unit that would be installed at $UNIT_DEST"
    say ""
    grep -v '^#' "$RENDERED" | grep -v '^$'
    say ""
    say "== nothing was changed"
    exit 0
fi

# ---------------------------------------------------------------------------
# Install.  Three privileged actions, named individually.
# ---------------------------------------------------------------------------

say "== installing (sudo)"
sudo install -m 0644 -o root -g root "$RENDERED" "$UNIT_DEST"
sudo systemctl daemon-reload

if [[ "$ENABLE" == 1 ]]; then
    sudo systemctl enable "$UNIT_NAME" >/dev/null
    say "   enabled at boot"
else
    say "   not enabled at boot (--no-enable)"
fi

# `restart` rather than `start`: idempotent, and it picks up a redeploy.  It
# also cleanly replaces a hand-started collector, because the new process takes
# the same single-instance lock.
sudo systemctl restart "$UNIT_NAME"

# ---------------------------------------------------------------------------
# Verify against the data, not against systemd.  `active (running)` only means
# the process has not exited; it says nothing about whether packets are
# arriving.  The counter does.
# ---------------------------------------------------------------------------

say "== verifying"

systemctl is-active --quiet "$UNIT_NAME" || {
    say ""
    systemctl status "$UNIT_NAME" --no-pager --lines=20 || true
    fail "the unit is not active"
}
say "   unit is active"

STATE_JSON="$STATE_DIR/state.json"
read_counter() {
    [[ -f "$STATE_JSON" ]] || { echo ""; return; }
    "$PYTHON" - "$STATE_JSON" <<'PY' 2>/dev/null || echo ""
import json, pathlib, sys
doc = json.loads(pathlib.Path(sys.argv[1]).read_text())
print(doc.get("counters", {}).get("telemetry_packets", 0), doc.get("collector", {}).get("status", "?"))
PY
}

first=""; last=""; status="?"
for _ in $(seq 1 20); do
    sleep 3
    reading="$(read_counter)"
    [[ -n "$reading" ]] || continue
    count="${reading%% *}"; status="${reading##* }"
    [[ -n "$first" ]] || first="$count"
    last="$count"
    [[ "$last" -gt "${first:-0}" ]] && break
done

if [[ -z "$last" ]]; then
    say "   no state document yet at $STATE_JSON"
    say "   the unit is running; give it a minute and check:"
    say "       journalctl -u $UNIT_NAME -n 40"
    exit 0
fi

say "   status: $status, telemetry packets: $last"

case "$status" in
    streaming)
        [[ "$last" -gt "${first:-0}" ]] \
            && say "   packets are arriving -- the detector link is live" \
            || say "   streaming, but the counter did not advance in the sample window"
        ;;
    obd-blocked)
        say "   the OBD guard is holding the link closed. That is the guard working,"
        say "   not a fault. Check: systemctl is-active hummer-rfcomm"
        ;;
    connecting|reconnecting)
        say "   waiting for the detector. If it is switched off, this is correct;"
        say "   the collector will connect on its own when it appears."
        ;;
esac

say ""
say "== installed"
say "   status:   systemctl status $UNIT_NAME"
say "   logs:     journalctl -u $UNIT_NAME -f"
say "   state:    jq . $STATE_JSON"
say "   stop:     sudo systemctl stop $UNIT_NAME"
say "   remove:   ./scripts/uninstall-service.sh"
