# ABOUTME: Tests every decoder's child lifecycle: a dead tool reads stopped, backs off, is killed.
# ABOUTME: Fakes stand at the process boundary; one real pipeline proves the SIGTERM-trap kill path.

import io
import shlex
import signal

import pytest

from sigint101 import (
    acars,
    adsb,
    ais,
    aprs,
    apt,
    childproc,
    decode,
    dmr,
    flock,
    pocsag,
    rds,
    sstv,
)
from sigint101.childproc import Pipeline, command_line, spawn
from tests.child_fakes import USB_BUSY, Child


class _Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


def _common():
    return {"radio_present": lambda: True, "which": lambda name: "/usr/bin/" + name}


# name -> (build(spawn, tmp_path), Unavailable, finite capture?)
DECODERS = {
    "decode": (lambda s, t: decode.Decoder(spawn=s, **_common()), decode.DecodeUnavailable, False),
    "aprs": (lambda s, t: aprs.AprsDecoder(spawn=s, **_common()), aprs.AprsUnavailable, False),
    "pocsag": (lambda s, t: pocsag.PocsagDecoder(spawn=s, **_common()),
               pocsag.PocsagUnavailable, False),
    "rds": (lambda s, t: rds.RDSDecoder(spawn=s, **_common()), rds.RDSUnavailable, False),
    "acars": (lambda s, t: acars.AcarsDecoder(spawn=s, **_common()),
              acars.AcarsUnavailable, False),
    "ais": (lambda s, t: ais.AisDecoder(spawn=s, **_common()), ais.AisUnavailable, False),
    "dmr": (lambda s, t: dmr.DmrDecoder(spawn=s, **_common()), dmr.DmrUnavailable, False),
    "adsb": (lambda s, t: adsb.AdsbDecoder(spawn=s, **_common()), adsb.AdsbUnavailable, False),
    "apt": (lambda s, t: apt.AptDecoder(spawn=s, output_dir=t / "apt", **_common()),
            apt.AptUnavailable, True),
    "sstv": (lambda s, t: sstv.SstvDecoder(spawn=s, output_dir=t / "sstv", data_dir=t,
                                           **_common()),
             sstv.SstvUnavailable, True),
    "flock": (lambda s, t: flock.FlockDetector(spawn=s, **_common()),
              flock.FlockUnavailable, False),
}


def _build(name, tmp_path, make_child):
    build, unavailable, _ = DECODERS[name]
    spawned = []

    def spawn_child(*_args):
        child = make_child()
        spawned.append(child)
        return child

    dec = build(spawn_child, tmp_path)
    clock = _Clock()
    dec.watch.clock = clock
    return dec, unavailable, spawned, clock


@pytest.fixture(autouse=True)
def _quick_stops(monkeypatch):
    # The real waits are seconds; the fakes answer at once or never, so a sliver is plenty.
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.01)
    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0.01)


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_a_child_that_dies_reads_stopped_and_is_not_respawned_every_poll(tmp_path, name):
    dec, unavailable, spawned, clock = _build(
        name, tmp_path, lambda: Child(["tool"], code=1, said=USB_BUSY))
    dec.start()
    dec.drain()
    assert dec.running is False

    # The next poll reports the death with the tool's own words, rather than respawning.
    with pytest.raises(unavailable) as first:
        dec.start()
    assert "exit status 1" in str(first.value)
    assert "usb_claim_interface error -6" in str(first.value)
    assert dec.watch.stopped is True
    assert dec.watch.retry_in() > 0

    # Polls inside the back-off keep saying so, and launch nothing.
    for _ in range(5):
        with pytest.raises(unavailable) as again:
            dec.start()
        assert "usb_claim_interface error -6" in str(again.value)
    assert len(spawned) == 1

    # Once the back-off runs out the console tries again — the dongle may have come free.
    clock.now += dec.watch.retry_in()
    dec.start()
    assert len(spawned) == 2
    dec.stop()


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_a_signal_death_is_reported_by_name(tmp_path, name):
    dec, unavailable, _, _ = _build(
        name, tmp_path, lambda: Child(["tool"], code=-signal.SIGKILL))
    dec.start()
    dec.drain()
    with pytest.raises(unavailable, match="killed by SIGKILL"):
        dec.start()


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_stop_escalates_to_kill_for_a_child_that_ignores_sigterm(tmp_path, name):
    dec, _, spawned, _ = _build(name, tmp_path, lambda: Child(["tool"], stubborn=True))
    dec.start()
    dec.stop()
    assert spawned[0].sent == ["TERM", "KILL"]
    assert spawned[0].poll() == -signal.SIGKILL
    assert dec.running is False
    assert dec.error is None


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_stop_says_so_when_a_child_survives_kill(tmp_path, name):
    dec, _, spawned, _ = _build(name, tmp_path, lambda: Child(["tool"], unkillable=True))
    dec.start()
    dec.stop()
    assert spawned[0].sent == ["TERM", "KILL"]
    assert "dongle" in dec.error or "interface" in dec.error


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_a_deliberate_stop_is_not_reported_as_a_death(tmp_path, name):
    dec, _, spawned, _ = _build(name, tmp_path, lambda: Child(["tool"]))
    dec.start()
    dec.stop()
    dec.drain()
    assert dec.watch.stopped is False
    dec.start()                     # no back-off: the console stopped it, nothing died
    assert len(spawned) == 2
    dec.stop()


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_live_command_is_read_off_the_running_child(tmp_path, name):
    argv = ["rtl_433", "-f", "433920000", "-F", "json"]
    dec, _, spawned, _ = _build(name, tmp_path, lambda: Child(argv))
    assert dec.live_command() is None           # nothing runs yet
    dec.start()
    assert dec.live_command() == "rtl_433 -f 433920000 -F json"
    dec.stop()
    assert dec.live_command() is None           # nothing runs any more


@pytest.mark.parametrize("name", sorted(DECODERS))
def test_live_command_is_none_once_the_child_has_died(tmp_path, name):
    dec, _, spawned, _ = _build(name, tmp_path, lambda: Child(["rtl_433"], code=1))
    dec.start()
    assert dec.live_command() is None


def test_a_streaming_pipeline_with_a_dead_first_stage_is_not_running(tmp_path):
    # rtl_fm died; multimon-ng has not noticed yet. Nothing is being decoded, so not running.
    def make():
        return Pipeline(Child(["rtl_fm"], code=1, said=USB_BUSY), Child(["multimon-ng"]))
    dec, unavailable, _, _ = _build("aprs", tmp_path, make)
    dec.start()
    assert dec.running is False
    with pytest.raises(unavailable, match="rtl_fm stopped"):
        dec.start()


def test_an_sstv_capture_that_finishes_is_not_a_death(tmp_path):
    # The capture is finite by design: sox trims, sstv decodes and exits 0, rtl_fm gets SIGPIPE.
    def make():
        return Pipeline(Child(["rtl_fm"], code=-signal.SIGPIPE), Child(["sox"], code=0),
                        Child(["sstv"], code=0), finite=True)
    dec, _, spawned, _ = _build("sstv", tmp_path, make)
    dec.start()
    dec.drain()
    dec.start()                     # the next capture begins at once
    assert len(spawned) == 2
    assert dec.watch.stopped is False


def test_an_apt_pass_that_times_out_cleanly_is_not_a_death(tmp_path):
    dec, _, spawned, _ = _build("apt", tmp_path, lambda: Child(["satdump"], code=0))
    dec.start()
    dec.start()
    assert len(spawned) == 2
    assert dec.watch.stopped is False


def test_a_real_pipeline_that_traps_sigterm_is_killed_on_stop(tmp_path, monkeypatch):
    # The audit's probe (T-06): both halves trap SIGTERM, and stop() used to return after 9 s
    # with both alive. Now it escalates, and neither survives.
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.3)
    procs = []

    def real_pipeline(freq, device):
        first = spawn(["bash", "-c", "trap '' TERM; while :; do sleep 0.1; done"])
        second = spawn(["bash", "-c", "trap '' TERM; cat >/dev/null"], stdin=first.stdout,
                       text=True)
        first.stdout.close()
        procs.extend([first, second])
        return Pipeline(first, second)

    dec = aprs.AprsDecoder(spawn=real_pipeline, **_common())
    dec.start()
    try:
        dec.stop()
        assert all(p.poll() is not None for p in procs)
        assert dec.error is None
    finally:
        for p in procs:
            if p.poll() is None:  # pragma: no cover - only when the assertion above failed
                p.kill()
                p.wait(timeout=3)
            for stream in (p.stdout, p.stderr):
                if stream is not None:
                    stream.close()


# --- every real launcher goes through childproc.spawn and carries its real argv ---------------


class _Launched:
    """What childproc.spawn hands back, recorded at the boundary instead of exec'd."""

    def __init__(self, argv, kw, code):
        self.args = list(argv)
        self.kw = kw
        self.code = code
        self.stdout = io.BytesIO()
        self.stderr = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -signal.SIGTERM

    def kill(self):
        self.code = -signal.SIGKILL

    def wait(self, timeout=None):
        return self.code


def _record_spawns(monkeypatch, module, code=None):
    seen = []

    def fake_spawn(argv, **kw):
        proc = _Launched(argv, kw, code)
        seen.append(proc)
        return proc

    monkeypatch.setattr(module, "spawn", fake_spawn)
    return seen


def _joined(seen):
    return " | ".join(shlex.join(p.args) for p in seen)


def test_decode_launches_rtl_433_through_spawn(monkeypatch):
    seen = _record_spawns(monkeypatch, decode)
    cmd = decode.Decoder(freq_hz=433_920_000, device=0).command()
    proc = decode._popen(cmd)
    assert seen[0].args == cmd
    assert command_line(proc) == shlex.join(cmd)


@pytest.mark.parametrize("module,tools", [
    (aprs, ("rtl_fm", "multimon-ng")),
    (pocsag, ("rtl_fm", "multimon-ng")),
    (rds, ("rtl_fm", "redsea")),
])
def test_rtl_fm_pipelines_launch_both_stages_through_spawn(monkeypatch, module, tools):
    seen = _record_spawns(monkeypatch, module)
    pipe = module._popen(144_390_000, 0)
    assert [p.args[0] for p in seen] == list(tools)
    assert "144390000" in seen[0].args
    assert seen[1].kw["stdin"] is seen[0].stdout       # rtl_fm feeds the decoder
    assert command_line(pipe) == _joined(seen)
    assert " | " in command_line(pipe)


def test_sstv_launches_its_three_stage_capture_through_spawn(monkeypatch, tmp_path):
    seen = _record_spawns(monkeypatch, sstv)
    pipe = sstv._popen(145_800_000, 0, tmp_path / "x.png")
    assert [p.args for p in seen] == sstv.pipeline_commands(145_800_000, 0, tmp_path / "x.png")
    assert command_line(pipe) == _joined(seen)
    assert pipe.finite is True


@pytest.mark.parametrize("launch,tool", [
    (lambda tmp: acars._popen(131_550_000, 0), "acarsdec"),
    (lambda tmp: dmr._popen(450_000_000, 0), "dsd-fme"),
    (lambda tmp: apt._popen(137_100_000, 0, tmp), "satdump"),
    (lambda tmp: flock._popen("wlan1"), "tcpdump"),
])
def test_single_tools_launch_through_spawn(monkeypatch, tmp_path, launch, tool):
    module = {"acarsdec": acars, "dsd-fme": dmr, "satdump": apt, "tcpdump": flock}[tool]
    seen = _record_spawns(monkeypatch, module)
    proc = launch(tmp_path)
    assert seen[0].args[0] == tool
    assert command_line(proc) == shlex.join(seen[0].args)


def test_ais_launches_rtl_ais_through_spawn(monkeypatch):
    seen = _record_spawns(monkeypatch, ais)
    feed = ais._popen(0, "127.0.0.1", 0)
    try:
        assert seen[0].args[0] == "rtl_ais"
        assert command_line(feed) == shlex.join(seen[0].args)
    finally:
        feed.terminate()


def test_the_sweep_launches_rtl_power_through_spawn(monkeypatch):
    # rtl_power holds the dongle like any decoder: own session, and gone if the console is.
    from sigint101 import spectrum
    seen = _record_spawns(monkeypatch, spectrum)
    cmd = ["rtl_power", "-f", "88000000:108000000:125000", "-i", "1", "-d", "0"]
    proc = spectrum._popen(cmd)
    assert seen[0].args == cmd
    assert proc.stderr is not None          # its stderr still lands where the sweeper reads it


def test_adsb_launch_failure_stops_dump1090_and_says_so(monkeypatch):
    # dump1090 exits before opening its SBS port: the launcher stops it and refuses, and the
    # argv it tried is the real one.
    seen = _record_spawns(monkeypatch, adsb, code=1)
    with pytest.raises(adsb.AdsbUnavailable):
        adsb._popen(0)
    assert seen[0].args[0] == "dump1090-fa"
    assert "--net-sbs-port" in seen[0].args
