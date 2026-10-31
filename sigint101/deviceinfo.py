# ABOUTME: Parses the Flipper `device_info` CLI reply into the identity the M29 panel shows.
# ABOUTME: Tolerant of a missing key — a firmware variant omits one rather than crashing the panel.

from __future__ import annotations

from dataclasses import dataclass

# The device_info reply is a block of `key : value` lines. These are the handful the panel reads;
# everything else (the radio-stack versions, the enclave counters) stays in `raw` for a later need
# without widening what the kiosk depends on.
_MODEL = "hardware_model"
_NAME = "hardware_name"
_FIRMWARE = "firmware_version"
# The human-readable region is the *provisioned* key ("US"), not `hardware_region`, which is the
# numeric code (4). Showing "4" to a learner would be a worse answer than showing nothing.
_REGION = "hardware_region_provisioned"
_RADIO = "radio_alive"


@dataclass(frozen=True)
class DeviceInfo:
    """What the Flipper reports about itself, reduced to what the panel displays.

    `raw` keeps the whole reply so a later panel can read another field without this parser
    growing a new attribute for each one.
    """

    model: str
    name: str
    firmware: str
    region: str
    radio_alive: bool
    raw: dict

    def as_dict(self) -> dict:
        """The panel payload — the five display fields, not the whole raw block."""
        return {
            "model": self.model,
            "name": self.name,
            "firmware": self.firmware,
            "region": self.region,
            "radio_alive": self.radio_alive,
        }


def parse_device_info(text: str) -> DeviceInfo:
    """Read a `device_info` reply into a DeviceInfo.

    Every line is split on its first colon; a line without one (the echoed command, a blank) is
    skipped. A key the reply does not carry resolves to the empty string rather than raising, so
    a firmware that renames or drops a field degrades to a thinner panel, never a crash.
    """
    raw: dict = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        raw[key.strip()] = value.strip()
    return DeviceInfo(
        model=raw.get(_MODEL, ""),
        name=raw.get(_NAME, ""),
        firmware=raw.get(_FIRMWARE, ""),
        region=raw.get(_REGION, ""),
        radio_alive=raw.get(_RADIO) == "true",
        raw=raw,
    )
