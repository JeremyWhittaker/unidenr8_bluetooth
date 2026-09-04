# Changelog

Notable changes, newest first. Dates are the day the work landed on `main`.

This project versions its *published documents* rather than itself: `state.json` is schema 1 and
frozen, `state-v2.json` is schema 2, and the SQLite history is schema 3. A consumer should pin the
schema it reads exactly. See [`docs/SCHEMA.md`](docs/SCHEMA.md).

## 2026-09-03

The day the detector gave up a coordinate.

### The headline

- **A coordinate recovered from the detector, measured to 8.0 m and 3.8 m** at
  two locations kilometres apart. Pressing MARK stores a position the detector
  derived from its own fix, and the record reads back over BLE.
- **`BTreqUMRK:1` works over BLE** — the first demonstration on any R-series
  detector. Upstream documented the command from a decompiled app and recorded
  that it had never been sent to hardware.
- **The add/delete transaction is reversible**, verified byte for byte.
- **The POI record layout is settled: 13 / 12 / 10**, confirmed at four
  different blob sizes, refuting the reading this project had been carrying. All
  three record types now observed.
- **The first drive**: 2,636 packets in motion, 0 unparsed, all eight compass
  headings. Speed is mph; altitude is refuted as metres. The GPS status letter
  decoded by correlation — `C` is a fix, `D` is not.

### Added

- `uniden-r8 survey` — enumerates the device's own GATT tree instead of a
  catalogue inherited from a different model. Reads no characteristic value.
  Found 14 characteristics and **no undocumented vendor surface**, plus a 1 Hz
  coordinate-bearing stream on the POI characteristic that nothing had ever
  subscribed to.
- `uniden-r8 poi-diff` and `uniden_r8.poi_diff` — compares two POI captures and
  reports distances, never coordinates.
- `scripts/drive-report.sh` — reads a drive back, and flags the silent failure
  where motion fields were never recorded.
- `scripts/poi-capture.sh` — one POI read that cannot leave the collector
  stopped.
- `scripts/install-service.sh`, `uninstall-service.sh`, and
  `--allow-user-control` for a narrow passwordless grant on one unit.
- SQLite history schema 3 with an additive migration.
- `docs/CAPABILITIES.md` — the works / partial / untested / unavailable
  inventory, and the README rewritten around it.

### Fixed

- A drive captured nothing because the repository shipped a unit template no
  script installed and a runbook that said not to enable it.
- The unit's own sandbox would have blocked the OBD guard: `RestrictAddressFamilies`
  lacked `AF_BLUETOOTH`, which `rfcomm` needs. Caught before installation.
- A cold boot could lose a whole drive — bond resolution exited rather than
  waiting, burning the restart limit.
- The reconnect backoff ceiling cut from 300 s to 60 s, so a truck starting
  after a night parked is picked up within a minute.
- **The alert gate would have discarded the first real detection.** It rejected
  a whole slot over a band string, direction code or signal value inherited from
  a different product. Now structural only.
- `_safe_word` destroyed an active POI warning's text; `_safe_group` keeps it.
- Retention swept once per process start; now also every six hours.
- `uninstall-service.sh` refused to uninstall anything, and `install-service.sh`
  mis-detected a running collector — both the same SIGPIPE-under-`pipefail` bug.
  There is now a test scanning every shipped script for the pattern.
- A privacy control that the clock could fail 200 seconds a day.
- Two flaky tests, one of which reached CI.
- `poi_diff` selected added records by byte offset, which this project's own
  evidence says is wrong: the set comes back reordered. Now selected by content.
- Documentation that claimed the project never runs `sudo` and installs no unit.

### Learned the hard way

- **Hex arguments to the detector must be UPPERCASE.** Lowercase is not
  rejected — it is mis-parsed and acted on, so a lowercase delete targets a
  coordinate nobody chose.
- **Never select a POI record by position.** The set is nearest-first and
  recomputed as the vehicle moves. A position-based selection deleted the wrong
  record during this project's own testing.
- The POI characteristic returns a moving nearby window, not a database, so a
  full export over BLE is not possible.

### Still not verified

No real radar alert has ever been captured — fifty minutes of driving produced
nothing to detect, and every active-alert field remains inherited from an R8w.
MQTT, the dashboard and the `gpsd` client have never met hardware.

## 2026-09-02 (evening)

### Added

- **`scripts/install-service.sh` and `scripts/uninstall-service.sh`.** The installer templates
  `User=`, `Group=`, `WorkingDirectory=`, `ExecStart=`, `PYTHONPATH`, `UNIDEN_R8_CONFIG` and
  `ReadWritePaths=` from the tree it runs out of and the node's own configuration, refuses to
  install unless `selftest` passes, warns when `[history] enabled` is off or the OBD guard names a
  unit the host lacks, and verifies success by watching the telemetry counter advance rather than
  trusting `systemctl is-active`. Idempotent; supports `--dry-run` and `--no-enable`.
- **`inspection.evaluate_layouts()`.** Runs both graded POI record-layout hypotheses
  (`whole-record`, 13/12/10; `payload-plus-header`, 15/14/12) against a capture and reports which
  one, if either, consumes the blob exactly. One capture now settles the question either way.
- **`uniden-r8 poi-diff` and `uniden_r8.poi_diff`.** Compares two POI captures already in the
  private store and reports what changed: bytes, record boundaries, which layout fits, and — given
  a reference fix supplied in a file rather than on a command line — how far the decoded point is
  from it. It never prints a coordinate. This is the offline half of the user-mark experiment.
- SQLite history schema 3: `telemetry.poi_raw` and `telemetry.poi_suspect`, with an additive
  migration from schema 2 so an existing database is upgraded rather than orphaned.

### Fixed

- **A drive captured nothing.** The repository shipped a systemd unit template that no script
  installed, and the runbook said not to enable it. Measured cost: one 2.5-hour drive with zero
  data. `scripts/install-service.sh` exists because of this (`docs/EVIDENCE.md` §11.1).
- **The unit's own sandbox would have blocked the OBD guard, even once installed.**
  `RestrictAddressFamilies=` lacked `AF_BLUETOOTH`, which the guard's `rfcomm` subprocess needs to
  open `socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_RFCOMM)` — measured with `strace` on the node.
  Without it the guard read "not bound" permanently and the collector sat in `obd-blocked` forever
  while systemd reported `active (running)`. Found before the unit was installed (§11.2).
- **A cold boot could lose a whole drive.** `_cmd_collect` exited 1 when `bonded_detector_address()`
  failed, which under `Restart=always` burns the unit's five-per-hour start limit in about two
  minutes and leaves it dead for the rest of the hour. A continuous collector now waits with
  backoff; a bounded trial still fails fast.
- **The reconnect backoff could cost the first five minutes of a drive.** The ceiling was 300 s, so
  a Pi left powered overnight reached it and then made the engine-on reconnect wait. Now 60 s.
- **The first real alert could have been silently discarded.** The slot gate rejected a whole
  detection when the band string, the direction code or the raw signal did not match values taken
  from a *different product* — and the raw signal's scale is documented here as unknown. The gate is
  now structural (active marker, field count, strength 1–8); an unfamiliar band, direction or signal
  marks that one value unknown and publishes the detection. `state.json` saying "clear" while the
  detector's screen shows Ka is the one output this parser must never produce.
- **The direction code was not case-normalised** although the band was, so a lowercase direction
  would have thrown away the detection.
- **A rejected slot left only a counter.** It now keeps its sanitised text, or a printable shape
  description when the text cannot be sanitised — the packet that would explain why the parser is
  wrong is no longer the one packet the parser discards.
- **`_safe_word` threw away an active POI warning's text.** `SPEEDCAM,500,35` failed the
  alphanumeric check wholesale and came back `raw: null`; `collect` keeps no raw packets, so it was
  gone. `_safe_group()` applies the same rules per sub-field and rejoins them. The POI group also
  gained the coordinate tripwire the GPS group already had.
- **The POI text was then discarded again at the SQLite boundary.** The `telemetry` table stored
  only a boolean, so a drive past a known camera left nothing to analyse afterwards.
- **Retention swept only once, at writer start.** A service left running for weeks carried rows past
  `retain_days` indefinitely. The writer thread now also sweeps every six hours.
- **The OBD guard could not tell "`rfcomm` could not run" from "nothing is bound."** Both read as
  `{device} is not bound`, sending an operator to the vehicle's link when the fault was on the host.
- **`record_boundaries` could step past the end of a blob**, and `evaluate_layouts` then reported the
  overshoot as a completed walk — on exactly the shape this test produces, a single 10-byte user mark
  read under a 12-byte layout.
- **A failed atomic publish leaked a per-PID temporary file**, and `Restart=always` guarantees a new
  PID each time. The usual cause is a full card, where leaking a file per attempt makes it worse.
- **`uniden-r8 history` gated its two output paths unequally.** The publication gate only walks keys
  when handed JSON, so the table branch fell back to a regex and a lone latitude column would have
  passed. Both branches are now gated on the structured rows, before rendering.
- `systemd/unidenr8-collector.service`: `Restart=on-failure` → `Restart=always` (continuous mode has
  no clean self-exit); added `Group=`; added `AF_BLUETOOTH` to `RestrictAddressFamilies=`.
- A test that only passed on a machine which had never run the tool (`history` resolved the default
  database against the process working directory).

### Added, for reading a drive back

- **`scripts/drive-report.sh`.** Summarises what a session actually captured,
  from the local history alone. Flags the failure that has been silent before —
  motion fields not recorded — rather than printing an empty column, prints any
  POI warning text, and calls out a non-all-clear alert packet verbatim, since
  that is the evidence this project has never had.

### Fixed after the first real install

The first `install-service.sh` run on the vehicle found three defects in the
installer itself. All were caught by the service failing loudly rather than by
it looking healthy, which is what the counter-verification is for.

- **It warned that a hand-started collector "will be replaced" and then replaced
  nothing.** The old process kept the single-instance lock, the unit exited 3 on
  every start, and the restart counter climbed toward the limit while the install
  reported success. It now stops them and waits for them to go, excluding the
  unit's own MainPID so re-running on a healthy node does not kill the service.
- **`RestartPreventExitStatus=2 3`.** Exit 2 (unreadable config, or `bleak`
  missing) and exit 3 (another instance holds the lock) are conditions a restart
  cannot fix. Retrying into them burned all five starts in about two minutes and
  left systemd refusing the unit for the rest of the hour — a fixable mistake
  turning into a dead unit and a lost drive.
- **Running it with `sudo` silently installed a root service.** `User=` is
  derived from `id -un`. The script now refuses to run as root and explains
  which account to use. (Root also could not actually run: the unit's empty
  `CapabilityBoundingSet` leaves it without `CAP_DAC_READ_SEARCH`, so it cannot
  read the `0771` source tree and dies with `ModuleNotFoundError`.)
- The installer now runs `systemctl reset-failed` before restarting, so the
  documented "re-run to upgrade" path works on the one node that needs it most —
  the one where something already went wrong.

### Verified on hardware

Parked with the engine running and the RFCOMM link bound and active: telemetry across several
sessions with **0 unparsed**, voltage 13.4–13.7 V, GPS locked throughout, OBD invariants unchanged,
and the e-paper display reading the collector's state. With `record_detector_motion` on and
`telemetry_every_seconds = 1.0`, rows carrying heading, speed and altitude were recorded at 1 Hz.
The schema 2→3 migration was proved against a copy of the real 728-row database before it was
allowed near the live one. Details in [`docs/EVIDENCE.md`](docs/EVIDENCE.md) §11.

### Still not verified

No real alert has ever been captured. No validation has happened with the vehicle moving — heading,
speed and altitude are read but not checked against a reference. The POI/user-mark coordinate
experiment has never been run, and the POI record layout remains two graded hypotheses, neither
checked against a populated database on any detector. See
[`docs/VALIDATION.md`](docs/VALIDATION.md).

## 2026-09-02

### Added

- **Lossless alert events.** Notifications are stamped, sequence-numbered and queued in the BLE
  callback; a consumer derives `alert_start` / `alert_update` / `alert_end` with duration, peak
  strength and peak raw signal. A dropped notification produces a `Gap` record naming the exact
  sequence numbers lost.
- **The full read-only packet surface**, with a per-field confidence grade published alongside it.
- **`state-v2.json`**, schema 2: the complete decoded surface, queue and loop-health metrics, open
  tracks, recent events and the external GNSS branch. `state.json` is unchanged.
- **A local SQLite history** on its own writer thread, with `uniden-r8 history`.
- **A `gpsd` client**, kept in its own `vehicle_gnss` branch — the detector sends no coordinates.
- **MQTT publication** with optional Home Assistant discovery, and a standard-library HTTP/SSE
  dashboard. Both off by default.
- **`uniden-r8 inspect`** — one confirmed read-only dump of the settings blocks and POI database
  into the private store. Decodes nothing.
- **A TOML configuration.** The OBD unit, device, Bluetooth adapter, state directory and every sink
  are settings now rather than constants, so this runs on a host that is not the original node.

### Fixed

- The five-second publisher could erase a whole detection: an alert that began and cleared between
  two publications reached no consumer.
- Alert correlation keyed on direction, which is geometry rather than identity — passing a fixed
  source walks front → side → rear for one source, so the matcher manufactured a spurious end and
  start at the closest point of every encounter.
- A live alert stayed latched in every published document after the detector went away.
- Retention could delete the whole history on a board with no real-time clock. It is now primarily a
  clock-immune row budget, with the wall-clock sweep capped and recorded.
- Publishing did synchronous SD-card writes on the loop holding the BLE subscription.
- Continuous mode had no ceiling on a stalled GATT call and no way to notice a link that had stopped
  speaking.
- A non-finite float from the wire made every published document invalid JSON.
- An empty or truncated alert notification read as a confident "all clear" and ended live tracks.
- The SQLite WAL sidecars took the process umask while holding the newest rows.

### Verified on hardware

184 packets across three windows with none unparsed; a 120-second collector trial at zero dropped,
zero gaps and 2.6 ms peak loop lag; WAL recovery after an abrupt kill; and every OBD invariant
unchanged. The GPS sub-group was read for the first time, which turned "the detector sends no
coordinates" from an inherited assumption into a measurement. Details in
[`docs/EVIDENCE.md`](docs/EVIDENCE.md) §10.

### Still not verified

No real radar detection has ever been captured from a non-W R8. Every active-alert field remains
R8w evidence. See [`docs/VALIDATION.md`](docs/VALIDATION.md).

## Earlier

Bounded discovery, guarded pairing, the Device Information identity probe, the bounded receive path
and the first background collector. The telemetry packet format was confirmed on a real R8 during
this period; the alert format was not. See `docs/EVIDENCE.md` §§6–8.
