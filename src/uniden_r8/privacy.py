"""Identifier redaction.

Everything this project observes over the air is, or contains, a device
identifier: Bluetooth addresses, advertised names with an address fragment
appended (the R8w advertises as ``R8W@xx``), and the host address of the node
doing the listening.  None of it may reach a log line, a test fixture, a
document, a commit, or a status summary.

The rule this module implements is *tokenise, do not drop*.  A scan report
that says "3 devices, all unnamed" is useless for deciding whether the
detector answered; a report that says "candidate ble:9f3c1a2b4d5e appeared in
both scans" is useful and still carries nothing an attacker or a reader can
turn back into an address.  So every identifier becomes a salted, truncated
SHA-256 token that is:

* **stable** within one installation, so two scans can be correlated;
* **useless** across installations, because the salt is per-install and lives
  chmod-600 next to the raw evidence it protects;
* **irreversible in practice**, because the 48-bit address space that a bare
  hash would leave brute-forceable is salted with 256 bits of urandom.

:func:`scrub` is the belt-and-braces pass.  Structured code paths call
:func:`token` deliberately; ``scrub`` exists so that free text which was never
supposed to contain an address -- an exception message from BlueZ, a
subprocess transcript -- cannot leak one by accident.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path
from typing import Final

__all__ = [
    "SALT_BYTES",
    "TOKEN_HEX_LEN",
    "MAC_RE",
    "IPV4_RE",
    "load_or_create_salt",
    "token",
    "redact_address",
    "redact_name",
    "scrub",
    "looks_like_identifier",
]

#: 256 bits.  A Bluetooth address is 48 bits, which is trivially enumerable
#: against an unsalted hash; the salt is what makes the token one-way in
#: practice rather than only in principle.
SALT_BYTES: Final[int] = 32

#: 12 hex characters (48 bits) of the digest.  Long enough that two observed
#: devices will not collide, short enough to read in a log line.
TOKEN_HEX_LEN: Final[int] = 12

#: Colon- or dash-separated six-octet address, the form BlueZ, bleak and
#: ``bluetoothctl`` all print.
MAC_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"
)

#: Host addresses are not Bluetooth identifiers, but they identify the node
#: and this project's evidence is gathered over SSH, so they are scrubbed too.
IPV4_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
)

#: The R8w appends an address fragment to its advertised name, so the name is
#: itself partly an identifier.  The model prefix is the part worth keeping.
_NAME_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z0-9]{1,12})([@\-_].*)$")


def load_or_create_salt(path: str | os.PathLike[str]) -> bytes:
    """Return the per-install salt at *path*, creating it if absent.

    The file is created ``0600`` and its parent ``0700``.  An existing file
    with looser permissions is tightened rather than trusted: a salt another
    account can read is a salt that makes every token reversible by anyone who
    can also see the redacted output.
    """
    salt_path = Path(path)
    salt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(salt_path.parent, 0o700)

    if salt_path.exists():
        os.chmod(salt_path, 0o600)
        salt = salt_path.read_bytes()
        if len(salt) >= SALT_BYTES:
            return salt
        # A truncated salt file is corruption, not a shorter salt.  Replacing
        # it changes every token, which is safe; keeping it would weaken them.

    salt = os.urandom(SALT_BYTES)
    # O_EXCL would refuse the rewrite above, so create then tighten, with the
    # umask closed for the window in between.
    previous_umask = os.umask(0o077)
    try:
        salt_path.write_bytes(salt)
    finally:
        os.umask(previous_umask)
    os.chmod(salt_path, 0o600)
    return salt


def token(value: str, salt: bytes, *, prefix: str = "ble") -> str:
    """Return a stable, salted, non-reversible token for *value*.

    Normalisation is case- and separator-insensitive so that ``E0:00:...``,
    ``e0-00-...`` and ``e000...`` all produce one token: BlueZ, bleak and
    ``bluetoothctl`` do not agree on the spelling of an address, and a report
    that showed the same device three times would be a bug, not privacy.
    """
    if not isinstance(value, str):
        raise TypeError(f"token() takes a string, got {type(value)!r}")
    if not salt:
        raise ValueError("refusing to tokenise with an empty salt")
    normalised = re.sub(r"[^0-9a-z]", "", value.lower())
    digest = hmac.new(salt, normalised.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{prefix}:{digest[:TOKEN_HEX_LEN]}"


def redact_address(address: str, salt: bytes) -> str:
    """Tokenise a Bluetooth address."""
    return token(address, salt, prefix="ble")


def redact_name(name: str | None, salt: bytes) -> str:
    """Return an advertised name with any identifier fragment tokenised.

    ``R8W@23D4`` becomes ``R8W@<nam:...>``: the model prefix is the whole
    reason the name is interesting, and the fragment after it is derived from
    the address.  A name with no separator is kept only if it is short and
    alphanumeric; anything else is tokenised whole, because a free-form name
    can carry a serial, an owner's name, or a phone model.
    """
    if not name:
        return "<unnamed>"
    stripped = name.strip()
    if not stripped:
        return "<unnamed>"

    match = _NAME_SPLIT_RE.match(stripped)
    if match:
        prefix, remainder = match.group(1), match.group(2)
        return f"{prefix}{remainder[0]}{token(remainder[1:], salt, prefix='nam')}"

    if stripped.isalnum() and len(stripped) <= 12:
        return stripped
    return token(stripped, salt, prefix="nam")


def scrub(text: str, salt: bytes) -> str:
    """Replace every identifier pattern in free text with a token.

    This is the last line of defence, not the first.  Structured output should
    already be tokenised; ``scrub`` catches the strings this project did not
    author -- BlueZ errors, tracebacks, command transcripts -- before they are
    printed or stored outside the private directory.
    """
    if not isinstance(text, str):
        raise TypeError(f"scrub() takes a string, got {type(text)!r}")
    text = MAC_RE.sub(lambda m: token(m.group(0), salt, prefix="ble"), text)
    return IPV4_RE.sub("<host-redacted>", text)


def looks_like_identifier(text: str) -> bool:
    """Return ``True`` if *text* still contains a raw address.

    Used by the tests, and by :mod:`uniden_r8.evidence` before anything is
    written outside the private directory.  It answers "would publishing this
    leak an address", which is the only question that matters at that boundary.
    """
    return bool(MAC_RE.search(text) or IPV4_RE.search(text))
