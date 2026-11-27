# ABOUTME: Decodes RDS off an FM station via rtl_fm|redsea for M12 — the data hidden in the signal.
# ABOUTME: No radio, no redsea, no groups — this reports absence, it never invents a station.

from __future__ import annotations

import json
import os
import shutil
import subprocess

from .childproc import (
    ChildWatch,
    RunGuard,
    command_line,
    spawn,
)
from .childproc import Pipeline as _Pipeline
from .hardware import (
    REDSEA,
    RTL_FM,
    DeviceSelectionError,
    probe_rtlsdr,
    rtlsdr_radio_for_run,
)
from .receiver_lifecycle import SupervisedReceiver, launch_consumer

# Where the RDS lesson listens. RDS rides a 57 kHz subcarrier on an ordinary FM broadcast station,
# so this must be a real FM channel, and the best one is the learner's strongest local station —
# which only the site knows. The provisioner sets it in this environment variable (Hz); without
# one, the console falls back to 106.5 MHz, the steady station at the box this course was built on.
RDS_HZ_ENV = "SIGINT101_RDS_HZ"
DEFAULT_RDS_HZ = 106_500_000

# 47 CFR 73.201: US FM broadcast channels are centred on the odd tenths from 88.1 to 107.9 MHz,
# 200 kHz apart. A frequency off that raster is not a station (96.0 MHz was a spur comb).
FM_FIRST_CHANNEL_HZ = 88_100_000
FM_LAST_CHANNEL_HZ = 107_900_000
FM_CHANNEL_SPACING_HZ = 200_000


def is_fm_channel(hz: int) -> bool:
    """Is this the centre of a US FM broadcast channel (88.1-107.9 MHz, odd tenths)?"""
    return (FM_FIRST_CHANNEL_HZ <= hz <= FM_LAST_CHANNEL_HZ
            and (hz - FM_FIRST_CHANNEL_HZ) % FM_CHANNEL_SPACING_HZ == 0)


def configured_fm_hz(name: str, default: int, environ=None) -> int:
    """An FM station set at provisioning in environment variable `name`, in Hz, or `default`.

    A value that is not an integer FM channel centre falls back to the default rather than taking
    the kiosk down at boot; the console reports the frequency it actually used, so a bad setting
    shows on the dial instead of hiding.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(name) or "").strip()
    try:
        hz = int(raw)
    except ValueError:
        return default
    return hz if is_fm_channel(hz) else default


def configured_rds_hz(environ=None) -> int:
    """The station RDS decodes: SIGINT101_RDS_HZ when it names an FM channel, else the default."""
    return configured_fm_hz(RDS_HZ_ENV, DEFAULT_RDS_HZ, environ)

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
    redsea = launch_consumer(rtl_fm, spawn, [REDSEA, "-r", str(REDSEA_SAMPLE_RATE)])
    return _Pipeline(rtl_fm, redsea)


class RDSDecoder(SupervisedReceiver):
    """Owns one rtl_fm|redsea pipeline and the station it assembles.

    A sibling of the rtl_433 Decoder: an SDR-exclusive tool with a reader thread off the request
    path and release/resume so it takes turns with the waterfall on the one dongle.
    """

    def __init__(self, *, freq_hz=None, device=0, spawn=_popen,
                 radio_present=None, which=shutil.which, serial=None, list_devices=None):
        # No frequency given means the site's provisioned station (SIGINT101_RDS_HZ).
        self.freq_hz = int(freq_hz) if freq_hz is not None else configured_rds_hz()
        # The frequency this decoder was built for; the console tunes a lesson back to it.
        self.home_hz = self.freq_hz
        self.device = device
        self.serial = serial or None
        self._list_devices = list_devices
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._station = RDSStation()
        # Called with the station dict when its identity is learned or changes (a new PI, or a new
        # non-empty PS name) — not once per group, which arrive about eleven a second. From the
        # reader thread only and outside the station lock. The console's hook logs it as a
        # reception and never raises, so the call is made directly: it cannot end the decode.
        self.on_reception = None
        self._runs = RunGuard()
        self._init_lifecycle()
        self.error = None
        # Who ended the child, and when the console may launch it again after it died on its own.
        self.watch = ChildWatch(RTL_FM)

    _unavailable = RDSUnavailable
    _released_reason = "the radio was released to another program; resume RDS to take it back"

    def _clear(self):
        self._station = RDSStation()

    def _choose_radio(self) -> dict:
        try:
            return rtlsdr_radio_for_run(self.device, self.serial, self._list_devices)
        except DeviceSelectionError as exc:
            raise RDSUnavailable(str(exc)) from exc

    def _prepare_launch(self, freq_hz, _number):
        for tool in (RTL_FM, REDSEA):
            if not self._which(tool):
                raise RDSUnavailable(f"{tool} is not installed, so SIGINT 101 cannot decode RDS")
        if not self._radio_present():
            raise RDSUnavailable("no RTL-SDR on the USB bus — SIGINT 101 will not fake a station")
        radio = self._choose_radio()

        def launch():
            try:
                proc = self._spawn(freq_hz, radio["index"])
            except (OSError, ValueError) as exc:
                raise RDSUnavailable(f"cannot run the RDS pipeline: {exc}") from exc
            self.device = radio["index"]
            return proc

        return launch, (radio,)

    def _read(self, proc, run: int, radio: dict) -> None:
        """Consume redsea's JSON on a background thread, assembling the station until it ends."""
        named = False   # whether the station on this PI has had its PS name reported yet
        try:
            for raw in proc.stdout:
                group = parse_rds_group(raw)
                if group is None:
                    continue
                with self._runs:
                    if not self._runs.is_current(run):
                        continue
                    station = self._station
                    known_pi = station.pi
                    station.absorb(group)
                    new_pi = station.pi is not None and station.pi != known_pi
                    if new_pi:
                        named = False
                    # Only the first PS name per PI: many stations scroll song titles through
                    # the PS field, and that is one station, not a new one every few seconds.
                    first_name = bool(station.ps) and not named
                    if first_name:
                        named = True
                    identity = station.as_dict() if (new_pi or first_name) else None
                    freq_hz = self.freq_hz
                    cmd = command_line(proc)
                    report = self._admit() if identity is not None else None
                self._emit(report, identity, command=cmd, freq_hz=freq_hz, radio=radio)
        except (OSError, ValueError) as exc:  # loud, never silent
            with self._runs:
                if self._runs.is_current(run):
                    self.error = str(exc)

    def station(self) -> RDSStation:
        """The station assembled so far."""
        with self._runs:
            return self._station
