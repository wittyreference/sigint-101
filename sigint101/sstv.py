# ABOUTME: Decodes SSTV on 2m (145.8 MHz) via rtl_fm|sox|sstv for M25 — a picture in the air.
# ABOUTME: No radio, no tools, no signal — reports an empty gallery, never a placeholder image.

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

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

# Where the SSTV lesson listens. 145.800 MHz is the 2m SSTV calling frequency; a pack can retune it
# via the frequency seam, but this is where a picture is most likely to appear.
DEFAULT_SSTV_HZ = 145_800_000

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


class _Pipeline:
    """The rtl_fm|sox|sstv trio as one object: sstv's stdout to read, all three to reap."""

    def __init__(self, rtl_fm, sox, sstv):
        self._procs = (rtl_fm, sox, sstv)
        self._sstv = sstv
        self.stdout = sstv.stdout

    def poll(self):
        return self._sstv.poll()

    def terminate(self):
        for proc in reversed(self._procs):
            try:
                proc.terminate()
            except (OSError, subprocess.SubprocessError):
                pass

    def wait(self, timeout=None):
        for proc in self._procs:
            try:
                proc.wait(timeout=timeout)
            except (OSError, subprocess.SubprocessError):
                pass
        return 0


def _popen(freq_hz: int, device: int, output_path):
    """Launch rtl_fm|sox|sstv so sstv decodes a picture into output_path, printing its mode."""
    rtl_cmd, sox_cmd, sstv_cmd = pipeline_commands(freq_hz, device, output_path)
    rtl_fm = subprocess.Popen(
        rtl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sox = subprocess.Popen(
        sox_cmd, stdin=rtl_fm.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    # sstv narrates the detected mode to stdout; stderr carries its progress bar, which we drop.
    sstv = subprocess.Popen(
        sstv_cmd, stdin=sox.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    # Let each upstream stage see SIGPIPE if the next dies, rather than fill a pipe nobody reads.
    for upstream in (rtl_fm.stdout, sox.stdout):
        if upstream is not None:
            upstream.close()
    return _Pipeline(rtl_fm, sox, sstv)


class SstvDecoder:
    """Owns one rtl_fm|sox|sstv pipeline and the gallery of pictures it decodes to disk.

    A sibling of the ACARS and APRS decoders: an SDR-exclusive tool with a reader thread off the
    request path and release/resume so it takes turns with the waterfall on the one dongle. Unlike
    those, its product is image files, not a log — so the gallery is read from disk each call and
    release() leaves the already-received pictures in place.
    """

    def __init__(self, *, freq_hz=DEFAULT_SSTV_HZ, device=0, spawn=_popen,
                 output_dir=DEFAULT_OUTPUT_DIR, data_dir=None,
                 radio_present=None, which=shutil.which, serial=None, list_devices=None,
                 clock=time.time):
        self.freq_hz = int(freq_hz)
        self.device = device
        self.serial = serial or None
        self.output_dir = Path(output_dir)
        self.data_dir = Path(data_dir) if data_dir is not None else self.output_dir.parent
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._clock = clock
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._life = threading.RLock()
        self._proc = None
        self._thread = None
        self._pending = None
        self._released = False
        self.error = None

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

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
        self.device = self._choose_device()
        received_at = self._clock()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.output_dir / self._name_for(received_at)
        try:
            self._proc = self._spawn(self.freq_hz, self.device, output_path)
        except (OSError, ValueError) as exc:
            self._proc = None
            raise SstvUnavailable(f"cannot run the SSTV pipeline: {exc}") from exc
        self.error = None
        self._pending = (output_path, received_at)
        self._thread = threading.Thread(target=self._read, name="sigint101-sstv", daemon=True)
        self._thread.start()

    @staticmethod
    def _name_for(received_at: float) -> str:
        """A sortable, collision-resistant file name for one capture's picture."""
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(received_at))
        millis = int((received_at % 1) * 1000)
        return f"{stamp}-{millis:03d}.png"

    def _read(self) -> None:
        """Watch sstv's output for the mode it detects, then record a sidecar for the picture.

        The PNG is sstv's to write; the mode it prints is ours to keep, so when the decode ends we
        pair the two in a sidecar next to the image. A decode that announced no mode writes none —
        a picture with an unknown mode is honest, an invented mode is not.
        """
        output_path, received_at = self._pending or (None, None)
        mode = None
        try:
            for raw in self._proc.stdout:
                found = parse_mode(raw)
                if found is not None:
                    mode = found
        except (OSError, ValueError) as exc:  # loud, never silent
            self.error = str(exc)
            return
        if mode is not None and output_path is not None:
            self._write_sidecar(output_path, mode, received_at)

    def _write_sidecar(self, output_path: Path, mode: str, received_at: float) -> None:
        sidecar = output_path.with_suffix(".json")
        payload = {
            "mode": mode,
            "freq_hz": self.freq_hz,
            "decoded_at": round(self._clock(), 3),
        }
        try:
            sidecar.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        except OSError as exc:  # loud, never silent
            self.error = str(exc)

    def stop(self) -> None:
        """End decoding and let go of the dongle."""
        with self._life:
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.error = "the SSTV pipeline did not stop; the dongle may still be held"
            except (OSError, subprocess.SubprocessError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for one capture to finish decoding. A fixed-length recording does end."""
        if self._thread is not None:
            self._thread.join(timeout)

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
