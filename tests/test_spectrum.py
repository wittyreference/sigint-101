# ABOUTME: Unit tests for the rtl_power sweep parser and the live Sweeper.
# ABOUTME: No radio means no data — the Sweeper reports absence, it never invents a signal.

import io
import queue
import subprocess
import threading
import time
from pathlib import Path

import pytest

# Through the module, so a name a later section adds fails only the tests that use it.
import sigint101.spectrum as spectrum_module
from sigint101.spectrum import (
    MAX_BINS,
    SPAN_GUARD_BINS,
    TUNER_MAX_HZ,
    TUNER_MIN_HZ,
    SpectrumUnavailable,
    Sweeper,
    SweepRow,
    TuneError,
    hop_plan,
    merge_chunks,
    parse_line,
    target_span,
    validate_gain,
    validate_span,
)

# Real rtl_power output, captured on nomad-b (Debian rtl-sdr 0.6.0-4, R828D) on 2026-11-01:
#   fm-88-108-125k.csv          rtl_power -f 88000000:108000000:125000 -i 1 -e 3   (8 hops x 32)
#   adsb-1089-1091-31k.csv      rtl_power -f 1089000000:1091000000:31250 -i 1 -1   (1 hop x 64)
#   fm-narrow-95.9-96.3-10k.csv rtl_power -f 95900000:96300000:10000 -i 1 -1       (1 hop x 64,
#                               the downsampled path: a 400 kHz span decimated by 7)
CAPTURES = Path(__file__).resolve().parent / "fixtures" / "rtl_power"


def _capture(name):
    """The non-blank lines of one real rtl_power capture."""
    return [line for line in (CAPTURES / name).read_text().splitlines() if line.strip()]


def _raw_values(line):
    """The power columns of an rtl_power line exactly as printed, before any parsing."""
    return [float(v) for v in line.split(",")[6:]]


FM_LINES = _capture("fm-88-108-125k.csv")

# Shaped like rtl_power 0.6.0's csv_dbm(): one 88.0-90.5 MHz hop in 78125 Hz bins prints its 32
# bins, then the last bin a second time (33 values). Its DC bin, printed at index 16, is a copy of
# index 17 ("nuke DC component": avg[0] = avg[1], before the 180-degree rotation).
_HEAD = "2026-08-24, 02:40:17, 88000000, 90500000, 78125.00, 768, "


def _hop_values(base):
    values = [base - i / 100 for i in range(32)]
    values[16] = values[17]           # rtl_power's DC copy
    return values + [values[-1]]      # rtl_power's trailing duplicate


LINE_A = _HEAD + ", ".join(f"{v:.2f}" for v in _hop_values(-10.0))
LINE_B = ("2026-08-24, 02:40:17, 90500000, 93000000, 78125.00, 768, "
          + ", ".join(f"{v:.2f}" for v in _hop_values(-20.0)))


def test_parse_line_reads_header_and_powers():
    row = parse_line(LINE_A)
    assert row.ts == "2026-08-24 02:40:17"
    assert row.freq_low == 88_000_000
    assert row.freq_high == 90_500_000
    assert row.freq_step == 78125.0
    assert len(row.dbm) == 32
    assert row.dbm[0] == -10.0


def test_parse_line_tolerates_whitespace_and_trailing_comma():
    row = parse_line("  " + LINE_A + " ,  \n")
    assert len(row.dbm) == 32


@pytest.mark.parametrize("junk", ["", "   ", "\n", "not,enough,fields", "# comment"])
def test_parse_line_rejects_junk(junk):
    assert parse_line(junk) is None


# --- every bin a measurement (T-03) -------------------------------------------------------
# rtl_power 0.6.0 prints len+1 values per hop: its loop prints bins 0..len-1, then prints the
# last bin again on its own. It also overwrites each hop's DC bin with its neighbour before
# printing. Neither copy is a measurement at the frequency it would be drawn at.


def test_the_real_capture_has_the_two_copies_this_parser_removes():
    # The premise, checked against real output rather than a reading of the C: every line ends
    # on a repeated value, and every hop's centre value equals the one above it.
    for line in FM_LINES:
        raw = _raw_values(line)
        assert len(raw) == 33
        assert raw[-1] == raw[-2]
        assert raw[16] == raw[17]


def test_parse_line_drops_rtl_powers_trailing_duplicate():
    raw = _raw_values(FM_LINES[0])
    row = parse_line(FM_LINES[0])
    assert len(row.dbm) == 32
    assert row.dbm[-1] == raw[31]


def test_each_bin_sits_at_its_fft_bin_centre():
    # FFT bin k of a hop is centred at low + k*step, so the first lands on the low edge and the
    # last a step short of the high edge, where the next hop's first bin is measured.
    row = parse_line(FM_LINES[0])
    freqs = row.frequencies()
    assert len(freqs) == len(row.dbm) == 32
    assert freqs[0] == 88_000_000
    assert freqs[-1] == 90_500_000 - 78125


def test_the_dc_copy_at_each_hop_centre_is_marked_unmeasured():
    for line in FM_LINES:
        row = parse_line(line)
        assert row.dbm[16] is None
        assert sum(v is None for v in row.dbm) == 1
        assert row.frequencies()[16] == (row.freq_low + row.freq_high) / 2


@pytest.mark.parametrize("name", ["adsb-1089-1091-31k.csv", "fm-narrow-95.9-96.3-10k.csv"])
def test_the_dc_copy_is_found_in_a_single_hop_capture_too(name):
    # The narrow capture takes rtl_power's downsampling path; the DC bin is still the hop centre.
    line = _capture(name)[0]
    raw = _raw_values(line)
    row = parse_line(line)
    assert len(row.dbm) == 64
    assert raw[32] == raw[33]
    assert row.dbm[32] is None
    assert [v for i, v in enumerate(row.dbm) if i != 32] == raw[:32] + raw[33:64]


def test_a_single_bin_hop_has_no_dc_copy():
    # rtl_power's "giant bins" mode (a bin of 1 MHz or more) prints one bin per hop, then the same
    # value again; with one bin there is no DC neighbour to copy, so nothing is marked.
    row = parse_line("2026-08-24, 02:40:17, 100000000, 102000000, 2000000.00, 4, -5.00, -5.00")
    assert row.dbm == (-5.0,)


def test_merge_keeps_every_hops_first_bin():
    chunks = [parse_line(line) for line in FM_LINES[:8]]
    merged = merge_chunks(chunks)
    assert len(merged.dbm) == 256
    assert merged.freq_low == 88_000_000
    assert merged.freq_high == 108_000_000
    # Hop 2's first bin is the only measurement at 90.5 MHz, so it is the one kept.
    assert merged.dbm[32] == chunks[1].dbm[0] == _raw_values(FM_LINES[1])[0]
    freqs = merged.frequencies()
    assert freqs[32] == 90_500_000
    assert freqs[-1] == 108_000_000 - 78125


def test_merge_leaves_the_eight_dc_bins_unmeasured_and_nothing_else():
    merged = merge_chunks([parse_line(line) for line in FM_LINES[:8]])
    assert [i for i, v in enumerate(merged.dbm) if v is None] == [16 + 32 * h for h in range(8)]


def test_merge_leaves_a_gap_between_hops_unmeasured_rather_than_closing_it():
    # Concatenating across a gap would slide every later bin onto the wrong frequency. A gap is
    # held open with unmeasured bins so each value stays at the frequency it was measured at.
    a = SweepRow(ts="t", freq_low=100.0, freq_high=104.0, freq_step=1.0,
                 dbm=(-1.0, -2.0, -3.0, -4.0))
    b = SweepRow(ts="t", freq_low=106.0, freq_high=108.0, freq_step=1.0,
                 dbm=(-5.0, -6.0))
    merged = merge_chunks([a, b])
    assert merged.dbm == (-1.0, -2.0, -3.0, -4.0, None, None, -5.0, -6.0)
    assert merged.frequencies()[6] == 106.0


def test_the_readout_ignores_unmeasured_bins():
    row = SweepRow(ts="t", freq_low=100.0, freq_high=105.0, freq_step=1.0,
                   dbm=(-30.0, -10.0, None, -31.0, -29.0))
    assert row.noise_floor_db() == -29.5
    assert row.peak() == (101.0, -10.0)


def test_as_dict_sends_an_unmeasured_bin_as_null():
    row = parse_line(FM_LINES[0])
    flat = row.as_dict()
    assert flat["dbm"][16] is None
    assert len(flat["dbm"]) == 32


def test_the_sweeper_publishes_real_rows_of_measured_bins():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc(FM_LINES), radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    rows = sweeper.rows()
    assert len(rows) == 3
    assert all(len(row.dbm) == 256 for row in rows)
    assert all(sum(v is None for v in row.dbm) == 8 for row in rows)


def test_merge_chunks_joins_hops_without_losing_or_repeating_a_bin():
    a, b = parse_line(LINE_A), parse_line(LINE_B)
    merged = merge_chunks([a, b])
    assert len(merged.dbm) == 64
    assert merged.freq_low == 88_000_000
    assert merged.freq_high == 93_000_000
    assert merged.frequencies()[-1] == 93_000_000 - 78125
    assert merged.dbm[31] == a.dbm[31]
    assert merged.dbm[32] == b.dbm[0]


def test_merge_chunks_sorts_out_of_order_chunks():
    a, b = parse_line(LINE_A), parse_line(LINE_B)
    assert merge_chunks([b, a]).freq_low == 88_000_000


def test_merge_chunks_single_chunk_is_identity():
    a = parse_line(LINE_A)
    assert merge_chunks([a]) == a


def test_merge_chunks_rejects_empty():
    with pytest.raises(ValueError):
        merge_chunks([])


class FakeProc:
    """Stands in for a Popen'd rtl_power at the process boundary."""

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_sweeper_assembles_chunks_sharing_a_timestamp_into_one_row():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A, LINE_B]), radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    rows = sweeper.rows()
    assert len(rows) == 1
    assert len(rows[0].dbm) == 64


def test_sweeper_starts_a_new_row_on_a_new_timestamp():
    later = LINE_A.replace("02:40:17", "02:40:19")
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A, LINE_B, later]),
                      radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert len(sweeper.rows()) == 2


def test_sweeper_buffer_is_bounded_to_the_newest_rows():
    lines = [LINE_A.replace("02:40:17", f"02:40:{s:02d}") for s in range(10, 40)]
    sweeper = Sweeper(spawn=lambda cmd: FakeProc(lines), history=5, radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    rows = sweeper.rows()
    assert len(rows) == 5
    assert rows[-1].ts.endswith("02:40:39")


def test_sweeper_refuses_to_start_without_a_radio():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([]), radio_present=lambda: False)
    with pytest.raises(SpectrumUnavailable):
        sweeper.start()
    assert sweeper.rows() == []


def test_sweeper_reports_a_missing_rtl_power_binary():
    def no_binary(cmd):
        raise FileNotFoundError("rtl_power")

    # Radio forced present so this exercises the missing-binary path, not the no-radio one.
    sweeper = Sweeper(spawn=no_binary, radio_present=lambda: True)
    with pytest.raises(SpectrumUnavailable):
        sweeper.start()


class DeadProc(FakeProc):
    """An rtl_power that has exited — the shape of a dongle pulled out mid-sweep.

    The distinction FakeProc cannot express: `poll()` returning a code means the
    process is gone, and rtl_power's stderr carries the only text that says why.
    """

    def __init__(self, lines, returncode=1, stderr="No matching devices found.\n"):
        super().__init__(lines)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class BlockingProc(FakeProc):
    """rtl_power as it really behaves: emits some lines, then blocks on the pipe until stopped.

    FakeProc/DeadProc exhaust their stdout on their own, which the live process never does — it
    blocks reading until terminate() closes the pipe. A test of stop() needs that: otherwise the
    reader can reach its end-of-stream handling before stop() has marked the interruption, which
    is a test artifact, not how the console runs. poll() is None until terminate(), then the code.
    """

    def __init__(self, lines, returncode=-15):
        super().__init__(lines)
        self._gate = threading.Event()
        self._returncode = returncode
        self.returncode = None
        self.stdout = self._emit(list(lines))

    def _emit(self, lines):
        for line in lines:
            yield line
        # Block like a live pipe until terminate() closes it, so the reader is still inside the
        # read loop when stop() sets _interrupted.
        self._gate.wait(5)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = self._returncode
        self._gate.set()

    def wait(self, timeout=None):
        return self.returncode


class FedProc(FakeProc):
    """rtl_power after a (re)start: it prints only what a test feeds it, blocks like a live pipe in
    between, and ends when stopped or when it exits with a status of its own, as a pulled dongle is
    expected to end it."""

    def __init__(self):
        super().__init__([])
        self._lines = queue.Queue()
        self.returncode = None
        self.stdout = iter(self._lines.get, None)
        self.stderr = io.StringIO("")

    def feed(self, lines):
        for line in lines:
            self._lines.put(line)

    def die(self, code):
        self.returncode = code
        self._lines.put(None)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15
        self._lines.put(None)

    def wait(self, timeout=None):
        return self.returncode


def wait_for_rows(sweeper, count, timeout=5.0):
    """Wait for the sweeper to hold `count` rows: a pass is published when the next one begins."""
    deadline = time.monotonic() + timeout
    while len(sweeper.rows()) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(sweeper.rows()) >= count, f"{len(sweeper.rows())} rows after {timeout} s"


def _bus(*serials):
    """A stand-in enumerator: the bus as librtlsdr would number it."""
    from sigint101.hardware import RtlSdrDevice
    return lambda: tuple(RtlSdrDevice(i, s) for i, s in enumerate(serials))


def test_without_an_enumerator_the_configured_index_stands():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([]), radio_present=lambda: True, device=3)
    sweeper.start()
    assert "-d" in sweeper.command()
    assert sweeper.command()[sweeper.command().index("-d") + 1] == "3"


def test_a_pinned_serial_picks_its_own_index_not_its_numeric_value():
    # The Nomad's radio is SN 00000001 at index 0; a KrakenSDR tuner sits at index 1.
    # Handing "00000001" straight to rtl_power would open index 1 — the wrong radio.
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([]), radio_present=lambda: True,
                      serial="00000001", list_devices=_bus("00000001", "1004", "1000"))
    sweeper.start()
    assert sweeper.device == 0


def test_an_ambiguous_bus_refuses_to_sweep_rather_than_guessing():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([]), radio_present=lambda: True,
                      list_devices=_bus("00000001", "1004", "1000"))
    with pytest.raises(SpectrumUnavailable) as caught:
        sweeper.start()
    # The refusal has to be actionable, so it names the candidates and how to pin one.
    assert "SIGINT101_RTLSDR_SERIAL" in str(caught.value)
    assert "1004" in str(caught.value)


def test_a_single_radio_still_needs_no_pinning():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([]), radio_present=lambda: True,
                      list_devices=_bus("00000001"))
    sweeper.start()
    assert sweeper.device == 0


def test_a_sweep_keeps_the_serial_the_bus_survey_listed_for_its_radio():
    # The shipped unit pins no serial. The survey made before the radio is opened lists the serial
    # string the dongle reports, and a baseline sample records it. Many factory-default dongles
    # report the same serial (the built-in reports 00000001), so it tells apart only dongles whose
    # strings differ.
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([]), radio_present=lambda: True,
                      list_devices=_bus("1004"))
    sweeper.start()
    assert (sweeper.device, sweeper.device_serial) == (0, "1004")


def test_a_sweep_with_no_surveyed_serial_keeps_none():
    # No survey (the caller chose the index), or a radio whose EEPROM holds no serial string:
    # either way the serial is unknown, and unknown is None, never a guess.
    unsurveyed = Sweeper(spawn=lambda cmd: FakeProc([]), radio_present=lambda: True)
    blank = Sweeper(spawn=lambda cmd: FakeProc([]), radio_present=lambda: True,
                    list_devices=_bus(""))
    unsurveyed.start()
    blank.start()
    assert unsurveyed.device_serial is None
    assert blank.device_serial is None


def _fed(procs):
    """A spawn that starts a FedProc each time, keeping them so a test can feed the newest."""
    def spawn(cmd):
        procs.append(FedProc())
        return procs[-1]
    return spawn


def test_after_a_gain_step_only_a_row_the_restarted_sweep_swept_is_a_reading():
    # A gain step keeps the old rows for the waterfall (Lesson 2 shows the step), but they were
    # swept at the old gain. A reading names the settings it was swept under, so until the
    # restarted rtl_power has finished a pass there is no reading at all.
    procs = []
    sweeper = Sweeper(spawn=_fed(procs), radio_present=lambda: True, list_devices=_bus("1004"))
    sweeper.start()
    procs[-1].feed(FM_LINES)
    wait_for_rows(sweeper, 2)
    row, settings = sweeper.reading()
    assert row == sweeper.rows()[-1]
    assert settings == {"serial": "1004", "gain": None, "bin_size": 125_000,
                        "freq_low": 88_000_000, "freq_high": 108_000_000}
    sweeper.set_gain(0.0)
    assert sweeper.reading() == (None, {**settings, "gain": 0.0})
    sweeper.start()
    assert sweeper.reading()[0] is None
    assert len(sweeper.rows()) == 2               # still there for the waterfall
    procs[-1].feed(FM_LINES)
    wait_for_rows(sweeper, 4)
    assert sweeper.reading() == (sweeper.rows()[-1], {**settings, "gain": 0.0})
    sweeper.stop()


def test_rows_a_dead_sweep_left_behind_are_no_reading():
    # When rtl_power exits, as a pulled dongle is expected to make it, its rows stay in the buffer.
    # Whatever radio is opened next, those rows are not a reading through it.
    procs = []
    sweeper = Sweeper(spawn=_fed(procs), radio_present=lambda: True, list_devices=_bus("1004"))
    sweeper.start()
    procs[-1].feed(FM_LINES)
    wait_for_rows(sweeper, 2)
    procs[-1].die(1)
    sweeper.drain()
    assert len(sweeper.rows()) == 2
    assert sweeper.reading()[0] is None


# --- C22: an rtl_power that stops sweeping without exiting leaves no reading -------------------
# WHY: reading() handed out the running sweep's newest row however old it was. An rtl_power that
# stops sweeping without exiting still reads as running, so its last row stayed the reading for
# good, and the baseline panel could keep reading HEALTHY on a level that only grew old. A row past
# the sweep's age limit is no reading now. The waterfall still draws the kept rows, as before.

def _fm_pass(n):
    """One pass of the captured FM band: the capture's three passes, taken in turn."""
    first = 8 * (n % 3)
    return FM_LINES[first:first + 8]


def _sweeping(clock):
    """A running sweep on an injected clock that has published two rows of the captured FM band.

    Its rtl_power stays alive and prints only what the test feeds procs[-1] next. The capture's
    third pass is held, as every pass is, until the next one begins.
    """
    procs = []
    sweeper = Sweeper(spawn=_fed(procs), radio_present=lambda: True, clock=clock)
    sweeper.start()
    procs[-1].feed(FM_LINES)
    wait_for_rows(sweeper, 2)
    return sweeper, procs


def test_a_sweep_that_stops_without_exiting_has_no_reading():
    clock = _Clock()
    sweeper, _ = _sweeping(clock)
    row, settings = sweeper.reading()
    assert row == sweeper.rows()[-1]
    clock.now += 60                               # a minute with no new pass
    assert sweeper.running                        # rtl_power is still alive
    assert sweeper.reading() == (None, settings)
    assert len(sweeper.rows()) == 2               # still there for the waterfall
    sweeper.stop()


def test_a_row_is_a_reading_up_to_its_age_limit_and_none_past_it():
    clock = _Clock()
    sweeper, _ = _sweeping(clock)
    newest = sweeper.rows()[-1]
    clock.now += sweeper.stale_after() - 0.01
    assert sweeper.reading()[0] == newest
    clock.now += 0.02
    assert sweeper.reading()[0] is None
    sweeper.stop()


def test_each_fresh_row_starts_the_age_limit_again():
    # A sweep delivering at its own pace never ages out, however long it runs: the reading is
    # judged by the newest row's age, so rows that each land just inside the limit keep it.
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    limit = sweeper.stale_after()
    for n in range(10):
        clock.now += limit - 0.01
        assert sweeper.reading()[0] == sweeper.rows()[-1]
        procs[-1].feed(_fm_pass(n))               # publishes the pass it held
        wait_for_rows(sweeper, 3 + n)
    clock.now += limit + 0.01
    assert sweeper.reading()[0] is None
    sweeper.stop()


def test_a_restarted_sweep_reads_only_its_own_rows_and_they_age_too():
    # As C12 made it: after a restart the old start's rows are no reading, fresh or not, until the
    # restarted rtl_power has finished a pass. Its own rows then age like any other.
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    sweeper.set_gain(0.0)
    sweeper.start()
    assert sweeper.reading()[0] is None
    clock.now += 60
    procs[-1].feed(FM_LINES)
    wait_for_rows(sweeper, 4)
    assert sweeper.reading()[0] == sweeper.rows()[-1]
    clock.now += 60
    assert sweeper.reading()[0] is None
    sweeper.stop()


@pytest.mark.parametrize("low, high, bin_size", [
    (88_000_000, 108_000_000, 125_000),           # the FM default: 8 hops
    (24_000_000, 124_000_000, 125_000),           # the widest span: 36 hops
    (24_000_000, 124_000_000, 1_000_000),         # the same span in one-bin hops: 100 hops
])
def test_the_age_limit_outlasts_three_rows_of_a_healthy_sweep(low, high, bin_size):
    # rtl_power prints a row at the first pass to end after each interval's tick, so a healthy
    # sweep can go an interval plus a pass between rows. The real FM capture integrated two passes
    # of its eight hops into each one-second row (512 samples a hop, 256 a pass): about 1/16 s a
    # hop. The limit must leave three such rows room at that pace, however many hops a span takes.
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([]), low=low, high=high, bin_size=bin_size)
    hops = len(hop_plan(low, high, bin_size).centres)
    assert sweeper.stale_after() >= 3 * (sweeper.integration + hops / 16)


# --- C23: the first rows after a stall carry levels from before it ------------------------------
# WHY: a pass is published when the next one begins. After a stall, the pass held back when
# rtl_power stalled is published, stamped with the time it is published, when the first pass after
# the stall arrives, and the row after it is the pass rtl_power was sweeping when it stalled,
# finished after. Both were the reading at once, and a Capture logged a level from before the
# stall. The first two rows published after a wait longer than stale_after() are no reading now;
# the third is.

def test_after_a_stall_the_first_two_rows_published_are_no_reading_and_the_third_is():
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    clock.now += 60                               # a minute with no new pass
    for n in range(2):
        procs[-1].feed(_fm_pass(n))               # publishes the pass held back, then the next
        wait_for_rows(sweeper, 3 + n)
        assert sweeper.reading()[0] is None, f"row {n + 1} after the stall is a reading"
    procs[-1].feed(_fm_pass(2))
    wait_for_rows(sweeper, 5)
    assert sweeper.reading()[0] == sweeper.rows()[-1]
    sweeper.stop()


def test_a_wait_just_past_the_age_limit_is_a_stall():
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    clock.now += sweeper.stale_after() + 0.01
    procs[-1].feed(_fm_pass(0))
    wait_for_rows(sweeper, 3)
    assert sweeper.reading()[0] is None
    sweeper.stop()


def test_rows_that_each_land_within_the_age_limit_are_each_a_reading_at_once():
    # No stall, no change: a sweep delivering at its own pace reads each row as it is published.
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    for n in range(3):
        clock.now += sweeper.stale_after() - 0.01
        procs[-1].feed(_fm_pass(n))
        wait_for_rows(sweeper, 3 + n)
        assert sweeper.reading()[0] == sweeper.rows()[-1]
    sweeper.stop()


def test_the_newest_row_reads_stalled_then_resuming_then_a_reading_again():
    # One rule for the reading, the Capture refusal and the waterfall: newest() says how the newest
    # row stands, and reading() hands a row out only when it is not stale.
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    states = [sweeper.newest()[1]]
    clock.now += 60
    states.append(sweeper.newest()[1])
    for n in range(3):
        procs[-1].feed(_fm_pass(n))
        wait_for_rows(sweeper, 3 + n)
        states.append(sweeper.newest()[1])
    assert states == [None, "stalled", "resuming", "resuming", None]
    sweeper.stop()


# --- C27: a reader that outlives its stop publishes nothing into the next run -----------------
# WHY: a reader can outlive stop()'s wait for it: an rtl_power that is not reaped keeps its pipe
# open, and a reader slowed on a loaded Pi can still be reading after its process has gone. It then
# shared its list of chunks with the next run's reader, and at its end it asked the new rtl_power,
# not its own, whether it had exited. So part of the new run's pass could be published as a whole
# row, and the old run's held pass as the new run's first reading. A gain step, a retune and the
# poll after a death all start a new run. A reader now keeps its own process and chunks, and
# publishes nothing once its run is stopped or replaced.

class LateReaderProc(FedProc):
    """rtl_power whose pipe outlives it, so its reader is still reading after the process ended.

    terminate() and die() end the process but not the pipe; only close_pipe() or break_pipe()
    ends what the reader reads. `reader` is the thread that reads it.
    """

    def __init__(self):
        super().__init__()
        self.reader = None
        self.stdout = self._stream()

    def _stream(self):
        self.reader = threading.current_thread()
        for line in iter(self._lines.get, None):
            if isinstance(line, OSError):
                raise line
            yield line

    def die(self, code):
        self.returncode = code

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def close_pipe(self):
        self._lines.put(None)

    def break_pipe(self, error):
        self._lines.put(error)


def _late_then_fed(procs):
    """A spawn whose first rtl_power's pipe outlives it; every later start gets a FedProc."""
    def spawn(cmd):
        procs.append(FedProc() if procs else LateReaderProc())
        return procs[-1]
    return spawn


def _spans(rows):
    """Each row's first and last frequency and its bin count: part of a pass shows as its own."""
    return [(row.freq_low, row.freq_high, len(row.dbm)) for row in rows]


def test_a_reader_that_outlives_a_gain_step_publishes_nothing_into_the_restarted_sweep():
    procs = []
    sweeper = Sweeper(spawn=_late_then_fed(procs), radio_present=lambda: True)
    sweeper.STOP_WAIT_S = 0.05                    # stop() stops waiting for the reader at once
    sweeper.start()
    procs[0].feed(FM_LINES)                       # two passes published, the third held
    wait_for_rows(sweeper, 2)
    kept = _spans(sweeper.rows())
    sweeper.set_gain(0.0)
    assert procs[0].reader.is_alive()             # the old reader outlived the stop
    sweeper.start()
    procs[1].feed(_fm_pass(0)[:4])                # half of the restarted sweep's first pass
    procs[0].close_pipe()                         # the old reader comes to the end of its pipe
    procs[0].reader.join(5)
    assert _spans(sweeper.rows()) == kept
    procs[1].feed(_fm_pass(0)[4:] + _fm_pass(1)[:1])   # the rest of the pass; the next begins
    wait_for_rows(sweeper, 3)
    assert _spans(sweeper.rows()) == kept + [(88_000_000, 108_000_000, 256)]
    assert sweeper.reading()[0] == sweeper.rows()[-1]
    sweeper.stop()


def test_a_reader_that_outlives_a_retune_paints_no_row_on_the_new_span():
    # The old reader reads on through the FM band after the restart. The retune dropped the old
    # span's rows; none may come back, and no part of the new span's first pass may stand as a row.
    procs = []
    sweeper = Sweeper(spawn=_late_then_fed(procs), radio_present=lambda: True)
    sweeper.STOP_WAIT_S = 0.05
    sweeper.start()
    procs[0].feed(FM_LINES[:12])                  # one pass published, half the next held
    wait_for_rows(sweeper, 1)
    sweeper.retune(88_000_000, 93_000_000, 78_125)    # the two hops LINE_A and LINE_B sweep
    assert procs[0].reader.is_alive()
    sweeper.start()
    procs[1].feed([LINE_A])                       # the new span's first hop
    procs[0].feed(FM_LINES[12:])
    procs[0].close_pipe()
    procs[0].reader.join(5)
    assert _spans(sweeper.rows()) == []
    procs[1].feed([LINE_B, LINE_A.replace("02:40:17", "02:40:18")])
    wait_for_rows(sweeper, 1)
    assert _spans(sweeper.rows()) == [(88_000_000, 93_000_000, 64)]
    assert sweeper.reading()[0] == sweeper.rows()[-1]
    sweeper.stop()


@pytest.mark.parametrize("stand_down, args, lines, span", [
    ("retune", (88_000_000, 93_000_000, 78_125),
     [LINE_A, LINE_B, LINE_A.replace("02:40:17", "02:40:18")], (88_000_000, 93_000_000, 64)),
    ("release", (), _fm_pass(0) + _fm_pass(1)[:1], (88_000_000, 108_000_000, 256)),
], ids=["retune", "release"])
def test_a_reader_that_outlives_its_stop_publishes_nothing_before_the_next_start(
        stand_down, args, lines, span):
    # Until the next start only the stop mark shuts the old reader out, as the run's number changes
    # at that start. After a retune that lasts until the next poll; after a release, as long as the
    # other tool holds the radio. Here the old reader ends the pass it held, begins the next, and
    # its pipe closes, all before the next start. No row of it may stand, then or after.
    procs = []
    sweeper = Sweeper(spawn=_late_then_fed(procs), radio_present=lambda: True)
    sweeper.STOP_WAIT_S = 0.05
    sweeper.start()
    procs[0].feed(FM_LINES[:12])                  # one pass published, half the next held
    wait_for_rows(sweeper, 1)
    getattr(sweeper, stand_down)(*args)
    procs[0].feed(FM_LINES[12:17])                # the held pass ends, and the next one begins
    procs[0].close_pipe()
    procs[0].reader.join(5)
    assert _spans(sweeper.rows()) == []
    sweeper.resume()                              # the radio comes back, after a release
    sweeper.start()
    procs[1].feed(lines)                          # a whole pass of the new run; the next begins
    wait_for_rows(sweeper, 1)
    assert _spans(sweeper.rows()) == [span]
    sweeper.stop()


def test_the_slow_reader_of_a_dead_sweep_publishes_nothing_into_the_next_one():
    # rtl_power exits on its own while its reader still has lines to read, and the next poll starts
    # another. The old reader's run is no longer the one running: it publishes nothing, and records
    # no death against the new one.
    procs = []
    sweeper = Sweeper(spawn=_late_then_fed(procs), radio_present=lambda: True)
    sweeper.start()
    procs[0].feed(FM_LINES[:12])                  # one pass published, half the next held
    wait_for_rows(sweeper, 1)
    kept = _spans(sweeper.rows())
    procs[0].die(1)
    sweeper.start()                               # the next poll finds it gone and starts again
    procs[1].feed(_fm_pass(0)[:4])
    procs[0].feed(FM_LINES[12:16])                # the old reader reads on, slowly
    procs[0].close_pipe()
    procs[0].reader.join(5)
    assert (_spans(sweeper.rows()), sweeper.error) == (kept, None)
    procs[1].feed(_fm_pass(0)[4:] + _fm_pass(1)[:1])
    wait_for_rows(sweeper, 2)
    assert _spans(sweeper.rows()) == kept + [(88_000_000, 108_000_000, 256)]
    assert sweeper.reading()[0] == sweeper.rows()[-1]
    sweeper.stop()


def test_a_reader_that_outlives_its_stop_leaves_no_failure_for_the_next_start():
    # The old pipe fails after the restart. That was the old run's pipe, and a failure kept would
    # be the reason the next start refuses, while the new run sweeps on.
    procs = []
    sweeper = Sweeper(spawn=_late_then_fed(procs), radio_present=lambda: True)
    sweeper.STOP_WAIT_S = 0.05
    sweeper.start()
    procs[0].feed(FM_LINES[:9])                   # one pass published
    wait_for_rows(sweeper, 1)
    sweeper.set_gain(0.0)
    sweeper.start()
    procs[0].break_pipe(OSError("Input/output error"))
    procs[0].reader.join(5)
    assert sweeper.error is None
    sweeper.stop()


def test_the_running_sweep_still_records_its_own_pipe_failing():
    # The other side of that guard: a failure of the pipe the sweep is reading now is kept.
    procs = []
    sweeper = Sweeper(spawn=_late_then_fed(procs), radio_present=lambda: True)
    sweeper.start()
    procs[0].feed(FM_LINES[:9])                   # one pass published
    wait_for_rows(sweeper, 1)
    procs[0].break_pipe(OSError("Input/output error"))
    procs[0].reader.join(5)
    assert sweeper.error == "Input/output error"
    sweeper.stop()


# --- C29: a reader whose pipe fails, or whose rtl_power dies, publishes no part of a pass -------
# WHY: a pass is published when the next one begins, so the reader always holds the pass rtl_power
# is sweeping. When its own pipe failed (an I/O error, or bytes that do not decode), the reader
# recorded the failure and then published what it held as a row: part of the band, handed out as
# the reading while rtl_power ran on. A death that poll() could not see yet did the same, as a
# clean finish: a dying process's pipe ends before its exit status is ready, and Popen.poll() reads
# None while another thread is reaping it. The held pass is now published only on a confirmed
# clean exit: the pipe ends and rtl_power's exit status, waited for, is 0. An rtl_power that closes
# its pipe and has not exited by the end of that wait is a failure too. Complete means what it
# means in normal running: rtl_power prints a whole pass under one timestamp, lowest hop first (the
# real capture: 8 lines per second), so a pass is known to be whole when the next one begins. A
# pass held at a failure is not known to be whole, and is dropped.

class WaitedProc(FedProc):
    """rtl_power with Popen's wait(): it returns the exit status once the process has exited, and
    otherwise waits up to `timeout` for it, then raises TimeoutExpired. Each timeout passed to it is
    kept in `waits`. close_pipe() ends the pipe and exit() ends the process, each on its own; die()
    does both."""

    def __init__(self):
        super().__init__()
        self.waits = []
        self._exited = threading.Event()
        self._status = None

    def exit(self, code):
        self._status = code
        self.returncode = code
        self._exited.set()

    def close_pipe(self):
        self._lines.put(None)

    def die(self, code):
        self.exit(code)
        self.close_pipe()

    def terminate(self):
        self.terminated = True
        if not self._exited.is_set():
            self.exit(-15)
        self.close_pipe()

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("rtl_power", timeout)
        return self._status


class FailingPipeProc(WaitedProc):
    """rtl_power whose pipe fails: an exception fed to it is raised by the read, as an I/O error or
    bytes that do not decode are. The process itself runs on (poll() is None)."""

    def __init__(self):
        super().__init__()
        self.stdout = self._stream()

    def _stream(self):
        for line in iter(self._lines.get, None):
            if isinstance(line, Exception):
                raise line
            yield line

    def break_pipe(self, error):
        self._lines.put(error)


class UnreapedProc(WaitedProc):
    """rtl_power that has died but is not reaped yet: its pipe has ended, and poll() still reads
    None, as Popen's can for a moment. wait() reaps it and returns its status."""

    def exit(self, code):
        self._status = code
        self._exited.set()

    def wait(self, timeout=None):
        status = super().wait(timeout)
        self.returncode = status
        return status


def _first_then_fed(first, procs):
    """A spawn whose first rtl_power is a `first`; every later start gets a FedProc."""
    def spawn(cmd):
        procs.append(first() if not procs else FedProc())
        return procs[-1]
    return spawn


def _pass_row(lines):
    """The row one whole pass of these lines makes."""
    return merge_chunks([parse_line(line) for line in lines])


def wait_for_reading(sweeper, timeout=5.0):
    """Wait for the running sweep's first row of its own: until then there is no reading."""
    deadline = time.monotonic() + timeout
    while sweeper.reading()[0] is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert sweeper.reading()[0] is not None, f"no reading after {timeout} s"


FM_SPAN = (88_000_000, 108_000_000, 256)


@pytest.mark.parametrize("held", [1, 4, 7], ids=["1-hop", "4-hops", "7-hops"])
def test_a_pipe_that_fails_mid_pass_publishes_no_part_of_it(held):
    # One pass is published as the next begins, `held` hops of the next are held, then the pipe
    # fails. The published pass stays the reading; the hops held are no row.
    procs = []
    sweeper = Sweeper(spawn=_first_then_fed(FailingPipeProc, procs), radio_present=lambda: True)
    sweeper.start()
    procs[0].feed(_fm_pass(0) + _fm_pass(1)[:held])
    wait_for_rows(sweeper, 1)
    procs[0].break_pipe(OSError("Input/output error"))
    sweeper.drain()
    assert _spans(sweeper.rows()) == [FM_SPAN]
    assert sweeper.reading()[0] == _pass_row(_fm_pass(0))
    assert sweeper.error == "Input/output error"  # recorded as before
    assert procs[0].waits == []                   # rtl_power runs on: nothing to wait for
    sweeper.stop()


def test_bytes_that_do_not_decode_publish_no_part_of_the_pass_either():
    # The other failure the reader catches: text that is not UTF-8 raises a ValueError mid-pass.
    undecodable = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    procs = []
    sweeper = Sweeper(spawn=_first_then_fed(FailingPipeProc, procs), radio_present=lambda: True)
    sweeper.start()
    procs[0].feed(_fm_pass(0) + _fm_pass(1)[:4])
    wait_for_rows(sweeper, 1)
    procs[0].break_pipe(undecodable)
    sweeper.drain()
    assert _spans(sweeper.rows()) == [FM_SPAN]
    assert sweeper.error == str(undecodable)
    assert procs[0].waits == []
    sweeper.stop()


def test_a_pass_the_next_had_not_begun_to_close_is_unfinished_at_a_failure():
    # Where "complete" is decided. All eight hops of the second pass are held, but no line of a
    # third has come, so nothing has shown that the second pass ended: at a failure it is dropped,
    # as a death drops the pass it held. The first pass, closed by the second, stands.
    procs = []
    sweeper = Sweeper(spawn=_first_then_fed(FailingPipeProc, procs), radio_present=lambda: True)
    sweeper.start()
    procs[0].feed(_fm_pass(0) + _fm_pass(1))
    wait_for_rows(sweeper, 1)
    procs[0].break_pipe(OSError("Input/output error"))
    sweeper.drain()
    assert sweeper.rows() == [_pass_row(_fm_pass(0))]
    sweeper.stop()


def test_after_its_pipe_failed_the_next_run_s_first_row_is_its_own_whole_pass():
    # rtl_power runs on after its pipe failed, so the sweep restarts at the next gain step, which
    # first reports the failure once.
    procs = []
    sweeper = Sweeper(spawn=_first_then_fed(FailingPipeProc, procs), radio_present=lambda: True)
    sweeper.start()
    procs[0].feed(_fm_pass(0) + _fm_pass(1)[:4])
    wait_for_rows(sweeper, 1)
    procs[0].break_pipe(OSError("Input/output error"))
    sweeper.drain()
    sweeper.set_gain(0.0)
    with pytest.raises(SpectrumUnavailable, match="Input/output error"):
        sweeper.start()
    sweeper.start()
    procs[1].feed(_fm_pass(2) + _fm_pass(0)[:1])  # a whole pass of the new run; the next begins
    wait_for_reading(sweeper)
    assert sweeper.rows() == [_pass_row(_fm_pass(0)), _pass_row(_fm_pass(2))]
    assert sweeper.reading()[0] == _pass_row(_fm_pass(2))
    sweeper.stop()


@pytest.mark.parametrize("first", [WaitedProc, UnreapedProc], ids=["reaped", "not-yet-reaped"])
def test_an_rtl_power_that_dies_mid_pass_publishes_no_part_of_it(first):
    # Its death is recorded and backs off as any death does, and once the back-off has run out the
    # next run's first row is its own whole pass.
    clock = _Clock()
    procs = []
    sweeper = Sweeper(spawn=_first_then_fed(first, procs), radio_present=lambda: True, clock=clock)
    sweeper.start()
    procs[0].feed(_fm_pass(0) + _fm_pass(1)[:4])
    wait_for_rows(sweeper, 1)
    procs[0].die(1)
    sweeper.drain()
    assert sweeper.rows() == [_pass_row(_fm_pass(0))]
    assert sweeper.error == "the sweep stopped (rtl_power status 1)"
    assert not sweeper.running
    assert sweeper.retry_in() is not None
    clock.now += sweeper.retry_in()
    with pytest.raises(SpectrumUnavailable, match="rtl_power status 1"):
        sweeper.start()
    sweeper.start()
    procs[1].feed(_fm_pass(2) + _fm_pass(0)[:1])
    wait_for_reading(sweeper)
    assert sweeper.rows() == [_pass_row(_fm_pass(0)), _pass_row(_fm_pass(2))]
    sweeper.stop()


def test_a_whole_pass_before_a_clean_end_is_still_published():
    # The boundary on the other side: the pipe ends and rtl_power exits 0 after its last pass, as
    # it does when its exit timer (-e) runs out. Read after the reap, that is a clean finish, and
    # the last pass is published, as before.
    procs = []
    sweeper = Sweeper(spawn=_first_then_fed(UnreapedProc, procs), radio_present=lambda: True)
    sweeper.start()
    procs[0].feed(_fm_pass(0) + _fm_pass(1))
    wait_for_rows(sweeper, 1)
    procs[0].die(0)
    sweeper.drain()
    assert sweeper.rows() == [_pass_row(_fm_pass(0)), _pass_row(_fm_pass(1))]
    assert sweeper.error is None


def test_an_rtl_power_that_closes_its_pipe_and_does_not_exit_publishes_nothing_and_is_reported():
    # The pipe ends holding part of a pass, and rtl_power has not exited when the wait for it runs
    # out (a dying process whose close of the radio hangs on a wedged USB device is one way). That
    # is no clean finish: the part pass is dropped, and the reason is recorded with the back-off a
    # death gets, so when rtl_power exits at last the next start reports it before relaunching.
    clock = _Clock()
    procs = []
    sweeper = Sweeper(spawn=_first_then_fed(WaitedProc, procs), radio_present=lambda: True,
                      clock=clock)
    sweeper.STOP_WAIT_S = 0.05
    sweeper.start()
    procs[0].stderr = io.StringIO("Signal caught, exiting!\n")   # what rtl_power said last
    procs[0].feed(_fm_pass(0) + _fm_pass(1)[:4])
    wait_for_rows(sweeper, 1)
    procs[0].close_pipe()
    reason = "rtl_power closed its output but did not exit: Signal caught, exiting!"
    try:
        sweeper.drain()
        assert procs[0].waits == [0.05]           # the reader waited, for STOP_WAIT_S and no more
        assert sweeper.rows() == [_pass_row(_fm_pass(0))]
        assert sweeper.error == reason            # with what rtl_power printed, as a death has
        assert sweeper.retry_in() is not None
    finally:
        procs[0].exit(1)                          # a reader still waiting is let go
    sweeper.drain()
    assert sweeper.error == reason
    assert not sweeper.running
    clock.now += sweeper.retry_in()
    with pytest.raises(SpectrumUnavailable, match="did not exit"):
        sweeper.start()
    sweeper.start()
    procs[1].feed(_fm_pass(2) + _fm_pass(0)[:1])
    wait_for_reading(sweeper)
    assert sweeper.rows() == [_pass_row(_fm_pass(0)), _pass_row(_fm_pass(2))]
    sweeper.stop()


@pytest.mark.parametrize("code, reason, backs_off", [
    (0, "Input/output error", False),
    (1, "the sweep stopped (rtl_power status 1)", True),
], ids=["exited-0", "exited-1"])
def test_a_pipe_that_fails_after_rtl_power_exited_publishes_no_part_of_the_pass(
        code, reason, backs_off):
    # rtl_power has already exited when its pipe fails, so the reader reads its status at once. An
    # exit 0 does not make the pass held whole: the pipe failed, and only the pipe error stands. A
    # non-zero exit is a death, recorded with its back-off, as it was before C29.
    procs = []
    sweeper = Sweeper(spawn=_first_then_fed(FailingPipeProc, procs), radio_present=lambda: True)
    sweeper.start()
    procs[0].feed(_fm_pass(0) + _fm_pass(1)[:4])
    wait_for_rows(sweeper, 1)
    procs[0].exit(code)
    procs[0].break_pipe(OSError("Input/output error"))
    sweeper.drain()
    assert sweeper.rows() == [_pass_row(_fm_pass(0))]
    assert sweeper.error == reason
    assert (sweeper.retry_in() is not None) == backs_off
    assert not sweeper.running


def test_a_sweeper_whose_process_exited_is_not_running():
    sweeper = Sweeper(spawn=lambda cmd: DeadProc([]), radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert not sweeper.running


# Verbatim stderr from `rtl_power -d 9` on the Nomad, captured 2026-08-27. The shape is
# the point: the refusal is on line 4 and rtl_power narrates its configuration anyway,
# so the reason a sweep died is not the last thing it printed.
BAD_DEVICE_STDERR = """Found 1 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001

No matching devices found.
Number of frequency hops: 1
Dongle bandwidth: 1000000Hz
Downsampling by: 1x
Cropping by: 0.00%
Total FFT bins: 16
Logged FFT bins: 16
FFT bin size: 62500.00Hz
Buffer size: 16384 bytes (8.19ms)
Reporting every 10 seconds
"""


def test_a_sweep_that_dies_records_why_it_died():
    sweeper = Sweeper(spawn=lambda cmd: DeadProc([], stderr=BAD_DEVICE_STDERR),
                      radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert "No matching devices" in (sweeper.error or "")


def test_the_reason_survives_the_narration_rtl_power_prints_after_it():
    sweeper = Sweeper(spawn=lambda cmd: DeadProc([], stderr=BAD_DEVICE_STDERR),
                      radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    # Keeping only rtl_power's last line was the bug: it is configuration chatter
    # printed after the refusal, so the fault itself has to survive from four lines up.
    assert "No matching devices found." in sweeper.error


def test_a_sweep_that_died_reports_the_reason_on_the_next_start():
    sweeper = Sweeper(spawn=lambda cmd: DeadProc([], stderr=BAD_DEVICE_STDERR),
                      radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    with pytest.raises(SpectrumUnavailable) as caught:
        sweeper.start()
    assert "No matching devices found." in str(caught.value)


def test_the_reason_is_reported_once_and_then_the_sweep_is_retried():
    spawned = []

    def spawn(cmd):
        proc = DeadProc([], stderr=BAD_DEVICE_STDERR)
        spawned.append(proc)
        return proc

    clock = _Clock()
    sweeper = Sweeper(spawn=spawn, radio_present=lambda: True, clock=clock)
    sweeper.start()
    sweeper.drain()
    # The poll that reports the death lands once the back-off has run out.
    clock.now += sweeper.retry_in()
    with pytest.raises(SpectrumUnavailable):
        sweeper.start()
    # A death must not wedge the console permanently: the poll after the report tries
    # the radio again, because a dongle coming back is what recovery looks like. The
    # count is the evidence — a second process was actually launched.
    sweeper.start()
    assert len(spawned) == 2


def test_a_sweep_that_ends_cleanly_is_not_an_error():
    sweeper = Sweeper(spawn=lambda cmd: DeadProc([LINE_A, LINE_B], returncode=0, stderr=""),
                      radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert sweeper.error is None
    assert len(sweeper.rows()) == 1


def test_a_deliberate_stop_is_not_recorded_as_a_failure():
    # stop() sends SIGTERM, but it sets _interrupted first, before the process is killed. A
    # blocking source keeps the reader in the read loop until then — as the live process does —
    # so the reader's final decision sees the interruption and drops the partial without fault.
    sweeper = Sweeper(spawn=lambda cmd: BlockingProc([LINE_A], returncode=-15),
                      radio_present=lambda: True)
    sweeper.start()
    sweeper.stop()
    assert sweeper.error is None
    # The partial pass is dropped, not published as a full-looking row.
    assert sweeper.rows() == []


# --- C3: a death mid-pass that stop() did NOT cause must not publish a partial ---
# WHY: _read's finally used to publish whatever chunks were in flight for any death but our own
# stop(), and _record_death ignored negative exit codes. So an external SIGTERM/SIGKILL, a USB
# wedge or an OOM kill mid-pass merged a fraction of the band into a full-looking SweepRow served
# as available:true, with no error recorded. A pass is now published only when it ended cleanly.

def test_an_external_signal_kill_mid_pass_drops_the_partial_and_records_it():
    # A signal we did not send (stop() is never called here): the in-flight pass is truncated,
    # so it is dropped rather than served, and the reason is recorded even for a negative exit.
    sweeper = Sweeper(spawn=lambda cmd: DeadProc([LINE_A], returncode=-9, stderr=""),
                      radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert sweeper.rows() == []
    assert "killed mid-pass" in (sweeper.error or "")


def test_an_rtl_power_error_mid_pass_drops_the_partial_and_records_it():
    # A positive error exit partway through a pass: the chunk in hand is a fraction of the band,
    # so it is dropped and rtl_power's reason recorded — not served as a complete row.
    sweeper = Sweeper(spawn=lambda cmd: DeadProc([LINE_A], returncode=1, stderr=BAD_DEVICE_STDERR),
                      radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert sweeper.rows() == []
    assert "No matching devices found." in (sweeper.error or "")


def test_a_finite_source_that_ends_cleanly_still_publishes_its_last_pass():
    # The boundary the fix must not cross: a source that ends on its own (poll() is None) is a
    # clean finish, so its last pass is still published even though it is below the band top.
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A, LINE_B]), radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert len(sweeper.rows()) == 1
    assert sweeper.error is None


def test_sweeper_stop_terminates_the_process():
    proc = FakeProc([LINE_A])
    sweeper = Sweeper(spawn=lambda cmd: proc, radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    sweeper.stop()
    assert proc.terminated is True
    assert sweeper.running is False


class WedgedProc:
    """A hung rtl_power: ignores terminate(), and only kill() frees the dongle."""

    def __init__(self):
        self.stdout = iter(())
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired(cmd="rtl_power", timeout=timeout or 0)
        return 0


def test_sweeper_stop_kills_a_process_that_ignores_terminate():
    proc = WedgedProc()
    sweeper = Sweeper(spawn=lambda cmd: proc, radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    sweeper.stop()
    assert proc.killed is True, "a wedged rtl_power was left holding the dongle"
    assert sweeper.running is False


def test_sweeper_splits_sweeps_that_share_a_timestamp():
    # rtl_power stamps to the second, so two sweeps can carry the same time. The
    # band wrapping back to its start is the reliable signal that a new row began.
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A, LINE_B, LINE_A, LINE_B]),
                      radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    rows = sweeper.rows()
    assert len(rows) == 2
    assert all(len(row.dbm) == 64 for row in rows)


@pytest.mark.parametrize("limit", [0, -5])
def test_sweeper_rows_rejects_a_nonpositive_limit(limit):
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert sweeper.rows() != []
    assert sweeper.rows(limit) == []


# --- tuning -------------------------------------------------------------

def test_default_span_is_the_fm_band():
    low, high, bin_size = validate_span(None, None, None)
    assert (low, high) == (88_000_000, 108_000_000)
    assert bin_size == 125_000


def test_a_valid_span_is_accepted_and_coerced_to_int():
    assert validate_span(144.0e6, 148.0e6, 25_000.0) == (144_000_000, 148_000_000, 25_000)


@pytest.mark.parametrize("low,high", [
    (10_000_000, 20_000_000),      # below what an R828D can reach
    (1_700_000_000, 1_900_000_000),  # above it
])
def test_span_outside_the_tuner_range_is_refused(low, high):
    with pytest.raises(TuneError) as exc:
        validate_span(low, high, None)
    assert "24" in str(exc.value) or "1766" in str(exc.value)


def test_inverted_span_is_refused():
    with pytest.raises(TuneError):
        validate_span(108_000_000, 88_000_000, None)


def test_span_narrower_than_one_bin_is_refused():
    with pytest.raises(TuneError):
        validate_span(100_000_000, 100_010_000, None)


def test_span_too_wide_to_stay_a_waterfall_is_refused():
    # A sweep of many hundreds of MHz takes minutes per row; that is not a waterfall.
    with pytest.raises(TuneError):
        validate_span(24_000_000, 1_000_000_000, None)


def test_bin_size_beyond_what_rtl_power_accepts_is_refused():
    with pytest.raises(TuneError):
        validate_span(88_000_000, 108_000_000, 5_000_000)


@pytest.mark.parametrize("low,high,bin_size", [
    (95_000_000, 97_000_000, 100),           # 20,000 bins in a 2 MHz span
    (24_000_000, 124_000_000, 1),            # 10^8 bins
])
def test_a_span_asking_for_more_bins_than_a_row_can_hold_is_refused(low, high, bin_size):
    with pytest.raises(TuneError) as exc:
        validate_span(low, high, bin_size)
    assert "bins" in str(exc.value)


def test_a_span_at_the_bin_limit_is_accepted():
    assert validate_span(88_000_000, 88_000_000 + MAX_BINS * 1_000, 1_000)


@pytest.mark.parametrize("bad", ["abc", None, float("nan"), float("inf")])
def test_unparseable_span_is_refused_not_guessed(bad):
    with pytest.raises(TuneError):
        validate_span(bad, 108_000_000, None)


def test_retune_applies_the_new_span_and_stands_down():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert sweeper.rows()

    sweeper.retune(144_000_000, 148_000_000, 25_000)
    # Stands down rather than relaunching; the next /api/spectrum poll starts it.
    assert sweeper.running is False
    assert sweeper.low == 144_000_000
    assert sweeper.high == 148_000_000
    assert sweeper.bin_size == 25_000
    assert "144000000:148000000:25000" in " ".join(sweeper.command())


def test_retune_discards_rows_from_the_old_span():
    # Rows carry their own frequency axis; keeping them across a retune would paint
    # two different bands onto one waterfall.
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert sweeper.rows()
    sweeper.retune(144_000_000, 148_000_000, 25_000)
    assert sweeper.rows() == []


# --- releasing the radio ------------------------------------------------

def test_release_stops_the_sweep_and_refuses_to_restart():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    sweeper.release()
    assert sweeper.running is False
    # A poll must not quietly grab the dongle back while rtl_fm is using it.
    with pytest.raises(SpectrumUnavailable) as exc:
        sweeper.start()
    assert "released" in str(exc.value).lower()


def test_resume_allows_the_sweep_again():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=lambda: True)
    sweeper.release()
    sweeper.resume()
    sweeper.start()
    assert sweeper.running is True
    sweeper.stop()


def test_release_is_safe_to_repeat():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=lambda: True)
    sweeper.release()
    sweeper.release()
    assert sweeper.running is False


def test_release_discards_rows_from_before():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert sweeper.rows()
    sweeper.release()
    assert sweeper.rows() == []


# --- lifecycle races ----------------------------------------------------

def test_release_does_not_return_while_a_sweep_is_being_born():
    """release() must not report success while start() is mid-flight.

    /api/spectrum and /api/radio/release are both sync routes, so FastAPI runs them
    on separate threadpool threads. Without a lifecycle lock, release() can pass
    through while start() is still inside its radio probe, and then start() assigns a
    live rtl_power afterwards — the endpoint says "released" and the dongle is held.
    """

    started = threading.Event()

    def slow_present():
        started.set()
        time.sleep(0.3)          # start() sits inside the guarded region
        return True

    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=slow_present)
    worker = threading.Thread(target=sweeper.start, daemon=True)
    worker.start()
    assert started.wait(2), "start() never began"

    sweeper.release()
    worker.join(3)

    assert sweeper.running is False, "release() returned while a sweep was still starting"
    assert sweeper.released is True


def test_two_concurrent_polls_spawn_only_one_sweep():

    spawned = []

    def counting_spawn(cmd):
        spawned.append(cmd)
        return FakeProc([LINE_A])

    sweeper = Sweeper(spawn=counting_spawn, radio_present=lambda: True)
    threads = [threading.Thread(target=sweeper.start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3)

    assert len(spawned) == 1, f"spawned {len(spawned)} rtl_power processes, orphaning some"
    sweeper.stop()


# --- gain ----------------------------------------------------------------

def test_gain_defaults_to_automatic():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([]))
    assert sweeper.gain is None
    assert "-g" not in sweeper.command()


def test_gains_the_tuner_can_do_are_accepted():
    assert validate_gain(None) is None
    assert validate_gain(0) == 0.0
    assert validate_gain(12.5) == 12.5
    assert validate_gain("25.4") == 25.4
    assert validate_gain(49.6) == 49.6


@pytest.mark.parametrize("bad", [-1, 50, float("nan"), float("inf"), "loud", ""])
def test_a_gain_the_tuner_cannot_do_is_refused(bad):
    with pytest.raises(TuneError):
        validate_gain(bad)


def test_command_carries_the_gain():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([]), gain=12.5)
    command = sweeper.command()
    assert command[command.index("-g") + 1] == "12.5"


def test_set_gain_restarts_the_sweep_but_keeps_the_rows():
    procs, argvs = [], []

    def spawn(cmd):
        argvs.append(list(cmd))
        procs.append(FakeProc([LINE_A]))
        return procs[-1]

    sweeper = Sweeper(spawn=spawn, radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
    assert len(sweeper.rows()) == 1

    sweeper.set_gain(25.4)
    assert procs[0].terminated is True
    assert sweeper.running is False
    # Unlike a retune, the frequency axis is unchanged, so history stays: the
    # brightness step between old-gain and new-gain rows is the lesson's observable.
    assert len(sweeper.rows()) == 1

    sweeper.start()
    # The gain must reach the process actually spawned, not just command()'s answer.
    assert argvs[-1][argvs[-1].index("-g") + 1] == "25.4"
    sweeper.drain()
    sweeper.stop()


class StallingProc:
    """Emits its lines, then hangs mid-band until terminated.

    A real rtl_power never ends on its own — it only ever stops by being told to —
    so the reader's final flush always happens on an interrupted pass.
    """

    def __init__(self, lines):
        self._lines = list(lines)
        self._unblock = threading.Event()
        self.terminated = False
        self.stdout = self._stream()

    def _stream(self):
        yield from self._lines
        self._unblock.wait(timeout=5)

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True
        self._unblock.set()

    def wait(self, timeout=None):
        return 0


def test_set_gain_discards_a_sweep_cut_off_mid_band():
    # Two complete sweeps, then a third pass interrupted after its first chunk. The
    # kept history must hold only whole sweeps: a partial row would compute SNR over
    # a fraction of the band and lie about where the band ends.
    sweep2 = [LINE_A.replace("02:40:17", "02:40:19"), LINE_B.replace("02:40:17", "02:40:19")]
    partial = [LINE_A.replace("02:40:17", "02:40:21")]
    proc = StallingProc([LINE_A, LINE_B] + sweep2 + partial)
    sweeper = Sweeper(spawn=lambda cmd: proc, radio_present=lambda: True)
    sweeper.start()

    # Wait until both complete sweeps are published and the reader is stalled mid-band.
    deadline = time.time() + 2
    while len(sweeper.rows()) < 2 and time.time() < deadline:
        time.sleep(0.01)

    sweeper.set_gain(12.5)
    rows = sweeper.rows()
    assert len(rows) == 2, "the interrupted pass leaked into the kept history"
    assert all(row.freq_high == 93_000_000 for row in rows)


def test_gain_zero_is_a_setting_not_automatic():
    # The classic falsy-zero trap: 0 dB is a real gain, not "no opinion".
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([]), gain=0)
    command = sweeper.command()
    assert command[command.index("-g") + 1] == "0.0"


def test_the_constructor_refuses_an_impossible_gain():
    with pytest.raises(TuneError):
        Sweeper(spawn=lambda cmd: FakeProc([]), gain=60)


def test_an_invalid_gain_leaves_the_sweep_untouched():
    proc = FakeProc([LINE_A])
    # Starting from a manual gain, not the default, so "changed nothing" is a real
    # claim rather than an assertion that the default is still the default.
    sweeper = Sweeper(spawn=lambda cmd: proc, radio_present=lambda: True, gain=12.5)
    sweeper.start()
    with pytest.raises(TuneError):
        sweeper.set_gain("banana")
    assert sweeper.running is True
    assert sweeper.gain == 12.5
    sweeper.stop()


# --- the readout numbers -------------------------------------------------

def _row(*dbm, low=88_000_000.0, step=78125.0):
    """A sweep row built directly, for the readout arithmetic alone."""
    return SweepRow(ts="2026-08-24 02:40:17", freq_low=low, freq_high=low + len(dbm) * step,
                    freq_step=step, dbm=tuple(dbm))


def test_row_noise_floor_is_the_median_of_its_bins():
    row = _row(-30.0, -10.0, -31.0, -29.0, -32.0)
    assert row.noise_floor_db() == -30.0


def test_row_peak_names_the_strongest_bin_and_its_frequency():
    row = _row(-30.0, -5.0, -31.0, low=100_000_000.0, step=100.0)
    hz, db = row.peak()
    assert (hz, db) == (100000100.0, -5.0)


def test_row_peak_tie_goes_to_the_first_bin():
    row = _row(-5.0, -31.0, -5.0, low=100_000_000.0, step=100.0)
    assert row.peak()[0] == 100000000.0


def test_row_snr_is_peak_over_floor():
    row = _row(-30.0, -10.0, -31.0, -29.0, -32.0)
    assert row.snr_db() == 20.0


def test_as_dict_carries_the_readout_fields():
    row = _row(-30.0, -10.0, -31.0, -29.0, -32.0)
    flat = row.as_dict()
    assert flat["floor_db"] == -30.0
    assert flat["peak_db"] == -10.0
    assert flat["snr_db"] == 20.0
    assert flat["peak_hz"] == 88_000_000 + 78125.0


# --- a span built around a target: keep the target off every hop's unmeasured centre ----------
# rtl_power cannot measure the bin at each hop's centre (its DC copy, kept as None). A span built
# straight around a target puts the target exactly there on a single-hop span (M9's 433.92 carrier
# vanished into it), and on a hop edge for an even hop count. The sweep is offset instead, so the
# target sits a few measured bins from the centre of the hop that holds it.

def _hops_in_capture(name):
    """(hop centres, bin step) of one real capture, read off its first pass."""
    centres, step, seen = [], None, set()
    for line in _capture(name):
        fields = [f.strip() for f in line.split(",")]
        low, high, step = int(fields[2]), int(fields[3]), float(fields[4])
        if low in seen:
            break
        seen.add(low)
        centres.append((low + high) // 2)
    return centres, step


@pytest.mark.parametrize("name, low, high, bin_size", [
    ("fm-88-108-125k.csv", 88_000_000, 108_000_000, 125_000),
    ("adsb-1089-1091-31k.csv", 1_089_000_000, 1_091_000_000, 31_250),
    ("fm-narrow-95.9-96.3-10k.csv", 95_900_000, 96_300_000, 10_000),
])
def test_the_hop_plan_matches_what_rtl_power_really_swept(name, low, high, bin_size):
    centres, step = _hops_in_capture(name)
    plan = hop_plan(low, high, bin_size)
    assert plan.centres == centres
    assert plan.step == step


def _clearances(low, high, bin_size, target):
    """How far the target sits from the nearest hop centre and from the edges of its own hop."""
    plan = hop_plan(low, high, bin_size)
    nearest = min(plan.centres, key=lambda c: abs(c - target))
    to_centre = abs(target - nearest)
    to_edge = plan.hop_width / 2 - to_centre
    return to_centre, to_edge, plan.step


def test_a_target_on_a_single_hop_centre_moves_a_few_bins_off_it():
    # M9's band: one 2 MHz hop, centred where the carrier is.
    low, high, bin_size = target_span(433_920_000, 2_000_000, 15_625)
    assert high - low == 2_000_000
    to_centre, to_edge, step = _clearances(low, high, bin_size, 433_920_000)
    assert to_centre == SPAN_GUARD_BINS * step
    assert to_edge > SPAN_GUARD_BINS * step


def test_a_target_on_a_hop_edge_moves_near_the_centre_of_a_hop():
    # The FM default: eight 2.5 MHz hops, and 98.0 MHz is the edge between the 4th and 5th.
    low, high, bin_size = target_span(98_000_000, 20_000_000, 125_000)
    assert high - low == 20_000_000
    to_centre, to_edge, step = _clearances(low, high, bin_size, 98_000_000)
    assert to_centre == SPAN_GUARD_BINS * step
    assert to_edge > SPAN_GUARD_BINS * step


def test_a_target_already_clear_of_the_centre_still_lands_the_same_way():
    # A target anywhere is placed the same few bins from the centre of its hop, so a tap on a
    # station always lands it where the hop measures best, not on a roll-off at the hop edge.
    for target in (433_000_000, 433_455_000, 434_010_000):
        low, high, bin_size = target_span(target, 2_000_000, 15_625)
        to_centre, _, step = _clearances(low, high, bin_size, target)
        assert to_centre == SPAN_GUARD_BINS * step


def test_the_offset_span_stays_inside_the_tuner():
    low, high, _ = target_span(25_000_000, 2_000_000, 15_625)
    assert low >= TUNER_MIN_HZ
    low, high, _ = target_span(1_765_000_000, 2_000_000, 15_625)
    assert high <= TUNER_MAX_HZ


def test_giant_bins_have_no_centre_gap_so_the_span_stays_centred():
    # One bin per hop prints no DC copy, so there is nothing to step around.
    assert target_span(100_000_000, 10_000_000, 1_000_000) == (95_000_000, 105_000_000, 1_000_000)


def test_a_span_with_too_few_bins_per_hop_to_offset_stays_centred():
    # Two bins a hop leave no measured bin a guard away from the centre that is not an edge.
    assert target_span(100_000_000, 200_000, 100_000) == (99_900_000, 100_100_000, 100_000)


def test_retune_to_a_target_offsets_the_sweep_and_remembers_the_target():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=lambda: True)
    sweeper.retune_to(433_920_000, 2_000_000, 15_625)
    assert sweeper.target == 433_920_000
    assert (sweeper.low, sweeper.high, sweeper.bin_size) == target_span(
        433_920_000, 2_000_000, 15_625)
    assert (sweeper.low + sweeper.high) // 2 != 433_920_000


def test_a_span_given_by_its_edges_has_no_target():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=lambda: True)
    sweeper.retune_to(433_920_000, 2_000_000, 15_625)
    sweeper.retune(144_000_000, 148_000_000, 25_000)
    assert sweeper.target is None


def test_retune_to_refuses_a_target_whose_span_the_radio_cannot_reach():
    sweeper = Sweeper(spawn=lambda cmd: FakeProc([LINE_A]), radio_present=lambda: True)
    with pytest.raises(TuneError):
        sweeper.retune_to(10_000_000, 2_000_000, 15_625)
    assert sweeper.target is None


# --- a sweep that keeps dying backs off, like every other radio tool ---------------------------

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_sweep_that_keeps_dying_is_not_respawned_on_every_poll():
    spawned = []

    def spawn(cmd):
        proc = DeadProc([], stderr=BAD_DEVICE_STDERR)
        spawned.append(proc)
        return proc

    clock = _Clock()
    sweeper = Sweeper(spawn=spawn, radio_present=lambda: True, clock=clock)
    sweeper.start()
    sweeper.drain()
    for _ in range(5):
        with pytest.raises(SpectrumUnavailable, match="No matching devices found."):
            sweeper.start()
    assert len(spawned) == 1
    assert sweeper.retry_in() > 0


def test_the_back_off_grows_while_the_deaths_keep_coming():
    def spawn(cmd):
        return DeadProc([], stderr=BAD_DEVICE_STDERR)

    clock = _Clock()
    sweeper = Sweeper(spawn=spawn, radio_present=lambda: True, clock=clock)
    waits = []
    for _ in range(3):
        sweeper.start()
        sweeper.drain()
        waits.append(sweeper.retry_in())
        clock.now += sweeper.retry_in()
        with pytest.raises(SpectrumUnavailable):
            sweeper.start()           # the reason is reported once the wait is over, then retried
    assert waits == sorted(waits) and waits[0] < waits[-1]


def test_a_deliberate_stop_starts_no_back_off():
    sweeper = Sweeper(spawn=lambda cmd: BlockingProc([LINE_A], returncode=-15),
                      radio_present=lambda: True, clock=_Clock())
    sweeper.start()
    sweeper.stop()
    assert sweeper.retry_in() is None


# The reader asks whether its run is still current only AFTER the exit wait. A stop that comes
# while the reader waits ends the run and kills the process: the status it then reads (-15) is our
# own stop's, not a death, and must not be recorded or start a back-off. Asking before the wait
# would record it. (The engineering manager's gate check, PR 87.)

def _reader_waiting(proc, timeout=2.0):
    """Wait until the reader is inside its wait for the exit status."""
    deadline = time.monotonic() + timeout
    while not proc.waits and time.monotonic() < deadline:
        time.sleep(0.005)
    assert proc.waits, "the reader never waited for the exit status"


def test_a_stop_during_the_readers_exit_wait_records_nothing():
    procs = []
    sweeper = Sweeper(spawn=_first_then_fed(WaitedProc, procs), radio_present=lambda: True)
    sweeper.STOP_WAIT_S = 1.0
    sweeper.start()
    procs[0].feed(_fm_pass(0) + _fm_pass(1)[:4])
    wait_for_rows(sweeper, 1)
    procs[0].close_pipe()
    _reader_waiting(procs[0])
    t0 = time.monotonic()
    sweeper.stop()
    took = time.monotonic() - t0
    assert sweeper.rows() == [_pass_row(_fm_pass(0))]
    assert sweeper.error is None, sweeper.error
    assert sweeper.retry_in() is None
    assert took < 0.5, took


def test_a_restart_during_the_readers_exit_wait_leaves_the_new_run_alone():
    procs = []
    sweeper = Sweeper(spawn=_first_then_fed(WaitedProc, procs), radio_present=lambda: True)
    sweeper.STOP_WAIT_S = 1.0
    sweeper.start()
    procs[0].feed(_fm_pass(0) + _fm_pass(1)[:4])
    wait_for_rows(sweeper, 1)
    procs[0].close_pipe()
    _reader_waiting(procs[0])
    sweeper.stop()                       # as a gain step or a retune does: stop, then start
    sweeper.start()                      # refused if the old run's end was taken for a death
    assert len(procs) == 2
    procs[1].feed(_fm_pass(2) + _fm_pass(3))
    wait_for_reading(sweeper)
    assert sweeper.reading()[0] == _pass_row(_fm_pass(2))
    assert sweeper.error is None, sweeper.error
    assert sweeper.retry_in() is None


# --- C24: the Restart sweep key ----------------------------------------------------------------
# WHY: an rtl_power that stops sweeping without exiting still reads as running. C25 made the
# waterfall say STALLED, but nothing ever ended that rtl_power: only a gain step, a retune or
# another tool's claim stops a sweep. The owner's ruling is a Restart sweep key, shown only while
# the waterfall says STALLED, and no automatic watchdog. A press stops the stalled rtl_power and
# keeps its rows; the next poll starts a new one. From the press until a run after it publishes a
# row the sweep reads "restarting", and a restarted run that brings no row within the stall limit
# plus a start-up allowance reads "stalled" again, so the key comes back. A press on a sweep that
# is not stalled, or that recovers inside the press, changes nothing.

def _stall(clock, sweeper):
    """A minute with no new pass from an rtl_power that is still alive."""
    clock.now += 60
    assert sweeper.newest()[1] == "stalled"


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), f"not so after {timeout} s"


class StoppedProc(WaitedProc):
    """rtl_power stopped by SIGSTOP: it prints only what is fed, and a SIGTERM stays pending, so
    terminate() changes nothing. kill() ends it (exit -9, pipe closed). `terminate_seen` is set by
    the terminate, so a test can act while the stop is under way."""

    def __init__(self):
        super().__init__()
        self.killed = False
        self.terminate_seen = threading.Event()

    def terminate(self):
        self.terminated = True
        self.terminate_seen.set()

    def kill(self):
        self.killed = True
        self.die(-9)


class HeldProc(StoppedProc):
    """rtl_power stuck in the kernel on a wedged USB call: it survives kill() too, and so still
    holds the dongle. free() ends it, so a test can clean up."""

    def kill(self):
        self.killed = True

    def free(self):
        self.die(-9)


class _HookClock(_Clock):
    """A clock whose next call from the thread that armed it runs a step first, once. The reader's
    own calls (the time a row is published) never run it."""

    def __init__(self):
        super().__init__()
        self._armed = None

    def arm(self, step):
        self._armed = (threading.get_ident(), step)

    def __call__(self):
        armed = self._armed
        if armed is not None and armed[0] == threading.get_ident():
            self._armed = None
            armed[1]()
        return self.now


class _SpyLock:
    """A real lock that runs a step once, just before the arming thread's n-th acquire."""

    def __init__(self):
        self._lock = threading.Lock()
        self._armed = None

    def arm(self, n, step):
        self._armed = [threading.get_ident(), n, step]

    def __enter__(self):
        armed = self._armed
        if armed is not None and armed[0] == threading.get_ident():
            armed[1] -= 1
            if armed[1] == 0:
                self._armed = None
                armed[2]()
        return self._lock.__enter__()

    def __exit__(self, *exc):
        return self._lock.__exit__(*exc)


def _flush_event(sweeper):
    """An event set after every attempt the reader makes to publish a pass."""
    done = threading.Event()
    real = sweeper._flush

    def spy(run, pending):
        try:
            real(run, pending)
        finally:
            done.set()

    sweeper._flush = spy
    return done


def test_restart_stops_a_stalled_sweep_keeps_its_rows_and_the_next_start_is_a_new_run():
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    kept = _spans(sweeper.rows())
    _stall(clock, sweeper)
    age = sweeper.restart()
    assert age > sweeper.stale_after()
    assert procs[0].terminated and not sweeper.running
    assert _spans(sweeper.rows()) == kept          # real rows, kept as a gain step keeps them
    sweeper.start()                                # the next poll
    assert len(procs) == 2 and sweeper.running
    sweeper.stop()


def test_from_the_press_to_the_first_row_after_it_the_sweep_reads_restarting():
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    _stall(clock, sweeper)
    sweeper.restart()
    states = [sweeper.newest()[1]]                 # pressed, not started yet
    sweeper.start()
    states.append(sweeper.newest()[1])             # started, no row yet
    procs[-1].die(1)                               # the new rtl_power dies before its first row
    _wait_until(lambda: sweeper.error is not None)
    states.append(sweeper.newest()[1])
    clock.now += 3                                 # past the 2 s back-off
    with pytest.raises(SpectrumUnavailable):
        sweeper.start()                            # reports the death
    sweeper.start()                                # spawns
    states.append(sweeper.newest()[1])
    procs[-1].feed(FM_LINES[:9])                   # one whole pass; the next begins
    wait_for_rows(sweeper, 3)
    states.append(sweeper.newest()[1])
    assert states == ["restarting"] * 4 + [None]   # never "resuming": swept wholly after the press
    assert sweeper.reading()[0] == sweeper.rows()[-1]
    sweeper.stop()


def test_a_restarted_run_with_no_row_reads_stalled_again_after_its_allowance():
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    _stall(clock, sweeper)
    sweeper.restart()
    sweeper.start()
    bound = sweeper.stale_after() + sweeper.START_ALLOWANCE_S
    clock.now += bound - 0.01
    assert sweeper.newest()[1] == "restarting"
    clock.now += 0.02
    assert sweeper.newest()[1] == "stalled"        # so the key comes back
    sweeper.restart()
    sweeper.start()
    assert len(procs) == 3
    sweeper.stop()


def _not_stalled(case):
    """A sweep in one of the states a press must leave alone, and the state it should name."""
    clock = _Clock()
    if case in ("never", "waiting"):
        procs = []
        sweeper = Sweeper(spawn=_fed(procs), radio_present=lambda: True, clock=clock)
        if case == "waiting":
            sweeper.start()
        return sweeper, procs, {"never": "stopped", "waiting": "waiting"}[case]
    sweeper, procs = _sweeping(clock)
    if case == "live":
        return sweeper, procs, "live"
    _stall(clock, sweeper)
    if case == "resuming":
        procs[-1].feed(_fm_pass(0))                # rtl_power recovers on its own
        wait_for_rows(sweeper, 3)
    elif case == "restarting":
        sweeper.restart()                          # the first press
        sweeper.start()
    elif case == "gain":
        sweeper.set_gain(0.0)
        return sweeper, procs, "stopped"
    elif case == "retune":
        sweeper.retune(88_000_000, 93_000_000, 78_125)
        return sweeper, procs, "stopped"
    elif case == "released":
        sweeper.release()
    elif case == "died":
        procs[-1].die(1)
        _wait_until(lambda: sweeper.error is not None)
        return sweeper, procs, "stopped"
    return sweeper, procs, case


def _snapshot(sweeper, procs):
    return (len(procs), [proc.terminated for proc in procs], _spans(sweeper.rows()),
            sweeper.gain, (sweeper.low, sweeper.high, sweeper.bin_size), sweeper.released,
            sweeper.error, sweeper.retry_in(), sweeper.running)


@pytest.mark.parametrize("case", ["never", "waiting", "live", "resuming", "restarting", "gain",
                                  "retune", "released", "died"])
def test_restart_changes_nothing_unless_the_sweep_is_stalled(case):
    sweeper, procs, state = _not_stalled(case)
    before = _snapshot(sweeper, procs)
    with pytest.raises(spectrum_module.SweepNotStalled) as refused:
        sweeper.restart()
    assert refused.value.state == state
    assert _snapshot(sweeper, procs) == before
    sweeper.stop()


def test_restart_kills_a_stalled_rtl_power_that_ignores_sigterm():
    procs = []
    clock = _Clock()
    sweeper = Sweeper(spawn=_first_then_fed(StoppedProc, procs), radio_present=lambda: True,
                      clock=clock)
    sweeper.STOP_WAIT_S = 0.05
    sweeper.start()
    reader = sweeper._thread
    procs[0].feed(FM_LINES)
    wait_for_rows(sweeper, 2)
    _stall(clock, sweeper)
    sweeper.restart()
    assert (procs[0].terminated, procs[0].killed) == (True, True)
    reader.join(5)
    assert not reader.is_alive()
    assert (sweeper.error, sweeper.retry_in()) == (None, None)   # our own stop is no death
    sweeper.stop()


def test_a_restart_that_cannot_free_the_dongle_is_reported_once_by_the_next_start():
    procs = []
    clock = _Clock()
    sweeper = Sweeper(spawn=_first_then_fed(HeldProc, procs), radio_present=lambda: True,
                      clock=clock)
    sweeper.STOP_WAIT_S = 0.05
    sweeper.start()
    procs[0].feed(FM_LINES)
    wait_for_rows(sweeper, 2)
    _stall(clock, sweeper)
    sweeper.restart()
    assert procs[0].killed
    assert sweeper.error == "rtl_power survived kill; the dongle may still be held"
    with pytest.raises(SpectrumUnavailable, match="survived kill"):
        sweeper.start()
    assert len(procs) == 1                         # nothing spawned into the held dongle then
    sweeper.start()
    assert len(procs) == 2
    assert sweeper.newest()[1] == "restarting"
    procs[0].free()
    sweeper.stop()


def test_a_reader_left_from_the_stalled_run_publishes_nothing_into_the_restarted_one():
    procs = []
    clock = _Clock()
    sweeper = Sweeper(spawn=_late_then_fed(procs), radio_present=lambda: True, clock=clock)
    sweeper.STOP_WAIT_S = 0.05
    sweeper.start()
    procs[0].feed(FM_LINES)                        # two passes published, the third held
    wait_for_rows(sweeper, 2)
    kept = _spans(sweeper.rows())
    _stall(clock, sweeper)
    sweeper.restart()
    assert procs[0].reader.is_alive()              # the old reader outlived the stop
    sweeper.start()
    procs[1].feed(_fm_pass(0)[:4])                 # half of the restarted run's first pass
    procs[0].feed(_fm_pass(1)[:1])                 # the old reader ends its held pass, begins one
    procs[0].close_pipe()
    procs[0].reader.join(5)
    assert _spans(sweeper.rows()) == kept
    procs[1].feed(_fm_pass(0)[4:] + _fm_pass(1)[:1])
    wait_for_rows(sweeper, 3)
    assert _spans(sweeper.rows()) == kept + [FM_SPAN]
    sweeper.stop()


def test_a_release_during_a_slow_restart_leaves_the_radio_released():
    procs = []
    clock = _Clock()
    sweeper = Sweeper(spawn=_first_then_fed(StoppedProc, procs), radio_present=lambda: True,
                      clock=clock)
    sweeper.STOP_WAIT_S = 0.2
    sweeper.start()
    procs[0].feed(FM_LINES)
    wait_for_rows(sweeper, 2)
    _stall(clock, sweeper)
    press = threading.Thread(target=sweeper.restart)
    press.start()
    assert procs[0].terminate_seen.wait(5)         # the press is inside its stop
    claim = threading.Thread(target=sweeper.release)
    claim.start()
    press.join(5)
    claim.join(5)
    assert sweeper.released and not sweeper.running
    with pytest.raises(SpectrumUnavailable, match="released"):
        sweeper.start()
    assert len(procs) == 1


@pytest.mark.parametrize("how", ["gain", "retune"])
def test_a_gain_step_or_retune_during_restarting_ends_it(how):
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    _stall(clock, sweeper)
    sweeper.restart()
    sweeper.start()
    if how == "gain":
        sweeper.set_gain(0.0)
    else:
        sweeper.retune(88_000_000, 93_000_000, 78_125)
    sweeper.start()
    assert sweeper.newest() == (None, None, None)  # the gain step's run, as today
    sweeper.stop()


def test_a_restart_after_a_pipe_failure_reports_it_once_then_starts():
    procs = []
    clock = _Clock()
    sweeper = Sweeper(spawn=_first_then_fed(FailingPipeProc, procs), radio_present=lambda: True,
                      clock=clock)
    sweeper.STOP_WAIT_S = 0.05
    sweeper.start()
    procs[0].feed(FM_LINES)
    wait_for_rows(sweeper, 2)
    procs[0].break_pipe(OSError("Input/output error"))
    sweeper.drain()                                # rtl_power runs on, unread
    _stall(clock, sweeper)
    sweeper.restart()
    assert procs[0].terminated
    with pytest.raises(SpectrumUnavailable, match="Input/output error"):
        sweeper.start()
    sweeper.start()
    assert len(procs) == 2
    assert sweeper.newest()[1] == "restarting"
    procs[1].feed(_fm_pass(0) + _fm_pass(1)[:1])
    wait_for_reading(sweeper)
    assert sweeper.reading()[0] == _pass_row(_fm_pass(0))
    sweeper.stop()


def test_a_press_on_a_sweep_that_recovers_inside_it_changes_nothing():
    # The reader takes only the row lock, and newest() lets go of it before it judges the age: a
    # pass can be published after the judgement. The clock newest() reads then runs the step.
    clock = _HookClock()
    sweeper, procs = _sweeping(clock)
    _stall(clock, sweeper)

    def recover():
        procs[0].feed(_fm_pass(0))                 # rtl_power resumes; its held pass is published
        wait_for_rows(sweeper, 3)

    clock.arm(recover)
    with pytest.raises(spectrum_module.SweepNotStalled) as refused:
        sweeper.restart()
    assert refused.value.state == "resuming"
    assert (procs[0].terminated, len(procs), sweeper.running) == (False, 1, True)
    assert len(sweeper.rows()) == 3
    sweeper.stop()


def test_a_pass_that_completes_after_the_recheck_is_not_published():
    # The press has re-checked the newest row and committed to stopping this run. A pass completed
    # now must not be published. The row lock is swapped for one that runs the step just before the
    # pressing thread's 4th acquire: after the judged entry, newest() and the re-check.
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    sweeper.STOP_WAIT_S = 0.05
    _stall(clock, sweeper)
    flushed = _flush_event(sweeper)
    spy = _SpyLock()
    sweeper._lock = spy
    ran = []

    def step():
        ran.append(1)
        assert sweeper._proc is not None, "the step ran after the stop began"
        flushed.clear()
        procs[0].feed(_fm_pass(0))                 # the held pass completes as the next begins
        assert flushed.wait(5)

    spy.arm(4, step)
    sweeper.restart()
    assert ran == [1]
    assert len(sweeper.rows()) == 2, "a row of the stopped run was published after the re-check"
    assert procs[0].terminated
    sweeper.stop()


def test_the_restart_flag_dies_with_the_first_row_after_it():
    clock = _Clock()
    sweeper, procs = _sweeping(clock)
    _stall(clock, sweeper)
    sweeper.restart()
    sweeper.start()
    procs[-1].feed(FM_LINES[:9])
    wait_for_rows(sweeper, 3)
    assert sweeper.newest()[1] is None
    procs[-1].die(1)                               # later, with no press, rtl_power dies
    _wait_until(lambda: sweeper.error is not None)
    assert sweeper.newest() == (None, None, None)
    clock.now += 3
    with pytest.raises(SpectrumUnavailable):
        sweeper.start()
    sweeper.start()
    assert len(procs) == 3
    assert sweeper.newest() == (None, None, None)  # an ordinary new run, not "restarting"
    sweeper.stop()


def test_the_stopped_runs_reader_publishes_nothing_before_the_next_start():
    procs = []
    clock = _Clock()
    sweeper = Sweeper(spawn=_late_then_fed(procs), radio_present=lambda: True, clock=clock)
    sweeper.STOP_WAIT_S = 0.05
    sweeper.start()
    procs[0].feed(FM_LINES)
    wait_for_rows(sweeper, 2)
    kept = _spans(sweeper.rows())
    _stall(clock, sweeper)
    sweeper.restart()
    procs[0].feed(_fm_pass(1)[:1])                 # its held pass ends before any new start
    procs[0].close_pipe()
    procs[0].reader.join(5)
    assert _spans(sweeper.rows()) == kept
    assert sweeper.newest() == (None, "restarting", None)
    sweeper.stop()


def test_a_restart_after_an_exit_that_does_not_come_waits_out_its_back_off():
    procs = []
    clock = _Clock()
    sweeper = Sweeper(spawn=_first_then_fed(WaitedProc, procs), radio_present=lambda: True,
                      clock=clock)
    sweeper.STOP_WAIT_S = 0.05
    sweeper.start()
    procs[0].feed(FM_LINES)
    wait_for_rows(sweeper, 2)
    clock.now += 60
    procs[0].close_pipe()                          # output closed, and no exit comes
    _wait_until(lambda: sweeper.error is not None)
    assert sweeper.error.startswith("rtl_power closed its output but did not exit")
    assert sweeper.retry_in() == pytest.approx(2.0)
    assert sweeper.newest()[1] == "stalled"
    sweeper.restart()
    assert procs[0].terminated
    with pytest.raises(SpectrumUnavailable, match="did not exit"):
        sweeper.start()                            # the back-off runs: reported, and kept
    clock.now += 2.1
    with pytest.raises(SpectrumUnavailable, match="did not exit"):
        sweeper.start()                            # run out: reported once more, and cleared
    sweeper.start()
    assert len(procs) == 2
    assert sweeper.newest()[1] == "restarting"
    sweeper.stop()
