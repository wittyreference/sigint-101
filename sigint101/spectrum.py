# ABOUTME: Drives rtl_power and turns its CSV sweeps into waterfall rows for Lesson 1.
# ABOUTME: No radio, no rtl_power, no data — this module reports absence, it never invents a signal.

from __future__ import annotations

import statistics
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass

from .childproc import FIRST_RETRY_S, MAX_RETRY_S, STABLE_AFTER_S, spawn
from .hardware import (
    RTL_POWER,
    DeviceSelectionError,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# The FM broadcast band at 125 kHz bins — rtl_power's own worked example, and the
# resolution where individual stations read as ~200 kHz blocks rather than hairlines.
DEFAULT_LOW = 88_000_000
DEFAULT_HIGH = 108_000_000
DEFAULT_BIN = 125_000
DEFAULT_INTEGRATION = 1
DEFAULT_HISTORY = 120

# What an R828D front end can actually reach. Asking for a frequency outside
# this is a real answer — "no" — not something to quietly clamp into range.
TUNER_MIN_HZ = 24_000_000
TUNER_MAX_HZ = 1_766_000_000

# A span of many hundreds of MHz sweeps in chunks of about 2.5 MHz, so a single row
# would take minutes to fill. That is a survey, not a waterfall.
MIN_SPAN_HZ = 100_000
MAX_SPAN_HZ = 100_000_000

# rtl_power's own documented bin range.
MIN_BIN_HZ = 1
MAX_BIN_HZ = 2_800_000

# How many bins one row may ask for: about the waterfall's width in pixels. rtl_power splits each
# hop into a power-of-two count of bins no wider than the request, so it can deliver up to twice
# this. Unbounded, a 1 Hz bin over 100 MHz asks for 10^8 values per row, every poll.
MAX_BINS = 1024

# What an R828D's input amplifier can do. rtl_power snaps a request to the tuner's
# nearest supported step (asking for 30 gets ~29.7), so the range is checked here
# but the step table is the tuner's business.
MIN_GAIN_DB = 0.0
MAX_GAIN_DB = 49.6

# rtl_power's own hop arithmetic (frequency_range() in rtl_power.c): a span is cut into the fewest
# equal hops no wider than this, and a bin this wide or wider is a "giant bin", one per hop.
RTL_POWER_MAX_HOP_HZ = 2_800_000
RTL_POWER_GIANT_BIN_HZ = 1_000_000

# How many measured bins a tuned-to target is kept from the centre of the hop that holds it. The
# centre bin itself is rtl_power's DC copy (unmeasured), and its neighbours carry the DC spike's
# skirt; a few bins out is clean, and still far from the roll-off at the hop's edges.
SPAN_GUARD_BINS = 4


class SpectrumUnavailable(RuntimeError):
    """The radio cannot be swept. Never a reason to synthesise data — the panel says so instead."""


class TuneError(ValueError):
    """The requested setting — span or gain — is not one this radio can do. Fails closed."""


def _hz(value, label: str) -> int:
    """Turn a user-supplied frequency into whole Hz, or refuse it by name.

    Refusing rather than coercing matters: silently turning a typo into some other
    frequency would point real hardware somewhere the learner did not ask for.
    """
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TuneError(f"{label} is not a frequency: {value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise TuneError(f"{label} is not a finite frequency: {value!r}")
    return int(number)


def validate_span(low=None, high=None, bin_size=None) -> tuple:
    """Check a requested sweep span, or fall back to the FM band.

    Fails closed like the TX gate: anything unparseable or out of reach is refused
    with a reason, never clamped into something that looks like it worked.
    """
    # Both edges absent means "no opinion", so fall back to the FM band. One edge absent
    # is a half-formed request and falls through to _hz below, which refuses it by name.
    if low is None and high is None:
        low, high = DEFAULT_LOW, DEFAULT_HIGH
    if bin_size is None:
        bin_size = DEFAULT_BIN

    low = _hz(low, "low")
    high = _hz(high, "high")
    bin_size = _hz(bin_size, "bin size")

    if low >= high:
        raise TuneError(f"low ({low} Hz) must be below high ({high} Hz)")
    # The tuner chip simply cannot reach outside this range. Refusing is the honest
    # answer; clamping would sweep a band the learner never asked for and report success.
    if low < TUNER_MIN_HZ or high > TUNER_MAX_HZ:
        raise TuneError(
            f"this radio tunes {TUNER_MIN_HZ // 1_000_000}-{TUNER_MAX_HZ // 1_000_000} MHz; "
            f"{low / 1e6:.3f}-{high / 1e6:.3f} MHz is outside it")
    if not MIN_BIN_HZ <= bin_size <= MAX_BIN_HZ:
        raise TuneError(f"bin size must be {MIN_BIN_HZ} Hz to {MAX_BIN_HZ} Hz, got {bin_size} Hz")

    span = high - low
    # A span narrower than a single bin cannot produce even one measurement, so there
    # would be nothing to draw.
    if span < max(MIN_SPAN_HZ, bin_size):
        raise TuneError(f"span {span} Hz is narrower than one {bin_size} Hz bin")
    # The dongle can only listen to about 2.5 MHz at once, so a wide span is swept in
    # chunks one after another. Past this width a single row takes minutes to fill, which
    # is a survey, not a waterfall.
    if span > MAX_SPAN_HZ:
        raise TuneError(
            f"span {span / 1e6:.1f} MHz is wider than {MAX_SPAN_HZ // 1_000_000} MHz; "
            "a sweep that wide takes minutes per row")
    if span / bin_size > MAX_BINS:
        raise TuneError(
            f"{span / 1e6:.3f} MHz in {bin_size} Hz bins is {span // bin_size} bins per row; "
            f"the waterfall holds at most {MAX_BINS}")

    return low, high, bin_size


def validate_gain(gain):
    """Check a requested tuner gain in dB, or pass None through as automatic.

    A dB expresses a ratio — every 10 dB is ten times the power — so gain is how
    hard the tuner's input amplifier works, not a loudness in absolute terms.
    Fails closed like the span gate: unparseable or out of range is refused with a
    reason, never clamped into something that looks like it worked.
    """
    if gain is None:
        return None
    try:
        number = float(gain)
    except (TypeError, ValueError) as exc:
        raise TuneError(f"gain is not a number: {gain!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise TuneError(f"gain is not a finite number: {gain!r}")
    if not MIN_GAIN_DB <= number <= MAX_GAIN_DB:
        raise TuneError(
            f"this tuner's gain runs {MIN_GAIN_DB:g}-{MAX_GAIN_DB:g} dB; got {number:g}")
    return number


@dataclass(frozen=True)
class HopPlan:
    """How rtl_power will cut a span: each hop's centre, the hops' width, and the bin it delivers.

    A hop's centre frequency is its DC bin, which rtl_power cannot measure (parse_line keeps it
    as None). `bins` is the count per hop; `step` is the bin width rtl_power prints.
    """

    centres: list
    hop_width: int
    bins: int
    step: float


def hop_plan(low, high, bin_size) -> HopPlan:
    """Predict rtl_power's hops for a span, the way frequency_range() in rtl_power.c lays them.

    The fewest equal hops no wider than 2.8 MHz, each centred half a hop in; the bins per hop the
    smallest power of two that keeps a bin no wider than asked. A giant bin (1 MHz or more) is one
    bin a hop, as wide as asked. Checked against real captures from nomad-b, including the
    downsampled path a span under 1 MHz takes.
    """
    low, high, bin_size = int(low), int(high), int(bin_size)
    span = high - low
    if bin_size >= RTL_POWER_GIANT_BIN_HZ:
        count = max(span // bin_size, 1)
        return HopPlan(centres=[low + i * bin_size + bin_size // 2 for i in range(count)],
                       hop_width=bin_size, bins=1, step=float(bin_size))
    # The smallest count whose (integer) hop width is within the limit, as rtl_power's loop finds.
    count = span // (RTL_POWER_MAX_HOP_HZ + 1) + 1
    width = span // count
    bins = 2
    while width / bins > bin_size and bins < 2 ** 21:
        bins *= 2
    return HopPlan(centres=[low + i * width + width // 2 for i in range(count)],
                   hop_width=width, bins=bins, step=width / bins)


def target_span(center, width, bin_size=None) -> tuple:
    """A span `width` wide around a target, offset so the target sits on measured bins.

    Centred exactly, a single-hop span puts its target on the hop's unmeasured centre bin (M9's
    433.92 MHz carrier disappeared into it), and an even hop count puts it on a hop edge, where
    the dongle's filter rolls off. So the span is shifted, by as little as it takes, until the
    target is SPAN_GUARD_BINS measured bins from the centre of the hop that holds it. The span
    keeps its width and bin size; the target, not the span's middle, is what the dial reads. A
    span with giant bins has no centre gap and stays centred; so does one with too few bins a hop
    to step aside, and one that no offset keeps inside the tuner (the span gate then decides).
    """
    bin_size = DEFAULT_BIN if bin_size is None else int(bin_size)
    center, width = int(center), int(width)
    low = center - width // 2
    high = low + width
    plan = hop_plan(low, high, bin_size)
    guard_bins = min(SPAN_GUARD_BINS, plan.bins // 4)
    if plan.bins < 2 or guard_bins < 1:
        return low, high, bin_size
    guard = guard_bins * plan.step
    shifts = sorted({round(center - hop - side * guard)
                     for hop in plan.centres for side in (1, -1)},
                    key=lambda d: (abs(d), -d))
    for shift in shifts:
        if low + shift >= TUNER_MIN_HZ and high + shift <= TUNER_MAX_HZ:
            return low + shift, high + shift, bin_size
    return low, high, bin_size


def validate_target_span(center, width, bin_size=None) -> tuple:
    """Check a target-centred request and build its offset span, refusing what cannot be swept.

    Fails closed like validate_span: the span centred on the target must pass its gate before any
    offset is worked out, and the offset span must pass it too.
    """
    center = _hz(center, "centre")
    width = _hz(width, "span")
    low = center - width // 2
    _, _, bin_size = validate_span(low, low + width, bin_size)
    return validate_span(*target_span(center, width, bin_size))


@dataclass(frozen=True)
class SweepRow:
    """One full-band sweep: the power across every bin at a single moment.

    A bin rtl_power did not measure is None in `dbm`, never a borrowed neighbour's value, so
    every number in a row is a measurement at the frequency it is drawn at.
    """

    ts: str
    freq_low: float
    freq_high: float
    freq_step: float
    dbm: tuple

    def frequencies(self) -> tuple:
        """Where each power reading sits in frequency, in Hz. Pairs one-to-one with `dbm`.

        A "bin" is one slice of spectrum the receiver measured power in — the finest
        detail this sweep can show. These are the bins' *centres*: FFT bin k of a hop is
        centred at low + k*step, so the first sits on freq_low and the last a step short
        of freq_high, where the next hop's first bin is measured.
        """
        return tuple(self.freq_low + i * self.freq_step for i in range(len(self.dbm)))

    def measured(self) -> list:
        """The power readings rtl_power actually measured, without the unmeasured bins."""
        return [v for v in self.dbm if v is not None]

    def noise_floor_db(self) -> float:
        """The power of the quiet bins — what the receiver hears when nobody transmits.

        The median across bins, because stations occupy a minority of any real span:
        the middle value lands in the noise however bright the few busy bins are.
        Rows always carry at least one measured bin (each hop prints at least one).
        """
        return statistics.median(self.measured())

    def peak(self) -> tuple:
        """The strongest bin, as (frequency in Hz, power in dB). First bin wins a tie."""
        strongest = max(self.measured())
        index = self.dbm.index(strongest)
        return (self.freq_low + index * self.freq_step, strongest)

    def snr_db(self) -> float:
        """Signal-to-noise ratio: how far the strongest bin stands above the floor.

        A subtraction because dB values are already logarithms — the ratio of peak
        power to floor power becomes peak minus floor. This is the number a gain
        change does or does not improve.
        """
        return self.peak()[1] - self.noise_floor_db()

    def as_dict(self) -> dict:
        """Flatten this sweep into JSON for the browser.

        Each row carries its own frequency axis rather than relying on a global one,
        because the sweep can be retuned between rows and stale rows must never be
        painted against a new band's axis. The readout numbers are computed here, not
        in the browser, so they stay under test.
        """
        peak_hz, peak_db = self.peak()
        floor_db = self.noise_floor_db()
        return {
            "ts": self.ts,
            "freq_low": self.freq_low,
            "freq_high": self.freq_high,
            "freq_step": self.freq_step,
            "dbm": list(self.dbm),
            "floor_db": floor_db,
            "peak_db": peak_db,
            "peak_hz": peak_hz,
            "snr_db": peak_db - floor_db,
        }


def parse_line(line) -> SweepRow | None:
    """Parse one rtl_power CSV line, or return None if it isn't one.

    Columns: date, time, Hz low, Hz high, Hz step, samples, dbm, dbm, ...

    rtl_power 0.6.0 (the Debian build on the Nomad) prints two values that are not
    measurements, and both are taken out here (csv_dbm() in rtl_power.c):
    - The trailing duplicate. Its loop prints bins 0..len-1, then prints the last bin a
      second time, so a line carries len+1 values and the last two are always equal. That
      extra value would be drawn at freq_high, which is the next hop's first bin.
    - The DC copy. Before printing, it overwrites the hop's DC bin with its neighbour
      ("nuke DC component"). After its 180-degree rotation that bin sits at the hop centre.
      It is kept in place as None, so the bins either side stay at their own frequencies and
      the waterfall can show the gap honestly instead of a borrowed value.
    """
    if not line:
        return None
    line = line.strip()
    # rtl_power interleaves blanks and the occasional banner with its data rows; neither
    # is a measurement, so skip rather than fail on them.
    if not line or line.startswith("#"):
        return None

    fields = [f.strip() for f in line.split(",")]
    while fields and fields[-1] == "":  # tolerate a trailing comma
        fields.pop()
    # Six header columns (date, time, low, high, step, samples) plus at least one power
    # reading. Fewer than that is a truncated line — a sweep cut off mid-write.
    if len(fields) < 7:
        return None

    try:
        low = float(fields[2])
        high = float(fields[3])
        step = float(fields[4])
        # Everything after the header is power in dB: a logarithmic scale, so a reading
        # 10 higher is ten times the power, not ten units more of it.
        printed = [float(x) for x in fields[6:]]
    except ValueError:
        # A partially written line is normal when the process is killed mid-sweep.
        return None

    # The trailing duplicate needs a value before it to duplicate; a lone value is a line cut
    # short, not a hop.
    if len(printed) < 2:
        return None
    dbm = printed[:-1]
    # One bin per hop ("giant bins") has no neighbour, so rtl_power copies nothing into it.
    dc = round((high - low) / (2 * step)) if step > 0 else -1
    if len(dbm) > 1 and 0 <= dc < len(dbm):
        dbm[dc] = None

    return SweepRow(ts=f"{fields[0]} {fields[1]}", freq_low=low, freq_high=high,
                    freq_step=step, dbm=tuple(dbm))


def merge_chunks(chunks) -> SweepRow:
    """Join the per-chunk lines of one sweep into a single full-band row.

    rtl_power splits a span wider than the dongle's bandwidth into chunks (hops) that
    abut: each hop's bins run from its low edge up to a step short of its high edge,
    which is where the next hop's first bin is measured. So the hops simply join end to
    end. A gap between two hops is held open with unmeasured bins rather than closed,
    because closing it would slide every later value onto a frequency it was not
    measured at.
    """
    chunks = sorted(chunks, key=lambda c: c.freq_low)
    if not chunks:
        raise ValueError("merge_chunks needs at least one chunk")
    if len(chunks) == 1:
        return chunks[0]

    step = chunks[0].freq_step
    dbm = list(chunks[0].dbm)
    for previous, chunk in zip(chunks, chunks[1:]):
        # Rounded rather than compared exactly: the edges are floats, and rtl_power's own
        # integer edge arithmetic can land a hop a hertz short of its neighbour.
        missing = round((chunk.freq_low - previous.freq_high) / step) if step > 0 else 0
        dbm.extend([None] * max(missing, 0))
        dbm.extend(chunk.dbm)

    return SweepRow(ts=chunks[0].ts, freq_low=chunks[0].freq_low,
                    freq_high=chunks[-1].freq_high, freq_step=step,
                    dbm=tuple(dbm))


def _popen(cmd):
    """Launch rtl_power so its CSV can be read line by line as it sweeps.

    The live feel comes from rtl_power itself, which flushes each CSV row as it
    completes; iterating the pipe then yields rows as they arrive.

    stderr goes to a temporary file rather than being discarded, because when rtl_power
    dies it says why only there ("No matching devices found."), and a sweep that fails
    silently is a blank waterfall claiming to be healthy.

    A file rather than a pipe because nothing here drains stderr while the sweep runs,
    and a pipe whose buffer fills blocks the writer — which would wedge the sweep rather
    than the diagnostic. Measured, rtl_power says little: one whole run emitted 15 lines
    and 383 bytes, all of it configuration printed at startup, none of it per-retune. So
    a pipe would very likely survive; a file removes the question, and costs nothing.
    """
    scratch = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    # spawn() gives rtl_power its own session and ties it to the console's life, so a console
    # killed outright does not leave it holding the dongle.
    proc = spawn(cmd, stdout=subprocess.PIPE, stderr=scratch, text=True)
    # Popen leaves .stderr as None when handed a file, so the handle is reattached
    # here: it gives the reader one place to look, real process or injected fake.
    proc.stderr = scratch
    return proc


class Sweeper:
    """Owns one long-lived rtl_power process and a bounded buffer of recent rows.

    One process, not one per request: opening the dongle costs seconds and detaches
    the kernel driver each time.
    """

    def __init__(self, *, low=DEFAULT_LOW, high=DEFAULT_HIGH, bin_size=DEFAULT_BIN,
                 integration=DEFAULT_INTEGRATION, device=0, history=DEFAULT_HISTORY,
                 spawn=_popen, radio_present=None, gain=None,
                 serial=None, list_devices=None, clock=time.monotonic):
        self.low = int(low)
        self.high = int(high)
        self.bin_size = int(bin_size)
        # The frequency the learner tuned to, when the span was built around one (target_span
        # offsets the sweep off it). None when the span was given by its edges, as the FM default
        # is: the dial then reads the span's middle.
        self.target = None
        self.integration = int(integration)
        # Deliberately not int(): a serial is text, and "00000001" coerced to a number
        # becomes the index 1, which on a bus shared with a KrakenSDR is a different radio.
        self.device = device
        self.serial = serial or None
        # An enumerator is how the sweeper checks which radio it is about to open. Left
        # unwired, the caller has already decided and `device` is taken at its word — which
        # is what a test injecting its own process has done. app.py wires the real one.
        self._list_devices = list_devices
        self.gain = validate_gain(gain)
        self._spawn = spawn
        self._radio_present = radio_present or (lambda: probe_rtlsdr().present)
        self._rows = deque(maxlen=history)
        # Two locks on purpose. _lock guards the row buffer and is taken by the reader
        # thread; _life guards start/stop/release so two threadpool routes cannot race
        # over the dongle. Sharing one would deadlock, because stop() joins the reader
        # while the reader is trying to append a row.
        self._lock = threading.Lock()
        self._life = threading.RLock()
        self._pending = []
        self._proc = None
        self._thread = None
        self._released = False
        self._interrupted = False
        self.error = None
        # The respawn back-off the decoders get from childproc.ChildWatch: an rtl_power that dies
        # on its own is not relaunched on every poll, and each quick death doubles the wait.
        self._clock = clock
        self._started_at = None
        self._deaths = 0
        self._retry_at = None

    @property
    def running(self) -> bool:
        """True while an rtl_power process is alive and holding the dongle.

        The process is polled rather than merely checked for existence. A dongle pulled
        mid-sweep leaves the handle behind, so a sweeper that only asked "is there a
        process object" would report a healthy sweep forever and the panel would show an
        empty waterfall with nothing wrong — absence disguised as silence, which is the
        no-mock invariant failing in the direction nobody watches for.
        """
        # Bound to a local first. _stop_locked clears _proc from another thread, so
        # reading the attribute twice can find a process on the first read and None on
        # the second, and poll() would then raise on None — a 500 to the learner in the
        # middle of a lesson, caused by nothing worse than handing the dongle back.
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _choose_device(self) -> int:
        """Which radio index to open, re-checked every time a sweep starts.

        Re-checked rather than settled once, because the bus changes underneath a running
        console: attaching a KrakenSDR adds five more RTL2832U devices and renumbers what
        index 0 means. A sweep opened on the wrong tuner does not fail — it returns a
        perfectly real waterfall of a port with no antenna on it, which no error path
        would ever catch.
        """
        if self._list_devices is None:
            return self.device
        try:
            return select_rtlsdr_index(self.serial, devices=self._list_devices())
        except DeviceSelectionError as exc:
            raise SpectrumUnavailable(str(exc)) from exc

    def command(self) -> list:
        """The rtl_power invocation for the current span.

        The `-f low:high:bin` argument packs the whole sweep definition into one
        colon-separated field. `-i` is how often rtl_power reports: it hops the span's
        ~2.5 MHz chunks within one interval when it can, but a wide span needs more
        hops than an interval holds and rows then arrive slower — the MAX_SPAN_HZ cap
        above is what keeps that bearable.
        """
        command = [RTL_POWER,
                   "-f", f"{self.low}:{self.high}:{self.bin_size}",
                   "-i", str(self.integration),
                   "-d", str(self.device)]
        # Without -g, rtl_power leaves the tuner on automatic gain.
        if self.gain is not None:
            command += ["-g", str(self.gain)]
        return command

    @property
    def released(self) -> bool:
        """True while the dongle has been handed to another program on purpose."""
        return self._released

    def release(self) -> None:
        """Hand the dongle to another program and stay off it until resumed.

        Only one process can hold an RTL-SDR, so rtl_fm and rtl_433 cannot run while
        a sweep does. Stopping alone is not enough: the next poll would start it again
        and take the radio back mid-listen. Holding _life across the whole operation is
        what makes the answer true — otherwise this can return while a concurrent
        start() is still inside its radio probe, about to claim the device.
        """
        with self._life:
            self._released = True
            self.stop()
            self._pending = []
            with self._lock:
                self._rows.clear()

    def resume(self) -> None:
        """Take the radio back after another program has finished with it."""
        with self._life:
            self._released = False

    def start(self) -> None:
        """Begin sweeping, unless the radio is absent or deliberately released."""
        with self._life:
            self._start_locked()

    def _start_locked(self) -> None:
        """Start the sweep. Callers must already hold `_life`.

        The locked body lives apart from `start` so the lock discipline stays visible:
        every mutation of `_proc` and `_thread` happens in a method whose name says the
        lock is already held, mirroring `_stop_locked` below.
        """
        if self.running:
            return
        if self._released:
            raise SpectrumUnavailable(
                "the radio was released to another program; resume the sweep to take it back")
        if not self._radio_present():
            raise SpectrumUnavailable(
                "no RTL-SDR on the USB bus — SIGINT 101 will not fake a waterfall")
        # A sweep that died since the last poll is reported before another is launched.
        # Without this the console would respawn a doomed rtl_power on every poll and
        # keep answering 200 with an empty row list — a waterfall that is blank because
        # nothing works, presented exactly like a band that is quiet. While the back-off
        # runs the reason stands; once it has run out the reason is reported one last time
        # and cleared, so a dongle that comes back is picked up on the next poll instead of
        # the console staying wedged on an old failure.
        # Read-and-clear is not atomic against the reader thread, which writes `error`
        # as a process dies. A write lost in that window is self-correcting rather than
        # silent: the sweep it described is dead, so the next spawn dies the same way and
        # records the same reason. Taking `_life` here instead would deadlock — stop()
        # joins the reader while holding it, which is why the two locks exist at all.
        if self.error:
            reason = self.error
            if self.retry_in() is None:
                self.error = None
            raise SpectrumUnavailable(reason)
        self.device = self._choose_device()
        # OSError is rtl_power not installed; ValueError is a malformed command. Both
        # mean no sweep, and the console says which rather than showing a blank panel.
        try:
            self._proc = self._spawn(self.command())
        except (OSError, ValueError) as exc:
            self._proc = None
            raise SpectrumUnavailable(f"cannot run {RTL_POWER}: {exc}") from exc

        self.error = None
        self._interrupted = False
        self._started_at = self._clock()
        # A daemon thread, so a wedged rtl_power cannot keep the whole console alive
        # after shutdown — the dongle must come free when the service stops.
        self._thread = threading.Thread(target=self._read, name="sigint101-sweeper", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Consume rtl_power's CSV on a background thread until the process ends.

        Runs off the request path because a sweep never finishes on its own — reading
        it inline would block the console forever.
        """
        try:
            for raw in self._proc.stdout:
                chunk = parse_line(raw)
                if chunk is not None:
                    self._absorb(chunk)
        except (OSError, ValueError) as exc:  # loud, never silent
            self.error = str(exc)
        finally:
            # A deliberate stop() cuts rtl_power off mid-pass, so its in-flight chunks cover a
            # fraction of the band — publishing them would hand the panel a row whose SNR and
            # axis both lie. Any other death is not ours: rtl_power exiting 0, or a finite
            # source ending on its own (poll() is None, as the tests feed), is a clean finish
            # whose last pass is published; but a non-zero exit — a positive rtl_power error, or
            # a negative signal from an external SIGTERM/SIGKILL, a USB wedge, or an OOM kill —
            # cut the pass short, so its chunks are dropped rather than merged into a full-looking
            # row, and the reason is recorded so /api/spectrum reports the block. Our own stop's
            # SIGTERM never reaches this branch: _interrupted is set before the process is killed,
            # and in production the read above blocks on the pipe until that kill closes it.
            if self._interrupted:
                self._pending = []
            else:
                code = None if self._proc is None else self._proc.poll()
                if code is None or code == 0:
                    self._flush()
                else:
                    self._record_abnormal_death(code)
                    self._pending = []

    def _record_abnormal_death(self, code) -> None:
        """Record why rtl_power died a death we did not cause, so the block is reported.

        A positive status is rtl_power's own error, and its stderr carries the reason. A
        negative status is a signal we did not send: our own stop() sets `_interrupted` and
        never reaches here, so a signal at this point is external — a SIGTERM/SIGKILL from
        another process, an OOM kill, or a USB wedge that took the process down. Either way
        the pass was cut short, so the reason is recorded rather than a truncated in-flight
        pass merged into a full-looking row and served as `available: true`.
        """
        proc = self._proc
        if proc is None:
            return
        if code > 0:
            self.error = f"the sweep stopped (rtl_power status {code}){self._stderr_tail(proc)}"
        else:
            self.error = (f"the sweep was killed mid-pass (signal {-code})"
                          f"{self._stderr_tail(proc)}")
        # The same back-off ChildWatch gives the decoders: a sweep that ran a while was working,
        # so its wait starts afresh; each quick death after it doubles the wait, up to the cap.
        now = self._clock()
        lived = now - self._started_at if self._started_at is not None else 0.0
        self._deaths = 1 if lived >= STABLE_AFTER_S else self._deaths + 1
        self._retry_at = now + min(FIRST_RETRY_S * 2 ** (self._deaths - 1), MAX_RETRY_S)
        # Nothing more will be written: the process is reaped. Closing here frees the
        # handle now rather than at the next start(), and the temporary file with it.
        self._close_stderr(proc)

    @staticmethod
    def _close_stderr(proc) -> None:
        """Release the scratch file a finished process was writing its stderr into."""
        stream = getattr(proc, "stderr", None)
        if stream is not None:
            try:
                stream.close()
            except OSError:  # already gone; the handle is what mattered, not the close
                pass

    #: How many trailing stderr lines to keep. rtl_power prints twelve lines of
    #: configuration when it starts, so a window this size still contains a startup
    #: failure whole; anything smaller truncates the reason away.
    STDERR_LINES = 12

    @classmethod
    def _stderr_tail(cls, proc) -> str:
        """What rtl_power said before it stopped, or nothing if it said nothing.

        A window of trailing lines, not the final line alone. rtl_power announces a bad
        device selector on its fourth line and then narrates its whole configuration
        anyway before exiting non-zero, so the last line is "Reporting every 10 seconds"
        and the reason sits above it. Nor is the order stable: a run that failed to claim
        the device printed its configuration first and the device list after. A window
        wide enough to hold the whole startup block sidesteps the ordering entirely,
        and the measured output is a few hundred bytes, so reading it whole is bounded.
        """
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return ""
        try:
            stream.seek(0)
            lines = [line.strip() for line in stream.read().splitlines() if line.strip()]
        except (OSError, ValueError):  # already closed, or never seekable
            return ""
        if not lines:
            return ""
        return ": " + " | ".join(lines[-cls.STDERR_LINES:])[:400]

    def _absorb(self, chunk: SweepRow) -> None:
        """Collect one chunk, closing off the previous sweep when a new one begins."""
        if self._pending and self._starts_a_new_sweep(chunk):
            self._flush()
        self._pending.append(chunk)

    def _starts_a_new_sweep(self, chunk: SweepRow) -> bool:
        """Has rtl_power begun a fresh pass across the band?"""
        # rtl_power stamps only to the second, so two sweeps can share a timestamp.
        # The band wrapping back to a lower frequency is the reliable structural signal.
        return chunk.ts != self._pending[0].ts or chunk.freq_low <= self._pending[-1].freq_low

    def _flush(self) -> None:
        """Turn the collected chunks into one full-band row and publish it."""
        if not self._pending:
            return
        row = merge_chunks(self._pending)
        self._pending = []
        with self._lock:
            self._rows.append(row)

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for a finite source to be fully consumed. Real sweeps never end."""
        if self._thread is not None:
            self._thread.join(timeout)

    def rows(self, limit: int | None = None) -> list:
        """The most recent sweeps, oldest first, newest last.

        Copied under the lock because the reader thread appends while the console reads.
        """
        with self._lock:
            rows = list(self._rows)
        if limit is None:
            return rows
        return rows[-limit:] if limit > 0 else []

    def set_gain(self, gain) -> None:
        """Point the input amplifier at a new gain and stand down; the next poll restarts.

        Validated before anything stops, so a refused gain leaves the sweep running.
        Unlike retune, buffered rows are kept: only complete sweeps are ever published
        (a pass cut off mid-band is dropped at stop), so every kept row still spans
        the same axis — and the brightness step between old-gain and new-gain rows is
        exactly the thing Lesson 2 wants the learner to see.
        """
        gain = validate_gain(gain)
        with self._life:
            self.stop()
            self.gain = gain

    def retry_in(self) -> float | None:
        """Seconds until a sweep that died on its own may be launched again, or None if it may."""
        if self._retry_at is None:
            return None
        left = self._retry_at - self._clock()
        return left if left > 0 else None

    def retune(self, low, high, bin_size=None, *, target=None) -> None:
        """Point the sweep at a new span and stand down.

        Rows carry their own frequency axis, so those from the old span are dropped —
        painting two bands onto one waterfall would be a lie about what was received.
        The next poll restarts the sweep on the new range. `target` is the frequency the
        span was built around (see retune_to); a span given only by its edges has none.
        """
        low, high, bin_size = validate_span(low, high, bin_size)
        with self._life:
            self.stop()
            self.low, self.high, self.bin_size = low, high, bin_size
            self.target = None if target is None else int(target)
            self._pending = []
            with self._lock:
                self._rows.clear()

    def retune_to(self, center, width, bin_size=None) -> None:
        """Tune to a frequency: a span `width` wide, offset so the target lands on measured bins.

        The sweep's own span then sits a little off the target (target_span says why); the
        target is kept so the dial reads where the learner tuned, not the span's middle.
        """
        low, high, bin_size = validate_target_span(center, width, bin_size)
        self.retune(low, high, bin_size, target=center)

    def stop(self) -> None:
        """End the sweep and let go of the dongle."""
        with self._life:
            self._stop_locked()

    def _stop_locked(self) -> None:
        """Actually release the radio. Callers must already hold `_life`.

        This is the code that hands the dongle back, so it has to finish the job before
        returning — `make listen` opens the device immediately afterwards.
        """
        # Mark the interruption before the process dies, so the reader's final flush
        # knows the chunks it holds are an incomplete pass and drops them.
        self._interrupted = True
        # Clear the handle first, in one step, so the sweeper reads as "not running" for
        # the whole of the slow shutdown below and nothing tries to kill it twice.
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                # terminate() only asks; wait() is what confirms the OS has reaped it and
                # the USB device is genuinely free for the next program.
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # The opposite of already-dead: rtl_power ignored the ask and still holds
                # the dongle, so escalate to kill. If even that goes unreaped, say so —
                # the next program will find the radio busy and deserves the reason.
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.error = "rtl_power survived kill; the dongle may still be held"
            except (OSError, subprocess.SubprocessError):
                # The process was already gone, which is the outcome we wanted. Unlike
                # the silent-failure anti-pattern this repo calls out elsewhere, there is
                # nothing here to report: success and this error mean the same thing.
                pass
        if self._thread is not None:
            # The reader owns the pipe; joining it prevents a dead thread appending a row
            # from the old sweep after a retune has already cleared the buffer.
            self._thread.join(timeout=3)
            self._thread = None
