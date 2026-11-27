# ABOUTME: Reads the Waveshare UPS Module 3S battery from its INA219 over I2C.
# ABOUTME: Uses read-only register access and reports unmeasured when there is no sample.

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

INA219_BUS = 1
INA219_ADDRESS = 0x41
# Waveshare UPS Module 3S schematic, R2 across the INA219 inputs: 0.1 ohm.
# https://www.waveshare.net/w/upload/5/53/Ups01.pdf
UPS_SHUNT_OHMS = 0.1

# Waveshare's demo for the UPS Module 3S (UPS_Module_3S_V2/INA219.py, its __main__) turns the
# pack's bus voltage into a percentage on a straight line: p = (bus_voltage - 9) / 3.6 * 100,
# clamped to 0..100. 9 V is three 18650s at 3.0 V; 12.6 V is the 3S charge voltage. It is a
# voltage reading, so it runs high while the charger is plugged in (Waveshare's UPS wiki).
V_EMPTY = 9.0
V_FULL = 12.6

# Under this the status bar draws the gauge in the warning colour.
LOW_PERCENT = 15

# Every page renders the bar. Both readings and failures share this polling interval.
CACHE_S = 10.0


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


@dataclass(frozen=True)
class INA219Sample:
    voltage: float
    shunt_voltage: float
    current_a: float
    power_w: float | None


def _register(bus, number, *, signed=False):
    # Point to one register and read its two bytes, most significant byte first.
    data = bus.read_i2c_block_data(INA219_ADDRESS, number, 2)
    if len(data) != 2:
        raise OSError(f"short read of INA219 register 0x{number:02x}")
    return int.from_bytes(data, "big", signed=signed)


def read_ina219(bus) -> INA219Sample:
    """Read a completed INA219 conversion using TI SBOS448G §§8.5.1–8.5.3 and 8.6.3.

    Bus is 4 mV/count and signed shunt voltage is 10 µV/count. The calibration word
    sets the current and power LSBs. A cold chip still measures shunt voltage, so
    the board's known resistance gives current even before those registers work.
    """
    bus_word = _register(bus, 0x02)
    if bus_word & 0x01:
        raise ValueError("INA219 measurement overflow")
    if not bus_word & 0x02:
        raise ValueError("INA219 conversion not ready")
    voltage = (bus_word >> 3) * 0.004
    shunt = _register(bus, 0x01, signed=True) * 0.00001
    calibration = _register(bus, 0x05) & 0xFFFE  # Read existing scale. Bit 0 is unused.
    current = _register(bus, 0x04, signed=True)
    power = _register(bus, 0x03)
    if calibration == 0:
        return INA219Sample(voltage, shunt, shunt / UPS_SHUNT_OHMS, None)
    current_lsb = 0.04096 / (calibration * UPS_SHUNT_OHMS)
    return INA219Sample(voltage, shunt, current * current_lsb, power * 20 * current_lsb)


class BatteryGauge:
    """Read the UPS directly and cache the sample, or its absence, for CACHE_S."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
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
                from smbus2 import SMBus  # A console without I2C can still serve the gauge.

                with SMBus(INA219_BUS) as bus:
                    sample = read_ina219(bus)
            except (ImportError, OSError, ValueError) as exc:
                self._reading, self.reason = None, f"INA219 reading unavailable: {exc}"
            else:
                self._reading = BatteryReading(
                    voltage=sample.voltage, current_a=sample.current_a,
                    percent=percent_from_voltage(sample.voltage), charging=sample.current_a > 0,
                )
                self.reason = ""
            return self._reading
