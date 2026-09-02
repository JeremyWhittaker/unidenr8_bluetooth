# Runbook

Operating procedure for the read-only Uniden R8 BLE work on the Hummer node.

Read `docs/SAFETY.md` first. The short version: never touch
`hummer-rfcomm.service` or `/dev/rfcomm0`, never `sudo`, never write an
application command to the detector, never publish an address.

```bash
# The node is reachable by a private VPN name.  This repository deliberately
# does not record it; set it in your shell and do not commit the value.
export PI_HOST="jeremy@<private-node-name>"
export UNIDEN_ROOT="/home/jeremy/unidenr8"
```

---

## Step 0 — the detector, before any Linux work

**Do this first.** If the firmware predates Bluetooth support, everything after
it is wasted effort.

### 0a. Read the firmware version off the detector

The R8's owner's manual (Issue 3, March 2024) documents exactly one version
screen, and it is *not* under a "System" menu — the R8 has no such submenu:

1. Press **MENU**.
2. Press **+** repeatedly to cycle until the screen reads
   **`S/W version / DSP Version / GPS Version`**.
3. Press **MENU** to display it.

Write down every number shown. On the R8w the equivalent screen also lists a
**BT/WiFi** firmware line; if the R8 shows one, that is a direct indication
Bluetooth firmware is present.

There is also a **`DB Ver`** screen (GPS on) for the database version.

### 0b. Look for the Bluetooth menu rows

While in the menu, check whether either of these exists:

* **`BT/WiFi`** — "Turns Bluetooth/WiFi on and off". Must be **On**.
* **`BT Pairing`** — the pairing screen.

**If neither appears, stop.** The firmware does not support Bluetooth and no
amount of Linux work will change that. Go to step 0c.

**If `BT/WiFi` appears but is Off**, set it to On. `BT Pairing` only appears
when it is.

### 0c. Update the firmware, if needed

Official route, from Uniden support article 153000192832. The R8 updates over
**USB** — it has no Wi-Fi and no over-the-air update; those are R8w features.

1. Go to <https://www.uniden.info/download/index.cfm?s=r8>.
2. Download the current **Uniden R Series Tool** (v2.25 Setup for Windows as
   of 2026-05-19; a Mac build, v2.22_MAC, also exists) and the current
   **Firmware Update** (V1.43, 2026-07-10) and **Database Update**.
3. On Windows, install `CP210x_Windows_Drivers` if the tool does not see the
   detector.
4. Run the setup wizard, accept the prompts.
5. Connect the R8 by the supplied USB cable.
6. Select the **UPDATE** tab, click **Start Update**.
7. **Wait for "Update Completed!" before unplugging.** The detector's display
   stays dark throughout — that port is data only, and a dark screen is not a
   failure.

Community reporting (Vortex Radar, rdforum — *not* an official statement) says
R8 firmware 1.28 does not work properly with R/Tach and 1.35 does. Uniden
publishes no minimum. Treat 1.35 as a floor to clear, not a specification, and
prefer the current release.

---

## Step 1 — node health, read-only

Nothing here changes anything.

```bash
ssh "$PI_HOST"

hostname; uptime; free -h; df -h /
python3 -VV
dpkg-query -W -f='${Package} ${Version}\n' bluez python3-venv
```

### The OBD invariants — check before and after every session

```bash
for unit in hummer-rfcomm hummer-btdiscover hummer-collector hummer-display; do
  printf '%-22s enabled=%-9s active=%-9s sub=%s\n' "$unit" \
    "$(systemctl is-enabled "$unit" 2>/dev/null || true)" \
    "$(systemctl is-active  "$unit" 2>/dev/null || true)" \
    "$(systemctl show -p SubState --value "$unit" 2>/dev/null || true)"
done

rfcomm                      # binding state; do not run `rfcomm release`
stat -c '%n %F mode=%a %U:%G' /dev/rfcomm0
systemctl --failed --no-pager
```

Expected, and unchanged by anything in this project:

| Unit | enabled | active | sub |
|---|---|---|---|
| `hummer-rfcomm` | enabled | active | exited |
| `hummer-display` | enabled | active | running |
| `hummer-btdiscover` | disabled | inactive | dead |
| `hummer-collector` | disabled | inactive | dead |

`rfcomm` must show `rfcomm0:` bound on channel 1 with state `closed`.
**`closed` is correct** for a `rfcomm bind` with nothing holding the port open;
it means bound-and-idle, not broken.

The controller must be `Powered: yes` and `Discovering: no`:

```bash
bluetoothctl show | grep -E 'Powered|Discovering|Name'
```

`bluetoothctl devices Paired` should list the OBDLink and nothing else, until
the detector is deliberately paired in a later phase.

---

## Step 2 — deploy

From the workstation:

```bash
HOST="$PI_HOST" ./scripts/deploy.sh
```

It copies `src/`, `tests/`, `docs/`, `scripts/` and packaging to
`/home/jeremy/unidenr8`. It installs no systemd unit, enables nothing, starts
nothing, runs no `sudo`, and never touches `/home/jeremy/hummer-obd`.

Then, on the node:

```bash
cd "$UNIDEN_ROOT"
python3 -m venv .venv          # PEP 668 marks the system Python externally
                               # managed; a venv is mandatory, not a style choice
.venv/bin/pip install bleak pytest
```

`bleak` is an *optional* dependency here. The safety gate, redaction and
classification import and test without it; the scan, identity and live
operations load it only when they use the radio.

---

## Step 3 — prove the read-only properties on the node

Run this on the node **before** anything goes near the detector.

```bash
cd "$UNIDEN_ROOT"
.venv/bin/python -m pytest -q
.venv/bin/python -m uniden_r8.cli selftest
.venv/bin/python -m uniden_r8.cli plan
```

`selftest` must print **"All read-only properties hold."** It checks that the
gate refuses the command characteristic, that all seven known R/Tach commands
are refused, that no module in the package references a characteristic-writing
bleak API (by AST, not by grep), that the private store is `0700`/`0600`, and
that the catalogue is self-consistent.

`plan` prints every operation the project is permitted to perform and every
command it refuses. It needs no radio and no `bleak`.

**If `selftest` fails, stop.** Do not scan.

---

## Step 4 — the bounded scan

This step uses the radio for **bounded advertisement-only discovery**: it
collects advertisements and stops. It does not connect, pair, or read a
characteristic. It is not radio-silent — BlueZ scans actively by default, so it
answers advertisements with scan requests — but nothing it sends can change the
detector's state.

### Arm the detector first

Upstream observed that its **R8w** advertises only in pairing mode when
unpaired, or while paired and idle. This R8's advertising behaviour outside
pairing mode was not established because it was re-armed during discovery.
Re-arming is therefore the reliable way to make a discovery scan useful; a
connected phone can still make the detector disappear from the scan.

On the detector: **MENU → cycle to `BT Pairing` → MENU**. The display shows a
pairing message. The window closes on its own after a while, so start the scan
promptly and expect to re-arm at least once.

Also turn Bluetooth **off on any phone** that has been paired with the
detector. A phone reconnects in the background without asking, and the detector
talks to one thing at a time — the symptom is an empty scan for a completely
different reason.

### Run it

```bash
cd "$UNIDEN_ROOT"
.venv/bin/python -m uniden_r8.cli scan --seconds 25 --save scan-01.json
```

The window is clamped to 3–60 s whatever is asked for, and a hung scanner is
cancelled at window + 5 s. Output is sanitized: tokens, never addresses.
`--save` writes the same sanitized report into `.private/`, `0600`.

### Reading the result

* **`strong`** — an R-series name. This is the detector.
* **`possible`** — the name mentions Uniden. Worth a second scan.
* **`unnamed`, random-static** — a hint, not an answer. Upstream's R8w uses a
  random static address, so an unnamed one is worth noting; so do plenty of
  unrelated devices.
* **No candidates** — the expected result if the pairing window closed, a
  phone grabbed the link, or the firmware has no Bluetooth. Re-arm and repeat
  once before concluding anything.

Whatever the result, record it as what was seen. An empty scan is a finding.

### After the scan

Re-run the OBD invariant checks from step 1. They must be identical.

---

## Step 5 — pull live data

Only once the detector is bonded (see the evidence ledger; it already is).

```bash
cd "$UNIDEN_ROOT"
.venv/bin/python -m uniden_r8.cli live --seconds 30 --save live-session.json
```

It resolves the detector from BlueZ's existing bond state — no discovery, and
the address is never written down — connects, checks the compatibility gate,
GATT-reads telemetry and alert once, subscribes to both, collects for the
window, and tears the link down.

The window is clamped to 5–120 s. There is no daemon: this runs once and exits.

Expected output on a parked vehicle with no radar around:

```
packets: 30 telemetry, 0 alert

  13.6 V     GPS locked

alerts: 0
  clear
```

Reading it:

* **`packets: N telemetry`** — roughly one per second. Zero means the link came
  up but nothing streamed; check the detector is on and no phone has taken it.
* **`M telemetry unparsed`** — appears only when packets did not fit the
  expected shape. Raw bytes are in a timestamped `.private/live-raw-*.json`;
  that is a
  firmware-format change worth investigating, not a crash.
* **`Device does not expose the required attributes`** — the compatibility gate
  refused. Nothing was read. Re-run `identity` and compare the service list.
* **`alerts: 0 / clear`** — no detections. This is the normal state.

Raw payloads land in `.private/`, `0600`. Nothing printed contains an address,
and heading, speed and altitude are deliberately absent from the output.

Afterwards, re-run the OBD invariant checks from step 1.

## Step 6 — stop

The live path runs once and exits. Turning it into a background collector — a
systemd unit, a database, a display — is a separate piece of work and needs its
own review: a service holding the BLE link continuously has a very different
relationship with the shared radio than a 30-second window does.

---

## Privileged commands, if they are ever needed

This project runs no `sudo`. Discovery, pairing, identity and the bounded live
capture all completed as the ordinary `jeremy` user.

If a later phase needs one, it is recorded here for Jeremy to run, and is not
executed by any agent:

| Situation | Exact command | Why |
|---|---|---|
| `rfkill list bluetooth` shows soft-blocked | `sudo rfkill unblock bluetooth` | Not currently blocked on this node — `hci0` is `UP RUNNING`. |
| `bluetoothctl pair` fails `AccessDenied` or "Rejected send message" | `sudo usermod -aG bluetooth $USER`, then log out and back in | Only if a `bluetooth` group exists and gates pairing. This node has no such group, and scanning works without it. |

Neither is needed today.

---

## Troubleshooting

**Empty scan.** Almost always the detector: not armed, window expired, phone
holding the link, or no Bluetooth firmware. Work through step 4's arming notes
before suspecting Linux.

**`bleak is not installed`.** The venv is missing or not the one being used.
`plan` and `selftest` still work — they need no radio.

**`selftest` reports a transmit path.** Someone added a write. Stop and revert;
do not go near the detector.

**A scan seems to affect the OBD link.** It should not — a scan changes no bond
and no binding — but if telemetry looks degraded, simply stop scanning: the
window is bounded and self-terminating. Do not restart `bluetooth.service` and
do not touch `hummer-rfcomm.service`; escalate instead.
