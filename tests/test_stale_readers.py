# ABOUTME: Tests the retuning and contact decoders against stale readers that outlive a stop (C31).
# ABOUTME: Covers retune, release, stop and died paths, lock ordering, DMR tail and AIS assembler.

from __future__ import annotations

import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from fastapi.testclient import TestClient

from sigint101 import acars, adsb, ais, aprs, childproc, dmr, flock, pocsag, rds, sstv
from sigint101.app import create_app
from sigint101.childproc import ChildWatch, Pipeline
from sigint101.childproc import spawn as spawn_child
from sigint101.events import read_events
from sigint101.hardware import RadioProbe
from tests.child_fakes import LateByteProc, LateProc
from tests.test_sstv import PNG_BYTES

FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "lesson_packs"

# Representative valid output lines (two distinct receptions per module).
ACARS_1 = (
    '{"timestamp":1793151825.5,"station_id":"recon","channel":0,"freq":131.550,'
    '"level":-8.5,"error":0,"mode":"G","label":"H1","block_id":"3","ack":false,'
    '"tail":"F-GTAE","flight":"AF7728","msgno":"D65C","text":"POS REPORT"}'
)
ACARS_2 = (
    '{"timestamp":1793151826.5,"station_id":"recon","channel":1,"freq":131.725,'
    '"level":-12.0,"error":0,"mode":"E","label":"5V","block_id":"4","ack":false,'
    '"tail":"PH-BXR","flight":"KL1681","msgno":"S53A"}'
)

APRS_1 = "APRS: KD2ABC-9>APU25N,WIDE1-1:!4042.00N/07400.00W>Test de KD2ABC"
APRS_2 = "APRS: N0CALL>APRS,WIDE1-1*:>Monitoring 146.52"

DMR_1 = (
    "16:00:15 Sync: +DMR  slot1 [slot2]  | Color Code=03 | VLC SLOT 2 "
    "TGT=21001 SRC=21002 FLCO=0x07 FID=0x10 SVC=0x00 Cap+ Private Call"
)
DMR_2 = "13:50:22 Sync: +P25p1  Group Voice Channel User - Group 2001 Source 1234567"

POCSAG_SECRET = "CALL EXT 2210 URGENT"
POCSAG_1 = f"POCSAG1200: Address: 1234567  Function: 3  Alpha:   {POCSAG_SECRET}"
POCSAG_2 = "POCSAG1200: Address:  123456  Function: 0  Numeric: 1234567890"

RDS_1 = '{"pi":"0x6201","group":"0A","ps":"YLEKLAS1"}'
RDS_2 = '{"pi":"0x7302","group":"0A","ps":"WXYZFM  "}'

ADSB_1 = (
    "MSG,1,1,1,A1B2C3,1,2026/10/28,10:15:00.000,2026/10/28,10:15:00.100,"
    "UAL123  ,,,,,,,,0,0,0,0"
)
ADSB_2 = (
    "MSG,3,1,1,DDEEFF,1,2026/10/28,10:15:03.000,2026/10/28,10:15:03.100,"
    ",39000H,,,37.80000,-122.40000,,,0,0,0,0"
)

AIS_1 = "!AIVDM,1,1,,B,177KQJ5000G?tO`K>RA1wUbN0TKH,0*5C"
AIS_2 = "!AIVDM,1,1,,A,13u?etPv2;0n:dDPwUM1U1Cb069D,0*24"
AIS_TYPE5_FRAG1 = (
    "!AIVDM,2,1,1,A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8,0*1C"
)
AIS_TYPE5_FRAG2 = "!AIVDM,2,2,1,A,88888888880,2*25"

_FLOCK_OUI = bytes.fromhex("b41e52")
_BROADCAST = b"\xff" * 6
_FLOCK_IES = bytes.fromhex(
    "0000" "020100" "0c0100" "7f080000000000000000" "dd07506f9a16030103"
    "2d1a" + "00" * 26 + "bf0c" + "00" * 12 + "dd070050f208000000"
)


def _mgmt_frame(subtype: int, addr1: bytes, addr2: bytes, addr3: bytes, ies: bytes = b"") -> bytes:
    return bytes([subtype << 4, 0x00]) + b"\x00\x00" + addr1 + addr2 + addr3 + b"\x00\x00" + ies


def _pcap_record(dot11: bytes, rt_len: int = 36) -> bytes:
    rec = struct.pack("<BBH", 0, 0, rt_len) + b"\x00" * (rt_len - 4) + dot11
    return struct.pack("<IIII", 0, 0, len(rec), len(rec)) + rec


PCAP_GLOBAL_HDR = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 512, 127)
MARAUDER_PCAP_HDR = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 512, 105)
FLOCK_DOT11_1 = _mgmt_frame(4, _BROADCAST, _FLOCK_OUI + b"\x00\x00\x08", _BROADCAST, _FLOCK_IES)
FLOCK_DOT11_2 = _mgmt_frame(8, _BROADCAST, _FLOCK_OUI + b"\x00\x00\x09", _BROADCAST)
FLOCK_1 = _pcap_record(FLOCK_DOT11_1)
FLOCK_2 = _pcap_record(FLOCK_DOT11_2)


def _retune_proc(tool: str):
    return lambda _run_no, freq_hz, device: LateProc(
        [tool, "-f", str(freq_hz), "-d", str(device)])


@dataclass(frozen=True)
class DecoderSpec:
    name: str
    build: Callable
    unavailable: type
    tool: str
    home_hz: object
    other_hz: int | None
    line1: object
    key1: object
    line2: object
    key2: object
    item_key: Callable
    listed: Callable
    hold_target: Callable   # dec -> (owner_obj, method_name) called inside the buffer lock
    make_proc: Callable
    releases: bool = True

    @property
    def restarts(self) -> tuple[str, ...]:
        paths = []
        if self.other_hz is not None:
            paths.append("retune")
        if self.releases:
            paths.append("release")
        paths.extend(("stop", "died"))
        return tuple(paths)

    @property
    def stand_downs(self) -> tuple[str, ...]:
        paths = []
        if self.other_hz is not None:
            paths.append("retune")
        if self.releases:
            paths.append("release")
        paths.append("stop")
        return tuple(paths)


def _common():
    return {"radio_present": lambda: True, "which": lambda name: "/usr/bin/" + name}


SPECS = (
    DecoderSpec(
        name="acars",
        build=lambda s: acars.AcarsDecoder(spawn=s, **_common()),
        unavailable=acars.AcarsUnavailable,
        tool="acarsdec",
        home_hz=acars.DEFAULT_ACARS_HZ,
        other_hz=131_725_000,
        line1=ACARS_1,
        key1="AF7728",
        line2=ACARS_2,
        key2="KL1681",
        item_key=lambda d: d["flight"],
        listed=lambda dec: [m["flight"] for m in dec.messages()],
        hold_target=lambda dec: (dec._log, "add"),
        make_proc=_retune_proc("acarsdec"),
    ),
    DecoderSpec(
        name="aprs",
        build=lambda s: aprs.AprsDecoder(spawn=s, **_common()),
        unavailable=aprs.AprsUnavailable,
        tool="rtl_fm",
        home_hz=aprs.DEFAULT_APRS_HZ,
        other_hz=144_800_000,
        line1=APRS_1,
        key1="KD2ABC-9",
        line2=APRS_2,
        key2="N0CALL",
        item_key=lambda d: d["source"],
        listed=lambda dec: [s["source"] for s in dec.stations()],
        hold_target=lambda dec: (dec._roster, "absorb"),
        make_proc=_retune_proc("rtl_fm"),
    ),
    DecoderSpec(
        name="dmr",
        build=lambda s: dmr.DmrDecoder(spawn=s, **_common()),
        unavailable=dmr.DmrUnavailable,
        tool="dsd-fme",
        home_hz=dmr.DEFAULT_DMR_HZ,
        other_hz=451_000_000,
        line1=DMR_1,
        key1=21001,
        line2=DMR_2,
        key2=2001,
        item_key=lambda d: d["talkgroup"],
        listed=lambda dec: [c["talkgroup"] for c in dec.calls()],
        hold_target=lambda dec: (dec._log, "add"),
        make_proc=_retune_proc("dsd-fme"),
    ),
    DecoderSpec(
        name="pocsag",
        build=lambda s: pocsag.PocsagDecoder(spawn=s, **_common()),
        unavailable=pocsag.PocsagUnavailable,
        tool="rtl_fm",
        home_hz=pocsag.DEFAULT_POCSAG_HZ,
        other_hz=929_662_500,
        line1=POCSAG_1,
        key1=1234567,
        line2=POCSAG_2,
        key2=123456,
        item_key=lambda d: d["address"],
        listed=lambda dec: [p["address"] for p in dec.pages()],
        hold_target=lambda dec: (dec._log, "add"),
        make_proc=_retune_proc("rtl_fm"),
    ),
    DecoderSpec(
        name="rds",
        build=lambda s: rds.RDSDecoder(freq_hz=rds.DEFAULT_RDS_HZ, spawn=s, **_common()),
        unavailable=rds.RDSUnavailable,
        tool="rtl_fm",
        home_hz=rds.DEFAULT_RDS_HZ,
        other_hz=94_700_000,
        line1=RDS_1,
        key1="0x6201",
        line2=RDS_2,
        key2="0x7302",
        item_key=lambda d: d["pi"],
        listed=lambda dec: [dec.station().pi] if dec.station().pi is not None else [],
        hold_target=lambda dec: (dec._station, "absorb"),
        make_proc=_retune_proc("rtl_fm"),
    ),
    DecoderSpec(
        name="adsb",
        build=lambda s: adsb.AdsbDecoder(spawn=s, **_common()),
        unavailable=adsb.AdsbUnavailable,
        tool="dump1090-fa",
        home_hz=adsb.DEFAULT_ADSB_HZ,
        other_hz=None,
        line1=ADSB_1,
        key1="A1B2C3",
        line2=ADSB_2,
        key2="DDEEFF",
        item_key=lambda d: d["hex_ident"],
        listed=lambda dec: [a["hex_ident"] for a in dec.aircraft()],
        hold_target=lambda dec: (dec._tracker, "absorb"),
        make_proc=lambda run_no, device: LateProc(
            ["dump1090-fa", "--device-index", str(device), "--run", str(run_no)]),
    ),
    DecoderSpec(
        name="ais",
        build=lambda s: ais.AisDecoder(spawn=s, **_common()),
        unavailable=ais.AisUnavailable,
        tool="rtl_ais",
        home_hz=[ais.AIS_CHANNEL_A_HZ, ais.AIS_CHANNEL_B_HZ],
        other_hz=None,
        line1=AIS_1,
        key1=477553000,
        line2=AIS_2,
        key2=265547250,
        item_key=lambda d: d["mmsi"],
        listed=lambda dec: [v["mmsi"] for v in dec.vessels()],
        hold_target=lambda dec: (dec._roster, "absorb"),
        make_proc=lambda run_no, device, host, port: LateProc(
            ["rtl_ais", "-d", str(device), "-h", str(host), "-P", str(port),
             "--run", str(run_no)]),
    ),
    DecoderSpec(
        name="flock",
        build=lambda s: flock.FlockDetector(spawn=s, **_common()),
        unavailable=flock.FlockUnavailable,
        tool="tcpdump",
        home_hz=None,
        other_hz=None,
        line1=FLOCK_1,
        key1="b4:1e:52:00:00:08",
        line2=FLOCK_2,
        key2="b4:1e:52:00:00:09",
        item_key=lambda d: d["mac"],
        listed=lambda dec: [d["mac"] for d in dec.detections()],
        hold_target=lambda dec: (dec._roster, "absorb"),
        make_proc=lambda run_no, interface: LateByteProc(
            ["tcpdump", "-i", str(interface), "--run", str(run_no)],
            preamble=PCAP_GLOBAL_HDR),
        releases=False,
    ),
)

SPEC_BY_NAME = {spec.name: spec for spec in SPECS}
RESTART_CASES = [pytest.param(s, r, id=f"{s.name}-{r}") for s in SPECS for r in s.restarts]
STAND_DOWN_CASES = [pytest.param(s, sd, id=f"{s.name}-{sd}") for s in SPECS for sd in s.stand_downs]
DURING_STOP_CASES = [
    pytest.param(s, sd, during, id=f"{s.name}-{sd}-{during}")
    for s in SPECS
    for sd in ((["retune", "stop"]) if s.other_hz is not None
               else (["release", "stop"] if s.releases else ["stop"]))
    for during in ("stopping", "terminate")
]


def _stand_down_for_restart(dec, spec: DecoderSpec) -> tuple[object, list]:
    """Stand down `dec` by its strongest path so the next start() launches a fresh run."""
    if spec.other_hz is not None:
        dec.retune(spec.other_hz)
        return spec.other_hz, []
    if spec.releases:
        dec.release()
        dec.resume()
        return spec.home_hz, []
    dec.stop()
    return spec.home_hz, [spec.key1]


class _Heard:
    """The console's reception hook in small: logs (key, freq_hz, command) as each arrives.

    Mirrors app.py's S-b hook: explicit `command` and `freq_hz` win over the live attributes.
    """

    def __init__(self, dec, item_key, default_freq=None):
        self.dec = dec
        self.item_key = item_key
        self.default_freq = default_freq
        self.log = []
        self._changed = threading.Condition()

    def __call__(self, item, *, command=None, freq_hz=None):
        freq = freq_hz if freq_hz is not None else getattr(self.dec, "freq_hz", self.default_freq)
        cmd = command if command is not None else self.dec.live_command()
        with self._changed:
            self.log.append((self.item_key(item), freq, cmd))
            self._changed.notify_all()

    def wait_for(self, count, timeout=5.0):
        with self._changed:
            assert self._changed.wait_for(lambda: len(self.log) >= count, timeout), self.log


class _Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


class _HeldLock:
    """Buffer lock that can hold one reader thread just before it enters the lock."""

    def __init__(self):
        self._lock = threading.Lock()
        self.held = None
        self.arrived = threading.Event()
        self.go_on = threading.Event()

    def hold(self, thread):
        self.held = thread

    def __enter__(self):
        if self.held is not None and threading.current_thread() is self.held:
            self.held = None
            self.arrived.set()
            self.go_on.wait(5)
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()


class _TellingLock(_HeldLock):
    """Buffer lock that also signals when a watched thread first tries to acquire it."""

    def __init__(self):
        super().__init__()
        self.watch_for = None
        self.tried = threading.Event()

    def __enter__(self):
        if self.watch_for is not None and threading.current_thread() is self.watch_for:
            self.watch_for = None
            self.tried.set()
        return super().__enter__()


class _OutlivedReader:
    """Thread handle whose join gives up immediately while the reader is held at its write."""

    def __init__(self, thread, inside, go_on):
        self.thread = thread
        self.inside = inside
        self.go_on = go_on

    def join(self, timeout=None):
        if self.inside.is_set() and not self.go_on.is_set():
            return
        self.thread.join(5)


def _line(proc):
    return shlex.join(proc.args)


def _end_pipe(proc):
    proc.close_pipe()
    proc.reader.join(5)
    assert not proc.reader.is_alive()


@pytest.fixture
def late_factory():
    """Build a decoder whose children have pipes that outlive them, cleaned up on teardown."""
    all_procs = []

    def make(spec: DecoderSpec):
        procs = []

        def spawn(*args):
            proc = spec.make_proc(len(procs) + 1, *args)
            procs.append(proc)
            all_procs.append(proc)
            return proc

        dec = spec.build(spawn)
        dec.STOP_WAIT_S = 0.05
        clock = _Clock()
        dec.watch = ChildWatch(dec.watch.tool, clock=clock)
        heard = _Heard(dec, spec.item_key, default_freq=spec.home_hz)
        dec.on_reception = heard
        return dec, procs, heard, clock

    yield make
    for proc in all_procs:
        proc.close_pipe()
    for proc in all_procs:
        if proc.reader is not None:
            proc.reader.join(5)


@pytest.mark.parametrize("spec, restart", RESTART_CASES)
def test_old_reader_outliving_restart_hears_nothing_into_the_new_run(late_factory, spec, restart):
    dec, procs, heard, clock = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    if restart == "retune":
        dec.retune(spec.other_hz)
        dec.start()
        new_freq = spec.other_hz
        kept = []
    elif restart == "release":
        dec.release()
        dec.resume()
        dec.start()
        new_freq = spec.home_hz
        kept = []
    elif restart == "stop":
        dec.stop()
        dec.start()
        new_freq = spec.home_hz
        kept = [spec.key1]
    else:  # died
        procs[0].die(1)
        with pytest.raises(spec.unavailable, match="status 1"):
            dec.start()
        clock.now += 60
        dec.start()
        new_freq = spec.home_hz
        kept = [spec.key1]

    assert procs[0].reader.is_alive()              # the old reader outlived the stop/death
    procs[0].feed(spec.line2)                      # read from the old pipe while the new run runs
    _end_pipe(procs[0])
    assert heard.log == [(spec.key1, spec.home_hz, _line(procs[0]))]
    assert spec.listed(dec) == kept

    # The new run still hears and lists its own reception under the new frequency and command.
    procs[1].feed(spec.line2)
    heard.wait_for(2)
    assert heard.log[1:] == [(spec.key2, new_freq, _line(procs[1]))]
    if spec.other_hz is not None:
        assert str(new_freq) in _line(procs[1])
    assert spec.key2 in spec.listed(dec)


@pytest.mark.parametrize("spec, stand_down", STAND_DOWN_CASES)
def test_old_reader_outliving_stop_hears_nothing_before_the_next_start(
        late_factory, spec, stand_down):
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    if stand_down == "retune":
        dec.retune(spec.other_hz)
        kept = []
    elif stand_down == "release":
        dec.release()
        kept = []
    else:
        dec.stop()
        kept = [spec.key1]

    assert procs[0].reader.is_alive()
    procs[0].feed(spec.line2)
    _end_pipe(procs[0])
    assert heard.log == [(spec.key1, spec.home_hz, _line(procs[0]))]
    assert spec.listed(dec) == kept

    if spec.releases:
        dec.resume()
    dec.start()
    procs[1].feed(spec.line2)
    heard.wait_for(2)
    expected_freq = getattr(dec, "freq_hz", spec.home_hz)
    assert heard.log[1:] == [(spec.key2, expected_freq, _line(procs[1]))]
    assert spec.key2 in spec.listed(dec)


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_old_readers_pipe_failing_after_retune_is_not_the_new_runs_failure(late_factory, spec):
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)
    _stand_down_for_restart(dec, spec)
    assert procs[0].reader.is_alive()
    dec.start()
    procs[0].break_pipe(OSError("old pipe failed"))
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None
    assert dec.running
    dec.stop()
    dec.start()                                    # not refused with the old run's reason
    assert len(procs) == 3
    assert dec.running


def _park_supervisor_diagnostic(dec, procs, found, go_on):
    enrich = dec.watch.enrich
    returned = threading.Event()

    def wait_before_enrich(proc, reason):
        if proc is procs[0]:
            found.set()
            assert go_on.wait(5)
        enrich(proc, reason)
        returned.set()

    dec.watch.enrich = wait_before_enrich
    return returned


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_old_readers_death_record_landing_after_next_start_is_not_the_new_runs(late_factory, spec):
    dec, procs, heard, clock = late_factory(spec)
    found_ended = dec.watch.ended
    found, go_on = threading.Event(), threading.Event()

    def ended(proc, **kw):
        reason = found_ended(proc, **kw)
        if reason is not None and proc is procs[0]:
            found.set()
            go_on.wait(5)                          # held between finding the death and recording it
        return reason

    diagnostic_done = None
    if hasattr(dec, "_run"):
        diagnostic_done = _park_supervisor_diagnostic(dec, procs, found, go_on)
    else:
        dec.watch.ended = ended
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)
    procs[0].die(1)
    procs[0].close_pipe()
    assert found.wait(5)
    with pytest.raises(spec.unavailable, match="status 1"):
        dec.start()
    clock.now += 60
    dec.start()
    assert len(procs) == 2
    go_on.set()
    if diagnostic_done is not None:
        assert diagnostic_done.wait(5)
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None
    if diagnostic_done is not None:
        assert dec.watch.reason is None
    dec.stop()
    dec.start()                                    # not refused with the old run's reason
    assert len(procs) == 3


def _restart_while_held(dec, lock, clock, spec: DecoderSpec) -> list:
    assert lock.arrived.wait(5)
    _, kept = _stand_down_for_restart(dec, spec)
    clock.now += 60
    dec.start()
    lock.go_on.set()
    return kept


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_read_on_its_way_in_at_restart_is_not_listed_or_logged(late_factory, spec):
    dec, procs, heard, clock = late_factory(spec)
    lock = _HeldLock()
    dec._runs.lock = lock
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)
    lock.hold(procs[0].reader)
    procs[0].feed(spec.line2)
    kept = _restart_while_held(dec, lock, clock, spec)
    _end_pipe(procs[0])
    assert spec.listed(dec) == kept
    assert heard.log == [(spec.key1, spec.home_hz, _line(procs[0]))]
    assert len(procs) == 2


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_pipe_failure_on_its_way_in_at_restart_is_not_the_new_runs(late_factory, spec):
    dec, procs, heard, clock = late_factory(spec)
    lock = _HeldLock()
    dec._runs.lock = lock
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)
    lock.hold(procs[0].reader)
    procs[0].break_pipe(OSError("old pipe failed"))
    _restart_while_held(dec, lock, clock, spec)
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None
    dec.stop()
    dec.start()
    assert len(procs) == 3


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_death_on_its_way_in_at_restart_is_not_the_new_runs(late_factory, spec):
    dec, procs, heard, clock = late_factory(spec)
    lock = _HeldLock()
    dec._runs.lock = lock
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)
    lock.hold(procs[0].reader)
    if hasattr(dec, "_run"):
        # Hold the exit observation as well as the old reader's notification. This is the
        # stop-first interleaving, so the supervisor must see cancellation before the exit.
        ticket = dec._run
        poll = procs[0].poll
        procs[0].poll = lambda: poll() if ticket.cancel.is_set() else None
    procs[0].die(1)
    procs[0].close_pipe()
    _restart_while_held(dec, lock, clock, spec)
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None
    dec.stop()
    dec.start()
    assert len(procs) == 3


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_stop_moves_the_run_on_inside_the_buffer_lock(late_factory, spec):
    # Hold the old reader inside the buffer lock (past its run check) while a stop/retune/release
    # runs in another thread. Because stop() ends the run under that same lock, the stopper waits
    # for the in-flight update: the reception logs under the old run's provenance.
    dec, procs, heard, _ = late_factory(spec)
    lock = _TellingLock()
    dec._runs.lock = lock
    inside, go_on = threading.Event(), threading.Event()
    armed = [False]

    owner, method_name = spec.hold_target(dec)
    plain = getattr(owner, method_name)

    def held_write(*args, **kw):
        if armed[0]:
            armed[0] = False
            inside.set()
            go_on.wait(5)
        return plain(*args, **kw)

    setattr(owner, method_name, held_write)

    dec.start()
    dec._thread = _OutlivedReader(dec._thread, inside, go_on)
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    def hook(item, *, command=None, freq_hz=None):
        heard(item, command=command, freq_hz=freq_hz)
        procs[0].close_pipe()

    dec.on_reception = hook
    armed[0] = True
    procs[0].feed(spec.line2)
    assert inside.wait(5)                          # past its run test, holding the buffer lock

    if spec.other_hz is not None:
        stopper = threading.Thread(target=dec.retune, args=(spec.other_hz,))
        cleared = True
    elif spec.releases:
        stopper = threading.Thread(target=dec.release)
        cleared = True
    else:
        stopper = threading.Thread(target=dec.stop)
        cleared = False
    lock.watch_for = stopper
    stopper.start()
    assert lock.tried.wait(5)                      # stop waits on the buffer lock
    go_on.set()
    stopper.join(10)
    assert not stopper.is_alive()
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert [(key, freq) for key, freq, _ in heard.log] == [
        (spec.key1, spec.home_hz), (spec.key2, spec.home_hz)]
    if cleared:
        assert spec.listed(dec) == []
    else:
        assert set(spec.listed(dec)) == {spec.key1, spec.key2}


@pytest.mark.parametrize("spec, stand_down, during", DURING_STOP_CASES)
def test_what_an_old_reader_reads_during_its_stop_is_not_kept(
        late_factory, spec, stand_down, during):
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)
    old = procs[0]
    owner = dec.watch if during == "stopping" else old
    plain = getattr(owner, during)

    def mid_stop(*args, **kw):
        result = plain(*args, **kw)
        setattr(owner, during, plain)              # once, for this stop only
        old.feed(spec.line2)
        old.break_pipe(OSError("old pipe failed during the stop"))
        if during != "stopping":
            # stopping() runs inside the run-guard critical section, so the reader cannot acquire
            # the buffer lock until stopping() returns; stop()/retune() joins it right after.
            old.reader.join(5)
        return result

    setattr(owner, during, mid_stop)
    if stand_down == "retune":
        dec.retune(spec.other_hz)
    elif stand_down == "release":
        dec.release()
    else:
        dec.stop()
    old.reader.join(5)
    assert not old.reader.is_alive()
    assert heard.log == [(spec.key1, spec.home_hz, _line(old))]
    assert spec.key2 not in spec.listed(dec)
    assert dec.error is None
    if stand_down == "release":
        dec.resume()
    dec.start()                                    # not refused with the old run's reason
    assert len(procs) == 2
    assert dec.running


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
@pytest.mark.parametrize("end, reason", [
    (lambda proc: proc.break_pipe(OSError("new pipe failed")), "new pipe failed"),
    (lambda proc: (proc.die(1), proc.close_pipe()), "status 1"),
], ids=["pipe", "death"])
def test_run_after_retune_still_records_its_own_failure(late_factory, spec, end, reason):
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)
    new_freq, _ = _stand_down_for_restart(dec, spec)
    assert procs[0].reader.is_alive()
    dec.start()
    procs[1].feed(spec.line2)
    heard.wait_for(2)
    assert heard.log[1] == (spec.key2, new_freq, _line(procs[1]))
    end(procs[1])
    procs[1].reader.join(5)
    assert not procs[1].reader.is_alive()
    if hasattr(dec, "_run") and procs[1].poll() is not None:
        assert dec._run.ready.wait(5)
    assert dec.error is not None and reason in dec.error
    dec.stop()
    with pytest.raises(spec.unavailable, match=reason):
        dec.start()


def test_dmr_old_reader_lines_never_pollute_the_new_runs_death_reason(late_factory):
    # Each dsd-fme reader keeps its own last-lines buffer: lines printed by a lingering old
    # dsd-fme after a retune must not show up when the new dsd-fme later dies.
    spec = SPEC_BY_NAME["dmr"]
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)
    dec.retune(spec.other_hz)
    assert procs[0].reader.is_alive()
    dec.start()
    # The new dsd-fme says why it is about to fail, then the lingering old reader prints stale
    # diagnostics before its pipe finally closes.
    procs[1].feed("new dsd-fme: usb_claim_interface error -6")
    procs[0].feed("old dsd-fme: stale diagnostic line 1", "old dsd-fme: stale diagnostic line 2")
    _end_pipe(procs[0])
    procs[1].die(1)
    procs[1].close_pipe()
    procs[1].reader.join(5)
    assert not procs[1].reader.is_alive()
    with pytest.raises(dmr.DmrUnavailable) as exc:
        dec.start()
    reason = str(exc.value)
    assert "new dsd-fme: usb_claim_interface error -6" in reason
    assert "old dsd-fme" not in reason


def test_pocsag_late_page_is_dropped_and_words_stay_withheld(late_factory):
    spec = SPEC_BY_NAME["pocsag"]
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(POCSAG_2)
    heard.wait_for(1)
    dec.retune(spec.other_hz)
    assert procs[0].reader.is_alive()
    dec.start()
    procs[0].feed(POCSAG_1)                        # carries POCSAG_SECRET on the stale pipe
    _end_pipe(procs[0])
    assert dec.pages() == []
    assert len(heard.log) == 1
    assert POCSAG_SECRET not in repr(dec.pages())
    procs[1].feed(POCSAG_1)
    heard.wait_for(2)
    assert dec.pages()[0]["text"] is None
    assert dec.pages()[0]["text_withheld"] is True
    assert POCSAG_SECRET not in repr(dec.pages())


# --- Round 2: stalled hook provenance, natural-death drain, atomic stop/watch invalidation ----


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_reader_stalled_after_unlocking_still_logs_old_runs_frequency_and_command(
        late_factory, spec):
    # Pause the old reader right after it releases the run guard (e.g. stalled on the console's
    # reception lock) past STOP_WAIT_S until the retuned replacement has started. The accepted
    # item must still log under the old run's frequency and command, never the replacement's.
    dec, procs, heard, _ = late_factory(spec)
    paused, go_on = threading.Event(), threading.Event()
    armed = [False]
    lock_free_at_hook = []

    def gated_hook(item, **kw):
        if armed[0]:
            armed[0] = False
            free = dec._runs.lock.acquire(blocking=False)
            if free:
                dec._runs.lock.release()
            lock_free_at_hook.append(free)
            paused.set()
            go_on.wait(5)
        heard(item, **kw)

    dec.on_reception = gated_hook
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    armed[0] = True
    procs[0].feed(spec.line2)
    assert paused.wait(5)
    assert lock_free_at_hook == [True]

    _stand_down_for_restart(dec, spec)
    dec.start()
    assert len(procs) == 2
    if spec.other_hz is not None:
        assert dec.freq_hz == spec.other_hz
    assert dec.live_command() == _line(procs[1])
    assert _line(procs[0]) != _line(procs[1])
    assert procs[0].reader.is_alive()

    go_on.set()
    heard.wait_for(2)
    _end_pipe(procs[0])
    assert heard.log == [
        (spec.key1, spec.home_hz, _line(procs[0])),
        (spec.key2, spec.home_hz, _line(procs[0])),
    ]


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_app_reception_hook_logs_old_provenance_when_reader_stalls_past_retune(tmp_path, spec):
    # End-to-end through app.py's _heard hook: when a reader stalls right after releasing the run
    # guard until the replacement has started, events.jsonl and /api/receptions still record the
    # old run's frequency and command.
    procs = []

    def spawn(*args):
        proc = spec.make_proc(len(procs) + 1, *args)
        procs.append(proc)
        return proc

    def factory():
        dec = spec.build(spawn)
        dec.STOP_WAIT_S = 0.05
        return dec

    def present():
        return RadioProbe(True, "lsusb", "RTL2832U 0bda:2838 on USB bus")
    app = create_app(
        pack_dir=FIXTURE_PACKS, data_dir=tmp_path, probe=present,
        **{f"{spec.name}_factory": factory},
    )
    first_done = threading.Event()
    paused, go_on = threading.Event(), threading.Event()
    seen = [0]
    instrument = "pager" if spec.name == "pocsag" else spec.name
    try:
        with TestClient(app) as client:
            dec = getattr(app.state, spec.name)
            real_hook = dec.on_reception

            def gated_hook(item, **kw):
                seen[0] += 1
                if seen[0] == 2:
                    paused.set()
                    go_on.wait(5)
                real_hook(item, **kw)
                if seen[0] == 1:
                    first_done.set()

            dec.on_reception = gated_hook
            dec.start()
            procs[0].feed(spec.line1)
            assert first_done.wait(5)

            procs[0].feed(spec.line2)
            assert paused.wait(5)

            _stand_down_for_restart(dec, spec)
            dec.start()
            assert len(procs) == 2
            if spec.other_hz is not None:
                assert dec.freq_hz == spec.other_hz
            assert dec.live_command() == _line(procs[1])
            assert _line(procs[0]) != _line(procs[1])

            go_on.set()
            _end_pipe(procs[0])

            receptions = client.get(f"/api/receptions?instrument={instrument}").json()["receptions"]
        events = [e for e in read_events(tmp_path / "events.jsonl") if e["kind"] == "reception"]
        assert len(events) == 2
        second = events[1]["payload"]
        assert spec.item_key(second["item"]) == spec.key2
        assert second["freq_hz"] == spec.home_hz
        assert second["command"] == _line(procs[0])
        assert receptions[0]["payload"]["freq_hz"] == spec.home_hz
        assert receptions[0]["payload"]["command"] == _line(procs[0])
    finally:
        go_on.set()
        for proc in procs:
            proc.close_pipe()
        for proc in procs:
            if proc.reader is not None:
                proc.reader.join(5)


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_natural_death_lets_retiring_reader_drain_complete_item_with_its_provenance(
        late_factory, spec):
    # A child dies with a complete item already read from its pipe. The next poll calls _settle(),
    # which must give the retiring reader a bounded chance to finish and log that item under the
    # dead run's provenance when no replacement starts.
    dec, procs, heard, _ = late_factory(spec)
    lock = _HeldLock()
    dec._runs.lock = lock
    dec.STOP_WAIT_S = 2.0
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    lock.hold(procs[0].reader)
    procs[0].feed(spec.line2)
    procs[0].die(1)
    procs[0].close_pipe()
    assert lock.arrived.wait(5)

    real_thread = dec._thread
    joined = threading.Event()

    class _ReleaseOnJoin:
        def join(self, timeout=None):
            joined.set()
            lock.go_on.set()
            return real_thread.join(timeout)

    dec._thread = _ReleaseOnJoin()

    with pytest.raises(spec.unavailable, match="status 1"):
        dec.start()
    assert joined.is_set()
    assert not real_thread.is_alive()
    assert len(procs) == 1
    assert spec.key2 in spec.listed(dec)
    assert heard.log == [
        (spec.key1, spec.home_hz, _line(procs[0])),
        (spec.key2, spec.home_hz, _line(procs[0])),
    ]


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_eof_during_stop_cannot_commit_retry_and_block_retuned_start(late_factory, spec):
    # If the old child exits and its reader handles EOF right as _runs.end() completes, the
    # watch's stop marker must already be set so ChildWatch.ended() never commits a retry
    # deadline that refuses the replacement start.
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    plain_end = dec._runs.end
    old = procs[0]

    def end_with_eof(*args, **kw):
        result = plain_end(*args, **kw)
        dec._runs.end = plain_end
        old.die(1)
        old.close_pipe()
        old.reader.join(5)
        return result

    dec._runs.end = end_with_eof
    _stand_down_for_restart(dec, spec)
    assert not old.reader.is_alive()
    assert dec.watch.stopped is False
    assert dec.watch.blocked() is None
    assert dec.error is None
    # Without advancing the clock, the next start must succeed immediately.
    dec.start()
    assert len(procs) == 2
    assert dec.running
    if spec.other_hz is not None:
        assert dec.freq_hz == spec.other_hz


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_retuned_start_succeeds_when_old_reader_was_waiting_on_exit_status_during_stop(
        late_factory, spec):
    # The old reader has reached EOF and is waiting inside exit_reason() for the child's exit
    # status while stop/retune/release runs. Computing exit diagnostics must not hold the buffer
    # lock, and once the stop invalidates the run and watch, the old reader cannot commit a retry.
    dec, procs, heard, _ = late_factory(spec)
    if hasattr(dec, "_run"):
        read = dec._read

        def finish_old_read(proc, *args):
            read(proc, *args)
            if proc is procs[0]:
                slow_wait()

        dec._read = finish_old_read
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    old = procs[0]
    waiting, release = threading.Event(), threading.Event()
    lock_free_during_wait = []
    plain_wait = old.wait

    def slow_wait(timeout=None):
        if threading.current_thread() is old.reader:
            free = dec._runs.lock.acquire(blocking=False)
            if free:
                dec._runs.lock.release()
            lock_free_during_wait.append(free)
            waiting.set()
            release.wait(5)
            old.die(1)
            return 1
        return plain_wait(timeout=timeout)

    if not hasattr(dec, "_run"):
        old.wait = slow_wait
    old.close_pipe()
    assert waiting.wait(5)
    assert lock_free_during_wait == [True]

    _stand_down_for_restart(dec, spec)
    dec.start()
    assert len(procs) == 2
    assert dec.running

    release.set()
    old.reader.join(5)
    assert not old.reader.is_alive()
    assert dec.watch.stopped is False
    assert dec.watch.blocked() is None
    assert dec.error is None
    dec.stop()
    dec.start()
    assert len(procs) == 3
    assert dec.running


# --- PR 2 specifics: AIS per-reader assembler, release reappearance, real socket/Marauder drain -


def test_ais_old_reader_half_message_cannot_combine_with_new_runs_second_half(late_factory):
    # Each AIS reader owns its own AisAssembler: fragment 1 from a lingering old reader after
    # release/restart must never pair with fragment 2 on the new run to complete a vessel.
    spec = SPEC_BY_NAME["ais"]
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(AIS_1)
    heard.wait_for(1)

    dec.release()
    dec.resume()
    dec.start()
    assert procs[0].reader.is_alive()

    # The old reader receives part 1 of EVER DIADEM (MMSI 351759000) and finishes; the new run
    # receives only part 2, followed by a real single-sentence vessel to prove it processed past.
    procs[0].feed(AIS_TYPE5_FRAG1)
    _end_pipe(procs[0])

    procs[1].feed(AIS_TYPE5_FRAG2, AIS_2)
    heard.wait_for(2)
    assert [v["mmsi"] for v in dec.vessels()] == [spec.key2]
    assert [key for key, _, _ in heard.log] == [spec.key1, spec.key2]


@pytest.mark.parametrize("name", ["adsb", "ais"])
def test_contact_heard_before_release_does_not_reappear_in_new_run(late_factory, name):
    # A vessel or aircraft already heard before release() must not reappear in the cleared
    # roster/scope when the lingering old reader receives another message for that same contact.
    spec = SPEC_BY_NAME[name]
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    dec.release()
    dec.resume()
    dec.start()
    assert procs[0].reader.is_alive()

    procs[0].feed(spec.line1)
    _end_pipe(procs[0])
    assert spec.listed(dec) == []
    assert len(heard.log) == 1


class _AliveProc:
    """Minimal process handle behind a real adsb._Feed or ais._Feed socket."""

    def __init__(self, args):
        self.args = list(args)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


def test_adsb_socket_buffered_lines_after_stop_do_not_leak_into_new_run():
    # Even though adsb._Feed closes its socket on stop(), lines already split from one recv()
    # chunk keep draining. Stalling on the first line past stop()+start() must drop the second.
    pairs = []
    procs = []

    def spawn(device):
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(adsb.FEED_POLL_S)
        pairs.append((client_sock, server_sock))
        proc = _AliveProc(
            ["dump1090-fa", "--device-index", str(device), "--run", str(len(procs) + 1)])
        procs.append(proc)
        return adsb._Feed(proc, client_sock)

    dec = adsb.AdsbDecoder(spawn=spawn, **_common())
    dec.STOP_WAIT_S = 0.05
    heard = _Heard(dec, lambda d: d["hex_ident"], default_freq=adsb.DEFAULT_ADSB_HZ)
    paused, go_on = threading.Event(), threading.Event()
    first = [True]

    def gated(item, **kw):
        heard(item, **kw)
        if first[0]:
            first[0] = False
            paused.set()
            go_on.wait(5)

    dec.on_reception = gated
    try:
        dec.start()
        old_thread = dec._thread
        # Deliver two SBS lines in one TCP chunk so the second stays buffered inside _Feed._lines.
        pairs[0][1].sendall((ADSB_1 + "\n" + ADSB_2 + "\n").encode())
        assert paused.wait(5)

        dec.release()
        dec.resume()
        dec.start()
        assert len(procs) == 2

        go_on.set()
        pairs[0][1].close()
        old_thread.join(5)
        assert not old_thread.is_alive()
        dec.stop()
        assert dec.aircraft() == []
        assert heard.log == [("A1B2C3", adsb.DEFAULT_ADSB_HZ, _line(procs[0]))]
    finally:
        go_on.set()
        dec.stop()
        for a, b in pairs:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass


def test_ais_udp_buffered_lines_after_stop_do_not_leak_into_new_run():
    # One UDP datagram carrying two AIVDM lines: closing the socket on stop() does not stop the
    # inner splitlines() loop from yielding the second line once the hook unblocks.
    socks = []
    procs = []

    def spawn(device, host, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        socks.append(sock)
        proc = _AliveProc(["rtl_ais", "-d", str(device), "--run", str(len(procs) + 1)])
        procs.append(proc)
        return ais._Feed(proc, sock)

    dec = ais.AisDecoder(spawn=spawn, **_common())
    dec.STOP_WAIT_S = 0.05
    heard = _Heard(dec, lambda d: d["mmsi"],
                   default_freq=[ais.AIS_CHANNEL_A_HZ, ais.AIS_CHANNEL_B_HZ])
    paused, go_on = threading.Event(), threading.Event()
    first = [True]

    def gated(item, **kw):
        heard(item, **kw)
        if first[0]:
            first[0] = False
            paused.set()
            go_on.wait(5)

    dec.on_reception = gated
    try:
        dec.start()
        old_thread = dec._thread
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto((AIS_1 + "\n" + AIS_2 + "\n").encode(), socks[0].getsockname())
        assert paused.wait(5)

        dec.release()
        dec.resume()
        dec.start()
        assert len(procs) == 2

        go_on.set()
        old_thread.join(5)
        assert not old_thread.is_alive()
        dec.stop()
        assert dec.vessels() == []
        assert heard.log == [
            (477553000, [ais.AIS_CHANNEL_A_HZ, ais.AIS_CHANNEL_B_HZ], _line(procs[0])),
        ]
    finally:
        go_on.set()
        dec.stop()
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


def test_marauder_buffered_frames_after_stop_do_not_leak_into_new_run(tmp_path):
    # MarauderFlockDetector inherits FlockDetector's RunGuard: closing the Marauder serial link
    # on stop() does not discard frames already buffered inside iter_marauder_frames(), so the
    # run guard must drop the second frame rather than listing or logging it under the new run.
    tty = tmp_path / "marauder"
    tty.write_text("", encoding="utf-8")

    def marauder_record(dot11: bytes) -> bytes:
        return struct.pack("<IIII", 1, 0, len(dot11), len(dot11)) + dot11

    chunk = (
        flock.MARAUDER_BEGIN
        + MARAUDER_PCAP_HDR
        + marauder_record(FLOCK_DOT11_1)
        + marauder_record(FLOCK_DOT11_2)
        + flock.MARAUDER_CLOSE
    )
    links = []

    class _FakeMarauderLink(LateByteProc):
        def __init__(self, run_no: int):
            super().__init__(["sniffprobe", "-serial", "--run", str(run_no)])
            self.answered = True
            self.end_reason = None

        def end(self, reason=None):
            self.end_reason = reason
            self.terminate()

    dec = flock.MarauderFlockDetector(tty=str(tty))
    dec.STOP_WAIT_S = 0.05

    def open_capture():
        link = _FakeMarauderLink(len(links) + 1)
        links.append(link)
        return link

    dec._open_capture = open_capture
    heard = _Heard(dec, lambda d: d["mac"], default_freq=None)
    paused, go_on = threading.Event(), threading.Event()
    first = [True]

    def gated(item, **kw):
        heard(item, **kw)
        if first[0]:
            first[0] = False
            paused.set()
            go_on.wait(5)

    dec.on_reception = gated
    try:
        dec.start()
        links[0].feed(chunk)
        assert paused.wait(5)

        dec.stop()
        dec.start()
        assert len(links) == 2

        go_on.set()
        _end_pipe(links[0])
        assert [d["mac"] for d in dec.detections()] == ["b4:1e:52:00:00:08"]
        assert heard.log == [("b4:1e:52:00:00:08", None, _line(links[0]))]
    finally:
        go_on.set()
        for link in links:
            link.close_pipe()
        dec.stop()
        for link in links:
            if link.reader is not None:
                link.reader.join(5)


# --- C39: natural-exit socket drain, live downstream settle, and post-drain invalidation -------


class _BoundarySocket:
    """Real socket whose first post-arm recv times out just as final bytes and child exit land."""

    def __init__(self, sock: socket.socket, on_timeout):
        self._sock = sock
        self._on_timeout = on_timeout
        self.armed = threading.Event()
        self._fired = False

    def recv(self, bufsize: int):
        if self.armed.is_set() and not self._fired:
            self._fired = True
            self._on_timeout()
            raise TimeoutError("timed out")
        return self._sock.recv(bufsize)

    def __enter__(self):
        self._sock.__enter__()
        return self

    def __exit__(self, *exc):
        return self._sock.__exit__(*exc)

    def __getattr__(self, name: str):
        return getattr(self._sock, name)


def _reap_stage(proc) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


@pytest.mark.parametrize("action", ["stop", "retune"])
def test_natural_exit_settle_with_live_downstream_stage_does_not_delay_stop_or_retune(
        monkeypatch, action):
    # When an upstream pipeline stage dies while a downstream stage stays alive holding stdout
    # open, _settle() must bring every stage down before joining the reader so a concurrent stop
    # or retune never waits out STOP_WAIT_S on the lifecycle lock.
    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0.05)
    stages = []
    upstream_in = []

    def spawn(freq_hz, device):
        up = spawn_child(
            [sys.executable, "-c", "import sys; sys.stdin.read(1); sys.exit(1)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        down = spawn_child(
            [sys.executable, "-c",
             "import sys, time; print(sys.argv[1], flush=True); time.sleep(30)", APRS_1],
            stdin=up.stdout, stdout=subprocess.PIPE, text=True)
        up.stdout.close()
        stages.extend((up, down))
        upstream_in.append(up.stdin)
        return Pipeline(up, down)

    dec = aprs.AprsDecoder(spawn=spawn, **_common())
    dec.STOP_WAIT_S = 0.6
    clock = _Clock()
    dec.watch = ChildWatch(dec.watch.tool, clock=clock)
    heard = _Heard(dec, lambda d: d["source"], default_freq=aprs.DEFAULT_APRS_HZ)
    dec.on_reception = heard
    try:
        dec.start()
        heard.wait_for(1)
        # Kill the upstream stage while the downstream stage remains alive holding stdout open.
        upstream_in[0].close()
        stages[0].wait(timeout=5)
        assert stages[0].poll() == 1
        assert stages[1].poll() is None

        settling = threading.Event()
        real_stopping = dec.watch.stopping

        def mark_stopping():
            real_stopping()
            settling.set()

        dec.watch.stopping = mark_stopping

        start_error = []

        def run_start():
            try:
                dec.start()
            except Exception as exc:
                start_error.append(exc)

        starter = threading.Thread(target=run_start)
        starter.start()
        assert settling.wait(5)

        t0 = time.monotonic()
        if action == "stop":
            dec.stop()
        else:
            dec.retune(144_800_000)
        elapsed = time.monotonic() - t0
        starter.join(5)

        assert not starter.is_alive()
        assert len(start_error) == 1 and isinstance(start_error[0], aprs.AprsUnavailable)
        assert stages[1].poll() is not None
        assert elapsed < dec.STOP_WAIT_S / 2
    finally:
        dec.stop()
        if dec._run is not None:
            assert dec._run.complete.wait(5)
        for stage in stages:
            _reap_stage(stage)


def test_pipeline_natural_exit_reaps_live_downstream_and_drains_buffered_pipe(monkeypatch):
    # Stopping a live downstream stage before the join leaves already-buffered stdout readable in
    # the pipe: the downstream process is already dead when join() runs, yet the second buffered
    # APRS packet still drains under the dead run's provenance.
    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0.05)
    stages = []
    upstream_in = []

    def spawn(freq_hz, device):
        up = spawn_child(
            [sys.executable, "-c", "import sys; sys.stdin.read(1); sys.exit(1)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        down = spawn_child(
            [sys.executable, "-c",
             "import sys, time; print(sys.argv[1], flush=True); "
             "print(sys.argv[2], flush=True); time.sleep(30)",
             APRS_1, APRS_2],
            stdin=up.stdout, stdout=subprocess.PIPE, text=True)
        up.stdout.close()
        stages.extend((up, down))
        upstream_in.append(up.stdin)
        return Pipeline(up, down)

    dec = aprs.AprsDecoder(spawn=spawn, **_common())
    dec.EOF_WAIT_S = 0.05
    dec.STOP_WAIT_S = 1.0
    clock = _Clock()
    dec.watch = ChildWatch(dec.watch.tool, clock=clock)
    heard = _Heard(dec, lambda d: d["source"], default_freq=aprs.DEFAULT_APRS_HZ)
    paused, go_on = threading.Event(), threading.Event()
    first = [True]

    def gated(item, **kw):
        heard(item, **kw)
        if first[0]:
            first[0] = False
            paused.set()
            go_on.wait(5)

    dec.on_reception = gated
    try:
        dec.start()
        assert paused.wait(5)
        expected_cmd = childproc.command_line(dec._proc)

        upstream_in[0].close()
        stages[0].wait(timeout=5)
        assert stages[0].poll() == 1
        assert stages[1].poll() is None

        real_thread = dec._thread
        down_poll_at_join = []

        class _CheckAtJoin:
            def join(self, timeout=None):
                down_poll_at_join.append(stages[1].poll())
                go_on.set()
                return real_thread.join(timeout)

        dec._thread = _CheckAtJoin()
        with pytest.raises(aprs.AprsUnavailable, match="status 1"):
            dec.start()

        assert down_poll_at_join and down_poll_at_join[0] is not None
        assert not real_thread.is_alive()
        assert {s["source"] for s in dec.stations()} == {"KD2ABC-9", "N0CALL"}
        assert heard.log == [
            ("KD2ABC-9", aprs.DEFAULT_APRS_HZ, expected_cmd),
            ("N0CALL", aprs.DEFAULT_APRS_HZ, expected_cmd),
        ]
    finally:
        go_on.set()
        dec.stop()
        if dec._run is not None:
            assert dec._run.complete.wait(5)
        for stage in stages:
            _reap_stage(stage)


def test_adsb_natural_exit_drains_complete_sbs_line_queued_in_socket():
    # Pause the ADS-B reader on its first accepted item, queue a second complete SBS line in the
    # TCP socket while leaving the peer open and idle, mark dump1090 exited, and give the drain a
    # budget shorter than FEED_POLL_S. Only non-blocking draining finishes before the join budget.
    pairs = []
    procs = []

    def spawn(device):
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(adsb.FEED_POLL_S)
        pairs.append((client_sock, server_sock))
        proc = _AliveProc(
            ["dump1090-fa", "--device-index", str(device), "--run", str(len(procs) + 1)])
        procs.append(proc)
        return adsb._Feed(proc, client_sock)

    dec = adsb.AdsbDecoder(spawn=spawn, **_common())
    dec.STOP_WAIT_S = 0.15
    assert dec.STOP_WAIT_S < adsb.FEED_POLL_S
    clock = _Clock()
    dec.watch = ChildWatch(dec.watch.tool, clock=clock)
    heard = _Heard(dec, lambda d: d["hex_ident"], default_freq=adsb.DEFAULT_ADSB_HZ)
    paused, go_on = threading.Event(), threading.Event()
    first = [True]

    def gated(item, **kw):
        heard(item, **kw)
        if first[0]:
            first[0] = False
            paused.set()
            go_on.wait(5)

    dec.on_reception = gated
    try:
        dec.start()
        pairs[0][1].sendall((ADSB_1 + "\n").encode())
        assert paused.wait(5)

        # Queue the second SBS line in the socket buffer and end the child naturally; keep the
        # peer socket open and idle so a blocking recv() would wait out FEED_POLL_S.
        pairs[0][1].sendall((ADSB_2 + "\n").encode())
        real_thread = dec._thread
        joined = threading.Event()
        alive_after_join = []

        class _ReleaseOnJoin:
            def join(self, timeout=None):
                joined.set()
                go_on.set()
                real_thread.join(timeout)
                alive_after_join.append(real_thread.is_alive())

        dec._thread = _ReleaseOnJoin()
        procs[0].returncode = 1


        with pytest.raises(adsb.AdsbUnavailable, match="status 1"):
            dec.start()
        assert joined.is_set()
        assert alive_after_join == [False]
        assert not real_thread.is_alive()
        assert len(procs) == 1
        assert {a["hex_ident"] for a in dec.aircraft()} == {"A1B2C3", "DDEEFF"}
        assert heard.log == [
            ("A1B2C3", adsb.DEFAULT_ADSB_HZ, _line(procs[0])),
            ("DDEEFF", adsb.DEFAULT_ADSB_HZ, _line(procs[0])),
        ]
    finally:
        go_on.set()
        dec.stop()
        for a, b in pairs:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass


def test_adsb_live_read_timeout_as_last_sbs_line_arrives_still_drains():
    # If a live blocking recv() times out right as dump1090 exits and the final SBS line arrives,
    # the feed must loop once more in non-blocking mode and drain that line rather than exiting
    # on the TimeoutError.
    pairs = []
    procs = []
    boundary = []
    timed_out = threading.Event()

    def spawn(device):
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(adsb.FEED_POLL_S)
        pairs.append((client_sock, server_sock))
        proc = _AliveProc(
            ["dump1090-fa", "--device-index", str(device), "--run", str(len(procs) + 1)])
        procs.append(proc)

        def on_timeout():
            server_sock.sendall((ADSB_2 + "\n").encode())
            proc.returncode = 1
            timed_out.set()

        wrapped = _BoundarySocket(client_sock, on_timeout)
        boundary.append(wrapped)
        return adsb._Feed(proc, wrapped)

    dec = adsb.AdsbDecoder(spawn=spawn, **_common())
    dec.STOP_WAIT_S = 0.15
    assert dec.STOP_WAIT_S < adsb.FEED_POLL_S
    clock = _Clock()
    dec.watch = ChildWatch(dec.watch.tool, clock=clock)
    heard = _Heard(dec, lambda d: d["hex_ident"], default_freq=adsb.DEFAULT_ADSB_HZ)
    paused, go_on = threading.Event(), threading.Event()
    first = [True]

    def gated(item, **kw):
        heard(item, **kw)
        if first[0]:
            first[0] = False
            paused.set()
            go_on.wait(5)

    dec.on_reception = gated
    try:
        dec.start()
        pairs[0][1].sendall((ADSB_1 + "\n").encode())
        assert paused.wait(5)

        real_thread = dec._thread
        alive_after_join = []

        class _CheckJoin:
            def join(self, timeout=None):
                real_thread.join(timeout)
                alive_after_join.append(real_thread.is_alive())

        dec._thread = _CheckJoin()
        boundary[0].armed.set()
        go_on.set()
        assert timed_out.wait(5)

        with pytest.raises(adsb.AdsbUnavailable, match="status 1"):
            dec.start()
        assert alive_after_join == [False]
        assert {a["hex_ident"] for a in dec.aircraft()} == {"A1B2C3", "DDEEFF"}
        assert heard.log == [
            ("A1B2C3", adsb.DEFAULT_ADSB_HZ, _line(procs[0])),
            ("DDEEFF", adsb.DEFAULT_ADSB_HZ, _line(procs[0])),
        ]
    finally:
        go_on.set()
        dec.stop()
        for a, b in pairs:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass


def test_ais_natural_exit_drains_complete_datagram_queued_in_socket():
    # Pause the AIS reader on its first accepted vessel, queue a second complete UDP datagram in
    # the socket, mark rtl_ais exited, and give the drain a budget shorter than FEED_POLL_S. Only
    # non-blocking draining finishes before the join budget on an idle UDP socket.
    socks = []
    procs = []

    def spawn(device, host, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        socks.append(sock)
        proc = _AliveProc(["rtl_ais", "-d", str(device), "--run", str(len(procs) + 1)])
        procs.append(proc)
        return ais._Feed(proc, sock)

    dec = ais.AisDecoder(spawn=spawn, **_common())
    dec.STOP_WAIT_S = 0.15
    assert dec.STOP_WAIT_S < ais.FEED_POLL_S
    clock = _Clock()
    dec.watch = ChildWatch(dec.watch.tool, clock=clock)
    ais_hz = [ais.AIS_CHANNEL_A_HZ, ais.AIS_CHANNEL_B_HZ]
    heard = _Heard(dec, lambda d: d["mmsi"], default_freq=ais_hz)
    paused, go_on = threading.Event(), threading.Event()
    first = [True]

    def gated(item, **kw):
        heard(item, **kw)
        if first[0]:
            first[0] = False
            paused.set()
            go_on.wait(5)

    dec.on_reception = gated
    try:
        dec.start()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto((AIS_1 + "\n").encode(), socks[0].getsockname())
            assert paused.wait(5)
            # Queue the second datagram in the UDP socket buffer and end the child naturally.
            sender.sendto((AIS_2 + "\n").encode(), socks[0].getsockname())
        real_thread = dec._thread
        joined = threading.Event()
        alive_after_join = []

        class _ReleaseOnJoin:
            def join(self, timeout=None):
                joined.set()
                go_on.set()
                real_thread.join(timeout)
                alive_after_join.append(real_thread.is_alive())

        dec._thread = _ReleaseOnJoin()
        procs[0].returncode = 1


        with pytest.raises(ais.AisUnavailable, match="status 1"):
            dec.start()
        assert joined.is_set()
        assert alive_after_join == [False]
        assert not real_thread.is_alive()
        assert len(procs) == 1
        assert {v["mmsi"] for v in dec.vessels()} == {477553000, 265547250}
        assert heard.log == [
            (477553000, ais_hz, _line(procs[0])),
            (265547250, ais_hz, _line(procs[0])),
        ]
    finally:
        go_on.set()
        dec.stop()
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


def test_ais_live_read_timeout_as_last_datagram_arrives_still_drains():
    # If a live blocking recv() times out right as rtl_ais exits and the final UDP datagram
    # arrives, the feed must loop once more in non-blocking mode and drain it rather than exiting
    # on the TimeoutError.
    socks = []
    procs = []
    boundary = []
    timed_out = threading.Event()

    def spawn(device, host, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        socks.append(sock)
        proc = _AliveProc(["rtl_ais", "-d", str(device), "--run", str(len(procs) + 1)])
        procs.append(proc)

        def on_timeout():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto((AIS_2 + "\n").encode(), sock.getsockname())
            proc.returncode = 1
            timed_out.set()

        wrapped = _BoundarySocket(sock, on_timeout)
        boundary.append(wrapped)
        return ais._Feed(proc, wrapped)

    dec = ais.AisDecoder(spawn=spawn, **_common())
    dec.STOP_WAIT_S = 0.15
    assert dec.STOP_WAIT_S < ais.FEED_POLL_S
    clock = _Clock()
    dec.watch = ChildWatch(dec.watch.tool, clock=clock)
    ais_hz = [ais.AIS_CHANNEL_A_HZ, ais.AIS_CHANNEL_B_HZ]
    heard = _Heard(dec, lambda d: d["mmsi"], default_freq=ais_hz)
    paused, go_on = threading.Event(), threading.Event()
    first = [True]

    def gated(item, **kw):
        heard(item, **kw)
        if first[0]:
            first[0] = False
            paused.set()
            go_on.wait(5)

    dec.on_reception = gated
    try:
        dec.start()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto((AIS_1 + "\n").encode(), socks[0].getsockname())
        assert paused.wait(5)

        real_thread = dec._thread
        alive_after_join = []

        class _CheckJoin:
            def join(self, timeout=None):
                real_thread.join(timeout)
                alive_after_join.append(real_thread.is_alive())

        dec._thread = _CheckJoin()
        boundary[0].armed.set()
        go_on.set()
        assert timed_out.wait(5)

        with pytest.raises(ais.AisUnavailable, match="status 1"):
            dec.start()
        assert alive_after_join == [False]
        assert {v["mmsi"] for v in dec.vessels()} == {477553000, 265547250}
        assert heard.log == [
            (477553000, ais_hz, _line(procs[0])),
            (265547250, ais_hz, _line(procs[0])),
        ]
    finally:
        go_on.set()
        dec.stop()
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_after_natural_exit_drain_times_out_old_reader_output_reaches_nothing(late_factory, spec):
    # Once _settle()'s bounded drain expires on a natural death, the run must end even when no
    # replacement starts: anything the lingering reader delivers during retry back-off is dropped.
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    procs[0].die(1)
    with pytest.raises(spec.unavailable, match="status 1"):
        dec.start()
    assert len(procs) == 1
    assert procs[0].reader.is_alive()
    assert dec.watch.blocked() is not None

    # Still stopped in retry back-off: late output must neither list nor log.
    procs[0].feed(spec.line2)
    _end_pipe(procs[0])
    assert spec.listed(dec) == [spec.key1]
    assert heard.log == [(spec.key1, spec.home_hz, _line(procs[0]))]


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_after_natural_exit_drain_times_out_old_reader_error_does_not_refuse_next_start(
        late_factory, spec):
    # A pipe failure from a reader that outlived its natural-exit drain must not set dec.error
    # during retry back-off and make the next eligible start refuse.
    dec, procs, heard, clock = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    procs[0].die(1)
    with pytest.raises(spec.unavailable, match="status 1"):
        dec.start()
    assert len(procs) == 1
    assert procs[0].reader.is_alive()
    assert dec.error is None

    procs[0].break_pipe(OSError("late pipe failure after drain"))
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None

    clock.now += 60
    dec.start()
    assert len(procs) == 2
    assert dec.running


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_after_natural_exit_drain_times_out_old_reader_death_record_reaches_nothing(
        late_factory, spec):
    # If the old reader finishes watch.ended() before _settle()'s drain times out and tries to
    # record dec.error afterwards while stopped in back-off, the ended run must block it so the
    # next eligible start succeeds rather than refusing again.
    dec, procs, heard, clock = late_factory(spec)
    found_ended = dec.watch.ended
    found, go_on = threading.Event(), threading.Event()

    def ended(proc, **kw):
        reason = found_ended(proc, **kw)
        if reason is not None and proc is procs[0]:
            found.set()
            go_on.wait(5)
        return reason

    diagnostic_done = None
    if hasattr(dec, "_run"):
        diagnostic_done = _park_supervisor_diagnostic(dec, procs, found, go_on)
    else:
        dec.watch.ended = ended
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)

    procs[0].die(1)
    procs[0].close_pipe()
    assert found.wait(5)
    with pytest.raises(spec.unavailable, match="status 1"):
        dec.start()
    assert len(procs) == 1
    assert dec.error is None

    # Release the old reader while still stopped in back-off (before any replacement starts).
    go_on.set()
    if diagnostic_done is not None:
        assert diagnostic_done.wait(5)
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None

    clock.now += 60
    dec.start()
    assert len(procs) == 2
    assert dec.running


def test_pipe_decoder_diagnostic_failure_during_settle_ends_run_stops_proc_and_drops_queued_item(
        monkeypatch):
    # A failed diagnostic leaves a usable exit reason. Reap and fence the old reader so a queued
    # item cannot publish after the drain deadline or change the replacement run's watch.
    procs = []

    class _TrackedLateProc(LateProc):
        def __init__(self, args):
            super().__init__(args)
            self.terminated = False

        def terminate(self):
            self.terminated = True
            super().terminate()

    def spawn(freq_hz, device):
        proc = _TrackedLateProc(["multimon-ng", "-f", str(freq_hz), "-d", str(device)])
        procs.append(proc)
        return proc

    dec = aprs.AprsDecoder(spawn=spawn, **_common())
    dec.STOP_WAIT_S = 0.15
    clock = _Clock()
    dec.watch = ChildWatch(dec.watch.tool, clock=clock)
    heard = _Heard(dec, lambda d: d["source"], default_freq=aprs.DEFAULT_APRS_HZ)
    paused, go_on = threading.Event(), threading.Event()
    first = [True]

    def gated(item, **kw):
        heard(item, **kw)
        if first[0]:
            first[0] = False
            paused.set()
            go_on.wait(5)

    dec.on_reception = gated
    try:
        dec.start()
        procs[0].feed(APRS_1)
        assert paused.wait(5)

        # Queue a second complete packet and mark the child exited, then fail exit diagnostics.
        procs[0].feed(APRS_2)
        def boom(_stage):
            raise OSError("stderr read failed")

        monkeypatch.setattr(childproc, "stderr_tail", boom)
        procs[0].die(1)
        real_thread = dec._thread

        with pytest.raises(aprs.AprsUnavailable, match="exit status 1.*stderr read failed"):
            dec.start()
        assert procs[0].terminated is True

        # Resuming the detached reader must publish nothing to the roster or reception hook.
        go_on.set()
        procs[0].close_pipe()
        real_thread.join(5)
        assert not real_thread.is_alive()
        assert "exit status 1" in dec.watch.reason and "stderr read failed" in dec.watch.reason
        clock.now += 60
        dec.start()
        assert dec.watch.stopped is False
        assert dec.error is None
        assert [s["source"] for s in dec.stations()] == ["KD2ABC-9"]
        assert heard.log == [("KD2ABC-9", aprs.DEFAULT_APRS_HZ, _line(procs[0]))]
    finally:
        go_on.set()
        for proc in procs:
            proc.close_pipe()
        dec.stop()


def test_adsb_socket_diagnostic_failure_during_settle_ends_run_and_closes_socket(monkeypatch):
    # A diagnostic error still leaves an exit reason, a stopped child and a closed socket.
    # A reader that outlived the drain cannot publish its queued SBS line.
    pairs = []
    procs = []

    class _TrackedAliveProc(_AliveProc):
        def __init__(self, args):
            super().__init__(args)
            self.terminated = False

        def terminate(self):
            self.terminated = True
            super().terminate()

    def spawn(device):
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(adsb.FEED_POLL_S)
        pairs.append((client_sock, server_sock))
        proc = _TrackedAliveProc(
            ["dump1090-fa", "--device-index", str(device), "--run", str(len(procs) + 1)])
        procs.append(proc)
        return adsb._Feed(proc, client_sock)

    dec = adsb.AdsbDecoder(spawn=spawn, **_common())
    dec.STOP_WAIT_S = 0.15
    clock = _Clock()
    dec.watch = ChildWatch(dec.watch.tool, clock=clock)
    heard = _Heard(dec, lambda d: d["hex_ident"], default_freq=adsb.DEFAULT_ADSB_HZ)
    paused, go_on = threading.Event(), threading.Event()
    first = [True]

    def gated(item, **kw):
        heard(item, **kw)
        if first[0]:
            first[0] = False
            paused.set()
            go_on.wait(5)

    dec.on_reception = gated
    try:
        dec.start()
        pairs[0][1].sendall((ADSB_1 + "\n").encode())
        assert paused.wait(5)

        pairs[0][1].sendall((ADSB_2 + "\n").encode())
        def boom(_stage):
            raise OSError("stderr read failed")

        monkeypatch.setattr(childproc, "stderr_tail", boom)
        real_thread = dec._thread
        procs[0].returncode = 1

        with pytest.raises(adsb.AdsbUnavailable, match="exit status 1.*stderr read failed"):
            dec.start()
        assert procs[0].terminated is True
        assert pairs[0][0].fileno() == -1

        # Resuming the detached reader must not publish the queued SBS line.
        go_on.set()
        real_thread.join(5)
        assert not real_thread.is_alive()
        assert "exit status 1" in dec.watch.reason and "stderr read failed" in dec.watch.reason
        clock.now += 60
        dec.start()
        assert dec.watch.stopped is False
        assert dec.error is None
        assert {a["hex_ident"] for a in dec.aircraft()} == {"A1B2C3"}
        assert heard.log == [("A1B2C3", adsb.DEFAULT_ADSB_HZ, _line(procs[0]))]
    finally:
        go_on.set()
        dec.stop()
        for a, b in pairs:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass


# --- SSTV retires a finite capture while its reader may outlive it ------------------


@pytest.fixture
def late_sstv(tmp_path):
    children, paths, reports, readers, gates = [], [], [], [], []
    clock = _Clock()

    def spawn(freq, device, path):
        child = LateProc(["sstv", "--freq", str(freq), "-o", str(path)])
        children.append(child)
        paths.append(path)
        return child

    dec = sstv.SstvDecoder(spawn=spawn, output_dir=tmp_path / "sstv", data_dir=tmp_path,
                           clock=clock, **_common())
    dec.watch.clock = clock
    dec.STOP_WAIT_S = 0.03

    def heard(item, *, command=None, freq_hz=None):
        reports.append((item, command, freq_hz))

    def reader_at_join(action=None):
        thread = dec._thread
        join = thread.join
        readers.append(join)

        def bounded_join(timeout=None):
            if action is not None:
                action()
            join(0.03 if action is None else 2)

        thread.join = bounded_join
        return join

    def gate():
        event = threading.Event()
        gates.append(event)
        return event

    dec.on_reception = heard
    yield dec, children, paths, reports, clock, reader_at_join, gate
    for event in gates:
        event.set()
    for child in children:
        child.close_pipe()
    dec.stop()
    for join in readers:
        join(2)


def _sstv_end(child, *, code=0, mode=None):
    child.die(code)
    if mode is not None:
        child.feed(f"Detected SSTV mode {mode}")
    child.close_pipe()


def test_sstv_finite_pipeline_reaps_live_upstream_before_join_while_draining_mode(
        late_sstv, monkeypatch):
    dec, children, paths, heard, _, reader_at_join, _ = late_sstv
    rtl_fm, sox = LateProc(["rtl_fm"]), LateProc(["sox"])
    spawn = dec._spawn
    monkeypatch.setattr(dec, "_spawn", lambda *args: Pipeline(
        rtl_fm, sox, spawn(*args), finite=True))
    dec.start()
    child = children[0]
    paths[0].write_bytes(PNG_BYTES)
    stages_at_join = []

    def after_reap():
        stages_at_join.append((rtl_fm.poll(), sox.poll()))
        child.feed("Detected SSTV mode Robot 36")
        child.close_pipe()

    reader_at_join(after_reap)
    expected = childproc.command_line(dec._proc)
    child.die(0)
    dec.stop()
    assert stages_at_join and None not in stages_at_join[0]
    [(image, command, freq)] = heard
    assert (command, freq, image["mode"]) == (expected, sstv.DEFAULT_SSTV_HZ, "Robot 36")


@pytest.mark.parametrize("control", ["stop", "release"])
@pytest.mark.parametrize("boundary", ["parsed", "unread", "exited"])
def test_sstv_mode_commit_and_stop_release_boundary(late_sstv, control, boundary):
    dec, children, paths, heard, _, reader_at_join, _ = late_sstv
    dec.start()
    child = children[0]
    staged = paths[0]
    staged.write_bytes(PNG_BYTES)
    capture = getattr(dec, "_capture", None)
    if boundary == "parsed":
        # A sentinel fetched after the first mode proves the reader processed that mode.
        parsed = threading.Event()

        class Sentinel(str):
            def strip(self):
                parsed.set()
                return super().strip()

        child.feed("Detected SSTV mode Robot 36", Sentinel("working"))
        assert parsed.wait(2)

    ticket = dec._run
    if boundary == "exited":
        reader_at_join(lambda: _sstv_end(child, mode="Robot 36"))
        child.die(0)
    else:
        join = reader_at_join()
    getattr(dec, control)()
    if boundary == "exited":
        assert ticket.complete.wait(2)
        [(image, command, freq)] = heard
        assert (command, freq, image["mode"]) == (
            _line(child), sstv.DEFAULT_SSTV_HZ, "Robot 36")
    else:
        child.feed("Detected SSTV mode PD 120")
        child.close_pipe()
        join(2)
        assert heard == []
        assert dec.images() == []
        assert not staged.with_suffix(".json").exists()
        assert capture.mode == ("Robot 36" if boundary == "parsed" else None)


@pytest.mark.parametrize("app_hook", [False, True], ids=["unit", "console"])
@pytest.mark.parametrize("slow", [False, True], ids=["drained", "timed-out"])
def test_sstv_clean_cycle_keeps_each_captures_command_freq_and_png(
        late_sstv, tmp_path, app_hook, slow):
    dec, children, paths, heard, clock, reader_at_join, _ = late_sstv

    def cycle(start, finish, media=None):
        start()
        child = children[0]
        paths[0].write_bytes(PNG_BYTES)
        action = None if slow else lambda: _sstv_end(child, mode="Robot 36")
        old_join = reader_at_join(action)
        child.die(0)
        clock.now += 1
        dec.freq_hz = 145_800_000
        start()
        assert len(children) == 2
        assert paths[0].name != paths[1].name
        assert child.terminated
        if slow:
            _sstv_end(child, mode="Robot 36")
            old_join(2)
        paths[1].write_bytes(PNG_BYTES)
        _sstv_end(children[1], mode="PD 120")
        dec.stop()
        records = finish()
        assert len(records) == 2
        for i, expected_freq in enumerate((sstv.DEFAULT_SSTV_HZ, 145_800_000)):
            image, command, freq = records[i]
            assert command == _line(children[i])
            assert freq == expected_freq
            assert image["path"] == f"sstv/{paths[i].name}"
            assert image in dec.images()
            mode = (None if slow else "Robot 36") if i == 0 else "PD 120"
            assert image["mode"] == mode
            if mode is None:
                assert image["meta"] == {}
            else:
                assert image["meta"]["freq_hz"] == expected_freq
            if media is not None:
                assert media(image["path"]) == 200

    if not app_hook:
        cycle(dec.start, lambda: heard)
        return

    app = create_app(pack_dir=FIXTURE_PACKS, data_dir=tmp_path, sstv_factory=lambda: dec,
                     probe=lambda: RadioProbe(False, "test", "no real RTL-SDR"))
    with TestClient(app) as client:
        def start():
            assert client.get("/api/sstv").status_code == 200

        def finish():
            return [(p["item"], p["command"], p["freq_hz"])
                    for event in read_events(tmp_path / "events.jsonl")
                    if event["kind"] == "reception" for p in [event["payload"]]]

        cycle(start, finish, lambda path: client.get(f"/media/{path}").status_code)


def _sstv_retire(dec, child, clock, boundary):
    if boundary in ("release", "stop"):
        getattr(dec, boundary)()
        dec.resume()
        dec.start()
    elif boundary == "clean-finish":
        child.die(0)
        dec.start()
    else:
        child.die(1)
        with pytest.raises(sstv.SstvUnavailable, match="status 1"):
            dec.start()
        if boundary != "back-off":
            clock.now += 60
            dec.start()


@pytest.mark.parametrize("boundary", ["release", "stop", "death", "clean-finish", "back-off"])
@pytest.mark.parametrize("late", ["clean-eof", "pipe-error"])
def test_sstv_stale_reader_cannot_publish_or_refuse_a_future_start(late_sstv, boundary, late):
    dec, children, paths, heard, clock, reader_at_join, _ = late_sstv
    dec.start()
    child = children[0]
    paths[0].write_bytes(PNG_BYTES)
    join = reader_at_join()
    _sstv_retire(dec, child, clock, boundary)
    baseline = list(heard)
    gallery = dec.images()
    if late == "clean-eof":
        child.feed("Detected SSTV mode Robot 36")
        child.close_pipe()
    else:
        child.break_pipe(OSError("old pipe failed"))
    join(2)
    assert not paths[0].with_suffix(".json").exists()
    assert heard == baseline
    assert dec.images() == gallery
    if len(paths) == 2:
        assert not paths[1].with_suffix(".json").exists()
        children[1].close_pipe()
        dec.stop()
    clock.now += 60
    assert dec.error is None
    dec.start()
    assert dec.running


@pytest.mark.parametrize("boundary", ["stop", "clean-finish"])
def test_sstv_in_flight_metadata_commit_crossing_retirement_is_dropped(
        late_sstv, monkeypatch, boundary):
    dec, children, paths, heard, clock, reader_at_join, gate = late_sstv
    inside, resume = gate(), gate()
    reader = None

    def clock_at_commit():
        if threading.current_thread() is reader:
            inside.set()
            assert resume.wait(5)
        return clock()

    monkeypatch.setattr(dec, "_clock", clock_at_commit)
    dec.start()
    reader = dec._thread
    join = reader_at_join()
    capture = getattr(dec, "_capture", None)
    child = children[0]
    paths[0].write_bytes(PNG_BYTES)
    child.feed("Detected SSTV mode Martin 1")
    child.close_pipe()
    assert inside.wait(2)
    _sstv_retire(dec, child, clock, boundary)
    baseline = list(heard)
    resume.set()
    join(2)
    assert not paths[0].with_suffix(".json").exists()
    assert heard == baseline
    assert (capture.mode, capture.decoded_at) == (None, None)
    assert dec.error is None


def test_sstv_stale_reader_never_reads_next_captures_path(late_sstv, monkeypatch):
    dec, children, paths, heard, clock, reader_at_join, gate = late_sstv
    entered, resume = gate(), gate()
    read = dec._read

    def read_after_gate(proc, *args):
        if proc is children[0]:
            entered.set()
            assert resume.wait(5)
        read(proc, *args)

    monkeypatch.setattr(dec, "_read", read_after_gate)
    dec.start()
    old = children[0]
    assert entered.wait(2)
    join = reader_at_join()
    dec.stop()
    clock.now += 1
    dec.start()
    paths[1].write_bytes(PNG_BYTES)
    resume.set()
    old.feed("Detected SSTV mode Martin 1")
    old.close_pipe()
    join(2)
    assert not paths[1].with_suffix(".json").exists()
    assert heard == []


def test_sstv_late_death_reason_cannot_corrupt_error_or_refuse_next_start(late_sstv, monkeypatch):
    dec, children, _, _, clock, reader_at_join, gate = late_sstv
    diagnosed, resume = gate(), gate()
    enrich = dec.watch.enrich

    def pause_reason(proc, reason):
        if proc is children[0]:
            diagnosed.set()
            assert resume.wait(5)
        enrich(proc, reason)

    monkeypatch.setattr(dec.watch, "enrich", pause_reason)
    dec.start()
    ticket = dec._run
    child = children[0]
    join = reader_at_join()
    _sstv_end(child, code=1)
    assert diagnosed.wait(2)
    with pytest.raises(sstv.SstvUnavailable, match="status 1"):
        dec.start()
    clock.now += 60
    dec.start()
    resume.set()
    assert ticket.diagnosed.wait(2)
    join(2)
    assert dec.error is None
    assert not dec.watch.stopped
    children[1].close_pipe()
    dec.stop()
    dec.start()
    assert dec.running


def test_sstv_exit_diagnostics_do_not_hold_the_run_guard(late_sstv, monkeypatch):
    dec, children, _, _, clock, reader_at_join, gate = late_sstv
    inside, resume = gate(), gate()
    dec.start()
    ticket = dec._run
    child = children[0]
    join = reader_at_join()
    tail = childproc.stderr_tail

    def gated_tail(proc):
        if proc is child:
            inside.set()
            assert resume.wait(5)
        return tail(proc)

    monkeypatch.setattr(childproc, "stderr_tail", gated_tail)
    _sstv_end(child, code=1)
    assert inside.wait(2)
    guard = dec._runs
    assert guard.lock.acquire(blocking=False)
    guard.lock.release()
    with pytest.raises(sstv.SstvUnavailable, match="status 1"):
        dec.start()
    clock.now += 60
    dec.stop()
    dec.start()
    resume.set()
    assert ticket.diagnosed.wait(2)
    join(2)
    assert dec.running
    assert not dec.watch.stopped
    assert dec.error is None


def test_sstv_concurrent_start_and_settle_never_tear_down_live_capture(late_sstv, monkeypatch):
    dec, children, _, _, _, _, gate = late_sstv
    spawning, resume, requesting = gate(), gate(), gate()
    spawn = dec._spawn

    def gated_spawn(*args):
        spawning.set()
        assert resume.wait(5)
        return spawn(*args)

    def request_start():
        requesting.set()
        dec.start()

    monkeypatch.setattr(dec, "_spawn", gated_spawn)
    a = threading.Thread(target=dec.start)
    b = threading.Thread(target=request_start)
    a.start()
    try:
        assert spawning.wait(2)
        b.start()
        assert requesting.wait(2)
    finally:
        resume.set()
        a.join(2)
        if b.ident is not None:
            b.join(2)
    assert not a.is_alive() and not b.is_alive()
    with dec._life:
        settle = getattr(dec, "_settle_locked", None)
        if settle is not None:
            settle()
    assert len(children) == 1
    assert not children[0].terminated
    assert dec.running
