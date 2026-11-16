# ABOUTME: The Recon's battery gauge — reads the INA219 on the Waveshare UPS Module 3S through the
# ABOUTME: local battery daemon (:9099), with the vendor's 3S percent curve; None when unmeasured.

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

# The battery daemon (battery-daemon.service, ~/battery_daemon.py) polls the INA219 at I2C 0x41
# every 5 s and serves the latest reading as JSON. The console reads that rather than the chip
# itself, so the two never contend for the bus or rewrite each other's calibration. The nftables
# lab guard keeps :9099 to loopback and the lab LAN; the console runs on the same box.
DAEMON_URL = "http://127.0.0.1:9099/battery"
DAEMON_TIMEOUT = 0.5

# Waveshare's demo for the UPS Module 3S (UPS_Module_3S_V2/INA219.py, its __main__) turns the
# pack's bus voltage into a percentage on a straight line: p = (bus_voltage - 9) / 3.6 * 100,
# clamped to 0..100. 9 V is three 18650s at 3.0 V; 12.6 V is the 3S charge voltage. It is a
# voltage reading, so it runs high while the charger is plugged in (Waveshare's UPS wiki).
V_EMPTY = 9.0
V_FULL = 12.6

# Under this the status bar draws the gauge in the warning colour.
LOW_PERCENT = 15

# The daemon polls every 5 s. A reading older than this means its poller has stopped, and an old
# number would be shown as if it were now.
MAX_AGE_S = 30.0

# Pages render the status bar on every request; the daemon is asked at most this often.
CACHE_S = 10.0

_TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class BatteryReading:
    """One measurement of the pack: bus volts, battery current, percent, and charging or not.

    Current follows Waveshare's sign: positive is the charger filling the pack, negative is the
    pack feeding the Pi.
    """

    voltage: float
    current_a: float
    percent: int
    charging: bool

    @property
    def low(self) -> bool:
        return self.percent < LOW_PERCENT

    def as_dict(self) -> dict:
        return {"present": True, "percent": self.percent, "voltage": self.voltage,
                "current_a": self.current_a, "charging": self.charging, "low": self.low}


def percent_from_voltage(volts: float) -> int:
    """The vendor's linear 3S curve, clamped and rounded to a whole percent."""
    pct = (volts - V_EMPTY) / (V_FULL - V_EMPTY) * 100.0
    return round(max(0.0, min(100.0, pct)))


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_daemon(payload, *, now: float) -> BatteryReading | None:
    """A reading from the daemon's JSON, or None when it holds no current measurement.

    None when the daemon reports an I2C error (it keeps its last numbers then), has not polled
    yet, or last polled more than MAX_AGE_S before `now` (wall-clock seconds).
    """
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    volts, amps = payload.get("voltage"), payload.get("current_a")
    if not (_number(volts) and _number(amps)):
        return None
    try:
        taken = datetime.strptime(payload.get("timestamp") or "", _TIMESTAMP)
    except ValueError:
        return None
    if now - taken.replace(tzinfo=timezone.utc).timestamp() > MAX_AGE_S:
        return None
    return BatteryReading(voltage=volts, current_a=amps,
                          percent=percent_from_voltage(volts), charging=amps > 0)


def fetch_daemon(url: str = DAEMON_URL, timeout: float = DAEMON_TIMEOUT):
    """GET the daemon's JSON. Raises on anything short of a parsed body (refused, timeout, junk)."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (loopback http)
        return json.loads(resp.read())


class BatteryGauge:
    """The status bar's battery source: the daemon's reading, cached for CACHE_S.

    `read()` gives a BatteryReading or None, never a default; `reason` says why it is None.
    A miss is cached too, so a box with no daemon (the droplet, a bare Pi) answers at once.
    """

    def __init__(self, *, fetch=fetch_daemon, clock=time.monotonic, wall_clock=time.time):
        self._fetch = fetch
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._read_at = None
        self._reading = None
        self.reason = ""

    def read(self) -> BatteryReading | None:
        with self._lock:
            now = self._clock()
            if self._read_at is not None and now - self._read_at < CACHE_S:
                return self._reading
            self._read_at = now
            try:
                payload = self._fetch()
            except Exception as exc:  # refused, timeout, junk JSON: no reading, and say why
                self._reading, self.reason = None, f"battery daemon unreachable: {exc}"
                return None
            self._reading = parse_daemon(payload, now=self._wall_clock())
            if self._reading is not None:
                self.reason = ""
            elif isinstance(payload, dict) and payload.get("error"):
                self.reason = f"battery daemon reports: {payload['error']}"
            else:
                self.reason = "battery daemon has no current reading"
            return self._reading
