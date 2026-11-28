#!/usr/bin/env python3
# ABOUTME: Stable service entrypoint exec'd by launcher.py with the exclusive writer lock FD.
# ABOUTME: Seals writer FD CLOEXEC, strips leaked env, isolates sealed runtime, and fences listen.

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import socket
import stat
import sys
import time
from pathlib import Path

WRITER_LOCK_FD_ENV = "SIGINT101_WRITER_LOCK_FD"
SERVED_MARKER_FILENAME = "served.json"
_STRIP_ENV_KEYS = (
    "PYTHONPATH",
    "SIGINT101_WRITER_LOCK_FD",
    "SIGINT101_SHARED_GATE_FD",
    "SIGINT101_MUTATION_GATE_FD",
    "SIGINT101_UPDATER_ROOT",
)


def _seal_inherited_writer_fd(
    writer_lock_path: Path | None,
    *,
    cli_fd: int | None = None,
) -> int:
    """Seal the exec-transferred writer FD close-on-exec, verify its inode, and re-assert lock."""
    raw = os.environ.pop(WRITER_LOCK_FD_ENV, None)
    if cli_fd is not None:
        fd = cli_fd
    elif raw is not None:
        fd = int(raw)
    else:
        raise RuntimeError(f"--writer-lock-fd or {WRITER_LOCK_FD_ENV} is required")
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        raise RuntimeError("inherited writer lock descriptor is not a trusted regular file")
    os.set_inheritable(fd, False)
    if writer_lock_path is not None:
        exp = os.lstat(writer_lock_path)
        if (st.st_dev, st.st_ino) != (exp.st_dev, exp.st_ino):
            raise RuntimeError("inherited writer lock FD does not match ROOT/locks/writer.lock")
    # F12: idempotently re-assert LOCK_EX|LOCK_NB to confirm this FD actually holds the writer lock.
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise RuntimeError(
                "inherited writer lock FD does not hold exclusive writer.lock"
            ) from exc
        raise
    return fd


def _clean_service_environ() -> None:
    """Remove control-dispatch env vars so neither service nor children inherit them (F5)."""
    for key in _STRIP_ENV_KEYS:
        os.environ.pop(key, None)


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
    """Write/fsync or re-fsync served.json AFTER public startup succeeds and BEFORE listen()."""
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
    """Bind the public address WITHOUT calling listen() yet; uvicorn listens after lifespan."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.set_inheritable(False)
    return sock


class _FencedASGIApp:
    """ASGI wrapper that writes served.json AFTER lifespan.startup.complete and BEFORE listen()."""

    def __init__(self, inner, *, on_startup_complete):
        self._inner = inner
        self._on_startup_complete = on_startup_complete

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            await self._inner(scope, receive, send)
            return

        async def fenced_send(message):
            if message.get("type") == "lifespan.startup.complete":
                self._on_startup_complete()
            await send(message)

        await self._inner(scope, receive, fenced_send)


def _assert_module_under(mod, expected_root: Path, label: str) -> None:
    mod_file = getattr(mod, "__file__", None)
    if not mod_file:
        raise RuntimeError(f"imported {label} has no backing file")
    resolved = Path(mod_file).resolve()
    if not resolved.is_relative_to(expected_root):
        raise RuntimeError(
            f"{label} resolved outside expected {expected_root}: {resolved}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SIGINT 101 managed/legacy service entry.")
    parser.add_argument("--mode", choices=("managed", "legacy"), required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--writer-lock-path", default=None)
    parser.add_argument("--writer-lock-fd", type=int, default=None)
    parser.add_argument("--release-root", default=None)
    parser.add_argument("--runtime-dir", default=None)
    parser.add_argument("--release-id", default=None)
    parser.add_argument("--generation-id", default=None)
    parser.add_argument("--operations-dir", default=None)
    parser.add_argument("--checkout-dir", default=None)
    args = parser.parse_args(argv)

    # CRITICAL (§2, F5, F12): seal writer FD close-on-exec & clean env BEFORE any app imports!
    lock_path = Path(args.writer_lock_path).resolve() if args.writer_lock_path else None
    _writer_fd = _seal_inherited_writer_fd(lock_path, cli_fd=args.writer_lock_fd)
    _clean_service_environ()

    sock: socket.socket | None = None
    try:
        if args.mode == "managed":
            release_root = Path(args.release_root).resolve()
            runtime_sp = (Path(args.runtime_dir) / "site-packages").resolve()
            data_dir = Path(args.data_dir).resolve()
            operations_dir = Path(args.operations_dir).resolve()

            # F3: launched with -I -S (stdlib only on sys.path); add ONLY release + sealed runtime.
            sys.path[:0] = [str(release_root), str(runtime_sp)]
            import fastapi
            import pydantic_core
            import uvicorn

            import sigint101.app as sigint101_app

            _assert_module_under(uvicorn, runtime_sp, "uvicorn")
            _assert_module_under(fastapi, runtime_sp, "fastapi")
            _assert_module_under(pydantic_core, runtime_sp, "pydantic_core")
            _assert_module_under(sigint101_app, release_root, "sigint101.app")

            # F1: bind WITHOUT calling listen(); uvicorn runs lifespan.startup() first, then
            # _FencedASGIApp writes/fsyncs served.json, then uvicorn calls loop.create_server
            # (which calls sock.listen()). A failed startup never writes served.json!
            sock = _bind_reserved_socket(args.host, args.port)
            raw_app = sigint101_app.create_app(
                pack_dir=release_root / "lesson_packs",
                data_dir=data_dir,
                managed=True,
            )
            fenced_app = _FencedASGIApp(
                raw_app,
                on_startup_complete=lambda: _fence_serving_marker(
                    operations_dir=operations_dir,
                    operation_id=args.operation_id,
                    release_id=str(args.release_id),
                    generation_id=str(args.generation_id),
                ),
            )
            server = uvicorn.Server(
                uvicorn.Config(fenced_app, host=args.host, port=args.port, log_level="warning")
            )
            server.run(sockets=[sock])
        else:
            # F2: run the original legacy application as its own unit did (sigint101.app:app,
            # cwd at checkout, no managed= kwarg so pre-PR-1 checkouts boot cleanly).
            checkout_dir = Path(args.checkout_dir).resolve()
            data_dir = Path(args.data_dir).resolve()
            expected_legacy_data = (checkout_dir / "data").resolve()
            if data_dir != expected_legacy_data:
                raise RuntimeError(
                    f"legacy data_dir {data_dir} must match checkout data dir "
                    f"{expected_legacy_data}"
                )
            os.chdir(checkout_dir)
            sys.path.insert(0, str(checkout_dir))
            import uvicorn

            import sigint101.app as legacy_app_mod

            sock = _bind_reserved_socket(args.host, args.port)
            server = uvicorn.Server(
                uvicorn.Config(
                    legacy_app_mod.app, host=args.host, port=args.port, log_level="warning"
                )
            )
            server.run(sockets=[sock])
    finally:
        if sock is not None:
            sock.close()
        os.close(_writer_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
