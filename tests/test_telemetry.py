"""The bounded receive-only path.

No radio: the client is injected. These tests are the reason the receive path
could be written and reviewed before it ever ran against the detector.
"""

from __future__ import annotations

import asyncio

import pytest

from fixtures import RANDOM_STATIC
from uniden_r8 import gatt, telemetry
from uniden_r8.evidence import FILE_MODE, PrivateStore
from uniden_r8.privacy import looks_like_identifier

SALT = b"\xcd" * 32

# Captured-shape packets from upstream's R8w writeup. Used as *shapes* to test
# the parser, not as claims about what this R8 sends.
TELEMETRY_PACKET = b"12.1&0&W,0,193,C&0&12&D&D"
ALERT_PACKET = b"1,00,KA,3,33,33.7850,R,1&0&0&0"
CLEAR_PACKET = b"0&0&0&0"


# ---------------------------------------------------------------- parsing

def test_telemetry_publishes_only_the_conservative_fields():
    """Heading, speed, altitude and POI detail describe where Jeremy is."""
    published = telemetry.parse_telemetry(TELEMETRY_PACKET).publishable()
    assert set(published) == {"voltage", "gps_locked", "poi_warning", "parsed"}
    assert published["voltage"] == 12.1
    assert published["gps_locked"] is True


def test_no_published_field_carries_position():
    published = telemetry.parse_telemetry(b"11.8&SPEEDCAM,500,35&N,45,312,C&0&5&D&D")
    as_published = published.publishable()
    for banned in ("heading", "speed", "altitude", "distance", "limit", "kind"):
        assert banned not in as_published
    # A POI warning is reported as a bare boolean, never its detail.
    assert as_published["poi_warning"] is True
    assert "SPEEDCAM" not in repr(as_published)
    assert "500" not in repr(as_published)


def test_alerts_publish_what_a_detector_is_for():
    alert = telemetry.parse_alerts(ALERT_PACKET)[0]
    assert alert.publishable() == {
        "band": "KA", "strength": 3, "frequency_ghz": 33.785,
        "direction": "rear", "muted": False,
    }


def test_a_clear_packet_is_an_empty_list():
    assert telemetry.parse_alerts(CLEAR_PACKET) == []


def test_a_muted_alert_is_reported():
    assert telemetry.parse_alerts(b"1,00,KA,1,31,35.2000,F,2&0&0&0")[0].muted


def test_a_laser_alert_does_not_invent_a_frequency():
    """Field 5 carries a gun identifier on laser; reading it as GHz is wrong."""
    alert = telemetry.parse_alerts(b"1,00,LASER,8,0,5,F,1&0&0&0")[0]
    assert alert.band == "LASER"
    assert alert.frequency_ghz is None


def test_laser_still_yields_a_gun_id_and_no_frequency():
    """The laser branch of the field-5 split must survive the gate rewrite.

    The gate that used to reject an unfamiliar band, direction or raw signal
    is gone, but the tagged-union split on field 5 is unchanged: laser still
    reads it as a gun identifier, never as a frequency. Losing this would
    make every laser detection either invent a bogus GHz reading or drop the
    gun identifier silently.
    """
    alert = telemetry.parse_alerts(b"1,00,LASER,8,0,5,F,1&0&0&0")[0]
    assert alert.laser_gun_id == 5
    assert alert.frequency_ghz is None
    assert alert.band_recognised is True


def test_a_known_band_still_sets_the_frequency_exactly_as_before():
    """The leniency added for unfamiliar values must not weaken the normal path.

    A band this project has already documented is still looked up by name,
    and field 5 is still read as GHz for it -- proving the new
    `band_recognised` branch only changes behaviour for bands that are
    actually unfamiliar.
    """
    alert = telemetry.parse_alerts(ALERT_PACKET)[0]
    assert alert.band == "KA"
    assert alert.band_recognised is True
    assert alert.frequency_ghz == 33.785


def test_a_lowercase_direction_code_no_longer_discards_the_detection():
    """Regression test for a real bug: a lowercase code used to lose the alert.

    Only `band` was case-normalised before; `direction` was not, so a
    detector sending `r` instead of `R` failed the old direction check and
    the *entire* detection was thrown away, on a detector that has never
    produced one to lose. `direction` is now `.strip().upper()`ed exactly
    like `band`, so a lowercase code decodes the same as its uppercase form.
    """
    alert = telemetry.parse_alerts(b"1,00,KA,3,33,33.7850,r,1&0&0&0")[0]
    assert alert.direction == "R"
    assert alert.direction_name == "rear"


def test_an_unfamiliar_band_is_published_with_field_5_left_uninterpreted():
    """An unfamiliar band is data, not a reason to discard the detection.

    `frequency_ghz` and `laser_gun_id` are both keyed on the band: an unknown
    band selects neither branch, because guessing which one would either
    invent a frequency out of a gun-type code or vice versa. `field_5_raw`
    is the one thing that always survives, so nothing is lost even though
    nothing is interpreted.
    """
    alert = telemetry.parse_alerts(b"1,00,MYSTERY,3,33,33.7850,R,1&0&0&0")[0]
    assert alert.band == "MYSTERY"
    assert alert.band_recognised is False
    assert alert.frequency_ghz is None
    assert alert.laser_gun_id is None
    assert alert.field_5_raw == "33.7850"


def test_a_non_integer_raw_signal_keeps_the_detection_with_signal_none():
    """The raw signal's scale was never established on this product either.

    Requiring it to parse as an integer used to reject the whole slot over a
    field whose meaning is not even documented here. It is now just another
    value that can come back unknown without taking the detection with it.
    """
    alert = telemetry.parse_alerts(b"1,00,KA,3,not-a-number,33.7850,R,1&0&0&0")[0]
    assert alert.signal is None
    assert alert.band == "KA"
    assert alert.strength == 3


def test_a_band_that_cannot_be_sanitised_still_rejects_the_slot():
    """The new leniency has a floor: an unsanitisable string is not a band.

    An unfamiliar band survives if `_safe_word` accepts it; one that carries
    a character no field of this protocol should carry -- here, angle
    brackets -- still rejects the slot outright, exactly as an unreadable
    field always has. Leniency about vocabulary is not leniency about
    injection.
    """
    assert telemetry.parse_alerts(b"1,00,K<A>,3,33,33.7850,R,1&0&0&0") == []


@pytest.mark.parametrize("payload", [
    b"1,00,KA,9,33,33.7850,R,1&0&0&0",
    b"2,00,KA,3,33,33.7850,R,1&0&0&0",
    b"1,00,KA,3,33,33.7850,R&0&0&0",
])
def test_a_structurally_invalid_slot_still_rejects_the_detection(payload):
    """The gate is structural, and the structure is still enforced.

    A strength outside 1..8, an active marker that is not `1`, and fewer
    than eight comma-fields are the three checks the rewrite kept, exactly
    because they describe the shape of a slot rather than a vocabulary
    borrowed from a different product. Any of the three must still refuse
    the slot, or "structural only" would mean "no gate at all".
    """
    assert telemetry.parse_alerts(payload) == []


def test_the_all_clear_form_still_parses_as_recognised_with_no_alerts():
    """The one packet this detector has actually produced must still work.

    Every change above loosens the gate for a *detection*; this proves none
    of it disturbed the all-clear shape, which is the only shape confirmed
    on real hardware. `recognised` must stay true and `alerts` empty, not
    merely "not crash".
    """
    snapshot = telemetry.parse_alert_snapshot(CLEAR_PACKET)
    assert snapshot.recognised is True
    assert snapshot.alerts == []


def test_an_unreadable_slot_carries_its_sanitised_text_in_slot_raw():
    """A rejected slot used to leave nothing but a counter.

    That was the worst possible outcome for a detector that has never
    produced a real alert: the one packet that would explain why the parser
    is wrong was the one packet the parser discarded. A slot short of the
    required eight fields is still rejected, but its sanitised text is now
    kept on `Slot.raw` rather than thrown away.
    """
    snapshot = telemetry.parse_alert_snapshot(b"1,00,KA,3,33,33.7850,R&0&0&0")
    slot = snapshot.slots[0]
    assert slot.state == telemetry.SLOT_UNREADABLE
    assert slot.raw == "1,00,KA,3,33,33.7850,R"


def test_an_unsanitisable_unreadable_slot_carries_a_shape_not_the_device_text():
    """The device's own characters must never reach `Slot.raw`.

    A slot that fails to sanitise still needs to be diagnosable, so
    `_describe_unreadable` falls back to a *shape* description -- field
    count and offending character classes -- and this checks that fallback
    for the specific device-supplied substring it must never contain, not
    merely that some string came back.
    """
    snapshot = telemetry.parse_alert_snapshot(
        b"<script>,00,KA,3,33,33.7850,R,1&0&0&0"
    )
    slot = snapshot.slots[0]
    assert slot.state == telemetry.SLOT_UNREADABLE
    assert slot.raw is not None
    assert "script" not in slot.raw
    assert "<script>" not in slot.raw
    assert "markup" in slot.raw


def test_a_genuinely_unreadable_slot_still_marks_the_snapshot_unrecognised():
    """`recognised` and `rejected_slots` are the tracker's only signal to hold on.

    A snapshot with one bad slot must still say so at the snapshot level --
    not just describe the slot -- because it is `recognised` the tracker
    reads to decide whether an open threat may safely be ended.
    """
    snapshot = telemetry.parse_alert_snapshot(
        b"<script>,00,KA,3,33,33.7850,R,1&0&0&0"
    )
    assert snapshot.recognised is False
    assert snapshot.rejected_slots == 1


@pytest.mark.parametrize("payload", [
    b"", b"\xff\xfe\x00", b"garbage", b"1&2", b"&&&&", "12.1",
    bytes(range(32)),
])
def test_no_payload_can_raise(payload):
    """This runs against a detector in a moving vehicle."""
    telemetry.parse_telemetry(payload)
    telemetry.parse_alerts(payload)


def test_an_unrecognised_packet_is_reported_as_unparsed_not_invented():
    """The R8w layout is unconfirmed here; a bad guess must be visible."""
    reading = telemetry.parse_telemetry(b"\xff\xfe\x00")
    assert reading.parsed is False
    assert reading.voltage is None
    assert reading.gps_locked is None


def test_a_short_packet_does_not_produce_confident_values():
    reading = telemetry.parse_telemetry(b"12.1&0")
    assert reading.parsed is False


def test_only_the_seven_field_shape_counts_as_parsed():
    """Seven fields is the only shape this R8 has ever produced.

    A longer packet is still decoded -- a firmware update that appends a field
    must not blank the voltage on a display -- but it is graded, and `parsed`
    stays false so the schema-1 document refuses it.
    """
    reading = telemetry.parse_telemetry(b"12.1&0&W,0,193,C&0&12&D&D&extra")
    assert reading.parsed is False
    assert reading.shape == "extended-8"
    assert reading.voltage == 12.1, "decoded, but not blessed"


def test_a_short_packet_is_decoded_no_further_than_its_field_count():
    """With fewer fields there is nothing to line the values up against."""
    reading = telemetry.parse_telemetry(b"12.1&0&W,0,193,C")
    assert reading.parsed is False
    assert reading.shape == "short-3"
    assert reading.voltage is None


def test_the_schema_one_document_refuses_an_unconfirmed_shape():
    """The grade is only useful if something acts on it."""
    from uniden_r8 import collector

    state = collector.CollectorState()
    state.record_telemetry(telemetry.parse_telemetry(b"12.1&0&0&0&12&D&D&extra"))
    document = collector.build_document(state)
    assert document["telemetry"]["voltage"] is None
    assert document["telemetry"]["shape_confirmed"] is False


def test_an_unknown_alert_band_is_published_not_discarded():
    """This assertion used to be the opposite, and the opposite was the bug.

    It asserted `parse_alerts(...) == []` for an unrecognised band, which was
    correct under the old gate -- and encoded exactly the failure this
    detector could not afford: every band string on file came from a
    *different product*, an R8w, and this detector has never produced a real
    active alert. Rejecting the whole detection over one unfamiliar band
    would have published "clear" while the detector's own screen showed a
    real threat. The gate is now structural only, so an unrecognised band
    that still sanitises is published, marked unrecognised, with field 5 left
    uninterpreted rather than guessed at.

    The original payload used ``PRIVATE-NOTE``, which is 12 characters --
    over the band sanitiser's 8-character limit -- so it would still come
    back empty today, for an unrelated reason that would have hidden this
    exact regression from anyone re-running the old test unchanged. The band
    below is shortened so the test actually exercises the behaviour its name
    claims.
    """
    payload = b"1,00,PRIVATE,3,33,33.7850,R,1&0&0&0"
    alert = telemetry.parse_alerts(payload)[0]
    assert alert.band == "PRIVATE"
    assert alert.band_recognised is False


# ------------------------------------------------------------ fake client

class FakeCharacteristic:
    def __init__(self, uuid):
        self.uuid = uuid


class FakeService:
    def __init__(self, uuid, characteristics=()):
        self.uuid = uuid
        self.characteristics = [FakeCharacteristic(c) for c in characteristics]


def _compatible_services():
    return [
        FakeService(gatt.DATA_SERVICE_UUID, [
            gatt.TELEMETRY_UUID, gatt.ALERT_UUID, gatt.POI_UUID,
            gatt.SETTINGS_1_UUID, gatt.SETTINGS_2_UUID,
        ]),
    ]


class FakeClient:
    """Records every characteristic read, subscribed and unsubscribed."""

    def __init__(  # noqa: PLR0913 - explicit knobs keep failure tests readable
        self, *, services=None, values=None, notify_fail=(), read_fail=(),
        emit=(), notify_hang=()
    ):
        self.services = _compatible_services() if services is None else services
        self.values = {
            gatt.TELEMETRY_UUID: TELEMETRY_PACKET,
            gatt.ALERT_UUID: ALERT_PACKET,
        } if values is None else values
        self.notify_fail = set(notify_fail)
        self.notify_hang = set(notify_hang)
        self.read_fail = set(read_fail)
        self.emit = list(emit)
        self.reads: list[str] = []
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
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
        if uuid in self.read_fail:
            raise OSError("Not permitted")
        return self.values.get(uuid, b"")

    async def start_notify(self, uuid, handler):
        if uuid in self.notify_hang:
            await asyncio.sleep(60)
        if uuid in self.notify_fail:
            raise OSError("Notify not supported")
        self.subscribed.append(uuid)
        for target, payload in self.emit:
            if target == uuid:
                handler(None, bytearray(payload))

    async def stop_notify(self, uuid):
        self.unsubscribed.append(uuid)


def _run(client, seconds=5.0, tmp_path=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(telemetry, "MIN_RECEIVE_SECONDS", 0.0)
    store = PrivateStore(tmp_path / "private").ensure()
    return asyncio.run(
        telemetry.receive(RANDOM_STATIC, SALT, store, seconds, lambda a: client)
    ), store


def _raw_capture(store):
    captures = list(store.root.glob("live-raw-*.json"))
    assert len(captures) == 1
    return captures[0]


# ------------------------------------------------------- compatibility gate

def test_the_gate_refuses_a_device_without_the_vendor_service(tmp_path, monkeypatch):
    client = FakeClient(services=[FakeService("0000180a-0000-1000-8000-00805f9b34fb")])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert session.connected and not session.compatible
    assert session.services_missing
    assert client.reads == [], "nothing may be read before the gate passes"
    assert client.subscribed == []


def test_the_gate_refuses_a_service_missing_the_telemetry_characteristic(
    tmp_path, monkeypatch
):
    client = FakeClient(services=[
        FakeService(gatt.DATA_SERVICE_UUID, [gatt.ALERT_UUID]),
    ])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert not session.compatible
    assert any(gatt.TELEMETRY_UUID in m for m in session.services_missing)


def test_the_gate_passes_on_the_confirmed_layout(tmp_path, monkeypatch):
    session, _ = _run(FakeClient(), 0.01, tmp_path, monkeypatch)
    assert session.compatible


def test_the_gate_checks_the_live_device_not_the_catalogue(tmp_path, monkeypatch):
    """A firmware update could move the table; assuming is not checking."""
    client = FakeClient(services=[])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert not session.compatible


def test_characteristics_must_belong_to_the_data_service(tmp_path, monkeypatch):
    client = FakeClient(services=[
        FakeService(gatt.DATA_SERVICE_UUID, [gatt.ALERT_UUID]),
        FakeService("0000180a-0000-1000-8000-00805f9b34fb", [gatt.TELEMETRY_UUID]),
    ])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert not session.compatible
    assert any(gatt.TELEMETRY_UUID in missing for missing in session.services_missing)
    assert client.reads == []


# ------------------------------------------------------------- allowlists

def test_only_telemetry_and_alert_are_read(tmp_path, monkeypatch):
    client = FakeClient()
    _run(client, 0.01, tmp_path, monkeypatch)
    assert client.reads == [gatt.TELEMETRY_UUID, gatt.ALERT_UUID]
    assert gatt.POI_UUID not in client.reads
    assert gatt.SETTINGS_1_UUID not in client.reads
    assert gatt.SETTINGS_2_UUID not in client.reads


def test_only_telemetry_and_alert_are_subscribed(tmp_path, monkeypatch):
    client = FakeClient()
    _run(client, 0.01, tmp_path, monkeypatch)
    assert set(client.subscribed) == {gatt.TELEMETRY_UUID, gatt.ALERT_UUID}


def test_poi_and_settings_are_refused_by_the_gate_itself():
    """Even though the wider probe allowlist would admit them."""
    for uuid in (gatt.POI_UUID, gatt.SETTINGS_1_UUID, gatt.SETTINGS_2_UUID):
        assert gatt.assert_readable(uuid) == uuid          # probe-legal
        with pytest.raises(gatt.WriteRefused):
            gatt.assert_live_readable(uuid)                 # live-illegal


def test_the_command_characteristic_is_refused_by_the_live_gate():
    with pytest.raises(gatt.WriteRefused):
        gatt.assert_live_readable(gatt.COMMAND_WRITE_UUID)
    with pytest.raises(gatt.WriteRefused):
        gatt.assert_live_notifiable(gatt.COMMAND_WRITE_UUID)


def test_the_fake_client_has_no_write_method(tmp_path, monkeypatch):
    """Any attempt to write would raise AttributeError and fail the test."""
    client = FakeClient()
    assert not hasattr(client, "write_gatt_char")
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert session.compatible


# ---------------------------------------------------------- notifications

def test_notifications_are_counted_and_parsed(tmp_path, monkeypatch):
    client = FakeClient(emit=[
        (gatt.TELEMETRY_UUID, b"12.4&0&N,0,200,C&0&3&D&D"),
        (gatt.ALERT_UUID, ALERT_PACKET),
    ])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert session.telemetry_packets == 1
    assert session.alert_packets == 1
    assert session.latest.voltage == 12.4
    assert session.alerts[0].band == "KA"


def test_an_unparsed_notification_is_counted_separately(tmp_path, monkeypatch):
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, b"\xff\xfe")])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert session.telemetry_packets == 1
    assert session.unparsed_telemetry == 1


def test_a_notification_handler_never_propagates(tmp_path, monkeypatch):
    """An exception here disappears into bleak and kills the subscription."""
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, None)])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert session.compatible


# ------------------------------------------------- partial failure + teardown

def test_a_failed_subscription_does_not_stop_the_other(tmp_path, monkeypatch):
    client = FakeClient(notify_fail={gatt.ALERT_UUID})
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert client.subscribed == [gatt.TELEMETRY_UUID]
    assert any("Alerts" in e for e in session.errors)


def test_teardown_unsubscribes_everything_that_was_established(tmp_path, monkeypatch):
    client = FakeClient(notify_fail={gatt.ALERT_UUID})
    _run(client, 0.01, tmp_path, monkeypatch)
    assert client.unsubscribed == [gatt.TELEMETRY_UUID]
    assert client.exited, "the link must be released"


def test_teardown_happens_when_a_read_fails(tmp_path, monkeypatch):
    client = FakeClient(read_fail={gatt.TELEMETRY_UUID, gatt.ALERT_UUID})
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert client.entered and client.exited
    assert len(session.errors) == 2


def test_teardown_happens_when_the_gate_refuses(tmp_path, monkeypatch):
    client = FakeClient(services=[])
    _run(client, 0.01, tmp_path, monkeypatch)
    assert client.exited


# ------------------------------------------------------------------ bounds

@pytest.mark.parametrize("requested,expected", [
    (30.0, 30.0), (0.0, 5.0), (-5.0, 5.0), (99999.0, 120.0),
    (None, 30.0), ("x", 30.0), (float("nan"), 30.0), (float("inf"), 120.0),
])
def test_the_receive_window_is_always_clamped(requested, expected):
    assert telemetry.bounded_receive_seconds(requested) == expected


def test_a_hung_session_is_cancelled(tmp_path, monkeypatch):
    async def hang(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(telemetry, "_session", hang)
    monkeypatch.setattr(telemetry, "RECEIVE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(telemetry, "CONNECT_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(telemetry, "MIN_RECEIVE_SECONDS", 0.01)
    store = PrivateStore(tmp_path / "private").ensure()
    session = asyncio.run(telemetry.receive(RANDOM_STATIC, SALT, store, 0.01))
    assert not session.connected
    assert "cancelled" in session.errors[0]


def test_timeout_unsubscribes_and_disconnects_after_partial_setup(
    tmp_path, monkeypatch
):
    client = FakeClient(notify_hang={gatt.ALERT_UUID})
    monkeypatch.setattr(telemetry, "MIN_RECEIVE_SECONDS", 0.01)
    monkeypatch.setattr(telemetry, "CONNECT_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(telemetry, "RECEIVE_GRACE_SECONDS", 0.02)
    store = PrivateStore(tmp_path / "private").ensure()

    session = asyncio.run(
        telemetry.receive(RANDOM_STATIC, SALT, store, 0.01, lambda _address: client)
    )

    assert not session.connected
    assert "cancelled" in session.errors[0]
    assert client.unsubscribed == [gatt.TELEMETRY_UUID]
    assert client.exited
    assert _raw_capture(store).exists(), "partial evidence must survive timeout"


def test_a_connection_failure_is_reported_not_raised(tmp_path, monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError("le-connection-abort-by-local")

    monkeypatch.setattr(telemetry, "_session", boom)
    store = PrivateStore(tmp_path / "private").ensure()
    session = asyncio.run(telemetry.receive(RANDOM_STATIC, SALT, store, 5.0))
    assert not session.connected
    assert "OSError" in session.errors[0]


def test_receiving_without_a_salt_is_refused(tmp_path):
    store = PrivateStore(tmp_path / "private").ensure()
    with pytest.raises(ValueError):
        asyncio.run(telemetry.receive(RANDOM_STATIC, b"", store, 5.0))


# ----------------------------------------------------------------- privacy

def test_raw_payloads_land_in_the_private_store_owner_only(tmp_path, monkeypatch):
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, TELEMETRY_PACKET)])
    _, store = _run(client, 0.01, tmp_path, monkeypatch)
    raw = _raw_capture(store)
    assert raw.exists()
    assert raw.stat().st_mode & 0o777 == FILE_MODE
    assert store.is_sealed()


def test_raw_payloads_are_not_in_the_published_output(tmp_path, monkeypatch):
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, b"12.1&0&W,0,193,C&0&12&D&D")])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    rendered = session.render() + repr(session.as_dict())
    assert "hex" not in rendered
    assert TELEMETRY_PACKET.decode() not in rendered
    assert "193" not in rendered, "altitude must not reach published output"


def test_the_session_output_carries_no_identifier(tmp_path, monkeypatch):
    client = FakeClient(emit=[(gatt.ALERT_UUID, ALERT_PACKET)])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    rendered = session.render() + repr(session.as_dict())
    assert not looks_like_identifier(rendered)
    assert RANDOM_STATIC not in rendered


def test_unrecognised_alert_text_stays_private(tmp_path, monkeypatch):
    """An unfamiliar band is published as a detection, but never as its text.

    Two rules meet here and both have to hold. A band this project has not seen
    must not discard the detection -- every band string on file came from a
    different product, so an unfamiliar one is exactly what this detector is
    expected to produce, and answering "clear" during a real threat is the worst
    output this parser has. But an arbitrary string from a device must not be
    reflected into what a person or another program reads.

    So the detection survives (`unparsed_alert_packets` stays 0) and the string
    does not reach the public render. The sanitised text is still available in
    the owner-only detailed view, which is where an unexpected value belongs.

    This test previously asserted the packet was *unparsed*. That encoded the
    bug: the whole detection was thrown away over field 2.
    """
    payload = b"1,00,PRIVATE-NOTE,3,33,33.7850,R,1&0&0&0"
    client = FakeClient(emit=[(gatt.ALERT_UUID, payload)])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)

    rendered = session.render() + repr(session.as_dict())
    assert "PRIVATE-NOTE" not in rendered
    assert session.unparsed_alert_packets == 0
    assert len(session.alerts) == 1

    alert = session.alerts[0]
    assert alert.band_recognised is False
    assert alert.publishable()["band"] == "unknown"
    # The text is not lost -- it is owner-only.
    assert alert.detailed()["band"] == "PRIVATE-NOTE"


def test_an_exception_message_is_scrubbed(tmp_path, monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError(f"Device {RANDOM_STATIC} disconnected")

    monkeypatch.setattr(telemetry, "_session", boom)
    store = PrivateStore(tmp_path / "private").ensure()
    session = asyncio.run(telemetry.receive(RANDOM_STATIC, SALT, store, 5.0))
    assert not looks_like_identifier(session.errors[0])


def test_retention_is_bounded(tmp_path, monkeypatch):
    """415 MiB of RAM; an unbounded capture list is a real hazard."""
    monkeypatch.setattr(telemetry, "MAX_RETAINED", 5)
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, TELEMETRY_PACKET)] * 50)
    _, store = _run(client, 0.01, tmp_path, monkeypatch)
    import json

    captured = json.loads(_raw_capture(store).read_text())
    assert len(captured["packets"]) <= 5


def test_the_render_survives_an_empty_window(tmp_path, monkeypatch):
    client = FakeClient(values={}, emit=[])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    assert "No telemetry arrived" in session.render() or session.latest is not None


# ------------------------------------------------- the detailed views render

def test_the_detailed_render_survives_an_active_poi_warning():
    """The regression this was written for.

    `LiveSession._render_detail` reached for POI fields that a later
    simplification of `PoiWarning` had removed, so `live --full` raised
    AttributeError the first time a POI warning was actually active -- a path no
    test exercised because no captured packet has ever had one.
    """
    session = telemetry.LiveSession(
        started_at="2026-09-02T00:00:00Z", seconds=1.0,
        connected=True, compatible=True, detailed=True,
    )
    session.latest = telemetry.parse_telemetry(
        b"11.8&SPEEDCAM,500,35&N,45,312,C&0&5&D&D"
    )
    assert session.latest.poi.active
    rendered = session.render()
    assert "POI warning active" in rendered
    assert "heading N" in rendered


# ------------------------------------------- the POI group sanitiser

def test_an_active_poi_warning_keeps_its_structured_text():
    """`_safe_group` must not throw away a whole group over one comma.

    `_safe_word` is the right check for a single field and the wrong one for
    a comma-joined group -- applying it to the whole group turned
    "SPEEDCAM,500,35" into `None`, discarding the only readable copy of the
    first real camera warning this project would ever see, since `collect`
    keeps no raw packets. The conservative schema-1 view stays a bare
    boolean regardless: a display built against schema 1 must not gain a
    camera type or a distance just because the parser got better underneath.
    """
    reading = telemetry.parse_telemetry(b"11.8&SPEEDCAM,500,35&N,45,312,C&0&5&D&D")
    assert reading.poi.active is True
    assert reading.poi.raw == "SPEEDCAM,500,35"

    published = reading.publishable()
    assert published["poi_warning"] is True
    assert "SPEEDCAM" not in repr(published)


def test_a_poi_group_with_an_unsafe_subfield_is_refused_entirely():
    """One bad sub-field refuses the whole group rather than leaking a partial one.

    A half-sanitised value that dropped only the unsafe piece would still
    look trustworthy; `raw` must be `None`, not "SPEEDCAM,,35" with the
    dangerous piece silently removed.
    """
    reading = telemetry.parse_telemetry(
        b"11.8&SPEEDCAM,<script>,35&N,45,312,C&0&5&D&D"
    )
    assert reading.poi.raw is None


def test_the_coordinate_tripwire_fires_on_an_adjacent_decimal_degree_pair():
    """POI is the characteristic that holds saved camera locations and user marks.

    If a POI warning ever carries the position of the thing it is warning
    about, that would be the most sensitive text the detector sends -- worse
    than the GPS group's own tripwire, because these coordinates would be a
    saved place, not merely the vehicle's current one. Two adjacent
    sub-fields that both read as signed decimal degrees withhold `raw`
    entirely and set `suspect_pair`, exactly as the GPS group's tripwire
    does.
    """
    reading = telemetry.parse_telemetry(
        b"11.8&CAM,33.4484,-112.0740&N,45,312,C&0&5&D&D"
    )
    assert reading.poi.raw is None
    assert reading.poi.suspect_pair is True


def test_a_poi_group_with_too_many_parts_is_refused():
    """The part-count bound is real, not merely documented.

    Nine comma-separated parts are well within the 48-character limit, so
    only the part-count check can be responsible for the refusal.
    """
    reading = telemetry.parse_telemetry(
        b"11.8&A,B,C,D,E,F,G,H,I&N,45,312,C&0&5&D&D"
    )
    assert reading.poi.raw is None


def test_a_poi_group_longer_than_the_limit_is_refused():
    """The overall length bound is enforced before anything is rejoined.

    A single over-long sub-field must not slip through because it happens to
    be alphanumeric.
    """
    over_long = "CAM," + "1" * 50
    reading = telemetry.parse_telemetry(
        f"11.8&{over_long}&N,45,312,C&0&5&D&D".encode()
    )
    assert reading.poi.raw is None


def test_an_empty_subfield_does_not_cost_the_whole_group():
    """Position matters in this wire format, so a blank field is kept, not fatal.

    Dropping the whole group over one empty sub-field would lose the
    positions of everything after it -- a distance field with no value is
    still a distance field.
    """
    reading = telemetry.parse_telemetry(b"11.8&CAM,,35&N,45,312,C&0&5&D&D")
    assert reading.poi.raw == "CAM,,35"


def test_every_confidence_key_names_something_actually_published():
    """A grade attached to a field nobody emits is decoration.

    The map is published verbatim into the schema-2 document so a consumer can
    join a grade to a field by name; keys that match nothing would make that
    join silently empty.
    """
    reading = telemetry.parse_telemetry(TELEMETRY_PACKET)
    alert = telemetry.parse_alerts(ALERT_PACKET)[0]

    def paths(document, prefix=""):
        for key, value in document.items():
            here = f"{prefix}{key}"
            yield here
            if isinstance(value, dict):
                yield from paths(value, f"{here}.")

    published = set(paths(reading.detailed()))
    published |= {f"alerts[].{key}" for key in alert.detailed()}
    published |= {"alerts_empty"}

    unmatched = [
        key for key in telemetry.FIELD_CONFIDENCE
        if key not in published and not key.startswith("unknown.upstream_names")
    ]
    assert not unmatched, f"graded but never published: {unmatched}"


def test_an_unlisted_mute_code_keeps_the_detection_but_not_the_string():
    """Two rules meet on field 7, and both have to hold.

    Losing a real Ka warning because the mute code was unfamiliar is the worst
    thing this parser could do -- and putting an arbitrary device string into a
    published document is the second worst.
    """
    alert = telemetry.parse_alerts(b"1,00,KA,3,33,33.7850,R,9&0&0&0")[0]
    assert alert.band == "KA", "the detection survives an unknown mute code"
    assert alert.muted is None, "and the mute state is unknown, not false"
    assert alert.mute_status == "unknown"

    hostile = telemetry.parse_alerts(
        b"1,00,KA,3,33,33.7850,R,../../etc/passwd&0&0&0"
    )
    assert hostile and hostile[0].mute_code is None, "no device string is kept"
    assert "passwd" not in repr(hostile[0].detailed())


def test_a_non_finite_value_never_reaches_a_published_document():
    """`float()` accepts "nan" and "inf"; JSON does not.

    Python's json module writes them out as the bare tokens NaN and Infinity,
    which are not JSON. One such value anywhere in one packet would make every
    document this project publishes unparseable to a conforming reader — the
    state file, the broker, the feed — for as long as the detector kept sending
    it. A field that cannot be a number is unknown.
    """
    import json

    for payload in (b"nan&0&0&0&12&D&D", b"inf&0&0&0&12&D&D",
                    b"-Infinity&0&0&0&12&D&D"):
        reading = telemetry.parse_telemetry(payload)
        assert reading.voltage is None, payload
        rendered = json.dumps(reading.detailed())
        assert "NaN" not in rendered and "Infinity" not in rendered
        json.loads(rendered)          # would raise on a non-finite token

    alert = telemetry.parse_alerts(b"1,00,KA,3,33,nan,R,1&0&0&0")
    assert alert and alert[0].frequency_ghz is None, "the detection survives"
    json.loads(json.dumps(alert[0].detailed()))


def test_an_empty_alert_notification_is_not_read_as_all_clear():
    """A truncated packet is what a marginal link produces.

    An empty or NUL-only payload decodes to no slots and no rejections, so
    under a rejected-count test it read as a confident "nothing is being
    detected" — which ends every live track. One truncated packet mid-encounter
    would have made the alert disappear with a fabricated end in the history.
    """
    for payload in (b"", b"\x00", b"   ", b"\x00\x00\x00"):
        snapshot = telemetry.parse_alert_snapshot(payload)
        assert snapshot.recognised is False, payload
        assert snapshot.uncertain is True, payload

    # And a genuine all-clear is still a confident statement.
    clear = telemetry.parse_alert_snapshot(CLEAR_PACKET)
    assert clear.recognised is True
    assert clear.uncertain is False
    assert clear.alerts == []


def test_a_truncated_packet_holds_a_live_track_open():
    """The behaviour the flag exists to produce, end to end."""
    from uniden_r8 import events

    tracker = events.AlertTracker()
    live = telemetry.parse_alerts(ALERT_PACKET)
    tracker.observe(live, seq=1, monotonic_ns=1_000, wall_ns=1_000)
    assert len(tracker) == 1

    for seq in (2, 3, 4):
        truncated = telemetry.parse_alert_snapshot(b"")
        tracker.observe(
            truncated.alerts, seq=seq, monotonic_ns=seq * 1_000,
            wall_ns=seq * 1_000, hold_open=truncated.uncertain,
        )
    assert len(tracker) == 1, "a truncated packet ended a live threat"


def test_the_gps_status_letter_d_reports_no_fix():
    """`D` means no fix, and saying so is better than saying "unknown".

    Measured, not assumed: a 50-minute drive from a cold start produced
    E -> D -> C in order, and the heading field was present in exactly the rows
    whose status was C and in none of the others. A collector that reported
    "unknown" for the three minutes of every cold start would be under-reporting
    something the detector was stating plainly.
    """
    reading = telemetry.parse_telemetry(b"13.2&0&,0,0,D&0&12&N&N")
    assert reading.gps.status_raw == "D"
    assert reading.gps.locked is False
    assert reading.publishable()["gps_locked"] is False


def test_the_gps_status_letter_c_reports_a_fix():
    """The other half of the same observation, and the older of the two."""
    reading = telemetry.parse_telemetry(b"13.2&0&NE,42,1289,C&0&12&N&N")
    assert reading.gps.locked is True
    assert reading.gps.direction_8 == "NE"


def test_an_unfamiliar_gps_status_letter_stays_unknown():
    """`E` has been seen twice. Two samples is not a meaning.

    "We could not tell" and "there is no fix" are different facts, and a field
    that collapses them tells a consumer it knows something it does not. This is
    the control that stops the next letter being guessed at.
    """
    for letter in (b"E", b"Z"):
        reading = telemetry.parse_telemetry(b"13.2&0&,0,0," + letter + b"&0&12&N&N")
        assert reading.gps.locked is None, letter
        assert reading.publishable()["gps_locked"] is None, letter
