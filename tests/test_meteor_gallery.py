# ABOUTME: Tests Meteor gallery publication, manifest-restricted media, separate retention,
# ABOUTME: symlink-safe pruning, managed sync durability, pre-launch freq_hz, and handoff listen.

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import struct
import threading
import zlib
from pathlib import Path, PurePosixPath

import pytest

from sigint101 import meteor, storage
from sigint101.events import read_events
from sigint101.hardware import RtlSdrDevice
from tests.test_meteor import MARKER, VALID_PNG, eventually, progress_line
from tests.test_meteor import meteor_rig as meteor_rig
from tests.test_meteor_routes import LISTEN, STATUS, meteor_client

PNG_BYTES = VALID_PNG


def _write_dataset(proc, satellite: str | None = "METEOR-M2 3") -> None:
    out = Path(proc.args[3])
    out.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": 1732795200}
    if satellite is not None:
        payload["satellite"] = satellite
    (out / "dataset.json").write_text(json.dumps(payload), encoding="utf-8")


def _complete_pass(decoder, proc, *, pictures=("MSU-MR-1.png",), satellite="METEOR-M2 3",
                   with_lock=True):
    if with_lock:
        proc.feed(progress_line())
    proc.feed(MARKER)
    assert decoder._run.physical.wait(2)
    for pic in pictures:
        proc.picture(pic)
    if satellite is not None:
        _write_dataset(proc, satellite)
    proc.die(0)
    proc.close_pipe()
    assert decoder._run.complete.wait(3)


@pytest.mark.parametrize("hz", ["137900000", "137100000"])
def test_meteor_freq_hz_reads_configured_site_frequency_before_first_launch(
        rig, monkeypatch, hz):
    decoder, _, _ = rig
    monkeypatch.setenv("SIGINT101_METEOR_HZ", hz)
    assert decoder.freq_hz == int(hz)


def test_meteor_listen_during_handoff_refuses_honestly_without_pretending_to_start(
        rig, tmp_path, monkeypatch):
    decoder, made, _ = rig
    monkeypatch.setattr(meteor, "RELEASE_GRACE_S", 1.5)
    with meteor_client(tmp_path, decoder) as client:
        assert client.post(LISTEN).status_code == 200
        proc = made[0][0]
        # Start a handoff (another claimant releasing Meteor) while SatDump has not yet
        # printed the close marker.
        releaser = threading.Thread(target=decoder.release, daemon=True)
        releaser.start()
        eventually(lambda: proc.term.is_set())
        # A Listen now tap during the handoff must refuse honestly with 503, never 200.
        tap = client.post(LISTEN)
        assert tap.status_code == 503
        assert "releasing" in tap.json()["reason"].lower()
        assert len(made) == 1
        # Unblock release cleanly.
        proc.feed(MARKER)
        releaser.join(timeout=3)
        proc.die(0)
        proc.close_pipe()
        assert decoder._run.complete.wait(3)


def test_meteor_gallery_uses_first_run_provenance_after_census_change(
        rig, tmp_path, monkeypatch):
    decoder, made, settings = rig
    settings["utc"] = 1_732_795_200.0
    monkeypatch.setenv("SIGINT101_METEOR_HZ", "137100000")
    monkeypatch.setenv("SIGINT101_METEOR_MODE", "80k")
    with meteor_client(tmp_path, decoder) as client:
        assert client.post(LISTEN).status_code == 200
        proc = made[0][0]
        # Mutate bus census and site env AFTER launch; published provenance must keep launch values.
        settings["devices"] = [RtlSdrDevice(9, "replacement-rx")]
        monkeypatch.setenv("SIGINT101_METEOR_HZ", "137900000")
        monkeypatch.setenv("SIGINT101_METEOR_MODE", "72k")
        _complete_pass(decoder, proc, pictures=("MSU-MR-rgb_122.png",), satellite="METEOR-M2 4")

        status = client.get(STATUS).json()
        assert status["state"] == "done"
        assert len(status["passes"]) == 1
        row = status["passes"][0]
        assert row["freq_hz"] == 137_100_000
        assert row["mode"] == "80k"
        assert row["radio"] == {"kind": "rtl-sdr", "serial": "rx", "index": 4}
        assert row["satellite"] == "METEOR-M2 4"
        assert row["result"] == "done"
        assert row["lock_ever"] is True
        assert row["ended_by"] == "self_exit"
        assert row["started_at"] == 1_732_795_200.0
        assert isinstance(row["ended_at"], (int, float))
        assert "--frequency 137100000" in row["command"]
        assert row["images"] == [{
            "path": f"meteor/{row['id']}/MSU-MR-rgb_122.png",
            "product": "rgb_122",
        }]

        # Refused POST still carries the last saved gallery.
        settings["present"] = False
        refusal = client.post(LISTEN)
        assert refusal.status_code == 503
        assert len(refusal.json()["passes"]) == 1
        assert refusal.json()["passes"][0]["id"] == row["id"]

        # Reception event carries immutable launch provenance.
        receptions = [
            e for e in read_events(tmp_path / "events.jsonl")
            if e["kind"] == "reception" and e["payload"].get("instrument") == "meteor"
        ]
        assert len(receptions) == 1
        pl = receptions[0]["payload"]
        assert pl["freq_hz"] == 137_100_000
        assert pl["radio"] == {"kind": "rtl-sdr", "serial": "rx", "index": 4}
        assert pl["item"]["pass_id"] == row["id"]
        assert pl["item"]["path"] == row["images"][0]["path"]


def test_meteor_pass_is_published_once_after_post_marker_claim(rig, tmp_path):
    decoder, made, _ = rig
    with meteor_client(tmp_path, decoder) as client:
        assert client.post(LISTEN).status_code == 200
        proc = made[0][0]
        proc.feed(progress_line())
        proc.feed(MARKER)
        assert decoder._run.physical.wait(2)
        # Spectrum claims radio after close marker; SatDump finishes CPU processing unharmed.
        assert client.get("/api/spectrum").status_code == 200
        assert proc.sent == [("nice", meteor.FINISH_NICE)] or "TERM" not in proc.sent
        proc.picture("MSU-MR-1.png")
        proc.die(0)
        proc.close_pipe()
        assert decoder._run.complete.wait(3)

        passes = client.get(STATUS).json()["passes"]
        assert len(passes) == 1
        assert passes[0]["satellite"] == "Meteor (unidentified)"
        # Repeat polls never re-emit receptions.
        client.get(STATUS)
        receptions = [
            e for e in read_events(tmp_path / "events.jsonl")
            if e["kind"] == "reception" and e["payload"].get("instrument") == "meteor"
        ]
        assert len(receptions) == 1
        stamps = client.get("/api/stamps").json()["stamps"]
        m_stamp = next(s for s in stamps if s["instrument"] == "meteor")
        assert m_stamp["count"] == 1


def test_meteor_media_serves_only_its_manifest_members(rig, tmp_path):
    decoder, made, _ = rig
    with meteor_client(tmp_path, decoder) as client:
        assert client.post(LISTEN).status_code == 200
        proc = made[0][0]
        _complete_pass(decoder, proc, pictures=("MSU-MR-1.png",), satellite=None)

        row = client.get(STATUS).json()["passes"][0]
        pid = row["id"]
        img_path = row["images"][0]["path"]

        # Verified manifest member serves as image/png.
        ok = client.get(f"/media/{img_path}")
        assert ok.status_code == 200
        assert ok.content.startswith(b"\x89PNG\r\n\x1a\n")

        # Unlisted intermediate PNG inside the published pass directory is refused.
        pass_dir = tmp_path / "meteor" / pid
        (pass_dir / "intermediate.png").write_bytes(PNG_BYTES)
        assert client.get(f"/media/meteor/{pid}/intermediate.png").status_code == 404

        # Symlink substituted for a manifest member is refused via O_NOFOLLOW.
        outside = tmp_path / "outside.png"
        outside.write_bytes(PNG_BYTES)
        real_img = tmp_path / img_path
        real_img.unlink()
        real_img.symlink_to(outside)
        assert client.get(f"/media/{img_path}").status_code == 404

        # Traversal and non-pass paths are refused.
        assert client.get(f"/media/meteor/{pid}/../outside.png").status_code == 404
        assert client.get("/media/meteor/tle.txt").status_code == 404
        assert client.get("/media/meteor/work/anything.png").status_code == 404


def test_meteor_picture_survives_25_quiet_runs(rig, tmp_path):
    decoder, made, settings = rig
    settings["utc"] = 1_732_700_000.0
    with meteor_client(tmp_path, decoder) as client:
        # Publish one real picture first.
        assert client.post(LISTEN).status_code == 200
        _complete_pass(decoder, made[-1][0], pictures=("MSU-MR-1.png",))
        first_id = client.get(STATUS).json()["passes"][0]["id"]

        # Run 25 quiet passes (no pictures) — published pass must survive, quiet capped at 5.
        for i in range(25):
            settings["utc"] += 100.0
            assert client.post(LISTEN).status_code == 200
            proc = made[-1][0]
            proc.feed(MARKER)
            assert decoder._run.physical.wait(2)
            proc.die(0)
            proc.close_pipe()
            assert decoder._run.complete.wait(3)

        passes = client.get(STATUS).json()["passes"]
        assert [p["id"] for p in passes] == [first_id]
        quiet_dir = tmp_path / "meteor" / "quiet"
        assert quiet_dir.is_dir()
        assert len(list(quiet_dir.glob("*.json"))) <= meteor.MAX_QUIET_RUNS

        # Publish 21 more passes — published gallery capped at 20, oldest evicted cleanly.
        for i in range(21):
            settings["utc"] += 100.0
            assert client.post(LISTEN).status_code == 200
            _complete_pass(decoder, made[-1][0], pictures=(f"MSU-MR-{i}.png",))

        passes_after = client.get(STATUS).json()["passes"]
        assert len(passes_after) == meteor.MAX_PUBLISHED_PASSES
        assert first_id not in [p["id"] for p in passes_after]
        assert not (tmp_path / "meteor" / first_id).exists()


def test_meteor_prune_skips_live_owners_and_never_follows_links(rig, tmp_path):
    decoder, made, _ = rig
    root = tmp_path / "meteor"
    work = root / "work"
    work.mkdir(parents=True, mode=0o700)
    tle = root / "tle.txt"
    tle.write_text("METEOR-M2 3\n", encoding="utf-8")

    # Unknown directory inside work/ stays untouched.
    unknown = work / "keep-me"
    unknown.mkdir(mode=0o700)
    (unknown / "note.txt").write_text("do not delete", encoding="utf-8")

    # Live owner dir with held exclusive flock on .owner.lock stays untouched.
    live_id = "a" * 32
    live_dir = work / live_id
    live_dir.mkdir(mode=0o700)
    live_lock_path = live_dir / ".owner.lock"
    live_lock_path.touch(mode=0o600)
    live_lock_fd = os.open(live_lock_path, os.O_RDWR)
    fcntl.flock(live_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    # Retired owner dir containing nested files + a symlink to an external directory.
    retired_id = "b" * 32
    retired_dir = work / retired_id
    retired_dir.mkdir(mode=0o700)
    (retired_dir / "nested").mkdir(mode=0o700)
    (retired_dir / "nested" / "junk.cadu").write_bytes(b"raw")
    (retired_dir / ".owner.lock").touch(mode=0o600)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    victim = outside / "precious.txt"
    victim.write_text("must survive", encoding="utf-8")
    (retired_dir / "link-out").symlink_to(outside, target_is_directory=True)

    # Interrupted creation (.new-<32hex>) with no active shared create lock is swept.
    pending_dir = work / (".new-" + "c" * 32)
    pending_dir.mkdir(mode=0o700)

    try:
        decoder.sweep_work()
        assert live_dir.is_dir()
        assert unknown.is_dir() and (unknown / "note.txt").is_file()
        assert tle.is_file() and tle.read_text(encoding="utf-8") == "METEOR-M2 3\n"
        assert not retired_dir.exists()
        assert not pending_dir.exists()
        assert victim.is_file() and victim.read_text(encoding="utf-8") == "must survive"
    finally:
        os.close(live_lock_fd)


@pytest.mark.parametrize(
    "fail_at",
    ["success", "png", "manifest", "pass_dir", "gallery_dir", "gallery_index", "gallery_commit"],
)
def test_meteor_managed_publication_syncs_before_reception(
        tmp_path, monkeypatch, fail_at):
    for name in ("SIGINT101_METEOR_MODE", "SIGINT101_METEOR_HZ", "SIGINT101_RTLSDR_SERIAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(meteor, "RELEASE_GRACE_S", 0.18)
    monkeypatch.setattr(meteor, "KILL_GRACE_S", 0.18)

    order: list[str] = []
    made = []

    def spawn(argv, **kwargs):
        from tests.test_meteor import MeteorChild
        proc = MeteorChild(argv)
        made.append(proc)
        return proc

    real_sync_media = storage.sync_media_asset
    real_fsync_file = storage.fsync_file
    real_fsync_dir = storage.fsync_dir
    fail_active = [False]
    meteor_dir_syncs = [0]
    gallery_sync_steps = ("gallery_dir", "gallery_commit", "gallery_commit_fence")

    def spy_sync_media(path):
        order.append("png")
        if fail_active[0] and fail_at == "png":
            raise OSError("injected png sync failure")
        return real_sync_media(path)

    def spy_fsync_file(path):
        name = Path(path).name
        if name == "manifest.json":
            order.append("manifest")
            if fail_active[0] and fail_at == "manifest":
                raise OSError("injected manifest sync failure")
        elif name.startswith(".gallery-"):
            order.append("gallery_index")
            if fail_active[0] and fail_at == "gallery_index":
                raise OSError("injected gallery_index sync failure")
        elif name.startswith(".commit-"):
            order.append("gallery_commit_record")
        return real_fsync_file(path)

    def spy_fsync_dir(path):
        p = Path(path)
        if p.name.startswith(".publish-"):
            order.append("pass_dir")
            if fail_active[0] and fail_at == "pass_dir":
                raise OSError("injected pass_dir sync failure")
        elif p.name == "meteor":
            meteor_dir_syncs[0] += 1
            step = gallery_sync_steps[meteor_dir_syncs[0] - 1]
            order.append(step)
            if fail_active[0] and fail_at == step:
                raise OSError(f"injected {step} sync failure")
        return real_fsync_dir(path)

    monkeypatch.setattr(meteor, "sync_media_asset", spy_sync_media)
    monkeypatch.setattr(meteor, "fsync_file", spy_fsync_file)
    monkeypatch.setattr(meteor, "fsync_dir", spy_fsync_dir)

    from sigint101.migrations import migrate_store_copy

    gen_dir = tmp_path / "gen"
    gen_dir.mkdir()
    migrate_store_copy(tmp_path / "m_p.db", gen_dir / "progress.db",
                       store="progress", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_p")
    migrate_store_copy(tmp_path / "m_e.db", gen_dir / "exam.db",
                       store="exam", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_e")

    utc = [1_732_800_000.0]
    decoder = meteor.MeteorDecoder(
        data_dir=gen_dir,
        which=lambda name: name,
        radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")],
        spawn=spawn,
        clock=lambda: utc[0],
        sync_assets=True,
    )
    try:
        with meteor_client(gen_dir, decoder, managed=True) as client:
            # First pass succeeds so we have a pre-existing gallery to prove stays intact.
            assert client.post(LISTEN).status_code == 200
            _complete_pass(decoder, made[-1], pictures=("MSU-MR-first.png",))
            first_pass = client.get(STATUS).json()["passes"][0]

            # Record reception emission order on the second pass.
            orig_hook = decoder.on_reception

            def spy_hook(item, **kw):
                order.append("reception")
                return orig_hook(item, **kw)

            decoder.on_reception = spy_hook
            order.clear()
            meteor_dir_syncs[0] = 0
            fail_active[0] = True
            utc[0] += 100.0

            assert client.post(LISTEN).status_code == 200
            _complete_pass(decoder, made[-1], pictures=("MSU-MR-second.png",))

            passes = client.get(STATUS).json()["passes"]
            receptions = [
                e for e in read_events(gen_dir / "events.jsonl")
                if e["kind"] == "reception" and e["payload"].get("instrument") == "meteor"
            ]
            if fail_at == "success":
                assert order == [
                    "png", "manifest", "pass_dir",
                    "gallery_dir", "gallery_index", "gallery_commit",
                    "gallery_commit_record", "gallery_commit_fence", "reception",
                ]
                assert len(passes) == 2
                assert len(receptions) == 2
                assert client.get(f"/media/{passes[0]['images'][0]['path']}").status_code == 200
            else:
                assert "reception" not in order
                assert [p["id"] for p in passes] == [first_pass["id"]]
                assert len(receptions) == 1
                # No leftover .publish-* or .gallery-* temp entries after failure cleanup.
                leftovers = (
                    list((gen_dir / "meteor").glob(".publish-*"))
                    + list((gen_dir / "meteor").glob(".gallery-*"))
                    + list((gen_dir / "meteor").glob(".commit-*"))
                )
                assert leftovers == []
    finally:
        for p in made:
            p.close_pipe()
        decoder.shutdown()


def _valid_png(width: int = 2, height: int = 2, rgb: tuple[int, int, int] = (12, 180, 90)) -> bytes:
    """Build a minimal spec-valid RGB PNG with valid CRCs and zlib scanlines."""
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = (b"\x00" + bytes(rgb) * width) * height
    idat = zlib.compress(raw)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def _new_managed_gen(tmp_path):
    from sigint101.migrations import migrate_store_copy

    gen_dir = tmp_path / "gen"
    gen_dir.mkdir(exist_ok=True)
    migrate_store_copy(tmp_path / "m_p.db", gen_dir / "progress.db",
                       store="progress", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_p")
    migrate_store_copy(tmp_path / "m_e.db", gen_dir / "exam.db",
                       store="exam", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_e")
    return gen_dir


def test_f1_truncated_png_rejected_at_publish_startup_and_serve_while_valid_png_passes(
    rig, tmp_path
):
    """F1: only a complete decodable PNG publishes, loads on startup, or serves over /media."""
    decoder, made, settings = rig
    settings["utc"] = 1_732_810_000.0
    valid_bytes = _valid_png()
    truncated_bytes = b"\x89PNG\r\n\x1a\n\x00"  # 9-byte truncated file with valid signature

    with meteor_client(tmp_path, decoder) as client:
        # 1. Truncated 9-byte file under expected product name must NOT publish, log, or stamp.
        assert client.post(LISTEN).status_code == 200
        proc = made[-1][0]
        proc.feed(progress_line())
        proc.feed(MARKER)
        assert decoder._run.physical.wait(2)
        out = Path(proc.args[3])
        out.mkdir(parents=True, exist_ok=True)
        (out / "MSU-MR-1.png").write_bytes(truncated_bytes)
        proc.die(0)
        proc.close_pipe()
        assert decoder._run.complete.wait(3)

        st = client.get(STATUS).json()
        assert st["state"] == "no_picture"
        assert st["passes"] == []
        receptions = [
            e for e in read_events(tmp_path / "events.jsonl")
            if e["kind"] == "reception" and e["payload"].get("instrument") == "meteor"
        ]
        assert receptions == []
        stamps = client.get("/api/stamps").json()["stamps"]
        m_stamp = next(s for s in stamps if s["instrument"] == "meteor")
        assert m_stamp["count"] == 0

        # 2. Small valid PNG publishes, serves, and earns one stamp.
        settings["utc"] += 100.0
        assert client.post(LISTEN).status_code == 200
        proc2 = made[-1][0]
        proc2.feed(progress_line())
        proc2.feed(MARKER)
        assert decoder._run.physical.wait(2)
        out2 = Path(proc2.args[3])
        out2.mkdir(parents=True, exist_ok=True)
        (out2 / "MSU-MR-valid.png").write_bytes(valid_bytes)
        proc2.die(0)
        proc2.close_pipe()
        assert decoder._run.complete.wait(3)

        st2 = client.get(STATUS).json()
        assert st2["state"] == "done"
        assert len(st2["passes"]) == 1
        img_path = st2["passes"][0]["images"][0]["path"]
        ok_resp = client.get(f"/media/{img_path}")
        assert ok_resp.status_code == 200 and ok_resp.content == valid_bytes
        stamps2 = client.get("/api/stamps").json()["stamps"]
        assert next(s for s in stamps2 if s["instrument"] == "meteor")["count"] == 1

        # 3. Corrupting saved PNG on disk -> refused at serve time AND skipped at startup!
        saved_png = tmp_path / img_path
        saved_png.write_bytes(valid_bytes[:20])
        assert client.get(f"/media/{img_path}").status_code == 404

    reopened = meteor.MeteorDecoder(
        data_dir=tmp_path,
        which=lambda name: name,
        radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")],
    )
    try:
        assert reopened.passes() == []
        assert reopened.read_media(img_path) is None
    finally:
        reopened.shutdown()


def test_f2_first_managed_publication_syncs_gallery_parent_with_existing_event_log(
    tmp_path, monkeypatch
):
    """F2: first managed publication syncs data_dir before reception even if events.jsonl exists."""
    from sigint101.events import log_event
    from tests.test_meteor import MeteorChild

    gen_dir = _new_managed_gen(tmp_path)
    # Pre-create a durable events.jsonl so appending a reception does NOT sync gen_dir itself.
    log_event(gen_dir / "events.jsonl", "boot", {"ok": True}, sync=True)

    synced_dirs: list[Path] = []
    fail_parent = [True]
    real_fsync_dir = storage.fsync_dir

    def spy_fsync_dir(path):
        p = Path(path).resolve()
        synced_dirs.append(p)
        if fail_parent[0] and p == gen_dir.resolve():
            raise OSError("injected gallery parent sync failure")
        return real_fsync_dir(path)

    monkeypatch.setattr(meteor, "fsync_dir", spy_fsync_dir)

    made = []

    def spawn(argv, **kwargs):
        proc = MeteorChild(argv)
        made.append(proc)
        return proc

    utc = [1_732_820_000.0]
    decoder = meteor.MeteorDecoder(
        data_dir=gen_dir,
        which=lambda name: name,
        radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")],
        spawn=spawn,
        clock=lambda: utc[0],
        sync_assets=True,
    )
    try:
        with meteor_client(gen_dir, decoder, managed=True) as client:
            # First attempt: parent dir sync fails -> no pass published, no reception logged.
            assert client.post(LISTEN).status_code == 200
            proc1 = made[-1]
            proc1.feed(progress_line())
            proc1.feed(MARKER)
            assert decoder._run.physical.wait(2)
            out1 = Path(proc1.args[3])
            out1.mkdir(parents=True, exist_ok=True)
            (out1 / "MSU-MR-1.png").write_bytes(_valid_png())
            proc1.die(0)
            proc1.close_pipe()
            assert decoder._run.complete.wait(3)

            assert gen_dir.resolve() in synced_dirs
            assert client.get(STATUS).json()["passes"] == []
            receptions = [
                e for e in read_events(gen_dir / "events.jsonl")
                if e["kind"] == "reception" and e["payload"].get("instrument") == "meteor"
            ]
            assert receptions == []

            # Second attempt with parent sync succeeding -> syncs gen_dir and publishes cleanly.
            synced_dirs.clear()
            fail_parent[0] = False
            utc[0] += 100.0
            assert client.post(LISTEN).status_code == 200
            proc2 = made[-1]
            proc2.feed(progress_line())
            proc2.feed(MARKER)
            assert decoder._run.physical.wait(2)
            out2 = Path(proc2.args[3])
            out2.mkdir(parents=True, exist_ok=True)
            (out2 / "MSU-MR-2.png").write_bytes(_valid_png())
            proc2.die(0)
            proc2.close_pipe()
            assert decoder._run.complete.wait(3)

            assert gen_dir.resolve() in synced_dirs
            assert len(client.get(STATUS).json()["passes"]) == 1
    finally:
        for p in made:
            p.close_pipe()
        decoder.shutdown()


@pytest.mark.parametrize("fail_on_sync", [1, 2])
def test_f3_failed_final_sync_and_failed_rollback_keeps_media_hidden_after_reopen(
    tmp_path, monkeypatch, fail_on_sync
):
    """F3: if final gallery sync and rollback both fail, a fresh decoder still hides the pass."""
    from tests.test_meteor import MeteorChild

    gen_dir = _new_managed_gen(tmp_path)
    real_fsync_dir = storage.fsync_dir
    sync_hits = [0]

    def fail_gallery_dir_sync(path):
        if Path(path).name == "meteor":
            sync_hits[0] += 1
            if sync_hits[0] == fail_on_sync:
                raise OSError("injected final gallery sync failure")
        return real_fsync_dir(path)

    def fail_rollback(_parent_fd, _name):
        raise OSError("injected rollback cleanup failure")

    monkeypatch.setattr(meteor, "fsync_dir", fail_gallery_dir_sync)
    monkeypatch.setattr(meteor, "_remove_dir_descriptor", fail_rollback)

    made = []

    def spawn(argv, **kwargs):
        proc = MeteorChild(argv)
        made.append(proc)
        return proc

    decoder = meteor.MeteorDecoder(
        data_dir=gen_dir,
        which=lambda name: name,
        radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")],
        spawn=spawn,
        clock=lambda: 1_732_830_000.0,
        sync_assets=True,
    )
    try:
        with meteor_client(gen_dir, decoder, managed=True) as client:
            assert client.post(LISTEN).status_code == 200
            proc = made[-1]
            proc.feed(progress_line())
            proc.feed(MARKER)
            assert decoder._run.physical.wait(2)
            out = Path(proc.args[3])
            out.mkdir(parents=True, exist_ok=True)
            (out / "MSU-MR-ghost.png").write_bytes(_valid_png())
            proc.die(0)
            proc.close_pipe()
            assert decoder._run.complete.wait(3)

            assert client.get(STATUS).json()["passes"] == []
            receptions = [
                e for e in read_events(gen_dir / "events.jsonl")
                if e["kind"] == "reception" and e["payload"].get("instrument") == "meteor"
            ]
            assert receptions == []
    finally:
        for p in made:
            p.close_pipe()
        decoder.shutdown()

    # Reopen a brand-new decoder on the same data_dir: unconfirmed media must stay hidden!
    reopened = meteor.MeteorDecoder(
        data_dir=gen_dir,
        which=lambda name: name,
        radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")],
        sync_assets=True,
    )
    try:
        assert reopened.passes() == []
        # Even if a caller guesses the leftover directory's path, read_media refuses it.
        for child in (gen_dir / "meteor").iterdir():
            if child.is_dir() and meteor._PASS_ID.fullmatch(child.name):
                assert reopened.read_media(f"meteor/{child.name}/MSU-MR-ghost.png") is None
    finally:
        reopened.shutdown()


def test_failed_index_sync_with_both_rollbacks_refused_preserves_last_commit(
    tmp_path, monkeypatch,
):
    from tests.test_meteor import MeteorChild

    gen_dir = _new_managed_gen(tmp_path)
    gallery = gen_dir / "meteor"
    made = []

    def spawn(argv, **kwargs):
        proc = MeteorChild(argv)
        made.append(proc)
        return proc

    decoder = meteor.MeteorDecoder(
        data_dir=gen_dir, which=lambda name: name, radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")], spawn=spawn, sync_assets=True,
    )
    with meteor_client(gen_dir, decoder, managed=True) as client:
        assert client.post(LISTEN).status_code == 200
        _complete_pass(decoder, made[-1], pictures=("MSU-MR-good.png",))
        good, = decoder.passes()
        good_path = good["images"][0]["path"]
        assert client.post(LISTEN).status_code == 200
        bad_id = decoder._run.args[0].id
        bad_path = f"meteor/{bad_id}/MSU-MR-unconfirmed.png"
        failed_sync = threading.Event()
        real_rename = os.rename
        real_remove = meteor._remove_dir_descriptor

        def fail_final_sync(path):
            index = gallery / "gallery.json"
            if (Path(path) == gallery and index.exists()
                    and bad_id in json.loads(index.read_text())["passes"]):
                failed_sync.set()
                raise OSError("injected final index sync failure")
            storage.fsync_dir(path)

        def fail_index_rollback(src, dst, **kwargs):
            if dst == "gallery.json" and failed_sync.is_set():
                raise OSError("injected index rollback failure")
            return real_rename(src, dst, **kwargs)

        def fail_pass_rollback(parent_fd, name):
            if name == bad_id:
                raise OSError("injected pass cleanup failure")
            real_remove(parent_fd, name)

        monkeypatch.setattr(meteor, "fsync_dir", fail_final_sync)
        monkeypatch.setattr(os, "rename", fail_index_rollback)
        monkeypatch.setattr(meteor, "_remove_dir_descriptor", fail_pass_rollback)
        _complete_pass(decoder, made[-1], pictures=("MSU-MR-unconfirmed.png",))
        assert failed_sync.is_set()
        assert (gen_dir / bad_path).exists()
        assert client.get(STATUS).json()["state"] == "no_picture"
        assert decoder.passes() == [good]
        assert client.get(f"/media/{bad_path}").status_code == 404

    reopened = meteor.MeteorDecoder(data_dir=gen_dir, sync_assets=True)
    with meteor_client(gen_dir, reopened, managed=True) as client:
        assert reopened.passes() == [good]
        assert client.get(f"/media/{good_path}").status_code == 200
        assert client.get(f"/media/{bad_path}").status_code == 404
        receptions = [e for e in read_events(gen_dir / "events.jsonl")
                      if e["kind"] == "reception" and e["payload"]["instrument"] == "meteor"]
        assert [e["payload"]["item"]["path"] for e in receptions] == [good_path]
        stamps = client.get("/api/stamps").json()["stamps"]
        assert next(s for s in stamps if s["instrument"] == "meteor")["count"] == 1


def test_overlapping_pass_keeps_media_servable_through_its_receipt(rig, tmp_path, monkeypatch):
    decoder, made, settings = rig
    monkeypatch.setattr(meteor, "MAX_PUBLISHED_PASSES", 1)
    first_receipt, finish_receipt, second_publication = (threading.Event() for _ in range(3))
    served = {}

    def finish_child(run, proc, name):
        proc.feed(progress_line())
        proc.feed(MARKER)
        assert run.physical.wait(2)
        proc.picture(name)
        proc.die(0)
        proc.close_pipe()

    with meteor_client(tmp_path, decoder) as client:
        hook = decoder.on_reception
        publish = decoder._publish_pass

        def publishing(run, capture):
            if run is second_run:
                second_publication.set()
            return publish(run, capture)

        def receive(item, *, command=None, freq_hz=None, radio=None):
            path = item["path"]
            if path == first_path:
                first_receipt.set()
                assert finish_receipt.wait(8)
            hook(item, command=command, freq_hz=freq_hz, radio=radio)
            served[path] = client.get(f"/media/{path}").status_code

        settings["utc"] = 1_732_840_100.0
        assert client.post(LISTEN).status_code == 200
        first_run = decoder._run
        first_path = f"meteor/{first_run.args[0].id}/MSU-MR-first.png"
        second_run = None
        decoder.on_reception = receive
        monkeypatch.setattr(decoder, "_publish_pass", publishing)
        try:
            finish_child(first_run, made[-1][0], "MSU-MR-first.png")
            assert first_receipt.wait(3)
            settings["utc"] = 1_732_800_000.0
            assert client.post(LISTEN).status_code == 200
            second_run = decoder._run
            assert second_run is not first_run
            finish_child(second_run, made[-1][0], "MSU-MR-second.png")
            assert second_publication.wait(3)
            # Let an unprotected second publication run before releasing the first receipt.
            second_run.complete.wait(1)
            first_status = client.get(f"/media/{first_path}").status_code
            assert client.get("/api/spectrum").status_code == 200
            assert len(client.get(STATUS).json()["passes"]) == 1
        finally:
            finish_receipt.set()
        assert first_run.complete.wait(3)
        assert second_run.complete.wait(3)
        second_path = f"meteor/{second_run.args[0].id}/MSU-MR-second.png"
        receipts = [e["payload"]["item"]["path"]
                    for e in read_events(tmp_path / "events.jsonl")
                    if e["kind"] == "reception" and e["payload"]["instrument"] == "meteor"]
        assert set(receipts) == {first_path, second_path}
        stamps = client.get("/api/stamps").json()["stamps"]
        assert next(s for s in stamps if s["instrument"] == "meteor")["count"] == 2
        assert served == {first_path: 200, second_path: 200}
        assert first_status == 200
        assert client.get(f"/media/{second_path}").status_code == 200
        assert client.get(f"/media/{first_path}").status_code == 404


def test_f4_retention_survives_backward_utc_step_and_reconciles_interrupted_eviction(
    rig, tmp_path, monkeypatch
):
    """F4: retention orders independently of UTC and reconciles on-disk overflow on next publish."""
    decoder, made, settings = rig
    monkeypatch.setattr(meteor, "MAX_PUBLISHED_PASSES", 2)

    def _run_valid_pass(client, fname: str) -> dict:
        assert client.post(LISTEN).status_code == 200
        proc = made[-1][0]
        proc.feed(progress_line())
        proc.feed(MARKER)
        assert decoder._run.physical.wait(2)
        out = Path(proc.args[3])
        out.mkdir(parents=True, exist_ok=True)
        (out / fname).write_bytes(_valid_png())
        proc.die(0)
        proc.close_pipe()
        assert decoder._run.complete.wait(3)
        return client.get(STATUS).json()

    with meteor_client(tmp_path, decoder) as client:
        # 1. Publish pass 1 and pass 2, then step UTC BACKWARDS before pass 3!
        settings["utc"] = 1_732_840_100.0
        st1 = _run_valid_pass(client, "MSU-MR-p1.png")
        p1_id = st1["passes"][0]["id"]

        settings["utc"] = 1_732_840_200.0
        st2 = _run_valid_pass(client, "MSU-MR-p2.png")
        p2_id = st2["passes"][0]["id"]

        # Step UTC backward by 10 hours: pass 3 MUST still be admitted and serve 200 (never 404)!
        settings["utc"] = 1_732_800_000.0
        st3 = _run_valid_pass(client, "MSU-MR-p3.png")
        assert st3["state"] == "done"
        p3_ids = [p["id"] for p in st3["passes"]]
        assert len(p3_ids) == 2
        assert p1_id not in p3_ids and p2_id in p3_ids
        p3_row = st3["passes"][0]
        assert p3_row["images"][0]["path"].endswith("MSU-MR-p3.png")
        assert client.get(f"/media/{p3_row['images'][0]['path']}").status_code == 200

        # 2. Simulate an interrupted eviction on pass 4 leaving 3 complete pass dirs on disk!
        real_remove = meteor._remove_dir_descriptor

        def fail_remove(*_args, **_kwargs):
            raise OSError("io")

        monkeypatch.setattr(meteor, "_remove_dir_descriptor", fail_remove)
        settings["utc"] = 1_732_840_300.0
        _run_valid_pass(client, "MSU-MR-p4.png")
        monkeypatch.setattr(meteor, "_remove_dir_descriptor", real_remove)

        on_disk_before = [
            d.name for d in (tmp_path / "meteor").iterdir()
            if d.is_dir() and meteor._PASS_ID.fullmatch(d.name)
        ]
        assert len(on_disk_before) == 3

    # Reopen decoder and publish pass 5: descriptor-safe reconciliation must prune overflow to 2!
    from tests.test_meteor import MeteorChild

    made2 = []
    decoder2 = meteor.MeteorDecoder(
        data_dir=tmp_path,
        which=lambda name: name,
        radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")],
        spawn=lambda argv, **kw: (made2.append(MeteorChild(argv)), made2[-1])[1],
        clock=lambda: 1_732_840_400.0,
    )
    try:
        with meteor_client(tmp_path, decoder2) as client2:
            assert len(client2.get(STATUS).json()["passes"]) == 2
            assert client2.post(LISTEN).status_code == 200
            proc5 = made2[-1]
            proc5.feed(progress_line())
            proc5.feed(MARKER)
            assert decoder2._run.physical.wait(2)
            out5 = Path(proc5.args[3])
            out5.mkdir(parents=True, exist_ok=True)
            (out5 / "MSU-MR-p5.png").write_bytes(_valid_png())
            proc5.die(0)
            proc5.close_pipe()
            assert decoder2._run.complete.wait(3)

            passes_final = client2.get(STATUS).json()["passes"]
            assert len(passes_final) == 2
            on_disk_after = [
                d.name for d in (tmp_path / "meteor").iterdir()
                if d.is_dir() and meteor._PASS_ID.fullmatch(d.name)
            ]
            assert sorted(on_disk_after) == sorted(p["id"] for p in passes_final)
    finally:
        for p in made2:
            p.close_pipe()
        decoder2.shutdown()


def test_f5_bad_manifest_field_types_and_unrenderable_timestamps_are_skipped(rig, tmp_path):
    """F5: damaged manifest (list mode, huge float, or 351-digit int timestamp) is skipped."""
    decoder, made, settings = rig
    settings["utc"] = 1_732_850_000.0

    with meteor_client(tmp_path, decoder) as client:
        ids = []
        for idx in range(4):
            settings["utc"] += 100.0
            assert client.post(LISTEN).status_code == 200
            proc = made[-1][0]
            proc.feed(progress_line())
            proc.feed(MARKER)
            assert decoder._run.physical.wait(2)
            out = Path(proc.args[3])
            out.mkdir(parents=True, exist_ok=True)
            (out / f"MSU-MR-{idx}.png").write_bytes(_valid_png())
            proc.die(0)
            proc.close_pipe()
            assert decoder._run.complete.wait(3)
            ids.append(client.get(STATUS).json()["passes"][0]["id"])

    good_id, bad_mode_id, bad_ts_id, huge_int_ts_id = ids
    # Corrupt manifest 1: mode is a JSON array (unhashable!)
    m1_path = tmp_path / "meteor" / bad_mode_id / "manifest.json"
    m1 = json.loads(m1_path.read_text(encoding="utf-8"))
    m1["mode"] = ["72k"]
    m1_path.write_text(json.dumps(m1), encoding="utf-8")

    # Corrupt manifest 2: started_at is an unrenderable huge float timestamp
    m2_path = tmp_path / "meteor" / bad_ts_id / "manifest.json"
    m2 = json.loads(m2_path.read_text(encoding="utf-8"))
    m2["started_at"] = 1e20
    m2_path.write_text(json.dumps(m2), encoding="utf-8")

    # Corrupt manifest 3: started_at is a 351-digit JSON integer (overflows float()!)
    m3_path = tmp_path / "meteor" / huge_int_ts_id / "manifest.json"
    m3 = json.loads(m3_path.read_text(encoding="utf-8"))
    m3["started_at"] = 10**350
    m3_path.write_text(json.dumps(m3), encoding="utf-8")

    reopened = meteor.MeteorDecoder(
        data_dir=tmp_path,
        which=lambda name: name,
        radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")],
    )
    try:
        assert [p["id"] for p in reopened.passes()] == [good_id]
    finally:
        reopened.shutdown()


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _indexed_png(width: int, height: int, bit_depth: int, plte: bytes,
                 raw_scanlines: bytes, *, interlace: int = 0) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, 3, 0, 0, interlace)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"PLTE", plte)
        + _png_chunk(b"IDAT", zlib.compress(raw_scanlines))
        + _png_chunk(b"IEND", b"")
    )


def test_f1a_indexed_png_out_of_palette_rejected_at_publish_and_filters_validated(rig, tmp_path):
    """F1a: reverse PNG filters across all passes and reject out-of-palette indexed samples."""
    decoder, made, settings = rig
    settings["utc"] = 1_732_860_000.0

    # CRC-correct 2x1 8-bit indexed PNG with 1-entry palette; Sub filter reconstructs [0, 1].
    bad_sub_indexed = _indexed_png(2, 1, 8, b"\x00\x00\x00", b"\x01\x00\x01")
    # Same scanlines with a 2-entry palette: reconstructed indices [0, 1] are both in range.
    good_sub_indexed = _indexed_png(2, 1, 8, b"\x00\x00\x00\xff\xff\xff", b"\x01\x00\x01")

    with meteor_client(tmp_path, decoder) as client:
        # 1. Out-of-palette indexed PNG must be refused at publication with zero receptions/stamps.
        assert client.post(LISTEN).status_code == 200
        proc = made[-1][0]
        proc.feed(progress_line())
        proc.feed(MARKER)
        assert decoder._run.physical.wait(2)
        out = Path(proc.args[3])
        out.mkdir(parents=True, exist_ok=True)
        (out / "MSU-MR-oob.png").write_bytes(bad_sub_indexed)
        proc.die(0)
        proc.close_pipe()
        assert decoder._run.complete.wait(3)

        st = client.get(STATUS).json()
        assert st["state"] == "no_picture"
        assert st["passes"] == []
        receptions = [
            e for e in read_events(tmp_path / "events.jsonl")
            if e["kind"] == "reception" and e["payload"].get("instrument") == "meteor"
        ]
        assert receptions == []
        stamps = client.get("/api/stamps").json()["stamps"]
        assert next(s for s in stamps if s["instrument"] == "meteor")["count"] == 0

        # 2. Valid Sub-filtered indexed control publishes, serves 200, and earns 1 stamp.
        settings["utc"] += 100.0
        assert client.post(LISTEN).status_code == 200
        proc2 = made[-1][0]
        proc2.feed(progress_line())
        proc2.feed(MARKER)
        assert decoder._run.physical.wait(2)
        out2 = Path(proc2.args[3])
        out2.mkdir(parents=True, exist_ok=True)
        (out2 / "MSU-MR-ok-idx.png").write_bytes(good_sub_indexed)
        proc2.die(0)
        proc2.close_pipe()
        assert decoder._run.complete.wait(3)

        st2 = client.get(STATUS).json()
        assert st2["state"] == "done"
        assert len(st2["passes"]) == 1
        img_path = st2["passes"][0]["images"][0]["path"]
        resp = client.get(f"/media/{img_path}")
        assert resp.status_code == 200 and resp.content == good_sub_indexed
        stamps2 = client.get("/api/stamps").json()["stamps"]
        assert next(s for s in stamps2 if s["instrument"] == "meteor")["count"] == 1

    # Unit checks across Up, Average, Paeth, Adam7, and sub-byte bit depths (1/2/4-bit).
    plte2 = b"\x00\x00\x00\xff\xff\xff"
    plte3 = plte2 + b"\x10\x20\x30"
    # Up filter (2): row 0 = [1], row 1 raw=1 -> recon = 1 + 1 = 2 (out of bounds for plte2).
    assert not meteor._is_valid_png(_indexed_png(1, 2, 8, plte2, b"\x00\x01\x02\x01"))
    assert meteor._is_valid_png(_indexed_png(1, 2, 8, plte3, b"\x00\x01\x02\x01"))
    # Average filter (3): row 0 = [0, 2], row 1 raw=[2, 1] -> [2, 1 + ((2+2)//2) = 3] (oob plte3).
    assert not meteor._is_valid_png(_indexed_png(2, 2, 8, plte3, b"\x00\x00\x02\x03\x02\x01"))
    assert meteor._is_valid_png(_indexed_png(2, 2, 8, plte3 + b"\x40\x50\x60",
                                             b"\x00\x00\x02\x03\x02\x01"))
    # Paeth filter (4): row 0=[1, 1], row 1 raw=[1, 1] -> recon [2, 3] (oob plte3, ok plte4).
    plte4 = plte3 + b"\x40\x50\x60"
    assert not meteor._is_valid_png(_indexed_png(2, 2, 8, plte3, b"\x00\x01\x01\x04\x01\x01"))
    assert meteor._is_valid_png(_indexed_png(2, 2, 8, plte4, b"\x00\x01\x01\x04\x01\x01"))
    # Adam7 2x2 indexed: passes (1x1), (1x1), (2x1). Pass 3 uses Sub [0, 2] -> oob plte2, ok plte3.
    adam7_raw = b"\x00\x00" + b"\x00\x01" + b"\x01\x00\x02"
    assert not meteor._is_valid_png(_indexed_png(2, 2, 8, plte2, adam7_raw, interlace=1))
    assert meteor._is_valid_png(_indexed_png(2, 2, 8, plte3, adam7_raw, interlace=1))
    # Sub-byte bit depths (4-bit, 2-bit, 1-bit): trailing pad bits on odd widths must be ignored,
    # while real pixel samples past the palette must be rejected.
    # 4-bit width=1: high nibble=0 (valid), low nibble=0xF (pad, ignored) -> valid for 1-entry plte.
    assert meteor._is_valid_png(_indexed_png(1, 1, 4, b"\x00\x00\x00", b"\x00\x0f"))
    # 4-bit width=2: high nibble=0, low nibble=2 -> oob for 2-entry plte, ok for 3-entry plte.
    assert not meteor._is_valid_png(_indexed_png(2, 1, 4, plte2, b"\x00\x02"))
    assert meteor._is_valid_png(_indexed_png(2, 1, 4, plte3, b"\x00\x02"))
    # 2-bit width=1: top 2 bits=0, remaining 6 pad bits=0b111111 -> valid for 1-entry plte.
    assert meteor._is_valid_png(_indexed_png(1, 1, 2, b"\x00\x00\x00", b"\x00\x3f"))
    # 2-bit width=2: pixel 0=0, pixel 1=2 (0b00_10_0000 = 0x20) -> oob for 2-entry plte.
    assert not meteor._is_valid_png(_indexed_png(2, 1, 2, plte2, b"\x00\x20"))
    # 1-bit width=1: top bit=0, remaining 7 pad bits=0x7f -> valid for 1-entry plte.
    assert meteor._is_valid_png(_indexed_png(1, 1, 1, b"\x00\x00\x00", b"\x00\x7f"))
    # 1-bit width=2: pixel 0=0, pixel 1=1 (0b01000000 = 0x40) -> oob for 1-entry plte.
    assert not meteor._is_valid_png(_indexed_png(2, 1, 1, b"\x00\x00\x00", b"\x00\x40"))


def test_f1b_swapped_valid_png_refused_on_serve_and_after_restart_without_redecode(
    rig, tmp_path, monkeypatch
):
    """F1b: manifest binds exact size + SHA-256; swapped valid PNG refused without PNG re-decode."""
    decoder, made, settings = rig
    settings["utc"] = 1_732_870_000.0
    control_bytes = _valid_png(2, 2, (10, 120, 200))
    target_bytes = _valid_png(2, 2, (40, 180, 90))
    replacement_bytes = _valid_png(3, 3, (220, 30, 30))
    assert replacement_bytes != target_bytes

    with meteor_client(tmp_path, decoder) as client:
        # Publish control pass first, then swap-target pass.
        pairs = (("MSU-MR-ctrl.png", control_bytes), ("MSU-MR-swap.png", target_bytes))
        for fname, content in pairs:
            settings["utc"] += 100.0
            assert client.post(LISTEN).status_code == 200
            proc = made[-1][0]
            proc.feed(progress_line())
            proc.feed(MARKER)
            assert decoder._run.physical.wait(2)
            out = Path(proc.args[3])
            out.mkdir(parents=True, exist_ok=True)
            (out / fname).write_bytes(content)
            proc.die(0)
            proc.close_pipe()
            assert decoder._run.complete.wait(3)

        passes = client.get(STATUS).json()["passes"]
        assert len(passes) == 2
        swap_row, ctrl_row = passes[0], passes[1]
        swap_path = swap_row["images"][0]["path"]
        ctrl_path = ctrl_row["images"][0]["path"]

        # Manifest records exact size + SHA-256; public API keeps exact path/product shape.
        ctrl_manifest = json.loads(
            (tmp_path / "meteor" / ctrl_row["id"] / "manifest.json").read_text(encoding="utf-8")
        )
        assert ctrl_manifest["images"][0]["size"] == len(control_bytes)
        assert ctrl_manifest["images"][0]["sha256"] == hashlib.sha256(control_bytes).hexdigest()
        assert ctrl_row["images"] == [{"path": ctrl_path, "product": "ctrl"}]

        # Swap target PNG on disk with a DIFFERENT valid PNG, leaving manifest & gallery untouched.
        (tmp_path / swap_path).write_bytes(replacement_bytes)

        # Serve must NOT re-decode PNGs: size + SHA-256 alone admits control and refuses swap.
        def forbid_redecode(_data):
            raise AssertionError("startup/serve must check size + SHA-256 without re-decoding PNG")

        monkeypatch.setattr(meteor, "_is_valid_png", forbid_redecode)
        assert client.get(f"/media/{swap_path}").status_code == 404
        ctrl_resp = client.get(f"/media/{ctrl_path}")
        assert ctrl_resp.status_code == 200
        assert ctrl_resp.content == control_bytes

    # Startup must also check only size + SHA-256 (no PNG re-decode), skip swapped, keep control.
    reopened = meteor.MeteorDecoder(
        data_dir=tmp_path,
        which=lambda name: name,
        radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")],
    )
    try:
        assert [p["id"] for p in reopened.passes()] == [ctrl_row["id"]]
        assert reopened.read_media(swap_path) is None
        assert reopened.read_media(ctrl_path) == control_bytes
    finally:
        reopened.shutdown()


def test_png_validator_rejects_corrupt_streams_and_accepts_interlaced_and_indexed():
    """Unit checks for pure-stdlib PNG validation edge cases."""
    def chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    good = _valid_png(3, 2)
    assert meteor._is_valid_png(good)
    assert not meteor._is_valid_png(b"")
    assert not meteor._is_valid_png(b"\x89PNG\r\n\x1a\n\x00")
    # Corrupted CRC
    bad_crc = bytearray(good)
    bad_crc[-1] ^= 0xFF
    assert not meteor._is_valid_png(bytes(bad_crc))
    # Trailing garbage after IEND
    assert not meteor._is_valid_png(good + b"\x00")
    # Invalid scanline filter byte (9 > 4)
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    bad_filter_raw = (b"\x09" + b"\x00" * 6) * 2
    bad_filter = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bad_filter_raw))
        + chunk(b"IEND", b"")
    )
    assert not meteor._is_valid_png(bad_filter)
    # Valid indexed PNG (color_type=3 with PLTE) vs missing PLTE
    ihdr_idx = struct.pack(">IIBBBBB", 2, 2, 8, 3, 0, 0, 0)
    idat_idx = zlib.compress((b"\x00\x00\x01") * 2)
    valid_idx = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr_idx)
        + chunk(b"PLTE", b"\x00\x00\x00\xff\xff\xff")
        + chunk(b"IDAT", idat_idx)
        + chunk(b"IEND", b"")
    )
    no_plte_idx = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr_idx)
        + chunk(b"IDAT", idat_idx)
        + chunk(b"IEND", b"")
    )
    assert meteor._is_valid_png(valid_idx)
    assert not meteor._is_valid_png(no_plte_idx)
    # Valid Adam7 interlaced 2x2 RGB PNG (passes: 1x1, 1x1, 2x1 -> 3 scanlines: 4, 4, 7 bytes)
    ihdr_adam7 = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 1)
    adam7_raw = (b"\x00\x01\x02\x03") + (b"\x00\x04\x05\x06") + (b"\x00\x01\x02\x03\x04\x05\x06")
    valid_adam7 = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr_adam7)
        + chunk(b"IDAT", zlib.compress(adam7_raw))
        + chunk(b"IEND", b"")
    )
    assert meteor._is_valid_png(valid_adam7)


def test_r5_manifest_size_boundary_keeps_restartable_bounded_subset_and_refuses_zero_fit(
    rig, tmp_path, monkeypatch
):
    """Round 5 (F1b regression): a pass with 250 valid long-named PNGs whose full manifest
    would exceed _MAX_MANIFEST_BYTES (64 KiB) commits only a deterministic bounded subset that
    fits within the reader's limit, logs/stamps only those kept products, writes no orphan PNGs,
    and reopens identically after a full console restart; if even 1 product cannot fit, nothing
    is committed, logged, or stamped."""
    decoder, made, settings = rig
    settings["utc"] = 1_732_880_000.0
    png_bytes = _valid_png(2, 2, (30, 140, 210))

    # 250 distinct valid long names matching _PRODUCT (96-char suffix -> ~81 KiB full manifest).
    all_names = [f"MSU-MR-prod-{i:03d}-{'x' * 80}.png" for i in range(250)]

    with meteor_client(tmp_path, decoder) as client:
        assert client.post(LISTEN).status_code == 200
        proc = made[-1][0]
        proc.feed(progress_line())
        proc.feed(MARKER)
        assert decoder._run.physical.wait(2)
        out = Path(proc.args[3])
        out.mkdir(parents=True, exist_ok=True)
        for name in all_names:
            (out / name).write_bytes(png_bytes)
        proc.die(0)
        proc.close_pipe()
        assert decoder._run.complete.wait(30)

        st = client.get(STATUS).json()
        assert st["state"] == "done"
        assert len(st["passes"]) == 1
        pass_row = st["passes"][0]
        pass_id = pass_row["id"]
        kept_images = pass_row["images"]
        kept_count = len(kept_images)
        assert 1 <= kept_count < len(all_names), (
            f"expected bounded subset strictly smaller than {len(all_names)}, got {kept_count}"
        )

        # Manifest on disk must fit within the startup reader's _MAX_MANIFEST_BYTES limit,
        # and adding one more product would cross that boundary.
        pass_dir = tmp_path / "meteor" / pass_id
        manifest_bytes = (pass_dir / "manifest.json").read_bytes()
        assert len(manifest_bytes) <= meteor._MAX_MANIFEST_BYTES
        manifest_obj = json.loads(manifest_bytes.decode("utf-8"))
        one_more = {**manifest_obj, "images": [*manifest_obj["images"], manifest_obj["images"][0]]}
        one_more_bytes = json.dumps(one_more, sort_keys=True).encode("utf-8")
        assert len(one_more_bytes) > meteor._MAX_MANIFEST_BYTES

        # Directory must contain only manifest.json and the exact kept PNGs (no orphan files).
        kept_fnames = {PurePosixPath(img["path"]).parts[2] for img in kept_images}
        disk_entries = {p.name for p in pass_dir.iterdir()}
        assert disk_entries == (kept_fnames | {"manifest.json"})

        # Logged receptions and stamps must match the exact kept subset (not 250).
        receptions = [
            e for e in read_events(tmp_path / "events.jsonl")
            if e["kind"] == "reception" and e["payload"].get("instrument") == "meteor"
        ]
        assert len(receptions) == kept_count
        assert [r["payload"]["item"]["path"] for r in receptions] == [
            img["path"] for img in kept_images
        ]
        stamps = client.get("/api/stamps").json()["stamps"]
        assert next(s for s in stamps if s["instrument"] == "meteor")["count"] == kept_count

        # Every kept image serves 200; dropped product serves 404.
        dropped_name = next(n for n in all_names if n not in kept_fnames)
        assert client.get(f"/media/{kept_images[0]['path']}").content == png_bytes
        assert client.get(f"/media/{kept_images[-1]['path']}").content == png_bytes
        assert client.get(f"/media/meteor/{pass_id}/{dropped_name}").status_code == 404

    # Restart console over the same data_dir without altering any file: pass and media survive!
    reopened = meteor.MeteorDecoder(
        data_dir=tmp_path,
        which=lambda name: name,
        radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")],
    )
    with meteor_client(tmp_path, reopened) as client2:
        st2 = client2.get(STATUS).json()
        assert len(st2["passes"]) == 1, "published pass vanished after console restart"
        assert st2["passes"][0]["id"] == pass_id
        assert st2["passes"][0]["images"] == kept_images
        for img in (kept_images[0], kept_images[-1]):
            resp = client2.get(f"/media/{img['path']}")
            assert resp.status_code == 200 and resp.content == png_bytes
        assert client2.get(f"/media/meteor/{pass_id}/{dropped_name}").status_code == 404
        stamps2 = client2.get("/api/stamps").json()["stamps"]
        assert next(s for s in stamps2 if s["instrument"] == "meteor")["count"] == kept_count

    # Edge case: when even a single product cannot fit within _MAX_MANIFEST_BYTES, commit is
    # refused cleanly with zero receptions, zero stamps, and no directory left behind.
    tiny_dir = tmp_path / "zero_fit"
    tiny_dir.mkdir()
    monkeypatch.setattr(meteor, "_MAX_MANIFEST_BYTES", 64)
    made2: list = []

    def _spawn2(argv, **_kw):
        from tests.test_meteor import MeteorChild
        proc = MeteorChild(argv)
        made2.append(proc)
        return proc

    dec_tiny = meteor.MeteorDecoder(
        data_dir=tiny_dir,
        which=lambda name: name,
        radio_present=lambda: True,
        list_devices=lambda: [RtlSdrDevice(4, "rx")],
        spawn=_spawn2,
        clock=lambda: 1_732_890_000.0,
    )
    with meteor_client(tiny_dir, dec_tiny) as client_tiny:
        assert client_tiny.post(LISTEN).status_code == 200
        p_tiny = made2[-1]
        p_tiny.feed(progress_line())
        p_tiny.feed(MARKER)
        assert dec_tiny._run.physical.wait(2)
        out_tiny = Path(p_tiny.args[3])
        out_tiny.mkdir(parents=True, exist_ok=True)
        (out_tiny / "MSU-MR-one.png").write_bytes(png_bytes)
        p_tiny.die(0)
        p_tiny.close_pipe()
        assert dec_tiny._run.complete.wait(3)

        st_tiny = client_tiny.get(STATUS).json()
        assert st_tiny["state"] == "no_picture"
        assert st_tiny["passes"] == []
        tiny_events = tiny_dir / "events.jsonl"
        assert not tiny_events.exists() or not [
            e for e in read_events(tiny_events)
            if e["kind"] == "reception" and e["payload"].get("instrument") == "meteor"
        ]
        tiny_stamps = client_tiny.get("/api/stamps").json()["stamps"]
        assert next(s for s in tiny_stamps if s["instrument"] == "meteor")["count"] == 0
