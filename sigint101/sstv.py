# ABOUTME: Decodes SSTV (the ISS downlink, 437.550 MHz) via rtl_fm|sox|sstv for M25 — a picture.
# ABOUTME: No radio, no tools, no signal — reports an empty gallery, never a placeholder image.

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
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

# The only names a capture stages: _allocate_capture's stem, then the picture or its sidecar.
_STAGED_NAME = re.compile(r"[0-9]{8}T[0-9]{6}-[0-9]{3}-r[0-9]+(?:-[0-9]+)?\.(?:png|json)")


class SstvUnavailable(RuntimeError):
    """The SSTV stream cannot be read. Never a reason to emit a placeholder — gallery stays bare."""


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


def _popen(freq_hz: int, device: int, output_path):
    """Launch rtl_fm|sox|sstv so sstv decodes a picture into output_path, printing its mode."""
    rtl_cmd, sox_cmd, sstv_cmd = pipeline_commands(freq_hz, device, output_path)
    rtl_fm = spawn(rtl_cmd, stdout=subprocess.PIPE)
    sox = None
    try:
        sox = spawn(sox_cmd, stdin=rtl_fm.stdout, stdout=subprocess.PIPE)
        # The detected mode comes on stdout. Drop the progress bar on stderr.
        sstv = spawn(sstv_cmd, stdin=sox.stdout, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True)
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

    def __init__(self, *, freq_hz=DEFAULT_SSTV_HZ, device=0, spawn=_popen,
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
        self._staging_swept = False
        self.data_dir = Path(data_dir) if data_dir is not None else self.output_dir.parent
        self._list_devices = list_devices
        self._spawn = spawn
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
            self._staging_dir.mkdir(parents=True, exist_ok=True)
            with self._life:
                # Held across the sweep, so no launch of this console can start under it.
                if not self._staging_swept and self._run is None:
                    self._staging_swept = True
                    self._sweep_previous_staging()
            with self._staging_lock:
                cleanup = self._staged_cleanup.copy()
            # Entries only get here by moving the exact owned file after a confirmed reap.
            cleanup.update(self._discard_dir.glob("*.png"))
            for path in cleanup:
                self._remove_staged(path)
            capture = self._allocate_capture(self._clock(), number, freq_hz)
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

    def _allocate_capture(self, received_at: float, run: int, freq_hz: int) -> _Capture:
        """Give this writer a sortable stem distinct from earlier runs and saved files."""
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(received_at))
        millis = int((received_at % 1) * 1000)
        base = f"{stamp}-{millis:03d}-r{run}"
        suffix = 0
        while True:
            stem = base if suffix == 0 else f"{base}-{suffix}"
            published = self.output_dir / f"{stem}.png"
            staged = self._staging_dir / published.name
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

    def _sweep_previous_staging(self) -> None:
        """Remove the staged captures an earlier console left behind, before this one stages any.

        Every stage is spawned with PR_SET_PDEATHSIG set to SIGKILL, so an earlier console's
        writers died with it. Only regular files directly in a real staging directory, named as
        _allocate_capture names them, are removed. Symlinks are never followed or removed.
        """
        try:
            fd = os.open(self._staging_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return
        try:
            try:
                if os.path.samestat(os.fstat(fd), os.stat(self.output_dir)):
                    return
            except OSError:
                return
            with os.scandir(fd) as entries:
                names = [entry.name for entry in entries
                         if _STAGED_NAME.fullmatch(entry.name)
                         and entry.is_file(follow_symlinks=False)]
            for name in names:
                try:
                    # Relative to the opened directory, and an unlink never follows a link.
                    os.unlink(name, dir_fd=fd)
                except OSError:
                    pass
        finally:
            os.close(fd)

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
