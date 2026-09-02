"""Guarded BlueZ pairing for the detector, and nothing else.

This is the first module in the package that runs an external program, and it
is the most dangerous thing here, so the guards come before the feature.

The danger is not the detector. It is that ``bluetoothctl`` on this node can
also reach the **OBDLink MX+**, whose bond the vehicle's telemetry depends on.
A single mistyped ``remove`` would destroy a bond that can only be recreated
by a human standing at the car pressing a button on the adapter. So:

**The protected set is discovered, not configured.** :func:`protected_addresses`
snapshots every device BlueZ already has a *bond* with, before this module does
anything, and every one of them is off limits for the rest of the run. There is
no list to keep up to date and no file to read, so the guard cannot drift away
from reality: a newly bonded adapter is protected the moment it exists.

It is bonds, specifically, and not everything BlueZ has heard from. A discovery
scan puts the detector in BlueZ's device cache, so protecting "known" devices
would protect the very device this module exists to pair with.

**The verb allowlist is small and specific.** :data:`ALLOWED_VERBS` permits
scanning, agent registration, pairing, inspection, untrusting and
disconnecting. It does *not* permit ``remove``, ``trust``, ``power``,
``discoverable`` or ``pairable`` — the first destroys a bond, the second causes
BlueZ to auto-reconnect and fight for the radio, and the rest change adapter
state the OBDLink is relying on.

**Nothing is trusted.** A trusted device is auto-reconnected by BlueZ on every
boot. On a node whose radio is shared with a live RFCOMM link that is a
permanent low-grade competitor for the antenna, and upstream separately reports
that it steals the link from userspace code. The detector is deliberately left
untrusted, and :func:`pair` untrusts it if BlueZ set the flag on its own.

Pairing is still a **persistent state change** to the node's Bluetooth stack.
It is not part of the read-only boundary and never runs by accident: it has its
own CLI subcommand, and that subcommand refuses to act without an explicit
confirmation flag.
"""

from __future__ import annotations

import contextlib
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Final

from .privacy import scrub

__all__ = [
    "ALLOWED_VERBS",
    "DETECTOR_NAME_RE",
    "bonded_devices",
    "bonded_detector_address",
    "FORBIDDEN_VERBS",
    "BLUETOOTHCTL",
    "PairingRefused",
    "PairingResult",
    "normalize_address",
    "is_address",
    "protected_addresses",
    "assert_command_allowed",
    "device_info",
    "pair",
]

BLUETOOTHCTL: Final[str] = "bluetoothctl"

#: The only verbs this module may send.  Everything is an inspection, a
#: bounded discovery, or an operation on the *detector*.
ALLOWED_VERBS: Final[frozenset[str]] = frozenset(
    {"agent", "default-agent", "scan", "pair", "info", "devices", "untrust",
     "disconnect", "exit", "yes"}
)

#: Refused whatever else changes.  Each of these can damage the OBDLink or the
#: adapter state the vehicle depends on.
#:
#: ``remove``       destroys a bond; the OBDLink's can only be recreated by hand
#: ``trust``        makes BlueZ auto-reconnect and compete for the radio
#: ``power``        turning the adapter off drops the RFCOMM link
#: ``discoverable`` / ``pairable``  change how the node answers strangers
#: ``connect``      pairing must not leave a link held; see the module docstring
FORBIDDEN_VERBS: Final[frozenset[str]] = frozenset(
    {"remove", "trust", "power", "discoverable", "pairable", "connect",
     "menu", "advertise", "system-alias", "reset-alias"}
)

_ADDRESS_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$"
)
_DEVICE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*)$"
)


#: An R-series detector's advertised name, as BlueZ records it on the bond.
#: Jeremy's unit bonds as ``R8@`` plus a short fragment; upstream's R8w used
#: ``R8W@``.  Anchored so a device merely *containing* "R8" cannot match.
DETECTOR_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^R[489]W?(?:[@\-_].*)?$", re.IGNORECASE
)


class PairingRefused(PermissionError):
    """Raised when a command would touch a protected device or a banned verb."""


@dataclass
class PairingResult:
    """What happened, in publishable form."""

    paired: bool
    already_paired: bool = False
    trusted: bool = False
    connected: bool = False
    attempts: int = 0
    detail: str = ""
    transcript: list[str] = field(default_factory=list)


def normalize_address(address: str) -> str:
    """Return the canonical uppercase colon form, or raise."""
    if not isinstance(address, str):
        raise PairingRefused(f"address must be a string, got {type(address)!r}")
    canonical = address.strip().upper().replace("-", ":")
    if not _ADDRESS_RE.match(canonical):
        raise PairingRefused(f"refusing {address!r}: not a Bluetooth address")
    return canonical


def is_address(text: str) -> bool:
    try:
        normalize_address(text)
    except PairingRefused:
        return False
    return True


def _run(*args: str, timeout: float = 20.0) -> tuple[int, str]:
    """Run bluetoothctl once.  The only binary this module may execute."""
    if shutil.which(BLUETOOTHCTL) is None:
        raise PairingRefused(f"{BLUETOOTHCTL} is not on PATH")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, validated args
            [BLUETOOTHCTL, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{BLUETOOTHCTL} timed out after {timeout}s"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def protected_addresses() -> set[str]:
    """Every device BlueZ has an existing **bond** with: off limits for this run.

    Bonded, not merely known.  ``bluetoothctl devices`` also lists everything
    the adapter has seen advertise, which after a discovery scan includes the
    detector we are about to pair with — so using it would protect the target
    from being paired at all.  What is precious here is the *bond*: the
    OBDLink's can only be recreated by a person standing at the vehicle
    pressing a button on the adapter, and a merely-discovered device has
    nothing to lose.

    Discovered rather than configured, so it cannot go stale.  If it cannot be
    determined, that is fatal: an empty protected set would silently mean
    "nothing is protected", which is the opposite of safe.
    """
    return {address for address, _ in bonded_devices()}


def bonded_devices() -> list[tuple[str, str]]:
    """Return ``(address, name)`` for every device BlueZ has a bond with.

    Fails closed on **any** nonzero return, including a timeout (124) that
    happened to print something first.  An earlier version tolerated 124 and
    tolerated a nonzero code whenever there was output, which meant a partial
    or timed-out listing could be read as a complete one -- and a short bond
    list is exactly what would let a protected device slip through the guard.
    A truncated answer here is worse than no answer.
    """
    code, out = _run("devices", "Paired")
    if code != 0:
        raise PairingRefused(
            f"bluetoothctl returned {code} while enumerating bonded devices; "
            f"refusing to continue on a possibly incomplete bond list"
        )
    found: list[tuple[str, str]] = []
    for line in out.splitlines():
        match = _DEVICE_LINE_RE.match(line.strip())
        if match:
            found.append((normalize_address(match.group(1)), match.group(2).strip()))
    return found


def bonded_detector_address() -> str:
    """Return the bonded detector's address, resolved from BlueZ's bond state.

    This is how a later phase finds the detector without the address ever being
    written down.  BlueZ already holds it -- it has to, to keep the bond -- so
    reading it back costs no new exposure, whereas caching it in a file would
    create a second copy in a place that can be backed up, synced or read.

    The address returned here lives in process memory only.  Nothing in this
    package serializes it, prints it, or puts it in a report; every published
    form goes through :func:`uniden_r8.privacy.redact_address` first.

    Fail-closed on ambiguity: two bonded detectors is not a situation to guess
    at, and zero means the pairing has gone away and should be noticed.
    """
    detectors = [
        address for address, name in bonded_devices() if DETECTOR_NAME_RE.match(name)
    ]
    if not detectors:
        raise LookupError(
            "no bonded R-series detector; pair one first, or it has been removed"
        )
    if len(detectors) > 1:
        raise LookupError(
            f"{len(detectors)} bonded R-series detectors; refusing to guess which"
        )
    return detectors[0]


def assert_command_allowed(command: str, protected: set[str]) -> str:
    """Return *command* if it is safe to send, else raise.

    Checks, in order: no shell metacharacters or command chaining, a verb on
    the allowlist and off the denylist, and no protected address anywhere in
    the arguments.
    """
    if not isinstance(command, str) or not command.strip():
        raise PairingRefused("refusing an empty bluetoothctl command")
    if any(ch in command for ch in ("\n", "\r", ";", "&", "|", "`", "$", "\x00")):
        raise PairingRefused(f"refusing {command!r}: command chaining is not allowed")

    parts = command.split()
    verb = parts[0].lower()
    if verb in FORBIDDEN_VERBS:
        raise PairingRefused(
            f"refusing {verb!r}: permanently forbidden — it can damage the "
            f"OBDLink bond or the adapter state the vehicle depends on"
        )
    if verb not in ALLOWED_VERBS:
        raise PairingRefused(f"refusing {verb!r}: not on the pairing allowlist")

    for argument in parts[1:]:
        if is_address(argument) and normalize_address(argument) in protected:
            raise PairingRefused(
                "refusing to operate on an already-bonded device: it is "
                "protected, and on this node that is the vehicle's OBD adapter"
            )
    return command


class _Session:
    """A persistent interactive bluetoothctl.

    It has to be persistent.  ``bluetoothctl pair <addr>`` as a one-shot
    registers a pairing agent, sends the request and exits, taking the agent
    with it before the detector has answered; BlueZ reports that as
    ``AuthenticationCanceled``, which reads as though the detector refused.
    """

    def __init__(self, protected: set[str]) -> None:
        if shutil.which(BLUETOOTHCTL) is None:
            raise PairingRefused(f"{BLUETOOTHCTL} is not on PATH")
        self.protected = protected
        self.transcript: list[str] = []
        self.proc = subprocess.Popen(  # noqa: S603 - fixed binary
            [BLUETOOTHCTL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        time.sleep(0.5)

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line.rstrip("\n"))

    def send(self, command: str) -> None:
        assert_command_allowed(command, self.protected)
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass  # the child died; wait_for will time out and say so

    def wait_for(self, needles: tuple[str, ...], timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=0.25)
            except queue.Empty:
                continue
            self.transcript.append(line)
            for needle in needles:
                if needle.lower() in line.lower():
                    return line
        return None

    def drain(self) -> None:
        while True:
            try:
                self.transcript.append(self._lines.get_nowait())
            except queue.Empty:
                return

    def close(self) -> None:
        try:
            self.send("exit")
            self.proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                self.proc.kill()


def device_info(address: str, protected: set[str]) -> dict[str, str]:
    """Parse ``bluetoothctl info <addr>`` into a dict.  Empty if unknown."""
    canonical = normalize_address(address)
    assert_command_allowed(f"info {canonical}", protected)
    code, out = _run("info", canonical)
    if code != 0 or "not available" in out.lower():
        return {}
    info: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, value = line.strip().partition(":")
            info[key.strip()] = value.strip()
    return info


_PAIR_OK: Final[tuple[str, ...]] = ("Pairing successful",)
_PAIR_FAIL: Final[tuple[str, ...]] = ("Failed to pair", "org.bluez.Error", "not available")

#: Prompts BlueZ's agent raises that a person would answer at the terminal.
#: Answering "yes" to a *confirmation* is not a decision this module is making
#: on Jeremy's behalf: the detector has already shown the same code on its own
#: screen, and the human action that authorises the whole exchange is pressing
#: BT Pairing on the unit.  A passkey *entry* prompt is different -- there is
#: nothing to type on a detector -- and is reported rather than guessed at.
_CONFIRM_PROMPTS: Final[tuple[str, ...]] = (
    "Confirm passkey",
    "Request confirmation",
    "Authorize service",
    "Request authorization",
)
_ENTRY_PROMPTS: Final[tuple[str, ...]] = ("Enter PIN code", "Enter passkey")

#: Agent capability.  ``KeyboardDisplay`` is what a phone presents, and the
#: detector is built to pair with phones.  bluetoothctl 5.82 registers an agent
#: at startup, but registering explicitly makes the capability deliberate
#: rather than whatever the default happens to be in a given release.
AGENT_CAPABILITY: Final[str] = "KeyboardDisplay"


def _await_pairing(session: _Session, salt: bytes) -> str | None:
    """Wait for a pairing verdict, answering confirmation prompts on the way.

    BlueZ raises the confirmation through the registered agent mid-exchange; if
    nobody answers it, the pairing dies as ``AuthenticationCanceled`` and looks
    exactly like a refusal by the detector.
    """
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        line = session.wait_for(
            _PAIR_OK + _PAIR_FAIL + _CONFIRM_PROMPTS + _ENTRY_PROMPTS,
            timeout=max(1.0, deadline - time.monotonic()),
        )
        if line is None:
            return None
        lowered = line.lower()
        if any(prompt.lower() in lowered for prompt in _CONFIRM_PROMPTS):
            session.send("yes")
            continue
        if any(prompt.lower() in lowered for prompt in _ENTRY_PROMPTS):
            # There is nothing to type on a radar detector.  Guessing a PIN is
            # not something this module will do.
            return scrub(
                f"BlueZ asked for a passkey to be entered: {line.strip()}", salt
            )
        return line
    return None


def pair(
    address: str,
    salt: bytes,
    *,
    attempts: int = 3,
    discover_seconds: float = 12.0,
) -> PairingResult:
    """Pair with the detector, leaving it bonded, untrusted and disconnected.

    Untrusted, because a trusted device is auto-reconnected by BlueZ forever
    after.  Disconnected, because BlueZ holds the link once pairing completes
    and the detector stops advertising while anything is connected — so the
    next thing that tries to read from it sees a device that does not exist.
    """
    canonical = normalize_address(address)
    protected = protected_addresses()
    if canonical in protected:
        raise PairingRefused(
            "refusing to pair with an already-bonded device; on this node the "
            "only existing bond is the vehicle's OBD adapter"
        )

    result = PairingResult(paired=False)
    session = _Session(protected)
    try:
        session.send(f"agent {AGENT_CAPABILITY}")
        session.send("default-agent")
        time.sleep(1.0)
        session.drain()

        # A single-radio adapter cannot scan and connect at the same time, and
        # BlueZ will not pair with an address it has not seen advertise.
        session.send("scan on")
        seen = session.wait_for((canonical,), timeout=discover_seconds)
        session.send("scan off")
        time.sleep(1.0)
        session.drain()
        if seen is None:
            result.detail = (
                "never saw the detector advertise; it is probably no longer in "
                "BT Pairing mode, or a phone has taken the link"
            )

        for attempt in range(1, attempts + 1):
            result.attempts = attempt
            session.drain()
            session.send(f"pair {canonical}")
            line = _await_pairing(session, salt)

            if line is not None and "pairing successful" in line.lower():
                result.paired = True
                break
            if line is None:
                result.detail = "no response from BlueZ in 60s"
            elif "authenticationcanceled" in line.lower().replace(" ", ""):
                result.detail = (
                    "AuthenticationCanceled — the detector refused. It is "
                    "almost certainly not in BT Pairing mode right now: it "
                    "keeps advertising while unpaired, so being visible is "
                    "not the same as being armed."
                )
            elif "authenticationfailed" in line.lower().replace(" ", ""):
                # Upstream reports this on a first attempt, twice, months
                # apart, with an identical retry succeeding.  Retry it.
                result.detail = "AuthenticationFailed (common on a first attempt)"
            else:
                result.detail = scrub(line.strip(), salt)
            time.sleep(2.0)

        info = device_info(canonical, protected)
        result.trusted = info.get("Trusted") == "yes"
        if result.trusted:
            session.send(f"untrust {canonical}")
            time.sleep(0.5)
            result.trusted = device_info(canonical, protected).get("Trusted") == "yes"

        if device_info(canonical, protected).get("Connected") == "yes":
            session.send(f"disconnect {canonical}")
            session.wait_for(("Disconnection successful", "Failed to disconnect"), 15.0)
        result.connected = device_info(canonical, protected).get("Connected") == "yes"
    finally:
        session.close()
        result.transcript = [scrub(line, salt) for line in session.transcript[-40:]]

    return result
