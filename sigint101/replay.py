# ABOUTME: Validates local cu8 SigMF pairs and replays their samples through a finite rtl_433 child.
# ABOUTME: Keeps replay output, events, and progress separate from live radio receptions.

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from sqlite3 import Error as SqliteError
from threading import Lock

from .childproc import ChildWatch, RunGuard, command_line, spawn
from .decode import Decode, parse_decode_line
from .events import log_event
from .hardware import RTL_433
from .operate import instrument, validate_decoder_hz
from .receiver_lifecycle import DIAGNOSTIC_WAIT_S, SupervisedReceiver, daemon
from .spectrum import TuneError

RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "recordings"
REPLAY_MAX_S = 30.0
_MAX_META = 65536
_MAX_PAIR = 5_000_000
_MAX_FOLDER = 25_000_000
_MAX_READS = 40
_META = ".sigmf-meta"
_DATA = ".sigmf-data"
_STEM = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_CHANGED = "the recording changed since it was listed"
_CAP_REASON = "the replay ran past 30 s and was stopped"


class RecordingRefused(ValueError):
    """The recording exists but does not meet the replay input contract."""


class RecordingNotFound(LookupError):
    """There is no SigMF metadata file with this stem."""


class ReplayUnavailable(RuntimeError):
    """A replay could not acquire or retain its offline decoder."""


@dataclass(frozen=True)
class Recording:
    """Validated index metadata and the file identities checked again at use."""

    stem: str
    instrument: str
    frequency_hz: int
    sample_rate: int
    datetime: str
    hw: str
    license: str
    source: str
    bytes: int
    _root: Path
    _root_id: tuple[int, int]
    _meta_id: tuple[int, int, int, int]
    _data_id: tuple[int, int, int, int]
    _sha512: str | None

    @property
    def input(self) -> str:
        return f"recordings/{self.stem}{_DATA}"


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@contextmanager
def _directory(root):
    fd = os.open(root, _READ_FLAGS | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def _file_info(directory, name):
    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise RecordingRefused(f"{name} must be a regular file.")
    return info


def _open_checked(directory, name, identity):
    # If the file was replaced by a FIFO after the inventory, open must not block.
    fd = os.open(name, _READ_FLAGS | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or _identity(info) != identity:
            raise RecordingRefused(_CHANGED)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _reject_constant(token):
    raise ValueError(token)


def _check(condition, name, rule):
    if not condition:
        raise RecordingRefused(f"{name} {rule}.")


def _integer(value):
    return type(value) is int or (type(value) is float and value.is_integer())


def _number(obj, name, minimum, maximum, *, default=None):
    value = obj.get(name, default)
    _check(_integer(value) and minimum <= value <= maximum, name,
           f"must be an integer from {minimum} to {maximum}")
    return int(value)


def _string(obj, name, maximum=None):
    value = obj.get(name)
    valid = isinstance(value, str) and bool(value.strip())
    _check(valid and (maximum is None or len(value) <= maximum), name,
           "must be a nonblank string" if maximum is None
           else f"must be a nonblank string of at most {maximum} characters")
    return value


def _validate_meta(meta):
    _check(isinstance(meta, dict), "metadata", "must be a JSON object")
    global_ = meta.get("global")
    _check(isinstance(global_, dict), "global", "must be an object")
    version = _string(global_, "core:version")
    _check(re.fullmatch(r"1\.[0-9]+\.[0-9]+", version), "core:version", "must be SigMF 1.x")
    _check(global_.get("core:datatype") == "cu8", "core:datatype", "must be cu8")
    rate = _number(global_, "core:sample_rate", 1, 3_200_000)
    hw = _string(global_, "core:hw", 200)
    license_ = _string(global_, "core:license")
    extensions = global_.get("core:extensions")
    _check(isinstance(extensions, list) and any(
        isinstance(item, dict) and item.get("name") == "sigint101" for item in extensions
    ), "core:extensions", "must declare sigint101")
    source = _string(global_, "sigint101:source")
    _check(global_.get("sigint101:instrument") == "ism", "sigint101:instrument", "must be ism")
    _number(global_, "core:num_channels", 1, 1, default=1)
    _number(global_, "core:trailing_bytes", 0, 0, default=0)
    _check(global_.get("core:metadata_only", False) is False, "core:metadata_only", "must be false")
    for field_name in ("core:dataset", "core:geolocation"):
        _check(field_name not in global_, field_name, "must be absent")
    sha512 = None
    if "core:sha512" in global_:
        sha512 = _string(global_, "core:sha512")
        _check(re.fullmatch(r"[0-9a-fA-F]{128}", sha512), "core:sha512",
               "must be a SHA-512 hex digest")

    captures = meta.get("captures")
    _check(isinstance(captures, list) and len(captures) == 1 and isinstance(captures[0], dict),
           "captures", "must have exactly one segment")
    segment = captures[0]
    _number(segment, "core:sample_start", 0, 0)
    freq = segment.get("core:frequency")
    _check(_integer(freq), "core:frequency", "must be integral Hz")
    try:
        freq = validate_decoder_hz(instrument("ism"), freq)
    except (TuneError, OverflowError) as exc:
        raise RecordingRefused("core:frequency must be inside the ISM band.") from exc
    captured = _string(segment, "core:datetime")
    try:
        if not captured.endswith("Z") or "T" not in captured:
            raise ValueError("not a UTC timestamp")
        datetime.fromisoformat(captured[:-1] + "+00:00")
    except ValueError as exc:
        raise RecordingRefused(
            "core:datetime must be an ISO-8601 UTC timestamp ending in Z."
        ) from exc
    _number(segment, "core:header_bytes", 0, 0, default=0)
    _check(isinstance(meta.get("annotations"), list), "annotations", "must be a list")
    return freq, rate, captured, hw, license_, source, sha512


def _read_meta(directory, name, identity):
    with os.fdopen(_open_checked(directory, name, identity), "rb") as handle:
        raw = handle.read(_MAX_META + 1)
        if _identity(os.fstat(handle.fileno())) != identity:
            raise RecordingRefused(_CHANGED)
    _check(len(raw) <= _MAX_META, "metadata", "must be at most 65,536 bytes")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise RecordingRefused("metadata must be UTF-8 JSON without NaN or Infinity.") from exc


def _valid_stem(stem):
    _check(isinstance(stem, str) and _STEM.fullmatch(stem), "recording stem",
           "must be 1–63 lowercase ASCII letters, digits or hyphens, "
           "starting with a letter or digit")


def _load(root, directory, stem):
    _valid_stem(stem)
    meta_name = stem + _META
    try:
        meta_info = _file_info(directory, meta_name)
    except FileNotFoundError as exc:
        raise RecordingNotFound("recording metadata was not found") from exc
    try:
        data_info = _file_info(directory, stem + _DATA)
    except FileNotFoundError as exc:
        raise RecordingRefused("the .sigmf-data file is missing") from exc
    _check(meta_info.st_size <= _MAX_META, "metadata", "must be at most 65,536 bytes")
    size = meta_info.st_size + data_info.st_size
    _check(size <= _MAX_PAIR, "recording pair", "must be at most 5 MB")
    _check(data_info.st_size > 0 and data_info.st_size % 2 == 0,
           "data", "must contain a nonempty, even number of bytes")
    meta_id, data_id = _identity(meta_info), _identity(data_info)
    freq, rate, captured, hw, license_, source, sha512 = _validate_meta(
        _read_meta(directory, meta_name, meta_id)
    )
    root_info = os.fstat(directory)
    return Recording(stem, "ism", freq, rate, captured, hw, license_, source, size,
                     root, (root_info.st_dev, root_info.st_ino), meta_id, data_id, sha512)


def _folder_entries(directory):
    total = 0
    stems = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name.endswith(_META):
                stems.append(entry.name[:-len(_META)])
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return sorted(stems), total


def load_recording(recordings_dir, stem) -> Recording:
    _valid_stem(stem)
    try:
        root = Path(recordings_dir).resolve(strict=True)
        with _directory(root) as directory:
            _, size = _folder_entries(directory)
            _check(size <= _MAX_FOLDER, "recordings folder", "must be at most 25 MB")
            return _load(root, directory, stem)
    except FileNotFoundError as exc:
        raise RecordingNotFound("recording metadata was not found") from exc
    except (OSError, RuntimeError) as exc:
        raise RecordingRefused("the recording files could not be read") from exc


def list_recordings(recordings_dir) -> tuple[list[Recording], list[tuple[str, str]]]:
    recordings, refused = [], []
    try:
        root = Path(recordings_dir).resolve(strict=True)
        with _directory(root) as directory:
            stems, size = _folder_entries(directory)
            if size > _MAX_FOLDER:
                return [], [(stem, "recordings folder must be at most 25 MB") for stem in stems]
            for stem in stems:
                try:
                    recordings.append(_load(root, directory, stem))
                except (RecordingRefused, RecordingNotFound) as exc:
                    refused.append((stem, str(exc)))
                except OSError:
                    refused.append((stem, "the recording files could not be read"))
    except FileNotFoundError:
        return [], []
    except (OSError, RuntimeError) as exc:
        raise RecordingRefused("the recordings folder could not be read") from exc
    return recordings, refused


def open_data(recording: Recording) -> int:
    _valid_stem(recording.stem)
    try:
        with _directory(recording._root) as directory:
            info = os.fstat(directory)
            meta = _file_info(directory, recording.stem + _META)
            if ((info.st_dev, info.st_ino) != recording._root_id
                    or _identity(meta) != recording._meta_id):
                raise RecordingRefused(_CHANGED)
            return _open_checked(directory, recording.stem + _DATA, recording._data_id)
    except (OSError, RecordingRefused) as exc:
        raise RecordingRefused(_CHANGED) from exc


def digest_of(recording: Recording) -> str:
    with os.fdopen(open_data(recording), "rb") as handle:
        try:
            size = recording._data_id[2]
            data = handle.read(size + 1)
            if len(data) != size or _identity(os.fstat(handle.fileno())) != recording._data_id:
                raise RecordingRefused(_CHANGED)
        except OSError as exc:
            raise RecordingRefused(_CHANGED) from exc
    expected = recording._sha512
    if expected is not None and hashlib.sha512(data).hexdigest() != expected.lower():
        raise RecordingRefused("core:sha512 does not match the recording data.")
    return hashlib.sha256(data).hexdigest()


def replay_command(recording: Recording) -> list[str]:
    return [RTL_433, "-F", "json", "-M", "level", "-s", str(recording.sample_rate),
            "-f", str(recording.frequency_hz), "-r", "cu8:-"]


@dataclass
class _Playback:
    recording: Recording
    digest: str
    reads: deque[Decode] = field(default_factory=lambda: deque(maxlen=_MAX_READS))
    items: int = 0
    command: str | None = None
    deadline: float = 0.0
    capped: bool = False
    journal_lock: Lock = field(default_factory=Lock)
    logged_start: bool = False


class ReplayDecoder(SupervisedReceiver):
    """Owns a finite file decode and its separate, bounded replay history."""

    _unavailable = ReplayUnavailable
    _released_reason = "the replay was released"

    def __init__(self, *, events_path=None, progress=None, sync=False,
                 spawn=spawn, which=shutil.which):
        self._spawn = spawn
        self._which = which
        self._events_path = events_path
        self._progress = progress
        self._sync = sync
        # Only competing starts share this lock. Stop and shutdown remain C40 controls.
        self._starting = Lock()
        self._recording = None
        self._start_error = None
        self._runs = RunGuard()
        self._init_lifecycle()
        self.watch = ChildWatch(RTL_433, finite=True)
        self.error = None

    @property
    def on_reception(self):
        return None

    def _prepare_launch(self, _freq_hz, _number):
        recording = self._recording
        if recording is None:
            raise ReplayUnavailable("no recording was selected")
        if not self._which(RTL_433):
            raise ReplayUnavailable(
                "rtl_433 is not installed, so SIGINT 101 cannot replay the recording"
            )
        playback = _Playback(recording, digest_of(recording))
        argv = replay_command(recording)

        def launch():
            fd = open_data(recording)
            try:
                proc = self._spawn(argv, stdin=fd, stdout=subprocess.PIPE, text=True)
            except (OSError, ValueError) as exc:
                raise ReplayUnavailable(f"cannot run rtl_433: {exc}") from exc
            finally:
                os.close(fd)
            playback.command = command_line(proc)
            playback.deadline = time.monotonic() + REPLAY_MAX_S
            return proc

        return launch, (playback,)

    def start_replay(self, recording: Recording) -> None:
        if not self._starting.acquire(blocking=False):
            raise ReplayUnavailable("a replay is already starting")
        try:
            if reason := self.release_failure(pending=True):
                raise ReplayUnavailable(reason)
            if self.running:
                raise ReplayUnavailable("a replay is already running")
            self._recording = recording
            try:
                intent = self.prepare_start()
                self.launch_start(intent)
            except (ReplayUnavailable, RecordingRefused) as exc:
                self._start_error = recording, str(exc)
                raise
            self._start_error = None
            daemon(self._expire, intent.run, name="sigint101-replay-cap")
        finally:
            self._recording = None
            self._starting.release()

    def _expire(self, run):
        playback = run.args[0]
        if run.complete.wait(max(0, playback.deadline - time.monotonic())):
            return
        # Prevent replacement between the identity check and the shared stop.
        with self._starting:
            with self._life:
                if self._run is not run or not self.running:
                    return
                playback.capped = True
            try:
                self.stop()
            except ReplayUnavailable:
                # A still-retiring run remains owned and reports its lifecycle failure.
                pass

    def _append_event(self, kind, payload):
        if self._events_path is not None:
            log_event(self._events_path, kind, payload, sync=self._sync)

    def _journal(self, playback, kind, payload):
        """Append with the run's journal lock held and the run guard released."""
        recording = playback.recording
        base = {"instrument": recording.instrument, "recording": recording.stem}
        if not playback.logged_start:
            self._append_event("replay_start", {
                **base, "digest": playback.digest, "command": playback.command,
                "input": recording.input,
            })
            playback.logged_start = True
        if kind is not None:
            self._append_event(kind, {**base, **payload})

    def _read(self, proc, run, playback):
        try:
            with playback.journal_lock:
                self._journal(playback, None, {})
            for raw in proc.stdout:
                row = parse_decode_line(raw)
                if row is None:
                    continue
                # Reserve this journal write before admitting the row. A stop can still
                # fence and reap promptly; its end event waits for the admitted write.
                with playback.journal_lock:
                    with self._runs:
                        if not self._runs.is_current(run):
                            return
                        playback.reads.append(row)
                        playback.items += 1
                        count = playback.items
                    if count <= _MAX_READS:
                        self._journal(playback, "replay", {
                            "digest": playback.digest, "replay": True, "source": "replay",
                            "item": row.as_dict(),
                        })
                    if count == 1 and self._progress is not None:
                        self._progress.record_replay_seen(
                            playback.recording.instrument, recording=playback.recording.stem,
                            digest=playback.digest, ts=time.time(),
                        )
        except (OSError, ValueError, OverflowError, SqliteError) as exc:
            with self._runs:
                if self._runs.is_current(run):
                    self.error = str(exc)

    def _state(self, run):
        if run is None:
            return "idle", None
        if run.args[0].capped:
            return "error", _CAP_REASON
        reason = run.failure or self.watch.reason or self.error
        if reason:
            return "error", reason
        if run.exit_code == 0 and run.ready.is_set():
            return "finished", None
        if run.cancel.is_set() and run.exit_code != 0:
            return "stopped", None
        return "replaying", None

    def _report(self, run):
        if run.exit_code not in (None, 0):
            run.diagnosed.wait(DIAGNOSTIC_WAIT_S)
        playback = run.args[0]
        exit_code = run.exit_code if run.exit_code is not None else run.proc.poll()
        with self._runs:
            state, reason = self._state(run)
            if state == "replaying" and run.exit_code == 0:
                state = "finished"
            payload = {"state": state, "exit_code": exit_code, "reason": reason,
                       "items": playback.items}
        try:
            with playback.journal_lock:
                self._journal(playback, "replay_end", payload)
        except OSError as exc:
            with self._runs:
                self.error = str(exc)

    def snapshot(self) -> dict:
        """Read the current replay without starting a child or touching an SDR."""
        with self._life, self._runs:
            run = self._run
            state, reason = self._state(run)
            playback = run.args[0] if run else None
            recording = playback.recording if playback else None
            if self._start_error is not None:
                recording, reason = self._start_error
                playback = None
                state = "error"
            if run and run.unreported_death and reason and reason == self.watch.reason:
                # A poll has shown this diagnostic. The watch still owns its timed back-off.
                run.unreported_death = False
                self.error = None
            return {
                "mode": "replay", "replay": True, "instrument": "ism", "state": state,
                "recording": recording.stem if recording else None,
                "digest": playback.digest if playback else None,
                "frequency_hz": recording.frequency_hz if recording else None,
                "sample_rate": recording.sample_rate if recording else None,
                "command": playback.command if playback else None,
                "input": recording.input if recording else None,
                "decodes": [row.as_dict() for row in playback.reads] if playback else [],
                "reason": reason, "retry_in": self.watch.retry_in(),
            }
