# Protocol reference

What the Uniden R8 puts on the wire over BLE, field by field, with the
provenance of every claim attached.

This is the document to read instead of `src/uniden_r8/telemetry.py`. It
describes the attribute table, the two payload formats, and every place where
this project's reading of a byte is weaker than it looks. It is written against
the code as it exists now, and every structural claim is traceable to a file
and a name in `src/uniden_r8/`.

Two facts govern everything below, and neither is negotiable by a later
paragraph:

1. **The live BLE stream carries no latitude and no longitude.** The detector
   plainly knows where it is — its red-light-camera warnings are impossible
   otherwise — but what it transmits is a heading to the nearest of eight
   compass points, a speed, an altitude and a status letter. Coordinates in
   this project come from a separate GNSS receiver over `gpsd`
   (`src/uniden_r8/gnss.py`) and live in their own branch of the published
   schema. See §7.
2. **No active alert has ever been seen from Jeremy's non-W R8.** The only
   alert payload this detector has produced is all-clear. Every field of an
   active alert described in §4 is UPSTREAM (captured on an R8w) or weaker.
   Where that matters, it is said again in place.

---

## 1. How to read this document

### 1.1 Evidence grades

Used verbatim from `docs/EVIDENCE.md` and `uniden_r8.gatt.Evidence`:

| Grade | Means |
|---|---|
| **OFFICIAL** | Uniden said it: product page, owner's manual, support article, or release note. |
| **OBSERVED** | Seen on Jeremy's own non-W R8 by this project. |
| **UPSTREAM** | Captured on an **R8w** by `AegisX86/UnidenR8wlink` @ `9072bc2f`. |
| **UPSTREAM-UNVERIFIED** | In that upstream, but documented there as never tested on hardware. |
| **INFERENCE** | This project's reasoning. Not observed anywhere. |

"No evidence" is a valid entry and appears throughout. A grade describes where
a claim came from, not how likely it is to be true.

### 1.2 The code's own three-word grades

`telemetry.FIELD_CONFIDENCE` publishes a grade for every field alongside the
detailed view, so a consumer can tell a measurement from a hypothesis without
reading this file. It uses three words, which map onto the five grades above:

| `FIELD_CONFIDENCE` value | Grade here | Means |
|---|---|---|
| `observed` | OBSERVED | Seen on this R8. |
| `upstream` | UPSTREAM | Captured on an R8w. |
| `candidate` | UPSTREAM-UNVERIFIED | Decompiled from the R/Tach app; never confirmed against any hardware, on either model. |

There is no `official` or `inference` value. The map is emitted into
`state-v2.json` (`collector.build_detail_document`) and into the `live --full`
JSON, so it travels with the data rather than staying in a comment.

### 1.3 One distinction that recurs

For most fields, *that a value arrives* and *what the value means* have
different grades. Seven `&`-separated telemetry fields arrive on this R8 in
every packet ever captured — that is OBSERVED. That field 4 is a "scan count"
is UPSTREAM-UNVERIFIED, and nothing this project has done tests it. The tables
below grade the meaning, and say so when the presence is stronger.

---

## 2. The GATT surface

### 2.1 Services

Three services carry anything this project reads. All five services the
detector exposes are listed for completeness.

| UUID | Role | Grade |
|---|---|---|
| `18424398-7cbc-11e9-8f9e-2a86e4085a59` | Uniden **data** service: telemetry, alerts, POI, settings | UPSTREAM for the definition; OBSERVED present on this R8 |
| `1842467c-7cbc-11e9-8f9e-2a86e4085a59` | Uniden **command** service: write and response | UPSTREAM for the definition; OBSERVED present on this R8 |
| `0000180a-0000-1000-8000-00805f9b34fb` | Device Information (Bluetooth SIG, 0x180A) | OFFICIAL (SIG); OBSERVED present |
| `00001800-0000-1000-8000-00805f9b34fb` | Generic Access (0x1800) | OFFICIAL (SIG); OBSERVED present |
| `00001801-0000-1000-8000-00805f9b34fb` | Generic Attribute (0x1801) | OFFICIAL (SIG); OBSERVED present |

Service discovery on the connected R8 found exactly these five and nothing
else (`docs/EVIDENCE.md` §6.10). The vendor UUIDs were first documented on an
R8w; that they are also on this R8 is a separate, later observation, and the
catalogue in `gatt.py` deliberately keeps the original provenance rather than
overwriting it.

The advertisement carries **no** service UUIDs (EVIDENCE §6.5), so none of this
can be established without connecting.

### 2.2 Characteristics

Every UUID below is copied from `src/uniden_r8/gatt.py`. The "device
properties" column is what the peripheral advertises; the gate columns are what
*this project* permits, which is a much smaller set.

| UUID | Name | Device properties | Live | Inspect | Probe | Grade |
|---|---|---|---|---|---|---|
| `6c290d2e-1c03-aca1-ab48-a9b908bae79e` | Telemetry ("ETC data") | Read, Write-w/o-response, Notify | read + notify | no | read + notify | UPSTREAM; layout OBSERVED on this R8 |
| `6eb675ab-8bd1-1b9a-7444-621e52ec6823` | Alerts | Read, Write-w/o-response, Notify | read + notify | no | read + notify | UPSTREAM; only all-clear OBSERVED here |
| `15005991-b131-3396-014c-664c9867b917` | POI database | Read, Write-w/o-response, Notify | **no** | read | read | UPSTREAM |
| `2d86686a-53dc-25b3-0c4a-f0e10c8dee20` | Settings 1 | Read, Write-w/o-response, Notify | **no** | read | read | UPSTREAM |
| `5a87b4ef-3bfa-76a8-e642-92933c31434f` | Settings 2 | Read, Write-w/o-response, Notify | **no** | read | read | UPSTREAM |
| `2c86686a-53dc-25b3-0c4a-f0e10c8dee20` | **Command write** | Write-w/o-response | **refused** | **refused** | **refused** | UPSTREAM-UNVERIFIED |
| `5987b4ef-3bfa-76a8-e642-92933c31434f` | Command response | Read, Notify | no | no | **no** | UPSTREAM-UNVERIFIED |
| `00002a24-0000-1000-8000-00805f9b34fb` | Model Number String (0x2A24) | Read | no | no | read | OFFICIAL (SIG) |
| `00002a26-0000-1000-8000-00805f9b34fb` | Firmware Revision String (0x2A26) | Read | no | no | read | OFFICIAL (SIG) |
| `00002a28-0000-1000-8000-00805f9b34fb` | Software Revision String (0x2A28) | Read | no | no | read | OFFICIAL (SIG) |
| `00002a29-0000-1000-8000-00805f9b34fb` | Manufacturer Name String (0x2A29) | Read | no | no | read | OFFICIAL (SIG) |

Two rows deserve a sentence each.

**Command response is admitted by no gate at all.** It is in the catalogue and
`describe()` returns it, but it is explicitly excluded from `READABLE_UUIDS`
and `NOTIFY_UUIDS`, so `assert_readable` raises for it like an unlisted UUID
would. The reasoning in `gatt.py` is that it only becomes interesting after a
command, and this project sends none.

**POI and settings are recorded in the catalogue as non-notifying.** Upstream's
table (EVIDENCE §3) lists Notify among the device's properties for all five
data-service characteristics; `gatt.CATALOGUE` sets `notifies=False` for POI,
Settings 1 and Settings 2. The effect is that `NOTIFY_UUIDS` contains only
telemetry and alerts, so nothing can subscribe to a coordinate database by
editing a loop. Read the catalogue flag as "this project may subscribe", not as
"the device refuses to notify".

### 2.3 What the three gates are for

Three gates rather than one function with a mode parameter, because "which
operations is *this* command allowed to perform" is a question each command
should answer at its own call sites.

| Gate | Functions | Admits | Used by |
|---|---|---|---|
| **Probe** | `assert_readable`, `assert_notifiable` | Everything readable except command-response; notify on telemetry and alerts | `identity.py`, `cli plan`, `cli selftest` |
| **Live** | `assert_live_readable`, `assert_live_notifiable` | Telemetry and alerts only | `telemetry.py`, `collector.py` |
| **Inspect** | `assert_inspect_readable` | Settings 1, Settings 2, POI only | `inspection.py` |

The live gate is the narrow one, and its exclusions are the point: POI holds
saved camera locations and user marks, settings hold device configuration, and
neither is needed to answer what the detector is reporting right now. The
inspect gate is the mirror image — it admits *only* what the live path refuses,
because a person running one deliberate confirmed dump into the owner-only
private store is a different act from a process that runs unattended for hours.

Above all three sits a permanent denylist. `FORBIDDEN_UUIDS` contains the
command-write characteristic, `_assert_not_forbidden` runs first in every gate,
and the check is case- and whitespace-insensitive because a gate that can be
bypassed by upper-casing an argument is not a gate.

### 2.4 The compatibility gate, before anything is read

`telemetry.check_compatibility(client)` runs on every connection, in both the
`live` path and the collector, before a single characteristic is touched. It
requires, on the *connected device*:

| Required attribute | Why |
|---|---|
| Data service `18424398-…` | Everything else hangs off it |
| Telemetry `6c290d2e-…`, **inside that service** | The thing being read |
| Alerts `6eb675ab-…`, **inside that service** | The thing being read |

The characteristics must belong to the data service, not merely exist
somewhere on the device — `check_compatibility` looks them up within the
service it found, and `tests/test_telemetry.py` pins that. If anything is
missing, the session reports what was absent, reads nothing, and disconnects.

The UUIDs are confirmed present on this R8, so the check passes today. It still
runs every time, because a firmware update could move the table and a gate that
is only correct today is not a gate.

### 2.5 Descriptors

Every vendor characteristic reportedly carries a 0x2901 Characteristic User
Description — the device's own name for its own attribute. That is the cheapest
discovery step available and the only one where the answer comes from the
firmware rather than from somebody's reverse engineering, so `inspection.py`
reads them first, for the five UUIDs in `gatt.DESCRIPTOR_READ_PLAN` (telemetry,
alerts, POI, both settings).

Grade: **UPSTREAM** that the descriptors exist. **No evidence** for what this
R8 returns — `uniden-r8 inspect` has not been run on it, and `docs/RUNBOOK.md`
has no step for it.

Descriptors live in their own plan rather than in `PROBE_PLAN`, because that
plan's contract — every entry is a `("read"|"notify", characteristic)` pair
whose UUID is in the catalogue — is asserted by three tests, and widening it to
admit a different kind of attribute would weaken all three to buy nothing.

---

## 3. The telemetry packet

Characteristic `6c290d2e-1c03-aca1-ab48-a9b908bae79e`. UTF-8 text, seven
fields separated by `&`. Notifies about once a second.

```
12.1&0&W,0,193,C&0&12&D&D
 ^   ^ ^         ^ ^  ^ ^
 0   1 2         3 4  5 6
```

Decoder: `telemetry.parse_telemetry`. It never raises — this runs against a
detector in a moving vehicle, and a malformed packet must cost one reading, not
the session.

### 3.1 What is established about the shape

| Claim | Grade | Source |
|---|---|---|
| Seven `&`-separated fields | OBSERVED | 324 of 324 packets on this R8: 31 in the 30-second window (EVIDENCE §7.3) and 293 in the five-minute trial (§8.1), zero unparsed in either |
| The GPS group holds exactly four comma fields | OBSERVED | Same captures |
| Voltage is field 0 | OBSERVED | 13.6 V steady across the first 31 packets (§7.4); 12.3 V → 13.6 V across the trial (§8.4) |
| Notification interval ≈ 1.0 s | OBSERVED | 0.97–1.02 s measured between consecutive packets (§7.2), ~0.98/s over 300 s (§8.1) |
| The device also accepts writes here | UPSTREAM | Recorded so the denylist has a reason attached; this project only reads |

### 3.2 The seven fields

`telemetry.TELEMETRY_FIELDS` names them in wire order, using the same neutral
names the parsed record uses; `telemetry.UPSTREAM_FIELD_NAMES` records what
upstream calls each of them. §3.6 explains why the two differ.

| # | Published as | Upstream's name | Format | Grade (meaning) | How this project handles it |
|---|---|---|---|---|---|
| 0 | `voltage` / `voltage_v` | voltage | decimal string, `12.1` | OBSERVED | `float()`; `None` if it does not parse. The only telemetry number in schema 1. |
| 1 | `poi_warning` | POI | `0`, or a comma group | OBSERVED for the literal `0`; active form UPSTREAM-UNVERIFIED | `_parse_poi_group`; see §3.5 |
| 2 | `detector_gps` | GPS | four comma fields | OBSERVED (shape) | `_parse_gps_group`; see §3.3 |
| 3 | `field_3_raw` | `warning` | short token, `0` in the example | UPSTREAM | `_safe_word(limit=16)`, kept verbatim, never interpreted |
| 4 | `field_4_raw` | `scanCount` | short token, `12` in the example | UPSTREAM-UNVERIFIED | `_safe_word(limit=16)`; `Telemetry.scan_field` exposes `int()` of it for the history writer |
| 5 | `field_5_raw` | "wifi status" | one character, `D` in the example | UPSTREAM name, **contradicted by OFFICIAL** — see §3.6 | `_safe_word(limit=4)`, kept verbatim |
| 6 | `field_6_raw` | "brightness status" | one character, `D` in the example | UPSTREAM | `_safe_word(limit=4)`, kept verbatim |

`_safe_word` is a sanitiser, not a decoder: it returns the value only if it is
non-empty, within the length limit, and alphanumeric once spaces, dots and
hyphens are removed. Anything else becomes `None`. The reason is that these
strings can reach a document a person reads, and an unbounded string from a
device is an injection surface however unlikely that seems here. The rejected
text is not lost — it stays in the private raw capture, which is where an
unexpected string belongs.

### 3.3 The GPS sub-group: field 2, four comma fields

This is the sub-group that decides what this project can and cannot say about
position, so it gets its own treatment.

```
W,0,193,C
^ ^  ^  ^
0 1  2  3
```

| # | Published as | Reading | Grade | Handling |
|---|---|---|---|---|
| 0 | `direction_8` | One of the eight compass points: `N NE E SE S SW W NW` | UPSTREAM | `_safe_word(limit=2)`, then kept **only if it is in `COMPASS_POINTS`**; anything else becomes `None` |
| 1 | `speed_raw` | Upstream reads it as miles per hour | UPSTREAM for the field, **no evidence for the unit** | `int()`; published with the literal string `"unknown (upstream reads it as mph)"` beside it |
| 2 | `altitude_raw` | Upstream reads it as feet | UPSTREAM for the field, **no evidence for the unit** | `int()`; published with `"unknown (upstream reads it as feet)"` beside it |
| 3 | `status_raw` | Status letter | The letter `C` is OBSERVED on this R8 while a fix was present; that it *means* anything is UPSTREAM-UNVERIFIED | `_safe_word(limit=4)` |

There is a heading here and no bearing. Eight compass points is the whole
resolution the detector offers; anything needing a real course must use an
external receiver.

The group has three outcomes, and the difference between the second and third
is deliberate:

| Field 2 content | `evaluated` | `present` | `locked` |
|---|---|---|---|
| Four comma fields, `status_raw == "C"` | true | true | `True` |
| Four comma fields, any other status | true | true | `None` — the letter is unknown, not a denial |
| Literal `0` or empty | true | false | `False` — the detector saying it has nothing |
| Any other field count | **false** | false | `None` — the packet could not be read |
| Tripwire fired (§3.4) | **false** | true | `None` |

Three states rather than two, because "the detector reports no fix" and "this
was not decodable" are different answers and collapsing them lets a decode
failure masquerade as a measurement. `Telemetry.gps_locked` is the schema-1
name for `locked`, and it is a tri-state boolean for exactly this reason.

The `speed_mph` and `altitude_ft` properties exist as aliases so a reader who
knows the upstream writeup finds what they expect. They return the raw integers
unchanged and assert nothing about units.

### 3.4 The coordinate tripwire, and why it exists

`_parse_gps_group` contains a check that looks paranoid and is not:

```python
first, second = _at(parts, 0), _at(parts, 1)
if _looks_like_coordinate(first) and _looks_like_coordinate(second):
    return DetectorGps(evaluated=False, present=True, suspect_pair=True)
```

The reason is honesty about what "no coordinates" actually rests on. What this
R8 has demonstrated is a four-part group. Four parts is exactly the right width
to be latitude, longitude, altitude and status. **The project's position rests
on upstream's naming of those fields, not on a measurement of this detector's
first three sub-fields**, which nobody has looked at.

So the parser checks its own assumption. `_looks_like_coordinate` is
deliberately loose — it is a tripwire, not a decoder — and returns true when a
value contains a `.`, parses as a float, falls within ±180, and is not a whole
number. If sub-fields 0 and 1 *both* look like that, the group is flagged
`suspect_pair`, marked unevaluated, and published as nothing at all: the raw
packet is already in the private capture where a person can look at it
deliberately.

In the observed layout the tripwire cannot fire, because sub-field 0 is a
compass letter and does not parse as a float. If it ever does fire, the honest
answer is that §3.3 and this document are wrong, and the flag is how that gets
noticed rather than published.

### 3.5 The POI sub-group: field 1

Only the inactive form — a literal `0` — has ever been observed, on either
detector. The active form is upstream's reading of the decompiled app.

`_parse_poi_group` therefore decodes no further than "something is being warned
about", plus at most 48 characters of validated text:

| Field 1 | `PoiWarning.active` | `PoiWarning.raw` |
|---|---|---|
| `0` or empty | `False` | `None` |
| anything else | `True` | `_safe_word(limit=48)` of the whole group |

`detailed()` reports `"decoded": None` explicitly. A structure nobody has ever
seen populated does not get a parser that can appear to succeed. Schema 1
publishes a bare boolean, `poi_warning`, and never the detail — a warning's
type and distance describe where the vehicle is.

### 3.6 Why fields 3–6 are published under neutral names

Upstream names these four fields `warning`, `scanCount`, `wifi status` and
`brightness status`. This project publishes them as `field_3_raw` …
`field_6_raw` and keeps upstream's names in one place,
`telemetry.UPSTREAM_FIELD_NAMES`, as documentation carried alongside the values
rather than as the values' identity.

Field 5 is the reason.

> `docs/EVIDENCE.md` §1.2, grade **OFFICIAL**: Uniden's own product page says
> the R8 has **no Wi-Fi** — "For drivers who want wireless update
> functionality, Uniden also offers the R8W, a Wi-Fi–enabled version with
> automatic update capabilities."

Upstream's field name came from an **R8w**, which is the Wi-Fi model. Whatever
byte sits at index 5 on a non-W unit, publishing it in a field called `wifi`
would assert a capability the manufacturer says the product does not have — in
a state document that other software parses, and in a history database that
outlives this conversation. A name is a claim. This one is not supported, so it
is not made.

The same reasoning applies more weakly to the other three: none of their
meanings has been tested on any hardware by this project, `scanCount` is graded
UPSTREAM-UNVERIFIED even upstream, and a field recorded verbatim can be decoded
later while a field renamed on a guess cannot be un-renamed out of a year of
stored rows. So all four keep neutral names, upstream's names travel with them
in the `unknown.upstream_names` block of the detailed view, and nothing is
thrown away.

### 3.7 Shape grades: `confirmed-7`, `extended-N`, `short-N`, `unreadable`

`Telemetry.shape` grades every packet by field count, and `Telemetry.parsed` is
true only for the shape this R8 has actually produced. What each grade causes:

| Field count | `shape` | `parsed` | Decoded? | Effect |
|---|---|---|---|---|
| 7 | `confirmed-7` | true if voltage parsed | fully | Schema 1 publishes voltage, GPS-fix state and the POI boolean |
| 8 or more | `extended-N` | **false** | fully, positionally | Schema 1 blanks all three telemetry values and sets `shape_confirmed: false`; schema 2 carries the decoded values with the grade attached; `unparsed_telemetry` increments |
| 2 to 6 | `short-N` | **false** | **no further than the field count** | Voltage is `None`; nothing is lined up against anything |
| 0 or 1 | `unreadable` | **false** | no | Same |

The asymmetry between longer and shorter is deliberate. A firmware update that
*appends* a field should not blank the voltage on a display, so an extended
packet is still decoded positionally and the consumer is told the shape is
unconfirmed. A packet with *fewer* fields offers nothing to line the values up
against, so it is not decoded at all — guessing which field was dropped would
be inventing data.

Schema 1 gates on `shape_confirmed`, not on `parsed`
(`collector.build_document`): a consumer with no way to express "probably"
should be told the reading is absent. `test_the_schema_one_document_refuses_an_unconfirmed_shape`
pins that, because a grade nothing acts on is decoration.

### 3.8 Where the decoded values go

Two views of every reading, and the difference is the privacy design.

| View | Method | Contents | Destination |
|---|---|---|---|
| Conservative | `Telemetry.publishable()` | `voltage`, `gps_locked`, `poi_warning`, `parsed` | `state.json` (schema 1), the e-paper display, MQTT and the local feed unless detail is switched on |
| Detailed | `Telemetry.detailed()` | Everything, including heading, speed, altitude, the four raw fields and the shape grade | `state-v2.json` (schema 2, `0600` in a `0700` directory), the SQLite history when `history.record_detector_motion` is on, and `live --full` |

Heading, speed and altitude are position-adjacent: a log of them is a rough
trace of a drive. They are decoded, because a field discarded cannot be decoded
later, and they are kept out of the conservative view, out of printed output,
and out of the history unless the operator turns them on.
`evidence.publish()` refuses any string that reaches printed or committed
output carrying a position at all.

---

## 4. The alert packet

Characteristic `6eb675ab-8bd1-1b9a-7444-621e52ec6823`. UTF-8 text. Notifies on
a detection change (UPSTREAM).

**Read this section knowing that the only alert payload this R8 has ever
produced is all-clear.** One packet from the initial GATT read in the
30-second window and one in the five-minute trial, with no notifications in
either (EVIDENCE §7.6, §8.6) — expected, with no radar source present. Every
field below that describes an *active* detection is UPSTREAM or
UPSTREAM-UNVERIFIED. None of it has been exercised on this detector.

### 4.1 Slot structure

The characteristic sends a **full snapshot on every change**, not a delta. The
payload is a list of slots separated by `&`; each slot is either the literal
`0`, meaning empty, or nine comma-separated fields describing one detection.

```
1,00,KA,3,33,33.7850,R,1  &  0  &  0  &  0
└──────── slot 0 ────────┘   s1   s2   s3
```

Because it is a full snapshot, an empty `alerts` list with
`AlertSnapshot.recognised` true is a *positive statement* — "nothing is being
detected" — and not an absence of information. That distinction is what makes
alert-end derivation possible at all.

Every known example, on either model, carries four segments. Whether this R8
uses a fixed four is **not established**; `parse_alert_snapshot` iterates
whatever it is given and records `slot_count`.

### 4.2 The nine fields of a slot

`telemetry.ALERT_FIELDS` names them in wire order. Decoder: `_parse_slot`.

| # | Name | Format | Grade | Handling |
|---|---|---|---|---|
| 0 | `active` | must be exactly `1` | UPSTREAM | Anything else rejects the slot |
| 1 | `alert_id` | `00` in every capture on either model | UPSTREAM | Kept as `alert_id_raw` (≤8 chars). **Not used as an identity** — see §4.7 |
| 2 | `band` | one of ten names | UPSTREAM | Upper-cased; must be in `BANDS` or the slot is rejected; re-checked against `collector.RECOGNISED_BANDS` before publication |
| 3 | `strength` | integer 1–8 | UPSTREAM | Must parse and must be within 1–8, or the slot is rejected |
| 4 | `raw_signal` | integer | UPSTREAM | Must parse as an integer. No unit is established |
| 5 | frequency **or** laser gun id **or** neither | three-state tagged union | mixed — see §4.3 | Dispatched on the band, never on whether the text looks numeric |
| 6 | `direction` | `F`, `S` or `R` | UPSTREAM | Must be one of the three or the slot is rejected; mapped to `front`/`side`/`rear` |
| 7 | `mute` | `1`–`6` | UPSTREAM for 1 and 2, UPSTREAM-UNVERIFIED for 3–6 | Unlisted codes leave `muted` unknown; the alert is still published |
| 8 | `receive_mode` | **absent from every known example** | UPSTREAM-UNVERIFIED | Kept verbatim as `receive_mode_raw` (≤8 chars); never given an interpreted label |

The recognised bands are `X`, `K`, `KA`, `K POP`, `KA POP`, `LASER`, `MRCD`,
`MRCT`, `RT3`, `RT4`. A band outside that set rejects the slot, and the
collector maps anything unrecognised to `"unknown"` rather than echoing it: the
state file is an artifact other software parses and must not carry arbitrary
strings from the device.

**Eight fields are required, nine are described.** Every documented example,
including both of this project's fixtures, carries eight; requiring nine would
reject every known-good packet, including the ones the requirement came from.
So `_parse_slot` requires `len(fields) >= 8` and treats field 8 as optional.

**Strictness is asymmetric on purpose.** The fields whose meaning is
established and which every consumer keys on — the active marker, the band, the
strength range, the direction — are strict, and a violation rejects the slot.
Everything else is lenient: an unlisted mute code, an absent frequency or an
unknown receive mode leaves that one value unknown and publishes the alert
anyway. Losing a real Ka warning over field 7 is the worst thing this parser
could do.

### 4.3 Field 5 is a three-state tagged union

This is the single most important decoding rule in the alert format, and
getting it wrong invents data.

| Band | Field 5 holds | Parsed as | Grade |
|---|---|---|---|
| `X`, `K`, `KA`, `K POP`, `KA POP` | a radar frequency in GHz | `float()` → `frequency_ghz` | UPSTREAM |
| `LASER` | a laser gun type identifier | `int()` → `laser_gun_id`, plus a name lookup | UPSTREAM-UNVERIFIED |
| `MRCD`, `MRCT`, `RT3`, `RT4` | **nothing with an established meaning** | neither — both stay `None` | UPSTREAM-UNVERIFIED |

The dispatch is on the band, never on whether the text happens to look
numeric, and the code says why: `3` parses as `3.0` perfectly well.

For the photo-radar and RT types, `float()` would happily turn whatever code
sits in field 5 into a plausible-looking frequency — `3` would be published as
"3 GHz", a number no radar band contains, attached to a real detection, in a
history database, indistinguishable from a measurement. Reporting no frequency
is strictly better than reporting a fabricated one. The raw text is always kept
as `field_5_raw` (≤12 chars) whatever the band, so nothing is lost.

For laser, the same logic runs the other way: reading a gun identifier as GHz
would publish "5 GHz" for a Kustom lidar.
`test_a_laser_alert_does_not_invent_a_frequency` pins that.

### 4.4 Laser gun identifiers

`telemetry.LASER_GUNS`, indexed by field 5. **Every entry is
UPSTREAM-UNVERIFIED**: the list was decompiled from the R/Tach app and no laser
packet has been captured on any hardware, on either model.

| Id | Name | Id | Name |
|---|---|---|---|
| 0 | laser | 10 | TraffiPat |
| 1 | LTI 20/20 | 11 | Truspeed S |
| 2 | Stalker | 12 | Stealth |
| 3 | RIEGL | 13 | TruCam |
| 4 | Laser Ally | 14 | XLR |
| 5 | Kustom | 15 | DragonEye Compact |
| 6 | Atlanta | 16 | DragonEye Full-Size |
| 7 | Laveg | 17 | PoliScan |
| 8 | SL700 | 18 | Traffistar s350 |
| 9 | SCS-102 | 19 | Vitronic Poliscan |

It is decoded at all because a named gun is more useful than a bare integer,
and the integer is always published alongside the name so a wrong name cannot
hide the real value. The lookup is a bounds check rather than bare indexing:
an unexpected identifier off the wire would otherwise raise inside a
notification callback, where the exception disappears into the BLE machinery
and takes the subscription with it.

### 4.5 Mute codes

`telemetry.MUTE_STATES`, field 7.

| Code | Meaning | Grade |
|---|---|---|
| `1` | not muted | UPSTREAM — a physical mute-button press was captured moving 1 to 2 |
| `2` | muted | UPSTREAM — same capture |
| `3` | mute memory | UPSTREAM-UNVERIFIED |
| `4` | auto mute memory | UPSTREAM-UNVERIFIED |
| `5` | blocked mute | UPSTREAM-UNVERIFIED |
| `6` | quiet ride mute | UPSTREAM-UNVERIFIED |

Codes 2 through 6 all count as muted. `Alert.muted` is **tri-state**: `True`,
`False`, or `None` when the code is not in the table — an unrecognised code
means "not known", and publishing that as "not muted" would be a claim nobody
can support. Schema 1's `muted` is coerced to a plain boolean for the frozen
consumer shape; schema 2 keeps the tri-state, and
`unknown_mute_codes` is counted and published so an unlisted code is visible
rather than silent.

Mute state is readable here, which means **mute can be observed without ever
sending a mute command**. That is worth stating because it removes the most
plausible argument for adding a write path.

### 4.6 The three slot states

`parse_alert_snapshot` records every slot position and what was in it:

| State | When | Effect |
|---|---|---|
| `active` | The slot decoded | Contributes an `Alert` |
| `empty` | The segment is the literal `0` | Contributes nothing; a positive "nothing here" |
| `unreadable` | The segment is neither `0` nor decodable | `recognised` goes false, `rejected_slots` increments, `uncertain` becomes true |

**An unreadable slot must never be treated as an empty one**, and this is the
reason the state is tracked per slot instead of being collapsed into a count.

The alert characteristic sends full snapshots, so the tracker in
`events.AlertTracker` ends a threat when it stops appearing in them. If a slot
that arrived but failed to decode were reported as empty, a single corrupted
byte would look like "that threat is gone" — and because the next good snapshot
would show it again, the result is a fabricated `alert_end` immediately
followed by a fabricated `alert_start`, written permanently into the history
with a duration and a peak strength that never happened. One bad byte would
manufacture a complete alert lifecycle.

So `AlertSnapshot.uncertain` is passed to `AlertTracker.observe` as
`hold_open=True`, which suppresses the end pass entirely for that snapshot.
Absence and failure are different facts and must not produce the same
behaviour. The collector counts `unreadable_slots` separately from
`unparsed_alert_packets` so both are visible in `state-v2.json`.

### 4.7 What the protocol does not give you: track identity

Field 1 is the obvious candidate for correlating one snapshot's Ka reading with
the next one's, and it reads `00` in every capture anyone has published, on
either model. **The protocol offers nothing to correlate on.**

That is a protocol fact with a large consequence, so it is stated here rather
than only in `events.py`: every notion of "the same threat over time" in this
project is *derived*, not received. `events.AlertTracker` scores candidate
matches on band family, frequency distance scaled to the band, direction
plausibility and strength continuity, stamps every derived event with
`TRACKING_ALGORITHM` (`cost-greedy-1`), and the history stores the snapshots it
worked from so a better matcher can be run over the same data later.

Three fields that look like identity and are not:

* **Direction is geometry.** Approaching and passing a fixed source gives F,
  then S, then R, for one source.
* **Frequency drifts** with signal strength, which is why the tolerance is
  per band (10 MHz for X and K, 25 MHz for Ka).
* **Band is not fixed.** K and K POP, Ka and Ka POP, MRCD and MRCT are
  reclassifications of one signal, not different threats.

Slot position is not identity either — the detector re-orders slots as signals
rise and fall — though `Alert.slot` is kept for diagnosing a capture.

---

## 5. Worked examples

**All three examples below are upstream R8w shapes, not captures from this
R8.** They are the fixtures in `tests/test_telemetry.py`, used as *shapes* to
exercise the parser. Jeremy's detector has produced telemetry of the same
seven-field shape and an all-clear alert packet; the specific bytes here are
upstream's.

### 5.1 Telemetry

```
12.1&0&W,0,193,C&0&12&D&D
```

| # | Raw | Decoded as | Grade of the reading |
|---|---|---|---|
| 0 | `12.1` | `voltage_v = 12.1` | OBSERVED (field position and meaning) |
| 1 | `0` | POI warning inactive | OBSERVED |
| 2 | `W,0,193,C` | GPS group, four fields — below | OBSERVED (shape) |
| 2.0 | `W` | `direction_8 = "W"`, west | UPSTREAM |
| 2.1 | `0` | `speed_raw = 0`, unit unknown | UPSTREAM field, no evidence for the unit |
| 2.2 | `193` | `altitude_raw = 193`, unit unknown | UPSTREAM field, no evidence for the unit |
| 2.3 | `C` | `status_raw = "C"` → `locked = True` | letter OBSERVED here, meaning UPSTREAM-UNVERIFIED |
| 3 | `0` | `field_3_raw = "0"` | UPSTREAM name (`warning`), meaning untested |
| 4 | `12` | `field_4_raw = "12"` | UPSTREAM-UNVERIFIED (`scanCount`) |
| 5 | `D` | `field_5_raw = "D"` | UPSTREAM name is "wifi status"; the R8 has no Wi-Fi (§3.6) |
| 6 | `D` | `field_6_raw = "D"` | UPSTREAM name (`brightness status`) |

Shape: `confirmed-7`. `parsed`: true. Schema 1 publishes
`{"voltage": 12.1, "gps_locked": true, "poi_warning": false, "parsed": true}`
and nothing else. Note what is absent from that: the heading, the speed and the
altitude, which are in the packet, are decoded, and stay in the detailed view.

The tripwire does not fire: sub-field 0 is `W`, which does not parse as a
float.

### 5.2 One active alert

```
1,00,KA,3,33,33.7850,R,1&0&0&0
```

Four slots. Slot 0 decodes; slots 1–3 are the literal `0` and are `empty`.

| # | Raw | Decoded as | Grade |
|---|---|---|---|
| 0 | `1` | slot is active | UPSTREAM |
| 1 | `00` | `alert_id_raw = "00"` — carried, never used for correlation | UPSTREAM |
| 2 | `KA` | `band = "KA"` | UPSTREAM |
| 3 | `3` | `strength = 3` of 8 | UPSTREAM |
| 4 | `33` | `raw_signal = 33`, unit unestablished | UPSTREAM |
| 5 | `33.7850` | `frequency_ghz = 33.785` — a frequency **because the band is Ka** | UPSTREAM |
| 6 | `R` | `direction = "rear"` | UPSTREAM |
| 7 | `1` | `mute_code = "1"` → `muted = False` | UPSTREAM |
| 8 | — | absent; `receive_mode_raw = None` | UPSTREAM-UNVERIFIED |

`field_count = 8`. Publishable form:

```json
{"band": "KA", "strength": 3, "frequency_ghz": 33.785,
 "direction": "rear", "muted": false}
```

Had the band been `MRCD` with the same field 5, `frequency_ghz` would be
`null` and `field_5_raw` would hold `33.7850` — the union in §4.3 in action.

**Nothing in this row has been seen on Jeremy's R8.** It is what the parser
would produce, verified against the parser, not against the detector.

### 5.3 All-clear

```
0&0&0&0
```

Four segments, all the literal `0`, all `empty`. `alerts` is an empty list,
`recognised` is true, `rejected_slots` is zero, `uncertain` is false. That is a
positive statement that nothing is being detected, and it is what
`AlertTracker` needs in order to end an open track.

This R8 **has** returned an all-clear alert packet, twice (EVIDENCE §7.6,
§8.6). The exact segment count of *this detector's* all-clear packet is not
recorded in the evidence ledger — the byte string above is upstream's shape.

---

## 6. POI and settings: read, deliberately not decoded

Three characteristics that no live path touches and that `uniden-r8 inspect
--confirm` reads once, into the owner-only private store.

| Characteristic | What upstream found | Grade |
|---|---|---|
| POI database `15005991-…` | Binary, variable-length records. The only POI database upstream ever read was **empty** | UPSTREAM |
| Settings 1 `2d86686a-…` | ~200 bytes, mostly undecoded | UPSTREAM |
| Settings 2 `5a87b4ef-…` | All `0xff` on upstream's R8w | UPSTREAM |

**No POI record is decoded, and that is a decision, not a gap.** Upstream
published a candidate record layout, and also recorded that the database it
read was empty. A parser built on that would be a parser that can appear to
succeed on bytes nobody has ever seen — and its output would be coordinates:
home, work, the roads Jeremy drives. A wrong address printed confidently is
worse than no address.

What `inspection.py` produces instead is shape without content:
`summarise_bytes` reports length, distinct byte count, dominant byte and
fraction, zero fraction and printable fraction — enough to answer "is it empty,
is it uniform, does it look like text" without printing a byte. For POI it also
runs `record_boundaries`, which walks the blob treating byte 0 of each record
as a type marker, using upstream's candidate lengths:

| Type byte | Candidate kind | Candidate length |
|---|---|---|
| `0x01` | speed camera | 15 |
| `0x02` | red-light camera | 14 |
| `0x03` | user mark | 12 |

Grade: **UPSTREAM-UNVERIFIED**, and the walk is printed as *suggestions*. If it
consumes the whole blob exactly, that is weak evidence the layout is right; if
it stops early, that is evidence it is wrong. Either answer is worth having and
neither is a coordinate. The raw hex goes to `.private/`, `0600`; the printed
summary contains no device bytes at all.

Settings are undecoded for the same reason with weaker consequences. The
intended route to a settings map is a diff: read, change exactly one menu item
on the detector by hand, read again, and the difference names the byte. That is
how a byte map gets built honestly, one physical toggle at a time.

Neither has been read from Jeremy's R8. There is **no evidence** for what any
of the three returns on this unit.

---

## 7. Coordinates come from somewhere else

Repeating fact 1 from the top, because it is the thing most likely to be
assumed away by someone skimming.

| Source | Field | Where it lands |
|---|---|---|
| Detector, telemetry field 2 | heading to one of eight points, speed, altitude, status letter | `detector_gps` branch of schema 2 |
| External GNSS via `gpsd` | latitude, longitude, altitude in metres, speed in m/s, true course in degrees, fix mode, satellite count, 95% error estimates, receiver timestamp | `vehicle_gnss` branch of schema 2 |

The two branches are adjacent and never merged. There is no code path that
writes a coordinate from `gnss.py` into a field named after the detector, and
that separation is not tidiness: a future reader debugging a disagreement
between the two sources needs to know which one said what, and a merged field
would make that unanswerable.

The GNSS client is off by default, and coordinate *recording* is a second,
separate switch (`gnss.record_coordinates`). With it off the client still
reports fix mode, satellite count, speed and course — enough to answer "was
there a valid fix when that alert fired", and enough to check the detector's
own speed and heading readings against a trusted source — without ever building
a record of where the vehicle has been.

---

## 8. Write commands: recorded, never sent

Upstream extracted seven application commands from `Constant.java` in the
decompiled R/Tach app. They are written to the command characteristic
`2c86686a-53dc-25b3-0c4a-f0e10c8dee20` without response.

| Command | Apparent intent |
|---|---|
| `BTreqMUTE:1` | mute |
| `BTreqMUTE:0` | unmute |
| `BTreqMMEM:1` | mute memory on |
| `BTreqMMEM:0` | mute memory off |
| `BTreqUMRK:1` | user mark on |
| `BTreqUMRK:0` | user mark off |
| `BTreqRLCD:0` | red-light camera delete |

All seven are **UPSTREAM-UNVERIFIED**. Upstream's own words: "None of these
have been tested against hardware." The apparent intents in the second column
are INFERENCE from the mnemonics.

**This project has no write path to the detector at all.** Not a disabled one —
an absent one. There is no `allow_writes` flag, here or anywhere in the
package, and `test_no_module_in_the_package_exposes_an_allow_writes_switch`
fails on one being added.

Why absence rather than a confirmation flag: the commands were lifted from a
decompiled app, upstream's own write path is documented as never having been
sent to any hardware, and the target is a live safety device in a moving
vehicle. A flag would make the capability one typo away.

How that absence is proven rather than asserted:

| Control | Where |
|---|---|
| The command characteristic is on a permanent denylist, checked first by every gate | `gatt.FORBIDDEN_UUIDS`, `_assert_not_forbidden` |
| `refuse_command()` raises unconditionally and takes no override argument | `gatt.refuse_command` |
| An AST parse of every module in the package reports any reference to a value-writing bleak API, by attribute, bare name, or `getattr` with a literal | `uniden_r8/audit.py`, run by `uniden-r8 selftest` and by two tests, one of which proves the check can fail |
| The seven commands are listed in code so the docs, the runbook and the gate cannot disagree about what is being refused | `gatt.KNOWN_WRITE_COMMANDS` |

A substring search would be useless here — these safety modules discuss
`write_gatt_char` in prose — so the audit parses instead.

One write does happen and this document does not pretend otherwise:
subscribing writes a Client Characteristic Configuration Descriptor. That is a
protocol descriptor write, it is a per-connection switch belonging to the
client, it is how a GATT client says "send me updates", and it cannot carry an
application command to the detector. The invariant defended here is *no
application-characteristic payload write*, which is narrower and provable.
`docs/SAFETY.md` §2 works through the distinction.

If a write is ever wanted, the route is in `docs/SAFETY.md`: explicit approval
for one exact query, a written record of the bytes, their provenance, the
expected response and the risk, reviewed — and only then any code.

---

## 9. What is still unknown

Everything here is an open question with what would answer it. Nothing in this
section is a hint that the answer is probably fine.

### 9.1 The whole active-alert format

| Question | What would answer it |
|---|---|
| Does the alert characteristic notify at all on this R8? | Any real detection while a capture runs. Zero alert notifications have been seen in about six minutes of connected time across three sessions, all of it with no radar source present. |
| Are the band names, the 1–8 strength scale, the direction letters and the frequency position the same on a non-W R8? | One captured detection. `uniden-r8 live --seconds 60 --full` beside a known K-band source — a supermarket door opener is the standard test — retains raw bytes in `.private/live-raw-*.json`. |
| Is field 1 ever anything but `00`? | The same capture. If it is not, track identity stays derived forever. |
| Is field 8 ever populated, and what is it? | The same capture. Absent from every example on either model. |
| How many slots does this R8's snapshot carry, and is it fixed? | The same capture, plus one with two simultaneous sources. |
| What does this detector's all-clear packet actually look like, byte for byte? | Re-reading a retained `live-raw-*.json`. It was captured; the segment count was never written down. |
| Are mute codes 3–6 real? | Exercising mute memory and quiet ride on the detector during a capture. Only 1 and 2 have hardware evidence, and that on an R8w. |
| Are the twenty laser gun identifiers real? | A laser encounter, which nobody should arrange deliberately. This is likely to stay UPSTREAM-UNVERIFIED indefinitely. |

### 9.2 Telemetry fields whose meaning is untested

| Question | What would answer it |
|---|---|
| What are fields 3, 4, 5 and 6? | The settings-diff technique applied to telemetry: capture a baseline, change exactly one detector menu item by hand, capture again, and see which field moved. Brightness is the obvious first target for field 6. |
| What is field 5 on a unit with no Wi-Fi? | The same procedure. Uniden's product page (EVIDENCE §1.2, OFFICIAL) says the R8 has no Wi-Fi, so upstream's name for it cannot be right here, and no alternative reading has any evidence. |
| Are the GPS sub-group's speed and altitude units mph and feet? | Enable `gnss` with a real receiver and log both branches of schema 2 during a drive. gpsd reports speed in m/s and altitude in metres; correlating the two answers both units at once. This is the one open question the current codebase is fully equipped to close. |
| Is sub-field 0 really a compass point on this R8? | The same log — compare `direction_8` against gpsd's `track_deg`. The tripwire in §3.4 covers the failure case in the meantime. |
| What status letters other than `C` exist, and what do they mean? | A capture taken with the detector's GPS unable to fix — indoors, or shortly after a cold start. |
| Does the POI group's active form match upstream's three-part reading? | Driving past a saved red-light camera with a capture running. Only the literal `0` has ever been seen, on either model. |

### 9.3 The attributes never read here

| Question | What would answer it |
|---|---|
| What do the 0x2901 descriptors say the vendor characteristics are called? | `uniden-r8 inspect --confirm`, parked. It is implemented and has not been run. |
| Is Settings 2 all `0xff` on this R8 as it was on the R8w? | The same command. |
| Do upstream's candidate POI record lengths fit real data? | The same command, on a detector with at least one saved mark, reading `record_boundaries` output — not by decoding the bytes. |
| What is in Settings 1? | The diff procedure in §6, one menu toggle at a time. |
| Does the command-response characteristic answer anything? | Only a command would make it interesting, and this project sends none. This question will not be answered here. |

### 9.4 Behaviour and stability

| Question | What would answer it |
|---|---|
| Does this R8 advertise outside pairing mode? | A bounded scan with the detector powered and not armed. Every sighting so far may fall inside a pairing window (EVIDENCE §6.7). |
| Do the vendor UUIDs survive a firmware update? | The compatibility gate re-checks on every connection and will refuse rather than assume. The detector is currently on 1.43, the latest release. |
| Does the notification cadence hold under radio contention with active OBD polling? | A trial run concurrently with an OBD collection trial. The five-minute trial ran beside an *idle* RFCOMM binding. `state-v2.json`'s `telemetry_interval` percentiles against the 0.97–1.02 s baseline are the cheap signal. |
| Does the alert characteristic keep up during a dense encounter? | A drive. The ingest queue records `Gap` entries with exact lost sequence numbers if it cannot. |

---

## 10. Where to look in the code

| Question | File |
|---|---|
| UUIDs, gates, denylist, the recorded write commands | `src/uniden_r8/gatt.py` |
| Both decoders, the field tables, `FIELD_CONFIDENCE`, the bounded session | `src/uniden_r8/telemetry.py` |
| Sequence numbering, gap records, alert start/update/end derivation | `src/uniden_r8/events.py` |
| The long-running collector, both published schemas, sinks, watchdog | `src/uniden_r8/collector.py` |
| External coordinates, kept separate | `src/uniden_r8/gnss.py` |
| Read-only settings and POI dump | `src/uniden_r8/inspection.py` |
| Redaction, the position gate, the loopback exemption | `src/uniden_r8/privacy.py` |
| What has actually been observed on this detector | `docs/EVIDENCE.md` |
| Why the boundary is where it is | `docs/SAFETY.md` |
