"""The POI differ: does it answer the coordinate question without leaking one.

Two things are being proved here, and the second matters more than the first.

The first is that the tool works: given two captures that differ by one stored
user mark, it finds the record, says which candidate layout accounts for the
blob, and -- against a reference fix the operator supplies -- says how far away
the decoded point is.

The second is that it cannot be made to print a coordinate.  This module exists
to look at the most sensitive bytes the detector holds, so every path out of it
is checked against the value it was derived from.  A tool built to investigate
saved locations is the worst possible place for a leak, and "we were careful"
is not a control.
"""

from __future__ import annotations

import json
import struct

import pytest

from uniden_r8 import cli
from uniden_r8.evidence import PrivateStore
from uniden_r8.poi_diff import (
    PLAUSIBLE_RADIUS_M,
    decode_point,
    diff_payloads,
    haversine_metres,
)

#: A latitude and longitude used only inside this file, never printed.
#:
#: Built from components rather than written as a literal pair.  Other test
#: files in this repository do use a literal synthetic coordinate, and that is
#: fine -- but this is the one module whose whole purpose is handling real
#: saved positions, and its tests assert on what must *not* appear in output.
#: Deriving the value keeps those assertions honest: they compare against
#: something computed, not against a string that also sits three lines above.
#:
#: Chosen away from zero on both axes so a sign error cannot pass unnoticed, and
#: with enough fractional digits to survive the float32 round-trip meaningfully.
REFERENCE_LAT = 33.0 + 0.4484
REFERENCE_LON = -(112.0 + 0.0740)


def _near(step: float) -> tuple[float, float]:
    """A distinct point derived from the reference, never written as a pair.

    Every synthetic record in this file is built from one of these rather than
    from a latitude and longitude written side by side, so that a reader never
    has to judge whether a given pair in this particular file is real.
    """
    return REFERENCE_LAT + step, REFERENCE_LON - step


def _user_mark(latitude: float, longitude: float) -> bytes:
    """A type-03 record under the ``whole-record`` reading: 10 bytes."""
    return bytes([3, 0]) + struct.pack(">ff", latitude, longitude)


def _speed_camera(latitude: float, longitude: float) -> bytes:
    """A type-01 record under the same reading: 13 bytes."""
    return (
        bytes([1, 0])
        + struct.pack(">ff", latitude, longitude)
        + struct.pack(">H", 90)
        + bytes([65])
    )


def _padded_user_mark(latitude: float, longitude: float) -> bytes:
    """A type-03 record under the ``payload-plus-header`` reading: 12 bytes."""
    return _user_mark(latitude, longitude) + bytes([0, 0])


# ---------------------------------------------------------------- distances


def test_a_point_compared_with_itself_is_zero_metres_away():
    """The distance function's fixed point, which every other case leans on."""
    assert haversine_metres(
        REFERENCE_LAT, REFERENCE_LON, REFERENCE_LAT, REFERENCE_LON
    ) == pytest.approx(0.0, abs=1e-6)


def test_a_tenth_of_a_degree_of_latitude_is_about_eleven_kilometres():
    """A scale check: a wrong radius or a degrees/radians slip fails here."""
    distance = haversine_metres(
        REFERENCE_LAT, REFERENCE_LON, REFERENCE_LAT + 0.1, REFERENCE_LON
    )
    assert 11_000 < distance < 11_200


# ------------------------------------------------------------------ decoding


def test_a_well_formed_record_decodes_to_the_coordinate_it_was_built_from():
    """The floats are big-endian at offset 2; a byte-order slip fails here."""
    point = decode_point(_user_mark(REFERENCE_LAT, REFERENCE_LON))
    assert point is not None
    assert haversine_metres(*point, REFERENCE_LAT, REFERENCE_LON) < 1.0


def test_a_record_of_arbitrary_bytes_is_refused_rather_than_decoded():
    """A wrong record boundary makes floats out of somebody else's bytes.

    Refusing them is what stops the tool inventing a location, which is the one
    failure worse than returning nothing.
    """
    assert decode_point(b"\x03\x00" + b"\xff" * 8) is None


def test_a_zeroed_record_is_refused():
    """An empty or padded record decodes to Null Island, where nobody parked."""
    assert decode_point(bytes(10)) is None


def test_a_record_shorter_than_a_coordinate_is_refused():
    """A truncated blob must not be read past its end."""
    assert decode_point(bytes([3, 0, 1, 2])) is None


# --------------------------------------------------------------------- diff


def test_two_identical_captures_report_no_change():
    """Pressing nothing must not look like storing something."""
    blob = _user_mark(*_near(0.10)) + _user_mark(*_near(0.20))
    result = diff_payloads(blob, blob)
    assert result.changed is False
    assert result.records_added == []
    assert "no change" in result.render()


def test_one_appended_user_mark_is_found_and_measured_against_a_reference():
    """The whole experiment, end to end: read, mark, read, diff.

    If this fails, the user-mark experiment in docs/VALIDATION.md V8 cannot
    return a verdict, and the coordinate question stays open for want of a
    tool rather than for want of evidence.
    """
    before = _user_mark(*_near(0.10)) + _user_mark(*_near(0.20))
    after = before + _user_mark(REFERENCE_LAT, REFERENCE_LON)

    result = diff_payloads(
        before, after, reference=_near(0.0001)
    )

    assert result.changed is True
    assert result.appended_only is True
    assert result.added_bytes == 10
    assert len(result.records_added) == 1

    record = result.records_added[0]
    assert record.kind == "user mark"
    assert record.layout == "whole-record"
    assert record.offset == len(before)
    assert record.decodes is True
    assert record.metres_from_reference is not None
    assert record.metres_from_reference < PLAUSIBLE_RADIUS_M
    assert record.summary()["agrees_with_reference"] is True


def test_a_reference_far_from_the_record_is_reported_as_disagreeing():
    """A layout that decoded the wrong bytes must not read as a confirmation.

    Without this the tool would answer "yes" to the coordinate question for any
    blob at all, which is worse than answering nothing.
    """
    before = _user_mark(*_near(0.10))
    after = before + _user_mark(REFERENCE_LAT, REFERENCE_LON)

    result = diff_payloads(before, after, reference=_near(4.0))

    record = result.records_added[0]
    assert record.metres_from_reference > PLAUSIBLE_RADIUS_M
    assert record.summary()["agrees_with_reference"] is False
    assert "DOES NOT agree" in result.render()


def test_the_layout_that_consumes_the_blob_exactly_is_the_one_that_is_used():
    """Both readings of upstream's numbers are on file; the bytes decide.

    A blob written to one layout desynchronises under the other, so at most one
    should consume it exactly -- and only that one may be walked for records.
    """
    before = _user_mark(*_near(0.10))
    after = before + _speed_camera(REFERENCE_LAT, REFERENCE_LON)

    result = diff_payloads(after, after)
    exact = [entry for entry in result.layouts if entry["exact"]]

    assert [entry["layout"] for entry in exact] == ["whole-record"]
    other = next(e for e in result.layouts if e["layout"] == "payload-plus-header")
    assert other["exact"] is False


def test_the_other_layout_wins_when_the_blob_is_written_that_way():
    """The adjudication is symmetric, not a preference for one hypothesis."""
    blob = _padded_user_mark(*_near(0.10)) + _padded_user_mark(*_near(0.20))

    result = diff_payloads(blob, blob)
    exact = [entry["layout"] for entry in result.layouts if entry["exact"]]

    assert "payload-plus-header" in exact


def test_no_record_is_decoded_when_no_layout_fits():
    """Unknown boundaries must produce a null result, not a guessed one."""
    before = b"\x07\x07\x07\x07"
    after = before + b"\x07\x07"

    result = diff_payloads(before, after, reference=_near(0.0))

    assert result.records_added == []
    assert any("no candidate layout" in note for note in result.notes)


def test_a_change_that_is_not_an_append_is_flagged():
    """A reordered database must not be read as one appended record."""
    before = _user_mark(*_near(0.10)) + _user_mark(*_near(0.20))
    after = _user_mark(*_near(0.20)) + _user_mark(*_near(0.10))

    result = diff_payloads(before, after)

    assert result.appended_only is False
    assert any("not a clean append" in note for note in result.notes)


# ------------------------------------------------------------------ privacy


@pytest.mark.parametrize("reference", [None, _near(0.0)])
def test_no_rendering_of_a_diff_contains_the_coordinate_it_decoded(reference):
    """The control this module most needs, checked against the real value.

    Every branch -- text, JSON summary and the dataclass itself -- is searched
    for the digits of the latitude and longitude that were encoded into the
    record.  If any of them ever carries the value instead of a distance, this
    fails, which is the whole reason it is written against the number rather
    than against a pattern.
    """
    before = _user_mark(*_near(0.10))
    after = before + _user_mark(REFERENCE_LAT, REFERENCE_LON)

    result = diff_payloads(before, after, reference=reference)
    renderings = [
        result.render(),
        json.dumps(result.summary()),
        repr(result),
    ]

    decoded = decode_point(_user_mark(REFERENCE_LAT, REFERENCE_LON))
    assert decoded is not None
    forbidden = [
        f"{REFERENCE_LAT:.4f}", f"{abs(REFERENCE_LON):.4f}",
        # Also the float32 round-trip, which differs in the last places and
        # would otherwise slip past a comparison against the input.
        f"{decoded[0]:.4f}", f"{abs(decoded[1]):.4f}",
    ]
    for text in renderings:
        for value in forbidden:
            assert value not in text


def test_the_summary_carries_no_device_bytes():
    """Shape and counts are publishable; content is not."""
    before = _user_mark(*_near(0.10))
    after = before + _user_mark(REFERENCE_LAT, REFERENCE_LON)

    summary = diff_payloads(before, after).summary()
    text = json.dumps(summary)

    assert after.hex() not in text
    assert _user_mark(REFERENCE_LAT, REFERENCE_LON).hex() not in text


# ---------------------------------------------------------------------- cli


def _write_capture(store: PrivateStore, name: str, payload: bytes) -> None:
    store.write_json(name, {
        "attributes": [{
            "uuid": "15005991-b131-3396-014c-664c9867b917",
            "name": "POI", "sensitive": True, "hex": payload.hex(),
        }],
    })


def test_the_poi_diff_command_reads_two_captures_and_reports_a_distance(
    capsys, tmp_path
):
    """The command needs no radio, and reports the verdict the experiment wants."""
    store = PrivateStore(str(tmp_path / ".private")).ensure()
    before = _user_mark(*_near(0.10))
    after = before + _user_mark(REFERENCE_LAT, REFERENCE_LON)
    _write_capture(store, "poi-a.json", before)
    _write_capture(store, "poi-b.json", after)

    reference = tmp_path / "ref.json"
    reference.write_text(json.dumps({
        "lat": REFERENCE_LAT + 0.0001, "lon": REFERENCE_LON - 0.0001,
    }))

    code = cli.main([
        "--store", str(tmp_path / ".private"),
        "poi-diff", "poi-a.json", "poi-b.json",
        "--reference-file", str(reference),
    ])
    out = capsys.readouterr().out

    assert code == 0
    assert "user mark" in out
    assert "agrees" in out
    assert f"{REFERENCE_LAT:.4f}" not in out
    assert f"{abs(REFERENCE_LON):.4f}" not in out


def test_the_poi_diff_command_reports_a_missing_capture_rather_than_raising(
    capsys, tmp_path
):
    """An operator naming the wrong capture gets a message, not a traceback."""
    store = PrivateStore(str(tmp_path / ".private")).ensure()
    _write_capture(store, "poi-a.json", _user_mark(*_near(0.10)))

    code = cli.main([
        "--store", str(tmp_path / ".private"),
        "poi-diff", "poi-a.json", "absent.json",
    ])

    assert code == 1
    assert "cannot read capture" in capsys.readouterr().err


def test_the_poi_diff_command_refuses_a_capture_name_that_escapes_the_store(
    capsys, tmp_path
):
    """Capture names are single path components; the store enforces it."""
    PrivateStore(str(tmp_path / ".private")).ensure()

    code = cli.main([
        "--store", str(tmp_path / ".private"),
        "poi-diff", "../outside.json", "../outside.json",
    ])

    assert code == 1
    assert "cannot read capture" in capsys.readouterr().err


def test_a_broken_reference_file_names_no_value_in_its_error(capsys, tmp_path):
    """The reference file is the one place a coordinate may sit.

    An error message is not that place, so the failure is reported by exception
    type rather than by echoing what could not be parsed.
    """
    store = PrivateStore(str(tmp_path / ".private")).ensure()
    before = _user_mark(*_near(0.10))
    _write_capture(store, "poi-a.json", before)
    _write_capture(store, "poi-b.json", before + _user_mark(*_near(0.20)))

    reference = tmp_path / "ref.json"
    reference.write_text('{"lat": ' + f"{REFERENCE_LAT}" + ', "lon": ')  # truncated

    code = cli.main([
        "--store", str(tmp_path / ".private"),
        "poi-diff", "poi-a.json", "poi-b.json",
        "--reference-file", str(reference),
    ])
    err = capsys.readouterr().err

    assert code == 1
    assert "cannot read the reference fix" in err
    assert f"{REFERENCE_LAT:.4f}" not in err


def test_a_record_added_at_the_front_does_not_make_the_rest_look_added():
    """The returned set is reordered, so "added" must be decided by content.

    `docs/EVIDENCE.md` §13.12 measured the detector returning its POI set
    ordered nearest-first: a record created at the reading's own location sorts
    FIRST and pushes every existing record along. An offset-based test then
    reports all of them as new.

    This is not hypothetical. §17.3 records a position-based selection picking
    the wrong record and deleting it, on real hardware, because of exactly this.
    """
    a, b = _user_mark(*_near(0.30)), _user_mark(*_near(0.40))
    before = a + b
    after = _user_mark(*_near(0.0)) + a + b        # new record at the FRONT

    result = diff_payloads(before, after)

    assert result.appended_only is False, "the fixture must not be a clean append"
    assert len(result.records_added) == 1, (
        "only the genuinely new record is added; the two displaced ones are not"
    )
    assert result.records_added[0].offset == 0


def test_a_reordered_set_with_no_change_reports_nothing_added():
    """The same records in a different order are not new records.

    The set is recomputed as the vehicle moves (§13.10), so two reads of an
    unchanged database can come back in a different order. A tool that called
    that a change would fire on every read taken while driving.
    """
    a, b = _user_mark(*_near(0.30)), _user_mark(*_near(0.40))

    result = diff_payloads(a + b, b + a)

    assert result.changed is True, "the bytes did change"
    assert result.records_added == [], "but no record is new"
