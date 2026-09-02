"""The identity read stays inside the read-only boundary."""

from __future__ import annotations

import asyncio

import pytest

from fixtures import RANDOM_STATIC
from uniden_r8 import gatt, identity

SALT = b"\x99" * 32


def test_the_read_plan_is_device_information_only():
    for uuid in gatt.IDENTITY_READ_PLAN:
        assert gatt.describe(uuid).service == gatt.DEVICE_INFORMATION_SERVICE_UUID


def test_the_serial_number_characteristic_is_not_read():
    """0x2A25 is a per-unit identifier with no diagnostic value here."""
    serial = "00002a25-0000-1000-8000-00805f9b34fb"
    assert serial not in gatt.IDENTITY_READ_PLAN
    assert serial not in {c.uuid for c in gatt.CATALOGUE}


def test_the_poi_characteristic_is_not_in_the_identity_plan():
    """It carries saved coordinates; identity does not need them."""
    assert gatt.POI_UUID not in gatt.IDENTITY_READ_PLAN


def test_every_planned_read_passes_the_gate():
    for uuid in gatt.IDENTITY_READ_PLAN:
        assert gatt.assert_readable(uuid) == uuid


def test_the_session_is_bounded():
    assert 0 < identity.CONNECT_TIMEOUT_SECONDS <= identity.SESSION_CEILING_SECONDS
    assert identity.SESSION_CEILING_SECONDS <= 120


def test_a_failure_to_connect_is_reported_not_raised(monkeypatch):
    """'The detector did not answer' is a finding the caller needs in-shape."""
    async def boom(address, salt, client_factory=None):
        raise OSError("le-connection-abort-by-local")

    monkeypatch.setattr(identity, "_session", boom)
    result = asyncio.run(identity.read_identity(RANDOM_STATIC, SALT))
    assert result.connected is False
    assert result.errors and "OSError" in result.errors[0]


def test_a_hung_session_is_cancelled(monkeypatch):
    async def hang(address, salt, client_factory=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(identity, "_session", hang)
    monkeypatch.setattr(identity, "SESSION_CEILING_SECONDS", 0.05)
    result = asyncio.run(identity.read_identity(RANDOM_STATIC, SALT))
    assert result.connected is False
    assert "timed out" in result.errors[0]


def test_reading_without_a_salt_is_refused():
    with pytest.raises(ValueError):
        asyncio.run(identity.read_identity(RANDOM_STATIC, b""))


def test_an_error_message_is_scrubbed(monkeypatch):
    """BlueZ puts the address in its error strings; it must not reach output."""
    from uniden_r8.privacy import looks_like_identifier

    async def boom(address, salt, client_factory=None):
        raise OSError(f"Device {RANDOM_STATIC} not available")

    monkeypatch.setattr(identity, "_session", boom)
    result = asyncio.run(identity.read_identity(RANDOM_STATIC, SALT))
    assert not looks_like_identifier(result.errors[0])
    assert RANDOM_STATIC not in result.errors[0]


def test_a_known_service_is_recognised():
    service = identity.DiscoveredService(uuid=gatt.DATA_SERVICE_UUID)
    assert service.is_known
    assert not identity.DiscoveredService(uuid="0000fe59-0000-1000-8000-00805f9b34fb").is_known


def test_render_and_as_dict_survive_a_failed_read():
    result = identity.Identity(read_at="now", connected=False, errors=["nope"])
    assert "Did not connect." in result.render()
    assert result.as_dict()["connected"] is False


# ------------------------------------------------------ injected fake client

class FakeCharacteristic:
    def __init__(self, uuid):
        self.uuid = uuid


class FakeService:
    def __init__(self, uuid, characteristics=()):
        self.uuid = uuid
        self.characteristics = [FakeCharacteristic(c) for c in characteristics]


class FakeClient:
    """Records every characteristic touched, and whether teardown happened."""

    def __init__(self, values=None, services=(), fail=()):
        self.values = values or {}
        self.services = list(services)
        self.fail = set(fail)
        self.reads: list[str] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    async def read_gatt_char(self, uuid):
        self.reads.append(uuid)
        if uuid in self.fail:
            raise OSError("Not supported")
        return self.values.get(uuid, b"")


DIS = gatt.DEVICE_INFORMATION_SERVICE_UUID


def _full_client(**kw):
    return FakeClient(
        values={
            gatt.MODEL_NUMBER_UUID: b"BTM10",
            gatt.MANUFACTURER_NAME_UUID: b"ATTOWAVE",
            gatt.FIRMWARE_REVISION_UUID: b"NA/NA",
            gatt.SOFTWARE_REVISION_UUID: b"R8/143/113",
        },
        services=[FakeService(DIS, [gatt.MODEL_NUMBER_UUID])],
        **kw,
    )


def test_only_the_four_device_information_characteristics_are_read():
    """The exact read allowlist, proven at the call site rather than declared."""
    client = _full_client()
    asyncio.run(identity.read_identity(RANDOM_STATIC, SALT, lambda a: client))
    assert client.reads == list(gatt.IDENTITY_READ_PLAN)
    assert gatt.POI_UUID not in client.reads
    assert gatt.TELEMETRY_UUID not in client.reads
    assert gatt.COMMAND_WRITE_UUID not in client.reads


def test_the_client_has_no_write_method_exercised():
    """The fake would raise AttributeError if anything tried to write."""
    client = _full_client()
    assert not hasattr(client, "write_gatt_char")
    result = asyncio.run(identity.read_identity(RANDOM_STATIC, SALT, lambda a: client))
    assert result.connected


def test_values_are_decoded_and_exposed():
    client = _full_client()
    result = asyncio.run(identity.read_identity(RANDOM_STATIC, SALT, lambda a: client))
    assert result.model == "BTM10"
    assert result.manufacturer == "ATTOWAVE"
    assert result.software == "R8/143/113"


def test_a_missing_characteristic_is_evidence_not_a_crash():
    client = _full_client(fail={gatt.FIRMWARE_REVISION_UUID})
    result = asyncio.run(identity.read_identity(RANDOM_STATIC, SALT, lambda a: client))
    assert result.connected
    assert result.firmware is None
    assert any("Firmware Revision String" in e for e in result.errors)
    # The remaining reads still happened.
    assert result.software == "R8/143/113"


def test_a_device_with_no_device_information_service_still_reports():
    client = FakeClient(values={}, services=[])
    result = asyncio.run(identity.read_identity(RANDOM_STATIC, SALT, lambda a: client))
    assert result.connected
    assert result.services == []
    assert result.model == ""


def test_teardown_happens_even_when_a_read_raises():
    """A leaked link makes the detector invisible to the next run."""
    client = _full_client(fail=set(gatt.IDENTITY_READ_PLAN))
    asyncio.run(identity.read_identity(RANDOM_STATIC, SALT, lambda a: client))
    assert client.entered and client.exited


def test_services_are_recorded_with_their_characteristic_counts():
    client = FakeClient(
        services=[
            FakeService(gatt.DATA_SERVICE_UUID, [gatt.TELEMETRY_UUID, gatt.ALERT_UUID]),
            FakeService("0000fe59-0000-1000-8000-00805f9b34fb", []),
        ]
    )
    result = asyncio.run(identity.read_identity(RANDOM_STATIC, SALT, lambda a: client))
    known = [s for s in result.services if s.is_known]
    assert len(known) == 1
    assert len(known[0].characteristic_uuids) == 2


def test_the_report_never_contains_the_address():
    from uniden_r8.privacy import looks_like_identifier

    client = _full_client()
    result = asyncio.run(identity.read_identity(RANDOM_STATIC, SALT, lambda a: client))
    rendered = result.render() + repr(result.as_dict())
    assert RANDOM_STATIC not in rendered
    assert not looks_like_identifier(rendered)
