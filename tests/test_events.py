"""The layer that decides whether a detection can be lost at all.

Most of this suite protects a convenience.  These tests protect the single
claim the project makes about itself: that an alert which began and ended
between two publications is still in the record afterwards.  That is the bug
:mod:`uniden_r8.events` was written for -- an alert at second 1, cleared at
second 3, published on a five-second timer, and simply gone -- so the test that
matters most in this file feeds a whole alert lifecycle in between two
hypothetical publishes and demands both ends of it back.

The module makes two promises of very different strength, and they are tested
to different standards on purpose.

**The queue is lossless, and that is checkable exactly.**  Every sequence
number handed out must be either present in what was drained or named by a gap
record, so the overflow tests assert that as a set equality over the drained
stream rather than by reading ``metrics.dropped``.  A counter is the wrong
witness for its own correctness: it can be wrong in the same direction as the
bug it is meant to reveal, and then the test agrees with the defect.

**Track identity is inference, and is tested as inference.**  These tests
assert the identity decisions the module names as its reason for existing --
that a source walking front to side to rear is one threat and not three, that a
reclassified band is not a new threat, that a frequency two bands away is --
and where a call is genuinely close they assert that it is *labelled* rather
than that it went a particular way.  Pinning the greedy winner would freeze an
implementation detail the module explicitly reserves the right to replace.

No radio, no broker, no network, and no real clock: the two clocks are injected
wherever the value of a timestamp is itself under test.  Every alert here is
built from wire-format slots and put through the real parser, so no test can
pass against an :class:`~uniden_r8.telemetry.Alert` shape the detector would
never send.
"""

from __future__ import annotations

import pytest

from uniden_r8 import events
from uniden_r8.events import (
    END,
    START,
    UPDATE,
    AlertKey,
    AlertTracker,
    Gap,
    Ingest,
    Notification,
    match_cost,
)
from uniden_r8.telemetry import Alert, parse_alerts

SECOND = 1_000_000_000

#: The wall clock is a long way from the monotonic one, which is the entire
#: point of carrying both: on a node with no battery-backed clock they are
#: unrelated numbers, and a test that let them coincide would not notice a
#: record built from one clock and stamped with the other.
WALL_EPOCH = 1_700_000_000 * SECOND

# A Ka reading and its small drift, a K reading, and a laser hit.  Frequencies
# are the ones upstream's own capture carries, used as shapes.
KA_FREQUENCY = "33.7850"
KA_DRIFTED = "33.7860"
KA_ELSEWHERE = "34.5000"
K_FREQUENCY = "24.1500"


#: One believable Ka slot, field by field.  A test names only what it is
#: changing, so the thing under test is the only thing that differs from the
#: line above it.
SLOT_FIELDS = {
    "band": "KA", "strength": 3, "signal": 33, "field_5": KA_FREQUENCY,
    "direction": "F", "mute": "1",
}


def slot(**overrides: object) -> str:
    """One alert slot in the detector's own comma-separated wire format.

    An unknown field name is refused rather than ignored: a typo that silently
    changed nothing would leave a test that still passes and no longer tests
    what its name says it does.
    """
    unknown = set(overrides) - set(SLOT_FIELDS)
    if unknown:
        raise TypeError(f"not a slot field: {sorted(unknown)}")
    fields = SLOT_FIELDS | overrides
    return ("1,00,{band},{strength},{signal},{field_5},"
            "{direction},{mute}").format(**fields)


def snapshot(*slots: str) -> list[Alert]:
    """Parse a full snapshot built from *slots*; no slots means "all clear".

    Everything goes through the real parser rather than constructing an
    :class:`Alert` directly.  A hand-built alert can carry a combination of
    fields the wire format cannot produce, and a tracker test that passes
    against one of those has proved nothing about the live path.
    """
    payload = "&".join(slots) if slots else "0"
    return parse_alerts(f"{payload}&0&0&0".encode())


def feed(tracker: AlertTracker, alerts: list[Alert], *, seq: int, second: int,
         **kwargs: object) -> list[events.AlertEvent]:
    """Fold one snapshot in at *second* on the monotonic clock."""
    return tracker.observe(
        alerts,
        seq=seq,
        monotonic_ns=second * SECOND,
        wall_ns=WALL_EPOCH + second * SECOND,
        **kwargs,
    )


def kinds(produced: list[events.AlertEvent]) -> list[str]:
    return [event.kind for event in produced]


class FakeClock:
    """Two clocks that advance together, a fixed distance apart.

    Injected in place of :func:`~uniden_r8.events.stamp` so that a record built
    from one instant on one clock and another instant on the other is visible
    as an arithmetic fact rather than a race.
    """

    def __init__(self, offset: int = WALL_EPOCH, step: int = SECOND) -> None:
        self.monotonic_ns = 0
        self.offset = offset
        self.step = step

    def __call__(self) -> tuple[int, int]:
        self.monotonic_ns += self.step
        return self.monotonic_ns, self.monotonic_ns + self.offset


# --------------------------------------------------------------- sequencing

def test_the_sequence_number_is_assigned_in_offer_not_by_the_consumer():
    """A number handed out after the queue could not prove what was lost."""
    queue = Ingest(maxsize=8)
    assert queue.sequence == 0
    first = queue.offer("alert", b"1,00,KA,3,33,33.7850,F,1&0&0&0")
    assert first.seq == 1
    assert queue.sequence == 1


def test_sequence_numbers_are_strictly_increasing_across_every_kind():
    """One numbering for the whole link, or a gap in it means nothing."""
    queue = Ingest(maxsize=64)
    seen = [queue.offer(kind, b"x").seq
            for kind in ("alert", "telemetry", "alert", "poi", "telemetry")]
    assert seen == [1, 2, 3, 4, 5]
    assert seen == sorted(set(seen)), "strictly increasing, never reused"


def test_the_record_returned_to_the_callback_is_the_one_that_was_enqueued():
    """The callback gets the current view without waiting for the consumer."""
    queue = Ingest(maxsize=8)
    returned = queue.offer("alert", b"payload")
    assert queue.drain() == [returned]


def test_the_payload_is_copied_so_a_reused_buffer_cannot_rewrite_history():
    """bleak hands the same bytearray back on the next notification.

    Keeping a reference to it would let packet 2 silently overwrite packet 1
    in the record, which is the loss this module exists to prevent, arriving
    by a different door.
    """
    buffer = bytearray(b"1,00,KA,3,33,33.7850,F,1")
    queue = Ingest(maxsize=8)
    note = queue.offer("alert", buffer)
    buffer[0:1] = b"0"
    assert note.payload.startswith(b"1,")
    assert isinstance(note.payload, bytes)


def test_a_notification_records_its_own_length():
    """A short packet is the signature of ATT truncation, so it is countable."""
    queue = Ingest(maxsize=8)
    assert queue.offer("alert", b"1234567").length == 7


def test_a_read_and_a_notify_stay_distinguishable():
    """A read is a snapshot of unknown age; a notify is an event."""
    queue = Ingest(maxsize=8)
    assert queue.offer("alert", b"x").source == "notify"
    assert queue.offer("alert", b"x", source="read").source == "read"


@pytest.mark.parametrize("maxsize", [1, 0, -1])
def test_a_queue_with_no_room_for_a_record_and_a_gap_is_refused(maxsize):
    """A queue that cannot hold the account of its own losses is not bounded,
    it is silent."""
    with pytest.raises(ValueError, match="record and a gap"):
        Ingest(maxsize=maxsize)


# ---------------------------------------------------------------- overflow

def test_the_latest_cell_is_updated_even_when_the_queue_is_overflowing():
    """The display must not go blind during exactly the burst that matters.

    ``latest`` is a single cell per kind and is written before any eviction
    decision, so a consumer that has fallen behind still shows the newest
    reading rather than the oldest surviving one.
    """
    queue = Ingest(maxsize=2)
    for index in range(50):
        queue.offer("alert", bytes([index]))
        queue.offer("telemetry", bytes([index]))
    assert queue.metrics.dropped > 0
    assert queue.latest["alert"].payload == bytes([49])
    assert queue.latest["telemetry"].payload == bytes([49])
    assert queue.latest["telemetry"].seq == queue.sequence


def test_overflow_drops_the_oldest_and_keeps_the_newest():
    """When a radar integration falls behind, the newest reading is the one
    worth having."""
    queue = Ingest(maxsize=4)
    for index in range(1, 7):
        queue.offer("alert", bytes([index]))
    notes = [record for record in queue.drain() if isinstance(record, Notification)]
    assert [note.seq for note in notes] == [4, 5, 6]


def test_a_drop_names_the_exact_sequence_numbers_it_lost():
    """A counter says "four were lost"; the record has to say which four."""
    queue = Ingest(maxsize=4)
    for index in range(1, 6):
        queue.offer("alert", bytes([index]))
    gap = queue.drain()[0]
    assert isinstance(gap, Gap)
    assert (gap.first_lost_seq, gap.last_lost_seq) == (1, 2)
    assert gap.count == 2


def test_consecutive_drops_merge_into_one_gap_instead_of_one_record_each():
    """A consumer two hundred packets behind needs one note saying so.

    Two hundred identical notes would themselves be the backlog, and they
    would crowd out the records that survived.
    """
    queue = Ingest(maxsize=4)
    for index in range(200):
        queue.offer("alert", bytes([index % 256]))
    drained = queue.drain()
    gaps = [record for record in drained if isinstance(record, Gap)]
    assert len(gaps) == 1
    assert queue.metrics.gaps == 1
    assert gaps[0].count == queue.metrics.dropped == 197


def test_the_gap_is_never_itself_dropped_under_sustained_overflow():
    """The smallest legal queue, overflowed a thousand times over.

    The gap holds a reserved slot outside the queue, so the account of the
    loss cannot be evicted by the flood that caused it.
    """
    queue = Ingest(maxsize=2)
    for index in range(1000):
        queue.offer("alert", bytes([index % 256]))
    drained = queue.drain()
    assert isinstance(drained[0], Gap)
    assert (drained[0].first_lost_seq, drained[0].last_lost_seq) == (1, 999)
    assert [record.seq for record in drained[1:]] == [1000]


def test_no_sequence_number_goes_unaccounted_for_however_bad_the_overflow():
    """The invariant the word "lossless" actually means here.

    Every number handed out is either a record that survived or a number a
    gap names.  Asserted as a set so that a miscount in either direction
    fails, rather than by reading the counter that would also be wrong.
    """
    queue = Ingest(maxsize=6)
    for index in range(40):
        queue.offer("alert", bytes([index]))
    accounted: set[int] = set()
    for record in queue.drain():
        if isinstance(record, Gap):
            accounted |= set(range(record.first_lost_seq, record.last_lost_seq + 1))
        else:
            accounted.add(record.seq)
    assert accounted == set(range(1, 41))


def test_the_gap_is_drained_ahead_of_the_records_that_outlived_it():
    """Everything a gap describes is older than everything still queued.

    Out of order, a consumer replaying the stream would apply a hole after the
    records that came later and mis-date it.
    """
    queue = Ingest(maxsize=4)
    for index in range(1, 8):
        queue.offer("alert", bytes([index]))
    drained = queue.drain()
    assert isinstance(drained[0], Gap)
    seqs = [record.seq for record in drained[1:]]
    assert seqs == sorted(seqs)
    assert drained[0].last_lost_seq + 1 == seqs[0]


def test_pop_returns_the_gap_before_any_notification_too():
    """``pop`` and ``drain`` must not disagree about the order of the stream."""
    queue = Ingest(maxsize=4)
    for index in range(1, 8):
        queue.offer("alert", bytes([index]))
    first = queue.pop()
    assert isinstance(first, Gap)
    rest = [queue.pop() for _ in range(3)]
    assert [record.seq for record in rest] == [5, 6, 7]
    assert queue.pop() is None


def test_both_the_drop_count_and_the_gap_count_are_published():
    """A silent drop is a lie, and one number alone cannot describe two facts:
    how much was lost, and how many separate holes it left."""
    queue = Ingest(maxsize=4)
    for index in range(1, 6):
        queue.offer("alert", bytes([index]))
    assert queue.metrics.dropped == 2
    assert queue.metrics.gaps == 1
    queue.drain()
    for index in range(1, 6):
        queue.offer("alert", bytes([index]))
    assert queue.metrics.dropped == 4
    assert queue.metrics.gaps == 2, "a fresh hole after a drain is a fresh gap"


def test_depth_and_high_water_describe_the_queue_including_its_gap():
    """The reserved slot is part of the budget, so it is part of the depth."""
    queue = Ingest(maxsize=8)
    for index in range(7):
        queue.offer("alert", bytes([index]))
    assert len(queue) == 7
    assert queue.metrics.depth == 7
    assert queue.metrics.dropped == 0

    queue.offer("alert", b"overflow")
    assert queue.metrics.dropped == 1
    assert len(queue) == 8 == queue.maxsize
    assert queue.metrics.depth == 8
    assert queue.metrics.high_water == 8

    queue.drain()
    assert len(queue) == 0
    assert queue.metrics.depth == 0
    assert queue.metrics.high_water == 8, "the peak is a record, not a gauge"


def test_metrics_publish_a_flat_readable_shape():
    queue = Ingest(maxsize=4)
    queue.offer("alert", b"x")
    assert queue.metrics.as_dict() == {
        "accepted": 1, "dropped": 0, "gaps": 0, "high_water": 1, "depth": 1,
    }


def test_a_gap_publishes_what_was_lost_and_when():
    gap = Gap(first_lost_seq=4, last_lost_seq=9, monotonic_ns=7, wall_ns=11)
    assert gap.as_dict() == {
        "kind": "gap", "first_lost_seq": 4, "last_lost_seq": 9, "count": 6,
        "monotonic_ns": 7, "wall_ns": 11,
    }


def test_an_empty_queue_drains_to_nothing_rather_than_raising():
    """This runs inside a consumer loop that must never die."""
    queue = Ingest(maxsize=4)
    assert queue.drain() == []
    assert queue.pop() is None
    assert len(queue) == 0


# ------------------------------------------------------------- both clocks

def test_a_notification_carries_one_instant_on_both_clocks(monkeypatch):
    """The pair is what lets a reader turn a monotonic duration into a window.

    Durations come from the monotonic clock because NTP cannot step it; the
    wall clock says *when*.  They are only usable together if both were read
    at the same instant.
    """
    monkeypatch.setattr(events, "stamp", FakeClock())
    queue = Ingest(maxsize=8)
    note = queue.offer("alert", b"x")
    assert note.wall_ns - note.monotonic_ns == WALL_EPOCH


def test_a_widened_gap_still_names_one_instant_on_both_clocks(monkeypatch):
    """A gap that grows must not end up half-stamped from two moments.

    The wall clock is the only one a database or a person can read, so a hole
    whose ``wall_ns`` came from the last loss and whose ``monotonic_ns`` came
    from the first is dated by however long the consumer was stalled -- which,
    on the backlog that caused the drop, is exactly when it is largest.
    """
    monkeypatch.setattr(events, "stamp", FakeClock())
    queue = Ingest(maxsize=2)
    for index in range(20):
        queue.offer("alert", bytes([index]))
    gap = queue.drain()[0]
    assert isinstance(gap, Gap)
    assert gap.wall_ns - gap.monotonic_ns == WALL_EPOCH


# ------------------------------------------------- the reason for the module

def test_a_short_alert_still_produces_a_start_and_an_end():
    """The bug the whole module exists to fix.

    A Ka hit that appears and clears inside one publication interval used to
    leave no trace anywhere: the state file, the display and everything
    downstream saw an unbroken clear.  Here the entire lifecycle happens
    between two hypothetical five-second publishes, and both transitions must
    still come out of the tracker.  This runs against a detector in a moving
    vehicle.
    """
    tracker = AlertTracker()
    publish_window = range(1, 5)  # four snapshots inside one publish interval

    produced: list[events.AlertEvent] = []
    for second in publish_window:
        alerts = snapshot(slot()) if second == 1 else snapshot()
        produced.extend(feed(tracker, alerts, seq=second, second=second))

    assert kinds(produced) == [START, END]
    assert produced[0].alert.band == "KA"
    assert produced[-1].correlation == "timeout"
    assert len(tracker) == 0, "nothing is left open when the threat is gone"


# ------------------------------------------------------------- correlation

def test_a_source_passing_front_to_side_to_rear_stays_one_track():
    """Direction is geometry, not identity.

    Approaching and passing one fixed source gives F, then S, then R.  Keying
    on direction manufactured an end and a start at the moment of closest
    approach -- the most interesting instant in the encounter.
    """
    tracker = AlertTracker()
    produced: list[events.AlertEvent] = []
    for second, direction in enumerate("FSR", start=1):
        produced.extend(
            feed(tracker, snapshot(slot(direction=direction)), seq=second, second=second)
        )

    assert kinds(produced) == [START, UPDATE, UPDATE]
    assert {event.track_id for event in produced} == {1}
    assert len(tracker) == 1
    assert tracker.open_tracks[0].directions == "FSR"


def test_a_patrol_car_overtaking_rear_to_side_to_front_stays_one_track():
    """The same walk in reverse: being overtaken, rather than passing."""
    tracker = AlertTracker()
    produced: list[events.AlertEvent] = []
    for second, direction in enumerate("RSF", start=1):
        produced.extend(
            feed(tracker, snapshot(slot(direction=direction)), seq=second, second=second)
        )

    assert kinds(produced) == [START, UPDATE, UPDATE]
    assert {event.track_id for event in produced} == {1}
    assert tracker.open_tracks[0].directions == "RSF"


def test_k_reclassified_as_k_pop_mid_encounter_stays_one_track():
    """K and K POP are re-readings of one signal, not two threats."""
    tracker = AlertTracker()
    first = feed(tracker, snapshot(slot(band="K", field_5=K_FREQUENCY)),
                 seq=1, second=1)
    second = feed(tracker, snapshot(slot(band="K POP", field_5=K_FREQUENCY)),
                  seq=2, second=2)

    assert kinds(first + second) == [START, UPDATE]
    assert second[0].track_id == first[0].track_id
    assert second[0].correlation == "matched"
    assert second[0].material is True, "a band change is a change a driver sees"
    assert tracker.open_tracks[0].key == AlertKey(family="K")


def test_a_ka_reading_drifting_within_tolerance_stays_one_track():
    """The detector reports to 0.1 MHz and its estimate wanders with signal."""
    tracker = AlertTracker()
    first = feed(tracker, snapshot(slot(field_5=KA_FREQUENCY)), seq=1, second=1)
    second = feed(tracker, snapshot(slot(field_5=KA_DRIFTED)), seq=2, second=2)

    assert kinds(first + second) == [START, UPDATE]
    assert second[0].track_id == first[0].track_id
    assert len(tracker) == 1
    track = tracker.open_tracks[0]
    assert track.min_frequency_ghz == 33.785
    assert track.max_frequency_ghz == 33.786


def test_a_ka_reading_most_of_a_gigahertz_away_is_a_different_threat():
    """Tolerating drift must not tolerate a wholly different source.

    Ka police radar covers 33.4 to 36.0 GHz; two readings that far apart are
    two transmitters, and merging them would hide one of them completely.
    """
    tracker = AlertTracker()
    first = feed(tracker, snapshot(slot(field_5=KA_FREQUENCY)), seq=1, second=1)
    second = feed(tracker, snapshot(slot(field_5=KA_ELSEWHERE)), seq=2, second=2)

    started = [event for event in second if event.kind == START]
    assert len(started) == 1
    assert started[0].track_id != first[0].track_id
    assert UPDATE not in kinds(second), "the drifting track did not absorb it"


def test_two_simultaneous_threats_produce_two_tracks_and_both_survive():
    """A Ka source and a K source in one snapshot are two encounters.

    Collapsing them would under-report a live threat, which is the failure
    mode a detector integration is least allowed to have.
    """
    both = [slot(field_5=KA_FREQUENCY), slot(band="K", field_5=K_FREQUENCY,
                                             direction="R")]
    tracker = AlertTracker()
    first = feed(tracker, snapshot(*both), seq=1, second=1)
    second = feed(tracker, snapshot(*both), seq=2, second=2)

    assert kinds(first) == [START, START]
    assert {event.track_id for event in first} == {1, 2}
    assert kinds(second) == []
    assert len(tracker) == 2
    assert {track.key.family for track in tracker} == {"KA", "K"}


def test_a_laser_alert_with_a_different_gun_id_is_a_different_track():
    """Field 5 on laser is a gun type, and two gun types are two encounters."""
    tracker = AlertTracker()
    first = feed(tracker, snapshot(slot(band="LASER", field_5="1", strength=8)),
                 seq=1, second=1)
    second = feed(tracker, snapshot(slot(band="LASER", field_5="5", strength=8)),
                  seq=2, second=2)

    started = [event for event in second if event.kind == START]
    assert len(started) == 1
    assert started[0].track_id != first[0].track_id
    assert AlertKey.of(first[0].alert) == AlertKey(family="LASER", laser_gun_id=1)
    assert AlertKey.of(started[0].alert) == AlertKey(family="LASER", laser_gun_id=5)


def test_a_match_two_open_tracks_fit_equally_well_is_labelled_ambiguous():
    """A close call is still decided, but it is never presented as clean.

    One reading exactly between two open Ka tracks could belong to either.
    Reporting "one threat" without saying it might be the other one is the
    kind of confident wrong answer this project refuses to give.
    """
    tracker = AlertTracker()
    apart = [slot(field_5="33.7800"), slot(field_5="33.8000")]
    started = feed(tracker, snapshot(*apart), seq=1, second=1)
    confirmed = feed(tracker, snapshot(*apart), seq=2, second=2)
    between = feed(tracker, snapshot(slot(field_5="33.7900")), seq=3, second=3)

    assert kinds(started) == [START, START]
    assert [event.correlation for event in confirmed] == []
    assert kinds(between) == [UPDATE]
    assert between[0].correlation == "ambiguous"
    matched = [track for track in tracker if track.track_id == between[0].track_id]
    assert matched[0].ambiguous is True


def test_an_ordinary_match_is_not_labelled_ambiguous():
    """The label has to mean something, so it must not be on everything."""
    tracker = AlertTracker()
    feed(tracker, snapshot(slot(field_5=KA_FREQUENCY)), seq=1, second=1)
    produced = feed(tracker, snapshot(slot(field_5=KA_DRIFTED)), seq=2, second=2)

    assert produced[0].correlation == "matched"
    assert tracker.open_tracks[0].ambiguous is False


# ---------------------------------------------------------- miss tolerance

def test_one_missed_snapshot_does_not_end_an_established_track():
    """A single dropped or truncated packet is not a threat going away."""
    tracker = AlertTracker()
    feed(tracker, snapshot(slot()), seq=1, second=1)
    feed(tracker, snapshot(slot()), seq=2, second=2)
    produced = feed(tracker, snapshot(), seq=3, second=3)

    assert produced == []
    assert len(tracker) == 1


def test_two_consecutive_missed_snapshots_end_the_track():
    """Tolerance is one snapshot, not an open-ended benefit of the doubt.

    A threat that never ends stays live in the history forever, which is worse
    than ending it a snapshot late.
    """
    tracker = AlertTracker()
    feed(tracker, snapshot(slot()), seq=1, second=1)
    feed(tracker, snapshot(slot()), seq=2, second=2)
    feed(tracker, snapshot(), seq=3, second=3)
    produced = feed(tracker, snapshot(), seq=4, second=4)

    assert kinds(produced) == [END]
    assert produced[0].correlation == "timeout"
    assert len(tracker) == 0


def test_a_track_seen_only_once_gets_the_same_miss_tolerance():
    """The snapshot that starts a track has plainly not missed it.

    The miss tolerance exists so one dropped packet cannot end a threat that
    never stopped, and a threat is at its newest -- and least confirmed -- in
    the snapshot it first appears in.  Charging a miss there gives a brand-new
    track no tolerance at all: it dies on the first absent snapshot, while an
    identical track that happened to be seen twice survives it.
    """
    tracker = AlertTracker()
    feed(tracker, snapshot(slot()), seq=1, second=1)
    assert tracker.open_tracks[0].misses == 0, "the snapshot that started it is not a miss"

    produced = feed(tracker, snapshot(), seq=2, second=2)
    assert produced == []
    assert len(tracker) == 1


def test_one_dropped_snapshot_does_not_split_one_threat_into_two():
    """The end-then-start pair the miss tolerance is there to prevent.

    One Ka source, present throughout, absent from a single snapshot because
    a packet was dropped.  Splitting it produces a spurious end, a spurious
    start and two half-length encounters in permanent history -- and the
    duration of neither is the duration of anything that happened.
    """
    tracker = AlertTracker()
    produced = feed(tracker, snapshot(slot()), seq=1, second=1)
    produced += feed(tracker, snapshot(), seq=2, second=2)
    produced += feed(tracker, snapshot(slot()), seq=3, second=3)

    assert kinds(produced) == [START]
    assert len(tracker) == 1
    assert tracker.open_tracks[0].track_id == 1


def test_hold_open_suppresses_the_end_pass_entirely():
    """Absence and failure are different facts.

    A slot that arrived and could not be decoded says nothing about whether
    the threat is still there, so treating it as absence would turn one bad
    byte into a complete fabricated alert lifecycle in permanent history.
    """
    tracker = AlertTracker()
    feed(tracker, snapshot(slot()), seq=1, second=1)
    feed(tracker, snapshot(slot()), seq=2, second=2)

    for second in range(3, 9):
        assert feed(tracker, snapshot(), seq=second, second=second,
                    hold_open=True) == []
    assert len(tracker) == 1
    assert tracker.open_tracks[0].misses == 0


def test_hold_open_still_starts_and_updates_the_tracks_it_can_read():
    """Holding the undecodable open must not blind the slots that decoded."""
    tracker = AlertTracker()
    started = feed(tracker, snapshot(slot()), seq=1, second=1, hold_open=True)
    updated = feed(tracker, snapshot(slot(strength=6)), seq=2, second=2,
                   hold_open=True)

    assert kinds(started + updated) == [START, UPDATE]
    assert updated[0].correlation == "matched"


# -------------------------------------------------------------- end stamps

def test_an_end_is_stamped_when_the_track_was_last_seen():
    """Not when the absence was noticed.

    Those differ by the miss tolerance, and stamping the end at the moment it
    was worked out would inflate every duration in the history by the same
    constant -- a bias no later analysis could remove, because the tolerance
    is not recorded with the event.
    """
    tracker = AlertTracker()
    feed(tracker, snapshot(slot()), seq=1, second=10)
    feed(tracker, snapshot(slot(strength=5)), seq=2, second=11)
    feed(tracker, snapshot(), seq=3, second=12)
    produced = feed(tracker, snapshot(), seq=4, second=13)

    ended = produced[0]
    assert ended.kind == END
    assert ended.monotonic_ns == 11 * SECOND, "the last snapshot it appeared in"
    assert ended.wall_ns == WALL_EPOCH + 11 * SECOND
    assert ended.duration_s == 1.0, "one second seen, not the three that elapsed"
    assert ended.seq == 4, "the snapshot that revealed the absence is still named"


def test_close_ends_every_open_track():
    """A dropped link, an OBD-blocked release and a clean shutdown all do this.

    Otherwise a threat that was live when the link died stays live in the
    history forever.
    """
    tracker = AlertTracker()
    feed(tracker, snapshot(slot(field_5=KA_FREQUENCY),
                           slot(band="K", field_5=K_FREQUENCY, direction="R")),
         seq=1, second=1)

    produced = tracker.close(seq=99, wall_ns=WALL_EPOCH + 5 * SECOND)

    assert kinds(produced) == [END, END]
    assert {event.correlation for event in produced} == {"closed"}
    assert {event.track_id for event in produced} == {1, 2}
    assert len(tracker) == 0
    assert tracker.close(seq=100, wall_ns=WALL_EPOCH) == []


def test_a_closed_track_is_still_dated_from_when_it_was_last_seen():
    """The link going away is not evidence the threat lasted until then."""
    tracker = AlertTracker()
    feed(tracker, snapshot(slot()), seq=1, second=4)
    ended = tracker.close(seq=9, wall_ns=WALL_EPOCH + 30 * SECOND)[0]

    assert ended.monotonic_ns == 4 * SECOND
    assert ended.duration_s == 0.0


# ------------------------------------------------------------ event shapes

def test_a_start_event_carries_no_aggregates():
    """"How strong did that get" has no answer yet, so it must not be given
    one: a zero or a first reading in that field would read as the peak."""
    tracker = AlertTracker()
    started = feed(tracker, snapshot(slot()), seq=1, second=1)[0]
    record = started.as_dict()

    assert set(record) == {
        "kind", "seq", "track_id", "monotonic_ns", "wall_ns",
        "correlation", "algorithm", "material", "alert",
    }
    for aggregate in ("duration_s", "samples", "max_strength", "max_raw_signal",
                      "directions"):
        assert aggregate not in record


def test_an_update_event_carries_no_aggregates_either():
    tracker = AlertTracker()
    feed(tracker, snapshot(slot()), seq=1, second=1)
    updated = feed(tracker, snapshot(slot(strength=6)), seq=2, second=2)[0]

    assert "max_strength" not in updated.as_dict()
    assert updated.samples is None
    assert updated.duration_s is None


def test_only_an_end_event_carries_the_whole_encounter():
    """An alert usually fades before it ends, so the last reading is not the
    peak; the aggregates are the only place the peak survives."""
    tracker = AlertTracker()
    feed(tracker, snapshot(slot(strength=2, signal=20)), seq=1, second=1)
    feed(tracker, snapshot(slot(strength=6, signal=99, direction="S")),
         seq=2, second=2)
    feed(tracker, snapshot(slot(strength=3, signal=10, direction="R")),
         seq=3, second=3)
    feed(tracker, snapshot(), seq=4, second=4)
    ended = feed(tracker, snapshot(), seq=5, second=5)[0].as_dict()

    assert ended["kind"] == END
    assert ended["max_strength"] == 6
    assert ended["max_raw_signal"] == 99
    assert ended["samples"] == 3
    assert ended["duration_s"] == 2.0
    assert ended["directions"] == "FSR"


def test_every_event_is_stamped_with_the_matcher_that_produced_it():
    """A history spanning an algorithm change has to stay readable.

    Without the stamp, output from two matchers averages silently into one
    number and every conclusion drawn from the archive is unfalsifiable.
    """
    tracker = AlertTracker()
    started = feed(tracker, snapshot(slot()), seq=1, second=1)[0]
    assert started.algorithm == events.TRACKING_ALGORITHM
    assert started.as_dict()["algorithm"] == "cost-greedy-2"


def test_an_event_carries_the_detailed_alert_not_the_reduced_one():
    """The history keeps everything; the sanitising layer reduces later."""
    tracker = AlertTracker()
    record = feed(tracker, snapshot(slot()), seq=1, second=1)[0].as_dict()

    assert record["alert"]["band"] == "KA"
    assert record["alert"]["raw_signal"] == 33
    assert record["alert"]["field_5_raw"] == KA_FREQUENCY
    assert record["alert"]["parsed"] is True


def test_an_event_cannot_be_mutated_under_another_consumer():
    """These reach the history, the state file, a broker and a feed."""
    tracker = AlertTracker()
    started = feed(tracker, snapshot(slot()), seq=1, second=1)[0]
    with pytest.raises(AttributeError):
        started.kind = END

    reduced = events.replace_alert(started, Alert(band="KA"))
    assert reduced.alert.band == "KA"
    assert reduced.alert is not started.alert
    assert started.alert.strength == 3, "the original is untouched"
    assert reduced.seq == started.seq


def test_the_three_event_kinds_are_the_only_ones():
    assert events.KINDS == (START, UPDATE, END)
    assert (START, UPDATE, END) == ("alert_start", "alert_update", "alert_end")


# ------------------------------------------------------- material changes

def test_a_change_only_in_the_raw_signal_produces_no_event_but_is_absorbed():
    """The raw signal moves constantly; publishing every twitch is noise.

    It is still folded into the track, because the peak raw signal is part of
    what the encounter was.
    """
    tracker = AlertTracker()
    feed(tracker, snapshot(slot(signal=33)), seq=1, second=1)
    produced = feed(tracker, snapshot(slot(signal=99)), seq=2, second=2)

    assert produced == []
    track = tracker.open_tracks[0]
    assert track.samples == 2
    assert track.max_raw_signal == 99


def test_a_frequency_drift_is_recorded_but_is_not_material():
    """A display may coalesce this; the history must not lose it."""
    tracker = AlertTracker()
    feed(tracker, snapshot(slot(field_5=KA_FREQUENCY)), seq=1, second=1)
    produced = feed(tracker, snapshot(slot(field_5=KA_DRIFTED)), seq=2, second=2)

    assert kinds(produced) == [UPDATE]
    assert produced[0].material is False


@pytest.mark.parametrize("changed", [
    {"strength": 6},
    {"direction": "R"},
    {"mute": "2"},
    {"band": "KA POP"},
])
def test_a_change_a_driver_would_care_about_is_material(changed):
    """Band, direction, strength and mute are what a person reacts to."""
    tracker = AlertTracker()
    feed(tracker, snapshot(slot()), seq=1, second=1)
    produced = feed(tracker, snapshot(slot(**changed)), seq=2, second=2)

    assert kinds(produced) == [UPDATE]
    assert produced[0].material is True


# ------------------------------------------------------------- match costs

def test_a_perfect_repeat_costs_nothing():
    alert = snapshot(slot())[0]
    assert match_cost(alert, alert) == 0.0


def test_match_cost_refuses_a_different_band_family():
    """K and Ka are different transmitters; no cost could make them one."""
    ka = snapshot(slot(field_5=KA_FREQUENCY))[0]
    k = snapshot(slot(band="K", field_5=K_FREQUENCY))[0]
    assert match_cost(ka, k) is None
    assert match_cost(k, ka) is None


def test_match_cost_refuses_an_impossible_strength_jump():
    """Bars move fast on a close approach; five in one snapshot is a new
    source, and merging it would hide the arrival of the stronger one."""
    weak = snapshot(slot(strength=1))[0]
    strong = snapshot(slot(strength=6))[0]
    assert match_cost(weak, strong) is None
    assert match_cost(strong, weak) is None


def test_the_largest_believable_strength_jump_is_still_a_match():
    """The refusal has to have an edge, and the edge has to be inside it."""
    weak = snapshot(slot(strength=1))[0]
    strong = snapshot(slot(strength=5))[0]
    assert match_cost(weak, strong) == pytest.approx(0.4)


def test_match_cost_refuses_a_frequency_outside_the_band_tolerance():
    near = snapshot(slot(field_5=KA_FREQUENCY))[0]
    far = snapshot(slot(field_5=KA_ELSEWHERE))[0]
    assert match_cost(near, far) is None


def test_match_cost_scales_frequency_distance_to_the_band():
    """One global tolerance would split Ka encounters or merge K sources.

    The same 5 MHz step sits well inside Ka's tolerance and outside K's
    altogether. The cost is that step as a fraction of the band's own
    tolerance, and asserting it that way keeps the test about the scaling law
    rather than about one tolerance's current value -- which matters now that
    Ka's has been re-measured against hardware (EVIDENCE 19.5).
    """
    assert events.BAND_TOLERANCE_GHZ["KA"] > events.BAND_TOLERANCE_GHZ["K"]
    step = 0.005
    ka = match_cost(snapshot(slot(field_5="33.7800"))[0],
                    snapshot(slot(field_5="33.7850"))[0])
    k = match_cost(snapshot(slot(band="K", field_5="24.1000"))[0],
                   snapshot(slot(band="K", field_5="24.1500"))[0])
    assert ka == pytest.approx(step / events.BAND_TOLERANCE_GHZ["KA"])
    assert k is None


def test_match_cost_refuses_a_laser_gun_swap():
    """The gun type is the one piece of identity laser actually offers."""
    first = snapshot(slot(band="LASER", field_5="1", strength=8))[0]
    second = snapshot(slot(band="LASER", field_5="5", strength=8))[0]
    assert match_cost(first, second) is None
    assert match_cost(first, first) == pytest.approx(0.5), "no frequency evidence"


def test_a_reclassified_band_costs_something_but_is_not_refused():
    """It is a weaker match than an identical reading, and still a match."""
    ka = snapshot(slot(band="KA", field_5=KA_FREQUENCY))[0]
    ka_pop = snapshot(slot(band="KA POP", field_5=KA_FREQUENCY))[0]
    cost = match_cost(ka, ka_pop)
    assert cost is not None
    assert cost == pytest.approx(0.5)


def test_a_step_along_the_direction_axis_is_cheaper_than_a_jump_across_it():
    """Passing a source walks through side; teleporting front to rear does not.

    Both are allowed, because a dropped packet can hide the middle step, but
    the ordinary one has to win when both are available.
    """
    front = snapshot(slot(direction="F"))[0]
    side = snapshot(slot(direction="S"))[0]
    rear = snapshot(slot(direction="R"))[0]
    step = match_cost(front, side)
    jump = match_cost(front, rear)
    assert step is not None and jump is not None
    assert step < jump < events.AMBIGUITY_MARGIN + jump


def test_band_family_folds_the_reclassifications_and_leaves_the_rest():
    assert events.band_family("K POP") == events.band_family("K") == "K"
    assert events.band_family("ka pop") == "KA"
    assert events.band_family("MRCT") == events.band_family("MRCD") == "PHOTO"
    assert events.band_family("RT4") == "RT"
    assert events.band_family("UNKNOWN") == "UNKNOWN", "never guessed at"


def test_the_key_holds_only_what_does_not_change():
    """Direction is geometry and frequency drifts, so neither is identity."""
    key = AlertKey.of(snapshot(slot(direction="R", field_5=KA_DRIFTED))[0])
    assert key == AlertKey(family="KA", laser_gun_id=None)
    assert key == AlertKey.of(snapshot(slot(direction="F",
                                            field_5=KA_FREQUENCY))[0])


def test_a_track_summarises_the_whole_encounter_not_its_last_reading():
    tracker = AlertTracker()
    feed(tracker, snapshot(slot(strength=2, signal=20, field_5="33.7800")),
         seq=1, second=1)
    feed(tracker, snapshot(slot(strength=6, signal=90, field_5="33.7850",
                                direction="S")), seq=2, second=2)
    summary = tracker.open_tracks[0].summary()

    assert summary["max_strength"] == 6
    assert summary["max_raw_signal"] == 90
    assert summary["min_frequency_ghz"] == 33.78
    assert summary["max_frequency_ghz"] == 33.785
    assert summary["directions"] == "FS"
    assert summary["samples"] == 2
    assert summary["duration_s"] == 1.0
    assert summary["ambiguous"] is False

# --------------------------------------- the measured Ka encounter

#: A verbatim 19-second excerpt from the first real Ka encounter this project
#: captured (EVIDENCE 19.5): one police source passing the vehicle, confirmed
#: from the detector's own screen by the driver. Milliseconds are relative to
#: the first packet. These payloads carry a band, a strength and a frequency
#: and no position of any kind.
#:
#: The detector's frequency estimate flips between 35.4780 and 35.4480 five
#: times across the pass -- 0.0300 GHz apart -- while strength and direction
#: move continuously, which is what a single source going past looks like.
KA_PASS = [
    (    0, "1,00,KA,4,92,35.4780,R,1&0&0&0"),
    (  488, "1,00,KA,4,91,35.4780,R,1&0&0&0"),
    (  975, "1,00,KA,4,86,35.4780,R,1&0&0&0"),
    ( 1481, "1,00,KA,4,89,35.4780,R,1&0&0&0"),
    ( 1988, "1,00,KA,4,87,35.4480,S,1&0&0&0"),
    ( 2475, "1,00,KA,4,87,35.4480,S,1&0&0&0"),
    ( 3000, "1,00,KA,4,97,35.4480,R,1&0&0&0"),
    ( 3488, "1,00,KA,5,104,35.4480,R,1&0&0&0"),
    ( 3994, "1,00,KA,5,106,35.4480,R,1&0&0&0"),
    ( 4481, "1,00,KA,5,97,35.4480,R,1&0&0&0"),
    ( 4988, "1,00,KA,5,103,35.4480,R,1&0&0&0"),
    ( 5494, "1,00,KA,5,107,35.4480,R,1&0&0&0"),
    ( 6000, "1,00,KA,5,93,35.4480,R,1&0&0&0"),
    ( 6488, "1,00,KA,5,93,35.4480,R,1&0&0&0"),
    ( 6994, "1,00,KA,5,93,35.4480,R,1&0&0&0"),
    ( 7500, "1,00,KA,5,95,35.4480,R,1&0&0&0"),
    ( 8007, "1,00,KA,4,77,35.4480,R,1&0&0&0"),
    ( 8494, "1,00,KA,3,77,35.4480,S,1&0&0&0"),
    ( 9000, "1,00,KA,3,78,35.4780,R,1&0&0&0"),
    ( 9524, "1,00,KA,3,78,35.4780,R,1&0&0&0"),
    ( 9994, "1,00,KA,3,69,35.4780,R,1&0&0&0"),
    (10500, "1,00,KA,3,62,35.4780,R,1&0&0&0"),
    (10988, "1,00,KA,3,62,35.4780,R,1&0&0&0"),
    (11512, "1,00,KA,3,70,35.4780,R,1&0&0&0"),
    (12000, "1,00,KA,3,75,35.4480,S,1&0&0&0"),
    (12509, "1,00,KA,3,81,35.4480,S,1&0&0&0"),
    (13013, "1,00,KA,3,81,35.4780,R,1&0&0&0"),
    (13501, "1,00,KA,3,81,35.4780,R,1&0&0&0"),
    (14007, "1,00,KA,3,81,35.4780,R,1&0&0&0"),
    (14513, "1,00,KA,3,76,35.4780,R,1&0&0&0"),
    (15001, "1,00,KA,3,64,35.4780,R,1&0&0&0"),
    (15507, "1,00,KA,3,64,35.4780,R,1&0&0&0"),
    (16013, "1,00,KA,3,64,35.4780,R,1&0&0&0"),
    (16519, "1,00,KA,3,69,35.4780,R,1&0&0&0"),
    (17007, "1,00,KA,3,69,35.4780,R,1&0&0&0"),
    (17513, "1,00,KA,4,85,35.4480,R,1&0&0&0"),
    (18001, "1,00,KA,4,85,35.4480,R,1&0&0&0"),
    (18526, "1,00,KA,4,90,35.4480,R,1&0&0&0"),
    (19032, "1,00,KA,4,95,35.4480,R,1&0&0&0"),
]


def _tracks(excerpt, tolerance):
    """Count tracks started when the excerpt is replayed at *tolerance*."""
    original = dict(events.BAND_TOLERANCE_GHZ)
    events.BAND_TOLERANCE_GHZ.update({"KA": tolerance, "KA POP": tolerance})
    try:
        tracker = events.AlertTracker()
        started = 0
        for seq, (offset_ms, payload) in enumerate(excerpt, start=1):
            at = offset_ms * 1_000_000
            for event in tracker.observe(
                parse_alerts(payload), seq=seq, monotonic_ns=at, wall_ns=at
            ):
                if event.kind == events.START:
                    started += 1
        return started
    finally:
        events.BAND_TOLERANCE_GHZ.clear()
        events.BAND_TOLERANCE_GHZ.update(original)


def test_one_ka_source_passing_produces_one_track():
    """The encounter that re-measured this tolerance, replayed.

    The driver saw one source. Anything above one track here is the matcher
    inventing threats out of its own frequency jitter -- which is what it did
    before EVIDENCE 19.5, and what a consumer counting alerts would have
    believed.
    """
    assert _tracks(KA_PASS, events.BAND_TOLERANCE_GHZ["KA"]) == 1


def test_the_old_ka_tolerance_split_that_single_source_six_ways():
    """The defect, kept executable rather than only described.

    0.025 GHz was inherited from upstream documentation and never measured. It
    is narrower than this detector's own frequency jitter, so every flip closed
    a track and opened another.
    """
    assert _tracks(KA_PASS, 0.025) == 6
    assert events.BAND_TOLERANCE_GHZ["KA"] > 0.030, (
        "the tolerance must exceed the jitter that was actually measured"
    )
