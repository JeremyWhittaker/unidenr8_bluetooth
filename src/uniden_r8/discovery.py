"""Bounded, read-only BLE discovery.

Bounded **advertisement-only discovery**: it collects advertisements for a
fixed number of seconds and stops.  It does not connect, enumerate services,
read a characteristic or pair.

It is *not* radio-silent, and this module does not claim to be.  BlueZ performs
**active** scanning by default, so an advertisement is answered with a scan
request and the device may reply with a scan response.  What discovery cannot
do is change the detector's state: a scan request asks a device to repeat
itself, it carries no application command, and there is no code path here that
writes a characteristic.

It cannot touch the OBDLink either.  The OBDLink MX+ is a BR/EDR Serial Port
Profile device, it is already bonded, and a bonded BR/EDR adapter does not
appear in an LE scan at all.

Why the scan is bounded, twice
------------------------------
An unbounded scan on this node is the one genuinely risky thing an
inattentive script could do.  ``hci0`` is a single radio shared with an
already-bound ``/dev/rfcomm0``, and BlueZ deprioritises an established link
while an LE discovery session is running.  A scan that outlives its window
because a coroutine hung would degrade the vehicle's telemetry link for as
long as the process stayed up.

So the window is enforced at three independent points, and each one alone is
sufficient:

1. :func:`bounded_seconds` clamps the requested duration at the argument
   boundary, so no code path can request an unbounded window;
2. the duration is passed to the scanner itself; and
3. the whole scan is wrapped in :func:`asyncio.wait_for` with a hard ceiling
   a little above the window, so a scanner that never returns is cancelled
   rather than waited on.

``bleak`` is imported inside the function, not at module scope.  Everything
this project can be reasoned about without a radio -- the gate, the redaction,
the classification -- must stay importable and testable on a machine with no
Bluetooth stack, which includes the workstation this was written on.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from .evidence import utc_stamp
from .gatt import COMMAND_SERVICE_UUID, DATA_SERVICE_UUID, DEVICE_INFORMATION_SERVICE_UUID
from .privacy import redact_address, redact_name

__all__ = [
    "MIN_SCAN_SECONDS",
    "MAX_SCAN_SECONDS",
    "DEFAULT_SCAN_SECONDS",
    "SCAN_GRACE_SECONDS",
    "Advertisement",
    "Candidate",
    "KNOWN_SERVICES",
    "ScanReport",
    "bounded_seconds",
    "classify",
    "is_random_static",
    "summarise",
    "scan",
]

#: A scan shorter than this cannot span an advertising interval reliably; one
#: longer than this is competing with the vehicle's telemetry link for the
#: radio for no additional benefit.  The detector's own pairing window is
#: measured in tens of seconds, so the useful range is narrow on both sides.
MIN_SCAN_SECONDS: Final[float] = 3.0
MAX_SCAN_SECONDS: Final[float] = 60.0
DEFAULT_SCAN_SECONDS: Final[float] = 20.0

#: Head-room for the outer cancellation, above the window the scanner was
#: asked for.  Long enough that a healthy scan is never cancelled by it,
#: short enough that a wedged one does not hold the radio.
SCAN_GRACE_SECONDS: Final[float] = 5.0

#: Advertised-name prefixes that make a device a strong candidate.  Upstream's
#: R8w advertises as ``R8W@xx``; the plain R8's advertised name has never been
#: observed by this project or documented by Uniden, so ``R8`` is included on
#: the reasonable-but-unproven assumption that the naming is consistent within
#: the family.  A miss here costs one manual look at the report, not a
#: failure: every device seen is counted and tiered, nothing is discarded.
_STRONG_PREFIXES: Final[tuple[str, ...]] = ("R8W", "R8", "R9W", "R9", "R4W", "R4")

#: Substrings that make a device worth a second look but prove nothing.
_POSSIBLE_SUBSTRINGS: Final[tuple[str, ...]] = ("UNIDEN", "R/TACH", "RTACH")


#: Services this project can name if it sees one advertised.  Recognising one
#: is strong evidence the vendor attribute table applies to this unit.
KNOWN_SERVICES: Final[dict[str, str]] = {
    DATA_SERVICE_UUID: "uniden-data",
    COMMAND_SERVICE_UUID: "uniden-command",
    DEVICE_INFORMATION_SERVICE_UUID: "device-information",
}


class _Scanner(Protocol):
    """The one bleak API this module uses, named so tests can substitute it."""

    async def __call__(self, timeout: float) -> Sequence[Any]: ...


@dataclass(frozen=True)
class Advertisement:
    """One device seen during a scan, before redaction.

    ``service_uuids`` is the highest-value thing an advertisement can carry
    here.  If the detector advertises the Uniden data service, that is
    evidence the vendor attribute table matches upstream's R8w *before*
    anything connects to it. Discovery is advertisement-only, but BlueZ scans
    actively and may send a link-layer scan request.
    """

    address: str
    name: str | None = None
    rssi: int | None = None
    service_uuids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    """One device as it appears in a *publishable* report.

    The address is never carried here, only its token.  There is no field that
    could hold one, which is the point: redaction happens at construction, so
    a later formatting mistake has nothing raw to reach for.
    """

    token: str
    name: str
    tier: str
    rssi: int | None
    random_static: bool
    #: Advertised service UUIDs that this project already knows about.  A
    #: service UUID is not an identifier -- it is the same value on every unit
    #: of the model -- so unlike everything else here it is published verbatim.
    known_services: tuple[str, ...] = ()

    def line(self) -> str:
        strength = f"{self.rssi} dBm" if self.rssi is not None else "rssi unknown"
        addr_kind = "random-static" if self.random_static else "other addr type"
        line = f"{self.tier:<9} {self.name:<22} {self.token}  {strength}, {addr_kind}"
        if self.known_services:
            line += f"\n{'':>9} advertises known service(s): " + ", ".join(self.known_services)
        return line


@dataclass
class ScanReport:
    """The sanitized result of one bounded scan."""

    started_at: str
    requested_seconds: float
    total_seen: int
    candidates: list[Candidate] = field(default_factory=list)
    timed_out: bool = False

    @property
    def strong(self) -> list[Candidate]:
        return [c for c in self.candidates if c.tier == "strong"]

    @property
    def possible(self) -> list[Candidate]:
        return [c for c in self.candidates if c.tier == "possible"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "requested_seconds": self.requested_seconds,
            "total_seen": self.total_seen,
            "timed_out": self.timed_out,
            "candidates": [
                {
                    "token": c.token,
                    "name": c.name,
                    "tier": c.tier,
                    "rssi": c.rssi,
                    "random_static": c.random_static,
                    "known_services": list(c.known_services),
                }
                for c in self.candidates
            ],
        }


def bounded_seconds(seconds: float | None) -> float:
    """Clamp *seconds* into the permitted window.

    Clamping rather than raising: this is called from the CLI, and a typo that
    aborts the run while Jeremy is standing at the detector with the pairing
    window open is a worse outcome than a scan of a slightly different length.
    The bound itself is not negotiable, only the caller's arithmetic.
    """
    if seconds is None:
        return DEFAULT_SCAN_SECONDS
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return DEFAULT_SCAN_SECONDS
    if math.isnan(value):
        return DEFAULT_SCAN_SECONDS
    return max(MIN_SCAN_SECONDS, min(MAX_SCAN_SECONDS, value))


def is_random_static(address: str) -> bool:
    """Return ``True`` if *address* is a BLE random static address.

    The top two bits of the most significant octet are set for a random static
    address.  Upstream reports its R8w uses one, so an *unnamed* device with a
    random static address is worth surfacing when the named search comes up
    empty.  It is a hint and is reported as one -- plenty of unrelated devices
    use random static addresses too.
    """
    cleaned = address.replace(":", "").replace("-", "").strip()
    if len(cleaned) < 2:
        return False
    try:
        first_octet = int(cleaned[:2], 16)
    except ValueError:
        return False
    return first_octet & 0xC0 == 0xC0


def classify(name: str | None) -> str:
    """Return the candidate tier for an advertised *name*.

    ``strong``    the name looks like a Uniden R-series detector
    ``possible``  the name mentions Uniden or the app
    ``unnamed``   no name resolved; judged on address type instead
    ``other``     something else entirely, counted and ignored
    """
    if name is None or not name.strip():
        return "unnamed"
    upper = name.strip().upper()
    for prefix in _STRONG_PREFIXES:
        if upper == prefix or upper.startswith(f"{prefix}@") or upper.startswith(f"{prefix}-"):
            return "strong"
    if any(needle in upper for needle in _POSSIBLE_SUBSTRINGS):
        return "possible"
    return "other"


def summarise(
    advertisements: Iterable[Advertisement],
    salt: bytes,
    *,
    started_at: str | None = None,
    requested_seconds: float = DEFAULT_SCAN_SECONDS,
    timed_out: bool = False,
) -> ScanReport:
    """Turn raw advertisements into a publishable :class:`ScanReport`.

    Every device seen is counted.  Only the interesting ones are listed, and
    even those carry a token rather than an address.  ``other`` devices are
    counted but never enumerated: the neighbours' phones and headphones are
    not this project's business, and listing them would be a small privacy
    leak of its own.
    """
    seen = list(advertisements)
    candidates: list[Candidate] = []

    for advertisement in seen:
        tier = classify(advertisement.name)
        random_static = is_random_static(advertisement.address)
        if tier == "other":
            continue
        if tier == "unnamed" and not random_static:
            continue
        matched = tuple(
            label
            for uuid, label in KNOWN_SERVICES.items()
            if uuid in {u.lower() for u in advertisement.service_uuids}
        )
        candidates.append(
            Candidate(
                token=redact_address(advertisement.address, salt),
                name=redact_name(advertisement.name, salt),
                tier=tier,
                rssi=advertisement.rssi,
                random_static=random_static,
                known_services=matched,
            )
        )

    rank = {"strong": 0, "possible": 1, "unnamed": 2}
    candidates.sort(key=lambda c: (rank.get(c.tier, 3), -(c.rssi or -999), c.token))

    return ScanReport(
        started_at=started_at or utc_stamp(),
        requested_seconds=requested_seconds,
        total_seen=len(seen),
        candidates=candidates,
        timed_out=timed_out,
    )


async def _discover(timeout: float) -> Any:
    """Run one bleak discovery.  Imported here so the module loads without it.

    ``return_adv=True`` because bleak 3.x moved RSSI and the advertised
    service UUIDs off ``BLEDevice`` and onto ``AdvertisementData``; without it
    every scan reports "rssi unknown" and sees no services.
    """
    from bleak import BleakScanner  # noqa: PLC0415 - deliberate lazy import

    return await BleakScanner.discover(timeout=timeout, return_adv=True)


def _to_advertisement(entry: Any) -> Advertisement:
    """Adapt one scanner result without depending on bleak's types.

    Accepts all three shapes this has to survive: a bare ``BLEDevice`` (bleak
    2.x and the tests), and a ``(BLEDevice, AdvertisementData)`` pair, which is
    what ``return_adv=True`` yields.
    """
    device, advertisement = entry if isinstance(entry, tuple) else (entry, None)

    def field(name: str, default: Any = None) -> Any:
        value = getattr(advertisement, name, None) if advertisement is not None else None
        return value if value is not None else getattr(device, name, default)

    return Advertisement(
        address=str(getattr(device, "address", "") or ""),
        name=field("local_name") or getattr(device, "name", None),
        rssi=field("rssi"),
        service_uuids=tuple(field("service_uuids", ()) or ()),
    )


def _iter_results(result: Any) -> Iterable[Any]:
    """Yield scanner entries from a list of devices or a ``return_adv`` dict."""
    if isinstance(result, dict):
        return list(result.values())
    return list(result)


async def scan(
    seconds: float | None = None,
    salt: bytes = b"",
    *,
    scanner: _Scanner | None = None,
) -> ScanReport:
    """Listen for advertisements for a bounded window and report, sanitized.

    Advertisement-only: no connection, no characteristic access.  Returns a
    report even when the scanner hangs and has to be cancelled -- ``timed_out``
    says so -- because "nothing answered" and "the radio wedged" are different
    findings and the operator needs to be told which one happened.
    """
    if not salt:
        raise ValueError("scan() needs a redaction salt; see PrivateStore.salt")

    window = bounded_seconds(seconds)
    started_at = utc_stamp()
    run = scanner or _discover

    timed_out = False
    devices: Any = ()
    try:
        devices = await asyncio.wait_for(
            run(timeout=window), timeout=window + SCAN_GRACE_SECONDS
        )
    except TimeoutError:
        timed_out = True

    return summarise(
        (_to_advertisement(entry) for entry in _iter_results(devices)),
        salt,
        started_at=started_at,
        requested_seconds=window,
        timed_out=timed_out,
    )
