# ABOUTME: Shares the descriptor and flock protocol for receiver staging owners.
# ABOUTME: Callers check their own roots and decide which staged payloads may be removed.

import fcntl
import os
import re
import stat
import time
import uuid

_OWNER_NAME = re.compile(r"[0-9a-f]{32}")
_PENDING_NAME = re.compile(r"\.new-[0-9a-f]{32}")
_OWNER_LOCK = ".owner.lock"
_CREATE_LOCK = ".create.lock"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_WAIT_S = 1.5
_CREATE_RETRY_S = 0.05


class _UntrustedStaging(OSError):
    """A staging entry this caller cannot verify."""


class _StagingBusy(OSError):
    """A sweeper kept the creation lock past the start budget."""


def _open_lock_file(dir_fd: int, name: str, *, create: bool) -> int:
    """Open a lock file in a verified folder, without following links: this account's, regular."""
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(name, flags | (os.O_CREAT if create else 0), 0o600, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise _UntrustedStaging(f"{name} is not this account's regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _still_named(dir_fd: int, name: str, fd: int) -> bool:
    """True while `name` in the folder still refers to the file this descriptor has open."""
    try:
        return os.path.samestat(os.stat(name, dir_fd=dir_fd, follow_symlinks=False), os.fstat(fd))
    except FileNotFoundError:
        return False


def _take_create_lock_shared(fd: int, label: str) -> None:
    """Take the creation lock shared, retrying briefly; refuse if a sweeper keeps it."""
    deadline = time.monotonic() + _CREATE_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise _StagingBusy("another console is tidying the staging folder; "
                                   f"try {label} again") from None
            time.sleep(_CREATE_RETRY_S)


def _close_all(fds: tuple) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def create_owner(staging_fd: int, label: str):
    """Keep the creation lock shared until the owner lock is installed and visible."""
    name = uuid.uuid4().hex
    pending = f".new-{name}"
    create_fd = sub_fd = lock_fd = None
    try:
        create_fd = _open_lock_file(staging_fd, _CREATE_LOCK, create=True)
        _take_create_lock_shared(create_fd, label)
        if not _still_named(staging_fd, _CREATE_LOCK, create_fd):
            raise _UntrustedStaging(f"{_CREATE_LOCK} was replaced while it was taken")
        os.mkdir(pending, 0o700, dir_fd=staging_fd)
        sub_fd = os.open(pending, _DIR_FLAGS, dir_fd=staging_fd)
        lock_fd = os.open(_OWNER_LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                          | os.O_CLOEXEC, 0o600, dir_fd=sub_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.rename(pending, name, src_dir_fd=staging_fd, dst_dir_fd=staging_fd)
        return name, sub_fd, lock_fd
    except BaseException:
        if lock_fd is not None:
            try:
                os.unlink(_OWNER_LOCK, dir_fd=sub_fd)
            except OSError:
                pass
            _close_all((lock_fd,))
        if sub_fd is not None:
            try:
                if _still_named(staging_fd, pending, sub_fd):
                    os.rmdir(pending, dir_fd=staging_fd)
            except OSError:
                pass
            _close_all((sub_fd,))
        raise
    finally:
        if create_fd is not None:
            _close_all((create_fd,))


def sweep_owners(staging_fd, cleanup, on_error, *, own=None):
    """Dispatch cleanup only when an owner is gone or its creation was interrupted."""
    names = os.listdir(staging_fd)

    def clean(name, pending):
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=staging_fd)
            try:
                if pending:
                    if _still_named(staging_fd, name, fd):
                        cleanup(staging_fd, name, fd, os.fstat(fd))
                    return
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                lock = os.open(_OWNER_LOCK, flags, dir_fd=fd)
                try:
                    info = os.fstat(lock)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                        raise _UntrustedStaging("the owner lock is not this account's regular file")
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if _still_named(fd, _OWNER_LOCK, lock) and _still_named(staging_fd, name, fd):
                        cleanup(staging_fd, name, fd, os.fstat(fd))
                finally:
                    os.close(lock)
            finally:
                os.close(fd)
        except (FileNotFoundError, BlockingIOError):
            pass
        except OSError as exc:
            on_error(name, exc)

    for name in names:
        if name != own and _OWNER_NAME.fullmatch(name):
            clean(name, False)
    pending = [name for name in names if _PENDING_NAME.fullmatch(name)]
    if pending:
        try:
            lock = _open_lock_file(staging_fd, _CREATE_LOCK, create=True)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if _still_named(staging_fd, _CREATE_LOCK, lock):
                    for name in pending:
                        clean(name, True)
            finally:
                os.close(lock)
        except BlockingIOError:
            pass
        except OSError as exc:
            on_error(_CREATE_LOCK, exc)
