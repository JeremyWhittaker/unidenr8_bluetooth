# Schema and consumer contract

Everything this project publishes, field by field, with the rules a consumer has
to follow to read it safely.

`docs/ARCHITECTURE.md` explains *why* the shapes are what they are.
`docs/EVIDENCE.md` records where each protocol fact came from. This file is the
reference: what the keys are, what types they hold, what they mean, and what a
reader must not assume.

Evidence grades are used here exactly as they are in `docs/EVIDENCE.md`:

| Grade | Means |
|---|---|
| **OFFICIAL** | Uniden said it: product page, owner's manual, support article, release note. |
| **OBSERVED** | Seen on Jeremy's own non-W R8 by this project. |
| **UPSTREAM** | Captured on an **R8w** by `AegisX86/UnidenR8wlink` @ `9072bc2f`. |
| **UPSTREAM-UNVERIFIED** | In that upstream, documented there as never tested on hardware. |
| **INFERENCE** | This project's reasoning. Not observed anywhere. |

---

## Two facts to read everything else through

**1. The live BLE telemetry carries no latitude and no longitude.** The GPS
sub-group of a telemetry packet has four comma-separated parts, and that
four-part shape is OBSERVED on this R8 — 31 of 31 packets, then 293 of 293
(`docs/EVIDENCE.md` §7.3, §8.1). The *reading* of those four parts as a
heading to the nearest of eight compass points, a speed, an altitude and a
status letter is UPSTREAM: it is how `AegisX86/UnidenR8wlink` named them on an
R8w, not something this project measured. The detector plainly knows where it
is — its red-light-camera warnings depend on it — but no coordinate has ever
been seen on the wire.

Coordinates therefore come from somewhere else. `uniden_r8/gnss.py` reads them
from `gpsd`, and they live in their own branch of the schema, `vehicle_gnss`,
next to and never merged with the detector's own `detector_gps`. A consumer
that finds a coordinate in this project's output is looking at a GNSS receiver's
answer, not the detector's.

Because the four-part naming is UPSTREAM rather than measured,
`telemetry.py::_parse_gps_group` carries a tripwire. If sub-fields 0 and 1 both
read as signed decimals inside coordinate range with a fractional part, the
group is flagged `suspect_coordinate_pair`, nothing is decoded from it, and
nothing is published from it. If that flag is ever true in a real document, this
section is wrong and the flag is how you find out.

**2. The only alert payload this R8 has ever produced is all-clear.** One
bounded receive and one five-minute collector trial each read the alert
characteristic once and saw no alert notification at all (`docs/EVIDENCE.md`
§7.6, §8.6). That is OBSERVED. Every field of an *active* alert — band,
strength, raw signal, the frequency-versus-gun-identifier split, direction, mute
codes above 2, and field 8 entirely — is UPSTREAM or weaker. The decoder for
them is written, tested against fixtures, and has never seen a real detection on
this hardware.

So a consumer must treat an active-alert record as a decode of an unconfirmed
format. The `confidence` map in the schema-2 document carries that grading per
field, and it is published rather than kept in a comment for exactly this
reason.

---

## Where each artifact lives

| Artifact | Path | Mode | Committed? | Schema | Written when |
|---|---|---|---|---|---|
| `state.json` | `<state_dir>/state.json` | `0600` in a `0700` directory | no — `.state/` is git-ignored | 1 | always, by the collector |
| `state-v2.json` | `<state_dir>/state-v2.json` | `0600` in a `0700` directory | no | 2 | only when `collector.detail = true` (the default) |
| `collector.lock` | `<state_dir>/collector.lock` | `0600` | no | n/a | held for the life of one collector |
| History database | `history.path`, relative paths resolving against `<state_dir>`; default `history.db` | `0600` | no | 1 (`storage.SCHEMA_VERSION`) | only when `history.enabled = true` |
| Raw packet captures | `.private/live-raw-<stamp>.json` | `0600` in a `0700` directory | no | n/a | only by the one-shot `live` command |

`<state_dir>` is `collector.state_dir`, default `.state`, overridable per run
with `uniden-r8 collect --state-dir`. Both state files are written atomically
via a temporary file and `os.replace`, so a reader sees the previous document or
the new one and never a partial. `state-v2.json` is written **first** and
`state.json` **last**: a consumer polling both is better off seeing schema 2
momentarily ahead than momentarily behind (`collector.py::publish_state`).

The collector does not retain raw payloads. Only the one-shot `live` diagnostic
writes them, and only into `.private/`.

---

## `state.json` — schema 1

### The freeze

**This shape is frozen.** A consumer that requires `schema == 1` already exists
— the e-paper display in the sibling `hummer_obdII` project — and it was written
against exactly these keys. `tests/test_collector.py::
test_state_json_is_still_schema_one_with_exactly_its_original_keys` pins the
top-level key set, and the `collector`, `link` and `counters` key sets, as
literals rather than against the module constant, because asserting
`doc["schema"] == SCHEMA_VERSION` is a tautology that stays green while the
consumer breaks.

Adding a key inside this document is safe. Changing what an existing key
*means* is not, and neither is removing one. A consumer that ignores keys it
does not recognise keeps working across an addition; nothing can protect it from
a redefinition. That is the whole reason the full decoded surface went into a
second file with a second version number instead of being folded in here.

### Top level

| Key | Type | Meaning |
|---|---|---|
| `schema` | int | Always `1`. A consumer **must** require this exactly. |
| `updated_at` | string | When this document was written. ISO-8601 UTC, **second** resolution, `YYYY-MM-DDTHH:MM:SSZ`. |
| `collector` | object | The process. |
| `obd` | object | The vehicle's RFCOMM link, as observed read-only. |
| `link` | object | The detector link. |
| `counters` | object | Monotonic counts for the life of the process. |
| `telemetry` | object | The conservative view of the most recent packet. |
| `alerts` | array | Recognised alerts from the most recent snapshot, at most 8. |
| `display_line` | string | A preformatted status line, at most 32 characters. |

### `collector`

| Key | Type | Meaning |
|---|---|---|
| `mode` | string | `"continuous"`, or `"trial"` when the run was started with `--duration`. |
| `status` | string | One of `starting`, `connecting`, `streaming`, `reconnecting`, `obd-blocked`, `incompatible`, `degraded`, `stopped`. |
| `started_at` | string | When the process started. ISO-8601 UTC, second resolution. |
| `reconnects` | int | How many times a session ended and the loop went round again. |
| `note` | string | A short fixed phrase, or `""`. Never free text and never exception text. |

The `note` vocabulary is a closed set: `""`, `"partial subscription"`,
`"link dropped"`, `"notifications dropped"`, `"trial deadline"`,
`"session timed out"`, `"session failed"`, `"clean shutdown"`, and
`"N required attribute(s) absent"`. BlueZ error strings contain device
addresses, so a failed session is recorded as the fixed phrase
`"session failed"` and the exception text is discarded rather than published.

`obd-blocked` is not an error. It means the OBDLink health probe answered no and
the collector let go of the detector on purpose. Investigate the OBD side.

`degraded` means the link was up and compatible but neither subscription could
be established.

### `obd`

| Key | Type | Meaning |
|---|---|---|
| `healthy` | bool | All three checks passed. The detector link is only opened when this is true. |
| `rfcomm_active` | bool | `systemctl is-active <obd.unit>` answered `active`. |
| `device_present` | bool | The configured device node exists. It is never opened. |
| `bound` | bool | `rfcomm` with no arguments lists a binding for that node. |
| `reason` | string | Why it is unhealthy, from a fixed vocabulary; `""` when healthy. |

`reason` is one of `""`, `"<unit> is not active"`, `"<device> is missing"`,
`"<device> is not bound"`, or `"guard disabled by configuration"`. It is a fixed
phrase and not command output, because command output can contain an address and
this document is published.

When `obd.guard = false` the probe is replaced by one that always answers
healthy with `reason = "guard disabled by configuration"`. Schema 1 has no field
saying the gate was disarmed; schema 2's `obd.guard_enabled` does.

### `link`

| Key | Type | Meaning |
|---|---|---|
| `connected` | bool | A BLE link to the detector is currently held. |
| `compatible` | bool | The connected device exposed every attribute the live path needs. |

Compatibility is re-checked against the live device on every connect rather than
assumed from the catalogue. The vendor UUIDs were documented on an R8w
(UPSTREAM) and confirmed present on this R8 (OBSERVED, `docs/EVIDENCE.md` §6.10),
but a firmware update could move the table and a gate that is only correct today
is not a gate.

### `counters`

| Key | Type | Meaning |
|---|---|---|
| `telemetry_packets` | int | Telemetry notifications and reads processed since start. |
| `alert_packets` | int | Alert snapshots processed since start. |
| `unparsed_telemetry` | int | Telemetry packets that did not reach the confirmed shape. |

These are monotonic for the life of the process and reset on restart. They do
not reset on reconnect.

### `telemetry`

| Key | Type | Meaning |
|---|---|---|
| `voltage` | float or null | Supply voltage in volts. OBSERVED — steady 13.6 V across 31 packets, `docs/EVIDENCE.md` §7.4. |
| `gps_locked` | bool or null | Tri-state. See below. |
| `poi_warning` | bool | Whether the detector is warning about a saved point of interest. |
| `shape_confirmed` | bool | Whether the packet had exactly the seven-field shape this R8 produces. |
| `age_s` | float or null | Seconds since the most recent packet, rounded to 0.1. `null` before the first packet. |
| `stale` | bool | True when `age_s` exceeds 10 seconds. |

**`shape_confirmed` is the one key added to schema 1 since it was frozen**, and
it is the illustration of why adding is safe. A consumer that never heard of it
reads `voltage`, `gps_locked` and `poi_warning` exactly as before. A consumer
that does read it gains the ability to tell "the detector reported 13.6 V" from
"a packet of an unexpected shape decoded to something that looked like 13.6 V".

The three readings are gated on it. When `shape_confirmed` is false, `voltage`
and `gps_locked` are `null` and `poi_warning` is `false`, *even if the packet
decoded*. The decoded values are still available, with their grades attached, in
the schema-2 document. Schema 1 has no way to express "probably", so it reports
absence, which is what a consumer with no way to express doubt should be told.

`gps_locked` is deliberately tri-state:

| Value | Means |
|---|---|
| `true` | The GPS sub-group was present and its status letter was `C`. |
| `false` | The sub-group was a bare `0` — the detector saying it has no fix. |
| `null` | Either the packet could not be evaluated, or the sub-group was present with a status letter other than `C`. |

`C` while a fix was present is OBSERVED on this R8 (`docs/EVIDENCE.md` §7.5).
That `C` *means* "connected", and what any other letter means, is not
established. `null` is the honest answer for an unknown letter and must not be
collapsed into `false`.

`stale` uses a hard-coded 10-second threshold (`collector.STALE_AFTER_SECONDS`).
The configuration file accepts `collector.stale_after_seconds` and range-checks
it, but nothing reads it — see [What the code does not do](#what-the-code-does-not-do).

### `alerts`

An array of at most 8 objects, from the most recent alert snapshot. An empty
array is a positive statement — the characteristic sends a full snapshot on
every change rather than a delta, so "no alerts" means the detector reported
none, not that information is missing.

| Key | Type | Meaning | Grade |
|---|---|---|---|
| `band` | string | An allowlisted band name, or `"unknown"`. | UPSTREAM |
| `strength` | int or null | 1 to 8. `null` if it was not an integer. | UPSTREAM |
| `frequency_ghz` | float or null | Radar frequency. `null` for laser and the photo-radar types. | UPSTREAM |
| `direction` | string | `"front"`, `"side"`, `"rear"`, or `"unknown"`. | UPSTREAM |
| `muted` | bool | **Two-state here**, unlike schema 2: an unrecognised mute code publishes as `false`. | UPSTREAM |

`band` is mapped through an allowlist —
`{X, K, KA, LASER, MRCD, MRCT, RT3, RT4, K POP, KA POP}` — and anything else
becomes `"unknown"`. `direction` is mapped the same way. This document is a
published artifact and must not carry an arbitrary string echoed from the
device.

`frequency_ghz` is a tagged union in the wire format. For the radar bands field
5 is a frequency; for laser it is a gun-type identifier; for `MRCD`, `MRCT`,
`RT3` and `RT4` it is neither, and reading it as GHz would invent a number. Only
the radar bands produce a `frequency_ghz` here. The laser gun identifier is
schema-2 only.

### `display_line`

One preformatted line, at most 32 characters, for a device that cannot parse
JSON. It is a *status* line, not a radar display: the e-paper panel it was
written for is 250×122 and refreshes every five minutes, and a five-minute-old
alert is not an alert.

Forms, in priority order: `R8 paused: OBD link`, `R8 collector stopped`,
`R8 connecting...`, `R8 incompatible`, `R8 linked, no data`,
`R8 <v>V <gps> STALE`, `R8 <v>V <band> <n>/8 <direction>`, `R8 <v>V <gps> clear`.

**A careful consumer should not use it.** The sibling display deliberately
ignores it and rebuilds its own line from the typed fields, because a
preformatted string is a shape that can change without a schema bump.

### Worked example

A complete `state.json`, generated from the code in this repository:

```json
{
  "alerts": [
    {
      "band": "KA",
      "direction": "front",
      "frequency_ghz": 34.712,
      "muted": false,
      "strength": 5
    }
  ],
  "collector": {
    "mode": "continuous",
    "note": "",
    "reconnects": 1,
    "started_at": "2026-09-02T05:12:44Z",
    "status": "streaming"
  },
  "counters": {
    "alert_packets": 63,
    "telemetry_packets": 1174,
    "unparsed_telemetry": 0
  },
  "display_line": "R8 13.6V KA 5/8 front",
  "link": {
    "compatible": true,
    "connected": true
  },
  "obd": {
    "bound": true,
    "device_present": true,
    "healthy": true,
    "reason": "",
    "rfcomm_active": true
  },
  "schema": 1,
  "telemetry": {
    "age_s": 0.4,
    "gps_locked": true,
    "poi_warning": false,
    "shape_confirmed": true,
    "stale": false,
    "voltage": 13.6
  },
  "updated_at": "2026-09-02T05:31:07Z"
}
```

The alert in that example is synthesised from a fixture. No such packet has been
seen on this detector.

### What a consumer of schema 1 must do

1. **Require `schema == 1` exactly.** Not `>= 1`. A future schema 3 in this file
   would mean the meanings changed, and a consumer that accepts it is a consumer
   that misreports.
2. **Validate every type before using it.** `voltage` can be `null`.
   `gps_locked` can be `null`. `strength` can be `null`. A display that assumes
   a float gets an exception in a vehicle.
3. **Reject stale data, on your own clock.** Parse `updated_at`, compare it
   against now, and fall back if the gap is larger than your refresh interval.
   Do not trust `telemetry.stale` alone: it measures the age of the last *BLE
   packet* relative to the last *write*, and says nothing about whether the file
   itself is being written. A collector killed with `SIGKILL` leaves a document
   whose `stale` is `false` forever.
4. **Reject an implausibly future `updated_at` too.** The node is a Pi Zero 2 W
   with no battery-backed clock; its wall clock is wrong at every cold boot and
   jumps when the network appears.
5. **Ignore keys you do not recognise.** That is what makes an addition safe.
6. **Allowlist the strings you act on.** `status`, `band` and `direction` are
   allowlisted on the producing side; allowlist them again on yours, and treat
   anything unexpected as unknown rather than rendering it.

---

## `state-v2.json` — schema 2

### What it is, and who it is not for

Schema 2 is a superset of schema 1 in content and a separate file in form. It
carries the full decoded packet surface, the detector's own heading, speed and
altitude, the ingest and loop-health metrics, the derived alert tracks, the sink
status, and the external GNSS branch.

**It carries position-adjacent data, and coordinates when the operator turns
them on.** A log of heading, speed and altitude at 1 Hz is a rough trace of a
drive. So the file is `0600` inside a `0700` directory, `.state/` is
git-ignored, and `evidence.publish()` refuses to print it. It is **not** what a
display or a broker receives by default: `mqtt.detail` and `feed.detail` both
default to `false`, and those sinks send the schema-1 document unless changed.

It is written only when `collector.detail` is true, which is the default —
the point of decoding the whole packet is to be able to look at it, and it lands
in the same owner-only directory either way.

Schema 2 is **not frozen**. It is expected to grow. Version it with `schema == 2`
and treat every branch as optional.

### Top level

| Key | Type | Meaning |
|---|---|---|
| `schema` | int | Always `2`. |
| `updated_at` | string | ISO-8601 UTC with **milliseconds**, `YYYY-MM-DDTHH:MM:SS.mmmZ`. Note this differs from schema 1's second resolution. |
| `seq` | int | Sequence number of the last notification the consumer loop processed. |
| `collector` | object | As schema 1, plus `adapter`. |
| `obd` | object | As schema 1, plus `guard_enabled`. |
| `link` | object | As schema 1, plus `last_packet_age_s` and `stale`. |
| `counters` | object | Schema 1's three, plus three more. |
| `ingest` | object | The queue between the BLE callback and the consumer. |
| `health` | object | Event-loop lag and inter-packet timing. |
| `detector` | object or null | The full decoded telemetry packet. `null` before the first packet. |
| `vehicle_gnss` | object or null | The external GNSS fix. `null` when GNSS is off, absent, or stale. |
| `alerts` | array | The most recent snapshot, decoded in full, at most 8. |
| `open_tracks` | array | Derived alert tracks currently open. |
| `recent_events` | array | The last 20 derived events and gap records, **newest first**. |
| `sinks` | object | Per-sink status. |
| `confidence` | object | The evidence grade for each decoded field. |

`collector.adapter` is the pinned BlueZ controller (for example `hci1`) or
`null` when BlueZ picks. `obd.guard_enabled` records whether the OBD gate was
armed at all, which schema 1 cannot express.

`link.last_packet_age_s` is the same measurement as schema 1's
`telemetry.age_s`, rounded to milliseconds instead of tenths.

`counters` adds `unparsed_alert_packets` (snapshots with at least one
undecodable slot), `unreadable_slots` (individual slots that failed), and
`unknown_mute_codes` (slots whose mute code was outside the documented set).

### `detector`

The decoded telemetry packet. `null` until one arrives.

| Key | Type | Meaning | Grade |
|---|---|---|---|
| `voltage_v` | float or null | Supply voltage. | OBSERVED |
| `detector_gps` | object | The GPS sub-group. See below. | mixed |
| `poi_warning` | object | The POI sub-group. See below. | mixed |
| `unknown` | object | Fields 3 to 6, verbatim, under neutral names. | UPSTREAM / INFERENCE |
| `field_count` | int | How many `&`-separated fields the packet had. | OBSERVED |
| `shape` | string | `confirmed-7`, `extended-N`, `short-N`, or `unreadable`. | OBSERVED |
| `parsed` | bool | True only when the voltage decoded **and** the shape was `confirmed-7`. | — |

`shape` is the grading of the packet layout:

| Value | Means |
|---|---|
| `confirmed-7` | Exactly seven fields — the shape this R8 produces, 324 times out of 324. |
| `extended-N` | More than seven. Decoded positionally anyway: a firmware update that appends a field should not blank the voltage on a display. |
| `short-N` | Two to six fields. Nothing is decoded; there is nothing to line the values up against. |
| `unreadable` | One field or none. |

Note the asymmetry, and that it is deliberate. An extended packet is decoded and
labelled; a short one is not decoded at all.

#### `detector.detector_gps`

**No coordinate appears here.** See [Two facts](#two-facts-to-read-everything-else-through).

| Key | Type | Meaning | Grade |
|---|---|---|---|
| `evaluated` | bool | The group was well-formed enough for the other flags to mean anything. | — |
| `present` | bool | The group carried a fix at all — a bare `0` means it did not. | OBSERVED |
| `locked` | bool or null | Tri-state, as schema 1's `gps_locked`. | mixed |
| `status_raw` | string or null | The status letter exactly as sent. `C` seen with a fix. | OBSERVED |
| `direction_8` | string or null | One of `N NE E SE S SW W NW`, or `null` if unrecognised. | UPSTREAM |
| `speed_raw` | int or null | A speed. Unit unverified. | UPSTREAM |
| `speed_unit` | string | The literal `"unknown (upstream reads it as mph)"`. | — |
| `altitude_raw` | int or null | An altitude. Unit unverified. | UPSTREAM |
| `altitude_unit` | string | The literal `"unknown (upstream reads it as feet)"`. | — |
| `suspect_coordinate_pair` | bool | The tripwire. If true, nothing was decoded from this group. | — |

The unit fields are strings saying "unknown" on purpose. The R8w writeup reads
these as miles per hour and feet; this project has never checked either against
a moving vehicle, and a consumer that wants to display a speed should show the
raw number rather than assert a unit nobody measured.

There are three states, not two, for a reason: "the detector reports no fix"
(`evaluated: true, present: false`) and "the packet could not be read"
(`evaluated: false`) are different answers, and collapsing them lets a decode
failure masquerade as a measurement.

#### `detector.poi_warning`

| Key | Type | Meaning |
|---|---|---|
| `active` | bool | Something is being warned about. |
| `raw` | string or null | The field verbatim, if short and alphanumeric. |
| `decoded` | null | Always null. There is no parser. |

`decoded` is permanently `null` and that is the honest answer. The only POI
field ever observed on either detector is the literal `0` (OBSERVED). The
three-part active form is upstream's reading of a decompiled app, and a
structure nobody has seen populated does not get a parser that can appear to
succeed.

Note the type change between documents: schema 1's `telemetry.poi_warning` is a
**boolean**; schema 2's `detector.poi_warning` is an **object**.

#### `detector.unknown`

Fields 3 to 6 of the packet, kept verbatim under neutral names
`field_3_raw` … `field_6_raw`, each `null` or a short validated token. The
`upstream_names` sub-object records what the R8w writeup calls them:

| Neutral name | Upstream's name |
|---|---|
| `field_3_raw` | `warning` |
| `field_4_raw` | `scanCount` |
| `field_5_raw` | `wifi status (R8w; this model has no Wi-Fi)` |
| `field_6_raw` | `brightness status` |

Field 5 is the reason for the indirection. Uniden's own product page says the R8
has no Wi-Fi and that the R8w is the Wi-Fi model (OFFICIAL, `docs/EVIDENCE.md`
§1.2). Whatever byte sits at index 5 on a non-W unit, publishing it under the
name "wifi" would assert a capability the manufacturer says the product does not
have.

### `vehicle_gnss`

The external fix, from `gpsd`. `null` when GNSS is disabled, when there is no
2D/3D fix, or when the last fix is older than `gnss.stale_after_seconds`.
Staleness is judged on the monotonic clock: returning a ten-second-old position
would attach a coordinate to an alert that happened somewhere else.

| Key | Type | Meaning |
|---|---|---|
| `source` | string | Always `"gpsd"`. |
| `mode` | int | gpsd fix mode: 0 unknown, 1 no fix, 2 2D, 3 3D. |
| `mode_name` | string | The same, named. |
| `valid` | bool | `mode >= 2`. |
| `speed_mps` | float or null | Ground speed, metres per second. |
| `speed_mph` | float or null | The same, converted, for comparison with the detector's own reading. |
| `track_deg` | float or null | Course over ground. |
| `climb_mps` | float or null | Vertical velocity. |
| `altitude_m` | float or null | Altitude in metres. |
| `epx_m`, `epy_m` | float or null | gpsd's 95%-confidence longitude and latitude error estimates, metres. |
| `satellites` | int or null | Satellites used in the fix, from the most recent SKY report. |
| `device_time` | string or null | gpsd's own timestamp, verbatim. |
| `age_s` | float | Seconds since the fix, monotonic, rounded to 0.01. |
| `lat`, `lon` | float or null | Coordinates. |
| `coordinates_withheld` | bool | Present and `true` **only** when coordinates are off. |

`lat` and `lon` are always **present** keys. With
`gnss.record_coordinates = false` — the default — they are `null` and
`coordinates_withheld` is added. That distinction matters: a consumer must be
able to tell "coordinates are switched off" from "this build has no GNSS at
all", and an absent key answers neither.

Keep `epx_m` and `epy_m` if you keep the fix. A five-metre fix and a
five-hundred-metre fix are not the same fact, and a coordinate without an
accuracy estimate is a number without a claim attached.

### `alerts`

Each entry is the full decoded slot, with the collector's allowlist re-applied
to `band` and `direction`.

| Key | Type | Meaning | Grade |
|---|---|---|---|
| `slot` | int | Position in the snapshot. **Not an identity** — the detector re-orders slots as signals rise and fall. | UPSTREAM |
| `band` | string | Allowlisted band, or `"unknown"`. | UPSTREAM |
| `strength_1_to_8` | int | Bars, 1 to 8. | UPSTREAM |
| `raw_signal` | int | The finer-grained signal number behind the bars. | UPSTREAM |
| `frequency_ghz` | float or null | Radar frequency, for the radar bands only. | UPSTREAM |
| `laser_gun_id` | int or null | Field 5 when the band is laser. | UPSTREAM-UNVERIFIED |
| `laser_gun` | string or null | The gun type name, looked up by bounds check. | UPSTREAM-UNVERIFIED |
| `field_5_raw` | string or null | Field 5 exactly as sent, whatever it turned out to mean. | OBSERVED (as bytes) |
| `direction` | string | `"front"`, `"side"`, `"rear"`, or `"unknown"`. | UPSTREAM |
| `direction_code` | string or null | The raw `F`/`S`/`R`. | UPSTREAM |
| `mute_code` | string or null | Field 7 verbatim. | UPSTREAM |
| `mute_state` | string | Decoded name, or `"unknown"`. | mixed |
| `muted` | bool or null | **Tri-state here**, unlike schema 1. | UPSTREAM |
| `alert_id_raw` | string or null | Field 1. Always `00` in every capture on either model. | UPSTREAM |
| `receive_mode_raw` | string or null | Field 8, verbatim. No reading of it is established. | UPSTREAM-UNVERIFIED |
| `field_count` | int | How many comma-separated fields the slot had. | — |
| `parsed` | bool | Always `true` for an entry that appears here. | — |

`muted` being tri-state is the difference that matters. Schema 1 publishes
`false` for an unrecognised mute code because it has nowhere to put "unknown";
schema 2 publishes `null`, because saying "not muted" about a code nobody has
decoded is a claim rather than a reading.

Only mute codes 1 and 2 have hardware support anywhere — upstream captured a
physical mute-button press moving 1 to 2 (UPSTREAM). Codes 3 to 6 come from the
decompiled app and may be wrong (UPSTREAM-UNVERIFIED).

`laser_gun` is published alongside `laser_gun_id` and never instead of it, so a
wrong name cannot hide the real value. No laser packet has been captured on
either model.

The decoder is strict about the fields every consumer keys on — the active
marker, the band, the 1-to-8 strength range, the direction — and lenient about
everything else. An unlisted mute code, an absent frequency or an unknown
receive mode leaves that one value unknown and publishes the alert anyway.
Losing a real Ka warning over field 7 would be the worst thing this parser could
do.

### `ingest`

The queue between the BLE notification callback and the consumer.

| Key | Type | Meaning |
|---|---|---|
| `accepted` | int | Notifications enqueued since start. |
| `dropped` | int | Notifications evicted to make room. |
| `gaps` | int | Contiguous runs of loss, from the queue's own accounting. |
| `high_water` | int | Deepest the queue has been. |
| `depth` | int | Current depth. |
| `lost_notifications` | int | The collector's own count: queue losses plus enqueue failures. |

`gaps` and `lost_notifications` are the collector's counters and are set from
the gap records it consumed; `accepted`, `dropped`, `high_water` and `depth`
come from the queue. In a healthy run everything but `accepted` and `high_water`
is zero.

The queue drops its **oldest** entry when full. For a radar integration the
newest snapshot is the one worth having, and blocking the callback is not an
option: on BlueZ a client that stops draining its D-Bus socket is disconnected
rather than merely delayed.

### `health`

| Key | Type | Meaning |
|---|---|---|
| `loop_lag_ms` | float | How late the watchdog's own 250 ms timer last fired, in milliseconds. |
| `loop_lag_max_ms` | float | The worst value seen since start. |
| `loop_lag_alarm` | bool | True when `loop_lag_max_ms` exceeds 1000. |
| `telemetry_interval` | object | `samples`, `median_s`, `p95_s`, `max_s` over the last 120 inter-packet intervals. All but `samples` are `null` until two packets have arrived. |

`telemetry_interval` is published for a reason that is not obvious. The OBD
health probe asks three *state* questions and all three stay green while radio
contention quietly triples this link's latency. A widened 95th percentile
against the 0.97–1.02 s baseline recorded in `docs/EVIDENCE.md` §7.2 is the only
cheap signal this project has for coexistence trouble.

`loop_lag_alarm` firing means something on the event loop blocked for the best
part of a second, which is the same order as a D-Bus disconnect threshold.

### `open_tracks`

One entry per derived alert track that is currently open. **A track is an
inference, not something the protocol provides** — see
[The alert event records](#the-alert-event-records).

| Key | Type | Meaning |
|---|---|---|
| `track_id` | int | Process-local, starts at 1, never reused within a session. |
| `family` | string | The band family: `K`, `KA`, `X`, `LASER`, `PHOTO`, `RT`, or the band itself if unrecognised. |
| `band` | string | The band as of the most recent observation. |
| `directions` | string | The sequence of direction codes seen, e.g. `"FSR"`. |
| `laser_gun_id` | int or null | Part of the track's identity for laser. |
| `samples` | int | Snapshots this track has appeared in. |
| `duration_s` | float | First to most recent observation, monotonic, rounded to 0.001. |
| `max_strength` | int or null | Peak bars. |
| `max_raw_signal` | int or null | Peak raw signal. |
| `min_frequency_ghz`, `max_frequency_ghz` | float or null | The frequency range observed. |
| `ambiguous` | bool | At some point a runner-up track scored nearly as well as the winner. |

The aggregates matter as much as the current values: an alert usually fades
before it ends, so "how strong did that get" cannot be answered from the last
snapshot.

### `recent_events`

The last 20 records, **newest first**. Each entry is either an alert event
record or a gap record — see the two sections below. They share the array, so
switch on `kind`: `alert_start`, `alert_update`, `alert_end`, or `gap`.

This is a bounded convenience view for a dashboard. It is not the record. The
SQLite history is, and only when `history.enabled` is true.

### `sinks`

Per-sink status. A sink that is not configured is `{"enabled": false}` and
nothing more.

`history`: `enabled`, `healthy`, `path`, `queued`, `written`, `dropped`,
`errors`, `error`. `dropped` is rows the write queue refused; `errors` is rows a
failed commit lost. A history that quietly stopped recording would be worse than
no history at all, so both are counted and published.

`gnss`: `enabled`, `connected`, `host`, `port`, `connects`, `reports`,
`malformed`, `record_coordinates`, `last_error`, `fix` (the same object as
`vehicle_gnss`, or `null`).

`mqtt`: `enabled`, `connected`, `host`, `port`, `base_topic`, `detail`,
`published`, `errors`, `last_error`.

`feed`: `enabled`, `bind`, `port`, `clients`, `served`, `refused`,
`dropped_frames`, `last_error`.

`last_error` and `error` hold an exception *class name*, never a message.

**A privacy note.** This branch echoes configured hostnames and the absolute
history path. With the defaults everything here is loopback, but an operator who
points MQTT at a remote broker puts that broker's address in `state-v2.json`.
Schema 1 has no `sinks` branch and is unaffected.

### `confidence`

The `FIELD_CONFIDENCE` map from `telemetry.py`, published verbatim so a consumer
can tell a measurement from a hypothesis without reading the source. Values are
`observed` (seen on this R8), `upstream` (captured on an R8w), and `candidate`
(decompiled from the R/Tach app, never confirmed against any hardware).

Its keys are the published paths — `voltage_v`, `detector_gps.speed_raw`,
`alerts[].mute_code` — so a consumer can join a grade to a field by name. An
earlier version graded names that nothing emitted, which made the map
decorative; `test_every_confidence_key_names_something_actually_published` now
holds it to the document it describes.

### Worked example

A complete `state-v2.json`, generated from the code in this repository, with
GNSS enabled and `record_coordinates` off:

```json
{
  "alerts": [
    {
      "alert_id_raw": "00",
      "band": "KA",
      "direction": "front",
      "direction_code": "F",
      "field_5_raw": "34.712",
      "field_count": 9,
      "frequency_ghz": 34.712,
      "laser_gun": null,
      "laser_gun_id": null,
      "mute_code": "1",
      "mute_state": "not muted",
      "muted": false,
      "parsed": true,
      "raw_signal": 142,
      "receive_mode_raw": "0",
      "slot": 0,
      "strength_1_to_8": 5
    }
  ],
  "collector": {
    "adapter": "hci0",
    "mode": "continuous",
    "note": "",
    "reconnects": 1,
    "started_at": "2026-09-02T05:12:44Z",
    "status": "streaming"
  },
  "confidence": {
    "alerts[].alert_id_raw": "upstream",
    "alerts[].band": "upstream",
    "alerts[].direction": "upstream",
    "alerts[].field_5_raw": "upstream",
    "alerts[].frequency_ghz": "upstream",
    "alerts[].laser_gun": "candidate",
    "alerts[].laser_gun_id": "candidate",
    "alerts[].mute_code": "upstream",
    "alerts[].mute_state": "candidate",
    "alerts[].raw_signal": "upstream",
    "alerts[].receive_mode_raw": "candidate",
    "alerts[].strength_1_to_8": "upstream",
    "alerts_empty": "observed",
    "detector_gps.altitude_raw": "upstream",
    "detector_gps.direction_8": "upstream",
    "detector_gps.locked": "candidate",
    "detector_gps.present": "observed",
    "detector_gps.speed_raw": "upstream",
    "detector_gps.status_raw": "observed",
    "field_count": "observed",
    "poi_warning.active": "observed",
    "poi_warning.decoded": "candidate",
    "poi_warning.raw": "observed",
    "shape": "observed",
    "unknown.field_3_raw": "upstream",
    "unknown.field_4_raw": "upstream",
    "unknown.field_5_raw": "upstream",
    "unknown.field_6_raw": "upstream",
    "voltage_v": "observed"
},
  "counters": {
    "alert_packets": 63,
    "telemetry_packets": 1174,
    "unknown_mute_codes": 0,
    "unparsed_alert_packets": 0,
    "unparsed_telemetry": 0,
    "unreadable_slots": 0
  },
  "detector": {
    "detector_gps": {
      "altitude_raw": 1247,
      "altitude_unit": "unknown (upstream reads it as feet)",
      "direction_8": "NE",
      "evaluated": true,
      "locked": true,
      "present": true,
      "speed_raw": 63,
      "speed_unit": "unknown (upstream reads it as mph)",
      "status_raw": "C",
      "suspect_coordinate_pair": false
    },
    "field_count": 7,
    "parsed": true,
    "poi_warning": {
      "active": false,
      "decoded": null,
      "raw": null
    },
    "shape": "confirmed-7",
    "unknown": {
      "field_3_raw": "0",
      "field_4_raw": "0",
      "field_5_raw": "0",
      "field_6_raw": "1",
      "upstream_names": {
        "field_3_raw": "warning",
        "field_4_raw": "scanCount",
        "field_5_raw": "wifi status (R8w; this model has no Wi-Fi)",
        "field_6_raw": "brightness status"
      }
    },
    "voltage_v": 13.6
  },
  "health": {
    "loop_lag_alarm": false,
    "loop_lag_max_ms": 41.0,
    "loop_lag_ms": 1.2,
    "telemetry_interval": {
      "max_s": 1.02,
      "median_s": 1.0,
      "p95_s": 1.02,
      "samples": 6
    }
  },
  "ingest": {
    "accepted": 1237,
    "depth": 0,
    "dropped": 0,
    "gaps": 0,
    "high_water": 3,
    "lost_notifications": 0
  },
  "link": {
    "compatible": true,
    "connected": true,
    "last_packet_age_s": 0.4,
    "stale": false
  },
  "obd": {
    "bound": true,
    "device_present": true,
    "guard_enabled": true,
    "healthy": true,
    "reason": "",
    "rfcomm_active": true
  },
  "open_tracks": [
    {
      "ambiguous": false,
      "band": "KA",
      "directions": "F",
      "duration_s": 1.0,
      "family": "KA",
      "laser_gun_id": null,
      "max_frequency_ghz": 34.712,
      "max_raw_signal": 142,
      "max_strength": 5,
      "min_frequency_ghz": 34.712,
      "samples": 2,
      "track_id": 1
    }
  ],
  "recent_events": [
    {
      "algorithm": "cost-greedy-1",
      "alert": {
        "alert_id_raw": "00",
        "band": "KA",
        "direction": "front",
        "direction_code": "F",
        "field_5_raw": "34.712",
        "field_count": 9,
        "frequency_ghz": 34.712,
        "laser_gun": null,
        "laser_gun_id": null,
        "mute_code": "1",
        "mute_state": "not muted",
        "muted": false,
        "parsed": true,
        "raw_signal": 142,
        "receive_mode_raw": "0",
        "slot": 0,
        "strength_1_to_8": 5
      },
      "correlation": "new",
      "kind": "alert_start",
      "material": true,
      "monotonic_ns": 1000000000,
      "seq": 1236,
      "track_id": 1,
      "wall_ns": 1756790000000000000
    }
  ],
  "schema": 2,
  "seq": 1237,
  "sinks": {
    "feed": {
      "enabled": false
    },
    "gnss": {
      "enabled": false
    },
    "history": {
      "dropped": 0,
      "enabled": true,
      "error": "",
      "errors": 0,
      "healthy": true,
      "path": "/home/jeremy/unidenr8/.state/history.db",
      "queued": 412,
      "written": 412
    },
    "mqtt": {
      "enabled": false
    }
  },
  "updated_at": "2026-09-02T05:31:07.749Z",
  "vehicle_gnss": {
    "age_s": 0.0,
    "altitude_m": 331.2,
    "climb_mps": 0.0,
    "coordinates_withheld": true,
    "device_time": "2026-09-02T05:31:07.000Z",
    "epx_m": 4.2,
    "epy_m": 6.1,
    "lat": null,
    "lon": null,
    "mode": 3,
    "mode_name": "3D fix",
    "satellites": null,
    "source": "gpsd",
    "speed_mph": 63.5289824,
    "speed_mps": 28.4,
    "track_deg": 41.5,
    "valid": true
  }
}
```

Again: the alert, the track and the fix are synthesised. No active alert has
been captured on this detector.

### One thing schema 2 does not guarantee

The `ingest`, `health`, `open_tracks`, `recent_events`, `detector` and
`vehicle_gnss` branches are refreshed by the streaming loop. The collector also
writes state from outside a session — when it publishes `connecting`,
`obd-blocked`, `reconnecting` and `stopped` — and those writes carry whatever
those branches last held. A document with `status: "stopped"` may still show the
last `detector` reading and the last open tracks. Read `status`, `link` and
`updated_at` first, and treat the rest as of that moment.

---

## The alert event records

Three kinds, produced by `events.py::AlertTracker` and stamped
`kind`: `alert_start`, `alert_update`, `alert_end`. They reach four places: the
`recent_events` array of `state-v2.json`, the MQTT `<base>/alert` topic, the SSE
`alert` event, and the `alert_events` table of the history database.

### Track identity is inference

This is the most important sentence in this document.

**The protocol provides nothing to correlate on.** Field 1 of an alert slot is
the alert id, and it reads `00` in every capture anyone has published, on either
model. The detector sends a full snapshot of its slots on every change, and it
re-orders those slots as signals rise and fall, so slot position is not an
identity either. Deciding that the Ka reading in this snapshot is the *same
threat* as the Ka reading in the last one is a guess this project makes.

The obvious key does not work, and each part of it fails in the situation that
matters most:

* **Direction is geometry, not identity.** Approaching and passing a fixed
  source gives F, then S, then R, for one source. Keying on direction
  manufactures an end and a start at the moment of closest approach — the single
  most interesting instant in the encounter.
* **Frequency drifts.** The estimate wanders with signal strength, and even
  1 MHz rounding is inside the drift for a weak Ka signal.
* **Band is not fixed either.** K and K POP, Ka and Ka POP, MRCD and MRCT are
  reclassifications of one signal, not different threats.

So matching is a *cost*, not a key. `events.py::match_cost` scores each open
track against each incoming slot on band family, frequency distance scaled to
the band, direction plausibility and strength continuity, and assignment is
greedy over the sorted scores. A hard disagreement — different band family,
frequency outside tolerance, a strength jump over 4 bars — refuses outright.

Two consequences a consumer must accept:

1. **`track_id` is a derived label, not a device identifier.** It is
   process-local, starts at 1 per collector run, and means nothing across
   restarts.
2. **The derivation can be redone.** Every event carries `algorithm`, currently
   `"cost-greedy-1"`. It is stored with the event so a history spanning an
   algorithm change can still be read honestly instead of silently averaging two
   different matchers' output. A consumer aggregating encounters across a long
   history should group by `algorithm` or filter to one.

### Fields on every event

| Key | Type | Meaning |
|---|---|---|
| `kind` | string | `alert_start`, `alert_update`, or `alert_end`. |
| `seq` | int | The ingest sequence number of the snapshot that caused this. |
| `track_id` | int | The derived track. Process-local. |
| `monotonic_ns` | int | Monotonic clock, nanoseconds. Use this for durations and ordering. |
| `wall_ns` | int | Wall clock, nanoseconds since the Unix epoch. Use this only to say *when*. |
| `correlation` | string | How the observation was matched. See below. |
| `algorithm` | string | The matcher that produced this. Currently `cost-greedy-1`. |
| `material` | bool | Whether a field a driver would care about changed. |
| `alert` | object | The full decoded slot, in the schema-2 `alerts` shape. |

**Two clocks, deliberately.** The Pi Zero 2 W has no battery-backed clock, so
its wall clock is wrong at every cold boot and then jumps by hours when the
network appears. A duration computed across that jump is nonsense. Durations,
staleness and ordering come from `monotonic_ns`; `wall_ns` exists so a human or
a database can say when. Both are captured at the same instant, which is what
lets a reader convert a monotonic duration into a wall-clock window later.

`material` is true when the band, direction, strength or mute code changed, and
false when only the fine-grained raw signal moved. A display may coalesce the
immaterial updates. The history must not.

### Fields that appear only on `alert_end`

These five keys are **absent**, not null, on a start or an update. They are the
aggregates the tracker computed while the encounter was live, which is why they
are attached at the end rather than reconstructed later.

| Key | Type | Meaning |
|---|---|---|
| `duration_s` | float | First to last observation, from the monotonic clock, rounded to 0.001. |
| `samples` | int | How many snapshots this track appeared in. |
| `max_strength` | int | Peak bars over the whole encounter. |
| `max_raw_signal` | int | Peak raw signal over the whole encounter. |
| `directions` | string | The sequence of direction codes seen, e.g. `"FSR"`. |

An alert usually fades before it ends, so `alert.strength_1_to_8` on the end
event is the *last* reading and `max_strength` is the *peak*. They are routinely
different, and the peak is almost always the one worth reporting.

The end's `monotonic_ns` is the last moment the track was actually **seen**, not
the moment its absence was noticed. Those differ by the miss tolerance, and
stamping the end when it was worked out would inflate every duration in the
history by the same constant.

`wall_ns` on an end is subtler, and the two clocks disagree on purpose. On a
`timeout` it is the last moment the track was seen, matching `monotonic_ns`. On
a `closed` — the link went away — it is the moment of teardown, which is later.
Compute durations from `duration_s` or from `monotonic_ns`, never by
subtracting wall clocks across a pair of events.

### `correlation` values

| Value | Appears on | Means |
|---|---|---|
| `new` | `alert_start` | No open track matched this slot. |
| `matched` | `alert_update` | An ordinary continuation, matched cleanly. |
| `ambiguous` | `alert_update`, `alert_end` | A runner-up track scored within 0.25 of the winner. The match was still made — refusing would be worse — but it is labelled. |
| `timeout` | `alert_end` | The track was absent from more snapshots than the miss tolerance allows. One miss is tolerated so a single dropped packet does not end and restart a threat that never stopped. |
| `closed` | `alert_end` | The link went away with the track still open — a drop, an OBD-blocked release, or a clean shutdown. |

Note the precedence: an ambiguous track that ends is labelled `ambiguous`, which
overwrites `timeout` or `closed`. If you need to tell a timeout from a teardown
you cannot do it from an ambiguous track's end event; compare its `wall_ns`
against the collector's `status` transition instead.

Every open track is ended when a session ends. Otherwise a threat that was live
when the link died would stay live in the history forever.

### A slot that arrives and cannot be read holds tracks open

When a snapshot contains a slot that arrived and failed to decode, the tracker
suppresses its end pass entirely. Absence and failure are different facts:
treating a decode failure as "the threat stopped" would fabricate a complete
alert lifecycle — start, updates, end, duration, peak strength — out of one bad
byte, permanently, in the history. The failure shows up instead as
`counters.unreadable_slots` and `counters.unparsed_alert_packets`.

### Worked examples

A start:

```json
{
  "algorithm": "cost-greedy-1",
  "alert": {
    "alert_id_raw": "00",
    "band": "KA",
    "direction": "front",
    "direction_code": "F",
    "field_5_raw": "34.712",
    "field_count": 9,
    "frequency_ghz": 34.712,
    "laser_gun": null,
    "laser_gun_id": null,
    "mute_code": "1",
    "mute_state": "not muted",
    "muted": false,
    "parsed": true,
    "raw_signal": 142,
    "receive_mode_raw": "0",
    "slot": 0,
    "strength_1_to_8": 5
  },
  "correlation": "new",
  "kind": "alert_start",
  "material": true,
  "monotonic_ns": 1000000000,
  "seq": 1236,
  "track_id": 1,
  "wall_ns": 1756790000000000000
}
```

The matching end, with the five aggregate keys present:

```json
{
  "algorithm": "cost-greedy-1",
  "alert": {
    "alert_id_raw": "00",
    "band": "KA",
    "direction": "front",
    "direction_code": "F",
    "field_5_raw": "34.712",
    "field_count": 9,
    "frequency_ghz": 34.712,
    "laser_gun": null,
    "laser_gun_id": null,
    "mute_code": "1",
    "mute_state": "not muted",
    "muted": false,
    "parsed": true,
    "raw_signal": 142,
    "receive_mode_raw": "0",
    "slot": 0,
    "strength_1_to_8": 5
  },
  "correlation": "closed",
  "directions": "F",
  "duration_s": 1.0,
  "kind": "alert_end",
  "material": true,
  "max_raw_signal": 142,
  "max_strength": 5,
  "monotonic_ns": 2000000000,
  "samples": 2,
  "seq": 1400,
  "track_id": 1,
  "wall_ns": 1756792000000000000
}
```

---

## The gap record

A gap says which notifications were lost and over what span. A counter alone is
not visibility: a counter says "four were lost", this says *which* four, so a
hole in the record is a documented hole rather than an absence nobody can date.

| Key | Type | Meaning |
|---|---|---|
| `kind` | string | Always `"gap"`. |
| `first_lost_seq` | int | Sequence number of the first notification dropped in this run of losses. |
| `last_lost_seq` | int | Sequence number of the last. |
| `count` | int | `last_lost_seq - first_lost_seq + 1`. |
| `monotonic_ns` | int | When the run of losses began. |
| `wall_ns` | int | When the most recent loss in this run happened. |

```json
{
  "count": 4,
  "first_lost_seq": 8801,
  "kind": "gap",
  "last_lost_seq": 8804,
  "monotonic_ns": 913442110000,
  "wall_ns": 1756790123456789000
}
```

One record per contiguous run of losses, not one per loss: a consumer that fell
behind by two hundred packets needs to know that, not to read two hundred
identical notes. The gap holds a reserved slot outside the queue, so the account
of what was lost can never itself be lost, and it is always delivered before any
queued notification — everything it describes is older than everything still
queued.

**What a consumer should do when it sees one.**

1. **Distrust the derived tracks across the gap.** Snapshots were lost, so what
   the tracker believes about open threats may be wrong: a threat may have
   started and ended inside the hole, or a track may have been continued across
   a discontinuity it should not have crossed.
2. **Do not treat the gap as an alert transition.** It is not one. It says
   nothing about whether anything was detected.
3. **Expect `collector.note` to read `"notifications dropped"`.** The collector
   sets it when it consumes a gap, and it is not cleared until the next session
   connects.
4. **Cross-check `ingest.dropped` and `ingest.lost_notifications`.** If they are
   growing, the consumer loop is falling behind and the fix is upstream of you.
5. **If you are computing statistics, exclude the affected interval** rather
   than interpolating across it. The stream is lossless by construction and its
   numbering proves what was lost; interpolation throws away the one guarantee
   this design bought.

Gap records appear **only** in `state-v2.json`'s `recent_events`. They are not
published to MQTT, not sent on the SSE feed, and not written to the history
database.

---

## MQTT topics

Off unless `mqtt.enabled` is true. Base topic is `mqtt.base_topic`, default
`unidenr8`, leading and trailing slashes stripped; an empty base falls back to
`unidenr8`. QoS is 0 on every topic.

| Topic | Payload | Retained | Published when |
|---|---|---|---|
| `<base>/status` | `online` / `offline` | **yes** | on connect, on clean stop, and as the last will |
| `<base>/availability` | `online` / `offline` | **yes** | on connect and on clean stop |
| `<base>/alert` | one event record, JSON | **no** | on every alert transition |
| `<base>/state` | one state document, JSON | **yes** | on every publish from inside a session |
| `homeassistant/<component>/unidenr8/<slug>/config` | discovery document, JSON | **yes** | on connect, only when `mqtt.home_assistant` is true |

### Why the retention is asymmetric

**A retained state is how a dashboard that connects mid-drive sees anything at
all.** Without it, a client subscribing between heartbeats sees nothing until
the next publish, and on a quiet link that can be a minute.

**A retained alert is a false alarm.** The broker would hand a Ka hit from
twenty minutes ago to every client that subscribes, as though it were happening
now. For a radar detector that is worse than silence: the whole value of an
alert is that it is current. An alert that arrives late is not an alert, which is
also why QoS is 0 — a retry mechanism buys nothing here and costs radio time on a
link this project is trying to leave alone.

**Availability is retained because absence is the message.** A status topic that
can only ever say `online` is not a status topic. The `offline` value has to
survive the process that would have published it.

### What the state topic carries, and what it does not

`<base>/state` carries the schema-1 document unless `mqtt.detail` is true, in
which case it carries schema 2 — including the detector's heading, speed and
altitude, and coordinates if the operator enabled them. Turning that on sends
position-adjacent data to a broker. `Config.warnings()` says so when the broker
is remote and TLS is off.

The state topic is published only from inside a streaming session. The
collector's `connecting`, `obd-blocked`, `reconnecting` and `stopped` writes go
to the state files and **not** to the broker. So the retained `<base>/state` can
show `streaming` after the collector has stopped. `<base>/status` is what tells
you the process is gone; a consumer must use it, not the state document's own
`status`, to decide whether the data is live.

### Home Assistant discovery

With `mqtt.home_assistant = true`, four retained discovery documents are
published under `homeassistant/`: sensors `voltage`, `status` and `band`, and a
binary sensor `alerting`. Their value templates read
`value_json.telemetry.voltage`, `value_json.collector.status` and
`value_json.alerts[0].band`, which are **schema-1 paths**. Setting
`mqtt.detail = true` alongside `home_assistant = true` breaks the voltage
sensor, because schema 2 has no top-level `telemetry` key. Pick one.

---

## The SSE feed

Off unless `feed.enabled` is true. Binds to loopback by default; there is no
authentication, because there is no good place to put a credential on a device
that boots unattended in a car. `Config.warnings()` complains loudly when the
bind address is changed. Reaching it from a phone is meant to go through the
node's existing VPN interface, not through an open port.

### Routes

| Method | Path | Response |
|---|---|---|
| GET | `/` | The bundled single-file dashboard, `text/html`. |
| GET | `/index.html` | The same. |
| GET | `/healthz` | `ok`, `text/plain`. |
| GET | `/state` | The most recently published state document, `application/json`. `{}` before the first publish. |
| GET | `/events` | An SSE stream, `text/event-stream`. |

`HEAD` is accepted on all of them. Any other method gets 405, any other path
gets 404. The cap is eight simultaneous `/events` connections; the ninth gets
503 `too many viewers`. More than eight viewers on a Pi Zero is a mistake rather
than a use case, and refusing is better than accepting a connection into a queue
nobody is draining.

### Event names on `/events`

| Event | Payload | Sent when |
|---|---|---|
| `state` | one state document | immediately on connect, then on every publish |
| `alert` | one alert event record | on every alert transition |

Plus a bare comment frame, `: keepalive`, every 20 seconds on an idle stream.
Without it a proxy or a phone's power manager silently drops a connection that
has said nothing. A consumer reading raw bytes must skip comment lines; a
browser `EventSource` does that for you.

Both payloads are compact single-line JSON, as SSE requires. `state` carries the
schema-1 document unless `feed.detail` is true, in which case it carries
schema 2 — the same position-adjacent trade as MQTT.

Per-client backlog is 64 frames and drops its **oldest** entry: a viewer that
fell behind wants the current state, not a replay of the last minute. A write
that takes more than 2 seconds costs that client its stream. A viewer on a bad
link must never cost the vehicle its radar data.

Like MQTT, the feed is only published from inside a streaming session. `/state`
after a shutdown returns the last streaming document.

---

## The SQLite history

Written only when `history.enabled` is true, by a dedicated writer thread that
owns its own connection. The asyncio loop never touches the disk: it appends to
a bounded queue and returns.

Location: `history.path`, resolved against `<state_dir>` when relative; default
`<state_dir>/history.db`. Created `0600` inside a `0700` directory.

Pragmas: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`,
`journal_size_limit=4194304`, `wal_autocheckpoint=512`, `temp_store=MEMORY`.
WAL with `NORMAL` is the configuration that fits a vehicle: the ignition is cut
mid-transaction routinely, and a power loss can lose the last few committed
transactions but cannot corrupt the file. Losing the last second of telemetry on
a hard power cut is acceptable; a corrupt file that takes the whole history with
it is not.

There is **no migration path**, on purpose. `storage.SCHEMA_VERSION` is `1`, and
opening a database written under a different version raises `HistoryError`
telling the operator to move the old file aside rather than mixing rows written
under different meanings.

Read it with `uniden-r8 history [stats|events|encounters|telemetry]`, which
touches no radio, or with any SQLite client.

### `meta`

| Column | Type | Notes |
|---|---|---|
| `key` | TEXT | PRIMARY KEY. |
| `value` | TEXT | NOT NULL. |

Holds exactly one row in practice: `('schema', '1')`.

### `sessions`

One row per collector run that opened the history.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT. Referenced by `session_id` elsewhere. |
| `started_at` | TEXT | NOT NULL. ISO-8601 UTC with milliseconds. |
| `started_wall_ns` | INTEGER | NOT NULL. |
| `started_monotonic_ns` | INTEGER | NOT NULL. |
| `ended_at` | TEXT | NULL until the writer thread shuts down cleanly. |
| `adapter` | TEXT | The pinned controller, or NULL when BlueZ picked. |
| `note` | TEXT | Always NULL in the current code. |

`session_id` is a plain integer with no foreign-key constraint.

### `alert_events`

One row per derived transition. This is the table the project exists to fill.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT. |
| `session_id` | INTEGER | NOT NULL. |
| `seq` | INTEGER | NOT NULL. The ingest sequence number. |
| `kind` | TEXT | NOT NULL. `alert_start`, `alert_update`, `alert_end`. |
| `track_id` | INTEGER | NOT NULL. Derived, process-local. |
| `at` | TEXT | NOT NULL. ISO-8601 UTC with milliseconds, from `wall_ns`. |
| `wall_ns` | INTEGER | NOT NULL. |
| `monotonic_ns` | INTEGER | NOT NULL. Use this for durations. |
| `band` | TEXT | The band as parsed. Constrained to the parser's allowlist. |
| `strength` | INTEGER | 1 to 8. |
| `raw_signal` | INTEGER | The finer-grained signal number. |
| `frequency_ghz` | REAL | NULL for laser and the photo-radar types. |
| `laser_gun_id` | INTEGER | NULL unless the band was laser. |
| `direction` | TEXT | The **named** direction: `front`, `side`, `rear`. Not the raw code. |
| `mute_code` | TEXT | Field 7 verbatim. |
| `alert_id_raw` | TEXT | Field 1. `00` in every capture on either model. |
| `receive_mode` | TEXT | Field 8 verbatim. No reading of it is established. |
| `correlation` | TEXT | `new`, `matched`, `ambiguous`, `timeout`, `closed`. |
| `material` | INTEGER | 0 or 1. |
| `duration_s` | REAL | **NULL except on `alert_end` rows.** |
| `samples` | INTEGER | **NULL except on `alert_end` rows.** |
| `max_strength` | INTEGER | **NULL except on `alert_end` rows.** |
| `max_raw_signal` | INTEGER | **NULL except on `alert_end` rows.** |
| `lat` | REAL | **NULL unless `gnss.record_coordinates` is on** *and* a current fix existed. |
| `lon` | REAL | Same. |
| `gnss_mode` | INTEGER | NULL unless a current GNSS fix existed. Not gated on the coordinate opt-in. |
| `gnss_speed_mps` | REAL | Same. |
| `gnss_track_deg` | REAL | Same. |

Indexes: `alert_events_at` on `(at)`, `alert_events_track` on
`(session_id, track_id)`.

The GNSS fix is attached at write time rather than joined later, because
"nearest fix" depends on both clocks and both sources' health, and deciding it
once keeps the stored row unambiguous. `gnss_mode`, `gnss_speed_mps` and
`gnss_track_deg` are recorded even with coordinates off — that is the genuinely
useful middle configuration: it answers "was there a valid fix when that alert
fired" and validates the detector's own speed reading against a trusted source
without building a record of where the vehicle has been.

Two fields present on an event record are **not** columns here: `algorithm` and
`directions`. See [What the code does not do](#what-the-code-does-not-do).

### `telemetry`

Throttled samples. One packet a second for a whole drive is 86 400 rows a day of
near-identical voltage readings on an SD card, so at most one row every
`history.telemetry_every_seconds` (default 10) is written.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT. |
| `session_id` | INTEGER | NOT NULL. |
| `at` | TEXT | NOT NULL. ISO-8601 UTC with milliseconds. |
| `wall_ns` | INTEGER | NOT NULL. |
| `monotonic_ns` | INTEGER | NOT NULL. |
| `voltage` | REAL | May be NULL for an unparsed packet. |
| `gps_locked` | INTEGER | 0, 1, or NULL — the same tri-state as the JSON. |
| `poi_active` | INTEGER | 0 or 1. |
| `direction_8` | TEXT | **NULL unless `history.record_detector_motion` is on.** |
| `speed_mph` | INTEGER | **NULL unless `history.record_detector_motion` is on.** |
| `altitude_ft` | INTEGER | **NULL unless `history.record_detector_motion` is on.** |
| `status_raw` | TEXT | The GPS status letter. Not gated. |
| `warning_raw` | TEXT | Field 3, verbatim. |
| `scan_raw` | TEXT | Field 4, verbatim. |

Index: `telemetry_at` on `(at)`.

The three motion columns are the opt-in, and it defaults to off, because a
history of where a vehicle has been is a different kind of file from a history
of what its radar detector heard. The column names `speed_mph` and
`altitude_ft` carry upstream's unit reading; neither unit has been verified by
this project.

### `gnss_fixes`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT. |
| `session_id` | INTEGER | NOT NULL. |
| `at` | TEXT | NOT NULL. |
| `wall_ns` | INTEGER | NOT NULL. |
| `monotonic_ns` | INTEGER | NOT NULL. |
| `mode` | INTEGER | gpsd fix mode. |
| `lat` | REAL | **NULL unless `gnss.record_coordinates` is on.** |
| `lon` | REAL | Same. |
| `alt_m` | REAL | Altitude, metres. |
| `speed_mps` | REAL | Ground speed. |
| `track_deg` | REAL | Course over ground. |
| `epx_m` | REAL | Longitude error estimate, metres, 95% confidence. |
| `epy_m` | REAL | Latitude error estimate, metres, 95% confidence. |
| `satellites` | INTEGER | Satellites used in the fix. |

Index: `gnss_at` on `(at)`.

**This table is created, indexed, counted and swept, and nothing writes to it.**
`HistoryWriter.record_fix` exists and no caller invokes it. Expect it to be
empty. See [What the code does not do](#what-the-code-does-not-do).

### Retention

`history.retain_days`, default 30. On writer start, rows in `alert_events`,
`telemetry` and `gnss_fixes` with `wall_ns` older than the cutoff are deleted,
along with `sessions` rows older than the cutoff. Zero disables expiry
entirely, and `Config.warnings()` says so out loud because it is a decision
rather than a default.

The sweep runs **once, at writer start**, not on a timer. A collector that runs
for a month without restarting does not prune during that month.

Retention is driven by the wall clock, which is the one place this project
cannot use the monotonic clock — a retention window has to mean calendar days.
On a node whose clock jumps at boot, a sweep can therefore delete more or less
than intended. It is a diagnostic history, not a system of record.

---

## Writing a consumer

The defensive rules, in one place. They are short because they are all the same
rule: nothing on the other side of this boundary is under your control.

1. **Pin the schema exactly.** `schema == 1` for `state.json`, `schema == 2` for
   `state-v2.json`. Never `>=`.
2. **Validate every type at the boundary.** Numbers can be `null`. Booleans can
   be tri-state and arrive as `null`. Arrays can be empty. Objects can be
   missing.
3. **Ignore keys you do not recognise.** That is what makes adding a key a safe
   change and it is the only reason `shape_confirmed` could be added.
4. **Reject stale data on your own clock**, from `updated_at`, and reject an
   implausibly *future* timestamp too. Do not rely on the producer's own
   `stale` flag: it measures packet age at write time and stops updating when
   the writer dies.
5. **Allowlist every string you act on.** `status`, `band`, `direction`,
   `correlation`, `kind`, `shape`, `note`. The producer allowlists them; do it
   again. Render anything unexpected as "unknown", never as itself.
6. **Do not parse `display_line`.** Build your own from the typed fields. The
   sibling e-paper display does exactly this and it is the right model.
7. **Treat `track_id` as a label, not an identity**, and treat every derived
   track as inference. Carry `algorithm` alongside anything you aggregate.
8. **Use `monotonic_ns` for durations and ordering, `wall_ns` only for
   display.** The node has no battery-backed clock.
9. **Handle the gap record.** It shares `recent_events` with alert events;
   switch on `kind`, and distrust track continuity across it.
10. **Distinguish absence from failure.** An empty `alerts` array is the
    detector saying "nothing", and it is a fact. `unparsed_alert_packets` and
    `unreadable_slots` above zero mean something arrived that could not be read,
    which is a different fact and must not be rendered as "clear".
11. **Use `<base>/status`, not the state document, to decide if MQTT data is
    live.** The retained state topic outlives the process that wrote it.
12. **Never treat an active alert as confirmed protocol.** No active alert has
    ever been captured on this detector. Read the `confidence` map, and if you
    are building anything a driver relies on, say so in your own interface.
13. **Do not assume position is available.** Schema 1 has none at all. Schema 2
    has coordinates only when the operator turned them on, and `lat`/`lon` are
    present-and-null with `coordinates_withheld: true` when they did not.

---

## What the code does not do

Places where the code as it stands differs from what its own documentation,
comments or tests describe. Recorded here rather than smoothed over, because a
consumer contract that describes intentions instead of behaviour is worse than
none.

| # | Claim | Reality |
|---|---|---|
| 1 | `FIELD_CONFIDENCE` reads like an index into the published document, but some keys named fields nothing emitted. | **Fixed.** The keys are now the published paths — `detector_gps.speed_raw`, `alerts[].mute_code`, and so on — and `test_every_confidence_key_names_something_actually_published` pins that they stay that way. |
| 2 | `LiveSession.render()` prints POI detail when `--full` is given. | **Fixed.** `_render_detail` reached for `PoiWarning` fields a later simplification had removed and would have raised `AttributeError` the first time a POI warning was active. It now prints field 1 verbatim and undecoded, and a regression test drives that path. |
| 3 | The bundled feed dashboard renders either document. | **Fixed.** `paint()` read schema-1 paths only, so `feed.detail = true` blanked the voltage, GPS and stale indicators. It now normalises both shapes. |
| 4 | `gnss_fixes` is a table of recorded fixes. | **Fixed.** Nothing called `HistoryWriter.record_fix`; fixes are now sampled on the telemetry throttle, respecting `record_coordinates`. |
| 5 | `collector.stale_after_seconds` is a configurable staleness threshold. | **Fixed.** It is carried on `CollectorState` and read by both documents. `collector.HEALTH_INTERVAL_SECONDS` and `PUBLISH_INTERVAL_SECONDS` remain module constants that nothing reads — the live values are `obd.interval_seconds` and `collector.heartbeat_seconds`. |
| 6 | `feed.py` closes an oversized request quietly rather than raising at the loop. | **Fixed.** `asyncio.LimitOverrunError` descends from `Exception` directly and escaped the handler into `client_connected_cb`. It is caught explicitly now, and the whole callback body has an outer guard. |
| 7 | The history "stores the snapshots it worked from", so a better matcher can be re-run over the same data. | **Fixed.** There is an `alert_snapshots` table now, written before anything is derived. It is the one place a raw packet reaches the disk, and only alert packets: a telemetry payload would carry heading, speed, altitude and POI detail past both opt-ins in one column, and an alert payload carries no position at all. |
| 8 | Every event carries `algorithm`, stored so a history spanning a matcher change can be read honestly. | **Fixed.** `alert_events.algorithm` exists. |
| 9 | An `alert_end` carries `directions`, the bearings walked over the encounter. | **Fixed.** `alert_events.directions` exists. |

Every one of these was found by writing this document against the code rather
than against the intent, and every one is now closed. The table is kept rather
than deleted because the method is the point: a reference that agrees with the
documentation instead of the source is worse than no reference, and the next
person to write one should expect to find the same kind of thing.

None of these affects the safety boundary in `docs/SAFETY.md`. They are all
places where an output is thinner than its documentation promises.
