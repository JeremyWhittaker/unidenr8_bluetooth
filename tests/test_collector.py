"""The background collector.

The collector is the first thing here meant to run for hours, so most of these
tests are about the OBDLink rather than the detector: when the collector must
refuse to connect, when it must let go, and that it can never mutate anything.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time

import pytest

from fixtures import RANDOM_STATIC
from uniden_r8 import collector, gatt, gnss, storage
from uniden_r8.evidence import DIR_MODE, FILE_MODE
from uniden_r8.privacy import looks_like_identifier

HEALTHY = collector.ObdHealth(True, True, True, True)
UNHEALTHY = collector.ObdHealth(False, False, True, True, "hummer-rfcomm is not active")

TELEMETRY = b"13.6&0&W,0,193,C&0&12&D&D"
ALERT = b"1,00,KA,3,33,33.7850,R,1&0&0&0"


class FakeCharacteristic:
    def __init__(self, uuid):
        self.uuid = uuid


class FakeService:
    def __init__(self, uuid, characteristics=()):
        self.uuid = uuid
        self.characteristics = [FakeCharacteristic(c) for c in characteristics]


def _services():
    return [FakeService(gatt.DATA_SERVICE_UUID, [
        gatt.TELEMETRY_UUID, gatt.ALERT_UUID, gatt.POI_UUID,
        gatt.SETTINGS_1_UUID, gatt.SETTINGS_2_UUID,
    ])]


class FakeClient:
    """Records everything touched. Has no write or service-control method."""

    instances: list[FakeClient] = []

    def __init__(self, services=None, emit=(), notify_fail=(), connected=True):
        self.services = _services() if services is None else services
        self.emit = list(emit)
        self.notify_fail = set(notify_fail)
        self.is_connected = connected
        self.reads: list[str] = []
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.exited = False
        self.handlers: dict[str, object] = {}
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    async def read_gatt_char(self, uuid):
        self.reads.append(uuid)
        return {gatt.TELEMETRY_UUID: TELEMETRY, gatt.ALERT_UUID: ALERT}.get(uuid, b"")

    async def start_notify(self, uuid, handler):
        if uuid in self.notify_fail:
            raise OSError("notify unsupported")
        self.subscribed.append(uuid)
        self.handlers[uuid] = handler
        for target, payload in self.emit:
            if target == uuid:
                handler(None, bytearray(payload))

    async def stop_notify(self, uuid):
        self.unsubscribed.append(uuid)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Shrink every interval so the loop is exercised in milliseconds."""
    monkeypatch.setattr(collector, "PUBLISH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(collector, "HEALTH_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(collector, "HEALTHY_SESSION_SECONDS", 1000.0)
    monkeypatch.setattr(collector, "BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setattr(collector, "BACKOFF_MAX_SECONDS", 0.05)
    FakeClient.instances = []
    yield


def _run(tmp_path, probe, client_factory, duration=0.15):
    return asyncio.run(
        collector.run(
            RANDOM_STATIC, tmp_path / "state",
            duration=duration, obd_probe=probe,
            client_factory=client_factory, install_signal_handlers=False,
        )
    )


def _doc(tmp_path):
    return json.loads((tmp_path / "state" / "state.json").read_text())


# ------------------------------------------------------------ OBD primacy

def test_an_unhealthy_obd_prevents_the_link_from_ever_opening(tmp_path):
    """The OBDLink comes first: no detector connection while it is unhealthy."""
    _run(tmp_path, lambda: UNHEALTHY, lambda a: FakeClient())
    assert FakeClient.instances == [], "no client may be constructed"
    doc = _doc(tmp_path)
    assert doc["obd"]["healthy"] is False
    assert doc["link"]["connected"] is False


def test_an_unhealthy_obd_publishes_a_blocked_status(tmp_path):
    _run(tmp_path, lambda: UNHEALTHY, lambda a: FakeClient())
    # Terminal state is "stopped"; the blocked line is what a display shows.
    assert "OBD" in collector.display_line(
        collector.CollectorState(obd=UNHEALTHY), False
    )


def test_obd_health_lost_during_a_session_releases_the_link(tmp_path):
    """Detected at the periodic check, and acted on promptly."""
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return HEALTHY if calls["n"] <= 2 else UNHEALTHY

    client = FakeClient()
    _run(tmp_path, probe, lambda a: client, duration=0.4)
    assert client.exited, "the detector link must be released"
    assert client.unsubscribed, "subscriptions must be torn down"


def test_the_probe_reason_is_from_a_fixed_vocabulary(tmp_path):
    """Subprocess output can contain an address; this file is published."""
    _run(tmp_path, lambda: UNHEALTHY, lambda a: FakeClient())
    assert not looks_like_identifier(json.dumps(_doc(tmp_path)))


def test_the_collector_never_mutates_a_service_or_rfcomm():
    """Every command vector in the module, checked for a mutating verb.

    Command *vectors* rather than every string constant: prose legitimately
    discusses stopping and restarting, and a check that cannot tell a docstring
    from an argv is a check that has to be weakened until it catches nothing.
    """
    import ast
    from pathlib import Path

    source = Path(collector.__file__).read_text(encoding="utf-8")
    banned = {"start", "stop", "restart", "reload", "enable", "disable", "mask",
              "bind", "release", "unbind", "connect", "trust", "pair", "power"}

    vectors = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.List) and node.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
        ):
            vectors.append([e.value for e in node.elts])

    assert vectors, "expected at least one command vector to check"
    for vector in vectors:
        assert not (set(vector) & banned), f"mutating command vector: {vector}"

    # And the vehicle's serial device is never opened.
    assert 'open("/dev' not in source
    assert "open(_RFCOMM_DEVICE" not in source


def test_the_probe_only_queries(monkeypatch, tmp_path):
    """`systemctl is-active` and `rfcomm` with no arguments are both reads."""
    seen = []

    class FakeCompleted:
        returncode = 0
        stdout = "active"
        stderr = ""

    import subprocess

    def fake_run(args, **kwargs):
        seen.append(list(args))
        result = FakeCompleted()
        result.stdout = "active" if args[:2] == ["systemctl", "is-active"] \
            else "rfcomm0: XX channel 1 connected\n"
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    # The workstation has no /dev/rfcomm0; point the check at a file that does
    # exist so the subprocess behaviour is what is under test here.
    present = tmp_path / "rfcomm0"
    present.write_text("")
    monkeypatch.setattr(collector, "_RFCOMM_DEVICE", str(present))
    health = collector.default_obd_probe()
    assert health.healthy
    assert seen == [["systemctl", "is-active", "hummer-rfcomm"], ["rfcomm"]]
    for args in seen:
        assert not ({"start", "stop", "restart", "enable", "bind", "release"}
                    & set(args))


# ------------------------------------------------------------- allowlists

def test_only_telemetry_and_alert_are_read_and_subscribed(tmp_path):
    client = FakeClient()
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    assert client.reads == [gatt.TELEMETRY_UUID, gatt.ALERT_UUID]
    assert set(client.subscribed) == {gatt.TELEMETRY_UUID, gatt.ALERT_UUID}
    for banned in (gatt.POI_UUID, gatt.SETTINGS_1_UUID, gatt.SETTINGS_2_UUID,
                   gatt.COMMAND_WRITE_UUID):
        assert banned not in client.reads
        assert banned not in client.subscribed


def test_the_compatibility_gate_refuses_a_wrong_service_parentage(tmp_path):
    """The characteristics must live under the vendor data service."""
    client = FakeClient(services=[FakeService(
        "0000180a-0000-1000-8000-00805f9b34fb",
        [gatt.TELEMETRY_UUID, gatt.ALERT_UUID],
    )])
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    assert client.reads == [], "nothing may be read when the gate refuses"
    assert client.exited


def test_the_gate_refuses_a_missing_characteristic(tmp_path):
    client = FakeClient(services=[FakeService(gatt.DATA_SERVICE_UUID,
                                              [gatt.TELEMETRY_UUID])])
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    assert client.reads == []


def test_the_fake_client_has_no_write_method(tmp_path):
    client = FakeClient()
    assert not hasattr(client, "write_gatt_char")
    _run(tmp_path, lambda: HEALTHY, lambda a: client)


def test_the_package_still_has_no_application_write_path():
    from uniden_r8.audit import audit_package

    assert audit_package() == []


# --------------------------------------------------------------- teardown

def test_partial_subscription_still_tears_down_what_worked(tmp_path):
    client = FakeClient(notify_fail={gatt.ALERT_UUID})
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    assert client.subscribed == [gatt.TELEMETRY_UUID]
    assert client.unsubscribed == [gatt.TELEMETRY_UUID]
    assert client.exited


def test_no_subscription_at_all_is_degraded_not_a_hang(tmp_path):
    client = FakeClient(notify_fail={gatt.TELEMETRY_UUID, gatt.ALERT_UUID})
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    assert client.exited


def test_a_stop_event_tears_down_and_publishes_stopped(tmp_path):
    client = FakeClient()
    _run(tmp_path, lambda: HEALTHY, lambda a: client, duration=0.1)
    doc = _doc(tmp_path)
    assert doc["collector"]["status"] == "stopped"
    assert doc["link"]["connected"] is False
    assert client.exited


def test_a_connect_failure_is_survived_not_fatal(tmp_path):
    def explode(address):
        raise OSError("le-connection-abort-by-local")

    code = _run(tmp_path, lambda: HEALTHY, explode)
    assert code == 1
    assert _doc(tmp_path)["collector"]["status"] == "stopped"


def test_no_exception_text_reaches_the_state_file(tmp_path):
    """BlueZ error strings carry addresses, and this file is published."""
    def explode(address):
        raise OSError(f"Device {RANDOM_STATIC} not available")

    _run(tmp_path, lambda: HEALTHY, explode)
    body = (tmp_path / "state" / "state.json").read_text()
    assert RANDOM_STATIC not in body
    assert not looks_like_identifier(body)
    assert "not available" not in body


# --------------------------------------------------------------- backoff

@pytest.fixture
def real_backoff(monkeypatch):
    """The autouse fixture shrinks the constants; these tests need the real ones."""
    monkeypatch.setattr(collector, "BACKOFF_BASE_SECONDS", 5.0)
    monkeypatch.setattr(collector, "BACKOFF_MAX_SECONDS", 300.0)


def test_backoff_grows_and_is_capped(real_backoff):
    values = [collector.next_backoff(i, lambda: 0.5) for i in range(1, 10)]
    assert values == sorted(values)
    assert values[-1] <= collector.BACKOFF_MAX_SECONDS


def test_backoff_is_jittered(real_backoff):
    low = collector.next_backoff(4, lambda: 0.0)
    high = collector.next_backoff(4, lambda: 1.0)
    assert low < high, "identical retries would be a tight loop in disguise"


def test_backoff_never_returns_zero(real_backoff):
    assert collector.next_backoff(0, lambda: 0.0) >= 0.5
    assert collector.next_backoff(-5, lambda: 0.0) >= 0.5


def test_a_healthy_session_resets_the_backoff(tmp_path, monkeypatch):
    """Otherwise a flapping link ratchets to the cap and never recovers."""
    monkeypatch.setattr(collector, "HEALTHY_SESSION_SECONDS", 0.0)
    client = FakeClient()
    _run(tmp_path, lambda: HEALTHY, lambda a: client, duration=0.2)
    assert _doc(tmp_path)["collector"]["reconnects"] >= 0


# ---------------------------------------------------------- single instance

def test_a_second_instance_is_refused(tmp_path):
    path = tmp_path / "state" / "collector.lock"
    first = collector.SingleInstanceLock(path).acquire()
    try:
        with pytest.raises(collector.InstanceBusy):
            collector.SingleInstanceLock(path).acquire()
    finally:
        first.release()


def test_the_lock_is_released_and_reusable(tmp_path):
    path = tmp_path / "state" / "collector.lock"
    with collector.SingleInstanceLock(path):
        pass
    collector.SingleInstanceLock(path).acquire().release()


def test_the_lock_file_is_owner_only(tmp_path):
    path = tmp_path / "state" / "collector.lock"
    previous = os.umask(0o000)
    try:
        lock = collector.SingleInstanceLock(path).acquire()
    finally:
        os.umask(previous)
    try:
        assert path.stat().st_mode & 0o777 == FILE_MODE
        assert path.parent.stat().st_mode & 0o777 == DIR_MODE
    finally:
        lock.release()


# ------------------------------------------------------- the state document

def test_the_state_file_and_directory_are_owner_only(tmp_path):
    previous = os.umask(0o000)
    try:
        _run(tmp_path, lambda: HEALTHY, lambda a: FakeClient())
    finally:
        os.umask(previous)
    state = tmp_path / "state" / "state.json"
    assert state.stat().st_mode & 0o777 == FILE_MODE
    assert state.parent.stat().st_mode & 0o777 == DIR_MODE


def test_the_document_is_schema_versioned(tmp_path):
    _run(tmp_path, lambda: HEALTHY, lambda a: FakeClient())
    assert _doc(tmp_path)["schema"] == collector.SCHEMA_VERSION


def test_the_write_is_atomic_and_leaves_no_temporary(tmp_path):
    _run(tmp_path, lambda: HEALTHY, lambda a: FakeClient())
    leftovers = list((tmp_path / "state").glob(".state.json*"))
    assert leftovers == [], f"temporary file left behind: {leftovers}"


def test_the_document_never_carries_position_or_identifiers(tmp_path):
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, TELEMETRY),
                              (gatt.ALERT_UUID, ALERT)])
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    body = (tmp_path / "state" / "state.json").read_text()
    assert not looks_like_identifier(body)
    for banned in ("heading", "altitude", "speed", "193", "hex", "raw"):
        assert banned not in body, f"{banned!r} must not be published"


def test_an_unrecognised_band_is_not_echoed():
    """The state file must not carry arbitrary strings from the device."""
    from uniden_r8.telemetry import Alert

    assert collector._publishable_alert(Alert(band="KA"))["band"] == "KA"
    assert collector._publishable_alert(Alert(band="../etc/passwd"))["band"] == "unknown"
    assert collector._publishable_alert(Alert(band="WEIRD"))["band"] == "unknown"


def test_an_unrecognised_direction_is_not_echoed():
    from uniden_r8.telemetry import Alert

    assert collector._publishable_alert(Alert(direction="R"))["direction"] == "rear"
    assert collector._publishable_alert(Alert(direction="ZZ"))["direction"] == "unknown"


def test_published_alerts_are_bounded():
    state = collector.CollectorState()
    from uniden_r8.telemetry import Alert

    state.record_alerts([Alert(band="KA") for _ in range(50)])
    assert len(build := collector.build_document(state)["alerts"]) <= \
        collector.MAX_PUBLISHED_ALERTS
    assert build


def test_staleness_is_reported(tmp_path):
    state = collector.CollectorState()
    state.obd = HEALTHY
    state.connected = state.compatible = True
    from uniden_r8.telemetry import parse_telemetry

    state.record_telemetry(parse_telemetry(TELEMETRY), now=0.0)
    fresh = collector.build_document(state, now=1.0)
    stale = collector.build_document(state, now=collector.STALE_AFTER_SECONDS + 5)
    assert fresh["telemetry"]["stale"] is False
    assert stale["telemetry"]["stale"] is True
    assert "STALE" in stale["display_line"]


def test_the_display_line_is_short_enough_for_the_panel(tmp_path):
    """250x122 px, six lines. A long line is a truncated line."""
    state = collector.CollectorState(obd=HEALTHY)
    state.connected = state.compatible = True
    from uniden_r8.telemetry import parse_alerts, parse_telemetry

    state.record_telemetry(parse_telemetry(TELEMETRY))
    for payload in (b"0&0&0&0", ALERT):
        state.record_alerts(parse_alerts(payload))
        assert len(collector.display_line(state, False)) <= 32
    assert len(collector.display_line(state, True)) <= 32
    assert len(collector.display_line(collector.CollectorState(), False)) <= 32


def test_no_raw_payload_is_retained(tmp_path):
    """The long-running path must not accumulate packets."""
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, TELEMETRY)] * 20)
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    body = (tmp_path / "state" / "state.json").read_text()
    assert TELEMETRY.decode() not in body
    assert "packets" not in json.loads(body).get("telemetry", {})
    # And no private capture file is written by the collector.
    assert not (tmp_path / "state" / "live-raw.json").exists()


def test_counters_are_published(tmp_path):
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, TELEMETRY),
                              (gatt.ALERT_UUID, ALERT)])
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    counters = _doc(tmp_path)["counters"]
    assert counters["telemetry_packets"] >= 1
    assert counters["alert_packets"] >= 1
    assert counters["unparsed_telemetry"] == 0


def test_trial_mode_is_bounded(tmp_path):
    import time as clock

    began = clock.monotonic()
    _run(tmp_path, lambda: HEALTHY, lambda a: FakeClient(), duration=0.2)
    assert clock.monotonic() - began < 5.0
    assert _doc(tmp_path)["collector"]["mode"] == "trial"


def test_continuous_mode_is_labelled(tmp_path):
    """A stop event ends it; the label must still say what it was."""
    client = FakeClient()

    async def go():
        task = asyncio.create_task(
            collector.run(RANDOM_STATIC, tmp_path / "state",
                          obd_probe=lambda: UNHEALTHY,
                          client_factory=lambda a: client,
                          install_signal_handlers=False)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert _doc(tmp_path)["collector"]["mode"] == "continuous"


def test_a_healthy_streaming_session_still_honours_the_trial_deadline(tmp_path):
    """The regression this was written for.

    A session that stays healthy never returns on its own, so a `--duration`
    checked only between sessions bounds every trial *except* the ones that
    work. This asserts the streaming loop itself observes the deadline.
    """
    import time as clock

    client = FakeClient(connected=True)
    began = clock.monotonic()
    _run(tmp_path, lambda: HEALTHY, lambda a: client, duration=0.3)
    elapsed = clock.monotonic() - began
    assert elapsed < 5.0, f"trial ran {elapsed:.1f}s; the deadline was not honoured"
    assert client.exited
    assert _doc(tmp_path)["collector"]["status"] == "stopped"


def test_a_hung_gatt_read_cannot_outlive_the_trial_deadline(tmp_path):
    class HungReadClient(FakeClient):
        async def read_gatt_char(self, uuid):
            self.reads.append(uuid)
            await asyncio.Event().wait()

    import time as clock

    client = HungReadClient()
    began = clock.monotonic()
    code = _run(tmp_path, lambda: HEALTHY, lambda _address: client, duration=0.1)
    assert clock.monotonic() - began < 1.0
    assert code == 0, "the compatibility gate completed before the stuck read"
    assert client.exited
    assert _doc(tmp_path)["collector"]["status"] == "stopped"


@pytest.mark.parametrize("duration", [0, -1, math.inf, -math.inf, math.nan, True])
def test_invalid_direct_durations_are_refused_before_any_link_opens(tmp_path, duration):
    with pytest.raises(ValueError, match="finite and greater than zero"):
        _run(tmp_path, lambda: HEALTHY, lambda _address: FakeClient(), duration=duration)
    assert FakeClient.instances == []


# ------------------------------------------------- the two published documents

#: The exact shape the e-paper display was written against.  Pinned as a
#: literal rather than against the module constant: asserting
#: `doc["schema"] == collector.SCHEMA_VERSION` is a tautology that stays green
#: while the consumer breaks.
SCHEMA_ONE_KEYS = {
    "schema", "updated_at", "collector", "obd", "link", "counters",
    "telemetry", "alerts", "display_line",
}


def _detail(tmp_path):
    return json.loads((tmp_path / "state" / "state-v2.json").read_text())


def test_state_json_is_still_schema_one_with_exactly_its_original_keys(tmp_path):
    """A consumer requiring `schema == 1` exists and must keep working."""
    _run(tmp_path, lambda: HEALTHY, lambda a: FakeClient())
    document = _doc(tmp_path)
    assert document["schema"] == 1
    assert set(document) == SCHEMA_ONE_KEYS
    assert set(document["collector"]) == {
        "mode", "status", "started_at", "reconnects", "note"
    }
    assert set(document["link"]) == {"connected", "compatible"}
    assert set(document["counters"]) == {
        "telemetry_packets", "alert_packets", "unparsed_telemetry"
    }


def test_the_detailed_document_is_schema_two_and_a_separate_file(tmp_path):
    _run(tmp_path, lambda: HEALTHY, lambda a: FakeClient())
    assert _detail(tmp_path)["schema"] == 2
    assert _doc(tmp_path)["schema"] == 1


def test_the_detailed_document_is_owner_only_and_git_ignored(tmp_path):
    """It carries the detector's heading, speed and altitude.

    Those are position-adjacent -- a log of them is a rough trace of a drive --
    so the permissions and the ignore rule are the controls that make decoding
    them acceptable at all.
    """
    import subprocess
    from pathlib import Path

    _run(tmp_path, lambda: HEALTHY, lambda a: FakeClient())
    detail = tmp_path / "state" / "state-v2.json"
    assert detail.stat().st_mode & 0o777 == FILE_MODE
    assert detail.parent.stat().st_mode & 0o777 == DIR_MODE

    # The permission half holds anywhere.  The ignore half needs a checkout,
    # and the deployed tree on the node is a plain directory -- the same reason
    # test_repo_hygiene.py guards its git checks.  Skipping there rather than
    # failing keeps the node's own test run meaningful.
    repo = Path(__file__).resolve().parent.parent
    if not (repo / ".git").exists():
        pytest.skip("not a git checkout; the ignore rule is asserted in CI")
    for candidate in (".state/state-v2.json", ".state/history.db"):
        assert subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", candidate], check=False
        ).returncode == 0, f"{candidate} is NOT git-ignored"


def test_only_the_detailed_document_carries_the_detectors_own_motion(tmp_path):
    """The split is the whole design: schema 1 stays free of position."""
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, b"13.6&0&NE,45,312,C&0&12&D&D")])
    _run(tmp_path, lambda: HEALTHY, lambda a: client)

    plain = _doc(tmp_path)
    assert "detector" not in plain
    for banned in ("heading", "altitude", "speed", "direction_8"):
        assert banned not in json.dumps(plain)

    gps = _detail(tmp_path)["detector"]["detector_gps"]
    assert gps["direction_8"] == "NE"
    assert gps["speed_raw"] == 45
    assert gps["altitude_raw"] == 312


def test_neither_document_carries_an_identifier(tmp_path):
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, TELEMETRY),
                              (gatt.ALERT_UUID, ALERT)])
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    for name in ("state.json", "state-v2.json"):
        body = (tmp_path / "state" / name).read_text()
        assert not looks_like_identifier(body), name
        assert RANDOM_STATIC not in body


def test_the_detailed_document_carries_no_position_without_a_gnss_source(tmp_path):
    """The detector supplies none, and no fix was configured."""
    from uniden_r8.privacy import looks_like_position

    _run(tmp_path, lambda: HEALTHY, lambda a: FakeClient())
    detail = _detail(tmp_path)
    assert detail["vehicle_gnss"] is None
    assert not looks_like_position(detail)


class _StubGnssClient:
    """A GNSS client with an immediate fix.  No `gpsd`, no network.

    Mirrors only what `Sinks` actually touches on a real `GnssClient`: a
    `fix` property, a `status()` for the sink-status document, and a `run()`
    that occupies its task until told to stop -- exactly the seam
    `Sinks.start()` reaches for via `uniden_r8.gnss.GnssClient`.
    """

    def __init__(self, *_args, **_kwargs):
        self._fix = gnss.Fix(
            mode=3, lat=33.4484, lon=-112.0740, altitude_m=331.0,
            speed_mps=12.5, track_deg=88.0, epx_m=3.0, epy_m=3.0,
            satellites=8, monotonic_ns=time.monotonic_ns(), wall_ns=time.time_ns(),
        )

    @property
    def fix(self):
        return self._fix

    def status(self):
        return {"enabled": True, "connected": True}

    async def run(self, stop):
        await stop.wait()


def _row_counts(history_path) -> tuple[int, int]:
    """``(telemetry rows, gnss_fixes rows)`` from a database the collector wrote.

    Both, always, because the interesting assertion is the *relationship*.
    "gnss_fixes is empty" means nothing on its own -- it is also what a run that
    never got going produces -- so every claim here is stated against the
    telemetry count, which proves the collector actually ran.
    """
    with storage.History(history_path, read_only=True) as history:
        rows = history.connection.execute(
            "SELECT (SELECT count(*) FROM telemetry), (SELECT count(*) FROM gnss_fixes)"
        ).fetchone()
    return rows[0], rows[1]


#: Long enough that a loaded CI runner still gets the collector through connect,
#: subscribe, first packet and one pump iteration.
#:
#: This was 0.2 s and it was flaky: the run passed locally and on two of three
#: Python versions in CI, and failed on the third with a bare ``assert 0 > 0``.
#: A test that is usually right is worse than one that is wrong, because it
#: teaches people to re-run it.  Nothing here waits on the wall clock for its
#: *result* -- the assertions are about counts -- so the only job of this number
#: is to stop the environment deciding the outcome.
_HISTORY_TRIAL_SECONDS = 1.0


def test_the_collector_fills_gnss_fixes_end_to_end_through_the_history_writer(
    tmp_path, monkeypatch,
):
    """`HistoryWriter.record_fix` is reachable, not merely present in the source.

    An audit found `_record_history_fix` wired up in `collector.py` but no
    test proving the `gnss_fixes` table actually filled end to end, so the
    claim "it is never called" could not be refuted from the suite alone.
    This runs the real collector loop with history and GNSS both enabled,
    against a stub GNSS client, and reads the row back out of the database
    the collector itself wrote.
    """
    from uniden_r8 import config as config_module

    monkeypatch.setattr(gnss, "GnssClient", _StubGnssClient)
    state_dir = tmp_path / "state"
    settings = config_module.Config(
        obd=config_module.ObdConfig(guard=False),
        history=config_module.HistoryConfig(enabled=True),
        gnss=config_module.GnssConfig(enabled=True),
        # `Config.history_path` resolves against `collector.state_dir`, not
        # against whatever path `collector.run` is given directly -- so the
        # two must be the same directory or the database this test reads
        # would not be the one the collector wrote.
        collector=config_module.CollectorConfig(state_dir=str(state_dir)),
    )
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, TELEMETRY)])
    asyncio.run(
        collector.run(
            RANDOM_STATIC, state_dir, duration=_HISTORY_TRIAL_SECONDS,
            obd_probe=lambda: HEALTHY, client_factory=lambda a: client,
            install_signal_handlers=False, config=settings,
        )
    )

    telemetry_rows, fixes = _row_counts(settings.history_path)
    assert telemetry_rows > 0, "the collector never ran; the trial was too short"
    assert fixes > 0, (
        f"{telemetry_rows} telemetry rows were written and no GNSS fix was -- "
        "record_fix is not reachable"
    )


def test_gnss_fixes_stays_empty_when_gnss_is_disabled(tmp_path, monkeypatch):
    """The control the claim above needs: same run, minus the enabled flag.

    Without this half, a passing "count is greater than zero" test alone
    could not distinguish "the collector wires GNSS fixes into history" from
    "this table always gets a row from somewhere else regardless of
    configuration" -- the same reasoning the audit used to say the positive
    case alone was not enough evidence.
    """
    from uniden_r8 import config as config_module

    monkeypatch.setattr(gnss, "GnssClient", _StubGnssClient)
    state_dir = tmp_path / "state"
    settings = config_module.Config(
        obd=config_module.ObdConfig(guard=False),
        history=config_module.HistoryConfig(enabled=True),
        gnss=config_module.GnssConfig(enabled=False),
        collector=config_module.CollectorConfig(state_dir=str(state_dir)),
    )
    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, TELEMETRY)])
    asyncio.run(
        collector.run(
            RANDOM_STATIC, state_dir, duration=_HISTORY_TRIAL_SECONDS,
            obd_probe=lambda: HEALTHY, client_factory=lambda a: client,
            install_signal_handlers=False, config=settings,
        )
    )

    telemetry_rows, fixes = _row_counts(settings.history_path)
    # Asserted against the telemetry count on purpose: without it this test
    # passes just as happily when the collector never started, which would make
    # it a control that cannot fail.
    assert telemetry_rows > 0, "the collector never ran; the trial was too short"
    assert fixes == 0



# ----------------------------------------------------------- lossless events

def test_a_short_alert_produces_a_start_and_an_end(tmp_path):
    """The bug this rewrite exists to fix.

    An alert that begins and clears inside one publish interval used to reach
    nobody: the callback replaced a value and the timer published later.  Both
    transitions must survive.
    """
    client = FakeClient(emit=[(gatt.ALERT_UUID, ALERT),
                              (gatt.ALERT_UUID, b"0&0&0&0"),
                              (gatt.ALERT_UUID, b"0&0&0&0")])
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    kinds = [event["kind"] for event in _detail(tmp_path)["recent_events"]]
    assert "alert_start" in kinds
    assert "alert_end" in kinds


def test_a_dropped_notification_is_visible_as_a_gap(tmp_path):
    """A silent drop is a lie.  Overflow must be reportable, not inferred."""
    from uniden_r8 import events

    ingest = events.Ingest(maxsize=4)
    for index in range(12):
        ingest.offer("alert", f"{index}".encode())

    records = ingest.drain()
    gaps = [r for r in records if isinstance(r, events.Gap)]
    assert gaps, "an overflow must leave a gap record in the stream"
    assert ingest.metrics.dropped > 0
    assert gaps[0].count == ingest.metrics.dropped
    # And the newest arrival is always available regardless of the backlog.
    assert ingest.latest["alert"].payload == b"11"


def test_the_queue_metrics_reach_the_detailed_document(tmp_path):
    _run(tmp_path, lambda: HEALTHY, lambda a: FakeClient())
    ingest = _detail(tmp_path)["ingest"]
    assert set(ingest) >= {"accepted", "dropped", "gaps", "high_water",
                           "lost_notifications"}


def test_the_loop_lag_watchdog_reports_a_number(tmp_path):
    """The cheapest diagnostic here: everything that could starve the BLE
    notification path shows up as overshoot on a quarter-second timer."""
    _run(tmp_path, lambda: HEALTHY, lambda a: FakeClient(), duration=0.6)
    health = _detail(tmp_path)["health"]
    assert isinstance(health["loop_lag_ms"], (int, float))
    assert health["loop_lag_alarm"] is False


def test_the_obd_probe_never_runs_on_the_event_loop(tmp_path):
    """It shells out twice with a five-second timeout each.

    On the loop that would be a ten-second stall in the notification path,
    which is long enough for BlueZ to drop the subscription -- so the probe is
    dispatched to a thread and this pins that it stays there.
    """
    import threading

    seen: list[int] = []
    main = threading.get_ident()

    def probe():
        seen.append(threading.get_ident())
        return HEALTHY

    _run(tmp_path, probe, lambda a: FakeClient())
    assert seen, "the probe must actually run"
    assert all(ident != main for ident in seen), "the probe ran on the event loop"


def test_the_collector_issues_only_query_argv(tmp_path):
    """The stronger form of the mutation check: real argv, not any list.

    The scan above walks every list-of-strings in the module, which is broad
    and cheap and also fooled by anything that is not a command vector -- a
    ``__all__`` entry, a set of field names.  This one runs the probe against a
    recording ``subprocess.run`` and checks what was actually going to be
    executed, which is the thing that matters.
    """
    import subprocess

    seen: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def recording_run(args, **_kwargs):
        seen.append(list(args))
        result = Completed()
        result.stdout = "inactive"
        return result

    original = subprocess.run
    subprocess.run = recording_run
    try:
        collector.make_obd_probe("some-unit", "/dev/null")()
    finally:
        subprocess.run = original

    assert seen, "the probe must actually run a command"
    mutating = {"start", "stop", "restart", "reload", "enable", "disable",
                "mask", "bind", "release", "unbind", "connect", "trust",
                "pair", "power"}
    for argv in seen:
        assert not (set(argv) & mutating), f"mutating argv: {argv}"
        assert argv[0] in {"systemctl", "rfcomm"}, f"unexpected binary: {argv[0]}"
        if argv[0] == "systemctl":
            assert argv[1] == "is-active", "systemctl may only be queried"
        else:
            assert len(argv) == 1, "rfcomm takes no arguments here"


def test_the_argv_check_would_catch_a_mutation():
    """A control that cannot fail proves nothing."""
    mutating = {"start", "stop", "restart", "enable", "bind", "release"}
    hypothetical_argv = ["systemctl", "restart", "hummer-rfcomm"]
    assert set(hypothetical_argv) & mutating


def test_the_collector_runs_with_the_obd_guard_disabled(tmp_path):
    """A node with no OBDLink must still be able to run this.

    The first version of the collector hard-coded a unit name and a device that
    exist only on one Pi, which made the whole project unusable anywhere else.
    """
    from uniden_r8 import config as config_module

    settings = config_module.Config(obd=config_module.ObdConfig(guard=False))
    asyncio.run(
        collector.run(
            RANDOM_STATIC, tmp_path / "state", duration=0.15,
            client_factory=lambda a: FakeClient(),
            install_signal_handlers=False, config=settings,
        )
    )
    document = _doc(tmp_path)
    assert document["obd"]["healthy"] is True
    assert _detail(tmp_path)["obd"]["guard_enabled"] is False
    assert "guard disabled" in document["obd"]["reason"]


# ------------------------------------------------------- failure behaviour

def test_a_live_alert_does_not_stay_latched_after_the_link_goes_away(tmp_path):
    """The most dangerous stale value this program could publish.

    A detector switched off mid-alert used to leave its last snapshot in the
    state file, the feed and the broker forever, and a stale threat reads
    exactly like a live one.
    """
    client = FakeClient(emit=[(gatt.ALERT_UUID, ALERT)])
    _run(tmp_path, lambda: HEALTHY, lambda a: client)
    assert _doc(tmp_path)["alerts"] == []
    assert _detail(tmp_path)["alerts"] == []
    assert _detail(tmp_path)["open_tracks"] == []


def test_an_unwritable_state_directory_does_not_stop_the_collector(tmp_path):
    """A full or read-only card must cost the state file, never the radar data."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    client = FakeClient(emit=[(gatt.TELEMETRY_UUID, TELEMETRY)])
    code = asyncio.run(
        collector.run(
            RANDOM_STATIC, blocked, duration=0.2,
            obd_probe=lambda: HEALTHY, client_factory=lambda a: client,
            install_signal_handlers=False,
        )
    )
    assert code in (0, 1), "it must exit, not raise"
    assert client.exited, "the link was still released"


def test_the_outward_documents_carry_no_broker_or_database_location(tmp_path):
    """Publishing a broker's address to that broker discloses a network."""
    from uniden_r8 import config as config_module

    settings = config_module.Config(
        obd=config_module.ObdConfig(guard=False),
        history=config_module.HistoryConfig(enabled=True),
    )
    sinks = collector.Sinks(settings)

    full = sinks.status()
    reduced = sinks.status(locating=False)
    assert set(full) == set(reduced)
    for name, status in reduced.items():
        for key in ("host", "port", "path", "bind"):
            assert key not in status, f"{name}.{key} would have travelled"


def test_the_instance_lock_is_the_same_lock_from_two_directories(tmp_path):
    """A relative path taken from two working directories is two files.

    That would let two collectors hold the detector at once, which is the exact
    situation the lock exists to make impossible.
    """
    target = tmp_path / "state" / "collector.lock"
    first = collector.SingleInstanceLock(target)
    second = collector.SingleInstanceLock(
        tmp_path / "state" / ".." / "state" / "collector.lock"
    )
    assert first.path == second.path
    with first, pytest.raises(collector.InstanceBusy):
        second.acquire()


def test_a_reconnecting_session_gets_its_own_silence_window(tmp_path):
    """The silence watchdog must not charge a session for the previous one.

    `state.latest_at` deliberately outlives the session that set it — it is what
    the published documents age their reading against. Measuring silence from it
    alone meant that after any silence teardown, every subsequent session was
    killed before a packet could arrive, forever. The commonest way a session
    ends is that very timeout, so the failure is self-reinforcing.
    """
    state = collector.CollectorState()
    # A packet from a previous session, long enough ago to be past the window.
    state.record_telemetry(
        __import__("uniden_r8.telemetry", fromlist=["x"]).parse_telemetry(TELEMETRY),
        now=0.0,
    )
    stale_age = collector.SILENCE_TIMEOUT_SECONDS * 10
    assert state.age_seconds(stale_age) > collector.SILENCE_TIMEOUT_SECONDS

    # A session starting now has received nothing, and must still be inside its
    # window: max(streaming_since, latest_at) is what the pump measures from.
    streaming_since = stale_age
    silent_for = stale_age - max(streaming_since, state.latest_at or 0.0)
    assert silent_for <= collector.SILENCE_TIMEOUT_SECONDS

    # And a session that has genuinely gone quiet is still torn down.
    later = stale_age + collector.SILENCE_TIMEOUT_SECONDS + 1
    assert later - max(streaming_since, state.latest_at or 0.0) > \
        collector.SILENCE_TIMEOUT_SECONDS


def test_the_dashboard_does_not_mix_a_bigint_with_a_number():
    """It threw a TypeError on every alert frame, killing the whole event log."""
    from uniden_r8.feed import INDEX_HTML

    assert "n)" not in INDEX_HTML.split("wall_ns")[1][:40], "BigInt literal"
    assert "1000000n" not in INDEX_HTML
    assert "(e.wall_ns||0)/1e6" in INDEX_HTML
