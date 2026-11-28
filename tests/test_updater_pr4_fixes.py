# ABOUTME: Regression tests for PR 4 build review findings F1-F14, NITs, and gate proofs.
# ABOUTME: Covers real CLI/systemd driver, host_python, lock-free restarts, parity, F8/F9/F10/F12.

from __future__ import annotations

import fcntl
import hashlib
import http.server
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sigint101.bench_gate import BenchGateError, require_bench_marker
from sigint101.enrollment import enroll_legacy_fixture
from sigint101.exam import ExamStore
from sigint101.locks import (
    EXIT_GATE_BUSY,
    acquire_mutation_gate,
    acquire_shared_gate,
    acquire_writer_lock,
)
from sigint101.process_checks import (
    KioskPageOpenError,
    SystemdUserController,
)
from sigint101.progress import Progress
from sigint101.release_prep import (
    DEFAULT_MAX_EXPORT_BYTES,
    DEFAULT_MAX_PACK_BYTES,
    PolicyRefusalError,
    ReleasePrepError,
    SpaceBudgetError,
    estimate_apply_budget_bytes,
    fetch_and_export_release,
    verify_release_manifest_integrity,
)
from sigint101.runtime import RuntimeCompatibilityError, probe_python_abi, stage_sealed_runtime
from sigint101.trial import run_private_trial
from sigint101.updater import (
    AbortError,
    UpdaterError,
    abort_unserved,
    apply_update,
    check_status,
    inspect_serving_marker,
    recover,
)
from sigint101.updater_cli import main as updater_cli_main
from sigint101.validation import (
    UnitSettings,
    compute_settings_fingerprint,
    parse_systemd_unit_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGED_DIR = REPO_ROOT / "deploy" / "managed"
BASH = shutil.which("bash")

_RUNTIME_TOP_ENTRIES = (
    "_yaml",
    "annotated_doc",
    "annotated_types",
    "anyio",
    "click",
    "fastapi",
    "h11",
    "idna",
    "jinja2",
    "markupsafe",
    "opentelemetry",
    "pydantic",
    "pydantic_core",
    "sniffio",
    "starlette",
    "typing_extensions.py",
    "typing_inspection",
    "uvicorn",
    "watchfiles",
    "yaml",
)


def _chmod_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for cur, dirs, files in os.walk(root, topdown=True, followlinks=False):
        cp = Path(cur)
        try:
            os.chmod(cp, 0o700)
        except OSError:
            pass
        for d in dirs:
            try:
                os.chmod(cp / d, 0o700)
            except OSError:
                pass
        for f in files:
            fp = cp / f
            if not fp.is_symlink():
                try:
                    os.chmod(fp, 0o600)
                except OSError:
                    pass


@pytest.fixture(autouse=True)
def _cleanup_readonly_tmp_path(tmp_path: Path):
    yield
    _chmod_tree_writable(tmp_path)


@pytest.fixture(scope="module")
def sealed_host_runtime(tmp_path_factory):
    """Stage a real sealed runtime from .venv site-packages for host_python -B -I -S."""
    base = tmp_path_factory.mktemp("sealed_rt_pr4")
    src_sp = base / "sp_src"
    src_sp.mkdir()
    venv_sp = (
        Path(sys.prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
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
    host_py = Path("/usr/bin/python3")
    exe = str(host_py) if host_py.exists() else sys.executable
    abi = probe_python_abi(exe)
    rt_dir = base / "rt-real"
    stage_sealed_runtime(src_sp, rt_dir, runtime_id="rt-real", python_abi=abi)
    try:
        yield rt_dir, src_sp, exe
    finally:
        _chmod_tree_writable(base)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(cwd),
    }
    res = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, f"git {' '.join(args)} failed: {res.stderr}"
    return res.stdout.strip()


def _build_origin(tmp_path: Path) -> tuple[Path, Path, str, str]:
    work = tmp_path / "git_work"
    bare = tmp_path / "origin.git"
    work.mkdir()
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")
    _git(work, "init", "-b", "main")
    _git(work, "remote", "add", "origin", str(bare))
    shutil.copytree(
        REPO_ROOT / "sigint101",
        work / "sigint101",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copytree(REPO_ROOT / "lesson_packs", work / "lesson_packs")
    shutil.copytree(
        REPO_ROOT / "deploy",
        work / "deploy",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(REPO_ROOT / "requirements.txt", work / "requirements.txt")
    (work / "data").mkdir()
    (work / "data" / ".gitkeep").write_bytes(b"")
    (work / "COURSE_REV").write_text("v1\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "v1")
    c1 = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")

    (work / "COURSE_REV").write_text("v2\n", encoding="utf-8")
    _git(work, "add", "COURSE_REV")
    _git(work, "commit", "-m", "v2")
    c2 = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    return bare, work, c1, c2


def _make_marker(
    tmp_path: Path, root: Path, *, machine_id: str = "bench-mid-1"
) -> tuple[Path, object]:
    mdir = tmp_path / "_bench_etc"
    mdir.mkdir(mode=0o700, exist_ok=True)
    mpath = mdir / "bench-updater-allow.json"
    mpath.write_text(
        json.dumps(
            {
                "format_version": 1,
                "purpose": "sigint101-pr4-disposable-bench",
                "hostname": socket.gethostname(),
                "machine_id": machine_id,
                "updater_root": str(root.resolve()),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(mpath, 0o600)

    def checker(r):
        return require_bench_marker(
            r, _marker_path=mpath, _expected_uid=os.geteuid(), _machine_id=machine_id
        )

    return mpath, checker


def _enroll_real_box(
    tmp_path: Path, sealed_host_runtime, *, prestage_runtime: bool = True
) -> dict:
    rt_dir_cached, src_sp, host_py = sealed_host_runtime
    bare, work, c1, c2 = _build_origin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    root = home / ".local" / "state" / "sigint101-updater"
    marker, bench_check = _make_marker(tmp_path, root)

    checkout = home / "sigint-101"
    _git(home, "clone", str(bare), str(checkout))
    _git(checkout, "checkout", c1)
    data_dir = checkout / "data"
    data_dir.mkdir(exist_ok=True)
    os.chmod(data_dir, 0o700)
    p = Progress(data_dir / "progress.db")
    p.mark_opened(1)
    p.close()
    e = ExamStore(data_dir / "exam.db")
    e.close()

    # Fake legacy venv python path distinct from host_py to test F2.
    legacy_venv_bin = checkout / ".venv" / "bin"
    legacy_venv_bin.mkdir(parents=True)
    legacy_py = legacy_venv_bin / "python"
    legacy_py.symlink_to(sys.executable)

    rt_dir = tmp_path / "xdg_rt"
    rt_dir.mkdir()
    (rt_dir / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")

    unit_path = home / ".config" / "systemd" / "user" / "sigint101.service"
    wrapper = tmp_path / "bin" / "sigint101-course"

    if prestage_runtime:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        (root / "runtimes").mkdir(mode=0o700, exist_ok=True)
        os.chmod(root / "runtimes", 0o700)
        shutil.copytree(rt_dir_cached, root / "runtimes" / "rt-0001")

    enroll_legacy_fixture(
        repo_root=REPO_ROOT,
        updater_root=root,
        legacy_checkout=checkout,
        legacy_data_dir=data_dir,
        legacy_python=legacy_py,
        source_site_packages=src_sp,
        unit_path=unit_path,
        desktop_wrapper_path=wrapper,
        home=home,
        runtime_dir=rt_dir,
        host_python=host_py,
        kiosk_process_lister=lambda: [],
        reload_systemd=lambda: None,
        drain_old_service=lambda: None,
        bench_marker_checker=bench_check,
    )
    return {
        "bare": bare,
        "work": work,
        "c1": c1,
        "c2": c2,
        "home": home,
        "root": root,
        "checkout": checkout,
        "data_dir": data_dir,
        "legacy_py": legacy_py,
        "host_py": host_py,
        "src_sp": src_sp,
        "unit_path": unit_path,
        "wrapper": wrapper,
        "xdg_rt": rt_dir,
        "marker": marker,
        "bench_check": bench_check,
    }


class _MinimalService:
    def __init__(self, root: Path, *, on_start=None, fail_start: bool = False):
        self.root = root
        self.active = "active"
        self.sub = "running"
        self.pid = 4200
        self.listener = True
        self.on_start = on_start
        self.fail_start = fail_start
        self.calls: list[str] = []

    def stop(self, timeout_s: float = 15.0) -> None:
        self.calls.append("stop")
        self.active = "inactive"
        self.sub = "dead"
        self.pid = 0
        self.listener = False

    def unit_state(self) -> tuple[str, str, int]:
        return self.active, self.sub, self.pid

    def get_cgroup_pids(self) -> list[int]:
        return []

    def is_listener_active(self) -> bool:
        return self.listener

    def kill_cgroup(self) -> None:
        self.calls.append("kill_cgroup")

    def reset_failed(self) -> None:
        self.calls.append("reset_failed")

    def start(self) -> None:
        self.calls.append("start")
        if self.on_start is not None:
            self.on_start()
        if self.fail_start:
            self.active = "failed"
            self.sub = "failed"
            self.pid = 0
            self.listener = False
            return
        sel = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        if sel["mode"] == "managed":
            mpath = self.root / "operations" / sel["operation_id"] / "served.json"
            mpath.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(mpath.parent, 0o700)
            mpath.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation_id": sel["operation_id"],
                        "release_id": sel["release_id"],
                        "generation_id": sel["generation_id"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(mpath, 0o600)
        self.active = "active"
        self.sub = "running"
        self.pid = 5200
        self.listener = True

    def is_http_ready(self) -> bool:
        return self.active == "active" and self.listener


# --- F1: SystemdUserController full ServiceAdapter + enrollment metadata + real CLI driver -----


def test_f1_systemd_user_controller_satisfies_service_adapter_and_minimal_env(
    tmp_path, monkeypatch
):
    port = _free_port()
    status_box = {"code": 200}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status_box["code"] if self.path == "/api/hardware" else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        ctl = SystemdUserController("sigint101.service", host="127.0.0.1", port=port)
        assert ctl.is_listener_active() is True
        assert ctl.is_http_ready() is True
        status_box["code"] = 503
        assert ctl.is_http_ready() is False
    finally:
        srv.shutdown()
        srv.server_close()

    assert ctl.is_listener_active() is False
    assert ctl.is_http_ready() is False

    # Stub systemctl to verify get_cgroup_pids() and minimal env (N11).
    env_dump = tmp_path / "env.txt"
    stub_dir = tmp_path / "stub_bin"
    stub_dir.mkdir()
    cg_dir = tmp_path / "fake_cg"
    cg_dir.mkdir()
    (cg_dir / "cgroup.procs").write_text("77\n88\n", encoding="utf-8")
    stub = stub_dir / "systemctl"
    stub.write_text(
        f"#!{BASH}\n"
        f"env > {env_dump}\n"
        f"echo 'ControlGroup={cg_dir}'\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SECRET_SHOULD_NOT_LEAK", "topsecret")
    monkeypatch.setenv("PYTHONPATH", "/tmp/bad")
    ctl2 = SystemdUserController("sigint101.service", host="127.0.0.1", port=port)
    assert ctl2.get_cgroup_pids(cgroup_root="/") == [77, 88]
    leaked = env_dump.read_text(encoding="utf-8")
    assert "SECRET_SHOULD_NOT_LEAK" not in leaked
    assert "PYTHONPATH=" not in leaked


def test_f1_enrollment_records_apply_metadata_without_hand_edits(tmp_path, sealed_host_runtime):
    fx = _enroll_real_box(tmp_path, sealed_host_runtime, prestage_runtime=False)
    enr = json.loads((fx["root"] / "enrollment.json").read_text(encoding="utf-8"))
    assert enr["trusted_remote"] == str(fx["bare"])
    assert enr["legacy_commit"] == fx["c1"]
    assert enr["requirements_sha256"] == hashlib.sha256(
        (fx["checkout"] / "requirements.txt").read_bytes()
    ).hexdigest()
    assert enr["host_python"] == fx["host_py"]
    assert enr["legacy_python"] == str(fx["legacy_py"])

    # Apply succeeds directly on enroll_legacy_fixture's output with zero hand edits!
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


def test_f1_apply_loads_unit_settings_when_omitted(tmp_path, sealed_host_runtime):
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    svc = _MinimalService(fx["root"])
    loaded: list[str] = []
    unit_cfg = parse_systemd_unit_config('[Service]\nEnvironment="FOO=bar"\n')

    def _loader(unit_name: str) -> UnitSettings:
        loaded.append(unit_name)
        return unit_cfg

    res = apply_update(
        root=fx["root"],
        remote_url=str(fx["bare"]),
        pinned_commit=fx["c2"],
        service=svc,
        home=fx["home"],
        unit_settings=None,
        unit_settings_loader=_loader,
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert res["status"] == "ready"
    assert loaded == ["sigint101.service"]
    sel = json.loads((fx["root"] / "current.json").read_text(encoding="utf-8"))
    assert sel["settings_fingerprint"] == compute_settings_fingerprint(unit_cfg.unit_only())


def test_f1_cli_drives_real_apply_and_recover_and_abort(
    tmp_path, sealed_host_runtime, capsys
):
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    svc = _MinimalService(fx["root"])
    unit_cfg = parse_systemd_unit_config('[Service]\nEnvironment="SIG_BENCH=1"\n')

    rc = updater_cli_main(
        ["--root", str(fx["root"]), "--apply", "--commit", fx["c2"]],
        _bench_marker_checker=fx["bench_check"],
        _service_factory=lambda _host, _port, _unit: svc,
        _home=fx["home"],
        _kiosk_process_lister=lambda: [],
        _unit_settings_loader=lambda _u: unit_cfg,
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "ready" in out
    assert fx["c2"][:7] in out

    # --recover via CLI works and returns 0.
    rc_rec = updater_cli_main(
        ["--root", str(fx["root"]), "--recover"],
        _bench_marker_checker=fx["bench_check"],
        _service_factory=lambda _host, _port, _unit: svc,
        _home=fx["home"],
        _kiosk_process_lister=lambda: [],
    )
    assert rc_rec == 0


# --- F2: Managed mode & trial use enrolled host_python, never legacy .venv python --------------


def test_f2_managed_conversion_uses_host_python_not_legacy_venv(tmp_path, sealed_host_runtime):
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    sel_legacy = json.loads((fx["root"] / "current.json").read_text(encoding="utf-8"))
    assert sel_legacy["python_executable"] == str(fx["legacy_py"])
    assert str(fx["legacy_py"]) != fx["host_py"]

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
    sel_managed = json.loads((fx["root"] / "current.json").read_text(encoding="utf-8"))
    assert sel_managed["python_executable"] == fx["host_py"]
    assert ".venv" not in sel_managed["python_executable"]


# --- F3: Pre-commit restart runs AFTER unlocking BOTH locks and verifies readiness -------------


def test_f3_precommit_restart_unlocks_both_locks_before_start_and_checks_readiness(
    tmp_path, sealed_host_runtime
):
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    root = fx["root"]
    locks_free_at_start: list[tuple[bool, bool]] = []

    def _probe_locks_on_start() -> None:
        mut_free = False
        wr_free = False
        try:
            with acquire_mutation_gate(root, blocking=False, home=fx["home"]):
                mut_free = True
        except Exception:
            pass
        try:
            with acquire_writer_lock(root, blocking=False, home=fx["home"]):
                wr_free = True
        except Exception:
            pass
        locks_free_at_start.append((mut_free, wr_free))

    svc = _MinimalService(root, on_start=_probe_locks_on_start)

    # Trigger a pre-commit failure AFTER service stop (e.g. post-stop space shortfall).
    calls = {"n": 0}

    class _SV:
        def __init__(self, avail):
            self.f_frsize = 1
            self.f_bavail = avail

    def _tight_space(_p):
        calls["n"] += 1
        return _SV(10 * 1024 * 1024 * 1024) if calls["n"] == 1 else _SV(1024)

    with pytest.raises(SpaceBudgetError):
        apply_update(
            root=root,
            remote_url=str(fx["bare"]),
            pinned_commit=fx["c2"],
            service=svc,
            home=fx["home"],
            statvfs_fn=_tight_space,
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    assert locks_free_at_start == [(True, True)], (
        f"both locks must be released BEFORE pre-commit restart! observed={locks_free_at_start}"
    )

    # If the pre-commit restart fails to come back ready, report explicitly with --recover.
    svc2 = _MinimalService(root, fail_start=True)
    calls["n"] = 0
    with pytest.raises(UpdaterError, match="--recover"):
        apply_update(
            root=root,
            remote_url=str(fx["bare"]),
            pinned_commit=fx["c2"],
            service=svc2,
            home=fx["home"],
            statvfs_fn=_tight_space,
            ready_timeout_s=0.1,
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )


# --- F4 & F5: Abort post-stop non-AbortError restart after unlock + retry interrupted abort ----


def test_f4_and_f5_abort_post_stop_restart_and_interrupted_abort_retry(
    tmp_path, sealed_host_runtime, monkeypatch
):
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    root = fx["root"]

    # First apply fails public startup (unserved -> abort eligible).
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

    # F4: inject a non-AbortError (RuntimeCompatibilityError) AFTER drain during abort.
    locks_free: list[tuple[bool, bool]] = []

    def _check_locks():
        m = w = False
        try:
            with acquire_mutation_gate(root, blocking=False, home=fx["home"]):
                m = True
        except Exception:
            pass
        try:
            with acquire_writer_lock(root, blocking=False, home=fx["home"]):
                w = True
        except Exception:
            pass
        locks_free.append((m, w))

    svc.fail_start = False
    svc.on_start = _check_locks
    validate_calls = {"n": 0}
    import sigint101.updater as upd_mod

    orig_val = upd_mod._validate_prior_selector_for_abort

    def _boom_on_second(r_arg, old_sel):
        validate_calls["n"] += 1
        if validate_calls["n"] == 2:
            raise RuntimeCompatibilityError("simulated post-stop runtime error")
        return orig_val(r_arg, old_sel)

    monkeypatch.setattr(upd_mod, "_validate_prior_selector_for_abort", _boom_on_second)
    with pytest.raises(RuntimeCompatibilityError, match="simulated post-stop"):
        abort_unserved(
            root=root,
            operation_id=op_id,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    assert locks_free == [(True, True)], (
        "abort post-stop refusal must release both locks and restart"
    )
    assert svc.active == "active"

    # Undo patch and simulate interrupted abort (crash right after replace, service stopped).
    monkeypatch.setattr(upd_mod, "_validate_prior_selector_for_abort", orig_val)
    svc.on_start = None
    # Remove served.json written when restarting above so abort stays eligible.
    mpath = root / "operations" / op_id / "served.json"
    if mpath.exists():
        mpath.unlink()

    class _Crash(RuntimeError):
        pass

    def _crash_after_replace(name: str):
        if name == "after_abort_selector_replace_before_parent_sync":
            raise _Crash("power cut")

    with pytest.raises(_Crash):
        abort_unserved(
            root=root,
            operation_id=op_id,
            service=svc,
            home=fx["home"],
            on_boundary=_crash_after_replace,
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    assert svc.active == "inactive"

    # F5: retrying the exact same abort command finishes recovery and starts the restored pair!
    retried = abort_unserved(
        root=root,
        operation_id=op_id,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert retried["status"] == "ready"
    assert svc.active == "active"


# --- F6 & N1: Launcher enforces R2-1 settings parity before served.json and walks via dir_fd ---


def test_f6_launcher_enforces_r2_1_parity_before_serving_marker_only(
    tmp_path, sealed_host_runtime, monkeypatch
):
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    root = fx["root"]
    unit_a = parse_systemd_unit_config('[Service]\nEnvironment="FOO=alpha"\n')
    svc = _MinimalService(root, fail_start=True)
    res = apply_update(
        root=root,
        remote_url=str(fx["bare"]),
        pinned_commit=fx["c2"],
        service=svc,
        home=fx["home"],
        unit_settings=unit_a,
        ready_timeout_s=0.1,
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    op_id = res["operation_id"]
    sel = json.loads((root / "current.json").read_text(encoding="utf-8"))
    assert sel["settings_fingerprint"] == compute_settings_fingerprint(unit_a.unit_only())

    # Stub systemctl returning DRIFTED unit env FOO=beta!
    stub_dir = tmp_path / "stub_sys"
    stub_dir.mkdir()
    unit_file = tmp_path / "drifted.service"
    unit_file.write_text('[Service]\nEnvironment="FOO=beta"\n', encoding="utf-8")
    stub = stub_dir / "systemctl"
    stub.write_text(
        f"#!{BASH}\n"
        f'if [ "$2" = "show-environment" ]; then echo "LANG=C.UTF-8"; exit 0; fi\n'
        f"echo 'FragmentPath={unit_file}'\n"
        f"echo 'DropInPaths='\n"
        f"echo 'Environment=FOO=beta'\n"
        f"echo 'EnvironmentFiles='\n"
        f"echo 'UnsetEnvironment='\n"
        f"echo 'DynamicUser=no'\n"
        f"echo 'PrivateTmp=no'\n"
        f"echo 'RootDirectory='\n"
        f"echo 'User='\n"
        f"echo 'Group='\n"
        f"echo 'ProtectSystem=no'\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"
    ctrl_id = json.loads((root / "control.json").read_text(encoding="utf-8"))["control_id"]
    ctrl_dir = root / "control" / ctrl_id
    env["PYTHONPATH"] = str(ctrl_dir)

    # 1) served.json ABSENT + drifted settings -> launcher MUST refuse before exec!
    proc = subprocess.run(
        [
            fx["host_py"],
            "-B",
            str(ctrl_dir / "deploy" / "managed" / "launcher.py"),
            "--root",
            str(root),
            "--control-dir",
            str(ctrl_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 1
    assert "parity" in proc.stderr.lower() or "settings" in proc.stderr.lower(), proc.stderr

    # 2) N1: hardlinked served.json MUST be refused!
    marker = root / "operations" / op_id / "served.json"
    marker.write_text(
        json.dumps(
            {
                "format_version": 1,
                "operation_id": op_id,
                "release_id": sel["release_id"],
                "generation_id": sel["generation_id"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)
    hardlink = root / "operations" / op_id / "served_alias.json"
    os.link(marker, hardlink)
    proc_hl = subprocess.run(
        [
            fx["host_py"],
            "-B",
            str(ctrl_dir / "deploy" / "managed" / "launcher.py"),
            "--root",
            str(root),
            "--control-dir",
            str(ctrl_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc_hl.returncode == 1
    hardlink.unlink()

    # 3) R2-1: valid served.json PRESENT -> drifted settings MUST NOT refuse!
    # Start launcher and confirm it boots service_entry and answers /api/hardware 200!
    port = _free_port()
    sel["port"] = port
    (root / "current.json").write_text(json.dumps(sel, indent=2) + "\n", encoding="utf-8")
    p_srv = subprocess.Popen(
        [
            fx["host_py"],
            "-B",
            str(ctrl_dir / "deploy" / "managed" / "launcher.py"),
            "--root",
            str(root),
            "--control-dir",
            str(ctrl_dir),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ctl = SystemdUserController("sigint101.service", host="127.0.0.1", port=port)
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and not ctl.is_http_ready():
            if p_srv.poll() is not None:
                break
            time.sleep(0.1)
        err_msg = p_srv.stderr.read() if p_srv.poll() is not None else "timeout"
        assert ctl.is_http_ready() is True, err_msg
    finally:
        p_srv.terminate()
        p_srv.wait(timeout=5)


# --- F7: Bench marker machine_id fail-closed + absolute canonical root in gate & dispatcher ----


def test_f7_bench_gate_and_dispatcher_host_binding(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    marker_dir = tmp_path / "bench_dir"
    marker_dir.mkdir(mode=0o700)
    marker = marker_dir / "bench-updater-allow.json"

    def _write(payload: dict):
        marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.chmod(marker, 0o600)

    # 1) Missing host machine-id MUST fail closed (not silently skip check!).
    import sigint101.bench_gate as bg_mod

    monkeypatch.setattr(bg_mod, "_read_host_machine_id", lambda: None)
    _write(
        {
            "format_version": 1,
            "purpose": "sigint101-pr4-disposable-bench",
            "hostname": socket.gethostname(),
            "machine_id": "any-id",
            "updater_root": str(root.resolve()),
        }
    )
    with pytest.raises(BenchGateError, match="machine"):
        require_bench_marker(root, _marker_path=marker, _expected_uid=os.geteuid())

    # 2) Relative updater_root MUST be refused even if CWD makes Path(".").resolve() == root!
    monkeypatch.chdir(root)
    _write(
        {
            "format_version": 1,
            "purpose": "sigint101-pr4-disposable-bench",
            "hostname": socket.gethostname(),
            "machine_id": "mid-1",
            "updater_root": ".",
        }
    )
    with pytest.raises(BenchGateError, match="absolute|canonical"):
        require_bench_marker(
            root, _marker_path=marker, _expected_uid=os.geteuid(), _machine_id="mid-1"
        )

    # 3) Parent dir mode 0o770 MUST be refused!
    _write(
        {
            "format_version": 1,
            "purpose": "sigint101-pr4-disposable-bench",
            "hostname": socket.gethostname(),
            "machine_id": "mid-1",
            "updater_root": str(root.resolve()),
        }
    )
    os.chmod(marker_dir, 0o770)
    with pytest.raises(BenchGateError, match="permissions"):
        require_bench_marker(
            root, _marker_path=marker, _expected_uid=os.geteuid(), _machine_id="mid-1"
        )
    os.chmod(marker_dir, 0o700)

    # 4) Hardlinked bench marker MUST be refused!
    alias = marker_dir / "alias.json"
    os.link(marker, alias)
    with pytest.raises(BenchGateError, match="hardlink"):
        require_bench_marker(
            root, _marker_path=marker, _expected_uid=os.geteuid(), _machine_id="mid-1"
        )
    alias.unlink()

    # 5) dispatcher._verify_bench_marker checks machine_id AND canonical absolute root!
    spec = importlib.util.spec_from_file_location("_disp_f7", MANAGED_DIR / "dispatcher.py")
    assert spec and spec.loader
    disp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(disp)

    monkeypatch.setattr(disp, "DEFAULT_BENCH_MARKER_PATH", marker)
    orig_fstat = os.fstat

    class _RootUidStat:
        def __init__(self, inner):
            self._i = inner
            self.st_uid = 0

        def __getattr__(self, name):
            return getattr(self._i, name)

    monkeypatch.setattr(disp.os, "fstat", lambda fd: _RootUidStat(orig_fstat(fd)))
    # Wrong machine_id must be refused by dispatcher!
    _write(
        {
            "format_version": 1,
            "purpose": "sigint101-pr4-disposable-bench",
            "hostname": socket.gethostname(),
            "machine_id": "definitely-wrong-machine-id",
            "updater_root": str(root.resolve()),
        }
    )
    with pytest.raises(RuntimeError, match="machine|host"):
        disp._verify_bench_marker(root)


# --- F8: Dispatcher isolates CWD shadow + CLI adopts inherited SIGINT101_MUTATION_GATE_FD ------


def test_f8_dispatcher_isolates_cwd_shadow_and_adopts_gate_fd(
    tmp_path, sealed_host_runtime, monkeypatch
):
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    root = fx["root"]

    # 1) Create a malicious shadow sigint101/updater_cli.py in CWD.
    evil_cwd = tmp_path / "evil_cwd"
    (evil_cwd / "sigint101").mkdir(parents=True)
    (evil_cwd / "sigint101" / "__init__.py").write_text("", encoding="utf-8")
    pwn_flag = tmp_path / "pwned.txt"
    (evil_cwd / "sigint101" / "updater_cli.py").write_text(
        f"from pathlib import Path\nPath({str(pwn_flag)!r}).write_text('PWNED')\n",
        encoding="utf-8",
    )

    disp_bin = root / "bin" / "dispatcher.py"
    res = subprocess.run(
        [fx["host_py"], "-B", str(disp_bin), "--root", str(root), "check"],
        cwd=str(evil_cwd),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert not pwn_flag.exists(), "dispatcher allowed CWD sigint101/ to shadow control generation!"
    assert res.returncode in (0, 10), (res.returncode, res.stdout, res.stderr)

    # Also test provision-shim.sh --check from evil_cwd!
    shim_env = os.environ.copy()
    shim_env["SIGINT101_UPDATER_ROOT"] = str(root)
    res_shim = subprocess.run(
        [BASH, str(fx["checkout"] / "deploy" / "provision.sh"), "--check"],
        cwd=str(evil_cwd),
        env=shim_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert not pwn_flag.exists()
    assert res_shim.returncode in (0, 10), (res_shim.returncode, res_shim.stdout, res_shim.stderr)

    # 2) Passing inherited SIGINT101_MUTATION_GATE_FD into CLI --apply adopts it, doesn't deadlock,
    # and releases it BEFORE service.start()!
    mut_lock = root / "locks" / "mutation.lock"
    fd = os.open(os.fspath(mut_lock), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(fd, True)
    monkeypatch.setenv("SIGINT101_MUTATION_GATE_FD", str(fd))

    gate_free_at_start: list[bool] = []

    def _check_gate_free():
        try:
            with acquire_mutation_gate(root, blocking=False, home=fx["home"]):
                gate_free_at_start.append(True)
        except Exception:
            gate_free_at_start.append(False)

    svc = _MinimalService(root, on_start=_check_gate_free)
    rc = updater_cli_main(
        ["--root", str(root), "--apply", "--commit", fx["c2"]],
        _bench_marker_checker=fx["bench_check"],
        _service_factory=lambda _h, _p, _u: svc,
        _home=fx["home"],
        _kiosk_process_lister=lambda: [],
        _unit_settings_loader=lambda _u: UnitSettings(),
    )
    assert rc == 0
    assert gate_free_at_start == [True], "inherited mutation gate FD must be released before start!"


# --- F9: Private trial runs under enrolled sealed runtime with -B -I -S ------------------------


def test_f9_private_trial_runs_under_sealed_runtime(tmp_path, sealed_host_runtime):
    rt_dir, _src_sp, host_py = sealed_host_runtime
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    root = fx["root"]
    rel_dir = root / "releases" / "rel-f9"
    fetch_and_export_release(
        remote_url=str(fx["bare"]),
        trusted_remote=str(fx["bare"]),
        pinned_commit=fx["c2"],
        current_commit=fx["c1"],
        dest_release_dir=rel_dir,
        scratch_git_dir=tmp_path / "sc_git",
        release_id="rel-f9",
    )
    from sigint101.snapshot import copy_generation_snapshot

    gen_dir = root / "data" / "gen-f9"
    copy_generation_snapshot(
        fx["data_dir"],
        gen_dir,
        generation_id="gen-f9",
        scratch_dir=tmp_path / "sc_db",
    )

    # 1) Valid sealed runtime succeeds under host_py -B -I -S.
    tr_ok = run_private_trial(
        python_executable=host_py,
        release_root=rel_dir,
        data_dir=gen_dir,
        runtime_dir=rt_dir,
        expected_release_id="rel-f9",
        expected_commit=fx["c2"],
        expected_generation_id="gen-f9",
    )
    assert tr_ok.ready is True, tr_ok.errors

    # 2) Empty/broken sealed runtime MUST fail even though pytest runs inside .venv with fastapi!
    bad_sp = tmp_path / "empty_sp"
    bad_sp.mkdir()
    (bad_sp / "_only.py").write_text("X=1\n", encoding="utf-8")
    bad_rt = tmp_path / "bad_rt"
    stage_sealed_runtime(bad_sp, bad_rt, runtime_id="rt-bad", python_abi=probe_python_abi(host_py))
    tr_bad = run_private_trial(
        python_executable=host_py,
        release_root=rel_dir,
        data_dir=gen_dir,
        runtime_dir=bad_rt,
        expected_release_id="rel-f9",
        expected_commit=fx["c2"],
        expected_generation_id="gen-f9",
    )
    assert tr_bad.ready is False


# --- F11: operations/<op> synced into operations/ before writing intent & prepared -------------


def test_f11_operations_dir_fsynced_before_intent_and_prepared(
    tmp_path, sealed_host_runtime, monkeypatch
):
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    root = fx["root"]
    events: list[tuple[str, str]] = []

    import sigint101.updater as upd_mod

    orig_fsync_dir = upd_mod.fsync_dir
    orig_write_json = upd_mod._write_json_durable

    def _spy_fsync_dir(p):
        events.append(("fsync_dir", str(Path(p).resolve())))
        return orig_fsync_dir(p)

    def _spy_write(path, payload, **kw):
        events.append(("write_start", Path(path).name))
        res = orig_write_json(path, payload, **kw)
        events.append(("write_done", Path(path).name))
        return res

    monkeypatch.setattr(upd_mod, "fsync_dir", _spy_fsync_dir)
    monkeypatch.setattr(upd_mod, "_write_json_durable", _spy_write)

    svc = _MinimalService(root)
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
    intent_idx = events.index(("write_start", "intent.json"))
    ops_syncs_before_intent = [
        e for e in events[:intent_idx] if e == ("fsync_dir", str((root / "operations").resolve()))
    ]
    assert ops_syncs_before_intent, (
        "operations/ must be fsynced after creating operations/<op> and BEFORE writing intent.json!"
    )


# --- F12 & N7: Pack cap enforced DURING git fetch + conservative space budget + mode verify ----


def test_f12_pack_cap_during_git_fetch_budget_alignment_and_mode_check(tmp_path):
    bare, work, c1, _c2 = _build_origin(tmp_path)
    # Add 512 KiB of uncompressible random bytes to exceed a small 128 KiB pack cap.
    (work / "big_blob.bin").write_bytes(os.urandom(512 * 1024))
    _git(work, "add", "big_blob.bin")
    _git(work, "commit", "-m", "oversized")
    c_big = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")

    scratch = tmp_path / "sc_big"
    with pytest.raises(PolicyRefusalError, match="pack|cap|size"):
        fetch_and_export_release(
            remote_url=str(bare),
            trusted_remote=str(bare),
            pinned_commit=c_big,
            current_commit=c1,
            dest_release_dir=tmp_path / "rel_big",
            scratch_git_dir=scratch,
            release_id="rel-big",
            max_pack_bytes=128 * 1024,
        )
    # RLIMIT_FSIZE stops git fetch BEFORE writing the full 512 KiB pack to disk!
    pack_files = list((scratch / "objects").rglob("*"))
    max_written = max((p.stat().st_size for p in pack_files if p.is_file()), default=0)
    assert max_written <= 128 * 1024 + 4096

    # Conservative space budget covers BOTH pack cap and export cap!
    budget = estimate_apply_budget_bytes(source_data_dir=tmp_path)
    assert budget >= DEFAULT_MAX_PACK_BYTES + DEFAULT_MAX_EXPORT_BYTES

    # N7: verify_release_manifest_integrity catches file mode tampering!
    rel_ok = tmp_path / "rel_mode"
    fetch_and_export_release(
        remote_url=str(bare),
        trusted_remote=str(bare),
        pinned_commit=c_big,
        current_commit=None,
        dest_release_dir=rel_ok,
        scratch_git_dir=tmp_path / "sc_mode",
        release_id="rel-mode",
    )
    verify_release_manifest_integrity(rel_ok)
    os.chmod(rel_ok, 0o700)
    os.chmod(rel_ok / "COURSE_REV", 0o600)
    with pytest.raises(ReleasePrepError, match="mode|writable|permission"):
        verify_release_manifest_integrity(rel_ok)


# --- F10: Real-process crash matrix + persistence model + real dispatcher/launcher/service -----


def test_f10_persistence_model_reverts_unsynced_selector_both_outcomes(
    tmp_path, sealed_host_runtime
):
    """Simulate power cut right after os.replace(current.json) before fsync_dir(ROOT):
    verify BOTH outcomes (old selector returns OR new selector survives) recover cleanly."""
    for outcome in ("old_returns", "new_survives"):
        sub = tmp_path / outcome
        sub.mkdir()
        fx = _enroll_real_box(sub, sealed_host_runtime)
        root = fx["root"]
        old_sel_bytes = (root / "current.json").read_bytes()
        svc = _MinimalService(root)

        class _PowerCut(BaseException):
            pass

        def _cut(name: str):
            if name == "after_selector_replace_before_parent_sync":
                if outcome == "old_returns":
                    # Directory entry rename was not synced: old current.json returns on boot!
                    (root / "current.json").write_bytes(old_sel_bytes)
                raise _PowerCut("cut")

        with pytest.raises(_PowerCut):
            apply_update(
                root=root,
                remote_url=str(fx["bare"]),
                pinned_commit=fx["c2"],
                service=svc,
                home=fx["home"],
                on_boundary=_cut,
                kiosk_process_lister=lambda: [],
                bench_marker_checker=fx["bench_check"],
            )

        rec = recover(
            root=root,
            service=svc,
            home=fx["home"],
            bench_marker_checker=fx["bench_check"],
        )
        assert rec["status"] == "ready"
        if outcome == "old_returns":
            assert rec["mode"] == "legacy"
            assert rec["incomplete_intents"], "uncommitted intent should be reported"
        else:
            assert rec["mode"] == "managed"


def test_f10_real_process_sigkill_crash_matrix(tmp_path, sealed_host_runtime):
    """Kill a real subprocess updater with SIGKILL at crash boundaries; verify recover()."""
    for boundary in (
        "after_intent_sync",
        "after_service_stop",
        "after_prepared_sync",
        "after_selector_replace_before_parent_sync",
        "after_selector_parent_sync",
    ):
        sub = tmp_path / f"sigkill_{boundary}"
        sub.mkdir()
        fx = _enroll_real_box(sub, sealed_host_runtime)
        root = fx["root"]
        script = (
            "import json, os, signal\n"
            "from pathlib import Path\n"
            "from sigint101.bench_gate import require_bench_marker\n"
            "from sigint101.updater import apply_update\n"
            "class Svc:\n"
            "    def __init__(self, root):\n"
            "        self.root = Path(root)\n"
            "        self.state = ('active', 'running', 1, True)\n"
            "    def stop(self, _t=15.0):\n"
            "        self.state = ('inactive', 'dead', 0, False)\n"
            "    def unit_state(self): return self.state[:3]\n"
            "    def get_cgroup_pids(self): return []\n"
            "    def is_listener_active(self): return self.state[3]\n"
            "    def kill_cgroup(self): pass\n"
            "    def reset_failed(self): pass\n"
            "    def start(self): self.state = ('active', 'running', 2, True)\n"
            "    def is_http_ready(self): return self.state[0] == 'active'\n"
            f"root = Path({str(root)!r})\n"
            f"marker = Path({str(fx['marker'])!r})\n"
            "def _kill(n):\n"
            f"    if n == {boundary!r}:\n"
            "        os.kill(os.getpid(), signal.SIGKILL)\n"
            "apply_update(\n"
            "    root=root,\n"
            f"    remote_url={str(fx['bare'])!r},\n"
            f"    pinned_commit={fx['c2']!r},\n"
            "    service=Svc(root),\n"
            f"    home=Path({str(fx['home'])!r}),\n"
            "    kiosk_process_lister=lambda: [],\n"
            "    bench_marker_checker=lambda r: require_bench_marker(\n"
            "        r, _marker_path=marker, _expected_uid=os.geteuid(),\n"
            "        _machine_id='bench-mid-1',\n"
            "    ),\n"
            "    on_boundary=_kill,\n"
            ")\n"
        )
        proc = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == -9, f"expected SIGKILL (-9), got {proc.returncode}: {proc.stderr}"

        # Real kernel flock must be released automatically when child received SIGKILL!
        svc_rec = _MinimalService(root)
        svc_rec.active = "inactive"
        svc_rec.pid = 0
        svc_rec.listener = False
        rec = recover(
            root=root,
            service=svc_rec,
            home=fx["home"],
            bench_marker_checker=fx["bench_check"],
        )
        assert rec["status"] == "ready"
        if boundary in ("after_intent_sync", "after_service_stop", "after_prepared_sync"):
            assert rec["mode"] == "legacy"
        else:
            assert rec["mode"] == "managed"


def test_f10_real_dispatcher_launcher_service_entry_apply_and_abort(tmp_path, sealed_host_runtime):
    """Drive real dispatcher -> launcher -> service_entry ASGI process across apply + abort."""
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    root = fx["root"]
    port = _free_port()
    sel = json.loads((root / "current.json").read_text(encoding="utf-8"))
    sel["port"] = port
    (root / "current.json").write_text(json.dumps(sel, indent=2) + "\n", encoding="utf-8")

    class _RealDispatcherService:
        def __init__(self):
            self.proc: subprocess.Popen | None = None
            self.ctl = SystemdUserController("sigint101.service", host="127.0.0.1", port=port)

        def start(self):
            self.proc = subprocess.Popen(
                [
                    fx["host_py"],
                    "-B",
                    str(root / "bin" / "dispatcher.py"),
                    "--root",
                    str(root),
                    "serve",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )

        def stop(self, timeout_s: float = 15.0):
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            self.proc = None

        def unit_state(self) -> tuple[str, str, int]:
            if self.proc is not None and self.proc.poll() is None:
                return "active", "running", self.proc.pid
            return "inactive", "dead", 0

        def get_cgroup_pids(self) -> list[int]:
            if self.proc is not None and self.proc.poll() is None:
                return [self.proc.pid]
            return []

        def is_listener_active(self) -> bool:
            return self.ctl.is_listener_active()

        def kill_cgroup(self) -> None:
            self.stop()

        def reset_failed(self) -> None:
            pass

        def is_http_ready(self) -> bool:
            return self.ctl.is_http_ready()

    svc = _RealDispatcherService()
    try:
        # 1) Real apply (legacy -> managed v2) booted by real dispatcher -> launcher -> entry!
        r2 = apply_update(
            root=root,
            remote_url=str(fx["bare"]),
            pinned_commit=fx["c2"],
            service=svc,
            home=fx["home"],
            ready_timeout_s=15.0,
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
        assert r2["status"] == "ready"
        assert (
            inspect_serving_marker(root, r2["operation_id"], r2["release_id"], r2["generation_id"])
            == "served"
        )

        # 2) Push v3 failing public startup (non-trial) so service_entry exits before served.json!
        app_py = fx["work"] / "sigint101" / "app.py"
        orig_app = app_py.read_text(encoding="utf-8")
        needle = "state.ppm_calibration = PpmCalibration()"
        assert needle in orig_app
        broken = orig_app.replace(
            needle,
            'raise RuntimeError("public-only bench failure")',
            1,
        )
        app_py.write_text(broken, encoding="utf-8")
        _git(fx["work"], "add", "sigint101/app.py")
        _git(fx["work"], "commit", "-m", "v3 public-only fail")
        c3 = _git(fx["work"], "rev-parse", "HEAD")
        _git(fx["work"], "push", "origin", "main")

        r3 = apply_update(
            root=root,
            remote_url=str(fx["bare"]),
            pinned_commit=c3,
            service=svc,
            home=fx["home"],
            ready_timeout_s=4.0,
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
        assert r3["status"] == "committed, console not ready"
        assert r3["abort_eligible"] is True
        assert (
            inspect_serving_marker(root, r3["operation_id"], r3["release_id"], r3["generation_id"])
            == "absent"
        )

        # 3) Abort v3 back to v2 through real dispatcher -> launcher -> service_entry!
        ab = abort_unserved(
            root=root,
            operation_id=r3["operation_id"],
            service=svc,
            home=fx["home"],
            ready_timeout_s=15.0,
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
        assert ab["status"] == "ready"
        assert ab["release_id"] == r2["release_id"]
        assert ab["generation_id"] == r2["generation_id"]
    finally:
        svc.stop()


# --- N2, N4, N5 & X1/X2/X6/X7/X12 guarantee tests ---------------------------------------------


def test_n2_n4_n5_and_extra_guarantee_checks(tmp_path, sealed_host_runtime):
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    root = fx["root"]

    # N2: check_status does NOT report busy (75) when only the SHARED gate (LOCK_SH) is held!
    with acquire_shared_gate(root, blocking=False, home=fx["home"]):
        chk_sh = check_status(
            root=root,
            remote_url=str(fx["bare"]),
            home=fx["home"],
            ls_remote_fn=lambda _u, _r: fx["c1"],
        )
    assert chk_sh["exit_code"] == 0, f"shared gate must not block --check: {chk_sh}"

    # N2: check_status DOES report 75 when exclusive mutation gate (LOCK_EX) is held!
    with acquire_mutation_gate(root, blocking=False, home=fx["home"]):
        chk_ex = check_status(root=root, remote_url=str(fx["bare"]), home=fx["home"])
    assert chk_ex["exit_code"] == EXIT_GATE_BUSY

    # N4: foreign remote refuses BEFORE allocating operations/<op>/intent.json!
    svc = _MinimalService(root)
    ops_dir = root / "operations"
    before_ops = sorted(p.name for p in ops_dir.iterdir()) if ops_dir.is_dir() else []
    with pytest.raises(PolicyRefusalError, match="foreign remote"):
        apply_update(
            root=root,
            remote_url="https://example.invalid/foreign.git",
            pinned_commit=fx["c2"],
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    after_ops = sorted(p.name for p in ops_dir.iterdir()) if ops_dir.is_dir() else []
    assert after_ops == before_ops, "foreign remote must refuse before writing intent.json!"

    # X7: kiosk page opening between release export and stop refuses before service.stop()!
    from sigint101.kiosk import KIOSK_PROFILE

    kiosk_open = {"v": False}

    def _lister():
        if kiosk_open["v"]:
            return [(99, ["chromium", f"--user-data-dir={fx['home'] / KIOSK_PROFILE}"])]
        return []

    svc.calls.clear()

    def _open_kiosk_after_export(name: str) -> None:
        if name == "after_release_export":
            kiosk_open["v"] = True

    with pytest.raises(KioskPageOpenError):
        apply_update(
            root=root,
            remote_url=str(fx["bare"]),
            pinned_commit=fx["c2"],
            service=svc,
            home=fx["home"],
            on_boundary=_open_kiosk_after_export,
            kiosk_process_lister=_lister,
            bench_marker_checker=fx["bench_check"],
        )
    assert "stop" not in svc.calls

    # N5: abort refuses if prepared["new_selector"] does not match current.json!
    svc.fail_start = True
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
    prep_path = root / "operations" / r["operation_id"] / "prepared.json"
    prep = json.loads(prep_path.read_text(encoding="utf-8"))
    prep["new_selector"]["release_id"] = "rel-tampered"
    prep_path.write_text(json.dumps(prep) + "\n", encoding="utf-8")
    with pytest.raises(AbortError, match="prepared|selector"):
        abort_unserved(
            root=root,
            operation_id=r["operation_id"],
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )


# --- F14: Reviewer revert-proof bench-gate tests (adopted from build review) -------------------


def _snapshot_tree(root: Path) -> list[tuple[str, int, bytes]]:
    if not root.exists():
        return []
    out: list[tuple[str, int, bytes]] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        st = p.lstat()
        body = p.read_bytes() if p.is_file() and not p.is_symlink() else b""
        out.append((rel, st.st_mode, body))
    return out


class _ForbiddenService:
    """Service adapter that fails the test immediately if ANY method is invoked."""

    def _boom(self, name: str):
        raise AssertionError(f"bench gate failed: service.{name}() called on non-bench host")

    def stop(self, _timeout_s: float = 15.0) -> None:
        self._boom("stop")

    def unit_state(self) -> tuple[str, str, int]:
        self._boom("unit_state")
        return ("inactive", "dead", 0)

    def get_cgroup_pids(self) -> list[int]:
        self._boom("get_cgroup_pids")
        return []

    def is_listener_active(self) -> bool:
        self._boom("is_listener_active")
        return False

    def kill_cgroup(self) -> None:
        self._boom("kill_cgroup")

    def reset_failed(self) -> None:
        self._boom("reset_failed")

    def start(self) -> None:
        self._boom("start")

    def is_http_ready(self) -> bool:
        self._boom("is_http_ready")
        return False


def test_review_gate_precedes_every_side_effect_on_enrolled_box(tmp_path, sealed_host_runtime):
    """Revert-proof F14 test: every mutating entry refuses before locks, kiosk, files, service."""
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
    root = fx["root"]
    before = _snapshot_tree(root)

    def _kiosk_must_not_run():
        raise AssertionError("kiosk check ran BEFORE bench gate!")

    def _reload_must_not_run():
        raise AssertionError("reload ran BEFORE bench gate!")

    def _drain_must_not_run():
        raise AssertionError("drain ran BEFORE bench gate!")

    # Hold exclusive mutation lock so a lock attempt raises LockBusyError, not BenchGateError.
    with acquire_mutation_gate(root, blocking=False, home=fx["home"]):
        with pytest.raises(BenchGateError):
            apply_update(
                root=root,
                remote_url=str(fx["bare"]),
                pinned_commit=fx["c2"],
                service=_ForbiddenService(),
                home=fx["home"],
                kiosk_process_lister=_kiosk_must_not_run,
            )
        with pytest.raises(BenchGateError):
            recover(
                root=root,
                service=_ForbiddenService(),
                home=fx["home"],
            )
        with pytest.raises(BenchGateError):
            abort_unserved(
                root=root,
                operation_id="op-never",
                service=_ForbiddenService(),
                home=fx["home"],
                kiosk_process_lister=_kiosk_must_not_run,
            )
        with pytest.raises(BenchGateError):
            enroll_legacy_fixture(
                repo_root=REPO_ROOT,
                updater_root=root,
                legacy_checkout=fx["checkout"],
                legacy_data_dir=fx["data_dir"],
                legacy_python=fx["legacy_py"],
                source_site_packages=fx["src_sp"],
                unit_path=fx["unit_path"],
                desktop_wrapper_path=fx["wrapper"],
                home=fx["home"],
                runtime_dir=fx["xdg_rt"],
                host_python=fx["host_py"],
                kiosk_process_lister=_kiosk_must_not_run,
                reload_systemd=_reload_must_not_run,
                drain_old_service=_drain_must_not_run,
            )

    # CLI mutating paths must refuse exit 11 before locks or side effects too.
    for argv in (
        ["--root", str(root), "--enroll"],
        ["--root", str(root), "--apply", "--commit", fx["c2"]],
        ["--root", str(root), "--recover"],
        ["--root", str(root), "--recover", "--abort", "op-never"],
        ["--root", str(root), "provision", "--apply"],
    ):
        assert updater_cli_main(argv) == 11

    assert _snapshot_tree(root) == before


def test_review_abort_and_recover_refuse_before_side_effects_on_a_failed_op(
    tmp_path, sealed_host_runtime
):
    """Revert-proof F14 test: on a real failed unserved op, default gate refuses abort & recover."""
    fx = _enroll_real_box(tmp_path, sealed_host_runtime)
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
    assert r["abort_eligible"] is True
    op_id = r["operation_id"]
    before = _snapshot_tree(root)

    def _kiosk_forbidden():
        raise AssertionError("kiosk before gate")

    # Now without the bench marker (default checker), BOTH abort_unserved and recover MUST refuse
    # with BenchGateError before touching locks, files, or service!
    with acquire_mutation_gate(root, blocking=False, home=fx["home"]):
        with pytest.raises(BenchGateError):
            abort_unserved(
                root=root,
                operation_id=op_id,
                service=_ForbiddenService(),
                home=fx["home"],
                kiosk_process_lister=_kiosk_forbidden,
            )
        with pytest.raises(BenchGateError):
            recover(
                root=root,
                service=_ForbiddenService(),
                home=fx["home"],
            )

    assert _snapshot_tree(root) == before
