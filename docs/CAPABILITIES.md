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
| Detector heading (8-point compass) | ✅ Works | §12 — all eight points observed on one drive |
| Detector speed | ✅ Works | §12.3 — **mph**, corroborated by the driver against the trip |
| Detector altitude | 🟡 Partial | §12.2 — **metres refuted** by measurement; feet consistent but not instrumented |
| Decode 2,636 packets in motion with zero errors | ✅ Works | §12 — 0 unparsed, one 88-second gap |
| Enumerate every GATT attribute the device exposes | ✅ Works | §16.1 — 14 characteristics, no undocumented vendor surface |
| Read the settings blocks | 🟡 Partial | §13.4 — 240 B each, read fine; **contents undecoded** |
| Read the POI database | ✅ Works | §13 — first non-empty POI read reported on any R-series unit |
| Radar alert events (start / update / end) | ⚪ Untested | Implemented and unit-tested; **no real alert has ever been captured** |

### The one big gap

**No real radar detection has ever been captured from this detector.** Every
field of an *active* alert — band, strength, frequency, direction, mute state —
is decoded from a protocol documented on an **R8w**, a different product. The
only alert packet this R8 has ever produced is all-clear (`0&0&0&0`), and fifty
minutes of driving produced nothing to detect (§12.5).

The parser was hardened for this: it no longer rejects a detection because a band
string, direction code or signal value differs from the R8w's (§ see
`telemetry._parse_slot`). But "hardened" is not "verified", and this remains the
most valuable outstanding work in the project.

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
| **Continuous vehicle position** | Not from the detector. Use a USB GNSS receiver — `gpsd` support is written and tested, and has never had hardware |

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
| `gpsd` client for external coordinates | ⚪ Untested | Implemented, 59 tests, no receiver has ever been attached |

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
| Shell scripts cannot use the SIGPIPE-under-`pipefail` pattern | A scan of every shipped script, with a companion proving it fires |

**This is a radio, so it does transmit.** BlueZ scans actively, connecting and
reading exchange frames, and subscribing writes a standard CCCD descriptor. None
of that carries an application command to the detector, which is the distinction
that actually protects it — see [`SAFETY.md`](SAFETY.md).

---

## 6. Known limitations and open work

**Hardware validation still outstanding**

1. **A real radar alert.** The single most valuable thing anyone could
   contribute. An automatic-door opener is a lawful K-band source and takes ten
   minutes.
2. **A moving repeat of the §18 coordinate search.** Read-only, and it closes the
   last real gap in that result.
3. **OBD coexistence under active polling**, for one to two hours.
4. **The settings map** — one physical toggle at a time, diffing 240 opaque bytes.

**Operational, and not fixable in software**

The node's power path decides whether anything is captured at all. The PiSugar 2
(IP5209) **cannot power the Pi back on** after it cuts, so a node that powers
down stays down until somebody reaches the vehicle. Two drives were lost to this.

Since measured on the vehicle: the node ran **16 h 42 min including overnight**
with the cell holding 3.75 V and rising at one point, which is only possible on
external power — the pack alone would be flat in six to eight hours. So the
return problem is currently solved by wiring rather than by software.

That trades it for a parasitic draw of roughly 1.2–1.6 Ah per day if the feed is
always-hot rather than ignition-switched. Neither is this project's to choose;
both are in [`RUNBOOK.md`](RUNBOOK.md), "Troubleshooting", with the commands to
tell which one you have.

**Recorded defects**

[`VALIDATION.md`](VALIDATION.md) §8 carries the running list, including several
now marked resolved with the measurement that resolved them.
