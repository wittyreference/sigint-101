# ABOUTME: Loads and validates SIGINT 101 lesson packs (YAML) into Lesson objects.
# ABOUTME: A lesson pack is the unit of curriculum; the console renders one at a time.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .spectrum import (
    MAX_SPAN_HZ,
    MIN_SPAN_HZ,
    TUNER_MAX_HZ,
    TUNER_MIN_HZ,
    TuneError,
    validate_span,
)

# A pack declares its shape with `type`. An intro lesson teaches a concept from a blank
# slate, so it opens with frame/demo/try before the learner predicts; a consolidation
# lesson drills a concept the learner has already met, so it goes straight to predict.
# Each type fixes the stages a pack of that type must define, in the order the console
# walks them. A pack that omits `type` is a consolidation pack: that four-stage shape is
# the one every pack had before intro lessons existed, so the default keeps them loading.
DEFAULT_TYPE = "consolidation"
REQUIRED_STAGES = {
    "intro": ("frame", "demo", "try", "predict", "observe", "explain"),
    "consolidation": ("predict", "operate", "observe", "explain"),
}

# The predict stage is the commit point of the teaching loop. Everything the console would
# otherwise reveal — the later stages, the tiered hints, the quiz, the captured waterfall
# artifact — stays sealed until the learner has written a prediction down. Both pack shapes
# place predict partway through the walk, so the seal is "every stage after predict" rather
# than a fixed list, which is why it is derived from each lesson's own order below.
COMMIT_STAGE = "predict"

# The post-lesson check: an optional, scored stage a pack adds with a `check:` block. It walks
# after explain, so it is past the predict gate and sealed with the rest of the reveal. It poses one
# new situation on the lesson's concept (near transfer), never the predict re-asked; unlike the
# predict, which is a guess before the teaching, the check is scored, because it measures whether
# the teaching landed.
CHECK_STAGE = "check"

# What a predict asks about, which sets how the explain names its answer. An instrument predict
# asks what the radio will show or do, so the reveal reads "What the radio does"; a knowledge
# predict asks what is true (the law, what a detection proves, what you need), so it reads "In
# fact". A lesson that needs a radio is an instrument predict unless its pack says
# `predict_kind: knowledge`; a lesson with no radio is a knowledge predict.
REVEAL_LABELS = {"instrument": "What the radio does", "knowledge": "In fact"}

# Panels the console can actually render. The waterfall and tune panel are the baseline
# every lesson gets; a pack turns extras on by name. Anything else refuses to load,
# because a panel that silently rendered nothing would fail mid-lesson, in front of
# the learner. "decode" and "lora" are the odd ones out: each is not an overlay on the waterfall
# but a different instrument (rtl_433, and the LoRa radio), and each REPLACES the waterfall, so an
# instrument panel cannot share the screen with the gain/provenance/ppm overlays — or with each
# other (there is one instrument per stage).
OVERLAY_PANELS = ("gain", "provenance", "ppm")
DECODE_PANEL = "decode"
LORA_PANEL = "lora"
RDS_PANEL = "rds"
ADSB_PANEL = "adsb"
APRS_PANEL = "aprs"
POCSAG_PANEL = "pocsag"
ACARS_PANEL = "acars"
AIS_PANEL = "ais"
DMR_PANEL = "dmr"
APT_PANEL = "apt"
SSTV_PANEL = "sstv"
FLOCK_PANEL = "flock"
RAYHUNTER_PANEL = "rayhunter"
FLIPPER_PANEL = "flipper"
INSTRUMENT_PANELS = (DECODE_PANEL, LORA_PANEL, RDS_PANEL, ADSB_PANEL, APRS_PANEL,
                     POCSAG_PANEL, ACARS_PANEL, AIS_PANEL, DMR_PANEL, APT_PANEL, SSTV_PANEL,
                     FLOCK_PANEL, RAYHUNTER_PANEL, FLIPPER_PANEL)
KNOWN_PANELS = (*OVERLAY_PANELS, *INSTRUMENT_PANELS)

# The tune buttons step by this much when a pack declares a band but not its own step. A
# lesson on a narrow signal can ask for a finer step; 100 kHz is the FM-band default.
DEFAULT_TUNE_STEP_HZ = 100_000

# What a lesson tile says the learner must bring, keyed by the pack's `hardware:` names. The
# RTL-SDR is built into the Recon, so it earns no badge. LoRa's radio is built in too, but its
# lessons need a second node to talk to, so that is the badge.
BUILT_IN_HARDWARE = ("rtl-sdr",)
HARDWARE_BADGES = {
    "wifi-monitor": "Wi-Fi adapter",
    "orbic-rayhunter": "Orbic",
    "lora": "2nd node",
    "flipper": "Flipper",
}


class LessonError(ValueError):
    """Raised when a lesson pack is missing required fields or is malformed."""


class RevealLockedError(RuntimeError):
    """Raised when post-predict content is requested before a prediction is committed.

    The console must commit the learner's prediction before it asks a lesson for the
    reveal. This is the code-level backstop for the predict -> operate/observe -> explain
    gate: a caller that forgets the order fails loudly here instead of quietly handing the
    learner the answer they were about to guess.
    """


@dataclass(frozen=True)
class Lesson:
    """One lesson, loaded from a YAML pack.

    Frozen because a lesson is curriculum, not state: nothing at runtime should be able
    to quietly rewrite what the learner was asked to do.
    """

    id: int
    slug: str
    title: str
    concept: str
    type: str
    requires_tx: bool
    hardware: list
    stages: dict
    hints: list
    quiz: list
    artifact: str
    panels: list
    choices: list
    tune: dict | None
    answer: tuple = ()
    verdict: str = ""
    check: dict | None = None
    predict_kind: str = "instrument"

    @property
    def reveal_label(self) -> str:
        """How the explain header names the answer: what the radio does, or a plain fact."""
        return REVEAL_LABELS[self.predict_kind]

    def check_feedback(self, key: str) -> dict:
        """Whether a check answer was the key, with the chosen chip's and the key's label and why.

        The screen shows the chosen why, and the key's why too when they differ, so a miss is
        answered with the reason, not just the right letter.
        """
        by_key = {choice["key"]: choice for choice in self.check["choices"]}

        def _side(k):
            return {"label": by_key[k]["label"], "why": by_key[k]["why"]}

        answer = self.check["answer"]
        return {"correct": key == answer, "chosen": _side(key), "key": _side(answer)}

    def is_correct(self, key: str):
        """Was this committed chip key right? None when the pack names no answer to score."""
        if not self.answer:
            return None
        return key in self.answer

    def score(self, key: str):
        """What the learner said and what the answer was, by label, for the explain header.

        None when the pack names no answer. A key the pack never offered is echoed back as-is
        rather than dropped, so the header still says what was committed.
        """
        if not self.answer:
            return None
        labels = {choice["key"]: choice["label"] for choice in self.choices}
        return {
            "said": labels.get(key, key),
            "answer": [labels[k] for k in self.answer],
            "correct": self.is_correct(key),
            "verdict": self.verdict,
        }

    @property
    def needs_hardware(self) -> bool:
        """Does this lesson require a radio to be attached before it can be worked?"""
        return bool(self.hardware)

    @property
    def hardware_badges(self) -> list:
        """The kit this lesson needs beyond the built-in radio, as the learner should read it.

        Shown on the lesson tile so a need is known before the lesson starts, not mid-demo. A name
        with no label is shown as written rather than dropped: an unlabelled need is still a need.
        """
        return [HARDWARE_BADGES.get(name, name)
                for name in self.hardware if name not in BUILT_IN_HARDWARE]

    @property
    def uses_decoder(self) -> bool:
        """True when the instrument is rtl_433 (the decode panel) instead of the waterfall.

        The two cannot run at once on one dongle, so this is how the console knows to render
        the decode panel and poll /api/decode rather than the sweep.
        """
        return DECODE_PANEL in self.panels

    @property
    def uses_lora(self) -> bool:
        """True when the instrument is the LoRa radio (the lora panel) instead of the waterfall.

        It reads meshtasticd, not the SDR, so the console renders the lora panel and polls
        /api/lora. The LoRa radio is separate, so there is no contention with a sweep.
        """
        return LORA_PANEL in self.panels

    @property
    def uses_rds(self) -> bool:
        """True when the instrument is the RDS decoder (the rds panel) instead of the waterfall.

        It runs rtl_fm|redsea on the SDR, so the console renders the rds panel and polls /api/rds;
        like the rtl_433 decoder it takes turns with the sweep on the one dongle.
        """
        return RDS_PANEL in self.panels

    @property
    def uses_adsb(self) -> bool:
        """True when the instrument is the ADS-B radar (the adsb panel) instead of the waterfall.

        It runs dump1090 at 1090 MHz on the SDR, so the console renders the radar scope and polls
        /api/adsb; like the other SDR instruments it takes turns with the sweep on the one dongle.
        """
        return ADSB_PANEL in self.panels

    @property
    def uses_aprs(self) -> bool:
        """True when the instrument is the APRS decoder (the aprs panel) instead of the waterfall.

        It runs rtl_fm|multimon-ng on the 2m packet channel, so the console renders the station
        roster and polls /api/aprs; like the other SDR instruments it takes turns with the sweep.
        """
        return APRS_PANEL in self.panels

    @property
    def uses_pocsag(self) -> bool:
        """True when the instrument is the POCSAG decoder (the pocsag panel), not the waterfall.

        It runs rtl_fm|multimon-ng on the pager band, so the console renders the page log and polls
        /api/pocsag; like the other SDR instruments it takes turns with the sweep on the one dongle.
        """
        return POCSAG_PANEL in self.panels

    @property
    def uses_acars(self) -> bool:
        """True when the instrument is the ACARS decoder (the acars panel), not the waterfall.

        It runs acarsdec on the VHF ACARS channel, so the console renders the message log and polls
        /api/acars; like the other SDR instruments it takes turns with the sweep on the one dongle.
        """
        return ACARS_PANEL in self.panels

    @property
    def uses_ais(self) -> bool:
        """True when the instrument is the AIS decoder (the ais panel), not the waterfall.

        It runs rtl_ais on the marine AIS channels, so the console renders the vessel roster and
        polls /api/ais; like the other SDR instruments it takes turns with the sweep on the dongle.
        """
        return AIS_PANEL in self.panels

    @property
    def uses_dmr(self) -> bool:
        """True when the instrument is the DMR/P25 decoder (the dmr panel), not the waterfall.

        It runs dsd-fme on the digital-voice channel, so the console renders the call log and polls
        /api/dmr; like the other SDR instruments it takes turns with the sweep on the one dongle.
        """
        return DMR_PANEL in self.panels

    @property
    def uses_apt(self) -> bool:
        """True when the instrument is the NOAA APT decoder (the apt panel), not the waterfall.

        It runs satdump on a ~137 MHz pass and writes a weather image, so the console renders the
        decoded picture and polls /api/apt, sharing the one dongle like the other SDR instruments.
        """
        return APT_PANEL in self.panels

    @property
    def uses_sstv(self) -> bool:
        """True when the instrument is the SSTV decoder (the sstv panel), not the waterfall.

        It runs rtl_fm|sox|sstv on 2m and writes a picture, so the console renders the decoded image
        and polls /api/sstv, sharing the one dongle like the other SDR instruments.
        """
        return SSTV_PANEL in self.panels

    @property
    def uses_flock(self) -> bool:
        """True when the instrument is the Flock detector (the flock panel), not the waterfall.

        It runs tcpdump on wlan1 in monitor mode, a separate radio from the SDR dongle, so the
        console renders the detections list and polls /api/flock with no contention with the sweep.
        """
        return FLOCK_PANEL in self.panels

    @property
    def uses_rayhunter(self) -> bool:
        """True when the instrument is the Rayhunter monitor (the rayhunter panel), not a waterfall.

        It polls an Orbic running EFF's Rayhunter over WiFi — a separate device, receive-only — so
        the console renders the recording state + warnings from /api/rayhunter, never the SDR.
        """
        return RAYHUNTER_PANEL in self.panels

    @property
    def uses_flipper(self) -> bool:
        """True when the instrument is the attached Flipper (the flipper panel), not a waterfall.

        The console reads the Flipper over its USB serial CLI — a separate device, like the Flock
        detector and the Rayhunter monitor — so it renders the device/capture from /api/flipper and
        never touches the SDR.
        """
        return FLIPPER_PANEL in self.panels

    @property
    def stage_order(self) -> tuple:
        """The stages this lesson walks, in order: fixed by its type, then its check if any."""
        return REQUIRED_STAGES[self.type] + ((CHECK_STAGE,) if self.check else ())

    @property
    def sealed_stages(self) -> tuple:
        """The stages that sit past the predict gate, withheld until a commit.

        Intro and consolidation packs place predict at different points in the walk, so
        the sealed set is read off this lesson's own order rather than hard-coded — the
        one definition that stays correct as pack shapes are added.
        """
        order = self.stage_order
        return order[order.index(COMMIT_STAGE) + 1:]

    def reveal(self, *, prediction_committed: bool) -> dict:
        """Hand back everything sealed behind the predict gate — only once committed.

        Commit-before-reveal is the rule the whole console is built to keep: the
        explanation, the stages after predict, the tiered hints, the quiz and the captured
        artifact (the waterfall a learner produces) stay out of reach until a prediction is
        written down. This refuses rather than returning an empty view, mirroring how the
        console declines to complete a lesson whose prediction was never made, so a
        mis-ordered caller breaks loudly instead of spoiling the answer. The raw `stages`
        map still holds the full curriculum; this method is the gate the console routes the
        reveal through, for both the four-stage and six-stage pack shapes.
        """
        if not prediction_committed:
            raise RevealLockedError(
                f"lesson {self.id}: the reveal stays sealed until a prediction is committed"
            )
        return {
            "stages": {stage: self.stages[stage] for stage in self.sealed_stages
                       if stage != CHECK_STAGE},
            "hints": list(self.hints),
            "quiz": list(self.quiz),
            "artifact": self.artifact,
            "answer": list(self.answer),
            "verdict": self.verdict,
            "check": self.check,
        }


def _require(data: dict, key: str, ctx: str):
    """Fetch a field a pack cannot omit, naming the pack and field if it is missing.

    Treats empty string and empty list as missing too: a lesson with a blank title is
    malformed in the same way as one with no title at all.
    """
    if key not in data or data[key] in (None, "", []):
        raise LessonError(f"{ctx}: missing required field '{key}'")
    return data[key]


def _parse_tune(data: dict, ctx: str) -> dict | None:
    """Validate an optional `tune` band and normalise it to centre/span/step + edges.

    A pack names where its instrument should start — `tune: {center_hz, span_hz, step_hz?,
    bin_hz?}` — because the eight FM-band lessons predate any need to leave 88-108 MHz, and a
    lesson on a signal elsewhere (OOK at 433.92 MHz) cannot be hand-tuned there a hundred kHz at
    a time. The band is checked against what the radio can actually reach and what the sweeper
    can sweep, at load, so an unreachable pack fails here rather than mid-lesson in front of a
    learner. A pack with no `tune` returns None and keeps the default FM sweep. `bin_hz` is the
    sweep's bin size; without one the sweep keeps its FM-sized default bin.
    """
    raw = data.get("tune")
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise LessonError(f"{ctx}: 'tune' must be a mapping of center_hz/span_hz/step_hz")

    def _positive_int(key: str, *, required: bool, default: int | None = None) -> int:
        if key not in raw or raw[key] is None:
            if required:
                raise LessonError(f"{ctx}: 'tune' needs an integer '{key}'")
            return default
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise LessonError(f"{ctx}: 'tune.{key}' must be a positive integer in Hz")
        return value

    center = _positive_int("center_hz", required=True)
    span = _positive_int("span_hz", required=True)
    step = _positive_int("step_hz", required=False, default=DEFAULT_TUNE_STEP_HZ)
    bin_hz = _positive_int("bin_hz", required=False)

    if span < MIN_SPAN_HZ or span > MAX_SPAN_HZ:
        raise LessonError(
            f"{ctx}: 'tune.span_hz' is {span / 1e6:.3f} MHz — the sweep spans "
            f"{MIN_SPAN_HZ // 1000} kHz to {MAX_SPAN_HZ // 1_000_000} MHz"
        )
    low = center - span // 2
    high = center + span // 2
    if low < TUNER_MIN_HZ or high > TUNER_MAX_HZ:
        raise LessonError(
            f"{ctx}: 'tune' band {low / 1e6:.3f}-{high / 1e6:.3f} MHz is outside the radio's "
            f"{TUNER_MIN_HZ // 1_000_000}-{TUNER_MAX_HZ // 1_000_000} MHz range"
        )
    if bin_hz is not None:
        # The same gate /api/tune applies, so a pack can never name a sweep the console refuses.
        try:
            validate_span(low, high, bin_hz)
        except TuneError as exc:
            raise LessonError(f"{ctx}: 'tune.bin_hz' {bin_hz} Hz: {exc}") from exc
    return {"center_hz": center, "span_hz": span, "step_hz": step, "bin_hz": bin_hz,
            "low_hz": low, "high_hz": high}


def _parse_answer(data: dict, choices: list, ctx: str) -> tuple:
    """Validate an optional `answer` (one key or a list) and its one-line `verdict`.

    The answer scores the predict gate, so every key in it must be one of the pack's own choice
    keys: a typo would mark every learner wrong, in front of them, and is caught here instead. A
    list lets a lesson whose explain defends two chips accept either. Answer and verdict travel
    together — a scored gate with no verdict to print, or a verdict with nothing to score, is a
    half-authored pack. Returns (answer tuple, verdict string); () and "" when the pack has none.
    """
    raw_answer = data.get("answer")
    raw_verdict = data.get("verdict")
    if raw_answer in (None, "", []) and raw_verdict in (None, ""):
        return (), ""
    if raw_answer in (None, "", []):
        raise LessonError(f"{ctx}: a 'verdict' needs an 'answer' to score against")
    if raw_verdict in (None, ""):
        raise LessonError(f"{ctx}: an 'answer' needs a one-line 'verdict' for the explain header")
    if not choices:
        raise LessonError(f"{ctx}: an 'answer' needs predict 'choices' to name")
    keys = raw_answer if isinstance(raw_answer, list) else [raw_answer]
    answer = []
    known = [choice["key"] for choice in choices]
    for key in keys:
        # Same YAML 1.1 trap as the choice keys: a bare `no` arrives as False.
        if isinstance(key, bool):
            raise LessonError(
                f'{ctx}: answer {key!r} is a YAML boolean — quote it in the pack (e.g. "no")')
        key = str(key).strip()
        if key not in known:
            raise LessonError(
                f"{ctx}: answer '{key}' is not one of its choice keys ({', '.join(known)})")
        answer.append(key)
    verdict = str(raw_verdict).strip()
    if "\n" in verdict:
        raise LessonError(f"{ctx}: the 'verdict' must be one line")
    return tuple(answer), verdict


def _parse_check(data: dict, ctx: str) -> dict | None:
    """Validate an optional `check:` block: a prompt, tappable choices with a why each, one answer.

    The check is scored in front of the learner, so a half-authored one fails here: every chip needs
    a label to read and a one-line `why` to teach with, and the answer is a single key among the
    chips (a check has one defensible answer; a list would hedge it). Returns None without one.
    """
    raw = data.get("check")
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise LessonError(f"{ctx}: 'check' must be a mapping of prompt/choices/answer")
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        raise LessonError(f"{ctx}: a 'check' needs a non-empty 'prompt'")
    choices_in = raw.get("choices")
    if not isinstance(choices_in, list) or len(choices_in) < 2:
        raise LessonError(f"{ctx}: a 'check' needs at least two 'choices'")
    choices = []
    for choice in choices_in:
        choice = choice if isinstance(choice, dict) else {}
        fields = {}
        for field in ("key", "label", "why"):
            value = choice.get(field)
            # The same YAML 1.1 trap as the predict chips: a bare `no` arrives as False.
            if isinstance(value, bool):
                raise LessonError(f"{ctx}: check choice {field} {value!r} is a YAML boolean — "
                                  "quote it in the pack")
            fields[field] = str(value).strip() if value is not None else ""
            if not fields[field]:
                raise LessonError(f"{ctx}: each check choice needs a non-empty '{field}'")
        if "\n" in fields["why"]:
            raise LessonError(f"{ctx}: check choice '{fields['key']}' 'why' must be one line")
        choices.append(fields)
    answer = raw.get("answer")
    if isinstance(answer, list):
        raise LessonError(f"{ctx}: a check 'answer' is a single key, not a list")
    if isinstance(answer, bool):
        raise LessonError(f"{ctx}: check answer {answer!r} is a YAML boolean — "
                          "quote it in the pack")
    answer = str(answer).strip() if answer is not None else ""
    if not answer:
        raise LessonError(f"{ctx}: a 'check' needs an 'answer'")
    known = [choice["key"] for choice in choices]
    if answer not in known:
        raise LessonError(
            f"{ctx}: check answer '{answer}' is not one of its choice keys ({', '.join(known)})")
    return {"prompt": prompt, "choices": choices, "answer": answer}


def parse_lesson(data: dict) -> Lesson:
    """Turn parsed YAML into a Lesson, refusing anything half-defined.

    Validation is strict on purpose. A pack that loads with a missing stage would fail
    silently in front of a learner, mid-lesson, which is the worst place to discover it.
    """
    ctx = f"lesson {data.get('id', '?')}"

    # A pack's type fixes which stages it must define. An absent or blank type means a
    # consolidation pack — the four-stage shape that predates intro lessons — so older
    # packs keep loading unchanged; an unrecognised type is a typo worth catching now.
    lesson_type = data.get("type") or DEFAULT_TYPE
    if lesson_type not in REQUIRED_STAGES:
        known = ", ".join(sorted(REQUIRED_STAGES))
        raise LessonError(f"{ctx}: unknown lesson type '{lesson_type}' (known: {known})")
    required = REQUIRED_STAGES[lesson_type]

    stages_in = _require(data, "stages", ctx)
    if not isinstance(stages_in, dict):
        raise LessonError(f"{ctx}: 'stages' must be a mapping")
    # Every stage this type requires must be present and non-empty. The teaching loop is
    # the lesson: a pack missing Predict would hand the learner the answer before they
    # committed to a guess, which is the one thing the whole console is built to prevent.
    for stage in required:
        if stage not in stages_in or not str(stages_in[stage]).strip():
            raise LessonError(f"{ctx}: stage '{stage}' is required and must be non-empty")

    # Hints are ordered least to most revealing, so their being a *list* is load-bearing
    # rather than incidental — the order is the gradual release.
    hints = data.get("hints", [])
    if not isinstance(hints, list):
        raise LessonError(f"{ctx}: 'hints' must be a list (tiered, least to most revealing)")

    # Panels are console machinery, so the console must recognise every name a pack
    # asks for — checked at load, which is the last moment before a learner sees it.
    panels = data.get("panels", [])
    if not isinstance(panels, list):
        raise LessonError(f"{ctx}: 'panels' must be a list")
    for name in panels:
        if name not in KNOWN_PANELS:
            raise LessonError(f"{ctx}: unknown panel '{name}' — the console has no such panel")
    instruments = [name for name in panels if name in INSTRUMENT_PANELS]
    if instruments and len(panels) > 1:
        raise LessonError(
            f"{ctx}: the '{instruments[0]}' panel is an instrument that replaces the waterfall, so "
            "it cannot be combined with another instrument or the overlay panels "
            "(gain/provenance/ppm)")

    # Choices are the keyboard-free predict options a pack may carry: one tappable chip per
    # entry, each a {key, label}. The key is what the commit records, the label is what the
    # learner reads. Optional — a pack without them renders no chips — but when present every
    # entry needs both, or a chip would be unlabelled or its tap unrecordable.
    choices_in = data.get("choices", [])
    if not isinstance(choices_in, list):
        raise LessonError(f"{ctx}: 'choices' must be a list of predict options")
    choices = []
    for choice in choices_in:
        raw_key = choice.get("key") if isinstance(choice, dict) else None
        raw_label = choice.get("label") if isinstance(choice, dict) else None
        # YAML 1.1 parses bare yes/no/true/false/on/off as booleans, so an unquoted choice word
        # (`key: yes`) arrives here as a Python bool, not a string. str()-coercing it would yield a
        # "True"/"False" chip — a silent wrong label the non-empty check below cannot catch. Refuse
        # the bool and name the fix; a quoted word in the pack stays a string and loads normally.
        for role, value in (("key", raw_key), ("label", raw_label)):
            if isinstance(value, bool):
                raise LessonError(
                    f'{ctx}: predict choice {role} {value!r} is a YAML boolean — quote it in the '
                    f'pack (e.g. {role}: "yes") so it stays text, not True/False'
                )
        key = str(raw_key).strip() if raw_key is not None else ""
        label = str(raw_label).strip() if raw_label is not None else ""
        if not key or not label:
            raise LessonError(f"{ctx}: each predict choice needs a non-empty 'key' and 'label'")
        choices.append({"key": key, "label": label})

    answer, verdict = _parse_answer(data, choices, ctx)

    hardware = list(data.get("hardware", []))
    predict_kind = data.get("predict_kind") or ("instrument" if hardware else "knowledge")
    if predict_kind not in REVEAL_LABELS:
        known = ", ".join(sorted(REVEAL_LABELS))
        raise LessonError(f"{ctx}: unknown predict_kind '{predict_kind}' (known: {known})")

    # Optional fields default to empty rather than None so the templates can iterate them
    # without guarding every one, and a pack can omit what it does not use. Only the stages
    # this type requires are carried onto the Lesson; any extra keys in the pack are dropped.
    return Lesson(
        id=int(_require(data, "id", ctx)),
        slug=str(_require(data, "slug", ctx)),
        title=str(_require(data, "title", ctx)),
        concept=str(_require(data, "concept", ctx)),
        type=lesson_type,
        requires_tx=bool(data.get("requires_tx", False)),
        hardware=hardware,
        stages={stage: str(stages_in[stage]).strip() for stage in required},
        hints=list(hints),
        quiz=list(data.get("quiz", [])),
        artifact=str(data.get("artifact", "")),
        panels=list(panels),
        choices=choices,
        tune=_parse_tune(data, ctx),
        answer=answer,
        verdict=verdict,
        check=_parse_check(data, ctx),
        predict_kind=predict_kind,
    )


def load_lesson_file(path: Path) -> Lesson:
    """Load and validate a single lesson pack from disk."""
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise LessonError(f"{path}: top-level YAML must be a mapping")
    return parse_lesson(data)


def load_lessons(pack_dir) -> dict:
    """Load every *.yaml pack in a directory, keyed by lesson id."""
    pack_dir = Path(pack_dir)
    lessons: dict = {}
    for path in sorted(pack_dir.glob("*.yaml")):
        lesson = load_lesson_file(path)
        if lesson.id in lessons:
            raise LessonError(f"duplicate lesson id {lesson.id} in {path}")
        lessons[lesson.id] = lesson
    return lessons
