# ABOUTME: Stable mutation-gate and writer-lock protocol on updater ROOT using Linux flock.
# ABOUTME: Enforces permanent lock inodes, non-blocking admission, and O_CLOEXEC FD transfer.

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .storage import validate_updater_root

EXIT_GATE_BUSY = 75
LOCK_DIR_NAME = "locks"
MUTATION_LOCK_NAME = "mutation.lock"
WRITER_LOCK_NAME = "writer.lock"
WRITER_LOCK_FD_ENV = "SIGINT101_WRITER_LOCK_FD"
SHARED_GATE_FD_ENV = "SIGINT101_SHARED_GATE_FD"
LOCK_PROTOCOL_VERSION = 1


class LockError(RuntimeError):
    """Raised when lock files are missing, symlinked, untrusted, or structurally invalid."""


class LockBusyError(LockError):
    """Raised when a non-blocking flock acquisition contends with another holder."""


@dataclass
class LockHandle:
    """An open, locked descriptor backed by one permanent lock inode."""

    fd: int
    path: Path
    st_dev: int
    st_ino: int
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)


def _open_trusted_dir(
    path: str | Path,
    *,
    dir_fd: int | None = None,
    label: str,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise LockError(f"{label} must be a real directory, not a symlink") from exc
        raise LockError(f"cannot open {label}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise LockError(f"{label} is not a directory")
        if st.st_uid != os.geteuid():
            raise LockError(f"{label} has untrusted owner uid {st.st_uid}")
        if st.st_mode & 0o022:
            raise LockError(
                f"{label} has unsafe permissions {oct(stat.S_IMODE(st.st_mode))}"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _verify_lock_fd(fd: int, *, label: str) -> os.stat_result:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise LockError(f"{label} must be a regular file")
    if st.st_uid != os.geteuid():
        raise LockError(f"{label} has untrusted owner uid {st.st_uid}")
    if st.st_nlink > 1:
        raise LockError(f"{label} must not be a hardlink alias (nlink={st.st_nlink})")
    return st


def ensure_lock_files(
    root: str | Path,
    *,
    home: str | Path | None = None,
) -> tuple[Path, Path]:
    """Create or open and fsync the two permanent lock inodes under ROOT/locks."""
    resolved_root = validate_updater_root(root, home=home)
    if not resolved_root.exists() and not resolved_root.is_symlink():
        resolved_root.mkdir(mode=0o700, parents=True)

    root_fd = _open_trusted_dir(resolved_root, label="updater root")
    try:
        try:
            os.mkdir(LOCK_DIR_NAME, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        locks_fd = _open_trusted_dir(LOCK_DIR_NAME, dir_fd=root_fd, label="locks directory")
        try:
            file_flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            for name in (MUTATION_LOCK_NAME, WRITER_LOCK_NAME):
                try:
                    fd = os.open(name, file_flags, 0o600, dir_fd=locks_fd)
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise LockError(f"lock file {name!r} must not be a symlink") from exc
                    raise LockError(f"cannot open lock file {name!r}: {exc}") from exc
                try:
                    _verify_lock_fd(fd, label=name)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.fsync(locks_fd)
        finally:
            os.close(locks_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)

    locks_dir = resolved_root / LOCK_DIR_NAME
    return locks_dir / MUTATION_LOCK_NAME, locks_dir / WRITER_LOCK_NAME


def _acquire_lock_file(
    root: str | Path,
    filename: str,
    *,
    lock_op: int,
    blocking: bool = False,
    home: str | Path | None = None,
) -> LockHandle:
    resolved_root = validate_updater_root(root, home=home)
    root_fd = _open_trusted_dir(resolved_root, label="updater root")
    try:
        locks_fd = _open_trusted_dir(LOCK_DIR_NAME, dir_fd=root_fd, label="locks directory")
    finally:
        os.close(root_fd)

    flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        try:
            fd = os.open(filename, flags, dir_fd=locks_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise LockError(f"lock file {filename!r} must not be a symlink") from exc
            raise LockError(f"cannot open lock file {filename!r}: {exc}") from exc

        try:
            st = _verify_lock_fd(fd, label=filename)
            op = lock_op if blocking else (lock_op | fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, op)
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise LockBusyError(f"lock {filename!r} is busy") from exc
                raise LockError(f"flock failed on {filename!r}: {exc}") from exc

            # Re-verify the lock inode after flock so a swapped path can never pass.
            after_lst = os.lstat(filename, dir_fd=locks_fd)
            if (after_lst.st_dev, after_lst.st_ino) != (st.st_dev, st.st_ino):
                raise LockError(f"lock file {filename!r} inode changed while locking")
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(locks_fd)

    return LockHandle(
        fd=fd,
        path=resolved_root / LOCK_DIR_NAME / filename,
        st_dev=st.st_dev,
        st_ino=st.st_ino,
    )


@contextmanager
def acquire_mutation_gate(
    root: str | Path,
    *,
    blocking: bool = False,
    home: str | Path | None = None,
) -> Iterator[LockHandle]:
    """Hold the exclusive mutation gate (LOCK_EX on mutation.lock) with O_CLOEXEC."""
    handle = _acquire_lock_file(
        root, MUTATION_LOCK_NAME, lock_op=fcntl.LOCK_EX, blocking=blocking, home=home
    )
    try:
        yield handle
    finally:
        handle.close()


@contextmanager
def acquire_shared_gate(
    root: str | Path,
    *,
    blocking: bool = False,
    home: str | Path | None = None,
) -> Iterator[LockHandle]:
    """Hold the shared admission gate (LOCK_SH on mutation.lock) with O_CLOEXEC."""
    handle = _acquire_lock_file(
        root, MUTATION_LOCK_NAME, lock_op=fcntl.LOCK_SH, blocking=blocking, home=home
    )
    try:
        yield handle
    finally:
        handle.close()


@contextmanager
def acquire_writer_lock(
    root: str | Path,
    *,
    blocking: bool = False,
    home: str | Path | None = None,
) -> Iterator[LockHandle]:
    """Hold the exclusive console writer lock (LOCK_EX on writer.lock) with O_CLOEXEC."""
    handle = _acquire_lock_file(
        root, WRITER_LOCK_NAME, lock_op=fcntl.LOCK_EX, blocking=blocking, home=home
    )
    try:
        yield handle
    finally:
        handle.close()


def adopt_inherited_shared_gate(
    root: str | Path,
    fd: int,
    *,
    home: str | Path | None = None,
) -> LockHandle:
    """Adopt a shared mutation-gate FD passed from the stable dispatcher and re-mark CLOEXEC."""
    resolved_root = validate_updater_root(root, home=home)
    os.set_inheritable(fd, False)
    st = _verify_lock_fd(fd, label="inherited shared gate")
    expected_path = resolved_root / LOCK_DIR_NAME / MUTATION_LOCK_NAME
    exp_st = os.lstat(expected_path)
    if (st.st_dev, st.st_ino) != (exp_st.st_dev, exp_st.st_ino):
        raise LockError("inherited shared gate FD does not match ROOT/locks/mutation.lock")
    return LockHandle(fd=fd, path=expected_path, st_dev=st.st_dev, st_ino=st.st_ino)


def lock_inherited_writer_fd(fd: int) -> int:
    """Mark an exec-transferred writer lock FD close-on-exec before importing app or spawning."""
    if fd < 0:
        raise LockError(f"invalid writer lock FD: {fd}")
    _verify_lock_fd(fd, label="inherited writer lock")
    os.set_inheritable(fd, False)
    return fd


class LauncherLockSession:
    """Non-blocking startup lock session: shared mutation gate + exclusive writer lock."""

    def __init__(self, shared_handle: LockHandle | None, writer_handle: LockHandle | None):
        self._shared = shared_handle
        self._writer = writer_handle

    @classmethod
    def acquire(
        cls,
        root: str | Path,
        *,
        inherited_shared_fd: int | None = None,
        home: str | Path | None = None,
    ) -> LauncherLockSession:
        """Take shared gate then writer lock without waiting; release shared if writer is busy."""
        if inherited_shared_fd is not None:
            shared = adopt_inherited_shared_gate(root, inherited_shared_fd, home=home)
        else:
            shared = _acquire_lock_file(
                root, MUTATION_LOCK_NAME, lock_op=fcntl.LOCK_SH, blocking=False, home=home
            )
        try:
            writer = _acquire_lock_file(
                root, WRITER_LOCK_NAME, lock_op=fcntl.LOCK_EX, blocking=False, home=home
            )
        except BaseException:
            shared.close()
            raise
        return cls(shared, writer)

    def drop_shared_gate(self) -> None:
        """Release the shared admission gate once selection and pre-serve checks are done."""
        if self._shared is not None:
            self._shared.close()
            self._shared = None

    def prepare_writer_fd_for_exec(self) -> int:
        """Make only the writer lock descriptor inheritable for immediate service exec."""
        if self._writer is None or self._writer.closed:
            raise LockError("writer lock is not held")
        fd = self._writer.fd
        os.set_inheritable(fd, True)
        self._writer.closed = True
        self._writer = None
        return fd

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None
