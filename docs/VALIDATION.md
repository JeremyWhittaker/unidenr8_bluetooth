# Hardware validation plan

This is the queue of work that is waiting on a powered-on detector.

Jeremy's R8 is currently switched **off**, so nothing here has been run. Every
test below exists to move one specific claim from UPSTREAM, UPSTREAM-UNVERIFIED
or INFERENCE to **OBSERVED** — to replace "an R8w did this, and the app was
decompiled to say so" with "this R8 did this, at this time, and here is the
count".

It is written to be used standing at the vehicle. Each test gives the exact
procedure, the exact command, the pass condition, and what grade it promotes.
Read `docs/SAFETY.md` and `docs/EVIDENCE.md` first; this document assumes both.

---

## 1. Safety and legality — read before anything else

These are not caveats appended to a test plan. They are the constraints the
plan was written inside, and a test that violates one of them is not worth its
result.

**Use emitters that already exist and are lawfully operating.** An automatic
sliding-door opener, a supermarket entrance sensor, a neighbouring vehicle's
blind-spot monitor, a fixed photo-radar or red-light installation. These are
already radiating, legally, whether or not anyone is watching them. Standing a
detector near one is observation, and observation is the whole of what this
project does.

**Do not build, buy or operate an unlicensed radar transmitter.** Not a
"low-power test source", not a modified motion sensor, not a signal generator on
a police band. Deliberately radiating on the K or Ka police bands is regulated,
and this project does not give legal advice about where the line is — it simply
refuses to go near it. There is no test in this document that requires a
transmitter, and if one appeared to, the correct answer is a longer wait for an
opportunistic capture.

**Do not speed to create a capture.** A Ka encounter is not worth a citation, a
collision, or the argument that follows. Every Ka test here is opportunistic by
construction: drive a normal route at a normal speed, log continuously, and take
what arrives.

**Do not operate a phone, a laptop or the Pi while driving.** Every test that
needs motion needs one of two arrangements:

* a **passenger** who runs the command, watches the output and writes down what
  happened, while the driver drives; or
* a **parked logging session** — start the collector before moving, drive, stop,
  switch off, and read the state and history afterwards.

The second is the default. It is slower and it is safe, and almost every test
below is written so that it works that way.

**The detector's own display and its audible alerts remain the authority.** This
is diagnostic software; it is not a radar display, and a screen showing a
decoded packet is not a reason to look away from the road.

**Re-check the OBD invariants before and after every session.** `docs/RUNBOOK.md`
step 1 has the exact commands. `hummer-rfcomm` active, `rfcomm0:` bound, no
failed units, controller `Powered: yes`. If any of them differs afterwards, that
is the finding, and it outranks whatever the detector said.

**One thing at a time on the detector.** Tests V8 and V9 involve a human
changing something on the unit — one user mark, one menu item. Change exactly
one, record it, and put it back. A settings diff across two changes names
nothing.

---

## 2. What is already observed, and does not need re-testing

From `docs/EVIDENCE.md` §§6–8, all graded OBSERVED on this specific R8. Do not
spend hardware time re-establishing any of it, and in particular **do not pair
again** — the bond exists and every command below resolves the detector from
BlueZ's own bond state without discovery.

| Already OBSERVED | Where |
|---|---|
| The detector advertises over BLE, as `R8@` + a four-character fragment — not `R8W@`. | §6.1, §6.2 |
| Its address is random static. | §6.3 |
| The advertisement carries no service UUIDs. | §6.5 |
| It pairs, and stays paired, left untrusted and disconnected. | §6.8 |
| It exposes five GATT services, including **both** Uniden vendor services. | §6.10 |
| Every vendor characteristic UUID upstream documented on an R8w is present, and nothing extra. | §6, vendor table |
| Device Information reads: `BTM10`, `ATTOWAVE`, `NA/NA/NA/NA/NA/NA/NA/NA`, `R8/143/113/126/107/20260702/999/999/113`. | §6 |
| Firmware 1.43 and database 20260702 — the current published versions. | §6 |
| Telemetry streams at ~1.0 s (measured 0.97–1.02 s). | §7.2 |
| The telemetry packet has exactly **7** `&`-separated fields, and the GPS group exactly **4** comma sub-fields. 324 of 324 packets across two runs. | §7.3, §8.1 |
| Battery voltage decodes: 13.6 V steady across one run, and 12.3 V rising to 13.6 V across another. The cause of that change was not established. | §7.4, §8.4 |
| The detector reports a GPS fix state. | §7.5 |
| The alert characteristic answers, and its **all-clear** form parses. | §7.6, §8.6 |
| A five-minute held link: 293 packets, 0 unparsed, 0 reconnects, clean self-stop. | §8.1–8.3 |
| A five-minute BLE session did not disturb the RFCOMM binding **while the OBD collector was idle**. | §8, and its own stated limits |
| State directory `0700`, `state.json` and `collector.lock` `0600`. | §8.5 |

Two things in that list are deliberately narrower than they look, and V10 exists
because of them: the OBD result was measured against an **idle** RFCOMM binding
for **five minutes**, and throughput was not measured at all.

**What is not yet observed is everything about an active alert.** Band, strength,
raw signal, frequency, the field-5 tagged union, direction, every mute code, and
field 8 are UPSTREAM (R8w) or weaker. The only alert payload this R8 has ever
produced is all-clear. Say so in any report, and treat any alert the collector
reports today as unvalidated.

---

## 3. Three evidence vocabularies, and how they line up

The same fact is graded in three places, with three spellings. Promoting a field
means changing all of them.

| `docs/EVIDENCE.md` | `uniden_r8.gatt.Evidence` | `uniden_r8.telemetry.FIELD_CONFIDENCE` | Means |
|---|---|---|---|
| OFFICIAL | `official` | — | Uniden said it. |
| OBSERVED | `observed-on-this-r8` | `observed` | Seen on Jeremy's R8 by this project. |
| UPSTREAM | `upstream-r8w` | `upstream` | Captured on an R8w by `AegisX86/UnidenR8wlink` @ `9072bc2f`. |
| UPSTREAM-UNVERIFIED | `upstream-r8w-unverified` | `candidate` | In that upstream, documented there as never tested on hardware. |
| INFERENCE | `inference` | — | This project's reasoning. Not observed anywhere. |

`FIELD_CONFIDENCE` is not a comment: it is published in the schema-2 document's
`confidence` block and in `live --full`, so a consumer can tell a measurement
from a hypothesis. Changing a grade there changes what every downstream reader
is told.

---

## 4. The common procedure

### 4.1 Before every session

```bash
cd "$UNIDEN_ROOT"                       # /home/jeremy/unidenr8
.venv/bin/uniden-r8 selftest            # must end: All read-only properties hold.
```

`.venv/bin/uniden-r8` and `.venv/bin/python -m uniden_r8.cli` are the same
program; `docs/RUNBOOK.md` uses the second form and either works here.

Then the OBD invariants from `docs/RUNBOOK.md` step 1. If `selftest` fails,
stop — do not go near the detector.

Turn Bluetooth **off on any phone** that normally connects to the detector,
except in V11, where the phone is the test.

### 4.2 Which command captures what

This is the single most important operational fact in this document, and it
decides how every protocol test below is run.

| | `live` | `collect` |
|---|---|---|
| Raw payload bytes retained | **Yes** — every read and notification, hex-encoded, into `.private/live-raw-<stamp>.json` (`telemetry.py`, `_session.record`, capped at 2000 packets) | **No.** By design; see `docs/SAFETY.md`, "Raw packets are not retained" |
| Duration | Clamped to 5–120 s (`bounded_receive_seconds`) | Unbounded, or `--duration N` |
| Alert events (start/update/end) | No | Yes |
| SQLite history | No | Yes, when `[history] enabled = true` |
| Published state documents | No | `state.json` and `state-v2.json` |

**Therefore: any test whose purpose is to promote a wire-format field to
OBSERVED must be run under `live`, because the collector cannot produce the
bytes.** The collector's schema-2 `alerts[]` entries and its history rows are a
strong secondary record — they carry every decoded field including
`field_5_raw`, `alert_id_raw`, `receive_mode_raw`, `mute_code` and
`field_count` — but they cannot answer "what were the exact bytes", and a
capture that was rejected by the slot gate leaves nothing behind but a counter.

### 4.3 Reading a raw capture

On the node only. This prints device bytes; nothing it prints may be pasted into
a document, a commit or a message.

```bash
cd "$UNIDEN_ROOT"
.venv/bin/python - <<'PY'
import json, pathlib
newest = sorted(pathlib.Path(".private").glob("live-raw-*.json"))[-1]
doc = json.loads(newest.read_text())
print(newest.name, len(doc["packets"]), "packets")
for packet in doc["packets"]:
    text = bytes.fromhex(packet["hex"]).decode("utf-8", "replace")
    print(f'{packet["at"]:>9.3f}  {packet["kind"]:<16} {text}')
PY
```

### 4.4 Reading the published state

```bash
jq . "$UNIDEN_ROOT/.state/state.json"                       # schema 1
jq '.detector, .counters, .health, .ingest' \
   "$UNIDEN_ROOT/.state/state-v2.json"                      # schema 2
jq '.alerts, .open_tracks, .recent_events[0:5]' \
   "$UNIDEN_ROOT/.state/state-v2.json"
```

Schema 2 is where almost every validation signal lives. Schema 1 is frozen for
the e-paper consumer and deliberately carries neither the detector's motion
fields, nor the unreadable-slot counters, nor the coordinate tripwire.

### 4.5 Configuration for a validation session

```toml
[collector]
detail = true            # default; state-v2.json is where the evidence is

[history]
enabled = true           # off by default
telemetry_every_seconds = 5.0
record_detector_motion = true   # needed for V1; position-adjacent, opt-in

[gnss]
enabled = true           # an independent reference for V1
record_coordinates = false      # keep it false; see below
```

Leave `gnss.record_coordinates` **false**. The gpsd client still reports fix
mode, satellite count, speed and course with it off, which is everything V1
needs, and it never records where the vehicle was. Two further reasons: the
history's `lat`/`lon` columns stay null, and `uniden-r8 history events --json`
refuses to print a document containing a coordinate — the refusal arrives as an
uncaught `PublicationRefused` traceback, which is a poor way to discover it in a
car park.

---

## 5. The test matrix

| ID | Test | Proves | Needs | Promotes |
|---|---|---|---|---|
| V1 | Moving GPS | Heading, speed and altitude fields, their units and their latency; the coordinate tripwire stays silent | A drive, a passenger or a logged session, gpsd | `gps.direction_8`, `gps.speed_mph`, `gps.altitude_ft`, `gps.status_raw` |
| V2 | K-band alert | The active-alert slot format decodes at all on this R8 | A parked car near an automatic door | The whole alert slot, for K |
| V3 | Ka alert | The same, on the band that matters | Opportunistic, over weeks | Alert slot for Ka; `BAND_TOLERANCE_GHZ` |
| V4 | Direction sweep F → S → R | The direction codes, and that the tracker follows one threat through a pass | A parked-to-rolling pass by a door opener | `DIRECTIONS`, the tracker's `_direction_cost` |
| V5 | Physical mute press | Mute codes 1 and 2, observed rather than decompiled | An active alert plus a finger | `MUTE_STATES` "1" and "2" |
| V6 | Two simultaneous threats | Multi-slot snapshots, and independent tracks | Two emitters in range | The `&`-separated multi-slot form |
| V7 | POI warning | Telemetry field 1's **active** form | A drive past a camera the R8 database knows | `poi.active` (active form), POI sub-fields |
| V8 | POI dump, before and after one user mark | The POI record framing | Parked, two `inspect` runs | POI record type byte and length |
| V9 | Settings diff, one menu change | Which byte is which setting | Parked, two or three `inspect` runs | One settings byte per repetition |
| V10 | OBD coexistence, 1–2 h under active polling | The claim the RUNBOOK gates service installation on | A real drive with `hummer-collector` running | Coexistence, from "parked, idle, 5 min" |
| V11 | Competing client (R/Tach app) | What happens when a second BLE client wants the detector | Parked, a phone | Single-client arbitration |
| V12 | Queue, gaps and a long session | That loss is visible, and the clocks behave | The V10 drive | Ingest and watchdog behaviour |

A workable order for one afternoon plus one ordinary week: V9 and V8 parked on
the driveway; V2, V4, V5 and V6 in a supermarket car park; V1 and V7 on one
drive with a passenger; V10 and V12 on any real drive; V11 parked afterwards;
V3 whenever it happens.

---

### V1 — Moving GPS: heading, speed, altitude, latency, and the tripwire

**What it proves.** That the four sub-fields of telemetry field 2 are what
upstream says they are, in the units upstream says, and how far behind reality
the detector's own reading is. And, negatively, that sub-fields 0 and 1 are not
a latitude and longitude — the claim this whole project's privacy design rests
on.

**Preconditions.** A GNSS receiver feeding `gpsd` on the node, `[gnss] enabled =
true`, `record_coordinates = false`. `[history] record_detector_motion = true`.
A passenger, or a logged session read afterwards.

**Procedure.**

1. Park, engine running, and confirm the detector has a fix (its own display,
   and `gps_locked` in the state file).
2. Start the session. Two shapes, both valid:
   * passenger, bytes wanted: `live` for up to 120 s while moving;
   * driver alone: `collect` for the whole drive, read afterwards.
3. Drive a route that includes: a straight run in one compass direction held for
   at least 30 s, a right-angle turn, a sustained speed of 25–35 and then 45–55
   (whatever the posted limit allows — no more), and a hill if one is available.
4. Note, by hand or from the passenger's phone, the wall-clock time of each turn
   and each speed change.

```bash
# Passenger, with bytes:
.venv/bin/uniden-r8 live --seconds 120 --json --full --save v1-drive.json

# Or, logged for the whole drive:
.venv/bin/uniden-r8 collect --duration 1800
# afterwards:
.venv/bin/uniden-r8 history telemetry -n 200
jq '.detector.detector_gps, .vehicle_gnss' "$UNIDEN_ROOT/.state/state-v2.json"
```

**Pass condition.**

* `detector_gps.direction_8` is one of the eight compass points and agrees with
  gpsd's `track_deg` to within one 45° sector across at least twenty samples
  spanning at least two headings. Anything else — a ninth value, a number, a
  bearing in degrees — is a finding and belongs in the ledger verbatim.
* `speed_raw` tracks gpsd. Compare it against `vehicle_gnss.speed_mph`
  (the client converts) and against `speed_mps × 3.6`. Agreement with the first
  confirms **mph**; agreement with the second means the field is km/h and
  `DetectorGps.speed_mph` is misnamed.
* `altitude_raw` ÷ gpsd `altitude_m` ≈ 3.28 confirms **feet**; ≈ 1.0 means
  metres.
* Latency: the detector's heading changes some number of packets after gpsd's
  does. Record that number of seconds. It is a real property of the device and
  nobody has measured it.
* `status_raw` reads `C` whenever a fix is present. If a second letter ever
  appears, record it and what the detector's own display was showing at that
  moment — the meaning of `C` is currently an inference.
* **`suspect_coordinate_pair` is false in every packet.** If it is ever true, go
  to §7 and stop.

**How to see the tripwire.** It is in schema 2 only:

```bash
jq '.detector.detector_gps.suspect_coordinate_pair' "$UNIDEN_ROOT/.state/state-v2.json"
```

Check it deliberately. When the tripwire fires, the parser blanks the whole
group and publishes nothing from it, so the *only* symptom in schema 1 is
`gps_locked: null` — the packet is still counted as parsed, and no unparsed
counter moves. A run could fire it on every single packet and look, in schema 1,
like a detector with a poor GPS fix.

**Promotes.** `gps.direction_8`, `gps.speed_mph` and `gps.altitude_ft` from
UPSTREAM to OBSERVED, with units either confirmed or corrected.
`gps.locked`/`status_raw` from `candidate` to OBSERVED. And it converts "the
detector sends no coordinates" from an argument about upstream's field naming
into a measurement.

---

### V2 — A K-band alert from an emitter that is already there

**What it proves.** That the nine-field alert slot format upstream documented on
an R8w decodes on this R8 at all. This is the single highest-value test in the
document: every alert field in the codebase currently rests on a different model
plus a decompiled app.

**Source.** An automatic sliding-door opener, a supermarket entrance sensor or a
similar installed motion sensor. These operate in the K band around 24.1 GHz and
are exactly what a radar detector was designed to false on. Park legally, within
the detector's range, engine running so the detector is powered, and do not
obstruct the doorway.

**Procedure.**

1. Confirm the detector's own display shows the K alert before touching the Pi.
   If the detector is not alerting, the Pi has nothing to receive.
2. Run a 60-second `live` window while the alert is sounding.
3. Move a few metres and repeat, so the strength differs between captures.

```bash
.venv/bin/uniden-r8 live --seconds 60 --json --full --save v2-kband-01.json
```

**Pass condition.**

* `alert_packets` greater than zero.
* `unparsed_alert_packets` is 0 and every slot decoded — a rejected slot is
  reported as `unreadable` and is the interesting failure, not a crash.
* At least one decoded alert with `band: "K"`, `strength` in 1–8,
  `frequency_ghz` near 24.1, `direction` one of front/side/rear, and
  `mute_code: "1"`.
* The raw capture in `.private/live-raw-*.json` contains an alert payload whose
  first sub-field is `1` and which has eight or nine comma-separated fields.

**If a slot is rejected**, `_parse_slot` refused it for one of six reasons, all
strict on purpose: fewer than eight fields, field 0 not `1`, an unlisted band
name, a strength outside 1–8, a non-numeric raw signal, or a direction letter
that is not exactly `F`, `S` or `R`. The raw capture tells you which. That is a
protocol finding of the first order — record it before changing any code, and
see §7.

**Promotes.** For the K band: `alert.active`, `alert.alert_id_raw`,
`alert.band`, `alert.strength`, `alert.raw_signal`, `alert.frequency_ghz`,
`alert.direction`, `alert.mute_code` from UPSTREAM to OBSERVED, and
`alert.receive_mode_raw` from `candidate` to "observed, meaning still unknown".
It does **not** promote Ka, laser, the photo-radar types, or mute codes above 2.

---

### V3 — A Ka alert

**What it proves.** The same slot format on the band that a radar detector
actually exists for, and the Ka frequency range the tracker's tolerance is tuned
to (`BAND_TOLERANCE_GHZ["KA"] = 0.025` GHz, chosen for a 33.4–36.0 GHz band that
wanders more than K does).

**Source.** Opportunistic and only opportunistic. A genuine law-enforcement
encounter on a normal drive at a normal speed, or a fixed photo-radar
installation that is already there. **Do not create one.** Do not speed. Do not
transmit.

**Procedure.** This is the test that cannot be scheduled, so it is run as a
standing arrangement rather than an event:

1. Run the collector with history enabled on ordinary drives (this is V10 and
   V12's session; one drive serves all three).
2. After each drive, check `uniden-r8 history encounters`.
3. When a Ka encounter appears and the location is repeatable — a fixed
   installation, a patrol position that recurs — schedule one 120-second `live`
   pass at that spot, with a passenger, to capture the bytes.

```bash
# standing capture, every drive
.venv/bin/uniden-r8 collect --duration 5400

# afterwards
.venv/bin/uniden-r8 history encounters
.venv/bin/uniden-r8 history events -n 100

# once a repeatable source is known, with a passenger
.venv/bin/uniden-r8 live --seconds 120 --json --full --save v3-ka-01.json
```

**Pass condition.** A decoded alert with `band: "KA"` (or `"KA POP"`), a
`frequency_ghz` inside 33.4–36.0, and `unreadable_slots` 0 for the encounter. An
`alert_end` row in the history with a plausible `duration_s`, a `max_strength`
above the strength at the start, and `correlation` of `timeout` rather than
`ambiguous`.

**A limitation to state plainly.** A Ka encounter caught by the collector
promotes the *decode* but not the *bytes*: the collector retains no payloads.
The history row and the schema-2 `alerts[]` entry carry nearly the whole slot —
including `field_5_raw` and `field_count` — which is enough to grade the fields
OBSERVED, but it is not a capture, and a slot that the gate rejected leaves only
`unreadable_slots += 1` behind. Note in the ledger which kind of evidence a
given Ka finding rests on.

**Promotes.** The alert slot for the Ka family, and the first real check on
whether `_FREQUENCY_BANDS` is the right split for field 5.

---

### V4 — Direction sweep, front to side to rear

**What it proves.** That `F`, `S` and `R` mean front, side and rear on this
unit, and — more valuable — that the tracker follows one physical source through
a pass instead of manufacturing an end and a start at the moment of closest
approach. That failure mode is the reason `events.py` does not key on direction,
and it has never been tested against a real pass.

**Procedure.** Use the same door opener as V2.

1. Start the collector (history on) with the vehicle stationary and well clear
   of the emitter, front-on.
2. Drive slowly and legally past it, so the source moves from ahead, to abeam,
   to behind. Walking pace across a car park is ideal.
3. Stop, wait ten seconds, and end the session.

```bash
.venv/bin/uniden-r8 collect --duration 300
.venv/bin/uniden-r8 history events -n 50
.venv/bin/uniden-r8 history encounters
```

**Pass condition.**

* Exactly **one** `alert_start` and **one** `alert_end` for the pass. Two of
  each means the tracker split one threat, which is the bug this test exists to
  find.
* The `alert_end` row's `directions` column reads `FSR` — the string accumulates
  as the direction changes (`AlertTrack.absorb`).
* `correlation` on the end row is `timeout`, not `ambiguous`.
* `samples` greater than three, and `duration_s` matching the wall-clock length
  of the pass.
* `unreadable_slots` stays 0 throughout.

**Promotes.** `DIRECTIONS` from UPSTREAM to OBSERVED, and gives the tracker's
`cost-greedy-1` algorithm its first real-world evidence. Record the
`TRACKING_ALGORITHM` string with the result: a later matcher will need to be
compared against this same encounter.

---

### V5 — A physical mute-button press during a live alert

**What it proves.** That mute state is observable on the wire, and that codes 1
and 2 mean "not muted" and "muted" on this unit. Upstream captured this
transition on an R8w; codes 3 to 6 come from the decompiled app and may be
wrong.

**This test sends nothing.** The mute is a human finger on the detector's own
button. `BTreqMUTE:1` is on the permanent refusal list, `refuse_command()`
raises unconditionally, and the AST audit proves no module can write a
characteristic value. Observing a mute and commanding one are different acts and
this project only does the first.

**Procedure.**

1. Park near the V2 emitter and get a sustained K alert.
2. Start a 60-second `live` window.
3. About 15 seconds in, press the detector's mute button once.
4. Let the alert continue for another 15 seconds; do not unmute.

```bash
.venv/bin/uniden-r8 live --seconds 60 --json --full --save v5-mute-01.json
```

**Pass condition.**

* In the raw capture, field 7 of the alert slot changes from `1` to `2` at the
  moment of the press.
* Decoded: `mute_code` "1" → "2", `mute_state` "not muted" → "muted",
  `muted` false → true.
* `unknown_mute_codes` stays 0. A non-zero value means the detector used a code
  outside `MUTE_STATES`, which is a finding worth more than the test itself.
* If the same press is run under `collect`: exactly one `alert_update` with
  `material: true` at that instant, and **no** `alert_end`/`alert_start` pair —
  the track must survive a mute.

**Promotes.** `MUTE_STATES` entries `"1"` and `"2"` from UPSTREAM to OBSERVED,
and `alert.mute_code` with them. Codes 3–6 stay UPSTREAM-UNVERIFIED; a mute
memory or quiet-ride code can only be promoted by a capture that actually
contains it.

---

### V6 — Two simultaneous threats

**What it proves.** That the alert characteristic really does send multi-slot
snapshots separated by `&`, that both slots decode, and that the tracker follows
them as two independent threats rather than merging or alternating between them.

**Source.** Two emitters that are already there: a shopping-centre entrance with
two door sensors, or one door opener plus a passing vehicle's blind-spot monitor
(also K band, and extremely common in a car park).

**Procedure.**

1. Position so that the detector's own display shows two simultaneous alerts.
   The display is the ground truth here; if it shows one, this test has not
   started.
2. Run a 60-second `live` window, and a `collect` session afterwards for the
   event view.

```bash
.venv/bin/uniden-r8 live --seconds 60 --json --full --save v6-two-01.json
.venv/bin/uniden-r8 collect --duration 180
jq '.alerts, .open_tracks' "$UNIDEN_ROOT/.state/state-v2.json"
```

**Pass condition.**

* At least one payload in the raw capture with two `&`-separated slots, both
  beginning `1,`.
* `slot_count` 2, `rejected_slots` 0, two entries in `alerts`.
* Two distinct `track_id` values in `open_tracks` at the same instant, with
  different `min_frequency_ghz`/`max_frequency_ghz` ranges.
* Record whether `ambiguous` is true on either track. Two similar K sources at
  similar strength is precisely the case `AMBIGUITY_MARGIN` was written for, and
  an honest `ambiguous: true` is a passing result — it is the tracker saying
  "this might be one source or two", which is the truth.

**Promotes.** The multi-slot snapshot form from UPSTREAM to OBSERVED, and gives
the greedy assignment its first contested case.

---

### V7 — A POI warning at a known camera

**What it proves.** The **active** form of telemetry field 1. Only the inactive
form — a literal `0` — has ever been observed, on either model, so the
three-part active structure upstream describes has no hardware evidence
anywhere.

**Source.** A red-light or speed camera that the R8's own GPS database already
knows about. The detector announces these on its own; no radar source is
involved and nothing is transmitted by anybody.

**Procedure.** Three constraints make this test unusual, and all three come from
the code:

* **It must be run under `live`.** The collector keeps no bytes, and field 1's
  active text is dropped before it reaches any published document (below).
* **Do not use `--full` without `--json`.** `LiveSession.render()` reads
  `poi.kind`, `poi.distance_raw` and `poi.speed_limit_raw`, none of which exist
  on `PoiWarning`, so the text renderer raises `AttributeError` on the first
  active POI warning. `--json --full` renders through `detailed()` and is safe.
  This is a real defect; fix it before the drive if there is time, and record
  that it was hit if there is not.
* **The bytes are location-revealing.** A POI warning identifies which camera the
  vehicle was next to. The raw capture stays in `.private/` and nothing derived
  from it is published without a specific decision.

1. Confirm the camera's location in advance from the detector's own behaviour on
   a previous drive, so the passenger knows when to start the window.
2. Start a 120-second `live` window about a minute before the camera.
3. Drive past at the posted limit.

```bash
.venv/bin/uniden-r8 live --seconds 120 --json --full --save v7-poi-01.json
```

**Pass condition.**

* At least one telemetry packet with `poi_warning.active` true.
* In the raw capture, field 1 is not `0` and has an internal structure —
  upstream describes a type, a distance and a speed limit.
* Ideally several consecutive packets showing the distance sub-field counting
  down on the approach. That is what turns a structure into a decoded field.

**Expect `raw` to be null.** `_parse_poi_group` passes the group through
`_safe_word(limit=48)`, which rejects any string containing a comma. Upstream's
active form is comma-separated, so the sanitized record will be
`{"active": true, "raw": null, "decoded": null}` — the boolean survives and the
text does not. That is the sanitizer working as designed; it is also why this
test cannot be run under the collector, whose only record would be
`poi_warning: true`.

**Promotes.** `poi.active` from "observed inactive" to "observed active", and,
if the private hex shows a stable three-part structure across several packets,
gives the first real evidence for the POI type, distance and speed-limit
sub-fields — currently all `candidate`.

---

### V8 — A stored-POI dump, before and after creating one user mark

**What it proves.** The POI database's record framing: the type byte, the record
length, and whether upstream's candidate lengths (speed camera 15, red-light 14,
user mark 12 bytes — the values in `CANDIDATE_RECORD_LENGTHS`, which is what the
boundary walk actually uses; the module docstring above it says 13, 12 and 10,
and the two disagree) hold on this unit. Upstream published those lengths and also
recorded that the only POI database it ever read was **empty**, so this may be
the first non-empty POI read on any unit.

**This is the most sensitive test in the document.** The POI characteristic is
the only attribute that carries real coordinates — saved cameras, and every user
mark Jeremy has ever made. `inspect` exists for exactly this, it requires
`--confirm`, and the collector has no code path that can reach it.

**Procedure.** Parked. Somewhere unremarkable: create the test mark in a car
park, not on the driveway, because its bytes are about to be examined.

1. `inspect --confirm` → snapshot A.
2. On the detector, create exactly **one** user mark.
3. `inspect --confirm` → snapshot B.
4. Delete the mark on the detector, by hand, if it is not wanted.

```bash
.venv/bin/uniden-r8 inspect --confirm          # snapshot A
# ... create one user mark on the detector ...
.venv/bin/uniden-r8 inspect --confirm          # snapshot B
```

Then diff the two, on the node:

```bash
cd "$UNIDEN_ROOT"
.venv/bin/python - <<'PY'
import json, pathlib
a, b = sorted(pathlib.Path(".private").glob("inspect-*.json"))[-2:]
first  = {x["uuid"]: x.get("hex", "") for x in json.loads(a.read_text())["attributes"]}
second = {x["uuid"]: x.get("hex", "") for x in json.loads(b.read_text())["attributes"]}
for uuid, before in first.items():
    after = second.get(uuid, "")
    if before == after:
        print(uuid, "unchanged,", len(before) // 2, "bytes")
        continue
    print(uuid, f"{len(before)//2} -> {len(after)//2} bytes")
    old, new = bytes.fromhex(before), bytes.fromhex(after)
    for offset in range(min(len(old), len(new))):
        if old[offset] != new[offset]:
            print(f"  offset {offset}: 0x{old[offset]:02x} -> 0x{new[offset]:02x}")
PY
```

**Pass condition.**

* The POI blob grows by exactly one record.
* The growth is a whole number of bytes matching one of upstream's candidate
  lengths, beginning with the corresponding type byte (`0x03` for a user mark).
  A 12-byte growth led by `0x03` confirms upstream's framing for that type.
* The printed `inspect` summary's record-boundary walk completes across the
  whole blob rather than stopping at an unrecognised type byte. `inspect`
  reports that already: "N candidate record boundaries on upstream's layout",
  with ", walk did not complete" appended when it stopped early.

**What may be written down.** The record length, the type byte, whether the walk
completed, and the byte offsets that changed. **Never** the delta's contents.
Those bytes are a coordinate — that is the entire point of the test — and the
project's own gate refuses to publish one.

**Promotes.** POI record framing for the user-mark type from UPSTREAM to
OBSERVED. It does **not** authorise a POI parser: decoding the coordinate out of
those bytes and printing it is a separate decision with a separate conversation,
and `inspection.py` explains at length why it reports shapes rather than
contents.

---

### V9 — A settings diff across exactly one physical menu change

**What it proves.** Which byte in the settings blocks corresponds to which menu
item. This is the only honest way to build a settings map: a community byte map
exists, it is incomplete, and it is keyed to firmware nobody here is running.

**Procedure.** Parked. One toggle per repetition, and pick a reversible,
non-safety item — display brightness, or the auto-dim setting. **Do not change
band enables, sensitivity or alert volume and leave them changed**; those are
what the detector does for a living.

1. `inspect --confirm` → snapshot A.
2. Change exactly one menu item on the detector. Write down which, and from what
   to what.
3. `inspect --confirm` → snapshot B.
4. Put the setting back.
5. `inspect --confirm` → snapshot C.

```bash
.venv/bin/uniden-r8 inspect --confirm     # A
# ... one menu change ...
.venv/bin/uniden-r8 inspect --confirm     # B
# ... put it back ...
.venv/bin/uniden-r8 inspect --confirm     # C
```

Diff with the script from V8, run twice (A against B, then B against C).

**Pass condition.**

* Exactly one byte, or one short contiguous run, differs between A and B.
* C matches A byte for byte. That is what makes the identification an
  observation rather than a coincidence — a byte that changed for some other
  reason will not change back.
* Record: the menu item, both values, the characteristic (settings 1 or
  settings 2), the offset, and both byte values.

**Also record, once.** Whether this R8's settings-2 block is all `0xff`, as
upstream's R8w was. The `inspect` summary answers that without printing
anything: `all_same` and `dominant_byte` in `summarise_bytes`, printed as "N
distinct byte values, X% 0xff, all identical".

**Promotes.** One settings byte per repetition, from no evidence at all to
OBSERVED. Nothing here promotes a *reading* of the whole block; a settings map is
built one physical toggle at a time and stays a list of individually confirmed
bytes until it is not.

---

### V10 — OBD coexistence for one to two hours under active polling

**What it proves.** The claim that gates everything else. `docs/EVIDENCE.md` §8
is explicit about its own two limits: the OBD collector was **not running**, so
the RFCOMM binding was bound and idle rather than carrying poll traffic, and
five minutes is not a drive. `docs/RUNBOOK.md` step 6 will not enable the
collector at boot until this test has been done.

**Preconditions.** The vehicle's own OBD collector actually polling. Starting it
is the OBD project's business and a human's decision; nothing in this project
starts, stops or enables a unit, and nothing here opens `/dev/rfcomm0`.

**Procedure.**

1. Record the OBD invariants and the controller counters before starting.
2. Start the vehicle's OBD collection as that project documents.
3. Start this project's collector for the length of the drive.
4. Drive normally for one to two hours.
5. Record everything again afterwards, and compare.

```bash
# before
systemctl is-active hummer-rfcomm; rfcomm; systemctl --failed --no-pager

# the session
.venv/bin/uniden-r8 collect --duration 5400

# a second shell, sampling throughout (a passenger, or left running)
watch -n 15 'systemctl is-active hummer-rfcomm; rfcomm; \
  jq -c ".obd, .health, .link" /home/jeremy/unidenr8/.state/state-v2.json'

# after
systemctl is-active hummer-rfcomm; rfcomm; systemctl --failed --no-pager
jq '.counters, .health, .ingest, .collector' "$UNIDEN_ROOT/.state/state-v2.json"
```

**Pass condition.**

| Signal | Expected |
|---|---|
| `hummer-rfcomm` | active in every sample |
| `rfcomm0:` | bound on channel 1 in every sample |
| `health.telemetry_interval.median_s` | ≈ 1.0, matching the 0.97–1.02 s baseline in EVIDENCE §7.2 |
| `health.telemetry_interval.p95_s` | not materially above the median — a widened p95 is the cheapest signal of radio contention this project has, and it is the reason `Timing` is published at all |
| `health.loop_lag_alarm` | false, and `loop_lag_max_ms` under 1000 |
| `collector.status` | `streaming`, never `obd-blocked` |
| `collector.reconnects` | 0, or each one explained |
| `counters.unparsed_telemetry` | 0 |
| `ingest.dropped` / `ingest.gaps` | 0 |
| The OBD project's own poll success rate | unchanged from a drive without this collector |

That last row is the one that actually answers the question, and this project
cannot measure it: confirming the OBD link still *works* would mean opening
`/dev/rfcomm0`, which is forbidden. The evidence available here is that the
binding and the controller stayed healthy and that this link's own latency did
not widen. Say exactly that in the ledger, and get the poll-rate comparison from
the OBD side.

If controller error counters are read (`hciconfig -a hci0`, if that tool is
present on this node), record RX and TX errors before and after. If it is not
present, record that instead of inventing a number.

**Promotes.** Coexistence from "parked, idle OBD binding, five minutes" to
"driving, active polling, one to two hours". Nothing about the wire protocol.

---

### V11 — Competing-client behaviour with the R/Tach phone app

**What it proves.** What the detector does when a second BLE client wants it.
Upstream observed on an R8w that a paired unit advertises only while nothing is
connected and stops the instant anything connects, including a phone in the
background; `docs/EVIDENCE.md` 6.7 records that this R8's advertising behaviour
outside pairing mode is **not established**. The practical question for Jeremy is
simpler: can the phone app and the Pi coexist, and which one loses?

**Procedure.** Parked, engine running. Two halves, run in this order.

*Pi first, then phone:*

1. Start `collect --duration 600`. Confirm `status: streaming`.
2. Open R/Tach on the phone and let it try to connect.
3. Watch the state document for 60 seconds.

*Phone first, then Pi:*

4. Stop the collector. Connect the phone with R/Tach and leave it connected.
5. Start `collect --duration 300` and watch what happens.
6. Close R/Tach, disconnect the phone, and confirm the collector recovers.

```bash
.venv/bin/uniden-r8 collect --duration 600
# in a second shell
watch -n 5 'jq -c ".collector, .link" /home/jeremy/unidenr8/.state/state-v2.json'
```

**Do not change any detector setting from the app during this test.** R/Tach can
write settings; V9's snapshots are the baseline it would invalidate.

**Pass condition.** There is no single right answer here — the point is to
establish which one is true, and both are acceptable outcomes:

* The phone takes the link: the collector publishes `note: "link dropped"`,
  `status: reconnecting`, and `reconnects` increments. **Then check the
  backoff**: successive attempts must be roughly 5, 10, 20, 40 s apart with ±20%
  jitter, never tighter than about 4 s, capped at 300 s. A tight retry loop
  against a shared radio is the failure this test is really looking for.
* The Pi keeps the link and the phone cannot connect. Record what the app shows.

Then, either way: after the collector releases the link, does the detector start
advertising again? A `scan` at that moment answers question 6.7 as a side
effect.

```bash
.venv/bin/uniden-r8 scan --seconds 25 --save v11-scan-after.json
```

**Promotes.** Single-client arbitration from no evidence to OBSERVED, and
possibly `docs/EVIDENCE.md` 6.7 with it. Also produces a straight operational
answer for the RUNBOOK: whether Jeremy has to choose between the app and the Pi.

---

### V12 — Queue, gaps, clocks and a long session

**What it proves.** That the ingest queue, the gap record and the loop-lag
watchdog behave over hours rather than over a five-minute trial, and that a Pi
with no battery-backed clock does not corrupt the history when its wall clock
jumps.

**Procedure.** This rides on V10's drive; it needs no separate session. Do not
run it parked for hours with the engine off — the detector is powered from the
vehicle, and the collector's own voltage reading is the thing to watch.
`docs/EVIDENCE.md` §8.4 recorded 12.3 V early in one trial and 13.6 V later, and
was careful to say the cause of that change was not established; a voltage that
falls steadily over a long parked session is the signal to end it.

```bash
jq '.seq, .counters, .ingest, .health' "$UNIDEN_ROOT/.state/state-v2.json"
jq '[.recent_events[] | select(.kind == "gap")]' "$UNIDEN_ROOT/.state/state-v2.json"
.venv/bin/uniden-r8 history
.venv/bin/uniden-r8 history telemetry -n 20
```

**Pass condition.**

* `ingest.accepted` is approximately `seq`, and `ingest.dropped` is 0.
* `ingest.high_water` is far below the configured `queue_size` (256 by default).
  A high-water mark approaching the limit means the consumer is being starved,
  and `health.loop_lag_max_ms` should say why.
* `ingest.gaps` is 0. **If it is not**, that is the designed behaviour working:
  `recent_events` will contain a record of kind `gap` with `first_lost_seq`,
  `last_lost_seq` and `count`, and `collector.note` reads "notifications
  dropped". Record the exact sequence numbers — that is the whole reason the gap
  record exists, and a hole with a date on it is worth far more than a counter.
* History row counts are plausible: one telemetry row per
  `history.telemetry_every_seconds`, and one `alert_events` row per transition.
* `sessions.ended_at` is set, meaning the writer thread closed cleanly.
* **The clocks.** If the Pi cold-booted without a network, its wall clock was
  wrong at the start of the session and jumped when the network appeared. Check
  that `alert_events.at` timestamps around that jump are odd but that
  `duration_s` values are not — durations come from the monotonic clock and must
  be unaffected. That separation is the reason every record carries both clocks,
  and it has never been checked against a real jump.

**A note on provoking a gap.** You cannot reliably make one happen in the field:
the consumer drains on every wake, and 256 slots is many seconds of headroom at
1 Hz. The overflow and gap paths are exercised by the test suite instead.
`gaps: 0` is the expected field result, and any non-zero value is a finding
rather than a passing test.

**Promotes.** Nothing about the protocol. It promotes the collector's own
behaviour from "held for five minutes, parked" to "held for hours, moving", and
it is the second half of the gate the RUNBOOK puts in front of
`systemctl enable`.

---

## 6. How to record a result

### 6.1 Two destinations, and only two

**The raw capture stays in `.private/`.** Mode `0700`, files `0600`,
git-ignored. `.private/live-raw-*.json`, `.private/inspect-*.json`, and anything
either of them was decoded into. Do not move it out, do not copy it to a
workstation, do not paste it into a message, an issue or a commit. The salt that
makes every published token irreversible lives in the same directory, for the
same reason.

**The sanitized finding goes into `docs/EVIDENCE.md`.** A new numbered section
after §8, with the same shape as the ones already there: a UTC timestamp, what
was done, a table of numbered observations, each with a grade, and a plain
statement of what the run did *not* establish.

### 6.2 What may be written down, and what may not

| Write this | Not this |
|---|---|
| "31 of 31 telemetry packets parsed" | The payloads |
| "field 5 read 24.152 on a K-band alert" | The whole packet hex |
| "the POI blob grew 12 bytes, led by `0x03`" | The 12 bytes |
| "settings 1, offset 47: `0x02` → `0x03` when auto-dim was switched off" | A settings block dump |
| "signal was −69 dBm from the node in its installed position" | The detector's address |
| "the warning fired about 400 m before a red-light camera" | Which camera, or where it is |
| A salted token (`ble:…`) | Any address, in any spelling |

Never write a Bluetooth address, an address-shaped literal, a routable host
address, a coordinate pair, or the location of a POI or a user mark. Two
controls enforce this and both are worth knowing about:
`tests/test_repo_hygiene.py` scans every file git would commit — tracked and
untracked, with no exception list — and `evidence.publish()` refuses at runtime
rather than sanitizing, because silently fixing a bad string would hide the bug
that produced it.

### 6.3 A template

```markdown
## 9. <what was tested>, observed on Jeremy's R8

One <n>-second <live|collect|inspect> run from the node, <UTC timestamp>.
<One sentence on the conditions: parked, moving, what the emitter was.>

| # | Observation | Grade |
|---|---|---|
| 9.1 | <what happened, with a count> | OBSERVED |
| 9.2 | <what did not happen, stated as a negative result> | OBSERVED |
| 9.3 | <what the run could not tell> | not established |

### What this promotes

<Which fields move from UPSTREAM/UPSTREAM-UNVERIFIED/INFERENCE to OBSERVED,
named individually.>

### What this does not establish

<The limits, plainly. "Five minutes is not a drive" is the model.>
```

### 6.4 The code that must change with the grade

Promoting a field is four edits, not one:

| File | What changes |
|---|---|
| `docs/EVIDENCE.md` | The new observation section, and the grade on any §3 claim it supersedes |
| `src/uniden_r8/telemetry.py` | `FIELD_CONFIDENCE` — the per-field grade published in schema 2 and in `live --full` |
| `src/uniden_r8/gatt.py` | The `Characteristic.evidence` and `note` on the affected catalogue entry |
| `README.md` | The capability matrix row |

If a payload turned out to have a different shape, add a fixture built from the
sanitized capture to `tests/`. An alert payload contains no address and makes a
legitimate fixture. **A POI payload never does**, under any circumstances.

---

## 7. If a capture contradicts the documented protocol

### 7.1 The rule

**The bytes win.** Every protocol statement in this repository is a claim about
what a device does, and a capture is the device answering. Do not adjust a
parser to make an awkward capture fit; record the capture, state that the
documented shape is wrong, and change the decoder afterwards with the bytes in
hand.

Nothing is promoted by argument. A field does not become OBSERVED because the
reasoning is good, and INFERENCE never becomes OBSERVED without a measurement.

### 7.2 The coordinate tripwire

This is the contradiction with the largest blast radius, and it has its own
mechanism because of that.

**What it is.** `_parse_gps_group` checks sub-fields 0 and 1 of the telemetry
GPS group. If both read as signed decimals inside ±180 with a fractional part,
the group is flagged `suspect_pair`, everything in it is discarded, and nothing
from it is published. `_looks_like_coordinate` is deliberately loose: it is a
tripwire, not a decoder.

**Why it exists.** This project's claim that the detector puts no latitude or
longitude on the wire rests on upstream's *naming* of four sub-fields, not on a
measurement. Four fields is also exactly the right width for latitude,
longitude, altitude and status. If the naming is wrong, the two most sensitive
bytes on the wire have been flowing through a parser that thought they were a
heading and a speed.

**How to check it, every time.**

```bash
jq '.detector.detector_gps.suspect_coordinate_pair' "$UNIDEN_ROOT/.state/state-v2.json"
```

Check it deliberately, because nothing else will tell you. The flag exists only
in schema 2. Schema 1 shows `gps_locked: null` and nothing more, the packet is
still counted as `parsed`, and no unparsed counter moves — a run that fired the
tripwire on every packet looks, from schema 1, like a detector with a poor GPS
fix.

**If it fires.**

1. **Stop the collector.** Do not start another session.
2. **Rule out the boring explanation first.** The tripwire needs sub-field 0 to
   be a signed decimal, and a compass point is not. Look at the raw hex: is the
   packet simply malformed, truncated, or a different shape? Compare
   `field_count` and `shape`. A `short-N` or `extended-N` shape alongside the
   flag points at a firmware change, not at a coordinate.
3. **If the group really is a coordinate pair**, treat every existing artefact
   as containing positions: every `.private/live-raw-*.json`, every
   `state-v2.json`, and every `telemetry` row written with
   `record_detector_motion = true`. None of it may be published, and the history
   database is now a record of where the vehicle has been.
4. **Write it up before changing code.** This project's premise, `gnss.py`'s
   reason for existing, the schema's separation of `detector_gps` from
   `vehicle_gnss`, `docs/SAFETY.md` §3, and every "the detector sends no
   coordinates" sentence in the repository are all wrong together, and they
   should be corrected together rather than one file at a time.
5. The correct code change is **not** to decode the field. It is to decide
   deliberately what the project does with a coordinate the detector supplies —
   which is a conversation, not a commit.

### 7.3 The other contradiction classes

Each of these is a counter that already exists. None of them is a crash, and all
of them are visible in schema 2.

| Symptom | Means | First action |
|---|---|---|
| `detector.shape` is `extended-N` | Firmware appended a field. Values still decode positionally; schema 1 blanks them and reports `shape_confirmed: false` | Capture under `live`, record the new field's raw text |
| `detector.shape` is `short-N` or `unreadable` | The packet is shorter than seven fields; nothing lines up | Keep the bytes; do not guess at an alignment |
| `counters.unparsed_telemetry` rising | Voltage unreadable, or the shape is not `confirmed-7` | Compare against `shape` in the same document |
| `counters.unreadable_slots` rising | An alert slot arrived and the gate rejected it — a band name, a strength outside 1–8, or a direction letter that is not `F`/`S`/`R` | This is a protocol finding. Capture it under `live` |
| `counters.unknown_mute_codes` rising | The detector used a mute code outside 1–6 | Record the raw code; do not extend `MUTE_STATES` without a capture |
| `link.compatible` false, `services_missing` non-empty | The attribute table moved. The live path refused and read nothing | The compatibility gate doing its job. Re-run `identity` and compare the service list |
| `field_5_raw` present but `frequency_ghz` null on a radar band | Field 5 was not numeric where the band said it should be | The tagged-union split in `_parse_slot` is wrong for that band |

### 7.4 What never happens in response to a contradiction

* No parser is loosened to make a rejected slot pass. A slot that decodes
  wrongly is worse than a slot that is counted as unreadable, because the wrong
  reading reaches the history and the display and the broker.
* No write is attempted to ask the detector what it meant. There is no
  application-write path, the capability is absent rather than disabled, and a
  confusing capture is not a reason to add one. `docs/SAFETY.md` sets out the
  route if a write is ever genuinely wanted, and it starts with Jeremy's
  explicit approval and a written review, not with code.
* No raw capture leaves `.private/` to be looked at somewhere more convenient.

---

## 8. Known defects to fix or work around before the vehicle

Found by reading the code against this plan, and listed here because each one
will be met by somebody standing at the car.

| Defect | Effect | Workaround |
|---|---|---|
| `LiveSession._render_detail` reads `poi.kind`, `poi.distance_raw` and `poi.speed_limit_raw`, which `PoiWarning` does not have | `live --full` raises `AttributeError` on the first active POI warning — exactly during V7 | Use `--json --full` |
| `_safe_word` rejects any string containing a comma | Telemetry field 1's active form is recorded as `raw: null`; the collector keeps no other trace of it | Run V7 under `live` and read the raw capture |
| `HistoryWriter.record_fix` is never called | The `gnss_fixes` table is always empty; GNSS data survives only where it is attached to an alert event | Read `vehicle_gnss` from `state-v2.json` during the session, not from the history afterwards |
| `uniden-r8 history events --json` runs the rows through `publish()`, which refuses a document containing a coordinate | An uncaught `PublicationRefused` traceback when `record_coordinates` was on | Keep `record_coordinates = false`; the text table is unaffected |
| The text history table prints `lat`/`lon` columns that the JSON path would refuse | The two output paths disagree about publishing coordinates | Same |
| `inspection.py`'s docstring quotes upstream's POI record lengths as 13, 12 and 10 bytes; `CANDIDATE_RECORD_LENGTHS`, which the boundary walk uses, says 15, 14 and 12 | The printed candidate boundaries follow the constant, not the prose | Settle which is right from the V8 capture, then correct the other |
| `[collector] stale_after_seconds` is validated and never read; the collector uses a hard-coded 10 s | Setting it has no effect | Do not rely on it |
| The dashboard reads `d.telemetry` (schema 1 only, absent from schema 2, which calls it `detector`) and `d.detector_gps` (absent from both) | Every tile shows an em-dash when `[feed] detail = true`; the heading tile never shows a compass point either way | Leave `feed.detail = false` |

None of these affects the safety boundary: no write path, no OBD interaction and
no publication of an address is involved in any of them.
