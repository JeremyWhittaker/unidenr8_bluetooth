"""The local history: what it records, what it refuses to record, and why it
cannot take the radio down with it.

Every test here runs against a real SQLite file in a temporary directory, and
none of them needs a radio, a broker, gpsd or a network.  That is not a
convenience.  The history is the one component in this project whose failures
are silent by construction -- :class:`~uniden_r8.storage.HistoryWriter`
swallows every exception on its own thread so that a slow or missing SD card
cannot stall the loop holding the BLE link -- and a component that is designed
not to fail loudly has to be pinned by tests instead of by a stack trace.

Four of those pins carry more weight than the rest.

**The opt-ins are the privacy control, and a privacy control is only real when
both answers are tested.**  ``history.record_detector_motion`` and
``gnss.record_coordinates`` default to off, so every assertion below that a
column is NULL has a twin asserting the value is written when the operator
turned the flag on.  A test that only proved the "off" direction would still
pass if the flag had been wired to a constant, and the "off" direction is the
one that would then be a lie.

**No raw packet reaches the disk.**  The wire format packs heading, speed,
altitude and POI detail into a single field of a single byte string.  A schema
with a ``payload`` column would therefore carry all of it past both opt-ins,
under a name that says nothing about what is inside, and no amount of care in
the query layer would get it back out again.  So the schema is enumerated here
rather than trusted to a reviewer's memory of it.

**The caller's side never raises.**  The task that enqueues a row is the task
that owns the notification callbacks.  Pointing the writer at a path that
cannot be created and then continuing to call it is the closest this suite can
get to a card that went away halfway through a drive.

**A file written under different meanings is refused, not reinterpreted.**
There is no migration, deliberately; the failure mode this replaces is a
history that silently mixes rows from two schemas and reads plausibly wrong.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from fixtures import RANDOM_STATIC
from uniden_r8 import events, gnss, storage, telemetry
from uniden_r8.evidence import DIR_MODE, FILE_MODE, iso_from_wall_ns
from uniden_r8.privacy import looks_like_identifier

# Captured-shape packets from upstream's R8w writeup, used here as *shapes*:
# what matters below is what the writer does with a decoded packet, not what
# the detector meant by one.
TELEMETRY_PACKET = b"12.1&0&W,0,193,C&0&12&D&D"
ALERT_PACKET = b"1,00,KA,3,33,33.7850,R,1&0&0&0"

#: One instant for the whole file, read from the clock once rather than
#: written down.  A hard-coded constant would age past the default thirty-day
#: retention within a month of being typed and start colliding with the sweep,
#: which is a fixture rotting into a failure that says nothing.
WALL_NS = time.time_ns()
#: Formatted with the project's own public helper, so the assertions below on
#: the stored ``at`` column also pin the writer's private copy of it to this.
WALL_ISO = iso_from_wall_ns(WALL_NS)
MONOTONIC_NS = 5_000_000_000

#: Far enough back that any retention an operator would configure has expired.
ANCIENT_WALL_NS = WALL_NS - 400 * 86_400 * 1_000_000_000

#: A position, assembled from whole numbers rather than pasted.  It is nowhere
#: anyone has been, and it is still precise enough that if it ever turned up in
#: a column that should hold NULL the test would recognise it.
LAT = 12 + 3456 / 10_000
LON = -(34 + 5678 / 10_000)

#: Column names that would mean a whole packet, or an undecoded fragment of
#: one, had reached the disk.  The ``*_raw`` columns this schema does have are
#: deliberately absent from this set: each of those holds one decoded field
#: under its own name, which is the opposite of a payload.
FORBIDDEN_COLUMNS = frozenset(
    {"payload", "raw", "hex", "blob", "packet", "packet_raw", "bytes",
     "data", "notification", "frame"}
)

EXPECTED_TABLES = frozenset(
    {"meta", "sessions", "alert_events", "alert_snapshots", "telemetry", "gnss_fixes"}
)

#: The one table that holds a packet, and the only one permitted to.
#:
#: The privacy argument against a payload column is about the *telemetry*
#: format specifically: it packs heading, speed, altitude and POI detail into
#: one byte string, so storing it whole carries all of that past both opt-ins.
#: An *alert* packet carries band, strength, frequency, direction and mute
#: state, and no position of any kind -- so the argument does not reach it, and
#: keeping alert packets is what makes the derived tracks re-derivable by a
#: better matcher later.  The exemption is named here rather than widened into
#: FORBIDDEN_COLUMNS, so it stays one table and one column.
PAYLOAD_EXEMPTION = ("alert_snapshots", "payload")


# ------------------------------------------------------------------ builders


def _alert(packet: bytes = ALERT_PACKET) -> telemetry.Alert:
    return telemetry.parse_alerts(packet)[0]


def _reading(packet: bytes = TELEMETRY_PACKET) -> telemetry.Telemetry:
    return telemetry.parse_telemetry(packet)


def _event(kind: str = "alert_start", *, seq: int = 1, track_id: int = 7,
           wall_ns: int = WALL_NS, **extra) -> events.AlertEvent:
    """One transition, built from a really parsed alert rather than a stub."""
    return events.AlertEvent(
        kind=kind, seq=seq, track_id=track_id, monotonic_ns=MONOTONIC_NS,
        wall_ns=wall_ns, alert=_alert(), **extra,
    )


def _fix(**overrides) -> gnss.Fix:
    """A 3D fix carrying coordinates good enough to identify a driveway."""
    values = {
        "mode": 3, "lat": LAT, "lon": LON, "altitude_m": 58.5,
        "speed_mps": 27.5, "track_deg": 271.5, "epx_m": 4.2, "epy_m": 3.9,
        "satellites": 11, "monotonic_ns": MONOTONIC_NS, "wall_ns": WALL_NS,
    }
    values.update(overrides)
    return gnss.Fix(**values)


def _started_writer(path, **options) -> storage.HistoryWriter:
    writer = storage.HistoryWriter(path, **options)
    assert writer.start(
        started_at=WALL_ISO, wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS, adapter="hci0",
    ) is True
    return writer


def _notification(payload: bytes, seq: int = 1) -> events.Notification:
    """One arrived packet, stamped, without needing a radio to produce it."""
    return events.Notification(
        seq=seq, kind="alert", payload=payload,
        monotonic_ns=MONOTONIC_NS, wall_ns=WALL_NS,
    )


def _rows(path, table: str) -> list[dict]:
    """Every row of *table*, oldest first, through a fresh read-only handle."""
    with storage.History(path, read_only=True) as history:
        cursor = history.connection.execute(f"SELECT * FROM {table} ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]


def _dump(path) -> str:
    """Every statement needed to rebuild the database, as one string."""
    with storage.History(path, read_only=True) as history:
        return "\n".join(history.connection.iterdump())


# --------------------------------------------------------------- permissions


def test_the_database_is_created_owner_only_inside_an_owner_only_directory(tmp_path):
    """A history any other account can read is a record of where a car goes.

    The umask is opened all the way first, because that is the hazard: both
    ``mkdir`` and sqlite's own ``open`` apply the process umask, so a file that
    is only tightened after the fact is world-readable for the window in
    between -- and on a vehicle node that window contains the first alert.
    """
    root = tmp_path / "state" / "uniden-r8"
    previous = os.umask(0o000)
    try:
        with storage.History(root / "history.db") as history:
            history.connection.execute("SELECT 1")
            # Read inside the block: a clean close checkpoints the WAL and
            # takes the sidecars with it, so afterwards there is nothing left
            # to check and the loop below would pass over an empty list.
            modes = {
                path.name: path.stat().st_mode & 0o777 for path in sorted(root.iterdir())
            }
    finally:
        os.umask(previous)

    assert root.stat().st_mode & 0o777 == DIR_MODE
    assert modes.pop("history.db") == FILE_MODE
    # The write-ahead log holds committed rows that have not been checkpointed
    # yet, which makes it exactly as sensitive as the file it belongs to.
    assert modes, "WAL mode should have produced sidecar files to check"
    for name, mode in modes.items():
        assert mode == FILE_MODE, name


def test_a_second_open_of_an_existing_database_leaves_the_mode_alone(tmp_path):
    """Reopening must not widen what the first open narrowed."""
    path = tmp_path / "history.db"
    storage.History(path).open().close()
    previous = os.umask(0o000)
    try:
        storage.History(path).open().close()
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == FILE_MODE


# -------------------------------------------------------------------- pragmas


def test_wal_is_actually_enabled_and_synchronous_is_normal(tmp_path):
    """The ignition is cut mid-transaction routinely; this is what survives it.

    Asserting the pragmas rather than the source line matters because
    ``journal_mode`` is the one pragma sqlite can silently decline -- on a
    filesystem with no shared memory it falls back and reports the fallback.
    """
    with storage.History(tmp_path / "history.db") as history:
        connection = history.connection
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        # 1 is NORMAL.  FULL would be an fsync per commit on an SD card, and
        # would buy only the last few transactions, which this data can lose.
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        # A WAL with no ceiling outlives the free space on the card.
        assert connection.execute("PRAGMA journal_size_limit").fetchone()[0] == 4_194_304


# ------------------------------------------------------------- the round trip


def test_start_and_stop_round_trip_an_alert_a_sample_and_a_fix(tmp_path):
    """What the loop enqueued has to be on the disk once stop() has returned.

    The writer's only promise to the caller is "later"; if stop() did not make
    that promise come due, a drive's history would end wherever the last batch
    happened to fall.
    """
    path = tmp_path / "history.db"
    writer = _started_writer(path, record_motion=True, record_coordinates=True)
    writer.record_alert_event(_event("alert_start"), _fix())
    writer.record_alert_event(
        _event("alert_end", seq=2, correlation="timeout", duration_s=4.25,
               samples=17, max_strength=5, max_raw_signal=41),
        _fix(),
    )
    writer.record_telemetry(_reading(), wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS)
    writer.record_fix(_fix(), monotonic_ns=MONOTONIC_NS)
    writer.stop()

    assert writer.queued == 4
    assert writer.written == 4
    assert writer.dropped == 0
    assert writer.errors == 0

    sessions = _rows(path, "sessions")
    assert len(sessions) == 1
    assert sessions[0]["started_at"] == WALL_ISO
    assert sessions[0]["adapter"] == "hci0"
    assert sessions[0]["ended_at"], "stop() must close the session it opened"

    start, end = _rows(path, "alert_events")
    assert start["session_id"] == sessions[0]["id"]
    assert (start["kind"], start["seq"], start["track_id"]) == ("alert_start", 1, 7)
    assert start["at"] == WALL_ISO
    assert start["wall_ns"] == WALL_NS
    assert start["monotonic_ns"] == MONOTONIC_NS
    assert start["band"] == "KA"
    assert start["strength"] == 3
    assert start["raw_signal"] == 33
    assert start["frequency_ghz"] == pytest.approx(33.785)
    assert start["direction"] == "rear"
    assert start["mute_code"] == "1"
    assert start["alert_id_raw"] == "00"
    assert start["correlation"] == "new"
    assert start["material"] == 1
    assert start["laser_gun_id"] is None

    assert end["kind"] == "alert_end"
    assert end["correlation"] == "timeout"
    assert end["duration_s"] == pytest.approx(4.25)
    assert (end["samples"], end["max_strength"], end["max_raw_signal"]) == (17, 5, 41)

    sample = _rows(path, "telemetry")[0]
    assert sample["at"] == WALL_ISO
    assert sample["voltage"] == pytest.approx(12.1)
    assert sample["gps_locked"] == 1
    assert sample["poi_active"] == 0
    assert sample["warning_raw"] == "0"
    assert sample["scan_raw"] == "12"

    stored_fix = _rows(path, "gnss_fixes")[0]
    assert stored_fix["mode"] == 3
    assert stored_fix["satellites"] == 11
    assert stored_fix["alt_m"] == pytest.approx(58.5)


def test_stop_is_safe_to_call_twice(tmp_path):
    """Shutdown is reached from more than one path and must not care."""
    writer = _started_writer(tmp_path / "history.db")
    writer.record_telemetry(_reading(), wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS)
    writer.stop()
    writer.stop()
    assert writer.healthy is False
    assert len(_rows(tmp_path / "history.db", "telemetry")) == 1


# ------------------------------------------------------------ the two opt-ins


@pytest.mark.parametrize("record_motion", [False, True])
def test_detector_motion_is_stored_only_when_the_operator_asked_for_it(tmp_path, record_motion):
    """Heading, speed and altitude at one second a piece reconstruct a route.

    Both directions are asserted on purpose.  Only checking the NULLs would
    still pass if the flag had been replaced by a constant, and the whole value
    of the flag is that the operator can turn it on and get the data.
    """
    path = tmp_path / "history.db"
    writer = _started_writer(path, record_motion=record_motion)
    writer.record_telemetry(_reading(), wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS)
    writer.stop()

    row = _rows(path, "telemetry")[0]
    if record_motion:
        assert row["direction_8"] == "W"
        assert row["speed_mph"] == 0
        assert row["altitude_ft"] == 193
    else:
        assert row["direction_8"] is None
        assert row["speed_mph"] is None
        assert row["altitude_ft"] is None

    # Neither setting touches what the detector is actually for, or the status
    # letter, which says whether the reading meant anything at all.
    assert row["voltage"] == pytest.approx(12.1)
    assert row["status_raw"] == "C"


@pytest.mark.parametrize("record_coordinates", [False, True])
def test_alert_coordinates_are_stored_only_when_the_operator_asked_for_them(
    tmp_path, record_coordinates
):
    """A fix with real coordinates is handed in either way; only one keeps it.

    The fix object is identical in both runs, so this proves the decision is
    made at write time by the flag and not by whatever the caller happened to
    pass.  Getting it backwards writes a map of a driver's week.
    """
    path = tmp_path / "history.db"
    writer = _started_writer(path, record_coordinates=record_coordinates)
    writer.record_alert_event(_event(), _fix())
    writer.stop()

    row = _rows(path, "alert_events")[0]
    if record_coordinates:
        assert row["lat"] == pytest.approx(LAT)
        assert row["lon"] == pytest.approx(LON)
    else:
        assert row["lat"] is None
        assert row["lon"] is None
        assert str(LAT) not in _dump(path)
        assert str(LON) not in _dump(path)

    # The fix quality is kept either way: it is how a reader judges whether the
    # absent coordinates were ever worth anything.
    assert row["gnss_mode"] == 3


@pytest.mark.parametrize("record_coordinates", [False, True])
def test_stored_fixes_follow_the_same_coordinate_opt_in(tmp_path, record_coordinates):
    """The gnss_fixes table is the denser trace, so it must obey the same flag."""
    path = tmp_path / "history.db"
    writer = _started_writer(path, record_coordinates=record_coordinates)
    writer.record_fix(_fix(), monotonic_ns=MONOTONIC_NS)
    writer.stop()

    row = _rows(path, "gnss_fixes")[0]
    if record_coordinates:
        assert (row["lat"], row["lon"]) == (pytest.approx(LAT), pytest.approx(LON))
    else:
        assert row["lat"] is None
        assert row["lon"] is None


def test_an_alert_with_no_fix_stores_nulls_rather_than_zeroes(tmp_path):
    """Absent is not the same as origin, and a zero here would read as a place."""
    path = tmp_path / "history.db"
    writer = _started_writer(path, record_coordinates=True)
    writer.record_alert_event(_event(), None)
    writer.stop()

    row = _rows(path, "alert_events")[0]
    for column in ("lat", "lon", "gnss_mode", "gnss_speed_mps", "gnss_track_deg"):
        assert row[column] is None, column


# -------------------------------------------------------------- no payloads


def test_no_table_in_the_schema_has_a_raw_payload_column(tmp_path):
    """One column holding the packet would carry everything past both opt-ins.

    The wire format keeps heading, speed, altitude and POI detail together in
    one byte string, so a ``payload`` column is not a convenience that could be
    filtered later -- it is the whole privacy design undone in one line.
    """
    with storage.History(tmp_path / "history.db") as history:
        connection = history.connection
        tables = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        assert set(tables) == EXPECTED_TABLES, "the schema grew a table this test has not read"

        offenders = []
        for table in tables:
            columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
            assert columns, f"{table} reported no columns; the enumeration is vacuous"
            for column in columns:
                name = str(column["name"]).lower()
                declared = str(column["type"]).lower()
                if (table, name) == PAYLOAD_EXEMPTION:
                    continue
                if name in FORBIDDEN_COLUMNS or "blob" in declared:
                    offenders.append(f"{table}.{column['name']} {column['type']}")
        assert not offenders, f"a raw payload column exists: {offenders}"


def test_the_one_payload_column_holds_alerts_and_never_telemetry(tmp_path):
    """The exemption has to stay exactly one table wide.

    An alert packet carries no position; a telemetry packet carries heading,
    speed, altitude and POI detail in a single field.  Storing the second whole
    would undo both opt-ins in one line, so the test that the exemption exists
    is only useful beside a test that it has not spread.
    """
    from uniden_r8 import telemetry as protocol

    writer = _started_writer(tmp_path / "history.db")
    note = _notification(b"1,00,KA,3,33,33.7850,R,1&0&0&0")
    writer.record_alert_snapshot(protocol.parse_alert_snapshot(note.payload), note)
    writer.record_telemetry(
        _reading(), wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS
    )
    writer.stop()

    snapshots = _rows(tmp_path / "history.db", "alert_snapshots")
    assert len(snapshots) == 1
    assert snapshots[0]["payload"] == "1,00,KA,3,33,33.7850,R,1&0&0&0"
    assert snapshots[0]["recognised"] == 1

    dumped = _dump(tmp_path / "history.db")
    assert TELEMETRY_PACKET.decode() not in dumped, (
        "a telemetry packet reached the disk whole"
    )


def test_alert_snapshots_can_be_switched_off(tmp_path):
    """It is on by default, so the off direction is the one worth proving."""
    from uniden_r8 import telemetry as protocol

    writer = _started_writer(tmp_path / "history.db", record_alert_snapshots=False)
    note = _notification(b"1,00,KA,3,33,33.7850,R,1&0&0&0")
    writer.record_alert_snapshot(protocol.parse_alert_snapshot(note.payload), note)
    writer.stop()
    assert _rows(tmp_path / "history.db", "alert_snapshots") == []


def test_no_stored_value_holds_the_packet_text(tmp_path):
    """Even with every opt-in on, the bytes off the wire are not what is kept.

    Run in the most permissive configuration on purpose: if the packet leaked
    into a column anywhere, this is the setting under which it would.
    """
    path = tmp_path / "history.db"
    writer = _started_writer(path, record_motion=True, record_coordinates=True)
    writer.record_telemetry(_reading(), wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS)
    writer.record_alert_event(_event(), _fix())
    writer.record_fix(_fix(), monotonic_ns=MONOTONIC_NS)
    writer.stop()

    dumped = _dump(path)
    assert "INSERT INTO" in dumped, "nothing was written; the scan would pass for free"
    for fragment in (
        TELEMETRY_PACKET.decode(),
        ALERT_PACKET.decode(),
        ALERT_PACKET.decode().split("&")[0],   # the alert slot on its own
        TELEMETRY_PACKET.decode().split("&")[2],   # the GPS sub-group on its own
        repr(TELEMETRY_PACKET),
    ):
        assert fragment not in dumped, fragment


def test_the_whole_database_dumps_without_an_identifier(tmp_path):
    """Nothing this writer stores is an address, whatever the opt-ins say.

    Dumped rather than queried column by column: the question is about the
    file, and a column added later would be missed by a list of column names.
    """
    path = tmp_path / "history.db"
    writer = _started_writer(path, record_motion=True, record_coordinates=True)
    writer.record_alert_event(_event("alert_start"), _fix())
    writer.record_alert_event(_event("alert_end", seq=2, duration_s=2.5), _fix())
    writer.record_telemetry(_reading(), wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS)
    writer.record_fix(_fix(), monotonic_ns=MONOTONIC_NS)
    writer.stop()

    dumped = _dump(path)
    assert dumped.count("INSERT INTO") >= 5, "too little was written to be a real scan"
    assert not looks_like_identifier(dumped)
    # The control must be able to fail, or a clean result proves nothing.
    assert looks_like_identifier(f"{dumped}\n-- device {RANDOM_STATIC}")


# --------------------------------------------------------- the queue is a wall


def test_a_full_write_queue_drops_and_counts_rather_than_growing_or_raising(tmp_path):
    """A backlog this deep means the card stopped answering; growing is worse.

    The writer is put into the state a wedged disk produces -- started, with
    nothing draining -- by filling the thread slot with the calling thread.
    A real writer thread would race this test for every row, and ``_offer``
    only ever asks whether a writer exists; nothing here reaches a connection.
    """
    writer = storage.HistoryWriter(tmp_path / "history.db")
    writer._thread = threading.current_thread()

    for _ in range(storage.WRITE_QUEUE_MAX):
        writer.record_telemetry(_reading(), wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS)
    assert writer.queued == storage.WRITE_QUEUE_MAX
    assert writer.dropped == 0

    overflow = 25
    for _ in range(overflow):
        writer.record_telemetry(_reading(), wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS)
        writer.record_alert_event(_event(), _fix())

    assert writer._queue.qsize() == storage.WRITE_QUEUE_MAX, "the queue grew past its cap"
    assert writer.dropped == overflow * 2
    assert writer.queued == storage.WRITE_QUEUE_MAX, "a dropped row must not count as queued"
    assert writer.status()["dropped"] == overflow * 2


# ------------------------------------------------------------ a broken path


def test_an_unwritable_path_fails_the_start_without_ever_raising(tmp_path):
    """The card is gone.  The collector keeps collecting; the flag goes false.

    The parent is an ordinary file, so the directory the database needs cannot
    be created at all -- the same shape as a card that unmounted mid-drive.
    Anything raised here would surface inside a notification callback.
    """
    blocked = tmp_path / "this-is-a-file-not-a-directory"
    blocked.write_text("a regular file, so nothing can be created beneath it\n")
    writer = storage.HistoryWriter(blocked / "state" / "history.db")

    assert writer.start(started_at=WALL_ISO, wall_ns=WALL_NS,
                        monotonic_ns=MONOTONIC_NS) is False
    assert writer.healthy is False

    # Every public method, on the loop's side of the queue, after the failure.
    writer.record_alert_event(_event(), _fix())
    writer.record_telemetry(_reading(), wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS)
    writer.record_fix(_fix(), monotonic_ns=MONOTONIC_NS)

    status = writer.status()
    assert status["healthy"] is False
    assert status["error"], "a failed open must be reported, not merely survived"
    assert (status["queued"], status["written"], status["errors"]) == (0, 0, 0)

    writer.stop()
    assert writer.healthy is False


def test_a_writer_that_never_started_ignores_every_call(tmp_path):
    """The collector may hold a writer the configuration disabled."""
    writer = storage.HistoryWriter(tmp_path / "history.db")
    writer.record_alert_event(_event(), _fix())
    writer.record_telemetry(_reading(), wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS)
    writer.stop()

    assert writer.status()["enabled"] is False
    assert writer.queued == 0
    assert not (tmp_path / "history.db").exists()


# ---------------------------------------------------------------- retention


def test_prune_with_a_zero_retention_deletes_nothing(tmp_path):
    """Zero means "keep everything", and it is the configured default.

    Reading zero as "older than now" would empty the database on the first
    start after an install, silently, with the operator having chosen nothing.
    """
    path = tmp_path / "history.db"
    writer = _started_writer(path, retain_days=0)
    writer.record_alert_event(_event(wall_ns=ANCIENT_WALL_NS), None)
    writer.record_telemetry(_reading(), wall_ns=ANCIENT_WALL_NS, monotonic_ns=MONOTONIC_NS)
    writer.stop()

    with storage.History(path) as history:
        assert history.prune(0) == 0
        assert history.prune(-7) == 0
        counts = history.stats()["counts"]
    assert counts["alert_events"] == 1
    assert counts["telemetry"] == 1
    assert counts["sessions"] == 1


def test_prune_with_a_real_retention_removes_only_what_is_older(tmp_path):
    """The zero case only means something if the sweep otherwise works."""
    path = tmp_path / "history.db"
    writer = _started_writer(path, retain_days=0)
    writer.record_alert_event(_event(seq=1, wall_ns=ANCIENT_WALL_NS), None)
    writer.record_alert_event(_event(seq=2, wall_ns=WALL_NS), None)
    writer.record_telemetry(_reading(), wall_ns=ANCIENT_WALL_NS, monotonic_ns=MONOTONIC_NS)
    writer.stop()

    with storage.History(path) as history:
        assert history.prune(30) == 2
        assert [row["seq"] for row in history.events()] == [2]
        assert history.telemetry() == []


def test_the_startup_sweep_does_not_delete_the_session_it_has_just_opened(tmp_path):
    """The retention sweep must not erase the drive it is about to record.

    ``prune`` deletes sessions by ``started_wall_ns``, so the order of the two
    startup steps decides this: sweeping after ``begin_session`` removes the
    row the writer is holding an id for, and every alert of that drive is then
    written against a session that is not in the file.

    The clock is what makes it reachable rather than theoretical, which is why
    the session here is started with a stale one.  This runs on a Pi Zero 2 W,
    which has no RTC; with no network at boot -- the normal state of a vehicle
    node -- the wall clock is whatever was last saved, and being further behind
    than ``retain_days`` is an ordinary Tuesday.  A sessions table that empties
    itself at startup is exactly the "quietly stopped recording" failure the
    module docstring says this design will not have.
    """
    path = tmp_path / "history.db"
    writer = storage.HistoryWriter(path, retain_days=30)
    assert writer.start(
        started_at=iso_from_wall_ns(ANCIENT_WALL_NS),
        wall_ns=ANCIENT_WALL_NS,
        monotonic_ns=MONOTONIC_NS,
    ) is True
    writer.record_alert_event(_event(), None)
    writer.stop()

    stored = _rows(path, "alert_events")
    sessions = _rows(path, "sessions")
    assert stored, "the alert must be recorded whatever the sweep did"
    assert sessions, "the sweep deleted the session the writer had just opened"
    assert stored[0]["session_id"] == sessions[0]["id"]


# ------------------------------------------------------------ schema refusal


def test_a_schema_version_mismatch_is_refused_rather_than_reinterpreted(tmp_path):
    """There is no migration here, so an old file must not be read as a new one.

    Silently mixing rows written under two sets of meanings produces a history
    that reads plausibly and is wrong, which is worse than one that will not
    open at all.
    """
    path = tmp_path / "history.db"
    storage.History(path).open().close()

    raw = sqlite3.connect(path)
    try:
        raw.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema'",
            (str(storage.SCHEMA_VERSION + 1),),
        )
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(storage.HistoryError) as refusal:
        storage.History(path).open()
    assert str(storage.SCHEMA_VERSION) in str(refusal.value)
    assert "aside" in str(refusal.value), "the refusal should say what to do about it"

    with pytest.raises(storage.HistoryError):
        storage.open_history(path)


def test_a_database_with_no_schema_marker_is_refused(tmp_path):
    """A foreign SQLite file is not an empty history, and must not become one."""
    path = tmp_path / "someone-elses.db"
    raw = sqlite3.connect(path)
    try:
        raw.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(storage.HistoryError):
        storage.open_history(path)


def test_a_file_that_is_not_a_database_is_refused_with_a_history_error(tmp_path):
    """A truncated or overwritten history is an ordinary outcome of a power cut.

    ``HistoryError`` is documented as "could not be opened or is a different
    schema", and this is the first half of that sentence.  It has to hold at
    the pragmas, not only at the version check: setting ``journal_mode`` is the
    first statement that reads the file header, so it is where a bad file
    announces itself.  The CLI's ``history`` command catches ``HistoryError``
    and nothing else, so anything escaping from here is a traceback in the
    operator's face instead of the one-line report it was written to print.
    """
    path = tmp_path / "history.db"
    path.write_text("this was a history until the ignition was cut\n" * 40)

    with pytest.raises(storage.HistoryError):
        storage.open_history(path)


def test_the_read_only_handle_refuses_a_database_that_is_not_there(tmp_path):
    """Querying a history that was never written is a report, not a new file."""
    with pytest.raises(storage.HistoryError):
        storage.open_history(tmp_path / "absent.db")
    assert not (tmp_path / "absent.db").exists()


def test_using_a_closed_history_is_an_error_and_not_a_silent_reopen(tmp_path):
    """A reopened connection would be a second writer nobody asked for."""
    history = storage.History(tmp_path / "history.db").open()
    history.close()
    with pytest.raises(storage.HistoryError):
        _ = history.connection


# ----------------------------------------------------------------- the reads


def _populated(path) -> storage.HistoryWriter:
    """Three transitions, two of them ends, and three telemetry samples."""
    writer = _started_writer(path, record_motion=True, record_coordinates=True)
    writer.record_alert_event(_event("alert_start", seq=1, track_id=1), _fix())
    writer.record_alert_event(
        _event("alert_end", seq=2, track_id=1, duration_s=3.5, samples=9,
               max_strength=4, max_raw_signal=38),
        _fix(),
    )
    writer.record_alert_event(
        _event("alert_end", seq=3, track_id=2, duration_s=1.25, samples=2,
               max_strength=2, max_raw_signal=17),
        _fix(),
    )
    for index in range(3):
        writer.record_telemetry(
            _reading(), wall_ns=WALL_NS + index, monotonic_ns=MONOTONIC_NS + index,
        )
    writer.stop()
    return writer


def test_stats_reports_counts_a_span_and_a_size(tmp_path):
    """Cheap enough to print anywhere, which is why the CLI leans on it."""
    path = tmp_path / "history.db"
    _populated(path)

    with storage.open_history(path) as history:
        stats = history.stats()

    assert stats["path"] == str(path)
    assert stats["schema"] == storage.SCHEMA_VERSION
    assert stats["size_bytes"] > 0
    assert stats["counts"] == {
        "sessions": 1, "alert_events": 3, "alert_snapshots": 0,
        "telemetry": 3, "gnss_fixes": 0,
    }
    assert stats["first_alert_at"] == WALL_ISO
    assert stats["last_alert_at"] == WALL_ISO


def test_stats_on_an_empty_history_reports_zeroes_rather_than_failing(tmp_path):
    """An operator's first `history stats` runs before anything has alerted."""
    path = tmp_path / "history.db"
    with storage.History(path) as history:
        stats = history.stats()
    assert stats["counts"]["alert_events"] == 0
    assert stats["first_alert_at"] is None
    assert stats["last_alert_at"] is None


def test_events_returns_transitions_newest_first_and_respects_its_limit(tmp_path):
    """Newest first because the question is almost always "what just happened"."""
    path = tmp_path / "history.db"
    _populated(path)

    with storage.open_history(path) as history:
        assert [row["seq"] for row in history.events()] == [3, 2, 1]
        assert [row["seq"] for row in history.events(limit=2)] == [3, 2]
        assert [row["kind"] for row in history.events(kind="alert_start")] == ["alert_start"]
        assert history.events(kind="nothing-of-that-kind") == []
        # A limit of zero is clamped up, not down: returning nothing would look
        # exactly like an empty history to whatever printed it.
        assert len(history.events(limit=0)) == 1
        assert len(history.events(limit=-5)) == 1
        # And an absurd limit is clamped down rather than trusted.
        assert len(history.events(limit=10_000_000)) == 3
        assert set(history.events(limit=1)[0]) >= {
            "id", "session_id", "seq", "kind", "track_id", "at", "wall_ns", "band",
        }


def test_encounters_returns_only_completed_threats(tmp_path):
    """An encounter is a finished thing; a start on its own is not one yet."""
    path = tmp_path / "history.db"
    _populated(path)

    with storage.open_history(path) as history:
        encounters = history.encounters()
        assert [row["seq"] for row in encounters] == [3, 2]
        assert all(row["kind"] == "alert_end" for row in encounters)
        assert encounters[0]["duration_s"] == pytest.approx(1.25)
        assert encounters[0]["max_strength"] == 2
        assert len(history.encounters(limit=1)) == 1
        assert len(history.encounters(limit=0)) == 1


def test_telemetry_returns_samples_newest_first_and_respects_its_limit(tmp_path):
    """The same clamping as events, because the same CLI flag reaches both."""
    path = tmp_path / "history.db"
    _populated(path)

    with storage.open_history(path) as history:
        samples = history.telemetry()
        assert len(samples) == 3
        assert [row["wall_ns"] for row in samples] == [WALL_NS + 2, WALL_NS + 1, WALL_NS]
        assert len(history.telemetry(limit=2)) == 2
        assert len(history.telemetry(limit=0)) == 1
        assert set(samples[0]) >= {"id", "session_id", "at", "voltage", "gps_locked"}


# ------------------------------------------------------- retention, bounded

def _bulk(history, table: str, count: int, wall_ns: int, tag: str = "t") -> None:
    """Insert *count* rows into *table* at one timestamp."""
    for index in range(count):
        history.connection.execute(
            "INSERT INTO alert_events (session_id, seq, kind, track_id, at, "
            "wall_ns, monotonic_ns, band) VALUES (1, ?, 'alert_end', 1, ?, ?, 1, 'KA')",
            (index, tag, wall_ns),
        )


def test_the_row_budget_bounds_the_table_without_consulting_a_clock(tmp_path):
    """The bound that actually protects the card.

    `rowid` is monotone and completely clock-immune, which matters because the
    board this runs on has no battery-backed clock and a vehicle node often
    never sees NTP at all. A retention scheme that needs a trustworthy clock
    is a retention scheme that never runs there.
    """
    path = tmp_path / "history.db"
    with storage.History(path) as history:
        history.begin_session(
            started_at=WALL_ISO, wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS
        )
        _bulk(history, "alert_events", 50, WALL_NS)

        # Retention off entirely; only the budget applies.
        history.prune(0, max_rows=20)
        rows = history.connection.execute(
            "SELECT COUNT(*) AS n FROM alert_events"
        ).fetchone()["n"]
        assert rows == 20

        # And it keeps the NEWEST, which is what "keep the last N" has to mean.
        kept = [row["seq"] for row in history.events(limit=100)]
        assert max(kept) == 49
        assert min(kept) == 30


def test_a_sweep_that_wants_most_of_a_table_is_refused_and_recorded(tmp_path):
    """The guard the rank statistic cannot provide on its own.

    Eleven rows written under a wrong clock are enough to move a tenth-newest
    reference, and a vehicle with no network writes eleven telemetry rows in
    under two minutes. This is the belt: whatever the reference says, a sweep
    that wants most of the history is not bounding disk usage and is refused.
    """
    path = tmp_path / "history.db"
    real_now = time.time_ns()
    day = 86_400 * 1_000_000_000
    bad_clock = real_now + 23_000 * day          # the board briefly reads ~2090

    with storage.History(path) as history:
        history.begin_session(
            started_at=WALL_ISO, wall_ns=real_now - 5 * day,
            monotonic_ns=MONOTONIC_NS,
        )
        _bulk(history, "alert_events", 40, real_now - day, tag="real")
        _bulk(history, "alert_events", 20, bad_clock, tag="bad")

        original = storage.time.time_ns
        storage.time.time_ns = lambda: bad_clock
        try:
            history.prune(30)
        finally:
            storage.time.time_ns = original

        survivors = history.connection.execute(
            "SELECT COUNT(*) AS n FROM alert_events WHERE at = 'real'"
        ).fetchone()["n"]
        assert survivors == 40, "a clock excursion destroyed the real history"

        record = history.last_prune()
        assert any(entry.startswith("alert_events:") for entry in record["refused"]), (
            "a refusal nobody can see is the half of this arithmetic cannot fix"
        )


def test_a_small_sweep_is_never_refused_by_the_fraction(tmp_path):
    """A proportion is meaningless on a handful of rows.

    Removing one row of two is far past the fraction and is also not the mass
    deletion the guard exists to stop.
    """
    path = tmp_path / "history.db"
    with storage.History(path) as history:
        history.begin_session(
            started_at=WALL_ISO, wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS
        )
        _bulk(history, "alert_events", 1, ANCIENT_WALL_NS, tag="old")
        _bulk(history, "alert_events", 1, time.time_ns(), tag="new")
        assert history.prune(30) >= 1
        assert history.last_prune()["refused"] == []


def test_what_the_sweep_did_is_recorded_and_reported(tmp_path):
    """A deletion nobody can see is the failure arithmetic cannot fix."""
    path = tmp_path / "history.db"
    with storage.History(path) as history:
        history.begin_session(
            started_at=WALL_ISO, wall_ns=WALL_NS, monotonic_ns=MONOTONIC_NS
        )
        assert history.last_prune() == {}, "nothing before the first sweep"
        history.prune(30)
        record = history.last_prune()
        assert set(record) == {"at", "removed", "refused", "reference_ns"}
        assert history.stats()["last_prune"] == record


def test_the_writer_publishes_what_its_startup_sweep_did(tmp_path):
    """The collector republishes this; it used to be discarded on the floor."""
    writer = _started_writer(tmp_path / "history.db", retain_days=30, max_rows=10)
    try:
        status = writer.status()
        assert "pruned" in status
        assert status["prune_refused"] == []
    finally:
        writer.stop()


def test_a_schema_two_database_is_migrated_rather_than_refused(tmp_path):
    """An additive upgrade must not orphan a database full of real drives.

    A bare refusal is raised inside the writer thread, which records the error
    and returns -- so the collector keeps running, keeps publishing a healthy
    state document, and silently stops recording.  On a vehicle that means the
    first upgrade after an install quietly loses every subsequent drive, and
    nobody finds out until they look for a detection that should be there.
    """
    path = tmp_path / "history.db"

    # Build a schema-2 database the way the previous build would have.
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta (key, value) VALUES ('schema', '2');
            CREATE TABLE telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER, at TEXT, wall_ns INTEGER,
                monotonic_ns INTEGER, voltage REAL, gps_locked INTEGER,
                poi_active INTEGER, direction_8 TEXT, speed_mph INTEGER,
                altitude_ft INTEGER, status_raw TEXT, warning_raw TEXT,
                scan_raw TEXT
            );
            INSERT INTO telemetry (voltage, poi_active) VALUES (13.6, 0);
            """
        )

    history = storage.History(path)
    history.open()
    try:
        columns = {
            row[1] for row in history.connection.execute(
                "PRAGMA table_info(telemetry)"
            )
        }
        assert "poi_raw" in columns
        assert "poi_suspect" in columns

        # The pre-existing row survives, and reads NULL for what it never
        # recorded -- which is the truth, not a default.
        row = history.connection.execute(
            "SELECT voltage, poi_raw, poi_suspect FROM telemetry"
        ).fetchone()
        assert row[0] == pytest.approx(13.6)
        assert row[1] is None
        assert row[2] is None

        marker = history.connection.execute(
            "SELECT value FROM meta WHERE key = 'schema'"
        ).fetchone()
        assert marker[0] == str(storage.SCHEMA_VERSION)
    finally:
        history.close()


def test_a_database_from_an_unknown_schema_is_still_refused(tmp_path):
    """Migration is for additions only; a change of meaning must still stop.

    The companion to the test above: without this one, "we added a migration"
    would quietly become "we open anything", and rows written under different
    meanings would be mixed silently.
    """
    path = tmp_path / "history.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta (key, value) VALUES ('schema', '99');"
        )

    history = storage.History(path)
    with pytest.raises(storage.HistoryError, match="schema 99"):
        history.open()
    history.close()


def _schema_two(path):
    """A database as the previous build would have left it.

    The full column list matters: `_create` re-runs the index statements on
    open, and `telemetry_at` needs the `at` column to exist.
    """
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta (key, value) VALUES ('schema', '2');
            CREATE TABLE telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER, at TEXT, wall_ns INTEGER,
                monotonic_ns INTEGER, voltage REAL, gps_locked INTEGER,
                poi_active INTEGER, direction_8 TEXT, speed_mph INTEGER,
                altitude_ft INTEGER, status_raw TEXT, warning_raw TEXT,
                scan_raw TEXT
            );
            """
        )


def test_a_read_only_open_refuses_to_migrate_rather_than_rewriting_the_file(tmp_path):
    """`uniden-r8 history` is a query. A query must not rewrite the database.

    `_check_version` calls `_migrate`, which runs ALTER TABLE and updates the
    schema marker — so a read-only open of an older database silently upgraded
    it. Verified before the fix: a schema-2 file came back as schema 3 with two
    new columns after nothing more than being opened to print rows from.

    Refusing is the right failure. Migrating is a decision, and the command
    whose job is to print rows should not make it for the operator.
    """
    path = tmp_path / "history.db"
    _schema_two(path)

    history = storage.History(path, read_only=True)
    with pytest.raises(storage.HistoryError, match="read-only open will not migrate"):
        history.open()
    history.close()

    # And the file is untouched.
    with sqlite3.connect(path) as connection:
        marker = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema'"
        ).fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(telemetry)")}
    assert marker == "2"
    assert "poi_raw" not in columns


def test_a_read_write_open_still_migrates(tmp_path):
    """The companion: refusing on read-only must not disable migration entirely.

    Without this, "read-only refuses" could be satisfied by never migrating at
    all — which would strand every existing database on the next schema bump,
    the failure the migration was written to prevent.
    """
    path = tmp_path / "history.db"
    _schema_two(path)

    history = storage.History(path)          # read-write
    history.open()
    try:
        assert history._stored_schema() == storage.SCHEMA_VERSION
    finally:
        history.close()


def test_stats_reports_the_schema_the_file_carries(tmp_path):
    """Not the version this build happens to write.

    `stats()` exists to report on a file. Printing the constant compiled into
    the code told the operator what the software believes rather than what the
    database says — the wrong way round for that command.
    """
    path = tmp_path / "history.db"
    _populated(path)
    with storage.open_history(path) as history:
        assert history.stats()["schema"] == history._stored_schema()
