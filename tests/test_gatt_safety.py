"""The read-only boundary.

These are the tests that matter.  Everything else in this suite protects a
convenience; these protect a live safety device in a moving vehicle.
"""

from __future__ import annotations

import pytest

from uniden_r8 import gatt
from uniden_r8.audit import TRANSMIT_ATTRS, audit_module, audit_package

# --------------------------------------------------------------- the gate

def test_command_write_characteristic_is_forbidden():
    assert gatt.COMMAND_WRITE_UUID in gatt.FORBIDDEN_UUIDS


def test_gate_refuses_reading_the_command_characteristic():
    with pytest.raises(gatt.WriteRefused):
        gatt.assert_readable(gatt.COMMAND_WRITE_UUID)


def test_gate_refuses_subscribing_to_the_command_characteristic():
    with pytest.raises(gatt.WriteRefused):
        gatt.assert_notifiable(gatt.COMMAND_WRITE_UUID)


@pytest.mark.parametrize("spelling", [
    gatt.COMMAND_WRITE_UUID.upper(),
    f"  {gatt.COMMAND_WRITE_UUID}  ",
    gatt.COMMAND_WRITE_UUID.title(),
])
def test_the_gate_cannot_be_bypassed_by_respelling_the_uuid(spelling):
    """Case and whitespace must not open the one forbidden characteristic."""
    with pytest.raises(gatt.WriteRefused):
        gatt.assert_readable(spelling)


def test_unknown_uuids_are_refused_not_guessed_at():
    with pytest.raises(gatt.UnknownCharacteristic):
        gatt.describe("0000dead-0000-1000-8000-00805f9b34fb")
    with pytest.raises(gatt.UnknownCharacteristic):
        gatt.assert_readable("0000dead-0000-1000-8000-00805f9b34fb")


def test_no_forbidden_characteristic_is_reachable_through_the_allowlists():
    assert not (gatt.READABLE_UUIDS & gatt.FORBIDDEN_UUIDS)
    assert not (gatt.NOTIFY_UUIDS & gatt.FORBIDDEN_UUIDS)


def test_every_probe_plan_step_passes_its_own_gate():
    """The plan and the gate must not be able to disagree."""
    for operation, uuid in gatt.PROBE_PLAN:
        if operation == "read":
            assert gatt.assert_readable(uuid) == uuid
        elif operation == "notify":
            assert gatt.assert_notifiable(uuid) == uuid
        else:  # pragma: no cover
            pytest.fail(f"probe plan contains a non-read-only operation: {operation}")


def test_the_probe_plan_contains_only_reads_and_subscriptions():
    assert {operation for operation, _ in gatt.PROBE_PLAN} == {"read", "notify"}


# ------------------------------------------------------- command refusal

@pytest.mark.parametrize("command", gatt.KNOWN_WRITE_COMMANDS)
def test_every_known_rtach_command_is_refused(command):
    with pytest.raises(gatt.WriteRefused):
        gatt.refuse_command(command)


def test_an_unknown_command_is_refused_too():
    """The refusal is unconditional, not a denylist that a new string escapes."""
    with pytest.raises(gatt.WriteRefused):
        gatt.refuse_command("BTreqSOMETHINGNEW:1")
    with pytest.raises(gatt.WriteRefused):
        gatt.refuse_command("")


def test_refusal_has_no_override_parameter():
    """A flag that enables writes would defeat the entire design."""
    import inspect

    signature = inspect.signature(gatt.refuse_command)
    assert list(signature.parameters) == ["command"]


def _every_module():
    """Import every module in the package.

    Enumerated rather than listed.  The previous version of this check named
    six modules by hand and had already fallen behind the package by four; a
    control with a hand-maintained list of what it covers is a control that
    silently stops covering the newest thing, which is always the thing least
    reviewed.
    """
    import importlib
    import pkgutil

    import uniden_r8

    modules = [uniden_r8]
    for info in pkgutil.iter_modules(uniden_r8.__path__):
        modules.append(importlib.import_module(f"uniden_r8.{info.name}"))
    return modules


def test_no_module_in_the_package_exposes_an_allow_writes_switch():
    """Upstream's write path is behind `allow_writes=True`.  There is no
    equivalent here, in any module, on any function or method."""
    import inspect

    modules = _every_module()
    assert len(modules) > 6, "enumeration is not finding the whole package"

    banned = {"allow_writes", "allow_write", "enable_writes", "unsafe_writes"}
    for module in modules:
        members = inspect.getmembers(module, inspect.isfunction)
        members += [
            (f"{cls_name}.{name}", method)
            for cls_name, cls in inspect.getmembers(module, inspect.isclass)
            if getattr(cls, "__module__", "") == module.__name__
            for name, method in inspect.getmembers(cls, inspect.isfunction)
        ]
        for name, member in members:
            if getattr(member, "__module__", "") != module.__name__:
                continue
            parameters = set(inspect.signature(member).parameters)
            offending = parameters & banned
            assert not offending, f"{module.__name__}.{name}: {offending}"


def test_the_allow_writes_check_would_catch_one():
    """A control that cannot fail proves nothing."""
    import inspect

    def tempting(allow_writes: bool = False) -> None:  # pragma: no cover
        pass

    assert "allow_writes" in inspect.signature(tempting).parameters


# ---------------------------------------------------- source-level audit

def test_the_package_contains_no_application_write_path():
    findings = audit_package()
    assert findings == [], f"transmitting API referenced: {[str(f) for f in findings]}"


def test_the_audit_actually_catches_a_write(tmp_path):
    """A control that cannot fail proves nothing, so prove it can."""
    module = tmp_path / "bad.py"
    module.write_text(
        "async def go(client):\n"
        "    await client.write_gatt_char('uuid', b'BTreqMUTE:1', response=False)\n"
    )
    findings = audit_module(module)
    assert [f.name for f in findings] == ["write_gatt_char"]
    assert findings[0].line == 2


def test_the_audit_catches_a_dynamic_write(tmp_path):
    module = tmp_path / "sneaky.py"
    module.write_text(
        "def go(client):\n"
        "    return getattr(client, 'write_gatt_char')\n"
    )
    assert [f.kind for f in audit_module(module)] == ["getattr"]


def test_the_audit_ignores_prose(tmp_path):
    """The safety modules discuss write_gatt_char; a grep would fail on them."""
    module = tmp_path / "prose.py"
    module.write_text(
        '"""This module never calls write_gatt_char."""\n'
        "# not even via write_gatt_char\n"
        "REFUSED_API = 'write_gatt_char'\n"
    )
    assert audit_module(module) == []


def test_start_notify_is_not_treated_as_an_application_write():
    """Subscribing is how you receive; flagging it would break the project.

    The CCCD write it performs is a real protocol descriptor write, and the
    docs say so.  It is excluded here because it cannot carry an application
    command, not because it is not a write.
    """
    assert "start_notify" not in TRANSMIT_ATTRS
    assert "write_gatt_descriptor" in TRANSMIT_ATTRS, (
        "the arbitrary-descriptor write must still be flagged"
    )


def test_the_docs_do_not_overclaim_radio_silence():
    """The invariant is 'no application write', not 'never transmits'.

    An earlier draft claimed the scan was passive and that the package could
    not transmit.  Both were false -- BlueZ scans actively by default, and
    connections, reads and subscriptions all exchange frames.  This pins the
    corrected language so the overclaim cannot quietly return.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    banned = re.compile(
        r"cannot transmit|never transmits?\b|passive (?:listen|scan|throughout)",
        re.IGNORECASE,
    )
    # A double-quoted span is the phrase being *cited* in order to be
    # disclaimed -- 'so "this project never transmits" would be false'. Those
    # are the corrections, not the overclaim, so they are stripped before the
    # search rather than allowlisted line by line.
    quoted = re.compile(r'"[^"]*"')

    offenders = []
    for path in [*root.glob("docs/*.md"), root / "README.md",
                 *(root / "src" / "uniden_r8").glob("*.py")]:
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if banned.search(quoted.sub("", line)):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "radio-silence overclaim returned:\n" + "\n".join(offenders)


def test_the_overclaim_detector_still_catches_a_real_overclaim(tmp_path):
    """The control must be able to fail, or it proves nothing."""
    import re

    banned = re.compile(
        r"cannot transmit|never transmits?\b|passive (?:listen|scan|throughout)",
        re.IGNORECASE,
    )
    quoted = re.compile(r'"[^"]*"')
    for bad in ("This package cannot transmit.",
                "One bounded, passive listen.",
                "It never transmits to the detector."):
        assert banned.search(quoted.sub("", bad)), bad


# --------------------------------------------------------- the catalogue

def test_identity_is_reachable_without_any_application_write():
    """The assignment's escalation clause turns on this fact.

    Firmware and software version are Bluetooth SIG Device Information
    characteristics.  Obtaining them is a GATT Read, not an application
    command written to the command characteristic, so the identity question is
    answerable entirely inside the read-only boundary.
    """
    for uuid in gatt.IDENTITY_READ_PLAN:
        assert gatt.assert_readable(uuid) == uuid
        assert gatt.describe(uuid).service == gatt.DEVICE_INFORMATION_SERVICE_UUID
        assert gatt.describe(uuid).evidence is gatt.Evidence.OFFICIAL
        assert not gatt.describe(uuid).device_accepts_writes


def test_catalogue_keeps_source_provenance_separate_from_runtime_evidence():
    """Hardware confirmation is recorded in the ledger, not by erasing origin."""
    assert not [c for c in gatt.CATALOGUE if c.evidence is gatt.Evidence.OBSERVED]


def test_vendor_characteristics_are_graded_as_r8w_evidence_not_fact():
    """Jeremy's detector is an R8; every vendor UUID here came from an R8w."""
    vendor = [c for c in gatt.CATALOGUE
              if c.service != gatt.DEVICE_INFORMATION_SERVICE_UUID]
    assert vendor
    for entry in vendor:
        assert entry.evidence in {
            gatt.Evidence.UPSTREAM,
            gatt.Evidence.UPSTREAM_UNVERIFIED,
        }, entry.name


def test_catalogue_uuids_are_unique_and_canonical():
    uuids = [c.uuid for c in gatt.CATALOGUE]
    assert len(uuids) == len(set(uuids))
    for uuid in uuids:
        assert uuid == gatt.normalize_uuid(uuid)
