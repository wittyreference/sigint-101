# ABOUTME: Checks SSTV completion and SDR handoff independently of a following start call.
# ABOUTME: Parks publication and reception hooks at the capture boundary using file fixtures.

import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sigint101 import childproc, sstv
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
        capture_files, tmp_path):  # noqa: F811
    decoder, launches, _heard = capture_files

    class BadStderr(io.StringIO):
        def read(self, *args):
            raise OSError("the stderr file could not be read")

    with _client(tmp_path, probe=_present, sstv_factory=lambda: decoder) as client:
        assert client.get("/api/sstv").status_code == 200
        child, staged = launches[0]
        child.stderr.close()
        child.stderr = BadStderr()
        staged.write_bytes(PNG_BYTES)
        _finish(child)
        decoder.stop()
        decoder.stop()
        [record] = client.get("/api/receptions?instrument=sstv").json()["receptions"]
    assert record["payload"]["item"]["meta"]["exit_diagnostic"] == {
        "status": 0, "reason": "the stderr file could not be read",
    }


def test_sstv_reads_the_failed_upstreams_diagnostics_once(capture_files, monkeypatch):  # noqa: F811
    decoder, launches, heard = capture_files
    upstream = LateProc(["rtl_fm"])
    reads = []

    class CountedStderr(io.StringIO):
        def read(self, *args):
            reads.append(threading.get_ident())
            return super().read(*args)

    upstream.stderr.close()
    upstream.stderr = CountedStderr("front end lost samples")
    spawn = decoder._spawn
    monkeypatch.setattr(decoder, "_spawn", lambda *args: childproc.Pipeline(
        upstream, spawn(*args), finite=True))
    decoder.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    upstream.die(7)
    _finish(child)
    assert until(lambda: decoder._run.complete.is_set(), seconds=3)
    assert len(heard) == 1
    assert "front end lost samples" in decoder.watch.reason
    assert len(reads) == 1


@pytest.mark.parametrize("when", ["before-admission", "after-link"])
def test_sstv_publication_and_reception_share_shutdown_admission(
        capture_files, tmp_path, monkeypatch, when):  # noqa: F811
    decoder, launches, _heard = capture_files
    with _client(tmp_path, probe=_present, sstv_factory=lambda: decoder) as client:
        sink = client.app.state.reception_sink
        decoder.start()
        child, staged = launches[0]
        staged.write_bytes(PNG_BYTES)
        pending_at_link = []
        real_link = os.link

        def link(source, target, *args, **kwargs):
            real_link(source, target, *args, **kwargs)
            if Path(target).suffix == ".png":
                pending_at_link.append(sink.pending())
                sink.seal()

        monkeypatch.setattr(os, "link", link)
        if when == "before-admission":
            sink.seal()
        _finish(child)
        decoder.stop()
        assert sink.pending() == 0
        records = [row["payload"] for row in read_events(tmp_path / "events.jsonl")
                   if row["kind"] == "reception"]
        if when == "before-admission":
            assert decoder.images() == [] and records == []
        else:
            [image] = decoder.images()
            assert [row["item"]["path"] for row in records] == [image["path"]]
            assert pending_at_link == [1]
        assert not staged.exists()


def test_sstv_owned_abort_cleanup_survives_restart(capture_files, monkeypatch):  # noqa: F811
    decoder, launches, heard = capture_files
    decoder.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    unrelated = staged.with_name("unconfirmed.png")
    unrelated.write_bytes(PNG_BYTES)
    unlink = Path.unlink

    def transient_unlink(path, *args, **kwargs):
        if path.name == staged.name:
            raise OSError("staging unlink failed")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", transient_unlink)
        child.close_pipe()
        decoder.stop()
    assert heard == [] and decoder.images() == []
    assert unrelated.exists()

    restarted = sstv.SstvDecoder(
        spawn=lambda *_: LateProc(["sstv"]), output_dir=decoder.output_dir,
        radio_present=lambda: True, which=lambda tool: tool,
        clock=decoder._clock)
    try:
        restarted.start()
        assert not staged.exists()
        assert unrelated.exists()
        assert list(staged.parent.rglob(staged.name)) == []
        assert restarted.images() == []
    finally:
        if restarted._proc is not None:
            restarted._proc.close_pipe()
        restarted.stop()


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
    assert not staged.exists()
    assert heard == [] and decoder.images() == []
