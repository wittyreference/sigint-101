# ABOUTME: Loads and validates SIGINT 101 lesson packs (YAML) into Lesson objects.
# ABOUTME: A lesson pack is the unit of curriculum; the console renders one at a time.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .spectrum import MAX_SPAN_HZ, MIN_SPAN_HZ, TUNER_MAX_HZ, TUNER_MIN_HZ

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
INSTRUMENT_PANELS = (DECODE_PANEL, LORA_PANEL, RDS_PANEL, ADSB_PANEL, APRS_PANEL, POCSAG_PANEL)
KNOWN_PANELS = (*OVERLAY_PANELS, *INSTRUMENT_PANELS)

# The tune buttons step by this much when a pack declares a band but not its own step. A
# lesson on a narrow signal can ask for a finer step; 100 kHz is the FM-band default.
DEFAULT_TUNE_STEP_HZ = 100_000


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

    @property
    def needs_hardware(self) -> bool:
        """Does this lesson require a radio to be attached before it can be worked?"""
        return bool(self.hardware)

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
    def stage_order(self) -> tuple:
        """The stages this lesson walks, in order, fixed by its type."""
        return REQUIRED_STAGES[self.type]

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
            "stages": {stage: self.stages[stage] for stage in self.sealed_stages},
            "hints": list(self.hints),
            "quiz": list(self.quiz),
            "artifact": self.artifact,
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

    A pack names where its instrument should start — `tune: {center_hz, span_hz, step_hz?}` —
    because the eight FM-band lessons predate any need to leave 88-108 MHz, and a lesson on a
    signal elsewhere (OOK at 433.92 MHz) cannot be hand-tuned there a hundred kHz at a time. The
    band is checked against what the radio can actually reach and what the sweeper can sweep, at
    load, so an unreachable pack fails here rather than mid-lesson in front of a learner. A pack
    with no `tune` returns None and keeps the default FM sweep.
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
    return {"center_hz": center, "span_hz": span, "step_hz": step,
            "low_hz": low, "high_hz": high}


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
        hardware=list(data.get("hardware", [])),
        stages={stage: str(stages_in[stage]).strip() for stage in required},
        hints=list(hints),
        quiz=list(data.get("quiz", [])),
        artifact=str(data.get("artifact", "")),
        panels=list(panels),
        choices=choices,
        tune=_parse_tune(data, ctx),
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
