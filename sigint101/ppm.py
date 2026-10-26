# ABOUTME: The frequency-error instrument — how far this receiver's crystal is from the dial.
# ABOUTME: Measures against two independent references; it never averages a disagreement away.

from __future__ import annotations

import cmath
import ctypes
import math
import re
import subprocess
from array import array

from .provenance import read_soc_temp_c

# The reference tone. Every stereo FM broadcast carries a 19 kHz pilot, and 47 CFR 73.322 holds
# it to 19 kHz +/- 2 Hz — so the transmitter is a frequency standard nobody here had to buy.
PILOT_HZ = 19_000.0

# rtl_fm -M fm -s 200000, with no -r and no de-emphasis. `-M wbfm` is the tempting shorthand and
# it is wrong here: it resamples to 32 kHz and de-emphasises, which erases the pilot before it
# can be measured.
DEMOD_RATE = 200_000

# 10 ms. 19 kHz is exactly 190 whole cycles in 2000 samples at this rate, so the Goertzel below
# sits on a real DFT bin and successive blocks share one phase reference with no derotation.
BLOCK = 2_000

# 1/T at 19 kHz is 52.63/seconds ppm, so one second resolves no better than 52.6 ppm — coarser
# than the error being measured. Twenty seconds gives 2.6 ppm, which is fine enough to be a
# measurement rather than a hint.
DEFAULT_SECONDS = 20.0

# +/-400 ppm is +/-7.6 Hz at the pilot. Wide enough for a bad crystal — this board measured
# +164.53 ppm on 2026-09-05 — and narrow enough to scan densely.
SEARCH_PPM = 400.0

# At 12.5 dB even a known-strong station failed to demodulate here (the 2026-08 antenna
# measurements), so the pilot needs real gain in front of it.
DEFAULT_GAIN = 40.0

# The pilot must stand this far above its own shoulders to count as present. The gating run
# measured 29.6-33.1 dB on a good station, against roughly 9 dB where there is no pilot at all.
MIN_PILOT_DB = 15.0

# Linux adjtimex reports the kernel's frequency correction in units of 2^-16 ppm, and sets
# STA_UNSYNC when nothing is disciplining the clock.
_PPM_SCALE = 65_536.0
_STA_UNSYNC = 0x0040


class PpmError(ValueError):
    """The requested measurement is not one this instrument can honestly make."""


def goertzel(samples, k, n) -> complex:
    """Energy and phase at one frequency bin of one block, without computing the other n-1.

    The question here is "how much of exactly this frequency is present", not "what does the
    whole spectrum look like" — so a full FFT would compute a million bins to read one. k is the
    bin index (frequency * n / rate); keeping it whole is what lets blocks be compared directly.
    """
    w = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s1, s2 = x + coeff * s1 - s2, s1
    return complex(s1 - s2 * math.cos(w), s2 * math.sin(w))


def magnitude_at(samples, hz, rate=DEMOD_RATE) -> float:
    """How much energy sits at one arbitrary frequency, for the is-there-a-pilot-at-all check."""
    n = len(samples)
    return abs(goertzel(samples, hz * n / rate, n))


def block_phasors(stream, *, hz=PILOT_HZ, rate=DEMOD_RATE, block=BLOCK, max_blocks=None):
    """One complex number per block: how much of hz it held, and in what phase.

    Streams deliberately. A twenty-second capture is eight megabytes on the wire, and this holds
    only the phasors — about thirty kilobytes — so the console never buffers the audio at all.

    Also returns one settled second of raw samples for the presence check. Not the opening
    second: rtl_fm's first blocks are the tuner and the AGC settling rather than the station, and
    a ratio taken across them says nothing about what is on the air.

    max_blocks is not optional in practice. rtl_fm streams until something stops it and never
    reports an end, so a reader that waits for a short read waits forever.
    """
    k = hz * block / rate
    if abs(k - round(k)) > 1e-9:
        raise PpmError(
            f"{hz} Hz is not a whole number of cycles in {block} samples at {rate} Hz — "
            "successive blocks would not share a phase reference")
    k = round(k)
    # Half a second skipped, then one second kept, for the is-there-a-pilot check further down.
    skip, keep = int(0.5 * rate / block), int(1.0 * rate / block)
    phasors, probe, n = [], array("h"), 0
    while max_blocks is None or n < max_blocks:
        # A short read is the only end a finite source signals; rtl_fm never gives one, which is
        # what max_blocks is for.
        raw = stream.read(block * 2)
        if len(raw) < block * 2:
            break
        # rtl_fm emits signed 16-bit little-endian mono, which is exactly array('h') here.
        buf = array("h")
        buf.frombytes(raw)
        if skip <= n < skip + keep:
            probe.extend(buf)
        n += 1
        # memoryview, not indexing: iterating the view runs this loop about three times faster
        # on a Pi 4 for identical arithmetic, and this is the hot path.
        phasors.append(goertzel(memoryview(buf), k, block))
    return phasors, (probe if len(probe) else None)


def offset_hz_from_phasors(phasors, *, block_rate, search_ppm=SEARCH_PPM, step_ppm=0.25):
    """Where the pilot actually sat, and how far its peak stood above the rest of the scan.

    The block phasors are themselves a slow complex tone rotating at the pilot's own offset, so
    finding the offset is one more single-frequency search — cheap over a few thousand points.
    """
    if not phasors:
        raise PpmError("no blocks to search")
    # The window is expressed in ppm and converted here, because ppm is what the crystal spec
    # and the search range are both stated in, while the arithmetic needs hertz.
    span = search_ppm * PILOT_HZ / 1e6
    step = step_ppm * PILOT_HZ / 1e6
    best, best_mag, mags = 0.0, -1.0, []
    for i in range(int(2 * span / step) + 1):
        # Derotate the phasor sequence by a candidate offset and sum: the candidate that stops
        # the rotation is the one the pilot is actually sitting at.
        d = -span + i * step
        w = -2.0j * math.pi * d / block_rate
        mag = abs(sum(p * cmath.exp(w * j) for j, p in enumerate(phasors)))
        mags.append(mag)
        if mag > best_mag:
            best_mag, best = mag, d
    # Prominence against the MEDIAN of the scan, not its mean: a real pilot leaves most
    # candidates at the noise level, and a mean would be dragged up by the peak it is judging.
    mags.sort()
    median = mags[len(mags) // 2]
    prominence = 20 * math.log10(best_mag / median) if median > 0 else float("inf")
    return best, prominence


def ppm_from_apparent(apparent_hz, nominal=PILOT_HZ) -> float:
    """Turn the pilot's apparent frequency into the crystal's fractional error.

    One crystal clocks both the tuner and the sample rate, so samples arriving at Rn(1+x) but
    read as Rn stretch assumed time: a true 19 kHz tone lands at 19000/(1+x). A fast crystal
    therefore drops the tone BELOW nominal, while pushing the frequency actually received above
    what the dial says.
    """
    if apparent_hz <= 0:
        raise PpmError("an apparent frequency of zero or less is not a measurement")
    return (nominal / apparent_hz - 1.0) * 1e6


def pilot_ratio_db(samples, apparent_hz, rate=DEMOD_RATE) -> float:
    """Is there a pilot at all? Its energy against its shoulders, measured where it actually is.

    This must be asked at the apparent frequency, not at 19 kHz. Over a one-second window the
    bins are 1 Hz wide and this receiver puts the pilot about 3 Hz low, so asking at 19 kHz
    measures the pilot's skirt and reports a strong station as absent. You cannot ask whether
    there is energy at 19 kHz until you know where your own radio puts 19 kHz.
    """
    scale = apparent_hz / PILOT_HZ
    shoulders = (17000, 17500, 18000, 18500, 19500, 20000, 20500, 21000)
    peak = magnitude_at(samples, apparent_hz, rate)
    ref = sum(magnitude_at(samples, f * scale, rate) for f in shoulders) / len(shoulders)
    return 20 * math.log10(peak / ref) if ref > 0 else float("inf")


def resolution_ppm(seconds) -> float:
    """The finest difference a capture of this length can distinguish, in ppm.

    Reported with every measurement, because a number without its resolution is a claim rather
    than a reading — and at 19 kHz a one-second capture cannot tell 30 ppm from 80 ppm.
    """
    if seconds <= 0:
        raise PpmError("a capture of no length resolves nothing")
    return 1e6 / (PILOT_HZ * seconds)


def _default_capture(*, hz, seconds, gain, device=None):
    """Demodulate one station to a raw sample stream with rtl_fm.

    Only one process may hold an RTL-SDR, so the caller is responsible for having asked the
    console to stand down first and for handing the radio back afterwards.
    """
    cmd = ["rtl_fm", "-f", str(int(hz)), "-M", "fm", "-s", str(DEMOD_RATE), "-g", str(gain), "-"]
    if device is not None:
        cmd[1:1] = ["-d", str(device)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return _CaptureStream(proc)


class _CaptureStream:
    """rtl_fm's output, with an off switch.

    rtl_fm holds the dongle until it is killed, so closing the pipe is not enough — the process
    has to go, or the console cannot take the radio back. Closing is idempotent because the
    caller closes in a finally block that also runs on the paths that already closed.
    """

    def __init__(self, proc):
        self._proc = proc

    def read(self, n):
        """Read one chunk of raw samples from the running rtl_fm."""
        return self._proc.stdout.read(n)

    def close(self):
        """Stop rtl_fm and release the dongle, whatever the measurement decided."""
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._proc.stdout:
            self._proc.stdout.close()


def measure_pilot_ppm(*, hz, seconds=DEFAULT_SECONDS, gain=DEFAULT_GAIN, device=None,
                      capture=_default_capture, temp=read_soc_temp_c,
                      min_pilot_db=MIN_PILOT_DB) -> dict:
    """Measure this receiver's crystal error against a broadcast stereo pilot.

    Returns ppm=None with a reason whenever the pilot is not there to be measured — a mono
    station carries none by design, and one too weak tonight reads identically. A measurement
    without its reference is not a measurement.
    """
    wanted = int(seconds * DEMOD_RATE / BLOCK)
    stream = capture(hz=hz, seconds=seconds, gain=gain, device=device)
    try:
        phasors, probe = block_phasors(stream, max_blocks=wanted)
    finally:
        # Always, including on a refusal: whatever is producing these samples is holding the
        # radio, and the console cannot have it back until that stops.
        close = getattr(stream, "close", None)
        if close:
            close()
    base = {"hz": hz, "gain": gain, "soc_temp_c": temp() if temp else None}
    if probe is None or len(phasors) < wanted // 2:
        return {**base, "ppm": None, "seconds": len(phasors) * BLOCK / DEMOD_RATE,
                "reason": "the capture ended before enough audio arrived to measure"}
    phasors = phasors[:wanted]

    # Find where the pilot sits BEFORE asking whether it is there: the presence check has to be
    # made at the frequency this receiver puts it, not at the nominal one.
    offset, prominence = offset_hz_from_phasors(phasors, block_rate=DEMOD_RATE / BLOCK)
    apparent = PILOT_HZ + offset
    ratio = pilot_ratio_db(probe, apparent)
    captured = len(phasors) * BLOCK / DEMOD_RATE
    base = {**base, "apparent_hz": apparent, "pilot_ratio_db": round(ratio, 2),
            "prominence_db": round(prominence, 2), "seconds": captured,
            "resolution_ppm": round(resolution_ppm(captured), 3)}
    if ratio < min_pilot_db:
        return {**base, "ppm": None,
                "reason": f"no 19 kHz pilot here ({ratio:.1f} dB above its shoulders) — "
                          "a mono station carries none, and a weak one reads the same"}
    return {**base, "ppm": round(ppm_from_apparent(apparent), 2), "reason": ""}


def parse_rtl_test(output) -> dict:
    """Read rtl_test -p's cumulative figure, and say whether it had settled.

    It counts delivered samples against the host clock, so it shares no equipment and no
    assumptions with the pilot. Its own message is "press ^C after a few minutes", which is not
    a criterion — so convergence is decided here, from whether the last three readings agree.
    """
    readings = [int(m) for m in re.findall(r"cumulative PPM:\s*(-?\d+)", output)]
    if not readings:
        return {"ppm": None, "readings": [], "converged": False,
                "reason": "rtl_test printed no cumulative PPM figure"}
    tail = readings[-3:]
    converged = len(tail) == 3 and max(tail) - min(tail) <= 2
    return {"ppm": readings[-1], "readings": readings, "converged": converged,
            "reason": "" if converged else "the reading was still moving when it stopped"}


def _adjtimex():
    """Ask the kernel for its clock status and frequency correction. Returns (status, freq)."""
    class Timex(ctypes.Structure):
        """The kernel's struct timex, laid out field for field so ctypes can read freq."""

        _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long),
                    ("freq", ctypes.c_long), ("maxerror", ctypes.c_long),
                    ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                    ("constant", ctypes.c_long), ("precision", ctypes.c_long),
                    ("tolerance", ctypes.c_long), ("time_sec", ctypes.c_long),
                    ("time_usec", ctypes.c_long), ("tick", ctypes.c_long),
                    ("ppsfreq", ctypes.c_long), ("jitter", ctypes.c_long),
                    ("shift", ctypes.c_int), ("stabil", ctypes.c_long),
                    ("jitcnt", ctypes.c_long), ("calcnt", ctypes.c_long),
                    ("errcnt", ctypes.c_long), ("stbcnt", ctypes.c_long),
                    ("tai", ctypes.c_int), ("padding", ctypes.c_int * 11)]
    buf = Timex()
    buf.modes = 0  # a pure query: modes=0 reads without adjusting anything
    ctypes.CDLL("libc.so.6", use_errno=True).adjtimex(ctypes.byref(buf))
    return buf.status, buf.freq


def read_host_clock_ppm(*, read_timex=_adjtimex):
    """What the Pi admits about its own crystal, and whether anything is disciplining it.

    rtl_test measures the dongle against this clock, so this is the reference's reference. An
    unsynchronised host makes that whole comparison a guess, which is why the flag travels with
    the number. Returns None off Linux or on any failure — never a fabricated zero.
    """
    try:
        status, freq = read_timex()
    except Exception:
        return None
    return {"ppm": round(freq / _PPM_SCALE, 3),
            "synchronised": not bool(status & _STA_UNSYNC),
            "source": "adjtimex"}


def compare_references(pilot_ppm, clock_ppm, *, agree_within_ppm=10.0,
                       clock_converged=True) -> dict:
    """Do the two instruments agree, and by how much?

    A disagreement is reported as a disagreement. Averaging two figures you cannot choose
    between produces a third you can defend even less than either. And an unsettled reference
    is reported as unsettled, not as a disagreement: `clock_converged=False` means the rtl_test
    readings were still moving when it stopped, so that number cannot check the pilot yet —
    printing a confident "they disagree by X" from it would be a verdict the evidence does not
    support. `settled` is None when there is only one reference, True/False when there are two.
    """
    if pilot_ppm is None or clock_ppm is None:
        return {"agree": None, "difference_ppm": None, "settled": None,
                "reason": "only one reference answered, and one reference cannot check itself"}
    difference = round(abs(pilot_ppm - clock_ppm), 2)
    if not clock_converged:
        return {"agree": None, "difference_ppm": difference, "settled": False,
                "reason": "the clock reference has not settled — its readings were still moving "
                          "when it stopped — so it cannot check the pilot yet"}
    agree = difference <= agree_within_ppm
    return {"agree": agree, "difference_ppm": difference,
            "threshold_ppm": agree_within_ppm, "settled": True,
            "reason": "" if agree else
                      "the two references disagree by more than the margin, and there is no "
                      "third to say which is wrong"}
