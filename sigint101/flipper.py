# ABOUTME: The Flipper Zero's USB-CDC serial transport — a stdlib CLI command channel that resolves
# ABOUTME: the device by-id, handles the CR terminator and connect banner, and never fakes a device.

from __future__ import annotations

import glob as _glob
import os
import select
import time
import tty
from dataclasses import dataclass

# Flipper Devices' CDC port, addressed by the stable by-id name rather than a ttyACM number.
# Port numbers move: other USB-CDC devices on the console enumerate as ACM ports too, and the
# Flipper itself can change number after a firmware update. Writing to whichever device landed on
# ACM0 would address the wrong radio entirely — the ambiguity half of the no-mock invariant,
# refused rather than guessed.
BY_ID_GLOB = "/dev/serial/by-id/*Flipper*-if00"

# The CLI prompt. Every text reply ends with it, which is how a read knows the device has finished
# talking rather than merely paused.
PROMPT = b">: "

# LOAD-BEARING: a command is terminated with a bare CR and never CRLF.
#
# `storage write_chunk <path> <N>` reads exactly N bytes from the stream *after* the command line.
# A trailing LF is still sitting in the device's input buffer when that read begins, so it is
# counted as data byte 0 — every real byte shifts down one and the last is lost. The file's length
# is still exactly N, so a size check, a directory listing and `storage stat` all report success.
# A 256-byte all-values probe round-trips with 0 diffs under CR, 256 under CRLF.
TERMINATOR = b"\r"

# `storage read_chunks` announces the length, then this marker, then that many raw bytes. The
# payload is binary and can contain the prompt, a CR, or anything else — so a read ends on the
# declared count and never on a sentinel.
READY_MARKER = b"Ready?\r\n"

# How long to wait on a device that has stopped talking mid-reply. Short, because every caller of
# this module is a console route a learner is waiting on.
DEFAULT_WAIT = 3.0

# Consecutive empty reads that end the settle wait. Fewer once the banner has started arriving
# (it comes in a burst, so a gap means it is done) than when nothing has been heard at all,
# where the port may simply be slow to produce anything.
_QUIET_AFTER_DATA = 3
_QUIET_WHEN_SILENT = 8


class FlipperError(RuntimeError):
    """The transport could not do what was asked, and will not guess at the result."""


class FlipperSelectionError(FlipperError):
    """Raised when which Flipper the console means cannot be determined.

    Separate from "no Flipper present": the console can see devices, it just cannot tell which is
    the subject of the lesson. Picking one would read from an arbitrary instrument and label it as
    the lesson's, which is the ambiguity half of the no-mock invariant.
    """


@dataclass(frozen=True)
class FlipperProbe:
    """Whether a Flipper is attached, and how the console knows.

    `method` is carried for the same reason `RadioProbe` carries it: "detected via serial-by-id"
    is a claim a learner can check, where a bare "detected" is one they must take on faith.
    """

    present: bool
    method: str
    detail: str


def find_flipper_port(*, glob=_glob.glob):
    """Resolve the Flipper to one device path, return None if absent, or refuse if ambiguous.

    Absence is a real answer and returns None. Two devices is not — it raises, naming both, so
    the operator can unplug one rather than being handed a reading from whichever won a race.
    """
    matches = sorted(glob(BY_ID_GLOB))
    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(os.path.basename(m) for m in matches)
        raise FlipperSelectionError(
            f"more than one Flipper on this bus, refusing to guess: {names}. "
            "Unplug all but the one this lesson means.")
    # The by-id path is returned, deliberately un-resolved. Resolving it to /dev/ttyACM0 would
    # discard the device's serial, which is the only thing that makes a reading attributable to a
    # particular instrument later — and attribution is the entire point of recording which
    # instrument took a reading. The symlink opens exactly as well as its target.
    return matches[0]


def probe_flipper(*, glob=_glob.glob) -> FlipperProbe:
    """Is a Flipper physically attached, and by what evidence?"""
    port = find_flipper_port(glob=glob)
    if port is None:
        return FlipperProbe(False, "serial-by-id", "no Flipper on /dev/serial/by-id")
    return FlipperProbe(True, "serial-by-id", port)


class _TtyChannel:
    """The real byte channel: a USB CDC port in raw mode.

    Raw mode matters because the tty layer would otherwise buffer by line and translate control
    characters, which mangles both the CR terminator and any binary payload. This is the whole of
    what pyserial would do for this case, so the dependency is not worth adding.
    """

    def __init__(self, port: str):
        self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self._fd)

    def write(self, data: bytes) -> None:
        """Put bytes on the wire, looping because a large write can be accepted in parts."""
        while data:
            data = data[os.write(self._fd, data):]

    def read(self, n: int, timeout: float) -> bytes:
        """Read what is available within the timeout, or b'' — never block indefinitely."""
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return b""
        return os.read(self._fd, n)

    def close(self) -> None:
        """Release the port. One process owns a radio at a time on this console."""
        os.close(self._fd)


def _default_opener(port: str):
    """Open the real channel. Isolated so tests can substitute a scripted stand-in."""
    return _TtyChannel(port)


class FlipperCli:
    """One command session over the Flipper's serial CLI.

    Used as a context manager so the port is always released; a leaked handle locks out the next
    reader, and on this console the next reader may be the lesson the learner is working.
    """

    def __init__(self, port=None, *, opener=_default_opener, timeout=DEFAULT_WAIT):
        self._port = port
        self._opener = opener
        self._timeout = timeout
        self._channel = None

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _open(self):
        """Open on first use, so constructing a session does not seize the port."""
        if self._channel is None:
            port = self._port or find_flipper_port()
            if port is None:
                raise FlipperError("no Flipper attached")
            self._channel = self._opener(port)
            self._settle()
        return self._channel

    def _settle(self):
        """Wait out the connect banner before treating the session as ready.

        The CLI prints a large ASCII-art banner when a session opens, and it arrives some
        milliseconds after the port itself does. Draining only what has already landed returns
        immediately, and the banner then shows up inside the first command's reply where nothing
        distinguishes it from the device's answer. Key/value replies survive that by accident —
        banner lines carry no colon — but a directory listing comes back with ASCII art in it.

        Gives up on a quiet channel rather than always paying the full timeout, so a session
        that opens without a banner is not slowed by waiting for one.
        """
        buf, quiet, deadline = b"", 0, time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            chunk = self._channel.read(4096, 0.1)
            if chunk:
                buf, quiet = buf + chunk, 0
                if buf.rstrip().endswith(PROMPT.strip()):
                    return
            else:
                quiet += 1
                # Something arrived and then stopped without a prompt, or nothing is coming at
                # all. Either way the session is as ready as waiting longer would make it.
                if quiet >= (_QUIET_AFTER_DATA if buf else _QUIET_WHEN_SILENT):
                    return

    def close(self) -> None:
        """Release the port if this session ever opened one."""
        if self._channel is not None:
            self._channel.close()
            self._channel = None

    def _drain(self) -> None:
        """Discard anything already buffered before issuing a command.

        The device may have left a prompt or the tail of an earlier reply in the buffer; reading
        that back as this command's answer would attribute one command's output to another.
        """
        channel = self._open()
        while channel.read(4096, 0.05):
            pass

    def command(self, text: str, *, wait: float = DEFAULT_WAIT) -> str:
        """Run one CLI command and return its reply, without the echo or the prompt.

        A device that stops answering returns what did arrive rather than hanging — the console
        would rather report a partial reading than stall the lesson.
        """
        channel = self._open()
        self._drain()
        channel.write(text.encode() + TERMINATOR)
        raw = self._read_until_prompt(channel, wait)
        return self._strip(raw.decode("utf-8", "replace"), text)

    @staticmethod
    def _read_until_prompt(channel, wait: float) -> bytes:
        """Collect bytes until the prompt returns or the reply goes quiet."""
        buf, deadline = b"", time.monotonic() + wait
        while time.monotonic() < deadline:
            chunk = channel.read(4096, 0.1)
            if chunk:
                buf += chunk
                if buf.rstrip().endswith(PROMPT.strip()):
                    break
        return buf

    @staticmethod
    def _strip(text: str, command: str) -> str:
        """Drop the echoed command and the prompt, leaving only what the device said."""
        kept = [line for line in text.splitlines()
                if line.strip() != command and not line.strip().startswith(">:")]
        return "\n".join(kept).strip()

    def read_file(self, path: str, size: int) -> bytes:
        """Pull `size` bytes of a file off the device's storage.

        Ends on the declared byte count, never on a sentinel: the payload is binary and may
        legitimately contain the prompt, a CR, or the marker text itself.
        """
        channel = self._open()
        self._drain()
        channel.write(f"storage read_chunks {path} {size}".encode() + TERMINATOR)
        # The deadline scales with the transfer: a 100 KB .fap cannot arrive in the few seconds
        # that suffice for a one-line text reply, and a fixed timeout would truncate large
        # captures while looking like a device fault.
        buf, deadline = b"", time.monotonic() + max(self._timeout, size / 4096.0)
        while time.monotonic() < deadline:
            chunk = channel.read(4096, 0.1)
            if chunk:
                buf += chunk
            # Stop on having the marker plus the declared count — not on a quiet channel, which
            # a mid-transfer stall would imitate, and not on the prompt, which the payload can
            # contain outright.
            start = buf.find(READY_MARKER)
            if start >= 0 and len(buf) - (start + len(READY_MARKER)) >= size:
                break
        start = buf.find(READY_MARKER)
        if start < 0:
            raise FlipperError(
                f"{path}: no '{READY_MARKER.decode().strip()}' marker in the reply — "
                f"the device answered with {buf[:80]!r}")
        # A short read is refused rather than returned. A truncated transfer still leaves the byte
        # count looking right at the call site, so only comparing against the declared length
        # catches it — a short read reported as a complete file is a silently corrupted capture.
        payload = buf[start + len(READY_MARKER):start + len(READY_MARKER) + size]
        if len(payload) != size:
            raise FlipperError(
                f"{path}: expected {size} bytes, got {len(payload)} — refusing to report a "
                "short read as a complete file")
        return payload
