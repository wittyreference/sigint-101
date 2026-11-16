# Blind expert pass: L1 to L29

> **Coordinator's note (2026-11-03).** This is the blind half of the 11-03 item-quality review. It
> pairs with `2026-11-03-item-review.md`, which holds the method, the per-item rubric and the
> rewrites.
>
> - **How it was run.** A subagent was given only the screens each learner saw before the question,
>   plus the choices. It had no keys, no verdicts and no lesson packs.
> - **One assumption is wrong.** Where it says "RTL-SDR Blog V4", it assumed the R828D dongle is a
>   V4. The Nomad's built-in radio is not:
>   - nomad-b measured about +164 ppm, not a 1 ppm crystal;
>   - nothing shows the V4's built-in FM filtering.
>
>   So its L4 point ("learners read 0 to 2 ppm") and its V4-specific L7 reasoning don't apply. L7 is
>   flagged BROKEN in the item review for a separate reason.

I answered from `items-blind.md` only. I did not open the keys or the lesson packs. The hardware assumed is an RTL-SDR with an R828D tuner on a Pi, in the US, with an rtl_power waterfall that draws one pixel per bin.

---

## L1 See the Invisible: next stripe wider, narrower or same?
1. **Answer:** `same`. Every US FM channel is a 200 kHz allocation, and analog FM occupancy is similar from station to station.
2. **Confidence:** medium for the textbook answer, low for what the panel will actually show.
3. **One correct answer?** No.
   - `wider` is defensible. Many US FM stations run hybrid HD Radio, whose digital sidebands sit about 129 to 198 kHz either side of the carrier. Such a station paints roughly 400 kHz wide, as a bright core with two dimmer flanking bands. A stronger station also looks wider on a dB colour scale, because more of its spectral skirt clears the floor.
   - `narrower` is defensible. A weaker station shows less skirt above the floor, so it looks thinner.
4. **Clarity:** issue. "Width" could mean the allocated channel, the occupied bandwidth or the displayed stripe, and these diverge on a real panel.
5. **Reachability:** partly. The demo gives "about 200 kHz wide" for one station but never says the width is a fixed allocation that every station shares.
6. **Panel:** at about 25 kHz per pixel for a 20 MHz sweep, an analog station is about 8 px wide and an HD station is about 16 px with dimmer edges. Width also tracks signal strength. In a US metro, a neighbour that looks a different width is common. The key risks marking a learner wrong for reporting what they actually see.

## L2 Turn It Up: SNR gap from middle to maximum gain
1. **Answer:** `narrower` for a strong local FM station on the stock whip. Near the top of the gain range the 8-bit ADC clips, intermod and spurs lift the floor, and the peak stops rising. `same` is close behind.
2. **Confidence:** medium.
3. **One correct answer?** No.
   - `same` is defensible for a weaker station. Once external noise dominates, the floor and the peak rise dB for dB.
   - `wider` is defensible in a quiet RF environment or on a weak signal. At mid gain the R828D's own noise or the ADC noise can still set the floor, and more LNA gain improves the noise figure.
4. **Clarity:** issue. The question does not say whether the station is strong or weak, and that is what decides the answer. It also leaves open whether the meter's "floor" is a median of the bins, which overload products move.
5. **Reachability:** partly. A novice who extrapolates their own low-to-mid trend will usually say "wider". The missing facts are that the floor rises with gain and that overload or clipping exists. The frame's "gain is not that number" points towards same or narrower.
6. **Panel:** strongly location-dependent. In urban US FM at 49.6 dB gain, expect a raised floor across the sweep plus spurs or ghost stations, so the gap narrows. In rural areas it is roughly flat.

## L3 Know Your Instrument: a slowly deafening receiver against a quiet band
1. **Answer:** `identical` to the eye. `little` is also defensible if "picture" includes the meter numbers.
2. **Confidence:** medium.
3. **One correct answer?** No, `identical` and `little` are both defensible. A lossy connector or a weak LNA lowers the displayed noise floor by a few dB, because less antenna noise is amplified, and lowers every signal. On an auto-scaled colour map, an empty band looks the same either way. On a fixed scale or the meter it is a few dB darker. `obvious` is defensible only for a fully dead LNA viewed on a fixed scale.
4. **Clarity:** issue.
   - "Next to a band merely quiet tonight, would its picture look…" is hard to parse. It is unclear what is compared with what: the same band through a healthy radio versus a deaf one, or a deaf radio versus a genuinely empty band.
   - "Picture" could mean the colour image or the numbers.
5. **Reachability:** partly. The frame's "fails quietly" points the way.
6. **Panel:** if the waterfall auto-scales its colours, the deaf state is visually indistinguishable. That is exactly why the baseline's reference-station level is the real tell.

## L4 The Number on the Dial: ppm error at 1090 MHz
1. **Answer:** `bigger`. 164 ppm × 1090 MHz ≈ 179 kHz, against 16 kHz at 100 MHz.
2. **Confidence:** high.
3. **One correct answer?** Yes.
4. **Clarity:** ok.
5. **Reachability:** yes. "164 Hz of error for every MHz you tune" was given.
6. **Panel, a fact-check on the screens:**
   - **The SDR's own clock.** The common R828D product is the RTL-SDR Blog V4, which has a 1 ppm TCXO. On that dongle a learner should read about 0 to 2 ppm, not "the low hundreds". A +164 ppm reading would then point to non-TCXO hardware or to a fault in the measurement.
   - **The pilot as a reference.** The FCC tolerance of ±2 Hz on 19 kHz (47 CFR 73.322(a)(2)) works out to ±105 ppm. By rule alone, the pilot cannot certify a clock to better than about ±105 ppm. Real exciters are usually much tighter, but "held to within 2 Hz" oversells the pilot as a precision reference.

## L5 The One Rule: transmitting on a ham repeater with no licence
1. **Answer:** `illegal`.
2. **Confidence:** high.
3. **One correct answer?** Yes. Identifying does not substitute for a licence (47 CFR 97.5). The only edge cases are an unlicensed third party speaking under a licensed control operator, and the 97.403 emergency exception. Neither fits "yourself, no licence".
4. **Clarity:** ok. Minor: the spelling is "licence" in a US course, where the usual spelling is "license".
5. **Reachability:** yes, and it is a giveaway. The demo and the try screen state it almost verbatim.
6. **Panel:** n/a.

## L6 Antennas Are Tuning Forks: leg length for 915 MHz
1. **Answer:** `longer`. λ/4 ≈ 8.2 cm at 915 MHz against 6.9 cm at 1090 MHz, or about 7.8 cm and 6.5 cm after end-effect shortening.
2. **Confidence:** high.
3. **One correct answer?** Yes.
4. **Clarity:** ok.
5. **Reachability:** yes. The formula was given and the question says "lower than 1090".
6. **Panel:** n/a for the question. A side note on the try screen: in practice the stock whip often still decodes ADS-B near an airport, so "nearly deaf" is overstated. The point about a faint hump rather than a stripe is right.

## L7 Build, Aim & Clean Up: does the FM notch help at 915?
1. **Answer:** `depends`. The notch helps at 915 only if strong FM is overloading the broadband front end, which "blinds the whole radio". If 915 is weak for any other reason (distance, antenna, gadget power), the notch does nothing and adds about 0.5 to 1 dB of loss.
2. **Confidence:** medium.
3. **One correct answer?** No, `no` is defensible on this hardware. The RTL-SDR Blog V4 already splits HF, VHF and UHF through a triplexer into the R828D's three inputs, and adds small FM/AM/DAB notches. FM is already filtered off the UHF path, so an external FM notch rarely changes anything at 915.
4. **Clarity:** issue. "Help" is ambiguous: improve SNR, or make a weak signal stronger? The question also does not say why the signal looks weak.
5. **Reachability:** partly. "Blind the whole radio" in the demo is the needed hint, but a novice will likely reason "notch only touches 88 to 108, so no".
6. **Panel:** on a V4-class dongle, expect no visible change at 915.

## L8 Two Shapes: does a carrier vanish between words?
1. **Answer:** `neither`. Airband is full-carrier DSB AM: while the push-to-talk is held, the carrier stays up through every pause, and only the sidebands come and go. The broadcast FM carrier is always on.
2. **Confidence:** high.
3. **One correct answer?** Yes, if "between words" means within one transmission. If it means between transmissions, the AM carrier does drop when the push-to-talk is released, and `am` becomes defensible.
4. **Clarity:** issue.
   - "Between words" versus "between transmissions" is ambiguous.
   - The try screen says an airband channel is "lit only while someone talks", which primes `am` and contradicts the intended answer.
5. **Reachability:** partly. The demo's "single hard bright line, dead still… carrier" with sidebands "blooming" supports `neither`, but the try screen undercuts it.
6. **Panel:** at rtl_power resolution the carrier and sidebands share one column, and integration of about 1 s per line will not resolve gaps between words. What the learner actually sees is the column switching on and off with push-to-talk, which looks like "AM vanishes". The panel cannot demonstrate the key's point.

## L9 The Carrier That Vanishes: what sits there between presses?
1. **Answer:** `floor`.
2. **Confidence:** high.
3. **One correct answer?** Yes. Between presses nothing is transmitting. Even inside a press, OOK "off" bits sit about 40 dB or more down on typical SAW-resonator transmitters.
4. **Clarity:** ok.
5. **Reachability:** yes, and it is a giveaway: the demo says "flat to the floor" and "no carrier left behind at all".
6. **Panel:** rtl_power lines integrate over far longer than an OOK pulse (milliseconds). The waterfall shows a column lit for the whole burst and then gone, not the individual pulses the demo describes. The Flipper's Read RAW is the only view that shows the pulses.

## L10 The Carrier That Shifts: how FSK carries a bit
1. **Answer:** `shift`.
2. **Confidence:** high.
3. **One correct answer?** Yes.
4. **Clarity:** ok.
5. **Reachability:** yes, and it is a giveaway, from the lesson title, the frame and the demo.
6. **Panel:** rtl_433 with `-M level` reports `mod: FSK` together with `freq1` and `freq2`. Most US 433.92 sensors will read ASK, as the screen says.

## L11 Below the Noise Floor: a LoRa packet at −12 dB SNR
1. **Answer:** `decodes`. Meshtastic LongFast is SF11 with 250 kHz bandwidth, and Semtech's demodulation floor for SF11 is about −17.5 dB.
2. **Confidence:** high on LongFast.
3. **One correct answer?** Yes on LongFast. `fails` is defensible at lower spreading factors: SF7 (−7.5 dB floor), SF8 (−10 dB) and SF9 (−12.5 dB, marginal). The faster presets and the MediumFast preset many meshes have moved to use these. `garbles` is never right, because LoRa has a CRC and Meshtastic drops bad packets rather than delivering garbled text.
4. **Clarity:** minor issue. The question does not state the preset or spreading factor, which decides the answer. The demo used "ten dB under" and the question says twelve.
5. **Reachability:** yes.
6. **Panel:** reported SNR values below about −15 dB are rare and noisy estimates. Most packets will land between −5 and +10 dB.

## L12 The Hidden Text: does RDS show on the waterfall?
1. **Answer:** `hidden`. There is nothing distinguishable on an RF waterfall.
2. **Confidence:** medium.
3. **One correct answer?** No, `faint` is defensible.
   - On a waterfall of the demodulated FM baseband (the multiplex, or MPX), RDS shows as a faint pair of lobes at 57 kHz ± about 2.4 kHz.
   - On the RF waterfall, the RDS energy is a few percent of the FM deviation. It is spread inside the station's ±100 kHz block and is indistinguishable.
4. **Clarity:** issue, and the premise is physically wrong.
   - "RDS hides its data in the carrier's phase — not its height or frequency" is incorrect. The data is BPSK on a 57 kHz subcarrier's phase, and that subcarrier is then frequency-modulated onto the main carrier. At RF, RDS lives in the carrier's frequency.
   - The question also implies that phase modulation is invisible on a power waterfall. That is false: PSK signals such as PSK31 or satellite BPSK paint clearly. RDS is invisible because it is small and buried inside the FM, not because it uses phase.
   - "The waterfall" does not say whether it means RF or baseband.
   - Nit: "phase flips nearly twelve hundred times a second". The bit rate is 1187.5 bps, but biphase coding produces up to about 2375 phase reversals per second.
5. **Reachability:** partly. The right reason (a quiet subcarrier inside the FM) is hinted at in the frame, but the question pushes the wrong reason.
6. **Panel:** the RF waterfall shows the station's normal stripe. Any HD sidebands are far more visible than RDS ever will be.

## L13 The Sky Talks: what you need to put an airliner on the scope
1. **Answer:** `nothing`.
2. **Confidence:** high.
3. **One correct answer?** Yes. ADS-B is unencrypted and receiving it needs no licence in the US. Strictly, a range-and-bearing scope also needs the receiver's own location configured, but that is not a key or a licence.
4. **Clarity:** ok.
5. **Reachability:** yes, and it is a giveaway: "an open broadcast".
6. **Panel:** this depends on the 1090 antenna. Earlier lessons say the stock setup may be deaf, so an empty scope is likely.

## L14 The Return Address: what is inside an APRS burst?
1. **Answer:** `framed`.
2. **Confidence:** high.
3. **One correct answer?** Yes. It is an AX.25 frame with addresses, path, info field and a frame check sequence. `scrambled` is wrong because Part 97.113 bars obscuring meaning.
4. **Clarity:** ok.
5. **Reachability:** yes, and it is a giveaway: the frame and demo describe the frame contents.
6. **Panel:** n/a.

## L15 Not Meant for You: what stands between you and the pager text?
1. **Answer:** `law`.
2. **Confidence:** medium-high.
3. **One correct answer?** Mostly. POCSAG has no native encryption, and 929 MHz paging transmitters are high-power simulcast, so `signal` is unlikely. `encryption` applies only to the rare network that layers proprietary encryption on top. Under ECPA (18 USC 2510(16) and 2511), display paging over a carrier is not "readily accessible", and 47 USC 605 bars divulging it.
4. **Clarity:** minor issue. Literally, what stands between the learner and the words is the console's own withholding code. That code is motivated by the law, but the learner may answer "the panel".
5. **Reachability:** yes. L5's demo said pager messages carried by a paging company may not be intercepted.
6. **Panel:** shows "withheld" by design. Many US carriers use FLEX, so the log may be empty.

## L16 What the Aircraft Says: what is inside an ACARS burst?
1. **Answer:** `text`.
2. **Confidence:** medium.
3. **One correct answer?** No.
   - `telemetry` is partly defensible. A large share of ACARS is machine-formatted data, terse codes or empty handshakes. OOOI times, position reports and engine reports read as numbers.
   - `encrypted` holds for a minority: military traffic, some airlines' proprietary encoding, and ARINC 823 secure ACARS.
4. **Clarity:** issue. Any single burst could be any of the three. The question asks for one label for all traffic, and the try screen itself says "some rows will be terse codes, some readable text".
5. **Reachability:** yes.
6. **Panel:** expect a mix with many empty or numeric rows. In many US areas, VDL Mode 2 now carries much of the traffic, so 131.550 may be sparse.

## L17 Who's Watching: an hour of detector silence
1. **Answer:** No (`proves-nothing`).
2. **Confidence:** high.
3. **One correct answer?** Yes for "prove". The key's slug overstates it: silence is weak evidence, not zero evidence. Many ALPR brands emit no WiFi at all, and Flock units are reported to randomise MACs and to probe on 5 GHz, which 2.4 GHz-only detectors miss.
4. **Clarity:** ok.
5. **Reachability:** yes.
6. **Panel:** n/a.

## L18 The Camera's Fingerprint: OUI match, IE fingerprint mismatch
1. **Answer:** `weak-hint`.
2. **Confidence:** medium-high.
3. **One correct answer?** Mostly. `strong-hit` would be defensible only if the OUI were registered exclusively to Flock and the mismatch came from a firmware change to the camera's IE order. Community detector projects report that the "Flock OUI" lists are mostly chip and module vendor prefixes, which produce false positives such as a T-Mobile hotspot.
4. **Clarity:** ok.
5. **Reachability:** partly. The try screen's "did the whole fingerprint match or only the prefix?" hints at the answer. The missing fact is that OUIs are often shared by WiFi chip vendors across many products.
6. **Panel:** n/a.

## L19 Catch the Catcher: does Rayhunter's red prove a catcher?
1. **Answer:** No (`look-closer`).
2. **Confidence:** high.
3. **One correct answer?** Yes for "prove". EFF's own heuristics docs list false positives, such as aircraft landing and null ciphers, and note that the 2G signature misfires where 2G is still live. A US 2G downgrade is strong evidence, but it is not proof.
4. **Clarity:** ok.
5. **Reachability:** yes.
6. **Panel:** n/a.

## L20 The First Inch: which amplifier goes first?
1. **Answer:** `quiet-first`. This is the Friis cascade formula.
2. **Confidence:** high.
3. **One correct answer?** Yes.
4. **Clarity:** ok.
5. **Reachability:** yes, and it is a giveaway: the demo states that the first stage's noise figure sets the receiver's sensitivity.
6. **Panel:** n/a. Theory only.

## L21 Slide It Down: does a second frequency land on 0.1 MHz?
1. **Answer:** `image`, 99.9 MHz.
2. **Confidence:** high.
3. **One correct answer?** Mostly. `harmonic` (200.1) is physically real in a mixer whose LO has harmonics: 2 × 100.0 = 200.0, and 200.1 − 200.0 = 0.1. Balanced mixers suppress even harmonics, and the RTL-SDR's known spurious responses are mainly third-harmonic. The image is the textbook first-order answer.
4. **Clarity:** ok.
5. **Reachability:** yes. "Keeps only the difference, which carries no sign" nearly states it.
6. **Panel:** the real R828D is a low-IF tuner with an image-reject mixer, followed by complex I/Q in the RTL2832U. A station at 99.9 does not pile onto 100.1 at full strength. Only faint images of very strong signals leak through. L22 teaches exactly this, so it is fine pedagogically.

## L22 Two Streams: bandwidth at 2.4 MSPS of I/Q
1. **Answer:** `full`, 2.4 MHz (±1.2 MHz around 100).
2. **Confidence:** high.
3. **One correct answer?** Yes. `half` is the classic real-sampling misconception.
4. **Clarity:** ok.
5. **Reachability:** yes. The try screen says each complex pair separates above from below.
6. **Panel:** usable width is about 2.0 to 2.2 MHz after filter roll-off, and there is a DC spike at the centre. The course's waterfall is a 20 MHz hopped sweep, so it is wider than one slice.

## L23 What Every Pixel Is: a strong signal just past the window edge
1. **Answer:** `aliases`. With complex sampling, a tone at +fs/2 + δ folds to −fs/2 + δ, a ghost near the opposite edge.
2. **Confidence:** high.
3. **One correct answer?** Yes, given "unfiltered".
4. **Clarity:** issue.
   - "Top edge" is ambiguous. L1 taught that top-to-bottom is time on the waterfall, but here "top" means the upper frequency edge, which is the right-hand side on the waterfall.
   - "Unfiltered" alone carries the answer.
5. **Reachability:** partly. Nyquist was named but folding was never explained. The try screen's "Does that mean it is not in your picture?" steers the learner.
6. **Panel:** the RTL2832U's decimation filter and the R828D's IF filter attenuate signals just outside the band but not completely. With rtl_power hops and no crop, strong FM stations leave faint ghost copies at hop boundaries. This is real and teachable.

## L24 A Picture From Space: a weak, noisy APT pass
1. **Answer:** `grainy`.
2. **Confidence:** high.
3. **One correct answer?** Yes. `none` holds only well below the FM threshold, where the output becomes pure noise and sync is lost.
4. **Clarity:** ok. "Buried in hiss" slightly invites `none`.
5. **Reachability:** yes. The try screen sets up the analog-versus-checksum contrast.
6. **Panel:** no live APT exists. NOAA-18 was retired in June 2025, and NOAA-19 and NOAA-15 on 13 and 19 August 2025. Any grainy image must come from a recording labelled "replay". The only live 137 MHz imagery is Meteor-M LRPT, which is digital (QPSK with FEC). It degrades block by block, which is closer to the `all-or-nothing` choice.

## L25 A Picture in the Air: what a minute of SSTV decodes to
1. **Answer:** `photo`.
2. **Confidence:** high.
3. **One correct answer?** Yes.
4. **Clarity:** ok.
5. **Reachability:** yes, and it is a giveaway: the demo describes a photograph painting.
6. **Panel:** the claim of 437.550 since 2026 checks out: ARISS SSTV Series 31 and 32 in April and May 2026 were on 437.550, Robot 36. Doppler at 70 cm is about ±10 kHz, so rtl_fm needs a wide enough filter or Doppler tracking. A weak pass gives a noisy but real image.

## L26 The Sea Talks: what joins a ship's position and name
1. **Answer:** `mmsi`.
2. **Confidence:** high.
3. **One correct answer?** Yes. Message types 1 to 3 (position) and type 5 (static data) both carry the MMSI.
4. **Clarity:** ok.
5. **Reachability:** yes. The try screen names the MMSI and the demo says "files both under the same vessel".
6. **Panel:** inland, the roster is empty.

## L27 The Coded Voice: how many DMR calls give clear speech?
1. **Answer:** `ids-voice-sometimes`.
2. **Confidence:** medium.
3. **One correct answer?** No.
   - `always-voice` is defensible on an amateur DMR repeater. Part 97.113(a)(4) bars encryption, so every voice call is decodable, though some "calls" are data.
   - `nothing` is literally true on this console, which has no speaker. It is also true on a fully encrypted business or public-safety system.
   - Business DMR is a mix of clear, Basic Privacy and AES.
4. **Clarity:** issue. "Give you clear speech" collides with the try screen's "this console has no speaker yet". The question also does not say whether the channel is ham or business.
5. **Reachability:** partly. Encryption of the voice payload was never mentioned. The demo's "signaling, in the clear" is only an implicit hint that the voice might not be.
6. **Panel:** IDs only, no audio.

## L28 A Message That Hops: does a text reach a node beyond range?
1. **Answer:** `relayed`.
2. **Confidence:** high.
3. **One correct answer?** Yes, with caveats. `dies` is possible if the middle node is CLIENT_MUTE, the hop limit is 0, or the middle node is on a different preset or frequency slot. The defaults (hop limit 3, CLIENT rebroadcasts) relay.
4. **Clarity:** ok.
5. **Reachability:** yes. The frame says "hand short… messages down a chain".
6. **Panel:** with a mesh of one, nothing shows.

## L29 Meet the Flipper: what the Flipper gains from the console
1. **Answer:** `a-bench`.
2. **Confidence:** high, by elimination. USB changes nothing about the radio's range or speed.
3. **One correct answer?** Yes.
4. **Clarity:** issue. The label "Screen + storage" is odd because the Flipper already has a screen and an SD card. The real gain is a large touchscreen, persistent logging, power, and correlation with the SDR in one console.
5. **Reachability:** partly. The demo never says what the console adds, so the learner can reach the answer only by eliminating the other two.
6. **Panel:** n/a.

---

## Summary

| item | your answer | confidence | single-answer | clarity | reachable |
|---|---|---|---|---|---|
| L1 | same | medium (low on the real panel) | n | issue | partly |
| L2 | narrower | medium | n | issue | partly |
| L3 | identical | medium | n | issue | partly |
| L4 | bigger | high | y | ok | y |
| L5 | illegal | high | y | ok | y |
| L6 | longer | high | y | ok | y |
| L7 | depends | medium | n | issue | partly |
| L8 | neither | high | y | issue | partly |
| L9 | floor | high | y | ok | y |
| L10 | shift | high | y | ok | y |
| L11 | decodes | high | y | issue (minor) | y |
| L12 | hidden | medium | n | issue | partly |
| L13 | nothing | high | y | ok | y |
| L14 | framed | high | y | ok | y |
| L15 | law | medium-high | y | issue (minor) | y |
| L16 | text | medium | n | issue | y |
| L17 | proves-nothing (No) | high | y | ok | y |
| L18 | weak-hint | medium-high | y | ok | partly |
| L19 | look-closer (No) | high | y | ok | y |
| L20 | quiet-first | high | y | ok | y |
| L21 | image | high | y | ok | y |
| L22 | full | high | y | ok | y |
| L23 | aliases | high | y | issue | partly |
| L24 | grainy | high | y | ok | y |
| L25 | photo | high | y | ok | y |
| L26 | mmsi | high | y | ok | y |
| L27 | ids-voice-sometimes | medium | n | issue | partly |
| L28 | relayed | high | y | ok | y |
| L29 | a-bench | high | y | issue | partly |

Giveaways, where the demo or try screen states the answer outright: L5, L9, L10, L13, L14, L20, L25.

Screen fact-checks that do not depend on the key: L4 (1 ppm TCXO on the V4, ±2 Hz pilot ≈ ±105 ppm), L12 (wrong physics in the premise), L24 (no live APT since August 2025).

## Sources
- HD Radio hybrid sidebands at about 129 to 198 kHz: [k6sti HD Radio self-noise](https://k6sti.neocities.org/iboc.htm), [US patent 7542517](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/7542517)
- RTL-SDR Blog V4 (R828D, 1 ppm TCXO, triplexer, AM/FM/DAB notches): [V4 datasheet](https://www.rtl-sdr.com/wp-content/uploads/2024/12/RTLSDR_V4_Datasheet_V_1_0.pdf), [rtl-sdr.com V4 product](https://www.rtl-sdr.com/product/rtl-sdr-blog-v4-r828d-rtl2832u-1ppm-tcxo-sma-software-defined-radio-dongle-only/)
- Pilot tolerance of 19 kHz ±2 Hz: [47 CFR 73.322 (Cornell LII)](https://www.law.cornell.edu/cfr/text/47/73.322)
- Meshtastic LongFast (SF11, 250 kHz, CR 4/5): [Meshtastic radio settings](https://meshtastic.org/docs/overview/radio-settings/), [Meshtastic signal meter](https://meshtastic.org/docs/software/android/user/signal-meter/). LoRa SNR floors per spreading factor are from the Semtech SX127x/SX126x datasheets (from memory).
- RTL-SDR overload at high gain on FM: [applied-ee SDR dynamic range notes](https://applied-ee.github.io/ee-notebook/docs/radio-rf/software-defined-radio/dynamic-range-and-front-end-limits), [SDR troubleshooting: gain and overload](https://hacker-playground.pages.dev/blog/sdr-troubleshooting-gain-sample-rate-and-overload)
- ARISS SSTV on 437.550 in 2026: [ARISS upcoming SSTV events](https://www.ariss.org/upcoming-sstv-events.html), [issfanclub Series 31](https://issfanclub.eu/2026/04/10/ariss-sstv-series-31-10-april-2026-starting-1200-utc-ending-14-april-2026-1355-utc/)
- Flock OUI and fingerprint reliability: [simeononsecurity: Flock detection arms race](https://simeononsecurity.com/articles/flock-camera-detection-arms-race-2026/), [flock-you detection types](https://virtuallyscott.github.io/flock-you/detection-types)
- NOAA POES decommissioning: [NOAA OSPO message, 2025-08-01](https://ospo.noaa.gov/data/messages/2025/08/MSG_20250801_1305.html), [NESDIS notice](https://www.nesdis.noaa.gov/news/suspension-of-poes-data-users-after-june-16-2025-and-decommissioning)
- Rayhunter false positives: [Rayhunter heuristics docs](https://efforg.github.io/rayhunter/heuristics.html), [EFF: Rayhunter, what we have found so far](https://www.eff.org/deeplinks/2025/09/rayhunter-what-we-have-found-so-far)
- From my own knowledge and not re-verified: ECPA 18 USC 2510/2511 on paging, 47 CFR 97.5, 97.113 and 97.403, RDS at 1187.5 bps with biphase coding, Friis, and R828D low-IF image rejection.
