# ABOUTME: Tests the low-battery safe-shutdown watcher (sigint101.power), its state machine,
# ABOUTME: INA219 plausibility/CNVR checks, notice schema, poweroff hygiene, and unit hardening.

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigint101 import battery
from sigint101.battery import BatteryReading, ConversionNotReadyError, INA219Config
from sigint101.power import (
    MAX_GAP_S,
    MINIMAL_ENV,
    NOTICE_S,
    NOTICE_STALE_S,
    PLAUSIBLE_MAX_V,
    PLAUSIBLE_MIN_V,
    POWEROFF_RETRY_S,
    POWEROFF_TIMEOUT_S,
    PowerWatcher,
    check_can_power_off,
    cnvr_retry_wait_s,
    main,
    parse_recover_v,
    parse_threshold,
    read_power_notice,
    run_poweroff,
)

REPO = Path(__file__).resolve().parent.parent
UNIT_FILE = REPO / "deploy" / "system" / "sigint101-power.service"

CFG_DEFAULT = INA219Config.from_word(0x399F)   # continuous shunt+bus, 12-bit 1-sample
CFG_128 = INA219Config.from_word(0x3FFF)       # continuous shunt+bus, 12-bit 128-sample
CFG_TRIGGERED = INA219Config.from_word(0x399B) # triggered shunt+bus (MODE=0b011)
CFG_SHUNT_ONLY = INA219Config.from_word(0x399D) # shunt-only continuous (MODE=0b101)


class _Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def _reading(voltage: float, *, current_a: float = -0.6, charging: bool | None = None):
    is_charging = (current_a > 0) if charging is None else charging
    return BatteryReading(
        voltage=voltage,
        current_a=current_a,
        percent=battery.percent_from_voltage(voltage),
        charging=is_charging,
    )


def _make_watcher(
    tmp_path: Path,
    *,
    threshold_v: float | None = 9.5,
    disarmed_reason: str = "unset",
    recover_v: float = 0.0,
    reader=None,
    clock=None,
    sleep=None,
    poweroff=None,
    can_power_off=None,
):
    clk = clock or _Clock()
    sleeps: list[float] = []
    poweroffs: list[float] = []
    notice_path = tmp_path / "notice.json"

    def _default_poweroff():
        poweroffs.append(clk())

    watcher = PowerWatcher(
        threshold_v=threshold_v,
        disarmed_reason=disarmed_reason,
        recover_v=recover_v,
        notice_path=notice_path,
        reader=reader or (lambda: (CFG_DEFAULT, _reading(11.4))),
        clock=clk,
        sleep=sleep or sleeps.append,
        poweroff=poweroff or _default_poweroff,
        can_power_off=can_power_off or (lambda: (True, 's "yes"')),
    )
    return watcher, clk, notice_path, poweroffs, sleeps


# --- Test 1: threshold parsing (slice 2b arms valid (8.0, 12.6), invalid stays disarmed) ---


@pytest.mark.parametrize("raw,expected_fragment", [
    (None, "unset"),
    ("", "unset"),
    ("   ", "unset"),
    ("abc", "invalid"),
    ("nan", "outside"),
    ("inf", "outside"),
    ("-inf", "outside"),
    ("8.0", "outside"),
    ("7.5", "outside"),
    ("12.6", "outside"),
    ("96", "outside"),
])
def test_unset_or_invalid_threshold_stays_disarmed_logs_once_and_makes_zero_bus_calls(
    tmp_path, caplog, raw, expected_fragment
):
    threshold_v, reason = parse_threshold(raw)
    assert threshold_v is None
    assert expected_fragment in reason

    bus_calls = 0

    def fail_if_read():
        nonlocal bus_calls
        bus_calls += 1
        return CFG_DEFAULT, _reading(9.1)

    watcher, clk, notice_path, poweroffs, _ = _make_watcher(
        tmp_path,
        threshold_v=threshold_v,
        disarmed_reason=reason,
        reader=fail_if_read,
    )
    # Plant a stale notice file to confirm startup clears it even when disarmed.
    notice_path.write_text('{"active": true}', encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="sigint101.power"):
        watcher.startup()
        for _ in range(60):  # 300 s at 5 s per tick
            assert watcher.tick() == "DISARMED"
            clk.advance(5.0)

    assert bus_calls == 0
    assert not notice_path.exists()
    assert poweroffs == []
    disarmed_logs = [r.getMessage() for r in caplog.records if "disarmed" in r.getMessage().lower()]
    assert len(disarmed_logs) == 1
    assert expected_fragment in disarmed_logs[0]


@pytest.mark.parametrize("raw", ["9.50", " 8.01 ", "12.59"])
def test_valid_threshold_stays_hard_disarmed_in_slice_2b(raw):
    threshold_v, reason = parse_threshold(raw)
    assert threshold_v is None
    assert "slice 2b" in reason and "2c" in reason


@pytest.mark.parametrize("bad_threshold", [float("nan"), float("inf"), 8.0, 7.5, 12.6, 96.0])
def test_armed_watcher_rejects_out_of_range_threshold_directly(tmp_path, bad_threshold):
    with pytest.raises(ValueError):
        _make_watcher(tmp_path, threshold_v=bad_threshold)


# --- Test 2: charging, None, implausible voltage, and wrong INA219 MODE ---------


@pytest.mark.parametrize("case,sample_fn", [
    ("charging", lambda: (CFG_DEFAULT, _reading(9.1, current_a=0.35, charging=True))),
    ("none", lambda: (CFG_DEFAULT, None)),
    ("zero_v", lambda: (CFG_DEFAULT, _reading(0.0, current_a=-0.5))),
    ("too_low_v", lambda: (CFG_DEFAULT, _reading(3.0, current_a=-0.5))),
    ("too_high_v", lambda: (CFG_DEFAULT, _reading(14.5, current_a=-0.5))),
    ("triggered_mode", lambda: (CFG_TRIGGERED, _reading(9.1, current_a=-0.5))),
    ("shunt_only_mode", lambda: (CFG_SHUNT_ONLY, _reading(9.1, current_a=-0.5))),
])
def test_no_poweroff_while_charging_with_no_reading_implausible_voltage_or_wrong_mode(
    tmp_path, caplog, case, sample_fn
):
    watcher, clk, notice_path, poweroffs, _ = _make_watcher(
        tmp_path, threshold_v=9.5, reader=sample_fn
    )
    with caplog.at_level(logging.INFO, logger="sigint101.power"):
        watcher.startup()
        for _ in range(60):  # 300 s
            assert watcher.tick() == "NORMAL"
            clk.advance(5.0)

    assert not notice_path.exists()
    assert poweroffs == []
    if case in ("triggered_mode", "shunt_only_mode"):
        mode_errors = [
            r for r in caplog.records
            if r.levelno == logging.ERROR and "continuously" in r.getMessage().lower()
        ]
        assert len(mode_errors) == 1


# --- Test 3: single low sample or intermittent sag ------------------------------


def test_no_poweroff_on_single_low_sample_or_intermittent_sag(tmp_path):
    current = [CFG_DEFAULT, _reading(9.2)]
    watcher, clk, notice_path, poweroffs, _ = _make_watcher(
        tmp_path, threshold_v=9.5, reader=lambda: tuple(current)
    )
    watcher.startup()

    # t=0: low enters HOLDING, no notice.
    assert watcher.tick() == "HOLDING"
    assert not notice_path.exists()

    # t=5: recovery resets to NORMAL.
    clk.advance(5.0)
    current[1] = _reading(9.8)
    assert watcher.tick() == "NORMAL"

    # t=55: second low starts a fresh 60 s hold from t=55 (not t=0).
    clk.advance(50.0)
    current[1] = _reading(9.2)
    assert watcher.tick() == "HOLDING"

    # Poll every 5 s up to t=110 (55 s into the new hold): still HOLDING.
    for _ in range(11):
        clk.advance(5.0)
        assert watcher.tick() == "HOLDING"
    assert not notice_path.exists()
    assert poweroffs == []


# --- Test 4: nominal hold/notice/poweroff, stall mid-notice, poweroff failure ---


def test_hold_notice_poweroff_nominal_monotonic_sequence(tmp_path):
    saw_notice_before_poweroff = []
    clk = _Clock(1000.0)
    notice_path = tmp_path / "notice.json"
    poweroffs: list[float] = []

    def _poweroff():
        saw_notice_before_poweroff.append(notice_path.is_file())
        poweroffs.append(clk())

    watcher = PowerWatcher(
        threshold_v=9.5,
        notice_path=notice_path,
        reader=lambda: (CFG_DEFAULT, _reading(9.2)),
        clock=clk,
        sleep=lambda _: None,
        poweroff=_poweroff,
        can_power_off=lambda: (True, 's "yes"'),
    )
    watcher.startup()

    # t=0..55: HOLDING, no notice file yet.
    assert watcher.tick() == "HOLDING"
    assert not notice_path.exists()
    for _ in range(11):
        clk.advance(5.0)
        assert watcher.tick() == "HOLDING"
        assert not notice_path.exists()

    # t=59.9: still HOLDING.
    clk.advance(4.9)
    assert watcher.tick() == "HOLDING"
    assert not notice_path.exists()

    # t=60.0: enters NOTICE, writes notice.json.
    clk.advance(0.1)
    assert watcher.tick() == "NOTICE"
    assert notice_path.is_file()
    raw = json.loads(notice_path.read_text(encoding="utf-8"))
    assert raw["active"] is True
    assert raw["failed"] is False
    assert raw["started_mono"] == pytest.approx(1060.0)
    assert raw["deadline_mono"] == pytest.approx(1120.0)
    assert raw["updated_mono"] == pytest.approx(1060.0)
    assert set(raw) == {
        "active", "failed", "voltage", "threshold_v",
        "started_mono", "deadline_mono", "updated_mono",
    }
    assert read_power_notice(notice_path, clock=clk)["remaining_s"] == 60

    # t=62..118: NOTICE refreshes updated_mono and counts down.
    for step in range(1, 30):
        clk.advance(2.0)
        assert watcher.tick() == "NOTICE"
        view = read_power_notice(notice_path, clock=clk)
        assert view["active"] is True and view["failed"] is False
        assert view["remaining_s"] == 60 - 2 * step
        assert poweroffs == []

    # t=119.9: 1 s left, still NOTICE.
    clk.advance(1.9)
    assert watcher.tick() == "NOTICE"
    assert read_power_notice(notice_path, clock=clk)["remaining_s"] == 1
    assert poweroffs == []

    # t=120.0: fresh good LOW -> POWERING_OFF, poweroff called once with notice already written.
    clk.advance(0.1)
    assert watcher.tick() == "POWERING_OFF"
    assert len(poweroffs) == 1
    assert saw_notice_before_poweroff == [True]

    # Subsequent ticks do not re-invoke poweroff.
    clk.advance(5.0)
    assert watcher.tick() == "POWERING_OFF"
    assert len(poweroffs) == 1


def test_stall_mid_notice_restarts_fresh_countdown_without_poweroff(tmp_path, caplog):
    watcher, clk, notice_path, poweroffs, _ = _make_watcher(
        tmp_path, threshold_v=9.5, reader=lambda: (CFG_DEFAULT, _reading(9.2))
    )
    watcher.startup()

    # Advance through the 60 s hold into NOTICE.
    assert watcher.tick() == "HOLDING"
    for _ in range(12):
        clk.advance(5.0)
        watcher.tick()
    assert watcher.state == "NOTICE"

    # Advance near the deadline (t=110), then stall 20 s past original deadline (t=130).
    for _ in range(25):
        clk.advance(2.0)
        assert watcher.tick() == "NOTICE"

    with caplog.at_level(logging.WARNING, logger="sigint101.power"):
        clk.advance(20.0)  # > NOTICE_STALE_S (15 s) and past original deadline 1120
        assert watcher.tick() == "NOTICE"

    assert poweroffs == []
    raw = json.loads(notice_path.read_text(encoding="utf-8"))
    assert raw["deadline_mono"] == pytest.approx(clk() + NOTICE_S)
    assert raw["updated_mono"] == pytest.approx(clk())
    assert any("stall" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(1, ["/usr/bin/systemctl", "poweroff"]),
    subprocess.TimeoutExpired(["/usr/bin/systemctl", "poweroff"], POWEROFF_TIMEOUT_S),
    FileNotFoundError("No such file or directory: '/usr/bin/systemctl'"),
])
def test_poweroff_failure_enters_failed_state_announces_retry_and_recovers(tmp_path, caplog, exc):
    sample = [_reading(9.2)]
    attempts = 0
    should_fail = True

    def flaky_poweroff():
        nonlocal attempts
        attempts += 1
        if should_fail:
            raise exc

    watcher, clk, notice_path, _, _ = _make_watcher(
        tmp_path,
        threshold_v=9.5,
        reader=lambda: (CFG_DEFAULT, sample[0]),
        poweroff=flaky_poweroff,
    )
    watcher.startup()

    # Reach NOTICE (60 s) and deadline (120 s).
    watcher.tick()
    for _ in range(12):
        clk.advance(5.0)
        watcher.tick()
    assert watcher.state == "NOTICE"
    for _ in range(29):
        clk.advance(2.0)
        watcher.tick()

    with caplog.at_level(logging.WARNING, logger="sigint101.power"):
        clk.advance(2.0)
        assert watcher.tick() == "POWEROFF_FAILED"

    assert attempts == 1
    crit_logs = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(crit_logs) >= 1

    raw = json.loads(notice_path.read_text(encoding="utf-8"))
    assert raw["active"] is True
    assert raw["failed"] is True
    assert set(raw) == {
        "active", "failed", "voltage", "threshold_v",
        "started_mono", "deadline_mono", "updated_mono", "retry_deadline_mono",
    }
    first_retry_deadline = raw["retry_deadline_mono"]
    assert first_retry_deadline == pytest.approx(clk() + POWEROFF_RETRY_S)

    # Mid-retry refresh updates updated_mono without moving retry_deadline_mono.
    clk.advance(10.0)
    assert watcher.tick() == "POWEROFF_FAILED"
    assert attempts == 1
    raw2 = json.loads(notice_path.read_text(encoding="utf-8"))
    assert raw2["retry_deadline_mono"] == pytest.approx(first_retry_deadline)
    assert raw2["updated_mono"] == pytest.approx(clk())
    view = read_power_notice(notice_path, clock=clk)
    assert view["active"] is True and view["failed"] is True and view["retry_in_s"] == 50

    # Stall > 15 s in POWEROFF_FAILED restarts retry countdown rather than retrying silently.
    clk.advance(20.0)
    assert watcher.tick() == "POWEROFF_FAILED"
    assert attempts == 1
    raw3 = json.loads(notice_path.read_text(encoding="utf-8"))
    assert raw3["retry_deadline_mono"] == pytest.approx(clk() + POWEROFF_RETRY_S)

    # Walk cleanly to the new retry deadline; retry triggers once on a fresh good LOW sample.
    for _ in range(29):
        clk.advance(2.0)
        assert watcher.tick() == "POWEROFF_FAILED"
    assert attempts == 1
    clk.advance(2.0)
    assert watcher.tick() == "POWEROFF_FAILED"
    assert attempts == 2

    # Plugging in the charger clears POWEROFF_FAILED cleanly and unlinks notice.json.
    clk.advance(2.0)
    sample[0] = _reading(9.1, current_a=0.4, charging=True)
    assert watcher.tick() == "NORMAL"
    assert not notice_path.exists()

    # Also verify a successful retry transitions to POWERING_OFF.
    should_fail = True
    sample[0] = _reading(9.2)
    watcher.tick()
    for _ in range(12):
        clk.advance(5.0)
        watcher.tick()
    for _ in range(30):
        clk.advance(2.0)
        watcher.tick()
    assert watcher.state == "POWEROFF_FAILED"
    should_fail = False
    for _ in range(30):
        clk.advance(2.0)
        watcher.tick()
    assert watcher.state == "POWERING_OFF"


# --- Test 5: recovery during notice with optional hysteresis --------------------


def test_recovery_during_notice_cancels_countdown_with_optional_hysteresis(tmp_path, caplog):
    # (a) Charging cancels immediately even below threshold.
    sample = [_reading(9.2)]
    watcher, clk, notice_path, poweroffs, _ = _make_watcher(
        tmp_path, threshold_v=9.5, reader=lambda: (CFG_DEFAULT, sample[0])
    )
    watcher.startup()
    watcher.tick()
    for _ in range(12):
        clk.advance(5.0)
        watcher.tick()
    assert watcher.state == "NOTICE"
    assert notice_path.is_file()

    with caplog.at_level(logging.INFO, logger="sigint101.power"):
        clk.advance(2.0)
        sample[0] = _reading(9.1, current_a=0.3, charging=True)
        assert watcher.tick() == "NORMAL"
    assert not notice_path.exists()
    assert poweroffs == []
    assert any("cancel" in r.getMessage().lower() for r in caplog.records)

    # (b) Default recover_v=0.0: discharging at exactly threshold cancels immediately.
    assert parse_recover_v(None) == (0.0, "")
    assert parse_recover_v("") == (0.0, "")
    assert parse_recover_v("0.15") == (pytest.approx(0.15), "")
    assert parse_recover_v("0")[0] == 0.0 and parse_recover_v("0")[1] != ""
    assert parse_recover_v("1.5")[0] == 0.0 and parse_recover_v("1.5")[1] != ""

    sample[0] = _reading(9.2)
    watcher.tick()
    for _ in range(12):
        clk.advance(5.0)
        watcher.tick()
    assert watcher.state == "NOTICE"
    clk.advance(2.0)
    sample[0] = _reading(9.5, current_a=-0.4)
    assert watcher.tick() == "NORMAL"
    assert not notice_path.exists()

    # (c) With recover_v=0.15: 9.55 V discharging stays in HOLDING/NOTICE; 9.65 V cancels.
    sample_h = [_reading(9.2)]
    w_h, clk_h, notice_h, poweroffs_h, _ = _make_watcher(
        tmp_path / "hyst", threshold_v=9.5, recover_v=0.15,
        reader=lambda: (CFG_DEFAULT, sample_h[0]),
    )
    (tmp_path / "hyst").mkdir()
    w_h.startup()
    assert w_h.tick() == "HOLDING"
    clk_h.advance(5.0)
    sample_h[0] = _reading(9.55, current_a=-0.4)  # inside hysteresis band [9.50, 9.65)
    assert w_h.tick() == "HOLDING"
    for _ in range(11):
        clk_h.advance(5.0)
        sample_h[0] = _reading(9.2)
        w_h.tick()
    assert w_h.state == "NOTICE"
    clk_h.advance(2.0)
    sample_h[0] = _reading(9.55, current_a=-0.4)
    assert w_h.tick() == "NOTICE"
    assert notice_h.is_file()

    # 9.65 V discharging cancels immediately.
    clk_h.advance(2.0)
    sample_h[0] = _reading(9.65, current_a=-0.4)
    assert w_h.tick() == "NORMAL"
    assert not notice_h.exists()

    # Charging at 9.10 V still cancels immediately even with recover_v=0.15.
    sample_h[0] = _reading(9.2)
    w_h.tick()
    for _ in range(12):
        clk_h.advance(5.0)
        w_h.tick()
    assert w_h.state == "NOTICE"
    clk_h.advance(2.0)
    sample_h[0] = _reading(9.10, current_a=0.2, charging=True)
    assert w_h.tick() == "NORMAL"
    assert not notice_h.exists()
    assert poweroffs_h == []


# --- Test 6: transient errors, 15 s gap, blind stall, pinned CNVR retry ---------


def test_transient_errors_every_tick_15s_gap_and_pinned_config_cnvr_retry(tmp_path, caplog):
    # (a) Single transient error mid-hold pauses without resetting; next LOW resumes.
    outcomes: list = [(CFG_DEFAULT, _reading(9.2))]

    def pop_reader():
        item = outcomes[0] if len(outcomes) == 1 else outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    watcher, clk, notice_path, poweroffs, sleeps = _make_watcher(
        tmp_path, threshold_v=9.5, reader=pop_reader
    )
    watcher.startup()

    assert watcher.tick() == "HOLDING"
    clk.advance(5.0)
    outcomes[:] = [OSError("Remote I/O error"), (CFG_DEFAULT, _reading(9.2))]
    assert watcher.tick() == "HOLDING"  # paused, not reset
    clk.advance(5.0)
    assert watcher.tick() == "HOLDING"  # resumed; hold_since still t=0
    for _ in range(10):
        clk.advance(5.0)
        watcher.tick()
    assert watcher.state == "NOTICE"  # reached at t=60 from original hold_since

    # (b) Continuous BAD > 15 s resets NOTICE -> NORMAL and unlinks notice.json.
    outcomes[:] = [OSError("I2C bus hang")]
    for _ in range(3):  # t=65, 70, 75 (<= 15 s gap)
        clk.advance(5.0)
        assert watcher.tick() == "NOTICE"
    clk.advance(1.0)  # t=76 (> 15 s since last_good_mono=60)
    assert watcher.tick() == "NORMAL"
    assert not notice_path.exists()
    assert poweroffs == []

    # (c) Blind stall during HOLDING (R2-F4): LOW at t=0, no ticks until t=70, then LOW ->
    # pre-tick gap check breaks old streak and restarts fresh 60 s hold at t=70.
    w_gap, clk_gap, notice_gap, po_gap, _ = _make_watcher(
        tmp_path / "gap", threshold_v=9.5, reader=lambda: (CFG_DEFAULT, _reading(9.2))
    )
    (tmp_path / "gap").mkdir()
    w_gap.startup()
    assert w_gap.tick() == "HOLDING"
    clk_gap.advance(70.0)
    assert w_gap.tick() == "HOLDING"  # restarted at t=70, NOT NOTICE
    assert not notice_gap.exists()
    assert po_gap == []
    clk_gap.advance(5.0)
    assert w_gap.tick() == "HOLDING"

    # (d) BAD on the exact t=60 (hold end) or t=120 (notice deadline) delays transition
    # until the next fresh good LOW sample.
    outcomes_d: list = [(CFG_DEFAULT, _reading(9.2))]

    def reader_d():
        item = outcomes_d[0] if len(outcomes_d) == 1 else outcomes_d.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    w_d, clk_d, notice_d, po_d, _ = _make_watcher(
        tmp_path / "edge", threshold_v=9.5, reader=reader_d
    )
    (tmp_path / "edge").mkdir()
    w_d.startup()
    assert w_d.tick() == "HOLDING"
    for _ in range(11):
        clk_d.advance(5.0)
        assert w_d.tick() == "HOLDING"
    # t=60: BAD delays HOLDING -> NOTICE.
    clk_d.advance(5.0)
    outcomes_d[:] = [ValueError("INA219 measurement overflow"), (CFG_DEFAULT, _reading(9.2))]
    assert w_d.tick() == "HOLDING"
    assert not notice_d.exists()
    # t=62: fresh LOW now enters NOTICE.
    clk_d.advance(2.0)
    assert w_d.tick() == "NOTICE"
    # Walk to deadline - 2 s.
    for _ in range(29):
        clk_d.advance(2.0)
        assert w_d.tick() == "NOTICE"
    # Deadline tick: BAD delays poweroff.
    clk_d.advance(2.0)
    outcomes_d[:] = [OSError("transient"), (CFG_DEFAULT, _reading(9.2))]
    assert w_d.tick() == "NOTICE"
    assert po_d == []
    # Next fresh LOW powers off.
    clk_d.advance(2.0)
    assert w_d.tick() == "POWERING_OFF"
    assert len(po_d) == 1

    # (e) Pinned config-sized CNVR retry timing (F1, R2-N3).
    assert cnvr_retry_wait_s(CFG_DEFAULT) == pytest.approx(0.02, abs=1e-6)
    assert cnvr_retry_wait_s(CFG_128) == pytest.approx(0.17025, abs=1e-3)

    # CNVR miss at 0x3FFF retries once after 0.17025 s and succeeds on the retry.
    cnvr_seq = [
        ConversionNotReadyError("INA219 conversion not ready", config=CFG_128),
        (CFG_128, _reading(9.2)),
    ]
    w_cnvr, clk_cnvr, _, _, sleeps_cnvr = _make_watcher(
        tmp_path / "cnvr",
        threshold_v=9.5,
        reader=lambda: (_raise(cnvr_seq.pop(0)) if isinstance(cnvr_seq[0], Exception)
                        else cnvr_seq.pop(0)),
    )
    (tmp_path / "cnvr").mkdir()
    w_cnvr.startup()
    assert w_cnvr.tick() == "HOLDING"
    assert len(sleeps_cnvr) == 1
    assert sleeps_cnvr[0] == pytest.approx(0.17025, abs=1e-3)

    # Second consecutive CNVR miss returns BAD (no third call).
    calls_2 = 0

    def always_cnvr():
        nonlocal calls_2
        calls_2 += 1
        raise ConversionNotReadyError("INA219 conversion not ready", config=CFG_DEFAULT)

    w_cnvr2, _, _, _, sleeps_cnvr2 = _make_watcher(
        tmp_path / "cnvr2", threshold_v=9.5, reader=always_cnvr
    )
    (tmp_path / "cnvr2").mkdir()
    w_cnvr2.startup()
    assert w_cnvr2.tick() == "NORMAL"
    assert calls_2 == 2
    assert sleeps_cnvr2 == [pytest.approx(0.02, abs=1e-6)]

    # Triggered mode 0x399B: no retry, BAD, logs ERROR once.
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="sigint101.power"):
        w_trig, _, _, _, sleeps_trig = _make_watcher(
            tmp_path / "trig", threshold_v=9.5, reader=lambda: (CFG_TRIGGERED, None)
        )
        (tmp_path / "trig").mkdir()
        w_trig.startup()
        assert w_trig.tick() == "NORMAL"
        assert w_trig.tick() == "NORMAL"
    assert sleeps_trig == []
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def _raise(exc: Exception):
    raise exc


# --- Test 7: frozen notice.json schema, staleness, symlink/FIFO safety ----------


def test_notice_file_schema_contract_staleness_and_rejects_corrupt_symlink_or_fifo(tmp_path):
    clk = _Clock(500.0)
    notice_path = tmp_path / "notice.json"

    # Missing file -> inactive.
    assert read_power_notice(notice_path, clock=clk) == {"active": False}

    # Corrupt JSON -> inactive.
    notice_path.write_text("{not json", encoding="utf-8")
    assert read_power_notice(notice_path, clock=clk) == {"active": False}

    # Oversized (> 4 KiB) -> inactive.
    notice_path.write_text(" " * 5000, encoding="utf-8")
    assert read_power_notice(notice_path, clock=clk) == {"active": False}

    # active: false -> inactive.
    notice_path.write_text(json.dumps({
        "active": False, "failed": False, "voltage": 9.2, "threshold_v": 9.5,
        "started_mono": 440.0, "deadline_mono": 500.0, "updated_mono": 500.0,
    }), encoding="utf-8")
    assert read_power_notice(notice_path, clock=clk) == {"active": False}

    # Symlink -> refused via O_NOFOLLOW.
    real_target = tmp_path / "real.json"
    real_target.write_text(json.dumps({
        "active": True, "failed": False, "voltage": 9.2, "threshold_v": 9.5,
        "started_mono": 480.0, "deadline_mono": 540.0, "updated_mono": 500.0,
    }), encoding="utf-8")
    symlink_path = tmp_path / "link.json"
    symlink_path.symlink_to(real_target)
    assert read_power_notice(symlink_path, clock=clk) == {"active": False}

    # FIFO -> refused without blocking via O_NONBLOCK + S_ISREG (F10).
    fifo_path = tmp_path / "notice.fifo"
    os.mkfifo(fifo_path)
    assert read_power_notice(fifo_path, clock=clk) == {"active": False}

    # Stale (> 15 s old) and future (> 1 s ahead) -> inactive.
    notice_path.write_text(json.dumps({
        "active": True, "failed": False, "voltage": 9.2, "threshold_v": 9.5,
        "started_mono": 420.0, "deadline_mono": 480.0, "updated_mono": 500.0 - NOTICE_STALE_S - 0.5,
    }), encoding="utf-8")
    assert read_power_notice(notice_path, clock=clk) == {"active": False}

    notice_path.write_text(json.dumps({
        "active": True, "failed": False, "voltage": 9.2, "threshold_v": 9.5,
        "started_mono": 490.0, "deadline_mono": 550.0, "updated_mono": 502.0,
    }), encoding="utf-8")
    assert read_power_notice(notice_path, clock=clk) == {"active": False}

    # Fresh NOTICE schema contract (exact keys).
    notice_path.write_text(json.dumps({
        "active": True, "failed": False, "voltage": 9.21, "threshold_v": 9.5,
        "started_mono": 480.0, "deadline_mono": 540.0, "updated_mono": 499.0,
    }), encoding="utf-8")
    assert read_power_notice(notice_path, clock=clk) == {
        "active": True,
        "failed": False,
        "remaining_s": 40,
        "voltage": 9.21,
        "threshold_v": 9.5,
    }

    # Fresh POWEROFF_FAILED schema contract (with retry_deadline_mono).
    notice_path.write_text(json.dumps({
        "active": True, "failed": True, "voltage": 9.18, "threshold_v": 9.5,
        "started_mono": 400.0, "deadline_mono": 460.0, "updated_mono": 499.5,
        "retry_deadline_mono": 547.0,
    }), encoding="utf-8")
    assert read_power_notice(notice_path, clock=clk) == {
        "active": True,
        "failed": True,
        "retry_in_s": 47,
        "voltage": 9.18,
        "threshold_v": 9.5,
    }

    # Writer cleans up notice.json on clean shutdown when not POWERING_OFF.
    watcher, _, w_notice, _, _ = _make_watcher(
        tmp_path / "sig", threshold_v=9.5, reader=lambda: (CFG_DEFAULT, _reading(9.2))
    )
    (tmp_path / "sig").mkdir()
    watcher.startup()
    w_notice.write_text('{"active": true}', encoding="utf-8")
    watcher.shutdown()
    assert not w_notice.exists()


# --- Test 8: poweroff command hygiene, CanPowerOff check, digests, --startup-only ---


def test_poweroff_command_hygiene_timeout_and_startup_can_power_off_check(
    tmp_path, monkeypatch, caplog
):
    recorded: list[tuple] = []

    def fake_run(cmd, **kwargs):
        recorded.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout='s "yes"\n', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_poweroff()
    assert len(recorded) == 1
    cmd, kw = recorded[0]
    assert cmd == ["/usr/bin/systemctl", "poweroff"]
    assert kw["check"] is True
    assert kw["timeout"] == POWEROFF_TIMEOUT_S
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["close_fds"] is True
    assert kw["env"] == MINIMAL_ENV

    # CanPowerOff check: "yes" -> authorized, no error; "no"/"challenge"/error -> ERROR logged.
    recorded.clear()
    ok, detail = check_can_power_off(runner=fake_run)
    assert ok is True and 's "yes"' in detail
    c_cmd, c_kw = recorded[0]
    assert c_cmd == [
        "/usr/bin/busctl", "call",
        "org.freedesktop.login1", "/org/freedesktop/login1",
        "org.freedesktop.login1.Manager", "CanPowerOff",
    ]
    assert c_kw["stdin"] is subprocess.DEVNULL
    assert c_kw["close_fds"] is True
    assert c_kw["timeout"] == 10.0
    assert c_kw["env"] == MINIMAL_ENV

    for reply in ('s "no"\n', 's "challenge"\n'):
        caplog.clear()
        watcher, _, _, _, _ = _make_watcher(
            tmp_path / "auth",
            threshold_v=9.5,
            can_power_off=lambda r=reply: (False, r.strip()),
        )
        (tmp_path / "auth").mkdir(exist_ok=True)
        with caplog.at_level(logging.INFO, logger="sigint101.power"):
            watcher.startup()
        assert any(
            r.levelno == logging.ERROR and "armed but not authorized to power off" in r.getMessage()
            for r in caplog.records
        )

    # --startup-only logs SHA-256 digests of power.py and battery.py + disarmed status when
    # unset, exits 0, and touches no I2C bus.
    bus_touched = False

    def boom(*_a, **_kw):
        nonlocal bus_touched
        bus_touched = True
        raise AssertionError("disarmed --startup-only must not read I2C")

    import sigint101.power as power_mod

    monkeypatch.setattr(battery, "read_battery_once", boom)
    monkeypatch.delenv("SIGINT101_POWER_THRESHOLD_V", raising=False)
    missing_conf = tmp_path / "missing-power.conf"
    monkeypatch.setattr(power_mod, "DEFAULT_CONFIG_PATH", missing_conf)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="sigint101.power"):
        rc = main([
            "--startup-only",
            "--notice-path",
            str(tmp_path / "cli-notice.json"),
        ])
    assert rc == 0
    assert bus_touched is False
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "power.py" in msgs and "battery.py" in msgs
    assert "disarmed (unset)" in msgs

    # Even with a valid single assignment in power.conf, slice 2b's hard-disarm lock keeps the
    # watcher disarmed with zero I2C reads and zero polkit calls.
    monkeypatch.setattr(power_mod, "check_can_power_off", lambda: (True, 's "yes"'))
    valid_conf = tmp_path / "armed-power.conf"
    valid_conf.write_text("SIGINT101_POWER_THRESHOLD_V=9.50\n", encoding="utf-8")
    monkeypatch.setattr(power_mod, "DEFAULT_CONFIG_PATH", valid_conf)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="sigint101.power"):
        rc_locked = main([
            "--startup-only",
            "--notice-path",
            str(tmp_path / "cli-notice.json"),
        ])
    assert rc_locked == 0
    assert bus_touched is False
    locked_msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "disarmed" in locked_msgs and "armed at" not in locked_msgs


# --- Test 9: systemd unit file is non-root and hardened -------------------------


def test_unit_file_is_non_root_and_hardened():
    assert UNIT_FILE.is_file(), f"missing unit file: {UNIT_FILE}"
    directives: dict[str, list[str]] = {}
    for line in UNIT_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        key, value = stripped.split("=", 1)
        directives.setdefault(key.strip(), []).append(value.strip())

    expected_exact = {
        "Type": "exec",
        "User": "sigint101-power",
        "Group": "sigint101-power",
        "SupplementaryGroups": "i2c",
        "ExecStart": "/usr/local/bin/sigint101-power",
        "RuntimeDirectory": "sigint101-power",
        "RuntimeDirectoryMode": "0755",
        "CapabilityBoundingSet": "",
        "NoNewPrivileges": "yes",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "PrivateTmp": "yes",
        "PrivateNetwork": "yes",
        "RestrictAddressFamilies": "AF_UNIX",
        "ProtectKernelTunables": "yes",
        "ProtectKernelModules": "yes",
        "ProtectKernelLogs": "yes",
        "ProtectControlGroups": "yes",
        "RestrictNamespaces": "yes",
        "LockPersonality": "yes",
        "RestrictSUIDSGID": "yes",
        "SystemCallArchitectures": "native",
        "UMask": "0022",
        "DevicePolicy": "closed",
        "DeviceAllow": "char-i2c rw",
        "MemoryDenyWriteExecute": "yes",
        "SystemCallFilter": "@system-service",
    }
    assert directives.get("User") != ["root"]
    assert "EnvironmentFile" not in directives
    for key, val in expected_exact.items():
        assert directives.get(key) == [val], (
            f"{key}: expected [{val!r}], got {directives.get(key)!r}"
        )
    # Sanity-check plausibility constants stay ordered.
    assert 0.0 < PLAUSIBLE_MIN_V < PLAUSIBLE_MAX_V
    assert MAX_GAP_S == 15.0 and sys.version_info >= (3, 11)


def test_notice_write_failure_logs_error_and_does_not_block_poweroff(tmp_path, caplog):
    # A missing or unwritable notice directory must never crash the watcher or prevent a
    # confirmed low battery from reaching systemctl poweroff (N2).
    missing_notice = tmp_path / "no-such-dir" / "notice.json"
    clk = _Clock(2000.0)
    poweroffs: list[float] = []

    watcher = PowerWatcher(
        threshold_v=9.5,
        notice_path=missing_notice,
        reader=lambda: (CFG_DEFAULT, _reading(9.2)),
        clock=clk,
        sleep=lambda _: None,
        poweroff=lambda: poweroffs.append(clk()),
        can_power_off=lambda: (True, 's "yes"'),
    )
    with caplog.at_level(logging.ERROR, logger="sigint101.power"):
        watcher.startup()
        assert watcher.tick() == "HOLDING"
        for _ in range(12):
            clk.advance(5.0)
            watcher.tick()
        assert watcher.state == "NOTICE"
        for _ in range(29):
            clk.advance(2.0)
            assert watcher.tick() == "NOTICE"
        # Notice write fails right before power-off too, yet power-off is still reached.
        clk.advance(2.0)
        assert watcher.tick() == "POWERING_OFF"

    assert len(poweroffs) == 1
    assert any(
        r.levelno == logging.ERROR and "notice" in r.getMessage().lower()
        for r in caplog.records
    )

    # Also confirm POWEROFF_FAILED survives a failing notice write without crashing.
    w_fail = PowerWatcher(
        threshold_v=9.5,
        notice_path=missing_notice,
        reader=lambda: (CFG_DEFAULT, _reading(9.2)),
        clock=clk,
        sleep=lambda _: None,
        poweroff=lambda: _raise(
            subprocess.CalledProcessError(1, ["/usr/bin/systemctl", "poweroff"])
        ),
        can_power_off=lambda: (True, 's "yes"'),
    )
    w_fail.startup()
    w_fail.tick()
    for _ in range(12):
        clk.advance(5.0)
        w_fail.tick()
    for _ in range(30):
        clk.advance(2.0)
        w_fail.tick()
    assert w_fail.state == "POWEROFF_FAILED"
    clk.advance(2.0)
    assert w_fail.tick() == "POWEROFF_FAILED"


# --- Slice 2b: GET /api/power, base.html banner, and learner-side desktop notice ---


def test_api_power_is_strictly_read_only_and_base_html_renders_notice(tmp_path):
    from fastapi.testclient import TestClient

    from sigint101.app import create_app
    from sigint101.hardware import RadioProbe

    clk = _Clock(1000.0)
    notice_path = tmp_path / "notice.json"
    data_dir = tmp_path / "data"
    gauge_calls = 0

    class _CountingGauge:
        reason = "test gauge"

        def read(self):
            nonlocal gauge_calls
            gauge_calls += 1
            return None

    app = create_app(
        pack_dir=REPO / "lesson_packs",
        data_dir=data_dir,
        probe=lambda: RadioProbe(False, "lsusb", "test"),
        battery_gauge=_CountingGauge(),
        power_notice_path=notice_path,
        power_clock=clk,
    )
    with TestClient(app) as client:
        db_path = data_dir / "progress.db"
        events_path = data_dir / "events.jsonl"
        db_before = db_path.read_bytes() if db_path.exists() else None
        events_before = events_path.read_bytes() if events_path.exists() else None

        # Absent notice -> inactive, zero I2C/gauge reads, zero DB/event writes.
        gauge_calls = 0
        r = client.get("/api/power")
        assert r.status_code == 200
        assert r.json() == {"active": False}
        assert gauge_calls == 0

        # Home HTML renders .power-notice hidden and loads power-notice.js.
        home = client.get("/")
        assert home.status_code == 200
        assert "/static/power-notice.js" in home.text
        assert 'data-power-notice' in home.text
        assert 'role="alert"' in home.text
        assert 'aria-live="assertive"' in home.text
        assert "LOW BATTERY · POWERING OFF" not in home.text

        # Active countdown notice -> /api/power serves remaining_s and base.html shows banner.
        notice_path.write_text(json.dumps({
            "active": True,
            "failed": False,
            "voltage": 9.21,
            "threshold_v": 9.5,
            "started_mono": 982.0,
            "deadline_mono": 1042.0,
            "updated_mono": 999.0,
        }), encoding="utf-8")
        gauge_calls = 0
        r2 = client.get("/api/power")
        assert r2.status_code == 200
        assert r2.json() == {
            "active": True,
            "failed": False,
            "remaining_s": 42,
            "voltage": 9.21,
            "threshold_v": 9.5,
        }
        assert gauge_calls == 0
        home2 = client.get("/")
        assert "LOW BATTERY · POWERING OFF IN 42 s" in home2.text
        assert "Pack 9.21 V (limit 9.50 V)" in home2.text
        assert "power-notice--failed" not in home2.text

        # Failed power-off notice -> /api/power serves retry_in_s and base.html marks failed.
        notice_path.write_text(json.dumps({
            "active": True,
            "failed": True,
            "voltage": 9.18,
            "threshold_v": 9.5,
            "started_mono": 900.0,
            "deadline_mono": 960.0,
            "updated_mono": 999.5,
            "retry_deadline_mono": 1047.0,
        }), encoding="utf-8")
        gauge_calls = 0
        r3 = client.get("/api/power")
        assert r3.status_code == 200
        assert r3.json() == {
            "active": True,
            "failed": True,
            "retry_in_s": 47,
            "voltage": 9.18,
            "threshold_v": 9.5,
        }
        assert gauge_calls == 0
        home3 = client.get("/")
        assert "power-notice--failed" in home3.text
        assert "AUTOMATIC POWER-OFF FAILED · RETRYING IN 47 s" in home3.text

        # Stale notice -> inactive.
        clk.advance(NOTICE_STALE_S + 2.0)
        assert client.get("/api/power").json() == {"active": False}

        # Confirm zero DB or event-log mutation across all /api/power and GET / calls.
        assert (db_path.read_bytes() if db_path.exists() else None) == db_before
        assert (events_path.read_bytes() if events_path.exists() else None) == events_before


def test_desktop_notice_reads_contract_updates_in_place_and_clears(tmp_path):
    from sigint101.power_desktop import (
        BusctlNotifier,
        DesktopNoticeMonitor,
        GdbusNotifier,
        format_desktop_notice,
    )
    from sigint101.power_desktop import (
        main as desktop_main,
    )

    clk = _Clock(500.0)
    notice_path = tmp_path / "notice.json"
    shown: list[tuple[str, str, bool]] = []
    clears = 0

    class _FakeNotifier:
        def show(self, summary: str, body: str, *, failed: bool) -> bool:
            shown.append((summary, body, failed))
            return True

        def clear(self) -> None:
            nonlocal clears
            clears += 1

    mon = DesktopNoticeMonitor(notice_path=notice_path, clock=clk, notifier=_FakeNotifier())

    # Absent notice -> INACTIVE, no show/clear calls.
    assert mon.tick() == "INACTIVE"
    assert shown == [] and clears == 0

    # Active countdown -> ACTIVE, shows countdown.
    notice_path.write_text(json.dumps({
        "active": True, "failed": False, "voltage": 9.21, "threshold_v": 9.5,
        "started_mono": 480.0, "deadline_mono": 540.0, "updated_mono": 499.0,
    }), encoding="utf-8")
    assert mon.tick() == "ACTIVE"
    assert len(shown) == 1
    assert shown[-1][0] == "LOW BATTERY · POWERING OFF IN 40 s"
    assert "9.21 V" in shown[-1][1] and shown[-1][2] is False

    # Failed state -> FAILED, shows retry countdown.
    notice_path.write_text(json.dumps({
        "active": True, "failed": True, "voltage": 9.18, "threshold_v": 9.5,
        "started_mono": 400.0, "deadline_mono": 460.0, "updated_mono": 499.5,
        "retry_deadline_mono": 547.0,
    }), encoding="utf-8")
    assert mon.tick() == "FAILED"
    assert "RETRYING IN 47 s" in shown[-1][0] and shown[-1][2] is True

    # Stale notice -> INACTIVE and clears the desktop notification once.
    clk.advance(NOTICE_STALE_S + 1.0)
    assert mon.tick() == "INACTIVE"
    assert clears == 1
    assert mon.tick() == "INACTIVE"
    assert clears == 1

    # Fresh notice then unlink -> shows again, then clears on unlink.
    notice_path.write_text(json.dumps({
        "active": True, "failed": False, "voltage": 9.20, "threshold_v": 9.5,
        "started_mono": clk() - 5.0, "deadline_mono": clk() + 55.0, "updated_mono": clk(),
    }), encoding="utf-8")
    assert mon.tick() == "ACTIVE"
    notice_path.unlink()
    assert mon.tick() == "INACTIVE"
    assert clears == 2

    # Active notice cleared on clean shutdown.
    notice_path.write_text(json.dumps({
        "active": True, "failed": False, "voltage": 9.20, "threshold_v": 9.5,
        "started_mono": clk() - 5.0, "deadline_mono": clk() + 55.0, "updated_mono": clk(),
    }), encoding="utf-8")
    assert mon.tick() == "ACTIVE"
    mon.shutdown()
    assert clears == 3

    # Pure formatter returns None for inactive.
    assert format_desktop_notice({"active": False}) is None

    # GdbusNotifier uses replaces_id and CloseNotification with bounded timeout + DEVNULL.
    gdbus_calls: list[tuple] = []

    def fake_gdbus(cmd, **kw):
        gdbus_calls.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout="(uint32 42,)\n", stderr="")

    gn = GdbusNotifier(gdbus_bin="/usr/bin/gdbus", runner=fake_gdbus)
    assert gn.show("LOW BATTERY · POWERING OFF IN 40 s", "Plug in", failed=False) is True
    assert gdbus_calls[-1][0][0] == "/usr/bin/gdbus"
    assert "org.freedesktop.Notifications.Notify" in gdbus_calls[-1][0]
    assert "0" in gdbus_calls[-1][0]
    assert gdbus_calls[-1][1]["stdin"] is subprocess.DEVNULL
    assert gdbus_calls[-1][1]["close_fds"] is True
    assert gdbus_calls[-1][1]["timeout"] > 0

    assert gn.show("LOW BATTERY · POWERING OFF IN 38 s", "Plug in", failed=False) is True
    assert "42" in gdbus_calls[-1][0]
    gn.clear()
    assert "org.freedesktop.Notifications.CloseNotification" in gdbus_calls[-1][0]
    assert gdbus_calls[-1][0][-1] == "42"

    # BusctlNotifier fallback invokes busctl --user call Notify and CloseNotification with DEVNULL.
    bc_calls: list[tuple] = []

    def fake_bc(cmd, **kw):
        bc_calls.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout="u 17\n", stderr="")

    bc = BusctlNotifier(busctl_bin="/usr/bin/busctl", runner=fake_bc)
    assert bc.show("LOW BATTERY · POWERING OFF IN 40 s", "Plug in", failed=False) is True
    assert bc_calls[-1][0][0] == "/usr/bin/busctl"
    assert "Notify" in bc_calls[-1][0]
    assert bc_calls[-1][1]["stdin"] is subprocess.DEVNULL
    assert bc_calls[-1][1]["close_fds"] is True
    bc.clear()
    assert "CloseNotification" in bc_calls[-1][0]
    assert bc_calls[-1][0][-1] == "17"

    # --once exits 0 when no notice file exists.
    assert desktop_main(["--once", "--notice-path", str(tmp_path / "missing.json")]) == 0


def test_desktop_notice_unit_and_launcher_run_in_learner_session():
    unit_file = REPO / "deploy" / "system" / "sigint101-power-notice.service"
    launcher = REPO / "deploy" / "system" / "sigint101-power-notice"
    assert unit_file.is_file()
    assert launcher.is_file()
    text = unit_file.read_text(encoding="utf-8")
    assert "ExecStart=/usr/local/bin/sigint101-power-notice" in text
    assert "WantedBy=default.target" in text
    assert "User=root" not in text
    assert "sigint101.power_desktop" in launcher.read_text(encoding="utf-8")
    assert "python3 -I" in launcher.read_text(encoding="utf-8")


# --- Round 3 (F1, F3, F4): config-only arming, notice bounds/overflow, close-capable desktop ---


def test_f1_power_conf_is_sole_arming_input_and_rejects_duplicates_through_installed_launcher(
    tmp_path,
):
    """F1: watcher reads power.conf directly (never EnvironmentFile / env vars), refuses duplicate
    threshold assignments (conflicting or identical) with a diagnostic, and stays disarmed."""
    # Unit file must no longer load power.conf via EnvironmentFile.
    unit_text = UNIT_FILE.read_text(encoding="utf-8")
    assert "EnvironmentFile=" not in unit_text

    # Stage the real installed layout in a temporary sysroot.
    sysroot = tmp_path / "sysroot"
    lib_pkg = sysroot / "usr" / "local" / "lib" / "sigint101-power" / "sigint101"
    lib_pkg.mkdir(parents=True)
    for py_name in ("__init__.py", "battery.py", "power.py"):
        (lib_pkg / py_name).write_bytes((REPO / "sigint101" / py_name).read_bytes())
    bin_dir = sysroot / "usr" / "local" / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "sigint101-power"
    launcher.write_bytes((REPO / "deploy" / "system" / "sigint101-power").read_bytes())
    launcher.chmod(0o755)
    notice_path = tmp_path / "notice.json"
    base_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}

    lib_root = sysroot / "usr" / "local" / "lib" / "sigint101-power"
    snippet = (
        "import pathlib, sys; sys.path.insert(0, sys.argv[1]); "
        "import sigint101.power as p; "
        "p.DEFAULT_CONFIG_PATH = pathlib.Path(sys.argv[2]); "
        "sys.exit(p.main(['--startup-only', '--notice-path', sys.argv[3]]))"
    )

    def _run_installed(conf_path: Path, *, extra_env: dict[str, str] | None = None) -> str:
        env = {**base_env, **(extra_env or {})}
        proc = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                snippet,
                str(lib_root),
                str(conf_path),
                str(notice_path),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        return proc.stdout + proc.stderr

    # 1) Absent file stays disarmed.
    out_absent = _run_installed(tmp_path / "missing.conf")
    assert "disarmed" in out_absent and "armed at" not in out_absent

    # 2) Threshold-free file (comments + recover_v only) stays disarmed.
    no_thresh = tmp_path / "no-thresh.conf"
    no_thresh.write_text("# comment\nSIGINT101_POWER_RECOVER_V=0.20\n", encoding="utf-8")
    out_nothresh = _run_installed(no_thresh)
    assert "disarmed" in out_nothresh and "armed at" not in out_nothresh

    # 3) Conflicting duplicate threshold assignments (bad+valid and two valid) refused + disarmed.
    for name, body, expect in (
        (
            "dup-bad-good.conf",
            "SIGINT101_POWER_THRESHOLD_V=abc\nSIGINT101_POWER_THRESHOLD_V=9.50\n",
            "invalid",
        ),
        (
            "dup-conflict.conf",
            "SIGINT101_POWER_THRESHOLD_V=9.20\nSIGINT101_POWER_THRESHOLD_V=9.50\n",
            "duplicate",
        ),
    ):
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        out = _run_installed(p)
        assert "disarmed" in out and "armed at" not in out, out
        assert expect in out.lower(), out

    # 4) Identical duplicate threshold assignments also refused + disarmed.
    dup_same = tmp_path / "dup-same.conf"
    dup_same.write_text(
        "SIGINT101_POWER_THRESHOLD_V=9.50\nSIGINT101_POWER_THRESHOLD_V=9.50\n",
        encoding="utf-8",
    )
    out_same = _run_installed(dup_same)
    assert "disarmed" in out_same and "armed at" not in out_same, out_same
    assert "duplicate" in out_same.lower(), out_same

    # 5) Environment variable with no file NEVER arms.
    out_env = _run_installed(
        tmp_path / "still-absent.conf",
        extra_env={"SIGINT101_POWER_THRESHOLD_V": "9.50"},
    )
    assert "disarmed" in out_env and "armed at" not in out_env, out_env

    # 6) Under slice 2b's hard-disarm lock, a valid single assignment also stays disarmed.
    valid_conf = tmp_path / "valid.conf"
    valid_conf.write_text(
        "# bench calibrated\nSIGINT101_POWER_THRESHOLD_V=9.50\nSIGINT101_POWER_RECOVER_V=0.20\n",
        encoding="utf-8",
    )
    out_valid = _run_installed(valid_conf)
    assert "disarmed" in out_valid and "armed at" not in out_valid, out_valid
    assert "slice 2b" in out_valid, out_valid


def test_f3_malformed_notices_never_crash_or_pass_as_active(tmp_path):
    """F3: unconvertible numbers (351-digit voltage) and out-of-invariant notices (18 V, 30 V
    threshold, deadline before start, bad retry deadline) return inactive in API and desktop."""
    from fastapi.testclient import TestClient

    from sigint101.app import create_app
    from sigint101.hardware import RadioProbe
    from sigint101.power_desktop import DesktopNoticeMonitor

    clk = _Clock(1000.0)
    notice_path = tmp_path / "notice.json"
    app = create_app(
        pack_dir=REPO / "lesson_packs",
        data_dir=tmp_path / "data",
        probe=lambda: RadioProbe(False, "lsusb", "test"),
        power_notice_path=notice_path,
        power_clock=clk,
    )

    class _NoopNotifier:
        def show(self, *_a, **_kw) -> bool:
            raise AssertionError("malformed notice must never show a desktop alert")

        def clear(self) -> None:
            pass

    mon = DesktopNoticeMonitor(notice_path=notice_path, clock=clk, notifier=_NoopNotifier())

    # 1) Codex's 351-digit integer voltage under 4 KiB (previously OverflowError / HTTP 500).
    huge_int_json = (
        '{"active": true, "failed": false, "voltage": '
        + ("9" * 351)
        + ', "threshold_v": 9.5, "started_mono": 980.0, "deadline_mono": 1040.0, '
        '"updated_mono": 999.0}'
    )
    assert len(huge_int_json.encode("utf-8")) < 4096

    # 2) Codex's out-of-invariant notice (18 V, 30 V threshold, deadline before start).
    bogus_invariant_json = json.dumps({
        "active": True,
        "failed": False,
        "voltage": 18.0,
        "threshold_v": 30.0,
        "started_mono": 990.0,
        "deadline_mono": 950.0,
        "updated_mono": 999.0,
    })

    # 3) Additional writer-invariant violations.
    cases = [
        ("huge_voltage_351_digits", huge_int_json),
        ("codex_18v_30v_deadline_before_start", bogus_invariant_json),
        ("implausible_low_voltage", json.dumps({
            "active": True, "failed": False, "voltage": 5.5, "threshold_v": 9.5,
            "started_mono": 980.0, "deadline_mono": 1040.0, "updated_mono": 999.0,
        })),
        ("threshold_at_lower_bound", json.dumps({
            "active": True, "failed": False, "voltage": 9.2, "threshold_v": 8.0,
            "started_mono": 980.0, "deadline_mono": 1040.0, "updated_mono": 999.0,
        })),
        ("threshold_at_upper_bound", json.dumps({
            "active": True, "failed": False, "voltage": 9.2, "threshold_v": 12.6,
            "started_mono": 980.0, "deadline_mono": 1040.0, "updated_mono": 999.0,
        })),
        ("updated_before_started", json.dumps({
            "active": True, "failed": False, "voltage": 9.2, "threshold_v": 9.5,
            "started_mono": 1000.0, "deadline_mono": 1060.0, "updated_mono": 999.0,
        })),
        ("retry_deadline_before_start", json.dumps({
            "active": True, "failed": True, "voltage": 9.2, "threshold_v": 9.5,
            "started_mono": 900.0, "deadline_mono": 960.0, "updated_mono": 999.0,
            "retry_deadline_mono": 850.0,
        })),
        ("retry_deadline_before_notice_deadline", json.dumps({
            "active": True, "failed": True, "voltage": 9.2, "threshold_v": 9.5,
            "started_mono": 900.0, "deadline_mono": 960.0, "updated_mono": 999.0,
            "retry_deadline_mono": 950.0,
        })),
    ]

    with TestClient(app) as client:
        for label, payload in cases:
            notice_path.write_text(payload, encoding="utf-8")
            assert read_power_notice(notice_path, clock=clk) == {"active": False}, label
            resp = client.get("/api/power")
            assert resp.status_code == 200, (label, resp.status_code)
            assert resp.json() == {"active": False}, label
            assert mon.tick() == "INACTIVE", label


def test_f4_desktop_notice_closes_via_close_capable_transport_on_recovery_and_expiry(
    tmp_path, monkeypatch
):
    """F4: desktop warnings use close-capable D-Bus transports (gdbus or busctl), never falling
    back to non-closing notify-send, and close the displayed ID on both recovery and expiry."""
    import sigint101.power_desktop as pd

    # build_notifier() must NEVER pick notify-send when gdbus is absent; it picks busctl or Null.
    monkeypatch.setattr(
        pd.shutil,
        "which",
        lambda name: {
            "gdbus": None,
            "busctl": "/usr/bin/busctl",
            "notify-send": "/usr/bin/notify-send",
        }.get(name),
    )
    picked = pd.build_notifier()
    assert isinstance(picked, pd.BusctlNotifier)

    monkeypatch.setattr(
        pd.shutil,
        "which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    picked_only_ns = pd.build_notifier()
    assert isinstance(picked_only_ns, pd.NullNotifier)

    # Stateful fake org.freedesktop.Notifications daemon driven through BOTH GdbusNotifier and
    # BusctlNotifier to prove recovery, expiry, and shutdown all close the displayed ID.
    for kind in ("gdbus", "busctl"):
        clk = _Clock(500.0)
        notice_path = tmp_path / f"notice-{kind}.json"
        live_notifs: dict[int, tuple[str, str]] = {}
        next_id = 10

        def _fake_daemon(cmd, **kw):
            nonlocal next_id
            assert kw["stdin"] is subprocess.DEVNULL
            assert kw["close_fds"] is True
            assert kw["timeout"] > 0
            if kind == "gdbus":
                method = cmd[cmd.index("--method") + 1]
                if method == "org.freedesktop.Notifications.Notify":
                    replaces = int(cmd[cmd.index(pd.APP_NAME) + 1])
                    nid = replaces if replaces > 0 else next_id
                    if replaces == 0:
                        next_id += 1
                    app_idx = cmd.index(pd.APP_NAME)
                    live_notifs[nid] = (cmd[app_idx + 3], cmd[app_idx + 4])
                    return SimpleNamespace(returncode=0, stdout=f"(uint32 {nid},)\n", stderr="")
                if method == "org.freedesktop.Notifications.CloseNotification":
                    nid = int(cmd[-1])
                    live_notifs.pop(nid, None)
                    return SimpleNamespace(returncode=0, stdout="()\n", stderr="")
            else:
                if "Notify" in cmd:
                    idx = cmd.index("susssasa{sv}i")
                    replaces = int(cmd[idx + 2])
                    nid = replaces if replaces > 0 else next_id
                    if replaces == 0:
                        next_id += 1
                    live_notifs[nid] = (cmd[idx + 4], cmd[idx + 5])
                    return SimpleNamespace(returncode=0, stdout=f"u {nid}\n", stderr="")
                if "CloseNotification" in cmd:
                    nid = int(cmd[-1])
                    live_notifs.pop(nid, None)
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected command: {cmd}")

        notifier = (
            pd.GdbusNotifier(gdbus_bin="/usr/bin/gdbus", runner=_fake_daemon)
            if kind == "gdbus"
            else pd.BusctlNotifier(busctl_bin="/usr/bin/busctl", runner=_fake_daemon)
        )
        mon = pd.DesktopNoticeMonitor(notice_path=notice_path, clock=clk, notifier=notifier)

        # 1) Active countdown -> 1 notification live; failed update replaces same ID in place.
        notice_path.write_text(json.dumps({
            "active": True, "failed": False, "voltage": 9.21, "threshold_v": 9.5,
            "started_mono": 480.0, "deadline_mono": 540.0, "updated_mono": 499.0,
        }), encoding="utf-8")
        assert mon.tick() == "ACTIVE"
        assert len(live_notifs) == 1
        first_id = next(iter(live_notifs))

        notice_path.write_text(json.dumps({
            "active": True, "failed": True, "voltage": 9.18, "threshold_v": 9.5,
            "started_mono": 400.0, "deadline_mono": 460.0, "updated_mono": 499.5,
            "retry_deadline_mono": 547.0,
        }), encoding="utf-8")
        assert mon.tick() == "FAILED"
        assert list(live_notifs.keys()) == [first_id]

        # 2) Recovery (notice removed) closes the notification in the daemon!
        notice_path.unlink()
        assert mon.tick() == "INACTIVE"
        assert live_notifs == {}, f"{kind} left warning open after recovery: {live_notifs}"

        # 3) Expiry (notice goes stale) also closes the notification in the daemon!
        notice_path.write_text(json.dumps({
            "active": True, "failed": True, "voltage": 9.15, "threshold_v": 9.5,
            "started_mono": 400.0, "deadline_mono": 460.0, "updated_mono": clk(),
            "retry_deadline_mono": clk() + 50.0,
        }), encoding="utf-8")
        assert mon.tick() == "FAILED"
        assert len(live_notifs) == 1
        clk.advance(NOTICE_STALE_S + 1.0)
        assert mon.tick() == "INACTIVE"
        assert live_notifs == {}, f"{kind} left warning open after expiry: {live_notifs}"


# --- Round 4 (LOCK, F1a, F1b, F2 server seed, F3 span/update, F4 close retry) ---


def test_r4_lock_f1a_f1b_hard_disarm_canonical_path_and_strict_grammar(tmp_path):
    """LOCK + F1a + F1b: 2b restores 2a's hard-disarm in parse_threshold; production launcher
    binds strictly to /etc/sigint101/power.conf (refusing --config-path); power.conf enforces a
    strict allowlisted grammar (rejecting BOM, unknown keys, quotes, export, key whitespace, and
    duplicates). Every case stays disarmed through the installed entry point with zero shutdowns."""
    # LOCK: parse_threshold never returns an armed threshold for any input in slice 2b.
    for raw in ("9.50", "8.01", "12.59", "10.0"):
        tv, reason = parse_threshold(raw)
        assert tv is None, f"slice 2b lock must keep parse_threshold({raw!r}) disarmed"
        assert reason, f"expected disarmed reason for {raw!r}"

    # Stage the real installed layout in a temporary sysroot.
    sysroot = tmp_path / "sysroot"
    lib_root = sysroot / "usr" / "local" / "lib" / "sigint101-power"
    lib_pkg = lib_root / "sigint101"
    lib_pkg.mkdir(parents=True)
    for py_name in ("__init__.py", "battery.py", "power.py"):
        (lib_pkg / py_name).write_bytes((REPO / "sigint101" / py_name).read_bytes())
    bin_dir = sysroot / "usr" / "local" / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "sigint101-power"
    launcher.write_bytes((REPO / "deploy" / "system" / "sigint101-power").read_bytes())
    launcher.chmod(0o755)
    notice_path = tmp_path / "notice.json"
    base_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}

    # F1a: installed launcher refuses --config-path with a valid alternate file (canonical absent).
    alt_conf = tmp_path / "alt-power.conf"
    alt_conf.write_text("SIGINT101_POWER_THRESHOLD_V=9.50\n", encoding="utf-8")
    proc_alt = subprocess.run(
        [str(launcher), "--config-path", str(alt_conf), "--notice-path", str(notice_path)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc_alt.returncode != 0, (
        "installed launcher must refuse --config-path, got rc=0: "
        f"{proc_alt.stdout}{proc_alt.stderr}"
    )

    # Full-loop probe through the installed lib/sigint101-power package (90 ticks at low 9.1 V,
    # fake shutdown spy, fake gauge spy) with canonical DEFAULT_CONFIG_PATH substituted in test.
    loop_snippet = r"""
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import sigint101.power as p
from sigint101.battery import BatteryReading, INA219Config

p.DEFAULT_CONFIG_PATH = pathlib.Path(sys.argv[2])
notice_path = pathlib.Path(sys.argv[3])

gauge_calls = 0
auth_calls = 0
shutdown_calls = 0
t = [1000.0]

def fake_reader():
    global gauge_calls
    gauge_calls += 1
    return INA219Config(0x399F), BatteryReading(
        voltage=9.10, current_a=-0.6, power_w=5.46, percent=5, charging=False
    )

def fake_auth():
    global auth_calls
    auth_calls += 1
    return True, 's "yes"'

def fake_poweroff():
    global shutdown_calls
    shutdown_calls += 1

def fake_clock():
    return t[0]

def fake_sleep(dt):
    t[0] += dt

# Stub main's watcher loop to run 90 ticks deterministically without touching host hardware.
p.battery.read_battery_once = fake_reader
p.check_can_power_off = fake_auth
p.run_poweroff = fake_poweroff
p.time.monotonic = fake_clock
ticks = [0]
orig_sleep = fake_sleep
def bounded_sleep(dt):
    ticks[0] += 1
    orig_sleep(dt)
    if ticks[0] >= 90:
        import os, signal
        os.kill(os.getpid(), signal.SIGTERM)
p.time.sleep = bounded_sleep

rc = p.main(["--notice-path", str(notice_path)])
print("RESULT:" + json.dumps({
    "rc": rc,
    "ticks": ticks[0],
    "gauge_calls": gauge_calls,
    "auth_calls": auth_calls,
    "shutdown_calls": shutdown_calls,
    "notice_exists": notice_path.exists(),
}))
"""

    def _run_loop(conf_path: Path, *, extra_env: dict[str, str] | None = None) -> tuple[dict, str]:
        env = {**base_env, **(extra_env or {})}
        proc = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                loop_snippet,
                str(lib_root),
                str(conf_path),
                str(notice_path),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        combined = proc.stdout + proc.stderr
        line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
        return json.loads(line[len("RESULT:"):]), combined

    cases: list[tuple[str, bytes | None, dict[str, str] | None, str]] = [
        ("absent_file", None, None, "disarmed"),
        ("threshold_free", b"# comment\n\nSIGINT101_POWER_RECOVER_V=0.20\n", None, "disarmed"),
        (
            "plain_dup_conflict",
            b"SIGINT101_POWER_THRESHOLD_V=9.20\nSIGINT101_POWER_THRESHOLD_V=9.50\n",
            None,
            "duplicate",
        ),
        (
            "plain_dup_identical",
            b"SIGINT101_POWER_THRESHOLD_V=9.50\nSIGINT101_POWER_THRESHOLD_V=9.50\n",
            None,
            "duplicate",
        ),
        (
            "bom_dup_conflict",
            b"\xef\xbb\xbfSIGINT101_POWER_THRESHOLD_V=9.20\nSIGINT101_POWER_THRESHOLD_V=9.50\n",
            None,
            "bom",
        ),
        (
            "bom_dup_identical",
            b"\xef\xbb\xbfSIGINT101_POWER_THRESHOLD_V=9.50\nSIGINT101_POWER_THRESHOLD_V=9.50\n",
            None,
            "bom",
        ),
        ("bom_single", b"\xef\xbb\xbfSIGINT101_POWER_THRESHOLD_V=9.50\n", None, "bom"),
        (
            "unknown_key",
            b"FOO=bar\nSIGINT101_POWER_THRESHOLD_V=9.50\n",
            None,
            "invalid",
        ),
        ("quoted_double", b'SIGINT101_POWER_THRESHOLD_V="9.50"\n', None, "invalid"),
        ("quoted_single", b"SIGINT101_POWER_THRESHOLD_V='9.50'\n", None, "invalid"),
        ("export_prefix", b"export SIGINT101_POWER_THRESHOLD_V=9.50\n", None, "invalid"),
        ("key_leading_space", b" SIGINT101_POWER_THRESHOLD_V=9.50\n", None, "invalid"),
        ("key_trailing_space", b"SIGINT101_POWER_THRESHOLD_V =9.50\n", None, "invalid"),
        ("semicolon_comment", b"; comment\nSIGINT101_POWER_THRESHOLD_V=9.50\n", None, "invalid"),
        ("env_only", None, {"SIGINT101_POWER_THRESHOLD_V": "9.50"}, "disarmed"),
        (
            "valid_single_assignment_locked_in_2b",
            (
                b"# bench calibrated\n"
                b"SIGINT101_POWER_THRESHOLD_V=9.50\n"
                b"SIGINT101_POWER_RECOVER_V=0.20\n"
            ),
            None,
            "disarmed",
        ),
    ]

    for label, content, extra_env, expected_diag in cases:
        conf = tmp_path / f"case-{label}.conf"
        if content is not None:
            conf.write_bytes(content)
        res, output = _run_loop(conf, extra_env=extra_env)
        assert res["rc"] == 0, (label, output)
        assert res["gauge_calls"] == 0, f"{label}: disarmed watcher read I2C"
        assert res["auth_calls"] == 0, f"{label}: disarmed watcher checked auth"
        assert res["shutdown_calls"] == 0, f"{label}: disarmed watcher invoked shutdown"
        assert res["notice_exists"] is False, f"{label}: disarmed watcher wrote notice.json"
        assert "armed at" not in output, (label, output)
        assert "disarmed" in output.lower(), (label, output)
        assert expected_diag in output.lower(), f"{label}: missing {expected_diag!r} in:\n{output}"


def test_r4_f2_base_html_seeds_structured_power_notice_state(tmp_path):
    """F2: base.html embeds structured JSON initial notice state on [data-power-notice] so the
    client controller can retain a server-rendered warning if the first /api/power poll fails."""
    import html as html_mod
    import re

    from fastapi.testclient import TestClient

    from sigint101.app import create_app
    from sigint101.hardware import RadioProbe

    clk = _Clock(1000.0)
    notice_path = tmp_path / "notice.json"
    app = create_app(
        pack_dir=REPO / "lesson_packs",
        data_dir=tmp_path / "data",
        probe=lambda: RadioProbe(False, "lsusb", "test"),
        power_notice_path=notice_path,
        power_clock=clk,
    )

    def _extract_initial(body: str) -> dict:
        m = re.search(r'data-power-initial="([^"]*)"', body)
        assert m, "base.html must emit data-power-initial structured notice state"
        return json.loads(html_mod.unescape(m.group(1)))

    with TestClient(app) as client:
        assert _extract_initial(client.get("/").text) == {"active": False}

        notice_path.write_text(json.dumps({
            "active": True, "failed": False, "voltage": 9.21, "threshold_v": 9.5,
            "started_mono": 982.0, "deadline_mono": 1042.0, "updated_mono": 999.0,
        }), encoding="utf-8")
        assert _extract_initial(client.get("/").text) == {
            "active": True, "failed": False, "remaining_s": 42,
            "voltage": 9.21, "threshold_v": 9.5,
        }

        notice_path.write_text(json.dumps({
            "active": True, "failed": True, "voltage": 9.18, "threshold_v": 9.5,
            "started_mono": 900.0, "deadline_mono": 960.0, "updated_mono": 999.5,
            "retry_deadline_mono": 1047.0,
        }), encoding="utf-8")
        assert _extract_initial(client.get("/").text) == {
            "active": True, "failed": True, "retry_in_s": 47,
            "voltage": 9.18, "threshold_v": 9.5,
        }


def test_r4_f3_rejects_impossible_countdown_span_and_premature_failed_update(tmp_path):
    """F3: read_power_notice rejects notices whose countdown span does not match NOTICE_S (~60 s)
    and failed notices whose updated_mono precedes the original deadline_mono, across both API
    and desktop monitor."""
    from fastapi.testclient import TestClient

    from sigint101.app import create_app
    from sigint101.hardware import RadioProbe
    from sigint101.power_desktop import DesktopNoticeMonitor

    clk = _Clock(1000.0)
    notice_path = tmp_path / "notice.json"
    app = create_app(
        pack_dir=REPO / "lesson_packs",
        data_dir=tmp_path / "data",
        probe=lambda: RadioProbe(False, "lsusb", "test"),
        power_notice_path=notice_path,
        power_clock=clk,
    )

    class _NoopNotifier:
        def show(self, *_a, **_kw) -> bool:
            raise AssertionError("invalid notice must never show a desktop alert")

        def clear(self) -> bool:
            return True

    mon = DesktopNoticeMonitor(notice_path=notice_path, clock=clk, notifier=_NoopNotifier())

    invalid_cases = [
        (
            "1ms_countdown_span",
            {
                "active": True, "failed": False, "voltage": 9.21, "threshold_v": 9.5,
                "started_mono": 999.0, "deadline_mono": 999.001, "updated_mono": 999.0,
            },
        ),
        (
            "short_30s_countdown_span",
            {
                "active": True, "failed": False, "voltage": 9.21, "threshold_v": 9.5,
                "started_mono": 980.0, "deadline_mono": 1010.0, "updated_mono": 999.0,
            },
        ),
        (
            "long_120s_countdown_span",
            {
                "active": True, "failed": False, "voltage": 9.21, "threshold_v": 9.5,
                "started_mono": 900.0, "deadline_mono": 1020.0, "updated_mono": 999.0,
            },
        ),
        (
            "failed_updated_before_deadline",
            {
                "active": True, "failed": True, "voltage": 9.18, "threshold_v": 9.5,
                "started_mono": 960.0, "deadline_mono": 1020.0, "updated_mono": 999.0,
                "retry_deadline_mono": 1040.0,
            },
        ),
    ]

    with TestClient(app) as client:
        for label, payload in invalid_cases:
            notice_path.write_text(json.dumps(payload), encoding="utf-8")
            assert read_power_notice(notice_path, clock=clk) == {"active": False}, label
            resp = client.get("/api/power")
            assert resp.status_code == 200 and resp.json() == {"active": False}, label
            assert mon.tick() == "INACTIVE", label


def test_r4_f4_retains_notification_id_and_retries_pending_close_for_each_transport(tmp_path):
    """F4: a transient nonzero exit or timeout on CloseNotification retains the displayed ID and
    retries on subsequent inactive ticks until close succeeds or the daemon confirms it gone."""
    import sigint101.power_desktop as pd

    for kind in ("gdbus", "busctl"):
        for failure_mode in ("nonzero", "timeout"):
            clk = _Clock(500.0)
            notice_path = tmp_path / f"retry-{kind}-{failure_mode}.json"
            live_notifs: dict[int, tuple[str, str]] = {}
            close_attempts: list[int] = []
            fail_first_close = {"armed": True}
            next_id = 30

            def _fake_daemon(cmd, **kw):
                nonlocal next_id
                assert kw["timeout"] > 0
                is_close = (
                    "org.freedesktop.Notifications.CloseNotification" in cmd
                    if kind == "gdbus"
                    else "CloseNotification" in cmd
                )
                if is_close:
                    nid = int(cmd[-1])
                    close_attempts.append(nid)
                    if fail_first_close["armed"]:
                        fail_first_close["armed"] = False
                        if failure_mode == "timeout":
                            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
                        return SimpleNamespace(
                            returncode=1,
                            stdout="",
                            stderr="Error: org.freedesktop.DBus.Error.NoReply",
                        )
                    live_notifs.pop(nid, None)
                    return SimpleNamespace(
                        returncode=0, stdout="()\n" if kind == "gdbus" else "", stderr=""
                    )
                if kind == "gdbus":
                    replaces = int(cmd[cmd.index(pd.APP_NAME) + 1])
                    nid = replaces if replaces > 0 else next_id
                    if replaces == 0:
                        next_id += 1
                    live_notifs[nid] = (cmd[cmd.index(pd.APP_NAME) + 3], "")
                    return SimpleNamespace(returncode=0, stdout=f"(uint32 {nid},)\n", stderr="")
                idx = cmd.index("susssasa{sv}i")
                replaces = int(cmd[idx + 2])
                nid = replaces if replaces > 0 else next_id
                if replaces == 0:
                    next_id += 1
                live_notifs[nid] = (cmd[idx + 4], "")
                return SimpleNamespace(returncode=0, stdout=f"u {nid}\n", stderr="")

            notifier = (
                pd.GdbusNotifier(gdbus_bin="/usr/bin/gdbus", runner=_fake_daemon)
                if kind == "gdbus"
                else pd.BusctlNotifier(busctl_bin="/usr/bin/busctl", runner=_fake_daemon)
            )
            mon = pd.DesktopNoticeMonitor(notice_path=notice_path, clock=clk, notifier=notifier)

            # 1) Show active countdown -> ID 30 live in daemon.
            notice_path.write_text(json.dumps({
                "active": True, "failed": False, "voltage": 9.21, "threshold_v": 9.5,
                "started_mono": 480.0, "deadline_mono": 540.0, "updated_mono": 499.0,
            }), encoding="utf-8")
            assert mon.tick() == "ACTIVE"
            assert list(live_notifs.keys()) == [30]

            # 2) Notice clears; first close fails -> ID 30 still live on daemon and retained.
            notice_path.unlink()
            assert mon.tick() == "INACTIVE"
            assert close_attempts == [30]
            assert list(live_notifs.keys()) == [30]

            # 3) Next inactive tick retries CloseNotification(30) and closes it cleanly!
            assert mon.tick() == "INACTIVE"
            assert close_attempts == [30, 30], (kind, failure_mode, close_attempts)
            assert live_notifs == {}, f"{kind}/{failure_mode} failed to close on retry"

            # 4) Further inactive ticks stop retrying once closed.
            assert mon.tick() == "INACTIVE"
            assert close_attempts == [30, 30]
