"""The wire protocol, and the bounded receive-only session that pulls it.

Two things live here, deliberately together so the parser and the only code
that feeds it cannot drift apart:

1. **The decoders.** :func:`parse_telemetry` and :func:`parse_alerts` turn the
   detector's two UTF-8 payload formats into structured readings, and never
   raise.
2. **The bounded session.** :func:`receive` connects to the already-bonded
   detector, GATT-reads telemetry and alerts once, subscribes to both, collects
   for a clamped window, and tears the link down.

What the session does not do: write an application characteristic, read POI,
read settings, or run in the background.  The continuous path is
:mod:`uniden_r8.collector`; the read-only POI/settings inspection is the
separate, explicitly confirmed :mod:`uniden_r8.inspection`.

Two views of every reading
--------------------------
Every parsed record renders two ways, and the difference is the whole privacy
design:

``publishable()``
    The conservative subset, unchanged since schema 1.  Voltage, GPS-fix
    boolean, a POI-warning boolean, and the alert fields a detector exists to
    report.  Nothing here describes *where* the vehicle is.

``detailed()``
    Everything the packet actually carried, including the detector's own
    heading, speed and altitude, the POI warning's type and distance, and every
    alert field including the ones whose meaning is still unknown.  Unknown
    values are carried through raw rather than dropped or guessed at, because a
    field recorded verbatim can be decoded later and a field discarded cannot.

Heading, speed and altitude are position-adjacent: a log of them is a rough
trace of a drive.  They are therefore available but not published by default:
the detailed view is what the owner-only state document and the local history
use, and the conservative view is what a display or a broker sees unless the
operator opts in.  ``docs/SAFETY.md`` §3 states where each one may go.

Evidence, and what is still a guess
-----------------------------------
The seven-field telemetry shape and the four-field GPS sub-group are
**observed on Jeremy's R8** (``docs/EVIDENCE.md`` §7): 31 of 31 packets, then
293 of 293 in a five-minute trial.  Everything about an *active* alert -- band,
strength, the raw signal, the frequency-versus-gun-identifier split, direction,
the mute codes above 2, and field 8 entirely -- comes from an R8w and a
decompiled app, and this R8 has only ever produced an all-clear packet.

:data:`FIELD_CONFIDENCE` records that per field, and it is published alongside
the detailed view rather than kept in a comment, so a consumer can tell a
measurement from a hypothesis.  A packet that does not fit a recognised shape
is reported as unparsed with its raw bytes retained privately -- never forced
into a partial reading.
"""

from __future__ import annotations

import asyncio
import math
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
    "TELEMETRY_FIELDS",
    "ALERT_FIELDS",
    "FIELD_CONFIDENCE",
    "LASER_GUNS",
    "MUTE_STATES",
    "BANDS",
    "DIRECTIONS",
    "IncompatibleDevice",
    "DetectorGps",
    "PoiWarning",
    "Telemetry",
    "Alert",
    "AlertSnapshot",
    "LiveSession",
    "parse_telemetry",
    "parse_alerts",
    "parse_alert_snapshot",
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

#: The seven top-level telemetry fields, in wire order.  Named so that a raw
#: packet can be discussed without counting ampersands.
#:
#: Indices 3 to 6 are numbered rather than named, deliberately.  Upstream's
#: names for them are recorded in :data:`UPSTREAM_FIELD_NAMES`, and index 5 is
#: why: upstream calls it "wifi status", and Uniden's own product page says the
#: R8 has no Wi-Fi.  Whatever sits there on a non-W unit, this project does not
#: name it after a capability the manufacturer says the product lacks.
TELEMETRY_FIELDS: Final[tuple[str, ...]] = (
    "voltage",
    "poi",
    "gps",
    "field_3",
    "field_4",
    "field_5",
    "field_6",
)

#: The nine comma-separated fields of one active alert slot, in wire order.
ALERT_FIELDS: Final[tuple[str, ...]] = (
    "active",
    "alert_id",
    "band",
    "strength",
    "raw_signal",
    "frequency_or_gun_id",
    "direction",
    "mute",
    "receive_mode",
)

#: How well each published field is evidenced, keyed by the name it is
#: published under.  ``observed`` means seen on Jeremy's own R8; ``upstream``
#: means captured on an R8w by ``AegisX86/UnidenR8wlink``; ``candidate`` means
#: decompiled from the R/Tach app and never confirmed against any hardware.
#: See ``docs/EVIDENCE.md`` for the underlying records.
#: Keys are the **published** paths, exactly as they appear in the schema-2
#: document, so a consumer can join a grade to a field by name.  An earlier
#: version graded field names that nothing emitted, which made the map
#: decorative.
FIELD_CONFIDENCE: Final[dict[str, str]] = {
    # Telemetry, under `detector` in the schema-2 document.
    "voltage_v": "observed",
    "field_count": "observed",
    "shape": "observed",
    "detector_gps.present": "observed",
    "detector_gps.status_raw": "observed",
    "detector_gps.locked": "candidate",
    "detector_gps.direction_8": "upstream",
    "detector_gps.speed_raw": "upstream",
    "detector_gps.altitude_raw": "upstream",
    "poi_warning.active": "observed",
    "poi_warning.raw": "observed",
    "poi_warning.decoded": "candidate",
    "unknown.field_3_raw": "upstream",
    "unknown.field_4_raw": "upstream",
    "unknown.field_5_raw": "upstream",
    "unknown.field_6_raw": "upstream",
    # Alerts, under `alerts[]`.  The clear state is the only one observed here.
    "alerts[].band": "upstream",
    "alerts[].strength_1_to_8": "upstream",
    "alerts[].raw_signal": "upstream",
    "alerts[].frequency_ghz": "upstream",
    "alerts[].laser_gun_id": "candidate",
    "alerts[].laser_gun": "candidate",
    "alerts[].field_5_raw": "upstream",
    "alerts[].direction": "upstream",
    "alerts[].mute_code": "upstream",
    "alerts[].mute_state": "candidate",
    "alerts[].alert_id_raw": "upstream",
    "alerts[].receive_mode_raw": "candidate",
    "alerts_empty": "observed",
}

#: Alert direction codes the detector uses.
DIRECTIONS: Final[dict[str, str]] = {"F": "front", "S": "side", "R": "rear"}
_DIRECTIONS = DIRECTIONS  # retained name, used at several call sites

#: Values documented by upstream. Unknown strings stay in the private packet
#: capture rather than being reflected into public output.
BANDS: Final[frozenset[str]] = frozenset(
    {"X", "K", "KA", "LASER", "MRCD", "MRCT", "RT3", "RT4", "K POP", "KA POP"}
)
_BANDS = BANDS

#: Bands whose field 5 is a radar frequency in GHz, and which are therefore
#: not believed unless it is present and numeric.
_FREQUENCY_BANDS: Final[frozenset[str]] = frozenset(
    {"X", "K", "KA", "K POP", "KA POP"}
)

#: Bands whose field 5 is something else entirely.  Laser carries a gun-type
#: identifier there; the photo-radar types carry no frequency at all.  Reading
#: either as GHz would invent a number, which is worse than reporting none.
_GUN_ID_BANDS: Final[frozenset[str]] = frozenset({"LASER"})
_NO_FREQUENCY_BANDS: Final[frozenset[str]] = frozenset(
    {"LASER", "RT3", "RT4", "MRCD", "MRCT"}
)

#: Laser gun types, indexed by the identifier in field 5.
#:
#: Every one of these is **candidate** evidence: the list was decompiled from
#: the R/Tach app and no laser packet has been captured on any hardware, on
#: either model.  It is decoded because a named gun is more useful than a bare
#: integer, and the integer is always published alongside it so a wrong name
#: cannot hide the real value.
LASER_GUNS: Final[tuple[str, ...]] = (
    "laser",
    "LTI 20/20",
    "Stalker",
    "RIEGL",
    "Laser Ally",
    "Kustom",
    "Atlanta",
    "Laveg",
    "SL700",
    "SCS-102",
    "TraffiPat",
    "Truspeed S",
    "Stealth",
    "TruCam",
    "XLR",
    "DragonEye Compact",
    "DragonEye Full-Size",
    "PoliScan",
    "Traffistar s350",
    "Vitronic Poliscan",
)

#: Mute codes.  Only 1 and 2 are confirmed on hardware upstream -- a physical
#: mute-button press was captured moving 1 to 2 -- and the rest come from the
#: decompiled app and may be wrong.
MUTE_STATES: Final[dict[str, str]] = {
    "1": "not muted",
    "2": "muted",
    "3": "mute memory",
    "4": "auto mute memory",
    "5": "blocked mute",
    "6": "quiet ride mute",
}
_MUTE = MUTE_STATES

_MUTED_CODES: Final[frozenset[str]] = frozenset({"2", "3", "4", "5", "6"})

#: The eight compass points the detector reports.  It has no finer heading and
#: no bearing in degrees; a consumer wanting a real course must use an external
#: GNSS receiver, which is what :mod:`uniden_r8.gnss` is for.
COMPASS_POINTS: Final[frozenset[str]] = frozenset(
    {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}
)

#: POI warning types, as the app appears to name them.  Candidate evidence: the
#: only POI field ever observed on either detector is the literal ``0``, which
#: exercises none of this.
POI_KINDS: Final[frozenset[str]] = frozenset(
    {"SPEEDCAM", "REDLIGHT", "USERMARK", "SPEEDTRAP", "AIRPATROL"}
)


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
    """Parse a decimal, rejecting the ones that are not numbers.

    ``float()`` accepts ``"nan"``, ``"inf"`` and ``"infinity"``, and Python's
    ``json`` module then writes them out as the bare tokens ``NaN`` and
    ``Infinity`` -- which are not JSON.  One such value anywhere in a packet
    would make *every* document this project publishes unparseable to a
    conforming reader, from the state file to the broker, for as long as the
    detector kept sending it.  A field that cannot be a number is unknown.
    """
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _safe_word(value: str | None, *, limit: int = 16) -> str | None:
    """Return *value* only if it is short and alphanumeric.

    Anything the detector sends could end up in a document a person reads, and
    an unbounded string from a device is an injection surface however unlikely
    that seems here.  Unknown values are retained in the private raw capture,
    which is where an unexpected string belongs.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > limit:
        return None
    if not candidate.replace(" ", "").replace(".", "").replace("-", "").isalnum():
        return None
    return candidate


def _safe_group(value: str | None, *, limit: int = 48, max_parts: int = 8) -> str | None:
    """Sanitise a comma-separated group without destroying its structure.

    :func:`_safe_word` is the right check for one field and the wrong one for a
    group.  A comma is not alphanumeric, so a whole group fails the check and
    the content is thrown away -- which is how an active POI warning reading
    ``SPEEDCAM,500,35`` became ``active=True, raw=None`` and left nothing behind
    but a boolean.  The `collect` path keeps no raw packets, so that loss was
    permanent, and the first real camera warning would have been unreadable.

    This applies exactly the same rules per sub-field and rejoins them, so the
    injection properties are unchanged: one bounded overall length, a bounded
    part count, every part still alphanumeric after spaces, dots and hyphens are
    stripped, and no separator other than the one the wire format already uses.
    One bad sub-field still refuses the whole group rather than emitting a
    partial one -- a half-sanitised value is worse than none, because it looks
    trustworthy.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > limit:
        return None
    pieces = candidate.split(",")
    if len(pieces) > max_parts:
        return None
    cleaned: list[str] = []
    for piece in pieces:
        stripped = piece.strip()
        if not stripped:
            # An empty sub-field is a shape fact, not a violation: the wire
            # format uses position, so dropping the group would lose the
            # positions of everything after it.
            cleaned.append("")
            continue
        safe = _safe_word(stripped, limit=limit)
        if safe is None:
            return None
        cleaned.append(safe)
    return ",".join(cleaned)


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------

#: Shape grades for a telemetry packet.  Seven fields is the only shape with
#: hardware evidence -- 324 of 324 packets on this R8 -- so it gets its own
#: grade, and anything else is decoded but labelled.
SHAPE_CONFIRMED: Final[str] = "confirmed-7"
SHAPE_UNREADABLE: Final[str] = "unreadable"


@dataclass
class DetectorGps:
    """The GPS sub-group of a telemetry packet: field 2, four comma fields.

    **No latitude or longitude appears here, and none appears anywhere else in
    the live stream.**  The detector plainly knows where it is -- its
    red-light-camera warnings depend on it -- but what it puts on the wire is a
    heading to the nearest of eight compass points, a speed, an altitude and a
    status letter.  A project that wants coordinates gets them from somewhere
    else; :mod:`uniden_r8.gnss` reads them from ``gpsd``, and the published
    schema keeps the two sources in separate branches so nobody can later
    mistake a Pi's fix for something the detector said.

    That "no coordinates" claim deserves one caveat, and :attr:`suspect_pair`
    is it.  What this R8 has actually demonstrated is a four-part group whose
    first three parts nobody has ever looked at -- and four parts is exactly
    the right width to be latitude, longitude, altitude and status.  The
    project's position rests on upstream's *naming* of those fields, not on a
    measurement.  So the parser checks: if sub-fields 0 and 1 both read as
    signed decimals inside coordinate range with a fractional part, the group
    is flagged, kept private, and published as nothing at all.  If that flag
    ever fires, the honest answer is that this documentation was wrong.
    """

    #: One of the eight compass points, or ``None`` if the packet had no fix.
    direction_8: str | None = None
    #: Upstream reads this as miles per hour.  The unit has no source in this
    #: project and has never been checked against a moving vehicle here.
    speed_raw: int | None = None
    #: Upstream reads this as feet.  Same caveat.
    altitude_raw: int | None = None
    #: The status letter exactly as sent.  ``C`` has been observed on this R8
    #: while a fix was present; that it *means* "connected", and what any other
    #: letter means, is not established.
    status_raw: str | None = None
    #: True when the sub-group carried a fix at all.  A bare ``0`` in field 2
    #: is the detector saying it has nothing.
    present: bool = False
    #: True when the packet was well-formed enough for the flags above to mean
    #: anything.  Three states are needed, not two: "the detector reports no
    #: fix" and "we could not read the packet" are different answers, and
    #: collapsing them lets a decode failure masquerade as a measurement.
    evaluated: bool = False
    #: The coordinate tripwire.  See the class docstring.
    suspect_pair: bool = False

    # Upstream's names for the two unit-bearing fields, kept as aliases so a
    # reader who knows the R8w writeup finds what they expect.
    @property
    def speed_mph(self) -> int | None:
        return self.speed_raw

    @property
    def altitude_ft(self) -> int | None:
        return self.altitude_raw

    @property
    def locked(self) -> bool | None:
        """Whether the detector reports a fix, or ``None`` if unknowable."""
        if not self.evaluated:
            return None
        if not self.present:
            return False
        if self.status_raw == "C":
            return True
        return None

    def detailed(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "present": self.present,
            "locked": self.locked,
            "status_raw": self.status_raw,
            "direction_8": self.direction_8,
            "speed_raw": self.speed_raw,
            "speed_unit": "unknown (upstream reads it as mph)",
            "altitude_raw": self.altitude_raw,
            "altitude_unit": "unknown (upstream reads it as feet)",
            "suspect_coordinate_pair": self.suspect_pair,
        }


@dataclass
class PoiWarning:
    """The POI sub-group of a telemetry packet: field 1.

    Only the inactive form -- a literal ``0`` -- has ever been observed, on
    either detector.  The three-part active form is upstream's reading of the
    app, so this decodes no further than "something is being warned about" plus
    the raw text: a structure nobody has seen populated does not get a parser
    that can appear to succeed.
    """

    active: bool = False
    raw: str | None = None
    #: The coordinate tripwire fired on this group: two adjacent sub-fields both
    #: parsed as signed decimal degrees.  ``raw`` is withheld when this is true.
    suspect_pair: bool = False

    def detailed(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "raw": self.raw,
            "suspect_pair": self.suspect_pair,
            "decoded": None,  # see the class docstring: no evidence, no parser
        }


#: What upstream calls the four telemetry fields this project keeps raw.
#: Recorded as documentation, not used as a name.
#:
#: Field 5 is the reason for this indirection.  Upstream calls it "wifi status",
#: and ``docs/EVIDENCE.md`` §1.2 records Uniden's own product page saying the R8
#: has no Wi-Fi -- the R8w is the Wi-Fi model.  Whatever byte sits at index 5 on
#: a non-W unit, publishing it under the name "wifi" would assert a capability
#: the manufacturer says the product does not have.
UPSTREAM_FIELD_NAMES: Final[dict[str, str]] = {
    "field_3_raw": "warning",
    "field_4_raw": "scanCount",
    "field_5_raw": "wifi status (R8w; this model has no Wi-Fi)",
    "field_6_raw": "brightness status",
}


@dataclass
class Telemetry:
    """One telemetry packet, decoded as far as the evidence allows."""

    voltage: float | None = None
    gps: DetectorGps = field(default_factory=DetectorGps)
    poi: PoiWarning = field(default_factory=PoiWarning)
    #: Fields 3 to 6, kept verbatim under neutral names.  See
    #: :data:`UPSTREAM_FIELD_NAMES` for what upstream calls them and why this
    #: project does not.
    field_3_raw: str | None = None
    field_4_raw: str | None = None
    field_5_raw: str | None = None
    field_6_raw: str | None = None
    field_count: int = 0
    #: ``confirmed-7``, ``extended-N``, ``short-N`` or ``unreadable``.
    shape: str = SHAPE_UNREADABLE
    #: True only for the shape this R8 has actually produced.  A packet with a
    #: different field count may still have decoded -- see :attr:`shape` -- but
    #: it is not the shape any evidence covers, and the schema-1 document
    #: refuses it for that reason.
    parsed: bool = False

    # ------------------------------------------------ backwards-compatible

    @property
    def gps_locked(self) -> bool | None:
        """Schema-1 name for :attr:`DetectorGps.locked`."""
        return self.gps.locked

    @property
    def poi_warning(self) -> bool:
        """Schema-1 name for :attr:`PoiWarning.active`."""
        return self.poi.active

    @property
    def scan_raw(self) -> str | None:
        """Upstream's name for field 4, kept for the history writer."""
        return self.field_4_raw

    @property
    def warning_raw(self) -> str | None:
        """Upstream's name for field 3, kept for the history writer."""
        return self.field_3_raw

    @property
    def scan_field(self) -> int | None:
        """Schema-1 name for the numeric reading of field 4."""
        return _int(self.field_4_raw)

    @property
    def shape_confirmed(self) -> bool:
        return self.shape == SHAPE_CONFIRMED

    # -------------------------------------------------------------- output

    def publishable(self) -> dict[str, Any]:
        """The conservative view: what the detector is reporting, not where.

        Unchanged since schema 1, and deliberately so -- the e-paper display
        and anything else built against that shape must keep working.  Heading,
        speed, altitude and POI detail are absent here by design; they are in
        :meth:`detailed`.
        """
        return {
            "voltage": self.voltage,
            "gps_locked": self.gps_locked,
            "poi_warning": self.poi_warning,
            "parsed": self.parsed,
        }

    def detailed(self) -> dict[str, Any]:
        """Everything the packet carried, including position-adjacent fields.

        Goes to the owner-only state document and the local history.  It does
        *not* go to a display, a broker, or a printed report by default; see
        ``docs/SAFETY.md`` §3.
        """
        return {
            "voltage_v": self.voltage,
            "detector_gps": self.gps.detailed(),
            "poi_warning": self.poi.detailed(),
            "unknown": {
                "field_3_raw": self.field_3_raw,
                "field_4_raw": self.field_4_raw,
                "field_5_raw": self.field_5_raw,
                "field_6_raw": self.field_6_raw,
                "upstream_names": dict(UPSTREAM_FIELD_NAMES),
            },
            "field_count": self.field_count,
            "shape": self.shape,
            "parsed": self.parsed,
        }


def _looks_like_coordinate(value: str | None) -> bool:
    """True if *value* could be a decimal-degrees coordinate.

    Deliberately loose.  This is a tripwire, not a decoder: its job is to fire
    if upstream's field naming turns out to be wrong, and a tripwire that only
    catches the obvious cases is not worth having.
    """
    if not value or "." not in value:
        return False
    try:
        number = float(value)
    except ValueError:
        return False
    return -180.0 <= number <= 180.0 and number != int(number)


def _parse_gps_group(raw: str | None) -> DetectorGps:
    """Decode field 2.  A malformed group yields "unknown", not a guess."""
    if not raw or raw == "0":
        return DetectorGps(evaluated=True, present=False)
    parts = raw.split(",")
    if len(parts) != 4:
        # The four-field shape is observed on this R8; anything else is a
        # firmware change worth noticing rather than partially believing, so
        # it comes back unevaluated -- "we could not tell", not "no fix".
        return DetectorGps(evaluated=False, present=False)

    first, second = _at(parts, 0), _at(parts, 1)
    if _looks_like_coordinate(first) and _looks_like_coordinate(second):
        # The tripwire fired.  Publish nothing from this group: if these are
        # coordinates, they are the most sensitive bytes the detector sends,
        # and the raw packet is already in the private capture where a person
        # can look at it deliberately.
        return DetectorGps(evaluated=False, present=True, suspect_pair=True)

    direction = _safe_word(first, limit=2)
    return DetectorGps(
        evaluated=True,
        present=True,
        direction_8=direction if direction in COMPASS_POINTS else None,
        speed_raw=_int(second),
        altitude_raw=_int(_at(parts, 2)),
        status_raw=_safe_word(_at(parts, 3), limit=4),
    )


def _parse_poi_group(raw: str | None) -> PoiWarning:
    """Decode field 1.  Only the inactive form has ever been observed.

    The group is retained as sanitised text, not parsed.  Upstream reads it as
    type, distance and speed limit; nobody has seen it populated on any
    detector, so a structure nobody has seen does not get a parser that can
    appear to succeed.  What it does get is the same coordinate tripwire the
    GPS group has, for a stronger reason: POI is the characteristic that holds
    saved camera locations and user marks, and if a warning ever carries the
    position of the thing being warned about, that is the most sensitive text
    the detector sends.  If the tripwire fires, ``raw`` is dropped and the
    boolean survives -- and the documentation is what needs correcting.
    """
    if not raw or raw == "0":
        return PoiWarning(active=False)
    parts = [piece.strip() for piece in raw.split(",")]
    if any(
        _looks_like_coordinate(left) and _looks_like_coordinate(right)
        for left, right in zip(parts, parts[1:], strict=False)
    ):
        return PoiWarning(active=True, raw=None, suspect_pair=True)
    return PoiWarning(active=True, raw=_safe_group(raw, limit=48))


def parse_telemetry(payload: bytes | bytearray | str) -> Telemetry:
    """Parse a telemetry packet, never raising.

    Seven fields is the shape this R8 produces, 324 times out of 324, and it is
    the only shape :attr:`Telemetry.parsed` is true for.  A packet with *more*
    fields is still decoded positionally and marked ``extended-N``: a firmware
    update that appends a field should not blank the voltage reading on a
    display, and the shape grade is how a consumer decides whether to trust it.
    A packet with *fewer* is decoded no further than its field count, because
    there is nothing to line the values up against.

    A malformed packet costs one reading, never the session: this runs against
    a detector in a moving vehicle.
    """
    text = _text(payload)
    fields = text.split("&")
    count = len(fields)
    expected = len(TELEMETRY_FIELDS)

    if count < expected:
        return Telemetry(
            field_count=count,
            shape=SHAPE_UNREADABLE if count <= 1 else f"short-{count}",
        )

    reading = Telemetry(
        field_count=count,
        shape=SHAPE_CONFIRMED if count == expected else f"extended-{count}",
    )
    reading.voltage = _float(_at(fields, 0))
    reading.poi = _parse_poi_group(_at(fields, 1))
    reading.gps = _parse_gps_group(_at(fields, 2))
    reading.field_3_raw = _safe_word(_at(fields, 3))
    reading.field_4_raw = _safe_word(_at(fields, 4))
    reading.field_5_raw = _safe_word(_at(fields, 5), limit=4)
    reading.field_6_raw = _safe_word(_at(fields, 6), limit=4)
    reading.parsed = reading.voltage is not None and reading.shape == SHAPE_CONFIRMED
    return reading


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

#: A slot's three possible states.  Three, not two, because "the detector
#: reports nothing in this slot" and "this slot arrived and we could not read
#: it" must produce different behaviour downstream: treating the second as the
#: first makes a single bad byte look like a threat that ended, which
#: fabricates a complete alert lifecycle in permanent history.
SLOT_ACTIVE: Final[str] = "active"
SLOT_EMPTY: Final[str] = "empty"
SLOT_UNREADABLE: Final[str] = "unreadable"


@dataclass
class Alert:
    """One active detection, with every field the slot carried.

    Nine fields are described upstream; every documented example, including
    both of this project's fixtures, carries eight.  So eight is what is
    required and the ninth is optional -- requiring nine would reject every
    known-good packet, including the ones the requirement came from.

    Two decoding rules matter more than the rest.  **Field 5 is a tagged
    union**: a frequency for the radar bands, a gun identifier for laser, and
    neither for the photo-radar types, where reading it as GHz would invent a
    number.  ``3`` parses as ``3.0`` perfectly well, so the split is by band
    rather than by whether the text happens to look numeric.  **An unreadable
    field never discards the detection**: an unlisted mute code or an absent
    frequency leaves that one value unknown and publishes the alert anyway,
    because losing a real Ka warning over field 7 is the worst thing this
    parser could do.
    """

    band: str = ""
    strength: int | None = None
    signal: int | None = None
    frequency_ghz: float | None = None
    direction: str | None = None
    mute_code: str | None = None
    parsed: bool = False
    #: Field 1.  Always ``00`` in every capture on either model, which is why
    #: :mod:`uniden_r8.events` correlates on signal characteristics instead.
    alert_id_raw: str | None = None
    #: Field 5 when the band is laser: an index into :data:`LASER_GUNS`.
    laser_gun_id: int | None = None
    #: Field 5 exactly as sent, whatever it turned out to mean.
    field_5_raw: str | None = None
    #: Field 8.  Preserved verbatim; no reading of it is established, so it is
    #: never given an interpreted label.
    receive_mode_raw: str | None = None
    #: Position in the snapshot.  Not an identity -- the detector re-orders
    #: slots as signals rise and fall -- but useful for diagnosing a capture.
    slot: int = 0
    #: How many comma-separated fields the slot actually had.
    field_count: int = 0
    #: Whether :attr:`band` is one of the strings upstream documented.  A band
    #: this project has never seen is still published -- with this false -- but
    #: field 5 is left uninterpreted, because the frequency-versus-gun-id split
    #: is keyed on the band and an unknown band decides neither.
    band_recognised: bool = True
    #: Field 6 exactly as sent, kept whether or not it matched a known code.
    #: The direction vocabulary comes from an R8w; if this detector uses a
    #: different one, this is the field that will show it.
    direction_raw: str | None = None

    @property
    def direction_name(self) -> str:
        return DIRECTIONS.get(self.direction or "", self.direction or "unknown")

    @property
    def muted(self) -> bool | None:
        if self.mute_code not in MUTE_STATES:
            return None
        return self.mute_code in _MUTED_CODES

    @property
    def mute_status(self) -> str:
        return MUTE_STATES.get(self.mute_code or "", "unknown")

    @property
    def laser_gun(self) -> str | None:
        """The gun-type name, if field 5 held a recognised identifier.

        Looked up by bounds check rather than by bare indexing: an unexpected
        identifier off the wire would raise inside a notification callback,
        where the exception disappears and takes the detection with it.
        """
        if self.laser_gun_id is None or not 0 <= self.laser_gun_id < len(LASER_GUNS):
            return None
        return LASER_GUNS[self.laser_gun_id]

    @property
    def band_name(self) -> str:
        """The band for public output: an allowlisted name, or ``"unknown"``.

        The two rules that meet here used to be in conflict, and the conflict
        was resolved the wrong way.  An unfamiliar band must not discard the
        detection -- publishing "clear" during a real threat is the worst thing
        this parser can do -- but an arbitrary string from a device must not be
        reflected into output that a person or another program reads.

        Both hold if the *detection* is published and the *string* is not.  So
        the public view says `unknown`, and the sanitised text survives in
        :meth:`detailed`, which goes only to the owner-only schema-2 document,
        the local history and `live --full`.  Nothing is lost and nothing
        unfamiliar is echoed.
        """
        return self.band if self.band_recognised else "unknown"

    def publishable(self) -> dict[str, Any]:
        """The conservative view.  Unchanged since schema 1."""
        return {
            "band": self.band_name,
            "strength": self.strength,
            "frequency_ghz": self.frequency_ghz,
            "direction": self.direction_name,
            "muted": self.muted,
        }

    def detailed(self) -> dict[str, Any]:
        """Every field, decoded where possible and raw where not."""
        return {
            "slot": self.slot,
            "band": self.band,
            "strength_1_to_8": self.strength,
            "raw_signal": self.signal,
            "frequency_ghz": self.frequency_ghz,
            "laser_gun_id": self.laser_gun_id,
            "laser_gun": self.laser_gun,
            "field_5_raw": self.field_5_raw,
            "band_recognised": self.band_recognised,
            "direction": self.direction_name,
            "direction_code": self.direction,
            "direction_raw": self.direction_raw,
            "mute_code": self.mute_code,
            "mute_state": self.mute_status,
            "muted": self.muted,
            "alert_id_raw": self.alert_id_raw,
            "receive_mode_raw": self.receive_mode_raw,
            "field_count": self.field_count,
            "parsed": self.parsed,
        }

    def __str__(self) -> str:
        frequency = f"{self.frequency_ghz:g} GHz" if self.frequency_ghz else "-"
        if self.laser_gun:
            frequency = self.laser_gun
        bars = "#" * (self.strength or 0)
        muted = "  [muted]" if self.muted is True else ""
        return (
            f"{self.band_name:<6} {frequency:<10} {bars:<8} "
            f"{self.direction_name}{muted}"
        )


@dataclass(frozen=True)
class Slot:
    """One position in the snapshot, and what was in it."""

    index: int
    state: str
    alert: Alert | None = None
    #: The slot's own text, sanitised, kept only when it could not be decoded.
    #:
    #: A rejected slot used to leave nothing but a counter.  That is the worst
    #: possible outcome for a detector that has never produced an active alert:
    #: the one packet that would have told us why the parser is wrong is the one
    #: packet the parser throws away.  A readable slot does not need this --
    #: every field of it is already published -- so this is only ever set for
    #: the failures, which is exactly where the information would otherwise go.
    raw: str | None = None

    def detailed(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "state": self.state,
            "raw": self.raw,
            "alert": self.alert.detailed() if self.alert else None,
        }


@dataclass
class AlertSnapshot:
    """One complete alert notification: every slot, readable or not.

    The characteristic sends a full snapshot on every change rather than a
    delta, so an empty :attr:`alerts` list with :attr:`recognised` true is a
    positive statement -- "nothing is being detected" -- and not an absence of
    information.  Distinguishing that from "a packet arrived that we could not
    read" is the whole reason :attr:`recognised` and the per-slot states exist:
    the tracker must not end a threat because one slot failed to decode.
    """

    slots: list[Slot] = field(default_factory=list)
    recognised: bool = True
    rejected_slots: int = 0
    unknown_mute_codes: int = 0

    @property
    def alerts(self) -> list[Alert]:
        """Only the slots that decoded, in slot order."""
        return [slot.alert for slot in self.slots if slot.alert is not None]

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    @property
    def uncertain(self) -> bool:
        """True when this notification could not be fully read.

        The tracker uses this to hold open tracks rather than ending them:
        absence and failure are different facts and must not produce the same
        behaviour.

        Derived from :attr:`recognised` rather than from the rejected count,
        because there is a third way to fail that has no rejected slot at all.
        An empty payload, or one that is only NUL bytes -- a truncated
        notification, which is exactly what a marginal link produces -- decodes
        to no slots and no rejections, and under the old form that read as a
        confident "nothing is being detected".  It would have ended every live
        track: one truncated packet mid-encounter, and the alert disappears
        from the state file, the feed and the history with a fabricated end.
        """
        return not self.recognised

    def __iter__(self):
        return iter(self.alerts)

    def __len__(self) -> int:
        return len(self.alerts)

    def detailed(self) -> dict[str, Any]:
        return {
            "slot_count": self.slot_count,
            "rejected_slots": self.rejected_slots,
            "unknown_mute_codes": self.unknown_mute_codes,
            "recognised": self.recognised,
            "slots": [slot.detailed() for slot in self.slots],
            "alerts": [alert.detailed() for alert in self.alerts],
        }


def _parse_slot(segment: str, slot: int) -> Alert | None:
    """Decode one comma-separated slot, or return ``None`` if it is not sane.

    Strict about *structure* -- the active marker, the field count, and the one
    numeric field with an established range -- and lenient about every
    vocabulary, because every vocabulary here came from a different product.
    An unlisted mute code, an unknown band, an unfamiliar direction code, an
    absent frequency or an unreadable raw signal leaves that one value marked
    unknown; none of them throws away the detection.
    """
    fields = segment.split(",")
    # Both are case- and whitespace-normalised.  The band always was; the
    # direction was not, so a detector that sent `f` instead of `F` had its
    # entire detection thrown away by the gate below.  Every direction code on
    # file comes from an R8w, and this detector has never produced an active
    # alert, so an assumption about its letter case was an assumption that
    # could only be tested by losing the first real one.
    band = (_at(fields, 2) or "").strip().upper()
    strength = _int(_at(fields, 3))
    signal = _int(_at(fields, 4))
    direction_raw = (_at(fields, 6) or "").strip().upper()

    # The hard gate is *structural* plus the one numeric field with an
    # established range.  A slot that starts with the active marker, carries at
    # least eight fields and reports a strength of 1-8 is a detection, and the
    # right response to an unfamiliar value elsewhere in it is to publish the
    # detection with that value marked unknown.
    #
    # This used to also require the band to be a known string, the direction to
    # be a known code, and the raw signal to parse as an integer.  Every one of
    # those vocabularies came from a *different product*, and the raw signal's
    # scale is documented here as unknown.  So on a detector that has never
    # produced an active alert, any small difference from the R8w would have
    # rejected the slot, set `recognised` false, and published "clear" while the
    # detector's own screen showed a Ka threat.  Silence is the one output this
    # parser must never produce from a real detection.
    if (
        len(fields) < 8
        or _at(fields, 0) != "1"
        or strength is None
        or not 1 <= strength <= 8
    ):
        return None

    # An unknown band is still published, but only if it sanitises: an arbitrary
    # device string must not reach a document, and one that fails `_safe_word`
    # is not a band by any reading.
    #
    # The limit is deliberately loose.  The longest band on file is `KA POP`, at
    # six characters, and a tighter bound would rediscover the bug this whole
    # branch exists to fix -- rejecting a real detection because its band name
    # was unfamiliar, this time by being unfamiliar *and long*.  The injection
    # protection here is the alphanumeric check, not the length; the length is
    # only there so the string is bounded at all.
    band_recognised = band in BANDS
    if not band_recognised:
        safe_band = _safe_word(band, limit=16)
        if safe_band is None:
            return None
        band = safe_band

    direction = direction_raw if direction_raw in DIRECTIONS else None

    info = _at(fields, 5)
    frequency: float | None = None
    gun_id: int | None = None
    if not band_recognised:
        # Field 5 is a tagged union keyed on the band.  An unknown band selects
        # no branch, so neither reading is applied and only the raw survives.
        pass
    elif band in _GUN_ID_BANDS:
        gun_id = _int(info)
    elif band not in _NO_FREQUENCY_BANDS:
        frequency = _float(info)
    # The photo-radar and RT types fall through with neither: field 5 has no
    # established meaning for them, and `_float` would happily turn a code into
    # a plausible-looking frequency.

    return Alert(
        band=band,
        strength=strength,
        signal=signal,
        frequency_ghz=frequency,
        laser_gun_id=gun_id,
        field_5_raw=_safe_word(info, limit=12),
        direction=direction,
        direction_raw=_safe_word(direction_raw, limit=4),
        band_recognised=band_recognised,
        # Validated as a short token even though its *value* is no longer
        # required to be a known code.  An unlisted mute code must not throw
        # away a real Ka detection -- but it must not put an arbitrary device
        # string into a document either, and this is the one field where those
        # two rules meet.
        mute_code=_safe_word(_at(fields, 7), limit=4),
        alert_id_raw=_safe_word(_at(fields, 1), limit=8),
        receive_mode_raw=_safe_word(_at(fields, 8), limit=8),
        slot=slot,
        field_count=len(fields),
        parsed=True,
    )


def _describe_unreadable(segment: str) -> str | None:
    """Say what an unreadable slot contained, without reflecting it.

    The sanitised text when it sanitises, and a *shape* when it does not.  A
    slot that fails :func:`_safe_group` is exactly the one worth knowing about
    -- it holds a character no field of this protocol should carry -- and
    returning ``None`` there would throw away the only clue at the only moment
    it matters.  So the fallback names the field count and the offending
    character classes and prints nothing the device sent.

    The bytes themselves are not lost either way: `collect` stores every alert
    payload verbatim in the owner-only history, and `live` writes them to the
    private capture.  This is what may be *read on a screen*.
    """
    safe = _safe_group(segment, limit=80, max_parts=12)
    if safe is not None:
        return safe
    parts = segment.split(",")
    classes = []
    if any(character.isspace() for character in segment):
        classes.append("whitespace")
    if any(not character.isprintable() for character in segment):
        classes.append("control")
    if any(character in "<>&\"'{}\\" for character in segment):
        classes.append("markup")
    if len(segment) > 80:
        classes.append("over-length")
    detail = ", ".join(classes) if classes else "unexpected characters"
    return f"<{len(parts)} fields, not printable: {detail}>"


def parse_alert_snapshot(payload: bytes | bytearray | str) -> AlertSnapshot:
    """Parse a full alert snapshot without reflecting unknown strings."""
    text = _text(payload)
    if not text:
        return AlertSnapshot(recognised=False)

    snapshot = AlertSnapshot()
    for index, raw_segment in enumerate(text.split("&")):
        segment = raw_segment.strip()
        if segment == "0":
            snapshot.slots.append(Slot(index, SLOT_EMPTY))
            continue
        alert = _parse_slot(segment, index)
        if alert is None:
            snapshot.slots.append(
                Slot(index, SLOT_UNREADABLE, raw=_describe_unreadable(segment))
            )
            snapshot.recognised = False
            snapshot.rejected_slots += 1
            continue
        if alert.mute_code not in MUTE_STATES:
            snapshot.unknown_mute_codes += 1
        snapshot.slots.append(Slot(index, SLOT_ACTIVE, alert))
    return snapshot


def _parse_alert_snapshot(payload: bytes | bytearray | str) -> tuple[list[Alert], bool]:
    """Legacy tuple form, kept because two call sites still read it that way."""
    snapshot = parse_alert_snapshot(payload)
    return snapshot.alerts, snapshot.recognised


def parse_alerts(payload: bytes | bytearray | str) -> list[Alert]:
    """Parse a full alert snapshot and return only the decoded slots."""
    return parse_alert_snapshot(payload).alerts


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


# --------------------------------------------------------------------------
# The bounded session
# --------------------------------------------------------------------------


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
    #: Every decoded alert seen during the window, not only the latest snapshot.
    #: A one-shot diagnostic that reported only the final state would have the
    #: same blind spot the collector was rewritten to remove.
    seen_alerts: list[Alert] = field(default_factory=list)
    #: When true, :meth:`as_dict` renders the detailed view.  Off by default so
    #: the diagnostic command's output stays free of position-adjacent fields
    #: unless the operator asks for them.
    detailed: bool = False

    def as_dict(self) -> dict[str, Any]:
        document = {
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
        if self.detailed:
            document["detail"] = {
                "telemetry": self.latest.detailed() if self.latest else None,
                "alerts": [a.detailed() for a in self.alerts],
                "seen_alerts": [a.detailed() for a in self.seen_alerts],
                "confidence": dict(FIELD_CONFIDENCE),
            }
        return document

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
            if self.detailed:
                lines += self._render_detail(self.latest)

        lines += ["", f"alerts: {len(self.alerts)}"]
        lines += [f"  {a}" for a in self.alerts] or ["  clear"]
        if self.detailed and self.seen_alerts:
            lines += ["", f"alerts seen during the window: {len(self.seen_alerts)}"]
            lines += [f"  {a}" for a in self.seen_alerts]
        if self.errors:
            lines += ["", "notes:"] + [f"  {e}" for e in self.errors]
        return "\n".join(lines)

    @staticmethod
    def _render_detail(latest: Telemetry) -> list[str]:
        """The position-adjacent lines, printed only on an explicit request."""
        gps = latest.gps
        heading = gps.direction_8 or "--"
        speed = "--" if gps.speed_mph is None else f"{gps.speed_mph} mph"
        altitude = "--" if gps.altitude_ft is None else f"{gps.altitude_ft} ft"
        lines = [
            f"  heading {heading:<4} speed {speed:<8} altitude {altitude}"
            f"   (detector GPS, status {gps.status_raw or '--'})"
        ]
        if latest.poi.active:
            # Printed verbatim and undecoded.  The three-part active form is
            # upstream's reading of the app and no populated POI field has been
            # seen on either detector, so this shows what arrived rather than
            # what it is guessed to mean.
            lines.append(f"  POI warning active, field 1 reads {latest.poi.raw!r}")
        return lines


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


def _default_client(address: str, adapter: str | None = None):
    from bleak import BleakClient  # noqa: PLC0415 - deliberate lazy import

    if adapter:
        return BleakClient(address, timeout=CONNECT_TIMEOUT_SECONDS, adapter=adapter)
    return BleakClient(address, timeout=CONNECT_TIMEOUT_SECONDS)


async def _session(  # noqa: PLR0913, PLR0917 - each parameter is an injection seam
    address: str,
    salt: bytes,
    seconds: float,
    store: Any,
    client_factory: Any = None,
    detailed: bool = False,
) -> LiveSession:
    build = client_factory or _default_client
    session = LiveSession(started_at=utc_stamp(), seconds=seconds, detailed=detailed)
    raw: list[dict[str, Any]] = []
    seen: dict[tuple, Alert] = {}

    def record(kind: str, payload: bytes) -> None:
        if len(raw) < MAX_RETAINED:
            raw.append({
                "at": round(time.monotonic(), 3),
                "kind": kind,
                "hex": bytes(payload).hex(),
            })

    def remember(alerts: list[Alert]) -> None:
        # Keep one representative per distinct threat so a short alert that
        # cleared before the window closed is still in the report.
        for alert in alerts:
            key = (alert.band, alert.direction, alert.frequency_ghz, alert.laser_gun_id)
            if key not in seen:
                seen[key] = alert
                session.seen_alerts.append(alert)

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
                    snapshot = parse_alert_snapshot(payload)
                    session.alerts = snapshot.alerts
                    remember(snapshot.alerts)
                    if not snapshot.recognised:
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
                    snapshot = parse_alert_snapshot(data)
                    session.alerts = snapshot.alerts
                    remember(snapshot.alerts)
                    session.alert_packets += 1
                    if not snapshot.recognised:
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


async def receive(  # noqa: PLR0913, PLR0917 - injection seams, again
    address: str,
    salt: bytes,
    store: Any,
    seconds: float | None = None,
    client_factory: Any = None,
    detailed: bool = False,
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
            _session(address, salt, window, store, client_factory, detailed),
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
