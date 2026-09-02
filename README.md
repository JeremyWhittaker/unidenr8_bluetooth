# Uniden R8 BLE Reader for Raspberry Pi

A receive-only Python integration for connecting a Bluetooth-capable
[Uniden R8](https://www.uniden.info/download/index.cfm?s=r8) radar detector to
Linux with Bluetooth Low Energy (BLE).

The project can discover and pair an R8, read its model and firmware, receive
live detector telemetry, observe the alert characteristic, and publish a small
sanitized JSON state document for local applications. It uses
[Bleak](https://github.com/hbldh/bleak) and BlueZ; it does **not** use RFCOMM
for the detector and contains no path for sending Uniden mute, mark, or
settings commands.

> [!IMPORTANT]
> This is a functional research integration, not yet a production radar-alert
> system. Telemetry is verified on a real non-W R8. The active-alert parser is
> implemented from the R8w protocol but still needs validation against a real
> detection on this R8. The optional background service is intentionally not
> installed or enabled automatically.

## What this gives you

Today, the project can:

- connect a Raspberry Pi or other BlueZ Linux host to an already paired R8;
- report detector voltage and whether its GPS has a fix, roughly once per
  second;
- report the observed clear/no-active-alert state;
- read the detector's standard Device Information values;
- run a bounded diagnostic capture or an OBD-aware background collector;
- publish identifier-free state for a display, logger, dashboard, MQTT bridge,
  or another local application; and
- coexist with an existing OBDLink MX+ RFCOMM binding without opening,
  releasing, or reconfiguring it.

It does **not** currently provide:

- latitude or longitude from the live R8 stream;
- a finished trip/GPS tracker;
- a field-validated active radar event feed;
- a real-time graphical or mobile interface;
- detector control, mute, user marks, settings changes, or firmware updates;
  or
- automatic service installation or boot enablement.

## Capability matrix

| Capability | Status | What is available |
|---|---|---|
| BLE advertisement discovery | **Verified** | Bounded scan with sanitized names and tokens; no connection. |
| Pairing | **Verified** | Explicitly confirmed BlueZ bond; detector is left untrusted and disconnected. |
| Model and firmware identity | **Verified** | Standard Device Information reads, with no vendor command. |
| Live telemetry | **Verified** | Approximately one packet per second; 31/31 packets parsed in the first hardware capture and 293/293 in a five-minute trial. |
| Detector/vehicle voltage | **Verified** | Published as a numeric voltage value. |
| GPS fix | **Verified** | Published as locked/not locked. |
| GPS coordinates | **Not available** | The live telemetry used here does not provide latitude/longitude. Saved POI coordinates are deliberately not read. |
| Heading, speed, altitude | **Not exposed** | The upstream format suggests these fields, but this project does not parse or publish them. They require separate validation before use. |
| No-active-alert state | **Verified** | The R8 returned an all-clear alert packet during hardware testing. |
| Band, strength, frequency, direction, mute state | **Implemented; field validation pending** | Parsed conservatively from the documented R8w alert layout. No real active alert has yet been captured from this R8. |
| POI warning flag | **Implemented; positive case unverified** | A boolean may be published; POI records and coordinates are never read. |
| Detector commands | **Intentionally unsupported** | No application-characteristic write path exists. |
| Sanitized state feed | **Verified** | Atomic schema-1 JSON with health, freshness, telemetry, counters, and recognized alerts. |
| Continuous collector | **Implemented; opt-in** | Designed for the Hummer node and guarded by its OBDLink health. No service is installed automatically. |
| Hummer e-paper status | **Implemented** | Shows connection/voltage/GPS/clear status. Its five-minute refresh makes it a health display, not a live radar UI. |
| OBD coexistence | **Partially verified** | The RFCOMM binding remained healthy throughout a parked five-minute BLE trial. Coexistence during active OBD polling and a full drive remains to be tested. |

## How it fits together

```mermaid
flowchart LR
    R8[Uniden R8] -->|BLE / GATT| BLE[Bleak + BlueZ]
    BLE --> LIVE[bounded live command]
    BLE --> COLLECTOR[optional collector]
    COLLECTOR --> STATE[.state/state.json]
    STATE --> APPS[display / logger / dashboard]

    OBD[OBDLink MX+] -->|Bluetooth Classic / SPP| RFCOMM[/dev/rfcomm0]
    COLLECTOR -. read-only health queries .-> RFCOMM
```

The R8 and OBDLink share one Bluetooth controller, but they remain separate
transports. The R8 uses BLE/GATT. The OBDLink remains the primary Bluetooth
Classic device bound to `/dev/rfcomm0`.

## Tested baseline

This repository was validated with:

- a non-W Uniden R8 running firmware 1.43;
- software revision
  `R8/143/113/126/107/20260702/999/999/113`;
- Device Information values `BTM10` / `ATTOWAVE`;
- a Raspberry Pi Zero 2 W running BlueZ and Python 3.11+; and
- an OBDLink MX+ already managed by a separate
  `hummer-rfcomm.service`.

The GATT UUID catalogue originated with
[AegisX86/UnidenR8wlink](https://github.com/AegisX86/UnidenR8wlink). Every
required vendor UUID and the seven-field telemetry shape were then checked
against the real R8. Other models and firmware versions should be treated as
unverified until their identity, attribute table, and payloads are observed.

## Quick start

### 1. Prepare the Linux host

On Raspberry Pi OS or another Debian-based BlueZ system:

```bash
sudo apt update
sudo apt install -y \
  bluetooth \
  bluez \
  rfkill \
  python3 \
  python3-venv \
  python3-pip \
  git \
  jq

sudo rfkill unblock bluetooth
sudo systemctl enable --now bluetooth
```

These are host-setup commands. The Python application itself runs as an
ordinary user and never invokes `sudo`.

### 2. Clone and install

```bash
git clone https://github.com/JeremyWhittaker/unidenr8_bluetooth.git
cd unidenr8_bluetooth

python3 -m venv .venv
.venv/bin/pip install -e ".[ble,dev]"
```

Python 3.11 or newer is required. Use the virtual environment on modern
Debian/Raspberry Pi OS installations; the system Python is normally marked as
externally managed.

### 3. Check the detector firmware

On the R8:

1. Press **MENU**.
2. Press **+** until
   **S/W version / DSP Version / GPS Version** appears.
3. Press **MENU** to display the values.
4. Confirm that **BT/WiFi** is enabled and **BT Pairing** is present.

Uniden added R/Tach support to the R8 in firmware 1.41. This project was tested
with 1.43. If the Bluetooth menu is absent, update the detector over USB with
the current **Uniden R Series Tool** from the
[official R8 download page](https://www.uniden.info/download/index.cfm?s=r8)
before debugging Linux.

### 4. Prove the local safety checks

Run these before using the radio:

```bash
.venv/bin/uniden-r8 plan
.venv/bin/uniden-r8 selftest
```

`selftest` must end with:

```text
All read-only properties hold.
```

It checks the UUID gates, refuses every known R/Tach command, audits the Python
AST for a Bleak application-write API, verifies private-store permissions, and
checks catalogue consistency.

### 5. Discover and pair the R8

The detector generally accepts only one BLE client. Disable Bluetooth on any
phone that normally connects to it.

On the detector, select **MENU → BT Pairing → MENU**, then promptly run the
optional discovery check:

```bash
.venv/bin/uniden-r8 scan --seconds 25
```

The pairing command performs its own scan. Re-enter **BT Pairing** immediately
before running it so the detector's pairing window has not expired:

```bash
.venv/bin/uniden-r8 pair --confirm --seconds 25 --then-read
```

The scan prints sanitized candidate information, never a Bluetooth address.
Pairing is the only command that intentionally changes persistent BlueZ state,
so `--confirm` is mandatory. On success, the R8 bond is retained, but the
device is explicitly left untrusted and disconnected.

If the detector is already paired, do not pair it again:

```bash
.venv/bin/uniden-r8 identity --seconds 20
```

### 6. Receive live data

```bash
.venv/bin/uniden-r8 live --seconds 30
```

Typical parked output:

```text
packets: 30 telemetry, 0 alert

  13.6 V     GPS locked

alerts: 0
  clear
```

The receive window is clamped to 5–120 seconds and always tears down the BLE
connection. Add `--json` for sanitized machine-readable output or
`--save NAME.json` to retain the sanitized session in the private store.

### 7. Run the optional collector

> [!WARNING]
> The collector is deployment-specific. Its default health gate requires
> `hummer-rfcomm.service`, `/dev/rfcomm0`, and an existing RFCOMM binding.
> A standalone R8 user can use `scan`, `pair`, `identity`, and `live`
> without those Hummer components, but should not run `collect` unchanged.

On the Hummer node, first run a watched, bounded trial:

```bash
.venv/bin/uniden-r8 collect --duration 300
jq . .state/state.json
```

The collector:

- resolves the detector from its existing BlueZ bond without scanning;
- checks the OBDLink before connecting and every 15 seconds thereafter;
- releases the R8 and publishes `obd-blocked` if the primary link is
  unhealthy;
- prevents multiple instances with `flock`;
- reconnects with bounded exponential backoff;
- drops raw packets instead of retaining an unbounded history; and
- atomically writes `.state/state.json` with directory mode `0700` and file
  mode `0600`.

Example state:

```json
{
  "schema": 1,
  "collector": {
    "mode": "trial",
    "status": "streaming",
    "reconnects": 0
  },
  "obd": {
    "healthy": true,
    "rfcomm_active": true,
    "device_present": true,
    "bound": true
  },
  "link": {
    "connected": true,
    "compatible": true
  },
  "counters": {
    "telemetry_packets": 297,
    "alert_packets": 1,
    "unparsed_telemetry": 0
  },
  "telemetry": {
    "voltage": 13.6,
    "gps_locked": true,
    "poi_warning": false,
    "age_s": 0.9,
    "stale": false
  },
  "alerts": [],
  "display_line": "R8 13.6V GPS clear"
}
```

The JSON state is the intended integration boundary for a dashboard, logger,
MQTT publisher, or other local consumer. Consumers should require
`schema == 1`, validate types, and reject stale data rather than displaying a
frozen reading.

The companion e-paper reader lives in
[JeremyWhittaker/hummer_obdII](https://github.com/JeremyWhittaker/hummer_obdII).
It preserves the OBD status line and treats R8 data as an untrusted optional
input.

## Command reference

| Command | Radio use | Persistent change | Purpose |
|---|---:|---:|---|
| `plan` | None | No | Print allowed operations and refused commands. |
| `selftest` | None | Creates/validates the private store | Prove the receive-only controls before hardware use. |
| `scan` | Bounded BLE scan | No | Find an advertising R-series detector with sanitized output. |
| `pair` | BLE + BlueZ pairing | **Yes** | Create the detector bond after explicit confirmation. |
| `identity` | Bounded BLE connection | No | Read standard model, manufacturer, firmware, and software values. |
| `live` | Bounded BLE connection | No detector setting change | Read and subscribe to telemetry and alert characteristics. |
| `collect` | Continuous BLE connection | Writes local state only | Publish Hummer/OBD-aware state until stopped. |

Run `.venv/bin/uniden-r8 COMMAND --help` for command-specific options.

## Installing the Hummer service

No script installs, starts, or enables a service. The checked-in unit is also
specific to user `jeremy`, `/home/jeremy/unidenr8`, and
`hummer-rfcomm.service`; review those values before using it on another
machine.

After a successful watched drive with active OBD polling:

```bash
cd /home/jeremy/unidenr8
sudo install -m 0644 systemd/unidenr8-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start unidenr8-collector.service
systemctl status unidenr8-collector.service
```

Do **not** enable it at boot based only on the parked five-minute trial. Once a
full drive confirms coexistence with active OBD polling:

```bash
sudo systemctl enable unidenr8-collector.service
```

The unit orders itself after `hummer-rfcomm.service` but deliberately does
not start, restart, require, or modify it.

## Safety and privacy

The important boundary is **no application-characteristic writes**:

- no mute/unmute command;
- no user mark or mute-memory command;
- no settings change;
- no write to the Uniden command characteristic; and
- no serial or RFCOMM operation against the detector.

BLE is not radio-silent. Active scanning exchanges scan requests, connecting
exchanges protocol frames, and subscribing writes the standard per-connection
Client Characteristic Configuration Descriptor (CCCD). That descriptor enables
notifications; it carries no Uniden application command.

Raw diagnostic packets and redaction salts stay under the git-ignored
`.private/` directory with `0700`/`0600` permissions. Public output and
collector state omit:

- Bluetooth addresses and per-device identifiers;
- raw packets;
- heading, speed, altitude, and POI details;
- saved coordinates; and
- exception text that might contain an address.

The project never opens, releases, or rebinds `/dev/rfcomm0`, and never
changes `hummer-rfcomm.service`. See [the complete safety
boundary](docs/SAFETY.md) for the controls and their tests.

## Hardware evidence

The real-device validation established:

- all required R8w-derived vendor UUIDs are present on the tested R8;
- the R8 telemetry payload has the expected seven-field shape;
- a 30-second capture received 30 telemetry notifications with no parse
  failures;
- a 300-second collector trial received 293 telemetry packets with zero
  unparsed packets and zero reconnects;
- `/dev/rfcomm0` remained bound in all 68 concurrent state samples; and
- Bluetooth controller error counters remained at zero.

The trial did **not** exercise active OBD polling, an entire drive, or a real
radar alert. Those limitations are part of the result, not footnotes.

## Troubleshooting

**No detector appears in a scan**

- Re-enter **BT Pairing** immediately before scanning.
- Disable Bluetooth on phones previously paired with the detector.
- Confirm the R8 Bluetooth setting is enabled.
- Confirm the firmware supports R/Tach.

**`bleak is not installed`**

- Activate the intended virtual environment or rerun
  `.venv/bin/pip install -e ".[ble]"`.
- `plan` and `selftest` still work without Bleak; radio commands do not.

**The collector reports `obd-blocked`**

- Treat that as a safety action, not a collector failure.
- Verify `hummer-rfcomm.service`, `/dev/rfcomm0`, and the existing binding.
- Do not bypass the gate or restart/rebind the OBD service from this project.

**The state says `stale: true`**

- The collector has not received telemetry for more than ten seconds.
- A consumer should show stale/unknown state rather than the last reading.

For the full operational sequence, see [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Repository guide

| Path | Purpose |
|---|---|
| `src/uniden_r8/cli.py` | CLI and bounded operation entry points. |
| `src/uniden_r8/discovery.py` | Sanitized, bounded BLE discovery. |
| `src/uniden_r8/pairing.py` | Guarded BlueZ pairing that protects existing bonds. |
| `src/uniden_r8/identity.py` | Standard Device Information reads. |
| `src/uniden_r8/gatt.py` | Evidence-graded GATT catalogue and read/notify allowlists. |
| `src/uniden_r8/telemetry.py` | Telemetry and conservative alert parsing. |
| `src/uniden_r8/collector.py` | OBD-aware collector and schema-1 state publisher. |
| `src/uniden_r8/audit.py` | AST audit for application-write APIs. |
| `src/uniden_r8/privacy.py` | Address/name tokenization and redaction. |
| `src/uniden_r8/evidence.py` | Private evidence store and final publish gate. |
| `systemd/unidenr8-collector.service` | Reviewed, opt-in Hummer service template. |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Firmware, installation, pairing, testing, and operation. |
| [`docs/SAFETY.md`](docs/SAFETY.md) | Safety boundary and enforcement details. |
| [`docs/EVIDENCE.md`](docs/EVIDENCE.md) | Official, upstream, and observed claims kept separate. |
| [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) | Implementation and verification checklist. |

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[ble,dev]"
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/uniden-r8 selftest
```

The test suite includes fail-closed pairing guards, privacy and repository
hygiene checks, payload parsers, collector deadlines and teardown, OBD
invariants, and an AST audit that must detect any future application-write API.

## Credits and disclaimer

Protocol groundwork came from
[AegisX86/UnidenR8wlink](https://github.com/AegisX86/UnidenR8wlink) (MIT), by
Aigis (P.R_Aigis). This project uses those findings as evidence about an R8w
and independently verifies applicable behavior on an R8; it does not copy the
upstream source.

This project is not affiliated with or endorsed by Uniden. It is diagnostic
software, not a substitute for the detector's own display or audible warnings.
