# ABOUTME: Exercises the console's reception log when a reader outlives its radio selection.
# ABOUTME: Covers USB enumeration, monitor interfaces, the Marauder bridge and finished APT passes.

import struct
import threading

import pytest
from fastapi.testclient import TestClient

from sigint101 import flock
from sigint101.app import RTLSDR_SERIAL_ENV, create_app
from sigint101.childproc import command_line, emit_reception
from sigint101.events import read_events
from sigint101.hardware import RadioProbe, RtlSdrDevice
from tests.child_fakes import LateByteProc, LateProc
from tests.test_stale_readers import (
    FIXTURE_PACKS,
    FLOCK_DOT11_1,
    FLOCK_DOT11_2,
    MARAUDER_PCAP_HDR,
    SPEC_BY_NAME,
)


def _console(tmp_path, list_devices, **factories):
    return create_app(
        pack_dir=FIXTURE_PACKS,
        data_dir=tmp_path,
        probe=lambda: RadioProbe(True, "test", "RTL-SDR present"),
        list_devices=list_devices,
        **factories,
    )


def _receptions(tmp_path):
    return [event["payload"] for event in read_events(tmp_path / "events.jsonl")
            if event["kind"] == "reception"]


def _marauder_chunk(frame):
    record = struct.pack("<IIII", 1, 0, len(frame), len(frame)) + frame
    return flock.MARAUDER_BEGIN + MARAUDER_PCAP_HDR + record + flock.MARAUDER_CLOSE


@pytest.mark.parametrize("name", [*SPEC_BY_NAME, "marauder"])
def test_delayed_reader_logs_the_radio_that_heard_its_item(tmp_path, monkeypatch, name):
    monkeypatch.delenv(RTLSDR_SERIAL_ENV, raising=False)
    spec = SPEC_BY_NAME["flock" if name == "marauder" else name]
    bus = [RtlSdrDevice(0, "radio-first")]
    ports = ["bridge-first"]
    expected = [
        {"kind": "rtl-sdr", "index": 0, "serial": "radio-first"},
        {"kind": "rtl-sdr", "index": 1, "serial": "radio-second"},
    ]
    payloads = spec.line1, spec.line2
    procs = []
    paused, resume, logged = threading.Event(), threading.Event(), threading.Event()

    def spawn(*args):
        proc = spec.make_proc(len(procs) + 1, *args)
        procs.append(proc)
        return proc

    class Link(LateByteProc):
        answered = True
        end_reason = None

        def end(self, reason=None):
            self.end_reason = reason
            self.terminate()

    def from_bridge():
        detector = flock.MarauderFlockDetector(glob=lambda _: ports)

        def open_capture():
            link = Link(["sniffprobe", "-serial", "--run", str(len(procs) + 1)])
            procs.append(link)
            return link

        detector._open_capture = open_capture
        return detector

    factories = {}
    if name == "flock":
        factories["flock_factory"] = lambda: spec.build(spawn)
        expected = [
            {"kind": "wifi", "interface": "monitor-first"},
            {"kind": "wifi", "interface": "monitor-second"},
        ]
    elif name == "marauder":
        factories["flock_factory"] = from_bridge
        expected = [
            {"kind": "wifi", "interface": "bridge-first"},
            {"kind": "wifi", "interface": "bridge-second"},
        ]
        payloads = _marauder_chunk(FLOCK_DOT11_1), _marauder_chunk(FLOCK_DOT11_2)
    app = _console(tmp_path, lambda: tuple(bus), **factories)

    with TestClient(app):
        receiver = getattr(app.state, "decoder" if spec.name == "decode" else spec.name)
        receiver.STOP_WAIT_S = 0.05
        receiver._which = lambda tool: tool
        if name == "flock":
            receiver.interface = "monitor-first"
        elif name != "marauder":
            receiver._spawn = spawn
        heard = receiver.on_reception

        def hold_after_admission(item, *, radio=None, **provenance):
            first = spec.item_key(item) == spec.key1
            if first:
                paused.set()
                assert resume.wait(5)
            if radio is not None:
                provenance["radio"] = radio
            heard(item, **provenance)
            if not first:
                logged.set()

        receiver.on_reception = hold_after_admission
        try:
            receiver.start()
            procs[0].feed(payloads[0])
            assert paused.wait(5)
            assert spec.key1 in spec.listed(receiver)

            receiver.stop()
            bus[:] = [RtlSdrDevice(1, "radio-second")]
            ports[:] = ["bridge-second"]
            if name == "flock":
                receiver.interface = "monitor-second"
            if spec.other_hz is not None:
                receiver.retune(spec.other_hz)
            receiver.start()
            assert len(procs) == 2 and receiver.running
            procs[1].feed(payloads[1])
            assert logged.wait(5)

            resume.set()
            procs[0].close_pipe()
            procs[0].reader.join(5)
            assert not procs[0].reader.is_alive()
        finally:
            resume.set()
            for proc in procs:
                proc.close_pipe()
            receiver.stop()
            for proc in procs:
                if proc.reader is not None:
                    proc.reader.join(5)

    new, old = _receptions(tmp_path)
    for row, key, proc in ((old, spec.key1, procs[0]), (new, spec.key2, procs[1])):
        assert spec.item_key(row["item"]) == key
        assert row["command"] == command_line(proc)
    assert old["freq_hz"] == spec.home_hz
    assert new["freq_hz"] == (spec.other_hz or spec.home_hz)
    assert old["radio"] == expected[0]
    assert new["radio"] == expected[1]


def test_delayed_ism_read_logs_its_launch_radio_command_and_frequency(tmp_path, monkeypatch):
    monkeypatch.delenv(RTLSDR_SERIAL_ENV, raising=False)
    bus = [RtlSdrDevice(0, "radio-first")]
    app = _console(tmp_path, lambda: tuple(bus))
    procs = []
    paused, resume, logged = threading.Event(), threading.Event(), threading.Event()

    def spawn(cmd):
        proc = LateProc([*cmd, "-v"])
        procs.append(proc)
        return proc

    with TestClient(app):
        decoder = app.state.decoder
        decoder.STOP_WAIT_S = 0.05
        decoder._which = lambda tool: tool
        decoder._spawn = spawn
        heard = decoder.on_reception
        first_hz = decoder.freq_hz

        def hold_after_admission(item, *, radio=None, **provenance):
            first = item["readings"]["id"] == 12
            if first:
                paused.set()
                assert resume.wait(5)
            heard(item, radio=radio, **provenance)
            if not first:
                logged.set()

        decoder.on_reception = hold_after_admission
        try:
            decoder.start()
            procs[0].feed('{"model":"TestSensor","id":12,"freq":433.91}')
            assert paused.wait(5)
            assert [read.model for read in decoder.recent()] == ["TestSensor"]

            decoder.retune(915_000_000)
            bus[:] = [RtlSdrDevice(1, "radio-second")]
            decoder.start()
            assert len(procs) == 2 and decoder.running and decoder.device == 1
            assert decoder.live_command() == command_line(procs[1])
            procs[1].feed('{"model":"TestSensor","id":34,"freq":915.01}')
            assert logged.wait(5)

            resume.set()
            procs[0].close_pipe()
            procs[0].reader.join(5)
            assert not procs[0].reader.is_alive()
        finally:
            resume.set()
            for proc in procs:
                proc.close_pipe()
            decoder.stop()
            for proc in procs:
                if proc.reader is not None:
                    proc.reader.join(5)
                    assert not proc.reader.is_alive()

    new, old = _receptions(tmp_path)
    for record, sensor_id in ((old, 12), (new, 34)):
        assert record["instrument"] == "ism" and record["item"]["model"] == "TestSensor"
        assert record["item"]["readings"]["id"] == sensor_id
    assert (old["radio"], old["command"], old["freq_hz"]) == (
        {"kind": "rtl-sdr", "index": 0, "serial": "radio-first"},
        command_line(procs[0]), first_hz,
    )
    assert (new["radio"], new["command"], new["freq_hz"]) == (
        {"kind": "rtl-sdr", "index": 1, "serial": "radio-second"},
        command_line(procs[1]), 915_000_000,
    )


@pytest.mark.parametrize("end", ["stop", "timeout"])
def test_apt_logs_the_pass_radio_after_the_census_changes(tmp_path, monkeypatch, end):
    monkeypatch.delenv(RTLSDR_SERIAL_ENV, raising=False)
    bus = [RtlSdrDevice(0, "radio-first")]
    app = _console(tmp_path, lambda: tuple(bus))
    passes = []

    def satdump(freq_hz, serial, output_dir):
        proc = LateProc(["satdump", "--source_id", serial, "--frequency", str(freq_hz)])
        passes.append(proc)
        return proc

    with TestClient(app):
        apt = app.state.apt
        apt._which = lambda tool: tool
        apt._spawn = satdump
        apt.start()
        (apt.output_dir / "captured.png").touch()

        # A later enumeration reuses the first radio's index for another device.
        bus[:] = [RtlSdrDevice(0, "radio-second")]
        adsb = app.state.adsb
        adsb._which = lambda tool: tool
        refresh = LateProc(["dump1090-fa"])
        refresh.close_pipe()
        adsb._spawn = lambda _device: refresh
        adsb.start()
        adsb.stop()

        freq_hz = apt.freq_hz
        if end == "timeout":
            passes[0].die(0)
            apt.start()
            assert len(passes) == 2
        else:
            apt.stop()

    [record] = _receptions(tmp_path)
    assert record["instrument"] == "apt"
    assert record["item"]["path"] == "apt/captured.png"
    assert record["command"] == command_line(passes[0])
    assert record["freq_hz"] == freq_hz
    assert record["radio"] == {"kind": "rtl-sdr", "index": 0, "serial": "radio-first"}


def test_emit_reception_only_passes_radio_to_hooks_that_accept_it():
    item = {"path": "apt/captured.png"}
    radio = {"kind": "rtl-sdr", "index": 0, "serial": "radio-first"}
    calls = []

    def old_hook(row, *, command, freq_hz):
        calls.append((row, command, freq_hz))

    def radio_hook(row, *, radio):
        calls.append((row, radio))

    def forwarding_hook(row, **provenance):
        old_hook(row, **provenance)

    def flexible_hook(row, *, radio, **provenance):
        calls.append((row, radio, provenance))

    for hook in (old_hook, radio_hook, forwarding_hook, flexible_hook, calls.append):
        emit_reception(hook, item, command="satdump", freq_hz=137_100_000, radio=radio)

    assert calls == [
        (item, "satdump", 137_100_000),
        (item, radio),
        (item, "satdump", 137_100_000),
        (item, radio, {"command": "satdump", "freq_hz": 137_100_000}),
        item,
    ]
