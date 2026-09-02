"""Local history: a small SQLite database, written from a thread that cannot
stall the radio.

The state file answers "what is happening now".  It cannot answer "what
alerted, where, when, for how long, and how strong", because it only ever holds
the present.  This module is the answer to the second question, and it has two
hard constraints that shaped every decision in it.

**It must never block the event loop.**  The same loop holds the BLE link and
runs the notification callbacks.  An ``fsync`` on the SD card of a Pi Zero 2 W
can take tens of milliseconds; a synchronous write in the middle of a Ka alert
would delay the very packets this history exists to record.  So every write
goes through :class:`HistoryWriter`, which owns a private connection on its own
thread and is fed by a queue.  The loop's side of that queue is a
non-blocking append.

**It must survive losing power.**  This runs in a vehicle.  The ignition is
cut mid-transaction routinely, and there is no clean shutdown.  WAL mode with
``synchronous=NORMAL`` is the configuration that fits: a power loss can lose
the last few committed transactions, and cannot corrupt the database.  Losing
the last second of telemetry on a hard power cut is an acceptable trade; a
corrupt file that takes the whole history with it is not.  ``journal_size_limit``
and a bounded autocheckpoint keep the WAL from growing without limit on a card
with finite write endurance.

What is stored, and what is opt-in
----------------------------------
Alert transitions are stored in full: they are the point.  Telemetry is
throttled -- one packet a second for a whole drive is tens of thousands of rows
of near-identical voltage readings -- and its position-adjacent fields (the
detector's heading, speed and altitude) are written only when
``history.record_detector_motion`` is on.  External coordinates are written only
when ``gnss.record_coordinates`` is on.  Both default to off, because a history
of where a vehicle has been is a different kind of file from a history of what
its radar detector heard.

The database is created ``0600`` inside a ``0700`` directory, and the retention
sweep is the only thing that deletes rows.
"""

from __future__ import annotations

import contextlib
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .evidence import DIR_MODE, FILE_MODE

__all__ = [
    "SCHEMA_VERSION",
    "History",
    "HistoryWriter",
    "HistoryError",
    "open_history",
]

#: Bumped when a table changes shape.  Migration is deliberately absent: this
#: is diagnostic history, not a system of record, and a version mismatch is
#: reported so the operator can archive the old file rather than silently
#: reinterpreting rows written under different meanings.
SCHEMA_VERSION: Final[int] = 1

#: Cap on the write queue.  Everything here is small; a backlog this deep means
#: the disk has stopped answering, and dropping is better than growing.
WRITE_QUEUE_MAX: Final[int] = 4096

#: Rows buffered before a commit, and the longest a buffered row waits.  Both
#: bounds exist: batching protects the SD card from a commit per packet, and
#: the time bound stops a quiet period from leaving the last alert unwritten.
BATCH_ROWS: Final[int] = 64
BATCH_SECONDS: Final[float] = 2.0

_SCHEMA: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at          TEXT    NOT NULL,
        started_wall_ns     INTEGER NOT NULL,
        started_monotonic_ns INTEGER NOT NULL,
        ended_at            TEXT,
        adapter             TEXT,
        note                TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alert_events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id     INTEGER NOT NULL,
        seq            INTEGER NOT NULL,
        kind           TEXT    NOT NULL,
        track_id       INTEGER NOT NULL,
        at             TEXT    NOT NULL,
        wall_ns        INTEGER NOT NULL,
        monotonic_ns   INTEGER NOT NULL,
        band           TEXT,
        strength       INTEGER,
        raw_signal     INTEGER,
        frequency_ghz  REAL,
        laser_gun_id   INTEGER,
        direction      TEXT,
        mute_code      TEXT,
        alert_id_raw   TEXT,
        receive_mode   TEXT,
        correlation    TEXT,
        material       INTEGER,
        duration_s     REAL,
        samples        INTEGER,
        max_strength   INTEGER,
        max_raw_signal INTEGER,
        lat            REAL,
        lon            REAL,
        gnss_mode      INTEGER,
        gnss_speed_mps REAL,
        gnss_track_deg REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS telemetry (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id    INTEGER NOT NULL,
        at            TEXT    NOT NULL,
        wall_ns       INTEGER NOT NULL,
        monotonic_ns  INTEGER NOT NULL,
        voltage       REAL,
        gps_locked    INTEGER,
        poi_active    INTEGER,
        direction_8   TEXT,
        speed_mph     INTEGER,
        altitude_ft   INTEGER,
        status_raw    TEXT,
        warning_raw   TEXT,
        scan_raw      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gnss_fixes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   INTEGER NOT NULL,
        at           TEXT    NOT NULL,
        wall_ns      INTEGER NOT NULL,
        monotonic_ns INTEGER NOT NULL,
        mode         INTEGER,
        lat          REAL,
        lon          REAL,
        alt_m        REAL,
        speed_mps    REAL,
        track_deg    REAL,
        epx_m        REAL,
        epy_m        REAL,
        satellites   INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS alert_events_at ON alert_events (at)",
    "CREATE INDEX IF NOT EXISTS alert_events_track ON alert_events (session_id, track_id)",
    "CREATE INDEX IF NOT EXISTS telemetry_at ON telemetry (at)",
    "CREATE INDEX IF NOT EXISTS gnss_at ON gnss_fixes (at)",
)


class HistoryError(RuntimeError):
    """The history database could not be opened or is a different schema."""


@dataclass(frozen=True)
class _Write:
    """One queued statement.  Kept generic so the writer thread stays dumb."""

    sql: str
    parameters: tuple


class History:
    """A synchronous handle on the database.

    Used directly by the read-only query commands, and privately by
    :class:`HistoryWriter`.  Not safe to share between threads -- each user
    opens its own connection, which is what ``sqlite3`` wants anyway.
    """

    def __init__(self, path: str | os.PathLike[str], *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self._connection: sqlite3.Connection | None = None

    # ------------------------------------------------------------ lifecycle

    def open(self) -> History:
        """Open, create if needed, and apply the pragmas that matter."""
        if self._connection is not None:
            return self

        if self.read_only and not self.path.exists():
            raise HistoryError(f"no history database at {self.path}")

        self.path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)

        previous = os.umask(0o077)
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None,      # explicit transactions, not implicit
                check_same_thread=True,
            )
        finally:
            os.umask(previous)

        connection.row_factory = sqlite3.Row
        try:
            # WAL so a reader (the CLI, a dashboard) never blocks the writer,
            # and so a power cut cannot corrupt the file.  NORMAL rather than
            # FULL because the cost of FULL is an fsync per commit on an SD
            # card and the benefit is only the last few transactions, which
            # this data can lose.
            #
            # Setting journal_mode is also the first statement that touches the
            # file's header, so it is where "this is not a database" surfaces.
            # It is wrapped because the caller was promised a HistoryError, and
            # a bare sqlite3 traceback in an operator's terminal is not that.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            # Bound the WAL.  Without this a long drive grows it until the next
            # checkpoint, and this card has finite space and finite writes.
            connection.execute("PRAGMA journal_size_limit=4194304")
            connection.execute("PRAGMA wal_autocheckpoint=512")
            connection.execute("PRAGMA temp_store=MEMORY")
        except sqlite3.DatabaseError as exc:
            connection.close()
            raise HistoryError(f"{self.path} is not a uniden-r8 history") from exc

        self._connection = connection
        try:
            if not self.read_only:
                self._create()
            self._check_version()
        except sqlite3.DatabaseError as exc:
            self.close()
            raise HistoryError(f"{self.path} is not a uniden-r8 history") from exc
        self._secure()
        return self

    def _secure(self) -> None:
        """Tighten the database file and both WAL sidecars to ``0600``.

        The sidecars are the reason this exists.  SQLite creates ``-wal`` and
        ``-shm`` itself, lazily, at the first write -- which is long after the
        umask was closed around ``connect()``.  Left alone they take the
        process umask, and a ``-wal`` file holds the most recent rows written:
        exactly the alert events, and, when the operator has enabled it, the
        coordinates.  A history that is ``0600`` with a group-readable journal
        beside it is not a private history.
        """
        for candidate in (
            self.path,
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        ):
            with contextlib.suppress(OSError):
                if candidate.exists() and candidate.stat().st_mode & 0o177:
                    os.chmod(candidate, FILE_MODE)

    def close(self) -> None:
        if self._connection is not None:
            with contextlib.suppress(Exception):
                self._connection.execute("PRAGMA optimize")
            with contextlib.suppress(Exception):
                self._connection.close()
            self._connection = None

    def __enter__(self) -> History:
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise HistoryError("the history database is not open")
        return self._connection

    def _create(self) -> None:
        connection = self.connection
        connection.execute("BEGIN")
        try:
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def _check_version(self) -> None:
        try:
            row = self.connection.execute(
                "SELECT value FROM meta WHERE key = 'schema'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise HistoryError(f"{self.path} is not a uniden-r8 history") from exc
        if row is None:
            raise HistoryError(f"{self.path} has no schema marker")
        if row["value"] != str(SCHEMA_VERSION):
            raise HistoryError(
                f"{self.path} is schema {row['value']}, this build writes "
                f"{SCHEMA_VERSION}.  Move the old file aside rather than "
                f"mixing rows written under different meanings."
            )

    # ---------------------------------------------------------------- write

    def begin_session(self, *, started_at: str, wall_ns: int, monotonic_ns: int,
                      adapter: str = "") -> int:
        cursor = self.connection.execute(
            "INSERT INTO sessions (started_at, started_wall_ns, "
            "started_monotonic_ns, adapter) VALUES (?, ?, ?, ?)",
            (started_at, wall_ns, monotonic_ns, adapter or None),
        )
        return int(cursor.lastrowid or 0)

    def end_session(self, session_id: int, *, ended_at: str, note: str = "") -> None:
        self.connection.execute(
            "UPDATE sessions SET ended_at = ?, note = ? WHERE id = ?",
            (ended_at, note or None, session_id),
        )

    def apply(self, writes: list[_Write]) -> None:
        """Execute a batch in one transaction."""
        if not writes:
            return
        connection = self.connection
        connection.execute("BEGIN")
        try:
            for write in writes:
                connection.execute(write.sql, write.parameters)
            connection.execute("COMMIT")
            # SQLite creates -wal and -shm at the first write, after the umask
            # window closed around connect().  Tighten them once they exist.
            self._secure()
        except Exception:
            with contextlib.suppress(Exception):
                connection.execute("ROLLBACK")
            raise

    def newest_wall_ns(self) -> int | None:
        """The newest timestamp anywhere in the database, or ``None`` if empty."""
        newest: int | None = None
        for table, column in (
            ("alert_events", "wall_ns"), ("telemetry", "wall_ns"),
            ("gnss_fixes", "wall_ns"), ("sessions", "started_wall_ns"),
        ):
            row = self.connection.execute(
                f"SELECT MAX({column}) AS newest FROM {table}"  # noqa: S608
            ).fetchone()
            value = row["newest"] if row else None
            if isinstance(value, int) and (newest is None or value > newest):
                newest = value
        return newest

    def prune(self, retain_days: int) -> int:
        """Delete rows older than *retain_days*.  Returns rows removed.

        Zero means never expire, and is a decision rather than a default: the
        configuration says so out loud in :meth:`Config.warnings`.

        **The reference instant is not simply "now".**  This runs on a Pi Zero
        2 W, which has no battery-backed clock: at every cold boot the wall
        clock reads whatever was written at the last shutdown, and it then
        steps by hours when the network appears.  A retention sweep of the form
        "delete everything older than now minus thirty days" is a data-
        destruction trigger on hardware like that -- a clock that briefly reads
        a date in 2090 deletes the entire history, permanently, with no error.

        So the reference is ``min(now, newest row we hold)``.  A clock stuck in
        the past makes the cutoff early and deletes nothing; a clock jumped
        into the future is ignored in favour of real data; and a correct clock
        behaves as expected because the newest row is never in the future.
        """
        if retain_days <= 0:
            return 0
        newest = self.newest_wall_ns()
        reference = time.time_ns() if newest is None else min(time.time_ns(), newest)
        cutoff_ns = reference - retain_days * 86_400 * 1_000_000_000
        removed = 0
        connection = self.connection
        connection.execute("BEGIN")
        try:
            for table in ("alert_events", "telemetry", "gnss_fixes"):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE wall_ns < ?", (cutoff_ns,)  # noqa: S608
                )
                removed += cursor.rowcount if cursor.rowcount > 0 else 0
            connection.execute(
                "DELETE FROM sessions WHERE started_wall_ns < ?", (cutoff_ns,)
            )
            connection.execute("COMMIT")
        except Exception:
            with contextlib.suppress(Exception):
                connection.execute("ROLLBACK")
            raise
        return removed

    # ----------------------------------------------------------------- read

    def stats(self) -> dict[str, Any]:
        """Row counts and the span covered.  Cheap enough to print anywhere."""
        connection = self.connection
        counts = {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608 - fixed names
                ).fetchone()["n"]
            )
            for table in ("sessions", "alert_events", "telemetry", "gnss_fixes")
        }
        span = connection.execute(
            "SELECT MIN(at) AS first, MAX(at) AS last FROM alert_events"
        ).fetchone()
        return {
            "path": str(self.path),
            "schema": SCHEMA_VERSION,
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "counts": counts,
            "first_alert_at": span["first"],
            "last_alert_at": span["last"],
        }

    def encounters(self, limit: int = 50) -> list[dict[str, Any]]:
        """One row per completed threat, newest first.

        Reconstructed from the ``alert_end`` rows because those already carry
        the aggregates -- duration, peak strength, sample count -- that the
        tracker computed while the encounter was live.
        """
        rows = self.connection.execute(
            "SELECT * FROM alert_events WHERE kind = 'alert_end' "
            "ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 10_000)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def events(self, limit: int = 200, kind: str | None = None) -> list[dict[str, Any]]:
        """Raw transitions, newest first."""
        bounded = max(1, min(limit, 10_000))
        if kind:
            rows = self.connection.execute(
                "SELECT * FROM alert_events WHERE kind = ? ORDER BY id DESC LIMIT ?",
                (kind, bounded),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM alert_events ORDER BY id DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [dict(row) for row in rows]

    def telemetry(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM telemetry ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 10_000)),),
        ).fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# The writer thread
# --------------------------------------------------------------------------


class HistoryWriter:
    """Non-blocking writes: the loop enqueues, a thread commits.

    The public methods are all called from the asyncio loop and all of them do
    the same three things -- build a tuple, append it to a queue, return.  None
    of them touches the disk, acquires a lock that the disk holds, or raises:
    the collector must keep collecting when the card is slow, full or gone.

    Failures surface as counters rather than exceptions, and the collector
    publishes them.  A history that quietly stopped recording would be worse
    than no history at all.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        retain_days: int = 30,
        record_motion: bool = False,
        record_coordinates: bool = False,
    ) -> None:
        self.path = Path(path)
        self.retain_days = retain_days
        self.record_motion = record_motion
        self.record_coordinates = record_coordinates
        self._queue: queue.Queue[_Write | None] = queue.Queue(maxsize=WRITE_QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._session_id = 0
        self._started = threading.Event()
        self._open_error: str = ""
        self.queued = 0
        self.written = 0
        self.dropped = 0
        self.errors = 0

    # ------------------------------------------------------------ lifecycle

    def start(self, *, started_at: str, wall_ns: int, monotonic_ns: int,
              adapter: str = "") -> bool:
        """Open the database on the writer thread.  Returns success.

        Opening happens on the thread that will use the connection because
        ``sqlite3`` connections are not portable across threads, and doing it
        here means a slow or failing open cannot stall the caller either.
        """
        if self._thread is not None:
            return not self._open_error
        self._thread = threading.Thread(
            target=self._run,
            args=(started_at, wall_ns, monotonic_ns, adapter),
            name="uniden-r8-history",
            daemon=True,
        )
        self._thread.start()
        # Bounded: if the card is wedged, the collector still starts, with the
        # failure visible in `healthy` rather than as a hang.
        self._started.wait(timeout=10.0)
        return not self._open_error

    def stop(self, timeout: float = 10.0) -> None:
        """Flush and close.  Safe to call more than once."""
        if self._thread is None:
            return
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def healthy(self) -> bool:
        return self._thread is not None and not self._open_error

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._thread is not None,
            "healthy": self.healthy,
            "path": str(self.path),
            "queued": self.queued,
            "written": self.written,
            "dropped": self.dropped,
            "errors": self.errors,
            "error": self._open_error or "",
        }

    # ---------------------------------------------------------------- write

    def _offer(self, sql: str, parameters: tuple) -> None:
        """Append one statement.  Never blocks, never raises."""
        if self._thread is None or self._open_error:
            return
        try:
            self._queue.put_nowait(_Write(sql, parameters))
            self.queued += 1
        except queue.Full:
            self.dropped += 1

    def record_alert_event(self, event: Any, fix: Any = None) -> None:
        """Store one transition, with the nearest GNSS fix if there is one.

        The fix is attached here rather than joined later because "nearest"
        depends on both clocks and both sources' health, and deciding it once
        at write time keeps the stored row unambiguous.
        """
        alert = event.alert
        lat = lon = None
        mode = speed = track = None
        if fix is not None:
            mode = fix.mode
            speed = fix.speed_mps
            track = fix.track_deg
            if self.record_coordinates:
                lat, lon = fix.lat, fix.lon
        self._offer(
            "INSERT INTO alert_events (session_id, seq, kind, track_id, at, "
            "wall_ns, monotonic_ns, band, strength, raw_signal, frequency_ghz, "
            "laser_gun_id, direction, mute_code, alert_id_raw, receive_mode, "
            "correlation, material, duration_s, samples, max_strength, "
            "max_raw_signal, lat, lon, gnss_mode, gnss_speed_mps, gnss_track_deg) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._session_id, event.seq, event.kind, event.track_id,
                _iso(event.wall_ns), event.wall_ns, event.monotonic_ns,
                alert.band, alert.strength, alert.signal, alert.frequency_ghz,
                alert.laser_gun_id, alert.direction_name, alert.mute_code,
                alert.alert_id_raw, alert.receive_mode_raw,
                event.correlation, int(bool(event.material)),
                event.duration_s, event.samples, event.max_strength,
                event.max_raw_signal, lat, lon, mode, speed, track,
            ),
        )

    def record_telemetry(self, reading: Any, *, wall_ns: int,
                         monotonic_ns: int) -> None:
        """Store one telemetry sample.  Motion fields respect the opt-in."""
        gps = reading.gps
        motion = (
            (gps.direction_8, gps.speed_mph, gps.altitude_ft)
            if self.record_motion else (None, None, None)
        )
        self._offer(
            "INSERT INTO telemetry (session_id, at, wall_ns, monotonic_ns, "
            "voltage, gps_locked, poi_active, direction_8, speed_mph, "
            "altitude_ft, status_raw, warning_raw, scan_raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._session_id, _iso(wall_ns), wall_ns, monotonic_ns,
                reading.voltage,
                None if reading.gps_locked is None else int(reading.gps_locked),
                int(bool(reading.poi_warning)),
                *motion,
                gps.status_raw, reading.warning_raw, reading.scan_raw,
            ),
        )

    def record_fix(self, fix: Any, *, monotonic_ns: int) -> None:
        """Store one GNSS fix.  Coordinates respect the opt-in."""
        lat, lon = (fix.lat, fix.lon) if self.record_coordinates else (None, None)
        self._offer(
            "INSERT INTO gnss_fixes (session_id, at, wall_ns, monotonic_ns, "
            "mode, lat, lon, alt_m, speed_mps, track_deg, epx_m, epy_m, "
            "satellites) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._session_id, _iso(fix.wall_ns), fix.wall_ns, monotonic_ns,
                fix.mode, lat, lon, fix.altitude_m, fix.speed_mps,
                fix.track_deg, fix.epx_m, fix.epy_m, fix.satellites,
            ),
        )

    # --------------------------------------------------------------- thread

    def _run(self, started_at: str, wall_ns: int, monotonic_ns: int,
             adapter: str) -> None:
        history = History(self.path)
        try:
            history.open()
            # Retention first, so the sweep never has to reason about the
            # session it is about to create.
            if self.retain_days:
                history.prune(self.retain_days)
            self._session_id = history.begin_session(
                started_at=started_at, wall_ns=wall_ns,
                monotonic_ns=monotonic_ns, adapter=adapter,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the loop
            self._open_error = type(exc).__name__
            self._started.set()
            history.close()
            return
        self._started.set()

        batch: list[_Write] = []
        last_commit = time.monotonic()
        stopping = False
        while not stopping:
            timeout = max(0.05, BATCH_SECONDS - (time.monotonic() - last_commit))
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = _SENTINEL_TICK
            if item is None:
                stopping = True
            elif item is not _SENTINEL_TICK:
                batch.append(item)

            due = (
                stopping
                or len(batch) >= BATCH_ROWS
                or (batch and time.monotonic() - last_commit >= BATCH_SECONDS)
            )
            if due and batch:
                try:
                    history.apply(batch)
                    self.written += len(batch)
                except Exception:  # noqa: BLE001 - a full card must not crash us
                    self.errors += len(batch)
                batch.clear()
                last_commit = time.monotonic()
            elif due:
                last_commit = time.monotonic()

        with contextlib.suppress(Exception):
            history.end_session(self._session_id, ended_at=_iso(time.time_ns()))
        history.close()


#: A private marker for "the queue timed out", distinct from ``None`` (stop)
#: and from a real write.
_SENTINEL_TICK: Final[object] = object()


def _iso(wall_ns: int) -> str:
    """Millisecond ISO-8601 UTC from a nanosecond wall clock reading."""
    from datetime import UTC, datetime  # noqa: PLC0415 - only needed here

    moment = datetime.fromtimestamp(wall_ns / 1e9, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def open_history(path: str | os.PathLike[str], *, read_only: bool = True) -> History:
    """Open an existing history for querying."""
    return History(path, read_only=read_only).open()
