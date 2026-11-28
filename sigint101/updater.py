# ABOUTME: Manual course updater orchestration: apply, selector switch, serving-fence inspection,
# ABOUTME: plain recovery, explicit unserved abort, and read-only preview with crash boundaries.

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .bench_gate import require_bench_marker
from .locks import (
    EXIT_GATE_BUSY,
    LockBusyError,
    acquire_mutation_gate,
    acquire_shared_gate,
    acquire_writer_lock,
)
from .process_checks import (
    SystemdUserController,
    require_kiosk_page_closed,
    start_unit_with_reset,
    wait_service_drained,
)
from .release_prep import (
    PolicyRefusalError,
    check_space_budget,
    estimate_apply_budget_bytes,
    fetch_and_export_release,
    validate_remote_url,
    verify_candidate_contracts,
)
from .runtime import (
    probe_python_abi,
    reclaim_abandoned_runtime_staging,
    verify_runtime_compatibility,
)
from .snapshot import copy_generation_snapshot
from .storage import fsync_dir, fsync_file, validate_updater_root
from .trial import run_private_trial
from .unit_settings import (
    UnitSettings,
    compute_settings_fingerprint,
    load_systemd_unit_settings,
)

SELECTOR_FORMAT_VERSION = 1
SERVED_MARKER_FILENAME = "served.json"
INTENT_FILENAME = "intent.json"
PREPARED_FILENAME = "prepared.json"


class UpdaterError(RuntimeError):
    """Base error for updater apply, recovery, or abort failures."""


class AbortError(UpdaterError):
    """Raised when an explicit unserved abort is refused or cannot safely restore."""


class RecoveryError(UpdaterError):
    """Raised when plain recovery refuses due to an invalid selector or unrelated listener."""


class ServiceAdapter(Protocol):
    """Stateful service control interface satisfied by SystemdUserController or test fixtures."""

    def stop(self, timeout_s: float = 15.0) -> None: ...
    def unit_state(self) -> tuple[str, str, int]: ...
    def get_cgroup_pids(self) -> list[int]: ...
    def is_listener_active(self) -> bool: ...
    def kill_cgroup(self) -> None: ...
    def reset_failed(self) -> None: ...
    def start(self) -> None: ...
    def is_http_ready(self) -> bool: ...


def _fire(on_boundary: Callable[[str], None] | None, name: str) -> None:
    if on_boundary is not None:
        on_boundary(name)


def _ensure_synced_dir(path: Path, *, mode: int = 0o700) -> None:
    """Create `path` if missing and fsync its parent directory so the new entry is durable (F11)."""
    if path.exists():
        return
    if not path.parent.exists():
        _ensure_synced_dir(path.parent, mode=mode)
    try:
        os.mkdir(path, mode)
    except FileExistsError:
        return
    fsync_dir(path.parent)


def _open_trusted_dir(path: Path, *, label: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.fspath(path), flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or (st.st_mode & 0o022):
            raise UpdaterError(f"untrusted directory for {label}: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_json_durable(
    path: Path,
    payload: dict,
    *,
    exclusive: bool = True,
    mode: int = 0o600,
    on_tmp_fsync: Callable[[], None] | None = None,
    on_replace: Callable[[], None] | None = None,
) -> None:
    """Write canonical JSON with file fsync, atomic replacement (if not exclusive), and dir sync."""
    _ensure_synced_dir(path.parent, mode=0o700)
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(os.fspath(path), flags, mode)
        try:
            view = memoryview(raw)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        fsync_dir(path.parent)
        return

    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{time.monotonic_ns()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.fspath(tmp), flags, mode)
    try:
        view = memoryview(raw)
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
    if on_tmp_fsync is not None:
        on_tmp_fsync()
    os.replace(tmp, path)
    if on_replace is not None:
        on_replace()
    fsync_dir(path.parent)


def _read_selector(root: Path) -> dict:
    sel_path = root / "current.json"
    if not sel_path.is_file() or sel_path.is_symlink():
        raise UpdaterError("current.json is missing or symlinked")
    try:
        data = json.loads(sel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdaterError(f"current.json is unreadable or malformed: {exc}") from exc
    if not isinstance(data, dict) or data.get("format_version") != SELECTOR_FORMAT_VERSION:
        raise UpdaterError("unsupported current.json format_version")
    if data.get("mode") not in ("legacy", "managed"):
        raise UpdaterError(f"unsupported current.json mode: {data.get('mode')!r}")
    op_id = data.get("operation_id")
    if not isinstance(op_id, str) or not op_id or "/" in op_id:
        raise UpdaterError(f"invalid operation_id in current.json: {op_id!r}")
    return data


def _read_control_dir(root: Path) -> tuple[str, Path, dict]:
    ctrl_file = root / "control.json"
    if not ctrl_file.is_file() or ctrl_file.is_symlink():
        raise UpdaterError("control.json is missing or symlinked")
    data = json.loads(ctrl_file.read_text(encoding="utf-8"))
    ctrl_id = data.get("control_id")
    if not isinstance(ctrl_id, str) or not ctrl_id or "/" in ctrl_id:
        raise UpdaterError(f"invalid control_id: {ctrl_id!r}")
    ctrl_dir = root / "control" / ctrl_id
    manifest_file = ctrl_dir / "control_manifest.json"
    if not manifest_file.is_file() or manifest_file.is_symlink():
        raise UpdaterError(f"control manifest missing in {ctrl_dir}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    return ctrl_id, ctrl_dir, manifest


def _read_enrollment_meta(root: Path) -> dict:
    enr_path = root / "enrollment.json"
    if not enr_path.is_file() or enr_path.is_symlink():
        raise UpdaterError("enrollment.json is missing or symlinked")
    return json.loads(enr_path.read_text(encoding="utf-8"))


def _new_unique_id(prefix: str, parent_dir: Path) -> str:
    _ensure_synced_dir(parent_dir, mode=0o700)
    for seq in range(1, 10000):
        cand = f"{prefix}-{time.time_ns()}-{seq:04d}"
        if not (parent_dir / cand).exists():
            return cand
    raise UpdaterError(f"could not allocate unique ID under {parent_dir}")


def inspect_serving_marker(
    root: str | Path,
    operation_id: str,
    release_id: str | None,
    generation_id: str | None,
) -> str:
    """Inspect operations/<op>/served.json via trusted dir_fd: 'absent', 'served', or 'uncertain'.

    Only a clean FileNotFoundError proves 'absent'. Any symlink, bad owner/mode, unreadable file,
    malformed JSON, or mismatched operation/release/generation identity returns 'uncertain' so
    explicit abort fails closed.
    """
    resolved_root = Path(root).resolve()
    try:
        root_fd = _open_trusted_dir(resolved_root, label="updater root")
    except (OSError, UpdaterError):
        return "uncertain"

    try:
        ops_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            ops_parent_fd = os.open("operations", ops_flags, dir_fd=root_fd)
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "uncertain"

        try:
            try:
                op_fd = os.open(operation_id, ops_flags, dir_fd=ops_parent_fd)
            except FileNotFoundError:
                return "absent"
            except OSError:
                return "uncertain"

            try:
                st_dir = os.fstat(op_fd)
                if st_dir.st_uid != os.geteuid() or (st_dir.st_mode & 0o022):
                    return "uncertain"

                file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                try:
                    fd = os.open(SERVED_MARKER_FILENAME, file_flags, dir_fd=op_fd)
                except FileNotFoundError:
                    return "absent"
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        return "uncertain"
                    return "uncertain"

                try:
                    st = os.fstat(fd)
                    if (
                        not stat.S_ISREG(st.st_mode)
                        or st.st_uid != os.geteuid()
                        or (st.st_mode & 0o022)
                        or st.st_nlink > 1
                    ):
                        return "uncertain"
                    raw = os.read(fd, 16384)
                finally:
                    os.close(fd)
            finally:
                os.close(op_fd)
        finally:
            os.close(ops_parent_fd)
    finally:
        os.close(root_fd)

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "uncertain"
    if not isinstance(data, dict) or data.get("format_version") != 1:
        return "uncertain"
    if (
        data.get("operation_id") != operation_id
        or data.get("release_id") != release_id
        or data.get("generation_id") != generation_id
    ):
        return "uncertain"
    return "served"


def _wait_live_ready(
    root: Path,
    selector: dict,
    service: ServiceAdapter,
    *,
    timeout_s: float = 15.0,
    poll_s: float = 0.05,
) -> bool:
    """Wait for the exact selected operation/pair to become active, fenced (if managed), and 200."""
    deadline = time.monotonic() + timeout_s
    op_id = selector["operation_id"]
    mode = selector["mode"]
    rel_id = selector.get("release_id")
    gen_id = selector.get("generation_id")

    while True:
        try:
            current = _read_selector(root)
        except UpdaterError:
            return False
        if current.get("operation_id") != op_id:
            return False

        active_state, _sub_state, main_pid = service.unit_state()
        if active_state == "active" and main_pid > 0 and service.is_http_ready():
            if mode == "legacy":
                return True
            if inspect_serving_marker(root, op_id, rel_id, gen_id) == "served":
                return True

        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def _list_incomplete_intents(root: Path, active_op_id: str) -> list[str]:
    ops_dir = root / "operations"
    if not ops_dir.is_dir():
        return []
    incomplete: list[str] = []
    for entry in sorted(ops_dir.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name == active_op_id:
            continue
        if (entry / INTENT_FILENAME).is_file() and not (entry / SERVED_MARKER_FILENAME).is_file():
            incomplete.append(entry.name)
    return incomplete


def _resolve_managed_host_python(enr: dict, old_selector: dict) -> str:
    """Resolve enrolled host Python for managed trial & service, never legacy .venv (F2)."""
    host_py = str(enr.get("host_python") or "")
    if not host_py:
        if old_selector["mode"] == "managed" and old_selector.get("python_executable"):
            host_py = str(old_selector["python_executable"])
        else:
            raise PolicyRefusalError("enrollment metadata is missing host_python")
    legacy_checkout = enr.get("legacy_checkout") or old_selector.get("checkout_dir")
    if legacy_checkout:
        try:
            if Path(host_py).resolve().is_relative_to(Path(legacy_checkout).resolve()):
                raise PolicyRefusalError(
                    f"host_python {host_py!r} must not live inside legacy checkout"
                )
        except OSError:
            pass
    return host_py


def apply_update(
    *,
    root: str | Path,
    remote_url: str,
    pinned_commit: str,
    service: ServiceAdapter,
    ref: str = "refs/heads/main",
    unit_name: str = "sigint101.service",
    unit_settings: UnitSettings | None = None,
    unit_settings_loader: Callable[[str], UnitSettings] | None = None,
    home: str | Path | None = None,
    kiosk_process_lister=None,
    statvfs_fn: Callable[[str | Path], object] = os.statvfs,
    ready_timeout_s: float = 15.0,
    on_boundary: Callable[[str], None] | None = None,
    inherited_gate_fd: int | None = None,
    bench_marker_checker: Callable[[Path], None] = require_bench_marker,
) -> dict:
    """Execute a full manual course apply or first legacy->managed conversion (§5).

    Enforces PR 4's root-owned bench marker BEFORE touching any directory, lock, file, or service.
    """
    resolved_root = validate_updater_root(root, home=home)
    # PR 4 bench gate FIRST — before locks, directories, files, service calls, or candidate code!
    bench_marker_checker(resolved_root)

    lister_kw = {"process_lister": kiosk_process_lister} if kiosk_process_lister is not None else {}
    require_kiosk_page_closed(home=home, **lister_kw)

    restart_old_after_unlock = False
    precommit_exc: BaseException | None = None
    old_selector_for_restart: dict | None = None

    with acquire_mutation_gate(
        resolved_root, blocking=False, inherited_gate_fd=inherited_gate_fd, home=home
    ):
        # Recheck kiosk page closed under the exclusive mutation gate immediately before work.
        require_kiosk_page_closed(home=home, **lister_kw)

        old_selector = _read_selector(resolved_root)
        enr = _read_enrollment_meta(resolved_root)
        _ctrl_id, ctrl_dir, baseline_ctrl_manifest = _read_control_dir(resolved_root)

        trusted_remote = str(enr.get("trusted_remote") or "")
        if not trusted_remote:
            raise PolicyRefusalError("enrollment metadata is missing trusted_remote")
        baseline_req_sha = str(enr.get("requirements_sha256") or "")
        if not baseline_req_sha:
            raise PolicyRefusalError("enrollment metadata is missing requirements_sha256")

        # N4: validate remote policy BEFORE allocating operation IDs or writing intent.json!
        validate_remote_url(remote_url, trusted_remote=trusted_remote)

        current_commit = (
            old_selector.get("commit")
            if old_selector["mode"] == "managed"
            else enr.get("legacy_commit")
        )
        # Same-commit no-op makes zero staging or service calls (§5.1).
        if old_selector["mode"] == "managed" and current_commit == pinned_commit:
            return {
                "action": "apply",
                "status": "noop",
                "commit": pinned_commit,
                "operation_id": old_selector["operation_id"],
                "release_id": old_selector.get("release_id"),
                "generation_id": old_selector.get("generation_id"),
            }

        source_data_dir = (
            resolved_root / "data" / old_selector["generation_id"]
            if old_selector["mode"] == "managed"
            else Path(old_selector["data_dir"]).resolve()
        )
        runtime_id = str(old_selector["runtime_id"])
        runtime_dir = resolved_root / "runtimes" / runtime_id
        # F2: managed trial and service ALWAYS run on enrolled host_python, never legacy .venv.
        host_python = _resolve_managed_host_python(enr, old_selector)

        # F1: load effective unit settings automatically on real systemd hosts or when loader given.
        effective_unit_settings = unit_settings
        if effective_unit_settings is None:
            if unit_settings_loader is not None:
                effective_unit_settings = unit_settings_loader(unit_name)
            elif isinstance(service, SystemdUserController):
                effective_unit_settings = load_systemd_unit_settings(service.unit)

        # Validate sealed runtime ABI before staging/stopping.
        verify_runtime_compatibility(
            runtime_dir, current_abi=probe_python_abi(host_python), verify_files=True
        )

        # F5: reclaim abandoned runtime staging siblings under the exclusive mutation gate
        # before space-budget checks so interrupted attempts cannot starve retries.
        reclaim_abandoned_runtime_staging(
            resolved_root / "runtimes",
            protected_names={runtime_id},
        )

        # Conservative initial space budget check BEFORE staging or stopping service (§1, §5.1).
        initial_budget = estimate_apply_budget_bytes(source_data_dir=source_data_dir)
        check_space_budget(resolved_root, required_bytes=initial_budget, statvfs_fn=statvfs_fn)

        # Allocate exclusive operation, release, generation, and staging IDs (F11 synced dirs).
        action = "convert" if old_selector["mode"] == "legacy" else "apply"
        op_id = _new_unique_id("op", resolved_root / "operations")
        release_id = _new_unique_id("rel", resolved_root / "releases")
        generation_id = _new_unique_id("gen", resolved_root / "data")
        _ensure_synced_dir(resolved_root / "staging", mode=0o700)
        staging_dir = resolved_root / "staging" / op_id
        ops_dir = resolved_root / "operations" / op_id
        dest_release_dir = resolved_root / "releases" / release_id
        dest_gen_dir = resolved_root / "data" / generation_id

        # 1) Create & fsync operations/<op> entry in operations/, then write durable intent (§5.2).
        _ensure_synced_dir(ops_dir, mode=0o700)
        _fire(on_boundary, "before_intent_sync")
        intent_payload = {
            "format_version": 1,
            "operation_id": op_id,
            "kind": action,
            "old_selector": old_selector,
            "target_commit": pinned_commit,
            "release_id": release_id,
            "generation_id": generation_id,
            "runtime_id": runtime_id,
        }
        _write_json_durable(ops_dir / INTENT_FILENAME, intent_payload, exclusive=True)
        _fire(on_boundary, "after_intent_sync")

        # 2) Private Git fetch, pin/ancestry/tree checks, and sealed release export.
        fetch_and_export_release(
            remote_url=remote_url,
            trusted_remote=trusted_remote,
            pinned_commit=pinned_commit,
            current_commit=current_commit,
            ref=ref,
            dest_release_dir=dest_release_dir,
            scratch_git_dir=staging_dir / "git",
            release_id=release_id,
        )
        verify_candidate_contracts(
            control_dir=ctrl_dir,
            baseline_control_manifest=baseline_ctrl_manifest,
            baseline_requirements_sha256=baseline_req_sha,
            candidate_release_dir=dest_release_dir,
        )
        _fire(on_boundary, "after_release_export")

        # 3) Stop and verify service + cgroup + listener drained before snapshotting.
        require_kiosk_page_closed(home=home, **lister_kw)
        wait_service_drained(
            stop_unit=service.stop,
            get_unit_state=service.unit_state,
            get_cgroup_pids=service.get_cgroup_pids,
            is_listener_active=service.is_listener_active,
            kill_cgroup=service.kill_cgroup,
        )
        _fire(on_boundary, "after_service_stop")

        service_stopped_precommit = True
        try:
            with acquire_writer_lock(resolved_root, blocking=False, home=home):
                if _read_selector(resolved_root) != old_selector:
                    raise UpdaterError("selector changed before snapshot/publication")

                # Recompute conservative space budget after stopping (recordings may have grown!).
                post_stop_budget = estimate_apply_budget_bytes(source_data_dir=source_data_dir)
                check_space_budget(
                    resolved_root, required_bytes=post_stop_budget, statvfs_fn=statvfs_fn
                )

                # 4) Copy & migrate complete quiesced generation via external scratch.
                copy_generation_snapshot(
                    source_data_dir,
                    dest_gen_dir,
                    generation_id=generation_id,
                    scratch_dir=staging_dir / "db_scratch",
                    allow_missing_stores=(old_selector["mode"] == "legacy"),
                )
                _fire(on_boundary, "after_snapshot_copy")

                # 5) Run private read-only trial under enrolled sealed runtime (-B -I -S, F9).
                trial_result = run_private_trial(
                    python_executable=host_python,
                    release_root=dest_release_dir,
                    data_dir=dest_gen_dir,
                    runtime_dir=runtime_dir,
                    unit_settings=effective_unit_settings,
                    expected_release_id=release_id,
                    expected_commit=pinned_commit,
                    expected_generation_id=generation_id,
                    socket_parent_dir=staging_dir,
                )
                if not trial_result.ready:
                    raise UpdaterError(
                        "private read-only trial failed: " + "; ".join(trial_result.errors)
                    )
                _fire(on_boundary, "after_trial")

                settings_fp = (
                    compute_settings_fingerprint(effective_unit_settings.unit_only())
                    if effective_unit_settings is not None
                    else None
                )
                predecessor = (
                    {
                        "release_id": old_selector["release_id"],
                        "commit": old_selector["commit"],
                        "operation_id": old_selector["operation_id"],
                    }
                    if old_selector["mode"] == "managed"
                    else None
                )
                new_selector = {
                    "format_version": SELECTOR_FORMAT_VERSION,
                    "mode": "managed",
                    "operation_id": op_id,
                    "action": action,
                    "release_id": release_id,
                    "commit": pinned_commit,
                    "generation_id": generation_id,
                    "runtime_id": runtime_id,
                    "settings_fingerprint": settings_fp,
                    "predecessor": predecessor,
                    "host": str(old_selector.get("host", "127.0.0.1")),
                    "port": int(old_selector.get("port", 8008)),
                    "python_executable": host_python,
                }

                # 6) Write & fsync immutable prepared.json BEFORE selector replacement (§5.2-5.3).
                _fire(on_boundary, "before_prepared_sync")
                prepared_payload = {
                    "format_version": 1,
                    "operation_id": op_id,
                    "kind": action,
                    "old_selector": old_selector,
                    "new_selector": new_selector,
                    "settings_fingerprint": settings_fp,
                    "trial_attestation": trial_result.attestation,
                }
                _write_json_durable(ops_dir / PREPARED_FILENAME, prepared_payload, exclusive=True)
                _fire(on_boundary, "after_prepared_sync")

                if _read_selector(resolved_root) != old_selector:
                    raise UpdaterError("selector drifted immediately before commit")

                # 7) Single atomic selector commit: write temp, fsync temp, replace, fsync ROOT.
                def _on_apply_replace() -> None:
                    nonlocal service_stopped_precommit
                    service_stopped_precommit = False
                    _fire(on_boundary, "after_selector_replace_before_parent_sync")

                _write_json_durable(
                    resolved_root / "current.json",
                    new_selector,
                    exclusive=False,
                    on_tmp_fsync=lambda: _fire(
                        on_boundary, "after_selector_tmp_fsync_before_replace"
                    ),
                    on_replace=_on_apply_replace,
                )
                service_stopped_precommit = False
                _fire(on_boundary, "after_selector_parent_sync")
        except BaseException as exc:
            # F3: defer restarting the original pair until AFTER BOTH locks are released!
            if service_stopped_precommit:
                try:
                    if _read_selector(resolved_root) == old_selector:
                        restart_old_after_unlock = True
                        old_selector_for_restart = old_selector
                        precommit_exc = exc
                except Exception:  # noqa: BLE001
                    pass
            if not restart_old_after_unlock:
                raise

        if not restart_old_after_unlock:
            _fire(on_boundary, "before_unlock_and_start")

    # F3: both locks are now released! If pre-commit refused post-stop, restart original & verify.
    if (
        restart_old_after_unlock
        and precommit_exc is not None
        and old_selector_for_restart is not None
    ):
        restarted_ready = False
        try:
            start_unit_with_reset(reset_failed=service.reset_failed, start_unit=service.start)
            restarted_ready = _wait_live_ready(
                resolved_root, old_selector_for_restart, service, timeout_s=ready_timeout_s
            )
        except Exception as restart_exc:  # noqa: BLE001
            raise UpdaterError(
                f"{precommit_exc} (pre-commit restart of original pair failed: {restart_exc}; "
                "run sigint101-updater --recover)"
            ) from precommit_exc
        if not restarted_ready:
            raise UpdaterError(
                f"{precommit_exc} (original pair restarted after refusal but is not ready; "
                "run sigint101-updater --recover)"
            ) from precommit_exc
        raise precommit_exc

    # 8) Locks are now released! Reset failed state, start unit, and await live readiness (§5.4).
    start_unit_with_reset(reset_failed=service.reset_failed, start_unit=service.start)
    _fire(on_boundary, "after_service_start_before_ready")

    ready = _wait_live_ready(resolved_root, new_selector, service, timeout_s=ready_timeout_s)
    if ready:
        return {
            "action": action,
            "status": "ready",
            "commit": pinned_commit,
            "short_commit": pinned_commit[:7],
            "operation_id": op_id,
            "release_id": release_id,
            "generation_id": generation_id,
            "rollback_target": predecessor["commit"][:7] if predecessor else None,
        }

    # Lost acknowledgement / failed start: NEVER auto-revert! Check fence for abort eligibility.
    marker_state = inspect_serving_marker(resolved_root, op_id, release_id, generation_id)
    recovery_cmd = (
        f"sigint101-updater --recover --abort {op_id}"
        if marker_state == "absent"
        else "sigint101-updater --recover"
    )
    return {
        "action": action,
        "status": "committed, console not ready",
        "commit": pinned_commit,
        "short_commit": pinned_commit[:7],
        "operation_id": op_id,
        "release_id": release_id,
        "generation_id": generation_id,
        "abort_eligible": marker_state == "absent",
        "recovery_command": recovery_cmd,
    }


def _validate_prior_selector_for_abort(root: Path, old_selector: dict) -> None:
    mode = old_selector.get("mode")
    if mode == "legacy":
        checkout = Path(old_selector["checkout_dir"]).resolve()
        data_dir = Path(old_selector["data_dir"]).resolve()
        if not checkout.is_dir():
            raise AbortError(f"recorded legacy checkout missing: {checkout}")
        if not data_dir.is_dir():
            raise AbortError(f"recorded legacy data directory missing: {data_dir}")
        return
    if mode == "managed":
        rel_dir = root / "releases" / old_selector["release_id"]
        gen_dir = root / "data" / old_selector["generation_id"]
        rt_dir = root / "runtimes" / old_selector["runtime_id"]
        for label, target in (("release", rel_dir), ("generation", gen_dir), ("runtime", rt_dir)):
            if not target.is_dir() or target.is_symlink():
                raise AbortError(f"recorded prior {label} missing or symlinked: {target}")
        python_exe = str(old_selector.get("python_executable") or sys.executable)
        verify_runtime_compatibility(
            rt_dir, current_abi=probe_python_abi(python_exe), verify_files=True
        )
        return
    raise AbortError(f"unsupported prior selector mode: {mode!r}")


def _is_selector_live_now(root: Path, selector: dict, service: ServiceAdapter) -> bool:
    active_state, _sub_state, main_pid = service.unit_state()
    if not (active_state == "active" and main_pid > 0 and service.is_http_ready()):
        return False
    if selector["mode"] == "legacy":
        return True
    return (
        inspect_serving_marker(
            root,
            selector["operation_id"],
            selector.get("release_id"),
            selector.get("generation_id"),
        )
        == "served"
    )


def abort_unserved(
    *,
    root: str | Path,
    operation_id: str,
    service: ServiceAdapter,
    home: str | Path | None = None,
    kiosk_process_lister=None,
    ready_timeout_s: float = 15.0,
    on_boundary: Callable[[str], None] | None = None,
    inherited_gate_fd: int | None = None,
    bench_marker_checker: Callable[[Path], None] = require_bench_marker,
) -> dict:
    """Abort a proven-unserved post-commit operation back to its exact prior pair (§5)."""
    resolved_root = validate_updater_root(root, home=home)
    bench_marker_checker(resolved_root)

    # 1) Read-only pre-check BEFORE stopping any running service (§5).
    current = _read_selector(resolved_root)

    # F5: idempotent retry of an already-committed abort returns immediately if live, or completes
    # fencing + start if a prior abort crashed after selector replacement before start!
    if current.get("action") == "abort" and current.get("aborted_operation_id") == operation_id:
        if _is_selector_live_now(resolved_root, current, service):
            if inherited_gate_fd is not None:
                os.close(inherited_gate_fd)
            return {
                "action": "abort",
                "status": "ready",
                "mode": current["mode"],
                "operation_id": current["operation_id"],
                "aborted_operation_id": operation_id,
                "release_id": current.get("release_id"),
                "generation_id": current.get("generation_id"),
            }
        with acquire_mutation_gate(
            resolved_root, blocking=False, inherited_gate_fd=inherited_gate_fd, home=home
        ):
            sel_path = resolved_root / "current.json"
            cur_locked = _read_selector(resolved_root)
            wait_service_drained(
                stop_unit=service.stop,
                get_unit_state=service.unit_state,
                get_cgroup_pids=service.get_cgroup_pids,
                is_listener_active=service.is_listener_active,
                kill_cgroup=service.kill_cgroup,
            )
            with acquire_writer_lock(resolved_root, blocking=False, home=home):
                fsync_file(sel_path)
                fsync_dir(resolved_root)
        start_unit_with_reset(reset_failed=service.reset_failed, start_unit=service.start)
        ready = _wait_live_ready(resolved_root, cur_locked, service, timeout_s=ready_timeout_s)
        out = {
            "action": "abort",
            "status": "ready" if ready else "committed, console not ready",
            "mode": cur_locked["mode"],
            "operation_id": cur_locked["operation_id"],
            "aborted_operation_id": operation_id,
            "release_id": cur_locked.get("release_id"),
            "generation_id": cur_locked.get("generation_id"),
        }
        if not ready:
            out["recovery_command"] = "sigint101-updater --recover"
        return out

    if current.get("operation_id") != operation_id:
        raise AbortError(
            f"operation {operation_id!r} does not match currently selected operation "
            f"{current.get('operation_id')!r}"
        )

    prepared_path = resolved_root / "operations" / operation_id / PREPARED_FILENAME
    if not prepared_path.is_file() or prepared_path.is_symlink():
        raise AbortError(f"durable prepared record missing for operation {operation_id!r}")
    try:
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AbortError(f"prepared record unreadable for {operation_id!r}: {exc}") from exc
    if not isinstance(prepared, dict) or prepared.get("operation_id") != operation_id:
        raise AbortError(f"prepared record does not match operation {operation_id!r}")
    if prepared.get("new_selector") != current:
        raise AbortError(
            f"prepared record new_selector does not match current.json for {operation_id!r}"
        )

    marker_state = inspect_serving_marker(
        resolved_root,
        operation_id,
        current.get("release_id"),
        current.get("generation_id"),
    )
    if marker_state == "served":
        raise AbortError(
            f"operation {operation_id!r} already reached the serving fence; "
            "abort refused (use plain --recover or compatible rollback)"
        )
    if marker_state != "absent":
        raise AbortError(
            f"serving marker state for {operation_id!r} is uncertain/malformed; abort refused"
        )

    old_selector = prepared.get("old_selector")
    if not isinstance(old_selector, dict):
        raise AbortError(f"prepared record for {operation_id!r} has no valid prior selector")
    _validate_prior_selector_for_abort(resolved_root, old_selector)

    lister_kw = {"process_lister": kiosk_process_lister} if kiosk_process_lister is not None else {}
    require_kiosk_page_closed(home=home, **lister_kw)

    # 2) Take mutation gate, re-check kiosk page, drain service, take writer lock, RE-VERIFY marker!
    restart_needed = False
    saved_exc: BaseException | None = None
    selector_for_restart: dict | None = None

    with acquire_mutation_gate(
        resolved_root, blocking=False, inherited_gate_fd=inherited_gate_fd, home=home
    ):
        require_kiosk_page_closed(home=home, **lister_kw)
        wait_service_drained(
            stop_unit=service.stop,
            get_unit_state=service.unit_state,
            get_cgroup_pids=service.get_cgroup_pids,
            is_listener_active=service.is_listener_active,
            kill_cgroup=service.kill_cgroup,
        )
        service_stopped_precommit = True
        try:
            with acquire_writer_lock(resolved_root, blocking=False, home=home):
                cur_after = _read_selector(resolved_root)
                if cur_after.get("operation_id") != operation_id:
                    raise AbortError("selected operation changed during drain; abort refused")

                marker_after = inspect_serving_marker(
                    resolved_root,
                    operation_id,
                    cur_after.get("release_id"),
                    cur_after.get("generation_id"),
                )
                if marker_after != "absent":
                    raise AbortError(
                        f"operation {operation_id!r} became {marker_after} before drain completed; "
                        "abort refused and selected pair preserved"
                    )

                _validate_prior_selector_for_abort(resolved_root, old_selector)

                abort_op_id = _new_unique_id("abort", resolved_root / "operations")
                abort_ops_dir = resolved_root / "operations" / abort_op_id
                _ensure_synced_dir(abort_ops_dir, mode=0o700)

                restored_selector = dict(old_selector)
                restored_selector["operation_id"] = abort_op_id
                restored_selector["action"] = "abort"
                restored_selector["aborted_operation_id"] = operation_id

                _write_json_durable(
                    abort_ops_dir / INTENT_FILENAME,
                    {
                        "format_version": 1,
                        "operation_id": abort_op_id,
                        "kind": "abort",
                        "aborted_operation_id": operation_id,
                        "restored_selector": restored_selector,
                    },
                    exclusive=True,
                )

                def _on_abort_replace() -> None:
                    nonlocal service_stopped_precommit
                    service_stopped_precommit = False
                    _fire(on_boundary, "after_abort_selector_replace_before_parent_sync")

                _write_json_durable(
                    resolved_root / "current.json",
                    restored_selector,
                    exclusive=False,
                    on_tmp_fsync=lambda: _fire(on_boundary, "after_abort_selector_tmp_fsync"),
                    on_replace=_on_abort_replace,
                )
                service_stopped_precommit = False
                _fire(on_boundary, "after_abort_selector_parent_sync")
        except BaseException as exc:
            # F4: ANY post-stop pre-commit refusal restarts the preserved pair AFTER unlocking!
            if service_stopped_precommit:
                restart_needed = True
                saved_exc = exc
                try:
                    selector_for_restart = _read_selector(resolved_root)
                except Exception:  # noqa: BLE001
                    selector_for_restart = current
            else:
                raise

    if restart_needed and saved_exc is not None:
        restarted_ready = False
        try:
            start_unit_with_reset(reset_failed=service.reset_failed, start_unit=service.start)
            if selector_for_restart is not None:
                restarted_ready = _wait_live_ready(
                    resolved_root, selector_for_restart, service, timeout_s=ready_timeout_s
                )
        except Exception as restart_exc:  # noqa: BLE001
            raise AbortError(
                f"{saved_exc} (restart of selected pair after refusal failed: {restart_exc}; "
                "console not ready; run sigint101-updater --recover)"
            ) from saved_exc
        if not restarted_ready:
            raise AbortError(
                f"{saved_exc} (selected pair restarted after refusal but console not ready; "
                "run sigint101-updater --recover)"
            ) from saved_exc
        raise saved_exc

    # 3) Locks released! Reset failed state, start restored prior pair, and await readiness.
    start_unit_with_reset(reset_failed=service.reset_failed, start_unit=service.start)
    ready = _wait_live_ready(resolved_root, restored_selector, service, timeout_s=ready_timeout_s)
    out = {
        "action": "abort",
        "status": "ready" if ready else "committed, console not ready",
        "mode": restored_selector["mode"],
        "operation_id": abort_op_id,
        "aborted_operation_id": operation_id,
        "release_id": restored_selector.get("release_id"),
        "generation_id": restored_selector.get("generation_id"),
    }
    if not ready:
        out["recovery_command"] = "sigint101-updater --recover"
    return out


def recover(
    *,
    root: str | Path,
    service: ServiceAdapter,
    home: str | Path | None = None,
    ready_timeout_s: float = 15.0,
    inherited_gate_fd: int | None = None,
    bench_marker_checker: Callable[[Path], None] = require_bench_marker,
) -> dict:
    """Validate and fence the selected pair, report incomplete intents, and ensure live."""
    resolved_root = validate_updater_root(root, home=home)
    bench_marker_checker(resolved_root)

    need_restart = False
    with acquire_mutation_gate(
        resolved_root, blocking=False, inherited_gate_fd=inherited_gate_fd, home=home
    ):
        sel_path = resolved_root / "current.json"
        selector = _read_selector(resolved_root)
        # Complete selector durability fence under the gate (§5).
        fsync_file(sel_path)
        fsync_dir(resolved_root)

        if selector["mode"] == "legacy":
            from .enrollment import ENROLLMENT_RECOVERY_FILENAME, recover_interrupted_enrollment

            if (resolved_root / ENROLLMENT_RECOVERY_FILENAME).exists():
                recover_interrupted_enrollment(
                    resolved_root,
                    reload_systemd=getattr(service, "daemon_reload", None),
                )

        op_id = selector["operation_id"]
        incomplete = _list_incomplete_intents(resolved_root, active_op_id=op_id)
        active_state, _sub_state, main_pid = service.unit_state()
        listener_up = service.is_listener_active()

        # Already-live exact pair is acknowledged without disturbing it.
        if active_state == "active" and main_pid > 0 and service.is_http_ready():
            if selector["mode"] == "legacy":
                return {
                    "action": "recover",
                    "status": "ready",
                    "mode": "legacy",
                    "operation_id": op_id,
                    "incomplete_intents": incomplete,
                }
            marker_state = inspect_serving_marker(
                resolved_root, op_id, selector.get("release_id"), selector.get("generation_id")
            )
            if marker_state == "served":
                mpath = resolved_root / "operations" / op_id / SERVED_MARKER_FILENAME
                fsync_file(mpath)
                fsync_dir(mpath.parent)
                return {
                    "action": "recover",
                    "status": "ready",
                    "mode": "managed",
                    "operation_id": op_id,
                    "release_id": selector.get("release_id"),
                    "generation_id": selector.get("generation_id"),
                    "incomplete_intents": incomplete,
                }

        # Stopped unit with an active listener means an unrelated process holds the port (§5).
        if active_state in ("inactive", "failed") and main_pid == 0 and listener_up:
            raise RecoveryError(
                "unrelated listener is bound on the console port while service is stopped"
            )

        # Drain any stale/failed service, acquire writer lock briefly to re-fence, then restart.
        wait_service_drained(
            stop_unit=service.stop,
            get_unit_state=service.unit_state,
            get_cgroup_pids=service.get_cgroup_pids,
            is_listener_active=service.is_listener_active,
            kill_cgroup=service.kill_cgroup,
        )
        with acquire_writer_lock(resolved_root, blocking=False, home=home):
            fsync_file(sel_path)
            fsync_dir(resolved_root)
            if selector["mode"] == "managed":
                marker_state = inspect_serving_marker(
                    resolved_root, op_id, selector.get("release_id"), selector.get("generation_id")
                )
                if marker_state == "served":
                    mpath = resolved_root / "operations" / op_id / SERVED_MARKER_FILENAME
                    fsync_file(mpath)
                    fsync_dir(mpath.parent)
        need_restart = True

    if need_restart:
        start_unit_with_reset(reset_failed=service.reset_failed, start_unit=service.start)
        ready = _wait_live_ready(resolved_root, selector, service, timeout_s=ready_timeout_s)
        return {
            "action": "recover",
            "status": "ready" if ready else "committed, console not ready",
            "mode": selector["mode"],
            "operation_id": op_id,
            "release_id": selector.get("release_id"),
            "generation_id": selector.get("generation_id"),
            "incomplete_intents": incomplete,
        }
    raise RecoveryError("unexpected recovery state")


def _probe_remote_tip(remote_url: str, ref: str = "refs/heads/main") -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    res = subprocess.run(
        ["git", "ls-remote", remote_url, ref],
        env=env,
        capture_output=True,
        text=True,
        close_fds=True,
        timeout=15,
    )
    if res.returncode != 0:
        raise UpdaterError(f"git ls-remote failed: {res.stderr.strip()}")
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == ref:
            return parts[0]
    raise UpdaterError(f"remote ref {ref!r} not found")


def check_status(
    *,
    root: str | Path,
    remote_url: str | None = None,
    ref: str = "refs/heads/main",
    home: str | Path | None = None,
    ls_remote_fn: Callable[[str, str], str] | None = None,
) -> dict:
    """Read-only preview (--check): creates zero locks, directories, or logs, and touches no unit.

    Returns exit_code: 0 (same commit), 10 (different tip, apply preflight required), 11 (known
    refusal / unenrolled), 75 (busy gate), or 1 (transport failure).
    """
    resolved_root = validate_updater_root(root, home=home)
    sel_path = resolved_root / "current.json"
    if not sel_path.is_file():
        return {
            "enrolled": False,
            "exit_code": 11,
            "status": "not enrolled; run verified enrollment first",
        }

    # N2: probe with LOCK_SH|LOCK_NB so shared console startup does not trigger false busy (75),
    # while any exclusive maintenance op (LOCK_EX) does!
    lock_file = resolved_root / "locks" / "mutation.lock"
    if lock_file.is_file() and not lock_file.is_symlink():
        try:
            with acquire_shared_gate(resolved_root, blocking=False, home=home):
                pass
        except LockBusyError:
            return {
                "enrolled": True,
                "exit_code": EXIT_GATE_BUSY,
                "status": "busy — maintenance holds mutation gate",
            }
        except OSError:
            pass

    try:
        selector = _read_selector(resolved_root)
    except UpdaterError as exc:
        return {"enrolled": True, "exit_code": 11, "status": f"invalid selector: {exc}"}

    enr_path = resolved_root / "enrollment.json"
    enr = json.loads(enr_path.read_text(encoding="utf-8")) if enr_path.is_file() else {}
    selected_commit = (
        selector.get("commit") if selector["mode"] == "managed" else enr.get("legacy_commit")
    )
    effective_remote = remote_url or enr.get("trusted_remote")

    out = {
        "enrolled": True,
        "mode": selector["mode"],
        "operation_id": selector["operation_id"],
        "release_id": selector.get("release_id"),
        "generation_id": selector.get("generation_id"),
        "commit": selected_commit,
        "exit_code": 0,
        "status": "up to date",
    }

    if effective_remote:
        probe = ls_remote_fn or _probe_remote_tip
        try:
            remote_tip = probe(str(effective_remote), ref)
        except (UpdaterError, OSError, subprocess.SubprocessError) as exc:
            out["exit_code"] = 1
            out["status"] = f"transport failure: {exc}"
            return out
        out["remote_tip"] = remote_tip
        if remote_tip != selected_commit:
            out["exit_code"] = 10
            out["status"] = "different tip, apply preflight required"
    return out


def requirements_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
