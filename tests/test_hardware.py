# ABOUTME: Unit tests for the RTL-SDR presence probe.
# ABOUTME: "Present" means a real RTL2832U is on the USB bus — never just tools-installed.

import subprocess

import pytest

from sigint101.hardware import (
    DeviceSelectionError,
    list_rtlsdr_devices,
    probe_rtlsdr,
    select_rtlsdr_index,
)

# Verbatim-shaped stderr from rtl_power with more than one RTL-SDR dongle on the bus: the
# Nomad's built-in radio (SN 00000001) plus two extra dongles someone plugged in. The
# serials are NOT in index order and a serial like "00000100" is a *string*, unrelated to
# its index — index and serial are genuinely independent.
MULTIPLE_DEVICES = """Found 3 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001
  1:  Realtek, RTL2838UHIDIR, SN: 00000100
  2:  Realtek, RTL2838UHIDIR, SN: 00000007

No matching devices found.
Number of frequency hops: 1
Reporting every 10 seconds
"""

ONE_DEVICE = """Found 1 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001

No matching devices found.
"""


def _enumerator(stderr):
    class Result:
        pass
    Result.stderr = stderr
    Result.stdout = ""
    return lambda *a, **k: Result()


def _which_for(*available):
    have = set(available)
    return lambda name: f"/usr/bin/{name}" if name in have else None


def test_dongle_on_usb_is_present():
    class Result:
        stdout = "Bus 001 Device 004: ID 0bda:2838 Realtek RTL2838 DVB-T"

    probe = probe_rtlsdr(which=_which_for("lsusb"), run=lambda *a, **k: Result())
    assert probe.present is True
    assert probe.method == "lsusb"


def test_lsusb_without_dongle_is_absent():
    class Result:
        stdout = "Bus 001 Device 002: ID 1d6b:0002 Linux Foundation 2.0 root hub"

    probe = probe_rtlsdr(which=_which_for("lsusb"), run=lambda *a, **k: Result())
    assert probe.present is False
    assert probe.method == "lsusb"


def test_tools_without_device_are_not_a_radio():
    # rtl_test present but no lsusb and no device → tools installed, not a radio.
    probe = probe_rtlsdr(which=_which_for("rtl_test"), run=lambda *a, **k: None)
    assert probe.present is False
    assert probe.method == "rtl_test"


def test_lsusb_is_authoritative_even_with_tools_present():
    # lsusb ran and found no dongle → authoritative negative, regardless of tools.
    class Result:
        stdout = "Bus 001 Device 002: ID 1d6b:0002 root hub"

    probe = probe_rtlsdr(which=_which_for("lsusb", "rtl_test"), run=lambda *a, **k: Result())
    assert probe.present is False
    assert probe.method == "lsusb"


def test_nothing_installed_is_absent():
    probe = probe_rtlsdr(which=lambda name: None)
    assert probe.present is False
    assert probe.method == "none"


def test_a_probe_that_crashes_is_absent_not_a_radio():
    # lsusb is present but the call itself fails — that is an absence of proof, not proof of
    # a radio, so the probe reports not-present rather than guessing a device into existence.
    def crash(*a, **k):
        raise OSError("lsusb blew up")

    probe = probe_rtlsdr(which=_which_for("lsusb"), run=crash)
    assert probe.present is False
    assert probe.method == "lsusb"


def test_every_radio_on_the_bus_is_listed_with_its_index():
    devices = list_rtlsdr_devices(which=_which_for("rtl_power"), run=_enumerator(MULTIPLE_DEVICES))
    assert [d.index for d in devices] == [0, 1, 2]
    assert [d.serial for d in devices] == ["00000001", "00000100", "00000007"]


def test_enumeration_needs_no_radio_to_be_opened():
    # The sentinel selector matches nothing, so rtl_power lists the bus and exits without
    # claiming a device — the console must never take the dongle just to look at it.
    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd

        class Result:
            stderr = MULTIPLE_DEVICES
            stdout = ""
        return Result()

    list_rtlsdr_devices(which=_which_for("rtl_power"), run=run)
    assert "-1" in seen["cmd"], "must ask for a single pass, not an open-ended sweep"


def test_a_single_radio_needs_no_serial_to_be_chosen():
    devices = list_rtlsdr_devices(which=_which_for("rtl_power"), run=_enumerator(ONE_DEVICE))
    assert select_rtlsdr_index(None, devices=devices) == 0


def test_more_than_one_radio_without_a_pinned_serial_is_refused():
    devices = list_rtlsdr_devices(which=_which_for("rtl_power"), run=_enumerator(MULTIPLE_DEVICES))
    with pytest.raises(DeviceSelectionError):
        select_rtlsdr_index(None, devices=devices)


def test_the_refusal_names_the_serials_it_found():
    devices = list_rtlsdr_devices(which=_which_for("rtl_power"), run=_enumerator(MULTIPLE_DEVICES))
    with pytest.raises(DeviceSelectionError) as caught:
        select_rtlsdr_index(None, devices=devices)
    message = str(caught.value)
    # A refusal the learner cannot act on is just a failure. Name every candidate.
    for serial in ("00000001", "00000100", "00000007"):
        assert serial in message


def test_a_numeric_serial_resolves_to_its_own_index_not_that_number():
    # The whole reason this code exists: librtlsdr reads "-d 00000100" as index 100, and
    # "-d 00000001" as index 1 — neither the radio whose serial that string actually is.
    # Resolving the serial to its own index is what makes pinning a radio mean what it says.
    devices = list_rtlsdr_devices(which=_which_for("rtl_power"), run=_enumerator(MULTIPLE_DEVICES))
    assert select_rtlsdr_index("00000001", devices=devices) == 0
    assert select_rtlsdr_index("00000100", devices=devices) == 1
    assert select_rtlsdr_index("00000007", devices=devices) == 2


def test_a_pinned_serial_that_is_not_on_the_bus_is_refused():
    devices = list_rtlsdr_devices(which=_which_for("rtl_power"), run=_enumerator(MULTIPLE_DEVICES))
    with pytest.raises(DeviceSelectionError):
        select_rtlsdr_index("NOSUCHRADIO", devices=devices)


def test_no_radio_at_all_is_refused():
    with pytest.raises(DeviceSelectionError):
        select_rtlsdr_index(None, devices=())


def test_a_bus_that_cannot_be_surveyed_is_not_reported_as_an_empty_bus():
    # "The survey failed" and "there are no radios" are different facts, and conflating
    # them is how a working radio gets reported as absent — the same lie the sweep-death
    # fix removed, one layer down.
    with pytest.raises(DeviceSelectionError) as caught:
        list_rtlsdr_devices(which=_which_for(), run=_enumerator(MULTIPLE_DEVICES))
    assert "no RTL-SDR" not in str(caught.value)


def test_an_enumeration_that_crashes_says_so_rather_than_claiming_no_radio():
    def crash(*a, **k):
        raise subprocess.SubprocessError("boom")

    with pytest.raises(DeviceSelectionError) as caught:
        list_rtlsdr_devices(which=_which_for("rtl_power"), run=crash)
    assert "no RTL-SDR" not in str(caught.value)


def test_a_surveyed_bus_with_no_radios_is_genuinely_empty():
    assert list_rtlsdr_devices(which=_which_for("rtl_power"),
                               run=_enumerator("Found 0 device(s):\n")) == ()


def test_a_radio_with_no_serial_is_still_counted():
    # librtlsdr prints "SN: " with nothing after it when a device carries no serial string.
    # Dropping that line would undercount the bus, and an undercounted bus stops looking
    # ambiguous — so the console would quietly pick a radio instead of refusing.
    blank = ("Found 2 device(s):\n"
             "  0:  Realtek, RTL2838UHIDIR, SN: 00000001\n"
             "  1:  Realtek, RTL2838UHIDIR, SN: \n")
    devices = list_rtlsdr_devices(which=_which_for("rtl_power"), run=_enumerator(blank))
    assert len(devices) == 2
    assert devices[1].serial == ""
    with pytest.raises(DeviceSelectionError):
        select_rtlsdr_index(None, devices=devices)


def test_a_blank_serial_can_never_be_selected():
    blank = ("Found 2 device(s):\n"
             "  0:  Realtek, RTL2838UHIDIR, SN: 00000001\n"
             "  1:  Realtek, RTL2838UHIDIR, SN: \n")
    devices = list_rtlsdr_devices(which=_which_for("rtl_power"), run=_enumerator(blank))
    with pytest.raises(DeviceSelectionError):
        select_rtlsdr_index("", devices=devices)
