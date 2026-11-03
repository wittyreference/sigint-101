# ABOUTME: Tests the battery gauge: the vendor's 3S voltage-to-percent curve, charging from the
# ABOUTME: current's sign, the honest None when nothing measures, and the status bar it feeds.

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101.app import create_app
from sigint101.battery import (
    LOW_PERCENT,
    BatteryGauge,
    BatteryReading,
    fetch_daemon,
    parse_daemon,
    percent_from_voltage,
)
from sigint101.hardware import RadioProbe

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_PACKS = FIXTURES / "lesson_packs"

# A real reading, captured from nomad-b's battery daemon (GET 127.0.0.1:9099/battery) on
# 2026-11-03 while the Recon sat on charge in the truck.
REAL = json.loads((FIXTURES / "battery" / "daemon-nomad-b.json").read_text())
# The wall-clock moment the real reading was taken, so the fixture reads as fresh.
REAL_AT = 1793671271.0  # 2026-11-03T02:01:11Z


# --- the vendor curve -----------------------------------------------------------


def test_percent_follows_the_vendors_linear_3s_curve():
    # Waveshare's INA219.py demo: p = (bus_voltage - 9) / 3.6 * 100, so 9 V is empty and
    # 12.6 V (the 3S charge voltage) is full.
    assert percent_from_voltage(9.0) == 0
    assert percent_from_voltage(10.8) == 50
    assert percent_from_voltage(12.6) == 100


def test_percent_is_clamped_to_the_gauge():
    # A charger can lift the bus above 12.6 V and a sagging pack can sit under 9 V; the vendor
    # demo clamps both rather than show 104 % or -3 %.
    assert percent_from_voltage(12.9) == 100
    assert percent_from_voltage(8.4) == 0


def test_percent_is_a_whole_number():
    assert percent_from_voltage(12.328) == 92


# --- parsing the daemon's reading -----------------------------------------------


def test_a_real_daemon_reading_parses_to_volts_percent_and_charging():
    reading = parse_daemon(REAL, now=REAL_AT)
    assert reading == BatteryReading(voltage=12.328, current_a=0.114, percent=92, charging=True)
    assert reading.low is False


def test_negative_current_is_the_pack_discharging():
    # Waveshare's UPS wiki: a negative current is the batteries feeding the Pi, positive is
    # the batteries charging.
    reading = parse_daemon(dict(REAL, current_a=-0.62), now=REAL_AT)
    assert reading.charging is False


def test_a_reading_under_the_low_mark_is_low():
    volts = 9.0 + 3.6 * (LOW_PERCENT - 1) / 100
    assert parse_daemon(dict(REAL, voltage=volts), now=REAL_AT).low is True
    assert parse_daemon(dict(REAL, voltage=12.0), now=REAL_AT).low is False


def test_a_daemon_reporting_an_i2c_error_is_no_reading():
    # The daemon keeps its last numbers when a poll fails and sets "error"; those numbers are no
    # longer a measurement of now.
    assert parse_daemon(dict(REAL, error="OSError(121, 'Remote I/O error')"), now=REAL_AT) is None


def test_a_daemon_that_has_not_polled_yet_is_no_reading():
    # battery_daemon.py's state before its first poll.
    blank = {"voltage": None, "current_a": None, "power_w": None, "percent": None,
             "timestamp": None, "error": None}
    assert parse_daemon(blank, now=REAL_AT) is None


def test_a_stale_reading_is_no_reading():
    # The daemon polls every 5 s; a reading a minute old means its poller has stopped.
    assert parse_daemon(REAL, now=REAL_AT + 60) is None
    assert parse_daemon(REAL, now=REAL_AT + 20) is not None


@pytest.mark.parametrize("payload", [
    {},
    {"voltage": "12.3", "current_a": 0.1, "timestamp": "2026-11-03T02:01:11Z", "error": None},
    dict(REAL, timestamp="yesterday"),
    dict(REAL, current_a=None),
    [],
])
def test_anything_else_is_no_reading(payload):
    assert parse_daemon(payload, now=REAL_AT) is None


# --- the gauge: one fetch per ten seconds, honest when absent ------------------


class _Clocks:
    def __init__(self):
        self.mono = 100.0
        self.wall = REAL_AT


def _gauge(fetch, clocks):
    return BatteryGauge(fetch=fetch, clock=lambda: clocks.mono, wall_clock=lambda: clocks.wall)


def test_the_gauge_reads_once_per_cache_window():
    calls = []
    clocks = _Clocks()

    def fetch():
        calls.append(1)
        return REAL

    gauge = _gauge(fetch, clocks)
    assert gauge.read().percent == 92
    clocks.mono += 9.9
    gauge.read()
    assert len(calls) == 1
    clocks.mono += 0.2
    gauge.read()
    assert len(calls) == 2


def test_an_unreachable_daemon_is_no_reading_and_is_not_hammered():
    calls = []
    clocks = _Clocks()

    def fetch():
        calls.append(1)
        raise ConnectionRefusedError(111, "Connection refused")

    gauge = _gauge(fetch, clocks)
    assert gauge.read() is None
    assert gauge.read() is None
    assert len(calls) == 1
    assert "Connection refused" in gauge.reason


def test_a_daemon_error_gives_its_reason():
    clocks = _Clocks()
    gauge = _gauge(lambda: dict(REAL, error="OSError(121, 'Remote I/O error')"), clocks)
    assert gauge.read() is None
    assert "Remote I/O error" in gauge.reason


# --- the real fetch, against a real local HTTP server ---------------------------


class _DaemonHandler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):
        body = json.dumps(REAL).encode()
        self.send_response(200 if self.path == "/battery" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def daemon_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DaemonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_fetch_daemon_reads_the_daemons_json(daemon_url):
    assert fetch_daemon(f"{daemon_url}/battery") == REAL


def test_fetch_daemon_raises_when_nothing_listens():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DaemonHandler)
    port = server.server_address[1]
    server.server_close()
    with pytest.raises(OSError):
        fetch_daemon(f"http://127.0.0.1:{port}/battery")


# --- the status bar on every page, and /api/battery -----------------------------


class _FixedGauge:
    """An app-boundary stand-in for the gauge: hands the console a reading, or none."""

    def __init__(self, reading, reason=""):
        self._reading = reading
        self.reason = reason

    def read(self):
        return self._reading


def _client(tmp_path, gauge):
    app = create_app(pack_dir=FIXTURE_PACKS, data_dir=tmp_path,
                     probe=lambda: RadioProbe(False, "lsusb", "no RTL2832U on USB bus"),
                     battery_gauge=gauge)
    return TestClient(app)


def _batt_value(html):
    import re
    return re.search(r'readout--batt.*?readout__value">(.*?)</span>', html, re.S).group(1)


@pytest.mark.parametrize("path", ["/", "/unit/1", "/lesson/1", "/exam", "/operate/waterfall"])
def test_every_page_shows_the_measured_battery(tmp_path, path):
    reading = BatteryReading(voltage=12.328, current_a=0.114, percent=92, charging=True)
    with _client(tmp_path, _FixedGauge(reading)) as client:
        html = client.get(path).text
    assert _batt_value(html) == "92%"
    assert 'batt__fill" style="width: 92%"' in html
    assert "12.33 V" in html


def test_the_error_page_shows_the_measured_battery_too(tmp_path):
    reading = BatteryReading(voltage=12.328, current_a=0.114, percent=92, charging=True)
    with _client(tmp_path, _FixedGauge(reading)) as client:
        html = client.get("/unit/99", headers={"accept": "text/html"}).text
    assert _batt_value(html) == "92%"


def test_no_reading_shows_a_dash_on_every_page(tmp_path):
    with _client(tmp_path, _FixedGauge(None, "battery daemon unreachable")) as client:
        html = client.get("/").text
    assert _batt_value(html) == "&mdash;"
    assert "batt--low" not in html


def test_the_bolt_shows_only_while_charging(tmp_path):
    charging = BatteryReading(voltage=12.3, current_a=0.11, percent=92, charging=True)
    draining = BatteryReading(voltage=11.4, current_a=-0.6, percent=67, charging=False)
    with _client(tmp_path, _FixedGauge(charging)) as client:
        on = client.get("/").text
    with _client(tmp_path, _FixedGauge(draining)) as client:
        off = client.get("/").text
    assert 'class="batt__bolt" aria-hidden="true">' in on
    assert 'class="batt__bolt" aria-hidden="true" hidden>' in off


def test_a_low_battery_takes_the_low_style(tmp_path):
    low = BatteryReading(voltage=9.4, current_a=-0.6, percent=11, charging=False)
    with _client(tmp_path, _FixedGauge(low)) as client:
        html = client.get("/").text
    assert "readout--batt batt batt--low" in html


def test_api_battery_returns_the_reading(tmp_path):
    reading = BatteryReading(voltage=12.328, current_a=0.114, percent=92, charging=True)
    with _client(tmp_path, _FixedGauge(reading)) as client:
        body = client.get("/api/battery").json()
    assert body == {"present": True, "percent": 92, "voltage": 12.328, "current_a": 0.114,
                    "charging": True, "low": False}


def test_api_battery_says_absent_with_the_reason(tmp_path):
    with _client(tmp_path, _FixedGauge(None, "battery daemon unreachable")) as client:
        body = client.get("/api/battery").json()
    assert body == {"present": False, "reason": "battery daemon unreachable"}
