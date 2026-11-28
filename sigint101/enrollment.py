# ABOUTME: Fixture-level enrollment prerequisite workflow establishing permanent locks, control
# ABOUTME: generation, sealed runtime, legacy selector, shims, and atomic unit replacement.

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from .bench_gate import require_bench_marker
from .kiosk import exit_flag
from .locks import acquire_mutation_gate, ensure_lock_files
from .process_checks import require_kiosk_page_closed
from .runtime import probe_python_abi, stage_sealed_runtime
from .storage import fsync_dir, fsync_file, validate_updater_root


class EnrollmentError(RuntimeError):
    """Raised when an enrollment prerequisite or ordering invariant fails."""


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.fspath(tmp), flags, mode)
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    fsync_dir(path.parent)


def _load_control_manifest_module(repo_root: Path):
    mod_path = repo_root / "deploy" / "managed" / "control_manifest.py"
    spec = importlib.util.spec_from_file_location("_sigint101_control_manifest", mod_path)
    if spec is None or spec.loader is None:
        raise EnrollmentError(f"cannot load control_manifest from {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _render_managed_unit(
    *,
    template_path: Path,
    host_python: str | Path,
    dispatcher_path: Path,
    updater_root: Path,
) -> str:
    """Render the installed managed unit directly from deploy/managed/sigint101.service (F4/F13)."""
    template = template_path.read_text(encoding="utf-8")
    rendered = template.replace(
        "WorkingDirectory=%h/.local/state/sigint101-updater",
        f"WorkingDirectory={updater_root}",
    ).replace(
        "ExecStart=/usr/bin/python3 -B %h/.local/state/sigint101-updater/bin/dispatcher.py serve",
        f"ExecStart={host_python} -B {dispatcher_path} --root {updater_root} serve",
    )
    if str(dispatcher_path) not in rendered:
        raise EnrollmentError("managed unit template did not match expected ExecStart line")
    return rendered


def _probe_checkout_git_value(checkout: Path, *git_args: str) -> str | None:
    if not (checkout / ".git").exists():
        return None
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(checkout),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    res = subprocess.run(
        ["git", "-C", str(checkout), *git_args],
        env=env,
        capture_output=True,
        text=True,
        close_fds=True,
        timeout=10,
    )
    if res.returncode != 0:
        return None
    val = res.stdout.strip()
    return val or None


def enroll_legacy_fixture(
    *,
    repo_root: str | Path,
    updater_root: str | Path,
    legacy_checkout: str | Path,
    legacy_data_dir: str | Path,
    legacy_python: str | Path,
    source_site_packages: str | Path,
    unit_path: str | Path,
    desktop_wrapper_path: str | Path,
    home: str | Path,
    runtime_dir: str | Path,
    host_python: str | Path = "/usr/bin/python3",
    trusted_remote: str | None = None,
    legacy_commit: str | None = None,
    requirements_sha256: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8008,
    operation_id: str = "enroll-0001",
    control_id: str = "ctrl-0001",
    runtime_id: str = "rt-0001",
    kiosk_process_lister=None,
    reload_systemd: Callable[[], None] | None = None,
    drain_old_service: Callable[[], None] | None = None,
    on_boundary: Callable[[str], None] | None = None,
    inherited_gate_fd: int | None = None,
    bench_marker_checker: Callable[[Path], None] = require_bench_marker,
) -> dict:
    """Run the ordered enrollment prerequisites in a fixture without touching live host paths.

    Ordering guarantees that every interruption leaves either the untouched legacy unit+checkout
    or a fully installed dispatcher + synced `legacy` selector booting the exact original pair.
    """
    root = validate_updater_root(updater_root, home=home)
    # PR 4 §9.4: refuse on non-bench hosts BEFORE creating directories or locks, editing files,
    # invoking service commands, or running candidate code.
    bench_marker_checker(root)

    repo = Path(repo_root).resolve()
    checkout = Path(legacy_checkout).resolve()
    data_dir = Path(legacy_data_dir).resolve()
    unit_file = Path(unit_path)
    wrapper_file = Path(desktop_wrapper_path)
    managed_dir = repo / "deploy" / "managed"

    resolved_remote = trusted_remote or _probe_checkout_git_value(
        checkout, "config", "--get", "remote.origin.url"
    )
    resolved_commit = legacy_commit or _probe_checkout_git_value(checkout, "rev-parse", "HEAD")
    req_file = checkout / "requirements.txt"
    resolved_req_sha = requirements_sha256
    if resolved_req_sha is None and req_file.is_file() and not req_file.is_symlink():
        resolved_req_sha = hashlib.sha256(req_file.read_bytes()).hexdigest()

    # 1) Require closed kiosk page and deliberate Exit flag before changing anything.
    lister_kw = {"process_lister": kiosk_process_lister} if kiosk_process_lister is not None else {}
    require_kiosk_page_closed(home=home, **lister_kw)
    if not exit_flag(runtime_dir).exists():
        raise EnrollmentError("deliberate Exit flag is required before enrollment starts")

    # 2) Create permanent lock inodes and hold exclusive mutation gate.
    ensure_lock_files(root, home=home)
    with acquire_mutation_gate(
        root, blocking=False, inherited_gate_fd=inherited_gate_fd, home=home
    ):
        # 3) Stage sealed dependency snapshot probed against host_python ABI (F4, U3).
        runtime_dir_path = root / "runtimes" / runtime_id
        if not runtime_dir_path.exists():
            abi = probe_python_abi(host_python)
            stage_sealed_runtime(
                source_site_packages,
                runtime_dir_path,
                runtime_id=runtime_id,
                python_abi=abi,
                forbidden_checkout_root=checkout,
            )

        # 4) Stage initial control generation and stable dispatcher.
        ctrl_mod = _load_control_manifest_module(repo)
        control_dir = root / "control" / control_id
        if not control_dir.exists():
            ctrl_mod.stage_control_generation(repo, control_dir, control_id=control_id)

        bin_dir = root / "bin"
        bin_dir.mkdir(mode=0o700, exist_ok=True)
        dispatcher_dest = bin_dir / "dispatcher.py"
        _atomic_write_bytes(
            dispatcher_dest,
            (managed_dir / "dispatcher.py").read_bytes(),
            mode=0o700,
        )

        if on_boundary is not None:
            on_boundary("before_control_sync")

        control_payload = {
            "format_version": 1,
            "control_id": control_id,
        }
        _atomic_write_bytes(
            root / "control.json",
            (json.dumps(control_payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )

        if on_boundary is not None:
            on_boundary("before_legacy_selector_sync")

        # 5) Write and sync durable initial `legacy` selector before replacing unit/shims!
        selector_payload = {
            "format_version": 1,
            "mode": "legacy",
            "operation_id": operation_id,
            "checkout_dir": str(checkout),
            "data_dir": str(data_dir),
            "python_executable": str(legacy_python),
            "runtime_id": runtime_id,
            "host": host,
            "port": port,
        }
        _atomic_write_bytes(
            root / "current.json",
            (json.dumps(selector_payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )

        # 6) Record original legacy script blobs & expected shim hashes, then install shims.
        shim_records = {}
        for legacy_name, shim_name in (
            ("install.sh", "install-shim.sh"),
            ("provision.sh", "provision-shim.sh"),
        ):
            target = checkout / "deploy" / legacy_name
            shim_bytes = (managed_dir / shim_name).read_bytes()
            orig_bytes = target.read_bytes() if target.is_file() else b""
            shim_records[legacy_name] = {
                "original_sha256": hashlib.sha256(orig_bytes).hexdigest(),
                "shim_sha256": hashlib.sha256(shim_bytes).hexdigest(),
            }
            if target.parent.is_dir():
                _atomic_write_bytes(target, shim_bytes, mode=0o755)

        enrollment_meta = {
            "operation_id": operation_id,
            "control_id": control_id,
            "runtime_id": runtime_id,
            "legacy_checkout": str(checkout),
            "legacy_data_dir": str(data_dir),
            "host_python": str(host_python),
            "legacy_python": str(legacy_python),
            "trusted_remote": resolved_remote,
            "legacy_commit": resolved_commit,
            "requirements_sha256": resolved_req_sha,
            "shims": shim_records,
        }
        _atomic_write_bytes(
            root / "enrollment.json",
            (json.dumps(enrollment_meta, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )

        # 7) Install gate-aware desktop wrapper atomically before unit switch/drain.
        _atomic_write_bytes(
            wrapper_file,
            (managed_dir / "sigint101-course-managed.sh").read_bytes(),
            mode=0o755,
        )

        if on_boundary is not None:
            on_boundary("before_unit_install")

        # 8) Atomically install the managed unit rendered from deploy/managed/sigint101.service.
        unit_text = _render_managed_unit(
            template_path=managed_dir / "sigint101.service",
            host_python=host_python,
            dispatcher_path=dispatcher_dest,
            updater_root=root,
        )
        _atomic_write_bytes(unit_file, unit_text.encode("utf-8"), mode=0o644)
        fsync_file(unit_file)

        if reload_systemd is not None:
            reload_systemd()

        if on_boundary is not None:
            on_boundary("after_unit_install_before_drain")

        if drain_old_service is not None:
            drain_old_service()

    return {
        "mode": "legacy",
        "operation_id": operation_id,
        "control_id": control_id,
        "runtime_id": runtime_id,
        "dispatcher": str(dispatcher_dest),
        "unit_path": str(unit_file),
    }
