# ABOUTME: Decodes SSTV (the ISS downlink, 437.550 MHz) via rtl_fm|sox|sstv for M25 — a picture.
# ABOUTME: No radio, no tools, no signal — reports an empty gallery, never a placeholder image.

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
import weakref
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from . import childproc, receiver_lifecycle
from .childproc import (
    ChildWatch,
    Pipeline,
    RunGuard,
    command_line,
    spawn,
)
from .hardware import (
    RTL_FM,
    DeviceSelectionError,
    probe_rtlsdr,
    rtlsdr_radio_for_run,
)
from .receiver_lifecycle import PartialLaunch, SupervisedReceiver

# The colaclanth/sstv command-line decoder. It turns one WAV of SSTV audio into one PNG, printing
# the mode it detected to stdout. Its binary name lives here, not in hardware.py, so parallel
# builders do not collide over the shared module.
SSTV = "sstv"

# sox bridges the two: rtl_fm emits raw 16-bit PCM, and sstv reads an audio file, so sox wraps the
# stream in a WAV container (and bounds it — see pipeline_commands) before sstv ever sees it.
SOX = "sox"

# Where the SSTV lesson listens: the ISS SSTV downlink. ARISS has sent its SSTV events on
# 437.550 MHz FM in Robot36 since 2026 (allow about +/-10 kHz of Doppler); through October 2025
# they ran on 145.800 MHz FM, mostly PD120. A caller can pass another frequency via freq_hz.
DEFAULT_SSTV_HZ = 437_550_000

# The audio rate the pipeline runs at. SSTV occupies roughly 1200-2300 Hz, so this is ample; sstv
# resamples to its own internal rate regardless.
SSTV_SAMPLE_RATE = 48_000

# How many seconds of audio each capture records before the WAV stream closes and sstv decodes it.
# A full-resolution SSTV frame runs under two minutes, so this spans one picture.
DEFAULT_CAPTURE_S = 120

# Where decoded pictures land, and the base their reported paths are relative to. output_dir sits
# under data_dir so the console can serve the files it lists.
DEFAULT_OUTPUT_DIR = "data/sstv"

# sstv prints exactly this, then the mode's name, when it locks onto a transmission.
_MODE_PREFIX = "Detected SSTV mode "

_log = logging.getLogger(__name__)

# Each decoder stages in a subfolder of the staging folder that it owns, named by a random uuid4
# and held under an exclusive flock on the lock file inside it for as long as the decoder lives.
# The lock, not any file name, says whether the owner is gone. A subfolder is only ever given its
# owner's name once its lock is held, so no sweep can find it unlocked while its owner is alive.
_OWNER_NAME = re.compile(r"[0-9a-f]{32}")
_OWNER_LOCK = ".owner.lock"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_SHARED_WRITE = stat.S_IWGRP | stat.S_IWOTH


class SstvUnavailable(RuntimeError):
    """The SSTV stream cannot be read. Never a reason to emit a placeholder — gallery stays bare."""


class _UntrustedStaging(OSError):
    """A staging folder or subfolder this console cannot identify as its own to clean."""


def _require_private_dir(info: os.stat_result, what: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise _UntrustedStaging(f"the {what} is not a directory")
    if info.st_uid != os.geteuid():
        raise _UntrustedStaging(f"the {what} belongs to another account")
    if info.st_mode & _SHARED_WRITE:
        raise _UntrustedStaging(f"the {what} is writable by other accounts")


def _open_staging(parent: Path, staging_name: str, gallery_name: str) -> int:
    """Open the staging folder by descriptor and prove it is this account's, not the gallery.

    The data folder is the configured root and is opened as given. Below it nothing is followed:
    the gallery and the staging folder are each opened relative to the root's descriptor, and the
    staging folder is compared with the gallery by the descriptors' own fstat.
    """
    root = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        gallery_fd = os.open(gallery_name, _DIR_FLAGS, dir_fd=root)
        try:
            gallery = os.fstat(gallery_fd)
        finally:
            os.close(gallery_fd)
        try:
            os.mkdir(staging_name, 0o700, dir_fd=root)
        except FileExistsError:
            pass
        fd = os.open(staging_name, _DIR_FLAGS, dir_fd=root)
        try:
            info = os.fstat(fd)
            if os.path.samestat(info, gallery) or os.path.samestat(info, os.fstat(root)):
                raise _UntrustedStaging("the staging folder is the gallery or the data folder")
            if info.st_mode & _SHARED_WRITE and info.st_uid == os.geteuid():
                # Earlier versions made it under the umask. It is this account's: close it.
                os.fchmod(fd, stat.S_IMODE(info.st_mode) & ~_SHARED_WRITE)
                info = os.fstat(fd)
            _require_private_dir(info, "staging folder")
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(root)
    return fd


def _empty_owned(staging_fd: int, name: str, sub_fd: int, sub_info: os.stat_result) -> None:
    """Remove a subfolder whose lock this caller holds: its entries, its lock, then itself.

    Every entry belongs to the subfolder's owner. unlink never follows a link, so a symlink goes as
    a link and its target stays. A nested directory is not entered: it stays, and so do the lock
    and the subfolder, which a later sweep tries again.
    """
    left = False
    for entry in os.listdir(sub_fd):
        if entry == _OWNER_LOCK:
            continue
        try:
            os.unlink(entry, dir_fd=sub_fd)
        except FileNotFoundError:
            pass
        except OSError:
            left = True
    if left:
        return
    os.unlink(_OWNER_LOCK, dir_fd=sub_fd)
    # rmdir removes only an empty directory, and only the one this descriptor opened.
    if os.path.samestat(os.stat(name, dir_fd=staging_fd, follow_symlinks=False), sub_info):
        os.rmdir(name, dir_fd=staging_fd)


def _sweep_retired(staging_fd: int, name: str) -> None:
    """Remove one subfolder of the staging folder if its owner is gone: its lock is free."""
    sub_fd = os.open(name, _DIR_FLAGS, dir_fd=staging_fd)
    try:
        sub_info = os.fstat(sub_fd)
        _require_private_dir(sub_info, f"staging subfolder {name}")
        # O_NONBLOCK: a FIFO in the lock's place must not hold up the console's start.
        lock_fd = os.open(_OWNER_LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                          dir_fd=sub_fd)
        try:
            lock = os.fstat(lock_fd)
            if not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.geteuid():
                raise _UntrustedStaging(f"the lock in staging subfolder {name} is not this "
                                        "account's regular file")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # Its owner, or a stage of its owner's, is alive.
            if not os.path.samestat(
                    os.stat(_OWNER_LOCK, dir_fd=sub_fd, follow_symlinks=False), lock):
                return  # Another sweep retired it first.
            _empty_owned(staging_fd, name, sub_fd, sub_info)
        finally:
            os.close(lock_fd)
    finally:
        os.close(sub_fd)


def _close_all(fds: tuple) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


class _StagingOwner:
    """One decoder's own staging subfolder, held under its flock until the decoder retires.

    The descriptors close when the owner is retired or garbage-collected, which frees the lock
    unless a pipeline stage that inherited it is still running.
    """

    def __init__(self, staging_fd: int, name: str, sub_fd: int, lock_fd: int):
        self.staging_fd = staging_fd
        self.name = name
        self.sub_fd = sub_fd
        self.lock_fd = lock_fd
        self.sub_info = os.fstat(sub_fd)
        self._close = weakref.finalize(self, _close_all, (lock_fd, sub_fd, staging_fd))

    @classmethod
    def create(cls, parent: Path, staging_name: str, gallery_name: str) -> _StagingOwner:
        staging_fd = _open_staging(parent, staging_name, gallery_name)
        name = uuid.uuid4().hex
        pending = f".new-{name}"
        sub_fd = lock_fd = None
        try:
            os.mkdir(pending, 0o700, dir_fd=staging_fd)
            sub_fd = os.open(pending, _DIR_FLAGS, dir_fd=staging_fd)
            lock_fd = os.open(_OWNER_LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                              | os.O_CLOEXEC, 0o600, dir_fd=sub_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.rename(pending, name, src_dir_fd=staging_fd, dst_dir_fd=staging_fd)
            return cls(staging_fd, name, sub_fd, lock_fd)
        except BaseException:
            if lock_fd is not None:
                _close_all((lock_fd,))
                try:
                    os.unlink(_OWNER_LOCK, dir_fd=sub_fd)
                except OSError:
                    pass
            if sub_fd is not None:
                _close_all((sub_fd,))
            try:
                os.rmdir(pending, dir_fd=staging_fd)
            except OSError:
                pass
            _close_all((staging_fd,))
            raise

    def sweep(self) -> None:
        """Remove every other subfolder whose owner is gone. Live owners' locks cannot be taken."""
        for name in os.listdir(self.staging_fd):
            if name == self.name or not _OWNER_NAME.fullmatch(name):
                continue
            try:
                _sweep_retired(self.staging_fd, name)
            except FileNotFoundError:
                pass  # Retired by another sweep, or never an owner's.
            except OSError as exc:
                _log.warning("SSTV left staging subfolder %s in place: %s", name, exc)

    def inherited_lock(self) -> int | None:
        """A duplicate of the lock for one launch to hand its stages, or None once retired."""
        return os.dup(self.lock_fd) if self._close.alive else None

    def retire(self, *, clean: bool) -> None:
        """Release the lock; first remove the subfolder when nothing of it can still be in use."""
        try:
            if clean and self._close.alive:
                _empty_owned(self.staging_fd, self.name, self.sub_fd, self.sub_info)
        except OSError as exc:
            _log.warning("SSTV could not remove its staging subfolder %s: %s", self.name, exc)
        finally:
            self._close()


@dataclass(frozen=True)
class SstvImage:
    """One decoded picture: where it lives, when it arrived, and what the tool made of it."""

    path: str                 # relative to the data dir, e.g. "sstv/20261030-120000.png"
    received_at: float        # the file's modification time, epoch seconds
    mode: str | None          # the SSTV mode sstv reported (Martin 1, Scottie 1, ...), when it did
    meta: dict                # the rest of the decode's sidecar (frequency, decode time), or empty

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "received_at": self.received_at,
            "mode": self.mode,
            "meta": self.meta,
        }


@dataclass
class _Capture:
    staged_path: Path
    published_path: Path
    freq_hz: int
    mode: str | None = None
    decoded_at: float | None = None


def parse_mode(line: str) -> str | None:
    """Pull the SSTV mode out of one sstv stdout line, or None if it is not the mode line.

    sstv prints "Detected SSTV mode <name>" once it locks onto a transmission; every other line it
    prints (header search, progress) is not a mode announcement.
    """
    text = (line or "").strip()
    if not text.startswith(_MODE_PREFIX):
        return None
    return text[len(_MODE_PREFIX):].strip() or None


def pipeline_commands(freq_hz: int, device: int, output_path,
                      capture_s: int = DEFAULT_CAPTURE_S) -> list:
    """The rtl_fm | sox | sstv argv list, in order. Pure — it builds the command, runs nothing.

    rtl_fm demodulates narrowband FM to raw PCM on stdout; sox wraps that in a WAV and trims it to
    `capture_s` so the stream ends and sstv can decode a finite recording; sstv writes the PNG.
    """
    rtl_fm = [
        RTL_FM, "-M", "fm", "-f", str(int(freq_hz)), "-s", str(SSTV_SAMPLE_RATE),
        "-g", "49.6", "-l", "0", "-d", str(device),
    ]
    sox = [
        SOX, "-t", "raw", "-r", str(SSTV_SAMPLE_RATE), "-e", "signed", "-b", "16", "-c", "1",
        "-", "-t", "wav", "-", "trim", "0", str(capture_s),
    ]
    sstv = [SSTV, "-d", "-", "-o", str(output_path)]
    return [rtl_fm, sox, sstv]


def _popen(freq_hz: int, device: int, output_path, *, pass_fds=()):
    """Launch rtl_fm|sox|sstv so sstv decodes a picture into output_path, printing its mode.

    Every stage inherits `pass_fds`: the decoder's staging lock, so a stage that outlives the
    console keeps the capture's staging subfolder from being swept.
    """
    rtl_cmd, sox_cmd, sstv_cmd = pipeline_commands(freq_hz, device, output_path)
    rtl_fm = spawn(rtl_cmd, stdout=subprocess.PIPE, pass_fds=pass_fds)
    sox = None
    try:
        sox = spawn(sox_cmd, stdin=rtl_fm.stdout, stdout=subprocess.PIPE, pass_fds=pass_fds)
        # The detected mode comes on stdout. Drop the progress bar on stderr.
        sstv = spawn(sstv_cmd, stdin=sox.stdout, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, pass_fds=pass_fds)
    except (OSError, ValueError) as exc:
        started = rtl_fm if sox is None else Pipeline(rtl_fm, sox, finite=True)
        raise PartialLaunch(started, exc) from exc
    finally:
        # Let each upstream stage see SIGPIPE if the next stage dies.
        for upstream in (rtl_fm, sox):
            if upstream is not None and upstream.stdout is not None:
                upstream.stdout.close()
    # One capture ends by design when sstv has decoded it, so it runs until that last stage ends.
    return Pipeline(rtl_fm, sox, sstv, finite=True)


class SstvDecoder(SupervisedReceiver):
    """Owns one rtl_fm|sox|sstv pipeline and the gallery of pictures it decodes to disk.

    A sibling of the ACARS and APRS decoders: an SDR-exclusive tool with a reader thread off the
    request path and release/resume so it takes turns with the waterfall on the one dongle. Unlike
    those, its product is image files, not a log — so the gallery is read from disk each call and
    release() leaves the already-received pictures in place.
    """

    EOF_WAIT_S = DEFAULT_CAPTURE_S

    def __init__(self, *, freq_hz=DEFAULT_SSTV_HZ, device=0, spawn=None,
                 output_dir=DEFAULT_OUTPUT_DIR, data_dir=None,
                 radio_present=None, which=shutil.which, serial=None, list_devices=None,
                 clock=time.time):
        self.freq_hz = int(freq_hz)
        self.device = device
        self.serial = serial or None
        self.output_dir = Path(output_dir)
        self._staging_dir = self.output_dir.parent / f".{self.output_dir.name}.staging"
        self._discard_dir = self._staging_dir / ".discard"
        self._staged_cleanup: set[Path] = set()
        self._staging_lock = threading.Lock()
        # This decoder's own staging subfolder, made at its first launch; retired at shutdown.
        self._owner: _StagingOwner | None = None
        self._owner_retired = False
        self.data_dir = Path(data_dir) if data_dir is not None else self.output_dir.parent
        self._list_devices = list_devices
        self._spawn = spawn if spawn is not None else self._spawn_pipeline
        self._which = which
        self._clock = clock
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._runs = RunGuard()
        self._init_lifecycle()
        self.error = None
        # Retirement calls the console's hook with the published picture, its capture's command
        # and frequency. The hook logs it as a reception and never raises. A picture already on
        # disk before the capture is never reported.
        self.on_reception = None
        # Who ended the child, and when the console may launch it again after it died on its own.
        # A capture ends by design after DEFAULT_CAPTURE_S, so a clean finish is not a death.
        self.watch = ChildWatch(SSTV, finite=True)

    _unavailable = SstvUnavailable
    _released_reason = "the radio was released to another program; resume SSTV to take it back"

    @property
    def _capture(self):
        return self._run.args[0] if self._run else None

    def _prepare_launch(self, freq_hz, number):
        for tool in (SSTV, RTL_FM, SOX):
            if not self._which(tool):
                raise SstvUnavailable(f"{tool} is not installed, so SIGINT 101 cannot decode SSTV")
        if not self._radio_present():
            raise SstvUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake a picture")
        try:
            radio = rtlsdr_radio_for_run(self.device, self.serial, self._list_devices)
        except DeviceSelectionError as exc:
            raise SstvUnavailable(str(exc)) from exc
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            owned = self._staging_dir / self._own_staging().name
            with self._staging_lock:
                cleanup = self._staged_cleanup.copy()
            # Entries only get here by moving the exact owned file after a confirmed reap.
            cleanup.update(self._discard_dir.glob("*.png"))
            for path in cleanup:
                self._remove_staged(path)
            capture = self._allocate_capture(self._clock(), number, freq_hz, owned)
        except (OSError, ValueError) as exc:
            raise SstvUnavailable(f"cannot stage the SSTV capture: {exc}") from exc

        def launch():
            try:
                proc = self._spawn(freq_hz, radio["index"], capture.staged_path)
            except (OSError, ValueError) as exc:
                raise SstvUnavailable(f"cannot run the SSTV pipeline: {exc}") from exc
            self.device = radio["index"]
            return proc

        return launch, (capture, radio)

    def _own_staging(self) -> _StagingOwner:
        """This decoder's staging subfolder. The first call makes it, then sweeps retired ones."""
        with self._staging_lock:
            if self._owner_retired:
                raise SstvUnavailable("SSTV has shut down")
            if self._owner is None:
                try:
                    owner = _StagingOwner.create(self._staging_dir.parent,
                                                 self._staging_dir.name, self.output_dir.name)
                except OSError as exc:
                    # Never fall back to paths: no subfolder, no sweep, and no capture.
                    _log.warning("SSTV staging cleanup aborted: %s", exc)
                    raise
                # Under this decoder's own lock, so its subfolder is never a candidate.
                owner.sweep()
                self._owner = owner
            return self._owner

    def _spawn_pipeline(self, freq_hz, device, output_path):
        """Launch the real pipeline. Each stage inherits this decoder's staging lock."""
        with self._staging_lock:
            lock = self._owner.inherited_lock() if self._owner is not None else None
        try:
            return _popen(freq_hz, device, output_path, pass_fds=() if lock is None else (lock,))
        finally:
            if lock is not None:
                os.close(lock)

    def shutdown(self):
        """Stop as every receiver does, then retire this decoder's staging subfolder.

        Only a shutdown that reaped and reported every run removes the subfolder. Otherwise its
        files stay and the lock is released: a later console sweeps them once no stage holds it.
        """
        clean = False
        try:
            super().shutdown()
            clean = True
        finally:
            with self._staging_lock:
                owner, self._owner = self._owner, None
                self._owner_retired = True
                cleanup = self._staged_cleanup.copy()
            if clean:
                for path in cleanup:
                    self._remove_staged(path)
            if owner is not None:
                owner.retire(clean=clean)

    def _allocate_capture(self, received_at: float, run: int, freq_hz: int,
                          owned: Path) -> _Capture:
        """Give this writer a sortable stem distinct from earlier runs and saved files."""
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(received_at))
        millis = int((received_at % 1) * 1000)
        base = f"{stamp}-{millis:03d}-r{run}"
        suffix = 0
        while True:
            stem = base if suffix == 0 else f"{base}-{suffix}"
            published = self.output_dir / f"{stem}.png"
            staged = owned / published.name
            paths = (staged, staged.with_suffix(".json"), published, published.with_suffix(".json"),
                     self._discard_dir / staged.name)
            if not any(os.path.lexists(path) for path in paths):
                return _Capture(staged, published, freq_hz)
            suffix += 1

    def _read(self, proc, run: int, capture: _Capture, _radio: dict) -> None:
        """Keep the mode sstv announced in memory while this capture is still current."""
        try:
            for raw in proc.stdout:
                found = parse_mode(raw)
                if found is not None:
                    decoded_at = round(self._clock(), 3)
                    with self._runs:
                        if self._runs.is_current(run):
                            capture.mode = found
                            capture.decoded_at = decoded_at
        except (OSError, ValueError) as exc:  # loud, never silent
            with self._runs:
                if self._runs.is_current(run):
                    self.error = str(exc)

    def _remove_staged(self, path: Path) -> None:
        """Retry cleanup of this retired path at the next launch if unlink fails."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            with self._staging_lock:
                self._staged_cleanup.add(path)
        else:
            with self._staging_lock:
                self._staged_cleanup.discard(path)

    def _discard_aborted(self, path: Path) -> None:
        """Move an owned, reaped capture to the discard queue before unlinking it."""
        discarded = self._discard_dir / path.name
        try:
            self._discard_dir.mkdir(exist_ok=True)
            path.replace(discarded)
        except FileNotFoundError:
            return
        except OSError:
            self._remove_staged(path)
        else:
            self._remove_staged(discarded)

    def _needs_diagnostics(self, run):
        return run.exit_code == 0 or super()._needs_diagnostics(run)

    def _describe_diagnostics(self, run, ended):
        read_tail = childproc.read_stderr_tail
        if run.exit_code == 0:
            # Successful captures check every stream after the writers are gone. Reuse those
            # reads for the exit reason if an upstream stage also reported a failure.
            run.physical.wait()
            if not run.reaped:
                return None
            tails = {id(stage): read_tail(stage) for stage in childproc._stages(run.proc)}
            return childproc.describe_exits(ended, finite=True, tool=run.watch.tool,
                                            read_tail=lambda stage: tails[id(stage)])
        return childproc.describe_exits(ended, finite=True, tool=run.watch.tool,
                                        read_tail=read_tail)

    def _report(self, run):
        """Use the retired capture's status and files once the physical outcome is known."""
        capture, radio = run.args
        if run.reap_failed or run.exit_code is None:
            self._discard_aborted(capture.staged_path)
            return
        if run.exit_code != 0:
            # The reap is confirmed, so this exact file is retired: discard it now.
            self._discard_aborted(capture.staged_path)
            return
        if not run.diagnosed.wait(receiver_lifecycle.DIAGNOSTIC_WAIT_S):
            # The reap is confirmed and clean, so the picture may still publish with the fault.
            if run.diagnostic_fault is None:
                run.diagnostic_fault = "exit diagnostic did not finish"
        try:
            info = capture.staged_path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            if self.error is None:
                self.error = str(exc)
            return
        if not stat.S_ISREG(info.st_mode):
            return
        if not info.st_size:
            self._remove_staged(capture.staged_path)
            return
        hook = self.on_reception
        sink = getattr(self, "reception_sink", None)
        publication = sink.publication() if sink is not None else nullcontext(True)
        with publication as admitted:
            if not admitted:
                self._discard_aborted(capture.staged_path)
                return
            if not self._publish_capture(capture):
                return
            item = self._describe(capture.published_path).as_dict()
            if run.diagnostic_fault is not None:
                item["meta"]["exit_diagnostic"] = {"status": run.exit_code,
                                                   "reason": run.diagnostic_fault}
            run.ready.set()
            if hook is not None:
                childproc.emit_reception(hook, item, command=command_line(run.proc),
                                         freq_hz=capture.freq_hz, radio=radio)

    def _publish_capture(self, capture: _Capture) -> bool:
        """The exclusive PNG link commits publication. Install its metadata afterwards."""
        png = capture.published_path
        sidecar = png.with_suffix(".json")
        staged_sidecar = None
        committed = False
        try:
            if os.path.lexists(png) or os.path.lexists(sidecar):
                raise FileExistsError(f"the SSTV picture stem is already occupied: {png.name}")
            if capture.mode is not None:
                candidate = capture.staged_path.with_suffix(".json")
                with candidate.open("x", encoding="utf-8") as handle:
                    staged_sidecar = candidate
                    json.dump({"mode": capture.mode, "freq_hz": capture.freq_hz,
                               "decoded_at": capture.decoded_at}, handle, sort_keys=True)
            os.link(capture.staged_path, png)
            committed = True
            if staged_sidecar is not None:
                os.link(staged_sidecar, sidecar)
        except OSError as exc:
            if self.error is None:
                self.error = str(exc)
        finally:
            if staged_sidecar is not None:
                self._remove_staged(staged_sidecar)
            self._remove_staged(capture.staged_path)
        return committed

    def images(self, limit: int | None = None) -> list:
        """The decoded pictures as plain dicts for the API, newest first, read fresh from disk."""
        try:
            pngs = [p for p in self.output_dir.iterdir()
                    if p.is_file() and p.suffix.lower() == ".png"]
        except (FileNotFoundError, NotADirectoryError):
            return []
        pngs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if limit:
            pngs = pngs[:limit]
        return [self._describe(p).as_dict() for p in pngs]

    def _describe(self, png: Path) -> SstvImage:
        mode, meta = self._sidecar_for(png)
        try:
            rel = png.relative_to(self.data_dir).as_posix()
        except ValueError:
            rel = png.name
        return SstvImage(
            path=rel,
            received_at=round(png.stat().st_mtime, 3),
            mode=mode,
            meta=meta,
        )

    @staticmethod
    def _sidecar_for(png: Path):
        """The (mode, meta) a picture's sidecar carries, or (None, {}) when there is none."""
        sidecar = png.with_suffix(".json")
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, {}
        if not isinstance(data, dict):
            return None, {}
        mode = data.get("mode")
        meta = {k: v for k, v in data.items() if k != "mode"}
        return mode, meta
