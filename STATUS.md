# SIGINT 101 — STATUS

## NEXT ACTION (resume here)
**First, deploy current `main` to the reference Recon** while MC isn't using the panel.
This restarts the server. Main includes #114 (`3fa2720`) and the provision-test fix (`8b47053`)
as of 2026-11-27. Record the deployed SHA before starting the checks. The last recorded
deployment was ux-polish at `4ac1e67` on 2026-11-16, the same tree as #15's `02c2095`.

**Then run the pending kiosk checks.** The board records no Recon run for #93–#113 as of
2026-11-27, and #114's merge message records none. Use the deployed build and record which
hardware and theme each result covers. The test and setup rows apply where the needed tools
are present or at the next spare-SD provision.

| PR | Check after deployment |
|---|---|
| #88 | On a stalled live waterfall, press Restart sweep. Check that it recovers with the retained rows, and that a failed restart shows its reason without cutting the caption. Check for key flash and a wedged process on repeated presses. #97 covers the first-row badge. |
| #93 | On a supported Node, run `make selftest` on the Recon and confirm the wrapper runs each JS file separately and requires a real passing test in each. |
| #94 | Disconnect the separate headless Chromium while a fit or scroll check is waiting on DevTools. Both checks should exit 1 with the connection-closed message. |
| #95 | At spare-SD provisioning, check the Python 3.10+ prerequisite and that bootstrap warns and continues if jq is missing. |
| #96 | Check `deploy/provision.sh --check` on the Recon and the JS runner on a supported Node. On an image with missing or old Node, the provisioning check should report it and the kiosk should still open. |
| #97 | After a gain step, retune, crash, ppm capture or Restart sweep, check that the badge reads WAITING before the first row of the current sweep and LIVE once that row arrives. |
| #98 | Run APT with a serial-selected RTL-SDR. Read back the command's `--source_id` and `--gain`, and compare the serial and rack gain with the run's settings. |
| #99 | Check the decoder API when its device survey comes back empty: the refusal names a signal. The waterfall keeps its sweep-specific reason. Check the visible No radio badge with #113. |
| #100 | Start APT with the network unavailable. Check that SatDump starts capture with the local TLE override and does not wait on CelesTrak. |
| #101 | Kill `rtl_fm` during an APRS decode and retune at once. Check listings, the reception log and the retry reason for the new run. Repeat as traffic permits on ACARS, DMR/P25, POCSAG and RDS. |
| #102 | Run `make render-check` with the console and Chromium available. Check fit, home scroll and the exam figure at 800x480. Confirm a clipped control or an unreachable console fails the command. |
| #103 | After a real APT pass, check each image's reception for the SatDump command and frequency of that capture. Cover both a timed pass and one ended by the console when available. |
| #104 | Stop or hand off ADS-B, AIS and Flock during traffic, including Flock on the Marauder bridge. Check that the next run gets no stale items or failure, and that AIS fragments do not join across runs. |
| #105 | Test and documentation check. Run the exam tests for SQLite cleanup. If reviewing a stored pager reception, check that readback clears the text and rebuilds the summary from its framing. |
| #106 | Exercise a real Marauder reboot. Check for the last frame before the boot text, the restart reason, and no frames from the boot text itself. |
| #107 | Start, stop and restart live sweeps. Check that stdout and stderr close once the reader finishes, with the failure reason still available if the sweep dies. |
| #108 | When a poll or a release observes a natural exit, check that queued final items drain before retirement. Cover an `rtl_fm` pipeline, the ADS-B and AIS feeds, and Flock. Check that an explicit stop still cuts off the retired reader. |
| #109 | Test only. Check the fake-child tests under `python -X dev` for unclosed stderr files. |
| #110 | Swap the selected RTL-SDR between captures and read the logged radio, command and frequency. If a report arrives late, it should still name the first run's radio. Check the recorded interface on Flock as well. |
| #111 | Try a failed Complete lesson save and Retry save. Check one emblem on both exam screens, arrow/Page key scrolling in the rack picker, and the left-aligned active tile code and Unit 4 grid at 800x480. |
| #112 | Read the panel in dark, sunlight and red-night at 800x480, including the dark sunlight waterfall. Check dim text, warning and status badges, and clipping at the larger text sizes. Check the ADS-B labels with #114. |
| #113 | Check the receiver states and retry or provision hints, the ADS-B count and APT dial on refusal, and distinct LIVE and SAMPLE colours. In Predict, Check, exam and practice exam, try Commit before and after a choice and check the status and lack of a browser validation pop-up. |
| #114 | Render every interactive lesson stage at 800x480 with Next or Commit in view, especially M4/M9 Predict, M2/M3 Check and M18 with a long Flock refusal. Check Home's next-row peek, whole prose lines, cues that disappear at the end, the selected rack instrument, 72 px Back/Exit/Theme and the three-second Exit confirmation. Check live ADS-B labels and centred refusals with whole provision commands. |

The provision fix (`8b47053`) is test-only. Its check is `tests/test_provision.py`, including the
pipe-drain regression.

Before the older live captures, clear any remaining render scratch (see follow-ups). Run the
L1, L2 and L8 key captures (`docs/audit/captures/2026-11-03/`) with the course closed and inside
the SDR flock, and re-render L2, L7 and L8 at 800x480.

## Review and sprint record

**Outside review of main (2026-11-15):** 70 findings. By #15's merge message (`02c2095`), 37 of
the 67 that could be ruled on were already fixed on ux-polish, by the verifiers' reading of the
code, and reached main with it. The remaining fixes are being merged as small pull requests.
Merged 2026-11-16 (UTC): #47 (lesson 5: Part 15 asks more than a power limit), #48 (a caution on
the ledger's antenna-off record) and #49 (lessons 11 and 28: what a second node needs before it
sends). Also merged that day, as separate requests rather than review findings: #50 (the owner's
name, tailnet addresses and home paths out of the tree; its guard test covers the addresses and
home paths, not the name), #51 (the write hook's password and API-key checks now fire), #52 (the
identity guard checks the name too, on local commits and merges) and #53 (the Wi-Fi capture,
weather-sensor ID and Flipper name in the test fixtures are made up).

**Phase 3: fix sprint against MC's cut line (2026-11-02).** Plan of record:
`docs/plans/2026-11-02-resume-audit-handover.md`.
- Phases 1–2 are done. The audits are in `docs/audit/`; the triage page is in the private notes.
- **MC's decisions, verbatim (2026-11-02):**
  - Gift box: "Their own Recon (Recommended)".
  - M15 pagers: "Framing live, text hidden (Recommended)". Show cap code, rate and length live
    on 929–932, mask the text, teach the statute.
  - Cut line: **"1–19 plus operator mode"**. That is triage items 1–19 (all S0 plus the cheap
    S1s, item 8 being real tuning and gain) plus `/operate` v1, all before 11-08.
- **Sprint base:** `ux-polish` with main merged in. Sprint branches stacked on it. ux-polish
  itself merged to main on 2026-11-16 (UTC): PR #15, squash `02c2095`. Its last merges were #45
  (two environment-dependent tests fixed) and #46 (a page's words withheld on every frequency).
- **The sprint finished on `ux-polish` at `494e268`, then reached main in #15.** All
  of the cut line except item 19:
  - #18 exam: 0 hidden Commit/Next across all 409 questions, plus Skip and answers that are final.
  - #19 shell: two-way nav and resume, no concept wall, room for narration, teaching-order
    codes, hardware badges, touch targets.
  - #20 exam keyboard scroll.
  - #21 lesson truth: M15 framing-only on 929.6125, 13 facts fixed, scored predicts with
    answer/verdict, site constants via env, SSTV 437.550.
  - #22 decoder truth: dead decoders show STOPPED, process sessions with parent-death kill and
    KILL escalation, a claim lock, the command strip, ppm/LoRa/AIS/events fixes.
  - #23 waterfall: measured bins only (unmeasured bins are gaps), real tuning and gain
    (HIL-proven), red-night all red, themed 404.
  - #25 operator mode v1 (`/operate`, the rack):
    - 16 instruments, free tuning, per-decoder frequency override within band limits;
    - a reception log in `events` with provenance;
    - panel partials shared with the lessons (render diff of 0 pixels);
    - centre-bin offset, so M9's 433.92 is a measured bin;
    - Sweeper back-off.
    - HIL: rtl_433 retuned in the rack, and a real Acurite reading (16.6 °C) logged.
  - #26 home: the exam and operator bars sit side by side (MC's call: "Bars side by side
    (Recommended)").
  - #27 polish:
    - the command strip at 18 px, with the whole panel as its tap target;
    - sunlight axis at 11.7:1;
    - gain-meter labels never overlap;
    - rtl_433 repeats collapse to one reception (HIL: 27 decodes became 9 receptions).
  - **Remaining in the cut line:** item 19 (prove provision on a spare SD, then cold boot).
- **MC's decision, verbatim (2026-11-03): "Desktop + course on top (Recommended)".** The box boots
  to the Pi desktop (raspi-config B4) with the course auto-opened full-screen. Home's **Exit
  SIGINT 101** closes it, leaving the desktop: Wi-Fi menu, terminal, files, on-screen keyboard.
  A **SIGINT 101** desktop icon and menu entry reopen it. The watchdog reopens only after a crash,
  never after Exit. Built on `desktop-exit` (PR against ux-polish). The switch was proved on the
  reference Recon in #32. The install steps are in Reference Recon / kiosk operations below.
- **Since the sprint (2026-11-02/03; its code went into `ux-polish`, in main since #15):**
  - #29 provision: Flock monitor mode is opt-in (`--with-flock-adapter`). On the reference Recon the
    RTL8821CU dongle in monitor mode browned out USB (192 over-current events in 2 min, 23
    touch-panel resets). It needs a powered hub; MC unplugged it.
  - #32 **desktop + course on top** (MC's call: "Desktop + course on top (Recommended)").
    - lightdm desktop autologin (B4), with the course opened by an XDG-autostart watchdog.
    - Home's Back is now **Exit SIGINT 101**: it closes the course to the Pi desktop.
    - A desktop icon and menu entry reopen it.
    - HIL on the reference Recon: cold boot opens the course, Exit lands on the desktop, the icon
      reopens it.
  - #33 **desktop theme** (MC's call, 2026-11-03):
    - phosphor wallpaper (an LED-matrix ☭ over "ШЭLСФМЭ СФМЯДDЭ");
    - a black bar with a green rule, green frames and labels, a green-on-black terminal;
    - the course icon in Soviet red.
    - Applied live on the reference Recon; MC saw it on the panel.
  - Flipper: firmware 1.5.1; the 7 approved apps are installed (#31, `docs/flipper-apps.md`).
  - **Flipper app cleanup** (MC, 2026-11-03: "just remove any apps that aren't ours or directly
    related to the Elmer stuff"):
    - 31 old catalog apps were removed from `/ext/apps`, all built for API 87.1 or older, which is
      the "out of date app" error. Each was backed up first and removed only after its checksum
      matched the backup. The backup directory `flipper-apps-removed/` holds the copies and
      SHA256SUMS.
    - Kept: firmware 1.5.1's own apps (checked against the official resources bundle), our 8,
      and the WiFi Marauder companion. Marauder was rebuilt at catalog pin `5dc2fd68` for API
      88.2; its MD5 matches and it launched.
  - Wi-Fi devboard: flashed with Marauder (hash-verified) to be the Flock radio in place of the
    dongle (MC's call, plan in #30).
    - It sat in ESP32 download mode until a reset. MC has since unplugged and re-seated it; that
      it booted is not checked.
    - The flock.py Marauder capture source is built (#40) and off by default
      (`SIGINT101_FLOCK_SOURCE=marauder`). It is NOT verified on the device: the bridge's
      `-if02` port and live frames are untested.
  - Antennas (MC, field kit): the ANT500 moved to the SDR jack, and a 915 MHz whip went on LoRa.
    FM SNR fell from 27 dB to 5.5 dB. Re-measure with the ANT500 fully extended.
  - MC ran `provision.sh` on the reference Recon (2026-11-02).
- **Also merged into `ux-polish`, 2026-11-03:**
  - #36: the bar's green rule runs the full width.
  - #37: the real battery from the UPS daemon.
    - Open question for MC: the daemon is custom to the reference Recon, so the gift box shows "—".
  - #38: Flipper module U10/M29. It supersedes #5, which is closed.
  - #39: explains ≤120 words and narration ≤60, enforced by a test.
  - #40: the Marauder Flock source.
- **Lesson item quality** (MC, 2026-11-03: "the quality of the questions is extremely poor. I've
  gotten every one wrong"):
  - MC's record: L1–L4, 4 of 4 missed.
  - The review is in `docs/audit/2026-11-03-item-review.md`: the method, a per-item rubric and
    rewrites. The blind expert pass is in `docs/audit/2026-11-03-item-blind-expert.md`.
  - **5 predicts were broken before #43:** L1, L2, L3, L7 and L8. The keys came from theory,
    rather than the screen. L4 was sound.
  - At that review there was no post-lesson check, so teaching quality was unmeasured.
  - **MC's call, verbatim: "Fix 5 + reframe + U1 check (Recommended)".** Both parts are merged
    into `ux-polish`:
    - **#42:**
      - the reveal reads "Your call: X · What the radio does: Y", or "In fact: Y" for
        `predict_kind: knowledge`, with no score on a predict (`correct` is still stored);
      - a scored post-lesson **check** on L1–L4 ("Right" / "Not yet" plus a why per chip);
      - `tests/test_item_lint.py` (one key, stem ≤45 words, chips within 2×, no giveaway, with a
        waiver table).
    - **#43:** L1, L2, L3, L7 and L8 rewritten. All pass the lint with no waivers.
  - **Keys still provisional (NOT VERIFIED on the real waterfall):**
    - L1 "Narrower", L2 "Rises with it" and L8 "Released push-to-talk". The kiosk held the dongle,
      and then the reference Recon went offline.
    - The capture scripts are in `docs/audit/captures/2026-11-03/`. Run them when the reference
      Recon is reachable and the course is closed. If the data disagrees, re-key to match the data.
    - L3 "No, they look alike" needs MC's antenna-off step (see Needed from MC).
  - Not yet re-rendered after the merge: L2, L7 and L8's new chips under #42's reveal header.
- **Known small follow-ups (not in the cut line):**
  - #112 and #113 set readable waterfall badge colours in sunlight. The panel check is above;
  - the red-night image filter is verified on a test pattern only;
  - LoRa staleness isn't proven live;
  - FLEX paging isn't decoded;
  - DMR repeat de-dup is unverified (no capture);
  - the RDS station is a provisioning value (no auto-pick);
  - Home's second unit row previously tucked about 20 px under the bars. #114 adds the next-row
    peek and scroll cue. The panel check is above;
  - the M19 Rayhunter panel now has a bounded reason line (#61) and the shared UNAVAILABLE
    state (#113). It still needs the live Orbic check;
  - in the tcpdump Flock path, a pcap parse error ends the reader while tcpdump keeps running,
    so the panel reads LIVE with nothing reading;
  - scratch to remove on the reference Recon once it's back:
    - the temporary `theme-wt` worktree and `fapchk` and `items-iq` directories;
    - the headless Chromium with profile `items-prof`, if it's still running (kill by PID).
- **Original hand-over target: 2026-11-08.**

## Current state
- **Main through #114 and `8b47053` (2026-11-27):** the 28-lesson curriculum (U1–U9), plus
  M29 (U10) in #15 and the changes in Shipped below. The 2026-11-02 checkpoint included:
  - #6: exam diagrams, render harness
  - #7, #9: kiosk autostart with the browser watchdog
  - #10, #11: `provision.sh` and the quick-start
  - #12: status bar never invents readings; waterfall pixelated
  - #13: audit reports
- **Kiosk cold boot PASSED (observed 2026-11-03 06:18 UTC).** After a reboot, the reference Recon
  came up with the server service and labwc, ran one kiosk browser, and showed the course home on
  the panel (grim screenshot). The persistent journal kept the previous boot.
- **The earlier branches (checked 2026-11-16 UTC):** each had merged, closed, or reached main
  through another branch. Current reviews are under Open work.
  - `ux-polish` (PR #15) makes lessons completable and makes the reading stages and home grid
    scroll. It merged to main on 2026-11-16 (UTC): **on main a learner can now complete a lesson.**
  - `flipper-module` (PR #5) is U10 Phase 1 (M29 + the `/api/flipper` live mirror). It is
    closed: #38 superseded it.
  - `flipper-module-refresh` (#38, merged into ux-polish 11-03 UTC, so in main since #15) merges
    ux-polish into #5 and supersedes it; HIL waits on the Flipper.
- **Reference Recon health at the 2026-11-02 checkpoint:**
  - No crash since the 11-01 reboot. Configs intact: meshtasticd `config.yaml`, `config.txt`
    (gpio4, pps-gpio 17, miniuart-bt), root fs clean.
  - The earlier crash left no log because the journal was volatile; it is persistent now.
  - The fan noise is mechanical: it needs a physical look (dust, reseat or replace).

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
| ux-polish sprint | M1–M29 | `02c2095` #15 (squash-merged 11-16 UTC) |
| Review burn-down | M5 | `e6b77c3` #47 Part 15 conditions |
| Review burn-down | — | `dc9a2c6` #48 ledger caution on the antenna-off record |
| Review burn-down | M11, M28 | `b2beb4e` #49 what a second node needs |
| Review burn-down | Waterfall | `95bfee8` #88 (C24) A Restart sweep key recovers a stalled `rtl_power` and keeps the measured rows. |
| Review burn-down | — | `86fdc69` #94 (Q04) Fit and scroll checks exit on a DevTools disconnect, so a lost browser cannot hang them. |
| Review burn-down | — | `72dbe46` #95 (D07) Bootstrap warns about missing jq and the README names Python 3.10+ for the app's annotations. |
| Review burn-down | — | `079d47d` #96 (D05, D06) Provisioning warns about missing or old Node and the JS tests enforce the 20.11 floor (21.2 on 21.x), so the kiosk can run without Node. |
| Review burn-down | Waterfall | `5bf7621` #97 (C32) The first-row badge waits for the current sweep, so retained rows cannot make a fresh run read LIVE. |
| Review burn-down | M24 | `9e89fee` #98 (C34) APT passes SatDump its real gain flag and the selected RTL-SDR's USB serial, so the tuner matches the rack. |
| Review burn-down | Decoders | `cdfb128` #99 (A47) Empty-bus decoder refusals say “signal” where there is no waterfall. |
| Review burn-down | M24 | `66781fc` #100 (C35) APT passes a local TLE path so an offline CelesTrak fetch cannot hold up capture startup. |
| Review burn-down | M12, M14–M16, M27 | `15b2632` #101 (C31 PR 1) ACARS, APRS, DMR, POCSAG and RDS guard their readers so retired runs cannot report into replacements. |
| Review burn-down | — | `c151e02` #102 (Q01) `make render-check` runs fit, scroll and figure checks and fails when the render, console or browser fails. |
| Review burn-down | M24 | `e7c7e04` #103 (C36) APT saves the command and frequency for each pass so its images carry provenance even when reported after exit. |
| Review burn-down | M13, M18, M26 | `ec332c0` #104 (C31 PR 2) ADS-B, AIS and both Flock sources guard old readers. Each AIS run assembles its own fragments. |
| Review burn-down | M18 | `81a3178` #106 (C41) Marauder delivers the final capture bytes before its reboot text so a frame in the same read is kept. |
| Review burn-down | Guarded decoders | `3a76466` #108 (C39) Settling a natural exit stops the stages and drains queued output within a bound, then retires the run and closes its transport. |
| Review burn-down | Guarded decoders, M24 | `71a7212` #110 (C38) Receptions carry their launch radio so a late item keeps the radio that heard it. |
| Separate request | — | `a78e45a` #50 owner's name, tailnet addresses, home paths out; guard test (addresses, home paths) |
| Separate request | — | `d21d620` #51 write hook's password and API-key checks |
| Separate request | — | `63eb64c` #52 identity guard checks the name too |
| Separate request | — | `545a207` #53 Wi-Fi capture, weather-sensor ID and Flipper name in the test fixtures made up |
| Separate request | — | `dd5ce58` #93 Each JS test file must pass a test it defines, so an empty file cannot ride another file's pass. |
| Separate request | Exam trainer, M15 | `ad0e0e4` #105 Exam tests close their SQLite stores. The pager docstrings name the text and derived summary that readback withholds. |
| Separate request | Waterfall | `6dfa667` #107 The sweep closes its temporary stderr file on a failed launch and both streams when the reader finishes, after reading any exit reason. |
| Separate request | — | `02c66e9` #109 Fake children keep diagnostics in memory so tests stop leaking temporary stderr files. |
| Separate request | Lessons, exam, rack | `ec9a85a` #111 A failed completion stays put with Retry save, each exam gets one emblem, the keyboard scrolls the rack picker, and the tile code and Unit 4 scroll are fixed. |
| Separate request | All screens | `730f8d8` #112 (UX 3, 6) Three themes reach 4.5:1 for meaningful ink, the CSS text floor is 13 px with instructions and status values at 18 px, and scanlines sit behind the text. |
| Separate request | Lessons, exam, rack | `ae11e37` #113 (UX 2, 4) Refusals say why the receiver is unavailable and how to proceed. Commit waits for a choice with a status line in the form. |
| Separate request | Home, lessons, rack | `3fa2720` #114 (UX 1, 5) Scroll cues and whole text lines show where more follows. Back, Exit and Theme are 72 px, Exit takes two taps, and ADS-B labels sit outside the scaled SVG at 13 px. Refusals are centred and commands stay whole. |
| Separate request | — | `8b47053` The provision test's fake `sudo tee` drains its pipe, so a fast stub cannot give the writer SIGPIPE under load. |

## Open work (2026-11-27)

- **Built, in review:**
  - C31 PR 3: SSTV stale-reader guard. Awaiting independent review.
  - C42, round 2: sweep the process group before reaping its leader.
  - C43: ISM receptions carry the radio, command and frequency at launch.
- **Planned, not built:** C40 is in plan review (retire a dead run without another start, let
  downstream stages finish and move the waits out of the shared radio lock). C37 covers a
  dump1090 that survives kill and its fixed SBS port.
- **Waiting on MC:** UX items 7–12 (progress rail, tune strip grouping, rack chooser, predict
  prompts in the body face, status bar and lesson copy).
- **Earlier open items:** A46 needs a plan to use A43's class on the other panels. C20 (Meteor)
  waits on the bench items. C26 waits on the kiosk. C33 (the Flipper OSError hang) is paused.
  P01 is still open, as are the hook items C17–C19 followed by D04.

**Needed from MC, one at a time when reached:**
1. **L3's key, a physical step.**
   - Record an empty 20 MHz span outside FM with the antenna on.
   - MC unscrews the SDR antenna, not the LoRa one.
   - Record the same span again, then MC screws the antenna back on.
   - Steps are in `docs/audit/captures/2026-11-03/capture.py`.
   - Also a ten-minute think-aloud on L1–L4: why "wider", why "smaller".
2. Check Marauder answers: in the Flipper's WiFi Marauder app, Scan APs. Then put GPIO →
   USB-UART Bridge on channel 1 to prove the `-if02` port and live Flock frames.
3. Extend the ANT500 fully, then re-measure FM SNR.
4. One look at the panel, to confirm #15's work there. It was PR #15's gate, in the PR's
   words "Draft until MC confirms on the panel (MC's gate)"; #15 merged to main on 2026-11-16
   (UTC) with no look recorded on the PR. The current deployment and panel checks are in
   NEXT ACTION.
5. Battery on the gift box: ship the reference Recon's daemon, or read the INA219 directly
   (read-only).
6. The recipient Recon's arrival date, and a spare SD card for the reference Recon. The provision
   proof is triage item 19.
7. Paste the 11-01 written feedback list. Only 2 items from it made it into the record.

## Unfinished-work register (found 2026-11-02; disposition set at triage)
This is the disposition of the plan deliverables found at triage, including those since built.
Sources are the owner's private plan, CLAUDE.md and the triage record.

| Item | Where promised | State | Disposition |
|---|---|---|---|
| Lesson completion | plan core | on main since #15 (2026-11-16 UTC) | land (S0) |
| Kiosk autostart | P0 DoD | **done** (#7, #9); cold boot passed 2026-11-02 | — |
| Provisioning + scrub of MC's data + recipient quick-start | P5/P6 | provisioning and quick-start on main (#10, #11); repo scrub and guards (#50, #92) | prove the fresh SD and recipient image at hand-over |
| Updater (atomic apply, rollback, migrations) | P5 | provisioning can fast-forward the checkout; atomic apply, rollback and migrations remain open | triage |
| Safe-shutdown + real battery readout | plan arch | #12 shows "—" without a reading; UPS INA219 readout (#37, via the battery daemon) on main since #15 | battery on the gift box: decision pending; shutdown: triage |
| `safety.py` antenna-before-TX guard + any TX path | D16, CLAUDE.md | never existed; no TX route | triage |
| Reception badges / replay marks | CLAUDE.md invariant | no code; `events.py` logs only | triage |
| SigMF recorded walkthroughs R1–R6, sample recording | plan curriculum | none | triage |
| Retrieval cards, challenge tiers, solo capstone | plan assessment | none; all packs are `intro` | triage |
| "First 15 minutes" boot flow + band map | plan | none | triage |
| M5 12-card sort + "show me the law" | plan | 3-choice predict only | triage |
| GPS + Wi-Fi/BLE survey lessons | plan U5 | replaced by POCSAG/ACARS, never recorded | triage |
| Flock passive BLE scan | plan CS | Wi-Fi only | triage |
| Flock provisioning (tcpdump caps, wlan1 monitor) | CS2 HIL | caps in `provision.sh`; the dongle browns out USB, so monitor mode is opt-in (#29); Marauder devboard is the replacement radio (#30) | capture source built (#40); RESET and live bridge proof still to check |
| ☭ glyph comes from a system-font fallback | restyle | unconfirmed on a fresh image | provisioning check |
| M8 predict 1px over at 800x480 | M8 HIL | measured before #43 rewrote the chips | re-render the current layout |
| Flipper Companion FAP (Track E) | 10-29 roadmap | superseded 10-31 by the USB-CLI live mirror (`flipper-module` spec §11) | confirm dropped at triage |
| Kit add-ons (DS3231 RTC, nRF52840, hub) | plan hardware | not bought | MC |
| Cold-boot panel fix, LoRa pin fix | plan arch | status unknown on the reference Recon | check |
| SD baseline image | flipper spec §6a | does not exist | MC-gated |
| Garden → rack → field (command strip, `/operate`, field card, solos) | MC's question 2026-11-02 | designed in the plan of record | triage |

## Deferred live HIL (hardware or physical, not code)
Every pipeline below is HIL-proven "honest-empty" on the reference Recon. A live capture needs:
- **ADS-B M13:** a 1090 MHz antenna (the reference Recon's is deaf there: 0 CRC-valid of 411k
  preambles).
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

## Reference Recon / kiosk operations
- **Access:** `ssh <user>@<reference Recon's address>`. Its login user, host name and address
  are in the owner's private host notes.
- **The panel is self-starting** (`deploy/install.sh`, idempotent; re-run it after changing
  `deploy/`).
  - The lesson server is the user unit `sigint101.service` on 127.0.0.1:8008, with linger.
  - What main's `deploy/install.sh` sets up since #15 (2026-11-16 UTC): the XDG autostart
    `~/.config/autostart/sigint101.desktop` → `/usr/local/bin/sigint101-course watch` (no labwc
    of its own), and a menu entry and desktop icon that run `sigint101-course open`; exit flag
    `$XDG_RUNTIME_DIR/sigint101-kiosk-exited`. It removes the tty1 kiosk it set up before #15
    (the `~/.profile` hook and `~/.local/bin/sigint101-kiosk`, labwc + a looping Chromium).
    `provision.sh` boots the box to the desktop with autologin.
  - In the last recorded Recon deployment (#32, from `ux-polish`), the box autologs in to the Pi
    desktop. The XDG autostart runs `/usr/local/bin/sigint101-course watch`, which reopens the kiosk Chromium
    (profile `~/.config/chromium-kiosk`) unless Exit wrote
    `$XDG_RUNTIME_DIR/sigint101-kiosk-exited`. The desktop icon runs `sigint101-course open`.
  - **Restart the course browser:** kill the kiosk Chromium by PID and the watchdog reopens it.
    Never kill `labwc` (that ends the whole desktop session) and never restart `getty@tty1`
    (duplicate sessions, see learnings).
  - **Restart the server:** `systemctl --user restart sigint101`, which also reloads lesson
    YAML.
  - **Manual server on 8008:** `systemctl --user stop sigint101` first.
- **Deploy an already-published branch** (`main` for NEXT ACTION): on the reference Recon,
  `cd ~/sigint-101 && git fetch origin <branch> && git checkout -f -B <branch> FETCH_HEAD`,
  then restart the server. The clone has no `origin/<branch>` refs: use FETCH_HEAD.
- **Screenshot the real panel:**
  `XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-N grim /tmp/panel.png`. N climbs
  after restarts.

## Render-verify (the UI test; no browser on the droplet)
- **Harness:** `tools/render/` (stdlib CDP websocket, viewport forced to 800x480).
  - `make render-check`: runs the checks with a separate headless Chromium against a scratch
    console it starts on a free port with its own data folder; `RENDER_URL` names a running
    console instead. A check blocks the console's `/api/*` only while its DevTools session is
    attached, so each one leaves the page on about:blank before it closes and the runner holds
    its own block for the whole run; a page that cannot reach about:blank fails the run, and a
    browser group that will not stop keeps its folder. The scratch console is built with no
    radio. Loading a page writes no learner state and retunes no receiver: the rack place, a
    lesson's open and its tuning are posts from the page.
  - `render_scroll.py`: explain scroll + Complete, home tile clipping.
  - `render_fit.py`: demo/predict button bottom.
  - `render_figure.py`: exam diagram.
  - Set `CDP_PORT` to the Chrome DevTools port.
- **Recipe:**
  1. Run the server from the worktree with the harness background runner (a `nohup` from a
     tool call dies when the call returns): `--host <droplet tailnet address> --port 80xx`.
  2. On the reference Recon, start a separate headless Chromium:
     `--headless=new --no-sandbox --disable-gpu --password-store=basic --remote-allow-origins=*
     --remote-debugging-port=93xx --user-data-dir=/tmp/<you>-prof --disk-cache-size=1`.
     **`--password-store=basic` is mandatory.** Without it the kiosk session's GNOME keyring
     blocks the first http page forever (see learnings).
  3. scp the scripts over and run them there against `http://<droplet tailnet address>:80xx/...`.
  4. Kill your Chromium by PID. Never `pkill chromium`: that kills the kiosk.
- **Always force the viewport** (`Emulation.setDeviceMetricsOverride` 800x480): a plain
  `--window-size` lays out at ~337px.
- **Always `Network.setCacheDisabled`.**
- **Reach sealed stages** by POSTing a prediction first:
  `curl -X POST ".../api/predict?lesson_id=N" -d "prediction=<key>" -H "content-type: application/x-www-form-urlencoded"`.
- **A LIVE dynamic panel** (lit waterfall) needs the server on the reference Recon and the
  real-sleep CDP capture recipe in `.claude/learnings.md`. `--virtual-time-budget` snapshots an
  empty panel.

## HIL targets
- **The reference Recon** is PRIMARY. It is a stock Nomad 2 Recon
  twin: RTL2832U (now on the ANT500; deaf at 1090 and 2m), MeshAdv HAT (`/dev/ttyAMA0`) with a
  915 MHz whip on the LoRa jack, and the Flipper (fw 1.5.1, Marauder devboard on its GPIO) on
  `/dev/ttyACM0` when plugged in. The RTL8821CU dongle is unplugged: it browns out USB without a
  powered hub.
- **The owner's second Recon** is the secondary. Its LoRa jack is not dead, and its broken antenna
  has been replaced (MC, 2026-11-16). Transmit is off and meshtasticd is disabled until MC
  re-enables them. Its reception since the new antenna is not tested.
- **A lab Pi** has an ESP32 on `/dev/ttyACM1`. No Flipper.

## Gotchas (durable)
- All `sigint101/` edits go through a worktree. Merge with `--squash` and use the configured
  repository author email. Stage files by name. Never `--no-verify`.
- Worktree setup: `ln -s <main checkout>/.venv <wt>/.venv && mkdir <wt>/data`.
  `pytest.ini` has `pythonpath = .`. Never `git add` the symlink.
- Shell is zsh: unquoted `$var` does not word-split.
- `pkill -f`/`pgrep -f` over ssh match the ssh command's own argv. Kill by PID, or use the
  `[u]vicorn` bracket trick.
- `rtl_power` printing "[R82XX] PLL not locked!" and "User cancel, exiting" is benign.
- ☭ (U+262D) renders via a system-font fallback (DejaVu on the reference Recon), not our bundled
  fonts. Confirm it on the recipient image or bundle an SVG.
- Predict is the tightest screen at 800x480: keep prompts to ~35–40 words. Reading stages
  scroll (on main since #15); instrument stages must fit (CLAUDE.md Corrections).
