"""The private store, and the gate between it and anything publishable."""

from __future__ import annotations

import os

import pytest

from fixtures import DOC_HOST_A, RANDOM_STATIC
from uniden_r8.evidence import (
    DIR_MODE,
    FILE_MODE,
    PrivateStore,
    PublicationRefused,
    publish,
    utc_stamp,
)

SAMPLE = RANDOM_STATIC


def test_store_is_created_owner_only_even_under_a_wide_umask(tmp_path):
    previous_umask = os.umask(0o000)
    try:
        store = PrivateStore(tmp_path / "private").ensure()
        store.write_text("raw.txt", f"device {SAMPLE}")
    finally:
        os.umask(previous_umask)

    assert store.root.stat().st_mode & 0o777 == DIR_MODE
    assert (store.root / "raw.txt").stat().st_mode & 0o777 == FILE_MODE
    assert store.is_sealed()


def test_an_existing_loose_store_is_tightened_not_trusted(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o755)
    loose = root / "leftover.txt"
    loose.write_text("raw")
    os.chmod(loose, 0o644)
    assert not PrivateStore(root).is_sealed()

    store = PrivateStore(root).ensure()
    assert store.root.stat().st_mode & 0o777 == DIR_MODE
    assert loose.stat().st_mode & 0o777 == FILE_MODE
    assert store.is_sealed()


def test_json_evidence_is_owner_only(tmp_path):
    store = PrivateStore(tmp_path / "private")
    path = store.write_json("scan.json", {"token": "ble:0123456789ab"})
    assert path.stat().st_mode & 0o777 == FILE_MODE
    assert store.is_sealed()


def test_the_salt_lives_inside_the_store_and_is_owner_only(tmp_path):
    store = PrivateStore(tmp_path / "private")
    assert len(store.salt) == 32
    assert (store.root / "redaction.salt").stat().st_mode & 0o777 == FILE_MODE
    assert store.salt == PrivateStore(tmp_path / "private").salt


def test_audit_reports_every_mode(tmp_path):
    store = PrivateStore(tmp_path / "private")
    store.write_text("a.txt", "x")
    store.write_text("b.txt", "y")
    assert dict(store.audit()) == {"a.txt": FILE_MODE, "b.txt": FILE_MODE}


@pytest.mark.parametrize("name", ["../escape.txt", "sub/dir.txt", "", "/abs.txt"])
def test_evidence_names_cannot_escape_the_store(name):
    with pytest.raises(ValueError):
        PrivateStore("/tmp/does-not-matter").path(name)


def test_publish_refuses_text_that_still_contains_an_address():
    with pytest.raises(PublicationRefused):
        publish(f"candidate at {SAMPLE}")
    with pytest.raises(PublicationRefused):
        publish(f"node reachable at {DOC_HOST_A}")


def test_publish_passes_sanitized_text_through_unchanged():
    text = "strong  R8W@nam:0123abcd  ble:0123456789ab  -61 dBm"
    assert publish(text) == text


def test_publish_refuses_rather_than_sanitizes():
    """Silently fixing the string would hide the bug that produced it."""
    with pytest.raises(PublicationRefused):
        publish(f"seen {SAMPLE}")


def test_utc_stamp_is_iso_utc():
    stamp = utc_stamp()
    assert stamp.endswith("Z") and "T" in stamp and len(stamp) == 20
