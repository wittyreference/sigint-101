# SIGINT-101 curriculum and learning-design audit (2026-11-02)

Scope: `origin/main` @ c22f7d9 (28 packs, all `type: intro`), plus `origin/flipper-module` (M29) and
`origin/ux-polish` (scroll and Complete button). This was a read-only audit; no repo edits were made.
Line references are `pack:line`, for example `04:56` means `lesson_packs/04-ppm-error.yaml` line 56.
Console facts are cited by `file:line`. Teaching order is the order in `units.py`:
1-16, 26, 27, 20-23, 17-19, 24, 25, 28, then 29 on the flipper branch. "Position" (pos) means a
lesson's place in that order.

## 0. Headline

- **Competency matrix.** The audit tracks 12 field outcomes. Six are **never practised** (find and
  identify an unknown signal, antennas with feedback, record and log, mesh operations, troubleshoot
  from the internals, real tools). Four are practised only passively or partly, in the garden (a
  pre-tuned panel fills while the learner watches). Legality is recall only. **Only one outcome,
  the licence exam, is assessed.** The rack and real-tool layers do not exist, so **0 of 12
  outcomes are practised in the rack and 1 of 12 with a real tool** (the Flipper's own Read RAW in
  M9).
- **Fidelity.** The first hour is where the course is most broken. U1 to U4 (M1, M2, M4, M6, M7,
  M8) tell the learner to tune, step the gain, switch radios, apply a correction, and pick airband
  presets. The console has none of these controls: tuning is display-only, there is no gain
  control, no presets, no radio picker, no ppm correction, and no audio. There are **31 impossible
  or false instructions**; 14 of them are S0.
- **Assessment.** Predictions are never scored. In **23 of 28 lessons** the answer has already been
  given on an earlier screen: the concept banner on the frame stage, the frame, the demo, or a
  choice label. Observe captures nothing. Nothing is retrieved, spaced, or faded.
- **Accuracy.** 13 statements are wrong, and 15 are imprecise. §5 has the sourced list. The
  worst:
  - **M15 says receiving pager traffic is legal.** In the US, decoding commercial-carrier pages is
    itself interception under 18 USC 2511 / 2510(16)(D), and M15's live panel runs on 929.6 MHz.
    So working the lesson as written is the offence. **This needs MC's call.**
  - M5 implies that intercepting cellular is fine if you don't pass it on.
  - M4 says +179 kHz makes ADS-B planes "simply not be there". They don't vanish: Mode S tolerance
    is ±1 MHz.
  - M8 says an idle airband channel shows a carrier. Airband is push-to-talk.
  - M6 gives antenna length in "centimetres" where the formula gives metres.
  - 433.92 is not a US ISM band, and US remotes mostly use 315/390 MHz.
  - Flock uploads over LTE, not WiFi.
  - M12 says a waterfall can never show PSK.
  - M11 says a weak CW carrier is "gone".
- **Load.** Explain stages run 46 to 294 words (median 222). 24 of 28 are over the ~50-word budget
  for one 800x480 screen. That makes them 4 to 6 scroll-screens on ux-polish.
- **Recommendation before 11-08.**
  - Fix the 14 S0 fidelity items (§4). That means wiring real tune and gain, or using the
    copy-only rewrites in §8.
  - Fix the 5 S0 fact items (§5a X1-X5), including MC's call on M15.
  - Neutralise the leaky choice labels, add an `answer:` key and a one-line verdict.
  - Show lesson codes in teaching order.
  - Ship the command strip.
  - Move everything else to the update track.

---

## 1. Target competency map: what "can work this kit in the field" means

A junior SIGINT or RF operator, whether a military collector/EW technician, a TSCM or RF-survey
technician, or a skilled amateur, spends their day on six things:
- checking out the receive chain;
- searching a band;
- characterising and identifying what they find;
- collecting and decoding it with standard tools;
- logging it so someone else can trust it;
- staying inside their legal authority.

They also have to aim an antenna and debug a chain that is lying to them. The list below maps
those duties onto this kit: Nomad 2 Recon (RTL2832U+R828D, SX1262 LoRa HAT, GPS, Pi WiFi), the
V3+dipole add-on kit, the Flipper, and the Orbic.

| # | Outcome | What the operator actually does | Why it matters on this kit |
|---|---|---|---|
| C1 | Sanity-check the instrument | Baseline against a reference, measure and apply ppm, set gain below overload, tell a deaf radio from an empty band | The built-in radio is +164 ppm with no TCXO. A field operator who can't trust the chain can't trust any reading |
| C2 | Find and identify an unknown | Search a band, measure centre/bandwidth/shape/duty/rate, name the modulation family and a plausible service | This is the core SIGINT skill. Every other outcome assumes the signal is already found |
| C3 | Pick, build, aim an antenna (and plan the path) | Resonant length, polarisation, antenna type, coax/connectors, filters/LNA/bias-tee, line of sight, pass timing | The stock whip is deaf at 1090 and 2 m (STATUS). Antennas are the cheapest gain there is |
| C4 | Decode the common services | Choose the tool and flags for ADS-B, AIS, APRS, ACARS, ISM, RDS, pager, DMR/P25, weather satellites, SSTV | Decoding is where the meaning is, and the tools are the same ones used outside the console |
| C5 | Judge legality | Receive vs intercept vs divulge vs decrypt vs transmit, per band and per service (US) | The recipient is an ethical hacker. The line is knowledge they will act on unsupervised (D16) |
| C6 | Record and log with provenance | IQ/SigMF capture, decode logs with time, place, radio, gain and ppm; write a short report | Without a record there is no evidence, no replay, and no honest badge (CLAUDE.md invariant) |
| C7 | Counter-surveillance epistemics | Tiered confidence, corroborate, "no alert is not absence", save the capture | M17-M19 and the Orbic. Overclaiming here does real harm |
| C8 | Mesh operations | Join or create a channel, manage keys and hop limit, send, range-test, read SNR | The Nomad *is* a Meshtastic node, and TX is enabled (D16) |
| C9 | Flipper capture and analysis | Find the frequency, Read RAW, save, analyse the pulse train, RFID/NFC/IR/iButton | A paired field tool, and U10 is in progress |
| C10 | Troubleshoot from the internals | Recognise overload, images, aliases, DC spike, ppm drift, a dead decoder | This is what separates "it's broken" from "I know which stage is lying" |
| C11 | Licence path | Pass Technician, then know Part 97 well enough to transmit | It is the on-ramp to the TX half of the gift (D16) |
| C12 | Operate the real tools outside the console | rtl_power/rtl_fm/rtl_433/dump1090/multimon-ng/satdump/SDR++/meshtastic CLI over SSH | Leaving the garden. The learner already lives in terminals (abstract-soaring-bunny §Course architecture) |

## 2. Competency matrix: teach / practise / assess, and the layer each is practised at

T = taught (copy explains it). P = the learner performs it with real feedback from the console as
built. A = there is a scored or checked outcome. Layer: G = garden (the lesson), R = rack
(`/operate`, not built), F = real tool.

| # | Taught in | Practised (layer) | Assessed | Verdict |
|---|---|---|---|---|
| C1 | M2, M3, M4, M7, M20 | M3 baseline capture (G; but the reference is fixed at 106.5 MHz, MC's site, `provenance.py:12-15`). M4 Measure ppm (G; only after hand-stepping the display-only dial to a strong stereo station). **Gain: never** (no control). **V3 control: never** (no picker) | none | PARTIAL, G only |
| C2 | M1, M8-M12 (family shapes), M10 ("eye vs decoder") | **none.** Every lesson pre-tunes and pre-picks the decoder; tuning is display-only (`console.html:741-760`). The plan's Signal-ID lesson (amendment M18) was dropped | none | **NEVER PRACTISED** |
| C3 | M6, M7; M24 names V-dipole/QFH | Legs can be moved, but there is **no feedback at 1090/915** (the instrument is stuck on 88-108, `spectrum.py:22-24`; M6/M7 have no `tune:`) | M6 predict (a good transfer item, unscored) | **NEVER PRACTISED with feedback**. Antenna types, coax, LNA/bias-tee and line of sight are never taught |
| C4 | M10-M16, M24-M28 | The learner watches a pre-tuned decoder fill (G, passive). They never choose the frequency, decoder or flags. RDS is fixed at 96.0 (`rds.py:21`), DMR at 450.0 (`dmr.py:24`). dsd-fme, satdump and sstv are not installed (STATUS) | none | PASSIVE ONLY |
| C5 | M5, M15, footnotes in M9/M10/M13/M14/M26, M17-M19 | M5 try is four scenarios sorted "in your head" (no input). M5 predict is one item with a leaky label | none (exam T1 covers Part 97, not ECPA/§605) | WEAK, and M5/M15 state the law wrongly (§5) |
| C6 | M3 (provenance), M19 ("save the capture") | M3 baseline only. No IQ/SigMF, no reception log, no export | none | **NEVER PRACTISED** |
| C7 | M17, M18, M19, M3, the honest-empty states | M18/M19 panels (G, passive). They depend on wlan1 monitor provisioning (STATUS "DEPLOYMENT FOLLOW-UP") and on the box joining the Orbic's WiFi, which no step teaches | leaky predicts (M17, M19) | Taught well; practised thinly |
| C8 | M11, M28 | Reading received packets only, and it is a "mesh of one" (28:35, 28:42). The learner never sends, never sets a channel, key, hop limit or preset; no meshtastic CLI | none | **NEVER PRACTISED** |
| C9 | M9 (Read RAW), M29 (flipper branch) | M9 Read RAW on the device (F: the Flipper is the real tool). M29 device_info mirror (G). Capture-to-console analysis is not wired (M30 parser exists) | none | PARTIAL |
| C10 | M20-M23 (no-RF theory), M4, M7 | **none.** U6 is `hardware: []`. The console can't show an image, alias, DC spike or overload on demand | unscored predicts | **NEVER PRACTISED** |
| C11 | M5 + exam trainer | Leitner study cards + the 35-question sim (409-question 2026-2030 pool) | **sim is scored, pass at 26** | **STRONGEST, and the only assessed outcome.** Not linked to any lesson; no TX path exists |
| C12 | Tool names appear in copy (rtl_433, dump1090, multimon-ng, acarsdec, redsea, rtl_ais, DSD-FME, satdump, meshtasticd, tcpdump), but no argv is shown, and 04:24 cites a `make ppm-reference` target that does not exist | **none** | none | **NEVER PRACTISED** |

**Layer count.**
- Garden: 6 outcomes are practised at all (C1, C4, C5 weakly, C7, C9, C11).
- Rack: 0 (`/operate` is not built).
- Real tool: 1 (C9, via the Flipper itself).

**Never practised: C2, C3, C6, C8, C10, C12.** Together these are the transfer gap MC raised. The
course teaches *about* the field tasks but never has the learner *do* one without the script.

**Missing topics** (never mentioned, or named without being taught):
- dBm vs dBFS (the gain meter's numbers are unlabelled relative dB)
- Hz basics and the word "bandwidth"
- bins and resolution bandwidth
- SSB, CW and HF
- AFSK vs FSK
- MSK/GMSK, and ACARS's AM-MSK
- PPM (pulse-position) for ADS-B
- squelch
- link budget and propagation, line of sight
- Doppler and pass prediction (M24 says "a predicted pass", 24:71, and never says how)
- direction finding
- antenna types, coax and connectors
- bias-tee and LNA in practice
- band plan
- the signal-ID workflow
- recording, SigMF and replay
- logging and report tradecraft
- per-band legal detail (Joffe and WiFi payload, §15.121 cellular, state mobile-scanner laws)

## 3. Prerequisite graph

### 3a. Concept glossary: first use vs first definition (teaching order)

| Concept | First used (pos) | First defined (pos) | Gap |
|---|---|---|---|
| waterfall axes | 01 (1) | 01 (1) | ok |
| Hz/kHz/MHz, frequency | 01:21 (1) | never | minor for this learner |
| channel width ("bandwidth") | 01:22 (1) | 01:41-44 (1), as "width". The word "bandwidth" is never used | S2 |
| **dB** | **02:20 (2, demo "At 0 dB")** | **02:47 (2, last lines of explain)** | forward ref within the lesson |
| noise floor, SNR | 02:21-22 (2) | 02:21-22 (2) | ok |
| **front end** | **03:36 (3, "dying front end")** | **20:20-22 (19)** | **16-position forward ref** |
| **overload** | **07:31, 07:51 (7)** | **20:37 (19, one clause)** | **12-position forward ref**. M7's whole reveal depends on it |
| **sampling** | **04:45-46 (4, "the part that samples it")** | **22:20, 23:20 (21-22)** | forward ref |
| stereo pilot | 04:22 (4) | 04:22 (4), misdescribed as an RF tone (§5) | false definition |
| carrier | 08:24 (8) | 08:24 (8) | ok |
| modulation, AM, FM | 08:18 (8) | 08 | ok |
| **ISM band** | **05:28 (5)** | **never**, and misapplied to 433 MHz in the US (09:27) | undefined and wrong |
| **bin / resolution** | **10:41, 10:48, 10:58 (10)** | **never** (23:36 "every pixel is an FFT number" is the closest, pos 22) | undefined |
| demodulate | 10:29 (10) | never (inferable) | S3 |
| phase | 12:9 (12), working definition "the timing of the wave" | 22:20-21 (21) | acceptable: defined in place, formalised later |
| subcarrier, MPX | 12:11 (12) | never | undefined |
| AFSK (vs FSK) | 14:12 (14) | never ("two AFSK tones", 14:34) | undefined |
| baud vs bps | 14:61 (14) | never | S3 |
| pulse-position (ADS-B) | 13:57 ("the pulses") | never named | gap; would collide with "ppm" |
| GMSK / MSK | 26:11 (17) | never | undefined |
| 4FSK / C4FM | 27:10 (18) | 27:10 inline | ok |
| front-end overload as "false copies" | 07 (7) | 20:37 (19) | see above |
| LNA, noise figure | 20 (19) | 20 (19) | ok; never practised |
| I/Q, LO, image | 21-22 (20-21) | 21-22 | ok |
| FFT, Nyquist, alias | 23 (22) | 23 (22) | ok. Every decoder at pos 13-18 relied on a sample rate the copy never mentioned |
| **Flipper operation** | **09:32, 09:37 (9), "Sub-GHz then Read RAW", "replay a capture"** | **M29 (29, unmerged)** | **20-position forward ref**; capture and replay are never taught |
| **2nd Meshtastic node** | **11:35 (11), "Key the second node"** | **28:35-43 (28), "a mesh of one … MC is bringing one"** | the forward ref is to a device the kit doesn't have |
| pass prediction / Doppler | 24:71 (26) | never | gap |
| "one-tap correction" | 04:56 (4) | never built | dangling |
| "Wi-Fi and satellite links waiting ahead" | 12:63 (12) | no WiFi lesson; M24 is analog APT | dangling |
| OOK *decoding* ("the next climb") | 09:60-62 (9) | never (perhaps M30 later) | dangling |

### 3b. Forward references (a concept used before it is defined, in teaching order)

1. dB: 02:20 (demo) and 02:27 (try), defined at 02:47.
2. Front end: 03:36, then defined at pos 19 (M20).
3. Overload: 07:31 and 07:51, then defined at pos 19 (M20:37).
4. Sampling: 04:45-46, then defined at pos 21-22.
5. Bin: 10:41/48/58, never defined.
6. ISM band: 05:28 and 09:27, never defined.
7. Subcarrier: 12:11 and 12:34, never defined (and the pilot at 04:22).
8. AFSK: 14:12, never defined.
9. GMSK: 26:11, never defined.
10. Flipper operation: 09:32/37, then defined at pos 29.
11. Node B: 11:35, then appears at pos 28 as absent.
12. Phase: 12:9, then defined at pos 21 (acceptable, defined in place).
13. Dangling promises: 04:56 one-tap correction, 12:63 WiFi/satellite PSK, 09:61 OOK decoding,
    24:71 predicted pass.

**Reading of the graph.** Unit 6 is placed as late consolidation, as the educator amendment asked
(amendment §0.3: "curiosity payoffs after thirty waterfalls"). That is defensible. The problem is
that two of its concepts are load-bearing much earlier:
- **front end and overload** are used in M3 and M7;
- **bin and resolution** are used in M10.

Both have a cheap just-in-time fix: one sentence each where they are first used (§7, §8).

---

## 4. Fidelity check: instructions that cannot be done on the console as built

What the console can do (origin/main):
- **Waterfall.** `rtl_power` sweeps 88-108 MHz in 125 kHz bins by default (`spectrum.py:20-24`).
  It retunes only when a pack has `tune:` (`app.py:476-479`); M1-M8 have none. A retune sends
  only the low and high edges, never a bin size (`waterfall.js:24-29`), so M9's 2 MHz band is just
  16 pixels of 125 kHz each.
- **Tuning.** +/- and tap-to-tune change only the readout (`console.html:741-760`). The tap is
  mapped onto a ±1.3 MHz span while the canvas shows 20 MHz, so the readout reports a **wrong
  frequency**. The ppm button measures whatever that readout says (`ppm.js:23-25`).
- **Gain.** No control. `Sweeper.set_gain` exists (`spectrum.py:597`) but no route calls it, and
  the sweep runs on automatic gain.
- **Missing controls.** No presets, no station list, no audio, no radio picker (the device comes
  from an env var, `app.py:198`), no ppm correction (the chip always reads RAW, `base.html:24-27`),
  no recording, no LoRa send, no observe input.
- **Completion.** The Complete button exists only on ux-polish.
- **Fixed frequencies.** RDS 96.0 MHz (`rds.py:21`; the pack's `tune` is never passed in,
  `app.py:227-232`), DMR 450.000 MHz (`dmr.py:24`). The ADS-B scope needs
  `SIGINT101_RX_LAT/LON` env vars (`app.py:66-67`, `adsb.js:108-112`). The provenance reference is
  fixed at 106.5 MHz (`provenance.py:15`).

| # | pack:line | Instruction | Why it can't be done | Sev |
|---|---|---|---|---|
| F1 | 01:21-22 | "I sweep the FM band … and tune the strongest station near you" | No auto-tune. The dial is a hardcoded 100.300 (`console.html:18`) | S1 |
| F2 | 01:28-31 | "tap a different strong one — any other bright stripe on the list. Watch the screen jump to it" | There is no list. A tap doesn't retune, and its readout is computed on the wrong span (a false frequency) | **S0** |
| F3 | 01:33, 01:37 | "tune two notches up the dial" | "Notch" is undefined and the dial is display-only. Doable only by eye on the full sweep | S1 |
| F4 | 02:22-24 | demo "Now I raise the gain" | Nothing is automated; there is no gain route | **S0** |
| F5 | 02:26-28 | "Step the gain from the bottom up … three settings" | No gain control | **S0** |
| F6 | 02:34 | "Take the gain to maximum" | No gain control | **S0** |
| F7 | 03:24-26 | reference = "the strong local FM station from Lesson 1" | The reference is fixed at 106.5 MHz, chosen for MC's site. At the recipient's site it may be an empty channel | **S0** (needs MC: recipient location) |
| F8 | 03:40-42 | "Put your empty-band waterfall beside your captured baseline" | No side-by-side or history view, and no way to tune to an empty band | S1 |
| F9 | 04:24 | "with `make ppm-reference` against the Pi's clock" | No such target (Makefile has bootstrap/test/lint/coverage/selftest/run) | S1 |
| F10 | 04:25-27, 04:31-32 | "plug in the kit's RTL-SDR V3 … Switch to the V3" | No radio picker. The serial is passed as an rtl_fm `-d` index (recon `app.py:749-751`). This is the lesson's core proof | **S0** |
| F11 | 04:30-31 | "read where its center lands: about +17 kHz high" | 125 kHz bins can't show 17 kHz, and the panel reports ppm, not a centre offset | S1 |
| F12 | 04:56-58 | "there is a one-tap correction … Turn it on" | It doesn't exist; the chip is always RAW | **S0** |
| F13 | 06:22-23 | "just tune up to 1090 MHz … Watch… almost nothing" | No `tune:`; the view stays on 88-108 | **S0** |
| F14 | 06:31-36 | "Keep the radio on 1090 … faint returns prick up" | No 1090 view, so no feedback | **S0** |
| F15 | 07:21-31, 07:33-37 | "radio on the aircraft band … watch the SNR … On 1090 … read the SNR" | No 1090 view, and M7 has no meter (`panels: []`); the status-bar SNR is always "—" | **S0** |
| F16 | 08:22-31 | airband controller channel, a "line" with brightness "around" it | No airband tune, and a <10 kHz AM signal is a single 125 kHz bin, so carrier and sidebands can't be told apart | **S0** |
| F17 | 08:34-35 | "Tap between the two presets" | No presets | **S0** |
| F18 | 08:46-48 | "Go back to the airband … through a whole exchange" | Impossible, and the observation contradicts the explain (§5) | **S0** |
| F19 | 08:10-11, 08:33-34, 08:38, 08:63-64 | "by sound", "leave the volume down", "when you do finally listen" | There is no audio path anywhere in the product | S1 |
| F20 | 09:36-37 | "Key a 433 MHz remote … or let the Flipper replay a capture" | US remotes are mostly 315/390 MHz (§5). The Flipper SD is stock-empty and capture is never taught. Near-field TX overloads the RTL front end (STATUS M9 caveat) | S1 |
| F21 | 11:35-36 | "Key the second node near the Nomad, or send a message from it" | No node B in the kit per the repo (28:42, STATUS). The console has no send. Also unsafe at close range: keep it ≥30-50 cm because of the SX1262's +10 dBm input limit (ports survey item 4) | **S0** (needs MC: is node B in the kit?) |
| F22 | 12:31 | "redsea is listening to 96.0 MHz, the strong local station" | Fixed at 96.0, MC's local station; it can't be changed in the console | S1 (needs MC) |
| F23 | 13:32-34 | the scope places "a contact … on its ring" | Without the RX_LAT/LON env vars the panel shows "receiver location not set". The kiosk learner can't set it, and the HAT's GPS isn't read | S1 |
| F24 | 18 (all instrument stages) | "a plain USB WiFi adapter, switched into monitor mode" | Monitor mode and tcpdump caps are manual HIL steps, not provisioned (STATUS); the adapter must be in the kit | S1 (provisioning) |
| F25 | 19:34-35 | "your console joins the Orbic's WiFi and simply polls that page" | No WiFi-join step in the kiosk and none taught | S1 |
| F26 | 22:37 | "1.2 above your tuning and 1.2 below — the waterfall's width" | The console's waterfall is a 20 MHz stitched sweep | S1 |
| F27 | 24:30-31 | "A wire antenna and your dongle can catch it" | NOAA APT is off the air, and the panel only runs NOAA APT; satdump isn't installed | S1 |
| F28 | 24:33 | "The waterfall is parked on the 137 megahertz … band" | M24 shows a decoder panel, not a waterfall | S2 |
| F29 | 27:21-23, 27:49 | "You decode a busy DMR channel" | Fixed at 450.000 MHz, which is not a channel; no tune; dsd-fme not installed | S1 |
| F30 | 27:40-41, 27:44-45 | "tells you whether real voice came through"; "listen: did clear speech come with it?" | The panel shows TG/source/kind only (`dmr.js:43-50`): no voice or encryption flag, no audio | S1 |
| F31 | all 28, observe stage | "Tell me what you actually see" | No input; nothing is captured | S2 (§6) |

**Placement contradictions** (copy that disagrees with where the lesson sits):

| pack:line | Says | Reality | Sev |
|---|---|---|---|
| 16:13-14, 16:25, 16:68 | "This closes the decode unit", "One last signal", "Unit complete" | U5 continues with M26 and M27 | S1 |
| 23:16 | "The last lesson, comrade" | Six more units follow (U7-U10) | S1 |
| 25:9, 25:25 | "Every decode so far / in this unit … handed you words" | The lesson before it in this unit (M24) handed an image | S1 |
| 25:14, 25:62 | "Same unit lesson", "the unit's whole lesson" | Refers to U5; M25 is in U8 | S2 |
| 20:16 | "Every lesson so far you worked the radio from outside" | M4 (crystal clocking the tuner and sampler) and M7 (front-end overload) already went inside | S2 |
| 17:17 | "Every lesson so far … you turned the radio outward" | The unit right before it (U6) turned it inward (23:16) | S2 |
| 17:53 | "then one for the fake towers" (that you build) | M19: "you borrow a different radio" | S2 |
| home/unit tiles | Codes `L{id}` (`app.py:164`) | U5 shows L13-16 then L26-27; U6 L20-23 comes before U7 L17-19 | S1 |
| 28:42, 28:66 | "MC is bringing one", "When MC's second node arrives" | Personal reference in the public product copy (D10) | S1 |

---

## 5. Factual accuracy (with sources)

US law only (D4). Primary sources wherever they exist; the items marked "(secondary)" had none.
LII = law.cornell.edu. Line numbers are re-checked against the packs.

### 5a. Wrong: teaches something false (13 items, ranked by how badly they'd mislead an operator)

| # | pack:line | Claim | What's true | Source | Sev |
|---|---|---|---|---|---|
| X1 | **15:55-58** (and the live panel) | "Receiving radio is passive and legal almost everywhere; acting on, recording, or repeating … very often is not" | Display pages are "electronic communications" (only tone-only pages are excluded, 18 USC 2510(12)(B)). Anything carried by a common carrier is **not** "readily accessible to the general public" (2510(16)(D)), so 2511(2)(g)(i) does not apply. **Decoding the text is itself interception** (2511(1)(a); up to 5 years, 2511(4)(a)). 929-932 MHz commercial paging is CMRS, which is treated as a common carrier (47 USC 332(c)(1)). In 1997 SDNY charged Breaking News Network staff in what prosecutors called the first ECPA pager-interception case. §605 separately bars divulging or using the contents. **M15's panel runs live on 929.6 MHz (`pocsag.py:22`), so working the lesson as written is the offence.** | LII 18/2510, 18/2511, 47/332, 47/605; rcfp.org/news-service-operators-arrested-intercepting-pager-signals | **S0, needs MC** |
| X2 | 05:24-26 | You "may not pass on or act on certain private communications … such as cellular" | Intercepting a cellular call is itself the offence: it is a "wire communication" (2510(1)) carried by a common carrier, 2511(1)(a). Scanners able to tune cellular are barred (47 USC 302a(d), 47 CFR 15.121) | LII 18/2510, 18/2511, 47/302a, cfr 47/15.121 | **S0** |
| X3 | 04:48-52 | +179 kHz at 1090 MHz is "absolutely fatal … the planes simply wouldn't be there" | Mode S tolerance is 1090 ±1 MHz and the signal is about 2 MHz wide; dump1090 at 2 MS/s shrugs off 179 kHz. The ppm error bites on narrow channels: 27 kHz at 162 MHz (AIS), 74 kHz at 450 MHz (DMR). This sends a learner debugging ppm when the real ADS-B fault is the antenna (STATUS: the stock whip is deaf at 1090) | LII cfr 14/43 App. F; github.com/antirez/dump1090 | **S0** |
| X4 | 08:22-24, 08:46-53 | A controller's channel shows "a single hard, bright line" before anyone talks; the carrier stays lit "in a gap between transmissions" | ATC is push-to-talk: there is no carrier between transmissions (except continuous ATIS/AWOS). Between words *within* one transmission, the AM carrier does stay | faa.gov AIM ch. 4 §1 (ATIS continuous) | **S0** |
| X5 | 06:58 | "a quarter-wave in centimetres is 300 over the frequency in MHz, then quartered" | It is metres; the concept line, 06:10, has it right | arithmetic | **S0** |
| X6 | 09:12, 09:25, 09:27; 10:12, 10:29 | 433.92 is "the little ISM band"; "garage remotes … half the 433 MHz band" | 433.05-434.79 is ISM only in ITU Region 1 (RR 5.138); it is not on the US list (47 CFR 18.301). US devices run under 15.231 inside the 70 cm ham band. US garage remotes are mostly 300-390 MHz (Security+ 2.0: 310/315/390); car fobs are mostly 315 MHz | LII cfr 47/18.301, 47/15.231; LiftMaster compatibility chart | S1 |
| X7 | 10:12, 10:33, 10:56 | "a weather sensor often reads FSK" on 433 | rtl_433 decodes Acurite and LaCrosse TX141 at 433.92 as OOK. US FSK sensors mostly sit on 915 MHz. **M10's try ("Find one FSK read") may be impossible in the US** (MC's own HIL saw only ASK) | github.com/merbanan/rtl_433 `src/devices/acurite.c`, `lacrosse_tx141x.c` | S1 |
| X8 | 17:25-27; 18:10-12, 18:24-25 | The camera "phones home" over the radio whose WiFi fingerprint you catch | Flock uploads over **LTE**. The WiFi radio is separate (station-mode probes about every 125 ms); that is what flock-you fingerprints. The plan said the same (eager-spinning-frost:241) | github.com/colonelpanichacks/flock-you README | S1 |
| X9 | 19:21-23 | The phone-tower conversation "never touches the open air you can sweep" | LTE signalling *is* over the air. SIBs are unencrypted (EFF's own heuristics doc), and LTE-Cell-Scanner finds cells with an RTL dongle. The barrier is a 5-20 MHz carrier plus a full LTE stack | github.com/EFForg/rayhunter `doc/heuristics.md`; github.com/Evrytania/LTE-Cell-Scanner | S1 |
| X10 | 04:21-23 | The pilot is "at exactly 19 kHz above its center" | The pilot is a baseband subcarrier at 19 kHz in the *demodulated* audio, held to ±2 Hz. It is not an RF tone above the carrier. **The code is right** (`ppm.py:19-25` demodulates with `rtl_fm -M fm` and measures the tone in audio); only the copy is wrong | LII cfr 47/73.322 | S1 |
| X11 | 12:9-10, 12:25-27, 12:52-56, 12:63 | A waterfall "can never show" PSK; "the Wi-Fi and satellite links … hide there" | A PSK signal fills about its symbol rate in bandwidth and shows plainly as energy (WiFi is a 20 MHz block). The waterfall can't show *which phase*, i.e. the bits. RDS is invisible only because it is a small subcarrier inside the FM baseband | LII cfr 47/73.322 | S1 |
| X12 | 11:32 | "A plain carrier at ten dB below the noise is gone" | SNR depends on bandwidth. A carrier at -10 dB in 250 kHz stands about +14 dB in a 1 kHz FFT bin. A narrow bin is processing gain too, so for a SIGINT operator this gets narrowband detection backwards | 10·log(250k/1k) = 24 dB | S1 |
| X13 | 12:32-33 | "the station's call sign — the PS name" | PS is an 8-character service name; US call letters are encoded in the PI code | NRSC-4-B (nrscstandards.org) | S2 |

### 5b. Imprecise (fix as you touch the copy)

| pack:line | Issue | Fix | Source |
|---|---|---|---|
| 01:41-43 | "From one station to the next the stripe is the same width": US hybrid HD Radio stations occupy about 400 kHz (digital sidebands about 129-199 kHz each side), so some stripes *are* wider | Add a one-line HD Radio note | US 7305056 (patent; secondary); engineeringradio.us (secondary) |
| 02:38-41 | "the radio starts adding hiss of its own": the R820T noise figure does not worsen at max gain. The top-end shrink is overload (8-bit ADC clipping, intermod, spurs) | "strong signals overload the radio and smear false copies" | rtl-sdr.com intermodulation measurements; R820T datasheet (ab9il.net) |
| 05:28-29, 05:50-52 | Calls 915 MHz "short-range transmitting … no personal licence". 902-928 is ISM in Region 2 (18.301) but TX rights come from Part 15.247 (up to 1 W + 6 dBi), and LoRa is not short-range | "licence-free transmitting under FCC Part 15, inside set power limits" | LII cfr 47/15.247 |
| 06:55-56 | "too long is every bit as deaf as too short": long elements resonate again at odd multiples of λ/4; for RX, SWR <5 costs about 2.5 dB | "the wrong length, long or short, costs you signal" | rtl-sdr.com/using-our-new-dipole-antenna-kit |
| 11:54, 11:13, 11:56 | -20 dB is SF12. LongFast (SF11/250 kHz) floors at about -17.5 dB. "Coin cell" is false for an SX1262 at +22 dBm (about 118 mA) | "down to about minus seventeen on LongFast"; "a small battery" | Semtech SX1276 datasheet Table 13; meshtastic.org/docs/overview/radio-settings |
| 11:58-60, 28:62-63 | "A node on a channel you are not on still carries your message": only if it shares the modem preset *and* the frequency slot, which is hashed from the primary channel name | "a node sharing your radio settings carries it onward, even on a channel it can't read" | meshtastic.org/docs/configuration/radio/lora |
| 12:11, 12:28 | "every FM broadcaster" carries RDS: RBDS is voluntary in the US | "most FM stations" | NRSC-4-B |
| 12:35 | "eleven hundred times a second" (it is 1187.5) | "nearly twelve hundred" | NRSC-4-B |
| 15:9-10, 15:25-26 | "Most use POCSAG": US carriers run FLEX/ReFLEX on 929-932. **The panel runs only POCSAG demods** (`pocsag.py:28`), so it will likely stay empty on US commercial paging | "POCSAG and, in the US, FLEX" | github.com/EliasOenal/multimon-ng |
| 16:31 | "the main ACARS channel": 131.550 is busy in North America, Europe's primary is 131.725, and much traffic is now VDL2 on 136.975. The AM-MSK 2400 bps modulation is never named | "one of North America's busiest ACARS channels" | docs.airframes.io/docs/technology/acars/intro |
| 18:56-58 | "better still an SSID keyword — 'flock' or 'penguin'": "Penguin"/"FS Ext Battery" are the battery pack's BLE names; flock-you's SSID-keyword tier is the weakest and off by default | "a 'Flock-' network name is a bonus" | flock-you README tiers |
| 19:30-33, 19:44 | Line colours: low = yellow dots, medium = orange dashes, only high = solid red; white = paused | "yellow, orange, or solid red as warnings grow" | github.com/EFForg/rayhunter `doc/using-rayhunter.md`, `generic_framebuffer.rs` |
| 24:15, 24:72-73 | "Meteor-M2 series": only Meteor-M N2-3 and N2-4 send LRPT, near 137.9 MHz (N2-3 is weak) | "Meteor-M N2-3 and N2-4, near 137.9 MHz" | space.oscar.wmo.int/satellites/view/meteor_m_n2_4 |
| 26:59-60 | Type 5 "arrives in two fragments": over the air it is one 2-slot burst; the split is NMEA's 82-character limit | "the decoder hands it up as two AIVDM sentences" | navcen.uscg.gov/node/85; gpsd AIVDM |
| 27 (explain) | No legal note | "Unencrypted public-safety voice is lawful to hear federally (2511(2)(g)(ii)(II)); decrypting it is not; some states restrict scanners in cars" | LII 18/2511 |

### 5c. Checked and correct

| Topic | Source |
|---|---|
| NOAA-18 off 6 Jun 2025, NOAA-19 13 Aug, NOAA-15 19 Aug ("the last of them that August"); 137.620/137.9125/137.100 | ospo.noaa.gov/data/messages/2025/08/MSG_20250819_1547.html |
| APT: AM 2400 Hz on an FM carrier, 120 lines/min, about 15-min passes at about 800-870 km | (secondary) |
| ISS SSTV on 145.800 FM, PD120, 1200 Hz sync | ariss.org |
| AIS 161.975/162.025, GMSK 9600, M.1371 | itu.int/rec/R-REC-M.1371 |
| ACARS from 1978, mostly unencrypted | docs.airframes.io |
| POCSAG 512/1200/2400 | ITU-R M.584 |
| RDS 57 kHz = 3 × 19, 1187.5 bps | NRSC-4-B |
| V3 1 ppm TCXO | rtl-sdr.com |
| 300/f; 6.9 cm @ 1090; 8.2 cm @ 915 | arithmetic |
| DMR AMBE+2; P25 Phase 1 IMBE/C4FM; Phase 2 AMBE+2/TDMA; DSD-FME needs mbelib | github.com/lwvmobile/dsd-fme |
| Meshtastic hop limit 3; LongFast key is public | meshtastic.org |
| Rayhunter on Orbic, 192.168.1.1:8080; all three EFF quotes in 19:53-55 | rayhunter `doc/heuristics.md` |
| US 2G gone (AT&T 1 Jan 2017; T-Mobile per its network-evolution page); a downgrade is still a real catcher trick; Android 12+ 2G toggle. A teaching gift: *any* 2G cell in the US is itself a red flag | t-mobile.com/support/coverage/t-mobile-network-evolution; source.android.com |
| 2511(2)(g)(ii)(I)/(III)/(IV) make ADS-B, APRS, ACARS and AIS lawful to receive | LII 18/2511 |
| rtl_433 `mod`/`freq1`/`freq2` with `-M level` (the console passes it, `decode.py:181`) | rtl_433 `r_api.c` |
| R820T low-IF with I/Q formed in the RTL2832U; M21/M22's two-mixer picture is an acceptable beginner simplification | osmocom rtl-sdr source |

Not resolved:
- whether Meteor N2-3/N2-4 LRPT is live this week;
- an official Flock spec sheet (the LTE claim rests on flock-you plus teardown and procurement sources);
- how the Breaking News Network case ended.

---

## 6. Assessment validity and learning science

### 6a. Predict design: leaks and scoring

| Lesson | Answer (from explain) | Where it leaked before the commit |
|---|---|---|
| M1 | same | not leaked (but see HD Radio, §5) |
| M2 | narrower/same | demo 02:23-24 "opens, then stalls, … can even close" |
| M3 | identical | frame 03:17-18 + concept banner |
| M4 | bigger | the question itself, 04:36-37 "it scales with frequency" |
| M5 | illegal | label "Illegal — transmitting needs a licence" + demo 05:26-27 |
| M6 | longer | **not leaked**: a real transfer item (apply 300/f) |
| M7 | no (and "depends" is defensible) | ambiguous key: the explain says "no — not on its own" and then gives the "it depends" case |
| M8 | neither | not leaked, but observe 08:47 asks about the gap *between transmissions*, where the carrier does vanish |
| M9 | floor | demo 09:29-30 "switching clean off … no carrier left behind" |
| M10 | shift | frame 10:24 "shifts it between two frequencies" + concept |
| M11 | decodes | demo 11:27-28 "often NEGATIVE … and yet the packet decoded" |
| M12 | hidden | frame 12:26-27 "a waterfall … is blind to it" + concept |
| M13 | open | frame 13:27-28 "No permit, no key, no secret" + the question's wording |
| M14 | framed | concept 14:9-10 "inside it is a fully framed packet" |
| M15 | readable | demo 15:34-35 "readable the instant it is demodulated" |
| M16 | text | demo 16:35-36 "nothing you had to break" |
| M26 | mmsi | concept 26:13 "keyed by the ship's MMSI" |
| M27 | ids, voice sometimes | try 27:45-46 + concept; the distractors are absolutes |
| M20 | quiet-first | demo 20:22 "the FIRST stage's sets … sensitivity" |
| M21 | image | label "Yes — its mirror below the LO" |
| M22 | full | label "2.4 MHz — the full rate"; distractor "Only 100 MHz itself" is a strawman |
| M23 | aliases | mild ("A ghost at the wrong spot") |
| M17 | proves-nothing | label "No — silence proves nothing" + concept |
| M18 | weak-hint | concept 18:13-14 "a bare OUI is a weak hint" |
| M19 | look-closer | label "No — red means look closer" + concept |
| M24 | grainy | concept 24:13-14 "a weak pass is grainy, never simply gone" |
| M25 | photo | concept + demo 25:40 |
| M28 | relayed | concept 28:11-12 |
| M29 | a-bench | label "A bench: screen + storage" vs two strawmen |

- **The concept banner leaks most of these.** `console.html:51` prints `lesson.concept` in the
  eyebrow of the frame stage, which every learner reads before predicting. For 13 lessons that
  banner states the answer.
- **Scoring.** There is no `answer` key anywhere. `progress.record_prediction` stores the chip key
  and nothing compares it to anything (`progress.py:91-118`). The explain never says "you
  predicted X".
- **Retry.** The form is disabled after commit; the store still allows an overwrite
  (`ON CONFLICT … DO UPDATE`).
- **Net effect.** In 23 of 28 lessons the predict gate measures reading of the previous screen,
  not prediction. Only M1, M6, M8 and M23 are genuine predictions (M7's key is ambiguous), and **only the early "change X"
  items** (M1, M2, M4, M6, M7) follow the educator amendment's "informed predict = what if we
  change X?" design. From M9 on, the predict restates the demo.

### 6b. Cognitive load (words per stage; reading budget ≈ 50 words per 800x480 screen, measured, STATUS)

| | frame | demo | try | predict | observe | explain |
|---|---|---|---|---|---|---|
| U1-U5, U7-U9 range | 39-111 | 78-206 | 48-120 | 21-68 | 32-84 | 130-294 |
| U6 (M20-M23) | 40-45 | 46-49 | 40-44 | 24-26 | 39-49 | 46-49 |

- Worst explains: M8 294, M28 277, M10 261, M7 259, M16 255, M12 254, M9 251, M11 249, M6 243.
  The median explain is 222 words, which is about 4.5 screens of scroll on ux-polish.
- **Demo is the real load problem.** On instrument lessons the demo (mostly 113-206 words) is read in a
  thin narration band under the panel while the learner is told to "watch". That is classic split
  attention: one set of eyes cannot read 180 words and watch a waterfall at once.
- U6 proves the ~50-word form works. Its explains still carry the core idea. They lose the
  "carry two things out the door" lines, which are good retrieval targets and belong somewhere.

### 6c. Worked example, practice, retrieval, spacing, fading

| Principle | Plan / amendment | As built |
|---|---|---|
| Worked example (I-do) | "Elmer drives the radio and narrates" | **The demo is narration of actions the console never performs** (M2 "I raise the gain", M4 "I plug in the V3", M6 "I … tune up to 1090", M7 "I slide an FM-notch filter"). On decoder lessons the "demo" is just the live panel. There is no worked example of a *decision* |
| Practice (you-do) | "learner operates, steps pre-filled" | "try" means watch the panel fill. The learner makes **no operational decision** (frequency, gain, decoder, flags) in 28 lessons |
| Retrieval | 2-3 cards from earlier lessons open every lesson (amendment §1.1) | none. The exam's Leitner engine exists but is siloed |
| Spacing | Leitner/SM-2 in SQLite | none for lessons. Each concept appears once. The callbacks (M26→M13, M28→M11) are narrative, not retrieval |
| Fading | demo-first intro → cold-predict `consolidation` packs (amendment §1.1); fade from U5 on (MC 11-02) | **none.** `consolidation` exists in `lessons.py` but no pack uses it. M28 carries the same scaffold as M1 |
| Feedback | per-unit tiers A/B/C, honest badges | none. Badges and replay marks are a CLAUDE.md invariant with no code |
| Interleaving | not in plan | none. Units are blocked by topic, which is fine for an intro course but weak for signal ID |

### 6d. A minimal assessment upgrade that fits in 6 days

| Step | What | Cost | Why it pays |
|---|---|---|---|
| A1 | Add `answer:` (a list, so M2/M7 can accept two keys) and a one-line `verdict:` to each pack. The explain header shows "You said *X* · Answer: *Y*". Store `correct` with the response | S | Turns 28 gates into scored items, and gives a per-unit score for free |
| A2 | Neutralise the 9 leaky labels (§8 R8). Stop printing `concept` on the frame eyebrow (keep it on explain) | S | Removes the cheapest leaks without rewriting the demos |
| A3 | "Observe snapshot": entering observe POSTs the live panel's state (count of decodes, SNR, empty/no-radio, ppm state, radio serial) to `events` | S-M | The first honest reception log. It is the substrate for badges and C6, and it costs one route plus a JS hook |
| A4 | A unit-end "Quick check" stage: 3-5 tap chips per unit drawn from that unit's verdicts. Nine checks, scored | M (update) | Retrieval + spacing with no new engine |
| A5 | Exam cross-links. Add `exam: [T9A04, T9A09, T9A10]` to packs and a `?q=` param on `/exam` | S | Joins C11 to the lessons. Map: M2→T5B09-11 (dB); M6→T9A04/09/10; M7→T3A03/T9A03 (polarisation); M14→T8D03/05, T1D03; M24/M25→T8B01-11 (satellite, T8B07 Doppler); M27→T8D07, T2B07; M13→T3C (line of sight) |

Before 11-08: A1, A2, A3 (and A5 if cheap). Via update: A4, then real retrieval cards in the
lesson open stage on the existing Leitner store.

---

## 7. Sequencing recommendation

**Unit 6: don't move it wholesale. Add just-in-time inserts now, split it later.**
- Moving U6 before U5 would put four no-RF theory screens between first light and the decode
  payoff. That is the motivation dip the amendment placed it late to avoid. The decoders don't
  need U6's theory to *work*.
- What U6's late placement actually breaks is three early uses:
  - front end in M3 and M7;
  - overload in M7;
  - bins in M10.
  One sentence at each first use fixes all three for 11-08 (§8 R13).
- Via update, split it:
  - Move **M20 (front end, noise figure, overload) into U3, right after M7**. It is the theory M7's
    notch reveal and M2's max-gain stall both need. The cost is a units.py tuple edit, plus the
    M20 frame and M21 "last lesson the front-end…" frame copy.
  - Keep M21-M23 as late consolidation.
  - Make all four hands-on, as the amendment's M17 intended: show the image, the DC spike and an
    alias with real tuning. Until then U6 teaches C10 without practising it.

**IDs: do not renumber. Display teaching-order codes instead.**
- The cost of renumbering is not mainly progress.db. There is no learner data before hand-over,
  so a fresh DB costs nothing. The cost is churn across:
  - URLs `/lesson/{id}`;
  - `events.jsonl` payloads;
  - ~30 test files (`test_lesson_mNN`);
  - `units.py`;
  - the flipper branch (M29-M38);
  - STATUS/learnings/plans, which say "M13" in hundreds of places.
  After hand-over it would also need a forward-only migration of `progress` and `responses`
  (cost M).
- The cheap fix (S): compute the tile code from the position in the flattened `UNITS` order
  (`app.py:164`) instead of `L{id}`.
  - ids 1-16 equal positions 1-16, so the existing copy refs ("Lesson 1/11/13", 03:25, 04:21,
    08:28, 12:37, 16:9/25/48, 26:9/24, 28:9/21/28/62) **stay correct**;
  - AIS displays as L17, DMR as L18, U6 as L19-22, U7 as L23-25, and so on;
  - ids stay the storage keys.
  Add a contract test that every `Lesson N` in copy refers to the lesson at position N.

---

## 8. Remediation list, ranked by learner impact ÷ cost

Severity follows the brief: S0 = false, or impossible on a hand-over-critical path; S1 = misleads
or confuses; S2 = polish; S3 = update track. Cost: S/M/L. "MC" = needs MC's input or hardware.

### 8a. Before 11-08

| Rank | ID | Sev | Cost | Fix | Notes |
|---|---|---|---|---|---|
| 1 | R1 | S0 | S + **MC** | Legal copy for M15 and M5 (rewrites below). **MC decides what M15's live panel does.** Options: (a) keep live text and state the law accurately, so the learner decides (D16 purist); (b) show cap code, rate and length live, with the text behind a tap (the amendment's R4 approach); (c) live framing plus a labelled *replay* of a lawful test transmission for the words. I recommend (b)+(c) | As written, the lesson walks the learner into a federal offence while calling it legal |
| 2 | R2 | S0/S1 | S | The §5a fixes X3-X13: M4 ADS-B and pilot, M8 carrier, M6 units, M9/M10 433 "ISM" and FSK, M17/M18 Flock LTE, M19 "never touches the air", M12 PSK and PS, M11 CW carrier, M22 waterfall width, M24 "can catch it" | Copy only |
| 3 | R3 | S0 | M | Real tuning: +/- and tap → `/api/tune` with the tap mapped to the live span, plus a gain stepper on `Sweeper.set_gain`. Add `tune:` for M6/M7 (e.g. 1089-1091 MHz) and M8 (airband, finer bins) | The technical cluster 3. It restores M1, M2, M6 and M7 as written and is the core of `/operate` v1. **If it slips, use the copy-only rewrites below** |
| 4 | R4 | S0 | S | M4: remove the one-tap correction, the V3 switch and `make ppm-reference`. Teach the real flags instead (rewrite below) | Turns a dead promise into a bridge to the real tools (C12) |
| 5 | R5 | S0 | S | M8: drop presets and audio; airband via `tune:` or a narrated "what operators see" | 125 kHz bins can't show AM structure even after R3. A finer view needs a `bin_hz` in the tune schema, carried through `parseTuneBand` |
| 6 | R6 | S0 | S/MC | M3/M12/M27/M13 location constants set at provisioning (reference station, RDS station, DMR channel, RX lat/lon, or read the HAT's GPS) | Needs MC: the recipient's area and strongest FM station |
| 7 | R7 | S0 | S/MC | M11 node B: ship one in the kit, or rewrite the try (below). Add the ≥50 cm safety line. Remove "MC" from M28 | Needs MC: is node B in the kit? |
| 8 | R8 | S1 | S | Neutralise the leaky labels (below). Drop the concept from the frame eyebrow | Assessment A2 |
| 9 | R9 | S1 | S | `answer:` + verdict line + store correctness | Assessment A1 |
| 10 | R10 | S1 | S | Placement contradictions: M16, M23, M25, M20, M17 | Rewrites below |
| 11 | R11 | S1 | S | Teaching-order tile codes | §7 |
| 12 | R12 | S1 | S-M | Command strip (the real argv from `Sweeper.command()` / `Decoder.command()`) | MC's "garden" bridge, the cheapest transfer win (C12) |
| 13 | R13 | S1 | S | Just-in-time glosses: dB first in M2 demo; "front end" in M3/M7; "bin" in M10; "subcarrier" in M12 | One sentence each |
| 14 | R14 | S1 | S-M | Observe snapshot to events | Assessment A3 |
| 15 | R15 | S2 | S | Exam cross-links | Assessment A5 |
| 16 | R16 | S1 | M | `/operate` v1 (rack) on top of R3 | Per MC's split; only if R3 lands by ~11-05 |

### 8b. Via update

| ID | Sev | Cost | Fix |
|---|---|---|---|
| U1 | S1 | M | Shorten explains to ≤120 words: verdict first, 3 "why" lines, one "carry out" line, the rest behind a fold. Shorten the instrument-stage demos to ≤60 words (split attention) |
| U2 | S1 | M | Retrieval cards at lesson open (2-3 from earlier lessons) on the exam's Leitner store; unit-end quick checks (A4) |
| U3 | S1 | L | The Signal-ID method lesson, plus an **unnamed-signal solo** per unit in the rack. This is the plan's dropped M18 and Tier C, and the C2 gap |
| U4 | S1 | M | Recording: IQ/SigMF capture with provenance, a reception log, and honest badges and replay marks (C6, the CLAUDE.md invariant) |
| U5 | S1 | M | Fading: from U5 on, stop pre-tuning or stop naming the decoder; convert later packs to `consolidation` (cold predict) |
| U6 | S2 | M | U6 hands-on, and M20 moves after M7 (§7) |
| U7 | S1 | M | "Leave the garden" sequence + field card: each panel mapped to rtl_433, dump1090-fa, SDR++/GQRX (GQRX ships in SpecFive's optional software pack, ports survey line 79, if the recipient's Recon has it), satdump, multimon-ng, meshtastic CLI and qFlipper |
| U8 | S2 | M | M5: the plan's 12-card sort + "show me the law" with cites (§705, 2511(2)(g), 2510(16), Joffe, 15.121, Part 97) |
| U9 | S3 | L | Missing topics: line of sight in M13; Doppler and pass prediction in M24/M25; antenna types, coax, LNA/bias-tee; squelch; SSB/CW/HF via V3 direct sampling; AFSK/GMSK/MSK/PPM one-liners; DF (a fox hunt with Flipper RSSI); band plan; logging and report tradecraft |
| U10 | S3 | M-L | Instrument gaps: Meteor LRPT in M24's panel (satdump); a tunable dsd-fme with a voice/encrypted flag; an audio path (rtl_fm → ALSA) for M8/M27; mesh send/config for C8 |

### 8c. Plan items swapped out or dropped with no record

STATUS, learnings and CLAUDE.md have zero mentions of any of these. The 10-29 roadmap records the
new tracks but not the drops. Each needs a disposition logged in MC's words.
- **Plan U5 lessons that were replaced:**
  - M14 ISM sensor zoo → replaced by APRS; partly absorbed by M10.
  - M15 WiFi/BLE survey with Joffe → replaced by POCSAG. The amendment had **explicitly moved
    POCSAG to a recorded walkthrough with the text blurred, for §705/2510(16) reasons** (amendment
    §1.3 R4, §5.1). The built M15 shows live pager text.
  - M16 GPS → replaced by ACARS.
- **Plan U6:**
  - M17's hands-on IQ (zero the Q, drop the sample rate) → built as no-RF text.
  - M18 Signal-ID + SigMF → dropped.
- **Recorded walkthroughs R1-R6 → none.**
  - R1 Meteor LRPT → built as live NOAA APT, which the amendment said "do not teach as live".
  - R2/R3/R4 became live M27/M26/M15.
- **Other dropped items:** retrieval cards, challenge tiers A/B/C, badges, the "first 15 minutes"
  band map, ppm auto-correct + chip, the M5 card sort and "show me the law".

### 8d. Copy rewrites for the S0/S1 copy fixes (before → after; bodies kept short)

Where R3 (real tune and gain) lands, the M1/M2/M6/M7 originals mostly stand. Only the parts marked
"always" are still needed.

**M1 try, 01:28-31 (copy-only path)**
- Before: "From the stations I just scanned, tap a different strong one — any other bright stripe
  on the list. Watch the screen jump to it and a fresh stripe bloom where that station lives.
  That's it — you just tuned a radio."
- After: "Your turn, comrade. The whole FM band, 88 to 108, is on screen at once. Find a different
  bright stripe, put your finger on it, and read its frequency off the axis below. That stripe is a
  station, arriving now."

**M1 predict/observe, 01:33, 01:37**
- "two notches up the dial" → "the next bright stripe to the right".

**M1 explain, 01:42-43 (always)**
- Add after "the same width": "A few look twice as wide: their HD Radio digital sidebands sit
  either side of the analog block."

**M2 demo, 02:20-24 (copy-only)**
- After: "Decibels first: a dB is a ratio, and every 10 dB is ten times the power. The grass along
  the bottom is the noise floor; the station pokes up above it. The meter reads both. Peak minus
  floor is the SNR, the one number worth chasing."

**M2 try, 02:26-28 (copy-only)**
- After: "Read the floor, the peak and the gap off the meter, and note them. This radio picks its
  own gain for now, so reason the next step instead of turning a knob."

**M2 explain, 02:41 (always)**
- "because the radio starts adding hiss of its own" → "because strong signals start to overload
  the receiver and smear false copies across the band".

**M3 demo, 03:24-25**
- "the strong local FM station from Lesson 1" → "a reference station the console watches, set for
  your area".

**M3 observe, 03:40-42**
- After: "Imagine this radio goes deaf tonight. From the waterfall alone, could you tell? Which
  number on the provenance panel would tell you?"

**M4 demo, 04:21-28**
- After: "Watch — hands off. Every stereo FM station hides a steady 19 kHz tone, the pilot, inside
  its audio, held to within 2 Hz by FCC rule. Tap Measure ppm on a strong station and the console
  times that tone against this radio's clock. It reads about +164 ppm fast: 164 Hz of error for
  every MHz you tune."

**M4 try, 04:30-34**
- After: "Your turn. Step the dial to your strongest station with + and −, then tap Measure ppm.
  Expect about +164. At 100 MHz that is about 16 kHz high, nothing inside a 200 kHz FM channel,
  which is why you never noticed."

**M4 explain, 04:48-52**
- "absolutely fatal in a 12.5 kHz one: at 1090 MHz you'd tune to the published frequency and the
  planes simply wouldn't be there" → "harmless for ADS-B, whose pulses fill about 2 MHz, but at 162
  MHz it is 27 kHz, more than a whole AIS channel".

**M4 explain, 04:55-58**
- After: "Carry two things out: this radio runs about +164 ppm, and the cure is to tell each tool
  its error. `rtl_fm -p 164`, `rtl_433 -p 164`, `dump1090 --ppm 164`. This console doesn't apply it
  yet; the PPM RAW chip says so. The kit's V3, with its 1 ppm clock, would read near zero."
  (Add "`rtl_test -p`" if MC wants the clock method back.)

**M5 demo, 05:24-26**
- "you may not pass on or act on certain private communications you happen to catch, such as
  cellular" → "you may not intercept phone calls or pager messages carried by a phone or paging
  company at all, and you may not pass on a private message you happen to catch".

**M6 explain, 06:58 (always)**
- "a quarter-wave in centimetres is 300 over the frequency in MHz, then quartered" → "a quarter-wave
  in metres is 300 over the frequency in MHz, then quartered".
- Soften 06:55 "too long is every bit as deaf as too short" → "the wrong length hears poorly, too
  long or too short".

**M6 demo and try, 06:21-36 (copy-only)**
- Keep the 300/f arithmetic.
- Replace "tune up to 1090" with: "This console can't show 1090 yet, so feel resonance on FM
  instead: collapse both dipole legs, then pull them out toward 75 cm (300 ÷ 100 ÷ 4) and watch the
  FM stripes brighten."

**M7 try, 07:33-37 (copy-only)**
- After: "Slide the FM-notch filter into the cable and watch the waterfall: the FM band should go
  dark. That is its one job. At 1090 that same job would have rescued the planes."

**M8 demo and observe, carrier (always)**
- 08:50-51 → "Here's the straight answer: neither, while a transmission lasts."
- Add: "One catch: airband is push-to-talk. When the speaker lets go, the whole transmission
  ends, carrier and all, and the channel goes dark until the next key-up."
- 08:46-47 → "Watch one transmission from key-up to release. Between words, does the line stay
  lit? And at release?"

**M8, audio (always)**
- Drop "and by sound" (08:10-11), "leave the volume down" (08:34) and "when you do finally listen…"
  (08:63-64).
- Add: "This console has no speaker path yet; the shapes are the lesson."

**M8 try, 08:34-35 (copy-only)**
- "Tap between the two presets" → "Find the wide FM blocks on the waterfall; an airband channel
  would be a single thin pixel at this resolution, blinking on only while someone talks."

**M9 demo, 09:27**
- "the little ISM band where keyfobs and sensors chatter" → "where cheap sensors and some remotes
  chatter (in the US most car and garage remotes sit at 315 or 390 MHz)".

**M9 try, 09:36-37**
- After: "Key a 433 MHz remote you own, held a metre from the antenna, or watch for a neighbour's
  sensor. On the Flipper, Sub-GHz → Frequency Analyzer finds a remote's frequency first."

**M11 try, 11:35-36**
- After: "If a second Meshtastic node is in reach, send from it, at least 50 cm from the Nomad, and
  read the SNR on the line that lands. No second node? An empty list is the honest answer."

**M12 concept, 12:9-10**
- "so a waterfall built from amplitude and frequency can never show it" → "so a waterfall shows
  the signal's energy but never its bits".

**M12 explain, 12:63**
- "the Wi-Fi and satellite links waiting ahead, hides there on purpose" → "Wi-Fi to satellite
  downlinks, shows only a hump of energy, never the message".

**M15 concept/frame, 15:9-10, 15:25-26**
- "POCSAG, the protocol most of them use" → "POCSAG, or in the US often its cousin FLEX".
- If FLEX stays, add `-a FLEX` to the decoder (`pocsag.py:28`).

**M15 explain, 15:55-58 (always)**
- Before: "Receiving radio is passive and legal almost everywhere; acting on, recording, or repeating
  a message that was not meant for you very often is not…"
- After: "In the US, pages carried by a paging company don't count as 'readily accessible to the
  public' under the Wiretap Act, so even intercepting the words can be a federal offence. Only
  tone-only pages are exempt. Study the cap code and the timing; the words are not yours."
- Optional, MC's call (D16): show message text behind a tap rather than by default.

**M16, 16:13-14, 16:25, 16:68**
- "This closes the decode unit" → delete.
- "One last signal" → "Another signal".
- "Unit complete." → "Two more in this unit: ships, then digital voice."

**M17 demo, 17:25-27 and M18 concept, 18:10-11**
- "To phone home it needs a radio, and that radio chatters in the clear: a WiFi fingerprint" → "It
  sends plates home over cellular, but it also carries a WiFi radio that chatters in the clear: a
  fingerprint you can recognise".

**M19 frame, 19:21-23**
- "that conversation never touches the open air you can sweep" → "that conversation is on the
  air, but packed inside an LTE carrier wider than your dongle and addressed to one phone; only a
  modem in the conversation can read it".

**M20 frame, 20:16**
- "Every lesson so far you worked the radio from outside." → "So far you mostly worked the radio
  from outside."

**M22 explain, 22:37**
- "— the waterfall's width." → "— one dongle's view. (The console's 20 MHz waterfall stitches
  many such slices.)"

**M23 frame, 23:16**
- "The last lesson, comrade:" → "Last of this unit, comrade:"

**M24 frame, 24:30-31**
- "A wire antenna and your dongle can catch it" → "A wire antenna and your dongle could catch it
  until 2025".

**M25 concept, 25:9 and frame, 25:25**
- "Every decode so far turned a signal into words … SSTV turns one into a picture" → "APT painted
  a picture in loudness; SSTV paints one in pitch".

**M28, 28:42 and 28:66**
- "MC is bringing one" → "add one when you can".
- "When MC's second node arrives" → "When a second node is in range".

**M8 demo, 08:22-24**
- "on a controller's channel: a single hard, bright line, dead still at center" → "on an ATIS
  channel, a recording that never stops transmitting: a single hard, bright line, dead still at
  center".

**M10 try, 10:38-40 (add)**
- "If every read says ASK, that's normal here: most US 433 MHz sensors are on-off keyed, and the
  FSK ones mostly live up at 915."

**M11 demo, 11:32-33**
- "A plain carrier at ten dB below the noise is gone. A LoRa chirp at ten dB below the noise is a
  packet." → "A plain data signal ten dB under the noise is gone; a LoRa chirp there is a packet."

**M12 demo, 12:32-33**
- "the station's call sign — the PS name —" → "the station's name — the PS, eight characters —".

**M19 demo, 19:30-33**
- "the moment a heuristic fires, that line turns red" → "when a heuristic fires the line turns
  yellow, orange or solid red as the warning grows".

**M24 explain, 24:72-73**
- "Russia's Meteor-M2 series" → "Russia's Meteor-M N2-3 and N2-4, near 137.9 MHz".

**M28 explain, 28:62-63**
- "a node on a channel you are not on still carries your message onward" → "a node with your
  radio settings carries your message onward even on a channel it cannot read".

**Leaky labels (R8)**

| Lesson | Before | After |
|---|---|---|
| M5 | "Legal — it's just one call" / "Illegal — transmitting needs a licence" / "Legal if you give your name first" | "Legal" / "Illegal" / "Legal if you identify" |
| M17 | "Yes — a clean hour means none" / "No — silence proves nothing" / "Mostly — it's strong evidence" | "Yes" / "No" / "Mostly" |
| M19 | "Yes — red proves a catcher" / "No — red means look closer" / "Nearly — it's near-proof" | "Yes" / "No" / "Nearly" |
| M21 | "Yes — its mirror below the LO" / "No — only 100.1 lands there" / "Yes — a harmonic higher up" | "Yes, 99.9" / "No, only 100.1" / "Yes, 200.1" |
| M22 | "1.2 MHz — half the rate" / "2.4 MHz — the full rate" / "Only 100 MHz itself" | "1.2 MHz" / "2.4 MHz" / "4.8 MHz" |
| M27 | "Yes — always clear speech" / "IDs always, voice sometimes" / "No — nothing decodes" | "All of them" / "Some of them" / "None of them" |
| M29 | "More radios / range" / "A bench: screen + storage" / "Nothing, just charging" | "More range" / "Screen + storage" / "Faster radios" |
| M13 (question) | "must you break a code, or just decode an open standard?" | "To put it on your scope, what must you have?" (chips: "Nothing extra" / "A key" / "A licence") |
| M4 (question) | "that error is per MHz, so it scales with frequency. Up at 1090 MHz…" | "Up at 1090 MHz…" (cut the hint) |

---

## Sources read

- Repo: `lesson_packs/*.yaml`, `sigint101/{units,lessons,app,spectrum,provenance,ppm,rds,dmr,exam,progress}.py`,
  `templates/{console,base,home,_tilegrid}.html`, `static/{waterfall,ppm,decode,dmr,adsb}.js`,
  `STATUS.md`.
- Branches: `origin/flipper-module` (`lesson_packs/29-meet-flipper.yaml`, the design spec) and
  `origin/ux-polish` (diff).
- Plans:
  - `eager-spinning-frost.md` (Curriculum, Assessment)
  - the educator amendment `…-agent-adcde0e12f27f43c3.md` (§0-§8)
  - the ports survey `…-agent-a7b69cfb5fa8bac25.md`
  - `abstract-soaring-bunny.md` (Course architecture)
- Word counts were computed from the YAML. The §5 fact-check used the primary sources cited in
  that section.
