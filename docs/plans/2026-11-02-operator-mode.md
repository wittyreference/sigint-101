# Operator mode v1 — the rack (spec, 2026-11-02)

**Why.** MC's 11-02 question: when the learner leaves the kiosk lessons, what then? The answer we
settled on is three layers.
- **Garden:** the lessons. They gain a command strip (sprint item 18).
- **Rack:** this spec. The same instruments with the lesson script removed. It is the field
  instrument, because in the field the touch panel is the only interface.
- **Field:** the real tools over SSH. That comes in later updates.

MC put the rack in the 11-08 cut line: **"1–19 plus operator mode"**.

**Depends on** two sprint branches. Build on top of both once they merge:
- `fix-decoder-truth`: honest stopped state, the command strip, and the kill path.
- `fix-waterfall-tune`: real tuning, gain, and only measured bins.

## Behaviour
- **Entry.**
  - Home gets a full-width bar under the unit grid, styled like the licence-exam bar:
    "Operator mode" with the line "The radios, no script".
  - Route `/operate` opens the last instrument used (or the waterfall), and `/operate/{instrument}`
    opens one directly.
  - Back always goes home.
- **Layout at 800x480 (fits, no page scroll).**
  - A left column of instrument buttons, 72 px or more, which scrolls on its own: Waterfall,
    ISM (rtl_433), ADS-B, APRS, Pager framing, ACARS, AIS, RDS, DMR/P25, APT, SSTV, LoRa,
    Flock, Rayhunter, Clock (ppm) and Health (provenance).
  - The rest is the selected panel at full size. Under it are the tune strip (frequency,
    ±, step, and gain where it applies), the command strip, and a one-line reception log.
- **Free tuning.**
  - The waterfall takes any centre and span within the tuner's reach and the sweep limits that
    already exist (`/api/tune`).
  - Single-channel decoders (APRS, POCSAG framing, RDS, DMR, ACARS, AIS) accept a frequency
    override: `POST /api/<decoder>/tune` restarts the child on the new frequency, within band
    sanity limits per decoder.
  - The M15 rule still holds in the rack: on 929–932 MHz POCSAG text is withheld (MC's call).
- **SDR arbitration.** Switching instruments claims the dongle through `_claim_sdr`, exactly as
  lessons do. LoRa, Flock and Rayhunter use their own radios.
- **Reception log.** Every decode the rack shows is appended to `events` with its provenance:
  instrument, frequency, gain, ppm state, radio serial and time. The strip shows the newest line.
  This is the substrate for honest badges later. A replay or sample never logs as a reception.
- **Honesty.** Every invariant in CLAUDE.md applies:
  - no invented signal;
  - a dead child shows stopped;
  - no-radio states are honest;
  - the command strip shows the real argv.

## Build notes
- First extract each instrument panel's markup from `console.html` into one partial or macro per
  panel. Lessons and the rack then render the same partial. This is a refactor: keep the lesson
  render byte-equivalent and prove it with the existing template tests plus a render diff.
- TDD the routes (`/operate`, `/operate/{instrument}`, the decoder tune routes with their band
  limits) and the reception-log writes.
- Render-verify every instrument view at 800x480 in dark and red-night.
- HIL on nomad-b inside the SDR flock (see `docs/plans/…` sprint brief): retune the waterfall,
  retune one decoder, and see a real reception land in the log (FM, or the local Acurite sensor
  on rtl_433).

## Out of scope for v1
Recording or IQ capture, saved presets, scanning, audio, transmit, and the field card. These
follow in later updates.
