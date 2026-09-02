## What this changes

<!-- One or two sentences. If it fixes an issue, link it. -->

## Why

<!-- The reasoning, not the diff. If this defends against a specific failure,
     name the failure — that is what the docstrings in this project do, and it
     is what a future reader will need. -->

## Checks

- [ ] `.venv/bin/python -m pytest -q` passes
- [ ] `.venv/bin/ruff check .` is clean
- [ ] `.venv/bin/python -m uniden_r8.cli selftest` still reports all read-only properties hold

## If this touches the protocol

- [ ] Every new claim carries an evidence grade (`OFFICIAL` / `OBSERVED` / `UPSTREAM` /
      `UPSTREAM-UNVERIFIED` / `INFERENCE`) — see [`docs/EVIDENCE.md`](../docs/EVIDENCE.md)
- [ ] Anything decoded from a real detector is recorded in the evidence ledger with the run
- [ ] No hypothesis is stated as a fact

## If this touches published output

- [ ] `state.json` is still schema 1 with its existing key set
- [ ] No Bluetooth address, coordinate, or POI record can reach printed or committed output
- [ ] Nothing slow was added to the asyncio event loop that carries the BLE subscription

<!-- The invariants and the test enforcing each one are listed in docs/HANDOFF.md.
     If you need to change one, say so explicitly here rather than in the diff. -->
