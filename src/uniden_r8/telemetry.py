"""Bounded, receive-only live data from the detector.

What this does: connects to the already-bonded detector, GATT-reads the
telemetry and alert characteristics once, subscribes to both, collects
notifications for a bounded window, and tears the link down.

What it does not do: write an application characteristic, read POI, read
settings, or run in the background.

The three boundaries
--------------------
**A compatibility gate, not an assumption.** The vendor UUIDs in
:mod:`uniden_r8.gatt` were documented on an **R8w**.  Service discovery has
since confirmed the same UUIDs on Jeremy's **R8**, but this module still checks
the connected device before reading anything: a firmware update could change
the table, and a gate that is only correct today is not a gate.  If the
required attributes are absent, it stops and says so.

**Compatibility is evidence-graded.** The bounded hardware capture confirmed
the seven-field telemetry shape on this R8; active-alert fields remain R8w
evidence. Every parser still treats a malformed packet as routine — unknown
fields become ``None``, nothing raises, and raw bytes are kept privately so a
wrong guess can be corrected later rather than silently discarded.
:attr:`Reading.parsed` says whether the shape was recognised at all, which
makes "we could not read it" a reportable result instead of a blank.

**POI and settings are never read.** :func:`uniden_r8.gatt.assert_live_readable`
refuses them, and this module calls it on every read.  POI holds saved camera
and user-mark coordinates — home, work, the roads Jeremy drives — and none of
it is needed to pull live data.

Subscribing writes a Client Characteristic Configuration Descriptor.  That is
a **protocol descriptor write**, stated plainly here and in ``docs/SAFETY.md``.
It is how a GATT client says "send me updates"; it carries no application
command, and it is the only write of any kind this module performs.

What gets published
-------------------
Raw payloads go to the owner-only private store.  Published output carries
battery voltage, whether GPS has a fix, and the alert fields a radar detector
exists to report — band, strength, direction, frequency, mute state.

It deliberately omits heading, speed, altitude and the details of any POI
warning.  Those describe where Jeremy is and where he has been, and none of
them is needed to answer "what is the detector seeing right now".  They remain
in the raw capture, private.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Final

from .evidence import utc_stamp
from .gatt import (
    ALERT_UUID,
    REQUIRED_LIVE_ATTRIBUTES,
    TELEMETRY_UUID,
    assert_live_notifiable,
    assert_live_readable,
    describe,
)
from .privacy import scrub

__all__ = [
    "MAX_RECEIVE_SECONDS",
    "DEFAULT_RECEIVE_SECONDS",
    "IncompatibleDevice",
    "Telemetry",
    "Alert",
    "LiveSession",
    "parse_telemetry",
    "parse_alerts",
    "check_compatibility",
    "receive",
]

MIN_RECEIVE_SECONDS: Final[float] = 5.0
DEFAULT_RECEIVE_SECONDS: Final[float] = 30.0
MAX_RECEIVE_SECONDS: Final[float] = 120.0

#: Head-room after the connect timeout for reads, subscription and teardown.
RECEIVE_GRACE_SECONDS: Final[float] = 10.0

#: Cap on retained notifications.  Telemetry arrives every 1-2 s and alerts can
#: arrive several times a second on a live signal, so an unbounded list would
#: grow without limit on a node with 415 MiB of RAM.
MAX_RETAINED: Final[int] = 2000

#: Alert direction codes the detector uses.
_DIRECTIONS: Final[dict[str, str]] = {"F": "front", "S": "side", "R": "rear"}

# Values documented by upstream. Unknown strings stay in the private packet
# capture rather than being reflected into public output.
_BANDS: Final[frozenset[str]] = frozenset(
    {"X", "K", "KA", "LASER", "MRCD", "MRCT", "RT3", "RT4", "K POP", "KA POP"}
)

#: Mute codes.  Only 1 and 2 are confirmed on hardware upstream; the rest come
#: from the decompiled app and may be wrong.
_MUTE: Final[dict[str, str]] = {
    "1": "not muted",
    "2": "muted",
    "3": "mute memory",
    "4": "auto mute memory",
    "5": "blocked mute",
    "6": "quiet ride mute",
}

_MUTED_CODES: Final[frozenset[str]] = frozenset({"2", "3", "4", "5", "6"})


class IncompatibleDevice(RuntimeError):
    """The connected device does not expose the attributes this phase needs."""


def _text(payload: bytes | bytearray | str) -> str:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8", "replace").strip("\x00").strip()
    return payload.strip()


def _at(parts: list[str], index: int) -> str | None:
    if 0 <= index < len(parts):
        value = parts[index].strip()
        return value or None
    return None


def _float(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class Telemetry:
    """One telemetry packet, split into publishable and private halves."""

    voltage: float | None = None
    gps_locked: bool | None = None
    poi_warning: bool = False
    scan_field: int | None = None
    field_count: int = 0
    parsed: bool = False

    def publishable(self) -> dict[str, Any]:
        """Only what answers 'what is the detector reporting right now'.

        Heading, speed, altitude and POI details are deliberately absent: they
        describe where Jeremy is, and they stay in the private raw capture.
        """
        return {
            "voltage": self.voltage,
            "gps_locked": self.gps_locked,
            "poi_warning": self.poi_warning,
            "parsed": self.parsed,
        }


@dataclass
class Alert:
    """One active detection."""

    band: str = ""
    strength: int | None = None
    signal: int | None = None
    frequency_ghz: float | None = None
    direction: str | None = None
    mute_code: str | None = None
    parsed: bool = False

    @property
    def direction_name(self) -> str:
        return _DIRECTIONS.get(self.direction or "", self.direction or "unknown")

    @property
    def muted(self) -> bool | None:
        if self.mute_code not in _MUTE:
            return None
        return self.mute_code in _MUTED_CODES

    @property
    def mute_status(self) -> str:
        return _MUTE.get(self.mute_code or "", "unknown")

    def publishable(self) -> dict[str, Any]:
        return {
            "band": self.band,
            "strength": self.strength,
            "frequency_ghz": self.frequency_ghz,
            "direction": self.direction_name,
            "muted": self.muted,
        }

    def __str__(self) -> str:
        frequency = f"{self.frequency_ghz:g} GHz" if self.frequency_ghz else "-"
        bars = "#" * (self.strength or 0)
        muted = "  [muted]" if self.muted is True else ""
        return (
            f"{self.band:<6} {frequency:<10} {bars:<8} {self.direction_name}{muted}"
        )


def parse_telemetry(payload: bytes | bytearray | str) -> Telemetry:
    """Parse a telemetry packet, never raising.

    The field order originated upstream and the seven-field shape was then
    observed on this R8. A packet that does not fit comes back with
    ``parsed=False`` and empty fields rather than an exception: this runs
    against a detector in a moving vehicle, and a malformed packet must cost
    one reading, not the session.
    """
    text = _text(payload)
    fields = text.split("&")
    reading = Telemetry(field_count=len(fields))

    # The real R8 capture established exactly seven top-level fields. A
    # shorter or longer packet is retained privately, not partially promoted
    # into public values.
    if len(fields) != 7:
        return reading

    reading.voltage = _float(_at(fields, 0))

    poi = _at(fields, 1)
    reading.poi_warning = bool(poi and poi != "0")

    gps = _at(fields, 2)
    if gps and gps != "0":
        parts = gps.split(",")
        if len(parts) != 4:
            return reading
        # Field 3 of the GPS group is the fix status; "C" means connected.
        # Heading, speed and altitude are parsed nowhere: they are not needed
        # and not published, so they are not extracted at all.
        status = _at(parts, 3)
        reading.gps_locked = True if status == "C" else None
    else:
        reading.gps_locked = False

    reading.scan_field = _int(_at(fields, 4))
    reading.parsed = reading.voltage is not None
    return reading


def _parse_alert_snapshot(payload: bytes | bytearray | str) -> tuple[list[Alert], bool]:
    """Return parsed alerts and whether the complete snapshot was recognised."""
    text = _text(payload)
    if not text:
        return [], False

    alerts: list[Alert] = []
    recognised = True
    for raw_segment in text.split("&"):
        segment = raw_segment.strip()
        if segment == "0":
            continue

        fields = segment.split(",")
        band = (_at(fields, 2) or "").upper()
        strength = _int(_at(fields, 3))
        signal = _int(_at(fields, 4))
        direction = _at(fields, 6)
        mute_code = _at(fields, 7)
        valid = (
            len(fields) >= 8
            and _at(fields, 0) == "1"
            and band in _BANDS
            and strength is not None
            and 1 <= strength <= 8
            and signal is not None
            and direction in _DIRECTIONS
            and mute_code in _MUTE
        )

        frequency = None
        if band not in {"LASER", "RT3", "RT4"}:
            frequency = _float(_at(fields, 5))
            valid = valid and frequency is not None
        if not valid:
            recognised = False
            continue

        alerts.append(
            Alert(
                band=band,
                strength=strength,
                signal=signal,
                frequency_ghz=frequency,
                direction=direction,
                mute_code=mute_code,
                parsed=True,
            )
        )
    return alerts, recognised


def parse_alerts(payload: bytes | bytearray | str) -> list[Alert]:
    """Parse a full alert snapshot without reflecting unknown strings."""
    alerts, _ = _parse_alert_snapshot(payload)
    return alerts


def bounded_receive_seconds(seconds: float | None) -> float:
    """Clamp the collection window."""
    if seconds is None:
        return DEFAULT_RECEIVE_SECONDS
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return DEFAULT_RECEIVE_SECONDS
    if value != value:  # noqa: PLR0124 - NaN check without importing math
        return DEFAULT_RECEIVE_SECONDS
    return max(MIN_RECEIVE_SECONDS, min(MAX_RECEIVE_SECONDS, value))


@dataclass
class LiveSession:
    """The sanitized result of one bounded receive window."""

    started_at: str = ""
    seconds: float = 0.0
    connected: bool = False
    compatible: bool = False
    telemetry_packets: int = 0
    alert_packets: int = 0
    latest: Telemetry | None = None
    alerts: list[Alert] = field(default_factory=list)
    unparsed_telemetry: int = 0
    unparsed_alert_packets: int = 0
    services_missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "seconds": self.seconds,
            "connected": self.connected,
            "compatible": self.compatible,
            "telemetry_packets": self.telemetry_packets,
            "alert_packets": self.alert_packets,
            "unparsed_telemetry": self.unparsed_telemetry,
            "unparsed_alert_packets": self.unparsed_alert_packets,
            "latest": self.latest.publishable() if self.latest else None,
            "alerts": [a.publishable() for a in self.alerts],
            "services_missing": list(self.services_missing),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        lines = [f"live receive {self.started_at}, window {self.seconds:g}s", ""]
        if not self.connected:
            lines.append("Did not connect.")
            return "\n".join(lines + [f"  {e}" for e in self.errors])
        if not self.compatible:
            lines.append("Device does not expose the required attributes:")
            lines += [f"  missing {m}" for m in self.services_missing]
            return "\n".join(lines)

        lines.append(
            f"packets: {self.telemetry_packets} telemetry, "
            f"{self.alert_packets} alert"
            + (f"  ({self.unparsed_telemetry} telemetry unparsed)"
               if self.unparsed_telemetry else "")
            + (f"  ({self.unparsed_alert_packets} alert unparsed)"
               if self.unparsed_alert_packets else "")
        )
        lines.append("")
        if self.latest is None:
            lines.append("No telemetry arrived in the window.")
        else:
            voltage = (f"{self.latest.voltage:.1f} V" if self.latest.voltage
                       else "voltage unknown")
            gps = ("GPS locked" if self.latest.gps_locked
                   else "no GPS fix" if self.latest.gps_locked is False
                   else "GPS unknown")
            lines.append(f"  {voltage}     {gps}")
            if self.latest.poi_warning:
                lines.append("  POI warning active")

        lines += ["", f"alerts: {len(self.alerts)}"]
        lines += [f"  {a}" for a in self.alerts] or ["  clear"]
        if self.errors:
            lines += ["", "notes:"] + [f"  {e}" for e in self.errors]
        return "\n".join(lines)


def check_compatibility(client: Any) -> list[str]:
    """Return the required attributes the connected device is missing.

    Checked against the live device rather than assumed from the catalogue: the
    UUIDs came from a different model, and a firmware update could move them.
    """
    data_service = None
    for service in getattr(client, "services", []) or []:
        if str(service.uuid).lower() == REQUIRED_LIVE_ATTRIBUTES[0]:
            data_service = service
            break
    if data_service is None:
        return list(REQUIRED_LIVE_ATTRIBUTES)

    present = {
        str(characteristic.uuid).lower()
        for characteristic in getattr(data_service, "characteristics", []) or []
    }
    return [
        uuid for uuid in REQUIRED_LIVE_ATTRIBUTES[1:] if uuid.lower() not in present
    ]


#: Connect timeout for the receive session.  Matches the identity probe's, and
#: is defined here rather than imported so this module has no dependency on it.
CONNECT_TIMEOUT_SECONDS: Final[float] = 25.0


def _default_client(address: str):
    from bleak import BleakClient  # noqa: PLC0415 - deliberate lazy import

    return BleakClient(address, timeout=CONNECT_TIMEOUT_SECONDS)


async def _session(
    address: str,
    salt: bytes,
    seconds: float,
    store: Any,
    client_factory: Any = None,
) -> LiveSession:
    build = client_factory or _default_client
    session = LiveSession(started_at=utc_stamp(), seconds=seconds)
    raw: list[dict[str, Any]] = []

    def record(kind: str, payload: bytes) -> None:
        if len(raw) < MAX_RETAINED:
            raw.append({
                "at": round(time.monotonic(), 3),
                "kind": kind,
                "hex": bytes(payload).hex(),
            })

    try:
        # `async with` guarantees the link is released even if a read raises. A
        # held link matters: the detector stops advertising while connected.
        async with build(address) as client:
            session.connected = True

            missing = check_compatibility(client)
            if missing:
                session.services_missing = [f"{m} ({_name(m)})" for m in missing]
                return session
            session.compatible = True

            # One GATT Read of each, for an immediate snapshot. The gate is
            # called per-UUID at the call site, so POI and settings cannot be
            # reached even if this list were edited.
            for uuid in (TELEMETRY_UUID, ALERT_UUID):
                permitted = assert_live_readable(uuid)
                try:
                    payload = bytes(await client.read_gatt_char(permitted))
                except Exception as exc:  # noqa: BLE001 - absence is evidence
                    session.errors.append(
                        scrub(
                            f"read {describe(permitted).name}: {type(exc).__name__}",
                            salt,
                        )
                    )
                    continue
                kind = "telemetry" if uuid == TELEMETRY_UUID else "alert"
                record(f"read:{kind}", payload)
                if uuid == TELEMETRY_UUID:
                    session.latest = parse_telemetry(payload)
                    if not session.latest.parsed:
                        session.unparsed_telemetry += 1
                else:
                    session.alerts, recognised = _parse_alert_snapshot(payload)
                    if not recognised:
                        session.unparsed_alert_packets += 1

            def on_telemetry(_sender: Any, data: bytearray) -> None:
                # Runs on bleak's notification path and must never propagate.
                try:
                    record("notify:telemetry", data)
                    reading = parse_telemetry(data)
                    session.telemetry_packets += 1
                    if not reading.parsed:
                        session.unparsed_telemetry += 1
                    session.latest = reading
                except Exception:  # noqa: BLE001, S110
                    pass

            def on_alert(_sender: Any, data: bytearray) -> None:
                try:
                    record("notify:alert", data)
                    session.alerts, recognised = _parse_alert_snapshot(data)
                    session.alert_packets += 1
                    if not recognised:
                        session.unparsed_alert_packets += 1
                except Exception:  # noqa: BLE001, S110
                    pass

            # Subscribing writes a CCCD. It is the only write this module
            # performs and carries no application command.
            subscribed: list[str] = []
            try:
                handlers = ((TELEMETRY_UUID, on_telemetry), (ALERT_UUID, on_alert))
                for uuid, handler in handlers:
                    permitted = assert_live_notifiable(uuid)
                    try:
                        await client.start_notify(permitted, handler)
                        subscribed.append(permitted)
                    except Exception as exc:  # noqa: BLE001
                        session.errors.append(
                            scrub(
                                f"subscribe {describe(permitted).name}: "
                                f"{type(exc).__name__}",
                                salt,
                            )
                        )

                if subscribed:
                    await asyncio.sleep(seconds)
            finally:
                # Explicitly undo every established subscription, including on
                # cancellation or a partial subscription failure. The context
                # manager then disconnects as a second teardown layer.
                for permitted in reversed(subscribed):
                    try:
                        await client.stop_notify(permitted)
                    except Exception as exc:  # noqa: BLE001
                        session.errors.append(
                            scrub(
                                f"unsubscribe {describe(permitted).name}: "
                                f"{type(exc).__name__}",
                                salt,
                            )
                        )
    finally:
        # Preserve packets even when the outer ceiling cancels the session.
        if raw:
            try:
                store.write_json(
                    _raw_capture_name(session.started_at),
                    {"captured_at": session.started_at, "packets": raw},
                )
            except Exception as exc:  # noqa: BLE001
                session.errors.append(
                    scrub(f"private capture: {type(exc).__name__}", salt)
                )
    return session


def _name(uuid: str) -> str:
    try:
        return describe(uuid).name
    except Exception:  # noqa: BLE001
        return "service"


def _raw_capture_name(started_at: str) -> str:
    stamp = "".join(character for character in started_at if character.isalnum())
    return f"live-raw-{stamp}.json"


async def receive(
    address: str,
    salt: bytes,
    store: Any,
    seconds: float | None = None,
    client_factory: Any = None,
) -> LiveSession:
    """Collect live data for a bounded window, then tear the link down.

    Failures are reported, not raised: "the detector did not answer" and "it is
    not compatible" are both findings the caller needs in the same shape as a
    success.
    """
    if not salt:
        raise ValueError("receive() needs a redaction salt")
    window = bounded_receive_seconds(seconds)
    try:
        return await asyncio.wait_for(
            _session(address, salt, window, store, client_factory),
            timeout=window + CONNECT_TIMEOUT_SECONDS + RECEIVE_GRACE_SECONDS,
        )
    except TimeoutError:
        return LiveSession(
            started_at=utc_stamp(),
            seconds=window,
            errors=[
                "session exceeded "
                f"{window + CONNECT_TIMEOUT_SECONDS + RECEIVE_GRACE_SECONDS:g}s "
                "and was cancelled"
            ],
        )
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return LiveSession(
            started_at=utc_stamp(),
            seconds=window,
            errors=[scrub(f"{type(exc).__name__}: {exc}", salt)],
        )
