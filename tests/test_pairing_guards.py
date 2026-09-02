"""The pairing guards.

Pairing is the only thing this project does that changes persistent state, and
``bluetoothctl`` can reach the OBDLink as easily as the detector. These tests
are about the OBDLink, not the detector.
"""

from __future__ import annotations

import pytest

from fixtures import PUBLIC_ADDRESS, RANDOM_STATIC, bt_address
from uniden_r8 import pairing

SALT = b"\x77" * 32

#: Stand-in for the OBDLink: a device that already has a bond.
BONDED = pairing.normalize_address(PUBLIC_ADDRESS)
DETECTOR = pairing.normalize_address(RANDOM_STATIC)
PROTECTED = {BONDED}


# ------------------------------------------------------------- the verbs

@pytest.mark.parametrize("verb", sorted(pairing.FORBIDDEN_VERBS))
def test_every_forbidden_verb_is_refused(verb):
    with pytest.raises(pairing.PairingRefused):
        pairing.assert_command_allowed(f"{verb} {DETECTOR}", PROTECTED)


def test_remove_is_forbidden_even_for_the_detector():
    """`remove` destroys a bond, and the guard must not depend on the target."""
    with pytest.raises(pairing.PairingRefused):
        pairing.assert_command_allowed(f"remove {DETECTOR}", PROTECTED)


def test_trust_is_forbidden():
    """A trusted device is auto-reconnected forever and competes for the radio."""
    assert "trust" in pairing.FORBIDDEN_VERBS
    with pytest.raises(pairing.PairingRefused):
        pairing.assert_command_allowed(f"trust {DETECTOR}", PROTECTED)


def test_power_is_forbidden():
    """Powering the adapter down would drop the vehicle's RFCOMM link."""
    with pytest.raises(pairing.PairingRefused):
        pairing.assert_command_allowed("power off", PROTECTED)


def test_untrust_is_allowed_because_it_is_the_safe_direction():
    assert pairing.assert_command_allowed(f"untrust {DETECTOR}", PROTECTED)


def test_an_unknown_verb_is_refused_not_passed_through():
    with pytest.raises(pairing.PairingRefused):
        pairing.assert_command_allowed("shutdown now", PROTECTED)


def test_the_allowlist_and_denylist_do_not_overlap():
    assert not (pairing.ALLOWED_VERBS & pairing.FORBIDDEN_VERBS)


# --------------------------------------------------------- protected devices

def test_a_bonded_address_is_refused_as_an_argument():
    with pytest.raises(pairing.PairingRefused):
        pairing.assert_command_allowed(f"pair {BONDED}", PROTECTED)


def test_a_bonded_address_is_refused_however_it_is_spelled():
    for spelling in (BONDED.lower(), BONDED.replace(":", "-"), f"  {BONDED}  "):
        with pytest.raises(pairing.PairingRefused):
            pairing.assert_command_allowed(f"info {spelling}", PROTECTED)


def test_the_detector_is_not_protected():
    assert pairing.assert_command_allowed(f"pair {DETECTOR}", PROTECTED)


def test_pair_refuses_a_target_that_already_has_a_bond(monkeypatch):
    monkeypatch.setattr(pairing, "protected_addresses", lambda: {DETECTOR})
    with pytest.raises(pairing.PairingRefused):
        pairing.pair(DETECTOR, SALT)


# ------------------------------------------------------------- injection

@pytest.mark.parametrize("payload", [
    "pair X; remove Y",
    "pair X && power off",
    "pair X\nremove Y",
    "pair X | sh",
    "pair `remove Y`",
    "pair $(remove Y)",
])
def test_command_chaining_is_refused(payload):
    with pytest.raises(pairing.PairingRefused):
        pairing.assert_command_allowed(payload, PROTECTED)


def test_an_empty_command_is_refused():
    with pytest.raises(pairing.PairingRefused):
        pairing.assert_command_allowed("   ", PROTECTED)


# --------------------------------------------------------- protected set

def test_the_protected_set_is_bonds_not_everything_seen(monkeypatch):
    """A discovery scan puts the detector in BlueZ's cache.

    Protecting everything BlueZ *knows* would protect the very device this
    module exists to pair with -- which is exactly the bug this test pins.
    """
    calls: list[tuple[str, ...]] = []

    def fake_run(*args, timeout=20.0):
        calls.append(args)
        return 0, f"Device {BONDED} OBDLink MX+\n"

    monkeypatch.setattr(pairing, "_run", fake_run)
    assert pairing.protected_addresses() == {BONDED}
    assert calls == [("devices", "Paired")], "must ask for bonds, not all devices"


def test_an_unreadable_bond_list_is_fatal_not_empty(monkeypatch):
    """An empty protected set would silently mean 'nothing is protected'."""
    monkeypatch.setattr(pairing, "_run", lambda *a, timeout=20.0: (1, ""))
    with pytest.raises(pairing.PairingRefused):
        pairing.protected_addresses()


# ------------------------------------------------------------- addresses

@pytest.mark.parametrize("bad", ["", "not-an-address", "00:11:22:33:44", "zz:11:22:33:44:55"])
def test_a_malformed_address_is_refused(bad):
    with pytest.raises(pairing.PairingRefused):
        pairing.normalize_address(bad)


def test_addresses_normalise_to_one_form():
    canonical = bt_address(0xE0, 0, 0, 0, 0x23, 0xD4)
    assert pairing.normalize_address(canonical.lower()) == canonical
    assert pairing.normalize_address(canonical.replace(":", "-")) == canonical


def test_only_bluetoothctl_may_be_executed():
    assert pairing.BLUETOOTHCTL == "bluetoothctl"


def test_a_passkey_entry_prompt_is_reported_not_guessed():
    """There is nothing to type on a radar detector; guessing a PIN is not ok."""
    assert pairing._ENTRY_PROMPTS
    assert not set(pairing._ENTRY_PROMPTS) & set(pairing._CONFIRM_PROMPTS)


# ------------------------------------------ correction 8: fail closed always

@pytest.mark.parametrize("code,out", [
    (1, ""),
    (1, f"Device {BONDED} OBDLink MX+\n"),      # nonzero WITH output
    (124, "bluetoothctl timed out after 20.0s"),  # timeout WITH diagnostics
    (124, ""),
    (127, "bluetoothctl: not found"),
])
def test_any_nonzero_bluetoothctl_return_fails_closed(monkeypatch, code, out):
    """A truncated bond list is worse than no bond list.

    The earlier version tolerated 124, and tolerated any nonzero code that
    happened to print something.  Either could hand back a short list, and a
    short list is exactly how a protected device slips past the guard.
    """
    monkeypatch.setattr(pairing, "_run", lambda *a, timeout=20.0: (code, out))
    with pytest.raises(pairing.PairingRefused):
        pairing.bonded_devices()
    with pytest.raises(pairing.PairingRefused):
        pairing.protected_addresses()


def test_a_clean_zero_return_is_accepted(monkeypatch):
    monkeypatch.setattr(
        pairing, "_run",
        lambda *a, timeout=20.0: (0, f"Device {BONDED} OBDLink MX+ 56122\n"),
    )
    assert pairing.bonded_devices() == [(BONDED, "OBDLink MX+ 56122")]


def test_an_empty_but_successful_listing_is_accepted(monkeypatch):
    """No bonds at all is a legitimate answer, distinct from a failed query."""
    monkeypatch.setattr(pairing, "_run", lambda *a, timeout=20.0: (0, ""))
    assert pairing.bonded_devices() == []
    assert pairing.protected_addresses() == set()
