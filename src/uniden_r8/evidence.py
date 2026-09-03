"""The private evidence store, and the boundary around it.

Raw BLE evidence is useful and dangerous in the same breath.  A scan result
carries Bluetooth addresses; a POI read would carry saved coordinates -- home,
work, and the places Jeremy drives often.  None of that may reach the
repository, a terminal transcript, or a report handed to anyone.

So there are exactly two destinations for anything this project observes:

``PrivateStore``
    A ``0700`` directory holding ``0600`` files.  Raw material goes here and
    nowhere else.  This class does not try to verify that the path it was
    given is git-ignored -- a check the caller can defeat by passing a
    different path is a false comfort -- so the default location is
    ``.private/``, ``.gitignore`` excludes it, and a test asserts that.

``publish``
    Sanitized output.  :func:`publish` re-checks the rendered text with
    :func:`uniden_r8.privacy.looks_like_identifier` and raises rather than
    return a string with an address in it.  The check is redundant when the
    caller has already tokenised properly, which is the point: it turns "we
    were careful" into "it cannot happen".

The permission handling is deliberate rather than incidental.  ``mkdir`` and
``open`` both apply the process umask, so a store created under a lax umask
would be world-readable at the instant of creation and only tightened
afterwards.  Every write here closes the umask for the window instead.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .privacy import load_or_create_salt, looks_like_identifier, looks_like_position

__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "PrivateStore",
    "PublicationRefused",
    "publish",
    "utc_stamp",
    "utc_stamp_ms",
    "iso_from_wall_ns",
]

#: Owner-only.  A group-readable evidence directory on a shared machine is
#: the same leak as a committed one, arriving more slowly.
DIR_MODE: Final[int] = 0o700
FILE_MODE: Final[int] = 0o600


class PublicationRefused(RuntimeError):
    """Raised when text bound for a public destination still has an identifier."""


def utc_stamp() -> str:
    """Return an ISO-8601 UTC timestamp, second resolution."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp_ms() -> str:
    """Return an ISO-8601 UTC timestamp with milliseconds.

    Second resolution was enough while the only timestamps were "when did this
    scan run".  It is not enough for alert transitions: a threat that appears
    and is muted inside the same second produces two events that cannot be
    ordered by their stamps, and ordering is the whole point of an event log.
    Sequence numbers carry the real ordering; this makes the stamps agree with
    them instead of quietly contradicting them.
    """
    return iso_from_wall_ns(time.time_ns())


def iso_from_wall_ns(wall_ns: int) -> str:
    """Format a nanosecond wall-clock reading as millisecond ISO-8601 UTC.

    Takes the reading rather than calling the clock so that a record stamped in
    a BLE callback and the text written for it hours later describe the same
    instant.  See :mod:`uniden_r8.events` for why the two clocks are separate.
    """
    moment = datetime.fromtimestamp(wall_ns / 1e9, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class PrivateStore:
    """A ``0700`` directory of ``0600`` files, with a per-store salt.

    The salt lives inside the store because it is exactly as sensitive as the
    raw evidence: with the salt, every token in a published report becomes a
    lookup against the 48-bit address space.  Keeping them together means one
    permission mistake is not two separate ones.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._salt: bytes | None = None

    # ------------------------------------------------------------ lifecycle

    def ensure(self) -> PrivateStore:
        """Create the store if absent and tighten it if it already exists.

        Tightening rather than trusting: a directory left ``0755`` by an
        earlier run, or by a hand-typed ``mkdir``, is corrected here instead
        of being taken as evidence that ``0755`` was intended.
        """
        self.root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(self.root, DIR_MODE)
        for child in self.root.iterdir():
            os.chmod(child, DIR_MODE if child.is_dir() else FILE_MODE)
        return self

    @property
    def salt(self) -> bytes:
        """The per-store redaction salt, created on first use."""
        if self._salt is None:
            self.ensure()
            self._salt = load_or_create_salt(self.root / "redaction.salt")
        return self._salt

    # ---------------------------------------------------------------- write

    def path(self, name: str) -> Path:
        """Return the path for *name*, refusing anything that escapes the store."""
        if not name or name != Path(name).name:
            raise ValueError(
                f"refusing {name!r}: evidence names are single path components"
            )
        return self.root / name

    def write_text(self, name: str, text: str) -> Path:
        """Write *text* into the store, ``0600``."""
        self.ensure()
        target = self.path(name)
        previous_umask = os.umask(0o077)
        try:
            target.write_text(text, encoding="utf-8")
        finally:
            os.umask(previous_umask)
        os.chmod(target, FILE_MODE)
        return target

    def write_json(self, name: str, payload: Any) -> Path:
        """Write *payload* as pretty JSON into the store, ``0600``."""
        return self.write_text(name, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def read_json(self, name: str) -> Any:
        """Read a document back out of the store.

        The counterpart to :meth:`write_json`, and the only supported way to
        get a capture back: it resolves *name* through :meth:`path`, so a name
        carrying ``..`` or an absolute path cannot reach outside the store.

        What comes back may contain device bytes -- that is the whole reason
        the store is ``0700``.  Everything downstream of this call is
        responsible for publishing structure rather than content.
        """
        return json.loads(self.path(name).read_text(encoding="utf-8"))

    # ----------------------------------------------------------- inspection

    def audit(self) -> list[tuple[str, int]]:
        """Return ``(name, mode)`` for everything in the store.

        Used by the runbook and the tests to prove the boundary holds, rather
        than asserting that it was set up correctly once.
        """
        if not self.root.exists():
            return []
        return sorted(
            (child.name, child.stat().st_mode & 0o777) for child in self.root.iterdir()
        )

    def is_sealed(self) -> bool:
        """Return ``True`` if the store and everything in it are owner-only."""
        if not self.root.exists():
            return False
        if self.root.stat().st_mode & 0o777 != DIR_MODE:
            return False
        for name, mode in self.audit():
            expected = DIR_MODE if (self.root / name).is_dir() else FILE_MODE
            if mode != expected:
                return False
        return True


def publish(text: str) -> str:
    """Return *text* if it is safe to print or commit, else raise.

    The last gate before anything leaves the private side.  It does not
    sanitize -- sanitizing here would hide the bug that produced an
    unsanitized string in the first place -- it refuses.

    Two questions are asked, not one.  The original was "does this still
    contain an address".  The second, "does this contain somewhere the vehicle
    has been", was added when an external GNSS source made a latitude possible
    for the first time: a gate that refused a MAC address while printing a
    coordinate would be defending the wrong thing.  Structured documents are
    decoded and walked by key so that ``{"lat": 33.4}`` is caught and
    ``{"voltage": 33.4}`` is not.
    """
    if looks_like_identifier(text):
        raise PublicationRefused(
            "refusing to publish: the text still contains a Bluetooth or host "
            "address.  Tokenise it with uniden_r8.privacy first; see "
            "docs/SAFETY.md."
        )
    if looks_like_position(_decoded(text)):
        raise PublicationRefused(
            "refusing to publish: the text contains a position.  Coordinates "
            "and the detector's own heading, speed and altitude belong in the "
            "owner-only state directory or the private store, never in "
            "printed or committed output; see docs/SAFETY.md."
        )
    return text


def _decoded(text: str) -> object:
    """Return *text* parsed as JSON if it is JSON, else the text itself.

    The position check is about meaning, and meaning lives in the keys.  A JSON
    document checked as a flat string would either miss ``"lat": 33.4`` or
    have to guess at every bare number in it.
    """
    stripped = text.lstrip()
    if stripped[:1] not in {"{", "["}:
        return text
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text
