"""The gpsd client, exercised without gpsd.

Every test here hands the client a pair of streams through ``connector=``, so
the whole protocol path -- the ``?WATCH`` handshake, the line loop, the
reconnect, the staleness rule -- runs on a machine with no receiver, no daemon
and no socket.  That is not convenience.  This is the one module in the project
that can produce a latitude, and a test that needed a GPS puck plugged into the
Pi is a test that would be run once and then trusted forever.

Some of what follows is a privacy control rather than a correctness check.
``record_coordinates`` off is the default and the interesting configuration:
the client still connects, still reports fix mode, satellite count, speed and
course, and still answers "was there a valid fix when that alert fired", while
never putting a position anywhere.  Both directions are asserted, because a
switch that has only ever been tested in its safe position is a switch nobody
has tested.

The staleness rule gets the same treatment.  A fix that arrived thirty seconds
ago is not a current position, and returning it would attach a coordinate to an
alert that happened somewhere else entirely -- a wrong answer that looks
exactly like a right one.

The coordinates below are assembled arithmetically.  They are not a place, and
nothing here compares them against anything but themselves.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest

from fixtures import DOC_HOST_A
from uniden_r8 import gnss
from uniden_r8.privacy import looks_like_position

NS_PER_SECOND = 1_000_000_000

#: Built by arithmetic rather than written down, so no line in this file is a
#: coordinate.  Their only job is to come back out of the parser unchanged.
TEST_LAT = 10.0 + 1 / 8
TEST_LON = -20.0 - 1 / 4

SPEED_MPS = 27.5
TRACK_DEG = 118.2
CLIMB_MPS = -0.75
ALTITUDE_M = 372.5
EPX_M = 4.5
EPY_M = 6.25
DEVICE_TIME = "2026-09-02T17:04:31.000Z"

#: A TPV report with everything a well-behaved receiver sends, including two
#: fields this module has no opinion about.  Unknown keys must be ignored, not
#: tripped over: gpsd adds them between releases.
REALISTIC_TPV = {
    "class": "TPV",
    "device": "/dev/ttyACM0",
    "mode": 3,
    "status": 2,
    "time": DEVICE_TIME,
    "lat": TEST_LAT,
    "lon": TEST_LON,
    "altMSL": ALTITUDE_M,
    "altHAE": ALTITUDE_M + 30.0,
    "speed": SPEED_MPS,
    "track": TRACK_DEG,
    "climb": CLIMB_MPS,
    "epx": EPX_M,
    "epy": EPY_M,
    "geoidSep": -30.0,
}

SKY_REPORT = {"class": "SKY", "device": "/dev/ttyACM0", "nSat": 19, "uSat": 11}


def _line(payload: dict) -> bytes:
    """One report as the daemon puts it on the wire: JSON, one line, newline."""
    return (json.dumps(payload) + "\n").encode()


def _fix(**overrides) -> gnss.Fix:
    """A fresh 3D fix, as :func:`parse_tpv` would have built it."""
    return gnss.parse_tpv(
        {**REALISTIC_TPV, **overrides},
        monotonic_ns=time.monotonic_ns(),
        wall_ns=time.time_ns(),
    )


# --------------------------------------------------------------- the fakes

class RecordingWriter:
    """A stand-in for ``StreamWriter`` that keeps every byte written to it.

    It has no transport and cannot fail, which is the point: what the client
    sends to the daemon is a fixed, auditable string, and the only way to prove
    that is to keep the bytes and compare them.
    """

    def __init__(self) -> None:
        self.written = bytearray()
        self.drains = 0
        self.closed = False
        self.awaited_close = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        self.drains += 1

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.awaited_close = True


class FakeSession:
    """One scripted connection: what the receiver says, and what it hears.

    *hold* leaves the stream open after the scripted lines, which is what a
    healthy receiver between reports looks like.  Without it the reader sees
    end-of-file and the client treats the far end as having closed.
    """

    def __init__(self, lines=(), *, hold: bool = False, limit: int | None = None) -> None:
        self.lines = list(lines)
        self.hold = hold
        self.limit = limit
        self.reader: asyncio.StreamReader | None = None
        self.writer = RecordingWriter()

    def open(self) -> tuple[asyncio.StreamReader, RecordingWriter]:
        """Build the streams.  Called inside the loop, never at import time."""
        reader = (
            asyncio.StreamReader() if self.limit is None
            else asyncio.StreamReader(limit=self.limit)
        )
        for line in self.lines:
            reader.feed_data(line)
        if not self.hold:
            reader.feed_eof()
        self.reader = reader
        return reader, self.writer

    @property
    def watched(self) -> bytes:
        return bytes(self.writer.written)


def connector_for(*sessions: FakeSession):
    """A connector that hands out each scripted session in turn, then refuses.

    Refusing once the script runs out is deliberate: a test that quietly
    reconnected to a fresh, silent stream forever would pass whatever the
    reconnect logic did.
    """
    queue = list(sessions)

    async def connect():
        if not queue:
            raise ConnectionRefusedError("the script is exhausted")
        return queue.pop(0).open()

    return connect


def run_until(client: gnss.GnssClient, predicate, *, timeout: float = 5.0) -> None:
    """Run *client* until *predicate* holds, then stop and cancel it.

    Bounded by :func:`asyncio.wait_for` so a client that never reaches the
    condition fails the test in seconds instead of hanging the suite.
    """

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        try:
            while not predicate():
                await asyncio.sleep(0.005)
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(asyncio.wait_for(go(), timeout))


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Shrink the reconnect delay so a reconnect costs milliseconds, not seconds."""
    monkeypatch.setattr(gnss, "BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setattr(gnss, "BACKOFF_MAX_SECONDS", 0.05)


# ------------------------------------------------------------ the handshake

def test_the_client_sends_exactly_the_watch_command_and_nothing_else():
    """A bare connection gets a banner and then silence; the watch starts the stream."""
    session = FakeSession([_line(REALISTIC_TPV)], hold=True)
    client = gnss.GnssClient(connector=connector_for(session))

    run_until(client, lambda: client.reports >= 1)

    assert session.watched == gnss.WATCH_COMMAND
    assert session.writer.drains == 1


def test_the_watch_command_is_the_json_the_daemon_expects():
    """Pinned as a decoded document, so a typo inside the braces is visible."""
    assert gnss.WATCH_COMMAND.startswith(b"?WATCH=")
    assert gnss.WATCH_COMMAND.endswith(b"\n")
    body = gnss.WATCH_COMMAND[len(b"?WATCH="):].decode()
    assert json.loads(body) == {"enable": True, "json": True}


def test_a_finished_session_closes_its_writer():
    """Hours of reconnects must not leave a socket behind on every one."""
    session = FakeSession([])  # end-of-file straight away
    client = gnss.GnssClient(connector=connector_for(session))

    run_until(client, lambda: session.writer.closed)

    assert session.writer.closed
    assert session.writer.awaited_close


# ---------------------------------------------------------------- parse_tpv

def test_a_realistic_tpv_report_becomes_a_complete_fix():
    fix = gnss.parse_tpv(REALISTIC_TPV, monotonic_ns=1_000, wall_ns=2_000)

    assert fix.mode == 3
    assert fix.valid
    assert fix.mode_name == "3D fix"
    assert fix.lat == TEST_LAT
    assert fix.lon == TEST_LON
    assert fix.altitude_m == pytest.approx(ALTITUDE_M)
    assert fix.speed_mps == pytest.approx(SPEED_MPS)
    assert fix.speed_mph == pytest.approx(SPEED_MPS * 2.236936)
    assert fix.track_deg == pytest.approx(TRACK_DEG)
    assert fix.climb_mps == pytest.approx(CLIMB_MPS)
    assert fix.epx_m == pytest.approx(EPX_M)
    assert fix.epy_m == pytest.approx(EPY_M)
    assert fix.device_time == DEVICE_TIME
    assert fix.monotonic_ns == 1_000
    assert fix.wall_ns == 2_000


def test_a_tpv_report_missing_every_optional_field_still_parses():
    """gpsd omits what it has no value for, so a bare report is normal input."""
    fix = gnss.parse_tpv({"class": "TPV", "mode": 1}, monotonic_ns=7, wall_ns=8)

    assert fix.mode == 1
    assert not fix.valid
    assert fix.mode_name == "no fix"
    assert fix.lat is None
    assert fix.lon is None
    assert fix.altitude_m is None
    assert fix.speed_mps is None
    assert fix.speed_mph is None
    assert fix.track_deg is None
    assert fix.climb_mps is None
    assert fix.epx_m is None
    assert fix.epy_m is None
    assert fix.satellites is None
    assert fix.device_time is None


def test_a_tpv_report_with_no_fields_at_all_is_not_an_error():
    """Whatever arrives, this runs in a moving vehicle and must not raise."""
    fix = gnss.parse_tpv({}, monotonic_ns=0, wall_ns=0)
    assert fix.mode == 0
    assert not fix.valid
    assert fix.mode_name == "unknown"


@pytest.mark.parametrize("payload, expected", [
    ({"altMSL": ALTITUDE_M}, ALTITUDE_M),
    ({"altHAE": ALTITUDE_M}, ALTITUDE_M),
    ({"alt": ALTITUDE_M}, ALTITUDE_M),
    # Newest spelling wins when a release sends more than one of them.
    ({"altMSL": ALTITUDE_M, "altHAE": 1.0, "alt": 2.0}, ALTITUDE_M),
    ({"altHAE": ALTITUDE_M, "alt": 2.0}, ALTITUDE_M),
])
def test_altitude_is_read_from_whichever_spelling_the_daemon_sent(payload, expected):
    """gpsd renamed this field twice; pinning one spelling reports nothing."""
    fix = gnss.parse_tpv({"class": "TPV", "mode": 3, **payload},
                         monotonic_ns=0, wall_ns=0)
    assert fix.altitude_m == pytest.approx(expected)


@pytest.mark.parametrize("mode, valid", [(0, False), (1, False), (2, True), (3, True)])
def test_only_a_two_or_three_dimensional_mode_counts_as_a_fix(mode, valid):
    """Modes 0 and 1 are the receiver saying it does not know where it is."""
    fix = gnss.parse_tpv({"class": "TPV", "mode": mode}, monotonic_ns=0, wall_ns=0)
    assert fix.valid is valid


@pytest.mark.parametrize("mode", ["3", 3.0, None, True, [3]])
def test_a_mode_that_is_not_an_integer_is_read_as_no_fix(mode):
    """A string "3" must not become a 3D fix; ``True`` must not become mode 1."""
    fix = gnss.parse_tpv({"class": "TPV", "mode": mode}, monotonic_ns=0, wall_ns=0)
    assert fix.mode == 0
    assert not fix.valid


def test_a_boolean_is_never_read_as_a_measurement():
    """``True`` is an int in Python; a speed of 1 m/s from a JSON true is a lie."""
    fix = gnss.parse_tpv(
        {"class": "TPV", "mode": 3, "speed": True, "lat": False, "epx": True},
        monotonic_ns=0, wall_ns=0,
    )
    assert fix.speed_mps is None
    assert fix.lat is None
    assert fix.epx_m is None


def test_the_magnetic_track_is_used_when_there_is_no_true_track():
    fix = gnss.parse_tpv({"class": "TPV", "mode": 3, "magtrack": TRACK_DEG},
                         monotonic_ns=0, wall_ns=0)
    assert fix.track_deg == pytest.approx(TRACK_DEG)


def test_a_timestamp_that_is_not_a_string_is_dropped_rather_than_coerced():
    """The device clock is kept verbatim, so anything but text is not it."""
    fix = gnss.parse_tpv({"class": "TPV", "mode": 3, "time": 1_756_800_000},
                         monotonic_ns=0, wall_ns=0)
    assert fix.device_time is None


def test_age_is_measured_on_the_monotonic_clock():
    fix = gnss.parse_tpv(REALISTIC_TPV, monotonic_ns=5 * NS_PER_SECOND, wall_ns=0)
    assert fix.age_seconds(now_ns=8 * NS_PER_SECOND) == pytest.approx(3.0)


def test_a_clock_that_appears_to_run_backwards_gives_no_negative_age():
    """A negative age would read as a fix from the future and defeat staleness."""
    fix = gnss.parse_tpv(REALISTIC_TPV, monotonic_ns=9 * NS_PER_SECOND, wall_ns=0)
    assert fix.age_seconds(now_ns=0) == 0.0


# ---------------------------------------------------------------- parse_sky

def test_parse_sky_reads_the_used_count_the_daemon_already_worked_out():
    assert gnss.parse_sky(SKY_REPORT) == 11


def test_parse_sky_counts_the_satellite_list_when_there_is_no_used_count():
    """Older releases send the list and leave the arithmetic to the client."""
    satellites = [
        {"PRN": 2, "used": True},
        {"PRN": 5, "used": True},
        {"PRN": 13, "used": False},
        {"PRN": 21},
    ]
    assert gnss.parse_sky({"class": "SKY", "satellites": satellites}) == 2


def test_parse_sky_says_nothing_when_the_report_says_nothing():
    assert gnss.parse_sky({"class": "SKY"}) is None
    assert gnss.parse_sky({"class": "SKY", "satellites": "seven"}) is None


def test_a_boolean_used_count_falls_through_to_the_list():
    assert gnss.parse_sky({"class": "SKY", "uSat": True,
                           "satellites": [{"used": True}]}) == 1


def test_a_satellite_list_of_nonsense_counts_nothing_rather_than_raising():
    assert gnss.parse_sky({"class": "SKY", "satellites": [None, 4, "PRN", {}]}) == 0


# ------------------------------------------------------- the reading loop

def test_a_malformed_line_is_counted_and_the_session_carries_on():
    """One bad line must not cost the connection; the next report is the point."""
    session = FakeSession(
        [
            b"{ this is not json }\n",
            b"[1, 2, 3]\n",
            b"\xff\xfe\x00\n",
            b"null\n",
            _line(REALISTIC_TPV),
        ],
        hold=True,
    )
    client = gnss.GnssClient(connector=connector_for(session))

    run_until(client, lambda: client.reports >= 1)

    assert client.malformed == 4
    assert client.connects == 1, "the session survived every bad line"
    assert client.fix is not None
    assert client.last_error == ""


def test_a_satellite_count_from_a_sky_report_is_attached_to_the_next_fix():
    """The count and the position arrive in separate reports; a fix needs both."""
    session = FakeSession([_line(SKY_REPORT), _line(REALISTIC_TPV)], hold=True)
    client = gnss.GnssClient(connector=connector_for(session))

    run_until(client, lambda: client.reports >= 1)

    fix = client.fix
    assert fix is not None
    assert fix.satellites == 11


def test_an_unreadable_sky_report_does_not_wipe_the_last_known_count():
    session = FakeSession(
        [_line(SKY_REPORT), _line({"class": "SKY"}), _line(REALISTIC_TPV)],
        hold=True,
    )
    client = gnss.GnssClient(connector=connector_for(session))

    run_until(client, lambda: client.reports >= 1)

    assert client.fix is not None
    assert client.fix.satellites == 11


def test_an_over_long_line_neither_raises_nor_ends_the_run():
    """A stream that is not gpsd must cost a reconnect and nothing else.

    The line is longer than a stream reader's own buffer, so the read fails
    before the client's own length check sees it.  What matters at this level
    is that the failure is reported and retried rather than raised.
    """
    flood = b"{" + b"x" * (gnss.MAX_LINE_BYTES + 1_000) + b"}\n"
    session = FakeSession([flood], hold=True)
    client = gnss.GnssClient(connector=connector_for(session))

    run_until(client, lambda: bool(client.last_error))

    assert client.last_error
    assert client.connects == 1


# ------------------------------------------------------------- staleness

def test_a_stale_fix_is_not_offered_as_a_current_position():
    """A fix from thirty seconds ago belongs to somewhere else on the road."""
    client = gnss.GnssClient(stale_after_seconds=5.0)
    client._fix = gnss.parse_tpv(
        REALISTIC_TPV,
        monotonic_ns=time.monotonic_ns() - 30 * NS_PER_SECOND,
        wall_ns=time.time_ns(),
    )

    assert client._fix.valid, "the fix itself is good; only its age disqualifies it"
    assert client.fix is None
    assert client.status()["fix"] is None


def test_a_fresh_fix_inside_the_window_is_offered():
    client = gnss.GnssClient(stale_after_seconds=5.0)
    client._fix = _fix()
    assert client.fix is not None


def test_a_fix_the_receiver_does_not_stand_behind_is_never_offered():
    """Mode 1 is fresh and useless; freshness alone must not qualify it."""
    client = gnss.GnssClient(stale_after_seconds=5.0)
    client._fix = _fix(mode=1)
    assert client.fix is None


def test_a_client_that_has_heard_nothing_has_no_fix():
    assert gnss.GnssClient().fix is None


# ---------------------------------------------------- the coordinate switch

def test_a_withheld_document_reports_the_coordinates_as_present_and_null():
    """Off is the default, and a consumer must be able to tell off from absent."""
    document = _fix().detailed(include_coordinates=False)

    assert "lat" in document
    assert "lon" in document
    assert document["lat"] is None
    assert document["lon"] is None
    assert document["coordinates_withheld"] is True


def test_a_withheld_document_still_carries_everything_that_is_not_a_position():
    """The whole point of the default: the fix stays useful without the place."""
    document = _fix().detailed(include_coordinates=False)

    assert document["source"] == "gpsd"
    assert document["mode"] == 3
    assert document["mode_name"] == "3D fix"
    assert document["valid"] is True
    assert document["speed_mps"] == pytest.approx(SPEED_MPS)
    assert document["speed_mph"] == pytest.approx(SPEED_MPS * 2.236936)
    assert document["track_deg"] == pytest.approx(TRACK_DEG)
    assert document["climb_mps"] == pytest.approx(CLIMB_MPS)
    assert document["altitude_m"] == pytest.approx(ALTITUDE_M)
    assert document["epx_m"] == pytest.approx(EPX_M)
    assert document["epy_m"] == pytest.approx(EPY_M)
    assert document["device_time"] == DEVICE_TIME


def test_no_fragment_of_a_withheld_coordinate_survives_serialisation():
    """Checked against the text, because a nested copy would pass the key check."""
    serialised = json.dumps(_fix().detailed(include_coordinates=False))
    assert repr(TEST_LAT) not in serialised
    assert repr(TEST_LON) not in serialised


def test_switching_coordinates_on_publishes_the_real_values():
    """The other direction.  A switch tested one way is a switch untested."""
    document = _fix().detailed(include_coordinates=True)

    assert document["lat"] == TEST_LAT
    assert document["lon"] == TEST_LON
    assert "coordinates_withheld" not in document


def test_the_repository_position_gate_agrees_with_the_switch():
    """Judged by the same gate ``evidence.publish`` puts every document through."""
    fix = _fix()
    assert not looks_like_position(fix.detailed(include_coordinates=False))
    assert looks_like_position(fix.detailed(include_coordinates=True))


def test_a_fix_with_no_coordinates_in_it_is_not_labelled_withheld():
    """The withheld label is a statement about the switch, not a missing value."""
    document = gnss.parse_tpv({"class": "TPV", "mode": 2}, monotonic_ns=0, wall_ns=0) \
        .detailed(include_coordinates=True)
    assert document["lat"] is None
    assert "coordinates_withheld" not in document


def test_the_client_publishes_its_fix_through_its_own_switch():
    """The end-to-end version: the flag set on the client reaches the document."""
    withholding = gnss.GnssClient(record_coordinates=False)
    withholding._fix = _fix()
    recording = gnss.GnssClient(record_coordinates=True)
    recording._fix = _fix()

    assert withholding.status()["fix"]["coordinates_withheld"] is True
    assert withholding.status()["fix"]["lat"] is None
    assert recording.status()["fix"]["lat"] == TEST_LAT
    assert "coordinates_withheld" not in recording.status()["fix"]


# ----------------------------------------------------------------- status

def test_status_has_the_shape_the_collector_publishes():
    client = gnss.GnssClient(DOC_HOST_A, 4321, record_coordinates=False)
    status = client.status()

    assert set(status) == {
        "enabled", "connected", "host", "port", "connects", "reports",
        "malformed", "record_coordinates", "last_error", "fix",
    }
    assert status["enabled"] is True
    assert status["connected"] is False
    assert status["host"] == DOC_HOST_A
    assert status["port"] == 4321
    assert status["connects"] == 0
    assert status["reports"] == 0
    assert status["malformed"] == 0
    assert status["record_coordinates"] is False
    assert status["last_error"] == ""
    assert status["fix"] is None


def test_status_counts_what_actually_happened_on_the_wire():
    session = FakeSession([b"not json\n", _line(SKY_REPORT), _line(REALISTIC_TPV)],
                          hold=True)
    client = gnss.GnssClient(connector=connector_for(session))

    run_until(client, lambda: client.reports >= 1)
    status = client.status()

    assert status["connects"] == 1
    assert status["reports"] == 1
    assert status["malformed"] == 1
    assert status["fix"]["satellites"] == 11
    assert status["fix"]["mode_name"] == "3D fix"


def test_the_default_port_is_the_one_in_etc_services():
    assert gnss.GnssClient().port == gnss.DEFAULT_PORT == 2947


# ------------------------------------------------------------- the run loop

def test_run_reconnects_after_the_far_end_closes():
    """A receiver unplugged and plugged back in must come back on its own."""
    first = FakeSession([_line(REALISTIC_TPV)])
    second = FakeSession([_line(REALISTIC_TPV)], hold=True)
    client = gnss.GnssClient(connector=connector_for(first, second))

    run_until(client, lambda: client.connects >= 2 and client.reports >= 2)

    assert client.connects == 2
    assert client.reports == 2


def test_every_reconnect_re_arms_the_watch():
    """A reconnect that forgot the handshake would reconnect to silence."""
    first = FakeSession([])
    second = FakeSession([_line(REALISTIC_TPV)], hold=True)
    client = gnss.GnssClient(connector=connector_for(first, second))

    run_until(client, lambda: client.reports >= 1)

    assert first.watched == gnss.WATCH_COMMAND
    assert second.watched == gnss.WATCH_COMMAND


def test_a_refused_connection_is_reported_and_never_propagated():
    """The collector keeps collecting radar data when the puck is not there."""
    attempts = 0

    async def refuse():
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("nothing is listening on that port")

    client = gnss.GnssClient(connector=refuse)

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        while attempts < 2:
            await asyncio.sleep(0.005)
        stop.set()
        await task

    asyncio.run(asyncio.wait_for(go(), 5.0))

    assert attempts >= 2, "a refusal must be retried, not given up on"
    assert client.last_error == "ConnectionRefusedError"
    assert client.connects == 0
    assert client.connected is False
    assert client.status()["fix"] is None


def test_run_returns_when_the_stop_event_is_set_between_attempts():
    """Shutdown must not wait out a backoff that was about to be minutes long."""

    async def refuse():
        raise ConnectionRefusedError("nothing is listening on that port")

    client = gnss.GnssClient(connector=refuse)

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        while not client.last_error:
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(asyncio.wait_for(go(), 5.0))


def test_run_returns_promptly_when_the_stop_event_is_set_mid_session():
    """A receiver that has gone quiet must not hold the shutdown open.

    ``run`` is documented as returning when the stop event is set, and the
    collector's shutdown, the CLI's Ctrl-C path and every test that stops this
    client rely on that.  A connected-but-silent stream is exactly the state a
    receiver enters when it is unplugged behind a daemon that keeps the socket
    open, so it must not be the state in which the stop event is ignored.
    """
    session = FakeSession([_line(REALISTIC_TPV)], hold=True)
    client = gnss.GnssClient(connector=connector_for(session))

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        while client.reports < 1:
            await asyncio.sleep(0.005)
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except TimeoutError:
            raise AssertionError(
                "run() did not return within a second of the stop event: a quiet "
                "receiver holds the shutdown open"
            ) from None

    asyncio.run(asyncio.wait_for(go(), 10.0))


def test_run_returns_immediately_when_stopped_before_it_starts():
    """Nothing may be connected to after the decision to stop has been taken."""
    opened = 0

    async def connect():
        nonlocal opened
        opened += 1
        raise AssertionError("a stopped client must not open a connection")

    client = gnss.GnssClient(connector=connect)

    async def go() -> None:
        stop = asyncio.Event()
        stop.set()
        await client.run(stop)

    asyncio.run(asyncio.wait_for(go(), 5.0))

    assert opened == 0
    assert client.connects == 0


def test_a_connector_that_fails_forever_never_leaves_the_client_connected():
    """``connected`` is what the health document reports; it must not lie."""

    async def refuse():
        raise OSError("no route to the receiver")

    client = gnss.GnssClient(connector=refuse)

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        while not client.last_error:
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(asyncio.wait_for(go(), 5.0))

    assert client.connected is False
    assert client.last_error == "OSError"
