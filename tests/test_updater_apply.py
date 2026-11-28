# ABOUTME: PR 4 updater tests covering bench gate, release preparation, selector switch, serving
# ABOUTME: fence, explicit unserved abort, plain recovery, preview/no-op, and crash matrices.

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sigint101.bench_gate import (
    DEFAULT_BENCH_MARKER_PATH,
    BenchGateError,
    require_bench_marker,
)
from sigint101.enrollment import enroll_legacy_fixture
from sigint101.exam import ExamStore, load_pool
from sigint101.lessons import load_lessons
from sigint101.progress import Progress
from sigint101.release_prep import (
    PolicyRefusalError,
    SpaceBudgetError,
    fetch_and_export_release,
    verify_release_manifest_integrity,
)
from sigint101.snapshot import GENERATION_MARKER_FILENAME
from sigint101.updater import (
    AbortError,
    RecoveryError,
    abort_unserved,
    apply_update,
    check_status,
    inspect_serving_marker,
    recover,
)
from sigint101.updater_cli import main as updater_cli_main
from sigint101.validation import UnitSettings

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGED_DIR = REPO_ROOT / "deploy" / "managed"

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

_CACHED_SITE_PACKAGES: Path | None = None
_CACHED_SEALED_RUNTIME: tuple[Path, str] | None = None


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


def _get_real_site_packages_source() -> Path:
    global _CACHED_SITE_PACKAGES
    if _CACHED_SITE_PACKAGES is not None and _CACHED_SITE_PACKAGES.is_dir():
        return _CACHED_SITE_PACKAGES
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="sigint101_sp_src_"))
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
        dst = base / entry
        if src.is_dir():
            shutil.copytree(
                src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
            )
        else:
            shutil.copy2(src, dst)
    _CACHED_SITE_PACKAGES = base
    return base


def _get_cached_sealed_runtime() -> tuple[Path, str]:
    global _CACHED_SEALED_RUNTIME
    if _CACHED_SEALED_RUNTIME is not None and _CACHED_SEALED_RUNTIME[0].is_dir():
        return _CACHED_SEALED_RUNTIME
    import tempfile

    from sigint101.runtime import probe_python_abi, stage_sealed_runtime

    sp_src = _get_real_site_packages_source()
    host_py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    base = Path(tempfile.mkdtemp(prefix="sigint101_rt_cached_"))
    rt_dir = base / "rt-0001"
    stage_sealed_runtime(sp_src, rt_dir, runtime_id="rt-0001", python_abi=probe_python_abi(host_py))
    _CACHED_SEALED_RUNTIME = (rt_dir, host_py)
    return _CACHED_SEALED_RUNTIME


@pytest.fixture(scope="module", autouse=True)
def _cleanup_cached_sp_src():
    yield
    global _CACHED_SITE_PACKAGES, _CACHED_SEALED_RUNTIME
    if _CACHED_SEALED_RUNTIME is not None:
        base = _CACHED_SEALED_RUNTIME[0].parent
        _chmod_tree_writable(base)
        shutil.rmtree(base, ignore_errors=True)
        _CACHED_SEALED_RUNTIME = None
    if _CACHED_SITE_PACKAGES is not None:
        _chmod_tree_writable(_CACHED_SITE_PACKAGES)
        shutil.rmtree(_CACHED_SITE_PACKAGES, ignore_errors=True)
        _CACHED_SITE_PACKAGES = None


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
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"git {' '.join(args)} failed: {res.stderr}"
    return res.stdout.strip()


def _write_bench_marker(
    path: Path,
    *,
    updater_root: Path,
    hostname: str | None = None,
    machine_id: str | None = "bench-machine-0001",
    mode: int = 0o600,
) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "format_version": 1,
        "purpose": "sigint101-pr4-disposable-bench",
        "hostname": hostname if hostname is not None else socket.gethostname(),
        "machine_id": machine_id,
        "updater_root": str(updater_root.resolve()),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def _make_bench_checker(marker_path: Path, *, machine_id: str = "bench-machine-0001"):
    def _check(root: Path) -> None:
        require_bench_marker(
            root,
            _marker_path=marker_path,
            _expected_uid=os.geteuid(),
            _machine_id=machine_id,
        )

    return _check


def _build_course_git_origin(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Create a real disposable bare origin with commit v1 (base) and commit v2 (descendant)."""
    work = tmp_path / "git_work"
    bare = tmp_path / "origin.git"
    work.mkdir()
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")
    _git(work, "init", "-b", "main")
    _git(work, "remote", "add", "origin", str(bare))

    # Copy the minimal real course files needed for trial + validation + control manifest.
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
    _git(work, "commit", "-m", "v1 base course")
    c1 = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")

    # v2: legitimate course content change (plus unrelated deploy file edit, no control drift).
    (work / "COURSE_REV").write_text("v2\n", encoding="utf-8")
    (work / "deploy" / "UNRELATED_NOTE.txt").write_text("docs note\n", encoding="utf-8")
    _git(work, "add", "COURSE_REV", "deploy/UNRELATED_NOTE.txt")
    _git(work, "commit", "-m", "v2 course update")
    c2 = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    return bare, work, c1, c2


@dataclass
class FakeServiceFixture:
    """Stateful systemd + ASGI service fixture modeling stop, drain, start-limit, and fencing."""

    root: Path
    active_state: str = "active"
    sub_state: str = "running"
    main_pid: int = 4242
    cgroup_pids: list[int] = field(default_factory=list)
    listener_active: bool = True
    start_limit_remaining: int = 10
    start_limited: bool = False
    fail_public_startup: bool = False
    write_served_marker_on_start: bool = True
    fail_readiness_after_serve: bool = False
    write_learner_row_on_serve: tuple[int, str] | None = None
    unrelated_listener: bool = False
    calls: list[str] = field(default_factory=list)
    last_started_selector: dict | None = None
    post_stop_hook: object = None
    on_marker_visible_before_dir_sync: object = None

    def stop(self, _timeout_s: float = 15.0) -> None:
        self.calls.append("stop")
        self.active_state = "inactive"
        self.sub_state = "dead"
        self.main_pid = 0
        self.cgroup_pids = []
        self.listener_active = self.unrelated_listener
        if callable(self.post_stop_hook):
            self.post_stop_hook()

    def unit_state(self) -> tuple[str, str, int]:
        return self.active_state, self.sub_state, self.main_pid

    def get_cgroup_pids(self) -> list[int]:
        return list(self.cgroup_pids)

    def is_listener_active(self) -> bool:
        return self.listener_active

    def kill_cgroup(self) -> None:
        self.calls.append("kill_cgroup")
        self.cgroup_pids = []

    def reset_failed(self) -> None:
        self.calls.append("reset-failed")
        self.start_limited = False
        self.start_limit_remaining = max(self.start_limit_remaining, 5)
        if self.active_state == "failed":
            self.active_state = "inactive"
            self.sub_state = "dead"

    def record_blocked_start(self) -> None:
        self.start_limit_remaining -= 1
        if self.start_limit_remaining <= 0:
            self.start_limited = True
            self.active_state = "failed"
            self.sub_state = "start-limit-hit"

    def start(self) -> None:
        self.calls.append("start")
        if self.start_limited or self.start_limit_remaining <= 0:
            self.active_state = "failed"
            self.sub_state = "start-limit-hit"
            self.main_pid = 0
            self.listener_active = False
            return

        sel = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        self.last_started_selector = sel
        if self.fail_public_startup:
            self.active_state = "failed"
            self.sub_state = "failed"
            self.main_pid = 0
            self.listener_active = False
            return

        if sel.get("mode") == "managed" and self.write_served_marker_on_start:
            ops_dir = self.root / "operations" / sel["operation_id"]
            ops_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            marker = ops_dir / "served.json"
            marker.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "operation_id": sel["operation_id"],
                        "release_id": sel["release_id"],
                        "generation_id": sel["generation_id"],
                        "fenced_at": time.time(),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(marker, 0o600)
            if callable(self.on_marker_visible_before_dir_sync):
                self.on_marker_visible_before_dir_sync(marker)

            if self.write_learner_row_on_serve is not None:
                lid, pred = self.write_learner_row_on_serve
                data_dir = self.root / "data" / sel["generation_id"]
                p = Progress(data_dir / "progress.db", managed=True)
                try:
                    p.mark_opened(lid)
                    p.record_prediction(lid, pred)
                finally:
                    p.close()

        self.active_state = "active"
        self.sub_state = "running"
        self.main_pid = 5151
        self.listener_active = True

    def is_http_ready(self) -> bool:
        if self.fail_readiness_after_serve:
            return False
        return self.active_state == "active" and self.listener_active


def _seed_legacy_generation(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    lessons = load_lessons(REPO_ROOT / "lesson_packs")
    l1_choice = lessons[1].choices[0]["key"]
    p = Progress(data_dir / "progress.db")
    p.mark_opened(1)
    p.record_prediction(1, l1_choice)
    p.mark_completed(1)
    p.record_stamps(
        [
            {
                "instrument": "ism",
                "first_ts": 1_700_000_000.0,
                "digest": "ab" * 16,
                "provenance": {"tool": "rtl_433", "freq_hz": 433_920_000},
            }
        ]
    )
    p.close()

    e = ExamStore(data_dir / "exam.db")
    pool = load_pool()
    first_q = pool.questions[0].id
    e.record(first_q, correct=True)
    e.close()

    (data_dir / "events.jsonl").write_text('{"kind":"boot"}\n', encoding="utf-8")
    (data_dir / "events.jsonl.1").write_text('{"kind":"older"}\n', encoding="utf-8")
    for sub in ("apt", "sstv", "sstv/.staging", "sstv/.discard"):
        d = data_dir / sub
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    (data_dir / "apt" / "tle.txt").write_text("NOAA 19\n1 33591U\n", encoding="utf-8")
    (data_dir / "sstv" / "pass1.png").write_bytes(b"\x89PNG\r\n\x1a\npass1")
    (data_dir / "sstv" / "pass1.json").write_text('{"mode":"PD120"}\n', encoding="utf-8")
    (data_dir / "sstv" / ".staging" / "inprog.png").write_bytes(b"\x89PNGpart")
    (data_dir / "sstv" / ".discard" / "bad.png").write_bytes(b"\x89PNGbad")


def _setup_enrolled_legacy_box(tmp_path: Path) -> dict:
    """Create a bench-authorized enrolled box in `legacy` mode ready for first conversion."""
    bare, work, c1, c2 = _build_course_git_origin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    root = home / ".local" / "state" / "sigint101-updater"
    marker = _write_bench_marker(tmp_path / "etc_bench" / "bench-allow.json", updater_root=root)
    bench_check = _make_bench_checker(marker)

    checkout = home / "sigint-101"
    shutil.copytree(work, checkout)
    legacy_data = checkout / "data"
    _seed_legacy_generation(legacy_data)

    # Pre-stage cached sealed runtime so F9's -B -I -S trial runs fast without 2000x fsync/test.
    cached_rt, host_py = _get_cached_sealed_runtime()
    sp_src = _get_real_site_packages_source()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    (root / "runtimes").mkdir(mode=0o700, exist_ok=True)
    os.chmod(root / "runtimes", 0o700)
    shutil.copytree(cached_rt, root / "runtimes" / "rt-0001")

    runtime_dir = tmp_path / "xdg_rt"
    runtime_dir.mkdir()
    (runtime_dir / "sigint101-kiosk-exited").write_text("1\n", encoding="utf-8")

    unit_path = home / ".config" / "systemd" / "user" / "sigint101.service"
    desktop_bin = tmp_path / "usr_local_bin" / "sigint101-course"

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
        host_python=host_py,
        trusted_remote=str(bare),
        legacy_commit=c1,
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
        "marker": marker,
        "bench_check": bench_check,
        "checkout": checkout,
        "legacy_data": legacy_data,
        "unit_path": unit_path,
        "runtime_dir": runtime_dir,
    }


# --- 1. PR 4 Non-bench refusal gate (§8, §9.4) ------------------------------------------------


def test_pr4_refuses_all_mutations_without_matching_bench_marker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = home / ".local" / "state" / "sigint101-updater"
    service_calls: list[str] = []

    # 1) Default missing marker refuses BEFORE creating root, locks, files, or calling service.
    assert not root.exists()
    with pytest.raises(BenchGateError):
        require_bench_marker(root, _marker_path=tmp_path / "missing" / "bench.json")
    assert not root.exists()

    # 2) Environment and CLI bypass attempts are completely ignored/refused!
    wrong_marker = _write_bench_marker(
        tmp_path / "bypass" / "bench.json", updater_root=root
    )
    monkeypatch.setenv("SIGINT101_BENCH_MARKER", str(wrong_marker))
    monkeypatch.setenv("SIGINT101_ALLOW_BENCH", "1")
    monkeypatch.setenv("SIGINT101_SKIP_BENCH_GATE", "1")

    with pytest.raises(BenchGateError):
        enroll_legacy_fixture(
            repo_root=REPO_ROOT,
            updater_root=root,
            legacy_checkout=tmp_path / "checkout",
            legacy_data_dir=tmp_path / "checkout" / "data",
            legacy_python=sys.executable,
            source_site_packages=tmp_path / "sp",
            unit_path=tmp_path / "unit.service",
            desktop_wrapper_path=tmp_path / "desktop.sh",
            home=home,
            runtime_dir=tmp_path / "rt",
            reload_systemd=lambda: service_calls.append("reload"),
            drain_old_service=lambda: service_calls.append("drain"),
        )
    assert not root.exists()
    assert service_calls == []

    # 3) Mismatched hostname, mismatched machine_id, mismatched root, non-root owner, writable mode,
    # and symlinked marker are ALL refused!
    mid = "bench-machine-0001"
    bad_host = _write_bench_marker(
        tmp_path / "m_host" / "b.json", updater_root=root, hostname="other-kiosk"
    )
    with pytest.raises(BenchGateError, match="hostname"):
        require_bench_marker(
            root, _marker_path=bad_host, _expected_uid=os.geteuid(), _machine_id=mid
        )

    bad_mid = _write_bench_marker(
        tmp_path / "m_mid" / "b.json", updater_root=root, machine_id="other-id"
    )
    with pytest.raises(BenchGateError, match="machine_id"):
        require_bench_marker(
            root, _marker_path=bad_mid, _expected_uid=os.geteuid(), _machine_id=mid
        )

    bad_root = _write_bench_marker(
        tmp_path / "m_root" / "b.json", updater_root=home / "different-root"
    )
    with pytest.raises(BenchGateError, match="updater_root"):
        require_bench_marker(
            root, _marker_path=bad_root, _expected_uid=os.geteuid(), _machine_id=mid
        )

    writable = _write_bench_marker(tmp_path / "m_mode" / "b.json", updater_root=root, mode=0o666)
    with pytest.raises(BenchGateError, match="permission"):
        require_bench_marker(
            root, _marker_path=writable, _expected_uid=os.geteuid(), _machine_id=mid
        )

    # Non-root owner check (real default expected_uid=0 rejects user-owned marker).
    if os.geteuid() != 0:
        with pytest.raises(BenchGateError, match="root|owner|uid"):
            require_bench_marker(
                root, _marker_path=wrong_marker, _expected_uid=0, _machine_id=mid
            )

    sym_dir = tmp_path / "m_sym"
    sym_dir.mkdir(mode=0o700)
    sym_marker = sym_dir / "b.json"
    sym_marker.symlink_to(wrong_marker)
    with pytest.raises(BenchGateError, match="symlink|regular"):
        require_bench_marker(
            root, _marker_path=sym_marker, _expected_uid=os.geteuid(), _machine_id=mid
        )

    # 4) Every mutating CLI entry refuses on non-bench host with zero directory/lock creation.
    for argv in (
        ["--root", str(root), "--apply", "--remote", "https://example.invalid/r", "--commit", "a"],
        ["--root", str(root), "--enroll"],
        ["--root", str(root), "--recover"],
        ["--root", str(root), "--recover", "--abort", "op-1"],
        ["--root", str(root), "--apply", "--skip-bench-gate"],
    ):
        rc = updater_cli_main(argv)
        assert rc != 0
        assert not root.exists()

    # 5) Default marker path stays outside user home and updater ROOT.
    assert DEFAULT_BENCH_MARKER_PATH.is_absolute()
    assert not DEFAULT_BENCH_MARKER_PATH.is_relative_to(home)

    # 6) Positive check on designated fixture bench marker passes cleanly.
    ok_marker = _write_bench_marker(tmp_path / "ok" / "b.json", updater_root=root)
    require_bench_marker(
        root, _marker_path=ok_marker, _expected_uid=os.geteuid(), _machine_id="bench-machine-0001"
    )


# --- 2. Pin, protected paths, runtime/control drift, and space budget (§8) --------------------


def test_pin_protected_paths_runtime_and_space_refuse_before_publication(tmp_path):
    fx = _setup_enrolled_legacy_box(tmp_path)
    root, bare, work, c1, c2 = fx["root"], fx["bare"], fx["work"], fx["c1"], fx["c2"]
    svc = FakeServiceFixture(root=root)

    # A) Foreign origin URL refused before any fetch or service stop.
    with pytest.raises(PolicyRefusalError, match="foreign|trusted remote"):
        apply_update(
            root=root,
            remote_url="https://evil.example/sigint-101.git",
            pinned_commit=c2,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    assert svc.calls == []

    # B) Ref mismatch (fetched tip != pinned_commit) refused before stop.
    with pytest.raises(PolicyRefusalError, match="pin|commit|mismatch"):
        apply_update(
            root=root,
            remote_url=str(bare),
            pinned_commit="0" * 40,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    assert svc.calls == []

    # C) Non-ancestor commit refused.
    _git(work, "checkout", "--orphan", "unrelated")
    _git(work, "rm", "-rf", ".")
    (work / "README.md").write_text("unrelated\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "unrelated root")
    orphan_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-f", "origin", "unrelated:refs/heads/unrelated")
    _git(work, "checkout", "main")

    with pytest.raises(PolicyRefusalError, match="descend|ancestor"):
        fetch_and_export_release(
            remote_url=str(bare),
            trusted_remote=str(bare),
            pinned_commit=orphan_sha,
            current_commit=c1,
            ref="refs/heads/unrelated",
            dest_release_dir=root / "releases" / "rel-orphan",
            scratch_git_dir=root / "staging" / "git-orphan",
            release_id="rel-orphan",
        )

    # D) Tracked data/ payload (other than empty data/.gitkeep) refused.
    _git(work, "checkout", "-b", "bad-data", c2)
    (work / "data" / "progress.db").write_bytes(b"overwrite-learner")
    _git(work, "add", "data/progress.db")
    _git(work, "commit", "-m", "bad tracked data")
    bad_data_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "bad-data:refs/heads/bad-data")
    with pytest.raises(PolicyRefusalError, match="protected path|data/"):
        fetch_and_export_release(
            remote_url=str(bare),
            trusted_remote=str(bare),
            pinned_commit=bad_data_sha,
            current_commit=c1,
            ref="refs/heads/bad-data",
            dest_release_dir=root / "releases" / "rel-baddata",
            scratch_git_dir=root / "staging" / "git-baddata",
            release_id="rel-baddata",
        )

    # E) Symlink inside candidate Git tree refused.
    _git(work, "checkout", "-b", "bad-symlink", c2)
    (work / "escape_link").symlink_to("/etc/passwd")
    _git(work, "add", "escape_link")
    _git(work, "commit", "-m", "symlink escape")
    bad_sym_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "bad-symlink:refs/heads/bad-symlink")
    with pytest.raises(PolicyRefusalError, match="symlink"):
        fetch_and_export_release(
            remote_url=str(bare),
            trusted_remote=str(bare),
            pinned_commit=bad_sym_sha,
            current_commit=c1,
            ref="refs/heads/bad-symlink",
            dest_release_dir=root / "releases" / "rel-badsym",
            scratch_git_dir=root / "staging" / "git-badsym",
            release_id="rel-badsym",
        )

    # F) Deployed-control drift refused; requirements.txt drift refused.
    _git(work, "checkout", "-b", "bad-ctrl", c2)
    (work / "deploy" / "managed" / "launcher.py").write_text("# drifted\n", encoding="utf-8")
    _git(work, "add", "deploy/managed/launcher.py")
    _git(work, "commit", "-m", "control drift")
    bad_ctrl_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "bad-ctrl:refs/heads/bad-ctrl")
    with pytest.raises(PolicyRefusalError, match="deployed-control|control"):
        apply_update(
            root=root,
            remote_url=str(bare),
            pinned_commit=bad_ctrl_sha,
            ref="refs/heads/bad-ctrl",
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )

    _git(work, "checkout", "-b", "bad-req", c2)
    (work / "requirements.txt").write_text("fastapi==99.0.0\n", encoding="utf-8")
    _git(work, "add", "requirements.txt")
    _git(work, "commit", "-m", "req drift")
    bad_req_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "bad-req:refs/heads/bad-req")
    with pytest.raises(PolicyRefusalError, match="requirements"):
        apply_update(
            root=root,
            remote_url=str(bare),
            pinned_commit=bad_req_sha,
            ref="refs/heads/bad-req",
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )

    # G) Initial low-space refusal leaves service running untouched.
    class _LowStat:
        f_bavail = 1
        f_frsize = 4096

    with pytest.raises(SpaceBudgetError, match="required"):
        apply_update(
            root=root,
            remote_url=str(bare),
            pinned_commit=c2,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
            statvfs_fn=lambda _p: _LowStat(),
        )
    assert svc.calls == []

    # H) Post-stop low-space refusal (recordings grew!) restarts the old service with old pair!
    space_calls = {"n": 0}

    class _OkStat:
        f_bavail = 10_000_000
        f_frsize = 4096

    def _shrink_after_stop(_p):
        space_calls["n"] += 1
        return _OkStat() if space_calls["n"] == 1 else _LowStat()

    with pytest.raises(SpaceBudgetError, match="required"):
        apply_update(
            root=root,
            remote_url=str(bare),
            pinned_commit=c2,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
            statvfs_fn=_shrink_after_stop,
        )
    assert "stop" in svc.calls
    assert "reset-failed" in svc.calls
    assert "start" in svc.calls
    assert svc.active_state == "active"
    sel_after = json.loads((root / "current.json").read_text(encoding="utf-8"))
    assert sel_after["mode"] == "legacy"


# --- 3. Full apply (convert + upgrade), complete generation, byte-stable manifests (§8) -------


def test_apply_exports_exact_archive_copies_complete_generation_and_keeps_manifests_byte_stable(
    tmp_path,
):
    fx = _setup_enrolled_legacy_box(tmp_path)
    root, bare, c2 = fx["root"], fx["bare"], fx["c2"]
    svc = FakeServiceFixture(root=root)

    # Capture original legacy file bytes to verify source is never mutated.
    orig_prog_bytes = (fx["legacy_data"] / "progress.db").read_bytes()
    orig_sstv_mtime = (fx["legacy_data"] / "sstv" / "pass1.png").stat().st_mtime_ns

    receipt = apply_update(
        root=root,
        remote_url=str(bare),
        pinned_commit=c2,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
        unit_settings=UnitSettings(unit_env={"SIGINT101_RX_LAT": "37.77"}),
    )
    assert receipt["status"] == "ready"
    assert receipt["commit"] == c2
    assert receipt["action"] == "convert"

    sel = json.loads((root / "current.json").read_text(encoding="utf-8"))
    assert sel["mode"] == "managed"
    assert sel["commit"] == c2
    rel_dir = root / "releases" / sel["release_id"]
    gen_dir = root / "data" / sel["generation_id"]

    # Source legacy data untouched byte-for-byte.
    assert (fx["legacy_data"] / "progress.db").read_bytes() == orig_prog_bytes

    # Staged generation carries migrated v1 stores, rotations, APT TLE, SSTV sidecars & hidden dirs.
    assert (gen_dir / "events.jsonl.1").read_text(encoding="utf-8") == '{"kind":"older"}\n'
    assert (gen_dir / "apt" / "tle.txt").read_text(encoding="utf-8") == "NOAA 19\n1 33591U\n"
    assert (gen_dir / "sstv" / "pass1.json").is_file()
    assert (gen_dir / "sstv" / ".staging" / "inprog.png").is_file()
    assert (gen_dir / "sstv" / ".discard" / "bad.png").is_file()
    assert (gen_dir / "sstv" / "pass1.png").stat().st_mtime_ns == orig_sstv_mtime
    assert (gen_dir / GENERATION_MARKER_FILENAME).is_file()

    # Release manifest is byte-stable with zero bytecode, and catches real payload tampering.
    assert not list(rel_dir.rglob("*.pyc"))
    assert not list(rel_dir.rglob("__pycache__"))
    verify_release_manifest_integrity(rel_dir)

    # Preview / no-op on same commit makes zero staging, lock, or service changes!
    calls_before = list(svc.calls)
    noop = apply_update(
        root=root,
        remote_url=str(bare),
        pinned_commit=c2,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert noop["status"] == "noop"
    assert svc.calls == calls_before

    # Read-only --check returns 0 for same commit, 10 for newer tip, and writes nothing.
    chk_same = check_status(root=root, remote_url=str(bare), home=fx["home"])
    assert chk_same["exit_code"] == 0
    assert chk_same["commit"] == c2


# --- 4. Pre-commit crash matrix, post-commit lost-ack, and switch fence (§8) ------------------


@pytest.mark.parametrize(
    "crash_at",
    [
        "after_intent_sync",
        "after_release_export",
        "after_service_stop",
        "after_snapshot_copy",
        "after_trial",
        "after_prepared_sync",
        "after_selector_tmp_fsync_before_replace",
    ],
)
def test_precommit_failures_reboot_old_pair_with_learning_intact(tmp_path, crash_at):
    fx = _setup_enrolled_legacy_box(tmp_path)
    root, bare, c2 = fx["root"], fx["bare"], fx["c2"]
    svc = FakeServiceFixture(root=root)
    old_sel = json.loads((root / "current.json").read_text(encoding="utf-8"))

    class _InjectedCrash(RuntimeError):
        pass

    def _bomb(stage: str) -> None:
        if stage == crash_at:
            raise _InjectedCrash(stage)

    with pytest.raises(_InjectedCrash, match=crash_at):
        apply_update(
            root=root,
            remote_url=str(bare),
            pinned_commit=c2,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
            on_boundary=_bomb,
        )

    # Old selection stays authoritative across every pre-commit crash!
    assert json.loads((root / "current.json").read_text(encoding="utf-8")) == old_sel

    # Plain recovery brings the old pair back cleanly and reports the incomplete intent.
    rec = recover(
        root=root,
        service=svc,
        home=fx["home"],
        bench_marker_checker=fx["bench_check"],
    )
    assert rec["status"] == "ready"
    assert rec["mode"] == "legacy"
    assert rec["incomplete_intents"], "incomplete intent should be reported"


def test_postcommit_readiness_failure_does_not_revert_acknowledged_learning(tmp_path):
    fx = _setup_enrolled_legacy_box(tmp_path)
    root, bare, c2 = fx["root"], fx["bare"], fx["c2"]
    # New service writes served.json AND acknowledges a learner write (lesson 2 prediction),
    # then readiness polling times out (lost acknowledgement).
    l2_choice = load_lessons(REPO_ROOT / "lesson_packs")[2].choices[0]["key"]
    svc = FakeServiceFixture(
        root=root,
        fail_readiness_after_serve=True,
        write_learner_row_on_serve=(2, l2_choice),
    )

    receipt = apply_update(
        root=root,
        remote_url=str(bare),
        pinned_commit=c2,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
        ready_timeout_s=0.2,
    )
    assert receipt["status"] == "committed, console not ready"
    op_id = receipt["operation_id"]

    # Selection MUST NOT auto-revert!
    sel = json.loads((root / "current.json").read_text(encoding="utf-8"))
    assert sel["mode"] == "managed"
    assert sel["operation_id"] == op_id

    # Served marker exists -> explicit abort MUST REFUSE without stopping the running service!
    calls_before_abort = list(svc.calls)
    with pytest.raises(AbortError, match="serving fence"):
        abort_unserved(
            root=root,
            operation_id=op_id,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    assert svc.calls == calls_before_abort

    # Plain recovery acknowledges/keeps the new pair and preserves the acknowledged learner write.
    svc.fail_readiness_after_serve = False
    rec = recover(
        root=root,
        service=svc,
        home=fx["home"],
        bench_marker_checker=fx["bench_check"],
    )
    assert rec["status"] == "ready"
    assert rec["operation_id"] == op_id
    p = Progress(root / "data" / sel["generation_id"] / "progress.db", managed=True, read_only=True)
    try:
        assert p.get_prediction(2) == l2_choice
    finally:
        p.close()


# --- 5. Public-only startup failure & explicit unserved abort (§8, U1) ------------------------


def test_public_only_start_failure_aborts_to_exact_prior_pair_and_handles_crash_retry(tmp_path):
    fx = _setup_enrolled_legacy_box(tmp_path)
    root, bare, c2 = fx["root"], fx["bare"], fx["c2"]
    # Private trial passes, selector commits, THEN public startup fails before served.json!
    svc = FakeServiceFixture(root=root, fail_public_startup=True)

    receipt = apply_update(
        root=root,
        remote_url=str(bare),
        pinned_commit=c2,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
        ready_timeout_s=0.2,
    )
    assert receipt["status"] == "committed, console not ready"
    failed_op = receipt["operation_id"]
    sel_failed = json.loads((root / "current.json").read_text(encoding="utf-8"))
    failed_gen_dir = root / "data" / sel_failed["generation_id"]
    assert inspect_serving_marker(
        root, failed_op, sel_failed["release_id"], sel_failed["generation_id"]
    ) == "absent"

    # Interrupt abort BEFORE selector replacement -> failed_op stays selected and fails start.
    class _AbortCrash(RuntimeError):
        pass

    with pytest.raises(AbortError, match="console not ready.*--recover") as crash_exc:
        abort_unserved(
            root=root,
            operation_id=failed_op,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
            ready_timeout_s=0.2,
            on_boundary=lambda s: (_ for _ in ()).throw(_AbortCrash())
            if s == "after_abort_selector_tmp_fsync"
            else None,
        )
    assert isinstance(crash_exc.value.__cause__, _AbortCrash)
    cur_after_crash = json.loads((root / "current.json").read_text(encoding="utf-8"))
    assert cur_after_crash["operation_id"] == failed_op

    # Complete abort -> restores exact legacy pair with fresh abort op ID, keeping failed gen.
    svc.fail_public_startup = False
    aborted = abort_unserved(
        root=root,
        operation_id=failed_op,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert aborted["status"] == "ready"
    assert aborted["mode"] == "legacy"
    assert aborted["aborted_operation_id"] == failed_op
    assert failed_gen_dir.is_dir()

    # Completed retry of the same abort is idempotent and makes zero service calls!
    calls_before_retry = list(svc.calls)
    retried = abort_unserved(
        root=root,
        operation_id=failed_op,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert retried["operation_id"] == aborted["operation_id"]
    assert retried["aborted_operation_id"] == failed_op
    assert svc.calls == calls_before_retry


def test_unserved_abort_refuses_uncertain_stale_and_raced_markers(tmp_path):
    fx = _setup_enrolled_legacy_box(tmp_path)
    root, bare, c2 = fx["root"], fx["bare"], fx["c2"]
    svc = FakeServiceFixture(root=root, fail_public_startup=True)

    receipt = apply_update(
        root=root,
        remote_url=str(bare),
        pinned_commit=c2,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
        ready_timeout_s=0.2,
    )
    op_id = receipt["operation_id"]
    sel = json.loads((root / "current.json").read_text(encoding="utf-8"))
    ops_dir = root / "operations" / op_id

    # 1) Stale/unknown operation ID refused before stopping service.
    calls_before = list(svc.calls)
    with pytest.raises(AbortError, match="does not match"):
        abort_unserved(
            root=root,
            operation_id="op-stale-999",
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    assert svc.calls == calls_before

    # 2) Malformed / unreadable / mismatched serving marker -> uncertain -> refused before stop!
    marker = ops_dir / "served.json"
    marker.write_text("{broken json", encoding="utf-8")
    state = inspect_serving_marker(root, op_id, sel["release_id"], sel["generation_id"])
    assert state == "uncertain"
    with pytest.raises(AbortError, match="uncertain|malformed|serving marker"):
        abort_unserved(
            root=root,
            operation_id=op_id,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    assert svc.calls == calls_before
    marker.unlink()

    # 3) Marker written during stop (raced!) -> post-stop recheck refuses & restarts selected pair!
    svc.fail_public_startup = False

    def _plant_marker_on_stop():
        marker.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "operation_id": op_id,
                    "release_id": sel["release_id"],
                    "generation_id": sel["generation_id"],
                }
            ),
            encoding="utf-8",
        )
        os.chmod(marker, 0o600)

    svc.post_stop_hook = _plant_marker_on_stop
    with pytest.raises(AbortError, match="served"):
        abort_unserved(
            root=root,
            operation_id=op_id,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
        )
    assert json.loads((root / "current.json").read_text(encoding="utf-8"))["operation_id"] == op_id
    assert svc.active_state == "active"


# --- 6. Start-limit reset & unrelated listener refusal (§8, U8) -------------------------------


def test_start_limit_is_reset_before_activation_and_recover_and_unrelated_listener(tmp_path):
    fx = _setup_enrolled_legacy_box(tmp_path)
    root, bare, c2 = fx["root"], fx["bare"], fx["c2"]
    svc = FakeServiceFixture(root=root, start_limit_remaining=2)
    # Exhaust start limit with blocked maintenance attempts.
    svc.record_blocked_start()
    svc.record_blocked_start()
    assert svc.start_limited is True

    receipt = apply_update(
        root=root,
        remote_url=str(bare),
        pinned_commit=c2,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert receipt["status"] == "ready"
    assert svc.calls.index("reset-failed") < svc.calls.index("start")

    # Unrelated listener while unit is stopped -> recover MUST refuse!
    svc.active_state = "inactive"
    svc.sub_state = "dead"
    svc.main_pid = 0
    svc.listener_active = True
    svc.unrelated_listener = True
    with pytest.raises(RecoveryError, match="unrelated listener"):
        recover(
            root=root,
            service=svc,
            home=fx["home"],
            bench_marker_checker=fx["bench_check"],
        )


def test_switch_fence_re_fsyncs_after_selector_replace_before_parent_sync(tmp_path):
    fx = _setup_enrolled_legacy_box(tmp_path)
    root, bare, c2 = fx["root"], fx["bare"], fx["c2"]
    svc = FakeServiceFixture(root=root)

    class _PowerCutAfterReplace(RuntimeError):
        pass

    with pytest.raises(_PowerCutAfterReplace):
        apply_update(
            root=root,
            remote_url=str(bare),
            pinned_commit=c2,
            service=svc,
            home=fx["home"],
            kiosk_process_lister=lambda: [],
            bench_marker_checker=fx["bench_check"],
            on_boundary=lambda s: (_ for _ in ()).throw(_PowerCutAfterReplace())
            if s == "after_selector_replace_before_parent_sync"
            else None,
        )

    # New selector is visible on disk; recover re-fsyncs selector + ROOT before starting service.
    sel = json.loads((root / "current.json").read_text(encoding="utf-8"))
    assert sel["mode"] == "managed"
    rec = recover(
        root=root,
        service=svc,
        home=fx["home"],
        bench_marker_checker=fx["bench_check"],
    )
    assert rec["status"] == "ready"
    assert rec["operation_id"] == sel["operation_id"]
    assert inspect_serving_marker(
        root, sel["operation_id"], sel["release_id"], sel["generation_id"]
    ) == "served"


def test_managed_to_managed_apply_and_unserved_abort_restores_prior_managed_pair(tmp_path):
    """Test managed->managed apply, followed by a failed v3 public start aborted back to v2."""
    fx = _setup_enrolled_legacy_box(tmp_path)
    root, bare, work, c2 = fx["root"], fx["bare"], fx["work"], fx["c2"]
    svc = FakeServiceFixture(root=root)

    # First convert legacy -> v2 (managed).
    r2 = apply_update(
        root=root,
        remote_url=str(bare),
        pinned_commit=c2,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert r2["status"] == "ready"
    sel_v2 = json.loads((root / "current.json").read_text(encoding="utf-8"))

    # Push v3 on main.
    _git(work, "checkout", "main")
    (work / "COURSE_REV").write_text("v3\n", encoding="utf-8")
    _git(work, "add", "COURSE_REV")
    _git(work, "commit", "-m", "v3 course update")
    c3 = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")

    # Read-only --check sees different tip -> exit_code 10 without touching service!
    calls_before_check = list(svc.calls)
    chk = check_status(root=root, remote_url=str(bare), home=fx["home"])
    assert chk["exit_code"] == 10
    assert chk["remote_tip"] == c3
    assert svc.calls == calls_before_check

    # Apply v3 with public startup failing before serving marker -> unserved abort back to v2!
    svc.fail_public_startup = True
    r3 = apply_update(
        root=root,
        remote_url=str(bare),
        pinned_commit=c3,
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
        ready_timeout_s=0.2,
    )
    assert r3["status"] == "committed, console not ready"
    assert r3["abort_eligible"] is True

    svc.fail_public_startup = False
    aborted = abort_unserved(
        root=root,
        operation_id=r3["operation_id"],
        service=svc,
        home=fx["home"],
        kiosk_process_lister=lambda: [],
        bench_marker_checker=fx["bench_check"],
    )
    assert aborted["status"] == "ready"
    assert aborted["mode"] == "managed"
    assert aborted["release_id"] == sel_v2["release_id"]
    assert aborted["generation_id"] == sel_v2["generation_id"]
    restored = json.loads((root / "current.json").read_text(encoding="utf-8"))
    assert restored["commit"] == c2
    assert restored["predecessor"] == sel_v2["predecessor"]

