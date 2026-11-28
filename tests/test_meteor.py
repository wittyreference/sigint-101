# ABOUTME: Pins the Meteor site settings, SatDump command, and decoder telemetry.
# ABOUTME: Uses a gated fake child and a synthetic file signature, with no radio or SatDump.

import io
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from sigint101.hardware import RtlSdrDevice
from tests.child_fakes import LateProc

MARKER = "[12:00:00 - 28/11/2026] (I) Stop processing"


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
        if line is None:
            return b""
        return line.encode("utf-8") + b"\n"

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


def progress_line(viterbi="SYNCED", deframer="SYNCED"):
    # SatDump 1.2.2 M2-x LRPT decoder progress format, with the stderr logger's color prefix.
    return ("[12:00:00 - 28/11/2026] \x1b[32m(I) Progress 31.42%, "
            f"Viterbi : {viterbi} BER : 0.012, Deframer : {deframer}\x1b[m")


def test_meteor_lock_requires_both_sync_reports(rig):
    decoder, made, settings = rig
    settings["now"] = 100
    decoder.start()
    settings["now"] += 2
    proc = made[0][0]
    proc.feed("log says Stop processing now", "Progress 31.42%, Viterbi BER : 0.012%, Lock : 1")
    eventually(lambda: decoder._run.args[0].tail
               and "Lock : 1" in decoder._run.args[0].tail[-1])
    assert decoder.snapshot()["lock"] == "unknown"
    proc.feed(progress_line("NOSYNC", "SYNCED"))
    eventually(lambda: decoder.snapshot()["lock"] == "searching")
    assert not decoder._run.physical.is_set()
    assert not decoder.snapshot()["lock_ever"]
    proc.feed(progress_line("SYNCED", "SYNCING"))
    eventually(lambda: "SYNCING" in decoder._run.args[0].tail[-1])
    assert decoder.snapshot()["lock"] == "searching"
    assert not decoder.snapshot()["lock_ever"]
    proc.feed(progress_line())
    eventually(lambda: decoder.snapshot()["lock"] == "locked")
    assert decoder.snapshot()["state"] == "receiving"
    assert decoder.snapshot()["lock_ever"]
    proc.feed(progress_line("SYNCED", "NOSYNC"))
    eventually(lambda: decoder.snapshot()["lock"] == "searching")
    assert decoder.snapshot()["lock_ever"]
    proc.feed(MARKER)
    proc.die(0)
    proc.close_pipe()
    assert decoder._run.complete.wait(2)
    assert decoder.snapshot()["reason"] == "The signal locked but no picture was decoded."


def test_meteor_spawn_error_closes_its_stage_and_is_typed(rig, monkeypatch):
    from sigint101.meteor import MeteorUnavailable

    decoder, _, _ = rig
    opened = {}

    def fail(argv, *, pass_fds, **kwargs):
        opened.update(stage=Path(argv[3]), fd=pass_fds[0])
        raise subprocess.SubprocessError("Exception occurred in preexec_fn")

    monkeypatch.setattr(decoder, "_spawn", fail)
    with pytest.raises(MeteorUnavailable, match="preexec_fn"):
        decoder.start()
    assert not opened["stage"].exists()
    with pytest.raises(OSError):
        os.fstat(opened["fd"])


def test_meteor_invalid_stderr_bytes_do_not_end_the_drain(rig, monkeypatch):
    decoder, made, _ = rig
    spawn = decoder._spawn
    output = b"undecodable \xff line\n" + (progress_line() + "\n" + MARKER + "\n").encode()

    def emit(argv, **kwargs):
        proc = spawn(argv, **kwargs)
        stderr = io.BytesIO(output)
        proc.stderr = io.TextIOWrapper(stderr, encoding="utf-8") if kwargs.get("text") else stderr
        proc.die(0)
        return proc

    monkeypatch.setattr(decoder, "_spawn", emit)
    decoder.start()
    assert decoder._run.complete.wait(2)
    assert decoder.snapshot()["lock_ever"]
    assert decoder.snapshot()["reason"] == "The signal locked but no picture was decoded."
    assert made[0][0].stderr.closed


def test_meteor_shutdown_does_not_queue_a_retry_of_its_initial_kill(tmp_path, monkeypatch):
    from sigint101 import meteor, receiver_lifecycle

    for name in ("SIGINT101_METEOR_MODE", "SIGINT101_METEOR_HZ", "SIGINT101_RTLSDR_SERIAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(receiver_lifecycle, "PHYSICAL_WAIT_S", 2.0)
    made, waits = [], []

    class Proc(MeteorChild):
        def wait(self, timeout=None):
            waits.append(timeout)
            raise subprocess.TimeoutExpired(self.args, timeout)

    def spawn(argv, **_):
        made.append(Proc(argv, resists_kill=True))
        return made[-1]

    decoder = meteor.MeteorDecoder(
        data_dir=tmp_path, which=lambda name: name, radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")], spawn=spawn,
    )
    decoder.start()
    proc, = made
    run = decoder._run
    try:
        started = time.monotonic()
        with pytest.raises(meteor.MeteorUnavailable, match="SatDump survived kill"):
            decoder.shutdown()
        elapsed = time.monotonic() - started
        assert run.complete.wait(3)
        assert proc.sent == ["KILL"]
        assert len(waits) == 1 and 0 < waits[0] <= meteor.KILL_GRACE_S
        assert elapsed < receiver_lifecycle.PHYSICAL_WAIT_S
        assert not run.reaped and decoder.release_failure()
    finally:
        proc.die(-9)
        proc.close_pipe()
        run.reader.join(3)
