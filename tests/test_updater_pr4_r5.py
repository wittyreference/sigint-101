# ABOUTME: Fault-injection tests for Updater PR 4 Round 5 findings F1-F4.
# ABOUTME: Covers control __init__ integrity, managed rerun refusal, rollback/recovery, and backups.

from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sigint101 import enrollment as enrollment_mod
from sigint101.enrollment import EnrollmentError, enroll_legacy_fixture
from sigint101.locks import LockBusyError, acquire_mutation_gate
from sigint101.updater import apply_update
from sigint101.updater_cli import main as updater_cli_main
from tests.test_updater_pr4_fixes import REPO_ROOT, _chmod_tree_writable, _MinimalService
from tests.test_updater_pr4_r2 import _make_stub_service_class
from tests.test_updater_pr4_r3 import (
    _build_legacy_box,
    _load_control_manifest_mod,
    host_rt,  # noqa: F401
)


@pytest.fixture(autouse=True)
def _cleanup_ro_tmp(tmp_path: Path):
    yield
    _chmod_tree_writable(tmp_path)


def _enroll_box(box: dict, src_sp: Path, host_py: str, **extra) -> dict:
    kwargs = dict(
        repo_root=box["checkout"],
        updater_root=box["root"],
        legacy_checkout=box["checkout"],
        legacy_data_dir=box["checkout"] / "data",
        legacy_python=sys.executable,
        source_site_packages=src_sp,
        unit_path=box["unit_path"],
        desktop_wrapper_path=box["wrapper_path"],
        home=box["home"],
        runtime_dir=box["xdg"],
        host_python=host_py,
        trusted_remote=str(box["bare"]),
        legacy_commit=box["c1"],
        kiosk_process_lister=lambda: [],
        reload_systemd=lambda: None,
        drain_old_service=lambda: None,
        bench_marker_checker=box["check"],
    )
    kwargs.update(extra)
    return enroll_legacy_fixture(**kwargs)


# --- F1 (blocker): verify generated sigint101/__init__.py bytes; refuse & reclaim if damaged ---


def test_r5_f1_damaged_control_pkg_init_is_refused_reclaimed_and_retry_launches(
    tmp_path, host_rt  # noqa: F811
):
    """F1: damaging sigint101/__init__.py inside a pre-existing control generation must cause
    enrollment to refuse and reclaim the generation before touching any selector or unit; a retry
    stages a clean generation whose dispatcher runs serve --help cleanly."""
    _rt_cached, src_sp, host_py = host_rt
    box = _build_legacy_box(tmp_path, name="f1_damaged_init")

    cm = _load_control_manifest_mod()
    ctrl_dir = box["root"] / "control" / "ctrl-0001"
    ctrl_dir.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(box["root"], 0o700)
    cm.stage_control_generation(box["checkout"], ctrl_dir, control_id="ctrl-0001")

    # Damage the generated sigint101/__init__.py with a syntax error (preserving manifest files).
    init_file = ctrl_dir / "sigint101" / "__init__.py"
    os.chmod(ctrl_dir / "sigint101", 0o700)
    os.chmod(init_file, 0o600)
    init_file.write_text("def broken(:\n", encoding="utf-8")
    os.chmod(init_file, 0o500)
    os.chmod(ctrl_dir / "sigint101", 0o500)

    orig_install = (box["checkout"] / "deploy" / "install.sh").read_bytes()
    orig_provision = (box["checkout"] / "deploy" / "provision.sh").read_bytes()

    with pytest.raises(EnrollmentError, match="control"):
        _enroll_box(box, src_sp, host_py)

    # Damaged generation was reclaimed before changing any selector, shim, or unit.
    assert not ctrl_dir.exists()
    assert not (box["root"] / "control.json").exists()
    assert not (box["root"] / "current.json").exists()
    assert not (box["root"] / "enrollment.json").exists()
    assert box["unit_path"].read_text(encoding="utf-8") == box["orig_unit"]
    assert (box["checkout"] / "deploy" / "install.sh").read_bytes() == orig_install
    assert (box["checkout"] / "deploy" / "provision.sh").read_bytes() == orig_provision

    # Retry stages a clean generation and dispatcher serves cleanly.
    receipt = _enroll_box(box, src_sp, host_py)
    assert receipt["handoff_complete"] is True
    cm.verify_control_generation(ctrl_dir, expected_control_id="ctrl-0001")
    disp = subprocess.run(
        [
            sys.executable,
            "-B",
            str(box["root"] / "bin" / "dispatcher.py"),
            "--root",
            str(box["root"]),
            "serve",
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert disp.returncode == 0, (disp.stdout, disp.stderr)


# --- F2 (high): refuse rerun on a kit with a managed generation already selected ---------------


def test_r5_f2_refuses_rerun_when_managed_generation_selected_under_gate_before_writes(
    tmp_path, host_rt, monkeypatch  # noqa: F811
):
    """F2: once apply_update has selected a managed generation in current.json, rerunning
    enrollment must be refused under the mutation gate BEFORE any write, even if an error is
    injected on enrollment.json, leaving current.json and every file untouched."""
    _rt_cached, src_sp, host_py = host_rt
    box = _build_legacy_box(tmp_path, name="f2_managed_rerun")
    os.chmod(box["checkout"] / "data", 0o700)

    _enroll_box(box, src_sp, host_py)

    svc = _MinimalService(box["root"])
    apply_res = apply_update(
        root=box["root"],
        remote_url=str(box["bare"]),
        pinned_commit=box["c2"],
        service=svc,
        home=box["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=box["check"],
    )
    assert apply_res["status"] == "ready"

    cur_path = box["root"] / "current.json"
    enr_path = box["root"] / "enrollment.json"
    ctrl_path = box["root"] / "control.json"
    cur_before = cur_path.read_bytes()
    enr_before = enr_path.read_bytes()
    ctrl_before = ctrl_path.read_bytes()
    unit_before = box["unit_path"].read_bytes()
    assert json.loads(cur_before.decode("utf-8"))["mode"] == "managed"

    # Part A: check happens under the mutation gate (busy gate refuses with LockBusyError).
    with acquire_mutation_gate(box["root"], blocking=False, home=box["home"]):
        with pytest.raises(LockBusyError):
            _enroll_box(box, src_sp, host_py)

    # Part B: inject ENOSPC on enrollment.json writes and track all writes; rerun refuses BEFORE
    # the first write and leaves current.json in managed mode.
    real_atomic = enrollment_mod._atomic_write_bytes
    writes: list[Path] = []

    def _spy_atomic(path: Path, data: bytes, **kw):
        writes.append(Path(path))
        if Path(path) == enr_path:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_atomic(path, data, **kw)

    monkeypatch.setattr(enrollment_mod, "_atomic_write_bytes", _spy_atomic)
    with pytest.raises(EnrollmentError, match="managed"):
        _enroll_box(box, src_sp, host_py)

    assert writes == [], f"refused managed rerun must perform zero writes, saw: {writes}"
    assert cur_path.read_bytes() == cur_before
    assert enr_path.read_bytes() == enr_before
    assert ctrl_path.read_bytes() == ctrl_before
    assert box["unit_path"].read_bytes() == unit_before


# --- F3 (high): complete rollback / honest reporting, temp cleanup, and durable recovery -------


def test_r5_f3_rename_parent_sync_drain_and_final_record_failures_roll_back_and_recover(
    tmp_path, host_rt, monkeypatch  # noqa: F811
):
    """F3: failures at rename, parent-directory sync, reload/drain, and final completed-record
    write must roll back cleanly (or report rollback_ok=False if any destination remains changed),
    remove unpublished temporary files, and persist durable recovery state across crashes."""
    _rt_cached, src_sp, host_py = host_rt

    # --- 3a: Failure at os.replace (rename) of deploy/provision.sh ---
    box_ren = _build_legacy_box(tmp_path, name="f3_rename_fail")
    install_sh = box_ren["checkout"] / "deploy" / "install.sh"
    provision_sh = box_ren["checkout"] / "deploy" / "provision.sh"
    orig_install = install_sh.read_bytes()
    orig_provision = provision_sh.read_bytes()

    real_replace = os.replace
    armed_ren = {"flag": True}

    def _fail_replace_provision(src, dst):
        if armed_ren["flag"] and Path(dst) == provision_sh:
            armed_ren["flag"] = False
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_replace_provision)
    with pytest.raises(EnrollmentError):
        _enroll_box(box_ren, src_sp, host_py)
    monkeypatch.setattr(os, "replace", real_replace)

    assert install_sh.read_bytes() == orig_install
    assert provision_sh.read_bytes() == orig_provision
    leftover_tmps = [
        p.name for p in (box_ren["checkout"] / "deploy").iterdir() if ".tmp." in p.name
    ]
    assert leftover_tmps == [], f"unpublished temp files left after rename failure: {leftover_tmps}"
    enr_ren = json.loads((box_ren["root"] / "enrollment.json").read_text(encoding="utf-8"))
    assert enr_ren["status"] == "failed"
    assert enr_ren["rollback_ok"] is True

    # Retry in the SAME process must succeed (no EEXIST from leftover temp shim).
    receipt_ren = _enroll_box(box_ren, src_sp, host_py)
    assert receipt_ren["handoff_complete"] is True

    # --- 3b-1: Failure at parent fsync_dir AFTER os.replace of deploy/provision.sh ---
    box_sync_p = _build_legacy_box(tmp_path, name="f3_sync_provision")
    inst_p = box_sync_p["checkout"] / "deploy" / "install.sh"
    prov_p = box_sync_p["checkout"] / "deploy" / "provision.sh"
    orig_inst_p = inst_p.read_bytes()
    orig_prov_p = prov_p.read_bytes()
    shim_prov_bytes = (REPO_ROOT / "deploy" / "managed" / "provision-shim.sh").read_bytes()

    real_fsync_dir = enrollment_mod.fsync_dir
    armed_sync_p = {"flag": True}

    def _fail_sync_after_prov_replace(path: Path):
        if (
            armed_sync_p["flag"]
            and Path(path) == prov_p.parent
            and prov_p.read_bytes() == shim_prov_bytes
        ):
            armed_sync_p["flag"] = False
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_fsync_dir(path)

    monkeypatch.setattr(enrollment_mod, "fsync_dir", _fail_sync_after_prov_replace)
    with pytest.raises(EnrollmentError):
        _enroll_box(box_sync_p, src_sp, host_py)
    monkeypatch.setattr(enrollment_mod, "fsync_dir", real_fsync_dir)

    assert inst_p.read_bytes() == orig_inst_p
    assert prov_p.read_bytes() == orig_prov_p, (
        "provision.sh stayed changed after parent-sync ENOSPC"
    )
    enr_sp = json.loads((box_sync_p["root"] / "enrollment.json").read_text(encoding="utf-8"))
    assert enr_sp["status"] == "failed"
    assert enr_sp["rollback_ok"] is True

    # --- 3b-2: Failure at parent fsync_dir AFTER os.replace of unit_file ---
    box_sync_u = _build_legacy_box(tmp_path, name="f3_sync_unit")
    inst_u = box_sync_u["checkout"] / "deploy" / "install.sh"
    prov_u = box_sync_u["checkout"] / "deploy" / "provision.sh"
    orig_inst_u = inst_u.read_bytes()
    orig_prov_u = prov_u.read_bytes()
    reloads_u = {"count": 0}
    armed_sync_u = {"flag": True}

    def _fail_sync_after_unit_replace(path: Path):
        if (
            armed_sync_u["flag"]
            and Path(path) == box_sync_u["unit_path"].parent
            and b"dispatcher.py" in box_sync_u["unit_path"].read_bytes()
        ):
            armed_sync_u["flag"] = False
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_fsync_dir(path)

    monkeypatch.setattr(enrollment_mod, "fsync_dir", _fail_sync_after_unit_replace)
    with pytest.raises(EnrollmentError):
        _enroll_box(
            box_sync_u,
            src_sp,
            host_py,
            reload_systemd=lambda: reloads_u.__setitem__("count", reloads_u["count"] + 1),
        )
    monkeypatch.setattr(enrollment_mod, "fsync_dir", real_fsync_dir)

    assert box_sync_u["unit_path"].read_text(encoding="utf-8") == box_sync_u["orig_unit"]
    assert inst_u.read_bytes() == orig_inst_u
    assert prov_u.read_bytes() == orig_prov_u
    assert reloads_u["count"] >= 1
    enr_su = json.loads((box_sync_u["root"] / "enrollment.json").read_text(encoding="utf-8"))
    assert enr_su["rollback_ok"] is True

    # --- 3b-3: Persistent failure leaving a destination changed MUST report rollback_ok=False ---
    box_bad_rb = _build_legacy_box(tmp_path, name="f3_incomplete_rollback")
    prov_bad = box_bad_rb["checkout"] / "deploy" / "provision.sh"
    real_restore = enrollment_mod._restore_file_best_effort
    armed_bad = {"flag": True}

    def _fail_sync_then_broken_restore(path: Path):
        if (
            armed_bad["flag"]
            and Path(path) == prov_bad.parent
            and prov_bad.read_bytes() == shim_prov_bytes
        ):
            armed_bad["flag"] = False
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_fsync_dir(path)

    monkeypatch.setattr(enrollment_mod, "fsync_dir", _fail_sync_then_broken_restore)
    monkeypatch.setattr(
        enrollment_mod,
        "_restore_file_best_effort",
        lambda p, d, mode: False if Path(p) == prov_bad else real_restore(p, d, mode=mode),
    )
    with pytest.raises(EnrollmentError, match="incomplete"):
        _enroll_box(box_bad_rb, src_sp, host_py)
    monkeypatch.setattr(enrollment_mod, "fsync_dir", real_fsync_dir)
    monkeypatch.setattr(enrollment_mod, "_restore_file_best_effort", real_restore)

    assert prov_bad.read_bytes() == shim_prov_bytes
    enr_bad = json.loads((box_bad_rb["root"] / "enrollment.json").read_text(encoding="utf-8"))
    assert enr_bad["status"] == "failed"
    assert enr_bad["rollback_ok"] is False
    assert (box_bad_rb["root"] / "enrollment-recovery.json").is_file()

    # --- 3c: Failure at drain_old_service AND failure at final completed enrollment.json write ---
    box_drain = _build_legacy_box(tmp_path, name="f3_drain_fail")
    inst_d = box_drain["checkout"] / "deploy" / "install.sh"
    prov_d = box_drain["checkout"] / "deploy" / "provision.sh"
    orig_inst_d = inst_d.read_bytes()
    orig_prov_d = prov_d.read_bytes()

    with pytest.raises(EnrollmentError):
        _enroll_box(
            box_drain,
            src_sp,
            host_py,
            drain_old_service=lambda: (_ for _ in ()).throw(RuntimeError("drain timed out")),
        )
    assert box_drain["unit_path"].read_text(encoding="utf-8") == box_drain["orig_unit"]
    assert inst_d.read_bytes() == orig_inst_d
    assert prov_d.read_bytes() == orig_prov_d
    enr_d = json.loads((box_drain["root"] / "enrollment.json").read_text(encoding="utf-8"))
    assert enr_d["status"] == "failed"
    assert enr_d["rollback_ok"] is True

    box_final = _build_legacy_box(tmp_path, name="f3_final_record_fail")
    inst_f = box_final["checkout"] / "deploy" / "install.sh"
    prov_f = box_final["checkout"] / "deploy" / "provision.sh"
    orig_inst_f = inst_f.read_bytes()
    orig_prov_f = prov_f.read_bytes()
    enr_f_path = box_final["root"] / "enrollment.json"
    real_atomic = enrollment_mod._atomic_write_bytes
    enr_writes = {"count": 0}

    def _fail_second_enrollment_write(path: Path, data: bytes, **kw):
        if Path(path) == enr_f_path:
            enr_writes["count"] += 1
            if enr_writes["count"] == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
        return real_atomic(path, data, **kw)

    monkeypatch.setattr(enrollment_mod, "_atomic_write_bytes", _fail_second_enrollment_write)
    with pytest.raises(EnrollmentError):
        _enroll_box(box_final, src_sp, host_py)
    monkeypatch.setattr(enrollment_mod, "_atomic_write_bytes", real_atomic)

    assert box_final["unit_path"].read_text(encoding="utf-8") == box_final["orig_unit"]
    assert inst_f.read_bytes() == orig_inst_f
    assert prov_f.read_bytes() == orig_prov_f
    enr_f = json.loads(enr_f_path.read_text(encoding="utf-8"))
    assert enr_f["status"] == "failed"
    assert enr_f["rollback_ok"] is True

    # --- 3d: Durable recovery state after an unrecovered crash (e.g. SIGKILL mid-handoff) ---
    box_crash = _build_legacy_box(tmp_path, name="f3_crash_durable")
    inst_c = box_crash["checkout"] / "deploy" / "install.sh"
    prov_c = box_crash["checkout"] / "deploy" / "provision.sh"
    orig_inst_c = inst_c.read_bytes()
    orig_prov_c = prov_c.read_bytes()

    class _SimulatedKill(BaseException):
        pass

    def _crash_after_unit(stage: str):
        if stage == "after_unit_install_before_drain":
            raise _SimulatedKill("simulated SIGKILL")

    with pytest.raises(_SimulatedKill):
        _enroll_box(box_crash, src_sp, host_py, on_boundary=_crash_after_unit)

    # Plant an orphaned unpublished temp file as well.
    orphan_tmp = box_crash["checkout"] / "deploy" / ".provision.sh.tmp.99999.deadbeef"
    orphan_tmp.write_bytes(b"partial")

    rec_path = box_crash["root"] / "enrollment-recovery.json"
    assert rec_path.is_file(), "durable recovery state must exist while destinations are in flight"

    # Recovering restores every destination + removes orphan temp + clears recovery journal.
    reloads_c = {"count": 0}
    rec_res = enrollment_mod.recover_interrupted_enrollment(
        box_crash["root"],
        reload_systemd=lambda: reloads_c.__setitem__("count", reloads_c["count"] + 1),
    )
    assert rec_res["recovered"] is True
    assert not orphan_tmp.exists()
    assert not rec_path.exists()
    assert box_crash["unit_path"].read_text(encoding="utf-8") == box_crash["orig_unit"]
    assert inst_c.read_bytes() == orig_inst_c
    assert prov_c.read_bytes() == orig_prov_c
    assert reloads_c["count"] == 1

    # Retry now completes cleanly.
    done_c = _enroll_box(box_crash, src_sp, host_py)
    assert done_c["handoff_complete"] is True
    assert not rec_path.exists()


# --- F4 (medium): saved launcher backup always matches recorded digest (digest-keyed) ----------


def test_r5_f4_launcher_backup_always_matches_recorded_digest_and_preserves_both(
    tmp_path, host_rt, capsys  # noqa: F811
):
    """F4: if an interrupted enrollment saved an earlier reviewed launcher and the operator later
    puts 3004a03 in the live entry before retrying, the backup recorded for 3004a03 MUST contain
    3004a03 bytes (while also keeping the earlier digest-keyed backup), and a post-activation rerun
    with enrollment.json removed MUST still recover 3004a03, never reverting to the earlier one."""
    _rt_cached, src_sp, host_py = host_rt

    launcher_earlier = (REPO_ROOT / "deploy" / "kiosk" / "start-kiosk.sh").read_bytes()
    sha_earlier = hashlib.sha256(launcher_earlier).hexdigest()
    launcher_3004 = (
        REPO_ROOT / "tests" / "fixtures" / "kiosk" / "start-kiosk-3004a03.sh"
    ).read_bytes()
    sha_3004 = hashlib.sha256(launcher_3004).hexdigest()
    managed_bytes = (
        REPO_ROOT / "deploy" / "managed" / "sigint101-course-managed.sh"
    ).read_bytes()
    assert sha_earlier != sha_3004

    box = _build_legacy_box(tmp_path, name="f4_two_launchers")
    box["wrapper_path"].write_bytes(launcher_earlier)
    os.chmod(box["wrapper_path"], 0o755)

    # 1) Interrupted enrollment while live entry holds the earlier launcher -> saves sha_earlier.
    with pytest.raises((EnrollmentError, RuntimeError)):
        _enroll_box(
            box,
            src_sp,
            host_py,
            on_boundary=lambda st: (
                (_ for _ in ()).throw(RuntimeError("interrupted"))
                if st == "before_unit_install"
                else None
            ),
        )

    # 2) Operator puts 3004a03 in the live desktop entry, stages .managed in read-only dir, retries.
    box["wrapper_path"].write_bytes(launcher_3004)
    os.chmod(box["wrapper_path"], 0o755)
    staged_path = box["wrapper_path"].with_name(f"{box['wrapper_path'].name}.managed")
    staged_path.write_bytes(managed_bytes)
    os.chmod(staged_path, 0o755)
    os.chmod(box["wrapper_dir"], 0o555)

    argv = [
        "--root",
        str(box["root"]),
        "--enroll",
        "--checkout",
        str(box["checkout"]),
        "--remote",
        str(box["bare"]),
        "--host-python",
        host_py,
        "--site-packages",
        str(src_sp),
        "--unit-path",
        str(box["unit_path"]),
        "--desktop-wrapper",
        str(box["wrapper_path"]),
        "--runtime-dir",
        str(box["xdg"]),
    ]
    try:
        rc1 = updater_cli_main(
            argv,
            _bench_marker_checker=box["check"],
            _service_factory=lambda *a: _make_stub_service_class()(),
            _home=box["home"],
            _kiosk_process_lister=lambda: [],
        )
        capsys.readouterr()
        assert rc1 == 0

        enr1 = json.loads((box["root"] / "enrollment.json").read_text(encoding="utf-8"))
        dw1 = enr1["desktop_wrapper"]
        assert dw1["original_sha256"] == sha_3004
        assert dw1["original_reviewed"] is True
        backup_path = Path(dw1["original_backup_path"])
        assert backup_path.is_file()
        assert hashlib.sha256(backup_path.read_bytes()).hexdigest() == sha_3004, (
            "recorded backup bytes must match the digest of the launcher replaced"
        )
        # Digest-keyed backups keep BOTH versions!
        originals_dir = box["root"] / "originals"
        earlier_backup = originals_dir / f"desktop-wrapper-{sha_earlier}.sh"
        assert earlier_backup.is_file()
        assert hashlib.sha256(earlier_backup.read_bytes()).hexdigest() == sha_earlier

        # 3) Activate managed wrapper, remove enrollment.json, and rerun -> MUST keep 3004a03!
        os.chmod(box["wrapper_dir"], 0o755)
        os.replace(staged_path, box["wrapper_path"])
        os.chmod(box["wrapper_dir"], 0o555)
        (box["root"] / "enrollment.json").unlink()

        rc2 = updater_cli_main(
            argv,
            _bench_marker_checker=box["check"],
            _service_factory=lambda *a: _make_stub_service_class()(),
            _home=box["home"],
            _kiosk_process_lister=lambda: [],
        )
        capsys.readouterr()
        assert rc2 == 0

        enr2 = json.loads((box["root"] / "enrollment.json").read_text(encoding="utf-8"))
        dw2 = enr2["desktop_wrapper"]
        assert dw2["original_sha256"] == sha_3004, (
            "rerun after activation must not revert to earlier interrupted launcher's digest"
        )
        assert dw2["original_reviewed"] is True
        backup2 = Path(dw2["original_backup_path"])
        assert hashlib.sha256(backup2.read_bytes()).hexdigest() == sha_3004
    finally:
        os.chmod(box["wrapper_dir"], 0o755)
