# ABOUTME: PR 3 updater tests for stable locks, runtime ABI checks, cgroup drain, start-limit reset,
# ABOUTME: deployed-control manifest, gate-aware launcher/desktop/shims, and enrollment fixtures.

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sigint101.bench_gate import require_bench_marker
from sigint101.enrollment import enroll_legacy_fixture
from sigint101.exam import ExamStore
from sigint101.locks import (
    EXIT_GATE_BUSY,
    LauncherLockSession,
    LockBusyError,
    acquire_mutation_gate,
    acquire_shared_gate,
    acquire_writer_lock,
    ensure_lock_files,
)
from sigint101.migrations import migrate_store_copy
from sigint101.process_checks import (
    KioskPageOpenError,
    ServiceDrainError,
    SystemdUserController,
    is_kiosk_page_open,
    require_kiosk_page_closed,
    start_unit_with_reset,
    wait_service_drained,
)
from sigint101.progress import Progress
from sigint101.runtime import (
    RuntimeCompatibilityError,
    check_runtime_imports,
    probe_python_abi,
    stage_sealed_runtime,
    verify_runtime_compatibility,
)
from sigint101.snapshot import GENERATION_MARKER_FILENAME
from sigint101.validation import write_release_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGED_DIR = REPO_ROOT / "deploy" / "managed"
BASH = shutil.which("bash")


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


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_stub_bin(path: Path, body: str) -> None:
    path.write_text(f"#!{BASH}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _fixture_bench_checker(tmp_path: Path, updater_root: Path):
    marker_dir = tmp_path / "_bench_marker_dir"
    marker_dir.mkdir(mode=0o700, exist_ok=True)
    marker = marker_dir / "bench-updater-allow.json"
    marker.write_text(
        json.dumps(
            {
                "format_version": 1,
                "purpose": "sigint101-pr4-disposable-bench",
                "hostname": socket.gethostname(),
                "machine_id": "fixture-machine-0001",
                "updater_root": str(updater_root.resolve()),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)
    return lambda r: require_bench_marker(
        r,
        _marker_path=marker,
        _expected_uid=os.geteuid(),
        _machine_id="fixture-machine-0001",
    )


def _create_managed_root_fixture(
    tmp_path: Path,
    *,
    mode: str = "managed",
    operation_id: str = "op-0001",
    host: str = "127.0.0.1",
    port: int | None = None,
    sealed_runtime_dir: Path | None = None,
    python_executable: str | None = None,
) -> dict:
    """Create an enrolled ROOT fixture with permanent locks, control generation, and selector."""
    sys.path.insert(0, str(MANAGED_DIR))
    from control_manifest import stage_control_generation

    root = tmp_path / "updater_root"
    root.mkdir(mode=0o700)
    ensure_lock_files(root)

    # Install stable dispatcher into ROOT/bin/dispatcher.py.
    bin_dir = root / "bin"
    bin_dir.mkdir(mode=0o700)
    shutil.copy2(MANAGED_DIR / "dispatcher.py", bin_dir / "dispatcher.py")
    (bin_dir / "dispatcher.py").chmod(0o700)

    # Stage initial control generation and control.json.
    control_dir = root / "control" / "ctrl-0001"
    stage_control_generation(REPO_ROOT, control_dir, control_id="ctrl-0001")
    (root / "control.json").write_text(
        json.dumps({"format_version": 1, "control_id": "ctrl-0001"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    listen_port = port or _free_loopback_port()
    ops_dir = root / "operations" / operation_id
    ops_dir.mkdir(parents=True, mode=0o700)

    if mode == "managed":
        rel_dir = root / "releases" / "rel-0001"
        rel_dir.mkdir(parents=True)
        shutil.copytree(
            REPO_ROOT / "sigint101",
            rel_dir / "sigint101",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        shutil.copytree(REPO_ROOT / "lesson_packs", rel_dir / "lesson_packs")
        write_release_manifest(rel_dir, release_id="rel-0001", commit="aaaa111")

        gen_dir = root / "data" / "gen-0001"
        gen_dir.mkdir(parents=True, mode=0o700)
        migrate_store_copy(
            tmp_path / "none_p.db",
            gen_dir / "progress.db",
            store="progress",
            allow_missing_source=True,
            scratch_dir=tmp_path / "sc_p",
        )
        migrate_store_copy(
            tmp_path / "none_e.db",
            gen_dir / "exam.db",
            store="exam",
            allow_missing_source=True,
            scratch_dir=tmp_path / "sc_e",
        )
        (gen_dir / GENERATION_MARKER_FILENAME).write_text(
            json.dumps({"generation_id": "gen-0001"}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # Use provided real sealed runtime (for full -I -S boot) or minimal marker-only runtime.
        runtime_dir = root / "runtimes" / "rt-0001"
        if sealed_runtime_dir is not None:
            shutil.copytree(sealed_runtime_dir, runtime_dir)
        else:
            sp_src = tmp_path / "empty_sp"
            sp_src.mkdir()
            (sp_src / "_sigint_rt_marker.py").write_text("MARKER = 'sealed'\n", encoding="utf-8")
            abi = probe_python_abi(python_executable or sys.executable)
            stage_sealed_runtime(sp_src, runtime_dir, runtime_id="rt-0001", python_abi=abi)

        selector = {
            "format_version": 1,
            "mode": "managed",
            "operation_id": operation_id,
            "release_id": "rel-0001",
            "generation_id": "gen-0001",
            "runtime_id": "rt-0001",
            "python_executable": python_executable or sys.executable,
            "host": host,
            "port": listen_port,
        }
    else:
        checkout = tmp_path / "legacy_checkout"
        checkout.mkdir()
        shutil.copytree(
            REPO_ROOT / "sigint101",
            checkout / "sigint101",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        shutil.copytree(REPO_ROOT / "lesson_packs", checkout / "lesson_packs")
        data_dir = checkout / "data"
        data_dir.mkdir()
        selector = {
            "format_version": 1,
            "mode": "legacy",
            "operation_id": operation_id,
            "checkout_dir": str(checkout),
            "data_dir": str(data_dir),
            "python_executable": sys.executable,
            "host": host,
            "port": listen_port,
        }

    (root / "current.json").write_text(
        json.dumps(selector, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "root": root,
        "dispatcher": bin_dir / "dispatcher.py",
        "selector": selector,
        "host": host,
        "port": listen_port,
        "operation_id": operation_id,
    }


# --- 1. Locks, non-blocking gate protocol, and writer-FD transfer with CLOEXEC -----------------


def test_locks_gate_writer_transfer_and_cloexec(tmp_path):
    # R2-4: lock initialization refuses ~/.local/share/sigint101.
    fake_home = tmp_path / "home"
    forbidden = fake_home / ".local" / "share" / "sigint101"
    with pytest.raises(ValueError, match="updater root"):
        ensure_lock_files(forbidden, home=fake_home)

    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    mut_path, writer_path = ensure_lock_files(root, home=fake_home)
    mut_ino = os.stat(mut_path).st_ino
    writer_ino = os.stat(writer_path).st_ino

    # Re-calling ensure_lock_files keeps permanent lock inodes unchanged.
    mut2, writer2 = ensure_lock_files(root, home=fake_home)
    assert os.stat(mut2).st_ino == mut_ino
    assert os.stat(writer2).st_ino == writer_ino

    # Exclusive mutation gate blocks both exclusive and shared contenders.
    with acquire_mutation_gate(root):
        with pytest.raises(LockBusyError):
            with acquire_mutation_gate(root, blocking=False):
                pass
        with pytest.raises(LockBusyError):
            with acquire_shared_gate(root, blocking=False):
                pass

    # Shared gate allows a second shared holder, blocks exclusive mutation gate.
    with acquire_shared_gate(root):
        with acquire_shared_gate(root, blocking=False):
            with pytest.raises(LockBusyError):
                with acquire_mutation_gate(root, blocking=False):
                    pass

    # LauncherLockSession drops shared gate immediately if writer lock is busy (never waits).
    with acquire_writer_lock(root):
        with pytest.raises(LockBusyError):
            LauncherLockSession.acquire(root)
        # Shared gate was NOT left held after writer-lock refusal!
        with acquire_mutation_gate(root, blocking=False):
            pass

    # Writer lock FD transfers across exec to service child, which marks it O_CLOEXEC before
    # spawning any grandchild (so child holds writer.lock while grandchild never inherits it).
    grandchild_fds_file = tmp_path / "grandchild_fds.json"
    child_ready_file = tmp_path / "child_ready"
    session = LauncherLockSession.acquire(root)
    session.drop_shared_gate()
    writer_fd = session.prepare_writer_fd_for_exec()
    child_code = (
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "from sigint101.locks import lock_inherited_writer_fd\n"
        "fd = int(os.environ['SIGINT101_WRITER_LOCK_FD'])\n"
        "lock_inherited_writer_fd(fd)\n"
        "assert os.get_inheritable(fd) is False\n"
        "gc_code = (\n"
        "    'import json, os\\n'\n"
        "    'from pathlib import Path\\n'\n"
        "    'targets = []\\n'\n"
        "    'for e in Path(\"/proc/self/fd\").iterdir():\\n'\n"
        "    '    try: targets.append(os.readlink(e))\\n'\n"
        "    '    except OSError: pass\\n'\n"
        "    'print(json.dumps(targets))\\n'\n"
        ")\n"
        "gc = subprocess.run(\n"
        "    [sys.executable, '-B', '-c', gc_code],\n"
        "    capture_output=True, text=True, check=True,\n"
        ")\n"
        f"Path({str(grandchild_fds_file)!r}).write_text(gc.stdout, encoding='utf-8')\n"
        f"Path({str(child_ready_file)!r}).write_text('ready', encoding='utf-8')\n"
        "time.sleep(30)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["SIGINT101_WRITER_LOCK_FD"] = str(writer_fd)
    proc = subprocess.Popen(
        [sys.executable, "-B", "-c", child_code],
        env=env,
        pass_fds=(writer_fd,),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Parent closes its copy of writer_fd after fork; child now uniquely holds LOCK_EX.
    os.close(writer_fd)
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not child_ready_file.is_file():
            assert proc.poll() is None, proc.stderr.read().decode("utf-8", "replace")
            time.sleep(0.05)
        assert child_ready_file.is_file()
        # While child lives, writer.lock is held exclusively; mutation.lock is FREE!
        with pytest.raises(LockBusyError):
            with acquire_writer_lock(root, blocking=False):
                pass
        with acquire_mutation_gate(root, blocking=False):
            pass
        # Grandchild did NOT inherit writer.lock.
        gc_targets = json.loads(grandchild_fds_file.read_text(encoding="utf-8"))
        assert str(writer_path) not in gc_targets
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    # After child exits, writer.lock is immediately available again.
    with acquire_writer_lock(root, blocking=False):
        pass


# --- 2. Installed unit & desktop wrapper block every start during copy (§8) --------------------


def test_installed_unit_blocks_every_start_during_the_copy(tmp_path, sealed_host_runtime):
    assert BASH, "bash is required"
    rt_dir, host_py = sealed_host_runtime

    # Main-side comparison: deploy/kiosk/start-kiosk.sh open has no mutation gate and calls
    # `systemctl --user start sigint101.service` immediately even while an update lock is held.
    legacy_bin = tmp_path / "legacy_bin"
    legacy_bin.mkdir()
    legacy_log = tmp_path / "legacy_calls.log"
    _make_stub_bin(legacy_bin / "systemctl", f'echo "systemctl $*" >> "{legacy_log}"')
    _make_stub_bin(legacy_bin / "curl", "exit 0")
    _make_stub_bin(legacy_bin / "pgrep", "exit 0")
    legacy_env = os.environ.copy()
    legacy_env["PATH"] = f"{legacy_bin}:{legacy_env.get('PATH', '')}"
    legacy_env["HOME"] = str(tmp_path / "legacy_home")
    legacy_env["XDG_RUNTIME_DIR"] = str(tmp_path / "legacy_rt")
    (tmp_path / "legacy_home").mkdir()
    (tmp_path / "legacy_rt").mkdir()
    # Curl fails first then succeeds so start-kiosk.sh invokes systemctl start.
    curl_flag = tmp_path / "legacy_curl_once"
    _make_stub_bin(
        legacy_bin / "curl",
        f'if [ ! -e "{curl_flag}" ]; then touch "{curl_flag}"; exit 1; fi; exit 0',
    )
    r_leg = subprocess.run(
        [BASH, str(REPO_ROOT / "deploy" / "kiosk" / "start-kiosk.sh"), "open"],
        env=legacy_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r_leg.returncode == 0
    assert "systemctl --user start sigint101.service" in legacy_log.read_text(encoding="utf-8")

    # PR 3 contract: hold exclusive mutation gate and test ALL four launch paths.
    fx = _create_managed_root_fixture(
        tmp_path,
        mode="managed",
        operation_id="op-gate-1",
        sealed_runtime_dir=rt_dir,
        python_executable=host_py,
    )
    root = fx["root"]
    gen_dir = root / "data" / "gen-0001"
    before_prog = (gen_dir / "progress.db").read_bytes()
    before_exam = (gen_dir / "exam.db").read_bytes()
    served_marker = root / "operations" / "op-gate-1" / "served.json"

    stub_bin = tmp_path / "managed_bin"
    stub_bin.mkdir()
    managed_log = tmp_path / "managed_calls.log"
    _make_stub_bin(stub_bin / "systemctl", f'echo "systemctl $*" >> "{managed_log}"')
    _make_stub_bin(stub_bin / "curl", "exit 1")
    _make_stub_bin(stub_bin / "pgrep", "exit 1")
    _make_stub_bin(stub_bin / "chromium", f'echo "chromium $*" >> "{managed_log}"')

    desk_home = tmp_path / "desk_home"
    desk_rt = tmp_path / "desk_rt"
    desk_home.mkdir()
    desk_rt.mkdir()
    # Create a Unix socket placeholder for WAYLAND_DISPLAY so watch() enters its loop.
    wayland_sock = desk_rt / "wayland-0"
    ws = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ws.bind(str(wayland_sock))

    desk_env = os.environ.copy()
    desk_env["PATH"] = f"{stub_bin}:{desk_env.get('PATH', '')}"
    desk_env["HOME"] = str(desk_home)
    desk_env["XDG_RUNTIME_DIR"] = str(desk_rt)
    desk_env["WAYLAND_DISPLAY"] = "wayland-0"
    desk_env["SIGINT101_UPDATER_ROOT"] = str(root)
    desk_env["SIGINT101_COURSE_URL"] = f"http://{fx['host']}:{fx['port']}"
    desk_env["SIGINT101_WATCH_ONCE"] = "1"

    try:
        with acquire_mutation_gate(root):
            # (a) Unit/manual/restart launch via dispatcher is refused with EXIT_GATE_BUSY.
            for _ in range(2):
                d_res = subprocess.run(
                    [sys.executable, "-B", str(fx["dispatcher"]), "--root", str(root), "serve"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert d_res.returncode == EXIT_GATE_BUSY, (d_res.returncode, d_res.stderr)
                assert "busy" in d_res.stderr.lower() or "maintenance" in d_res.stderr.lower()

            # (b) Managed desktop icon opener stands down while mutation gate is held.
            open_res = subprocess.run(
                [BASH, str(MANAGED_DIR / "sigint101-course-managed.sh"), "open"],
                env=desk_env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert open_res.returncode == EXIT_GATE_BUSY, (open_res.returncode, open_res.stderr)

            # (c) Managed watchdog stands down while mutation gate is held.
            watch_res = subprocess.run(
                [BASH, str(MANAGED_DIR / "sigint101-course-managed.sh"), "watch"],
                env=desk_env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert watch_res.returncode == 0
            assert not managed_log.exists() or managed_log.read_text(encoding="utf-8") == ""

            # Zero writes to either DB and no serving marker created during barrier!
            assert (gen_dir / "progress.db").read_bytes() == before_prog
            assert (gen_dir / "exam.db").read_bytes() == before_exam
            assert not served_marker.exists()
    finally:
        ws.close()
        wayland_sock.unlink()

    # After releasing the mutation gate, the real managed launcher starts, writes/fsyncs the
    # serving marker BEFORE listening, holds writer.lock, and serves /api/hardware HTTP 200!
    proc = subprocess.Popen(
        [sys.executable, "-B", str(fx["dispatcher"]), "--root", str(root), "serve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        import urllib.request

        url = f"http://{fx['host']}:{fx['port']}/api/hardware"
        deadline = time.monotonic() + 12.0
        body = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"managed launcher exited ({proc.returncode}): "
                    f"{proc.stderr.read().decode('utf-8', 'replace')}"
                )
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    if resp.status == 200:
                        body = json.loads(resp.read().decode("utf-8"))
                        break
            except OSError:
                time.sleep(0.1)
        assert body is not None, "managed console did not become reachable after gate release"
        assert served_marker.is_file(), "serving marker must be written before listening"
        marker_data = json.loads(served_marker.read_text(encoding="utf-8"))
        assert marker_data["operation_id"] == "op-gate-1"
        assert marker_data["generation_id"] == "gen-0001"
        # While serving, writer.lock is held exclusively and mutation.lock is released!
        with pytest.raises(LockBusyError):
            with acquire_writer_lock(root, blocking=False):
                pass
        with acquire_mutation_gate(root, blocking=False):
            pass
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


# --- 3. Writer cgroup & listener must be gone before snapshot (§8) -----------------------------


def test_writer_must_be_gone_before_snapshot(tmp_path):
    # Main-side comparison: deploy/provision.sh has no cgroup drain predicate.
    prov_text = (REPO_ROOT / "deploy" / "provision.sh").read_text(encoding="utf-8")
    assert "cgroup.procs" not in prov_text

    # Spawn a real ancillary child process (simulating a leaked decoder/helper in service cgroup)
    # while main PID has already exited and TCP listener is already closed!
    ancillary = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"])
    try:
        # 1) Ancillary cgroup process alive after port closed & unit inactive -> MUST refuse!
        with pytest.raises(ServiceDrainError, match="cgroup|ancillary|process"):
            wait_service_drained(
                stop_unit=lambda _t: None,
                get_unit_state=lambda: ("inactive", "dead", 0),
                get_cgroup_pids=lambda: [ancillary.pid] if ancillary.poll() is None else [],
                is_listener_active=lambda: False,
                kill_cgroup=None,
                timeout_s=0.4,
                poll_s=0.05,
            )

        # 2) Public listener still open even if unit inactive & cgroup empty -> MUST refuse!
        with pytest.raises(ServiceDrainError, match="listener"):
            wait_service_drained(
                stop_unit=lambda _t: None,
                get_unit_state=lambda: ("inactive", "dead", 0),
                get_cgroup_pids=lambda: [],
                is_listener_active=lambda: True,
                timeout_s=0.3,
                poll_s=0.05,
            )

        # 3) Unit still deactivating even if port closed & cgroup empty -> MUST refuse!
        with pytest.raises(ServiceDrainError, match="unit|active|deactivating"):
            wait_service_drained(
                stop_unit=lambda _t: None,
                get_unit_state=lambda: ("deactivating", "stop-sigterm", 0),
                get_cgroup_pids=lambda: [],
                is_listener_active=lambda: False,
                timeout_s=0.3,
                poll_s=0.05,
            )

        # 4) With explicit cgroup kill, ancillary process is terminated and drain succeeds!
        kill_called = {"n": 0}

        def do_kill_cgroup():
            kill_called["n"] += 1
            ancillary.kill()
            ancillary.wait(timeout=3)

        report = wait_service_drained(
            stop_unit=lambda _t: None,
            get_unit_state=lambda: ("inactive", "dead", 0),
            get_cgroup_pids=lambda: [ancillary.pid] if ancillary.poll() is None else [],
            is_listener_active=lambda: False,
            kill_cgroup=do_kill_cgroup,
            timeout_s=2.0,
            poll_s=0.05,
        )
        assert report.drained is True
        assert kill_called["n"] == 1
        assert ancillary.poll() is not None
    finally:
        if ancillary.poll() is None:
            ancillary.kill()
            ancillary.wait(timeout=3)

    # Kiosk-page open predicate refuses while a matching user-data-dir process exists.
    profile = tmp_path / "home" / ".config" / "chromium-kiosk"
    open_procs = [(4242, ["chromium", f"--user-data-dir={profile}", "--app=http://127.0.0.1:8008"])]
    other_procs = [(5555, ["chromium", "--user-data-dir=/tmp/other-profile"])]
    assert is_kiosk_page_open(profile, process_lister=lambda: open_procs) is True
    assert is_kiosk_page_open(profile, process_lister=lambda: other_procs) is False
    with pytest.raises(KioskPageOpenError, match="Exit"):
        require_kiosk_page_closed(profile, process_lister=lambda: open_procs)
    require_kiosk_page_closed(profile, process_lister=lambda: other_procs)


# --- 4. Enrollment interruption keeps a bootable legacy pair until commit (§8, fixture) --------


@pytest.mark.parametrize(
    "stop_at",
    [
        "before_control_sync",
        "before_legacy_selector_sync",
        "before_unit_install",
        "after_unit_install_before_drain",
        None,
    ],
)
def test_enrollment_crash_keeps_a_bootable_legacy_pair_until_commit(tmp_path, stop_at):
    # Main-side comparison: deploy/install.sh directly enables/starts the checkout service without
    # creating a durable legacy selector first.
    install_text = (REPO_ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "enable --now sigint101.service" in install_text
    assert "current.json" not in install_text

    # Build a disposable legacy checkout with seeded legacy learner rows.
    home = tmp_path / "home"
    home.mkdir()
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    desktop_bin = tmp_path / "usr_local_bin" / "sigint101-course"
    desktop_bin.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "deploy" / "kiosk" / "start-kiosk.sh", desktop_bin)

    checkout = tmp_path / "sigint-101"
    checkout.mkdir()
    shutil.copytree(
        REPO_ROOT / "sigint101",
        checkout / "sigint101",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copytree(REPO_ROOT / "lesson_packs", checkout / "lesson_packs")
    (checkout / "deploy").mkdir()
    shutil.copy2(REPO_ROOT / "deploy" / "install.sh", checkout / "deploy" / "install.sh")
    shutil.copy2(REPO_ROOT / "deploy" / "provision.sh", checkout / "deploy" / "provision.sh")

    legacy_data = checkout / "data"
    legacy_data.mkdir()
    p = Progress(legacy_data / "progress.db")
    p.mark_completed(1)
    p.record_prediction(1, "wider")
    p.close()
    e = ExamStore(legacy_data / "exam.db")
    e.record("T1A01", True)
    e.close()
    (legacy_data / "events.jsonl").write_text('{"kind":"boot","ts":1}\n', encoding="utf-8")

    port = _free_loopback_port()
    unit_path = unit_dir / "sigint101.service"
    unit_path.write_text(
        "[Service]\n"
        f"WorkingDirectory={checkout}\n"
        f"ExecStart={sys.executable} -B -m uvicorn sigint101.app:app "
        f"--host 127.0.0.1 --port {port}\n",
        encoding="utf-8",
    )

    root = tmp_path / "updater_root"
    sp_src = tmp_path / "sp_src"
    sp_src.mkdir()
    (sp_src / "_sealed_mod.py").write_text("OK = True\n", encoding="utf-8")

    runtime_dir = tmp_path / "xdg_rt"
    runtime_dir.mkdir()
    # Enrollment requires deliberate Exit flag and closed kiosk page.
    (runtime_dir / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")

    class _CrashAtBoundary(RuntimeError):
        pass

    def boundary_hook(stage: str) -> None:
        if stop_at is not None and stage == stop_at:
            raise _CrashAtBoundary(stage)

    bench_check = _fixture_bench_checker(tmp_path, root)

    if stop_at is not None:
        with pytest.raises(_CrashAtBoundary, match=stop_at):
            enroll_legacy_fixture(
                repo_root=REPO_ROOT,
                updater_root=root,
                legacy_checkout=checkout,
                legacy_data_dir=legacy_data,
                legacy_python=sys.executable,
                source_site_packages=sp_src,
                unit_path=unit_path,
                desktop_wrapper_path=desktop_bin,
                home=home,
                runtime_dir=runtime_dir,
                host="127.0.0.1",
                port=port,
                kiosk_process_lister=lambda: [],
                reload_systemd=lambda: None,
                drain_old_service=lambda: None,
                on_boundary=boundary_hook,
                bench_marker_checker=bench_check,
            )
    else:
        receipt = enroll_legacy_fixture(
            repo_root=REPO_ROOT,
            updater_root=root,
            legacy_checkout=checkout,
            legacy_data_dir=legacy_data,
            legacy_python=sys.executable,
            source_site_packages=sp_src,
            unit_path=unit_path,
            desktop_wrapper_path=desktop_bin,
            home=home,
            runtime_dir=runtime_dir,
            host="127.0.0.1",
            port=port,
            kiosk_process_lister=lambda: [],
            reload_systemd=lambda: None,
            drain_old_service=lambda: None,
            bench_marker_checker=bench_check,
        )
        assert receipt["mode"] == "legacy"

    # At EVERY boundary, the currently installed unit boots and serves the seeded legacy progress!
    unit_text = unit_path.read_text(encoding="utf-8")
    exec_line = next(
        line.split("=", 1)[1].strip()
        for line in unit_text.splitlines()
        if line.strip().startswith("ExecStart=")
    )
    workdir = next(
        (
            line.split("=", 1)[1].strip()
            for line in unit_text.splitlines()
            if line.strip().startswith("WorkingDirectory=")
        ),
        str(checkout),
    )
    import shlex
    import urllib.request

    boot_env = os.environ.copy()
    boot_env["PYTHONPATH"] = str(checkout)
    proc = subprocess.Popen(
        shlex.split(exec_line),
        cwd=workdir,
        env=boot_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 12.0
        html = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"unit failed to boot after {stop_at}: "
                    f"{proc.stderr.read().decode('utf-8', 'replace')}"
                )
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.0) as resp:
                    if resp.status == 200:
                        html = resp.read().decode("utf-8")
                        break
            except OSError:
                time.sleep(0.1)
        assert html is not None
        assert "SIGINT 101" in html
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

    # Original legacy learner DB still intact with lesson 1 completed and T1A01 box 2!
    p_chk = Progress(legacy_data / "progress.db")
    e_chk = ExamStore(legacy_data / "exam.db")
    try:
        assert p_chk.get(1)["completed"] == 1
        assert e_chk.box_of("T1A01") == 2
    finally:
        p_chk.close()
        e_chk.close()


# --- 5. Provision shim contends on gate, install shim refuses enrolled box, delegates once -----


def test_provision_contends_with_update_and_delegates_once(tmp_path):
    assert BASH, "bash is required"

    # Main-side comparison: deploy/provision.sh does not acquire mutation.lock.
    prov_text = (REPO_ROOT / "deploy" / "provision.sh").read_text(encoding="utf-8")
    assert "mutation.lock" not in prov_text

    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    ensure_lock_files(root)
    (root / "current.json").write_text(
        '{"format_version": 1, "mode": "managed"}\n', encoding="utf-8"
    )
    (root / "control.json").write_text(
        '{"format_version": 1, "control_id": "c1"}\n', encoding="utf-8"
    )

    # Install a recording dispatcher stub under ROOT/bin/dispatcher.py.
    dispatch_log = tmp_path / "dispatch.log"
    bin_dir = root / "bin"
    bin_dir.mkdir(mode=0o700)
    _make_stub_bin(bin_dir / "dispatcher.py", f'echo "dispatched:$*" >> "{dispatch_log}"')

    shim_env = os.environ.copy()
    shim_env["SIGINT101_UPDATER_ROOT"] = str(root)

    # 1) Fresh legacy installer shim refuses on an enrolled root.
    inst_res = subprocess.run(
        [BASH, str(MANAGED_DIR / "install-shim.sh")],
        env=shim_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert inst_res.returncode != 0
    assert "enrolled" in inst_res.stderr.lower()

    # 2) Provision shim refuses with busy contention while mutation gate is held; zero delegation!
    with acquire_mutation_gate(root):
        busy_res = subprocess.run(
            [BASH, str(MANAGED_DIR / "provision-shim.sh"), "--apply"],
            env=shim_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert busy_res.returncode == EXIT_GATE_BUSY, (busy_res.returncode, busy_res.stderr)
        assert "busy" in busy_res.stderr.lower() or "lock" in busy_res.stderr.lower()
        assert not dispatch_log.exists()

    # 3) When free, provision shim acquires gate and delegates ONCE with no git/bootstrap/install.
    ok_res = subprocess.run(
        [BASH, str(MANAGED_DIR / "provision-shim.sh"), "--apply"],
        env=shim_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert ok_res.returncode == 0, (ok_res.stdout, ok_res.stderr)
    lines = dispatch_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert "--apply" in lines[0]


# --- 6. Same-ABI Python point updates keep boot & apply available; ABI drift refuses (§8) ------


def test_same_abi_system_python_update_keeps_boot_and_apply_available(tmp_path):
    sp_src = tmp_path / "site_packages_src"
    sp_src.mkdir()
    pkg = sp_src / "demo_dep"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    # Plant __pycache__ and .pyc in source to verify staging excludes bytecode completely (U8).
    cache = pkg / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312.pyc").write_bytes(b"stale-pyc")

    checkout = tmp_path / "legacy_checkout"
    checkout.mkdir()

    abi_before = {
        "major": 3,
        "minor": 12,
        "patch": 3,
        "major_minor": "3.12",
        "soabi": "cpython-312-x86_64-linux-gnu",
        "implementation": "cpython",
        "platform_machine": "x86_64",
    }
    rt_dir = tmp_path / "runtimes" / "rt-1"
    receipt = stage_sealed_runtime(
        sp_src,
        rt_dir,
        runtime_id="rt-1",
        python_abi=abi_before,
        forbidden_checkout_root=checkout,
    )
    assert receipt["runtime_id"] == "rt-1"
    assert not list(rt_dir.rglob("__pycache__"))
    assert not list(rt_dir.rglob("*.pyc"))

    # Same-ABI host Python point update (3.12.3 -> 3.12.9, constant major.minor & SOABI) PASSES!
    abi_point_update = dict(abi_before, patch=9)
    verified = verify_runtime_compatibility(rt_dir, current_abi=abi_point_update)
    assert verified["runtime_id"] == "rt-1"

    # Incompatible major.minor (3.12 -> 3.13) REFUSED with named reason!
    abi_minor_bump = dict(
        abi_before, minor=13, major_minor="3.13", soabi="cpython-313-x86_64-linux-gnu"
    )
    with pytest.raises(RuntimeCompatibilityError, match="ABI|3.12|3.13"):
        verify_runtime_compatibility(rt_dir, current_abi=abi_minor_bump)

    # Incompatible SOABI at same major.minor REFUSED!
    abi_soabi_drift = dict(abi_before, soabi="cpython-312-aarch64-linux-gnu")
    with pytest.raises(RuntimeCompatibilityError, match="SOABI|ABI"):
        verify_runtime_compatibility(rt_dir, current_abi=abi_soabi_drift)

    # Editable install (.pth pointing into checkout or .egg-link) REFUSED during staging!
    editable_sp = tmp_path / "editable_sp"
    editable_sp.mkdir()
    (editable_sp / "sigint101.pth").write_text(f"{checkout}\n", encoding="utf-8")
    with pytest.raises(RuntimeCompatibilityError, match="editable|checkout|pth"):
        stage_sealed_runtime(
            editable_sp,
            tmp_path / "runtimes" / "rt-bad-pth",
            runtime_id="rt-bad-pth",
            python_abi=abi_before,
            forbidden_checkout_root=checkout,
        )

    egg_sp = tmp_path / "egg_sp"
    egg_sp.mkdir()
    (egg_sp / "sigint101.egg-link").write_text(f"{checkout}\n", encoding="utf-8")
    with pytest.raises(RuntimeCompatibilityError, match="editable|egg-link"):
        stage_sealed_runtime(
            egg_sp,
            tmp_path / "runtimes" / "rt-bad-egg",
            runtime_id="rt-bad-egg",
            python_abi=abi_before,
            forbidden_checkout_root=checkout,
        )

    # Real -B -I import check verifies module resolves from sealed runtime, never checkout!
    real_abi = probe_python_abi(sys.executable)
    real_rt = tmp_path / "runtimes" / "rt-real"
    stage_sealed_runtime(
        sp_src,
        real_rt,
        runtime_id="rt-real",
        python_abi=real_abi,
        forbidden_checkout_root=checkout,
    )
    rel_stub = tmp_path / "rel_stub"
    (rel_stub / "sigint101").mkdir(parents=True)
    (rel_stub / "sigint101" / "__init__.py").write_text("REL = True\n", encoding="utf-8")
    imp = check_runtime_imports(
        sys.executable,
        real_rt,
        release_root=rel_stub,
        modules=("sigint101", "demo_dep"),
        forbidden_checkout_root=checkout,
    )
    assert imp["demo_dep"].startswith(str((real_rt / "site-packages").resolve()))
    assert imp["sigint101"].startswith(str(rel_stub.resolve()))
    # Sealed runtime stayed byte-stable (zero bytecode created!).
    verify_runtime_compatibility(real_rt, current_abi=real_abi, verify_files=True)

    # Planting bytecode or modifying a sealed payload file MUST fail verification!
    os.chmod(real_rt / "site-packages" / "demo_dep", 0o700)
    planted_pyc = real_rt / "site-packages" / "demo_dep" / "rogue.pyc"
    planted_pyc.write_bytes(b"pyc")
    try:
        with pytest.raises(RuntimeCompatibilityError, match="bytecode"):
            verify_runtime_compatibility(real_rt, current_abi=real_abi, verify_files=True)
    finally:
        planted_pyc.unlink()
        os.chmod(real_rt / "site-packages" / "demo_dep", 0o555)

    # Missing current.json in an enrolled root is a hard launcher error (never guesses).
    bad_root = tmp_path / "missing_sel_root"
    bad_root.mkdir(mode=0o700)
    ensure_lock_files(bad_root)
    (bad_root / "bin").mkdir(mode=0o700)
    shutil.copy2(MANAGED_DIR / "dispatcher.py", bad_root / "bin" / "dispatcher.py")
    sys.path.insert(0, str(MANAGED_DIR))
    from control_manifest import stage_control_generation

    stage_control_generation(REPO_ROOT, bad_root / "control" / "c1", control_id="c1")
    (bad_root / "control.json").write_text(
        json.dumps({"format_version": 1, "control_id": "c1"}) + "\n", encoding="utf-8"
    )
    missing_sel = subprocess.run(
        [
            sys.executable,
            "-B",
            str(bad_root / "bin" / "dispatcher.py"),
            "--root",
            str(bad_root),
            "serve",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert missing_sel.returncode == 1
    assert "current.json" in missing_sel.stderr


# --- 7. Deployed-control manifest covers ONLY executed/installed managed paths (§8) ------------


def test_control_manifest_only_covers_executed_installed_paths(tmp_path):
    sys.path.insert(0, str(MANAGED_DIR))
    from control_manifest import compare_control_manifests, compute_control_manifest

    # Copy relevant subtrees into a scratch repo.
    repo_copy = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "deploy", repo_copy / "deploy")
    shutil.copytree(
        REPO_ROOT / "sigint101",
        repo_copy / "sigint101",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    baseline = compute_control_manifest(repo_copy)

    # 1) Editing unrelated files under deploy/ causes ZERO deployed-control drift!
    (repo_copy / "deploy" / "desktop" / "gtk-3.0" / "gtk.css").write_text(
        "/* edit */\n", encoding="utf-8"
    )
    (repo_copy / "deploy" / "unrelated-notes.txt").write_text("not executed\n", encoding="utf-8")
    after_unrelated = compute_control_manifest(repo_copy)
    assert compare_control_manifests(baseline, after_unrelated) == []

    # 2) Editing an actual executed/installed managed file IS detected as control drift!
    launcher_copy = repo_copy / "deploy" / "managed" / "launcher.py"
    launcher_copy.write_text(
        launcher_copy.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8"
    )
    after_managed_edit = compute_control_manifest(repo_copy)
    drifts = compare_control_manifests(baseline, after_managed_edit)
    assert any("deploy/managed/launcher.py" in d for d in drifts)

    # 3) Editing a protocol version is also detected as control drift!
    proto_drifted = json.loads(json.dumps(baseline))
    proto_drifted["selector_format_version"] = 99
    proto_drifts = compare_control_manifests(baseline, proto_drifted)
    assert any("selector_format_version" in d for d in proto_drifts)


# --- 8. Start limiter is reset before activation and recovery (§8) -----------------------------


def test_start_limit_is_reset_before_activation_and_recover():
    # Main-side comparison: deploy/kiosk/start-kiosk.sh calls start without reset-failed.
    kiosk_text = (REPO_ROOT / "deploy" / "kiosk" / "start-kiosk.sh").read_text(encoding="utf-8")
    assert "systemctl --user start sigint101.service" in kiosk_text
    assert "reset-failed" not in kiosk_text

    # Stateful unit fixture modeling systemd's start-limit-hit after maintenance busy restarts.
    state = {"failures": 0, "limit": 3, "failed_limit": False, "active": False, "resets": 0}

    def simulated_busy_restart():
        state["failures"] += 1
        if state["failures"] >= state["limit"]:
            state["failed_limit"] = True

    def start_unit():
        if state["failed_limit"]:
            raise RuntimeError("Job for sigint101.service failed: start-limit-hit")
        state["active"] = True

    def reset_failed():
        state["resets"] += 1
        state["failures"] = 0
        state["failed_limit"] = False

    # Exhaust start limiter with 3 blocked maintenance-window starts.
    for _ in range(3):
        simulated_busy_restart()
    assert state["failed_limit"] is True

    # Starting without reset-failed FAILS!
    with pytest.raises(RuntimeError, match="start-limit-hit"):
        start_unit()
    assert state["active"] is False

    # start_unit_with_reset resets failed state FIRST and succeeds!
    start_unit_with_reset(reset_failed=reset_failed, start_unit=start_unit)
    assert state["resets"] == 1
    assert state["active"] is True

    # Verify SystemdUserController parses `systemctl --user show` properties cleanly.
    ctrl = SystemdUserController(unit="sigint101.service")
    parsed = ctrl.parse_show_output(
        "ActiveState=failed\nSubState=failed\nMainPID=0\nResult=start-limit-hit\n"
        "ControlGroup=/user.slice/user-1000.slice/sigint101.service\n"
    )
    assert parsed["ActiveState"] == "failed"
    assert parsed["Result"] == "start-limit-hit"
    assert parsed["MainPID"] == "0"


def test_locks_process_and_runtime_edge_paths(tmp_path):
    from sigint101.locks import LockError, adopt_inherited_shared_gate
    from sigint101.process_checks import is_tcp_port_listening, read_cgroup_pids

    # 1) Lock safety: symlinked locks dir or lock file refused; unsafe dir permissions refused.
    sym_root = tmp_path / "sym_root"
    sym_root.mkdir(mode=0o700)
    real_elsewhere = tmp_path / "elsewhere"
    real_elsewhere.mkdir(mode=0o700)
    (sym_root / "locks").symlink_to(real_elsewhere)
    with pytest.raises(LockError, match="symlink|directory"):
        ensure_lock_files(sym_root)

    bad_perm_root = tmp_path / "bad_perm_root"
    bad_perm_root.mkdir(mode=0o777)
    bad_perm_root.chmod(0o777)
    with pytest.raises(LockError, match="unsafe permissions"):
        ensure_lock_files(bad_perm_root)

    sym_file_root = tmp_path / "sym_file_root"
    sym_file_root.mkdir(mode=0o700)
    (sym_file_root / "locks").mkdir(mode=0o700)
    outside_lock = tmp_path / "outside.lock"
    outside_lock.write_text("", encoding="utf-8")
    (sym_file_root / "locks" / "mutation.lock").symlink_to(outside_lock)
    with pytest.raises(LockError, match="symlink"):
        ensure_lock_files(sym_file_root)

    # adopt_inherited_shared_gate accepts matching mutation.lock FD and rejects wrong file FD.
    ok_root = tmp_path / "ok_root"
    ok_root.mkdir(mode=0o700)
    mut_path, _ = ensure_lock_files(ok_root)
    fd_ok = os.open(os.fspath(mut_path), os.O_RDWR)
    adopted = adopt_inherited_shared_gate(ok_root, fd_ok)
    assert os.get_inheritable(adopted.fd) is False
    adopted.close()

    fd_wrong = os.open(os.fspath(outside_lock), os.O_RDWR)
    try:
        with pytest.raises(LockError, match="mutation.lock"):
            adopt_inherited_shared_gate(ok_root, fd_wrong)
    finally:
        os.close(fd_wrong)

    # 2) Process helpers: real TCP listener check, cgroup.procs reader, and SystemdUserController.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        # Bound without listen() -> not accepting connections yet.
        assert is_tcp_port_listening("127.0.0.1", port, timeout_s=0.1) is False
        srv.listen(1)
        assert is_tcp_port_listening("127.0.0.1", port, timeout_s=0.2) is True

    cg_dir = tmp_path / "fake_cg"
    cg_dir.mkdir()
    assert read_cgroup_pids(cg_dir) == []
    (cg_dir / "cgroup.procs").write_text("101\n202\n\nnotapid\n", encoding="utf-8")
    assert read_cgroup_pids(cg_dir) == [101, 202]

    stub_bin = tmp_path / "sd_bin"
    stub_bin.mkdir()
    sd_log = tmp_path / "sd.log"
    _make_stub_bin(
        stub_bin / "systemctl",
        f'echo "$*" >> "{sd_log}"\n'
        'if [ "$2" = "show" ]; then\n'
        '  echo "ActiveState=inactive"\n'
        '  echo "SubState=dead"\n'
        '  echo "MainPID=0"\n'
        f'  echo "ControlGroup={cg_dir.name}"\n'
        'fi\n',
    )
    sd_env = os.environ.copy()
    sd_env["PATH"] = f"{stub_bin}:{sd_env.get('PATH', '')}"
    ctrl = SystemdUserController(unit="sigint101.service", env=sd_env)
    assert ctrl.unit_state() == ("inactive", "dead", 0)
    assert ctrl.cgroup_pids(cgroup_root=tmp_path) == [101, 202]
    ctrl.stop()
    ctrl.kill_cgroup()
    ctrl.reset_failed()
    ctrl.daemon_reload()
    ctrl.start()
    sd_calls = sd_log.read_text(encoding="utf-8")
    assert "reset-failed sigint101.service" in sd_calls
    assert "kill --kill-who=all --signal=SIGKILL sigint101.service" in sd_calls

    # 3) Runtime direct_url.json editable refusal & executable .pth hook refusal.
    abi = probe_python_abi(sys.executable)
    ed_dir = tmp_path / "direct_url_ed"
    (ed_dir / "pkg.dist-info").mkdir(parents=True)
    (ed_dir / "pkg.dist-info" / "direct_url.json").write_text(
        json.dumps({"url": "file:///tmp/x", "dir_info": {"editable": True}}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeCompatibilityError, match="editable"):
        stage_sealed_runtime(ed_dir, tmp_path / "rt_ed", runtime_id="rt-ed", python_abi=abi)

    pth_dir = tmp_path / "pth_exec"
    pth_dir.mkdir()
    (pth_dir / "evil.pth").write_text("import os\n", encoding="utf-8")
    with pytest.raises(RuntimeCompatibilityError, match="executable .pth"):
        stage_sealed_runtime(pth_dir, tmp_path / "rt_pth", runtime_id="rt-pth", python_abi=abi)


# --- Round 2 PR 3 build-review regression tests (F1-F13) ---------------------------------------


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


@pytest.fixture(scope="module")
def sealed_host_runtime(tmp_path_factory):
    """Stage a real sealed runtime from .venv site-packages for /usr/bin/python3 -I -S."""
    base = tmp_path_factory.mktemp("sealed_rt_mod")
    src_sp = base / "sp_src"
    src_sp.mkdir()
    venv_sp = Path(sys.prefix) / "lib" / (
        f"python{sys.version_info.major}.{sys.version_info.minor}"
    ) / "site-packages"
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
    abi = probe_python_abi(host_py if host_py.exists() else sys.executable)
    rt_dir = base / "rt-real"
    stage_sealed_runtime(src_sp, rt_dir, runtime_id="rt-real", python_abi=abi)
    return rt_dir, (str(host_py) if host_py.exists() else sys.executable)


def test_f1_f5_f7_serving_fence_order_env_hygiene_cloexec_and_shared_admission(
    tmp_path, sealed_host_runtime
):
    rt_dir, host_py = sealed_host_runtime
    sys.path.insert(0, str(MANAGED_DIR))
    from control_manifest import stage_control_generation

    # 1) F1: failed public startup (missing exam.db) MUST NOT write served.json!
    root_fail = tmp_path / "root_fail"
    root_fail.mkdir(mode=0o700)
    ensure_lock_files(root_fail)
    (root_fail / "bin").mkdir(mode=0o700)
    shutil.copy2(MANAGED_DIR / "dispatcher.py", root_fail / "bin" / "dispatcher.py")
    (root_fail / "bin" / "dispatcher.py").chmod(0o700)
    stage_control_generation(REPO_ROOT, root_fail / "control" / "c1", control_id="c1")
    (root_fail / "control.json").write_text(
        json.dumps({"format_version": 1, "control_id": "c1"}) + "\n", encoding="utf-8"
    )
    rel_dir = root_fail / "releases" / "rel-1"
    shutil.copytree(
        REPO_ROOT / "sigint101",
        rel_dir / "sigint101",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copytree(REPO_ROOT / "lesson_packs", rel_dir / "lesson_packs")
    write_release_manifest(rel_dir, release_id="rel-1", commit="aaaa111")

    gen_bad = root_fail / "data" / "gen-bad"
    gen_bad.mkdir(parents=True, mode=0o700)
    migrate_store_copy(
        tmp_path / "np_fail.db",
        gen_bad / "progress.db",
        store="progress",
        allow_missing_source=True,
        scratch_dir=tmp_path / "sc_p_fail",
    )
    # Intentionally omit exam.db so managed public startup fails inside lifespan!
    shutil.copytree(rt_dir, root_fail / "runtimes" / "rt-real")
    os.chmod(root_fail / "runtimes" / "rt-real", 0o555)
    ops_fail = root_fail / "operations" / "op-fail"
    ops_fail.mkdir(parents=True, mode=0o700)
    port_fail = _free_loopback_port()
    (root_fail / "current.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "mode": "managed",
                "operation_id": "op-fail",
                "release_id": "rel-1",
                "generation_id": "gen-bad",
                "runtime_id": "rt-real",
                "python_executable": host_py,
                "host": "127.0.0.1",
                "port": port_fail,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    fail_res = subprocess.run(
        [
            host_py,
            "-B",
            str(root_fail / "bin" / "dispatcher.py"),
            "--root",
            str(root_fail),
            "serve",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert fail_res.returncode != 0
    assert not (ops_fail / "served.json").exists(), (
        "failed public startup must never write served.json"
    )

    # 2) Healthy managed start WHILE holding shared gate (M5 shared admission!),
    # verifying F5 (no PYTHONPATH/FD env leak), F7 (writer FD has O_CLOEXEC in fdinfo),
    # and F1 (served.json written AFTER startup and BEFORE listen)!
    gen_ok = root_fail / "data" / "gen-ok"
    gen_ok.mkdir(parents=True, mode=0o700)
    migrate_store_copy(
        tmp_path / "np_ok.db",
        gen_ok / "progress.db",
        store="progress",
        allow_missing_source=True,
        scratch_dir=tmp_path / "sc_p_ok",
    )
    migrate_store_copy(
        tmp_path / "ne_ok.db",
        gen_ok / "exam.db",
        store="exam",
        allow_missing_source=True,
        scratch_dir=tmp_path / "sc_e_ok",
    )
    (gen_ok / GENERATION_MARKER_FILENAME).write_text(
        json.dumps({"generation_id": "gen-ok"}) + "\n", encoding="utf-8"
    )
    ops_ok = root_fail / "operations" / "op-ok"
    ops_ok.mkdir(parents=True, mode=0o700)
    port_ok = _free_loopback_port()
    (root_fail / "current.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "mode": "managed",
                "operation_id": "op-ok",
                "release_id": "rel-1",
                "generation_id": "gen-ok",
                "runtime_id": "rt-real",
                "python_executable": host_py,
                "host": "127.0.0.1",
                "port": port_ok,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Hold shared gate (like desktop wrapper during wait_for_server) — serve MUST succeed (M5)!
    with acquire_shared_gate(root_fail):
        proc = subprocess.Popen(
            [
                host_py,
                "-B",
                str(root_fail / "bin" / "dispatcher.py"),
                "--root",
                str(root_fail),
                "serve",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            import urllib.request

            url = f"http://127.0.0.1:{port_ok}/api/hardware"
            deadline = time.monotonic() + 15.0
            ok = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise AssertionError(
                        f"managed serve failed ({proc.returncode}): "
                        f"{proc.stderr.read().decode('utf-8', 'replace')}"
                    )
                try:
                    with urllib.request.urlopen(url, timeout=1.0) as resp:
                        if resp.status == 200:
                            ok = True
                            break
                except OSError:
                    time.sleep(0.1)
            assert ok, "managed serve did not become ready while shared gate was held"
            assert (ops_ok / "served.json").is_file()

            # F5: inspect live process /proc/<pid>/environ — no PYTHONPATH or internal FD/ROOT vars!
            raw_env = Path(f"/proc/{proc.pid}/environ").read_bytes().split(b"\0")
            env_keys = {
                item.decode("utf-8", "replace").split("=", 1)[0]
                for item in raw_env
                if b"=" in item
            }
            assert "PYTHONPATH" not in env_keys, f"PYTHONPATH leaked into live service: {env_keys}"
            assert "SIGINT101_WRITER_LOCK_FD" not in env_keys
            assert "SIGINT101_SHARED_GATE_FD" not in env_keys

            # F7 (M2): inspect /proc/<pid>/fd + fdinfo — writer.lock is held with O_CLOEXEC set!
            writer_lock_str = str((root_fail / "locks" / "writer.lock").resolve())
            writer_fd_num = None
            for fd_entry in Path(f"/proc/{proc.pid}/fd").iterdir():
                try:
                    if os.readlink(fd_entry) == writer_lock_str:
                        writer_fd_num = fd_entry.name
                        break
                except OSError:
                    continue
            assert writer_fd_num is not None, "live service does not hold writer.lock"
            fdinfo_text = Path(f"/proc/{proc.pid}/fdinfo/{writer_fd_num}").read_text(
                encoding="utf-8"
            )
            flags_line = next(
                line for line in fdinfo_text.splitlines() if line.startswith("flags:")
            )
            fd_flags = int(flags_line.split(":", 1)[1].strip(), 8)
            assert fd_flags & os.O_CLOEXEC, f"writer.lock fd lacks O_CLOEXEC: {oct(fd_flags)}"
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)


def test_f2_legacy_selector_boots_pre_pr1_checkout_without_managed_kwarg(tmp_path):
    # Pre-PR-1 checkout whose create_app() does NOT accept managed=False, only module-level `app`.
    sys.path.insert(0, str(MANAGED_DIR))
    from control_manifest import stage_control_generation

    checkout = tmp_path / "old_checkout"
    (checkout / "sigint101").mkdir(parents=True)
    (checkout / "data").mkdir()
    (checkout / "sigint101" / "__init__.py").write_text("", encoding="utf-8")
    (checkout / "sigint101" / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "def create_app():\n"
        "    a = FastAPI()\n"
        "    @a.get('/api/hardware')\n"
        "    def hw():\n"
        "        return {'legacy_pre_pr1': True}\n"
        "    return a\n"
        "app = create_app()\n",
        encoding="utf-8",
    )

    root = tmp_path / "root_legacy"
    root.mkdir(mode=0o700)
    ensure_lock_files(root)
    (root / "bin").mkdir(mode=0o700)
    shutil.copy2(MANAGED_DIR / "dispatcher.py", root / "bin" / "dispatcher.py")
    (root / "bin" / "dispatcher.py").chmod(0o700)
    stage_control_generation(REPO_ROOT, root / "control" / "c1", control_id="c1")
    (root / "control.json").write_text(
        json.dumps({"format_version": 1, "control_id": "c1"}) + "\n", encoding="utf-8"
    )
    port = _free_loopback_port()
    (root / "current.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "mode": "legacy",
                "operation_id": "enroll-pre-pr1",
                "checkout_dir": str(checkout),
                "data_dir": str(checkout / "data"),
                "python_executable": sys.executable,
                "host": "127.0.0.1",
                "port": port,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [sys.executable, "-B", str(root / "bin" / "dispatcher.py"), "--root", str(root), "serve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        import urllib.request

        deadline = time.monotonic() + 10.0
        payload = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"pre-PR-1 legacy checkout failed to boot ({proc.returncode}): "
                    f"{proc.stderr.read().decode('utf-8', 'replace')}"
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/hardware", timeout=1.0
                ) as r:
                    if r.status == 200:
                        payload = json.loads(r.read().decode("utf-8"))
                        break
            except OSError:
                time.sleep(0.1)
        assert payload == {"legacy_pre_pr1": True}
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


def test_f3_managed_service_refuses_to_boot_when_sealed_runtime_lacks_fastapi(tmp_path):
    # A sealed runtime containing only a marker file (no FastAPI/uvicorn) MUST fail to start
    # under -I -S even if sys.executable is the dev venv that has FastAPI installed!
    fx = _create_managed_root_fixture(tmp_path, mode="managed", operation_id="op-no-fastapi")
    res = subprocess.run(
        [
            sys.executable,
            "-B",
            str(fx["dispatcher"]),
            "--root",
            str(fx["root"]),
            "serve",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode != 0, "managed service must not fall back to interpreter site-packages"
    err_lo = res.stderr.lower()
    assert "no module named" in err_lo or "fastapi" in err_lo or "uvicorn" in err_lo


def test_f4_f13_enrollment_renders_unit_from_template_with_host_python(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    checkout = tmp_path / "sigint-101"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "data").mkdir()
    shutil.copy2(REPO_ROOT / "deploy" / "install.sh", checkout / "deploy" / "install.sh")
    shutil.copy2(REPO_ROOT / "deploy" / "provision.sh", checkout / "deploy" / "provision.sh")

    sp_src = tmp_path / "sp"
    sp_src.mkdir()
    (sp_src / "_m.py").write_text("X = 1\n", encoding="utf-8")
    rt_dir = tmp_path / "xdg_rt"
    rt_dir.mkdir()
    (rt_dir / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")

    unit_path = home / ".config" / "systemd" / "user" / "sigint101.service"
    wrapper_path = tmp_path / "bin" / "sigint101-course"
    root = tmp_path / "updater_root"
    host_py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    fake_legacy_venv_py = str(checkout / ".venv" / "bin" / "python")

    enroll_legacy_fixture(
        repo_root=REPO_ROOT,
        updater_root=root,
        legacy_checkout=checkout,
        legacy_data_dir=checkout / "data",
        legacy_python=fake_legacy_venv_py,
        host_python=host_py,
        source_site_packages=sp_src,
        unit_path=unit_path,
        desktop_wrapper_path=wrapper_path,
        home=home,
        runtime_dir=rt_dir,
        kiosk_process_lister=lambda: [],
        reload_systemd=lambda: None,
        drain_old_service=lambda: None,
        bench_marker_checker=_fixture_bench_checker(tmp_path, root),
    )
    rendered = unit_path.read_text(encoding="utf-8")
    # Managed unit MUST run host_python (not checkout/.venv/bin/python)!
    assert f"ExecStart={host_py} -B " in rendered
    assert fake_legacy_venv_py not in rendered
    # Legacy selector retains legacy_python for legacy boot!
    sel = json.loads((root / "current.json").read_text(encoding="utf-8"))
    assert sel["python_executable"] == fake_legacy_venv_py


def test_f6_managed_desktop_wrapper_keeps_start_kiosk_fixes_outside_gate_blocks():
    legacy_text = (REPO_ROOT / "deploy" / "kiosk" / "start-kiosk.sh").read_text(encoding="utf-8")
    managed_text = (MANAGED_DIR / "sigint101-course-managed.sh").read_text(encoding="utf-8")

    # Must retain output_size(), forget_window_placement(), dynamic --window-size, and initial wait.
    assert "output_size()" in managed_text
    assert "forget_window_placement()" in managed_text
    assert '--window-size="${size%x*},${size#*x}"' in managed_text

    # Stripping marked managed-gate blocks (and fd 8 closes + header) must match start-kiosk.sh!
    def strip_managed(text: str) -> list[str]:
        out: list[str] = []
        skip = False
        for line in text.splitlines():
            if line.strip() == "# >>> managed-gate >>>":
                skip = True
                continue
            if line.strip() == "# <<< managed-gate <<<":
                skip = False
                continue
            if skip or line.startswith("# ABOUTME:"):
                continue
            out.append(line.replace(" 8>&- 9>&-", " 9>&-"))
        return out

    legacy_body = [
        line for line in legacy_text.splitlines() if not line.startswith("# ABOUTME:")
    ]
    assert strip_managed(managed_text) == legacy_body


def test_f7_f8_f9_f10_f11_control_import_closure_and_nits(tmp_path):
    sys.path.insert(0, str(MANAGED_DIR))
    from control_manifest import CONTROL_ALLOWLIST, stage_control_generation

    # F7: every allowlisted sigint101/*.py module imports cleanly under -B -I -S from control_dir!
    ctrl_dir = tmp_path / "ctrl_isolated"
    stage_control_generation(REPO_ROOT, ctrl_dir, control_id="ctrl-iso")
    py_mods = [
        rel[:-3].replace("/", ".")
        for rel in CONTROL_ALLOWLIST
        if rel.startswith("sigint101/") and rel.endswith(".py")
    ]
    assert py_mods
    host_py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    snippet = (
        f"import importlib, sys\n"
        f"sys.path.insert(0, {str(ctrl_dir)!r})\n"
        f"for m in {py_mods!r}:\n"
        f"    importlib.import_module(m)\n"
        f"print('ALL_IMPORTED')\n"
    )
    res = subprocess.run(
        [host_py, "-B", "-I", "-S", "-c", snippet],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "ALL_IMPORTED" in res.stdout

    # F8: adopt_inherited_shared_gate refuses when another holder holds exclusive mutation.lock!
    from sigint101.locks import LockBusyError, adopt_inherited_shared_gate

    r_f8 = tmp_path / "root_f8"
    r_f8.mkdir(mode=0o700)
    mut_path, _ = ensure_lock_files(r_f8)
    with acquire_mutation_gate(r_f8):
        unheld_fd = os.open(os.fspath(mut_path), os.O_RDWR)
        try:
            with pytest.raises(LockBusyError):
                adopt_inherited_shared_gate(r_f8, unheld_fd)
        finally:
            os.close(unheld_fd)

    # F9: kiosk predicate matches trailing slash on --user-data-dir=<profile>/!
    prof = tmp_path / "kiosk_prof"
    procs_slash = [(99, ["chromium", f"--user-data-dir={prof}/", "--app=http://127.0.0.1:8008"])]
    assert is_kiosk_page_open(prof, process_lister=lambda: procs_slash) is True

    # F10: mismatched .so ABI tag inside sealed runtime is refused!
    bad_so_sp = tmp_path / "bad_so_sp"
    bad_so_sp.mkdir()
    (bad_so_sp / "mod.cpython-312-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
    abi_314 = {
        "major": 3,
        "minor": 14,
        "patch": 0,
        "major_minor": "3.14",
        "soabi": "cpython-314-x86_64-linux-gnu",
        "implementation": "cpython",
        "platform_machine": "x86_64",
    }
    with pytest.raises(RuntimeCompatibilityError, match="SOABI|extension|.so"):
        stage_sealed_runtime(
            bad_so_sp, tmp_path / "rt_bad_so", runtime_id="rt-bad-so", python_abi=abi_314
        )

    # F11: read_cgroup_pids walks child cgroups recursively!
    from sigint101.process_checks import read_cgroup_pids

    cg_top = tmp_path / "cg_tree"
    (cg_top / "child.scope").mkdir(parents=True)
    (cg_top / "cgroup.procs").write_text("10\n", encoding="utf-8")
    (cg_top / "child.scope" / "cgroup.procs").write_text("20\n30\n", encoding="utf-8")
    assert read_cgroup_pids(cg_top) == [10, 20, 30]


# --- PR 4 carry-over must-fixes (2, 4-F12) ----------------------------------------------------


def test_dispatcher_accepts_provision_shim_handoff_not_only_serve(tmp_path):
    """Must-fix (2): real dispatcher.py accepts provision-shim.sh hand-off, not only 'serve'."""
    fx = _create_managed_root_fixture(tmp_path, mode="managed")
    root = fx["root"]

    shim_env = os.environ.copy()
    shim_env["SIGINT101_UPDATER_ROOT"] = str(root)

    # Running provision-shim.sh --check through the REAL installed dispatcher.py must NOT fail
    # with argparse "invalid choice" (the PR 3 bug where dispatcher only accepted 'serve').
    res = subprocess.run(
        [BASH, str(MANAGED_DIR / "provision-shim.sh"), "--check"],
        env=shim_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "invalid choice" not in res.stderr.lower(), res.stderr
    assert res.returncode in (0, 10), (res.returncode, res.stdout, res.stderr)

    # On a non-bench host, provision-shim.sh --apply through REAL dispatcher.py MUST be refused
    # by the PR 4 bench gate (not argparse invalid choice!).
    res_mut = subprocess.run(
        [BASH, str(MANAGED_DIR / "provision-shim.sh"), "--apply"],
        env=shim_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res_mut.returncode != 0
    assert "invalid choice" not in res_mut.stderr.lower(), res_mut.stderr
    assert "bench" in res_mut.stderr.lower(), res_mut.stderr


def test_f12_service_entry_rejects_unlocked_writer_fd_and_accepts_locked_fd(tmp_path):
    """Must-fix (4): service_entry._seal_inherited_writer_fd re-asserts LOCK_EX on writer.lock."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_sigint101_service_entry_f12", MANAGED_DIR / "service_entry.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root = tmp_path / "root_f12"
    root.mkdir(mode=0o700)
    _, writer_lock = ensure_lock_files(root)

    # 1) Another holder owns LOCK_EX on writer.lock; passing an unlocked FD MUST be refused!
    with acquire_writer_lock(root):
        unheld_fd = os.open(os.fspath(writer_lock), os.O_RDWR)
        try:
            with pytest.raises(RuntimeError, match="does not hold exclusive writer.lock"):
                mod._seal_inherited_writer_fd(writer_lock, cli_fd=unheld_fd)
        finally:
            os.close(unheld_fd)

    # 2) Passing a genuinely locked FD succeeds idempotently and clears inheritable (O_CLOEXEC).
    with acquire_writer_lock(root) as handle:
        os.set_inheritable(handle.fd, True)
        sealed = mod._seal_inherited_writer_fd(writer_lock, cli_fd=handle.fd)
        assert sealed == handle.fd
        assert os.get_inheritable(sealed) is False



