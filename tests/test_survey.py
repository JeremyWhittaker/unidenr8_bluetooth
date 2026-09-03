"""The survey command: what it finds, what it never reads, and what it prints.

`uniden_r8.survey` enumerates the detector's own GATT tree -- services,
characteristics, properties, and ``0x2901`` user descriptions -- without ever
issuing a GATT *read* against a characteristic value.  That is what lets it run
with no `--confirm`: unlike `inspection.py`, there is no saved coordinate it
could reach, because there is no value it ever asks for.  `--listen` is the one
addition, and it is still not a read: it subscribes to the command-response
characteristic (and only that one) and sends nothing.

These tests exist to keep those two claims true as this module changes:

1. Enumeration reads descriptors and metadata only -- proven here by a fake
   client that records every `read_gatt_char` call and asserting the list
   stays empty through a full survey, `--listen` included.
2. `--listen` cannot reach any other characteristic -- proven by
   `gatt.assert_survey_notifiable`, its own narrow gate, and by showing the
   pre-existing `READABLE_UUIDS`/`NOTIFY_UUIDS` allowlists were not quietly
   widened to admit the same characteristic.

No test here needs a radio, `bleak`, or the wall clock: `survey()` takes a
`client_factory`, the fake below records what it is asked for, and every timing
value that would otherwise need a real sleep is produced by monkeypatching
`asyncio.sleep` and `time.monotonic`.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fixtures import RANDOM_STATIC
from uniden_r8 import cli, gatt, survey
from uniden_r8.evidence import PrivateStore, publish

#: A characteristic UUID this project has never catalogued.  Not a Bluetooth
#: address -- an ordinary 128-bit GATT UUID, the same kind already used as a
#: literal throughout `gatt.py` and `test_gatt_safety.py` -- so it carries no
#: identifier `test_repo_hygiene.py` would need to treat specially.
UNKNOWN_CHARACTERISTIC_UUID = "0000dead-0000-1000-8000-00805f9b34fb"

#: Handles for the fake 0x2901 descriptors below.  Arbitrary integers, the
#: same role `SETTINGS_1_HANDLE` plays in `test_config_and_inspection.py`.
TELEMETRY_DESCRIPTOR_HANDLE = 11
UNKNOWN_DESCRIPTOR_HANDLE = 22
UNPRINTABLE_DESCRIPTOR_HANDLE = 33
TOO_LONG_DESCRIPTOR_HANDLE = 44
FAILING_DESCRIPTOR_HANDLE = 55

#: A recognisable byte pattern for the "never reaches the publishable view"
#: test.  Deliberately not valid UTF-8 (0xca is not a legal leading byte), so
#: `_readable_name` cannot turn it into a `text` field either -- the hex is the
#: only place it can leak from.
NOTIFICATION_BYTES = bytes.fromhex("cafebabefeedfacedeadbeefbaadf00d")


# --------------------------------------------------------------- fake device

class FakeDescriptor:
    def __init__(self, uuid, handle):
        self.uuid = uuid
        self.handle = handle


class FakeCharacteristic:
    def __init__(self, uuid, properties=(), descriptors=()):
        self.uuid = uuid
        self.properties = tuple(properties)
        self.descriptors = list(descriptors)


class FakeService:
    def __init__(self, uuid, characteristics=()):
        self.uuid = uuid
        self.characteristics = list(characteristics)


class FakeClient:
    """A detector that answers descriptor reads and remembers every call.

    `read_gatt_char` is implemented -- and recorded -- purely so
    `test_the_survey_reads_no_characteristic_value` has something to prove an
    empty list about; nothing in `survey.py` is expected to call it.
    `start_notify` delivers any `pending_notifications` synchronously, right
    when the survey subscribes, so a notification test needs no real waiting
    and no background task racing the asserting thread.
    """

    def __init__(self, services=(), descriptions=None, descriptor_failures=(),
                 notify_failures=(), pending_notifications=()):
        self.services = list(services)
        self.descriptions = dict(descriptions or {})
        self.descriptor_failures = set(descriptor_failures)
        self.notify_failures = set(notify_failures)
        self.pending_notifications = list(pending_notifications)
        self.reads: list[str] = []
        self.descriptor_reads: list[int] = []
        self.start_notify_calls: list[str] = []
        self.stop_notify_calls: list[str] = []
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    async def read_gatt_char(self, uuid):
        self.reads.append(uuid)
        return b""

    async def read_gatt_descriptor(self, handle):
        self.descriptor_reads.append(handle)
        if handle in self.descriptor_failures:
            raise OSError("descriptor not readable on this firmware")
        return self.descriptions.get(handle, b"")

    async def start_notify(self, uuid, callback):
        self.start_notify_calls.append(uuid)
        if uuid in self.notify_failures:
            raise OSError("subscribe refused")
        for payload in self.pending_notifications:
            callback(None, bytearray(payload))

    async def stop_notify(self, uuid):
        self.stop_notify_calls.append(uuid)


class RecordingFactory:
    """The injection seam with a memory; see `test_config_and_inspection.py`."""

    def __init__(self, client=None):
        self.client = client
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, address, adapter=None):
        self.calls.append((address, adapter))
        if self.client is None:
            raise AssertionError("a client was constructed when none should have been")
        return self.client


def _store(tmp_path) -> PrivateStore:
    return PrivateStore(tmp_path / "private")


def _survey(store, factory, **kwargs):
    """Run one survey against a fake client, on this thread, with no radio."""
    return asyncio.run(
        survey.survey(RANDOM_STATIC, store, client_factory=factory, **kwargs)
    )


async def _instant_sleep(_seconds):
    """Stand in for `asyncio.sleep` so a listen window costs no wall-clock time."""
    return None


class _FakeClock:
    """A stand-in for the `time` module, bound only inside `survey.py`.

    Patching `survey.time.monotonic` in place would also change what
    `asyncio`'s own event loop sees, because it imports the very same `time`
    module for its internal scheduling -- and the loop calls `monotonic()`
    itself, an unpredictable number of times, before this module's own code
    ever runs.  Rebinding the *name* `survey.time` to this object instead
    leaves the real `time` module, and therefore the event loop, untouched;
    only `survey.py`'s own `time.monotonic()` calls see the fixed sequence.
    """

    def __init__(self, values):
        self._values = iter(values)

    def monotonic(self):
        return next(self._values)


def _fragments(payload: bytes) -> list[str]:
    """Every overlapping two-byte run of *payload*, hex-encoded.

    Matches `tests/test_config_and_inspection.py::_fragments`: checking only
    the whole hex string would pass on output that leaked everything except
    the first and last byte, which is not the invariant anyone wants.
    """
    return [payload[index:index + 2].hex() for index in range(len(payload) - 1)]


def _haystack(text: str, result: survey.Survey) -> str:
    """*text* with the timestamp and the capture filename derived from it removed.

    Mirrors `test_config_and_inspection.py::_haystack`. Both fields are made of
    digits, which are also hex digits, so a payload fragment can coincide with
    the clock by pure chance -- that exact collision already reached this
    project once, at a measured rate of 0.23% of runs, and cost a fifth of a
    percent of test runs before it was diagnosed. Neither field can carry
    device content, so both are removed before the search runs at all.
    """
    for generated in (result.capture_name, result.read_at):
        if generated:
            text = text.replace(generated, "")
            text = text.replace("".join(c for c in generated if c.isalnum()), "")
    return text


def _typical_tree():
    """A vendor tree with one of everything this suite needs to distinguish.

    Telemetry: known, with a readable 0x2901 name.  Alert: known, undescribed.
    POI: known and sensitive.  Command write: known and forbidden.  Command
    response: known.  One characteristic this project has never catalogued,
    so `unknown_attributes` always has something to report.
    """
    return [
        FakeService(gatt.DATA_SERVICE_UUID, [
            FakeCharacteristic(
                gatt.TELEMETRY_UUID, properties=("read", "notify"),
                descriptors=[FakeDescriptor(
                    gatt.CHARACTERISTIC_USER_DESCRIPTION_UUID, TELEMETRY_DESCRIPTOR_HANDLE
                )],
            ),
            FakeCharacteristic(gatt.ALERT_UUID, properties=("read", "notify")),
            FakeCharacteristic(gatt.POI_UUID, properties=("read",)),
            FakeCharacteristic(
                UNKNOWN_CHARACTERISTIC_UUID, properties=("read",),
                descriptors=[FakeDescriptor(
                    gatt.CHARACTERISTIC_USER_DESCRIPTION_UUID, UNKNOWN_DESCRIPTOR_HANDLE
                )],
            ),
        ]),
        FakeService(gatt.COMMAND_SERVICE_UUID, [
            FakeCharacteristic(gatt.COMMAND_WRITE_UUID, properties=("write-without-response",)),
            FakeCharacteristic(gatt.COMMAND_RESPONSE_UUID, properties=("read", "notify")),
        ]),
    ]


TYPICAL_DESCRIPTIONS = {
    TELEMETRY_DESCRIPTOR_HANDLE: b"Telemetry\x00",
    UNKNOWN_DESCRIPTOR_HANDLE: b"\xff\xfe",
}


# =================================================================== enumeration

def test_a_known_characteristic_is_labelled_with_its_catalogue_name(tmp_path):
    """The catalogue's job here is only to *name* what the device already has.

    Getting this wrong either way is a real cost: a known attribute reported
    as unknown buries a real finding in noise, and the reverse invents a name
    the device never claimed.
    """
    client = FakeClient(services=_typical_tree(), descriptions=TYPICAL_DESCRIPTIONS)
    result = _survey(_store(tmp_path), RecordingFactory(client))
    by_uuid = {a.uuid: a for s in result.services for a in s.attributes}
    assert by_uuid[gatt.TELEMETRY_UUID].known_as == gatt.describe(gatt.TELEMETRY_UUID).name
    assert not by_uuid[gatt.TELEMETRY_UUID].unknown


def test_a_service_uuid_the_catalogue_recognises_is_labelled_the_same_way(tmp_path):
    """`_enumerate` names a service through the identical `gatt.describe` lookup
    it uses for a characteristic -- there is no separate service-name table.

    `gatt.CATALOGUE` today holds only characteristic entries (see
    `test_none_of_this_projects_vendor_service_uuids_are_in_the_catalogue`
    below), so no *real* GATT service can currently come back named. This uses
    one of those characteristic UUIDs as a stand-in service UUID purely to
    exercise the shared lookup path itself, not to claim a real service would
    be labelled today.
    """
    labelled_uuid = gatt.FIRMWARE_REVISION_UUID
    client = FakeClient(services=[FakeService(labelled_uuid, [
        FakeCharacteristic(gatt.TELEMETRY_UUID, properties=("read",)),
    ])])
    result = _survey(_store(tmp_path), RecordingFactory(client))
    assert result.services[0].known_as == gatt.describe(labelled_uuid).name


def test_none_of_this_projects_vendor_service_uuids_are_in_the_catalogue():
    """Documents a real, checked consequence of `gatt.CATALOGUE` holding only
    characteristics: every actual vendor *service* this project names as a
    constant is, today, invisible to `describe()` and so is reported by the
    survey as "not in this project's catalogue" even though this project does
    in fact know its name (see `discovery.KNOWN_SERVICES`). Not a `src/`
    change requested here -- pinned so the gap is visible rather than silent.
    """
    for uuid in (
        gatt.DATA_SERVICE_UUID, gatt.COMMAND_SERVICE_UUID,
        gatt.DEVICE_INFORMATION_SERVICE_UUID,
    ):
        with pytest.raises(gatt.UnknownCharacteristic):
            gatt.describe(uuid)


def test_an_uncatalogued_characteristic_is_reported_as_unknown_and_surfaced(tmp_path):
    """This is the entire point of the command: an attribute nobody catalogued
    is the interesting result, not an error, and it must not be lost in the
    ordinary service listing -- it has to show up in `unknown_attributes` too,
    since that is what a report or a future catalogue update would read.
    """
    client = FakeClient(services=_typical_tree(), descriptions=TYPICAL_DESCRIPTIONS)
    result = _survey(_store(tmp_path), RecordingFactory(client))
    by_uuid = {a.uuid: a for s in result.services for a in s.attributes}

    unknown = by_uuid[UNKNOWN_CHARACTERISTIC_UUID]
    assert unknown.known_as is None
    assert unknown.unknown is True
    assert unknown in result.unknown_attributes
    assert "UNKNOWN to this project" in result.render()


def test_properties_are_reported_exactly_as_the_device_gave_them(tmp_path):
    """The survey must not normalise, reorder or invent a property list."""
    client = FakeClient(services=[FakeService(gatt.DATA_SERVICE_UUID, [
        FakeCharacteristic(gatt.TELEMETRY_UUID, properties=("read", "notify")),
        FakeCharacteristic(gatt.ALERT_UUID, properties=("write-without-response",)),
        FakeCharacteristic(gatt.POI_UUID, properties=()),
    ])])
    result = _survey(_store(tmp_path), RecordingFactory(client))
    by_uuid = {a.uuid: a for s in result.services for a in s.attributes}
    assert by_uuid[gatt.TELEMETRY_UUID].properties == ("read", "notify")
    assert by_uuid[gatt.ALERT_UUID].properties == ("write-without-response",)
    assert by_uuid[gatt.POI_UUID].properties == ()


def test_a_short_printable_descriptor_value_is_reported_as_described_as(tmp_path):
    """The device's own name for an attribute is the cheapest discovery there is."""
    client = FakeClient(
        services=[FakeService(gatt.DATA_SERVICE_UUID, [
            FakeCharacteristic(gatt.TELEMETRY_UUID, descriptors=[
                FakeDescriptor(gatt.CHARACTERISTIC_USER_DESCRIPTION_UUID,
                                TELEMETRY_DESCRIPTOR_HANDLE),
            ]),
        ])],
        descriptions={TELEMETRY_DESCRIPTOR_HANDLE: b"Telemetry\x00"},
    )
    result = _survey(_store(tmp_path), RecordingFactory(client))
    assert result.services[0].attributes[0].described_as == "Telemetry"


def test_an_unprintable_descriptor_value_is_dropped_to_none(tmp_path):
    """This value comes off the device and would reach a terminal verbatim.

    Bytes that are not valid UTF-8, or that decode to something with a
    non-printable character, are not a name -- and the whole point of the
    filter is that they must never be echoed just because the device sent them.
    """
    client = FakeClient(
        services=[FakeService(gatt.DATA_SERVICE_UUID, [
            FakeCharacteristic(gatt.TELEMETRY_UUID, descriptors=[
                FakeDescriptor(gatt.CHARACTERISTIC_USER_DESCRIPTION_UUID,
                                UNPRINTABLE_DESCRIPTOR_HANDLE),
            ]),
        ])],
        descriptions={UNPRINTABLE_DESCRIPTOR_HANDLE: b"\xff\xfe\x01"},
    )
    result = _survey(_store(tmp_path), RecordingFactory(client))
    assert result.services[0].attributes[0].described_as is None


def test_a_descriptor_value_longer_than_the_limit_is_dropped_to_none(tmp_path):
    """A user description is meant to be a short name, not an essay; anything
    longer than `MAX_DESCRIPTION_CHARS` is not trusted to be one.
    """
    too_long = ("A" * (survey.MAX_DESCRIPTION_CHARS + 1)).encode("utf-8")
    client = FakeClient(
        services=[FakeService(gatt.DATA_SERVICE_UUID, [
            FakeCharacteristic(gatt.TELEMETRY_UUID, descriptors=[
                FakeDescriptor(gatt.CHARACTERISTIC_USER_DESCRIPTION_UUID,
                                TOO_LONG_DESCRIPTOR_HANDLE),
            ]),
        ])],
        descriptions={TOO_LONG_DESCRIPTOR_HANDLE: too_long},
    )
    result = _survey(_store(tmp_path), RecordingFactory(client))
    assert result.services[0].attributes[0].described_as is None


def test_a_descriptor_read_that_raises_is_recorded_and_does_not_abort_the_survey(tmp_path):
    """Absence is evidence: one refused descriptor must not cost the rest of
    the walk, exactly as a failed attribute read must not in `inspection.py`.
    """
    client = FakeClient(
        services=[FakeService(gatt.DATA_SERVICE_UUID, [
            FakeCharacteristic(gatt.TELEMETRY_UUID, descriptors=[
                FakeDescriptor(gatt.CHARACTERISTIC_USER_DESCRIPTION_UUID,
                                FAILING_DESCRIPTOR_HANDLE),
            ]),
            FakeCharacteristic(gatt.ALERT_UUID, descriptors=[
                FakeDescriptor(gatt.CHARACTERISTIC_USER_DESCRIPTION_UUID,
                                TELEMETRY_DESCRIPTOR_HANDLE),
            ]),
        ])],
        descriptor_failures={FAILING_DESCRIPTOR_HANDLE},
        descriptions={TELEMETRY_DESCRIPTOR_HANDLE: b"Alerts\x00"},
    )
    result = _survey(_store(tmp_path), RecordingFactory(client))
    by_uuid = {a.uuid: a for s in result.services for a in s.attributes}

    assert by_uuid[gatt.TELEMETRY_UUID].error == "OSError"
    assert by_uuid[gatt.TELEMETRY_UUID].described_as is None
    # The walk continued: the second characteristic's descriptor was still read.
    assert by_uuid[gatt.ALERT_UUID].described_as == "Alerts"
    assert by_uuid[gatt.ALERT_UUID].error == ""
    assert result.attribute_count == 2


def test_the_poi_characteristic_is_flagged_sensitive(tmp_path):
    """POI holds saved camera locations and user marks; the survey must say so."""
    client = FakeClient(services=_typical_tree(), descriptions=TYPICAL_DESCRIPTIONS)
    result = _survey(_store(tmp_path), RecordingFactory(client))
    by_uuid = {a.uuid: a for s in result.services for a in s.attributes}
    assert by_uuid[gatt.POI_UUID].sensitive is True
    assert by_uuid[gatt.TELEMETRY_UUID].sensitive is False


def test_the_command_write_characteristic_is_flagged_forbidden(tmp_path):
    """It is the one characteristic that actuates the detector."""
    client = FakeClient(services=_typical_tree(), descriptions=TYPICAL_DESCRIPTIONS)
    result = _survey(_store(tmp_path), RecordingFactory(client))
    by_uuid = {a.uuid: a for s in result.services for a in s.attributes}
    assert by_uuid[gatt.COMMAND_WRITE_UUID].forbidden is True
    assert by_uuid[gatt.TELEMETRY_UUID].forbidden is False


# ======================================================= reads no characteristic

def test_the_survey_reads_no_characteristic_value(tmp_path, monkeypatch):
    """The safety property the whole command depends on.

    A survey with the fullest tree this suite has, run with `--listen` active
    (so both halves of the command execute), must never once call
    `read_gatt_char`. If it did, that call could return a saved coordinate --
    exactly the risk `inspection.py` gates behind `--confirm` -- and this
    command has no such gate because it is not supposed to be able to reach one.
    """
    monkeypatch.setattr(survey.asyncio, "sleep", _instant_sleep)
    client = FakeClient(services=_typical_tree(), descriptions=TYPICAL_DESCRIPTIONS)
    result = _survey(
        _store(tmp_path), RecordingFactory(client), listen_seconds=survey.MIN_LISTEN_SECONDS
    )
    assert result.connected
    assert client.reads == []


def test_the_read_recorder_can_actually_catch_a_violation(tmp_path):
    """A check that cannot fail proves nothing.

    Without this, `test_the_survey_reads_no_characteristic_value` would pass
    just as well if `FakeClient.read_gatt_char` were never wired up at all, or
    if `reads.append` were silently dropped -- an empty list would look
    identical either way. Calling it directly here proves the list is not
    permanently empty by construction.
    """
    client = FakeClient()
    assert client.reads == []
    asyncio.run(client.read_gatt_char(gatt.TELEMETRY_UUID))
    assert client.reads == [gatt.TELEMETRY_UUID]


# ============================================================== the listen gate

@pytest.mark.parametrize("uuid", [
    gatt.COMMAND_WRITE_UUID,
    gatt.TELEMETRY_UUID,
    gatt.POI_UUID,
])
def test_the_survey_notify_gate_refuses_everything_but_the_response_characteristic(uuid):
    """`--listen` must not be able to reach a write, a live-data feed, or POI.

    Getting this gate wrong would let the one command with no `--confirm`
    quietly grow the ability to subscribe to something that matters -- the
    write characteristic, or a source of saved coordinates -- through an
    argument nobody thought of as a write path.
    """
    with pytest.raises(gatt.WriteRefused):
        gatt.assert_survey_notifiable(uuid)


def test_the_survey_notify_gate_permits_only_the_command_response_characteristic():
    """The one allowance this gate makes, and the whole reason it exists.

    A gate that refused everything would be indistinguishable from a bug that
    broke `--listen` entirely; this is the companion showing it still lets the
    one legitimate case through.
    """
    assert gatt.assert_survey_notifiable(gatt.COMMAND_RESPONSE_UUID) == gatt.COMMAND_RESPONSE_UUID
    assert frozenset({gatt.COMMAND_RESPONSE_UUID}) == gatt.SURVEY_LISTEN_UUIDS


def test_the_old_allowlists_still_exclude_the_response_characteristic():
    """A regression here would mean the new gate had been built by *relaxing*
    the live-path allowlists instead of adding a separate, narrower one --
    which would let the response characteristic leak into every other command
    that trusts `READABLE_UUIDS`/`NOTIFY_UUIDS`, not just the survey.
    """
    assert gatt.COMMAND_RESPONSE_UUID not in gatt.READABLE_UUIDS
    assert gatt.COMMAND_RESPONSE_UUID not in gatt.NOTIFY_UUIDS


def test_with_no_listen_time_start_notify_is_never_called(tmp_path):
    """The default survey (no `--listen`) must never touch the radio's CCCD.

    If `start_notify` fired anyway, a plain `uniden-r8 survey` -- the command
    documented as needing no `--confirm` because it reads nothing -- would be
    subscribing to a live vendor characteristic every time it ran, unasked.
    """
    client = FakeClient(services=_typical_tree(), descriptions=TYPICAL_DESCRIPTIONS)
    result = _survey(_store(tmp_path), RecordingFactory(client))
    assert result.listened is False
    assert client.start_notify_calls == []
    assert client.stop_notify_calls == []


def test_a_listen_window_subscribes_only_to_the_response_characteristic_and_unsubscribes(
    tmp_path, monkeypatch,
):
    """`stop_notify` must run even though the window ended normally -- the
    radio is shared with the vehicle's OBD link and must not be left
    subscribed after the command finishes.
    """
    monkeypatch.setattr(survey.asyncio, "sleep", _instant_sleep)
    client = FakeClient(services=_typical_tree(), descriptions=TYPICAL_DESCRIPTIONS)
    result = _survey(
        _store(tmp_path), RecordingFactory(client), listen_seconds=survey.MIN_LISTEN_SECONDS
    )
    assert result.listened is True
    assert client.start_notify_calls == [gatt.COMMAND_RESPONSE_UUID]
    assert client.stop_notify_calls == [gatt.COMMAND_RESPONSE_UUID]


def test_unprompted_notifications_are_captured_with_offset_and_length(tmp_path, monkeypatch):
    """The arrival offset and the length are what a report can safely show;
    the payload itself is not (see the publication tests below).
    """
    monkeypatch.setattr(survey.asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(survey, "time", _FakeClock([100.0, 100.25, 100.6, 105.0]))

    first, second = b"\x01\x02\x03", b"\x04\x05"
    client = FakeClient(pending_notifications=[first, second])
    result = _survey(
        _store(tmp_path), RecordingFactory(client), listen_seconds=survey.MIN_LISTEN_SECONDS
    )

    assert [n.length for n in result.notifications] == [len(first), len(second)]
    assert [n.at_seconds for n in result.notifications] == [
        pytest.approx(0.25), pytest.approx(0.6),
    ]
    assert result.listened_seconds == pytest.approx(5.0)


def test_silence_over_the_whole_window_is_a_measurement_not_a_failure(tmp_path, monkeypatch):
    """The absence of a notification is a real, useful answer, not an error.

    A render that let silence read like a failure -- a bare empty section, or
    no mention at all -- would train an operator to treat a quiet detector as
    something having gone wrong, when "it never spoke unprompted" is exactly
    the measurement `--listen` exists to take.
    """
    monkeypatch.setattr(survey.asyncio, "sleep", _instant_sleep)
    client = FakeClient(pending_notifications=[])
    result = _survey(
        _store(tmp_path), RecordingFactory(client), listen_seconds=survey.MIN_LISTEN_SECONDS
    )
    assert result.listened is True
    assert result.notifications == []
    rendered = result.render()
    assert "silence" in rendered
    assert "not a failure" in rendered


def test_notifications_beyond_the_retention_limit_are_dropped_not_accumulated(
    tmp_path, monkeypatch,
):
    """A characteristic that turns out to be chatty must not fill memory or a
    report; the excess is counted, not stored.
    """
    monkeypatch.setattr(survey.asyncio, "sleep", _instant_sleep)
    overflow = 7
    total = survey.MAX_RETAINED_NOTIFICATIONS + overflow
    payloads = [bytes([index % 256]) for index in range(total)]
    client = FakeClient(pending_notifications=payloads)
    result = _survey(
        _store(tmp_path), RecordingFactory(client), listen_seconds=survey.MIN_LISTEN_SECONDS
    )
    assert len(result.notifications) == survey.MAX_RETAINED_NOTIFICATIONS
    assert result.notifications_dropped == overflow


# ================================================================== publication

def test_a_notification_payload_never_reaches_the_publishable_view(tmp_path, monkeypatch):
    """`as_dict()` and `render()` are what a report or a terminal ever see;
    `private_dict()` is the one place the raw bytes are allowed to be.
    """
    monkeypatch.setattr(survey.asyncio, "sleep", _instant_sleep)
    client = FakeClient(pending_notifications=[NOTIFICATION_BYTES])
    result = _survey(
        _store(tmp_path), RecordingFactory(client), listen_seconds=survey.MIN_LISTEN_SECONDS
    )

    rendered = _haystack(result.render(), result)
    printed = _haystack(json.dumps(result.as_dict(), sort_keys=True), result)
    private = json.dumps(result.private_dict(), sort_keys=True)

    assert NOTIFICATION_BYTES.hex() not in rendered
    assert NOTIFICATION_BYTES.hex() not in printed
    for fragment in _fragments(NOTIFICATION_BYTES):
        assert fragment not in rendered, fragment
        assert fragment not in printed, fragment
    assert NOTIFICATION_BYTES.hex() in private
    assert all("hex" not in entry for entry in result.as_dict()["notifications"])


def test_publish_accepts_both_the_render_and_the_json_view(tmp_path, monkeypatch):
    """The last gate before anything leaves the private side must not itself
    refuse ordinary, already-sanitised survey output.
    """
    monkeypatch.setattr(survey.asyncio, "sleep", _instant_sleep)
    client = FakeClient(
        services=_typical_tree(), descriptions=TYPICAL_DESCRIPTIONS,
        pending_notifications=[b"hello"],
    )
    result = _survey(
        _store(tmp_path), RecordingFactory(client), listen_seconds=survey.MIN_LISTEN_SECONDS
    )
    assert publish(result.render())
    assert publish(json.dumps(result.as_dict(), sort_keys=True))


# ========================================================================= CLI

def test_survey_is_a_registered_subcommand_that_needs_no_confirm():
    """Unlike `inspect`, there is no coordinate this command could reach, so
    it carries no `--confirm` gate at all -- checked on the parsed namespace,
    not merely on the help text, so a `--confirm` flag added by mistake would
    fail this rather than only look odd in `--help`.
    """
    parser = cli.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert "survey" in actions[0].choices

    args = parser.parse_args(["survey"])
    assert args.command == "survey"
    assert not hasattr(args, "confirm")


def test_a_listen_value_below_the_minimum_is_clamped_up(tmp_path, monkeypatch):
    """Checked through `survey()` itself, not the argument parser -- the
    parser only knows it received a float; the clamp is this module's
    decision about how long it is willing to hold the vehicle's radio.
    """
    sleep_calls: list[float] = []

    async def recording_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(survey.asyncio, "sleep", recording_sleep)
    client = FakeClient()
    _survey(_store(tmp_path), RecordingFactory(client), listen_seconds=0.001)
    assert sleep_calls == [survey.MIN_LISTEN_SECONDS]


def test_a_listen_value_above_the_maximum_is_clamped_down(tmp_path, monkeypatch):
    """An operator-supplied `--listen` cannot hold the shared radio indefinitely.

    Without this clamp a typo or a mistaken unit (minutes instead of seconds)
    would keep the CCCD subscription open far longer than the docstring's
    stated 5-120 s bound, competing with the vehicle's OBD link the whole time.
    """
    sleep_calls: list[float] = []

    async def recording_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(survey.asyncio, "sleep", recording_sleep)
    client = FakeClient()
    _survey(_store(tmp_path), RecordingFactory(client), listen_seconds=10_000.0)
    assert sleep_calls == [survey.MAX_LISTEN_SECONDS]


def test_the_vendor_services_are_named_rather_than_reported_as_unknown():
    """Services are named from `discovery.KNOWN_SERVICES`, not the catalogue.

    `gatt.describe` is keyed by *characteristic* and raises for every service
    UUID, including the two vendor services this project depends on. Labelling
    services through it reported every service as "not in this project's
    catalogue" -- which turned the one signal this command exists to produce,
    "here is something nobody knew about", into noise that fires on everything.
    """
    from uniden_r8.survey import _service_name

    assert _service_name(gatt.DATA_SERVICE_UUID) == "uniden-data"
    assert _service_name(gatt.COMMAND_SERVICE_UUID) == "uniden-command"
    assert _service_name(gatt.DEVICE_INFORMATION_SERVICE_UUID) == "device-information"


def test_a_service_this_project_has_never_heard_of_is_still_reported_unknown():
    """The companion: naming known services must not name everything.

    Without this, a fix that returned a label for any input would pass the test
    above and destroy the same signal it was written to protect.
    """
    from uniden_r8.survey import _service_name

    assert _service_name("0000dead-0000-1000-8000-00805f9b34fb") is None
