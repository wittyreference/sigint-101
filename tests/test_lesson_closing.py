# ABOUTME: Tests for the closing "Leave the Garden" lesson pack (register 11c, L40): loader,
# ABOUTME: item lint, byte-identical M10 /field command sourcing, and self-reported completion.

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sigint101.app import create_app
from sigint101.childproc import command_line
from sigint101.events import log_event, read_events
from sigint101.lessons import LessonError, load_lesson_file, load_lessons, parse_lesson
from sigint101.progress import Progress
from sigint101.spectrum import DEFAULT_BIN
from sigint101.stamps import mint
from sigint101.units import unit_for_lesson
from tests.child_fakes import Child
from tests.test_app import _absent
from tests.test_field_card import _codes, _LiveStub, _row_html, _strict_factories
from tests.test_item_lint import lint_pack
from tests.test_lesson_answers import VERDICT_MAX_CHARS
from tests.test_lesson_reading_load import EXPLAIN_MAX_WORDS

REPO = Path(__file__).resolve().parent.parent
PACK_PATH = REPO / "lesson_packs" / "40-leave-the-garden.yaml"
FIELD_HTML = REPO / "sigint101" / "templates" / "field.html"
QUICKSTART_MD = REPO / "docs" / "quickstart.md"

STOP_CMD = "systemctl --user stop sigint101"
RESTART_CMD = "systemctl --user restart sigint101"
ALLOWED_SERVICE_CMDS = {STOP_CMD, RESTART_CMD}

# Tool binaries the console runs or could run — none may be hand-written in the closing pack YAML,
# because the M10 command must come dynamically from sigint101/fieldcard.py, never a static string.
FORBIDDEN_STATIC_TOOLS = (
    "rtl_433",
    "rtl_power",
    "rtl_fm",
    "dump1090",
    "multimon-ng",
    "acarsdec",
    "rtl_ais",
    "redsea",
    "dsd-fme",
    "satdump",
    "tcpdump",
    "sniffprobe",
    "nmcli",
    "sudo",
    "systemctl --user start",
    "journalctl",
)

SYSTEMCTL_RE = re.compile(
    r"\bsystemctl\s+--user\s+[a-zA-Z0-9_-]+\s+[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*"
)
CLI_FLAG_RE = re.compile(r"(?:^|\s)--?[a-zA-Z0-9][a-zA-Z0-9_-]*")
FAKE_DECODE_CLAIM_RE = re.compile(
    r"\b(?:console|page|screen)\s+(?:sees|saw|confirms|confirmed|verified)\s+(?:your|the)\s+"
    r"(?:terminal|decode|sensor)\b"
    r"|\byou\s+(?:will\s+see|saw)\s+an?\s+acurite\b"
    r"|\b(?:output|reads?|decodes?)\s+you\s+saw\b",
    re.I,
)
HARDCODED_ISM_FREQ_RE = re.compile(r"\b(?:315|433|868|915)(?:\.\d+)?\s*MHz\b", re.I)
CERTAIN_BUSY_RE = re.compile(r"\bthe\s+radio\s+is\s+busy\b", re.I)
TRANSMIT_OR_LAW_RE = re.compile(
    r"\b(?:18\s+USC|47\s+CFR|federal\s+offen[cs]e|transmit\s+on|key\s+the\s+transmitter)\b",
    re.I,
)


def _field_lesson_html(html: str) -> str:
    match = re.search(
        r'<div\b[^>]*data-field-lesson="ism"[^>]*>(.*?)</div>\s*</div>\s*</div>\s*</div>',
        html,
        re.S,
    )
    if match:
        return match.group(1)
    # Fallback: capture the data-field-lesson container block cleanly.
    start = html.find('data-field-lesson="ism"')
    assert start != -1, "missing data-field-lesson='ism' container in /lesson/40"
    end = html.find("</section>", start)
    return html[start:end]


def _check_closing_prose(text: str) -> list[str]:
    """Guard the closing-lesson claims and command honesty on any candidate prose."""
    issues: list[str] = []
    lower = text.lower()

    for tool in FORBIDDEN_STATIC_TOOLS:
        if tool.lower() in lower:
            issues.append(f"hand-written or disallowed tool/command '{tool}'")

    found_systemctl = set(SYSTEMCTL_RE.findall(text))
    unexpected = found_systemctl - ALLOWED_SERVICE_CMDS
    if unexpected:
        issues.append(f"unexpected systemctl command(s): {sorted(unexpected)}")
    if STOP_CMD not in found_systemctl:
        issues.append(f"missing field-card stop line: {STOP_CMD}")
    if RESTART_CMD not in found_systemctl:
        issues.append(f"missing quickstart restart line: {RESTART_CMD}")

    # Strip the two allowed systemctl commands before checking for stray CLI flags (e.g. -f, -F).
    stripped = text.replace(STOP_CMD, "").replace(RESTART_CMD, "")
    # Ignore human key-chord notation like Ctrl-C.
    stripped = re.sub(r"\bCtrl-[A-Za-z]\b", "", stripped)
    stray_flags = [m.group(0).strip() for m in CLI_FLAG_RE.finditer(stripped)]
    if stray_flags:
        issues.append(f"hand-written CLI flags in pack prose: {stray_flags}")

    if "terminal" not in lower:
        issues.append("missing Terminal instruction")
    if "keyboard" not in lower:
        issues.append("missing physical keyboard step")
    if "compare" not in lower or "rack" not in lower:
        issues.append("missing instruction to compare terminal output with what the rack showed")
    if FAKE_DECODE_CLAIM_RE.search(text):
        issues.append("claims or presumes a terminal decode the console cannot see")
    if HARDCODED_ISM_FREQ_RE.search(text):
        issues.append("hard-codes an ISM frequency the retuned command may not carry")
    if CERTAIN_BUSY_RE.search(text):
        issues.append("presents 'the radio is busy' as certain instead of 'may/can be busy'")
    if TRANSMIT_OR_LAW_RE.search(text):
        issues.append("mentions transmit or legal claim outside scope")
    return issues


# --- Both-directions claim probe (METHOD.md) --------------------------------------------------


def test_closing_prose_probe_rejects_defects_and_accepts_rewordings():
    good = (
        "Exit to the desktop and open Terminal on the physical keyboard. "
        f"Free the radio with {STOP_CMD}, run the Lesson 10 command shown above, "
        "and compare what your Terminal shows with the rack. "
        "The radio may be busy if the server still holds the dongle. "
        f"When done, bring the server back with {RESTART_CMD}."
    )
    assert _check_closing_prose(good) == []

    handwritten_cmd = good + " Run rtl_433 -f 433920000 -F json."
    assert any("rtl_433" in i for i in _check_closing_prose(handwritten_cmd))

    stray_flag = good + " Pass -M level to see SNR."
    assert any("CLI flags" in i for i in _check_closing_prose(stray_flag))

    wrong_restart = good.replace(RESTART_CMD, "systemctl --user start sigint101")
    assert any("quickstart restart line" in i for i in _check_closing_prose(wrong_restart))

    claims_decode = good + " The page confirms your terminal decode automatically."
    assert any("cannot see" in i for i in _check_closing_prose(claims_decode))

    presumes_output = good + " The output you saw came straight from the tool."
    assert any("cannot see" in i for i in _check_closing_prose(presumes_output))

    hardcoded_freq = good + " Wait for a 433 MHz sensor to chirp."
    assert any("frequency" in i for i in _check_closing_prose(hardcoded_freq))

    certain_busy = good + " The radio is busy whenever the server runs."
    assert any("may/can be busy" in i for i in _check_closing_prose(certain_busy))


# --- Loader, item lint, reading load, unit placement ------------------------------------------


def test_closing_pack_loads_lints_and_fits_word_caps():
    assert PACK_PATH.is_file(), f"missing closing pack: {PACK_PATH}"
    raw = yaml.safe_load(PACK_PATH.read_text(encoding="utf-8"))
    lesson = load_lesson_file(PACK_PATH)

    assert lesson.id == 40
    assert lesson.requires_tx is False
    assert lesson.hardware == []
    assert lesson.panels == []
    assert lesson.field_instrument == "ism"
    assert lint_pack(raw) == []
    assert len(lesson.stages["explain"].split()) <= EXPLAIN_MAX_WORDS
    assert len(lesson.verdict) <= VERDICT_MAX_CHARS

    loaded = load_lessons(PACK_PATH.parent)
    assert 40 in loaded
    assert unit_for_lesson(40) is not None


def test_pack_loader_refuses_unknown_or_non_command_field_instrument():
    base = {
        "id": 99,
        "slug": "bad-field",
        "title": "Bad Field",
        "concept": "test",
        "stages": {"predict": "p", "operate": "o", "observe": "b", "explain": "e"},
    }
    for bad in ("bogus", "lora", "clock", "health", 123):
        with pytest.raises(LessonError, match="field_instrument"):
            parse_lesson({**base, "field_instrument": bad})


def test_closing_pack_uses_documented_service_lines_and_no_invented_commands():
    assert STOP_CMD in FIELD_HTML.read_text(encoding="utf-8")
    assert RESTART_CMD in QUICKSTART_MD.read_text(encoding="utf-8")

    raw = yaml.safe_load(PACK_PATH.read_text(encoding="utf-8"))
    lesson = load_lesson_file(PACK_PATH)
    all_prose = " ".join([
        lesson.title,
        lesson.concept,
        *lesson.stages.values(),
        lesson.verdict,
        *(c["label"] for c in lesson.choices),
    ])
    assert _check_closing_prose(all_prose) == []

    # Raw YAML itself (even outside parsed strings) must never hard-code any receiver tool command.
    raw_text = PACK_PATH.read_text(encoding="utf-8")
    for tool in FORBIDDEN_STATIC_TOOLS:
        assert tool.lower() not in raw_text.lower(), f"raw YAML must not contain '{tool}'"
    assert raw.get("requires_tx") is False


# --- Dynamic /field parity (byte-for-byte across every state) ---------------------------------


def _closing_client(tmp_path, **factory_overrides):
    def _sweeper():
        stub = _LiveStub()
        stub.bin_size = DEFAULT_BIN
        return stub

    factories = _strict_factories({"sweeper_factory": _sweeper, **factory_overrides})
    app = create_app(
        pack_dir=PACK_PATH.parent,
        data_dir=tmp_path,
        probe=_absent,
        **factories,
    )
    return TestClient(app)


def _open_unsealed_closing(client) -> str:
    """Commit the closing lesson's predict gate and return the unsealed /lesson/40 HTML."""
    lesson = client.app.state.lessons[40]
    first_choice = lesson.choices[0]["key"]
    resp = client.post(
        "/api/predict?lesson_id=40",
        data={"prediction": first_choice},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get("/lesson/40")
    assert page.status_code == 200
    return page.text


def test_closing_lesson_seals_operate_before_predict_and_completes_by_self_report(tmp_path):
    with _closing_client(tmp_path) as client:
        sealed = client.get("/lesson/40")
        assert sealed.status_code == 200
        assert 'data-field-lesson="ism"' not in sealed.text
        assert 'stage-panel" data-stage="operate"' not in sealed.text
        assert STOP_CMD not in sealed.text

        unsealed = _open_unsealed_closing(client)
        assert 'data-field-lesson="ism"' in unsealed
        assert "Complete lesson" in unsealed
        assert 'data-complete data-lesson="40"' in unsealed

        done = client.post("/api/complete?lesson_id=40")
        assert done.status_code == 200
        assert done.json()["completed"] is True


def test_closing_lesson_when_no_m10_command_on_record_shows_rack_first_prompt(tmp_path):
    with _closing_client(tmp_path) as client:
        field_html = client.get("/field").text
        lesson_html = _open_unsealed_closing(client)

    assert _codes(_row_html(field_html, "ism")) == []
    box = _field_lesson_html(lesson_html)
    assert _codes(box) == []
    assert "no command on record" in unescape(box).lower()
    assert "run it once in the rack first" in unescape(box).lower()


def test_closing_lesson_live_m10_command_matches_field_card_byte_for_byte(tmp_path):
    proc = Child(["rtl_433", "-f", "433920000", "-F", "json", "-M", "level", "-d", "0"])
    expected_cmd = command_line(proc)
    with _closing_client(tmp_path, decoder_factory=lambda: _LiveStub(proc=proc)) as client:
        field_html = client.get("/field").text
        lesson_html = _open_unsealed_closing(client)

    field_cmds = _codes(_row_html(field_html, "ism"))
    lesson_cmds = _codes(_field_lesson_html(lesson_html))
    assert field_cmds == [expected_cmd]
    assert lesson_cmds == field_cmds
    assert "Running now" in _field_lesson_html(lesson_html)


def test_closing_lesson_logged_and_reception_m10_command_matches_field_card_byte_for_byte(tmp_path):
    events_path = tmp_path / "events.jsonl"
    old_cmd = "rtl_433 -f 433920000 -F json -M level -d 0"
    new_cmd = "rtl_433 -f 915000000 -F json -M level -d 0"
    log_event(
        events_path,
        "decode_start",
        {"command": old_cmd, "command_from": "process"},
        clock=lambda: 1700000300.0,
    )
    log_event(
        events_path,
        "reception",
        {
            "instrument": "ism",
            "tool": "rtl_433",
            "command": new_cmd,
            "freq_hz": 915_000_000,
            "radio": {"kind": "rtl-sdr", "index": 0, "serial": None},
            "item": {"model": "Acurite-Tower"},
        },
        clock=lambda: 1700000400.0,
    )
    with _closing_client(tmp_path) as client:
        field_html = client.get("/field").text
        lesson_html = _open_unsealed_closing(client)

    field_cmds = _codes(_row_html(field_html, "ism"))
    lesson_cmds = _codes(_field_lesson_html(lesson_html))
    assert field_cmds == [new_cmd]
    assert lesson_cmds == field_cmds
    assert "Last run" in _field_lesson_html(lesson_html)


def test_closing_lesson_stamp_fallback_and_replay_exclusion_match_field_card(tmp_path):
    # Recorded/replay reception must be ignored, just like /field.
    events_path = tmp_path / "events.jsonl"
    log_event(
        events_path,
        "reception",
        {
            "instrument": "ism",
            "tool": "rtl_433",
            "command": "rtl_433 -r fake.cu8",
            "replay": True,
            "freq_hz": 433_920_000,
            "radio": {"kind": "rtl-sdr", "index": 0, "serial": None},
            "item": {"model": "Acurite-Tower"},
        },
        clock=lambda: 1700000500.0,
    )
    # Seed a real earned reception stamp so both /field and /lesson/40 fall back to First heard.
    badge_cmd = "rtl_433 -f 433920000 -F json -M level -d 0"
    stamp = mint({
        "ts": 1700000600.0,
        "kind": "reception",
        "event_id": "closing-stamp-1",
        "payload": {
            "instrument": "ism",
            "tool": "rtl_433",
            "freq_hz": 433_920_000,
            "command": badge_cmd,
            "radio": {"kind": "rtl-sdr", "index": 0, "serial": "00000001"},
            "item": {"model": "Acurite-Tower"},
        },
    })
    assert stamp is not None
    prog = Progress(tmp_path / "progress.db")
    prog.record_stamps((stamp,))
    prog.close()

    with _closing_client(tmp_path) as client:
        field_html = client.get("/field").text
        lesson_html = _open_unsealed_closing(client)

    field_cmds = _codes(_row_html(field_html, "ism"))
    lesson_cmds = _codes(_field_lesson_html(lesson_html))
    assert field_cmds == [badge_cmd]
    assert lesson_cmds == field_cmds
    assert "First heard" in _field_lesson_html(lesson_html)


def test_operate_places_field_command_box_above_instructions(tmp_path):
    # FIX 1: the field command box must lead the operate stage so the learner sees the command
    # (or rack-first prompt) immediately at scrollTop=0 instead of hidden below the fold.
    events_path = tmp_path / "events.jsonl"
    log_event(
        events_path,
        "decode_start",
        {"command": "rtl_433 -f 433920000 -F json -M level -d 0", "command_from": "process"},
        clock=lambda: 1700000700.0,
    )
    with _closing_client(tmp_path) as client:
        html = _open_unsealed_closing(client)

    op_start = html.index('stage-panel" data-stage="operate"')
    op_section = html[op_start:html.index("</section>", op_start)]
    assert op_section.index('data-field-lesson="ism"') < op_section.index(STOP_CMD)


def test_closing_lesson_ignores_other_instruments_wrong_tool_and_non_process_starts(tmp_path):
    # FIX 2 (isolation): unrelated instruments, wrong tool names, non-process starts, and replays
    # must never leak into L40's ISM command box.
    events_path = tmp_path / "events.jsonl"
    log_event(events_path, "reception", {
        "instrument": "adsb", "tool": "dump1090", "command": "dump1090 --net",
        "freq_hz": 1_090_000_000, "radio": {"kind": "rtl-sdr", "index": 0, "serial": None},
        "item": {"icao": "abc123"},
    }, clock=lambda: 1700000800.0)
    log_event(events_path, "aprs_start", {
        "command": "rtl_fm -f 144390000 | multimon-ng -a AFSK1200", "command_from": "process",
    }, clock=lambda: 1700000810.0)
    log_event(events_path, "reception", {
        "instrument": "ism", "tool": "dump1090", "command": "dump1090 --wrong-tool",
        "freq_hz": 433_920_000, "radio": {"kind": "rtl-sdr", "index": 0, "serial": None},
        "item": {"model": "Wrong"},
    }, clock=lambda: 1700000820.0)
    log_event(events_path, "decode_start", {
        "command": "rtl_433 --from-config", "command_from": "config",
    }, clock=lambda: 1700000830.0)
    log_event(events_path, "reception", {
        "instrument": "ism", "tool": "rtl_433", "command": "rtl_433 -r replay.cu8",
        "replay": True, "freq_hz": 433_920_000,
        "radio": {"kind": "rtl-sdr", "index": 0, "serial": None},
        "item": {"model": "Replay"},
    }, clock=lambda: 1700000840.0)

    with _closing_client(tmp_path) as client:
        html = _open_unsealed_closing(client)

    box = _field_lesson_html(html)
    assert _codes(box) == []
    assert "run it once in the rack first" in unescape(box).lower()


def test_closing_lesson_get_adds_no_writes_beyond_shared_lesson_view_side_effects(tmp_path):
    # FIX 2: GET /lesson/40 runs only the shared lesson_view side effects every lesson page runs
    # (mark_opened, enroll_recall, and _restore_lesson_tuning) and adds zero extra writes, events,
    # rack-place changes, or receiver starts from reading the field command.
    prog = Progress(tmp_path / "progress.db")
    prog.record_place("rack", "adsb")
    prog.close()

    def _sweeper():
        stub = _LiveStub()
        stub.bin_size = DEFAULT_BIN
        return stub

    stubs = {name: fn() for name, fn in _strict_factories({"sweeper_factory": _sweeper}).items()}
    factories = {name: (lambda s=stub: s) for name, stub in stubs.items()}
    app = create_app(
        pack_dir=PACK_PATH.parent,
        data_dir=tmp_path,
        probe=_absent,
        **factories,
    )
    with TestClient(app) as client:
        _open_unsealed_closing(client)
        events_before = read_events(tmp_path / "events.jsonl")
        stamps_before = client.app.state.progress.stamps()
        replays_before = client.app.state.progress.replays_seen()

        for _ in range(3):
            resp = client.get("/lesson/40")
            assert resp.status_code == 200

        assert read_events(tmp_path / "events.jsonl") == events_before
        assert client.app.state.progress.stamps() == stamps_before
        assert client.app.state.progress.replays_seen() == replays_before
        assert client.app.state.progress.get_place("rack") == "adsb"
        assert client.app.state.progress.get(40)["opened"] == 1
        assert all(not stub.running for stub in stubs.values())
