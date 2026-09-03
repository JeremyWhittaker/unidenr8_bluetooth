"""Enumerate what the detector actually exposes, rather than what we expect.

Every other read path in this project works from
:data:`uniden_r8.gatt.CATALOGUE` -- a table of attributes upstream documented on
an **R8w**. That is the right way to *use* the detector and the wrong way to
*learn* about it: an attribute this project has never heard of is exactly the
attribute a catalogue cannot tell you about, and firmware 1.43 on a non-W R8 has
never been enumerated by anybody.

So this walks the device's own GATT tree and reports what is there, with the
catalogue used only to *label* what it finds. An attribute the catalogue does
not know is the interesting result, not an error.

What it will not do
-------------------
**It reads no characteristic values.** Not one -- not even the ones the live
path reads freely. It reports each attribute's UUID, its properties, and its
``0x2901`` user description, which is a short human-readable name the device
publishes about itself. Nothing here can return a coordinate, because nothing
here reads a characteristic that could hold one; that is what makes this command
safe to run without the ``--confirm`` that :mod:`uniden_r8.inspection` requires.

``--listen`` is the one exception and it is still not a read: it subscribes to
the command-response characteristic and waits, sending nothing.

Why listen to a response characteristic with no command
--------------------------------------------------------
Because "pointless without a command" was an assumption, and this project has a
rule about those. The detector may report state on it unprompted -- a boot
banner, a mode change, an acknowledgement of something done on its own keypad.
If it does, that is a data source obtainable without ever writing an application
value. If it stays silent for the whole window, that is a measurement too, and a
more useful one than the assumption it replaces.

Subscribing writes a CCCD descriptor, which is what receiving BLE data means on
any device and cannot carry an application command. The permanent forbidden-UUID
check runs first, and :func:`uniden_r8.gatt.assert_survey_notifiable` permits
that one characteristic and nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Final

from .evidence import utc_stamp
from .gatt import (
    CHARACTERISTIC_USER_DESCRIPTION_UUID,
    COMMAND_RESPONSE_UUID,
    FORBIDDEN_UUIDS,
    SENSITIVE_UUIDS,
    assert_survey_notifiable,
    describe,
    normalize_uuid,
)
from .privacy import scrub

__all__ = [
    "SurveyedAttribute",
    "SurveyedService",
    "ResponseNotification",
    "Survey",
    "survey",
    "MAX_LISTEN_SECONDS",
    "MAX_DESCRIPTION_CHARS",
]

#: Ceiling on the whole connect-enumerate-disconnect cycle.
CONNECT_TIMEOUT_SECONDS: Final[float] = 25.0
SESSION_CEILING_SECONDS: Final[float] = 180.0

#: Bounds on the listen window.  The radio is shared with the vehicle's OBD
#: link, so this is bounded for the same reason everything else here is.
#:
#: Not called a "passive" window, and `test_the_docs_do_not_overclaim_radio_silence`
#: is what stopped it being: subscribing writes a CCCD descriptor and the link
#: exchanges frames throughout. The accurate claim is that no *application*
#: value is written to a vendor characteristic, which is the invariant this
#: project actually holds. "Listening" here means "sending no command", not
#: "not transmitting".
MIN_LISTEN_SECONDS: Final[float] = 5.0
MAX_LISTEN_SECONDS: Final[float] = 120.0

#: Longest a 0x2901 description may be before it is treated as not a name.
MAX_DESCRIPTION_CHARS: Final[int] = 48

#: How many unprompted notifications to retain before counting the rest.  A
#: characteristic that turns out to be chatty must not fill memory or a report.
MAX_RETAINED_NOTIFICATIONS: Final[int] = 40


def _readable_name(payload: bytes) -> str | None:
    """A ``0x2901`` description, if it looks like one.

    The value comes off the device and is printed to a terminal, so it is
    bounded and filtered: a user description is meant to be a short printable
    name, and anything that is not one is a finding for the private capture
    rather than something to echo.
    """
    try:
        text = payload.decode("utf-8").strip("\x00").strip()
    except UnicodeDecodeError:
        return None
    if not text or len(text) > MAX_DESCRIPTION_CHARS:
        return None
    if not all(character.isprintable() for character in text):
        return None
    return text


@dataclass
class SurveyedAttribute:
    """One characteristic as the device describes it."""

    uuid: str
    #: The catalogue's name, or ``None`` when this project has never heard of it.
    known_as: str | None = None
    properties: tuple[str, ...] = ()
    #: The ``0x2901`` user description the device published, if any.
    described_as: str | None = None
    descriptor_uuids: tuple[str, ...] = ()
    sensitive: bool = False
    forbidden: bool = False
    error: str = ""

    @property
    def unknown(self) -> bool:
        """True when the catalogue has no entry for this attribute."""
        return self.known_as is None

    def summary(self) -> dict[str, Any]:
        """Publishable: identity and shape.  No value is ever read."""
        return {
            "uuid": self.uuid,
            "known_as": self.known_as,
            "unknown_to_this_project": self.unknown,
            "properties": list(self.properties),
            "described_as": self.described_as,
            "descriptors": list(self.descriptor_uuids),
            "sensitive": self.sensitive,
            "forbidden": self.forbidden,
            "error": self.error,
        }


@dataclass
class SurveyedService:
    """One service and the characteristics under it."""

    uuid: str
    known_as: str | None = None
    attributes: list[SurveyedAttribute] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "known_as": self.known_as,
            "attributes": [a.summary() for a in self.attributes],
        }


@dataclass
class ResponseNotification:
    """Something the detector said on the response characteristic, unprompted."""

    at_seconds: float
    length: int
    #: The bytes, hex-encoded.  Private store only -- never in the summary.
    hex: str = ""
    #: A printable rendering, when the payload is short printable text.  These
    #: are protocol tokens like ``RDrespACK`` in every published example, but
    #: nobody has seen this characteristic speak on a non-W R8, so it is
    #: sanitised before it can reach a terminal.
    text: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "at_seconds": round(self.at_seconds, 3),
            "length": self.length,
            "text": self.text,
        }


@dataclass
class Survey:
    """What the device exposes, and what it said while nobody spoke to it."""

    read_at: str = ""
    connected: bool = False
    services: list[SurveyedService] = field(default_factory=list)
    listened_seconds: float = 0.0
    listened: bool = False
    notifications: list[ResponseNotification] = field(default_factory=list)
    notifications_dropped: int = 0
    errors: list[str] = field(default_factory=list)
    capture_name: str = ""

    @property
    def attribute_count(self) -> int:
        return sum(len(service.attributes) for service in self.services)

    @property
    def unknown_attributes(self) -> list[SurveyedAttribute]:
        """Everything the catalogue could not name.  The point of the exercise."""
        return [
            attribute
            for service in self.services
            for attribute in service.attributes
            if attribute.unknown
        ]

    def as_dict(self) -> dict[str, Any]:
        """Safe to print.  Structure and identity; no characteristic value."""
        return {
            "read_at": self.read_at,
            "connected": self.connected,
            "services": [s.summary() for s in self.services],
            "attribute_count": self.attribute_count,
            "unknown_attribute_count": len(self.unknown_attributes),
            "listened": self.listened,
            "listened_seconds": round(self.listened_seconds, 1),
            "notifications": [n.summary() for n in self.notifications],
            "notifications_dropped": self.notifications_dropped,
            "errors": list(self.errors),
            "capture_name": self.capture_name,
        }

    def private_dict(self) -> dict[str, Any]:
        """Everything, including any bytes the response characteristic sent."""
        document = self.as_dict()
        document["notifications"] = [
            {**n.summary(), "hex": n.hex} for n in self.notifications
        ]
        return document

    def render(self) -> str:
        lines = [f"survey {self.read_at}", ""]
        if not self.connected:
            lines.append("  not connected")
            lines.extend(f"  ! {error}" for error in self.errors)
            return "\n".join(lines)

        for service in self.services:
            label = service.known_as or "not in this project's catalogue"
            lines.append(f"  service {service.uuid}  ({label})")
            for attribute in service.attributes:
                marks = []
                if attribute.forbidden:
                    marks.append("FORBIDDEN")
                if attribute.sensitive:
                    marks.append("sensitive")
                if attribute.unknown:
                    marks.append("UNKNOWN to this project")
                suffix = ("  [" + ", ".join(marks) + "]") if marks else ""
                name = attribute.known_as or attribute.described_as or "?"
                lines.append(f"      {attribute.uuid}  {name}{suffix}")
                lines.append(
                    f"          properties: {', '.join(attribute.properties) or 'none'}"
                )
                if attribute.described_as and attribute.known_as:
                    lines.append(f'          device calls it: "{attribute.described_as}"')
                if attribute.error:
                    lines.append(f"          error: {attribute.error}")
            lines.append("")

        unknown = self.unknown_attributes
        lines.append(
            f"  {self.attribute_count} characteristics, "
            f"{len(unknown)} not in this project's catalogue"
        )
        if unknown:
            lines.append("  ^ each of those is undocumented surface worth recording")

        if self.listened:
            lines.append("")
            lines.append(
                f"  listened on the command-response characteristic for "
                f"{self.listened_seconds:.0f}s, sending nothing:"
            )
            if not self.notifications:
                lines.append(
                    "      silence. It says nothing unprompted -- which is a "
                    "measurement, not a failure."
                )
            else:
                for note in self.notifications:
                    shown = note.text if note.text else f"{note.length} bytes"
                    lines.append(f"      +{note.at_seconds:6.2f}s  {shown}")
                if self.notifications_dropped:
                    lines.append(f"      ... and {self.notifications_dropped} more")
                lines.append(
                    "      ^ the detector speaks unprompted. Raw bytes are in the "
                    "private capture."
                )

        lines.extend(f"  ! {error}" for error in self.errors)
        if self.capture_name:
            lines.append(f"  written to the private store as {self.capture_name}")
        lines.append("")
        lines.append("No characteristic value was read.  See docs/SAFETY.md.")
        return "\n".join(lines)


def _properties_of(characteristic: Any) -> tuple[str, ...]:
    raw = getattr(characteristic, "properties", ()) or ()
    return tuple(str(item) for item in raw)


async def _enumerate(client: Any, result: Survey) -> None:
    """Walk the device's own service tree.  Reads descriptors, never values."""
    services = getattr(client, "services", None) or []
    for service in services:
        service_uuid = normalize_uuid(str(getattr(service, "uuid", "")))
        try:
            service_name = describe(service_uuid).name
        except Exception:  # noqa: BLE001 - an unknown service is the finding
            service_name = None
        surveyed = SurveyedService(uuid=service_uuid, known_as=service_name)

        for characteristic in getattr(service, "characteristics", []) or []:
            uuid = normalize_uuid(str(getattr(characteristic, "uuid", "")))
            try:
                known = describe(uuid).name
            except Exception:  # noqa: BLE001 - the whole point of surveying
                known = None
            attribute = SurveyedAttribute(
                uuid=uuid,
                known_as=known,
                properties=_properties_of(characteristic),
                sensitive=uuid in SENSITIVE_UUIDS,
                forbidden=uuid in FORBIDDEN_UUIDS,
            )

            descriptors = getattr(characteristic, "descriptors", []) or []
            attribute.descriptor_uuids = tuple(
                normalize_uuid(str(getattr(d, "uuid", ""))) for d in descriptors
            )
            for descriptor in descriptors:
                descriptor_uuid = normalize_uuid(str(getattr(descriptor, "uuid", "")))
                if descriptor_uuid != CHARACTERISTIC_USER_DESCRIPTION_UUID:
                    continue
                try:
                    payload = bytes(
                        await client.read_gatt_descriptor(
                            int(getattr(descriptor, "handle", 0))
                        )
                    )
                    attribute.described_as = _readable_name(payload)
                except Exception as exc:  # noqa: BLE001 - absence is evidence
                    attribute.error = type(exc).__name__
            surveyed.attributes.append(attribute)
        result.services.append(surveyed)


async def _listen(client: Any, result: Survey, seconds: float, salt: bytes) -> None:
    """Subscribe to the command-response characteristic and send nothing."""
    permitted = assert_survey_notifiable(COMMAND_RESPONSE_UUID)
    started = time.monotonic()

    def _on_notify(_sender: Any, data: bytearray) -> None:
        payload = bytes(data)
        if len(result.notifications) >= MAX_RETAINED_NOTIFICATIONS:
            result.notifications_dropped += 1
            return
        result.notifications.append(
            ResponseNotification(
                at_seconds=time.monotonic() - started,
                length=len(payload),
                hex=payload.hex(),
                text=_readable_name(payload),
            )
        )

    try:
        await client.start_notify(permitted, _on_notify)
    except Exception as exc:  # noqa: BLE001 - a refusal is itself a result
        result.errors.append(scrub(f"listen: {type(exc).__name__}", salt))
        return

    result.listened = True
    try:
        await asyncio.sleep(seconds)
    finally:
        result.listened_seconds = time.monotonic() - started
        with contextlib.suppress(Exception):
            await client.stop_notify(permitted)


async def survey(  # noqa: PLR0913, PLR0917 - injection seams
    address: str,
    store: Any,
    listen_seconds: float = 0.0,
    client_factory: Any = None,
    adapter: str | None = None,
) -> Survey:
    """Enumerate the device, optionally listening for unprompted responses.

    No ``--confirm`` gate, deliberately: unlike :func:`uniden_r8.inspection`,
    this reads no characteristic value, so there is no saved coordinate for it
    to reach. The decision this command asks the operator to make is only how
    long to hold the vehicle's radio.
    """
    from .inspection import _default_client  # noqa: PLC0415 - shared seam

    build = client_factory or _default_client
    salt = store.salt
    result = Survey(read_at=utc_stamp())

    if listen_seconds:
        listen_seconds = max(MIN_LISTEN_SECONDS, min(MAX_LISTEN_SECONDS, listen_seconds))

    try:
        async with build(address, adapter) as client:
            result.connected = True
            await _enumerate(client, result)
            if listen_seconds:
                await _listen(client, result, listen_seconds, salt)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        result.errors.append(scrub(f"{type(exc).__name__}: {exc}", salt))
        return result

    name = "survey-" + "".join(
        character for character in result.read_at if character.isalnum()
    ) + ".json"
    try:
        store.write_json(name, result.private_dict())
        result.capture_name = name
    except Exception as exc:  # noqa: BLE001
        result.errors.append(scrub(f"private capture: {type(exc).__name__}", salt))
    return result
