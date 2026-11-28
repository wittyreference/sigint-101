# ABOUTME: Tests the battery gauge: the vendor's 3S voltage-to-percent curve, charging from the
# ABOUTME: current's sign, the honest None when nothing measures, and the status bar it feeds.

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sigint101 import battery
from sigint101.app import create_app
from sigint101.battery import BatteryGauge, BatteryReading, percent_from_voltage
from sigint101.hardware import RadioProbe

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_PACKS = FIXTURES / "lesson_packs"

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


class _Clocks:
    mono = 100.0


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
    with _client(tmp_path, _FixedGauge(None, "INA219 unavailable")) as client:
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
    with _client(tmp_path, _FixedGauge(None, "INA219 unavailable")) as client:
        body = client.get("/api/battery").json()
    assert body == {"present": False, "reason": "INA219 unavailable"}


# Datasheet-based bytes (TI SBOS448G, sections 8.5.1 and 8.6.3), not bench captures.
# Bus 3082 x 4 mV with CNVR set. R2 is 0.1 ohm on the Waveshare 3S schematic.
INA219_CHARGING = {
    0x01: b"\x04\x74",  # 1140 x 10 µV
    0x02: b"\x60\x52",  # 12.328 V
    0x03: b"\x02\xbe",  # 702 x 2 mW
    0x04: b"\x04\x74",  # 1140 x 100 µA
    0x05: b"\x10\x00",  # Existing calibration 4096, 100 µA per current count
}


class _ReadOnlyBus:
    """An SMBus double which exposes only register reads and close."""

    def __init__(self, number, registers):
        assert number == 1
        self.registers = registers
        self.reads = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True

    def read_i2c_block_data(self, address, register, length):
        assert (address, length) == (0x41, 2)
        self.reads.append(register)
        value = self.registers[register]
        if isinstance(value, OSError):
            raise value
        return list(value)


def _fake_ina219(monkeypatch, registers):
    buses = []

    def connect(number):
        bus = _ReadOnlyBus(number, registers)
        buses.append(bus)
        return bus

    monkeypatch.setitem(sys.modules, "smbus2", SimpleNamespace(SMBus=connect))
    return buses


def test_ina219_decodes_datasheet_byte_order_and_calibrated_scaling():
    bus = _ReadOnlyBus(1, INA219_CHARGING)
    sample = battery.read_ina219(bus)

    assert sample.voltage == pytest.approx(12.328)
    assert sample.shunt_voltage == pytest.approx(0.0114)
    assert sample.current_a == pytest.approx(0.114)
    assert sample.power_w == pytest.approx(1.404)
    assert set(bus.reads) == set(INA219_CHARGING)


def test_ina219_current_is_signed_and_power_is_unsigned():
    # A second existing calibration doubles the calibration word, halving both LSBs.
    # Negative one shunt count produces negative two current counts.
    registers = INA219_CHARGING | {
        0x01: b"\xff\xff",
        0x03: b"\x00\x00",
        0x04: b"\xff\xfe",
        0x05: b"\x20\x00",
    }
    sample = battery.read_ina219(_ReadOnlyBus(1, registers))
    assert sample.shunt_voltage == pytest.approx(-0.00001)
    assert sample.current_a == pytest.approx(-0.0001)
    # At 25.6 V and 1.2801 A, the unsigned power word has its high bit set.
    high_bus_voltage = registers | {
        0x01: b"\x32\x01",  # 12801 shunt counts
        0x02: b"\xc8\x02",
        0x03: b"\x80\x02",
        0x04: b"\x64\x02",  # 25602 current counts
    }
    sample = battery.read_ina219(_ReadOnlyBus(1, high_bus_voltage))
    assert sample.power_w == pytest.approx(32.770)


def test_uncalibrated_ina219_reads_pack_current_from_shunt(monkeypatch):
    registers = INA219_CHARGING | {
        0x01: b"\xe7\xc8",  # -6200 x 10 µV / 0.1 ohm = -0.62 A
        0x02: b"\x59\x12",  # 2850 x 4 mV with CNVR set
        0x03: b"\x00\x00",
        0x04: b"\x00\x00",
        0x05: b"\x00\x00",
    }
    buses = _fake_ina219(monkeypatch, registers)
    gauge = BatteryGauge()
    reading = gauge.read()
    assert reading is not None
    assert reading.voltage == pytest.approx(11.4)
    assert reading.current_a == pytest.approx(-0.62)
    assert reading.percent == 67
    assert reading.charging is False
    assert len(buses) == 1 and buses[0].closed
    assert battery.read_ina219(_ReadOnlyBus(1, registers)).power_w is None


@pytest.mark.parametrize("bad_register,replacement,reason", [
    (0x02, b"\x60\x50", "conversion"),
    (0x02, b"\x60\x53", "overflow"),
    (0x04, b"\x04", "short"),
    (0x01, OSError("Remote I/O error"), "Remote I/O error"),
])
def test_ina219_measurement_failure_clears_the_cached_reading(
        monkeypatch, bad_register, replacement, reason):
    registers = INA219_CHARGING.copy()
    buses = _fake_ina219(monkeypatch, registers)
    clocks = _Clocks()
    gauge = BatteryGauge(clock=lambda: clocks.mono)
    assert gauge.read().percent == 92
    assert gauge.read().percent == 92
    assert len(buses) == 1 and buses[0].closed
    registers[bad_register] = replacement
    clocks.mono += 10
    assert gauge.read() is None
    assert reason in gauge.reason
    assert gauge.read() is None
    assert len(buses) == 2 and buses[1].closed


def test_console_uses_the_ina219_for_page_and_api(tmp_path, monkeypatch):
    buses = _fake_ina219(monkeypatch, INA219_CHARGING)
    app = create_app(pack_dir=FIXTURE_PACKS, data_dir=tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/battery")
        assert response.status_code == 200
        body = response.json()
        assert body["present"] and body["charging"] and body["percent"] == 92
        assert body["current_a"] == pytest.approx(0.114)
        assert _batt_value(client.get("/operate/waterfall").text) == "92%"
    assert len(buses) == 1 and buses[0].closed



def test_unavailable_ina219_reports_unknown_on_page_and_api(tmp_path):
    app = create_app(pack_dir=FIXTURE_PACKS, data_dir=tmp_path)
    with TestClient(app) as client:
        body = client.get("/api/battery").json()
        assert body["present"] is False
        assert "I2C bus is unavailable" in body["reason"]
        assert _batt_value(client.get("/").text) == "&mdash;"
