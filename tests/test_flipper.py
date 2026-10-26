# ABOUTME: Unit tests for the Flipper Zero USB-CDC serial transport.
# ABOUTME: The bare-CR terminator, the connect banner, and the ambiguity refusal are guarded here.

import pytest

from sigint101.flipper import (
    FlipperCli,
    FlipperProbe,
    FlipperSelectionError,
    find_flipper_port,
    probe_flipper,
)

BY_ID = "/dev/serial/by-id/usb-Flipper_Devices_Inc._Flipper_Lle0zik_flip_Lle0zik-if00"
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
    assert "Lle0zik" in message and "Zzz9xyz" in message


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
