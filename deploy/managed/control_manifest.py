# ABOUTME: Explicit deployed-control contract allowlist, manifest hashing, and control staging.
# ABOUTME: Covers only executed/installed managed paths and protocol versions, never whole deploy/.

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

CONTROL_MANIFEST_FILENAME = "control_manifest.json"
CONTROL_FORMAT_VERSION = 1
SELECTOR_FORMAT_VERSION = 1
DISPATCHER_PROTOCOL_VERSION = 1
LOCK_PROTOCOL_VERSION = 1

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
    "sigint101/enrollment.py",
    "sigint101/kiosk.py",
    "sigint101/locks.py",
    "sigint101/migrations.py",
    "sigint101/process_checks.py",
    "sigint101/runtime.py",
    "sigint101/schema_fixtures.json",
    "sigint101/snapshot.py",
    "sigint101/storage.py",
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


def stage_control_generation(
    repo_root: str | Path,
    dest_control_dir: str | Path,
    *,
    control_id: str,
) -> dict:
    """Stage allowlisted managed control files into an exclusive immutable control generation."""
    if not control_id or "/" in control_id or control_id in (".", ".."):
        raise ValueError(f"invalid control_id: {control_id!r}")
    root = Path(repo_root).resolve()
    dst = Path(dest_control_dir)
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"destination control generation already exists: {dst}")

    manifest = compute_control_manifest(root)
    manifest["control_id"] = control_id

    dst.parent.mkdir(parents=True, exist_ok=True)
    os.mkdir(dst, 0o700)
    created_dirs: set[Path] = {dst}
    created_files: list[Path] = []

    # Minimal package marker so staged sigint101/ support modules import cleanly without
    # pulling the full course tree into the control generation.
    pkg_init = dst / "sigint101" / "__init__.py"
    pkg_init.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    created_dirs.add(pkg_init.parent)
    pkg_init.write_text(
        "# ABOUTME: Package marker for staged managed-control support modules.\n"
        "# ABOUTME: Keeps control runtime imports isolated from course releases.\n",
        encoding="utf-8",
    )
    created_files.append(pkg_init)

    for rel in CONTROL_ALLOWLIST:
        src_file = root / rel
        out_file = dst / rel
        out_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        cur = out_file.parent
        while cur != dst and cur.is_relative_to(dst):
            created_dirs.add(cur)
            cur = cur.parent
        data = src_file.read_bytes()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(os.fspath(out_file), flags, 0o600)
        try:
            view = memoryview(data)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        created_files.append(out_file)

    manifest_path = dst / CONTROL_MANIFEST_FILENAME
    m_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    m_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    m_fd = os.open(os.fspath(manifest_path), m_flags, 0o600)
    try:
        os.write(m_fd, m_bytes)
        os.fsync(m_fd)
    finally:
        os.close(m_fd)
    created_files.append(manifest_path)

    for fpath in created_files:
        mode = 0o500 if fpath.suffix in (".py", ".sh") else 0o400
        os.chmod(fpath, mode)
    for dpath in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
        _fsync_dir(dpath)
        os.chmod(dpath, 0o500)
    _fsync_dir(dst.parent)
    return manifest
