# Adversarial verification of the 2026-11-02 technical audit

**Scope.** I tried to refute each S0 and S1 finding (T-01 to T-14) in `2026-11-02-technical.md`. Code was read at **c22f7d9** with `git archive` (line numbers below are at that commit). Upstream sources were fetched fresh. I made no edits to the repo or to nomad-b and ran nothing on nomad-b. Live claims are judged against the saved `d-*` and `m-*` files only.

**Verdict labels**
- **CONFIRMED**: the claim stands as written.
- **CONFIRMED, corrected**: the headline and severity stand, but a supporting detail is wrong. The correction is given.
- **PARTIAL**: the headline needs narrowing.
- **REFUTED**: the claim is wrong.
- **FIXED**: the issue is already fixed on origin/main.

## Summary

| ID | Verdict | Correction / note |
|---|---|---|
| T-01 | CONFIRMED | ADS-B catches a dump1090 that dies *at start* (5 s SBS-port guard). It does not catch one that dies mid-run. A SIGSTOPped child is alive, not dead, so exit-code checks can't catch it. |
| T-02 | FIXED | On origin/main in 5f553ff (#12). `app.css:763` now reads `image-rendering: pixelated`. |
| T-03 | CONFIRMED, corrected | 8 app-made copies per FM row, not 9. The total is **16 of 257 (6.2%)**, not 17 (6.6%). 7 real bins are dropped. |
| T-04 | CONFIRMED | 96.0 isn't a channel under 73.201. The saved scan supports "narrow carrier". The hop-invariance test isn't in the saved files. |
| T-05 | CONFIRMED, corrected | The last 145.800/PD120 event was **Oct 3–9 2025** (Series 29), not July 2025. Nov 2025 was attempted on 145.800 and failed. All 2026 events used 437.550/Robot36. |
| T-06 | CONFIRMED, corrected | `ppm.py:220` also escalates to `kill()`. It isn't only the Sweeper and decode.py. |
| T-07 | CONFIRMED | — |
| T-08 | CONFIRMED, corrected | Only M1 (01:28/33/37) asks the learner to tune or tap. 06:22 is the narrator's own retune and 07:40 is a hypothetical in a predict question. |
| T-09 | CONFIRMED | — |
| T-10 | CONFIRMED, corrected | The server accepts overwrites. The kiosk UI shows no re-answer control (disabled radios, no form on the review page), so the only ways in are history/resubmit or a direct POST. Arguably S2. |
| T-11 | CONFIRMED, corrected | SatDump 1.2.2 lists 137.1 MHz as Meteor LRPT's **backup** channel. 137.9 is primary. |
| T-12 | CONFIRMED | 929.6000 is off the 12.5 kHz-offset 25 kHz raster in both Part 90 and Part 22. |
| T-13 | CONFIRMED (code) | The library publishes `meshtastic.connection.lost` when the reader dies. The app never subscribes to it. |
| T-14 | CONFIRMED | dmr ppm 0 is at `dmr.py:187`, not :33 (which is a comment). The comb-based LO inference stays UNVERIFIED (see note). |

Counts: CONFIRMED 8, CONFIRMED-corrected 6 (T-03, T-05, T-06, T-08, T-10, T-11), PARTIAL 0, REFUTED 0, FIXED 1 (T-02).

---

## S0

### T-01 Dead decoder shows LIVE: CONFIRMED

- **Readers ignore the exit code.** Every reader ends on EOF and only sets `error` on `OSError`/`ValueError`. It never reads `returncode`. Example, `aprs.py:295-304`:
  `for raw in self._proc.stdout: ... except (OSError, ValueError) as exc: self.error = str(exc)`
  The same shape appears at `decode.py:227-236`, `flock.py:389-401`, `rds.py:214-223`, `pocsag.py:238-247`, `acars.py:203-212`, `adsb.py:357-366`, `ais.py:496-505`, `dmr.py:277-286` and `sstv.py:248-264`. `apt.py` has no reader thread.
- **Pipelines poll only the last process.**
  - `aprs.py:179-180` `def poll(self): return self._multimon.poll()`
  - `rds.py:98` (redsea), `pocsag.py:119` (multimon) and `sstv.py:112` (sstv) do the same.
  - `running` (`aprs.py:238-240`) is `proc is not None and proc.poll() is None`.
- **Routes respawn and answer 200.**
  - `app.py:969-983` (aprs): `if not decoder.running: _claim_sdr("aprs"); decoder.start()`, then `return {"available": True, ...}`.
  - `start()` raises only if `self.error` is set (`aprs.py:282-284`), and EOF never sets it.
  - `app.py:865-878` (flock) has the same shape.
- **Flock's presence probe doesn't check monitor mode.** `flock.py:326` `return os.path.exists(f"/sys/class/net/{interface}")`, so a DOWN, managed-mode wlan1 passes.
- **The saved evidence agrees.**
  - `m-rerun.txt` flock block: 21 requests, all 200, `last_body {"available":true,"interface":"wlan1","detections":[]}`, `events_lines_delta: 21`.
  - `tech-evidence/audit-events.jsonl` has 42 `flock_start` out of 187 lines. The report says 180, which is immaterial.
  - `d-aprs.txt`: after SIGKILL of multimon-ng, `aprs_start` goes 2 to 3 and every poll returns 200.
  - `d-decode.txt`: after SIGKILL of rtl_433, `decode_start` goes 3 to 4.
- **Narrowing, without refuting.**
  - (a) ADS-B's `_popen` waits up to 5 s for the SBS port and raises `AdsbUnavailable` if dump1090 exits first (`adsb.py:241-252`). An *immediately* dead dump1090 is therefore a 503. Only a mid-run death goes silent.
  - (b) The SIGSTOP drills show a *hung* child, which `poll()` correctly reports as alive. Porting `_record_abnormal_death` won't catch that case. It needs a staleness/liveness check, for example no output for N s.

### T-02 Waterfall smoothing: FIXED on origin/main

`git show origin/main:sigint101/static/app.css` has `image-rendering: pixelated;` at lines 763 and 976. It came in with commit 5f553ff "…waterfall scaled without smoothing (#12)". Not re-litigated.

### T-03 rtl_power join duplicates: CONFIRMED, corrected count

- **Primary source.** I fetched rtl_power.c three ways:
  - osmocom `v0.6.0` (`raw.githubusercontent.com/osmocom/rtl-sdr/v0.6.0/src/rtl_power.c`);
  - osmocom master;
  - **Debian's own 0.6.0-4** (`sources.debian.org/data/main/r/rtl-sdr/0.6.0-4/src/rtl_power.c`, the bookworm package on nomad-b).
  `csv_dbm()` is byte-identical in all three.
  - Line 722 nukes DC: `ts->avg[0] = ts->avg[1];`
  - Lines 724-728 do the 180° rotation.
  - Lines 738-744: `for (i=i1; i<=i2; i++) { ... fprintf(file, "%.2f, ", dbm); }`
  - Lines 745-750 print the last bin again: `dbm = (double)ts->avg[i2] / (...); ... fprintf(file, "%.2f\n", dbm);`
  - So each line carries (i2−i1+1)+1 values. The app passes no `-c` (`spectrum.py:380-387`), so crop is 0, i1=0, i2=len−1, and each line has **len+1** values. The "N+1 values" claim holds.
  - keenerd's fork does *not* have the trailing duplicate. It prints `i1..i2` with the newline on the last value. It isn't the binary on the box.
- **The geometry is real.** `frequency_range()` (lines 452-478) splits 88–108 MHz/125k into 8 hops of 2.5 MHz, with `bin_e=5` (32 bins, 78.125 kHz). Each hop's FFT bin k sits at `low + k*step`. The trailing duplicate is labelled `high`, which is the next hop's bin 0, but its value is bin 31's.
- **The app keeps the wrong one.**
  - `spectrum.py:266-267`: `dbm.extend(chunk.dbm[1:] if _adjacent(previous, chunk) else chunk.dbm)`. It keeps hop n's copied value and drops hop n+1's measured bin 0.
  - `SweepRow.frequencies()` (`spectrum.py:145-153`) then assigns value i to `low + i*step`.
- **The centre-bin copy is real.** `avg[0]=avg[1]` runs *before* the rotation, so the printed index len/2 (the hop centre, merged index 16 in each hop) is a copy of index len/2+1. librtlsdr disables zero-IF for R820T/R828D (`librtlsdr.c` 0.6.0, "disable Zero-IF mode"), so there's no analog DC spike to hide. The nuke replaces a genuine measurement with its neighbour.
- **Simulation.** `scratchpad/verify-merge.py` runs the real `parse_line`/`merge_chunks` on synthetic lines shaped like v0.6.0 output. Result: 33 values per line and 257 merged bins. Of those, **8** at `i%32==0` equal the previous bin (7 joins plus the final bin), **8** are DC copies, and **7** real bins are absent.
- **Corrected statement.** "Per FM row, 8 app-made copies (7 joins + the trailing duplicate) plus 8 rtl_power DC copies: **16 of 257 pixels (6.2%)** are not measurements, and 7 real bins are discarded." The report's own "320 of 320 at i%32==0" over 40 rows is 8 per row, which agrees with this.

### T-04 96.0 MHz "strong local station": CONFIRMED

- **47 CFR 73.201** (eCFR): "The FM broadcast band consists of that portion of the radio frequency spectrum between 88 MHz and 108 MHz." The table runs 95.9 = ch 240, 96.1 = ch 241, on odd tenths from 88.1 (ch 201) to 107.9. 96.0 isn't a channel.
- **Code and copy.**
  - `rds.py:21` `DEFAULT_RDS_HZ = 96_000_000`.
  - `12-rds.yaml:18` `center_hz: 96000000`.
  - `12-rds.yaml:31` "redsea is listening to 96.0 MHz, the strong local station".
  - `.claude/learnings.md:18` "Strongest station 96.0 MHz at ~29 dB over the sweep floor" blames the antenna.
  - `rds.py:119` runs rtl_fm at `-s 171k`, about ±85 kHz, so a real station on 95.9 or 96.1 lies 100 kHz off and outside the capture.
- **Saved evidence for "spur".** `d-fmscan.txt`:
  `peak 96.0005 MHz 48.2 dB | occupied 36.6 kHz narrow (carrier/spur)`
  The file also shows 94.8005 and 97.2004 as narrow (1.2 MHz spacing), with wide stations at 91.59, 93.55 and 91.07.
- **Caveats.** These narrow the evidence, not the finding.
  - The hop-centre invariance test (hops at 95.4/96.0/96.6) isn't in any saved file, so I can't re-check it.
  - `d-offset.txt`'s coarse pass puts the centroid near 96.12 MHz. A weak real station at 96.1 next to the spur isn't excluded. It wouldn't rescue M12, because 96.0 is 100 kHz off it.

### T-05 ISS SSTV frequency: CONFIRMED, corrected history

See **T-05 ruling** below. Code and copy are as reported:
- `sstv.py:30-32` reads "145.800 MHz is the 2m SSTV calling frequency" … `DEFAULT_SSTV_HZ = 145_800_000`.
- `25-sstv.yaml:19` `center_hz: 145800000`.
- `25-sstv.yaml:60-61` "The ISS sends its images on 145.800 FM during ARISS events, often in a mode called PD120".

There's an extra wrong claim in the same comment. The ARRL band plan lists "145.80-146.00 … OSCAR subband", so 145.800 isn't a terrestrial "2m SSTV calling frequency" either (arrl.org/band-plan; the 2 m section has no SSTV line).

## S1

### T-06 No kill path, no process groups: CONFIRMED, corrected

- **The wait wrappers swallow `TimeoutExpired`.** It's a subclass of `SubprocessError`, and the wrappers return 0:
  - `aprs.py:189-195`, `rds.py:107-113`, `pocsag.py:128-134` and `sstv.py:121-127` do `except (OSError, subprocess.SubprocessError): pass … return 0`;
  - `adsb.py:227-231`, `ais.py:380-384` and `dmr.py:178-182` do `return 0`.
  - So `except subprocess.TimeoutExpired` at `aprs.py:317`, `rds.py:236`, `pocsag.py:260`, `sstv.py:291`, `adsb.py:379`, `ais.py:518` and `dmr.py:299` is dead code.
- **acars, flock and apt are different.** They call `Popen.wait` directly, so their `TimeoutExpired` branch is live and sets `error`. But they never `kill()`.
- **Escalation and process groups.**
  - `.kill()` appears only at `spectrum.py:653`, `decode.py:250` and **`ppm.py:220`** (`_CaptureStream.close`). The report missed ppm.
  - `killpg`, `start_new_session`, `PDEATHSIG`, `preexec_fn` and `setsid` appear nowhere.
- **Timing.** The 9.0 s probe matches the code: 3 s + 3 s per-process waits, then a 3 s thread join.
- **The live hang (PID 21681)** appears in `d-race.txt`'s process list. The futex/SigCgt/lsof detail isn't saved, so that part is taken on trust.
- **The mitigation note holds.** `deploy/kiosk/sigint101.service` on origin/main has no `KillMode`, so it defaults to control-group.

### T-07 ppm measure races the waterfall; 500 on missing rtl_fm: CONFIRMED

- **The route doesn't claim the radio.** `app.py:741-754` calls `app.state.sweeper.stop()` and never `_claim_sdr`. The spectrum route restarts the sweep whenever `not sweeper.running` (`app.py:780-786`), and `waterfall.js:78` polls at `POLL_MS = 900`. M4 renders the ppm panel inside the instrument beside the waterfall (`console.html:641-652`, pack `panels: [ppm]`).
- **The race is in the evidence.** `d-race.txt`: `children seen during measure: [('rtl_fm', 21534), ('rtl_power', 21522), ('rtl_power', 21589)]`, `codes={'200': 2, '503': 2}`, `spectrum_start: 3`.
- **Missing rtl_fm gives a 500.** The Popen at `ppm.py:194` runs via `capture(...)` at `ppm.py:235`, *outside* the `try`, and the route doesn't catch it. FastAPI returns a text 500, `r.json()` throws, and `ppm.js:38-41` shows "measure failed" / "lost contact with the console".
- **Wrong device selector.** The route passes `device=os.environ.get(RTLSDR_SERIAL_ENV)` to `rtl_fm -d`. In osmocom `convenience.c` (v0.6.0) lines 264-270, `device = (int)strtol(s, &s2, 0); if (s2[0] == '\0' && device >= 0 && device < device_count) … return device;` checks the index before the serial. With base 0, "00000001" becomes index 1.

### T-08 Tune and tap are display-only: CONFIRMED, corrected copy list

- **The controls only change the readout.** `console.html:741` "tune readout: +/- buttons and tap-to-tune (display only)", and `:758` `mhz = (mhz - 1.3) + frac * 2.6;`. The tap is relative to the current readout, not the canvas span, so repeated taps drift.
- **ppm measures the readout.** `ppm.js:24` `var mhz = freqEl ? parseFloat(freqEl.textContent) : NaN;`
- **Correction to the copy list.**
  - 01-see-the-invisible.yaml:28/33/37 are learner instructions ("tap a different strong one", "tune two notches up the dial").
  - 06-resonance.yaml:22 is the *narrator's* demo ("Now I … tune up to 1090 MHz"). It's a mismatch, because the M6 instrument never retunes, but it isn't a learner control step.
  - 07-build-and-aim.yaml:40 is a hypothetical in a predict question ("retune down to 915 MHz … and suppose"). No control is needed.

### T-09 Gain can't be changed; FLOOR is the min: CONFIRMED

- 02-gain-and-snr.yaml:26 "Step the gain from the bottom up".
- `Sweeper.set_gain` (`spectrum.py:597`) has no caller. The only `Sweeper(` construction (`app.py:196-200`) passes no gain, and `/api/tune` (`app.py:1166-1191`) accepts only low/high/bin_size.
- `waterfall.js:183-186` uses `var lo = Math.min.apply(null, row.dbm); … p.floorDbm = … lo` as FLOOR, and the meter draws from `p.floorDbm`. No JS reads `floor_db`.
- `m-spectrum.json`'s first bins are `-15.3,-14.32,-12.66,…` against about −9.5.

### T-10 Answers overwritable after reveal: CONFIRMED, corrected exposure

- **Predictions.** `progress.py:107-110` runs `ON CONFLICT(lesson_id, kind) DO UPDATE SET body=excluded.body`. The code comment says so too: "this overwrites an earlier prediction rather than failing".
- **Practice exam.** `exam.py:329-345` `record_sim_answer` sets `answers[question_id] = choice` unconditionally. `app.py:621-633` doesn't check for an existing answer.
- **Narrowing.**
  - Once committed, the lesson renders the predict radios `disabled` (`console.html:233`).
  - The sim review page renders no form (`exam_sim.html:21` `{% if not reviewed %}`).
  - So the kiosk UI offers no re-answer control. The hole is reachable by browser history/resubmit or a direct POST. The fix is still right, but S2 is defensible.

### T-11 APT chases dead NOAA-19 and restarts: CONFIRMED, corrected

- **NOAA-19 is hard-coded.**
  - `apt.py:25` `APT_PIPELINE = "noaa_apt"`.
  - `:29` `DEFAULT_APT_HZ = 137_100_000`.
  - `:45-49` `137_100_000: "NOAA-19"`.
  - `:197` labels every PNG `satellite_for(self.freq_hz)`.
- **satdump restarts.** `:67` passes `--timeout 900`. When satdump exits, `running` goes False (`:99-101`), and `/api/apt` (`app.py:1101-1104`) re-claims and restarts on the next `apt.js:30` `POLL_MS = 4000` tick. `_stop_locked` never kills (`apt.py:162-171`).
- **Decommission dates** come from NESDIS/OSPO: NOAA-18 2025-06-06, NOAA-19 2025-08-13 (emergency, battery), NOAA-15 2025-08-19 (nesdis.noaa.gov "suspension of POES data users…"; ospo.noaa.gov POES status).
- **Correction.** SatDump 1.2.2 `pipelines/Meteor-M.json` `meteor_m2-x_lrpt` lists frequencies **137.9e6 first, 137.1e6 second** (Primary/Backup per the research notes). So "137.1 is now a Meteor LRPT channel" is true, but it's the *backup*. N2-4 mostly uses 137.9.

### T-12 POCSAG 929.6000 between channels: CONFIRMED

- **Channel plans.**
  - 47 CFR 90.493(a) (eCFR) has "… 929.5625, 929.5875, 929.6125, 929.6375 …". 929.6000 is absent.
  - 47 CFR 22.531 lists 931.0125–931.9875 on the same x.x125/x.x375/x.x625/x.x875 raster.
- **Code and pack.** `pocsag.py:22` `DEFAULT_POCSAG_HZ = 929_600_000`, `15-pocsag.yaml:18` `center_hz: 929600000`, and `pocsag.py:140-141` `rtl_fm … "-s", str(POCSAG_SAMPLE_RATE)` (22050).
- **Nuance.** rtl_fm decimates with a boxcar, so a pager 12.5 kHz off *aliases* into the ±11 kHz band at reduced level instead of vanishing. The decode becomes unreliable rather than strictly impossible.

### T-13 LoRa `running` never re-checks: CONFIRMED (code)

- **The app's check.** `lora.py:133-136` `return self._interface is not None`. The only subscription is `"meshtastic.receive"` (`:153-155`).
- **The library's behaviour.** I fetched meshtastic **2.7.11** from GitHub.
  - `tcp_interface.py:142-156`: on `recv` returning `b""` it calls `_reconnect()`.
  - `:158-183`: `_reconnect` does `time.sleep(1)` and then `self.myConnect()` once, which is `socket.create_connection`. That raises if the daemon is down.
  - `stream_interface.py:236-249` catches the `OSError` as "Unexpected OSError, terminating meshtastic reader...". Its `finally` runs `self._disconnected()`.
  - `mesh_interface.py:1157-1162` then clears `isConnected` and publishes `"meshtastic.connection.lost"`, which the app never hears.
- **Live state** remains UNVERIFIED, as the report says.

### T-14 ppm correction unbacked: CONFIRMED

- **No decoder applies a correction.**
  - `rds.py:119` `"-p", "0"`.
  - `dmr.py:187` `rtl:{device}:{freq_hz}:0:0:{bw}`, where the ppm field is 0. The report's `:33` is the explanatory comment.
  - No other decoder passes ppm. `ais.py:398`'s `-P` is the UDP port.
- **The promised tooling doesn't exist.** The Makefile targets are bootstrap/test/lint/coverage/selftest/run. There's no `ppm-reference`, yet 04-ppm-error.yaml:24 cites it and :56 promises "a one-tap correction".
- **The tuner crystal.** librtlsdr 0.6.0 (`librtlsdr.c:1592-1597`) sets `dev->tun_xtal = dev->rtl_xtal; … case RTLSDR_TUNER_R828D: dev->tun_xtal = R828D_XTAL_FREQ;` with `#define R828D_XTAL_FREQ 16000000` in `tuner_r82xx.h:30`. That supports the report's hedge that the R828D may run off its own 16 MHz crystal, contrary to `ppm.py:149` "One crystal clocks both".
- **Note against the comb argument.** If the 1.2 MHz comb is generated from the dongle's own 28.8 MHz crystal (28.8/24, as the report suggests), its error partly tracks the receiver's. Its near-nominal reading then doesn't isolate the LO error. The NOAA WX `-p 0` vs `-p 150` HIL in the report remains the right decider, so the claim stays UNVERIFIED, as written.

---

## T-05 ruling: which frequency and mode does ISS SSTV use?

**Both have been used, at different times.** 145.800 MHz FM PD120 was the ISS SSTV downlink through **October 2025**. Every ISS SSTV event in **2026** used **437.550 MHz FM, Robot36**, which is what ARISS lists today. The curriculum audit's "145.800 FM PD120 correct" was right historically but is now outdated. The technical audit's direction is right, and its "last 145.800 event July 2025" is wrong.

Dated timeline:

| When | Freq / mode | Source |
|---|---|---|
| Apr 11–16 2025 (Series 26) | 145.800 FM, PD120 | ariss.org/upcoming-sstv-events.html: "Frequency: 145.800 MHz FM … the mode is PD120" |
| Jul 14–20 2025 | 145.800 FM, PD 120 | ARISS News Release 25-39 (2025-07-12): "145.800 MHz FM … PD 120" |
| **Oct 3–9 2025 (Series 29)** | **145.800 FM, PD120** | amsat-uk.org/2025/10/03/iss-slow-scan-tv-oct-3-9: "145.800 MHz FM, (+/- 3.5 kHz Doppler shift) … using SSTV mode PD120"; SBARC 2025-08-31 |
| Nov 12–19 2025 (Series 30) | 145.800 attempted; failed | community.libre.space thread 13989: "no signal at 145.800 mhz" (2025-11-13); RV3DR relayed that the Zvezda Kenwood D710 TX keys for only 3–4 s. ARISS page: "SSTV has been offline since starting on Wednesday." |
| Dec 5–13 2025 (Series 30 retread) | 437.825, Robot 36, **from RS-38S, not the ISS** | ARISS page: "Upcoming transmission of Series 30 images will be from RS-38S on 437.825 MHz in Robot 36." |
| Apr 10–14 2026 (Series 31) | 437.55, Robot 36 | ARISS page: "Frequency: 437.55 MHz … SSTV Mode: Robot 36" |
| May 8–12 2026 (Series 32) | 437.550, Robot 36 | ARISS News Release 26-28 (2026-05-08): "437.55 MHz (+/- Doppler Shift) using SSTV set at Robot36 mode" |
| Oct 2–6 2026 (Series 33) | 437.550, Robot 36 | ARISS page |
| Oct 22–26 2026 (Series 34) | 437.55, Robot 36 | ARISS page; amsat-uk.org 2026-10-20: "437.550 MHz FM, (+/- 10 kHz Doppler shift) … Robot 36" |

- **Current reference.** ariss.org/contact-the-iss.html reads "SSTV Downlink 437.550 MHz with Robot36 mode" and "Voice Downlink: 145.80 (Worldwide)".
- **The old radio isn't fixed yet.** ARISS's station status page (as of 2026-10-27) still says the Zvezda RS0ISS radio is "still in the process of troubleshooting and testing". 145.800/PD120 could return if that Kenwood is fixed.

**Recommended copy for M25.** Use 437.550 MHz FM, Robot36, and allow ±10 kHz of Doppler. Say that 2025 and earlier events used 145.800 FM PD120, and point the learner to ariss.org/upcoming-sstv-events.html for each event's frequency and mode.

## Sources fetched this pass

- github.com/osmocom/rtl-sdr at v0.6.0 and master: `src/rtl_power.c`, `src/librtlsdr.c`, `src/convenience/convenience.c`, `include/tuner_r82xx.h`
- sources.debian.org rtl-sdr 0.6.0-4 `src/rtl_power.c`
- github.com/keenerd/rtl-sdr `src/rtl_power.c`
- github.com/meshtastic/python at 2.7.11: `tcp_interface.py`, `stream_interface.py`, `mesh_interface.py`
- github.com/SatDump/SatDump at 1.2.2: `pipelines/Meteor-M.json`
- eCFR 47 CFR 73.201, 90.493, 22.531
- arrl.org/band-plan
- ariss.org pages: upcoming-sstv-events, contact-the-iss, current-status-of-iss-stations, press releases 25-39 and 05-2026
- amsat-uk.org posts of 2025-10-03 and 2026-10-20
- sbarc.co.uk (2025-08-31)
- community.libre.space thread 13989
- NESDIS/OSPO POES decommission notices (via search)

Simulation script: `scratchpad/verify-merge.py`. Extracted c22f7d9 source: `scratchpad/verify-src/`. Downloads: `scratchpad/verify-dl/`.
