# ABOUTME: Consistent generation snapshot inventory and copy over trusted directory descriptors.
# ABOUTME: Rejects symlinks, aliases, special files, mount crossings, and unsafe directory modes.

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

from .migrations import migrate_store_copy
from .storage import fsync_dir

GENERATION_MARKER_FILENAME = "generation.json"
SQLITE_STORE_FILES = ("progress.db", "exam.db")
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_SQLITE_SIDECAR_NAMES = frozenset(
    f"{store}{ext}" for store in SQLITE_STORE_FILES for ext in _SQLITE_SIDECAR_SUFFIXES
)
_COPY_CHUNK_BYTES = 65536


class SnapshotError(RuntimeError):
    """Raised when a generation tree fails path, permission, mount, or copy verification."""


MountIdReader = Callable[[int, str], int]


def read_fd_mount_id(
    fd: int,
    _rel_path: str = "",
    *,
    fdinfo_root: str | Path | None = None,
) -> int:
    """Return the kernel mount ID for an open descriptor via /proc/self/fdinfo.

    Fails closed (SnapshotError) when fdinfo or mnt_id cannot be read — never falls back to st_dev
    because same-device bind mounts share st_dev (F6).
    """
    base = Path(fdinfo_root) if fdinfo_root is not None else Path("/proc/self/fdinfo")
    fdinfo = base / str(fd)
    try:
        text = fdinfo.read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotError(
            f"cannot read mount ID from fdinfo for {_rel_path or fd!r}: {exc}"
        ) from exc
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError as exc:
                raise SnapshotError(
                    f"malformed mnt_id in fdinfo for {_rel_path or fd!r}"
                ) from exc
    raise SnapshotError(f"mnt_id missing from fdinfo for {_rel_path or fd!r}")


def _check_dir_stat(
    st: os.stat_result,
    rel_path: str,
    *,
    expected_uid: int,
    check_permissions: bool,
) -> None:
    if not stat.S_ISDIR(st.st_mode):
        raise SnapshotError(f"expected directory at {rel_path!r}")
    if check_permissions:
        if st.st_uid != expected_uid:
            raise SnapshotError(
                f"untrusted directory owner at {rel_path!r}: uid {st.st_uid} != {expected_uid}"
            )
        if st.st_mode & 0o022:
            mode_str = oct(stat.S_IMODE(st.st_mode))
            raise SnapshotError(
                f"unsafe group/world-writable directory permissions {mode_str} at {rel_path!r}"
            )


def _check_file_stat(
    st: os.stat_result,
    rel_path: str,
    *,
    expected_uid: int,
    check_permissions: bool,
) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise SnapshotError(f"non-regular special file refused at {rel_path!r}")
    if check_permissions and st.st_uid != expected_uid:
        raise SnapshotError(
            f"untrusted file owner at {rel_path!r}: uid {st.st_uid} != {expected_uid}"
        )


def _open_dir_nofollow(
    path: str | Path,
    *,
    dir_fd: int | None = None,
    rel_path: str,
    expected_uid: int,
    check_permissions: bool,
    mount_id_reader: MountIdReader,
    expected_mount_id: int | None,
) -> tuple[int, os.stat_result, int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            # Distinguish symlink from non-directory cleanly via lstat when needed.
            try:
                lst = os.lstat(os.fspath(path), dir_fd=dir_fd)
                if stat.S_ISLNK(lst.st_mode):
                    raise SnapshotError(f"symlink refused at {rel_path!r}") from exc
            except OSError:
                pass
            raise SnapshotError(f"path is not a real directory at {rel_path!r}: {exc}") from exc
        raise SnapshotError(f"cannot open directory {rel_path!r}: {exc}") from exc

    try:
        st = os.fstat(fd)
        _check_dir_stat(
            st, rel_path, expected_uid=expected_uid, check_permissions=check_permissions
        )
        mnt_id = mount_id_reader(fd, rel_path)
        if expected_mount_id is not None and mnt_id != expected_mount_id:
            raise SnapshotError(
                f"mount boundary crossed at {rel_path!r}: mount {mnt_id} != {expected_mount_id}"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd, st, mnt_id


def _open_reg_nofollow(
    name: str,
    *,
    dir_fd: int,
    rel_path: str,
    expected_uid: int,
    check_permissions: bool,
    mount_id_reader: MountIdReader,
    expected_mount_id: int,
) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SnapshotError(f"symlink refused at {rel_path!r}") from exc
        if exc.errno == errno.ENXIO:
            raise SnapshotError(f"non-regular special file refused at {rel_path!r}") from exc
        raise SnapshotError(f"cannot open file {rel_path!r}: {exc}") from exc

    try:
        st = os.fstat(fd)
        _check_file_stat(
            st, rel_path, expected_uid=expected_uid, check_permissions=check_permissions
        )
        mnt_id = mount_id_reader(fd, rel_path)
        if mnt_id != expected_mount_id:
            raise SnapshotError(
                f"mount boundary crossed at {rel_path!r}: mount {mnt_id} != {expected_mount_id}"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _hash_fd(fd: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, _COPY_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _hash_file_path(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.fspath(path), flags)
    try:
        return _hash_fd(fd)
    finally:
        os.close(fd)


def inventory_generation(
    source_dir: str | Path,
    *,
    mount_id_reader: MountIdReader | None = None,
    check_permissions: bool = True,
) -> dict:
    """Walk a quiesced generation over trusted descriptors and inventory every entry."""
    reader = mount_id_reader or read_fd_mount_id
    expected_uid = os.geteuid()
    src_path = Path(source_dir)
    if not src_path.exists() and not src_path.is_symlink():
        raise SnapshotError(f"source generation directory does not exist: {src_path}")

    root_fd, _, root_mnt = _open_dir_nofollow(
        src_path,
        rel_path=".",
        expected_uid=expected_uid,
        check_permissions=check_permissions,
        mount_id_reader=reader,
        expected_mount_id=None,
    )

    dirs: list[str] = []
    empty_dirs: list[str] = []
    files: dict[str, dict] = {}
    inode_links: dict[tuple[int, int], list[tuple[str, int]]] = {}

    def walk(dir_fd: int, prefix: str) -> None:
        names = sorted(os.listdir(dir_fd))
        if prefix and not names:
            empty_dirs.append(prefix)
        for name in names:
            rel = f"{prefix}/{name}" if prefix else name
            lst = os.lstat(name, dir_fd=dir_fd)
            mode = lst.st_mode
            if stat.S_ISLNK(mode):
                raise SnapshotError(f"symlink refused at {rel!r}")
            if stat.S_ISDIR(mode):
                child_fd, _, _ = _open_dir_nofollow(
                    name,
                    dir_fd=dir_fd,
                    rel_path=rel,
                    expected_uid=expected_uid,
                    check_permissions=check_permissions,
                    mount_id_reader=reader,
                    expected_mount_id=root_mnt,
                )
                dirs.append(rel)
                try:
                    walk(child_fd, rel)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(mode):
                file_fd, st = _open_reg_nofollow(
                    name,
                    dir_fd=dir_fd,
                    rel_path=rel,
                    expected_uid=expected_uid,
                    check_permissions=check_permissions,
                    mount_id_reader=reader,
                    expected_mount_id=root_mnt,
                )
                try:
                    sha256, size = _hash_fd(file_fd)
                finally:
                    os.close(file_fd)
                inode_links.setdefault((st.st_dev, st.st_ino), []).append((rel, st.st_nlink))
                files[rel] = {
                    "size": size,
                    "sha256": sha256,
                    "mtime_ns": st.st_mtime_ns,
                    "atime_ns": st.st_atime_ns,
                    "st_dev": st.st_dev,
                    "st_ino": st.st_ino,
                    "st_nlink": st.st_nlink,
                }
            else:
                raise SnapshotError(f"non-regular special file refused at {rel!r}")

    try:
        walk(root_fd, "")
    finally:
        os.close(root_fd)

    # F4: allow in-generation hardlinks (such as an interrupted SSTV publish where both
    # sstv/<png> and sstv/.staging/<png> share one inode with nlink == 2), while refusing any
    # inode whose link count exceeds the links observed inside this generation.
    for entries in inode_links.values():
        nlinks = {n for _, n in entries}
        expected_nlink = entries[0][1]
        if len(nlinks) != 1 or len(entries) != expected_nlink:
            raise SnapshotError(
                f"external hardlink alias refused at {entries[0][0]!r} "
                f"(nlink={expected_nlink}, in_generation={len(entries)})"
            )

    return {
        "root": str(src_path.resolve()),
        "mount_id": root_mnt,
        "dirs": dirs,
        "empty_dirs": empty_dirs,
        "files": files,
    }


def _validate_snapshot_boundaries(
    source_dir: Path,
    dest_dir: Path,
    scratch_dir: Path,
) -> tuple[Path, Path]:
    src = source_dir.resolve()
    dst = dest_dir.resolve()
    scratch = scratch_dir.resolve()
    if dst.exists() or dest_dir.is_symlink():
        raise FileExistsError(f"destination generation already exists: {dest_dir}")
    if dst == src or dst.is_relative_to(src) or src.is_relative_to(dst):
        raise ValueError(f"destination generation {dst} must stay separate from source {src}")
    for label, bound in (("source", src), ("destination", dst)):
        if scratch == bound or scratch.is_relative_to(bound) or bound.is_relative_to(scratch):
            raise ValueError(
                f"recovery scratch directory must stay outside {label} generation {bound}: "
                f"{scratch}"
            )
    if scratch.exists() or scratch_dir.is_symlink():
        raise FileExistsError(f"recovery scratch directory already exists: {scratch_dir}")
    return dst, scratch


def _open_subpath_dir_fd(root_fd: int, rel_dir: str, *, expected_uid: int) -> int:
    """Open a relative subdirectory under an already-trusted root_fd without following symlinks."""
    cur_fd = os.dup(root_fd)
    if not rel_dir:
        return cur_fd
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        for part in rel_dir.split("/"):
            next_fd = os.open(part, flags, dir_fd=cur_fd)
            os.close(cur_fd)
            cur_fd = next_fd
            st = os.fstat(cur_fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != expected_uid:
                raise SnapshotError(f"unexpected directory state at {rel_dir!r}")
    except BaseException:
        os.close(cur_fd)
        raise
    return cur_fd


def copy_generation_snapshot(
    source_dir: str | Path,
    dest_dir: str | Path,
    *,
    generation_id: str,
    scratch_dir: str | Path,
    allow_missing_stores: bool = False,
    mount_id_reader: MountIdReader | None = None,
    check_permissions: bool = True,
) -> dict:
    """Copy a complete quiesced generation into a new exclusive destination generation.

    SQLite stores are migrated/copied to standalone DELETE mode via private scratch outside both
    generations; all other regular files (events + rotations, apt/ + tle.txt, sstv/ + sidecars +
    hidden .staging/.discard, ordinary files) and empty directories are copied over trusted
    descriptors with nanosecond mtime preservation and verified SHA-256 hashes. Source is never
    modified or deleted.
    """
    if not generation_id or "/" in generation_id or generation_id in (".", ".."):
        raise ValueError(f"invalid generation_id: {generation_id!r}")

    src_path = Path(source_dir)
    dst_path = Path(dest_dir)
    scratch_path = Path(scratch_dir)
    reader = mount_id_reader or read_fd_mount_id
    expected_uid = os.geteuid()

    # Inventory and validate the entire source tree BEFORE touching destination or scratch.
    inv = inventory_generation(
        src_path, mount_id_reader=reader, check_permissions=check_permissions
    )
    resolved_dst, resolved_scratch = _validate_snapshot_boundaries(
        src_path, dst_path, scratch_path
    )

    parent_generation_id = None
    if GENERATION_MARKER_FILENAME in inv["files"]:
        try:
            old_marker = json.loads(
                (src_path / GENERATION_MARKER_FILENAME).read_text(encoding="utf-8")
            )
            if isinstance(old_marker, dict) and isinstance(old_marker.get("generation_id"), str):
                parent_generation_id = old_marker["generation_id"]
        except (OSError, ValueError):
            parent_generation_id = None

    resolved_dst.parent.mkdir(parents=True, exist_ok=True)
    os.mkdir(resolved_dst, 0o700)

    # 1) Migrate/copy progress.db and exam.db via private scratch outside both generations.
    db_receipts: dict[str, dict] = {}
    for store, filename in (("progress", "progress.db"), ("exam", "exam.db")):
        db_receipts[store] = migrate_store_copy(
            src_path / filename,
            resolved_dst / filename,
            store=store,
            scratch_dir=resolved_scratch / store,
            allow_missing_source=allow_missing_stores,
        )

    # 2) Open trusted root descriptors for source and destination.
    src_root_fd, _, src_mnt = _open_dir_nofollow(
        src_path,
        rel_path=".",
        expected_uid=expected_uid,
        check_permissions=check_permissions,
        mount_id_reader=reader,
        expected_mount_id=inv["mount_id"],
    )
    dst_root_fd, _, _ = _open_dir_nofollow(
        resolved_dst,
        rel_path=".",
        expected_uid=expected_uid,
        check_permissions=True,
        mount_id_reader=reader,
        expected_mount_id=None,
    )

    sha256_by_path: dict[str, str] = {}
    mtime_ns_by_path: dict[str, int] = {}
    try:
        # 3) Create every inventoried subdirectory (including empty and hidden dirs) exclusively.
        for rel_dir in inv["dirs"]:
            parent_rel, _, leaf = rel_dir.rpartition("/")
            parent_fd = _open_subpath_dir_fd(dst_root_fd, parent_rel, expected_uid=expected_uid)
            try:
                os.mkdir(leaf, 0o700, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)

        # 4) Copy every non-DB, non-SQLite-sidecar, non-generation-marker regular file.
        skip_top = set(SQLITE_STORE_FILES) | _SQLITE_SIDECAR_NAMES | {GENERATION_MARKER_FILENAME}
        for rel_file, meta in sorted(inv["files"].items()):
            if "/" not in rel_file and rel_file in skip_top:
                continue
            parent_rel, _, leaf = rel_file.rpartition("/")
            s_dir_fd = _open_subpath_dir_fd(src_root_fd, parent_rel, expected_uid=expected_uid)
            d_dir_fd = _open_subpath_dir_fd(dst_root_fd, parent_rel, expected_uid=expected_uid)
            try:
                src_fd, src_st = _open_reg_nofollow(
                    leaf,
                    dir_fd=s_dir_fd,
                    rel_path=rel_file,
                    expected_uid=expected_uid,
                    check_permissions=check_permissions,
                    mount_id_reader=reader,
                    expected_mount_id=src_mnt,
                )
                try:
                    if (
                        src_st.st_ino != meta["st_ino"]
                        or src_st.st_dev != meta["st_dev"]
                        or src_st.st_size != meta["size"]
                        or src_st.st_mtime_ns != meta["mtime_ns"]
                    ):
                        raise SnapshotError(
                            f"source file changed during snapshot copy: {rel_file!r}"
                        )
                    dst_flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0)
                    )
                    dst_fd = os.open(leaf, dst_flags, 0o600, dir_fd=d_dir_fd)
                    try:
                        digest = hashlib.sha256()
                        while True:
                            chunk = os.read(src_fd, _COPY_CHUNK_BYTES)
                            if not chunk:
                                break
                            view = memoryview(chunk)
                            written = 0
                            while written < len(view):
                                written += os.write(dst_fd, view[written:])
                            digest.update(chunk)
                        if digest.hexdigest() != meta["sha256"]:
                            raise SnapshotError(
                                f"source digest drifted during snapshot copy: {rel_file!r}"
                            )
                        os.utime(dst_fd, ns=(meta["atime_ns"], meta["mtime_ns"]))
                        os.fsync(dst_fd)
                    finally:
                        os.close(dst_fd)
                finally:
                    os.close(src_fd)

                # Verify destination bytes, digest, and nanosecond mtime from disk.
                verify_fd, verify_st = _open_reg_nofollow(
                    leaf,
                    dir_fd=d_dir_fd,
                    rel_path=rel_file,
                    expected_uid=expected_uid,
                    check_permissions=True,
                    mount_id_reader=reader,
                    expected_mount_id=reader(d_dir_fd, parent_rel or "."),
                )
                try:
                    actual_sha, actual_size = _hash_fd(verify_fd)
                finally:
                    os.close(verify_fd)
                if actual_sha != meta["sha256"] or actual_size != meta["size"]:
                    raise SnapshotError(f"destination verification mismatch for {rel_file!r}")
                if verify_st.st_mtime_ns != meta["mtime_ns"]:
                    raise SnapshotError(f"destination mtime mismatch for {rel_file!r}")
                sha256_by_path[rel_file] = actual_sha
                mtime_ns_by_path[rel_file] = verify_st.st_mtime_ns
            finally:
                os.close(s_dir_fd)
                os.close(d_dir_fd)

        # 5) Write generation.json marker exclusively and fsync it.
        db_digests = {}
        for filename in SQLITE_STORE_FILES:
            sha, size = _hash_file_path(resolved_dst / filename)
            db_digests[filename] = {"sha256": sha, "size": size}
        marker_payload = {
            "generation_id": generation_id,
            "parent_generation_id": parent_generation_id,
            "databases": db_digests,
            "files": sha256_by_path,
            "empty_dirs": inv["empty_dirs"],
        }
        marker_bytes = (json.dumps(marker_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        marker_flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        )
        m_fd = os.open(GENERATION_MARKER_FILENAME, marker_flags, 0o600, dir_fd=dst_root_fd)
        try:
            view = memoryview(marker_bytes)
            written = 0
            while written < len(view):
                written += os.write(m_fd, view[written:])
            os.fsync(m_fd)
        finally:
            os.close(m_fd)

        # 6) Fsync every subdirectory bottom-up, then destination root and its parent.
        for rel_dir in sorted(inv["dirs"], key=lambda p: p.count("/"), reverse=True):
            sub_fd = _open_subpath_dir_fd(dst_root_fd, rel_dir, expected_uid=expected_uid)
            try:
                os.fsync(sub_fd)
            finally:
                os.close(sub_fd)
        os.fsync(dst_root_fd)
    finally:
        os.close(src_root_fd)
        os.close(dst_root_fd)

    # F3: re-inventory the entire source generation after copy (including progress.db, exam.db,
    # sidecars, captures, and directories) so any post-inventory write or newly published file
    # fails closed rather than producing an inconsistent snapshot. Ignore read-updated atime_ns.
    post_inv = inventory_generation(
        src_path, mount_id_reader=reader, check_permissions=check_permissions
    )

    def _stable_files(file_map: dict[str, dict]) -> dict[str, dict]:
        return {
            rel: {k: v for k, v in meta.items() if k != "atime_ns"}
            for rel, meta in file_map.items()
        }

    if (
        post_inv["dirs"] != inv["dirs"]
        or post_inv["empty_dirs"] != inv["empty_dirs"]
        or _stable_files(post_inv["files"]) != _stable_files(inv["files"])
    ):
        raise SnapshotError("source generation changed during snapshot copy")

    fsync_dir(resolved_dst.parent)
    return {
        "generation_id": generation_id,
        "parent_generation_id": parent_generation_id,
        "dest_dir": str(resolved_dst),
        "manifest_path": str(resolved_dst / GENERATION_MARKER_FILENAME),
        "files_copied": sorted(sha256_by_path),
        "empty_dirs": list(inv["empty_dirs"]),
        "db_receipts": db_receipts,
        "sha256_by_path": sha256_by_path,
        "mtime_ns_by_path": mtime_ns_by_path,
    }
