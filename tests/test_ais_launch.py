# ABOUTME: Tests AIS launch and UDP ownership through the console with private loopback ports.
# ABOUTME: Exercises preflight, reception provenance, datagram refusals and supervised teardown.

import errno
import os
import shlex
import signal
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from sigint101 import ais, childproc, receiver_lifecycle
from sigint101.events import read_events
from sigint101.hardware import RtlSdrDevice
from tests.child_fakes import Child
from tests.test_ais import TYPE1, TYPE5_FRAG1, TYPE5_FRAG2
from tests.test_app import _client, _present, _StubSweeper
from tests.test_rtl_ais_native import native as native
from tests.test_rtl_ais_native import wait_for_stderr

HOST = "127.0.0.1"
TOOL = "rtl_ais-sigint101"
MODE = "--sigint101-udp-stdout"
QUERY = "--sigint101-capability"
CAPABILITY = "sigint101-ais-udp-v1"
OWNED = (TYPE1 + "\r\n").encode("ascii")
FOREIGN = (TYPE5_FRAG1 + "\r\n" + TYPE5_FRAG2 + "\r\n").encode("ascii")


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def companion(tmp_path):
    return _script(tmp_path / TOOL, f'[ "$1" = {QUERY} ] && echo {CAPABILITY}\n')


def _decoder(companion, *, host=HOST, port=0):
    return ais.AisDecoder(
        host=host, port=port, spawn=ais._popen, radio_present=lambda: True,
        which=lambda tool: (str(companion) if companion else None) if tool == TOOL else tool,
        list_devices=lambda: [RtlSdrDevice(0, "ais-test-radio")],
    )


def _console(tmp_path, decoder, sweep=None):
    return _client(tmp_path, probe=_present, ais_factory=lambda: decoder,
                   sweeper_factory=lambda: sweep or _StubSweeper([]))


def _wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def _fake_launch(monkeypatch):
    launches = []

    def spawn(argv, **kwargs):
        child = Child(argv)
        launches.append((child, kwargs))
        return child

    monkeypatch.setattr(ais, "spawn", spawn)
    return launches


def _events(tmp_path, kind):
    return [row["payload"] for row in read_events(tmp_path / "events.jsonl") if row["kind"] == kind]


def _badge(client):
    return next(row for row in client.get("/api/stamps").json()["stamps"]
                if row["instrument"] == "ais")["count"]


def test_ais_preflight_preserves_a_running_waterfall(tmp_path, monkeypatch, companion, ais_port):
    launches = _fake_launch(monkeypatch)
    sweep = _StubSweeper([])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port = ais_port
        holder.bind((HOST, port))
        receiver = _decoder(companion, port=port)
        with _console(tmp_path, receiver, sweep) as client:
            assert client.get("/api/spectrum").status_code == 200
            refused = client.get("/api/ais")
            assert refused.status_code == 503
            assert f"UDP port {HOST}:{port}" in refused.json()["reason"]
            assert refused.json()["next_step"] == "Free the port, then retry AIS."
            assert sweep.running and not launches
            holder.setblocking(False)
            with pytest.raises(BlockingIOError):
                holder.recvfrom(4096)
            holder.close()
            assert client.get("/api/ais").status_code == 200
            assert len(launches) == 1 and not sweep.running


def test_ais_launch_refuses_a_port_taken_after_preflight(
    tmp_path, monkeypatch, companion, ais_port,
):
    launches = _fake_launch(monkeypatch)
    port = ais_port
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        class LateBinder(_StubSweeper):
            def release(self):
                if self.running:
                    holder.bind((HOST, port))
                super().release()

        sweep = LateBinder([])
        receiver = _decoder(companion, port=port)
        with _console(tmp_path, receiver, sweep) as client:
            assert client.get("/api/spectrum").status_code == 200
            refused = client.get("/api/ais")
            assert refused.status_code == 503
            assert refused.json()["next_step"] == "Free the port, then retry AIS."
            assert not sweep.running and not launches


def test_ais_launch_rejects_nonloopback_destination(tmp_path, monkeypatch, companion):
    launches = _fake_launch(monkeypatch)
    binds = []
    bind = socket.socket.bind

    def record(sock, address):
        binds.append(address)
        return bind(sock, address)

    monkeypatch.setattr(socket.socket, "bind", record)
    sweep = _StubSweeper([])
    receiver = _decoder(companion, host="0.0.0.0")
    with _console(tmp_path, receiver, sweep) as client:
        assert client.get("/api/spectrum").status_code == 200
        refused = client.get("/api/ais")
        assert refused.status_code == 503
        assert "loopback" in refused.json()["reason"]
        assert sweep.running and not launches and not binds


def test_ais_unowned_datagrams_never_reach_the_assembler(
    tmp_path, monkeypatch, companion, ais_port,
):
    launches = _fake_launch(monkeypatch)
    port = ais_port
    receiver = _decoder(companion, port=port)
    bind = socket.socket.bind
    pushed = []
    push = ais.AisAssembler.push

    def record(assembler, raw):
        pushed.append(raw)
        return push(assembler, raw)

    monkeypatch.setattr(ais.AisAssembler, "push", record)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as foreign:
        foreign.bind((HOST, 0))
        foreign_port = foreign.getsockname()[1]

        def prequeue(sock, address):
            bind(sock, address)
            if address == (HOST, port):
                foreign.sendto(FOREIGN, address)
                foreign.sendto(FOREIGN, address)

        monkeypatch.setattr(socket.socket, "bind", prequeue)
        with _console(tmp_path, receiver) as client:
            assert client.get("/api/ais").status_code == 200
            _wait_for(lambda: pushed or getattr(receiver, "rejections", {}) == {"foreign": 2})
            assert pushed == [] and receiver.vessels() == []
            assert _events(tmp_path, "reception") == [] and _badge(client) == 0

            [(child, kwargs)] = launches
            sender = kwargs["stdout"]
            assert sender.getpeername() == (HOST, port)
            foreign.sendto(FOREIGN, (HOST, port))  # Connected UDP drops this without queueing it.
            sender.send(OWNED)
            _wait_for(lambda: _badge(client) == 1)
            assert pushed == [TYPE1] and [v["mmsi"] for v in receiver.vessels()] == [477553000]
            [record] = _events(tmp_path, "reception")
            assert shlex.split(record["command"]) == child.args == [str(companion), MODE, "-d", "0"]
            assert record["radio"] == {"kind": "rtl-sdr", "serial": "ais-test-radio", "index": 0}
            assert record["freq_hz"] == [161_975_000, 162_025_000]
            receiver.stop()
            assert receiver._run.complete.wait(3)
            assert _events(tmp_path, "ais_foreign_datagrams") == [
                {"count": 2, "source_port": foreign_port},
            ]


@pytest.mark.parametrize(("data", "reason"), [
    (OWNED * 100, "truncated"), (b"wrong\r\n", "framing"),
])
def test_ais_child_datagram_rejection_ends_the_run(
    tmp_path, monkeypatch, companion, ais_port, data, reason,
):
    launches = _fake_launch(monkeypatch)
    port = ais_port
    receiver = _decoder(companion, port=port)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stock_sender:
        with _console(tmp_path, receiver) as client:
            assert client.get("/api/ais").status_code == 200
            [(child, kwargs)] = launches
            sender = kwargs["stdout"]
            if isinstance(sender, socket.socket):
                sender.send(data)
            else:
                stock_sender.sendto(data, (HOST, port))
            run = receiver._run
            assert run.complete.wait(1.5), "a rejected child datagram left the run alive"
            assert child.sent == ["TERM"] and not receiver.running
            assert receiver.rejections == {reason: 1}
            assert _events(tmp_path, "reception") == [] and _badge(client) == 0
            response = client.get("/api/ais")
            assert response.status_code == 503
            assert reason in response.json()["reason"]
            assert "deploy/provision.sh" in response.json()["reason"]


def test_ais_rejection_reason_survives_immediate_child_exit(tmp_path, monkeypatch, companion):
    launches = _fake_launch(monkeypatch)
    receiver = _decoder(companion)
    resume = threading.Event()
    read = receiver._read

    def gated(*args):
        resume.wait(3)
        read(*args)

    receiver._read = gated
    with _console(tmp_path, receiver) as client:
        assert client.get("/api/ais").status_code == 200
        run = receiver._run
        child, kwargs = launches[0]
        try:
            sender = kwargs["stdout"]
            if isinstance(sender, socket.socket):
                sender.send(b"wrong\r\n")
            child.code = 1
            run.wake.set()
            _wait_for(lambda: run.unreported_death)
        finally:
            resume.set()
        assert run.complete.wait(3)
        refused = client.get("/api/ais")
        assert refused.status_code == 503 and "framing" in refused.json()["reason"]


def test_ais_sender_port_stays_reserved_until_natural_drain_finishes(monkeypatch, companion):
    receiver = _decoder(companion)
    children = []
    sender_address = []
    destination = []
    reader_paused, resume = threading.Event(), threading.Event()
    draining = threading.Event()
    heard = []

    def spawn(argv, *, stdout, **_):
        child = Child(argv)
        children.append(child)
        if isinstance(stdout, socket.socket):
            sender = socket.socket(fileno=os.dup(stdout.fileno()))
            destination.append(sender.getpeername())
        else:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.bind((HOST, 0))
            destination.append((HOST, receive_port[0]))
            sender.connect(destination[0])
        sender_address.append(sender.getsockname())
        with sender:
            sender.send(OWNED)
        return child

    receive_port = []
    bind = socket.socket.bind

    def record_bind(sock, address):
        bind(sock, address)
        receive_port.append(sock.getsockname()[1])

    read = receiver._read
    drain_reader = receiver._drain_reader

    def gated(*args):
        reader_paused.set()
        resume.wait()
        return read(*args)

    def record_drain(run):
        # The supervisor calls this after stop_child returns, with the reader still gated.
        draining.set()
        drain_reader(run)

    receiver._read = gated
    receiver._drain_reader = record_drain
    receiver.on_reception = lambda item, **_: heard.append(item["mmsi"])
    monkeypatch.setattr(socket.socket, "bind", record_bind)
    monkeypatch.setattr(ais, "spawn", spawn)
    try:
        receiver.start()
        run = receiver._run
        assert reader_paused.wait(3)
        children[0].code = 1
        run.wake.set()
        _wait_for(lambda: children[0].sent)
        assert draining.wait(3)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
            with pytest.raises(OSError) as exc:
                other.bind(sender_address[0])
            assert exc.value.errno == errno.EADDRINUSE
        assert receive_port.index(sender_address[0][1]) < receive_port.index(destination[0][1])
        resume.set()
        assert run.complete.wait(3) and run.reaped
        assert heard == [477553000]
        for address in [*sender_address, *destination]:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
                other.bind(address)
    finally:
        resume.set()
        receiver.shutdown()


def test_ais_post_spawn_fault_keeps_child_under_supervision(
    tmp_path, monkeypatch, companion, ais_port,
):
    receiver = _decoder(companion, port=ais_port)
    sweep = _StubSweeper([])
    can_reap = threading.Event()
    fault_sockets = []
    launched = []

    class RecoveringChild(Child):
        def kill(self):
            self.unkillable = not can_reap.is_set()
            super().kill()

    def spawn(argv, **kwargs):
        child = RecoveringChild(argv, unkillable=True)
        launched.append((child, kwargs["stdout"]))
        return child

    class BrokenSocket(socket.socket):
        def settimeout(self, timeout):
            if self.fileno() != -1 and self.getsockname() == (HOST, receiver.port):
                fault_sockets.append(self)
                raise OSError("feed timeout setup failed")
            super().settimeout(timeout)

        def close(self):
            if self in fault_sockets and not can_reap.is_set():
                raise OSError("feed socket close failed")
            super().close()

    monkeypatch.setattr(ais, "spawn", spawn)
    monkeypatch.setattr(socket, "socket", BrokenSocket)
    monkeypatch.setattr(childproc, "STOP_WAIT_S", 0.01)
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.03)
    with _console(tmp_path, receiver, sweep) as client:
        try:
            first = client.get("/api/ais")
            assert first.status_code == 503 and fault_sockets
            [(child, sender)] = launched
            run = receiver._run
            assert run is not None, "startup lost the child that still owned the SDR"
            assert run.proc.procs == (child,) and run.reader is None and not run.reaped
            assert "transport could not close" in first.json()["reason"]
            assert sender.fileno() == -1, "sender close was skipped after receiver close failed"
            assert client.get("/api/spectrum").status_code == 503 and not sweep.running
        finally:
            can_reap.set()
            if receiver._run is None:
                for sock in fault_sockets:
                    sock.close()
        assert run.complete.wait(3) and run.reaped
        assert child.sent[0] == "TERM" and set(child.sent[1:]) == {"KILL"} and len(child.sent) >= 3
        assert fault_sockets[0].fileno() == -1
        assert client.get("/api/spectrum").status_code == 200 and sweep.running


def test_ais_missing_companion_refuses_before_stand_down(tmp_path, monkeypatch, companion):
    launches = _fake_launch(monkeypatch)
    sweep = _StubSweeper([])
    receiver = _decoder(None)
    with _console(tmp_path, receiver, sweep) as client:
        assert client.get("/api/spectrum").status_code == 200
        refused = client.get("/api/ais")
        assert refused.status_code == 503 and refused.json()["cause"] == "missing_tool"
        assert TOOL in refused.json()["reason"]
        assert refused.json()["next_step"] == "Run deploy/provision.sh"
        assert sweep.running and not launches
        receiver._which = lambda _: str(companion)
        assert client.get("/api/ais").status_code == 200
        assert len(launches) == 1 and not sweep.running


def test_ais_command_strip_shows_a_shell_command(tmp_path, monkeypatch, companion, ais_port):
    launches = _fake_launch(monkeypatch)
    receiver = _decoder(companion, port=ais_port)
    with _console(tmp_path, receiver) as client:
        response = client.get("/api/ais")
        assert response.status_code == 200
        assert shlex.split(response.json()["command"]) == [
            "rtl_ais", "-d", "0", "-h", HOST, "-P", str(receiver.port),
        ]
        [(child, kwargs)] = launches
        assert child.args == [str(companion), MODE, "-d", "0"]
        assert kwargs["stdin"] == subprocess.DEVNULL and isinstance(kwargs["stdout"], socket.socket)


@pytest.mark.parametrize("recovery", ["same-binary", "replacement"])
def test_ais_timed_out_capability_is_killed_reaped_and_retried(
    tmp_path, monkeypatch, companion, recovery,
):
    launches = _fake_launch(monkeypatch)
    pid_log = tmp_path / "queries"
    ready = tmp_path / "ready"
    executable = _script(tmp_path / "slow-companion", f'echo $$ >> "{pid_log}"\n'
                         f'if [ -f "{ready}" ]; then echo {CAPABILITY}; exit; fi\n'
                         'while :; do :; done\n')
    receiver = _decoder(executable)
    sweep = _StubSweeper([])
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(ais, "time", SimpleNamespace(monotonic=lambda: clock.now, time=time.time))
    monkeypatch.setattr(ais, "AIS_CAPABILITY_TIMEOUT_S", 0.5, raising=False)
    monkeypatch.setattr(ais, "AIS_CAPABILITY_RETRY_S", 30.0, raising=False)

    def denied(client):
        result = client.get("/api/ais")
        assert result.status_code == 503
        assert QUERY in result.json()["reason"] and "timed out" in result.json()["reason"]

    def queries():
        return pid_log.read_text(encoding="utf-8").splitlines()

    with _console(tmp_path, receiver, sweep) as client:
        assert client.get("/api/spectrum").status_code == 200
        denied(client)
        clock.now = 29.0
        denied(client)
        assert sweep.running and not launches
        [pid] = queries()
        with pytest.raises(ChildProcessError):
            os.waitpid(int(pid), os.WNOHANG)
        if recovery == "replacement":
            companion.replace(executable)
        else:
            clock.now = 30.0
            denied(client)
            logged = queries()
            assert len(logged) == 2, "the same executable was not queried after the cooldown"
            first, second = logged
            assert first == pid
            with pytest.raises(ChildProcessError):
                os.waitpid(int(second), os.WNOHANG)
            ready.touch()
            clock.now = 59.0
            denied(client)
            assert queries() == [first, second] and sweep.running and not launches
            clock.now = 60.0
        assert client.get("/api/ais").status_code == 200 and len(launches) == 1
        assert len(queries()) == (3 if recovery == "same-binary" else 1)


@pytest.mark.parametrize("fault", ["exec-busy", "signalled"])
def test_ais_capability_transient_query_failure_is_not_cached(monkeypatch, companion, fault):
    receiver = _decoder(companion)

    def fail(argv, **_kwargs):
        if fault == "exec-busy":
            raise OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))
        return subprocess.CompletedProcess(argv, -signal.SIGKILL, stdout=b"")

    with monkeypatch.context() as m:
        m.setattr(ais.subprocess, "run", fail)
        with pytest.raises(ais.AisUnavailable):
            receiver._companion()
    assert receiver._companion() == str(companion)


@pytest.mark.parametrize("query_result", ["wrong-reply", "nonzero", errno.ENOENT, errno.EACCES])
def test_ais_capability_caches_definitive_refusals(monkeypatch, companion, query_result):
    calls = []

    def incompatible(argv, **_kwargs):
        calls.append(argv)
        if isinstance(query_result, int):
            raise OSError(query_result, os.strerror(query_result))
        if query_result == "wrong-reply":
            return subprocess.CompletedProcess(argv, 0, stdout=b"wrong-version\n")
        return subprocess.CompletedProcess(argv, 1, stdout=ais.AIS_CAPABILITY)

    monkeypatch.setattr(ais.subprocess, "run", incompatible)
    receiver = _decoder(companion)
    for _ in range(2):
        with pytest.raises(ais.AisUnavailable):
            receiver._companion()
    assert calls == [[str(companion), QUERY]]


def test_ais_stop_sends_sigterm_to_a_companion_still_sending(monkeypatch, native):
    receiver = _decoder(native)
    started = []
    output = []

    def spawn(argv, **kwargs):
        child = childproc.spawn([str(native), *argv[1:]], stdout=kwargs["stdout"],
                                stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        started.append(child)
        terminate = child.terminate

        def after_closed():
            if child.poll() is not None:
                return
            # A blocked recvmsg can still hold the closed receiver in the kernel.
            reader = receiver._run.reader
            reader.join(timeout=3)
            assert not reader.is_alive()
            child.stdin.write(b"N")
            child.stdin.flush()
            output.append(wait_for_stderr(child, b"stub: emitted\n"))
            terminate()

        child.terminate = after_closed
        return child

    monkeypatch.setattr(ais, "spawn", spawn)
    try:
        receiver.start()
        [child] = started
        assert child.args[1] == MODE, "stop test must exercise the inherited socket"
        wait_for_stderr(child, b"stub: reading\n")
        receiver.stop()
        assert receiver._run.complete.wait(3) and receiver._run.reaped
        assert child.returncode == 0
        assert len(output) == 1 and b"stub: ECONNREFUSED" in output[0]
    finally:
        for child in started:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
        receiver.shutdown()
        for child in started:
            child.stdin.close()
