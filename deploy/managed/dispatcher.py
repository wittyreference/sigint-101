#!/usr/bin/env python3
# ABOUTME: Stable managed updater dispatcher installed under ROOT/bin/dispatcher.py.
# ABOUTME: Acquires shared or exclusive gate BEFORE reading control.json and retains it across exec.

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import stat
import sys
from pathlib import Path

EXIT_GATE_BUSY = 75
DEFAULT_ROOT_REL = Path(".local") / "state" / "sigint101-updater"
SHARED_GATE_FD_ENV = "SIGINT101_SHARED_GATE_FD"
MUTATION_GATE_FD_ENV = "SIGINT101_MUTATION_GATE_FD"


def _default_root() -> Path:
    env_root = os.environ.get("SIGINT101_UPDATER_ROOT")
    if env_root:
        return Path(env_root)
    return Path.home() / DEFAULT_ROOT_REL


def _open_trusted_dir(path: str | Path, *, dir_fd: int | None = None, label: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.fspath(path), flags, dir_fd=dir_fd)
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or (st.st_mode & 0o022):
        os.close(fd)
        raise RuntimeError(f"untrusted {label}")
    return fd


def _acquire_gate_fd(root: Path, *, exclusive: bool) -> int:
    root_fd = _open_trusted_dir(root, label="updater root")
    try:
        locks_fd = _open_trusted_dir("locks", dir_fd=root_fd, label="locks directory")
    finally:
        os.close(root_fd)

    flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open("mutation.lock", flags, dir_fd=locks_fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink > 1:
                raise RuntimeError("untrusted mutation.lock")
            op = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            fcntl.flock(fd, op)
            after = os.lstat("mutation.lock", dir_fd=locks_fd)
            if (after.st_dev, after.st_ino) != (st.st_dev, st.st_ino):
                raise RuntimeError("mutation.lock inode changed while locking")
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(locks_fd)
    return fd


def _read_control_id(root: Path) -> str:
    ctrl_file = root / "control.json"
    if not ctrl_file.is_file() or ctrl_file.is_symlink():
        raise RuntimeError("control.json is missing or symlinked")
    data = json.loads(ctrl_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format_version") != 1:
        raise RuntimeError("unsupported control.json format")
    ctrl_id = data.get("control_id")
    if not isinstance(ctrl_id, str) or not ctrl_id or "/" in ctrl_id or ctrl_id in (".", ".."):
        raise RuntimeError(f"invalid control_id in control.json: {ctrl_id!r}")
    return ctrl_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stable SIGINT 101 managed dispatcher.")
    parser.add_argument("--root", default=None, help="Updater ROOT directory.")
    parser.add_argument("command", choices=("serve",), help="Dispatch command.")
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _default_root().resolve()
    exclusive = args.command != "serve"

    raw_mut_fd = os.environ.pop(MUTATION_GATE_FD_ENV, None) if exclusive else None
    try:
        if raw_mut_fd is not None:
            gate_fd = int(raw_mut_fd)
            os.set_inheritable(gate_fd, False)
            st = os.fstat(gate_fd)
            exp = os.lstat(root / "locks" / "mutation.lock")
            if (st.st_dev, st.st_ino) != (exp.st_dev, exp.st_ino):
                raise RuntimeError("inherited mutation gate FD does not match mutation.lock")
        else:
            gate_fd = _acquire_gate_fd(root, exclusive=exclusive)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            print(
                "sigint101-dispatcher: busy — maintenance holds mutation gate; retry shortly",
                file=sys.stderr,
            )
            return EXIT_GATE_BUSY
        print(f"sigint101-dispatcher: cannot acquire gate: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"sigint101-dispatcher: {exc}", file=sys.stderr)
        return 1

    try:
        control_id = _read_control_id(root)
        control_dir = root / "control" / control_id
        if not control_dir.is_dir() or control_dir.is_symlink():
            raise RuntimeError(f"selected control generation missing: {control_id}")
        launcher_path = control_dir / "deploy" / "managed" / "launcher.py"
        if not launcher_path.is_file() or launcher_path.is_symlink():
            raise RuntimeError(f"control launcher missing: {launcher_path}")

        # Retain the exact gate descriptor across exec into the selected control generation.
        os.set_inheritable(gate_fd, True)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(control_dir)
        env[MUTATION_GATE_FD_ENV if exclusive else SHARED_GATE_FD_ENV] = str(gate_fd)
        exec_argv = [
            sys.executable,
            "-B",
            str(launcher_path),
            "--root",
            str(root),
            "--control-dir",
            str(control_dir),
            *args.extra,
        ]
        os.execve(sys.executable, exec_argv, env)
    except BaseException as exc:
        os.close(gate_fd)
        print(f"sigint101-dispatcher: startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
