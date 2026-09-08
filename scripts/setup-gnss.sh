#!/bin/bash
# Make the node's GNSS receiver durable, and point the collector at it.
#
# Run this from a workstation; it reaches the node over SSH (a Tailscale name
# works, and is the expected case).  It is safe to re-run: every step checks
# the state it wants before changing anything.
#
# WHAT IT FIXES
#
# Debian ships /etc/default/gpsd with DEVICES="" and USBAUTO="true", which
# registers a receiver through a udev *hotplug* event.  Plug the receiver in
# while gpsd is not running -- the normal case, because the unit is
# socket-activated and idle until a client connects -- and nothing ever
# registers it.  gpsd then accepts connections and serves no device, which from
# the client side is indistinguishable from a receiver that cannot see the sky.
#
# THE ORDERING MATTERS
#
# A node may already be running a private gpsd on a spare port as a stopgap
# (see docs/RUNBOOK.md).  That instance holds the serial device, and it works.
# This script therefore proves the *system* gpsd has the device before it
# stops the stopgap or moves the collector onto it.  Doing it the other way
# round leaves a vehicle with no position feed and nothing saying so.
#
# HOST is deliberately not defaulted to a name or address: the node is
# reachable by a private VPN name that this repository must not record.  It is
# read from the environment, or from .private/pi-host.txt, which is git-ignored.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/.." && pwd)"

usage() {
    cat <<'USAGE'
usage: setup-gnss.sh [--device PATH] [--coordinates|--no-coordinates] [--port N]

  --device PATH       serial device to hand gpsd (default: auto-detect)
  --coordinates       record latitude and longitude in the history
  --no-coordinates    do not record them
                      (default: leave the current setting alone)
  --port N            port the collector should use (default: 2947, the
                      system gpsd)

  HOST=user@node      required; also read from .private/pi-host.txt
USAGE
}

DEVICE=""
COORDINATES="keep"
TARGET_PORT="2947"
STOPGAP_PORT="2948"

while [ $# -gt 0 ]; do
    case "$1" in
        --device) DEVICE="${2:?--device needs a path}"; shift 2 ;;
        --coordinates) COORDINATES="true"; shift ;;
        --no-coordinates) COORDINATES="false"; shift ;;
        --port) TARGET_PORT="${2:?--port needs a number}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "${HOST:-}" ] && [ -r "$REPO_ROOT/.private/pi-host.txt" ]; then
    HOST="$(tr -d '[:space:]' < "$REPO_ROOT/.private/pi-host.txt")"
fi
: "${HOST:?set HOST=user@node, or create .private/pi-host.txt (never commit it)}"

# The node's name is a private VPN address.  It is used, never printed.
say() { echo "# $*"; }

say "checking the node is reachable"
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" true 2>/dev/null; then
    echo "cannot reach the node over SSH." >&2
    echo "If it is on Tailscale, check 'tailscale status' on both ends." >&2
    exit 1
fi

# --------------------------------------------------------------- 1. the device

if [ -z "$DEVICE" ]; then
    say "looking for a GNSS receiver"
    # `find` rather than a glob: a glob that matches nothing expands to itself
    # under this shell, and the node may have neither kind of device node.
    DEVICE="$(ssh -o BatchMode=yes "$HOST" \
        'find /dev -maxdepth 1 \( -name "ttyUSB*" -o -name "ttyACM*" \) 2>/dev/null | sort | sed -n 1p')"
fi
if [ -z "$DEVICE" ]; then
    echo "no /dev/ttyUSB* or /dev/ttyACM* on the node." >&2
    echo "Is the receiver plugged in? Check 'lsusb' there." >&2
    exit 1
fi
say "using device: $DEVICE"

# ------------------------------------------------- 2. make gpsd own it, durably

say "writing /etc/default/gpsd (sudo on the node will prompt)"
# -t so sudo can ask for a password on this terminal.  The remote command is
# deliberately short and quoted once: this is the only privileged step.
ssh -t "$HOST" "
set -e
sudo cp -n /etc/default/gpsd /etc/default/gpsd.bak 2>/dev/null || true
sudo sed -i 's|^DEVICES=.*|DEVICES=\"$DEVICE\"|; s|^GPSD_OPTIONS=.*|GPSD_OPTIONS=\"-n\"|' /etc/default/gpsd
grep -c . /etc/default/gpsd >/dev/null
sudo systemctl enable gpsd
sudo systemctl restart gpsd
"

# ------------------------------------------- 3. prove it before tearing down

say "checking the system gpsd actually has the device"
PROBE_OUTPUT="$(ssh -o BatchMode=yes "$HOST" "TARGET_PORT=$TARGET_PORT python3 -" <<'REMOTE'
"""Report whether gpsd has a device, and whether it has a fix.

Prints no latitude or longitude: a device list and a fix mode, nothing else.
"""
import json, os, socket, sys, time

port = int(os.environ.get("TARGET_PORT", "2947"))
try:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
except OSError as exc:
    print("PROBE unreachable %s" % exc)
    sys.exit(0)

sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
buf, devices, mode = b"", [], None
end = time.time() + 20
while time.time() < end:
    try:
        chunk = sock.recv(4096)
    except socket.timeout:
        break
    if not chunk:
        break
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        kind = message.get("class")
        if kind == "DEVICES":
            devices = [d.get("path") for d in message.get("devices", [])]
        elif kind == "DEVICE" and message.get("path"):
            devices.append(message["path"])
        elif kind == "TPV":
            mode = message.get("mode")
    if devices and mode and mode >= 2:
        break
sock.close()
print("PROBE devices=%s mode=%s" % (",".join(p for p in devices if p) or "NONE", mode))
REMOTE
)"
say "$PROBE_OUTPUT"

case "$PROBE_OUTPUT" in
    *devices=NONE*|*unreachable*)
        echo >&2
        echo "The system gpsd still has no device, so nothing was switched over." >&2
        echo "The node is left exactly as it was -- if a stopgap gpsd was" >&2
        echo "serving the receiver on port $STOPGAP_PORT, it still is." >&2
        echo >&2
        echo "Read the receiver directly to tell a gpsd fault from a dead one:" >&2
        echo "  python3 -c \"import serial;p=serial.Serial('$DEVICE',4800,timeout=2);" >&2
        echo "  print([p.readline()[:6] for _ in range(5)])\"" >&2
        exit 1
        ;;
esac

case "$PROBE_OUTPUT" in
    *mode=None*|*mode=0*|*mode=1*)
        say "note: gpsd has the device but no fix yet -- normal indoors, or on a"
        say "      cold start. The switch below is still safe: the device is what"
        say "      matters, and a fix will follow when the receiver sees sky."
        ;;
esac

# ------------------------------- 4. retire the stopgap and move the collector

say "retiring any stopgap gpsd on port $STOPGAP_PORT"
ssh -o BatchMode=yes "$HOST" "pkill -f 'gpsd .*-S $STOPGAP_PORT' || true"

say "pointing the collector at port $TARGET_PORT (coordinates: $COORDINATES)"
ssh -o BatchMode=yes "$HOST" \
    "TARGET_PORT=$TARGET_PORT COORDINATES=$COORDINATES python3 -" <<'REMOTE'
"""Edit only the [gnss] table, and only the keys asked for."""
import os, pathlib, re, time

port = os.environ["TARGET_PORT"]
coordinates = os.environ["COORDINATES"]
config = pathlib.Path.home() / "unidenr8" / "unidenr8.toml"
text = config.read_text(encoding="utf-8")

if "[gnss]" not in text:
    # No section at all is the silent failure this whole exercise exists to
    # prevent: the client never starts and gnss_fixes simply stays empty.
    text = text.rstrip("\n") + (
        "\n\n[gnss]\nenabled = true\nhost = \"127.0.0.1\"\n"
        "port = %s\nrecord_coordinates = false\nstale_after_seconds = 5\n" % port
    )

config.with_suffix(".toml.bak.%d" % time.time()).write_text(text, encoding="utf-8")

# Bounded to the [gnss] table: `port` also appears under [mqtt].
text = re.sub(r"(\[gnss\][^\[]*?)enabled\s*=\s*\w+",
              r"\g<1>enabled = true", text, flags=re.S)
text = re.sub(r"(\[gnss\][^\[]*?)port\s*=\s*\d+",
              r"\g<1>port = %s" % port, text, flags=re.S)
if coordinates in ("true", "false"):
    text = re.sub(r"(\[gnss\][^\[]*?)record_coordinates\s*=\s*\w+",
                  r"\g<1>record_coordinates = %s" % coordinates, text, flags=re.S)

config.write_text(text, encoding="utf-8")
section = text.split("[gnss]", 1)[1].split("[", 1)[0]
print("[gnss]" + section.rstrip())
REMOTE

say "restarting the collector"
ssh -o BatchMode=yes "$HOST" "sudo systemctl restart unidenr8-collector"
sleep 8

# ------------------------------------------------------------- 5. what we got

say "verifying"
ssh -o BatchMode=yes "$HOST" "TARGET_PORT=$TARGET_PORT bash -s" <<'REMOTE'
set -u
echo "  gpsd active   : $(systemctl is-active gpsd)"
echo "  gpsd at boot  : $(systemctl is-enabled gpsd 2>/dev/null || echo unknown)"
echo "  collector     : $(systemctl is-active unidenr8-collector)"
echo "  stopgap gpsd  : $(pgrep -c -f 'gpsd .*-S 2948' || true) process(es)"
python3 - <<'PY'
import os, sqlite3
db = os.path.expanduser("~/unidenr8/.state/history.db")
try:
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    total, located = con.execute(
        "SELECT COUNT(*), COUNT(lat) FROM gnss_fixes").fetchone()
    print("  gnss_fixes    : %d rows, %d carrying a position" % (total, located))
except sqlite3.Error as exc:
    print("  gnss_fixes    : unreadable (%s)" % exc)
PY
REMOTE

say "done."
say "GNSS rows are only written while the detector is connected -- the fix"
say "recorder runs inside the session pump -- so with the vehicle off you will"
say "see no new rows. That is expected, not a fault."
