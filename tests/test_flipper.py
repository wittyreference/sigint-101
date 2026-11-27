# ABOUTME: Unit tests for the Flipper Zero USB-CDC serial transport.
# ABOUTME: Guards the CR terminator, the banner, the ambiguity refusal and a failing port's refusal.

import errno
import json
import os
import re
import shutil
import subprocess
import termios
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101.app import create_app
from sigint101.flipper import (
    FlipperCli,
    FlipperError,
    FlipperProbe,
    FlipperSelectionError,
    _TtyChannel,
    find_flipper_port,
    probe_flipper,
)
from sigint101.flipperconsole import FlipperConsole
from tests.test_app import FIXTURE_PACKS, _absent
from tests.test_templates import FIXED_PANEL_MESSAGE_LINES

BY_ID = "/dev/serial/by-id/usb-Flipper_Devices_Inc._Flipper_Ex4mple_flip_Ex4mple-if00"
SECOND = "/dev/serial/by-id/usb-Flipper_Devices_Inc._Flipper_Zzz9xyz_flip_Zzz9xyz-if00"

# A representative `info device` reply — a block of `key : value` lines. The transport tests only
# need a multi-line reply to prove the echo and the prompt are stripped while the body survives.
INFO_DEVICE = """hardware.target               : 7
hardware.region.provisioned   : US
firmware.version              : 1.4.3
firmware.api.major            : 87
firmware.api.minor            : 1
firmware.origin.fork          : Official
"""


class ScriptedChannel:
    """A stand-in for the CDC byte channel that replays canned replies and records writes.

    Keyed on the command text so a caller issuing several commands gets the right answer to
    each, and so a test can assert on exactly what bytes went out — which is the only way to
    catch the terminator regression, since a wrong terminator still produces a plausible reply.
    """

    def __init__(self, replies=None, banner=b"\r\n>: "):
        self.replies = replies or {}
        self.writes = []
        self._pending = bytearray(banner)
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        text = data.decode("utf-8", "replace").strip()
        for command, reply in self.replies.items():
            if text == command:
                self._pending += (command + "\r\n").encode() + reply + b"\r\n>: "
                return
        self._pending += b"\r\n>: "

    def read(self, n: int, timeout: float) -> bytes:
        chunk = bytes(self._pending[:n])
        del self._pending[:n]
        return chunk

    def close(self) -> None:
        self.closed = True

    @property
    def written_text(self) -> str:
        return b"".join(self.writes).decode("utf-8", "replace")


def _globber(*paths):
    return lambda pattern: list(paths)


def _cli(replies=None):
    channel = ScriptedChannel(replies)
    return FlipperCli(port=BY_ID, opener=lambda port: channel), channel


# --- device selection: resolve one Flipper, or refuse to guess ---------------------------

def test_one_match_resolves_to_a_port():
    assert find_flipper_port(glob=_globber(BY_ID)) == BY_ID


def test_no_match_is_absence_not_an_error():
    # Absence is a real answer, exactly as probe_rtlsdr treats an empty bus.
    assert find_flipper_port(glob=_globber()) is None


def test_two_flippers_are_refused_rather_than_guessed():
    with pytest.raises(FlipperSelectionError):
        find_flipper_port(glob=_globber(BY_ID, SECOND))


def test_the_refusal_names_every_candidate():
    with pytest.raises(FlipperSelectionError) as caught:
        find_flipper_port(glob=_globber(BY_ID, SECOND))
    message = str(caught.value)
    # A refusal the operator cannot act on is just a failure. Name both.
    assert "Ex4mple" in message and "Zzz9xyz" in message


def test_probe_reports_how_it_knows():
    probe = probe_flipper(glob=_globber(BY_ID))
    assert isinstance(probe, FlipperProbe)
    assert probe.present is True
    # "detected via serial-by-id" is checkable; a bare "detected" must be taken on faith.
    assert "by-id" in probe.method


def test_probe_with_nothing_attached_is_absent():
    probe = probe_flipper(glob=_globber())
    assert probe.present is False


# --- the terminator: the regression that corrupts every binary ---------------------------

def test_a_command_is_terminated_with_a_bare_cr():
    cli, channel = _cli({"info device": INFO_DEVICE.encode()})
    cli.command("info device")
    assert channel.written_text.endswith("\r")


def test_no_command_ever_writes_a_linefeed():
    """A trailing LF is counted as data byte 0 by write_chunk, shifting the whole file.

    The corrupted file still has the correct length, so only this assertion — on the bytes
    actually written — separates a working transport from one that silently mangles captures.
    """
    cli, channel = _cli({"info device": INFO_DEVICE.encode()})
    cli.command("info device")
    cli.command("info power")
    assert b"\n" not in b"".join(channel.writes)


def test_the_reply_excludes_the_echoed_command_and_the_prompt():
    cli, channel = _cli({"info device": INFO_DEVICE.encode()})
    reply = cli.command("info device")
    assert "info device" not in reply
    assert ">:" not in reply
    assert "firmware.version" in reply


# The CLI prints a large ASCII-art banner when a session opens, and it arrives over USB some
# milliseconds after the port does. Verbatim shape, trimmed: the art, then the product name,
# then the prompt.
CONNECT_BANNER = (b"\r\n"
                  b"              _.-------.._                    -,\r\n"
                  b"          .-\"```\"--..,,_/ /`-,               -,  \\ \r\n"
                  b" _L_  _     ___  ___  ___  ___  ____--\"`___  _     ___\r\n"
                  b"| __|| |   |_ _|| _ \\| _ \\| __|| _ \\   / __|| |   |_ _|\r\n"
                  b"\r\n>: ")


class LatentChannel(ScriptedChannel):
    """A channel whose banner arrives a few reads late, the way USB actually delivers it.

    The plain ScriptedChannel hands the banner over on the first read, which cannot reproduce
    the defect: draining a buffer that is already full works fine. The bug only appears when
    the drain finds nothing, returns, and the banner lands during the command's own read.
    """

    def __init__(self, replies=None, banner=b"", late_reads=3):
        super().__init__(replies, banner=b"")
        self._late = banner
        self._reads_left = late_reads

    def read(self, n: int, timeout: float) -> bytes:
        if self._reads_left > 0:
            self._reads_left -= 1
            return b""
        if self._late:
            late, self._late = self._late, b""
            return late[:n]
        return super().read(n, timeout)


def test_the_connect_banner_does_not_leak_into_the_first_reply():
    """A session opens with a banner that arrives after the port does.

    Draining only what has already landed returns immediately, and the banner then arrives
    during the first command's read where it is indistinguishable from the device's answer.
    Key/value replies survive it by accident because banner lines carry no colon — a directory
    listing does not, and comes back with ASCII art in it.
    """
    channel = LatentChannel({"storage list /ext/subghz": b"\t[D] assets"},
                            banner=CONNECT_BANNER)
    with FlipperCli(port=BY_ID, opener=lambda port: channel) as cli:
        reply = cli.command("storage list /ext/subghz")
    assert "_.-------.." not in reply, "the connect banner leaked into the reply"
    assert "[D] assets" in reply


def test_a_silent_device_returns_what_arrived_instead_of_hanging():
    channel = ScriptedChannel(banner=b"")
    cli = FlipperCli(port=BY_ID, opener=lambda port: channel)
    assert cli.command("info device", wait=0.05) == ""


def test_the_port_is_released_when_the_session_ends():
    channel = ScriptedChannel()
    with FlipperCli(port=BY_ID, opener=lambda port: channel):
        pass
    # One process owns a radio at a time; a leaked handle locks out the next reader.
    assert channel.closed is True


# --- read_file: exact-length binary transfer ---------------------------------------------

def test_read_file_returns_exactly_the_declared_bytes():
    payload = bytes(range(64))
    replies = {"storage read_chunks /ext/x.sub 64":
               b"Size: 64\r\n\r\nReady?\r\n" + payload}
    cli, _ = _cli(replies)
    assert cli.read_file("/ext/x.sub", 64) == payload


def test_read_file_survives_a_payload_containing_the_prompt_bytes():
    # Binary payloads contain anything, including b">: " — the length, not a sentinel, ends it.
    payload = b"abc>: def\r\n>: " + bytes(range(16))
    replies = {f"storage read_chunks /ext/x.sub {len(payload)}":
               b"Size: %d\r\n\r\nReady?\r\n" % len(payload) + payload}
    cli, _ = _cli(replies)
    assert cli.read_file("/ext/x.sub", len(payload)) == payload


def test_a_garbled_read_raises_instead_of_returning_a_short_file():
    replies = {"storage read_chunks /ext/x.sub 64": b"Usage:\r\nstorage <cmd> <path> <args>"}
    cli, _ = _cli(replies)
    with pytest.raises(Exception):
        cli.read_file("/ext/x.sub", 64)


# --- C33: a port that will not open, or stops answering, is a refusal with its reason -------
# WHY: an OSError or a termios.error from the Flipper's port was no FlipperError, so /api/flipper
# answered 500 with no reason, and lesson 29's panel stayed on "reading the Flipper over USB…" for
# as long as the page was open. Every failure to open or talk to the port is now a FlipperError
# with the system's own words and the port named once, refused by the route like no Flipper. No
# test opens a /dev/serial name: ports are tmp paths, a pty, or fakes that never reach os.open.

# The longest by-id name: the firmware caps a Flipper's name at 8 characters (as in test_flock).
LONG_NAME = "usb-Flipper_Devices_Inc._Flipper_Abcdefgh_flip_Abcdefgh-if00"
LONG_PORT = "/dev/serial/by-id/" + LONG_NAME
SHORT_PORT = "/dev/serial/by-id/usb-Flipper_Devices_Inc._Flipper_Aaa_flip_Aaa-if00"
DEVICE_INFO = (Path(__file__).parent / "fixtures" / "flipper" / "device_info.txt").read_bytes()
# Characters in a line of the panel's message (lesson 18's panel has the same box; test_flock).
# The message does not break inside a word, so a word longer than this runs out of the panel.
PANEL_MESSAGE_CHARS = 66
# A refusal for a port that would not open says so, in any of the usual words.
SAYS_IT_WOULD_NOT_OPEN = re.compile(r"\b(cannot|could not|can't|unable to|failed to) open\b")
# The panel's poll intervals (flipper.js): again soon while nothing is read, gently once it is.
RETRY_MS, POLL_MS = 4000, 2000


def _words(code):
    """How Python words an error number: "[Errno 5] Input/output error"."""
    return f"[Errno {code}] {os.strerror(code)}"


# What can stop the open, as os.open and tty.setraw raise it, and the words the refusal must keep.
# os.open's OSError carries the path it was given; termios.error is no OSError and carries a tuple.
OPEN_FAILURES = {
    "enoent-with-filename": (FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), LONG_PORT),
                             _words(errno.ENOENT)),
    "eacces-permission": (PermissionError(errno.EACCES, os.strerror(errno.EACCES), LONG_PORT),
                          _words(errno.EACCES)),
    "eperm-permission": (PermissionError(errno.EPERM, os.strerror(errno.EPERM)),
                         _words(errno.EPERM)),
    "ebusy-no-filename": (OSError(errno.EBUSY, os.strerror(errno.EBUSY)), _words(errno.EBUSY)),
    "enxio-no-filename": (OSError(errno.ENXIO, os.strerror(errno.ENXIO)), _words(errno.ENXIO)),
    "enodev-with-filename": (OSError(errno.ENODEV, os.strerror(errno.ENODEV), LONG_PORT),
                             _words(errno.ENODEV)),
    "message-only": (OSError("the port went away"), "the port went away"),
    "strerror-no-errno": (OSError(None, "the port went away"), "the port went away"),
    "no-words-at-all": (OSError(), "OSError"),
    "termios-enotty": (termios.error(errno.ENOTTY, os.strerror(errno.ENOTTY)),
                       _words(errno.ENOTTY)),
    "termios-eio": (termios.error(errno.EIO, os.strerror(errno.EIO)), _words(errno.EIO)),
    "termios-message-only": (termios.error("tcsetattr failed"), "tcsetattr failed"),
}

# A port that opened and then failed: where it failed, and how.
TALK_FAILURES = {
    "vanishes-during-the-banner": ("read", 1, OSError(errno.EIO, os.strerror(errno.EIO)),
                                   _words(errno.EIO)),
    "vanishes-before-the-command": ("read", 2, OSError(errno.ENXIO, os.strerror(errno.ENXIO)),
                                    _words(errno.ENXIO)),
    "vanishes-mid-reply": ("read", 3, OSError(errno.EIO, os.strerror(errno.EIO)),
                           _words(errno.EIO)),
    "write-refused": ("write", 1, BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN)),
                      _words(errno.EAGAIN)),
    "write-on-a-gone-device": ("write", 1, OSError(errno.ENODEV, os.strerror(errno.ENODEV)),
                               _words(errno.ENODEV)),
    "termios-on-read": ("read", 2, termios.error(errno.EIO, os.strerror(errno.EIO)),
                        _words(errno.EIO)),
}


def _opener_raising(exc, opened=None):
    def opener(port):
        if opened is not None:
            opened.append(port)
        raise exc
    return opener


class FailingChannel(ScriptedChannel):
    """A channel that opened and then fails: its Nth read, or its Nth write, raises."""

    def __init__(self, where, nth, exc, *, close_exc=None):
        super().__init__({"device_info": DEVICE_INFO})
        self._where, self._left, self._exc, self._close_exc = where, nth, exc, close_exc

    def _tick(self, where):
        if where == self._where:
            self._left -= 1
            if self._left == 0:
                raise self._exc

    def read(self, n, timeout):
        self._tick("read")
        return super().read(n, timeout)

    def write(self, data):
        self._tick("write")
        super().write(data)

    def close(self):
        super().close()
        if self._close_exc is not None:
            raise self._close_exc


def _assert_refusal(reason, words, port=LONG_PORT):
    assert words in reason, reason
    name = os.path.basename(port)
    assert reason.count(name) == 1, reason                # the port, named once
    assert "Flipper" in reason, reason
    # Whole in the panel: its two lines of message, no word longer than a line.
    lines = textwrap.wrap(reason, PANEL_MESSAGE_CHARS, break_long_words=False)
    assert len(lines) <= FIXED_PANEL_MESSAGE_LINES, lines
    assert all(len(line) <= PANEL_MESSAGE_CHARS for line in lines), lines


@pytest.mark.parametrize("port", [LONG_PORT, SHORT_PORT], ids=["name8", "name3"])
@pytest.mark.parametrize("shape", OPEN_FAILURES)
def test_a_port_that_will_not_open_is_a_refusal_with_the_system_words(shape, port):
    exc, words = OPEN_FAILURES[shape]
    cli = FlipperCli(port=port, opener=_opener_raising(exc))
    with pytest.raises(FlipperError) as refused:
        cli.command("device_info")
    reason = str(refused.value)
    _assert_refusal(reason, words, port)
    assert SAYS_IT_WOULD_NOT_OPEN.search(reason), reason
    assert refused.value.__cause__ is exc                  # the system's error stays attached


@pytest.mark.parametrize("shape", TALK_FAILURES)
def test_a_port_that_fails_after_it_opened_is_a_refusal_and_is_released(shape):
    where, nth, exc, words = TALK_FAILURES[shape]
    channel = FailingChannel(where, nth, exc)
    cli = FlipperCli(port=LONG_PORT, opener=lambda port: channel)
    with pytest.raises(FlipperError) as refused:
        cli.command("device_info")
    _assert_refusal(str(refused.value), words)
    assert refused.value.__cause__ is exc
    # A session whose port failed holds no handle: the next read opens afresh.
    assert channel.closed is True


def test_a_port_that_fails_mid_file_is_a_refusal_and_is_released():
    exc = OSError(errno.EIO, os.strerror(errno.EIO))
    channel = FailingChannel("read", 3, exc)
    cli = FlipperCli(port=LONG_PORT, opener=lambda port: channel)
    with pytest.raises(FlipperError) as refused:
        cli.read_file("/ext/x.sub", 64)
    _assert_refusal(str(refused.value), _words(errno.EIO))
    assert channel.closed is True


def test_a_release_that_fails_too_keeps_the_reason_the_port_failed():
    # A gone device can fail its close as well; the refusal must still be the port's own failure,
    # not a bare OSError from the release that the route would answer with a 500.
    channel = FailingChannel("read", 2, OSError(errno.EIO, os.strerror(errno.EIO)),
                             close_exc=OSError(errno.EIO, os.strerror(errno.EIO)))
    with pytest.raises(FlipperError) as refused:
        with FlipperCli(port=LONG_PORT, opener=lambda port: channel) as cli:
            cli.command("device_info")
    _assert_refusal(str(refused.value), _words(errno.EIO))


def test_a_release_that_fails_after_a_good_read_keeps_the_reading():
    # The device answered; a close that fails afterwards releases the descriptor all the same
    # (close(2) on Linux), so the reading stands and nothing escapes as a server error.
    channel = FailingChannel("read", 0, None, close_exc=OSError(errno.EIO, os.strerror(errno.EIO)))
    with FlipperCli(port=LONG_PORT, opener=lambda port: channel) as cli:
        reply = cli.command("device_info")
    assert "Ex4mple" in reply
    assert channel.closed is True


class _Bug(TypeError):
    """A fault in the code, not the port: what a broad catch would hide as "lost the Flipper"."""


# Where a fault that is no port error can arise, and how: it must escape as itself, never be
# reworded as a refusal or swallowed, so a bug still shows as a bug.
NOT_PORT_ERRORS = {
    "the-open": lambda: FlipperCli(port=LONG_PORT, opener=_opener_raising(_Bug("opener bug"))),
    "the-banner-wait": lambda: FlipperCli(
        port=LONG_PORT, opener=lambda port: FailingChannel("read", 1, _Bug("read bug"))),
    "a-read": lambda: FlipperCli(
        port=LONG_PORT, opener=lambda port: FailingChannel("read", 3, _Bug("read bug"))),
    "a-write": lambda: FlipperCli(
        port=LONG_PORT, opener=lambda port: FailingChannel("write", 1, _Bug("write bug"))),
    "the-release": lambda: FlipperCli(
        port=LONG_PORT, opener=lambda port: FailingChannel("read", 0, None,
                                                           close_exc=_Bug("close bug"))),
}


@pytest.mark.parametrize("where", NOT_PORT_ERRORS)
def test_a_fault_that_is_no_port_error_escapes_as_itself(where):
    with pytest.raises(_Bug) as escaped:
        with NOT_PORT_ERRORS[where]() as cli:
            cli.command("device_info")
    assert not isinstance(escaped.value, FlipperError)


@pytest.mark.parametrize("error_type", [_Bug, KeyboardInterrupt])
def test_a_startup_banner_fault_closes_the_port_and_reraises(error_type):
    fault = error_type("banner read failed")
    channel = FailingChannel("read", 1, fault)
    with pytest.raises(error_type) as escaped:
        with FlipperCli(port=LONG_PORT, opener=lambda port: channel):
            pytest.fail("the session opened despite the banner fault")
    assert escaped.value is fault
    assert channel.closed is True


def test_an_error_with_words_but_no_number_does_not_show_errno_none():
    exc, words = OPEN_FAILURES["strerror-no-errno"]
    with pytest.raises(FlipperError) as refused:
        FlipperCli(port=LONG_PORT, opener=_opener_raising(exc)).command("device_info")
    assert "Errno None" not in str(refused.value), str(refused.value)
    assert words in str(refused.value)


def _open_fds():
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.parametrize("kind", ["missing-path", "regular-file", "directory"])
def test_a_real_open_that_fails_is_a_refusal_and_leaves_no_descriptor(tmp_path, kind):
    # Real os.open and tty.setraw, on paths that are no serial device: a missing path (ENOENT), a
    # plain file (opens, then setraw raises termios.error ENOTTY), a directory (EISDIR).
    port = tmp_path / LONG_NAME
    if kind == "regular-file":
        port.write_bytes(b"")
    elif kind == "directory":
        port.mkdir()
    words = {"missing-path": _words(errno.ENOENT), "regular-file": _words(errno.ENOTTY),
             "directory": _words(errno.EISDIR)}[kind]
    before = _open_fds()
    with pytest.raises(FlipperError) as refused:
        with FlipperCli(port=str(port), timeout=0.2) as cli:
            cli.command("device_info")
    reason = str(refused.value)
    assert SAYS_IT_WOULD_NOT_OPEN.search(reason), reason
    assert words in reason, reason
    assert reason.count(str(port)) == 1, reason            # not a by-id name: the path, once
    assert _open_fds() == before                           # setraw's failure leaks no handle


def test_a_real_pty_that_hangs_up_mid_session_is_a_refusal_and_its_port_is_released():
    master, slave = os.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    before = _open_fds()
    try:
        with pytest.raises(FlipperError) as refused:
            with FlipperCli(port=path, timeout=0.2) as cli:
                os.close(master)                           # the device goes away
                master = None
                cli.command("device_info", wait=0.2)
    finally:
        if master is not None:
            os.close(master)
    reason = str(refused.value)
    assert _words(errno.EIO) in reason, reason
    assert reason.count(path) == 1, reason
    assert _open_fds() == before - 1                       # the master is gone, the slave released


def test_the_raw_channel_closes_its_descriptor_when_raw_mode_fails(tmp_path):
    # os.open succeeds on a plain file and tty.setraw then refuses it: the descriptor os.open gave
    # must not outlive the failure, or every poll of a wrong port leaks one more.
    port = tmp_path / "plain"
    port.write_bytes(b"")
    before = _open_fds()
    with pytest.raises(termios.error):
        _TtyChannel(str(port))
    assert _open_fds() == before


# --- through the route and the panel ---

def _serve(tmp_path, console):
    """The console as the kiosk runs it: a fault the route does not handle answers 500."""
    app = create_app(pack_dir=FIXTURE_PACKS, data_dir=tmp_path, probe=_absent,
                     flipper_factory=lambda: console)
    return TestClient(app, raise_server_exceptions=False)


def _console(cli_factory, port=LONG_PORT):
    return FlipperConsole(probe=probe_flipper, cli_factory=cli_factory,
                          glob=lambda pattern: [port])


def _panel_shows(status, text):
    """The answer put through lesson 29's panel script (flipper.js, decoder-panel.js) in node."""
    node = shutil.which("node")
    assert node, "node is not on PATH, so the panel's own script cannot be run"
    result = subprocess.run([node, str(Path(__file__).parent / "flipper_poll.js")],
                            input=json.dumps([status, text]), capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


ROUTE_CASES = {
    "open-enoent-with-filename": ("open", OPEN_FAILURES["enoent-with-filename"]),
    "open-eacces": ("open", OPEN_FAILURES["eacces-permission"]),
    "open-ebusy-no-filename": ("open", OPEN_FAILURES["ebusy-no-filename"]),
    "open-message-only": ("open", OPEN_FAILURES["message-only"]),
    "open-termios-enotty": ("open", OPEN_FAILURES["termios-enotty"]),
    "vanishes-mid-reply": ("talk", TALK_FAILURES["vanishes-mid-reply"]),
    "write-on-a-gone-device": ("talk", TALK_FAILURES["write-on-a-gone-device"]),
}


@pytest.mark.parametrize("case", ROUTE_CASES)
def test_the_route_refuses_a_failing_port_and_the_panel_shows_why(tmp_path, case):
    kind, spec = ROUTE_CASES[case]
    if kind == "open":
        exc, words = spec
        factory = lambda: FlipperCli(port=LONG_PORT, opener=_opener_raising(exc))  # noqa: E731
    else:
        where, nth, exc, words = spec
        factory = lambda: FlipperCli(  # noqa: E731
            port=LONG_PORT, opener=lambda port: FailingChannel(where, nth, exc))
    with _serve(tmp_path, _console(factory)) as client:
        resp = client.get("/api/flipper")
    assert resp.status_code == 503, (resp.status_code, resp.text)
    body = resp.json()
    assert body["available"] is False and body["device"] is None and body["command"] is None
    _assert_refusal(body["reason"], words)
    shown = _panel_shows(resp.status_code, resp.text)
    assert shown["badge"] == "UNAVAILABLE", shown
    assert shown["empty"] == body["reason"], shown         # the reason, whole, as the route gave it
    assert shown["emptyHidden"] is False and shown["cardHidden"] is True, shown
    assert shown["nextPollMs"] == [RETRY_MS], shown


def test_a_real_missing_port_through_the_route_is_refused_with_its_reason(tmp_path):
    port = tmp_path / LONG_NAME                            # no such path: os.open raises ENOENT
    console = _console(lambda: FlipperCli(port=str(port), timeout=0.2), port=str(port))
    with _serve(tmp_path, console) as client:
        resp = client.get("/api/flipper")
    assert resp.status_code == 503, (resp.status_code, resp.text)
    assert _words(errno.ENOENT) in resp.json()["reason"]
    assert _panel_shows(resp.status_code, resp.text)["empty"] == resp.json()["reason"]


class _Faulting:
    """A console whose read fails in a way the route does not refuse: a fault in the console."""

    def device(self):
        raise RuntimeError("a fault in the console")


def test_a_server_fault_is_shown_as_no_reading_and_polled_again(tmp_path):
    with _serve(tmp_path, _Faulting()) as client:
        resp = client.get("/api/flipper")
    assert resp.status_code == 500                         # not a refusal: a fault, logged as one
    shown = _panel_shows(resp.status_code, resp.text)
    reading = _panel_shows(None, "")                       # not even an answer: the same rule
    for down in (shown, reading):
        assert down["badge"] == "UNAVAILABLE", down
        assert "Checking receiver" not in down["empty"], down
        assert "no Flipper on USB" not in down["empty"], down   # an error is not an absence
        assert down["emptyHidden"] is False and down["cardHidden"] is True, down
        assert down["nextPollMs"] == [RETRY_MS], down
    assert "500" in shown["empty"], shown
    assert reading["empty"] == "Lost contact with the console.", reading
