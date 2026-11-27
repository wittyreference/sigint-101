# ABOUTME: Guards Part 15 LongFast vs Long Turbo facts in L5/L28 and NOAA APT 2025 status in L24.
# ABOUTME: Verifies the claims, D16 non-lockout framing, and primary sources in each pack's YAML.

import re
from pathlib import Path

import pytest
import yaml

from sigint101.lessons import load_lesson_file

PACK_DIR = Path(__file__).resolve().parent.parent / "lesson_packs"
PACK_M5 = PACK_DIR / "05-the-rules.yaml"
PACK_M24 = PACK_DIR / "24-noaa-apt.yaml"
PACK_M28 = PACK_DIR / "28-meshtastic.yaml"

# --- Part 15 LongFast (250 kHz, 47 CFR 15.249) vs Long Turbo (500 kHz, 47 CFR 15.247) ----------

PART15_249 = re.compile(r"\b15\.249\b")
PART15_247 = re.compile(r"\b15\.247(?:\(a\)\(2\))?\b")
LONGFAST_250 = re.compile(
    r"\blong\s*fast\b[^.?!]*\b250\s*kHz\b|\b250\s*kHz\b[^.?!]*\blong\s*fast\b", re.I)
LOW_POWER_249 = re.compile(r"\b0\.75\s*mW\b|\b50\s*mV/m\b", re.I)
LONGTURBO_500 = re.compile(
    r"\blong\s*turbo\b[^.?!]*\b500\s*kHz\b|\b500\s*kHz\b[^.?!]*\blong\s*turbo\b", re.I)
ONE_WATT = re.compile(r"\b1\s*W\b|\bone\s+watt\b", re.I)
RECON_LONG_TURBO = re.compile(r"\brecon\b[^.?!]*\blong\s*turbo\b", re.I)
LOCKOUT_PHRASE = re.compile(
    r"\byou\s+(?:may|must|can)\s+not\b|\byou\s+cannot\b|\bforbidden\b", re.I)

# --- NOAA APT 2025 passivation & Meteor-M LRPT 137.900 MHz interim honesty ---------------------

NO_NOAA_SINCE_AUG_2025 = re.compile(
    r"\bno\s+noaa\b[^.?!]*\bapt\b[^.?!]*\b(?:august|aug\.?)\s+2025\b", re.I)
NOAA_DATES = (
    re.compile(r"\bnoaa[\s-]*18\b[^.;)]*\b(?:6\s+june|june\s+6)\b", re.I),
    re.compile(r"\bnoaa[\s-]*19\b[^.;)]*\b(?:13\s+august|august\s+13)\b", re.I),
    re.compile(r"\bnoaa[\s-]*15\b[^.;)]*\b(?:19\s+august|august\s+19)\b", re.I),
)
LIVE_APT_PROMISE = re.compile(
    r"\bsatdump\s+decodes\s+it\s+live\b"
    r"|\bpanel\s+stays\s+empty\s+until\s+you\s+aim\b", re.I)
LABELLED_RECORDING = re.compile(r"\blabelled?\s+recording\b", re.I)
METEOR_LRPT_137900 = re.compile(
    r"\bmeteor[\s-]*m\b[^.?!]*\blrpt\b[^.?!]*\b137\.900\s*MHz\b[^.?!]*\bdecoder\b", re.I)


def _raw_pack(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _learner_prose(lesson) -> str:
    return " ".join([lesson.concept, *lesson.stages.values(), lesson.verdict])


def _part15_preset_sentences(text: str) -> list:
    return [s for s in re.split(r"(?<=[.?!])\s+", text)
            if re.search(r"\b(?:15\.247|15\.249|long\s*turbo|long\s*fast)\b", s, re.I)]


def _check_part15_claim(text: str) -> list:
    """Return missing/broken items for the Part 15 LongFast vs Long Turbo teaching claim."""
    issues = []
    if not PART15_249.search(text):
        issues.append("missing 15.249")
    if not PART15_247.search(text):
        issues.append("missing 15.247")
    if not LONGFAST_250.search(text):
        issues.append("missing LongFast 250 kHz")
    if not LOW_POWER_249.search(text):
        issues.append("missing 15.249 power/field limit (0.75 mW or 50 mV/m)")
    if not LONGTURBO_500.search(text):
        issues.append("missing Long Turbo 500 kHz")
    if not ONE_WATT.search(text):
        issues.append("missing 1 W limit")
    if not RECON_LONG_TURBO.search(text):
        issues.append("missing why the Recon runs Long Turbo")
    for sentence in _part15_preset_sentences(text):
        if LOCKOUT_PHRASE.search(sentence):
            issues.append(f"lockout phrasing in Part 15 preset text: {sentence!r}")
    return issues


def _check_apt_honesty(text: str) -> list:
    """Return missing/broken items for the L24 NOAA APT 2025 status and Meteor LRPT handoff."""
    issues = []
    if not NO_NOAA_SINCE_AUG_2025.search(text):
        issues.append(
            "missing plain statement that no NOAA satellite transmitted APT since Aug 2025"
        )
    if not re.search(r"\bnesdis\b", text, re.I):
        issues.append("missing NESDIS attribution")
    for idx, pat in enumerate(NOAA_DATES, 1):
        if not pat.search(text):
            issues.append(f"missing NOAA decommission date #{idx}")
    if LIVE_APT_PROMISE.search(text):
        issues.append("still promises a live APT pass will decode/fill the panel")
    if not LABELLED_RECORDING.search(text):
        issues.append("missing labelled recording note for APT")
    if not METEOR_LRPT_137900.search(text):
        issues.append("missing Meteor-M LRPT on 137.900 MHz once its decoder lands")
    return issues


def _source_urls(raw: dict) -> list:
    sources = raw.get("sources")
    if not isinstance(sources, list):
        return []
    return [str(entry.get("url", "")) for entry in sources if isinstance(entry, dict)]


# --- Both-directions unit probes (defect restatements fail; fair rewordings pass) --------------


def test_part15_claim_probe_rejects_defects_and_accepts_rewordings():
    missing_rule = (
        "Meshtastic is licence-free under FCC Part 15 on compliant equipment set to the right "
        "region, inside its power, antenna and no-interference conditions."
    )
    lockout_wording = (
        "LongFast (250 kHz) sits under 15.249 at 0.75 mW so you may not run it; "
        "Long Turbo (500 kHz) meets 15.247 at 1 W, so the Recon runs Long Turbo."
    )
    fair_rewording = (
        "LongFast uses 250 kHz on one channel, which falls under 47 CFR 15.249 (50 mV/m at 3 m, "
        "about 0.75 mW EIRP). Long Turbo widens to 500 kHz to meet 47 CFR 15.247 and its one watt "
        "cap, so the Recon runs Long Turbo."
    )
    assert _check_part15_claim(missing_rule)
    assert any("lockout" in item for item in _check_part15_claim(lockout_wording))
    assert _check_part15_claim(fair_rewording) == []


def test_apt_honesty_probe_rejects_defects_and_accepts_rewordings():
    old_promise = (
        "satdump decodes it live. NOAA-15, NOAA-18 and NOAA-19 were decommissioned in 2025. "
        "The panel stays empty until you aim at a predicted pass of Meteor-M near 137.9 MHz."
    )
    fair_rewording = (
        "No NOAA bird has sent APT since August 2025 (NESDIS: NOAA-18 June 6, NOAA-19 August 13, "
        "NOAA-15 August 19, 2025). APT will come from a labelled recording; the live pass will be "
        "Meteor-M LRPT on 137.900 MHz once its decoder is wired."
    )
    assert _check_apt_honesty(old_promise)
    assert _check_apt_honesty(fair_rewording) == []


CALLS_8DBM_THE_GRANT = re.compile(
    r"\b8\s*dBm\s+(?:fcc\s+)?grant\b"
    r"|\b(?:fcc\s+)?grant\s+(?:of|is|at|:)\s+8\s*dBm\b"
    r"|\b8\s*dBm\s+is\s+(?:the|its)\s+(?:module['’]?s\s+)?(?:fcc\s+)?grant\b",
    re.I,
)
EIGHT_DBM_UNDER_GRANT = re.compile(
    r"\b8\s*dBm\b[^.?!]*\b(?:under|below|within|inside)\b[^.?!]*\b(?:fcc\s+)?grant\b"
    r"|\b(?:under|below|within|inside)\b[^.?!]*\b(?:fcc\s+)?grant\b[^.?!]*\b8\s*dBm\b",
    re.I,
)


def _check_m28_long_turbo_pointer(text: str) -> list:
    """M28 says why the kit runs Long Turbo (500 kHz), points to L05, and puts 8 dBm under grant."""
    issues = []
    if not LONGTURBO_500.search(text):
        issues.append("missing Long Turbo 500 kHz")
    if not RECON_LONG_TURBO.search(text):
        issues.append("missing why the Recon runs Long Turbo")
    if not re.search(r"\blesson\s+0?5\b", text, re.I):
        issues.append("missing pointer to Lesson 05")
    if not re.search(r"\b8\s*dBm\b", text):
        issues.append("missing 8 dBm transmit power setting")
    if not re.search(r"\bfcc\s+grant\b", text, re.I):
        issues.append("missing module FCC grant attribution")
    if CALLS_8DBM_THE_GRANT.search(text):
        issues.append("calls 8 dBm the FCC grant (grant is ~9 dBm at 500 kHz; 8 dBm sits under it)")
    if not EIGHT_DBM_UNDER_GRANT.search(text):
        issues.append("does not place 8 dBm under/below/within the module's FCC grant")
    if not re.search(r"\bprovision\w*\b", text, re.I):
        issues.append("missing provisioning attribution for 8 dBm")
    for sentence in _part15_preset_sentences(text):
        if LOCKOUT_PHRASE.search(sentence):
            issues.append(f"lockout phrasing in Part 15 preset text: {sentence!r}")
    return issues


def test_m28_pointer_probe_rejects_defects_and_accepts_rewordings():
    missing_pointer = (
        "This Recon runs Long Turbo (500 kHz) at 8 dBm because of Part 15."
    )
    calls_8dbm_the_grant = (
        "This Recon runs Long Turbo (500 kHz) for the US Part 15 bandwidth rule in Lesson 05, "
        "and provisioning keeps transmit power within the module's 8 dBm FCC grant."
    )
    grant_of_8dbm = (
        "This Recon runs Long Turbo (500 kHz) for the US Part 15 bandwidth rule in Lesson 05, "
        "and provisioning holds power under its FCC grant of 8 dBm."
    )
    fair_rewording = (
        "This Recon runs Long Turbo (500 kHz) for the US Part 15 bandwidth rule in Lesson 05, "
        "with provisioning keeping transmit power at 8 dBm, under the module's FCC grant."
    )
    assert _check_m28_long_turbo_pointer(missing_pointer)
    assert any("8 dBm the FCC grant" in item
               for item in _check_m28_long_turbo_pointer(calls_8dbm_the_grant))
    assert any("8 dBm the FCC grant" in item
               for item in _check_m28_long_turbo_pointer(grant_of_8dbm))
    assert _check_m28_long_turbo_pointer(fair_rewording) == []


def test_no_lesson_calls_8dbm_the_fcc_grant():
    # The E22-900M22S module's FCC grant is ~9 dBm at 500 kHz; 8 dBm is the provisioned ceiling
    # kept below it. No shipped lesson may call 8 dBm the grant itself.
    for path in sorted(PACK_DIR.glob("*.yaml")):
        lesson = load_lesson_file(path)
        choice_labels = [str(c.get("label", "")) for c in (lesson.choices or [])]
        prose = " ".join([_learner_prose(lesson), *choice_labels])
        match = CALLS_8DBM_THE_GRANT.search(prose)
        assert not match, f"{path.name} calls 8 dBm the FCC grant: {match.group(0)!r}"


# --- Shipped pack checks (fail on main, pass after edits) --------------------------------------


def test_m5_states_part15_rule_and_why_recon_runs_long_turbo():
    lesson = load_lesson_file(PACK_M5)
    issues = _check_part15_claim(_learner_prose(lesson))
    assert issues == [], f"{PACK_M5.name}: {issues}"


def test_m28_points_to_lesson_05_for_long_turbo_and_notes_8dbm_fcc_grant():
    lesson = load_lesson_file(PACK_M28)
    issues = _check_m28_long_turbo_pointer(_learner_prose(lesson))
    assert issues == [], f"{PACK_M28.name}: {issues}"


@pytest.mark.parametrize("path", [PACK_M5, PACK_M28], ids=["L05", "L28"])
def test_m5_and_m28_cite_ecfr_and_meshtastic_sources(path):
    urls = _source_urls(_raw_pack(path))
    assert any("ecfr.gov" in u and "15.247" in u for u in urls), f"{path.name}: missing 15.247 URL"
    assert any("ecfr.gov" in u and "15.249" in u for u in urls), f"{path.name}: missing 15.249 URL"
    assert any("meshtastic.org" in u for u in urls), f"{path.name}: missing meshtastic.org URL"


def test_m24_honestly_states_noaa_apt_end_and_meteor_handoff():
    lesson = load_lesson_file(PACK_M24)
    issues = _check_apt_honesty(_learner_prose(lesson))
    assert issues == [], f"{PACK_M24.name}: {issues}"


def test_m24_cites_nesdis_and_meteor_sources():
    urls = _source_urls(_raw_pack(PACK_M24))
    assert any("nesdis.noaa.gov" in u for u in urls), "L24: missing NESDIS source URL"
    assert any("oscar.wmo.int" in u and "meteor_m" in u for u in urls), (
        "L24: missing WMO OSCAR Meteor-M source URL"
    )
