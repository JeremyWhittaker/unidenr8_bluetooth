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


def test_an_unknown_alert_band_is_not_reflected_into_public_output():
    payload = b"1,00,PRIVATE-NOTE,3,33,33.7850,R,1&0&0&0"
    assert telemetry.parse_alerts(payload) == []


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
    payload = b"1,00,PRIVATE-NOTE,3,33,33.7850,R,1&0&0&0"
    client = FakeClient(emit=[(gatt.ALERT_UUID, payload)])
    session, _ = _run(client, 0.01, tmp_path, monkeypatch)
    rendered = session.render() + repr(session.as_dict())
    assert "PRIVATE-NOTE" not in rendered
    assert session.unparsed_alert_packets == 1


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
