# SIGINT-101 technical audit — 2026-11-02

**Scope.** I audited `main` at **c22f7d9** plus the unmerged `origin/ux-polish` (345fbfb) and `origin/flipper-module` (41b37a4). I measured on **nomad-b** (spec5b, Pi 4 8 GB, R828D on Debian librtlsdr 0.6.0-4) from a detached worktree at `/tmp/audit-tech`, serving on 127.0.0.1:8028. I didn't touch the kiosk checkout or port 8008. Code citations are `file:line` at c22f7d9.

**After the audit, `origin/main` moved to 3538592.** It gained a server service (#7), a fix for the kiosk respawn storm (#9) and `deploy/provision.sh` (#10, #11). Branch `instrument-honesty` (4151223, not merged) fixes T-02. I didn't re-audit any of these. I mark findings they may touch.

**The measurement window was polluted.** From 21:05 to 21:20 PST (nomad-b clock) the coordinator's kiosk-Chromium respawn storm drove load to 20.8. **I re-took every CPU, RSS, latency and diskstats number below after load fell below 1.2 (21:23 onward).** That covers the idle/spectrum/provenance runs, all 17 route scenarios and the events-scaling timings. The decoder and other-route scenarios ran at load 1.3–3.9, because other sessions' Chromium renders were still running. Per-process numbers barely move with that load, but latency figures carry the caveat.

Measurements I did **not** re-take (none of them depend on timing):
- The H1 pixel render, at about 21:18.
- The first APRS and rtl_433 dead-child drills. Their facts are API bodies and PIDs. The two ~9.5 s request latencies from those drills are discarded as polluted. A clean re-test of the same restart took 132 ms, and clean consumer switches took 0.38–0.92 s.

**Cleanup is done.** I removed `/tmp/audit-tech` and the scripts dir, killed every one of my processes by PID, and confirmed with `rtl_test -t` that the dongle opens. The kiosk uvicorn (PID 3762) was left alone, and so was `/tmp/uxverify`.

## Findings table

| ID | Sev | Finding | Evidence type | Cost |
|---|---|---|---|---|
| T-01 | **S0** | Dead decoder child shows as LIVE and empty, respawns every poll. **Flock is in this state on nomad-b right now.** | live + code + probe | M |
| T-02 | **S0** | Waterfall canvas is smoothed: 94% of pixels on a scanline are blends no bin measured (H1) | live render | S |
| T-03 | **S0** | Rows contain synthetic bins: duplicated value at every hop join plus a trailing duplicate; real first bins are dropped; DC-null copies are unmarked (M1) | live capture + rtl_power.c | S |
| T-04 | **S0** | M12 RDS decodes "96.0 MHz, the strong local station", which is actually a narrowband spur comb. 96.0 is not a US FM channel. | live RF measurement | S–M |
| T-05 | **S0** | M25 teaches ISS SSTV on 145.800 MHz. ARISS moved SSTV to 437.550 MHz (Robot36) in 2025–26. | primary source | S |
| T-06 | S1 | Kill path is dead and there are no process groups. A hung rtl_fm leaked mid-session and wedged the dongle ("usb_claim_interface error -6") until killed. kill -9 of uvicorn orphans the children. (H3) | live + probe | S–M |
| T-07 | S1 | `/api/ppm/measure` bypasses `_claim_sdr`. M4's own waterfall restarts rtl_power mid-measure. A missing rtl_fm gives a 500 that the UI shows as "lost contact". (H4) | live + probe | S |
| T-08 | S1 | Tune +/- and tap-to-tune only change the display. Copy tells the learner to tap and tune. The tap maps ±1.3 MHz onto any span, and ppm measures that wrong readout. (H5) | code | M |
| T-09 | S1 | M2 says "step the gain", but no route or UI can change gain; `set_gain` is never called (H6). The meter's FLOOR is the hop-edge roll-off minimum, not the floor. | code + live row | M |
| T-10 | S1 | Predictions and practice-exam answers can be overwritten after the reveal (M4) | probe | S |
| T-11 | S1 | APT runs satdump `noaa_apt` on 137.1 MHz (dead NOAA-19; now Meteor LRPT) with a 900 s restart loop, and labels images "NOAA-19" (M3) | code + primary source | M |
| T-12 | S1 | POCSAG 929.6000 MHz is not an assigned channel (929.5875 / 929.6125). It sits at the edge of rtl_fm's ±11 kHz passband. | 47 CFR + code | S |
| T-13 | S1 | LoRa `running` never re-checks the connection. meshtastic 2.7.11 reconnects once, so "available: true" can freeze (M8). | code (live UNVERIFIED) | S |
| T-14 | S1 | M4's "one-tap correction" doesn't exist: no decoder applies ppm and `make ppm-reference` doesn't exist. The +164 ppm tuning claim is UNVERIFIED for this board's LO. | code + live | S |
| T-15 | S2 | events.jsonl is unbounded and re-parsed every 1.5 s: 1.33 s CPU per poll at 100k lines. A dead Flock writes 42 lines/min. (M6) | live | S |
| T-16 | S2 | AIS reader thread and UDP socket leak on every stop. Switching away from AIS stalls 3.2 s. Stale fragments can garble type-5 messages. (M7) | live + probe | S |
| T-17 | S2 | `_claim_sdr` has no lock. Two pollers thrash: 14 APRS restarts in 30 s, roster wiped each time. This produced the T-06 leak. | live | M |
| T-18 | S2 | `/api/tune` accepts 1 Hz bins over 100 MHz (M2). A 2 MHz/100 Hz tune asks rtl_power for 32,768 bins per row. | probe + rtl_power banner | S |
| T-19 | S2 | DVB kernel driver is not blacklisted, so detach/reattach happens on every hand-off. Optional tools (satdump, sstv, sox, dsd-fme) were absent at audit, so M24/M25/M27 return 503 on device. | live | S |
| T-20 | S2 | Bias-tee statement in provenance is unverifiable (the EEPROM is generic and SpecFive is silent) | primary + live | S |
| T-21 | S2 | Rayhunter makes 4 GETs per 3 s poll; status and warnings can come from different manifests (M11) | code | S |
| T-22 | S3 | Decoder lifecycle is copy-pasted: about 1,100 lines across 11 classes. T-01, T-06 and T-16 live in the copies. | count | L |
| T-23 | S3 | Smaller items: M9 tune ignored, M10 DSP, `dbm` naming, idle sweeper, zombies, `seen` dedupe, `updated` NULL, Rayhunter default host, great-circle | various | S |

### Suite health

All four trees ran green on the droplet. Coverage is enforced at 80%.

| Tree | `make selftest` | Tests | Coverage | `make lint` | Output |
|---|---|---|---|---|---|
| main c22f7d9 | pass (95 s) | 805 | 88.94% | clean | pristine (no warnings) |
| ux-polish 345fbfb | pass (94 s) | 805 + node scroll-keys | 88.94% | clean | pristine |
| flipper-module 41b37a4 | pass (102 s) | 820 | 89.07% | clean | pristine |
| trial integration (main + ux-polish + flipper octopus `--no-commit`) | pass (91 s) | 820 | 89.07% | clean | pristine |

Integration conflicts: none. `STATUS.md`, `static/app.css` and `templates/console.html` auto-merged. Test counts come from the dot rows, because pytest.ini's `-q` plus the Makefile's `-q` suppress the summary line. **No test covers any S0 above.** Every one of them passes the suite.

### Measurements on nomad-b (clean re-run, at the panel JS cadence)

| Route (cadence) | uvicorn CPU avg / 1 s max | uvicorn RSS | Child CPU | Latency p50 / p95 | Payload | events.jsonl growth |
|---|---|---|---|---|---|---|
| idle (no polling) | 0.2% / 1.0% | 56.5 MB | — | — | — | 0 |
| `/api/spectrum?limit=40` (0.9 s), FM 88–108 | **4.4% / 9.8%** | 56.5 → 58.1 MB | rtl_power 1.9 s/min (3.1%) | **45.7 / 99.1 ms** (max 110) | **53 KB p50, 77 KB at 40 rows** (257 bins/row) | 1 line per start |
| spectrum + `/api/provenance` (1.5 s) | 6.1% / 10.7% | 58.9 MB | rtl_power 2.2 s/min | prov 8.1 / 60.3 ms | prov 269 B | 0 |
| `/api/decode` (1.5 s) | 0.5% | 58.9 MB | rtl_433 0.36 s/30 s | 4.7 / 7.8 ms | 0.7 KB | 1 |
| `/api/adsb` (1.5 s) | 0.4% | 59.0 MB | dump1090 1.74 s/30 s (5.8%) | 4.4 ms; first call 977 ms | 78 B | 1 |
| `/api/aprs` (2 s) | 0.4% | 59.0 MB | **rtl_fm 8.2 s/30 s (27% of a core)** | 4.6 ms; first call 206 ms | 52 B | 1 |
| `/api/pocsag` (1.5 s) | 0.4% | 59.0 MB | rtl_fm 8.1 s (27%), multimon 0.7 s | 4.4 ms | 49 B | 1 |
| `/api/ais` (1.5 s) | 0.4% | 59.0 MB | rtl_ais 3.9 s (13%) | 4.0 ms | 81 B | 1 |
| `/api/acars` (1.5 s) | 0.4% | 59.0 MB | acarsdec 1.45 s (5%) | 4.0 ms; **first call 3,244 ms** (AIS teardown, T-16) | 52 B | 1 |
| `/api/rds` (1.5 s) | 0.5% | 59.0 MB | **rtl_fm 11.5 s (37%)**, redsea 0.7 s | 4.7 ms | 119 B | 1 |
| `/api/dmr`, `/apt`, `/sstv` | 0.3% | 59.0 MB | — (tool missing → 503) | 5–6 ms | ~100 B | **14 lines/min** (`*_blocked` every 5 s) |
| `/api/lora` (1.5 s) | 0.4% | +5.6 MB once the meshtastic lib loads | — | 4.5 ms; first connect 1.8 s | 347 B | 0 |
| `/api/flock` (1.5 s) | 0.6% | 59.1 MB | tcpdump dies at once | 7.3 ms | 54 B | **42 lines/min** (T-01) |
| `/api/provenance` alone | 0.6% | 59.1 MB | vcgencmd per poll | 8.8 / 17.4 ms | 267 B | 0 |
| `/api/ppm/measure` (8 s capture) | — | — | rtl_fm | **wall 11.2–11.6 s** | — | 1 per measure |

- **SD writes.** uvicorn's own `/proc/<pid>/io` write_bytes was 0–12 KB per 30–60 s scenario, from events lines and SQLite commits. The system-wide `/proc/diskstats` delta is dominated by other processes: 3.0 MB/min idle, and up to 240 MB/30 s during the respawn storm. The app's SD write load is negligible. **Its only unbounded growth is events.jsonl (T-15).**
- **The Pi's real CPU cost is the children, not the server.** One rtl_fm pipeline is 27–37% of a core.

---

## S0

### T-01 Dead decoder shows LIVE, respawns silently (H2; Flock dead on nomad-b now)

**Evidence**
- **Live, Flock.** `/api/flock` returned `200 {"available":true,"interface":"wlan1","detections":[]}` 21 times in 30 s, and events.jsonl gained 21 `flock_start` lines, one per poll. That is 42 of the 180 events in the audit log.
- The app's own command fails immediately on the box:
  ```
  $ tcpdump -i wlan1 -U -s 512 -w - "type mgt"
  tcpdump: 802.11 link-layer types supported only on 802.11   (exit 1)
  ```
  wlan1 is `DOWN`, link-type EN10MB, not in monitor mode, after the reboot.
- **Live, APRS drill.** I sent SIGKILL to multimon-ng. Every poll still answered `200 available:true`, a new rtl_fm and multimon-ng pair appeared with nothing reported, and the only trace was an extra `aprs_start` event. Under SIGSTOP the API also kept saying `available:true`.
- **Live, rtl_433 drill.** I SIGSTOPped, then SIGKILLed, rtl_433. The last real Acurite decode stayed in `decodes` and was served as live the whole time. The process respawned silently.
- **Live, after the T-06 crash.** The APRS route answered `200 available:true` while each new rtl_fm died at once ("usb_claim_interface error -6"), because a ghost from the previous server held the dongle.
- **Code.** The `_read` loops end on EOF without looking at the exit code: aprs.py:295–304, decode.py:227–236, flock.py:389–401, and the same shape in rds/pocsag/acars/adsb/ais/dmr/sstv. apt has no reader at all. `running` polls only the last process in a pipeline (aprs.py:179–180), so a dead rtl_fm with a live multimon-ng also reads as running. Only the Sweeper records the death (spectrum.py:489–519).
- **Probe.** With a pipeline that exits 1 immediately, AprsDecoder showed `running=False error=None` across three polls and spawned three times.

**Learner impact.** M18 tells the learner "no alert never means no camera". The instrument breaks that rule itself: it shows a live, empty monitor while the sensor is dead. Every decode lesson can show "live, nothing heard" for a radio that isn't running.

**Fix sketch.** Port the Sweeper's `_record_abnormal_death` into each reader's `finally`: a non-zero or signal exit code plus a stderr tail goes to `self.error`. Then `start()` raises it once, the route returns 503 with the reason, and the next poll can retry. Make `running` require every process in a pipeline to be alive. Stop logging `*_start` on respawn, or log `*_died` with the code.

**Failing tests**
- `test_flock_dead_tcpdump_reports_503`: spawn `bash -c 'echo "802.11 link-layer..." >&2; exit 1'`, drain; assert `/api/flock` returns 503 and the stderr text is in `reason`.
- `test_aprs_pipeline_death_sets_error`: kill the first process; assert `decoder.error` is not None and `running` is False.

### T-02 Waterfall interpolates bins (H1)

**Evidence**
- app.css:755–761 gives `.waterfall` no `image-rendering`. The canvas is sized to the bin count (waterfall.js:176–177; 257 bins on the FM band) and stretched to ~760×120 px.
- I rendered a 257-px canvas of alternating pure red and blue bins with the real app.css, using the Pi's own Chromium. On scanline y=60, **697 of 742 px were blends**: 17 distinct colours, for example (111,0,143), from a 2-colour canvas.
- The same page with `.waterfall{image-rendering:pixelated}` gave **742 of 742 pure**.
- Evidence files: tech-evidence/h1.png and tech-evidence/h1_fixed.png.

**Learner impact.** This breaks CLAUDE.md's "one pixel per bin, no interpolation, no synthetic fill". Narrow carriers smear, and colours appear that no bin measured.

**Fix.** Add one CSS line. It is already on branch `instrument-honesty` (4151223), not yet merged. Cost: S.

**Test.** `render_waterfall_is_nearest_neighbour` in the render harness: every pixel on a scanline of an alternating-bin canvas is one of the two bin colours.

### T-03 Synthetic bins at rtl_power hop joins (M1)

**Evidence**
- **Primary source.** In osmocom rtl-sdr v0.6.0 `csv_dbm()` (the Debian 0.6.0-4 binary on the box), the loop `for (i=i1; i<=i2; i++)` prints every bin, then `dbm = ts->avg[i2]...; fprintf(file, "%.2f\n", dbm);` prints the last bin a second time. Every CSV line therefore carries len+1 values, and **the last two are always identical**. The research notes have the verbatim source.
- `merge_chunks` (spectrum.py:265–267) keeps that duplicate and throws away the next hop's first value (`chunk.dbm[1:]`). That first value is the only real bin at that frequency.
- **Live capture** of 40 rows × 257 bins on nomad-b, FM 88–108:
  - At the hop-join indices (i % 32 == 0): **320 of 320 equal the previous bin**. Other neighbours: 383 of 9,920 (3.9%).
  - Final bin equals the penultimate bin: **40 of 40**.
  - rtl_power's own DC null (`avg[0]=avg[1]`, rotated to each hop's centre) shows as index 17 equal to index 16 in **320 of 320** hops.
- Per FM row, that is 9 app-made copies plus 8 unmarked rtl_power DC copies: **17 of 257 pixels (6.6%) are not measurements**, and 7 real bins are discarded.

**Learner impact.** This breaks the "one pixel per bin, no synthetic fill" invariant. M23 ("Every Pixel") teaches exactly this invariant.

**Fix sketch**
- In `parse_line`, drop the trailing duplicate (`dbm[:-1]`) and keep `freq_high = low + n*step`.
- In `merge_chunks`, keep every hop's first bin and drop the `[1:]`.
- Emit the DC bins as `null` and draw them as background or hatched. Explaining the DC spike is a teaching opportunity.

**Cost.** S.

**Tests**
- `test_parse_line_drops_rtl_powers_trailing_duplicate`: a line ending `..., -9.1, -9.1` yields n-1 values.
- `test_merge_keeps_each_hops_first_bin`: two 4-bin hops merge to 8 values, and `row.dbm[4] == hop2.dbm[0]`.

### T-04 M12's "strong local station" at 96.0 MHz is a spur comb, not a station

**Evidence (live, rtl_power at 586 Hz–1.2 kHz bins)**
- The strongest thing in the FM band is a carrier at **96.0005 MHz, 48 dB over the floor, 2 kHz wide at −10 dB and 5 kHz at −20 dB**.
- It stays at 96.000 MHz whether the hop is centred at 95.4, 96.0 or 96.6 MHz. That rules out DC and LO leakage.
- It is part of a comb on 1.2 MHz multiples: 94.8005, 96.0005 and 97.2004 MHz, plus 97.6008. 1.2 MHz is 28.8 MHz / 24, consistent with the dongle's or the board's own clocks. That source is UNVERIFIED.
- A stereo FM broadcast occupies ≥150 kHz. The genuine wideband stations were at 91.07, 91.59, 92.10 and 93.55 MHz.
- US FM channels are odd tenths only (47 CFR 73.201: "95.9 240, 96.1 241"), so no US station can sit on 96.0.
- **Code and copy.** rds.py:21 `DEFAULT_RDS_HZ = 96_000_000`. Copy at 12-rds.yaml:31: "redsea is listening to 96.0 MHz, the strong local station". learnings.md (2026-10-27 M12) attributes the empty RDS decode to the FM antenna.
- The other location-pinned constants have the same weakness: provenance.py:14 `REFERENCE_HZ = 106.5 MHz` ("this rural spot"), the dial default 100.3, and the ppm fallback 98.1 (console.html:651).

**Learner impact.** M12's live decode can't succeed at the coded frequency anywhere in the US. The lesson names a spur as a station. On the M1 waterfall, the brightest "station" a learner will tap is this spur.

**Fix sketch**
- RDS tunes to the dial or the lesson tune.
- Add a scan helper that picks the strongest **wide** peak and snaps it to an odd tenth.
- Fix the copy. Teach the comb as "spurs vs stations" (M23 material).
- Re-pick the provenance reference on the recipient's site.

**Cost.** S–M.

**Tests**
- `test_rds_freq_follows_lesson_tune`
- `test_fm_channel_snap_is_odd_tenth` (pure)

### T-05 M25 teaches ISS SSTV on 145.800 MHz (now 437.550 MHz)

**Evidence**
- ARISS (ariss.org/contact-the-iss.html): "SSTV Downlink 437.550 MHz with Robot36 mode".
- Every 2026 event series (31–34, the latest Oct 22–26) ran on 437.55 MHz in Robot36. The last 145.800 MHz PD120 event was July 14–20, 2025.
- 145.80 is still the ISS *voice* downlink.
- **Code and copy.** sstv.py:32 `DEFAULT_SSTV_HZ = 145_800_000` with the comment "2m SSTV calling frequency". 25-sstv.yaml:14, 30 and 60 say "the ISS sends its images on 145.800 FM ... often PD120".

**Learner impact.** This teaches something false. A learner waiting for an ARISS event will listen on the wrong band and in the wrong mode.

**Fix.** Change the constant, the pack `tune` and the copy to 437.550 MHz / Robot36. Note Doppler (about ±10 kHz at UHF) and point to ariss.org/upcoming-sstv-events. Cost S. On the same pass, the T-11 APT copy is already honest.

**Test.** `test_m25_pack_and_decoder_agree_on_iss_sstv_downlink`: `DEFAULT_SSTV_HZ == pack center_hz == 437_550_000`.

## S1

### T-06 Kill path dead and no process groups; a hung rtl_fm wedged the dongle (H3)

**Evidence (code)**
- `_Pipeline.wait` / `_Feed.wait` / `_DsdProcess.wait` catch `subprocess.SubprocessError`, which is the parent of `TimeoutExpired`, and return 0. That happens in aprs.py:189–195, rds.py:107, pocsag.py:128, sstv.py:121, adsb.py:227–231, ais.py:380–384 and dmr.py:178–182. So each `except subprocess.TimeoutExpired` in `_stop_locked` is dead code.
- None of those decoders, nor acars/apt/flock, ever call `kill()`. Only Sweeper and decode.py escalate.
- Nothing anywhere uses `start_new_session`, `killpg` or `PR_SET_PDEATHSIG`.

**Evidence (probe)** A real two-process pipeline that traps SIGTERM: `AprsDecoder.stop()` returned after **9.0 s** (3 + 3 + 3 s of waits), with **both children alive and `error=None`**.

**Evidence (live, in-session leak)**
- During the concurrent-claim drill (T-17), rtl_fm PID 21681 caught SIGTERM (SigCgt 0x100005006) and hung in `futex_wait_queue` for 98 s.
- The decoder dropped its handle anyway. rtl_fm kept `/dev/bus/usb/001/005` open (lsof).
- Every `/api/spectrum` then returned 503 "...usb_claim_interface error -6 | Failed to open rtlsdr device #0", until I killed the PID by hand.

**Evidence (live, kill -9 uvicorn)**
- rtl_fm 23082 and multimon-ng 23083 were reparented to PID 1, still holding the dongle.
- The restarted console flapped between `200 rows:[]` and 503 on the waterfall, and APRS answered `200 available:true` (T-01).

**Mitigation now on main.** `deploy/kiosk/sigint101.service` uses the default `KillMode=control-group`, so systemd SIGTERMs leftover children when the main process dies. A hung rtl_fm can still delay the restart by up to `TimeoutStopSec` (90 s). The **in-process** leak is not covered.

**Learner impact.** One hung child turns every radio lesson into "no radio" until a restart, and nothing tells the learner why.

**Fix sketch** One shared helper `stop_process(p)`:
- terminate, then `wait(3)`; on timeout, `kill()` and `wait(3)`; if it still won't die, set `error`.
- Wrappers re-raise `TimeoutExpired`.
- Spawn with `start_new_session=True` and kill the group.
- Add a `preexec_fn` that sets `prctl(PR_SET_PDEATHSIG, SIGKILL)`.

**Cost.** S–M.

**Test.** `test_stop_kills_child_that_ignores_sigterm`: a real `bash -c "trap '' TERM; sleep 30"` pipeline; after `stop()`, `poll()` is not None for both within 7 s.

### T-07 `/api/ppm/measure` races the page's own waterfall; 500 on missing rtl_fm (H4)

**Evidence**
- app.py:741–754 calls only `sweeper.stop()`, not `_claim_sdr`. M4's ppm panel sits beside the live waterfall, which polls every 0.9 s (console.html:641–651).
- **Live:** during an 11.6 s measure, two rtl_power processes spawned and died with status 1, the waterfall showed 503 twice, and 3 `spectrum_start` events appeared. rtl_fm won this time. If rtl_power wins the ~0.5 s window, the measure reports "capture ended before enough audio".
- If another decoder holds the radio, rtl_fm fails the same way.
- **Probe:** with no rtl_fm installed, the route returned **500 Internal Server Error**. `_default_capture`'s Popen (ppm.py:194) is uncaught. ppm.js then shows "measure failed / lost contact with the console".
- **Device selection:** the route passes the *serial* to `rtl_fm -d`. rtl_fm parses a numeric string as an index first, which is the bug `select_rtlsdr_index` (hardware.py:154–163) exists to avoid. It only bites when `SIGINT101_RTLSDR_SERIAL` is set and more than one RTL is attached.
- **Timing:** wall time is 11.2–11.6 s for 8 s of audio. The pure-Python offset search alone takes 1.6 s on the Pi (see T-23).

**Fix.** Register ppm as an SDR consumer (`_claim_sdr("ppm")` and a held release until done). Catch `OSError` and return `{"ppm":null,"reason":"rtl_fm is not installed"}`. Pass the resolved index. Cost S.

**Tests**
- `test_ppm_measure_missing_rtl_fm_is_reasoned_not_500`
- `test_ppm_measure_releases_every_sdr_consumer`

### T-08 Tune and tap are display-only; the copy asks for them (H5)

**Evidence**
- console.html:741–759 only rewrites `[data-freq]` text.
- The tap maps `(mhz - 1.3) + frac * 2.6` whatever the span is; the FM default is 20 MHz, and decoder lessons use 0.2–2 MHz.
- ppm.js:22–26 measures that readout. On a 20 MHz sweep, tapping a station sends the measure to the wrong frequency.
- The copy that relies on it:
  - 01-see-the-invisible.yaml:28 "tap a different strong one"; :33 and :37 "tune two notches up the dial"
  - 06:22 "tune up to 1090 MHz"; the sweep cannot leave FM from M6, which has no `tune:`
  - 07:40 "retune down to 915 MHz"

**Learner impact.** Core "you-do" steps can't be carried out, and the one control that does something (ppm) measures the wrong frequency.

**Fix.**
- Wire +/- and tap to `/api/tune`: re-centre the current span, with tap frequency = `low + frac*(high-low)` from the canvas `data-tune-low`/`data-tune-high`.
- Or cut the instructions.

**Cost.** M.

**Tests**
- Node `tapToHz(0.5, 88e6, 108e6) === 98e6`
- Py `test_tune_step_posts_recentred_span`

### T-09 Gain cannot be changed; meter floor is not the floor (H6)

**Evidence**
- 02-gain-and-snr.yaml:26 says "Step the gain from the bottom up". `Sweeper.set_gain` (spectrum.py:597) has no caller, route or UI. The sweep runs AGC (`gain: null` in every response).
- The meter takes `Math.min(row.dbm)` (waterfall.js:183) as its FLOOR. Real rows start with the hop-edge roll-off (−15.3, −14.3, −12.7 dB against about −9.5 dB of floor), so the FLOOR line and the SNR gap overstate.
- The API already computes the median floor (`floor_db`) and the meter ignores it.

**Fix.** Add `POST /api/gain` and a stepper. Have the meter use `floor_db`/`peak_db`. Cost M.

**Test.** `test_post_gain_restarts_sweep_with_g_flag`: after POST, `sweeper.command()` contains `-g 29.7`.

### T-10 Commit-before-reveal answers can be changed after the reveal (M4)

**Evidence (probe, real app)**
- `POST /api/predict` "a" → 303, the reveal is rendered. `POST` "b" → 303, and the stored prediction is now "b". The cause is `ON CONFLICT ... DO UPDATE` at progress.py:109.
- Practice exam: answer wrong → review shown → `POST` the correct letter → stored answer replaced, so the score goes up. The cause is exam.py:329–345.

**Fix.** Make the prediction write-once (`DO NOTHING`, then 409). Ignore a sim re-answer once the question has been answered. Cost S.

**Tests**
- `test_prediction_is_immutable_once_committed`
- `test_sim_answer_cannot_change_after_review`

### T-11 APT chases a dead satellite and mislabels (M3)

**Evidence**
- apt.py:24, 29 and 45–49 run `satdump live noaa_apt` on 137.100 MHz and label the images by frequency as "NOAA-19".
- `--timeout 900` ends the run, and the 4 s `/api/apt` poll restarts it forever (`running` is False, so it re-claims). That holds the dongle and adds T-01's silent-death issue.
- NESDIS: "NOAA-19 was decommissioned on August 13, 2025"; NOAA-15 on August 19 and NOAA-18 on June 6, 2025.
- 137.1 MHz is now a Meteor-M N2-3/N2-4 **LRPT** (digital) channel. SatDump's pipeline for it is `meteor_m2-x_lrpt`.
- M24's copy is already honest that the birds are dead. The code isn't.
- satdump was not installed at audit time, so the live path is UNVERIFIED. That includes whether a no-pass run writes a noise PNG labelled NOAA-19.

**Fix.** Switch to the Meteor LRPT pipeline, or make M24 replay-only. Don't auto-restart. Label from satdump's own products. Cost M.

**Test.** `test_apt_does_not_restart_after_capture_ends`.

### T-12 POCSAG 929.6000 MHz is between channels

**Evidence**
- 47 CFR 90.493(a) lists "929.5875, 929.6125". 929.6000 is not an assigned channel.
- pocsag.py:141 uses `rtl_fm -s 22050`, which gives roughly a ±11 kHz passband. A pager on 929.6125 lands 12.5 kHz off-centre, at or past the filter edge.

**Fix.** Use 929.6125 MHz, or the recipient's local paging channel, and follow the dial. Cost S.

### T-13 LoRa "available" can freeze after a meshtasticd restart (M8)

**Evidence**
- lora.py:134–136 sets `running = self._interface is not None` and never checks the connection.
- In meshtastic 2.7.11 on the box (tcp_interface.py:142–183), a closed socket triggers one `_reconnect()` after `time.sleep(1)`. If meshtasticd isn't back yet, `myConnect()` raises in the reader thread and the reader dies.
- The listener never subscribes to `meshtastic.connection.lost`.
- **Live drill UNVERIFIED:** restarting meshtasticd was off limits.

**Fix.** Subscribe to `connection.lost`, which should call `stop()`. Make `running` check `iface.isConnected.is_set()`. Cost S.

**Test.** Publish `meshtastic.connection.lost`; assert `running` is False.

### T-14 M4's ppm promises are unbacked; the +164 ppm tuning claim is unverified on this board

**Evidence (code)**
- No decoder applies a correction. rds.py:119 passes `-p 0`, dmr.py:33 passes ppm 0, and the others pass no `-p`/`--ppm` at all.
- 04-ppm-error.yaml:56 promises "a one-tap correction".
- 04:24 cites `make ppm-reference`, but the Makefile has no such target.

**Evidence (live)**
- `rtl_test -p` against an NTP-synced host gave cumulative **+143, +143, +147, +154 ppm**. That is the RTL2832U sample clock, and it agrees in order of magnitude with M4's +164.
- The app's pilot instrument found no pilot on 92.1, 91.1 or 93.5 MHz (ratios 0.9–7.2 dB, below the 15 dB gate). It couldn't confirm.
- The narrowband comb lines read **+0.4 to +0.8 kHz** from exact 1.2 MHz multiples. A proportional +150 ppm LO error would put them about 14–15 kHz off.
- So this board's R828D LO may **not** share the RTL2832U's crystal: stock 0.6.0 drives the R828D with a 16 MHz `R828D_XTAL_FREQ`. If so, M4's "+17 kHz high of the book" and ppm.py:149's "one crystal clocks both" are wrong here.
- UNVERIFIED: whether the comb's source clock is itself accurate.

**Decisive HIL.** Run `rtl_fm` on a known NFM carrier, such as NOAA WX 162.40–162.55, with `-p 0` and with `-p 150`, and see which one centres it. Cost S.

## S2

**T-15 events.jsonl is unbounded and fully re-parsed every 1.5 s (M6)**
- Measured on the Pi, re-taken at load ≈1.1: `read_baseline` takes 10.8 ms at 1k lines, 110 ms at 10k, **1.33 s at 100k** (89% of a core at the 1.5 s poll) and 8.2 s at 500k.
- Growth: a dead Flock adds 42 lines/min (T-01). A missing tool adds 14 lines/min (`*_blocked`, from app.py:1106 and siblings). Healthy routes add about 1 line per start.
- A Flock page left open about 2.4 days reaches 100k lines. After that, M3's provenance panel stalls and holds the GIL; the waterfall p95 suffers too.
- `/api/provenance` also forks vcgencmd every poll.
- Fix: keep baseline samples in SQLite, or cache them in memory as they append; cap and rotate the JSONL; don't log a respawn as `*_start`. Cost S.
- Test: `test_provenance_poll_does_not_reread_event_log`.

**T-16 AIS reader and socket leak; fragment mixing (M7)**
- Live: after AIS stopped, uvicorn thread 4157 sat in `recvfrom` (`__skb_wait_for_more_packets`, syscall 207) on fd 17, which was already closed. `127.0.0.1:10110` stayed bound with no owning fd.
- The `_stop_locked` join waits its 3 s on every hand-off away from AIS: measured **3,244 ms** on the next route.
- Probe: the reader thread is still alive 3 s after `_Feed.terminate()`.
- `AisAssembler` (ais.py:243–270) keys partials by `(seq_id, channel)` and never expires them. A stale part 2 plus a new part 1 decodes as a garbled type-5 report (a wrong name or callsign) shown as a real vessel.
- Fix: `sock.shutdown(SHUT_RDWR)` before close, or a 1 s timeout loop on a stop flag; expire partials older than about 2 s. Cost S.
- Tests:
  - `test_ais_feed_terminate_unblocks_reader`
  - `test_assembler_discards_stale_fragment`

**T-17 `_claim_sdr` has no global lock; concurrent pollers thrash**
- Live, 30 s of spectrum (0.9 s) plus APRS (2 s): 14 `aprs_start` and 5 `spectrum_start` events, 12 multimon-ng and as many rtl_fm processes spawned, the APRS roster wiped on every claim, and the spectrum returning 4×503 ("released", "status 1").
- This drill produced the T-06 wedge.
- Exposure: two open panels or tabs, or a late poll after navigating away.
- Fix: one claim lock held across claim and start; an ownership token per page; 409 when another lesson holds the radio. Cost M.

**T-18 `/api/tune` is unbounded in bins (M2)**
- Probe: `validate_span(24e6, 124e6, 1)` is accepted.
- Live: `rtl_power -f 95000000:97000000:100` announced "Total FFT bins: 32768 ... FFT bin size: 61.04Hz".
- At `limit=40` that is about 1.3 M floats per poll (≈9 MB JSON every 0.9 s), and a 32,769-px canvas, beyond Chromium's 32,767 px per-dimension cap.
- The end-to-end payload is UNVERIFIED: the leaked rtl_fm (T-06) held the dongle during that drill.
- Fix: cap bins per row, for example `(high-low)/bin ≤ 2048`, in `validate_span`. Cost S.
- Test: `test_validate_span_refuses_more_than_2048_bins`.

**T-19 Device provisioning gaps (partly fixed after the audit)**
- At audit time satdump, sstv, sox and dsd-fme were absent, so M24/M25/M27 return honest 503s on the device.
- Main now has `deploy/provision.sh --with-optional` (#10/#11), not yet run on nomad-b.
- **Still open:** `dvb_usb_rtl28xxu`, `r820t` and `rtl2832_sdr` are loaded and not blacklisted (`/etc/modprobe.d` has no rule, and provision.sh has none). Every SDR hand-off detaches and reattaches the kernel driver ("Detached/Reattached kernel driver").
- Fix: add `blacklist dvb_usb_rtl28xxu` to provisioning. Cost S.

**T-20 Bias-tee statement is unverified**
- provenance.py:22 says "internal SDR has no bias tee".
- The EEPROM reads Realtek / RTL2838UHIDIR / 00000001, generic defaults. It is not a Blog V4, which reads "RTLSDRBlog"/"Blog V4" and does have a bias tee.
- SpecFive publishes only "Built-in RTL-SDR".
- Plausible but unproven. It needs a meter on the SMA centre pin with `rtl_biast -b 1`, a physical step for MC.
- Fix: until then, word it as "not verified". Cost S.

**T-21 Rayhunter makes four GETs per poll (M11)**
- rayhunter.py:280–301: `status()` does manifest plus analysis report, and `warnings()` does manifest plus analysis report again. The full report is fetched twice every 3 s from a small device.
- The two calls can disagree if the recording rotates between them.
- Not measured, because no Orbic was reachable (192.168.1.1:8080 refused).
- Fix: one manifest and one report per poll. Cost S.

## S3

**T-22 Duplicated decoder lifecycle**
- Counted with ast: **1,094 lines** of lifecycle methods (`__init__`, running, released, `_choose_device`, release, resume, start, `_start_locked`, `_read`, stop, `_stop_locked`, drain) across 11 decoder classes. That is 76–115 lines each, plus 210 in the Sweeper.
- Also: 104 lines of 7 near-identical process wrappers, 11 `build_*` factories, and 11 near-identical claim/start/503 route bodies (app.py:806–1140).
- The three process defects (T-01, T-06, T-16) are all in the copies that missed the Sweeper's fixes.
- **Recommendation:** before hand-over, add only a shared `stop_process()` and `exit_reason()` helper (S). After hand-over, a `ManagedProcess` base (spawn, read loop, exit-code-to-error, kill escalation, process group) plus per-decoder parse/spawn hooks. That would remove about 800 lines (L, with the full suite as the safety net).

**T-23 Smaller items**
- **M9 pack `tune` ignored by decoders.** Pack 24 says 137.5 MHz, the decoder uses 137.1. Dial strings are static (console.html:458 shows "162.0" for AIS).
- **M10 ppm DSP.** The offset search over 800 phasors takes 1.60 s on the Pi and holds the GIL; it would be about 4 s at the 20 s default. Spectrum p50 rose to 120 ms (max 612) during a measure. Goertzel uses 4% of real time and is fine.
- **`dbm` naming.** The JSON field `dbm` and JS `floorDbm` carry uncalibrated dB (rtl_power's 10·log10 of FFT power, not dBm). The UI shows "dB", so only identifiers mislead.
- **The sweeper never stands down when idle.** rtl_power keeps running at about 3% CPU after you leave the lesson.
- **Zombies.** Reaped children are not collected (defunct tcpdump and rtl_fm observed).
- **`seen` dedupe.** waterfall.js:270 dedupes by a 1-second timestamp, so two sweeps in one second drop the second.
- **`progress.updated` is always NULL** in production, because no clock is passed.
- **Rayhunter default host 192.168.1.1** is most home routers' address.
- **Async routes** (predict, exam) do sync SQLite on the event loop: measured 3–22 ms. Fine.
- **Great-circle** (adsb.py:118–131) is correct. JFK→LHR comes out 2,989 nm, bearing 51.4°. R = 3,437.7 nm against a mean 3,440.1 nm (0.07%).
- **The 9.5 s restart latencies** seen twice inside the polluted window did not reproduce on clean runs.

## RF constants vs primary sources

| Constant | Code | Verdict | Source |
|---|---|---|---|
| SSTV 145.800 MHz | sstv.py:32, pack 25 | **OUTDATED**: ISS SSTV is 437.550 MHz Robot36 (T-05) | ariss.org/contact-the-iss.html; ariss.org/upcoming-sstv-events.html |
| APT 137.100 = NOAA-19 (also 137.62 / 137.9125) | apt.py:29, 45–49 | Historically right, **dead**: NOAA-18 off 2025-06-06, 19 off 2025-08-13, 15 off 2025-08-19. 137.1 is now Meteor LRPT (T-11) | nesdis.noaa.gov decommissioning notice; WMO OSCAR; SatDump 1.2.2 pipelines |
| acarsdec `-g 49.6` | acars.py:116 | **CORRECT** for the 3.7 build on the box: "-g gain : set rtl gain in db (0 to 49.6 ...)" | `acarsdec` usage output on nomad-b |
| AIS 161.975 / 162.025 MHz | ais.py:22–23 | **CORRECT** | 47 CFR 80.393; 71 FR 60067 (Ch 87B/88B); rtl_ais defaults |
| APRS 144.390 MHz | aprs.py:23 | **CORRECT** (North America) | ARRL Letter 2022-02-10 |
| ACARS 131.550 MHz | acars.py:22 | **CORRECT**, primary worldwide SITA channel (secondary sources) | airframes.io; RadioReference |
| POCSAG 929.600 MHz | pocsag.py:22 | **WRONG**: between 929.5875 and 929.6125 (T-12) | 47 CFR 90.493(a) |
| RDS 96.0 MHz | rds.py:21 | **WRONG**: not a US FM channel, and a measured spur (T-04) | 47 CFR 73.201; live sweep |
| DMR 450.000 MHz | dmr.py:26 | Placeholder: band edge, no DMR channel | 47 CFR 97.301, 74.402 |
| Bias tee "none" | provenance.py:22 | **UNVERIFIED** (T-20) | rtl_eeprom; rtl-sdr.com V4 page |
| R828D 24–1766 MHz, gain 0–49.6 | spectrum.py:28–46 | Range correct for R820T/V4, optimistic for a generic R828D above about 1 GHz (UNVERIFIED). Gain table matches `rtl_test` (29 steps, 0.0–49.6) | rtl-sdr.com; r82xx.c; `rtl_test -t` on box |
| "dbm" | spectrum.py, waterfall.js | Misnomer: uncalibrated dB (T-23) | rtl_power.c `10*log10(avg/rate/samples)` |
| Great-circle | adsb.py:118–131 | **CORRECT** | Haversine plus forward azimuth, checked numerically |

## Not verified, and why

- **LoRa freeze (T-13):** restarting meshtasticd was off limits.
- **APT/SSTV/DMR live behaviour (T-11):** the tools are not installed on nomad-b.
- **The full `/api/tune` 100 Hz payload (T-18):** a leaked rtl_fm held the dongle during that drill.
- **The +164 ppm LO claim (T-14):** no stereo pilot at nomad-b, and the 2 kHz-resolution midpoint method was too coarse. The HIL step is in T-14.
- **The bias tee (T-20):** needs a meter.
- **The source of the 1.2 MHz comb:** whether it is the dongle or the Nomad board needs a shielded or second-SDR comparison.
- **Rayhunter (T-21):** no Orbic was reachable.

## Evidence files (scratchpad)

- `audit/m-rerun.txt`: clean measurement JSON. `m-decoders.txt` and `m-others.txt` are from the polluted window, kept for comparison only.
- `audit/d-aprs.txt`, `d-decode.txt`, `d-race.txt`, `d-bigtune.txt`, `d-offset.txt`, `d-fmscan.txt`, `d-ppm-solo.txt`, `d-rtltest-ppm.txt`
- `audit/tech-evidence/h1.png`, `h1_fixed.png`, `audit-events.jsonl`
- `audit/suite-{main,uxp,flp,int}.txt`
- `research/rf-constants.md` (primary-source quotes, including verbatim rtl_power.c)
- `scripts/` (load driver, drills, logic probes; none committed)
