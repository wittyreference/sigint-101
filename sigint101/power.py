# ABOUTME: Low-battery safe-shutdown watcher reading the UPS INA219 over I2C (read-only).
# ABOUTME: Ships hard-disarmed in slice 2a; manages hold, notice.json contract, and poweroff.

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

from . import battery
from .battery import V_FULL, BatteryReading, ConversionNotReadyError, INA219Config

LOGGER = logging.getLogger("sigint101.power")

DEFAULT_NOTICE_PATH = Path("/run/sigint101-power/notice.json")
ENV_THRESHOLD_V = "SIGINT101_POWER_THRESHOLD_V"
ENV_RECOVER_V = "SIGINT101_POWER_RECOVER_V"

# Exclusive threshold bounds (volts) for a 3S Li-ion pack.
THRESHOLD_MIN_V = 8.0
THRESHOLD_MAX_V = V_FULL  # 12.6 V
RECOVER_MAX_V = 1.0

# Plausibility window for a running 3S pack; readings outside this are BAD, never LOW.
PLAUSIBLE_MIN_V = 6.0
PLAUSIBLE_MAX_V = 13.5

# Timing constants (all monotonic seconds).
POLL_INTERVAL_S = 5.0
NOTICE_POLL_S = 2.0
HOLD_S = 60.0
NOTICE_S = 60.0
MAX_GAP_S = 15.0
NOTICE_STALE_S = 15.0
NOTICE_FUTURE_TOLERANCE_S = 1.0
NOTICE_MAX_BYTES = 4096
POWEROFF_TIMEOUT_S = 30.0
POWEROFF_RETRY_S = 60.0
CAN_POWER_OFF_TIMEOUT_S = 10.0

CNVR_RETRY_MIN_S = 0.02
CNVR_RETRY_MAX_S = 0.5
CNVR_RETRY_MARGIN = 1.25

POWEROFF_CMD = ("/usr/bin/systemctl", "poweroff")
CAN_POWER_OFF_CMD = (
    "/usr/bin/busctl",
    "call",
    "org.freedesktop.login1",
    "/org/freedesktop/login1",
    "org.freedesktop.login1.Manager",
    "CanPowerOff",
)
MINIMAL_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}

LOCKED_2A_REASON = "arming waits on the notice slice (2b)"

# TI SBOS448G §8.6.2.1 Table 5 typical ADC conversion times (seconds) indexed by 4-bit BADC/SADC.
_ADC_CONVERSION_TIME_S: tuple[float, ...] = (
    0.000084,  # 0000: 9-bit, 1 sample (84 us)
    0.000148,  # 0001: 10-bit, 1 sample (148 us)
    0.000276,  # 0010: 11-bit, 1 sample (276 us)
    0.000532,  # 0011: 12-bit, 1 sample (532 us)
    0.000084,  # 0100: 9-bit, 1 sample (bit 2 don't care)
    0.000148,  # 0101: 10-bit, 1 sample
    0.000276,  # 0110: 11-bit, 1 sample
    0.000532,  # 0111: 12-bit, 1 sample
    0.000532,  # 1000: 12-bit, 1 sample (532 us)
    0.001060,  # 1001: 2 samples (1.06 ms)
    0.002130,  # 1010: 4 samples (2.13 ms)
    0.004260,  # 1011: 8 samples (4.26 ms)
    0.008510,  # 1100: 16 samples (8.51 ms)
    0.017020,  # 1101: 32 samples (17.02 ms)
    0.034050,  # 1110: 64 samples (34.05 ms)
    0.068100,  # 1111: 128 samples (68.10 ms)
)


def _validate_threshold_v(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("threshold_v must be a finite float")
    val = float(value)
    if not math.isfinite(val) or not (THRESHOLD_MIN_V < val < THRESHOLD_MAX_V):
        raise ValueError(
            f"threshold_v must lie strictly between {THRESHOLD_MIN_V} V and {THRESHOLD_MAX_V} V"
        )
    return val


def parse_threshold(raw: str | None) -> tuple[float | None, str]:
    """Parse SIGINT101_POWER_THRESHOLD_V.

    Slice 2a ships hard-disarmed in code: any configured value is refused until the
    notice slice (2b) lands under its own review.
    """
    if raw is None or not raw.strip():
        return None, "unset"
    return None, LOCKED_2A_REASON


def parse_recover_v(raw: str | None) -> tuple[float, str]:
    """Parse optional SIGINT101_POWER_RECOVER_V hysteresis in (0.0, 1.0] volts."""
    if raw is None or not raw.strip():
        return 0.0, ""
    try:
        value = float(raw.strip())
    except ValueError:
        return 0.0, f"invalid {ENV_RECOVER_V} value {raw!r}; using 0.0 V"
    if not math.isfinite(value) or not (0.0 < value <= RECOVER_MAX_V):
        return 0.0, f"{ENV_RECOVER_V}={value} outside (0.0, {RECOVER_MAX_V}]; using 0.0 V"
    return value, ""


def adc_time_s(code: int) -> float:
    """Return TI SBOS448G Table 5 typical conversion time (seconds) for a 4-bit ADC code."""
    return _ADC_CONVERSION_TIME_S[code & 0x0F]


def cnvr_retry_wait_s(config: INA219Config | None) -> float:
    """Compute bounded one-shot CNVR retry delay from register 0x00 BADC and SADC fields."""
    if config is None:
        return CNVR_RETRY_MIN_S
    cycle_s = adc_time_s(config.sadc) + adc_time_s(config.badc)
    return min(CNVR_RETRY_MAX_S, max(CNVR_RETRY_MIN_S, CNVR_RETRY_MARGIN * cycle_s))


def is_plausible_reading(reading: BatteryReading | None) -> bool:
    """Return True iff reading carries finite values inside the 3S plausibility bounds."""
    if reading is None:
        return False
    if not (math.isfinite(reading.voltage) and math.isfinite(reading.current_a)):
        return False
    return PLAUSIBLE_MIN_V <= reading.voltage <= PLAUSIBLE_MAX_V


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read_power_notice(
    path: Path = DEFAULT_NOTICE_PATH,
    *,
    clock=time.monotonic,
) -> dict:
    """Read and validate notice.json without following symlinks or blocking on FIFOs."""
    inactive = {"active": False}
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return inactive
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > NOTICE_MAX_BYTES:
            return inactive
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, NOTICE_MAX_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > NOTICE_MAX_BYTES:
                return inactive
    except OSError:
        return inactive
    finally:
        os.close(fd)

    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return inactive
    if not isinstance(data, dict) or data.get("active") is not True:
        return inactive

    failed = data.get("failed")
    if not isinstance(failed, bool):
        return inactive
    for field in ("voltage", "threshold_v", "started_mono", "deadline_mono", "updated_mono"):
        if not _is_finite_number(data.get(field)):
            return inactive

    now = float(clock())
    updated_mono = float(data["updated_mono"])
    age = now - updated_mono
    if age > NOTICE_STALE_S or age < -NOTICE_FUTURE_TOLERANCE_S:
        return inactive

    voltage = float(data["voltage"])
    threshold_v = float(data["threshold_v"])
    if failed:
        retry_deadline = data.get("retry_deadline_mono")
        if not _is_finite_number(retry_deadline):
            return inactive
        return {
            "active": True,
            "failed": True,
            "retry_in_s": max(0, math.ceil(float(retry_deadline) - now)),
            "voltage": voltage,
            "threshold_v": threshold_v,
        }

    deadline_mono = float(data["deadline_mono"])
    return {
        "active": True,
        "failed": False,
        "remaining_s": max(0, math.ceil(deadline_mono - now)),
        "voltage": voltage,
        "threshold_v": threshold_v,
    }


def _write_notice_file(path: Path, payload: dict) -> None:
    """Atomically write notice.json via a sibling temporary file (mode 0644, O_NOFOLLOW)."""
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    tmp = path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _unlink_notice_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        LOGGER.warning("could not remove notice file %s: %s", path, exc)


def run_poweroff() -> None:
    """Invoke systemctl poweroff with bounded timeout and clean child environment."""
    subprocess.run(
        list(POWEROFF_CMD),
        check=True,
        timeout=POWEROFF_TIMEOUT_S,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        env=dict(MINIMAL_ENV),
    )


def check_can_power_off(runner=subprocess.run) -> tuple[bool, str]:
    """Query logind CanPowerOff over D-Bus without making any state change."""
    try:
        result = runner(
            list(CAN_POWER_OFF_CMD),
            capture_output=True,
            text=True,
            timeout=CAN_POWER_OFF_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            env=dict(MINIMAL_ENV),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode == 0 and out == 's "yes"':
        return True, out
    return False, out or err or f"exit {result.returncode}"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def installed_digests() -> dict[str, str]:
    return {
        "power.py": file_sha256(Path(__file__).resolve()),
        "battery.py": file_sha256(Path(battery.__file__).resolve()),
    }


class PowerWatcher:
    """Monotonic low-battery safe-shutdown state machine."""

    def __init__(
        self,
        *,
        threshold_v: float | None = None,
        disarmed_reason: str = "unset",
        recover_v: float = 0.0,
        notice_path: Path = DEFAULT_NOTICE_PATH,
        reader=None,
        clock=time.monotonic,
        sleep=time.sleep,
        poweroff=run_poweroff,
        can_power_off=check_can_power_off,
        logger: logging.Logger | None = None,
    ):
        self.threshold_v = (
            None if threshold_v is None else _validate_threshold_v(threshold_v)
        )
        self.disarmed_reason = disarmed_reason
        self.recover_v = float(recover_v)
        self.notice_path = Path(notice_path)
        self._reader = reader or battery.read_battery_once
        self._clock = clock
        self._sleep = sleep
        self._poweroff = poweroff
        self._can_power_off = can_power_off
        self._log = logger or LOGGER

        self.state = "DISARMED" if self.threshold_v is None else "NORMAL"
        self._hold_since: float | None = None
        self._notice_since: float | None = None
        self._deadline_mono: float | None = None
        self._retry_deadline_mono: float | None = None
        self._last_notice_write: float | None = None
        self._last_good_mono: float | None = None
        self._logged_disarmed = False
        self._logged_bad_mode = False
        self._last_bad_log: float | None = None

    @property
    def poll_interval_s(self) -> float:
        if self.state in ("NOTICE", "POWEROFF_FAILED"):
            return NOTICE_POLL_S
        return POLL_INTERVAL_S

    def startup(self) -> None:
        digests = installed_digests()
        for name, sha in digests.items():
            self._log.info("installed %s sha256=%s", name, sha)
        _unlink_notice_file(self.notice_path)
        if self.threshold_v is None:
            self.state = "DISARMED"
            self._log_disarmed_once()
            return
        self.state = "NORMAL"
        self._log.info(
            "armed at %.2f V (recover_v=%.2f V)", self.threshold_v, self.recover_v
        )
        ok, detail = self._can_power_off()
        if not ok:
            self._log.error("armed but not authorized to power off: %s", detail)

    def shutdown(self) -> None:
        if self.state != "POWERING_OFF":
            _unlink_notice_file(self.notice_path)

    def _log_disarmed_once(self) -> None:
        if not self._logged_disarmed:
            self._logged_disarmed = True
            self._log.info(
                "power watcher disarmed (%s); no I2C reads will be made and box will not power off",
                self.disarmed_reason,
            )

    def _log_bad_mode_once(self, config: INA219Config) -> None:
        if not self._logged_bad_mode:
            self._logged_bad_mode = True
            self._log.error(
                "INA219 not converting continuously (config=0x%04x, mode=0x%x): "
                "cannot act on readings",
                config.raw,
                config.mode,
            )

    def _log_bad_sample(self, now: float, detail: str) -> None:
        if self._last_bad_log is None or now - self._last_bad_log >= MAX_GAP_S:
            self._last_bad_log = now
            self._log.warning("bad battery sample: %s", detail)

    def _reset_to_normal(self, *, cancel_reason: str | None = None) -> None:
        was_notice = self.state in ("NOTICE", "POWEROFF_FAILED")
        self.state = "NORMAL"
        self._hold_since = None
        self._notice_since = None
        self._deadline_mono = None
        self._retry_deadline_mono = None
        self._last_notice_write = None
        self._last_good_mono = None
        if was_notice:
            _unlink_notice_file(self.notice_path)
            if cancel_reason:
                self._log.info("power-off notice cancelled: %s", cancel_reason)

    def _read_and_classify(self, now: float) -> tuple[str, BatteryReading | None]:
        def _single_read() -> tuple[INA219Config, BatteryReading | None]:
            return self._reader()

        try:
            config, reading = _single_read()
        except ConversionNotReadyError as exc:
            if exc.config is not None and not exc.config.continuous_shunt_and_bus:
                self._log_bad_mode_once(exc.config)
                return "BAD", None
            wait_s = cnvr_retry_wait_s(exc.config)
            self._sleep(wait_s)
            try:
                config, reading = _single_read()
            except (ConversionNotReadyError, ImportError, OSError, ValueError) as retry_exc:
                self._log_bad_sample(now, f"CNVR retry failed: {retry_exc}")
                return "BAD", None
        except (ImportError, OSError, ValueError) as exc:
            self._log_bad_sample(now, str(exc))
            return "BAD", None

        if not config.continuous_shunt_and_bus:
            self._log_bad_mode_once(config)
            return "BAD", None
        self._logged_bad_mode = False

        if not is_plausible_reading(reading):
            detail = (
                "no reading"
                if reading is None
                else f"implausible voltage {reading.voltage:.3f} V"
            )
            self._log_bad_sample(now, detail)
            return "BAD", None

        assert reading is not None and self.threshold_v is not None
        if reading.charging or reading.current_a > 0:
            return "OK", reading
        if reading.voltage >= self.threshold_v + self.recover_v:
            return "OK", reading
        if reading.voltage < self.threshold_v:
            return "LOW", reading
        return "HYSTERESIS", reading

    def _write_notice(self, now: float, reading: BatteryReading, *, failed: bool) -> None:
        assert self.threshold_v is not None
        assert self._notice_since is not None and self._deadline_mono is not None
        payload: dict[str, object] = {
            "active": True,
            "failed": failed,
            "voltage": round(reading.voltage, 4),
            "threshold_v": self.threshold_v,
            "started_mono": self._notice_since,
            "deadline_mono": self._deadline_mono,
            "updated_mono": now,
        }
        if failed:
            assert self._retry_deadline_mono is not None
            payload["retry_deadline_mono"] = self._retry_deadline_mono
        _write_notice_file(self.notice_path, payload)
        self._last_notice_write = now

    def _attempt_poweroff(self, now: float, reading: BatteryReading) -> None:
        self._log.critical(
            "low battery persisted through notice (%.2f V < %.2f V); invoking systemctl poweroff",
            reading.voltage,
            self.threshold_v,
        )
        try:
            self._poweroff()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            after = float(self._clock())
            self._log.critical("automatic power-off failed: %s", exc)
            self.state = "POWEROFF_FAILED"
            self._retry_deadline_mono = after + POWEROFF_RETRY_S
            self._last_good_mono = after
            self._write_notice(after, reading, failed=True)
            return
        self.state = "POWERING_OFF"

    def tick(self) -> str:
        """Advance the watcher by one poll cycle and return the resulting state."""
        if self.state == "DISARMED":
            self._log_disarmed_once()
            return self.state
        if self.state == "POWERING_OFF":
            return self.state

        now = float(self._clock())
        kind, reading = self._read_and_classify(now)

        if kind == "OK":
            assert reading is not None
            reason = (
                f"charger connected ({reading.voltage:.2f} V, {reading.current_a:+.2f} A)"
                if reading.charging
                else f"voltage recovered ({reading.voltage:.2f} V)"
            )
            self._reset_to_normal(cancel_reason=reason)
            return self.state

        if kind == "BAD":
            if self._last_good_mono is not None and now - self._last_good_mono > MAX_GAP_S:
                self._log.warning(
                    "battery readings unavailable for %.1f s (> %.1f s); resetting to NORMAL",
                    now - self._last_good_mono,
                    MAX_GAP_S,
                )
                self._reset_to_normal(cancel_reason="persistent sensor error")
            return self.state

        assert reading is not None
        if self.state == "NORMAL":
            if kind == "LOW":
                self.state = "HOLDING"
                self._hold_since = now
                self._last_good_mono = now
            return self.state

        if self.state == "HOLDING":
            # Every-tick gap check (R2-F4): a blind stall > 15 s breaks the hold streak.
            if self._last_good_mono is not None and now - self._last_good_mono > MAX_GAP_S:
                self._log.warning(
                    "hold streak broken by %.1f s gap (> %.1f s); restarting hold",
                    now - self._last_good_mono,
                    MAX_GAP_S,
                )
                self._reset_to_normal()
                if kind == "LOW":
                    self.state = "HOLDING"
                    self._hold_since = now
                    self._last_good_mono = now
                return self.state

            self._last_good_mono = now
            assert self._hold_since is not None
            if kind == "LOW" and now - self._hold_since >= HOLD_S:
                self.state = "NOTICE"
                self._notice_since = now
                self._deadline_mono = now + NOTICE_S
                self._write_notice(now, reading, failed=False)
                self._log.warning(
                    "low battery held for %.0f s (%.2f V < %.2f V); starting %.0f s notice",
                    HOLD_S,
                    reading.voltage,
                    self.threshold_v,
                    NOTICE_S,
                )
            return self.state

        if self.state == "NOTICE":
            assert self._last_notice_write is not None and self._deadline_mono is not None
            if (
                now - self._last_notice_write > NOTICE_STALE_S
                or (self._last_good_mono is not None and now - self._last_good_mono > MAX_GAP_S)
            ):
                self._log.warning(
                    "watcher stalled during notice (%.1f s); restarting fresh %.0f s countdown",
                    now - self._last_notice_write,
                    NOTICE_S,
                )
                self._notice_since = now
                self._deadline_mono = now + NOTICE_S
                self._last_good_mono = now
                self._write_notice(now, reading, failed=False)
                return self.state

            self._last_good_mono = now
            if kind == "LOW" and now >= self._deadline_mono:
                self._write_notice(now, reading, failed=False)
                self._attempt_poweroff(now, reading)
            else:
                self._write_notice(now, reading, failed=False)
            return self.state

        if self.state == "POWEROFF_FAILED":
            assert self._last_notice_write is not None and self._retry_deadline_mono is not None
            if (
                now - self._last_notice_write > NOTICE_STALE_S
                or (self._last_good_mono is not None and now - self._last_good_mono > MAX_GAP_S)
            ):
                self._log.warning(
                    "watcher stalled in POWEROFF_FAILED (%.1f s); "
                    "restarting %.0f s retry countdown",
                    now - self._last_notice_write,
                    POWEROFF_RETRY_S,
                )
                self._retry_deadline_mono = now + POWEROFF_RETRY_S
                self._last_good_mono = now
                self._write_notice(now, reading, failed=True)
                return self.state

            self._last_good_mono = now
            if kind == "LOW" and now >= self._retry_deadline_mono:
                self._attempt_poweroff(now, reading)
            else:
                self._write_notice(now, reading, failed=True)
            return self.state

        return self.state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SIGINT 101 low-battery power watcher")
    parser.add_argument(
        "--startup-only",
        action="store_true",
        help="Run startup checks (log digests and armed/disarmed status) and exit 0.",
    )
    parser.add_argument(
        "--notice-path",
        type=Path,
        default=DEFAULT_NOTICE_PATH,
        help="Path to the runtime notice.json state file.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    threshold_v, disarmed_reason = parse_threshold(os.environ.get(ENV_THRESHOLD_V))
    recover_v, recover_warn = parse_recover_v(os.environ.get(ENV_RECOVER_V))
    if recover_warn:
        LOGGER.warning("%s", recover_warn)

    watcher = PowerWatcher(
        threshold_v=threshold_v,
        disarmed_reason=disarmed_reason,
        recover_v=recover_v,
        notice_path=args.notice_path,
    )
    watcher.startup()
    if args.startup_only:
        return 0

    stop = False

    def _handle_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    try:
        while not stop:
            watcher.tick()
            if watcher.state == "POWERING_OFF":
                break
            time.sleep(watcher.poll_interval_s)
    finally:
        watcher.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
