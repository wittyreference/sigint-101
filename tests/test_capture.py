# ABOUTME: Proves finite IQ capture, exact radio selection, and private pair publication.
# ABOUTME: Uses real pipes and confined storage fixtures without opening an RTL-SDR.

import ast
import errno
import hashlib
import json
import os
import stat
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from sigint101 import childproc, receiver_lifecycle
from sigint101.hardware import RtlSdrDevice
from tests.child_fakes import Child
from tests.test_app import FIXTURE_PACKS, _present

HZ = 433_921_017
TUNE = {"hz": HZ, "gain": "19.7"}
AUTO = {"hz": HZ, "gain": None}
IQ = bytes(4_500_000)
RADIO = (RtlSdrDevice(0, "abc"),)
INVENTORY = "Found 1 device(s):\n  0:  Vendor, Model, SN: abc\n\nUsing device 0: Model\n"


class Recorder(Child):
    def __init__(self, root, *, diagnostic=INVENTORY, **kwargs):
        super().__init__(**kwargs)
        self.stderr = tempfile.TemporaryFile(dir=root)
        self.stderr.write(diagnostic.encode())
        self.stderr.flush()
        fd, self.out = os.pipe()
        self.stdout = os.fdopen(fd, "rb", buffering=0)

    def feed(self, data=IQ, *, code=0, hold_eof=False):
        def produce():
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(self.out, view):]
            except BrokenPipeError:
                pass
            finally:
                self.code = code
                if not hold_eof:
                    self.close_pipe()

        worker = threading.Thread(target=produce, daemon=True)
        worker.start()
        return worker

    def close_pipe(self):
        if self.out is not None:
            fd, self.out = self.out, None
            os.close(fd)

    def terminate(self):
        was_running = self.code is None
        super().terminate()
        if was_running and self.code is not None:
            self.close_pipe()

    def kill(self):
        was_running = self.code is None
        super().kill()
        if was_running and self.code is not None:
            self.close_pipe()


def capture(root, proc=None, *, spawn=None, radios=RADIO, serial=None, **kwargs):
    from sigint101.capture import Capture

    return Capture(root, spawn=spawn or (lambda _argv, **_opts: proc),
                   which=lambda name: name, list_devices=lambda: radios,
                   radio_present=lambda: True, serial=serial, **kwargs)


def start(cap, tune=TUNE, *, settings=None, ppm=lambda: 1.25, claim=None):
    def launch():
        intent = cap.prepare_start(for_claim=True)
        cap.validate_start(intent)
        return cap.launch_start(intent, claimed=True)

    return cap.request_start(tune, settings=settings or (lambda: tune), ppm=ppm,
                             claim=claim or launch)


def client(root, cap, **kwargs):
    from sigint101.app import create_app
    from sigint101.spectrum import Sweeper

    def sweeper():
        result = Sweeper(radio_present=lambda: True, list_devices=lambda: RADIO,
                         spawn=lambda *args, **kw: Child())
        result.retune_to(HZ, 2_000_000)
        result.set_gain(19.7)
        return result

    return TestClient(create_app(pack_dir=FIXTURE_PACKS, data_dir=root, probe=_present,
                                 capture_factory=lambda: cap, list_devices=lambda: (),
                                 sweeper_factory=kwargs.pop("sweeper_factory", sweeper), **kwargs))


@pytest.fixture(autouse=True)
def bounded_stops(monkeypatch):
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.02)
    monkeypatch.setattr(receiver_lifecycle, "SUPERVISOR_POLL_S", 0.01)


def test_capture_uses_the_confirmed_waterfall_tune(tmp_path):
    from sigint101.spectrum import Sweeper

    sweep = Sweeper()
    sweep.retune(432_000_001, 434_000_001)
    assert sweep.capture_settings() == {"hz": 433_000_001, "gain": None}
    sweep.retune_to(HZ, 2_000_000)
    sweep.set_gain(19.7)
    argv = []
    proc = Recorder(tmp_path)

    def spawn(command, **opts):
        argv[:] = command
        assert not opts.get("text", False) and not opts.get("pass_fds")
        assert set(opts["env"]) == {"PATH", "LC_ALL"}
        assert not any(str(tmp_path) in arg for arg in command)
        return proc

    cap = capture(tmp_path, spawn=spawn)
    with ThreadPoolExecutor() as pool:
        with sweep._life:
            assert pool.submit(sweep.capture_settings).result(1) == TUNE
        start(cap, settings=sweep.capture_settings)
    sweep.retune_to(HZ + 1000, 2_000_000)
    sweep.set_gain(None)
    proc.feed().join(3)
    cap.drain()
    assert argv == ["rtl_sdr", "-d", "0", "-f", str(HZ), "-s", "250000",
                    "-n", "2250000", "-g", "19.7", "-"]
    meta, = (tmp_path / "captures").glob("c-*/*.sigmf-meta")
    content = json.loads(meta.read_text(encoding="utf-8"))
    glob, segment = content["global"], content["captures"][0]
    assert segment["core:frequency"] == HZ
    prov = glob["sigint101:provenance"]
    assert (prov["freq_hz"], prov["gain"], prov["ppm"]["measured_ppm"]) == (HZ, "19.7", 1.25)
    assert prov["radio"] == {"kind": "rtl-sdr", "index": 0, "serial": "abc"}
    assert glob["core:sha512"] == hashlib.sha512(IQ).hexdigest()
    assert glob["core:datatype"] == "cu8"


@pytest.mark.parametrize("case", ["malformed", "stale", "outside", "zero", "tool",
                                  "not-present", "ambiguous", "quota", "pinned", "serial-less"])
def test_capture_preflight_refuses_before_standdown(tmp_path, case):
    from sigint101.capture import CaptureUnavailable
    from sigint101.capture_store import PAIR_ALLOWANCE

    proc = Recorder(tmp_path)
    radios, serial, tune = RADIO, None, TUNE
    diagnostic = INVENTORY
    if case == "pinned":
        radios, serial = (*RADIO, RtlSdrDevice(1, "def")), "def"
        diagnostic = ("Found 2 device(s):\n  0:  Vendor, Model, SN: abc\n"
                      "  1:  Vendor, Model, SN: def\nUsing device 1: Model\n")
    if case == "serial-less":
        radios, diagnostic = (RtlSdrDevice(0, ""),), INVENTORY.replace("abc", "")
    if case == "ambiguous":
        radios = (*RADIO, RtlSdrDevice(1, "def"))
    proc.stderr.seek(0)
    proc.stderr.truncate()
    proc.stderr.write(diagnostic.encode())
    proc.stderr.flush()
    args = []
    cap = capture(tmp_path, spawn=lambda argv, **_opts: (args.extend(argv), proc)[1],
                  radios=radios, serial=serial)
    if case == "malformed":
        tune = {"hz": str(HZ), "gain": "19.7"}
    if case == "stale":
        tune = {**TUNE, "hz": HZ + 1}
    if case == "outside":
        tune = {**TUNE, "hz": 930_000_000}
    if case == "zero":
        tune = {**TUNE, "gain": "0.01"}
    if case == "tool":
        cap._which = lambda _: None
    if case == "not-present":
        cap._radio_present = lambda: False
    if case == "quota":
        for _ in range(5):
            cap.store.save(bytes(PAIR_ALLOWANCE - 2), b"{}")
    try:
        if case in ("pinned", "serial-less"):
            start(cap, AUTO)
            proc.feed().join(3)
            cap.drain()
            assert args[2] == ("1" if case == "pinned" else "0")
            assert "-g" not in args
            assert cap.snapshot()["state"] == "saved"
        else:
            def cannot_claim():
                pytest.fail("an invalid capture reached radio stand-down")

            with pytest.raises(CaptureUnavailable):
                start(cap, tune, settings=lambda: TUNE, claim=cannot_claim)
            assert not args
    finally:
        if not args:
            proc.stdout.close()
            proc.stderr.close()
        proc.close_pipe()


def test_capture_preflight_does_not_protect_the_radio(tmp_path, monkeypatch):
    from sigint101.capture_store import CaptureStorageError

    entered, allow_refusal = threading.Event(), threading.Event()
    cap = capture(tmp_path, spawn=lambda *_a, **_kw: pytest.fail("capture must not launch"))

    def preflight():
        entered.set()
        assert allow_refusal.wait(5)
        raise CaptureStorageError("Capture storage is full: 25 MB capture quota", full=True)

    monkeypatch.setattr(cap.store, "preflight", preflight)
    with client(tmp_path, cap) as c, ThreadPoolExecutor(1) as pool:
        pending = pool.submit(c.post, "/api/capture/start", json=TUNE)
        try:
            assert entered.wait(3)
            assert c.get("/api/spectrum").status_code == 200
            assert c.get("/api/capture").json()["state"] == "idle"
            assert c.post("/api/capture/start", json=TUNE).status_code == 409
        finally:
            allow_refusal.set()
        assert pending.result(3).status_code == 507


@pytest.mark.parametrize("where", ["preflight", "claim"])
def test_capture_refusal_preserves_a_saved_clip_and_status(tmp_path, monkeypatch, where):
    proc = Recorder(tmp_path)
    cap = capture(tmp_path, proc)
    with client(tmp_path, cap) as c:
        assert c.post("/api/capture/start", json=TUNE).status_code == 200
        proc.feed().join(3)
        cap.drain()
        saved = c.get("/api/capture").json()
        assert saved["state"] == "saved" and cap.store.catalog() == [saved["last_saved"]]
        with monkeypatch.context() as patch:
            if where == "preflight":
                patch.setattr(cap, "_which", lambda _: None)
            else:
                patch.setattr(c.app.state.ppm_calibration, "release_failure",
                              lambda: "the calibration child has not exited")
            response = c.post("/api/capture/start", json=TUNE)
        assert response.status_code == 503
        assert c.get("/api/capture").json() == saved
        assert cap.store.catalog() == [saved["last_saved"]]


@pytest.mark.parametrize("where", ["preflight", "spawn"])
def test_unexpected_capture_failure_leaves_start_available(tmp_path, monkeypatch, where):
    proc = Recorder(tmp_path)
    cap = capture(tmp_path, proc)

    def broken(*_args, **_kwargs):
        raise RuntimeError("fixture unexpectedly failed")

    attr = (cap.store, "preflight") if where == "preflight" else (cap, "_spawn")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(*attr, broken)
            with pytest.raises(RuntimeError, match="fixture unexpectedly failed"):
                start(cap)
        assert cap.snapshot()["state"] == "idle" and cap.protection() is None
        assert start(cap)["state"] == "recording"
    finally:
        cap.shutdown()
        proc.close_pipe()
        proc.stdout.close()
        proc.stderr.close()


@pytest.mark.parametrize("inventory", [
    INVENTORY, INVENTORY.replace("abc", "def"), INVENTORY.replace("device 0:", "device 1:"),
    INVENTORY.replace("Found 1", "Found 2"), INVENTORY.replace("SN: abc", "SN:"),
    INVENTORY.replace("Using device 0:", "Using device 0:\nUsing device 0:"),
    INVENTORY.replace("Using device 0:", ""),
    INVENTORY.replace("Found 1", "Found 2").replace("\n\nUsing", "\n  0:  V, M, SN: def\nUsing"),
])
def test_capture_checks_the_rtl_sdr_device_it_selected(tmp_path, inventory):
    proc = Recorder(tmp_path, diagnostic=inventory)
    cap = capture(tmp_path, proc)
    start(cap)
    proc.feed().join(3)
    cap.drain()
    status = cap.snapshot()
    if inventory == INVENTORY:
        assert status["state"] == "saved" and len(cap.store.catalog()) == 1
    else:
        assert status["state"] == "error"
        assert "radio changed while opening" in status["reason"].lower()
        assert not cap.store.catalog()


@pytest.mark.parametrize("case", ["good", "late", "short", "odd", "empty", "extra", "exit",
                                  "tuner", "usb", "head", "tail", "stop"])
def test_capture_requires_exact_samples_and_eof(tmp_path, monkeypatch, case):
    diagnostic = INVENTORY + {
        "tuner": "[R82XX] PLL not locked!\n",
        "usb": "Short write, samples lost\nusb transfer error\n",
    }.get(case, "")
    proc = Recorder(tmp_path, diagnostic=diagnostic)
    cap = capture(tmp_path, proc)
    if case in ("head", "tail"):
        real = os.pread
        count = 0

        def fail(fd, size, off):
            nonlocal count
            count += 1
            if count == (1 if case == "head" else 2):
                raise OSError(errno.EIO, "fixture diagnostic failure")
            return real(fd, size, off)

        monkeypatch.setattr(os, "pread", fail)
    start(cap)
    if case == "stop":
        cap.stop()
    else:
        stream = {"short": IQ[:-2], "odd": IQ[:-1], "empty": b"", "extra": IQ + b"x"}.get(case, IQ)
        if case == "late":
            proc.code = 0
            assert not cap.store.catalog()
        proc.feed(stream, code=1 if case == "exit" else 0).join(3)
    cap.drain()
    status = cap.snapshot()
    assert not cap.release_failure(pending=True)
    if case in ("good", "late"):
        assert status["state"] == "saved"
        meta, = (tmp_path / "captures").glob("c-*/*.sigmf-meta")
        content = json.loads(meta.read_text())
        prov = content["global"]["sigint101:provenance"]
        dt = content["captures"][0]["core:datetime"]
        assert dt.endswith("Z") and dt >= prov["launched_at"]
        assert "arrival" in prov["timestamp_basis"]
    else:
        assert status["state"] == ("stopped" if case == "stop" else "error")
        assert not cap.store.catalog()
        if case in ("head", "tail"):
            assert "diagnostic" in status["reason"]
        if case == "usb":
            assert "radio stopped answering" in status["reason"]


@pytest.mark.parametrize("case", ["queue", "spawn", "silent", "no-eof"])
def test_capture_deadline_starts_before_spawn_and_covers_no_eof(tmp_path, monkeypatch, case):
    import sigint101.capture as module

    monkeypatch.setattr(module, "CAPTURE_TIMEOUT_S", 0.15)
    proc = Recorder(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def spawn(*_args, **_opts):
        entered.set()
        if case == "spawn":
            assert release.wait(4)
        return proc

    cap = capture(tmp_path, spawn=spawn)

    def launch():
        intent = cap.prepare_start(for_claim=True)
        cap.validate_start(intent)
        if case == "queue":
            entered.set()
            assert release.wait(4)
        return cap.launch_start(intent, claimed=True)

    with ThreadPoolExecutor() as pool:
        future = pool.submit(start, cap, claim=launch)
        try:
            assert entered.wait(2)
            if case == "queue":
                assert not release.wait(0.25)
                assert cap.snapshot()["state"] == "idle" and cap.protection() is None
                release.set()
                future.result(2)
            if case == "no-eof":
                future.result(2)
                proc.feed(hold_eof=True).join(2)
            if case != "queue":
                from time import monotonic, sleep

                until = monotonic() + 3
                while not cap.snapshot()["reason"] and monotonic() < until:
                    sleep(0.01)
                assert "deadline" in cap.snapshot()["reason"].lower()
                if case == "spawn":
                    assert cap.release_failure(pending=True)
            else:
                cap.stop()
        finally:
            proc.close_pipe()
            release.set()
            try:
                future.result(3)
            except module.CaptureUnavailable:
                pass
            cap.shutdown()
    assert not cap.store.catalog()


def _park_commit(cap, monkeypatch):
    entered, proceed = threading.Event(), threading.Event()
    save = cap.store.save

    def gated(data, meta, admit):
        def gate():
            entered.set()
            assert proceed.wait(4)
            return admit()
        return save(data, meta, gate)

    monkeypatch.setattr(cap.store, "save", gated)
    return entered, proceed


@pytest.mark.parametrize("control", ["stop", "shutdown"])
def test_stop_cancels_a_capture_save_before_commit(tmp_path, monkeypatch, control):
    proc = Recorder(tmp_path)
    cap = capture(tmp_path, proc)
    entered, proceed = _park_commit(cap, monkeypatch)
    start(cap)
    proc.feed().join(3)
    assert entered.wait(2)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(getattr(cap, control))
        try:
            assert cap._run.args[0].cancelled.wait(2)
        finally:
            proceed.set()
        pending.result(3)
    assert cap.snapshot()["state"] == "stopped"
    assert not cap.store.catalog()


def test_competing_release_preserves_a_clean_capture_save(tmp_path, monkeypatch):
    proc = Recorder(tmp_path)
    cap = capture(tmp_path, proc)
    entered, proceed = _park_commit(cap, monkeypatch)
    start(cap)
    proc.feed().join(3)
    assert entered.wait(3)
    try:
        cap.release()
        assert cap.snapshot()["state"] == "saving"
        assert not cap.release_failure(pending=True)
    finally:
        proceed.set()
    cap.drain()
    assert cap.snapshot()["state"] == "saved"
    cap.stop()
    assert len(cap.store.catalog()) == 1


def test_capture_inherits_supervised_stop_and_retry():
    from sigint101.capture import Capture

    assert Capture.__bases__ == (receiver_lifecycle.SupervisedReceiver,)
    assert not (set(vars(Capture)) & {
        "_stop_child", "_stop_run", "_retry", "_retry_reap", "_cancel", "_supervise",
    })
    app_source = Path("sigint101/app.py").read_text()
    tree = ast.parse(app_source)
    opt_ins = [node.args[0].value for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "_claim_sdr"
               and any(k.arg == "explicit" and isinstance(k.value, ast.Constant)
                       and k.value.value is True for k in node.keywords)]
    assert sorted(opt_ins) == ["capture", "meteor", "ppm"]


def _store(path):
    from sigint101.capture_store import CaptureStore
    return CaptureStore(path)


@pytest.mark.parametrize("setgid", [False, True], ids=["private", "setgid-parent"])
def test_capture_publishes_one_private_pair_atomically(tmp_path, monkeypatch, setgid):
    import re

    from sigint101 import capture_store

    if setgid:
        os.chmod(tmp_path, 0o2700)
    store = _store(tmp_path)
    trace = []
    sync = os.fsync
    rename = capture_store._rename_new
    published, proceed = threading.Event(), threading.Event()

    def fsync(fd):
        trace.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        sync(fd)

    def gate(*args):
        published.set()
        assert proceed.wait(3)
        trace.append("rename")
        return rename(*args)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(capture_store, "_rename_new", gate)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(store.save, b"iq", b"{}")
        try:
            assert published.wait(3)
            assert pool.submit(store.catalog).result(1) == []
        finally:
            proceed.set()
        receipt = pending.result(3)
    stem = receipt["id"]
    assert re.fullmatch(r"c-[0-9]{8}t[0-9]{6}z-[0-9a-f]{32}", stem)
    clip = tmp_path / "captures" / stem
    assert stat.S_IMODE(clip.stat().st_mode) == stat.S_IMODE(clip.parent.stat().st_mode) == 0o700
    assert sorted(p.name for p in clip.iterdir()) == [f"{stem}.sigmf-data", f"{stem}.sigmf-meta"]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in clip.iterdir())
    i = trace.index("rename")
    assert trace[:i][-3:] == ["file", "file", "dir"]
    assert trace[i + 1:i + 3] == ["dir", "dir"]
    assert [item["id"] for item in store.catalog()] == [stem]


@pytest.mark.parametrize("tamper", ["root", "symlink", "hardlink", "fifo", "unknown"])
def test_capture_writes_and_cleanup_stay_beneath_data_dir(tmp_path, tamper):
    from sigint101.capture_store import CaptureStorageError

    store = _store(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    marker = other / "keep"
    marker.write_text("keep")
    if tamper == "root":
        (tmp_path / "captures").symlink_to(other, target_is_directory=True)
        assert store.catalog() == []
        with pytest.raises(CaptureStorageError):
            store.save(b"iq", b"{}")
    else:
        receipt = store.save(b"iq", b"{}")
        stem = receipt["id"]
        clip = tmp_path / "captures" / stem
        path = clip / (stem + ".sigmf-meta")
        path.unlink()
        if tamper == "symlink":
            path.symlink_to(marker)
        elif tamper == "hardlink":
            os.link(marker, path)
        elif tamper == "fifo":
            os.mkfifo(path, 0o600)
        else:
            (clip / "unknown").write_text("keep")
        assert store.catalog() == []
        store.preflight()
        assert clip.is_dir()
    assert marker.read_text() == "keep"


def test_two_consoles_keep_each_others_held_capture_stages(tmp_path, monkeypatch):
    from sigint101 import capture_store
    from sigint101.staging import create_owner

    a, b = _store(tmp_path), _store(tmp_path)
    a.preflight()
    root = tmp_path / "captures"
    first, finish = threading.Event(), threading.Event()
    rename = capture_store._rename_new

    def park(*args):
        first.set()
        assert finish.wait(4)
        return rename(*args)

    monkeypatch.setattr(capture_store, "_rename_new", park)
    with ThreadPoolExecutor() as pool:
        save = pool.submit(a.save, b"iq", b"{}")
        try:
            assert first.wait(3)
            stage, = (p for p in (root / ".staging").iterdir() if p.is_dir())
            with pytest.raises(capture_store.CaptureStorageError, match="busy"):
                b.preflight()
            assert stage.is_dir() and b.catalog() == []
        finally:
            finish.set()
        save.result(3)
    assert len(b.catalog()) == 1
    assert not stage.exists()

    fd = os.open(root / ".staging", os.O_RDONLY | os.O_DIRECTORY)
    try:
        name, owner, lock = create_owner(fd, "Capture")
        held = root / ".staging" / name
        try:
            b.preflight()
            assert held.is_dir()
        finally:
            os.close(lock)
            os.close(owner)
        b.preflight()
        assert not held.exists()
    finally:
        os.close(fd)


def test_capture_space_floor_preserves_progress_and_events(tmp_path, monkeypatch):
    from sigint101 import capture_store

    free = capture_store.PAIR_ALLOWANCE + capture_store.HEADROOM
    monkeypatch.setattr(os, "fstatvfs", lambda _fd: SimpleNamespace(f_bavail=free, f_frsize=1))
    store = _store(tmp_path)
    first = store.save(b"previous", b"{}")
    for name in ("progress.db", "events.jsonl"):
        (tmp_path / name).write_text("keep")
    free -= 1
    with pytest.raises(capture_store.CaptureStorageError, match="full"):
        store.preflight()
    with pytest.raises(capture_store.CaptureStorageError, match="full"):
        store.save(b"next", b"{}")
    assert [item["id"] for item in store.catalog()] == [first["id"]]
    for name in ("progress.db", "events.jsonl"):
        assert (tmp_path / name).read_text() == "keep"


@pytest.mark.parametrize("op,code", [
    ("mkdir", errno.ENOSPC), ("write", errno.EDQUOT), ("fsync", errno.EIO),
    ("rename", errno.EIO), ("post-sync", errno.EIO),
])
def test_capture_disk_failure_keeps_previous_pairs(tmp_path, monkeypatch, op, code):
    from sigint101 import capture_store

    store = _store(tmp_path)
    first = store.save(b"previous", b"{}")
    owner = capture_store if op == "rename" else os
    name = {"rename": "_rename_new", "post-sync": "fsync"}.get(op, op)
    original = getattr(owner, name)
    failed = published = False

    def after_rename(*args):
        nonlocal published
        rename(*args)
        published = True

    rename = capture_store._rename_new

    def inject(*args, **kwargs):
        nonlocal failed
        armed = published if op == "post-sync" else op != "mkdir" or args[0] == "payload"
        if armed and not failed:
            failed = True
            raise OSError(code, "fixture storage failure")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        if op == "post-sync":
            patch.setattr(capture_store, "_rename_new", after_rename)
        patch.setattr(owner, name, inject)
        with pytest.raises(capture_store.CaptureStorageError, match="full" if code in (
                errno.ENOSPC, errno.EDQUOT) else "storage"):
            store.save(b"next", b"{}")
    assert failed and store.catalog() == [first]
    result = store.save(b"next", b"{}")
    assert result in store.catalog() and len(store.catalog()) == 2


@pytest.mark.parametrize("operation", ["preflight", "save"])
@pytest.mark.parametrize("obstacle", ["symlink", "vanished-owner"])
def test_capture_quota_survives_a_stray_entry(tmp_path, monkeypatch, operation, obstacle):
    from sigint101 import capture_store

    store = _store(tmp_path)
    first = store.save(b"kept", b"{}")
    if obstacle == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "untouched").write_text("leave this alone")
        stray = tmp_path / "captures" / ("c-20260101t000000z-" + "0" * 32)
        stray.symlink_to(elsewhere, target_is_directory=True)
    else:
        used, listdir = capture_store._used, os.listdir
        vanished = "0" * 32

        def race(root, staging, mount):
            # Create this owner after the sweep. It vanishes between listing and opening.
            os.mkdir(vanished, 0o700, dir_fd=staging)

            def list_then_remove(fd):
                names = listdir(fd)
                if fd == staging:
                    os.rmdir(vanished, dir_fd=staging)
                return names

            with monkeypatch.context() as patch:
                patch.setattr(os, "listdir", list_then_remove)
                return used(root, staging, mount)

        monkeypatch.setattr(capture_store, "_used", race)

    expected = [first]
    if operation == "preflight":
        store.preflight()
    else:
        expected.append(store.save(b"next", b"{}"))
    assert sorted(store.catalog(), key=lambda item: item["id"]) == sorted(
        expected, key=lambda item: item["id"]
    )
    if obstacle == "symlink":
        assert stray.is_symlink() and (elsewhere / "untouched").read_text() == "leave this alone"


@pytest.mark.parametrize("publish,rollback", [
    (errno.ENOSPC, errno.EIO), (errno.EDQUOT, errno.ENOSPC), (errno.EIO, errno.EDQUOT),
])
def test_capture_reports_full_when_publication_and_rollback_fail(
        tmp_path, monkeypatch, publish, rollback):
    from sigint101 import capture_store

    store = _store(tmp_path)
    first = store.save(b"previous", b"{}")
    root_stat = (tmp_path / "captures").stat()
    rename, fsync = capture_store._rename_new, capture_store.fsync_fd
    renames = 0

    def track_rename(*args):
        nonlocal renames
        rename(*args)
        renames += 1

    def fail_parent_sync(fd):
        if renames and os.path.samestat(os.fstat(fd), root_stat):
            if renames == 1:
                raise OSError(publish, "fixture publication sync failed")
            raise OSError(rollback, "fixture rollback sync failed")
        fsync(fd)

    monkeypatch.setattr(capture_store, "_rename_new", track_rename)
    monkeypatch.setattr(capture_store, "fsync_fd", fail_parent_sync)
    with pytest.raises(capture_store.CaptureStorageError) as refused:
        store.save(b"next", b"{}")
    error = refused.value
    assert error.full and str(error).startswith("Capture storage is full: ")
    assert f"[Errno {publish}] fixture publication sync failed" in str(error)
    assert f"[Errno {rollback}] fixture rollback sync failed" in str(error)
    assert renames == 2 and store.catalog() == [first]
