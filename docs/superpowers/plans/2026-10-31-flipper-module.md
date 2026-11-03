# Flipper Zero module — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a comprehensive Flipper Zero unit (U10, M29–M38) to SIGINT-101, with the nomad console as a live mirror of the device over its USB serial CLI.

**Architecture:** A new `FlipperLink` backend owns `/dev/ttyACM0` on nomad-b and exposes read-only device state to the kiosk — mirroring the `RayhunterMonitor` pattern (separate device, injected transport seam, `*Unavailable` refusal, no SDR arbitration). The live mirror is SD-file-driven: the learner saves a capture on the device, the console pulls and parses it; `device_info`/`loader`/`storage` are always-on reads. Lessons are device-in-hand, taught + verified on the 800×480 panel.

**Tech Stack:** Python 3 / FastAPI / Jinja2 / pyserial (already on nomad-b) / vanilla JS panels + node tests. SQLite progress unchanged.

**Spec:** `docs/superpowers/specs/2026-10-31-flipper-module-design.md` (read it alongside this plan).

## Global Constraints

- Target: nomad-b (`spec5b`), Flipper on `/dev/ttyACM0`, official fw 1.4.3. Dev loop: author in this droplet worktree → push branch → on nomad-b `git fetch && git checkout -f <branch>` → run uvicorn → HIL over `ssh spec5@100.108.20.20`.
- **No fake/mock mode in the product.** The only test double is `FlipperCli`'s `opener` seam (a scripted byte channel) and `FlipperConsole`'s injected `cli_factory`/`glob`; no real serial in unit tests. Acceptance is real device output.
- **Build on the existing transport** (`flipper.py`: `FlipperCli`/`probe_flipper`/`read_file`, errors `FlipperError`/`FlipperSelectionError`) and `.sub` parser (`subfile.py`). Do not reimplement them or edit their tests. No new exception type (reuse `FlipperError`).
- **TDD for logic:** parsers are test-first against **real captured fixtures** (CLI text / saved `.sub`/`.nfc`/`.ir`/RFID/iButton files pulled from the device), the way AIS used the gpsd vectors. Never edit a test to fit a bug.
- The Flipper is its own USB device: **not** in `sdr_consumers`, **no** `_claim_sdr`. It owns the serial port exclusively (must not fight qFlipper).
- 800×480 fixed; lesson copy shrinks to fit (reading body ≤~50 words); instrument lessons render demo Next≈423 / predict Commit≈474; never scroll, never ask.
- ABOUTME 2-line headers on every `.py`; evergreen names; `ruff` clean (≤100-col); `tests/` green and pristine.

## Review Focus

- **An app is open on the Flipper** → a read returns `this command cannot be run while an application is open`; `FlipperConsole` must surface that as `FlipperError`, not parse it as data. (Task 1, `test_device_app_open_raises_flippererror`.)
- **`device_info` with a missing/renamed key** (fw variant) → `parse_device_info` yields the keys present, never KeyErrors. (Task 1, `test_parse_device_info_tolerates_missing_key`.)
- **Two Flippers on the bus** → `probe_flipper`/`find_flipper_port` already refuse (`FlipperSelectionError`); `FlipperConsole.device()` must propagate, not pick one. (Task 1, `test_device_two_attached_raises_selection_error`.)
- **A saved capture file that is empty or malformed** → the capability parser raises/returns empty, the panel shows honest-empty, no invented capture. (`.sub` already covered by `test_subfile.py`; Phase 2 per-capability tasks for the rest.)
- **Port busy / partial serial read / connect-banner leak** → already handled and tested in the existing transport (`test_flipper.py`); not re-tested here.

---

## Phase 1 — Seam + `/api/flipper` + M29 (the shippable core)

Proves the whole live mirror end-to-end on real hardware: the console shows real `device_info` and answers "is the Flipper plugged into the nomad?".

> **Builds on existing work (commit `8a3c739`).** The transport `sigint101/flipper.py`
> (`FlipperCli`, `probe_flipper`, `find_flipper_port`, `read_file`, `FlipperError`,
> `FlipperSelectionError`) and the `.sub` parser `sigint101/subfile.py` (`parse_sub`→`SubCapture`)
> already exist and are tested. Do NOT reimplement them; do NOT edit `tests/test_flipper.py` or
> `tests/test_subfile.py`. Task 1 adds the parser + read layer on top.

### Task 1: `device_info` parser + `FlipperConsole` read layer

**Files:**
- Create: `sigint101/deviceinfo.py` (the `device_info` parser)
- Create: `sigint101/flipperconsole.py` (the console-facing read layer on `FlipperCli`)
- Test: `tests/test_deviceinfo.py`, `tests/test_flipperconsole.py`
- Fixture: `tests/fixtures/flipper/device_info.txt` (already captured from the live device)

**Interfaces:**
- Consumes: `FlipperCli`, `probe_flipper`, `find_flipper_port`, `FlipperError`, `FlipperSelectionError` from `sigint101/flipper.py`.
- Produces:
  - `@dataclass(frozen=True) class DeviceInfo` — `model`, `name`, `firmware`, `region`, `radio_alive: bool`, `raw: dict`; `as_dict() -> dict`. (`region` from `hardware_region_provisioned`.)
  - `parse_device_info(text: str) -> DeviceInfo` — tolerant `key : value` parser; missing keys → empty string, never a KeyError.
  - `APP_OPEN_MARKER = "cannot be run while an application is open"`.
  - `class FlipperConsole` — `__init__(self, *, probe=probe_flipper, cli_factory=FlipperCli, glob=glob.glob)`; `device() -> dict` — `probe()`; if absent raise `FlipperError`, if two attached `probe` already raised `FlipperSelectionError`; else open one `cli_factory()` session, run `device_info`, return `parse_device_info(reply).as_dict()`; a reply containing `APP_OPEN_MARKER` raises `FlipperError`.

- [ ] **Step 1: Fixture** — already captured at `tests/fixtures/flipper/device_info.txt` (real device output). Confirm it is present.
- [ ] **Step 2: Write failing** `tests/test_deviceinfo.py`:
  - `test_parse_device_info_reads_core_fields` — load the fixture; `model == "Flipper Zero"`, `name == "Lle0zik"`, `firmware == "1.4.3"`, `region == "US"`, `radio_alive is True`.
  - `test_parse_device_info_tolerates_missing_key` — a block without `hardware_region_provisioned`; `region == ""`, no KeyError.
  and `tests/test_flipperconsole.py` (fake `cli_factory` returning a context manager whose `.command("device_info")` yields the fixture; fake `glob`):
  - `test_device_returns_parsed_dict_when_present` — present glob; `console.device()["firmware"] == "1.4.3"`.
  - `test_device_absent_raises_flippererror` — empty glob (`probe` reports absent) → `FlipperError`.
  - `test_device_two_attached_raises_selection_error` — two by-id matches → `FlipperSelectionError`.
  - `test_device_app_open_raises_flippererror` — `.command` returns `APP_OPEN_MARKER` text → `FlipperError`.
- [ ] **Step 3: Run** `./.venv/bin/pytest tests/test_deviceinfo.py tests/test_flipperconsole.py -q` → FAIL.
- [ ] **Step 4: Implement** `deviceinfo.py` (parser: `key.partition(":")`; `raw` dict; map known keys; `radio_alive == "true"`) and `flipperconsole.py` (compose `probe` + `cli_factory` + `parse_device_info`).
- [ ] **Step 5: Run** both → PASS; `ruff check sigint101/deviceinfo.py sigint101/flipperconsole.py tests/test_deviceinfo.py tests/test_flipperconsole.py` clean.
- [ ] **Step 6: Commit** (`git add sigint101/deviceinfo.py sigint101/flipperconsole.py tests/test_deviceinfo.py tests/test_flipperconsole.py tests/fixtures/flipper/device_info.txt`).

### Task 2: `/api/flipper` route, factory, stub

**Files:**
- Modify: `sigint101/app.py` (add `build_flipper` factory + `GET /api/flipper`, following the `build_rayhunter` / `/api/rayhunter` try/except shape — NOT `_claim_sdr`).
- Test: `tests/test_app.py` (add `_StubFlipper`, register `build_flipper`, route tests).

**Interfaces:**
- Consumes: `FlipperConsole`, `FlipperError`, `FlipperSelectionError` from Task 1 / existing transport.
- Produces: `build_flipper(...) -> FlipperConsole`; `GET /api/flipper` → `{"available": bool, "device": {...} | None, "error": str | None}`.

- [ ] **Step 1: Write failing route tests** in `tests/test_app.py`: `_StubFlipper` with a `device()` returning a canned dict and a mode that raises `FlipperError`; assert `/api/flipper` returns `available:true` + device when present, and `available:false` + `error` (HTTP 200) when it raises. Add `/api/flipper` to the honest-unavailable route list (NOT the SDR-arbitration contract list).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the factory + route in `app.py`, wrapping `FlipperError`/`FlipperSelectionError` like `/api/rayhunter` wraps `RayhunterUnavailable`.
- [ ] **Step 4: Run** `./.venv/bin/pytest tests/test_app.py -q` → PASS.
- [ ] **Step 5: Commit.**

### Task 3: Unit U10, home tile glyph, device panel JS

**Files:**
- Modify: `sigint101/units.py` (add `Unit(10, "flipper", "Flipper", "flipper", (29,30,31,32,33,34,35,36,37,38))`).
- Modify: `tests/test_units.py` (10 units; partition covers `range(1,39)`; U10 lesson list).
- Modify: `sigint101/templates/_tilegrid.html` (add a `flipper` glyph).
- Create: `sigint101/static/flipper.js` (pure `deviceLine(info)` helper + panel renderer).
- Create: `tests/flipper.test.js` (node test for `deviceLine`).
- Modify: `sigint101/static/app.css` (flipper panel class).
- Modify: `sigint101/lessons.py` (`FLIPPER_PANEL` in `INSTRUMENT_PANELS`; `uses_flipper` property).
- Modify: `sigint101/templates/console.html` (frozen + live branch for the flipper panel).

**Interfaces:**
- Produces: `uses_flipper` lesson property; `flipper` panel key; `deviceLine(info) -> string` in `flipper.js`.

- [ ] **Step 1: Write failing tests** — `tests/test_units.py` updated assertions (10 units, partition 1..38); `tests/flipper.test.js` for `deviceLine({model,firmware,region}) -> "Flipper Zero · fw 1.4.3 · US"`.
- [ ] **Step 2: Run** `pytest tests/test_units.py -q` and `node tests/flipper.test.js` → FAIL.
- [ ] **Step 3: Implement** units entry, glyph, `flipper.js`, css, `lessons.py` panel, console branch.
- [ ] **Step 4: Run** both → PASS.
- [ ] **Step 5: Commit.**

### Task 4: M29 "Meet the Flipper" lesson pack

**Files:**
- Create: `lesson_packs/29-meet-flipper.yaml` (panels: `[flipper]`; reading bodies ≤~50 words; demo + predict stages).
- Test: `tests/test_lesson_m29.py` (pack loads; panels==['flipper']; stage structure).

- [ ] **Step 1: Write failing** `tests/test_lesson_m29.py`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Author** the pack (orientation copy: Dolphin, radios/coils/GPIO/IR/iButton contact, SD; demo shows `device_info`; predict = "is it plugged into the nomad?").
- [ ] **Step 4: Run** → PASS; full `pytest -q` + `ruff` green.
- [ ] **Step 5: Commit.**

### Task 5: HIL acceptance on nomad-b (core)

Not code — the named device-output acceptance for the core.

- [ ] **Step 1:** push `flipper-module`; on nomad-b `git fetch && git checkout -f flipper-module`; symlink/confirm `.venv`; run uvicorn on 127.0.0.1:8008.
- [ ] **Step 2:** `curl /api/flipper` → real `available:true` + the live `device_info` (fw 1.4.3). Record verbatim.
- [ ] **Step 3:** render M29 demo + predict at a forced 800×480; confirm Next≈423 / Commit≈474.
- [ ] **Step 4:** restore main on nomad-b; update `STATUS.md` (in-worktree) with observed evidence; push. **Ship Phase 1 as its own PR** (small usable core).

---

## Phase 2 — Capability lessons M30–M36

Each is the same shape. **Capability-lesson task template** (instantiate once per lesson):

**Files (per lesson X / capability C):** `sigint101/<C>file.py` (+`parse_<C>(text) -> record`, a per-format parser module in the `subfile.py` style — NOT added to the transport `flipper.py`), `tests/test_<C>file.py` (+parser tests vs a real fixture), `tests/fixtures/flipper/<C>.<ext>` (a real saved file pulled from the device), `sigint101/flipperconsole.py` (+a `<C>(path)` read method using `cli.read_file`), `sigint101/app.py` (+`GET /api/flipper/<C>`), `tests/test_app.py` (+stub/route), `sigint101/static/flipper.js` (+`<C>Line`/renderer) + `tests/flipper.test.js`, `sigint101/lessons.py` (+panel), `sigint101/templates/console.html` (+branch), `lesson_packs/X-<slug>.yaml`, `tests/test_lesson_mX.py`.

**Steps (per lesson):** (1) capture the real fixture from the device (`storage read_chunks /ext/<C>/<saved>` via `cli.read_file`, or an idle CLI read); (2) write failing parser + console-read + route + lesson tests; (3) run → FAIL; (4) implement parser + console read + route + panel + pack; (5) run → PASS, `ruff`/node green; (6) commit; (7) HIL on nomad-b: present a real object (fob/card/remote) → save on device → console mirrors it; record evidence. (M30's parser is already done — see Task 6.)

**Instances:**

- [ ] **Task 6 — M30 Sub-GHz** (`C=subghz`, file `.sub`): **parser already done** — `subfile.parse_sub`→`SubCapture` (`frequency_hz`/`preset`/`protocol`/`pulses`, `duration_us`, `edge_count`, `width_histogram()`), tested in `test_subfile.py`. This task only wires it: a `FlipperConsole.subghz(path)` read that pulls the `.sub` via `cli.read_file` and returns `parse_sub(...)`, the `/api/flipper/subghz` route, the panel (frequency + preset/modulation + edge count + the width histogram as the pulse-scope view, tying to M9/M10), and the M30 lesson. HIL: capture a real remote.
- [ ] **Task 7 — M31 125 kHz RFID** (`C=lfrfid`, `.rfid` text): parse Key type + Data. Mirror shows tag type + ID. Direct `rfid read` allowed only when idle. HIL: a real EM4100/HID fob.
- [ ] **Task 8 — M32 NFC** (`C=nfc`, `.nfc`): parse Device type + UID + ATQA/SAK. Mirror shows UID + tech. HIL: a real MIFARE/NTAG card.
- [ ] **Task 9 — M33 Infrared** (`C=infrared`, `.ir`): parse name + protocol + address/command (or raw). Mirror shows protocol + code. HIL: a real TV remote button.
- [ ] **Task 10 — M34 iButton/1-Wire** (`C=ibutton`, `.ibtn`): parse key family + ID. Mirror shows the key. HIL: a real Dallas key (if available; else honest-empty + fixture-only parser proof).
- [ ] **Task 11 — M35 GPIO & hardware** (`C=gpio`): no saved-file format — mirror via an idle CLI read (`gpio` read / `i2c` scan). Lesson covers the 18-pin header, 3V3, UART/SPI/I2C, USB-UART bridge. HIL: read a pin / scan I²C.
- [ ] **Task 12 — M36 BadUSB** (`C=badusb`): parse a DuckyScript `.txt` from `/ext/badusb` and show it; teach HID injection + the authorization/ethics line; a benign demo payload. Mirror shows the payload + (via `loader info`) run state. HIL: show a demo script; run only with explicit authorization note.

Each task ships green; batch into one Phase-2 PR or ship per-lesson (per STATUS cadence).

---

## Phase 3 — M37 console-rig runbook + lesson

### Task 13: Whole-rig runbook + M37 lesson

**Files:** Create `docs/runbook.md` (authoritative recovery doc, cross-links `machines/hosts/nomad-b.md`); `lesson_packs/37-rig-rescue.yaml`; `tests/test_lesson_m37.py`. Optionally `GET /api/flipper` already supplies fw/SD diagnostics; add `storage info` (FS free) to the panel for the rig-health read.

- [ ] **Step 1:** write `docs/runbook.md` — Diagnose (won't boot / panel-dark EDID pin / kiosk down / radio missing / Flipper not mounting / meshtasticd / battery-UPS / tailnet / lab-guard), Restore (SD backup+restore, config from `machines/hosts/nomad-b.md`, meshtastic identity on spec5, re-flash, re-pair), Update (RPi OS/apt, kiosk app, Flipper fw 1.4.3→). Acceptance = a real dry-run of at least the diagnose checks on nomad-b, recorded.
- [ ] **Step 2:** author `37-rig-rescue.yaml` (walks the high points; ≤~50-word bodies) + `test_lesson_m37.py`; run → PASS.
- [ ] **Step 3: SD-baseline-image GATE.** The runbook documents the restore *procedure*; **stop and get MC's explicit approval** before creating the baseline SD image (no image exists today — spec §6a). Record the chosen method (physical card-reader vs network `dd`/`rpi-clone`) and destination as MC's call; do not image the shared rig without it.
- [ ] **Step 4:** commit; HIL the diagnose checks on nomad-b; record.

---

## Phase 4 — M38 capstone + polish

### Task 14: M38 "The nomad bench" + unit polish

**Files:** `lesson_packs/38-nomad-bench.yaml`; `tests/test_lesson_m38.py`; any `console.html`/css polish for the combined panel.

- [ ] **Step 1:** write failing `test_lesson_m38.py`.
- [ ] **Step 2:** author the capstone (Flipper as field tool, nomad as analyzer/library; standalone vs plugged-in recap) with the combined live-mirror panel.
- [ ] **Step 3:** run full `pytest -q` + all node tests + `ruff` → green/pristine.
- [ ] **Step 4:** HIL the full unit at 800×480 on nomad-b; update STATUS; push; final PR; squash-merge; prune the worktree.

---

## Self-review notes

- **Spec coverage:** §4 unit → Tasks 3/4/6–14; §5 seam → Tasks 1–2; §6 rig/runbook → Task 13; §6a SD-image → Task 13 Step 3 gate; §7 constraints → Global Constraints; §8 testing → each task's tests + HIL steps; §9 phasing → Phases 1–4.
- **Review Focus** inputs are pinned to Task 1 (port busy / app-open / short read / missing key) and the Phase-2 template (malformed saved file).
- **Type consistency:** `FlipperConsole`, `FlipperError`/`FlipperSelectionError` (reused from the transport), `parse_device_info`/`DeviceInfo.as_dict`, `build_flipper`, `/api/flipper[/<C>]`, `uses_flipper`, `deviceLine` used consistently across tasks. No `FlipperLink`/`FlipperUnavailable`.
- **Proportion:** Phase 1 is detailed because its interfaces are novel; M30–M36 are one template × 7 parameterics (DRY), not seven transcripts.
