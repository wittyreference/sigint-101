# ABOUTME: Regression and review tests for PR 4 Round 2 findings R2-1 through R2-5.
# ABOUTME: Covers venv .pth skipping, atomic staging, preflight/originals, abort, MG4/R1b/X1/X5/X13.

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from sigint101.enrollment import EnrollmentError, enroll_legacy_fixture
from sigint101.locks import EXIT_GATE_BUSY, acquire_mutation_gate
from sigint101.process_checks import SystemdUserController
from sigint101.runtime import (
    RuntimeCompatibilityError,
    check_runtime_imports,
    probe_python_abi,
    stage_sealed_runtime,
)
from sigint101.updater import (
    AbortError,
    abort_unserved,
    apply_update,
    inspect_serving_marker,
)
from sigint101.updater_cli import main as updater_cli_main
from sigint101.validation import compute_settings_fingerprint, parse_systemd_unit_config
from tests.test_updater_pr4_fixes import (
    _RUNTIME_TOP_ENTRIES,
    REPO_ROOT,
    _build_origin,
    _chmod_tree_writable,
    _enroll_real_box,
    _git,
    _make_marker,
    _MinimalService,
    _snapshot_tree,
)


@pytest.fixture(autouse=True)
def _cleanup_ro_tmp(tmp_path: Path):
    yield
    _chmod_tree_writable(tmp_path)


@pytest.fixture(scope="module")
def host_rt(tmp_path_factory):
    """Stage a sealed runtime from .venv site-packages for host_python -B -I -S."""
    base = tmp_path_factory.mktemp("sealed_rt_r2")
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

DISTUTILS_PRECEDENCE = (
    "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local') == "
    "'local'; enabled and __import__('_distutils_hack').add_shim(); \n"
)


def _load_launcher_module():
    mod_path = REPO_ROOT / "deploy" / "managed" / "launcher.py"
    spec = importlib.util.spec_from_file_location("_sigint101_launcher_r2", mod_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_stub_service_class():
    class _Svc:
        def __init__(self, *_a):
            pass

        def __getattr__(self, name):
            def _fn(*_a, **_k):
                if name == "unit_state":
                    return ("inactive", "dead", 0)
                if name == "get_cgroup_pids":
                    return []
                if name in ("is_listener_active", "is_http_ready"):
                    return False
                return None

            return _fn

    return _Svc


# --- R2-1: skip .pth files when staging stock venvs, record them, and stage atomically ---------


def test_r2_1_stock_requirements_venv_stages_skips_pth_and_enrolls(tmp_path):
    """R2-1: a stock requirements.txt venv (carrying a1_coverage.pth + distutils-precedence.pth)
    stages cleanly with all .pth files skipped and recorded in runtime.json and enrollment.json."""
    purelib = Path(sysconfig.get_paths()["purelib"]).resolve()
    abi = probe_python_abi(sys.executable)

    # 1) Stage the actual requirements.txt venv purelib directly.
    live_rt = tmp_path / "rt-live-venv"
    live_manifest = stage_sealed_runtime(
        purelib, live_rt, runtime_id="rt-live-venv", python_abi=abi
    )
    live_pths = sorted(p.name for p in purelib.glob("*.pth"))
    assert live_manifest["skipped_pth_files"] == live_pths
    assert list((live_rt / "site-packages").rglob("*.pth")) == []

    # 2) Build a venv site-packages copy carrying BOTH a1_coverage.pth and Bookworm setuptools'
    # distutils-precedence.pth, then enroll via the real CLI on that full site-packages.
    stock_sp = tmp_path / "stock_sp"
    shutil.copytree(
        purelib,
        stock_sp,
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.egg-link"),
    )
    (stock_sp / "a1_coverage.pth").write_text(
        "import sys; sys.modules.get('coverage')\n", encoding="utf-8"
    )
    (stock_sp / "_distutils_hack").mkdir(exist_ok=True)
    (stock_sp / "_distutils_hack" / "__init__.py").write_text("", encoding="utf-8")
    (stock_sp / "distutils-precedence.pth").write_text(DISTUTILS_PRECEDENCE, encoding="utf-8")

    bare, _work, c1, _c2 = _build_origin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    root = home / ".local" / "state" / "sigint101-updater"
    _marker, check = _make_marker(tmp_path, root)
    checkout = home / "sigint-101"
    _git(home, "clone", str(bare), str(checkout))
    _git(checkout, "checkout", c1)
    (checkout / "data").mkdir(exist_ok=True)
    venv_bin = checkout / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    (xdg / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")
    wrapper = home / ".local" / "bin" / "sigint101-course"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_bytes((REPO_ROOT / "deploy" / "kiosk" / "start-kiosk.sh").read_bytes())

    rc = updater_cli_main(
        [
            "--root",
            str(root),
            "--enroll",
            "--checkout",
            str(checkout),
            "--remote",
            str(bare),
            "--host-python",
            sys.executable,
            "--site-packages",
            str(stock_sp),
            "--unit-path",
            str(home / ".config" / "systemd" / "user" / "sigint101.service"),
            "--desktop-wrapper",
            str(wrapper),
            "--runtime-dir",
            str(xdg),
        ],
        _bench_marker_checker=check,
        _service_factory=lambda *a: _make_stub_service_class()(),
        _home=home,
        _kiosk_process_lister=lambda: [],
    )
    assert rc == 0

    rt_dir = root / "runtimes" / "rt-0001"
    rt_manifest = json.loads((rt_dir / "runtime.json").read_text(encoding="utf-8"))
    enr = json.loads((root / "enrollment.json").read_text(encoding="utf-8"))
    for skipped_name in ("a1_coverage.pth", "distutils-precedence.pth"):
        assert skipped_name in rt_manifest["skipped_pth_files"]
        assert skipped_name in enr["skipped_pth_files"]
    assert list((rt_dir / "site-packages").rglob("*.pth")) == []

    # Sealed runtime imports fastapi, uvicorn, pydantic_core, and sigint101 under -B -I -S.
    resolved = check_runtime_imports(
        sys.executable,
        rt_dir,
        release_root=REPO_ROOT,
        modules=("sigint101", "fastapi", "uvicorn", "pydantic_core"),
        forbidden_checkout_root=checkout,
    )
    assert Path(resolved["fastapi"]).is_relative_to(rt_dir / "site-packages")


def test_r2_1_failed_staging_is_atomic_and_rerun_never_reuses_partial_runtime(
    tmp_path, host_rt
):
    """R2-1: refused staging leaves zero partial runtime dir, and enrollment refuses any partial."""
    _rt_cached, src_sp, host_py = host_rt
    abi = probe_python_abi(host_py)
    runtimes_dir = tmp_path / "runtimes"
    dst_rt = runtimes_dir / "rt-0001"

    bad_sp = tmp_path / "bad_sp"
    bad_sp.mkdir()
    (bad_sp / "ok_mod.py").write_text("X = 1\n", encoding="utf-8")
    (bad_sp / "editable.egg-link").write_text("/tmp/editable\n", encoding="utf-8")

    with pytest.raises(RuntimeCompatibilityError, match="egg-link"):
        stage_sealed_runtime(bad_sp, dst_rt, runtime_id="rt-0001", python_abi=abi)

    # Atomic staging: no rt-0001 and no leftover temp staging directory remain!
    assert not dst_rt.exists()
    leftovers = list(runtimes_dir.iterdir()) if runtimes_dir.exists() else []
    assert leftovers == [], f"partial runtime artifacts left behind: {leftovers}"

    # Rerun with clean site-packages succeeds cleanly into the same rt-0001 path.
    manifest = stage_sealed_runtime(src_sp, dst_rt, runtime_id="rt-0001", python_abi=abi)
    assert manifest["runtime_id"] == "rt-0001"
    assert (dst_rt / "runtime.json").is_file()

    # Enrollment must also refuse to reuse a manually planted partial runtime lacking runtime.json!
    case2 = tmp_path / "case2"
    case2.mkdir()
    bare, _work, c1, _c2 = _build_origin(case2)
    home = tmp_path / "case2" / "home"
    home.mkdir(parents=True)
    root = home / ".local" / "state" / "sigint101-updater"
    _marker, check = _make_marker(tmp_path / "case2", root)
    checkout = home / "sigint-101"
    _git(home, "clone", str(bare), str(checkout))
    _git(checkout, "checkout", c1)
    (checkout / "data").mkdir(exist_ok=True)
    xdg = tmp_path / "case2" / "xdg"
    xdg.mkdir()
    (xdg / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")
    partial_rt = root / "runtimes" / "rt-0001"
    (partial_rt / "site-packages").mkdir(parents=True)
    os.chmod(root, 0o700)

    with pytest.raises((EnrollmentError, RuntimeCompatibilityError)):
        enroll_legacy_fixture(
            repo_root=checkout,
            updater_root=root,
            legacy_checkout=checkout,
            legacy_data_dir=checkout / "data",
            legacy_python=sys.executable,
            source_site_packages=src_sp,
            unit_path=home / ".config" / "systemd" / "user" / "sigint101.service",
            desktop_wrapper_path=home / ".local" / "bin" / "sigint101-course",
            home=home,
            runtime_dir=xdg,
            host_python=host_py,
            trusted_remote=str(bare),
            legacy_commit=c1,
            kiosk_process_lister=lambda: [],
            bench_marker_checker=check,
        )


# --- R2-2: preflight every install target + recognise originals via reviewed-launchers.sha256 ---


def test_r2_2_enroll_preflights_unwritable_wrapper_and_preserves_originals_on_rerun(
    tmp_path, host_rt, capsys
):
    """R2-2 (adapted from reviewer's B2 probe): unwritable desktop wrapper refuses BEFORE touching
    install.sh, provision.sh, current.json, or root, names the exact sudo install step, and a rerun
    after installing the managed wrapper or fixing permissions never records shims as originals."""
    _rt_cached, src_sp, host_py = host_rt
    bare, _work, c1, _c2 = _build_origin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    root = home / ".local" / "state" / "sigint101-updater"
    _marker, check = _make_marker(tmp_path, root)
    checkout = home / "sigint-101"
    _git(home, "clone", str(bare), str(checkout))
    _git(checkout, "checkout", c1)
    orig_install = (checkout / "deploy" / "install.sh").read_bytes()
    orig_provision = (checkout / "deploy" / "provision.sh").read_bytes()
    orig_install_sha = hashlib.sha256(orig_install).hexdigest()
    orig_provision_sha = hashlib.sha256(orig_provision).hexdigest()
    (checkout / "data").mkdir(exist_ok=True)
    venv_bin = checkout / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    (xdg / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")

    sysbin = tmp_path / "usr_local_bin"
    sysbin.mkdir()
    wrapper_path = sysbin / "sigint101-course"
    legacy_launcher_bytes = (REPO_ROOT / "deploy" / "kiosk" / "start-kiosk.sh").read_bytes()
    legacy_launcher_sha = hashlib.sha256(legacy_launcher_bytes).hexdigest()
    managed_wrapper_bytes = (
        REPO_ROOT / "deploy" / "managed" / "sigint101-course-managed.sh"
    ).read_bytes()
    managed_wrapper_sha = hashlib.sha256(managed_wrapper_bytes).hexdigest()
    wrapper_path.write_bytes(legacy_launcher_bytes)
    os.chmod(sysbin, 0o555)

    unit_path = home / ".config" / "systemd" / "user" / "sigint101.service"
    argv = [
        "--root",
        str(root),
        "--enroll",
        "--checkout",
        str(checkout),
        "--remote",
        str(bare),
        "--host-python",
        host_py,
        "--site-packages",
        str(src_sp),
        "--unit-path",
        str(unit_path),
        "--desktop-wrapper",
        str(wrapper_path),
        "--runtime-dir",
        str(xdg),
    ]
    try:
        # 1) First run with unwritable wrapper dir MUST refuse before changing ANYTHING.
        rc = updater_cli_main(
            argv,
            _bench_marker_checker=check,
            _service_factory=lambda *a: _make_stub_service_class()(),
            _home=home,
            _kiosk_process_lister=lambda: [],
        )
        err = capsys.readouterr().err
        assert rc == 11
        assert "Permission denied" in err
        assert "sudo install -m 0755" in err
        assert str(wrapper_path) in err
        # Zero side effects!
        assert (checkout / "deploy" / "install.sh").read_bytes() == orig_install
        assert (checkout / "deploy" / "provision.sh").read_bytes() == orig_provision
        assert not root.exists()
        assert not unit_path.exists()

        # 2) Operator runs the exact privileged step (wrapper now holds managed bytes while
        # sysbin stays 0555), then reruns --enroll with the same argv.
        os.chmod(sysbin, 0o755)
        wrapper_path.write_bytes(managed_wrapper_bytes)
        os.chmod(wrapper_path, 0o755)
        os.chmod(sysbin, 0o555)

        rc2 = updater_cli_main(
            argv,
            _bench_marker_checker=check,
            _service_factory=lambda *a: _make_stub_service_class()(),
            _home=home,
            _kiosk_process_lister=lambda: [],
        )
        capsys.readouterr()
        assert rc2 == 0
        enr = json.loads((root / "enrollment.json").read_text(encoding="utf-8"))
        assert enr["shims"]["install.sh"]["original_sha256"] == orig_install_sha
        assert enr["shims"]["provision.sh"]["original_sha256"] == orig_provision_sha
        assert enr["desktop_wrapper"]["managed_sha256"] == managed_wrapper_sha
        assert enr["desktop_wrapper"]["original_sha256"] != managed_wrapper_sha
        assert enr["desktop_wrapper"]["original_sha256"] == legacy_launcher_sha

        # 3) A rerun AFTER shims are already installed MUST still preserve the true originals
        # (never recording the shim bytes as original_sha256).
        install_shim_sha = hashlib.sha256(
            (REPO_ROOT / "deploy" / "managed" / "install-shim.sh").read_bytes()
        ).hexdigest()
        provision_shim_sha = hashlib.sha256(
            (REPO_ROOT / "deploy" / "managed" / "provision-shim.sh").read_bytes()
        ).hexdigest()
        assert (
            hashlib.sha256((checkout / "deploy" / "install.sh").read_bytes()).hexdigest()
            == install_shim_sha
        )
        # Remove enrollment.json to prove rerun does not rely solely on prior enrollment.json!
        (root / "enrollment.json").unlink()
        rc3 = updater_cli_main(
            argv,
            _bench_marker_checker=check,
            _service_factory=lambda *a: _make_stub_service_class()(),
            _home=home,
            _kiosk_process_lister=lambda: [],
        )
        capsys.readouterr()
        assert rc3 == 0
        enr3 = json.loads((root / "enrollment.json").read_text(encoding="utf-8"))
        assert enr3["shims"]["install.sh"]["original_sha256"] == orig_install_sha
        assert enr3["shims"]["install.sh"]["original_sha256"] != install_shim_sha
        assert enr3["shims"]["provision.sh"]["original_sha256"] == orig_provision_sha
        assert enr3["shims"]["provision.sh"]["original_sha256"] != provision_shim_sha
        assert enr3["desktop_wrapper"]["original_sha256"] != managed_wrapper_sha
    finally:
        os.chmod(sysbin, 0o755)


def test_r2_2_preflight_refuses_unwritable_unit_dir_before_any_write(tmp_path, host_rt):
    """R2-2: unwritable systemd unit target also refuses before changing anything."""
    _rt_cached, src_sp, host_py = host_rt
    bare, _work, c1, _c2 = _build_origin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    root = home / ".local" / "state" / "sigint101-updater"
    _marker, check = _make_marker(tmp_path, root)
    checkout = home / "sigint-101"
    _git(home, "clone", str(bare), str(checkout))
    _git(checkout, "checkout", c1)
    orig_install = (checkout / "deploy" / "install.sh").read_bytes()
    (checkout / "data").mkdir(exist_ok=True)
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    (xdg / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")

    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    os.chmod(unit_dir, 0o555)
    try:
        with pytest.raises(EnrollmentError, match="Permission denied"):
            enroll_legacy_fixture(
                repo_root=checkout,
                updater_root=root,
                legacy_checkout=checkout,
                legacy_data_dir=checkout / "data",
                legacy_python=sys.executable,
                source_site_packages=src_sp,
                unit_path=unit_dir / "sigint101.service",
                desktop_wrapper_path=home / ".local" / "bin" / "sigint101-course",
                home=home,
                runtime_dir=xdg,
                host_python=host_py,
                trusted_remote=str(bare),
                legacy_commit=c1,
                kiosk_process_lister=lambda: [],
                bench_marker_checker=check,
            )
        assert (checkout / "deploy" / "install.sh").read_bytes() == orig_install
        assert not root.exists()
    finally:
        os.chmod(unit_dir, 0o755)


# --- R2-3: failed restart after refused post-stop abort is reported, never swallowed -----------


def test_r2_3_abort_poststop_failed_restart_is_reported_never_swallowed(
    tmp_path, host_rt
):
    """R2-3: when abort is refused after stop and restarting the selected pair fails or stays
    unready, abort_unserved reports console-not-ready and --recover rather than swallowing it."""
    fx = _enroll_real_box(tmp_path, host_rt)
    root = fx["root"]
    # First create a real unserved failed op to abort.
    svc = _MinimalService(root, fail_start=True)
    r = apply_update(
        root=root,
        remote_url=str(fx["bare"]),
        pinned_commit=fx["c2"],
        service=svc,
        home=fx["home"],
        ready_timeout_s=0.1,
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert r["abort_eligible"] is True
    op_id = r["operation_id"]

    # Case A: post-stop ENOSPC refusal AND restart's service.start() raises an error.
    class _RaisingRestartService(_MinimalService):
        def start(self) -> None:
            self.calls.append("start")
            raise RuntimeError("systemd start exploded")

    svc_raise = _RaisingRestartService(root, fail_start=True)

    def _enospc_after_drain(name: str) -> None:
        if name == "after_abort_selector_tmp_fsync":
            raise OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(AbortError) as exc_info:
        abort_unserved(
            root=root,
            operation_id=op_id,
            service=svc_raise,
            home=fx["home"],
            ready_timeout_s=0.1,
            kiosk_process_lister=lambda: [],
            on_boundary=_enospc_after_drain,
            bench_marker_checker=fx["bench_check"],
        )
    msg = str(exc_info.value)
    assert "console not ready" in msg.lower()
    assert "--recover" in msg
    assert "start" in svc_raise.calls

    # Case B: post-stop refusal AND restart's service.start() returns, but console never gets ready.
    svc_unready = _MinimalService(root, fail_start=True)
    with pytest.raises(AbortError) as exc_info_b:
        abort_unserved(
            root=root,
            operation_id=op_id,
            service=svc_unready,
            home=fx["home"],
            ready_timeout_s=0.1,
            kiosk_process_lister=lambda: [],
            on_boundary=_enospc_after_drain,
            bench_marker_checker=fx["bench_check"],
        )
    msg_b = str(exc_info_b.value)
    assert "console not ready" in msg_b.lower()
    assert "--recover" in msg_b


# --- R2-5: five guarantees with mutation teeth (MG4, R1b, X1, X5, X13) -------------------------


def test_r2_5_dispatcher_bench_gate_precedes_its_own_flock(tmp_path, host_rt):
    """R2-5 (MG4): holding mutation.lock exclusively, real dispatcher mutating commands MUST fail
    at the bench gate (exit 11), NOT EXIT_GATE_BUSY (75), with zero side effects."""
    fx = _enroll_real_box(tmp_path, host_rt)
    root = fx["root"]
    dispatcher = root / "bin" / "dispatcher.py"
    before = _snapshot_tree(root)

    with acquire_mutation_gate(root, blocking=False, home=fx["home"]):
        for cmd in ("apply", "enroll", "recover", "provision"):
            res = subprocess.run(
                [sys.executable, "-B", str(dispatcher), "--root", str(root), cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert res.returncode == 11, (cmd, res.returncode, res.stderr)
            assert res.returncode != EXIT_GATE_BUSY
            assert "bench" in res.stderr.lower() or "marker" in res.stderr.lower()
            assert "busy" not in res.stderr.lower()

    assert _snapshot_tree(root) == before


def test_r2_5_apply_loads_unit_settings_with_real_systemd_controller(
    tmp_path, host_rt, monkeypatch
):
    """R2-5 (R1b): apply_update with a real SystemdUserController (and no injected unit_settings
    or unit_settings_loader) loads unit settings itself and records settings_fingerprint."""
    fx = _enroll_real_box(tmp_path, host_rt)
    root = fx["root"]
    unit_file = fx["unit_path"]
    unit_file.write_text(
        unit_file.read_text(encoding="utf-8").replace(
            "[Service]\n",
            "[Service]\nEnvironment=SIGINT101_R25_UNIT_VAR=from_real_controller\n",
            1,
        ),
        encoding="utf-8",
    )

    stub_bin = tmp_path / "stub_sd_bin"
    stub_bin.mkdir()
    systemctl = stub_bin / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "show" ]; then\n'
        f'    echo "FragmentPath={unit_file}"\n'
        '    echo "DropInPaths="\n'
        '    echo "Environment=SIGINT101_R25_UNIT_VAR=from_real_controller"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    os.chmod(systemctl, 0o755)
    monkeypatch.setenv("PATH", f"{stub_bin}:{os.environ.get('PATH', '')}")

    class _StubbedRealController(SystemdUserController):
        """Real SystemdUserController subclass whose runtime probes drive the local fixture."""

        def __init__(self, unit_name: str, *, host: str, port: int) -> None:
            super().__init__(unit_name, host=host, port=port)
            self._delegate = _MinimalService(root)

        def stop(self, timeout_s: float = 15.0) -> None:
            self._delegate.stop(timeout_s)

        def unit_state(self) -> tuple[str, str, int]:
            return self._delegate.unit_state()

        def get_cgroup_pids(self) -> list[int]:
            return self._delegate.get_cgroup_pids()

        def is_listener_active(self) -> bool:
            return self._delegate.is_listener_active()

        def kill_cgroup(self) -> None:
            self._delegate.kill_cgroup()

        def reset_failed(self) -> None:
            self._delegate.reset_failed()

        def start(self) -> None:
            self._delegate.start()

        def is_http_ready(self) -> bool:
            return self._delegate.is_http_ready()

    svc = _StubbedRealController("sigint101.service", host="127.0.0.1", port=8008)
    assert isinstance(svc, SystemdUserController)

    # Deliberately omit unit_settings and unit_settings_loader!
    res = apply_update(
        root=root,
        remote_url=str(fx["bare"]),
        pinned_commit=fx["c2"],
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert res["status"] == "ready"
    selector = json.loads((root / "current.json").read_text(encoding="utf-8"))
    expected_settings = parse_systemd_unit_config(unit_file.read_text(encoding="utf-8"))
    expected_fp = compute_settings_fingerprint(expected_settings.unit_only())
    assert selector.get("settings_fingerprint") == expected_fp


def test_r2_5_served_marker_hardlink_is_uncertain_and_blocks_abort(tmp_path, host_rt):
    """R2-5 (X1): a hardlinked served.json (st_nlink > 1) reads 'uncertain' and blocks abort."""
    fx = _enroll_real_box(tmp_path, host_rt)
    root = fx["root"]
    svc = _MinimalService(root, fail_start=True)
    r = apply_update(
        root=root,
        remote_url=str(fx["bare"]),
        pinned_commit=fx["c2"],
        service=svc,
        home=fx["home"],
        ready_timeout_s=0.1,
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    op_id = r["operation_id"]
    rel_id = r["release_id"]
    gen_id = r["generation_id"]

    # Plant a valid-looking served.json AND a hardlink to it (st_nlink == 2).
    marker = root / "operations" / op_id / "served.json"
    marker.write_text(
        json.dumps(
            {
                "format_version": 1,
                "operation_id": op_id,
                "release_id": rel_id,
                "generation_id": gen_id,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)
    extra = tmp_path / "hardlink_served.json"
    os.link(marker, extra)
    assert marker.stat().st_nlink == 2

    assert inspect_serving_marker(root, op_id, rel_id, gen_id) == "uncertain"

    svc.calls.clear()
    with pytest.raises(AbortError, match="uncertain"):
        abort_unserved(
            root=root,
            operation_id=op_id,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    assert "stop" not in svc.calls


def test_r2_5_launcher_refsyncs_existing_serving_marker_and_parent_dir(tmp_path, monkeypatch):
    """R2-5 (X5): launcher._inspect_and_sync_serving_marker fsyncs BOTH served.json and op dir."""
    launcher = _load_launcher_module()
    root = tmp_path / "root"
    op_dir = root / "operations" / "op-x5"
    op_dir.mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(root / "operations", 0o700)
    os.chmod(op_dir, 0o700)
    marker = op_dir / "served.json"
    marker.write_text(
        json.dumps(
            {
                "format_version": 1,
                "operation_id": "op-x5",
                "release_id": "rel-1",
                "generation_id": "gen-1",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)

    synced_inodes: list[int] = []
    real_fsync = os.fsync

    def _spy_fsync(fd: int) -> None:
        synced_inodes.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy_fsync)
    state = launcher._inspect_and_sync_serving_marker(
        root,
        operation_id="op-x5",
        release_id="rel-1",
        generation_id="gen-1",
    )
    assert state == "served"
    assert marker.stat().st_ino in synced_inodes, "served.json fd was not re-fsynced"
    assert op_dir.stat().st_ino in synced_inodes, "operations/<op> dir fd was not re-fsynced"


def test_r2_5_launcher_refuses_serving_marker_generation_mismatch(tmp_path, host_rt):
    """R2-5 (X13): launcher refuses an existing served.json whose generation_id differs."""
    launcher = _load_launcher_module()
    root = tmp_path / "root"
    op_dir = root / "operations" / "op-x13"
    op_dir.mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(root / "operations", 0o700)
    os.chmod(op_dir, 0o700)
    marker = op_dir / "served.json"
    marker.write_text(
        json.dumps(
            {
                "format_version": 1,
                "operation_id": "op-x13",
                "release_id": "rel-1",
                "generation_id": "gen-WRONG",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)

    with pytest.raises(RuntimeError, match="does not match current.json"):
        launcher._inspect_and_sync_serving_marker(
            root,
            operation_id="op-x13",
            release_id="rel-1",
            generation_id="gen-1",
        )
