# ABOUTME: Fault-injection tests for Updater PR 4 Round 3 findings F1-F5.
# ABOUTME: Covers control staging, wrapper staging, shim preflight, originals, and reclaim.

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import pytest

from sigint101.enrollment import EnrollmentError, enroll_legacy_fixture
from sigint101.locks import EXIT_GATE_BUSY, acquire_mutation_gate
from sigint101.runtime import probe_python_abi, stage_sealed_runtime
from sigint101.updater import apply_update
from sigint101.updater_cli import main as updater_cli_main
from tests.test_updater_pr4_fixes import (
    _RUNTIME_TOP_ENTRIES,
    REPO_ROOT,
    _build_origin,
    _chmod_tree_writable,
    _enroll_real_box,
    _git,
    _make_marker,
    _MinimalService,
)
from tests.test_updater_pr4_r2 import _make_stub_service_class


@pytest.fixture(scope="module")
def host_rt(tmp_path_factory):
    """Stage a sealed runtime from .venv site-packages for host_python -B -I -S."""
    base = tmp_path_factory.mktemp("sealed_rt_r3")
    src_sp = base / "sp_src"
    src_sp.mkdir()
    venv_sp = Path(sysconfig.get_paths()["purelib"]).resolve()
    for entry in _RUNTIME_TOP_ENTRIES:
        src = venv_sp / entry
        if not src.exists():
            continue
        dst = src_sp / entry
        if src.is_dir():
            shutil.copytree(
                src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
            )
        else:
            shutil.copy2(src, dst)
    host_py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    rt_dir = base / "rt-real"
    stage_sealed_runtime(src_sp, rt_dir, runtime_id="rt-real", python_abi=probe_python_abi(host_py))
    yield (rt_dir, src_sp, host_py)
    _chmod_tree_writable(base)


@pytest.fixture(autouse=True)
def _cleanup_ro_tmp(tmp_path: Path):
    yield
    _chmod_tree_writable(tmp_path)


def _make_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
    os.chmod(path, 0o755)


def _run_desktop_open(
    wrapper: Path,
    *,
    tmp_path: Path,
    home: Path,
    root: Path,
    xdg: Path,
) -> tuple[subprocess.CompletedProcess, str]:
    """Execute `<wrapper> open` in an isolated fixture with stubbed chromium/curl/systemctl."""
    stub_bin = tmp_path / f"desk_stub_{time.monotonic_ns()}"
    stub_bin.mkdir()
    log = stub_bin / "browser.log"
    _make_stub(stub_bin / "systemctl", "exit 0")
    _make_stub(stub_bin / "curl", "exit 0")
    _make_stub(stub_bin / "pgrep", "exit 1")
    _make_stub(stub_bin / "chromium", f'echo "chromium $*" >> "{log}"\nexit 0')

    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}:{env.get('PATH', '')}"
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    env["SIGINT101_UPDATER_ROOT"] = str(root)
    env["SIGINT101_COURSE_URL"] = "http://127.0.0.1:8008"

    proc = subprocess.run(
        ["/bin/bash", str(wrapper), "open"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    launched = ""
    if proc.returncode == 0:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if log.is_file():
                launched = log.read_text(encoding="utf-8").strip()
                if launched:
                    break
            time.sleep(0.05)
    return proc, launched


def _build_legacy_box(tmp_path: Path, *, name: str = "box") -> dict:
    base = tmp_path / name
    base.mkdir(parents=True, exist_ok=True)
    bare, _work, c1, c2 = _build_origin(base)
    home = base / "home"
    home.mkdir()
    root = home / ".local" / "state" / "sigint101-updater"
    _marker, check = _make_marker(base, root)
    checkout = home / "sigint-101"
    _git(home, "clone", str(bare), str(checkout))
    _git(checkout, "checkout", c1)
    (checkout / "data").mkdir(exist_ok=True)
    venv_bin = checkout / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    xdg = base / "xdg"
    xdg.mkdir()
    (xdg / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")
    unit_path = home / ".config" / "systemd" / "user" / "sigint101.service"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    orig_unit = (REPO_ROOT / "deploy" / "kiosk" / "sigint101.service").read_text(encoding="utf-8")
    unit_path.write_text(orig_unit, encoding="utf-8")
    wrapper_dir = base / "usr_local_bin"
    wrapper_dir.mkdir()
    wrapper_path = wrapper_dir / "sigint101-course"
    wrapper_path.write_bytes((REPO_ROOT / "deploy" / "kiosk" / "start-kiosk.sh").read_bytes())
    os.chmod(wrapper_path, 0o755)
    return {
        "base": base,
        "bare": bare,
        "c1": c1,
        "c2": c2,
        "home": home,
        "root": root,
        "check": check,
        "checkout": checkout,
        "xdg": xdg,
        "unit_path": unit_path,
        "orig_unit": orig_unit,
        "wrapper_dir": wrapper_dir,
        "wrapper_path": wrapper_path,
    }


def _load_control_manifest_mod():
    mod_path = REPO_ROOT / "deploy" / "managed" / "control_manifest.py"
    spec = importlib.util.spec_from_file_location("_sigint101_cm_r3", mod_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- F1 (blocker): private atomic control staging + manifest verification + partial reclaim ----


def test_f1_control_enospc_staging_is_atomic_and_retry_dispatcher_launches_cleanly(
    tmp_path, host_rt, monkeypatch
):
    """F1: ENOSPC while staging launcher.py leaves no partial control dir or unit change; a retry
    stages a full verified control generation whose dispatcher finds launcher.py. A pre-existing
    partial control directory is refused and reclaimed, never selected."""
    _rt_cached, src_sp, host_py = host_rt
    box = _build_legacy_box(tmp_path, name="enospc_ctrl")

    real_open = os.open
    fail_once = {"armed": True}

    def _enospc_on_launcher(path, flags, *args, **kwargs):
        if fail_once["armed"] and str(path).endswith("deploy/managed/launcher.py") and (
            flags & os.O_CREAT
        ):
            fail_once["armed"] = False
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _enospc_on_launcher)
    with pytest.raises((OSError, EnrollmentError)):
        enroll_legacy_fixture(
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
    monkeypatch.setattr(os, "open", real_open)

    # Atomic staging: no partial control/ctrl-0001, no leftover sibling, untouched unit.
    ctrl_parent = box["root"] / "control"
    assert not (ctrl_parent / "ctrl-0001").exists()
    leftovers = list(ctrl_parent.iterdir()) if ctrl_parent.exists() else []
    assert leftovers == [], f"leftover control staging artifacts: {leftovers}"
    assert not (box["root"] / "control.json").exists()
    assert box["unit_path"].read_text(encoding="utf-8") == box["orig_unit"]

    # Retry succeeds and the installed dispatcher finds launcher.py cleanly.
    receipt = enroll_legacy_fixture(
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
    assert receipt["mode"] == "legacy"
    ctrl_dir = box["root"] / "control" / "ctrl-0001"
    assert (ctrl_dir / "deploy" / "managed" / "launcher.py").is_file()
    assert (ctrl_dir / "control_manifest.json").is_file()
    cm = _load_control_manifest_mod()
    cm.verify_control_generation(ctrl_dir, expected_control_id="ctrl-0001")

    # Dispatcher finds and execs launcher.py from the verified control generation cleanly!
    disp_serve = subprocess.run(
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
    assert disp_serve.returncode == 0, (disp_serve.stdout, disp_serve.stderr)
    assert "control launcher missing" not in disp_serve.stderr
    disp_status = subprocess.run(
        [
            sys.executable,
            "-B",
            str(box["root"] / "bin" / "dispatcher.py"),
            "--root",
            str(box["root"]),
            "status",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert disp_status.returncode in (0, 10), (disp_status.stdout, disp_status.stderr)
    assert "action=status" in disp_status.stdout
    assert "op=enroll-0001" in disp_status.stdout

    # Part B: a pre-existing partial control directory is REFUSED and RECLAIMED, never selected.
    box2 = _build_legacy_box(tmp_path, name="planted_partial_ctrl")
    partial = box2["root"] / "control" / "ctrl-0001" / "sigint101"
    partial.mkdir(parents=True)
    os.chmod(box2["root"], 0o700)
    (partial / "__init__.py").write_text("# partial\n", encoding="utf-8")

    with pytest.raises(EnrollmentError, match="control"):
        enroll_legacy_fixture(
            repo_root=box2["checkout"],
            updater_root=box2["root"],
            legacy_checkout=box2["checkout"],
            legacy_data_dir=box2["checkout"] / "data",
            legacy_python=sys.executable,
            source_site_packages=src_sp,
            unit_path=box2["unit_path"],
            desktop_wrapper_path=box2["wrapper_path"],
            home=box2["home"],
            runtime_dir=box2["xdg"],
            host_python=host_py,
            trusted_remote=str(box2["bare"]),
            legacy_commit=box2["c1"],
            kiosk_process_lister=lambda: [],
            reload_systemd=lambda: None,
            drain_old_service=lambda: None,
            bench_marker_checker=box2["check"],
        )
    assert not (box2["root"] / "control" / "ctrl-0001").exists(), (
        "partial control was not reclaimed"
    )
    assert not (box2["root"] / "control.json").exists()
    assert box2["unit_path"].read_text(encoding="utf-8") == box2["orig_unit"]

    # Clean retry after partial reclaim succeeds and publishes a verified control generation.
    receipt2 = enroll_legacy_fixture(
        repo_root=box2["checkout"],
        updater_root=box2["root"],
        legacy_checkout=box2["checkout"],
        legacy_data_dir=box2["checkout"] / "data",
        legacy_python=sys.executable,
        source_site_packages=src_sp,
        unit_path=box2["unit_path"],
        desktop_wrapper_path=box2["wrapper_path"],
        home=box2["home"],
        runtime_dir=box2["xdg"],
        host_python=host_py,
        trusted_remote=str(box2["bare"]),
        legacy_commit=box2["c1"],
        kiosk_process_lister=lambda: [],
        reload_systemd=lambda: None,
        drain_old_service=lambda: None,
        bench_marker_checker=box2["check"],
    )
    assert receipt2["mode"] == "legacy"
    assert (box2["root"] / "control" / "ctrl-0001" / "deploy" / "managed" / "launcher.py").is_file()

    # Part C: a pre-existing control dir whose launcher.py drifted from control_manifest.json
    # is also refused and reclaimed rather than selected.
    box3 = _build_legacy_box(tmp_path, name="tampered_ctrl")
    tampered_dir = box3["root"] / "control" / "ctrl-0001"
    cm.stage_control_generation(box3["checkout"], tampered_dir, control_id="ctrl-0001")
    os.chmod(box3["root"], 0o700)
    launcher_file = tampered_dir / "deploy" / "managed" / "launcher.py"
    os.chmod(launcher_file.parent, 0o700)
    os.chmod(launcher_file, 0o600)
    launcher_file.write_text("# tampered\n", encoding="utf-8")
    os.chmod(launcher_file, 0o500)
    os.chmod(launcher_file.parent, 0o500)
    with pytest.raises(EnrollmentError, match="control"):
        enroll_legacy_fixture(
            repo_root=box3["checkout"],
            updater_root=box3["root"],
            legacy_checkout=box3["checkout"],
            legacy_data_dir=box3["checkout"] / "data",
            legacy_python=sys.executable,
            source_site_packages=src_sp,
            unit_path=box3["unit_path"],
            desktop_wrapper_path=box3["wrapper_path"],
            home=box3["home"],
            runtime_dir=box3["xdg"],
            host_python=host_py,
            trusted_remote=str(box3["bare"]),
            legacy_commit=box3["c1"],
            kiosk_process_lister=lambda: [],
            reload_systemd=lambda: None,
            drain_old_service=lambda: None,
            bench_marker_checker=box3["check"],
        )
    assert not tampered_dir.exists()
    assert not (box3["root"] / "control.json").exists()


# --- F2 (blocker): desktop entry stays usable until enrollment is complete and usable ----------


def test_f2_staged_privileged_wrapper_keeps_existing_desktop_entry_usable_until_complete(
    tmp_path, host_rt, capsys
):
    """F2: when desktop wrapper directory is unwritable, preflight prescribes staging a separate
    .managed sibling without overwriting the live desktop entry. If enrollment is interrupted or
    refused after that privileged step, `<wrapper> open` still launches the browser (never 75)."""
    _rt_cached, src_sp, host_py = host_rt
    box = _build_legacy_box(tmp_path, name="f2_wrapper")
    wrapper_dir = box["wrapper_dir"]
    wrapper_path = box["wrapper_path"]
    staged_path = wrapper_path.with_name(f"{wrapper_path.name}.managed")
    managed_bytes = (
        REPO_ROOT / "deploy" / "managed" / "sigint101-course-managed.sh"
    ).read_bytes()
    legacy_bytes = wrapper_path.read_bytes()

    os.chmod(wrapper_dir, 0o555)
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
        str(wrapper_path),
        "--runtime-dir",
        str(box["xdg"]),
    ]
    try:
        # 1) First run refuses with zero writes and prescribes staging to <wrapper>.managed.
        rc1 = updater_cli_main(
            argv,
            _bench_marker_checker=box["check"],
            _service_factory=lambda *a: _make_stub_service_class()(),
            _home=box["home"],
            _kiosk_process_lister=lambda: [],
        )
        err1 = capsys.readouterr().err
        assert rc1 == 11
        assert "Permission denied" in err1
        assert "sudo install -m 0755" in err1
        assert str(staged_path) in err1
        assert not box["root"].exists()

        # 2) Follow the prescribed privileged staging step ONLY (live wrapper stays untouched).
        os.chmod(wrapper_dir, 0o755)
        staged_path.write_bytes(managed_bytes)
        os.chmod(staged_path, 0o755)
        os.chmod(wrapper_dir, 0o555)
        assert wrapper_path.read_bytes() == legacy_bytes

        # 3a) Before retrying --enroll, the live desktop entry still opens the browser!
        proc_before, launched_before = _run_desktop_open(
            wrapper_path,
            tmp_path=tmp_path,
            home=box["home"],
            root=box["root"],
            xdg=box["xdg"],
        )
        assert proc_before.returncode == 0, (proc_before.returncode, proc_before.stderr)
        assert proc_before.returncode != EXIT_GATE_BUSY
        assert "chromium " in launched_before

        # 3b) If the next --enroll attempt is REFUSED (e.g. Exit flag cleared by open above!),
        # the live desktop entry STILL opens the browser and never returns 75!
        rc_refused = updater_cli_main(
            argv,
            _bench_marker_checker=box["check"],
            _service_factory=lambda *a: _make_stub_service_class()(),
            _home=box["home"],
            _kiosk_process_lister=lambda: [],
        )
        capsys.readouterr()
        assert rc_refused == 11
        proc_refused, launched_refused = _run_desktop_open(
            wrapper_path,
            tmp_path=tmp_path,
            home=box["home"],
            root=box["root"],
            xdg=box["xdg"],
        )
        assert proc_refused.returncode == 0, (proc_refused.returncode, proc_refused.stderr)
        assert "chromium " in launched_refused

        # 3c) If enrollment is INTERRUPTED mid-way (after locks exist, before unit install),
        # the live desktop entry STILL opens the browser!
        (box["xdg"] / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")

        def _interrupt(stage: str) -> None:
            if stage == "before_unit_install":
                raise RuntimeError("simulated interruption before unit install")

        with pytest.raises(RuntimeError, match="simulated interruption"):
            enroll_legacy_fixture(
                repo_root=box["checkout"],
                updater_root=box["root"],
                legacy_checkout=box["checkout"],
                legacy_data_dir=box["checkout"] / "data",
                legacy_python=sys.executable,
                source_site_packages=src_sp,
                unit_path=box["unit_path"],
                desktop_wrapper_path=wrapper_path,
                home=box["home"],
                runtime_dir=box["xdg"],
                host_python=host_py,
                trusted_remote=str(box["bare"]),
                legacy_commit=box["c1"],
                kiosk_process_lister=lambda: [],
                reload_systemd=lambda: None,
                drain_old_service=lambda: None,
                on_boundary=_interrupt,
                bench_marker_checker=box["check"],
            )
        proc_int, launched_int = _run_desktop_open(
            wrapper_path,
            tmp_path=tmp_path,
            home=box["home"],
            root=box["root"],
            xdg=box["xdg"],
        )
        assert proc_int.returncode == 0, (proc_int.returncode, proc_int.stderr)
        assert "chromium " in launched_int

        # 4) Complete enrollment cleanly; handoff record is complete and names activation command.
        (box["xdg"] / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")
        rc_ok = updater_cli_main(
            argv,
            _bench_marker_checker=box["check"],
            _service_factory=lambda *a: _make_stub_service_class()(),
            _home=box["home"],
            _kiosk_process_lister=lambda: [],
        )
        out_ok = capsys.readouterr().out
        assert rc_ok == 0
        assert f"sudo mv -f {staged_path} {wrapper_path}" in out_ok
        enr = json.loads((box["root"] / "enrollment.json").read_text(encoding="utf-8"))
        assert enr["status"] == "complete"
        assert enr["handoff_complete"] is True
        assert enr["desktop_wrapper"]["state"] == "staged_pending_activation"
        assert enr["desktop_wrapper"]["activation_command"] == (
            f"sudo mv -f {staged_path} {wrapper_path}"
        )

        # 5) Activate staged wrapper atomically after enrollment is complete and usable.
        os.chmod(wrapper_dir, 0o755)
        os.replace(staged_path, wrapper_path)
        os.chmod(wrapper_dir, 0o555)

        # Now the managed wrapper enforces the mutation gate (75 while locked, 0 when free).
        with acquire_mutation_gate(box["root"], blocking=False, home=box["home"]):
            proc_busy, _ = _run_desktop_open(
                wrapper_path,
                tmp_path=tmp_path,
                home=box["home"],
                root=box["root"],
                xdg=box["xdg"],
            )
            assert proc_busy.returncode == EXIT_GATE_BUSY
        proc_after, launched_after = _run_desktop_open(
            wrapper_path,
            tmp_path=tmp_path,
            home=box["home"],
            root=box["root"],
            xdg=box["xdg"],
        )
        assert proc_after.returncode == 0, (proc_after.returncode, proc_after.stderr)
        assert "chromium " in launched_after
    finally:
        os.chmod(wrapper_dir, 0o755)


# --- F3 (fix): preflight every shim target + completed handoff before wrapper + late recovery --


def test_f3_preflights_shim_targets_publishes_handoff_before_wrapper_and_recovers_late_failure(
    tmp_path, host_rt, monkeypatch
):
    """F3: (a) a directory at deploy/provision.sh is refused BEFORE any write; (b) completed
    enrollment.json is published BEFORE enabling the managed wrapper; (c) a late write failure
    while writing provision.sh rolls back install.sh, leaves unit & wrapper untouched, and retry
    succeeds cleanly."""
    _rt_cached, src_sp, host_py = host_rt

    # (a) Directory at deploy/provision.sh MUST refuse before creating ROOT or touching shims.
    box_dir = _build_legacy_box(tmp_path, name="f3_dir_at_provision")
    orig_install = (box_dir["checkout"] / "deploy" / "install.sh").read_bytes()
    prov_path = box_dir["checkout"] / "deploy" / "provision.sh"
    prov_path.unlink()
    prov_path.mkdir()

    with pytest.raises(EnrollmentError, match="provision.sh"):
        enroll_legacy_fixture(
            repo_root=box_dir["checkout"],
            updater_root=box_dir["root"],
            legacy_checkout=box_dir["checkout"],
            legacy_data_dir=box_dir["checkout"] / "data",
            legacy_python=sys.executable,
            source_site_packages=src_sp,
            unit_path=box_dir["unit_path"],
            desktop_wrapper_path=box_dir["wrapper_path"],
            home=box_dir["home"],
            runtime_dir=box_dir["xdg"],
            host_python=host_py,
            trusted_remote=str(box_dir["bare"]),
            legacy_commit=box_dir["c1"],
            kiosk_process_lister=lambda: [],
            reload_systemd=lambda: None,
            drain_old_service=lambda: None,
            bench_marker_checker=box_dir["check"],
        )
    assert not box_dir["root"].exists()
    assert (box_dir["checkout"] / "deploy" / "install.sh").read_bytes() == orig_install
    assert box_dir["unit_path"].read_text(encoding="utf-8") == box_dir["orig_unit"]

    # Symlink planted at deploy/install.sh also refuses before any write.
    box_sym = _build_legacy_box(tmp_path, name="f3_symlink_at_install")
    orig_prov_sym = (box_sym["checkout"] / "deploy" / "provision.sh").read_bytes()
    inst_sym = box_sym["checkout"] / "deploy" / "install.sh"
    inst_sym.unlink()
    inst_sym.symlink_to(box_sym["checkout"] / "README.md")
    with pytest.raises(EnrollmentError, match="install.sh"):
        enroll_legacy_fixture(
            repo_root=box_sym["checkout"],
            updater_root=box_sym["root"],
            legacy_checkout=box_sym["checkout"],
            legacy_data_dir=box_sym["checkout"] / "data",
            legacy_python=sys.executable,
            source_site_packages=src_sp,
            unit_path=box_sym["unit_path"],
            desktop_wrapper_path=box_sym["wrapper_path"],
            home=box_sym["home"],
            runtime_dir=box_sym["xdg"],
            host_python=host_py,
            trusted_remote=str(box_sym["bare"]),
            legacy_commit=box_sym["c1"],
            kiosk_process_lister=lambda: [],
            reload_systemd=lambda: None,
            drain_old_service=lambda: None,
            bench_marker_checker=box_sym["check"],
        )
    assert not box_sym["root"].exists()
    assert (box_sym["checkout"] / "deploy" / "provision.sh").read_bytes() == orig_prov_sym

    # (b) Completed handoff record is published BEFORE the managed wrapper is enabled.
    box_order = _build_legacy_box(tmp_path, name="f3_handoff_order")
    legacy_wrapper_bytes = box_order["wrapper_path"].read_bytes()
    observed = {}

    def _check_before_wrapper(stage: str) -> None:
        if stage == "before_wrapper_activation":
            enr_text = (box_order["root"] / "enrollment.json").read_text(encoding="utf-8")
            enr_now = json.loads(enr_text)
            observed["status"] = enr_now.get("status")
            observed["handoff_complete"] = enr_now.get("handoff_complete")
            observed["wrapper_still_legacy"] = (
                box_order["wrapper_path"].read_bytes() == legacy_wrapper_bytes
            )
            observed["unit_is_managed"] = "dispatcher.py" in box_order["unit_path"].read_text(
                encoding="utf-8"
            )

    enroll_legacy_fixture(
        repo_root=box_order["checkout"],
        updater_root=box_order["root"],
        legacy_checkout=box_order["checkout"],
        legacy_data_dir=box_order["checkout"] / "data",
        legacy_python=sys.executable,
        source_site_packages=src_sp,
        unit_path=box_order["unit_path"],
        desktop_wrapper_path=box_order["wrapper_path"],
        home=box_order["home"],
        runtime_dir=box_order["xdg"],
        host_python=host_py,
        trusted_remote=str(box_order["bare"]),
        legacy_commit=box_order["c1"],
        kiosk_process_lister=lambda: [],
        reload_systemd=lambda: None,
        drain_old_service=lambda: None,
        on_boundary=_check_before_wrapper,
        bench_marker_checker=box_order["check"],
    )
    assert observed == {
        "status": "complete",
        "handoff_complete": True,
        "wrapper_still_legacy": True,
        "unit_is_managed": True,
    }

    # (c) Late write failure on provision.sh (after install.sh was replaced) rolls back install.sh,
    # leaves unit and wrapper untouched, keeps course openable, and allows clean retry.
    box_late = _build_legacy_box(tmp_path, name="f3_late_fail")
    orig_install_late = (box_late["checkout"] / "deploy" / "install.sh").read_bytes()
    orig_prov_late = (box_late["checkout"] / "deploy" / "provision.sh").read_bytes()
    orig_wrapper_late = box_late["wrapper_path"].read_bytes()

    import sigint101.enrollment as enr_mod

    real_atomic = enr_mod._atomic_write_bytes
    fail_prov = {"armed": True}

    def _fail_provision_write(path: Path, data: bytes, *, mode: int = 0o644) -> None:
        if fail_prov["armed"] and path == box_late["checkout"] / "deploy" / "provision.sh":
            fail_prov["armed"] = False
            raise OSError(errno.ENOSPC, "No space left on device")
        real_atomic(path, data, mode=mode)

    monkeypatch.setattr(enr_mod, "_atomic_write_bytes", _fail_provision_write)
    with pytest.raises(EnrollmentError, match="rolled back|recovery|provision.sh"):
        enroll_legacy_fixture(
            repo_root=box_late["checkout"],
            updater_root=box_late["root"],
            legacy_checkout=box_late["checkout"],
            legacy_data_dir=box_late["checkout"] / "data",
            legacy_python=sys.executable,
            source_site_packages=src_sp,
            unit_path=box_late["unit_path"],
            desktop_wrapper_path=box_late["wrapper_path"],
            home=box_late["home"],
            runtime_dir=box_late["xdg"],
            host_python=host_py,
            trusted_remote=str(box_late["bare"]),
            legacy_commit=box_late["c1"],
            kiosk_process_lister=lambda: [],
            reload_systemd=lambda: None,
            drain_old_service=lambda: None,
            bench_marker_checker=box_late["check"],
        )
    monkeypatch.setattr(enr_mod, "_atomic_write_bytes", real_atomic)

    # Automatic rollback restored install.sh; provision.sh, unit, and wrapper remain legacy!
    assert (box_late["checkout"] / "deploy" / "install.sh").read_bytes() == orig_install_late
    assert (box_late["checkout"] / "deploy" / "provision.sh").read_bytes() == orig_prov_late
    assert box_late["unit_path"].read_text(encoding="utf-8") == box_late["orig_unit"]
    assert box_late["wrapper_path"].read_bytes() == orig_wrapper_late
    enr_failed = json.loads((box_late["root"] / "enrollment.json").read_text(encoding="utf-8"))
    assert enr_failed.get("status") != "complete"
    assert enr_failed.get("handoff_complete") is not True

    # Desktop entry still opens the legacy course cleanly.
    proc_open, launched_open = _run_desktop_open(
        box_late["wrapper_path"],
        tmp_path=tmp_path,
        home=box_late["home"],
        root=box_late["root"],
        xdg=box_late["xdg"],
    )
    assert proc_open.returncode == 0, proc_open.stderr
    assert "chromium " in launched_open

    # Retry succeeds cleanly and preserves exact original shim digests!
    (box_late["xdg"] / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")
    enroll_legacy_fixture(
        repo_root=box_late["checkout"],
        updater_root=box_late["root"],
        legacy_checkout=box_late["checkout"],
        legacy_data_dir=box_late["checkout"] / "data",
        legacy_python=sys.executable,
        source_site_packages=src_sp,
        unit_path=box_late["unit_path"],
        desktop_wrapper_path=box_late["wrapper_path"],
        home=box_late["home"],
        runtime_dir=box_late["xdg"],
        host_python=host_py,
        trusted_remote=str(box_late["bare"]),
        legacy_commit=box_late["c1"],
        kiosk_process_lister=lambda: [],
        reload_systemd=lambda: None,
        drain_old_service=lambda: None,
        bench_marker_checker=box_late["check"],
    )
    enr_done = json.loads((box_late["root"] / "enrollment.json").read_text(encoding="utf-8"))
    assert enr_done["status"] == "complete"
    assert enr_done["shims"]["install.sh"]["original_sha256"] == hashlib.sha256(
        orig_install_late
    ).hexdigest()
    assert enr_done["shims"]["provision.sh"]["original_sha256"] == hashlib.sha256(
        orig_prov_late
    ).hexdigest()


# --- F4 (fix): preserve verified 3004a03 launcher; record unknown if unrecoverable -------------


def test_f4_preserves_3004a03_original_launcher_and_marks_unrecoverable_original_unknown(
    tmp_path, host_rt, capsys
):
    """F4: starting with the allowlisted 3004a03 launcher and a read-only wrapper dir, following
    the staged privileged step preserves 3004a03's exact digest and bytes across reruns. If the
    prior launcher was already overwritten without a preserved backup, enrollment records unknown
    with original_reviewed=False, never the current checkout's launcher digest."""
    _rt_cached, src_sp, host_py = host_rt
    launcher_3004 = (
        REPO_ROOT / "tests" / "fixtures" / "kiosk" / "start-kiosk-3004a03.sh"
    ).read_bytes()
    sha_3004 = hashlib.sha256(launcher_3004).hexdigest()
    sha_checkout_launcher = hashlib.sha256(
        (REPO_ROOT / "deploy" / "kiosk" / "start-kiosk.sh").read_bytes()
    ).hexdigest()
    managed_bytes = (
        REPO_ROOT / "deploy" / "managed" / "sigint101-course-managed.sh"
    ).read_bytes()
    assert sha_3004 != sha_checkout_launcher

    # Part A: 3004a03 launcher + read-only wrapper dir + staged step -> preserves 3004a03.
    box = _build_legacy_box(tmp_path, name="f4_3004a03")
    box["wrapper_path"].write_bytes(launcher_3004)
    os.chmod(box["wrapper_path"], 0o755)
    staged_path = box["wrapper_path"].with_name(f"{box['wrapper_path'].name}.managed")
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
        assert rc1 == 11

        # Stage managed wrapper separately without erasing 3004a03.
        os.chmod(box["wrapper_dir"], 0o755)
        staged_path.write_bytes(managed_bytes)
        os.chmod(staged_path, 0o755)
        os.chmod(box["wrapper_dir"], 0o555)

        rc2 = updater_cli_main(
            argv,
            _bench_marker_checker=box["check"],
            _service_factory=lambda *a: _make_stub_service_class()(),
            _home=box["home"],
            _kiosk_process_lister=lambda: [],
        )
        capsys.readouterr()
        assert rc2 == 0
        enr = json.loads((box["root"] / "enrollment.json").read_text(encoding="utf-8"))
        assert enr["desktop_wrapper"]["original_sha256"] == sha_3004
        assert enr["desktop_wrapper"]["original_reviewed"] is True
        backup = box["root"] / "originals" / "desktop-wrapper.sh"
        assert backup.is_file() and backup.read_bytes() == launcher_3004

        # Activate managed wrapper, remove enrollment.json, and rerun -> STILL recovers 3004a03!
        os.chmod(box["wrapper_dir"], 0o755)
        os.replace(staged_path, box["wrapper_path"])
        os.chmod(box["wrapper_dir"], 0o555)
        (box["root"] / "enrollment.json").unlink()

        rc3 = updater_cli_main(
            argv,
            _bench_marker_checker=box["check"],
            _service_factory=lambda *a: _make_stub_service_class()(),
            _home=box["home"],
            _kiosk_process_lister=lambda: [],
        )
        capsys.readouterr()
        assert rc3 == 0
        enr3 = json.loads((box["root"] / "enrollment.json").read_text(encoding="utf-8"))
        assert enr3["desktop_wrapper"]["original_sha256"] == sha_3004
        assert enr3["desktop_wrapper"]["original_reviewed"] is True
    finally:
        os.chmod(box["wrapper_dir"], 0o755)

    # Part B: wrapper was already replaced with managed bytes and NO backup/prior record exists
    # -> MUST record original_sha256='unknown' and original_reviewed=False.
    box_unk = _build_legacy_box(tmp_path, name="f4_unknown")
    box_unk["wrapper_path"].write_bytes(managed_bytes)
    os.chmod(box_unk["wrapper_path"], 0o755)
    os.chmod(box_unk["wrapper_dir"], 0o555)
    try:
        enroll_legacy_fixture(
            repo_root=box_unk["checkout"],
            updater_root=box_unk["root"],
            legacy_checkout=box_unk["checkout"],
            legacy_data_dir=box_unk["checkout"] / "data",
            legacy_python=sys.executable,
            source_site_packages=src_sp,
            unit_path=box_unk["unit_path"],
            desktop_wrapper_path=box_unk["wrapper_path"],
            home=box_unk["home"],
            runtime_dir=box_unk["xdg"],
            host_python=host_py,
            trusted_remote=str(box_unk["bare"]),
            legacy_commit=box_unk["c1"],
            kiosk_process_lister=lambda: [],
            reload_systemd=lambda: None,
            drain_old_service=lambda: None,
            bench_marker_checker=box_unk["check"],
        )
        enr_unk = json.loads((box_unk["root"] / "enrollment.json").read_text(encoding="utf-8"))
        assert enr_unk["desktop_wrapper"]["original_sha256"] == "unknown"
        assert enr_unk["desktop_wrapper"]["original_sha256"] != sha_checkout_launcher
        assert enr_unk["desktop_wrapper"]["original_reviewed"] is False
    finally:
        os.chmod(box_unk["wrapper_dir"], 0o755)


# --- F5 (fix): reclaim abandoned staging siblings under mutation gate, keep published runtimes --


def test_f5_reclaims_abandoned_runtime_staging_under_mutation_gate_without_touching_published(
    tmp_path, host_rt
):
    """F5: killing a staging process with SIGKILL leaves a .rt-0001.staging.<pid>.<hex> sibling.
    A retry under the mutation gate reclaims abandoned staging siblings while preserving every
    published/in-use runtime and never following symlinks."""
    _rt_cached, src_sp, host_py = host_rt
    box = _build_legacy_box(tmp_path, name="f5_reclaim")
    runtimes_dir = box["root"] / "runtimes"
    runtimes_dir.mkdir(parents=True, mode=0o700)
    os.chmod(box["root"], 0o700)

    # 1) Plant a real published runtime that MUST NOT be removed.
    published_rt = runtimes_dir / "rt-published"
    stage_sealed_runtime(
        src_sp, published_rt, runtime_id="rt-published", python_abi=probe_python_abi(host_py)
    )
    # Plant a symlink looking like a staging name; its outside target MUST NOT be touched!
    outside_dir = tmp_path / "outside_precious"
    outside_dir.mkdir()
    outside_marker = outside_dir / "keep.txt"
    outside_marker.write_text("precious\n", encoding="utf-8")
    (runtimes_dir / ".rt-symlink.staging.1.deadbeef").symlink_to(outside_dir)

    # 2) Spawn a child process that stages rt-0001 and pauses right before os.rename -> SIGKILL it!
    ready_file = tmp_path / "staging_ready"
    helper_code = (
        "import os, pathlib, sys, time\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import sigint101.runtime as rt\n"
        "real_rename = os.rename\n"
        "def _pause(src, dst):\n"
        f"    pathlib.Path({str(ready_file)!r}).write_text(str(src), encoding='utf-8')\n"
        "    time.sleep(60)\n"
        "os.rename = _pause\n"
        f"rt.stage_sealed_runtime({str(src_sp)!r}, {str(runtimes_dir / 'rt-0001')!r}, "
        f"runtime_id='rt-0001', python_abi=rt.probe_python_abi({str(host_py)!r}))\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", helper_code])
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not ready_file.is_file():
            if proc.poll() is not None:
                raise AssertionError(f"staging helper exited early with {proc.returncode}")
            time.sleep(0.05)
        assert ready_file.is_file(), "staging helper did not reach rename point"
        abandoned = Path(ready_file.read_text(encoding="utf-8"))
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    # Confirm SIGKILL left the read-only staging sibling behind and no published rt-0001.
    assert abandoned.is_dir()
    assert not (runtimes_dir / "rt-0001").exists()

    # 3) Retry enrollment under the mutation gate -> reclaims abandoned staging sibling & publishes.
    enroll_legacy_fixture(
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
    assert not abandoned.exists(), f"abandoned staging sibling was not reclaimed: {abandoned}"
    assert (runtimes_dir / "rt-0001" / "runtime.json").is_file()
    assert (published_rt / "runtime.json").is_file(), "published runtime was removed!"
    assert outside_marker.read_text(encoding="utf-8") == "precious\n"

    # 4) apply_update under mutation gate also reclaims abandoned staging siblings in runtimes/
    # without touching the active rt-0001!
    apply_dir = tmp_path / "apply_reclaim"
    apply_dir.mkdir()
    fx = _enroll_real_box(apply_dir, host_rt)
    stale_apply = fx["root"] / "runtimes" / ".rt-0002.staging.99999.cafebabe"
    stale_apply.mkdir(mode=0o700)
    (stale_apply / "junk.bin").write_bytes(b"x" * 1024)
    os.chmod(stale_apply / "junk.bin", 0o444)
    os.chmod(stale_apply, 0o555)

    svc = _MinimalService(fx["root"])
    res = apply_update(
        root=fx["root"],
        remote_url=str(fx["bare"]),
        pinned_commit=fx["c2"],
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert res["status"] == "ready"
    assert not stale_apply.exists(), "apply_update did not reclaim abandoned runtime staging"
    assert (fx["root"] / "runtimes" / "rt-0001" / "runtime.json").is_file()
