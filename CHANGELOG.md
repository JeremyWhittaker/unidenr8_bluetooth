# Changelog

Notable changes, newest first. Dates are the day the work landed on `main`.

This project versions its *published documents* rather than itself: `state.json` is schema 1 and
frozen, `state-v2.json` is schema 2, and the SQLite history is schema 2. A consumer should pin the
schema it reads exactly. See [`docs/SCHEMA.md`](docs/SCHEMA.md).

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
