# ABOUTME: Tests Meteor gallery publication, manifest-restricted media, separate retention,
# ABOUTME: symlink-safe pruning, managed sync durability, pre-launch freq_hz, and handoff listen.

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path

import pytest

from sigint101 import meteor, storage
from sigint101.events import read_events
from sigint101.hardware import RtlSdrDevice
from tests.test_meteor import MARKER, eventually, progress_line
from tests.test_meteor import meteor_rig as meteor_rig
from tests.test_meteor_routes import LISTEN, STATUS, meteor_client

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"synthetic-meteor-lrpt-fixture"


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


@pytest.mark.parametrize("fail_at", ["success", "png", "manifest", "pass_dir", "gallery_dir"])
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

    def spy_sync_media(path):
        order.append("png")
        if fail_active[0] and fail_at == "png":
            raise OSError("injected png sync failure")
        return real_sync_media(path)

    def spy_fsync_file(path):
        if Path(path).name == "manifest.json":
            order.append("manifest")
            if fail_active[0] and fail_at == "manifest":
                raise OSError("injected manifest sync failure")
        return real_fsync_file(path)

    def spy_fsync_dir(path):
        p = Path(path)
        if p.name.startswith(".publish-"):
            order.append("pass_dir")
            if fail_active[0] and fail_at == "pass_dir":
                raise OSError("injected pass_dir sync failure")
        elif p.name == "meteor":
            order.append("gallery_dir")
            if fail_active[0] and fail_at == "gallery_dir":
                raise OSError("injected gallery_dir sync failure")
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
                assert order == ["png", "manifest", "pass_dir", "gallery_dir", "reception"]
                assert len(passes) == 2
                assert len(receptions) == 2
                assert client.get(f"/media/{passes[0]['images'][0]['path']}").status_code == 200
            else:
                assert "reception" not in order
                assert [p["id"] for p in passes] == [first_pass["id"]]
                assert len(receptions) == 1
                # No leftover .publish-* candidate directories after failure cleanup.
                leftovers = list((gen_dir / "meteor").glob(".publish-*"))
                assert leftovers == []
    finally:
        for p in made:
            p.close_pipe()
        decoder.shutdown()
