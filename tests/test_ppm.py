# ABOUTME: Tests the frequency-error instrument — the receiver's crystal against two references.
# ABOUTME: Every seam is injected: a synthesised pilot stands in for the radio, no dongle opened.

import io
import json
import math
import os
import random
import sys
import threading
import time
from array import array

import pytest

from sigint101 import childproc, ppm
from tests.child_fakes import Child
from tests.test_app import _client, _rows, _StubSweeper


def synth_stream(ppm_error, seconds=20.0, pilot_amp=4000.0, rate=ppm.DEMOD_RATE, noise=1500.0):
    """A demodulated FM baseband whose pilot carries a crystal error we chose.

    Not a mock of the instrument — it is a real signal with a known answer, which is the only
    way to find out whether the instrument reports the number that is actually there.
    """
    n = int(rate * seconds)
    tone = ppm.PILOT_HZ / (1.0 + ppm_error / 1e6)
    w, w1 = 2.0 * math.pi * tone / rate, 2.0 * math.pi * 1000.0 / rate
    buf = array("h", bytes(2 * n))
    # A deterministic stand-in for programme audio and noise: seeded randomness would still be
    # randomness, and a repeating waveform makes a failure reproducible by eye.
    for i in range(n):
        v = (pilot_amp * math.sin(w * i) + 6000.0 * math.sin(w1 * i)
             + noise * math.sin(2.0 * math.pi * 6113.0 * i / rate))
        buf[i] = max(-32768, min(32767, int(v)))
    return io.BytesIO(buf.tobytes())


def measure(ppm_error, **kw):
    """Run the whole pilot measurement against a synthesised capture."""
    seconds = kw.pop("seconds", 5.0)
    return ppm.measure_pilot_ppm(
        hz=106_500_000, seconds=seconds,
        capture=lambda **_: synth_stream(ppm_error, seconds=seconds, **kw))


# --- the measurement recovers the error that was actually there ---------

def test_a_synthesised_pilot_recovers_the_ppm_it_was_built_with():
    assert measure(137.0)["ppm"] == pytest.approx(137.0, abs=0.5)


def test_a_fast_crystal_puts_the_pilot_below_nineteen_kilohertz():
    # The single easiest thing in this module to get backwards. The crystal clocks the sample
    # rate, so a fast crystal stretches assumed time and the tone lands low; where it also clocks
    # the tuner, the dial reads lower than the frequency actually being received.
    result = measure(137.0)
    assert result["apparent_hz"] < ppm.PILOT_HZ
    assert measure(-46.0)["apparent_hz"] > ppm.PILOT_HZ


def test_a_negative_error_is_recovered_too():
    assert measure(-46.0)["ppm"] == pytest.approx(-46.0, abs=0.5)


def test_an_error_the_size_this_radio_actually_has_is_recovered():
    # The Nomad's board measured +164.53 ppm on 2026-09-05, far outside the 30-100 ppm a generic
    # board is usually assumed to carry. The search window has to reach it.
    assert measure(164.5)["ppm"] == pytest.approx(164.5, abs=0.5)


def test_a_weak_pilot_still_measures():
    assert measure(137.0, pilot_amp=400.0)["ppm"] == pytest.approx(137.0, abs=0.5)


# --- refusing, rather than reporting a number that means nothing --------

def test_noise_with_no_pilot_returns_no_ppm_and_says_why():
    # A mono station carries no pilot by design and a station too weak tonight reads identically.
    # Either way the honest answer is no measurement — never a number.
    result = ppm.measure_pilot_ppm(
        hz=106_500_000, seconds=3.0,
        capture=lambda **_: synth_stream(0.0, seconds=3.0, pilot_amp=0.0, noise=6000.0))
    assert result["ppm"] is None
    assert "pilot" in result["reason"]


def test_a_capture_that_ended_early_is_refused_not_extrapolated():
    result = ppm.measure_pilot_ppm(hz=106_500_000, seconds=20.0,
                                   capture=lambda **_: io.BytesIO(b""))
    assert result["ppm"] is None
    assert result["reason"]


def silent_stream(seconds):
    """rtl_fm running but delivering exact zeros: not even the noise a working receiver hears."""
    return io.BytesIO(bytes(2 * int(ppm.DEMOD_RATE * seconds)))


def test_a_silent_capture_is_refused_not_read_as_the_edge_of_the_search():
    # Zeros make every level a division by zero. The infinite ratio that left passed the pilot
    # threshold, so silence came back as +400.16 ppm, the edge of the search window, with no
    # reason; and the route could not even send it, because JSON has no infinity.
    result = ppm.measure_pilot_ppm(hz=106_500_000, seconds=2.0,
                                   capture=lambda **_: silent_stream(2.0))
    assert result["ppm"] is None
    assert "zero" in result["reason"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [math.inf, math.nan], ids=["inf", "nan"])
def test_a_pilot_ratio_that_is_not_a_finite_number_is_refused(monkeypatch, bad):
    # A level with nothing under it to compare against is not a level. Neither infinity nor NaN
    # is below the pilot threshold, so either one used to come back as a measurement.
    monkeypatch.setattr(ppm, "pilot_ratio_db", lambda *a, **kw: bad)
    result = measure(137.0, seconds=1.0)
    assert result["ppm"] is None
    assert result["reason"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [math.inf, math.nan], ids=["inf", "nan"])
def test_a_prominence_that_is_not_a_finite_number_is_refused(monkeypatch, bad):
    # The search's prominence travels with every measurement, so a non-finite one spoils the
    # answer as surely as a non-finite ratio does.
    search = ppm.offset_hz_from_phasors
    monkeypatch.setattr(ppm, "offset_hz_from_phasors",
                        lambda *a, **kw: (search(*a, **kw)[0], bad))
    result = measure(137.0, seconds=1.0)
    assert result["ppm"] is None
    assert result["reason"]
    json.dumps(result, allow_nan=False)


def level_stream(seconds, value=1000):
    """One sample value held throughout: energy, but no tone of any frequency."""
    return io.BytesIO((array("h", [value]) * int(ppm.DEMOD_RATE * seconds)).tobytes())


def low_noise_stream(seconds, amplitude=3, seed=101):
    """Noise a few counts high, as from a radio hearing almost nothing. Seeded, so it repeats."""
    rng = random.Random(seed)
    n = int(ppm.DEMOD_RATE * seconds)
    return io.BytesIO(array("h", rng.choices(range(-amplitude, amplitude + 1), k=n)).tobytes())


@pytest.mark.parametrize("stream", [level_stream, low_noise_stream], ids=["constant", "low-noise"])
def test_quiet_audio_that_is_not_silence_is_still_judged_by_its_pilot(stream):
    # Only exact zeros are refused as silence. A constant level or a faint hiss is audio with no
    # pilot in it, and the pilot check refuses it, in its own words, as it always did.
    result = ppm.measure_pilot_ppm(hz=106_500_000, seconds=2.0, capture=lambda **_: stream(2.0))
    assert result["ppm"] is None
    assert "above its shoulders" in result["reason"]


class EndlessStream:
    """rtl_fm, honestly: it streams until something stops it, and never reports an end.

    The synthesised captures above are all finite, which is exactly why they cannot catch a
    reader that never stops. This one refuses to be read past the point where a bounded reader
    would have finished, so an unbounded one fails the test instead of hanging the suite.
    """

    def __init__(self, block_bytes, limit):
        self.chunk = bytes(block_bytes)
        self.limit = limit
        self.reads = 0

    def read(self, n):
        self.reads += 1
        if self.reads > self.limit:
            raise AssertionError(f"read {self.reads} blocks — the capture is not bounded")
        return self.chunk[:n]

    def close(self):
        self.closed = True


def test_a_measurement_stops_reading_once_it_has_what_it_asked_for():
    # rtl_fm does not end on its own. Whatever asks it for twenty seconds has to stop at twenty
    # seconds, or the console blocks forever on a route a learner just pressed a button on.
    wanted = int(2.0 * ppm.DEMOD_RATE / ppm.BLOCK)
    stream = EndlessStream(ppm.BLOCK * 2, limit=wanted + 2)
    result = ppm.measure_pilot_ppm(hz=106_500_000, seconds=2.0, capture=lambda **_: stream)
    assert result["seconds"] == pytest.approx(2.0, abs=0.05)


def test_the_capture_is_closed_even_when_there_is_no_pilot():
    # A refusal must still release the radio; leaving rtl_fm running would hold the dongle
    # against the console for as long as the process lived.
    wanted = int(2.0 * ppm.DEMOD_RATE / ppm.BLOCK)
    stream = EndlessStream(ppm.BLOCK * 2, limit=wanted + 2)
    ppm.measure_pilot_ppm(hz=106_500_000, seconds=2.0, capture=lambda **_: stream)
    assert getattr(stream, "closed", False) is True


def test_a_block_that_does_not_hold_whole_cycles_is_refused():
    # Successive blocks only share a phase reference when the pilot is a whole number of cycles
    # in one block. Without that the second stage is measuring its own bookkeeping.
    with pytest.raises(ppm.PpmError):
        ppm.block_phasors(io.BytesIO(b""), hz=19_000.0, rate=200_000, block=333)


# --- a number is reported with what it can and cannot distinguish -------

def test_a_measurement_reports_the_resolution_its_capture_length_allows():
    # 1/T at 19 kHz: one second resolves no better than 52.6 ppm, which is coarser than the
    # error being measured — the reason this needs twenty seconds and not one.
    assert ppm.resolution_ppm(1.0) == pytest.approx(52.63, abs=0.1)
    assert ppm.resolution_ppm(30.0) == pytest.approx(1.75, abs=0.05)


def test_the_measurement_carries_the_soc_temperature():
    # Crystal frequency moves with temperature, so a ppm figure without one is not comparable
    # to the next one. Lesson 3 already records it with every reading.
    result = ppm.measure_pilot_ppm(hz=106_500_000, seconds=5.0, temp=lambda: 52.5,
                                   capture=lambda **_: synth_stream(137.0, seconds=5.0))
    assert result["soc_temp_c"] == 52.5


# --- rtl_test, the second and unrelated reference -----------------------

RTL_TEST_OUTPUT = """Found 1 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001
Sampling at 2048000 S/s.
Reporting PPM error measurement every 20 seconds...
real sample rate: 2050168 current PPM: 1059 cumulative PPM: 158
real sample rate: 2048367 current PPM: 179 cumulative PPM: 160
real sample rate: 2048281 current PPM: 137 cumulative PPM: 158
real sample rate: 2048377 current PPM: 184 cumulative PPM: 160
real sample rate: 2048303 current PPM: 148 cumulative PPM: 159
"""


def test_rtl_test_output_is_parsed_from_a_real_captured_run():
    # Verbatim from the 2026-09-05 gating run.
    result = ppm.parse_rtl_test(RTL_TEST_OUTPUT)
    assert result["ppm"] == 159
    assert result["converged"] is True
    assert result["readings"][-3:] == [158, 160, 159]


def test_rtl_test_that_has_not_settled_says_so_rather_than_claiming_a_number():
    drifting = "\n".join(f"real sample rate: 2048000 current PPM: 5 cumulative PPM: {v}"
                         for v in (40, 80, 120))
    result = ppm.parse_rtl_test(drifting)
    assert result["converged"] is False


def test_rtl_test_that_printed_nothing_useful_is_a_refusal_not_a_zero():
    result = ppm.parse_rtl_test("usb_claim_interface error -6\n")
    assert result["ppm"] is None
    assert result["reason"]


# --- the reference's own reference --------------------------------------

def test_the_host_clock_reports_its_own_error_and_whether_it_is_disciplined():
    # rtl_test measures the dongle against THIS clock, so its own error is part of the answer.
    # The Nomad's kernel reported freq=-1429383 during the gating run.
    result = ppm.read_host_clock_ppm(read_timex=lambda: (0, -1429383))
    assert result["ppm"] == pytest.approx(-21.81, abs=0.01)
    assert result["synchronised"] is True


def test_an_unsynchronised_host_clock_is_reported_as_such():
    # STA_UNSYNC (0x40) set: nothing is holding the clock, so the figure beside it is a guess.
    result = ppm.read_host_clock_ppm(read_timex=lambda: (0x40, -1429383))
    assert result["synchronised"] is False


def test_a_host_clock_that_cannot_be_read_is_none_not_zero():
    def boom():
        raise OSError("no adjtimex here")
    assert ppm.read_host_clock_ppm(read_timex=boom) is None


# --- two references, and what to do when they disagree ------------------

def test_two_references_that_agree_are_reported_as_agreeing():
    result = ppm.compare_references(164.53, 159, agree_within_ppm=10.0)
    assert result["agree"] is True
    assert result["difference_ppm"] == pytest.approx(5.53, abs=0.01)


def test_two_references_that_disagree_are_reported_as_disagreeing_not_averaged():
    # Averaging two figures you cannot choose between produces a third you cannot defend.
    result = ppm.compare_references(164.53, 40.0, agree_within_ppm=10.0)
    assert result["agree"] is False
    assert "ppm" not in result or result.get("ppm") is None


def test_a_comparison_with_only_one_reference_is_not_an_agreement():
    assert ppm.compare_references(164.53, None, agree_within_ppm=10.0)["agree"] is None


# --- T-07: rtl_fm runs like every other tool, and a missing one is a refusal, not a crash -----


def test_capture_launches_rtl_fm_through_spawn_on_the_given_index(monkeypatch):
    seen = {}

    def fake_spawn(argv, **kw):
        seen["argv"] = argv
        return Child(argv, code=0)

    monkeypatch.setattr(ppm, "spawn", fake_spawn)
    ppm._default_capture(hz=98_100_000, seconds=1, gain=40, device=1)
    argv = seen["argv"]
    assert argv[0] == "rtl_fm"
    assert argv[argv.index("-d") + 1] == "1"


def test_capture_without_rtl_fm_is_a_refusal(monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", "rtl_fm")

    monkeypatch.setattr(ppm, "spawn", missing)
    with pytest.raises(ppm.PpmUnavailable, match="rtl_fm"):
        ppm._default_capture(hz=98_100_000, seconds=1, gain=40)


def test_closing_a_capture_kills_an_rtl_fm_that_ignores_sigterm(monkeypatch):
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.01)
    proc = Child(["rtl_fm"], stubborn=True)
    proc.stdout = io.BytesIO(b"")
    ppm._CaptureStream(proc).close()
    assert proc.sent == ["TERM", "KILL"]


# --- a stalled rtl_fm is stopped at a deadline; a working or finished one reads as before ---


def _stall_rtl_fm(monkeypatch, stand_in=("sleep", "30")):
    """Make rtl_fm one that never delivers its capture: by default alive and writing nothing.

    `sleep` stands in for rtl_fm on a stalled USB stream: a real process on a real pipe, so the
    read blocks exactly as it would on the radio's output, and nothing here opens a dongle. The
    margin drops to half a second so a test takes about one; raising=False keeps the test
    runnable against a capture with no margin to shorten, where it shows the wait. Returns the
    stand-ins as they start.
    """
    monkeypatch.setattr(ppm, "CAPTURE_MARGIN_S", 0.5, raising=False)
    children = []

    def spawn(argv, **kw):
        children.append(childproc.spawn(list(stand_in), **kw))
        return children[-1]

    monkeypatch.setattr(ppm, "spawn", spawn)
    return children


def _reap(children):
    """Leave no stand-in behind, whatever the test did."""
    for child in children:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stdout:
            child.stdout.close()


def _within(seconds, fn):
    """fn's result, or a failure rather than a hung suite if it has not returned in time."""
    box = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:  # handed back to the test, which re-raises it
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(f"still blocked after {seconds:.1f} s")
    if "error" in box:
        raise box["error"]
    return box["result"]


def test_a_stalled_rtl_fm_is_stopped_at_the_deadline_and_measures_nothing(monkeypatch):
    # rtl_fm can stay alive with its USB stream stalled, writing nothing, and a pipe read waits as
    # long as its writer lives: the measurement waited with it, holding the radio. A capture gets
    # its own length plus a margin; then rtl_fm is stopped and the answer is no measurement.
    children = _stall_rtl_fm(monkeypatch)
    deadline = 0.5 + 0.5
    started = time.monotonic()
    try:
        result = _within(deadline + 3.0,
                         lambda: ppm.measure_pilot_ppm(hz=106_500_000, seconds=0.5))
        took = time.monotonic() - started
        codes = [child.poll() for child in children]
    finally:
        _reap(children)
    assert deadline <= took < deadline + 1.0
    assert result["ppm"] is None
    assert "stall" in result["reason"]
    assert codes and None not in codes   # the capture itself stopped rtl_fm
    json.dumps(result, allow_nan=False)


# An rtl_fm that is alive and writing, but far too slowly ever to deliver its capture: two bytes
# every 50 ms. Each single wait for data ends within 50 ms, so only a deadline on the whole
# capture stops it.
_TRICKLE = """
import sys, time
while True:
    sys.stdout.buffer.write(bytes(2))
    sys.stdout.buffer.flush()
    time.sleep(0.05)
"""


def test_an_rtl_fm_that_only_trickles_is_stopped_at_the_deadline_for_the_whole_capture(
        monkeypatch):
    # A timeout for each wait never fires on a trickle: data keeps coming, never enough, and the
    # radio would stay held for as long as it lasted. The deadline is for the whole capture,
    # counted from rtl_fm's start.
    children = _stall_rtl_fm(monkeypatch, [sys.executable, "-c", _TRICKLE])
    deadline = 0.5 + 0.5
    started = time.monotonic()
    try:
        result = _within(deadline + 3.0,
                         lambda: ppm.measure_pilot_ppm(hz=106_500_000, seconds=0.5))
        took = time.monotonic() - started
        codes = [child.poll() for child in children]
    finally:
        _reap(children)
    assert deadline <= took < deadline + 1.0
    assert result["ppm"] is None
    assert "stall" in result["reason"]
    assert codes and None not in codes   # the capture itself stopped rtl_fm


def test_a_read_that_starts_after_the_deadline_takes_what_is_there_and_waits_for_nothing(
        monkeypatch):
    # The reader can be busy elsewhere when the deadline passes. Its next read takes what the
    # pipe already holds and stops there: the time left is then below zero, and a wait for that
    # long is, to poll, a wait without end.
    monkeypatch.setattr(ppm, "CAPTURE_MARGIN_S", 0.0)
    proc = Child(["rtl_fm"])
    held, writer = os.pipe()
    os.write(writer, bytes(1000))       # some audio already waiting, and no more coming
    proc.stdout = os.fdopen(held, "rb")
    stream = ppm._CaptureStream(proc, 0.0)
    time.sleep(0.05)                    # the deadline is 50 ms gone
    try:
        started = time.monotonic()
        data = _within(2.0, lambda: stream.read(4000))
        took = time.monotonic() - started
    finally:
        os.close(writer)
        stream.close()
    assert len(data) == 1000
    assert stream.stalled is True
    assert took < 0.25


def test_a_stalled_rtl_fm_that_will_not_stop_is_not_reported_as_stopped(monkeypatch):
    # An rtl_fm in uninterruptible sleep outlives SIGKILL and keeps the dongle, and the next tool
    # will find it busy. The answer must not say it was stopped; it says it would not stop.
    monkeypatch.setattr(ppm, "CAPTURE_MARGIN_S", 0.2)
    proc = Child(["rtl_fm"], unkillable=True)
    quiet, writer = os.pipe()           # an output that stays open and never carries a byte
    proc.stdout = os.fdopen(quiet, "rb")
    try:
        result = _within(5.0, lambda: ppm.measure_pilot_ppm(
            hz=106_500_000, seconds=0.2, capture=lambda **_: ppm._CaptureStream(proc, 0.2)))
    finally:
        os.close(writer)
    assert proc.sent == ["TERM", "KILL"]
    assert result["ppm"] is None
    assert "would not stop" in result["reason"]
    assert "was stopped" not in result["reason"]


# A working rtl_fm, standing in on a real pipe: it streams a capture file in pieces that are not
# whole blocks, pausing between them so the reader keeps catching up, then idles, never ending.
_STREAM_THEN_IDLE = """
import sys, time
data = open(sys.argv[1], "rb").read()
for i in range(0, len(data), 1500):
    sys.stdout.buffer.write(data[i:i + 1500])
    sys.stdout.buffer.flush()
    time.sleep(0.001)
time.sleep(30)
"""


def test_a_working_rtl_fm_is_read_in_whole_blocks_through_a_real_pipe(tmp_path, monkeypatch):
    # The deadline means reading the pipe in whatever pieces have arrived; a block must still be
    # a whole block, and a capture that keeps up must never be called a stall.
    recorded = tmp_path / "pilot.raw"
    recorded.write_bytes(synth_stream(137.0, seconds=1.0).getvalue())
    children = []

    def spawn(argv, **kw):
        children.append(childproc.spawn([sys.executable, "-c", _STREAM_THEN_IDLE, str(recorded)],
                                        **kw))
        return children[-1]

    monkeypatch.setattr(ppm, "spawn", spawn)
    try:
        result = _within(10.0, lambda: ppm.measure_pilot_ppm(hz=106_500_000, seconds=1.0))
        codes = [child.poll() for child in children]
    finally:
        _reap(children)
    assert result["ppm"] == pytest.approx(137.0, abs=0.5)
    assert codes and None not in codes   # stopped once it had all it asked for


def test_an_rtl_fm_that_exits_is_an_early_end_not_a_stall(monkeypatch):
    # rtl_fm exits at once when another process holds the dongle. Its end is the end of the read,
    # straight away, and not something to wait out to the deadline and call a stall.
    children = []

    def spawn(argv, **kw):
        children.append(childproc.spawn(["true"], **kw))
        return children[-1]

    monkeypatch.setattr(ppm, "spawn", spawn)
    started = time.monotonic()
    try:
        result = _within(10.0, lambda: ppm.measure_pilot_ppm(hz=106_500_000, seconds=1.0))
    finally:
        _reap(children)
    assert time.monotonic() - started < 1.0
    assert result["ppm"] is None
    assert result["reason"] == "the capture ended before enough audio arrived to measure"


# --- through the route: a refusal is an answer, the last real figure stands, the radio returns --


def _short(seconds, **fixed):
    """The real measurement as the route calls it, over a shorter capture than the route asks."""
    def measure(**kw):
        return ppm.measure_pilot_ppm(**{**kw, "seconds": seconds, **fixed})
    return measure


def test_a_silent_capture_through_the_route_is_an_answer_and_keeps_the_last_real_figure(tmp_path):
    # Silence used to reach the route as +400.16 ppm: the route kept it as the last measurement,
    # to be stamped on every later reception, and then failed to send it. A refusal is a 200 the
    # panel can read, and the last real measurement stays the one receptions carry.
    captures = iter([synth_stream(137.0, seconds=1.0), silent_stream(1.0)])
    with _client(tmp_path, ppm_measure=_short(1.0, capture=lambda **_: next(captures)),
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        real = client.post("/api/ppm/measure?hz=98100000").json()
        silent = client.post("/api/ppm/measure?hz=98100000")
        last = client.app.state.last_ppm
    assert real["ppm"] == pytest.approx(137.0, abs=0.5)
    assert silent.status_code == 200
    assert silent.json()["ppm"] is None
    assert silent.json()["reason"]
    assert last == real["ppm"]


def test_a_stalled_capture_through_the_route_answers_and_hands_the_radio_back(tmp_path,
                                                                              monkeypatch):
    # The measurement holds the one radio lock for its whole capture, so a stall blocked every
    # SDR route behind it: the waterfall's next poll waited for as long as rtl_fm lived.
    children = _stall_rtl_fm(monkeypatch)
    with _client(tmp_path, ppm_measure=_short(0.5),
                 sweeper_factory=lambda: _StubSweeper(_rows())) as client:
        try:
            resp = _within(4.0, lambda: client.post("/api/ppm/measure?hz=98100000"))
            codes = [child.poll() for child in children]
            spectrum = _within(2.0, lambda: client.get("/api/spectrum"))
        finally:
            _reap(children)
        last = client.app.state.last_ppm
        sweeping = client.app.state.sweeper.running
    assert resp.status_code == 200
    assert resp.json()["ppm"] is None
    assert "stall" in resp.json()["reason"]
    assert last is None
    assert codes and None not in codes
    assert spectrum.status_code == 200
    assert sweeping                      # the waterfall's poll got the radio back
