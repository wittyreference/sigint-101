# ABOUTME: Tests the M24 "A Picture From Space" pack loads as a valid six-stage intro — id 24, the
# ABOUTME: apt image instrument on the 137 MHz band, and the grainy-but-real answer at predict.

import re
from pathlib import Path

from sigint101.lessons import REQUIRED_STAGES, load_lesson_file

PACK = (Path(__file__).resolve().parent.parent / "lesson_packs" / "24-noaa-apt.yaml")

# A23: the pack said "A packet with one flipped bit fails its checksum and is thrown away whole."
# The course's decoders correct errors before they give a packet up: multimon-ng repairs up to two
# flipped bits in each POCSAG codeword unless told otherwise (its -b level defaults to 2, and
# sigint101/pocsag.py passes none), so one flipped bit costs a page nothing. A packet is lost when
# it carries more errors than its error correction can repair; for dump1090 as the console runs it
# (no --fix) that is one. A sentence fails when it has a single bit (one flipped, wrong or bad bit,
# a flipped bit, one bit, a single-bit error, flip one bit) cost a packet its checksum or its life,
# unless the same sentence has that bit repaired, corrected or fixed.
SINGLE_BIT = re.compile(
    r"\b(?:one|a|a\s+single|single)[\s-]+(?:flipped|wrong|bad|corrupt(?:ed)?|damaged|errant|stray"
    r"|broken|garbled)\s+bits?\b"
    r"|\b(?:one|a\s+single|single)[\s-]+bit\b|\ba\s+bit[\s-]+(?:error|flip)\b"
    r"|\bflip(?:s|ped|ping)?\s+(?:a|one|a\s+single|just\s+one|even\s+one)\s+bit\b", re.I)
LOSS = re.compile(
    r"\b(?:fail(?:s|ed|ing)?|thrown|throws?|threw|discard\w*|drop(?:s|ped|ping)?|lost|lose|loses"
    r"|losing|vanish\w*|gone|kill\w*|ruin\w*|reject\w*|destroy\w*|wreck\w*|dies|dead)\b", re.I)
REPAIRED = re.compile(r"\b(?:repaired|corrected|fixed|repairs|corrects|fixes)\b", re.I)

# What the pack says instead, in the ruling's words: a packet is thrown away when it carries more
# errors than its error correction can repair. One sentence must name a packet (or a frame, a
# codeword, a page or a message), its repair or correction, and its loss.
PACKET = re.compile(r"\b(?:packets?|frames?|codewords?|pages?|messages?)\b", re.I)
REPAIR = re.compile(r"\b(?:repair\w*|correct(?:s|ed|ing|ion)?|fix(?:es|ed|ing)?)\b", re.I)

# A23 too: "every digital decoder behind you was all-or-nothing". The ruling drops "every", so the
# contrast with analog stays a general rule, not a claim about each decoder of the course. A
# sentence fails when it puts every, each, all or any decoder (or packet, frame, signal, mode or
# protocol) beside all-or-nothing or whole-or-gone, or calls digital always all-or-nothing.
EVERY_DECODER = re.compile(
    r"\b(?:every|each|all|any)(?:\s+(?:one|single))?(?:\s+of)?(?:\s+(?:the|your|those|these))?"
    r"(?:\s+\w+){0,2}?\s+(?:decoders?|packets?|frames?|signals?|modes?|protocols?)\b", re.I)
ALL_OR_NOTHING = re.compile(r"\ball[\s-]+or[\s-]+nothing\b|\bwhole\s+or\s+(?:gone|nothing|lost)\b",
                            re.I)
ALWAYS_ALL_OR_NOTHING = re.compile(r"\balways\s+(?:\w+\s+){0,2}?all[\s-]+or[\s-]+nothing\b", re.I)

# A28: the pack said a weak pass is "grainy, never simply gone" and "analog only gets noisier".
# APT's tone rides an FM carrier, and an FM receiver has a threshold: below a critical
# carrier-to-noise ratio its output falls apart and lines are lost, so a pass weak enough is gone.
# A sentence fails when it says a pass, a picture or analog is never gone, lost, vanished or dead,
# never fails or drops out completely, or always leaves a picture; or that analog only (just,
# merely) gets noisier, grainier, fainter or worse.
NEVER_GONE = re.compile("|".join((
    r"\bnever\s+(?:\w+\s+){0,2}?(?:gone|lost|vanish\w*|disappear\w*|blank|dead|dies|empty)\b",
    r"\bnever\s+(?:\w+\s+){0,2}?(?:fails?|drops?|cuts?)\s+(?:out(?:right)?|completely|entirely"
    r"|altogether|totally|for\s+good)\b",
    r"\balways\s+(?:\w+\s+){0,3}?(?:pictures?|images?)\b",
    r"\b(?:pictures?|images?)\s+(?:\w+\s+){0,2}?always\s+(?:survives?|remains?|arrives?"
    r"|comes?\s+through|gets?\s+through)\b",
)), re.I)
ONLY_NOISIER = re.compile(
    r"\b(?:only|just|merely|simply)\s+(?:ever\s+)?(?:gets?|grows?|becomes?|turns?|goes|go)\s+"
    r"(?:\w+\s+){0,2}?(?:noisier|grainier|fainter|weaker|worse|dimmer|snowier|more\s+\w+)\b"
    r"|\bonly\s+ever\s+(?:noisier|grainier|fainter|weaker|worse)\b", re.I)


def _sentences(text):
    """A text's sentences: split after a full stop, question mark or bang and a space."""
    return [s for s in re.split(r"(?<=[.?!])\s+", text) if s]


def _learner_text(lesson):
    """Every sentence the learner reads in M24: concept, stages, verdict, chips and any check."""
    texts = [lesson.concept, *lesson.stages.values(), lesson.verdict,
             *(choice["label"] for choice in lesson.choices)]
    if lesson.check:
        texts.append(lesson.check["prompt"])
        texts.extend(choice["why"] for choice in lesson.check["choices"])
    return [s for text in texts for s in _sentences(text)]


def test_m24_pack_is_a_valid_intro_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "intro"
    assert lesson.id == 24
    assert lesson.slug == "noaa-apt"


def test_m24_has_all_six_intro_stages_non_empty():
    lesson = load_lesson_file(PACK)
    assert lesson.stage_order == ("frame", "demo", "try", "predict", "observe", "explain")
    for stage in REQUIRED_STAGES["intro"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"


def test_m24_uses_the_apt_image_instrument_on_the_137_mhz_band():
    lesson = load_lesson_file(PACK)
    assert lesson.uses_apt is True
    assert lesson.panels == ["apt"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    assert lesson.tune is not None
    assert lesson.tune["center_hz"] == 137_500_000


def test_m24_offers_the_grainy_but_real_answer_as_a_predict_choice():
    lesson = load_lesson_file(PACK)
    assert lesson.choices, "M24 must offer keyboard-free predict choices"
    for choice in lesson.choices:
        assert isinstance(choice.get("key"), str) and choice["key"]
        assert isinstance(choice.get("label"), str) and choice["label"]
    # APT is analog, so it degrades gracefully into a grainy-but-real picture, not all-or-nothing.
    assert any(c["key"] == "grainy" for c in lesson.choices)


def test_m24_explains_the_graceful_analog_degradation_in_the_reveal():
    lesson = load_lesson_file(PACK)
    assert "explain" in lesson.sealed_stages
    assert "analog" in lesson.stages["explain"].lower()


def test_m24_never_says_one_flipped_bit_throws_a_packet_away():
    claims = [s for s in _learner_text(load_lesson_file(PACK))
              if SINGLE_BIT.search(s) and LOSS.search(s) and not REPAIRED.search(s)]
    assert not claims, f"M24 still says a single flipped bit loses a whole packet: {claims}"


def test_m24_ties_a_lost_packet_to_its_error_correction():
    sentences = _learner_text(load_lesson_file(PACK))
    assert any(PACKET.search(s) and REPAIR.search(s) and LOSS.search(s) for s in sentences), (
        "M24 must say a packet is thrown away only with more errors than its error correction "
        "can repair")


def test_m24_never_says_every_digital_decoder_was_all_or_nothing():
    claims = [s for s in _learner_text(load_lesson_file(PACK))
              if (EVERY_DECODER.search(s) and ALL_OR_NOTHING.search(s))
              or ALWAYS_ALL_OR_NOTHING.search(s)]
    assert not claims, f"M24 still says every digital decoder was all-or-nothing: {claims}"


def test_m24_never_says_a_weak_pass_is_never_gone():
    claims = [s for s in _learner_text(load_lesson_file(PACK)) if NEVER_GONE.search(s)]
    assert not claims, f"M24 still says a weak analog pass is never gone: {claims}"


def test_m24_never_says_analog_only_gets_noisier():
    claims = [s for s in _learner_text(load_lesson_file(PACK)) if ONLY_NOISIER.search(s)]
    assert not claims, f"M24 still says analog only gets noisier: {claims}"
