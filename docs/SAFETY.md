# Safety boundary

Two things on this node must not be harmed: the vehicle's OBD telemetry link,
and the radar detector. This document states what is protected, what enforces
it, and where a human decision is required.

---

## 1. The OBDLink invariants

The OBDLink MX+ is primary. Nothing in this project may change any of the
following, and nothing in it can.

| Invariant | Enforced by |
|---|---|
| `hummer-rfcomm.service` is never edited, stopped, started, enabled, disabled or reloaded. | The collector runs only `systemctl is-active hummer-rfcomm` as a query. Its own unit has only an `After=` ordering edge to the OBD unit — never `Wants=`, `Requires=`, restart propagation or a mutating command. |
| `/dev/rfcomm0` is never opened, released, rebound or reconfigured. | Nothing here imports `serial`, `pyserial` or `os.open` on a device node. `/dev/rfcomm0` is `root:dialout`, and this project runs as `jeremy` and never asks for it. |
| `/etc/default/hummer-rfcomm` is untouched. | Root-owned. Nothing in the *package* runs `sudo` or writes outside its own tree; `scripts/install-service.sh` does escalate, for its own unit only, and never touches this file. |
| The OBDLink **bond** and its trust state are untouched. | `pairing.py` discovers the existing bond set at run time and refuses every command naming one of them. See §1a. |
| `/home/jeremy/hummer-obd` and its collector state are untouched. | This project installs to `/home/jeremy/unidenr8`, a separate tree. |
| The detector is treated as BLE/GATT, never RFCOMM. | The only transport is `bleak`, imported lazily inside one function. There is no SPP path. |

## 1a. The one external program, and its guards

`uniden_r8/pairing.py` runs `bluetoothctl`. It is the only module that can make
a persistent external change, and it exists because BlueZ has no other
supported way to complete a pairing agent exchange. The collector separately
runs two fixed read-only health queries, `systemctl is-active` and `rfcomm`
without arguments. Pairing remains the most dangerous code here, because
`bluetoothctl` can reach the OBDLink as easily as the detector.

Four guards, all tested in `tests/test_pairing_guards.py`:

**One binary.** `bluetoothctl`, by absolute verb, never through a shell.
Commands containing `;`, `&`, `|`, backticks, `$`, or a newline are refused
before anything is spawned.

**A small verb allowlist.** Scanning, agent registration, pairing, inspection,
untrusting, disconnecting. Everything else is refused, and these are refused
*permanently*, with a reason each:

| Verb | Why it is banned |
|---|---|
| `remove` | Destroys a bond. The OBDLink's can only be recreated by a person at the vehicle pressing the adapter's button. |
| `trust` | A trusted device is auto-reconnected by BlueZ forever, permanently competing for a radio the vehicle link is using. |
| `power` | Powering the adapter down drops the RFCOMM link. |
| `discoverable`, `pairable` | Change how the node answers strangers. |
| `connect` | Pairing must not leave a link held; the detector stops advertising while connected. |

**The protected set is discovered, not configured.** Before doing anything,
`protected_addresses()` asks BlueZ which devices it already has a **bond**
with, and every one of them is off limits for the rest of the run. There is no
list to maintain and no file to read, so the guard cannot drift: a newly bonded
adapter is protected the moment it exists. If the bond list cannot be read at
all, that is fatal — an empty protected set would silently mean "nothing is
protected".

It is *bonds*, specifically, and not everything BlueZ has heard from. That
distinction was found the hard way: the first implementation protected every
*known* device, and since a discovery scan puts the detector in BlueZ's cache,
it refused to pair with the very device it existed to pair with. It failed
closed, which is the correct direction to fail, but it was still wrong.

**Nothing is left trusted.** After a successful pair the device is untrusted if
BlueZ set the flag itself, and disconnected so BlueZ releases the link.

### Pairing is not read-only

Pairing is a persistent change to the node's BlueZ state. It is deliberately
outside the read-only boundary and cannot happen by accident: it has its own
subcommand, and that subcommand exits non-zero without an explicit `--confirm`.
A pairing exchange does put frames on the air — it is link-layer security
negotiation — but it carries no application command on the command
characteristic, which is the boundary this project defends.

The agent answers a *confirmation* prompt automatically. That is not a decision
made on Jeremy's behalf: the detector displays the same exchange on its own
screen, and the human action authorising all of it is pressing **BT Pairing**
on the unit. A passkey *entry* prompt is different — there is nothing to type on
a radar detector — and is reported rather than guessed at.

---

### Why the bounded LE scan does not mutate RFCOMM state

`hci0` is one radio serving both. An LE discovery session and an established
BR/EDR link coexist — the controller interleaves them — but a *long* discovery
session degrades the link's share of the radio. That is the real risk, and it
is a duration risk, not a state risk: a scan changes no bond, no trust flag and
no binding.

So the scan is bounded, in three independent places, each sufficient on its own:

1. `discovery.bounded_seconds()` clamps every requested window to
   3–60 s before it can reach anything;
2. the clamped window is what is handed to the scanner; and
3. the whole scan sits inside `asyncio.wait_for(...)` with a ceiling of
   window + 5 s, so a scanner that hangs is cancelled rather than awaited.

`tests/test_discovery.py` proves all three, including that the ceiling actually
fires rather than merely being configured.

The OBDLink also cannot be mistaken for the detector: it is a BR/EDR Serial
Port Profile device and does not appear in an LE scan at all. Belt and braces,
`discovery.classify()` grades its advertised name as `other`, and
`test_the_obdlink_is_never_a_candidate` holds that.

---

## 2. The detector: no application-write path, by absence

### The honest invariant

This project is a radio, and radios transmit. Being precise about that matters
more than sounding safe:

* BlueZ performs **active** scanning by default, so discovery answers an
  advertisement with a scan request — which is addressed to that device at the
  link layer — and the device may send a scan response.
* A connection, a GATT Read and a notification subscription all exchange
  link-layer and ATT frames.
* Subscribing writes a Client Characteristic Configuration Descriptor. That is
  a **protocol descriptor write**, not a metaphor.

So "this project never transmits" would be false, and earlier drafts of this
document said it. What is true, provable and actually protective:

> **There is no application-characteristic write path.** Nothing here writes to
> the Uniden command characteristic. No mute, unmute, mute-memory, user-mark,
> red-light-camera-delete, settings or any other R/Tach command is ever sent.
> And nothing here manages OBD or RFCOMM state.

That is the invariant the AST audit proves and the tests pin. Everything below
is scoped to it.

The default probe has **no application write path**. Not a disabled one — an
absent one.

### What is permitted

* Bounded advertisement-only discovery. Active scanning, as BlueZ does by
  default, so a scan request may be addressed to the detector at the link
  layer. What discovery does **not** do is make a connection or access any
  characteristic.
* Connecting, and the service discovery BlueZ performs on connect.
* **GATT Read** of the characteristics in `gatt.READABLE_UUIDS`.
* **Subscribing** to the characteristics in `gatt.NOTIFY_UUIDS`.

The scan uses only the first operation. Identity uses standard Device
Information reads. The bounded live path uses only telemetry/alert reads and
subscriptions through its narrower allowlist.

### What is refused

* Any write to `2c86686a-53dc-25b3-0c4a-f0e10c8dee20`, the command
  characteristic. It is in `gatt.FORBIDDEN_UUIDS` and both gates raise
  `WriteRefused` for it, in any letter case or spacing.
* Any application command: mute, unmute, mute memory, user mark, red light
  camera delete, or anything else. `gatt.refuse_command()` raises
  unconditionally and takes no override argument.
* Anything not in the catalogue. Unknown UUIDs raise
  `UnknownCharacteristic` rather than being attempted.

### GATT Read versus an application command

The assignment draws this line and it is the right line. Calling something a
"read" is not enough:

* A **GATT Read** is an ATT-layer request for a value the peripheral already
  holds. Nothing of the caller's reaches the application.
* An **application command** is caller-supplied bytes written to a
  characteristic the firmware interprets — `BTreqMUTE:1` on `2c86686a-…`. That
  is a write however it is described.

Firmware and software version are Bluetooth SIG Device Information
characteristics 0x2A26 and 0x2A28. Reading them is the first kind. Upstream
gets them that way (`r8link/client.py`, `read_device_info`), so
**identity does not require an application write**, and the assignment's
escalation clause does not fire. See `docs/EVIDENCE.md` §3.

Subscribing writes a Client Characteristic Configuration Descriptor. That is a
**protocol descriptor write** and this document does not pretend otherwise; it
is permitted deliberately. The CCCD is a per-connection switch belonging to the
client, setting it is how a client says "send me updates", and it cannot carry
an application command to the detector. `uniden_r8/audit.py` records why
`start_notify` is not treated as an application-write path, while
`write_gatt_descriptor` — the arbitrary-descriptor write — is flagged like any
other.

### How absence is proven, not asserted

`uniden_r8/audit.py` parses the AST of every module in the package and reports
any reference to a value-writing bleak API, by attribute, bare name, or
`getattr` with a literal. It proves no module can write a characteristic value;
it does not, and cannot, make the project radio-silent. A substring search would be useless here — these
safety modules discuss `write_gatt_char` in prose — so the check parses instead.

It runs three ways:

* `uniden-r8 selftest`, on the node, before anything else;
* `test_the_package_contains_no_application_write_path`, in CI or by hand; and
* `test_the_audit_actually_catches_a_write`, which proves the control can fail,
  because a check that cannot fail proves nothing.

### The receive path

`uniden_r8/telemetry.py` is the live-data phase, and it narrows the boundary
rather than widening it.

**A compatibility gate runs before anything is accessed.** The vendor UUIDs
were documented on an R8w. Service discovery has confirmed them on Jeremy's R8,
but the module still checks the connected device each time: a firmware update
could move the table, and a gate that is only correct today is not a gate. If a
required attribute is absent it stops, reads nothing, and reports what was
missing.

**A narrower allowlist than the probe's.** `gatt.LIVE_READ_UUIDS` and
`gatt.LIVE_NOTIFY_UUIDS` contain telemetry and alert, and nothing else.
`assert_live_readable` refuses POI and both settings characteristics **even
though the wider probe allowlist would admit them** — POI holds saved camera and
user-mark coordinates, settings hold device configuration, and neither is needed
to pull live data. The gate is called per-UUID at the call site, so editing the
loop cannot smuggle one in.

**Subscribing writes a CCCD.** That is a protocol descriptor write and it is
the only write of any kind this module performs. It is how a GATT client says
"send me updates"; it carries no application command. The invariant remains *no
application-characteristic payload write*, not *no RF writes*.

**Bounded, with deterministic teardown.** The window is clamped to 5–120 s and
the whole session sits under `window + 25 s connect timeout + 10 s cleanup`.
Teardown unsubscribes
every subscription that was actually established — including when the other one
failed or the outer ceiling cancels the session — and `async with` releases the
link even if a read raises or the gate refuses. Partial raw evidence is saved
on cancellation. A held link matters: the detector stops advertising while
connected.

**Notification handlers never raise.** An exception on bleak's notification
path disappears into the BLE machinery and takes the subscription with it, so
both handlers swallow everything.

**Compatibility is evidence-graded.** The telemetry shape is now observed on
this R8, while active-alert fields remain R8w evidence. Any packet that does
not fit a recognised shape is recorded as unparsed, with its raw bytes kept,
rather than reflected into public output. Separate telemetry and alert
unparsed counters make that visible instead of silent.

**Published output is conservative by default.** Raw payloads go to the
owner-only private store. The default output carries voltage, GPS-fix state, a
POI-warning boolean, and the alert fields a detector exists to report.

Heading, speed, altitude and POI detail *are* decoded now — that changed with
this build, and the earlier wording that they were "parsed nowhere" is
withdrawn. What replaced it is a boundary rather than an absence: they appear
only in the schema-2 document and the local history, both `0600` in a `0700`
git-ignored directory; they reach a printed terminal only on an explicit
`live --full`; they reach a broker or the feed only on an explicit
`detail = true`; they enter the history only on
`history.record_detector_motion`; and `evidence.publish()` refuses any document
carrying a coordinate. See §3, "Position is now a category of its own".

### One command has been sent to the detector, from outside this package

On 2026-09-03, at the owner's explicit and repeated instruction, a single
`BTreqUMRK:1` was written to the command characteristic by a **standalone script
that is not part of this package and is not in this repository**. It created a
user mark. `docs/EVIDENCE.md` §14 records the result.

This section exists so that fact is not discoverable only from a commit message.

**The invariant below is unchanged and still true.** Nothing in
`src/uniden_r8/` writes an application value to a vendor characteristic; the AST
audit still proves it; `selftest` and CI still run that proof. Everything this
project *installs and runs* remains read-only.

The experiment was deliberately kept outside the package rather than added to
it, because the ordering matters: nobody had ever sent that command to hardware
on any model, so establishing that it does anything at all is a different and
much cheaper question than deciding how a project should expose it. The second
question is now worth asking; it had not been before.

If the capability is ever brought inside, this project's standard for it is
already written down and does not change: a single reversible command, an
explicit confirmation flag, the response characteristic captured for ACK/NAK, a
mandatory before-and-after readback, and a written rationale here. Not a feature
flag.

The bounded `live` command itself remains a one-shot diagnostic. The separate
collector below is the only continuous path. Its unit is installed by
`scripts/install-service.sh`, which escalates for a short, enumerated list —
writing one unit file, reloading the manager, and starting one unit — and
touches no other unit. It never restarts, edits, enables or disables
`hummer-rfcomm`, `bluetooth`, or the display service.

### The event path

`uniden_r8/events.py` was added to fix a correctness bug, and it introduced one
new rule the rest of the project now depends on.

**A notification callback does four things and returns.** Copy the bytes, stamp
the monotonic and wall clocks, take a sequence number, hand it to a bounded
queue. Nothing else. Parsing, publishing and disk I/O happen on a separate task,
because bleak's BlueZ backend delivers notifications as D-Bus signals processed
on the event loop, and `dbus-daemon` disconnects a client that will not drain its
socket. A blocking call on that path does not cost latency; it costs the link.

**Nothing slow may run on the event loop, and that is measured.** The OBD probe
runs two subprocesses with five-second timeouts each and is dispatched to a
thread; the SQLite writer owns a thread; MQTT uses paho's own network thread;
the `gpsd` and feed clients are non-blocking asyncio with per-write timeouts. A
watchdog sleeps 250 ms in a loop and publishes how late it actually was, as
`health.loop_lag_ms`. `test_the_obd_probe_never_runs_on_the_event_loop` pins the
first of those.

**A drop is visible.** The queue is bounded and drops its oldest entries, which
is the right policy when the newest snapshot is the one that matters — but a
silent drop is a lie. Sequence numbers are assigned in the callback, before
anything can be lost, and the queue emits a `Gap` record naming the exact first
and last sequence numbers lost. The gap holds a reserved slot outside the queue
so the account of a loss cannot be evicted by the overflow it describes.

**Track identity is inference, and it is labelled.** Correlating an alert across
snapshots is a guess: the protocol's alert-id field reads `00` in every capture
anyone has published. So every derived event carries the matcher's version and an
`ambiguous` flag, and the snapshots the matcher worked from are what the history
stores. A derivation presented as a record would make every future improvement
invalidate everything already collected.

### The optional sinks

Four things were added that can send data somewhere. Each is **off by default**,
each fails independently, and none of them can stop the collector reading the
detector.

| Sink | Default | What it exposes | Where it goes |
|---|---|---|---|
| SQLite history | off | alert transitions, throttled telemetry; motion and coordinates only on their own opt-ins | one file, `0600`, in the `0700` state directory, git-ignored |
| `gpsd` client | off | nothing outward; it *reads* | a loopback socket |
| MQTT | off | the state document and alert transitions | a broker, i.e. off the machine |
| HTTP/SSE feed | off | the state document and alert transitions | a loopback port by default |

Two of those deserve their own note.

**MQTT and the feed are the first components here that add sustained radio
load.** Everything before them was a bounded window; a broker connection and a
held SSE stream are traffic for the whole drive, on the same 2.4 GHz front end as
the vehicle's RFCOMM link. The OBD health probe cannot see that: it asks three
*state* questions and all three stay green while contention triples the link's
latency. So MQTT publishes alert transitions plus a slow heartbeat and never the
1 Hz telemetry stream, at QoS 0; the feed binds to loopback; and
`docs/RUNBOOK.md` makes a with-and-without comparison trial a gate before either
runs on a drive. `health.telemetry_interval` against the measured 0.97–1.02 s
baseline is the signal to watch.

**`config.Config.warnings()` says the risky things out loud.** Disabling the OBD
guard, binding the feed to a non-loopback address, publishing to a remote broker
without TLS, enabling coordinate recording, and disabling retention each produce
a warning printed when the collector starts. They are all legal configurations,
and all things a person may not have meant.

### The inspection command

`uniden_r8/inspection.py` is the only code here that deliberately reads the
detector's saved coordinates, and it is shaped by that.

It requires `--confirm`, checked at the command *before* anything reaches the
radio, so a refusal does not first open a link. It reads exactly settings 1,
settings 2 and the POI database, through `gatt.assert_inspect_readable` — a third
gate, narrower than the probe's and wider than the live path's. Three gates
rather than one with a mode parameter, because a single gate with a mode is a
single gate somebody can pass the wrong mode to. The bytes go to the private
store and nowhere else; the printed summary carries lengths, byte histograms and
record-boundary *candidates*, and no device bytes at all.

**It decodes nothing.** Upstream published a candidate POI layout and also
recorded that the only POI database it ever read was empty. A parser built on
that could appear to succeed on bytes nobody has ever seen, and its output would
be somebody's home address. A wrong coordinate printed confidently is worse than
no coordinate. The same reasoning applies, more weakly, to the settings blocks:
what this produces is a *snapshot that can be diffed*, and `docs/VALIDATION.md`
sets out how to turn one physical menu change into one understood byte.

### The background collector

`uniden_r8/collector.py` is the first thing here meant to run for hours, and
length is what makes it different: a 30-second window that shares the radio is
a nuisance, an unattended process that does it all night is a hazard. So its
design starts from the OBDLink.

**The OBDLink is checked, not assumed.** An injected read-only probe runs
before the detector link opens and every 15 s while it is held. It asks three
questions — is `hummer-rfcomm` active, does `/dev/rfcomm0` exist, does BlueZ
report a binding on it — using `systemctl is-active` and `rfcomm` as *queries*.
If the answer is no, the detector link is released promptly, an `obd-blocked`
state is published, and the collector backs off rather than retrying into a
busy radio.

It never starts, stops, restarts, enables, disables or masks a unit; never
binds or releases `/dev/rfcomm0`; never opens the serial device; and never
reconfigures the Bluetooth controller or service.
`test_the_collector_never_mutates_a_service_or_rfcomm` checks every command
vector in the module against a list of mutating verbs — vectors rather than all
strings, because prose legitimately discusses stopping things.

**One instance only.** A `flock` on the state directory makes a second
collector refuse rather than silently steal the link, and it is released on
every exit path.

**Bounded reconnection.** Exponential backoff from 5 s to a 60 s cap with ±20%
jitter, reset only after a session that lasted at least 30 s. Without that last
condition a link that connects and immediately drops looks like success and
retries instantly, forever.

The cap was 300 s and is now 60 s, which is a deliberate loosening. A Pi left
powered while the vehicle is parked spends the night failing to connect, so the
backoff reaches its ceiling — and then the engine starts, the detector powers
up, and the collector waits up to another five minutes. The first minutes of a
drive are not interchangeable with any other minutes. Connect attempts only
happen while the OBD guard reports the vehicle's link healthy, each one is
bounded by a 25 s connect timeout, and `docs/EVIDENCE.md` §8 measured a
five-minute *held* BLE session leaving the RFCOMM binding undisturbed — a failed
connect is strictly less than that.

**The trial deadline is enforced twice:** inside the streaming loop and by an
outer asyncio ceiling around the complete BLE session. The outer ceiling also
cancels a connect/read/subscribe call that never returns. Nonpositive, NaN and
infinite CLI durations are rejected before bond lookup or link construction.
A healthy session once ignored the duration entirely; a test caught that real
bug by hanging, and the hung-GATT regression now pins the stronger boundary.

**Raw packets are not retained.** The one-shot `live` command remains the
explicit private diagnostic capture. A long-running process that accumulated
payloads would grow without bound on a 415 MiB node and turn every crash into a
disclosure question.

**Nothing published can carry an identifier.** The state document has no
address, no token, no raw payload, no heading, speed or altitude, no POI detail,
and no exception text — BlueZ error strings contain addresses, so failures are
recorded as fixed phrases like `session failed` rather than as messages. Band
and direction are mapped through an allowlist, so an unrecognised value is
published as `unknown` instead of echoing an arbitrary string from the device.

**No unit is installed.** `systemd/unidenr8-collector.service` is a template
for deliberate manual installation. It orders itself `After=hummer-rfcomm` but
deliberately does **not** `Want` or `Require` it: a unit that can start the
vehicle's RFCOMM binding is a unit that can interfere with it, and the
collector's own health check is the real protection. Its `[Install]` section
only makes a later, explicit `systemctl enable` possible; nothing here invokes
that command.

**The display is a separate, untrusted consumer.** The sibling Hummer display
requires schema 1, a recent timestamp and typed/allowlisted fields. It builds
its own short line and ignores `display_line`, notes, reasons, identifiers,
positions and raw data. Missing or invalid state falls back to the existing
Tailscale line, while the OBD line remains unchanged.

### If a write is ever wanted

It is a change to the project's purpose, not a feature flag. The route is:
the parent obtains Jeremy's explicit approval for one exact, proven, read-only
query; the exact bytes, their provenance, the expected response and the risk go
into a document for review; and only then does anyone discuss code. Adding an
`allow_writes` parameter would be reverted —
`test_no_module_in_the_package_exposes_an_allow_writes_switch` fails on it.

---

## 3. Privacy

Everything observable over the air is an identifier.

**Nothing raw is ever published.** Not to stdout, a log, a document, a commit,
or a status summary. Two destinations exist:

* **`.private/`** — mode `0700`, files `0600`, git-ignored. Raw material lives
  here and nowhere else, along with `redaction.salt`, which is exactly as
  sensitive as the material it protects and is stored with it for that reason.
* **Sanitized output** — Bluetooth addresses become salted, truncated
  HMAC-SHA-256 tokens (`ble:9f3c1a2b4d5e`); host addresses become
  `<host-redacted>`; advertised names keep the model prefix and tokenise the
  address fragment (`R8W@nam:…`), because the fragment is address-derived.

Tokens are stable within one install, so two scans can be compared, and
useless across installs, because the salt is per-install and 256 bits.

`evidence.publish()` is the last gate. It re-checks rendered text and
**raises** rather than sanitizing: silently fixing a bad string would hide the
bug that produced it.

Devices that are not candidates are counted but never listed. The neighbours'
phones are not this project's business.

`tests/test_repo_hygiene.py` scans every file git would commit — tracked and
untracked — for an address pattern. It has no exception list, which is why
`tests/fixtures.py` assembles test addresses from octets instead of writing
them out.

### Position is now a category of its own

Until an external GNSS source existed, nothing here could produce a latitude, so
the publication gate only knew about Bluetooth and host addresses. A gate that
refuses a MAC address while printing where the vehicle was would be defending the
wrong thing, so two questions are now asked at that boundary:

* `privacy.looks_like_identifier` — does this still contain an address?
* `privacy.looks_like_position` — does this contain somewhere the vehicle has
  been?

The second walks a decoded document by key rather than pattern-matching its text,
so `{"lat": 33.4}` is caught and `{"voltage": 33.4}` is not, and it also catches a
decimal-degrees pair in prose. It is deliberately willing to be wrong in the
cautious direction: refusing to publish a document that merely looks positional
costs a developer five minutes, and the opposite mistake is permanent.

Three grades of data, three destinations:

| Data | May appear in |
|---|---|
| voltage, GPS-fix boolean, band, strength, frequency, direction, mute | anywhere: `state.json`, a display, a broker, this repository |
| detector heading, speed, altitude; POI warning detail | `state-v2.json` and the history, both `0600` in a `0700` git-ignored directory; a broker or the feed only with `detail = true` |
| coordinates, POI database bytes, raw packets, addresses | the `0700` private store, or the history with `record_coordinates` explicitly on. Never printed, never committed. |

`.gitignore` covers `.private/`, `.state/`, and every `*.db`/`*.sqlite` with their
`-wal` and `-shm` companions. That last detail matters more than it looks: the
`-wal` file holds the most recently written rows, SQLite creates it lazily at the
first write — long after the umask was closed around `connect()` — and
`storage.History._secure()` exists to tighten it afterwards. A history that is
`0600` with a group-readable journal beside it is not a private history.

### Loopback is not an identifier

`127.0.0.1` matches the IPv4 pattern and identifies nobody: every machine has one,
and a configuration file that cannot write it is a configuration file whose
documentation has to talk around its own defaults.
`privacy.is_non_identifying_host` names the exact exemption — loopback, the
unspecified address, the broadcast address — and nothing else. A private-range
address is still an identifier, because one plus a little context identifies a
network. The exemption lives in the redaction module, where a reviewer reading the
rules will see it, and not as an exception list inside the hygiene test, where it
would be invisible.

### Especially private

The POI characteristic is the only one that carries real coordinates: saved
camera locations and user marks — home, work, and the roads Jeremy drives.
The `inspect` command reads it only on an explicit `--confirm`, the bytes go to
`.private/`, and nothing derived from them is decoded or published. See "The
inspection command" above.

---

## 4. Privilege

**The package never runs `sudo`.** Discovery, pairing, identity, the bounded
live capture, the collector, `survey` and `poi-diff` all run as an ordinary
user.

**`scripts/install-service.sh` does**, and it is the only thing here that does.
On the default path it escalates five times — install the unit file,
`daemon-reload`, `enable`, `reset-failed`, `restart` — and with
`--allow-user-control` it also validates and installs one file in
`/etc/sudoers.d`. Each is named individually below.

The completed discovery, pairing, identity and bounded live capture needed
none: the user-owned venv/tree and BlueZ D-Bus access were sufficient.

If a genuinely missing privileged dependency appears, it is recorded as an
exact minimal command for Jeremy to run — never executed here, and never
worked around. `docs/RUNBOOK.md` holds the current list.
