# SIGINT 101 — STATUS

## NEXT ACTION (resume here)
**Phase 1: three-fold audit, in progress (2026-11-02).** Plan of record:
`docs/plans/2026-11-02-resume-audit-handover.md`, approved by MC 2026-11-02.
- Three auditor subagents are writing `docs/audit/2026-11-02-{technical,ux,curriculum}.md`:
  - technical: code vs intent, RF accuracy, Pi 4 performance, measured on nomad-b
  - UI/UX: render sweep at 800x480 × 3 themes
  - curriculum: competency map, prerequisites, fidelity, fact-check
- Next: an adversarial verifier re-checks every S0/S1 finding, then **MC triages** (sets the
  11-08 cut line), then the fix sprint (Phase 3).
- **Hand-over is 2026-11-08.** The gift is **the recipient's own Recon** (MC's call 2026-11-02).
  nomad-b stays our HIL rig, and the provisioning script gets proven on a spare SD card in
  nomad-b.

**Needed from MC, one at a time when reached:**
1. The recipient Recon's arrival date, and a spare SD card for nomad-b.
2. Re-plug the Flipper on nomad-b and update it 1.4.3 → 1.5.1 in the app.
3. One look at the panel. The kiosk is up now on `ux-polish`; confirming it unblocks that PR.
4. Paste the 11-01 written feedback list. Only 2 items from it made it into the record.
5. The triage cut line.

## Current state (observed 2026-11-02)
- **main** @ `11defa4`. The 28-lesson curriculum is on main (U1–U9).
  - Exam diagrams T-1/T-2/T-3 now show (#6).
  - The render-assert harness is in `tools/render/` (#6).
  - nomad-b boots the course onto the panel: a server user-service, a tty1 kiosk and a
    persistent journal via `deploy/install.sh` (#7).
- **Open branches:**
  - `ux-polish` (@345fbfb, no PR yet) makes lessons completable and makes the reading stages
    and home grid scroll. **On main a learner still cannot complete a lesson.** It merges once
    MC has looked at the panel.
  - `flipper-module` (PR #5) is U10 Phase 1 (M29 + the `/api/flipper` live mirror). It is
    parked behind the S0 fixes and needs a rebase after ux-polish lands (both touch `app.css`
    and `console.html`).
- **nomad-b health:**
  - No crash since the 11-01 reboot. Configs intact: meshtasticd `config.yaml`, `config.txt`
    (gpio4, pps-gpio 17, miniuart-bt), root fs clean.
  - The earlier crash left no log because the journal was volatile; it is persistent now.
  - The fan noise is mechanical: it needs a physical look (dust, reseat or replace).

## Unfinished-work register (found 2026-11-02; disposition set at triage)
These are plan deliverables that were promised but never built, and mostly never mentioned
again. Sources: `/home/mc/.claude/plans/eager-spinning-frost.md`, CLAUDE.md, the transcripts.

| Item | Where promised | State | Disposition |
|---|---|---|---|
| Lesson completion | plan core | only on unmerged `ux-polish` | land (S0) |
| Kiosk autostart | P0 DoD | **done** `11defa4` (cold reboot not yet run) | — |
| Provisioning + scrub of MC's data + recipient quick-start | P5/P6 | nothing exists | build (hand-over) |
| Updater (atomic apply, rollback, migrations) | P5 | nothing exists | triage |
| Safe-shutdown + real battery readout | plan arch | status bar shows a hardcoded **100%** (invented) | triage (S0: no-mock) |
| `safety.py` antenna-before-TX guard + any TX path | D16, CLAUDE.md | never existed; no TX route | triage |
| Reception badges / replay marks | CLAUDE.md invariant | no code; `events.py` logs only | triage |
| SigMF recorded walkthroughs R1–R6, sample recording | plan curriculum | none | triage |
| Retrieval cards, challenge tiers, solo capstone | plan assessment | none; all packs are `intro` | triage |
| "First 15 minutes" boot flow + band map | plan | none | triage |
| M5 12-card sort + "show me the law" | plan | 3-choice predict only | triage |
| GPS + Wi-Fi/BLE survey lessons | plan U5 | replaced by POCSAG/ACARS, never recorded | triage |
| Flock passive BLE scan | plan CS | Wi-Fi only | triage |
| Flock provisioning (tcpdump caps, wlan1 monitor) | CS2 HIL | manual steps | provisioning |
| ☭ glyph comes from a system-font fallback | restyle | unconfirmed on a fresh image | provisioning check |
| M8 predict 1px over at 800x480 | M8 HIL | open | fix sprint |
| Flipper Companion FAP (Track E) | 10-29 roadmap | superseded 10-31 by the USB-CLI live mirror (`flipper-module` spec §11) | confirm dropped at triage |
| Kit add-ons (DS3231 RTC, nRF52840, hub) | plan hardware | not bought | MC |
| Cold-boot panel fix, LoRa pin fix | plan arch | status unknown on nomad-b | check |
| SD baseline image | flipper spec §6a | does not exist | MC-gated |
| Garden → rack → field (command strip, `/operate`, field card, solos) | MC's question 2026-11-02 | designed in the plan of record | triage |

## Deferred live HIL (hardware or physical, not code)
Every pipeline below is HIL-proven "honest-empty" on nomad-b. A live capture needs:
- **ADS-B M13:** a 1090 MHz antenna (nomad-b's is deaf there: 0 CRC-valid of 411k preambles).
- **APRS M14:** a 2m antenna, plus local traffic.
- **POCSAG M15:** a pager-band antenna, plus traffic.
- **ACARS M16:** VHF traffic. The chain is already proven with acarsdec's own `test.wav`.
- **RDS M12 and M4 ppm:** a tuned FM antenna or a strong stereo station (the best measured
  pilot ratio is 1.5 dB; 15 is needed).
- **FSK M10:** an on-frequency transmitter. An ambient Acurite sensor was already decoded.
- **DMR/P25 M27:** a dsd-fme source build, plus a repeater in range.
- **APT M24 and SSTV M25:** builds of satdump and sstv, a 137 MHz antenna and a pass, or an
  SSTV transmission.
- **Rayhunter M19:** the Orbic powered on its Wi-Fi.
- **Meshtastic M28:** MC's second node.
- **Flipper M29+:** the Flipper plugged in (M9's carrier blink was captured 10-27).

## Shipped (main)
| Unit | Lessons | Key commits |
|---|---|---|
| U1 Instrument | M1–M4 | `e22c289` M1, `592bf84` gain meter, `53b0e79` provenance, `cdbfb53` ppm |
| U2 Rules | M5 | `bceb085` |
| U3 Antennas | M6–M7 | `b383cbb` |
| U4 Modulation | M8–M12 | `8ed4c3e` M9 + tune band, `c72a395` M10, `7a8015d` M11, `e390c5c` M12 |
| U5 Decode | M13–M16, M26, M27 | M13–M16 squash-merged 10-28, `c8aaa62` M26/M27 |
| U6 Under the Hood | M20–M23 | `23e0e3e` |
| U7 Counter-surveillance | M17–M19 | `2602469` M19 |
| U8 Images | M24–M25 | `180e879` |
| U9 Mesh | M28 | `2602469` |
| Exam trainer | — | `aa2cc5a` card, `32ec4a0` sim, `84bd0b1` home CTA, `71ad24a` diagrams |
| Platform | — | `a8bfef8` Phosphor restyle, `0e2fc54` units nav, `6b23996` `claim()` arbitration, `11defa4` kiosk autostart |

## nomad-b / kiosk operations
- **Access:** `ssh spec5@100.108.20.20`. Login user is `spec5`, host `spec5b`. Registry:
  `/home/mc/workspaces/machines/hosts/nomad-b.md`.
- **The panel is self-starting** (`deploy/install.sh`, idempotent; re-run it after changing
  `deploy/`).
  - The lesson server is the user unit `sigint101.service` on 127.0.0.1:8008, with linger.
  - The kiosk is tty1 autologin → `~/.profile` hook → `~/.local/bin/sigint101-kiosk` (labwc +
    a looping Chromium).
  - **Restart the kiosk:** kill the `labwc` PID and autologin respawns it. Never also restart
    `getty@tty1`: you get duplicate sessions (see learnings).
  - **Restart the server:** `systemctl --user restart sigint101`, which also reloads lesson
    YAML.
  - **Manual server on 8008:** `systemctl --user stop sigint101` first.
- **Deploy a branch:** push it, then on nomad-b
  `cd ~/sigint-101 && git fetch origin <branch> && git checkout -f -B <branch> FETCH_HEAD`,
  then restart the server. The clone has no `origin/<branch>` refs: use FETCH_HEAD.
- **Screenshot the real panel:**
  `XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-N grim /tmp/panel.png`. N climbs
  after restarts.

## Render-verify (the UI test; no browser on the droplet)
- **Harness:** `tools/render/` (stdlib CDP websocket, viewport forced to 800x480).
  - `render_scroll.py`: explain scroll + Complete, home tile clipping.
  - `render_fit.py`: demo/predict button bottom.
  - `render_figure.py`: exam diagram.
  - Set `CDP_PORT` to the Chrome DevTools port.
- **Recipe:**
  1. Run the server from the worktree with the harness background runner (a `nohup` from a
     tool call dies when the call returns): `--host 100.92.112.54 --port 80xx`.
  2. On nomad-b, start a separate headless Chromium:
     `--headless=new --remote-debugging-port=93xx --user-data-dir=/tmp/<you>-prof --disk-cache-size=1`.
  3. scp the scripts over and run them there against `http://100.92.112.54:80xx/...`.
  4. Kill your Chromium by PID. Never `pkill chromium`: that kills the kiosk.
- **Always force the viewport** (`Emulation.setDeviceMetricsOverride` 800x480): a plain
  `--window-size` lays out at ~337px.
- **Always `Network.setCacheDisabled`.**
- **Reach sealed stages** by POSTing a prediction first:
  `curl -X POST ".../api/predict?lesson_id=N" -d "prediction=<key>" -H "content-type: application/x-www-form-urlencoded"`.
- **A LIVE dynamic panel** (lit waterfall) needs the server on nomad-b and the real-sleep CDP
  capture recipe in `.claude/learnings.md`. `--virtual-time-budget` snapshots an empty panel.

## HIL targets
- **nomad-b** (`spec5b`, 100.108.20.20, user `spec5`) is PRIMARY. It is a stock Nomad 2 Recon
  twin: RTL2832U with a working antenna (deaf at 1090 and 2m), MeshAdv HAT (`/dev/ttyAMA0`), a
  working LoRa jack, the RTL8821CU monitor-mode adapter as `wlan1`, and the Flipper on
  `/dev/ttyACM0` when plugged in.
- **spec5** (100.90.68.20) is the deaf secondary: busted RX antenna, dead LoRa jack. Useful as
  a deaf-receiver fixture.
- **pi5** (100.126.132.9, user `mc`) has an ESP32 on `/dev/ttyACM1`. No Flipper.

## Gotchas (durable)
- All `sigint101/` edits go through a worktree. Merge with `--squash`, passing
  `--author-email mc8805@outlook.com`. Stage files by name. Never `--no-verify`.
- Worktree setup: `ln -s /home/mc/workspaces/sigint-101/.venv <wt>/.venv && mkdir <wt>/data`.
  `pytest.ini` has `pythonpath = .`. Never `git add` the symlink.
- Shell is zsh: unquoted `$var` does not word-split.
- `pkill -f`/`pgrep -f` over ssh match the ssh command's own argv. Kill by PID, or use the
  `[u]vicorn` bracket trick.
- `rtl_power` printing "[R82XX] PLL not locked!" and "User cancel, exiting" is benign.
- ☭ (U+262D) renders via a system-font fallback (DejaVu on nomad-b), not our bundled fonts.
  Confirm it on the recipient image or bundle an SVG.
- Predict is the tightest screen at 800x480: keep prompts to ~35–40 words. Reading stages
  scroll (on `ux-polish`); instrument stages must fit (CLAUDE.md Corrections).
