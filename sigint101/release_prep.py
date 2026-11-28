# ABOUTME: Pinned private Git fetch, bounded release export, manifest verification, and space
# ABOUTME: budget enforcement for manual course updates without touching live or legacy checkouts.

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

from .storage import fsync_dir

RELEASE_MANIFEST_FILENAME = "release.json"
DEFAULT_MAX_PACK_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_EXPORT_BYTES = 64 * 1024 * 1024
DEFAULT_RELEASE_ESTIMATE_BYTES = 16 * 1024 * 1024
DEFAULT_FREE_RESERVE_BYTES = 64 * 1024 * 1024


class ReleasePrepError(RuntimeError):
    """Base error raised when release preparation, verification, or budgeting fails."""


class PolicyRefusalError(ReleasePrepError):
    """Raised when origin, pin, ancestry, protected paths, or contract checks refuse a release."""


class SpaceBudgetError(ReleasePrepError):
    """Raised when available filesystem space falls below the conservative apply budget."""


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = Path(root) / name
            try:
                st = fp.lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def estimate_apply_budget_bytes(
    *,
    source_data_dir: str | Path,
    release_estimate_bytes: int = DEFAULT_RELEASE_ESTIMATE_BYTES,
    reserve_bytes: int = DEFAULT_FREE_RESERVE_BYTES,
) -> int:
    """Compute conservative required bytes: Git pack, release, 2nd generation, scratch, reserve."""
    gen_bytes = _dir_size_bytes(Path(source_data_dir))
    # Full second generation + scratch SQLite recovery copies + growth margin.
    data_budget = gen_bytes * 2 + 16 * 1024 * 1024
    # Private bare Git pack + unpacked release tree.
    release_budget = release_estimate_bytes * 2
    return data_budget + release_budget + reserve_bytes


def check_space_budget(
    target_path: str | Path,
    *,
    required_bytes: int,
    statvfs_fn: Callable[[str | Path], object] = os.statvfs,
) -> tuple[int, int]:
    """Require available bytes >= required_bytes; fail with both numbers on shortfall."""
    probe = Path(target_path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    sv = statvfs_fn(probe)
    available = int(sv.f_bavail) * int(sv.f_frsize)
    if available < required_bytes:
        raise SpaceBudgetError(
            f"insufficient disk space: required {required_bytes} bytes, "
            f"available {available} bytes"
        )
    return required_bytes, available


def _normalize_remote(url: str) -> str:
    return url.strip().rstrip("/")


def validate_remote_url(remote_url: str, *, trusted_remote: str) -> None:
    """Refuse any foreign remote URL that does not match the enrolled trusted origin."""
    if _normalize_remote(remote_url) != _normalize_remote(trusted_remote):
        raise PolicyRefusalError(
            f"foreign remote refused: {remote_url!r} does not match trusted remote "
            f"{trusted_remote!r}"
        )


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    timeout_s: float = 60.0,
) -> subprocess.CompletedProcess[bytes]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        close_fds=True,
        timeout=timeout_s,
    )


def _is_protected_or_unsafe_relpath(rel_path: str, size_bytes: int) -> str | None:
    parts = Path(rel_path).parts
    if not parts or rel_path.startswith("/") or ".." in parts:
        return f"unsafe path in release tree: {rel_path!r}"
    if parts[0] == "data":
        if rel_path == "data/.gitkeep" and size_bytes == 0:
            return None
        return f"protected path refused under data/: {rel_path!r}"
    if "__pycache__" in parts or rel_path.endswith((".pyc", ".pyo")):
        return f"bytecode artifact refused in release tree: {rel_path!r}"
    return None


def fetch_and_export_release(
    *,
    remote_url: str,
    trusted_remote: str,
    pinned_commit: str,
    current_commit: str | None,
    dest_release_dir: str | Path,
    scratch_git_dir: str | Path,
    release_id: str,
    ref: str = "refs/heads/main",
    max_pack_bytes: int = DEFAULT_MAX_PACK_BYTES,
    max_export_bytes: int = DEFAULT_MAX_EXPORT_BYTES,
) -> dict:
    """Fetch one ref into a private scratch bare repo, verify pin/ancestry/tree, and seal export."""
    if not release_id or "/" in release_id or release_id in (".", ".."):
        raise ValueError(f"invalid release_id: {release_id!r}")
    validate_remote_url(remote_url, trusted_remote=trusted_remote)

    dst = Path(dest_release_dir)
    scratch = Path(scratch_git_dir)
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"destination release directory already exists: {dst}")
    if scratch.exists() or scratch.is_symlink():
        raise FileExistsError(f"scratch git directory already exists: {scratch}")

    scratch.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.mkdir(scratch, 0o700)

    init_res = _run_git(["init", "--bare"], cwd=scratch)
    if init_res.returncode != 0:
        err = init_res.stderr.decode(errors="replace")
        raise ReleasePrepError(f"git init --bare failed: {err}")

    # Disable automatic unpacking, maintenance, and tag fetching in scratch repo (§1).
    for k, v in (
        ("gc.auto", "0"),
        ("fetch.unpackLimit", "1"),
        ("transfer.unpackLimit", "1"),
        ("maintenance.auto", "false"),
    ):
        _run_git(["config", k, v], cwd=scratch)

    fetch_res = _run_git(
        ["fetch", "--no-tags", remote_url, f"{ref}:refs/remotes/origin/target"],
        cwd=scratch,
    )
    if fetch_res.returncode != 0:
        raise ReleasePrepError(
            f"git fetch failed: {fetch_res.stderr.decode(errors='replace').strip()}"
        )

    pack_bytes = _dir_size_bytes(scratch / "objects")
    if pack_bytes > max_pack_bytes:
        raise PolicyRefusalError(
            f"fetched Git objects ({pack_bytes} bytes) exceed cap ({max_pack_bytes} bytes)"
        )

    rev_res = _run_git(["rev-parse", "refs/remotes/origin/target"], cwd=scratch)
    if rev_res.returncode != 0:
        raise PolicyRefusalError("fetched ref did not resolve to a commit")
    fetched_sha = rev_res.stdout.decode("utf-8").strip()
    if fetched_sha != pinned_commit:
        raise PolicyRefusalError(
            f"fetched ref commit {fetched_sha} does not match pinned commit {pinned_commit}"
        )

    if current_commit is not None:
        anc = _run_git(["merge-base", "--is-ancestor", current_commit, fetched_sha], cwd=scratch)
        if anc.returncode != 0:
            raise PolicyRefusalError(
                f"pinned commit {fetched_sha} does not descend from selected commit "
                f"{current_commit}"
            )

    # Inspect the full Git tree BEFORE writing a single file to dest_release_dir.
    ls_res = _run_git(["ls-tree", "-r", "-l", "--full-tree", "-z", fetched_sha], cwd=scratch)
    if ls_res.returncode != 0:
        raise ReleasePrepError(f"git ls-tree failed: {ls_res.stderr.decode(errors='replace')}")

    entries: list[tuple[str, str, int, str]] = []
    total_export = 0
    for raw_entry in ls_res.stdout.split(b"\x00"):
        if not raw_entry:
            continue
        meta_bytes, path_bytes = raw_entry.split(b"\t", 1)
        meta_parts = meta_bytes.decode("utf-8").split()
        mode_str, obj_type, blob_sha, size_str = (
            meta_parts[0],
            meta_parts[1],
            meta_parts[2],
            meta_parts[3],
        )
        rel_path = path_bytes.decode("utf-8")
        if mode_str == "120000":
            raise PolicyRefusalError(f"symlink refused in candidate release tree: {rel_path!r}")
        if mode_str == "160000" or obj_type != "blob":
            raise PolicyRefusalError(
                f"unsupported git tree entry ({mode_str} {obj_type}) refused: {rel_path!r}"
            )
        if mode_str not in ("100644", "100755"):
            raise PolicyRefusalError(f"unsupported file mode {mode_str} at {rel_path!r}")
        size_bytes = int(size_str)
        bad_reason = _is_protected_or_unsafe_relpath(rel_path, size_bytes)
        if bad_reason is not None:
            raise PolicyRefusalError(bad_reason)
        total_export += size_bytes
        if total_export > max_export_bytes:
            raise PolicyRefusalError(
                f"release export size ({total_export} bytes) exceeds cap ({max_export_bytes} bytes)"
            )
        entries.append((mode_str, blob_sha, size_bytes, rel_path))

    # Extract exclusively into dest_release_dir using O_CREAT|O_EXCL|O_NOFOLLOW and hash each file.
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.mkdir(dst, 0o700)
    created_dirs: set[Path] = {dst}
    created_files: list[tuple[Path, str]] = []
    manifest_files: dict[str, dict] = {}

    for mode_str, blob_sha, expected_size, rel_path in sorted(entries, key=lambda e: e[3]):
        blob_res = _run_git(["cat-file", "blob", blob_sha], cwd=scratch)
        if blob_res.returncode != 0:
            raise ReleasePrepError(f"git cat-file failed for {rel_path}")
        blob_bytes = blob_res.stdout
        if len(blob_bytes) != expected_size:
            raise ReleasePrepError(f"blob size mismatch while exporting {rel_path}")

        out_path = dst / rel_path
        out_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        cur = out_path.parent
        while cur != dst and cur.is_relative_to(dst):
            created_dirs.add(cur)
            cur = cur.parent

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(os.fspath(out_path), flags, 0o600)
        try:
            view = memoryview(blob_bytes)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        created_files.append((out_path, mode_str))
        manifest_files[rel_path] = {
            "sha256": hashlib.sha256(blob_bytes).hexdigest(),
            "size": len(blob_bytes),
            "mode": mode_str,
        }

    manifest_body = {
        "release_id": release_id,
        "commit": fetched_sha,
        "files": manifest_files,
    }
    canonical = json.dumps(manifest_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    manifest_body["manifest_digest"] = hashlib.sha256(canonical).hexdigest()

    manifest_path = dst / RELEASE_MANIFEST_FILENAME
    m_bytes = (json.dumps(manifest_body, indent=2, sort_keys=True) + "\n").encode("utf-8")
    m_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    m_fd = os.open(os.fspath(manifest_path), m_flags, 0o600)
    try:
        os.write(m_fd, m_bytes)
        os.fsync(m_fd)
    finally:
        os.close(m_fd)
    created_files.append((manifest_path, "100644"))

    # Seal release read-only and fsync directory tree bottom-up.
    for fpath, mode_str in created_files:
        os.chmod(fpath, 0o500 if mode_str == "100755" else 0o400)
    for dpath in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
        fsync_dir(dpath)
        os.chmod(dpath, 0o500)
    fsync_dir(dst.parent)
    return manifest_body


def verify_release_manifest_integrity(release_dir: str | Path) -> dict:
    """Re-verify every file hash and confirm zero bytecode or extra files in a sealed release."""
    root = Path(release_dir).resolve()
    manifest_path = root / RELEASE_MANIFEST_FILENAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ReleasePrepError(f"missing or symlinked release manifest in {root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_files: dict = manifest.get("files") or {}

    observed: set[str] = set()
    for cur_root, dirs, files in os.walk(root, followlinks=False):
        cur_path = Path(cur_root)
        for d in dirs:
            if d == "__pycache__":
                raise ReleasePrepError(f"unexpected bytecode directory in sealed release: {d}")
            if (cur_path / d).is_symlink():
                raise ReleasePrepError(f"unexpected symlink directory in sealed release: {d}")
        for name in files:
            fp = cur_path / name
            if fp == manifest_path:
                continue
            if fp.is_symlink():
                raise ReleasePrepError(f"unexpected symlink in sealed release: {name}")
            if name.endswith((".pyc", ".pyo")):
                raise ReleasePrepError(f"unexpected bytecode file in sealed release: {name}")
            rel = fp.relative_to(root).as_posix()
            observed.add(rel)
            if rel not in expected_files:
                raise ReleasePrepError(f"unexpected extra file in sealed release: {rel}")
            data = fp.read_bytes()
            actual_sha = hashlib.sha256(data).hexdigest()
            exp = expected_files[rel]
            if actual_sha != exp["sha256"] or len(data) != exp["size"]:
                raise ReleasePrepError(f"payload hash/size mismatch in sealed release: {rel}")

    missing = sorted(set(expected_files.keys()) - observed)
    if missing:
        raise ReleasePrepError(f"missing files in sealed release: {', '.join(missing[:5])}")

    check_body = {
        "release_id": manifest["release_id"],
        "commit": manifest["commit"],
        "files": expected_files,
    }
    canonical = json.dumps(check_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != manifest.get("manifest_digest"):
        raise ReleasePrepError("release manifest digest mismatch")
    return manifest


def _load_control_manifest_helper(control_dir: Path):
    mod_path = control_dir / "deploy" / "managed" / "control_manifest.py"
    spec = importlib.util.spec_from_file_location("_sigint101_ctrl_manifest_prep", mod_path)
    if spec is None or spec.loader is None:
        raise ReleasePrepError(f"cannot load control_manifest.py from {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def verify_candidate_contracts(
    *,
    control_dir: str | Path,
    baseline_control_manifest: dict,
    baseline_requirements_sha256: str,
    candidate_release_dir: str | Path,
) -> None:
    """Refuse candidate releases with deployed-control drift or requirements changes (§5, §7)."""
    ctrl_mod = _load_control_manifest_helper(Path(control_dir))
    cand_root = Path(candidate_release_dir).resolve()

    req_file = cand_root / "requirements.txt"
    if not req_file.is_file() or req_file.is_symlink():
        raise PolicyRefusalError("candidate release is missing requirements.txt")
    cand_req_sha = hashlib.sha256(req_file.read_bytes()).hexdigest()
    if cand_req_sha != baseline_requirements_sha256:
        raise PolicyRefusalError(
            "candidate requirements.txt drifted from sealed runtime baseline; "
            "course apply never runs pip"
        )

    try:
        cand_manifest = ctrl_mod.compute_control_manifest(cand_root)
    except FileNotFoundError as exc:
        raise PolicyRefusalError(f"deployed-control contract incomplete: {exc}") from exc

    drifts = ctrl_mod.compare_control_manifests(baseline_control_manifest, cand_manifest)
    if drifts:
        raise PolicyRefusalError(
            "deployed-control contract drift requires a managed control upgrade: "
            + "; ".join(drifts)
        )
