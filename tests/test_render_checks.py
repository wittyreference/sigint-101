# ABOUTME: Tests render tools' verdicts and behavior when Chrome disconnects.
# ABOUTME: A fake DevTools endpoint stands in for Chrome; the scripts run as real processes.

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCROLL = REPO / "tools" / "render" / "render_scroll.py"
FIGURE = REPO / "tools" / "render" / "render_figure.py"
FIT = REPO / "tools" / "render" / "render_fit.py"

# Runs a script as __main__ with time.sleep made instant: the checks wait seconds for a real page
# to settle, and the fake answers at once. Nothing else changes, so the process's exit status is
# the script's own. Two tests below run the scripts plainly, sleeps and all.
FAST = (
    "import runpy, sys, time\n"
    "time.sleep = lambda seconds: None\n"
    "script = sys.argv.pop(1)\n"
    "runpy.run_path(script, run_name='__main__')\n"
)

# Fit uses a fixed Chrome port. Run its main against the fake's ephemeral port.
FIT_ON_FAKE_PORT = (
    "import os, runpy, sys\n"
    "script = sys.argv.pop(1)\n"
    "main = runpy.run_path(script)['main']\n"
    "main.__globals__['PORT'] = int(os.environ['CDP_PORT'])\n"
    "main()\n"
)

# An expression that throws in the page: Chrome answers with exceptionDetails and no value.
THROWS = object()

# A piece of each measuring expression, to tell them apart.
EXPLAIN = 'data-stage="explain"'
HOME = "querySelector('.tiles')"
CARD = "exam-figure__open"
OVERLAY = "exam-figure:target"

EXPLAIN_OK = {"hasBody": True, "overflowY": "auto", "scrollH": 900, "clientH": 400,
              "scrollable": True, "hasCompleteBtn": True, "btnBottom": 470, "viewportH": 480}
HOME_OK = {"overflowY": "auto", "gridAutoRows": "auto", "tiles": 12, "tilesWithClippedMeta": 0,
           "clipped": [], "scrollH": 900, "clientH": 400, "viewportH": 480}
CARD_OK = {"theme": "dark", "commitBottom": 470, "openW": 200, "openH": 80, "docScroll": 480,
           "vh": 480}
OVERLAY_OK = {"imgW": 700, "imgH": 400, "barBottom": 470, "barH": 40, "loaded": 1200}


def _ws_frame(payload):
    """A server's websocket text frame: unmasked, as Chrome sends them."""
    head = bytearray([0x81])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + payload


class FakeDevTools:
    """Chrome's DevTools endpoint on 127.0.0.1, as far as the render checks use it.

    `answers` maps a piece of an evaluated expression to the value the page gives back, or to
    THROWS. document.readyState is always "complete"; any other expression gives no value.
    `disconnect_at` can end the connection during the handshake or the first reply frame.
    """

    def __init__(self, answers, *, disconnect_at=None):
        self.answers = answers
        self.disconnect_at = disconnect_at
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        finally:
            self.server.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            head, rest = head.split(b"\r\n\r\n", 1)
            path = head.split(b" ", 2)[1].decode()
            if b"upgrade: websocket" in head.lower():
                if self.disconnect_at == "handshake":
                    return
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                             b"Connection: Upgrade\r\n\r\n")
                self._websocket(conn, rest)
                return
            if path == "/json/version":
                url = f"ws://127.0.0.1:{self.port}/devtools/browser/fake"
                body = json.dumps({"webSocketDebuggerUrl": url}).encode()
                status = b"200 OK"
            elif path == "/json":
                url = f"ws://127.0.0.1:{self.port}/devtools/page/fake"
                body = json.dumps([{"type": "page", "webSocketDebuggerUrl": url}]).encode()
                status = b"200 OK"
            else:
                body, status = b"{}", b"404 Not Found"
            conn.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)

    def _websocket(self, conn, buf):
        def need(n):
            nonlocal buf
            while len(buf) < n:
                chunk = conn.recv(65536)
                if not chunk:
                    raise EOFError
                buf += chunk

        try:
            while True:
                need(2)
                n, off = buf[1] & 0x7F, 2
                if n == 126:
                    need(4)
                    n, off = struct.unpack(">H", buf[2:4])[0], 4
                elif n == 127:
                    need(10)
                    n, off = struct.unpack(">Q", buf[2:10])[0], 10
                need(off + 4 + n)
                mask = buf[off:off + 4]
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(buf[off + 4:off + 4 + n]))
                buf = buf[off + 4 + n:]
                msg = json.loads(data)
                reply = {"id": msg["id"], "result": self._result(msg)}
                frame = _ws_frame(json.dumps(reply).encode())
                if self.disconnect_at == "frame":
                    conn.sendall(frame[:-1])
                    return
                conn.sendall(frame)
        except (EOFError, OSError):
            return

    def _result(self, msg):
        method, params = msg["method"], msg.get("params", {})
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "page-1"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "attached-1"}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"a png").decode()}
        if method != "Runtime.evaluate":
            return {}
        expression = params["expression"]
        if expression == "document.readyState":
            return {"result": {"type": "string", "value": "complete"}}
        for piece, value in self.answers.items():
            if piece in expression:
                if value is THROWS:
                    return {"result": {"type": "object", "subtype": "error"},
                            "exceptionDetails": {"text": "Uncaught"}}
                return {"result": {"type": "object", "value": value}}
        return {"result": {"type": "undefined"}}


@pytest.fixture
def devtools():
    made = []

    def make(answers, *, disconnect_at=None):
        fake = FakeDevTools(answers, disconnect_at=disconnect_at)
        made.append(fake)
        return fake.port

    yield make
    for fake in made:
        fake.close()


def _closed_port():
    """A port on 127.0.0.1 nothing listens on: no browser there."""
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


def run(script, *args, port, fast=True, timeout=60):
    env = dict(os.environ, CDP_PORT=str(port), no_proxy="127.0.0.1", NO_PROXY="127.0.0.1")
    if script == FIT:
        cmd = [sys.executable, "-c", FIT_ON_FAKE_PORT, str(script), *args]
    elif fast:
        cmd = [sys.executable, "-c", FAST, str(script), *args]
    else:
        cmd = [sys.executable, str(script), *args]
    return subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout)


def _verdicts(proc):
    return [line for line in proc.stdout.splitlines() if line.startswith("VERDICT")]


def assert_ends(proc, status, verdict):
    """The check printed one verdict, as its last line, then exited with `status`, no traceback."""
    lines = proc.stdout.strip().splitlines()
    assert _verdicts(proc) == [verdict], proc.stdout
    assert lines[-1] == verdict, proc.stdout
    assert proc.stderr == ""
    assert proc.returncode == status, (proc.returncode, proc.stdout)


def assert_no_pass(proc):
    """An error ends the check non-zero, and it never claims a PASS."""
    assert proc.returncode != 0, proc.stdout
    assert "VERDICT: PASS" not in proc.stdout


SCROLL_CASES = {
    "explain-pass": ("explain", {EXPLAIN: EXPLAIN_OK}, 0, "VERDICT: PASS"),
    "explain-not-scrollable": ("explain", {EXPLAIN: {**EXPLAIN_OK, "scrollable": False}}, 1,
                               "VERDICT: FAIL"),
    "explain-button-below-screen": ("explain", {EXPLAIN: {**EXPLAIN_OK, "btnBottom": 520}}, 1,
                                    "VERDICT: FAIL"),
    # The early return: the page has no explain stage (a missing or unloaded page, or the
    # prediction not committed).
    "explain-no-panel": ("explain", {EXPLAIN: {"err": "no explain panel (prediction committed?)"}},
                         1, "VERDICT: FAIL - no explain panel (prediction committed?)"),
    # The early return: the measuring expression threw in the page, so there is no result.
    "explain-no-result": ("explain", {EXPLAIN: THROWS}, 1, "VERDICT: FAIL - no result"),
    "home-pass": ("home", {HOME: HOME_OK}, 0, "VERDICT: PASS"),
    "home-clipped-tiles": ("home", {HOME: {**HOME_OK, "tilesWithClippedMeta": 2,
                                           "clipped": ["T1", "T2"]}}, 1, "VERDICT: FAIL"),
    "home-no-grid": ("home", {HOME: {"err": "no .tiles grid"}}, 1,
                     "VERDICT: FAIL - no .tiles grid"),
}


@pytest.mark.parametrize("case", SCROLL_CASES.values(), ids=SCROLL_CASES.keys())
def test_render_scroll_exits_0_only_on_pass(devtools, case):
    mode, answers, status, verdict = case
    proc = run(SCROLL, "http://127.0.0.1:1/lesson/2", mode, port=devtools(answers))
    assert_ends(proc, status, verdict)


def test_render_scroll_run_plainly_exits_1_on_fail(devtools):
    port = devtools({EXPLAIN: {**EXPLAIN_OK, "scrollable": False}})
    proc = run(SCROLL, "http://127.0.0.1:1/lesson/2", "explain", port=port, fast=False)
    assert_ends(proc, 1, "VERDICT: FAIL")


def test_render_scroll_with_no_browser_exits_non_zero():
    proc = run(SCROLL, "http://127.0.0.1:1/lesson/2", "explain", port=_closed_port())
    assert_no_pass(proc)


@pytest.mark.parametrize("script", [SCROLL, FIT], ids=["scroll", "fit"])
@pytest.mark.parametrize("disconnect_at", ["handshake", "frame"])
def test_render_exits_when_devtools_disconnects(devtools, script, disconnect_at):
    port = devtools({}, disconnect_at=disconnect_at)
    proc = run(script, "http://127.0.0.1:1/lesson/2", port=port, timeout=3)
    assert proc.returncode == 1
    assert proc.stderr.strip() == "Chrome DevTools connection closed"
    assert _verdicts(proc) == []


FIGURE_CASES = {
    "pass": ({CARD: CARD_OK, OVERLAY: OVERLAY_OK}, 0, "VERDICT: PASS"),
    "commit-below-screen": ({CARD: {**CARD_OK, "commitBottom": 520}, OVERLAY: OVERLAY_OK}, 1,
                            "VERDICT: FAIL"),
    "card-scrolls": ({CARD: {**CARD_OK, "docScroll": 600}, OVERLAY: OVERLAY_OK}, 1,
                     "VERDICT: FAIL"),
    "image-not-loaded": ({CARD: CARD_OK, OVERLAY: {**OVERLAY_OK, "loaded": 0}}, 1,
                         "VERDICT: FAIL"),
    "overlay-not-open": ({CARD: CARD_OK, OVERLAY: {"err": "overlay not open"}}, 1,
                         "VERDICT: FAIL"),
    "overlay-threw": ({CARD: CARD_OK, OVERLAY: THROWS}, 1, "VERDICT: FAIL"),
}


@pytest.mark.parametrize("case", FIGURE_CASES.values(), ids=FIGURE_CASES.keys())
def test_render_figure_exits_0_only_on_pass(devtools, tmp_path, case):
    answers, status, verdict = case
    proc = run(FIGURE, "http://127.0.0.1:1/exam", "dark", str(tmp_path / "fig"),
               port=devtools(answers))
    assert_ends(proc, status, verdict)


def test_render_figure_run_plainly_exits_1_on_fail(devtools, tmp_path):
    port = devtools({CARD: CARD_OK, OVERLAY: {"err": "overlay not open"}})
    proc = run(FIGURE, "http://127.0.0.1:1/exam", "dark", str(tmp_path / "fig"), port=port,
               fast=False)
    assert_ends(proc, 1, "VERDICT: FAIL")


def test_render_figure_errors_exit_non_zero(devtools, tmp_path):
    out = str(tmp_path / "fig")
    # The card's expression threw: a page with no app on it (an error page, a wrong address).
    assert_no_pass(run(FIGURE, "http://127.0.0.1:1/exam", "dark", out,
                       port=devtools({CARD: THROWS, OVERLAY: OVERLAY_OK})))
    # No browser listening.
    assert_no_pass(run(FIGURE, "http://127.0.0.1:1/exam", "dark", out, port=_closed_port()))
    # The screenshots cannot be saved: their folder does not exist.
    assert_no_pass(run(FIGURE, "http://127.0.0.1:1/exam", "dark",
                       str(tmp_path / "missing" / "fig"),
                       port=devtools({CARD: CARD_OK, OVERLAY: OVERLAY_OK})))
    # No page address given.
    assert_no_pass(run(FIGURE, port=_closed_port()))
