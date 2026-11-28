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
from .runtime import probe_python_abi, stage_sealed_runtime, verify_runtime_compatibility
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


def _probe_checkout_git_bytes(checkout: Path, *git_args: str) -> bytes | None:
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
        close_fds=True,
        timeout=10,
    )
    if res.returncode != 0 or not res.stdout:
        return None
    return res.stdout


def _load_reviewed_launchers(repo: Path) -> tuple[set[str], str | None, str | None]:
    """Parse deploy/reviewed-launchers.sha256 into (legacy_digests, default_legacy, managed)."""
    path = repo / "deploy" / "reviewed-launchers.sha256"
    legacy_digests: set[str] = set()
    default_legacy: str | None = None
    managed_digest: str | None = None
    if not path.is_file() or path.is_symlink():
        return legacy_digests, default_legacy, managed_digest
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, label = parts[0].lower(), parts[1].strip()
        if len(digest) != 64:
            continue
        if label == "deploy/managed/sigint101-course-managed.sh":
            managed_digest = digest
        else:
            legacy_digests.add(digest)
            if label == "deploy/kiosk/start-kiosk.sh":
                default_legacy = digest
    return legacy_digests, default_legacy, managed_digest


def _nearest_existing_dir(path: Path) -> Path:
    cur = path
    while not cur.exists():
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return cur


def _dir_is_writable(path: Path) -> tuple[bool, Path]:
    anc = _nearest_existing_dir(path)
    if not anc.is_dir():
        return False, anc
    use_eff = os.access in os.supports_effective_ids
    return os.access(anc, os.W_OK | os.X_OK, effective_ids=use_eff), anc


def _preflight_install_targets(
    *,
    root: Path,
    checkout: Path,
    unit_file: Path,
    wrapper_file: Path,
    managed_dir: Path,
) -> bool:
    """Verify every install target is writable BEFORE creating locks, shims, or current.json (R2-2).

    Returns True when the desktop wrapper already holds the exact managed wrapper bytes (e.g.
    after the operator ran the printed sudo install step) and needs no write.
    """
    managed_wrapper_src = managed_dir / "sigint101-course-managed.sh"
    managed_wrapper_bytes = managed_wrapper_src.read_bytes()

    wrapper_already_installed = False
    if wrapper_file.is_dir():
        raise EnrollmentError(
            f"Permission denied: desktop wrapper target is a directory: {wrapper_file} "
            "(no changes made)"
        )
    if (
        wrapper_file.is_file()
        and not wrapper_file.is_symlink()
        and wrapper_file.read_bytes() == managed_wrapper_bytes
        and bool(wrapper_file.stat().st_mode & 0o111)
    ):
        wrapper_already_installed = True
    else:
        ok, anc = _dir_is_writable(wrapper_file.parent)
        if not ok:
            raise EnrollmentError(
                f"Permission denied: cannot write install target {wrapper_file} "
                f"(directory {anc} is not writable); install the managed desktop wrapper with: "
                f"sudo install -m 0755 {managed_wrapper_src} {wrapper_file} "
                "(or pass --desktop-wrapper <writable-path>), then rerun --enroll "
                "(no changes made)"
            )

    if unit_file.is_dir():
        raise EnrollmentError(
            f"Permission denied: unit target is a directory: {unit_file} (no changes made)"
        )
    ok_unit, anc_unit = _dir_is_writable(unit_file.parent)
    if not ok_unit:
        raise EnrollmentError(
            f"Permission denied: cannot write install target {unit_file} "
            f"(directory {anc_unit} is not writable); fix permissions with: "
            f"sudo chown {os.geteuid()}:{os.getegid()} {anc_unit} && chmod 0755 {anc_unit} "
            "and rerun --enroll (no changes made)"
        )

    deploy_dir = checkout / "deploy"
    if deploy_dir.is_dir():
        ok_deploy, anc_deploy = _dir_is_writable(deploy_dir)
        if not ok_deploy:
            raise EnrollmentError(
                f"Permission denied: cannot write checkout shims under {deploy_dir} "
                f"(directory {anc_deploy} is not writable); fix permissions with: "
                f"sudo chown {os.geteuid()}:{os.getegid()} {anc_deploy} && chmod 0755 {anc_deploy} "
                "and rerun --enroll (no changes made)"
            )

    ok_root, anc_root = _dir_is_writable(root)
    if not ok_root:
        raise EnrollmentError(
            f"Permission denied: cannot write updater root {root} "
            f"(directory {anc_root} is not writable); fix permissions with: "
            f"sudo chown {os.geteuid()}:{os.getegid()} {anc_root} && chmod 0700 {anc_root} "
            "and rerun --enroll (no changes made)"
        )

    return wrapper_already_installed


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

    # 1b) R2-2: Preflight EVERY install target (including /usr/local/bin/sigint101-course)
    # BEFORE creating locks, staging runtimes, writing current.json, or touching shims!
    wrapper_already_installed = _preflight_install_targets(
        root=root,
        checkout=checkout,
        unit_file=unit_file,
        wrapper_file=wrapper_file,
        managed_dir=managed_dir,
    )

    # 2) Create permanent lock inodes and hold exclusive mutation gate.
    ensure_lock_files(root, home=home)
    with acquire_mutation_gate(
        root, blocking=False, inherited_gate_fd=inherited_gate_fd, home=home
    ):
        # 3) Stage sealed dependency snapshot probed against host_python ABI (F4, U3, R2-1).
        # Never blindly reuse an existing runtime directory: verify its manifest or refuse.
        runtime_dir_path = root / "runtimes" / runtime_id
        abi = probe_python_abi(host_python)
        if runtime_dir_path.exists():
            rt_manifest = verify_runtime_compatibility(
                runtime_dir_path,
                current_abi=abi,
                verify_files=True,
                forbidden_checkout_root=checkout,
            )
        else:
            rt_manifest = stage_sealed_runtime(
                source_site_packages,
                runtime_dir_path,
                runtime_id=runtime_id,
                python_abi=abi,
                forbidden_checkout_root=checkout,
            )
        skipped_pth_files = list(rt_manifest.get("skipped_pth_files", []))

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

        # 6) Resolve original legacy script and launcher digests (R2-2: never record shims as
        # originals; recognise reviewed legacy launchers via deploy/reviewed-launchers.sha256),
        # persist enrollment.json durably, then install checkout shims.
        prior_enrollment: dict = {}
        enr_path = root / "enrollment.json"
        if enr_path.is_file() and not enr_path.is_symlink():
            try:
                loaded_enr = json.loads(enr_path.read_text(encoding="utf-8"))
                if isinstance(loaded_enr, dict):
                    prior_enrollment = loaded_enr
            except (OSError, ValueError):
                prior_enrollment = {}

        prior_shims = prior_enrollment.get("shims")
        if not isinstance(prior_shims, dict):
            prior_shims = {}

        shim_records: dict[str, dict[str, str]] = {}
        shim_writes: list[tuple[Path, bytes]] = []
        for legacy_name, shim_name in (
            ("install.sh", "install-shim.sh"),
            ("provision.sh", "provision-shim.sh"),
        ):
            target = checkout / "deploy" / legacy_name
            shim_bytes = (managed_dir / shim_name).read_bytes()
            shim_sha = hashlib.sha256(shim_bytes).hexdigest()
            cur_bytes = target.read_bytes() if target.is_file() else b""
            cur_sha = hashlib.sha256(cur_bytes).hexdigest()

            if cur_sha != shim_sha:
                orig_sha = cur_sha
            else:
                # Target already carries the shim (rerun): recover original digest from prior
                # enrollment.json or from the enrolled git commit; never record shim_sha!
                prev_entry = prior_shims.get(legacy_name)
                prev_orig = (
                    prev_entry.get("original_sha256")
                    if isinstance(prev_entry, dict)
                    else None
                )
                if isinstance(prev_orig, str) and prev_orig and prev_orig != shim_sha:
                    orig_sha = prev_orig
                else:
                    git_blob = (
                        _probe_checkout_git_bytes(
                            checkout, "show", f"{resolved_commit}:deploy/{legacy_name}"
                        )
                        if resolved_commit
                        else None
                    )
                    git_sha = hashlib.sha256(git_blob).hexdigest() if git_blob else None
                    if git_sha and git_sha != shim_sha:
                        orig_sha = git_sha
                    else:
                        raise EnrollmentError(
                            f"refusing rerun: cannot determine original digest for already-"
                            f"shimmed deploy/{legacy_name}"
                        )

            shim_records[legacy_name] = {
                "original_sha256": orig_sha,
                "shim_sha256": shim_sha,
            }
            if target.parent.is_dir():
                shim_writes.append((target, shim_bytes))

        reviewed_legacy_digests, default_legacy_digest, reviewed_managed_digest = (
            _load_reviewed_launchers(repo)
        )
        managed_wrapper_bytes = (managed_dir / "sigint101-course-managed.sh").read_bytes()
        managed_wrapper_sha = hashlib.sha256(managed_wrapper_bytes).hexdigest()
        prior_wrapper = prior_enrollment.get("desktop_wrapper")
        prior_wrapper_orig = (
            prior_wrapper.get("original_sha256")
            if isinstance(prior_wrapper, dict)
            else None
        )
        cur_wrapper_sha = (
            hashlib.sha256(wrapper_file.read_bytes()).hexdigest()
            if wrapper_file.is_file() and not wrapper_file.is_symlink()
            else None
        )
        if cur_wrapper_sha in reviewed_legacy_digests:
            wrapper_orig_sha = cur_wrapper_sha
            wrapper_orig_reviewed = True
        elif cur_wrapper_sha in (managed_wrapper_sha, reviewed_managed_digest):
            if (
                isinstance(prior_wrapper_orig, str)
                and prior_wrapper_orig
                and prior_wrapper_orig not in (managed_wrapper_sha, reviewed_managed_digest)
            ):
                wrapper_orig_sha = prior_wrapper_orig
                wrapper_orig_reviewed = prior_wrapper_orig in reviewed_legacy_digests
            else:
                wrapper_orig_sha = default_legacy_digest
                wrapper_orig_reviewed = default_legacy_digest is not None
        elif cur_wrapper_sha is not None:
            wrapper_orig_sha = cur_wrapper_sha
            wrapper_orig_reviewed = False
        else:
            wrapper_orig_sha = default_legacy_digest
            wrapper_orig_reviewed = default_legacy_digest is not None

        desktop_wrapper_record = {
            "path": str(wrapper_file),
            "original_sha256": wrapper_orig_sha,
            "original_reviewed": wrapper_orig_reviewed,
            "managed_sha256": managed_wrapper_sha,
        }

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
            "skipped_pth_files": skipped_pth_files,
            "desktop_wrapper": desktop_wrapper_record,
            "shims": shim_records,
        }
        _atomic_write_bytes(
            enr_path,
            (json.dumps(enrollment_meta, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )

        for target, shim_bytes in shim_writes:
            _atomic_write_bytes(target, shim_bytes, mode=0o755)

        # 7) Install gate-aware desktop wrapper atomically before unit switch/drain (or verify
        # exact managed wrapper bytes if operator already installed it via sudo install).
        if not wrapper_already_installed:
            _atomic_write_bytes(wrapper_file, managed_wrapper_bytes, mode=0o755)
        elif wrapper_file.read_bytes() != managed_wrapper_bytes:
            raise EnrollmentError(f"desktop wrapper drifted during enrollment: {wrapper_file}")

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
        "skipped_pth_files": skipped_pth_files,
        "dispatcher": str(dispatcher_dest),
        "unit_path": str(unit_file),
    }
