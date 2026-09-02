"""Read the detector's identity, using GATT Reads only.

This is the answer to "what model and firmware is it, really", and it is
obtainable entirely inside the read-only boundary. Firmware and software
version are Bluetooth SIG Device Information characteristics — 0x2A26 and
0x2A28 — and reading them is an ATT read of a value the peripheral already
holds. No application command is written to the command characteristic, which
is the line ``docs/SAFETY.md`` §2 draws and this module stays on the safe side
of.

Every read goes through :func:`uniden_r8.gatt.assert_readable` first. There is
no code path here that writes, and :mod:`uniden_r8.audit` proves it.

Service discovery is included because it costs nothing extra — BlueZ performs
it on connect regardless — and it answers the open question that matters most:
whether Jeremy's **R8** exposes the same vendor attribute table that upstream
documented on an **R8w**. The scan already showed the two models differ in
their advertised name, so this is not a formality.

Deliberately not read
---------------------
* **0x2A25, Serial Number String.** A per-unit identifier with no diagnostic
  value here. Not in the plan and not requested.
* **The POI characteristic.** It carries saved coordinates — home, work, the
  roads Jeremy drives. Identity does not need it.
* **Anything vendor-specific.** Not required to answer the model and firmware
  question. The project later confirmed the UUID layout and telemetry shape,
  but this identity operation remains confined to Device Information.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .discovery import KNOWN_SERVICES
from .evidence import utc_stamp
from .gatt import (
    IDENTITY_READ_PLAN,
    assert_readable,
    describe,
)
from .privacy import scrub

__all__ = ["Identity", "DiscoveredService", "read_identity"]

#: Ceiling on the whole connect-read-disconnect cycle.  The radio is shared
#: with the vehicle's RFCOMM link, so this is bounded for the same reason the
#: scan is; see docs/SAFETY.md §1.
CONNECT_TIMEOUT_SECONDS: float = 25.0
SESSION_CEILING_SECONDS: float = 60.0


@dataclass(frozen=True)
class DiscoveredService:
    """One service the detector actually exposes."""

    uuid: str
    characteristic_uuids: tuple[str, ...] = ()

    @property
    def is_known(self) -> bool:
        return self.uuid.lower() in KNOWN_SERVICES


@dataclass
class Identity:
    """What the detector said about itself."""

    read_at: str = ""
    connected: bool = False
    values: dict[str, str] = field(default_factory=dict)
    services: list[DiscoveredService] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def model(self) -> str | None:
        return self.values.get("Model Number String")

    @property
    def manufacturer(self) -> str | None:
        return self.values.get("Manufacturer Name String")

    @property
    def firmware(self) -> str | None:
        return self.values.get("Firmware Revision String")

    @property
    def software(self) -> str | None:
        return self.values.get("Software Revision String")

    def as_dict(self) -> dict[str, Any]:
        return {
            "read_at": self.read_at,
            "connected": self.connected,
            "values": dict(self.values),
            "services": [
                {"uuid": s.uuid, "known": s.is_known,
                 "characteristics": list(s.characteristic_uuids)}
                for s in self.services
            ],
            "errors": list(self.errors),
        }

    def render(self) -> str:
        lines = [f"identity read {self.read_at}", ""]
        if not self.connected:
            lines.append("Did not connect.")
            lines += [f"  {e}" for e in self.errors]
            return "\n".join(lines)

        for uuid in IDENTITY_READ_PLAN:
            name = describe(uuid).name
            value = self.values.get(name)
            lines.append(f"  {name:<26} {value if value else '<not exposed>'}")

        lines += ["", f"services exposed: {len(self.services)}"]
        for service in self.services:
            mark = "KNOWN  " if service.is_known else "unknown"
            lines.append(f"  {mark} {service.uuid}  ({len(service.characteristic_uuids)} chars)")
        if self.errors:
            lines += ["", "notes:"] + [f"  {e}" for e in self.errors]
        return "\n".join(lines)


def _decode(raw: bytes) -> str:
    return bytes(raw).decode("utf-8", "replace").strip("\x00").strip()


def _default_client(address: str):
    """Build a real bleak client.  Imported lazily so this module loads without it."""
    from bleak import BleakClient  # noqa: PLC0415 - deliberate lazy import

    return BleakClient(address, timeout=CONNECT_TIMEOUT_SECONDS)


async def _session(address: str, salt: bytes, client_factory=None) -> Identity:
    """One connect-read-disconnect cycle.

    ``client_factory`` exists so the whole path is exercisable without a radio:
    the tests substitute a client that records exactly which characteristics
    were touched, which is the only way to prove the read allowlist is honoured
    at the call site rather than merely declared.
    """
    build = client_factory or _default_client
    identity = Identity(read_at=utc_stamp())

    # `async with` guarantees teardown: the link is released even if a read
    # raises.  A held link matters here -- the detector stops advertising while
    # anything is connected, so a leaked connection makes it invisible to the
    # next run.
    async with build(address) as client:
        identity.connected = True

        for service in client.services:
            identity.services.append(
                DiscoveredService(
                    uuid=str(service.uuid).lower(),
                    characteristic_uuids=tuple(
                        str(c.uuid).lower() for c in service.characteristics
                    ),
                )
            )

        for uuid in IDENTITY_READ_PLAN:
            # The gate, not a comment, is what confines this to reads.  It is
            # called per-UUID at the call site so a future edit to the plan
            # cannot smuggle in something the allowlist would reject.
            permitted = assert_readable(uuid)
            name = describe(permitted).name
            try:
                identity.values[name] = _decode(await client.read_gatt_char(permitted))
            except Exception as exc:  # noqa: BLE001 - any failure is just "absent"
                identity.errors.append(scrub(f"{name}: {type(exc).__name__}: {exc}", salt))

    return identity


async def read_identity(address: str, salt: bytes, client_factory=None) -> Identity:
    """Connect, read the Device Information characteristics, disconnect.

    Bounded end to end. A failure to connect is reported, not raised: "the
    detector did not answer" is a finding, and the caller needs it in the same
    shape as a success.
    """
    if not salt:
        raise ValueError("read_identity() needs a redaction salt")
    try:
        return await asyncio.wait_for(
            _session(address, salt, client_factory), timeout=SESSION_CEILING_SECONDS
        )
    except TimeoutError:
        return Identity(
            read_at=utc_stamp(),
            connected=False,
            errors=[f"timed out after {SESSION_CEILING_SECONDS:g}s"],
        )
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return Identity(
            read_at=utc_stamp(),
            connected=False,
            errors=[scrub(f"{type(exc).__name__}: {exc}", salt)],
        )
