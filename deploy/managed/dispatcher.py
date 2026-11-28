#!/usr/bin/env python3
# ABOUTME: Stable managed updater dispatcher installed under ROOT/bin/dispatcher.py.
# ABOUTME: Acquires shared or exclusive gate BEFORE reading control.json and retains it across exec.

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import socket
import stat
import sys
from pathlib import Path

EXIT_GATE_BUSY = 75
EXIT_POLICY_REFUSED = 11
DEFAULT_ROOT_REL = Path(".local") / "state" / "sigint101-updater"
DEFAULT_BENCH_MARKER_PATH = Path("/etc/sigint101/bench-updater-allow.json")
SHARED_GATE_FD_ENV = "SIGINT101_SHARED_GATE_FD"
MUTATION_GATE_FD_ENV = "SIGINT101_MUTATION_GATE_FD"
DISPATCH_COMMANDS = ("serve", "provision", "apply", "enroll", "recover", "check", "status")


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


def _verify_bench_marker(root: Path) -> None:
    """Verify the root-owned local bench marker BEFORE locks or candidate code (PR 4 §9.4)."""
    marker = DEFAULT_BENCH_MARKER_PATH
    resolved_root = root.resolve()
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        dfd = os.open(os.fspath(marker.parent), dir_flags)
    except OSError as exc:
        raise RuntimeError(
            "non-bench host refused: root-owned bench marker unavailable"
        ) from exc
    try:
        dst = os.fstat(dfd)
        if not stat.S_ISDIR(dst.st_mode) or dst.st_uid != 0 or (dst.st_mode & 0o022):
            raise RuntimeError("non-bench host refused: untrusted bench marker directory")
        fflags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(marker.name, fflags, dir_fd=dfd)
        except OSError as exc:
            raise RuntimeError(
                "non-bench host refused: root-owned bench marker missing"
            ) from exc
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != 0
                or (st.st_mode & 0o022)
                or st.st_nlink > 1
            ):
                raise RuntimeError("non-bench host refused: untrusted bench marker file")
            raw = os.read(fd, 16384)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("non-bench host refused: malformed bench marker") from exc
    if (
        not isinstance(data, dict)
        or data.get("format_version") != 1
        or data.get("purpose") != "sigint101-pr4-disposable-bench"
        or data.get("hostname") != socket.gethostname()
        or not isinstance(data.get("updater_root"), str)
        or Path(data["updater_root"]).resolve() != resolved_root
    ):
        raise RuntimeError("non-bench host refused: bench marker does not match this host/root")


def _is_readonly_dispatch(command: str, extra: list[str]) -> bool:
    if command in ("check", "status"):
        return True
    if command == "provision" and ("--check" in extra or "--status" in extra):
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stable SIGINT 101 managed dispatcher.")
    parser.add_argument("--root", default=None, help="Updater ROOT directory.")
    parser.add_argument("command", choices=DISPATCH_COMMANDS, help="Dispatch command.")
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else _default_root().resolve()
    read_only = _is_readonly_dispatch(args.command, list(args.extra))
    exclusive = (args.command != "serve") and not read_only

    # PR 4 §9.4: refuse mutating dispatches on non-bench hosts BEFORE touching locks or code.
    if exclusive:
        try:
            _verify_bench_marker(root)
        except RuntimeError as exc:
            raw_mut = os.environ.pop(MUTATION_GATE_FD_ENV, None)
            if raw_mut is not None:
                try:
                    os.close(int(raw_mut))
                except (ValueError, OSError):
                    pass
            print(f"sigint101-dispatcher: {exc}", file=sys.stderr)
            return EXIT_POLICY_REFUSED

    gate_fd: int | None = None
    if not read_only:
        raw_mut_fd = os.environ.pop(MUTATION_GATE_FD_ENV, None) if exclusive else None
        try:
            if raw_mut_fd is not None:
                gate_fd = int(raw_mut_fd)
                os.set_inheritable(gate_fd, False)
                st = os.fstat(gate_fd)
                exp = os.lstat(root / "locks" / "mutation.lock")
                if (st.st_dev, st.st_ino) != (exp.st_dev, exp.st_ino):
                    raise RuntimeError("inherited mutation gate FD does not match mutation.lock")
                # F8: idempotently re-assert LOCK_EX|LOCK_NB to confirm the lock is actually held.
                fcntl.flock(gate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
    else:
        raw_mut = os.environ.pop(MUTATION_GATE_FD_ENV, None)
        if raw_mut is not None:
            try:
                os.close(int(raw_mut))
            except (ValueError, OSError):
                pass

    try:
        control_id = _read_control_id(root)
        control_dir = root / "control" / control_id
        if not control_dir.is_dir() or control_dir.is_symlink():
            raise RuntimeError(f"selected control generation missing: {control_id}")

        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(control_dir)
        if gate_fd is not None:
            os.set_inheritable(gate_fd, True)
            env[MUTATION_GATE_FD_ENV if exclusive else SHARED_GATE_FD_ENV] = str(gate_fd)

        if args.command == "serve":
            launcher_path = control_dir / "deploy" / "managed" / "launcher.py"
            if not launcher_path.is_file() or launcher_path.is_symlink():
                raise RuntimeError(f"control launcher missing: {launcher_path}")
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
        else:
            cli_args = (
                ["provision", *args.extra]
                if args.command == "provision"
                else [f"--{args.command}", *args.extra]
            )
            exec_argv = [
                sys.executable,
                "-B",
                "-m",
                "sigint101.updater_cli",
                "--root",
                str(root),
                *cli_args,
            ]
        os.execve(sys.executable, exec_argv, env)
    except BaseException as exc:
        if gate_fd is not None:
            os.close(gate_fd)
        print(f"sigint101-dispatcher: startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
