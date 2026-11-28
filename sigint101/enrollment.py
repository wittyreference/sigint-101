# ABOUTME: Fixture-level enrollment prerequisite workflow establishing permanent locks, control
# ABOUTME: generation, sealed runtime, legacy selector, shims, and atomic unit replacement.

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

from .bench_gate import require_bench_marker
from .kiosk import exit_flag
from .locks import acquire_mutation_gate, ensure_lock_files
from .process_checks import require_kiosk_page_closed
from .runtime import (
    probe_python_abi,
    reclaim_abandoned_runtime_staging,
    stage_sealed_runtime,
    verify_runtime_compatibility,
)
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


def staged_desktop_wrapper_path(wrapper_file: Path) -> Path:
    """Return the separate privileged staging sibling for the desktop wrapper (F2)."""
    return wrapper_file.with_name(f"{wrapper_file.name}.managed")


def _nearest_existing_dir(path: Path) -> Path:
    cur = path
    while not cur.exists() and not cur.is_symlink():
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return cur


def _dir_is_writable(path: Path) -> tuple[bool, Path]:
    anc = _nearest_existing_dir(path)
    if anc.is_symlink() or not anc.is_dir():
        return False, anc
    use_eff = os.access in os.supports_effective_ids
    return os.access(anc, os.W_OK | os.X_OK, effective_ids=use_eff), anc


def _is_valid_hex_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _preflight_replaceable_file(
    target: Path,
    *,
    label: str,
    require_readable: bool = False,
    check_sticky: bool = True,
) -> None:
    """Check target type, parent chain, readability, and replaceability before any write (F3)."""
    anc = _nearest_existing_dir(target.parent)
    if anc.is_symlink() or not anc.is_dir():
        raise EnrollmentError(
            f"Permission denied: parent path for {label} is not a directory: {anc} "
            "(no changes made)"
        )
    try:
        lst = os.lstat(target)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(lst.st_mode):
        raise EnrollmentError(
            f"Permission denied: {label} is a directory: {target} (no changes made)"
        )
    if not stat.S_ISREG(lst.st_mode):
        raise EnrollmentError(
            f"Permission denied: {label} is not a regular file: {target} (no changes made)"
        )
    use_eff = os.access in os.supports_effective_ids
    if require_readable and not os.access(target, os.R_OK, effective_ids=use_eff):
        raise EnrollmentError(
            f"Permission denied: cannot read {label}: {target} (no changes made)"
        )
    if check_sticky and target.parent.is_dir():
        parent_st = os.stat(target.parent)
        if (parent_st.st_mode & stat.S_ISVTX) and os.geteuid() not in (
            lst.st_uid,
            parent_st.st_uid,
        ):
            raise EnrollmentError(
                f"Permission denied: cannot replace {label} in sticky directory {target.parent} "
                "(no changes made)"
            )


def _file_matches_managed_wrapper(path: Path, managed_bytes: bytes) -> bool:
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(lst.st_mode) or not (lst.st_mode & 0o111):
        return False
    try:
        return path.read_bytes() == managed_bytes
    except OSError:
        return False


def _preflight_install_targets(
    *,
    root: Path,
    checkout: Path,
    unit_file: Path,
    wrapper_file: Path,
    managed_dir: Path,
) -> str:
    """Verify every install target's type and replaceability BEFORE any write (R2-2, F2, F3).

    Returns wrapper_mode:
    - "direct": wrapper_file.parent is writable; enrollment activates wrapper_file only after
      publishing the completed hand-over record.
    - "staged_privileged": wrapper_file.parent is unwritable, live wrapper_file stays untouched,
      and `<wrapper>.managed` already holds exact executable managed wrapper bytes ready for
      post-enrollment activation (`sudo mv -f`).
    - "already_active": wrapper_file.parent is unwritable and wrapper_file already holds exact
      executable managed wrapper bytes (e.g. rerun after prior activation).
    """
    managed_wrapper_src = managed_dir / "sigint101-course-managed.sh"
    managed_wrapper_bytes = managed_wrapper_src.read_bytes()
    staged_wrapper = staged_desktop_wrapper_path(wrapper_file)

    # Check root itself first (must not be a symlink or non-directory file).
    try:
        root_lst = os.lstat(root)
    except FileNotFoundError:
        root_lst = None
    if root_lst is not None and (
        stat.S_ISLNK(root_lst.st_mode) or not stat.S_ISDIR(root_lst.st_mode)
    ):
        raise EnrollmentError(
            f"Permission denied: updater root is not a directory: {root} (no changes made)"
        )
    ok_root, anc_root = _dir_is_writable(root)
    if not ok_root:
        raise EnrollmentError(
            f"Permission denied: cannot write updater root {root} "
            f"(directory {anc_root} is not writable); fix permissions with: "
            f"sudo chown {os.geteuid()}:{os.getegid()} {anc_root} && chmod 0700 {anc_root} "
            "and rerun --enroll (no changes made)"
        )

    # Check systemd unit destination type and writability.
    _preflight_replaceable_file(unit_file, label="unit target", require_readable=False)
    ok_unit, anc_unit = _dir_is_writable(unit_file.parent)
    if not ok_unit:
        raise EnrollmentError(
            f"Permission denied: cannot write install target {unit_file} "
            f"(directory {anc_unit} is not writable); fix permissions with: "
            f"sudo chown {os.geteuid()}:{os.getegid()} {anc_unit} && chmod 0755 {anc_unit} "
            "and rerun --enroll (no changes made)"
        )

    # F3: check checkout/deploy AND every individual shim destination (type + replaceability).
    deploy_dir = checkout / "deploy"
    try:
        deploy_lst = os.lstat(deploy_dir)
    except FileNotFoundError:
        deploy_lst = None
    if deploy_lst is not None:
        if stat.S_ISLNK(deploy_lst.st_mode) or not stat.S_ISDIR(deploy_lst.st_mode):
            raise EnrollmentError(
                f"Permission denied: checkout deploy path is not a directory: {deploy_dir} "
                "(no changes made)"
            )
        ok_deploy, anc_deploy = _dir_is_writable(deploy_dir)
        if not ok_deploy:
            raise EnrollmentError(
                f"Permission denied: cannot write checkout shims under {deploy_dir} "
                f"(directory {anc_deploy} is not writable); fix permissions with: "
                f"sudo chown {os.geteuid()}:{os.getegid()} {anc_deploy} && chmod 0755 {anc_deploy} "
                "and rerun --enroll (no changes made)"
            )
        for legacy_name in ("install.sh", "provision.sh"):
            _preflight_replaceable_file(
                deploy_dir / legacy_name,
                label=f"checkout shim target deploy/{legacy_name}",
                require_readable=True,
            )

    # F2/F3: check desktop wrapper and its staged sibling without replacing the live entry early.
    _preflight_replaceable_file(
        wrapper_file,
        label="desktop wrapper target",
        require_readable=False,
        check_sticky=False,
    )
    _preflight_replaceable_file(
        staged_wrapper,
        label="staged desktop wrapper target",
        require_readable=False,
        check_sticky=False,
    )

    ok_wrap, anc_wrap = _dir_is_writable(wrapper_file.parent)
    if ok_wrap:
        _preflight_replaceable_file(
            wrapper_file,
            label="desktop wrapper target",
            require_readable=False,
            check_sticky=True,
        )
        return "direct"

    if _file_matches_managed_wrapper(wrapper_file, managed_wrapper_bytes):
        return "already_active"

    if _file_matches_managed_wrapper(staged_wrapper, managed_wrapper_bytes):
        return "staged_privileged"

    raise EnrollmentError(
        f"Permission denied: cannot write install target {wrapper_file} "
        f"(directory {anc_wrap} is not writable); stage the managed desktop wrapper without "
        f"replacing the live entry: sudo install -m 0755 {managed_wrapper_src} {staged_wrapper} "
        "(or pass --desktop-wrapper <writable-path>), then rerun --enroll; after enrollment "
        f"completes, activate it with: sudo mv -f {staged_wrapper} {wrapper_file} "
        "(no changes made)"
    )


def _save_original_once(originals_dir: Path, name: str, data: bytes) -> Path:
    """Persist verified original bytes once (0400, fsynced) without overwriting on reruns (F4)."""
    originals_dir.mkdir(mode=0o700, exist_ok=True)
    dest = originals_dir / name
    try:
        lst = os.lstat(dest)
    except FileNotFoundError:
        lst = None
    if lst is not None and stat.S_ISREG(lst.st_mode):
        return dest
    _atomic_write_bytes(dest, data, mode=0o400)
    return dest


def _read_regular_bytes(path: Path) -> bytes | None:
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(lst.st_mode):
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _restore_file_best_effort(path: Path, data: bytes, *, mode: int) -> bool:
    """Restore a file after a late enrollment write failure, even if temp creation fails."""
    try:
        _atomic_write_bytes(path, data, mode=mode)
        return True
    except OSError:
        pass
    try:
        flags = os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(os.fspath(path), flags)
        try:
            view = memoryview(data)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        fsync_dir(path.parent)
        return True
    except OSError:
        return False


def _is_completed_active_control(root: Path, control_id: str) -> bool:
    ctrl_raw = _read_regular_bytes(root / "control.json")
    enr_raw = _read_regular_bytes(root / "enrollment.json")
    if not ctrl_raw or not enr_raw:
        return False
    try:
        ctrl = json.loads(ctrl_raw.decode("utf-8"))
        enr = json.loads(enr_raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return (
        isinstance(ctrl, dict)
        and ctrl.get("control_id") == control_id
        and isinstance(enr, dict)
        and (enr.get("status") == "complete" or enr.get("handoff_complete") is True)
    )


def _collect_protected_ids(root: Path) -> tuple[set[str], set[str]]:
    protected_runtimes: set[str] = set()
    protected_controls: set[str] = set()
    for fname in ("current.json", "control.json", "enrollment.json"):
        raw = _read_regular_bytes(root / fname)
        if not raw:
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            rid = data.get("runtime_id")
            if isinstance(rid, str) and rid:
                protected_runtimes.add(rid)
            cid = data.get("control_id")
            if isinstance(cid, str) and cid:
                protected_controls.add(cid)
    return protected_runtimes, protected_controls


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

    # 1b) R2-2 / F2 / F3: Preflight EVERY install target (type + replaceability + permissions)
    # BEFORE creating locks, staging runtimes, writing current.json, or touching shims or wrapper!
    wrapper_mode = _preflight_install_targets(
        root=root,
        checkout=checkout,
        unit_file=unit_file,
        wrapper_file=wrapper_file,
        managed_dir=managed_dir,
    )
    staged_wrapper = staged_desktop_wrapper_path(wrapper_file)

    # 2) Create permanent lock inodes and hold exclusive mutation gate.
    ensure_lock_files(root, home=home)
    with acquire_mutation_gate(
        root, blocking=False, inherited_gate_fd=inherited_gate_fd, home=home
    ):
        # 2b) F5 & F1: Under the mutation gate, reclaim abandoned private staging siblings left by
        # killed attempts in runtimes/ and control/, never touching published or active entries.
        protected_runtimes, protected_controls = _collect_protected_ids(root)
        reclaim_abandoned_runtime_staging(root / "runtimes", protected_names=protected_runtimes)
        ctrl_mod = _load_control_manifest_module(repo)
        ctrl_mod.reclaim_abandoned_control_staging(
            root / "control", protected_names=protected_controls
        )

        # 3) Stage sealed dependency snapshot probed against host_python ABI (F4, U3, R2-1).
        # Never blindly reuse an existing runtime directory: verify its manifest or refuse.
        runtime_dir_path = root / "runtimes" / runtime_id
        abi = probe_python_abi(host_python)
        if runtime_dir_path.exists() or runtime_dir_path.is_symlink():
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

        # 4) F1: Stage control generation privately & atomically, and verify any existing control
        # generation against its manifest before selecting it or replacing the unit.
        control_dir = root / "control" / control_id
        if control_dir.exists() or control_dir.is_symlink():
            try:
                ctrl_mod.verify_control_generation(control_dir, expected_control_id=control_id)
            except (RuntimeError, OSError, ValueError) as exc:
                if not control_dir.is_symlink() and not _is_completed_active_control(
                    root, control_id
                ):
                    ctrl_mod.remove_staging_tree(control_dir)
                    if control_dir.parent.is_dir():
                        fsync_dir(control_dir.parent)
                    raise EnrollmentError(
                        f"partial or corrupt control generation refused and reclaimed: "
                        f"{control_dir} ({exc}); rerun --enroll"
                    ) from exc
                raise EnrollmentError(
                    f"existing control generation failed manifest verification: "
                    f"{control_dir} ({exc})"
                ) from exc
        else:
            ctrl_mod.stage_control_generation(repo, control_dir, control_id=control_id)

        # Verify control generation against its manifest immediately before selecting it.
        ctrl_mod.verify_control_generation(control_dir, expected_control_id=control_id)

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

        # 6) Resolve & durably preserve original legacy scripts and desktop launcher bytes/digests
        # BEFORE replacing any shim, unit, or desktop wrapper (R2-2, F3, F4).
        originals_dir = root / "originals"
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
        shim_writes: list[tuple[Path, bytes, bytes | None, int]] = []
        for legacy_name, shim_name in (
            ("install.sh", "install-shim.sh"),
            ("provision.sh", "provision-shim.sh"),
        ):
            target = checkout / "deploy" / legacy_name
            shim_bytes = (managed_dir / shim_name).read_bytes()
            shim_sha = hashlib.sha256(shim_bytes).hexdigest()
            cur_bytes = _read_regular_bytes(target)
            cur_mode = (os.lstat(target).st_mode & 0o7777) if cur_bytes is not None else 0o755
            cur_sha = hashlib.sha256(cur_bytes or b"").hexdigest()

            if cur_bytes is not None and cur_sha != shim_sha:
                orig_sha = cur_sha
                _save_original_once(originals_dir, legacy_name, cur_bytes)
            elif cur_bytes is None:
                orig_sha = cur_sha
            else:
                # Target already carries the shim (rerun): recover original digest from preserved
                # originals/, prior enrollment.json, or enrolled git commit; never record shim_sha!
                saved_orig = _read_regular_bytes(originals_dir / legacy_name)
                saved_sha = hashlib.sha256(saved_orig).hexdigest() if saved_orig else None
                prev_entry = prior_shims.get(legacy_name)
                prev_orig = (
                    prev_entry.get("original_sha256")
                    if isinstance(prev_entry, dict)
                    else None
                )
                if saved_sha and saved_sha != shim_sha:
                    orig_sha = saved_sha
                elif isinstance(prev_orig, str) and prev_orig and prev_orig != shim_sha:
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
                shim_writes.append((target, shim_bytes, cur_bytes, cur_mode))

        reviewed_legacy_digests, _default_legacy_digest, reviewed_managed_digest = (
            _load_reviewed_launchers(repo)
        )
        managed_wrapper_bytes = (managed_dir / "sigint101-course-managed.sh").read_bytes()
        managed_wrapper_sha = hashlib.sha256(managed_wrapper_bytes).hexdigest()
        managed_wrapper_digests = {managed_wrapper_sha}
        if reviewed_managed_digest:
            managed_wrapper_digests.add(reviewed_managed_digest)

        prior_wrapper = prior_enrollment.get("desktop_wrapper")
        prior_wrapper_orig = (
            prior_wrapper.get("original_sha256")
            if isinstance(prior_wrapper, dict)
            else None
        )
        cur_wrapper_bytes = _read_regular_bytes(wrapper_file)
        cur_wrapper_sha = (
            hashlib.sha256(cur_wrapper_bytes).hexdigest()
            if cur_wrapper_bytes is not None
            else None
        )

        # F4: Preserve the verified original launcher bytes/digest BEFORE replacing the wrapper;
        # if the prior launcher was already overwritten and cannot be recovered, record "unknown"
        # with original_reviewed=False (never substitute the current checkout's launcher digest).
        wrapper_backup_path: Path | None = None
        if cur_wrapper_sha is not None and cur_wrapper_sha not in managed_wrapper_digests:
            wrapper_backup_path = _save_original_once(
                originals_dir, "desktop-wrapper.sh", cur_wrapper_bytes or b""
            )
            wrapper_orig_sha = cur_wrapper_sha
            wrapper_orig_reviewed = cur_wrapper_sha in reviewed_legacy_digests
        else:
            saved_wrapper_bytes = _read_regular_bytes(originals_dir / "desktop-wrapper.sh")
            saved_wrapper_sha = (
                hashlib.sha256(saved_wrapper_bytes).hexdigest()
                if saved_wrapper_bytes is not None
                else None
            )
            if saved_wrapper_sha and saved_wrapper_sha not in managed_wrapper_digests:
                wrapper_backup_path = originals_dir / "desktop-wrapper.sh"
                wrapper_orig_sha = saved_wrapper_sha
                wrapper_orig_reviewed = saved_wrapper_sha in reviewed_legacy_digests
            elif (
                _is_valid_hex_sha256(prior_wrapper_orig)
                and prior_wrapper_orig not in managed_wrapper_digests
            ):
                wrapper_orig_sha = str(prior_wrapper_orig)
                wrapper_orig_reviewed = wrapper_orig_sha in reviewed_legacy_digests
            else:
                wrapper_orig_sha = "unknown"
                wrapper_orig_reviewed = False

        activation_command = (
            f"sudo mv -f {staged_wrapper} {wrapper_file}"
            if wrapper_mode == "staged_privileged"
            else None
        )
        desktop_wrapper_record: dict[str, object] = {
            "path": str(wrapper_file),
            "original_sha256": wrapper_orig_sha,
            "original_reviewed": wrapper_orig_reviewed,
            "managed_sha256": managed_wrapper_sha,
            "state": (
                "staged_pending_activation"
                if wrapper_mode == "staged_privileged"
                else ("active" if wrapper_mode == "already_active" else "pending")
            ),
            "staged_path": str(staged_wrapper) if wrapper_mode == "staged_privileged" else None,
            "activation_command": activation_command,
        }
        if wrapper_backup_path is not None:
            desktop_wrapper_record["original_backup_path"] = str(wrapper_backup_path)

        prior_unit_bytes = _read_regular_bytes(unit_file)
        prior_unit_mode = (
            (os.lstat(unit_file).st_mode & 0o7777) if prior_unit_bytes is not None else 0o644
        )
        if prior_unit_bytes is not None and b"dispatcher.py" not in prior_unit_bytes:
            _save_original_once(originals_dir, "sigint101.service", prior_unit_bytes)

        enrollment_meta: dict[str, object] = {
            "status": "in_progress",
            "handoff_complete": False,
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

        # 6b) Install checkout shims with defined rollback on any late write failure (F3).
        written_shims: list[tuple[Path, bytes | None, int]] = []
        unit_replaced = False

        def _rollback_late_writes(failed_step: str, exc: BaseException) -> EnrollmentError:
            rollback_ok = True
            if unit_replaced:
                if prior_unit_bytes is not None:
                    rollback_ok &= _restore_file_best_effort(
                        unit_file, prior_unit_bytes, mode=prior_unit_mode
                    )
                else:
                    try:
                        unit_file.unlink(missing_ok=True)
                        fsync_dir(unit_file.parent)
                    except OSError:
                        rollback_ok = False
                if reload_systemd is not None:
                    try:
                        reload_systemd()
                    except Exception:
                        rollback_ok = False
            for shim_target, orig_bytes, orig_mode in reversed(written_shims):
                if orig_bytes is not None:
                    rollback_ok &= _restore_file_best_effort(
                        shim_target, orig_bytes, mode=orig_mode
                    )
                else:
                    try:
                        shim_target.unlink(missing_ok=True)
                        fsync_dir(shim_target.parent)
                    except OSError:
                        rollback_ok = False
            recovery_cmd = (
                "resolve the write error and rerun sigint101-updater --enroll"
                if rollback_ok
                else (
                    f"restore originals from {originals_dir} (or git -C {checkout} checkout -- "
                    "deploy/install.sh deploy/provision.sh), then rerun sigint101-updater --enroll"
                )
            )
            failed_meta = {
                **enrollment_meta,
                "status": "failed",
                "handoff_complete": False,
                "failed_step": failed_step,
                "rollback_ok": rollback_ok,
                "recovery_command": recovery_cmd,
            }
            try:
                _atomic_write_bytes(
                    enr_path,
                    (json.dumps(failed_meta, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                    mode=0o600,
                )
            except OSError:
                pass
            state_note = (
                "earlier enrollment writes rolled back cleanly"
                if rollback_ok
                else "automatic rollback incomplete"
            )
            return EnrollmentError(
                f"late enrollment write failed at {failed_step}: {exc} "
                f"({state_note}; recovery: {recovery_cmd})"
            )

        for target, shim_bytes, orig_bytes, orig_mode in shim_writes:
            try:
                _atomic_write_bytes(target, shim_bytes, mode=0o755)
            except OSError as exc:
                raise _rollback_late_writes(f"deploy/{target.name}", exc) from exc
            written_shims.append((target, orig_bytes, orig_mode))

        if on_boundary is not None:
            on_boundary("before_unit_install")

        # 7) Re-verify control generation, then atomically install the managed unit and reload.
        ctrl_mod.verify_control_generation(control_dir, expected_control_id=control_id)
        unit_text = _render_managed_unit(
            template_path=managed_dir / "sigint101.service",
            host_python=host_python,
            dispatcher_path=dispatcher_dest,
            updater_root=root,
        )
        try:
            _atomic_write_bytes(unit_file, unit_text.encode("utf-8"), mode=0o644)
            unit_replaced = True
            fsync_file(unit_file)
            if reload_systemd is not None:
                reload_systemd()
        except OSError as exc:
            raise _rollback_late_writes("unit_install", exc) from exc

        if on_boundary is not None:
            on_boundary("after_unit_install_before_drain")

        if drain_old_service is not None:
            drain_old_service()

        # 8) F3: Publish durable completed hand-over record BEFORE enabling the managed wrapper.
        final_wrapper_state = (
            "staged_pending_activation" if wrapper_mode == "staged_privileged" else "active"
        )
        completed_wrapper_record = {
            **desktop_wrapper_record,
            "state": final_wrapper_state,
        }
        completed_meta = {
            **enrollment_meta,
            "status": "complete",
            "handoff_complete": True,
            "desktop_wrapper": completed_wrapper_record,
        }
        _atomic_write_bytes(
            enr_path,
            (json.dumps(completed_meta, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )

        if on_boundary is not None:
            on_boundary("before_wrapper_activation")

        # 9) F2: Activate the managed desktop wrapper ONLY AFTER enrollment is complete and usable.
        if wrapper_mode == "direct":
            try:
                _atomic_write_bytes(wrapper_file, managed_wrapper_bytes, mode=0o755)
            except OSError as exc:
                managed_src = managed_dir / "sigint101-course-managed.sh"
                raise EnrollmentError(
                    f"enrollment completed but activating desktop wrapper {wrapper_file} failed: "
                    f"{exc}; recovery: install -m 0755 {managed_src} "
                    f"{wrapper_file} or rerun --enroll"
                ) from exc
        elif wrapper_mode == "staged_privileged":
            if not _file_matches_managed_wrapper(staged_wrapper, managed_wrapper_bytes):
                raise EnrollmentError(
                    f"staged desktop wrapper drifted during enrollment: {staged_wrapper}"
                )
        elif not _file_matches_managed_wrapper(wrapper_file, managed_wrapper_bytes):
            raise EnrollmentError(f"desktop wrapper drifted during enrollment: {wrapper_file}")

    result: dict[str, object] = {
        "mode": "legacy",
        "operation_id": operation_id,
        "control_id": control_id,
        "runtime_id": runtime_id,
        "skipped_pth_files": skipped_pth_files,
        "dispatcher": str(dispatcher_dest),
        "unit_path": str(unit_file),
        "handoff_complete": True,
        "wrapper_state": final_wrapper_state,
    }
    if activation_command is not None:
        result["wrapper_activation_command"] = activation_command
    return result
