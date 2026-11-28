# ABOUTME: Tests Wi-Fi survey parsing, read-only nmcli execution, /api/survey, and lesson 39.
# ABOUTME: Uses format-based nmcli fixtures (no live scan) and guards V1-V5 requirements.

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
from sigint101 import survey as survey_mod
from sigint101.app import create_app
from sigint101.events import read_events
from sigint101.hardware import RadioProbe
from sigint101.lessons import REQUIRED_STAGES, load_lesson_file, parse_lesson
from sigint101.survey import (
    CENSUS_FIELDS,
    DEVICE_COMMAND,
    ENVELOPE_FIELDS,
    RADIO_COMMAND,
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
DENSE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "survey" / "dense_nmcli.txt"

# Format-based nmcli fixtures following nmcli(1) terse output (-t -f
# SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY dev wifi list --rescan no, default --escape yes: colons and
# backslashes inside fields are backslash-escaped, hidden networks carry an empty SSID). All BSSIDs
# use locally administered MAC addresses (02:...).
NMCLI_FIXTURE = "\n".join([
    r"CafeNet:02\:00\:00\:00\:ee\:01:1:2412 MHz:84:WPA2",
    r"Lab\:5G:02\:00\:00\:00\:ee\:02:36:5180 MHz:71:WPA2 WPA3",
    r"Ops\\West\:2:02\:00\:00\:00\:ee\:03:6:2437 MHz:63:WPA2",
    r":02\:00\:00\:00\:ee\:04:6:2437 MHz:55:WPA2",
    r"GuestOpen:02\:00\:00\:00\:ee\:05:6:2437 MHz:42:",
    r"LibraryFree:02\:00\:00\:00\:ee\:06:11:2462 MHz:39:--",
    r"--:02\:00\:00\:00\:ee\:07:11:2462 MHz:28:WPA2",
    r"SixGigAP:02\:00\:00\:00\:ee\:08:1:5955 MHz:77:WPA3",
])

NMCLI_DEVICES_READY = "wlan0:wifi:connected\nwlan1:wifi:unmanaged\neth0:ethernet:connected\n"


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


def _fake_nmcli_spawn(
    *,
    radio_out="enabled\n",
    devices_out=NMCLI_DEVICES_READY,
    list_out=NMCLI_FIXTURE,
    radio_rc=0,
    devices_rc=0,
    list_rc=0,
    radio_err="",
    devices_err="",
    list_err="",
    timeout_on=None,
    seen=None,
):
    """Fake spawn dispatching nmcli radio, device, and wifi list commands like real nmcli."""
    def spawn(argv):
        cmd = tuple(argv)
        if cmd == RADIO_COMMAND:
            child = _FakeScanChild(
                argv, stdout=radio_out, stderr=radio_err, returncode=radio_rc,
                timeout=(timeout_on == "radio"),
            )
        elif cmd == DEVICE_COMMAND:
            child = _FakeScanChild(
                argv, stdout=devices_out, stderr=devices_err, returncode=devices_rc,
                timeout=(timeout_on == "device"),
            )
        elif cmd == SURVEY_COMMAND:
            child = _FakeScanChild(
                argv, stdout=list_out, stderr=list_err, returncode=list_rc,
                timeout=(timeout_on == "list"),
            )
        else:
            raise AssertionError(f"unexpected nmcli argv: {argv!r}")
        if seen is not None:
            seen.append(child)
        return child

    return spawn


def _absent():
    return RadioProbe(False, "lsusb", "no RTL2832U on USB bus")


def _client(tmp_path, *, pack_dir=FIXTURE_PACKS, survey_factory=None):
    return TestClient(create_app(
        pack_dir=pack_dir,
        data_dir=tmp_path,
        probe=_absent,
        survey_factory=survey_factory,
    ))


# --- V3 & V4: nmcli command, terse parser, band + channel census -------------------------------


def test_command_is_read_only_terse_wifi_list_with_rescan_no_and_freq():
    assert RADIO_COMMAND == ("nmcli", "-t", "-f", "WIFI", "radio")
    assert DEVICE_COMMAND == ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device")
    assert SURVEY_COMMAND == (
        "nmcli", "-t", "-f", "SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY",
        "dev", "wifi", "list", "--rescan", "no",
    )
    assert "connect" not in SURVEY_COMMAND
    assert ENVELOPE_FIELDS == ("ssid", "bssid", "band", "channel", "freq_mhz", "signal", "security")
    assert CENSUS_FIELDS == ("band", "channel", "count", "hidden", "open", "strongest", "ssids")


def test_split_nmcli_terse_unescapes_colons_and_backslashes():
    line = r"Ops\\West\:2:02\:00\:00\:00\:ee\:03:6:2437 MHz:63:WPA2"
    assert split_nmcli_terse(line) == [
        r"Ops\West:2", "02:00:00:00:ee:03", "6", "2437 MHz", "63", "WPA2",
    ]


def test_parse_nmcli_line_handles_escaped_ssid_colons_band_hidden_and_open():
    colon_net = parse_nmcli_line(r"Lab\:5G:02\:00\:00\:00\:ee\:02:36:5180 MHz:71:WPA2 WPA3")
    assert colon_net == WifiNetwork(
        ssid="Lab:5G",
        bssid="02:00:00:00:EE:02",
        band="5 GHz",
        channel=36,
        freq_mhz=5180,
        signal=71,
        security="WPA2 WPA3",
    )

    six_net = parse_nmcli_line(r"SixGigAP:02\:00\:00\:00\:ee\:08:1:5955 MHz:77:WPA3")
    assert six_net is not None
    assert six_net.band == "6 GHz"
    assert six_net.channel == 1
    assert six_net.freq_mhz == 5955

    hidden = parse_nmcli_line(r":02\:00\:00\:00\:ee\:04:6:2437 MHz:55:WPA2")
    assert hidden is not None
    assert hidden.ssid is None
    assert hidden.bssid == "02:00:00:00:EE:04"
    assert hidden.band == "2.4 GHz"
    assert hidden.channel == 6

    dash_hidden = parse_nmcli_line(r"--:02\:00\:00\:00\:ee\:07:11:2462 MHz:28:WPA2")
    assert dash_hidden is not None
    assert dash_hidden.ssid is None

    open_empty = parse_nmcli_line(r"GuestOpen:02\:00\:00\:00\:ee\:05:6:2437 MHz:42:")
    open_dash = parse_nmcli_line(r"LibraryFree:02\:00\:00\:00\:ee\:06:11:2462 MHz:39:--")
    assert open_empty is not None and open_empty.security == "Open"
    assert open_dash is not None and open_dash.security == "Open"


@pytest.mark.parametrize("bad_line", [
    "",
    "   ",
    "too:few:fields",
    "too:many:fields:here:1:2:3",
    r"BadChan:02\:00\:00\:00\:ee\:01:notanint:2412 MHz:50:WPA2",
    r"ZeroChan:02\:00\:00\:00\:ee\:01:0:2412 MHz:50:WPA2",
    r"BadFreq:02\:00\:00\:00\:ee\:01:6:notafreq:50:WPA2",
    r"BadSig:02\:00\:00\:00\:ee\:01:6:2437 MHz:loud:WPA2",
    r"NoBssid::6:2437 MHz:50:WPA2",
    r"DashBssid:--:6:2437 MHz:50:WPA2",
])
def test_parse_nmcli_line_ignores_blank_or_malformed_lines(bad_line):
    assert parse_nmcli_line(bad_line) is None


def test_parse_nmcli_wifi_deduplicates_bssid_per_band_and_channel():
    raw = "\n".join([
        r"CafeNet:02\:00\:00\:00\:ee\:01:1:2412 MHz:40:WPA2",
        r"CafeNet:02\:00\:00\:00\:EE\:01:1:2412 MHz:82:WPA2",
        r"CafeNet6:02\:00\:00\:00\:EE\:01:1:5955 MHz:75:WPA3",
    ])
    nets = parse_nmcli_wifi(raw)
    assert len(nets) == 2
    by_band = {n.band: n for n in nets}
    assert by_band["2.4 GHz"].signal == 82
    assert by_band["6 GHz"].signal == 75


def test_channel_census_keys_by_band_and_channel_never_merging_6ghz_into_24ghz():
    nets = parse_nmcli_wifi(NMCLI_FIXTURE)
    assert len(nets) == 8
    census = channel_census(nets)
    assert [(row["band"], row["channel"]) for row in census] == [
        ("2.4 GHz", 1),
        ("2.4 GHz", 6),
        ("2.4 GHz", 11),
        ("5 GHz", 36),
        ("6 GHz", 1),
    ]

    ch1_24 = census[0]
    ch1_6 = census[4]
    assert ch1_24["ssids"] == ["CafeNet"] and ch1_24["count"] == 1
    assert ch1_6["ssids"] == ["SixGigAP"] and ch1_6["count"] == 1

    ch6 = census[1]
    assert ch6 == {
        "band": "2.4 GHz",
        "channel": 6,
        "count": 3,
        "hidden": 1,
        "open": 1,
        "strongest": 63,
        "ssids": ["GuestOpen", r"Ops\West:2"],
    }

    for net in nets:
        assert tuple(net.as_dict().keys()) == ENVELOPE_FIELDS
    for bucket in census:
        assert set(bucket.keys()) == set(CENSUS_FIELDS)
        for forbidden in ("payload", "data", "body", "content", "packet", "frame"):
            assert forbidden not in bucket


def test_dense_fixture_parses_many_networks_across_all_three_bands():
    nets = parse_nmcli_wifi(DENSE_FIXTURE.read_text(encoding="utf-8"))
    census = channel_census(nets)
    assert len(nets) >= 18
    assert len(census) >= 10
    bands = {row["band"] for row in census}
    assert bands == {"2.4 GHz", "5 GHz", "6 GHz"}
    # Both 2.4 GHz ch 1 and 6 GHz ch 1 appear as distinct rows; 2.4 GHz ch 1 has two 32-char SSIDs.
    ch1_24 = next(r for r in census if r["band"] == "2.4 GHz" and r["channel"] == 1)
    ch1_6 = next(r for r in census if r["band"] == "6 GHz" and r["channel"] == 1)
    assert ch1_24["count"] == 6 and ch1_24["hidden"] == 1 and ch1_24["open"] == 1
    assert any(len(s) == 32 for s in ch1_24["ssids"])
    assert ch1_6["ssids"] == ["SixGigCafe"]


# --- V2 & V3: WifiSurvey execution, radio-off / unmanaged refusal, and timeout -----------------


def test_wifi_survey_checks_radio_and_devices_then_runs_rescan_no_census():
    seen = []
    scanner = WifiSurvey(
        spawn=_fake_nmcli_spawn(seen=seen),
        which=lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
        radio_present=lambda: True,
        timeout=7.5,
    )
    result = scanner.scan()
    assert [tuple(c.args) for c in seen] == [RADIO_COMMAND, DEVICE_COMMAND, SURVEY_COMMAND]
    assert all(c.seen_timeout == 7.5 for c in seen)
    assert result["total"] == 8
    assert [(c["band"], c["channel"]) for c in result["channels"]] == [
        ("2.4 GHz", 1), ("2.4 GHz", 6), ("2.4 GHz", 11), ("5 GHz", 36), ("6 GHz", 1),
    ]
    assert result["command"] == (
        "nmcli -t -f SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY dev wifi list --rescan no"
    )
    assert result["cached"] is True
    assert "age_s" not in result
    assert all(tuple(n.keys()) == ENVELOPE_FIELDS for n in result["networks"])


def test_wifi_survey_reports_cached_without_invented_console_age():
    # F2: --rescan no reads NetworkManager's cache; without NetworkManager's real LastScan
    # timestamp, the survey reports cached=True and never invents a console-observed age_s.
    current_list = [NMCLI_FIXTURE]

    def dynamic_spawn(argv):
        cmd = tuple(argv)
        if cmd == RADIO_COMMAND:
            return _FakeScanChild(argv, stdout="enabled\n")
        if cmd == DEVICE_COMMAND:
            return _FakeScanChild(argv, stdout=NMCLI_DEVICES_READY)
        if cmd == SURVEY_COMMAND:
            return _FakeScanChild(argv, stdout=current_list[0])
        raise AssertionError(f"unexpected nmcli argv: {argv!r}")

    scanner = WifiSurvey(
        spawn=dynamic_spawn,
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
    )
    first = scanner.scan()
    second = scanner.scan()
    current_list[0] = r"CafeNet:02\:00\:00\:00\:ee\:01:1:2412 MHz:90:WPA2"
    changed = scanner.scan()
    for snap in (first, second, changed):
        assert snap["cached"] is True
        assert "age_s" not in snap


def test_nmcli_popen_forces_lc_all_c_locale(monkeypatch):
    # F9: nmcli(1) localizes words like disabled/unmanaged/unavailable unless run with LC_ALL=C.
    import sys

    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    captured = {}

    def fake_spawn(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return _FakeScanChild(argv)

    monkeypatch.setattr(survey_mod, "spawn", fake_spawn)
    survey_mod._popen(list(RADIO_COMMAND))
    assert captured["argv"] == list(RADIO_COMMAND)
    assert captured["kwargs"]["env"]["LC_ALL"] == "C"

    # End-to-end proof through childproc.spawn: a real child inherits LC_ALL=C, not fr_FR.UTF-8.
    monkeypatch.setattr(survey_mod, "spawn", childproc.spawn)
    proc = survey_mod._popen([sys.executable, "-c", "import os; print(os.environ['LC_ALL'])"])
    out, _ = proc.communicate(timeout=5)
    assert proc.returncode == 0
    assert out.strip() == "C"


def test_wifi_survey_refuses_honestly_when_wifi_radio_is_off_even_though_list_exits_0():
    # Real nmcli behaviour: with Wi-Fi soft-blocked or off, `nmcli -t -f WIFI radio` prints
    # `disabled` and `dev wifi list` would print nothing and exit 0. We must refuse "Wi-Fi radio
    # is off" rather than reporting LIVE with 0 networks.
    seen = []
    scanner = WifiSurvey(
        spawn=_fake_nmcli_spawn(radio_out="disabled\n", list_out="", seen=seen),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
    )
    with pytest.raises(SurveyUnavailable, match="Wi-Fi radio is off"):
        scanner.scan()
    assert [tuple(c.args) for c in seen] == [RADIO_COMMAND]


@pytest.mark.parametrize("devices_out, complaint", [
    ("wlan0:wifi:unmanaged\neth0:ethernet:connected\n", "no managed Wi-Fi device"),
    ("eth0:ethernet:connected\nlo:loopback:connected (externally)\n", "no managed Wi-Fi device"),
    ("wlan0:wifi:unavailable\n", "unavailable"),
])
def test_wifi_survey_refuses_when_no_managed_wifi_device_is_available(devices_out, complaint):
    seen = []
    scanner = WifiSurvey(
        spawn=_fake_nmcli_spawn(devices_out=devices_out, list_out="", seen=seen),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
    )
    with pytest.raises(SurveyUnavailable, match=complaint):
        scanner.scan()
    assert [tuple(c.args) for c in seen] == [RADIO_COMMAND, DEVICE_COMMAND]


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
    scanner = WifiSurvey(
        spawn=_fake_nmcli_spawn(timeout_on="list", seen=children),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
        timeout=2.0,
    )
    with pytest.raises(SurveyUnavailable, match="timed out"):
        scanner.scan()
    assert children[-1].terminated is True


def test_wifi_survey_reports_nonzero_exit_from_nmcli_honestly():
    crashed = WifiSurvey(
        spawn=_fake_nmcli_spawn(radio_rc=4, radio_err="Error: NetworkManager is not running.\n"),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
    )
    with pytest.raises(SurveyUnavailable, match="NetworkManager is not running"):
        crashed.scan()


# --- /api/survey route -------------------------------------------------------------------------


def test_api_survey_returns_census_with_envelope_fields_only(tmp_path):
    scanner = WifiSurvey(
        spawn=_fake_nmcli_spawn(),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
    )
    with _client(tmp_path, survey_factory=lambda: scanner) as client:
        resp = client.get("/api/survey")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["cached"] is True
    assert "age_s" not in body
    assert body["total"] == 8
    assert body["command"] == (
        "nmcli -t -f SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY dev wifi list --rescan no"
    )
    assert [(c["band"], c["channel"]) for c in body["channels"]] == [
        ("2.4 GHz", 1), ("2.4 GHz", 6), ("2.4 GHz", 11), ("5 GHz", 36), ("6 GHz", 1),
    ]
    assert all(tuple(n.keys()) == ENVELOPE_FIELDS for n in body["networks"])
    assert "payload" not in json.dumps(body).lower()


@pytest.mark.parametrize("spawn_kwargs, expected_text", [
    ({"radio_out": "disabled\n", "list_out": ""}, "Wi-Fi radio is off"),
    ({"devices_out": "wlan0:wifi:unmanaged\n", "list_out": ""}, "no managed Wi-Fi device"),
    ({"devices_out": "wlan0:wifi:unavailable\n", "list_out": ""}, "unavailable"),
])
def test_api_survey_reports_radio_off_or_unmanaged_as_no_radio_503_not_live(
    tmp_path, spawn_kwargs, expected_text
):
    scanner = WifiSurvey(
        spawn=_fake_nmcli_spawn(**spawn_kwargs),
        which=lambda name: "/usr/bin/nmcli",
        radio_present=lambda: True,
    )
    with _client(tmp_path, survey_factory=lambda: scanner) as client:
        resp = client.get("/api/survey")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["cause"] == "no_radio"
    assert expected_text in body["reason"]
    assert body["total"] == 0 and body["channels"] == []


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


# A remedy a learner could paste or run: a package manager, an install verb, a script, sudo.
_INSTALL_COMMAND = re.compile(
    r"\b(?:sudo|apt(?:-get)?|dpkg|install(?:s|ing)?|enable|systemctl|raspi-config)\b"
    r"|\S+\.sh\b|^Run\b",
    re.I,
)


def test_missing_nmcli_remedy_names_networkmanager_and_gives_no_install_command(tmp_path):
    # F8: nmcli ships with NetworkManager, so it is missing only where NetworkManager does not run
    # the box's network (dhcpcd or ifupdown). Installing it there takes over every interface not
    # in /etc/network/interfaces, and a learner on Wi-Fi SSH could lose the link. provision.sh
    # does not install it either. So the remedy says why and leaves the switch to the bench.
    scanner = WifiSurvey(
        spawn=lambda argv: pytest.fail("must not spawn when nmcli missing"),
        which=lambda name: None,
        radio_present=lambda: True,
    )
    with _client(tmp_path, survey_factory=lambda: scanner) as client:
        body = client.get("/api/survey").json()
    step = body["next_step"]
    assert body["cause"] == "missing_tool"
    assert "NetworkManager" in step, step
    assert not _INSTALL_COMMAND.search(step), step


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
        assert "746 F.3d 920" not in sealed  # explain stays sealed before prediction

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
        assert "746 F.3d 920" in open_html


# --- V1: Joffe v. Google amended-opinion legal-claim guard (both directions) -------------------

_JOFFE_CASE = re.compile(r"\bjoffe\s+v\.?\s+google\b", re.I)
_JOFFE_CITE = re.compile(r"\b746\s+F\.?3d\s+920\b")
_NINTH_CIRCUIT = re.compile(r"\bninth\s+circuit\b|\b9th\s+cir\.?\b", re.I)
_STATUTE_2510 = re.compile(r"\b2510\(16\)")
_STATUTE_2511 = re.compile(r"\b2511\(2\)\(g\)\(i\)")
_STATUTE_2511_DEFAULT = re.compile(r"\b2511\(1\)\(a\)")
_INTENTIONALLY_INTERCEPT = re.compile(
    r"\b2511\(1\)\(a\)[^.?!]*\bintentionally\s+intercept\w*\b"
    r"|\bintentionally\s+intercept\w*[^.?!]*\b2511\(1\)\(a\)",
    re.I,
)
_OUTSIDE_PAYLOAD_OPEN = re.compile(
    r"\b(?:outside\s+(?:the\s+)?(?:ninth|9th)\s+circuit|elsewhere)\b[^.?!]*\bpayload\b"
    r"[^.?!]*\b(?:open|unsettled|undecided)\b",
    re.I,
)
_RADIO_COMM_HOLDING = re.compile(r"radio\s+communication", re.I)
_DENIAL_MOTION_TO_DISMISS = re.compile(
    r"\breviewed\s+the\s+denial\s+of\b[^.?!]*\bmotion\s+to\s+dismiss\b"
    r"|\bdenial\s+of\b[^.?!]*\bmotion\s+to\s+dismiss\b",
    re.I,
)
_ONE_CIRCUIT = re.compile(
    r"\bbinds?\s+(?:only\s+)?(?:the\s+)?(?:ninth|9th)\s+circuit\b"
    r"|\bone\s+circuit\b|\bno\s+other\s+(?:federal\s+)?(?:court\s+of\s+appeals|circuit)\b",
    re.I,
)
_AS_OF_2026 = re.compile(r"\bnone\s+had\s+ruled\s+as\s+of\s+2026\b|\bas\s+of\s+2026\b", re.I)
_BEACON_SILENT = re.compile(
    r"\b(?:never\s+decided|did\s+not\s+decide|left\s+[^.?!]*undecided|silent)\b"
    r"[^.?!]*\b(?:beacon|metadata|ssid|header)\b"
    r"|\b(?:beacon|metadata)\b[^.?!]*\b(?:undecided|not\s+decided|never\s+decided)\b",
    re.I,
)

# Defects that must NEVER appear anywhere in lesson 39 learner text:
_OVERSTATES_VIOLATION = re.compile(
    r"\b(?:violates?|breaks?|broke)\s+the\s+wiretap\s+act\b"
    r"|\bfound\s+(?:google\s+)?(?:liable|in\s+violation)\b"
    r"|\bpayload\b[^.?!]*\bis\s+(?:illegal|unlawful|a\s+crime)\b",
    re.I,
)
_DELETED_SEPT_HOLDING = re.compile(
    r"\bnor\s+[\"']?readily\s+accessible\b"
    r"|\bnot\s+[\"']?readily\s+accessible[^.?!]*ordinary\s+meaning\b"
    r"|\b330\s*feet\b",
    re.I,
)
_NATIONWIDE_OVERCLAIM = re.compile(
    r"\bfederal\s+law\s+(?:protects|forbids|bars|prohibits)\b"
    r"|\bwhere\s+does\s+federal\s+law\s+draw\b"
    r"|\b(?:illegal|unlawful|a\s+crime)\s+(?:everywhere|nationwide)\b"
    r"|\b(?:everywhere|nationwide)\b[^.?!]*\b(?:illegal|unlawful|a\s+crime)\b",
    re.I,
)
_FALSE_BEACON_HOLDING = re.compile(
    r"\b(?:held|ruled)\s+(?:that\s+)?(?:collecting\s+|reading\s+|scanning\s+)?"
    r"(?:wi-?fi\s+)?(?:beacons?|metadata|headers?)\b[^.?!;]*\b(?:lawful|legal)\b",
    re.I,
)
# F7: Wi-Fi payload IS carried by radio; Joffe's point is the statute's term "radio communication"
# (18 USC 2510(16)). A bare "not radio" is false as physics; a quoted "radio" or the full term
# is not.
_BARE_NOT_RADIO = re.compile(
    r"\b(?:not|isn['’]t|aren['’]t|never)\s+(?:an?\s+)?radio\b(?!\s*-|\s+communications?\b)",
    re.I,
)


def _check_joffe_explain(explain: str) -> list[str]:
    """Check that explain states the amended Joffe holding accurately without overclaiming."""
    issues = []
    if not _JOFFE_CASE.search(explain):
        issues.append("missing Joffe v. Google")
    if not _JOFFE_CITE.search(explain):
        issues.append("missing 746 F.3d 920 citation")
    if not _NINTH_CIRCUIT.search(explain):
        issues.append("missing Ninth Circuit attribution")
    if not _STATUTE_2510.search(explain) or not _RADIO_COMM_HOLDING.search(explain):
        issues.append("missing 18 USC 2510(16) 'radio communication' holding")
    if not _STATUTE_2511.search(explain):
        issues.append("missing 18 USC 2511(2)(g)(i) exemption reference")
    if not _STATUTE_2511_DEFAULT.search(explain):
        issues.append("missing 18 USC 2511(1)(a) Wiretap Act default rule")
    if not _INTENTIONALLY_INTERCEPT.search(explain):
        issues.append("2511(1)(a) default rule must specify 'intentionally' intercepting")
    if not _OUTSIDE_PAYLOAD_OPEN.search(explain):
        issues.append("outside-Ninth-Circuit clause must name the payload question as open")
    if not _DENIAL_MOTION_TO_DISMISS.search(explain):
        issues.append("missing denial-of-motion-to-dismiss posture")
    if not _ONE_CIRCUIT.search(explain):
        issues.append("missing one-circuit scope")
    if not _AS_OF_2026.search(explain):
        issues.append("missing 'as of 2026' time-bounded circuit count")
    if not _BEACON_SILENT.search(explain):
        issues.append("missing statement that Joffe left beacon metadata undecided")
    return issues


def _check_joffe_overstatements(all_text: str) -> list[str]:
    """Check all learner-visible prose for the legal overstatements."""
    issues = []
    if _OVERSTATES_VIOLATION.search(all_text):
        issues.append("overstates motion-to-dismiss ruling as a Wiretap Act violation")
    if _DELETED_SEPT_HOLDING.search(all_text):
        issues.append("teaches the deleted September ordinary-meaning holding")
    if _NATIONWIDE_OVERCLAIM.search(all_text):
        issues.append("presents one circuit's ruling as nationwide settled federal law")
    if _FALSE_BEACON_HOLDING.search(all_text):
        issues.append("claims Joffe held beacon metadata lawful")
    if _BARE_NOT_RADIO.search(all_text):
        issues.append("says payload is not radio (false as physics); name the statute's term")
    return issues


def test_payload_radio_guard_rejects_bare_not_radio_and_accepts_the_statutory_term():
    # F7: restatements of the defect must fail.
    for defect in (
        "Payload is not radio; scan metadata",
        "Open Wi-Fi payload isn't radio, so a survey may read it.",
        "Wi-Fi data is not a radio signal under Joffe.",
        "Payload: not radio. This panel reads metadata.",
    ):
        assert _check_joffe_overstatements(defect), defect
    # Fair wordings that mark or name the statute's term must pass.
    for fair in (
        'Not "radio communication"; metadata only',
        'Payload is not "radio"; scan metadata',
        "Payload is not a radio communication under 18 USC 2510(16); this panel stops at metadata.",
        'Wi-Fi payload rides on radio waves, but it is not a "radio communication" (9th Cir.).',
    ):
        assert _check_joffe_overstatements(fair) == [], fair


def _unquoted_radio_chips(labels) -> list[str]:
    """Chips that use "radio" outside quote marks."""
    return [label for label in labels
            if re.search(r"\bradio\b", re.sub(r"\"[^\"]*\"|“[^”]*”", "", label), re.I)]


def test_m39_chips_that_use_the_statutory_term_all_quote_it():
    # Quote marks on the statute's term must not single out the scored chip: every chip that says
    # "radio" quotes it, the distractors as much as the answer, and so does the predict's question.
    lesson = load_lesson_file(PACK_M39)
    labels = [c["label"] for c in lesson.choices]
    assert _unquoted_radio_chips([*labels, lesson.stages["predict"]]) == []


_NOT_RADIO_COMMUNICATION = re.compile(r"\bnot\b[^;.]*radio\s+communication", re.I)
# The scored chip scopes the panel to metadata; it must not say reading metadata is lawful,
# which Joffe never decided.
_LAWFUL_CLAIM = re.compile(
    r"\b(?:lawful|legal(?:ly)?|allowed|permitted|permissible|authori[sz]ed|fair\s+game|ok(?:ay)?)\b",
    re.I,
)


def _scored_chip_issues(choices, answer) -> list[str]:
    """What is wrong with how a lesson-39 choice set scores the Joffe question."""
    labels = {c["key"]: c["label"] for c in choices}
    issues = []
    if len(answer) != 1 or answer[0] not in labels:
        return [f"expected one scored key among the chips, got {list(answer)}"]
    scored = labels[answer[0]]
    if not (_NOT_RADIO_COMMUNICATION.search(scored) and re.search(r"\bmetadata\b", scored, re.I)):
        issues.append(f"scored chip is not the payload-is-not-radio-communication one: {scored}")
    if _LAWFUL_CLAIM.search(scored):
        issues.append(f"scored chip claims lawfulness: {scored}")
    for key, label in labels.items():
        if key != answer[0] and _NOT_RADIO_COMMUNICATION.search(label):
            issues.append(f"a distractor carries the holding: {label}")
    return issues


def test_m39_scores_the_chip_that_puts_payload_outside_radio_communication():
    lesson = load_lesson_file(PACK_M39)
    assert _scored_chip_issues(lesson.choices, lesson.answer) == []


def test_scored_chip_guard_rejects_a_flipped_answer_and_a_lawfulness_claim():
    lesson = load_lesson_file(PACK_M39)
    keys = [c["key"] for c in lesson.choices]
    for other in keys:
        if other not in lesson.answer:
            assert _scored_chip_issues(lesson.choices, (other,)), other
    for claim in ('Not "radio communication"; metadata is lawful',
                  'Not "radio communication"; metadata is legal',
                  'Not "radio communication"; metadata is fair game',
                  'Not "radio communication"; metadata allowed'):
        choices = [dict(c, label=claim) if c["key"] in lesson.answer else c
                   for c in lesson.choices]
        assert _scored_chip_issues(choices, lesson.answer), claim
    for fair in ('Not "radio communication"; panel reads metadata',
                 'Payload is not a "radio communication"; metadata only'):
        choices = [dict(c, label=fair) if c["key"] in lesson.answer else c
                   for c in lesson.choices]
        assert _scored_chip_issues(choices, lesson.answer) == [], fair


def test_joffe_claim_probe_rejects_round1_overstatements_and_accepts_accurate_rewordings():
    round1_explain = (
        "In Joffe v. Google, Inc., 746 F.3d 920 (9th Cir. 2013), the Ninth Circuit held that "
        "unencrypted Wi-Fi payload data is neither a 'radio communication' under 18 USC 2510(16) "
        "nor 'readily accessible to the general public' under 18 USC 2511(2)(g)(i), so "
        "intercepting it violates the Wiretap Act."
    )
    assert _check_joffe_explain(round1_explain)
    assert len(_check_joffe_overstatements(round1_explain)) == 2

    beacon_overclaim = (
        "Joffe v. Google (746 F.3d 920, 9th Cir.) held beacon metadata lawful while denying "
        "Google's motion to dismiss on payload under 2510(16) radio communication and "
        "2511(2)(g)(i), binding only the Ninth Circuit."
    )
    assert _check_joffe_overstatements(beacon_overclaim)

    # F6: surviving mutation M8 must be rejected.
    nationwide_everywhere = (
        "So capturing open Wi-Fi payload is illegal everywhere."
    )
    assert _check_joffe_overstatements(nationwide_everywhere)
    assert _check_joffe_overstatements("Federal law forbids capturing open Wi-Fi payload.")

    # F3 round 3 defects: ambiguous antecedent ("Elsewhere the question") and no "intentionally".
    ambiguous_elsewhere = (
        "In Joffe v. Google, 746 F.3d 920 (9th Cir. 2013), the Ninth Circuit reviewed the denial "
        "of Google's motion to dismiss and held that Wi-Fi payload is not a 'radio communication' "
        "under 18 USC 2510(16), so the unencrypted-radio rule does not reach 18 USC 2511(2)(g)(i). "
        "It binds only the Ninth Circuit (no other circuit had ruled as of 2026) and never decided "
        "whether reading beacon metadata is lawful. Elsewhere the question is open, not cleared: "
        "the Act's default, 18 USC 2511(1)(a), forbids intercepting an electronic communication "
        "unless an exemption applies."
    )
    ambiguous_issues = _check_joffe_explain(ambiguous_elsewhere)
    assert any("intentionally" in issue for issue in ambiguous_issues), ambiguous_issues
    assert any("payload question" in issue for issue in ambiguous_issues), ambiguous_issues

    missing_intentionally = ambiguous_elsewhere.replace(
        "Elsewhere the question is open",
        "Outside the Ninth Circuit the payload question is open",
    )
    assert any("intentionally" in issue for issue in _check_joffe_explain(missing_intentionally))

    accurate_rewording = (
        "In the amended opinion in Joffe v. Google, 746 F.3d 920 (9th Cir. 2013), the Ninth "
        "Circuit reviewed the denial of Google's motion to dismiss and held that Wi-Fi payload "
        "is not a 'radio communication' under 18 USC 2510(16), so the unencrypted-radio rule does "
        "not reach 18 USC 2511(2)(g)(i). The ruling binds only the Ninth Circuit (no other circuit "
        "had ruled as of 2026), decided no liability, and left beacon metadata undecided. Outside "
        "the Ninth Circuit the payload question is open: the Act's default, 18 USC 2511(1)(a), "
        "forbids intentionally intercepting an electronic communication unless an exemption "
        "applies."
    )
    assert _check_joffe_explain(accurate_rewording) == []
    assert _check_joffe_overstatements(accurate_rewording) == []


def test_m39_legal_text_matches_amended_joffe_opinion_and_cites_primary_sources():
    lesson = load_lesson_file(PACK_M39)
    all_prose = " ".join([
        lesson.concept,
        *lesson.stages.values(),
        lesson.verdict,
        *(c["label"] for c in lesson.choices),
    ])
    assert _check_joffe_explain(lesson.stages["explain"]) == []
    assert _check_joffe_overstatements(all_prose) == []

    raw = yaml.safe_load(PACK_M39.read_text(encoding="utf-8"))
    sources = [s for s in (raw.get("sources") or []) if isinstance(s, dict)]
    urls = [str(s.get("url", "")) for s in sources]
    titles = [str(s.get("title", "")) for s in sources]
    assert any("cdn.ca9.uscourts.gov" in u and "11-17483_opinion122713.pdf" in u for u in urls), (
        urls
    )
    # 2510(16) is cited from the official U.S. Code (govinfo or the House's uscode site).
    assert any(("govinfo.gov" in u or "uscode.house.gov" in u) and "2510" in u for u in urls), urls
    assert any("law.cornell.edu" in u and "/18/2511" in u for u in urls), urls
    assert any("2511(1)(a)" in t for t in titles), titles
    assert any("supremecourt.gov" in u and "13-1181" in u for u in urls), urls
