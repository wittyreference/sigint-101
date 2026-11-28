# ABOUTME: Tests Tier B live-find challenges: catalog validation, provenance window evaluation,
# ABOUTME: _heard credit under lock, startup backfill, D16 non-lockout, saved progress, and routes.

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101 import app as console
from sigint101 import events
from sigint101.challenges import (
    CHALLENGES,
    Challenge,
    challenge_for,
    evaluate,
    format_evidence,
    format_freq,
    qualifies,
    status,
    validate_catalog,
)
from sigint101.hardware import RadioProbe
from sigint101.progress import Progress
from sigint101.stamps import STAMP_INSTRUMENTS, mint
from tests.test_operate_routes import _client

RADIO = {"kind": "rtl-sdr", "index": 0, "serial": None}
LORA_RADIO = {"kind": "meshtasticd", "host": "127.0.0.1:4403"}
RX = {"model": "Test sensor", "readings": {"temperature_C": 20.0}}
LORA_RX = {"from_id": "!1234abcd", "snr_db": 5.5, "summary": "ping"}


def _absent():
    return RadioProbe(False, "lsusb", "no RTL2832U on USB bus")


def _event(key="ism", ts=1000.0, **payload):
    base = {
        "instrument": key,
        "tool": "rtl_433",
        "radio": RADIO.copy(),
        "command": "rtl_433 -d 0",
        "freq_hz": 433_920_000,
        "item": {},
    }
    if key == "lora":
        base.update(tool="meshtasticd", radio=LORA_RADIO.copy(), command=None, freq_hz=None)
    elif key == "ais":
        base.update(tool="rtl_ais", command="rtl_ais -n", freq_hz=[161_975_000, 162_025_000])
    base.update(payload)
    return {"kind": "reception", "ts": ts, "payload": base}


def _hear(client, **changes):
    kwargs = {"command": "rtl_433 -d 0", "freq_hz": 433_920_000, "radio": RADIO, **changes}
    client.app.state.decoder.on_reception(RX, **kwargs)


def _hear_lora(client, **changes):
    kwargs = {"command": None, "freq_hz": None, "radio": LORA_RADIO, **changes}
    client.app.state.lora.on_reception(LORA_RX, **kwargs)


# --- Catalog ------------------------------------------------------------------------------------


def test_catalog_has_exact_slice_1_units_and_instruments():
    assert [c.unit_id for c in CHALLENGES] == [4, 5, 9]

    u4 = challenge_for(4)
    assert u4 is not None
    assert u4.allowed == ("ism", "rds", "lora")
    assert u4.window_s == 900
    assert u4.prompt == "Log a live ISM sensor, RDS station or LoRa packet within 15 minutes."

    u5 = challenge_for(5)
    assert u5 is not None
    assert u5.allowed == ("adsb", "aprs", "pager", "acars", "ais", "dmr")
    assert u5.window_s == 900
    assert u5.prompt == (
        "Log a live ADS-B, APRS, pager, ACARS, AIS or DMR/P25 reception within 15 minutes."
    )

    u9 = challenge_for(9)
    assert u9 is not None
    assert u9.allowed == ("lora",)
    assert u9.window_s == 900
    assert u9.prompt == (
        "Log a live LoRa packet from another Meshtastic node within 15 minutes."
    )

    for unit_id in (1, 2, 3, 6, 7, 8, 10):
        assert challenge_for(unit_id) is None

    for ch in CHALLENGES:
        assert all(key in STAMP_INSTRUMENTS for key in ch.allowed)


@pytest.mark.parametrize(
    "bad_row",
    [
        Challenge(99, ("ism",), 900, "Unknown unit"),
        Challenge(4, ("waterfall",), 900, "Not a stamp instrument"),
        Challenge(4, ("bogus",), 900, "Not an instrument at all"),
        Challenge(8, ("apt",), 900, "APT is forbidden in slice 1"),
        Challenge(7, ("flock",), 900, "Flock is forbidden"),
        Challenge(7, ("rayhunter",), 900, "Rayhunter is forbidden"),
        Challenge(4, ("ism",), 0, "Zero window"),
        Challenge(4, ("ism",), -60, "Negative window"),
    ],
    ids=[
        "unknown-unit",
        "not-stamp-instrument",
        "unknown-instrument",
        "forbidden-apt",
        "forbidden-flock",
        "forbidden-rayhunter",
        "zero-window",
        "negative-window",
    ],
)
def test_catalog_import_validation_rejects_bad_rows(bad_row):
    with pytest.raises(ValueError):
        validate_catalog((bad_row,))


# --- evaluate & qualifies -----------------------------------------------------------------------


def test_evaluate_window_boundaries_are_inclusive():
    ch = challenge_for(4)
    attempt = {"unit_id": 4, "started_at": 1000.0, "window_s": 900}

    assert evaluate(ch, attempt, _event("ism", ts=1000.0)) is not None
    assert evaluate(ch, attempt, _event("ism", ts=1450.0)) is not None
    assert evaluate(ch, attempt, _event("ism", ts=1900.0)) is not None

    assert evaluate(ch, attempt, _event("ism", ts=999.999)) is None
    assert evaluate(ch, attempt, _event("ism", ts=1900.001)) is None

    # Wrong instrument for Unit 4
    assert evaluate(ch, attempt, _event("adsb", ts=1200.0)) is None


@pytest.mark.parametrize(
    "bad_event",
    [
        {**_event("ism", ts=1200.0), "kind": "replay"},
        {**_event("ism", ts=1200.0), "replay": True},
        {**_event("ism", ts=1200.0), "sample": True},
        {**_event("ism", ts=1200.0), "source": "replay"},
        {**_event("ism", ts=1200.0), "source": "sample"},
        _event("ism", ts=1200.0, replay=True),
        _event("ism", ts=1200.0, sample=True),
        _event("ism", ts=1200.0, source="replay"),
        _event("ism", ts=1200.0, source="sample"),
        _event("ism", ts=1200.0, radio=None),
        _event("ism", ts=1200.0, command=None),
        _event("ism", ts=1200.0, command="   "),
        _event("ism", ts=1200.0, freq_hz=None),
        _event("ism", ts=1200.0, freq_hz=0),
        _event("ism", ts=1200.0, tool="  "),
        _event("flock", ts=1200.0),
        _event("rayhunter", ts=1200.0),
    ],
    ids=[
        "kind-replay",
        "event-replay-true",
        "event-sample-true",
        "event-source-replay",
        "event-source-sample",
        "payload-replay-true",
        "payload-sample-true",
        "payload-source-replay",
        "payload-source-sample",
        "radio-missing",
        "command-missing",
        "command-blank",
        "freq-missing",
        "freq-zero",
        "tool-blank",
        "flock",
        "rayhunter",
    ],
)
def test_evaluate_rejects_replays_samples_incomplete_provenance_and_heuristics(bad_event):
    ch = challenge_for(4)
    attempt = {"unit_id": 4, "started_at": 1000.0, "window_s": 900}
    assert evaluate(ch, attempt, bad_event) is None


# --- status -------------------------------------------------------------------------------------


def test_status_all_four_states_boundaries_and_clock_moved():
    ch = challenge_for(4)
    attempt = {"unit_id": 4, "started_at": 1000.0, "window_s": 900}
    pass_row = {
        "unit_id": 4,
        "started_at": 1000.0,
        "passed_at": 1100.0,
        "instrument": "ism",
        "provenance": {"tool": "rtl_433", "freq_hz": 433_920_000},
        "digest": "ab" * 32,
    }

    s_idle = status(ch, None, None, now=1000.0)
    assert s_idle["state"] == "idle"
    assert s_idle["remaining_s"] == 0
    assert s_idle["reason"] is None

    s_start = status(ch, attempt, None, now=1000.0)
    assert s_start["state"] == "active"
    assert s_start["remaining_s"] == 900
    assert s_start["reason"] is None

    s_end = status(ch, attempt, None, now=1900.0)
    assert s_end["state"] == "active"
    assert s_end["remaining_s"] == 0
    assert s_end["reason"] is None

    s_expired = status(ch, attempt, None, now=1900.001)
    assert s_expired["state"] == "expired"
    assert s_expired["remaining_s"] == 0
    assert s_expired["reason"] is None

    s_clock = status(ch, attempt, None, now=999.0)
    assert s_clock["state"] == "expired"
    assert s_clock["remaining_s"] == 0
    assert s_clock["reason"] == "clock_moved"

    s_passed = status(ch, attempt, pass_row, now=1500.0)
    assert s_passed["state"] == "passed"
    assert s_passed["remaining_s"] == 0
    assert s_passed["reason"] is None

    # Restart after an earlier pass: latest_pass has old started_at, so new attempt is active.
    attempt2 = {"unit_id": 4, "started_at": 2000.0, "window_s": 900}
    s_restarted = status(ch, attempt2, pass_row, now=2100.0)
    assert s_restarted["state"] == "active"
    assert s_restarted["remaining_s"] == 800


# --- Through _heard, backfill, durability, and failure recovery ---------------------------------


def _challenge_client(tmp_path, now_box, *, pack_dir=None, **factories):
    kwargs = {"data_dir": tmp_path, "probe": _absent, "clock": lambda: now_box[0], **factories}
    if pack_dir is not None:
        kwargs["pack_dir"] = pack_dir
    else:
        kwargs["pack_dir"] = Path(__file__).resolve().parent / "fixtures" / "lesson_packs"
    return TestClient(console.create_app(**kwargs))


def _patch_log_clock(monkeypatch, now_box):
    real_log = events.log_event
    monkeypatch.setattr(
        console,
        "log_event",
        lambda *args, **kwargs: real_log(*args, **kwargs, clock=lambda: now_box[0]),
    )


def test_heard_credits_challenge_respects_window_restart_multi_unit_and_stamp_retry(
    tmp_path, monkeypatch
):
    now = [1000.0]
    _patch_log_clock(monkeypatch, now)

    with _challenge_client(tmp_path, now) as client:
        # Reception logged BEFORE start must NOT pass Unit 4 when started later.
        _hear(client)
        now[0] = 1010.0
        started = client.post("/api/challenges/4/start")
        assert started.status_code == 200
        body = started.json()
        assert body["state"] == "active"
        assert body["started_at"] == 1010.0
        assert body["remaining_s"] == 900
        assert body["passes"] == 0
        assert body["latest_pass"] is None

        # Reception AFTER the window does NOT pass.
        now[0] = 1910.5
        _hear(client)
        after = client.get("/api/challenges/4").json()
        assert after["state"] == "expired"
        assert after["passes"] == 0

        # Restart after expiry reads active, then an in-window reception passes with evidence.
        now[0] = 2000.0
        restarted = client.post("/api/challenges/4/start").json()
        assert restarted["state"] == "active"
        now[0] = 2050.0
        _hear(client)
        passed = client.get("/api/challenges/4").json()
        assert passed["state"] == "passed"
        assert passed["passes"] == 1
        assert passed["latest_pass"]["instrument"] == "ism"
        assert passed["latest_pass"]["tool"] == "rtl_433"
        assert passed["latest_pass"]["freq_hz"] == 433_920_000
        assert passed["latest_pass"]["started_at"] == 2000.0
        assert passed["latest_pass"]["passed_at"] == 2050.0
        assert passed["latest_pass"]["digest"]

        # challenge_start and challenge_pass events were logged.
        evts = events.read_events(tmp_path / "events.jsonl")
        assert any(e["kind"] == "challenge_start" and e["payload"]["unit"] == 4 for e in evts)
        pass_evts = [e for e in evts if e["kind"] == "challenge_pass"]
        assert len(pass_evts) == 1
        assert pass_evts[0]["payload"]["unit"] == 4
        assert pass_evts[0]["payload"]["instrument"] == "ism"
        assert pass_evts[0]["payload"]["event_id"]

        # Restart after a pass keeps passes & latest_pass and reads active.
        now[0] = 2100.0
        again = client.post("/api/challenges/4/start").json()
        assert again["state"] == "active"
        assert again["passes"] == 1
        assert again["latest_pass"]["passed_at"] == 2050.0

        # One LoRa reception passes Units 4 and 9 together.
        client.post("/api/challenges/9/start")
        now[0] = 2120.0
        _hear_lora(client)
        u4 = client.get("/api/challenges/4").json()
        u9 = client.get("/api/challenges/9").json()
        assert u4["state"] == "passed" and u4["passes"] == 2
        assert u4["latest_pass"]["instrument"] == "lora"
        assert u9["state"] == "passed" and u9["passes"] == 1
        assert u9["latest_pass"]["instrument"] == "lora"

        # A reception during a stamp retry backoff STILL credits its challenge immediately.
        now[0] = 3000.0
        client.post("/api/challenges/4/start")
        mono_now = [500.0]
        monkeypatch.setattr(console, "monotonic", lambda: mono_now[0])
        client.app.state.stamp_retry_at = 600.0
        now[0] = 3010.0
        _hear(client)
        during_retry = client.get("/api/challenges/4").json()
        assert during_retry["state"] == "passed"
        assert during_retry["passes"] == 3


def test_credit_failure_preserves_reception_and_stamp_and_recovers_on_startup_backfill(
    tmp_path, monkeypatch
):
    now = [1000.0]
    _patch_log_clock(monkeypatch, now)

    with _challenge_client(tmp_path, now) as client:
        client.post("/api/challenges/4/start")

        def fail_credit(*_args, **_kwargs):
            raise sqlite3.OperationalError("simulated challenge table lock")

        monkeypatch.setattr(client.app.state.progress, "credit_challenges", fail_credit)
        now[0] = 1020.0
        _hear(client)

        # Reception was still logged, stamp was still counted, and challenge error is surfaced.
        receptions = client.get("/api/receptions?instrument=ism").json()["receptions"]
        assert len(receptions) == 1
        ism_stamp = next(
            row for row in client.get("/api/stamps").json()["stamps"]
            if row["instrument"] == "ism"
        )
        assert ism_stamp["count"] == 1
        state = client.get("/api/challenges/4").json()
        assert state["state"] == "active"
        assert state["passes"] == 0
        assert state["error"] and "simulated challenge table lock" in state["error"]

    # Next startup's backfill recovers the missed pass from the retained event log.
    with _challenge_client(tmp_path, now) as client:
        recovered = client.get("/api/challenges/4").json()
        assert recovered["state"] == "passed"
        assert recovered["passes"] == 1
        assert recovered["latest_pass"]["instrument"] == "ism"
        assert recovered["error"] is None


def test_challenge_pass_survives_restart_and_log_rotation(tmp_path, monkeypatch):
    now = [1000.0]
    _patch_log_clock(monkeypatch, now)

    with _challenge_client(tmp_path, now) as client:
        client.post("/api/challenges/4/start")
        now[0] = 1030.0
        _hear(client)
        first = client.get("/api/challenges/4").json()
        assert first["state"] == "passed" and first["passes"] == 1

        # Rotate all reception lines out of the retained log.
        for _ in range(events.KEEP_ROTATED + 2):
            events.log_event(tmp_path / "events.jsonl", "tick", {}, max_bytes=1)
        assert all(
            e["kind"] != "reception" for e in events.read_events(tmp_path / "events.jsonl")
        )

    with _challenge_client(tmp_path, now) as client:
        reopened = client.get("/api/challenges/4").json()
        assert reopened["state"] == "passed"
        assert reopened["passes"] == 1
        assert reopened["latest_pass"] == first["latest_pass"]


# --- D16 non-lockout ----------------------------------------------------------------------------


def test_d16_active_and_expired_challenges_never_gate_lessons_rack_or_completion(
    tmp_path, monkeypatch
):
    now = [1000.0]
    _patch_log_clock(monkeypatch, now)

    with _challenge_client(tmp_path, now, pack_dir=console.PACK_DIR) as client:
        baseline_unit = client.get("/unit/4").status_code
        baseline_lesson = client.get("/lesson/10").status_code
        baseline_operate = client.get("/operate/ism").status_code
        baseline_logbook = client.get("/logbook").status_code
        assert (baseline_unit, baseline_lesson, baseline_operate, baseline_logbook) == (
            200, 200, 200, 200
        )

        # Active attempt: every route answers identically.
        assert client.post("/api/challenges/4/start").json()["state"] == "active"
        for path in ("/unit/4", "/lesson/10", "/operate/ism", "/logbook"):
            assert client.get(path).status_code == 200

        # Expired attempt: every route still answers 200, and completing lesson 10 succeeds.
        now[0] = 2500.0
        assert client.get("/api/challenges/4").json()["state"] == "expired"
        for path in ("/unit/4", "/lesson/10", "/operate/ism", "/logbook"):
            assert client.get(path).status_code == 200

        pred = client.post(
            "/api/predict?lesson_id=10",
            data={"prediction": "fsk"},
            follow_redirects=False,
        )
        assert pred.status_code == 303
        comp = client.post("/api/complete?lesson_id=10")
        assert comp.status_code == 200
        assert comp.json()["completed"] is True


# --- Saved progress migration safety ------------------------------------------------------------


def test_existing_progress_db_without_challenge_tables_reopens_intact(tmp_path):
    db_path = tmp_path / "progress.db"
    prog = Progress(db_path)
    prog.mark_opened(1)
    prog.mark_completed(1)
    prog.record_prediction(1, "a", correct=True)
    prog.record_stage(1, "explain", 4)
    prog.record_place("rack", "ism")
    prog.record_sort_answer(5, "aircraft-am", "legal", correct=True)
    stamp = mint(_event("ism", ts=123.25))
    assert stamp is not None
    prog.record_stamps((stamp,))
    prog.close()

    # Drop the two new tables to simulate a saved progress.db from main.
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS challenges")
    conn.execute("DROP TABLE IF EXISTS challenge_passes")
    conn.commit()
    conn.close()

    reopened = Progress(db_path)
    try:
        assert reopened.get(1)["completed"] == 1
        assert reopened.get_prediction(1) == "a"
        assert reopened.get_stage(1)["stage"] == "explain"
        assert reopened.get_place("rack") == "ism"
        assert reopened.get_sort_answers(5)["aircraft-am"]["choice"] == "legal"
        assert len(reopened.stamps()) == 1
        assert reopened.stamps()[0]["instrument"] == "ism"

        # Both additive tables now exist and work.
        assert reopened.challenge(4) == {"attempt": None, "latest_pass": None, "passes": 0}
        assert reopened.start_challenge(4, 1000.0, 900) is True
        assert reopened.challenge(4)["attempt"]["started_at"] == 1000.0
    finally:
        reopened.close()


# --- Routes & Back navigation -------------------------------------------------------------------


def test_challenge_routes_unit_tiles_rack_back_and_safe_back_refusals(tmp_path):
    with _client(tmp_path, pack_dir=console.PACK_DIR) as client:
        assert client.get("/unit/1/challenge").status_code == 404
        assert client.get("/api/challenges/1").status_code == 404
        assert client.post("/api/challenges/99/start").status_code == 404

        u4_html = client.get("/unit/4").text
        assert '/unit/4/challenge' in u4_html
        assert "Live find" in u4_html
        assert "FIND" in u4_html

        u1_html = client.get("/unit/1").text
        assert "/unit/1/challenge" not in u1_html
        assert "Live find" not in u1_html

        ch4_html = client.get("/unit/4/challenge").text
        assert 'href="/unit/4"' in ch4_html
        assert 'href="/operate/ism?back=/unit/4/challenge"' in ch4_html
        assert 'href="/operate/rds?back=/unit/4/challenge"' in ch4_html
        assert 'href="/operate/lora?back=/unit/4/challenge"' in ch4_html

        # Unit 5 uses the rack picker's POCSAG label for pager.
        ch5_html = client.get("/unit/5/challenge").text
        assert 'href="/operate/pager?back=/unit/5/challenge"' in ch5_html
        assert "POCSAG" in ch5_html

        # /operate/ism?back=/unit/4/challenge puts Back there labelled "Live find".
        rack_from_find = client.get("/operate/ism?back=/unit/4/challenge").text
        assert 'href="/unit/4/challenge"' in rack_from_find
        assert "Live find" in rack_from_find

        # Default /operate/ism keeps Back to Home.
        rack_default = client.get("/operate/ism").text
        assert 'href="/"' in rack_default

        # _safe_back refuses protocol-relative, extra segments, and external URLs.
        for bad in ("//x", "/unit/4/challenge/x", "http://x"):
            html = client.get("/operate/ism", params={"back": bad}).text
            assert f'href="{bad}"' not in html
            assert 'href="/"' in html

        # Pure qualifies helper sanity check
        assert qualifies(
            challenge_for(4),
            {"unit_id": 4, "started_at": 10.0, "window_s": 60},
            {"instrument": "ism", "first_ts": 70.0},
        )


def test_evidence_and_frequency_formatting_cover_all_display_states():
    assert format_freq(433_920_000) == "433.92 MHz"
    assert format_freq([161_975_000, 162_025_000]) == "161.975 / 162.025 MHz"
    assert format_freq(None) is None

    passed = {
        "state": "passed",
        "reason": None,
        "error": None,
        "latest_pass": {
            "instrument": "ism",
            "tool": "rtl_433",
            "freq_hz": 433_920_000,
            "passed_at": 1764338531.0,
        },
    }
    assert format_evidence(passed) == "Found: ISM · rtl_433 · 433.92 MHz · 2025-11-28 14:02:11 UTC"

    found_before = {**passed, "state": "active"}
    assert format_evidence(found_before).startswith("Found before: ISM · rtl_433 · 433.92 MHz")

    lora_pass = {
        "state": "passed",
        "reason": None,
        "error": None,
        "latest_pass": {
            "instrument": "lora",
            "tool": "meshtasticd",
            "freq_hz": None,
            "passed_at": 1764338531.0,
        },
    }
    assert format_evidence(lora_pass) == "Found: LoRa · meshtasticd · 2025-11-28 14:02:11 UTC"

    clock_moved = {"state": "expired", "reason": "clock_moved", "latest_pass": None, "error": None}
    assert format_evidence(clock_moved) == "The clock changed. Start again."

    with_error = {**passed, "error": "a live find pass could not be saved: disk full"}
    assert format_evidence(with_error) == "a live find pass could not be saved: disk full"

    idle = {"state": "idle", "reason": None, "latest_pass": None, "error": None}
    assert format_evidence(idle) == ""
