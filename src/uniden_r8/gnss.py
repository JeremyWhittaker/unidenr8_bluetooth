"""Coordinates, from somewhere that actually has them.

The detector does not put latitude or longitude on the wire.  It knows where it
is -- red-light-camera warnings are impossible otherwise -- but the live GPS
sub-group carries a heading to the nearest of eight compass points, a speed, an
altitude and a status letter, and that is all.  Anything that needs a real
position, a real course, or a timestamp with a fix quality attached has to get
it from a separate receiver.

This module is that separate receiver's client.  It speaks the ``gpsd`` JSON
protocol over a TCP connection, which means any USB GNSS puck, a phone feeding
``gpsd``, or a shared vehicle receiver works without this project knowing
anything about NMEA, serial ports or chipsets.

Kept honestly separate
----------------------
Everything here lands in its own branch of the published schema, named
``vehicle_gnss``, next to and never merged with ``detector_gps``.  There is no
code path that writes a coordinate from this module into a field named after
the detector.  That separation is not tidiness: a future reader debugging a
disagreement between the two sources needs to know which one said what, and a
merged field would make that unanswerable.

Two things it will not do
-------------------------
**It will not block the event loop.**  ``asyncio.open_connection`` and
``StreamReader.readline`` are used rather than a socket read, because the same
loop holds the BLE link, and a receiver that stops answering must cost this
module its connection rather than costing the detector its subscription.

**It will not record coordinates unless told to.**  With
``gnss.record_coordinates`` off -- the default -- the client still connects,
still reports fix mode, satellite count, speed and course, and still lets the
collector answer "was there a valid fix when that alert fired".  It simply
never puts a latitude anywhere.  That is a genuinely useful configuration: it
validates the detector's own speed and heading readings against a trusted
source without building a record of where the vehicle has been.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from dataclasses import dataclass, replace
from typing import Any, Final

__all__ = [
    "DEFAULT_PORT",
    "WATCH_COMMAND",
    "Fix",
    "GnssClient",
    "parse_tpv",
    "parse_sky",
]

#: gpsd's default TCP port, as listed in ``/etc/services``.
DEFAULT_PORT: Final[int] = 2947

#: What a client sends to start the stream.  gpsd answers a bare connection
#: with a VERSION banner and then nothing at all until it is asked to watch, so
#: a client that only reads sits silent forever.
WATCH_COMMAND: Final[bytes] = b'?WATCH={"enable":true,"json":true}\n'

#: Reconnect policy.  A GNSS receiver that is unplugged mid-drive must not
#: produce a tight connect loop against a host that is not listening.
BACKOFF_BASE_SECONDS: Final[float] = 2.0
BACKOFF_MAX_SECONDS: Final[float] = 60.0

#: Longest a single line may be before the connection is treated as broken.
#: A SKY report with many satellites is a few kilobytes; anything far past that
#: is not gpsd.
MAX_LINE_BYTES: Final[int] = 65536

#: gpsd fix modes, from the JSON protocol's TPV report.
FIX_MODES: Final[dict[int, str]] = {
    0: "unknown",
    1: "no fix",
    2: "2D fix",
    3: "3D fix",
}


@dataclass(frozen=True)
class Fix:
    """One position report, with everything needed to judge how good it is.

    The accuracy estimates are kept because a fix without them is a number
    without a claim attached.  ``epx``/``epy`` are gpsd's 95%-confidence
    longitude and latitude error estimates in metres, and a five-metre fix and
    a five-hundred-metre fix should not be recorded as if they were the same
    fact.
    """

    mode: int = 0
    lat: float | None = None
    lon: float | None = None
    altitude_m: float | None = None
    speed_mps: float | None = None
    track_deg: float | None = None
    climb_mps: float | None = None
    epx_m: float | None = None
    epy_m: float | None = None
    satellites: int | None = None
    #: gpsd's own timestamp for the fix, as sent.  Kept verbatim rather than
    #: re-parsed: it comes from the satellites, which makes it the one clock in
    #: this system that a Pi with no RTC can trust.
    device_time: str | None = None
    monotonic_ns: int = 0
    wall_ns: int = 0

    @property
    def valid(self) -> bool:
        """A 2D or 3D fix.  Modes 0 and 1 are the receiver saying it has none."""
        return self.mode >= 2

    @property
    def mode_name(self) -> str:
        return FIX_MODES.get(self.mode, "unknown")

    @property
    def speed_mph(self) -> float | None:
        """Speed in mph, for comparison with the detector's own reading."""
        return None if self.speed_mps is None else self.speed_mps * 2.236936

    def age_seconds(self, now_ns: int | None = None) -> float:
        now = time.monotonic_ns() if now_ns is None else now_ns
        return max(0.0, (now - self.monotonic_ns) / 1e9)

    def detailed(self, *, include_coordinates: bool) -> dict[str, Any]:
        """The published form.

        *include_coordinates* is threaded through explicitly rather than read
        from a module global so that every call site has to decide, and so a
        test can prove both answers.
        """
        document: dict[str, Any] = {
            "source": "gpsd",
            "mode": self.mode,
            "mode_name": self.mode_name,
            "valid": self.valid,
            "speed_mps": self.speed_mps,
            "speed_mph": self.speed_mph,
            "track_deg": self.track_deg,
            "climb_mps": self.climb_mps,
            "altitude_m": self.altitude_m,
            "epx_m": self.epx_m,
            "epy_m": self.epy_m,
            "satellites": self.satellites,
            "device_time": self.device_time,
            "age_s": round(self.age_seconds(), 2),
        }
        if include_coordinates:
            document["lat"] = self.lat
            document["lon"] = self.lon
        else:
            # Present and null, not absent.  A consumer must be able to tell
            # "coordinates are switched off" from "this build has no GNSS".
            document["lat"] = None
            document["lon"] = None
            document["coordinates_withheld"] = True
        return document


def _number(payload: dict[str, Any], *names: str) -> float | None:
    """Return the first numeric value among *names*, or ``None``.

    gpsd renames fields between releases -- altitude has been ``alt``,
    ``altHAE`` and ``altMSL`` -- so every read tries the alternatives rather
    than pinning one spelling and silently reporting nothing on a different
    version.
    """
    for name in names:
        value = payload.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def parse_tpv(payload: dict[str, Any], *, monotonic_ns: int, wall_ns: int) -> Fix:
    """Turn one TPV report into a :class:`Fix`.

    Tolerant by construction: gpsd omits any field it has no value for, so
    every read is an optional read and a missing key is normal rather than an
    error.
    """
    mode = payload.get("mode")
    return Fix(
        mode=mode if isinstance(mode, int) and not isinstance(mode, bool) else 0,
        lat=_number(payload, "lat"),
        lon=_number(payload, "lon"),
        altitude_m=_number(payload, "altMSL", "altHAE", "alt"),
        speed_mps=_number(payload, "speed"),
        track_deg=_number(payload, "track", "magtrack"),
        climb_mps=_number(payload, "climb"),
        epx_m=_number(payload, "epx"),
        epy_m=_number(payload, "epy"),
        device_time=payload.get("time") if isinstance(payload.get("time"), str) else None,
        monotonic_ns=monotonic_ns,
        wall_ns=wall_ns,
    )


def parse_sky(payload: dict[str, Any]) -> int | None:
    """Return the number of satellites used in the fix, from a SKY report."""
    used = payload.get("uSat")
    if isinstance(used, int) and not isinstance(used, bool):
        return used
    satellites = payload.get("satellites")
    if isinstance(satellites, list):
        return sum(
            1 for entry in satellites
            if isinstance(entry, dict) and entry.get("used") is True
        )
    return None


class GnssClient:
    """A reconnecting gpsd reader.

    Owns one task.  :meth:`run` returns only when the stop event is set; every
    failure in between -- host down, receiver unplugged, malformed line -- is a
    reconnect with backoff, not an exception, because the collector must keep
    collecting radar data when the GPS puck falls out of its socket.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = DEFAULT_PORT,
        *,
        record_coordinates: bool = False,
        stale_after_seconds: float = 5.0,
        connector: Any = None,
    ) -> None:
        self.host = host
        self.port = port
        self.record_coordinates = record_coordinates
        self.stale_after_seconds = stale_after_seconds
        #: Injection seam: the tests supply a pair of streams so the whole
        #: protocol path is exercisable with no gpsd and no network.
        self._connector = connector or self._open
        self._fix: Fix | None = None
        self._satellites: int | None = None
        self.connected = False
        self.connects = 0
        self.reports = 0
        self.malformed = 0
        self.last_error = ""

    # ------------------------------------------------------------- reading

    @property
    def fix(self) -> Fix | None:
        """The most recent valid fix, or ``None`` if there is none or it is old.

        Staleness is judged on the monotonic clock: a receiver that stopped
        answering ten seconds ago has no current position, whatever it said
        last, and returning the stale value would attach a coordinate to an
        alert that happened somewhere else.
        """
        if self._fix is None or not self._fix.valid:
            return None
        if self._fix.age_seconds() > self.stale_after_seconds:
            return None
        return self._fix

    def status(self) -> dict[str, Any]:
        current = self.fix
        return {
            "enabled": True,
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "connects": self.connects,
            "reports": self.reports,
            "malformed": self.malformed,
            "record_coordinates": self.record_coordinates,
            "last_error": self.last_error,
            "fix": (
                current.detailed(include_coordinates=self.record_coordinates)
                if current else None
            ),
        }

    # --------------------------------------------------------------- lifecycle

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(self.host, self.port)

    async def run(self, stop: asyncio.Event) -> None:
        """Connect, watch, and keep reading until *stop* is set."""
        attempt = 0
        while not stop.is_set():
            try:
                await self._session(stop)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                self.last_error = type(exc).__name__
                attempt += 1
            finally:
                self.connected = False
            if stop.is_set():
                break
            delay = min(
                BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** min(attempt, 5))
            )
            # Jitter for the same reason the detector's backoff has it: a
            # flapping source must not settle into a fixed retry cadence.
            delay *= 0.8 + 0.4 * random.random()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _session(self, stop: asyncio.Event) -> None:
        reader, writer = await self._connector()
        self.connected = True
        self.connects += 1
        self.last_error = ""
        # The read is raced against the stop event rather than simply awaited.
        # A GNSS receiver with no fix can be silent for minutes, and a bare
        # `await readline()` would hold shutdown open for exactly that long --
        # which on a vehicle means the systemd stop timeout expires and the
        # process is killed with the detector link still open.
        stopper = asyncio.ensure_future(stop.wait())
        pending_read: asyncio.Future | None = None
        try:
            writer.write(WATCH_COMMAND)
            await writer.drain()
            while not stop.is_set():
                if pending_read is None:
                    pending_read = asyncio.ensure_future(reader.readline())
                done, _ = await asyncio.wait(
                    {pending_read, stopper},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stopper in done:
                    return
                line = pending_read.result()
                pending_read = None
                if not line:
                    return  # the far end closed; reconnect
                if len(line) > MAX_LINE_BYTES:
                    self.malformed += 1
                    continue
                self._consume(line)
        finally:
            # The read task is cancelled rather than abandoned: an orphaned
            # task holding a closed stream is the kind of thing that turns into
            # a "task was destroyed but it is pending" line at exit and a
            # reference nothing ever collects.
            for task in (pending_read, stopper):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            self.connected = False
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _consume(self, line: bytes) -> None:
        """Decode one line.  A bad line is counted, never raised."""
        try:
            payload = json.loads(line.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            self.malformed += 1
            return
        if not isinstance(payload, dict):
            self.malformed += 1
            return

        report = payload.get("class")
        if report == "TPV":
            self.reports += 1
            fix = parse_tpv(payload, monotonic_ns=time.monotonic_ns(),
                            wall_ns=time.time_ns())
            if self._satellites is not None:
                fix = replace(fix, satellites=self._satellites)
            self._fix = fix
        elif report == "SKY":
            used = parse_sky(payload)
            if used is not None:
                self._satellites = used
