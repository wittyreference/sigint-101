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
    LOCKED_2A_REASON,
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


# --- Test 1: disarmed threshold handling (including 2a hard-disarm lock) --------


@pytest.mark.parametrize("raw,expected_reason", [
    (None, "unset"),
    ("", "unset"),
    ("   ", "unset"),
    ("abc", LOCKED_2A_REASON),
    ("nan", LOCKED_2A_REASON),
    ("inf", LOCKED_2A_REASON),
    ("-inf", LOCKED_2A_REASON),
    ("8.0", LOCKED_2A_REASON),
    ("7.5", LOCKED_2A_REASON),
    ("12.6", LOCKED_2A_REASON),
    ("96", LOCKED_2A_REASON),
    ("9.50", LOCKED_2A_REASON),
])
def test_unset_invalid_or_2a_locked_threshold_stays_disarmed_logs_once_and_makes_zero_bus_calls(
    tmp_path, caplog, raw, expected_reason
):
    threshold_v, reason = parse_threshold(raw)
    assert threshold_v is None
    assert reason == expected_reason

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
    assert expected_reason in disarmed_logs[0]


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

    # --startup-only logs SHA-256 digests of power.py and battery.py + disarmed status, exits 0,
    # and touches no I2C bus.
    bus_touched = False

    def boom(*_a, **_kw):
        nonlocal bus_touched
        bus_touched = True
        raise AssertionError("disarmed --startup-only must not read I2C")

    monkeypatch.setattr(battery, "read_battery_once", boom)
    monkeypatch.setenv("SIGINT101_POWER_THRESHOLD_V", "9.50")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="sigint101.power"):
        rc = main(["--startup-only", "--notice-path", str(tmp_path / "cli-notice.json")])
    assert rc == 0
    assert bus_touched is False
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "power.py" in msgs and "battery.py" in msgs
    assert LOCKED_2A_REASON in msgs


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
        "EnvironmentFile": "-/etc/sigint101/power.conf",
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
