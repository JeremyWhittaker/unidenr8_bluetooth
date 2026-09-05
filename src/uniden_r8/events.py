"""Lossless capture: timestamped notifications, and the alert events derived
from them.

This module exists because of one specific bug.  The first collector updated an
in-memory value inside the BLE notification callback and published it on a
five-second timer.  An alert that began at second 1 and cleared at second 3
therefore began and ended between two publications: the state file, the
display, and anything downstream saw an unbroken "clear".  A radar detector
integration that can silently lose a whole detection is not one.

The fix is a shape, not a faster timer.

1. The BLE callback does the least possible work -- copy the bytes, stamp two
   clocks, take a sequence number, hand it to a bounded queue, return.  Parsing,
   publishing and disk I/O all happen elsewhere, because anything slow on
   bleak's callback path delays the next notification and, on BlueZ, a client
   that will not drain its D-Bus socket gets disconnected rather than merely
   slowed.
2. A single consumer parses in arrival order and derives transitions.
3. Publication is driven by those transitions, with the periodic write demoted
   to a heartbeat.

Two layers, and only one of them is lossless
--------------------------------------------
This is the distinction that governs the whole design, and getting it wrong
would have made "lossless" a marketing word.

**The snapshot stream is the record.**  Every notification gets a sequence
number *in the callback*, before anything can go wrong with it.  A consumer
that sees a gap in that sequence knows precisely what it lost, without
trusting any counter, and :class:`Ingest` emits a :class:`Gap` record saying
so.  That layer is lossless in the real sense: what was received is what is
stored, and the numbering proves it.

**Track identity is inference.**  Deciding that the Ka reading in this snapshot
is the *same threat* as the Ka reading in the last one is a guess, because the
protocol offers nothing to correlate on -- field 1, the alert id, reads ``00``
in every capture anyone has published.  So :class:`AlertTracker` is a derived
*view*, it is stamped with :data:`TRACKING_ALGORITHM`, and the history stores
the snapshots it worked from.  A better matcher can be written later and run
against the same recorded stream; if the derivation were the only record, every
improvement would invalidate everything already collected.

Why the obvious key does not work
---------------------------------
The first version of this matched on band, direction and frequency together.
Each of those is unstable in exactly the situation that matters most:

* **Direction is geometry, not identity.**  Approaching and passing a fixed
  source gives F, then S, then R, for one source.  A patrol car overtaking gives
  the reverse.  Keying on direction manufactures an end and a start at the
  moment of closest approach -- the single most interesting instant in the
  encounter.
* **Frequency drifts.**  The detector reports to 0.1 MHz and its estimate
  wanders with signal strength, so equality-matching churns and even 1 MHz
  rounding is inside the drift for a weak Ka signal.
* **Band is not fixed either.**  K and K POP, Ka and Ka POP, MRCD and MRCT are
  reclassifications of one signal, not different threats.

So matching is a *cost*, not a key: :func:`match_cost` scores each open track
against each incoming slot on band family, frequency distance scaled to the
band, direction plausibility and strength continuity, and the assignment is
greedy over the sorted scores.  When two tracks score within
:data:`AMBIGUITY_MARGIN` of each other the result is flagged ``ambiguous``
rather than presented as a clean answer.

Two clocks, deliberately
------------------------
Every record carries ``monotonic_ns`` and ``wall_ns``.  Durations, staleness and
ordering come from the monotonic clock, which NTP cannot step; the wall clock is
only for saying *when* in a form a human or a database can read.  A Pi Zero 2 W
has no battery-backed clock, so its wall clock is wrong at every cold boot and
then jumps by hours when the network appears.  An alert duration computed across
that jump would be nonsense, and a retention sweep driven by it would be
dangerous.

Nothing here imports bleak, sqlite, or anything with a side effect.  The whole
event model is pure and testable without a radio.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Any, Final

from .telemetry import Alert

__all__ = [
    "TRACKING_ALGORITHM",
    "DEFAULT_QUEUE_SIZE",
    "BAND_TOLERANCE_GHZ",
    "TRACK_MISS_TOLERANCE",
    "AMBIGUITY_MARGIN",
    "START",
    "UPDATE",
    "END",
    "KINDS",
    "Notification",
    "Gap",
    "Ingest",
    "IngestMetrics",
    "AlertKey",
    "AlertTrack",
    "AlertEvent",
    "AlertTracker",
    "match_cost",
    "stamp",
]

#: Stamped on every derived event and stored with it.  Bump it whenever the
#: matching changes, so a history containing two algorithms' output can still
#: be read honestly instead of silently averaging them.
TRACKING_ALGORITHM: Final[str] = "cost-greedy-2"

#: Depth of the ingest queue.  Telemetry arrives at about 1 Hz and alerts a few
#: times a second, so this is many seconds of headroom for a consumer that is
#: momentarily busy -- and small enough that a wedged consumer cannot eat the
#: 415 MiB this node has.
DEFAULT_QUEUE_SIZE: Final[int] = 256

#: How far two frequency readings may differ and still be the same threat,
#: per band.  Ka police radar covers 33.4-36.0 GHz and wanders more than K
#: does, so one global tolerance would either split Ka encounters or merge
#: adjacent K sources.  Values are in GHz.
#:
#: **Ka is the only one of these set from a measurement.**  The first real Ka
#: encounter (EVIDENCE 19.5) had the detector reporting 35.4780 on 174 snapshots
#: and 35.4480 on 71, alternating within seconds while direction and strength
#: moved continuously -- one source passing the vehicle, which the driver
#: confirmed from the detector's own screen.  That is a spread of 0.0300 GHz,
#: and the old 0.025 split the pass into six tracks, several under a second.
#:
#: 0.050 is not the measured spread.  It is the measured spread with room, and
#: the room is affordable because the US Ka allocations police radar actually
#: uses -- 33.8, 34.7 and 35.5 GHz -- sit roughly 700-900 MHz apart.  A 50 MHz
#: window is an order of magnitude too narrow to bridge two of them, so buying
#: margin here costs nothing that could merge genuinely different sources.
#:
#: The other bands keep their inherited values: nothing has measured them, and
#: widening a tolerance on the strength of a different band's jitter would be
#: the same mistake in the other direction.
BAND_TOLERANCE_GHZ: Final[dict[str, float]] = {
    "X": 0.010,
    "K": 0.010,
    "K POP": 0.010,
    "KA": 0.050,
    "KA POP": 0.050,
}
_DEFAULT_TOLERANCE_GHZ: Final[float] = 0.020

#: Bands that are re-readings of one signal rather than different threats.  A
#: detector reclassifying K as K POP mid-encounter must not rename the track.
_BAND_FAMILIES: Final[dict[str, str]] = {
    "K": "K", "K POP": "K",
    "KA": "KA", "KA POP": "KA",
    "MRCD": "PHOTO", "MRCT": "PHOTO",
    "RT3": "RT", "RT4": "RT",
    "X": "X",
    "LASER": "LASER",
}

#: Direction as a position on a front-to-rear axis, for scoring plausibility.
_DIRECTION_AXIS: Final[dict[str, int]] = {"F": 0, "S": 1, "R": 2}

#: How many consecutive snapshots a track may be absent from before it ends.
#: Zero would be right if every snapshot were complete and reliable; it is not,
#: so one tolerated miss absorbs a single dropped or truncated packet without
#: inventing an end-then-start pair for a threat that never went away.
TRACK_MISS_TOLERANCE: Final[int] = 1

#: A match is ambiguous when the runner-up scores within this of the winner.
#: An ambiguous match is still made -- refusing to match would be worse -- but
#: it is labelled, so a count of "one threat" that might be two says so.
AMBIGUITY_MARGIN: Final[float] = 0.25

#: The largest strength jump between consecutive snapshots that is still
#: believable as one signal.  Bars move fast on a close approach; five bars in
#: one snapshot is a different source.
_MAX_STRENGTH_JUMP: Final[int] = 4

#: What an :class:`AlertEvent` can be.
START: Final[str] = "alert_start"
UPDATE: Final[str] = "alert_update"
END: Final[str] = "alert_end"
KINDS: Final[tuple[str, str, str]] = (START, UPDATE, END)


def stamp() -> tuple[int, int]:
    """Return ``(monotonic_ns, wall_ns)`` for right now.

    Called from the BLE callback, so it is two clock reads and nothing else.
    Both are captured at the same instant on purpose: the pair is what lets a
    reader convert a monotonic duration into a wall-clock window later.
    """
    return time.monotonic_ns(), time.time_ns()


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Notification:
    """One raw payload, exactly as it arrived, with the instant it arrived.

    Immutable and parse-free.  The callback that builds this decodes nothing:
    a parser bug on the notification path would take the whole subscription
    down with it, and a slow parser there delays the next packet.
    """

    seq: int
    kind: str
    payload: bytes
    monotonic_ns: int
    wall_ns: int
    #: ``"notify"`` for a subscription callback, ``"read"`` for the one-shot
    #: GATT read at the start of a session.  Kept because a read is a snapshot
    #: of unknown age while a notify is an event.
    source: str = "notify"

    @property
    def length(self) -> int:
        """Payload size.  Recorded because a short packet is the signature of
        ATT truncation, and a truncated packet must be diagnosable rather than
        silently half-parsed."""
        return len(self.payload)


@dataclass(frozen=True, slots=True)
class Gap:
    """A record of what the queue dropped, carried in the stream itself.

    A counter alone is not visibility.  A counter says "four were lost"; this
    says *which* four and over what span, so a hole in the history is a
    documented hole rather than an absence nobody can date.  :class:`Ingest`
    reserves capacity for these, so the record of a drop can never itself be
    dropped.
    """

    first_lost_seq: int
    last_lost_seq: int
    monotonic_ns: int
    wall_ns: int

    @property
    def count(self) -> int:
        return self.last_lost_seq - self.first_lost_seq + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "gap",
            "first_lost_seq": self.first_lost_seq,
            "last_lost_seq": self.last_lost_seq,
            "count": self.count,
            "monotonic_ns": self.monotonic_ns,
            "wall_ns": self.wall_ns,
        }


@dataclass
class IngestMetrics:
    """What the queue did.  Published, because a silent drop is a lie."""

    accepted: int = 0
    dropped: int = 0
    gaps: int = 0
    high_water: int = 0
    depth: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "accepted": self.accepted,
            "dropped": self.dropped,
            "gaps": self.gaps,
            "high_water": self.high_water,
            "depth": self.depth,
        }


class Ingest:
    """A bounded, ordered, sequence-numbered hand-off from callback to consumer.

    A plain ``asyncio.Queue`` would be the obvious choice and is not used, for
    two reasons.  The *policy on overflow* matters more than the await
    mechanics: when a radar integration falls behind, the newest snapshot is
    the one worth having, so a full queue drops the **oldest** record rather
    than blocking the callback or refusing the newest arrival.  And the
    sequence number has to be assigned on the producing side, before anything
    can be lost, or a gap becomes undetectable.

    Three places a record can be, deliberately, because one structure cannot
    serve three jobs:

    * :attr:`latest` is a single cell per kind, always overwritten and never
      dropped, so the *current view* survives any backlog;
    * the queue is the *record*, and it drops its oldest entries when it must;
    * and the gap holds its own reserved slot, outside the queue entirely, so
      the account of what was lost can never itself be lost.

    That last one is why this is not simply a ``deque`` with a ``maxlen``.  A
    gap record inside the queue competes for space with the records it is
    describing: under sustained overflow it gets evicted in turn, and either
    the loss becomes invisible or the code has to keep merging evicted gaps
    into new ones -- which also puts the gap *after* records that arrived after
    the loss, so the stream is no longer in order.  Keeping it separate makes
    both problems disappear: the gap always describes everything lost since the
    consumer last read, and it always comes first, because everything it
    describes is older than everything still queued.
    """

    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        if maxsize < 2:
            raise ValueError("the ingest queue needs room for a record and a gap")
        self._maxsize = maxsize
        # One slot of the budget belongs to the gap, permanently.
        self._items: deque[Notification] = deque(maxlen=maxsize - 1)
        self._seq = 0
        self._gap: Gap | None = None
        self.metrics = IngestMetrics()
        #: The most recent notification of each kind, never dropped.
        self.latest: dict[str, Notification] = {}

    def __len__(self) -> int:
        return len(self._items) + (1 if self._gap is not None else 0)

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @property
    def sequence(self) -> int:
        """The last sequence number handed out."""
        return self._seq

    def offer(self, kind: str, payload: bytes, *, source: str = "notify") -> Notification:
        """Stamp, number and enqueue one payload.  Never raises, never blocks.

        Called from bleak's notification path, where an exception vanishes into
        the BLE machinery and takes the subscription with it, and where any
        delay costs the next packet.  The returned record is the same one that
        was enqueued, so a caller that wants the current view does not have to
        wait for the consumer.
        """
        monotonic_ns, wall_ns = stamp()
        self._seq += 1
        note = Notification(
            seq=self._seq,
            kind=kind,
            payload=bytes(payload),
            monotonic_ns=monotonic_ns,
            wall_ns=wall_ns,
            source=source,
        )
        self.latest[kind] = note
        self.metrics.accepted += 1

        # Make room, remembering exactly what was lost.  The newest snapshot is
        # the one a radar integration needs, so the oldest goes.
        while len(self._items) >= self._items.maxlen:
            evicted = self._items.popleft()
            self.metrics.dropped += 1
            self._widen_gap(evicted.seq, monotonic_ns, wall_ns)

        self._items.append(note)
        self.metrics.depth = len(self)
        self.metrics.high_water = max(self.metrics.high_water, self.metrics.depth)
        return note

    def _widen_gap(self, seq: int, monotonic_ns: int, wall_ns: int) -> None:
        """Fold one lost sequence number into the pending gap.

        One record per contiguous run of losses, not one per loss: a consumer
        that fell behind by two hundred packets needs to know that, not to read
        two hundred identical notes about it.
        """
        if self._gap is None:
            self._gap = Gap(seq, seq, monotonic_ns, wall_ns)
            self.metrics.gaps += 1
            return
        # Both clocks stay at the moment the hole opened.  They are a pair
        # taken at one instant -- that is what makes a monotonic duration
        # convertible to a wall-clock window later -- and advancing one without
        # the other would leave a record whose two timestamps describe
        # different moments.
        self._gap = Gap(
            min(self._gap.first_lost_seq, seq),
            max(self._gap.last_lost_seq, seq),
            self._gap.monotonic_ns,
            self._gap.wall_ns,
        )

    def drain(self) -> list[Notification | Gap]:
        """Remove and return everything queued, in arrival order.

        The gap comes first when there is one: everything it describes is older
        than everything still in the queue.
        """
        drained: list[Notification | Gap] = []
        if self._gap is not None:
            drained.append(self._gap)
            self._gap = None
        drained.extend(self._items)
        self._items.clear()
        self.metrics.depth = 0
        return drained

    def pop(self) -> Notification | Gap | None:
        """Remove and return the oldest record, or ``None`` if empty.

        A pending gap is always returned before any notification, for the same
        reason :meth:`drain` orders it first.
        """
        if self._gap is not None:
            record: Notification | Gap = self._gap
            self._gap = None
        elif self._items:
            record = self._items.popleft()
        else:
            return None
        self.metrics.depth = len(self)
        return record


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def band_family(band: str) -> str:
    """Return the family a band belongs to, or the band itself if unknown."""
    return _BAND_FAMILIES.get(band.upper(), band.upper())


def _tolerance(band: str) -> float:
    return BAND_TOLERANCE_GHZ.get(band.upper(), _DEFAULT_TOLERANCE_GHZ)


def match_cost(track_alert: Alert, incoming: Alert) -> float | None:
    """Score *incoming* against a track's last reading; ``None`` means no match.

    Lower is better, zero is a perfect match.  The components are weighted so
    that a hard disagreement (a different band family, a frequency outside
    tolerance, an impossible strength jump) refuses outright, while soft
    disagreements (a plausible direction change, a small frequency drift) cost
    something and still allow a match when nothing better exists.
    """
    if band_family(track_alert.band) != band_family(incoming.band):
        return None
    if track_alert.laser_gun_id != incoming.laser_gun_id:
        return None

    cost = 0.0
    if track_alert.band != incoming.band:
        # Same family, different variant: a reclassification, not a new threat.
        cost += 0.5

    if track_alert.frequency_ghz is not None and incoming.frequency_ghz is not None:
        tolerance = _tolerance(incoming.band)
        delta = abs(track_alert.frequency_ghz - incoming.frequency_ghz)
        if delta > tolerance:
            return None
        cost += delta / tolerance
    else:
        # Laser, or a photo-radar type with no frequency.  Neither confirms nor
        # denies, so it costs a little rather than nothing: a match made
        # without frequency evidence is weaker than one made with it.
        cost += 0.5

    cost += _direction_cost(track_alert.direction, incoming.direction)

    if track_alert.strength is not None and incoming.strength is not None:
        jump = abs(track_alert.strength - incoming.strength)
        if jump > _MAX_STRENGTH_JUMP:
            return None
        cost += jump * 0.1
    return cost


def _direction_cost(before: str | None, after: str | None) -> float:
    """Score a bearing change.

    A source does not move between snapshots; the vehicle does.  Passing a
    fixed source walks F to S to R, and being overtaken walks R to S to F, so
    both directions of travel along the axis are ordinary.  Only the jump from
    one end to the other without the middle is suspicious, and even that is
    possible across a dropped packet -- so it costs, rather than refuses.
    """
    if before == after:
        return 0.0
    first = _DIRECTION_AXIS.get(before or "")
    second = _DIRECTION_AXIS.get(after or "")
    if first is None or second is None:
        return 0.5
    return 0.3 if abs(first - second) == 1 else 0.8


# --------------------------------------------------------------------------
# Alert tracks
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlertKey:
    """The stable part of a threat's identity.

    Only what genuinely does not change: the band *family* and, for laser, the
    gun type.  Direction is absent because it describes geometry, and frequency
    is absent because it drifts -- both are used for *matching*, in
    :func:`match_cost`, which is a different job from identity.
    """

    family: str
    laser_gun_id: int | None = None

    @classmethod
    def of(cls, alert: Alert) -> AlertKey:
        return cls(family=band_family(alert.band), laser_gun_id=alert.laser_gun_id)


@dataclass
class AlertTrack:
    """One threat, followed across the snapshots it appears in.

    The aggregates matter as much as the current values.  "How strong did that
    get" cannot be answered from the last snapshot, because an alert usually
    fades before it ends; the peaks are kept without keeping every packet.
    """

    track_id: int
    key: AlertKey
    alert: Alert
    first_monotonic_ns: int
    first_wall_ns: int
    last_monotonic_ns: int
    last_wall_ns: int
    samples: int = 1
    max_strength: int | None = None
    max_raw_signal: int | None = None
    min_frequency_ghz: float | None = None
    max_frequency_ghz: float | None = None
    directions: str = ""
    misses: int = 0
    ambiguous: bool = False

    def __post_init__(self) -> None:
        self.max_strength = self.alert.strength
        self.max_raw_signal = self.alert.signal
        self.min_frequency_ghz = self.alert.frequency_ghz
        self.max_frequency_ghz = self.alert.frequency_ghz
        self.directions = self.alert.direction or ""

    @property
    def duration_s(self) -> float:
        return (self.last_monotonic_ns - self.first_monotonic_ns) / 1e9

    def absorb(self, alert: Alert, monotonic_ns: int, wall_ns: int) -> bool:
        """Fold a new observation in.  Returns ``True`` if anything changed.

        "Changed" is judged on the whole publishable shape rather than a chosen
        subset: an update carrying no new information would be noise in the
        history, and one suppressed because the wrong field was checked would be
        a hole in it.
        """
        changed = alert.publishable() != self.alert.publishable()
        if alert.direction and not self.directions.endswith(alert.direction):
            self.directions += alert.direction
        self.alert = alert
        self.last_monotonic_ns = monotonic_ns
        self.last_wall_ns = wall_ns
        self.samples += 1
        self.misses = 0
        if alert.strength is not None:
            self.max_strength = (
                alert.strength if self.max_strength is None
                else max(self.max_strength, alert.strength)
            )
        if alert.signal is not None:
            self.max_raw_signal = (
                alert.signal if self.max_raw_signal is None
                else max(self.max_raw_signal, alert.signal)
            )
        if alert.frequency_ghz is not None:
            self.min_frequency_ghz = (
                alert.frequency_ghz if self.min_frequency_ghz is None
                else min(self.min_frequency_ghz, alert.frequency_ghz)
            )
            self.max_frequency_ghz = (
                alert.frequency_ghz if self.max_frequency_ghz is None
                else max(self.max_frequency_ghz, alert.frequency_ghz)
            )
        return changed

    def summary(self) -> dict[str, Any]:
        """The aggregate view: what this whole encounter looked like."""
        return {
            "track_id": self.track_id,
            "family": self.key.family,
            "band": self.alert.band,
            "directions": self.directions,
            "laser_gun_id": self.key.laser_gun_id,
            "samples": self.samples,
            "duration_s": round(self.duration_s, 3),
            "max_strength": self.max_strength,
            "max_raw_signal": self.max_raw_signal,
            "min_frequency_ghz": self.min_frequency_ghz,
            "max_frequency_ghz": self.max_frequency_ghz,
            "ambiguous": self.ambiguous,
        }


@dataclass(frozen=True)
class AlertEvent:
    """One transition, ready to be stored or published.

    Immutable because these reach more than one consumer -- history, the state
    file, a broker, a feed -- and a record one consumer can mutate under
    another is a debugging problem nobody needs.
    """

    kind: str
    seq: int
    track_id: int
    monotonic_ns: int
    wall_ns: int
    alert: Alert
    #: How this observation was matched.  ``"new"`` for a start, ``"matched"``
    #: for an ordinary continuation, ``"ambiguous"`` when a runner-up scored
    #: nearly as well, ``"timeout"`` when a track ended by absence, and
    #: ``"closed"`` when the link went away with it still open.
    correlation: str = "new"
    #: The matcher that produced this.  Stored so a history spanning an
    #: algorithm change can still be read honestly.
    algorithm: str = TRACKING_ALGORITHM
    #: Set on ``alert_end`` only, measured from the monotonic clock.
    duration_s: float | None = None
    samples: int | None = None
    max_strength: int | None = None
    max_raw_signal: int | None = None
    directions: str | None = None
    #: True when a field a driver would care about changed -- band, direction,
    #: strength or mute -- as opposed to only the fine-grained raw signal.  A
    #: display may coalesce the immaterial ones; the history must not.
    material: bool = True

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "kind": self.kind,
            "seq": self.seq,
            "track_id": self.track_id,
            "monotonic_ns": self.monotonic_ns,
            "wall_ns": self.wall_ns,
            "correlation": self.correlation,
            "algorithm": self.algorithm,
            "material": self.material,
            "alert": self.alert.detailed(),
        }
        for name, value in (
            ("duration_s", None if self.duration_s is None else round(self.duration_s, 3)),
            ("samples", self.samples),
            ("max_strength", self.max_strength),
            ("max_raw_signal", self.max_raw_signal),
            ("directions", self.directions),
        ):
            if value is not None:
                record[name] = value
        return record


def _material_change(before: Alert, after: Alert) -> bool:
    """True if a change matters to a person rather than only to a log."""
    return (
        before.band != after.band
        or before.direction != after.direction
        or before.strength != after.strength
        or before.mute_code != after.mute_code
    )


class AlertTracker:
    """Turns a stream of full snapshots into start/update/end events.

    One instance per link.  :meth:`observe` takes every parsed alert snapshot in
    arrival order and returns the transitions it caused; :meth:`close` ends
    everything still open, which a dropped link, an OBD-blocked release or a
    clean shutdown must all do -- otherwise a threat that was live when the link
    died stays live in the history forever.

    The assignment is greedy over :func:`match_cost`, sorted best-first.  A full
    optimal assignment would be more machinery than a handful of slots
    deserves, and the failure mode of a greedy mistake is a split or merged
    track in the history -- labelled ``ambiguous`` when it was close -- rather
    than a lost detection.  The snapshots themselves are kept, so a better
    matcher can be run over the same data later.
    """

    def __init__(self, *, miss_tolerance: int = TRACK_MISS_TOLERANCE) -> None:
        self.miss_tolerance = miss_tolerance
        self._tracks: dict[int, AlertTrack] = {}
        self._next_track_id = 1

    # ------------------------------------------------------------- inspect

    @property
    def open_tracks(self) -> list[AlertTrack]:
        return list(self._tracks.values())

    def __len__(self) -> int:
        return len(self._tracks)

    def __iter__(self) -> Iterator[AlertTrack]:
        return iter(self._tracks.values())

    # -------------------------------------------------------------- update

    def observe(
        self,
        alerts: Iterable[Alert],
        *,
        seq: int,
        monotonic_ns: int,
        wall_ns: int,
        hold_open: bool = False,
    ) -> list[AlertEvent]:
        """Fold one snapshot in and return the transitions it caused.

        *hold_open* suppresses the end pass.  It is set when the snapshot had a
        slot that arrived and could not be decoded: absence and failure are
        different facts, and treating a decode failure as "the threat stopped"
        would turn one bad byte into a complete fabricated alert lifecycle in
        permanent history.
        """
        incoming = list(alerts)
        events: list[AlertEvent] = []

        assignments, ambiguous = self._assign(incoming)
        # Seeded from the matches, then grown as new tracks are started: a
        # track this snapshot *created* has obviously not been absent from it.
        # Leaving it out was a real bug -- it charged every new track a miss on
        # its own first snapshot, which halved the miss tolerance and made a
        # single dropped packet split one threat into two.
        claimed = set(assignments.values())

        for index, alert in enumerate(incoming):
            track_id = assignments.get(index)
            if track_id is None:
                track = self._start(alert, monotonic_ns, wall_ns)
                claimed.add(track.track_id)
                events.append(
                    AlertEvent(
                        kind=START, seq=seq, track_id=track.track_id,
                        monotonic_ns=monotonic_ns, wall_ns=wall_ns,
                        alert=alert, correlation="new",
                    )
                )
                continue

            track = self._tracks[track_id]
            track.ambiguous = track.ambiguous or index in ambiguous
            before = track.alert
            if track.absorb(alert, monotonic_ns, wall_ns):
                events.append(
                    AlertEvent(
                        kind=UPDATE, seq=seq, track_id=track.track_id,
                        monotonic_ns=monotonic_ns, wall_ns=wall_ns,
                        alert=alert,
                        correlation="ambiguous" if index in ambiguous else "matched",
                        material=_material_change(before, alert),
                    )
                )

        # Anything this snapshot did not claim has gone quiet.  One miss is
        # tolerated so a single dropped packet does not end and restart a
        # threat that never actually stopped.
        if hold_open:
            return events
        for track in list(self._tracks.values()):
            if track.track_id in claimed:
                continue
            track.misses += 1
            if track.misses > self.miss_tolerance:
                events.append(self._end(track, seq, wall_ns))

        return events

    def close(
        self, *, seq: int, wall_ns: int, reason: str = "closed"
    ) -> list[AlertEvent]:
        """End every open track.  Called when the link goes away."""
        return [
            self._end(track, seq, wall_ns, reason=reason)
            for track in list(self._tracks.values())
        ]

    # ------------------------------------------------------------ internals

    def _assign(
        self, incoming: list[Alert]
    ) -> tuple[dict[int, int], set[int]]:
        """Greedily assign incoming slots to open tracks, best score first.

        Returns ``(slot index -> track id, indices whose match was close)``.
        """
        scored: list[tuple[float, int, int]] = []
        per_slot: dict[int, list[float]] = {}
        for index, alert in enumerate(incoming):
            for track in self._tracks.values():
                cost = match_cost(track.alert, alert)
                if cost is None:
                    continue
                scored.append((cost, index, track.track_id))
                per_slot.setdefault(index, []).append(cost)

        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        assignments: dict[int, int] = {}
        used: set[int] = set()
        for _cost, index, track_id in scored:
            if index in assignments or track_id in used:
                continue
            assignments[index] = track_id
            used.add(track_id)

        ambiguous = {
            index
            for index, costs in per_slot.items()
            if index in assignments
            and len(costs) > 1
            and sorted(costs)[1] - sorted(costs)[0] < AMBIGUITY_MARGIN
        }
        return assignments, ambiguous

    def _start(self, alert: Alert, monotonic_ns: int, wall_ns: int) -> AlertTrack:
        track = AlertTrack(
            track_id=self._next_track_id,
            key=AlertKey.of(alert),
            alert=alert,
            first_monotonic_ns=monotonic_ns,
            first_wall_ns=wall_ns,
            last_monotonic_ns=monotonic_ns,
            last_wall_ns=wall_ns,
        )
        self._next_track_id += 1
        self._tracks[track.track_id] = track
        return track

    def _end(
        self, track: AlertTrack, seq: int, wall_ns: int, reason: str = "timeout"
    ) -> AlertEvent:
        """End a track at the last moment it was actually seen.

        Not at the moment the absence was noticed.  Those differ by the miss
        tolerance, and stamping the end when we worked it out would inflate
        every duration in the history by the same constant.
        """
        self._tracks.pop(track.track_id, None)
        return AlertEvent(
            kind=END,
            seq=seq,
            track_id=track.track_id,
            monotonic_ns=track.last_monotonic_ns,
            wall_ns=track.last_wall_ns if reason == "timeout" else wall_ns,
            alert=track.alert,
            correlation="ambiguous" if track.ambiguous else reason,
            duration_s=track.duration_s,
            samples=track.samples,
            max_strength=track.max_strength,
            max_raw_signal=track.max_raw_signal,
            directions=track.directions,
        )


def replace_alert(event: AlertEvent, alert: Alert) -> AlertEvent:
    """Return *event* with a different alert attached.

    Used by the sanitising layer, which must be able to publish a reduced form
    of an alert without the event record itself being mutable.
    """
    return replace(event, alert=alert)
