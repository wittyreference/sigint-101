# ABOUTME: Exercise APT retirement, pass inventory, and cancellation before taking the radio.
# ABOUTME: Use existing image paths and process fakes, with no radio or decoder binary.

import threading
from contextlib import contextmanager

from sigint101 import apt
from sigint101.radio_control import RadioArbiter
from tests.child_fakes import Child
from tests.test_app import _client, _present, _StubDecoder, _StubSweeper


def _decoder(tmp_path):
    launched = []

    def start(freq_hz, _source, output):
        child = Child(["satdump", str(freq_hz), str(len(launched) + 1)])
        launched.append((child, output))
        return child

    decoder = apt.AptDecoder(output_dir=tmp_path / "apt", data_dir=tmp_path,
                             spawn=start, radio_present=lambda: True, which=lambda tool: tool)
    return decoder, launched


def test_apt_pass_timeout_is_reported_without_another_request(tmp_path):
    decoder, launched = _decoder(tmp_path)
    delivered = threading.Event()
    received = []

    def receive(item, *, command=None, freq_hz=None, radio=None):
        received.append((item["path"], command, freq_hz, radio))
        delivered.set()

    decoder.on_reception = receive
    decoder.output_dir.mkdir()
    (decoder.output_dir / "old.png").touch()
    decoder.start()
    try:
        child, output = launched[0]
        (output / "capture.png").touch()
        child.code = 0
        assert delivered.wait(1), "the finished pass was never reported after SatDump exited"
        decoder.stop()
        assert len(received) == 1
        assert received[0][:3] == ("apt/capture.png", " ".join(child.args), decoder.freq_hz)
        assert received[0][3]["kind"] == "rtl-sdr"
    finally:
        decoder.stop()


def test_apt_pending_local_start_does_not_block_a_sweep_claim(tmp_path, monkeypatch):
    decoder, launched = _decoder(tmp_path)
    entered, scanned, swept = (threading.Event() for _ in range(3))
    inventory = decoder._pngs

    def scan():
        entered.set()
        assert scanned.wait(5)
        return inventory()

    class Sweep(_StubSweeper):
        def start(self):
            super().start()
            swept.set()

    monkeypatch.setattr(decoder, "_pngs", scan)
    with _client(tmp_path, probe=_present, apt_factory=lambda: decoder,
                 sweeper_factory=lambda: Sweep([])) as client:
        replies = {}

        def request(name):
            replies[name] = client.get(f"/api/{name}")

        first = threading.Thread(target=request, args=("apt",), daemon=True)
        sweep = threading.Thread(target=request, args=("spectrum",), daemon=True)
        first.start()
        try:
            assert entered.wait(2)
            sweep.start()
            assert swept.wait(1), "the sweep waited for an APT intent that held no hardware"
            assert not launched
        finally:
            scanned.set()
            first.join(5)
            if sweep.ident is not None:
                sweep.join(5)
        assert replies["apt"].status_code == 503
        assert "cancelled" in replies["apt"].json()["reason"]
        assert replies["spectrum"].status_code == 200
        assert not launched


def test_apt_logging_does_not_serialize_sdr_start(tmp_path, monkeypatch):
    decoder, launched = _decoder(tmp_path)
    scanning, scanned, reporting, reported, swept = (threading.Event() for _ in range(5))
    scan = decoder._pngs
    calls = []

    def inventory():
        if launched and launched[0][0].code == 0 and not scanning.is_set():
            scanning.set()
            assert scanned.wait(5)
        return scan()

    def receive(item, **_):
        calls.append(item["path"])
        if len(calls) == 1:
            reporting.set()
            assert reported.wait(5)

    class Sweep(_StubSweeper):
        def start(self):
            super().start()
            swept.set()

    monkeypatch.setattr(decoder, "_pngs", inventory)
    with _client(tmp_path, probe=_present, apt_factory=lambda: decoder,
                 sweeper_factory=lambda: Sweep([])) as client:
        decoder.on_reception = receive
        decoder.start()
        child, output = launched[0]
        (output / "first.png").touch()
        child.code = 0
        sweep = threading.Thread(target=lambda: client.get("/api/spectrum"), daemon=True)
        start = threading.Thread(target=lambda: client.get("/api/apt"), daemon=True)
        sweep.start()
        try:
            assert scanning.wait(2)
            assert swept.wait(1), "the old pass inventory delayed physical handoff"
            start.start()
            assert len(launched) == 1
            scanned.set()
            assert reporting.wait(2)
            start.join(2)
            assert len(launched) == 2, "the old pass hook delayed the next launch"
            (output / "second.png").touch()
        finally:
            scanned.set()
            reported.set()
            sweep.join(5)
            if start.ident is not None:
                start.join(5)
        decoder.stop()
        assert calls == ["apt/first.png", "apt/second.png"]


def test_cancelled_apt_intent_cannot_release_the_successor_sweep(tmp_path, monkeypatch):
    decoder, launched = _decoder(tmp_path)
    releasing, release, prepared, queued = (threading.Event() for _ in range(4))
    reserve = RadioArbiter.reserve
    inventory = decoder._pngs

    def scan():
        candidates = inventory()
        prepared.set()
        return candidates

    @contextmanager
    def observed_reserve(self, target, *args):
        if target == "apt":
            queued.set()
        with reserve(self, target, *args):
            yield

    class Ism(_StubDecoder):
        def release(self):
            releasing.set()
            assert release.wait(5)
            super().release()

    monkeypatch.setattr(RadioArbiter, "reserve", observed_reserve)
    monkeypatch.setattr(decoder, "_pngs", scan)
    with _client(tmp_path, probe=_present, apt_factory=lambda: decoder,
                 decoder_factory=lambda: Ism([]),
                 sweeper_factory=lambda: _StubSweeper([])) as client:
        replies = {}

        def get(route):
            replies[route] = client.get(f"/api/{route}")

        sweep = threading.Thread(target=get, args=("spectrum",), daemon=True)
        capture = threading.Thread(target=get, args=("apt",), daemon=True)
        sweep.start()
        try:
            assert releasing.wait(2)
            capture.start()
            assert prepared.wait(2), "APT inventory waited for someone else's reservation"
            assert queued.wait(2), "the waiting APT request did not preflight off-lock"
        finally:
            release.set()
            sweep.join(5)
            if capture.ident is not None:
                capture.join(5)
        assert replies["spectrum"].status_code == 200
        assert replies["apt"].status_code == 503
        assert "cancelled" in replies["apt"].json()["reason"]
        assert not launched
        assert client.app.state.sweeper.running
