# Requirement checklist

Parent-review findings from `.foreman/claude-opus-phase2-review.md`, each with
a status and the evidence for it.

Status values: **implemented** · **blocked** · **deferred** · **not applicable**

---

## 1. Correct the RF / "cannot transmit" overclaims — **implemented**

The earlier drafts said the scan was passive and the package could not
transmit. Both were wrong. BlueZ scans **actively** by default, so an
advertisement is answered with a scan request; and connections, GATT Reads and
notification subscriptions all exchange frames. Subscribing writes a CCCD,
which is a real protocol descriptor write.

The invariant now stated everywhere:

> **No application-characteristic write path.** Nothing writes to the Uniden
> command characteristic; no mute, user-mark, settings or other R/Tach command
> is ever sent. And no OBD/RFCOMM management.

Phase 1's scan is called **bounded advertisement-only discovery** throughout.

| Evidence | Where |
|---|---|
| Corrected wording | `README.md`, `docs/SAFETY.md` §2, `docs/RUNBOOK.md`, `docs/EVIDENCE.md`, `.foreman/PAIR_READY.md`, and the `__init__`, `audit`, `discovery`, `cli` docstrings |
| Regression test | `test_the_docs_do_not_overclaim_radio_silence` — scans docs and source for the banned phrasing, ignoring double-quoted citations of it |
| Proof the control can fail | `test_the_overclaim_detector_still_catches_a_real_overclaim` |
| CCCD stated accurately | `docs/SAFETY.md` §2, `audit.py` docstring, `test_start_notify_is_not_treated_as_an_application_write` |

## 2. Fix the identifier-leak enumeration — **implemented**

`git status` reports only *changed* paths, so once the tree was committed and
clean the scan covered nothing and passed vacuously.

Now `committable_paths()` unions `git ls-files -z` (tracked, whether modified
or not) with `git ls-files -z --others --exclude-standard` (non-ignored
untracked), splitting on NUL so a path containing a newline or quote cannot be
mis-parsed.

| Evidence | Where |
|---|---|
| Rewritten enumeration | `tests/test_repo_hygiene.py::committable_paths` |
| Regression test for the exact bug | `test_the_enumeration_includes_unchanged_tracked_files` — asserts an unchanged tracked file is enumerated and that `git status` omits it |
| Vacuity guard | `test_the_enumeration_is_not_vacuous`, plus an in-test assertion that the path list is non-empty |
| NUL safety | `test_the_enumeration_survives_an_awkward_filename`, against a scratch repo |

Note: the unchanged-tracked-file test **skips** while nothing is committed,
which is the current state. It becomes active on the parent's first commit.

## 3. Bounded connected identity probe — **implemented, not run again**

`src/uniden_r8/identity.py` plus the `identity` subcommand.

| Requirement | Status | Evidence |
|---|---|---|
| Select exactly one strong candidate | implemented | `cli._find_one_strong`; `test_exactly_one_strong_candidate_is_selected` |
| Refuse ambiguity | implemented | `test_two_strong_candidates_are_refused_not_guessed_at` |
| Never print the raw address | implemented | `test_the_ambiguity_message_carries_no_address`, `test_the_report_never_contains_the_address` |
| **Never serialize the address** | implemented | The `.private/detector-address.txt` cache was **removed**; the file was deleted from the node. Address now resolves from BlueZ's own bond state via `pairing.bonded_detector_address()` and lives only in process memory. `test_no_module_writes_an_address_to_disk`, `test_the_private_store_holds_no_address_after_a_scan` |
| Strict connect timeout | implemented | `CONNECT_TIMEOUT_SECONDS` 25 s, `SESSION_CEILING_SECONDS` 60 s; `test_the_session_is_bounded`, `test_a_hung_session_is_cancelled` |
| Pairing only behind an explicit flag | implemented | `pair --confirm`; `test_pairing_refuses_to_act_without_explicit_confirmation` |
| Read only 0x2A24/0x2A29/0x2A26/0x2A28 | implemented | `test_only_the_four_device_information_characteristics_are_read` asserts the exact read list against an injected client |
| Missing service/characteristics are evidence | implemented | `test_a_missing_characteristic_is_evidence_not_a_crash`, `test_a_device_with_no_device_information_service_still_reports` |
| Scrub all exception/output paths | implemented | `test_an_error_message_is_scrubbed`, `test_a_failure_to_connect_is_reported_not_raised` |
| Teardown | implemented | `async with`; `test_teardown_happens_even_when_a_read_raises` |

**Not re-run against hardware.** It ran once before this review, during the
pairing window the parent coordinated; those results are in
`docs/EVIDENCE.md` §6. It has not been run since.

## 4. Bounded receive-only vendor-data phase — **deferred**

Deferred at the parent's explicit instruction, twice: *"Before adding
receive-path files, stop at a green pairing/identity checkpoint"* and *"Do not
create the receive module yet."*

**No receive module exists.** `src/uniden_r8/telemetry.py` was not created.

One piece of groundwork was added to the *gate* rather than to a receive path:
`gatt.LIVE_READ_UUIDS`, `gatt.LIVE_NOTIFY_UUIDS`, `gatt.REQUIRED_LIVE_ATTRIBUTES`
and the two `assert_live_*` helpers. These add **refusals**, not capability —
they narrow the permitted set to telemetry and alert, and make POI and the two
settings characteristics raise even though the wider probe allowlist would
admit them. Flagged here explicitly so the parent can strip them if the
checkpoint is meant to be strictly frozen.

Design settled for the next phase:

* proceed only behind a compatibility gate requiring the connected unit to
  expose the vendor service and the telemetry/alert characteristics — now known
  to be present (§1 below), though payload formats remain unconfirmed;
* GATT-read telemetry/alert, and `start_notify` only on those two allowlisted
  UUIDs, with the CCCD write described accurately as a protocol descriptor
  write;
* treat a parse failure as **expected**: the packet layouts are R8w evidence and
  have not been confirmed on this unit;
* **never** read POI or settings by default — POI carries saved coordinates;
* bound the session and prove teardown;
* raw payloads to the owner-only private store; publish only identifier-safe
  metadata and conservative parsed fields;
* no background service.

## 5. Comprehensive DI/fake tests, lint, docs — **implemented** (for phases 1–3)

212 tests, 1 skipped. Ruff clean across `src` and `tests`.

| Area | Test file |
|---|---|
| Selection ambiguity, bond resolution, no serialization | `test_selection.py` |
| Timeouts, teardown, missing DIS, exact read allowlist, scrubbing | `test_identity.py` |
| Pairing guards: verbs, protected bonds, injection, bond-vs-known set | `test_pairing_guards.py` |
| AST rejection of application-write APIs | `test_gatt_safety.py` |
| Bounded discovery windows, advertisement adaptation | `test_discovery.py` |
| Redaction, salt permissions | `test_privacy.py` |
| Private store modes, publish gate | `test_evidence_store.py` |
| Repository hygiene enumeration | `test_repo_hygiene.py` |
| CLI surface, confirmation gate | `test_cli.py` |

Vendor-UUID compatibility gate and notification allowlist tests are **deferred**
with finding 4 — there is no receive path to test yet.

Docs updated: `README.md`, `docs/SAFETY.md` (new §1a on the pairing guards),
`docs/RUNBOOK.md`, `docs/EVIDENCE.md` (§6 observations), and this file.

## 6. Code-only redeploy, Pi tests/selftest, OBD invariants — **implemented**

No scan, no connect, no pair. See the final summary artifact for the captured
before/after invariants.

---

---

## Checkpoint corrections (`.foreman/checkpoint-corrections.md`)

| # | Correction | Status |
|---|---|---|
| 1 | Characteristic UUIDs confirmed, not just counts | **implemented** — `docs/EVIDENCE.md` §6 gains a per-service UUID table; README, conclusion and open question 4.8 updated. Stated as UUID presence only, not payload format. |
| 2 | Withdraw the continuous-advertising claim and the `AuthenticationCanceled` cause | **implemented** — 6.7 and 6.9 are now graded *not established*; the R8-vs-R8w difference count drops from three to two. |
| 3 | README `no subprocess` / "only discovery uses the radio" | **implemented** — architecture and command list corrected; the `bluetoothctl` guard is named. |
| 4 | SAFETY overclaimed that discovery does not address the detector | **implemented** — now says a scan request may be addressed at link layer, and claims only that no connection is made and no characteristic accessed. |
| 5 | Node baseline said `bleak` absent | **implemented** — records the observed 3.0.2 in the user-owned venv, with the pre-install state noted. |
| 6 | Use the official release notes | **implemented** — v1.35 changed the Bluetooth default Off→On and moved the menus; v1.41 explicitly added R/Tach support; v1.43 applies DB 260702. The "Bluetooth arrived after March 2024" inference is **withdrawn**, and the third-party 1.28/1.35 discussion is no longer relied on. |
| 7 | `ATTOWAVE`/`BTM10` inference; "empty" firmware string; RSSI wording | **implemented** — exact strings marked observed and the module reading marked inference with no primary source; 0x2A26 described as all-`NA` placeholders, not empty; −69 dBm recorded as measured, with the "comfortable" judgement marked inference. |
| 8 | `bonded_devices` must fail closed on any nonzero return | **implemented** — `pairing.bonded_devices` now raises on any nonzero code including 124-with-output. Regression: `test_any_nonzero_bluetoothctl_return_fails_closed` (5 cases), plus tests that a clean zero and a legitimately empty list still pass. |
| 9 | Pairing postconditions must fail loudly | **implemented** — the CLI returns 1 and prints `PAIRING POSTCONDITION FAILED` with the remedial command if the device is left trusted or connected, even when the bond succeeded. Four tests. |
| 10 | Re-run everything, deploy, update statuses and summary | **implemented** — see the final summary artifact. |

---

## Standing constraints — all honoured

| Constraint | Status |
|---|---|
| Do not claim/release the Foreman baton | not applicable — untouched |
| Do not spawn agents or subagents | honoured — all work done directly |
| Do not commit or push | honoured — tree is untracked; parent ships |
| Do not pair again | honoured — no pairing after the review |
| Do not run a connected hardware probe | honoured — no connect since the review |
| Do not scan again | honoured |
| No `sudo` | honoured — nothing needed it |
| Preserve OBD invariants | honoured — rechecked; see final summary |
