# ABOUTME: Runs one live Meteor-M LRPT pass with SatDump under the shared receiver lifecycle.
# ABOUTME: Separates radio release from finishing, stderr drain, and result classification.

from __future__ import annotations

import copy
import fcntl
import json
import logging
import math
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import threading
import time
import zlib
from collections import deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from . import childproc, receiver_lifecycle
from .hardware import (
    RTLSDR_SERIAL_ENV,
    DeviceSelectionError,
    describe_devices,
    list_rtlsdr_devices,
    probe_rtlsdr,
    rtlsdr_radio_for_run,
)
from .receiver_lifecycle import PartialLaunch, SupervisedReceiver
from .storage import fsync_dir, fsync_file, sync_media_asset

SATDUMP = "satdump"
MODE_ENV = "SIGINT101_METEOR_MODE"
HZ_ENV = "SIGINT101_METEOR_HZ"
DEFAULT_MODE = "72k"
DEFAULT_HZ = 137_900_000
PIPELINES = {"72k": "meteor_m2-x_lrpt", "80k": "meteor_m2-x_lrpt_80k"}
CHANNELS = ("137900000", "137100000")
CAPTURE_S = 900
FINISH_CAP_S = 600  # Measure the longest product processing time on the kiosk.
RELEASE_GRACE_S = 3.0
KILL_GRACE_S = 3.0
FINISH_NICE = 10
MAX_PUBLISHED_PASSES = 20
MAX_QUIET_RUNS = 5
UNIDENTIFIED_SATELLITE = "Meteor (unidentified)"

_CLOSE_MARKER = "Stop processing"
_LOG_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}(?: - \d{2}/\d{2}/\d{4})?\] \([TDIWEC]\) ")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_STATUS = re.compile(
    r"Progress [^,]+%,\s*Viterbi\s*:\s*(SYNCED|NOSYNC)\s+BER\s*:\s*[^,]+,"
    r"\s*Deframer\s*:\s*(NOSYNC|SYNCING|SYNCED)"
)
_PRODUCT = re.compile(r"MSU[-_]MR[-_]([A-Za-z0-9][A-Za-z0-9 _-]{0,95})\.png")
_PASS_ID = re.compile(r"[0-9a-f]{32}")
_PENDING_NAME = re.compile(r"\.new-[0-9a-f]{32}")
_OWNER_LOCK = ".owner.lock"
_CREATE_LOCK = ".create.lock"
_CREATE_WAIT_S = 1.5
_CREATE_RETRY_S = 0.05
_DIR_FLAGS = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_PNG = b"\x89PNG\r\n\x1a\n"
_GALLERY_INDEX = "gallery.json"
_MAX_LINE = 1024
_MAX_ENTRIES = 4096
_MAX_DEPTH = 8
_MAX_MANIFEST_BYTES = 65536
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_DECODED_PNG_BYTES = 96 * 1024 * 1024
_MAX_PNG_DIM = 16384
_MAX_PNG_PIXELS = 64_000_000
_MAX_TIMESTAMP_S = 4_102_444_800.0
_PNG_COLOR_SPECS = {
    0: (1, (1, 2, 4, 8, 16)),
    2: (3, (8, 16)),
    3: (1, (1, 2, 4, 8)),
    4: (2, (8, 16)),
    6: (4, (8, 16)),
}
_ADAM7_PASSES = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)
_log = logging.getLogger(__name__)


class MeteorUnavailable(RuntimeError):
    """SatDump cannot start or relinquish its RTL-SDR."""


def _settings():
    mode = os.environ.get(MODE_ENV, DEFAULT_MODE)
    hz = os.environ.get(HZ_ENV, str(DEFAULT_HZ))
    if mode not in PIPELINES:
        raise MeteorUnavailable(f"{MODE_ENV} must be 72k or 80k.")
    if hz not in CHANNELS:
        raise MeteorUnavailable(f"{HZ_ENV} must be 137900000 or 137100000.")
    return mode, int(hz)


@contextmanager
def _directory(path, parent=None, *, create=False, private=True):
    if create:
        try:
            os.mkdir(path, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(path, _DIR_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if private and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise PermissionError("Meteor staging must be owned by the console and private")
        yield fd
    finally:
        os.close(fd)


def _open_lock_file(dir_fd: int, name: str, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(name, flags | (os.O_CREAT if create else 0), 0o600, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError(f"{name} is not this account's regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _still_named(dir_fd: int, name: str, fd: int) -> bool:
    try:
        return os.path.samestat(
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False), os.fstat(fd)
        )
    except FileNotFoundError:
        return False


def _take_create_lock_shared(fd: int) -> None:
    deadline = time.monotonic() + _CREATE_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise OSError("another console is tidying Meteor staging") from None
            time.sleep(_CREATE_RETRY_S)


@dataclass
class _Stage:
    output: Path
    parent: int
    fd: int
    lock_fd: int

    @classmethod
    def create(cls, path: Path, work: int, name: str):
        # PR 154 registration protocol: hold shared .create.lock across the .new-<id> window
        # so a concurrent sweep taking .create.lock exclusively never touches an active creation.
        pending = f".new-{name}"
        create_fd = fd = lock = parent = None
        named = False
        created = False
        try:
            create_fd = _open_lock_file(work, _CREATE_LOCK, create=True)
            _take_create_lock_shared(create_fd)
            if not _still_named(work, _CREATE_LOCK, create_fd):
                raise PermissionError(f"{_CREATE_LOCK} was replaced while it was taken")
            os.mkdir(pending, 0o700, dir_fd=work)
            created = True
            fd = os.open(pending, _DIR_FLAGS, dir_fd=work)
            lock = os.open(_OWNER_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                           0o600, dir_fd=fd)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            parent = os.dup(work)
            os.rename(pending, name, src_dir_fd=work, dst_dir_fd=work)
            named = True
            os.close(create_fd)
            create_fd = None
            stage = cls(path / name, parent, fd, lock)
            stage.verify()
            return stage
        except BaseException:
            if lock is not None:
                os.close(lock)
                try:
                    os.unlink(_OWNER_LOCK, dir_fd=fd)
                except OSError:
                    pass
            if fd is not None:
                os.close(fd)
            if parent is not None:
                os.close(parent)
            if created:
                try:
                    os.rmdir(name if named else pending, dir_fd=work)
                except OSError:
                    pass
            if create_fd is not None:
                os.close(create_fd)
            raise

    def verify(self):
        if not os.path.samestat(self.output.stat(follow_symlinks=False), os.fstat(self.fd)):
            raise OSError("Meteor staging directory changed")

    def close(self):
        # This is the exact retired run, with its ownership lock still held. Pruning other
        # owners and retaining published output belong to gallery publication.
        try:
            info = os.fstat(self.fd)
            _clear_run(self.fd, info.st_dev, 0, [_MAX_ENTRIES])
            if os.path.samestat(os.stat(self.output.name, dir_fd=self.parent,
                                        follow_symlinks=False), info):
                os.unlink(".owner.lock", dir_fd=self.fd)
                os.rmdir(self.output.name, dir_fd=self.parent)
        finally:
            for fd in (self.lock_fd, self.fd, self.parent):
                os.close(fd)


def _clear_run(fd, device, depth, budget):
    """Discard an owned pass by directory descriptor, leaving unknown data in place."""
    if depth >= _MAX_DEPTH:
        raise OSError("Meteor work directory exceeded its cleanup depth")
    with os.scandir(fd) as entries:
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                raise OSError("Meteor work directory exceeded its cleanup size")
            if depth == 0 and entry.name == ".owner.lock":
                continue
            info = entry.stat(follow_symlinks=False)
            if info.st_dev != device or info.st_uid != os.geteuid():
                raise PermissionError("Meteor left a foreign work entry in place")
            if stat.S_ISDIR(info.st_mode):
                with _directory(entry.name, fd, private=False) as nested:
                    if not os.path.samestat(os.fstat(nested), info):
                        raise OSError("Meteor work entry changed during cleanup")
                    _clear_run(nested, device, depth + 1, budget)
                if not os.path.samestat(os.stat(entry.name, dir_fd=fd, follow_symlinks=False),
                                        info):
                    raise OSError("Meteor work entry changed during cleanup")
                os.rmdir(entry.name, dir_fd=fd)
            else:
                os.unlink(entry.name, dir_fd=fd)


def _is_valid_png(data: bytes) -> bool:
    """Fully decode and validate a PNG byte stream without external imaging dependencies."""
    if (not isinstance(data, (bytes, bytearray))
            or len(data) < 33 or len(data) > _MAX_IMAGE_BYTES or not data.startswith(_PNG)):
        return False
    pos = len(_PNG)
    n = len(data)
    saw_ihdr = False
    saw_iend = False
    saw_plte = False
    plte_entries: int | None = None
    in_idat = False
    after_idat = False
    idat_parts: list[bytes] = []
    width = height = bit_depth = color_type = interlace = channels = 0
    while pos < n:
        if pos + 12 > n:
            return False
        length = struct.unpack_from(">I", data, pos)[0]
        ctype = bytes(data[pos + 4 : pos + 8])
        if not all(65 <= b <= 90 or 97 <= b <= 122 for b in ctype):
            return False
        data_start = pos + 8
        data_end = data_start + length
        if data_end + 4 > n:
            return False
        expected_crc = struct.unpack_from(">I", data, data_end)[0]
        if (zlib.crc32(data[pos + 4 : data_end]) & 0xFFFFFFFF) != expected_crc:
            return False
        chunk = data[data_start:data_end]
        pos = data_end + 4
        if not saw_ihdr:
            if ctype != b"IHDR" or length != 13:
                return False
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if (width < 1 or height < 1 or width > _MAX_PNG_DIM or height > _MAX_PNG_DIM
                    or width * height > _MAX_PNG_PIXELS
                    or comp != 0 or filt != 0 or interlace not in (0, 1)):
                return False
            spec = _PNG_COLOR_SPECS.get(color_type)
            if spec is None or bit_depth not in spec[1]:
                return False
            channels = spec[0]
            saw_ihdr = True
            continue
        if ctype == b"IHDR":
            return False
        if ctype == b"PLTE":
            if (saw_plte or in_idat or after_idat or color_type in (0, 4)
                    or length == 0 or length % 3 != 0):
                return False
            entries = length // 3
            max_entries = 1 << min(bit_depth, 8)
            if entries > max_entries:
                return False
            saw_plte = True
            plte_entries = entries
            continue
        if ctype == b"IDAT":
            if after_idat:
                return False
            in_idat = True
            idat_parts.append(bytes(chunk))
            continue
        if in_idat:
            in_idat = False
            after_idat = True
        if ctype == b"IEND":
            if length != 0 or pos != n:
                return False
            saw_iend = True
            break
        if (ctype[0] & 0x20) == 0:
            return False
    if not saw_ihdr or not saw_iend or not idat_parts:
        return False
    if color_type == 3 and plte_entries is None:
        return False

    bpp = channels * bit_depth
    if interlace == 0:
        passes = [(height, (width * bpp + 7) // 8)]
    else:
        passes = []
        for x0, y0, dx, dy in _ADAM7_PASSES:
            pw = (width - x0 + dx - 1) // dx if width > x0 else 0
            ph = (height - y0 + dy - 1) // dy if height > y0 else 0
            if pw > 0 and ph > 0:
                passes.append((ph, (pw * bpp + 7) // 8))
    expected_len = sum(ph * (1 + row_bytes) for ph, row_bytes in passes)
    if expected_len <= 0 or expected_len > _MAX_DECODED_PNG_BYTES:
        return False
    decomp = zlib.decompressobj()
    try:
        raw = decomp.decompress(b"".join(idat_parts), expected_len + 1)
        if decomp.unconsumed_tail:
            return False
        raw += decomp.flush()
    except zlib.error:
        return False
    if not decomp.eof or decomp.unused_data or len(raw) != expected_len:
        return False
    offset = 0
    for ph, row_bytes in passes:
        stride = 1 + row_bytes
        for _ in range(ph):
            if raw[offset] > 4:
                return False
            offset += stride
    return True


def _is_valid_timestamp(value: object) -> bool:
    """Accept only finite POSIX timestamps that render cleanly as UTC datetimes."""
    if type(value) not in (int, float) or not math.isfinite(value):
        return False
    if value < 0 or value > _MAX_TIMESTAMP_S:
        return False
    try:
        datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return False
    return True


def _pictures(fd, device, prefix=(), budget=None):
    """Inspect only bounded, same-owner, same-device contents beneath the owned pass."""
    if budget is None:
        budget = [_MAX_ENTRIES]
    if len(prefix) >= _MAX_DEPTH or budget[0] <= 0:
        return
    with os.scandir(fd) as entries:
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                break
            try:
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device or info.st_uid != os.geteuid():
                    continue
                parts = (*prefix, entry.name)
                if stat.S_ISDIR(info.st_mode):
                    with _directory(entry.name, fd, private=False) as nested:
                        if os.path.samestat(os.fstat(nested), info):
                            yield from _pictures(nested, device, parts, budget)
                elif stat.S_ISREG(info.st_mode) and (product := _PRODUCT.fullmatch(entry.name)):
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                    with os.fdopen(os.open(entry.name, flags, dir_fd=fd), "rb") as image:
                        current = os.fstat(image.fileno())
                        if (os.path.samestat(current, info)
                                and 0 < current.st_size <= _MAX_IMAGE_BYTES):
                            raw = image.read(_MAX_IMAGE_BYTES + 1)
                            if len(raw) <= _MAX_IMAGE_BYTES and _is_valid_png(raw):
                                yield {"path": "/".join(parts), "product": product[1]}
            except OSError:
                continue


def _read_regular_bytes(dir_fd: int, rel_parts: tuple[str, ...], *,
                        max_bytes: int) -> bytes | None:
    """Open a regular same-owner file under dir_fd using O_NOFOLLOW at every path component."""
    if not rel_parts or any(p in ("", ".", "..") or "/" in p for p in rel_parts):
        return None
    cur_fd = dir_fd
    opened: list[int] = []
    try:
        root_dev = os.fstat(dir_fd).st_dev
        for part in rel_parts[:-1]:
            info = os.stat(part, dir_fd=cur_fd, follow_symlinks=False)
            if (not stat.S_ISDIR(info.st_mode) or info.st_dev != root_dev
                    or info.st_uid != os.geteuid()):
                return None
            nxt = os.open(part, _DIR_FLAGS, dir_fd=cur_fd)
            opened.append(nxt)
            if not os.path.samestat(os.fstat(nxt), info):
                return None
            cur_fd = nxt
        leaf = rel_parts[-1]
        info = os.stat(leaf, dir_fd=cur_fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_dev != root_dev
                or info.st_uid != os.geteuid() or info.st_size <= 0
                or info.st_size > max_bytes):
            return None
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open(leaf, flags, dir_fd=cur_fd)
        try:
            st = os.fstat(fd)
            if not os.path.samestat(st, info) or not stat.S_ISREG(st.st_mode):
                return None
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, max_bytes + 1 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    return None
            return b"".join(chunks)
        finally:
            os.close(fd)
    except OSError:
        return None
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _read_satellite_name(stage_fd: int) -> str:
    """Read satellite identity from the same-pass dataset.json, or unidentified fallback."""
    raw = _read_regular_bytes(stage_fd, ("dataset.json",), max_bytes=_MAX_MANIFEST_BYTES)
    if not raw:
        return UNIDENTIFIED_SATELLITE
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return UNIDENTIFIED_SATELLITE
    if not isinstance(data, dict):
        return UNIDENTIFIED_SATELLITE
    for key in ("satellite", "satellite_name"):
        val = data.get(key)
        if isinstance(val, str):
            cleaned = " ".join(val.split())
            if cleaned and len(cleaned) <= 80 and all(ord(c) >= 32 for c in cleaned):
                return cleaned
    return UNIDENTIFIED_SATELLITE


def _remove_dir_descriptor(parent_fd: int, name: str) -> None:
    """Remove a private child directory under parent_fd via descriptors, never following links."""
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise PermissionError(f"refused to remove non-owned directory {name!r}")
    parent_dev = os.fstat(parent_fd).st_dev
    if info.st_dev != parent_dev:
        raise PermissionError(f"refused to cross device removing {name!r}")
    with _directory(name, parent_fd, private=False) as fd:
        if not os.path.samestat(os.fstat(fd), info):
            raise OSError(f"directory {name!r} changed during cleanup")
        _clear_run(fd, parent_dev, 0, [_MAX_ENTRIES])
        try:
            os.unlink(_OWNER_LOCK, dir_fd=fd)
        except FileNotFoundError:
            pass
    if os.path.samestat(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), info):
        os.rmdir(name, dir_fd=parent_fd)


def _read_gallery_index(root_fd: int) -> tuple[int, list[str]] | None:
    """Read committed gallery order from data/meteor/gallery.json without following links."""
    raw = _read_regular_bytes(root_fd, (_GALLERY_INDEX,), max_bytes=_MAX_MANIFEST_BYTES)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    seq = data.get("seq")
    if type(seq) is not int or seq < 0:
        return None
    raw_passes = data.get("passes")
    if not isinstance(raw_passes, list):
        return None
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw_passes:
        if isinstance(item, str) and _PASS_ID.fullmatch(item) and item not in seen:
            seen.add(item)
            ordered.append(item)
    return seq, ordered


def _write_gallery_index(root_fd: int, root_path: Path, *, seq: int, pass_ids: list[str],
                         tmp_name: str, sync: bool) -> None:
    """Atomically write and optionally fsync the committed gallery index."""
    payload = {"version": 1, "seq": seq, "passes": pass_ids}
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(tmp_name, flags, 0o644, dir_fd=root_fd)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
    if sync:
        fsync_file(root_path / tmp_name)
    os.rename(tmp_name, _GALLERY_INDEX, src_dir_fd=root_fd, dst_dir_fd=root_fd)


def _validate_manifest(pass_id: str, data: object) -> dict | None:
    """Validate a saved pass manifest.json and its image entries."""
    if not isinstance(data, dict) or data.get("id") != pass_id:
        return None
    mode = data.get("mode")
    if not isinstance(mode, str) or mode not in PIPELINES:
        return None
    freq_hz = data.get("freq_hz")
    if type(freq_hz) is not int or str(freq_hz) not in CHANNELS:
        return None
    command = data.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    radio = data.get("radio")
    if not isinstance(radio, dict):
        return None
    satellite = data.get("satellite")
    if not isinstance(satellite, str) or not satellite.strip():
        return None
    started_at = data.get("started_at")
    ended_at = data.get("ended_at")
    if not _is_valid_timestamp(started_at) or not _is_valid_timestamp(ended_at):
        return None
    lock_ever = data.get("lock_ever")
    if data.get("result") != "done" or type(lock_ever) is not bool:
        return None
    ended_by = data.get("ended_by")
    if not isinstance(ended_by, str) or not ended_by.strip():
        return None
    images = data.get("images")
    if not isinstance(images, list) or not images:
        return None
    clean_images = []
    seen_paths: set[str] = set()
    for img in images:
        if not isinstance(img, dict):
            return None
        path = img.get("path")
        product = img.get("product")
        if not isinstance(path, str) or not isinstance(product, str) or not product.strip():
            return None
        parts = PurePosixPath(path).parts
        if (len(parts) != 3 or parts[0] != "meteor" or parts[1] != pass_id
                or parts[2] in ("", ".", "..") or not parts[2].endswith(".png")
                or path in seen_paths):
            return None
        seen_paths.add(path)
        clean_images.append({"path": path, "product": product})
    return {
        "id": pass_id,
        "started_at": float(started_at),
        "ended_at": float(ended_at),
        "freq_hz": freq_hz,
        "mode": mode,
        "command": command,
        "radio": dict(radio),
        "satellite": satellite,
        "result": "done",
        "lock_ever": lock_ever,
        "ended_by": ended_by,
        "images": clean_images,
    }


@dataclass
class _Capture:
    id: str
    mode: str
    hz: int
    radio: dict
    argv: tuple = ()
    stage: _Stage | None = None
    started_at: float | None = None
    launched: float = 0
    deadline: float = 0
    control: threading.Lock = field(default_factory=threading.Lock)
    eof: threading.Event = field(default_factory=threading.Event)
    handoff_done: threading.Event = field(default_factory=threading.Event)
    handoff_deadline: float | None = None
    kill_deadline: float | None = None
    term_sent: bool = False
    marker_at: float | None = None
    ended_at: float | None = None
    ended_by: str | None = None
    interrupted: str | None = None
    reports: tuple[str, str] | None = None
    lock_ever: bool = False
    tail: deque = field(default_factory=lambda: deque(maxlen=3))
    result: tuple | None = None
    pictures: tuple = ()
    publish_error: str | None = None

    @property
    def output(self):
        return self.stage.output

    @property
    def lock(self):
        if self.reports == ("SYNCED", "SYNCED"):
            return "locked"
        return "searching" if self.reports else "unknown"


class _SatDumpProcess:
    """Keep stderr with the sole reader. Forward group operations to the real child."""

    def __init__(self, proc):
        if not callable(getattr(proc.stderr, "readline", None)):
            raise ValueError("SatDump has no readable stderr pipe")
        self.proc = proc
        self.stdout = proc.stderr

    def __getattr__(self, name):
        return getattr(self.proc, name)


class MeteorDecoder(SupervisedReceiver):
    RETIRE_BEFORE_FENCE = True
    _unavailable = MeteorUnavailable
    _released_reason = "the radio was released to another program; use Listen now to take it back"

    def __init__(self, *, data_dir, radio_present=None, list_devices=list_rtlsdr_devices,
                 spawn=childproc.spawn, which=shutil.which, clock=time.time,
                 monotonic=time.monotonic, sync_assets=False):
        self.data_dir = Path(data_dir).resolve()
        self._last_hz = DEFAULT_HZ
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._clock = clock
        self._monotonic = monotonic
        self._sync_assets = bool(sync_assets)
        self._runs = childproc.RunGuard()
        self._gallery_lock = threading.Lock()
        self._passes: list[dict] = []
        self._seq: int = 0
        self._has_index: bool = False
        self._root_parent_synced: bool = False
        self._media_index: dict[str, tuple[str, str]] = {}
        self.watch = childproc.ChildWatch(SATDUMP, finite=True, clock=monotonic)
        self._init_lifecycle()
        self.error = None
        self.on_reception = None
        self.reception_sink = None
        self._load_passes()

    @property
    def freq_hz(self) -> int:
        run = self._run
        if run and not run.ready.is_set():
            return run.args[0].hz
        try:
            return _settings()[1]
        except MeteorUnavailable:
            return self._last_hz

    @freq_hz.setter
    def freq_hz(self, value: int) -> None:
        self._last_hz = int(value)

    def handoff_in_progress(self) -> bool:
        """True while Meteor is releasing the radio for another claimant or cancelling a spawn."""
        intent, run = self._intent, self._run
        if intent and intent.spawning and intent.cancelled and intent.run is None:
            return True
        if run and not run.ready.is_set() and not run.physical.is_set():
            capture, = run.args
            if run.cancel.is_set() or capture.handoff_deadline is not None:
                return True
        return False

    def prepare_start(self, *, for_claim=False, deadline=None):
        if self.handoff_in_progress():
            raise MeteorUnavailable(
                self.release_failure(pending=True) or "SatDump is still releasing the radio"
            )
        return super().prepare_start(for_claim=for_claim, deadline=deadline)

    def passes(self) -> list[dict]:
        """Deep copy of the verified published passes, newest first."""
        with self._gallery_lock:
            return copy.deepcopy(self._passes)

    def _rebuild_media_index_unlocked(self) -> None:
        index: dict[str, tuple[str, str]] = {}
        for row in self._passes:
            pid = row["id"]
            for img in row["images"]:
                fname = PurePosixPath(img["path"]).parts[2]
                index[img["path"]] = (pid, fname)
        self._media_index = index

    def _load_passes(self) -> None:
        """Read committed verified passes on startup without creating dirs or emitting events."""
        root = self.data_dir / "meteor"
        if not root.is_dir():
            return
        loaded: list[dict] = []
        seq = 0
        has_index = False
        try:
            with _directory(self.data_dir, private=False) as data_fd:
                with _directory("meteor", data_fd, private=False) as root_fd:
                    index = _read_gallery_index(root_fd)
                    if index is None:
                        return
                    seq, ordered_ids = index
                    has_index = True
                    root_dev = os.fstat(root_fd).st_dev
                    for pass_id in ordered_ids:
                        try:
                            info = os.stat(pass_id, dir_fd=root_fd, follow_symlinks=False)
                            if (not stat.S_ISDIR(info.st_mode)
                                    or info.st_dev != root_dev
                                    or info.st_uid != os.geteuid()):
                                continue
                            with _directory(pass_id, root_fd, private=False) as pfd:
                                if not os.path.samestat(os.fstat(pfd), info):
                                    continue
                                raw = _read_regular_bytes(
                                    pfd, ("manifest.json",), max_bytes=_MAX_MANIFEST_BYTES
                                )
                                if not raw:
                                    continue
                                manifest = _validate_manifest(
                                    pass_id, json.loads(raw.decode("utf-8"))
                                )
                                if manifest is None:
                                    continue
                                valid = True
                                for img in manifest["images"]:
                                    fname = PurePosixPath(img["path"]).parts[2]
                                    png_bytes = _read_regular_bytes(
                                        pfd, (fname,), max_bytes=_MAX_IMAGE_BYTES
                                    )
                                    if not png_bytes or not _is_valid_png(png_bytes):
                                        valid = False
                                        break
                                if valid:
                                    loaded.append(manifest)
                        except (OSError, UnicodeDecodeError, ValueError, TypeError):
                            continue
        except OSError:
            return
        with self._gallery_lock:
            self._passes = loaded[:max(0, int(MAX_PUBLISHED_PASSES))]
            self._seq = max(seq, len(loaded))
            self._has_index = has_index
            self._rebuild_media_index_unlocked()

    def read_media(self, rel_path: str) -> bytes | None:
        """Serve only a verified PNG listed in its pass's committed manifest, with O_NOFOLLOW."""
        with self._gallery_lock:
            target = self._media_index.get(rel_path)
        if target is None:
            return None
        pass_id, fname = target
        try:
            with _directory(self.data_dir, private=False) as data_fd:
                with _directory("meteor", data_fd, private=False) as root_fd:
                    data = _read_regular_bytes(
                        root_fd, (pass_id, fname), max_bytes=_MAX_IMAGE_BYTES
                    )
        except OSError:
            return None
        if not data or not _is_valid_png(data):
            return None
        return data

    def sweep_work(self) -> None:
        """Remove retired owned staging folders and crashed .new- folders safely by descriptor."""
        work_path = self.data_dir / "meteor" / "work"
        if not work_path.exists():
            return
        active_id = None
        run = self._run
        if run and not run.ready.is_set():
            active_id = run.args[0].id
        try:
            with _directory(self.data_dir, private=False) as data_fd:
                with _directory("meteor", data_fd, private=False) as root_fd:
                    with _directory("work", root_fd, private=True) as work_fd:
                        names = os.listdir(work_fd)
                        for name in names:
                            if name == active_id or not _PASS_ID.fullmatch(name):
                                continue
                            self._sweep_retired_owner(work_fd, name)
                        pending = [n for n in names if _PENDING_NAME.fullmatch(n)]
                        if pending:
                            self._tidy_pending(work_fd, pending)
        except OSError as exc:
            _log.warning("Meteor work sweep skipped: %s", exc)

    @staticmethod
    def _sweep_retired_owner(work_fd: int, name: str) -> None:
        try:
            info = os.stat(name, dir_fd=work_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                return
            with _directory(name, work_fd, private=True) as sub_fd:
                if not os.path.samestat(os.fstat(sub_fd), info):
                    return
                lock_fd = os.open(
                    _OWNER_LOCK,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=sub_fd,
                )
                try:
                    linfo = os.fstat(lock_fd)
                    if not stat.S_ISREG(linfo.st_mode) or linfo.st_uid != os.geteuid():
                        return
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return  # Live owner or child holding inherited lock.
                    if not os.path.samestat(
                        os.stat(_OWNER_LOCK, dir_fd=sub_fd, follow_symlinks=False), linfo
                    ):
                        return
                    _clear_run(sub_fd, os.fstat(work_fd).st_dev, 0, [_MAX_ENTRIES])
                    os.unlink(_OWNER_LOCK, dir_fd=sub_fd)
                    if os.path.samestat(
                        os.stat(name, dir_fd=work_fd, follow_symlinks=False), info
                    ):
                        os.rmdir(name, dir_fd=work_fd)
                finally:
                    os.close(lock_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            _log.warning("Meteor left staging folder %s in place: %s", name, exc)

    @staticmethod
    def _tidy_pending(work_fd: int, names: list[str]) -> None:
        try:
            create_fd = _open_lock_file(work_fd, _CREATE_LOCK, create=True)
        except OSError as exc:
            _log.warning("Meteor left interrupted staging folders in place: %s", exc)
            return
        try:
            try:
                fcntl.flock(create_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            if not _still_named(work_fd, _CREATE_LOCK, create_fd):
                return
            for name in names:
                try:
                    _remove_dir_descriptor(work_fd, name)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    _log.warning("Meteor left pending folder %s in place: %s", name, exc)
        finally:
            os.close(create_fd)

    def _record_quiet_run(self, capture: _Capture, phase: str, reason: str | None) -> None:
        """Record a compact quiet/interrupted run metadata file and prune to MAX_QUIET_RUNS."""
        try:
            with _directory(self.data_dir, private=False) as data_fd:
                with _directory("meteor", data_fd, create=True) as root_fd:
                    with _directory("quiet", root_fd, create=True) as quiet_fd:
                        record = {
                            "id": capture.id,
                            "started_at": capture.started_at,
                            "ended_at": capture.ended_at,
                            "freq_hz": capture.hz,
                            "mode": capture.mode,
                            "result": phase,
                            "reason": reason,
                            "lock_ever": capture.lock_ever,
                            "ended_by": capture.ended_by,
                        }
                        fname = f"{capture.id}.json"
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                        fd = os.open(fname, flags, 0o600, dir_fd=quiet_fd)
                        with os.fdopen(fd, "w", encoding="utf-8") as handle:
                            json.dump(record, handle, sort_keys=True)
                        # Prune oldest quiet records independently of published passes.
                        records = []
                        with os.scandir(quiet_fd) as entries:
                            for entry in entries:
                                if not entry.name.endswith(".json"):
                                    continue
                                stem = entry.name[:-5]
                                if not _PASS_ID.fullmatch(stem):
                                    continue
                                try:
                                    info = entry.stat(follow_symlinks=False)
                                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
                                        records.append((info.st_mtime_ns, entry.name))
                                except OSError:
                                    continue
                        records.sort(reverse=True)
                        for _, old_name in records[MAX_QUIET_RUNS:]:
                            try:
                                os.unlink(old_name, dir_fd=quiet_fd)
                            except OSError:
                                pass
        except OSError as exc:
            _log.warning("Meteor could not record quiet run %s: %s", capture.id, exc)

    def _publish_pass(self, run, capture: _Capture) -> bool:
        """Stage verified PNGs + manifest, sync in order when managed, commit, then emit."""
        sink = getattr(self, "reception_sink", None)
        publication = sink.publication() if sink is not None else nullcontext(True)
        with publication as admitted:
            if not admitted:
                return False
            pass_row = self._commit_gallery_pass(capture)
            if pass_row is None:
                return False
            run.ready.set()
            hook = self.on_reception
            if hook is not None:
                for img in pass_row["images"]:
                    item = {
                        "pass_id": pass_row["id"],
                        "path": img["path"],
                        "product": img["product"],
                        "satellite": pass_row["satellite"],
                        "mode": pass_row["mode"],
                        "started_at": pass_row["started_at"],
                        "ended_at": pass_row["ended_at"],
                        "ended_by": pass_row["ended_by"],
                    }
                    childproc.emit_reception(
                        hook, item, command=pass_row["command"],
                        freq_hz=pass_row["freq_hz"], radio=pass_row["radio"],
                    )
            return True

    def _commit_gallery_pass(self, capture: _Capture) -> dict | None:
        """Decode and stage PNGs + manifest, sync durably, commit gallery.json, reconcile cap."""
        if (not _is_valid_timestamp(capture.started_at)
                or not _is_valid_timestamp(capture.ended_at)):
            return None
        stage_fd = capture.stage.fd
        satellite = _read_satellite_name(stage_fd)
        verified_files: list[tuple[str, str, bytes]] = []
        used_names: set[str] = set()
        for pic in capture.pictures:
            rel_parts = tuple(PurePosixPath(pic["path"]).parts)
            data = _read_regular_bytes(stage_fd, rel_parts, max_bytes=_MAX_IMAGE_BYTES)
            if not data or not _is_valid_png(data):
                continue
            base = rel_parts[-1]
            fname = base
            idx = 2
            while fname in used_names:
                stem, ext = os.path.splitext(base)
                fname = f"{stem}-{idx}{ext}"
                idx += 1
            used_names.add(fname)
            verified_files.append((fname, pic["product"], data))
        if not verified_files:
            return None

        candidate_name = f".publish-{capture.id}"
        index_tmp_name = f".gallery-{capture.id}.tmp"
        root_path = self.data_dir / "meteor"
        candidate_path = root_path / candidate_name
        renamed_pass = False
        renamed_index = False
        with self._gallery_lock:
            cap = max(0, int(MAX_PUBLISHED_PASSES))
            prev_pass_ids = [row["id"] for row in self._passes]
            next_pass_ids = ([capture.id] + prev_pass_ids)[:cap]
            if capture.id not in next_pass_ids:
                return None
            prev_seq = self._seq
            next_seq = prev_seq + 1
            had_index = self._has_index
            try:
                with _directory(self.data_dir, private=False) as data_fd:
                    with _directory("meteor", data_fd, create=True) as root_fd:
                        os.mkdir(candidate_name, 0o700, dir_fd=root_fd)
                        try:
                            with _directory(candidate_name, root_fd, private=True) as cand_fd:
                                images_meta = []
                                png_paths = []
                                for fname, product, data in verified_files:
                                    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                             | os.O_NOFOLLOW | os.O_CLOEXEC)
                                    fd = os.open(fname, flags, 0o644, dir_fd=cand_fd)
                                    with os.fdopen(fd, "wb") as handle:
                                        handle.write(data)
                                    images_meta.append({
                                        "path": f"meteor/{capture.id}/{fname}",
                                        "product": product,
                                    })
                                    png_paths.append(candidate_path / fname)

                                pass_row = {
                                    "id": capture.id,
                                    "started_at": float(capture.started_at),
                                    "ended_at": float(capture.ended_at),
                                    "freq_hz": capture.hz,
                                    "mode": capture.mode,
                                    "command": shlex.join(capture.argv),
                                    "radio": capture.radio.copy(),
                                    "satellite": satellite,
                                    "result": "done",
                                    "lock_ever": capture.lock_ever,
                                    "ended_by": capture.ended_by,
                                    "images": images_meta,
                                }
                                mflags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                          | os.O_NOFOLLOW | os.O_CLOEXEC)
                                mfd = os.open("manifest.json", mflags, 0o644, dir_fd=cand_fd)
                                with os.fdopen(mfd, "w", encoding="utf-8") as handle:
                                    json.dump(pass_row, handle, sort_keys=True)
                                manifest_path = candidate_path / "manifest.json"

                            if self._sync_assets:
                                for png_path in png_paths:
                                    sync_media_asset(png_path)
                                fsync_file(manifest_path)
                                fsync_dir(candidate_path)
                                if not self._root_parent_synced:
                                    fsync_dir(self.data_dir)
                                    self._root_parent_synced = True

                            os.rename(
                                candidate_name, capture.id,
                                src_dir_fd=root_fd, dst_dir_fd=root_fd,
                            )
                            renamed_pass = True

                            if self._sync_assets:
                                fsync_dir(root_path)

                            _write_gallery_index(
                                root_fd, root_path,
                                seq=next_seq, pass_ids=next_pass_ids,
                                tmp_name=index_tmp_name, sync=self._sync_assets,
                            )
                            renamed_index = True

                            if self._sync_assets:
                                fsync_dir(root_path)
                        except OSError as exc:
                            capture.publish_error = f"could not publish Meteor pass: {exc}"[:160]
                            if renamed_index:
                                rb_name = f".gallery-rb-{capture.id}.tmp"
                                try:
                                    if had_index:
                                        _write_gallery_index(
                                            root_fd, root_path,
                                            seq=prev_seq, pass_ids=prev_pass_ids,
                                            tmp_name=rb_name, sync=False,
                                        )
                                    else:
                                        os.unlink(_GALLERY_INDEX, dir_fd=root_fd)
                                except OSError:
                                    try:
                                        os.unlink(rb_name, dir_fd=root_fd)
                                    except OSError:
                                        pass
                            try:
                                os.unlink(index_tmp_name, dir_fd=root_fd)
                            except OSError:
                                pass
                            cleanup_target = capture.id if renamed_pass else candidate_name
                            try:
                                _remove_dir_descriptor(root_fd, cleanup_target)
                            except OSError:
                                pass
                            return None

                        self._passes = ([pass_row] + self._passes)[:cap]
                        self._seq = next_seq
                        self._has_index = True
                        self._rebuild_media_index_unlocked()
                        self._reconcile_gallery_overflow(root_fd, {r["id"] for r in self._passes})
                        return copy.deepcopy(pass_row)
            except OSError as exc:
                capture.publish_error = f"could not publish Meteor pass: {exc}"[:160]
                return None

    @staticmethod
    def _reconcile_gallery_overflow(root_fd: int, keep_ids: set[str]) -> None:
        """Prune evicted or unconfirmed pass directories under data/meteor safely by descriptor."""
        try:
            with os.scandir(root_fd) as entries:
                stale = [
                    entry.name for entry in entries
                    if _PASS_ID.fullmatch(entry.name) and entry.name not in keep_ids
                ]
        except OSError as exc:
            _log.warning("Meteor could not scan gallery for overflow: %s", exc)
            return
        for stale_id in stale:
            try:
                _remove_dir_descriptor(root_fd, stale_id)
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.warning("Meteor could not prune old pass %s: %s", stale_id, exc)

    @contextmanager
    def _work(self):
        with (_directory(self.data_dir, private=False) as data,
              _directory("meteor", data, create=True) as root,
              _directory("work", root, create=True) as work):
            yield work

    def _prepare_launch(self, _freq_hz, _number):
        mode, hz = _settings()
        if not self._which(SATDUMP):
            raise MeteorUnavailable("satdump is not installed, so SIGINT 101 cannot decode Meteor")
        if not self._radio_present():
            raise MeteorUnavailable("no RTL-SDR on the USB bus — plug in the RTL-SDR")
        try:
            devices = tuple(self._list_devices())
            pin = os.environ.get(RTLSDR_SERIAL_ENV)
            if pin and sum(device.serial == pin for device in devices) > 1:
                raise DeviceSelectionError(
                    f"serial {pin} matches more than one RTL-SDR. "
                    f"Found: {describe_devices(devices)}")
            radio = rtlsdr_radio_for_run(0, pin, lambda: devices)
            with self._work() as work:
                if not os.access(".", os.W_OK | os.X_OK, dir_fd=work):
                    raise PermissionError("Meteor work directory is not writable")
            self.sweep_work()
        except (OSError, ValueError, DeviceSelectionError) as exc:
            raise MeteorUnavailable(f"cannot prepare Meteor: {exc}" if isinstance(exc, OSError)
                                    else str(exc)) from exc
        capture = _Capture(uuid4().hex, mode, hz, radio)
        root = self.data_dir / "meteor"
        output = root / "work" / capture.id
        selector = ("--source_id", radio["serial"]) if radio["serial"] else ()
        capture.argv = (SATDUMP, "live", PIPELINES[mode], str(output), "--source", "rtlsdr",
                        *selector, "--samplerate", "1024000", "--frequency", str(hz),
                        "--gain", "49.6", "--timeout", str(CAPTURE_S),
                        "--tle_override", str(root / "tle.txt"), "--finish_processing")

        def launch():
            try:
                with self._work() as work:
                    capture.stage = _Stage.create(output.parent, work, capture.id)
                capture.launched = self._monotonic()
                capture.started_at = self._clock()
                capture.deadline = capture.launched + CAPTURE_S + FINISH_CAP_S
                proc = self._spawn(capture.argv, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   pass_fds=(capture.stage.lock_fd,))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self._discard_stage(capture)
                raise MeteorUnavailable(f"cannot run SatDump: {exc}") from exc
            try:
                child = _SatDumpProcess(proc)
            except (AttributeError, ValueError) as exc:
                raise PartialLaunch(proc, exc) from exc
            self.freq_hz = hz
            return child

        return launch, (capture,)

    def launch_start(self, intent, *, claimed=False):
        super().launch_start(intent, claimed=claimed)
        return intent is not None

    def _read(self, *_):
        """The supervisor calls the run-aware reader below."""

    def _read_run(self, run):
        capture, = run.args
        try:
            stream = run.proc.stdout
            while line := stream.readline(_MAX_LINE + 1):
                if len(line) > _MAX_LINE:
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(_MAX_LINE + 1)
                    if not line:
                        break
                    continue
                self._line(run, capture, line.decode("utf-8", errors="replace"))
            capture.eof.set()
        except (OSError, ValueError) as exc:
            with capture.control:
                capture.tail.append(f"stderr read failed: {exc}"[:160])
        finally:
            run.wake.set()
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _line(self, run, capture, line):
        message = _LOG_PREFIX.sub("", _ANSI.sub("", line).strip(), count=1)
        with capture.control:
            if capture.result is not None:
                return
            if message:
                capture.tail.append(message[:160])
            if message == _CLOSE_MARKER and capture.marker_at is None:
                if capture.interrupted is None and not run.cancel.is_set() and not self._closing:
                    capture.marker_at = self._monotonic()
                    capture.deadline = min(capture.deadline, capture.marker_at + FINISH_CAP_S)
                    run.physical.set()
                    capture.handoff_done.set()
                    run.wake.set()
            elif match := _STATUS.fullmatch(message):
                capture.reports = match.groups()
                capture.lock_ever |= capture.lock == "locked"

    def release(self, *, for_claim=False):
        deadline = self._monotonic() + RELEASE_GRACE_S + KILL_GRACE_S
        with self._life:
            if for_claim and self.release_failure():
                return
            self._released = True
            intent = self._cancel_intent()
            run = self._run
            if run and not run.ready.is_set():
                self._fence(run)
                capture, = run.args
                with capture.control:
                    if not run.physical.is_set() and capture.handoff_deadline is None:
                        capture.handoff_deadline = deadline - KILL_GRACE_S
                    if capture.handoff_deadline is not None:
                        deadline = min(deadline, capture.handoff_deadline + KILL_GRACE_S)
                run.wake.set()
            retry = None if for_claim else self._request_retry(run)
        if intent:
            intent.done.wait(max(0, deadline - self._monotonic()))
            run = intent.run or run
        if run and not run.ready.is_set():
            (retry or run.args[0].handoff_done).wait(max(0, deadline - self._monotonic()))

    def release_failure(self, *, pending=False):
        intent, run = self._intent, self._run
        if pending and intent and intent.spawning and intent.cancelled and intent.run is None:
            return "SatDump is still opening the radio"
        if run and not run.physical.is_set():
            if run.failure:
                return run.failure
            if pending:
                return "SatDump is still releasing the radio"

    def _cancel(self):
        intent = self._cancel_intent()
        run = self._run
        if run and not run.ready.is_set():
            self._fence(run)
            capture, = run.args
            with capture.control:
                if self._closing:
                    self._interrupt(run, capture, "shutdown", "Console shut down.")
                else:
                    self._interrupt(run, capture, "stopped", "SatDump was stopped.")
            run.wake.set()
        return run, intent, self._request_retry(run)

    def _interrupt(self, run, capture, cause, reason):
        if capture.interrupted is None:
            capture.ended_by = cause
            capture.interrupted = reason
        run.cancel.set()

    def _observe_exit(self, run):
        capture, = run.args
        priority_set = False
        while True:
            run.wake.clear()
            code = run.proc.poll()
            if code is not None:
                return [(run.proc, code)], self._monotonic()
            with capture.control:
                now = self._monotonic()
                if self._closing:
                    self._interrupt(run, capture, "shutdown", "Console shut down.")
                elif run.cancel.is_set() and capture.interrupted is None:
                    self._interrupt(run, capture, "handoff", "SatDump launch was cancelled.")
                elif now >= capture.deadline:
                    self._interrupt(run, capture, "watchdog",
                                    "SatDump exceeded its finishing deadline.")
                if run.cancel.is_set():
                    return [], None
                if run.physical.is_set():
                    lower_priority = not priority_set
                    deadline = capture.deadline
                else:
                    lower_priority = False
                    release = capture.handoff_deadline
                    if release is not None:
                        if now >= release:
                            self._interrupt(run, capture, "handoff",
                                            "SatDump did not release the radio in time.")
                            capture.kill_deadline = release + KILL_GRACE_S
                            return [], None
                        if not capture.term_sent:
                            capture.term_sent = True
                            run.proc.terminate()
                    deadline = capture.deadline if release is None else min(
                        capture.deadline, release)
            if lower_priority:
                priority_set = True
                try:
                    # Linux applies this priority to SatDump's leader thread.
                    run.proc.lower_priority(FINISH_NICE)
                except (OSError, ValueError) as exc:
                    run.diagnostic_fault = f"SatDump priority could not be lowered: {exc}"[:160]
            run.wake.wait(min(receiver_lifecycle.SUPERVISOR_POLL_S,
                              max(0, deadline - self._monotonic())))

    def _stop_run(self, run, *, kill_only=False, wait_s=None):
        capture, = run.args

        def interrupt():
            with capture.control:
                if capture.interrupted is None:
                    self._interrupt(run, capture, "handoff", "SatDump cleanup was interrupted.")

        proc = run.proc.proc if isinstance(run.proc, _SatDumpProcess) else run.proc
        return childproc.stop_and_confirm(
            proc, kill_only=True, wait_s=KILL_GRACE_S if wait_s is None else wait_s,
            deadline=None if kill_only else capture.kill_deadline,
            retry_kill_wait=True, before_kill=interrupt, clock=self._monotonic,
        )

    def _stop_reason(self, run, result):
        reason = self._unreaped_reason(run, timed_out=result.timed_out)
        if result.error is not None:
            return f"{reason}: {str(result.error)[:160]}"
        return reason

    def _unreaped_reason(self, run, *, timed_out=False):
        result = "survived kill" if timed_out else "could not be confirmed stopped"
        if run.physical.is_set():
            return f"SatDump finisher {result}"
        return f"SatDump {result}; the dongle may still be held"

    def _physical_wait_event(self, run):
        # The attempt can finish with a failed reap while the dongle is still held.
        capture, = run.args
        return capture.handoff_done

    def _notify_physical(self, run):
        capture, = run.args
        if run.reaped:
            run.physical.set()
        if run.reaped or run.reap_failed:
            capture.handoff_done.set()

    def _drain_reader(self, _run):
        """Wait for explicit EOF during retirement, including cancelled and retried exits."""

    def _retire_reaped(self, run, observed_at):
        capture, = run.args
        capture.ended_at = self._clock()
        eof = run.reader is not None and capture.eof.wait(receiver_lifecycle.STREAM_EOF_S)
        code = run.exit_code = run.proc.poll()
        if eof and code == 0 and capture.stage is not None and not run.cancel.is_set():
            try:
                fd = capture.stage.fd
                capture.pictures = tuple(_pictures(fd, os.fstat(fd).st_dev))
            except OSError as exc:
                run.diagnostic_fault = f"could not inspect SatDump work: {exc}"[:160]
        with capture.control:
            phase, reason = self._classify(capture, code, eof)
            if phase != "done":
                capture.pictures = ()
            if capture.ended_by is None:
                capture.ended_by = ("handoff" if capture.term_sent else
                                    "timeout" if self._monotonic() >= capture.launched + CAPTURE_S
                                    else "self_exit")
        if phase == "done":
            published = self._publish_pass(run, capture)
            if not published:
                phase = "no_picture"
                reason = capture.publish_error or "Meteor pass could not be published."
        with capture.control:
            capture.result = phase, reason
        if phase != "done":
            self._record_quiet_run(capture, phase, reason)
        if phase == "stopped":
            when = self._monotonic() if observed_at is None else observed_at
            with self._runs:
                if self._runs.is_current(run.number):
                    run.unreported_death = bool(run.watch.observed(
                        run.proc, reason, when))
                    if run.unreported_death:
                        self.error = reason
        self._discard_stage(capture)

    @staticmethod
    def _classify(capture, code, eof):
        if capture.interrupted is not None:
            return "interrupted", capture.interrupted
        if not eof:
            return "interrupted", "SatDump stderr did not reach EOF."
        if code == 0:
            if capture.pictures:
                return "done", None
            if capture.marker_at is None:
                return "no_picture", "The close marker was never seen."
            if capture.lock_ever:
                return "no_picture", "The signal locked but no picture was decoded."
            if capture.lock == "unknown":
                return "no_picture", "SatDump reported no lock status or picture."
            return "no_picture", "No signal lock or picture was decoded."
        detail = ": " + capture.tail[-1] if capture.tail else ""
        if capture.marker_at is not None:
            return "no_picture", f"SatDump could not finish decoding (exit {code}){detail}"
        if capture.term_sent:
            return "interrupted", f"SatDump stopped during the radio handoff (exit {code})."
        return "stopped", f"SatDump stopped before releasing the radio (exit {code}){detail}"

    @staticmethod
    def _discard_stage(capture):
        if capture.stage is not None:
            try:
                capture.stage.close()
            except OSError as exc:
                capture.tail.append(f"could not discard SatDump work: {exc}"[:160])

    def shutdown(self):
        # Reuse the report budget, and also check a surviving finisher whose radio is free.
        super().shutdown()
        run = self._run
        if run and not run.reaped:
            raise MeteorUnavailable(run.failure or self._unreaped_reason(run))

    def live_command(self):
        run = self._run
        return shlex.join(run.args[0].argv) if run and not run.ready.is_set() else None

    def snapshot(self):
        run = self._run
        body = {"available": True, "passes": self.passes(), "command": None}
        if run is None:
            try:
                mode, hz = _settings()
            except MeteorUnavailable:
                mode, hz = None, None
            return {**body, "state": "idle", "reason": "Ready to listen", "mode": mode,
                    "freq_hz": hz, "lock": "unknown", "lock_ever": False, "radio": None,
                    "started_at": None, "ended_at": None, "ended_by": None, "elapsed_s": None}
        capture, = run.args
        with capture.control:
            lock = capture.lock
            if capture.result is not None:
                state, reason = capture.result
            elif capture.marker_at is not None:
                state, reason = "decoding", None
            elif self.watch.settling():
                state, reason = "starting", None
            elif lock == "locked":
                state, reason = "receiving", None
            else:
                state = "searching"
                reason = f"Listening on {capture.hz / 1e6:.3f} MHz · searching"
            now = self._monotonic()
            elapsed = ((now - capture.marker_at) if state == "decoding" else
                       (now - capture.launched)) if capture.result is None else None
            body.update(state=state, reason=reason, lock=lock, lock_ever=capture.lock_ever,
                        mode=capture.mode, freq_hz=capture.hz, radio=capture.radio.copy(),
                        command=shlex.join(capture.argv) if capture.result is None else None,
                        started_at=capture.started_at, ended_at=capture.ended_at,
                        ended_by=capture.ended_by, elapsed_s=elapsed)
        if state == "stopped":
            body["retry_in"] = round(self.watch.retry_in() or 0, 1)
        return body
