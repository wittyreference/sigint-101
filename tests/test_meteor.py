# ABOUTME: Pins the Meteor site settings, SatDump command, and decoder telemetry.
# ABOUTME: Uses a gated fake child and a synthetic file signature, with no radio or SatDump.

import subprocess
import threading
import time
from pathlib import Path

import pytest

from sigint101.hardware import RtlSdrDevice
from tests.child_fakes import LateProc

MARKER = "[12:00:00 - 28/11/2026] (I) Stop processing"
VITERBI = "[12:00:00 - 28/11/2026] (D) Viterbi: SYNCED"
DEFRAMER = "[12:00:00 - 28/11/2026] (D) Deframer: SYNCED"


class MeteorChild(LateProc):
    def __init__(self, argv, *, resists_kill=False):
        super().__init__(argv)
        self.stderr = self
        self.sent = []
        self.term = threading.Event()
        self.killed = threading.Event()
        self.exited = threading.Event()
        self.closed = threading.Event()
        self.resists_kill = resists_kill

    def readline(self, _size):
        line = next(self.stdout, None)
        return line + "\n" if line is not None else ""

    def close(self):
        self.closed.set()

    def terminate(self):
        self.sent.append("TERM")
        self.term.set()

    def kill(self):
        self.sent.append("KILL")
        if not self.resists_kill and self.poll() is None:
            self.die(-9)
            self.close_pipe()
        self.killed.set()

    def die(self, code):
        super().die(code)
        self.exited.set()

    def wait(self, timeout=None):
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def lower_priority(self, nice):
        self.sent.append(("nice", nice))

    def picture(self, name="MSU-MR-1.png"):
        output = Path(self.args[3]) / "MSU-MR"
        output.mkdir(exist_ok=True)
        (output / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake fixture data")


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


@pytest.fixture(name="rig")
def meteor_rig(tmp_path, monkeypatch):
    from sigint101 import meteor

    for name in ("SIGINT101_METEOR_MODE", "SIGINT101_METEOR_HZ", "SIGINT101_RTLSDR_SERIAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(meteor, "RELEASE_GRACE_S", 0.18)
    monkeypatch.setattr(meteor, "KILL_GRACE_S", 0.18)
    made = []
    settings = {"present": True, "tool": True, "devices": [RtlSdrDevice(4, "rx")]}

    def spawn(argv, **kwargs):
        proc = MeteorChild(argv, resists_kill=settings.get("resists_kill", False))
        made.append((proc, kwargs))
        return proc

    decoder = meteor.MeteorDecoder(
        data_dir=tmp_path, which=lambda name: name if settings["tool"] else None,
        radio_present=lambda: settings["present"], list_devices=lambda: settings["devices"],
        spawn=spawn, clock=lambda: settings.get("utc", 1234567.0),
        monotonic=lambda: settings.get("now", time.monotonic()),
    )
    try:
        yield decoder, made, settings
    finally:
        for proc, _ in made:
            if proc.poll() is None:
                proc.resists_kill = False
            proc.close_pipe()
        decoder.shutdown()


@pytest.mark.parametrize(("tool", "present", "radios", "pin", "expected"), [
    (False, True, [(4, "rx")], None, "satdump is not installed"),
    (True, False, [], None, "no RTL-SDR"),
    (True, True, [], None, "no RTL-SDR"),
    (True, True, [(4, "rx")], "other", "no radio on this bus has serial"),
    (True, True, [(4, "rx"), (5, "rx")], "rx", "matches more than one"),
    (True, True, [(4, "rx"), (5, "other")], None, "none is pinned"),
    (True, True, [(4, "rx"), (5, "other")], "other", {"index": 5, "serial": "other"}),
    (True, True, [(4, "")], None, {"index": 4, "serial": None}),
])
def test_meteor_preflight_selects_the_radio_or_refuses(
        rig, monkeypatch, tool, present, radios, pin, expected):
    from sigint101.meteor import MeteorUnavailable

    decoder, made, settings = rig
    settings.update(tool=tool, present=present, devices=[RtlSdrDevice(*item) for item in radios])
    if pin is not None:
        monkeypatch.setenv("SIGINT101_RTLSDR_SERIAL", pin)
    if isinstance(expected, str):
        with pytest.raises(MeteorUnavailable, match=expected):
            decoder.start()
        assert made == []
    else:
        decoder.start()
        assert decoder.snapshot()["radio"] == {"kind": "rtl-sdr", **expected}


@pytest.mark.parametrize("mode", ["72k", "80k"])
def test_meteor_satdump_argv_uses_snapshot_and_absolute_local_tle(rig, tmp_path, monkeypatch, mode):
    decoder, made, settings = rig
    hz = 137900000 if mode == "72k" else 137100000
    pipeline = "meteor_m2-x_lrpt" if mode == "72k" else "meteor_m2-x_lrpt_80k"
    monkeypatch.setenv("SIGINT101_METEOR_MODE", mode)
    monkeypatch.setenv("SIGINT101_METEOR_HZ", str(hz))
    if mode == "80k":
        settings["devices"] = [RtlSdrDevice(4, "")]
    intent = decoder.prepare_start()
    monkeypatch.setenv("SIGINT101_METEOR_MODE", "bad after preflight")
    settings["devices"] = []
    decoder.launch_start(intent)
    proc, kwargs = made[0]
    output = Path(proc.args[3])
    assert output.is_dir() and output.is_relative_to(tmp_path / "meteor")
    tle = tmp_path / "meteor" / "tle.txt"
    assert not tle.exists()
    selector = ["--source_id", "rx"] if mode == "72k" else []
    assert proc.args == [
        "satdump", "live", pipeline, str(output), "--source", "rtlsdr", *selector,
        "--samplerate", "1024000", "--frequency", str(hz), "--gain", "49.6",
        "--timeout", "900", "--tle_override", str(tle), "--finish_processing",
    ]
    assert {key: kwargs[key] for key in ("stdin", "stdout", "stderr")} == {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE,
    }
    assert len(kwargs["pass_fds"]) == 1
    assert decoder.snapshot()["radio"]["serial"] == ("rx" if selector else None)


def test_meteor_lock_requires_both_sync_reports(rig):
    decoder, made, _ = rig
    decoder.start()
    proc = made[0][0]
    assert decoder.snapshot()["lock"] == "unknown"
    proc.feed("log says Stop processing now", "Viterbi: NOSYNC", DEFRAMER)
    eventually(lambda: decoder.snapshot()["lock"] == "searching")
    assert not decoder._run.physical.is_set()
    assert decoder.snapshot()["state"] == "starting"
    proc.feed(VITERBI)
    eventually(lambda: decoder.snapshot()["state"] == "receiving")
    assert decoder.snapshot()["lock"] == "locked"
    assert decoder.snapshot()["lock_ever"]
    proc.feed("Deframer: NOSYNC", "unrelated")
    eventually(lambda: decoder.snapshot()["lock"] == "searching")
    assert decoder.snapshot()["state"] != "receiving"
    assert decoder.snapshot()["lock_ever"]
