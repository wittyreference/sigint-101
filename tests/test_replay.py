# ABOUTME: Exercises the 5a-1 recording loader, replay child and separate replay ledger.
# ABOUTME: Uses temporary SigMF pairs and child fakes, with no receiver or rtl_433 needed.

import ast
import hashlib
import importlib
import json
import os
import signal
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from threading import Event, Thread

import pytest

from sigint101.events import log_event, read_events
from sigint101.migrations import FIXTURES_PATH, migrate_store_copy, reviewed_signatures
from sigint101.progress import Progress
from sigint101.receiver_lifecycle import SupervisedReceiver
from sigint101.stamps import mint, stamps_from_log
from sigint101.storage import schema_signature
from tests.child_fakes import Child, LateProc, Output
from tests.test_app import _client
from tests.test_updater_storage import _build_progress_v0

DATA = bytes((127, 129, 130, 126))
LINE = '{"model":"Sensor","time":"@0.005","id":42,"freq":433.92,"snr":9.5}'
ARGV = ["rtl_433", "-F", "json", "-M", "level", "-s", "250000",
        "-f", "433920000", "-r", "cu8:-"]
DIGEST = hashlib.sha256(DATA).hexdigest()
MISSING = object()


def _metadata():
    return {
        "global": {
            "core:version": "1.2.0",
            "core:datatype": "cu8",
            "core:sample_rate": 250000,
            "core:hw": "RTL-SDR",
            "core:license": "https://creativecommons.org/publicdomain/zero/1.0/",
            "core:extensions": [{"name": "sigint101", "version": "1.0.0", "optional": False}],
            "sigint101:source": "Test capture, 2026-01-01, rtl_sdr -f 433920000",
            "sigint101:instrument": "ism",
        },
        "captures": [{"core:sample_start": 0, "core:frequency": 433920000,
                      "core:datetime": "2026-01-01T00:00:00Z"}],
        "annotations": [],
    }


def _pair(root, stem="capture", *, meta=None, data=DATA):
    root.mkdir(exist_ok=True)
    (root / f"{stem}.sigmf-meta").write_text(
        json.dumps(_metadata() if meta is None else meta), encoding="utf-8")
    data_path = root / f"{stem}.sigmf-data"
    data_path.write_bytes(data)
    return data_path


@pytest.fixture
def replay():
    return importlib.import_module("sigint101.replay")


@pytest.fixture
def recording(replay, tmp_path):
    root = tmp_path / "recordings"
    _pair(root)
    return replay.load_recording(root, "capture")


@pytest.mark.parametrize(("where", "key", "value"), [
    ("top", "global", []),
    ("top", "captures", []),
    ("top", "captures", [{}] * 2),
    ("top", "annotations", {}),
    ("top", "annotations", MISSING),
    ("global", "core:version", "2.0.0"),
    ("global", "core:version", MISSING),
    ("global", "core:datatype", "ci8"),
    ("global", "core:datatype", "cu16_le"),
    ("global", "core:datatype", "cf32_le"),
    ("global", "core:datatype", MISSING),
    ("global", "core:sample_rate", MISSING),
    ("global", "core:sample_rate", True),
    ("global", "core:sample_rate", 0),
    ("global", "core:sample_rate", 250000.1),
    ("global", "core:sample_rate", 3200001),
    ("global", "core:hw", " "),
    ("global", "core:hw", "x" * 201),
    ("global", "core:hw", MISSING),
    ("global", "core:license", " "),
    ("global", "core:license", MISSING),
    ("global", "core:extensions", []),
    ("global", "core:extensions", ["sigint101"]),
    ("global", "core:extensions", MISSING),
    ("global", "sigint101:source", " "),
    ("global", "sigint101:source", MISSING),
    ("global", "sigint101:instrument", "aprs"),
    ("global", "sigint101:instrument", MISSING),
    ("global", "core:num_channels", 2),
    ("global", "core:num_channels", True),
    ("global", "core:trailing_bytes", 2),
    ("global", "core:trailing_bytes", False),
    ("global", "core:metadata_only", True),
    ("global", "core:metadata_only", 0),
    ("global", "core:dataset", None),
    ("global", "core:geolocation", {}),
    ("global", "core:sha512", "a" * 127),
    ("capture", "core:sample_start", MISSING),
    ("capture", "core:sample_start", 1),
    ("capture", "core:frequency", MISSING),
    ("capture", "core:frequency", 118000000),
    ("capture", "core:frequency", 433920000.1),
    ("capture", "core:datetime", MISSING),
    ("capture", "core:datetime", "2026-01-01T00:00:00+00:00"),
    ("capture", "core:datetime", "2026-02-30T00:00:00Z"),
    ("capture", "core:header_bytes", 2),
    ("capture", "core:header_bytes", False),
])
def test_sigmf_fields_are_checked(replay, tmp_path, where, key, value):
    meta = _metadata()
    obj = {"top": meta, "global": meta["global"], "capture": meta["captures"][0]}[where]
    if value is MISSING:
        obj.pop(key)
    else:
        obj[key] = value
    _pair(tmp_path, meta=meta)
    with pytest.raises(replay.RecordingRefused) as refused:
        replay.load_recording(tmp_path, "capture")
    assert key in str(refused.value)


@pytest.mark.parametrize("raw", [b"[]", b"\xff", b'{"ignored": NaN}', b'{"ignored": Infinity}',
                                 b'{"ignored": -Infinity}', b" " * 65537])
def test_bad_sigmf_metadata_is_refused(replay, tmp_path, raw):
    _pair(tmp_path)
    (tmp_path / "capture.sigmf-meta").write_bytes(raw)
    with pytest.raises(replay.RecordingRefused, match="meta"):
        replay.load_recording(tmp_path, "capture")


@pytest.mark.parametrize("data", [b"", b"1", None])
def test_empty_odd_or_missing_data_is_refused(replay, tmp_path, data):
    path = _pair(tmp_path, data=DATA if data is None else data)
    if data is None:
        path.unlink()
    with pytest.raises(replay.RecordingRefused, match="data"):
        replay.load_recording(tmp_path, "capture")
    with pytest.raises(replay.RecordingNotFound):
        replay.load_recording(tmp_path, "unknown")


@pytest.mark.parametrize("stem", ["../x", "a/b", r"a\b", ".x", "A", "x.sigmf-meta", "x\0",
                                  "x" * 64, "x\n"])
def test_stems_are_confined(replay, tmp_path, stem):
    with pytest.raises(replay.RecordingRefused, match="stem"):
        replay.load_recording(tmp_path, stem)


@pytest.mark.parametrize("outside", [False, True])
def test_loader_refuses_symlinks_inside_and_outside_its_root(replay, tmp_path, outside):
    root = tmp_path / "recordings"
    path = _pair(root)
    target = (tmp_path if outside else root) / "target.data"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(replay.RecordingRefused, match="regular"):
        replay.load_recording(root, "capture")


@pytest.mark.parametrize("suffix", ["meta", "data"])
def test_loader_refuses_nonregular_files(replay, tmp_path, suffix):
    _pair(tmp_path)
    path = tmp_path / f"capture.sigmf-{suffix}"
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(replay.RecordingRefused, match="regular"):
        replay.load_recording(tmp_path, "capture")


@pytest.mark.parametrize("change", ["inode", "size", "mtime", "symlink", "root", "metadata"])
def test_open_refuses_replacement_after_loading(replay, tmp_path, change):
    root = tmp_path / "recordings"
    data = _pair(root)
    item = replay.load_recording(root, "capture")
    if change == "inode":
        data.rename(root / "old")
        data.write_bytes(DATA)
    elif change == "size":
        data.write_bytes(DATA * 2)
    elif change == "mtime":
        stamp = data.stat().st_mtime_ns
        os.utime(data, ns=(stamp, stamp + 100000))
    elif change == "symlink":
        data.rename(root / "old")
        data.symlink_to(root / "old")
    elif change == "root":
        root.rename(tmp_path / "previous")
        root.symlink_to(tmp_path / "previous", target_is_directory=True)
    else:
        meta = root / "capture.sigmf-meta"
        meta.rename(root / "old")
        meta.write_text(json.dumps(_metadata()), encoding="utf-8")
    for verify in (replay.digest_of, replay.open_data):
        with pytest.raises(replay.RecordingRefused, match="changed since it was listed"):
            verify(item)


def test_open_uses_nofollow_even_if_last_check_races(replay, tmp_path, monkeypatch):
    data = _pair(tmp_path)
    item = replay.load_recording(tmp_path, "capture")
    open_file = os.open
    swapped = False

    def race(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path).name == data.name and not swapped:
            assert flags & os.O_NOFOLLOW
            swapped = True
            data.rename(tmp_path / "old")
            data.symlink_to(tmp_path / "old")
        return open_file(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    with pytest.raises(replay.RecordingRefused, match="changed"):
        replay.open_data(item)
    assert swapped


def test_pair_and_folder_caps_count_metadata_and_all_regular_files(replay, tmp_path):
    path = _pair(tmp_path)
    size = (tmp_path / "capture.sigmf-meta").stat().st_size
    data_size = ((5_000_000 - size) // 2 + 1) * 2
    with path.open("r+b") as file:
        file.truncate(data_size)
    with pytest.raises(replay.RecordingRefused, match="5 MB"):
        replay.load_recording(tmp_path, "capture")

    _pair(tmp_path)
    _pair(tmp_path, "second")
    with (tmp_path / "extra").open("wb") as file:
        file.truncate(25_000_000)
    valid, refused = replay.list_recordings(tmp_path)
    assert valid == []
    assert [stem for stem, _ in refused] == ["capture", "second"]
    assert all("25 MB" in reason for _, reason in refused)
    with pytest.raises(replay.RecordingRefused, match="recordings folder must be at most 25 MB"):
        replay.load_recording(tmp_path, "capture")


def test_listing_is_sorted_and_returns_refusals(replay, tmp_path):
    _pair(tmp_path, "zulu")
    _pair(tmp_path, "alpha")
    _pair(tmp_path, "bad", data=b"")
    valid, refused = replay.list_recordings(tmp_path)
    assert [item.stem for item in valid] == ["alpha", "zulu"]
    assert [stem for stem, _ in refused] == ["bad"]
    assert replay.list_recordings(tmp_path / "absent") == ([], [])


@pytest.mark.parametrize("matches", [False, True])
def test_digest_is_of_the_data_and_checks_optional_sha512(replay, tmp_path, matches):
    meta = _metadata()
    meta["global"]["core:sha512"] = hashlib.sha512(DATA).hexdigest() if matches else "0" * 128
    meta["global"]["ignored"] = {"key": 1}
    _pair(tmp_path, meta=meta)
    item = replay.load_recording(tmp_path, "capture")
    if matches:
        assert replay.digest_of(item) == DIGEST
    else:
        with pytest.raises(replay.RecordingRefused, match="core:sha512"):
            replay.digest_of(item)


def _until(predicate):
    until = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < until
        time.sleep(0.005)


def _decoder(replay, spawn, **kwargs):
    return replay.ReplayDecoder(spawn=spawn, which=lambda _: "rtl_433", **kwargs)


def _snapshot(decoder, state):
    snapshot = decoder.snapshot()
    assert snapshot.keys() == {
        "mode", "replay", "instrument", "state", "recording", "digest", "frequency_hz",
        "sample_rate", "command", "input", "decodes", "reason", "retry_in",
    }
    assert snapshot["mode"] == "replay" and snapshot["replay"] is True
    assert snapshot["instrument"] == "ism" and snapshot["state"] == state
    return snapshot


def test_replay_structure_and_read_only_reception_hook(replay):
    assert issubclass(replay.ReplayDecoder, SupervisedReceiver)
    decoder = _decoder(replay, lambda *a, **kw: pytest.fail("no replay was started"))
    assert decoder.watch.tool == "rtl_433" and decoder.watch.finite
    assert decoder.on_reception is None
    with pytest.raises(AttributeError):
        decoder.on_reception = lambda _: None
    tree = ast.parse(Path(replay.__file__).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module == "decode"
                for alias in node.names}
    assert imported == {"Decode", "parse_decode_line"}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
    assert names.isdisjoint({"stop_process", "Thread", "emit_reception", "reception_record",
                             "probe_rtlsdr", "rtlsdr_radio_for_run", "_admit", "_emit"})


def test_replay_exact_command_and_checked_stdin(replay, recording, tmp_path):
    opened = []

    def spawn(argv, *, stdin, stdout, text):
        assert argv == ARGV and stdout == subprocess.PIPE and text
        opened.append(stdin)
        assert os.read(stdin, len(DATA)) == DATA
        proc = Child(argv, code=0)
        proc.stdout = Output([LINE])
        return proc

    decoder = _decoder(replay, spawn, events_path=tmp_path / "events.jsonl")
    _snapshot(decoder, "idle")
    assert replay.replay_command(recording) == ARGV
    decoder.start_replay(recording)
    assert decoder._run.complete.wait(3)
    with pytest.raises(OSError):
        os.fstat(opened[0])
    snapshot = _snapshot(decoder, "finished")
    assert decoder.watch.blocked() is None
    assert snapshot["recording"] == "capture" and snapshot["digest"] == DIGEST
    assert snapshot["input"] == "recordings/capture.sigmf-data"
    assert snapshot["command"] == " ".join(ARGV)
    assert [item["ts"] for item in snapshot["decodes"]] == ["@0.005"]


def test_spawn_failure_closes_the_checked_descriptor(replay, recording):
    opened = []

    def spawn(_argv, *, stdin, **_):
        opened.append(stdin)
        raise OSError("spawn failed")

    decoder = _decoder(replay, spawn)
    with pytest.raises(replay.ReplayUnavailable, match="spawn failed"):
        decoder.start_replay(recording)
    _snapshot(decoder, "error")
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_a_failed_replay_restart_clears_the_previous_rows(replay, recording):
    attempts = 0

    def spawn(argv, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("spawn failed")
        proc = Child(argv, code=0)
        proc.stdout = Output([LINE])
        return proc

    decoder = _decoder(replay, spawn)
    decoder.start_replay(recording)
    assert decoder._run.complete.wait(3)
    assert decoder.snapshot()["decodes"]
    with pytest.raises(replay.ReplayUnavailable, match="spawn failed"):
        decoder.start_replay(recording)
    failed = _snapshot(decoder, "error")
    assert "spawn failed" in failed["reason"]
    assert failed["decodes"] == [] and failed["command"] is None


def test_swap_between_digest_and_spawn_never_opens_the_replacement(replay, tmp_path, monkeypatch):
    data = _pair(tmp_path)
    item = replay.load_recording(tmp_path, "capture")
    original_digest = replay.digest_of

    def swap_after_hash(recording):
        digest = original_digest(recording)
        data.rename(tmp_path / "old")
        data.symlink_to(tmp_path / "old")
        return digest

    monkeypatch.setattr(replay, "digest_of", swap_after_hash)
    decoder = _decoder(replay, lambda *args, **kwargs: pytest.fail("spawned with replaced input"))
    with pytest.raises(replay.RecordingRefused, match="changed since it was listed"):
        decoder.start_replay(item)


def test_fifo_swapped_in_at_spawn_is_refused_without_a_blocking_open(replay, tmp_path, monkeypatch):
    data = _pair(tmp_path)
    item = replay.load_recording(tmp_path, "capture")
    digest_of, open_file = replay.digest_of, os.open
    hashed = swapped = False

    def after_hash(recording):
        nonlocal hashed
        digest = digest_of(recording)
        hashed = True
        return digest

    def race(path, flags, *args, **kwargs):
        nonlocal swapped
        if hashed and Path(path).name == data.name:
            data.unlink()
            os.mkfifo(data)
            swapped = True
            # Check before the real open so the regression cannot hang the test worker.
            assert flags & os.O_NONBLOCK
        return open_file(path, flags, *args, **kwargs)

    monkeypatch.setattr(replay, "digest_of", after_hash)
    monkeypatch.setattr(os, "open", race)
    decoder = _decoder(replay, lambda *a, **kw: pytest.fail("spawned with a replaced FIFO"))
    with pytest.raises(replay.RecordingRefused, match="changed since it was listed"):
        decoder.start_replay(item)
    assert swapped
    _snapshot(decoder, "error")


def test_stop_during_spawn_leaves_replay_stopped(replay, recording, tmp_path):
    entered, resume = Event(), Event()
    child = Child(ARGV)
    events_path = tmp_path / "events.jsonl"

    def spawn(*_args, **_kwargs):
        entered.set()
        assert resume.wait(5)
        return child

    decoder = _decoder(replay, spawn, events_path=events_path)
    refusals = []

    def start():
        try:
            decoder.start_replay(recording)
        except replay.ReplayUnavailable as exc:
            refusals.append(str(exc))

    starter = Thread(target=start, daemon=True)
    stopper = Thread(target=decoder.stop, daemon=True)
    starter.start()
    try:
        try:
            assert entered.wait(3)
            intent = decoder._intent
            stopper.start()
            _until(lambda: intent.cancelled)
        finally:
            resume.set()
            starter.join(3)
            if stopper.ident is not None:
                stopper.join(3)
        assert not starter.is_alive() and not stopper.is_alive()
        assert refusals == ["receiver start was cancelled"]
        assert child.sent == ["TERM"] and decoder._run.reaped
        assert _snapshot(decoder, "stopped")["reason"] is None
        assert read_events(events_path)[-1]["payload"]["state"] == "stopped"
    finally:
        decoder.shutdown()


def test_replay_stop_fences_before_signalling_and_drops_late_lines(replay, recording, tmp_path):
    events_path = tmp_path / "events.jsonl"
    child = LateProc(ARGV)
    decoder = _decoder(replay, lambda *args, **kwargs: child, events_path=events_path)
    decoder.start_replay(recording)
    run = decoder._run
    child.feed(LINE)
    _until(lambda: len(decoder.snapshot()["decodes"]) == 1)
    _snapshot(decoder, "replaying")
    terminate = child.terminate

    def signal_then_feed():
        with decoder._runs:
            assert not decoder._runs.is_current(run.number)
        terminate()
        child.feed(LINE.replace("42", "43"))
        child.close_pipe()

    child.terminate = signal_then_feed
    try:
        decoder.stop()
        assert run.complete.wait(3)
        snapshot = _snapshot(decoder, "stopped")
        assert [row["readings"]["id"] for row in snapshot["decodes"]] == [42]
        assert [evt["kind"] for evt in read_events(events_path)] == [
            "replay_start", "replay", "replay_end",
        ]
    finally:
        child.close_pipe()
        decoder.shutdown()


def test_stop_reports_after_an_admitted_replay_line(replay, recording, tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    child = LateProc(ARGV)
    decoder = _decoder(replay, lambda *a, **kw: child, events_path=events_path)
    accepted, resume, reporting, wrote_end = (Event() for _ in range(4))
    journal = decoder._journal
    report = decoder._report

    def park(playback, kind, payload):
        if kind == "replay":
            accepted.set()
            assert resume.wait(3)
        journal(playback, kind, payload)
        if kind == "replay_end":
            wrote_end.set()

    def report_entered(run):
        reporting.set()
        report(run)

    monkeypatch.setattr(decoder, "_journal", park)
    monkeypatch.setattr(decoder, "_report", report_entered)
    decoder.start_replay(recording)
    child.feed(LINE)
    assert accepted.wait(3)
    stopper = Thread(target=decoder.stop, daemon=True)
    stopper.start()
    try:
        assert reporting.wait(3)
        assert not wrote_end.wait(0.1), "replay_end overtook an admitted line"
    finally:
        resume.set()
        child.close_pipe()
        stopper.join(3)
    assert not stopper.is_alive()
    assert [event["kind"] for event in read_events(events_path)] == [
        "replay_start", "replay", "replay_end",
    ]


def test_replay_failure_diagnostic_and_then_restart_after_backoff(replay, recording):
    children = [Child(ARGV, code=1, said="cannot decode samples"), Child(ARGV, code=0)]
    decoder = _decoder(replay, lambda *_args, **_kwargs: children.pop(0))
    now = [100.0]
    decoder.watch.clock = lambda: now[0]
    decoder.start_replay(recording)
    assert decoder._run.complete.wait(3)
    snapshot = _snapshot(decoder, "error")
    assert "cannot decode samples" in snapshot["reason"]
    assert 0 < snapshot["retry_in"] <= 2
    with pytest.raises(replay.ReplayUnavailable, match="cannot decode samples"):
        decoder.start_replay(recording)
    assert len(children) == 1
    now[0] += 60
    decoder.start_replay(recording)  # No one-shot stale death after a status read.
    assert decoder._run.complete.wait(3)
    assert not children and decoder.snapshot()["state"] == "finished"


def test_unreaped_replay_refuses_until_supervisor_confirms_exit(replay, recording, monkeypatch):
    child = Child(ARGV, unkillable=True)
    decoder = _decoder(replay, lambda *args, **kwargs: child)
    monkeypatch.setattr("sigint101.receiver_lifecycle.REAP_RETRY_MIN_S", 0.01)
    decoder.start_replay(recording)
    run = decoder._run
    try:
        decoder.stop()
        reason = decoder.release_failure(pending=True)
        assert "survived kill" in reason
        with pytest.raises(replay.ReplayUnavailable, match="survived kill"):
            decoder.start_replay(recording)
        assert child.poll() is None and not run.complete.is_set()
    finally:
        child.unkillable = False
    assert run.complete.wait(3) and run.reaped


def test_replay_cap_stops_the_exact_child(replay, recording, monkeypatch, tmp_path):
    monkeypatch.setattr(replay, "REPLAY_MAX_S", 0.05)
    child = LateProc(ARGV)
    events_path = tmp_path / "events.jsonl"
    decoder = _decoder(replay, lambda *args, **kwargs: child, events_path=events_path)
    decoder.start_replay(recording)
    run = decoder._run
    try:
        assert run.complete.wait(3)
        assert child.returncode == -signal.SIGTERM
        snapshot = decoder.snapshot()
        assert snapshot["state"] == "error"
        assert snapshot["reason"] == "the replay ran past 30 s and was stopped"
        last = read_events(events_path)[-1]
        assert last["kind"] == "replay_end" and last["payload"]["state"] == "error"
    finally:
        child.close_pipe()
        decoder.shutdown()


def _read_only_stamps(db):
    with closing(sqlite3.connect(db)) as conn:
        return ([tuple(row) for row in conn.execute("SELECT * FROM stamps")],
                [tuple(row) for row in conn.execute("SELECT * FROM stamp_events")])


def test_replay_events_and_rows_have_no_live_effect(replay, recording, tmp_path):
    events_path = tmp_path / "events.jsonl"
    db = tmp_path / "progress.db"
    progress = Progress(db)

    def spawn(argv, **_):
        proc = Child(argv, code=0)
        proc.stdout = Output([LINE.replace("42", str(i)) for i in range(42)])
        return proc

    before = _read_only_stamps(db)
    decoder = _decoder(replay, spawn, events_path=events_path, progress=progress)
    try:
        for _ in range(2):
            decoder.start_replay(recording)
            assert decoder._run.complete.wait(3)
        assert len(decoder.snapshot()["decodes"]) == 40
        assert decoder.snapshot()["decodes"][0]["readings"]["id"] == 2
        assert progress.replays_seen()["ism"]["count"] == 2
    finally:
        decoder.shutdown()
        progress.close()

    events = read_events(events_path)
    assert [event["kind"] for event in events] == [
        "replay_start", *(["replay"] * 40), "replay_end",
    ] * 2
    assert all(mint(event) is None for event in events)
    assert list(stamps_from_log(events)) == []
    assert events[1]["payload"] == {
        "instrument": "ism", "recording": "capture", "digest": DIGEST,
        "replay": True, "source": "replay", "item": replay.parse_decode_line(
            LINE.replace("42", "0")).as_dict(),
    }
    with _client(tmp_path) as client:
        assert client.get("/api/receptions").json()["receptions"] == []
    assert _read_only_stamps(db) == before


def _earn_stamp(progress, events_path):
    payload = {"instrument": "ism", "tool": "rtl_433", "freq_hz": 433920000,
               "command": "rtl_433 -F json", "radio": {"kind": "rtl-sdr", "index": 0},
               "item": {"model": "Sensor"}}
    event = log_event(events_path, "reception", payload)
    stamp = mint(event)
    assert stamp is not None
    assert progress.record_stamps([stamp])


def test_replay_ledger_survives_reopen_and_log_rotation_alongside_a_stamp(tmp_path):
    path = tmp_path / "progress.db"
    events_path = tmp_path / "events.jsonl"
    progress = Progress(path)
    try:
        _earn_stamp(progress, events_path)
        before = progress.stamps()
        assert progress.record_replay_seen("ism", recording="first", digest="a", ts=123)
        assert progress.record_replay_seen("ism", recording="second", digest="b", ts=456)
        assert progress.stamps() == before
    finally:
        progress.close()

    assert not progress.record_replay_seen("ism", recording="third", digest="c", ts=789)
    events_path.rename(tmp_path / "events.jsonl.1")
    events_path.touch()
    reopened = Progress(path)
    try:
        assert reopened.replays_seen() == {
            "ism": {"first_ts": 123, "count": 2, "recording": "first", "digest": "a"},
        }
        assert reopened.stamps() == before
    finally:
        reopened.close()


def test_existing_progress_database_gains_replay_table_without_losing_any_rows(tmp_path):
    path = tmp_path / "progress.db"
    expected = _build_progress_v0(path, "v0_challenges_retrieval")
    progress = Progress(path)
    try:
        assert progress.replays_seen() == {}
        with closing(sqlite3.connect(path)) as conn:
            for table, rows in expected.items():
                assert conn.execute(f'SELECT * FROM "{table}"').fetchall() == rows
    finally:
        progress.close()


def test_replay_schema_enrolls_main_legacy_layout_without_touching_source(tmp_path):
    source = tmp_path / "old" / "previous.db"
    source.parent.mkdir()
    expected = _build_progress_v0(source, "v0_challenges_retrieval")
    signature = reviewed_signatures("progress")["v0"]["v0_challenges_retrieval"]
    with closing(sqlite3.connect(source)) as conn:
        assert schema_signature(conn) == signature
    original = source.read_bytes()
    dest = tmp_path / "next" / "progress.db"
    receipt = migrate_store_copy(source, dest, store="progress", scratch_dir=tmp_path / "scratch")
    assert source.read_bytes() == original
    assert receipt["from_version"] == 0 and receipt["to_version"] == 1 and receipt["migrated"]
    assert receipt["matched_v0"] == "v0_challenges_retrieval"
    with closing(Progress(dest, managed=True)) as managed:
        assert managed.replays_seen() == {}
        assert managed.record_replay_seen("ism", recording="clip", digest="hash", ts=123)
        assert managed.replays_seen()["ism"]["count"] == 1
    with closing(sqlite3.connect(dest)) as conn:
        for table, rows in expected.items():
            assert conn.execute(f'SELECT * FROM "{table}"').fetchall() == rows


@pytest.mark.parametrize(("variant", "base"), [
    ("v0_replays_seen", "v0_1a35a1a"),
    ("v0_retrieval_replays_seen", "v0_retrieval"),
    ("v0_challenges_retrieval_replays_seen", "v0_challenges_retrieval"),
])
def test_replay_legacy_variants_are_frozen(variant, base):
    frozen = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))["progress"]
    assert reviewed_signatures("progress") == frozen
    replay = frozen["v1"]["tables"]["replays_seen"]
    before = frozen["v0"][base]
    tables = {**before["tables"], "replays_seen": replay}
    assert frozen["v0"][variant] == {**before, "tables": tables}


def test_migrating_legacy_progress_preserves_replays_already_seen(tmp_path):
    source = tmp_path / "old" / "progress.db"
    source.parent.mkdir()
    legacy = Progress(source)
    try:
        assert legacy.record_replay_seen("ism", recording="clip", digest="hash", ts=123)
        before = legacy.replays_seen()
    finally:
        legacy.close()
    dest = tmp_path / "next" / "progress.db"
    report = migrate_store_copy(source, dest, store="progress", scratch_dir=tmp_path / "scratch")
    assert report["from_version"] == 0 and report["to_version"] == 1
    assert report["matched_v0"] == "v0_challenges_retrieval_replays_seen"
    managed = Progress(dest, managed=True)
    try:
        assert managed.replays_seen() == before
    finally:
        managed.close()


def test_replay_wait_error_escalates_before_returning(replay, recording, monkeypatch):
    class WaitError(Child):
        def wait(self, timeout=None):
            if self.sent == ["TERM"]:
                raise OSError("injected wait error")
            return super().wait(timeout)

    child = WaitError(ARGV, stubborn=True)
    monkeypatch.setattr("sigint101.receiver_lifecycle.REAP_RETRY_MIN_S", 30)
    decoder = _decoder(replay, lambda *args, **kwargs: child)
    try:
        decoder.start_replay(recording)
        decoder.stop()
        assert child.sent == ["TERM", "KILL"]
        assert not decoder.release_failure(pending=True)
    finally:
        child.code = -9
        decoder.shutdown()


@pytest.mark.parametrize("control", ["stop", "release", "retune"])
def test_later_replay_control_retries_unconfirmed_reap_now(
        replay, recording, monkeypatch, control):
    child = Child(ARGV, unkillable=True)
    monkeypatch.setattr("sigint101.receiver_lifecycle.REAP_RETRY_MIN_S", 30)
    decoder = _decoder(replay, lambda *args, **kwargs: child)
    try:
        decoder.start_replay(recording)
        decoder.stop()
        assert decoder.release_failure(pending=True)
        child.unkillable = False
        getattr(decoder, control)(*([433_000_000] if control == "retune" else []))
        assert child.sent == ["TERM", "KILL", "KILL"]
        assert not decoder.release_failure(pending=True)
    finally:
        child.code = -9
        decoder.shutdown()


def test_replay_reports_a_refused_kill_as_unconfirmed(replay, recording, monkeypatch):
    class RefusedKill(Child):
        def kill(self):
            if refused:
                raise PermissionError("injected signal refusal")
            super().kill()

    refused = True
    child = RefusedKill(ARGV, stubborn=True)
    monkeypatch.setattr("sigint101.receiver_lifecycle.REAP_RETRY_MIN_S", 30)
    decoder = _decoder(replay, lambda *args, **kwargs: child)
    try:
        decoder.start_replay(recording)
        decoder.stop()
        assert "could not be confirmed stopped" in decoder.release_failure(pending=True)
    finally:
        refused = False
        decoder.shutdown()
