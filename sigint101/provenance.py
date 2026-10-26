# ABOUTME: The provenance instrument — the radio's account of itself, and the pre-TX baseline.
# ABOUTME: Observational, not a gate: every reading it cannot take is None, never faked as 0.

from __future__ import annotations

import shutil
import statistics
import subprocess

from .events import log_event, read_events

# The reference station. 106.5 MHz is the one broadcast signal that reproduces across sessions
# in this rural spot (104.9 swung 32 dB between nights; 106.5 held) — so it is the fixed thing
# a receiver's health can be measured against. A front end that goes deaf shows up as this
# level dropping while the transmitter has not moved.
REFERENCE_HZ = 106_500_000

# Where the Pi exposes its SoC temperature, in milli-degrees Celsius.
THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"

# The internal SDR is a generic RTL2832U with no bias tee — it cannot inject DC onto the coax
# at all. Provenance states this rather than leaving it blank, so a reader never wonders whether
# a bias voltage is silently powering (or frying) something on the port.
BIAS_STATE = "off — internal SDR has no bias tee"

# `vcgencmd get_throttled` returns a bitfield. The low nibble is "happening right now", the bits
# at 16+ are "has happened since boot". Naming them is what turns 0x50005 into an answer a learner
# (or a future degradation check) can act on rather than a hex value to look up every time.
_THROTTLED_BITS = {
    0: ("under-voltage now", "now"),
    1: ("arm frequency capped now", "now"),
    2: ("throttled now", "now"),
    3: ("soft temperature limit now", "now"),
    16: ("under-voltage since boot", "boot"),
    17: ("arm frequency cap since boot", "boot"),
    18: ("throttled since boot", "boot"),
    19: ("soft temperature limit since boot", "boot"),
}


def _default_read(path) -> str:
    """Read a sysfs file's text. Isolated so tests can substitute the file contents."""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_soc_temp_c(*, path=THERMAL_PATH, read=_default_read):
    """The Pi's SoC temperature in degrees Celsius, or None if it cannot be read.

    sysfs reports milli-degrees, so the raw integer is divided by 1000. Returns None rather
    than raising on any read failure, because provenance observes the radio's state — a missing
    sensor is a thing to report, not a reason to take the console down.
    """
    try:
        raw = read(path)
    except OSError:
        return None
    try:
        return int(raw.strip()) / 1000.0
    except (ValueError, TypeError):
        return None


def read_throttled(*, which=shutil.which, run=subprocess.run):
    """Decode `vcgencmd get_throttled` into a health verdict, or None if vcgencmd is absent.

    `ok` is true only when the whole field is zero. `flags` names each set bit, and `any_now` /
    `any_since_boot` split "power is sagging right now" from "power sagged at some point" —
    a live under-voltage is a different problem from one that happened during boot.
    """
    if not which("vcgencmd"):
        return None
    try:
        result = run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return None
    text = (getattr(result, "stdout", "") or "").strip()
    # Output is "throttled=0x0"; take whatever follows the '='.
    raw_hex = text.split("=", 1)[1].strip() if "=" in text else text
    try:
        value = int(raw_hex, 16)
    except ValueError:
        return None
    flags, any_now, any_since_boot = [], False, False
    # Walk the known bits; a set bit adds its label and marks whether the condition is live
    # ("now") or historical ("boot"), which the caller reports differently.
    for bit, (label, when) in _THROTTLED_BITS.items():
        if value & (1 << bit):
            flags.append(label)
            if when == "now":
                any_now = True
            else:
                any_since_boot = True
    return {"raw": raw_hex, "ok": value == 0, "flags": flags,
            "any_now": any_now, "any_since_boot": any_since_boot}


def reference_level_db(row, hz: int = REFERENCE_HZ):
    """The measured power at the reference frequency in a sweep row, or None if out of band.

    Picks the bin whose centre is nearest `hz`. Returns None when the sweep does not cover the
    reference — a reference the sweep never looked at must be absent, not a guessed value, or the
    baseline would compare against a number the hardware never produced.
    """
    if row is None or not row.dbm:
        return None
    if hz < row.freq_low or hz > row.freq_high:
        return None
    if row.freq_step <= 0:
        return None
    index = round((hz - row.freq_low) / row.freq_step)
    index = max(0, min(index, len(row.dbm) - 1))
    return row.dbm[index]


def gather_provenance(*, serial, gain, row=None,
                      temp=read_soc_temp_c, throttled=read_throttled):
    """Assemble the radio's account of itself into one snapshot.

    Pulls the settings the console already knows (serial, gain) together with what only the
    hardware can say right now (temperature, power health, the reference level). Anything that
    cannot be read is None in the snapshot, so a gap reads as "unknown" rather than a plausible
    zero that a later comparison would trust.
    """
    return {
        "serial": serial or None,
        "gain": gain,
        "bias_state": BIAS_STATE,
        "soc_temp_c": temp(),
        "throttled": throttled(),
        "ref_hz": REFERENCE_HZ,
        "ref_db": reference_level_db(row, REFERENCE_HZ) if row is not None else None,
    }


def record_baseline_sample(events_path, provenance: dict) -> dict:
    """Append one baseline sample to the event log and return the event.

    The baseline is append-only like everything else in sigint101.events: each sample is one line,
    taken while the front end is believed healthy, so a later reading can be measured against it.
    """
    return log_event(events_path, "baseline_sample", provenance)


def read_baseline(events_path) -> list:
    """Every baseline sample recorded so far, oldest first."""
    return [event["payload"] for event in read_events(events_path)
            if event.get("kind") == "baseline_sample"]


def compare_to_baseline(current_ref_db, samples, *, drop_threshold_db: float = 6.0) -> dict:
    """Compare a current reference level against the recorded baseline.

    `drop_threshold_db` is deliberately a parameter, not a constant: the honest threshold is the
    spread the baseline itself shows across its samples (which is why the baseline is taken more
    than once), so the caller sets it from the data rather than this module guessing. `degraded`
    means the reference has fallen further than that threshold below the baseline median — the
    signature of a front end that has gone deaf while the transmitter has not moved.
    """
    levels = [s["ref_db"] for s in samples if s.get("ref_db") is not None]
    if not levels:
        return {"has_baseline": False}
    baseline = statistics.median(levels)
    delta = None if current_ref_db is None else current_ref_db - baseline
    degraded = delta is not None and delta < -abs(drop_threshold_db)
    return {
        "has_baseline": True,
        "n_samples": len(levels),
        "baseline_ref_db": baseline,
        "current_ref_db": current_ref_db,
        "delta_db": delta,
        "degraded": degraded,
        "drop_threshold_db": abs(drop_threshold_db),
    }
