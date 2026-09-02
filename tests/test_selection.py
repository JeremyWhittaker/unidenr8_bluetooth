"""Candidate selection and bond resolution.

Two ways the detector gets found, and both must refuse ambiguity rather than
guess, and must never let a raw address reach output.
"""

from __future__ import annotations

import asyncio

import pytest

from fixtures import PUBLIC_ADDRESS, RANDOM_STATIC, bt_address
from uniden_r8 import cli, discovery, pairing
from uniden_r8.evidence import PrivateStore
from uniden_r8.privacy import looks_like_identifier

SALT = b"\xab" * 32
FRAGMENT = RANDOM_STATIC.replace(":", "")[-4:]


class Device:
    def __init__(self, address, name):
        self.address = address
        self.name = name
        self.rssi = -60


def _fake_discover(devices):
    async def run(timeout):
        return list(devices)

    return run


# ------------------------------------------------------------- selection

def test_exactly_one_strong_candidate_is_selected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        discovery, "_discover",
        _fake_discover([Device(RANDOM_STATIC, f"R8@{FRAGMENT}"),
                        Device(PUBLIC_ADDRESS, "Someone's Watch")]),
    )
    store = PrivateStore(tmp_path / "private").ensure()
    candidate, total = asyncio.run(cli._find_one_strong(store, 5.0))
    assert candidate.address == RANDOM_STATIC
    assert total == 2


def test_two_strong_candidates_are_refused_not_guessed_at(tmp_path, monkeypatch):
    monkeypatch.setattr(
        discovery, "_discover",
        _fake_discover([
            Device(RANDOM_STATIC, f"R8@{FRAGMENT}"),
            Device(bt_address(0xE0, 1, 2, 3, 4, 5), "R8W@0405"),
        ]),
    )
    store = PrivateStore(tmp_path / "private").ensure()
    with pytest.raises(LookupError, match="refusing to guess"):
        asyncio.run(cli._find_one_strong(store, 5.0))


def test_no_candidate_is_an_explained_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        discovery, "_discover", _fake_discover([Device(PUBLIC_ADDRESS, "Watch")])
    )
    store = PrivateStore(tmp_path / "private").ensure()
    with pytest.raises(LookupError, match="BT Pairing"):
        asyncio.run(cli._find_one_strong(store, 5.0))


def test_the_ambiguity_message_carries_no_address(tmp_path, monkeypatch):
    monkeypatch.setattr(
        discovery, "_discover",
        _fake_discover([
            Device(RANDOM_STATIC, f"R8@{FRAGMENT}"),
            Device(bt_address(0xE0, 1, 2, 3, 4, 5), "R8W@0405"),
        ]),
    )
    store = PrivateStore(tmp_path / "private").ensure()
    try:
        asyncio.run(cli._find_one_strong(store, 5.0))
    except LookupError as exc:
        assert not looks_like_identifier(str(exc))
    else:  # pragma: no cover
        pytest.fail("expected a refusal")


# -------------------------------------------------- no address is serialized

def test_no_module_writes_an_address_to_disk():
    """The raw address may live in memory and BlueZ's bond state only."""
    from pathlib import Path

    root = Path(cli.__file__).resolve().parent
    for module in root.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "detector-address" not in text, f"{module.name} caches an address"
        assert "ADDRESS_CACHE" not in text, f"{module.name} caches an address"


def test_the_private_store_holds_no_address_after_a_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(
        discovery, "_discover",
        _fake_discover([Device(RANDOM_STATIC, f"R8@{FRAGMENT}")]),
    )
    store_path = tmp_path / "private"
    assert cli.main(["--store", str(store_path), "scan", "-s", "3",
                     "--save", "scan.json"]) == 0
    for path in store_path.iterdir():
        if path.suffix in (".json", ".txt"):
            assert not looks_like_identifier(path.read_text(encoding="utf-8")), path


# --------------------------------------------------------- bond resolution

def test_the_bonded_detector_is_resolved_from_bluez(monkeypatch):
    monkeypatch.setattr(
        pairing, "_run",
        lambda *a, timeout=20.0: (
            0,
            f"Device {PUBLIC_ADDRESS} OBDLink MX+ 56122\n"
            f"Device {RANDOM_STATIC} R8@{FRAGMENT}\n",
        ),
    )
    assert pairing.bonded_detector_address() == RANDOM_STATIC


def test_no_bonded_detector_is_an_explained_lookup_error(monkeypatch):
    monkeypatch.setattr(
        pairing, "_run",
        lambda *a, timeout=20.0: (0, f"Device {PUBLIC_ADDRESS} OBDLink MX+ 56122\n"),
    )
    with pytest.raises(LookupError, match="no bonded R-series detector"):
        pairing.bonded_detector_address()


def test_two_bonded_detectors_are_refused(monkeypatch):
    monkeypatch.setattr(
        pairing, "_run",
        lambda *a, timeout=20.0: (
            0,
            f"Device {RANDOM_STATIC} R8@{FRAGMENT}\n"
            f"Device {bt_address(0xE0, 1, 2, 3, 4, 5)} R8W@0405\n",
        ),
    )
    with pytest.raises(LookupError, match="refusing to guess"):
        pairing.bonded_detector_address()


def test_the_obdlink_is_never_mistaken_for_a_detector():
    assert not pairing.DETECTOR_NAME_RE.match("OBDLink MX+ 56122")
    assert not pairing.DETECTOR_NAME_RE.match("My R8 Speaker")


@pytest.mark.parametrize("name", ["R8@A7", "R8W@23D4", "R4-0001", "R9W", "r8@ab"])
def test_detector_names_match(name):
    assert pairing.DETECTOR_NAME_RE.match(name)
