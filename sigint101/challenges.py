# ABOUTME: Tier B live-find challenge catalog, provenance-backed qualification, and state machine.
# ABOUTME: Frozen to receiving units with real stamp instruments; only stamps.mint() passes a find.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from . import operate, stamps
from .stamps import STAMP_INSTRUMENTS
from .units import UNITS

# Instruments that must never appear in a slice-1 live-find row: APT has no live NOAA signal,
# and flock/rayhunter are heuristic counter-surveillance alerts that never mint reception stamps.
FORBIDDEN_INSTRUMENTS = ("apt", "flock", "rayhunter")


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


def qualifies(challenge: Challenge, attempt, stamp: dict | None) -> bool:
    """True when a minted stamp matches an allowed instrument inside the inclusive window."""
    if not stamp or not attempt:
        return False
    started_at, window_s = _attempt_bounds(attempt)
    first_ts = stamp.get("first_ts")
    if type(first_ts) not in (int, float):
        return False
    return (
        stamp.get("instrument") in challenge.allowed
        and started_at <= first_ts <= started_at + window_s
    )


def evaluate(challenge: Challenge, attempt, event) -> dict | None:
    """Mint a reception event and return its stamp only if it passes `challenge` and `attempt`."""
    stamp = stamps.mint(event)
    if stamp is None or not qualifies(challenge, attempt, stamp):
        return None
    return stamp


def status(challenge: Challenge, attempt: dict | None, latest_pass: dict | None,
           now: float) -> dict:
    """Compute the challenge's current state, clamped remaining seconds, and expiry reason."""
    if attempt is None:
        return {
            "state": "idle",
            "started_at": None,
            "window_s": challenge.window_s,
            "remaining_s": 0,
            "reason": None,
        }
    started_at, window_s = _attempt_bounds(attempt)
    if latest_pass is not None and latest_pass.get("started_at") == started_at:
        return {
            "state": "passed",
            "started_at": started_at,
            "window_s": window_s,
            "remaining_s": 0,
            "reason": None,
        }
    if started_at <= now <= started_at + window_s:
        remaining = min(max(started_at + window_s - now, 0.0), float(window_s))
        return {
            "state": "active",
            "started_at": started_at,
            "window_s": window_s,
            "remaining_s": round(remaining, 3),
            "reason": None,
        }
    return {
        "state": "expired",
        "started_at": started_at,
        "window_s": window_s,
        "remaining_s": 0,
        "reason": "clock_moved" if now < started_at else None,
    }


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
