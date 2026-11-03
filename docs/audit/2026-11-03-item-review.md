# SIGINT 101: review of the predict items and a lesson-QA method

**Bottom line.** Of MC's 0-for-4, three misses trace to flawed items: L1, L2 and L3 are BROKEN. L4 is sound. Across all 29 predicts, 5 are BROKEN (L1, L2, L3, L7, L8), 12 are MINOR and 12 are OK.

"Do we teach badly?" cannot be answered today, because the course has no post-instruction check. Nothing records whether an explain worked.

Missing a pre-instruction predict is normal, and it helps learning when two conditions hold: the guess was a plausible one, and what the learner then observes is unambiguous. Several of these items break the second condition. Their key contradicts what this console's waterfall actually draws.

For scale: answering at random, the chance of 0/4 on these items is 8/81, about 10%, because L2 accepts 2 of its 3 chips. One learner on four items settles nothing statistically. The evidence is at the item level.

*Evidence label.* The hardware claims below are inferred from the console's code: `sigint101/spectrum.py` `hop_plan()`, `-i 1`, and `static/waterfall.js` `drawRow()`. From that code:
- The default 88–108 MHz sweep runs as 8 hops of 2.5 MHz with 78.125 kHz bins, and each hop's centre bin is unmeasured: 89.25, 91.75 … 106.75 MHz.
- The airband sweep uses 42.4 kHz bins.
- Every sweep draws one row per second.
- The colour scale re-fits each row to its own minimum and maximum.

**Not checked:** any live capture on nomad-b.

---

## 1. Method

### 1.1 The rubric, and where each criterion comes from

| # | Criterion | Source | What failure looks like here |
|---|---|---|---|
| S | **The stem is clear.** It states the central idea and the directions plainly. | Haladyna, Downing & Rodriguez 2002, guidelines 14–15 | L3 stacks two hypotheticals and never names what "its picture" is compared with. |
| K | **There is a single defensible key, on this instrument.** | HDR 2002, guideline 19 ("only one of these choices is the right answer"). The "on this instrument" part is ours. | L1: "same width" is not what the 78 kHz, auto-scaled waterfall draws. |
| D | **Every distractor is plausible and functional.** | HDR 2002, guidelines 29–30; Rodriguez 2005 (3 options are optimal); Tarrant et al. 2009 (a distractor chosen by fewer than 5% is non-functional); Gierl et al. 2017 | L26: the stem itself rules out "one message holds both". |
| T | **The item is not a trick or trap.** | HDR 2002, guideline 7 ("avoid trick items"); Roberts 1993 | L8: the key hinges on "between words" versus "between overs", a distinction never taught. |
| R | **The answer is reachable, and not given away.** A novice can build a related guess from the screens shown (Kornell et al. 2009; Grimaldi & Karpicke 2012), but the screens must not state the answer outright (HDR 2002, guideline 28, "conspicuous correct choice"). A giveaway removes the surprise that makes predicting work (Brod et al. 2018; Brod 2021). | | R−: L1 needs the FCC allocation fact, which was not yet shown. R+ (giveaway): L10's demo names "frequency-shift". |
| F | **The verdict teaches.** It gives task- or process-level feedback, ideally per distractor, and it is not a score. | Butler & Roediger 2008; Hattie & Timperley 2007; Kluger & DeNisi 1996 | Verdicts explain the key but never the chosen distractor. The "MISSED" tag is score feedback. |
| H | **The prediction matches what the real instrument will show,** or the item is labelled a thought experiment. | POE (White & Gunstone 1992); Miller et al. 2013: conceptual learning is contingent on a *correct observation* | L2: the explain narrates "past the middle the gap barely moved" whatever the meter actually showed. |

**Flag rules:**
- **BROKEN:** a careful learner can be marked MISSED for a defensible answer (K, T or H fails), or the key is unreachable from the screens shown.
- **MINOR:** fixable wording, a giveaway, or a non-functional distractor.
- **OK:** none of the above.

### 1.2 What the literature says a predict gate needs

- **Wrong guesses help, under conditions.**
  - **The evidence.** Pretesting and prequestioning improve later learning even when the pretest answers are wrong (Richland, Kornell & Kao 2009; Kornell, Hays & Bjork 2009; review: Pan & Carpenter 2023).
  - **Conditions 1–2: the guess must be related, and feedback must follow straight away.** The guess has to be semantically related, so that it activates the right neighbourhood, and correct information must follow promptly. Grimaldi & Karpicke 2012 found no benefit for unrelated pairs or after a brief delay.
  - **Condition 3: the learner must be able to be surprised.** Predicting works partly through surprise at an expectancy-violating outcome (Brod et al. 2018; Brod 2021). A giveaway produces no surprise. A trap produces "surprise" at the item rather than at the physics.
  - **Condition 4: confidence matters.** High-confidence errors are corrected best (Butterfield & Metcalfe 2001; Metcalfe 2017). That only helps if the console captures confidence and the frame stays low-stakes.
- **Multiple-choice prequestions are fine if the lures compete.** Plausible, competitive distractors make MC tests trigger productive retrieval (Little et al. 2012). Lures can also be learned as facts unless feedback follows (Butler & Roediger 2008). The 3-chip format is supported (Rodriguez 2005), so the format is not the problem.
- **POE has three rules.** Predict-observe-explain asks the learner for a prediction with a reason, then lets them observe, then reconciles the two (White & Gunstone 1992):
  - Predictions are elicited, not graded. Crouch et al. 2004 found that predicting before a demo improved learning relative to passive watching.
  - The observation must be unambiguous. Students often misobserve or misremember demos in line with their prior beliefs, and learning depends on a correct observation (Miller et al. 2013).
  - A key that disagrees with the screen is the worst case POE has: the learner's own eyes "lose".
- **Failure is productive only when consolidated.** In productive-failure designs, the initial failure helps only when it is followed by instruction that builds on the learner's attempt (Kapur 2016; meta-analysis g = 0.36, Sinha & Kapur 2021).
- **Score-framed feedback can backfire.** Feedback that pulls attention toward the self ("MISSED") lowered performance in over a third of feedback interventions (Kluger & DeNisi 1996). Self-level feedback is the weakest kind (Hattie & Timperley 2007).

### 1.3 Pattern-level causes in this set

1. **Keys come from textbook theory, not from this instrument.** The observe stage then asks the learner to report what they see, and scores them against the theory.
   - L1: the station width varies with bin alignment (3 or 4 bins), per-row auto-scaling, HD sidebands and hop-centre gaps.
   - L2: from low to middle gain the gap usually widens, so "wider" is the learner's own trend. The meter's "peak" is the strongest bin in the whole band.
   - L3: a front end going deaf lowers the floor reading.
   - L7: the demo itself says FM overload "blinds the whole radio".
   - L8: one-second rows cannot resolve word gaps. The try even says an airband column is "lit only while someone talks".
   - L9: OOK pulses are invisible at one row per second.
   - The explains then *narrate* an outcome ("the stripe is the same width", "the gap barely moved") instead of using the learner's reading.
2. **A pre-instruction guess is graded like an exam.**
   - The header reads `RIGHT / MISSED · You said X · Answer: Y`. That is score feedback on a guess the design *expects* to be wrong.
   - No reason or confidence is captured, so a misconception can't be told from a coin flip (two-tier items: Treagust 1988; certainty index: Hasan et al. 1999), and high-confidence errors can't be singled out for hypercorrection.
   - No verdict speaks to the chip the learner actually chose.
   - The result is a tally that reads "0/4 = I failed", for an activity whose success measure is learning *after* the reveal.
3. **Difficulty is bimodal: traps early, giveaways later.**
   - **Unreachable or trap items:** L1 needs the FCC allocation fact; L3 needs the auto-scale behaviour; L7 and L8 need distinctions not yet shown.
   - **Giveaways:** in L5, L10, L11, L14, L20 and L25 the frame or demo states the key, so these work as comprehension checks, not predictions. L26 is near that: its demo and try strongly cue the shared ID.
   - **Neither delivers the "plausible guess, then honest surprise" the pretesting literature relies on.**
   - **Hypothesis to test, not a finding.** Three counterintuitive keys in a row can train test-wiseness: "pick the non-obvious chip" (Millman, Bishop & Ebel 1965). That would explain "smaller" on the sound L4.
4. **Keys that cover two chips, and binary stems on three chips.**
   - L2 and L7 accept 2 of 3 chips. Only one answer can be wrong, and "it depends" becomes a safe hedge.
   - L14, L16 and L26 pose an either/or question over three chips, and in L26 the stem rules one chip out. L28 has two chips that mean the same thing. Each is effectively a two-option item.
5. **Observe stages that aren't observations.**
   - Nearly half the lessons (L3–L7 and L17–L23 entirely; L24 and L28 in practice) are thought experiments. Their "observe" step reads "Hold your guess against…" or "Imagine…".
   - That is legitimate, but then the key must follow from facts already shown, and the stage should be labelled *reason*, not *observe*.
6. **No post-check, no transfer item, no pre/post measure.** Teaching quality is unmeasured, so MC's question has no data behind either answer.

### 1.4 The repeatable QA pipeline

Run the tiers in order. Tiers A, B and C are automated or scripted. Tier D needs real people.

| Tier | Step | Who | Trigger | Cost | Pass |
|---|---|---|---|---|---|
| **A. Lint** (deterministic) | Check each pack's predict: exactly one key, or a written `multi_key_reason`; stem ≤ 45 words; the stem doesn't pose an "X or Y" binary over 3 chips; no chip label's content words appear in frame/demo/try (giveaway heuristic); chip lengths within 2× of each other (longest-answer cue, HDR guideline 28); key position spread across the course. | Automated, inside `make selftest` | Every commit | Seconds | All checks green, or a waiver line in the pack |
| **B1. Rubric review** | An LLM reviewer applies the 7-criterion rubric (§1.1) to the changed pack and returns OK/MINOR/BROKEN plus a reason per criterion. It also checks that the try doesn't make the learner do what the predict asks them to predict (L2's bug). | Automated | Every change to a pack | ~1 min | No BROKEN; MINORs either fixed or acknowledged |
| **B2. Simulated-learner panel** | 3 personas × 10 samples each: "hacker, new to radio", "licensed ham", "RF engineer". Each sees *only* the screens before the question and returns a chip, a one-line reason and a confidence. | Automated | Every change to a predict | ~2 min | No key ambiguity: the RF engineer persona picks the key ≥ 9/10, and no persona argues a *defensible* non-key. Not a giveaway: novice persona < 9/10 on the key. No dead chip: every chip is picked at least once with a reason. |
| **C. HIL key check** | Run the predict's observe step on nomad-b. Commit the CSV rows and a screenshot to the repo. A script asserts the key against the data, for example: L1 counts lit bins per stripe; L2 compares the floor dB at middle and maximum gain. | Scripted. MC only if a physical step is needed (e.g. swapping an antenna). | Every change to an instrument predict, and every change to `spectrum.py` or `waterfall.js` | ~10 min | Key equals what the capture shows; the capture lives in the repo next to the pack. |
| **D1. Expert cold answer** | One radio-literate person (ham/SDR) answers each new predict cold, at the console, and says whether they could defend another chip. | Real person | Batched per unit, for new or rewritten predicts | ~10 min per unit | 100% on the key. Any expert miss, or any "I could argue B", makes the item BROKEN. |
| **D2. Cognitive interview** | 2–3 people who match the learner profile think aloud while answering, then take retrospective probes (script below). MC counts as participant 1. | Real people | Per unit, before hand-over, and after any BROKEN fix | ~20 min per person per unit | No misread stems, no "that was a trick", no missing-prerequisite codes left unfixed |
| **D3. Field log** | Record chip, confidence, time to commit, the post-check result and a one-tap "fair question?" rating. The progress DB already stores the prediction and `correct`. | Passive | Always | Free | Read as anecdotes until an item has around 30+ learners; only then compute statistics (§1.6). |

**Caveats on B2.** LLM respondents get coarse difficulty ordering roughly right (Lu & Wang 2024; Liu, Bhandari & Pardos 2024, with ensembles). They fail to reproduce real learners' misconceptions and difficulty:
- They correlate weakly with humans and shouldn't be zero-shot pilot participants (Säuberli, Frassinelli & Plank 2025).
- Strong models can't simulate the struggles of weaker students (Li et al. 2025/2026; Yuan et al. 2026, the "competence paradox").
- LLM judges have replicated known instructional effects at the level of whole materials (He-Yueya, Goodman & Brunskill 2024).

So use B2 only to find ambiguous keys, giveaways and dead chips. Never use it as a difficulty estimate or as proof that an item works.

**D2 interview script** (concurrent think-aloud plus verbal probes: Willis 2005; Leighton 2017; Ericsson & Simon 1993):
1. "Read the screens and talk while you pick."
2. "In your own words, what is the question asking?"
3. "What does *this one* or *its picture* refer to?"
4. "What would have to be true for each of the other chips to be right?"
5. "Sure, hunch or guess?"
6. After the reveal: "Did the explanation change your mind, or did it feel like a trick? Did the screen agree with the answer?"

Code each item for: misread stem / missing prerequisite / defensible alternative / felt like a trick / giveaway / screen disagreed with key. Adams & Wieman 2011 describe this interview-driven loop for validating science-concept instruments.

**The cheapest next step for MC's four.** A ten-minute retrospective probe with MC on L1–L4 ("why wider? why smaller?"). It settles whether L4's miss was a misreading, a real misconception, or test-wiseness after three traps.

### 1.5 Where post-checks and transfer belong

- **Lesson level.** Add a scored **check** stage after the explain:
  - It poses one *new* situation on the same concept (near transfer), never the predict re-asked.
  - Its key goes through tiers A–D like any predict, including tier C when the key is about the instrument.
  - Score it RIGHT / NOT YET, with a verdict per distractor.
  - Feed a missed check into the Leitner scheduler the exam trainer already uses (`exam.py`, intervals of 0/1/3/7/30 days). That gives spaced retrieval for free.
- **Unit level.** Build a 10–12-item concept inventory from the check bank, never from the predicts. Give it at the start and end of each unit and compute the normalized gain ⟨g⟩ = (post − pre)/(100 − pre) (Hake 1998). The concept-inventory model is the Force Concept Inventory (Hestenes et al. 1992).
- **Capstone.** One far-transfer task per unit, on something not taught (Barnett & Ceci 2002). Example: hand the learner an unlabelled capture and ask which modulation it is, and how they know.
- **Kirkpatrick levels for this course** (Kirkpatrick 1959; Kirkpatrick & Kirkpatrick 2006):
  - **Level 1, Reaction:** a one-tap "fair question?" after each verdict.
  - **Level 2, Learning:** checks plus unit gain.
  - **Level 3, Behaviour:** does the learner apply a skill unprompted, visible in the events log? Examples: passes their ppm to a decoder; captures a baseline before a session.
  - **Level 4, Results:** real receptions with provenance badges. The console already mints these.

**Sample checks for MC's four** (keys to be confirmed by tier C):

| Lesson | Check item | Key |
|---|---|---|
| L1 | One stripe is twice as wide as its neighbours. Likeliest reason? HD Radio sidebands / Twice the power / Two stations overlapping | HD sidebands |
| L2 | Your SNR is stuck at 6 dB with gain at maximum. What raises it? A better-placed antenna / Even more gain / A wider sweep | Antenna |
| L3 | The band is blank tonight, and your reference sits 15 dB below baseline. Verdict? The radio has gone deaf / The band is quiet / Can't tell | Deaf |
| L4 | At +164 ppm, how far off is 162.0 MHz (AIS)? ~2.7 kHz / ~27 kHz / ~270 kHz | ~27 kHz (162 × 164 Hz = 26.6 kHz) |

**Small UI changes.** These are proposals only; nothing in the repo was edited.
- For predicts, replace `RIGHT / MISSED · Answer:` with "Your call: X · What the radio does: Y".
- Add a keyboard-free confidence tap (Sure / Hunch / Guess).
- Add one verdict line per distractor.
- Keep the score for checks only.

### 1.6 What "good" looks like (numbers)

| Measure | Target | Basis |
|---|---|---|
| Predict, first-try accuracy in a pilot pool | Roughly **0.3–0.7**. Above 0.85 means a giveaway, and no surprise is possible. Below chance (0.33 for a single-key 3-chip item) among knowledgeable people means a trap or a flawed key. | Pretesting works on errors (Richland et al. 2009; Pan & Carpenter 2023); the range is our working band, not a published standard. |
| Predict, expert cold accuracy | **100%** | An expert miss means the key isn't defensible (HDR guideline 19). |
| Distractor function | Each chip draws **≥ 5%** in a pilot (≥ 1 persona in B2) | Tarrant et al. 2009 |
| Post-check, first-try p | **≈ 0.75–0.85** | Lord 1952's optimum for 3-option items is ≈ 0.77 (taken from a secondary source; original not read). |
| Post-check discrimination | D **≥ 0.30**, *only* once n ≥ ~30 | Ebel & Frisbie: ≥ 0.40 very good; 0.30–0.39 good; 0.20–0.29 marginal; below that, poor. Bands vary slightly by edition. |
| Miss→hit rate (predict missed, check right) | **≥ 0.7** | This is the pretesting effect actually happening. A low rate means the explain didn't correct the idea. |
| Unit normalized gain ⟨g⟩ | **≥ 0.5** good; **< 0.3** means redesign | Hake 1998: interactive-engagement courses averaged 0.48, traditional courses 0.23. |
| "Fair question?" | **≥ 80%** yes. Any single "unfair" sends the item to D1. | Kirkpatrick level 1 |

**One-learner caveat.** This course has essentially one learner, plus MC as tester. Item difficulty and discrimination statistics need dozens of respondents. Until then, quality rests on tiers A–C, the expert cold answer and interviews, and per-learner miss→hit and gain read as case data.

---

## 2. Per-item table

Failed criteria use the codes from §1.1: S, K, D, T, R− (unreachable), R+ (giveaway), F, H. "MC" shows MC's answer where MC answered.

| # | Lesson | Key | Flag | Fails | Reason (one line) |
|---|---|---|---|---|---|
| L1 | See the Invisible | same (MC: wider) | **BROKEN** | K, R−, T, H | The screen doesn't draw "same": 78 kHz bins give 3 or 4 bins per station by alignment, per-row auto-scale lights strong stations' skirts, US HD-hybrid stations run about 400 kHz, and hop-centre gaps cut some stripes. Equal allocation is never shown first, and the next stripe is already on screen. |
| L2 | Turn It Up | same / narrower (MC: wider) | **BROKEN** | S, K, T, H | The try already sends the learner to maximum, so this is a report, not a prediction. The 2-of-3 key leaves only the extrapolation of the learner's own low→middle widening as "wrong". "About the same" has no dB tolerance. The explain asserts the result. |
| L3 | Know Your Instrument | identical (MC: obvious) | **BROKEN** | S, K, T, H | Two stacked hypotheticals with no stated comparison. "Identical" is absolute, while a slowly deafening front end lowers the floor reading and "slowly" invites "a little". No FM-band picture is ever quiet. Nothing can be observed in-lesson. |
| L4 | The Number on the Dial | bigger (MC: smaller) | OK | — | The try gives "164 Hz per MHz", so the key follows by arithmetic. "About the same" is the real misconception. The verdict gives numbers. Why MC picked "smaller" is unknown: ask MC. Content check to make: does pilot-timed ppm equal the RF tuning error on this board (shared vs separate tuner crystal)? |
| L5 | The One Rule | illegal | MINOR | R+ | The demo states that ham transmitting needs a licence, and the try lists this exact case. It's a comprehension check, not a prediction. "Legal if you identify" is a good lure. |
| L6 | Antennas Are Tuning Forks | longer | OK | — | The formula is shown but inverse reasoning is still needed. "Same length" is a real misconception, and there is one key. |
| L7 | Build, Aim & Clean Up | no / depends | **BROKEN** | K, R, T | The demo says FM overload "blinds the whole radio", which supports "Yes". The key accepts two chips, and the verdict's "only if FM is overloading" makes all three defensible on an unstated condition. |
| L8 | Two Shapes: AM vs FM | neither | **BROKEN** | K, T, H, R− | The try says an airband column is "lit only while someone talks". One-second rows can't resolve word gaps, so what the learner sees points to "AM". The key rests on words-vs-overs (push-to-talk), first taught in the explain. |
| L9 | The Carrier That Vanishes | floor | MINOR | T (trivial), R+, H | "When nothing is keyed" is the floor for *any* modulation, so it doesn't test OOK. The demo already says "no carrier left behind". One-second rows can't show OOK pulses; that belongs to the Flipper's Read RAW view. |
| L10 | The Carrier That Shifts | shift | MINOR | R+, D | The demo names "frequency-shift" and the stem rules out "Blinks off", leaving a two-chip item whose key is the acronym. |
| L11 | Below the Noise Floor | decodes | MINOR | R+, K | The demo states the answer. −12 dB decodes on LongFast (SF11, limit ≈ −17.5 dB) but not on short-range presets, so add "on LongFast". The verdict should say why "garbled" can't happen: the CRC drops a corrupted packet. |
| L12 | The Hidden Text | nothing | MINOR | S (premise) | "The carrier's phase" is wrong: RDS is BPSK on a 57 kHz subcarrier that frequency-modulates the carrier. The explain's own "a PSK signal still shows up as energy" makes "A faint smear" arguable. Ask "a mark of its own on the waterfall?" instead. |
| L13 | The Sky Talks | nothing | OK | — | Both lures (key, licence) are real beginner beliefs. Single key, and the verdict cites the statute. |
| L14 | The Return Address | framed | MINOR | S, D, R+ | A binary stem over three chips. The frame and demo already list callsign, path and position. "Scrambled" is ruled out by "None of it is hidden". |
| L15 | Not Meant for You | law | OK | — | "Encryption" is exactly the hacker's lure. The key was taught in L5, and the observe step ties to the "withheld" column. Strong item. |
| L16 | What the Aircraft Says | text | MINOR | S, K | A binary stem. The live log is full of handshakes and terse codes, so "Only raw numbers" can match what the learner sees, and the verdict hedges "mostly". Ask "Is it encrypted?" instead. |
| L17 | Who's Watching | proves-nothing | OK | — | "Prove" makes "No" the single key. "Mostly" is a tempting Bayesian lure, and the verdict teaches the rule. |
| L18 | The Camera's Fingerprint | weak-hint | OK | — | Reachable from the try's prefix-vs-fingerprint prompt, and the verdict explains the tiers. |
| L19 | Catch the Catcher | look-closer | OK | — | Same shape as L17. The key is backed by EFF's documented false positives. |
| L20 | The First Inch | quiet-first | MINOR | R+, D | The demo says "the FIRST stage's sets the whole receiver's sensitivity". "The quiet one last" is non-functional. |
| L21 | Slide It Down | image (99.9) | OK | — | It needs reasoning from "keeps only the difference". "200.1" (the sum product) is a smart lure. Inside the single-mixer model the lesson states, the key is sound; the real R828D's image rejection is next lesson's point. |
| L22 | Two Streams | full (2.4 MHz) | OK | — | The real-vs-complex Nyquist misconception is the lure. The key is idealised; the usable slice is slightly under 2.4 MHz. |
| L23 | What Every Pixel Is | aliases | OK | — | "Unfiltered" pins the key, and "true spot" is a real misconception. |
| L24 | A Picture From Space | grainy | MINOR | K, H | "Tone buried in hiss" describes a below-noise pass, where APT loses sync and you get static; say "faint and hissy". Not observable (NOAA APT ended in 2025), so label it a thought experiment. |
| L25 | A Picture in the Air | photo | MINOR | R+, D | The frame says "a photograph paints itself". Both distractors are implausible after that. |
| L26 | The Sea Talks | mmsi | MINOR | D | The stem's "two different AIS messages" rules out "One message holds both", leaving two real chips. |
| L27 | The Coded Voice | some | OK | — | "Some" is defensible anywhere, and both lures (all, none) are real beliefs. |
| L28 | A Message That Hops | relayed | MINOR | D, K | "Dies at your range" and "Only nodes you hear" are the same idea. Relaying depends on the middle node's role (CLIENT_MUTE doesn't rebroadcast), so say "a default node". |
| L29 | Meet the Flipper | a-bench | OK | — | "More range" is a plausible USB-power belief. The key is defensible and the verdict teaches the field/bench split. |

**Totals:** BROKEN 5 (L1, L2, L3, L7, L8) · MINOR 12 · OK 12.

---

## 3. Rewrites for the BROKEN items

Each rewrite keeps 3 chips and about 40 words. For each key marked *provisional*, the rewrite ships only after the tier-C capture confirms it.

### L1: See the Invisible
- **Stem (34 words):** "Next you'll tap the dimmest stripe you can find and hold it beside the brightest one. On this screen, will the dim station's stripe look NARROWER, the SAME width, or WIDER? Lock it in."
- **Chips:** Narrower · Same width · Wider
- **Key:** Narrower *(provisional)*
- **Verdict:** "Narrower: every FM station gets the same 200 kHz slice, but this screen paints only what clears the noise floor, so a weak station shows just its core."
- **Pack edits:** In the explain, drop "from one station to the next the stripe is the same width". Teach licensed slice versus visible width, and keep the HD "twice as wide" line. Move the original "width names the kind of signal" idea into the post-check (§1.5).
- **Tier C:** Capture 3 bright/dim pairs from the default sweep and count the bins at least 6 dB above the row median per stripe. The key holds if the dim stripe is narrower in all 3 pairs. Avoid example stations within one bin of a hop centre (89.25, 91.75 … 106.75 MHz).

### L2: Turn It Up
- **Try fix:** "stop at two settings — low, then middle". Remove "maximum" from the try.
- **Stem (39 words):** "You've read the meter at low and middle gain. Next you'll push to maximum and the station's peak will climb. Watch the grassy noise floor: will it RISE with the peak, STAY PUT, or SINK? Lock it in."
- **Chips:** Rises with it · Stays put · Sinks
- **Key:** Rises with it *(provisional)*
- **Verdict:** "Rises with it: gain can't tell signal from noise, so it lifts both, and the gap between them, your SNR, barely moves. At the very top, overload can even shrink it."
- **Pack edits:** The explain uses the learner's own readings ("your gap moved N dB") instead of asserting "barely moved".
- **Tier C:** The meter floor at maximum is at least 3 dB above the floor at middle, in 3 runs on nomad-b. Set the threshold from that capture.

### L3: Know Your Instrument
- **Stem (39 words):** "Two pictures of the same empty stretch of band: one from a healthy radio, one from a radio whose antenna cable has corroded through. From the pictures alone, could you tell which radio is deaf? Lock it in."
- **Chips:** Yes, at a glance · Only by a faint dimming · No, they look alike
- **Key:** No, they look alike *(provisional)*
- **Verdict:** "They look alike: this screen rescales its colours to whatever noise it gets, so a deaf radio and an empty band both paint plain grass. Only your reference against the baseline tells them apart."
- **Pack edits:** The observe stage shows a labelled *replay* of both captures, antenna on and off, over the same empty span outside FM. Or the learner unscrews the SDR antenna there: the SDR is receive-only, so this is safe.
- **Tier C:** Capture both on nomad-b. Two people who don't know which picture is which pick the deaf radio at no better than chance over 5 pairs. If they can pick it (the floor drop shows), re-key to "Only by a faint dimming" and teach that the number moves even when the picture doesn't.

### L7: Build, Aim & Clean Up
- **Stem (32 words):** "Your 915 MHz gadget reads weak. No FM tower is near, and your dipole legs are still cut for 1090. Will sliding in the FM-notch filter make 915 stronger? Lock it in."
- **Chips:** Yes, it cleans the radio up · A little, less noise overall · No, wrong culprit
- **Key:** No, wrong culprit (single key)
- **Verdict:** "No: the notch removes only 88 to 108 MHz. With no strong FM overloading the front end, it does nothing for 915. Lengthen the legs to about 8.2 cm."
- **Pack edits:** The demo's "blind the whole radio" case moves into the explain as the one condition where the notch *would* help.

### L8: Two Shapes: AM vs FM
- **Stem (28 words):** "Watch an airband channel through a conversation: its thin column lights, goes dark for a few seconds, then lights again. When it goes dark, what happened? Lock it in."
- **Chips:** The speaker let go of push-to-talk · The speaker paused between words · The signal faded out
- **Key:** The speaker let go of push-to-talk
- **Verdict:** "Let go of push-to-talk: an AM carrier holds through the pauses between words. The column goes dark only when the transmitter stops."
- **Pack edits:** The explain says plainly that this screen's one-second rows are too slow to show a word gap, so every dark stretch is a whole transmission ending.
- **Optional observe upgrade:** If a local ATIS channel is in range (a looping recording on a continuous carrier), make it the observe target. Its column stays lit through every pause.
- **Tier C:** Capture airband rows on nomad-b and confirm the dark gaps are at least one row long and line up with transmissions ending. Check this against a handheld scanner, or against an ATIS channel staying lit.

---

## 4. Citations

Verified this session by web search unless marked *(from memory)*. Where only an abstract or secondary source was reachable, that is noted.

**Item writing and distractors**
- Haladyna, T. M., Downing, S. M., & Rodriguez, M. C. (2002). A review of multiple-choice item-writing guidelines for classroom assessment. *Applied Measurement in Education, 15*(3), 309–333. doi:10.1207/S15324818AME1503_5. Guideline wordings were checked against a secondary transcription.
- Haladyna, T. M., & Rodriguez, M. C. (2013). *Developing and Validating Test Items.* Routledge. *(from memory)*
- Rodriguez, M. C. (2005). Three options are optimal for multiple-choice items: A meta-analysis of 80 years of research. *Educational Measurement: Issues and Practice, 24*(2), 3–13.
- Gierl, M. J., Bulut, O., Guo, Q., & Zhang, X. (2017). Developing, analyzing, and using distractors for multiple-choice tests in education: A comprehensive review. *Review of Educational Research, 87*(6), 1082–1116.
- Tarrant, M., Ware, J., & Mohammed, A. M. (2009). An assessment of functioning and non-functioning distractors in multiple-choice questions: A descriptive analysis. *BMC Medical Education, 9*, 40.
- Roberts, D. M. (1993). An empirical study on the nature of trick test questions. *Journal of Educational Measurement, 30*(4), 331–344.
- Millman, J., Bishop, C. H., & Ebel, R. (1965). An analysis of test-wiseness. *Educational and Psychological Measurement, 25*(3), 707–726.

**Classical item analysis**
- Ebel, R. L., & Frisbie, D. A. (1991). *Essentials of Educational Measurement* (5th ed.). Prentice Hall. The discrimination bands were confirmed through secondary sources only.
- Lord, F. M. (1952). The relation of the reliability of multiple-choice tests to the distribution of item difficulties. *Psychometrika, 17*(2), 181–194. Title verified; pages from memory; the 0.77 optimum comes via a secondary source.
- Ding, L., Chabay, R., Sherwood, B., & Beichner, R. (2006). Evaluating an electricity and magnetism assessment tool: Brief Electricity and Magnetism Assessment. *Physical Review ST – Physics Education Research, 2*, 010105. A worked example of difficulty, discrimination and point-biserial analysis; its exact thresholds were not confirmed.

**Cognitive interviewing and instrument validation**
- Willis, G. B. (2005). *Cognitive Interviewing: A Tool for Improving Questionnaire Design.* Sage.
- Leighton, J. P. (2017). *Using Think-Aloud Interviews and Cognitive Labs in Educational Research.* Oxford University Press.
- Ericsson, K. A., & Simon, H. A. (1993). *Protocol Analysis: Verbal Reports as Data* (rev. ed.). MIT Press. *(from memory)*
- Adams, W. K., & Wieman, C. E. (2011). Development and validation of instruments to measure learning of expert-like thinking. *International Journal of Science Education, 33*(9), 1289–1312.
- Treagust, D. F. (1988). Development and use of diagnostic tests to evaluate students' misconceptions in science. *International Journal of Science Education, 10*(2), 159–169.
- Hasan, S., Bagayoko, D., & Kelley, E. L. (1999). Misconceptions and the Certainty of Response Index (CRI). *Physics Education, 34*(5), 294–299.
- AERA, APA, & NCME (2014). *Standards for Educational and Psychological Testing.* *(from memory)*

**Pretesting, errorful generation and feedback**
- Richland, L. E., Kornell, N., & Kao, L. S. (2009). The pretesting effect: Do unsuccessful retrieval attempts enhance learning? *Journal of Experimental Psychology: Applied, 15*(3), 243–257.
- Kornell, N., Hays, M. J., & Bjork, R. A. (2009). Unsuccessful retrieval attempts enhance subsequent learning. *JEP: Learning, Memory, and Cognition, 35*(4), 989–998.
- Grimaldi, P. J., & Karpicke, J. D. (2012). When and why do retrieval attempts enhance subsequent encoding? *Memory & Cognition, 40*(4), 505–513.
- Pan, S. C., & Carpenter, S. K. (2023). Prequestioning and pretesting effects: A review of empirical research, theoretical perspectives, and implications for educational practice. *Educational Psychology Review, 35*, 97.
- Little, J. L., Bjork, E. L., Bjork, R. A., & Angello, G. (2012). Multiple-choice tests exonerated, at least of some charges: Fostering test-induced learning and avoiding test-induced forgetting. *Psychological Science, 23*(11), 1337–1344.
- Butler, A. C., & Roediger, H. L. (2008). Feedback enhances the positive effects and reduces the negative effects of multiple-choice testing. *Memory & Cognition, 36*(3), 604–616.
- Butterfield, B., & Metcalfe, J. (2001). Errors committed with high confidence are hypercorrected. *JEP: Learning, Memory, and Cognition, 27*(6), 1491–1494.
- Metcalfe, J. (2017). Learning from errors. *Annual Review of Psychology, 68*, 465–489.
- Brod, G., Hasselhorn, M., & Bunge, S. A. (2018). When generating a prediction boosts learning: The element of surprise. *Learning and Instruction, 55*, 22–31.
- Brod, G. (2021). Predicting as a learning strategy. *Psychonomic Bulletin & Review, 28*, 1839–1847.
- Kapur, M. (2016). Examining productive failure, productive success, unproductive failure, and unproductive success in learning. *Educational Psychologist, 51*(2), 289–299.
- Sinha, T., & Kapur, M. (2021). When problem solving followed by instruction works: Evidence for productive failure. *Review of Educational Research, 91*(5), 761–798.
- Kluger, A. N., & DeNisi, A. (1996). The effects of feedback interventions on performance: A historical review, a meta-analysis, and a preliminary feedback intervention theory. *Psychological Bulletin, 119*(2), 254–284.
- Hattie, J., & Timperley, H. (2007). The power of feedback. *Review of Educational Research, 77*(1), 81–112.

**Predict-observe-explain and demonstrations**
- White, R. T., & Gunstone, R. F. (1992). *Probing Understanding.* Falmer Press.
- Crouch, C. H., Fagen, A. P., Callan, J. P., & Mazur, E. (2004). Classroom demonstrations: Learning tools or entertainment? *American Journal of Physics, 72*(6), 835–838.
- Miller, K., Lasry, N., Chu, K., & Mazur, E. (2013). Role of physics lecture demonstrations in conceptual learning. *Physical Review ST – Physics Education Research, 9*, 020113.

**Module validation: gain, transfer, Kirkpatrick**
- Hake, R. R. (1998). Interactive-engagement versus traditional methods: A six-thousand-student survey of mechanics test data for introductory physics courses. *American Journal of Physics, 66*(1), 64–74.
- Hestenes, D., Wells, M., & Swackhamer, G. (1992). Force Concept Inventory. *The Physics Teacher, 30*(3), 141–158. *(from memory)*
- Barnett, S. M., & Ceci, S. J. (2002). When and where do we apply what we learn? A taxonomy for far transfer. *Psychological Bulletin, 128*(4), 612–637. *(from memory)*
- Kirkpatrick, D. L. (1959). Techniques for evaluating training programs. A four-article series beginning November 1959 in the *Journal of the American Society of Training Directors*.
- Kirkpatrick, D. L., & Kirkpatrick, J. D. (2006). *Evaluating Training Programs: The Four Levels* (3rd ed.). Berrett-Koehler. *(from memory)*

**Simulated (LLM) students for item piloting**
- Lu, X., & Wang, X. (2024). Generative Students: Using LLM-simulated student profiles to support question item evaluation. *Proceedings of the 11th ACM Conference on Learning @ Scale (L@S '24).* arXiv:2405.11591.
- Liu, Y., Bhandari, S., & Pardos, Z. A. (2024). Leveraging LLM-respondents for item evaluation: A psychometric analysis. arXiv:2407.10899. Formatted for BJET; journal publication not confirmed.
- He-Yueya, J., Goodman, N. D., & Brunskill, E. (2024). Evaluating and optimizing educational content with large language model judgments. *Proceedings of EDM 2024.* arXiv:2403.02795.
- Säuberli, A., Frassinelli, D., & Plank, B. (2025). Do LLMs give psychometrically plausible responses in educational assessments? *BEA Workshop at ACL 2025.* arXiv:2506.09796.
- Li, M., Chen, H., Xiao, Y., Chen, J., Jiao, H., & Zhou, T. (2025; ACL 2026). Can LLMs estimate student struggles? Human-AI difficulty alignment with proficiency simulation for item difficulty prediction. arXiv:2512.18880.
- Yuan, Z., Xiao, Y., Li, M., Xuan, W., Tong, R., Diab, M., & Mitchell, T. (2026). Towards valid student simulation with large language models. arXiv:2601.05473.

**Not found.** No published source gives target accuracy bands specifically for *pre-instruction predict* items. The 0.3–0.7 band in §1.6 is our working rule, derived from the pretesting literature rather than taken from it.
