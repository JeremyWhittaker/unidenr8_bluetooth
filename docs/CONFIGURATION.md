# Configuration

One TOML file, validated strictly, with safe defaults. Everything the collector
can do beyond "connect to the bonded detector and write a state file" is off
until it is switched on here, because each of those things is a place data can
go.

The source of truth for this document is `src/uniden_r8/config.py`. Every key
below is a field of a frozen dataclass in that module; every range comes from
its `_RANGES` table; every warning is quoted from `Config.warnings()`. Where
this document describes what a key *does*, it names the file that consumes it,
because a configuration reference that cannot be checked against the code is a
configuration reference that will drift.

Two facts from `docs/EVIDENCE.md` shape several of the decisions here, and are
repeated wherever they matter:

* **The live BLE telemetry contains no latitude or longitude** (OBSERVED, §7.3).
  The detector's GPS sub-group carries a heading to the nearest of eight compass
  points, a speed, an altitude and a status letter. Coordinates come from a
  separate GNSS receiver over gpsd and live in their own branch of the schema,
  named `vehicle_gnss`, never merged with `detector_gps`.
* **The only alert payload ever seen from this non-W R8 is all-clear**
  (OBSERVED, §7.6). Every field of an *active* alert — band, strength,
  frequency, direction, mute — is UPSTREAM: captured on an R8w by
  AegisX86/UnidenR8wlink @ `9072bc2f`, never confirmed here. Anything this
  configuration turns on that stores, publishes or displays an active alert is
  therefore storing, publishing or displaying an unverified reading.

---

## 1. Where the configuration comes from

`find_config()` in `src/uniden_r8/config.py` resolves exactly one file, in this
order. The first hit wins.

| Order | Source | If it does not exist |
|---|---|---|
| 1 | `--config PATH` on the command line | **error** — `no configuration file at PATH`, exit 2 |
| 2 | `$UNIDEN_R8_CONFIG` | **error** — `UNIDEN_R8_CONFIG points at PATH, which does not exist`, exit 2 |
| 3 | `./unidenr8.toml` (working directory) | fall through |
| 4 | `./.unidenr8.toml` (working directory) | fall through |
| 5 | `$XDG_CONFIG_HOME/uniden-r8/config.toml`, or `~/.config/uniden-r8/config.toml` when `XDG_CONFIG_HOME` is unset or empty | fall through |
| — | nothing found | **not an error** — the built-in defaults are used, and `source` becomes `defaults` |

The asymmetry in that table is deliberate and is documented in the function's
own docstring: "I told you where it is" and "look around and use one if you find
one" deserve different answers. Silently ignoring a `--config` typo would run
the collector with a configuration nobody chose.

Note that the two working-directory names are searched only in the working
directory, and the config-home file has a different name (`config.toml`, under a
`uniden-r8/` directory). That is what `uniden-r8 config --example` prints in its
header, and it is what the code does.

### Which file actually won

`Config.source` records the resolved path, and `uniden-r8 config` prints it on
the first line:

```
configuration from /home/jeremy/unidenr8/unidenr8.toml
```

or `configuration from defaults` when nothing was found. That line exists for
the "why is it behaving like that" question, which on a node with a service, a
shell and a `~/.config` directory is not always obvious.

### The environment variable, and why a service should set it

`systemd/unidenr8-collector.service` sets

```
Environment=UNIDEN_R8_CONFIG=/home/jeremy/unidenr8/unidenr8.toml
```

rather than relying on `WorkingDirectory`. A service whose behaviour depends on
its working directory is a service that behaves differently when someone runs it
by hand to debug it — which is precisely when the difference is most expensive.

### Getting a starting file

```bash
uniden-r8 config --example > unidenr8.toml   # every key, at its default
uniden-r8 config                             # what that file resolved to
```

`example_toml()` is generated from the dataclasses, not maintained by hand, so
it cannot describe a key that no longer exists or omit one that was just added.

---

## 2. Why an unknown key is an error

Validation is unforgiving on purpose, and the reason is one specific failure
mode rather than a general taste for strictness.

`record_coordinates` defaults to **off**. A file containing
`record_coordinate = true` — one missing letter — would, under a permissive
loader, load cleanly, log nothing, and leave coordinate recording exactly where
it was. The operator would believe coordinates were being recorded when they
were not; the same typo in the other direction (a key meant to *disable*
something) would leave data flowing that the operator believed was off. A
privacy-relevant setting that silently does nothing is the exact failure this
strictness prevents. "Your config had a typo" is a much better failure than
"your config did not do what you thought".

Five classes of rejection, all raised as `ConfigError` and reported by the CLI
as `configuration: …` with exit status 2:

| Input | Result |
|---|---|
| `[collektor]` | `<file>: unknown section(s) collektor.  Known sections: collector, feed, gnss, history, mqtt, obd` |
| `[collector] state_dirr = ".x"` | `[collector] has unknown key(s): state_dirr.  Known keys: adapter, detail, heartbeat_seconds, queue_size, stale_after_seconds, state_dir` |
| `[collector] heartbeat_seconds = 0.1` | `[collector] heartbeat_seconds = 0.1 is outside the permitted range 0.5-300` |
| `[collector] queue_size = true` | `[collector] queue_size must be a whole number` |
| `collector = 3` | `[collector] must be a table` |

The error names the known keys, because the common case is a typo and the fix is
usually visible in the message.

Two details of the type checking are worth knowing:

**A boolean is not a number.** Python makes `bool` a subclass of `int`, so a
naive check would accept `queue_size = true` and silently store 1. `_coerce()`
rejects it. The same guard runs on float fields.

**A float field accepts an integer.** `heartbeat_seconds = 5` is valid and
becomes `5.0`, which is what makes the generated example file round-trip: it
prints `heartbeat_seconds = 5`.

**Every numeric value is range-checked at load**, before the radio is touched.
A bad number fails at start-up rather than in the middle of a drive.

---

## 3. Section reference

Six sections, all optional. An absent section means "all defaults"; an empty
section (`[collector]` with nothing under it) is legal and means the same thing.

Two properties are derived rather than configured, and cannot be set in the
file: `Config.source` (above) and `Config.history_path` (§3.3).

### 3.1 `[collector]` — the collector itself

| Key | Type | Default | Range | What it does |
|---|---|---|---|---|
| `state_dir` | string | `".state"` | — | Directory for `state.json`, `state-v2.json` and `collector.lock`. Created `0700`; files are written `0600`, atomically. A relative path resolves against the working directory. |
| `adapter` | string | `""` | — | BlueZ controller to pin the detector link to, for example `hci1`. Empty means "whatever BlueZ picks". |
| `detail` | bool | `true` | — | Also write the schema-2 document `state-v2.json` beside the schema-1 one. |
| `heartbeat_seconds` | float | `5.0` | 0.5–300 | Floor for rewriting state when nothing has changed. Transitions publish immediately regardless. |
| `queue_size` | int | `256` | 8–100000 | Depth of the ingest queue between the BLE notification callback and the consumer. |
| `stale_after_seconds` | float | `10.0` | 1.0–3600 | **Accepted and range-checked, but not consumed.** See §7. |

**`adapter` is the documented remedy for radio contention.** Left unset, bleak
asks BlueZ for its default adapter, which is the first powered one — an order
that is not guaranteed across reboots. If the shared controller cannot carry the
vehicle's RFCOMM link and a BLE notification stream at once, the remedy is a
second USB adapter, and a second adapter is useless if the collector might pick
either. `_default_client_factory()` in `collector.py` passes the name as
bleak's `bluez={"adapter": …}`; `inspect` uses the same value
(`cli.py`, `_cmd_inspect`).

**`heartbeat_seconds` is also the streaming loop's wake interval.** In
`_Session._pump()` the loop waits at most `heartbeat_seconds` for a packet, so
it also bounds how promptly the OBD re-check and the link-dropped check can run.
A very long heartbeat does not only make the state file staler; it makes the
guard coarser.

**`queue_size` reserves one slot.** `events.Ingest` keeps the gap record outside
the queue proper, so the usable capacity is `queue_size − 1` notifications and
the account of what was lost can never itself be evicted. On overflow the
**oldest** record is dropped — for a radar integration the newest snapshot is
the one worth having — and the loss is published as a `Gap` carrying the exact
sequence numbers, plus `ingest.dropped` and `note: "notifications dropped"`.

### 3.2 `[obd]` — the OBDLink health gate

On the Hummer node this is the most important section in the file. It is what
stops a long-running BLE session from competing with the vehicle's telemetry
link. On a node with no OBDLink it must be switched off, or the collector will
publish `obd-blocked` and refuse to connect — correctly.

| Key | Type | Default | Range | What it does |
|---|---|---|---|---|
| `guard` | bool | `true` | — | Run the OBD probe at all. False substitutes `unguarded_obd_probe`, which always reports healthy with `reason: "guard disabled by configuration"`. |
| `unit` | string | `"hummer-rfcomm"` | — | The systemd unit queried with `systemctl is-active`. Queried only. |
| `device` | string | `"/dev/rfcomm0"` | — | The device node checked for existence and for a binding line in `rfcomm` output. Never opened. |
| `interval_seconds` | float | `15.0` | 1–600 | How often the gate re-checks while the detector link is held. |

The probe asks three questions, all queries: is the unit active, does the device
node exist, and does BlueZ report a binding on it. `make_obd_probe()` in
`collector.py` runs `systemctl is-active <unit>` and `rfcomm` with no arguments,
and nothing else. It never starts, stops, restarts, enables, disables or masks a
unit; never binds or releases the device; and never opens the serial device.
`tests/test_collector.py::test_the_collector_never_mutates_a_service_or_rfcomm`
checks every command vector in the module against a list of mutating verbs.

The probe also runs **once before every connection attempt**, not only on the
`interval_seconds` cadence, so `guard = true` with a long interval still refuses
to open a link into an unhealthy radio.

`interval_seconds` is a floor, not a guarantee: the check happens when the
streaming loop wakes, so its real granularity is bounded below by
`collector.heartbeat_seconds`.

### 3.3 `[history]` — the local SQLite history

| Key | Type | Default | Range | What it does |
|---|---|---|---|---|
| `enabled` | bool | `false` | — | Open the database and record sessions, alert transitions, throttled telemetry. |
| `path` | string | `"history.db"` | — | Database file. A relative path resolves against `collector.state_dir`; an absolute path is used as given. |
| `retain_days` | int | `30` | 0–3650 | Rows older than this are deleted. `0` disables expiry entirely. |
| `telemetry_every_seconds` | float | `10.0` | 0.0–3600 | Minimum spacing between stored telemetry rows. `0` stores every packet. |
| `record_detector_motion` | bool | `false` | — | Store the detector's own heading, speed and altitude in the `telemetry` table. Off means those three columns are written `NULL`. |

`Config.history_path` is the resolved answer, and `storage.HistoryWriter` is the
only thing that writes it: one private connection on its own thread, fed by a
non-blocking queue, WAL with `synchronous=NORMAL`, a bounded WAL size and a
bounded autocheckpoint. The reasoning is in `storage.py`'s module docstring — an
`fsync` on this SD card can take tens of milliseconds, and the same event loop
holds the BLE subscription.

**`telemetry_every_seconds = 0` is a real choice with a real cost.** The
detector sends about one telemetry packet a second (OBSERVED: ~0.98/s over a
300-second trial, `docs/EVIDENCE.md` §8.1), so no throttle is roughly 86,400
rows a day of mostly identical voltage readings, on an SD card with finite write
endurance.

**`retain_days` is applied once, at writer start** (`storage.HistoryWriter._run`
calls `History.prune`). A collector that runs for a month without restarting
does not sweep again in the meantime.

**The sweep does not trust the clock.** `History.prune` measures back from
`min(now, newest row held)` rather than from `now`. This board has no
battery-backed clock: at a cold boot the wall clock reads whatever was written
at the last shutdown and then steps by hours when the network appears, and a
plain "delete everything older than now minus N days" is a data-destruction
trigger on hardware like that. A clock stuck in the past deletes nothing; a
clock jumped into the future is ignored in favour of real data.

**A schema mismatch is reported, not migrated.** This is diagnostic history, not
a system of record; `History._check_version` refuses a file written by a
different schema version rather than silently reinterpreting rows written under
different meanings.

### 3.4 `[gnss]` — external coordinates, from gpsd

The detector does not put latitude or longitude on the wire. Nothing in this
section changes that: it configures a *separate* source, and the schema keeps it
in a separate branch so a fix from a USB receiver can never be mistaken for
something the detector said.

| Key | Type | Default | Range | What it does |
|---|---|---|---|---|
| `enabled` | bool | `false` | — | Run the gpsd client. |
| `host` | string | `"127.0.0.1"` | — | gpsd host. |
| `port` | int | `2947` | 1–65535 | gpsd port. |
| `record_coordinates` | bool | `false` | — | Whether latitude and longitude may be stored or published **at all**. |
| `stale_after_seconds` | float | `5.0` | 0.5–600 | A fix older than this is treated as no fix. |

**`enabled = true` with `record_coordinates = false` is a genuinely useful
configuration, not a half-measure.** The client still connects, still reports fix
mode, satellite count, speed and course, and still lets the collector answer "was
there a valid fix when that alert fired" — which is what validates the
detector's own speed and heading readings against a trusted source — without
ever building a record of where the vehicle has been. With coordinates withheld,
`Fix.detailed()` emits `lat: null`, `lon: null` and
`coordinates_withheld: true`, so a consumer can tell "switched off" from "this
build has no GNSS".

**Staleness is judged on the monotonic clock.** A receiver that stopped
answering ten seconds ago has no current position, whatever it said last;
returning the stale value would attach a coordinate to an alert that happened
somewhere else.

### 3.5 `[mqtt]` — publication to a broker

Anything sent here leaves the node. `paho-mqtt` is an optional extra
(`pip install 'uniden-r8-ble[mqtt]'`); with `enabled = true` and paho absent the
publisher records `last_error` and the collector carries on.

| Key | Type | Default | Range | What it does |
|---|---|---|---|---|
| `enabled` | bool | `false` | — | Connect to the broker and publish. |
| `host` | string | `"127.0.0.1"` | — | Broker host. |
| `port` | int | `1883` | 1–65535 | Broker port. |
| `username` | string | `""` | — | Empty means no credentials are sent. |
| `password_file` | string | `""` | — | Path to a `0600` file holding the password. See §5. |
| `base_topic` | string | `"unidenr8"` | — | Topic root. Leading and trailing `/` are stripped; an empty result falls back to `unidenr8`. |
| `detail` | bool | `false` | — | Publish the schema-2 document instead of the schema-1 one. |
| `home_assistant` | bool | `false` | — | Emit Home Assistant MQTT discovery documents on connect. |
| `tls` | bool | `false` | — | Enable TLS (`paho`'s `tls_set()` with the system trust store). |

Topics, from `mqtt.topic_map()`:

| Topic | Retained | Contents |
|---|---|---|
| `<base>/status` | yes | `online` / `offline`, with a last will so a dead collector says so |
| `<base>/state` | yes | The current state document |
| `<base>/alert` | **no** | One alert transition per message |
| `<base>/availability` | yes | Home Assistant's availability topic |
| `homeassistant/{sensor,binary_sensor}/unidenr8/…/config` | yes | Discovery, only when `home_assistant = true` |

Retention is chosen per topic on purpose. A retained *state* means a dashboard
connecting mid-drive sees something immediately; a retained *alert* would mean
the broker replaying a threat that ended twenty minutes ago to every client that
subscribes, which is worse than silence.

**The 1 Hz telemetry stream is not published.** Only transitions and a slow
heartbeat. MQTT is the first component in this project that puts sustained
Wi-Fi traffic on the same 2.4 GHz front end as the vehicle's RFCOMM link, and a
packet a second for a whole drive is a meaningful load on a Pi Zero 2 W's shared
radio for very little benefit.

**The Home Assistant discovery documents describe an active alert.** The `band`
sensor and the `alerting` binary sensor read `alerts[0]`, whose fields are
UPSTREAM, not OBSERVED. An automation built on them is an automation built on an
R8w capture.

### 3.6 `[feed]` — a local HTTP and server-sent-events feed

| Key | Type | Default | Range | What it does |
|---|---|---|---|---|
| `enabled` | bool | `false` | — | Bind the port and serve. |
| `bind` | string | `"127.0.0.1"` | — | Interface to bind. |
| `port` | int | `8787` | 1–65535 | Port. |
| `detail` | bool | `false` | — | Push the schema-2 document instead of the schema-1 one. |

Standard library only: no framework, no CDN, one embedded page. This runs on a
node that frequently has no route to the internet, and a dashboard that needs to
fetch a script from elsewhere is a dashboard that is blank in a tunnel.

**There is no authentication, and there is no plan to add one**, because there
is no good place to put a credential on a device that boots unattended in a car.
What exists instead is the loopback default and a loud warning when it changes
(§4). Reaching the feed from a phone is meant to go through the private VPN
interface the node already has, not through an open port.

At most `MAX_CLIENTS` (8) simultaneous viewers; further connections are refused
with a plain message. Each client has a bounded backlog that drops its oldest
frame, and a write that takes longer than two seconds costs that client its
stream rather than costing the vehicle its radar data. A port already in use is
reported in `sinks.feed.last_error` and does not stop the collector: radar data
is the product and the dashboard is a convenience.

---

## 4. The settings that change what leaves the machine

This is the section to read before enabling anything. With the defaults the
collector writes two state documents and a lock file, all `0600` inside a
`0700` directory, and sends nothing anywhere.

### 4.1 Where data can go, and what turns each one on

| Destination | Enabled by | Carries | Who can reach it |
|---|---|---|---|
| `<state_dir>/state.json` | always | Schema 1: freshness, health, counters, voltage, GPS-lock boolean, POI-warning boolean, recognised alerts, a short display line | The owning account |
| `<state_dir>/state-v2.json` | `collector.detail` (**on by default**) | Schema 2: all of the above plus the detector's own heading, speed and altitude, per-field confidence grades, queue and loop metrics, open tracks, recent events, and the `vehicle_gnss` branch | The owning account |
| `<state_dir>/history.db` | `history.enabled` | Sessions, every alert transition with the nearest GNSS fix attached, throttled telemetry | The owning account |
| MQTT topics | `mqtt.enabled` | Schema 1, or schema 2 when `mqtt.detail` | The broker, and everything subscribed to it |
| `http://<bind>:<port>/` and `/events` | `feed.enabled` | Schema 1, or schema 2 when `feed.detail` | **Anything that can reach that address. No authentication.** |
| Terminal output | any CLI command | Guarded by `evidence.publish()`, except `config`, `collect`'s own startup lines, and `live --full` | Whoever is at the terminal |

### 4.2 What each setting actually exposes

**`history.enabled`** — creates a durable record on disk of every alert
transition with wall-clock timestamps, and of throttled telemetry. Without it
the project holds only the present: the state document is overwritten and
nothing accumulates. With it, `retain_days` decides how long "what this vehicle
heard, and when" survives; `0` means forever.

**`history.record_detector_motion`** — adds the detector's own `direction_8`,
`speed_mph` and `altitude_ft` to each stored telemetry row. There is no
coordinate in those fields — the detector does not send one (OBSERVED) — but a
timestamped sequence of heading, speed and altitude at the
`telemetry_every_seconds` cadence for a whole drive is a rough trace of that
drive, which is why it is opt-in even though decoding
those fields is not. Note the scope: **this key gates the SQLite columns only.**
The same three values are in `state-v2.json` whenever `collector.detail` is on,
which is the default, and go to the broker and the feed whenever `mqtt.detail`
or `feed.detail` is on.

**`gnss.record_coordinates`** — the single switch that decides whether a real
latitude and longitude may exist anywhere outside the gpsd client's memory. It
gates three things at once: the `lat`/`lon` columns in the history's
`alert_events` and `gnss_fixes` tables (`storage.py`), the `vehicle_gnss` branch
of the schema-2 document (`collector._publish`), and the fix embedded in
`sinks.gnss.fix`. With `mqtt.detail` or `feed.detail` also on, those coordinates
go to the broker or to every feed viewer. The schema-1 document has no GNSS
branch at all and never carries a coordinate, whatever this is set to.

**`mqtt.*`** — the only setting group that sends data off the node by default
when enabled. `host`, `port`, `username`, `password_file` and `tls` decide where
and how; `base_topic` decides what a subscriber has to know to receive it;
`detail` decides whether the broker gets the conservative document or the full
one including the detector's motion fields and, if recording is on, coordinates.
A broker is a fan-out point: everything with a subscription to `<base>/#` sees
what is published, and retained topics are replayed to clients that connect
later.

**`feed.bind`** — the difference between a dashboard only this machine can open
and one every device on the attached network can open, with no password. On a
coffee-shop network, a bind to a routable interface publishes live vehicle
telemetry to strangers. `127.0.0.1` is the default for that reason.

### 4.3 What the publication gate does and does not cover

`evidence.publish()` is the last gate before anything is *printed*. It asks two
questions — does this still contain a Bluetooth or host address, and does it
contain a position — and it **raises** rather than sanitizing, because silently
fixing a bad string would hide the bug that produced it.

It guards most of the CLI's own output: `scan`, `pair`, `identity`, `live`
without `--full`, `inspect` and `history`. Three paths deliberately do not go
through it. `live --full` bypasses it at one call site, with the reason attached
in `cli.py`: the refusal is exactly right for the default surface and exactly
wrong when a person has asked to see the position-adjacent fields on their own
terminal. `uniden-r8 config` prints the effective settings verbatim, so a host
address in the file is printed as written. And the collector's startup lines,
including the warnings, are plain prints.

It does **not** guard the collector's sinks.
`collector.publish_state()` writes both documents directly, and the MQTT and
feed publishers send what they are given. That is deliberate — those files and
those sinks are exactly where the detail is supposed to go — but it means the
configuration flags in §4.2, not the gate, are what decide whether a coordinate
reaches a broker.

Two consequences worth knowing before turning coordinates on:

* `uniden-r8 history events --json` **aborts** with `PublicationRefused` if any
  row carries a coordinate, because the gate walks the decoded JSON and a `lat`
  key holding a number is a position. Verified against the code.
* The same command *without* `--json` prints those coordinates as a table. The
  gate's free-text check looks for a comma-separated decimal-degrees pair, and
  the table separates columns with spaces. See §7.

### 4.4 Two configuration strings that are echoed into published output

`obd.unit` and `obd.device` appear verbatim in the `obd.reason` field of both
published documents — `"<unit> is not active"`, `"<device> is missing"`,
`"<device> is not bound"` — and therefore in anything published to MQTT or the
feed. The reason vocabulary is otherwise fixed precisely because subprocess
output can contain a Bluetooth address. Do not put anything identifying in those
two values.

`collector.adapter` likewise appears as `collector.adapter` in the schema-2
document and in the history's `sessions.adapter` column. `hci0` identifies
nothing; a name chosen to mean something else might.

### 4.5 The warnings list, reproduced

`Config.warnings()` returns strings rather than logging them, so the CLI can
print them and the tests can assert on them. They are printed by `uniden-r8
config` under a `notes:` heading, and by `collect` on stderr as it starts.

**None of these is an error.** They are all things a person may genuinely want,
and all things a person may not have meant.

| # | Fires when | Text |
|---|---|---|
| 1 | `obd.guard` is false | `OBD guard disabled: the collector will hold the detector link without checking the vehicle's RFCOMM binding first` |
| 2 | `feed.enabled` and `feed.bind` is not `127.0.0.1`, `::1` or `localhost` | `feed bound to <bind>: live vehicle telemetry will be reachable from other machines on that network` |
| 3 | `mqtt.enabled`, `tls` is false, and `mqtt.host` is not `127.0.0.1`, `::1` or `localhost` | `MQTT to a remote broker without TLS: state will cross the network in clear text` |
| 4 | `gnss.record_coordinates` is true | `coordinate recording enabled: the history will contain where the vehicle has been` |
| 5 | `history.enabled` and `retain_days == 0` | `history retention disabled: rows are never expired` |

**1 — the OBD guard.** On a node with an OBDLink this is the one warning that is
about someone else's data. With the guard off, the collector will open and hold
a BLE link without first checking that the vehicle's RFCOMM binding is healthy,
and will not release it when that binding goes away. On a node with no OBDLink
the warning is expected and correct: it is telling you that a protection is not
armed, which is true, and `unguarded_obd_probe` records
`reason: "guard disabled by configuration"` in the published state so a reader
of the state file learns the same thing.

**2 — the feed bind.** There is no authentication on the feed. This warning is
the whole of the protection when the default is changed.

**3 — MQTT without TLS.** Username and password are sent in the clear to a
non-loopback broker, and so is everything published. Note that the check is on
the *host*, not on the route: a broker on the same LAN produces this warning, as
it should.

**4 — coordinate recording.** The text understates the effect. Coordinates also
reach `state-v2.json`, and, with `mqtt.detail` or `feed.detail` on, the broker
and the feed. See §4.2, and §7.

**5 — retention disabled.** `retain_days = 0` is a supported choice, not a
mistake, which is why it is a note rather than a refusal. It means a drive
recorded today is still on the card next year.

---

## 5. Why there is a `password_file` and no `password`

There is no inline `password` key, and adding one would be a regression.

A broker password in a configuration file is a password in a backup, in a diff,
in a support paste, and eventually in a commit. Pointing at a file keeps it in
one place, with permissions that mean something, and keeps it out of every
document that quotes the configuration — including `uniden-r8 config`, which
prints the effective settings and would happily print a password if one were a
setting. The *path* is configuration; the password is not.

The loader enforces the permissions rather than trusting them
(`_check_password_file`), and only when `mqtt.enabled` is true:

| Condition | Result |
|---|---|
| `mqtt.enabled = false` | Not checked at all, even if `password_file` is set |
| File does not exist | `[mqtt] password_file <path> does not exist` |
| Any group or other permission bit set | `[mqtt] password_file <path> is readable by group or other (mode 644); chmod 600 it` |
| Mode `0600`, file present | Accepted |

`read_password()` reads the file and strips surrounding whitespace, so a
trailing newline from `echo` is harmless.

```bash
umask 077
printf '%s' 'the-broker-password' > ~/unidenr8/.private/mqtt.pass
chmod 600 ~/unidenr8/.private/mqtt.pass
```

The check is strict about *group and other*, not about ownership: a file the
collector's own account can read is the point. `.private/` is already `0700` and
git-ignored, which makes it the natural home.

---

## 6. Three worked configurations

Each of these has been loaded through `load_config()` and its warnings checked
against what is claimed.

### 6.1 The Hummer node, as deployed

**Who this is for:** the node this project was written on — a Pi Zero 2 W in a
vehicle, sharing one Bluetooth controller with an OBDLink MX+ that is bound to
`/dev/rfcomm0` by a separate `hummer-rfcomm.service`. The OBD link is primary
and the radar detector is the guest. History is on because "what did it hear,
and when" is the whole reason for the project; coordinates are off because the
detector does not supply them (OBSERVED) and no GNSS receiver is attached;
nothing is published off the node, so there is no broker, no feed, and nothing
to secure.

```toml
[collector]
state_dir = "/home/jeremy/unidenr8/.state"
detail = true
heartbeat_seconds = 5.0
queue_size = 256

[obd]
guard = true
unit = "hummer-rfcomm"
device = "/dev/rfcomm0"
interval_seconds = 15.0

[history]
enabled = true
path = "history.db"
retain_days = 30
telemetry_every_seconds = 10.0
record_detector_motion = false

[gnss]
enabled = false

[mqtt]
enabled = false

[feed]
enabled = false
```

Warnings: **none**.

The absolute `state_dir` is deliberate. The systemd unit names it in
`ReadWritePaths=`, and a relative path there would depend on
`WorkingDirectory` — which is exactly the coupling `UNIDEN_R8_CONFIG` exists to
remove. `history.path` stays relative, so the database lands in that same
directory and inside the same `ReadWritePaths=` grant, along with its `-wal` and
`-shm` companions.

### 6.2 A standalone R8 on a bare Pi, with a local dashboard

**Who this is for:** anyone reproducing this work without a vehicle telemetry
link — a detector, a Pi, and no OBDLink at all. The guard must be off, or the
collector will check for a unit and a device node that do not exist, publish
`obd-blocked`, and never connect. The feed is on so a browser on the same
machine, or a phone over an existing private VPN or an SSH tunnel, can watch
alerts as they happen; the e-paper-style state file alone refreshes too slowly
to be a radar display.

```toml
[collector]
state_dir = "/home/pi/unidenr8/.state"
detail = true

[obd]
guard = false

[history]
enabled = true
retain_days = 90
telemetry_every_seconds = 10.0

[gnss]
enabled = false

[mqtt]
enabled = false

[feed]
enabled = true
bind = "127.0.0.1"
port = 8787
detail = true
```

Warnings:

```
! OBD guard disabled: the collector will hold the detector link without checking the vehicle's RFCOMM binding first
```

That warning is expected here and is the correct behaviour: it says a protection
is not armed, which is true. On this node there is nothing for it to protect.

`feed.detail = true` is reasonable *because* `bind` is loopback: the schema-2
document carries the detector's own heading, speed and altitude, and on this
configuration the only thing that can read it is a process on the same machine.
Changing `bind` and leaving `detail = true` publishes a drive trace to the
network; changing `bind` at all produces warning 2. To reach the dashboard from
a phone, forward the port (`ssh -L 8787:127.0.0.1:8787 …`) or use the node's
existing private VPN interface rather than opening the bind.

Anything the dashboard shows for an *active* alert — band, strength, direction,
frequency — is UPSTREAM (R8w), not OBSERVED. Only an all-clear alert packet has
ever been seen from a non-W R8 by this project.

### 6.3 A full home-automation node

**Who this is for:** a node with a GNSS receiver feeding gpsd and a broker that
Home Assistant already watches, where the operator has decided that a record of
where the vehicle has been is worth having. This is the most exposed
configuration in this document, and every one of its warnings should be read as
a statement of fact rather than as noise.

```toml
[collector]
state_dir = "/home/pi/unidenr8/.state"
detail = true
heartbeat_seconds = 5.0

[obd]
guard = false

[history]
enabled = true
retain_days = 365
telemetry_every_seconds = 30.0
record_detector_motion = true

[gnss]
enabled = true
host = "127.0.0.1"
port = 2947
record_coordinates = true
stale_after_seconds = 5.0

[mqtt]
enabled = true
host = "broker.lan"
port = 8883
username = "unidenr8"
password_file = "/home/pi/unidenr8/.private/mqtt.pass"
base_topic = "vehicle/unidenr8"
detail = true
home_assistant = true
tls = true

[feed]
enabled = true
bind = "127.0.0.1"
port = 8787
detail = true
```

Warnings:

```
! OBD guard disabled: the collector will hold the detector link without checking the vehicle's RFCOMM binding first
! coordinate recording enabled: the history will contain where the vehicle has been
```

There is no MQTT warning, because `tls = true`. Remove the TLS line and warning
3 appears, correctly: the credentials and every published document would cross
the network in clear text.

What this configuration actually exposes, stated plainly:

* every alert transition, with a coordinate, to `vehicle/unidenr8/alert`;
* the full schema-2 state document, with the detector's heading, speed and
  altitude and the current fix, retained on `vehicle/unidenr8/state`, replayed
  to every client that subscribes later;
* a year of that on the SD card, in `history.db`.

The broker is a fan-out point. Everything with access to `vehicle/unidenr8/#` —
including anything else on the broker with a wildcard subscription — receives
the vehicle's position at every alert.

`password_file` must exist and be `0600` before the collector starts, or the
configuration will not load at all (§5). `base_topic` with a `/` in it is fine:
only leading and trailing separators are stripped.

Two honesty notes. The Home Assistant entities describe an active alert, whose
fields are UPSTREAM; an automation that reacts to `band = "KA"` is reacting to a
field this project has never seen populated on a non-W R8. And the coordinate in
an alert row comes from gpsd, not from the detector — the detector sends no
latitude or longitude (OBSERVED) — so it is where the *receiver* said the
vehicle was, subject to `gnss.stale_after_seconds`.

---

## 7. Sharp edges, checked against the code

Every item here was verified against the source as it stands. They are recorded
rather than smoothed over, because a reference that quietly agrees with the
documentation instead of the code is worse than no reference.

**`collector.stale_after_seconds` is accepted but not consumed.** It is
type-checked and range-checked at load, and it appears in `uniden-r8 config` and
in the generated example file, but nothing reads
`config.collector.stale_after_seconds`. The `stale` flag in both published
documents is computed against the module constant
`collector.STALE_AFTER_SECONDS`, fixed at 10.0 seconds. Setting the key changes
nothing.

**`collect --state-dir` does not move the history database.** The flag overrides
where `state.json`, `state-v2.json` and `collector.lock` are written, but
`Config.history_path` resolves a relative `history.path` against
`collector.state_dir` from the file. With `--state-dir /tmp/trial` and a
relative history path, the state documents go to `/tmp/trial` and the database
stays in the configured directory. Give `history.path` an absolute path if that
matters.

**`uniden-r8 history --json` refuses to print recorded coordinates.** With
`gnss.record_coordinates` on and coordinates in the rows, the command exits
non-zero with an uncaught `PublicationRefused` traceback rather than a message,
because `evidence.publish()` raises and `_cmd_history` does not catch it. The
same query without `--json` prints the coordinates as a table: the gate's
free-text check looks for a comma-separated decimal pair, and the table is
space-separated. Both behaviours are current; the first is loud in an ugly way
and the second is quiet in a way worth knowing about.

**Warning 4 fires on the flag alone.** `gnss.record_coordinates = true` with
`gnss.enabled = false` still produces the coordinate warning, even though no
coordinate can be obtained. Loud in the safe direction, but it means the warning
is not proof that anything is being recorded.

**Every command except `plan` and `selftest` loads the configuration, including
`config --example`.** A file with a typo therefore blocks the one command that
prints a known-good example. Run it from another directory, or with
`--config` naming a file that does load.

**`history` reads whatever database is at `history_path`, regardless of
`history.enabled`.** The flag controls writing, not reading; querying a database
left over from an earlier configuration works.

**The `gnss_fixes` table is created but never written.** `HistoryWriter` has a
`record_fix` method and nothing calls it, so `uniden-r8 history` always reports
`gnss_fixes 0` however `[gnss]` is configured. GNSS data does reach the history:
`record_alert_event` attaches the nearest fix — mode, speed, course, and, when
`record_coordinates` is on, latitude and longitude — to each alert row. The
empty table is a missing wiring, not a privacy control; do not read `0` there as
evidence that nothing positional was stored.

---

## 8. Which subcommands read which sections

| Command | Loads a config file | Sections read | Radio |
|---|---|---|---|
| `plan` | **no** | none | none |
| `selftest` | **no** | none | none |
| `config` | yes | prints all six | none |
| `history` | yes | `collector.state_dir`, `history.path` (via `Config.history_path`) | none |
| `scan` | yes, then ignores it | none | bounded discovery |
| `pair` | yes, then ignores it | none | discovery plus a bond |
| `identity` | yes, then ignores it | none | one connection |
| `live` | yes, then ignores it | none | one connection |
| `inspect` | yes | `collector.adapter` | one connection |
| `collect` | yes | **all six** | continuous |

Two things this table is saying that are easy to miss.

**`plan` and `selftest` do not load a configuration at all**, by design
(`cli.main`). They are what a reviewer reaches for when something is broken, and
a broken configuration file must not be able to stop someone reading the probe
plan or proving the receive-only properties.

**`scan`, `pair`, `identity` and `live` load and validate the file without
reading a single value from it.** A malformed file therefore fails them with
`configuration: …` and exit 2 even though none of their behaviour depends on it.
That is a consequence of `main()` loading before dispatch, not a deliberate
dependency — but the effect is useful: a configuration error is caught before
any command touches the radio.

Not configurable in TOML at all: the private evidence store, which is the global
`--store` flag (default `.private`), and the bounded window lengths for `scan`,
`identity` and `live`, which are `--seconds` and are clamped in code.

---

## 9. Related documents

| Document | What it holds |
|---|---|
| [`EVIDENCE.md`](EVIDENCE.md) | Every protocol claim, with its source and grade. Read it before trusting any alert field. |
| [`SAFETY.md`](SAFETY.md) | The OBDLink invariants, the no-application-write boundary, and the privacy rules these settings operate inside. |
| [`RUNBOOK.md`](RUNBOOK.md) | The operational procedure: pairing, the bounded trial, reading the state document, installing the unit. |
| [`REQUIREMENTS.md`](REQUIREMENTS.md) | What was asked for. |
