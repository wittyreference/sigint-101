# ABOUTME: Serializes SDR ownership without taking decoder or I/O locks inside the arbiter.
# ABOUTME: Runs console shutdown work in daemon threads with a single monotonic deadline.

import asyncio
import threading
import time
from contextlib import contextmanager

from .receiver_lifecycle import daemon

CLAIM_QUEUE_WAIT_S = 2.0
# PPM reads for up to 13 seconds, then can spend both C42 stop phases reaping rtl_fm.
PPM_HOLD_S = 20.0


class RadioArbiter:
    def __init__(self):
        self._lock = threading.Condition()
        self._claim = None
        self._closing = False

    def close(self):
        with self._lock:
            self._closing = True
            self._lock.notify_all()

    @contextmanager
    def reserve(self, receiver, unavailable, deadline):
        with self._lock:
            ppm_deadline = None
            while self._claim is not None and not self._closing:
                owner, started = self._claim
                if owner == "ppm" and ppm_deadline is None:
                    ppm_deadline = started + PPM_HOLD_S
                left = max(deadline, ppm_deadline or 0) - time.monotonic()
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


class ReceptionSink:
    def __init__(self):
        self._lock = threading.Lock()
        self._open = True
        self._pending = 0

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

    def seal(self):
        with self._lock:
            self._open = False


async def finish_console(receivers, stores, sink, timeout):
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
    errors.extend(f"{name}: shutdown still pending" for name, job in jobs.items() if job.is_alive())
    if pending := sink.pending():
        errors.append(f"{pending} reception reports still pending")
    stores = start_work(stores)
    await observe(stores, deadline)
    errors.extend(f"{name}: close still pending" for name, job in stores.items() if job.is_alive())
    return errors
