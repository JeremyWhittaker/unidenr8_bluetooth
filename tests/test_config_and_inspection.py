"""Configuration loading, and the one command that reads saved coordinates.

Two modules in one file because they fail in the same direction.  The
configuration file decides what the collector may do with what it hears --
whether anything is written to disk, whether it crosses the network, whether
the vehicle's position is recorded at all -- and every one of those switches is
off until a person turns it on.  The inspection command is the other half of
the same decision: it is the only code path in this project that deliberately
reads the POI database, which holds saved camera locations and user marks, and
it will not run without ``--confirm``.

The failure both of them exist to make impossible is *silent enablement*.  A
mistyped ``record_coordinates`` that leaves coordinate logging at its default
looks exactly like a working configuration, and an inspection that connected
first and asked afterwards would already have opened the link.  So the loader
treats an unknown key as an error rather than a warning, and the tests below
assert that the refusal happens before a client object exists at all -- not
that it happens eventually.

None of this needs a radio, a broker, a network or a real detector.
``inspect()`` takes a ``client_factory``, and the fake here records every
attribute it is asked for, so "reads settings and POI and nothing else" is a
list that can be compared rather than a claim that has to be believed.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import fields

import pytest

from fixtures import DOC_HOST_A, DOC_HOST_B, RANDOM_STATIC
from uniden_r8 import config, gatt, inspection
from uniden_r8.evidence import FILE_MODE, PrivateStore, publish
from uniden_r8.privacy import looks_like_position

SALT = b"\x5a" * 32


@pytest.fixture(autouse=True)
def _no_ambient_configuration(tmp_path, monkeypatch):
    """Cut the loader off from the machine the tests happen to run on.

    ``find_config`` searches the working directory and the user's config home,
    so without this a developer with their own ``unidenr8.toml`` would get
    different results from CI, and "the defaults load with no file at all"
    would be testing their file.  The working directory is a *separate* empty
    directory from ``tmp_path`` so that a configuration a test writes is found
    only when that test names it.
    """
    monkeypatch.delenv(config.CONFIG_ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    empty = tmp_path / "cwd"
    empty.mkdir()
    monkeypatch.chdir(empty)


def _config_file(tmp_path, text: str, name: str = "unidenr8.toml"):
    """Write *text* as a configuration file and return its path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ============================================================ configuration
# --------------------------------------------------------------- defaults

def test_the_defaults_load_with_no_file_at_all():
    """A node with no configuration file is a supported node, not an error."""
    loaded = config.load_config()
    assert loaded.source == "defaults"
    assert loaded.collector.state_dir == config.CollectorConfig.state_dir


def test_every_optional_sink_is_off_in_the_defaults():
    """Each of these is a place data can go; none of them opens itself.

    This is the safety default, so it is asserted switch by switch rather than
    by comparing whole objects: a future default that flipped one of them would
    have to change this test and say why.
    """
    loaded = config.load_config()
    assert not loaded.history.enabled
    assert not loaded.gnss.enabled
    assert not loaded.gnss.record_coordinates
    assert not loaded.mqtt.enabled
    assert not loaded.feed.enabled
    assert not loaded.history.record_detector_motion


def test_the_obd_guard_is_the_one_default_that_is_armed():
    """The guard defaults on because forgetting it costs the vehicle's link."""
    assert config.Config().obd.guard


def test_the_defaults_produce_no_warnings():
    """If the shipped defaults warned about themselves the notes would be noise."""
    assert config.Config().warnings() == []


# ---------------------------------------------------------- strict parsing

def test_an_unknown_section_is_an_error_not_a_warning(tmp_path):
    """A section nobody reads is a section that does nothing, silently."""
    path = _config_file(tmp_path, "[privacy]\nrecord_coordinates = false\n")
    with pytest.raises(config.ConfigError) as caught:
        config.load_config(path)
    assert "privacy" in str(caught.value)
    assert "Known sections" in str(caught.value)


def test_an_unknown_key_is_an_error_not_a_warning(tmp_path):
    """The typo this refuses is the whole reason the loader is unforgiving.

    ``record_coordinate`` is one character from the key that decides whether
    the history records where the vehicle has been.  Accepted and ignored, it
    would read as "coordinates are on" and behave as "coordinates are off".
    """
    path = _config_file(tmp_path, "[gnss]\nenabled = true\nrecord_coordinate = true\n")
    with pytest.raises(config.ConfigError) as caught:
        config.load_config(path)
    assert "record_coordinate" in str(caught.value)
    assert "record_coordinates" in str(caught.value), "the message must name the real key"


def test_a_malformed_file_is_reported_against_its_path(tmp_path):
    """A parse error names the file, so the answer is not "somewhere"."""
    path = _config_file(tmp_path, "[collector\nstate_dir = 'x'\n")
    with pytest.raises(config.ConfigError) as caught:
        config.load_config(path)
    assert str(path) in str(caught.value)


# ------------------------------------------------------------ type checks

def test_a_string_where_a_number_belongs_is_refused(tmp_path):
    """Failing at load beats failing mid-drive with the radio already open."""
    path = _config_file(tmp_path, "[collector]\nheartbeat_seconds = 'fast'\n")
    with pytest.raises(config.ConfigError, match="must be a number"):
        config.load_config(path)


def test_a_bool_where_a_whole_number_belongs_is_refused(tmp_path):
    """``bool`` is a subclass of ``int``, so ``true`` would quietly become 1.

    A queue of depth one is not a queue, and nothing later in the run would
    say where the 1 came from.
    """
    path = _config_file(tmp_path, "[collector]\nqueue_size = true\n")
    with pytest.raises(config.ConfigError, match="whole number"):
        config.load_config(path)


def test_a_number_where_a_bool_belongs_is_refused(tmp_path):
    """``detail = 1`` reads as "on" and is not what the field accepts."""
    path = _config_file(tmp_path, "[collector]\ndetail = 1\n")
    with pytest.raises(config.ConfigError, match="true or false"):
        config.load_config(path)


def test_a_number_where_a_string_belongs_is_refused(tmp_path):
    path = _config_file(tmp_path, "[obd]\nunit = 5\n")
    with pytest.raises(config.ConfigError, match="must be a string"):
        config.load_config(path)


@pytest.mark.parametrize("section,key,value", [
    ("collector", "queue_size", "1"),
    ("collector", "heartbeat_seconds", "0.1"),
    ("collector", "stale_after_seconds", "99999"),
    ("obd", "interval_seconds", "0.5"),
    ("history", "retain_days", "4000"),
    ("gnss", "port", "70000"),
    ("mqtt", "port", "0"),
    ("feed", "port", "65536"),
])
def test_a_number_outside_its_declared_range_is_refused(tmp_path, section, key, value):
    """Every numeric value is range-checked at load, before the radio is touched."""
    path = _config_file(tmp_path, f"[{section}]\n{key} = {value}\n")
    with pytest.raises(config.ConfigError, match="outside the permitted range"):
        config.load_config(path)


def test_a_number_at_the_edge_of_its_range_is_accepted(tmp_path):
    """The bounds are inclusive; a check that rejected its own limits would lie."""
    path = _config_file(tmp_path, "[collector]\nqueue_size = 8\n[gnss]\nport = 65535\n")
    loaded = config.load_config(path)
    assert loaded.collector.queue_size == 8
    assert loaded.gnss.port == 65535


# ----------------------------------------------------------- derived paths

def test_a_relative_history_path_resolves_against_the_state_dir(tmp_path):
    """The history belongs inside the owner-only state directory by default."""
    path = _config_file(
        tmp_path,
        "[collector]\nstate_dir = 'var/state'\n[history]\npath = 'history.db'\n",
    )
    loaded = config.load_config(path)
    assert str(loaded.history_path) == "var/state/history.db"


def test_an_absolute_history_path_is_left_alone(tmp_path):
    """Naming an absolute path is a decision, and the loader does not second-guess it."""
    elsewhere = tmp_path / "elsewhere" / "history.db"
    path = _config_file(
        tmp_path,
        f"[collector]\nstate_dir = 'var/state'\n[history]\npath = '{elsewhere}'\n",
    )
    assert config.load_config(path).history_path == elsewhere


# --------------------------------------------------------------- warnings

def test_a_disabled_obd_guard_is_worth_saying_out_loud(tmp_path):
    """Legal on a bare Pi, and a mistake on the node with a vehicle attached."""
    path = _config_file(tmp_path, "[obd]\nguard = false\n")
    notes = config.load_config(path).warnings()
    assert len(notes) == 1
    assert "OBD guard disabled" in notes[0]


def test_a_feed_bound_off_loopback_is_worth_saying_out_loud(tmp_path):
    """Off loopback, live vehicle telemetry is readable by the whole network."""
    path = _config_file(tmp_path, f"[feed]\nenabled = true\nbind = '{DOC_HOST_A}'\n")
    notes = config.load_config(path).warnings()
    assert len(notes) == 1
    assert "feed bound to" in notes[0]


def test_a_loopback_feed_does_not_warn(tmp_path):
    """A warning that fires on the default binding would be ignored by everyone."""
    path = _config_file(tmp_path, "[feed]\nenabled = true\n")
    assert config.load_config(path).warnings() == []


def test_mqtt_to_a_remote_broker_without_tls_is_worth_saying_out_loud(tmp_path):
    """State crossing a network in clear text is a decision, not a detail."""
    path = _config_file(tmp_path, f"[mqtt]\nenabled = true\nhost = '{DOC_HOST_B}'\n")
    notes = config.load_config(path).warnings()
    assert len(notes) == 1
    assert "without TLS" in notes[0]


def test_mqtt_to_a_remote_broker_with_tls_does_not_warn(tmp_path):
    path = _config_file(
        tmp_path, f"[mqtt]\nenabled = true\nhost = '{DOC_HOST_B}'\ntls = true\n"
    )
    assert config.load_config(path).warnings() == []


def test_coordinate_recording_is_worth_saying_out_loud(tmp_path):
    """The history becomes a trace of where the vehicle has been.  Say so."""
    path = _config_file(tmp_path, "[gnss]\nenabled = true\nrecord_coordinates = true\n")
    notes = config.load_config(path).warnings()
    assert len(notes) == 1
    assert "coordinate recording enabled" in notes[0]


def test_disabled_retention_is_worth_saying_out_loud(tmp_path):
    """Rows that are never expired accumulate a drive history nobody chose."""
    path = _config_file(tmp_path, "[history]\nenabled = true\nretain_days = 0\n")
    notes = config.load_config(path).warnings()
    assert len(notes) == 1
    assert "retention disabled" in notes[0]


# ---------------------------------------------------------- password file

def test_a_group_or_world_readable_password_file_is_refused(tmp_path):
    """A broker password anyone else on the machine can read is not a secret."""
    secret = tmp_path / "broker.pw"
    secret.write_text("hunter2\n", encoding="utf-8")
    path = _config_file(
        tmp_path, f"[mqtt]\nenabled = true\npassword_file = '{secret}'\n"
    )
    for mode in (0o640, 0o604, 0o666):
        secret.chmod(mode)
        with pytest.raises(config.ConfigError, match="readable by group or other"):
            config.load_config(path)


def test_an_owner_only_password_file_is_accepted(tmp_path):
    secret = tmp_path / "broker.pw"
    secret.write_text("hunter2\n", encoding="utf-8")
    secret.chmod(0o600)
    path = _config_file(
        tmp_path, f"[mqtt]\nenabled = true\npassword_file = '{secret}'\n"
    )
    loaded = config.load_config(path)
    assert loaded.mqtt.password_file == str(secret)
    assert config.read_password(loaded) == "hunter2"


def test_a_missing_password_file_is_refused(tmp_path):
    """Discovered at load, not at the first publish attempt hours later."""
    path = _config_file(
        tmp_path, f"[mqtt]\nenabled = true\npassword_file = '{tmp_path / 'absent'}'\n"
    )
    with pytest.raises(config.ConfigError, match="does not exist"):
        config.load_config(path)


def test_there_is_no_inline_password_key(tmp_path):
    """A password in the config file is a password in a backup and a diff."""
    assert "password" not in {f.name for f in fields(config.MqttConfig)}
    path = _config_file(tmp_path, "[mqtt]\npassword = 'hunter2'\n")
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.load_config(path)


# ------------------------------------------------------------ the example

def test_the_example_file_round_trips_to_exactly_the_defaults(tmp_path):
    """The example is generated from the dataclasses, so it must parse back.

    An example that no longer loads -- or that loads to something other than
    the defaults it claims to show -- is documentation that lies, and this is
    the file people copy before they edit it.
    """
    path = _config_file(tmp_path, config.example_toml(), name="example.toml")
    loaded = config.load_config(path)
    default = config.Config()
    for section in ("collector", "obd", "history", "gnss", "mqtt", "feed"):
        assert getattr(loaded, section) == getattr(default, section), section
    assert loaded.warnings() == []
    assert loaded.source == str(path)


def test_the_example_names_every_section_and_every_key():
    """Generated, not maintained by hand, so it cannot fall behind the code."""
    text = config.example_toml()
    for section, cls in config._SECTIONS.items():
        assert f"[{section}]" in text
        for field in fields(cls):
            assert f"{field.name} = " in text, f"{section}.{field.name}"


# ---------------------------------------------------------- finding a file

def test_an_explicitly_named_missing_file_is_an_error(tmp_path):
    """A named file that is not there is a typo, and a typo deserves an answer.

    Ignoring it would run the collector on a configuration nobody chose.
    """
    with pytest.raises(config.ConfigError, match="no configuration file"):
        config.find_config(tmp_path / "absent.toml")
    with pytest.raises(config.ConfigError):
        config.load_config(tmp_path / "absent.toml")


def test_a_searched_missing_file_is_not_an_error():
    """"Look around and use one if you find one" is answered with None."""
    assert config.find_config() is None


def test_a_file_in_the_working_directory_is_found(tmp_path):
    path = tmp_path / "cwd" / config.DEFAULT_CONFIG_NAMES[0]
    path.write_text("[collector]\nadapter = 'hci1'\n", encoding="utf-8")
    assert config.find_config() == path
    assert config.load_config().collector.adapter == "hci1"


def test_the_environment_variable_pointing_nowhere_is_an_error(tmp_path, monkeypatch):
    """A service unit naming a file that is not there must not fall back.

    Falling back would run the collector on the defaults while systemd's own
    configuration says otherwise, which is the hardest kind of surprise to
    diagnose from a log.
    """
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(tmp_path / "absent.toml"))
    with pytest.raises(config.ConfigError, match=config.CONFIG_ENV_VAR):
        config.find_config()


# ================================================================ inspection

#: A recognisable pattern for the POI read.  Byte 0 is 3, upstream's "user
#: mark" marker, so the boundary walk has something to walk; the rest is
#: chosen to be findable in a haystack and to be nothing like a coordinate.
POI_BYTES = bytes.fromhex("03cafebabefeedfacedeadbe")

#: Settings 1 on the R8w is roughly 200 opaque bytes.  A run of consecutive
#: values makes any leak of the content obvious in a diff.
SETTINGS_1_BYTES = bytes(range(0x80, 0xC0))

#: Settings 2 was all 0xff on upstream's detector.  Kept, because "uniform" is
#: the case the summary is meant to answer without printing anything.
SETTINGS_2_BYTES = b"\xff" * 64

#: Handles for the 0x2901 descriptors the fake device exposes.
SETTINGS_1_HANDLE = 42
TELEMETRY_HANDLE = 7


class FakeDescriptor:
    def __init__(self, uuid, handle):
        self.uuid = uuid
        self.handle = handle


class FakeCharacteristic:
    def __init__(self, uuid, descriptors=()):
        self.uuid = uuid
        self.descriptors = list(descriptors)


class FakeService:
    def __init__(self, uuid, characteristics=()):
        self.uuid = uuid
        self.characteristics = list(characteristics)


def _detector_services():
    """The vendor layout, with a user description on two attributes.

    Telemetry carries one as well as settings, so the tests can tell "read the
    descriptors of what it is allowed to read" apart from "read every
    descriptor it discovered".
    """
    return [
        FakeService(gatt.DATA_SERVICE_UUID, [
            FakeCharacteristic(gatt.TELEMETRY_UUID, [
                FakeDescriptor(gatt.CHARACTERISTIC_USER_DESCRIPTION_UUID,
                               TELEMETRY_HANDLE),
            ]),
            FakeCharacteristic(gatt.ALERT_UUID),
            FakeCharacteristic(gatt.SETTINGS_1_UUID, [
                FakeDescriptor(gatt.CHARACTERISTIC_USER_DESCRIPTION_UUID,
                               SETTINGS_1_HANDLE),
            ]),
            FakeCharacteristic(gatt.SETTINGS_2_UUID),
            FakeCharacteristic(gatt.POI_UUID),
        ]),
    ]


class FakeClient:
    """A detector that answers reads and remembers every attribute touched.

    It has no method that writes a characteristic value, so a session that
    tried would raise rather than quietly succeed against a permissive mock.
    """

    def __init__(self, services=None, payloads=None, descriptions=None, failures=()):
        self.services = _detector_services() if services is None else services
        self.payloads = {
            gatt.SETTINGS_1_UUID: SETTINGS_1_BYTES,
            gatt.SETTINGS_2_UUID: SETTINGS_2_BYTES,
            gatt.POI_UUID: POI_BYTES,
        } if payloads is None else dict(payloads)
        self.descriptions = {SETTINGS_1_HANDLE: b"Settings 1\x00"} \
            if descriptions is None else dict(descriptions)
        self.failures = set(failures)
        self.reads: list[str] = []
        self.descriptor_reads: list[int] = []
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    async def read_gatt_char(self, uuid):
        self.reads.append(uuid)
        if uuid in self.failures:
            raise OSError("attribute not readable on this firmware")
        return self.payloads.get(uuid, b"")

    async def read_gatt_descriptor(self, handle):
        self.descriptor_reads.append(handle)
        return self.descriptions.get(handle, b"")


class RecordingFactory:
    """The injection seam with a memory.

    ``calls`` is the assertion that matters for the confirmation gate: a
    refusal that happened after the client was built would already have
    reached for the radio.
    """

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


def _inspect(store, factory, **kwargs):
    """Run one inspection against a fake client, on this thread, with no radio."""
    return asyncio.run(
        inspection.inspect(
            RANDOM_STATIC, SALT, store, client_factory=factory, **kwargs
        )
    )


def _fragments(payload: bytes) -> list[str]:
    """Every overlapping two-byte run of *payload*, hex-encoded.

    Checking the whole hex string alone would pass on output that leaked half
    the blob, which is not the invariant anyone wants.
    """
    return [payload[index:index + 2].hex() for index in range(len(payload) - 1)]


def _haystack(text: str) -> str:
    """*text* with the catalogue's own UUIDs removed before searching it.

    A UUID is a constant of this project, printed because the summary names
    the attribute it describes; it is not something the detector said.  Its
    hex digits collide with the hex of a payload by coincidence -- a run of
    consecutive settings bytes matched four characters of the Settings 2 UUID
    while this test was being written -- and a leak check that fails on its
    own vocabulary is a check people learn to weaken.  Removing them leaves
    the part of the output that could actually carry content.
    """
    for entry in gatt.CATALOGUE:
        text = text.replace(entry.uuid, "")
    return text


# ------------------------------------------------------- the confirmation

def test_inspect_without_confirmation_refuses_before_a_client_exists(tmp_path):
    """The gate is the whole command.  Nothing may reach the radio first.

    This is the only code path here that deliberately reads the detector's
    saved coordinates, so the refusal has to come before the connection, not
    after it.
    """
    factory = RecordingFactory()
    with pytest.raises(inspection.InspectionRefused, match="--confirm"):
        _inspect(_store(tmp_path), factory)
    assert factory.calls == [], "no client may be constructed"


def test_the_refusal_names_what_it_is_refusing(tmp_path):
    """A person deciding whether to confirm needs to know what they are confirming."""
    factory = RecordingFactory()
    with pytest.raises(inspection.InspectionRefused) as caught:
        _inspect(_store(tmp_path), factory)
    assert "POI" in str(caught.value)
    assert "camera locations" in str(caught.value)


def test_a_missing_salt_also_refuses_before_a_client_exists(tmp_path):
    """Without a salt an error message could not be scrubbed, so nothing starts."""
    factory = RecordingFactory()
    with pytest.raises(ValueError, match="salt"):
        asyncio.run(
            inspection.inspect(
                RANDOM_STATIC, b"", _store(tmp_path),
                confirmed=True, client_factory=factory,
            )
        )
    assert factory.calls == []


# ------------------------------------------------------------- what it reads

def test_a_confirmed_inspection_reads_settings_and_poi_in_that_order(tmp_path):
    """Least sensitive first: if it stops early, POI has not been read.

    The order is asserted as a list rather than as a set for exactly that
    reason -- "these three, eventually" is a different and weaker promise.
    """
    client = FakeClient()
    result = _inspect(_store(tmp_path), RecordingFactory(client), confirmed=True)
    assert client.reads == [
        gatt.SETTINGS_1_UUID, gatt.SETTINGS_2_UUID, gatt.POI_UUID,
    ]
    assert result.connected
    assert result.compatible


def test_a_confirmed_inspection_reads_nothing_outside_the_inspection_set(tmp_path):
    """The command characteristic and its response are not read, ever."""
    client = FakeClient()
    _inspect(_store(tmp_path), RecordingFactory(client), confirmed=True)
    assert set(client.reads) <= set(gatt.INSPECT_READ_UUIDS)
    assert gatt.COMMAND_WRITE_UUID not in client.reads
    assert gatt.COMMAND_RESPONSE_UUID not in client.reads
    assert gatt.TELEMETRY_UUID not in client.reads
    assert gatt.ALERT_UUID not in client.reads


def test_the_adapter_and_address_reach_the_client_factory(tmp_path):
    """The seam carries the controller choice, so a second radio can be pinned."""
    factory = RecordingFactory(FakeClient())
    _inspect(_store(tmp_path), factory, confirmed=True, adapter="hci1")
    assert factory.calls == [(RANDOM_STATIC, "hci1")]


def test_only_the_descriptors_of_readable_attributes_are_read(tmp_path):
    """The device's own name for an attribute is the cheapest discovery there is.

    It is still read only for the attributes the gate admits: the telemetry
    descriptor is discovered by the walk and left alone.
    """
    client = FakeClient()
    result = _inspect(_store(tmp_path), RecordingFactory(client), confirmed=True)
    assert client.descriptor_reads == [SETTINGS_1_HANDLE]
    assert TELEMETRY_HANDLE not in client.descriptor_reads
    described = {dump.name: dump.described_as for dump in result.attributes}
    assert described[gatt.describe(gatt.SETTINGS_1_UUID).name] == "Settings 1"
    assert described[gatt.describe(gatt.POI_UUID).name] is None


def test_the_session_is_closed_even_though_nothing_went_wrong(tmp_path):
    """The radio is shared with the vehicle's link; the link is not left open."""
    client = FakeClient()
    _inspect(_store(tmp_path), RecordingFactory(client), confirmed=True)
    assert client.exited


# ------------------------------------------------------ what it never prints

def test_the_summary_and_the_render_carry_no_device_bytes(tmp_path):
    """POI bytes are saved coordinates.  They go to the private store only.

    A recognisable pattern is fed to the POI characteristic and then looked
    for, in fragments, in everything this command is willing to print.  The
    raw hex has to be somewhere -- that is the point of reading it -- and the
    somewhere is an owner-only file.
    """
    store = _store(tmp_path)
    result = _inspect(store, RecordingFactory(FakeClient()), confirmed=True)

    rendered = _haystack(result.render())
    printed = _haystack(json.dumps(result.as_dict(), sort_keys=True))
    for blob in (POI_BYTES, SETTINGS_1_BYTES, SETTINGS_2_BYTES):
        assert blob.hex() not in rendered
        assert blob.hex() not in printed
        for fragment in _fragments(blob):
            assert fragment not in rendered, fragment
            assert fragment not in printed, fragment

    assert all("hex" not in entry for entry in result.as_dict()["attributes"])
    assert publish(result.render())
    assert publish(json.dumps(result.as_dict(), sort_keys=True))


def test_the_raw_hex_is_written_to_the_private_store_owner_only(tmp_path):
    """Undecoded and complete, in the one place raw evidence is allowed to be."""
    store = _store(tmp_path)
    result = _inspect(store, RecordingFactory(FakeClient()), confirmed=True)

    assert result.capture_name
    captured = store.path(result.capture_name)
    text = captured.read_text(encoding="utf-8")
    assert POI_BYTES.hex() in text
    assert SETTINGS_1_BYTES.hex() in text
    assert captured.stat().st_mode & 0o777 == FILE_MODE
    assert store.is_sealed()


def test_the_render_says_the_poi_contents_were_not_decoded(tmp_path):
    """A summary that looked like a parse would invite trusting it.

    The render now lists one line per candidate layout (`evaluate_layouts`),
    not one blessed guess, so the test checks that every layout name is
    present rather than a single sentence that named none of them.
    """
    rendered = _inspect(
        _store(tmp_path), RecordingFactory(FakeClient()), confirmed=True
    ).render()
    assert "contents NOT decoded" in rendered
    for name in inspection.CANDIDATE_LAYOUTS:
        assert name in rendered


def test_nothing_the_command_prints_looks_like_a_position(tmp_path):
    """The gate that stands between the POI database and anything published."""
    result = _inspect(_store(tmp_path), RecordingFactory(FakeClient()), confirmed=True)
    assert not looks_like_position(result.as_dict())
    assert not looks_like_position(result.render())


# --------------------------------------------------------- byte summaries

def test_summarise_bytes_on_an_empty_blob_measures_nothing_it_cannot_measure():
    """An empty POI database is a real and useful answer, not a failure."""
    assert inspection.summarise_bytes(b"") == {"length": 0, "empty": True}


def test_summarise_bytes_recognises_a_uniform_block():
    """Settings 2 was all 0xff upstream; "uniform" is the fact worth having."""
    stats = inspection.summarise_bytes(b"\xff" * 64)
    assert stats["length"] == 64
    assert not stats["empty"]
    assert stats["distinct_bytes"] == 1
    assert stats["all_same"]
    assert stats["dominant_byte"] == "0xff"
    assert stats["dominant_fraction"] == 1.0
    assert stats["zero_fraction"] == 0.0
    assert stats["printable_fraction"] == 0.0


def test_summarise_bytes_on_a_varied_blob_reports_shape_and_not_content():
    """Every number here is a statistic; none of them is a byte of the blob."""
    payload = b"R8" + bytes([0x00, 0x00, 0xFF, 0x41])
    stats = inspection.summarise_bytes(payload)
    assert stats["length"] == 6
    assert stats["distinct_bytes"] == 5
    assert not stats["all_same"]
    assert stats["dominant_byte"] == "0x00"
    assert stats["zero_fraction"] == round(2 / 6, 3)
    assert stats["printable_fraction"] == round(3 / 6, 3)
    assert payload.hex() not in json.dumps(stats)


# ------------------------------------------------------- record boundaries

@pytest.mark.parametrize("layout_name", sorted(inspection.CANDIDATE_LAYOUTS))
def test_record_boundaries_stops_at_an_unrecognised_type_byte(layout_name):
    """It stops at the first byte it does not know rather than walking on.

    Continuing past an unknown marker would produce offsets into bytes nobody
    has established the layout of, printed with the same confidence as the
    ones that fit.  Checked against every candidate layout -- not just
    whichever one `record_boundaries` happens to default to -- because a
    length table is exactly the kind of detail a test can bless by accident.
    """
    table = inspection.CANDIDATE_LAYOUTS[layout_name]
    _, user_mark_length = table[3]
    payload = (
        bytes([3]) + b"\x00" * (user_mark_length - 1)
        + bytes([0x99]) + b"\x00" * 32
    )
    walk = inspection.record_boundaries(payload, table)
    assert [entry["offset"] for entry in walk] == [0, user_mark_length]
    assert walk[0]["kind"] == "user mark"
    assert walk[-1]["kind"] == "unrecognised"
    assert walk[-1]["type_byte"] == "0x99"
    assert "walk stopped" in walk[-1]["note"]


def test_record_boundaries_does_not_walk_off_the_end_of_a_short_blob():
    """A record whose declared length overruns the blob is reported, not read."""
    walk = inspection.record_boundaries(bytes([1, 2, 3]))
    assert len(walk) == 1
    assert walk[0]["kind"] == "speed camera"
    assert walk[0]["fits"] is False


@pytest.mark.parametrize("layout_name", sorted(inspection.CANDIDATE_LAYOUTS))
def test_record_boundaries_reports_a_walk_that_consumes_the_whole_blob(layout_name):
    """Consuming the blob exactly is weak evidence the candidate layout is right.

    Built from each layout's own declared lengths rather than one hardcoded
    15/14, so this stays true for whichever table is under test instead of
    only the one `record_boundaries` used to default to.
    """
    table = inspection.CANDIDATE_LAYOUTS[layout_name]
    _, camera_length = table[1]
    _, redlight_length = table[2]
    payload = (
        bytes([1]) + bytes(range(camera_length - 1))
        + bytes([2]) + bytes(range(redlight_length - 1))
    )
    walk = inspection.record_boundaries(payload, table)
    assert [entry["offset"] for entry in walk] == [0, camera_length]
    assert all(entry["fits"] for entry in walk)
    assert not [entry for entry in walk if entry["kind"] == "unrecognised"]


#: Every key either `record_boundaries` or `evaluate_layouts` puts into a
#: returned dict.  One shared whitelist so a coordinate that slipped into
#: either function's output would be caught by the same check.
_BOUNDARY_AND_LAYOUT_KEYS = {
    "offset", "type_byte", "kind", "candidate_length", "fits", "note",
    "layout", "record_lengths", "records", "all_fit", "bytes_consumed",
    "bytes_total", "complete", "exact", "stopped_at",
}


@pytest.mark.parametrize("layout_name", sorted(inspection.CANDIDATE_LAYOUTS))
def test_record_boundaries_never_returns_a_coordinate(layout_name):
    """Offsets and lengths are not addresses.  Nothing here is decoded.

    Upstream published a candidate layout with big-endian floats at two
    offsets.  Reading those would produce coordinates -- home, work, the roads
    Jeremy drives -- from bytes nobody has verified, so no float is produced
    at all, in either candidate layout's output or `evaluate_layouts`'s
    summary of it.
    """
    table = inspection.CANDIDATE_LAYOUTS[layout_name]
    _, camera_length = table[1]
    _, mark_length = table[3]
    payload = (
        bytes([1]) + bytes(range(camera_length - 1))
        + bytes([3]) + bytes(range(mark_length - 1))
    )
    walk = inspection.record_boundaries(payload, table)
    assert walk
    assert not looks_like_position(walk)
    assert not [value for entry in walk for value in entry.values()
                if isinstance(value, float)]
    for entry in walk:
        assert set(entry) <= _BOUNDARY_AND_LAYOUT_KEYS, entry

    verdicts = inspection.evaluate_layouts(payload)
    assert not [value for verdict in verdicts for value in verdict.values()
                if isinstance(value, float)]
    for verdict in verdicts:
        assert set(verdict) <= _BOUNDARY_AND_LAYOUT_KEYS, verdict


def test_record_boundaries_on_an_empty_blob_suggests_nothing():
    assert inspection.record_boundaries(b"") == []


# ------------------------------------------------------- evaluating layouts

def test_evaluate_layouts_picks_whole_record_when_the_blob_is_built_that_way():
    """A blob written in one layout desynchronises almost immediately under the other.

    Two 10-byte user marks and one 13-byte speed camera, laid out the way
    ``whole-record`` (13/12/10) reads the type byte -- that table must
    consume the blob exactly, and ``payload-plus-header`` (15/14/12), reading
    the same bytes on the wrong assumption, must desynchronise and stop
    short.  This is the measurement `evaluate_layouts` exists to make: which
    of the two competing readings of upstream's numbers is the one this R8
    actually produces.
    """
    blob = (
        (bytes([3, 0]) + struct.pack(">ff", 33.1, -112.1)) * 2
        + bytes([1, 0]) + struct.pack(">ff", 33.3, -112.3)
        + struct.pack(">H", 90) + bytes([65])
    )
    assert len(blob) == 33
    verdicts = {v["layout"]: v for v in inspection.evaluate_layouts(blob)}

    whole_record = verdicts["whole-record"]
    assert whole_record["exact"] is True
    assert whole_record["records"] == 3
    assert whole_record["bytes_consumed"] == 33

    payload_plus_header = verdicts["payload-plus-header"]
    assert payload_plus_header["exact"] is False
    assert payload_plus_header["stopped_at"] is not None


def test_evaluate_layouts_picks_payload_plus_header_when_the_blob_is_built_that_way():
    """The reverse of the case above: the discriminator has to cut both ways.

    A layout table that always reported "exact" would not be evidence of
    anything; it has to be capable of saying no, and it has to say no to the
    layout the blob was *not* built for.
    """
    blob = (
        (bytes([3, 0]) + struct.pack(">ff", 33.1, -112.1) + bytes([0, 0])) * 2
        + bytes([1, 0]) + struct.pack(">ff", 33.3, -112.3)
        + struct.pack(">H", 90) + bytes([65, 0, 0])
    )
    assert len(blob) == 39
    verdicts = {v["layout"]: v for v in inspection.evaluate_layouts(blob)}

    payload_plus_header = verdicts["payload-plus-header"]
    assert payload_plus_header["exact"] is True
    assert payload_plus_header["records"] == 3
    assert payload_plus_header["bytes_consumed"] == 39

    whole_record = verdicts["whole-record"]
    assert whole_record["exact"] is False
    assert whole_record["stopped_at"] is not None


def test_a_record_that_would_run_past_the_end_of_the_blob_is_not_a_completed_walk():
    """An overshooting record must not be graded as a walk that finished cleanly.

    One well-formed user mark followed by a speed-camera marker with only 4
    bytes left -- nowhere near its declared 13 -- is exactly the shape that
    used to let ``offset += length`` step past the end of the blob and stop
    the loop with no sign anything had gone wrong.  Getting this wrong would
    let a truncated, half-read POI database be reported the same way as one
    that was read in full: the last record is expected here with
    ``fits=False``, and `evaluate_layouts` is expected to say, in its
    ``complete`` field, that this walk did not finish -- not merely that it
    was not ``exact``.
    """
    table = inspection.CANDIDATE_LAYOUTS["whole-record"]
    payload = bytes([3]) + bytes(9) + bytes([1]) + bytes(4)

    walk = inspection.record_boundaries(payload, table)
    assert walk[-1]["kind"] == "speed camera"
    assert walk[-1]["fits"] is False

    verdict = next(
        v for v in inspection.evaluate_layouts(payload) if v["layout"] == "whole-record"
    )
    assert verdict["all_fit"] is False
    assert verdict["exact"] is False
    assert verdict["complete"] is False


# ----------------------------------------------------------- the bad cases

def test_a_device_without_the_vendor_service_is_incompatible_and_nothing_is_read(
    tmp_path,
):
    """The UUIDs came from a different model; the layout is checked, not assumed."""
    client = FakeClient(services=[])
    result = _inspect(_store(tmp_path), RecordingFactory(client), confirmed=True)
    assert result.connected
    assert not result.compatible
    assert client.reads == [], "an incompatible device is not read anyway"
    assert client.descriptor_reads == []
    assert result.attributes == []
    assert result.capture_name == ""
    assert any(gatt.DATA_SERVICE_UUID in missing for missing in result.services_missing)
    assert "does not expose the required attributes" in result.render()


def test_a_read_that_raises_is_recorded_against_that_attribute_only(tmp_path):
    """Absence is evidence: one refused attribute must not cost the other two."""
    client = FakeClient(failures={gatt.SETTINGS_2_UUID})
    result = _inspect(_store(tmp_path), RecordingFactory(client), confirmed=True)

    assert client.reads == [
        gatt.SETTINGS_1_UUID, gatt.SETTINGS_2_UUID, gatt.POI_UUID,
    ]
    by_uuid = {dump.uuid: dump for dump in result.attributes}
    assert by_uuid[gatt.SETTINGS_2_UUID].error == "OSError"
    assert by_uuid[gatt.SETTINGS_2_UUID].hex == ""
    assert by_uuid[gatt.SETTINGS_2_UUID].length == 0
    assert by_uuid[gatt.SETTINGS_1_UUID].hex == SETTINGS_1_BYTES.hex()
    assert by_uuid[gatt.POI_UUID].hex == POI_BYTES.hex()
    assert not by_uuid[gatt.POI_UUID].error
    assert result.capture_name, "the attributes that did read are still captured"
    assert "OSError" in result.render()


def test_an_attribute_error_is_a_type_name_and_not_a_device_string(tmp_path):
    """An exception message from BlueZ can carry an address; the type name cannot."""
    client = FakeClient(failures={gatt.POI_UUID})
    result = _inspect(_store(tmp_path), RecordingFactory(client), confirmed=True)
    poi = [dump for dump in result.attributes if dump.uuid == gatt.POI_UUID][0]
    assert poi.error == "OSError"
    assert "firmware" not in result.render()
