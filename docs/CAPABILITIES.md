# Capabilities

Everything this project can and cannot do, with the evidence for each.

This is the honest inventory. If you are deciding whether this is useful to you,
read the three tables and stop; if you are deciding whether to trust a specific
claim, follow it into [`EVIDENCE.md`](EVIDENCE.md), which records every
measurement with the run it came from.

**How to read the status column**

| | |
|---|---|
| ✅ **Works** | Proven against the real detector, on the target hardware, with the run recorded |
| 🟡 **Partial** | Works, with a stated limit — usually "the code is right but the meaning is inherited" |
| ⚪ **Untested** | Implemented and unit-tested, never met the hardware it is for |
| ❌ **Not available** | Looked for and not found, or refused by design |

The distinction that matters most in this project: **observed on this non-W R8**
versus **inherited from an R8w**. Roughly half of what is publicly "known" about
this protocol has never been confirmed against any detector, and this document
never rounds the second up to the first.

---

## 1. Getting data out of the detector

| Capability | Status | Evidence |
|---|---|---|
| Discover, pair, and hold a BLE link | ✅ Works | §6 — bond survives, left untrusted and disconnected |
| Read model, firmware and software revision | ✅ Works | §6 — `BTM10`, `ATTOWAVE`, firmware 1.43, database 20260702 |
| Live telemetry at ~1 Hz | ✅ Works | §7.2 — measured 0.97–1.02 s |
| Battery voltage | ✅ Works | §7.4 — 12.0–13.7 V across sessions, tracks engine state |
| GPS fix state | ✅ Works | §12.1 — the status letter decoded by correlation, not assumption |
| Detector heading (8-point compass) | ✅ Works | §20.5 — checked against GNSS true bearing; W within 0.6°, every well-sampled point within ~5° |
| Detector speed | ✅ Works | §20.3 — **mph**, `det = 1.0003 × gps + 0.851`, 89% within ±2 mph against a GNSS reference |
| Detector altitude | ✅ Works | §20.4 — **feet above MSL**, agreeing with GNSS to a few feet once the fix matures; metres refuted by +844 ft |
| Decode 2,636 packets in motion with zero errors | ✅ Works | §12 — 0 unparsed, one 88-second gap |
| Enumerate every GATT attribute the device exposes | ✅ Works | §16.1 — 14 characteristics, no undocumented vendor surface |
| Read the settings blocks | 🟡 Partial | §13.4 — 240 B each, read fine; **contents undecoded** |
| Read the POI database | ✅ Works | §13 — first non-empty POI read reported on any R-series unit |
| Radar alert events (start / update / end) | ✅ Works | §19 — a real Ka encounter, 252 packets, 0 rejected, 0 unrecognised |
| Every active-alert field: band, strength, raw signal, frequency, direction, mute | ✅ Works | §19.1 — all promoted from UPSTREAM to OBSERVED in one capture |

### The gap that closed

**A real Ka encounter was captured on 2026-09-04** — 252 packets, none rejected,
none unrecognised — and every active-alert field moved from UPSTREAM to
OBSERVED in one go (§19). The decoded frequency, `35.4780`, matches what the
driver read off the detector's own display, recorded before the capture was
retrieved.

What the capture also produced is a defect nothing else could have found:
`BAND_TOLERANCE_GHZ["KA"]` was 0.025 GHz, and the detector's own frequency
reading for one physical source jitters by 0.030. So the matcher split a single
encounter into **six** tracks, some under a second long (§19.5).

That is now fixed, and fixed from the measurement rather than from a guess. The
snapshots were replayed through the tracker at a range of tolerances; the pass
collapses to one track at 0.035 and above. The tolerance is now **0.050** —
the measured jitter with room, where the room is safe because the US Ka
allocations police radar uses sit 700–900 MHz apart, so a 50 MHz window cannot
bridge two real sources. `TRACKING_ALGORITHM` moved to `cost-greedy-2`
accordingly, the first time that stamp has changed.

This is what the lossless snapshots were for: the tracks were re-derived from
stored bytes, and doing so also corrected the track count this document
originally reported as seven (§19.5.1).

**Still unobserved:** more than one simultaneous threat (every packet carried a
single active slot), and every mute code except `1`.

---

## 2. Coordinates and position

The most-asked question, and the one most easily misread. Four separate claims:

| Question | Answer | Evidence |
|---|---|---|
| Live lat/lon in the 1 Hz telemetry packet? | ❌ **No** | §10.8, §12 — the field upstream numbering suggests is a compass point; thousands of packets with the coordinate tripwire silent |
| Live position **anywhere** on the device? | ❌ **No** | §18 — searched for directly, with a positive control |
| Stored coordinates readable from the detector? | ✅ **Yes** | §13 — the POI characteristic, decoded and measured |
| A coordinate the detector derived from its own fix? | ✅ **Yes — 8.0 m and 3.8 m** | §13.5, §13.11 — two locations kilometres apart |

### How the "no live position" claim was established

Weak negatives say "we did not find one". This one is stronger. A mark was
created so the detector's **own current fix was known as exact bytes**, and then
every attribute it exposes was searched for those bytes in **40 encodings** —
float32 and float64 in both byte orders, jitter-tolerant 3-byte prefixes, scaled
integers at 1e5/1e6/1e7, and ASCII decimal at several precisions.

The POI data acted as a positive control and the search found the coordinate
there, so the search demonstrably works. It appears nowhere else: not in either
240-byte settings block, not in telemetry, not in the alert characteristic, not
in the command response (§18).

**What that does not cover**, stated plainly: the vehicle was stationary, both
settings blocks were read once rather than watched, and a compressed or
non-adjacent encoding would defeat a substring search regardless.

### What you *can* get

| | |
|---|---|
| **A position sample on demand** | Create a mark, read it, delete it. ~10 s per cycle, one flash write per sample. Measured to 3.8 m. §13.11, §15.3 |
| **A 1 Hz stream of nearby saved points** | The POI characteristic notifies once a second with the whole current window. Coordinates of *saved places near you*, not of the vehicle. §16.2, §17.1 |
| **Where a radar source was detected** | With `gnss.record_coordinates` on, every alert row carries the fix current when it fired. Read it back with `history events --full`. To map the *source*, take the highest-`strength` `alert_update`, not the `alert_end` — by the end you have driven past it |
| **Continuous vehicle position** | Not from the detector. Use a USB GNSS receiver — proven on hardware (§20). Position is stored only if you opt in with `record_coordinates` |

### What you cannot get

**A full export of the POI database.** The characteristic does not expose a stable
database that you can page through — it exposes whatever the detector currently
considers *nearby*, recomputed as the vehicle moves. Two reads ten minutes apart
from a stationary vehicle shared no bytes at all (§13.7). A read is a sample of a
moving window, not a backup. The official Windows USB tool's `UMR` command
remains the only candidate for a real export, and it is untested here.

**A meaning for the GPS status letter `E`.** Seen twice, both times on the first
packet after the link came up. Two samples is not a meaning, so `locked` returns
"unknown" for it rather than guessing (§12.1).

---

## 3. Writing to the detector

**The installed package cannot write to the detector.** Nothing in
`src/uniden_r8/` puts an application value on a vendor characteristic; an AST
audit proves it, and that audit runs in `selftest` and in CI.

Separately, and at the owner's explicit instruction, commands *have* been sent
from standalone scripts outside the package. What was learned:

| Command | Status | Evidence |
|---|---|---|
| `BTreqUMRK:1` — add a user mark | ✅ Works | §14 — first demonstration on any R-series detector |
| `BTreqUMRK:0,<LAT>,<LON>` — targeted delete | ✅ Works | §15.2 — **uppercase hex only** |
| `BTreqUMRK:0` bare — delete nearby | ⚪ Untested | Selects a record the caller did not choose; the targeted form is strictly safer |
| `BTreqMUTE:1` / `:0` — mute | ⚪ Untested | Documented upstream, never sent |
| `BTreqMMEM:…` — mute memory | ⚪ Untested | Never sent |
| `BTreqRLCD:0` — delete red-light camera | ⚪ Untested | Not going to be, on a database the owner did not build |
| `BTreqSETC:<i>=<v>` — settings | ⚪ Untested | Never sent |
| Long-press "delete all" equivalent | ❌ Unknown | No documented BLE form. Not something to arrive at by guessing |

### Two things that will bite you

**Hex arguments must be UPPERCASE.** The detector's parse is case-sensitive and
it does not *reject* a lowercase argument — it acts on the mis-parse. A lowercase
delete is not a no-op; it is a delete aimed at a coordinate nobody chose (§15.2).

**Never select a record by its position in the list.** The returned set is
ordered nearest-first and is recomputed as the vehicle moves, so a mark made
where you are standing sorts *first*, not last. Identify records by set
difference against a baseline read. Getting this wrong deleted the wrong record
in this project's own testing (§17.3).

---

## 4. What the software does with the data

| Capability | Status | Notes |
|---|---|---|
| Long-running collector as a systemd service | ✅ Works | Installed, enabled at boot, survives reboot |
| `state.json` (schema 1) for the e-paper display | ✅ Works | Live on the vehicle; frozen schema for the sibling project |
| `state-v2.json` (schema 2), owner-only | ✅ Works | Every decoded field, `0600` in a `0700` directory |
| SQLite history — sessions, telemetry, alerts, GNSS | ✅ Works | 1 Hz sampling proven over a 50-minute drive |
| Alert snapshots stored **verbatim** | ✅ Works | Lossless: a real alert survives even if the parser reads it wrongly |
| Retention, clock-immune | ✅ Works | Measures from `min(now, tenth-newest row)`, not `now` — the board has no RTC |
| `drive-report.sh` — read a drive back | ✅ Works | Flags the silent failure where motion fields were never recorded |
| OBD-II coexistence guard | 🟡 Partial | Proven with the RFCOMM link **idle**; a drive under active polling is still outstanding |
| MQTT + Home Assistant discovery | ⚪ Untested | Implemented, unit-tested, no broker has ever been attached |
| Web dashboard over SSE | ⚪ Untested | Implemented, unit-tested, never run against the real feed |
| `gpsd` client for external coordinates | ✅ Works | §20 — BU-353S4 on a drive; 535 fixes, all 3D. Supplied the reference that validated three detector fields |

---

## 5. Privacy and safety properties

These are enforced, not promised.

| Property | Enforced by |
|---|---|
| No Bluetooth address in any published output | `privacy.py` tokenisation; `evidence.publish()` refuses; a repository scan of every committable file with **no exception list** |
| No coordinate in published output | `privacy.looks_like_position`, called by `publish()`; a scan of every doc and the README |
| No application write path in the package | `audit.py` parses the AST of every module; runs in `selftest` and CI, with a companion test proving the audit can still fail |
| `state.json` stays schema 1 | A test pinning its exact key set — a sibling project depends on it |
| Position-adjacent files are `0600` in a `0700` directory | A test that reads the modes back |
| Nothing slow on the asyncio event loop | A test, plus a published `health.loop_lag_ms` — on a Pi Zero 2 W it peaks around 2.6 ms |
| The state file never walks backwards in time | A publish ticket taken when the document is built; a write carrying an older ticket is dropped. Three ablation-tested regression tests |
| Shell scripts cannot use the SIGPIPE-under-`pipefail` pattern | A scan of every shipped script, with a companion proving it fires |

**This is a radio, so it does transmit.** BlueZ scans actively, connecting and
reading exchange frames, and subscribing writes a standard CCCD descriptor. None
of that carries an application command to the detector, which is the distinction
that actually protects it — see [`SAFETY.md`](SAFETY.md).

---

## 6. Known limitations and open work

**Hardware validation still outstanding**

1. **A radar alert since the matcher changed.** `cost-greedy-2` widened the Ka
   tolerance from a measurement (§19.5.1); no encounter has been captured since.
2. **A moving repeat of the §18 coordinate search.** Read-only, and it closes the
   last real gap in that result.
3. **OBD coexistence under active polling**, for one to two hours.
4. **The settings map** — one physical toggle at a time, diffing 240 opaque bytes.
5. **A drive with real elevation change.** §20.4 settled altitude's *unit* and
   *datum* but not its responsiveness: over a 175 ft range the regression slope
   is confounded by GNSS vertical noise, so no claim is made either way.

**Operational, and not fixable in software**

The node's power path decides whether anything is captured at all. The PiSugar 2
(IP5209) **cannot power the Pi back on** after it cuts, so a node that powers
down stays down until somebody reaches the vehicle. Two drives were lost to this.

An earlier reading of this was **wrong, and is corrected here.** The node once
ran 16 h 42 min including overnight with the cell holding 3.75 V, and that was
taken as proof of an always-hot external feed, on the reasoning that the pack
alone would be flat in six to eight hours. The reasoning was sound; the
conclusion did not survive a measurement.

The cell was logged for 3¼ hours against the vehicle's actual state:

| time (local) | volts | rate | vehicle |
|---|---|---|---|
| 08:26 | 4.161 | — | parked |
| 09:08–09:22 | 4.179 | **+0.13 V/h** | **engine running** |
| 10:18 | 3.966 | −0.242 V/h | parked |
| 11:47 | 3.769 | −0.145 V/h | parked |

The only interval in which the cell gained charge is the drive. **The feed is
ignition-switched**, so the node charges while the engine runs and discharges
whenever it does not — roughly **−0.145 V/h**, which is about two and a half
hours from a parked 3.77 V to the 3.40 V action threshold, and six to eight
hours from full. So the "solved by wiring" conclusion above was premature: the
return problem is not solved, it is merely deferred by however long the pack
lasts.

There is consequently **no parasitic-draw trade-off to weigh** — the earlier
1.2–1.6 Ah/day figure applied only to an always-hot feed, which this is not.
[`RUNBOOK.md`](RUNBOOK.md), "Troubleshooting", has the commands to tell which
one a given installation has; the discharge slope while parked is the test, and
a flat or rising cell on a parked vehicle is the signature of always-hot.

**Recorded defects**

[`VALIDATION.md`](VALIDATION.md) §8 carries the running list, including several
now marked resolved with the measurement that resolved them.
