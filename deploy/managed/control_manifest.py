# ABOUTME: Explicit deployed-control contract allowlist, manifest hashing, and control staging.
# ABOUTME: Covers only executed/installed managed paths and protocol versions, never whole deploy/.

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path

CONTROL_MANIFEST_FILENAME = "control_manifest.json"
CONTROL_FORMAT_VERSION = 1
SELECTOR_FORMAT_VERSION = 1
DISPATCHER_PROTOCOL_VERSION = 1
LOCK_PROTOCOL_VERSION = 1
_CONTROL_STAGING_RE = re.compile(r"^\.[^/]+\.staging\.\d+\.[0-9a-f]+$")


class ControlGenerationError(RuntimeError):
    """Raised when a control generation is missing, partial, symlinked, or fails manifest checks."""

# Explicit allowlist of executed/installed managed control files and their stdlib support modules.
# Changes elsewhere under deploy/ (e.g. desktop theme CSS or docs) are NOT control drift.
CONTROL_ALLOWLIST: tuple[str, ...] = (
    "deploy/managed/control_manifest.py",
    "deploy/managed/dispatcher.py",
    "deploy/managed/launcher.py",
    "deploy/managed/service_entry.py",
    "deploy/managed/sigint101.service",
    "deploy/managed/sigint101-course-managed.sh",
    "deploy/managed/provision-shim.sh",
    "deploy/managed/install-shim.sh",
    "sigint101/bench_gate.py",
    "sigint101/enrollment.py",
    "sigint101/kiosk.py",
    "sigint101/locks.py",
    "sigint101/migrations.py",
    "sigint101/process_checks.py",
    "sigint101/release_prep.py",
    "sigint101/runtime.py",
    "sigint101/schema_fixtures.json",
    "sigint101/snapshot.py",
    "sigint101/storage.py",
    "sigint101/trial.py",
    "sigint101/unit_settings.py",
    "sigint101/updater.py",
    "sigint101/updater_cli.py",
)


def _sha256_file(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def compute_control_manifest(repo_root: str | Path) -> dict:
    """Hash every allowlisted managed-control file and record protocol format versions."""
    root = Path(repo_root).resolve()
    files: dict[str, dict] = {}
    for rel in CONTROL_ALLOWLIST:
        target = root / rel
        if not target.is_file() or target.is_symlink():
            raise FileNotFoundError(f"required deployed-control file missing or symlinked: {rel}")
        sha, size = _sha256_file(target)
        files[rel] = {"sha256": sha, "size": size}

    payload = {
        "control_format_version": CONTROL_FORMAT_VERSION,
        "selector_format_version": SELECTOR_FORMAT_VERSION,
        "dispatcher_protocol_version": DISPATCHER_PROTOCOL_VERSION,
        "lock_protocol_version": LOCK_PROTOCOL_VERSION,
        "files": files,
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload["contract_digest"] = hashlib.sha256(canonical).hexdigest()
    return payload


def compare_control_manifests(baseline: dict, candidate: dict) -> list[str]:
    """Return specific drift descriptions between two control manifests (empty means identical)."""
    drifts: list[str] = []
    for key in (
        "control_format_version",
        "selector_format_version",
        "dispatcher_protocol_version",
        "lock_protocol_version",
    ):
        if baseline.get(key) != candidate.get(key):
            drifts.append(
                f"protocol {key} changed: {baseline.get(key)!r} -> {candidate.get(key)!r}"
            )

    base_files = baseline.get("files") or {}
    cand_files = candidate.get("files") or {}
    for rel in sorted(set(base_files) | set(cand_files)):
        if rel not in base_files:
            drifts.append(f"added deployed-control file: {rel}")
        elif rel not in cand_files:
            drifts.append(f"removed deployed-control file: {rel}")
        elif base_files[rel] != cand_files[rel]:
            drifts.append(f"modified deployed-control file: {rel}")
    return drifts


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(os.fspath(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exclusive_synced(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.fspath(path), flags, mode)
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_staging_tree(path: Path) -> None:
    """Remove an incomplete or partial control directory, resetting read-only modes first."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        return
    for cur, dirs, files in os.walk(path, topdown=True, followlinks=False):
        cp = Path(cur)
        try:
            os.chmod(cp, 0o700)
        except OSError:
            pass
        for d in dirs:
            dp = cp / d
            if not dp.is_symlink():
                try:
                    os.chmod(dp, 0o700)
                except OSError:
                    pass
        for f in files:
            fp = cp / f
            if not fp.is_symlink():
                try:
                    os.chmod(fp, 0o600)
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)


def reclaim_abandoned_control_staging(
    control_parent: str | Path,
    *,
    protected_names: set[str] | None = None,
) -> list[str]:
    """Reclaim abandoned control staging siblings (never published generations or symlinks)."""
    parent = Path(control_parent)
    if parent.is_symlink() or not parent.is_dir():
        return []
    protected = protected_names or set()
    reclaimed: list[str] = []
    for name in sorted(os.listdir(parent)):
        if name in protected or not _CONTROL_STAGING_RE.match(name):
            continue
        entry = parent / name
        try:
            lst = os.lstat(entry)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(lst.st_mode) or not stat.S_ISDIR(lst.st_mode):
            continue
        remove_staging_tree(entry)
        reclaimed.append(name)
    if reclaimed:
        _fsync_dir(parent)
    return reclaimed


def verify_control_generation(
    control_dir: str | Path,
    *,
    expected_control_id: str | None = None,
) -> dict:
    """Verify a staged control generation against its manifest before selecting or booting it."""
    cdir = Path(control_dir)
    try:
        dir_st = os.lstat(cdir)
    except OSError as exc:
        raise ControlGenerationError(f"control generation missing: {cdir}") from exc
    if stat.S_ISLNK(dir_st.st_mode) or not stat.S_ISDIR(dir_st.st_mode):
        raise ControlGenerationError(f"control generation is not a real directory: {cdir}")

    manifest_path = cdir / CONTROL_MANIFEST_FILENAME
    fflags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        mfd = os.open(os.fspath(manifest_path), fflags)
    except OSError as exc:
        raise ControlGenerationError(f"control manifest missing or unreadable: {cdir}") from exc
    try:
        mst = os.fstat(mfd)
        if not stat.S_ISREG(mst.st_mode):
            raise ControlGenerationError(f"control manifest is not a regular file: {manifest_path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(mfd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        raw_manifest = b"".join(chunks)
    finally:
        os.close(mfd)

    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ControlGenerationError(f"malformed control manifest in {cdir}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ControlGenerationError(f"invalid control manifest object in {cdir}")

    expected_versions = {
        "control_format_version": CONTROL_FORMAT_VERSION,
        "selector_format_version": SELECTOR_FORMAT_VERSION,
        "dispatcher_protocol_version": DISPATCHER_PROTOCOL_VERSION,
        "lock_protocol_version": LOCK_PROTOCOL_VERSION,
    }
    for key, exp in expected_versions.items():
        if manifest.get(key) != exp:
            raise ControlGenerationError(
                f"control manifest {key} mismatch in {cdir}: {manifest.get(key)!r} != {exp!r}"
            )

    cid = manifest.get("control_id")
    if not isinstance(cid, str) or not cid or "/" in cid or cid in (".", ".."):
        raise ControlGenerationError(f"invalid control_id in manifest: {cid!r}")
    if expected_control_id is not None and cid != expected_control_id:
        raise ControlGenerationError(
            f"control_id mismatch in {cdir}: {cid!r} != {expected_control_id!r}"
        )

    expected_files = manifest.get("files")
    if not isinstance(expected_files, dict):
        raise ControlGenerationError(f"control manifest missing files table in {cdir}")
    if set(expected_files.keys()) != set(CONTROL_ALLOWLIST):
        raise ControlGenerationError(
            f"control manifest files table does not match allowlist in {cdir}"
        )

    digest_payload = {**expected_versions, "files": expected_files}
    canonical = json.dumps(digest_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    actual_contract_digest = hashlib.sha256(canonical).hexdigest()
    if manifest.get("contract_digest") != actual_contract_digest:
        raise ControlGenerationError(f"control manifest contract_digest mismatch in {cdir}")

    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    actual_files: dict[str, dict] = {}
    has_pkg_init = False
    has_manifest = False

    def _scan(fd: int, prefix: str) -> None:
        nonlocal has_pkg_init, has_manifest
        for name in sorted(os.listdir(fd)):
            rel = f"{prefix}/{name}" if prefix else name
            lst = os.lstat(name, dir_fd=fd)
            if stat.S_ISLNK(lst.st_mode):
                raise ControlGenerationError(f"symlink refused in control generation: {rel}")
            if stat.S_ISDIR(lst.st_mode):
                child_fd = os.open(name, dir_flags, dir_fd=fd)
                try:
                    _scan(child_fd, rel)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(lst.st_mode):
                if rel == CONTROL_MANIFEST_FILENAME:
                    has_manifest = True
                    continue
                if rel == "sigint101/__init__.py":
                    has_pkg_init = True
                    continue
                if rel not in expected_files:
                    raise ControlGenerationError(f"unexpected file in control generation: {rel}")
                rfd = os.open(name, fflags, dir_fd=fd)
                try:
                    rst = os.fstat(rfd)
                    if not stat.S_ISREG(rst.st_mode):
                        raise ControlGenerationError(
                            f"non-regular file in control generation: {rel}"
                        )
                    fchunks: list[bytes] = []
                    while True:
                        part = os.read(rfd, 65536)
                        if not part:
                            break
                        fchunks.append(part)
                    payload = b"".join(fchunks)
                finally:
                    os.close(rfd)
                actual_files[rel] = {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
            else:
                raise ControlGenerationError(f"non-regular file in control generation: {rel}")

    root_fd = os.open(os.fspath(cdir), dir_flags)
    try:
        _scan(root_fd, "")
    finally:
        os.close(root_fd)

    if not has_manifest or not has_pkg_init:
        raise ControlGenerationError(f"control generation missing required marker files: {cdir}")
    if actual_files != expected_files:
        raise ControlGenerationError(f"control generation files drifted from manifest in {cdir}")

    return manifest


def stage_control_generation(
    repo_root: str | Path,
    dest_control_dir: str | Path,
    *,
    control_id: str,
) -> dict:
    """Stage allowlisted managed control files privately, verify, seal, and publish atomically."""
    if not control_id or "/" in control_id or control_id in (".", ".."):
        raise ValueError(f"invalid control_id: {control_id!r}")
    root = Path(repo_root).resolve()
    dst = Path(dest_control_dir)
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"destination control generation already exists: {dst}")

    manifest = compute_control_manifest(root)
    manifest["control_id"] = control_id

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst: Path | None = (
        dst.parent / f".{dst.name}.staging.{os.getpid()}.{secrets.token_hex(4)}"
    )
    created_dirs: set[Path] = set()
    created_files: list[Path] = []

    try:
        os.mkdir(tmp_dst, 0o700)
        created_dirs.add(tmp_dst)

        # Minimal package marker so staged sigint101/ support modules import cleanly without
        # pulling the full course tree into the control generation.
        pkg_init = tmp_dst / "sigint101" / "__init__.py"
        pkg_init.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        created_dirs.add(pkg_init.parent)
        _write_exclusive_synced(
            pkg_init,
            (
                "# ABOUTME: Package marker for staged managed-control support modules.\n"
                "# ABOUTME: Keeps control runtime imports isolated from course releases.\n"
            ).encode("utf-8"),
            mode=0o600,
        )
        created_files.append(pkg_init)

        for rel in CONTROL_ALLOWLIST:
            src_file = root / rel
            out_file = tmp_dst / rel
            out_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            cur = out_file.parent
            while cur != tmp_dst and cur.is_relative_to(tmp_dst):
                created_dirs.add(cur)
                cur = cur.parent
            data = src_file.read_bytes()
            _write_exclusive_synced(out_file, data, mode=0o600)
            created_files.append(out_file)

        manifest_path = tmp_dst / CONTROL_MANIFEST_FILENAME
        m_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _write_exclusive_synced(manifest_path, m_bytes, mode=0o600)
        created_files.append(manifest_path)

        # Verify the staged sibling completely against its manifest before sealing & publishing.
        verify_control_generation(tmp_dst, expected_control_id=control_id)

        for fpath in created_files:
            mode = 0o500 if fpath.suffix in (".py", ".sh") else 0o400
            os.chmod(fpath, mode)
        for dpath in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
            _fsync_dir(dpath)
            os.chmod(dpath, 0o500)

        os.rename(tmp_dst, dst)
        tmp_dst = None
        _fsync_dir(dst.parent)
    except BaseException:
        if tmp_dst is not None:
            remove_staging_tree(tmp_dst)
        raise

    return manifest
