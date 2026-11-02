# Adversarial verification of 2026-11-02-curriculum.md

Read-only. Code read from `origin/main` (d688082; packs, templates, static and app are unchanged since
c22f7d9) and `origin/ux-polish` (345fbfb), extracted with `git archive`. One live render: a
TestClient GET of `/lesson/1` and `/lesson/7` on the extracted origin/main tree, using the repo
venv, with no radio.

**Tally: 37 CONFIRMED, 3 PARTIAL, 1 REFUTED.**

## §5a Wrong facts (X1-X13): 13 CONFIRMED

| # | Verdict | Evidence / correction |
|---|---|---|
| X1 | CONFIRMED (caveats) | 2510(12)(B) excludes only "any communication made through a tone-only paging device". 2510(16)(D): a radio comm is not readily accessible if "transmitted over a communication system provided by a common carrier, unless the communication is a tone only paging system communication". 2511(1)(a) covers "intentionally intercepts, endeavors to intercept", so an empty run still counts as an attempt. 2511(4)(a): up to 5 years. The old reduced penalty for paging under (4)(b) was struck in 2002 (Pub. L. 107-296). BNN 1997: SDNY's US Attorney called it the first ECPA pager-interception prosecution (rcfp.org/news-service-operators-arrested-intercepting-pager-signals). Pack 15:56 "Receiving radio is passive and legal almost everywhere". `pocsag.py:22` `DEFAULT_POCSAG_HZ = 929_600_000`. **Caveats:** (a) the offence attaches only if the 929.6 system *is* a common carrier. A private (PMRS) on-site paging system on a shared 929 MHz channel is not caught by 16(D), so 2511(2)(g)(i) could apply to it, though §605 divulge/use still does. The learner can't tell which kind of system they are hearing from the decode. (b) 47 USC 332(c)(1)(A) treats CMRS providers as common carriers "for purposes of this chapter", meaning the Communications Act, not Title 18. It is persuasive for 2510(16)(D), not dispositive. LII 18/2510, 18/2511, 47/332 |
| X2 | CONFIRMED (one nuance) | 05:24-26 "you may not pass on or act on certain private communications … such as cellular". A cell call is a "wire communication" (2510(1): an aural transfer "in whole or in part" through wire facilities), and wire comms have no readily-accessible exception. Interception is itself the offence under 2511(1)(a). **Nuance:** 47 USC 302a(d) and 47 CFR 15.121 govern the *equipment authorization* of scanners: design, marketing, and modification counted as manufacture. They do not bar a user from owning or using a receiver. So "scanners able to tune cellular are barred" should not be read as making the RTL-SDR unlawful to own. LII cfr 47/15.121 |
| X3 | CONFIRMED (details corrected) | The conclusion stands: 179 kHz is a small fraction of a ~2 MHz PPM signal. Two corrections: (1) 14 CFR 43 App. F ¶(a) is 1090 ±3 MHz for all ATCRBS and for Mode S classes 1B/2B/3B, and ±1 MHz for classes 1A/2A/3A/4. Real transmitters are allowed to be 1-3 MHz off, so receivers must tolerate far more than 179 kHz. (2) The console runs **dump1090-fa** (`adsb.py:1,237`), whose default is `Modes.sample_rate = 2400000.0` (flightaware/dump1090 `dump1090.c`), not 2 MS/s. That gives even more margin. The AIS figure (27 kHz at 162 MHz) and the DMR figure (74 kHz at 450 MHz) check out: 164e-6 × f |
| X4 | CONFIRMED | 08:22-24 shows "a single hard, bright line … no message yet" *before* "a pilot keys up". 08:46-48 observes "in a gap between transmissions", and 08:53-54 says the carrier "is transmitted the entire time". ATC voice is push-to-talk. AIM 4-1-13 calls ATIS "the continuous broadcast of recorded noncontrol information", and that is the exception. Within one transmission the full-carrier AM line does persist between words |
| X5 | CONFIRMED | 06:58 "a quarter-wave in centimetres is 300 over the frequency in MHz, then quartered". 300/1090/4 = 0.069, which is metres. 06:10 (concept) and 06:26 (demo) correctly say metres |
| X6 | CONFIRMED | 47 CFR 18.301 lists only 6.78, 13.56, 27.12, 40.68, 915 ±13, 2450, 5800 MHz and higher. 433.05-434.79 is absent. Copy at 09:12/25/27 and 10:12/29 |
| X7 | CONFIRMED (hedged as written) | rtl_433 `acurite.c` has 8 r_devices, all OOK (PPM/PWM). `lacrosse_tx141x.c` uses `.modulation = OOK_PULSE_PWM`. "May be impossible" is a fair hedge |
| X8 | CONFIRMED | flock-you README: "Flock cameras are also known to use cellular LTE modems to upload". It fingerprints wildcard WiFi probes and BLE separately. Flock's own FAQ says solar + LTE. No source shows a WiFi uplink, though none rules out a WiFi fallback either |
| X9 | CONFIRMED | 19:21-23 "never touches the open air you can sweep". rayhunter `doc/heuristics.md`: "SIB broadcast messages are not encrypted or authenticated". The signalling is radio; the limits are bandwidth and the LTE stack (an RTL dongle tops out near 1.77 GHz, enough for the 600/700/850 MHz bands) |
| X10 | CONFIRMED (one nuance) | 04:21-23 "at exactly 19 kHz above its center". 47 CFR 73.322 defines the pilot as a 19 kHz ±2 Hz baseband subcarrier. `ppm.py:19-21` demodulates with `rtl_fm -M fm -s 200000` and times the audio tone, so the code is right. **Nuance:** FM-modulating the pilot does put weak RF sidelines at *±*19 kHz, symmetric. "Not an RF tone above the carrier" is a slight overstatement; "not a single landmark above the centre, and not what the code measures" is exact |
| X11 | CONFIRMED | 12:9-10 / 12:26-27 / 12:59 ("not one bit of it moves a single pixel"). A PSK signal occupies roughly its symbol rate and shows as energy. RDS is invisible on *this* console only because it is a 57 kHz subcarrier inside the FM baseband, under a 125 kHz-bin RF waterfall. An MPX-baseband waterfall shows it plainly |
| X12 | CONFIRMED (scope note) | 11:32. As general physics the copy is false: 10·log(250k/1k) = 24 dB, so a carrier at -10 dB in 250 kHz stands at about +14 dB in a 1 kHz bin. On *this* console's 125 kHz bins, a -10 dB carrier would indeed be invisible, so the minimal fix is to scope the sentence ("on this waterfall's wide bins") |
| X13 | CONFIRMED | PS is an 8-character service name. US call letters derive from the PI code (NRSC-4-B, RBDS PI-from-callsign annex) |

## §4 Fidelity S0 (14): 13 CONFIRMED, 1 PARTIAL

ux-polish adds only `scroll-keys.js`, `data-lesson-id` and the Complete button
(`git diff origin/main origin/ux-polish`). Searching for `api/gain`, `sweeper.set_gain`,
`data-preset`, `data-radio` and `ppm_corrected=` across main, ux-polish, flipper-module,
kiosk-watchdog, status-rewrite and worktree-unit6-theory returns nothing.

| # | Verdict | Evidence |
|---|---|---|
| F2 01:28-31 | CONFIRMED | `console.html:741-760` "tune readout … (display only)": +/- and the click change only `freqEl.innerHTML`. The tap maps to `(mhz - 1.3) + frac * 2.6` while the canvas axis is the live 88-108 sweep (`waterfall.js:159-168`, `spectrum.py:22-24`). No list exists |
| F4 02:22-24, F5 02:26-28, F6 02:34 | CONFIRMED ×3 | No gain route among the `app.py` routes (414-1166). `Sweeper.set_gain` (`spectrum.py:597`) has no caller. `build_sweeper` passes no gain (`app.py:196-200`), so `rtl_power` runs without `-g`, which is automatic gain (`spectrum.py:384-386`). The `gain` panel is a read-only meter (`console.html:605-624`) |
| F7 03:24-26 | CONFIRMED | `provenance.py:16` `REFERENCE_HZ = 106_500_000`, with a comment about "this rural spot". No env override (grep `REFERENCE_HZ`) |
| F10 04:25-27/31-32 | CONFIRMED | Every consumer takes `serial=os.environ.get(RTLSDR_SERIAL_ENV)` (`app.py:198` and others). No picker in any template or JS |
| F12 04:56-58 | CONFIRMED | `base.html:25-27` shows `PPM CORRECTED` only if `ppm_corrected`, which no route ever sets. Rendered `/lesson/1` and `/lesson/7`: chip text "PPM RAW" |
| F13 06:22-23, F14 06:31-36 | CONFIRMED ×2 | Pack 06 has no `tune:`. `waterfall.js:298-311` retunes only from `data-tune-low/high`, so the view stays on 88-108 |
| F15 07:21-37 | **PARTIAL** | Right: no `tune:`, `panels: []`, so no meter and no 1090 view. Wrong: "status-bar SNR is always '—'". `snr_db` is never passed, and in Jinja `Undefined is none` is False, so the readout renders a bare **"dB"**. Observed in the render: `SNR readout: 'dB'` on lessons 1 and 7. That is a separate small UI bug |
| F16 08:22-31 | CONFIRMED | Pack 08 has no `tune:`. `parseTuneBand` sends only low and high (`waterfall.js:24-29,303-306`), and `validate_span` defaults `bin_size` to 125 kHz (`spectrum.py:82-83`) |
| F17 08:34-35 | CONFIRMED | No preset element or handler anywhere |
| F18 08:46-48 | CONFIRMED | Same as F16, and its premise contradicts X4 |
| F21 11:35-36 | CONFIRMED | 28:42 "MC is bringing one". No send route (`/api/lora` is GET only). Note: third-party LongFast nodes in range could still fill the list, so the *try* is not dead everywhere; only "key the second node" is |

## §6a Predict leaks and scoring: 11 CONFIRMED, 2 PARTIAL, 1 REFUTED

| Claim | Verdict | Evidence / correction |
|---|---|---|
| The concept prints on the frame eyebrow | CONFIRMED | `console.html:51`: the concept shows unless the stage is an instrument stage on a hardware lesson, or predict. Rendered: lesson 1 shows "Step 1 of 6 · FRAME · A radio signal is a shape…" |
| "For 13 lessons that banner states the answer" | **PARTIAL** (undercount) | I count ≥18: M3, M5, M9, M10, M11, M12, M13, M14, M15, M16, M17, M18, M19, M24, M25, M26, M27, M28. Examples: 13 "Nothing is cracked"; 16 "Most of it is sent in the clear"; 25 "SSTV turns one into a picture"; 28 "hops node to node … to reach a node far beyond your own range". M20 and M22 also hint |
| No `answer` key; nothing scores | CONFIRMED | No `answer` in `lessons.py`, `progress.py` or packs; the only `question.answer` is the exam (`app.py:550`). `progress.py:91-118` stores `body` only |
| Retry overwrite | CONFIRMED | `console.html:233` disables the inputs. `progress.py` uses `ON CONFLICT(lesson_id, kind) DO UPDATE SET body=…` |
| Leaky labels | CONFIRMED | M5 "Illegal — transmitting needs a licence"; M17 "No — silence proves nothing"; M19 "No — red means look closer"; M21 "Yes — its mirror below the LO"; M22 "2.4 MHz — the full rate" |
| Spot rows M3, M10, M12, M14, M17, M21, M24, M26 | CONFIRMED ×8 | Line refs check out: 03:17-18; 10:24; 12:26-27; 14:9; 24:13; 26:13 |
| M8 "not leaked" | **REFUTED** | Demo 08:22-26: "a single hard, bright line … That's the carrier … no message yet … then falls back when they stop. The line never moves, only the brightness on it". The demo tells the learner the AM carrier persists without speech, which gives away "neither" before predict |
| "23 of 28 … only M1, M6, M8, M23 genuine" | **PARTIAL** | Corrected: **24 of 28** leaked. Genuine predictions are M1, M6 and M23; M7's key is ambiguous |

Not checked: §5b, §5c, §8 rewrites (for example, whether dump1090-fa accepts `--ppm`), F1/F3/F8-F9/F11/F19-F31 (S1 and S2).
