# ABOUTME: Tests for the /field command field card: live/logged/badge resolution, isolation,
# ABOUTME: Flock source honesty, five sentence rows, read-only guarantee, and CSS font/wrap rules.

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import pytest

from sigint101.childproc import command_line
from sigint101.events import log_event, read_events
from sigint101.flock import FLOCK_SOURCE_ENV, FLOCK_SOURCE_MARAUDER, configured_flock_detector
from sigint101.operate import INSTRUMENTS
from sigint101.progress import Progress
from sigint101.spectrum import Sweeper
from sigint101.stamps import mint
from tests.child_fakes import Child
from tests.test_app import _client, _present
from tests.test_app_radio_routes import ROUTES
from tests.test_templates import APP_CSS, _css_rule, _font_px

REPO = Path(__file__).resolve().parent.parent
FIELD_HTML = REPO / "sigint101" / "templates" / "field.html"

PROMPT_LINE = "run it once in the rack to see its command"
SERVICE_STOP = "systemctl --user stop sigint101"
SERVICE_START = "systemctl --user start sigint101"
MARAUDER_NOTE = (
    "Typed into Marauder's serial console over the Flipper's USB-UART bridge, "
    "not into a Linux terminal."
)
PAGER_LAW = (
    "Run by hand, multimon-ng prints each page's words. The console drops them before anything is "
    "stored or shown: reading a carrier's page is a federal offence (18 USC 2511(1)(a)). "
    "Lesson 15 explains why."
)


def _row_html(html: str, key: str) -> str:
    pattern = rf'<li\b[^>]*data-field-instrument="{re.escape(key)}"[^>]*>(.*?)</li>'
    match = re.search(pattern, html, re.S)
    assert match, f"missing row for instrument '{key}'"
    return match.group(1)


def _codes(fragment: str) -> list[str]:
    return [unescape(m) for m in re.findall(r"<code\b[^>]*>(.*?)</code>", fragment, re.S)]


class _LiveStub:
    """Stand-in receiver that exposes a fixed live_command() and raises on any mutating call."""

    def __init__(self, *, proc=None, running=False, tool=None, port=4403):
        self._proc = proc
        self._running = running if proc is None else (proc.poll() is None)
        if tool is not None:
            self.tool = tool
        self.port = port
        self.low = 88_000_000
        self.high = 108_000_000
        self.freq_hz = 433_920_000

    @property
    def running(self):
        return self._running

    def live_command(self):
        if self._proc is not None and self._proc.poll() is None:
            return command_line(self._proc)
        return None

    def command(self):
        raise AssertionError("GET /field must never call .command()")

    def start(self):
        raise AssertionError("GET /field must never call .start()")

    def release(self):
        raise AssertionError("GET /field must never call .release()")

    def resume(self):
        raise AssertionError("GET /field must never call .resume()")

    def retune(self, *_args, **_kwargs):
        raise AssertionError("GET /field must never call .retune()")

    def stop(self):
        pass

    def shutdown(self):
        pass


def _strict_factories(overrides=None):
    overrides = overrides or {}
    factories = {
        "sweeper_factory": lambda: _LiveStub(),
        "decoder_factory": lambda: _LiveStub(),
        "lora_factory": lambda: _LiveStub(port=4403),
        "rds_factory": lambda: _LiveStub(),
        "adsb_factory": lambda: _LiveStub(),
        "aprs_factory": lambda: _LiveStub(),
        "pocsag_factory": lambda: _LiveStub(),
        "acars_factory": lambda: _LiveStub(),
        "ais_factory": lambda: _LiveStub(),
        "dmr_factory": lambda: _LiveStub(),
        "apt_factory": lambda: _LiveStub(),
        "sstv_factory": lambda: _LiveStub(),
        "flock_factory": lambda: _LiveStub(tool="tcpdump"),
        "rayhunter_factory": lambda: _LiveStub(),
        "flipper_factory": lambda: _LiveStub(),
    }
    factories.update(overrides)
    return factories


def _all_start_routes(tmp_path):
    """The 12 command-backed API routes with real decoders backed by Child fakes."""
    items = [
        ("spectrum", "/api/spectrum", "sweeper_factory",
         lambda: Sweeper(spawn=lambda cmd: Child(cmd), radio_present=_present)),
    ]
    for path, factory_kw, build, _key, kind in ROUTES:
        argv = [kind, "--sample-flag", "1"]

        def _make(b=build, a=argv, t=tmp_path / kind):
            return b(lambda *_: Child(a), t)

        items.append((kind, path, factory_kw, _make))
    return items


def test_rows_match_all_17_instruments_in_order_and_never_run_shows_prompt(tmp_path):
    with _client(tmp_path, **_strict_factories()) as client:
        resp = client.get("/field")
    assert resp.status_code == 200
    html = resp.text

    found_keys = re.findall(r'data-field-instrument="([^"]+)"', html)
    assert found_keys == [inst.key for inst in INSTRUMENTS]
    assert len(found_keys) == 17

    # Back link goes to /operate ("Rack").
    assert 'href="/operate"' in html
    assert "Rack" in html

    # Freeing the radio entry pairs stop and start.
    assert SERVICE_STOP in html
    assert SERVICE_START in html

    # All 12 command rows show their tool, the prompt line, and no <code>.
    sentence_keys = {"lora", "rayhunter", "flipper", "clock", "health"}
    for inst in INSTRUMENTS:
        row = _row_html(html, inst.key)
        expected_label = "POCSAG" if inst.key == "pager" else inst.label
        assert expected_label in unescape(row)
        if inst.key not in sentence_keys:
            assert inst.tool in unescape(row)
            assert PROMPT_LINE in unescape(row).lower()
            assert _codes(row) == []

    # Pager row carries lesson 15's law line as knowledge (D16).
    assert PAGER_LAW in unescape(_row_html(html, "pager"))


def test_five_sentence_rows_never_show_code_or_prompt_even_with_events(tmp_path):
    events_path = tmp_path / "events.jsonl"
    for key in ("lora", "rayhunter", "flipper", "clock", "health"):
        log_event(events_path, "reception", {
            "instrument": key, "tool": key, "command": f"fake-{key} --flag",
            "freq_hz": 915_000_000, "radio": {"kind": "meshtasticd", "host": "127.0.0.1:4403"},
            "item": {"text": "hi"},
        })
    running_lora = _LiveStub(running=True, port=4499)
    running_ray = _LiveStub(running=True)
    with _client(tmp_path, **_strict_factories({
        "lora_factory": lambda: running_lora,
        "rayhunter_factory": lambda: running_ray,
    })) as client:
        html = client.get("/field").text

    expected = {
        "lora": (
            "No command line. meshtasticd is a daemon that runs as its own service; "
            "the console reads its API on port 4499. Stopping SIGINT 101 leaves it running."
        ),
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
    for key, sentence in expected.items():
        row = unescape(_row_html(html, key))
        assert sentence in row, (key, row)
        assert _codes(_row_html(html, key)) == []
        assert PROMPT_LINE not in row.lower()


def test_live_running_receiver_shows_exact_spawned_argv_marked_running_now(tmp_path):
    proc = Child(["rtl_433", "-f", "433920000", "-F", "json", "-M", "level", "-d", "0"])
    expected_cmd = command_line(proc)
    with _client(tmp_path, **_strict_factories({
        "decoder_factory": lambda: _LiveStub(proc=proc),
    })) as client:
        html = client.get("/field").text

    row = _row_html(html, "ism")
    assert "Running now" in row
    assert _codes(row) == [expected_cmd]
    assert PROMPT_LINE not in unescape(row).lower()


def test_logged_command_uses_start_event_and_prefers_newer_reception(tmp_path):
    events_path = tmp_path / "events.jsonl"
    # Waterfall logs only spectrum_start (never receptions).
    wf_cmd = "rtl_power -f 88000000:108000000:125000 -i 1 -d 0"
    log_event(events_path, "spectrum_start",
              {"command": wf_cmd, "command_from": "process"},
              clock=lambda: 1700000100.0)

    # ADS-B: quiet band with only adsb_start.
    adsb_cmd = "dump1090 --device 0 --net-sbs-port 30003"
    log_event(events_path, "adsb_start",
              {"freq_hz": 1090000000, "command": adsb_cmd, "command_from": "process"},
              clock=lambda: 1700000200.0)

    # ISM: decode_start followed by a newer reception -> reception wins.
    ism_old = "rtl_433 -f 433920000 -F json -M level -d 0"
    ism_new = "rtl_433 -f 915000000 -F json -M level -d 0"
    log_event(events_path, "decode_start",
              {"command": ism_old, "command_from": "process"},
              clock=lambda: 1700000300.0)
    log_event(events_path, "reception", {
        "instrument": "ism", "tool": "rtl_433", "command": ism_new,
        "freq_hz": 915000000, "radio": {"kind": "rtl-sdr", "index": 0, "serial": None},
        "item": {"model": "Acurite"},
    }, clock=lambda: 1700000400.0)

    with _client(tmp_path, **_strict_factories()) as client:
        html = client.get("/field").text

    wf_utc = datetime.fromtimestamp(1700000100.0, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    wf_row = _row_html(html, "waterfall")
    assert "Last run" in wf_row
    assert _codes(wf_row) == [wf_cmd]
    assert wf_utc in wf_row

    adsb_row = _row_html(html, "adsb")
    assert "Last run" in adsb_row
    assert _codes(adsb_row) == [adsb_cmd]

    ism_utc = datetime.fromtimestamp(1700000400.0, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    ism_row = _row_html(html, "ism")
    assert "Last run" in ism_row
    assert _codes(ism_row) == [ism_new]
    assert ism_utc in ism_row


def test_every_command_route_logs_live_command_and_command_from_process(tmp_path):
    for kind, path, factory_kw, build in _all_start_routes(tmp_path):
        run_dir = tmp_path / kind
        run_dir.mkdir()
        with _client(run_dir, probe=_present, **{factory_kw: build}) as client:
            resp = client.get(path)
            assert resp.status_code == 200, (kind, resp.text)
            live_cmd = resp.json()["command"]
            assert live_cmd, f"{kind} returned empty live command"
        starts = [e for e in read_events(run_dir / "events.jsonl") if e["kind"] == f"{kind}_start"]
        assert len(starts) == 1, (kind, starts)
        payload = starts[0]["payload"]
        assert payload["command"] == live_cmd, kind
        assert payload["command_from"] == "process", kind
        if kind == "flock":
            assert payload["tool"] == "tcpdump"


def test_refused_start_logs_no_start_event(tmp_path):
    def no_radio():
        return False

    with _client(tmp_path, probe=no_radio,
                 sweeper_factory=lambda: Sweeper(spawn=lambda cmd: Child(cmd),
                                                 radio_present=no_radio)) as client:
        resp = client.get("/api/spectrum")
    assert resp.status_code == 503
    starts = [e for e in read_events(tmp_path / "events.jsonl") if e["kind"] == "spectrum_start"]
    assert starts == []


def test_ignored_events_legacy_spectrum_start_and_recorded_receptions(tmp_path):
    events_path = tmp_path / "events.jsonl"
    # Legacy spectrum_start without command_from must be ignored.
    log_event(events_path, "spectrum_start",
              {"command": "rtl_power -f 88000000:108000000:125000 -i 1 -d 0"})

    # Recorded receptions in every _recorded form on both event and payload must be ignored.
    bad_records = [
        ({"replay": True},
         {"instrument": "ism", "tool": "rtl_433", "command": "rtl_433 -r a.cu8"}),
        ({"sample": True},
         {"instrument": "adsb", "tool": "dump1090", "command": "dump1090 --ifile a"}),
        ({"source": "replay"},
         {"instrument": "aprs", "tool": "multimon-ng", "command": "multimon-ng -a AFSK1200"}),
        ({"source": "sample"},
         {"instrument": "acars", "tool": "acarsdec", "command": "acarsdec -f test.wav"}),
        ({},
         {"instrument": "rds", "tool": "redsea", "command": "redsea -f a.mpx", "replay": True}),
        ({},
         {"instrument": "ais", "tool": "rtl_ais", "command": "rtl_ais -f a", "sample": True}),
        ({},
         {"instrument": "dmr", "tool": "dsd-fme", "command": "dsd-fme -i a.wav",
          "source": "replay"}),
        ({},
         {"instrument": "apt", "tool": "satdump", "command": "satdump a.wav",
          "source": "sample"}),
    ]
    with open(events_path, "a", encoding="utf-8") as handle:
        for top_extra, payload in bad_records:
            event = {"ts": 1700000500.0, "kind": "reception", "payload": payload, **top_extra}
            handle.write(json.dumps(event) + "\n")

    with _client(tmp_path, **_strict_factories()) as client:
        html = client.get("/field").text

    for key in ("waterfall", "ism", "adsb", "aprs", "acars", "rds", "ais", "dmr", "apt"):
        row = _row_html(html, key)
        assert _codes(row) == [], (key, row)
        assert PROMPT_LINE in unescape(row).lower()


def test_rotation_falls_back_to_reception_badge_marked_first_heard(tmp_path):
    # Pre-seed a real reception stamp in progress.db while events.jsonl is empty (rotated away).
    db_path = tmp_path / "progress.db"
    badge_cmd = "rtl_433 -f 433920000 -F json -M level -d 0"
    event = {
        "ts": 1700000600.0,
        "kind": "reception",
        "event_id": "abc123",
        "payload": {
            "instrument": "ism",
            "tool": "rtl_433",
            "freq_hz": 433_920_000,
            "command": badge_cmd,
            "radio": {"kind": "rtl-sdr", "index": 0, "serial": "00000001"},
            "item": {"model": "Acurite-Tower"},
        },
    }
    stamp = mint(event)
    assert stamp is not None
    prog = Progress(db_path)
    prog.record_stamps((stamp,))
    prog.close()

    with _client(tmp_path, **_strict_factories()) as client:
        html = client.get("/field").text

    badge_utc = datetime.fromtimestamp(1700000600.0, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    ism_row = _row_html(html, "ism")
    assert "First heard" in ism_row
    assert _codes(ism_row) == [badge_cmd]
    assert badge_utc in ism_row

    # Non-badge / unstamped command rows still show the prompt.
    wf_row = _row_html(html, "waterfall")
    assert _codes(wf_row) == []
    assert PROMPT_LINE in unescape(wf_row).lower()


def test_isolation_shared_sweeper_and_shared_multimon_tool(tmp_path):
    wf_proc = Child(["rtl_power", "-f", "88000000:108000000:125000", "-i", "1", "-d", "0"])
    aprs_proc = Child(["rtl_fm", "-f", "144390000", "|", "multimon-ng", "-a", "AFSK1200"])
    with _client(tmp_path, **_strict_factories({
        "sweeper_factory": lambda: _LiveStub(proc=wf_proc),
        "aprs_factory": lambda: _LiveStub(proc=aprs_proc),
    })) as client:
        html = client.get("/field").text

    assert _codes(_row_html(html, "waterfall")) == [command_line(wf_proc)]
    assert "rtl_power" not in _row_html(html, "clock")
    assert "rtl_power" not in _row_html(html, "health")
    assert _codes(_row_html(html, "aprs")) == [command_line(aprs_proc)]
    assert _codes(_row_html(html, "pager")) == []
    assert PROMPT_LINE in unescape(_row_html(html, "pager")).lower()

    # And the other way round: only pager running -> APRS shows prompt.
    pager_proc = Child(["rtl_fm", "-f", "929612500", "|", "multimon-ng", "-a", "POCSAG1200"])
    with _client(tmp_path, **_strict_factories({
        "pocsag_factory": lambda: _LiveStub(proc=pager_proc),
    })) as client:
        html2 = client.get("/field").text
    assert _codes(_row_html(html2, "pager")) == [command_line(pager_proc)]
    assert _codes(_row_html(html2, "aprs")) == []
    assert PROMPT_LINE in unescape(_row_html(html2, "aprs")).lower()


@pytest.mark.parametrize("source, active_tool, other_tool, active_cmd, other_cmd", [
    (None, "tcpdump", "Marauder",
     "tcpdump -i wlan1 -U -w - type mgt subtype probe-req",
     "sniffprobe -serial"),
    (FLOCK_SOURCE_MARAUDER, "Marauder", "tcpdump",
     "sniffprobe -serial",
     "tcpdump -i wlan1 -U -w - type mgt subtype probe-req"),
], ids=["wlan1-tcpdump", "marauder"])
def test_flock_both_sources_honesty_and_tool_filtering(
    tmp_path, monkeypatch, source, active_tool, other_tool, active_cmd, other_cmd
):
    if source is None:
        monkeypatch.delenv(FLOCK_SOURCE_ENV, raising=False)
    else:
        monkeypatch.setenv(FLOCK_SOURCE_ENV, source)

    events_path = tmp_path / "events.jsonl"
    # Log an event from the OTHER tool first: must be ignored.
    log_event(events_path, "flock_start", {
        "interface": "other", "tool": other_tool, "command": other_cmd,
        "command_from": "process",
    }, clock=lambda: 1700000700.0)

    flock_factories = _strict_factories({"flock_factory": configured_flock_detector})
    with _client(tmp_path, **flock_factories) as client:
        html_before = client.get("/field").text

    # Other tool's logged command is filtered out, and other tool is never named on the page.
    assert other_tool.lower() not in html_before.lower()
    flock_row_before = _row_html(html_before, "flock")
    assert active_tool in flock_row_before
    assert _codes(flock_row_before) == []
    if active_tool == "Marauder":
        assert MARAUDER_NOTE in unescape(flock_row_before)
    else:
        assert MARAUDER_NOTE not in unescape(flock_row_before)

    # Now log a matching start event for the active tool.
    log_event(events_path, "flock_start", {
        "interface": "active", "tool": active_tool, "command": active_cmd,
        "command_from": "process",
    }, clock=lambda: 1700000800.0)
    with _client(tmp_path, **flock_factories) as client:
        html_after = client.get("/field").text

    assert other_tool.lower() not in html_after.lower()
    flock_row_after = _row_html(html_after, "flock")
    assert _codes(flock_row_after) == [active_cmd]


def test_field_is_strictly_read_only(tmp_path):
    prog = Progress(tmp_path / "progress.db")
    prog.record_place("rack", "adsb")
    prog.close()

    stubs = {name: fn() for name, fn in _strict_factories().items()}
    factories = {name: (lambda s=stub: s) for name, stub in stubs.items()}
    with _client(tmp_path, **factories) as client:
        resp = client.get("/field")
        assert resp.status_code == 200
        assert client.app.state.progress.get_place("rack") == "adsb"

    assert read_events(tmp_path / "events.jsonl") == []
    for stub in stubs.values():
        assert stub.running is False


def test_no_template_commands_in_field_html_and_only_known_code_blocks(tmp_path):
    tpl = FIELD_HTML.read_text(encoding="utf-8")
    for tool_word in ("rtl_power", "rtl_433", "dump1090", "multimon-ng", "acarsdec",
                      "rtl_ais", "redsea", "dsd-fme", "satdump", "tcpdump", "sniffprobe"):
        assert tool_word not in tpl, f"field.html must not hardcode tool command '{tool_word}'"

    proc = Child(["dump1090", "--device", "0", "--net-sbs-port", "30003"])
    live_cmd = command_line(proc)
    with _client(tmp_path, **_strict_factories({
        "adsb_factory": lambda: _LiveStub(proc=proc),
    })) as client:
        html = client.get("/field").text

    assert set(_codes(html)) == {SERVICE_STOP, SERVICE_START, live_cmd}


def test_field_css_meets_18px_floor_and_wraps_long_commands():
    assert _font_px(_css_rule(".field")) >= 18
    entries = _css_rule(".field__entries")
    assert "overflow-wrap: anywhere" in entries
    # Command blocks inherit >= 18 px (no rule shrinks .field code below 18 px).
    text = APP_CSS.read_text(encoding="utf-8")
    for match in re.finditer(r"\.field[^{]*code\s*\{([^}]*)\}", text, re.S):
        if "font-size:" in match.group(1):
            assert _font_px(match.group(1)) >= 18


def test_sweeper_live_command_reflects_running_child_only():
    proc = Child(["rtl_power", "-f", "90000000:92000000:50000", "-i", "1", "-d", "0"])
    sweeper = Sweeper(spawn=lambda _cmd: proc, radio_present=_present)
    assert sweeper.live_command() is None
    sweeper.start()
    try:
        assert sweeper.live_command() == command_line(proc)
        proc.code = 0
        assert sweeper.live_command() is None
    finally:
        sweeper.stop()
    assert sweeper.live_command() is None


def test_rack_views_link_to_field_card(tmp_path):
    with _client(tmp_path, **_strict_factories()) as client:
        for path in ("/operate/ism", "/operate/waterfall"):
            resp = client.get(path)
            assert resp.status_code == 200
            assert 'href="/field"' in resp.text
            assert "FIELD" in resp.text
