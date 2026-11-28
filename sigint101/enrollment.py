# ABOUTME: Fixture-level enrollment prerequisite workflow establishing permanent locks, control
# ABOUTME: generation, sealed runtime, legacy selector, shims, and atomic unit replacement.

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import secrets
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

ENROLLMENT_RECOVERY_FILENAME = "enrollment-recovery.json"


class EnrollmentError(RuntimeError):
    """Raised when an enrollment prerequisite or ordering invariant fails."""


def _atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o644,
    before_replace: Callable[[], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.fspath(tmp), flags, mode)
    replaced = False
    try:
        try:
            view = memoryview(data)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        if before_replace is not None:
            before_replace()
        os.replace(tmp, path)
        replaced = True
    except BaseException:
        if not replaced:
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    fsync_dir(path.parent)


def _cleanup_unpublished_temps(target: Path) -> None:
    """Remove unpublished regular `.tmp.<pid>[.<hex>]` siblings for `target` without touching it."""
    parent = target.parent
    if parent.is_symlink() or not parent.is_dir():
        return
    pat = re.compile(rf"^\.{re.escape(target.name)}\.tmp\.\d+(?:\.[0-9a-f]+)?$")
    removed = False
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for name in names:
        if not pat.match(name):
            continue
        candidate = parent / name
        try:
            lst = os.lstat(candidate)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(lst.st_mode):
            continue
        try:
            candidate.unlink()
            removed = True
        except OSError:
            pass
    if removed:
        try:
            fsync_dir(parent)
        except OSError:
            pass


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


def _save_digest_backup(
    originals_dir: Path,
    stem: str,
    suffix: str,
    data: bytes,
) -> tuple[Path, str]:
    """Persist verified immutable digest-keyed backup (0400, fsynced) and check bytes agree (F4)."""
    originals_dir.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    dest = originals_dir / f"{stem}-{digest}{suffix}"
    existing = _read_regular_bytes(dest)
    if existing is not None:
        if hashlib.sha256(existing).hexdigest() != digest:
            raise EnrollmentError(
                f"digest-keyed backup corrupted on disk: {dest} does not match {digest}"
            )
        return dest, digest
    _atomic_write_bytes(dest, data, mode=0o400)
    return dest, digest


def _update_latest_original(originals_dir: Path, name: str, data: bytes) -> Path:
    """Keep `originals/<name>` synced to the exact bytes of the file currently being replaced."""
    originals_dir.mkdir(mode=0o700, exist_ok=True)
    dest = originals_dir / name
    existing = _read_regular_bytes(dest)
    if existing == data:
        return dest
    _atomic_write_bytes(dest, data, mode=0o400)
    return dest


def _find_verified_launcher_backup(
    originals_dir: Path,
    *,
    expected_sha: str | None = None,
) -> tuple[Path | None, str | None]:
    """Locate a launcher backup whose on-disk SHA-256 matches `expected_sha` (or latest)."""
    if expected_sha is not None and _is_valid_hex_sha256(expected_sha):
        keyed = originals_dir / f"desktop-wrapper-{expected_sha}.sh"
        raw = _read_regular_bytes(keyed)
        if raw is not None and hashlib.sha256(raw).hexdigest() == expected_sha:
            return keyed, expected_sha
        latest = originals_dir / "desktop-wrapper.sh"
        raw_latest = _read_regular_bytes(latest)
        if raw_latest is not None and hashlib.sha256(raw_latest).hexdigest() == expected_sha:
            return latest, expected_sha
        return None, None

    latest = originals_dir / "desktop-wrapper.sh"
    raw_latest = _read_regular_bytes(latest)
    if raw_latest is None:
        return None, None
    sha = hashlib.sha256(raw_latest).hexdigest()
    keyed = originals_dir / f"desktop-wrapper-{sha}.sh"
    raw_keyed = _read_regular_bytes(keyed)
    if raw_keyed is not None and hashlib.sha256(raw_keyed).hexdigest() == sha:
        return keyed, sha
    return latest, sha


def _refuse_if_managed_selected(root: Path) -> None:
    """Refuse enrollment when current.json already selects a managed generation (F2)."""
    cur_path = root / "current.json"
    try:
        lst = os.lstat(cur_path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(lst.st_mode):
        raise EnrollmentError(
            f"refusing enrollment: {cur_path} is not a regular file (no changes made)"
        )
    try:
        selector = json.loads(cur_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise EnrollmentError(
            f"refusing enrollment: {cur_path} is corrupt or unreadable: {exc} (no changes made)"
        ) from exc
    if not isinstance(selector, dict):
        raise EnrollmentError(
            f"refusing enrollment: {cur_path} is malformed (no changes made)"
        )
    mode = selector.get("mode")
    if mode == "managed":
        raise EnrollmentError(
            f"refusing enrollment: managed generation is already selected in {cur_path} "
            f"(release={selector.get('release_id')!r}, "
            f"generation={selector.get('generation_id')!r}); no changes made"
        )
    if mode != "legacy":
        raise EnrollmentError(
            f"refusing enrollment: unsupported selector mode {mode!r} in {cur_path} "
            "(no changes made)"
        )


def _verify_destination_matches(
    path: Path,
    *,
    existed: bool,
    expected_bytes: bytes | None = None,
    expected_sha: str | None = None,
    expected_mode: int | None = None,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> str | None:
    """Verify file type, contents, mode, and ownership match pre-enrollment state (F3b).

    Returns None iff every checked attribute matches, otherwise a diagnostic naming the drift.
    """
    if not existed:
        if path.exists() or path.is_symlink():
            return "file still exists (originally absent)"
        return None
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return f"cannot stat: {exc}"
    if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
        return "not a regular file"

    fflags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), fflags)
    except OSError as exc:
        return f"cannot open: {exc}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "not a regular file"
        chunks: list[bytes] = []
        while True:
            part = os.read(fd, 65536)
            if not part:
                break
            chunks.append(part)
        actual = b"".join(chunks)
    except OSError as exc:
        return f"cannot read: {exc}"
    finally:
        os.close(fd)

    diffs: list[str] = []
    if expected_bytes is not None:
        if actual != expected_bytes:
            diffs.append("bytes mismatch")
    elif expected_sha is not None:
        if hashlib.sha256(actual).hexdigest() != expected_sha:
            diffs.append("bytes mismatch")
    if expected_mode is not None:
        actual_mode = st.st_mode & 0o7777
        if actual_mode != expected_mode:
            diffs.append(f"mode {actual_mode:04o} != expected {expected_mode:04o}")
    if expected_uid is not None and st.st_uid != expected_uid:
        diffs.append(f"uid {st.st_uid} != expected {expected_uid}")
    if expected_gid is not None and st.st_gid != expected_gid:
        diffs.append(f"gid {st.st_gid} != expected {expected_gid}")
    return "; ".join(diffs) if diffs else None


def _destination_matches_original(
    path: Path,
    orig_bytes: bytes | None,
    *,
    mode: int | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> bool:
    """Return True iff `path` on disk currently matches its pre-enrollment state."""
    return (
        _verify_destination_matches(
            path,
            existed=orig_bytes is not None,
            expected_bytes=orig_bytes,
            expected_mode=mode,
            expected_uid=uid,
            expected_gid=gid,
        )
        is None
    )


def _persist_recovery_journal(rec_path: Path, record: dict[str, object]) -> None:
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _restore_file_best_effort(rec_path, payload, mode=0o600)


def _is_journal_already_committed(root: Path, record: dict[str, object]) -> bool:
    """Return True iff `enrollment.json` durably committed this exact journal's attempt_id (F3c)."""
    journal_aid = record.get("attempt_id")
    if not isinstance(journal_aid, str) or not journal_aid:
        return False
    enr_raw = _read_regular_bytes(root / "enrollment.json")
    if not enr_raw:
        return False
    try:
        enr = json.loads(enr_raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return (
        isinstance(enr, dict)
        and enr.get("status") == "complete"
        and enr.get("handoff_complete") is True
        and enr.get("attempt_id") == journal_aid
    )


def recover_interrupted_enrollment(
    updater_root: str | Path,
    *,
    reload_systemd: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Restore destinations recorded in durable `enrollment-recovery.json` after a crash (F3)."""
    root = Path(updater_root)
    rec_path = root / ENROLLMENT_RECOVERY_FILENAME
    raw = _read_regular_bytes(rec_path)
    if raw is None:
        return {"recovered": False, "had_recovery_state": False, "restored": []}
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EnrollmentError(
            f"corrupt durable enrollment recovery state at {rec_path}: {exc}"
        ) from exc
    if not isinstance(record, dict) or not isinstance(record.get("destinations"), list):
        raise EnrollmentError(f"invalid durable enrollment recovery state at {rec_path}")

    # F3c: If this journal belongs to an already-committed handover, retire it cleanly without
    # replaying or restoring legacy files.
    if _is_journal_already_committed(root, record):
        try:
            rec_path.unlink(missing_ok=True)
            fsync_dir(root)
        except OSError as exc:
            raise EnrollmentError(
                f"enrollment already committed but failed to retire leftover journal "
                f"{rec_path}: {exc}"
            ) from exc
        return {
            "recovered": False,
            "had_recovery_state": True,
            "retired_completed_journal": True,
            "restored": [],
        }

    restored: list[str] = []
    unrestored: list[str] = []
    reload_pending = bool(record.get("unit_reload_pending", False))
    unit_unrestored = False
    unit_reloaded = False

    for entry in reversed(record["destinations"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            unrestored.append("<invalid-entry>")
            continue
        label = str(entry.get("label") or entry["path"])
        dest = Path(entry["path"])
        _cleanup_unpublished_temps(dest)
        existed = bool(entry.get("existed"))
        if existed:
            expected_sha = entry.get("original_sha256")
            backup_str = entry.get("backup_path")
            orig_mode = int(entry.get("original_mode", 0o644))
            raw_uid = entry.get("original_uid")
            raw_gid = entry.get("original_gid")
            orig_uid = int(raw_uid) if isinstance(raw_uid, int) and not isinstance(
                raw_uid, bool
            ) else None
            orig_gid = int(raw_gid) if isinstance(raw_gid, int) and not isinstance(
                raw_gid, bool
            ) else None

            mismatch = _verify_destination_matches(
                dest,
                existed=True,
                expected_sha=expected_sha if isinstance(expected_sha, str) else None,
                expected_mode=orig_mode,
                expected_uid=orig_uid,
                expected_gid=orig_gid,
            )
            if mismatch is None:
                continue

            backup_bytes = (
                _read_regular_bytes(Path(backup_str)) if isinstance(backup_str, str) else None
            )
            if (
                backup_bytes is None
                or not _is_valid_hex_sha256(expected_sha)
                or hashlib.sha256(backup_bytes).hexdigest() != expected_sha
            ):
                unrestored.append(f"{label} (missing or mismatched backup; {mismatch})")
                if label == "unit_install":
                    unit_unrestored = True
                    reload_pending = True
                continue
            ok = _restore_file_with_owner(
                dest, backup_bytes, mode=orig_mode, uid=orig_uid, gid=orig_gid
            )
            post_mismatch = _verify_destination_matches(
                dest,
                existed=True,
                expected_bytes=backup_bytes,
                expected_mode=orig_mode,
                expected_uid=orig_uid,
                expected_gid=orig_gid,
            )
            if not ok or post_mismatch is not None:
                detail = post_mismatch or "restore write/sync failed"
                unrestored.append(f"{label} ({detail})")
                if label == "unit_install":
                    unit_unrestored = True
                    reload_pending = True
                continue
            restored.append(label)
            if label == "unit_install":
                reload_pending = True
        else:
            mismatch = _verify_destination_matches(dest, existed=False)
            if mismatch is not None:
                try:
                    dest.unlink()
                    fsync_dir(dest.parent)
                except OSError as exc:
                    unrestored.append(f"{label} ({exc})")
                    if label == "unit_install":
                        unit_unrestored = True
                        reload_pending = True
                    continue
                post_mismatch = _verify_destination_matches(dest, existed=False)
                if post_mismatch is not None:
                    unrestored.append(f"{label} ({post_mismatch})")
                    if label == "unit_install":
                        unit_unrestored = True
                        reload_pending = True
                    continue
                restored.append(label)
                if label == "unit_install":
                    reload_pending = True

    # F3a: Durably persist unit_reload_pending BEFORE attempting reload so a crash or failure
    # during reload guarantees the next recovery retries reload even when the unit file matches.
    if reload_pending and not record.get("unit_reload_pending"):
        record = {**record, "unit_reload_pending": True}
        _persist_recovery_journal(rec_path, record)

    if reload_pending and not unit_unrestored:
        if reload_systemd is None:
            unrestored.append("reload_systemd (no service manager reload callback)")
        else:
            try:
                reload_systemd()
            except Exception as exc:
                unrestored.append(f"reload_systemd ({exc})")
            else:
                reload_pending = False
                unit_reloaded = True

    if unrestored or reload_pending:
        incomplete_rec = {
            **record,
            "status": "recovery_incomplete",
            "unit_reload_pending": reload_pending,
            "unrestored": unrestored,
        }
        _persist_recovery_journal(rec_path, incomplete_rec)
        raise EnrollmentError(
            f"enrollment recovery incomplete for {', '.join(unrestored)}; "
            f"durable recovery state kept at {rec_path}"
        )

    try:
        rec_path.unlink(missing_ok=True)
        fsync_dir(root)
    except OSError as exc:
        raise EnrollmentError(
            f"recovered destinations but failed to clear recovery state {rec_path}: {exc}"
        ) from exc

    return {
        "recovered": True,
        "had_recovery_state": True,
        "restored": restored,
        "unit_reloaded": unit_reloaded,
    }


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


def _restore_file_best_effort(
    path: Path,
    data: bytes,
    *,
    mode: int,
) -> bool:
    """Restore a file after a late enrollment write failure, fencing bytes and mode."""
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
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        fsync_dir(path.parent)
        return True
    except OSError:
        return False


def _restore_file_with_owner(
    path: Path,
    data: bytes,
    *,
    mode: int,
    uid: int | None = None,
    gid: int | None = None,
) -> bool:
    """Restore file bytes, mode, and original ownership, fencing metadata before returning."""
    if not _restore_file_best_effort(path, data, mode=mode):
        return False
    if uid is None and gid is None:
        return True
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return False
            target_uid = st.st_uid if uid is None else uid
            target_gid = st.st_gid if gid is None else gid
            if (st.st_uid, st.st_gid) != (target_uid, target_gid):
                os.fchown(fd, target_uid, target_gid)
                os.fchmod(fd, mode)
                os.fsync(fd)
                fsync_dir(path.parent)
        finally:
            os.close(fd)
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
        # 2a) F2: Refuse rerun immediately under the mutation gate and BEFORE the first write
        # if a managed generation is already selected in current.json.
        _refuse_if_managed_selected(root)

        # 2b) F3: If a prior interrupted enrollment left durable recovery state on disk, restore
        # every out-of-step destination to its verified pre-enrollment bytes before proceeding.
        if (root / ENROLLMENT_RECOVERY_FILENAME).exists():
            recover_interrupted_enrollment(root, reload_systemd=reload_systemd)

        # 2c) F5 & F1: Under the mutation gate, reclaim abandoned private staging siblings left by
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
        ctrl_manifest = ctrl_mod.verify_control_generation(
            control_dir, expected_control_id=control_id
        )

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
            "contract_digest": ctrl_manifest["contract_digest"],
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
        shim_writes: list[
            tuple[Path, bytes, bytes | None, int, int | None, int | None, Path | None]
        ] = []
        for legacy_name, shim_name in (
            ("install.sh", "install-shim.sh"),
            ("provision.sh", "provision-shim.sh"),
        ):
            target = checkout / "deploy" / legacy_name
            shim_bytes = (managed_dir / shim_name).read_bytes()
            shim_sha = hashlib.sha256(shim_bytes).hexdigest()
            cur_bytes = _read_regular_bytes(target)
            if cur_bytes is not None:
                cur_st = os.lstat(target)
                cur_mode = cur_st.st_mode & 0o7777
                cur_uid: int | None = cur_st.st_uid
                cur_gid: int | None = cur_st.st_gid
            else:
                cur_mode = 0o755
                cur_uid = None
                cur_gid = None
            cur_sha = hashlib.sha256(cur_bytes or b"").hexdigest()
            backup_path: Path | None = None

            if cur_bytes is not None and cur_sha != shim_sha:
                stem, suffix = os.path.splitext(legacy_name)
                backup_path, orig_sha = _save_digest_backup(originals_dir, stem, suffix, cur_bytes)
                _update_latest_original(originals_dir, legacy_name, cur_bytes)
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
                # Preserve exact pre-run bytes (even if already shimmed) for durable recovery.
                stem, suffix = os.path.splitext(legacy_name)
                backup_path, _ = _save_digest_backup(originals_dir, stem, suffix, cur_bytes)

            shim_records[legacy_name] = {
                "original_sha256": orig_sha,
                "shim_sha256": shim_sha,
            }
            if target.parent.is_dir():
                shim_writes.append(
                    (target, shim_bytes, cur_bytes, cur_mode, cur_uid, cur_gid, backup_path)
                )

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

        # F4: Preserve verified digest-keyed backup for the exact launcher being replaced (keeping
        # earlier versions too) AND keep `originals/desktop-wrapper.sh` synced to that same digest.
        # Recorded `original_backup_path` always matches `original_sha256`.
        wrapper_backup_path: Path | None = None
        if cur_wrapper_sha is not None and cur_wrapper_sha not in managed_wrapper_digests:
            wrapper_backup_path, wrapper_orig_sha = _save_digest_backup(
                originals_dir, "desktop-wrapper", ".sh", cur_wrapper_bytes or b""
            )
            _update_latest_original(
                originals_dir, "desktop-wrapper.sh", cur_wrapper_bytes or b""
            )
            wrapper_orig_reviewed = wrapper_orig_sha in reviewed_legacy_digests
        elif (
            _is_valid_hex_sha256(prior_wrapper_orig)
            and prior_wrapper_orig not in managed_wrapper_digests
        ):
            wrapper_orig_sha = str(prior_wrapper_orig)
            wrapper_orig_reviewed = wrapper_orig_sha in reviewed_legacy_digests
            wrapper_backup_path, _ = _find_verified_launcher_backup(
                originals_dir, expected_sha=wrapper_orig_sha
            )
        else:
            cand_path, cand_sha = _find_verified_launcher_backup(originals_dir)
            if cand_sha and cand_sha not in managed_wrapper_digests:
                wrapper_backup_path = cand_path
                wrapper_orig_sha = cand_sha
                wrapper_orig_reviewed = cand_sha in reviewed_legacy_digests
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
        if prior_unit_bytes is not None:
            prior_unit_st = os.lstat(unit_file)
            prior_unit_mode = prior_unit_st.st_mode & 0o7777
            prior_unit_uid: int | None = prior_unit_st.st_uid
            prior_unit_gid: int | None = prior_unit_st.st_gid
        else:
            prior_unit_mode = 0o644
            prior_unit_uid = None
            prior_unit_gid = None
        prior_unit_backup_path: Path | None = None
        if prior_unit_bytes is not None:
            prior_unit_backup_path, _ = _save_digest_backup(
                originals_dir, "sigint101", ".service", prior_unit_bytes
            )
            if b"dispatcher.py" not in prior_unit_bytes:
                _update_latest_original(originals_dir, "sigint101.service", prior_unit_bytes)

        # F3: Write durable recovery state with a unique per-attempt identity BEFORE modifying any
        # shim or unit destination so a crash or incomplete rollback leaves a verified restoration
        # plan on disk, and a matching committed receipt safely retires any leftover journal.
        attempt_id = f"{operation_id}-{secrets.token_hex(8)}"
        recovery_path = root / ENROLLMENT_RECOVERY_FILENAME
        recovery_destinations: list[dict[str, object]] = []
        for (
            target,
            _shim_bytes,
            orig_bytes,
            orig_mode,
            orig_uid,
            orig_gid,
            backup_path,
        ) in shim_writes:
            recovery_destinations.append(
                {
                    "label": f"deploy/{target.name}",
                    "path": str(target),
                    "existed": orig_bytes is not None,
                    "original_sha256": (
                        hashlib.sha256(orig_bytes).hexdigest() if orig_bytes is not None else None
                    ),
                    "original_mode": orig_mode,
                    "original_uid": orig_uid,
                    "original_gid": orig_gid,
                    "backup_path": str(backup_path) if backup_path is not None else None,
                }
            )
        recovery_destinations.append(
            {
                "label": "unit_install",
                "path": str(unit_file),
                "existed": prior_unit_bytes is not None,
                "original_sha256": (
                    hashlib.sha256(prior_unit_bytes).hexdigest()
                    if prior_unit_bytes is not None
                    else None
                ),
                "original_mode": prior_unit_mode,
                "original_uid": prior_unit_uid,
                "original_gid": prior_unit_gid,
                "backup_path": (
                    str(prior_unit_backup_path) if prior_unit_backup_path is not None else None
                ),
            }
        )
        recovery_payload: dict[str, object] = {
            "format_version": 1,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "status": "in_progress",
            "unit_reload_pending": False,
            "destinations": recovery_destinations,
        }
        _atomic_write_bytes(
            recovery_path,
            (json.dumps(recovery_payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )

        enrollment_meta: dict[str, object] = {
            "status": "in_progress",
            "handoff_complete": False,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
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
            "recovery_state_path": str(recovery_path),
        }
        _atomic_write_bytes(
            enr_path,
            (json.dumps(enrollment_meta, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )

        # 6b) Install checkout shims, unit, reload, drain, and final record inside one recovery
        # scope (F3). Each replacement is recorded BEFORE os.replace, unpublished temp files are
        # cleaned up, and rollback_ok is True ONLY after verifying every destination (bytes, mode,
        # ownership) and confirming systemd reload.
        attempted_shims: list[tuple[Path, bytes | None, int, int | None, int | None]] = []
        unit_attempted = False

        def _rollback_late_writes(failed_step: str, exc: BaseException) -> EnrollmentError:
            rollback_ok = True
            _cleanup_unpublished_temps(unit_file)
            _cleanup_unpublished_temps(enr_path)
            for target, _sb, _ob, _om, _ou, _og, _bp in shim_writes:
                _cleanup_unpublished_temps(target)

            unit_mismatch_before = _verify_destination_matches(
                unit_file,
                existed=prior_unit_bytes is not None,
                expected_bytes=prior_unit_bytes,
                expected_mode=prior_unit_mode,
                expected_uid=prior_unit_uid,
                expected_gid=prior_unit_gid,
            )
            reload_pending = False
            reload_error: str | None = None
            if unit_attempted or unit_mismatch_before is not None:
                reload_pending = True
                if prior_unit_bytes is not None:
                    rollback_ok &= _restore_file_with_owner(
                        unit_file,
                        prior_unit_bytes,
                        mode=prior_unit_mode,
                        uid=prior_unit_uid,
                        gid=prior_unit_gid,
                    )
                else:
                    try:
                        unit_file.unlink(missing_ok=True)
                        fsync_dir(unit_file.parent)
                    except OSError:
                        rollback_ok = False

            seen_targets: set[Path] = set()
            restore_order: list[
                tuple[Path, bytes | None, int, int | None, int | None]
            ] = list(reversed(attempted_shims))
            for target, _sb, orig_bytes, orig_mode, orig_uid, orig_gid, _bp in reversed(
                shim_writes
            ):
                if target not in {t for t, _, _, _, _ in restore_order}:
                    if not _destination_matches_original(
                        target, orig_bytes, mode=orig_mode, uid=orig_uid, gid=orig_gid
                    ):
                        restore_order.append((target, orig_bytes, orig_mode, orig_uid, orig_gid))

            for shim_target, orig_bytes, orig_mode, orig_uid, orig_gid in restore_order:
                if shim_target in seen_targets:
                    continue
                seen_targets.add(shim_target)
                if orig_bytes is not None:
                    rollback_ok &= _restore_file_with_owner(
                        shim_target, orig_bytes, mode=orig_mode, uid=orig_uid, gid=orig_gid
                    )
                else:
                    try:
                        shim_target.unlink(missing_ok=True)
                        fsync_dir(shim_target.parent)
                    except OSError:
                        rollback_ok = False

            # Independently verify every single destination on disk (bytes, mode, ownership):
            # NEVER report rollback_ok=True if any destination drifted or reload is unconfirmed!
            unrestored: list[str] = []
            unit_mismatch = _verify_destination_matches(
                unit_file,
                existed=prior_unit_bytes is not None,
                expected_bytes=prior_unit_bytes,
                expected_mode=prior_unit_mode,
                expected_uid=prior_unit_uid,
                expected_gid=prior_unit_gid,
            )
            if unit_mismatch is not None:
                unrestored.append(f"unit_install ({unit_mismatch})")
            elif reload_pending:
                if reload_systemd is None:
                    reload_error = "reload_systemd (no service manager reload callback)"
                    unrestored.append(reload_error)
                else:
                    try:
                        reload_systemd()
                    except Exception as r_exc:
                        reload_error = f"reload_systemd ({r_exc})"
                        unrestored.append(reload_error)
                    else:
                        reload_pending = False

            for target, _sb, orig_bytes, orig_mode, orig_uid, orig_gid, _bp in shim_writes:
                shim_mismatch = _verify_destination_matches(
                    target,
                    existed=orig_bytes is not None,
                    expected_bytes=orig_bytes,
                    expected_mode=orig_mode,
                    expected_uid=orig_uid,
                    expected_gid=orig_gid,
                )
                if shim_mismatch is not None:
                    unrestored.append(f"deploy/{target.name} ({shim_mismatch})")
            if unrestored or reload_pending:
                rollback_ok = False

            if rollback_ok:
                try:
                    recovery_path.unlink(missing_ok=True)
                    fsync_dir(root)
                except OSError:
                    pass
            else:
                incomplete_rec = {
                    **recovery_payload,
                    "status": "rollback_incomplete",
                    "unit_reload_pending": reload_pending,
                    "failed_step": failed_step,
                    "unrestored": unrestored,
                }
                _persist_recovery_journal(recovery_path, incomplete_rec)

            recovery_cmd = (
                "resolve the write error and rerun sigint101-updater --enroll"
                if rollback_ok
                else (
                    f"restore originals from {originals_dir} (durable state at {recovery_path}) "
                    "and rerun sigint101-updater --enroll"
                )
            )
            failed_meta = {
                **enrollment_meta,
                "status": "failed",
                "handoff_complete": False,
                "failed_step": failed_step,
                "rollback_ok": rollback_ok,
                "unit_reload_pending": reload_pending,
                "unrestored_destinations": unrestored,
                "recovery_command": recovery_cmd,
            }
            failed_bytes = (json.dumps(failed_meta, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
            _restore_file_best_effort(enr_path, failed_bytes, mode=0o600)
            detail = ", ".join(unrestored) or reload_error or "reload/sync failed"
            state_note = (
                "earlier enrollment writes rolled back cleanly"
                if rollback_ok
                else f"automatic rollback incomplete ({detail})"
            )
            return EnrollmentError(
                f"late enrollment write failed at {failed_step}: {exc} "
                f"({state_note}; recovery: {recovery_cmd})"
            )

        for target, shim_bytes, orig_bytes, orig_mode, orig_uid, orig_gid, _bp in shim_writes:
            try:
                _atomic_write_bytes(
                    target,
                    shim_bytes,
                    mode=0o755,
                    before_replace=lambda t=target, ob=orig_bytes, om=orig_mode,
                    ou=orig_uid, og=orig_gid: (
                        attempted_shims.append((t, ob, om, ou, og))
                    ),
                )
            except Exception as exc:
                raise _rollback_late_writes(f"deploy/{target.name}", exc) from exc

        if on_boundary is not None:
            on_boundary("before_unit_install")

        # 7) Re-verify control generation, then atomically install the managed unit and reload.
        def _mark_unit_attempted() -> None:
            nonlocal unit_attempted
            unit_attempted = True
            recovery_payload["unit_reload_pending"] = True
            _persist_recovery_journal(recovery_path, recovery_payload)

        try:
            ctrl_mod.verify_control_generation(control_dir, expected_control_id=control_id)
            unit_text = _render_managed_unit(
                template_path=managed_dir / "sigint101.service",
                host_python=host_python,
                dispatcher_path=dispatcher_dest,
                updater_root=root,
            )
            _atomic_write_bytes(
                unit_file,
                unit_text.encode("utf-8"),
                mode=0o644,
                before_replace=_mark_unit_attempted,
            )
            fsync_file(unit_file)
            if reload_systemd is not None:
                reload_systemd()
        except Exception as exc:
            raise _rollback_late_writes("unit_install", exc) from exc

        if on_boundary is not None:
            on_boundary("after_unit_install_before_drain")

        try:
            if drain_old_service is not None:
                drain_old_service()
        except Exception as exc:
            raise _rollback_late_writes("drain_old_service", exc) from exc

        if on_boundary is not None:
            on_boundary("before_completed_record")

        # 8) F3/F3c: Publish durable completed hand-over record with matching attempt_id BEFORE
        # retiring the recovery journal or enabling the managed wrapper. This atomic replace is
        # the single commit point of the hand-over.
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
        completed_meta.pop("recovery_state_path", None)
        try:
            _atomic_write_bytes(
                enr_path,
                (json.dumps(completed_meta, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                mode=0o600,
            )
        except Exception as exc:
            raise _rollback_late_writes("completed_record", exc) from exc

        if on_boundary is not None:
            on_boundary("after_completed_record_before_journal_unlink")

        # Completed hand-over record is durable; retire the recovery journal atomically.
        # Even if unlink fails or the process dies before unlink, the matching committed
        # attempt_id in enrollment.json ensures recovery retires this journal without replay.
        try:
            recovery_path.unlink(missing_ok=True)
            fsync_dir(root)
        except OSError:
            pass

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
