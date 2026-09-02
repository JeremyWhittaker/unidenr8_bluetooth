"""Read-only Uniden R8 BLE support for the Hummer telemetry node.

This package receives.  It has **no application-characteristic write path**:
it never writes to the Uniden command characteristic and never sends a mute,
user-mark, settings or any other R/Tach command.  See :mod:`uniden_r8.gatt` for
the allowlist that enforces it and ``docs/SAFETY.md`` for why the capability is
absent rather than disabled.

That is the honest invariant, and it is narrower than "never transmits".  This
is a radio.  BlueZ discovery is *active* by default, so a scan answers an
advertisement with a scan request; a connection, a GATT Read and a notification
subscription all put frames on the air, and subscribing writes a descriptor.
None of those carries an application command to the detector, which is the
distinction that actually protects it.

It also has nothing to do with the OBDLink MX+.  That adapter is a BR/EDR
Serial Port Profile device bound to ``/dev/rfcomm0`` by
``hummer-rfcomm.service``, and nothing here imports a serial library, touches
that device node, or manages that unit.

Nothing at this level imports :mod:`bleak`. The gate, redaction and
classification remain testable without a Bluetooth stack; the discovery,
identity and telemetry modules import their radio dependencies lazily at the
operation that needs them.
"""

from __future__ import annotations

from .audit import Finding, audit_module, audit_package
from .discovery import (
    DEFAULT_SCAN_SECONDS,
    MAX_SCAN_SECONDS,
    MIN_SCAN_SECONDS,
    Advertisement,
    Candidate,
    ScanReport,
    bounded_seconds,
    classify,
    scan,
    summarise,
)
from .evidence import PrivateStore, PublicationRefused, publish
from .gatt import (
    CATALOGUE,
    FORBIDDEN_UUIDS,
    IDENTITY_READ_PLAN,
    KNOWN_WRITE_COMMANDS,
    NOTIFY_UUIDS,
    PROBE_PLAN,
    READABLE_UUIDS,
    Characteristic,
    Evidence,
    UnknownCharacteristic,
    WriteRefused,
    assert_notifiable,
    assert_readable,
    describe,
    refuse_command,
)
from .privacy import redact_address, redact_name, scrub, token

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # safety
    "Evidence", "Characteristic", "CATALOGUE",
    "READABLE_UUIDS", "NOTIFY_UUIDS", "FORBIDDEN_UUIDS",
    "IDENTITY_READ_PLAN", "PROBE_PLAN", "KNOWN_WRITE_COMMANDS",
    "assert_readable", "assert_notifiable", "describe", "refuse_command",
    "WriteRefused", "UnknownCharacteristic",
    # privacy
    "token", "redact_address", "redact_name", "scrub",
    "PrivateStore", "publish", "PublicationRefused",
    # source-level audit
    "audit_package", "audit_module", "Finding",
    # scanning
    "Advertisement", "Candidate", "ScanReport",
    "scan", "summarise", "classify", "bounded_seconds",
    "MIN_SCAN_SECONDS", "MAX_SCAN_SECONDS", "DEFAULT_SCAN_SECONDS",
]
