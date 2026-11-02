# ABOUTME: Decodes RDS off an FM station via rtl_fm|redsea for M12 — the data hidden in the signal.
# ABOUTME: No radio, no redsea, no groups — this reports absence, it never invents a station.

from __future__ import annotations

import json
import shutil
import subprocess
import threading

from .childproc import ChildWatch, running_command, spawn, stop_process
from .childproc import Pipeline as _Pipeline
from .hardware import (
    REDSEA,
    RTL_FM,
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# Where the RDS lesson listens. RDS rides a 57 kHz subcarrier on an ordinary FM broadcast station,
# so this is an FM-band frequency; a pack points it at the strongest local station via tune.
DEFAULT_RDS_HZ = 96_000_000

# rtl_fm's FM-multiplex rate. 171 kHz = 9 x 19 kHz keeps the whole baseband through the 57 kHz
# RDS subcarrier, and is the rate redsea is told to expect.
REDSEA_SAMPLE_RATE = 171_000


class RDSUnavailable(RuntimeError):
    """The RDS stream cannot be read. Never a reason to synthesise a station — the panel says so."""


def parse_rds_group(line: str) -> dict | None:
    """Turn one redsea JSON line into a group dict, or None if it is not an RDS group.

    redsea prints one JSON object per decoded group; each carries a `group` type (0A, 2A, ...).
    A line that is not JSON, or JSON without a `group`, is not a group to absorb.
    """
    text = (line or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "group" not in obj:
        return None
    return obj


class RDSStation:
    """The station identity assembled from a stream of RDS groups.

    RDS sends its data a few bits at a time: the programme-service name (`ps`, the 8-char call sign)
    and the RadioText build up over many groups, and redsea reports the assembled value when it
    changes. This keeps the latest of each — the hidden data the waterfall never showed — plus a
    running count so a learner can watch it fill in.
    """

    def __init__(self):
        self.pi = None
        self.ps = None
        self.radiotext = None
        self.prog_type = None
        self.group_count = 0

    def absorb(self, group: dict | None) -> None:
        if not group:
            return
        self.group_count += 1
        if group.get("pi"):
            self.pi = group["pi"]
        if group.get("ps"):
            self.ps = group["ps"].strip()
        if group.get("radiotext"):
            self.radiotext = group["radiotext"].strip()
        if group.get("prog_type"):
            self.prog_type = group["prog_type"]

    def as_dict(self) -> dict:
        return {
            "pi": self.pi,
            "ps": self.ps,
            "radiotext": self.radiotext,
            "prog_type": self.prog_type,
            "group_count": self.group_count,
        }


def _popen(freq_hz: int, device: int):
    """Launch rtl_fm demodulating the FM multiplex into redsea, which prints RDS groups as JSON."""
    rtl_fm = spawn(
        [RTL_FM, "-M", "fm", "-l", "0", "-A", "std", "-p", "0", "-s", "171k",
         "-g", "49", "-F", "9", "-d", str(device), "-f", str(freq_hz)],
        stdout=subprocess.PIPE)
    redsea = spawn(
        [REDSEA, "-r", str(REDSEA_SAMPLE_RATE)],
        stdin=rtl_fm.stdout, stdout=subprocess.PIPE, text=True)
    # Let rtl_fm see SIGPIPE if redsea dies, rather than fill a pipe nobody drains.
    if rtl_fm.stdout is not None:
        rtl_fm.stdout.close()
    return _Pipeline(rtl_fm, redsea)


class RDSDecoder:
    """Owns one rtl_fm|redsea pipeline and the station it assembles.

    A sibling of the rtl_433 Decoder: an SDR-exclusive tool with a reader thread off the request
    path and release/resume so it takes turns with the waterfall on the one dongle.
    """

    def __init__(self, *, freq_hz=DEFAULT_RDS_HZ, device=0, spawn=_popen,
                 radio_present=None, which=shutil.which, serial=None, list_devices=None):
        self.freq_hz = int(freq_hz)
        self.device = device
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._station = RDSStation()
        self._lock = threading.Lock()
        self._life = threading.RLock()
        self._proc = None
        self._thread = None
        self._released = False
        self.error = None
        # Who ended the child, and when the console may launch it again after it died on its own.
        self.watch = ChildWatch(RTL_FM)

    @property
    def running(self) -> bool:
        """True while the pipeline is alive and holding the dongle."""
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
            raise RDSUnavailable(str(exc)) from exc

    def release(self) -> None:
        """Hand the dongle to another program (the sweeper) and stay off it until resumed."""
        with self._life:
            self._released = True
            self.stop()
            with self._lock:
                self._station = RDSStation()

    def resume(self) -> None:
        with self._life:
            self._released = False

    def start(self) -> None:
        """Begin decoding RDS, unless the radio is absent, a tool is missing, or released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.running:
            return
        self._settle()
        if self._released:
            raise RDSUnavailable(
                "the radio was released to another program; resume RDS to take it back")
        for tool in (RTL_FM, REDSEA):
            if not self._which(tool):
                raise RDSUnavailable(f"{tool} is not installed, so SIGINT 101 cannot decode RDS")
        if not self._radio_present():
            raise RDSUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake a station")
        if self.error:
            reason, self.error = self.error, None
            raise RDSUnavailable(reason)
        # A child that died on its own is not relaunched on every poll: the reason stands, and the
        # console waits out a back-off that grows while the deaths keep coming.
        blocked = self.watch.blocked()
        if blocked:
            raise RDSUnavailable(blocked)
        self.device = self._choose_device()
        try:
            self._proc = self._spawn(self.freq_hz, self.device)
        except (OSError, ValueError) as exc:
            self._proc = None
            raise RDSUnavailable(f"cannot run the RDS pipeline: {exc}") from exc
        self.watch.spawned(self._proc)
        self.error = None
        self._thread = threading.Thread(target=self._read, args=(self._proc,),
                                        name="sigint101-rds", daemon=True)
        self._thread.start()

    def _read(self, proc) -> None:
        """Consume redsea's JSON on a background thread, assembling the station until it ends."""
        try:
            for raw in proc.stdout:
                group = parse_rds_group(raw)
                if group is not None:
                    with self._lock:
                        self._station.absorb(group)
        except (OSError, ValueError) as exc:  # loud, never silent
            self.error = str(exc)
        finally:
            self._note_end(proc)

    def _note_end(self, proc) -> None:
        """Record why the child ended, if it was not us, so the next poll reports it as stopped."""
        reason = self.watch.ended(proc)
        if reason and self.error is None:
            self.error = reason

    def _settle(self) -> None:
        """Account for a child that ended since the last poll, then clear away what it left."""
        if self._proc is not None:
            self._note_end(self._proc)
            self._stop_locked()

    def stop(self) -> None:
        """End decoding and let go of the dongle."""
        with self._life:
            self._stop_locked()

    def _stop_locked(self) -> None:
        # Marked first, so the reader seeing the output end never reports our stop as a death.
        self.watch.stopping()
        proc, self._proc = self._proc, None
        if proc is not None and not stop_process(proc):
            self.error = "the RDS pipeline survived kill; the dongle may still be held"
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for a finite source to be fully consumed. A real RDS stream never ends."""
        if self._thread is not None:
            self._thread.join(timeout)

    def station(self) -> RDSStation:
        """The station assembled so far."""
        with self._lock:
            return self._station
