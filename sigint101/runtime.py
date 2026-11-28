# ABOUTME: Sealed dependency runtime staging, ABI compatibility (U3), and import verification.
# ABOUTME: Excludes bytecode (U8), rejects editable/checkout leaks, and accepts same-ABI updates.

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path

from .snapshot import MountIdReader, read_fd_mount_id
from .storage import fsync_dir

RUNTIME_MANIFEST_FILENAME = "runtime.json"
RUNTIME_FORMAT_VERSION = 1
_COPY_CHUNK = 65536
_BYTECODE_SUFFIXES = (".pyc", ".pyo")


class RuntimeCompatibilityError(RuntimeError):
    """Raised when a Python ABI mismatches, a runtime is tampered, or an editable leak exists."""


def probe_python_abi(python_executable: str | Path = sys.executable) -> dict:
    """Probe Python major.minor and SOABI without hashing the host binary or stdlib (U3)."""
    exe = Path(python_executable).resolve()
    if exe == Path(sys.executable).resolve():
        return {
            "major": sys.version_info.major,
            "minor": sys.version_info.minor,
            "patch": sys.version_info.micro,
            "major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
            "soabi": sysconfig.get_config_var("SOABI") or "",
            "implementation": sys.implementation.name,
            "platform_machine": platform.machine(),
        }
    snippet = (
        "import json, platform, sys, sysconfig; "
        "print(json.dumps({"
        "'major': sys.version_info.major, "
        "'minor': sys.version_info.minor, "
        "'patch': sys.version_info.micro, "
        "'major_minor': f'{sys.version_info.major}.{sys.version_info.minor}', "
        "'soabi': sysconfig.get_config_var('SOABI') or '', "
        "'implementation': sys.implementation.name, "
        "'platform_machine': platform.machine()"
        "}))"
    )
    proc = subprocess.run(
        [str(python_executable), "-B", "-I", "-c", snippet],
        capture_output=True,
        text=True,
        close_fds=True,
        timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeCompatibilityError(
            f"cannot probe Python ABI for {python_executable}: {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeCompatibilityError(f"invalid ABI probe output: {exc}") from exc


def _check_not_editable(
    rel_path: str,
    payload: bytes,
    *,
    forbidden_checkout_root: Path | None,
) -> None:
    name = rel_path.rsplit("/", 1)[-1]
    if name.endswith(".egg-link"):
        raise RuntimeCompatibilityError(
            f"editable .egg-link install refused in sealed runtime: {rel_path}"
        )
    if name == "direct_url.json":
        try:
            meta = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            meta = {}
        if isinstance(meta, dict):
            dir_info = meta.get("dir_info")
            if isinstance(dir_info, dict) and dir_info.get("editable") is True:
                raise RuntimeCompatibilityError(
                    f"editable direct_url.json install refused in sealed runtime: {rel_path}"
                )
    if name.endswith(".pth"):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeCompatibilityError(f"unreadable .pth file: {rel_path}") from exc
        resolved_checkout = (
            forbidden_checkout_root.resolve() if forbidden_checkout_root is not None else None
        )
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("import ") or line.startswith("import\t"):
                raise RuntimeCompatibilityError(
                    f"executable .pth hook refused in sealed runtime: {rel_path}"
                )
            p = Path(line)
            if p.is_absolute() or ".." in p.parts:
                raise RuntimeCompatibilityError(
                    f"editable or external path in .pth refused ({rel_path}): {line}"
                )
            if resolved_checkout is not None and p.resolve().is_relative_to(resolved_checkout):
                raise RuntimeCompatibilityError(
                    f".pth resolves back into legacy checkout ({rel_path}): {line}"
                )


def _hash_manifest_files(files: dict[str, dict]) -> str:
    canonical = json.dumps(files, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def stage_sealed_runtime(
    source_site_packages: str | Path,
    dest_runtime_dir: str | Path,
    *,
    runtime_id: str,
    python_abi: dict | None = None,
    forbidden_checkout_root: str | Path | None = None,
    mount_id_reader: MountIdReader | None = None,
) -> dict:
    """Copy site-packages into an exclusive read-only runtime snapshot, excluding bytecode."""
    if not runtime_id or "/" in runtime_id or runtime_id in (".", ".."):
        raise ValueError(f"invalid runtime_id: {runtime_id!r}")
    src_sp = Path(source_site_packages)
    dst_rt = Path(dest_runtime_dir)
    if dst_rt.exists() or dst_rt.is_symlink():
        raise FileExistsError(f"destination runtime already exists: {dst_rt}")

    abi = python_abi if python_abi is not None else probe_python_abi()
    forbidden = Path(forbidden_checkout_root).resolve() if forbidden_checkout_root else None
    reader = mount_id_reader or read_fd_mount_id

    # Open source site-packages with O_NOFOLLOW to reject symlinks.
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        src_root_fd = os.open(os.fspath(src_sp), dir_flags)
    except OSError as exc:
        raise RuntimeCompatibilityError(f"cannot open source site-packages: {exc}") from exc

    files_inventory: dict[str, dict] = {}
    created_dirs: list[Path] = []
    created_files: list[Path] = []
    dst_sp = dst_rt / "site-packages"

    try:
        src_mnt = reader(src_root_fd, ".")
        dst_rt.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(dst_rt, 0o700)
        os.mkdir(dst_sp, 0o700)
        created_dirs.extend([dst_rt, dst_sp])

        def copy_dir(src_fd: int, dst_dir: Path, prefix: str) -> None:
            for name in sorted(os.listdir(src_fd)):
                if name == "__pycache__" or name.endswith(_BYTECODE_SUFFIXES):
                    continue
                rel = f"{prefix}/{name}" if prefix else name
                lst = os.lstat(name, dir_fd=src_fd)
                mode = lst.st_mode
                if stat.S_ISLNK(mode):
                    raise RuntimeCompatibilityError(
                        f"symlink refused in site-packages: {rel}"
                    )
                if stat.S_ISDIR(mode):
                    child_src_fd = os.open(name, dir_flags, dir_fd=src_fd)
                    try:
                        if reader(child_src_fd, rel) != src_mnt:
                            raise RuntimeCompatibilityError(
                                f"mount boundary crossed in site-packages: {rel}"
                            )
                        child_dst = dst_dir / name
                        os.mkdir(child_dst, 0o700)
                        created_dirs.append(child_dst)
                        copy_dir(child_src_fd, child_dst, rel)
                    finally:
                        os.close(child_src_fd)
                elif stat.S_ISREG(mode):
                    f_flags = (
                        os.O_RDONLY
                        | os.O_NOFOLLOW
                        | getattr(os, "O_NONBLOCK", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                    )
                    in_fd = os.open(name, f_flags, dir_fd=src_fd)
                    try:
                        st = os.fstat(in_fd)
                        if not stat.S_ISREG(st.st_mode):
                            raise RuntimeCompatibilityError(f"special file refused: {rel}")
                        if reader(in_fd, rel) != src_mnt:
                            raise RuntimeCompatibilityError(
                                f"mount boundary crossed in site-packages: {rel}"
                            )
                        chunks: list[bytes] = []
                        while True:
                            chunk = os.read(in_fd, _COPY_CHUNK)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        payload = b"".join(chunks)
                    finally:
                        os.close(in_fd)

                    _check_not_editable(rel, payload, forbidden_checkout_root=forbidden)
                    out_path = dst_dir / name
                    out_flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0)
                    )
                    out_fd = os.open(os.fspath(out_path), out_flags, 0o600)
                    try:
                        view = memoryview(payload)
                        written = 0
                        while written < len(view):
                            written += os.write(out_fd, view[written:])
                        os.fsync(out_fd)
                    finally:
                        os.close(out_fd)
                    created_files.append(out_path)
                    files_inventory[rel] = {
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                    }
                else:
                    raise RuntimeCompatibilityError(
                        f"non-regular file refused in site-packages: {rel}"
                    )

        copy_dir(src_root_fd, dst_sp, "")
    finally:
        os.close(src_root_fd)

    manifest = {
        "format_version": RUNTIME_FORMAT_VERSION,
        "runtime_id": runtime_id,
        "python_major_minor": str(abi["major_minor"]),
        "soabi": str(abi["soabi"]),
        "implementation": str(abi.get("implementation", "cpython")),
        "platform_machine": str(abi.get("platform_machine", platform.machine())),
        "manifest_digest": _hash_manifest_files(files_inventory),
        "files": files_inventory,
    }
    manifest_path = dst_rt / RUNTIME_MANIFEST_FILENAME
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    m_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    m_fd = os.open(os.fspath(manifest_path), m_flags, 0o600)
    try:
        os.write(m_fd, manifest_bytes)
        os.fsync(m_fd)
    finally:
        os.close(m_fd)
    created_files.append(manifest_path)

    # Seal read-only (0444 files, 0555 directories) and fsync bottom-up.
    for fpath in created_files:
        os.chmod(fpath, 0o444)
    for dpath in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
        fsync_dir(dpath)
        os.chmod(dpath, 0o555)
    fsync_dir(dst_rt.parent)

    return manifest


def verify_runtime_compatibility(
    runtime_dir: str | Path,
    *,
    current_abi: dict | None = None,
    verify_files: bool = True,
    forbidden_checkout_root: str | Path | None = None,
) -> dict:
    """Verify sealed runtime ABI (major.minor + SOABI) and optional byte-stable manifest."""
    rt_path = Path(runtime_dir)
    manifest_path = rt_path / RUNTIME_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise RuntimeCompatibilityError(f"runtime manifest missing: {manifest_path.name}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeCompatibilityError(f"cannot read runtime manifest: {exc}") from exc

    if manifest.get("format_version") != RUNTIME_FORMAT_VERSION:
        raise RuntimeCompatibilityError(
            f"unsupported runtime manifest format_version: {manifest.get('format_version')!r}"
        )

    abi = current_abi if current_abi is not None else probe_python_abi()
    exp_mm = manifest.get("python_major_minor")
    act_mm = str(abi.get("major_minor", ""))
    if act_mm != exp_mm:
        raise RuntimeCompatibilityError(
            f"Python ABI major.minor mismatch: host is {act_mm!r}, runtime requires {exp_mm!r}"
        )
    exp_soabi = manifest.get("soabi")
    act_soabi = str(abi.get("soabi", ""))
    if act_soabi != exp_soabi:
        raise RuntimeCompatibilityError(
            f"Python SOABI mismatch: host is {act_soabi!r}, runtime requires {exp_soabi!r}"
        )

    expected_files = manifest.get("files")
    if not isinstance(expected_files, dict):
        raise RuntimeCompatibilityError("runtime manifest missing files table")
    if _hash_manifest_files(expected_files) != manifest.get("manifest_digest"):
        raise RuntimeCompatibilityError("runtime manifest digest mismatch")

    if verify_files:
        sp_path = rt_path / "site-packages"
        if not sp_path.is_dir() or sp_path.is_symlink():
            raise RuntimeCompatibilityError(
                "sealed site-packages directory is missing or symlinked"
            )
        forbidden = Path(forbidden_checkout_root).resolve() if forbidden_checkout_root else None
        actual_files: dict[str, dict] = {}
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

        def scan(fd: int, prefix: str) -> None:
            for name in sorted(os.listdir(fd)):
                rel = f"{prefix}/{name}" if prefix else name
                if name == "__pycache__" or name.endswith(_BYTECODE_SUFFIXES):
                    raise RuntimeCompatibilityError(
                        f"unexpected bytecode artifact in sealed runtime: {rel}"
                    )
                lst = os.lstat(name, dir_fd=fd)
                if stat.S_ISLNK(lst.st_mode):
                    raise RuntimeCompatibilityError(f"symlink in sealed runtime: {rel}")
                if stat.S_ISDIR(lst.st_mode):
                    child_fd = os.open(name, dir_flags, dir_fd=fd)
                    try:
                        scan(child_fd, rel)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(lst.st_mode):
                    f_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                    f_fd = os.open(name, f_flags, dir_fd=fd)
                    try:
                        chunks = []
                        while True:
                            c = os.read(f_fd, _COPY_CHUNK)
                            if not c:
                                break
                            chunks.append(c)
                        payload = b"".join(chunks)
                    finally:
                        os.close(f_fd)
                    _check_not_editable(rel, payload, forbidden_checkout_root=forbidden)
                    actual_files[rel] = {
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                    }
                else:
                    raise RuntimeCompatibilityError(f"non-regular file in sealed runtime: {rel}")

        root_sp_fd = os.open(os.fspath(sp_path), dir_flags)
        try:
            scan(root_sp_fd, "")
        finally:
            os.close(root_sp_fd)

        if actual_files != expected_files:
            raise RuntimeCompatibilityError("sealed runtime files drifted from runtime.json")

    return manifest


def check_runtime_imports(
    python_executable: str | Path,
    runtime_dir: str | Path,
    *,
    release_root: str | Path,
    modules: tuple[str, ...] = ("sigint101",),
    forbidden_checkout_root: str | Path | None = None,
) -> dict[str, str]:
    """Smoke-test imports under -B -I using only release_root and sealed site-packages."""
    rel = Path(release_root).resolve()
    sp = (Path(runtime_dir) / "site-packages").resolve()
    forbidden = Path(forbidden_checkout_root).resolve() if forbidden_checkout_root else None
    snippet = (
        "import importlib, json, pathlib, sys\n"
        f"sys.path[:0] = [{str(rel)!r}, {str(sp)!r}]\n"
        f"mods = {list(modules)!r}\n"
        "out = {}\n"
        "for name in mods:\n"
        "    m = importlib.import_module(name)\n"
        "    f = getattr(m, '__file__', None)\n"
        "    out[name] = str(pathlib.Path(f).resolve()) if f else ''\n"
        "print(json.dumps(out))\n"
    )
    proc = subprocess.run(
        [str(python_executable), "-B", "-I", "-c", snippet],
        capture_output=True,
        text=True,
        close_fds=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeCompatibilityError(
            f"runtime import smoke-test failed: {proc.stderr.strip()}"
        )
    resolved = json.loads(proc.stdout)
    for mod_name, mod_path in resolved.items():
        if not mod_path:
            raise RuntimeCompatibilityError(f"imported module {mod_name!r} has no backing file")
        p = Path(mod_path).resolve()
        if forbidden is not None and p.is_relative_to(forbidden):
            raise RuntimeCompatibilityError(
                f"module {mod_name!r} resolved back into legacy checkout: {p}"
            )
        if mod_name == "sigint101" and not p.is_relative_to(rel):
            raise RuntimeCompatibilityError(
                f"sigint101 resolved outside candidate release: {p}"
            )
    return resolved
