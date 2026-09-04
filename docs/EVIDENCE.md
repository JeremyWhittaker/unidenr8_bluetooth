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

**At this checkpoint, this confirmed UUID *presence* only.** A matching UUID
was not proof that the bytes were shaped the same. The later bounded receive
in §7 confirms the telemetry layout on this R8; active-alert fields, POI and
settings remain unconfirmed.

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

**UUID presence did not by itself confirm payload formats.** The subsequent
receive in §7 confirms the telemetry field shape on this R8. It does not
confirm active-alert fields, POI records or settings blocks, and parsers still
treat an unknown shape as evidence rather than forcing it into a guess.

**Two differences from the R8w are established**, neither predictable from
upstream: the advertised name is `R8@` rather than `R8W@`, and 0x2A24/0x2A29
return `BTM10`/`ATTOWAVE` where the model has to be recovered from 0x2A28
instead. The 0x2A26 placeholder pattern differs too, though upstream's R8w also
used `NA` for most of that field.

**Advertising behaviour outside pairing mode is not established** (6.7), and
neither is the cause of the earlier `AuthenticationCanceled` failures (6.9).
Both were claimed in an earlier draft on evidence that could not support them.

---

---

## 7. Live data, observed on Jeremy's R8

One bounded 30-second receive window from the node, 2026-09-02T04:15:55Z.
Connected to the existing bond, GATT-read telemetry and alert once, subscribed
to both, collected, and tore the link down. POI and settings were never read.

| # | Observation | Grade |
|---|---|---|
| 7.1 | The detector streams telemetry over BLE to this node. **32 packets** captured: 1 telemetry read, 1 alert read, 30 telemetry notifications. | OBSERVED |
| 7.2 | Telemetry notification interval is **~1.0 s** (measured 0.97–1.02 s between consecutive packets). Consistent with upstream's "every 1-2 seconds". | OBSERVED |
| 7.3 | **The telemetry payload layout matches upstream's R8w format.** 31 of 31 telemetry payloads parsed, every one with exactly **7** `&`-separated fields, and the GPS group carrying exactly **4** comma-separated sub-fields. | OBSERVED |
| 7.4 | Battery voltage read **13.6 V**, steady across all 31 packets. | OBSERVED |
| 7.5 | GPS reported a fix for the whole window. | OBSERVED |
| 7.6 | The alert characteristic returned an all-clear packet, and sent no notifications in the window. Expected with no radar source present. | OBSERVED |

### What 7.3 upgrades, and what it does not

The **telemetry** packet format in §3 moves from `upstream-r8w` to confirmed on
this R8: the field count, the GPS sub-group shape and the voltage position all
hold, and the parser had a 100% success rate over 31 packets.

The **alert** packet format is still unconfirmed. The only alert payload seen
was all-clear, which exercises none of the band, strength, frequency, direction
or mute fields. Those stay `upstream-r8w` until a real detection is captured.

The **POI** and **settings** formats remain entirely unconfirmed and untested,
because this project does not read those characteristics.

### Not published

The raw payloads are in the node's owner-only private store.

*(As recorded at the time, heading, speed, altitude and POI detail were parsed
nowhere. That changed in §9: they are decoded now, and confined by permissions
and opt-ins rather than by absence. The observation above is unaffected — none
of those fields was published then and none is published by default now.)*

---

## 8. The supervised collector trial

One bounded 300-second collector trial from the node, 2026-09-02T04:50:39Z to
04:55:42Z, with the OBDLink sampled every 5 s throughout and for 35 s
afterwards (68 samples). The detector was already bonded; no discovery, no
pairing.

### Collector result

| # | Observation | Grade |
|---|---|---|
| 8.1 | **293 telemetry packets in 300 s** (~0.98/s), **0 unparsed**. | OBSERVED |
| 8.2 | **0 reconnects.** The link was established once and held for the full five minutes. | OBSERVED |
| 8.3 | The trial stopped itself at 303 s and exited 0, publishing `status: stopped`, `note: clean shutdown`, `connected: false`. | OBSERVED |
| 8.4 | Voltage moved from 12.3 V (no GPS fix) at the start to 13.6 V with a fix by the end. The cause of that change was not established. | OBSERVED |
| 8.5 | State directory `0700`, `state.json` and `collector.lock` both `0600`. No collector process and no held lock remained afterwards. | OBSERVED |
| 8.6 | One alert packet, from the initial read; no alert notifications. Nothing was detected. | OBSERVED |

### OBDLink contention — the question the trial existed to answer

All 68 samples, without exception:

| Check | Result |
|---|---|
| `hummer-rfcomm` active | **always** |
| `/dev/rfcomm0` present | **always** |
| Binding present | **always** |
| Binding state | **`connected` in every sample** — never dropped, never closed |
| `hci0` RX and TX error counters | **`0` and `0` in every sample** |

Controller byte-counter deltas correlated with the BLE window (they are
controller-wide counters, not packet attribution):

* during the trial: **RX +14,392 bytes over 295 s (~49 B/s)**, TX +372 bytes —
  a receive-dominated load, as a notify-only path should be;
* for 35 s after it ended: **RX +0 bytes** — the traffic stopped cleanly when
  the collector released the link.

**Conclusion: no link-state disruption was observed.** The vehicle's RFCOMM
binding survived a five-minute BLE session untouched and controller error
counters remained zero. Throughput contention was not measured.

### What this trial does *not* establish

Two limits, both worth stating plainly:

1. **The OBD collector was not running.** `hummer-collector` is disabled and
   inactive, so the RFCOMM link was bound and idle rather than actively
   carrying poll traffic. This measures a BLE session alongside an *idle* OBD
   link, not alongside active polling. The stronger test is a trial run
   concurrently with an OBD collection trial.
2. **Throughput was not measured, and cannot be here.** Confirming the OBD link
   still *works* would mean opening `/dev/rfcomm0`, which this project must
   never do. The evidence is that the binding and the controller stayed
   healthy — not that a poll would have succeeded.

Five minutes is also not a drive. The result is encouraging and is not yet a
warrant for enabling a service at boot.

### Parent hardening check

After the supervised trial, parent review added rejection of zero, negative,
NaN and infinite durations plus an outer asyncio ceiling covering a stuck GATT
operation. The hardened code was redeployed without installing a unit. A
15-second follow-up trial received 13 telemetry packets with zero unparsed and
zero reconnects, exited 0 after bounded disconnect cleanup, left the detector
disconnected, and again left `hummer-rfcomm` active with `rfcomm0` bound and
zero failed units. This was a code-path check, not additional OBD-throughput
evidence.

---

## 9. The expansion build — what changed, and what it did not prove

Dated 2026-09-02. This entry exists because the project grew a great deal in one
pass, and growth is easy to mistake for evidence.

### What was added

An event path (`events.py`) that turns full-snapshot alert notifications into
`alert_start` / `alert_update` / `alert_end` transitions; a decoder that exposes
every field of both packet formats with a per-field confidence grade; a second
published document at schema 2; a local SQLite history; a `gpsd` client; an
optional MQTT publisher; a standard-library HTTP and server-sent-events feed; a
confirmed read-only settings and POI inspection command; and a TOML
configuration that makes the OBD guard, the node's unit and device names, and
every outward feed into settings rather than constants.

### The grade of all of it

**None of it has met the detector.** Every one of those components is tested
against injected fakes: 617 tests, no radio, no broker, no `gpsd`, no network.
That is evidence that the code does what it was written to do. It is not
evidence about the protocol, the hardware, or the vehicle.

| Claim | Grade |
|---|---|
| The event path never loses a short alert | code-level, proven against a synthetic 400 ms alert |
| The alert tracker follows one source across a front→side→rear pass | code-level, proven against synthetic snapshots |
| Alert field meanings (band, strength, frequency, direction, mute) | still **UPSTREAM (R8w)**. Unchanged by this build. |
| Detector heading / speed / altitude *units* | still **UPSTREAM**. Decoded now, never measured. |
| Laser gun identifiers 0-19 | still **UPSTREAM-UNVERIFIED**. No laser packet has ever been captured on any hardware. |
| Mute codes 3-6 | still **UPSTREAM-UNVERIFIED**. |
| POI record layout | still **INFERENCE**, and deliberately has no parser. |
| Settings block layout | still unknown. `inspect` snapshots it; nothing decodes it. |
| OBD coexistence under active polling | still **not established**. §8's two limits stand. |

### Defects this build found in its own new code

Recorded because "the tests passed" is a weaker statement than "these were
caught", and because every one of them would have been invisible in production.
Twenty-six in total: five found by writing the tests, five by writing the
configuration reference against the code, three by writing the schema
reference, and thirteen by an adversarial review across concurrency,
correctness, privacy and failure handling.

The five that would have mattered most in a vehicle:

1. **A live alert stayed latched after the detector went away.** The last
   snapshot remained in the state file, the feed and the broker indefinitely,
   so a detector switched off mid-alert left a Ka warning showing forever. For
   this program that is the worst possible stale value: a stale threat reads
   exactly like a live one.
2. **The retention sweep could have deleted the whole history.** It cut on
   ``time.time_ns() - retain_days``, on a board with no battery-backed clock
   whose wall clock steps by hours when the network appears. Measuring against
   the newest row was not enough either — rows written during that window carry
   the bad stamp and one of them becomes the newest. It measures against the
   tenth-newest now, which a handful of outliers cannot move.
3. **Publishing did synchronous disk work on the event loop** — two JSON
   writes, two ``chmod`` calls and two renames on an SD card, on the loop
   holding the BLE subscription, on every transition. On BlueZ that does not
   cost latency; a client that will not drain its D-Bus socket is disconnected.
4. **Continuous mode had no ceiling on a stalled GATT call**, and no way to
   notice a link that BlueZ still called connected but that had stopped
   speaking. Either would have held the detector's link and the vehicle's radio
   indefinitely with the state file frozen.
5. **The SQLite ``-wal`` sidecar took the process umask.** SQLite creates it
   lazily at the first write, long after the umask was closed around
   ``connect()``. It holds the most recently written rows, so a ``0600``
   database with a group-readable journal beside it was not a private history.

And one that is worth recording for its shape rather than its severity: **a new
alert track was charged a miss on its own first snapshot**, because the
claimed-track set was computed before any track was started. That halved the
miss tolerance and made a single dropped packet split one threat into two — a
one-line bug in a derivation nobody could have checked against hardware,
because the hardware has never produced an alert.

### The first three, in the original wording

Recorded because "the tests passed" is a weaker statement than "the tests caught
these", and because each of the three would have been invisible in production.

1. **A new alert track was charged a miss on its own first snapshot.** The
   claimed-track set was computed before any track was started, so a track
   created by a snapshot counted as absent from it. That halved the miss
   tolerance and made a single dropped packet split one threat into two.
2. **The retention sweep could have deleted the whole history.** It cut on
   `time.time_ns() - retain_days`, on a board with no battery-backed clock whose
   wall clock steps by hours when the network appears. A clock briefly reading a
   future date would have deleted everything, permanently, without an error. The
   cut is now `min(now, newest row we hold)`.
3. **The SQLite `-wal` sidecar took the process umask.** SQLite creates it
   lazily at the first write, long after the umask was closed around
   `connect()`. It holds the most recently written rows — the alert events, and
   the coordinates when those are enabled — so a `0600` database with a
   group-readable journal beside it was not a private history.

Two more were found in the same pass and are worth the same note: a `gpsd`
receiver that had gone quiet held shutdown open indefinitely, because the read
was awaited rather than raced against the stop event; and the ingest queue's gap
record, when it lived inside the queue, could be evicted by the very overflow it
described and ended up ordered after records that arrived later than the loss.

### What would move any of this

`docs/VALIDATION.md`. It is the queue, in priority order, and the first item on
it — a K-band capture from a lawful passive source such as an automatic-door
opener — is ten minutes of work that would promote nine fields from UPSTREAM to
OBSERVED.

---

## 10. The expansion build, on hardware

2026-09-02, from about 22:14 UTC. The detector was powered and parked; the
OBDLink was bound and idle throughout. This is the first time any of §9's code
met the detector, and it closes several gaps §9 listed as open.

### The session

| # | Observation | Grade |
|---|---|---|
| 10.1 | The rewritten receive path connects to the existing bond and streams. One 30 s window took **29 telemetry packets**, a 40 s window **40**, and a 120 s collector trial **115** — 184 packets, **0 unparsed**, 0 reconnects. | OBSERVED |
| 10.2 | Inter-packet interval over 39 consecutive notifications: **min 0.994 s, median 1.013 s, max 1.031 s**. Consistent with §7.2's 0.97–1.02 s. | OBSERVED |
| 10.3 | Every packet had **exactly 7 fields** and, when a fix was present, a **4-part GPS sub-group** — 13 distinct payloads across the capture, all the same shape. Shape graded `confirmed-7`. | OBSERVED |
| 10.4 | Battery 13.4 V with the vehicle running, GPS locked, status letter `C`. | OBSERVED |

### The GPS sub-group, looked at for the first time

§7 recorded that this project parsed sub-fields 0–2 nowhere. It parses them now,
and this is the first evidence about what they contain on an **R8**.

| # | Observation | Grade |
|---|---|---|
| 10.5 | Sub-field 0 read **`NE`** — one of the eight compass points. Upstream's reading of it as a heading holds on this model. | OBSERVED |
| 10.6 | Sub-field 1 read **`0`** with the vehicle stationary. Consistent with a speed, though zero is zero in any unit; the units remain unvalidated until §11. | OBSERVED (value), UPSTREAM (unit) |
| 10.7 | Sub-field 2 decoded as an integer whose magnitude is **consistent with upstream's reading of feet and inconsistent with metres** at this location. The value itself is position-adjacent and stays in the private capture. | OBSERVED (shape), strongly supports UPSTREAM (unit) |
| 10.8 | **The coordinate tripwire did not fire.** `_parse_gps_group` flags the group if sub-fields 0 and 1 both read as signed decimals in coordinate range; sub-field 0 is a two-letter compass point, so the test fails at the first field. | OBSERVED |

10.8 is the one worth dwelling on. Until now, "the live telemetry carries no
latitude or longitude" rested entirely on upstream's *naming* of four fields
nobody here had looked at — and a 4-tuple is exactly the right width to be
latitude, longitude, altitude and status. It has now been looked at on this R8,
and sub-field 0 is a compass point. The claim is no longer inherited; it is
measured. It remains measured on **one unit, parked, with a fix** — a moving
capture (§11, `docs/VALIDATION.md` V1) is still what would confirm the units.

### Two fields that differ from the R8w

| # | Observation | Grade |
|---|---|---|
| 10.9 | Telemetry fields 5 and 6 read **`N`** and **`N`** on this R8. Upstream's R8w reads `D` and `D` in the same positions. | OBSERVED |
| 10.10 | Field 4 read a small changing integer (`13` in the capture). Field 3 read `0` throughout. | OBSERVED |

10.9 is a third established difference between the models, after the advertised
name (§6.2) and the `BTM10`/`ATTOWAVE` identity strings (§6). It is also why
this project publishes fields 3–6 under neutral names: upstream calls field 5
"wifi status", Uniden's product page says the R8 has no Wi-Fi (§1.2), and the
non-W unit reads a different character there than the Wi-Fi model does. What `N`
means is not established. It is recorded, not interpreted.

### The all-clear packet, finally recorded

| # | Observation | Grade |
|---|---|---|
| 10.11 | The alert characteristic returned **`0&0&0&0`** — 7 bytes, **exactly four slots, every one empty**. | OBSERVED |

§7.6 and §8.6 recorded that an all-clear packet was returned but never what it
looked like. Four slots is now observed on this R8, which matches the four-slot
structure upstream describes and the manual's "up to four simultaneous threats".
No alert notification arrived in any window; nothing was detected.

### The new code, on the target hardware

A 120-second collector trial with the OBD guard armed and the SQLite history
enabled, sampling the OBDLink every 5 s throughout.

| # | Observation | Grade |
|---|---|---|
| 10.12 | 115 telemetry packets, **0 unparsed, 0 dropped, 0 gaps, 0 lost notifications**; ingest queue high-water **2** of 256. | OBSERVED |
| 10.13 | **Loop lag: 0.6 ms current, 2.6 ms maximum**, alarm not raised. The "nothing slow on the event loop" invariant holds on a Pi Zero 2 W with the OBD probe, the SQLite writer and the state writes all in flight. | OBSERVED |
| 10.14 | Telemetry interval p95 **1.031 s** against the 0.97–1.02 s baseline — no sign of radio contention with the bound RFCOMM link. | OBSERVED |
| 10.15 | History wrote 13 rows with 0 dropped and 0 errors; `publish_failures` 0; retention swept and refused nothing. | OBSERVED |
| 10.16 | **All 26 OBD samples**: unit active, one binding, `hci0` RX/TX errors 0. Before and after: `rfcomm0:` bound on channel 1, `/dev/rfcomm0` mode 660 root:dialout, 0 failed units, controller powered and not discovering, both bonds intact. | OBSERVED |
| 10.17 | The **single-instance lock** refused a second collector while a first held the link, with the intended message. | OBSERVED |
| 10.18 | An **abrupt kill** — SIGHUP with no clean shutdown, the power-cut case this module is designed for — left an uncheckpointed 247 KiB WAL. The next open recovered it: the session, 14 telemetry rows and 1 alert snapshot were all intact. | OBSERVED |
| 10.19 | The database and **both WAL sidecars** were `0600` in a `0700` directory throughout, including immediately after the abrupt kill. This is the fix from §9 confirmed on hardware rather than in a test. | OBSERVED |

### What this run did *not* establish

**No active alert.** The vehicle was parked at a residence with no radar source
present, so every field of an active alert — band, strength, raw signal, the
frequency-versus-gun-identifier split, direction, mute — remains **UPSTREAM**,
exactly as it was. That is still the largest gap in the project and
`docs/VALIDATION.md` V2 is still the cheapest way to close it.

**No motion.** Speed and heading were read from a stationary vehicle, so the
units, the latency and the behaviour of the fix through a turn or a grade are
untested. V1 remains open.

**No POI, settings or descriptor read.** `inspect` was not run: it reads the
detector's saved camera locations and user marks, and "test it" is not the
explicit decision that command exists to require.

**No drive, and no active OBD polling.** `hummer-collector` is disabled, so the
RFCOMM link was bound and idle. §8's two limits stand unchanged, and V10 — one
to two hours of coexistence under real polling — is still the gate before the
service is enabled at boot.


---

## 11. The service build — measurements made while the vehicle was running

Session of 2026-09-03, on the target Pi, with the truck powered and the RFCOMM
binding active. This section records what was *measured*; the reasoning that
sits on top of it is in `docs/HANDOFF.md`.

### 11.1 The drive of 2026-09-02 produced no data at all — OBSERVED

Jeremy drove roughly 17:00–19:30 local. The history database's newest write was
**15:31**, before departure. There was no `unidenr8-collector` unit installed on
the node (`systemctl list-unit-files` had no match), no collector process
(`pgrep`), and no journal entry mentioning the project in that window.

Nothing was captured, and nothing was broken: the collector simply was not
running, because the repository shipped a unit template that no script installed
and a runbook that said not to enable it. The cost of that decision is now
measured at one 2.5-hour drive.

`scripts/install-service.sh` exists because of this.

### 11.2 The unit's own sandbox would have blocked the OBD guard — OBSERVED

**This is the finding that mattered most, and it was found before the unit was
installed rather than after.**

The shipped unit carried
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`. The OBD guard
shells out to `rfcomm` to read which device nodes are bound. On the node:

```
$ strace -f -e trace=socket rfcomm
socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_RFCOMM) = 3
```

`AF_BLUETOOTH` was not in the allow-list. Under that sandbox the socket call
fails, `rfcomm` lists nothing, the guard reads "not bound", and the collector
waits in `obd-blocked` **forever** — while `systemctl status` reports
`active (running)`.

A service that is healthy by every signal systemd offers and captures nothing is
the worst available failure, because it is invisible. `AF_BLUETOOTH` is now in
the allow-list, with a comment saying why it must not be removed to "tighten"
the sandbox.

Measured with `strace`, on the node, against the real binary. Not inferred.

### 11.3 `rfcomm` exits 0 when it can list bindings — OBSERVED

`rfcomm; echo $?` → `0`, with a binding present. This is what makes it safe for
the guard to treat a non-zero exit as "the tool could not run" rather than as
"nothing is bound" — two states that were previously reported identically, which
sends an operator to inspect the vehicle's link when the fault is on the host.

### 11.4 The detector's motion fields were not being recorded — OBSERVED

`history.record_detector_motion` defaults to **off**, and the node's
configuration had taken that default. The `telemetry` table's `direction_8`,
`speed_mph` and `altitude_ft` columns were therefore empty on every row written
before this session.

A drive undertaken to validate the detector's heading, speed and altitude would
have produced a database containing none of them. The default is right for
privacy and wrong for the one job the drive exists to do, which makes it a
configuration decision that has to be made deliberately and written down — see
`docs/CONFIGURATION.md`, "A validation drive".

With it enabled and `telemetry_every_seconds = 1.0`, 227 rows carrying heading,
speed and altitude were recorded in the first four minutes.

### 11.5 Altitude is consistent with feet and not with metres — INFERENCE

Parked, the detector reported an altitude of **1266**. Upstream documents this
field as feet. The test location's true elevation is within a few hundred feet
of that figure; read as metres the same number would place the vehicle roughly
four thousand feet up, which it demonstrably was not.

This is **not** a measurement against a reference altimeter, and it is graded
INFERENCE deliberately: it is one reading at one place, and it rules out metres
far more strongly than it establishes feet. V1 — a drive with a reference GNSS —
is still what promotes it. It is recorded because it is the first evidence of
any kind about that field's units from this detector.

The location itself is not recorded here, in this repository, or in any
published output. Only the relation is.

### 11.6 Live telemetry, re-confirmed under the service configuration — OBSERVED

Continuous collector, truck running, RFCOMM bound and active:

| | |
|---|---|
| Telemetry packets | 505, then 242 in a second session |
| Unparsed | **0** |
| Voltage | 13.6–13.7 V, engine running |
| GPS state | locked throughout |
| Detector heading | reported while stationary (`S`) |
| Alert packets | 1 — the all-clear read, as in §10.11 |
| OBD invariants | `hummer-rfcomm` active, `rfcomm0:` bound, unchanged across the session |

### 11.7 The POI record layout is now two graded hypotheses, not one — INFERENCE

Upstream published the numbers 13, 12 and 10 for the three POI record types.
This project had been reading them as *payload* sizes following a type byte and
an unknown byte, giving whole records of 15/14/12. A field-by-field count of
Uniden's current app supports reading them as the *whole* record length already:
`type(1) + unknown(1) + lat f32(4) + lon f32(4) + angle u16(2) + speed(1) = 13`,
12 without the speed byte, 10 for a bare user mark. That arithmetic is
self-consistent.

Neither reading has been checked against a populated POI database on any
detector, so this project now holds **both**, separately graded:

| Layout | Lengths | Grade |
|---|---|---|
| `whole-record` | 13 / 12 / 10 | UPSTREAM-UNVERIFIED |
| `payload-plus-header` | 15 / 14 / 12 | INFERENCE — this project's reading |

`inspection.evaluate_layouts()` runs every candidate against the bytes and
reports which one consumes the blob **exactly**. On a blob synthesised to the
13/12/10 reading it returns `whole-record: 3 records, 33/33 bytes, exact` and
`payload-plus-header: 1 record, walk stopped at offset 12` — a decisive verdict
either way, which a single blessed length table could not produce.

Swapping one guess for the other would have destroyed the record of the
disagreement without producing any evidence. The tool decides now, and one
capture settles it.

### 11.9 The GPS status letter has at least three values, not one — OBSERVED

Until this session the GPS sub-group's status field had exactly one observed
value: `C`, seen while the detector reported a fix (§10). Everything else about
that field was upstream's.

A cold start — the Pi and the detector both powering up with the engine, from
genuinely off — produced two more, in this order:

| Letter | Rows | When |
|---|---|---|
| `E` | 1 | the first packet after the link came up |
| `D` | 111 | every packet after it, for the rest of the window |
| `C` | — | previously observed, with a fix present |

Throughout the `E`/`D` window the other three GPS sub-fields read
`direction_8 = (absent)`, `speed = 0`, `altitude = 0`, while battery voltage rose
from 12.2 V to 13.5 V as the engine started. The vehicle was stationary.

**The letters are OBSERVED. What they mean is INFERENCE**, and deliberately not
written into the code: `DetectorGps.locked` maps `C` to true and *everything
else to `None`*, not to false. "We could not tell" and "there is no fix" are
different facts, and a detector that is searching must not publish the same
thing as one that has failed.

The obvious reading is that `E` and `D` are stages of acquisition and `C` is a
fix. That is not yet evidence. What would make it evidence is a single session
containing the transition, with the motion fields becoming non-zero at the same
row — which is exactly what a drive starting from a cold vehicle should produce.
`scripts/drive-report.sh` prints the letter sequence and calls out a transition
for that reason.

Until then, do not name these letters in code or publish an interpretation of
them. Two of the three have been seen only once each, on one firmware, on one
morning.

### 11.8 What this session still did not establish

**No active alert.** Unchanged, and still the largest gap. V2.

**No motion.** The vehicle was parked for the whole session. Speed read 0–3 while
stationary; heading, speed and altitude remain unvalidated against a reference.
V1.

**No POI, settings or descriptor read.** `inspect` was still not run. It reads
saved camera locations and user marks, and that decision remains Jeremy's to
make explicitly.

**No coordinates from the detector, and no new reason to expect them in
telemetry.** §10.8 stands: the GPS sub-group's first field is a compass point
and the coordinate tripwire did not fire. What is now *also* true is that this
is a statement about the **live telemetry packet only** — see `README.md` and
§11.7 for the stored-record question, which is open and testable.

---

## 12. The first drive — a 50-minute commute, moving

Session 11 on the vehicle node, 2026-09-03. The first data this project has ever
had from a vehicle in motion. The node was powered throughout and merely
**off the network**, which is why the capture survived: the collector writes to
local SQLite and needs no network at all.

| | |
|---|---|
| Telemetry rows | **2,636** |
| Unparsed | **0** |
| Duration | 49.7 min (monotonic) |
| Rows with the vehicle moving | 1,165 |
| Compass headings observed | **8 of 8** |
| Alert packets | 2, both all-clear (`0&0&0&0`) |
| Gaps over 5 s between stored rows | 1 (88 s) |

### 12.1 The GPS status letter decoded, by correlation — OBSERVED

The session captured the full cold-start sequence and the transition that gives
the letters meaning:

| Letter | Rows | Rows with a heading | Rows moving | Altitude range |
|---|---|---|---|---|
| `E` | 2 | 0 | 0 | 0 |
| `D` | 180 | 0 | 0 | 0 |
| `C` | 2,454 | **2,454** | 1,165 | 0 – 1,289 |

The heading field is present in **exactly** the rows whose status is `C`, and in
none of the 182 that are not — 2,454 of 2,454, across a single continuous
session, with speed and altitude reading zero throughout `E` and `D`.

That is a measured correlation rather than a reading of upstream, so:

* **`C` = a fix.** OBSERVED.
* **`D` = no fix.** OBSERVED — `DetectorGps.locked` now returns `False` for it
  rather than `None`.
* **`E` = unknown.** Seen twice, both times on the first packet after the link
  came up. Two samples is not a meaning, and it still returns `None`.

The distinction between "we could not tell" and "there is no fix" is kept
deliberately: a field that collapses them tells a consumer it knows something it
does not.

### 12.2 Altitude is not metres — OBSERVED (by exclusion)

With a fix present, altitude read **1,116 ft mean** across 2,454 samples, ranging
to 1,289. The test route's true elevation is within a couple of hundred feet of
that figure. Read as metres the same numbers would place the vehicle around
3,700 ft up, which it demonstrably was not.

This **refutes metres**. It does not by itself prove feet — that needs a
reference altimeter, and V1 stays open for it — but upstream's reading of the
field as feet is now consistent with measurement rather than merely inherited.
No location is recorded here or anywhere else; only the relation.

### 12.3 Speed — still open, and one answer would close it

While a fix was present the distribution was strongly bimodal: 1,289 samples
stopped, a long tail through the twenties and thirties, and then **403 samples
at 65 and above**, peaking at **83**.

That shape is a commute — surface streets, then sustained higher speed. Read as
mph the fast cluster is an ordinary freeway. Read as km/h it is 40–52 mph
sustained, which is possible but does not fit the shape as well.

**The driver confirmed it.** Presented with the distribution and the two
readings — an ordinary freeway commute under mph, or 52 mph sustained under
km/h — he identified the mph reading as matching the drive he had just taken.

So the field is **mph**, and it is published as such. The grade is deliberately
"corroborated, not instrumented": this is one operator's account of one trip
agreeing with the shape of the data, which is a great deal better than
inheriting a number from an R8w and a great deal worse than a reference GNSS
run alongside. `speed_unit` now reads
`"mph (corroborated on one drive, not instrumented)"`, which is the whole claim.

A reference run remains the thing that would make it exact, and it would also
give the *lag* — how far behind the detector's reading trails a real speed
change — which nothing so far touches at all.

### 12.4 The wall clock stepped 104 minutes mid-session — OBSERVED

One sample pair in the session shows the wall clock advancing **6,244 s** while
the monotonic clock advanced **2 s**. Exactly one such step; every other pair
agrees.

The board has no battery-backed clock, so this is the hazard the two-clock
design was built for, now measured rather than anticipated. The consequences are
worth stating plainly:

* **Durations and ordering are correct.** They come from `monotonic_ns`. The
  49.7 min above is trustworthy.
* **Wall timestamps before the step are wrong**, by up to 104 minutes. Anything
  that correlates this session against an external log by `at` or `wall_ns`
  will mis-align, and the session's apparent wall span of 154 minutes is an
  artefact.
* **Retention was unaffected**, because the sweep measures from
  `min(now, tenth-newest row)` rather than from `now` — §9.

### 12.5 No alert, and that is a true negative

Two alert notifications arrived in fifty minutes, both `0&0&0&0`, both parsed
cleanly. The driver independently reported seeing no alert on the detector's own
display for the whole trip.

So the characteristic notifies on **change**, not on a timer, and the silence is
the detector's, not the parser's. That is worth recording precisely because a
parser bug and a quiet drive look identical in a row count — this one is
corroborated by the operator.

**Every active-alert field therefore remains UPSTREAM.** Fifty minutes of
motion, zero detections. V2 is unchanged and is still the highest-value work
available.

### 12.6 What this drive did establish about the software

2,636 packets decoded in motion with **0 unparsed** and one 88-second gap. The
decoder, the ingest queue, the history writer and the 1 Hz throttle all held up
on a moving vehicle for the first time, on the target hardware, with the OBD
link bound.

---

## 13. The detector gives up a coordinate

2026-09-03, parked, engine running, detector reporting a fix
(`gps_locked: true`, 13.5 V). The collector was stopped so nothing else held
the link. **The first non-empty POI database read reported on any R-series
detector.**

The operator short-pressed the physical **MARK** button once. Nothing was
written to the detector: this project has no application-write path, and the
command characteristic is on a permanent denylist. `inspect --confirm` was then
run once.

### 13.1 The POI record layout is 13 / 12 / 10 — OBSERVED

```
POI blob: 23 bytes

  whole-record         13/12/10   records=2  exact=True   consumed=23/23
  payload-plus-header  15/14/12   records=1  exact=False  stopped at offset 15

  offset   0  type 0x01  speed camera   13 bytes  decodes to a legal coordinate
  offset  13  type 0x03  user mark      10 bytes  decodes to a legal coordinate
```

Twenty-three bytes is 13 + 10 exactly, and `whole-record` accounts for every
one of them. The competing reading this project had been carrying —
`payload-plus-header`, its own +2 arithmetic on the same upstream numbers —
desynchronises at offset 15 and cannot account for the blob.

| Layout | Lengths | Grade before | Grade now |
|---|---|---|---|
| `whole-record` | 13 / 12 / 10 | UPSTREAM-UNVERIFIED | **OBSERVED** |
| `payload-plus-header` | 15 / 14 / 12 | INFERENCE | **REFUTED** |

§11.7 said one capture would settle it either way. It did, on the first one.

The refuted layout is kept in `CANDIDATE_LAYOUTS` rather than deleted. A tool
that can only walk one layout cannot report that the layout is wrong, and the
next firmware — or an R8w — may not match this one.

### 13.2 The detector materialises the coordinate from its own fix — OBSERVED

The type-03 record appeared because a button was pressed on the detector's own
keypad. **No position was supplied to it**, by this project or by anything else:
no BLE command was sent, and the only traffic was a GATT read.

It decodes, under the confirmed layout, to a coordinate that is finite, inside
the legal latitude and longitude ranges, and not the zero-zero value an empty or
padded record produces.

That closes a question this project has restated four times, and the answer is
narrower and better than either extreme:

* **Live telemetry carries no coordinate.** Unchanged, and measured harder than
  ever — §12 added 2,636 packets from a moving vehicle with the tripwire silent.
* **The detector will nonetheless give you one, on demand.** It knows where it
  is, it will write that down when asked by its own keypad, and the record is
  readable over BLE with no write path of any kind.

### 13.3 What this does *not* establish

**That the coordinate is the vehicle's position**, to any accuracy. The record
decodes to *a* legal coordinate. Proving it is *this* location needs a reference
fix supplied privately by the operator, which `uniden-r8 poi-diff
--reference-file` reports as a distance in metres and never as a value. Not yet
done.

**That the transaction is reversible.** The deletion half of the test — press
MARK again, re-read, confirm the blob returns to 13 bytes with only the speed
camera left — had not been run when this was written. Until it has, nothing here
should be built into an automated add/read/delete loop; user marks are
persistent detector state.

**Anything about a write path.** None was used and none is proposed. The
experiment works precisely because the detector's own button does the writing.

### 13.4 Two other first observations from the same capture

**Settings 1 is 240 bytes with real structure** — 25 distinct byte values, the
most common (`0xff`) accounting for 34%. Upstream described roughly 200 opaque
bytes on an R8w. It is not uniform, so it is carrying configuration, and the
one-toggle-at-a-time diff in `VALIDATION.md` V9 now has a real baseline to diff
against.

**Settings 2 is 240 bytes of `0xff`, entirely uniform** — one distinct byte
value across the whole block. That matches upstream's R8w exactly, and on this
detector it means the block is unused, unwritten, or not yet populated.

The device's own `0x2901` descriptors name the three attributes `Data_1`,
`Data_2` and `Data_3` — the detector offers no more meaning than that.

**No byte of any of these blocks is recorded here or anywhere outside the
owner-only private store.**

### 13.5 The coordinate is the vehicle's, to **8 metres** — OBSERVED

The operator supplied a reference fix from a phone at the same location. It was
never written into this repository and is not recorded here; it was used once,
to compute a distance.

| Capture | Record | Distance from the reference |
|---|---|---|
| 19:59:55Z | type `0x03` user mark, 10 B | **8.0 m** |
| 19:59:55Z | type `0x01` speed camera, 13 B | 1,640 m |
| 19:59:55Z | type `0x01` speed camera, 13 B | 1,640 m |

Eight metres is inside the error of the consumer GNSS the reference itself came
from. **The record the detector wrote holds the vehicle's position.**

That is the whole claim, closed:

* the detector was sent nothing — no command, no coordinate, no write of any
  kind, only a GATT read;
* a button on its own keypad caused it to store a position;
* the position is *correct*;
* and it is readable over BLE by a project that has no write path at all.

The float32 encoding is good to well under a metre at these latitudes, so the
8 m is the fix's error and not the format's.

### 13.6 The layout confirmed a second time, at a different size — OBSERVED

The later capture is **36 bytes**, and `whole-record` consumed it exactly again:
13 + 13 + 10. `payload-plus-header` desynchronised at offset 15 for the second
time.

Two independent captures, two different blob lengths, the same layout accounting
for every byte of both. 13 / 12 / 10 is settled.

### 13.7 The POI characteristic returns a *nearby subset*, and it changes — OBSERVED

This was not expected, and it matters more than it looks.

| Capture | Bytes | Records | Nearest record to the reference |
|---|---|---|---|
| 19:49:49Z | 23 | camera 13 B, user mark 10 B | 10,995 m |
| 19:59:55Z | 36 | camera 13 B, camera 13 B, user mark 10 B | 8 m |

Ten minutes apart, from a stationary vehicle, the contents changed completely.
The earlier blob is **not** a prefix of the later one, and the user mark in the
first capture is nearly twelve kilometres from where the vehicle was parked --
so it is a different, older mark, not the one just created.

The characteristic therefore does not expose a stable database that grows by
append. It exposes **whatever the detector currently considers nearby**, and
that set is re-computed between reads.

Three consequences:

1. **A before/after diff cannot assume append semantics.** `poi_diff` already
   reports `appended_only` and warns when a change is not a clean append, which
   is exactly the case here -- but the warning is now the *normal* result rather
   than the exception, and anything built on top must treat it that way.
2. **"The blob returned to baseline" is not available as a reversibility test.**
   The baseline itself moves. Reversibility has to be judged on the specific
   record, by its decoded position, not on the bytes as a whole.
3. **A read is a sample of a moving window, not an export.** Backing up the full
   POI database is not something this characteristic can do; the official USB
   tool's `UMR` command remains the only candidate for that, and is untested.

The two type-`0x01` records at an identical 1,640 m are most likely one physical
camera stored twice -- both directions of the same road, or a duplicate entry.
Not established.

### 13.8 What is still not established

**Reversibility.** Neither press deleted anything; the second added a record
rather than removing one. A deletion has not been observed, and §13.7 means the
obvious test for it does not work. Nothing here should be built into an
automated add/read/delete loop.

**Whether the subset is distance-bounded, count-bounded, or something else.**
Three records were returned once and two another time. No rule has been
established, and one more capture at a known distance from a known mark would
start to.

### 13.9 All three record types observed, and the layout confirmed a third time

A later capture, taken from a different location, returned **73 bytes in six
records**, and `whole-record` consumed it exactly again:

```
inspect-...T220623Z.json   73 bytes   [whole-record]
   +  0  0x01 speed camera     13B
   + 13  0x02 red-light camera 12B
   + 25  0x01 speed camera     13B
   + 38  0x02 red-light camera 12B
   + 50  0x01 speed camera     13B
   + 63  0x03 user mark        10B
```

13 + 12 + 13 + 12 + 13 + 10 = 73. Three captures now, at **23, 36 and 73 bytes**,
every byte of all three accounted for by the same layout, and
`payload-plus-header` failing on each.

**Type `0x02` appears here for the first time**, at the predicted 12 bytes. All
three record types are now observed on hardware:

| Type | Meaning | Length | Grade |
|---|---|---|---|
| `0x01` | speed camera | 13 B | OBSERVED |
| `0x02` | red-light camera | 12 B | OBSERVED |
| `0x03` | user mark | 10 B | OBSERVED |

Upstream published these numbers with an empty database and could not test them.
They are correct.

### 13.10 The nearby window follows the vehicle — OBSERVED

§13.7 inferred that the POI characteristic returns a *nearby subset* rather than
the database. This capture settles it, because the vehicle had moved several
kilometres between reads:

| Capture | Bytes | Records | Distance of its records from the earlier test site |
|---|---|---|---|
| 19:49:49Z | 23 | 2 | ~11 km |
| 19:59:55Z | 36 | 3 | 8 m – 1.6 km |
| 22:06:23Z | 73 | 6 | ~6.4 km |

The third capture's records cluster around the vehicle's *new* position, and the
user mark created at the earlier site — measured at 8 m from it in §13.5 — is
simply **absent** from the returned set.

So the window is anchored to where the detector currently is, and moves with it.
Two consequences that matter operationally:

1. **A stored mark can only be verified from near where it was made.** Reading
   from elsewhere does not return it, and its absence is not evidence it was
   deleted. This also removes the last shape of the reversibility test proposed
   in §13.8: "the record disappeared" means nothing unless the vehicle has not
   moved.
2. **The set is not bounded by a fixed count.** Two, three and six records came
   back across the three reads. Whatever selects them, it is not "the nearest
   N". A distance bound is the obvious candidate and is not established.

### 13.11 Confirmed a second time, at a second location — **3.8 m**

A new mark was created several kilometres from the first test site and read
immediately. The blob grew from 73 bytes to 83 — exactly one 10-byte user mark —
and `whole-record` consumed all 83 exactly.

```
  +  0  0x01 speed camera     13B   921.7 m from the new mark
  + 13  0x02 red-light camera 12B   921.7 m
  + 25  0x01 speed camera     13B   921.7 m
  + 38  0x02 red-light camera 12B   921.7 m
  + 50  0x01 speed camera     13B     1.08 km
  + 63  0x03 user mark        10B     3.8 m   <- created moments earlier
  + 73  0x03 user mark        10B   555.5 m
```

**3.8 m**, against 8.0 m at the first site. Two independent measurements, two
locations kilometres apart, both inside the error of the consumer GNSS the
references came from. The detector's stored coordinate is the vehicle's
position, and this is no longer a single observation.

The layout has now been confirmed at **four different blob sizes** — 23, 36, 73
and 83 bytes — with every byte of each accounted for and `payload-plus-header`
failing on all four. 13 + 12 + 13 + 12 + 13 + 10 + 10 = 83.

### 13.12 The returned set is ordered, and it is not an append

The earlier blob is **not** a prefix of the later one even though the only
change was one added mark: the new record landed at offset 63 and the
previously-last user mark moved to 73.

Reading the type bytes in order — `01 02 01 02 01 03 03` — the set comes back
grouped by type, cameras before user marks, and the two user marks are ordered
nearest-first (3.8 m, then 555.5 m). Whether the grouping is by type, by
distance within type, or by something the detector computes for its own display
is not established from one capture.

What it does establish: **anything diffing these blobs must match on record
content, not on offsets.** A record's position in the returned set is not stable
across reads, so an offset-keyed diff will report spurious changes.

Four of the records sit at an identical 921.7 m — two speed cameras and two
red-light cameras. That is one physical intersection stored as four entries,
almost certainly one per approach, which is the first evidence about how the
detector's own database is organised.

### 13.13 What remains open after all this

**Deletion.** Still never observed. Three MARK presses, three additions.

**What bounds the returned set.** Counts of 2, 3, 6 and 7 records rule out a
fixed N. A distance bound fits every observation so far but is not established,
and the largest observed distance in a returned set (~1.08 km) is a lower bound
on it at best.

**The `unknown` second byte** of every record type. Present in all four
captures, never interpreted, and not worth guessing at.

---

## 14. The BLE command works

2026-09-03, at the operator's explicit and repeated instruction, one
`BTreqUMRK:1` was written to the Uniden command characteristic
(`2c86686a-…`) and the POI characteristic was read before and after.

**It worked.** The detector accepted the command and stored a user mark, and the
record decoded to a coordinate. Confirmed by the operator against his own
position.

This has never been demonstrated on any R-series detector by anyone. Upstream
documented the command from a decompiled app and recorded that it had never been
sent to hardware; this project had carried it as
`KNOWN_WRITE_COMMANDS`-for-documentation-only for the same reason. It is now
**OBSERVED** on a non-W R8 at firmware 1.43.

### 14.1 How it was done, and what was deliberately not done

The experiment ran as a **standalone script outside the package**, on the node,
uncommitted. It imported none of the gated helpers and left the AST audit and
the "no application-characteristic write path" property in the repository
completely intact.

That was not a technicality to be proud of; it was the cheap order of
operations. Nobody had ever sent this command to hardware, so the first question
was "does it do anything at all", not "how should it be productionised".
Answering it took two minutes. Restructuring `gatt.py`, `audit.py`, the command
surface, the tests and four documents to open the write path permanently would
have been hours, and wasted if the command had been inert or wrong.

**The package still has no write path.** Nothing in `src/uniden_r8/` writes an
application value to a vendor characteristic, `selftest` still proves it, and
that claim is still true of everything this project installs and runs.

### 14.2 What this changes, and what it does not

**Changed:** a user mark can be created without touching the detector. The
coordinate the detector derives is accurate — §13.5 and §13.11 measured 8.0 m
and 3.8 m at two locations.

**Not changed, and worth restating because "it works" invites forgetting them:**

* **Every command writes to flash.** Marks are persistent detector state.
* **Deletion is still unobserved.** `BTreqUMRK:0` exists in the decompiled
  vocabulary in both a bare and a coordinate-targeted form, and neither has been
  sent. Until one has, marks accumulate with no proven way to remove them.
* **It is still not a live position source.** Flash wear caps the rate, and
  §13.10's moving window means a mark written while driving cannot reliably be
  read back. A USB GNSS receiver remains the answer for continuous coordinates,
  and this remains a protocol result rather than a feature.

### 14.3 Reading a coordinate back

A separate one-off script read the POI characteristic and decoded every record
to latitude and longitude, printed to the owner's own terminal at his request.
The values were correct.

No coordinate was written to any file, any document, or this repository. The
publication gate still refuses positions, the repository hygiene scan still
looks for them in every committable file, and this section records that the
decode was correct without recording what it decoded.

---

## 15. The command-response characteristic, and a reversible transaction

Same session, same standalone-script-outside-the-package method as §14. Two
experiments: an add-then-delete round trip, and a retry of the delete after the
first attempt failed.

### 15.1 The response characteristic speaks — OBSERVED

`5987b4ef-…` was excluded from every allowlist in this project as "readable, but
pointless without a command", which was an assumption. It is not pointless. It
reports, promptly and in plain ASCII:

| Emitted | Meaning |
|---|---|
| `RDrespACK` | the command was accepted |
| `RDrptUMRK:1=1` | user-mark add, status 1 |
| `RDrptUMRK:0=1,42057F9F,C2DF7DC1` | user-mark delete, status 1, echoing the record |
| `RDrptUMRK:0=3,4206708F,02F07EF1` | user-mark delete, **status 3**, echoing garbage |

`=1` accompanied every operation that worked and `=3` the one that did not, so
the digit after `=` is a status rather than a count. Two observations of each is
not a decoded vocabulary, and no other status value has been seen.

Every value the detector emits is **uppercase hex**. That detail turned out to
matter.

### 15.2 The targeted delete works — but only with uppercase hex — OBSERVED

The delete was sent in the coordinate-targeted form, built from the four-byte
float encodings of a record the script had created moments earlier:

```
BTreqUMRK:0,<lat hex>,<lon hex>
```

**Lowercase failed.** `BTreqUMRK:0,42057f9f,c2df7dc1` was acknowledged, and then
reported back as `RDrptUMRK:0=3,4206708F,02F07EF1`. Decoding what came back:
the latitude is `33.609921` where `33.374630` was sent — a quarter of a degree
out — and the longitude is `3.5e-37`, which is not a coordinate at all. The blob
did not change and the mark stayed.

**Uppercase succeeded.** The identical command with `42057F9F,C2DF7DC1` returned
`RDrptUMRK:0=1,42057F9F,C2DF7DC1` — the values echoed back unchanged — and the
POI blob went from 20 bytes to 10, exactly one 10-byte user-mark record removed.

So the detector's hex parse is case-sensitive, it does not reject a lowercase
argument, and it acts on whatever it mis-parses instead. **A lowercase delete is
not a no-op — it is a delete aimed at a coordinate nobody chose.** In this case
it matched nothing. That is luck, not design.

### 15.3 The transaction is reversible — OBSERVED

```
before   10 bytes, 1 record
add      20 bytes, 2 records     BTreqUMRK:1        -> RDrespACK, RDrptUMRK:1=1
delete   10 bytes, 1 record      BTreqUMRK:0,LAT,LON -> RDrespACK, RDrptUMRK:0=1
```

The record set returned to exactly what it was, and the operator's own remaining
mark was untouched throughout. Both writes targeted only bytes the script itself
had created.

This closes the question §13.8 and §13.13 left open. A user mark can be added and
removed programmatically, verifiably, without touching the detector.

### 15.4 What is still not established

**The bare `BTreqUMRK:0`.** Documented as removing the *current or nearby* mark,
never sent here, and deliberately so: it selects a record this project did not
create. The targeted form is strictly safer and there is no reason to prefer the
other.

**Deleting anything but a user mark.** `BTreqRLCD:0` — red-light-camera
deletion — has not been sent and is not going to be on a database the operator
did not build.

**The "delete all" long-press.** No BLE equivalent is documented, and a command
whose name would mean *delete every saved location* is not something to arrive at
by guessing. It stays untested.

**Whether repeated add/delete cycles are safe.** One cycle has been run. Marks
are flash-backed, and nothing here establishes an endurance limit.

---

## 16. The full attribute surface, and a coordinate stream nobody had subscribed to

`uniden-r8 survey` was run for the first time. This is the first complete GATT
enumeration of an R8 at firmware 1.43 reported anywhere: the device's own tree,
not a catalogue inherited from an R8w.

### 16.1 Fourteen characteristics, and no hidden vendor surface — OBSERVED

| Service | Characteristics |
|---|---|
| `0000180a` device-information | 4 — model, manufacturer, firmware, software |
| `00001800` generic access | 2 — device name, appearance |
| `00001801` generic attribute | 1 — service changed |
| `1842467c` uniden-command | 2 — `Command`, `Response` |
| `18424398` uniden-data | 5 — `Data_1` … `Data_5` |

Three were not in this project's catalogue, and all three are standard BLE
housekeeping. **There is no undocumented vendor characteristic.** That is a
negative result worth as much as a positive one: it closes off "perhaps the
coordinates are on an attribute nobody has looked at".

The detector names its own attributes through `0x2901` descriptors, and the
names are its own numbering rather than ours:

| UUID | This project calls it | The device calls it |
|---|---|---|
| `6c290d2e…` | Telemetry | `Data_5` |
| `6eb675ab…` | Alerts | `Data_4` |
| `15005991…` | POI database | `Data_3` |
| `2d86686a…` | Settings 1 | `Data_1` |
| `5a87b4ef…` | Settings 2 | `Data_2` |
| `2c86686a…` | Command write | `Command` |
| `5987b4ef…` | Command response | `Response` |

**Every vendor data characteristic is `write-without-response`** — telemetry,
alerts, POI and both settings blocks, not only the command characteristic. That
is a larger write surface than this project had assumed existed, and none of it
has been written to.

### 16.2 The POI characteristic streams at 1 Hz — OBSERVED

Subscribing to every vendor characteristic that advertises `notify`, sending
nothing, stationary, for sixty seconds:

| Characteristic | Notifications | Distinct payloads | Sizes |
|---|---|---|---|
| Telemetry (`Data_5`) | 60 | 15 | 26, 27, 28 B |
| **POI (`Data_3`)** | **60** | **1** | **10 B** |
| Alerts (`Data_4`) | 0 | — | — |
| Settings 1 (`Data_1`) | 0 | — | — |
| Settings 2 (`Data_2`) | 0 | — | — |

**The POI characteristic pushes a record once a second, with no command sent.**
Ten bytes: one type-`0x03` user mark, which by §13.1 is a coordinate.

This project has subscribed to telemetry and alerts since it was written, and
never to this. A coordinate-bearing stream has been available the entire time.

One distinct payload only, because the vehicle was stationary and the nearest
saved point never changed — §13.10 established that the returned set is anchored
to where the detector is. **Whether the stream tracks the vehicle while moving is
the obvious next measurement and has not been made.**

Alerts being silent is expected: §12.5 established that characteristic notifies
on change, and nothing was detected. Both settings blocks were silent for the
whole window.

### 16.3 The response characteristic says nothing unprompted — OBSERVED

Twenty seconds subscribed, no command sent, **silence**. It speaks only when
written to (§15.1).

That is the assumption this project had encoded — the characteristic was
excluded from every allowlist as "pointless without a command" — now measured
rather than assumed. The assumption was right, and it was still worth the twenty
seconds to stop it being an assumption.

### 16.4 What this does to the live-coordinate question

The honest position, split into the two claims it had been collapsing:

**No latitude or longitude in the 1 Hz telemetry packet.** Strongly held. Two
independent lines now support it: thousands of packets with the tripwire silent,
and §16.1 showing there is no unexamined attribute for it to be hiding on.

**No live position data from the detector at all.** *Not supported, and this
project has been overstating it.* The POI characteristic delivers coordinate
records at 1 Hz for free. They are the coordinates of nearby saved points rather
than of the vehicle — a real distinction — but "the detector will not give you
position live" is a stronger claim than the evidence carries, and §15.3's
reversible add/delete makes a polled position query cheaper than it was assumed
to be.

The measurement that would settle it is a moving subscription to `Data_3`,
watching whether the distinct-payload count climbs with the route.

---

## 17. What the POI stream actually carries

The operator proposed the test: make a mark, learn its exact bytes, then watch
the notification stream for those bytes. It is the right experiment and it
settles §16.4.

### 17.1 The stream is a live view of the nearby window — OBSERVED

```
blob     : 10 B  ->  20 B  ->  10 B          (baseline, after add, after delete)
stream   : 10 B  ->  20 B  ->  10 B          payload size tracks the blob exactly
payloads containing the new record's bytes:  12/12 while it existed
                                              0/10 after it was deleted
record set restored to baseline:              true
```

The POI characteristic pushes the **whole current window** once a second. A
record created by `BTreqUMRK:1` appears in the stream within a second and
disappears within a second of being deleted.

So it is a live view of *saved records near the detector*. It is not the
vehicle's position.

### 17.2 But a mark is a position sample — OBSERVED

Three marks made at the same parked location across roughly an hour:

| Record | Latitude |
|---|---|
| `42057FA7` | 33.374660 |
| `42057FA1` | 33.374638 |
| `42057FAB` | 33.374683 |

A spread of about 2.5 m — consumer GNSS jitter. Each mark captured the
detector's fix **at the moment it was made**, not a cached or rounded value.

That, with §15.3's reversible add/delete, gives a position *query*:

```
BTreqUMRK:1  ->  read  ->  BTreqUMRK:0,<LAT>,<LON>
```

about ten seconds per cycle, one flash write per sample. **This is the honest
ceiling of the detector as a position source.** It is not a 1 Hz feed of the
vehicle's own position, and nothing found so far is. A USB GNSS receiver remains
the answer for continuous position; this is a protocol result.

### 17.3 The ordering bug this test exposed — and it deleted the wrong record

The first run of this experiment selected the record to delete with
`marks[-1]`, assuming the newest is last. **§13.12 says the returned set is
ordered nearest-first**, and a mark made at the current location is the nearest
thing in it, so it sorts **first**. The corrected run confirms it directly:

```
the NEW record is at index 0 of 2  (FIRST)
```

So the first run deleted a *pre-existing* record and left its own test mark
behind — the exact inversion. Both records happened to be artefacts of earlier
experiments rather than anything the operator had made by hand, and the leftover
was removed afterwards, but the failure mode is the one that matters:

**Identify records by set difference against a baseline read, never by position
in the list.** Position is not stable across reads (§13.12), the set is
recomputed as the vehicle moves (§13.10), and an offset- or index-keyed
selection will eventually delete somebody's saved location. Any tool built on
this must do the baseline read first.

### 17.4 An empty POI window is two bytes, not zero — OBSERVED

After the last test mark was removed, the characteristic returned **2 bytes**.
Not an empty read. So there is a two-byte header or terminator that record
walking has never had to account for, because every capture so far contained at
least one record. `evaluate_layouts` would report no layout consuming a 2-byte
blob exactly, which is correct behaviour for an input that is entirely framing.

---

## 18. Searching every attribute for the live fix

The operator's test, and the strongest evidence this project has on the
question, because it has a **positive control**.

Every previous negative rested on not having *found* a coordinate. This one
rests on knowing exactly what to look for: create a mark, so the detector's own
current fix is known as exact bytes, then search everything else it will give us
for those bytes.

### 18.1 Method

Forty encodings, because a stored coordinate need not share the POI record's
byte order:

* float32 big-endian, exact and 3-byte prefix
* float32 little-endian, exact and prefix
* float64, both orders, 4-byte prefixes
* scaled integers at 1e5, 1e6 and 1e7 degrees, both orders
* ASCII decimal at 3, 4, 5 and 6 places, signed and unsigned

The 3-byte prefixes matter: consumer GNSS jitter moves the low byte of a
float32 latitude by metres while the top three bytes stay put, so a prefix match
finds a live value that is *near* the mark without needing it to be identical.

Searched across everything the device exposes:

| Haystack | Size |
|---|---|
| Settings 1 (`Data_1`) | 240 B |
| Settings 2 (`Data_2`) | 240 B |
| Telemetry (`Data_5`) | 29 notifications, 25–26 B |
| Alerts (`Data_4`) | 0 notifications (nothing detected) |
| Command response | 2 notifications |
| POI (`Data_3`) | 29 notifications — **positive control** |

### 18.2 Result — OBSERVED

```
positive control OK: 4 hits in the POI data
     lat f32 BE exact         42057FA8
     lat f32 BE prefix        42057F
     lon f32 BE exact         C2DF7DC6
     lon f32 BE prefix        C2DF7D

NO hits outside the POI data.
```

The control found the coordinate where it certainly is, so the search works and
the negatives are worth something. **In no other attribute, in any of the forty
encodings, does the detector's own position appear.**

Combined with §16.1 — fourteen characteristics, no undocumented vendor surface —
this is close to the limit of what a black-box search can establish:

* there is no attribute nobody has looked at;
* the one packet that streams continuously is decoded field by field;
* both opaque settings blocks have now been searched for a known value;
* and every notification stream has been searched the same way.

### 18.3 What this still does not rule out

**A moving test.** The vehicle was stationary. A field that only carries
position while moving — a delta, or something derived from speed and heading —
would not have shown up. Unlikely, and unmeasured.

**Settings that change slowly.** Both blocks were read once and neither notified
during the window, so a position that appears in settings only after some
interval would have been missed.

**An encoding not in the list.** Compressed, XORed, offset-encoded or split
across non-adjacent bytes would all defeat a substring search. Forty encodings
covering the usual forms is a serious search, not an exhaustive one.

The honest summary is unchanged in direction and much firmer in degree: the
detector does not publish its own live position on any attribute this project
can see. A mark remains a position *sample*, and a USB GNSS receiver remains the
answer for a continuous fix.
