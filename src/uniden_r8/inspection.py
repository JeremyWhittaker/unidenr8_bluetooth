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
**It does not decode POI records.**  Upstream published a candidate layout --
payloads of 13, 12 and 10 bytes after a type byte and one unknown byte, giving
whole records of 15, 14 and 12, with big-endian floats at offsets 2 and 6 --
and also recorded that the only POI database it ever read was empty.  A parser built on
that would be a parser that can appear to succeed on bytes nobody has ever
seen, and its output would be coordinates: home, work, the roads Jeremy drives.
A wrong address printed confidently is worse than no address.  So this reports
lengths, byte histograms and record-boundary *candidates*, and leaves the
decoding to a person looking at the hex with the detector in front of them.

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
]

#: Ceiling on the whole connect-read-disconnect cycle.  The radio is shared
#: with the vehicle's link, so this is bounded for the same reason everything
#: else here is.
CONNECT_TIMEOUT_SECONDS: Final[float] = 25.0
SESSION_CEILING_SECONDS: Final[float] = 90.0

#: Record lengths upstream proposed for the three POI types, used only to
#: *suggest* where records might begin.  Candidates, printed as candidates.
CANDIDATE_RECORD_LENGTHS: Final[dict[int, tuple[str, int]]] = {
    1: ("speed camera", 15),
    2: ("red-light camera", 14),
    3: ("user mark", 12),
}


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


def record_boundaries(payload: bytes) -> list[dict[str, Any]]:
    """Suggest where POI records might begin.  Suggestions, not a parse.

    Walks the blob treating byte 0 of each record as a type marker and using
    upstream's candidate lengths, and stops the moment it meets a byte that is
    not a known type.  If the walk consumes the whole blob exactly, that is
    weak evidence the layout is right; if it does not, that is evidence it is
    wrong.  Either answer is worth having and neither is a coordinate.
    """
    suggestions: list[dict[str, Any]] = []
    offset = 0
    while offset < len(payload):
        marker = payload[offset]
        candidate = CANDIDATE_RECORD_LENGTHS.get(marker)
        if candidate is None:
            suggestions.append({
                "offset": offset,
                "type_byte": f"0x{marker:02x}",
                "kind": "unrecognised",
                "note": "walk stopped: not a candidate record type",
            })
            break
        kind, length = candidate
        suggestions.append({
            "offset": offset,
            "type_byte": f"0x{marker:02x}",
            "kind": kind,
            "candidate_length": length,
            "fits": offset + length <= len(payload),
        })
        offset += length
    return suggestions


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
        walk = record_boundaries(payload)
        recognised = [entry for entry in walk if entry.get("kind") != "unrecognised"]
        lines.append(
            f"{len(recognised)} candidate record boundar"
            f"{'y' if len(recognised) == 1 else 'ies'} on upstream's layout"
            + ("" if len(recognised) == len(walk) else ", walk did not complete")
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
