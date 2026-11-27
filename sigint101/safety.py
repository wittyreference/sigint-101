# ABOUTME: Holds the course's one hardware-protection notice (antenna or 50-ohm load before TX).
# ABOUTME: Keeps the notice non-blocking and authorizes one radio call per rack Send request.

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from secrets import compare_digest

# One plain sentence (<= 20 words), shown wherever the LoRa radio is live or the learner swaps
# antennas. Hardware protection for the PA, never a legal gate or a disabled control (D16).
ANTENNA_NOTICE = (
    "LoRa transmits on its own: keep an antenna or 50-ohm load on the jack when powered."
)

# Lessons where the learner swaps antennas on the SDR jack (M6 Resonance, M7 Build, Aim & Clean Up).
ANTENNA_LESSONS = frozenset({6, 7})
_rack_authorized = ContextVar("rack_lora_authorized", default=False)


@contextmanager
def rack_lora_send(request, permit):
    """Authorize one call in the rack POST's request context."""
    supplied = request.headers.get("X-LoRa-Send", "")
    if (request.method != "POST" or getattr(request.scope.get("route"), "name", None) != "lora_send"
            or not compare_digest(supplied.encode(), permit.encode())):
        raise PermissionError("Use Send in the LoRa rack.")
    token = _rack_authorized.set(True)
    try:
        yield
    finally:
        _rack_authorized.reset(token)


def require_rack_send() -> None:
    if not _rack_authorized.get():
        raise PermissionError("Use Send in the LoRa rack.")


def send_lora_text(interface, text: str) -> dict:
    """Hand one valid UTF-8 text to the radio, requesting its broadcast acknowledgement."""
    if not 1 <= len(text.encode("utf-8")) <= 233:
        raise ValueError("LoRa text must be 1 to 233 UTF-8 bytes.")
    require_rack_send()
    _rack_authorized.set(False)
    packet = interface.sendText(text, destinationId="^all", wantAck=True, channelIndex=0)
    return {"packet_id": packet.id, "notice": ANTENNA_NOTICE}


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
