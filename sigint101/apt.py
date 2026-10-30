# ABOUTME: Decodes NOAA APT at ~137 MHz via satdump for M24 — a weather image from a live pass.
# ABOUTME: No radio, no satdump, no pass decoded — an empty image list, never a fabricated picture.

from __future__ import annotations

import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .hardware import (
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# satdump's binary. It opens the RTL-SDR directly, runs the NOAA APT pipeline on a live pass, and
# writes the decoded image products as files — so this decoder orchestrates a process and then reads
# the output directory, rather than parsing a line stream. The name lives here, not in hardware.py,
# so parallel decoder builds do not collide editing a shared module.
SATDUMP = "satdump"

# satdump's pipeline id for the NOAA APT downlink. `satdump live <pipeline> <output_dir> ...`.
APT_PIPELINE = "noaa_apt"

# Where the APT lesson listens by default. NOAA-19 transmits APT on 137.100 MHz; the pass is a
# narrowband FM signal that lasts roughly 10-15 minutes as the satellite crosses the sky.
DEFAULT_APT_HZ = 137_100_000

# Baseband sample rate for the pass. APT is only ~34 kHz wide, but satdump wants enough bandwidth to
# find and track the carrier; ~1 MS/s is comfortable for an RTL-SDR and is what APT captures use.
APT_SAMPLE_RATE_HZ = 1_024_000

# RTL-SDR tuner gain handed to satdump (its --general_gain). Near the top of the RTL range, matching
# the other high-band lessons — a weak 137 MHz pass needs the front end wide open.
APT_GAIN = 49

# How long a single capture runs before satdump stops and finalises the image. One pass is ~15 min;
# this bounds the run so the decoder is finite and the dongle comes back for the next lesson.
APT_PASS_TIMEOUT_S = 900

# The APT birds and their downlink frequencies, so an image can name the satellite it came from.
# We tune one frequency at a time, so the pass's satellite follows from the frequency we tuned.
NOAA_APT_FREQUENCIES = {
    137_620_000: "NOAA-15",
    137_912_500: "NOAA-18",
    137_100_000: "NOAA-19",
}


class AptUnavailable(RuntimeError):
    """The APT pass cannot be decoded. Never a reason to invent an image — the list stays empty."""


def satellite_for(freq_hz: int) -> str:
    """Name the NOAA bird for an APT downlink frequency, or a plain 'NOAA' when unknown."""
    return NOAA_APT_FREQUENCIES.get(int(freq_hz), "NOAA")


def _popen(freq_hz: int, device: int, output_dir: Path):
    """Launch satdump on the NOAA APT pipeline, writing decoded image products into output_dir."""
    return subprocess.Popen(
        [SATDUMP, "live", APT_PIPELINE, str(output_dir),
         "--source", "rtlsdr", "--source_id", str(device),
         "--samplerate", str(APT_SAMPLE_RATE_HZ), "--frequency", str(freq_hz),
         "--general_gain", str(APT_GAIN), "--timeout", str(APT_PASS_TIMEOUT_S)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class AptDecoder:
    """Owns one satdump process decoding a NOAA APT pass, and reads the images it writes to disk.

    A sibling of the ACARS and ADS-B decoders: an SDR-exclusive tool with release/resume so it takes
    turns with the waterfall on the one dongle. Unlike them it produces an image, not a record
    stream — start() spawns satdump to write PNGs into output_dir; images() scans that directory.
    A produced PNG is a real reception; an empty directory is honest "no pass yet", never a fake.
    """

    def __init__(self, *, output_dir, data_dir=None, freq_hz=DEFAULT_APT_HZ, device=0, spawn=_popen,
                 radio_present=None, which=shutil.which, serial=None, list_devices=None):
        self.output_dir = Path(output_dir)
        # Returned paths are relative to the data dir the console serves from; output_dir sits under
        # it (data/apt under data/), so its parent is the right default.
        self.data_dir = Path(data_dir) if data_dir is not None else self.output_dir.parent
        self.freq_hz = int(freq_hz)
        self.device = device
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._life = threading.RLock()
        self._proc = None
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
            raise AptUnavailable(str(exc)) from exc

    def release(self) -> None:
        """Hand the dongle to another program (the sweeper) and stay off it until resumed.

        The decoded images are real receptions on disk, so releasing stops the capture but keeps
        them — unlike the in-memory message logs, there is nothing here to throw away.
        """
        with self._life:
            self._released = True
            self.stop()

    def resume(self) -> None:
        with self._life:
            self._released = False

    def start(self) -> None:
        """Begin a capture, unless the radio is absent, satdump is missing, or released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        if self._released:
            raise AptUnavailable(
                "the radio was released to another program; resume APT to take it back")
        if not self._which(SATDUMP):
            raise AptUnavailable(
                f"{SATDUMP} is not installed, so SIGINT 101 cannot decode an APT pass")
        if not self._radio_present():
            raise AptUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake an image")
        if self.error:
            reason, self.error = self.error, None
            raise AptUnavailable(reason)
        self.device = self._choose_device()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._proc = self._spawn(self.freq_hz, self.device, self.output_dir)
        except (OSError, ValueError) as exc:
            self._proc = None
            raise AptUnavailable(f"cannot run satdump: {exc}") from exc
        self.error = None

    def stop(self) -> None:
        """End the capture and let go of the dongle."""
        with self._life:
            self._stop_locked()

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.error = "satdump did not stop; the dongle may still be held"
            except (OSError, subprocess.SubprocessError):
                pass

    def drain(self, timeout: float = APT_PASS_TIMEOUT_S) -> None:
        """Wait for the current pass to finish. Unlike a live feed, a satdump capture is finite."""
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass

    def images(self, limit: int | None = None) -> list[dict]:
        """The decoded PNGs as plain dicts for the API, newest first; empty until a pass decodes."""
        if not self.output_dir.exists():
            return []
        pngs = [p for p in self.output_dir.rglob("*.png") if p.is_file()]
        pngs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if limit:
            pngs = pngs[:limit]
        rows = []
        for png in pngs:
            stat = png.stat()
            received_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            rows.append({
                "path": png.relative_to(self.data_dir).as_posix(),
                "received_at": received_at,
                "satellite": satellite_for(self.freq_hz),
                "meta": {"filename": png.name, "size_bytes": stat.st_size},
            })
        return rows
