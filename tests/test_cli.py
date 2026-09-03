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
    assert "not established" in out
    assert "re-arm" in out


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
    """No subcommand writes an application characteristic.

    `pair` changes BlueZ bond state; `identity`, `live` and `collect` read, and
    the latter two also write a CCCD to subscribe. None of them writes a value
    to a vendor characteristic, which is the invariant.

    `survey` connects and enumerates, but reads no characteristic *value* --
    not one -- so it cannot return a saved coordinate and needs no `--confirm`.
    Its `--listen` subscribes to the command-response characteristic and sends
    nothing.

    `history`, `config` and `poi-diff` touch no radio at all -- `poi-diff`
    compares two captures that already exist on disk, which is the whole reason
    the coordinate experiment needs no write path.

    The set is pinned so a new subcommand has to be justified here rather than
    appearing silently.
    """
    parser = cli.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions, "expected a subcommand action"
    assert set(actions[0].choices) == {
        "plan", "selftest", "scan", "pair", "identity", "live", "collect",
        "inspect", "history", "config", "poi-diff", "survey",
    }


def test_inspect_refuses_without_explicit_confirmation(capsys):
    """It reads the POI database; that is a decision, not a default.

    Checked at the command rather than only in `inspection.inspect()`, because
    the point is that nothing reaches the radio: a refusal that happened after
    connecting would already have opened the link.
    """
    assert cli.main(["inspect"]) == 2
    message = capsys.readouterr().err
    assert "--confirm" in message
    assert "POI" in message


def test_the_offline_commands_need_no_radio_and_no_detector(capsys, tmp_path):
    """`history` and `config` must work on a machine with no Bluetooth.

    The "no database yet" case is pointed at an explicit, empty tmp_path
    rather than the default `.state/history.db`: that default resolves
    against the process's cwd, so leaving it implicit would pass only on a
    machine where nobody had ever run the tool from the repo root.
    """
    assert cli.main(["--config", str(tmp_path / "absent.toml"), "config"]) == 2
    assert cli.main(["config", "--example"]) == 0
    assert "[collector]" in capsys.readouterr().out

    written = tmp_path / "offline.toml"
    written.write_text(
        f'[history]\npath = "{tmp_path / "history.db"}"\n', encoding="utf-8"
    )
    assert cli.main(["--config", str(written), "history"]) == 1


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_collect_refuses_a_nonpositive_or_nonfinite_duration(value):
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(["collect", "--duration", value])
    assert caught.value.code == 2


def test_no_subcommand_can_reach_an_application_write(tmp_path):
    """Whatever the CLI grows, the AST audit must still come back empty."""
    from uniden_r8.audit import audit_package

    assert audit_package() == []


def test_live_json_is_sanitized_and_parseable(tmp_path, capsys, monkeypatch):
    from uniden_r8 import telemetry

    async def fake_receive(*_args, **_kwargs):
        return telemetry.LiveSession(
            started_at="2026-09-02T00:00:00Z",
            seconds=5.0,
            connected=True,
            compatible=True,
        )

    monkeypatch.setattr("uniden_r8.pairing.bonded_detector_address", lambda: SAMPLE)
    monkeypatch.setattr(telemetry, "receive", fake_receive)
    code = cli.main(
        ["--store", str(tmp_path / "private"), "live", "--seconds", "5", "--json"]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["connected"] is True
    assert SAMPLE not in captured.out + captured.err


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


# ------------------------------------------------- configuration plumbing

def test_config_example_works_even_when_the_config_file_is_broken(capsys, tmp_path):
    """The one command whose job is to print a known-good file.

    A file with a typo would otherwise block exactly what somebody reaches for
    when their file has a typo.
    """
    broken = tmp_path / "broken.toml"
    broken.write_text("[not-a-section]\nx = 1\n", encoding="utf-8")

    assert cli.main(["--config", str(broken), "config", "--example"]) == 0
    assert "[collector]" in capsys.readouterr().out
    # And the same broken file is still an error for anything that uses it.
    assert cli.main(["--config", str(broken), "config"]) == 2


def test_state_dir_moves_the_history_with_it(tmp_path, monkeypatch):
    """A trial must not append to production history.

    `--state-dir` overrides where the state documents go; a relative
    `history.path` resolves against the state directory, so if the override were
    used alongside the settings instead of folded into them, a bounded trial
    would quietly write into the configured database.
    """
    from uniden_r8 import cli as cli_module

    seen: dict = {}

    async def fake_run(_address, state_dir, **kwargs):
        seen["state_dir"] = str(state_dir)
        seen["history_path"] = str(kwargs["config"].history_path)
        return 0

    monkeypatch.setattr("uniden_r8.pairing.bonded_detector_address", lambda: SAMPLE)
    monkeypatch.setattr("uniden_r8.collector.run", fake_run)

    configured = tmp_path / "configured"
    trial = tmp_path / "trial"
    settings = cli_module.load_config(None)
    from dataclasses import replace

    settings = replace(
        settings,
        collector=replace(settings.collector, state_dir=str(configured)),
    )
    assert cli_module._cmd_collect(str(trial), 1.0, settings) == 0

    assert seen["state_dir"] == str(trial)
    assert seen["history_path"].startswith(str(trial)), (
        "the history followed the configured directory, not the trial one"
    )


def test_history_omits_recorded_coordinates_unless_asked(tmp_path, capsys):
    """Omitting a column is a better answer than failing.

    The publication gate refuses a document containing a position, and it is
    right to — but this is the owner querying their own history on their own
    terminal, so the default drops the columns and `--full` is the explicit ask.
    """
    from uniden_r8 import storage

    path = tmp_path / "history.db"
    with storage.History(path) as history:
        session = history.begin_session(
            started_at="2026-09-02T00:00:00.000Z", wall_ns=1, monotonic_ns=1,
        )
        history.connection.execute(
            "INSERT INTO alert_events (session_id, seq, kind, track_id, at, "
            "wall_ns, monotonic_ns, band, lat, lon) "
            "VALUES (?,1,'alert_end',1,'2026-09-02T00:00:01.000Z',2,2,'KA',?,?)",
            (session, 33.4484, -112.0740),
        )

    written = tmp_path / "c.toml"
    written.write_text(
        f'[history]\nenabled = true\npath = "{path}"\n', encoding="utf-8"
    )

    assert cli.main(["--config", str(written), "history", "events"]) == 0
    plain = capsys.readouterr().out
    assert "33.4484" not in plain
    assert "lat" not in plain.splitlines()[0].split()

    assert cli.main(["--config", str(written), "history", "events", "--full"]) == 0
    full = capsys.readouterr().out
    assert "33.4484" in full

    # And the JSON form must not end in an uncaught refusal either way.
    assert cli.main(["--config", str(written), "history", "events", "--json"]) == 0
    assert cli.main(
        ["--config", str(written), "history", "events", "--json", "--full"]
    ) == 0
