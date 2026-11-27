# ABOUTME: Probes for real RF hardware (RTL-SDR) presence. Reports absence honestly — never mocks.
# ABOUTME: Lesson 1's OPERATE step stays gated until a genuine radio is detected on the Nomad.

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

# RealTek RTL2832U — the chip inside common RTL-SDR / DVB-T dongles (incl. the Nomad's).
RTLSDR_USB_IDS = ("0bda:2838", "0bda:2832")

# Pin a radio by serial because its index can change with plug order.
RTLSDR_SERIAL_ENV = "SIGINT101_RTLSDR_SERIAL"

# The sweeper's binary. Enumeration asks the very program that will do the sweeping how it
# numbers the bus, so the index SIGINT 101 picks is the index rtl_power will honour.
RTL_POWER = "rtl_power"

# The decoder's binary. rtl_433 holds the same dongle rtl_power does, one program at a time,
# so the decode panel and the waterfall take turns on the radio (Sweeper.release/resume).
RTL_433 = "rtl_433"

# The RDS decode pipeline (M12): rtl_fm demodulates the FM multiplex, redsea pulls the RDS data from
# the 57 kHz subcarrier. Both hold the one dongle, so the RDS panel takes turns with the sweep too.
RTL_FM = "rtl_fm"
REDSEA = "redsea"

# The ADS-B decoder (M13). dump1090 demodulates Mode S at 1090 MHz and serves decoded aircraft as
# BaseStation (SBS-1) text; it holds the same one dongle, so the radar panel takes its turn too.
DUMP1090 = "dump1090-fa"

# The APRS decoder (M14). rtl_fm demodulates the 2m FM packet channel, multimon-ng decodes the
# AFSK1200 AX.25 frames; both hold the one dongle, so the APRS panel takes its turn on the sweep.
MULTIMON = "multimon-ng"

# The ACARS decoder (M16). acarsdec opens the RTL-SDR directly on the VHF ACARS channels and prints
# decoded aircraft messages as JSON; it holds the one dongle, so the ACARS panel takes its turn too.
ACARSDEC = "acarsdec"

# A selector that cannot match any real serial. Given one, rtl_power prints the whole device
# list and exits without claiming a radio — which is how the bus gets surveyed without taking
# the dongle away from a lesson in progress.
ENUMERATION_SENTINEL = "sigint101-enumerate-only"

# The survey runs while the sweeper holds its lifecycle lock, so this ceiling is also how
# long a hung rtl_power could delay handing the dongle back to `make listen`. Measured cost
# on the Nomad is 0.12 s; five seconds is headroom, not an expectation.
ENUMERATION_TIMEOUT_S = 5

# rtl_power lists each radio as "  0:  Realtek, RTL2838UHIDIR, SN: 00000001". The serial
# group is allowed to be empty because a device carrying no serial string prints "SN: "
# with nothing after it, and a line dropped there would undercount the bus — an
# undercounted bus stops looking ambiguous, which is exactly when guessing resumes.
_DEVICE_LINE = re.compile(r"^\s*(\d+):\s*.*,\s*SN:\s*(\S*)\s*$")


@dataclass(frozen=True)
class RadioProbe:
    """What we found when we looked for a radio, and how we looked.

    `method` is carried so the console can say *how* it knows — "detected via lsusb" is
    a claim a learner can check, where a bare "detected" is one they must take on faith.
    """

    present: bool
    method: str
    detail: str
    known: bool = True


def probe_rtlsdr(*, which=shutil.which, run=subprocess.run) -> RadioProbe:
    """Detect an actual RTL-SDR on the USB bus.

    Presence means a device is physically attached — not merely that the rtl-sdr
    tools are installed. Absence is a real answer, and the OPERATE step stays gated
    until a genuine radio shows up.
    """
    # Truth: an RTL2832U physically present on the USB bus.
    if which("lsusb"):
        try:
            result = run(["lsusb"], capture_output=True, text=True, timeout=5, check=True)
            text = (result.stdout or "").lower()
            for usb_id in RTLSDR_USB_IDS:
                if usb_id in text:
                    return RadioProbe(True, "lsusb", f"RTL2832U {usb_id} on USB bus")
            return RadioProbe(False, "lsusb", "no RTL2832U on USB bus")
        except (subprocess.SubprocessError, OSError) as exc:
            return RadioProbe(False, "lsusb", f"probe failed: {exc}", known=False)

    # No lsusb available: tools may exist, but that is not proof of an attached radio.
    if which("rtl_test"):
        return RadioProbe(False, "rtl_test",
                          "rtl-sdr tools installed, but cannot confirm a device (no lsusb)",
                          known=False)

    return RadioProbe(False, "none", "no lsusb and no rtl-sdr tools found", known=False)


@dataclass(frozen=True)
class RtlSdrDevice:
    """One radio as librtlsdr numbers it.

    Index and serial are carried separately because they are genuinely independent: librtlsdr
    assigns the index from its own USB discovery order, so a serial says nothing about where
    in the list its radio sits, and the index can change the next time the bus is enumerated.
    """

    index: int
    serial: str


class DeviceSelectionError(RuntimeError):
    """Raised when which radio the learner means cannot be determined.

    Separate from "no radio present": the console can see radios, it just cannot tell which
    one is the subject of the lesson. Guessing would sweep an arbitrary tuner and draw a
    perfectly real waterfall of the wrong thing, which is the no-mock invariant broken in
    the one way a blank panel would not reveal.
    """


def list_rtlsdr_devices(*, which=shutil.which, run=subprocess.run) -> tuple:
    """Every RTL-SDR on the bus, in the order rtl_power numbers them.

    Raises rather than returning an empty tuple when the survey itself cannot be run: an
    empty result must mean "looked, found nothing", never "could not look".

    Enumeration is a survey, not a claim: the sentinel selector matches nothing, so
    rtl_power prints its inventory and exits without opening a device. Asking rtl_power
    rather than lsusb is deliberate — lsusb sorts by bus and device number, librtlsdr
    numbers by its own discovery order, and it is librtlsdr's numbering that `-d` obeys.
    """
    if not which(RTL_POWER):
        raise DeviceSelectionError(
            f"cannot survey the bus: {RTL_POWER} is not installed, so SIGINT 101 cannot tell "
            "which radios are attached")
    try:
        # A one-shot sweep of a 1 MHz span: the arguments have to parse, but the device
        # lookup fails first, so none of it is ever run.
        result = run([RTL_POWER, "-d", ENUMERATION_SENTINEL,
                      "-f", "88M:89M:1M", "-1", os.devnull],
                     capture_output=True, text=True, timeout=ENUMERATION_TIMEOUT_S)
    except (subprocess.SubprocessError, OSError) as exc:
        # Returning an empty list here would say "there are no radios", which is a claim
        # about the bus this code just failed to look at.
        raise DeviceSelectionError(f"cannot survey the bus: {exc}") from exc
    # rtl_power narrates to stderr, inventory included.
    devices = []
    for line in (getattr(result, "stderr", "") or "").splitlines():
        matched = _DEVICE_LINE.match(line)
        if matched:
            devices.append(RtlSdrDevice(int(matched.group(1)), matched.group(2)))
    return tuple(devices)


def describe_devices(devices) -> str:
    """Name every radio found, so a refusal is something the learner can act on."""
    return ", ".join(f"index {d.index} serial {d.serial or '(none)'}" for d in devices)


def select_rtlsdr_index(serial, *, devices) -> int:
    """Turn the radio the console wants into the index rtl_power needs, or refuse."""
    return _select_rtlsdr_device(serial, devices).index


def _select_rtlsdr_device(serial, devices) -> RtlSdrDevice:
    """Resolve which enumerated radio a tool should open.

    A serial cannot be handed to `-d` directly. librtlsdr parses that argument as an
    integer index first and only falls back to matching serials, so the Nomad's own
    "00000001" is read as index 1 — a different radio when the bus holds more than one, and
    no device at all when it does not. Looking the serial up here and passing the resulting
    index is what makes pinning a radio mean what it says.

    With several radios on the bus and none pinned, this refuses rather than defaulting.
    Index 0 is whichever device enumerated first, which is an accident of plug order, and
    a lesson swept from an arbitrary radio would look entirely healthy while being wrong.
    """
    if not devices:
        raise DeviceSelectionError(
            "no RTL-SDR on the USB bus — SIGINT 101 will not fake a signal")
    if serial:
        for device in devices:
            if device.serial == serial:
                return device
        raise DeviceSelectionError(
            f"no radio on this bus has serial {serial}. Found: {describe_devices(devices)}")
    if len(devices) == 1:
        return devices[0]
    raise DeviceSelectionError(
        "more than one RTL-SDR is on the bus and none is pinned. "
        f"Found: {describe_devices(devices)}. "
        "Set SIGINT101_RTLSDR_SERIAL to one of those serials "
        "(not the index: it follows plug order).")


def rtlsdr_radio_for_run(index, serial, list_devices) -> dict:
    """Snapshot the index and serial together using the enumeration that chose the radio."""
    if list_devices is not None:
        device = _select_rtlsdr_device(serial, list_devices())
        index, serial = device.index, device.serial
    else:
        # A receiver built without an enumerator keeps its fixed index and any configured serial.
        serial = serial or os.environ.get(RTLSDR_SERIAL_ENV)
    return {"kind": "rtl-sdr", "index": index, "serial": serial or None}
