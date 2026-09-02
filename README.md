# Uniden R8 → Linux: a receive-only Bluetooth LE integration

Connect a Bluetooth-capable [Uniden R8](https://www.uniden.info/download/index.cfm?s=r8) radar
detector to a Raspberry Pi or any other BlueZ Linux host, and get its live telemetry and its radar
alerts out as structured data: a JSON state file, a searchable local history, an MQTT feed, and a
live web dashboard.

It uses [Bleak](https://github.com/hbldh/bleak) and BlueZ. It has **no write path to the
detector** — no mute command, no settings change, no user mark — and it is designed to share one
Bluetooth controller with a vehicle's OBD-II adapter without disturbing it.

> [!IMPORTANT]
> **Live telemetry is verified on real hardware. Active radar alerts are not.**
> Every field of an *active* alert — band, strength, frequency, direction, mute state — is decoded
> from a protocol documented on a different model (the R8w). The only alert packet this project has
> ever seen from a real non-W R8 is *all-clear*. Treat any alert it reports as unvalidated until a
> real detection has been captured and checked. [`docs/VALIDATION.md`](docs/VALIDATION.md) is the
> queue of hardware tests waiting to change that.

---

## What it does

- **Streams live detector telemetry** — battery voltage, GPS fix state, and the detector's own
  eight-point heading, speed and altitude — at about one packet per second.
- **Turns radar alerts into events.** Alert notifications are full snapshots, so the project derives
  `alert_start`, `alert_update` and `alert_end` transitions from them, with duration, peak strength
  and peak raw signal. An alert that begins and ends inside a second is recorded, not lost.
- **Keeps a local history** in SQLite: every transition, throttled telemetry, and optional GNSS
  fixes, queryable from the command line.
- **Publishes** to a JSON state file, an MQTT broker (with Home Assistant discovery), and a
  self-contained live web dashboard on the local machine.
- **Fuses external coordinates** from `gpsd`, kept rigorously separate from anything the detector
  said, because the detector does not put latitude or longitude on the wire.
- **Reads the detector's identity, settings blocks and POI database** on request, into an
  owner-only private store, for reverse-engineering work.
- **Protects the vehicle's OBD link.** On a shared controller it checks the RFCOMM binding before
  connecting and while connected, and lets go of the detector rather than compete.

It does **not** do: detector control of any kind, firmware updates, raw RF or IQ data,
dBm-calibrated signal strength, or latitude and longitude from the detector itself. Those last three
are not limitations of this code — no public evidence shows the BLE interface offers them.

## Capability matrix

| Capability | Status | Notes |
|---|---|---|
| BLE discovery and pairing | **Verified** | Bounded scan, sanitized output; guarded `bluetoothctl` bond, left untrusted and disconnected. |
| Model, firmware and database version | **Verified** | Standard Device Information reads. No vendor command. |
| Live telemetry stream | **Verified** | ~1.0 s cadence; 508 of 508 packets parsed across four hardware runs, the most recent on this build. |
| Voltage | **Verified** | Published as a number. |
| GPS fix state | **Verified** | Tri-state: locked, no fix, unknown. |
| Detector heading, speed, altitude | **Decoded; units unvalidated** | Read on real hardware: the heading is a compass point, the speed reads 0 while parked, and the altitude's magnitude fits feet rather than metres. The units still need a moving capture. |
| Latitude and longitude from the detector | **Not available — now measured, not assumed** | The GPS sub-group's first field reads a compass point on this R8, so it is not a coordinate pair. A tripwire in the parser fires if that ever changes. Use `gpsd`; the schema keeps the two sources apart. |
| Coordinates from an external GNSS receiver | **Implemented; opt-in** | `gpsd` TPV/SKY, with fix quality and error estimates. Off by default. |
| Clear / no-alert state | **Verified** | `0&0&0&0` on this R8 — four slots, all empty. |
| Band, strength, frequency, direction, mute | **Implemented; field validation pending** | Decoded from the R8w protocol. No real detection captured on this unit yet. |
| Laser gun identification | **Implemented; never seen** | The 0–19 table is decompiled from the app and unverified on any hardware. |
| Multiple simultaneous threats | **Implemented** | Every slot is preserved; the tracker follows each independently. |
| Alert start / update / end events | **Implemented** | With duration, peak strength, and an explicit ambiguity flag when correlation was close. |
| Alert history and search | **Implemented** | SQLite WAL, configurable retention, `uniden-r8 history`. |
| MQTT / Home Assistant | **Implemented; opt-in** | Retained state, non-retained alerts, discovery documents. |
| Live web dashboard | **Implemented; opt-in** | Standard library only; loopback by default. |
| Settings and POI inspection | **Implemented; read-only, confirmed** | Bytes go to the private store. Nothing is decoded — see below. |
| Detector control (mute, marks, settings) | **Intentionally absent** | Not disabled: absent. There is no code path, and a test proves it. |
| Firmware update over BLE | **Not available** | The non-W R8 updates over USB with Uniden's own tool. |
| OBD coexistence | **Partially verified** | A parked five-minute trial disturbed nothing. A drive under active OBD polling is still outstanding. |

## How it fits together

```mermaid
flowchart LR
    R8[Uniden R8] -->|BLE / GATT notify| INGEST[ingest queue<br/>seq + two clocks]
    INGEST --> PARSE[decoders]
    PARSE --> TRACK[alert tracker<br/>start / update / end]
    TRACK --> STATE[state.json + state-v2.json]
    TRACK --> DB[(SQLite history)]
    TRACK --> MQTT[MQTT broker]
    TRACK --> FEED[HTTP + SSE dashboard]
    GPSD[gpsd] -.->|coordinates, opt-in| TRACK
    OBD[OBDLink MX+] -->|Bluetooth Classic| RFCOMM[/dev/rfcomm0]
    COLLECTOR[collector] -. read-only health queries .-> RFCOMM
```

The R8 and the OBDLink share one Bluetooth controller but remain separate transports: the R8 is
BLE/GATT, the OBDLink is Bluetooth Classic SPP bound to `/dev/rfcomm0`. Nothing here opens, releases
or reconfigures that device node.

## Documentation

| Document | What is in it |
|---|---|
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | **Start here if you are picking this up.** Current state, what is proven, what is not, and where to work next. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Module map, data flow, threading model, and the reasoning behind each. |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | The reverse-engineered wire format, field by field, with evidence grades. |
| [`docs/SCHEMA.md`](docs/SCHEMA.md) | The consumer contract: both state documents, the event records, MQTT topics, the SQLite schema. |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | Every configuration key, with worked examples. |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operating procedure, from firmware check to service installation. |
| [`docs/VALIDATION.md`](docs/VALIDATION.md) | The hardware test matrix: what is still waiting on a powered-on detector. |
| [`docs/SAFETY.md`](docs/SAFETY.md) | The safety and privacy boundary, and what enforces each part of it. |
| [`docs/EVIDENCE.md`](docs/EVIDENCE.md) | Every factual claim with its source and grade. Nothing here is asserted from memory. |
| [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) | Implementation and verification checklist. |

---

## Quick start

### 1. The detector, first

If the firmware predates Bluetooth support, no amount of Linux work will help.

On the R8: press **MENU**, then **+** until **S/W version / DSP Version / GPS Version** appears, and
**MENU** to display it. Check that **BT/WiFi** exists and is **On**, and that **BT Pairing** appears.

Uniden added R/Tach support to the R8 in firmware **1.41**. This project was validated on **1.43**
with GPS database **20260702**. If the Bluetooth menu is missing, update over USB with the **Uniden R
Series Tool** from the [official R8 download page](https://www.uniden.info/download/index.cfm?s=r8)
before debugging anything on the Linux side.

### 2. The host

Any Debian-based BlueZ system, including Raspberry Pi OS:

```bash
sudo apt update
sudo apt install -y bluetooth bluez rfkill python3 python3-venv python3-pip git jq
sudo rfkill unblock bluetooth
sudo systemctl enable --now bluetooth
```

Those are host-setup commands. The application itself runs as an ordinary user and never invokes
`sudo`.

```bash
git clone https://github.com/JeremyWhittaker/unidenr8_bluetooth.git
cd unidenr8_bluetooth

python3 -m venv .venv
.venv/bin/pip install -e ".[ble,dev]"          # add ,mqtt if you want a broker
```

Python 3.11 or newer. On modern Debian and Raspberry Pi OS the system Python is marked externally
managed, so the virtual environment is mandatory rather than a preference.

### 3. Prove the safety controls before touching the radio

```bash
.venv/bin/uniden-r8 plan        # what the project is permitted to do
.venv/bin/uniden-r8 selftest    # prove it, on this machine
```

`selftest` must end with `All read-only properties hold.` It checks the UUID gates, refuses every
known R/Tach command, parses the package's own AST for any characteristic-writing Bleak call,
verifies the private store's permissions, and checks the catalogue for self-consistency. Neither
command needs Bluetooth. **If `selftest` fails, stop.**

### 4. Pair

The detector accepts one BLE client at a time. Turn Bluetooth off on any phone that normally
connects to it, then put the detector into **MENU → BT Pairing → MENU** and immediately run:

```bash
.venv/bin/uniden-r8 pair --confirm --seconds 25 --then-read
```

Pairing is the only operation here that deliberately changes persistent BlueZ state, so `--confirm`
is mandatory. It refuses an ambiguous candidate, protects every pre-existing bond — including the
OBDLink's — from being targeted, and fails loudly unless the detector ends up paired, **untrusted**
and **disconnected**. If the detector is already bonded, skip straight to:

```bash
.venv/bin/uniden-r8 identity
```

### 5. Look at live data

```bash
.venv/bin/uniden-r8 live --seconds 30
```

```text
packets: 30 telemetry, 0 alert

  13.6 V     GPS locked

alerts: 0
  clear
```

Add `--full` to also print the detector's own heading, speed and altitude, and every decoded alert
field. That is off by default because those fields describe where the vehicle is; see
[`docs/SAFETY.md`](docs/SAFETY.md) §3. Add `--json` for machine-readable output, or
`--save NAME.json` to keep the sanitized session in the private store. The window is clamped to
5–120 seconds and the link is always torn down.

### 6. Run the collector

```bash
.venv/bin/uniden-r8 config --example > unidenr8.toml   # edit it
.venv/bin/uniden-r8 config                             # check what it resolved to
.venv/bin/uniden-r8 collect --duration 300             # a watched, bounded trial
jq . .state/state.json
jq .health,.ingest .state/state-v2.json
```

> [!WARNING]
> The OBD guard is **on by default** and expects `hummer-rfcomm.service` and `/dev/rfcomm0`. On a
> node with no OBDLink, set `guard = false` under `[obd]` in the configuration file, or the
> collector will publish `obd-blocked` and refuse to connect — correctly.

The collector resolves the detector from its existing BlueZ bond without scanning, checks the OBD
link before connecting and periodically while connected, reconnects with bounded jittered backoff,
refuses to start a second instance, and writes two documents atomically:

- **`state.json`** — schema 1, unchanged and byte-compatible with the existing e-paper consumer.
- **`state-v2.json`** — schema 2: the full decoded surface, per-field confidence grades, queue and
  loop-health metrics, open alert tracks, recent events, and the external GNSS branch.

Both are `0600` in a `0700` directory, and both are git-ignored.

### 7. Ask what happened

```bash
.venv/bin/uniden-r8 history                 # counts and the span covered
.venv/bin/uniden-r8 history encounters      # one row per completed threat
.venv/bin/uniden-r8 history events -n 50    # every transition
```

History is off by default; turn it on with `enabled = true` under `[history]`.

## Command reference

| Command | Radio | Persistent change | Purpose |
|---|---|---|---|
| `plan` | none | no | Print the permitted operations and the refusal list. |
| `selftest` | none | creates/validates the private store | Prove the receive-only controls. |
| `config` | none | no | Show the effective configuration, or emit an example file. |
| `history` | none | no | Query the local history database. |
| `scan` | bounded discovery | no | Find an advertising R-series detector, sanitized. |
| `pair` | discovery + bond | **yes** | Create the bond, after explicit confirmation. |
| `identity` | one connection | no | Read the standard Device Information values. |
| `live` | one connection | no | Bounded read and subscribe of telemetry and alerts. |
| `inspect` | one connection | no | **Confirmed** read of settings and POI, into the private store. |
| `collect` | continuous | writes local state only | Hold the link and publish everything. |

Run `uniden-r8 COMMAND --help` for options.

## Tested baseline

- A non-W Uniden R8, firmware **1.43**, software revision `R8/143/113/126/107/20260702/999/999/113`,
  Device Information `BTM10` / `ATTOWAVE`.
- A Raspberry Pi Zero 2 W: Debian 13, kernel 6.18, Python 3.13, BlueZ 5.82, bleak 3.0.2, 415 MiB RAM.
- An OBDLink MX+ already bound to `/dev/rfcomm0` by a separate `hummer-rfcomm.service`.

The GATT catalogue originated with [AegisX86/UnidenR8wlink](https://github.com/AegisX86/UnidenR8wlink)
and was confirmed UUID-by-UUID on the real R8. Other models and firmware should be treated as
unverified until their identity, attribute table and payloads have been observed —
[`docs/EVIDENCE.md`](docs/EVIDENCE.md) keeps those apart on purpose.

## Safety and privacy in one paragraph

There is **no application-characteristic write path**. Not a disabled one — an absent one. Nothing
writes to the Uniden command characteristic; no mute, mute-memory, user-mark, red-light-camera-delete
or settings command is ever sent; and nothing here manages the OBD link or `/dev/rfcomm0`. That is
proved by an AST audit of the package's own source, which runs in `selftest` and in the test suite,
and which has a companion test proving it can still fail. This is a radio, so it does transmit:
BlueZ scans actively, connecting and reading exchange frames, and subscribing writes a standard
CCCD descriptor. None of that carries an application command to the detector, which is the
distinction that actually protects it. Bluetooth addresses become salted tokens, coordinates are
refused by the publication gate, raw captures stay in the git-ignored `0700` private store, and the
documents that carry position-adjacent data are `0600` and git-ignored. The full boundary, and what
enforces each part of it, is in [`docs/SAFETY.md`](docs/SAFETY.md).

## Development

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/uniden-r8 selftest
```

The suite needs no radio, no broker, no `gpsd` and no network: every external dependency has an
injection seam and every one of them is exercised with a fake. It covers fail-closed pairing guards,
the read-only gates, privacy and repository hygiene, payload parsing against adversarial input,
alert correlation, queue overflow and gap visibility, collector deadlines and teardown, the OBD
invariants, both published schemas, and an AST audit that must detect any future write path.

## Credits and disclaimer

Protocol groundwork came from [AegisX86/UnidenR8wlink](https://github.com/AegisX86/UnidenR8wlink)
(MIT), by Aigis (P.R_Aigis). This project treats those findings as evidence about an *R8w*,
independently verifies what applies to an *R8*, and does not copy the upstream source.

Not affiliated with or endorsed by Uniden. This is diagnostic software and not a substitute for the
detector's own display or audible warnings. Do not operate a phone, a Pi or a dashboard while
driving.
