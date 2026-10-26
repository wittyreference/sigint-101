# ABOUTME: Tests the frequency-error instrument — the receiver's crystal against two references.
# ABOUTME: Every seam is injected: a synthesised pilot stands in for the radio, no dongle opened.

import io
import math
from array import array

import pytest

from sigint101 import ppm


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
    # The single easiest thing in this module to get backwards. One crystal clocks both the
    # tuner and the sample rate, so a fast crystal stretches assumed time and the tone lands low
    # — while the dial reads lower than the frequency actually being received.
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
