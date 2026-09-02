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
| `/etc/default/hummer-rfcomm` is untouched. | Root-owned; this project never runs `sudo` and never writes outside its own tree. |
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

**Published output is conservative.** Raw payloads go to the owner-only private
store. Published output carries voltage, GPS-fix state, a POI-warning boolean,
and the alert fields a detector exists to report. Heading, speed, altitude and
POI detail are parsed nowhere and published nowhere: they describe where Jeremy
is and has been.

The bounded `live` command itself remains a one-shot diagnostic. The separate
collector below is the only continuous path and its unit is not installed by
this repository.

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

**Bounded reconnection.** Exponential backoff from 5 s to a 300 s cap with ±20%
jitter, reset only after a session that lasted at least 30 s. Without that last
condition a link that connects and immediately drops looks like success and
retries instantly, forever.

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

### Especially private

The POI characteristic is the only one that carries real coordinates: saved
camera locations and user marks — home, work, and the roads Jeremy drives.
If it is ever read, the bytes go to `.private/` and nothing derived from them
is published without a specific decision.

---

## 4. Privilege

This project never runs `sudo`.

The completed discovery, pairing, identity and bounded live capture needed
none: the user-owned venv/tree and BlueZ D-Bus access were sufficient.

If a genuinely missing privileged dependency appears, it is recorded as an
exact minimal command for Jeremy to run — never executed here, and never
worked around. `docs/RUNBOOK.md` holds the current list.
