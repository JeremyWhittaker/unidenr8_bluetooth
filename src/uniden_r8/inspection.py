"""One deliberate, read-only look at the attributes the collector never touches.

The live path reads telemetry and alerts, and refuses POI and settings even
though the wider probe allowlist would admit them.  That refusal is right for a
process that runs unattended for hours.  It is not a reason never to look: the
settings blocks and the POI database are the last undecoded parts of this
interface, and nothing can be decoded that is never read.

So this is the other shape of the same care.  A person runs it once, with
``--confirm``, while the vehicle is parked.  It connects, reads three
characteristics and their descriptors, writes everything into the owner-only
private store, and prints a summary that contains no device bytes at all.

What it refuses to do
---------------------
**It does not decode POI records.**  Upstream published the numbers 13, 12 and
10 for the three record types, and recorded that the only POI database it ever
read was empty.  Those numbers have two readings, and this project has held
both: that they are *payload* sizes following a type byte and one unknown byte,
making whole records of 15, 14 and 12; or that they are the *whole* record
lengths already, counting the type byte and the unknown byte, with big-endian
floats at offsets 2 and 6.  The second reading is what a field-by-field count
of Uniden's own app produces, and it is self-consistent: 1 + 1 + 4 + 4 + 2 + 1
= 13 for a speed camera, 12 without the speed byte, 10 for a bare user mark.

One of them has now been checked against a populated database on this
detector, and won: see ``docs/EVIDENCE.md`` §13.  This module still evaluates
every candidate against the bytes rather than assuming the winner, because a
tool that can only walk one layout cannot report that the layout is wrong --
and the next firmware, or the R8w, may not match.
That is a measurement; picking one and writing a parser would be a guess whose
output is coordinates: home, work, the roads Jeremy drives.  A wrong address
printed confidently is worse than no address.  So this reports lengths, byte
histograms and per-layout record-boundary *verdicts*, and leaves the decoding to
a person looking at the hex with the detector in front of them.

**It does not decode settings.**  Same reason, weaker consequences.  A
community byte map exists, it is incomplete, and it is keyed to firmware nobody
here is running.  What this produces instead is a *snapshot* that can be
diffed: read, change exactly one menu item on the detector by hand, read again,
and the difference names the byte.  That is how a settings map gets built
honestly, one physical toggle at a time, and ``docs/VALIDATION.md`` sets out
the procedure.

**It does not write anything to the detector.**  Every read goes through
:func:`uniden_r8.gatt.assert_inspect_readable`, the command characteristic
stays on the permanent denylist, and the AST audit that proves no module in
this package can write a characteristic value covers this file like every
other.

Descriptors
-----------
Every vendor characteristic reportedly carries a 0x2901 Characteristic User
Description -- the device's own name for its own attribute.  Reading those is
the cheapest discovery step available and the only one where the answer comes
from the firmware rather than from somebody's guess, so it happens first.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Final

from .evidence import utc_stamp
from .gatt import (
    CHARACTERISTIC_USER_DESCRIPTION_UUID,
    DESCRIPTOR_READ_PLAN,
    INSPECT_READ_PLAN,
    SENSITIVE_UUIDS,
    assert_inspect_readable,
    describe,
    normalize_uuid,
)
from .privacy import scrub
from .telemetry import check_compatibility

__all__ = [
    "InspectionRefused",
    "AttributeDump",
    "Inspection",
    "inspect",
    "summarise_bytes",
    "record_boundaries",
    "evaluate_layouts",
    "CANDIDATE_LAYOUTS",
]

#: Ceiling on the whole connect-read-disconnect cycle.  The radio is shared
#: with the vehicle's link, so this is bounded for the same reason everything
#: else here is.
CONNECT_TIMEOUT_SECONDS: Final[float] = 25.0
SESSION_CEILING_SECONDS: Final[float] = 90.0

#: The competing readings of upstream's POI record lengths, kept side by side
#: because nobody has read a populated POI database on any detector and a swap
#: would destroy the record of the disagreement rather than settle it.
#:
#: **Settled by measurement on 2026-09-03.**  A populated POI database was read
#: on this non-W R8 at firmware 1.43 -- 23 bytes, the first non-empty POI read
#: anybody has reported on any model -- and ``whole-record`` consumed it exactly,
#: 23 of 23 bytes, as a 13-byte speed camera followed by a 10-byte user mark.
#: ``payload-plus-header`` desynchronised at offset 15.  ``docs/EVIDENCE.md`` §13.
#:
#: ``whole-record`` reads upstream's 13/12/10 as the total record length, which
#: is what a field-by-field count of Uniden's own app produces:
#: type(1) + unknown(1) + lat f32(4) + lon f32(4) + angle u16(2) + speed(1).
#: **OBSERVED on this detector.**
#:
#: ``payload-plus-header`` read the same numbers as the payload *after* a type
#: byte and an unknown byte, giving 15/14/12.  It was this project's own
#: reading, and it is now **REFUTED** by that capture.
#:
#: The loser is kept rather than deleted.  It is what makes
#: :func:`evaluate_layouts` a measurement instead of an assertion: a tool that
#: can only walk one layout cannot report that the layout is wrong, and the next
#: firmware or model may not match this one.
CANDIDATE_LAYOUTS: Final[dict[str, dict[int, tuple[str, int]]]] = {
    "whole-record": {
        1: ("speed camera", 13),
        2: ("red-light camera", 12),
        3: ("user mark", 10),
    },
    "payload-plus-header": {
        1: ("speed camera", 15),
        2: ("red-light camera", 14),
        3: ("user mark", 12),
    },
}

#: Retained under its original name so an existing caller keeps working, and now
#: pointing at the layout the hardware confirmed rather than the one it refuted.
CANDIDATE_RECORD_LENGTHS: Final[dict[int, tuple[str, int]]] = CANDIDATE_LAYOUTS[
    "whole-record"
]


class InspectionRefused(RuntimeError):
    """The inspection was not explicitly confirmed, or the device is wrong."""


@dataclass
class AttributeDump:
    """One characteristic, read once, described without being interpreted."""

    uuid: str
    name: str
    length: int = 0
    #: The bytes, hex-encoded.  Written to the private store only; never in the
    #: printed summary, and never in anything published.
    hex: str = ""
    #: The 0x2901 description the device gave for this attribute, if any.
    described_as: str | None = None
    error: str = ""
    sensitive: bool = False

    def summary(self) -> dict[str, Any]:
        """The publishable shape: shape and statistics, never content.

        Deliberately excludes :attr:`hex`.  A byte histogram tells you whether a
        settings block is all ``0xff`` or whether a POI database is empty, which
        is what a summary is for; the bytes themselves tell you where somebody
        lives.
        """
        return {
            "uuid": self.uuid,
            "name": self.name,
            "length": self.length,
            "described_as": self.described_as,
            "error": self.error,
            "sensitive": self.sensitive,
        }


@dataclass
class Inspection:
    """The result of one look."""

    read_at: str = ""
    connected: bool = False
    compatible: bool = False
    attributes: list[AttributeDump] = field(default_factory=list)
    services_missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Where the raw dump was written.  A name, not a path with a home
    #: directory in it.
    capture_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The sanitized result.  Safe to print; carries no device bytes."""
        return {
            "read_at": self.read_at,
            "connected": self.connected,
            "compatible": self.compatible,
            "attributes": [a.summary() for a in self.attributes],
            "services_missing": list(self.services_missing),
            "errors": list(self.errors),
            "capture_name": self.capture_name,
        }

    def private_dict(self) -> dict[str, Any]:
        """The full dump, for the private store and nowhere else."""
        return {
            "read_at": self.read_at,
            "attributes": [
                {**a.summary(), "hex": a.hex} for a in self.attributes
            ],
        }

    def render(self) -> str:
        lines = [f"inspection {self.read_at}", ""]
        if not self.connected:
            lines.append("Did not connect.")
            return "\n".join(lines + [f"  {e}" for e in self.errors])
        if not self.compatible:
            lines.append("Device does not expose the required attributes:")
            return "\n".join(lines + [f"  missing {m}" for m in self.services_missing])

        width = max((len(a.name) for a in self.attributes), default=10)
        for dump in self.attributes:
            if dump.error:
                lines.append(f"  {dump.name.ljust(width)}  -- {dump.error}")
                continue
            described = f'  "{dump.described_as}"' if dump.described_as else ""
            lines.append(
                f"  {dump.name.ljust(width)}  {dump.length:>5} bytes{described}"
            )
            lines += [f"      {line}" for line in _describe_bytes(dump)]

        if self.capture_name:
            lines += [
                "",
                f"raw bytes written to the private store as {self.capture_name}",
                "Nothing above is decoded.  See docs/VALIDATION.md for how to",
                "turn a settings diff or a POI dump into evidence.",
            ]
        if self.errors:
            lines += ["", "notes:"] + [f"  {e}" for e in self.errors]
        return "\n".join(lines)


def summarise_bytes(payload: bytes) -> dict[str, Any]:
    """Describe a blob without revealing it.

    Enough to answer the questions worth asking before decoding anything: is it
    empty, is it uniform, how varied is it, does it look like text.  A block of
    all ``0xff`` and a block of dense varied bytes are different facts, and
    neither needs the bytes printed to be established.
    """
    if not payload:
        return {"length": 0, "empty": True}
    counts = Counter(payload)
    distinct = len(counts)
    most_common, most_common_count = counts.most_common(1)[0]
    printable = sum(1 for byte in payload if 0x20 <= byte < 0x7F)
    return {
        "length": len(payload),
        "empty": False,
        "distinct_bytes": distinct,
        "all_same": distinct == 1,
        "dominant_byte": f"0x{most_common:02x}",
        "dominant_fraction": round(most_common_count / len(payload), 3),
        "zero_fraction": round(counts.get(0, 0) / len(payload), 3),
        "printable_fraction": round(printable / len(payload), 3),
    }


def record_boundaries(
    payload: bytes,
    lengths: dict[int, tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Suggest where POI records might begin.  Suggestions, not a parse.

    Walks the blob treating byte 0 of each record as a type marker and using one
    candidate length table, and stops the moment it meets a byte that is not a
    known type, or a record that would run off the end.  If the walk consumes
    the whole blob exactly, that is weak evidence the layout is right; if it
    does not, that is evidence it is wrong.  Either answer is worth having and
    neither is a coordinate.

    *lengths* defaults to the ``payload-plus-header`` reading only so that an
    existing caller behaves as it did.  Prefer :func:`evaluate_layouts`, which
    runs every candidate and reports which one fits: with a single table this
    can only ever say "the one layout I was given did not work", which is a
    false negative dressed as a result.
    """
    table = CANDIDATE_RECORD_LENGTHS if lengths is None else lengths
    suggestions: list[dict[str, Any]] = []
    offset = 0
    while offset < len(payload):
        marker = payload[offset]
        candidate = table.get(marker)
        if candidate is None:
            suggestions.append({
                "offset": offset,
                "type_byte": f"0x{marker:02x}",
                "kind": "unrecognised",
                "note": "walk stopped: not a candidate record type",
            })
            break
        kind, length = candidate
        fits = offset + length <= len(payload)
        suggestions.append({
            "offset": offset,
            "type_byte": f"0x{marker:02x}",
            "kind": kind,
            "candidate_length": length,
            "fits": fits,
        })
        if not fits:
            # Stop rather than stepping past the end.  Continuing would advance
            # `offset` beyond `len(payload)`, ending the loop silently and
            # rendering an overshoot as a completed walk -- which is the one
            # answer this function must never give.
            break
        offset += length
    return suggestions


def evaluate_layouts(payload: bytes) -> list[dict[str, Any]]:
    """Run every candidate POI layout against *payload* and report the verdicts.

    This is the instrument the layout question is actually decided with.  Each
    layout gets: how many records it walked, whether every one of them fit, how
    many bytes it consumed, and -- the discriminator -- whether it consumed the
    blob **exactly**.  A blob written in one layout desynchronises almost
    immediately under the other, so on a populated database at most one entry
    should come back ``exact``.

    Returns a list ordered so that layouts which consumed the blob exactly come
    first, then by how far each got.  Nothing here decodes a coordinate: the
    result carries counts, offsets and layout names, and no value read out of
    the payload except type bytes.
    """
    verdicts: list[dict[str, Any]] = []
    for name, table in CANDIDATE_LAYOUTS.items():
        walk = record_boundaries(payload, table)
        recognised = [entry for entry in walk if entry.get("kind") != "unrecognised"]
        consumed = sum(
            int(entry["candidate_length"]) for entry in recognised if entry.get("fits")
        )
        # "Complete" must mean the walk consumed the blob, which needs BOTH
        # that it never met an unknown type byte AND that every record fitted.
        # An overshooting record still has a recognised `kind`, so counting
        # entries alone reported a truncated walk as a clean pass -- and the one
        # capture this is meant to adjudicate, a single 10-byte user mark read
        # under a 12-byte layout, is exactly that shape.
        complete = (
            bool(recognised)
            and len(recognised) == len(walk)
            and all(entry.get("fits") for entry in recognised)
        )
        verdicts.append({
            "layout": name,
            "record_lengths": "/".join(
                str(length) for _, length in sorted(table.values(), key=lambda v: -v[1])
            ),
            "records": len(recognised),
            "all_fit": all(entry.get("fits") for entry in recognised),
            "bytes_consumed": consumed,
            "bytes_total": len(payload),
            "complete": complete,
            "exact": complete and consumed == len(payload),
            "stopped_at": None if complete else (
                walk[-1]["offset"] if walk else 0
            ),
        })
    verdicts.sort(key=lambda v: (not v["exact"], -v["bytes_consumed"]))
    return verdicts


def _describe_bytes(dump: AttributeDump) -> list[str]:
    """The per-attribute lines of the printed summary."""
    if not dump.hex:
        return []
    payload = bytes.fromhex(dump.hex)
    stats = summarise_bytes(payload)
    if stats.get("empty"):
        return ["empty"]
    lines = [
        f"{stats['distinct_bytes']} distinct byte values, "
        f"{stats['dominant_fraction']:.0%} {stats['dominant_byte']}"
        + (", all identical" if stats["all_same"] else "")
    ]
    if dump.sensitive and not stats["all_same"]:
        for verdict in evaluate_layouts(payload):
            summary = (
                "consumes the blob exactly" if verdict["exact"]
                else f"{verdict['bytes_consumed']}/{verdict['bytes_total']} bytes, "
                     + ("all records fit" if verdict["complete"]
                        else f"walk stopped at offset {verdict['stopped_at']}")
            )
            lines.append(
                f"{verdict['layout']} ({verdict['record_lengths']}): "
                f"{verdict['records']} record"
                f"{'' if verdict['records'] == 1 else 's'}, {summary}"
            )
        lines.append("contents NOT decoded: they would be saved coordinates")
    return lines


#: Longest a 0x2901 description may be before it is treated as not a name.
MAX_DESCRIPTION_CHARS: Final[int] = 48


def _readable(payload: bytes) -> str | None:
    """Return a short printable description, or ``None``.

    The raw bytes are in the private capture either way; this is only what may
    be printed.
    """
    text = payload.decode("utf-8", "replace").strip("\x00").strip()
    if not text or len(text) > MAX_DESCRIPTION_CHARS:
        return None
    if not all(character.isprintable() for character in text):
        return None
    return text


def _default_client(address: str, adapter: str | None = None):
    from bleak import BleakClient  # noqa: PLC0415 - deliberate lazy import

    if adapter:
        return BleakClient(address, timeout=CONNECT_TIMEOUT_SECONDS,
                           bluez={"adapter": adapter})
    return BleakClient(address, timeout=CONNECT_TIMEOUT_SECONDS)


def _descriptions(client: Any) -> dict[str, str]:
    """Read every 0x2901 the plan names, tolerating every absence.

    Descriptors are addressed by handle, not by UUID, so this walks the
    discovered services to find them.  A device that exposes none is a finding,
    not a failure.
    """
    found: dict[str, str] = {}
    wanted = {normalize_uuid(uuid) for uuid in DESCRIPTOR_READ_PLAN}
    for service in getattr(client, "services", []) or []:
        for characteristic in getattr(service, "characteristics", []) or []:
            if normalize_uuid(str(characteristic.uuid)) not in wanted:
                continue
            for descriptor in getattr(characteristic, "descriptors", []) or []:
                if normalize_uuid(str(descriptor.uuid)) == \
                        CHARACTERISTIC_USER_DESCRIPTION_UUID:
                    found[normalize_uuid(str(characteristic.uuid))] = str(
                        getattr(descriptor, "handle", "")
                    )
    return found


async def _session(
    address: str,
    salt: bytes,
    store: Any,
    client_factory: Any = None,
    adapter: str | None = None,
) -> Inspection:
    build = client_factory or _default_client
    result = Inspection(read_at=utc_stamp())

    async with build(address, adapter) as client:
        result.connected = True

        missing = check_compatibility(client)
        if missing:
            result.services_missing = [f"{uuid} ({_name(uuid)})" for uuid in missing]
            return result
        result.compatible = True

        handles = _descriptions(client)

        for uuid in INSPECT_READ_PLAN:
            # The gate at the call site, per UUID, exactly as the live path
            # does it: editing this loop cannot smuggle in an attribute the
            # allowlist would reject.
            permitted = assert_inspect_readable(uuid)
            entry = describe(permitted)
            dump = AttributeDump(
                uuid=permitted,
                name=entry.name,
                sensitive=permitted in SENSITIVE_UUIDS,
            )
            try:
                payload = bytes(await client.read_gatt_char(permitted))
                dump.length = len(payload)
                dump.hex = payload.hex()
            except Exception as exc:  # noqa: BLE001 - absence is evidence
                dump.error = f"{type(exc).__name__}"

            handle = handles.get(permitted)
            if handle:
                try:
                    described = bytes(
                        await client.read_gatt_descriptor(int(handle))
                    )
                    # Bounded and filtered.  This string comes off the device
                    # and is printed to a terminal; a descriptor is meant to
                    # hold a short human-readable name, so anything that is not
                    # one is a finding for the private capture rather than
                    # something to echo.
                    dump.described_as = _readable(described)
                except Exception:  # noqa: BLE001 - a missing description is fine
                    dump.described_as = None
            result.attributes.append(dump)

    return result


def _name(uuid: str) -> str:
    try:
        return describe(uuid).name
    except Exception:  # noqa: BLE001
        return "attribute"


async def inspect(  # noqa: PLR0913, PLR0917 - injection seams
    address: str,
    salt: bytes,
    store: Any,
    *,
    confirmed: bool = False,
    client_factory: Any = None,
    adapter: str | None = None,
) -> Inspection:
    """Read settings and POI once, into the private store.

    *confirmed* is not a formality.  This is the only command in the project
    that deliberately reads the detector's saved coordinates, and a person has
    to say so: there is no configuration file that turns it on, no environment
    variable, and no way for the collector to reach this code path.
    """
    if not salt:
        raise ValueError("inspect() needs a redaction salt")
    if not confirmed:
        raise InspectionRefused(
            "inspection reads the POI database, which holds saved camera "
            "locations and user marks.  Re-run with --confirm."
        )

    try:
        result = await asyncio.wait_for(
            _session(address, salt, store, client_factory, adapter),
            timeout=SESSION_CEILING_SECONDS,
        )
    except TimeoutError:
        return Inspection(
            read_at=utc_stamp(),
            errors=[f"timed out after {SESSION_CEILING_SECONDS:g}s"],
        )
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return Inspection(
            read_at=utc_stamp(),
            errors=[scrub(f"{type(exc).__name__}: {exc}", salt)],
        )

    if any(dump.hex for dump in result.attributes):
        name = "inspect-" + "".join(
            character for character in result.read_at if character.isalnum()
        ) + ".json"
        try:
            store.write_json(name, result.private_dict())
            result.capture_name = name
        except Exception as exc:  # noqa: BLE001
            result.errors.append(scrub(f"private capture: {type(exc).__name__}", salt))
    return result
