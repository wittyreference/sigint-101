# ABOUTME: Tests Wi-Fi survey parsing, read-only nmcli execution, /api/survey, and lesson 39.
# ABOUTME: Uses format-based nmcli fixtures (no live scan) and guards the metadata-only invariant.

from __future__ import annotations

import io
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sigint101 import childproc
from sigint101.app import create_app
from sigint101.events import read_events
from sigint101.hardware import RadioProbe
from sigint101.lessons import REQUIRED_STAGES, load_lesson_file, parse_lesson
from sigint101.survey import (
    CENSUS_FIELDS,
    ENVELOPE_FIELDS,
    SURVEY_COMMAND,
    SurveyUnavailable,
    WifiNetwork,
    WifiSurvey,
    channel_census,
    parse_nmcli_line,
    parse_nmcli_wifi,
    split_nmcli_terse,
)
from sigint101.units import teaching_position, unit_for_lesson

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK_M39 = REPO_ROOT / "lesson_packs" / "39-wifi-survey.yaml"
FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "lesson_packs"

# Format-based nmcli fixtures following nmcli(1) terse output (-t -f SSID,BSSID,CHAN,SIGNAL,SECURITY
# dev wifi list, default --escape yes: colons and backslashes inside fields are backslash-escaped,
# hidden networks carry an empty SSID). A real capture from the kiosk replaces these at the next
# bench sitting.
NMCLI_FIXTURE = "\n".join([
    r"CafeNet:AA\:BB\:CC\:DD\:EE\:01:1:84:WPA2",
    r"Lab\:5G:AA\:BB\:CC\:DD\:EE\:02:36:71:WPA2 WPA3",
    r"Ops\\West\:2:AA\:BB\:CC\:DD\:EE\:03:6:63:WPA2",
    r":AA\:BB\:CC\:DD\:EE\:04:6:55:WPA2",
    r"GuestOpen:AA\:BB\:CC\:DD\:EE\:05:6:42:",
    r"LibraryFree:AA\:BB\:CC\:DD\:EE\:06:11:39:--",
    r"--:AA\:BB\:CC\:DD\:EE\:07:11:28:WPA2",
])


class _FakeScanChild:
    """In-memory child matching GroupPopen's surface so tests never spawn real nmcli."""

    def __init__(self, argv, *, stdout="", stderr="", returncode=0, timeout=False):
        self.args = list(argv)
        self._stdout = stdout
        self.returncode = None if timeout else returncode
        self._timeout = timeout
        self.stderr = io.StringIO(stderr)
        self.stdout = io.StringIO(stdout)
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        self.seen_timeout = timeout
        if self._timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.stdout.close()
        return self._stdout, None

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


def _absent():
    return RadioProbe(False, "lsusb", "no RTL2832U on USB bus")


def _client(tmp_path, *, pack_dir=FIXTURE_PACKS, survey_factory=None):
    return TestClient(create_app(
        pack_dir=pack_dir,
        data_dir=tmp_path,
        probe=_absent,
        survey_factory=survey_factory,
    ))


# --- nmcli terse parser & channel census -------------------------------------------------------


def test_command_is_read_only_terse_wifi_list_with_envelope_fields_only():
    assert SURVEY_COMMAND == (
        "nmcli", "-t", "-f", "SSID,BSSID,CHAN,SIGNAL,SECURITY", "dev", "wifi", "list",
    )
    assert "connect" not in SURVEY_COMMAND
    assert ENVELOPE_FIELDS == ("ssid", "bssid", "channel", "signal", "security")


def test_split_nmcli_terse_unescapes_colons_and_backslashes():
    line = r"Ops\\West\:2:AA\:BB\:CC\:DD\:EE\:03:6:63:WPA2"
    assert split_nmcli_terse(line) == [
        r"Ops\West:2", "AA:BB:CC:DD:EE:03", "6", "63", "WPA2",
    ]


def test_parse_nmcli_line_handles_escaped_ssid_colons_hidden_and_open():
    colon_net = parse_nmcli_line(r"Lab\:5G:AA\:BB\:CC\:DD\:EE\:02:36:71:WPA2 WPA3")
    assert colon_net == WifiNetwork(
        ssid="Lab:5G",
        bssid="AA:BB:CC:DD:EE:02",
        channel=36,
        signal=71,
        security="WPA2 WPA3",
    )

    hidden = parse_nmcli_line(r":AA\:BB\:CC\:DD\:EE\:04:6:55:WPA2")
    assert hidden is not None
    assert hidden.ssid is None
    assert hidden.bssid == "AA:BB:CC:DD:EE:04"
    assert hidden.channel == 6

    dash_hidden = parse_nmcli_line(r"--:AA\:BB\:CC\:DD\:EE\:07:11:28:WPA2")
    assert dash_hidden is not None
    assert dash_hidden.ssid is None

    open_empty = parse_nmcli_line(r"GuestOpen:AA\:BB\:CC\:DD\:EE\:05:6:42:")
    open_dash = parse_nmcli_line(r"LibraryFree:AA\:BB\:CC\:DD\:EE\:06:11:39:--")
    assert open_empty is not None and open_empty.security == "Open"
    assert open_dash is not None and open_dash.security == "Open"


@pytest.mark.parametrize("bad_line", [
    "",
    "   ",
    "too:few:fields",
    "too:many:fields:here:1:2",
    r"BadChan:AA\:BB\:CC\:DD\:EE\:01:notanint:50:WPA2",
    r"ZeroChan:AA\:BB\:CC\:DD\:EE\:01:0:50:WPA2",
    r"BadSig:AA\:BB\:CC\:DD\:EE\:01:6:loud:WPA2",
    r"NoBssid::6:50:WPA2",
    r"DashBssid:--:6:50:WPA2",
])
def test_parse_nmcli_line_ignores_blank_or_malformed_lines(bad_line):
    assert parse_nmcli_line(bad_line) is None


def test_parse_nmcli_wifi_deduplicates_bssid_keeping_strongest_signal():
    raw = "\n".join([
        r"CafeNet:aa\:bb\:cc\:dd\:ee\:01:1:40:WPA2",
        r"CafeNet:AA\:BB\:CC\:DD\:EE\:01:1:82:WPA2",
    ])
    nets = parse_nmcli_wifi(raw)
    assert len(nets) == 1
    assert nets[0].bssid == "AA:BB:CC:DD:EE:01"
    assert nets[0].signal == 82


def test_channel_census_groups_networks_per_channel_with_envelope_fields_only():
    nets = parse_nmcli_wifi(NMCLI_FIXTURE)
    assert len(nets) == 7
    census = channel_census(nets)
    assert [row["channel"] for row in census] == [1, 6, 11, 36]

    ch6 = census[1]
    assert ch6 == {
        "channel": 6,
        "count": 3,
        "hidden": 1,
        "open": 1,
        "strongest": 63,
        "ssids": ["GuestOpen", r"Ops\West:2"],
    }

    ch11 = census[2]
    assert ch11 == {
        "channel": 11,
        "count": 2,
        "hidden": 1,
        "open": 1,
        "strongest": 39,
        "ssids": ["LibraryFree"],
    }

    for net in nets:
        assert tuple(net.as_dict().keys()) == ENVELOPE_FIELDS
    for bucket in census:
        assert set(bucket.keys()) == set(CENSUS_FIELDS)
        for forbidden in ("payload", "data", "body", "content", "packet", "frame"):
            assert forbidden not in bucket


# --- WifiSurvey execution, timeout, and honest no-radio state ----------------------------------


def test_wifi_survey_runs_read_only_nmcli_with_timeout_and_returns_census():
    seen = []

    def fake_spawn(argv):
        child = _FakeScanChild(argv, stdout=NMCLI_FIXTURE)
        seen.append(child)
        return child

    scanner = WifiSurvey(
        spawn=fake_spawn,
        which=lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
        radio_present=lambda: True,
        timeout=7.5,
    )
    result = scanner.scan()
    assert len(seen) == 1
    assert tuple(seen[0].args) == SURVEY_COMMAND
    assert seen[0].seen_timeout == 7.5
    assert result["total"] == 7
    assert [c["channel"] for c in result["channels"]] == [1, 6, 11, 36]
    assert result["command"] == "nmcli -t -f SSID,BSSID,CHAN,SIGNAL,SECURITY dev wifi list"
    assert all(tuple(n.keys()) == ENVELOPE_FIELDS for n in result["networks"])


def test_wifi_survey_refuses_honestly_when_nmcli_is_missing():
    scanner = WifiSurvey(
        spawn=lambda argv: pytest.fail("must not spawn when nmcli is missing"),
        which=lambda name: None,
        radio_present=lambda: True,
    )
    with pytest.raises(SurveyUnavailable, match="nmcli is not installed"):
        scanner.scan()


def test_wifi_survey_refuses_honestly_when_no_wifi_radio_exists():
    scanner = WifiSurvey(
        spawn=lambda argv: pytest.fail("must not spawn when no Wi-Fi radio exists"),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: False,
    )
    with pytest.raises(SurveyUnavailable, match="no Wi-Fi radio"):
        scanner.scan()


def test_wifi_survey_stops_timed_out_child_and_refuses(monkeypatch):
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.01)
    children = []

    def slow_spawn(argv):
        child = _FakeScanChild(argv, timeout=True)
        children.append(child)
        return child

    scanner = WifiSurvey(
        spawn=slow_spawn,
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
        timeout=2.0,
    )
    with pytest.raises(SurveyUnavailable, match="timed out"):
        scanner.scan()
    assert children[0].terminated is True


def test_wifi_survey_reports_nonzero_exit_and_disabled_wifi_honestly():
    disabled = WifiSurvey(
        spawn=lambda argv: _FakeScanChild(
            argv, returncode=10, stderr="Error: Wi-Fi is disabled.\n"
        ),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
    )
    with pytest.raises(SurveyUnavailable, match="no Wi-Fi radio"):
        disabled.scan()

    crashed = WifiSurvey(
        spawn=lambda argv: _FakeScanChild(
            argv, returncode=4, stderr="Error: NetworkManager is not running.\n"
        ),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
    )
    with pytest.raises(SurveyUnavailable, match="NetworkManager is not running"):
        crashed.scan()


# --- /api/survey route -------------------------------------------------------------------------


def test_api_survey_returns_census_with_envelope_fields_only(tmp_path):
    scanner = WifiSurvey(
        spawn=lambda argv: _FakeScanChild(argv, stdout=NMCLI_FIXTURE),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
    )
    with _client(tmp_path, survey_factory=lambda: scanner) as client:
        resp = client.get("/api/survey")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["total"] == 7
    assert body["command"] == "nmcli -t -f SSID,BSSID,CHAN,SIGNAL,SECURITY dev wifi list"
    assert [c["channel"] for c in body["channels"]] == [1, 6, 11, 36]
    assert all(tuple(n.keys()) == ENVELOPE_FIELDS for n in body["networks"])
    assert "payload" not in json.dumps(body).lower()


def test_api_survey_reports_honest_no_radio_503_and_deduplicates_log(tmp_path):
    scanner = WifiSurvey(
        spawn=lambda argv: pytest.fail("must not run scan without a radio"),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: False,
    )
    with _client(tmp_path, survey_factory=lambda: scanner) as client:
        first = client.get("/api/survey")
        second = client.get("/api/survey")
    assert first.status_code == 503
    assert second.status_code == 503
    body = first.json()
    assert body["available"] is False
    assert body["cause"] == "no_radio"
    assert "no Wi-Fi radio" in body["reason"]
    assert body["total"] == 0
    assert body["channels"] == []
    assert body["networks"] == []
    assert body["command"] is None
    blocked = [e for e in read_events(tmp_path / "events.jsonl") if e["kind"] == "survey_blocked"]
    assert len(blocked) == 1


def test_api_survey_reports_missing_nmcli_as_missing_tool(tmp_path):
    scanner = WifiSurvey(
        spawn=lambda argv: pytest.fail("must not spawn when nmcli missing"),
        which=lambda name: None,
        radio_present=lambda: True,
    )
    with _client(tmp_path, survey_factory=lambda: scanner) as client:
        resp = client.get("/api/survey")
    assert resp.status_code == 503
    body = resp.json()
    assert body["cause"] == "missing_tool"
    assert "nmcli is not installed" in body["reason"]


# --- Lesson loader + panel template + Lesson 39 consolidation pack -----------------------------


def test_survey_panel_loads_and_cannot_combine_with_other_panels():
    base = {
        "id": 99,
        "slug": "survey-test",
        "title": "Survey Test",
        "concept": "c",
        "panels": ["survey"],
        "hardware": ["wifi"],
        "stages": {"predict": "p", "operate": "o", "observe": "b", "explain": "e"},
    }
    lesson = parse_lesson(base)
    assert lesson.uses_survey is True
    assert lesson.needs_hardware is True
    assert lesson.hardware_badges == []
    with pytest.raises(ValueError, match="survey"):
        parse_lesson({**base, "panels": ["survey", "gain"]})
    with pytest.raises(ValueError, match="survey"):
        parse_lesson({**base, "panels": ["survey", "flock"]})


def test_m39_pack_is_a_valid_unit_5_consolidation_lesson_with_survey_panel():
    lesson = load_lesson_file(PACK_M39)
    assert lesson.id == 39
    assert lesson.type == "consolidation"
    assert lesson.stage_order == ("predict", "operate", "observe", "explain")
    for stage in REQUIRED_STAGES["consolidation"]:
        assert lesson.stages[stage].strip()
    assert lesson.panels == ["survey"]
    assert lesson.uses_survey is True
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False
    assert lesson.tune is None
    assert lesson.predict_kind == "knowledge"
    assert lesson.reveal_label == "In fact"
    assert unit_for_lesson(39).id == 5
    assert teaching_position(39) == 19


def test_m39_renders_sealed_predict_then_survey_panel_without_payload_field(tmp_path):
    with _client(tmp_path, pack_dir=PACK_M39.parent) as client:
        unit_html = client.get("/unit/5").text
        assert 'href="/lesson/39"' in unit_html
        assert re.search(r'tile__code">\s*L19\s*<', unit_html)

        sealed = client.get("/lesson/39").text
        assert "CENSUS HIDDEN" in sealed
        assert "waterfall-wrap--frozen" not in sealed
        assert "Joffe" not in sealed  # explain stays sealed before prediction

        commit = client.post(
            "/api/predict?lesson_id=39",
            data={"prediction": "envelope-only"},
            follow_redirects=False,
        )
        assert commit.status_code == 303

        open_html = client.get("/lesson/39").text
        assert "data-survey-panel" in open_html
        assert "data-survey-list" in open_html
        assert "data-survey-empty" in open_html
        assert 'src="/static/survey.js"' in open_html
        assert 'class="waterfall"' not in open_html
        assert "data-survey-payload" not in open_html
        assert "Joffe v. Google" in open_html


# --- Joffe v. Google metadata-vs-payload legal-claim guard (both directions) -------------------

_JOFFE_CASE = re.compile(r"\bjoffe\s+v\.?\s+google\b", re.I)
_JOFFE_CITE = re.compile(r"\b746\s+F\.?3d\s+920\b")
_WIRETAP_2511 = re.compile(r"\b2511\(2\)\(g\)\(i\)")
_METADATA_LINE = re.compile(
    r"\b(?:metadata|envelopes?)\b[^.?!]*\bpayload\b|\bpayload\b[^.?!]*\b(?:metadata|envelopes?)\b",
    re.I,
)
_OPEN_WIFI_PAYLOAD_PROTECTED = re.compile(
    r"\bopen\s+wi-?fi\b|\bunencrypted\s+wi-?fi\b", re.I
)
_FALSE_OPEN_EXEMPT = re.compile(
    r"\bopen\s+wi-?fi\s+payload\s+(?:is|counts\s+as)\s+(?:legal|lawful|readily\s+accessible|exempt)",
    re.I,
)


def _check_joffe_claim(text: str) -> list[str]:
    """Check that prose states the metadata-vs-payload line with Joffe v. Google and 18 USC 2511."""
    issues = []
    if not _JOFFE_CASE.search(text):
        issues.append("missing Joffe v. Google")
    if not _JOFFE_CITE.search(text):
        issues.append("missing 746 F.3d 920 citation")
    if not _WIRETAP_2511.search(text):
        issues.append("missing 18 USC 2511(2)(g)(i)")
    if not _METADATA_LINE.search(text):
        issues.append("missing line between metadata/envelopes and payload")
    if not _OPEN_WIFI_PAYLOAD_PROTECTED.search(text):
        issues.append("missing open/unencrypted Wi-Fi scope")
    if _FALSE_OPEN_EXEMPT.search(text):
        issues.append("falsely claims open Wi-Fi payload is exempt/legal")
    return issues


def test_joffe_claim_probe_rejects_defects_and_accepts_fair_rewordings():
    missing_source = (
        "Count Wi-Fi metadata like SSID and channel, never payload data on open Wi-Fi."
    )
    wrong_law = (
        "Open Wi-Fi payload is legal to read under 2511(2)(g)(i) because Joffe v. Google "
        "(746 F.3d 920) said metadata and payload are both broadcast radio."
    )
    fair_rewording = (
        "Wi-Fi beacons advertise metadata, not payload. Under Joffe v. Google, 746 F.3d 920, "
        "payload data on unencrypted Wi-Fi is not readily accessible under 18 USC 2511(2)(g)(i)."
    )
    assert _check_joffe_claim(missing_source)
    assert any("falsely" in item for item in _check_joffe_claim(wrong_law))
    assert _check_joffe_claim(fair_rewording) == []


def test_m39_explain_states_joffe_metadata_payload_line_with_primary_sources():
    lesson = load_lesson_file(PACK_M39)
    assert _check_joffe_claim(lesson.stages["explain"]) == []
    raw = yaml.safe_load(PACK_M39.read_text(encoding="utf-8"))
    urls = [str(s.get("url", "")) for s in (raw.get("sources") or []) if isinstance(s, dict)]
    assert any("govinfo.gov" in u and "11-17483" in u for u in urls), urls
    assert any("law.cornell.edu" in u and "/18/2511" in u for u in urls), urls
    assert any("law.cornell.edu" in u and "/18/2510" in u for u in urls), urls
