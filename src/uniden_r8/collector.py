"""Background collector: hold the detector link, publish a display-ready state.

This is the first thing in the project that is meant to run for a long time,
and length is what makes it different. A 30-second window that competes with
the vehicle's telemetry link is a nuisance; a process that does it for hours,
unattended, is a hazard. So the design starts from the OBDLink, not from the
detector.

**The OBDLink is primary, and that is enforced, not assumed.** Before the R8
link is opened, and every :data:`HEALTH_INTERVAL_SECONDS` while it is held, an
injected read-only probe checks that ``hummer-rfcomm`` is active and that
``/dev/rfcomm0`` exists and is bound. If that check fails the detector link is
released promptly, an ``obd-blocked`` state is published, and the collector
backs off instead of retrying into a busy radio. The probe only *reads*: it
runs ``systemctl is-active`` and ``rfcomm`` as queries, opens no serial device,
and mutates nothing. There is no code path here that starts, stops, restarts,
enables or disables any unit, or that touches ``/dev/rfcomm0``.

**Nothing is discovered and nothing is paired.** The address comes from BlueZ's
existing bond, in memory. There is no scan loop, no pairing, no trust change,
no POI or settings access, and no application-characteristic write.

**Two instances must never fight over the detector.** A ``flock`` on the state
directory makes the second one refuse rather than silently steal the link, and
it is released on every exit path.

**Raw packets are not retained.** The one-shot ``live`` command remains the
explicit private diagnostic capture; a long-running process that accumulated
payloads would grow without bound on a node with 415 MiB of RAM, and would turn
every crash into a disclosure question. This one keeps counters and the latest
conservative reading.

What gets published
-------------------
One schema-versioned JSON document, written atomically so a reader never sees a
half-written file. It carries freshness, health, counters, conservative
telemetry, recognised alerts, and a short preformatted line a display can print
without parsing anything.

It never carries an address, a token, a raw payload, heading, speed, altitude,
POI detail, an arbitrary string echoed from the detector, or exception text that
might contain an identifier.
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .evidence import DIR_MODE, FILE_MODE, utc_stamp
from .gatt import (
    ALERT_UUID,
    TELEMETRY_UUID,
    assert_live_notifiable,
    assert_live_readable,
)
from .telemetry import (
    Alert,
    Telemetry,
    check_compatibility,
    parse_alerts,
    parse_telemetry,
)

__all__ = [
    "SCHEMA_VERSION",
    "RECOGNISED_BANDS",
    "ObdHealth",
    "CollectorState",
    "SingleInstanceLock",
    "InstanceBusy",
    "default_obd_probe",
    "publish_state",
    "next_backoff",
    "display_line",
    "build_document",
    "run",
]

#: Bumped whenever the published document's shape changes.  A consumer that
#: does not recognise the version should show "unknown" rather than guess.
SCHEMA_VERSION: Final[int] = 1

#: How often the OBD health probe runs while the detector link is held.
HEALTH_INTERVAL_SECONDS: Final[float] = 15.0

#: How often the state document is rewritten while streaming.  The e-paper
#: display refreshes at 300 s, so anything faster than this is for other
#: consumers, not for the panel.
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

#: At most this many alerts are published.  The detector tracks a handful; an
#: unbounded list from a malformed packet would be a memory bug on this node.
MAX_PUBLISHED_ALERTS: Final[int] = 8

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


class InstanceBusy(RuntimeError):
    """Another collector already holds the lock."""


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


def default_obd_probe() -> ObdHealth:
    """Read-only OBDLink health check.

    Three questions, all queries: is the unit active, does the device node
    exist, and does BlueZ report a binding on it.  Nothing here mutates a unit,
    a binding, or the controller, and nothing opens the serial device — opening
    ``/dev/rfcomm0`` is exactly what the collector must never do.

    The reason string is drawn from a fixed vocabulary.  Subprocess output can
    contain a Bluetooth address, and this value is published.
    """
    import shutil  # noqa: PLC0415 - only needed on the real path
    import subprocess  # noqa: PLC0415 - queries only; see module docstring

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

    _, active_out = query(["systemctl", "is-active", _RFCOMM_UNIT])
    rfcomm_active = active_out.strip() == "active"

    device_present = Path(_RFCOMM_DEVICE).exists()

    _, rfcomm_out = query(["rfcomm"])
    bound = any(
        line.strip().startswith("rfcomm0:") for line in rfcomm_out.splitlines()
    )

    if not rfcomm_active:
        return ObdHealth(False, rfcomm_active, device_present, bound,
                         "hummer-rfcomm is not active")
    if not device_present:
        return ObdHealth(False, rfcomm_active, device_present, bound,
                         "/dev/rfcomm0 is missing")
    if not bound:
        return ObdHealth(False, rfcomm_active, device_present, bound,
                         "/dev/rfcomm0 is not bound")
    return ObdHealth(True, rfcomm_active, device_present, bound, "")


class SingleInstanceLock:
    """An advisory ``flock`` so two collectors cannot hold the detector at once.

    Advisory is enough: the only thing that would take this lock is another
    copy of this program.  It is released on every exit path, including a
    signal, because the file object is closed in a ``finally``.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
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


@dataclass
class CollectorState:
    """Everything the published document is built from."""

    mode: str = "continuous"
    status: str = "starting"
    started_at: str = field(default_factory=utc_stamp)
    obd: ObdHealth = field(default_factory=lambda: ObdHealth(False))
    connected: bool = False
    compatible: bool = False
    reconnects: int = 0
    telemetry_packets: int = 0
    alert_packets: int = 0
    unparsed_telemetry: int = 0
    latest: Telemetry | None = None
    latest_at: float | None = None
    alerts: list[Alert] = field(default_factory=list)
    note: str = ""

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
        if isinstance(state.latest.voltage, float)
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
    """Assemble the published document.  Nothing here may carry an identifier."""
    age = state.age_seconds(now)
    stale = age is not None and age > STALE_AFTER_SECONDS
    latest = state.latest
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
            "voltage": latest.voltage if latest else None,
            "gps_locked": latest.gps_locked if latest else None,
            "poi_warning": bool(latest.poi_warning) if latest else False,
            "age_s": round(age, 1) if age is not None else None,
            "stale": stale,
        },
        "alerts": [_publishable_alert(a) for a in state.alerts[:MAX_PUBLISHED_ALERTS]],
        "display_line": display_line(state, stale),
    }


def publish_state(state_dir: Path, state: CollectorState,
                  now: float | None = None) -> Path:
    """Write the document atomically, ``0600`` in a ``0700`` directory.

    Atomic because a display may read at any moment and a half-written file is
    worse than a stale one: ``os.replace`` is atomic within a filesystem, so a
    reader sees either the previous document or the new one, never a partial.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(state_dir, DIR_MODE)

    target = state_dir / "state.json"
    temporary = state_dir / f".state.json.{os.getpid()}.tmp"
    payload = json.dumps(build_document(state, now), indent=2, sort_keys=True) + "\n"

    previous = os.umask(0o077)
    try:
        temporary.write_text(payload, encoding="utf-8")
    finally:
        os.umask(previous)
    os.chmod(temporary, FILE_MODE)
    os.replace(temporary, target)
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


def _default_client(address: str):
    from bleak import BleakClient  # noqa: PLC0415 - deliberate lazy import

    return BleakClient(address, timeout=CONNECT_TIMEOUT_SECONDS)


async def _stream(  # noqa: PLR0913, PLR0917 - every parameter is an injection seam
    address: str,
    state: CollectorState,
    state_dir: Path,
    stop: asyncio.Event,
    obd_probe: Callable[[], ObdHealth],
    client_factory: Callable[[str], Any],
    deadline: float | None = None,
) -> float:
    """Hold one session.  Returns how long it lasted, in seconds.

    Returning the duration is what lets the caller tell a healthy session from
    a link that connected and dropped immediately; without that distinction a
    flapping detector resets the backoff on every failure and retries forever.

    ``deadline`` is the trial bound, and it has to be honoured *here* as well
    as between sessions.  The streaming loop below is the one place that can
    run indefinitely: a session that stays healthy never returns on its own, so
    a ``--duration`` checked only by the caller would bound every trial except
    the ones that actually worked.
    """
    began = time.monotonic()
    build = client_factory or _default_client

    # `async with` releases the link on every path out of this function --
    # normal return, exception, or cancellation from a signal.  A held link
    # matters: the detector stops advertising while connected.
    async with build(address) as client:
        state.connected = True
        state.note = ""

        missing = check_compatibility(client)
        if missing:
            state.compatible = False
            state.status = "incompatible"
            state.note = f"{len(missing)} required attribute(s) absent"
            publish_state(state_dir, state)
            return time.monotonic() - began
        state.compatible = True

        for uuid in (TELEMETRY_UUID, ALERT_UUID):
            permitted = assert_live_readable(uuid)
            try:
                payload = bytes(await client.read_gatt_char(permitted))
            except Exception:  # noqa: BLE001 - absence is evidence, not fatal
                continue
            if uuid == TELEMETRY_UUID:
                state.record_telemetry(parse_telemetry(payload))
            else:
                state.record_alerts(parse_alerts(payload))

        def on_telemetry(_sender: Any, data: bytearray) -> None:
            # bleak calls this from the event loop.  It must never raise: an
            # exception here vanishes into the BLE machinery and takes the
            # subscription with it.  Nothing is retained but the parsed value.
            with contextlib.suppress(Exception):
                state.record_telemetry(parse_telemetry(data))

        def on_alert(_sender: Any, data: bytearray) -> None:
            with contextlib.suppress(Exception):
                state.record_alerts(parse_alerts(data))

        # Subscribing writes a CCCD: a protocol descriptor write, and the only
        # write of any kind this module performs.
        subscribed: list[str] = []
        try:
            for uuid, handler in ((TELEMETRY_UUID, on_telemetry),
                                  (ALERT_UUID, on_alert)):
                permitted = assert_live_notifiable(uuid)
                try:
                    await client.start_notify(permitted, handler)
                    subscribed.append(permitted)
                except Exception:  # noqa: BLE001
                    state.note = "partial subscription"

            if not subscribed:
                state.status = "degraded"
                publish_state(state_dir, state)
                return time.monotonic() - began

            state.status = "streaming"
            publish_state(state_dir, state)

            last_health = time.monotonic()
            while not stop.is_set():
                wait_seconds = PUBLISH_INTERVAL_SECONDS
                if deadline is not None:
                    wait_seconds = min(
                        wait_seconds, max(0.0, deadline - time.monotonic())
                    )
                if wait_seconds <= 0:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=wait_seconds)
                now = time.monotonic()

                # The OBDLink comes first.  If it is no longer healthy the
                # detector link is released immediately rather than at the end
                # of some longer cycle.
                if now - last_health >= HEALTH_INTERVAL_SECONDS:
                    last_health = now
                    state.obd = obd_probe()
                    if not state.obd.healthy:
                        state.status = "obd-blocked"
                        publish_state(state_dir, state, now)
                        return time.monotonic() - began

                if not getattr(client, "is_connected", True):
                    state.note = "link dropped"
                    break

                publish_state(state_dir, state, now)
        finally:
            # Every subscription that was actually established is torn down,
            # including when the other one failed and when a signal cancelled
            # the wait.
            for permitted in subscribed:
                with contextlib.suppress(Exception):
                    await client.stop_notify(permitted)

    return time.monotonic() - began


async def run(  # noqa: PLR0913 - the injection seams are the point
    address: str,
    state_dir: str | os.PathLike[str],
    *,
    duration: float | None = None,
    obd_probe: Callable[[], ObdHealth] = default_obd_probe,
    client_factory: Callable[[str], Any] | None = None,
    rng: Callable[[], float] = random.random,
    install_signal_handlers: bool = True,
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

    state_dir = Path(state_dir)
    state = CollectorState(mode="trial" if duration is not None else "continuous")
    stop = asyncio.Event()
    deadline = time.monotonic() + duration if duration is not None else None

    loop = asyncio.get_running_loop()
    if install_signal_handlers:
        for signame in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(signame, stop.set)

    attempt = 0
    ever_compatible = False
    try:
        while not stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break

            state.obd = obd_probe()
            if not state.obd.healthy:
                # Never open the detector link while the OBDLink is unhealthy.
                state.connected = False
                state.compatible = False
                state.status = "obd-blocked"
                publish_state(state_dir, state)
                attempt += 1
                await _wait(stop, next_backoff(attempt, rng), deadline)
                continue

            state.status = "connecting"
            state.connected = False
            publish_state(state_dir, state)

            try:
                stream = _stream(
                    address, state, state_dir, stop, obd_probe, client_factory, deadline
                )
                if deadline is None:
                    lasted = await stream
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining <= 0:
                        stream.close()
                        break
                    lasted = await asyncio.wait_for(stream, timeout=remaining)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # This outer ceiling also covers a backend call that never
                # returns (connect/read/subscribe), not just the healthy loop.
                # No backend exception text is published: it can contain the
                # device address.
                lasted = 0.0
                state.note = "trial deadline" if deadline is not None else "session timed out"
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
            publish_state(state_dir, state)
            await _wait(stop, next_backoff(attempt, rng), deadline)
    finally:
        state.connected = False
        state.compatible = False
        state.status = "stopped"
        state.note = state.note or "clean shutdown"
        publish_state(state_dir, state)

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
