# SIGINT 101 — Learnings

The running ledger of surprising discoveries. Capture inline the moment you learn something
non-obvious (doc-flywheel skill: Capture → Promote → Clear). Dated section headers, newest
first; bold the finding, then the context.

## 2026-10-27: M11 reads LoRa off the Nomad's own meshtasticd — node B is the M5 Cardputer
**Finding.** The Nomad already runs `meshtasticd` on its SPI LoRa radio (a MeshAdv-Mini sx1262,
US 900 MHz, config `/etc/meshtasticd/config.d/lora-MeshAdv-Mini-900M22S.yaml`), with the standard
TCP API on `0.0.0.0:4403`. M11 reads that API rather than touching the radio — the `meshtastic`
python lib's `TCPInterface(hostname, portNumber=4403)` plus a `pub.subscribe(_on_receive,
"meshtastic.receive")`; each MeshPacket dict carries `rxSnr`/`rxRssi`, and a *negative* rxSnr that
still decoded is the lesson. The LoRa radio is separate from the RTL-SDR, so there is NO contention
with the sweeper/decoder (no release/resume). Added `meshtastic` to requirements and installed it
into both venvs (the droplet's shared one and nomad-b's own). lazy-import it inside the connect so
the pure parser tests run without the lib. **node B = the M5 Cardputer** (ESP32-S3, USB id
`303a:1001`, on `/dev/ttyACM0` where the Flipper used to be; `spec5` is in `plugdev`). It is itself a
Meshtastic node (US region 1, LongFast preset 0, default primary channel) so its packets decode on
node A. Drive it from the droplet with `meshtastic.serial_interface.SerialInterface(devPath=
"/dev/ttyACM0")` then `iface.sendText("...")`. **HIL-verified end to end:** node B sent 3 texts,
node A received and decoded all 3 on `/api/lora` with real SNR.
**Bench range cannot show a below-floor (negative) SNR.** At ~cm separation and 1 W (tx_power 30),
RSSI was -12 dBm and SNR +6 dB — a genuinely strong link; lowering tx_power cannot drive SNR
negative while RSSI stays ~100 dB above the noise floor. A real below-floor capture needs distance:
carry the (battery, keyboard-equipped) Cardputer far from the Nomad and send from its keys — node A
still receives over RF, and `/api/lora` surfaces the weak reception. One HIL fix the render caught:
the panel was listing the node's OWN telemetry (sender present, no `rxSnr`) — a no-rxSnr packet is
locally originated, not an air reception, so `parse_lora_packet` now skips it.
**How to apply.** For a meshtasticd feature, read its 4403 API with the `meshtastic` lib; a real air
reception always carries `rxSnr`, so filter on it. To drive node B, SerialInterface + sendText over
`/dev/ttyACM0`. A below-floor SNR is a field measurement, not a bench one.

## 2026-10-27: headless --window-size is NOT a faithful 800x480 — force the viewport or the fit lies
**Finding.** `chromium --headless=new --window-size=800,480 --screenshot` lays the page out in a
viewport whose height is NOT 480 (measured `window.innerHeight` = 337 on nomad-b), then scales the
shot — so a render that "fits" there is checked against a shorter screen than the real kiosk, and a
render that overflows the true 480 can still look fine. This is why earlier predict-fit checks were
unreliable: re-measured at a *forced* 800x480, M9's shipped predict put its Commit button at 513 px
(33 px below the fold) and M10's at 582 — both "verified" before but actually overflowing. The
honest render drives DevTools `Emulation.setDeviceMetricsOverride {width:800,height:480,
deviceScaleFactor:1}` over the stdlib-WebSocket client before measuring/screenshotting; then
`window.innerHeight` reads 480 and `getBoundingClientRect().bottom <= 480` is the real fit test.
Root cause of the predict overflow itself: the eyebrow reprinted the full lesson `concept` on the
predict stage (4 lines), on top of the prompt + frozen frame + chips. Dropping the concept on
predict (it already rode the frame) pulled Commit to 474 for both M9 and M10; M10 also needed a
2-line prompt, one-phrase chip labels, and a compact 104px frozen decoder.
**How to apply.** NEVER trust a headless `--window-size` screenshot for an 800x480 fit claim — force
the viewport with `Emulation.setDeviceMetricsOverride` first, then assert the Commit/Next button's
`getBoundingClientRect().bottom <= 480`. Predict is the tightest stage: keep prompts ~2 lines, chip
labels one short phrase, and watch the eyebrow (no repeated concept). **FOLLOW-UP:** M8's predict
still sits 1 px over (481) — its prompt is ~60 words / 5 lines, over the 35-40 guideline; trim it.

## 2026-10-27: M10 reads FSK with rtl_433 — a decode panel that takes turns on the dongle
**Finding.** FSK's two tones sit tens of kHz apart but the sweep uses 125 kHz bins, so FSK does not
resolve on the waterfall — M10's instrument is the rtl_433 decoder instead (`panels: [decode]`,
MC's call). `sigint101/decode.py` (a Decoder sibling of Sweeper) owns one long-lived `rtl_433 -F
json` process and parses each line into a Decode (model, mod, freq_hz, **freq2_hz** for the FSK
mark/space pair, snr, readings). The catch: rtl_433 and rtl_power both need the one RTL-SDR
exclusively, so `/api/decode` calls `sweeper.release()` (release, not stop — a lazy spectrum poll
would otherwise reclaim it mid-decode) before starting the decoder, and `/api/spectrum` releases the
decoder and `resume()`s the sweeper to take the dongle back. The console renders the decode panel
instead of the waterfall when `lesson.uses_decoder`, driven by `static/decode.js`. **HIL-verified on
nomad-b:** the live panel listed a real ambient Acurite-Tower sensor (18.2 °C, 51 % RH, mod ASK) —
the decoder instrument works end to end. **But ambient 433 at nomad-b is all ASK/OOK** (Acurite temp,
Smoke-GS558 beacon), so the FSK-specific two-tone read is NOT captured — it needs an actual FSK
transmitter on-frequency (a real FSK sensor, or a crafted Flipper 2-FSK via `tx_from_file`), the same
way M9's blink needed a live OOK source.
**How to apply.** rtl_433 FSK reads carry `freq1`/`freq2` (not `freq`); that pair IS the lesson.
A new SDR-exclusive tool (rtl_433, rtl_fm) must `release()` the sweeper and be released on the way
back, or the lazy /api/spectrum restart steals the dongle. For M10's FSK payoff capture, stage a
real FSK source — ambient ISM here won't oblige.

## 2026-10-27: the Flipper CLI drives sub-GHz TX from the droplet — it WAS the charge-only cable
**Finding.** Swapping the Flipper's USB cable for a data cable made it enumerate on nomad-b exactly
as predicted: `0483:5740` in `lsusb`, `/dev/ttyACM0` (root:dialout 660; `spec5` is in `dialout`),
a clean `cdc_acm` attach in `dmesg`, product "Flipper Lle0zik". System `python3` has `pyserial`
3.5 (the `.venv` does NOT — use system python for serial). Full CLI works over 115200: `device_info`
(Flipper Zero, official fw 1.4.3, region US, `radio_alive: true`) and, crucially, `subghz tx <3-byte
key hex> <freq Hz> <te us> <repeat> <device: 0=CC1101_INT>` — e.g. `subghz tx 0x00F7F7F7 433920000
350 60 0`. So the droplet can key a 433.92 OOK transmitter on demand. **A `subghz tx` left running
when the serial port is closed can leave a command pending; send `\x03` (Ctrl-C) + CR to abort and
confirm the `>: ` prompt returns before trusting the device is idle (it was not stuck keying — the
PA stops at burst end — but the CLI read was mid-command).**
**How to apply.** Flipper-over-USB features are now unblocked on nomad-b. Open `/dev/ttyACM0` with
system `python3`+`pyserial`; drive the CLI by writing `<cmd>\r\n` and reading after a short settle.
Only one process can hold the port — stop any background TX task before a script that needs it.

## 2026-10-27: a LIVE dynamic panel render needs a REAL-time wait, not --virtual-time-budget
**Finding.** Capturing the M9 waterfall with a *lit* carrier exposed that `chromium --headless
--virtual-time-budget=N --screenshot` snapshots an EMPTY waterfall ("WAITING"): virtual time races
the page's `setTimeout` polls but does NOT wait for the real `rtl_power` sweeps (~1 row/sec) to land
over the network, so 0 rows are painted at snapshot. (The prior empty-band captures looked fine only
because empty is empty regardless of row count.) Pre-warming the buffer doesn't help either —
`waterfall.js` auto-POSTs `/api/tune` on load and `retune()` ALWAYS clears the buffer (no same-band
short-circuit), wiping the warm rows. The fix that worked: drive DevTools directly. nomad-b has no
node/wscat/websocket libs, so a ~60-line **pure-stdlib WebSocket client** (raw socket, manual
`Sec-WebSocket-Key` handshake, masked client frames, 126/127 length + continuation reassembly) sends
`Page.captureScreenshot` after a genuine `time.sleep`. One foreground ssh runs: launch chromium with
`--remote-debugging-port=9222 --remote-allow-origins=*` (newer Chrome rejects the ws upgrade without
the allow-origins flag), poll `/json` for the page target, real-sleep while rows accumulate, capture,
kill chromium — all in one call so the process lives the whole time. Newest sweep paints at the TOP,
scrolling down (`drawRow`), and colour is normalised per-row (min/max eased), so a no-carrier row is
greenish noise, not black. To capture "the carrier that vanishes" in one frame: key ON ~14s (lit rows
fill), stop keying, wait ~9s (quiet rows push in at top over the red carrier band below).
**Near-field overload:** the Flipper sitting on the SDR whip is so strong it overloads the RTL front
end and smears energy broadband-red across the whole 2 MHz span instead of a clean 433.92 column
(real overload + OOK keying splatter — honest, not invented). A crisp single column needs the Flipper
moved ~1 m off the antenna (physical).
**How to apply.** For any live-data panel with real async backing, do NOT trust `--virtual-time-budget`
to wait — use the stdlib-WS + `Page.captureScreenshot` + real-sleep recipe. Reuse the band-clear fact:
the page re-tunes (and clears) on load, so the carrier must be live DURING the chromium session.

## 2026-10-27: before M9 every instrument lesson ran the FM default sweep, whatever its copy said
**Finding.** The Sweeper defaults to 88-108 MHz (`DEFAULT_LOW/HIGH`) and nothing was per-lesson:
`lesson_view` passed no `tuned_hz`/`step_hz`, so `console.html`'s `|default(100300000)` stood for
every lesson, and the live `/api/spectrum` sweep stayed on the global FM band. So M6's copy ("tune
up to 1090 MHz") rode an FM waterfall — a latent copy-vs-instrument mismatch no test caught. The
manual tune buttons step 100 kHz, so a learner cannot hand-walk from FM to 433 MHz either. Fixed by
a per-lesson `tune: {center_hz, span_hz, step_hz?}` (`8ed4c3e`): `lessons.py` validates the band at
load against `TUNER_MIN/MAX_HZ` + `MIN/MAX_SPAN_HZ` (imported from spectrum.py — no cycle), the
route passes centre/step/edges, the live waterfall canvas carries `data-tune-low/high`, and
`waterfall.js` POSTs `/api/tune` once on load (reusing the existing retune-clears-the-canvas path).
**How to apply.** Any lesson off the FM band MUST declare `tune:` or it silently shows FM. The pure
bit (`parseTuneBand`) is node-tested; the DOM read + POST is browser-only (verified by HIL render).
To HIL-verify the retune: run the server ON nomad-b (live SDR), commit a prediction to unseal
observe, render `/lesson/N` — the axis + dial read the band. The *empty* band is the honest state
when nothing is keying; a signal needs a real transmitter on that frequency.

## 2026-10-27: the Flipper does not enumerate as USB CDC on nomad-b (charge-only cable suspected)
**Finding.** With the Flipper "plugged in" to nomad-b there was no `/dev/ttyACM*` and `lsusb` showed
only the RTL-SDR (`0bda:2838`) and the MeshAdv HAT (`0484:5750`) — not the Flipper's `0483:5740`
(STM32 virtual COM) — and `dmesg` logged no recent CDC attach. Almost always a charge-only USB
cable (very common with the Flipper) or USB not "connected" on its screen. M9's live OOK demo does
NOT need the CDC: the Flipper transmits at 433.92 over the air and the RTL-SDR hears it over the air,
so hand-operating it works; the USB link only matters to drive its TX from the droplet (the plan's
host-driven FAP / an automated blink capture). **433 ISM at nomad-b:** ambient OOK is intermittent —
a 15s `rtl_power` sweep caught one +40 dB burst above floor, otherwise quiet; `rtl_433` is NOT
installed there (M10's dependency, a system install).
**How to apply.** For a Flipper-over-USB feature, first confirm `0483:5740` in `lsusb` and a
`/dev/ttyACM*` on nomad-b — if absent, it is the cable, not the code. For an OOK blink capture you
need a real transmitter on-frequency during the render window; don't expect ambient 433 to oblige.

## 2026-10-27: the instrument narration is a ~27-65px scroll band — the eyebrow concept was eating it
**Finding.** At 800x480 the instrument stage's narration (`.instrument > .stage-panel__body`) is a
tiny scrolling band: the fixed chrome (status bar, stage rail, eyebrow, title, the 120px waterfall
row, the tune strip) consumes almost the whole 480px. Measured with a throwaway same-origin iframe
fit-view that flips a hidden `.stage-panel[data-stage=...]` visible and prints
`scrollHeight - clientHeight`: the shipped M2 demo (78 words) left a 46px band and scrolled 157px;
M7's demo left only **27px** (one line) because its eyebrow reprinted the full lesson `concept` and
wrapped to 3 lines. The eyebrow `Step N of M · STAGE · <concept>` repeats on EVERY stage, so the
concept (already shown on the frame) was the single biggest band-eater on the tight instrument
stages. Dropping it there (keeping it on frame/predict/explain + no-hardware stages) grew the band
27px -> 65px (~one line -> two) uniformly, zero content lost (`85d126d`). Copy-trimming the narration
helps proportionally but cannot make a dense demo "fit" — a 150-180-word demo still scrolls ~450px in
a 65px band. The narration is a scroll band BY DESIGN (592bf84); trimming reduces the scroll, it does
not eliminate it.
**How to apply.** Treat any instrument-stage prose as scrolling (~1-2 lines show at once), and spend
the fixed vertical budget carefully — don't repeat chrome (like the concept) on stages that already
showed it. To measure a stage's real scroll, render a throwaway same-origin fit-view (iframe to
`/lesson/N`, flip the target `data-stage` panel visible, print `body.scrollHeight - body.clientHeight`)
and screenshot it on nomad-b. **Delete the fit-view before committing.**

## 2026-10-26: Harness installed

- **Feature-factory harness seeded (TDD + self-learning + self-documenting, solo footprint).**
  Hooks, pipeline/self-learning commands, and six skills are in place; this ledger is the
  self-learning substrate — append findings here, promote stable ones to the relevant doc.

## 2026-10-26: parallel workers sharing a schema need identical field names
**Finding.** Two parallel workers built against a shared "choices" contract from separate
briefs: the console's `lessons.py` parser required `{key,label}` and fails closed (refuses an
entry missing `key`), while the M1 pack was authored with `{label,value}`. Each branch's suite
passed alone; once both merged, M1 refused to load and its tests went red — a latent contract
conflict a clean textual merge does not catch.
**How to apply.** When fanning out parallel workers that share a data contract (a field name,
an enum, a file format), pin the exact names in every brief, and run the FULL suite on the
integration branch before landing, never only per-branch.

## 2026-10-26: YAML coerces yes/no choice keys into booleans
**Finding.** Authoring M7's predict `choices` as `{key: yes, label: Yes}` / `{key: no, label: No}`
silently loaded as `key='True'`/`'False'` with labels `'True'`/`'False'`: PyYAML parses YAML 1.1,
where `yes|no|Yes|No|on|off|true|false` are booleans, and `lessons.py` then `str()`-coerces them.
The loader's key+label check still passed (both non-empty), so the test stayed green while the
learner would have seen "True / False" chips instead of "Yes / No".
**How to apply.** Quote any lesson-choice key or label that is a YAML 1.1 boolean word
(`yes/no/on/off/true/false`, any case) — e.g. `{key: "yes", label: "Yes"}`. **The loader now fails
loud on this** (`ca3ba48`): `parse_lesson` refuses a bool choice key/label with a message telling you
to quote it, so an unquoted word is caught at load instead of silently becoming a "True"/"False"
chip. (The guard is bool-only — numeric keys still str-coerce; eyeball those.)

## 2026-10-26: ☭ (U+262D) renders via system fallback, not our bundled fonts
**Finding.** The eyebrow/predict hammer-and-sickle (`content:"\262D"` / `&#9773;`) renders fine on
nomad-b, but NOT because of our self-hosted fonts: the PT Mono @font-face `unicode-range` lists only
Latin/Cyrillic blocks and excludes U+262D, so the browser uses a system monospace fallback (DejaVu
Sans Mono, which has the glyph). The offline-font guarantee does not cover it.
**How to apply.** A glyph outside every bundled @font-face `unicode-range` depends on the renderer's
system fonts — it can tofu on a different target even when it renders on your dev box. Before kiosk
ship, confirm U+262D on the real Pi image's fontconfig, or bundle an inline SVG. Verify symbol
glyphs on the ACTUAL target, not just the render host.

## 2026-10-26: a nohup background process dies when the Bash tool call returns
**Finding.** `nohup .venv/bin/uvicorn ... &` from a Bash tool call looked launched but left no
process and no log — the backgrounded child is torn down when the tool's shell exits. Cost a couple
of confused retries.
**How to apply.** For a server/daemon that must outlive the call, use the harness background runner
(run_in_background), not `nohup ... &` inside a one-shot Bash call.

## 2026-10-26: predict is the tightest screen at 800x480 — keep prompts short
**Finding.** The predict prompt is rendered as the stage `<h1>` title; a 75-word prompt in the
Russo One display face pushed the tap chips and Commit button ~200px below the fold (Commit
unreachable on the fixed kiosk). Chips (72px touch) + Commit + eyebrow + frozen frame already fill
the ~360px stage area, leaving almost nothing for a multi-line title.
**How to apply.** Write predict prompts to ~35-40 words. The fix that landed: trim the prompt, hold
the title at the 18px floor (predict-scoped font-size), and reclaim chrome height (statusbar
`--bar-h` 72→66, rail `--rail-h` 48→40). Render-check every new lesson's predict screen at 800x480.

## 2026-10-26: HIL render recipe for a LIVE panel (server on nomad-b, not the droplet)
**Finding.** The waterfall and the gain meter are LIVE (poll `/api/spectrum`). A droplet-served
render shows the honest "no radio" state — the droplet has no SDR. To see a live panel with real
data the server must run ON nomad-b (its checkout is `/home/spec5/sigint-101`, has a `.venv` and
`rtl_power`). Recipe that worked: (1) on nomad-b `git fetch origin <branch> && git checkout -b
<branch> FETCH_HEAD`; (2) hold the server open as a droplet background task:
`ssh spec5@100.108.20.20 'cd /home/spec5/sigint-101 && exec .venv/bin/uvicorn sigint101.app:app
--host 127.0.0.1 --port 8008'` (backgrounding uvicorn *inside* a one-shot ssh fails with exit 255 —
hold the connection open instead); (3) warm the sweep: `curl /api/spectrum` then wait ~8s for real
rows; (4) to show a hidden instrument stage, POST a prediction (`/api/predict?lesson_id=N`) so the
first SEALED stage (observe) becomes the visible one, then screenshot `/lesson/N` DIRECTLY — a
headless iframe wrapper renders blank, but a real stage renders; (5) restore nomad-b: kill procs,
`git checkout main && git branch -D <branch>`, remove any scp'd wrapper. ssh to nomad-b is flaky on
long/compound commands (intermittent 255) — keep each ssh call simple and retry.
**How to apply.** Any live-data panel (gain/provenance/ppm, the waterfall) is a HIL check — verify
structure + no-radio on the droplet, then the live reading on nomad-b with this recipe.

## 2026-10-26: the demo/try/observe instrument stages were over-subscribed at 800x480
**Finding.** Only home/FRAME/predict were ever render-verified. The instrument stages (waterfall +
tune + a long "I-do/you-do" narration + Next-step footer + title + eyebrow) overflowed 800x480 on
EVERY instrument lesson (reproduced on M1, which has no gain panel) — the narration spilled over the
controls. Measured: 0px was left for the narration after the fixed chrome. Fixed (`592bf84`) by
moving Next-step onto the tune strip, compacting the tune, scrolling the narration, and trimming M2's
copy.
**How to apply.** Render-verify EVERY stage of a lesson at 800x480, not just the first — a stage
that is hidden-by-default (demo/try/observe behind the predict gate) is easy to never look at. The
other instrument lessons (M1,M3,M4,M6,M7,M8) still need their copy trimmed (they scroll meanwhile).

## 2026-10-26: a per-stage live panel must drive ALL its panels, not querySelector the first
**Finding.** prov.js used `document.querySelector("[data-prov-panel]")` and updated only the first
match. But an M3 lesson renders the panel on every instrument stage (demo/try/observe), and the
first in the DOM is the hidden demo stage — so the VISIBLE (observe) panel never updated and showed
its placeholder "—". The waterfall already avoids this with `querySelectorAll` (one poll feeds all
canvases, so a hidden stage is already correct when you step to it).
**How to apply.** Any live panel driven by a poll must `querySelectorAll` and update every instance,
exactly like waterfall.js — a lesson shows the same panel on each of its instrument stages.
