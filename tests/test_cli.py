"""The command line: what a reviewer and the operator actually run."""

from __future__ import annotations

import json

import pytest

from fixtures import RANDOM_STATIC
from uniden_r8 import cli, discovery, gatt

SAMPLE = RANDOM_STATIC
FRAGMENT = SAMPLE.replace(":", "")[-4:]


def test_plan_prints_every_permitted_operation(capsys):
    assert cli.main(["plan"]) == 0
    out = capsys.readouterr().out
    for _, uuid in gatt.PROBE_PLAN:
        assert gatt.describe(uuid).name in out


def test_plan_names_the_forbidden_characteristic_and_every_refused_command(capsys):
    assert cli.main(["plan"]) == 0
    out = capsys.readouterr().out
    assert gatt.COMMAND_WRITE_UUID in out
    for command in gatt.KNOWN_WRITE_COMMANDS:
        assert command in out


def test_plan_needs_no_radio_and_no_bleak():
    """It is what a reviewer runs before the project goes near the detector."""
    import sys

    assert "bleak" not in sys.modules
    assert cli.main(["plan"]) == 0
    assert "bleak" not in sys.modules


def test_selftest_passes_on_a_clean_tree(tmp_path, capsys):
    assert cli.main(["--store", str(tmp_path / "private"), "selftest"]) == 0
    assert "All read-only properties hold." in capsys.readouterr().out


def test_selftest_creates_a_sealed_store(tmp_path):
    from uniden_r8.evidence import PrivateStore

    store_path = tmp_path / "private"
    assert cli.main(["--store", str(store_path), "selftest"]) == 0
    assert PrivateStore(store_path).is_sealed()


def test_scan_output_is_sanitized(tmp_path, capsys, monkeypatch):
    from uniden_r8 import privacy

    class Device:
        address = SAMPLE
        name = f"R8W@{FRAGMENT}"
        rssi = -55

    async def fake(timeout):
        return [Device()]

    monkeypatch.setattr(discovery, "_discover", fake)
    assert cli.main(["--store", str(tmp_path / "private"), "scan", "-s", "3"]) == 0

    out = capsys.readouterr().out
    assert not privacy.looks_like_identifier(out)
    assert SAMPLE not in out
    assert FRAGMENT not in out
    assert "strong" in out


def test_scan_json_is_sanitized_and_parseable(tmp_path, capsys, monkeypatch):
    class Device:
        address = SAMPLE
        name = f"R8W@{FRAGMENT}"
        rssi = -55

    async def fake(timeout):
        return [Device()]

    monkeypatch.setattr(discovery, "_discover", fake)
    assert cli.main(["--store", str(tmp_path / "private"), "scan", "-s", "3", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["total_seen"] == 1
    assert payload["candidates"][0]["token"].startswith("ble:")
    assert "address" not in payload["candidates"][0]


def test_an_empty_scan_explains_itself(tmp_path, capsys, monkeypatch):
    """'Nothing found' is the expected result and must not read as a failure."""
    async def fake(timeout):
        return []

    monkeypatch.setattr(discovery, "_discover", fake)
    assert cli.main(["--store", str(tmp_path / "private"), "scan", "-s", "3"]) == 0
    out = capsys.readouterr().out
    assert "BT Pairing" in out
    assert "does not advertise" in out


def test_a_saved_report_lands_in_the_private_store_owner_only(tmp_path, monkeypatch):
    from uniden_r8.evidence import FILE_MODE

    async def fake(timeout):
        return []

    monkeypatch.setattr(discovery, "_discover", fake)
    store_path = tmp_path / "private"
    assert cli.main(
        ["--store", str(store_path), "scan", "-s", "3", "--save", "scan-01.json"]
    ) == 0

    saved = store_path / "scan-01.json"
    assert saved.exists()
    assert saved.stat().st_mode & 0o777 == FILE_MODE


def test_a_missing_bleak_is_reported_not_crashed(tmp_path, capsys, monkeypatch):
    async def fake(timeout):
        raise ImportError("No module named 'bleak'")

    monkeypatch.setattr(discovery, "_discover", fake)
    assert cli.main(["--store", str(tmp_path / "private"), "scan", "-s", "3"]) == 2
    assert "bleak is not installed" in capsys.readouterr().err


def test_the_cli_exposes_no_subcommand_that_transmits_to_the_detector():
    """`pair` changes BlueZ state and `identity` reads; neither transmits.

    The set is pinned so a new subcommand has to be considered here rather
    than appearing silently.
    """
    parser = cli.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions, "expected a subcommand action"
    assert set(actions[0].choices) == {"plan", "selftest", "scan", "pair", "identity"}


def test_pairing_refuses_to_act_without_explicit_confirmation(tmp_path, capsys):
    """Pairing is persistent BlueZ state; it must never happen by accident."""
    assert cli.main(["--store", str(tmp_path / "private"), "pair"]) == 2
    assert "persistent change" in capsys.readouterr().err


def test_an_over_long_scan_request_is_clamped_not_rejected(tmp_path, monkeypatch):
    seen: list[float] = []

    async def fake(timeout):
        seen.append(timeout)
        return []

    monkeypatch.setattr(discovery, "_discover", fake)
    assert cli.main(["--store", str(tmp_path / "private"), "scan", "-s", "9999"]) == 0
    assert seen == [discovery.MAX_SCAN_SECONDS]


@pytest.mark.parametrize("argv", [["scan", "-s", "3"], ["plan"], ["selftest"]])
def test_no_subcommand_can_transmit(argv, tmp_path, monkeypatch):
    """Whatever the CLI is asked to do, the package still has no write path."""
    from uniden_r8.audit import audit_package

    async def fake(timeout):
        return []

    monkeypatch.setattr(discovery, "_discover", fake)
    cli.main(["--store", str(tmp_path / "private"), *argv])
    assert audit_package() == []


# ------------------------------- correction 9: pairing postconditions are loud

class _Result:
    def __init__(self, paired=True, trusted=False, connected=False):
        self.paired = paired
        self.trusted = trusted
        self.connected = connected
        self.attempts = 1
        self.detail = ""
        self.transcript = []


def _pair_harness(monkeypatch, result, tmp_path):
    from uniden_r8 import cli as cli_module

    monkeypatch.setattr("uniden_r8.pairing.pair", lambda address, salt: result)
    monkeypatch.setattr(
        "uniden_r8.pairing.bonded_detector_address", lambda: SAMPLE
    )
    return cli_module.main(
        ["--store", str(tmp_path / "private"), "pair", "--confirm", "--use-bond"]
    )


def test_a_pair_left_trusted_fails_loudly(monkeypatch, tmp_path, capsys):
    """A trusted device auto-reconnects forever and competes for the radio."""
    code = _pair_harness(monkeypatch, _Result(trusted=True), tmp_path)
    err = capsys.readouterr().err
    assert code == 1
    assert "POSTCONDITION FAILED" in err
    assert "TRUSTED" in err


def test_a_pair_left_connected_fails_loudly(monkeypatch, tmp_path, capsys):
    """A held link makes the detector invisible to everything else."""
    code = _pair_harness(monkeypatch, _Result(connected=True), tmp_path)
    err = capsys.readouterr().err
    assert code == 1
    assert "CONNECTED" in err


def test_both_postcondition_violations_are_reported(monkeypatch, tmp_path, capsys):
    code = _pair_harness(monkeypatch, _Result(trusted=True, connected=True), tmp_path)
    err = capsys.readouterr().err
    assert code == 1
    assert "TRUSTED" in err and "CONNECTED" in err


def test_a_clean_pair_succeeds(monkeypatch, tmp_path, capsys):
    code = _pair_harness(monkeypatch, _Result(), tmp_path)
    assert code == 0
    assert "POSTCONDITION" not in capsys.readouterr().err
