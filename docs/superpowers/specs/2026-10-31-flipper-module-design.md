# Flipper Zero module — design spec

Date: 2026-10-31 · Status: approved in chat (MC, 2026-10-31), pending spec review
Branch: `flipper-module`

## 1. Purpose and success criteria

Add a comprehensive **Flipper Zero** unit to SIGINT-101: a device-in-hand tour of the
Flipper's base capabilities, taught on the nomad console. MC's success criteria, verbatim
intent: by the end of the module the learner knows

1. **what the Flipper is capable of**,
2. **how to use it as a standalone device**, and
3. **what having it next to / plugged into the nomad brings to the party**.

Plus a section for **diagnosing / troubleshooting / restoring + updating firmware** — scoped to
the **whole console rig** (the SpecFive Nomad that hosts the course), not just the Flipper (MC,
2026-10-31).

The "plugged into the nomad" payoff is a **live mirror**: the console reads the Flipper over its
USB CLI and shows what it is doing on the 800×480 panel — the Flipper becomes *another
instrument*, driven over USB instead of over an SDR (MC's call, 2026-10-31). No on-device C /
FAP in the core; that is a possible later extension, explicitly out of scope here.

## 2. Ground truth (observed 2026-10-31, nomad-b)

- Flipper Zero on nomad-b (`spec5b`), `/dev/ttyACM0`, USB id `0483:5740`, hardware name `Lle0zik`.
- **Official firmware 1.4.3** (build 05-12-2025), US region, BLE radio alive. Stock firmware,
  not a custom distro — the restore/update section targets official-firmware paths.
- Text CLI reachable over the serial port (115200, USB-CDC). Command surface includes:
  `subghz`, `nfc`, `rfid`, `ir`, `ikey`/`onewire` (iButton/1-Wire), `gpio`, `i2c`, `bt`,
  `storage`, `loader`, `update`, `factory_reset`, `device_info`, `start_rpc_session`, plus
  `led`/`vibro`/`buzzer`/`input`. `device_info` prints a clean `key : value` block.
- SD (`/ext`) maps 1:1 to capabilities: `subghz/ nfc/ lfrfid/ infrared/ ibutton/ badusb/ apps/
  dolphin/ update/ …`.
- Access: the droplet reaches nomad-b directly as user **`spec5`** (`ssh spec5@100.108.20.20`);
  the `spec5->claude-factory` key in nomad-b's `authorized_keys` is the droplet's. `spec5` is in
  `dialout`, so it can open `/dev/ttyACM0`. (Registry: `machines/hosts/nomad-b.md`.)

## 3. Where the code runs, and the dev loop

The kiosk/console app (FastAPI, the 800×480 panel) runs **on nomad-b**, where the Flipper and the
radios live. `FlipperLink` opens the **local** `/dev/ttyACM0` on nomad-b — no networked serial.

Dev loop (same as the SDR tracks): author in this droplet worktree → push the branch → on nomad-b
`git fetch && git checkout -f` the branch and run uvicorn → HIL against the real Flipper over SSH
as `spec5`. Acceptance evidence is real device output captured from nomad-b.

## 4. The unit: U10 "Flipper", lessons M29–M38

`units.py` gains `Unit(10, "flipper", "Flipper", "flipper", (29, 30, 31, 32, 33, 34, 35, 36, 37,
38))`. A new `flipper` tile glyph is added to `_tilegrid.html` (home grid) in the Flipper's visual
idiom. Each lesson is device-in-hand: the learner acts on the Flipper; the console teaches and
live-mirrors over the CLI.

| Lesson | Title | Standalone action | Live mirror (what the console shows) |
|---|---|---|---|
| M29 | Meet the Flipper | Orientation: the Dolphin, the radios/coils/GPIO/IR/iButton contact, navigation, the SD | `device_info` live: model, firmware, region, "plugged into the nomad?" |
| M30 | Sub-GHz | Read a remote (doorbell/garage), save, replay; frequency analyzer; legal note | the capture — frequency, modulation, the decoded/raw signal |
| M31 | 125 kHz RFID (LF) | Read an EM4100/HID prox fob, save, emulate | the decoded tag type + ID |
| M32 | NFC (13.56 MHz) | Read a MIFARE/NTAG card, UID, emulate; crypto limits | UID + tech/type |
| M33 | Infrared | Capture a TV-remote button, learn, replay; universal remote | the captured protocol + code |
| M34 | iButton / 1-Wire | Read a Dallas key, save, emulate | the key family + ID |
| M35 | GPIO & hardware | The 18-pin header, 3V3, UART/SPI/I2C, drive a pin, USB-UART bridge | a pin read / an I²C scan over the CLI |
| M36 | BadUSB | HID injection, DuckyScript, the authorization/ethics line; a benign demo payload | the payload + run state |
| M37 | Console rig: diagnose · restore · update | (see §6) | rig diagnostics: fw version, SD free, services |
| M38 | The nomad bench (capstone) | Everything together: Flipper as field tool, nomad as analyzer/library | the full live-mirror panel |

Scope note: the capability set (M30–M36) is the base Flipper feature spine MC named ("rfid, IR,
etc."). U2F, Apps and JS scripting are mentioned in M29/M38, not given full lessons, to keep the
unit bounded. Capability lessons may be paired down later at MC's discretion; this spec builds the
full set.

## 5. The live-mirror seam

New backend `sigint101/flipper.py`, mirroring the established decoder-module pattern
(`acars.py`/`ais.py`): module-level constants, `FlipperUnavailable(RuntimeError)`, a frozen
dataclass per record with `as_dict()`, pure parse functions, and a `FlipperLink` class with an
injected serial seam.

- `FlipperLink(*, port="/dev/ttyACM0", serial_factory=_open_serial, which=shutil.which)` — the
  `serial_factory` is the single hardware boundary (a fake serial port is the only test double).
- **No-mock refusal ladder** (no fake data in the product): `start()` raises `FlipperUnavailable`
  when the port is absent (no device), when the port is held by another process (busy — qFlipper),
  or when pyserial is missing. Honest-empty otherwise.
- **Not an SDR consumer.** The Flipper is its own USB device, like LoRa/Flock/Rayhunter — it is
  **not** added to `sdr_consumers` and gets **no `_claim_sdr` arbitration**. It owns `/dev/ttyACM0`
  exclusively and must not fight a qFlipper session.
- CLI transaction primitive: write `cmd\r\n`, read until the `>: ` prompt or a timeout, strip the
  echoed command. Pure parsers turn each response into a record (`device_info`, a tag read, a
  `storage list` listing).
- Reads exposed to the panels: `device_info()`, per-capability live reads, and `storage` pulls of
  files the learner saved on the SD. Read-only; the module never writes to the device beyond
  issuing read/scan commands.

Routes (mirroring `/api/ais` etc.): `GET /api/flipper` (presence + `device_info`), and
per-capability endpoints the lesson panels poll. `app.py` gains a `build_flipper` factory and the
routes wrap `FlipperUnavailable` in try/except to render honest-unavailable, exactly like
`/api/rayhunter`.

Frontend: a `flipper.js` (and per-capability renderers as needed) following `acars.js`/`ais.js` —
pure exported helpers for node tests, list/scalar renderers for the panels. `console.html` gains
frozen + live branches; `lessons.py` gains a `FLIPPER_PANEL` (and per-capability panels) in
`INSTRUMENT_PANELS` with `uses_flipper` properties. Instrument-lesson 800×480 fit is unchanged
(Next 423 / Commit 474).

**Plan recon (first implementation task, does not change this design):** confirm the exact
per-capability CLI read subcommands (`subghz`/`rfid`/`nfc`/`ir`/`ikey` usage) and decide, per
capability, whether the live read is CLI-driven (console issues the read) or SD-file-polled (the
learner saves on-device, the console pulls the file). `FlipperLink` abstracts both.

## 6. M37 — console rig: diagnose · restore · update (whole rig)

Covers the whole SpecFive Nomad bench, Flipper included. Delivered as a committed **runbook**
(`docs/runbook.md`, the authoritative recovery doc, cross-linking `machines/hosts/nomad-b.md` as
the inventory of record) **plus** the M37 console lesson that walks the high points.

- **Diagnose:** won't boot / panel dark (the HDMI EDID pin, `cmdline.txt`), kiosk app down, an
  SDR/radio missing, Flipper not mounting, `meshtasticd`, the battery/UPS daemon, tailnet
  reachability, the `lab-guard` firewall.
- **Restore:** SD backup & restore, config restore from `machines/hosts/nomad-b.md` + the
  meshtastic node identity (already exported on spec5 at `~/backups/meshtastic/nomadB-*`),
  re-flash, re-pair.
- **Update:** RPi OS / apt, the kiosk app, Flipper firmware (official 1.4.3 → update via qFlipper /
  web updater / `update` CLI).

### 6a. Prerequisite with a cost (separately approvable) — the SD baseline image

`machines/hosts/nomad-b.md` is explicit: **no disk image of the SD card exists anywhere**, and the
root partition was never expanded (~26 G of the 58 G card unallocated). A genuine *restore* has
nothing to restore *from*. So a real restore story requires **creating that baseline image first**:

- It is its own task, with a real cost: imaging an 8.8 G-used card (physical card-reader capture,
  or a `dd`/`rpi-clone`-style pull over the network), stored somewhere durable.
- It touches the **shared rig** (MC's gate) and is a one-way-ish operation.
- **Not folded into the module silently.** The M37 lesson/runbook document the restore *procedure*;
  actually producing the baseline image is a flagged task MC approves separately, with the method
  (physical vs network) and the storage destination chosen then.

## 7. Constraints carried from the course (unchanged)

- 800×480, fixed; lesson copy shrinks to fit (reading body ≤~50 words), never scroll, never ask.
- **No fake/mock mode in the product.** `FlipperLink` talks to the real device; the only test
  double is a fake serial port at the hardware boundary. Acceptance is real device output.
- **TDD** for logic: the parsers (`device_info`, a tag read, `storage list`) are test-first against
  **real captured CLI output** used as fixtures (the way AIS used the published gpsd vectors). A
  fixture corpus is captured from the real Flipper on nomad-b.
- ABOUTME 2-line headers on `.py`; evergreen names; lint clean.

## 8. Testing & acceptance

- **Unit:** `tests/test_flipper.py` — parsers vs real CLI fixtures; the refusal ladder with a fake
  serial boundary (no device / busy / no pyserial). `tests/flipper.test.js` — the JS pure helpers.
- **Route:** `tests/test_app.py` gains a `_StubFlipper` and the `build_flipper` factory; the
  parametrized contract test and route tests cover `/api/flipper*`. The Flipper is NOT in the
  SDR-arbitration contract list.
- **Lesson:** `tests/test_lesson_m29.py` … `m38.py` (pack structure + panels). `tests/test_units.py`
  updated: 10 units, partition covers 1..38.
- **HIL on nomad-b (named device-output acceptance):** the panel shows real `device_info`; at least
  one capability live-reads a real object (present an RFID fob / NFC tag / IR remote → the console
  shows it). Per-lesson demo+predict render at a forced 800×480 (Next 423 / Commit 474). Lint green.

## 9. Plan shape (ship the small core first)

One spec, phased implementation plan:

1. **Seam + proof:** `FlipperLink` + `/api/flipper` + the per-capability CLI recon (§5) + **M29**.
   Proves the live mirror end-to-end on real hardware. This is the small usable core.
2. **Capability lessons:** M30–M36, each with its live read, one at a time (TDD + HIL each).
3. **M37 rig runbook + lesson** (the SD-image prerequisite flagged to MC before any imaging).
4. **M38 capstone** + unit polish.

Each phase is independently shippable; STATUS updated and the branch pushed at each boundary.

## 10. Decisions recorded

- MC's call (2026-10-31): live mirror is the integration centerpiece; no on-device FAP in the core.
- MC's call (2026-10-31): troubleshoot/restore covers the whole rig, not just the Flipper.
- Assumption (stated, correct me): runbook lives in `sigint-101/docs/runbook.md` cross-linking the
  `machines` repo, rather than in `machines` itself.
- Open: whether to pair down the capability lessons (M30–M36) — building the full set unless told.

## 11. Out of scope

On-device C / FAP companion app; custom firmware (Unleashed/RogueMaster/Momentum); any write to the
Flipper beyond issuing read/scan commands; producing the SD baseline image (flagged prerequisite,
separately approved).
