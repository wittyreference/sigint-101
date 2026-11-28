#!/usr/bin/env python3
# ABOUTME: Stable service entrypoint exec'd by launcher.py with the exclusive writer lock FD.
# ABOUTME: Marks writer FD close-on-exec before imports, binds + fences served.json, then listens.

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import sys
import time
from pathlib import Path

WRITER_LOCK_FD_ENV = "SIGINT101_WRITER_LOCK_FD"
SERVED_MARKER_FILENAME = "served.json"


def _seal_inherited_writer_fd() -> int:
    """Mark the exec-transferred writer lock FD close-on-exec BEFORE importing app code."""
    raw = os.environ.pop(WRITER_LOCK_FD_ENV, None)
    if raw is None:
        raise RuntimeError(f"{WRITER_LOCK_FD_ENV} is required")
    fd = int(raw)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        raise RuntimeError("inherited writer lock descriptor is not a trusted regular file")
    os.set_inheritable(fd, False)
    return fd


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(os.fspath(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fence_serving_marker(
    *,
    operations_dir: Path,
    operation_id: str,
    release_id: str,
    generation_id: str,
) -> Path:
    """Write/fsync or re-fsync the operation-and-pair-specific serving marker before listen()."""
    operations_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker_path = operations_dir / SERVED_MARKER_FILENAME

    if marker_path.exists() or marker_path.is_symlink():
        if marker_path.is_symlink() or not marker_path.is_file():
            raise RuntimeError("existing serving marker is not a regular file")
        existing = json.loads(marker_path.read_text(encoding="utf-8"))
        if (
            not isinstance(existing, dict)
            or existing.get("operation_id") != operation_id
            or existing.get("release_id") != release_id
            or existing.get("generation_id") != generation_id
        ):
            raise RuntimeError("existing serving marker does not match selected operation/pair")
        # Re-fsync visible marker and parent directory before authorizing listen (§5).
        fd = os.open(os.fspath(marker_path), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(operations_dir)
        return marker_path

    payload = {
        "format_version": 1,
        "operation_id": operation_id,
        "release_id": release_id,
        "generation_id": generation_id,
        "fenced_at": time.time(),
    }
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp = operations_dir / f".{SERVED_MARKER_FILENAME}.tmp.{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.fspath(tmp), flags, 0o600)
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
    os.replace(tmp, marker_path)
    _fsync_dir(operations_dir)
    return marker_path


def _bind_reserved_socket(host: str, port: int) -> socket.socket:
    """Bind the public address WITHOUT calling listen() yet so bind errors stay recoverable."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.set_inheritable(False)
    return sock


def main(argv: list[str] | None = None) -> int:
    # CRITICAL (§2): mark writer lock FD close-on-exec BEFORE any app or third-party import!
    _writer_fd = _seal_inherited_writer_fd()

    parser = argparse.ArgumentParser(description="SIGINT 101 managed/legacy service entry.")
    parser.add_argument("--mode", choices=("managed", "legacy"), required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--release-root", default=None)
    parser.add_argument("--runtime-dir", default=None)
    parser.add_argument("--release-id", default=None)
    parser.add_argument("--generation-id", default=None)
    parser.add_argument("--operations-dir", default=None)
    parser.add_argument("--checkout-dir", default=None)
    args = parser.parse_args(argv)

    sock: socket.socket | None = None
    try:
        if args.mode == "managed":
            release_root = Path(args.release_root).resolve()
            runtime_sp = (Path(args.runtime_dir) / "site-packages").resolve()
            data_dir = Path(args.data_dir).resolve()
            operations_dir = Path(args.operations_dir).resolve()

            # Prepend immutable release and sealed runtime before importing sigint101.
            sys.path[:0] = [str(release_root), str(runtime_sp)]
            import uvicorn

            from sigint101.app import create_app

            # Bind without listening first so bind failures occur before writing served.json.
            sock = _bind_reserved_socket(args.host, args.port)
            app = create_app(
                pack_dir=release_root / "lesson_packs",
                data_dir=data_dir,
                managed=True,
            )
            # Write & fsync the serving marker BEFORE calling listen()!
            _fence_serving_marker(
                operations_dir=operations_dir,
                operation_id=args.operation_id,
                release_id=str(args.release_id),
                generation_id=str(args.generation_id),
            )
            sock.listen(128)
            server = uvicorn.Server(
                uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
            )
            server.run(sockets=[sock])
        else:
            checkout_dir = Path(args.checkout_dir).resolve()
            data_dir = Path(args.data_dir).resolve()
            sys.path.insert(0, str(checkout_dir))
            import uvicorn

            from sigint101.app import create_app

            sock = _bind_reserved_socket(args.host, args.port)
            app = create_app(
                pack_dir=checkout_dir / "lesson_packs",
                data_dir=data_dir,
                managed=False,
            )
            sock.listen(128)
            server = uvicorn.Server(
                uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
            )
            server.run(sockets=[sock])
    finally:
        if sock is not None:
            sock.close()
        os.close(_writer_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
