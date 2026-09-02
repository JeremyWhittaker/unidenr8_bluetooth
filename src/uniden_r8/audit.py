"""Source-level proof that this package has no application-write path.

``docs/SAFETY.md`` claims the project has no application-characteristic write
path.  A claim in a document is not a control, and neither is a substring
search: this file's own prose mentions ``write_gatt_char`` several times, and a
grep-based check would either fail on that or be weakened until it stopped
catching anything.

Note what this proves and what it does not.  It proves no module can write a
value to a characteristic.  It does **not** make the project radio-silent --
nothing could, short of not using Bluetooth -- and it is not a claim that no
frames are transmitted.

So the check parses.  :func:`audit_package` walks the AST of every module in
the package and reports any *reference* to a transmitting bleak API --
attribute access, bare name, or a dynamic ``getattr`` with a literal name.
Strings inside docstrings and comments are invisible to it, because they are
not references, and a real call is visible however it is spelled.

What is deliberately *not* flagged
----------------------------------
``start_notify`` writes to a Client Characteristic Configuration Descriptor.
That **is** a protocol descriptor write, and this module does not pretend
otherwise -- it is simply a different thing from an application write.  The
CCCD is a per-connection switch belonging to the client; setting it is how a
client says "send me updates", and it cannot carry an application command to
the detector.  Flagging it would be pedantically correct and practically
wrong, since subscribing is what "receive BLE data" means.  The distinction
this project actually cares about -- and the one the assignment draws -- is
between a GATT operation that reads or subscribes, and one that puts
application bytes on the detector's command characteristic.  So descriptor
writes to the CCCD are permitted through :func:`uniden_r8.gatt.assert_notifiable`
and nowhere else, while :func:`write_gatt_descriptor` -- the arbitrary-descriptor
write -- is flagged like any other transmit path.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["TRANSMIT_ATTRS", "Finding", "audit_module", "audit_package"]

#: bleak APIs that put caller-supplied bytes on the peripheral.  ``start_notify``
#: is excluded on purpose; see the module docstring.
TRANSMIT_ATTRS: Final[frozenset[str]] = frozenset(
    {"write_gatt_char", "write_gatt_descriptor", "write_char", "write_without_response"}
)


@dataclass(frozen=True)
class Finding:
    """One reference to a transmitting API."""

    module: str
    line: int
    name: str
    kind: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line} {self.kind} {self.name}"


def audit_module(path: str | Path, *, source: str | None = None) -> list[Finding]:
    """Return every transmit-API reference in one Python file."""
    module_path = Path(path)
    text = source if source is not None else module_path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(module_path))
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in TRANSMIT_ATTRS:
            findings.append(Finding(module_path.name, node.lineno, node.attr, "attribute"))
        elif isinstance(node, ast.Name) and node.id in TRANSMIT_ATTRS:
            findings.append(Finding(module_path.name, node.lineno, node.id, "name"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in TRANSMIT_ATTRS
        ):
            findings.append(
                Finding(module_path.name, node.lineno, node.args[1].value, "getattr")
            )

    return sorted(findings, key=lambda f: (f.module, f.line))


def audit_package(root: str | Path | None = None) -> list[Finding]:
    """Return every transmit-API reference across the whole package."""
    package_root = Path(root) if root is not None else Path(__file__).resolve().parent
    findings: list[Finding] = []
    for module_path in sorted(package_root.rglob("*.py")):
        findings.extend(audit_module(module_path))
    return findings
