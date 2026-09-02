"""Identifier redaction: nothing observable may reach a publishable string."""

from __future__ import annotations

import os
import re

import pytest

from fixtures import (
    DOC_HOST_A,
    DOC_HOST_B,
    RANDOM_STATIC,
    RANDOM_STATIC_ALT,
    bt_address,
)
from uniden_r8 import privacy

SALT = b"\x11" * 32
OTHER_SALT = b"\x22" * 32

# Built from octets rather than written out; see fixtures.py for why no test
# file may contain an address-shaped literal.
SAMPLE = RANDOM_STATIC


def test_token_is_stable_for_the_same_input_and_salt():
    assert privacy.token(SAMPLE, SALT) == privacy.token(SAMPLE, SALT)


def test_token_differs_across_installs():
    assert privacy.token(SAMPLE, SALT) != privacy.token(SAMPLE, OTHER_SALT)


@pytest.mark.parametrize("spelling", [
    RANDOM_STATIC,
    bt_address(0xE0, 0, 0, 0, 0x23, 0xD4, upper=False),
    bt_address(0xE0, 0, 0, 0, 0x23, 0xD4, separator="-"),
    bt_address(0xE0, 0, 0, 0, 0x23, 0xD4, separator="", upper=False),
])
def test_the_same_address_tokenises_identically_however_it_is_spelled(spelling):
    """BlueZ, bleak and bluetoothctl disagree on spelling; the report must not."""
    assert privacy.token(spelling, SALT) == privacy.token(SAMPLE, SALT)


def test_different_addresses_get_different_tokens():
    assert privacy.token(SAMPLE, SALT) != privacy.token(RANDOM_STATIC_ALT, SALT)


def test_a_token_contains_no_fragment_of_the_address():
    token = privacy.token(SAMPLE, SALT)
    assert not privacy.looks_like_identifier(token)
    assert "23d4" not in token.lower()
    assert "e000" not in token.lower()


def test_tokenising_without_a_salt_is_refused():
    """An unsalted 48-bit hash is enumerable, so this must never be optional."""
    with pytest.raises(ValueError):
        privacy.token(SAMPLE, b"")


def test_redact_name_keeps_the_model_and_tokenises_the_fragment():
    fragment = SAMPLE.replace(":", "")[-4:]
    redacted = privacy.redact_name(f"R8W@{fragment}", SALT)
    assert redacted.startswith("R8W@")
    assert fragment not in redacted
    assert not privacy.looks_like_identifier(redacted)


def test_redact_name_handles_an_absent_name():
    assert privacy.redact_name(None, SALT) == "<unnamed>"
    assert privacy.redact_name("   ", SALT) == "<unnamed>"


def test_redact_name_tokenises_a_free_form_name_whole():
    """A phone or headset name can carry a person's name; keep none of it."""
    redacted = privacy.redact_name("Jeremy's iPhone 15 Pro", SALT)
    assert "Jeremy" not in redacted
    assert redacted.startswith("nam:")


def test_scrub_removes_addresses_from_free_text():
    text = f"Failed to connect to {SAMPLE}: org.bluez.Error.AuthenticationFailed"
    scrubbed = privacy.scrub(text, SALT)
    assert SAMPLE not in scrubbed
    assert "AuthenticationFailed" in scrubbed
    assert not privacy.looks_like_identifier(scrubbed)


def test_scrub_removes_host_addresses():
    scrubbed = privacy.scrub(f"ssh to {DOC_HOST_A} over {DOC_HOST_B}", SALT)
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", scrubbed)
    assert scrubbed.count("<host-redacted>") == 2


def test_scrub_maps_repeated_addresses_to_one_token():
    """A transcript mentioning one device twice must not look like two."""
    scrubbed = privacy.scrub(f"{SAMPLE} then {SAMPLE.lower()}", SALT)
    tokens = re.findall(r"ble:[0-9a-f]+", scrubbed)
    assert len(tokens) == 2 and len(set(tokens)) == 1


def test_looks_like_identifier_finds_what_scrub_removes():
    assert privacy.looks_like_identifier(f"device {SAMPLE}")
    assert privacy.looks_like_identifier(f"host {DOC_HOST_A}")
    assert not privacy.looks_like_identifier("device ble:0123456789ab")


def test_salt_file_is_created_owner_only(tmp_path):
    salt_path = tmp_path / "nested" / "redaction.salt"
    previous_umask = os.umask(0o000)  # the worst case a caller could hand us
    try:
        salt = privacy.load_or_create_salt(salt_path)
    finally:
        os.umask(previous_umask)

    assert len(salt) == privacy.SALT_BYTES
    assert salt_path.stat().st_mode & 0o777 == 0o600
    assert salt_path.parent.stat().st_mode & 0o777 == 0o700


def test_salt_is_reused_across_calls(tmp_path):
    path = tmp_path / "redaction.salt"
    assert privacy.load_or_create_salt(path) == privacy.load_or_create_salt(path)


def test_a_loose_existing_salt_file_is_tightened(tmp_path):
    path = tmp_path / "redaction.salt"
    path.write_bytes(b"\x33" * privacy.SALT_BYTES)
    os.chmod(path, 0o644)
    privacy.load_or_create_salt(path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_truncated_salt_file_is_replaced_not_used(tmp_path):
    """A short salt is corruption; using it would silently weaken every token."""
    path = tmp_path / "redaction.salt"
    path.write_bytes(b"\x44" * 4)
    salt = privacy.load_or_create_salt(path)
    assert len(salt) == privacy.SALT_BYTES
