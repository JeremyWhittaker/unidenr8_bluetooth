# Handoff

Everything a person or an agent needs to pick this project up. Read this first; it is short on
purpose and points at the rest.

---

## What this is, in one paragraph

A receive-only Bluetooth LE integration between a Uniden R8 radar detector and a Linux host,
originally a Raspberry Pi Zero 2 W that also carries a vehicle's OBD-II adapter on the same
Bluetooth controller. It pairs with the detector, streams its telemetry, decodes its radar alerts
into start/update/end events, keeps a local SQLite history, and publishes to a JSON state file, an
MQTT broker and a live web dashboard. It has no write path to the detector at all.

## The state of it, honestly

**Working and proven on real hardware.** Discovery, pairing, identity, the telemetry stream and its
decoding, the collector's session management, OBD-gated startup and teardown, and coexistence with
an *idle* OBD link over five minutes. The numbers are in [`EVIDENCE.md`](EVIDENCE.md) §§6–8.

**Run against the detector, on the target hardware.** The event path, the full decoder, the schema-2
document, the SQLite history and the collector were exercised on the Pi with the detector powered and
the vehicle's RFCOMM link bound: 184 packets across three windows, 0 unparsed, 0 dropped, 0 gaps,
loop lag peaking at 2.6 ms, and every OBD invariant unchanged. `EVIDENCE.md` §10 has the numbers.
Untested on hardware: the `gpsd` client (no receiver attached), MQTT (no broker), the local feed, and
the `inspect` command — which reads saved coordinates and needs its own explicit decision.

640 tests pass with no radio, no broker, no `gpsd` and no network. That proves the code does what it
was written to do. It does not prove the *protocol* is what upstream says it is.

**Not proven, and the honest gap.** No real radar detection has ever been captured from this
detector. Every field of an active alert — band, strength, frequency, direction, mute state — is
decoded from a protocol documented on an **R8w**, a different product. The only alert packet this R8
has ever sent is `0&0&0&0`. Likewise the heading and speed are now read from real packets, but from a
*stationary* vehicle, so their units and their latency are still upstream's reading rather than a
measurement.

One claim did get stronger. "The live telemetry carries no latitude or longitude" used to rest
entirely on upstream's naming of four fields nobody here had looked at — and a 4-tuple is exactly the
right width to be lat, lon, altitude and status. Those fields have now been read on this R8, and the
first is a compass point. See `EVIDENCE.md` §10.8.

[`VALIDATION.md`](VALIDATION.md) is the queue of hardware work that would change this. It is a
checklist meant to be used standing at the vehicle.

## Where to start

1. Read [`SAFETY.md`](SAFETY.md). It is the shortest path to understanding why the code is shaped
   the way it is, and it names every invariant you must not break.
2. Run `uniden-r8 plan` and `uniden-r8 selftest`. Neither needs a radio. They will tell you what the
   project is permitted to do and prove that it still holds.
3. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) for the module map and the threading model.
4. Read [`PROTOCOL.md`](PROTOCOL.md) when you need the wire format.
5. Run the tests: `.venv/bin/python -m pytest -q`. They are fast and they are the specification.

## The invariants — do not break these

These are not style preferences. Each one has a test, and several have a companion test proving the
control can still fail.

| Invariant | Enforced by |
|---|---|
| **No application-characteristic write path.** Nothing writes a value to a Uniden characteristic; no mute, mark, settings or R/Tach command is ever sent. Not disabled — absent. | `audit.py` parses the AST of every module; runs in `selftest` and in `test_the_package_contains_no_application_write_path`. `test_the_audit_actually_catches_a_write` proves it can fail. |
| **No `allow_writes` switch, anywhere.** | `test_no_module_in_the_package_exposes_an_allow_writes_switch`, which enumerates the package rather than naming modules. |
| **The command-write characteristic is permanently forbidden**, above the allowlist, in any letter case. | `gatt.FORBIDDEN_UUIDS`, checked before every gate. |
| **No Bluetooth or host address in any published output, ever.** | `privacy.py` tokenisation; `evidence.publish()` refuses; `test_repo_hygiene.py` scans every file git would commit, with no exception list. |
| **No coordinate in published output.** | `privacy.looks_like_position`, called by `evidence.publish()`. |
| **`state.json` stays schema 1 with its current key set.** A consumer requires `schema == 1` exactly. | `test_state_json_is_still_schema_one_with_exactly_its_original_keys`. |
| **The collector never mutates a unit, a binding, or the serial device.** | Two tests: an AST scan of every string list in the module, and an argv-level check against the recording probe. |
| **Nothing slow runs on the asyncio event loop.** | `test_the_obd_probe_never_runs_on_the_event_loop`; the published `health.loop_lag_ms`. |
| **A dropped notification is visible.** | `Gap` records in the stream; `test_a_dropped_notification_is_visible_as_a_gap`. |
| **Position-adjacent files are `0600` in a `0700` directory and git-ignored.** | `test_the_detailed_document_is_owner_only_and_git_ignored`. |

If you need to change one of these, that is a change to the project's purpose. Say so out loud, in
writing, before touching code.

## Things that will surprise you

**The detector sends no latitude or longitude.** It knows where it is, but the live BLE telemetry
carries only an eight-point heading, a speed, an altitude and a status letter. Coordinates come from
`gpsd` and live in a separate `vehicle_gnss` branch that is never merged with `detector_gps`. There
is a tripwire in `_parse_gps_group` that fires if the first two GPS sub-fields ever look like a
coordinate pair — if it fires, this paragraph is wrong and the documentation needs correcting, not
the tripwire.

**Track identity is a guess, and it is labelled as one.** The alert id field reads `00` in every
capture anyone has published, so the project correlates on band family, frequency within a
band-scaled tolerance, direction plausibility and strength continuity. The events carry an
`algorithm` stamp and an `ambiguous` flag. The *snapshots* are the record; the tracks are a derived
view. See [`ARCHITECTURE.md`](ARCHITECTURE.md), "Two layers".

**Direction is geometry, not identity.** Passing a fixed source walks front → side → rear for one
source. An early version of the tracker keyed on direction and manufactured a spurious end and start
at exactly the most interesting moment of an encounter.

**Field 5 of the telemetry packet is not called "wifi" here**, even though upstream calls it that,
because Uniden's own product page says the R8 has no Wi-Fi — the R8w is the Wi-Fi model. Fields 3
through 6 are published as `field_3_raw` … `field_6_raw`, with upstream's names recorded as
documentation.

**A seven-field telemetry packet is the only shape blessed.** A longer one is still decoded — a
firmware update that appends a field must not blank the voltage on a display — but it is graded
`extended-N` and the schema-1 document refuses its values.

**`obd-blocked` is not an error.** It means the gate did its job and let go of the detector. Look at
the OBD side, not at this project.

## Test conventions

- Nothing in the suite needs hardware. Every external dependency has an injection seam; see
  [`ARCHITECTURE.md`](ARCHITECTURE.md), "Where the injection seams are". If you add a component that
  talks outside the process, give it a seam in the same shape.
- No test file may contain an address-shaped literal. `tests/fixtures.py` builds them from octets,
  and the hygiene scan has no exception list — that is what makes it a control rather than a
  convention.
- Test names are sentences. Docstrings state the invariant and, where it helps, the consequence of
  getting it wrong.
- A control that cannot fail proves nothing, so several tests have a companion that demonstrates the
  control catching a deliberate violation.

## Current shape of the repository

```
src/uniden_r8/
  gatt.py         attribute catalogue with provenance; three read-only gates
  privacy.py      tokenisation, the loopback exemption, the position gate
  evidence.py     the 0700 private store, timestamps, the publication gate
  audit.py        AST proof there is no write path
  config.py       strict TOML configuration
  telemetry.py    the wire decoders + the bounded one-shot `live` session
  events.py       ingest queue, gap records, alert track derivation
  storage.py      SQLite history on its own writer thread
  gnss.py         gpsd client
  feed.py         standard-library HTTP + server-sent-events dashboard
  mqtt.py         optional paho publisher
  discovery.py    bounded advertisement-only scanning
  pairing.py      the one guarded bluetoothctl call
  identity.py     Device Information reads
  inspection.py   the confirmed settings/POI dump
  collector.py    the long-running process that wires it together
  cli.py          the command surface
```

## What to build next

Roughly in order of value, assuming the detector can be powered on:

1. **Run the validation matrix.** Nothing else in this list is worth as much as promoting the alert
   fields from UPSTREAM to OBSERVED. Start with the K-band test — an automatic-door opener is a
   lawful passive source and takes ten minutes.
2. **Prove OBD coexistence under load.** The existing evidence is a parked five-minute trial with
   the OBD collector *idle*. A one-to-two-hour drive with active polling is the test that would
   justify enabling the service at boot. Watch `health.telemetry_interval` while you do it.
3. **Read the settings blocks and build a map, one physical toggle at a time.** `uniden-r8 inspect
   --confirm` produces the snapshot; the procedure is in [`VALIDATION.md`](VALIDATION.md). This is
   slow, safe, and the only honest way to get a settings map for *this* firmware.
4. **Decode the POI record layout** — but only after a controlled test where a single known user
   mark is created and removed. There is deliberately no POI parser in the codebase: a parser that
   can appear to succeed on a structure nobody has seen populated would manufacture coordinates that
   somebody would believe.

And one thing that is deliberately **not** on this list: adding a write path. Upstream's own write
commands were decompiled from an app and have never been sent to hardware on any model, the target
is a live safety device in a moving vehicle, and this project's answer has been that the capability
is absent rather than disabled. If that is to change, it changes as a decision with a written
rationale, a single reversible command, a captured response, and a readback — not as a feature flag.

## Contact points with other systems

- **`hummer_obdII`** (sibling repository) reads `.state/state.json` for its e-paper display. It
  requires `schema == 1`, a recent timestamp, and typed allowlisted fields, and falls back cleanly
  when the state is missing or stale. That is why schema 1 is frozen.
- **`hummer-rfcomm.service`** owns `/dev/rfcomm0`. This project queries its state and never touches
  it. The unit and device path are configuration, not constants.
- **`gpsd`**, if present, on the loopback port. Optional.
- **An MQTT broker**, if configured. Optional, and off by default.
- **`hummer_obdII/docs/RUNBOOK.md`** is the documented route to the Pi, and reading it is correct.
  Do not, however, execute its host commands on the dev workstation. `nmcli -t -f ACTIVE,SSID,SIGNAL
  device wifi` and the `status.py` display probes are Pi-targeted; there they hit a real
  Wi-Fi card and, because Jeremy works over seatless RDP, polkit prompts him for a password every
  time ("System policy prevents Wi-Fi scans"). Run Pi commands over `ssh "$PI_HOST"`, or use
  `--rescan no` if you only need the cached SSID list. Tracked in
  `hummer_obdII/docs/AGENT-TODO-wifi-scan.md`.
