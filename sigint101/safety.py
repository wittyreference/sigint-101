# ABOUTME: Holds the course's one hardware-protection notice (antenna or 50-ohm load before TX).
# ABOUTME: Non-blocking text only (D16): never disables controls; sole module allowed TX verbs.

from __future__ import annotations

# One plain sentence (<= 20 words), shown wherever the LoRa radio is live or the learner swaps
# antennas. Hardware protection for the PA, never a legal gate or a disabled control (D16).
ANTENNA_NOTICE = (
    "LoRa transmits on its own: keep an antenna or 50-ohm load on the jack when powered."
)

# Lessons where the learner swaps antennas on the SDR jack (M6 Resonance, M7 Build, Aim & Clean Up).
ANTENNA_LESSONS = frozenset({6, 7})


def notice_for(*, lesson=None, instrument=None) -> str | None:
    """Return the antenna notice text when a lesson or rack view needs it, else None.

    Returns text only, never a boolean or flag, so templates render it directly without gating any
    control behind it.
    """
    if lesson is not None:
        if getattr(lesson, "requires_tx", False):
            return ANTENNA_NOTICE
        panels = getattr(lesson, "panels", None) or ()
        if getattr(lesson, "uses_lora", False) or "lora" in panels:
            return ANTENNA_NOTICE
        if getattr(lesson, "id", None) in ANTENNA_LESSONS:
            return ANTENNA_NOTICE
    if instrument is not None:
        if instrument == "lora":
            return ANTENNA_NOTICE
        if (getattr(instrument, "key", None) == "lora"
                or getattr(instrument, "radio", None) == "lora"):
            return ANTENNA_NOTICE
    return None
