# ABOUTME: Tier C solo engine, catalog validator, audit gate, legal-call scorer, and state machine.
# ABOUTME: Enforces one instrument per leg, leak-free pre-pass payloads, and US allocation labels.

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import operate
from .challenges import (
    FORBIDDEN_INSTRUMENTS,
    AttemptKind,
    attempt_status,
    format_freq,
    in_window,
    rack_label,
)
from .law import LAW_SECTIONS
from .stamps import STAMP_INSTRUMENTS
from .units import UNITS

LEGAL_CALLS = ("legal", "illegal", "depends", "not_sure")
DECISIVE_LEGAL_CALLS = ("legal", "illegal", "depends")
MAX_WHY_OPTIONS = 3
MAX_WHY_LABEL_CHARS = 130
MAX_PROMPT_LINES = 3
MAX_PROMPT_CHARS = 195
MAX_FEEDBACK_LINES = 4
MAX_FEEDBACK_CHARS = 280
NO_FREQ_INSTRUMENTS = frozenset({"lora"})

# Known non-US / decoder-internal band ranges that must never appear in learner band labels.
_DISALLOWED_BAND_SUBSTRINGS = (
    "87.5",
    "300–928",
    "300-928",
    "433.05",
    "434.79",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SOLO_KIND = AttemptKind(
    kind="solo",
    attempts_table="solo_attempts",
    passes_table="solo_passes",
    key_column="solo_id",
    slot_column="band_slot",
    start_event="solo_start",
    pass_event="solo_pass",
    credit="live_reception",
    event_key_field="solo_id",
    label="solo",
)


@dataclass(frozen=True)
class SoloWhyOption:
    """One primary-source-backed true statement of law offered on Step B of the call screen."""

    key: str
    label: str
    supports: tuple[str, ...]
    feedback: str
    cite: str
    source_url: str
    read_date: str
    law_section: str


@dataclass(frozen=True)
class SoloBandLeg:
    """One signal leg of a solo: one instrument, its band clue, and its legal-call item."""

    slot: int
    unit_id: int
    band_label: str
    clue: str
    instrument: str
    band_hz: tuple[tuple[int, int], ...]
    hardware_note: str
    legal_prompt: str
    legal_answers: tuple[str, ...]
    ambiguous: bool
    why_options: tuple[SoloWhyOption, ...]
    sources: tuple[str, ...]
    audit_id: str
    item_revision: str


@dataclass(frozen=True)
class Solo:
    """One timed Tier C solo (per-unit when unit_id is set; capstone when unit_id is None)."""

    id: str
    unit_id: int | None
    title: str
    legs: tuple[SoloBandLeg, ...]
    window_s: int


@dataclass(frozen=True)
class SoloAuditRecord:
    """Independent blind-expert audit verdict gating whether a solo leg may be shown."""

    audit_id: str
    solo_id: str
    slot: int
    author: str
    evaluator: str
    verdict: str
    date: str
    item_revision: str


# Slice 1a-ii ships no user-visible solos; Units 4, 5, 9 and the capstone land in Slices 1b-3
# after independent blind-expert legal audits.
SOLOS: tuple[Solo, ...] = ()
SOLO_AUDITS: dict[str, SoloAuditRecord] = {}


def forbidden_leak_words() -> tuple[str, ...]:
    """Derive every instrument key, label token, tool token, and rack label that must not leak."""
    tokens: set[str] = {"Meshtastic"}
    for inst in operate.INSTRUMENTS:
        tokens.add(inst.key)
        tokens.add(inst.label)
        for part in re.split(r"[/|]", inst.label):
            part = part.strip()
            if part:
                tokens.add(part)
        for part in re.split(r"[/|]", inst.tool):
            part = part.strip()
            if part:
                tokens.add(part)
        tokens.add(rack_label(inst.key))
    # Sort longest first for deterministic diagnostics.
    return tuple(sorted((t for t in tokens if t), key=lambda s: (-len(s), s.lower())))


def find_leaked_words(text: str) -> list[str]:
    """Return every derived forbidden word matching `text` case-insensitively on whole words."""
    if not text:
        return []
    hits: list[str] = []
    for tok in forbidden_leak_words():
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])", re.I)
        if pattern.search(text):
            hits.append(tok)
    return hits


def solo_is_audited(solo: Solo, audits: dict[str, SoloAuditRecord] | None) -> bool:
    """True only when every leg has an independent passing audit record for its item_revision."""
    if not audits or not solo.legs:
        return False
    for leg in solo.legs:
        rec = audits.get(leg.audit_id)
        if rec is None:
            return False
        if rec.audit_id != leg.audit_id or rec.solo_id != solo.id or rec.slot != leg.slot:
            return False
        if rec.item_revision != leg.item_revision:
            return False
        if rec.verdict != "pass":
            return False
        author = (rec.author or "").strip().lower()
        evaluator = (rec.evaluator or "").strip().lower()
        if not author or not evaluator or author == evaluator:
            return False
        if not _DATE_RE.match(rec.date or ""):
            return False
    return True


def _bands_overlap(
    first: tuple[tuple[int, int], ...],
    second: tuple[tuple[int, int], ...],
) -> bool:
    for lo1, hi1 in first:
        for lo2, hi2 in second:
            if lo1 <= hi2 and lo2 <= hi1:
                return True
    return False


def validate_solo_catalog(
    catalog: tuple[Solo, ...],
    audits: dict[str, SoloAuditRecord] | None = None,
    *,
    require_audit: bool = True,
) -> None:
    """Validate a solo catalog; raises ValueError (never assert) on any contract breach."""
    known_units = {unit.id for unit in UNITS}
    known_laws = {sec.id for sec in LAW_SECTIONS}
    seen_ids: set[str] = set()
    seen_units: set[int | None] = set()

    for solo in catalog:
        if not isinstance(solo.id, str) or not solo.id.strip():
            raise ValueError("solo id must be a non-empty string")
        if solo.id in seen_ids:
            raise ValueError(f"duplicate solo id {solo.id!r}")
        seen_ids.add(solo.id)
        if solo.unit_id is not None and solo.unit_id not in known_units:
            raise ValueError(f"solo {solo.id!r} references unknown unit {solo.unit_id!r}")
        if solo.unit_id in seen_units:
            raise ValueError(f"duplicate solo for unit {solo.unit_id!r}")
        seen_units.add(solo.unit_id)
        if type(solo.window_s) is not int or solo.window_s <= 0:
            raise ValueError(f"solo {solo.id!r} window_s must be a positive int")
        if not isinstance(solo.title, str) or not solo.title.strip():
            raise ValueError(f"solo {solo.id!r} has a blank title")
        if leaks := find_leaked_words(solo.title):
            raise ValueError(f"solo {solo.id!r} title leaks forbidden word(s): {leaks}")
        if not solo.legs:
            raise ValueError(f"solo {solo.id!r} must have at least one leg")

        seen_leg_units: set[int] = set()
        seen_instruments: set[str] = set()
        seen_bands: list[tuple[tuple[int, int], ...]] = []
        for idx, leg in enumerate(solo.legs):
            if leg.slot != idx:
                raise ValueError(f"solo {solo.id!r} leg {idx} has mismatched slot {leg.slot!r}")
            if leg.unit_id not in known_units:
                raise ValueError(f"solo {solo.id!r} leg {idx} unknown unit {leg.unit_id!r}")
            if leg.unit_id in seen_leg_units:
                raise ValueError(f"solo {solo.id!r} leg {idx} duplicates unit {leg.unit_id}")
            seen_leg_units.add(leg.unit_id)

            if not isinstance(leg.instrument, str) or not leg.instrument:
                raise ValueError(f"solo {solo.id!r} leg {idx} must name exactly one instrument")
            if leg.instrument in FORBIDDEN_INSTRUMENTS:
                raise ValueError(
                    f"solo {solo.id!r} leg {idx} forbids instrument {leg.instrument!r}"
                )
            if leg.instrument not in STAMP_INSTRUMENTS:
                raise ValueError(
                    f"solo {solo.id!r} leg {idx} instrument {leg.instrument!r} "
                    "not in STAMP_INSTRUMENTS"
                )
            if operate.instrument(leg.instrument) is None:
                raise ValueError(
                    f"solo {solo.id!r} leg {idx} instrument {leg.instrument!r} not in rack"
                )
            if leg.instrument in seen_instruments:
                raise ValueError(
                    f"solo {solo.id!r} leg {idx} duplicates instrument {leg.instrument!r}"
                )
            seen_instruments.add(leg.instrument)

            if leg.instrument in NO_FREQ_INSTRUMENTS:
                if leg.band_hz != ():
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} ({leg.instrument}) must have empty band_hz"
                    )
            else:
                if not isinstance(leg.band_hz, tuple) or not leg.band_hz:
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} ({leg.instrument}) requires non-empty band_hz"
                    )
                for pair in leg.band_hz:
                    if (
                        not isinstance(pair, tuple)
                        or len(pair) != 2
                        or type(pair[0]) is not int
                        or type(pair[1]) is not int
                        or pair[0] <= 0
                        or pair[0] > pair[1]
                    ):
                        raise ValueError(
                            f"solo {solo.id!r} leg {idx} has invalid band_hz bound {pair!r}"
                        )
                for prev in seen_bands:
                    if _bands_overlap(prev, leg.band_hz):
                        raise ValueError(
                            f"solo {solo.id!r} leg {idx} band_hz overlaps another leg"
                        )
                seen_bands.append(leg.band_hz)

            if not isinstance(leg.band_label, str) or not leg.band_label.strip():
                raise ValueError(f"solo {solo.id!r} leg {idx} has a blank band_label")
            for bad in _DISALLOWED_BAND_SUBSTRINGS:
                if bad in leg.band_label:
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} band_label uses non-US/internal range {bad!r}"
                    )
            for field_name, value in (
                ("band_label", leg.band_label),
                ("clue", leg.clue),
                ("hardware_note", leg.hardware_note),
            ):
                if field_name != "hardware_note" and (
                    not isinstance(value, str) or not value.strip()
                ):
                    raise ValueError(f"solo {solo.id!r} leg {idx} has a blank {field_name}")
                if leaks := find_leaked_words(value):
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} {field_name} leaks forbidden word(s): {leaks}"
                    )

            if (
                not isinstance(leg.legal_prompt, str)
                or not leg.legal_prompt.strip()
                or len(leg.legal_prompt.splitlines()) > MAX_PROMPT_LINES
                or len(leg.legal_prompt) > MAX_PROMPT_CHARS
            ):
                raise ValueError(f"solo {solo.id!r} leg {idx} legal_prompt exceeds cap or is blank")

            if (
                not isinstance(leg.legal_answers, tuple)
                or not leg.legal_answers
                or any(ans not in DECISIVE_LEGAL_CALLS for ans in leg.legal_answers)
                or len(set(leg.legal_answers)) != len(leg.legal_answers)
            ):
                raise ValueError(f"solo {solo.id!r} leg {idx} has invalid legal_answers")
            if type(leg.ambiguous) is not bool:
                raise ValueError(f"solo {solo.id!r} leg {idx} ambiguous must be a bool")

            if (
                not isinstance(leg.why_options, tuple)
                or not (1 <= len(leg.why_options) <= MAX_WHY_OPTIONS)
            ):
                raise ValueError(
                    f"solo {solo.id!r} leg {idx} must have 1..{MAX_WHY_OPTIONS} why_options"
                )
            seen_opt_keys: set[str] = set()
            supported_calls: set[str] = set()
            for opt in leg.why_options:
                if not isinstance(opt.key, str) or not opt.key.strip():
                    raise ValueError(f"solo {solo.id!r} leg {idx} why option has blank key")
                if opt.key in seen_opt_keys:
                    raise ValueError(f"solo {solo.id!r} leg {idx} duplicate why_option {opt.key!r}")
                seen_opt_keys.add(opt.key)
                if (
                    not isinstance(opt.label, str)
                    or not opt.label.strip()
                    or len(opt.label) > MAX_WHY_LABEL_CHARS
                    or len(opt.label.splitlines()) > 2
                ):
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} option {opt.key!r} label exceeds "
                        f"{MAX_WHY_LABEL_CHARS} chars or 2 lines"
                    )
                if (
                    not isinstance(opt.feedback, str)
                    or not opt.feedback.strip()
                    or len(opt.feedback.splitlines()) > MAX_FEEDBACK_LINES
                    or len(opt.feedback) > MAX_FEEDBACK_CHARS
                ):
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} option {opt.key!r} feedback exceeds cap"
                    )
                if not isinstance(opt.cite, str) or not opt.cite.strip():
                    raise ValueError(f"solo {solo.id!r} leg {idx} option {opt.key!r} missing cite")
                if not isinstance(opt.source_url, str) or not opt.source_url.startswith("https://"):
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} option {opt.key!r} missing https source_url"
                    )
                if not isinstance(opt.read_date, str) or not _DATE_RE.match(opt.read_date):
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} option {opt.key!r} invalid read_date"
                    )
                if opt.law_section not in known_laws:
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} option {opt.key!r} unknown law_section "
                        f"{opt.law_section!r}"
                    )
                if not isinstance(opt.supports, tuple) or any(
                    call not in LEGAL_CALLS for call in opt.supports
                ):
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} option {opt.key!r} invalid supports"
                    )
                supported_calls.update(opt.supports)

            for ans in leg.legal_answers:
                if ans not in supported_calls:
                    raise ValueError(
                        f"solo {solo.id!r} leg {idx} legal_answer {ans!r} has no supporting option"
                    )
            if leg.ambiguous and "not_sure" not in supported_calls:
                raise ValueError(
                    f"solo {solo.id!r} leg {idx} is ambiguous but no option supports 'not_sure'"
                )
            if not leg.ambiguous and "not_sure" in supported_calls:
                raise ValueError(
                    f"solo {solo.id!r} leg {idx} is unambiguous but an option supports 'not_sure'"
                )
            if not isinstance(leg.item_revision, str) or not leg.item_revision.strip():
                raise ValueError(f"solo {solo.id!r} leg {idx} missing item_revision")

        if require_audit and not solo_is_audited(solo, audits):
            raise ValueError(
                f"solo {solo.id!r} lacks a valid independent passing blind-expert audit record"
            )


validate_solo_catalog(SOLOS, SOLO_AUDITS, require_audit=True)


def _freq_in_bands(freq_hz, band_hz: tuple[tuple[int, int], ...]) -> bool:
    if isinstance(freq_hz, list):
        nums = [hz for hz in freq_hz if type(hz) in (int, float) and hz > 0]
        return bool(nums) and all(
            any(lo <= hz <= hi for lo, hi in band_hz) for hz in nums
        )
    if type(freq_hz) in (int, float) and freq_hz > 0:
        return any(lo <= freq_hz <= hi for lo, hi in band_hz)
    return False


def qualifies_leg(leg: SoloBandLeg, attempt: dict, stamp: dict | None) -> bool:
    """True when a minted live stamp satisfies this leg's instrument, band, and window."""
    if not stamp or not attempt or not in_window(attempt, stamp):
        return False
    if stamp.get("instrument") != leg.instrument:
        return False
    prov = stamp.get("provenance") or {}
    freq_hz = prov.get("freq_hz")
    if leg.instrument in NO_FREQ_INSTRUMENTS:
        return leg.band_hz == () and freq_hz is None
    return _freq_in_bands(freq_hz, leg.band_hz)


def qualifies_slot(solo: Solo, attempt: dict, stamp: dict | None) -> int | None:
    """Return the matching leg slot (int) for `stamp` on `solo`, or None."""
    for leg in solo.legs:
        if qualifies_leg(leg, attempt, stamp):
            return leg.slot
    return None


def score_legal_call(leg: SoloBandLeg, call: str, why_key: str) -> dict:
    """Score one learner legal call against a specific heard leg; raises ValueError on bad input."""
    if not isinstance(call, str) or call not in LEGAL_CALLS:
        raise ValueError(f"invalid legal call {call!r}")
    if not isinstance(why_key, str) or not why_key.strip():
        raise ValueError("why_key is required")
    chosen = next((opt for opt in leg.why_options if opt.key == why_key), None)
    if chosen is None:
        raise ValueError(f"unknown why_key {why_key!r} for leg {leg.slot}")
    call_correct = bool(leg.ambiguous) if call == "not_sure" else (call in leg.legal_answers)
    why_correct = call in chosen.supports
    return {
        "call": call,
        "why_key": chosen.key,
        "call_correct": call_correct,
        "why_correct": why_correct,
        "legal_passed": call_correct and why_correct,
        "feedback": chosen.feedback,
        "cite": chosen.cite,
        "law_section": chosen.law_section,
        "item_revision": leg.item_revision,
    }


def format_heard_evidence(pass_row: dict) -> str:
    """Compact heard-evidence line shown on Step A and Result only after reception passes."""
    prov = pass_row.get("provenance") or {}
    inst = pass_row["instrument"]
    parts = [rack_label(inst)]
    tool = prov.get("tool")
    if tool:
        parts.append(str(tool))
    freq = format_freq(prov.get("freq_hz"))
    if freq:
        parts.append(freq)
    else:
        radio = prov.get("radio") or {}
        host = radio.get("host")
        if host:
            parts.append(str(host))
    passed_at = pass_row.get("passed_at")
    if type(passed_at) in (int, float):
        stamp = datetime.fromtimestamp(passed_at, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        parts.append(stamp)
    return " · ".join(parts)


def format_solo_payload(
    solo: Solo,
    attempt: dict | None,
    attempt_passes: list[dict],
    attempt_calls: list[dict],
    latest_complete_pass_ts: float | None,
    now: float,
    *,
    error: str | None = None,
) -> dict:
    """Build the leak-free public API payload for one solo.

    Before a leg's reception passes on the current attempt, emits zero instrument, tool,
    frequency, allowed, legal_answers, ambiguous, supports, or feedback fields.
    """
    passes_by_slot = {p["band_slot"]: p for p in attempt_passes}
    calls_by_slot = {c["band_slot"]: c for c in attempt_calls}
    all_receptions = attempt is not None and all(leg.slot in passes_by_slot for leg in solo.legs)
    all_calls = attempt is not None and all(leg.slot in calls_by_slot for leg in solo.legs)
    any_missed = any(
        not (c["call_correct"] and c["why_correct"]) for c in calls_by_slot.values()
    )
    pending_call_slots = [
        leg.slot
        for leg in solo.legs
        if leg.slot in passes_by_slot and leg.slot not in calls_by_slot
    ]

    if attempt is None:
        state = "idle"
        started_at = None
        window_s = solo.window_s
        remaining_s: float = 0
        reason = None
    elif all_receptions and all_calls:
        state = "call_missed" if any_missed else "passed"
        started_at = attempt["started_at"]
        window_s = attempt["window_s"]
        remaining_s = 0
        reason = None
    elif pending_call_slots:
        base = attempt_status(solo.window_s, attempt, None, now)
        state = "reception_passed"
        started_at = attempt["started_at"]
        window_s = attempt["window_s"]
        remaining_s = 0 if all_receptions else base["remaining_s"]
        reason = None
    else:
        base = attempt_status(solo.window_s, attempt, None, now)
        state = base["state"]
        started_at = base["started_at"]
        window_s = base["window_s"]
        remaining_s = base["remaining_s"]
        reason = base["reason"]

    passed_before_date = None
    if type(latest_complete_pass_ts) in (int, float):
        passed_before_date = datetime.fromtimestamp(
            latest_complete_pass_ts, tz=timezone.utc
        ).strftime("%Y-%m-%d")

    legs_out: list[dict] = []
    for leg in solo.legs:
        pass_row = passes_by_slot.get(leg.slot) if attempt is not None else None
        call_row = calls_by_slot.get(leg.slot) if attempt is not None else None
        leg_data: dict = {
            "slot": leg.slot,
            "band_label": leg.band_label,
            "clue": leg.clue,
            "hardware_note": leg.hardware_note,
            "passed": pass_row is not None,
            "called": call_row is not None,
        }
        if pass_row is not None:
            prov = pass_row.get("provenance") or {}
            leg_data["evidence"] = format_heard_evidence(pass_row)
            leg_data["heard"] = {
                "instrument": pass_row["instrument"],
                "label": rack_label(pass_row["instrument"]),
                "tool": prov.get("tool"),
                "freq_hz": prov.get("freq_hz"),
                "passed_at": pass_row["passed_at"],
            }
            leg_data["legal_prompt"] = leg.legal_prompt
            leg_data["why_options"] = [
                {"key": opt.key, "label": opt.label} for opt in leg.why_options
            ]
        if call_row is not None:
            opt = next((o for o in leg.why_options if o.key == call_row["why_key"]), None)
            call_ok = bool(call_row["call_correct"])
            why_ok = bool(call_row["why_correct"])
            leg_data["call_result"] = {
                "call": call_row["call"],
                "why_key": call_row["why_key"],
                "call_correct": call_ok,
                "why_correct": why_ok,
                "legal_passed": call_ok and why_ok,
                "feedback": opt.feedback if opt is not None else "",
                "cite": opt.cite if opt is not None else "",
                "law_section": opt.law_section if opt is not None else "",
                "item_revision": call_row["item_revision"],
            }
        legs_out.append(leg_data)

    return {
        "id": solo.id,
        "unit_id": solo.unit_id,
        "title": solo.title,
        "window_s": window_s,
        "state": state,
        "started_at": started_at,
        "remaining_s": remaining_s,
        "reason": reason,
        "error": error,
        "passed_before_date": passed_before_date,
        "legs": legs_out,
    }
