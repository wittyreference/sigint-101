# SIGINT 101 — STATUS

## NEXT ACTION (resume here)
**CURRICULUM PINNED (MC, 2026-10-29).** Unit 5 Decode is complete (M13-M16 landed). MC scoped the
rest of the course across four tracks — **12 new lessons + the Flipper Companion FAP** — in a 4-round
AskUserQuestion pass. Full map in **CURRICULUM ROADMAP** below. **Build order (MC's call): Counter-
surveillance FIRST**, then Unit 6 → Decode+ → Meshtastic → Flipper FAP. **ALL 12 CURRICULUM LESSONS
LANDED: CS1 (M17), CS2 (M18), CS3 Rayhunter (M19), Unit 6 (M20-M23), Decode+ data/voice (M26 AIS,
M27 DMR/P25), Decode+ Images (M24 APT, M25 SSTV), Meshtastic (M28), plus the `claim()` arbitration
refactor — all HIL-verified at 800x480.** Units: U7 counter-surveillance = 17-19; M26/M27 join Unit 5
Decode (13-16, 26, 27); M24/M25 are Unit 8 Images; M28 is Unit 9 Mesh. **Next (LAST track): the
Flipper Companion FAP** — Pulse Scope + lesson sync (show the current lesson on the Flipper, push a
capture back to the console). An on-device C app, its own build track.
- **Deferred live HIL (hardware, not code):** M19 live needs the Orbic powered on its WiFi (honest-503
  verified); M27 live needs the dsd-fme build + a DMR repeater; M28 live mesh needs MC's 2nd node
  (the LoRa panel runs live honest-empty on nomad-b now); M24/M25 real images need an antenna + pass.

**Unit 6 fit note (durable, now in CLAUDE.md Corrections):** the reading-stage layout has ~250-300px
of fixed overhead (concept banner on every stage + title + footer), so a reading body must be **≤~50
words** to fit 800x480 — measured: 85-word bodies render to 543-590px, 50-word bodies to 437px
(ceiling 480). Shrink lesson copy to fit, always; never scroll, never ask.

**CS2 HIL (observed on nomad-b 2026-10-29):** the RTL8821CU dongle is on nomad-b as `wlan1`
(`rtw_8821cu`) in monitor mode; `tcpdump` given `CAP_NET_RAW` so the non-root app captures; `/api/flock`
returns `available:true` + honest-empty (no Flock camera in MC's airspace, real frames flowing — the
parser is proven on a real 207-frame off-air capture in the unit tests); both gates render at a forced
800x480 (predict Commit 474, demo panel LIVE). The detector reads wlan1, a separate radio, so no SDR
arbitration. **DEPLOYMENT FOLLOW-UP (not automated yet):** the kiosk must (a) `setcap
cap_net_raw,cap_net_admin+eip` on tcpdump and (b) put wlan1 into monitor mode before the detector runs
— today those are manual HIL steps, not a boot service. Log as a CS-track provisioning item.

**Backends built by workflow (2026-10-30):** the `decode-plus-backends` Workflow (4 agents, isolated
worktrees, TDD) built all four Decode+ decoder modules. AIS + DMR landed (below); APT + SSTV sources
are staged in scratchpad `decode-plus/` (apt.py/sstv.py + tests) for the Images unit — reviewed-on-
integration, not yet wired. The earlier `sigint-curriculum-fanout` run drafted + verified all 10
lesson packs (staged in scratchpad `lessons/`).

**M26 (AIS) + M27 (DMR/P25) DONE and landed (observed, 2026-10-30) — Decode+ data/voice, join Unit 5.**
`sigint101/ais.py`: rtl_ais opens the dongle and pushes AIVDM over UDP; the decoder binds that
socket and decodes types 1/2/3 (position) + 5 (static, two-fragment reassembly) in pure Python into
a roster keyed by MMSI (the sea's twin of the ADS-B radar). TDD against the **published gpsd AIVDM
vectors** (MMSI 477553000 position; EVER DIADEM static) — real-data proof at the unit level.
`sigint101/dmr.py`: dsd-fme auto-detects DMR/P25 and prints signaling to stderr; the parser reads
sync + talkgroup/source/slot/colour into a call log (requires a sync AND an ID — a bare sync is not
a call). Both are the **8th and 9th SDR-exclusive consumers**, registered in `sdr_consumers` (one
line each, thanks to `claim()`). `/api/ais` + `/api/dmr` + `ais`/`dmr` panels (ais.js/dmr.js) +
frozen branches (VESSELS/CALLS HIDDEN) + packs `26-ais.yaml` / `27-digital-voice.yaml`. **HIL on
nomad-b:** both lessons' demo + predict render at a forced 800x480 (demo Next=423, predict
Commit=474 — same as the shipped instrument lessons); `rtl_ais` built from source + installed, live
`/api/ais` runs honest-empty on the real radio (available:true, vessels:[] — no ships inland) with
rtl_ais genuinely running, and the `claim()` arbitration released it when `/api/spectrum` reclaimed
the dongle. **Deferred:** dsd-fme build for live `/api/dmr` + a real DMR capture (see open items).

**M24 (NOAA APT) + M25 (SSTV) DONE and landed (observed, 2026-10-30) — Decode+ Images, new Unit 8.**
The first instrument whose product is an IMAGE, not a record list. `sigint101/apt.py`: satdump runs
the NOAA APT pipeline on a ~137 MHz pass and writes PNGs; the decoder orchestrates it and scans the
output dir (release stops the capture but KEEPS the images — real receptions on disk).
`sigint101/sstv.py`: rtl_fm|sox|sstv decodes a bounded WAV to a PNG, a reader thread captures the
mode into a sidecar. Both are the **10th and 11th SDR-exclusive consumers**. New image panel: a
`.gallery` frame that shows the decoded `<img>` (served by a guarded `/media/{path}` route that
exposes ONLY .png under data/apt + data/sstv — never the progress/exam/events stores), honest
"waiting" until a real decode, no placeholder. `/api/apt` + `/api/sstv`, apt.js/sstv.js, packs
`24-noaa-apt.yaml` / `25-sstv.yaml`, Unit 8 "Images" (images glyph). **HIL on nomad-b:** both render
at a forced 800x480 (demo Next=423, predict Commit=474); live `/api/apt` + `/api/sstv` honest-503
(tools absent); the `/media` serving route proven on the real deploy (a dropped PNG served 200, a
non-image folder 404). **Deferred (install + physical):** build satdump + colaclanth/sstv, then a
real pass/transmission to decode an actual picture — the image-display path is unit- and
HIL-plumbing-tested, but a genuine decoded image needs a 137 MHz antenna + NOAA pass (APT) or an
SSTV transmission (SSTV). No fake/placeholder image is ever shown — the honest empty frame stands in.

## CURRICULUM ROADMAP (pinned 2026-10-29)
Lesson ids assigned by build order from M17 up; unit/track placement finalized per lesson at build
time. All lessons keep the six-stage intro-pack shape, TDD backends, and HIL-on-nomad-b discipline.

**Track A — Counter-surveillance (7th track, "detect & recognize", BUILD FIRST).** 3 lessons:
- **CS1 framing** — how mass surveillance works (ALPR networks, cell-site simulators) + the law/ethics.
- **CS2 Flock cameras** — **reproduce the FlockYou detection capability natively on nomad-b** (MC's
  correction 2026-10-29: do NOT ingest the M5 device; rebuild the algorithm on our own radio). The M5
  Atom Echo / Elmer corpus is the *algorithm reference only*. Detector logic = pure
  `parse_management_frame → match_signatures` TDD'd against real 802.11 captures (same pattern as the
  decode lessons). Capture layer = **a monitor-mode USB WiFi adapter MC will add** (Alfa AWUS036ACM /
  mt7612u or an rtl8812au); nomad-b's built-in BCM43455/brcmfmac has **no monitor mode** and the
  RTL-SDR can't reach 2.4 GHz, so live capture defers until the adapter lands (honest-empty until
  then). Passive/RX-only. **Signature set (authoritative: upstream `colonelpanichacks/flock-you`
  @6704f01, `main.cpp`):** 34 OUIs (32 community + 2 firmware-extracted incl. Flock's own `b4:1e:52`),
  3 SSID keywords (`flock`/`penguin`/`fs ext battery`), exact probe-request IE fingerprint
  `2,12,127,221:506f9a16030103,45,191,221:0050f208000000`, matched in tiers on addr2/addr1/addr3.
  **OPEN BLE-TX question: answered for upstream — passive (`setActiveScan(false)`, no SCAN_REQ); MC's
  specific vendor binary still needs the corpus's RF-observation test to be 100% sure, but we're
  reproducing upstream, so our build is passive by construction.** Corpus:
  `pi5:~/elmer/.claude/references/hardware/flockyou-detector.md`. Repo clone in this session's
  scratchpad (`flockyou-repo`).
- **CS3 Rayhunter** — EFF IMSI-catcher / cell-site-simulator detector on an Orbic hotspot. Web UI/JSON
  at `http://192.168.1.1:8080`. Orbic is NOT on the tailnet; **MC's call: the course box (nomad-b)
  joins the Orbic's WiFi AP and polls it** (box is off the main net during that capture). Onboarding
  corpus: `pi5:~/elmer/analysis/sessions/2026-09-29-device-onboarding-blackhat-pager-orbic.md`.

**Track B — Unit 6 "Under the Hood" (4 lessons, in-spine).** How the instrument actually works:
front-end (antenna+LNA) → mixing/downconversion → I/Q sampling → FFT/decimation (DSP). No build/
provisioning capstone (MC excluded it).

**Track C — Decode+ (split by type, 4 lessons).**
- *Images* (new unit): **NOAA APT** weather-sat (137 MHz, decode to a cloud image) + **SSTV**.
- *Data/voice* (near Unit 5): **AIS** ship tracking + **DMR/P25** digital voice (full decode, no
  special ethics framing — MC's call).

**Track D — Meshtastic (own mini-track, 1 lesson).** The mesh/app layer above the M11 LoRa PHY —
nodes, channels, messages. Needs a second mesh node (MC acquiring; maybe a Flipper LoRa hat).

**Track E — Flipper Companion FAP (on-device app, own build track, LAST).** Pulse Scope + lesson sync
(show current lesson on the Flipper, push a capture back to the console).

**Installs:** MC pre-approved everything these tracks need, incl. daemons/services for Flock ingest
and Rayhunter.
Open items:
- **DONE (MC-approved 2026-10-30, squash-merged `6b23996`): `claim(target)` arbitration helper.** The
  SDR routes' open-coded "release the others, resume me" lists are now one `_claim_sdr(target)` call
  off the `sdr_consumers` tuple in `app.py`; adding a consumer is one tuple entry + its route. A
  parametrized contract test pins the full set for every SDR route (resume self, release the rest,
  never touch LoRa/Flock). Proven live on nomad-b: starting rtl_ais then hitting `/api/spectrum`
  released it (dongle handed back). There are now **9 SDR-exclusive consumers** (+ ais, dmr).
- **DEFERRED (install + physical): dsd-fme for live DMR/P25 (M27).** `dsd-fme` is a source build
  (cmake + mbelib + libsndfile/ncurses/libusb) — not in Debian. The M27 route is HIL-verified
  honest-503 (tool absent) and the arbitration is proven via AIS (identical pattern), but a live
  `/api/dmr` honest-empty run, and later a real DMR/P25 capture to exercise the parser on live
  output, await the build + a digital-voice repeater in range. The parser is TDD'd against dsd-fme's
  own source format strings.
- **ACARS live capture (deferred, physical)** — M16's pipeline is HIL-proven BOTH ways: live
  `/api/acars` runs honest-empty on the real radio (nomad-b hears no aircraft on 131.55), AND the
  full chain was proven with REAL data by feeding acarsdec's own `test.wav` through the real decoder
  (12 genuine ACARS messages decoded + rendered — see the populated screenshot). `acarsdec` built
  from source (cmake, `-Drtl=ON`) + installed to `/usr/local/bin`. Live-off-air needs VHF ACARS
  traffic + antenna.
- **POCSAG live capture (deferred, physical)** — M15's pipeline is HIL-proven honest-empty; needs a
  pager-band antenna + live POCSAG traffic in range (nomad-b heard none). `multimon-ng` installed.
- **APRS live capture (deferred, physical)** — M14's pipeline is HIL-proven honest-empty, but
  **nomad-b is deaf at 2m** (0 packets decoded on 144.390 in 50s). Needs a 2m/VHF antenna + local
  APRS traffic. `multimon-ng` installed on nomad-b (apt).
- **ADS-B live radar capture (deferred, physical)** — M13's pipeline is HIL-proven end to end
  (dump1090 built + serving, /api/adsb runs live honest-empty), but **nomad-b's antenna is deaf at
  1090 MHz** (411k Mode-S preambles, 0 CRC-valid, blank signal power — no planes decode). This is a
  **UHF** antenna, independent of the FM one. Drop a 1090 MHz ADS-B antenna (or a tuned ~6.9 cm whip)
  on the SDR and the scope lights up; set `SIGINT101_RX_LAT`/`RX_LON` to place contacts on the rings.
- **RDS below-the-waterfall capture (deferred, physical)** — needs a tuned FM dipole/telescopic on
  the SDR (same block as M4's pilot; an FM antenna unblocks both M12 RDS and M4 ppm).
- **LoRa below-floor capture — DONE** (−4.75 to −5.25 dB SNR, 2026-10-27).
- **FSK two-tone live capture (deferred)** — needs an on-frequency FSK transmitter.
- **M4 real-ppm (deferred)** — same FM-antenna block as M12's RDS; a good FM antenna unblocks both.
- **Follow-up: M8's predict prompt is ~60 words / 5 lines — 1 px over at a true 800x480.** Trim it.
- **Small: commit the 3 NCVEC diagram JPGs (T-1/T-2/T-3)** so exam figure questions show their diagram.

**M16 (ACARS) DONE and landed (observed, 2026-10-28), squash-merged to main — COMPLETES UNIT 5.**
ACARS is the airlines' VHF text link (~131.550 MHz): typed messages (position/weather/engine reports,
free text), mostly in the clear. New `sigint101/acars.py`: `parse_acars_message` (acarsdec `-o 4`
JSON → tail/flight/label/mode/block_id/msgno/ack/text/freq/level; label always present, text
optional), `AcarsLog` (bounded newest-first), `AcarsDecoder` (owns the `acarsdec` process, which
opens the dongle **directly** like dump1090 — no rtl_fm front end; reader thread, release/resume) —
the **7th SDR-exclusive consumer**. `/api/acars` + an `acars` panel reusing the decoder list
(`static/acars.js`) + `lesson_packs/16-acars.yaml`. `acarsdec` built from source on nomad-b (cmake,
`-Drtl=ON`) → `/usr/local/bin`. **HIL on nomad-b — the strongest yet:** live `/api/acars` honest-empty
on the real radio, 7-way arbitration both directions, observe + predict fit at a forced 800x480
(Next=423, Commit=474), AND the full decode→parse→API→JS→render chain **proven with REAL data** by
feeding acarsdec's own `test.wav` through the real decoder (12 genuine ACARS messages decoded and
rendered). **The decode unit (M13-M16) is complete.**

**M15 (POCSAG) DONE and landed (observed, 2026-10-28), squash-merged to main.** Continues Unit 5
(Decode): POCSAG pager traffic on 929.6 MHz is a half-second FSK burst on the waterfall but a
complete unencrypted message decoded — a cap code (one pager's address) and a line of text/digits.
The lesson pairs the capability with the ethics (can vs. may — receiving is passive, but acting on or
divulging a private message often is not). New `sigint101/pocsag.py`: `parse_pocsag_line` (the
`POCSAG<baud>: Address: <n>  Function: <f>  Alpha:/Numeric:` format from multimon-ng's `pocsag.c`;
dash-address + tone-only pages handled), `PageLog` (bounded newest-first history — pages are one-off
events, not beacons), `PocsagDecoder` (rtl_fm|multimon-ng pipeline, reader thread, release/resume) —
a **6th SDR-exclusive consumer**. `/api/pocsag` + a `pocsag` panel reusing the decoder list
(`static/pocsag.js`) + `lesson_packs/15-pocsag.yaml`. Reuses the already-installed `multimon-ng`.
**HIL on nomad-b:** live `/api/pocsag` runs honest-empty, the 6-way arbitration works both directions,
observe log + predict gate render at a forced 800x480 (Next=423, Commit=474; predict trimmed from the
start). **Deferred (physical):** live capture needs a pager-band antenna + traffic in range.

**M14 (APRS) DONE and landed (observed, 2026-10-28), squash-merged to main.** Continues Unit 5
(Decode): amateur APRS packet on 2m (144.390) is a half-second blip on the waterfall but a fully
framed AX.25 packet decoded — callsign, digipeater path, and a payload (position / message / status).
New `sigint101/aprs.py`: `parse_aprs_line` (strips multimon-ng's `APRS: ` prefix), `parse_aprs_frame`
(TNC2 → source/dest/path/info + classifies the info field: uncompressed position → decimal lat/lon,
message → addressee+text, status), `StationRoster` (latest packet per callsign), `AprsDecoder` (owns
the rtl_fm|multimon-ng pipeline, reader thread, release/resume) — a **5th SDR-exclusive consumer** in
the turn-taking. The TNC2 format + prefix taken from multimon-ng's `hdlc.c`. `/api/aprs` + an `aprs`
panel reusing the decoder list (`static/aprs.js`) + `lesson_packs/14-aprs.yaml`. `multimon-ng`
installed via apt (covers M15 POCSAG too). **HIL on nomad-b:** live `/api/aprs` runs honest-empty, the
5-way dongle arbitration works both directions (multimon-ng ⇄ sweeper), observe roster + predict gate
render at a forced 800x480 (Next=423, Commit=474). **Deferred (physical):** live capture needs a 2m
antenna — nomad-b decoded 0 packets on 144.390 in 50s.

**M13 (ADS-B) DONE and landed (observed, 2026-10-28), squash-merged to main.** Opens Unit 5 (Decode):
aircraft broadcast identity + position in the clear on 1090 MHz, and the console decodes the Mode S /
ADS-B into contacts placed on a **radar scope** (MC's call over a tile map — fully offline, no map
assets) by range (ring) and bearing (angle) from the receiver. New `sigint101/adsb.py`:
`parse_sbs_message` (dump1090 BaseStation lines — field layout + 'H' geometric-altitude suffix + -1/0
flags taken from dump1090-fa's `net_io.c`), `AircraftTracker` (per-ICAO accumulator, prunes stale),
`great_circle` (haversine range + bearing), `AdsbDecoder` (owns the dump1090 process + SBS socket on
port 30003, reader thread, release/resume) — a **4th SDR-exclusive consumer** joining the sweeper /
rtl_433 / rtl_fm turn-taking. `/api/adsb` + an `adsb` instrument panel (inline-SVG radar in
`static/adsb.js`) + `lesson_packs/13-adsb.yaml`. `dump1090-fa` built from source on nomad-b (needs
`librtlsdr-dev` + `libncurses-dev`; `make BLADERF=no`) and installed to `/usr/local/bin`. **HIL on
nomad-b:** live `/api/adsb` runs end to end honest-empty, the 4-way dongle arbitration works both
directions (dump1090 ⇄ sweeper), observe scope (LIVE, honest-empty) + predict gate render at a forced
800x480 (Next=473, Commit=474). **Deferred (physical):** live aircraft capture needs a 1090 MHz
antenna — nomad-b's is deaf there (0 CRC-valid of 411k preambles).

**M12 (RDS/PSK) DONE and landed (observed, 2026-10-27), main @ `e390c5c`.** PSK hides data in phase,
which the waterfall (amplitude × frequency) can never show; M12 reads it through RDS — the station
name/RadioText on an FM 57 kHz subcarrier — with the redsea decode panel (a third SDR-exclusive tool
sharing the dongle via release/resume). `redsea` built from source on nomad-b (installed to
/usr/local/bin) and proven on real RDS (its own test MPX); `sigint101/rds.py` TDD'd against that real
redsea JSON. **HIL on nomad-b:** the live `/api/rds` pipeline runs end to end (honest-empty), observe
+ predict render at a true 800x480. **Deferred (physical):** the live RDS *decode* needs a proper FM
antenna — nomad-b's reception can't recover the subcarrier (no station decodes; same as M4).

**M11 (LoRa) DONE and landed (observed, 2026-10-27), main @ `7a8015d`.** LoRa decodes below the noise
floor by chirp processing gain; M11's instrument is the Nomad's own meshtastic radio (MC's call: a
decode panel like M10), read over meshtasticd's TCP API 4403 — a separate radio from the SDR, so no
contention. New `sigint101/lora.py` (parse + LoRaListener), `/api/lora`, a `lora` instrument panel +
`static/lora.js`; only genuine air receptions (carrying rxSnr) are shown. `meshtastic` added to
requirements and installed on both venvs. **HIL-verified end to end on nomad-b:** node B (an M5
Cardputer Meshtastic node on `/dev/ttyACM0`, driven via SerialInterface.sendText) transmitted three
texts, the Nomad decoded all three on `/api/lora` with real SNR, and the observe stage renders at a
true 800x480. **The below-floor payoff is captured too:** with the antenna off, around a corner, and
fist-held, node A decoded packets at **−4.75 to −5.25 dB SNR** — genuinely below the noise floor,
highlighted on the panel. nomad-b is cleaned up and back on main.

**M10 (FSK) DONE and landed (observed, 2026-10-27), main @ `c72a395`.** FSK's two tones sit below the
waterfall's 125 kHz bins, so M10 reads the band with the rtl_433 decoder instead of the eye (MC's
call). New `sigint101/decode.py` (a Decoder sibling of Sweeper) owns one `rtl_433 -F json` process;
`/api/decode` arbitrates the single dongle with the sweeper (release/resume); a `decode` panel
replaces the waterfall, driven by `static/decode.js`. **HIL-verified on nomad-b at a forced 800x480:**
the live panel listed a real ambient Acurite-Tower sensor (**18.2 °C, 51 % RH, mod ASK**), and M10 +
M9 predict both land Commit on-screen. Also fixed a systemic predict overflow (the eyebrow reprinted
the concept on predict) — see learnings; `rtl_433` is now installed on nomad-b.

**M9 live carrier-blink CAPTURED (observed, 2026-10-27).** The Flipper USB blocker was the
charge-only cable — MC swapped to a data cable and it enumerates (`0483:5740`, `/dev/ttyACM0`,
fw 1.4.3, region US). The droplet drove `subghz tx 0x00F7F7F7 433920000 350 60 0` on the Flipper
while nomad-b's SDR swept the 433.92 band: carrier reached **+22 dB above the noise floor** at the
433.92 bin (API), and the live `/lesson/9` observe waterfall rendered the carrier present ("LIVE·14")
and then, after keying stopped mid-capture, the quiet rows pushing in over it ("LIVE·24") — the
carrier that vanishes, on real hardware. Caveat: the near-field Flipper overloads the RTL front end,
so the lit rows smear broadband instead of a clean 433.92 column (honest overload + OOK splatter, not
invented signal); a crisp column needs the Flipper moved ~1 m off the whip (physical). Screenshots
delivered to MC. Full render recipe for a LIVE dynamic panel (stdlib-WebSocket DevTools capture with
a real-time wait — `--virtual-time-budget` snapshots an empty waterfall) in `.claude/learnings.md`.

**This session landed (11 features, all squash-merged; HIL where live):** Phosphor restyle + predict
fit (`a8bfef8`), units→lessons nav (`0e2fc54`), parser bool guard (`ca3ba48`), comrade voice
(`d2d4ae9`), M5 legal lesson (`bceb085`), exam study card (`aa2cc5a`), instrument-stage fit + gain/SNR
meter (`592bf84`), provenance panel (`53b0e79`), ppm panel (`cdbfb53`). All three instrument panels
(gain/provenance/ppm) built + HIL-verified on nomad-b (gain SNR 34 dB; provenance HEALTHY; ppm honest
no-pilot). main @ `fe16b0b`.

**Blocked on MC (physical, not code):** nomad-b recovers no FM stereo pilot — the strongest local
station (96.0 MHz, 17 dB) measured only a **1.5 dB pilot ratio** (<15 needed), so M4's real-ppm
measurement + the ppm-correction feature await the kit antenna / a known strong *stereo* station.

The units nav: home = 6 unit tiles (U1 Instrument .. U6 Under the Hood), `/unit/{id}` lists that
unit's loaded lessons, unbuilt units show an honest "no lessons here yet" (no lock, D16). Unit map
is `sigint101/units.py` (U1=M1-4, U2=M5, U3=M6-7, U4=M8-12, U5=M13-16, U6=M17-18). A new lesson pack
only shows up once its id is in that map — a contract test enforces every pack lands in a unit.
**Counter-surveillance (Rayhunter/Flock) is a separate track, NOT one of the 6 units** — surface it
as a 7th track tile only if MC asks.

## Render-verify recipe (no browser on the droplet — render on nomad-b's Chromium)
From the feature worktree (or main for a read-only render):
```
mkdir -p data
# Start the server via the HARNESS background runner (run_in_background), NOT `nohup ... &` — a
# nohup child dies when the Bash tool call returns (see learnings). Then on nomad-b (reaches the
# droplet over tailnet; user spec5 on host spec5b = nomad-b; 800x480 = kiosk size):
ssh spec5@100.108.20.20 '/usr/bin/chromium --headless=new --no-sandbox --disable-gpu \
  --hide-scrollbars --force-device-scale-factor=1 --window-size=800,480 --virtual-time-budget=6000 \
  --screenshot=/tmp/s.png http://100.92.112.54:8008/lesson/1'
scp spec5@100.108.20.20:/tmp/s.png <scratch>/   # then Read it
```
- `/` = home (tiles); `/lesson/1` = FRAME stage (eyebrow ☭ + Russo title + Back).
- **To render a hidden instrument stage (demo/try/observe)** — e.g. to check a panel or the copy fit —
  POST a prediction first so a sealed stage becomes the visible one, then screenshot `/lesson/N`
  directly (no iframe): `curl -X POST "http://127.0.0.1:8008/api/predict?lesson_id=N" -d
  "prediction=<any choice key>" -H "content-type: application/x-www-form-urlencoded"` → observe shows.
- **A LIVE panel (gain/provenance/ppm, the waterfall) needs real data → run the server ON nomad-b**,
  not the droplet (no SDR there). Full HIL recipe (checkout the branch at `/home/spec5/sigint-101`,
  hold the ssh open running uvicorn, warm the sweep, render localhost, restore) is in `.claude/learnings.md`.
  **For a LIVE DYNAMIC panel that must show accumulated real data (a lit waterfall), `--virtual-time-budget`
  does NOT work — it snapshots an empty "WAITING" panel because it races the real async sweeps. Use the
  stdlib-WebSocket `Page.captureScreenshot` + real-`sleep` recipe in `.claude/learnings.md` instead, and
  keep the signal live DURING the chromium session (the page re-tunes and clears the buffer on load).**
- **For an 800x480 FIT claim, FORCE the viewport** — headless `--window-size=800,480` lays out in a
  shorter viewport (`innerHeight` ~337), so a fit checked there is a lie. Drive DevTools
  `Emulation.setDeviceMetricsOverride {width:800,height:480,deviceScaleFactor:1}` first, then assert the
  Commit/Next button's `getBoundingClientRect().bottom <= 480`. (This is how M9's predict overflow hid.)
- The **predict** screen is JS-navigated (not a static shot). Best way to render it faithfully:
  write a throwaway `sigint101/static/_predictview.html` that loads `/lesson/1` in a same-origin
  iframe and flips the visible `.stage-panel` to the one with `data-stage="predict"` (the predict
  panel is server-rendered into the page, just `hidden`) — that screenshots the real production DOM.
  Add `getBoundingClientRect().bottom` of `.btn-commit` to confirm it is <=480. **Delete it before
  committing.** (Lesson YAML is cached at server start — restart uvicorn after editing a pack.)
- Kill the server after: `pkill -f "[u]vicorn.sigint101"` (bracket avoids self-match). Note: a
  `nohup ... &` from a Bash tool call dies when the call returns — run the server via the harness
  background runner (run_in_background) instead.

## What the restyle contains (landed on main @ a8bfef8)
- **`sigint101/static/app.css`** rethemed to the "Phosphor Terminal" (MC picked direction A of three):
  PT Mono everywhere (self-hosted OFL woff2 in `static/fonts/`, full Cyrillic, offline);
  double-line box-drawing borders; scanline+vignette+glow in the phosphor themes; blinking block
  cursor on the live control. Three themes keep their jobs: **dark** = green P1 CRT, **sunlight** =
  high-contrast dark-green-on-cream no-glow, **red-night** = red-on-black only (state by
  brightness+glyph+word). `data-theme` values are **dark / sunlight / red-night**.
- **Russo One** (OFL, self-hosted) on the large titles only (statusbar title, stage-panel title,
  home tile names) — legible Soviet-industrial display, not fake-Cyrillic.
- **Frozen-context predict frame** (MC's taste call): a dimmed, data-free instrument outline on the
  predict stage — orients to the band without revealing the answer; no canvas, nothing invented.
  Predict chips are a horizontal auto-fit grid.
- **☭ (U+262D)** on every section-label eyebrow (CSS `content`) and the predict question (template).
- **"comrade"** in the predict chrome copy (hint + committed note) — fuller lesson-copy pass pending.
- **Bug fixed** (found by the first real render): hidden stage-panels overlapped because
  `.stage-panel{display:flex}` beat the UA `[hidden]` rule — added `.stage-panel[hidden]{display:none}`.
- **Predict-fit fix** (this session): the predict prompt doubles as its `<h1>` stage title, so at
  800x480 the long question pushed the chips + Commit button ~200px off-screen (Commit unreachable).
  Fixed per MC's calls — (a) trimmed lesson 1's predict prompt to its setup + question, (b) held the
  predict title at the 18px floor (predict-scoped `font-size`), (c) reclaimed chrome height from the
  statusbar (`--bar-h` 72→66) and stage rail (`--rail-h` 48→40). Commit button now lands at y=478.
  Tradeoff MC accepted: the Back button is now 66px tall (just under the 72px touch floor).
- Captions raised to >=18px. **Verified:** `make selftest` green (290 tests, 94% cov, pristine);
  `make lint` clean; home + FRAME + PREDICT render clean on nomad-b at 800x480; Russo One (Latin +
  Cyrillic) loads; ☭ renders as a glyph. Preview of all three directions (what MC chose from):
  https://claude.ai/artifact/1MnyjjtpDXizFRZVnDnzNh

## Both owner gates are CLEARED
- **Tailnet SSH open:** acls `tag:dev -> tag:fleet:22` added; droplet key authorized on all three
  fleet hosts. Droplet can SSH spec5, pi5, nomad-b.
- **Taste answers:** waterfall-with-no-radio = badged SAMPLE recording; home nav = units→lessons→
  lesson (not yet built — see pending); predict = frozen context frame (built); console look =
  Phosphor Terminal (LANDED @ a8bfef8).

## HIL targets (MC's call) — nomad-b is PRIMARY
- **nomad-b** (hostname `spec5b`, `100.108.20.20`, user `spec5`) = PRIMARY acceptance target: stock
  Nomad 2 Recon twin, built-in RTL2832U + working antenna, MeshAdv HAT serial (`/dev/serial0`,
  `/dev/ttyAMA0`), working LoRa jack. **P0 acceptance PASSED** here: live `/api/hardware` +
  `/api/spectrum` (real FM sweep, ~96 MHz, ~29 dB SNR), pages 200, clean dongle release.
- **spec5** (`100.90.68.20`) = degraded secondary: **busted RX antenna + dead LoRa jack**. Useful
  as a "deaf receiver" fixture for the M3 deaf-vs-empty lesson.
- **pi5** (`100.126.132.9`, user `mc`) = Flipper target, but **no Flipper attached** (it has an
  ESP32 / Espressif USB-JTAG on `/dev/ttyACM1` — good for the later counter-surveillance track).
- Dev on the droplet (D14); these are acceptance targets over SSH. **No browser on the droplet** —
  render on nomad-b's Chromium.
- **ppm (M4):** panel LANDED (`cdbfb53`); `ppm.py` + the panel honesty verified on hardware
  (refuses a ppm with no clear pilot). Happy path still blocked — **nomad-b recovers no FM stereo
  pilot**: a fresh HIL measured even the strongest local station (96.0 MHz, 17 dB) at only a
  **1.5 dB pilot ratio**, far below the 15 dB needed. This is reception, not code — MC's physical
  call (kit antenna / a known strong *stereo* station). The real-ppm measurement awaits a pilot.

## Where the build stands
- Repo: github.com/wittyreference/sigint-101 (public, MIT), main @ `8ed4c3e`. Stack: FastAPI +
  Jinja2 + polling JS + offline CSS + SQLite + JSONL events, Pi 4 / Bookworm / aarch64, Chromium kiosk.
- 13 modules (added `units.py`); console serves the shell end-to-end (commit-before-reveal server-side; /api/spectrum
  honest 503). 9 lesson packs (M1-M9); a pack can declare its own tune band (`tune:`) so the
  instrument starts off the FM default. Exam trainer backend on the verified NCVEC
  2026-2030 pool (409 + errata), reachable from home. `make selftest` = 361 tests + 17 node (JS) tests, 94%, pristine; `make lint` clean.

## Pending work
- Commit the 3 NCVEC diagram JPGs (T-1/T-2/T-3) so figure questions in the exam show their diagram
  (today they read "not shown here yet").
- Capture M9's live carrier-blink (needs a 433.92 MHz OOK source transmitting). Flipper not
  enumerating over USB on nomad-b (no /dev/ttyACM*, lsusb lacks 0483:5740 — likely a charge-only
  cable); fix it to drive its TX over the CLI for an automated blink capture.
- Remaining lessons M10-M18 + decode backends (M10 FSK needs `rtl_433` on nomad-b — not installed,
  a new system install); counter-surveillance (Flock ESP32 on pi5 + Rayhunter ingest);
  kiosk/provisioning/updater; Flipper Lesson Companion FAP (Pulse Scope, own worker track).

## Gotchas (learned this session)
- All `sigint101/` edits go through a worktree (worktree-gate). Docs (STATUS/CLAUDE.md) are outside
  the gate and land on main. Merge with **--squash**. Stage files by name. Never `--no-verify`.
  Git identity must be **Michael Carpenter <mc8805@outlook.com>** (repo-local, hook-enforced).
- **Worktree setup:** `git worktree add ../sigint-101-<name> -b feat/<name>`, then
  `ln -s /home/mc/workspaces/sigint-101/.venv <worktree>/.venv` and `mkdir <worktree>/data`.
  No editable install — `pytest.ini` has `pythonpath = .`, so running `.venv/bin/pytest` FROM the
  worktree imports the worktree's `sigint101/` (not main's). `.venv`/`data/` are gitignored; never
  `git add` the symlink. Lesson YAML is cached at server start — restart uvicorn after editing a pack.
- Shell is **zsh**: unquoted `$var` does NOT word-split (`set -- $x` / `$NB cmd` break) — inline or use arrays.
- `pkill -f "pattern"` matches its own command line — use the bracket trick `"[p]attern"`.
- Render on nomad-b (no Chrome on the droplet). `rtl_power` "[R82XX] PLL not locked!" + "User
  cancel, exiting" are benign (appear on healthy nomad-b too).
- **☭ (U+262D) renders via a SYSTEM-FONT FALLBACK, not our bundled fonts.** The PT Mono @font-face
  `unicode-range` excludes U+262D, so the glyph comes from whatever monospace the renderer falls
  back to (DejaVu Sans Mono on nomad-b, which has it). This works on nomad-b but is NOT guaranteed
  on the actual kiosk Pi if its fontconfig lacks a glyph for U+262D — it would tofu. **TODO before
  kiosk ship:** confirm U+262D on the real kiosk image, or bundle an inline SVG hammer-sickle.
- **Predict is the tightest screen at 800x480.** Keep predict prompts SHORT (~35-40 words): the
  prompt is the stage `<h1>`, and chips (72px touch) + Commit + frozen frame leave little room. New
  lessons' predict copy should be written to fit; the content pass should check each one renders.

## Done / verified
- Logic (droplet): 361 tests green (+ 17 node JS tests), 94%, pristine.
- M9 "The Carrier That Vanishes" (OOK) + per-lesson tune band LANDED (`8ed4c3e`): a pack can
  declare `tune: {center_hz, span_hz, step_hz?}`; lessons.py validates it against the tuner reach +
  sweep limits at load, the lesson route points the dial/step at it, and waterfall.js POSTs the band
  to /api/tune once on load (packs with no band keep the FM default — the 8 existing lessons are
  untouched). M9 is a six-stage intro tuned to 433.92: a keyed OOK source blinks the carrier fully
  to the noise floor; message = pattern of bursts/gaps; two-view idea (waterfall=frequency, Flipper
  Read RAW=time). TDD throughout (band 6, route 2, render 2, parseTuneBand 2 node, M9 content 5).
  **HIL-verified on nomad-b at 800x480**: the live sweep retunes to 432.92-434.92 MHz on the real
  SDR (dial 433.920, 20 kHz step); frame, predict (41-word prompt fits, 3 chips + Commit on screen),
  observe all render clean; the band shows honest-empty with nothing keying. **Not yet captured:**
  the live carrier-blink (needs a 433 OOK source transmitting — see NEXT ACTION), like M4's deferred
  happy-path. Also unblocks later non-FM lessons (ADS-B 1090, GPS).
- Exam-nav LANDED (`84bd0b1`, MC-reviewed): the licence exam trainer now has a home affordance —
  a full-width CTA under the unit grid ("Licence exam / Practice for your Technician ticket →")
  that lands on `/exam` (which links on to the 35-Q practice exam). MC's call: a full-width bar
  below the grid, not a 7th tile — the trainer is a cross-cutting licence track, not a curriculum
  unit, so it reads apart (accent double border, inline-SVG diamond glyph, no U-code/progress bar).
  TDD (1 route test + 1 template test). **HIL-verified on nomad-b at 800x480** (dark theme): grid +
  CTA fit with no overflow, both tile rows intact, glyph + arrow render. red-night/sunlight inferred
  (shared theme tokens, no state-by-colour). `/exam` returns 200.
- Exam-sim (scored 35-question practice exam) LANDED (`32ec4a0`, MC-reviewed): `/exam/sim` draws a
  35-Q sheet (one per NCVEC group) and serves the current question with a running score; answering
  reveals the right option + the learner's miss (commit-before-reveal) and advances; `/exam/sim/
  results` scores it — PASS/NOT YET at 26/35 (74%), the percentage, and a per-subelement breakdown
  that flags the areas not aced; Retake starts fresh. The in-progress sim persists in a one-row
  `sim` table in exam.db (survives power loss); each answer also feeds the Leitner store, so a sim
  sharpens the study card. MC's call: answer-then-advance, not all-35-then-score. TDD (sim store +
  routes + templates). **HIL-verified on nomad-b at 800x480**: the question card, the review (correct
  marked, miss flagged), and the results screen (13/35 NOT YET, pass line, 10-cell subelement grid)
  all render clean, Back always visible. Study card links to the sim; neither is on home yet (pending).
- Instrument-lesson copy trim + eyebrow fit LANDED (`85d126d`, MC-reviewed): trimmed the demo/try/
  observe narration (and over-long predict prompts) on M1/M3/M4/M6/M7/M8 the way M2 was, 20-43% off
  each demo, every teaching beat kept. Render-verify on nomad-b exposed the real constraint — the
  instrument narration is a thin scroll band (waterfall 120px + tune + title eat ~480px), and the
  eyebrow reprinted the full lesson `concept` on EVERY stage, leaving only ~27px on a radio lesson.
  MC's call: drop the concept from the eyebrow on the hardware instrument stages (demo/try/operate/
  observe) — it already rode the frame; text/no-hardware stages keep it. HIL-verified: the M7
  narration band grew 27px -> 65px (one line -> ~two) across every instrument stage. MC chose NOT to
  gut the dense demos, so M6/M7/M8 still scroll by design (content-faithful, like the shipped M2).
  TDD for the template change (2 eyebrow tests). 327 tests green.
- Units→lessons nav LANDED on main (`0e2fc54`): home shows 6 unit tiles, `/unit/{id}` lists a unit's
  lessons, unbuilt units render an honest empty page (no lock). Render-verified on nomad-b at 800x480
  (home, /unit/1 Instrument, /unit/5 Decode empty); 304 tests green, lint clean. TDD throughout.
- Lesson-pack loader hardened (`ca3ba48`): `parse_lesson` now refuses a predict choice key/label
  that YAML coerced to a bool (unquoted yes/no/true/false), so an authoring slip fails loud at load
  instead of becoming a silent "True"/"False" chip. TDD; 304 tests green.
- Light-touch "comrade" voice pass LANDED (`d2d4ae9`): one address at the frame opening + the
  "your turn" beat of all 7 packs; teaching copy + predict prompts untouched. MC's call: light, not
  full persona. Render-verified lesson 2 FRAME on nomad-b. (Fuller lesson copy comes with new lessons.)
- M5 "The One Rule" legal lesson LANDED (`bceb085`, MC-reviewed): U2's lesson, no-RF; listening is
  broad/lawful, transmitting is licensed (D16 framing, jurisdiction caveat). Shipped with a general
  console fix — a no-hardware lesson renders as text with a chips-only predict (gated on
  `lesson.needs_hardware`). TDD; render-verified M5 frame + predict on nomad-b. Unit 2 is no longer
  empty in the nav.
- Exam trainer study card LANDED (`aa2cc5a`, MC-reviewed): `/exam` serves the weakest spaced-
  retrieval card; tap + commit → `/exam/review` marks the correct option (bright + check) and a
  wrong pick (dashed + cross), with the FCC ref. Reuses the predict-chip pattern + the Leitner
  store. TDD; render-verified on nomad-b. Exam-sim mode + results screen + 3 diagram JPGs deferred.
- Gain/SNR meter (M2) + instrument-stage fit LANDED (`592bf84`, MC-reviewed): a vertical level meter
  (floor/peak lines, shaded SNR gap) beside the waterfall, live off the same sweep. The demo/try/
  observe stages now fit 800x480 — Next-step on the tune strip, scrolling narration (they were
  over-subscribed and never render-verified). **HIL-verified on nomad-b**: the live meter read
  PEAK -14.7 / FLOOR ~-49 / SNR 34.0 dB off a real FM sweep.
- ppm panel (M4) LANDED (`cdbfb53`, MC-reviewed): measure-on-demand — the Measure button captures
  the tuned station's 19 kHz pilot via `/api/ppm/measure` and shows the crystal error, or an honest
  "no clear pilot". HIL-verified on nomad-b: the measurement runs and honestly refuses (no
  recoverable pilot there yet — see HIL targets). All three instrument panels (gain/provenance/ppm)
  are now built.
- Provenance panel (M3) LANDED (`53b0e79`, MC-reviewed): a verdict-first panel (HEALTHY/DEGRADED/
  NO BASELINE/NO RADIO) off the live reference vs. a captured baseline, with a Capture button; new
  `/api/provenance` + `/api/provenance/baseline` routes. **HIL-verified on nomad-b**: HEALTHY off a
  real sweep (REF -13.2 vs base -12.5, temp 37C, power OK).
- Real hardware (nomad-b): P0 acceptance passed; spectrum.py + ppm.py honesty verified on real SDR.
- Phosphor-Terminal restyle LANDED on main (`a8bfef8`): home + FRAME + PREDICT render-verified on
  nomad-b at 800x480; Russo One (Latin + Cyrillic) loads; ☭ renders as a glyph (via system
  fallback — see Gotchas); predict fits with the title at 18px; tests 290 green/94%, lint clean.
