# Requirement and verification checklist

What was asked for, what was built, and what proves it. Each row names the
evidence rather than asserting completion.

Status values: **done** · **done, hardware-unvalidated** · **deferred** ·
**refused** · **not applicable**

`docs/HANDOFF.md` is the shorter version of this document for somebody picking
the project up. `docs/VALIDATION.md` is the queue of work that would move rows
from *hardware-unvalidated* to *done*.

---

## Part 1 — the expansion build

Requirements from the September 2026 integration research report, which audited
the project at commit `36aa1bd4` and recommended a phased expansion.

### P0 — make the read path lossless

| Requirement | Status | Evidence |
|---|---|---|
| Timestamped queue ingestion for both notification kinds | done | `events.Ingest`; `test_events.py` |
| Publish and record every alert snapshot; derive start/update/end | done | `events.AlertTracker`; `collector._Session._consume`; `test_a_short_alert_produces_a_start_and_an_end` |
| Track packet counts, parse failures, queue depth, drops, last-packet age, reconnects | done | `build_detail_document` → `counters`, `ingest`, `health`, `link.last_packet_age_s`; `test_the_queue_metrics_reach_the_detailed_document` |
| Preserve the state file as a compatibility output, written on transitions rather than only on a timer | done | `state.json` is byte-compatible schema 1; the heartbeat is a floor, not the rate; `test_state_json_is_still_schema_one_with_exactly_its_original_keys` |
| **Acceptance**: a synthetic short alert always produces start and end | done | Proven at 400 ms end to end, and in unit form |
| **Acceptance**: replayed notifications preserve order with drops visible | done | `events.Gap` with exact sequence numbers; `test_a_dropped_notification_is_visible_as_a_gap` |

Two things went further than the requirement, both because the requirement as
written would have been unsound:

- The report asked for a lossless *event* path. What was built makes the
  **snapshot stream** the lossless layer and the tracks an explicitly derived
  view, stamped with the matcher's version. A derivation presented as a record
  makes every future improvement invalidate the data already collected.
- The report proposed correlating on band, direction and frequency. Direction is
  geometry, not identity — passing a fixed source walks front → side → rear for
  one source — so keying on it would manufacture a spurious end and start at the
  most interesting moment of an encounter. Matching is a cost function instead.

### P1 — expose the full read-only packet surface

| Requirement | Status | Evidence |
|---|---|---|
| Decode direction, speed, altitude and the raw GPS status | done, hardware-unvalidated | `DetectorGps`; units have no source in this project and are labelled `unknown` |
| Retain candidate POI detail without treating it as proven | done | `PoiWarning` keeps field 1 verbatim and decodes nothing |
| Publish all alert fields and every simultaneous slot | done, hardware-unvalidated | `Alert.detailed()`, `AlertSnapshot.slots` |
| Separate radar frequency from laser gun identifier | done | A three-state union: frequency, gun id, or neither. `MRCD`/`MRCT`/`RT3`/`RT4` get no frequency, because `_float` would turn a code into a plausible number |
| Keep source-confidence labels for unvalidated fields | done | `FIELD_CONFIDENCE`, published in schema 2; `test_every_confidence_key_names_something_actually_published` |
| **Acceptance**: captured examples round-trip; malformed packets stay diagnosable | done | `test_telemetry.py`; shape grades `confirmed-7` / `extended-N` / `short-N` |

Departures from the report, each with a reason:

- **Fields 3–6 are published as `field_3_raw` … `field_6_raw`**, not under
  upstream's names. Upstream calls field 5 "wifi status"; Uniden's own product
  page says the R8 has no Wi-Fi. Publishing it under that name would assert a
  capability the manufacturer says the product lacks.
- **An alert slot needs eight fields, not nine.** The report specifies nine, and
  all three of its own examples carry eight. Requiring nine would reject every
  known-good packet, including the ones the requirement came from.
- **An unlisted mute code or an absent frequency no longer discards the
  detection.** Losing a real Ka warning over field 7 is the worst thing this
  parser could do.

### P2 — history, coordinates, and real-time delivery

| Requirement | Status | Evidence |
|---|---|---|
| SQLite WAL alert/telemetry tables with configurable retention | done | `storage.py`; `test_storage.py` |
| `gpsd` TPV/SKY ingestion with explicit `vehicle_gnss` fields | done, hardware-unvalidated | `gnss.py`; `test_gnss.py`. No GNSS receiver has been attached. |
| MQTT / WebSocket / SSE output; keep the e-paper to health status | done | `mqtt.py`, `feed.py`. SSE rather than WebSocket: it is one-way, it reconnects itself, and it needs no library. |
| Protect location files; make coordinate retention opt-in | done | `record_coordinates` defaults off; `.state/` and `*.db*` git-ignored; `_secure()` tightens the WAL sidecars; `looks_like_position` gates publication |
| **Acceptance**: every alert queryable with its nearest valid fix, duration, peak strength | done | `history encounters`; the fix is attached at write time |
| **Acceptance**: loss of GNSS does not stop radar collection | done | `Sinks` failures are independent; `test_gnss.py` covers a refused connection |

### P3 — validate on the non-W R8

| Requirement | Status |
|---|---|
| Run the capture matrix; promote fields from UPSTREAM to OBSERVED | **deferred — the detector is powered off.** `docs/VALIDATION.md` is the checklist. |

This is the largest outstanding item in the project and the highest-value work
available. Nothing in P1 or P2 changes the grade of a single protocol fact.

### P4 — inspect settings and POI, read-only

| Requirement | Status | Evidence |
|---|---|---|
| Read all vendor characteristics and their 0x2901 descriptors | done, never run against hardware | `inspection.py`, `gatt.DESCRIPTOR_READ_PLAN` |
| Snapshot settings and POI before and after exactly one physical change | done (tooling) | The procedure is in `docs/VALIDATION.md`; the command produces the snapshot |
| Version the mapping by exact model/software vector; reject an unknown vector | not applicable yet | There is no mapping. Nothing is decoded, so there is nothing to key. |

The report proposed a POI record parser. **Refused.** Upstream published a
candidate layout and also recorded that the only POI database it ever read was
empty; a parser built on that can appear to succeed on bytes nobody has seen,
and its output would be somebody's home address. The command reports lengths,
byte histograms and record-boundary candidates instead.

### P5 — narrowly scoped controls

| Requirement | Status |
|---|---|
| Subscribe to command responses; test mute/unmute; add an allowlist, audit log, rate limit and readback | **refused for now, deliberately.** |

The report itself places this last, after hardware validation that has not
happened. Beyond that: upstream's write commands were decompiled from an app and
have never been sent to hardware on any model, and the target is a live safety
device in a moving vehicle. This project's answer has been that the capability is
absent rather than disabled, and an AST audit plus two tests enforce that.

If it is to change, it changes as a decision with a written rationale, a single
reversible command, a captured response and a readback — not as a feature flag.
`docs/SAFETY.md` §2, "If a write is ever wanted", sets out the route.

### P6 — deploy in the vehicle

| Requirement | Status |
|---|---|
| A moving one-to-two-hour endurance test with active OBD polling | **deferred.** The existing evidence is a parked five-minute trial with the OBD collector idle. |
| Move the R8 to a dedicated USB adapter if the shared controller is unstable | done (support) | `collector.adapter` pins BlueZ via `bluez={"adapter": …}`; not needed yet |
| Install the unit with restart backoff, a health check and bounded logs | done | `systemd/unidenr8-collector.service`, installed by `scripts/install-service.sh`, which templates every path from the tree, refuses to install unless `selftest` passes, and verifies against the telemetry counter rather than `systemctl` |
| Document that the Pi owns the detector's single BLE connection while running | done | `README.md`, `docs/RUNBOOK.md` |

---

## Part 2 — cross-cutting requirements

| Requirement | Status | Evidence |
|---|---|---|
| The project must be replicable on a node that is not Jeremy's | done | The OBD unit, device, state directory, adapter and every sink are configuration; `guard = false` runs it with no OBDLink. `test_the_collector_runs_with_the_obd_guard_disabled` |
| No dependency may be required that is not needed | done | `bleak` and `paho-mqtt` are extras, both lazily imported; the history, GNSS client and feed are standard library |
| Nothing slow on the event loop | done | The OBD probe on a thread, SQLite on a thread, MQTT on paho's thread; `health.loop_lag_ms`; `test_the_obd_probe_never_runs_on_the_event_loop` |
| The schema-1 consumer must keep working | done | `state.json` frozen; a literal-`1` test plus a key-set test |
| No position in published output | done | `looks_like_position` in `evidence.publish()`; `test_publish_refuses_a_coordinate` |
| No address in anything committable | done | `test_repo_hygiene.py`, no exception list; the loopback exemption lives in `privacy.py` where it is reviewable |
| The tests must need no hardware | done | 618 tests, no radio, no broker, no `gpsd`, no network |
| Documentation sufficient for a stranger to take over | done | `HANDOFF`, `ARCHITECTURE`, `PROTOCOL`, `SCHEMA`, `CONFIGURATION`, `RUNBOOK`, `VALIDATION`, `SAFETY`, `EVIDENCE` |

---

## Part 3 — the earlier phases

Requirements from the original build, retained because they are still the
controls this project rests on. All were verified before the expansion and all
still pass.

| Area | Status | Test file |
|---|---|---|
| Corrected RF overclaims; "no application-characteristic write path" stated everywhere | done | `test_gatt_safety.py::test_the_docs_do_not_overclaim_radio_silence` |
| Identifier-leak enumeration covers unchanged tracked files | done | `test_repo_hygiene.py` |
| Bounded connected identity probe; ambiguity refused; address never serialized | done | `test_identity.py`, `test_selection.py` |
| Bounded receive-only vendor-data phase; compatibility gate; POI/settings refused by the live gate | done | `test_telemetry.py` |
| Pairing guards: one binary, verb allowlist, discovered protected bonds, nothing left trusted | done | `test_pairing_guards.py` |
| Background collector: OBD primacy, single instance, bounded backoff, atomic state, deterministic teardown | done | `test_collector.py` |
| AST audit of application-write APIs, with a proof it can fail | done | `test_gatt_safety.py` |

The detailed history of those phases, including the two real bugs they found —
a `--duration` that a healthy session ignored, and a hygiene scan that passed
vacuously on a clean tree — is in the git history and in `docs/EVIDENCE.md`.
