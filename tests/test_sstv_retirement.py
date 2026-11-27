# ABOUTME: Checks SSTV completion and SDR handoff independently of a following start call.
# ABOUTME: Parks publication and reception hooks at the capture boundary using file fixtures.

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from sigint101 import childproc
from sigint101.events import read_events
from tests.child_fakes import LateProc
from tests.test_app import _client, _present, _StubSweeper
from tests.test_receiver_retirement import until
from tests.test_sstv import (
    PNG_BYTES,
    _finish,
    capture_files,  # noqa: F401
)


def test_sstv_finishes_at_eof_without_a_follow_up_request(capture_files):  # noqa: F811
    decoder, launches, heard = capture_files
    decoder.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    _finish(child, mode="Robot 36")
    assert until(lambda: len(heard) == 1, seconds=2), "clean EOF did not publish the picture"
    decoder.stop()
    image, command, freq = heard[0]
    assert len(heard) == 1 and not staged.exists()
    assert image in decoder.images() and image["mode"] == "Robot 36"
    assert command == childproc.command_line(child) and freq == decoder.freq_hz


def test_sstv_publication_does_not_delay_handoff(
        capture_files, tmp_path, monkeypatch):  # noqa: F811
    decoder, launches, _heard = capture_files
    entered, publish, hooked, report, swept = (threading.Event() for _ in range(5))
    actual = decoder._publish_capture

    def gate(capture):
        entered.set()
        assert publish.wait(5)
        return actual(capture)

    class Sweep(_StubSweeper):
        def start(self):
            super().start()
            swept.set()

    monkeypatch.setattr(decoder, "_publish_capture", gate)
    client = _client(tmp_path, probe=_present, sstv_factory=lambda: decoder,
                     sweeper_factory=lambda: Sweep([]))
    with client:
        heard = decoder.on_reception

        def park(item, *, command=None, freq_hz=None, radio=None):
            hooked.set()
            assert report.wait(5)
            heard(item, command=command, freq_hz=freq_hz, radio=radio)

        decoder.on_reception = park
        decoder.start()
        child, staged = launches[0]
        staged.write_bytes(PNG_BYTES)
        _finish(child)
        successor = threading.Thread(target=lambda: client.get("/api/spectrum"), daemon=True)
        successor.start()
        try:
            assert entered.wait(2)
            assert child.poll() == 0
            assert swept.wait(1), "SSTV publication held the radio"
            publish.set()
            assert hooked.wait(2)
            assert client.app.state.sweeper.running
        finally:
            publish.set()
            report.set()
            successor.join(5)
    records = client.app.state.reception_error
    assert records is None
    [payload] = [row["payload"] for row in read_events(tmp_path / "events.jsonl")
                 if row["kind"] == "reception"]
    assert payload["command"] == childproc.command_line(child)
    assert payload["radio"]["kind"] == "rtl-sdr"


def test_sstv_clean_reap_logs_the_diagnostic_fault(
        capture_files, tmp_path, monkeypatch):  # noqa: F811
    decoder, launches, _heard = capture_files

    def broken(_stage):
        raise OSError("the stderr file could not be read")

    monkeypatch.setattr(childproc, "stderr_tail", broken)
    with _client(tmp_path, probe=_present, sstv_factory=lambda: decoder) as client:
        assert client.get("/api/sstv").status_code == 200
        child, staged = launches[0]
        staged.write_bytes(PNG_BYTES)
        _finish(child)
        decoder.stop()
        decoder.stop()
        [record] = client.get("/api/receptions?instrument=sstv").json()["receptions"]
    assert record["payload"]["item"]["meta"]["exit_diagnostic"] == {
        "status": 0, "reason": "the stderr file could not be read",
    }


@pytest.mark.parametrize("boundary", ["expiry", "competing-claim"])
def test_sstv_post_input_wait_aborts_an_unfinished_suffix(
        capture_files, tmp_path, monkeypatch, boundary):  # noqa: F811
    decoder, launches, heard = capture_files
    upstream = LateProc(["rtl_fm"])
    spawn = decoder._spawn
    monkeypatch.setattr(decoder, "_spawn", lambda *args: childproc.Pipeline(
        upstream, spawn(*args), finite=True))
    decoder.EOF_WAIT_S = 0.15 if boundary == "expiry" else 120
    if boundary == "expiry":
        decoder.start()
        child, staged = launches[0]
        staged.write_bytes(PNG_BYTES)
        upstream.die(7)
        assert until(lambda: child.poll() is not None, seconds=2)
        decoder.stop()
    else:
        with _client(tmp_path, probe=_present, sstv_factory=lambda: decoder,
                     sweeper_factory=lambda: _StubSweeper([])) as client:
            decoder.on_reception = lambda item: heard.append(item)
            decoder.start()
            child, staged = launches[0]
            staged.write_bytes(PNG_BYTES)
            upstream.die(7)
            with ThreadPoolExecutor(1) as pool:
                response = pool.submit(client.get, "/api/spectrum").result(timeout=2)
            assert response.status_code == 200
            assert child.poll() is not None
    assert staged.exists()
    assert heard == [] and decoder.images() == []
