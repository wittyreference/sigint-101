# ABOUTME: Decodes SSTV (the ISS downlink, 437.550 MHz) via rtl_fm|sox|sstv for M25 — a picture.
# ABOUTME: No radio, no tools, no signal — reports an empty gallery, never a placeholder image.

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .childproc import (
    ChildWatch,
    Pipeline,
    RunGuard,
    command_line,
    emit_reception,
    running_command,
    settle_ended,
    spawn,
    stop_process,
)
from .hardware import (
    RTL_FM,
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

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


def _confirmed_reaped(proc) -> bool:
    """Check every stage after a settle that did not return its stop result."""
    try:
        stages = getattr(proc, "procs", (proc,))
        if any(stage.poll() is None for stage in stages):
            return False
        # Use the pipeline's wait contract as well, including process groups when supported.
        proc.wait(timeout=0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return True


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
    sox = spawn(sox_cmd, stdin=rtl_fm.stdout, stdout=subprocess.PIPE)
    # sstv narrates the detected mode to stdout; stderr carries its progress bar, which we drop.
    sstv = spawn(sstv_cmd, stdin=sox.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                 text=True)
    # Let each upstream stage see SIGPIPE if the next dies, rather than fill a pipe nobody reads.
    for upstream in (rtl_fm.stdout, sox.stdout):
        if upstream is not None:
            upstream.close()
    # One capture ends by design when sstv has decoded it, so it runs until that last stage ends.
    return Pipeline(rtl_fm, sox, sstv, finite=True)


class SstvDecoder:
    """Owns one rtl_fm|sox|sstv pipeline and the gallery of pictures it decodes to disk.

    A sibling of the ACARS and APRS decoders: an SDR-exclusive tool with a reader thread off the
    request path and release/resume so it takes turns with the waterfall on the one dongle. Unlike
    those, its product is image files, not a log — so the gallery is read from disk each call and
    release() leaves the already-received pictures in place.
    """

    STOP_WAIT_S = 3.0

    def __init__(self, *, freq_hz=DEFAULT_SSTV_HZ, device=0, spawn=_popen,
                 output_dir=DEFAULT_OUTPUT_DIR, data_dir=None,
                 radio_present=None, which=shutil.which, serial=None, list_devices=None,
                 clock=time.time):
        self.freq_hz = int(freq_hz)
        self.device = device
        self.serial = serial or None
        self.output_dir = Path(output_dir)
        self._staging_dir = self.output_dir.parent / f".{self.output_dir.name}.staging"
        self._staged_cleanup: set[Path] = set()
        self.data_dir = Path(data_dir) if data_dir is not None else self.output_dir.parent
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._clock = clock
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._life = threading.RLock()
        self._runs = RunGuard()
        self._proc = None
        self._thread = None
        self._capture: _Capture | None = None
        self._released = False
        self.error = None
        # Retirement calls the console's hook with the published picture, its capture's command
        # and frequency. The hook logs it as a reception and never raises. A picture already on
        # disk before the capture is never reported.
        self.on_reception = None
        # Who ended the child, and when the console may launch it again after it died on its own.
        # A capture ends by design after DEFAULT_CAPTURE_S, so a clean finish is not a death.
        self.watch = ChildWatch(SSTV, finite=True)

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def live_command(self) -> str | None:
        """The exact command running now, read off the live process; None when nothing runs."""
        return running_command(self._proc)

    @property
    def released(self) -> bool:
        return self._released

    def _choose_device(self) -> int:
        if self._list_devices is None:
            return self.device
        try:
            return select_rtlsdr_index(self.serial, devices=self._list_devices())
        except DeviceSelectionError as exc:
            raise SstvUnavailable(str(exc)) from exc

    def release(self) -> None:
        """Hand the dongle to another program (the sweeper) and stay off it until resumed.

        The pictures already decoded stay on disk — they are real receptions, not an in-memory
        roster to clear.
        """
        with self._life:
            self._released = True
            self.stop()

    def resume(self) -> None:
        with self._life:
            self._released = False

    def start(self) -> None:
        """Begin decoding SSTV, unless the radio is absent, a tool is missing, or released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        self._settle_locked()
        if self._released:
            raise SstvUnavailable(
                "the radio was released to another program; resume SSTV to take it back")
        for tool in (SSTV, RTL_FM, SOX):
            if not self._which(tool):
                raise SstvUnavailable(f"{tool} is not installed, so SIGINT 101 cannot decode SSTV")
        if not self._radio_present():
            raise SstvUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake a picture")
        if self.error:
            reason, self.error = self.error, None
            raise SstvUnavailable(reason)
        # A child that died on its own is not relaunched on every poll: the reason stands, and the
        # console waits out a back-off that grows while the deaths keep coming.
        blocked = self.watch.blocked()
        if blocked:
            raise SstvUnavailable(blocked)
        self.device = self._choose_device()
        run = self._runs.end(self.watch)
        freq_hz = self.freq_hz
        received_at = self._clock()
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._staging_dir.mkdir(parents=True, exist_ok=True)
            for path in tuple(self._staged_cleanup):
                self._remove_staged(path)
            capture = self._allocate_capture(received_at, run, freq_hz)
            self._proc = self._spawn(freq_hz, self.device, capture.staged_path)
        except (OSError, ValueError) as exc:
            self._proc = None
            raise SstvUnavailable(f"cannot run the SSTV pipeline: {exc}") from exc
        self._capture = capture
        self.watch.spawned(self._proc)
        self._thread = threading.Thread(target=self._read, args=(self._proc, run, capture),
                                        name="sigint101-sstv", daemon=True)
        self._thread.start()

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
            paths = (staged, staged.with_suffix(".json"), published, published.with_suffix(".json"))
            if not any(os.path.lexists(path) for path in paths):
                return _Capture(staged, published, freq_hz)
            suffix += 1

    def _read(self, proc, run: int, capture: _Capture) -> None:
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
        finally:
            self._note_end(proc, run)

    def _note_end(self, proc, run: int) -> None:
        """Record why the current child ended so the next poll can report it as stopped."""
        reason = self.watch.ended(proc, runs=self._runs, run=run)
        if reason:
            with self._runs:
                if self._runs.is_current(run) and self.error is None:
                    self.error = reason

    def _detach(self) -> tuple[_Capture, threading.Thread | None]:
        capture, self._capture = self._capture, None
        thread, self._thread = self._thread, None
        self._proc = None
        assert capture is not None
        return capture, thread

    def _settle_locked(self) -> None:
        """Reap the finished pipeline, then drain its remaining mode lines and retire it."""
        proc = self._proc
        if proc is None or (exit_code := proc.poll()) is None:
            return
        capture, thread = self._detach()
        try:
            reaped = settle_ended(proc, thread, self._runs, self.watch,
                                  note_end=self._note_end, wait_s=self.STOP_WAIT_S)
        except Exception:
            # The helper attempts a stop when diagnostics fail, but can only return its reap
            # result on a normal return. Keep any file whose writer might still be alive.
            if _confirmed_reaped(proc):
                self._staged_cleanup.add(capture.staged_path)
            raise
        self._finalize_capture(proc, capture, reaped=reaped, exit_code=exit_code)

    def _remove_staged(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self._staged_cleanup.add(path)
            if self.error is None:
                self.error = str(exc)
        else:
            self._staged_cleanup.discard(path)

    def _finalize_capture(self, proc, capture: _Capture, *, reaped: bool,
                          exit_code: int | None) -> None:
        """Publish a clean, reaped capture and report it with its own launch provenance."""
        if not reaped:
            self.error = "the SSTV pipeline survived kill; the dongle may still be held"
            return
        if exit_code != 0:
            self._remove_staged(capture.staged_path)
            return
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
        if not self._publish_capture(capture):
            return
        report = self.on_reception
        if report is not None:
            emit_reception(report, self._describe(capture.published_path).as_dict(),
                           command=command_line(proc), freq_hz=capture.freq_hz)

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
            if committed:
                self._remove_staged(capture.staged_path)
        return committed

    def stop(self) -> None:
        """End decoding and let go of the dongle."""
        with self._life:
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is not None:
            self._settle_locked()
            return
        # Invalidate before signaling so EOF from a deliberate stop cannot commit a death.
        self._runs.end(self.watch)
        capture, thread = self._detach()
        reaped = stop_process(proc)
        if thread is not None:
            thread.join(timeout=self.STOP_WAIT_S)
        self._finalize_capture(proc, capture, reaped=reaped, exit_code=None)

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for one capture's reader to finish. Retirement publishes the picture."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

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
