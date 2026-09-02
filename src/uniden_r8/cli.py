"""Command line entry points.

The command surface is deliberately small:

``plan``
    Print the read-only probe plan and the refusal list.  No hardware, no
    network.  This is what a reviewer runs to see what the project would do
    before it is allowed anywhere near the detector.
``selftest``
    Prove the safety properties on the machine that is about to run the scan:
    that the gate refuses the command characteristic, that every known R/Tach
    command is rejected, that the package contains no write path, and that the
    private store is sealed.  It needs no Bluetooth stack, so it is also the
    honest answer to "is the deployment healthy" on a node where ``bleak`` has
    not been installed yet.
``scan``
    One bounded advertisement-only discovery window.  Prints a sanitized
    report.  Active scanning, as BlueZ does by default; no connection.
``pair``
    One explicitly confirmed, guarded BlueZ bond operation.
``identity``
    One bounded connection reading only standard Device Information values.
``live``
    One bounded connection reading and subscribing only to telemetry/alerts.
``collect``
    The long-running collector: hold the link, publish state, feed the
    optional history, GNSS, MQTT and local-feed sinks.
``inspect``
    One explicitly confirmed read of the settings blocks and the POI database,
    written only into the private store.  The only command that deliberately
    touches the detector's saved coordinates.
``history``
    Query the local SQLite history.  No radio at all.
``config``
    Print the effective configuration, or an example file.  No radio.

Every radio operation except ``collect`` is bounded.  Nothing here retries
forever except the collector, which is what it is for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .audit import audit_package
from .config import ConfigError, example_toml, load_config
from .config import describe as describe_config
from .discovery import (
    DEFAULT_SCAN_SECONDS,
    MAX_SCAN_SECONDS,
    MIN_SCAN_SECONDS,
    classify,
    scan,
)
from .evidence import PrivateStore, publish
from .gatt import (
    CATALOGUE,
    COMMAND_WRITE_UUID,
    FORBIDDEN_UUIDS,
    KNOWN_WRITE_COMMANDS,
    PROBE_PLAN,
    WriteRefused,
    assert_notifiable,
    assert_readable,
    describe,
    refuse_command,
)
from .privacy import redact_address, redact_name

__all__ = ["build_parser", "main"]

#: Default private store, relative to the project root.  Git-ignored.
DEFAULT_STORE = ".private"

#: Default collector state directory.  Separate from the private store: this
#: holds a sanitized document meant to be read by a display, not raw evidence.
DEFAULT_STATE_DIR = ".state"


def _positive_finite_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must be a number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be finite and greater than zero")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uniden-r8",
        description="Receive-only Uniden R8 Bluetooth LE support. No "
                    "application-characteristic write path: never sends a "
                    "Uniden command, settings, mute or user-mark write. Never "
                    "touches the OBDLink or /dev/rfcomm0.",
    )
    parser.add_argument("--version", action="version", version=f"uniden-r8 {__version__}")
    parser.add_argument(
        "--store",
        default=DEFAULT_STORE,
        help=f"private evidence directory, 0700 (default: {DEFAULT_STORE})",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="configuration file (default: search ./unidenr8.toml, then "
             "$XDG_CONFIG_HOME/uniden-r8/config.toml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("plan", help="print the read-only probe plan and refusal list")
    sub.add_parser(
        "selftest", help="prove the no-application-write properties; needs no radio"
    )

    scan_parser = sub.add_parser(
        "scan", help="one bounded advertisement-only discovery window"
    )
    scan_parser.add_argument(
        "-s",
        "--seconds",
        type=float,
        default=DEFAULT_SCAN_SECONDS,
        help=(
            f"scan window, clamped to "
            f"{MIN_SCAN_SECONDS:g}-{MAX_SCAN_SECONDS:g} s "
            f"(default: {DEFAULT_SCAN_SECONDS:g})"
        ),
    )
    scan_parser.add_argument(
        "--json", action="store_true", help="emit the sanitized report as JSON"
    )
    scan_parser.add_argument(
        "--save",
        metavar="NAME",
        help="also write the sanitized report into the private store under NAME",
    )

    pair_parser = sub.add_parser(
        "pair",
        help="scan, then pair with the single strong candidate (persistent change)",
    )
    pair_parser.add_argument(
        "--confirm",
        action="store_true",
        help="required: pairing is a persistent change to the node's BlueZ state",
    )
    pair_parser.add_argument("-s", "--seconds", type=float, default=20.0)
    pair_parser.add_argument(
        "--use-bond",
        action="store_true",
        help="resolve the detector from BlueZ's existing bond state, no discovery",
    )
    pair_parser.add_argument(
        "--then-read",
        action="store_true",
        help="on success, immediately read the Device Information characteristics",
    )

    identity_parser = sub.add_parser(
        "identity", help="GATT-read the Device Information characteristics"
    )
    identity_parser.add_argument("-s", "--seconds", type=float, default=20.0)

    live_parser = sub.add_parser(
        "live", help="bounded receive-only live telemetry and alerts"
    )
    live_parser.add_argument(
        "-s", "--seconds", type=float, default=None,
        help="collection window, clamped to 5-120 s (default: 30)",
    )
    live_parser.add_argument(
        "--json", action="store_true", help="emit the sanitized session as JSON"
    )
    live_parser.add_argument(
        "--save", metavar="NAME",
        help="also write the sanitized session into the private store under NAME",
    )
    live_parser.add_argument(
        "--full", action="store_true",
        help="also print and emit the full decoded surface, including the "
             "detector's own heading, speed and altitude (off by default: "
             "those describe where the vehicle is)",
    )

    collect_parser = sub.add_parser(
        "collect",
        help="background collector: hold the link, publish display state",
    )
    collect_parser.add_argument(
        "--state-dir", default=None,
        help=f"where state.json is written, 0700 (default: the configured "
             f"value, else {DEFAULT_STATE_DIR})",
    )
    collect_parser.add_argument(
        "--duration", type=_positive_finite_seconds, default=None,
        help="bounded trial: stop after this many seconds (default: run until "
             "SIGTERM/SIGINT)",
    )

    inspect_parser = sub.add_parser(
        "inspect",
        help="one confirmed read-only dump of settings and POI into the "
             "private store",
    )
    inspect_parser.add_argument(
        "--confirm", action="store_true",
        help="required: this reads the POI database, which holds saved camera "
             "locations and user marks",
    )

    history_parser = sub.add_parser(
        "history", help="query the local history database; no radio"
    )
    history_parser.add_argument(
        "what", nargs="?", default="stats",
        choices=("stats", "events", "encounters", "telemetry"),
        help="what to show (default: stats)",
    )
    history_parser.add_argument("-n", "--limit", type=int, default=20)
    history_parser.add_argument(
        "--json", action="store_true", help="emit rows as JSON"
    )
    history_parser.add_argument(
        "--full", action="store_true",
        help="include recorded coordinates (omitted by default, because the "
             "publication gate refuses a position and omitting a column is a "
             "better answer than failing)",
    )

    config_parser = sub.add_parser(
        "config", help="print the effective configuration; no radio"
    )
    config_parser.add_argument(
        "--example", action="store_true",
        help="print a complete commented configuration file instead",
    )
    return parser


def _cmd_plan() -> int:
    print(f"uniden-r8 {__version__}  read-only probe plan\n")
    print("Permitted operations, in order:\n")
    for index, (operation, uuid) in enumerate(PROBE_PLAN, 1):
        entry = describe(uuid)
        print(f"  {index:>2}. {operation:<7} {entry.name:<28} [{entry.evidence.value}]")

    print("\nPermanently forbidden:\n")
    for uuid in sorted(FORBIDDEN_UUIDS):
        entry = describe(uuid)
        print(f"      {entry.name} ({uuid})")

    print("\nApplication commands this project refuses to transmit:\n")
    for command in KNOWN_WRITE_COMMANDS:
        print(f"      {command}")
    print(
        "\n  Recorded from AegisX86/UnidenR8wlink @ 9072bc2f, which documents "
        "\n  them as decompiled from the Uniden R/Tach app and never sent to "
        "\n  hardware on any model.  There is no flag that enables them here."
    )
    return 0


def _cmd_selftest(store_path: str) -> int:
    checks: list[tuple[str, bool, str]] = []

    # 1. The command characteristic is refused by the gate.
    for label, gate in (("read", assert_readable), ("notify", assert_notifiable)):
        try:
            gate(COMMAND_WRITE_UUID)
        except WriteRefused:
            checks.append((f"gate refuses {label} of the command characteristic", True, ""))
        else:  # pragma: no cover - a regression here is a release blocker
            checks.append((f"gate refuses {label} of the command characteristic", False,
                           "the gate ALLOWED it"))

    # 2. Every known R/Tach command is refused.
    refused = 0
    for command in KNOWN_WRITE_COMMANDS:
        try:
            refuse_command(command)
        except WriteRefused:
            refused += 1
    checks.append(
        (f"all {len(KNOWN_WRITE_COMMANDS)} known R/Tach commands refused",
         refused == len(KNOWN_WRITE_COMMANDS),
         f"only {refused} refused"),
    )

    # 3. No write path exists in the installed package source.  This parses
    # rather than greps: the safety modules discuss write_gatt_char in prose,
    # and a check that cannot tell a docstring from a call is not a check.
    findings = audit_package(Path(__file__).resolve().parent)
    checks.append(
        ("no module references an application-write bleak API", not findings,
         "; ".join(str(f) for f in findings)),
    )

    # 4. The private store is sealed.
    store = PrivateStore(store_path).ensure()
    _ = store.salt
    checks.append(
        (f"private store {store_path} is 0700/0600", store.is_sealed(),
         f"modes: {store.audit()}"),
    )

    # 5. The catalogue is internally consistent.
    consistent = all(
        not (entry.readable and entry.uuid in FORBIDDEN_UUIDS) for entry in CATALOGUE
    )
    checks.append(("no forbidden characteristic is marked readable", consistent, ""))

    width = max(len(label) for label, _, _ in checks)
    failures = 0
    for label, passed, detail in checks:
        status = "ok  " if passed else "FAIL"
        print(f"  {status}  {label.ljust(width)}" + (f"   {detail}" if not passed else ""))
        failures += not passed

    print()
    if failures:
        print(f"{failures} check(s) FAILED; do not run against the detector.")
        return 1
    print("All read-only properties hold.")
    return 0


def _render(report) -> str:
    lines = [
        f"scan started {report.started_at}, window {report.requested_seconds:g}s",
        f"devices seen: {report.total_seen}"
        + ("  (scan timed out and was cancelled)" if report.timed_out else ""),
        "",
    ]
    if not report.candidates:
        lines += [
            "No R-series candidate advertised in this window.",
            "",
            "Upstream's R8w advertises while in BT Pairing or while paired and",
            "idle. This R8's behaviour outside pairing mode is not established;",
            "re-arm BT Pairing, and make sure a phone is not holding its link.",
        ]
    else:
        lines.append("tier      name                   token          signal")
        lines += [f"{candidate.line()}" for candidate in report.candidates]
    return "\n".join(lines)


def _cmd_scan(store_path: str, seconds: float, as_json: bool, save: str | None) -> int:
    store = PrivateStore(store_path).ensure()
    try:
        report = asyncio.run(scan(seconds, store.salt))
    except ImportError:
        print(
            "bleak is not installed in this environment.\n"
            "The scan needs it; the plan and selftest subcommands do not.\n"
            "  python3 -m venv .venv && .venv/bin/pip install bleak",
            file=sys.stderr,
        )
        return 2

    if save:
        store.write_json(save, report.as_dict())

    output = json.dumps(report.as_dict(), indent=2) if as_json else _render(report)
    # publish() refuses rather than sanitizes: reaching it with an address in
    # hand means the redaction above failed, and that must be loud.
    print(publish(output))
    return 0


async def _find_one_strong(store, seconds: float):
    """Return the single strong candidate's raw address, or raise.

    Fail-closed on ambiguity.  The address is returned for immediate use and
    never printed, stored outside the private store, or returned to a caller
    that would publish it.
    """
    from .discovery import _discover, _iter_results, _to_advertisement, bounded_seconds

    window = bounded_seconds(seconds)
    results = await asyncio.wait_for(_discover(timeout=window), timeout=window + 5.0)
    advertisements = [_to_advertisement(entry) for entry in _iter_results(results)]
    strong = [a for a in advertisements if classify(a.name) == "strong"]

    if not strong:
        raise LookupError(
            f"no R-series candidate advertised in {window:g}s.  An unpaired "
            f"detector does not advertise unless it is in BT Pairing mode, and "
            f"a paired one stops the moment anything connects to it."
        )
    if len(strong) > 1:
        raise LookupError(
            f"{len(strong)} R-series candidates advertised; refusing to guess "
            f"which one is the detector"
        )
    return strong[0], len(advertisements)


def _cmd_pair(
    store_path: str, seconds: float, confirm: bool, then_read: bool, use_bond: bool
) -> int:
    from .pairing import PairingRefused, pair

    if not confirm:
        print(
            "Pairing is a persistent change to this node's Bluetooth state.\n"
            "Re-run with --confirm once the detector is in BT Pairing mode.",
            file=sys.stderr,
        )
        return 2

    store = PrivateStore(store_path).ensure()
    salt = store.salt

    if use_bond:
        # BlueZ already holds the address to keep the bond, so reading it back
        # costs no new exposure -- unlike writing a second copy to a file.
        from .pairing import bonded_detector_address

        try:
            address = bonded_detector_address()
        except LookupError as exc:
            print(publish(str(exc)), file=sys.stderr)
            return 1
        print(f"using the bonded detector {redact_address(address, salt)} (no discovery)")
    else:
        try:
            candidate, total = asyncio.run(_find_one_strong(store, seconds))
        except (LookupError, TimeoutError) as exc:
            print(publish(str(exc)), file=sys.stderr)
            return 1
        except ImportError:
            print("bleak is not installed in this environment.", file=sys.stderr)
            return 2
        address = candidate.address
        print(f"found 1 strong candidate out of {total} devices seen:")
        print(f"  {redact_name(candidate.name, salt)}  {redact_address(address, salt)}")

    print("pairing (leaving it untrusted and disconnected)...")

    try:
        result = pair(address, salt)
    except PairingRefused as exc:
        print(publish(f"refused: {exc}"), file=sys.stderr)
        return 1

    store.write_json("pair-result.json", {
        "paired": result.paired, "trusted": result.trusted,
        "connected": result.connected, "attempts": result.attempts,
        "detail": result.detail,
    })

    print(f"  paired:    {result.paired}   (attempts: {result.attempts})")
    print(f"  trusted:   {result.trusted}  (must be False: BlueZ would auto-reconnect)")
    print(f"  connected: {result.connected} (must be False: BlueZ must release the link)")
    if result.detail:
        print(f"  note: {publish(result.detail)}")
    if not result.paired:
        print("\nlast bluetoothctl lines:", file=sys.stderr)
        for line in result.transcript[-12:]:
            print(f"  {publish(line)}", file=sys.stderr)
        return 1

    # Postconditions.  A bond that succeeded but left the device trusted or
    # connected is a failure, not a success with a footnote: a trusted device
    # is auto-reconnected by BlueZ forever and competes for the radio the
    # vehicle link uses, and a held link makes the detector invisible to
    # everything else.  Both must be loud.
    violations = []
    if result.trusted:
        violations.append(
            "device is still TRUSTED — BlueZ will auto-reconnect to it on every "
            "boot and compete for the radio; run: bluetoothctl untrust <device>"
        )
    if result.connected:
        violations.append(
            "device is still CONNECTED — BlueZ is holding the link, so nothing "
            "else can reach it; run: bluetoothctl disconnect <device>"
        )
    if violations:
        print("\nPAIRING POSTCONDITION FAILED:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1

    if then_read:
        print()
        return _cmd_identity(store_path, seconds, address=address)
    return 0


def _cmd_identity(store_path: str, seconds: float, address: str | None = None) -> int:
    from .identity import read_identity

    store = PrivateStore(store_path).ensure()
    salt = store.salt

    if address is None:
        # Prefer BlueZ's own bond state: no discovery, and no second copy of
        # the address anywhere.
        from .pairing import PairingRefused, bonded_detector_address

        try:
            address = bonded_detector_address()
        except (LookupError, PairingRefused):
            address = None
    if address is None:
        try:
            candidate, _ = asyncio.run(_find_one_strong(store, seconds))
            address = candidate.address
        except (LookupError, TimeoutError) as exc:
            print(publish(str(exc)), file=sys.stderr)
            return 1
        except ImportError:
            print("bleak is not installed in this environment.", file=sys.stderr)
            return 2

    identity = asyncio.run(read_identity(address, salt))
    store.write_json("identity.json", identity.as_dict())
    print(publish(identity.render()))
    return 0 if identity.connected else 1


def _cmd_live(store_path: str, seconds: float | None, as_json: bool,
              save: str | None, full: bool = False) -> int:
    from .pairing import PairingRefused, bonded_detector_address
    from .telemetry import receive

    store = PrivateStore(store_path).ensure()
    salt = store.salt

    try:
        address = bonded_detector_address()
    except (LookupError, PairingRefused) as exc:
        print(publish(str(exc)), file=sys.stderr)
        return 1

    print(
        f"receiving from the bonded detector {redact_address(address, salt)}",
        file=sys.stderr if as_json else sys.stdout,
    )
    try:
        session = asyncio.run(
            receive(address, salt, store, seconds, detailed=full)
        )
    except ImportError:
        print("bleak is not installed in this environment.", file=sys.stderr)
        return 2

    if save:
        store.write_json(save, session.as_dict())

    output = json.dumps(session.as_dict(), indent=2) if as_json else session.render()
    if full:
        # publish() refuses a position, which is exactly right for the default
        # surface and exactly wrong here: --full is a person asking to see the
        # position-adjacent fields on their own terminal.  The refusal is
        # bypassed deliberately, at one call site, with the reason attached.
        print(output)
    else:
        print(publish(output))
    if not session.connected:
        return 1
    return 0 if session.compatible else 1


def _cmd_collect(state_dir: str | None, duration: float | None,
                 settings) -> int:
    from .collector import InstanceBusy, SingleInstanceLock, run
    from .pairing import PairingRefused, bonded_detector_address

    # An explicit --state-dir wins over the configuration file, which wins over
    # the built-in default.  Stated in that order because a person typing a
    # flag has just made a decision and a file made one earlier.
    #
    # The override is folded back into the settings rather than used alongside
    # them, because a relative `history.path` resolves against
    # `collector.state_dir`.  Used alongside, `--state-dir /tmp/trial` would put
    # the state documents in the trial directory and leave the database writing
    # into the real one -- a trial quietly appending to production history.
    if state_dir:
        settings = replace(
            settings, collector=replace(settings.collector, state_dir=state_dir)
        )
    resolved = settings.collector.state_dir

    try:
        address = bonded_detector_address()
    except (LookupError, PairingRefused) as exc:
        print(publish(str(exc)), file=sys.stderr)
        return 1

    lock = SingleInstanceLock(Path(resolved) / "collector.lock")
    try:
        lock.acquire()
    except InstanceBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3

    mode = f"bounded trial, {duration:g}s" if duration is not None else "continuous"
    print(f"collector starting ({mode}); state -> {resolved}/state.json")
    for note in settings.warnings():
        print(f"  ! {note}", file=sys.stderr)
    try:
        return asyncio.run(
            run(address, resolved, duration=duration, config=settings)
        )
    except ImportError:
        print("bleak is not installed in this environment.", file=sys.stderr)
        return 2
    finally:
        # Released on every exit path, including a signal: without this a
        # crashed collector would lock out its own replacement.
        lock.release()


def _cmd_inspect(store_path: str, confirmed: bool, settings) -> int:
    """One deliberate look at settings and POI, into the private store only."""
    from .inspection import InspectionRefused, inspect
    from .pairing import PairingRefused, bonded_detector_address

    if not confirmed:
        print(
            "inspect reads the detector's settings blocks and its POI "
            "database.\nPOI holds saved camera locations and user marks -- "
            "home, work, the roads\nyou drive.  Nothing is decoded and "
            "nothing leaves the private store, but\nthe bytes are read.  "
            "Re-run with --confirm.",
            file=sys.stderr,
        )
        return 2

    store = PrivateStore(store_path).ensure()
    salt = store.salt
    try:
        address = bonded_detector_address()
    except (LookupError, PairingRefused) as exc:
        print(publish(str(exc)), file=sys.stderr)
        return 1

    try:
        result = asyncio.run(
            inspect(address, salt, store, confirmed=True,
                    adapter=settings.collector.adapter or None)
        )
    except InspectionRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ImportError:
        print("bleak is not installed in this environment.", file=sys.stderr)
        return 2

    print(publish(result.render()))
    return 0 if result.compatible else 1


def _cmd_history(what: str, limit: int, as_json: bool, settings,
                 full: bool = False) -> int:
    """Read the local history.  No radio, no detector, no network."""
    from .storage import HistoryError, open_history

    try:
        history = open_history(settings.history_path)
    except HistoryError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        if what == "stats":
            rows = [history.stats()]
        elif what == "events":
            rows = history.events(limit)
        elif what == "encounters":
            rows = history.encounters(limit)
        else:
            rows = history.telemetry(limit)
    finally:
        history.close()

    if not full:
        # The publication gate refuses a document containing a position, and it
        # is right to -- but this is the owner querying their own history on
        # their own terminal, so the answer is to omit the columns rather than
        # to fail. `--full` is the explicit ask, the same shape as `live --full`.
        rows = [
            {key: value for key, value in row.items() if key not in _POSITION_COLUMNS}
            for row in rows
        ]

    if as_json:
        rendered = json.dumps(rows, indent=2, default=str)
        print(rendered if full else publish(rendered))
        return 0
    if not rows:
        print(f"no {what} recorded")
        return 0
    rendered = _render_rows(what, rows)
    print(rendered if full else publish(rendered))
    return 0


#: History columns that describe where the vehicle was or how it was moving.
#: Omitted unless `--full` is given.
#:
#: The detector's own heading, speed and altitude belong here as much as a
#: coordinate does: a printed column of them across a drive is a rough trace of
#: it, which is the whole reason recording them is opt-in in the first place.
#: Leaving them out of this set would have made `history telemetry` the one
#: command that printed them with no gate at all.
_POSITION_COLUMNS = frozenset(
    {"lat", "lon", "direction_8", "speed_mph", "altitude_ft"}
)


def _render_rows(what: str, rows: list) -> str:
    """Format history rows as a table, dropping the columns that are empty.

    Columns that are null in every row are omitted rather than printed as a
    field of dashes: a history recorded without coordinates should not show an
    empty latitude column implying one was expected.
    """
    if what == "stats":
        stats = rows[0]
        lines = [
            f"history {stats['path']}",
            f"  schema {stats['schema']}, {stats['size_bytes']:,} bytes",
        ]
        lines += [f"  {name:<14} {count:>8,}"
                  for name, count in stats["counts"].items()]
        if stats["first_alert_at"]:
            lines.append(f"  alerts span {stats['first_alert_at']} .. "
                         f"{stats['last_alert_at']}")
        return "\n".join(lines)

    columns = [
        name for name in rows[0]
        if any(row.get(name) is not None for row in rows)
        and name not in {"id", "session_id", "wall_ns", "monotonic_ns"}
    ]
    widths = {
        name: max(len(name), *(len(_cell(row.get(name))) for row in rows))
        for name in columns
    }
    header = "  ".join(name.ljust(widths[name]) for name in columns)
    lines = [header, "-" * len(header)]
    lines += [
        "  ".join(_cell(row.get(name)).ljust(widths[name]) for name in columns)
        for row in rows
    ]
    return "\n".join(lines)


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _cmd_config(settings, example: bool) -> int:
    print(example_toml() if example else describe_config(settings))
    return 0


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 - one per command
    args = build_parser().parse_args(argv)

    # The two commands that need no configuration do not load one, so a broken
    # config file cannot stop a reviewer reading the plan or proving the
    # safety properties -- which are exactly what someone reaches for when
    # something is broken.
    if args.command == "plan":
        return _cmd_plan()
    if args.command == "selftest":
        return _cmd_selftest(args.store)
    if args.command == "config" and args.example:
        # Deliberately before the load.  A file with a typo would otherwise
        # block the one command whose job is to print a known-good file, which
        # is exactly what somebody reaches for when their file has a typo.
        print(example_toml())
        return 0

    try:
        settings = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration: {exc}", file=sys.stderr)
        return 2

    if args.command == "config":
        return _cmd_config(settings, args.example)
    if args.command == "scan":
        return _cmd_scan(args.store, args.seconds, args.json, args.save)
    if args.command == "pair":
        return _cmd_pair(
            args.store, args.seconds, args.confirm, args.then_read, args.use_bond
        )
    if args.command == "identity":
        return _cmd_identity(args.store, args.seconds)
    if args.command == "live":
        return _cmd_live(args.store, args.seconds, args.json, args.save, args.full)
    if args.command == "collect":
        return _cmd_collect(args.state_dir, args.duration, settings)
    if args.command == "inspect":
        return _cmd_inspect(args.store, args.confirm, settings)
    if args.command == "history":
        return _cmd_history(args.what, args.limit, args.json, settings, args.full)
    return 2  # pragma: no cover - argparse rejects unknown subcommands first


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
