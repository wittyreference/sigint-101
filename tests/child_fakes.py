# ABOUTME: A radio tool at the process boundary for tests: argv, output, and how (or if) it ends.
# ABOUTME: Shared by the decoder-lifecycle and route tests; never used by the product itself.

import signal
import subprocess
import tempfile

# What rtl_fm and friends print when another process already holds the dongle.
USB_BUSY = "usb_claim_interface error -6\nFailed to open rtlsdr device #0.\n"


class Output:
    """A child's stdout: iterated as text lines by most readers, read() as bytes by Flock's."""

    def __init__(self, lines=()):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)

    def read(self, _n=-1):
        return b""

    def close(self):
        pass


class Child:
    """A radio tool at the process boundary: its argv, its output, and how (or whether) it ends.

    `code` None is a running tool. `stubborn` ignores SIGTERM; `unkillable` ignores SIGKILL too
    (uninterruptible sleep). It says `said` on stderr and, for a tool whose stderr is its output
    (dsd-fme), on stdout as well.
    """

    def __init__(self, args=("tool",), *, code=None, said="", stubborn=False, unkillable=False):
        self.args = list(args)
        self.code = code
        self.stdout = Output(said.splitlines())
        self.stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.stderr.write(said)
        self.stderr.flush()
        self.stubborn = stubborn or unkillable
        self.unkillable = unkillable
        self.sent = []

    def poll(self):
        return self.code

    def terminate(self):
        self.sent.append("TERM")
        if not self.stubborn and self.code is None:
            self.code = -signal.SIGTERM

    def kill(self):
        self.sent.append("KILL")
        if not self.unkillable and self.code is None:
            self.code = -signal.SIGKILL

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("tool", timeout)
        return self.code
