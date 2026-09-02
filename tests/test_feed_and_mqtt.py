"""The two publication sinks: the local feed and the MQTT bridge.

Neither of these is allowed to matter.  The detector's subscription is the
product; a browser propped on the dash and a broker in a garage are
conveniences, and most of what is pinned here is that they degrade rather than
interfere -- a port already in use is reported and not raised, a broker that
refuses a publish is counted and not propagated, a viewer on a bad link costs
itself its stream and nothing else.

Neither half needs a network in the sense that matters.  The feed is bound to
an ephemeral port on loopback and talked to over a real socket, because the
thing worth checking is the bytes on the wire: an SSE frame a browser will not
parse is not caught by asserting on a Python object.  The MQTT half opens no
socket at all -- ``client_factory=`` takes a recorder -- because every decision
worth checking there is a decision about the *arguments* handed to paho: which
topic, which retain flag, and in what order.

Two invariants are the reason the file exists.  Retention is asymmetric on
purpose, and the two calls that set it are one keyword apart in the source.
And a per-client backlog must drop its oldest frames rather than grow, because
that queue shares an event loop with the BLE subscription; the overflow test
forces the condition synchronously, so the bound is exercised rather than
raced against.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from fixtures import DOC_HOST_A
from uniden_r8.feed import CLIENT_QUEUE, INDEX_HTML, MAX_CLIENTS, StateFeed
from uniden_r8.mqtt import QOS, MqttPublisher, MqttUnavailable, topic_map
from uniden_r8.privacy import looks_like_identifier

#: Loopback, and exempt from the repository's address check by
#: ``privacy.is_non_identifying_host``.  Spelled as the literal rather than
#: ``localhost`` so the bind resolves to exactly one socket: with a name that
#: has both an A and a AAAA record, port 0 can be answered with a different
#: ephemeral port per family, and the test would then talk to the wrong one.
HOST = "127.0.0.1"

#: Every wait in this file is bounded.  A suite that can hang is a suite people
#: stop running, and these tests hold real sockets open.
TIMEOUT = 5.0


# ---------------------------------------------------------------- the feed


def _port_of(feed: StateFeed) -> int:
    """The ephemeral port the feed actually bound.

    ``status()`` reports the port that was *asked for*, which is zero here, so
    the socket is the only place the real number exists.
    """
    return feed._server.sockets[0].getsockname()[1]  # noqa: SLF001


async def _stopped(feed: StateFeed) -> None:
    """Stop the feed, with a bound on the wait.

    ``stop()`` waits for the server and the server waits for its handler tasks,
    so a stream that is never told to close makes this hang rather than fail.
    The bound turns that regression into a red test instead of a suite someone
    has to interrupt.
    """
    await asyncio.wait_for(feed.stop(), TIMEOUT)


@contextlib.asynccontextmanager
async def _serving(**kwargs: Any):
    """A started feed on a loopback port nobody else is using."""
    feed = StateFeed(HOST, 0, **kwargs)
    assert await feed.start(), feed.last_error
    try:
        yield feed, _port_of(feed)
    finally:
        await _stopped(feed)


def _get(path: str) -> bytes:
    return f"GET {path} HTTP/1.1\r\nHost: r8\r\n\r\n".encode()


async def _fetch(port: int, request: bytes) -> bytes:
    """Send one raw request and read the whole answer, to end of file."""
    reader, writer = await asyncio.open_connection(HOST, port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), TIMEOUT)
    finally:
        _shutdown(writer)


def _shutdown(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()


def _status_line(response: bytes) -> str:
    return response.split(b"\r\n", 1)[0].decode("latin-1")


def _body(response: bytes) -> bytes:
    return response.split(b"\r\n\r\n", 1)[1]


async def _open_stream(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open ``/events`` and swallow the response headers, leaving the frames.

    Returning only after the headers arrive matters: the server registers the
    client before it writes them, so a caller that has read this far knows the
    viewer is counted.
    """
    reader, writer = await asyncio.open_connection(HOST, port)
    writer.write(_get("/events"))
    await writer.drain()
    headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
    assert b"text/event-stream" in headers, headers
    return reader, writer


async def _next_frame(reader: asyncio.StreamReader) -> tuple[str, Any]:
    """One SSE frame, up to the blank line that terminates it."""
    raw = await asyncio.wait_for(reader.readuntil(b"\n\n"), TIMEOUT)
    name = ""
    data = ""
    for line in raw.decode("utf-8").strip().splitlines():
        field, _, value = line.partition(": ")
        if field == "event":
            name = value
        elif field == "data":
            data = value
    return name, json.loads(data)


def test_the_index_page_is_served_whole_and_asks_the_internet_for_nothing():
    """A dashboard that fetches a script from a CDN is blank in a tunnel."""

    async def go():
        async with _serving() as (_, port):
            return await _fetch(port, _get("/"))

    response = asyncio.run(go())
    assert _status_line(response) == "HTTP/1.1 200 OK"
    assert b"text/html" in response
    page = _body(response).decode("utf-8")
    assert page == INDEX_HTML
    assert "http://" not in page and "https://" not in page


def test_healthz_answers_ok_so_a_watchdog_can_tell_the_port_is_alive():
    async def go():
        async with _serving() as (_, port):
            return await _fetch(port, _get("/healthz"))

    response = asyncio.run(go())
    assert _status_line(response) == "HTTP/1.1 200 OK"
    assert _body(response) == b"ok\n"


def test_state_serves_the_document_that_was_published_last():
    async def go():
        async with _serving() as (feed, port):
            feed.publish_state({"collector": {"status": "linked"}, "alerts": []})
            first = await _fetch(port, _get("/state"))
            feed.publish_state({"collector": {"status": "stopped"}, "alerts": []})
            return first, await _fetch(port, _get("/state"))

    first, second = asyncio.run(go())
    assert b"application/json" in first
    assert json.loads(_body(first))["collector"]["status"] == "linked"
    assert json.loads(_body(second))["collector"]["status"] == "stopped"


def test_state_is_valid_json_before_anything_has_been_published():
    """A dashboard opened before the first packet must not see a parse error."""

    async def go():
        async with _serving() as (_, port):
            return await _fetch(port, _get("/state"))

    assert json.loads(_body(asyncio.run(go()))) == {}


def test_a_document_json_cannot_encode_still_reaches_the_dashboard():
    """The dashboard is often how someone notices the document is wrong.

    Serialising with ``default=str`` rather than failing is deliberate: a state
    document that cannot be encoded is a bug worth seeing, and an empty panel
    says nothing about it.
    """

    async def go():
        async with _serving() as (feed, port):
            feed.publish_state({"when": object(), "voltage": 13.6})
            return await _fetch(port, _get("/state"))

    document = json.loads(_body(asyncio.run(go())))
    assert document["voltage"] == 13.6
    assert isinstance(document["when"], str)


def test_an_unknown_path_is_a_404_and_not_the_dashboard():
    async def go():
        async with _serving() as (_, port):
            return await _fetch(port, _get("/../etc/passwd"))

    response = asyncio.run(go())
    assert _status_line(response) == "HTTP/1.1 404 Not Found"
    assert b"<!doctype html>" not in _body(response)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_a_request_that_would_change_something_is_refused(method):
    """Nothing here accepts input, and the port a phone can reach is the last
    place to start."""

    async def go():
        async with _serving() as (_, port):
            request = f"{method} / HTTP/1.1\r\nHost: r8\r\nContent-Length: 0\r\n\r\n"
            return await _fetch(port, request.encode())

    response = asyncio.run(go())
    assert _status_line(response) == "HTTP/1.1 405 Method Not Allowed"
    assert _body(response) == b"method not allowed\n"


def test_an_oversized_request_is_closed_quietly_rather_than_raised_at_the_loop():
    """A request too large to read must cost a closed socket and nothing else.

    ``_handle`` bounds the request deliberately -- this server answers three
    routes, and anything larger is not a request it needs to read -- and it
    catches the failures that reading one can raise so the socket is closed and
    forgotten.  ``asyncio.LimitOverrunError`` is not among them: it descends
    from ``Exception`` directly rather than from ``ValueError`` or ``OSError``,
    so a header longer than the stream reader's own limit escapes the handler
    and is reported to the event loop instead.  That loop is the one holding
    the detector's subscription, which is the whole reason this module catches
    rather than propagates.
    """

    async def go():
        reached_the_loop = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reached_the_loop.append(context)
        )
        async with _serving() as (_, port):
            reader, writer = await asyncio.open_connection(HOST, port)
            writer.write(b"GET / HTTP/1.1\r\nX-Long: " + b"a" * (64 * 1024 + 1))
            await writer.drain()
            answer = await asyncio.wait_for(reader.read(), TIMEOUT)
            _shutdown(writer)
        return reached_the_loop, answer

    reached_the_loop, answer = asyncio.run(go())
    assert answer == b"", "an unreadable request must not be answered"
    assert reached_the_loop == [], (
        "the request reached the event loop: "
        + "; ".join(str(context.get("message")) for context in reached_the_loop)
    )


def test_an_events_stream_opens_with_the_state_that_is_already_current():
    """A viewer joining mid-drive is shown the picture, not a blank page."""

    async def go():
        async with _serving() as (feed, port):
            feed.publish_state({"collector": {"status": "linked"}})
            reader, writer = await _open_stream(port)
            frame = await _next_frame(reader)
            _shutdown(writer)
            return frame

    assert asyncio.run(go()) == ("state", {"collector": {"status": "linked"}})


def test_an_alert_reaches_an_open_stream_as_its_own_event():
    """This is the reason the feed exists: a threat can last four seconds."""

    async def go():
        async with _serving() as (feed, port):
            reader, writer = await _open_stream(port)
            opening = await _next_frame(reader)
            feed.publish_event({"kind": "alert_start", "alert": {"band": "KA"}})
            alert = await _next_frame(reader)
            feed.publish_state({"collector": {"status": "linked"}})
            update = await _next_frame(reader)
            _shutdown(writer)
            return opening, alert, update

    opening, alert, update = asyncio.run(go())
    assert opening[0] == "state"
    assert alert == ("alert", {"kind": "alert_start", "alert": {"band": "KA"}})
    assert update == ("state", {"collector": {"status": "linked"}})


def test_the_viewer_after_the_last_one_is_refused_rather_than_quietly_queued():
    """More than MAX_CLIENTS viewers on a Pi Zero is a mistake, not a use case.

    The refusal must also leave the viewers who were already there untouched:
    the connection that arrives too late is the only one that pays.
    """

    async def go():
        async with _serving() as (feed, port):
            streams = []
            for _ in range(MAX_CLIENTS):
                reader, writer = await _open_stream(port)
                await _next_frame(reader)
                streams.append((reader, writer))
            refused = await _fetch(port, _get("/events"))
            feed.publish_event({"kind": "alert_start"})
            survivor = await _next_frame(streams[0][0])
            status = feed.status()
            for _, writer in streams:
                _shutdown(writer)
            return refused, survivor, status

    refused, survivor, status = asyncio.run(go())
    assert _status_line(refused) == "HTTP/1.1 503 Service Unavailable"
    assert _body(refused) == b"too many viewers\n"
    assert survivor == ("alert", {"kind": "alert_start"})
    assert status["clients"] == MAX_CLIENTS
    assert status["refused"] == 1
    assert status["served"] == MAX_CLIENTS + 1


def test_a_viewer_that_falls_behind_loses_its_oldest_frames_not_its_newest():
    """A viewer that fell behind wants the current threat, not a replay.

    The flood is synchronous on purpose.  Nothing is awaited between the
    publishes, so the per-client task cannot drain and the backlog is genuinely
    forced past its bound instead of being raced against the loop.
    """
    overflow = 20
    total = CLIENT_QUEUE + overflow

    async def go():
        async with _serving() as (feed, port):
            reader, writer = await _open_stream(port)
            await _next_frame(reader)
            for index in range(total):
                feed.publish_event({"kind": "alert_start", "n": index})
            dropped = feed.status()["dropped_frames"]
            seen = [(await _next_frame(reader))[1]["n"] for _ in range(CLIENT_QUEUE)]
            _shutdown(writer)
            return dropped, seen

    dropped, seen = asyncio.run(go())
    assert dropped == overflow, "the backlog grew instead of dropping"
    assert seen == list(range(overflow, total)), "the wrong end of the queue was lost"


def test_stop_closes_every_open_stream():
    """A collector shutting down may not leave a browser holding a socket."""

    async def go():
        async with _serving() as (feed, port):
            streams = []
            for _ in range(3):
                reader, writer = await _open_stream(port)
                await _next_frame(reader)
                streams.append((reader, writer))
            await _stopped(feed)
            status = feed.status()
            tails = [
                await asyncio.wait_for(reader.read(), TIMEOUT)
                for reader, _ in streams
            ]
            for _, writer in streams:
                _shutdown(writer)
            return status, tails

    status, tails = asyncio.run(go())
    assert tails == [b"", b"", b""], "a stream survived stop()"
    assert status["clients"] == 0
    assert status["enabled"] is False


def test_a_port_already_in_use_is_reported_and_not_raised():
    """Radar data is the product; the dashboard is a convenience.

    A second collector started by hand while the service is running must fail
    to bind and carry on collecting, not take the link down with it.
    """

    async def go():
        async with _serving() as (_, port):
            second = StateFeed(HOST, port)
            started = await second.start()
            return started, second.last_error, second.status()

    started, error, status = asyncio.run(go())
    assert started is False
    assert str(status["port"]) in error, error
    assert status["enabled"] is False
    assert status["clients"] == 0


def test_publishing_with_nobody_watching_is_synchronous_and_silent():
    """Both calls happen on the loop that holds the BLE subscription.

    So neither may return something to await and neither may raise: a state
    push on every cycle must cost nothing when no browser is connected, which
    is the normal case for a drive.
    """
    feed = StateFeed(HOST, 0)
    assert feed.publish_state({"collector": {"status": "linked"}}) is None
    assert feed.publish_event({"kind": "alert_end", "duration_s": 4.0}) is None
    status = feed.status()
    assert status["clients"] == 0
    assert status["enabled"] is False
    assert status["dropped_frames"] == 0


def test_status_counts_every_request_it_answered():
    async def go():
        async with _serving() as (feed, port):
            for path in ("/", "/healthz", "/state", "/nowhere"):
                await _fetch(port, _get(path))
            return feed.status()

    status = asyncio.run(go())
    assert status["served"] == 4
    assert status["refused"] == 0
    assert status["bind"] == HOST
    assert status["last_error"] == ""


# ---------------------------------------------------------------- the mqtt


class RecordingClient:
    """A paho stand-in that records instead of connecting.

    Deliberately not a mock.  The order of ``will_set``, ``connect`` and
    ``loop_start`` is most of what is being tested here, and an object that
    answers every attribute would let a misspelled method name look like a
    pass -- which is exactly the failure this seam exists to rule out.
    """

    def __init__(self, *, publish_error=None, connect_error=None) -> None:
        self.calls: list[str] = []
        self.published: list[tuple[str, Any, int, bool]] = []
        self.will: tuple[Any, ...] | None = None
        self.credentials: tuple[str, str | None] | None = None
        self.connected_to: tuple[str, int, int] | None = None
        self._publish_error = publish_error
        self._connect_error = connect_error

    def will_set(self, topic, payload, qos=0, retain=False):
        self.calls.append("will_set")
        self.will = (topic, payload, qos, retain)

    def username_pw_set(self, username, password=None):
        self.calls.append("username_pw_set")
        self.credentials = (username, password)

    def tls_set(self):
        self.calls.append("tls_set")

    def connect(self, host, port, keepalive=60):
        self.calls.append("connect")
        if self._connect_error is not None:
            raise self._connect_error
        self.connected_to = (host, port, keepalive)

    def loop_start(self):
        self.calls.append("loop_start")

    def loop_stop(self):
        self.calls.append("loop_stop")

    def disconnect(self):
        self.calls.append("disconnect")

    def publish(self, topic, payload, qos=0, retain=False):
        self.calls.append("publish")
        if self._publish_error is not None:
            raise self._publish_error
        self.published.append((topic, payload, qos, retain))


def _publisher(client: RecordingClient, **kwargs: Any) -> MqttPublisher:
    return MqttPublisher(client_factory=lambda: client, **kwargs)


def _sent(client: RecordingClient, topic: str) -> list[tuple[Any, bool]]:
    """Every ``(payload, retain)`` published to *topic*, in order."""
    return [
        (payload, retain)
        for name, payload, _qos, retain in client.published
        if name == topic
    ]


def _topics(client: RecordingClient) -> list[str]:
    return [topic for topic, *_ in client.published]


def test_the_will_is_registered_before_the_connection_because_after_is_too_late():
    """A status topic that can only ever say online is not a status topic.

    The will is what the broker publishes when this process dies without saying
    goodbye -- a crash, a flat battery, an engine off -- and it can only be
    registered on a connection that has not been made yet.
    """
    client = RecordingClient()
    publisher = _publisher(client)
    assert publisher.start() is True
    assert client.calls.index("will_set") < client.calls.index("connect")
    assert client.will == (publisher.topics["status"], "offline", QOS, True)


def test_start_announces_itself_online_on_both_status_topics():
    """A person subscribes to status; Home Assistant watches availability."""
    client = RecordingClient()
    publisher = _publisher(client)
    publisher.start()
    assert _sent(client, publisher.topics["status"]) == [("online", True)]
    assert _sent(client, publisher.topics["availability"]) == [("online", True)]
    assert publisher.status()["connected"] is True


def test_the_network_thread_is_started_only_after_the_connection_is_made():
    """paho's publish() only enqueues; the thread it owns is what sends.

    That threading is the reason this library was chosen: the asyncio loop
    holding the BLE subscription must never wait on a socket to a broker that
    has gone away.
    """
    client = RecordingClient()
    _publisher(client).start()
    assert client.calls.index("connect") < client.calls.index("loop_start")
    assert client.calls.index("loop_start") < client.calls.index("publish")


def test_state_is_retained_so_a_dashboard_joining_mid_drive_sees_something():
    client = RecordingClient()
    publisher = _publisher(client)
    publisher.start()
    publisher.publish_state({"telemetry": {"voltage": 13.6}, "alerts": []})
    payload, retain = _sent(client, publisher.topics["state"])[-1]
    assert retain is True
    assert json.loads(payload)["telemetry"]["voltage"] == 13.6


def test_an_alert_is_not_retained_because_a_replayed_threat_is_a_false_alarm():
    """The asymmetry with publish_state is the whole point of the pair.

    A retained alert is a broker handing an hour-old KA hit to every client
    that subscribes, and a false alarm at speed is worse than silence.  The two
    calls differ by one keyword in the source, so it is pinned here.
    """
    client = RecordingClient()
    publisher = _publisher(client)
    publisher.start()
    publisher.publish_state({"alerts": []})
    publisher.publish_event({"kind": "alert_start", "alert": {"band": "KA"}})
    state_retain = _sent(client, publisher.topics["state"])[-1][1]
    alert_payload, alert_retain = _sent(client, publisher.topics["alert"])[-1]
    assert alert_retain is False
    assert state_retain is True
    assert json.loads(alert_payload)["alert"]["band"] == "KA"


def test_stop_says_goodbye_before_it_stops_the_thread_that_would_deliver_it():
    """Order matters: the farewell has to be sent while a sender still exists."""
    client = RecordingClient()
    publisher = _publisher(client)
    publisher.start()
    publisher.stop()
    assert _sent(client, publisher.topics["status"])[-1] == ("offline", True)
    assert _sent(client, publisher.topics["availability"])[-1] == ("offline", True)
    last_publish = max(i for i, call in enumerate(client.calls) if call == "publish")
    assert last_publish < client.calls.index("loop_stop")
    assert client.calls.index("loop_stop") < client.calls.index("disconnect")


def test_stop_is_safe_to_call_twice_because_two_paths_lead_to_it():
    """A signal handler and a `finally` both call it; neither may fail."""
    client = RecordingClient()
    publisher = _publisher(client)
    publisher.start()
    publisher.stop()
    after_first = list(client.calls)
    publisher.stop()
    assert client.calls == after_first, "the second stop touched the client"
    assert publisher.status()["enabled"] is False
    assert publisher.status()["connected"] is False


def test_a_broker_that_refuses_a_publish_is_counted_and_never_raises():
    """A broker outage degrades to nothing published, never to nothing logged.

    The collector calls these on the loop that holds the BLE subscription, so
    an exception out of publish() would take the radar data with it.
    """
    client = RecordingClient(publish_error=OSError("broker went away"))
    publisher = _publisher(client)
    assert publisher.start() is True
    publisher.publish_state({"telemetry": {"voltage": 13.6}})
    publisher.publish_event({"kind": "alert_start"})
    publisher.stop()
    status = publisher.status()
    assert client.published == []
    assert status["published"] == 0
    assert status["errors"] == 6
    assert status["last_error"] == "OSError"


def test_a_connection_that_fails_leaves_the_publisher_inert_not_half_open():
    client = RecordingClient(connect_error=OSError("no route to the broker"))
    publisher = _publisher(client)
    assert publisher.start() is False
    assert "loop_start" not in client.calls
    publisher.publish_state({"telemetry": {"voltage": 13.6}})
    publisher.publish_event({"kind": "alert_start"})
    publisher.stop()
    assert client.published == [], "published through a client that never connected"
    status = publisher.status()
    assert status["enabled"] is False
    assert status["connected"] is False
    assert status["errors"] == 1
    assert status["last_error"] == "OSError"


def test_a_missing_paho_is_reported_rather_than_ending_the_drive():
    """paho is an optional extra, so its absence is a normal configuration."""

    def factory():
        raise MqttUnavailable("MQTT publication needs paho-mqtt, an optional extra")

    publisher = MqttPublisher(client_factory=factory)
    assert publisher.start() is False
    assert "optional extra" in publisher.last_error
    assert publisher.status()["enabled"] is False


def test_credentials_are_offered_only_when_a_username_is_configured():
    anonymous = RecordingClient()
    _publisher(anonymous).start()
    assert "username_pw_set" not in anonymous.calls

    named = RecordingClient()
    _publisher(named, username="collector", password="not-a-real-password").start()
    assert named.credentials == ("collector", "not-a-real-password")
    assert named.calls.index("username_pw_set") < named.calls.index("connect")


def test_tls_is_negotiated_before_the_connection_or_not_at_all():
    plain = RecordingClient()
    _publisher(plain).start()
    assert "tls_set" not in plain.calls

    secured = RecordingClient()
    _publisher(secured, tls=True).start()
    assert secured.calls.index("tls_set") < secured.calls.index("connect")


def test_the_broker_host_and_port_are_handed_to_the_client_not_guessed():
    client = RecordingClient()
    publisher = _publisher(client, host=DOC_HOST_A, port=8883)
    assert publisher.start() is True
    assert client.connected_to == (DOC_HOST_A, 8883, 60)


def test_home_assistant_discovery_is_emitted_only_when_it_is_asked_for():
    quiet = RecordingClient()
    _publisher(quiet).start()
    assert [t for t in _topics(quiet) if t.startswith("homeassistant/")] == []

    loud = RecordingClient()
    _publisher(loud, home_assistant=True).start()
    announced = [t for t in _topics(loud) if t.startswith("homeassistant/")]
    assert len(announced) == 4
    assert all(topic.endswith("/config") for topic in announced)
    assert {topic.split("/")[1] for topic in announced} == {"sensor", "binary_sensor"}


def test_every_discovery_document_names_an_availability_topic_and_a_device():
    """Without availability a dashboard shows a stale voltage forever.

    The device block is what groups the entities into one thing in the user
    interface; without it they arrive as four unrelated sensors.
    """
    client = RecordingClient()
    publisher = _publisher(client, home_assistant=True)
    publisher.start()

    documents = [
        (topic, json.loads(payload), retain)
        for topic, payload, _qos, retain in client.published
        if topic.startswith("homeassistant/")
    ]
    assert documents
    for topic, document, retain in documents:
        assert retain is True, f"{topic} would not survive a broker restart"
        assert document["availability_topic"] == publisher.topics["availability"]
        assert document["state_topic"] == publisher.topics["state"]
        assert document["device"]["identifiers"] == ["unidenr8"]
        assert document["unique_id"].startswith("unidenr8_")


@pytest.mark.parametrize("base", ["unidenr8", "/unidenr8", "unidenr8/", "//unidenr8//"])
def test_a_base_topic_with_stray_slashes_lands_on_the_same_topics(base):
    """An operator's configuration file will have a trailing slash eventually."""
    assert topic_map(base) == topic_map("unidenr8")


@pytest.mark.parametrize("base", ["", "/", "///"])
def test_an_empty_base_falls_back_instead_of_publishing_at_the_root(base):
    """A topic named `/status` is a different topic from `status`, and worse."""
    topics = topic_map(base)
    assert topics["root"] == "unidenr8"
    assert [name for name in topics.values() if name.startswith("/")] == []


def test_a_nested_base_keeps_the_slashes_that_are_inside_it():
    topics = topic_map("/home/garage/r8/")
    assert topics["root"] == "home/garage/r8"
    assert topics["alert"] == "home/garage/r8/alert"
    assert topics["status"] == "home/garage/r8/status"


def test_the_configured_base_is_what_is_actually_published_to():
    """Everything except discovery, whose prefix belongs to Home Assistant."""
    client = RecordingClient()
    publisher = _publisher(client, base_topic="/home/garage/r8/", home_assistant=True)
    publisher.start()
    publisher.publish_state({"alerts": []})
    publisher.publish_event({"kind": "alert_start"})
    publisher.stop()
    for topic in _topics(client):
        assert topic.startswith(("home/garage/r8/", "homeassistant/")), topic


def test_every_payload_that_should_be_json_is_json_and_the_rest_are_words():
    """`mosquitto_sub` on the status topic should print a word, not a document.

    Status and availability are read by people and by Home Assistant's
    availability handling, both of which want ``online``; everything else is a
    document, and a consumer that cannot parse one has nothing to show.
    """
    client = RecordingClient()
    publisher = _publisher(client, home_assistant=True)
    publisher.start()
    publisher.publish_state({"telemetry": {"voltage": 13.6}, "alerts": []})
    publisher.publish_event({"kind": "alert_start", "alert": {"band": "KA"}})
    publisher.stop()

    words = {publisher.topics["status"], publisher.topics["availability"]}
    assert client.published
    for topic, payload, qos, _retain in client.published:
        assert qos == QOS
        if topic in words:
            assert payload in {"online", "offline"}
        else:
            assert isinstance(json.loads(payload), dict), topic


def test_no_published_payload_carries_the_broker_address_or_its_password():
    """Broker settings are configuration, not telemetry.

    The host is built from octets by ``fixtures.ipv4`` because no file in this
    repository may contain an address-shaped literal; if it ever appeared in a
    payload, ``looks_like_identifier`` is the same check that guards evidence
    on its way out of the private directory.
    """
    client = RecordingClient()
    publisher = _publisher(
        client,
        host=DOC_HOST_A,
        port=8883,
        username="collector",
        password="not-a-real-password",
        tls=True,
        home_assistant=True,
    )
    publisher.start()
    publisher.publish_state({"telemetry": {"voltage": 13.6}, "alerts": []})
    publisher.publish_event({"kind": "alert_start", "alert": {"band": "KA"}})
    publisher.stop()

    assert client.published
    for topic, payload, _qos, _retain in client.published:
        text = f"{topic} {payload}"
        assert not looks_like_identifier(text), text
        assert "not-a-real-password" not in text


def test_status_carries_the_broker_host_which_is_why_it_is_not_a_document():
    """``status()`` is for the operator's own state file, not for a subscriber.

    It reports the host the broker lives on, which is an address, so anything
    that folds a sink's status into something going out over the network is
    publishing one.  The two look interchangeable from the outside, and this is
    the line between them.
    """
    publisher = _publisher(RecordingClient(), host=DOC_HOST_A)
    publisher.start()
    assert looks_like_identifier(json.dumps(publisher.status()))
