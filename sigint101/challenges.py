# ABOUTME: Tier B live-find challenge catalog, provenance-backed qualification, and state machine.
# ABOUTME: Frozen to receiving units with real stamp instruments; only stamps.mint() passes a find.

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from . import operate, stamps
from .stamps import STAMP_INSTRUMENTS
from .units import UNITS

# Instruments that must never appear in a slice-1 live-find row: APT has no live NOAA signal,
# and flock/rayhunter are heuristic counter-surveillance alerts that never mint reception stamps.
FORBIDDEN_INSTRUMENTS = ("apt", "flock", "rayhunter")

CREDIT_MODES = ("live_reception", "replay_answer")
CreditMode = Literal["live_reception", "replay_answer"]
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BASE_ATTEMPT_COLUMNS = frozenset({"started_at", "window_s"})


@dataclass(frozen=True)
class AttemptKind:
    """Descriptor for a timed attempt kind shared by live finds, solos, and replay benches."""

    kind: str
    attempts_table: str
    passes_table: str
    key_column: str
    slot_column: str | None
    start_event: str
    pass_event: str
    credit: CreditMode
    attempt_columns: tuple[str, ...] = ()
    pass_columns: tuple[str, ...] = ("passed_at", "instrument", "provenance", "digest")
    json_columns: tuple[str, ...] | None = None
    event_key_field: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not _IDENT_RE.match(self.kind):
            raise ValueError(f"invalid attempt kind {self.kind!r}")
        if self.credit not in CREDIT_MODES:
            raise ValueError(
                f"attempt kind {self.kind!r} credit must be one of {CREDIT_MODES}, "
                f"got {self.credit!r}"
            )
        for field_name in ("attempts_table", "passes_table", "key_column"):
            val = getattr(self, field_name)
            if not isinstance(val, str) or not _IDENT_RE.match(val):
                raise ValueError(f"invalid SQL identifier for {field_name}: {val!r}")
        if self.slot_column is not None:
            if not isinstance(self.slot_column, str) or not _IDENT_RE.match(self.slot_column):
                raise ValueError(f"invalid SQL identifier for slot_column: {self.slot_column!r}")
            if self.slot_column == self.key_column or self.slot_column in _BASE_ATTEMPT_COLUMNS:
                raise ValueError(f"slot_column {self.slot_column!r} collides with base columns")
        for field_name in ("start_event", "pass_event"):
            val = getattr(self, field_name)
            if not isinstance(val, str) or not _IDENT_RE.match(val):
                raise ValueError(f"invalid event kind for {field_name}: {val!r}")
        if self.event_key_field:
            if not isinstance(self.event_key_field, str) or not _IDENT_RE.match(
                self.event_key_field
            ):
                raise ValueError(f"invalid event_key_field: {self.event_key_field!r}")
        reserved_attempt = _BASE_ATTEMPT_COLUMNS | {self.key_column}
        seen_attempt: set[str] = set()
        for col in self.attempt_columns:
            if not isinstance(col, str) or not _IDENT_RE.match(col):
                raise ValueError(f"invalid attempt column {col!r}")
            if col in reserved_attempt or col in seen_attempt:
                raise ValueError(f"duplicate or reserved attempt column {col!r}")
            seen_attempt.add(col)
        if not self.pass_columns:
            raise ValueError(f"attempt kind {self.kind!r} requires at least one pass_column")
        reserved_pass = {self.key_column, "started_at"}
        if self.slot_column is not None:
            reserved_pass.add(self.slot_column)
        seen_pass: set[str] = set()
        for col in self.pass_columns:
            if not isinstance(col, str) or not _IDENT_RE.match(col):
                raise ValueError(f"invalid pass column {col!r}")
            if col in reserved_pass or col in seen_pass:
                raise ValueError(f"duplicate or reserved pass column {col!r}")
            seen_pass.add(col)
        if "passed_at" not in seen_pass:
            raise ValueError(f"attempt kind {self.kind!r} pass_columns must include 'passed_at'")
        if self.json_columns is not None:
            for col in self.json_columns:
                if col not in seen_pass:
                    raise ValueError(f"json_column {col!r} is not in pass_columns")

    @property
    def payload_key(self) -> str:
        """Event payload key name (defaults to key_column when event_key_field is unset)."""
        return self.event_key_field or self.key_column

    @property
    def display_label(self) -> str:
        """Human-readable label used in error messages for this attempt kind."""
        return self.label or self.kind

    @property
    def json_pass_columns(self) -> tuple[str, ...]:
        """Pass columns serialized as JSON text in SQLite."""
        if self.json_columns is not None:
            return self.json_columns
        return ("provenance",) if "provenance" in self.pass_columns else ()

    def replay_flags(self) -> dict:
        """Return replay flags derived solely from `credit`; never passed by callers."""
        return {"replay": True, "source": "replay"} if self.credit == "replay_answer" else {}


CHALLENGE_KIND = AttemptKind(
    kind="challenge",
    attempts_table="challenges",
    passes_table="challenge_passes",
    key_column="unit_id",
    slot_column=None,
    start_event="challenge_start",
    pass_event="challenge_pass",
    credit="live_reception",
    event_key_field="unit",
    label="live find",
)


@dataclass(frozen=True)
class Challenge:
    """One timed per-unit live-find challenge: any real reception on an allowed instrument."""

    unit_id: int
    allowed: tuple[str, ...]
    window_s: int
    prompt: str


CHALLENGES: tuple[Challenge, ...] = (
    Challenge(
        unit_id=4,
        allowed=("ism", "rds", "lora"),
        window_s=900,
        prompt="Log a live ISM sensor, RDS station or LoRa packet within 15 minutes.",
    ),
    Challenge(
        unit_id=5,
        allowed=("adsb", "aprs", "pager", "acars", "ais", "dmr"),
        window_s=900,
        prompt="Log a live ADS-B, APRS, pager, ACARS, AIS or DMR/P25 reception within 15 minutes.",
    ),
    Challenge(
        unit_id=9,
        allowed=("lora",),
        window_s=900,
        prompt="Log a live LoRa packet from another Meshtastic node within 15 minutes.",
    ),
)


def validate_catalog(catalog: tuple[Challenge, ...]) -> None:
    """Validate a challenge catalog; raises ValueError (never assert) on any rule breach."""
    known_units = {unit.id for unit in UNITS}
    seen_units: set[int] = set()
    for row in catalog:
        if row.unit_id not in known_units:
            raise ValueError(f"challenge unit {row.unit_id} is not in UNITS")
        if row.unit_id in seen_units:
            raise ValueError(f"duplicate challenge for unit {row.unit_id}")
        seen_units.add(row.unit_id)
        if not row.allowed:
            raise ValueError(f"challenge for unit {row.unit_id} has no allowed instruments")
        if type(row.window_s) is not int or row.window_s <= 0:
            raise ValueError(
                f"challenge for unit {row.unit_id} window_s must be a positive int, "
                f"got {row.window_s!r}"
            )
        if not isinstance(row.prompt, str) or not row.prompt.strip():
            raise ValueError(f"challenge for unit {row.unit_id} has a blank prompt")
        for key in row.allowed:
            if key in FORBIDDEN_INSTRUMENTS:
                raise ValueError(
                    f"challenge for unit {row.unit_id} forbids instrument {key!r}"
                )
            if key not in STAMP_INSTRUMENTS:
                raise ValueError(
                    f"challenge for unit {row.unit_id} key {key!r} is not in STAMP_INSTRUMENTS"
                )
            if operate.instrument(key) is None:
                raise ValueError(
                    f"challenge for unit {row.unit_id} key {key!r} is not a rack instrument"
                )


validate_catalog(CHALLENGES)
_BY_UNIT = {row.unit_id: row for row in CHALLENGES}


def challenge_for(unit_id: int) -> Challenge | None:
    """Return the live-find Challenge for `unit_id`, or None if the unit has no live find."""
    return _BY_UNIT.get(unit_id)


def rack_label(key: str) -> str:
    """The rack picker's own label for an instrument key (so `pager` reads POCSAG)."""
    if key == "pager":
        return "POCSAG"
    inst = operate.instrument(key)
    return inst.label if inst is not None else key.upper()


def _attempt_bounds(attempt) -> tuple[float, int]:
    if isinstance(attempt, dict):
        return attempt["started_at"], attempt["window_s"]
    return attempt.started_at, attempt.window_s


def in_window(attempt, stamp: dict | None) -> bool:
    """True when a minted stamp's first_ts falls inside the attempt's inclusive window."""
    if not stamp or not attempt:
        return False
    started_at, window_s = _attempt_bounds(attempt)
    first_ts = stamp.get("first_ts")
    if type(first_ts) not in (int, float):
        return False
    return started_at <= first_ts <= started_at + window_s


def qualifies(challenge: Challenge, attempt, stamp: dict | None) -> bool:
    """True when a minted stamp matches an allowed instrument inside the inclusive window."""
    return (
        bool(stamp)
        and stamp.get("instrument") in challenge.allowed
        and in_window(attempt, stamp)
    )


def evaluate(challenge: Challenge, attempt, event) -> dict | None:
    """Mint a reception event and return its stamp only if it passes `challenge` and `attempt`."""
    stamp = stamps.mint(event)
    if stamp is None or not qualifies(challenge, attempt, stamp):
        return None
    return stamp


def attempt_status(
    window_s: int,
    attempt: dict | None,
    latest_pass: dict | None,
    now: float,
    *,
    attempt_pass: dict | None = None,
) -> dict:
    """Compute an attempt's current state, clamped remaining seconds, and expiry reason."""
    if attempt is None:
        return {
            "state": "idle",
            "started_at": None,
            "window_s": window_s,
            "remaining_s": 0,
            "reason": None,
        }
    started_at, attempt_window_s = _attempt_bounds(attempt)
    own_pass = attempt_pass if attempt_pass is not None else latest_pass
    if own_pass is not None and own_pass.get("started_at") == started_at:
        return {
            "state": "passed",
            "started_at": started_at,
            "window_s": attempt_window_s,
            "remaining_s": 0,
            "reason": None,
        }
    if started_at <= now <= started_at + attempt_window_s:
        remaining = min(max(started_at + attempt_window_s - now, 0.0), float(attempt_window_s))
        return {
            "state": "active",
            "started_at": started_at,
            "window_s": attempt_window_s,
            "remaining_s": round(remaining, 3),
            "reason": None,
        }
    return {
        "state": "expired",
        "started_at": started_at,
        "window_s": attempt_window_s,
        "remaining_s": 0,
        "reason": "clock_moved" if now < started_at else None,
    }


def status(challenge: Challenge, attempt: dict | None, latest_pass: dict | None,
           now: float, *, attempt_pass: dict | None = None) -> dict:
    """Compute the challenge's current state, clamped remaining seconds, and expiry reason."""
    return attempt_status(
        challenge.window_s, attempt, latest_pass, now, attempt_pass=attempt_pass,
    )


def format_pass_summary(pass_row: dict | None) -> dict | None:
    """Project a stored challenge_passes row into the public API's latest_pass object."""
    if not pass_row:
        return None
    prov = pass_row.get("provenance") or {}
    return {
        "started_at": pass_row["started_at"],
        "passed_at": pass_row["passed_at"],
        "instrument": pass_row["instrument"],
        "tool": prov.get("tool"),
        "freq_hz": prov.get("freq_hz"),
        "digest": pass_row["digest"],
    }


def format_freq(freq_hz) -> str | None:
    """Format a frequency (single Hz int or AIS Hz list) as MHz, or None when absent."""
    if isinstance(freq_hz, list):
        parts = [f"{hz / 1_000_000:.3f}".rstrip("0").rstrip(".") for hz in freq_hz if hz]
        return f"{' / '.join(parts)} MHz" if parts else None
    if type(freq_hz) in (int, float) and freq_hz > 0:
        mhz = f"{freq_hz / 1_000_000:.3f}".rstrip("0").rstrip(".")
        return f"{mhz} MHz"
    return None


def format_evidence(payload: dict) -> str:
    """Single-line status/evidence text for the challenge screen."""
    if payload.get("error"):
        return str(payload["error"])
    latest = payload.get("latest_pass")
    state = payload.get("state")
    if state == "passed" and latest:
        return f"Found: {_pass_parts(latest)}"
    if payload.get("reason") == "clock_moved":
        return "The clock changed. Start again."
    if latest:
        return f"Found before: {_pass_parts(latest)}"
    return ""


def _pass_parts(latest: dict) -> str:
    label = rack_label(latest["instrument"])
    parts = [label]
    if latest.get("tool"):
        parts.append(str(latest["tool"]))
    freq = format_freq(latest.get("freq_hz"))
    if freq:
        parts.append(freq)
    passed_at = latest.get("passed_at")
    if type(passed_at) in (int, float):
        stamp = datetime.fromtimestamp(passed_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        parts.append(stamp)
    return " · ".join(parts)
