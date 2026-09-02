"""The bounded scan: timeouts, classification, and sanitized reporting.

No radio and no bleak.  The scanner is a substituted coroutine, which is the
whole reason :func:`uniden_r8.discovery.scan` takes one.
"""

from __future__ import annotations

import asyncio

import pytest

from fixtures import PUBLIC_ADDRESS, RANDOM_STATIC, RESOLVABLE_PRIVATE, bt_address
from uniden_r8 import discovery, gatt, privacy

SALT = b"\x55" * 32
SAMPLE = RANDOM_STATIC
FRAGMENT = SAMPLE.replace(":", "")[-4:]


class FakeDevice:
    """Shaped like a bleak ``BLEDevice``, without importing bleak."""

    def __init__(self, address, name=None, rssi=None):
        self.address = address
        self.name = name
        self.rssi = rssi


def _scanner(devices, *, delay=0.0, record=None):
    async def run(timeout):
        if record is not None:
            record.append(timeout)
        if delay:
            await asyncio.sleep(delay)
        return devices

    return run


# ------------------------------------------------------------- boundedness

@pytest.mark.parametrize("requested,expected", [
    (20.0, 20.0),
    (0.0, discovery.MIN_SCAN_SECONDS),
    (-99.0, discovery.MIN_SCAN_SECONDS),
    (10_000.0, discovery.MAX_SCAN_SECONDS),
    (None, discovery.DEFAULT_SCAN_SECONDS),
    ("not a number", discovery.DEFAULT_SCAN_SECONDS),
    (float("nan"), discovery.DEFAULT_SCAN_SECONDS),
    (float("inf"), discovery.MAX_SCAN_SECONDS),
])
def test_the_scan_window_is_always_clamped(requested, expected):
    assert discovery.bounded_seconds(requested) == expected


def test_the_shipped_bounds_are_sane():
    """The constants themselves, not just the clamping logic."""
    assert 0 < discovery.MIN_SCAN_SECONDS <= discovery.DEFAULT_SCAN_SECONDS
    assert discovery.DEFAULT_SCAN_SECONDS <= discovery.MAX_SCAN_SECONDS
    assert discovery.MAX_SCAN_SECONDS <= 60.0, "a longer window shares the radio badly"
    assert discovery.SCAN_GRACE_SECONDS > 0


def test_no_input_can_produce_an_unbounded_window():
    for value in (None, 0, -1, 1e9, float("inf"), "x", [], {}):
        window = discovery.bounded_seconds(value)  # type: ignore[arg-type]
        assert discovery.MIN_SCAN_SECONDS <= window <= discovery.MAX_SCAN_SECONDS


def test_the_clamped_window_is_what_reaches_the_scanner():
    """A bound the scanner never hears about is not a bound."""
    seen: list[float] = []
    report = asyncio.run(discovery.scan(10_000.0, SALT, scanner=_scanner([], record=seen)))
    assert seen == [discovery.MAX_SCAN_SECONDS]
    assert report.requested_seconds == discovery.MAX_SCAN_SECONDS


def test_a_scanner_that_never_returns_is_cancelled_not_awaited(monkeypatch):
    """The outer ceiling is what protects the shared radio from a hung scan.

    Without it a scanner coroutine that never completes would hold an LE
    discovery session open for the life of the process, on the same radio the
    vehicle's OBDLink link is using.
    """
    monkeypatch.setattr(discovery, "SCAN_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(discovery, "MIN_SCAN_SECONDS", 0.05)
    report = asyncio.run(
        discovery.scan(discovery.MIN_SCAN_SECONDS, SALT, scanner=_scanner([], delay=30.0))
    )
    assert report.timed_out is True
    assert report.total_seen == 0


def test_a_hung_scan_still_produces_a_report(monkeypatch):
    """'Nothing answered' and 'the radio wedged' are different findings."""
    monkeypatch.setattr(discovery, "SCAN_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(discovery, "MIN_SCAN_SECONDS", 0.05)
    report = asyncio.run(
        discovery.scan(discovery.MIN_SCAN_SECONDS, SALT, scanner=_scanner([], delay=10.0))
    )
    assert report.timed_out and report.candidates == []


def test_the_ceiling_actually_fires_rather_than_being_merely_configured(monkeypatch):
    """A timeout that is never reached is not evidence of a bound."""
    import time

    monkeypatch.setattr(discovery, "SCAN_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(discovery, "MIN_SCAN_SECONDS", 0.05)
    started = time.monotonic()
    asyncio.run(
        discovery.scan(discovery.MIN_SCAN_SECONDS, SALT, scanner=_scanner([], delay=30.0))
    )
    assert time.monotonic() - started < 1.0


def test_scanning_without_a_salt_is_refused():
    with pytest.raises(ValueError):
        asyncio.run(discovery.scan(5.0, b"", scanner=_scanner([])))


# ---------------------------------------------------------- classification

@pytest.mark.parametrize("name,tier", [
    ("R8W@ABCD", "strong"),
    ("R8@ABCD", "strong"),
    ("r8w@abcd", "strong"),
    ("R8", "strong"),
    ("R9W@0001", "strong"),
    ("R4-1234", "strong"),
    ("Uniden Thing", "possible"),
    ("R/TACH bridge", "possible"),
    ("OBDLink MX+ 56122", "other"),
    ("Some Headphones", "other"),
    (None, "unnamed"),
    ("", "unnamed"),
    ("   ", "unnamed"),
])
def test_classification(name, tier):
    assert discovery.classify(name) == tier


def test_the_obdlink_is_never_a_candidate():
    """Belt and braces: it is BR/EDR and cannot appear in an LE scan anyway."""
    assert discovery.classify("OBDLink MX+ 56122") == "other"
    report = discovery.summarise(
        [discovery.Advertisement(address=PUBLIC_ADDRESS, name="OBDLink MX+ 56122")],
        SALT,
    )
    assert report.total_seen == 1
    assert report.candidates == []


@pytest.mark.parametrize("address,expected", [
    (RANDOM_STATIC, True),                             # top two bits set
    (bt_address(0xC0, 0x11, 0x22, 0x33, 0x44, 0x55), True),
    (PUBLIC_ADDRESS, False),
    (RESOLVABLE_PRIVATE, False),
    ("", False),
    ("zz", False),
])
def test_random_static_address_detection(address, expected):
    assert discovery.is_random_static(address) is expected


# --------------------------------------------------------------- reporting

def test_a_report_never_carries_an_address():
    report = discovery.summarise(
        [
            discovery.Advertisement(address=SAMPLE, name=f"R8W@{FRAGMENT}", rssi=-55),
            discovery.Advertisement(address=PUBLIC_ADDRESS, name="OBDLink MX+ 56122"),
            discovery.Advertisement(
                address=bt_address(0xC0, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE), name=None, rssi=-80
            ),
        ],
        SALT,
    )
    rendered = repr(report.as_dict()) + "\n".join(c.line() for c in report.candidates)
    assert not privacy.looks_like_identifier(rendered)
    assert SAMPLE not in rendered
    assert FRAGMENT not in rendered


def test_every_device_seen_is_counted_even_when_not_listed():
    """A report that hid the uninteresting devices would misstate the scan."""
    report = discovery.summarise(
        [
            discovery.Advertisement(
                address=bt_address(0x00, 0x04, 0x3E, 0x11, 0x22, n), name="Headphones"
            )
            for n in range(5)
        ],
        SALT,
    )
    assert report.total_seen == 5
    assert report.candidates == []


def test_uninteresting_devices_are_not_enumerated():
    """Listing the neighbours' phones would be a privacy leak of its own."""
    report = discovery.summarise(
        [discovery.Advertisement(address=PUBLIC_ADDRESS, name="Someone's Watch")],
        SALT,
    )
    assert report.candidates == []


def test_an_unnamed_random_static_device_is_surfaced_as_a_hint():
    report = discovery.summarise(
        [
            discovery.Advertisement(
                address=bt_address(0xE0, 0x11, 0x22, 0x33, 0x44, 0x55), name=None, rssi=-70
            )
        ],
        SALT,
    )
    assert [c.tier for c in report.candidates] == ["unnamed"]
    assert report.candidates[0].random_static is True


def test_an_unnamed_public_device_is_not_surfaced():
    report = discovery.summarise(
        [discovery.Advertisement(address=PUBLIC_ADDRESS, name=None)], SALT
    )
    assert report.candidates == []


def test_strong_candidates_sort_before_possible_and_unnamed():
    report = discovery.summarise(
        [
            discovery.Advertisement(
                address=bt_address(0xE0, 0, 0, 0, 0, 1), name=None, rssi=-40
            ),
            discovery.Advertisement(
                address=bt_address(0xE0, 0, 0, 0, 0, 2), name="Uniden gadget", rssi=-90
            ),
            discovery.Advertisement(
                address=bt_address(0xE0, 0, 0, 0, 0, 3), name="R8@0003", rssi=-95
            ),
        ],
        SALT,
    )
    assert [c.tier for c in report.candidates] == ["strong", "possible", "unnamed"]


def test_the_same_device_tokenises_the_same_way_across_two_scans():
    """So a second scan can be compared with the first without an address."""
    advertisement = discovery.Advertisement(address=SAMPLE, name=f"R8W@{FRAGMENT}")
    first = discovery.summarise([advertisement], SALT)
    second = discovery.summarise([advertisement], SALT)
    assert first.candidates[0].token == second.candidates[0].token


def test_report_dict_is_json_serialisable():
    import json

    report = discovery.summarise(
        [discovery.Advertisement(address=SAMPLE, name=f"R8W@{FRAGMENT}", rssi=-55)], SALT
    )
    assert json.loads(json.dumps(report.as_dict()))["total_seen"] == 1


def test_scan_adapts_bleak_shaped_devices():
    devices = [
        FakeDevice(SAMPLE, f"R8W@{FRAGMENT}", -55),
        FakeDevice(PUBLIC_ADDRESS, "Watch"),
    ]
    report = asyncio.run(discovery.scan(5.0, SALT, scanner=_scanner(devices)))
    assert report.total_seen == 2
    assert [c.tier for c in report.candidates] == ["strong"]
    assert report.candidates[0].rssi == -55


def test_scan_tolerates_a_device_missing_optional_attributes():
    class Bare:
        address = SAMPLE

    report = asyncio.run(discovery.scan(5.0, SALT, scanner=_scanner([Bare()])))
    assert report.total_seen == 1


# ------------------------------------------- advertisement data adaptation

class FakeAdvertisementData:
    """Shaped like bleak 3.x ``AdvertisementData``."""

    def __init__(self, local_name=None, rssi=None, service_uuids=()):
        self.local_name = local_name
        self.rssi = rssi
        self.service_uuids = list(service_uuids)


def test_rssi_and_services_are_read_from_advertisement_data():
    """bleak 3.x moved both off BLEDevice; reading only the device loses them."""
    device = FakeDevice(SAMPLE, name=None)
    advertisement = FakeAdvertisementData(
        local_name=f"R8@{FRAGMENT}", rssi=-58, service_uuids=[gatt.DATA_SERVICE_UUID]
    )
    report = asyncio.run(
        discovery.scan(5.0, SALT, scanner=_scanner({SAMPLE: (device, advertisement)}))
    )
    assert report.total_seen == 1
    candidate = report.candidates[0]
    assert candidate.tier == "strong"
    assert candidate.rssi == -58
    assert candidate.known_services == ("uniden-data",)


def test_a_return_adv_dict_and_a_plain_list_give_the_same_answer():
    device = FakeDevice(SAMPLE, f"R8@{FRAGMENT}", -60)
    from_list = asyncio.run(discovery.scan(5.0, SALT, scanner=_scanner([device])))
    from_dict = asyncio.run(
        discovery.scan(5.0, SALT, scanner=_scanner({SAMPLE: (device, None)}))
    )
    assert from_list.as_dict()["candidates"] == from_dict.as_dict()["candidates"]


def test_an_unknown_advertised_service_is_not_reported_as_known():
    device = FakeDevice(SAMPLE, name=None)
    advertisement = FakeAdvertisementData(
        local_name=f"R8@{FRAGMENT}",
        service_uuids=["0000fe59-0000-1000-8000-00805f9b34fb"],
    )
    report = asyncio.run(
        discovery.scan(5.0, SALT, scanner=_scanner({SAMPLE: (device, advertisement)}))
    )
    assert report.candidates[0].known_services == ()


def test_service_uuid_matching_is_case_insensitive():
    device = FakeDevice(SAMPLE, name=None)
    advertisement = FakeAdvertisementData(
        local_name=f"R8@{FRAGMENT}", service_uuids=[gatt.DATA_SERVICE_UUID.upper()]
    )
    report = asyncio.run(
        discovery.scan(5.0, SALT, scanner=_scanner({SAMPLE: (device, advertisement)}))
    )
    assert report.candidates[0].known_services == ("uniden-data",)


def test_a_service_uuid_is_published_verbatim_not_tokenised():
    """A service UUID is the same on every unit of a model: not an identifier."""
    device = FakeDevice(SAMPLE, name=None)
    advertisement = FakeAdvertisementData(
        local_name=f"R8@{FRAGMENT}", service_uuids=[gatt.DATA_SERVICE_UUID]
    )
    report = asyncio.run(
        discovery.scan(5.0, SALT, scanner=_scanner({SAMPLE: (device, advertisement)}))
    )
    line = report.candidates[0].line()
    assert "uniden-data" in line
    assert not privacy.looks_like_identifier(line)
