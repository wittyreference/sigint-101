# ABOUTME: Tests for the Flipper device_info parser (M29) against a real captured CLI block.
# ABOUTME: Tolerant of a missing key so a firmware variant never crashes the panel.

from pathlib import Path

from sigint101.deviceinfo import DeviceInfo, parse_device_info

FIXTURE = (Path(__file__).parent / "fixtures" / "flipper" / "device_info.txt").read_text()


def test_parse_device_info_reads_core_fields():
    info = parse_device_info(FIXTURE)
    assert isinstance(info, DeviceInfo)
    assert info.model == "Flipper Zero"
    assert info.name == "Lle0zik"
    assert info.firmware == "1.4.3"
    assert info.region == "US"  # from hardware_region_provisioned, not the numeric hardware_region
    assert info.radio_alive is True


def test_parse_device_info_tolerates_missing_key():
    block = "hardware_model : Flipper Zero\nfirmware_version : 1.4.3\nradio_alive : true\n"
    info = parse_device_info(block)
    assert info.region == ""  # no hardware_region_provisioned -> empty, never a KeyError
    assert info.firmware == "1.4.3"
    assert info.model == "Flipper Zero"


def test_as_dict_is_the_panel_payload():
    d = parse_device_info(FIXTURE).as_dict()
    assert d == {
        "model": "Flipper Zero",
        "name": "Lle0zik",
        "firmware": "1.4.3",
        "region": "US",
        "radio_alive": True,
    }
