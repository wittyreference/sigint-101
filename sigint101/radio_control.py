# ABOUTME: Serializes SDR ownership without taking decoder or I/O locks inside the arbiter.
# ABOUTME: Runs console shutdown work in daemon threads with a single monotonic deadline.

import asyncio
import math
import re
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .receiver_lifecycle import daemon

CLAIM_QUEUE_WAIT_S = 2.0
# PPM reads for up to 13 seconds, then can spend both C42 stop phases reaping rtl_fm.
PPM_HOLD_S = 20.0


class RadioArbiter:
    def __init__(self):
        self._lock = threading.Condition()
        self._claim = None
        self._closing = False

    @property
    def busy(self):
        with self._lock:
            return self._claim is not None

    def close(self):
        with self._lock:
            self._closing = True
            self._lock.notify_all()

    @contextmanager
    def reserve(self, receiver, unavailable, deadline):
        with self._lock:
            # A ppm holder may stretch this claim's wait, but never past one PPM_HOLD_S from its
            # arrival, however many ppm claims take the radio in turn.
            ppm_cap = time.monotonic() + PPM_HOLD_S
            while self._claim is not None and not self._closing:
                owner, started = self._claim
                holder_deadline = deadline
                if owner == "ppm":
                    holder_deadline = max(deadline, min(started + PPM_HOLD_S, ppm_cap))
                left = holder_deadline - time.monotonic()
                if left <= 0:
                    raise unavailable("the radio is busy finishing another request")
                self._lock.wait(left)
            if self._closing:
                raise unavailable("the console is shutting down")
            self._claim = (receiver, time.monotonic())
        try:
            yield
        finally:
            with self._lock:
                self._claim = None
                self._lock.notify_all()


# How long a radio claim or a tune waits on a lesson's tuning restore before it is refused (C81).
TUNING_WAIT_S = 2.0
TUNING_REASON = "tuning the radios for this lesson"
# What a wait says when a restore is held up by a ppm calibration, which holds the radio and the
# launch permit for its capture: the calibration, as its panel names it, not a tuning.
CALIBRATION_REASON = "measuring the clock error (ppm)"
CALIBRATION = "ppm"

# What a restore did with each receiver it is responsible for (C81).
TUNED = "tuned"
YOU_RETUNED = "you retuned"           # the learner tuned it after the restore's action
NEWER_LESSON = "a newer lesson"       # a lesson opened after this one is restoring instead
STALE_REASON = "a newer lesson was opened"


def boot_epoch() -> str:
    """The clock epoch stamps are read against: this boot's id.

    time.monotonic() is the system's monotonic clock (Linux CLOCK_MONOTONIC), the same for every
    process until the next boot, so a page rendered before the console restarted still stamps on
    the clock the console reads. Without a boot id, a fresh token: such pages fall back to their
    arrival time.
    """
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        boot_id = ""
    token = boot_id.replace("-", "").lower()
    return token if re.fullmatch(r"[0-9a-f]{8,64}", token) else secrets.token_hex(8)


class TuningBusy(Exception):
    """A claim or a tune waited TUNING_WAIT_S on a lesson's tuning restore and gave up."""


class Superseded(Exception):
    """A restore or a tune that a newer learner action has already overtaken."""


class TuningGate:
    """Orders a lesson's tuning restore (POST /api/tuning) against tunes and radio claims.

    The lesson page posts the restore without blocking, so a tune or a panel poll can arrive
    while a slow one is still stopping tools, and a request can reach the console after one the
    learner sent later. These rules keep what the learner sees true:

    - Order is the learner's, not the network's. Each restore and tune carries the time of the
      learner's action on the console's own monotonic clock (`stamp`). The page maps the
      browser's shared timeline onto it: the console's time at render, plus the browser time
      since the response arrived (action-clock.js), so every tab and reload maps the same way.
      A restore older than the newest one is refused before it starts (Superseded), as is a
      tune older than the newest restore or the newest tune of its receiver. A request without
      a stamp, or from another boot, is stamped with its arrival, taken before any worker or
      hardware probe runs; a stamp is never later than that arrival.
    - A restore never undoes a newer action: each receiver it resets is a step under the step
      lock, skipped when the learner tuned that receiver after the restore's action
      (YOU_RETUNED) or a newer lesson's restore has begun (NEWER_LESSON). The restore reports
      each receiver's outcome, so the page never says "tuned" for a skipped one.
    - No receiver starts mid-restore. A claim's last admission and its launch hold a permit
      (admit_claim/end_claim) that a restore cannot begin under: begin_restore marks itself
      first, so no new permit is granted, then waits for the permits in hand to end. A claim
      that finds a restore begun while it queued lets the radio go and waits (wait_idle).
    - Nothing waits without bound: a claim (across its retries) and a tune wait at most
      TUNING_WAIT_S, then are refused with TUNING_REASON. A tune waits only for the step in
      hand. A restore waits for the permits in hand, each bounded by its own launch.
    """

    def __init__(self):
        self._step = threading.Lock()
        self._state = threading.Condition()
        self._restoring = 0
        self._claims = 0
        self._calibrating = 0
        self._newest_restore = float("-inf")
        self._tuned_at = {}
        self.epoch = boot_epoch()

    @staticmethod
    def now_ms() -> float:
        return time.monotonic() * 1000.0

    def anchor(self) -> tuple:
        """The clock a page stamps its actions on: (epoch, the console's time in ms). Read-only."""
        return self.epoch, round(self.now_ms(), 3)

    def stamp(self, epoch, at, arrived: float | None = None) -> float:
        """The time of a learner's action as the page stamped it, or the request's arrival when
        it cannot be trusted: no stamp, another epoch, not a number. Never later than the
        arrival, so a page cannot stamp an action ahead of what it has seen. `arrived` is taken
        as the request reached the console (arrival_ms), before any worker or probe ran."""
        arrived = self.now_ms() if arrived is None else arrived
        if at is None or epoch != self.epoch:
            return arrived
        try:
            at = float(at)
        except (TypeError, ValueError):
            return arrived
        return min(at, arrived) if math.isfinite(at) else arrived

    def begin_restore(self, at: float) -> float:
        """Register a restore of the learner's action at `at`, or raise Superseded if a newer
        lesson's restore is already registered. Returns the restore's token."""
        with self._state:
            if at < self._newest_restore:
                raise Superseded(STALE_REASON)
            self._newest_restore = at
            self._restoring += 1
            # No new permit is granted from here; the ones in hand end with their launch.
            self._state.wait_for(lambda: self._claims == 0)
            return at

    def end_restore(self) -> None:
        with self._state:
            self._restoring -= 1
            self._state.notify_all()

    def skip_restore(self, at: float) -> None:
        """Order a faded lesson that restores no receiver against earlier restores and tunes,
        without holding radio claims or touching any receiver.

        Publishes `at` at once so earlier restores that have not yet entered a step are cancelled,
        then fences any already-admitted step under `_step` (bounded by TUNING_WAIT_S) so a faded
        load never acknowledges completion while an older reset can still write.
        """
        with self._state:
            if at < self._newest_restore:
                raise Superseded(STALE_REASON)
            self._newest_restore = at
        if not self._step.acquire(timeout=TUNING_WAIT_S):
            raise TuningBusy("an earlier restore is still in progress")
        try:
            with self._state:
                if at < self._newest_restore:
                    raise Superseded(STALE_REASON)
        finally:
            self._step.release()

    def restore_step(self, receiver: str, token: float, reset) -> str:
        """Run `reset` for `receiver` unless a newer action overtook restore `token`; say which."""
        with self._step:
            with self._state:
                if self._newest_restore > token:
                    return NEWER_LESSON
                if self._tuned_at.get(receiver, float("-inf")) > token:
                    return YOU_RETUNED
            reset()
            return TUNED

    def tune(self, receiver: str, at: float, action):
        """Run a learner's tune of `receiver` at `at`, unless a newer action has overtaken it."""
        if not self._step.acquire(timeout=TUNING_WAIT_S):
            raise TuningBusy(TUNING_REASON)
        try:
            with self._state:
                if at < self._newest_restore:
                    raise Superseded(STALE_REASON)
                if at < self._tuned_at.get(receiver, float("-inf")):
                    raise Superseded("a newer tune was sent")
                self._tuned_at[receiver] = at
            return action()
        finally:
            self._step.release()

    @staticmethod
    def past(deadline: float) -> bool:
        return time.monotonic() >= deadline

    @staticmethod
    def claim_budget() -> float:
        """The deadline a claim's waits on restores share, across its retries."""
        return time.monotonic() + TUNING_WAIT_S

    def busy_reason(self) -> str:
        """Why a claim cannot start now: a calibration holding up the restore, or the tuning."""
        with self._state:
            return self._reason()

    def _reason(self) -> str:
        return CALIBRATION_REASON if self._calibrating else TUNING_REASON

    def state(self) -> dict:
        """What GET /api/tuning reports: is a restore under way, and is it waiting on a
        calibration that holds the radio. Read-only."""
        with self._state:
            restoring = self._restoring > 0
            return {"restoring": restoring,
                    "waiting_on": "calibration" if restoring and self._calibrating else None}

    def wait_idle(self, until: float | None = None) -> None:
        """Wait for every restore under way to finish, or raise TuningBusy at `until`, saying
        what really holds the radio.

        A claim that had to wait keeps its deadline when it gets the lock back: a wait can time
        out while another thread holds the condition, and find the gate idle once it reacquires
        it. That claim's budget is spent, so it is refused rather than let through late."""
        if until is None:
            until = self.claim_budget()
        with self._state:
            if self._restoring == 0:
                return
            left = lambda: max(0.0, until - time.monotonic())  # noqa: E731
            idle = self._state.wait_for(lambda: self._restoring == 0, timeout=left())
            if not idle or self.past(until):
                raise TuningBusy(self._reason())

    def admit_claim(self, owner: str | None = None) -> bool:
        """Take the permit a claim launches under, unless a restore has begun (then False).
        `owner` CALIBRATION marks a ppm calibration, so a wait behind it names it."""
        with self._state:
            if self._restoring:
                return False
            self._claims += 1
            if owner == CALIBRATION:
                self._calibrating += 1
            return True

    def end_claim(self, owner: str | None = None) -> None:
        with self._state:
            self._claims -= 1
            if owner == CALIBRATION:
                self._calibrating -= 1
            self._state.notify_all()


class ReceptionSink:
    def __init__(self):
        self._lock = threading.Lock()
        self._open = True
        self._pending = 0
        self._publication = threading.local()

    def admit(self):
        with self._lock:
            if not self._open:
                return False
            self._pending += 1
            return True

    def finished(self):
        with self._lock:
            self._pending -= 1

    def pending(self):
        with self._lock:
            return self._pending

    def open(self):
        with self._lock:
            return self._open

    def allows_log(self):
        """A publication already admitted on this thread can finish after sealing."""
        return getattr(self._publication, "active", False) or self.open()

    @contextmanager
    def publication(self):
        """Admit a gallery publication and its reception together before either can start."""
        if not self.admit():
            yield False
            return
        active = getattr(self._publication, "active", False)
        self._publication.active = True
        try:
            yield True
        finally:
            self._publication.active = active
            self.finished()

    def seal(self):
        with self._lock:
            self._open = False


async def finish_console(receivers, stores, sink, timeout, *, report_owners=None):
    deadline = time.monotonic() + timeout
    # Keep one second for both independent closes, scaled down with an injected test deadline.
    drain_end = deadline - min(1.0, timeout / 4)
    errors = []

    def start_work(actions):
        jobs = {}
        for name, action in actions:
            def work(name=name, action=action):
                try:
                    action()
                except Exception as exc:
                    errors.append(f"{name}: {exc}")

            jobs[name] = daemon(work, name=f"sigint101-shutdown-{name}")
        return jobs

    async def observe(jobs, until, pending=lambda: False):
        while (any(job.is_alive() for job in jobs.values()) or pending()):
            left = until - time.monotonic()
            if left <= 0:
                return
            await asyncio.sleep(min(left, 0.01))

    # All stop intents are posted before awaiting any one recipient.
    jobs = start_work(receivers)
    await observe(jobs, drain_end, sink.pending)
    sink.seal()
    if report_owners is not None:
        try:
            errors.extend(report_owners())
        except Exception as exc:
            errors.append(f"radio ownership: {exc}")
    errors.extend(f"{name}: shutdown still pending" for name, job in jobs.items() if job.is_alive())
    if pending := sink.pending():
        errors.append(f"{pending} reception reports still pending")
    stores = start_work(stores)
    await observe(stores, deadline)
    errors.extend(f"{name}: close still pending" for name, job in stores.items() if job.is_alive())
    return list(dict.fromkeys(errors))
