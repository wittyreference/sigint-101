# ABOUTME: Unit tests for the rtl_power sweep parser and the live Sweeper.
# ABOUTME: No radio means no data — the Sweeper reports absence, it never invents a signal.

import io
import subprocess
import threading
import time
from pathlib import Path

import pytest

from sigint101.spectrum import (
    MAX_BINS,
    SpectrumUnavailable,
    Sweeper,
    SweepRow,
    TuneError,
    merge_chunks,
    parse_line,
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

    sweeper = Sweeper(spawn=spawn, radio_present=lambda: True)
    sweeper.start()
    sweeper.drain()
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
