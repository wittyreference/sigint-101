# ABOUTME: Builds the /field command card rows from live receivers, logged events, and stamps.
# ABOUTME: Resolves each rack instrument by key; never invents or rebuilds a command from defaults.

from __future__ import annotations

from datetime import datetime, timezone

from .operate import INSTRUMENTS
from .operate import instrument as rack_instrument
from .stamps import STAMP_INSTRUMENTS, _recorded

# Maps each command-backed instrument key to:
#   (app.state receiver attr, its *_start event kind, its logged reception instrument key or None).
# Never resolve by inst.receiver (clock and health share sweeper) or by tool name (APRS and pager
# share multimon-ng).
COMMAND_SOURCES: dict[str, tuple[str, str, str | None]] = {
    "waterfall": ("sweeper", "spectrum_start", None),
    "ism": ("decoder", "decode_start", "ism"),
    "adsb": ("adsb", "adsb_start", "adsb"),
    "aprs": ("aprs", "aprs_start", "aprs"),
    "pager": ("pocsag", "pocsag_start", "pager"),
    "acars": ("acars", "acars_start", "acars"),
    "ais": ("ais", "ais_start", "ais"),
    "rds": ("rds", "rds_start", "rds"),
    "dmr": ("dmr", "dmr_start", "dmr"),
    "apt": ("apt", "apt_start", "apt"),
    "sstv": ("sstv", "sstv_start", "sstv"),
    "flock": ("flock", "flock_start", "flock"),
}

PROMPT_LINE = "Run it once in the rack to see its command."

MARAUDER_NOTE = (
    "Typed into Marauder's serial console over the Flipper's USB-UART bridge, "
    "not into a Linux terminal."
)

PAGER_LAW_NOTE = (
    "Run by hand, multimon-ng prints each page's words. The console drops them before anything is "
    "stored or shown: reading a carrier's page is a federal offence (18 USC 2511(1)(a)). "
    "Lesson 15 explains why."
)

_FIXED_SENTENCES: dict[str, str] = {
    "rayhunter": (
        "No command line. Rayhunter runs on the Orbic itself; "
        "the console only polls its web API over Wi-Fi, read-only."
    ),
    "flipper": (
        "No command line. The console reads the Flipper's identity in one short session "
        "on its USB serial CLI and closes it; nothing is left running."
    ),
    "clock": (
        'No command line. "ppm" is the console\'s own clock check, not a program: '
        "it captures about eight seconds of one FM station with rtl_fm and measures "
        "the crystal error against the 19 kHz stereo pilot. The console keeps no record "
        "of that capture's command."
    ),
    "health": (
        'No command line. "provenance" is the console\'s own health view, not a program: '
        "it gathers the radio's settings, the board's temperature and power state, and "
        "the reference level off the live sweep. The sweep's command, when the console "
        "has run it, is on the Waterfall row."
    ),
}


def _nonblank(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _utc_time(ts) -> datetime | None:
    if type(ts) not in (int, float):
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _tool_matches(payload: dict, current_tool: str, *, require_tool: bool = False) -> bool:
    named = payload.get("tool")
    if named is None or named == "":
        return not require_tool
    return named == current_tool


def _latest_logged(events: list, start_kind: str, reception_inst: str | None,
                   current_tool: str, *, require_tool: bool = False
                   ) -> tuple[str, datetime | None] | None:
    """Newest matching real command from events (already ordered oldest first by read_events)."""
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if _recorded(event) or _recorded(payload):
            continue
        kind = event.get("kind")
        matched = (
            (kind == "reception" and reception_inst is not None
             and payload.get("instrument") == reception_inst)
            or (kind == start_kind and payload.get("command_from") == "process")
        )
        if not matched:
            continue
        if not _tool_matches(payload, current_tool, require_tool=require_tool):
            continue
        cmd = _nonblank(payload.get("command"))
        if cmd is not None:
            return cmd, _utc_time(event.get("ts"))
    return None


def _badge_command(stamp: dict | None, current_tool: str) -> tuple[str, datetime | None] | None:
    if not isinstance(stamp, dict):
        return None
    provenance = stamp.get("provenance")
    if not isinstance(provenance, dict) or _recorded(provenance):
        return None
    if not _tool_matches(provenance, current_tool):
        return None
    cmd = _nonblank(provenance.get("command"))
    if cmd is None:
        return None
    return cmd, _utc_time(stamp.get("first_ts"))


def _row_note(key: str, tool: str) -> str | None:
    if key == "flock" and tool == "Marauder":
        return MARAUDER_NOTE
    if key == "pager":
        return PAGER_LAW_NOTE
    return None


def _sentence_for(key: str, app_state) -> str:
    if key == "lora":
        lora = getattr(app_state, "lora", None)
        port = getattr(lora, "port", 4403)
        return (
            "No command line. meshtasticd is a daemon that runs as its own service; "
            f"the console reads its API on port {port}. Stopping SIGINT 101 leaves it running."
        )
    return _FIXED_SENTENCES[key]


def build_field_rows(app_state, events: list, stamps: list) -> list[dict]:
    """Build the 17 ordered rows for /field without starting, claiming, or mutating any receiver."""
    flock = getattr(app_state, "flock", None)
    flock_tool = getattr(flock, "tool", None) or rack_instrument("flock").tool
    earned_stamps = {stamp["instrument"]: stamp for stamp in stamps if isinstance(stamp, dict)}
    rows = []

    for inst in INSTRUMENTS:
        label = "POCSAG" if inst.key == "pager" else inst.label
        tool = flock_tool if inst.key == "flock" else inst.tool

        if inst.key not in COMMAND_SOURCES:
            rows.append({
                "key": inst.key,
                "label": label,
                "tool": None,
                "kind": "sentence",
                "sentence": _sentence_for(inst.key, app_state),
                "command": None,
                "state": None,
                "state_label": None,
                "when": None,
                "prompt": None,
                "note": None,
            })
            continue

        attr, start_kind, reception_inst = COMMAND_SOURCES[inst.key]
        receiver = getattr(app_state, attr, None)
        live_fn = getattr(receiver, "live_command", None) if receiver is not None else None
        live_cmd = _nonblank(live_fn()) if callable(live_fn) else None
        note = _row_note(inst.key, tool)

        if live_cmd is not None:
            rows.append({
                "key": inst.key,
                "label": label,
                "tool": tool,
                "kind": "command",
                "sentence": None,
                "command": live_cmd,
                "state": "live",
                "state_label": "Running now",
                "when": None,
                "prompt": None,
                "note": note,
            })
            continue

        logged = _latest_logged(
            events, start_kind, reception_inst, tool, require_tool=(inst.key == "flock")
        )
        if logged is not None:
            cmd, when = logged
            rows.append({
                "key": inst.key,
                "label": label,
                "tool": tool,
                "kind": "command",
                "sentence": None,
                "command": cmd,
                "state": "last",
                "state_label": "Last run",
                "when": when,
                "prompt": None,
                "note": note,
            })
            continue

        badge = (_badge_command(earned_stamps.get(inst.key), tool)
                 if inst.key in STAMP_INSTRUMENTS else None)
        if badge is not None:
            cmd, when = badge
            rows.append({
                "key": inst.key,
                "label": label,
                "tool": tool,
                "kind": "command",
                "sentence": None,
                "command": cmd,
                "state": "stamp",
                "state_label": "First heard",
                "when": when,
                "prompt": None,
                "note": note,
            })
            continue

        rows.append({
            "key": inst.key,
            "label": label,
            "tool": tool,
            "kind": "command",
            "sentence": None,
            "command": None,
            "state": "prompt",
            "state_label": "No command on record",
            "when": None,
            "prompt": PROMPT_LINE,
            "note": note,
        })

    return rows
