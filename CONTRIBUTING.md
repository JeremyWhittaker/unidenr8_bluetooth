# Contributing

There are two very different ways to help here, and the first one needs no code at all.

---

## 1. Send an observation from your detector

This is the most valuable contribution to this project, by a wide margin.

Almost everything documented about an **active** radar alert — band, strength, the raw signal, the
frequency-versus-laser-gun-id split, direction, the mute codes — comes from **one R8w on one
firmware version**, plus a decompiled copy of Uniden's app. The detector this project is developed
against is a non-W R8, and it has never produced an alert packet that was not all-clear.

So if you have any R-series unit and you see a real detection, that is evidence nobody has.

**What is useful:**

- the decoded packet text, e.g. `1,00,KA,3,33,33.7850,R,1&0&0&0`
- what your detector's own display said at that moment — band, bars, frequency, direction
- your model and your software revision string (`uniden-r8 identity` prints it)

Open a [hardware observation issue](https://github.com/JeremyWhittaker/unidenr8_bluetooth/issues/new?template=hardware-observation.yml).
The form walks through it.

**What to strip before posting.** Please do not paste a raw capture unedited:

- **Bluetooth addresses.** Yours and anyone else's.
- **Anything from the POI characteristic.** It holds saved camera locations and user marks — home,
  work, the roads you drive. This project never reads it without an explicit `--confirm` for exactly
  that reason.
- **The GPS sub-group** if you would rather not share a heading and altitude. The alert packet
  itself carries no position.

### Getting a capture safely

Use a lawful source that is already radiating. A supermarket's automatic door opener is a K-band
emitter that runs all day; so is a neighbouring car's blind-spot monitor. Approach it, log, and take
what arrives.

Please do **not** build or operate an unlicensed transmitter on a police band, do not speed to
create a capture, and do not run any of this from the driver's seat while moving. A parked logging
session, or a passenger, is the whole procedure.
[`docs/VALIDATION.md`](docs/VALIDATION.md) sets out the test matrix properly.

---

## 2. Contribute code

### Before you start

Read [`docs/HANDOFF.md`](docs/HANDOFF.md). It is short, and it lists the invariants that must not
break along with the test that enforces each one. The important ones:

- **No application-characteristic write path.** Not a disabled one — an absent one. An AST audit of
  the package's own source runs in `selftest` and in CI, and there is a companion test proving that
  audit can still fail. If you have a reason to want a write path, open an issue and make the case
  before writing code; [`docs/SAFETY.md`](docs/SAFETY.md) §2 describes the route.
- **`state.json` stays schema 1.** A consumer requires `schema == 1` exactly. New surface goes into
  `state-v2.json`.
- **No address or coordinate in published output.** `evidence.publish()` refuses both, and a
  repository test scans every committable file with no exception list.
- **Nothing slow on the asyncio event loop.** It carries the BLE subscription, and on BlueZ a client
  that will not drain its D-Bus socket is disconnected rather than merely delayed.

### Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[ble,dev]"
.venv/bin/python -m pytest -q      # 700 tests, no hardware needed
.venv/bin/ruff check .
```

Nothing in the suite needs a radio, a broker, `gpsd` or a network. Every external dependency has an
injection seam — see "Where the injection seams are" in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). **If you add something that talks outside the
process, give it a seam in the same shape.** A test that needs the real thing is a test that will
not be run.

### House style

The code is heavier on prose than most projects, deliberately. Docstrings explain *why* a design is
the way it is, usually by naming the specific bug or hazard it defends against — because most of the
non-obvious decisions here exist because something went wrong, and a future reader who does not know
that will "simplify" it back.

A few concrete conventions:

- Test names are sentences: `test_a_short_alert_still_produces_a_start_and_an_end`.
- A test docstring states the invariant and, where it helps, the consequence of getting it wrong.
- Several controls have a companion test that demonstrates the control catching a deliberate
  violation. A check that cannot fail proves nothing.
- No test file may contain an address-shaped literal. `tests/fixtures.py` builds them from octets.
- Line length 100. `ruff check .` must be clean.

### Adding a protocol claim

If you are documenting something new about the wire format, grade it. The grades are defined at the
top of [`docs/EVIDENCE.md`](docs/EVIDENCE.md) and used consistently across the code and docs:

| Grade | Means |
|---|---|
| `OFFICIAL` | Uniden said it — product page, manual, support article, release note. |
| `OBSERVED` | Seen on real hardware by this project, with the run recorded in the ledger. |
| `UPSTREAM` | Captured on an R8w by `AegisX86/UnidenR8wlink`. |
| `UPSTREAM-UNVERIFIED` | In that upstream, documented there as never tested on hardware. |
| `INFERENCE` | Reasoning. Not observed anywhere. |

"No evidence" is a valid and expected entry. Please do not round a hypothesis up to a fact — the
whole value of this repository over a decompiled app is that it tells you which is which.

---

## Reporting a problem

Ordinary bugs: an issue with what you ran, what happened and what you expected.

If you believe you have found something with security or privacy consequences — a path by which an
address, a coordinate or a POI record could escape into published output, or any way to reach the
detector's command characteristic — please open an issue describing the *class* of problem without a
working exploit, and it will be dealt with promptly.
