# unidenr8 — read-only Uniden R8 Bluetooth LE for the Hummer node

A small, read-only BLE listener for a Uniden R8 radar detector, running
alongside the existing OBD-II telemetry on a Raspberry Pi Zero 2 W.

Two things it will not do, by construction rather than by configuration:

* **It has no application-characteristic write path.** It never writes to the
  Uniden command characteristic, and never sends a mute, user-mark, settings or
  any other R/Tach command. No `allow_writes` flag exists, and a test parses the
  AST of every module to prove no write API is even referenced.

  That is deliberately narrower than "never transmits". This is a radio: BlueZ
  scans actively by default, and connections, reads and subscriptions all
  exchange frames. What none of them do is carry a command to the detector.
  See [docs/SAFETY.md](docs/SAFETY.md).
* **It cannot disturb the OBDLink.** No `systemctl`, no serial import, no
  `sudo`, no systemd unit, and nothing that touches `/dev/rfcomm0` or
  `hummer-rfcomm.service`. It installs to its own tree.

  One module — `pairing.py` — does run an external program: `bluetoothctl`,
  and only through a strict guard. Fixed binary, no shell, a short verb
  allowlist that permanently bans `remove`, `trust`, `power`, `discoverable`,
  `pairable` and `connect`, and a protected set discovered at run time from
  BlueZ's own bond list so the OBDLink's bond can never be an argument. See
  [docs/SAFETY.md](docs/SAFETY.md) §1a.

---

## Status

Phase 1 (read-only discovery) is complete and the detector has been seen.

| | |
|---|---|
| Detector advertises over BLE | **Yes** — so its firmware has working Bluetooth |
| Advertised name | `R8@…` — note **not** `R8W@…` |
| Address type | Random static |
| Signal from the node | −69 dBm |
| Vendor service UUIDs in the advertisement | None — but see below |
| Paired | Yes — first attempt, left **untrusted** and disconnected |
| Model / Manufacturer (0x2A24 / 0x2A29) | `BTM10` / `ATTOWAVE` — the BLE module, not the detector |
| Software revision (0x2A28) | `R8/143/113/126/107/20260702/999/999/113` |
| Firmware | **1.43** — the current official release. No update needed. |
| Vendor services on connect | **Both present**, and every individual vendor characteristic UUID matches upstream's R8w table |
| OBD invariants | Unchanged, verified before and after every operation |

The GATT attribute table this project carries came from a different model —
upstream's Uniden **R8w**. Service discovery has now confirmed every vendor
**UUID** on Jeremy's R8, so the attribute table applies. **Payload formats have
not been confirmed**: no vendor characteristic has been read here, so the packet
layouts stay R8w evidence.

Two differences from the R8w are established: the advertised name is `R8@`, not
`R8W@`, and `0x2A24`/`0x2A29` return `BTM10`/`ATTOWAVE` rather than naming the
detector, so the model has to be read from the software-revision string. See
[docs/EVIDENCE.md](docs/EVIDENCE.md).

## What it does

```bash
uniden-r8 plan        # every permitted operation, and every refused command
uniden-r8 selftest    # prove the no-application-write properties. No radio.
uniden-r8 scan        # bounded advertisement-only discovery. Sanitized.
uniden-r8 pair        # bond with the detector. Needs --confirm.
uniden-r8 identity    # GATT-read the Device Information characteristics.
```

`plan` and `selftest` use no radio at all. `scan` uses it for
advertisement-only discovery and never connects. `pair` and `identity` do
connect — and `pair` is the one command that runs `bluetoothctl` and changes
persistent state, which is why it refuses to act without `--confirm` and fails
loudly if the result is left trusted or connected. None of them writes an
application characteristic.

```
$ uniden-r8 scan --seconds 25
scan started 2026-09-02T03:34:28Z, window 25s
devices seen: 18

tier      name                   token          signal
strong    R8@nam:2cf1419a567c    ble:bd5fb706914d  -69 dBm, random-static
```

No address appears in that output, and none can: addresses become salted
tokens at the point the report is built, and `evidence.publish()` refuses to
print a string that still contains one.

## Install

```bash
python3 -m venv .venv          # Debian marks the system Python externally
                               # managed; the venv is mandatory
.venv/bin/pip install -e ".[ble,dev]"
```

`bleak` is optional. The safety gate, the redaction and the classification all
import and test without it, so `plan`, `selftest` and the whole suite run on a
machine with no Bluetooth stack.

## Layout

| Path | What |
|---|---|
| `src/uniden_r8/gatt.py` | The attribute catalogue, evidence-graded, and the read-only allowlist. |
| `src/uniden_r8/audit.py` | AST audit proving no module writes a characteristic value. |
| `src/uniden_r8/privacy.py` | Salted tokenisation of addresses and names. |
| `src/uniden_r8/evidence.py` | The `0700`/`0600` private store, and the publish gate. |
| `src/uniden_r8/discovery.py` | Bounded, advertisement-only discovery. |
| `src/uniden_r8/cli.py` | `plan`, `selftest`, `scan`, guarded `pair`, and read-only `identity`. |
| `docs/SAFETY.md` | The boundary: OBD invariants, read-only rules, privacy. |
| `docs/EVIDENCE.md` | Every claim, with its source and its grade. |
| `src/uniden_r8/pairing.py` | Guarded `bluetoothctl` driver. Protects the OBDLink bond. |
| `src/uniden_r8/identity.py` | Connected Device Information reads. No characteristic writes. |
| `docs/RUNBOOK.md` | Firmware check, deploy, discovery, and what to do when it is empty. |
| `docs/REQUIREMENTS.md` | Parent-review checklist with status and evidence. |

## Documentation

* **[docs/SAFETY.md](docs/SAFETY.md)** — what is protected and what enforces it.
* **[docs/EVIDENCE.md](docs/EVIDENCE.md)** — the citation ledger. Official
  Uniden sources, upstream reverse engineering, and this project's own
  observations, kept separate and graded.
* **[docs/RUNBOOK.md](docs/RUNBOOK.md)** — operating procedure, including
  reading the firmware version off the detector and updating it with the
  official Uniden R Series Tool.

## Credit

Protocol groundwork from [`AegisX86/UnidenR8wlink`](https://github.com/AegisX86/UnidenR8wlink)
(MIT), by Aigis (P.R_Aigis) — a careful piece of reverse engineering with an
unusually honest account of what it did and did not verify. This project reuses
its findings as *evidence about an R8w*, not as facts about an R8, and shares
none of its code.

Not affiliated with or endorsed by Uniden.
