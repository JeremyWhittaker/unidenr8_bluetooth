# Evidence ledger

Every factual claim this project makes, with its source and its grade.
Nothing here is asserted from memory, and nothing is asserted about Jeremy's
detector that his detector has not said.

Grades, used here and in `uniden_r8.gatt.Evidence`:

| Grade | Means |
|---|---|
| **OFFICIAL** | Uniden said it: product page, owner's manual, or support article. |
| **UPSTREAM** | Read out of `AegisX86/UnidenR8wlink` @ `9072bc2f`, confirmed there against a real **R8w**. |
| **UPSTREAM-UNVERIFIED** | In that upstream, but documented there as untested on any hardware. |
| **INFERENCE** | This project's reasoning. Not observed. |
| **OBSERVED** | Seen on Jeremy's own R8 by this project. See §6. |

---

## 1. The model question

Jeremy's detector is a Uniden **R8**. The upstream library targets the
**R8w**. These are different products, and the difference is not cosmetic.

| # | Claim | Grade | Source |
|---|---|---|---|
| 1.1 | The R8 has Bluetooth, used by Uniden's R/Tach app. | OFFICIAL | [uniden.com/products/r8](https://uniden.com/products/r8): "Bluetooth connection allows for seamless connectivity to your phone, allowing you to change settings on the fly with Uniden's R/Tach app." |
| 1.2 | The R8 has **no** Wi-Fi; the R8w is the Wi-Fi model. | OFFICIAL | Same page: "For drivers who want wireless update functionality, Uniden also offers the R8W, a Wi-Fi–enabled version with automatic update capabilities." |
| 1.3 | Uniden's own R/Tach start-up guide covers non-W models. | OFFICIAL | [support.uniden.com article 153000224590](https://support.uniden.com/support/solutions/articles/153000224590-r-tach-application-start-up-guide-r4-r8-r9), titled for "R4, R8, R9" and also served as "R4w R8w R9w and Non-w". |
| 1.4 | The pairing action on the detector is a menu screen labelled **BT Pairing**. | OFFICIAL | Same article: "scroll over to the screen labeled 'BT Pairing' and press the menu key to select it". |
| 1.5 | A successful pairing shows a **B** on the detector display. | OFFICIAL | Same article: "you will see the blue letter 'B' on your display". |
| 1.6 | **The R8 owner's manual documents no Bluetooth at all.** | OFFICIAL | `R8om.pdf`, Issue 3, March 2024 (from [uniden.info downloads](https://www.uniden.info/download/index.cfm?s=r8)). A full-text search of all 50 pages returns zero matches for "Bluetooth", "BT Pairing", "R/Tach" or "pairing". Its menu table has no `BT/WiFi` and no `BT Pairing` row. |
| 1.7 | The R8w owner's manual **does** document them. | OFFICIAL | `R8Wom.pdf`, Issue 1, July 2024. Menu rows `BT/WiFi` ("Turns Bluetooth/WiFi on and off", default **On**) and `BT Pairing Mode` ("Initiates when BT is pairing with the R8w", shown only "If BT/WiFi mode On"); section "Establishing a Bluetooth Connection" gives the four-step procedure. |
| 1.8 | Bluetooth on the R8 is **older than firmware 1.35**, which changed its *default* rather than introducing it. | OFFICIAL | v1.35 release note (2024-10-29): "Factory default of Bluetooth is changed from Off to On." A default cannot be changed for a feature that does not exist. |
| 1.9 | Firmware **1.35** also moved the Bluetooth menus to where they are now. | OFFICIAL | Same release note: "The Bluetooth On/Off and BT Pairing are moved to be before GPS On/Off menu in the Menu." |
| 1.10 | **R/Tach app support was added in firmware 1.41.** | OFFICIAL | v1.41 release note (2026-02-03): "Added support for the Laser I/F, Junction Box, associated menus, Installer Test Mode, and R/Tach app." |

Note what 1.8–1.10 replace. An earlier draft inferred from the manual's
silence that Bluetooth "arrived after March 2024". That inference is withdrawn:
Uniden's own release notes show Bluetooth predating 1.35, and the manual's
silence only tells us the manual was not revised. Absence of documentation is
not evidence of absence of a feature.

### What 1.6 means in practice

The assignment asked for the firmware check at `Menu > System > Firmware Info`.
**The R8 has no such path.** Its manual documents one version screen:

> `S/W version/DSP Version/GPS Version` — "Displays the latest firmware, DSP,
> and GPS versions."

reached by pressing `MENU` and then `+` to cycle to it. There is no "System"
submenu and no "Firmware Info" item. The R8w's equivalent row is `S/W version`,
"Displays the latest firmware version for UI, DSP, GPS, Sound, and **BT/WiFi**"
— the presence of a BT/WiFi line in that display is itself a Bluetooth
indicator. `docs/RUNBOOK.md` uses the documented R8 path, not the assignment's.

---

## 2. Firmware and the official update tool

| # | Claim | Grade | Source |
|---|---|---|---|
| 2.1 | The current R8 firmware is **V1.43**, released 2026-07-10. | OFFICIAL | [uniden.info downloads for R8](https://www.uniden.info/download/index.cfm?s=r8). Prior releases listed there: 1.41, 1.39, 1.38, 1.36, 1.35, 1.28, 1.27, 1.26, 1.25. |
| 2.2 | The official PC tool is **"Uniden R Series Tool"**, current v2.25 (Windows, 2026-05-19); a Mac build exists (v2.22_MAC). | OFFICIAL | Same page, "Update Tools". |
| 2.3 | The R8 updates over **USB**, not over the air. | OFFICIAL | [support.uniden.com article 153000192832](https://support.uniden.com/support/solutions/articles/153000192832-updating-an-r-series-radar-detector): "plug in your radar via the provided USB cable that came with your unit"; select the UPDATE tab, click Start Update, wait for "Update Completed!". A `CP210x_Windows_Drivers` package is listed alongside the tool. |
| 2.4 | The detector's display stays dark during an update. | OFFICIAL | Same article: "The display on the radar will not light up during this process as this port is strictly for data transfers." |
| 2.5 | **R/Tach support was added in firmware 1.41**, per Uniden's own release note. | OFFICIAL | v1.41 (2026-02-03) release note on the downloads page. This supersedes the earlier third-party discussion of a 1.28-vs-1.35 minimum (Vortex Radar, rdforum), which is no longer relied on: an official release note naming the feature is stronger evidence than a community report of when it seemed to start working. |
| 2.6 | v1.43's own change is the database: "New GPS DB 260702 has been applied." | OFFICIAL | v1.43 (2026-07-10) release note. |

---

## 3. The BLE protocol, as known from the R8w

Source for all of §3: `https://github.com/AegisX86/UnidenR8wlink`, commit
`9072bc2f2ad3a02d3a624ecaff6e976b62aa856a` ("Add r8link-pair console script",
2026-08-01), MIT licensed, files `r8link/client.py`, `r8link/protocol.py`,
`r8link/pair.py` and `PROTOCOL.md`. The author states the work came from
decompiling the R/Tach Android app (`com.uniden.rtach`) with JADX plus live
captures from **one R8w on one firmware version**.

The author's unit reports firmware `R8W/NA/NA/NA/NA/20251002/NA/NA` and
software `R8W/127/109/122/103/20251002/999/999/120`.

### Services

| UUID | Role | Grade |
|---|---|---|
| `18424398-7cbc-11e9-8f9e-2a86e4085a59` | Uniden data service | UPSTREAM |
| `1842467c-7cbc-11e9-8f9e-2a86e4085a59` | Uniden command service | UPSTREAM |
| `0000180a-0000-1000-8000-00805f9b34fb` | Device Information (Bluetooth SIG) | OFFICIAL (SIG) |

### Characteristics

| UUID | Role | Device properties | Grade |
|---|---|---|---|
| `6c290d2e-1c03-aca1-ab48-a9b908bae79e` | Telemetry ("ETC data") | Read, Write-w/o-response, Notify | UPSTREAM |
| `6eb675ab-8bd1-1b9a-7444-621e52ec6823` | Alerts | Read, Write-w/o-response, Notify | UPSTREAM |
| `15005991-b131-3396-014c-664c9867b917` | POI database | Read, Write-w/o-response, Notify | UPSTREAM |
| `2d86686a-53dc-25b3-0c4a-f0e10c8dee20` | Settings 1 | Read, Write-w/o-response, Notify | UPSTREAM |
| `5a87b4ef-3bfa-76a8-e642-92933c31434f` | Settings 2 | Read, Write-w/o-response, Notify | UPSTREAM |
| `2c86686a-53dc-25b3-0c4a-f0e10c8dee20` | **Command write** | Write-w/o-response | UPSTREAM-UNVERIFIED |
| `5987b4ef-3bfa-76a8-e642-92933c31434f` | Command response | Read, Notify | UPSTREAM-UNVERIFIED |
| `00002a26-…` (0x2A26) | Firmware Revision String | Read | OFFICIAL (SIG) |
| `00002a28-…` (0x2A28) | Software Revision String | Read | OFFICIAL (SIG) |
| `00002a24-…` (0x2A24) | Model Number String | Read | OFFICIAL (SIG) |
| `00002a29-…` (0x2A29) | Manufacturer Name String | Read | OFFICIAL (SIG) |

Upstream never read 0x2A24 or 0x2A29. They are in this project's plan because
Model Number is the most direct answer to "is this table even applicable".

### Advertising and pairing behaviour

| # | Claim | Grade |
|---|---|---|
| 3.1 | An **unpaired** R8w does not advertise at all until it is put in pairing mode. | UPSTREAM ("Confirmed the hard way") |
| 3.2 | A **paired** R8w advertises only while nothing is connected to it, and stops the instant anything connects — including a phone in the background. | UPSTREAM |
| 3.3 | The R8w advertises as `R8W@xx`. | UPSTREAM |
| 3.4 | The R8w uses a **random static** address, stable across power cycles. | UPSTREAM |
| 3.5 | GATT operations require a system-level BlueZ pairing first; unpaired, reads return empty or fail authentication with no useful error. | UPSTREAM |
| 3.6 | A first `pair` attempt often fails `AuthenticationFailed` and an identical retry succeeds. | UPSTREAM ("Seen twice, months apart") |
| 3.7 | BlueZ holds the link after pairing; it must be disconnected before Python can connect. | UPSTREAM |
| 3.8 | The plain R8's advertised name is **unknown**. | none |
| 3.9 | Whether the R8 exposes the same vendor UUIDs is **unknown**. | none |

3.8 and 3.9 are graded `none` because there is no evidence either way. Upstream
says the same: "Yours may differ in MAC address, firmware behaviour, and
possibly characteristic UUIDs … I have no idea whether any of this applies to
the R4W or anything else Uniden sells."

### Write commands — recorded, never sent

Upstream's `Constant.java` extraction, all **UPSTREAM-UNVERIFIED**:
`BTreqMUTE:1`, `BTreqMUTE:0`, `BTreqMMEM:1`, `BTreqMMEM:0`, `BTreqUMRK:1`,
`BTreqUMRK:0`, `BTreqRLCD:0`, written to `2c86686a-…` without response.

Upstream's own words: "**None of these have been tested against hardware.**"
Its library requires `allow_writes=True`; this project has no equivalent flag
and no write path at all. See `docs/SAFETY.md`.

### Upstream write-surface audit

A full read of the package at `9072bc2f` finds exactly **one** call that
transmits to the detector: `r8link/client.py:371`,
`await self._client.write_gatt_char(WRITE_CMD_UUID, command.encode(), response=False)`,
reachable only from `send_command()` behind `allow_writes`. Everything else is
`read_gatt_char` (lines 238, 251, 353) and `start_notify` (255, 256).

**This matters for the identity question.** Upstream obtains firmware and
software version with plain GATT *reads* of 0x2A26 and 0x2A28
(`read_device_info`, `client.py`). No application command is written to obtain
them. The assignment's escalation clause — "if identity/version can only be
obtained by an application write, stop and document" — therefore **does not
fire**: identity is reachable entirely inside the read-only boundary, and this
project's probe plan reaches it with reads alone.

---

## 4. Open questions for Jeremy's unit

Answered so far by the Phase 1 scan; the rest need a later phase.

| # | Question | Status |
|---|---|---|
| 4.1 | Does the unit have Bluetooth firmware at all? | **Answered — yes.** See 6.1. |
| 4.2 | What name does it advertise? | **Answered — `R8@…`.** See 6.2. |
| 4.3 | What address type? | **Answered — random static.** See 6.3. |
| 4.4 | Does it advertise the Uniden service UUIDs? | **Answered — no.** See 6.5. Says nothing about what it exposes once connected. |
| 4.5 | What firmware version is on it? | **Answered — 1.43, the current release.** See 6.8. |
| 4.6 | Does it expose the same vendor service UUIDs? | **Answered — yes, both.** See 6.10. |
| 4.7 | What do 0x2A24/0x2A29/0x2A26/0x2A28 return? | **Answered.** See 6.7–6.9. |
| 4.8 | Do the *characteristic* UUIDs inside those services match? | **Answered — yes.** The exact UUID set matches upstream; see 6.10. Payload formats remain unconfirmed. |

---

## 5. Node baseline (sanitized)

Captured read-only from the Pi on 2026-09-02 (UTC). Addresses withheld by
policy; see `docs/SAFETY.md`.

| Property | Value |
|---|---|
| Board | Raspberry Pi Zero 2 W Rev 1.0, aarch64 |
| OS | Debian GNU/Linux 13 (trixie) |
| Kernel | 6.18.39+rpt-rpi-v8 |
| Python | 3.13.5, `venv` and `ensurepip` both present |
| BlueZ | 5.82-1.1+rpt2 |
| Controller | `hci0`, UART, Broadcom, HCI 4.2 — Bluetooth 4.2 implies LE support |
| Controller state | `UP RUNNING PSCAN`, `Powered: yes`, `Discovering: no` |
| `bleak` installed | **Yes — 3.0.2**, in the user-owned venv at `/home/jeremy/unidenr8/.venv`. (Before this project ran, it was absent; the venv was created without `sudo`.) |
| PEP 668 | `/usr/lib/python3.13/EXTERNALLY-MANAGED` present — a venv is mandatory |
| Memory | 415 MiB total |

Upstream's own tested configuration was a Pi 4 on Debian Trixie with Python
3.13, BlueZ 5.82 and bleak 3.0.2 — the same OS, Python and BlueZ this node
runs. The differences are the board (Zero 2 W, less RAM) and the fact that
this radio is shared with a bound RFCOMM link.

---

## 6. Observed on Jeremy's R8

Two bounded 25-second advertisement-only discovery windows from the node, 2026-09-02T03:31:59Z and
03:34:28Z, while Jeremy had the detector in `BT Pairing` mode. No connection
was made and no characteristic was touched. Addresses withheld; the tokens
below are the sanitized, salted form the tooling emits.

| # | Observation | Grade |
|---|---|---|
| 6.1 | The detector advertises over BLE. **Its firmware therefore has working Bluetooth support** — closing open question 4.1, and making the §2 firmware-update path a precaution rather than a prerequisite. | OBSERVED |
| 6.2 | It advertises as **`R8@` + a four-character fragment** — `R8`, *not* `R8W`. Upstream only ever documented `R8W@xx`; a name filter matching `R8W` alone would have missed this unit entirely. | OBSERVED |
| 6.3 | Its address is **random static**, matching upstream's observation for the R8w (§3.4) and consistent with a stable address across power cycles. | OBSERVED |
| 6.4 | Signal was **−69 dBm** from the node in its installed position. (Whether that margin is comfortable is an inference; it is recorded here only as the measured value.) | OBSERVED |
| 6.5 | The advertisement carries **no service UUIDs**. So the vendor attribute table cannot be confirmed or refuted without connecting. Recorded as a negative result, not as absence of the services. | OBSERVED |
| 6.6 | The same device token appeared in both scans, three minutes apart, out of 17 and 18 devices seen. One detector, seen twice — not two sightings of different things. | OBSERVED |
| 6.7 | Whether this unit advertises **outside** pairing mode is **unknown**. Jeremy re-armed `BT Pairing` repeatedly through the session, so every sighting may fall inside a window. Upstream reports an unpaired R8w advertises only when armed (§3.1); this project has no evidence either way for the R8. | not established |
| 6.8 | Paired successfully on the **first attempt** of the run that used a persistent `KeyboardDisplay` agent with confirmation handling. Left untrusted and disconnected. | OBSERVED |
| 6.9 | Earlier attempts failed `org.bluez.Error.AuthenticationCanceled`. **The cause is not established.** Those runs also lacked the final agent handling, so a closed pairing window and a missing confirmation responder are both consistent with what was seen, and the evidence cannot separate them. | not established |
| 6.10 | It exposes **five** GATT services: Generic Access (0x1800), Generic Attribute (0x1801), Device Information (0x180A), and **both** Uniden vendor services. | OBSERVED |

### The vendor attribute table, confirmed on this unit

Service discovery on the connected R8 enumerated the individual characteristic
UUIDs, not merely their counts. Every vendor UUID upstream documented on an
**R8w** is present on Jeremy's **R8**, and nothing else is:

| Service | Characteristics found | Matches upstream |
|---|---|---|
| Data `18424398-7cbc-11e9-8f9e-2a86e4085a59` | POI `15005991-…`, alert `6eb675ab-…`, settings-1 `2d86686a-…`, telemetry `6c290d2e-…`, settings-2 `5a87b4ef-…` | all five, exactly |
| Command `1842467c-7cbc-11e9-8f9e-2a86e4085a59` | command-write `2c86686a-…`, command-response `5987b4ef-…` | both, exactly |
| Device Information `0000180a-…` | 0x2A24, 0x2A26, 0x2A29, 0x2A28 | standard SIG |
| Generic Access `00001800-…` | 0x2A00, 0x2A01 | standard SIG |
| Generic Attribute `00001801-…` | 0x2A05 (Service Changed) | standard SIG |

**This confirms UUID *presence* only.** It says nothing about payload format:
no vendor characteristic has been read on this unit, so the packet layouts in
§3 remain R8w evidence. A matching UUID is a strong indication the same
firmware family is behind it; it is not proof the bytes are shaped the same.

### Device Information, read verbatim

GATT reads of 0x2A24 / 0x2A29 / 0x2A26 / 0x2A28. No application command was
written. Recorded exactly as the detector returned them.

| Characteristic | Value |
|---|---|
| Model Number String (0x2A24) | `BTM10` |
| Manufacturer Name String (0x2A29) | `ATTOWAVE` |
| Firmware Revision String (0x2A26) | `NA/NA/NA/NA/NA/NA/NA/NA` |
| Software Revision String (0x2A28) | `R8/143/113/126/107/20260702/999/999/113` |

Three things follow, and only the first is certain.

**Model and Manufacturer do not name the detector.** The exact returned strings
are `BTM10` and `ATTOWAVE` — neither is "R8" or "Uniden". That much is
**observed**. The natural reading, that they identify a Bluetooth module rather
than the detector, is an **inference**: this project has not found a primary
source for an ATTOWAVE BTM10 part, and has not looked for one. What matters
operationally holds either way — 0x2A24 does *not* answer "R8 or R8w", so the
expectation recorded in §3 that Model Number would be the direct answer was
wrong, and the software string is what actually carries the model.

**0x2A26 returns all-`NA` placeholders on this unit** —
`NA/NA/NA/NA/NA/NA/NA/NA`, eight fields, every one the literal `NA`. It is not
an empty string and not a failed read: the characteristic exists and answers.
Upstream's R8w returned `R8W/NA/NA/NA/NA/20251002/NA/NA`, which carries a model
and a date in the same shape. So this unit populates 0x2A28 and leaves 0x2A26
at placeholders.

**The software string decodes against the owner's manual's naming convention**
(firmware, DSP, GPS, then the GPS database date):

| Field | Value | Reading |
|---|---|---|
| 0 | `R8` | model — and the only place the model actually appears |
| 1 | `143` | **firmware 1.43** — matches the current official release V1.43 (§2.1) |
| 2 | `113` | DSP 1.13 |
| 3 | `126` | GPS 1.26 |
| 4 | `107` | unknown; upstream's R8w had `103` here |
| 5 | `20260702` | **database 20260702** — matches the current official database (§2) |
| 6, 7 | `999`, `999` | placeholders, as upstream also observed |
| 8 | `113` | unknown; upstream's R8w had `120` |

Fields 1 and 5 are confident: both match a published Uniden release exactly,
and the positions agree with the manual. Fields 4 and 8 are unknown on both
models and are recorded, not interpreted.

**The detector is fully up to date.** Firmware 1.43 (released 2026-07-10) and
database 20260702 are the current published versions, so the firmware-update
path in §2 is a precaution rather than an action.

### What this does and does not establish

It establishes that the hardware, the firmware, the node's radio and the
tooling all work, and that the detector is reachable from where it sits.

### What this now establishes

**The attribute table applies.** Both Uniden vendor services are present on the
R8, and every individual vendor characteristic UUID upstream documented on the
R8w is present, with nothing extra. §3's service and characteristic UUIDs are
confirmed on this unit.

**Payload formats are still unconfirmed.** No vendor characteristic has been
read here. Every packet layout in §3 — telemetry fields, alert fields, POI
records, the settings blocks — remains R8w evidence, and the receive phase must
treat a parse failure as expected rather than exceptional.

**Two differences from the R8w are established**, neither predictable from
upstream: the advertised name is `R8@` rather than `R8W@`, and 0x2A24/0x2A29
return `BTM10`/`ATTOWAVE` where the model has to be recovered from 0x2A28
instead. The 0x2A26 placeholder pattern differs too, though upstream's R8w also
used `NA` for most of that field.

**Advertising behaviour outside pairing mode is not established** (6.7), and
neither is the cause of the earlier `AuthenticationCanceled` failures (6.9).
Both were claimed in an earlier draft on evidence that could not support them.

---
