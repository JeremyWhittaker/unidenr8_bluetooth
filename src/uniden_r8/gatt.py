"""The GATT catalogue and the read-only gate.

Two jobs, deliberately in one file so they cannot drift apart:

1. Record what is known about the Uniden BLE attribute table, with the
   provenance of every entry attached, and
2. refuse, mechanically, any operation that is not provably read-only.

Provenance matters more than usual here.  Everything in the vendor-specific
half of this table comes from one person's reverse engineering of one *R8w*
(``AegisX86/UnidenR8wlink``, commit ``9072bc2f``), and Jeremy's detector is a
plain **R8**, a different model whose March 2024 owner's manual predates the
current Bluetooth/R/Tach documentation. Each entry retains its source
provenance. Runtime observations on Jeremy's R8 are recorded separately in
``docs/EVIDENCE.md`` so later confirmation does not erase where a definition
originally came from.

The gate
--------
This is an **allowlist**, in the same shape as the OBD node's command gate.
An operation is permitted only if it is named here as permitted; an unknown
UUID is refused rather than guessed at.  On top of that:

* :data:`COMMAND_WRITE_UUID` is on a permanent denylist, so a mistake in the
  allowlist cannot open the one characteristic that actuates the detector;
* :data:`KNOWN_WRITE_COMMANDS` is recorded for documentation only, and
  :func:`refuse_command` is the *only* function in this package that takes
  one -- it raises, unconditionally, and there is no flag that changes that;
* no module in this package imports or calls ``write_gatt_char``.  That is
  asserted by a test that reads the package source, because a comment saying
  so is not a control.

Writing to a radar detector is not a theoretical risk to be managed with a
confirmation flag.  Upstream's own write path is documented as never having
been sent to hardware, the commands were lifted from a decompiled app, and
the detector in question is a live safety device in a moving vehicle.  This
project's answer is that the capability is absent, not disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "Evidence",
    "Characteristic",
    "DATA_SERVICE_UUID",
    "COMMAND_SERVICE_UUID",
    "DEVICE_INFORMATION_SERVICE_UUID",
    "TELEMETRY_UUID",
    "ALERT_UUID",
    "POI_UUID",
    "SETTINGS_1_UUID",
    "SETTINGS_2_UUID",
    "COMMAND_WRITE_UUID",
    "COMMAND_RESPONSE_UUID",
    "MANUFACTURER_NAME_UUID",
    "MODEL_NUMBER_UUID",
    "FIRMWARE_REVISION_UUID",
    "SOFTWARE_REVISION_UUID",
    "CATALOGUE",
    "READABLE_UUIDS",
    "NOTIFY_UUIDS",
    "FORBIDDEN_UUIDS",
    "IDENTITY_READ_PLAN",
    "PROBE_PLAN",
    "LIVE_READ_UUIDS",
    "LIVE_NOTIFY_UUIDS",
    "REQUIRED_LIVE_ATTRIBUTES",
    "assert_live_readable",
    "assert_live_notifiable",
    "KNOWN_WRITE_COMMANDS",
    "WriteRefused",
    "UnknownCharacteristic",
    "normalize_uuid",
    "describe",
    "assert_readable",
    "assert_notifiable",
    "refuse_command",
]


class Evidence(StrEnum):
    """How well a claim in this file is supported.

    ``OFFICIAL``
        Stated by Uniden, in the product page, an owner's manual, or a
        support article.  Cited in :doc:`../docs/EVIDENCE.md`.
    ``UPSTREAM``
        Read out of ``AegisX86/UnidenR8wlink`` at commit ``9072bc2f``, whose
        author confirmed it against a real **R8w**. Runtime confirmation on
        this R8 is tracked separately in the evidence ledger.
    ``UPSTREAM_UNVERIFIED``
        Present in that upstream, but documented there as untested even on
        the R8w -- decompiled from the app and never sent to hardware.
    ``INFERENCE``
        This project's reasoning.  Not observed anywhere.
    ``OBSERVED``
        Seen on Jeremy's own detector by this project. The catalogue currently
        retains source provenance while the evidence ledger records observed
        runtime results.
    """

    OFFICIAL = "official"
    UPSTREAM = "upstream-r8w"
    UPSTREAM_UNVERIFIED = "upstream-r8w-unverified"
    INFERENCE = "inference"
    OBSERVED = "observed-on-this-r8"


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------

#: Vendor data service carrying telemetry, alerts, POI and settings.
DATA_SERVICE_UUID: Final[str] = "18424398-7cbc-11e9-8f9e-2a86e4085a59"

#: Vendor command service.  This project never writes to it.
COMMAND_SERVICE_UUID: Final[str] = "1842467c-7cbc-11e9-8f9e-2a86e4085a59"

#: Bluetooth SIG Device Information service (0x180A).
DEVICE_INFORMATION_SERVICE_UUID: Final[str] = "0000180a-0000-1000-8000-00805f9b34fb"


# --------------------------------------------------------------------------
# Characteristics
# --------------------------------------------------------------------------

TELEMETRY_UUID: Final[str] = "6c290d2e-1c03-aca1-ab48-a9b908bae79e"
ALERT_UUID: Final[str] = "6eb675ab-8bd1-1b9a-7444-621e52ec6823"
POI_UUID: Final[str] = "15005991-b131-3396-014c-664c9867b917"
SETTINGS_1_UUID: Final[str] = "2d86686a-53dc-25b3-0c4a-f0e10c8dee20"
SETTINGS_2_UUID: Final[str] = "5a87b4ef-3bfa-76a8-e642-92933c31434f"

#: Write-without-response command characteristic.  Permanently forbidden.
COMMAND_WRITE_UUID: Final[str] = "2c86686a-53dc-25b3-0c4a-f0e10c8dee20"

#: Where a command response would arrive.  Readable, but pointless without a
#: command, so it is not in the probe plan.
COMMAND_RESPONSE_UUID: Final[str] = "5987b4ef-3bfa-76a8-e642-92933c31434f"

# Standard Device Information characteristics.  These are ordinary GATT
# *reads*: no application-protocol command is written to obtain them.  That
# distinction is the whole reason the identity question is answerable inside
# the read-only boundary, so it is stated here as well as in docs/SAFETY.md.
MANUFACTURER_NAME_UUID: Final[str] = "00002a29-0000-1000-8000-00805f9b34fb"
MODEL_NUMBER_UUID: Final[str] = "00002a24-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_UUID: Final[str] = "00002a26-0000-1000-8000-00805f9b34fb"
SOFTWARE_REVISION_UUID: Final[str] = "00002a28-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class Characteristic:
    """One attribute-table entry and how well it is evidenced."""

    uuid: str
    name: str
    service: str
    readable: bool
    notifies: bool
    #: True if the *device* accepts writes here.  This project never uses it;
    #: it is recorded so the denylist has a reason attached to it.
    device_accepts_writes: bool
    evidence: Evidence
    note: str = ""


CATALOGUE: Final[tuple[Characteristic, ...]] = (
    Characteristic(
        uuid=FIRMWARE_REVISION_UUID,
        name="Firmware Revision String",
        service=DEVICE_INFORMATION_SERVICE_UUID,
        readable=True,
        notifies=False,
        device_accepts_writes=False,
        evidence=Evidence.OFFICIAL,
        note=(
            "Bluetooth SIG characteristic 0x2A26. A plain GATT read; this "
            "R8 returned eight literal NA placeholders."
        ),
    ),
    Characteristic(
        uuid=SOFTWARE_REVISION_UUID,
        name="Software Revision String",
        service=DEVICE_INFORMATION_SERVICE_UUID,
        readable=True,
        notifies=False,
        device_accepts_writes=False,
        evidence=Evidence.OFFICIAL,
        note="Bluetooth SIG characteristic 0x2A28.  Plain GATT read.",
    ),
    Characteristic(
        uuid=MODEL_NUMBER_UUID,
        name="Model Number String",
        service=DEVICE_INFORMATION_SERVICE_UUID,
        readable=True,
        notifies=False,
        device_accepts_writes=False,
        evidence=Evidence.OFFICIAL,
        note=(
            "Bluetooth SIG characteristic 0x2A24.  Upstream never read it. "
            "It is the most direct answer available to 'is this an R8 or an "
            "R8w', so this project reads it first."
        ),
    ),
    Characteristic(
        uuid=MANUFACTURER_NAME_UUID,
        name="Manufacturer Name String",
        service=DEVICE_INFORMATION_SERVICE_UUID,
        readable=True,
        notifies=False,
        device_accepts_writes=False,
        evidence=Evidence.OFFICIAL,
        note="Bluetooth SIG characteristic 0x2A29.  Upstream never read it.",
    ),
    Characteristic(
        uuid=TELEMETRY_UUID,
        name="Telemetry (ETC data)",
        service=DATA_SERVICE_UUID,
        readable=True,
        notifies=True,
        device_accepts_writes=True,
        evidence=Evidence.UPSTREAM,
        note=(
            "UTF-8, '&'-delimited: voltage, POI, GPS group, warning, "
            "scanCount, wifi, brightness.  Notifies every 1-2 s on the R8w. "
            "The device accepts writes here; this project only reads."
        ),
    ),
    Characteristic(
        uuid=ALERT_UUID,
        name="Alerts",
        service=DATA_SERVICE_UUID,
        readable=True,
        notifies=True,
        device_accepts_writes=True,
        evidence=Evidence.UPSTREAM,
        note=(
            "UTF-8 full snapshots, '&' between alert slots.  Notifies on "
            "detection change.  Mute state is field 7, so mute can be "
            "observed without ever sending a mute command."
        ),
    ),
    Characteristic(
        uuid=POI_UUID,
        name="POI database",
        service=DATA_SERVICE_UUID,
        readable=True,
        notifies=False,
        device_accepts_writes=True,
        evidence=Evidence.UPSTREAM,
        note=(
            "Binary, variable-length records.  The only characteristic that "
            "carries real coordinates, so its contents are private evidence "
            "even when everything else is publishable."
        ),
    ),
    Characteristic(
        uuid=SETTINGS_1_UUID,
        name="Settings 1",
        service=DATA_SERVICE_UUID,
        readable=True,
        notifies=False,
        device_accepts_writes=True,
        evidence=Evidence.UPSTREAM,
        note="~200 bytes, mostly undecoded upstream.  Read as opaque bytes.",
    ),
    Characteristic(
        uuid=SETTINGS_2_UUID,
        name="Settings 2",
        service=DATA_SERVICE_UUID,
        readable=True,
        notifies=False,
        device_accepts_writes=True,
        evidence=Evidence.UPSTREAM,
        note="All 0xff on upstream's R8w.  Read as opaque bytes.",
    ),
    Characteristic(
        uuid=COMMAND_RESPONSE_UUID,
        name="Command response",
        service=COMMAND_SERVICE_UUID,
        readable=True,
        notifies=True,
        device_accepts_writes=False,
        evidence=Evidence.UPSTREAM_UNVERIFIED,
        note=(
            "Upstream never confirmed this answers anything.  Only a command "
            "would make it interesting, and this project sends none, so it "
            "is excluded from the probe plan."
        ),
    ),
    Characteristic(
        uuid=COMMAND_WRITE_UUID,
        name="Command write",
        service=COMMAND_SERVICE_UUID,
        readable=False,
        notifies=False,
        device_accepts_writes=True,
        evidence=Evidence.UPSTREAM_UNVERIFIED,
        note=(
            "Write-without-response.  The one characteristic that actuates "
            "the detector.  Permanently forbidden here; see FORBIDDEN_UUIDS."
        ),
    ),
)

_BY_UUID: Final[dict[str, Characteristic]] = {c.uuid: c for c in CATALOGUE}

#: Characteristics this project may issue a GATT Read against.
READABLE_UUIDS: Final[frozenset[str]] = frozenset(
    c.uuid for c in CATALOGUE if c.readable and c.uuid != COMMAND_RESPONSE_UUID
)

#: Characteristics this project may subscribe to.  Subscribing writes to the
#: CCCD, not to the characteristic, and it is what "receive BLE data" means;
#: it cannot carry an application command to the detector.
NOTIFY_UUIDS: Final[frozenset[str]] = frozenset(
    c.uuid for c in CATALOGUE if c.notifies and c.uuid != COMMAND_RESPONSE_UUID
)

#: Never touched, by any operation, whatever else changes.
FORBIDDEN_UUIDS: Final[frozenset[str]] = frozenset({COMMAND_WRITE_UUID})

#: The identity question, answered with GATT reads only.  Ordered so that the
#: model answer -- the thing that decides whether the rest of the upstream
#: table applies at all -- arrives first.
IDENTITY_READ_PLAN: Final[tuple[str, ...]] = (
    MODEL_NUMBER_UUID,
    MANUFACTURER_NAME_UUID,
    FIRMWARE_REVISION_UUID,
    SOFTWARE_REVISION_UUID,
)

#: Every operation a future connected probe is permitted to perform, in order.
#: Reads first, then subscriptions.  Nothing else is in the plan, and the gate
#: below is what enforces that at the call site.
PROBE_PLAN: Final[tuple[tuple[str, str], ...]] = (
    *(("read", uuid) for uuid in IDENTITY_READ_PLAN),
    ("read", TELEMETRY_UUID),
    ("read", ALERT_UUID),
    ("read", SETTINGS_1_UUID),
    ("read", SETTINGS_2_UUID),
    ("read", POI_UUID),
    ("notify", TELEMETRY_UUID),
    ("notify", ALERT_UUID),
)

#: The **live data** subset: the only characteristics the receive path may
#: touch.  Deliberately narrower than :data:`READABLE_UUIDS`, which describes
#: everything a read-only probe *could* legitimately read.
#:
#: POI and the two settings characteristics are excluded on purpose.  POI holds
#: saved camera and user-mark coordinates -- home, work, the roads Jeremy
#: drives -- and settings hold device configuration.  Neither is needed to pull
#: live detector data, so neither is read.  Excluding them is cheaper than
#: handling them carefully.
LIVE_READ_UUIDS: Final[frozenset[str]] = frozenset({TELEMETRY_UUID, ALERT_UUID})

#: The only characteristics the receive path may subscribe to.
LIVE_NOTIFY_UUIDS: Final[frozenset[str]] = frozenset({TELEMETRY_UUID, ALERT_UUID})

#: Vendor attributes that must actually be present on the connected device
#: before the receive path will run. Upstream documented these on an *R8w*;
#: this project has since observed the exact service/characteristic layout on
#: Jeremy's R8, but the gate still checks every connection so firmware drift is
#: refused rather than assumed compatible.
REQUIRED_LIVE_ATTRIBUTES: Final[tuple[str, ...]] = (
    DATA_SERVICE_UUID,
    TELEMETRY_UUID,
    ALERT_UUID,
)


def assert_live_readable(uuid: str) -> str:
    """Return *uuid* if the receive path may GATT-read it, else raise."""
    canonical = assert_readable(uuid)
    if canonical not in LIVE_READ_UUIDS:
        raise WriteRefused(
            f"{describe(canonical).name} is readable in principle but is not "
            f"in the live-data set; POI and settings are never read by default"
        )
    return canonical


def assert_live_notifiable(uuid: str) -> str:
    """Return *uuid* if the receive path may subscribe to it, else raise."""
    canonical = assert_notifiable(uuid)
    if canonical not in LIVE_NOTIFY_UUIDS:
        raise WriteRefused(
            f"{describe(canonical).name} is not in the live-data notify set"
        )
    return canonical


#: Application commands upstream decompiled out of the Uniden R/Tach app.
#:
#: Recorded so the runbook, the docs and the gate cannot disagree about what
#: is being refused, and so a reviewer can see the exact bytes that will never
#: be sent.  Upstream states plainly that none of these was ever transmitted
#: to hardware, on any model.  :func:`refuse_command` is the only function
#: that accepts one and it always raises.
KNOWN_WRITE_COMMANDS: Final[tuple[str, ...]] = (
    "BTreqMUTE:1",
    "BTreqMUTE:0",
    "BTreqMMEM:1",
    "BTreqMMEM:0",
    "BTreqUMRK:1",
    "BTreqUMRK:0",
    "BTreqRLCD:0",
)


class UnknownCharacteristic(ValueError):
    """Raised for a UUID that is not in :data:`CATALOGUE`."""


class WriteRefused(PermissionError):
    """Raised for any attempt to transmit to the detector.

    A ``PermissionError`` rather than a ``ValueError`` on purpose: this is not
    a malformed argument that a caller could fix by passing something else.
    There is no argument that is accepted.
    """


def normalize_uuid(uuid: str) -> str:
    """Return the canonical lowercase form of *uuid*.

    BlueZ, bleak and this file do not always agree on case, and a gate that
    can be bypassed by upper-casing an argument is not a gate.
    """
    if not isinstance(uuid, str):
        raise UnknownCharacteristic(f"uuid must be a string, got {type(uuid)!r}")
    return uuid.strip().lower()


def describe(uuid: str) -> Characteristic:
    """Return the catalogue entry for *uuid*, or raise."""
    entry = _BY_UUID.get(normalize_uuid(uuid))
    if entry is None:
        raise UnknownCharacteristic(
            f"{uuid!r} is not in the catalogue; unknown attributes are "
            f"refused, not guessed at"
        )
    return entry


def _assert_not_forbidden(uuid: str) -> str:
    canonical = normalize_uuid(uuid)
    if canonical in FORBIDDEN_UUIDS:
        raise WriteRefused(
            f"{canonical} is the command-write characteristic and is "
            f"permanently forbidden by this project"
        )
    return canonical


def assert_readable(uuid: str) -> str:
    """Return *uuid* if a GATT Read against it is permitted, else raise."""
    canonical = _assert_not_forbidden(uuid)
    entry = describe(canonical)
    if canonical not in READABLE_UUIDS:
        raise WriteRefused(
            f"reading {entry.name} ({canonical}) is not on the read-only "
            f"allowlist"
        )
    return canonical


def assert_notifiable(uuid: str) -> str:
    """Return *uuid* if subscribing to it is permitted, else raise."""
    canonical = _assert_not_forbidden(uuid)
    entry = describe(canonical)
    if canonical not in NOTIFY_UUIDS:
        raise WriteRefused(
            f"subscribing to {entry.name} ({canonical}) is not on the "
            f"read-only allowlist"
        )
    return canonical


def refuse_command(command: str) -> None:
    """Always raise :class:`WriteRefused`.

    This exists so that "the project refuses to transmit" is a tested
    behaviour with a call site, rather than an absence someone has to take on
    trust.  There is no ``allow_writes`` parameter, here or anywhere else in
    this package, and adding one would be a change to the project's purpose
    rather than a feature.
    """
    known = " (a known R/Tach command)" if command in KNOWN_WRITE_COMMANDS else ""
    raise WriteRefused(
        f"refusing to transmit {command!r}{known}: this project has no "
        f"write path to the detector.  Every command of this kind is "
        f"decompiled, untested on any hardware, and aimed at a live safety "
        f"device.  See docs/SAFETY.md."
    )
