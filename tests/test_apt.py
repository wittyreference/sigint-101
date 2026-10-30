# ABOUTME: Tests the NOAA APT backend — orchestrating satdump to write a decoded PNG, discovered
# ABOUTME: newest-first. satdump is a fake at the boundary; no radio / no satdump is honest.

import os

import pytest

from sigint101 import apt
from sigint101.apt import (
    APT_PIPELINE,
    DEFAULT_APT_HZ,
    SATDUMP,
    AptDecoder,
    AptUnavailable,
    satellite_for,
)
from sigint101.hardware import RtlSdrDevice


class FakeProcess:
    """Stands in for the satdump process at the boundary (it writes PNGs and exits on timeout)."""

    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _decoder(tmp_path, **kw):
    kw.setdefault("output_dir", tmp_path / "apt")
    kw.setdefault("radio_present", lambda: True)
    kw.setdefault("which", lambda name: "/usr/bin/" + name)
    kw.setdefault("spawn", lambda freq, device, output_dir: FakeProcess())
    return AptDecoder(**kw)


def _drop_png(directory, name, *, mtime=None):
    """Write a tiny stand-in PNG — a test double at the tool boundary for a completed decode."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    # The 8-byte PNG signature is enough to be a real file on disk; images() never reads pixels.
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_popen_builds_the_satdump_apt_live_command(tmp_path, monkeypatch):
    captured = {}

    class Rec:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw

    monkeypatch.setattr(apt.subprocess, "Popen", Rec)
    apt._popen(DEFAULT_APT_HZ, device=2, output_dir=tmp_path)
    argv = captured["argv"]
    assert argv[0] == SATDUMP
    assert argv[1] == "live"
    assert argv[2] == APT_PIPELINE
    assert argv[3] == str(tmp_path)
    assert argv[argv.index("--source") + 1] == "rtlsdr"
    assert argv[argv.index("--source_id") + 1] == "2"
    assert argv[argv.index("--frequency") + 1] == str(DEFAULT_APT_HZ)
    assert "--timeout" in argv
    # satdump narrates heavily; its chatter is silenced so the console output stays pristine.
    assert captured["kw"]["stdout"] == apt.subprocess.DEVNULL
    assert captured["kw"]["stderr"] == apt.subprocess.DEVNULL


def test_start_passes_the_default_device_to_the_spawn(tmp_path):
    captured = {}

    def spawn(freq, device, output_dir):
        captured.update(freq=freq, device=device, output_dir=output_dir)
        return FakeProcess()

    dec = _decoder(tmp_path, device=5, freq_hz=DEFAULT_APT_HZ, spawn=spawn)
    dec.start()
    assert captured["device"] == 5
    assert captured["freq"] == DEFAULT_APT_HZ
    assert captured["output_dir"] == tmp_path / "apt"


def test_start_selects_the_pinned_radio_by_serial(tmp_path):
    captured = {}

    def spawn(freq, device, output_dir):
        captured["device"] = device
        return FakeProcess()

    devices = (RtlSdrDevice(3, "AAAA"), RtlSdrDevice(7, "ZZZZ"))
    dec = _decoder(tmp_path, serial="ZZZZ", list_devices=lambda: devices, spawn=spawn)
    dec.start()
    assert captured["device"] == 7  # the index that carries the pinned serial, not plug order


def test_start_refuses_when_the_radio_is_ambiguous(tmp_path):
    devices = (RtlSdrDevice(0, "AAAA"), RtlSdrDevice(1, "BBBB"))
    dec = _decoder(tmp_path, list_devices=lambda: devices)  # two radios, none pinned
    with pytest.raises(AptUnavailable):
        dec.start()


def test_images_discovers_a_decoded_png_newest_first(tmp_path):
    dec = _decoder(tmp_path)
    out = tmp_path / "apt"
    _drop_png(out, "pass-a.png", mtime=1_000_000)
    _drop_png(out, "pass-b.png", mtime=2_000_000)
    rows = dec.images()
    assert [r["path"] for r in rows] == ["apt/pass-b.png", "apt/pass-a.png"]  # newest first
    newest = rows[0]
    assert newest["satellite"] == "NOAA-19"           # 137.1 MHz
    assert newest["received_at"].startswith("1970")   # mtime rendered as an ISO-8601 instant
    assert newest["meta"]["size_bytes"] == 8


def test_images_is_empty_when_no_pass_has_decoded(tmp_path):
    dec = _decoder(tmp_path)  # output_dir does not even exist yet
    assert dec.images() == []  # the honest answer — never a placeholder image


def test_images_honours_a_limit(tmp_path):
    dec = _decoder(tmp_path)
    out = tmp_path / "apt"
    for i in range(3):
        _drop_png(out, f"pass-{i}.png", mtime=1_000_000 + i)
    assert len(dec.images(limit=2)) == 2


def test_images_finds_pngs_in_per_satellite_subdirectories(tmp_path):
    dec = _decoder(tmp_path)
    _drop_png(tmp_path / "apt" / "NOAA-19", "avhrr.png", mtime=1_500_000)
    rows = dec.images()
    assert rows[0]["path"] == "apt/NOAA-19/avhrr.png"


def test_decoder_refuses_without_a_radio(tmp_path):
    dec = _decoder(tmp_path, radio_present=lambda: False)
    with pytest.raises(AptUnavailable):
        dec.start()


def test_decoder_refuses_when_satdump_is_missing(tmp_path):
    dec = _decoder(tmp_path, which=lambda name: None if name == SATDUMP else "/usr/bin/" + name)
    with pytest.raises(AptUnavailable):
        dec.start()


def test_decoder_refuses_once_released_until_resumed(tmp_path):
    started = []
    dec = _decoder(tmp_path, spawn=lambda f, d, o: started.append(1) or FakeProcess())
    dec.release()
    with pytest.raises(AptUnavailable):
        dec.start()
    assert started == []  # a released decoder never touches the dongle
    dec.resume()
    dec.start()
    assert started == [1]


def test_release_keeps_captured_images(tmp_path):
    # Unlike the in-memory message logs, a decoded PNG is a real reception on disk; releasing the
    # dongle must not delete it.
    dec = _decoder(tmp_path)
    _drop_png(tmp_path / "apt", "pass.png", mtime=1_000_000)
    dec.start()
    dec.release()
    assert [r["path"] for r in dec.images()] == ["apt/pass.png"]


def test_start_is_idempotent_while_running(tmp_path):
    spawns = []
    dec = _decoder(tmp_path, spawn=lambda f, d, o: spawns.append(1) or FakeProcess())
    dec.start()
    dec.start()
    assert spawns == [1]  # already running — not spawned twice onto the one dongle


def test_release_stops_the_process(tmp_path):
    proc = FakeProcess()
    dec = _decoder(tmp_path, spawn=lambda f, d, o: proc)
    dec.start()
    assert dec.running
    dec.release()
    assert proc.terminated
    assert not dec.running


def test_satellite_for_maps_known_apt_frequencies():
    assert satellite_for(137_100_000) == "NOAA-19"
    assert satellite_for(137_620_000) == "NOAA-15"
    assert satellite_for(137_912_500) == "NOAA-18"
    assert satellite_for(145_000_000) == "NOAA"  # unknown APT frequency falls back honestly


def test_default_apt_frequency_is_noaa19():
    assert DEFAULT_APT_HZ == 137_100_000
