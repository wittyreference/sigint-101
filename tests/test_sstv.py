# ABOUTME: Tests the SSTV backend — orchestrating rtl_fm|sox|sstv and discovering decoded PNGs.
# ABOUTME: The pipeline is a fake at the boundary; no radio / no tools / no picture is honest.

import base64
import json
import os

import pytest

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

    def __init__(self, lines, output_path=None):
        self.stdout = iter(lines)
        self.terminated = False
        if output_path is not None:
            os.makedirs(os.path.dirname(str(output_path)), exist_ok=True)
            with open(output_path, "wb") as handle:
                handle.write(PNG_BYTES)

    def poll(self):
        return None

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

def test_default_frequency_is_the_2m_sstv_calling_frequency():
    assert DEFAULT_SSTV_HZ == 145_800_000


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
    dec.drain()
    imgs = dec.images()
    assert len(imgs) == 1
    assert imgs[0]["mode"] == "Martin 1"
    assert imgs[0]["meta"]["freq_hz"] == DEFAULT_SSTV_HZ


def test_a_decode_with_no_reported_mode_writes_no_sidecar(tmp_path):
    dec = _decoder(tmp_path, ["Decoding image... done"])
    dec.start()
    dec.drain()
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
    dec.drain()
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
    dec = _decoder(tmp_path, ["Detected SSTV mode Martin 2"])
    dec.start()
    dec.drain()
    assert dec.images()
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
