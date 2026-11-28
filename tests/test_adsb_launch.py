# ABOUTME: Exercises ADS-B startup and its fixed SBS endpoint without opening a radio.
# ABOUTME: Checks process ownership, SDR handoff, and reception records through the console.

import errno
import os
import select
import shlex
import socket
import subprocess
import sys
import threading
from contextlib import ExitStack, contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from sigint101 import adsb, childproc, receiver_lifecycle
from sigint101.events import read_events
from tests.child_fakes import Child
from tests.test_adsb import POSITION
from tests.test_app import _client, _present, _StubSweeper


def _decoder():
    return adsb.AdsbDecoder(device=2, serial="adsb-test", radio_present=lambda: True,
                            which=lambda tool: tool)


def _records(path):
    return [event["payload"] for event in read_events(path / "events.jsonl")
            if event["kind"] == "reception"]


@pytest.mark.parametrize("fault", ["readiness-poll", "socket-setup-and-close"])
def test_adsb_post_spawn_fault_retains_child_until_reaped(tmp_path, monkeypatch, fault):
    can_reap = threading.Event()
    fault_raised = threading.Event()
    launches = []

    class RecoveringChild(Child):
        def poll(self):
            if fault == "readiness-poll" and not fault_raised.is_set():
                fault_raised.set()
                raise OSError("readiness poll failed")
            return super().poll()

        def kill(self):
            self.unkillable = not can_reap.is_set()
            super().kill()

    class BrokenSocket:
        closed = False

        def settimeout(self, _timeout):
            fault_raised.set()
            raise OSError("socket timeout setup failed")

        def shutdown(self, _how):
            pass

        def close(self):
            if not can_reap.is_set():
                raise OSError("socket close failed")
            self.closed = True

        def fileno(self):
            return -1 if self.closed else 1

    proc = RecoveringChild(unkillable=True)
    sock = BrokenSocket()

    def spawn(argv, **kwargs):
        proc.args = argv
        launches.append(proc)
        return proc

    def connect(*args, **kwargs):
        if fault == "readiness-poll":
            raise ConnectionRefusedError("socket not listening")
        return sock

    monkeypatch.setattr(adsb, "spawn", spawn)
    monkeypatch.setattr(adsb, "_sbs_peer", lambda _: nullcontext(lambda _: True), raising=False)
    monkeypatch.setattr(adsb.socket, "create_connection", connect)
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.01)
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.02)
    receiver = _decoder()
    retry_reap = receiver._retry_reap

    def retry_after_probes(run, timeout):
        # Hold the startup refusal steady until the handoff assertions have sampled it.
        # Then let the periodic retry demonstrate recovery with the same child and socket.
        if can_reap.wait(5):
            retry_reap(run, timeout)

    monkeypatch.setattr(receiver, "_retry_reap", retry_after_probes)
    sweep = _StubSweeper([])
    with _client(tmp_path, probe=_present, adsb_factory=lambda: receiver,
                 sweeper_factory=lambda: sweep) as client:
        try:
            first = client.get("/api/adsb")
            assert first.status_code == 503 and fault_raised.is_set()
            run = receiver._run
            assert run is not None, "startup lost the child that still owned the SDR"
            assert run.proc.procs == (proc,)
            assert run.reader is None and not run.reaped
            assert first.json()["reason"] == run.failure
            assert ("transport could not close" if fault == "socket-setup-and-close"
                    else "survived kill") in run.failure
            assert client.get("/api/adsb").status_code == 503
            competing = client.get("/api/spectrum")
            assert competing.status_code == 503 and not sweep.running
            assert competing.json()["reason"] == run.failure
            assert launches == [proc] and _records(tmp_path) == []
        finally:
            can_reap.set()
        assert run.complete.wait(3) and run.reaped
        assert proc.sent[0] == "TERM" and set(proc.sent[1:]) == {"KILL"}
        assert len(proc.sent) >= 3
        assert sock.closed is (fault == "socket-setup-and-close")
        assert client.get("/api/spectrum").status_code == 200 and sweep.running


# Stream data is a checked-in test fixture. The stand-ins do not execute dump1090 or use RF.
_FOREIGN = (POSITION.replace("A1B2C3", "A0A0A0") + "\n").encode("ascii")
_OWNED = (POSITION.replace("A1B2C3", "B0B0B0") + "\n").encode("ascii")

_SERVE_SBS = """
import socket
import sys

port, dual_stack, payload = sys.argv[1:]
family = socket.AF_INET6 if dual_stack == "True" else socket.AF_INET
with socket.socket(family) as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    server.bind(("::" if family == socket.AF_INET6 else "127.0.0.1", int(port)))
    server.listen()
    print("ready", flush=True)
    with server.accept()[0] as peer:
        peer.sendall(bytes.fromhex(payload))
        while peer.recv(1):
            pass
    sys.stdin.buffer.read()
"""


@contextmanager
def _stand_in(script, *args, pass_fds=(), ready=False):
    proc = childproc.GroupPopen(
        [sys.executable, "-c", script, *map(str, args)], start_new_session=True, pass_fds=pass_fds,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        if ready:
            assert select.select([proc.stdout], [], [], 3)[0], "child did not bind"
            assert proc.stdout.readline() == b"ready\n"
        yield proc
    finally:
        childproc.stop_process(proc)
        proc.stdin.close()
        proc.stdout.close()


def _listen(port):
    server = socket.socket()
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen()
        return server
    except BaseException:
        server.close()
        raise


def _short_startup(monkeypatch):
    monkeypatch.setattr(adsb, "SBS_STARTUP_S", 1.5, raising=False)


def _trace_connections(monkeypatch):
    opened = []
    connect = socket.create_connection

    def dial(*args, **kw):
        peer = connect(*args, **kw)
        opened.append(peer)
        return peer

    monkeypatch.setattr(adsb.socket, "create_connection", dial)
    return opened


def _stamp_count(client):
    return next(row["count"] for row in client.get("/api/stamps").json()["stamps"]
                if row["instrument"] == "adsb")


def _assert_no_reception(client, tmp_path, body):
    assert body["aircraft"] == [] and body["command"] is None
    assert _records(tmp_path) == [] and _stamp_count(client) == 0


def _assert_owned_reception(client, receiver, child, delivered, tmp_path):
    assert delivered.wait(3), "the managed child did not deliver its aircraft"
    assert [plane["hex_ident"] for plane in receiver.aircraft()] == ["B0B0B0"]
    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["item"]["hex_ident"] == "B0B0B0"
    assert records[0]["command"] == shlex.join(child.args)
    assert records[0]["radio"] == {"kind": "rtl-sdr", "index": 2, "serial": "adsb-test"}
    assert records[0]["freq_hz"] == 1_090_000_000
    assert _stamp_count(client) == 1


def _observe_receptions(receiver):
    delivered = threading.Event()
    hook = receiver.on_reception

    def receive(item, *, command, freq_hz, radio):
        hook(item, command=command, freq_hz=freq_hz, radio=radio)
        delivered.set()

    receiver.on_reception = receive
    return delivered


def _launch_argv(monkeypatch):
    seen = []

    def spawn(argv, **kwargs):
        seen.extend(argv)
        return Child(argv, code=1)

    monkeypatch.setattr(adsb, "spawn", spawn)
    monkeypatch.setattr(adsb, "_sbs_peer", lambda _: nullcontext(lambda _: True), raising=False)
    with pytest.raises(adsb.AdsbUnavailable):
        _decoder().start()
    return seen


def test_adsb_launch_disables_raw_and_beast_inputs_on_loopback(monkeypatch):
    seen = _launch_argv(monkeypatch)
    assert "--net" in seen and "--net-only" not in seen
    switches = dict(zip(seen[1:], seen[2:]))
    assert {flag: switches.get(flag) for flag in (
        "--net-bind-address", "--net-ri-port", "--net-bi-port"
    )} == {"--net-bind-address": "127.0.0.1", "--net-ri-port": "0", "--net-bi-port": "0"}


def test_adsb_disables_unused_output_ports(monkeypatch):
    checked = []
    bind = socket.socket.bind

    def record_bind(sock, address):
        bind(sock, address)
        checked.append(address)

    monkeypatch.setattr(socket.socket, "bind", record_bind)
    argv = _launch_argv(monkeypatch)
    switches = dict(zip(argv[1:], argv[2:]))
    assert switches["--net-ro-port"] == switches["--net-bo-port"] == "0"
    assert switches["--net-sbs-port"] == str(adsb.SBS_PORT)
    assert checked == [("127.0.0.1", adsb.SBS_PORT)]


@pytest.mark.parametrize(("bind_after", "status"), [(1.6, 200), (5.1, 503)],
                         ids=["bind-within-spawn-budget", "bind-after-spawn-budget"])
def test_adsb_slow_stand_down_preserves_spawn_budget(tmp_path, monkeypatch, bind_after, status):
    now = 0.0
    spawned_at = []

    def advance(seconds):
        nonlocal now
        now += seconds

    class SlowSweeper(_StubSweeper):
        def release(self):
            if self.running:
                advance(3.5)
            super().release()

    def spawn(argv, **_kwargs):
        assert not sweep.running
        spawned_at.append(now)
        return Child(argv)

    def connect(address, *, timeout):
        assert address == ("127.0.0.1", adsb.SBS_PORT)
        if now - spawned_at[0] < bind_after:
            raise ConnectionRefusedError("the stand-in is still binding")
        return client_socket

    clock = SimpleNamespace(monotonic=lambda: now, sleep=advance, time=adsb.time.time)
    monkeypatch.setattr(adsb, "time", clock)
    monkeypatch.setattr(adsb, "spawn", spawn)
    monkeypatch.setattr(adsb.socket, "create_connection", connect)
    monkeypatch.setattr(adsb, "_sbs_peer", lambda _: nullcontext(lambda _: True), raising=False)
    sweep = SlowSweeper([])
    receiver = _decoder()
    with ExitStack() as sockets:
        client_socket, peer = socket.socketpair()
        sockets.enter_context(client_socket)
        sockets.enter_context(peer)
        with _client(tmp_path, probe=_present, adsb_factory=lambda: receiver,
                     sweeper_factory=lambda: sweep) as console:
            assert console.get("/api/spectrum").status_code == 200
            assert sweep.running
            reply = console.get("/api/adsb")
            assert spawned_at == [3.5]
            assert reply.status_code == status, reply.json()
            if status == 503:
                assert "did not open its SBS port" in reply.json()["reason"]
                assert now == pytest.approx(8.5)


def test_adsb_refuses_an_occupied_sbs_port_before_spawning(tmp_path, monkeypatch, adsb_ports):
    launches = []

    def spawn(argv, **kwargs):
        child = Child(argv, code=1)
        launches.append(child)
        return child

    monkeypatch.setattr(adsb, "spawn", spawn)
    receiver = _decoder()
    sweep = _StubSweeper([])
    with ExitStack() as children, _client(
        tmp_path, probe=_present, adsb_factory=lambda: receiver, sweeper_factory=lambda: sweep,
    ) as client:
        assert client.get("/api/spectrum").status_code == 200 and sweep.running
        with _listen(adsb_ports) as listener:
            response = client.get("/api/adsb")
            assert response.status_code == 503
            assert "already in use" in response.json()["reason"]
            assert f"127.0.0.1:{adsb_ports}" in response.json()["reason"]
            assert response.json()["next_step"] == "Free the port, then retry ADS-B."
            assert launches == [] and sweep.running
            assert not select.select([listener], [], [], 0)[0]
            _assert_no_reception(client, tmp_path, response.json())

        def start_child(argv, **kwargs):
            child = children.enter_context(_stand_in(_SERVE_SBS, adsb.SBS_PORT, False, _OWNED.hex(),
                                                      ready=True))
            launches.append(child)
            return child

        monkeypatch.setattr(adsb, "spawn", start_child)
        response = client.get("/api/adsb")
        assert response.status_code == 200 and len(launches) == 1


def test_adsb_reports_dump1090_stderr_when_a_bind_fails_after_preflight(tmp_path, monkeypatch,
                                                                     adsb_ports):
    error = (f"Error opening the listening port {adsb_ports} (Basestation TCP output): "
             "Address already in use")

    def spawn(argv, **kwargs):
        return Child(argv, code=1, said=error)

    monkeypatch.setattr(adsb, "spawn", spawn)
    monkeypatch.setattr(adsb, "_sbs_peer", lambda _: nullcontext(lambda _: True), raising=False)
    receiver = _decoder()
    with _client(tmp_path, probe=_present, adsb_factory=lambda: receiver) as client:
        response = client.get("/api/adsb")
        assert response.status_code == 503 and error in response.json()["reason"]
        assert receiver._run.complete.wait(3) and receiver._run.reaped
        _assert_no_reception(client, tmp_path, response.json())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux socket ownership proof")
@pytest.mark.parametrize("owner", ["foreign-binder", "shared-listener"])
def test_adsb_rejects_an_unowned_sbs_connection_after_preflight(tmp_path, monkeypatch, owner):
    _short_startup(monkeypatch)
    opened = _trace_connections(monkeypatch)
    receiver = _decoder()
    with ExitStack() as children, _client(
        tmp_path, probe=_present, adsb_factory=lambda: receiver,
    ) as client:
        def spawn(argv, **kwargs):
            listener = children.enter_context(_listen(adsb.SBS_PORT))
            inherited = (listener.fileno(),) if owner == "shared-listener" else ()
            child = children.enter_context(_stand_in("import sys; sys.stdin.buffer.read()",
                                                      pass_fds=inherited))

            served = threading.Event()

            def serve():
                with listener.accept()[0] as peer:
                    peer.sendall(_FOREIGN)
                    served.set()
                    while peer.recv(1):
                        pass

            listener.settimeout(3)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            children.callback(thread.join, 3)
            spawn.child = child
            spawn.served = served
            return child

        monkeypatch.setattr(adsb, "spawn", spawn)
        refused = client.get("/api/adsb")
        assert refused.status_code == 503
        assert "verify" in refused.json()["reason"]
        assert spawn.served.is_set(), "the unowned peer never sent its fixture line"
        assert receiver._run.complete.wait(3) and receiver._run.reaped
        assert spawn.child.poll() is not None
        assert len(opened) == 1 and opened[0].fileno() == -1
        _assert_no_reception(client, tmp_path, refused.json())
        children.close()

        def restart(argv, **kwargs):
            restart.child = children.enter_context(_stand_in(
                _SERVE_SBS, adsb.SBS_PORT, False, _OWNED.hex(), ready=True))
            return restart.child

        monkeypatch.setattr(adsb, "spawn", restart)
        delivered = _observe_receptions(receiver)
        available = client.get("/api/adsb")
        assert available.status_code == 200
        _assert_owned_reception(client, receiver, restart.child, delivered, tmp_path)


@pytest.mark.skipif(sys.platform != "linux" or not hasattr(os, "pidfd_open"), reason="Linux pidfd")
@pytest.mark.parametrize("fault", ["unreadable-fds", "reused-pid"])
def test_adsb_refuses_when_peer_identity_cannot_be_verified(tmp_path, monkeypatch, fault):
    opened = _trace_connections(monkeypatch)
    _short_startup(monkeypatch)
    matched = threading.Event()
    real_has_socket = getattr(adsb, "_child_has_socket", None)
    real_open = os.pidfd_open
    real_close = os.close
    child = None
    pidfds = []
    closed = []

    def open_pidfd(pid, flags=0):
        fd = real_open(pid, flags)
        if child is not None and pid == child.pid:
            pidfds.append(fd)
        return fd

    def close(fd):
        real_close(fd)
        if fd in pidfds and fd not in closed:
            closed.append(fd)

    def lookup(pid, inode):
        if real_has_socket(pid, inode):
            matched.set()
            if fault == "unreadable-fds":
                raise PermissionError(errno.EACCES, "cannot inspect child socket fds")
            return True
        return False

    monkeypatch.setattr(os, "pidfd_open", open_pidfd)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(adsb, "_child_has_socket", lookup, raising=False)
    # asyncio can open a pidfd with explicit flags before the receiver spawns its child.
    os.close(os.pidfd_open(os.getpid(), 0))
    assert not pidfds and not closed
    if fault == "reused-pid":
        # Both kernel observations can match a reused numeric PID. Only the original pidfd
        # says that the launch whose identity those observations were meant to prove has exited.
        monkeypatch.setattr(adsb, "_pidfd_exited", lambda _fd: matched.is_set(), raising=False)

    receiver = _decoder()
    with ExitStack() as children, _client(
        tmp_path, probe=_present, adsb_factory=lambda: receiver,
    ) as client:
        def spawn(argv, **kwargs):
            nonlocal child
            child = children.enter_context(_stand_in(
                _SERVE_SBS, adsb.SBS_PORT, False, _OWNED.hex(), ready=True))
            return child

        monkeypatch.setattr(adsb, "spawn", spawn)
        response = client.get("/api/adsb")
        assert response.status_code == 503
        assert matched.is_set()
        reason = response.json()["reason"]
        assert ("cannot inspect child socket fds" if fault == "unreadable-fds"
                else "exited during SBS ownership verification") in reason
        assert receiver._run.complete.wait(3) and receiver._run.reaped
        assert len(pidfds) == 1 and closed == pidfds
        assert child.poll() is not None
        assert len(opened) == 1 and opened[0].fileno() == -1
        _assert_no_reception(client, tmp_path, response.json())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux socket ownership proof")
@pytest.mark.parametrize("case", ["inode-before-fd", "dual-stack", "pidfd-fallback"])
def test_adsb_waits_for_owned_peer_and_keeps_its_provenance(tmp_path, monkeypatch, case):
    inspected = []
    real_has_socket = getattr(adsb, "_child_has_socket", None)
    child = None

    def lookup(pid, inode):
        if case == "pidfd-fallback":
            assert child._waitpid_lock.locked()
        owns = real_has_socket(pid, inode)
        if owns:
            inspected.append((pid, inode))
            if case == "inode-before-fd" and len(inspected) == 1:
                return False  # The kernel has published the inode, accept has yet to install fd.
        return owns

    monkeypatch.setattr(adsb, "_child_has_socket", lookup, raising=False)
    real_open = getattr(os, "pidfd_open", None)
    if case == "pidfd-fallback" and real_open is not None:
        def no_pidfd(pid, flags=0):
            if child is not None and pid == child.pid:
                raise OSError(errno.ENOSYS, "pidfd unavailable")
            return real_open(pid, flags)

        monkeypatch.setattr(os, "pidfd_open", no_pidfd)
        with pytest.raises(OSError) as original:
            real_open(-1, 0)
        with pytest.raises(OSError) as delegated:
            os.pidfd_open(-1, 0)
        assert delegated.value.errno == original.value.errno
    if case == "dual-stack":
        try:
            with socket.socket(socket.AF_INET6) as probe:
                probe.bind(("::", 0))
        except OSError:
            pytest.skip("dual-stack loopback unavailable")

    receiver = _decoder()
    with ExitStack() as children, _client(
        tmp_path, probe=_present, adsb_factory=lambda: receiver,
    ) as client:
        def spawn(argv, **kwargs):
            nonlocal child
            child = children.enter_context(_stand_in(
                _SERVE_SBS, adsb.SBS_PORT, case == "dual-stack", _OWNED.hex(), ready=True))
            return child

        monkeypatch.setattr(adsb, "spawn", spawn)
        delivered = _observe_receptions(receiver)
        response = client.get("/api/adsb")
        assert response.status_code == 200
        assert len(inspected) == (2 if case == "inode-before-fd" else 1)
        assert len(set(inspected)) == 1
        _assert_owned_reception(client, receiver, child, delivered, tmp_path)
