# ABOUTME: Tests Tier B live-find challenges: catalog validation, provenance window evaluation,
# ABOUTME: _heard credit under lock, startup backfill, D16 non-lockout, saved progress, and routes.

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101 import app as console
from sigint101 import events
from sigint101 import storage as storage_mod
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
from sigint101.migrations import migrate_store_copy
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
        assert reopened.challenge(4) == {
            "attempt": None, "attempt_pass": None, "latest_pass": None, "passes": 0,
        }
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


# --- F1: managed mode migrates, starts, credits, and fsyncs live-find events -------------------


def test_managed_mode_migrated_store_runs_live_find_and_syncs_log_writes(tmp_path, monkeypatch):
    now = [1000.0]
    _patch_log_clock(monkeypatch, now)
    sync_calls: list[tuple[str, str]] = []
    real_handle = storage_mod.fsync_handle
    real_dir = storage_mod.fsync_dir

    def spy_handle(handle):
        sync_calls.append(("file", Path(handle.name).name))
        real_handle(handle)

    def spy_dir(path):
        sync_calls.append(("dir", Path(path).name))
        real_dir(path)

    monkeypatch.setattr(events, "fsync_handle", spy_handle)
    monkeypatch.setattr(events, "fsync_dir", spy_dir)

    gen = tmp_path / "managed_gen"
    gen.mkdir()
    migrate_store_copy(
        tmp_path / "missing_p.db", gen / "progress.db",
        store="progress", allow_missing_source=True, scratch_dir=tmp_path / "scratch_p",
    )
    migrate_store_copy(
        tmp_path / "missing_e.db", gen / "exam.db",
        store="exam", allow_missing_source=True, scratch_dir=tmp_path / "scratch_e",
    )

    with TestClient(console.create_app(
        pack_dir=Path(__file__).resolve().parent / "fixtures" / "lesson_packs",
        data_dir=gen, probe=_absent, clock=lambda: now[0], managed=True,
    )) as client:
        sync_calls.clear()
        started = client.post("/api/challenges/4/start")
        assert started.status_code == 200, started.text
        body = started.json()
        assert body["state"] == "active"
        assert body["error"] is None
        assert ("file", "events.jsonl") in sync_calls
        assert ("dir", "managed_gen") in sync_calls

        sync_calls.clear()
        now[0] = 1020.0
        _hear(client)
        passed = client.get("/api/challenges/4").json()
        assert passed["state"] == "passed"
        assert passed["passes"] == 1
        assert passed["error"] is None
        # Both the reception line and the challenge_pass line were fsynced in managed mode.
        assert sync_calls.count(("file", "events.jsonl")) >= 2


# --- F2: backfill only credits receptions logged after the attempt's challenge_start -----------


def test_backfill_never_credits_pre_start_reception_after_clock_step_restart_or_rotation(
    tmp_path, monkeypatch
):
    now = [2000.0]
    _patch_log_clock(monkeypatch, now)

    # Probe P1 (restart): reception heard at ts=2000 BEFORE Start; clock steps back to 1500;
    # learner starts Unit 4 (window [1500, 2400], which contains 2000!). Startup backfill on
    # restart MUST NOT credit the pre-Start reception.
    with _challenge_client(tmp_path, now) as client:
        _hear(client)
        now[0] = 1500.0
        assert client.post("/api/challenges/4/start").status_code == 200
        assert client.get("/api/challenges/4").json()["state"] == "active"

    with _challenge_client(tmp_path, now) as client:
        after_restart = client.get("/api/challenges/4").json()
        assert after_restart["state"] == "active"
        assert after_restart["passes"] == 0
        assert after_restart["latest_pass"] is None

        # A real post-Start reception passes Unit 4 at ts=1510.
        now[0] = 1510.0
        _hear(client)
        u4 = client.get("/api/challenges/4").json()
        assert u4["state"] == "passed" and u4["latest_pass"]["passed_at"] == 1510.0

        # Probe P1b (rotation in same run): Unit 9 hears LoRa at ts=2000 before Start, clock
        # steps back to 1500, Unit 9 starts, log rotates so next reception triggers _backfill_stamps
        # under reception_lock. Unit 9 MUST NOT pass from the pre-Start LoRa reception.
        now[0] = 2000.0
        _hear_lora(client)
        now[0] = 1500.0
        assert client.post("/api/challenges/9/start").status_code == 200

        # Force rotation on events.jsonl so the next minted reception triggers _backfill_stamps.
        events.log_event(tmp_path / "events.jsonl", "tick", {}, max_bytes=1, clock=lambda: 1505.0)
        now[0] = 1520.0
        _hear(client)  # ISM reception triggers rotation backfill; must not credit pre-Start LoRa.
        u9 = client.get("/api/challenges/9").json()
        assert u9["state"] == "active" and u9["passes"] == 0


# --- F3: current attempt's pass stays passed even after a backward clock step ------------------


def test_passed_state_keys_on_current_attempt_pass_after_backward_clock_step(
    tmp_path, monkeypatch
):
    now = [5000.0]
    _patch_log_clock(monkeypatch, now)

    # Probe P2: Attempt A starts at 5000, passes at 5100. Clock steps back to 3000; learner
    # taps Try again (Attempt B starts at 3000) and a real reception passes Attempt B at 3050.
    with _challenge_client(tmp_path, now) as client:
        assert client.post("/api/challenges/4/start").status_code == 200
        now[0] = 5100.0
        _hear(client)
        a = client.get("/api/challenges/4").json()
        assert a["state"] == "passed" and a["passes"] == 1

        now[0] = 3000.0
        assert client.post("/api/challenges/4/start").json()["state"] == "active"
        now[0] = 3050.0
        _hear(client)

        b = client.get("/api/challenges/4").json()
        assert b["state"] == "passed"
        assert b["passes"] == 2
        assert b["latest_pass"]["started_at"] == 3000.0
        assert b["latest_pass"]["passed_at"] == 3050.0

        # Even after Attempt B's window elapses, it remains passed (never TIME UP).
        now[0] = 4500.0
        b_later = client.get("/api/challenges/4").json()
        assert b_later["state"] == "passed"
        assert b_later["latest_pass"]["passed_at"] == 3050.0


# --- F6: Start returns JSON error on DB/log failure and logs challenge_start only when saved ---


def test_start_handles_db_error_and_closed_store_without_logging_challenge_start(
    tmp_path, monkeypatch
):
    now = [1000.0]
    _patch_log_clock(monkeypatch, now)

    with _challenge_client(tmp_path, now) as client:
        def boom(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(client.app.state.progress, "start_challenge", boom)
        res = client.post("/api/challenges/4/start")
        assert res.status_code == 500
        body = res.json()
        assert body["error"] and "database is locked" in body["error"]
        assert body["state"] == "idle"
        assert [
            e for e in events.read_events(tmp_path / "events.jsonl")
            if e["kind"] == "challenge_start"
        ] == []

        # Closed store returns False -> must NOT log challenge_start and must return JSON error.
        monkeypatch.setattr(client.app.state.progress, "start_challenge", lambda *_a, **_k: False)
        res2 = client.post("/api/challenges/4/start")
        assert res2.status_code == 500
        assert res2.json()["error"]
        assert [
            e for e in events.read_events(tmp_path / "events.jsonl")
            if e["kind"] == "challenge_start"
        ] == []


# --- F9: crediting twice adds one pass (idempotent across backfills and ON CONFLICT DO NOTHING)


def test_credit_challenges_idempotent_across_double_calls_backfills_and_conflict(
    tmp_path, monkeypatch
):
    # Direct Progress-level double credit + racing conflict check (kills removing ON CONFLICT).
    db_path = tmp_path / "idemp.db"
    prog = Progress(db_path)
    try:
        prog.start_challenge(4, 1000.0, 900)
        stamp = mint(_event("ism", ts=1050.0))
        assert stamp is not None
        qual = lambda attempt, st: qualifies(challenge_for(4), attempt, st)  # noqa: E731

        first = prog.credit_challenges((stamp,), qual)
        second = prog.credit_challenges((stamp,), qual)
        assert len(first) == 1
        assert second == []
        assert prog.challenge(4)["passes"] == 1

        # Start a new attempt and simulate a concurrent insert before credit_challenges inserts:
        # ON CONFLICT DO NOTHING must absorb the conflict cleanly instead of raising IntegrityError.
        prog.start_challenge(4, 2000.0, 900)
        stamp2 = mint(_event("ism", ts=2050.0))
        assert stamp2 is not None

        def racing_qual(attempt, st):
            prog._conn.execute(
                "INSERT INTO challenge_passes("
                "unit_id, started_at, passed_at, instrument, provenance, digest"
                ") VALUES(?, ?, ?, ?, ?, ?)",
                (attempt["unit_id"], attempt["started_at"], st["first_ts"],
                 st["instrument"], "{}", st["digest"]),
            )
            return True

        raced = prog.credit_challenges((stamp2,), racing_qual)
        assert raced == []
        assert prog.challenge(4)["passes"] == 2
    finally:
        prog.close()

    # Probe P3: live credit -> rotation backfill -> restart backfill = 1 pass, 1 challenge_pass.
    now = [1000.0]
    _patch_log_clock(monkeypatch, now)
    run_dir = tmp_path / "p3"
    run_dir.mkdir()
    with _challenge_client(run_dir, now) as client:
        assert client.post("/api/challenges/4/start").status_code == 200
        now[0] = 1020.0
        _hear(client)
        # Rotate and hear again to force _backfill_stamps over retained events.
        events.log_event(run_dir / "events.jsonl", "tick", {}, max_bytes=1, clock=lambda: 1025.0)
        now[0] = 1030.0
        _hear(client)
    with _challenge_client(run_dir, now) as client:
        state = client.get("/api/challenges/4").json()
        assert state["state"] == "passed"
        assert state["passes"] == 1
    pass_events = [
        e for e in events.read_events(run_dir / "events.jsonl")
        if e["kind"] == "challenge_pass"
    ]
    assert len(pass_events) == 1


# --- R2-N1 & R2-N2: failed challenge_start write undoes attempt; start error persists on poll ---


def test_start_log_failure_undoes_attempt_so_clock_step_restart_never_credits_old_reception(
    tmp_path, monkeypatch
):
    # Probe P1d: LoRa heard at ts=2000; clock steps back to 1500; Start's challenge_start write
    # fails (disk full). The attempt must be undone so restart backfill never credits ts=2000.
    now = [2000.0]
    _patch_log_clock(monkeypatch, now)
    real_log = console.log_event

    with _challenge_client(tmp_path, now) as client:
        _hear_lora(client)
        now[0] = 1500.0

        def fail_start(path, kind, payload, *args, **kwargs):
            if kind == "challenge_start":
                raise OSError("disk full")
            return real_log(path, kind, payload, *args, **kwargs)

        monkeypatch.setattr(console, "log_event", fail_start)
        failed = client.post("/api/challenges/9/start")
        assert failed.status_code == 500
        body = failed.json()
        assert body["state"] == "idle"
        assert body["error"] and "disk full" in body["error"]

        # R2-N2: Start's error reason persists across subsequent GET polls until a clean Start.
        polled = client.get("/api/challenges/9").json()
        assert polled["state"] == "idle"
        assert polled["error"] and "disk full" in polled["error"]
        # Other units do not inherit Unit 9's start error.
        assert client.get("/api/challenges/4").json()["error"] is None

    # After restart (with log writes working again), backfill must NOT credit ts=2000.
    monkeypatch.setattr(console, "log_event", real_log)
    with _challenge_client(tmp_path, now) as client:
        after_restart = client.get("/api/challenges/9").json()
        assert after_restart["state"] == "idle"
        assert after_restart["passes"] == 0
        assert after_restart["latest_pass"] is None

        # A clean Start clears any prior start error and begins a live attempt.
        ok = client.post("/api/challenges/9/start")
        assert ok.status_code == 200
        assert ok.json()["state"] == "active"
        assert ok.json()["error"] is None

        # If a later Try-again Start fails to log, the previous attempt is restored intact.
        now[0] = 1600.0
        real_log2 = console.log_event

        def fail_second_start(path, kind, payload, *args, **kwargs):
            if kind == "challenge_start":
                raise OSError("read-only fs")
            return real_log2(path, kind, payload, *args, **kwargs)

        monkeypatch.setattr(console, "log_event", fail_second_start)
        retry_failed = client.post("/api/challenges/9/start")
        assert retry_failed.status_code == 500
        retry_body = retry_failed.json()
        assert retry_body["state"] == "active"
        assert retry_body["started_at"] == 1500.0
        assert retry_body["error"] and "read-only fs" in retry_body["error"]
        assert client.get("/api/challenges/9").json()["error"] == retry_body["error"]

        # Restoring log writes and succeeding clears the persisted start error.
        monkeypatch.setattr(console, "log_event", real_log2)
        recovered = client.post("/api/challenges/9/start")
        assert recovered.status_code == 200
        assert recovered.json()["started_at"] == 1600.0
        assert recovered.json()["error"] is None
        assert client.get("/api/challenges/9").json()["error"] is None
