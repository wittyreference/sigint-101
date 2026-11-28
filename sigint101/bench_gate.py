# ABOUTME: PR 4 root-owned disposable-bench gate refusing all updater mutations on non-bench hosts.
# ABOUTME: Binds marker to host identity and canonical updater ROOT with no CLI or env bypass.

from __future__ import annotations

import errno
import json
import os
import socket
import stat
from pathlib import Path

DEFAULT_BENCH_MARKER_PATH = Path("/etc/sigint101/bench-updater-allow.json")
BENCH_MARKER_FORMAT_VERSION = 1
BENCH_MARKER_PURPOSE = "sigint101-pr4-disposable-bench"


class BenchGateError(PermissionError):
    """Raised when a PR 4 mutating entry is invoked without a valid root-owned bench marker."""


def _read_host_machine_id() -> str | None:
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    return None


def require_bench_marker(
    updater_root: str | Path,
    *,
    _marker_path: Path | None = None,
    _expected_uid: int = 0,
    _hostname: str | None = None,
    _machine_id: str | None = None,
) -> dict:
    """Verify the root-owned local bench marker BEFORE any directory, lock, file, or service call.

    Production callers must invoke `require_bench_marker(updater_root)` with no private overrides.
    Environment variables and CLI flags are intentionally never consulted, so neither the artifact,
    CLI, nor environment can create or bypass this gate.
    """
    marker_path = _marker_path if _marker_path is not None else DEFAULT_BENCH_MARKER_PATH
    resolved_root = Path(updater_root).resolve()
    parent = marker_path.parent

    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        dir_fd = os.open(os.fspath(parent), dir_flags)
    except OSError as exc:
        raise BenchGateError(
            f"non-bench host refused: bench marker directory unavailable ({exc.strerror or exc})"
        ) from exc

    try:
        dir_st = os.fstat(dir_fd)
        if not stat.S_ISDIR(dir_st.st_mode):
            raise BenchGateError("non-bench host refused: bench marker parent is not a directory")
        if dir_st.st_uid != _expected_uid:
            raise BenchGateError(
                f"non-bench host refused: bench marker directory owner uid {dir_st.st_uid} "
                f"!= required root uid {_expected_uid}"
            )
        if dir_st.st_mode & 0o022:
            raise BenchGateError(
                "non-bench host refused: bench marker directory has unsafe group/world permissions"
            )

        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(marker_path.name, file_flags, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise BenchGateError(
                    "non-bench host refused: bench marker must be a regular file, not a symlink"
                ) from exc
            raise BenchGateError(
                "non-bench host refused: root-owned bench marker is missing; "
                "mutations stay disabled until rollback ships"
            ) from exc

        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise BenchGateError("non-bench host refused: bench marker is not a regular file")
            if st.st_uid != _expected_uid:
                raise BenchGateError(
                    f"non-bench host refused: bench marker owner uid {st.st_uid} "
                    f"!= required root uid {_expected_uid}"
                )
            if st.st_mode & 0o022:
                raise BenchGateError(
                    "non-bench host refused: bench marker has unsafe group/world write permissions"
                )
            if st.st_nlink > 1:
                raise BenchGateError("non-bench host refused: bench marker must not be hardlinked")

            raw = os.read(fd, 16384)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BenchGateError(f"non-bench host refused: bench marker is malformed: {exc}") from exc

    if not isinstance(data, dict):
        raise BenchGateError("non-bench host refused: bench marker must be a JSON object")
    if data.get("format_version") != BENCH_MARKER_FORMAT_VERSION:
        raise BenchGateError("non-bench host refused: unsupported bench marker format_version")
    if data.get("purpose") != BENCH_MARKER_PURPOSE:
        raise BenchGateError("non-bench host refused: invalid bench marker purpose")

    expected_host = _hostname if _hostname is not None else socket.gethostname()
    if data.get("hostname") != expected_host:
        raise BenchGateError(
            f"non-bench host refused: bench marker hostname {data.get('hostname')!r} "
            f"does not match host {expected_host!r}"
        )

    expected_mid = _machine_id if _machine_id is not None else _read_host_machine_id()
    marker_mid = data.get("machine_id")
    if expected_mid is not None and marker_mid != expected_mid:
        raise BenchGateError("non-bench host refused: bench marker machine_id does not match host")

    marker_root = data.get("updater_root")
    if not isinstance(marker_root, str) or Path(marker_root).resolve() != resolved_root:
        raise BenchGateError(
            f"non-bench host refused: bench marker updater_root {marker_root!r} "
            f"does not match canonical root {str(resolved_root)!r}"
        )

    return data
