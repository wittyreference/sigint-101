# ABOUTME: Tests the rtl_433 decode backend — JSON-line parsing and the Decoder lifecycle.
# ABOUTME: The process is a fake at the boundary; no radio or no rtl_433 is an honest refusal.

import io
import queue
import shlex
import threading
from collections import deque
from pathlib import Path

import pytest

from sigint101.childproc import ChildWatch
from sigint101.decode import (
    DEFAULT_DECODE_HZ,
    Decoder,
    DecodeUnavailable,
    RepeatFilter,
    parse_decode_line,
)

# Real rtl_433 -F json output shapes. The ASK line is one captured live on nomad-b from an
# Acurite-Tower weather sensor; the FSK line is rtl_433's documented FSK shape, which reports
# the two mark/space tones as freq1 and freq2 (the whole point of M10) rather than one freq.
ASK_LINE = ('{"time" : "2026-10-27 13:04:11", "model" : "Acurite-Tower", "id" : 12345, '
            '"channel" : "A", "battery_ok" : 1, "temperature_C" : 18.000, "humidity" : 51, '
            '"mic" : "CHECKSUM", "mod" : "ASK", "freq" : 433.950, "rssi" : -1.112, '
            '"snr" : 36.261, "noise" : -37.373}')
FSK_LINE = ('{"time" : "2026-10-27 13:10:02", "model" : "LaCrosse-TX141THBv2", "id" : 252, '
            '"channel" : 0, "battery_ok" : 1, "temperature_C" : 21.3, "humidity" : 45, '
            '"mod" : "FSK", "freq1" : 433.882, "freq2" : 433.958, "rssi" : -8.1, '
            '"snr" : 22.4, "noise" : -30.5}')


def test_parse_reads_an_ask_sensor_line():
    d = parse_decode_line(ASK_LINE)
    assert d is not None
    assert d.model == "Acurite-Tower"
    assert d.mod == "ASK"
    assert d.freq_hz == 433_950_000
    assert d.freq2_hz is None            # ASK is one frequency switched on and off
    assert d.snr_db == pytest.approx(36.261)
    assert d.readings["temperature_C"] == pytest.approx(18.0)
    assert d.readings["humidity"] == 51
    # the envelope keys are not sensor readings
    assert "freq" not in d.readings and "snr" not in d.readings and "model" not in d.readings


def test_parse_reads_an_fsk_sensor_with_both_tones():
    d = parse_decode_line(FSK_LINE)
    assert d is not None
    assert d.model == "LaCrosse-TX141THBv2"
    assert d.mod == "FSK"
    # FSK rides two frequencies; both are carried so the lesson can name the shift.
    assert d.freq_hz == 433_882_000
    assert d.freq2_hz == 433_958_000
    assert d.readings["temperature_C"] == pytest.approx(21.3)


def test_parse_skips_a_non_json_narration_line():
    assert parse_decode_line("Tuned to 433.920MHz.") is None
    assert parse_decode_line("") is None
    assert parse_decode_line("   ") is None


def test_parse_skips_json_without_a_model():
    # rtl_433 can emit non-decode JSON (e.g. a stats frame); it is not a sensor read.
    assert parse_decode_line('{"time" : "2026-10-27 13:00:00", "count" : 3}') is None


def test_as_dict_round_trips_the_fields_the_api_sends():
    d = parse_decode_line(FSK_LINE)
    out = d.as_dict()
    assert out["model"] == "LaCrosse-TX141THBv2"
    assert out["mod"] == "FSK"
    assert out["freq_hz"] == 433_882_000
    assert out["freq2_hz"] == 433_958_000
    assert out["readings"]["humidity"] == 45


class FakeProc:
    """Stands in for a Popen'd rtl_433 at the process boundary."""

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _decoder(lines, **kw):
    kw.setdefault("radio_present", lambda: True)
    kw.setdefault("which", lambda name: "/usr/bin/" + name)
    return Decoder(spawn=lambda cmd: FakeProc(lines), **kw)


def test_decoder_buffers_parsed_reads():
    dec = _decoder([ASK_LINE, "Tuned to 433.920MHz.", FSK_LINE])
    dec.start()
    dec.drain()
    reads = dec.recent()
    assert [r.model for r in reads] == ["Acurite-Tower", "LaCrosse-TX141THBv2"]


def test_decoder_buffer_is_bounded_to_newest():
    lines = [ASK_LINE.replace("12345", str(1000 + n)) for n in range(30)]
    dec = _decoder(lines, history=5)
    dec.start()
    dec.drain()
    reads = dec.recent()
    assert len(reads) == 5
    assert reads[-1].readings["id"] == 1029


def test_decoder_refuses_without_a_radio():
    dec = _decoder([], radio_present=lambda: False)
    with pytest.raises(DecodeUnavailable):
        dec.start()


def test_decoder_refuses_when_rtl_433_is_not_installed():
    dec = _decoder([], which=lambda name: None)
    with pytest.raises(DecodeUnavailable):
        dec.start()


def test_decoder_refuses_to_start_once_released():
    dec = _decoder([ASK_LINE])
    dec.release()
    with pytest.raises(DecodeUnavailable):
        dec.start()
    dec.resume()
    dec.start()          # taking the radio back works
    dec.drain()
    assert dec.recent()


def test_command_targets_the_decode_frequency_and_json():
    dec = Decoder(freq_hz=433_920_000, device=0)
    cmd = dec.command()
    assert "rtl_433" in cmd[0]
    assert "433920000" in cmd
    assert "json" in cmd


def test_default_decode_frequency_is_the_ism_band():
    assert DEFAULT_DECODE_HZ == 433_920_000


def test_recent_keeps_only_the_newest_when_limited():
    dec = _decoder([ASK_LINE.replace("12345", str(1000 + n)) for n in range(6)])
    dec.start()
    dec.drain()
    assert [r.readings["id"] for r in dec.recent(2)] == [1004, 1005]


def test_stop_reaps_the_process_and_releases_the_dongle():
    proc_box = {}

    def spawn(cmd):
        proc_box["proc"] = FakeProc([ASK_LINE])
        return proc_box["proc"]

    dec = Decoder(spawn=spawn, radio_present=lambda: True,
                  which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    dec.stop()
    assert proc_box["proc"].terminated is True
    assert dec.running is False


def test_a_reader_error_is_recorded_loudly_not_swallowed():
    class RaisingProc(FakeProc):
        def __init__(self):
            def boom():
                raise OSError("pipe died")
                yield  # pragma: no cover - makes this a generator
            self.stdout = boom()
            self.terminated = False

    dec = Decoder(spawn=lambda cmd: RaisingProc(), radio_present=lambda: True,
                  which=lambda name: "/usr/bin/" + name)
    dec.start()
    dec.drain()
    assert dec.error == "pipe died"


def test_a_device_it_cannot_single_out_is_a_decode_refusal():
    # Two radios on the bus and none pinned: the decoder refuses rather than opening an arbitrary
    # tuner, exactly as the sweeper does — a wrong-radio decode would look perfectly real.
    from sigint101.hardware import RtlSdrDevice
    dec = Decoder(spawn=lambda cmd: FakeProc([]), radio_present=lambda: True,
                  which=lambda name: "/usr/bin/" + name,
                  list_devices=lambda: (RtlSdrDevice(0, "00000001"), RtlSdrDevice(1, "1004")))
    with pytest.raises(DecodeUnavailable):
        dec.start()


def test_parse_tolerates_a_malformed_frequency_without_inventing_one():
    # A line whose freq is not a number still parses (it is a real read), but freq_hz is 0 rather
    # than a guessed frequency, and a non-numeric snr becomes None.
    d = parse_decode_line(
        '{"time":"t","model":"X","mod":"ASK","freq":"nope","snr":"bad","humidity":5}')
    assert d is not None
    assert d.freq_hz == 0 and d.snr_db is None
    assert d.readings["humidity"] == 5


# --- one reception per transmission, however many copies rtl_433 prints -------------------------

# A real capture: 75 s of `rtl_433 -f 433920000 -F json -M level -d 0` on nomad-b (2026-11-02),
# each line prefixed with its arrival time (epoch s). The Acurite-Tower sends every reading three
# times in one burst and rtl_433 prints all three, byte-identical, within 6 ms; the next burst
# comes ~17 s later carrying the same 16.0 °C / 42 % — a new reading that happens to match.
REPEATS = Path(__file__).parent / "fixtures" / "rtl_433" / "acurite-tower-repeats.txt"


def _captured():
    """The capture as (arrival time, rtl_433 line) pairs, in arrival order."""
    pairs = []
    for row in REPEATS.read_text().splitlines():
        stamp, line = row.split(" ", 1)
        pairs.append((float(stamp), line))
    return pairs


def test_the_capture_is_four_bursts_of_three_identical_copies():
    lines = [line for _, line in _captured()]
    assert len(lines) == 12
    assert len(set(lines[0:3])) == len(set(lines[3:6])) == 1
    assert lines[0] != lines[3]                 # the burst's envelope differs; the reading does not


def test_copies_of_one_transmission_are_repeats_and_the_next_burst_is_not():
    repeats = RepeatFilter()
    verdicts = [repeats.is_repeat(parse_decode_line(line), now) for now, line in _captured()]
    # One new reception per burst, even though every burst reports the same values.
    assert verdicts == [False, True, True] * 4


def test_the_same_values_after_the_window_are_a_new_reading():
    repeats = RepeatFilter(window_s=2.0)
    first = parse_decode_line(_captured()[0][1])
    assert repeats.is_repeat(first, 100.0) is False
    assert repeats.is_repeat(first, 101.9) is True
    assert repeats.is_repeat(first, 102.0) is False  # measured from the copy that was reported


def test_a_changed_reading_or_another_sensor_inside_the_window_is_new():
    repeats = RepeatFilter()
    tower_now = parse_decode_line(_captured()[0][1])   # id 12345, 16.0 °C, 42 %
    tower_then = parse_decode_line(ASK_LINE)           # same sensor, 18.0 °C, 51 %
    lacrosse = parse_decode_line(FSK_LINE)
    assert repeats.is_repeat(tower_now, 10.0) is False
    assert repeats.is_repeat(tower_then, 10.1) is False
    assert repeats.is_repeat(lacrosse, 10.2) is False


def test_the_decoder_reports_one_reception_per_burst_but_lists_every_copy():
    burst = [line for _, line in _captured()[0:3]]
    dec = _decoder(burst + [FSK_LINE])
    heard = []
    dec.on_reception = heard.append
    dec.start()
    dec.drain()
    assert [h["model"] for h in heard] == ["Acurite-Tower", "LaCrosse-TX141THBv2"]
    assert heard[0]["readings"]["temperature_C"] == pytest.approx(16.0)
    # The panel still shows what rtl_433 printed: three copies of the tower, then the LaCrosse.
    assert len(dec.recent()) == 4


# --- C28: a reader that outlives its stop hears nothing into the next run ----------------------
# WHY: a reader can outlive stop()'s wait for it: an rtl_433 that is not reaped keeps its pipe
# open, and a reader slowed on a loaded Pi can still be reading after its process has gone. It went
# on listing and reporting what it read. After a retune the console then logged a reception under
# the new frequency and the new rtl_433's command, the panel listed it among the new frequency's
# reads, and it could mark the new run's next copy of that reading as a repeat. Its pipe failing,
# or its death, could set the decoder's error while the next rtl_433 ran, and the start after that
# one's stop refused with the old reason. A reader now lists, reports and records nothing once its
# run is stopped.

class LateProc:
    """rtl_433 whose pipe outlives it, so its reader is still reading after the process ended.

    It prints only what a test feeds it. terminate() and die() end the process but not the pipe;
    only close_pipe() or break_pipe() ends what the reader reads. `reader` is the thread reading.
    """

    def __init__(self, cmd):
        self.args = list(cmd)
        self.returncode = None
        self.terminated = False
        self.reader = None
        self.stderr = io.StringIO("")
        self._lines = queue.Queue()
        self.stdout = self._stream()

    def _stream(self):
        self.reader = threading.current_thread()
        for line in iter(self._lines.get, None):
            if isinstance(line, OSError):
                raise line
            yield line

    def feed(self, *lines):
        for line in lines:
            self._lines.put(line)

    def close_pipe(self):
        self._lines.put(None)

    def break_pipe(self, error):
        self._lines.put(error)

    def die(self, code):
        self.returncode = code

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


class _Heard:
    """The console's reception hook in small: each reception is logged with the frequency and the
    command the decoder reports at that moment, read as the console's own hook reads them."""

    def __init__(self, dec):
        self.dec = dec
        self.log = []
        self._changed = threading.Condition()

    def __call__(self, item):
        with self._changed:
            self.log.append((item["model"], self.dec.freq_hz, self.dec.live_command()))
            self._changed.notify_all()

    def wait_for(self, count, timeout=5.0):
        with self._changed:
            assert self._changed.wait_for(lambda: len(self.log) >= count, timeout), self.log


class _Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


def _line(proc):
    """The command a process runs, as the decoder reports it while that process is live."""
    return shlex.join(proc.args)


@pytest.fixture
def late():
    """A decoder whose every rtl_433 has a pipe that outlives it, and whose stop() stops waiting
    for the reader at once; its receptions are logged as the console logs them."""
    procs = []

    def spawn(cmd):
        procs.append(LateProc(cmd))
        return procs[-1]

    dec = Decoder(spawn=spawn, radio_present=lambda: True, which=lambda name: "/usr/bin/" + name)
    dec.STOP_WAIT_S = 0.05
    heard = _Heard(dec)
    dec.on_reception = heard
    yield dec, procs, heard
    for proc in procs:
        proc.close_pipe()
    for proc in procs:
        if proc.reader is not None:
            proc.reader.join(5)


def _end_pipe(proc):
    """Let a reader come to the end of its pipe, and wait until it has."""
    proc.close_pipe()
    proc.reader.join(5)
    assert not proc.reader.is_alive()


def test_a_reader_that_outlives_a_retune_logs_nothing_under_the_new_frequency(late):
    dec, procs, heard = late
    dec.start()
    procs[0].feed(ASK_LINE)
    heard.wait_for(1)
    dec.retune(315_000_000)
    assert procs[0].reader.is_alive()             # the old reader outlived the stop
    dec.start()
    procs[0].feed(FSK_LINE)                       # read from the old pipe after the restart
    _end_pipe(procs[0])
    assert heard.log == [("Acurite-Tower", 433_920_000, _line(procs[0]))]
    assert dec.recent() == []
    # The new run hears its own reception, and the old reader did not make it a repeat.
    procs[1].feed(FSK_LINE)
    heard.wait_for(2)
    assert heard.log[1:] == [("LaCrosse-TX141THBv2", 315_000_000, _line(procs[1]))]
    assert "315000000" in _line(procs[1])
    assert [r.model for r in dec.recent()] == ["LaCrosse-TX141THBv2"]


@pytest.mark.parametrize("stand_down, kept", [
    (lambda dec: dec.retune(315_000_000), []),
    (lambda dec: dec.release(), []),
    (lambda dec: dec.stop(), ["Acurite-Tower"]),
], ids=["retune", "release", "stop"])
def test_a_reader_that_outlives_its_stop_hears_nothing_before_the_next_start(
        late, stand_down, kept):
    # Between a stop and the next start no run is current. After a retune that lasts until the
    # next poll; after a release, as long as the other program holds the radio. The old reader
    # reads on in that gap; nothing it reads may be listed or logged, then or after the start.
    dec, procs, heard = late
    dec.start()
    procs[0].feed(ASK_LINE)
    heard.wait_for(1)
    stand_down(dec)
    assert procs[0].reader.is_alive()
    procs[0].feed(FSK_LINE)
    _end_pipe(procs[0])
    assert heard.log == [("Acurite-Tower", 433_920_000, _line(procs[0]))]
    assert [r.model for r in dec.recent()] == kept
    dec.resume()
    dec.start()
    procs[1].feed(FSK_LINE)
    heard.wait_for(2)
    assert heard.log[1:] == [("LaCrosse-TX141THBv2", dec.freq_hz, _line(procs[1]))]
    assert [r.model for r in dec.recent()] == kept + ["LaCrosse-TX141THBv2"]


def test_an_old_readers_pipe_failing_after_a_retune_is_not_the_new_runs_failure(late):
    dec, procs, heard = late
    dec.start()
    procs[0].feed(ASK_LINE)
    heard.wait_for(1)
    dec.retune(315_000_000)
    assert procs[0].reader.is_alive()
    dec.start()
    procs[0].break_pipe(OSError("old pipe failed"))
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None
    assert dec.running
    dec.stop()
    dec.start()                                   # not refused with the old run's reason
    assert len(procs) == 3
    assert dec.running


def test_an_old_readers_death_record_landing_after_the_next_start_is_not_the_new_runs(late):
    # The old reader finds its rtl_433 dead, and is held there before it records why. Meanwhile
    # the next poll reports the death itself, and once the back-off has run out a new run starts.
    # Only then does the old reader's record land: it must not become the new run's failure.
    dec, procs, heard = late
    clock = _Clock()
    dec.watch = ChildWatch(dec.watch.tool, clock=clock)
    found_ended = dec.watch.ended
    found, go_on = threading.Event(), threading.Event()

    def ended(proc, **kw):
        reason = found_ended(proc, **kw)
        if reason is not None and proc is procs[0]:
            found.set()
            go_on.wait(5)                         # held between finding the death and recording it
        return reason

    dec.watch.ended = ended
    dec.start()
    procs[0].feed(ASK_LINE)
    heard.wait_for(1)
    procs[0].die(1)
    procs[0].close_pipe()
    assert found.wait(5)
    with pytest.raises(DecodeUnavailable, match="status 1"):
        dec.start()                               # the next poll reports the death, then backs off
    clock.now += 60
    dec.start()
    assert len(procs) == 2
    go_on.set()
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None
    dec.stop()
    dec.start()                                   # not refused with the old run's reason
    assert len(procs) == 3


class _HeldLock:
    """The decoder's buffer lock, able to hold one thread just before that thread takes it.

    It forces the order a test needs across the lock: the old reader is held on its way into the
    lock while the test retunes and starts the next run, then let in. The reader's run test must be
    made inside the lock, or what it tested is out of date by the time it writes.
    """

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


@pytest.fixture
def held(late):
    """The `late` decoder with a buffer lock that can hold its old reader at the door, and a child
    watch on a clock the test runs, so a death's back-off can run out at once."""
    dec, procs, heard = late
    dec._lock = _HeldLock()
    dec.watch = ChildWatch(dec.watch.tool, clock=_Clock())
    return dec, procs, heard


def _restart_while_held(dec):
    """With the old reader held at the buffer lock: retune, let any back-off run out, start the
    next run, and only then let the old reader in."""
    assert dec._lock.arrived.wait(5)
    dec.retune(315_000_000)
    dec.watch.clock.now += 60
    dec.start()
    dec._lock.go_on.set()


def test_a_read_on_its_way_in_at_a_restart_is_not_listed_or_logged(held):
    dec, procs, heard = held
    dec.start()
    procs[0].feed(ASK_LINE)
    heard.wait_for(1)
    dec._lock.hold(procs[0].reader)
    procs[0].feed(FSK_LINE)                       # read, and held on its way to be listed
    _restart_while_held(dec)
    _end_pipe(procs[0])
    assert dec.recent() == []
    assert heard.log == [("Acurite-Tower", 433_920_000, _line(procs[0]))]
    assert len(procs) == 2


def test_a_pipe_failure_on_its_way_in_at_a_restart_is_not_the_new_runs(held):
    dec, procs, heard = held
    dec.start()
    procs[0].feed(ASK_LINE)
    heard.wait_for(1)
    dec._lock.hold(procs[0].reader)
    procs[0].break_pipe(OSError("old pipe failed"))   # held on its way to be recorded
    _restart_while_held(dec)
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None
    dec.stop()
    dec.start()                                   # not refused with the old run's reason
    assert len(procs) == 3


def test_a_death_on_its_way_in_at_a_restart_is_not_the_new_runs(held):
    dec, procs, heard = held
    dec.start()
    procs[0].feed(ASK_LINE)
    heard.wait_for(1)
    dec._lock.hold(procs[0].reader)
    procs[0].die(1)                               # found, and held on its way to be recorded
    procs[0].close_pipe()
    _restart_while_held(dec)
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert dec.error is None
    dec.stop()
    dec.start()                                   # not refused with the old run's reason
    assert len(procs) == 3


class _HeldAppend(deque):
    """The decoder's list of reads; one append, once armed, waits inside the buffer lock."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.armed = False
        self.inside = threading.Event()
        self.go_on = threading.Event()

    def append(self, item):
        if self.armed:
            self.armed = False
            self.inside.set()
            self.go_on.wait(5)
        super().append(item)


class _TellingLock(_HeldLock):
    """The buffer lock, telling when one given thread first tries to take it."""

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
    """The reader's thread as stop() sees it: while the reader is held at its append, the stop's
    wait gives up at once, as it does for a reader that outlives it; otherwise it waits for the
    reader to end."""

    def __init__(self, thread, reads):
        self.thread, self.reads = thread, reads

    def join(self, timeout=None):
        if self.reads.inside.is_set() and not self.reads.go_on.is_set():
            return
        self.thread.join(5)


def test_the_stop_moves_the_run_on_inside_the_buffer_lock(late):
    # The old reader has passed its run test and is held inside the buffer lock, at the append,
    # while a retune runs in another thread. The stop must move the run on under that lock, so it
    # waits for the held reader: the read is then the old run's, listed and cleared by the retune,
    # and logged on the old frequency. A stop that moved the run on without the lock would go on
    # past the held reader, retune, and only then let it in, to be logged at 315 MHz.
    dec, procs, heard = late
    dec._lock = _TellingLock()
    dec._reads = _HeldAppend(maxlen=dec._reads.maxlen)
    dec.start()
    dec._thread = _OutlivedReader(dec._thread, dec._reads)
    procs[0].feed(ASK_LINE)
    heard.wait_for(1)

    def hook(item):                               # the console's hook, then the old pipe ends
        heard(item)
        procs[0].close_pipe()

    dec.on_reception = hook
    dec._reads.armed = True
    procs[0].feed(FSK_LINE)
    assert dec._reads.inside.wait(5)              # past its run test, holding the lock
    retune = threading.Thread(target=dec.retune, args=(315_000_000,))
    dec._lock.watch_for = retune
    retune.start()
    assert dec._lock.tried.wait(5)                # the retune now waits on the lock
    dec._reads.go_on.set()
    retune.join(10)
    assert not retune.is_alive()
    procs[0].reader.join(5)
    assert not procs[0].reader.is_alive()
    assert [(model, freq) for model, freq, _ in heard.log] == [
        ("Acurite-Tower", 433_920_000), ("LaCrosse-TX141THBv2", 433_920_000)]
    assert dec.recent() == []


@pytest.mark.parametrize("stand_down", ["retune", "stop"])
@pytest.mark.parametrize("during", ["stopping", "terminate"])
def test_what_an_old_reader_reads_during_its_stop_is_not_kept(late, during, stand_down):
    # The run must end as the stop begins, not somewhere inside it. Here the old reader reads one
    # more decode and then its pipe fails while the stop is under way: as the child watch is told
    # the stop is ours, or as rtl_433 is asked to terminate. The stop goes on only once that reader
    # has ended. Nothing it read may be listed or logged, its failure may not stand, and the next
    # start may not be refused with it.
    dec, procs, heard = late
    dec.start()
    procs[0].feed(ASK_LINE)
    heard.wait_for(1)
    old = procs[0]
    owner = dec.watch if during == "stopping" else old
    plain = getattr(owner, during)

    def mid_stop(*args, **kw):
        result = plain(*args, **kw)
        setattr(owner, during, plain)             # once, for this stop only
        old.feed(FSK_LINE)
        old.break_pipe(OSError("old pipe failed during the stop"))
        old.reader.join(5)
        return result

    setattr(owner, during, mid_stop)
    if stand_down == "retune":
        dec.retune(315_000_000)
    else:
        dec.stop()
    assert not old.reader.is_alive()
    assert heard.log == [("Acurite-Tower", 433_920_000, _line(old))]
    assert "LaCrosse-TX141THBv2" not in [r.model for r in dec.recent()]
    assert dec.error is None
    dec.start()                                   # not refused with the old run's reason
    assert len(procs) == 2
    assert dec.running


@pytest.mark.parametrize("end, reason", [
    (lambda proc: proc.break_pipe(OSError("new pipe failed")), "new pipe failed"),
    (lambda proc: (proc.die(1), proc.close_pipe()), "status 1"),
], ids=["pipe", "death"])
def test_the_run_after_a_retune_still_records_its_own_failure(late, end, reason):
    # The other side of the guard: with an old reader still reading, the run started after the
    # retune hears its own receptions and records its own failure, and the next start reports it.
    dec, procs, heard = late
    dec.start()
    procs[0].feed(ASK_LINE)
    heard.wait_for(1)
    dec.retune(315_000_000)
    assert procs[0].reader.is_alive()
    dec.start()
    procs[1].feed(FSK_LINE)
    heard.wait_for(2)
    assert heard.log[1] == ("LaCrosse-TX141THBv2", 315_000_000, _line(procs[1]))
    end(procs[1])
    procs[1].reader.join(5)
    assert not procs[1].reader.is_alive()
    assert reason in dec.error
    dec.stop()
    with pytest.raises(DecodeUnavailable, match=reason):
        dec.start()
