# ABOUTME: Tests the five retuning text decoders against stale readers that outlive a stop (C31).
# ABOUTME: Covers retune, release, stop and died paths, lock ordering, and DMR's per-reader tail.

from __future__ import annotations

import shlex
import threading
from dataclasses import dataclass
from typing import Callable

import pytest

from sigint101 import acars, aprs, dmr, pocsag, rds
from sigint101.childproc import ChildWatch
from tests.child_fakes import LateProc

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


@dataclass(frozen=True)
class DecoderSpec:
    name: str
    build: Callable
    unavailable: type
    tool: str
    home_hz: int
    other_hz: int
    line1: str
    key1: object
    line2: str
    key2: object
    item_key: Callable
    listed: Callable
    hold_target: Callable   # dec -> (owner_obj, method_name) called inside the buffer lock


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
    ),
)

SPEC_BY_NAME = {spec.name: spec for spec in SPECS}


class _Heard:
    """The console's reception hook in small: logs (key, freq_hz, live_command) as each arrives."""

    def __init__(self, dec, item_key):
        self.dec = dec
        self.item_key = item_key
        self.log = []
        self._changed = threading.Condition()

    def __call__(self, item):
        with self._changed:
            self.log.append((self.item_key(item), self.dec.freq_hz, self.dec.live_command()))
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

        def spawn(freq_hz, device):
            proc = LateProc([spec.tool, "-f", str(freq_hz), "-d", str(device)])
            procs.append(proc)
            all_procs.append(proc)
            return proc

        dec = spec.build(spawn)
        dec.STOP_WAIT_S = 0.05
        clock = _Clock()
        dec.watch = ChildWatch(dec.watch.tool, clock=clock)
        heard = _Heard(dec, spec.item_key)
        dec.on_reception = heard
        return dec, procs, heard, clock

    yield make
    for proc in all_procs:
        proc.close_pipe()
    for proc in all_procs:
        if proc.reader is not None:
            proc.reader.join(5)


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
@pytest.mark.parametrize("restart", ["retune", "release", "stop", "died"])
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
    assert str(new_freq) in _line(procs[1])
    assert spec.key2 in spec.listed(dec)


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
@pytest.mark.parametrize("stand_down", ["retune", "release", "stop"])
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

    dec.resume()
    dec.start()
    procs[1].feed(spec.line2)
    heard.wait_for(2)
    assert heard.log[1:] == [(spec.key2, dec.freq_hz, _line(procs[1]))]
    assert spec.key2 in spec.listed(dec)


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_old_readers_pipe_failing_after_retune_is_not_the_new_runs_failure(late_factory, spec):
    dec, procs, heard, _ = late_factory(spec)
    dec.start()
    procs[0].feed(spec.line1)
    heard.wait_for(1)
    dec.retune(spec.other_hz)
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
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None
    dec.stop()
    dec.start()                                    # not refused with the old run's reason
    assert len(procs) == 3


def _restart_while_held(dec, lock, clock, other_hz):
    assert lock.arrived.wait(5)
    dec.retune(other_hz)
    clock.now += 60
    dec.start()
    lock.go_on.set()


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
    _restart_while_held(dec, lock, clock, spec.other_hz)
    _end_pipe(procs[0])
    assert spec.listed(dec) == []
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
    _restart_while_held(dec, lock, clock, spec.other_hz)
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
    procs[0].die(1)
    procs[0].close_pipe()
    _restart_while_held(dec, lock, clock, spec.other_hz)
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None
    dec.stop()
    dec.start()
    assert len(procs) == 3


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_stop_moves_the_run_on_inside_the_buffer_lock(late_factory, spec):
    # Hold the old reader inside the buffer lock (past its run check) while a retune runs in
    # another thread. Because stop() ends the run under that same lock, retune waits for the
    # in-flight update: the reception logs at home_hz and is cleared by the retune.
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

    def hook(item):
        heard(item)
        procs[0].close_pipe()

    dec.on_reception = hook
    armed[0] = True
    procs[0].feed(spec.line2)
    assert inside.wait(5)                          # past its run test, holding the buffer lock

    retune = threading.Thread(target=dec.retune, args=(spec.other_hz,))
    lock.watch_for = retune
    retune.start()
    assert lock.tried.wait(5)                      # retune waits on the buffer lock
    go_on.set()
    retune.join(10)
    assert not retune.is_alive()
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert [(key, freq) for key, freq, _ in heard.log] == [
        (spec.key1, spec.home_hz), (spec.key2, spec.home_hz)]
    assert spec.listed(dec) == []


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
@pytest.mark.parametrize("stand_down", ["retune", "stop"])
@pytest.mark.parametrize("during", ["stopping", "terminate"])
def test_what_an_old_reader_reads_during_its_stop_is_not_kept(
        late_factory, spec, during, stand_down):
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
        old.reader.join(5)
        return result

    setattr(owner, during, mid_stop)
    if stand_down == "retune":
        dec.retune(spec.other_hz)
    else:
        dec.stop()
    assert not old.reader.is_alive()
    assert heard.log == [(spec.key1, spec.home_hz, _line(old))]
    assert spec.key2 not in spec.listed(dec)
    assert dec.error is None
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
    dec.retune(spec.other_hz)
    assert procs[0].reader.is_alive()
    dec.start()
    procs[1].feed(spec.line2)
    heard.wait_for(2)
    assert heard.log[1] == (spec.key2, spec.other_hz, _line(procs[1]))
    end(procs[1])
    procs[1].reader.join(5)
    assert not procs[1].reader.is_alive()
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
