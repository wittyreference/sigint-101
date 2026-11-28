# ABOUTME: Fault-injection tests for Updater PR 4 Round 6 findings F1 and F3a-F3c.
# ABOUTME: Checks dispatcher verify, reload retry, mode/owner verify, and journal retirement.

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from sigint101 import enrollment as enrollment_mod
from sigint101.enrollment import (
    ENROLLMENT_RECOVERY_FILENAME,
    EnrollmentError,
    enroll_legacy_fixture,
    recover_interrupted_enrollment,
)
from sigint101.updater import recover as updater_recover
from tests.test_updater_pr4_fixes import REPO_ROOT, _chmod_tree_writable
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


class _FakeServiceManager:
    """Fake service manager tracking the exact unit file text loaded on daemon_reload()."""

    def __init__(self, unit_path: Path, *, fail_reload: bool = False):
        self.unit_path = unit_path
        self.fail_reload = fail_reload
        self.reload_calls = 0
        self.loaded_unit = unit_path.read_text(encoding="utf-8") if unit_path.exists() else ""

    def daemon_reload(self) -> None:
        self.reload_calls += 1
        if self.fail_reload:
            raise RuntimeError("injected daemon-reload failure")
        self.loaded_unit = (
            self.unit_path.read_text(encoding="utf-8") if self.unit_path.exists() else ""
        )

    def unit_state(self) -> tuple[str, str, int]:
        return ("active", "running", 4242)

    def is_listener_active(self) -> bool:
        return True

    def is_http_ready(self) -> bool:
        return True


# --- F1 (blocker): trusted dispatcher verifies selected generation before BOTH exec paths ------


def test_r6_f1_dispatcher_verifies_selected_control_before_exec_on_both_paths(
    tmp_path, host_rt  # noqa: F811
):
    """F1: damaging the already-selected generation's sigint101/__init__.py with a sentinel must
    make both `serve` and `status` refuse from trusted dispatcher code before the sentinel runs."""
    _rt_cached, src_sp, host_py = host_rt
    box = _build_legacy_box(tmp_path, name="r6_f1_dispatcher")
    receipt = _enroll_box(box, src_sp, host_py)
    assert receipt["handoff_complete"] is True

    dispatcher = box["root"] / "bin" / "dispatcher.py"
    ctrl_dir = box["root"] / "control" / "ctrl-0001"

    # Baseline: both serve --help and status --help succeed on the clean selected generation.
    for cmd in ("serve", "status"):
        ok = subprocess.run(
            [sys.executable, "-B", str(dispatcher), "--root", str(box["root"]), cmd, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert ok.returncode == 0, (cmd, ok.stdout, ok.stderr)

    # Damage ONLY the already-selected generation's sigint101/__init__.py with a sentinel that
    # prints and exits 26, leaving control_manifest.json untouched.
    init_file = ctrl_dir / "sigint101" / "__init__.py"
    sentinel = (
        "import sys\n"
        "print('SENTINEL_MARKER_EXECUTED', flush=True)\n"
        "sys.exit(26)\n"
    )
    os.chmod(ctrl_dir / "sigint101", 0o700)
    os.chmod(init_file, 0o600)
    init_file.write_text(sentinel, encoding="utf-8")
    os.chmod(init_file, 0o500)
    os.chmod(ctrl_dir / "sigint101", 0o500)

    for cmd in ("serve", "status"):
        res = subprocess.run(
            [sys.executable, "-B", str(dispatcher), "--root", str(box["root"]), cmd, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert res.returncode != 0, (cmd, res.stdout, res.stderr)
        assert res.returncode != 26, f"sentinel executed on {cmd}: {res.stdout!r} {res.stderr!r}"
        assert "SENTINEL_MARKER_EXECUTED" not in res.stdout
        assert "SENTINEL_MARKER_EXECUTED" not in res.stderr

    # Restore canonical __init__.py and instead tamper an allowlisted module (updater_cli.py):
    # both serve and status must ALSO refuse before exec!
    cm = _load_control_manifest_mod()
    os.chmod(ctrl_dir / "sigint101", 0o700)
    os.chmod(init_file, 0o600)
    init_file.write_bytes(cm.CONTROL_PKG_INIT_BYTES)
    os.chmod(init_file, 0o500)

    cli_file = ctrl_dir / "sigint101" / "updater_cli.py"
    os.chmod(cli_file, 0o600)
    cli_file.write_text(sentinel, encoding="utf-8")
    os.chmod(cli_file, 0o500)
    os.chmod(ctrl_dir / "sigint101", 0o500)

    for cmd in ("serve", "status"):
        res = subprocess.run(
            [sys.executable, "-B", str(dispatcher), "--root", str(box["root"]), cmd, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert res.returncode != 0, (cmd, res.stdout, res.stderr)
        assert res.returncode != 26, f"tampered module ran on {cmd}: {res.stdout!r} {res.stderr!r}"
        assert "SENTINEL_MARKER_EXECUTED" not in res.stdout
        assert "SENTINEL_MARKER_EXECUTED" not in res.stderr

    # Trusted dispatcher constants stay in exact sync with control_manifest.py.
    import importlib.util

    disp_spec = importlib.util.spec_from_file_location("_disp_r6", dispatcher)
    assert disp_spec is not None and disp_spec.loader is not None
    disp_mod = importlib.util.module_from_spec(disp_spec)
    disp_spec.loader.exec_module(disp_mod)
    assert disp_mod.CONTROL_ALLOWLIST == cm.CONTROL_ALLOWLIST
    assert disp_mod.CONTROL_PKG_INIT_BYTES == cm.CONTROL_PKG_INIT_BYTES
    assert disp_mod.CONTROL_MANIFEST_FILENAME == cm.CONTROL_MANIFEST_FILENAME
    assert disp_mod.CONTROL_PKG_INIT_REL == cm.CONTROL_PKG_INIT_REL


# --- F3a (blocker): unconfirmed unit reload is kept durably and retried until confirmed --------


def test_r6_f3a_unconfirmed_unit_reload_is_recorded_durably_and_retried_until_confirmed(
    tmp_path, host_rt  # noqa: F811
):
    """F3a: a failed daemon-reload on recovery #1 must be persisted durably and retried on
    recovery #2 (both direct recover_interrupted_enrollment and updater.recover), even though
    the unit file on disk already matches the original."""
    _rt_cached, src_sp, host_py = host_rt

    # Case A: two consecutive direct recover_interrupted_enrollment calls.
    box1 = _build_legacy_box(tmp_path, name="r6_f3a_direct")
    mgr1 = _FakeServiceManager(box1["unit_path"])

    class _Crash(BaseException):
        pass

    def _crash_after_unit_load(stage: str):
        if stage == "after_unit_install_before_drain":
            raise _Crash("interrupted right after unit install and reload")

    with pytest.raises(_Crash):
        _enroll_box(
            box1,
            src_sp,
            host_py,
            reload_systemd=mgr1.daemon_reload,
            on_boundary=_crash_after_unit_load,
        )

    assert "dispatcher.py" in mgr1.loaded_unit
    rec_path1 = box1["root"] / ENROLLMENT_RECOVERY_FILENAME
    assert rec_path1.is_file()

    # Recovery #1: restores the original unit file on disk, then fails daemon-reload.
    mgr1.fail_reload = True
    with pytest.raises(EnrollmentError, match="reload"):
        recover_interrupted_enrollment(box1["root"], reload_systemd=mgr1.daemon_reload)

    assert box1["unit_path"].read_text(encoding="utf-8") == box1["orig_unit"]
    assert "dispatcher.py" in mgr1.loaded_unit
    assert rec_path1.is_file()
    rec_data = json.loads(rec_path1.read_text(encoding="utf-8"))
    assert rec_data.get("unit_reload_pending") is True

    # Recovery #2: unit file ALREADY matches on disk, yet reload MUST still run and load the
    # original unit before clearing the journal!
    mgr1.fail_reload = False
    res2 = recover_interrupted_enrollment(box1["root"], reload_systemd=mgr1.daemon_reload)
    assert res2["recovered"] is True
    assert mgr1.loaded_unit == box1["orig_unit"]
    assert not rec_path1.exists()

    # Case B: public updater.recover must NOT return `ready` while reload is unconfirmed,
    # and MUST reload the original unit on retry before returning `ready`.
    box2 = _build_legacy_box(tmp_path, name="r6_f3a_updater")
    mgr2 = _FakeServiceManager(box2["unit_path"])
    with pytest.raises(_Crash):
        _enroll_box(
            box2,
            src_sp,
            host_py,
            reload_systemd=mgr2.daemon_reload,
            on_boundary=_crash_after_unit_load,
        )
    assert "dispatcher.py" in mgr2.loaded_unit
    rec_path2 = box2["root"] / ENROLLMENT_RECOVERY_FILENAME

    mgr2.fail_reload = True
    with pytest.raises(EnrollmentError, match="reload"):
        updater_recover(
            root=box2["root"],
            service=mgr2,
            home=box2["home"],
            bench_marker_checker=box2["check"],
        )
    assert rec_path2.is_file()
    assert "dispatcher.py" in mgr2.loaded_unit

    # Still failing on second updater.recover: must STILL refuse, never return `ready`!
    with pytest.raises(EnrollmentError, match="reload"):
        updater_recover(
            root=box2["root"],
            service=mgr2,
            home=box2["home"],
            bench_marker_checker=box2["check"],
        )
    assert rec_path2.is_file()
    assert "dispatcher.py" in mgr2.loaded_unit

    # Once reload succeeds, updater.recover reloads the original unit, clears journal, and is ready.
    mgr2.fail_reload = False
    ready = updater_recover(
        root=box2["root"],
        service=mgr2,
        home=box2["home"],
        bench_marker_checker=box2["check"],
    )
    assert ready["status"] == "ready"
    assert mgr2.loaded_unit == box2["orig_unit"]
    assert not rec_path2.exists()


# --- F3b (fix): a restored file checks bytes, mode, AND ownership ------------------------------


def test_r6_f3b_recovery_verifies_bytes_mode_and_ownership_before_clearing_journal(
    tmp_path, host_rt, monkeypatch  # noqa: F811
):
    """F3b: recovery checks bytes, mode, and ownership; a file whose bytes match but whose mode
    or ownership drifted must never be accepted as restored."""
    _rt_cached, src_sp, host_py = host_rt

    # Case A: provision.sh starts at mode 0750; fallback recovery hits ENOSPC on temp write +
    # EIO on fchmod, leaving original bytes at 0755; retry MUST restore 0750 before clearing.
    box = _build_legacy_box(tmp_path, name="r6_f3b_mode")
    prov = box["checkout"] / "deploy" / "provision.sh"
    orig_prov_bytes = prov.read_bytes()
    os.chmod(prov, 0o750)

    class _Crash(BaseException):
        pass

    with pytest.raises(_Crash):
        _enroll_box(
            box,
            src_sp,
            host_py,
            on_boundary=lambda st: (_ for _ in ()).throw(_Crash())
            if st == "after_unit_install_before_drain"
            else None,
        )
    assert (os.lstat(prov).st_mode & 0o7777) == 0o755
    rec_path = box["root"] / ENROLLMENT_RECOVERY_FILENAME

    real_atomic = enrollment_mod._atomic_write_bytes
    real_fchmod = os.fchmod

    def _enospc_prov(path: Path, data: bytes, **kw):
        if Path(path) == prov:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_atomic(path, data, **kw)

    def _eio_fchmod(fd: int, mode: int):
        if mode == 0o750:
            raise OSError(errno.EIO, "fchmod failed")
        return real_fchmod(fd, mode)

    monkeypatch.setattr(enrollment_mod, "_atomic_write_bytes", _enospc_prov)
    monkeypatch.setattr(os, "fchmod", _eio_fchmod)

    with pytest.raises(EnrollmentError, match="provision.sh"):
        recover_interrupted_enrollment(box["root"], reload_systemd=lambda: None)

    assert prov.read_bytes() == orig_prov_bytes
    assert (os.lstat(prov).st_mode & 0o7777) == 0o755
    assert rec_path.is_file()

    # Even if bytes match at 0644 (non-executable) while fchmod still fails, recovery MUST refuse.
    os.chmod(prov, 0o644)
    with pytest.raises(EnrollmentError, match="mode"):
        recover_interrupted_enrollment(box["root"], reload_systemd=lambda: None)
    assert rec_path.is_file()

    # Clear injected faults: retry restores mode 0750 and clears the journal.
    monkeypatch.setattr(enrollment_mod, "_atomic_write_bytes", real_atomic)
    monkeypatch.setattr(os, "fchmod", real_fchmod)
    ok = recover_interrupted_enrollment(box["root"], reload_systemd=lambda: None)
    assert ok["recovered"] is True
    assert prov.read_bytes() == orig_prov_bytes
    assert (os.lstat(prov).st_mode & 0o7777) == 0o750
    assert not rec_path.exists()

    # Case B: ownership mismatch (recorded original_uid/gid differs from file on disk and cannot
    # be restored): recovery MUST report the ownership difference and keep the journal.
    box_own = _build_legacy_box(tmp_path, name="r6_f3b_owner")
    prov_own = box_own["checkout"] / "deploy" / "provision.sh"
    with pytest.raises(_Crash):
        _enroll_box(
            box_own,
            src_sp,
            host_py,
            on_boundary=lambda st: (_ for _ in ()).throw(_Crash())
            if st == "after_unit_install_before_drain"
            else None,
        )
    rec_own = box_own["root"] / ENROLLMENT_RECOVERY_FILENAME
    rec_obj = json.loads(rec_own.read_text(encoding="utf-8"))
    found_prov = False
    for entry in rec_obj["destinations"]:
        if entry.get("label") == "deploy/provision.sh":
            assert isinstance(entry.get("original_uid"), int)
            assert isinstance(entry.get("original_gid"), int)
            entry["original_uid"] = os.geteuid() + 1
            found_prov = True
    assert found_prov
    rec_own.write_text(json.dumps(rec_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(EnrollmentError, match="uid|owner"):
        recover_interrupted_enrollment(box_own["root"], reload_systemd=lambda: None)
    assert rec_own.is_file()
    assert stat.S_ISREG(os.lstat(prov_own).st_mode)


# --- F3c (fix): atomic journal retirement & leftover completed journal never undoes handover ---


def test_r6_f3c_completed_handover_retires_journal_and_leftover_never_undoes_handover(
    tmp_path, host_rt, monkeypatch  # noqa: F811
):
    """F3c: journal-unlink failure after commit and child os._exit on both sides of the commit
    point must preserve a completed handover and still recover an uncommitted or newer attempt."""
    _rt_cached, src_sp, host_py = host_rt

    # Case A: inject an error ONLY on unlinking enrollment-recovery.json after commit.
    box_a = _build_legacy_box(tmp_path, name="r6_f3c_unlink")
    rec_a = box_a["root"] / ENROLLMENT_RECOVERY_FILENAME
    real_unlink = Path.unlink

    def _fail_rec_unlink(self: Path, missing_ok: bool = False):
        if self == rec_a:
            raise OSError(errno.EIO, "injected unlink error on recovery journal")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _fail_rec_unlink)
    res_a = _enroll_box(box_a, src_sp, host_py)
    assert res_a["handoff_complete"] is True
    assert rec_a.is_file()

    managed_unit_a = box_a["unit_path"].read_text(encoding="utf-8")
    managed_prov_a = (box_a["checkout"] / "deploy" / "provision.sh").read_bytes()
    assert "dispatcher.py" in managed_unit_a

    # Restore unlink: recovery MUST retire the leftover completed journal WITHOUT restoring legacy
    # unit or shims!
    monkeypatch.setattr(Path, "unlink", real_unlink)
    mgr_a = _FakeServiceManager(box_a["unit_path"])
    rec_res = recover_interrupted_enrollment(box_a["root"], reload_systemd=mgr_a.daemon_reload)
    assert rec_res.get("retired_completed_journal") is True
    assert rec_res["restored"] == []
    assert mgr_a.reload_calls == 0
    assert not rec_a.exists()
    assert box_a["unit_path"].read_text(encoding="utf-8") == managed_unit_a
    assert (box_a["checkout"] / "deploy" / "provision.sh").read_bytes() == managed_prov_a

    # Case B: real child process os._exit(99) on BOTH sides of the commit point.
    helper_script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from sigint101.enrollment import enroll_legacy_fixture\n"
        "root, checkout, unit_p, wrap_p, home, xdg, src_sp, host_py, bare, c1, stop_at = (\n"
        "    sys.argv[2:13]\n"
        ")\n"
        "def on_b(stage):\n"
        "    if stage == stop_at:\n"
        "        os._exit(99)\n"
        "enroll_legacy_fixture(\n"
        "    repo_root=checkout, updater_root=root, legacy_checkout=checkout,\n"
        "    legacy_data_dir=Path(checkout) / 'data', legacy_python=sys.executable,\n"
        "    source_site_packages=src_sp, unit_path=unit_p, desktop_wrapper_path=wrap_p,\n"
        "    home=home, runtime_dir=xdg, host_python=host_py, trusted_remote=bare,\n"
        "    legacy_commit=c1, kiosk_process_lister=lambda: [],\n"
        "    reload_systemd=lambda: None, drain_old_service=lambda: None,\n"
        "    on_boundary=on_b, bench_marker_checker=lambda _r: None,\n"
        ")\n"
    )

    # B1: os._exit BEFORE the commit point -> recovery MUST restore legacy unit & shims.
    box_pre = _build_legacy_box(tmp_path, name="r6_f3c_pre_commit")
    orig_prov_pre = (box_pre["checkout"] / "deploy" / "provision.sh").read_bytes()
    proc_pre = subprocess.run(
        [
            sys.executable, "-c", helper_script, str(REPO_ROOT),
            str(box_pre["root"]), str(box_pre["checkout"]), str(box_pre["unit_path"]),
            str(box_pre["wrapper_path"]), str(box_pre["home"]), str(box_pre["xdg"]),
            str(src_sp), host_py, str(box_pre["bare"]), box_pre["c1"],
            "before_completed_record",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc_pre.returncode == 99, (proc_pre.stdout, proc_pre.stderr)
    assert (box_pre["root"] / ENROLLMENT_RECOVERY_FILENAME).is_file()
    assert "dispatcher.py" in box_pre["unit_path"].read_text(encoding="utf-8")

    mgr_pre = _FakeServiceManager(box_pre["unit_path"])
    r_pre = recover_interrupted_enrollment(box_pre["root"], reload_systemd=mgr_pre.daemon_reload)
    assert r_pre["recovered"] is True
    assert box_pre["unit_path"].read_text(encoding="utf-8") == box_pre["orig_unit"]
    assert (box_pre["checkout"] / "deploy" / "provision.sh").read_bytes() == orig_prov_pre
    assert mgr_pre.loaded_unit == box_pre["orig_unit"]
    assert not (box_pre["root"] / ENROLLMENT_RECOVERY_FILENAME).exists()

    # B2: os._exit AFTER the commit point (before journal unlink) -> recovery MUST retire the
    # leftover journal WITHOUT undoing the completed handover!
    box_post = _build_legacy_box(tmp_path, name="r6_f3c_post_commit")
    proc_post = subprocess.run(
        [
            sys.executable, "-c", helper_script, str(REPO_ROOT),
            str(box_post["root"]), str(box_post["checkout"]), str(box_post["unit_path"]),
            str(box_post["wrapper_path"]), str(box_post["home"]), str(box_post["xdg"]),
            str(src_sp), host_py, str(box_post["bare"]), box_post["c1"],
            "after_completed_record_before_journal_unlink",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc_post.returncode == 99, (proc_post.stdout, proc_post.stderr)
    rec_post = box_post["root"] / ENROLLMENT_RECOVERY_FILENAME
    assert rec_post.is_file()
    managed_unit_post = box_post["unit_path"].read_text(encoding="utf-8")
    managed_prov_post = (box_post["checkout"] / "deploy" / "provision.sh").read_bytes()
    assert "dispatcher.py" in managed_unit_post

    mgr_post = _FakeServiceManager(box_post["unit_path"])
    r_post = recover_interrupted_enrollment(box_post["root"], reload_systemd=mgr_post.daemon_reload)
    assert r_post.get("retired_completed_journal") is True
    assert r_post["restored"] == []
    assert mgr_post.reload_calls == 0
    assert not rec_post.exists()
    assert box_post["unit_path"].read_text(encoding="utf-8") == managed_unit_post
    assert (box_post["checkout"] / "deploy" / "provision.sh").read_bytes() == managed_prov_post

    # Case C: a journal for a NEWER interrupted attempt (different attempt_id from the earlier
    # completed enrollment.json) MUST still restore its own destinations!
    enr_post = json.loads((box_post["root"] / "enrollment.json").read_text(encoding="utf-8"))
    assert enr_post["status"] == "complete"
    assert isinstance(enr_post.get("attempt_id"), str) and enr_post["attempt_id"]
    orig_unit_backup = box_post["root"] / "originals" / "sigint101.service"
    orig_unit_bytes = orig_unit_backup.read_bytes()
    newer_journal = {
        "format_version": 1,
        "operation_id": "enroll-0002",
        "attempt_id": "enroll-0002-newer-interrupted",
        "status": "in_progress",
        "unit_reload_pending": True,
        "destinations": [
            {
                "label": "unit_install",
                "path": str(box_post["unit_path"]),
                "existed": True,
                "original_sha256": __import__("hashlib").sha256(orig_unit_bytes).hexdigest(),
                "original_mode": os.lstat(box_post["unit_path"]).st_mode & 0o7777,
                "original_uid": os.geteuid(),
                "original_gid": os.getegid(),
                "backup_path": str(orig_unit_backup),
            }
        ],
    }
    rec_post.write_text(
        json.dumps(newer_journal, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    r_newer = recover_interrupted_enrollment(
        box_post["root"], reload_systemd=mgr_post.daemon_reload
    )
    assert r_newer["recovered"] is True
    assert box_post["unit_path"].read_bytes() == orig_unit_bytes
    assert mgr_post.reload_calls == 1
    assert not rec_post.exists()
