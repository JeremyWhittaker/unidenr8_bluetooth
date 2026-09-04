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

**Alert payloads are stored verbatim; telemetry payloads never are.**  That
asymmetry is deliberate and it is the one place this module keeps a raw packet.

The reason to keep them: correlating an alert across snapshots is *inference*,
and :mod:`uniden_r8.events` says so.  A derivation that is the only record makes
every future improvement to the matcher worthless, because there is nothing left
to re-run it against -- and this protocol is still being reverse-engineered, on
a detector that has never yet produced a real detection.  ``alert_snapshots``
is what makes "the snapshots are the record, the tracks are a view" a true
statement rather than an aspiration.

The reason telemetry payloads are excluded: the telemetry packet packs heading,
speed, altitude and POI detail into one byte string, so a payload column for it
would carry all of that past both opt-ins under a name that says nothing about
what is inside.  An *alert* packet carries band, strength, frequency, direction
and mute state -- and no position of any kind.  The privacy argument applies to
one format and not the other, so the schema follows the argument rather than
applying it uniformly and losing the thing worth keeping.

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
SCHEMA_VERSION: Final[int] = 3

#: Cap on the write queue.  Everything here is small; a backlog this deep means
#: the disk has stopped answering, and dropping is better than growing.
WRITE_QUEUE_MAX: Final[int] = 4096

#: Rows buffered before a commit, and the longest a buffered row waits.  Both
#: bounds exist: batching protects the SD card from a commit per packet, and
#: the time bound stops a quiet period from leaving the last alert unwritten.
BATCH_ROWS: Final[int] = 64
BATCH_SECONDS: Final[float] = 2.0

#: How long the caller waits for the writer thread to open the database before
#: giving up on it.  An SD card that has stopped answering must cost the
#: history, not the collector's start-up.
OPEN_TIMEOUT_SECONDS: Final[float] = 10.0

#: Retention measures against the timestamp at this rank from the newest, not
#: against the newest itself.  Ten is enough that a burst of rows written
#: during the minute between boot and the first NTP answer cannot define the
#: reference, and small enough to be exact on any database worth pruning.
ROBUST_RANK: Final[int] = 10

#: The largest share of a table a single wall-clock sweep may remove before it
#: refuses and records the refusal instead.
#:
#: The rank statistic above buys margin, not safety: eleven rows written under
#: a wrong clock are enough to move it, and a vehicle node with no network
#: writes eleven telemetry rows in under two minutes -- or eleven session rows
#: across six minutes of a systemd restart loop, having recorded nothing at
#: all.  This is the belt to that pair of braces, and unlike them it cannot be
#: defeated by any number of poisoned rows: a sweep that wants to take most of
#: the history is refused whatever its reference says.
PRUNE_MAX_FRACTION: Final[float] = 0.25

#: ...but only on a table with enough rows for a proportion to mean anything.
#: Removing one row of two is far past the limit and is also not the mass
#: deletion this guard exists to stop.  The floor is on the *table*, not on the
#: number of doomed rows: putting it on the latter would let a restart loop
#: take nine rows a sweep from a table of any size, which is the slow version
#: of the same failure.
PRUNE_MIN_TABLE_ROWS: Final[int] = 20

#: How often the writer thread sweeps retention after the first sweep at start.
#:
#: Retention used to run exactly once, when the writer opened the database.  A
#: collector started by hand for an hour was therefore swept every time, and a
#: collector installed as a service and left alone for a month was swept once --
#: which is precisely backwards, because only the second one accumulates.  At
#: one telemetry row a second a full day is around 86k rows, so a service that
#: survives a fortnight of driving would carry the whole fortnight regardless of
#: ``retain_days``, on an SD card.
#:
#: Six hours is short enough that no realistic drive outgrows its window and
#: long enough that the sweep is never in the way of a write.  It runs on the
#: writer thread, like every other statement here, so it is off the event loop.
PRUNE_INTERVAL_SECONDS: Final[float] = 6 * 60 * 60.0

#: Tables the retention sweep applies to, with the column that dates each row.
_PRUNABLE: Final[tuple[tuple[str, str], ...]] = (
    ("alert_events", "wall_ns"),
    ("alert_snapshots", "wall_ns"),
    ("telemetry", "wall_ns"),
    ("gnss_fixes", "wall_ns"),
    ("sessions", "started_wall_ns"),
)

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
        algorithm      TEXT,
        directions     TEXT,
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
    CREATE TABLE IF NOT EXISTS alert_snapshots (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   INTEGER NOT NULL,
        seq          INTEGER NOT NULL,
        at           TEXT    NOT NULL,
        wall_ns      INTEGER NOT NULL,
        monotonic_ns INTEGER NOT NULL,
        payload      TEXT    NOT NULL,
        slot_count   INTEGER,
        recognised   INTEGER,
        rejected     INTEGER
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
        poi_raw       TEXT,
        poi_suspect   INTEGER,
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
    "CREATE INDEX IF NOT EXISTS alert_snapshots_seq ON alert_snapshots (session_id, seq)",
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
            if self.read_only:
                # A read-only open must not rewrite the file, and this one did:
                # `_migrate` runs ALTER TABLE and updates the schema marker, so
                # `uniden-r8 history` -- documented as a query that touches
                # nothing -- silently upgraded any older database somebody
                # pointed it at.  Verified by opening a schema-2 file read-only
                # and finding schema 3 and two new columns afterwards.
                #
                # Refusing is the right failure. Migrating is a decision, and a
                # command whose job is to print rows should not make it on the
                # operator's behalf.
                raise HistoryError(
                    f"{self.path} is schema {row['value']}, this build reads "
                    f"{SCHEMA_VERSION}, and a read-only open will not migrate "
                    f"it.  Run the collector once against it to upgrade it "
                    f"deliberately, or copy it aside first."
                )
            if self._migrate(row["value"]):
                return
            raise HistoryError(
                f"{self.path} is schema {row['value']}, this build writes "
                f"{SCHEMA_VERSION}.  Move the old file aside rather than "
                f"mixing rows written under different meanings."
            )

    def _migrate(self, found: str) -> bool:
        """Apply an additive upgrade, or return ``False`` to refuse.

        Only ever *adds* columns, and only for the steps listed here.  Nothing
        is rewritten and nothing is dropped, so a row written under an older
        schema keeps exactly the meaning it had: the new columns read NULL,
        which is the truth -- that reading did not record them.

        This exists because a bare refusal is the wrong failure for a service.
        The refusal raises inside the writer thread, which records the error and
        returns; the collector then keeps running, keeps publishing a healthy
        state document, and silently writes nothing.  On a vehicle that means
        the first upgrade after an install quietly stops capturing drives, and
        nobody finds out until they go looking for a detection that should have
        been there.  A refusal is still correct for a change of *meaning*; this
        handles only the case where the old rows are still true.
        """
        steps = {
            # 2 -> 3: the POI warning's text and its coordinate tripwire.  The
            # text used to be discarded by the parser, so there was nothing to
            # store; it survives now, and a drive past a known camera is only
            # analysable afterwards if it was written down at the time.
            "2": [
                "ALTER TABLE telemetry ADD COLUMN poi_raw TEXT",
                "ALTER TABLE telemetry ADD COLUMN poi_suspect INTEGER",
            ],
        }
        if found not in steps:
            return False
        connection = self.connection
        connection.execute("BEGIN")
        try:
            for statement in steps[found]:
                connection.execute(statement)
            connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema'",
                (str(SCHEMA_VERSION),),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            return False
        return True

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

    def reference_wall_ns(self) -> int | None:
        """A timestamp to measure retention against, robust to a few bad rows.

        Not the maximum.  A Pi Zero 2 W has no battery-backed clock, so its
        wall clock is wrong at every cold boot and steps by hours when the
        network appears; a handful of rows written during that window can carry
        a timestamp years out.  Taking the maximum would let *one* such row
        define "now" and take the whole history with it.

        So this takes the :data:`ROBUST_RANK`-th newest timestamp instead.  A
        few outliers cannot move it, and on a database with fewer rows than
        that it degenerates to the newest -- which is fine, because there is
        very little there to lose.
        """
        rows = self.connection.execute(
            "SELECT wall_ns FROM ("
            "  SELECT wall_ns FROM alert_events"
            "  UNION ALL SELECT wall_ns FROM alert_snapshots"
            "  UNION ALL SELECT wall_ns FROM telemetry"
            "  UNION ALL SELECT wall_ns FROM gnss_fixes"
            "  UNION ALL SELECT started_wall_ns FROM sessions"
            ") ORDER BY wall_ns DESC LIMIT 1 OFFSET ?",
            (ROBUST_RANK,),
        ).fetchone()
        if rows is not None:
            return int(rows["wall_ns"])
        newest = self.connection.execute(
            "SELECT MAX(wall_ns) AS newest FROM ("
            "  SELECT wall_ns FROM alert_events"
            "  UNION ALL SELECT wall_ns FROM alert_snapshots"
            "  UNION ALL SELECT wall_ns FROM telemetry"
            "  UNION ALL SELECT wall_ns FROM gnss_fixes"
            "  UNION ALL SELECT started_wall_ns FROM sessions"
            ")"
        ).fetchone()
        value = newest["newest"] if newest else None
        return int(value) if isinstance(value, int) else None

    #: Kept as the old name because the tests and the docs both use it.
    def newest_wall_ns(self) -> int | None:
        """The single newest timestamp.  Reported, never used for retention."""
        row = self.connection.execute(
            "SELECT MAX(wall_ns) AS newest FROM ("
            "  SELECT wall_ns FROM alert_events"
            "  UNION ALL SELECT wall_ns FROM alert_snapshots"
            "  UNION ALL SELECT wall_ns FROM telemetry"
            "  UNION ALL SELECT wall_ns FROM gnss_fixes"
            "  UNION ALL SELECT started_wall_ns FROM sessions"
            ")"
        ).fetchone()
        value = row["newest"] if row else None
        return int(value) if isinstance(value, int) else None

    def prune(self, retain_days: int, max_rows: int = 0) -> int:
        """Bound the database.  Returns rows removed.

        Two mechanisms, and the order matters.

        **A row budget, by insertion order.**  ``max_rows`` keeps at most that
        many rows per table, deleting the lowest ``id`` first.  ``rowid`` is
        monotone and completely immune to the clock, so this is the mechanism
        that actually bounds an SD card -- and it keeps working on a node that
        never sees NTP, which is the normal state of a vehicle.

        **A retention window, by wall clock.**  ``retain_days`` is a
        best-effort secondary.  Zero disables it, which is a choice a person
        can make and not a default; the configuration says so out loud in
        :meth:`Config.warnings`.

        The wall-clock half is where the danger is, and it has three guards.

        The reference is neither "now" nor "the newest row".  A Pi Zero 2 W has
        no battery-backed clock, so its wall clock is wrong at every cold boot
        and steps by hours when the network appears; a sweep of the form
        "delete everything older than now minus thirty days" is a data-
        destruction trigger on hardware like that.  Using the newest *row* is
        no better, because rows written during that window carry the bad stamp.
        So the reference is ``min(now, the tenth-newest timestamp)``.

        That buys margin rather than safety -- eleven poisoned rows would move
        a rank statistic, and a drive with a wrong clock writes eleven
        telemetry rows in under two minutes.  So a sweep that would remove more
        than :data:`PRUNE_MAX_FRACTION` of a table is **refused** and recorded,
        whatever the reference says.  A retention sweep exists to bound disk
        usage; one that wants three quarters of the history is not doing that
        job, and the row budget above will bound the disk anyway.

        And the outcome is written down.  Every sweep records what it removed,
        what it refused and what reference it used, into ``meta`` -- because a
        deletion nobody can see is the half of this failure that no amount of
        arithmetic fixes.
        """
        removed = 0
        refused: list[str] = []
        connection = self.connection

        if max_rows > 0:
            connection.execute("BEGIN")
            try:
                for table, _column in _PRUNABLE:
                    cursor = connection.execute(
                        f"DELETE FROM {table} WHERE id <= ("  # noqa: S608
                        f"  SELECT id FROM {table} ORDER BY id DESC "
                        f"  LIMIT 1 OFFSET ?)",
                        (max_rows,),
                    )
                    removed += max(0, cursor.rowcount)
                connection.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    connection.execute("ROLLBACK")
                raise

        reference: int | None = None
        if retain_days > 0:
            reference = self.reference_wall_ns()
            now = time.time_ns()
            reference = now if reference is None else min(now, reference)
            cutoff_ns = reference - retain_days * 86_400 * 1_000_000_000

            connection.execute("BEGIN")
            try:
                for table, column in _PRUNABLE:
                    total = int(connection.execute(
                        f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608
                    ).fetchone()["n"])
                    if not total:
                        continue
                    doomed = int(connection.execute(
                        f"SELECT COUNT(*) AS n FROM {table} "  # noqa: S608
                        f"WHERE {column} < ?",
                        (cutoff_ns,),
                    ).fetchone()["n"])
                    if (
                        total >= PRUNE_MIN_TABLE_ROWS
                        and doomed > total * PRUNE_MAX_FRACTION
                    ):
                        refused.append(f"{table}:{doomed}/{total}")
                        continue
                    cursor = connection.execute(
                        f"DELETE FROM {table} WHERE {column} < ?",  # noqa: S608
                        (cutoff_ns,),
                    )
                    removed += max(0, cursor.rowcount)
                connection.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    connection.execute("ROLLBACK")
                raise

        self._record_prune(removed, refused, reference)
        return removed

    def _record_prune(
        self, removed: int, refused: list[str], reference: int | None
    ) -> None:
        """Write what the sweep did into ``meta``, so it can be seen."""
        entries = {
            "last_prune_at": _iso(time.time_ns()),
            "last_prune_removed": str(removed),
            "last_prune_refused": ",".join(refused),
            "last_prune_reference_ns": "" if reference is None else str(reference),
        }
        with contextlib.suppress(Exception):
            connection = self.connection
            connection.execute("BEGIN")
            for key, value in entries.items():
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            connection.execute("COMMIT")

    def _stored_schema(self) -> int | str:
        """The schema marker this file carries, or ``"?"`` if it has none.

        Returned as an ``int`` whenever it parses as one, so the published shape
        of ``stats()["schema"]`` is unchanged for every database this build can
        actually open.  A file carrying something unparseable reports the raw
        string rather than a guess.
        """
        try:
            row = self.connection.execute(
                "SELECT value FROM meta WHERE key = 'schema'"
            ).fetchone()
        except sqlite3.DatabaseError:
            return "?"
        if row is None:
            return "?"
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def last_prune(self) -> dict[str, Any]:
        """What the most recent sweep did.  Empty before the first one."""
        rows = self.connection.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'last_prune_%'"
        ).fetchall()
        record = {row["key"].removeprefix("last_prune_"): row["value"] for row in rows}
        if "removed" in record:
            record["removed"] = int(record["removed"] or 0)
        if "refused" in record:
            record["refused"] = [
                entry for entry in record["refused"].split(",") if entry
            ]
        return record

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
            for table in ("sessions", "alert_events", "alert_snapshots",
                      "telemetry", "gnss_fixes")
        }
        span = connection.execute(
            "SELECT MIN(at) AS first, MAX(at) AS last FROM alert_events"
        ).fetchone()
        return {
            "path": str(self.path),
            # The version recorded *in this file*, not the one this build
            # writes.  Printing the build's number told the operator what the
            # code believes rather than what the database says -- which is
            # exactly the wrong way round for a command that exists to report
            # on a file.
            "schema": self._stored_schema(),
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "counts": counts,
            "first_alert_at": span["first"],
            "last_alert_at": span["last"],
            "last_prune": self.last_prune(),
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

    def snapshots(self, limit: int = 200) -> list[dict[str, Any]]:
        """Raw alert notifications, newest first.  The re-derivable record."""
        rows = self.connection.execute(
            "SELECT * FROM alert_snapshots ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 10_000)),),
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

    def __init__(  # noqa: PLR0913 - keyword-only retention and opt-in knobs
        self,
        path: str | os.PathLike[str],
        *,
        retain_days: int = 30,
        record_motion: bool = False,
        record_coordinates: bool = False,
        record_alert_snapshots: bool = True,
        max_rows: int = 0,
    ) -> None:
        self.path = Path(path)
        self.retain_days = retain_days
        self.record_motion = record_motion
        self.record_coordinates = record_coordinates
        self.record_alert_snapshots = record_alert_snapshots
        self.max_rows = max_rows
        self.pruned = 0
        self.prune_refused: list[str] = []
        self._queue: queue.Queue[_Write | None] = queue.Queue(maxsize=WRITE_QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._session_id = 0
        self._started = threading.Event()
        self._stopping = threading.Event()
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
        # failure visible in `healthy` rather than as a hang.  A timeout is a
        # failure, not a success -- reporting one as healthy would have rows
        # written under a session id of zero and no way to notice.
        if not self._started.wait(timeout=OPEN_TIMEOUT_SECONDS):
            self._open_error = "TimeoutError"
        return not self._open_error

    def stop(self, timeout: float = 10.0) -> None:
        """Flush and close.  Safe to call more than once."""
        if self._thread is None:
            return
        # A flag as well as a sentinel.  A full queue would swallow the
        # sentinel, and the thread would never be told to stop -- so the flag
        # is what actually ends the loop and the sentinel only wakes it early.
        self._stopping.set()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def healthy(self) -> bool:
        """Open, and actually writing.

        Errors count, not just the open.  A writer that opened cleanly and has
        since failed every commit -- a card that filled up mid-drive -- is not
        healthy, and reporting it as such would put a green light on a history
        that has silently stopped recording.
        """
        return (
            self._thread is not None
            and not self._open_error
            and self.errors == 0
        )

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
            "pruned": self.pruned,
            "prune_refused": list(self.prune_refused),
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
            "correlation, algorithm, directions, material, duration_s, samples, "
            "max_strength, max_raw_signal, lat, lon, gnss_mode, gnss_speed_mps, "
            "gnss_track_deg) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._session_id, event.seq, event.kind, event.track_id,
                _iso(event.wall_ns), event.wall_ns, event.monotonic_ns,
                alert.band, alert.strength, alert.signal, alert.frequency_ghz,
                alert.laser_gun_id, alert.direction_name, alert.mute_code,
                alert.alert_id_raw, alert.receive_mode_raw,
                event.correlation, event.algorithm, event.directions,
                int(bool(event.material)),
                event.duration_s, event.samples, event.max_strength,
                event.max_raw_signal, lat, lon, mode, speed, track,
            ),
        )

    def record_alert_snapshot(self, snapshot: Any, note: Any) -> None:
        """Store one alert notification exactly as it arrived.

        The lossless layer.  Tracks are derived from these and can be re-derived
        from them; without this table, a better matcher written next year would
        have nothing to run against.  Alert packets arrive only on a detection
        change, so this is a handful of short rows per encounter rather than a
        stream.
        """
        if not self.record_alert_snapshots:
            return
        self._offer(
            "INSERT INTO alert_snapshots (session_id, seq, at, wall_ns, "
            "monotonic_ns, payload, slot_count, recognised, rejected) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self._session_id, note.seq, _iso(note.wall_ns), note.wall_ns,
                note.monotonic_ns,
                note.payload.decode("utf-8", "replace"),
                snapshot.slot_count, int(bool(snapshot.recognised)),
                snapshot.rejected_slots,
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
        poi = getattr(reading, "poi", None)
        # The POI text sits behind the same opt-in as heading, speed and
        # altitude, for the same reason: "SPEEDCAM,500,35" says which fixed
        # camera the vehicle is beside, which is a position by another route.
        # The tripwire flag is recorded either way -- it carries no content, and
        # a reading that was withheld must not look like one that was empty.
        poi_raw = poi.raw if (poi is not None and self.record_motion) else None
        poi_suspect = int(bool(poi.suspect_pair)) if poi is not None else None
        self._offer(
            "INSERT INTO telemetry (session_id, at, wall_ns, monotonic_ns, "
            "voltage, gps_locked, poi_active, poi_raw, poi_suspect, "
            "direction_8, speed_mph, altitude_ft, status_raw, warning_raw, "
            "scan_raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._session_id, _iso(wall_ns), wall_ns, monotonic_ns,
                reading.voltage,
                None if reading.gps_locked is None else int(reading.gps_locked),
                int(bool(reading.poi_warning)), poi_raw, poi_suspect,
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
            # The return value is kept, not discarded.  A sweep that removed
            # thousands of rows -- or one that has been refusing for a week --
            # must be visible in the published status, because a deletion
            # nobody can see is the half of this that arithmetic cannot fix.
            self.pruned = history.prune(self.retain_days, self.max_rows)
            self.prune_refused = history.last_prune().get("refused", [])
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
        last_prune = time.monotonic()
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
            # The flag is the backstop for a sentinel that could not be queued
            # because the queue was full.  It only ends the loop once the queue
            # has been drained, so a stop never costs the rows already in it.
            if self._stopping.is_set() and self._queue.empty():
                stopping = True

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

            # Sweep on a timer, not only at start.  Deliberately after the
            # commit and only between batches, so a sweep can never sit between
            # rows of the same batch, and guarded like every other statement on
            # this thread: a retention failure must degrade to "the database
            # grows", never to "the collector stopped recording".
            if not stopping and time.monotonic() - last_prune >= PRUNE_INTERVAL_SECONDS:
                last_prune = time.monotonic()
                try:
                    self.pruned += history.prune(self.retain_days, self.max_rows)
                    self.prune_refused = history.last_prune().get("refused", [])
                except Exception:  # noqa: BLE001 - reported via status, never raised
                    self.errors += 1

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
