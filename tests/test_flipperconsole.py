# ABOUTME: Tests for FlipperConsole.device() — the kiosk read layer over the existing transport.
# ABOUTME: Presence/absence/ambiguity come from probe_flipper; an open app is an honest refusal.

from pathlib import Path

import pytest

from sigint101.flipper import FlipperError, FlipperSelectionError, probe_flipper
from sigint101.flipperconsole import APP_OPEN_MARKER, FlipperConsole

FIXTURE = (Path(__file__).parent / "fixtures" / "flipper" / "device_info.txt").read_text()

BY_ID = "/dev/serial/by-id/usb-Flipper_Devices_Inc._Flipper_Lle0zik_flip_Lle0zik-if00"
SECOND = "/dev/serial/by-id/usb-Flipper_Devices_Inc._Flipper_Zzz9xyz_flip_Zzz9xyz-if00"


class _FakeCli:
    """A stand-in FlipperCli context manager that returns a canned reply to `command`."""

    def __init__(self, reply):
        self._reply = reply

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def command(self, text, *, wait=None):
        return self._reply


def _globber(*paths):
    return lambda pattern: list(paths)


def _console(reply=FIXTURE, present=(BY_ID,)):
    return FlipperConsole(
        probe=probe_flipper,
        cli_factory=lambda: _FakeCli(reply),
        glob=_globber(*present),
    )


def test_device_returns_parsed_dict_when_present():
    assert _console().device()["firmware"] == "1.4.3"
    assert _console().device()["region"] == "US"


def test_device_absent_raises_flippererror():
    with pytest.raises(FlipperError):
        _console(present=()).device()


def test_device_two_attached_raises_selection_error():
    with pytest.raises(FlipperSelectionError):
        _console(present=(BY_ID, SECOND)).device()


def test_device_app_open_raises_flippererror():
    reply = f"this command {APP_OPEN_MARKER}"
    with pytest.raises(FlipperError):
        _console(reply=reply).device()
