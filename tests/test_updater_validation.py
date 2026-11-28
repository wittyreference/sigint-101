# ABOUTME: PR 2 updater tests covering exact pair attestation, saved-state validation,
# ABOUTME: private read-only trial execution/reaping, drop-in settings (R2-1), and snapshot copy.

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101.app import PACK_DIR, create_app
from sigint101.events import log_event
from sigint101.exam import POOL_PATH, ExamStore
from sigint101.hardware import RadioProbe
from sigint101.migrations import EXAM_SCHEMA_VERSION, PROGRESS_SCHEMA_VERSION, migrate_store_copy
from sigint101.progress import Progress
from sigint101.snapshot import (
    GENERATION_MARKER_FILENAME,
    SnapshotError,
    copy_generation_snapshot,
    inventory_generation,
)
from sigint101.trial import (
    TRIAL_READY_PATH,
    create_trial_app,
    http_get_unix,
    run_private_trial,
)
from sigint101.validation import (
    RELEASE_MANIFEST_FILENAME,
    UnitSettingsError,
    ValidationError,
    check_unit_settings_parity,
    compute_settings_fingerprint,
    parse_systemd_unit_config,
    verify_pair_attestation,
    write_release_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _absent():
    return RadioProbe(present=False, known=True, method="stub", detail="no radio attached")


def _make_managed_generation(
    tmp_path: Path,
    name: str = "gen1",
    *,
    generation_id: str | None = None,
) -> Path:
    """Create a valid managed v1 data generation with generation.json marker."""
    gen_dir = tmp_path / name
    gen_dir.mkdir(parents=True, exist_ok=True)
    gid = generation_id or name
    migrate_store_copy(
        tmp_path / f"missing_p_{name}.db",
        gen_dir / "progress.db",
        store="progress",
        allow_missing_source=True,
        scratch_dir=tmp_path / f"scratch_p_{name}",
    )
    migrate_store_copy(
        tmp_path / f"missing_e_{name}.db",
        gen_dir / "exam.db",
        store="exam",
        allow_missing_source=True,
        scratch_dir=tmp_path / f"scratch_e_{name}",
    )
    (gen_dir / GENERATION_MARKER_FILENAME).write_text(
        json.dumps({"generation_id": gid}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return gen_dir


def _copy_release_tree(dest_root: Path, *, release_id: str = "rel-test-1", commit: str = "abc1234"):
    """Copy a clean, bytecode-free sigint101 release tree into dest_root with release.json."""
    dest_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        REPO_ROOT / "sigint101",
        dest_root / "sigint101",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copytree(REPO_ROOT / "lesson_packs", dest_root / "lesson_packs")
    write_release_manifest(dest_root, release_id=release_id, commit=commit)
    return dest_root


# --- 1. Exact pair attestation (§8: test_readiness_requires_the_actual_open_pair) ---------------


def test_readiness_requires_the_actual_open_pair(tmp_path):
    # Main-side comparison: normal home route returns 200 without pair/generation attestation,
    # even against a different same-head data directory with no generation marker.
    unattested_dir = tmp_path / "unattested"
    unattested_dir.mkdir()
    with TestClient(create_app(data_dir=unattested_dir, probe=_absent)) as legacy_client:
        assert legacy_client.get("/").status_code == 200

    # PR 2 contract: build a clean release tree and two distinct same-head data generations.
    rel_root = _copy_release_tree(tmp_path / "release_a", release_id="rel-001", commit="1111aaa")
    gen_a = _make_managed_generation(tmp_path, "gen_a", generation_id="gen-A")
    gen_b = _make_managed_generation(tmp_path, "gen_b", generation_id="gen-B")

    # Trial against (rel_root, gen_a) passes and attests exact open DB identities & package root.
    res = run_private_trial(
        python_executable=sys.executable,
        release_root=rel_root,
        data_dir=gen_a,
        expected_release_id="rel-001",
        expected_commit="1111aaa",
        expected_generation_id="gen-A",
        socket_parent_dir=tmp_path,
    )
    assert res.ready is True, res.errors
    assert res.status_code == 200
    att = res.attestation
    assert att["release"]["release_id"] == "rel-001"
    assert att["release"]["commit"] == "1111aaa"
    assert att["release"]["package_root"] == str(rel_root.resolve())
    assert att["generation"]["generation_id"] == "gen-A"
    assert att["databases"]["progress"]["st_ino"] == os.stat(gen_a / "progress.db").st_ino
    assert att["databases"]["exam"]["st_ino"] == os.stat(gen_a / "exam.db").st_ino
    assert att["databases"]["progress"]["user_version"] == PROGRESS_SCHEMA_VERSION
    assert att["databases"]["exam"]["user_version"] == EXAM_SCHEMA_VERSION

    # Opening gen_a while expecting gen_b (same schema head, different inode/marker) MUST fail.
    with pytest.raises(ValidationError, match="generation|inode|path"):
        verify_pair_attestation(
            att,
            expected_release_id="rel-001",
            expected_commit="1111aaa",
            expected_package_root=rel_root,
            expected_generation_id="gen-B",
            expected_data_dir=gen_b,
        )

    # Even if gen_b has the SAME generation_id string in generation.json, its dir/DB inodes differ!
    (gen_b / GENERATION_MARKER_FILENAME).write_text(
        json.dumps({"generation_id": "gen-A"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="identity|inode|path|database"):
        verify_pair_attestation(
            att,
            expected_release_id="rel-001",
            expected_commit="1111aaa",
            expected_package_root=rel_root,
            expected_generation_id="gen-A",
            expected_data_dir=gen_b,
        )

    # Replacing progress.db with a fresh copy inside gen_a changes its inode -> refused!
    replaced = gen_a / "progress.db.new"
    shutil.copy2(gen_a / "progress.db", replaced)
    replaced.replace(gen_a / "progress.db")
    with pytest.raises(ValidationError, match="inode|path|database"):
        verify_pair_attestation(
            att,
            expected_release_id="rel-001",
            expected_commit="1111aaa",
            expected_package_root=rel_root,
            expected_generation_id="gen-A",
            expected_data_dir=gen_a,
        )

    # Expecting a different release commit or package root MUST fail.
    rel_other = _copy_release_tree(tmp_path / "release_b", release_id="rel-002", commit="2222bbb")
    with pytest.raises(ValidationError, match="release|commit|package"):
        verify_pair_attestation(
            att,
            expected_release_id="rel-002",
            expected_commit="2222bbb",
            expected_package_root=rel_other,
            expected_generation_id="gen-A",
            expected_data_dir=gen_a,
        )

    # Missing generation.json or release.json fails trial readiness.
    gen_no_marker = _make_managed_generation(tmp_path, "gen_no_marker")
    (gen_no_marker / GENERATION_MARKER_FILENAME).unlink()
    bad_marker = run_private_trial(
        python_executable=sys.executable,
        release_root=rel_root,
        data_dir=gen_no_marker,
        expected_release_id="rel-001",
        expected_commit="1111aaa",
        socket_parent_dir=tmp_path,
    )
    assert bad_marker.ready is False
    assert any("generation" in err.lower() for err in bad_marker.errors)

    # Launch env vars cannot spoof attestation: fake env values are ignored by live inspection.
    spoof_settings = parse_systemd_unit_config(
        "[Service]\n"
        "Environment=SIGINT101_RELEASE_ID=spoofed-rel\n"
        "Environment=SIGINT101_GENERATION_ID=spoofed-gen\n"
    )
    spoof_res = run_private_trial(
        python_executable=sys.executable,
        release_root=rel_root,
        data_dir=gen_a,
        unit_settings=spoof_settings,
        expected_release_id="rel-001",
        expected_commit="1111aaa",
        expected_generation_id="gen-A",
        socket_parent_dir=tmp_path,
    )
    assert spoof_res.ready is True
    assert spoof_res.attestation["release"]["release_id"] == "rel-001"
    assert spoof_res.attestation["generation"]["generation_id"] == "gen-A"


# --- 2. Incomplete content & saved references (§8) ---------------------------------------------


def _seed_valid_saved_state(gen_dir: Path) -> None:
    """Populate realistic valid learner progress & exam rows for positive readiness."""
    prog = Progress(gen_dir / "progress.db", managed=True)
    try:
        prog.record_prediction(1, "wider")
        prog.record_stage(1, "observe", 4)
        prog.record_check(1, "power", correct=True)
        prog.record_sort_answer(5, "aircraft-am", "legal", correct=True)
        prog.record_place("rack", "waterfall")
    finally:
        prog.close()

    exam = ExamStore(gen_dir / "exam.db", managed=True)
    try:
        exam.record("T1A01", True)
        exam.start_sim(["T1A01", "T1B01"])
        exam.record_sim_answer("T1A01", "A")
        exam.skip_sim_question("T1B01")
    finally:
        exam.close()


def _corrupt_saved_state(gen_dir: Path, case: str) -> None:
    p_conn = sqlite3.connect(gen_dir / "progress.db")
    e_conn = sqlite3.connect(gen_dir / "exam.db")
    try:
        if case == "progress_unknown_lesson":
            p_conn.execute("INSERT INTO progress(lesson_id, opened, completed) VALUES(999, 1, 0)")
        elif case == "stages_unknown_lesson":
            p_conn.execute(
                "INSERT INTO stages(lesson_id, stage, furthest) VALUES(999, 'predict', 0)"
            )
        elif case == "stages_unknown_stage":
            p_conn.execute(
                "INSERT INTO stages(lesson_id, stage, furthest) VALUES(1, 'nonexistent', 0)"
            )
        elif case == "stages_bad_furthest":
            p_conn.execute(
                "INSERT INTO stages(lesson_id, stage, furthest) VALUES(1, 'predict', 99)"
            )
        elif case == "response_bad_prediction_choice":
            p_conn.execute(
                "INSERT INTO responses(lesson_id, kind, body) VALUES(1, 'prediction', 'bogus-chip')"
            )
        elif case == "response_bad_check_choice":
            p_conn.execute(
                "INSERT INTO responses(lesson_id, kind, body) VALUES(1, 'check', 'bogus-check')"
            )
        elif case == "response_unknown_kind":
            p_conn.execute(
                "INSERT INTO responses(lesson_id, kind, body) VALUES(1, 'mystery', 'hello')"
            )
        elif case == "sort_unknown_card":
            p_conn.execute(
                "INSERT INTO sort_answers(lesson_id, card_key, choice, correct) "
                "VALUES(5, 'deleted-card', 'legal', 1)"
            )
        elif case == "sort_bad_choice":
            p_conn.execute(
                "INSERT INTO sort_answers(lesson_id, card_key, choice, correct) "
                "VALUES(5, 'aircraft-am', 'maybe', 0)"
            )
        elif case == "sort_lesson_without_deck":
            p_conn.execute(
                "INSERT INTO sort_answers(lesson_id, card_key, choice, correct) "
                "VALUES(1, 'aircraft-am', 'legal', 1)"
            )
        elif case == "place_unknown_instrument":
            p_conn.execute("INSERT INTO places(name, value) VALUES('rack', 'ghost-instrument')")
        elif case == "srs_unknown_question_no_sim":
            e_conn.execute(
                "INSERT INTO srs(question_id, box, last_seen, seen, correct) "
                "VALUES('T9Z99', 2, 1000.0, 1, 1)"
            )
        elif case == "srs_bad_box":
            e_conn.execute(
                "INSERT INTO srs(question_id, box, last_seen, seen, correct) "
                "VALUES('T1A01', 99, 1000.0, 1, 1)"
            )
        elif case == "sim_unknown_question_id":
            e_conn.execute(
                "INSERT INTO sim(id, question_ids, answers, created, skipped) "
                "VALUES(1, '[\"T1A01\", \"T9Z99\"]', '{}', 1.0, '[]')"
            )
        elif case == "sim_bad_answer_key":
            e_conn.execute(
                "INSERT INTO sim(id, question_ids, answers, created, skipped) "
                "VALUES(1, '[\"T1A01\"]', '{\"T9Z99\": \"A\"}', 1.0, '[]')"
            )
        elif case == "sim_bad_answer_option":
            e_conn.execute(
                "INSERT INTO sim(id, question_ids, answers, created, skipped) "
                "VALUES(1, '[\"T1A01\"]', '{\"T1A01\": \"Z\"}', 1.0, '[]')"
            )
        elif case == "sim_bad_skipped_id":
            e_conn.execute(
                "INSERT INTO sim(id, question_ids, answers, created, skipped) "
                "VALUES(1, '[\"T1A01\"]', '{}', 1.0, '[\"T9Z99\"]')"
            )
        else:
            raise AssertionError(f"unhandled case: {case}")
        p_conn.commit()
        e_conn.commit()
    finally:
        p_conn.close()
        e_conn.close()


def test_readiness_rejects_incomplete_content_and_every_saved_reference(tmp_path):
    # Main-side comparison: normal console returns 200 on home even with empty packs or absent pool.
    legacy_empty_packs = tmp_path / "legacy_empty_packs"
    legacy_empty_packs.mkdir()
    legacy_data = tmp_path / "legacy_data"
    with TestClient(
        create_app(
            pack_dir=legacy_empty_packs,
            data_dir=legacy_data,
            pool_path=tmp_path / "no_pool.json",
            probe=_absent,
        )
    ) as legacy_client:
        assert legacy_client.get("/").status_code == 200

    # Positive radio-free readiness with real course content, real pool, and valid saved state.
    valid_gen = _make_managed_generation(tmp_path, "valid_gen")
    _seed_valid_saved_state(valid_gen)
    trial_app = create_trial_app(
        pack_dir=PACK_DIR,
        data_dir=valid_gen,
        pool_path=POOL_PATH,
        release_root=REPO_ROOT,
        require_release_manifest=False,
        probe=_absent,
    )
    with TestClient(trial_app) as client:
        ok = client.get(TRIAL_READY_PATH)
        assert ok.status_code == 200, ok.json()
        assert ok.json()["ready"] is True

    # Content & asset refusals: empty pack dir, missing required pack, broken template, bad pool.
    empty_packs = tmp_path / "empty_packs"
    empty_packs.mkdir()
    with TestClient(
        create_trial_app(
            pack_dir=empty_packs,
            data_dir=valid_gen,
            pool_path=POOL_PATH,
            release_root=REPO_ROOT,
            require_release_manifest=False,
            probe=_absent,
        )
    ) as c_empty:
        r = c_empty.get(TRIAL_READY_PATH)
        assert r.status_code == 503
        assert r.json()["ready"] is False

    incomplete_packs = tmp_path / "incomplete_packs"
    shutil.copytree(PACK_DIR, incomplete_packs)
    (incomplete_packs / "05-the-rules.yaml").unlink()
    with TestClient(
        create_trial_app(
            pack_dir=incomplete_packs,
            data_dir=valid_gen,
            pool_path=POOL_PATH,
            release_root=REPO_ROOT,
            require_release_manifest=False,
            probe=_absent,
        )
    ) as c_inc:
        r = c_inc.get(TRIAL_READY_PATH)
        assert r.status_code == 503
        assert any("lesson 5" in e.lower() or "missing" in e.lower() for e in r.json()["errors"])

    broken_tpl = tmp_path / "broken_templates"
    shutil.copytree(REPO_ROOT / "sigint101" / "templates", broken_tpl)
    (broken_tpl / "home.html").write_text("{% block broken %}", encoding="utf-8")
    with TestClient(
        create_trial_app(
            pack_dir=PACK_DIR,
            data_dir=valid_gen,
            pool_path=POOL_PATH,
            release_root=REPO_ROOT,
            templates_dir=broken_tpl,
            require_release_manifest=False,
            probe=_absent,
        )
    ) as c_tpl:
        r = c_tpl.get(TRIAL_READY_PATH)
        assert r.status_code == 503
        assert any("template" in e.lower() for e in r.json()["errors"])

    bad_pool = tmp_path / "bad_pool.json"
    bad_pool.write_text('{"questions": []}', encoding="utf-8")
    for pool_case in (tmp_path / "missing_pool.json", bad_pool):
        with TestClient(
            create_trial_app(
                pack_dir=PACK_DIR,
                data_dir=valid_gen,
                pool_path=pool_case,
                release_root=REPO_ROOT,
                require_release_manifest=False,
                probe=_absent,
            )
        ) as c_pool:
            r = c_pool.get(TRIAL_READY_PATH)
            assert r.status_code == 503
            assert any("exam pool" in e.lower() or "pool" in e.lower() for e in r.json()["errors"])


@pytest.mark.parametrize(
    "case",
    [
        "progress_unknown_lesson",
        "stages_unknown_lesson",
        "stages_unknown_stage",
        "stages_bad_furthest",
        "response_bad_prediction_choice",
        "response_bad_check_choice",
        "response_unknown_kind",
        "sort_unknown_card",
        "sort_bad_choice",
        "sort_lesson_without_deck",
        "place_unknown_instrument",
        "srs_unknown_question_no_sim",
        "srs_bad_box",
        "sim_unknown_question_id",
        "sim_bad_answer_key",
        "sim_bad_answer_option",
        "sim_bad_skipped_id",
    ],
)
def test_readiness_rejects_every_unsupported_saved_reference(tmp_path, case):
    gen = _make_managed_generation(tmp_path, f"gen_{case}")
    _corrupt_saved_state(gen, case)
    trial_app = create_trial_app(
        pack_dir=PACK_DIR,
        data_dir=gen,
        pool_path=POOL_PATH,
        release_root=REPO_ROOT,
        require_release_manifest=False,
        probe=_absent,
    )
    with TestClient(trial_app) as client:
        res = client.get(TRIAL_READY_PATH)
        assert res.status_code == 503, f"{case} unexpectedly passed: {res.json()}"
        body = res.json()
        assert body["ready"] is False
        assert body["errors"], f"{case} reported no error detail"


# --- 3. Read-only, private socket, no leaked lock FD, and reaped on parent death (§8) ----------


def test_trial_is_read_only_private_and_reaped_even_on_parent_death(tmp_path):
    # Main-side comparison: normal console lifespan writes stamp backfill to progress.db
    # and appends exam_disabled to events.jsonl when the pool is absent.
    main_gen = _make_managed_generation(tmp_path, "main_cmp")
    events_path = main_gen / "events.jsonl"
    log_event(
        events_path,
        "reception",
        {
            "instrument": "ism",
            "tool": "rtl_433",
            "command": "rtl_433 -f 433920000 -F json",
            "freq_hz": 433_920_000,
            "item": {"model": "Acurite-Tower", "temp_c": 21.0},
            "radio": {"kind": "rtl-sdr", "serial": "001", "index": 0},
        },
        clock=lambda: 1_700_000_000.0,
    )
    events_before_main = events_path.read_text(encoding="utf-8")
    with TestClient(
        create_app(
            data_dir=main_gen,
            pool_path=tmp_path / "absent_pool.json",
            probe=_absent,
            managed=True,
        )
    ) as legacy_client:
        assert legacy_client.get("/").status_code == 200
        assert len(legacy_client.app.state.progress.stamps()) == 1
    assert "exam_disabled" in events_path.read_text(encoding="utf-8")
    assert events_path.read_text(encoding="utf-8") != events_before_main

    # PR 2 trial: seed backfillable events + captures, hold a CLOEXEC lock FD, and run real trial.
    rel_root = _copy_release_tree(tmp_path / "rel_trial", release_id="rel-ro", commit="3333ccc")
    trial_gen = _make_managed_generation(tmp_path, "trial_gen", generation_id="gen-ro")
    trial_events = trial_gen / "events.jsonl"
    log_event(
        trial_events,
        "reception",
        {
            "instrument": "ism",
            "tool": "rtl_433",
            "command": "rtl_433 -f 433920000 -F json",
            "freq_hz": 433_920_000,
            "item": {"model": "Acurite-Tower", "temp_c": 21.0},
            "radio": {"kind": "rtl-sdr", "serial": "001", "index": 0},
        },
        clock=lambda: 1_700_000_000.0,
    )
    (trial_gen / "apt").mkdir()
    (trial_gen / "apt" / "pass.png").write_bytes(b"\x89PNG\r\n\x1a\ndata")

    before_progress = (trial_gen / "progress.db").read_bytes()
    before_exam = (trial_gen / "exam.db").read_bytes()
    before_events = trial_events.read_bytes()
    before_apt = (trial_gen / "apt" / "pass.png").read_bytes()

    lock_file = tmp_path / "updater.lock"
    lock_fd = os.open(os.fspath(lock_file), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    observed: dict = {}

    def inspect_running_trial(sock_path: Path, child_pid: int) -> None:
        # 1) Socket directory is private mode 0700.
        observed["dir_mode"] = stat.S_IMODE(os.stat(sock_path.parent).st_mode)
        # 2) Normal routes with mutating GETs are unreachable on the trial socket.
        for path in ("/", "/lesson/1", "/api/spectrum", "/api/hardware", "/operate"):
            status, _ = http_get_unix(sock_path, path)
            observed[f"route:{path}"] = status
        # 3) Parent's O_CLOEXEC lock file descriptor is NOT leaked into the trial child.
        child_fd_targets = []
        fd_dir = Path(f"/proc/{child_pid}/fd")
        if fd_dir.is_dir():
            for entry in fd_dir.iterdir():
                try:
                    child_fd_targets.append(os.readlink(entry))
                except OSError:
                    pass
        observed["leaked_lock"] = str(lock_file) in child_fd_targets

    try:
        res = run_private_trial(
            python_executable=sys.executable,
            release_root=rel_root,
            data_dir=trial_gen,
            expected_release_id="rel-ro",
            expected_commit="3333ccc",
            expected_generation_id="gen-ro",
            socket_parent_dir=tmp_path,
            on_ready=inspect_running_trial,
        )
    finally:
        os.close(lock_fd)

    assert res.ready is True, res.errors
    assert observed["dir_mode"] == 0o700
    assert observed["leaked_lock"] is False
    for path in ("/", "/lesson/1", "/api/spectrum", "/api/hardware", "/operate"):
        assert observed[f"route:{path}"] == 404, (path, observed[f"route:{path}"])

    # Zero mutations to progress.db (stamp backfill suppressed), exam.db, events.jsonl, captures!
    assert (trial_gen / "progress.db").read_bytes() == before_progress
    assert (trial_gen / "exam.db").read_bytes() == before_exam
    assert trial_events.read_bytes() == before_events
    assert (trial_gen / "apt" / "pass.png").read_bytes() == before_apt

    # Even when exam pool is absent, trial suppresses exam_disabled event write!
    bad_pool_res = run_private_trial(
        python_executable=sys.executable,
        release_root=rel_root,
        data_dir=trial_gen,
        pool_path=tmp_path / "missing_pool.json",
        expected_release_id="rel-ro",
        expected_commit="3333ccc",
        expected_generation_id="gen-ro",
        socket_parent_dir=tmp_path,
    )
    assert bad_pool_res.ready is False
    assert trial_events.read_bytes() == before_events

    # Reaped: child PID must no longer exist after run_private_trial returns.
    with pytest.raises(ProcessLookupError):
        os.kill(res.pid, 0)

    # Parent-death cleanup (PR_SET_PDEATHSIG): kill a parent holding a live trial child.
    pid_file = tmp_path / "trial_child.pid"
    helper = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "from sigint101.trial import run_private_trial\n"
        f"pid_file = Path({str(pid_file)!r})\n"
        "def hold(_sock, child_pid):\n"
        "    pid_file.write_text(str(child_pid), encoding='utf-8')\n"
        "    time.sleep(30)\n"
        "run_private_trial(\n"
        "    python_executable=sys.executable,\n"
        f"    release_root=Path({str(rel_root)!r}),\n"
        f"    data_dir=Path({str(trial_gen)!r}),\n"
        "    expected_release_id='rel-ro',\n"
        "    expected_commit='3333ccc',\n"
        "    expected_generation_id='gen-ro',\n"
        f"    socket_parent_dir=Path({str(tmp_path)!r}),\n"
        "    on_ready=hold,\n"
        ")\n"
    )
    parent_env = os.environ.copy()
    parent_env["PYTHONPATH"] = str(REPO_ROOT)
    parent = subprocess.Popen(
        [sys.executable, "-B", "-c", helper],
        env=parent_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not pid_file.is_file():
            assert parent.poll() is None, parent.stderr.read().decode("utf-8", "replace")
            time.sleep(0.05)
        assert pid_file.is_file(), "parent never started trial child"
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        # Confirm child is alive before parent dies.
        os.kill(child_pid, 0)
        # Abruptly SIGKILL parent; PR_SET_PDEATHSIG must kill the trial child promptly.
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        death_deadline = time.monotonic() + 5.0
        alive = True
        while time.monotonic() < death_deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        assert not alive, f"trial child {child_pid} survived parent SIGKILL"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


# --- 4. Effective drop-in settings, shared startup, and R2-1 parity (§8) -----------------------


def test_trial_uses_effective_drop_in_settings_and_public_initialization(tmp_path):
    # 1) Structured unit + drop-in parsing: no shell evaluation, no secret leakage in repr.
    pwn_marker = tmp_path / "shell_was_evaluated"
    unit_text = (
        "[Unit]\nDescription=SIGINT 101\n"
        "[Service]\n"
        "ExecStart=/usr/bin/uvicorn sigint101.app:app --host 127.0.0.1 --port 8008\n"
        'Environment="SIGINT101_FLOCK_SOURCE=marauder"\n'
    )
    dropin_text = (
        "[Service]\n"
        f'Environment="SIGINT101_TEST_SECRET=top-secret-token-999" '
        f'"SIGINT101_NO_SHELL=$(touch {pwn_marker})"\n'
    )
    settings = parse_systemd_unit_config(unit_text, dropin_texts=(dropin_text,))
    assert settings.env["SIGINT101_FLOCK_SOURCE"] == "marauder"
    assert settings.env["SIGINT101_NO_SHELL"] == f"$(touch {pwn_marker})"
    assert "top-secret-token-999" not in repr(settings)
    assert not pwn_marker.exists()

    # 2) Unsupported execution settings are refused rather than guessed.
    for bad_directive in (
        "DynamicUser=yes",
        "PrivateTmp=yes",
        "RootDirectory=/srv/chroot",
        "EnvironmentFile=/etc/sigint101.env",
        "User=nobody",
    ):
        with pytest.raises(UnitSettingsError, match="unsupported"):
            parse_systemd_unit_config(unit_text, dropin_texts=(f"[Service]\n{bad_directive}\n",))

    # 3) Run real trial with effective drop-in settings: fingerprint recorded, secret not leaked,
    # and shell metacharacters never executed.
    rel_root = _copy_release_tree(tmp_path / "rel_cfg", release_id="rel-cfg", commit="4444ddd")
    gen = _make_managed_generation(tmp_path, "gen_cfg", generation_id="gen-cfg")
    res = run_private_trial(
        python_executable=sys.executable,
        release_root=rel_root,
        data_dir=gen,
        unit_settings=settings,
        expected_release_id="rel-cfg",
        expected_commit="4444ddd",
        expected_generation_id="gen-cfg",
        socket_parent_dir=tmp_path,
    )
    assert res.ready is True, res.errors
    assert not pwn_marker.exists()
    assert res.settings_fingerprint == compute_settings_fingerprint(settings.unit_only())
    assert "top-secret-token-999" not in json.dumps(res.attestation)

    # 4) Shared startup: a broken release pack (invalid YAML) fails BOTH public startup
    # AND the private trial through the shared initialize_console_state path.
    rel_bad_yaml = _copy_release_tree(
        tmp_path / "rel_bad_yaml", release_id="rel-bad-yaml", commit="4444bad"
    )
    (rel_bad_yaml / "lesson_packs" / "99-broken.yaml").write_text(
        "id: 99\nnot_valid: [", encoding="utf-8"
    )
    with pytest.raises(Exception):
        with TestClient(
            create_app(
                pack_dir=rel_bad_yaml / "lesson_packs",
                data_dir=gen,
                probe=_absent,
                managed=True,
            )
        ):
            pass
    bad_res = run_private_trial(
        python_executable=sys.executable,
        release_root=rel_bad_yaml,
        data_dir=gen,
        unit_settings=settings,
        expected_release_id="rel-bad-yaml",
        expected_commit="4444bad",
        expected_generation_id="gen-cfg",
        socket_parent_dir=tmp_path,
    )
    assert bad_res.ready is False
    assert bad_res.errors

    # 5) R2-1 parity check:
    # Before serving marker exists, drift in the unit's own settings is REFUSED.
    recorded_fp = compute_settings_fingerprint(settings.unit_only())
    drifted_unit = parse_systemd_unit_config(
        unit_text + 'Environment="SIGINT101_UNIT_FLAG=changed"\n',
        dropin_texts=(dropin_text,),
    )
    with pytest.raises(UnitSettingsError, match="parity|drift|settings"):
        check_unit_settings_parity(
            recorded_fingerprint=recorded_fp,
            current_settings=drifted_unit,
            serving_marker_exists=False,
        )
    # Matching unit settings with a site drop-in edit on a served generation MUST NOT refuse boot!
    site_edited_after_serve = parse_systemd_unit_config(
        unit_text,
        dropin_texts=(
            '[Service]\nEnvironment="SIGINT101_RDS_HZ=101100000" "SIGINT101_RX_LAT=38.9"\n',
        ),
    )
    assert (
        check_unit_settings_parity(
            recorded_fingerprint=recorded_fp,
            current_settings=site_edited_after_serve,
            serving_marker_exists=True,
        )
        is True
    )
    # Even before serving marker, editing site-owned drop-in settings keeps unit parity intact.
    assert (
        check_unit_settings_parity(
            recorded_fingerprint=recorded_fp,
            current_settings=site_edited_after_serve,
            serving_marker_exists=False,
        )
        is True
    )


# --- 5. Consistent snapshot copying & inventory (§3, §8, R2-7) ---------------------------------


def test_snapshot_copies_complete_generation_and_preserves_sources(tmp_path, monkeypatch):
    src = tmp_path / "source_gen"
    src.mkdir(mode=0o700)

    # Seed legacy v0 progress.db (with uncheckpointed WAL!) and exam.db.
    prog = Progress(src / "progress.db")
    prog.mark_completed(1)
    prog.record_prediction(1, "wider")
    prog._conn.execute("PRAGMA journal_mode = WAL")
    prog._conn.execute("PRAGMA wal_autocheckpoint = 0")
    prog._conn.execute("INSERT INTO progress(lesson_id, opened, completed) VALUES(2, 1, 1)")
    prog._conn.commit()
    # Snapshot active WAL pair before closing so -wal/-shm remain on disk as in a crash copy.
    wal_copy = tmp_path / "wal_tmp"
    wal_copy.mkdir()
    shutil.copy2(src / "progress.db", wal_copy / "progress.db")
    shutil.copy2(src / "progress.db-wal", wal_copy / "progress.db-wal")
    if (src / "progress.db-shm").exists():
        shutil.copy2(src / "progress.db-shm", wal_copy / "progress.db-shm")
    prog.close()
    shutil.copy2(wal_copy / "progress.db", src / "progress.db")
    shutil.copy2(wal_copy / "progress.db-wal", src / "progress.db-wal")
    if (wal_copy / "progress.db-shm").exists():
        shutil.copy2(wal_copy / "progress.db-shm", src / "progress.db-shm")

    exam = ExamStore(src / "exam.db")
    exam.record("T1A01", True)
    exam.close()

    # Seed events.jsonl + rotations, apt/ + tle.txt, sstv/ + sidecars + hidden .staging/.discard,
    # ordinary files, and empty directories.
    files_to_create = {
        "events.jsonl": b'{"event":"boot","ts":1}\n',
        "events.jsonl.1": b'{"event":"old1","ts":0}\n',
        "events.jsonl.2": b'{"event":"old2","ts":0}\n',
        "apt/tle.txt": b"NOAA 19\n1 33591U\n2 33591\n",
        "apt/noaa19/pass1.png": b"\x89PNG\r\n\x1a\napt-pass",
        "sstv/frame1.png": b"\x89PNG\r\n\x1a\nsstv-frame",
        "sstv/frame1.png.json": b'{"mode":"Robot36"}\n',
        "sstv/.staging/partial.tmp": b"staged-bytes",
        "sstv/.discard/corrupt.png": b"discarded-bytes",
        "ordinary_note.txt": b"keep-this-file\n",
    }
    empty_dirs = ("empty_top", "apt/empty_sat", "sstv/.empty_hidden")
    for rel in empty_dirs:
        d = src / rel
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)

    expected_ns: dict[str, int] = {}
    for idx, (rel, payload) in enumerate(files_to_create.items(), start=1):
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.parent.chmod(0o700)
        target.write_bytes(payload)
        ns = 1_700_000_000_000_000_000 + idx * 123_456_789
        os.utime(target, ns=(ns, ns))
        expected_ns[rel] = os.stat(target).st_mtime_ns

    # Record exact source bytes before copying; forbid shutil.rmtree.
    src_bytes_before = {
        p.relative_to(src).as_posix(): p.read_bytes()
        for p in sorted(src.rglob("*"))
        if p.is_file()
    }

    def forbid_rmtree(*_args, **_kwargs):
        raise AssertionError("snapshot copy must never call shutil.rmtree")

    monkeypatch.setattr(shutil, "rmtree", forbid_rmtree)

    dest = tmp_path / "dest_gen"
    scratch = tmp_path / "op_scratch"
    receipt = copy_generation_snapshot(
        src,
        dest,
        generation_id="gen-0002",
        scratch_dir=scratch,
    )

    assert receipt["generation_id"] == "gen-0002"
    assert (dest / GENERATION_MARKER_FILENAME).is_file()

    # Every non-DB file copied with identical bytes, SHA-256, and nanosecond mtime!
    for rel, payload in files_to_create.items():
        copied = dest / rel
        assert copied.is_file(), f"missing copied file: {rel}"
        assert copied.read_bytes() == payload
        assert os.stat(copied).st_mtime_ns == expected_ns[rel]
        assert receipt["sha256_by_path"][rel] == hashlib.sha256(payload).hexdigest()

    # Empty & hidden directories preserved.
    for rel in empty_dirs:
        assert (dest / rel).is_dir(), f"missing empty dir: {rel}"

    # Migrated stores open cleanly in managed mode with WAL-recovered lesson 2 and no sidecars!
    for ext in ("-wal", "-shm", "-journal"):
        assert not Path(f"{dest / 'progress.db'}{ext}").exists()
        assert not Path(f"{dest / 'exam.db'}{ext}").exists()

    p_dst = Progress(dest / "progress.db", managed=True)
    e_dst = ExamStore(dest / "exam.db", managed=True)
    try:
        assert p_dst.get(1)["completed"] == 1
        assert p_dst.get(2)["completed"] == 1
        assert e_dst.box_of("T1A01") == 2
    finally:
        p_dst.close()
        e_dst.close()

    # Source files are 100% byte-for-byte untouched!
    src_bytes_after = {
        p.relative_to(src).as_posix(): p.read_bytes()
        for p in sorted(src.rglob("*"))
        if p.is_file()
    }
    assert src_bytes_after == src_bytes_before


def test_snapshot_refuses_symlinks_bind_mounts_special_files_and_existing_dest(tmp_path):
    def make_clean_src(name: str) -> Path:
        d = tmp_path / name
        d.mkdir(mode=0o700)
        migrate_store_copy(
            tmp_path / f"none_p_{name}.db",
            d / "progress.db",
            store="progress",
            allow_missing_source=True,
            scratch_dir=tmp_path / f"sc_p_{name}",
        )
        migrate_store_copy(
            tmp_path / f"none_e_{name}.db",
            d / "exam.db",
            store="exam",
            allow_missing_source=True,
            scratch_dir=tmp_path / f"sc_e_{name}",
        )
        (d / "events.jsonl").write_text('{"event":"ok"}\n', encoding="utf-8")
        return d

    # 1) External symlink refused.
    s_ext = make_clean_src("src_ext_sym")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (s_ext / "leak.txt").symlink_to(outside)
    with pytest.raises(SnapshotError, match="symlink"):
        copy_generation_snapshot(
            s_ext, tmp_path / "dst_ext", generation_id="g1", scratch_dir=tmp_path / "sc_ext"
        )

    # 2) Same-root alias symlink refused.
    s_alias = make_clean_src("src_alias_sym")
    (s_alias / "alias.jsonl").symlink_to(s_alias / "events.jsonl")
    with pytest.raises(SnapshotError, match="symlink"):
        inventory_generation(s_alias)

    # 3) Source directory itself as a symlink refused.
    s_real = make_clean_src("src_real")
    s_link = tmp_path / "src_is_symlink"
    s_link.symlink_to(s_real)
    with pytest.raises(SnapshotError, match="symlink"):
        copy_generation_snapshot(
            s_link, tmp_path / "dst_link", generation_id="g2", scratch_dir=tmp_path / "sc_link"
        )

    # 4) Special file (FIFO) refused without blocking.
    s_fifo = make_clean_src("src_fifo")
    os.mkfifo(s_fifo / "pipe.fifo")
    with pytest.raises(SnapshotError, match="regular|special|file"):
        inventory_generation(s_fifo)

    # 5) Same-device bind mount (mount-ID change on a child directory, even when empty) refused.
    s_mnt = make_clean_src("src_mount")
    sub = s_mnt / "apt"
    sub.mkdir(mode=0o700)

    def fake_mount_id(_fd: int, rel_path: str) -> int:
        return 202 if rel_path.startswith("apt") else 101

    with pytest.raises(SnapshotError, match="mount"):
        copy_generation_snapshot(
            s_mnt,
            tmp_path / "dst_mnt",
            generation_id="g3",
            scratch_dir=tmp_path / "sc_mnt",
            mount_id_reader=fake_mount_id,
        )

    # 6) Unsafe group/world-writable directory permissions refused.
    s_perm = make_clean_src("src_perm")
    bad_dir = s_perm / "apt"
    bad_dir.mkdir(mode=0o777)
    bad_dir.chmod(0o777)
    with pytest.raises(SnapshotError, match="permission|writable"):
        inventory_generation(s_perm)

    # 7) Existing destination directory refused.
    existing_dest = tmp_path / "dst_already_exists"
    existing_dest.mkdir(mode=0o700)
    with pytest.raises(FileExistsError):
        copy_generation_snapshot(
            s_real, existing_dest, generation_id="g4", scratch_dir=tmp_path / "sc_ex"
        )

    # 8) Scratch inside or equal to source or dest refused.
    with pytest.raises(ValueError, match="scratch"):
        copy_generation_snapshot(
            s_real, tmp_path / "dst_bad_sc", generation_id="g5", scratch_dir=s_real / "scratch"
        )


# --- 6. Sealed release tree stays byte-stable (no .pyc / __pycache__) (§8, U8) ----------------


def test_trial_keeps_sealed_release_tree_byte_stable(tmp_path):
    rel_root = _copy_release_tree(
        tmp_path / "sealed_rel", release_id="rel-sealed", commit="5555eee"
    )
    gen = _make_managed_generation(tmp_path, "gen_sealed", generation_id="gen-sealed")

    def tree_snapshot(root: Path) -> dict[str, bytes]:
        return {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    before = tree_snapshot(rel_root)
    assert not any("__pycache__" in k or k.endswith(".pyc") for k in before)

    # Seal the release tree read-only (0555 dirs, 0444 files) to prove zero writes occur.
    all_paths = list(rel_root.rglob("*"))
    for p in all_paths:
        if p.is_file():
            p.chmod(0o444)
    for p in sorted(all_paths, key=lambda x: len(x.parts), reverse=True):
        if p.is_dir():
            p.chmod(0o555)
    rel_root.chmod(0o555)
    try:
        res = run_private_trial(
            python_executable=sys.executable,
            release_root=rel_root,
            data_dir=gen,
            expected_release_id="rel-sealed",
            expected_commit="5555eee",
            expected_generation_id="gen-sealed",
            socket_parent_dir=tmp_path,
        )
        assert res.ready is True, res.errors
        after = tree_snapshot(rel_root)
        assert after == before
        assert not list(rel_root.rglob("__pycache__"))
        assert not list(rel_root.rglob("*.pyc"))
    finally:
        rel_root.chmod(0o755)
        for p in all_paths:
            if p.is_dir():
                p.chmod(0o755)
        for p in all_paths:
            if p.is_file():
                p.chmod(0o644)


def test_validation_snapshot_and_trial_edge_refusals(tmp_path):
    # 1) Unit settings edge cases: WorkingDirectory, Restart, Environment= reset, errors.
    parsed = parse_systemd_unit_config(
        "[Service]\n"
        "WorkingDirectory=/srv/rel\n"
        "Restart=on-failure\n"
        'Environment="A=1" "B=hello world"\n',
        dropin_texts=("[Service]\nEnvironment=\nEnvironment=C=3\n",),
    )
    assert parsed.working_directory == "/srv/rel"
    assert parsed.other_directives["Restart"] == "on-failure"
    assert parsed.env == {"C": "3"}

    for bad_unit in (
        "[Service]\nNotAKeyValueLine\n",
        '[Service]\nEnvironment="UNTERMINATED=1\n',
        "[Service]\nEnvironment=NO_EQUALS\n",
        "[Service]\nEnvironment==emptykey\n",
    ):
        with pytest.raises(UnitSettingsError):
            parse_systemd_unit_config(bad_unit)

    # 2) Release manifest & generation marker refusals.
    from sigint101.validation import read_generation_marker, read_release_manifest

    empty_rel = tmp_path / "empty_rel"
    empty_rel.mkdir()
    with pytest.raises(ValidationError, match="missing"):
        read_release_manifest(empty_rel)
    (empty_rel / RELEASE_MANIFEST_FILENAME).write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValidationError, match="parsed"):
        read_release_manifest(empty_rel)
    (empty_rel / RELEASE_MANIFEST_FILENAME).write_text('{"release_id": ""}', encoding="utf-8")
    with pytest.raises(ValidationError, match="release_id"):
        read_release_manifest(empty_rel)
    (empty_rel / RELEASE_MANIFEST_FILENAME).write_text(
        '{"release_id": "r1", "commit": ""}', encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="commit"):
        read_release_manifest(empty_rel)

    bad_gen = tmp_path / "bad_gen"
    bad_gen.mkdir()
    (bad_gen / GENERATION_MARKER_FILENAME).write_text("{bad", encoding="utf-8")
    with pytest.raises(ValidationError, match="parsed"):
        read_generation_marker(bad_gen)
    (bad_gen / GENERATION_MARKER_FILENAME).write_text('{"generation_id": ""}', encoding="utf-8")
    with pytest.raises(ValidationError, match="generation_id"):
        read_generation_marker(bad_gen)

    # 3) Snapshot refuses external hardlink aliases and invalid generation_id.
    hard_src = tmp_path / "hard_src"
    hard_src.mkdir(mode=0o700)
    outside_hard = tmp_path / "outside_hard.txt"
    outside_hard.write_text("one", encoding="utf-8")
    os.link(outside_hard, hard_src / "b.txt")
    with pytest.raises(SnapshotError, match="hardlink"):
        inventory_generation(hard_src)

    with pytest.raises(ValueError, match="generation_id"):
        copy_generation_snapshot(
            hard_src,
            tmp_path / "dst_bad_id",
            generation_id="../escape",
            scratch_dir=tmp_path / "sc",
        )

    # 4) run_private_trial catches post-HTTP attestation mismatch and early child exit.
    rel_root = _copy_release_tree(tmp_path / "rel_edge", release_id="rel-edge", commit="9999fff")
    gen = _make_managed_generation(tmp_path, "gen_edge", generation_id="gen-edge")
    mismatch = run_private_trial(
        python_executable=sys.executable,
        release_root=rel_root,
        data_dir=gen,
        expected_release_id="rel-WRONG",
        expected_commit="9999fff",
        expected_generation_id="gen-edge",
        socket_parent_dir=tmp_path,
    )
    assert mismatch.ready is False
    assert mismatch.status_code == 503
    assert any("release_id mismatch" in err for err in mismatch.errors)

    early = run_private_trial(
        python_executable="/bin/false",
        release_root=rel_root,
        data_dir=gen,
        socket_parent_dir=tmp_path,
    )
    assert early.ready is False
    assert any("exited early" in err for err in early.errors)


# --- Round 2 build-review fixes: F1-F11 regression tests ---------------------------------------


def test_f1_dropin_pack_env_cannot_redirect_trial_and_pack_pool_are_attested(tmp_path):
    # Break release's own lesson_packs by deleting lesson 1.
    rel_broken = _copy_release_tree(
        tmp_path / "rel_broken", release_id="rel-broken", commit="bad0001"
    )
    (rel_broken / "lesson_packs" / "01-see-the-invisible.yaml").unlink()
    gen = _make_managed_generation(tmp_path, "gen_f1", generation_id="gen-f1")

    # 1) Setting SIGINT101_PACK_DIR/DATA_DIR/POOL_PATH in unit settings is refused outright.
    for var in ("SIGINT101_PACK_DIR", "SIGINT101_DATA_DIR", "SIGINT101_POOL_PATH"):
        with pytest.raises(UnitSettingsError, match=var):
            parse_systemd_unit_config(
                "[Service]\nExecStart=/usr/bin/uvicorn sigint101.app:app\n",
                dropin_texts=(f'[Service]\nEnvironment="{var}={PACK_DIR}"\n',),
            )

    # 2) Even if SIGINT101_PACK_DIR is smuggled via a raw UnitSettings object, the trial
    # ignores it, validates the release's own broken pack, and fails with missing lesson 1!
    from sigint101.validation import UnitSettings

    smuggled = UnitSettings(dropin_env={"SIGINT101_PACK_DIR": str(PACK_DIR)})
    res = run_private_trial(
        python_executable=sys.executable,
        release_root=rel_broken,
        data_dir=gen,
        unit_settings=smuggled,
        expected_release_id="rel-broken",
        expected_commit="bad0001",
        expected_generation_id="gen-f1",
        socket_parent_dir=tmp_path,
    )
    assert res.ready is False
    assert any("lesson 1" in e.lower() for e in res.errors)

    # 3) On a healthy release, attestation records exact pack dir and pool file identities,
    # and verify_pair_attestation rejects an attestation pointing outside the release!
    rel_ok = _copy_release_tree(tmp_path / "rel_ok", release_id="rel-ok", commit="ok00001")
    ok_res = run_private_trial(
        python_executable=sys.executable,
        release_root=rel_ok,
        data_dir=gen,
        expected_release_id="rel-ok",
        expected_commit="ok00001",
        expected_generation_id="gen-f1",
        socket_parent_dir=tmp_path,
    )
    assert ok_res.ready is True, ok_res.errors
    assert ok_res.attestation["pack"]["path"] == str((rel_ok / "lesson_packs").resolve())
    assert ok_res.attestation["pack"]["st_ino"] == os.stat(rel_ok / "lesson_packs").st_ino
    expected_pool = rel_ok / "sigint101" / "exam_data" / "technician-2026-2030.json"
    assert ok_res.attestation["pool"]["path"] == str(expected_pool.resolve())
    assert ok_res.attestation["pool"]["st_ino"] == os.stat(expected_pool).st_ino

    tampered_att = json.loads(json.dumps(ok_res.attestation))
    tampered_att["pack"]["path"] = str(PACK_DIR.resolve())
    tampered_att["pack"]["st_ino"] = os.stat(PACK_DIR).st_ino
    with pytest.raises(ValidationError, match="pack"):
        verify_pair_attestation(
            tampered_att,
            expected_release_id="rel-ok",
            expected_commit="ok00001",
            expected_package_root=rel_ok,
            expected_generation_id="gen-f1",
            expected_data_dir=gen,
        )


def test_f2_legacy_startup_matches_main_on_env_and_missing_packs(tmp_path, monkeypatch):
    # V1: legacy create_app with a missing lesson_packs directory serves / with 200 (0 lessons).
    missing_packs = tmp_path / "no_such_lesson_packs"
    legacy_data = tmp_path / "legacy_data"
    with TestClient(
        create_app(pack_dir=missing_packs, data_dir=legacy_data, probe=_absent)
    ) as client:
        res = client.get("/")
        assert res.status_code == 200
        assert client.app.state.lessons == {}

    # V2: legacy create_app ignores SIGINT101_DATA_DIR / SIGINT101_PACK_DIR in os.environ.
    hijack_data = tmp_path / "hijack_data"
    hijack_packs = tmp_path / "hijack_packs"
    monkeypatch.setenv("SIGINT101_DATA_DIR", str(hijack_data))
    monkeypatch.setenv("SIGINT101_PACK_DIR", str(hijack_packs))
    leg_dir = tmp_path / "real_legacy_data"
    with TestClient(create_app(data_dir=leg_dir, probe=_absent)) as client2:
        assert client2.get("/").status_code == 200
        assert (leg_dir / "progress.db").is_file()
        assert not hijack_data.exists()


def test_f3_snapshot_detects_concurrent_writes_after_inventory(tmp_path, monkeypatch):
    from sigint101 import snapshot as snap_mod

    def make_source(name: str) -> Path:
        s = tmp_path / name
        s.mkdir(mode=0o700)
        p = Progress(s / "progress.db")
        p.mark_completed(1)
        p.close()
        e = ExamStore(s / "exam.db")
        e.record("T1A01", True)
        e.close()
        (s / "events.jsonl").write_text('{"kind":"boot","ts":1}\n', encoding="utf-8")
        (s / "sstv").mkdir(mode=0o700)
        (s / "sstv" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\na")
        return s

    real_migrate = snap_mod.migrate_store_copy

    for case in ("append_events", "commit_progress_row", "publish_new_sstv"):
        src = make_source(f"src_{case}")
        injected = {"done": False}

        def hooked_migrate(*args, _case=case, _src=src, _inj=injected, **kwargs):
            if not _inj["done"]:
                _inj["done"] = True
                if _case == "append_events":
                    with (_src / "events.jsonl").open("ab") as fh:
                        fh.write(b'{"kind":"late","ts":2}\n')
                elif _case == "commit_progress_row":
                    p = Progress(_src / "progress.db")
                    p.mark_completed(2)
                    p.close()
                elif _case == "publish_new_sstv":
                    (_src / "sstv" / "b.png").write_bytes(b"\x89PNG\r\n\x1a\nb")
            return real_migrate(*args, **kwargs)

        monkeypatch.setattr(snap_mod, "migrate_store_copy", hooked_migrate)
        with pytest.raises(SnapshotError, match="changed|drift"):
            copy_generation_snapshot(
                src,
                tmp_path / f"dst_{case}",
                generation_id=f"gen-{case}",
                scratch_dir=tmp_path / f"sc_{case}",
            )
        monkeypatch.undo()


def test_f4_snapshot_accepts_in_generation_sstv_hardlink_and_refuses_external(tmp_path):
    # Legitimate interrupted SSTV publish: published PNG and staged PNG share inode (nlink=2)
    # inside the same generation -> snapshot MUST succeed and preserve both names!
    src = tmp_path / "src_sstv_link"
    src.mkdir(mode=0o700)
    migrate_store_copy(
        tmp_path / "np.db",
        src / "progress.db",
        store="progress",
        allow_missing_source=True,
        scratch_dir=tmp_path / "sc_p",
    )
    migrate_store_copy(
        tmp_path / "ne.db",
        src / "exam.db",
        store="exam",
        allow_missing_source=True,
        scratch_dir=tmp_path / "sc_e",
    )
    (src / "sstv").mkdir(mode=0o700)
    (src / "sstv" / ".staging").mkdir(mode=0o700)
    staged = src / "sstv" / ".staging" / "a.png"
    published = src / "sstv" / "a.png"
    staged.write_bytes(b"\x89PNG\r\n\x1a\nsstv-in-gen")
    os.link(staged, published)
    assert os.stat(published).st_nlink == 2

    dest = tmp_path / "dst_sstv_link"
    receipt = copy_generation_snapshot(
        src, dest, generation_id="gen-sstv-hl", scratch_dir=tmp_path / "sc_hl"
    )
    assert (dest / "sstv" / "a.png").read_bytes() == b"\x89PNG\r\n\x1a\nsstv-in-gen"
    assert (dest / "sstv" / ".staging" / "a.png").read_bytes() == b"\x89PNG\r\n\x1a\nsstv-in-gen"
    assert "sstv/a.png" in receipt["files_copied"]
    assert "sstv/.staging/a.png" in receipt["files_copied"]

    # External hardlink (one link inside generation, one outside -> in_generation != nlink)
    # MUST still be refused!
    ext_src = tmp_path / "src_ext_hl"
    ext_src.mkdir(mode=0o700)
    outside = tmp_path / "outside_target.txt"
    outside.write_text("outside", encoding="utf-8")
    os.link(outside, ext_src / "linked.txt")
    with pytest.raises(SnapshotError, match="hardlink"):
        inventory_generation(ext_src)


def test_f5_systemd_effective_settings_dropin_refusals_and_env_hygiene(tmp_path):
    from sigint101.validation import load_systemd_unit_settings

    # 1) Read structured settings from systemctl --user show + show-environment,
    # preserving systemd-expanded %h, C escapes (\x30\x31 -> 01, \t), and session env!
    unit_file = tmp_path / "sigint101.service"
    unit_file.write_text(
        "[Service]\n"
        "ExecStart=/usr/bin/uvicorn sigint101.app:app\n"
        'Environment="SIGINT101_UNIT_ONLY=from-unit"\n',
        encoding="utf-8",
    )
    dropin_file = tmp_path / "10-site.conf"
    dropin_file.write_text(
        "[Service]\n"
        'Environment="SIGINT101_TLE=%h/tle.txt" "SIGINT101_RTLSDR_SERIAL=\\x30\\x31"\n',
        encoding="utf-8",
    )

    expanded_tle = str(tmp_path / "tle.txt")

    def fake_systemctl(args):
        if "show-environment" in args:
            return "LANG=C.UTF-8\nSIGINT101_SESSION_VAR=from-manager\n"
        return (
            f"FragmentPath={unit_file}\n"
            f"DropInPaths={dropin_file}\n"
            "Environment=SIGINT101_UNIT_ONLY=from-unit "
            f'SIGINT101_TLE={expanded_tle} "SIGINT101_RTLSDR_SERIAL=01" '
            '"SIGINT101_TAB=00\t01"\n'
            "EnvironmentFiles=\n"
            "UnsetEnvironment=\n"
            "ExecStart={ path=/usr/bin/uvicorn ; argv[]=/usr/bin/uvicorn sigint101.app:app }\n"
            "WorkingDirectory=\n"
            "DynamicUser=no\n"
            "PrivateTmp=no\n"
            "RootDirectory=\n"
            "ProtectSystem=\n"
        )

    eff = load_systemd_unit_settings("sigint101.service", runner=fake_systemctl)
    assert eff.env["SIGINT101_SESSION_VAR"] == "from-manager"
    assert eff.env["SIGINT101_TLE"] == expanded_tle
    assert eff.env["SIGINT101_RTLSDR_SERIAL"] == "01"
    assert eff.env["SIGINT101_TAB"] == "00\t01"
    assert eff.unit_env == {"SIGINT101_UNIT_ONLY": "from-unit"}

    # 2) Drop-in overriding ExecStart, WorkingDirectory, or RestartSec is REFUSED!
    for bad_dropin in (
        "[Service]\nExecStart=\nExecStart=/usr/bin/env python3 -m http.server\n",
        "[Service]\nWorkingDirectory=/tmp\n",
        "[Service]\nRestartSec=10\n",
    ):
        bad_file = tmp_path / "bad-dropin.conf"
        bad_file.write_text(bad_dropin, encoding="utf-8")

        def bad_runner(args, _bf=bad_file):
            if "show-environment" in args:
                return "LANG=C.UTF-8\n"
            return (
                f"FragmentPath={unit_file}\n"
                f"DropInPaths={_bf}\n"
                "Environment=\nEnvironmentFiles=\nUnsetEnvironment=\n"
                "DynamicUser=no\nPrivateTmp=no\nRootDirectory=\nProtectSystem=\n"
            )

        with pytest.raises(UnitSettingsError, match="drop-in|unsupported"):
            load_systemd_unit_settings("sigint101.service", runner=bad_runner)
        with pytest.raises(UnitSettingsError, match="drop-in|unsupported"):
            parse_systemd_unit_config(unit_file.read_text(encoding="utf-8"), (bad_dropin,))

    # 3) PYTHON* and LD_* are refused in unit/drop-in settings!
    for forbidden_env in ("PYTHONHOME=/x", "LD_PRELOAD=/tmp/x.so", "PYTHONPATH=/tmp/evil"):
        with pytest.raises(UnitSettingsError, match="PYTHON|LD_|forbidden|unsupported"):
            parse_systemd_unit_config(
                "[Service]\nExecStart=/usr/bin/uvicorn sigint101.app:app\n",
                dropin_texts=(f'[Service]\nEnvironment="{forbidden_env}"\n',),
            )

    # 4) Text parser refuses unexpanded % specifiers and backslash escapes rather than guessing!
    for ambiguous in (
        '[Service]\nEnvironment="SIGINT101_TLE=%h/tle.txt"\n',
        '[Service]\nEnvironment="SIGINT101_RTLSDR_SERIAL=\\x30\\x31"\n',
        "[Service]\nEnvironment=SIGINT101_TAB=00\\t01\n",
    ):
        with pytest.raises(UnitSettingsError, match="specifier|escape|systemd"):
            parse_systemd_unit_config(
                "[Service]\nExecStart=/usr/bin/uvicorn sigint101.app:app\n",
                dropin_texts=(ambiguous,),
            )


def test_f6_mount_id_fails_closed_without_proc_and_catches_file_mount(tmp_path):
    from sigint101.snapshot import read_fd_mount_id

    # 1) Unreadable/missing fdinfo MUST raise SnapshotError (never fall back to st_dev).
    probe_file = tmp_path / "f.txt"
    probe_file.write_text("hi", encoding="utf-8")
    fd = os.open(os.fspath(probe_file), os.O_RDONLY)
    try:
        with pytest.raises(SnapshotError, match="mount|fdinfo"):
            read_fd_mount_id(fd, "f.txt", fdinfo_root=tmp_path / "no_proc_fdinfo")
    finally:
        os.close(fd)

    # 2) File-level mount boundary crossing (directory has mount 101, single file has mount 202)
    # MUST be refused by inventory_generation!
    s = tmp_path / "file_mnt_src"
    s.mkdir(mode=0o700)
    (s / "events.jsonl").write_text('{"kind":"ok"}\n', encoding="utf-8")

    def file_only_mount(_fd: int, rel_path: str) -> int:
        return 202 if rel_path == "events.jsonl" else 101

    with pytest.raises(SnapshotError, match="mount"):
        inventory_generation(s, mount_id_reader=file_only_mount)


def test_f7_trial_ro_connections_package_root_r21_post_marker_and_sigkill_reap(tmp_path):
    from sigint101.trial import _stop_and_reap

    # 1) T2: trial opens both SQLite connections read-only (writes fail with OperationalError).
    gen = _make_managed_generation(tmp_path, "gen_f7", generation_id="gen-f7")
    trial_app = create_trial_app(
        pack_dir=PACK_DIR,
        data_dir=gen,
        pool_path=POOL_PATH,
        release_root=REPO_ROOT,
        require_release_manifest=False,
        probe=_absent,
    )
    with TestClient(trial_app) as client:
        assert client.get(TRIAL_READY_PATH).status_code == 200
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            trial_app.state.progress._conn.execute(
                "INSERT INTO progress(lesson_id, opened, completed) VALUES(1, 1, 0)"
            )
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            trial_app.state.exam_store._conn.execute(
                "INSERT INTO srs(question_id, box, last_seen, seen, correct) "
                "VALUES('T1A01', 1, 1.0, 1, 1)"
            )

    # 2) A2: identical release.json in a DIFFERENT directory fails package-root identity check!
    rel1 = _copy_release_tree(tmp_path / "rel_same_1", release_id="rel-same", commit="same123")
    rel2 = _copy_release_tree(tmp_path / "rel_same_2", release_id="rel-same", commit="same123")
    res = run_private_trial(
        python_executable=sys.executable,
        release_root=rel1,
        data_dir=gen,
        expected_release_id="rel-same",
        expected_commit="same123",
        expected_generation_id="gen-f7",
        socket_parent_dir=tmp_path,
    )
    assert res.ready is True, res.errors
    with pytest.raises(ValidationError, match="package root"):
        verify_pair_attestation(
            res.attestation,
            expected_release_id="rel-same",
            expected_commit="same123",
            expected_package_root=rel2,
            expected_generation_id="gen-f7",
            expected_data_dir=gen,
        )

    # 3) R1 (R2-1): after serving marker exists, even a changed unit-owned setting MUST NOT refuse!
    u1 = parse_systemd_unit_config(
        '[Service]\nExecStart=/usr/bin/uvicorn sigint101.app:app\nEnvironment="A=1"\n'
    )
    u2 = parse_systemd_unit_config(
        '[Service]\nExecStart=/usr/bin/uvicorn sigint101.app:app\nEnvironment="A=2"\n'
    )
    fp1 = compute_settings_fingerprint(u1.unit_only())
    with pytest.raises(UnitSettingsError, match="parity|drift"):
        check_unit_settings_parity(
            recorded_fingerprint=fp1, current_settings=u2, serving_marker_exists=False
        )
    assert (
        check_unit_settings_parity(
            recorded_fingerprint=fp1, current_settings=u2, serving_marker_exists=True
        )
        is True
    )

    # 4) F8: unit_only() fingerprint covers ONLY unit env, not ExecStart/RestartSec.
    u1_diff_restart = parse_systemd_unit_config(
        '[Service]\nExecStart=/usr/bin/uvicorn sigint101.app:app\nRestartSec=9\nEnvironment="A=1"\n'
    )
    assert compute_settings_fingerprint(u1_diff_restart.unit_only()) == fp1

    # 5) _stop_and_reap escalates to SIGKILL and reaps a child that ignores SIGTERM!
    ign_code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-B", "-c", ign_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout.readline().strip() == b"ready"
    _stop_and_reap(proc, grace_s=0.3)
    assert proc.poll() == -signal.SIGKILL
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)
    proc.stdout.close()
    proc.stderr.close()


