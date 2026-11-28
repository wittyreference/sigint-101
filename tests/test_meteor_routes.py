# ABOUTME: Exercises the Meteor Listen now API and SDR claims through FastAPI.
# ABOUTME: Injects the child, bus census, sweeper, and ppm measurement at their boundaries.

import threading

import pytest
from fastapi.testclient import TestClient

from sigint101 import app, receiver_lifecycle
from sigint101.events import read_events
from tests.test_ais_launch import _decoder as make_ais
from tests.test_ais_launch import _fake_launch
from tests.test_ais_launch import companion as companion
from tests.test_app import FIXTURE_PACKS, _present, _StubSweeper
from tests.test_meteor import MARKER
from tests.test_meteor import meteor_rig as meteor_rig

LISTEN = "/api/meteor/listen"
STATUS = "/api/meteor"


class Sweep(_StubSweeper):
    def __init__(self):
        super().__init__([])
        self.released = False
        self.releases = 0

    def release(self):
        super().release()
        self.released = True
        self.releases += 1

    def resume(self):
        self.released = False


def meteor_client(tmp_path, decoder, **kwargs):
    sweep = kwargs.pop("sweeper_factory", Sweep)
    return TestClient(app.create_app(
        data_dir=tmp_path, pack_dir=FIXTURE_PACKS, probe=_present,
        sweeper_factory=sweep, meteor_factory=lambda: decoder, **kwargs,
    ))


def test_meteor_status_is_available_with_the_default_factory(tmp_path):
    with TestClient(app.create_app(data_dir=tmp_path, pack_dir=FIXTURE_PACKS,
                                    probe=_present, sweeper_factory=Sweep)) as client:
        assert client.get(STATUS).status_code == 200


def test_meteor_get_is_passive_and_listen_is_the_only_start(rig, tmp_path):
    decoder, made, _ = rig
    with meteor_client(tmp_path, decoder) as client:
        assert client.get(STATUS).json()["state"] == "idle"
        assert client.get(LISTEN).status_code == 405
        assert client.post(STATUS).status_code == 405
        assert client.post("/api/meteor/start").status_code == 404
        assert client.post("/api/meteor/predict").status_code == 404
        assert made == [] and client.app.state.sweeper.releases == 0
        result = client.post(LISTEN)
        assert result.status_code == 200
        assert result.json()["passes"] == [] and len(made) == 1
        assert client.get(STATUS).status_code == 200 and len(made) == 1


def test_meteor_refused_preflight_keeps_sweeper_running(rig, tmp_path):
    decoder, made, settings = rig
    settings["tool"] = False
    with meteor_client(tmp_path, decoder) as client:
        assert client.get("/api/spectrum").status_code == 200
        result = client.post(LISTEN)
        assert result.status_code == 503
        assert result.json()["cause"] == "missing_tool"
        assert result.json()["next_step"] == "Run deploy/provision.sh --with-optional"
        assert result.json()["passes"] == []
        assert client.app.state.sweeper.running and made == []
        assert client.app.state.sweeper.releases == 0


@pytest.mark.parametrize("setting", ["mode", "hz"])
def test_meteor_site_settings_are_validated_before_standdown(rig, tmp_path, monkeypatch, setting):
    decoder, _, _ = rig
    env = "SIGINT101_METEOR_" + setting.upper()
    with meteor_client(tmp_path, decoder) as client:
        idle = client.get(STATUS).json()
        assert (idle["mode"], idle["freq_hz"]) == ("72k", 137900000)
        client.get("/api/spectrum")
        monkeypatch.setenv(env, "invalid")
        failed = client.post(LISTEN)
        assert failed.status_code == 503
        assert env in failed.json()["reason"]
        for allowed in ("72k", "80k") if setting == "mode" else ("137900000", "137100000"):
            assert allowed in failed.json()["reason"]
        assert client.app.state.sweeper.running
        monkeypatch.setenv(env, "80k" if setting == "mode" else "137100000")
        changed = client.post(LISTEN)
        assert changed.status_code == 200
        assert (changed.json()["mode"], changed.json()["freq_hz"]) == (
            ("80k", 137900000) if setting == "mode" else ("72k", 137100000))


def test_meteor_listen_while_finishing_refuses_without_interrupting(rig, tmp_path, monkeypatch):
    decoder, made, _ = rig
    monkeypatch.setattr(app, "CLAIM_QUEUE_WAIT_S", 0.06)
    with meteor_client(tmp_path, decoder) as client:
        assert client.post(LISTEN).status_code == 200
        proc = made[0][0]
        proc.feed(MARKER)
        assert decoder._run.physical.wait(2)
        response = client.post(LISTEN)
        assert response.status_code == 503
        assert "finishing" in response.json()["reason"]
        assert client.get(STATUS).json()["state"] == "decoding"
        assert len(made) == 1 and not proc.term.is_set() and not proc.killed.is_set()


def test_meteor_claim_after_marker_sends_no_signal_and_finishes(rig, tmp_path):
    decoder, made, _ = rig
    with meteor_client(tmp_path, decoder) as client:
        assert client.post(LISTEN).status_code == 200
        proc = made[0][0]
        proc.feed(MARKER)
        assert decoder._run.physical.wait(2)
        assert client.get("/api/spectrum").status_code == 200
        assert proc.poll() is None and not proc.term.is_set() and not proc.killed.is_set()
        proc.picture()
        proc.die(0)
        proc.close_pipe()
        assert decoder._run.complete.wait(2)
        assert client.get(STATUS).json()["state"] == "done"
        assert client.get("/api/receptions?instrument=meteor").json()["receptions"] == []
        assert not any(e["kind"] == "reception" for e in read_events(tmp_path / "events.jsonl"))


def test_meteor_hardware_reads_present_during_finishing(rig, tmp_path):
    decoder, made, _ = rig
    with meteor_client(tmp_path, decoder) as client:
        assert client.post(LISTEN).status_code == 200
        assert client.get("/api/hardware").json()["state"] == "busy"
        made[0][0].feed(MARKER)
        assert decoder._run.physical.wait(2)
        assert client.get("/api/hardware").json()["state"] == "present"
        assert client.get(STATUS).json()["state"] == "decoding"


def test_meteor_ppm_can_claim_during_finishing(rig, tmp_path):
    decoder, made, _ = rig
    with meteor_client(tmp_path, decoder, ppm_measure=lambda **_: {"ppm": 3.5},
                       list_devices=lambda: ()) as client:
        assert client.post(LISTEN).status_code == 200
        proc = made[0][0]
        proc.feed(MARKER)
        assert decoder._run.physical.wait(2)
        assert client.post("/api/ppm/measure").status_code == 200
        assert proc.poll() is None and not proc.term.is_set() and not proc.killed.is_set()


@pytest.mark.parametrize("end", ["idle", "watchdog", "shutdown", "incomplete-drain", "death"])
def test_meteor_idle_and_interrupted_have_no_death_retry(rig, tmp_path, monkeypatch, end):
    from sigint101.meteor import FINISH_CAP_S

    monkeypatch.setattr(receiver_lifecycle, "STREAM_EOF_S", 0.06)
    decoder, made, settings = rig
    settings["now"] = 1
    with meteor_client(tmp_path, decoder) as client:
        if end != "idle":
            assert client.post(LISTEN).status_code == 200
            proc = made[0][0]
            if end == "watchdog":
                settings["now"] += 900 + FINISH_CAP_S
                decoder._run.wake.set()
            elif end == "shutdown":
                decoder.shutdown()
            else:
                proc.die(4 if end == "death" else 0)
                if end == "death":
                    proc.close_pipe()
            assert decoder._run.complete.wait(2)
        value = client.get(STATUS).json()
        if end == "death":
            assert value["state"] == "stopped" and "retry_in" in value
        else:
            assert value["state"] == ("idle" if end == "idle" else "interrupted")
            assert "retry_in" not in value
            assert value["reason"] == {
                "idle": "Ready to listen", "watchdog": "SatDump exceeded its finishing deadline.",
                "shutdown": "Console shut down.",
                "incomplete-drain": "SatDump stderr did not reach EOF.",
            }[end]


@pytest.mark.parametrize("managed", [True, False], ids=["managed", "legacy"])
def test_meteor_factory_passes_managed_media_sync_flag(tmp_path, managed):
    from sigint101.migrations import migrate_store_copy

    data = tmp_path / "data"
    data.mkdir()
    if managed:
        for store in ("progress", "exam"):
            migrate_store_copy(tmp_path / f"missing-{store}.db", data / f"{store}.db",
                               store=store, allow_missing_source=True,
                               scratch_dir=tmp_path / f"scratch-{store}")
    application = app.create_app(data_dir=data, pack_dir=FIXTURE_PACKS,
                                 probe=_present, sweeper_factory=Sweep, managed=managed)
    with TestClient(application):
        assert application.state.meteor._sync_assets is managed


def test_meteor_listen_refuses_unreaped_rtl433(rig, tmp_path):
    from sigint101.decode import Decoder
    from tests.child_fakes import Child

    decoder, made, _ = rig
    child = Child(["rtl_433"], unkillable=True)
    rtl433 = Decoder(spawn=lambda *_: child, which=lambda name: name, radio_present=lambda: True)
    with meteor_client(tmp_path, decoder, decoder_factory=lambda: rtl433) as client:
        if not hasattr(rtl433, "release_failure"):
            pytest.skip("requires PR 148's retained rtl_433 release failure")
        rtl433.start()
        try:
            result = client.post(LISTEN)
            assert result.status_code == 503 and "rtl_433" in result.json()["reason"]
            assert made == []
            assert client.get("/api/hardware").json()["state"] == "busy"
        finally:
            child.unkillable = False


def test_meteor_listen_honors_a_ppm_failure_published_while_queued(rig, tmp_path, monkeypatch):
    from sigint101.ppm import PpmUnavailable

    decoder, made, _ = rig
    measured, finish, preparing = threading.Event(), threading.Event(), threading.Event()

    def measure(**_):
        measured.set()
        finish.wait(3)
        failed.set()
        raise PpmUnavailable("rtl_fm survived kill")

    failed = threading.Event()
    with meteor_client(tmp_path, decoder, ppm_measure=measure, list_devices=lambda: ()) as client:
        calibration = getattr(client.app.state, "ppm_calibration", None)
        if calibration is None:
            pytest.skip("requires C73's retained ppm release failure")
        monkeypatch.setattr(calibration, "release_failure",
                            lambda: "rtl_fm survived kill" if failed.is_set() else None)
        prepare = decoder.prepare_start

        def mark_preflight(**kwargs):
            intent = prepare(**kwargs)
            preparing.set()
            return intent

        monkeypatch.setattr(decoder, "prepare_start", mark_preflight)
        ppm = threading.Thread(target=lambda: client.post("/api/ppm/measure"), daemon=True)
        result = []
        listening = threading.Thread(target=lambda: result.append(client.post(LISTEN)), daemon=True)
        ppm.start()
        try:
            assert measured.wait(2)
            listening.start()
            assert preparing.wait(2)
        finally:
            finish.set()
        ppm.join(3)
        listening.join(3)
        assert result[0].status_code == 503
        assert "rtl_fm survived kill" in result[0].json()["reason"]
        assert made == [] and client.get("/api/hardware").json()["state"] == "busy"


def test_meteor_provenance_poll_does_not_reclaim_the_radio(rig, tmp_path):
    decoder, made, _ = rig
    with meteor_client(tmp_path, decoder) as client:
        if not hasattr(client.app.state, "ppm_calibration"):
            pytest.skip("requires C73's passive provenance claim")
        assert client.get("/api/spectrum").status_code == 200
        assert client.post(LISTEN).status_code == 200
        for finishing in (False, True):
            if finishing:
                made[0][0].feed(MARKER)
                assert decoder._run.physical.wait(2)
            assert client.get("/api/provenance").status_code == 200
            assert not client.app.state.sweeper.running
            assert made[0][0].sent in ([], [("nice", 10)])


def test_meteor_and_ais_share_verified_handoffs(rig, tmp_path, monkeypatch, companion, ais_port):
    decoder, made, _ = rig
    launches = _fake_launch(monkeypatch)
    ais = make_ais(companion, port=ais_port)
    which = ais._which
    monkeypatch.setattr(ais, "_which", lambda _: None)
    spawn = decoder._spawn

    def after_ais_reaps(*args, **kwargs):
        if launches:
            assert launches[-1][0].poll() is not None
        return spawn(*args, **kwargs)

    monkeypatch.setattr(decoder, "_spawn", after_ais_reaps)
    with meteor_client(tmp_path, decoder, ais_factory=lambda: ais) as client:
        assert client.post(LISTEN).status_code == 200
        first = made[0][0]
        assert client.get("/api/ais").status_code == 503
        assert first.sent == [] and launches == []
        first.feed(MARKER)
        assert decoder._run.physical.wait(2)
        monkeypatch.setattr(ais, "_which", which)
        assert client.get("/api/ais").status_code == 200 and len(launches) == 1
        assert not first.term.is_set() and not first.killed.is_set()
        first.die(0)
        first.close_pipe()
        assert decoder._run.complete.wait(2)
        assert client.post(LISTEN).status_code == 200
        assert launches[0][0].sent[0] == "TERM" and len(made) == 2


def test_meteor_unsafe_staging_refuses_before_standdown(rig, tmp_path):
    decoder, made, _ = rig
    root = tmp_path / "meteor"
    root.mkdir(mode=0o700)
    other = tmp_path / "other"
    other.mkdir()
    (root / "work").symlink_to(other, target_is_directory=True)
    with meteor_client(tmp_path, decoder) as client:
        assert client.get("/api/spectrum").status_code == 200
        refusal = client.post(LISTEN)
        assert refusal.status_code == 503 and "cannot prepare Meteor" in refusal.json()["reason"]
        assert client.app.state.sweeper.running and not made
        assert list(other.iterdir()) == []
