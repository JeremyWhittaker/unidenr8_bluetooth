#!/bin/bash
# Measure how well the GNSS receiver can actually see the sky.
#
# Run this after moving the antenna.  "Does it have a fix?" is the wrong
# question: a receiver tucked under trim will hold a fix perfectly well on a
# parked car and then lose it under the first overpass.  What matters is the
# margin -- how many satellites it is *using* above the four a 3D fix needs,
# and how lopsided the error estimate is.
#
# Reports no latitude or longitude: satellite counts, signal strengths and
# error estimates only.
#
# HOST is read from the environment or .private/pi-host.txt, and never printed.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/.." && pwd)"

SECONDS_TO_SAMPLE="${SECS:-90}"
PORT="${PORT:-2947}"

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    cat <<'USAGE'
usage: gnss-quality.sh

  SECS=90        how long to sample (default 90)
  PORT=2947      gpsd port (default 2947)
  HOST=user@node required; also read from .private/pi-host.txt

Reads gpsd and prints fix stability, satellite counts, C/N0 and error
estimates. Compare "satellites USED" against 4 -- that is the floor for a
3D fix, and everything above it is margin for a bridge, a tree or a garage.
USAGE
    exit 0
fi

if [ -z "${HOST:-}" ] && [ -r "$REPO_ROOT/.private/pi-host.txt" ]; then
    HOST="$(tr -d '[:space:]' < "$REPO_ROOT/.private/pi-host.txt")"
fi
: "${HOST:?set HOST=user@node, or create .private/pi-host.txt (never commit it)}"

echo "# sampling ${SECONDS_TO_SAMPLE}s of gpsd on port ${PORT}"
ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" \
    "PORT=$PORT SECS=$SECONDS_TO_SAMPLE python3 -" <<'REMOTE'
"""GNSS signal quality over a sampling window.

Prints no latitude or longitude: counts, signal strengths and error
estimates, which is everything needed to judge a mounting position.
"""
import json, os, socket, statistics as st, time

port = int(os.environ.get("PORT", "2947"))
secs = float(os.environ.get("SECS", "90"))

try:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
except OSError as exc:
    print("  cannot reach gpsd on %d: %s" % (port, exc))
    raise SystemExit(1)

sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
buf = b""
modes, used_counts, seen_counts, snr_used = [], [], [], []
epx, epy, epv = [], [], []
sky_samples = 0
end = time.time() + secs
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
        if kind == "TPV":
            modes.append(message.get("mode") or 0)
            for key, sink in (("epx", epx), ("epy", epy), ("epv", epv)):
                if isinstance(message.get(key), (int, float)):
                    sink.append(message[key])
        elif kind == "SKY":
            sats = message.get("satellites")
            # An empty or absent list is not a measurement. Counting it as
            # "zero satellites" reads as a dead antenna on a working one.
            if not isinstance(sats, list) or not sats:
                continue
            sky_samples += 1
            used = [s for s in sats if s.get("used")]
            seen_counts.append(len(sats))
            used_counts.append(len(used))
            for sat in used:
                if isinstance(sat.get("ss"), (int, float)) and sat["ss"] > 0:
                    snr_used.append(sat["ss"])
sock.close()


def summary(name, values, unit=""):
    if not values:
        print("  %-22s no samples" % name)
        return
    print("  %-22s min %5.1f  median %5.1f  max %5.1f  n=%d%s"
          % (name, min(values), st.median(values), max(values), len(values), unit))


if modes:
    fixes = sum(1 for m in modes if m >= 2)
    print("  %-22s %d" % ("TPV messages", len(modes)))
    print("  %-22s %d (%.0f%%)" % ("had a fix", fixes, 100 * fixes / len(modes)))
    print("  %-22s %d" % ("3D fixes", sum(1 for m in modes if m == 3)))
else:
    print("  no TPV messages at all -- gpsd has no device, or no receiver")
summary("satellites seen", seen_counts)
summary("satellites USED", used_counts)
summary("C/N0 of used sats", snr_used, "  dB-Hz")
summary("epx (east-west, m)", epx)
summary("epy (north-south, m)", epy)
summary("est. vert error (m)", epv)

print()
if used_counts:
    spare = st.median(used_counts) - 4
    print("  MARGIN: median %.0f satellites used, %.0f above the 4 a 3D fix needs."
          % (st.median(used_counts), spare))
    if spare <= 1:
        print("  That is thin. A fix this close to the floor holds on a parked car")
        print("  and drops under a bridge, a tree line or a garage. Expect gaps in")
        print("  motion, and re-check with a drive before trusting the positions.")
    else:
        print("  Comfortable. Losing a satellite or two still leaves a 3D fix.")
if epx and epy:
    ratio = max(st.median(epx), st.median(epy)) / max(1e-9, min(st.median(epx), st.median(epy)))
    if ratio >= 2.0:
        print()
        print("  LOPSIDED: one axis is %.1fx worse than the other. That is the shape"
              % ratio)
        print("  of a partly blocked sky -- satellites in one direction only, which is")
        print("  what a puck against a pillar or under a metal panel produces.")
REMOTE
