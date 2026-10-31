# Flipper Zero module — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a comprehensive Flipper Zero unit (U10, M29–M38) to SIGINT-101, with the nomad console as a live mirror of the device over its USB serial CLI.

**Architecture:** A new `FlipperLink` backend owns `/dev/ttyACM0` on nomad-b and exposes read-only device state to the kiosk — mirroring the `RayhunterMonitor` pattern (separate device, injected transport seam, `*Unavailable` refusal, no SDR arbitration). The live mirror is SD-file-driven: the learner saves a capture on the device, the console pulls and parses it; `device_info`/`loader`/`storage` are always-on reads. Lessons are device-in-hand, taught + verified on the 800×480 panel.

**Tech Stack:** Python 3 / FastAPI / Jinja2 / pyserial (already on nomad-b) / vanilla JS panels + node tests. SQLite progress unchanged.

**Spec:** `docs/superpowers/specs/2026-10-31-flipper-module-design.md` (read it alongside this plan).

## Global Constraints

- Target: nomad-b (`spec5b`), Flipper on `/dev/ttyACM0`, official fw 1.4.3. Dev loop: author in this droplet worktree → push branch → on nomad-b `git fetch && git checkout -f <branch>` → run uvicorn → HIL over `ssh spec5@100.108.20.20`.
- **No fake/mock mode in the product.** The only test double is the injected serial-transaction seam (`transact`); a fake returns canned CLI text. Acceptance is real device output.
- **TDD for logic:** parsers are test-first against **real captured fixtures** (CLI text / saved `.sub`/`.nfc`/`.ir`/RFID/iButton files pulled from the device), the way AIS used the gpsd vectors. Never edit a test to fit a bug.
- The Flipper is its own USB device: **not** in `sdr_consumers`, **no** `_claim_sdr`. It owns the serial port exclusively (must not fight qFlipper).
- 800×480 fixed; lesson copy shrinks to fit (reading body ≤~50 words); instrument lessons render demo Next≈423 / predict Commit≈474; never scroll, never ask.
- ABOUTME 2-line headers on every `.py`; evergreen names; `ruff` clean (≤100-col); `tests/` green and pristine.

## Review Focus

- **Port held by qFlipper / another reader** → `start()` must raise `FlipperUnavailable` ("busy"), not hang or return stale data. (Task 1.)
- **An app is open on the Flipper** → a direct CLI read returns `this command cannot be run while an application is open`; `FlipperLink` must surface that as unavailable-for-now, not parse it as data. (Task 1.)
- **A partial/short serial read** (prompt not yet returned within timeout) → the transaction returns what it has without blocking forever, and the parser rejects an incomplete block rather than inventing fields. (Task 1.)
- **`device_info` with a missing/renamed key** (fw variant) → parser yields the keys present, never KeyErrors. (Task 1.)
- **A saved capture file that is empty or malformed** → the capability parser raises/returns empty, the panel shows honest-empty, no invented capture. (Phase 2 per-capability tasks.)

---

## Phase 1 — Seam + `/api/flipper` + M29 (the shippable core)

Proves the whole live mirror end-to-end on real hardware: the console shows real `device_info` and answers "is the Flipper plugged into the nomad?".

### Task 1: `FlipperLink` + `device_info` parser

**Files:**
- Create: `sigint101/flipper.py`
- Test: `tests/test_flipper.py`
- Fixture: `tests/fixtures/flipper/device_info.txt` (real CLI output captured from nomad-b)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class FlipperUnavailable(RuntimeError)`
  - `@dataclass(frozen=True) class DeviceInfo` with fields `model: str`, `name: str`, `firmware: str`, `region: str`, `radio_alive: bool`, `raw: dict` and `as_dict() -> dict`.
  - `parse_device_info(text: str) -> DeviceInfo` — pure; parses the `key : value` block.
  - `APP_OPEN_MARKER = "cannot be run while an application is open"`, `EXT_CMD_MISSING = "failed to load external command"`.
  - `def _default_transact(port: str, command: str, timeout: float) -> str` — opens the serial port, writes `command + "\r\n"`, reads until the `>: ` prompt or timeout, returns the body with the echoed command and prompt stripped. Raises `FlipperUnavailable` on `serial.SerialException` (absent/busy port).
  - `class FlipperLink` — `__init__(self, *, port="/dev/ttyACM0", transact=_default_transact, which=shutil.which, timeout=3.0)`; `running` property; `start()` (one `device_info` probe; raises `FlipperUnavailable` if the port is absent/busy or pyserial missing); `stop()`; `device_info() -> dict` (raises `FlipperUnavailable` if unreachable); `loader_state() -> dict` (parses `loader info` / `open?`); helper `_cmd(command) -> str` that raises `FlipperUnavailable` when the response contains `APP_OPEN_MARKER`.

- [ ] **Step 1: Capture the fixture.** From the droplet: `ssh spec5@100.108.20.20 python3 - < .claude/tmp/fprobe.py` already prints `device_info`; save the real block to `tests/fixtures/flipper/device_info.txt`. (It is device output, not invented.)
- [ ] **Step 2: Write failing tests** in `tests/test_flipper.py`:
  - `test_parse_device_info_reads_core_fields` — load the fixture; assert `model == "Flipper Zero"`, `name == "Lle0zik"`, `firmware == "1.4.3"`, `region == "US"`, `radio_alive is True`.
  - `test_parse_device_info_tolerates_missing_key` — feed a block missing `hardware_region`; assert it parses and `region == ""` (no KeyError).
  - `test_start_raises_when_port_absent` — `transact` raises `FlipperUnavailable`; assert `start()` propagates it and `running is False`.
  - `test_cmd_app_open_is_unavailable` — `transact` returns a string containing `APP_OPEN_MARKER`; assert `_cmd("rfid")` raises `FlipperUnavailable`.
  - `test_device_info_via_fake_transact` — a fake `transact` returns the fixture text; assert `FlipperLink(transact=fake).device_info()["firmware"] == "1.4.3"`.
- [ ] **Step 3: Run** `./.venv/bin/pytest tests/test_flipper.py -q` → FAIL (module/functions missing).
- [ ] **Step 4: Implement** `sigint101/flipper.py` to pass. Parser: split lines on the first `:`; keep a `raw` dict; map the known keys; `radio_alive` from `raw.get("radio_alive") == "true"`.
- [ ] **Step 5: Run** the tests → PASS; `ruff check sigint101/flipper.py tests/test_flipper.py` clean.
- [ ] **Step 6: Commit** (`git add sigint101/flipper.py tests/test_flipper.py tests/fixtures/flipper/device_info.txt`).

### Task 2: `/api/flipper` route, factory, stub

**Files:**
- Modify: `sigint101/app.py` (add `build_flipper` factory + `GET /api/flipper`, following the `build_rayhunter` / `/api/rayhunter` try/except shape — NOT `_claim_sdr`).
- Test: `tests/test_app.py` (add `_StubFlipper`, register `build_flipper`, route tests).

**Interfaces:**
- Consumes: `FlipperLink`, `FlipperUnavailable` from Task 1.
- Produces: `build_flipper(...) -> FlipperLink`; `GET /api/flipper` → `{"available": bool, "device": {...} | None, "error": str | None}`.

- [ ] **Step 1: Write failing route tests** in `tests/test_app.py`: `_StubFlipper` with a `device_info()` returning a canned dict and a mode that raises `FlipperUnavailable`; assert `/api/flipper` returns `available:true` + device when present, and `available:false` + `error` (HTTP 200) when it raises. Add `/api/flipper` to the honest-unavailable route list (NOT the SDR-arbitration contract list).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the factory + route in `app.py`, wrapping `FlipperUnavailable` like `/api/rayhunter`.
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

**Files (per lesson X / capability C):** `sigint101/flipper.py` (+`parse_<C>(text|file) -> record`), `tests/test_flipper.py` (+parser tests vs a real fixture), `tests/fixtures/flipper/<C>.<ext>` (a real saved file pulled from the device), `sigint101/app.py` (+`GET /api/flipper/<C>`), `tests/test_app.py` (+stub/route), `sigint101/static/flipper.js` (+`<C>Line`/renderer) + `tests/flipper.test.js`, `sigint101/lessons.py` (+panel), `sigint101/templates/console.html` (+branch), `lesson_packs/X-<slug>.yaml`, `tests/test_lesson_mX.py`.

**Steps (per lesson):** (1) capture the real fixture from the device (`storage read /ext/<C>/<saved>` or an idle CLI read); (2) write failing parser + route + lesson tests; (3) run → FAIL; (4) implement parser + route + panel + pack; (5) run → PASS, `ruff`/node green; (6) commit; (7) HIL on nomad-b: present a real object (fob/card/remote) → save on device → console mirrors it; record evidence.

**Instances:**

- [ ] **Task 6 — M30 Sub-GHz** (`C=subghz`, file `.sub`): parse the Flipper `.sub` (Filetype/Version/Frequency/Preset/Protocol/RAW_Data or key). Mirror shows frequency + modulation + protocol. HIL: capture a real remote.
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
- **Type consistency:** `FlipperLink`, `FlipperUnavailable`, `parse_device_info`, `DeviceInfo.as_dict`, `build_flipper`, `/api/flipper[/<C>]`, `uses_flipper`, `deviceLine` used consistently across tasks.
- **Proportion:** Phase 1 is detailed because its interfaces are novel; M30–M36 are one template × 7 parameterics (DRY), not seven transcripts.
