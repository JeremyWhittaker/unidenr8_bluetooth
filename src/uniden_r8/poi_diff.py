"""Compare two POI captures without publishing what is in them.

This is the instrument for the one experiment that can answer "can this
detector give us a coordinate?" without sending it a single command.

The experiment
--------------
Park with a clear view of the sky and wait for the detector to report a GPS
fix.  Read the POI characteristic (``uniden-r8 inspect --confirm``).  Press the
detector's physical **MARK** button once -- a short press; press-and-hold is
*delete all* -- which the manual says stores a user mark at the current
location.  Read again.  Diff the two.

If a record appeared, the detector materialised a coordinate from its own fix,
because the operator supplied none.  That settles a question that reading the
live telemetry packet cannot: the telemetry carries a compass point, a speed and
an altitude, and no position at all.

What this module will and will not say
--------------------------------------
It reports **structure**: how many bytes changed, where, how many records
appeared, which candidate layout accounts for the blob exactly, and whether the
bytes that would be a coordinate under that layout decode to a *plausible* one.

It does not report the coordinate.  Not to the terminal, not to a file outside
the private store, not in a diagnostic.  The POI database holds saved camera
locations and user marks -- home, work, the roads somebody drives -- and this
whole project's answer to that has been that such values never reach printed or
published output.  A tool built to investigate them is exactly the wrong place
to make an exception.

So the strongest statement available here is a *distance*: given a reference fix
the operator supplies privately, how far is the decoded point from it.  Twenty
metres says yes.  Four thousand kilometres says the layout is wrong.  Neither
number is a location.

Nothing here talks to a detector.  It reads two captures that already exist.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Any, Final

from .inspection import CANDIDATE_LAYOUTS, evaluate_layouts

__all__ = [
    "RecordCandidate",
    "PoiDiff",
    "diff_payloads",
    "decode_point",
    "haversine_metres",
    "PLAUSIBLE_RADIUS_M",
]

#: How close a decoded point must be to the operator's reference fix before this
#: reports it as agreeing.  Consumer GNSS error and the moment between the two
#: readings both live inside this; the float32 encoding itself is good to well
#: under a metre at usual latitudes, so a miss by more than this is a wrong
#: layout rather than a bad fix.
PLAUSIBLE_RADIUS_M: Final[float] = 50.0

#: Earth's mean radius, for the only distance calculation here.
_EARTH_RADIUS_M: Final[float] = 6_371_000.0


def haversine_metres(
    lat_a: float, lon_a: float, lat_b: float, lon_b: float
) -> float:
    """Great-circle distance in metres.

    Adequate for "is this the same place": at these distances the difference
    between a spherical and an ellipsoidal model is far smaller than the GNSS
    error it is being compared against.
    """
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = math.radians(lat_b - lat_a)
    d_lambda = math.radians(lon_b - lon_a)
    h = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def decode_point(record: bytes) -> tuple[float, float] | None:
    """Read a record's two big-endian float32s as a latitude and longitude.

    Returns ``None`` unless both are finite and inside the legal ranges.  That
    rejection is the point: a wrong record boundary produces floats made of
    somebody else's bytes, and those are overwhelmingly likely to be enormous,
    tiny, NaN, or outside +/-90 -- so "it decoded at all" is itself evidence
    about the layout.

    **The caller must not print the return value.**  It exists so that a
    distance can be computed against a reference the operator already knows.
    """
    if len(record) < 10:
        return None
    try:
        latitude, longitude = struct.unpack(">ff", record[2:10])
    except struct.error:
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    if latitude == 0.0 and longitude == 0.0:
        # Null Island is what an empty or zeroed record decodes to, and it is
        # never where anybody parked.
        return None
    return latitude, longitude


@dataclass
class RecordCandidate:
    """One record that appeared between two captures, under one layout."""

    layout: str
    offset: int
    type_byte: int
    kind: str
    length: int
    #: Whether the two float32s at offsets 2..10 decode to a legal coordinate.
    #: The value itself is deliberately absent from this object.
    decodes: bool = False
    #: Distance in metres from the operator's reference fix, when one was
    #: supplied.  ``None`` when it was not, or when the record did not decode.
    metres_from_reference: float | None = None

    def summary(self) -> dict[str, Any]:
        """The publishable shape.  Carries no coordinate and no device byte."""
        return {
            "layout": self.layout,
            "offset": self.offset,
            "type_byte": f"0x{self.type_byte:02x}",
            "kind": self.kind,
            "length": self.length,
            "decodes_to_a_legal_coordinate": self.decodes,
            "metres_from_reference": (
                None if self.metres_from_reference is None
                else round(self.metres_from_reference, 1)
            ),
            "agrees_with_reference": (
                None if self.metres_from_reference is None
                else self.metres_from_reference <= PLAUSIBLE_RADIUS_M
            ),
        }


@dataclass
class PoiDiff:
    """What changed between two reads of the POI characteristic."""

    before_length: int = 0
    after_length: int = 0
    #: Offset of the first differing byte, or ``None`` when the blobs match.
    first_difference: int | None = None
    #: True when the later blob starts with the whole of the earlier one -- the
    #: shape a single appended record makes, and the easiest result to trust.
    appended_only: bool = False
    added_bytes: int = 0
    layouts: list[dict[str, Any]] = field(default_factory=list)
    records_added: list[RecordCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.first_difference is not None or self.before_length != self.after_length

    def summary(self) -> dict[str, Any]:
        """Safe to print.  Structure, counts and distances -- never content."""
        return {
            "before_length": self.before_length,
            "after_length": self.after_length,
            "changed": self.changed,
            "first_difference": self.first_difference,
            "appended_only": self.appended_only,
            "added_bytes": self.added_bytes,
            "layouts": list(self.layouts),
            "records_added": [r.summary() for r in self.records_added],
            "notes": list(self.notes),
        }

    def render(self) -> str:
        """The operator's view, as lines."""
        lines = [
            f"POI capture: {self.before_length} bytes -> {self.after_length} bytes",
        ]
        if not self.changed:
            lines.append("  no change -- the mark was not stored, or not stored here")
            return "\n".join(lines)

        lines.append(
            f"  {self.added_bytes:+d} bytes"
            + (", appended to the end" if self.appended_only
               else f", first difference at offset {self.first_difference}")
        )
        for verdict in self.layouts:
            lines.append(
                f"  {verdict['layout']} ({verdict['record_lengths']}): "
                f"{verdict['records']} record"
                f"{'' if verdict['records'] == 1 else 's'}, "
                + ("consumes the blob exactly" if verdict["exact"]
                   else f"{verdict['bytes_consumed']}/{verdict['bytes_total']} bytes")
            )
        for record in self.records_added:
            agrees = record.summary()["agrees_with_reference"]
            verdict = (
                "no reference supplied" if agrees is None
                else f"{record.metres_from_reference:.0f} m from the reference fix"
                     + (" -- agrees" if agrees else " -- DOES NOT agree")
            )
            lines.append(
                f"  new {record.kind} at offset {record.offset} "
                f"({record.length} bytes, {record.layout}): "
                + ("decodes to a legal coordinate, " if record.decodes
                   else "does NOT decode to a legal coordinate, ")
                + verdict
            )
        lines.extend(f"  note: {note}" for note in self.notes)
        lines.append("  no coordinate is printed here, by design")
        return "\n".join(lines)


def diff_payloads(
    before: bytes,
    after: bytes,
    reference: tuple[float, float] | None = None,
) -> PoiDiff:
    """Compare two POI blobs and adjudicate the record layout.

    *reference* is the operator's own trusted fix, used only to compute a
    distance.  It is never stored, echoed, or written anywhere by this function.
    """
    result = PoiDiff(before_length=len(before), after_length=len(after))
    result.added_bytes = len(after) - len(before)

    limit = min(len(before), len(after))
    for index in range(limit):
        if before[index] != after[index]:
            result.first_difference = index
            break
    if result.first_difference is None and len(before) != len(after):
        result.first_difference = limit
    result.appended_only = (
        len(after) > len(before) and after[: len(before)] == before
    )

    result.layouts = evaluate_layouts(after)

    if not result.changed:
        return result

    if not result.appended_only:
        result.notes.append(
            "the change is not a clean append; a record may have been inserted, "
            "or the database may be reordered on write"
        )

    # Only walk the appended region, and only under a layout that accounts for
    # the whole blob.  Reading a record out of a layout that does not fit is how
    # a tool invents a coordinate, which is the one outcome worth more than a
    # null result.
    exact = [verdict for verdict in result.layouts if verdict["exact"]]
    if not exact:
        result.notes.append(
            "no candidate layout consumes the blob exactly; record boundaries "
            "are unknown, so no record was decoded"
        )
        return result
    if len(exact) > 1:
        result.notes.append(
            "more than one layout consumes the blob exactly; the capture does "
            "not discriminate between them"
        )

    for verdict in exact:
        table = CANDIDATE_LAYOUTS[verdict["layout"]]
        offset = 0
        while offset < len(after):
            candidate = table.get(after[offset])
            if candidate is None:
                break
            kind, length = candidate
            if offset + length > len(after):
                break
            if offset >= len(before) or result.first_difference is not None and (
                offset >= result.first_difference
            ):
                record = after[offset : offset + length]
                point = decode_point(record)
                entry = RecordCandidate(
                    layout=verdict["layout"],
                    offset=offset,
                    type_byte=after[offset],
                    kind=kind,
                    length=length,
                    decodes=point is not None,
                )
                if point is not None and reference is not None:
                    entry.metres_from_reference = haversine_metres(
                        point[0], point[1], reference[0], reference[1]
                    )
                result.records_added.append(entry)
            offset += length

    if not result.records_added:
        result.notes.append(
            "the blob changed but no new record was found at the change point"
        )
    return result
