#!/usr/bin/env python3
# ABOUTME: Stable managed updater dispatcher installed under ROOT/bin/dispatcher.py.
# ABOUTME: Acquires shared or exclusive gate BEFORE reading control.json and retains it across exec.

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
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
_CLI_PASSTHROUGH_ENV = ("PATH", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG")

# Trusted control-generation contract verified from outside the generation before every exec (F1).
CONTROL_MANIFEST_FILENAME = "control_manifest.json"
CONTROL_PKG_INIT_REL = "sigint101/__init__.py"
CONTROL_PKG_INIT_BYTES = (
    "# ABOUTME: Package marker for staged managed-control support modules.\n"
    "# ABOUTME: Keeps control runtime imports isolated from course releases.\n"
).encode("utf-8")
CONTROL_FORMAT_VERSION = 1
SELECTOR_FORMAT_VERSION = 1
DISPATCHER_PROTOCOL_VERSION = 1
LOCK_PROTOCOL_VERSION = 1
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


def _default_root() -> Path:
    env_root = os.environ.get("SIGINT101_UPDATER_ROOT")
    if env_root:
        return Path(env_root)
    return Path.home() / DEFAULT_ROOT_REL


def _read_host_machine_id() -> str | None:
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    return None


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


def _read_regular_fd_bytes(fd: int, *, label: str, allow_group_write: bool = False) -> bytes:
    st = os.fstat(fd)
    bad_mode = (st.st_mode & 0o002) if allow_group_write else (st.st_mode & 0o022)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or bad_mode:
        raise RuntimeError(f"untrusted {label}")
    chunks: list[bytes] = []
    while True:
        part = os.read(fd, 65536)
        if not part:
            break
        chunks.append(part)
    return b"".join(chunks)


def _read_control_record(root: Path) -> tuple[str, str | None]:
    root_fd = _open_trusted_dir(root, label="updater root")
    fflags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        try:
            fd = os.open("control.json", fflags, dir_fd=root_fd)
        except OSError as exc:
            raise RuntimeError("control.json is missing or symlinked") from exc
        try:
            # control.json sits inside the 0700 updater root; allow umask-0002 group bit on it.
            raw = _read_regular_fd_bytes(fd, label="control.json", allow_group_write=True)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"malformed control.json: {exc}") from exc
    if not isinstance(data, dict) or data.get("format_version") != 1:
        raise RuntimeError("unsupported control.json format")
    ctrl_id = data.get("control_id")
    if not isinstance(ctrl_id, str) or not ctrl_id or "/" in ctrl_id or ctrl_id in (".", ".."):
        raise RuntimeError(f"invalid control_id in control.json: {ctrl_id!r}")
    digest = data.get("contract_digest")
    if digest is not None and (not isinstance(digest, str) or len(digest) != 64):
        raise RuntimeError(f"invalid contract_digest in control.json: {digest!r}")
    return ctrl_id, digest


def _read_control_id(root: Path) -> str:
    ctrl_id, _ = _read_control_record(root)
    return ctrl_id


def _verify_control_generation(
    control_dir: Path,
    *,
    expected_control_id: str,
    expected_contract_digest: str | None = None,
) -> dict:
    """Verify the selected control generation's manifest and canonical marker before exec (F1)."""
    root_fd = _open_trusted_dir(control_dir, label=f"control generation {expected_control_id}")
    fflags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        try:
            mfd = os.open(CONTROL_MANIFEST_FILENAME, fflags, dir_fd=root_fd)
        except OSError as exc:
            raise RuntimeError(
                f"selected control generation manifest missing: {expected_control_id}"
            ) from exc
        try:
            raw_manifest = _read_regular_fd_bytes(mfd, label=CONTROL_MANIFEST_FILENAME)
        finally:
            os.close(mfd)

        try:
            manifest = json.loads(raw_manifest.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"malformed control manifest in {expected_control_id}: {exc}"
            ) from exc
        if not isinstance(manifest, dict):
            raise RuntimeError(f"invalid control manifest object in {expected_control_id}")

        expected_versions = {
            "control_format_version": CONTROL_FORMAT_VERSION,
            "selector_format_version": SELECTOR_FORMAT_VERSION,
            "dispatcher_protocol_version": DISPATCHER_PROTOCOL_VERSION,
            "lock_protocol_version": LOCK_PROTOCOL_VERSION,
        }
        for key, exp in expected_versions.items():
            if manifest.get(key) != exp:
                raise RuntimeError(
                    f"control manifest {key} mismatch in {expected_control_id}: "
                    f"{manifest.get(key)!r} != {exp!r}"
                )

        if manifest.get("control_id") != expected_control_id:
            raise RuntimeError(
                f"control_id mismatch in manifest: {manifest.get('control_id')!r} != "
                f"{expected_control_id!r}"
            )

        expected_files = manifest.get("files")
        if not isinstance(expected_files, dict) or set(expected_files.keys()) != set(
            CONTROL_ALLOWLIST
        ):
            raise RuntimeError(
                f"control manifest files table does not match allowlist in {expected_control_id}"
            )

        digest_payload = {**expected_versions, "files": expected_files}
        canonical = json.dumps(digest_payload, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        actual_contract_digest = hashlib.sha256(canonical).hexdigest()
        if manifest.get("contract_digest") != actual_contract_digest:
            raise RuntimeError(
                f"control manifest contract_digest mismatch in {expected_control_id}"
            )
        if (
            expected_contract_digest is not None
            and actual_contract_digest != expected_contract_digest
        ):
            raise RuntimeError(
                f"control generation contract_digest does not match control.json in "
                f"{expected_control_id}"
            )

        actual_files: dict[str, dict] = {}
        has_pkg_init = False
        has_manifest = False

        def _scan(fd: int, prefix: str) -> None:
            nonlocal has_pkg_init, has_manifest
            for name in sorted(os.listdir(fd)):
                rel = f"{prefix}/{name}" if prefix else name
                lst = os.lstat(name, dir_fd=fd)
                if stat.S_ISLNK(lst.st_mode):
                    raise RuntimeError(f"symlink refused in control generation: {rel}")
                if stat.S_ISDIR(lst.st_mode):
                    child_fd = _open_trusted_dir(
                        name, dir_fd=fd, label=f"control generation directory {rel}"
                    )
                    try:
                        _scan(child_fd, rel)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(lst.st_mode):
                    if rel == CONTROL_MANIFEST_FILENAME:
                        has_manifest = True
                        continue
                    if rel == CONTROL_PKG_INIT_REL:
                        rfd = os.open(name, fflags, dir_fd=fd)
                        try:
                            init_payload = _read_regular_fd_bytes(rfd, label=rel)
                        finally:
                            os.close(rfd)
                        if init_payload != CONTROL_PKG_INIT_BYTES:
                            raise RuntimeError(
                                f"control package marker drifted from canonical bytes: {rel}"
                            )
                        has_pkg_init = True
                        continue
                    if rel not in expected_files:
                        raise RuntimeError(f"unexpected file in control generation: {rel}")
                    rfd = os.open(name, fflags, dir_fd=fd)
                    try:
                        payload = _read_regular_fd_bytes(rfd, label=rel)
                    finally:
                        os.close(rfd)
                    actual_files[rel] = {
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                    }
                else:
                    raise RuntimeError(f"non-regular file in control generation: {rel}")

        _scan(root_fd, "")
        if not has_manifest or not has_pkg_init:
            raise RuntimeError(
                f"control generation missing required marker files: {expected_control_id}"
            )
        if actual_files != expected_files:
            raise RuntimeError(
                f"control generation files drifted from manifest in {expected_control_id}"
            )
        return manifest
    finally:
        os.close(root_fd)


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

    host_mid = _read_host_machine_id()
    if not host_mid:
        raise RuntimeError("non-bench host refused: host machine-id is unavailable")
    marker_mid = data.get("machine_id") if isinstance(data, dict) else None
    marker_root = data.get("updater_root") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("format_version") != 1
        or data.get("purpose") != "sigint101-pr4-disposable-bench"
        or data.get("hostname") != socket.gethostname()
        or not isinstance(marker_mid, str)
        or marker_mid != host_mid
        or not isinstance(marker_root, str)
        or not os.path.isabs(marker_root)
        or os.path.realpath(marker_root) != marker_root
        or Path(marker_root) != resolved_root
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
        control_id, expected_contract_digest = _read_control_record(root)
        control_dir = root / "control" / control_id
        _verify_control_generation(
            control_dir,
            expected_control_id=control_id,
            expected_contract_digest=expected_contract_digest,
        )

        if args.command == "serve":
            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONPATH"] = str(control_dir)
            if gate_fd is not None:
                os.set_inheritable(gate_fd, True)
                env[SHARED_GATE_FD_ENV] = str(gate_fd)
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
            # F8: isolate CLI dispatch from CWD (e.g. ~/sigint-101) with chdir(root) + -P -s
            # so a local checkout's sigint101/ tree can never shadow ROOT/control/<id>.
            env = {k: os.environ[k] for k in _CLI_PASSTHROUGH_ENV if k in os.environ}
            env.setdefault("PATH", "/usr/bin:/bin")
            env.setdefault("LANG", "C.UTF-8")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONPATH"] = str(control_dir)
            if gate_fd is not None:
                os.set_inheritable(gate_fd, True)
                env[MUTATION_GATE_FD_ENV] = str(gate_fd)
            os.chdir(root)
            cli_args = (
                ["provision", *args.extra]
                if args.command == "provision"
                else [f"--{args.command}", *args.extra]
            )
            exec_argv = [
                sys.executable,
                "-B",
                "-P",
                "-s",
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
