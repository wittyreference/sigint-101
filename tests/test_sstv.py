# ABOUTME: Tests the SSTV backend — orchestrating rtl_fm|sox|sstv and discovering decoded PNGs.
# ABOUTME: The pipeline is a fake at the boundary; no radio / no tools / no picture is honest.

import base64
import io
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from sigint101 import childproc, receiver_lifecycle
from sigint101.hardware import RTL_FM, RtlSdrDevice
from sigint101.sstv import (
    DEFAULT_SSTV_HZ,
    SOX,
    SSTV,
    SstvDecoder,
    SstvUnavailable,
    parse_mode,
    pipeline_commands,
)
from tests.child_fakes import LateProc
from tests.test_app import _client, _present

# A real 1x1 PNG, used as a stand-in for a completed decode. The module never reads pixels; this
# only has to be a genuine image file on disk, the way sstv would leave one behind.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _drop_image(output_dir, name, *, mode=None, mtime=None, freq_hz=None):
    """Drop a PNG (and an optional sidecar) into the output dir, as a finished decode would."""
    output_dir = str(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    png = os.path.join(output_dir, name)
    with open(png, "wb") as handle:
        handle.write(PNG_BYTES)
    if mode is not None or freq_hz is not None:
        data = {}
        if mode is not None:
            data["mode"] = mode
        if freq_hz is not None:
            data["freq_hz"] = freq_hz
        with open(os.path.splitext(png)[0] + ".json", "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data))
    if mtime is not None:
        os.utime(png, (mtime, mtime))
    return png


class FakePipeline:
    """Stands in for the rtl_fm|sox|sstv pipeline at the boundary.

    Its stdout is sstv's line iterator (where the mode is logged); on construction it drops a tiny
    PNG at the output path, the way a finished decode leaves an image behind.
    """

    code = None

    def __init__(self, lines, output_path=None):
        self.stdout = iter(lines)
        self.terminated = False
        if output_path is not None:
            os.makedirs(os.path.dirname(str(output_path)), exist_ok=True)
            with open(output_path, "wb") as handle:
                handle.write(PNG_BYTES)

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _decoder(tmp_path, lines=(), **kw):
    kw.setdefault("radio_present", lambda: True)
    kw.setdefault("which", lambda name: "/usr/bin/" + name)
    kw.setdefault("output_dir", tmp_path / "sstv")
    kw.setdefault("data_dir", tmp_path)

    def spawn(freq, device, output_path):
        return FakePipeline(list(lines), output_path=output_path)

    kw.setdefault("spawn", spawn)
    return SstvDecoder(**kw)


# --- the default frequency -------------------------------------------------

def test_default_frequency_is_the_current_iss_sstv_downlink():
    # Every ARISS ISS SSTV event in 2026 used 437.550 MHz FM, Robot36; 145.800 PD120 last ran in
    # October 2025 (ariss.org; technical audit T-05 ruling).
    assert DEFAULT_SSTV_HZ == 437_550_000


# --- parsing the mode out of sstv's output ---------------------------------

def test_parse_mode_reads_the_detected_mode_line():
    assert parse_mode("Detected SSTV mode Martin 1") == "Martin 1"
    assert parse_mode("Detected SSTV mode Scottie DX") == "Scottie DX"
    assert parse_mode("Detected SSTV mode Robot 36\n") == "Robot 36"


def test_parse_mode_ignores_other_lines():
    assert parse_mode("Searching for calibration header... Found!") is None
    assert parse_mode("Decoding image... [####]") is None
    assert parse_mode("") is None


# --- command construction --------------------------------------------------

def test_pipeline_commands_chains_rtl_fm_sox_and_sstv(tmp_path):
    out = tmp_path / "sstv" / "pic.png"
    cmds = pipeline_commands(145_800_000, 0, out)
    assert len(cmds) == 3
    rtl_fm, sox, sstv = cmds
    assert rtl_fm[0] == RTL_FM
    assert sox[0] == SOX
    assert sstv[0] == SSTV


def test_pipeline_commands_tunes_the_front_end_to_the_frequency_and_device(tmp_path):
    out = tmp_path / "sstv" / "pic.png"
    rtl_fm = pipeline_commands(145_800_000, 3, out)[0]
    assert rtl_fm[rtl_fm.index("-f") + 1] == "145800000"
    assert rtl_fm[rtl_fm.index("-d") + 1] == "3"


def test_pipeline_commands_points_sstv_at_the_output_path(tmp_path):
    out = tmp_path / "sstv" / "pic.png"
    sstv = pipeline_commands(145_800_000, 0, out)[2]
    assert sstv[sstv.index("-o") + 1] == str(out)
    # sstv reads the WAV the sox stage streams on stdin, not a file on disk.
    assert sstv[sstv.index("-d") + 1] == "-"


# --- device selection ------------------------------------------------------

def test_start_selects_the_only_radio_on_the_bus(tmp_path):
    calls = []

    def spawn(freq, device, output_path):
        calls.append(device)
        return FakePipeline([], output_path=output_path)

    dec = _decoder(tmp_path, spawn=spawn,
                   list_devices=lambda: (RtlSdrDevice(index=4, serial="ABC"),))
    dec.start()
    dec.drain()
    assert calls == [4]


def test_start_refuses_when_several_radios_and_none_pinned(tmp_path):
    dec = _decoder(tmp_path, list_devices=lambda: (
        RtlSdrDevice(index=0, serial="AAA"), RtlSdrDevice(index=1, serial="BBB")))
    with pytest.raises(SstvUnavailable):
        dec.start()


def test_start_selects_the_pinned_serial(tmp_path):
    calls = []

    def spawn(freq, device, output_path):
        calls.append(device)
        return FakePipeline([], output_path=output_path)

    dec = _decoder(tmp_path, spawn=spawn, serial="BBB", list_devices=lambda: (
        RtlSdrDevice(index=0, serial="AAA"), RtlSdrDevice(index=1, serial="BBB")))
    dec.start()
    dec.drain()
    assert calls == [1]


# --- discovering decoded images -------------------------------------------

def test_images_lists_decoded_pngs_newest_first(tmp_path):
    dec = _decoder(tmp_path)
    _drop_image(tmp_path / "sstv", "older.png", mtime=1000)
    _drop_image(tmp_path / "sstv", "newer.png", mtime=2000)
    imgs = dec.images()
    assert [os.path.basename(i["path"]) for i in imgs] == ["newer.png", "older.png"]


def test_images_path_is_relative_to_the_data_dir(tmp_path):
    dec = _decoder(tmp_path)
    _drop_image(tmp_path / "sstv", "pic.png")
    assert dec.images()[0]["path"] == "sstv/pic.png"


def test_images_reads_mode_and_meta_from_a_sidecar(tmp_path):
    dec = _decoder(tmp_path)
    _drop_image(tmp_path / "sstv", "pic.png", mode="Scottie 1", freq_hz=145_800_000)
    img = dec.images()[0]
    assert img["mode"] == "Scottie 1"
    assert img["meta"]["freq_hz"] == 145_800_000
    assert "mode" not in img["meta"]


def test_images_without_a_sidecar_report_no_mode(tmp_path):
    dec = _decoder(tmp_path)
    _drop_image(tmp_path / "sstv", "pic.png")
    img = dec.images()[0]
    assert img["mode"] is None
    assert img["meta"] == {}


def test_images_carry_a_received_timestamp(tmp_path):
    dec = _decoder(tmp_path)
    _drop_image(tmp_path / "sstv", "pic.png", mtime=1500)
    assert dec.images()[0]["received_at"] == pytest.approx(1500.0)


def test_images_honours_a_limit(tmp_path):
    dec = _decoder(tmp_path)
    for i in range(4):
        _drop_image(tmp_path / "sstv", f"pic{i}.png", mtime=1000 + i)
    assert len(dec.images(limit=2)) == 2


def test_images_is_empty_before_anything_is_decoded(tmp_path):
    dec = _decoder(tmp_path)
    assert dec.images() == []


def test_images_is_empty_when_the_output_dir_does_not_exist(tmp_path):
    dec = SstvDecoder(output_dir=tmp_path / "never" / "sstv", data_dir=tmp_path,
                      radio_present=lambda: True, which=lambda name: "/usr/bin/" + name)
    assert dec.images() == []


def test_a_malformed_sidecar_degrades_to_no_mode(tmp_path):
    dec = _decoder(tmp_path)
    png = _drop_image(tmp_path / "sstv", "pic.png")
    with open(os.path.splitext(png)[0] + ".json", "w", encoding="utf-8") as handle:
        handle.write("{not json")
    img = dec.images()[0]
    assert img["mode"] is None
    assert img["meta"] == {}


def test_a_non_object_sidecar_degrades_to_no_mode(tmp_path):
    dec = _decoder(tmp_path)
    png = _drop_image(tmp_path / "sstv", "pic.png")
    with open(os.path.splitext(png)[0] + ".json", "w", encoding="utf-8") as handle:
        handle.write("[1, 2, 3]")   # valid JSON, but not a metadata object
    img = dec.images()[0]
    assert img["mode"] is None
    assert img["meta"] == {}


def test_path_falls_back_to_the_filename_when_outside_the_data_dir(tmp_path):
    # If the output dir is not under the data dir, the path cannot be made relative to it, so the
    # bare filename is reported rather than an absolute path or a crash.
    dec = SstvDecoder(output_dir=tmp_path / "a" / "sstv", data_dir=tmp_path / "b",
                      radio_present=lambda: True, which=lambda name: "/usr/bin/" + name)
    _drop_image(tmp_path / "a" / "sstv", "pic.png")
    assert dec.images()[0]["path"] == "pic.png"


# --- the reader turns sstv's report into a sidecar -------------------------

def test_a_finished_decode_is_discoverable_with_its_mode(tmp_path):
    lines = [
        "Searching for calibration header... Found!",
        "Detected SSTV mode Martin 1",
        "Decoding image... done",
    ]
    dec = _decoder(tmp_path, lines)
    dec.start()
    dec._proc.code = 0
    dec.drain()
    dec.stop()
    imgs = dec.images()
    assert len(imgs) == 1
    assert imgs[0]["mode"] == "Martin 1"
    assert imgs[0]["meta"]["freq_hz"] == DEFAULT_SSTV_HZ


def test_a_decode_with_no_reported_mode_writes_no_sidecar(tmp_path):
    dec = _decoder(tmp_path, ["Decoding image... done"])
    dec.start()
    dec._proc.code = 0
    dec.drain()
    dec.stop()
    img = dec.images()[0]
    assert img["mode"] is None
    assert img["meta"] == {}


# --- the no-mock refusal ladder --------------------------------------------

def test_start_refuses_without_a_radio(tmp_path):
    dec = _decoder(tmp_path, radio_present=lambda: False)
    with pytest.raises(SstvUnavailable):
        dec.start()


def test_start_refuses_when_sstv_is_missing(tmp_path):
    dec = _decoder(tmp_path, which=lambda name: None if name == SSTV else "/usr/bin/" + name)
    with pytest.raises(SstvUnavailable):
        dec.start()


def test_start_refuses_when_rtl_fm_is_missing(tmp_path):
    dec = _decoder(tmp_path, which=lambda name: None if name == RTL_FM else "/usr/bin/" + name)
    with pytest.raises(SstvUnavailable):
        dec.start()


def test_start_refuses_when_sox_is_missing(tmp_path):
    dec = _decoder(tmp_path, which=lambda name: None if name == SOX else "/usr/bin/" + name)
    with pytest.raises(SstvUnavailable):
        dec.start()


def test_start_refuses_once_released_until_resumed(tmp_path):
    dec = _decoder(tmp_path, ["Detected SSTV mode Robot 36"])
    assert dec.released is False
    dec.release()
    assert dec.released is True
    with pytest.raises(SstvUnavailable):
        dec.start()
    dec.resume()
    assert dec.released is False
    dec.start()
    dec._proc.code = 0
    dec.drain()
    dec.stop()
    assert dec.images()[0]["mode"] == "Robot 36"


def test_start_refuses_when_the_pipeline_cannot_launch(tmp_path):
    def spawn(freq, device, output_path):
        raise OSError("no such binary")

    dec = _decoder(tmp_path, spawn=spawn)
    with pytest.raises(SstvUnavailable):
        dec.start()


def test_a_prior_reader_error_blocks_the_next_start_once(tmp_path):
    class ExitedRaisingPipeline(FakePipeline):
        def __init__(self, output_path=None):
            def boom():
                raise OSError("pipe died")
                yield  # pragma: no cover
            self.stdout = boom()
            self.terminated = False

        def poll(self):
            return 1  # the pipeline has exited, so the decoder is no longer running

    dec = SstvDecoder(spawn=lambda f, d, o: ExitedRaisingPipeline(), radio_present=lambda: True,
                      which=lambda name: "/usr/bin/" + name,
                      output_dir=tmp_path / "sstv", data_dir=tmp_path)
    dec.start()
    dec.drain()
    assert dec.error == "pipe died"
    with pytest.raises(SstvUnavailable):
        dec.start()
    assert dec.error is None  # the error is surfaced once, then cleared


def test_start_is_a_no_op_while_already_running(tmp_path):
    calls = []

    def spawn(freq, device, output_path):
        calls.append(output_path)
        return FakePipeline([], output_path=output_path)

    dec = _decoder(tmp_path, spawn=spawn)
    dec.start()
    dec.start()
    assert len(calls) == 1


def test_release_keeps_the_pictures_it_already_received(tmp_path):
    # Received pictures are durable artifacts on disk, not an in-memory roster: handing the dongle
    # to the sweeper must not throw away a picture the learner already pulled out of the air.
    now = 1735689600.0
    dec = _decoder(tmp_path, ["Detected SSTV mode Martin 2"], clock=lambda: now)
    dec.start()
    dec._proc.code = 0
    dec.drain()
    dec.stop()
    assert dec.images()
    now += 1
    dec.start()
    assert dec.running
    dec.release()
    assert len(dec.images()) == 1


def test_a_reader_error_is_recorded_not_swallowed(tmp_path):
    class RaisingPipeline(FakePipeline):
        def __init__(self, output_path=None):
            def boom():
                raise OSError("pipe died")
                yield  # pragma: no cover
            self.stdout = boom()
            self.terminated = False

    dec = SstvDecoder(spawn=lambda f, d, o: RaisingPipeline(), radio_present=lambda: True,
                      which=lambda name: "/usr/bin/" + name,
                      output_dir=tmp_path / "sstv", data_dir=tmp_path)
    dec.start()
    dec.drain()
    assert dec.error == "pipe died"


# --- publication belongs to the capture that supplied the bytes ------------------


@pytest.fixture
def capture_files(tmp_path):
    launches = []
    heard = []

    def spawn(freq, device, path):
        child = LateProc([SSTV, "-o", str(path), "--test-frequency", str(freq)])
        launches.append((child, path))
        return child

    dec = _decoder(tmp_path, spawn=spawn, clock=lambda: 1735689600.0)

    def report(item, *, command=None, freq_hz=None):
        heard.append((item, command, freq_hz))

    dec.on_reception = report
    yield dec, launches, heard
    for child, _ in launches:
        child.close_pipe()
    dec.stop()


def _finish(child, *, mode="Robot 36", code=0):
    if mode is not None:
        child.feed(f"Detected SSTV mode {mode}")
    child.die(code)
    child.close_pipe()


class _StubbornStage(LateProc):
    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        if self.poll() is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.poll()


def test_sstv_live_picture_cannot_be_listed_or_served(tmp_path, capture_files):
    dec, launches, _ = capture_files
    with _client(tmp_path, probe=_present, sstv_factory=lambda: dec) as client:
        assert client.get("/api/sstv").status_code == 200
        _, staged = launches[0]
        staged.write_bytes(PNG_BYTES)
        listed = dec.images()
        served = client.get(f"/media/{staged.relative_to(tmp_path)}").status_code
        assert (listed, served) == ([], 404)


@pytest.mark.parametrize("exit_code", [None, 1], ids=["live-abort", "failed-exit"])
def test_sstv_aborted_or_failed_capture_discards_its_picture(capture_files, exit_code):
    dec, launches, heard = capture_files
    dec.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    if exit_code is not None:
        child.die(exit_code)
    child.close_pipe()
    dec.stop()
    assert not staged.exists()
    assert dec.images() == []
    assert heard == []
    if exit_code is not None:
        with pytest.raises(SstvUnavailable, match=f"exit status {exit_code}"):
            dec.start()
    else:
        dec.start()
    assert not staged.exists()



def test_sstv_unreaped_writer_keeps_its_picture_private(capture_files, monkeypatch):
    dec, launches, heard = capture_files
    upstream = _StubbornStage(["rtl_fm"])
    spawn = dec._spawn

    def launch(*args):
        return childproc.Pipeline(upstream, spawn(*args), finite=True)

    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0)
    monkeypatch.setattr(dec, "_spawn", launch)
    dec.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    _finish(child)
    dec.stop()
    assert "survived kill" in dec.error
    assert upstream.poll() is None
    assert staged.is_file()
    assert dec.images() == []
    assert heard == []


def test_sstv_restart_cleans_an_aborted_capture_after_reap_retry(
        tmp_path, capture_files, monkeypatch):
    dec, launches, heard = capture_files
    upstream = _StubbornStage(["rtl_fm"])
    spawn = dec._spawn
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.02)
    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0)
    monkeypatch.setattr(
        dec, "_spawn", lambda *args: childproc.Pipeline(upstream, spawn(*args), finite=True))
    dec.start()
    child, staged = launches[0]
    ticket = dec._run
    staged.write_bytes(PNG_BYTES)
    unrelated = staged.with_name("another-capture.png")
    unrelated.touch()
    discarded = dec._discard_dir / staged.name
    unlink = Path.unlink

    def interrupted_unlink(path, *args, **kwargs):
        if path == discarded:
            raise OSError("discard cleanup interrupted")
        return unlink(path, *args, **kwargs)

    try:
        dec.stop()
        assert ticket.reap_failed and not ticket.reaped
        assert staged.exists() and not discarded.exists()
        with monkeypatch.context() as interruption:
            interruption.setattr(Path, "unlink", interrupted_unlink)
            upstream.die(7)
            assert ticket.complete.wait(3)
            assert ticket.reaped and ticket.reap_failed
            assert not staged.exists() and discarded.exists()
        restarted = _decoder(tmp_path, spawn=spawn)
        restarted.start()
        assert not discarded.exists()
        assert unrelated.exists()
        assert restarted.images() == [] and heard == []
        restarted.stop()
    finally:
        upstream.die(7)
        child.close_pipe()
        assert ticket.complete.wait(3)


def test_sstv_only_clean_reaped_exit_zero_publishes_and_never_overwrites(
        tmp_path, capture_files):
    dec, launches, _ = capture_files
    with _client(tmp_path, probe=_present, sstv_factory=lambda: dec) as client:
        assert client.get("/api/sstv").status_code == 200
        child, staged = launches[0]
        staged.write_bytes(PNG_BYTES)
        assert dec.images() == []
        assert client.get("/api/receptions?instrument=sstv").json()["receptions"] == []
        _finish(child)
        dec.drain()
        dec.stop()
        dec.release()
        [record] = client.get("/api/receptions?instrument=sstv").json()["receptions"]
        payload = record["payload"]
        [image] = dec.images()
        assert payload["command"] == childproc.command_line(child)
        assert payload["freq_hz"] == DEFAULT_SSTV_HZ
        assert payload["item"] == image
        assert image["mode"] == "Robot 36"
        assert Path(image["path"]).name == staged.name
        assert client.get(f"/media/{image['path']}").status_code == 200


@pytest.mark.parametrize("failure", ["sidecar-write", "png-link", "source-unlink"])
def test_sstv_publication_commits_at_exclusive_png_link(capture_files, monkeypatch, failure):
    dec, launches, heard = capture_files
    dec.start()
    child, staged = launches[0]
    published = dec.output_dir / staged.name
    staged.write_bytes(PNG_BYTES)
    real_link, real_unlink, real_open = os.link, Path.unlink, Path.open
    attempted = []

    def open_path(path, mode="r", *args, **kwargs):
        if failure == "sidecar-write" and mode in ("w", "x") and path.suffix == ".json":
            attempted.append("sidecar-write")
            raise OSError("sidecar write failed")
        return real_open(path, mode, *args, **kwargs)

    def link(src, dst, *args, **kwargs):
        if failure == "png-link" and Path(dst) == published:
            attempted.append("png-link")
            raise OSError("PNG link failed")
        return real_link(src, dst, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        if failure == "source-unlink" and path == staged:
            attempted.append("source-unlink")
            raise OSError("staged source unlink failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(Path, "unlink", unlink)
    _finish(child)
    dec.stop()
    dec.stop()
    if failure == "source-unlink":
        assert staged != published  # Source cleanup follows a separate, visible commit.
        [report] = heard
        assert report[1] == childproc.command_line(child)
        assert report[0]["mode"] == "Robot 36"
        assert published.is_file() and staged.is_file()
    else:
        assert dec.images() == []
        assert not published.with_suffix(".json").exists()
        assert heard == []
        assert dec.error
        assert not staged.exists()
    assert attempted == [failure]


def test_sstv_rollover_retries_source_cleanup_and_launches_the_next_capture(
        capture_files, monkeypatch):
    dec, launches, heard = capture_files
    dec.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    real_unlink = Path.unlink
    attempts = 0

    def unlink(path, *args, **kwargs):
        nonlocal attempts
        if path == staged:
            attempts += 1
            if attempts == 1:
                raise OSError("staged source unlink failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    _finish(child)
    dec.start()

    assert len(launches) == 2 and dec.running
    assert dec.error is None
    assert attempts == 2 and not staged.exists()
    assert len(heard) == 1
    assert (dec.output_dir / staged.name).is_file()


def test_sstv_persistent_source_cleanup_does_not_mask_the_next_capture_exit(
        capture_files, monkeypatch):
    dec, launches, heard = capture_files
    dec.start()
    first, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    real_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == staged:
            raise OSError("staged source unlink failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    _finish(first)
    dec.stop()
    # Exercise the queued retry separately from the first cleanup failure at retirement.
    dec.error = None
    dec.start()
    assert staged in dec._staged_cleanup and staged.exists()
    second, _ = launches[1]
    _finish(second, code=1)
    dec.drain()

    with pytest.raises(SstvUnavailable, match="exit status 1"):
        dec.start()
    assert len(heard) == 1


@pytest.mark.parametrize("when", ["before", "at-link"])
def test_sstv_occupied_png_never_borrows_new_mode(capture_files, monkeypatch, when):
    dec, launches, heard = capture_files
    dec.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    published = dec.output_dir / staged.name
    original_size = 1
    seen = []
    real_link = os.link

    def inspect_gallery():
        seen.extend(image["mode"] for image in dec.images())

    def link(src, dst, *args, **kwargs):
        if Path(dst) == published and when == "at-link":
            published.write_bytes(b"x")
        inspect_gallery()
        return real_link(src, dst, *args, **kwargs)

    if when == "before":
        published.write_bytes(b"x")
        inspect_gallery()
    monkeypatch.setattr(os, "link", link)
    _finish(child)
    dec.stop()
    assert all(mode is None for mode in seen)
    [image] = dec.images()
    assert image["mode"] is None
    assert published.stat().st_size == original_size
    assert not published.with_suffix(".json").exists()
    assert heard == []
    assert dec.error
    assert not staged.exists()


@pytest.mark.parametrize("outcome", ["clean", "failed-reap", "unknown-reap", "nonzero"])
def test_sstv_settle_diagnostic_error_sweeps_only_its_confirmed_reaped_capture(
        capture_files, monkeypatch, outcome):
    dec, launches, heard = capture_files
    stage = _StubbornStage if outcome == "failed-reap" else LateProc
    upstream = stage(["rtl_fm"])
    spawn = dec._spawn
    links = []
    link = os.link

    def launch(*args):
        return childproc.Pipeline(upstream, spawn(*args), finite=True)

    class BrokenStderr(io.StringIO):
        def read(self, *args):
            raise OSError("diagnostics failed")

    def break_stderr(child):
        child.stderr.close()
        child.stderr = BrokenStderr()

    def record_link(source, target):
        link(source, target)
        links.append(Path(target).suffix)

    monkeypatch.setattr(dec, "_spawn", launch)
    monkeypatch.setattr(os, "link", record_link)
    dec.start()
    child, staged = launches[0]
    break_stderr(child)
    staged.write_bytes(PNG_BYTES)
    unrelated = staged.with_name("another-capture.png")
    unrelated.write_bytes(PNG_BYTES)
    code = 9 if outcome == "nonzero" else 0
    if outcome == "unknown-reap":
        monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.02)
        poll = upstream.poll

        def unknown():
            if upstream.terminated:
                raise OSError("exit status unavailable")
            return poll()

        monkeypatch.setattr(upstream, "poll", unknown)

    _finish(child, code=code)
    dec.stop()
    assert upstream.terminated
    assert not dec.running
    assert unrelated.exists()

    if outcome == "clean":
        assert upstream.poll() is not None and child.poll() == 0
        dec.stop()
        [(item, command, freq)] = heard
        assert item["meta"]["exit_diagnostic"] == {"status": 0, "reason": "diagnostics failed"}
        assert item["mode"] == "Robot 36"
        assert command == childproc.command_line(childproc.Pipeline(upstream, child, finite=True))
        assert freq == DEFAULT_SSTV_HZ
        assert links == [".png", ".json"]
        assert not staged.exists()
        assert len(dec.images()) == 1
    else:
        # A confirmed reap discards its own failed capture at once; an unconfirmed one keeps it.
        assert staged.exists() is (outcome != "nonzero")
        assert heard == []
        assert links == []
        assert dec.images() == []

    if outcome in ("unknown-reap", "failed-reap"):
        for _ in range(2):
            with pytest.raises(SstvUnavailable, match="survived kill|exit status unavailable"):
                dec.start()
        assert len(launches) == 1
        assert staged.exists()
        if outcome == "unknown-reap":
            ticket = dec._run
            monkeypatch.setattr(upstream, "poll", poll)
            assert ticket.complete.wait(3)
            assert ticket.reaped and ticket.reap_failed
            assert heard == [] and links == [] and dec.images() == []
            assert not staged.exists()
            monkeypatch.setattr(dec, "_spawn", spawn)
            dec.start()
            # Recovery must not suppress the next capture, or lose its own diagnostic fault.
            successor, next_staged = launches[1]
            break_stderr(successor)
            next_staged.write_bytes(PNG_BYTES)
            _finish(successor)
            dec.stop()
            [(record, _, _)] = heard
            assert Path(record["path"]).name == next_staged.name
            assert record["meta"]["exit_diagnostic"] == {
                "status": 0, "reason": "diagnostics failed",
            }
    elif outcome == "nonzero":
        assert dec._staged_cleanup == set()
        with pytest.raises(SstvUnavailable, match="exit status 9.*diagnostics failed"):
            dec.start()
        assert not staged.exists()
    assert unrelated.exists()


@pytest.mark.parametrize("collision", ["unreaped-writer", "orphan-sidecar"])
def test_sstv_run_numbered_names_isolate_delayed_writers_and_orphan_sidecars(
        capture_files, monkeypatch, collision):
    dec, launches, heard = capture_files
    if collision == "orphan-sidecar":
        dec.output_dir.mkdir()
        marker = json.dumps({"mode": "Scottie 1", "freq_hz": 145_800_000})
        clock = dec._clock

        def leave_orphan_at_launch():
            run = getattr(getattr(dec, "_intent", None), "number", dec._runs.current)
            suffix = "" if run is None else f"-r{run}"
            (dec.output_dir / f"20250101T000000-000{suffix}.json").write_text(
                marker, encoding="utf-8")
            return clock()

        monkeypatch.setattr(dec, "_clock", leave_orphan_at_launch)
        dec.start()
        child, staged = launches[0]
        staged.write_bytes(PNG_BYTES)
        _finish(child, mode=None)
        dec.stop()
        [image] = dec.images()
        assert (image["mode"], image["meta"]) == (None, {})
        return

    spawn = dec._spawn
    upstream = _StubbornStage(["rtl_fm"])
    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0)
    monkeypatch.setattr(
        dec, "_spawn", lambda *args: childproc.Pipeline(upstream, spawn(*args), finite=True))
    dec.start()
    first, first_path = launches[0]
    first_run = getattr(getattr(dec, "_runs", None), "current", None)
    first_path.write_bytes(PNG_BYTES)
    _finish(first)
    dec.stop()
    assert first_path.exists()
    for _ in range(2):
        with pytest.raises(SstvUnavailable, match="survived kill"):
            dec.start()
    assert len(launches) == 1 and heard == []
    ticket = dec._run
    upstream.die(7)
    assert ticket.complete.wait(5)
    assert not first_path.exists()
    monkeypatch.setattr(dec, "_spawn", spawn)
    dec.start()
    second, second_path = launches[1]
    second_run = getattr(getattr(dec, "_runs", None), "current", None)

    assert first_path != second_path
    assert first_path.stem.endswith(f"-r{first_run}")
    assert second_path.stem.endswith(f"-r{second_run}")
    first_path.write_bytes(b"x")
    second_path.write_bytes(PNG_BYTES)
    _finish(second)
    dec.stop()
    [report] = heard
    assert Path(report[0]["path"]).name == second_path.name
    assert (dec.output_dir / second_path.name).stat().st_size == len(PNG_BYTES)
    assert first_path.exists()


# --- bounded retirement and staged leftovers -------------------------------------


def test_sstv_stalled_exit_diagnostic_does_not_hold_a_clean_capture(capture_files, monkeypatch):
    dec, launches, heard = capture_files
    monkeypatch.setattr(receiver_lifecycle, "DIAGNOSTIC_WAIT_S", 0.05)
    stalled, unstall = threading.Event(), threading.Event()

    class StalledStderr(io.StringIO):
        def read(self, *args):
            stalled.set()
            unstall.wait(10)
            return ""

    dec.start()
    child, staged = launches[0]
    child.stderr.close()
    child.stderr = StalledStderr()
    staged.write_bytes(PNG_BYTES)
    ticket = dec._run
    try:
        _finish(child)
        assert stalled.wait(2)
        assert ticket.ready.wait(2), "a stalled exit diagnostic held the finished capture"
        assert ticket.complete.wait(2)
        assert ticket.reaped and not ticket.reap_failed
        # A confirmed clean reap still publishes, and says its diagnostic never finished.
        [(item, command, _)] = heard
        assert item["meta"]["exit_diagnostic"] == {
            "status": 0, "reason": "exit diagnostic did not finish",
        }
        assert command == childproc.command_line(child)
        assert [image["path"] for image in dec.images()] == [item["path"]]
        assert not staged.exists()
        dec.start()
        assert len(launches) == 2 and dec.running
    finally:
        unstall.set()


def test_sstv_failed_exit_discards_its_picture_at_retirement(capture_files):
    dec, launches, heard = capture_files
    dec.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    ticket = dec._run
    _finish(child, code=1)
    assert ticket.complete.wait(3)
    assert ticket.reaped
    # Gone now, not parked in memory for a later launch that a console restart would skip.
    assert not os.path.lexists(staged)
    assert not os.path.lexists(dec._discard_dir / staged.name)
    assert dec._staged_cleanup == set()
    assert heard == [] and dec.images() == []


def test_sstv_first_launch_sweeps_only_a_previous_consoles_staged_captures(
        tmp_path, capture_files):
    dec, launches, _ = capture_files
    staging = dec._staging_dir
    staging.mkdir(parents=True)
    leftovers = [staging / name for name in (
        "20250101T000000-000-r3.png", "20250101T000000-000-r3.json",
        "20250101T000000-000-r12-1.png", "20250101T000000-000-r10-10.json")]
    for path in leftovers:
        path.write_bytes(PNG_BYTES)
    # Each name is one edge of the generated form, off by one part; widening that part sweeps it.
    unrelated = [staging / name for name in (
        "notes.txt", "another-capture.png", "20250101T000000-000-r3.png.part",
        "x20250101T000000-000-r5.png", "20250101T000000-000-r5.PNG",
        "20250101T000000-000-r5.wav", "20250101T000000-000-r5.pngx",
        "20250101T000000-000-r5.png.json", "20250101T000000-000-r5",
        "20250101T000000-00-r5.png", "20250101T000000-0000-r5.png",
        "2025010T000000-000-r5.png", "202501011T000000-000-r5.png",
        "20250101T00000-000-r5.png", "20250101T0000000-000-r5.png",
        "20250101-000000-000-r5.png", "20250101t000000-000-r5.png",
        "20250101T000000-000-5.png", "20250101T000000-000-r.png",
        "20250101T000000-000-r0.png", "20250101T000000-000-r007.png",
        "20250101T000000-000-r5-0.png", "20250101T000000-000-r5-01.png",
        "20250101T000000-000-r5-.png", "20250101T000000-000-r5-1-1.png",
        "2025010١T000000-000-r5.png", "20250101T000000-000-r5.png\n")]
    for path in unrelated:
        path.write_bytes(b"keep")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "20250101T000000-000-r4.png"
    target.write_bytes(PNG_BYTES)
    link = staging / target.name
    link.symlink_to(target)
    same_named_dir = staging / "20250101T000000-000-r6.png"
    same_named_dir.mkdir()
    published = Path(_drop_image(dec.output_dir, "20250101T000000-000-r7.png", mode="Robot 36"))

    dec.start()
    assert [path.name for path in leftovers if os.path.lexists(path)] == []
    assert all(path.read_bytes() == b"keep" for path in unrelated)
    assert link.is_symlink() and target.read_bytes() == PNG_BYTES
    assert same_named_dir.is_dir()
    assert published.read_bytes() == PNG_BYTES
    assert published.with_suffix(".json").exists()

    # Only the first launch sweeps: later staged files may be this console's own writers.
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    _finish(child)
    dec.stop()
    later = staging / "20250101T000000-000-r8.png"
    later.write_bytes(PNG_BYTES)
    dec.start()
    assert len(launches) == 2
    assert later.exists()


@pytest.mark.parametrize("layout", [
    "staging-links-elsewhere", "staging-links-to-gallery", "gallery-links-to-staging"])
def test_sstv_staging_sweep_stays_inside_a_real_staging_dir(tmp_path, capture_files, layout):
    dec, launches, _ = capture_files
    if layout == "staging-links-elsewhere":
        real, linked = tmp_path / "elsewhere", dec._staging_dir
    elif layout == "staging-links-to-gallery":
        real, linked = dec.output_dir, dec._staging_dir
    else:
        real, linked = dec._staging_dir, dec.output_dir
    picture = Path(_drop_image(real, "20250101T000000-000-r7.png", mode="Robot 36"))
    linked.symlink_to(real, target_is_directory=True)
    dec.start()
    assert len(launches) == 1
    assert picture.read_bytes() == PNG_BYTES
    assert picture.with_suffix(".json").exists()
