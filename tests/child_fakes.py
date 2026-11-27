# ABOUTME: A radio tool at the process boundary for tests: argv, output, and how (or if) it ends.
# ABOUTME: Shared by the decoder-lifecycle and route tests; never used by the product itself.

import io
import queue
import signal
import subprocess
import tempfile
import threading

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


class LateProc:
    """A child whose pipe outlives it, so its reader is still reading after the process ended.

    It prints only what a test feeds it. terminate() and die() end the process but not the pipe;
    only close_pipe() or break_pipe() ends what the reader reads. `reader` is the thread reading.
    """

    def __init__(self, cmd):
        self.args = list(cmd)
        self.returncode = None
        self.terminated = False
        self.reader = None
        self.stderr = io.StringIO("")
        self._lines = queue.Queue()
        self.stdout = self._stream()

    def _stream(self):
        self.reader = threading.current_thread()
        for line in iter(self._lines.get, None):
            if isinstance(line, OSError):
                raise line
            yield line

    def feed(self, *lines):
        for line in lines:
            self._lines.put(line)

    def close_pipe(self):
        self._lines.put(None)

    def break_pipe(self, error):
        self._lines.put(error)

    def die(self, code):
        self.returncode = code

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


class _ByteStream:
    """Blocking binary stream backed by a queue of bytes chunks, None (EOF), or an exception."""

    def __init__(self, owner):
        self._owner = owner
        self._chunks = queue.Queue()
        self._buf = bytearray()

    def put(self, item):
        self._chunks.put(item)

    def read(self, n: int = -1) -> bytes:
        self._owner.reader = threading.current_thread()
        if not self._buf:
            item = self._chunks.get()
            if item is None:
                return b""
            if isinstance(item, BaseException):
                raise item
            self._buf.extend(item)
        if n is None or n < 0:
            out = bytes(self._buf)
            self._buf.clear()
            return out
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class LateByteProc:
    """A byte-stream sibling of LateProc for pcap readers (Flock) that call stdout.read(n).

    Feed raw bytes chunks (such as pcap records after an optional preamble header) with feed();
    terminate() and die() end the process without closing the pipe, and close_pipe() or
    break_pipe() ends the reader.
    """

    def __init__(self, cmd, *, preamble: bytes = b""):
        self.args = list(cmd)
        self.returncode = None
        self.terminated = False
        self.reader = None
        self.stderr = io.StringIO("")
        self._stream = _ByteStream(self)
        self.stdout = self._stream
        if preamble:
            self._stream.put(preamble)

    def read(self, n: int = -1) -> bytes:
        return self._stream.read(n)

    def feed(self, *chunks: bytes):
        for chunk in chunks:
            self._stream.put(chunk)

    def close_pipe(self):
        self._stream.put(None)

    def break_pipe(self, error):
        self._stream.put(error)

    def die(self, code):
        self.returncode = code

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode
