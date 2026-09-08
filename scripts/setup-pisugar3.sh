#!/bin/bash
# Install and configure the PiSugar power manager for a PiSugar 3.
#
# Run this from a workstation AFTER the PiSugar 3 hardware is physically
# installed.  It refuses to run against PiSugar 2 hardware, deliberately.
#
# WHY THIS EXISTS RATHER THAN `curl | bash`
#
# PiSugar's own installer is a shell script fetched over the network and piped
# to a shell.  The packages it ends up installing are published as signed
# release artifacts, so this uses those directly: they are staged on the node
# ahead of time, their checksums recorded, and installed with the model
# **preseeded** rather than answered by a prompt.
#
# THE MODEL TRAP
#
# `pisugar-poweroff`'s postinst does:
#
#     db_get pisugar-poweroff/model
#     OPTS="$OPTS --model '$RET'"
#     echo "OPTS=$OPTS" > /etc/default/pisugar-poweroff
#
# The value comes from a debconf prompt.  On a non-interactive install the
# prompt does not run, and the shipped default is used -- which is how a node
# ends up sending commands for the wrong PMIC and failing silently.  This
# script sets both packages' model with `debconf-set-selections` first, so the
# answer is chosen rather than defaulted.
#
# WHAT IT DOES NOT DO
#
# It does not switch `hummer-battery` from `stop-collector` to `poweroff`.
# That belongs to the sibling hummer-obd project, and more importantly it must
# not happen until the wake path has been *bench tested* -- see
# docs/RUNBOOK.md, "Swapping to a PiSugar 3".  Halting a node whose wake path
# is unproven is how this project lost two drives.
#
# HOST is read from the environment or .private/pi-host.txt, never printed.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/.." && pwd)"
STAGING="${STAGING:-\$HOME/pisugar3-staging}"

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    cat <<'USAGE'
usage: setup-pisugar3.sh [--force-model "PiSugar 3"]

Installs pisugar-server and pisugar-poweroff from packages staged on the node,
with the model preseeded, then enables auto power-on and verifies.

Refuses to run unless PiSugar 3 hardware is detected. Run it only after the
hardware swap. See docs/RUNBOOK.md, "Swapping to a PiSugar 3".

  HOST=user@node   required; also read from .private/pi-host.txt
  STAGING=path     where the .deb files live (default ~/pisugar3-staging)
USAGE
    exit 0
fi

MODEL="PiSugar 3"
if [ "${1:-}" = "--force-model" ]; then
    MODEL="${2:?--force-model needs a value}"
fi

if [ -z "${HOST:-}" ] && [ -r "$REPO_ROOT/.private/pi-host.txt" ]; then
    HOST="$(tr -d '[:space:]' < "$REPO_ROOT/.private/pi-host.txt")"
fi
: "${HOST:?set HOST=user@node, or create .private/pi-host.txt (never commit it)}"

say() { echo "# $*"; }

say "checking the node is reachable"
ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" true 2>/dev/null || {
    echo "cannot reach the node over SSH." >&2; exit 1; }

# ------------------------------------------------------- 1. which pack is fitted

say "identifying the fitted pack over I2C"
DETECTED="$(ssh -o BatchMode=yes "$HOST" 'python3 -' <<'REMOTE'
"""Identify the PiSugar generation by which I2C address answers.

Reads only. A register read is a one-byte write of the register number
followed by a one-byte read, which is what every driver here does; it changes
nothing on either chip.
"""
import fcntl, os

I2C_SLAVE = 0x0703
CANDIDATES = ((0x57, "PiSugar 3"), (0x75, "PiSugar 2"))


def answers(bus: int, address: int) -> bool:
    path = "/dev/i2c-%d" % bus
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.ioctl(fd, I2C_SLAVE, address)
        os.write(fd, bytes([0x00]))
        os.read(fd, 1)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


found = []
for bus in (1, 0):
    if not os.path.exists("/dev/i2c-%d" % bus):
        continue
    for address, name in CANDIDATES:
        if answers(bus, address) and name not in found:
            found.append(name)
print("DETECTED " + (",".join(found) if found else "NONE"))
REMOTE
)"
say "$DETECTED"

case "$DETECTED" in
    *"PiSugar 3"*) say "PiSugar 3 present, continuing" ;;
    *"PiSugar 2"*)
        echo >&2
        echo "PiSugar 2 is still fitted. Nothing was installed." >&2
        echo >&2
        echo "This is refused on purpose. pisugar-server's IP5209 driver writes" >&2
        echo "registers on init and disables the chip's own light-load auto" >&2
        echo "shutdown when auto_power_on is set -- changing the behaviour of the" >&2
        echo "pack that is currently keeping this node alive. Swap the hardware" >&2
        echo "first; see docs/RUNBOOK.md, \"Swapping to a PiSugar 3\"." >&2
        exit 1 ;;
    *)
        echo >&2
        echo "No PiSugar responded on I2C. Is the pack seated and its switch on?" >&2
        exit 1 ;;
esac

# --------------------------------------------------------- 2. staged packages

say "checking the staged packages"
ssh -o BatchMode=yes "$HOST" "
set -e
cd $STAGING 2>/dev/null || { echo 'staging directory missing' >&2; exit 1; }
for f in pisugar-server_*_arm64.deb pisugar-poweroff_*_arm64.deb; do
    [ -s \"\$f\" ] || { echo \"missing: \$f\" >&2; exit 1; }
    dpkg-deb -f \"\$f\" Package Version | tr '\n' ' '; echo
done
sha256sum *.deb
"

# ------------------------------------------- 3. install, with the model chosen

say "installing with model preseeded as '$MODEL' (sudo will prompt)"
ssh -t "$HOST" "
set -e
cd $STAGING
echo 'pisugar-server pisugar-server/model select $MODEL' | sudo debconf-set-selections
echo 'pisugar-poweroff pisugar-poweroff/model select $MODEL' | sudo debconf-set-selections
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ./pisugar-server_*_arm64.deb ./pisugar-poweroff_*_arm64.deb
"

# ------------------------------------------------------ 4. the model that landed

say "confirming the model that actually landed in the unit files"
ssh -o BatchMode=yes "$HOST" '
echo "  /etc/default/pisugar-server   : $(grep -o "\-\-model .[^\"]*" /etc/default/pisugar-server 2>/dev/null || echo "(none)")"
echo "  /etc/default/pisugar-poweroff : $(grep -o "\-\-model .[^\"]*" /etc/default/pisugar-poweroff 2>/dev/null || echo "(none)")"
'

# ------------------------------------------------------------ 5. auto power-on

say "enabling auto power-on"
ssh -o BatchMode=yes "$HOST" '
sleep 3
echo "set_auto_power_on true" | nc -q 0 127.0.0.1 8423 2>/dev/null | tr -d "\r" | sed "s/^/  set: /"
sleep 1
echo "get auto_power_on" | nc -q 0 127.0.0.1 8423 2>/dev/null | tr -d "\r" | sed "s/^/  get: /"
echo "get model" | nc -q 0 127.0.0.1 8423 2>/dev/null | tr -d "\r" | sed "s/^/  " "/"
'

# ----------------------------------------------------------------- 6. verify

say "state"
ssh -o BatchMode=yes "$HOST" '
for u in pisugar-server pisugar-poweroff; do
  printf "  %-20s active=%-8s enabled=%s\n" "$u" "$(systemctl is-active $u 2>/dev/null)" "$(systemctl is-enabled $u 2>/dev/null)"
done
echo "  hummer-battery on-low : $(grep -o "on-low [a-z-]*" /etc/default/hummer-battery 2>/dev/null || echo unknown)"
'

cat <<'NEXT'

# Installed. NOT yet switched to graceful shutdown -- on purpose.
#
# Before changing `hummer-battery` to `--on-low poweroff`, bench test the wake
# path, because a halt with an unproven wake path is what cost this project two
# drives. The procedure is in docs/RUNBOOK.md, "Swapping to a PiSugar 3":
#
#   1. `sudo systemctl poweroff` on the node.
#   2. Confirm the Pi's LED goes fully dark AND the 5V rail actually drops --
#      a halted Pi that is still powered will never wake, and looks identical.
#   3. Without touching the PiSugar switch, restore external USB power.
#   4. The Pi should boot on its own. If it does not, stop: do not enable
#      poweroff mode.
#
# Only after that passes, on the node:
#
#   sudo sed -i 's/--on-low stop-collector/--on-low poweroff/' /etc/default/hummer-battery
#   sudo systemctl restart hummer-battery
NEXT
