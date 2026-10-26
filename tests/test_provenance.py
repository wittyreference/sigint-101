# ABOUTME: Tests the provenance instrument — the radio describing itself, and the pre-TX baseline.
# ABOUTME: Temp/throttled sources are injected so the readings are deterministic off the Pi.

from sigint101.provenance import (
    REFERENCE_HZ,
    compare_to_baseline,
    gather_provenance,
    read_baseline,
    read_soc_temp_c,
    read_throttled,
    record_baseline_sample,
    reference_level_db,
)
from sigint101.spectrum import SweepRow


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


# --- SoC temperature ----------------------------------------------------

def test_soc_temp_is_millidegrees_over_a_thousand():
    # /sys/class/thermal reports milli-degrees C: 42355 means 42.355 C.
    assert read_soc_temp_c(read=lambda _p: "42355\n") == 42.355


def test_soc_temp_is_none_when_unreadable():
    # Observational, not a gate: a missing sensor reports "unknown", it does not crash.
    def boom(_p):
        raise OSError("no such file")

    assert read_soc_temp_c(read=boom) is None


# --- throttling / power health -----------------------------------------

def test_throttled_all_clear_is_ok():
    result = read_throttled(which=lambda _: "/usr/bin/vcgencmd",
                            run=lambda *a, **k: _Result("throttled=0x0\n"))
    assert result["ok"] is True
    assert result["flags"] == []
    assert result["raw"] == "0x0"


def test_throttled_decodes_since_boot_and_now_bits():
    # 0x50005: bits 0 (under-voltage now), 2 (throttled now), 16 (under-voltage since boot),
    # 18 (throttled since boot). ok is false whenever any bit is set.
    result = read_throttled(which=lambda _: "/usr/bin/vcgencmd",
                            run=lambda *a, **k: _Result("throttled=0x50005\n"))
    assert result["ok"] is False
    assert result["any_now"] is True
    assert result["any_since_boot"] is True
    joined = " ".join(result["flags"]).lower()
    assert "under-voltage" in joined and "throttl" in joined


def test_throttled_is_none_without_vcgencmd():
    assert read_throttled(which=lambda _: None) is None


# --- the 106.5 MHz reference level --------------------------------------

def _row(low=88_000_000, high=108_000_000, step=1_000_000):
    # dbm[i] = -100 + i, so each bin is distinguishable and the bin index is provable.
    n = int((high - low) / step) + 1
    return SweepRow(ts="t", freq_low=low, freq_high=high, freq_step=step,
                    dbm=tuple(-100.0 + i for i in range(n)))


def test_reference_level_reads_the_bin_at_the_reference_frequency():
    row = _row()
    # 106 MHz is bin 18 exactly; dbm[18] == -82.
    assert reference_level_db(row, hz=106_000_000) == -82.0


def test_reference_level_is_in_band_for_the_default_reference():
    # 106.5 MHz sits inside 88-108, so the default reference resolves to a real bin.
    assert reference_level_db(_row(), hz=REFERENCE_HZ) is not None


def test_reference_level_is_none_outside_the_band():
    # The reference must be honestly absent when the sweep does not cover it, never guessed.
    assert reference_level_db(_row(), hz=200_000_000) is None


# --- assembling the provenance snapshot ---------------------------------

def test_gather_provenance_carries_the_radios_own_account_of_itself():
    snapshot = gather_provenance(
        serial="00000001", gain=12.5, row=_row(),
        temp=lambda: 42.3,
        throttled=lambda: {"raw": "0x0", "ok": True, "flags": [],
                           "any_now": False, "any_since_boot": False},
    )
    assert snapshot["serial"] == "00000001"
    assert snapshot["gain"] == 12.5
    assert snapshot["soc_temp_c"] == 42.3
    assert snapshot["throttled"]["ok"] is True
    assert snapshot["ref_hz"] == REFERENCE_HZ
    assert snapshot["ref_db"] is not None
    # The internal SDR has no bias tee — provenance says so plainly rather than omitting it.
    assert "bias tee" in snapshot["bias_state"]


def test_gather_provenance_reports_missing_pieces_as_none_not_zero():
    snapshot = gather_provenance(
        serial=None, gain=None, row=None,
        temp=lambda: None,
        throttled=lambda: None,
    )
    assert snapshot["soc_temp_c"] is None
    assert snapshot["throttled"] is None
    # No row means no reference reading — absent, not a fabricated 0 dB.
    assert snapshot["ref_db"] is None


# --- the baseline: record, read back, compare ---------------------------

def test_a_recorded_baseline_sample_reads_back(tmp_path):
    events = tmp_path / "events.jsonl"
    snapshot = {"serial": "00000001", "ref_hz": REFERENCE_HZ, "ref_db": -42.0,
                "soc_temp_c": 42.3}
    record_baseline_sample(events, snapshot)
    samples = read_baseline(events)
    assert len(samples) == 1
    assert samples[0]["ref_db"] == -42.0


def test_compare_reports_no_baseline_before_any_sample(tmp_path):
    verdict = compare_to_baseline(-42.0, [])
    assert verdict["has_baseline"] is False


def test_compare_flags_a_reference_that_has_dropped(tmp_path):
    # Three baseline samples near -42 dB; a current reading 10 dB down is a deaf front end.
    samples = [{"ref_db": -42.0}, {"ref_db": -43.0}, {"ref_db": -41.0}]
    verdict = compare_to_baseline(-53.0, samples, drop_threshold_db=6.0)
    assert verdict["has_baseline"] is True
    assert verdict["n_samples"] == 3
    assert verdict["baseline_ref_db"] == -42.0  # median of the three
    assert verdict["degraded"] is True
    assert verdict["delta_db"] < 0


def test_compare_does_not_flag_a_reference_within_the_threshold(tmp_path):
    samples = [{"ref_db": -42.0}, {"ref_db": -43.0}, {"ref_db": -41.0}]
    verdict = compare_to_baseline(-44.0, samples, drop_threshold_db=6.0)
    assert verdict["degraded"] is False
