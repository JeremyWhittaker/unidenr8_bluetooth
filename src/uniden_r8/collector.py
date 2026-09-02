"""Background collector: hold the detector link, publish what it is saying.

This is the first thing in the project meant to run for a long time, and length
is what makes it different. A 30-second window that competes with the vehicle's
telemetry link is a nuisance; a process that does it for hours, unattended, is
a hazard. So the design starts from the OBDLink, not from the detector.

**The OBDLink is primary, and that is enforced, not assumed.** Before the R8
link is opened, and periodically while it is held, an injected read-only probe
checks that the configured RFCOMM unit is active and that its device node
exists and is bound. If that check fails the detector link is released promptly,
an ``obd-blocked`` state is published, and the collector backs off instead of
retrying into a busy radio. The probe only *reads*: it runs ``systemctl
is-active`` and ``rfcomm`` as queries, opens no serial device, and mutates
nothing. There is no code path here that starts, stops, restarts, enables or
disables any unit, or that touches the serial device node. It also runs on a
worker thread, never on the event loop -- see below.

**Nothing is discovered and nothing is paired.** The address comes from BlueZ's
existing bond, in memory. There is no scan loop, no pairing, no trust change,
no POI or settings access, and no application-characteristic write.

**Two instances must never fight over the detector.** A ``flock`` on the state
directory makes the second one refuse rather than silently steal the link, and
it is released on every exit path.

The event path, and the bug that shaped it
------------------------------------------
The first version of this file updated an in-memory value inside the BLE
notification callback and wrote the public document on a five-second timer. An
alert that began at second 1 and cleared at second 3 therefore began and ended
between two writes, and every consumer saw an unbroken "clear". A radar
integration that can lose a whole detection is not one.

So notifications are events now. The callback stamps two clocks, takes a
sequence number, and enqueues; a single consumer parses in arrival order,
derives ``alert_start`` / ``alert_update`` / ``alert_end`` through
:mod:`uniden_r8.events`, and publishes *on the transition*. The periodic write
is demoted to a heartbeat. When the queue does overflow -- it should not, but
"should not" is not a control -- the drop is recorded as a
:class:`uniden_r8.events.Gap` carrying the exact sequence numbers lost, so a
hole in the record is a documented hole rather than an absence nobody can date.

**Nothing slow runs on the event loop.** The same loop carries the BLE
subscription, and on BlueZ a client that stops draining its D-Bus socket is
disconnected rather than merely delayed -- so a blocking call here does not cost
latency, it costs the link. The OBD probe runs in a thread, the history writer
owns a thread, MQTT runs paho's own network thread, and the GNSS and feed
clients are non-blocking asyncio. A watchdog measures the loop's own lag and
publishes it, because that number is the cheapest early warning available for
every one of those decisions being wrong.

**Raw packets are not retained.** The one-shot ``live`` command remains the
explicit private diagnostic capture. A long-running process that accumulated
payloads would grow without bound on a node with 415 MiB of RAM, and would turn
every crash into a disclosure question.

What gets published
-------------------
Two documents, written atomically into the same owner-only directory.

``state.json`` is **schema 1**, unchanged, byte-compatible with the e-paper
consumer that already reads it. It carries freshness, health, counters,
conservative telemetry, recognised alerts, and a short preformatted line. It
never carries an address, a token, a payload, heading, speed, altitude, POI
detail, a coordinate, an arbitrary string echoed from the detector, or exception
text.

``state-v2.json`` is **schema 2**: everything above plus the full decoded
packet surface, the detector's own heading/speed/altitude, per-field confidence
grades, event and queue metrics, and the external GNSS branch. It is a superset
in content and a separate file in form, because the existing consumer requires
``schema == 1`` exactly and breaking it to add fields would be a poor trade.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import math
import os
import random
import signal
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .config import Config
from .events import AlertEvent, AlertTracker, Gap, Ingest, Notification
from .evidence import DIR_MODE, FILE_MODE, iso_from_wall_ns, utc_stamp, utc_stamp_ms
from .gatt import (
    ALERT_UUID,
    TELEMETRY_UUID,
    assert_live_notifiable,
    assert_live_readable,
)
from .telemetry import (
    FIELD_CONFIDENCE,
    Alert,
    Telemetry,
    check_compatibility,
    parse_alert_snapshot,
    parse_telemetry,
)

__all__ = [
    "SCHEMA_VERSION",
    "DETAIL_SCHEMA_VERSION",
    "RECOGNISED_BANDS",
    "ObdHealth",
    "CollectorState",
    "SingleInstanceLock",
    "InstanceBusy",
    "default_obd_probe",
    "make_obd_probe",
    "unguarded_obd_probe",
    "Sinks",
    "publish_state",
    "next_backoff",
    "display_line",
    "build_document",
    "build_detail_document",
    "run",
]

#: The document the e-paper display reads.  Frozen: a consumer that requires
#: ``schema == 1`` exists, and adding fields it cannot use is not worth
#: breaking it.  New surface goes to :data:`DETAIL_SCHEMA_VERSION` instead.
SCHEMA_VERSION: Final[int] = 1

#: The full document, in its own file.
DETAIL_SCHEMA_VERSION: Final[int] = 2

#: How often the OBD health probe runs while the detector link is held.
HEALTH_INTERVAL_SECONDS: Final[float] = 15.0

#: How often state is rewritten when nothing has changed.  Alert transitions
#: publish immediately; this is the floor, not the rate.
PUBLISH_INTERVAL_SECONDS: Final[float] = 5.0

#: Telemetry arrives about once a second.  Past this, the reading on screen is
#: describing the past and must say so.
STALE_AFTER_SECONDS: Final[float] = 10.0

#: Reconnect backoff.  Bounded and jittered so a detector that is switched off
#: cannot produce a tight retry loop against a radio the vehicle is using.
BACKOFF_BASE_SECONDS: Final[float] = 5.0
BACKOFF_FACTOR: Final[float] = 2.0
BACKOFF_MAX_SECONDS: Final[float] = 300.0
BACKOFF_JITTER: Final[float] = 0.2

#: A session that lasted at least this long counts as healthy and resets the
#: backoff.  Without it, a link that connects and immediately drops would look
#: like success and retry instantly, forever.
HEALTHY_SESSION_SECONDS: Final[float] = 30.0

#: Connect timeout for one attempt.
CONNECT_TIMEOUT_SECONDS: Final[float] = 25.0

#: Ceiling on each individual GATT read and subscribe.  bleak's own timeout
#: covers connecting and not these, and in continuous mode there is no outer
#: deadline: one call that never returns would hold the link and the vehicle's
#: radio indefinitely, with the state file frozen on "connecting".
SETUP_TIMEOUT_SECONDS: Final[float] = 20.0

#: How long a streaming session may go without a single packet before it is
#: treated as dead and torn down for a retry.  The detector sends telemetry at
#: about 1 Hz, so a minute of silence on a link BlueZ still calls connected is
#: a link that is not going to recover on its own.
SILENCE_TIMEOUT_SECONDS: Final[float] = 60.0

#: At most this many alerts are published.  The detector tracks a handful; an
#: unbounded list from a malformed packet would be a memory bug on this node.
MAX_PUBLISHED_ALERTS: Final[int] = 8

#: How many recent inter-packet intervals to keep for the timing summary.
#: About two minutes at the observed 1 Hz cadence -- long enough for a
#: percentile to mean something, short enough to react within a drive.
INTERVAL_WINDOW: Final[int] = 120

#: How often the loop-lag watchdog wakes.  Its overshoot *is* the measurement.
WATCHDOG_INTERVAL_SECONDS: Final[float] = 0.25

#: Loop lag past this is reported as unhealthy.  A quarter-second timer that
#: fires a full second late means something on this loop blocked for the best
#: part of a second, which is the same order as a D-Bus disconnect threshold.
LOOP_LAG_ALARM_MS: Final[float] = 1000.0

#: Bands this project will name.  A band outside this set is published as
#: ``"unknown"`` rather than echoed: the state file is a public artifact and
#: must not carry arbitrary strings from the device.
RECOGNISED_BANDS: Final[frozenset[str]] = frozenset(
    {"K", "KA", "X", "LASER", "MRCD", "MRCT", "RT3", "RT4", "K POP", "KA POP"}
)

#: Directions this project will name.  Same reasoning.
_RECOGNISED_DIRECTIONS: Final[frozenset[str]] = frozenset({"front", "side", "rear"})

_RFCOMM_DEVICE: Final[str] = "/dev/rfcomm0"
_RFCOMM_UNIT: Final[str] = "hummer-rfcomm"

_TELEMETRY: Final[str] = "telemetry"
_ALERT: Final[str] = "alert"


class InstanceBusy(RuntimeError):
    """Another collector already holds the lock."""


# --------------------------------------------------------------------------
# The OBDLink gate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ObdHealth:
    """The OBDLink's state, as observed read-only."""

    healthy: bool
    rfcomm_active: bool = False
    device_present: bool = False
    bound: bool = False
    #: A short fixed phrase, never free text from a subprocess: the state file
    #: is public and command output could carry an address.
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "rfcomm_active": self.rfcomm_active,
            "device_present": self.device_present,
            "bound": self.bound,
            "reason": self.reason,
        }


def make_obd_probe(
    unit: str = _RFCOMM_UNIT, device: str = _RFCOMM_DEVICE
) -> Callable[[], ObdHealth]:
    """Build a read-only OBDLink health check for a named unit and device.

    Three questions, all queries: is the unit active, does the device node
    exist, and does BlueZ report a binding on it.  Nothing here mutates a unit,
    a binding, or the controller, and nothing opens the serial device -- opening
    it is exactly what the collector must never do.

    The unit and device are parameters rather than constants because a node
    that is not Jeremy's Hummer has neither, and hard-coding them was what made
    the first version of this file unusable anywhere else.  The reason strings
    stay a fixed vocabulary: subprocess output can contain a Bluetooth address,
    and this value is published.
    """

    def probe() -> ObdHealth:
        import shutil  # noqa: PLC0415 - only needed on the real path
        import subprocess  # noqa: PLC0415 - queries only; see the docstring

        def query(args: list[str], timeout: float = 5.0) -> tuple[int, str]:
            if shutil.which(args[0]) is None:
                return 127, ""
            try:
                done = subprocess.run(  # noqa: S603 - fixed binaries, literal args
                    args, capture_output=True, text=True, timeout=timeout, check=False
                )
            except (subprocess.TimeoutExpired, OSError):
                return 124, ""
            return done.returncode, (done.stdout or "")

        _, active_out = query(["systemctl", "is-active", unit])
        rfcomm_active = active_out.strip() == "active"

        device_present = Path(device).exists()

        _, rfcomm_out = query(["rfcomm"])
        node = Path(device).name
        bound = any(
            line.strip().startswith(f"{node}:") for line in rfcomm_out.splitlines()
        )

        if not rfcomm_active:
            return ObdHealth(False, rfcomm_active, device_present, bound,
                             f"{unit} is not active")
        if not device_present:
            return ObdHealth(False, rfcomm_active, device_present, bound,
                             f"{device} is missing")
        if not bound:
            return ObdHealth(False, rfcomm_active, device_present, bound,
                             f"{device} is not bound")
        return ObdHealth(True, rfcomm_active, device_present, bound, "")

    return probe


def default_obd_probe() -> ObdHealth:
    """The Hummer node's probe: the module defaults, read at call time.

    A thin wrapper rather than a pre-built closure so that
    :data:`_RFCOMM_UNIT` and :data:`_RFCOMM_DEVICE` remain the single source of
    truth -- and so a test can point the device check at a path that exists on
    a workstation without also having to rebuild the probe.
    """
    return make_obd_probe(_RFCOMM_UNIT, _RFCOMM_DEVICE)()


def unguarded_obd_probe() -> ObdHealth:
    """Always healthy: for a node that has no OBDLink to protect.

    Not a way of switching the guard off on a node that *does* have one.  The
    configuration says which, the warning in
    :meth:`uniden_r8.config.Config.warnings` says it out loud, and the state
    document records that the gate was not armed.
    """
    return ObdHealth(True, True, True, True, "guard disabled by configuration")


class SingleInstanceLock:
    """An advisory ``flock`` so two collectors cannot hold the detector at once.

    Advisory is enough: the only thing that would take this lock is another
    copy of this program.  It is released on every exit path, including a
    signal, because the file object is closed in a ``finally``.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        # Resolved, because the lock's whole job is to be the *same* lock for
        # two processes that mean the same directory.  A relative path taken
        # from two different working directories is two different files, and
        # two collectors would then both hold the detector -- the exact
        # situation this class exists to make impossible.
        self.path = Path(path).expanduser().resolve()
        self._handle = None

    def acquire(self) -> SingleInstanceLock:
        self.path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(self.path.parent, DIR_MODE)
        previous = os.umask(0o077)
        try:
            handle = self.path.open("a+")
        finally:
            os.umask(previous)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise InstanceBusy(
                "another uniden-r8 collector is already running; refusing to "
                "open a second link to the detector"
            ) from exc
        os.chmod(self.path, FILE_MODE)
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(Exception):
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                self._handle.close()
            self._handle = None

    def __enter__(self) -> SingleInstanceLock:
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


@dataclass
class Timing:
    """Inter-packet intervals, summarised.

    Worth publishing for a reason that is not obvious: the OBD health probe
    asks three *state* questions and all three stay green while radio
    contention quietly triples this link's latency.  A widened 95th percentile
    against the 0.97-1.02 s baseline recorded in ``docs/EVIDENCE.md`` §7.2 is
    the only cheap signal this project has for coexistence trouble.
    """

    intervals: deque[float] = field(
        default_factory=lambda: deque(maxlen=INTERVAL_WINDOW)
    )
    last_monotonic_ns: int = 0

    def record(self, monotonic_ns: int) -> None:
        if self.last_monotonic_ns:
            self.intervals.append((monotonic_ns - self.last_monotonic_ns) / 1e9)
        self.last_monotonic_ns = monotonic_ns

    def summary(self) -> dict[str, Any]:
        if not self.intervals:
            return {"samples": 0, "median_s": None, "p95_s": None, "max_s": None}
        ordered = sorted(self.intervals)
        return {
            "samples": len(ordered),
            "median_s": round(ordered[len(ordered) // 2], 3),
            "p95_s": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
            "max_s": round(ordered[-1], 3),
        }


@dataclass
class CollectorState:
    """Everything the published documents are built from."""

    mode: str = "continuous"
    #: How old a reading may be before it is reported as stale.  Carried on the
    #: state rather than read from the module constant so the configured value
    #: actually reaches `build_document`, which is a pure function of this
    #: object.  It defaults to the constant, so nothing that constructs a bare
    #: CollectorState changes behaviour.
    stale_after_seconds: float = STALE_AFTER_SECONDS
    status: str = "starting"
    started_at: str = field(default_factory=utc_stamp)
    obd: ObdHealth = field(default_factory=lambda: ObdHealth(False))
    obd_guarded: bool = True
    adapter: str = ""
    connected: bool = False
    compatible: bool = False
    reconnects: int = 0
    #: Consecutive failures to write the state documents.  A full or read-only
    #: card must degrade this program, not stop it, so the failure is counted
    #: and published rather than raised.
    publish_failures: int = 0
    telemetry_packets: int = 0
    alert_packets: int = 0
    unparsed_telemetry: int = 0
    unparsed_alert_packets: int = 0
    unreadable_slots: int = 0
    unknown_mute_codes: int = 0
    latest: Telemetry | None = None
    latest_at: float | None = None
    alerts: list[Alert] = field(default_factory=list)
    note: str = ""
    #: Sequence number of the last notification the consumer processed.
    seq: int = 0
    queue: dict[str, int] = field(default_factory=dict)
    gaps: int = 0
    lost_notifications: int = 0
    loop_lag_ms: float = 0.0
    loop_lag_max_ms: float = 0.0
    timing: Timing = field(default_factory=Timing)
    open_tracks: list[dict[str, Any]] = field(default_factory=list)
    recent_events: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=20)
    )
    sinks: dict[str, Any] = field(default_factory=dict)
    gnss: dict[str, Any] | None = None

    def age_seconds(self, now: float | None = None) -> float | None:
        if self.latest_at is None:
            return None
        return max(0.0, (now if now is not None else time.monotonic()) - self.latest_at)

    def record_telemetry(self, reading: Telemetry, now: float | None = None) -> None:
        self.telemetry_packets += 1
        if not reading.parsed:
            self.unparsed_telemetry += 1
        self.latest = reading
        self.latest_at = now if now is not None else time.monotonic()

    def record_alerts(self, alerts: list[Alert]) -> None:
        self.alert_packets += 1
        self.alerts = alerts[:MAX_PUBLISHED_ALERTS]

    def clear_alerts(self) -> None:
        """Forget what was being detected, and what was being tracked.

        Called when the link goes away.  Without it the last snapshot stays in
        every published document forever: a detector switched off mid-alert
        would leave a Ka warning on the display and in the feed indefinitely,
        which is the most dangerous possible failure for this particular
        program -- a stale threat reads exactly like a live one.

        The open-track list goes with it.  It is a copy taken at the last
        publish, so clearing only the alerts would leave the documents
        disagreeing with themselves: nothing detected, one threat open.
        """
        self.alerts = []
        self.open_tracks = []


def _safe_band(band: str) -> str:
    """Return a recognised band name, or ``"unknown"``.

    The state file is published; echoing an arbitrary string the detector sent
    would put unvalidated device output into it.
    """
    candidate = (band or "").strip().upper()
    return candidate if candidate in RECOGNISED_BANDS else "unknown"


def _safe_direction(direction: str) -> str:
    return direction if direction in _RECOGNISED_DIRECTIONS else "unknown"


def _publishable_alert(alert: Alert) -> dict[str, Any]:
    frequency = alert.frequency_ghz
    return {
        "band": _safe_band(alert.band),
        "strength": alert.strength if isinstance(alert.strength, int) else None,
        "frequency_ghz": frequency if isinstance(frequency, float) else None,
        "direction": _safe_direction(alert.direction_name),
        "muted": bool(alert.muted),
    }


def _detailed_alert(alert: Alert) -> dict[str, Any]:
    """The full alert, with the same allowlisting applied to its strings.

    Every value the detector supplies as text -- band, direction, the mute
    code, the receive mode -- is either mapped through an allowlist or carried
    as a short validated token.  The schema-2 document is owner-only rather than
    published, but "owner-only" is a permission, not a reason to relax what goes
    into a file something else will parse.
    """
    detailed = alert.detailed()
    detailed["band"] = _safe_band(alert.band)
    detailed["direction"] = _safe_direction(alert.direction_name)
    # Tri-state, unlike the schema-1 view: an unrecognised mute code means "we
    # do not know", and publishing that as "not muted" would be a claim.
    detailed["muted"] = alert.muted
    return detailed


def display_line(state: CollectorState, stale: bool) -> str:  # noqa: PLR0911
    """One short line a display can print without parsing anything.

    The e-paper panel is 250x122 with six lines of text, and it refreshes every
    five minutes.  So this is a *status* line -- is the detector connected, what
    is the voltage, is anything being detected -- and not a radar alert display.
    A five-minute-old alert is not an alert.
    """
    if not state.obd.healthy:
        return "R8 paused: OBD link"
    if state.status == "stopped":
        return "R8 collector stopped"
    if not state.connected:
        return "R8 connecting..."
    if not state.compatible:
        return "R8 incompatible"
    if state.latest is None:
        return "R8 linked, no data"

    voltage = (
        f"{state.latest.voltage:.1f}V"
        if isinstance(state.latest.voltage, float) and state.latest.parsed
        else "--V"
    )
    gps = "GPS" if state.latest.gps_locked else "no-fix"
    if stale:
        return f"R8 {voltage} {gps} STALE"
    if state.alerts:
        first = _publishable_alert(state.alerts[0])
        bars = first["strength"] if first["strength"] is not None else "?"
        return f"R8 {voltage} {first['band']} {bars}/8 {first['direction']}"
    return f"R8 {voltage} {gps} clear"


def build_document(state: CollectorState, now: float | None = None) -> dict[str, Any]:
    """Assemble the schema-1 document.

    Frozen in shape.  Nothing here may carry an identifier, a coordinate, the
    detector's heading, speed or altitude, POI detail, a payload, or exception
    text -- and nothing here may carry a nanosecond timestamp either, because a
    consumer built against this shape has no field to put one in.

    Telemetry values are gated on the *confirmed* packet shape.  A packet with
    an unexpected field count may still have decoded, and its values are in the
    schema-2 document with their grade attached; this document reports the shape
    as unconfirmed and the readings as absent, which is what a consumer with no
    way to express "probably" should be told.
    """
    age = state.age_seconds(now)
    stale = age is not None and age > state.stale_after_seconds
    latest = state.latest
    confirmed = bool(latest and latest.shape_confirmed)
    return {
        "schema": SCHEMA_VERSION,
        "updated_at": utc_stamp(),
        "collector": {
            "mode": state.mode,
            "status": state.status,
            "started_at": state.started_at,
            "reconnects": state.reconnects,
            "note": state.note,
        },
        "obd": state.obd.as_dict(),
        "link": {"connected": state.connected, "compatible": state.compatible},
        "counters": {
            "telemetry_packets": state.telemetry_packets,
            "alert_packets": state.alert_packets,
            "unparsed_telemetry": state.unparsed_telemetry,
        },
        "telemetry": {
            "voltage": latest.voltage if confirmed else None,
            "gps_locked": latest.gps_locked if confirmed else None,
            "poi_warning": bool(latest.poi_warning) if confirmed else False,
            "shape_confirmed": confirmed,
            "age_s": round(age, 1) if age is not None else None,
            "stale": stale,
        },
        "alerts": [_publishable_alert(a) for a in state.alerts[:MAX_PUBLISHED_ALERTS]],
        "display_line": display_line(state, stale),
    }


def build_detail_document(
    state: CollectorState, now: float | None = None
) -> dict[str, Any]:
    """Assemble the schema-2 document: everything, with its provenance.

    A superset of schema 1 in content, a separate file in form.  It carries the
    detector's own heading, speed and altitude, which are position-adjacent --
    a log of them is a rough trace of a drive -- so it lives ``0600`` in a
    ``0700`` directory, is git-ignored, and is not what a display or a broker
    receives unless the operator turns that on.
    """
    age = state.age_seconds(now)
    stale = age is not None and age > state.stale_after_seconds
    latest = state.latest
    return {
        "schema": DETAIL_SCHEMA_VERSION,
        "updated_at": utc_stamp_ms(),
        "seq": state.seq,
        "collector": {
            "mode": state.mode,
            "status": state.status,
            "started_at": state.started_at,
            "reconnects": state.reconnects,
            "adapter": state.adapter or None,
            "note": state.note,
        },
        "obd": {**state.obd.as_dict(), "guard_enabled": state.obd_guarded},
        "link": {
            "connected": state.connected,
            "compatible": state.compatible,
            "last_packet_age_s": round(age, 3) if age is not None else None,
            "stale": stale,
        },
        "counters": {
            "telemetry_packets": state.telemetry_packets,
            "alert_packets": state.alert_packets,
            "unparsed_telemetry": state.unparsed_telemetry,
            "unparsed_alert_packets": state.unparsed_alert_packets,
            "unreadable_slots": state.unreadable_slots,
            "unknown_mute_codes": state.unknown_mute_codes,
        },
        "ingest": {
            **state.queue,
            "gaps": state.gaps,
            "lost_notifications": state.lost_notifications,
        },
        "publish_failures": state.publish_failures,
        "health": {
            "loop_lag_ms": round(state.loop_lag_ms, 1),
            "loop_lag_max_ms": round(state.loop_lag_max_ms, 1),
            "loop_lag_alarm": state.loop_lag_max_ms > LOOP_LAG_ALARM_MS,
            "telemetry_interval": state.timing.summary(),
        },
        "detector": latest.detailed() if latest else None,
        "vehicle_gnss": state.gnss,
        "alerts": [_detailed_alert(a) for a in state.alerts[:MAX_PUBLISHED_ALERTS]],
        "open_tracks": list(state.open_tracks),
        "recent_events": list(state.recent_events),
        "sinks": dict(state.sinks),
        "confidence": dict(FIELD_CONFIDENCE),
    }


def _write_atomic(target: Path, payload: str) -> None:
    """Write ``0600``, atomically.

    Atomic because a display may read at any moment and a half-written file is
    worse than a stale one: ``os.replace`` is atomic within a filesystem, so a
    reader sees either the previous document or the new one, never a partial.
    """
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    previous = os.umask(0o077)
    try:
        temporary.write_text(payload, encoding="utf-8")
    finally:
        os.umask(previous)
    os.chmod(temporary, FILE_MODE)
    os.replace(temporary, target)


async def publish_state_async(
    state_dir: Path,
    state: CollectorState,
    now: float | None = None,
    *,
    detail: bool = False,
) -> None:
    """Build the documents here, write them on a thread.

    Serialising is microseconds of CPU; writing is two files, two ``chmod``
    calls and two renames on an SD card, and that is not something to do on the
    loop holding the BLE subscription.  The documents are built first, so what
    lands on disk describes the instant the call was made rather than whenever
    the thread got scheduled.

    Failures are swallowed and counted.  A card that has filled up or gone
    read-only must cost the state file, never the radar data: the collector's
    job is to keep reading the detector.
    """
    payloads: list[tuple[str, str]] = []
    if detail:
        payloads.append((
            "state-v2.json",
            json.dumps(build_detail_document(state, now), indent=2, sort_keys=True)
            + "\n",
        ))
    payloads.append((
        "state.json",
        json.dumps(build_document(state, now), indent=2, sort_keys=True) + "\n",
    ))
    try:
        await asyncio.to_thread(_write_documents, Path(state_dir), payloads)
        state.publish_failures = 0
    except Exception:  # noqa: BLE001 - a full card degrades, it does not stop us
        state.publish_failures += 1


def _write_documents(state_dir: Path, payloads: list[tuple[str, str]]) -> None:
    """The blocking half of :func:`publish_state_async`.  Runs on a thread."""
    _ensure_state_dir(state_dir)
    for name, payload in payloads:
        _write_atomic(state_dir / name, payload)


def _ensure_state_dir(state_dir: Path) -> None:
    """Create the directory ``0700``, and tighten it only if it is loose.

    An unconditional ``chmod`` on every publish would silently undo a
    deliberate ``0750`` an operator had set for a display running as another
    account -- once per heartbeat, with no way to notice.  Tightening a
    directory that is group- or world-accessible is still right; leaving one
    the owner has narrowed differently alone is also right.
    """
    state_dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    if state_dir.stat().st_mode & 0o077:
        os.chmod(state_dir, DIR_MODE)


def publish_state(
    state_dir: Path,
    state: CollectorState,
    now: float | None = None,
    *,
    detail: bool = False,
) -> Path:
    """Write the document or documents, atomically, ``0600`` in a ``0700`` dir.

    The schema-1 file is written last.  Both files describe the same instant,
    and a consumer that polls the pair is better off seeing a schema-2 document
    that is momentarily ahead than one that is momentarily behind: ahead is a
    reading it has not shown yet, behind is a reading it has already shown
    being contradicted.
    """
    state_dir = Path(state_dir)
    _ensure_state_dir(state_dir)

    if detail:
        _write_atomic(
            state_dir / "state-v2.json",
            json.dumps(build_detail_document(state, now), indent=2, sort_keys=True)
            + "\n",
        )

    target = state_dir / "state.json"
    _write_atomic(
        target,
        json.dumps(build_document(state, now), indent=2, sort_keys=True) + "\n",
    )
    return target


def next_backoff(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Bounded exponential backoff with jitter.

    Deterministic given *rng*, so the policy is testable rather than merely
    plausible.  Jitter matters because without it every restart of a flapping
    link retries on the same cadence, which is the shape of a tight loop.
    """
    attempt = max(attempt, 1)
    raw = BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR ** (attempt - 1))
    capped = min(raw, BACKOFF_MAX_SECONDS)
    spread = capped * BACKOFF_JITTER
    return max(0.5, capped - spread + (2.0 * spread * rng()))


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------


class Sinks:
    """Everything the collector can send data to, started and stopped together.

    Each one is optional, each one fails independently, and none of them can
    stop the collector.  A broker that is down, a GNSS receiver that is
    unplugged and an SD card that is full are all conditions the vehicle will
    produce, and in every one of them the right behaviour is to keep reading the
    detector and say in the state document that the sink is unhealthy.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.history: Any = None
        self.gnss: Any = None
        self.mqtt: Any = None
        self.feed: Any = None
        self._gnss_task: asyncio.Task | None = None

    async def start(self, stop: asyncio.Event, *, adapter: str = "") -> None:
        cfg = self.config
        if cfg.history.enabled:
            from .storage import HistoryWriter  # noqa: PLC0415 - optional path

            monotonic_ns, wall_ns = time.monotonic_ns(), time.time_ns()
            self.history = HistoryWriter(
                cfg.history_path,
                retain_days=cfg.history.retain_days,
                record_motion=cfg.history.record_detector_motion,
                record_coordinates=cfg.gnss.record_coordinates,
                record_alert_snapshots=cfg.history.record_alert_snapshots,
                max_rows=cfg.history.max_rows,
            )
            # Opening waits on the writer thread, which is waiting on the
            # card.  A slow or failing open must not hold the loop either.
            await asyncio.to_thread(
                self.history.start,
                started_at=iso_from_wall_ns(wall_ns), wall_ns=wall_ns,
                monotonic_ns=monotonic_ns, adapter=adapter,
            )

        if cfg.gnss.enabled:
            from .gnss import GnssClient  # noqa: PLC0415 - optional path

            self.gnss = GnssClient(
                cfg.gnss.host, cfg.gnss.port,
                record_coordinates=cfg.gnss.record_coordinates,
                stale_after_seconds=cfg.gnss.stale_after_seconds,
            )
            self._gnss_task = asyncio.create_task(self.gnss.run(stop))

        if cfg.mqtt.enabled:
            from .config import read_password  # noqa: PLC0415
            from .mqtt import MqttPublisher  # noqa: PLC0415 - optional extra

            self.mqtt = MqttPublisher(
                host=cfg.mqtt.host, port=cfg.mqtt.port,
                username=cfg.mqtt.username, password=read_password(cfg),
                base_topic=cfg.mqtt.base_topic, detail=cfg.mqtt.detail,
                tls=cfg.mqtt.tls, home_assistant=cfg.mqtt.home_assistant,
            )
            # `connect()` is a blocking TCP connect with a DNS lookup in front
            # of it.  An unreachable broker would hold this loop for the
            # resolver's timeout before the detector's link was ever opened.
            await asyncio.to_thread(self.mqtt.start)

        if cfg.feed.enabled:
            from .feed import StateFeed  # noqa: PLC0415 - optional path

            self.feed = StateFeed(cfg.feed.bind, cfg.feed.port, detail=cfg.feed.detail)
            await self.feed.start()

    async def close(self) -> None:
        if self._gnss_task is not None:
            self._gnss_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._gnss_task
            self._gnss_task = None
        if self.feed is not None:
            with contextlib.suppress(Exception):
                await self.feed.stop()
        if self.mqtt is not None:
            with contextlib.suppress(Exception):
                self.mqtt.stop()
        if self.history is not None:
            # `stop()` joins the writer thread, which may be mid-commit on an
            # SD card.  On a thread, so a slow flush costs shutdown time and
            # not the loop that is still tearing the BLE link down.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.history.stop)

    #: Status keys that name a host or a path.  They are configuration, which
    #: the operator can already read, and they are the one part of the status
    #: block that would travel: publishing a broker's address *to* that broker,
    #: and to every viewer of the feed, discloses the shape of somebody's
    #: network for no benefit.
    _LOCATING_KEYS: Final[frozenset[str]] = frozenset({"host", "port", "path", "bind"})

    def status(self, *, locating: bool = True) -> dict[str, Any]:
        """Per-sink health.  With *locating* false, hosts and paths are omitted.

        The full form goes into the owner-only state documents; the reduced one
        is what reaches a broker or a viewer.
        """
        raw = {
            "history": self.history.status() if self.history else {"enabled": False},
            "gnss": self.gnss.status() if self.gnss else {"enabled": False},
            "mqtt": self.mqtt.status() if self.mqtt else {"enabled": False},
            "feed": self.feed.status() if self.feed else {"enabled": False},
        }
        if locating:
            return raw
        return {
            name: {
                key: value for key, value in status.items()
                if key not in self._LOCATING_KEYS
            }
            for name, status in raw.items()
        }


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------


def _default_client_factory(adapter: str = "") -> Callable[[str], Any]:
    """Build a bleak client factory, pinned to an adapter if one is named.

    Pinning matters on a node with two controllers.  Left unset, bleak asks
    BlueZ for its default adapter, which is the first powered one -- an order
    that is not guaranteed across reboots.  The documented remedy for RFCOMM
    contention is a second USB dongle, and a second dongle is useless if the
    collector might pick either.
    """

    def build(address: str) -> Any:
        from bleak import BleakClient  # noqa: PLC0415 - deliberate lazy import

        if adapter:
            # bleak 3.x: the bare `adapter=` keyword is deprecated.
            return BleakClient(
                address, timeout=CONNECT_TIMEOUT_SECONDS,
                bluez={"adapter": adapter},
            )
        return BleakClient(address, timeout=CONNECT_TIMEOUT_SECONDS)

    return build


class _Session:
    """One held link, from connect to teardown.

    A class rather than a function because the notification callbacks, the
    consumer loop and the teardown all need the same handful of objects, and
    threading eight parameters through three closures was how the first version
    of this file became hard to follow.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - every parameter is a seam
        self,
        address: str,
        state: CollectorState,
        state_dir: Path,
        stop: asyncio.Event,
        obd_probe: Callable[[], ObdHealth],
        client_factory: Callable[[str], Any],
        config: Config,
        sinks: Sinks,
        deadline: float | None = None,
    ) -> None:
        self.address = address
        self.state = state
        self.state_dir = state_dir
        self.stop = stop
        self.obd_probe = obd_probe
        self.client_factory = client_factory
        self.config = config
        self.sinks = sinks
        self.deadline = deadline
        self.ingest = Ingest(config.collector.queue_size)
        self.tracker = AlertTracker()
        self.wake = asyncio.Event()
        self.last_history_telemetry = 0.0
        self.last_history_fix = 0.0

    # ------------------------------------------------------------ callbacks

    def _on_notification(self, kind: str, data: Any) -> None:
        """The whole of what happens on bleak's notification path.

        Copy, stamp, number, enqueue, wake.  No parsing, no disk, no locks.
        The exception guard is narrow on purpose: swallowing everything here is
        how a queue overflow becomes invisible, so only the enqueue itself is
        protected and the failure is counted rather than discarded.
        """
        try:
            self.ingest.offer(kind, bytes(data))
        except Exception:  # noqa: BLE001 - must never reach the BLE machinery
            self.state.lost_notifications += 1
        self.wake.set()

    # -------------------------------------------------------------- consume

    def _consume(self) -> list[AlertEvent]:
        """Drain the queue, in arrival order, and return the transitions."""
        events: list[AlertEvent] = []
        while (record := self.ingest.pop()) is not None:
            if isinstance(record, Gap):
                self.state.gaps += 1
                self.state.lost_notifications += record.count
                self.state.recent_events.appendleft(record.as_dict())
                # A gap means snapshots were lost, so what the tracker believes
                # about open threats may be wrong.  Saying so is better than
                # quietly carrying on.
                self.state.note = "notifications dropped"
                continue
            events.extend(self._apply(record))
        return events

    def _apply(self, note: Notification) -> list[AlertEvent]:
        self.state.seq = note.seq
        if note.kind == _TELEMETRY:
            self.state.timing.record(note.monotonic_ns)
            reading = parse_telemetry(note.payload)
            self.state.record_telemetry(reading)
            self._record_history_telemetry(reading, note)
            return []

        snapshot = parse_alert_snapshot(note.payload)
        if self.sinks.history is not None:
            # Before anything is derived from it.  The snapshot is the record;
            # the tracks below are a view of it that a later matcher may
            # disagree with.
            self.sinks.history.record_alert_snapshot(snapshot, note)
        self.state.record_alerts(snapshot.alerts)
        if not snapshot.recognised:
            self.state.unparsed_alert_packets += 1
        self.state.unreadable_slots += snapshot.rejected_slots
        self.state.unknown_mute_codes += snapshot.unknown_mute_codes

        # A slot that arrived and could not be read holds every open track
        # open.  Absence and failure are different facts, and ending a track on
        # a decode failure would fabricate a whole alert lifecycle from one bad
        # byte -- permanently, in the history.
        return self.tracker.observe(
            snapshot.alerts, seq=note.seq,
            monotonic_ns=note.monotonic_ns, wall_ns=note.wall_ns,
            hold_open=snapshot.uncertain,
        )

    def _record_history_telemetry(
        self, reading: Telemetry, note: Notification
    ) -> None:
        if self.sinks.history is None:
            return
        every = self.config.history.telemetry_every_seconds
        now = note.monotonic_ns / 1e9
        if every and now - self.last_history_telemetry < every:
            return
        self.last_history_telemetry = now
        self.sinks.history.record_telemetry(
            reading, wall_ns=note.wall_ns, monotonic_ns=note.monotonic_ns
        )

    # -------------------------------------------------------------- publish

    def _fix(self) -> Any:
        return self.sinks.gnss.fix if self.sinks.gnss is not None else None

    def _record_history_fix(self, now: float) -> None:
        """Store a GNSS fix on the same throttle as telemetry.

        Alert rows already carry the fix that was current when they were
        written, which is what "where did that alert happen" needs.  This is the
        separate track: a sampled record of the route, so a drive can be
        reconstructed rather than only its detections.  It respects the same
        coordinate opt-in, so with `record_coordinates` off it stores fix
        quality, speed and course and no position at all.
        """
        if self.sinks.history is None:
            return
        fix = self._fix()
        if fix is None:
            return
        every = self.config.history.telemetry_every_seconds
        if every and now - self.last_history_fix < every:
            return
        self.last_history_fix = now
        self.sinks.history.record_fix(fix, monotonic_ns=fix.monotonic_ns)

    def _dispatch(self, events: list[AlertEvent]) -> None:
        """Send transitions everywhere they go.  Never raises."""
        fix = self._fix()
        for event in events:
            record = event.as_dict()
            self.state.recent_events.appendleft(record)
            if self.sinks.history is not None:
                self.sinks.history.record_alert_event(event, fix)
            if self.sinks.mqtt is not None:
                self.sinks.mqtt.publish_event(record)
            if self.sinks.feed is not None:
                self.sinks.feed.publish_event(record)

    async def _publish(self, now: float | None = None) -> None:
        state = self.state
        state.queue = self.ingest.metrics.as_dict()
        state.open_tracks = [track.summary() for track in self.tracker.open_tracks]
        state.sinks = self.sinks.status()
        fix = self._fix()
        state.gnss = (
            fix.detailed(include_coordinates=self.config.gnss.record_coordinates)
            if fix is not None else None
        )
        await publish_state_async(
            self.state_dir, state, now, detail=self.config.collector.detail
        )
        # Outward copies get the reduced sink status: no broker address, no
        # database path.  Rebuilt around that rather than filtered afterwards,
        # so a future key cannot leak by being forgotten in a filter.
        outward = state.sinks
        state.sinks = self.sinks.status(locating=False)
        try:
            if self.sinks.mqtt is not None:
                self.sinks.mqtt.publish_state(
                    build_detail_document(state, now) if self.config.mqtt.detail
                    else build_document(state, now)
                )
            if self.sinks.feed is not None:
                self.sinks.feed.publish_state(
                    build_detail_document(state, now) if self.config.feed.detail
                    else build_document(state, now)
                )
        finally:
            state.sinks = outward

    # ------------------------------------------------------------------ run

    async def run(self) -> float:
        """Hold one session.  Returns how long it lasted, in seconds.

        Returning the duration is what lets the caller tell a healthy session
        from a link that connected and dropped immediately; without that
        distinction a flapping detector resets the backoff on every failure and
        retries forever.
        """
        began = time.monotonic()
        state = self.state

        # `async with` releases the link on every path out of this function --
        # normal return, exception, or cancellation from a signal.  A held link
        # matters: the detector stops advertising while connected.
        async with self.client_factory(self.address) as client:
            state.connected = True
            state.note = ""

            missing = check_compatibility(client)
            if missing:
                state.compatible = False
                state.status = "incompatible"
                state.note = f"{len(missing)} required attribute(s) absent"
                await self._publish()
                return time.monotonic() - began
            state.compatible = True

            for uuid in (TELEMETRY_UUID, ALERT_UUID):
                permitted = assert_live_readable(uuid)
                try:
                    # Bounded explicitly.  bleak's timeout covers connecting,
                    # not a read that never answers, and in continuous mode
                    # there is no outer deadline to catch one -- a stalled read
                    # would hold the detector's link and the vehicle's radio
                    # forever, silently.
                    payload = bytes(await asyncio.wait_for(
                        client.read_gatt_char(permitted),
                        timeout=SETUP_TIMEOUT_SECONDS,
                    ))
                except Exception:  # noqa: BLE001 - absence is evidence, not fatal
                    continue
                kind = _TELEMETRY if uuid == TELEMETRY_UUID else _ALERT
                self.ingest.offer(kind, payload, source="read")
            self._dispatch(self._consume())

            subscribed: list[str] = []
            try:
                # Subscribing writes a CCCD: a protocol descriptor write, and
                # the only write of any kind this module performs.
                for uuid, kind in ((TELEMETRY_UUID, _TELEMETRY), (ALERT_UUID, _ALERT)):
                    permitted = assert_live_notifiable(uuid)
                    try:
                        await asyncio.wait_for(
                            client.start_notify(
                                permitted,
                                lambda _s, data, kind=kind:
                                    self._on_notification(kind, data),
                            ),
                            timeout=SETUP_TIMEOUT_SECONDS,
                        )
                        subscribed.append(permitted)
                    except Exception:  # noqa: BLE001
                        state.note = "partial subscription"

                if not subscribed:
                    state.status = "degraded"
                    await self._publish()
                    return time.monotonic() - began

                state.status = "streaming"
                await self._publish()
                await self._pump(client)
            finally:
                for permitted in subscribed:
                    # Bounded like the setup calls, and for the same reason:
                    # this is the teardown path, so an unsubscribe that never
                    # returns would hold the link open forever *while trying to
                    # release it*, which is the worst place to be unbounded.
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            client.stop_notify(permitted),
                            timeout=SETUP_TIMEOUT_SECONDS,
                        )
                # Whatever arrived before teardown is still worth having, and
                # any threat still open when the link goes away must be ended
                # rather than left live in the history forever.
                self._dispatch(self._consume())
                self._dispatch(
                    self.tracker.close(seq=self.state.seq, wall_ns=time.time_ns())
                )
                # And forget what was on screen.  A detector switched off
                # mid-alert would otherwise leave a Ka warning in the state
                # file, the feed and the broker indefinitely, and a stale
                # threat reads exactly like a live one.
                self.state.clear_alerts()

        return time.monotonic() - began

    async def _pump(self, client: Any) -> None:
        """The streaming loop: wake on a packet, publish on a transition."""
        state = self.state
        # Packets can only start arriving now, so this is where the silence
        # window opens.
        #
        # It has to be a local, and that is the whole subtlety.
        # ``state.latest_at`` deliberately outlives the session that set it --
        # it is what the published documents age their reading against, and
        # nulling it would republish the previous session's voltage as fresh,
        # which is the one thing this program must never do.  But using it
        # alone would charge a brand-new session for the *previous* one's
        # silence: since the commonest way a session ends is this very
        # timeout, every reconnect after the first would be killed before a
        # packet could arrive, forever.  Taking the later of the two also
        # stops the session being charged for its own connect and setup, which
        # can legitimately take most of a minute.
        streaming_since = time.monotonic()
        last_health = streaming_since
        last_publish = 0.0
        heartbeat = self.config.collector.heartbeat_seconds

        while not self.stop.is_set():
            wait = heartbeat
            if self.deadline is not None:
                wait = min(wait, max(0.0, self.deadline - time.monotonic()))
                if wait <= 0:
                    return
            # Clear *before* checking the queue, and skip the wait if anything
            # is already there.  Clearing after a check would discard a wakeup
            # for a record that arrived in between, and the consumer would then
            # sit on it until the heartbeat.
            self.wake.clear()
            if not len(self.ingest):
                # Raced against the stop event, so SIGTERM is acted on at once
                # rather than whenever the next packet or heartbeat arrives.
                waiters = [
                    asyncio.ensure_future(self.wake.wait()),
                    asyncio.ensure_future(self.stop.wait()),
                ]
                try:
                    await asyncio.wait(
                        waiters, timeout=wait, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for waiter in waiters:
                        waiter.cancel()
            if self.stop.is_set():
                return

            now = time.monotonic()
            events = self._consume()
            if events:
                self._dispatch(events)
            self._record_history_fix(now)

            # The OBDLink comes first.  The probe runs subprocesses, so it goes
            # to a thread: on this loop it could block for up to ten seconds,
            # which is long enough for BlueZ to drop the subscription.
            if now - last_health >= self.config.obd.interval_seconds:
                last_health = now
                state.obd = await asyncio.to_thread(self.obd_probe)
                if not state.obd.healthy:
                    state.status = "obd-blocked"
                    await self._publish(now)
                    return

            if not getattr(client, "is_connected", True):
                state.note = "link dropped"
                return

            # A link BlueZ still calls connected but that has stopped speaking
            # is the failure mode a reconnect exists for, and nothing else
            # detects it: the OBD gate is green, the client is "connected", and
            # the state file freezes on a reading that is quietly hours old.
            #
            # Measured from whichever is later: this session starting to
            # stream, or the last packet actually received.  A session that has
            # never received a single packet is covered by the first, which the
            # earlier "age is not None" form was not -- it streamed forever.
            silent_for = now - max(streaming_since, state.latest_at or 0.0)
            if silent_for > SILENCE_TIMEOUT_SECONDS:
                state.note = "no packets"
                return

            # Publish on a transition, or on the heartbeat.  A transition is
            # the whole point; the heartbeat is what keeps freshness honest
            # when nothing is happening.
            if events or (now - last_publish) >= heartbeat:
                last_publish = now
                await self._publish(now)


async def _watchdog(state: CollectorState, stop: asyncio.Event) -> None:
    """Measure how late this loop's own timer is, and publish the answer.

    The cheapest useful diagnostic in the whole project.  Everything that could
    starve the BLE notification path -- a synchronous disk commit, a blocking
    socket, a subprocess -- shows up here as overshoot, and a number in the
    state document is how a problem that would otherwise present as "the link
    keeps dropping" becomes a problem with a cause.
    """
    while not stop.is_set():
        began = time.monotonic()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop.wait(), timeout=WATCHDOG_INTERVAL_SECONDS
            )
        if stop.is_set():
            return
        lag_ms = (time.monotonic() - began - WATCHDOG_INTERVAL_SECONDS) * 1000.0
        state.loop_lag_ms = max(0.0, lag_ms)
        state.loop_lag_max_ms = max(state.loop_lag_max_ms, state.loop_lag_ms)


async def run(  # noqa: PLR0913 - the injection seams are the point
    address: str,
    state_dir: str | os.PathLike[str],
    *,
    duration: float | None = None,
    obd_probe: Callable[[], ObdHealth] | None = None,
    client_factory: Callable[[str], Any] | None = None,
    rng: Callable[[], float] = random.random,
    install_signal_handlers: bool = True,
    config: Config | None = None,
) -> int:
    """Run the collector until stopped, or until *duration* elapses.

    ``duration`` is the bounded trial mode: the same code path, with a hard
    stop.  It is how a first supervised run should happen, following the OBD
    project's own trial precedent, rather than enabling a service and hoping.

    Returns a process exit status: 0 for a clean stop, 1 if it never managed a
    compatible session.
    """
    if (
        duration is not None
        and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        )
    ):
        raise ValueError("duration must be finite and greater than zero")
    if duration is not None:
        duration = float(duration)

    settings = config or Config()
    state_dir = Path(state_dir)
    if obd_probe is None:
        obd_probe = (
            make_obd_probe(settings.obd.unit, settings.obd.device)
            if settings.obd.guard else unguarded_obd_probe
        )
    if client_factory is None:
        client_factory = _default_client_factory(settings.collector.adapter)

    state = CollectorState(
        mode="trial" if duration is not None else "continuous",
        obd_guarded=settings.obd.guard,
        adapter=settings.collector.adapter,
        stale_after_seconds=settings.collector.stale_after_seconds,
    )
    stop = asyncio.Event()
    deadline = time.monotonic() + duration if duration is not None else None

    loop = asyncio.get_running_loop()
    if install_signal_handlers:
        for signame in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(signame, stop.set)

    sinks = Sinks(settings)
    watchdog: asyncio.Task | None = None

    attempt = 0
    ever_compatible = False
    try:
        # Inside the try, so a failure anywhere after the history thread has
        # started still reaches the finally that joins it.  Outside, a broker
        # that refused a connection would leak a live writer thread holding an
        # open database for the life of the process.
        await sinks.start(stop, adapter=settings.collector.adapter)
        watchdog = asyncio.create_task(_watchdog(state, stop))

        while not stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break

            state.obd = await asyncio.to_thread(obd_probe)
            state.sinks = sinks.status()
            if not state.obd.healthy:
                # Never open the detector link while the OBDLink is unhealthy.
                state.connected = False
                state.compatible = False
                state.status = "obd-blocked"
                await publish_state_async(
                    state_dir, state, detail=settings.collector.detail
                )
                attempt += 1
                await _wait(stop, next_backoff(attempt, rng), deadline)
                continue

            state.status = "connecting"
            state.connected = False
            await publish_state_async(
                state_dir, state, detail=settings.collector.detail
            )

            session = _Session(
                address, state, state_dir, stop, obd_probe, client_factory,
                settings, sinks, deadline,
            )
            try:
                if deadline is None:
                    lasted = await session.run()
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining <= 0:
                        break
                    lasted = await asyncio.wait_for(session.run(), timeout=remaining)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # This outer ceiling also covers a backend call that never
                # returns (connect/read/subscribe), not just the healthy loop.
                # No backend exception text is published: it can contain the
                # device address.
                lasted = 0.0
                state.note = (
                    "trial deadline" if deadline is not None else "session timed out"
                )
            except Exception:  # noqa: BLE001 - a failed session is routine
                # Deliberately no exception text: it can carry the address, and
                # this state file is published.
                lasted = 0.0
                state.note = "session failed"
            finally:
                state.connected = False

            ever_compatible = ever_compatible or state.compatible

            if stop.is_set():
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            if lasted >= HEALTHY_SESSION_SECONDS:
                attempt = 0          # a real session resets the policy
            else:
                attempt += 1
            state.reconnects += 1
            state.status = "reconnecting"
            await publish_state_async(
                state_dir, state, detail=settings.collector.detail
            )
            await _wait(stop, next_backoff(attempt, rng), deadline)
    finally:
        stop.set()
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog
        state.connected = False
        state.compatible = False
        state.status = "stopped"
        state.note = state.note or "clean shutdown"
        state.sinks = sinks.status()
        await publish_state_async(
            state_dir, state, detail=settings.collector.detail
        )
        await sinks.close()

    return 0 if ever_compatible else 1


async def _wait(stop: asyncio.Event, seconds: float,
                deadline: float | None) -> None:
    """Sleep, but wake early on stop or on the trial deadline."""
    if deadline is not None:
        seconds = min(seconds, max(0.0, deadline - time.monotonic()))
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
