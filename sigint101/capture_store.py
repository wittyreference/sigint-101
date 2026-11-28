# ABOUTME: Publishes private SigMF pairs below the configured data directory.
# ABOUTME: Checks space and ownership under a cross-console lock, with passive catalog reads.

import ctypes
import errno
import fcntl
import logging
import os
import re
import stat
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .snapshot import SnapshotError, read_fd_mount_id
from .staging import (
    _DIR_FLAGS,
    _OWNER_LOCK,
    _OWNER_NAME,
    _PENDING_NAME,
    _open_lock_file,
    _still_named,
    create_owner,
    sweep_owners,
)
from .storage import fsync_fd

PAIR_ALLOWANCE = 5_000_000
QUOTA = 25_000_000
HEADROOM = 64 * 1024 * 1024
META_LIMIT = 65_536
STEM = re.compile(r"c-[0-9]{8}t[0-9]{6}z-[0-9a-f]{32}")
FILE_FLAGS = os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
ROOT = "captures"
STAGING = ".staging"
LOCK = ".store.lock"
PAYLOAD = "payload"
SUFFIXES = (".sigmf-data", ".sigmf-meta")
_LOG = logging.getLogger(__name__)


class CaptureStorageError(RuntimeError):
    def __init__(self, message, *, full=False):
        super().__init__(message)
        self.full = full


def _invalid(what):
    return OSError(errno.EPERM, f"cannot verify {what}")


def _private_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise _invalid("capture file")
    return info


def _private_dir(fd, *, mount=None, parent=False):
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if (info.st_uid != os.geteuid() or (mode & 0o022 if parent else mode != 0o700)
            or (mount is not None and read_fd_mount_id(fd) != mount)):
        raise _invalid("capture directory")
    return info


@contextmanager
def _directory(parent, name, mount, *, create=False):
    made = False
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            made = True
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    try:
        if made:
            # A setgid data parent can pass its bit to a mkdir that requested 0700.
            _private_dir(fd, mount=mount, parent=True)
            if not _still_named(parent, name, fd):
                raise _invalid("capture directory identity")
            os.fchmod(fd, 0o700)
            fsync_fd(fd)
            fsync_fd(parent)
        _private_dir(fd, mount=mount)
        yield fd
    finally:
        os.close(fd)


def _names(stem):
    return [stem + suffix for suffix in SUFFIXES]


def _rename_new(src_fd, src, dst_fd, dst):
    # Linux renameat2 makes the absence check part of the rename. Plain rename could
    # replace a pre-existing empty directory between a userspace check and the syscall.
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if rename(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


@contextmanager
def _entries(fd, names, mount):
    """Keep checked descriptors open while the caller verifies identities or unlinks."""
    with ExitStack() as stack:
        files = {}
        for name in names:
            entry = os.open(name, os.O_RDONLY | FILE_FLAGS, dir_fd=fd)
            stack.callback(os.close, entry)
            info = _private_file(entry)
            if read_fd_mount_id(entry) != mount or not _still_named(fd, name, entry):
                raise _invalid("capture file identity")
            files[name] = (entry, info)
        yield files


def _empty_stage(staging, name, owner, owner_info):
    mount = read_fd_mount_id(staging)
    _private_dir(owner, mount=mount)
    if not _still_named(staging, name, owner):
        raise _invalid("capture staging owner identity")
    entries = set(os.listdir(owner))
    if entries - {_OWNER_LOCK, PAYLOAD}:
        raise _invalid("capture staging contents")
    if PAYLOAD in entries:
        with _directory(owner, PAYLOAD, mount) as payload:
            names = os.listdir(payload)
            stems = {entry.removesuffix(suffix) for entry in names for suffix in SUFFIXES
                     if entry.endswith(suffix)}
            if names and (len(stems) != 1 or not STEM.fullmatch(stem := next(iter(stems)))
                          or not set(names) <= set(_names(stem))):
                raise _invalid("capture staging payload names")
            with _entries(payload, names, mount) as files:
                if (not _still_named(staging, name, owner)
                        or not _still_named(owner, PAYLOAD, payload)
                        or not all(_still_named(payload, n, f) for n, (f, _) in files.items())):
                    raise _invalid("capture staging identity")
                for entry in names:
                    os.unlink(entry, dir_fd=payload)
            if _still_named(owner, PAYLOAD, payload):
                os.rmdir(PAYLOAD, dir_fd=owner)
    if _OWNER_LOCK in entries:
        with _entries(owner, [_OWNER_LOCK], mount):
            os.unlink(_OWNER_LOCK, dir_fd=owner)
    if os.path.samestat(os.stat(name, dir_fd=staging, follow_symlinks=False), owner_info):
        os.rmdir(name, dir_fd=staging)


def _warn_stage(name, error):
    _LOG.warning("Capture left staging entry %s in place: %s", name, error)


def _write(parent, name, data, mount):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS, 0o600, dir_fd=parent)
    try:
        if read_fd_mount_id(fd) != mount:
            raise _invalid("capture storage mount")
        _private_file(fd)
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if not count:
                raise OSError(errno.EIO, "capture write made no progress")
            view = view[count:]
        fsync_fd(fd)
    finally:
        os.close(fd)


def _measure_files(parent):
    total = 0
    for name in os.listdir(parent):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def _used(root, staging, mount):
    size = 0
    for stem in os.listdir(root):
        if not STEM.fullmatch(stem):
            continue
        try:
            with _directory(root, stem, mount) as clip:
                size += _measure_files(clip)
        except OSError as exc:
            # A stray link or a vanished name is not a clip. Checked directory opens never
            # follow it. Ownership, mount and other verification failures still refuse.
            if exc.errno not in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                raise
    for name in os.listdir(staging):
        if not (_OWNER_NAME.fullmatch(name) or _PENDING_NAME.fullmatch(name)):
            continue
        try:
            with (_directory(staging, name, mount) as owner,
                  _directory(owner, PAYLOAD, mount) as payload):
                size += _measure_files(payload)
        except FileNotFoundError:
            pass
    return size


def _space(root, staging, mount):
    disk = os.fstatvfs(root)
    if disk.f_bavail * disk.f_frsize < PAIR_ALLOWANCE + HEADROOM:
        raise CaptureStorageError("Capture storage is full: keep 64 MiB free", full=True)
    if _used(root, staging, mount) + PAIR_ALLOWANCE > QUOTA:
        raise CaptureStorageError("Capture storage is full: 25 MB capture quota", full=True)


def _is_full(exc):
    return isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}


def _storage_error(exc):
    if _is_full(exc):
        return CaptureStorageError("Capture storage is full: disk write refused", full=True)
    return CaptureStorageError(f"Capture storage error: {exc}")


class CaptureStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self._hidden = set()

    @contextmanager
    def _root(self, *, create):
        parent = os.open(self.data_dir, _DIR_FLAGS)
        try:
            _private_dir(parent, parent=True)
            mount = read_fd_mount_id(parent)
            with _directory(parent, ROOT, mount, create=create) as root:
                yield parent, root, mount
        finally:
            os.close(parent)

    @contextmanager
    def _writer(self):
        try:
            with self._root(create=True) as (parent, root, mount):
                lock = _open_lock_file(root, LOCK, create=True)
                try:
                    _private_file(lock)
                    deadline = time.monotonic() + 0.25
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise CaptureStorageError("Capture storage is busy") from None
                            time.sleep(0.01)
                    if not _still_named(root, LOCK, lock) or not _still_named(parent, ROOT, root):
                        raise _invalid("capture store lock or root")
                    with _directory(root, STAGING, mount, create=True) as staging:
                        sweep_owners(staging, _empty_stage, _warn_stage)
                        _space(root, staging, mount)
                        yield parent, root, staging, mount
                finally:
                    os.close(lock)
        except (OSError, SnapshotError) as exc:
            raise _storage_error(exc) from exc

    def preflight(self):
        with self._writer():
            pass

    def catalog(self):
        """List complete private pairs without creating anything or taking the writer lock."""
        result = []
        try:
            with self._root(create=False) as (parent, root, mount):
                for stem in sorted(os.listdir(root)):
                    if not STEM.fullmatch(stem) or stem in self._hidden:
                        continue
                    try:
                        with _directory(root, stem, mount) as clip:
                            if set(os.listdir(clip)) != set(names := _names(stem)):
                                continue
                            with _entries(clip, names, mount) as files:
                                if (_still_named(root, stem, clip)
                                        and _still_named(parent, ROOT, root)):
                                    result.append({"id": stem, "bytes": sum(
                                        info.st_size for _, info in files.values())})
                    except (OSError, SnapshotError):
                        pass
        except (OSError, SnapshotError):
            pass
        return result

    def save(self, data, meta, admit=lambda: True):
        if len(meta) > META_LIMIT or len(data) + len(meta) > PAIR_ALLOWANCE:
            raise CaptureStorageError("Capture storage error: pair exceeds its size limit")
        with self._writer() as (parent, root, staging, mount):
            while True:
                date = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
                stem = f"c-{date}-{uuid.uuid4().hex}"
                try:
                    os.stat(stem, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    break
            name, owner, lock = create_owner(staging, "Capture")
            try:
                _private_dir(owner, mount=mount)
                _private_file(lock)
                with _directory(owner, PAYLOAD, mount, create=True) as payload:
                    names = _names(stem)
                    for entry, contents in zip(names, (data, meta), strict=True):
                        _write(payload, entry, contents, mount)
                    fsync_fd(payload)
                    if not admit():
                        return None
                    with _entries(payload, names, mount) as files:
                        data_parent = os.stat(self.data_dir, follow_symlinks=False)
                        if (not os.path.samestat(data_parent, os.fstat(parent))
                                or not _still_named(parent, ROOT, root)
                                or not _still_named(root, STAGING, staging)
                                or not _still_named(staging, name, owner)
                                or not _still_named(owner, PAYLOAD, payload)
                                or set(os.listdir(payload)) != set(names)
                                or not all(_still_named(payload, n, fd)
                                           for n, (fd, _) in files.items())):
                            raise _invalid("capture publish identity")
                        _rename_new(owner, PAYLOAD, root, stem)
                        try:
                            fsync_fd(owner)
                            fsync_fd(root)
                        except OSError as publication_error:
                            self._hidden.add(stem)
                            try:
                                if not _still_named(root, stem, payload):
                                    raise _invalid("capture rollback identity")
                                _rename_new(root, stem, owner, PAYLOAD)
                                fsync_fd(root)
                                fsync_fd(owner)
                            except OSError as rollback_error:
                                full = _is_full(publication_error) or _is_full(rollback_error)
                                label = ("Capture storage is full" if full
                                         else "Capture storage error")
                                raise CaptureStorageError(
                                    f"{label}: could not sync saved pair ({publication_error}) "
                                    f"or roll it back ({rollback_error})", full=full,
                                ) from rollback_error
                            raise
                return {"id": stem, "bytes": len(data) + len(meta)}
            finally:
                try:
                    _empty_stage(staging, name, owner, os.fstat(owner))
                except (OSError, SnapshotError) as exc:
                    _warn_stage(name, exc)
                finally:
                    os.close(lock)
                    os.close(owner)
