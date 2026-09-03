#!/bin/bash
# Summarise what a drive actually captured.  Reads the local history database
# and nothing else: no radio, no network, no detector.
#
#     ./scripts/drive-report.sh              # the most recent session
#     ./scripts/drive-report.sh --all        # every session in the database
#     ./scripts/drive-report.sh --session 7  # one session by id
#
# It answers the three questions worth asking after a drive, in order:
#
#   1. Did anything get captured at all, and was any of it unparsed?
#   2. Did a real alert arrive -- and if one did, is its raw text here?
#   3. Did the detector's own motion fields actually get written down?
#
# The third question is the one that has silently failed before.  With
# `record_detector_motion` off, the collector runs perfectly, publishes a
# healthy state document and writes heading, speed and altitude as NULL on every
# row -- so a drive undertaken to validate exactly those fields produces a
# database that cannot answer it.  This report says so loudly rather than
# printing an empty column.
#
# It prints no coordinate: the detector sends none in telemetry, external ones
# are opt-in, and the position-adjacent motion fields are summarised as ranges
# and counts rather than as a track.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

DB=""
MODE="latest"
SESSION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)     MODE="all"; shift ;;
        --session) MODE="one"; SESSION="${2:?--session needs an id}"; shift 2 ;;
        --db)      DB="${2:?--db needs a path}"; shift 2 ;;
        -h|--help) sed -n '2,25p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$DB" ]]; then
    DB="$(
        "$PYTHON" - "$ROOT" <<'PY' 2>/dev/null || true
import pathlib, sys
root = pathlib.Path(sys.argv[1])
config = root / "unidenr8.toml"
state, path = root / ".state", "history.db"
if config.exists():
    try:
        import tomllib
    except ModuleNotFoundError:                  # pragma: no cover - py<3.11
        import tomli as tomllib
    doc = tomllib.loads(config.read_text())
    value = (doc.get("collector") or {}).get("state_dir")
    if value:
        candidate = pathlib.Path(value).expanduser()
        state = candidate if candidate.is_absolute() else root / candidate
    path = (doc.get("history") or {}).get("path") or path
target = pathlib.Path(path).expanduser()
print(target if target.is_absolute() else state / target)
PY
    )"
fi

[[ -n "$DB" && -f "$DB" ]] || {
    echo "drive-report: no history database at ${DB:-<unresolved>}" >&2
    echo "    Is [history] enabled = true in unidenr8.toml?" >&2
    exit 1
}

MODE="$MODE" SESSION="$SESSION" "$PYTHON" - "$DB" <<'PY'
import os, sqlite3, sys

db = sys.argv[1]
mode = os.environ.get("MODE", "latest")
wanted = os.environ.get("SESSION") or ""

connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
q = connection.execute

schema = q("SELECT value FROM meta WHERE key='schema'").fetchone()
print(f"history {db}")
print(f"  schema {schema['value'] if schema else '?'}, "
      f"{os.path.getsize(db):,} bytes\n")

if mode == "one":
    rows = q("SELECT * FROM sessions WHERE id = ?", (wanted,)).fetchall()
elif mode == "all":
    rows = q("SELECT * FROM sessions ORDER BY id").fetchall()
else:
    rows = q("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchall()

if not rows:
    print("no sessions recorded")
    raise SystemExit(1)

def counts(sid, table):
    return q(f"SELECT count(*) c FROM {table} WHERE session_id = ?",
             (sid,)).fetchone()["c"]

for session in rows:
    sid = session["id"]
    print(f"session {sid}  {session['started_at']} -> "
          f"{session['ended_at'] or '(still open)'}")

    telemetry = counts(sid, "telemetry")
    snapshots = counts(sid, "alert_snapshots")
    events = counts(sid, "alert_events")
    print(f"  telemetry rows      {telemetry:,}")
    print(f"  alert snapshots     {snapshots:,}")
    print(f"  alert events        {events:,}")

    if not telemetry:
        print("  -> nothing recorded for this session\n")
        continue

    span = q(
        "SELECT min(monotonic_ns) a, max(monotonic_ns) b FROM telemetry "
        "WHERE session_id = ?", (sid,)).fetchone()
    seconds = (span["b"] - span["a"]) / 1e9 if span["a"] is not None else 0.0
    print(f"  duration            {seconds/60:.1f} min "
          f"({seconds:.0f}s, monotonic -- immune to a clock step)")

    volts = q(
        "SELECT min(voltage) lo, max(voltage) hi, avg(voltage) av FROM telemetry "
        "WHERE session_id = ? AND voltage IS NOT NULL", (sid,)).fetchone()
    if volts["lo"] is not None:
        print(f"  voltage             {volts['lo']:.1f} - {volts['hi']:.1f} V "
              f"(mean {volts['av']:.2f})")

    # --- the question that has silently failed before ---
    motion = q(
        "SELECT count(direction_8) d, count(speed_mph) s, count(altitude_ft) a "
        "FROM telemetry WHERE session_id = ?", (sid,)).fetchone()
    if not any((motion["d"], motion["s"], motion["a"])):
        print("  MOTION FIELDS       *** NOT RECORDED ***")
        print("      heading, speed and altitude are NULL on every row.")
        print("      Set [history] record_detector_motion = true and drive again;")
        print("      this drive cannot answer anything about those three fields.")
    else:
        speed = q(
            "SELECT min(speed_mph) lo, max(speed_mph) hi FROM telemetry "
            "WHERE session_id = ? AND speed_mph IS NOT NULL", (sid,)).fetchone()
        alt = q(
            "SELECT min(altitude_ft) lo, max(altitude_ft) hi FROM telemetry "
            "WHERE session_id = ? AND altitude_ft IS NOT NULL", (sid,)).fetchone()
        moving = q(
            "SELECT count(*) c FROM telemetry WHERE session_id = ? "
            "AND speed_mph > 0", (sid,)).fetchone()["c"]
        headings = q(
            "SELECT direction_8 d, count(*) c FROM telemetry WHERE session_id = ? "
            "AND direction_8 IS NOT NULL GROUP BY direction_8 ORDER BY c DESC",
            (sid,)).fetchall()
        print(f"  motion rows         {motion['d']:,} heading / "
              f"{motion['s']:,} speed / {motion['a']:,} altitude")
        print(f"  moving samples      {moving:,}"
              + ("   <- the vehicle actually moved" if moving else
                 "   *** the vehicle never moved: units still unvalidated ***"))
        if speed["lo"] is not None:
            print(f"  detector speed      {speed['lo']} - {speed['hi']} "
                  f"(units UNVALIDATED; upstream reads them as mph)")
        if alt["lo"] is not None:
            print(f"  detector altitude   {alt['lo']} - {alt['hi']} "
                  f"(units UNVALIDATED; upstream reads them as feet)")
        if headings:
            spread = " ".join(f"{r['d']}:{r['c']}" for r in headings)
            print(f"  headings seen       {len(headings)} of 8   {spread}")
            if len(headings) == 1:
                print("      only one heading: a turn is what validates this field")

    unparsed = q(
        "SELECT count(*) c FROM telemetry WHERE session_id = ? AND voltage IS NULL",
        (sid,)).fetchone()["c"]
    if unparsed:
        print(f"  UNPARSED            {unparsed:,} rows carried no voltage")

    # --- POI ---
    poi = q("SELECT count(*) c FROM telemetry WHERE session_id = ? AND poi_active = 1",
            (sid,)).fetchone()["c"]
    suspect = q(
        "SELECT count(*) c FROM telemetry WHERE session_id = ? AND poi_suspect = 1",
        (sid,)).fetchone()["c"]
    if poi:
        print(f"  POI WARNINGS        {poi:,} rows  <- V7 data, look at poi_raw")
        shapes = q(
            "SELECT DISTINCT poi_raw FROM telemetry WHERE session_id = ? "
            "AND poi_raw IS NOT NULL LIMIT 5", (sid,)).fetchall()
        for shape in shapes:
            print(f"      {shape['poi_raw']}")
        if not shapes:
            print("      (poi_raw is NULL -- record_detector_motion gates it)")
    if suspect:
        print(f"  POI TRIPWIRE FIRED  {suspect:,} rows -- the POI group looked like")
        print("      a coordinate pair, so the text was withheld. Read docs/PROTOCOL.md")
        print("      3.5: if this is real, the documentation is what needs correcting.")

    # --- the headline ---
    if snapshots:
        real = q(
            "SELECT payload, at, slot_count, recognised, rejected "
            "FROM alert_snapshots WHERE session_id = ? AND payload NOT LIKE '0&0&0&0%' "
            "ORDER BY id LIMIT 10", (sid,)).fetchall()
        allclear = snapshots - len(q(
            "SELECT id FROM alert_snapshots WHERE session_id = ? "
            "AND payload NOT LIKE '0&0&0&0%'", (sid,)).fetchall())
        print(f"  all-clear packets   {allclear:,}")
        if real:
            print()
            print("  *** A NON-ALL-CLEAR ALERT PACKET WAS CAPTURED ***")
            print("  This is the evidence this project has never had. The raw text is")
            print("  below and is stored verbatim, so it stands whether or not the")
            print("  parser read it correctly.")
            for row in real:
                flag = "" if row["recognised"] else "   <- PARSER REJECTED A SLOT"
                print(f"      {row['at']}  slots={row['slot_count']} "
                      f"rejected={row['rejected']}{flag}")
                print(f"      {row['payload']}")
            print()
            print("  Next: docs/VALIDATION.md V2. Compare each field against what the")
            print("  detector's own screen showed, and promote the alert rows in")
            print("  docs/EVIDENCE.md from UPSTREAM to OBSERVED.")
        else:
            print("  no active alert captured this session")

    if events:
        print()
        for row in q(
            "SELECT kind, at, band, strength, direction, duration_s, max_strength "
            "FROM alert_events WHERE session_id = ? ORDER BY id LIMIT 20",
            (sid,)):
            print(f"      {row['kind']:<12} {row['at']}  {row['band']:<6} "
                  f"str={row['strength']} {row['direction']} "
                  f"dur={row['duration_s']}")
    print()
PY
