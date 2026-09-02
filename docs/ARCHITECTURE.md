# Architecture

How the pieces fit, and why each one is shaped the way it is. Read
[`HANDOFF.md`](HANDOFF.md) first if you are taking this over; read
[`PROTOCOL.md`](PROTOCOL.md) if you want the wire format rather than the program.

---

## The one constraint that explains most of the design

This runs on a Raspberry Pi Zero 2 W with **415 MiB of RAM and one Bluetooth controller**, and that
controller is already carrying the vehicle's OBD-II link over Bluetooth Classic SPP. The radar
detector is a second, unrelated device on the same radio.

Two consequences run through everything:

**The OBD link comes first.** It is the vehicle's telemetry. If holding the detector's link would
compete with it, the detector's link is dropped. That is a check, not a comment — see
[The OBD gate](#the-obd-gate).

**Nothing slow may run on the event loop.** The same asyncio loop carries the BLE subscription, and
bleak's BlueZ backend delivers notifications as D-Bus signals processed on that loop. A blocking
call there does not cost latency; `dbus-daemon` enforces per-connection queue limits by
*disconnecting* a client that will not drain, so a blocking call costs the link. Every component
that could block has been moved off the loop, and a watchdog measures whether that worked.

---

## Module map

| Module | Imports a radio? | What it owns |
|---|---|---|
| `gatt.py` | no | The attribute catalogue with provenance, and three read-only gates. |
| `privacy.py` | no | Address tokenisation, the loopback exemption, the position gate. |
| `evidence.py` | no | The `0700` private store, timestamps, and the publication gate. |
| `audit.py` | no | AST proof that no module can write a characteristic value. |
| `config.py` | no | The TOML configuration and its strict validation. |
| `telemetry.py` | lazily | The wire decoders, and the bounded one-shot `live` session. |
| `events.py` | no | Ingest queue, gap records, alert start/update/end derivation. |
| `storage.py` | no | SQLite history, on its own writer thread. |
| `gnss.py` | no | The `gpsd` client. |
| `feed.py` | no | A standard-library HTTP + server-sent-events server. |
| `mqtt.py` | no | An optional `paho-mqtt` publisher. |
| `discovery.py` | lazily | Bounded advertisement-only scanning. |
| `pairing.py` | no (runs `bluetoothctl`) | The one guarded external-program call. |
| `identity.py` | lazily | Device Information reads. |
| `inspection.py` | lazily | The confirmed settings/POI dump. |
| `collector.py` | lazily | The long-running process that wires all of it together. |
| `cli.py` | lazily | The command surface. |

Nothing at package level imports `bleak`. The gate, redaction and classification must be importable
and testable on a machine with no Bluetooth stack — which is also the honest answer to "is this
deployment healthy" on a node where the radio dependency is not installed yet.

---

## Data flow

```
  detector                                                        consumers
  ────────                                                        ─────────
                    ┌──────────────┐
  BLE notify  ─────▶│   callback   │  copy bytes, stamp two clocks,
                    │  (on loop)   │  take a sequence number, enqueue, return
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐   bounded, drop-oldest
                    │    Ingest    │   + a reserved slot for a Gap record
                    │              │   + a `latest` cell that never drops
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   consumer   │  parse → CollectorState
                    │  (one task)  │  → AlertTracker → transitions
                    └──────┬───────┘
                           │
        ┌──────────────┬───┴────────┬───────────────┬──────────────┐
        ▼              ▼            ▼               ▼              ▼
   state.json    state-v2.json   SQLite         MQTT            SSE feed
   (schema 1)    (schema 2)    (thread)      (paho thread)     (loop, bounded)
```

### Why the callback does so little

The first version of this project updated an in-memory value inside the notification callback and
published it on a five-second timer. An alert that began at second 1 and cleared at second 3 began
and ended between two publications, and every consumer saw an unbroken "clear". A radar integration
that can lose a whole detection is not one.

So the callback now does exactly four things — copy, stamp, number, enqueue — and returns. Anything
else on that path either delays the next packet or, worse, risks the subscription itself.

### Why the sequence number is assigned in the callback

Because a gap has to be *detectable*. A counter incremented where the loss happens tells you a
number; a sequence number assigned before anything can be lost lets a consumer see the
discontinuity directly, without trusting any counter. `Ingest` also emits a `Gap` record carrying
the exact first and last sequence numbers lost, and reserves a queue slot for it so the account of
a loss can never itself be lost.

### Why there are three places a record can be

One structure cannot serve three jobs:

- `Ingest.latest[kind]` — a single always-overwritten cell. The **current view** must survive any
  backlog, so this can never drop.
- the queue — the **record**. Drops its oldest entries when it must, because when a radar
  integration falls behind, the newest snapshot is the one worth having.
- the gap slot — the **account of what was lost**, outside the queue entirely so it cannot be
  evicted by the very overflow it describes, and so it always sorts before everything still queued.

---

## Two layers, and only one of them is lossless

This distinction governs the whole event design.

**The snapshot stream is the record.** What was received is what is stored, and the sequence
numbering proves it. That layer is lossless in the real sense.

**Track identity is inference.** Deciding that the Ka reading in this snapshot is the *same threat*
as the Ka reading in the last one is a guess. The protocol offers nothing to correlate on: field 1,
the alert id, reads `00` in every capture anyone has published. So `AlertTracker` is a derived
*view*, every event it emits is stamped with `TRACKING_ALGORITHM`, and the snapshots it worked from
are what the history keeps. A better matcher can be written later and run against the same recorded
data. If the derivation were the only record, every improvement would invalidate everything already
collected.

### Why the obvious correlation key does not work

The first implementation keyed on band, direction and frequency together. Every one of those is
unstable in exactly the situation that matters most:

- **Direction is geometry, not identity.** Approaching and passing a fixed source gives F, then S,
  then R, for *one* source; a patrol car overtaking gives the reverse. Keying on direction
  manufactures an end and a start at the moment of closest approach — the most interesting instant
  in the encounter.
- **Frequency drifts.** The detector reports to 0.1 MHz and its estimate wanders with signal
  strength. Equality-matching churns, and even 1 MHz rounding is inside the drift for a weak Ka
  signal.
- **Band is not fixed.** K and K POP, Ka and Ka POP, MRCD and MRCT are reclassifications of one
  signal, not different threats.

So matching is a **cost**, not a key. `events.match_cost` scores each open track against each
incoming slot on band family, frequency distance scaled to the band, direction plausibility and
strength continuity; the assignment is greedy over the sorted scores. When two tracks score within
`AMBIGUITY_MARGIN` of each other, the result is labelled `ambiguous` rather than presented as a
clean answer. A greedy mistake costs a split or merged track in the history — labelled — not a lost
detection.

One more rule that is easy to miss: a slot that arrives and **cannot be decoded** holds every open
track open. Absence and failure are different facts, and ending a track on a decode failure would
turn one bad byte into a complete fabricated alert lifecycle, permanently, in the history.

---

## Threading model

There is exactly one asyncio loop, and it runs the BLE link. Everything that can block lives
somewhere else.

| Work | Where it runs | Why |
|---|---|---|
| BLE notifications, reads, subscribe/unsubscribe | the loop | It is the loop's job. |
| The consumer: parse, track, publish | the loop | Pure CPU on tiny payloads; microseconds. |
| The OBD health probe | `asyncio.to_thread` | Two subprocesses with 5 s timeouts each. On the loop that is a potential ten-second stall — long enough for BlueZ to drop the subscription. |
| SQLite writes | a dedicated writer thread | An `fsync` on an SD card can take hundreds of milliseconds. A connection is not portable across threads, so the thread also opens it. |
| MQTT network I/O | paho's own thread (`loop_start`) | `publish()` then only enqueues. This is why paho was chosen over an async client that would share the loop. |
| `gpsd` reads | the loop | `asyncio.open_connection` and `readline`; genuinely non-blocking. |
| The SSE feed | the loop | Every write has a timeout, and a slow client is disconnected rather than waited for. |
| The lag watchdog | the loop | Measuring the loop's own lateness is the whole point. |

### The watchdog

A task sleeps 250 ms in a loop and records how late it actually was. That overshoot is published as
`health.loop_lag_ms` and `health.loop_lag_max_ms`, with an alarm flag past one second.

It is the cheapest diagnostic in the project. Everything that could starve the notification path
shows up here, which turns a problem that would otherwise present as "the link keeps dropping" into
a problem with a cause.

---

## The OBD gate

`make_obd_probe(unit, device)` builds a read-only check that asks three questions:

1. is the configured unit active? — `systemctl is-active <unit>`
2. does the device node exist? — a `Path.exists()`
3. does BlueZ report a binding on it? — `rfcomm`, with no arguments

All three are queries. Nothing here starts, stops, restarts, enables, disables or masks a unit;
nothing binds or releases the device; nothing opens the serial port — opening it is precisely what
this project must never do. Its reason strings come from a fixed vocabulary because subprocess
output can contain a Bluetooth address and the reason is published.

The check runs before the detector link is opened and every `obd.interval_seconds` while it is held.
On failure the collector publishes `obd-blocked`, releases the link promptly, and backs off.

The unit and device are **configuration**, not constants. Hard-coding them is what made the first
version of this project unusable on any node but one. `obd.guard = false` disables the gate entirely
for a host with no OBDLink, and says so in the state document and in `config.warnings()`.

### What the gate cannot see

It asks three *state* questions, and all three stay green while radio contention triples the link's
latency. Coexistence damage is a throughput and latency phenomenon; state probes cannot detect it.
The partial answer is `health.telemetry_interval`: the detector's own cadence was measured at
0.97–1.02 s ([`EVIDENCE.md`](EVIDENCE.md) §7.2), so a widened 95th percentile against that baseline
is a real signal that something is competing for the radio.

---

## Two published documents, and why not one

The e-paper display in the sibling `hummer_obdII` project requires `schema == 1` exactly. Adding the
full decoded surface to that document would break it.

So `state.json` stays schema 1, frozen in shape, and `state-v2.json` sits beside it with everything.
Schema 2 is a superset in content and a separate file in form. The schema-1 file is written *last*,
because a consumer polling the pair is better off seeing a schema-2 document that is momentarily
ahead than one that is momentarily behind: ahead is a reading it has not shown yet, behind is a
reading it has already shown being contradicted.

The split is also the privacy boundary. Schema 1 carries no position of any kind. Schema 2 carries
the detector's own heading, speed and altitude — and coordinates when the operator enables them —
which is why it is `0600` in a `0700` directory and git-ignored, and why
`evidence.publish()` refuses to print it.

---

## The three gates

`gatt.py` holds an allowlist, not a denylist, and three separate entry points into it rather than
one with a mode parameter — because "which operations is *this* command allowed to perform" is a
question each command should answer at its own call sites, and a single gate with a mode is a single
gate somebody can pass the wrong mode to.

| Gate | Admits | Used by |
|---|---|---|
| `assert_readable` / `assert_notifiable` | everything a read-only probe could legitimately touch | the probe plan, `identity` |
| `assert_live_readable` / `assert_live_notifiable` | telemetry and alerts, and nothing else | `live`, `collect` |
| `assert_inspect_readable` | settings 1, settings 2, POI | `inspect` only |

`COMMAND_WRITE_UUID` is on a permanent denylist that sits *above* the allowlist, so a mistake in the
allowlist cannot open the one characteristic that actuates the detector. An unknown UUID raises
rather than being attempted. And `refuse_command()` is the only function in the package that accepts
an R/Tach command string; it raises unconditionally and takes no override argument.

---

## Failure handling, by design

Every one of these is a condition a vehicle will actually produce.

| Failure | What happens |
|---|---|
| Detector switched off | Connect fails, `session failed` is noted with no exception text, backoff with jitter, retry. |
| Link drops mid-session | Detected on the next loop iteration; every open alert track is ended at the last moment it was seen; the link is released; reconnect. |
| OBD link unhealthy | `obd-blocked` published, link released promptly, backoff. Not an error in this project — it is the gate doing its job. |
| SD card full or read-only | The history writer's thread counts errors and keeps going; `sinks.history.healthy` goes false; the collector keeps reading the detector. |
| Power cut mid-write | SQLite WAL with `synchronous=NORMAL` cannot corrupt the database; the last few transactions may be lost. That trade is deliberate. |
| Broker unreachable | `MqttPublisher` counts an error and returns. Nothing propagates. |
| `gpsd` unplugged | The client reconnects with backoff; `fix` goes `None` once the last fix is stale; alerts are still recorded, without a position. |
| Two collectors started | The second refuses on the `flock` rather than silently stealing the link. |
| Clock steps hours (no RTC) | Durations and staleness come from the monotonic clock and are unaffected. Wall-clock stamps are for display only. |
| Queue overflow | Oldest records drop, a `Gap` records exactly which, counters rise, the note says so. |

The rule underneath all of them: **an optional sink can never stop the collector reading the
detector.** Radar data is the product; everything else is a consumer of it.

---

## Where the injection seams are

Every external dependency is injectable, which is why the whole suite runs with no radio, no broker,
no `gpsd` and no network.

| Component | Seam |
|---|---|
| bleak client | `client_factory` on `telemetry.receive`, `collector.run`, `inspection.inspect` |
| OBD probe | `obd_probe` on `collector.run` |
| backoff jitter | `rng` on `collector.run` and `next_backoff` |
| gpsd socket | `connector` on `GnssClient` |
| MQTT client | `client_factory` on `MqttPublisher` |
| signal handlers | `install_signal_handlers=False` |
| trial bound | `duration` on `collector.run` |

If you add a component that talks to something outside the process, give it a seam in the same
shape. A test that needs the real thing is a test that will not be run.
