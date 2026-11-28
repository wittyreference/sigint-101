# ABOUTME: Builds the pinned AIS companion with cc and a USB-free librtlsdr stub.
# ABOUTME: Exercises CLI validation, datagram boundaries, failed sends and SIGTERM.

import os
import select
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STUB = Path(__file__).with_name("rtl_ais_native").resolve()
PIN = "d84764bd20d93de68d3275e4914f897adfb353cc"
UPSTREAM = "https://github.com/dgiardini/rtl-ais"
PATCH = ROOT / "deploy/patches/rtl-ais-companion.patch"
MODE = "--sigint101-udp-stdout"
QUERY = "--sigint101-capability"
CAPABILITY = b"sigint101-ais-udp-v1\n"
SINGLE = b"!AIVDM,1,1,,A,fixture,0*00\r\n"
MULTIPART = b"!AIVDM,2,1,1,A,first,0*00\r\n!AIVDM,2,2,1,A,last,2*00\r\n"


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    work = tmp_path_factory.mktemp("rtl-ais-native")
    if supplied := os.environ.get("SIGINT101_RTL_AIS_SOURCE"):
        source = Path(supplied).resolve()
    else:
        # Fetch the same immutable source pin as provision.sh. Provision passes its
        # already patched checkout so the check there needs no second fetch.
        source = work / "source"
        source.mkdir()
        for args in (["init", "-q"], ["fetch", "-q", "--depth", "1", UPSTREAM, PIN],
                     ["checkout", "-q", "--detach", "FETCH_HEAD"]):
            fetched = subprocess.run(["git", *args], cwd=source, capture_output=True, timeout=30)
            assert fetched.returncode == 0, fetched.stderr.decode()
        if PATCH.is_file():
            patched = subprocess.run(["patch", "--batch", "--forward", "-p1", "-i", PATCH],
                                     cwd=source, capture_output=True, timeout=10)
            assert patched.returncode == 0, patched.stdout + patched.stderr
    exe = work / "rtl_ais"
    files = ["main.c", "rtl_ais.c", "convenience.c", "aisdecoder/aisdecoder.c",
             "aisdecoder/sounddecoder.c", "aisdecoder/lib/receiver.c", "aisdecoder/lib/protodec.c",
             "aisdecoder/lib/hmalloc.c", "aisdecoder/lib/filter.c", "tcp_listener/tcp_listener.c"]
    compiled = subprocess.run([
        "cc", "-O1", "-g", "-I", STUB, "-I", "aisdecoder", "-I", "aisdecoder/lib",
        "-pthread", *files, STUB / "stub.c", "-Wl,--wrap=send", "-lm", "-o", exe,
    ], cwd=source, capture_output=True, timeout=45)
    assert compiled.returncode == 0, compiled.stderr.decode()
    return exe


@contextmanager
def feeds():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        sender.bind(("127.0.0.1", 0))
        receiver.bind(("127.0.0.1", 0))
        sender.connect(receiver.getsockname())
        receiver.settimeout(2)
        yield sender, receiver


@contextmanager
def native_child(exe, sender, *args):
    with subprocess.Popen([exe, MODE, "-d", "0", *args], stdin=subprocess.PIPE,
                          stdout=sender, stderr=subprocess.PIPE, bufsize=0) as child:
        try:
            yield child
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)


def wait_for_stderr(child, marker):
    deadline = time.monotonic() + 3
    output = b""
    while marker not in output:
        left = max(0, deadline - time.monotonic())
        ready, _, _ = select.select([child.stderr], [], [], left)
        assert ready, output
        chunk = os.read(child.stderr.fileno(), 4096)
        assert chunk, output
        output += chunk
    return output


def test_rtl_ais_companion_preserves_datagram_framing(native):
    with feeds() as (sender, receiver), native_child(native, sender, "-n") as child:
        ready = wait_for_stderr(child, b"stub: reading\n")
        assert b"stub: device search\n" in ready
        assert b"stub: open\n" in ready
        assert b"AIS data will be sent to" not in ready
        child.stdin.write(b"N")
        assert receiver.recv(4096) == SINGLE
        assert receiver.recv(4096) == MULTIPART
        assert SINGLE in wait_for_stderr(child, b"stub: emitted\n")
        receiver.setblocking(False)
        with pytest.raises(BlockingIOError):
            receiver.recv(4096)
        child.terminate()
        assert child.wait(timeout=3) == 0, child.stderr.read()
        assert child.stderr.read().endswith(b"stub: reader exited\nstub: close\n")


def test_rtl_ais_companion_drops_failed_sends_before_sigterm(native):
    with feeds() as (sender, receiver), native_child(native, sender) as child:
        wait_for_stderr(child, b"stub: reading\n")
        receiver.close()
        child.stdin.write(b"N")
        assert b"stub: ECONNREFUSED\n" in wait_for_stderr(child, b"stub: emitted\n")
        assert child.poll() is None
        child.terminate()
        assert child.wait(timeout=3) == 0, child.stderr.read()
        output = child.stderr.read()
        assert b"Signal caught, exiting!" in output
        assert output.endswith(b"stub: reader exited\nstub: close\n")


@pytest.mark.parametrize(("trigger", "reason"), [
    (b"X", b"Bad file descriptor"), (b"P", b"Operation not permitted"),
])
def test_rtl_ais_companion_reports_fatal_send_error(native, trigger, reason):
    with feeds() as (sender, _), native_child(native, sender) as child:
        wait_for_stderr(child, b"stub: reading\n")
        child.stdin.write(trigger)
        assert child.wait(timeout=3) == 1
        output = child.stderr.read()
        assert output.count(b"AIS output socket failed: ") == 1
        assert reason in output


@pytest.mark.parametrize("trigger", [b"A", b"B", b"I"])
def test_rtl_ais_companion_recovers_from_transient_send_errors(native, trigger):
    with feeds() as (sender, receiver), native_child(native, sender) as child:
        wait_for_stderr(child, b"stub: reading\n")
        child.stdin.write(trigger)
        assert b"AIS output socket failed" not in wait_for_stderr(child, b"stub: emitted\n")
        assert child.poll() is None
        receiver.setblocking(False)
        with pytest.raises(BlockingIOError):
            receiver.recv(4096)
        receiver.settimeout(2)
        child.stdin.write(b"N")
        assert receiver.recv(4096) == SINGLE and receiver.recv(4096) == MULTIPART
        child.terminate()
        assert child.wait(timeout=3) == 0, child.stderr.read()


@pytest.mark.parametrize("output", ["pipe", "unconnected", "nonloopback", "conflicting"])
def test_rtl_ais_companion_validates_output_before_device_search(native, output):
    with feeds() as (sender, _), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
        args = [native, MODE, "-d", "0"]
        if output == "nonloopback":
            other.connect(("192.0.2.1", 9))  # Connect only. No packet is sent.
        if output == "conflicting":
            args.extend(["-P", "0"])
        stdout = {"pipe": subprocess.PIPE, "unconnected": other, "nonloopback": other,
                  "conflicting": sender}[output]
        run = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=stdout,
                             stderr=subprocess.PIPE, timeout=3)
    assert run.returncode == 2 and MODE.encode() in run.stderr
    assert b"stub:" not in run.stderr


def test_rtl_ais_capability_precedes_option_parsing(native):
    run = subprocess.run([native, "-d", "0", QUERY], stdin=subprocess.DEVNULL,
                         capture_output=True, timeout=3)
    assert (run.returncode, run.stdout, run.stderr) == (0, CAPABILITY, b"")
