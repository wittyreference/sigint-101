# ABOUTME: Tests render tools' verdicts and behavior when Chrome disconnects.
# ABOUTME: A fake DevTools endpoint stands in for Chrome; the scripts run as real processes.

import base64
import errno
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parent.parent
SCROLL = REPO / "tools" / "render" / "render_scroll.py"
FIGURE = REPO / "tools" / "render" / "render_figure.py"
FIT = REPO / "tools" / "render" / "render_fit.py"
LOGBOOK = REPO / "tools" / "render" / "render_logbook.py"
LORA = REPO / "tools" / "render" / "render_lora.py"
CARD_SORT = REPO / "tools" / "render" / "render_card_sort.py"
SURVEY = REPO / "tools" / "render" / "render_survey.py"
RECALL = REPO / "tools" / "render" / "render_recall.py"
LIVE_FIND = REPO / "tools" / "render" / "render_live_find.py"
FIELD = REPO / "tools" / "render" / "render_field.py"
BANDMAP = REPO / "tools" / "render" / "render_bandmap.py"
CLOSING = REPO / "tools" / "render" / "render_closing.py"
POWER_NOTICE = REPO / "tools" / "render" / "render_power_notice.py"
EVAL = REPO / "tools" / "render" / "render_eval.py"
CHECK = REPO / "tools" / "render" / "render_check.py"
LIVE_CONSOLE_PORT = 8008   # the kiosk's own console (make run), which a check must never default to

# Runs a script as __main__ with time.sleep made instant: the checks wait seconds for a real page
# to settle, and the fake answers at once. Nothing else changes, so the process's exit status is
# the script's own. Two tests below run the scripts plainly, sleeps and all.
FAST = (
    "import pathlib, runpy, sys, time\n"
    "time.sleep = lambda seconds: None\n"
    "script = sys.argv.pop(1)\n"
    "sys.path.insert(0, str(pathlib.Path(script).parent))\n"
    "runpy.run_path(script, run_name='__main__')\n"
)

# An expression that throws in the page: Chrome answers with exceptionDetails and no value.
THROWS = object()

# A piece of each measuring expression, to tell them apart.
EXPLAIN = 'data-stage="explain"'
HOME = "querySelector('.tiles')"
CARD = "exam-figure__open"
OVERLAY = "exam-figure:target"
DEMO_FIT = "})('demo')"
PREDICT_FIT = "})('predict')"

EXPLAIN_OK = {"hasBody": True, "overflowY": "auto", "scrollH": 900, "clientH": 400,
              "scrollable": True, "hasCompleteBtn": True, "btnBottom": 470, "viewportH": 480}
HOME_OK = {"overflowY": "auto", "gridAutoRows": "auto", "tiles": 12, "tilesWithClippedMeta": 0,
           "clipped": [], "scrollH": 900, "clientH": 400, "viewportH": 480}
CARD_OK = {"theme": "dark", "commitBottom": 470, "openW": 200, "openH": 80, "docScroll": 480,
           "vh": 480}
OVERLAY_OK = {"imgW": 700, "imgH": 400, "barBottom": 470, "barH": 40, "loaded": 1200}
NEXT_FITS = {"name": "Next step", "left": 520, "top": 404, "right": 800, "bottom": 480,
             "clipped": False}
CHOICE_FITS = {"name": "A Screen + storage", "left": 0, "top": 320, "right": 250,
               "bottom": 396, "clipped": False}
COMMIT_FITS = {**NEXT_FITS, "name": "Commit prediction"}
DEMO_FITS = {"primaryBottom": 480, "controls": [NEXT_FITS]}
PREDICT_FITS = {"primaryBottom": 480, "controls": [CHOICE_FITS, COMMIT_FITS]}
PREDICT_KEY_BELOW = {**PREDICT_FITS,
                     "controls": [{**CHOICE_FITS, "bottom": 480.25}, COMMIT_FITS]}


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
    THROWS, or to a function of the loaded page's URL that gives one. document.readyState is
    always "complete"; any other expression gives no value.
    `disconnect_at` can end the connection during the handshake or the first reply frame.
    """

    def __init__(self, answers, *, disconnect_at=None, blank_fails=False, errors=(),
                 polls=False, drop_on_blank=False):
        self.answers = answers
        self.disconnect_at = disconnect_at
        # A navigation to about:blank that Chrome refuses: the page stays where it was.
        self.blank_fails = blank_fails
        # Methods Chrome answers with a protocol error instead of a result.
        self.errors = set(errors)
        # DevTools going away mid-teardown: the connection drops when about:blank is asked for,
        # and the page stays where it was.
        self.drop_on_blank = drop_on_blank
        # Every DevTools command a check sent, in order, as (method, params). A websocket that
        # closes adds ("<closed>", {}); a GET of /mark/<name> adds ("<mark>", {"name": name}).
        self.calls = []
        self.url = "about:blank"
        self.page_closed = False
        self.browser_gone = False
        self.sessions = 0
        # Signalled each time a session ends, after its close is recorded (settled).
        self.idle = threading.Condition()
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()
        if polls:
            threading.Thread(target=self._poll, daemon=True).start()

    def settled(self, timeout=5):
        """The calls, once every session has ended and its close is recorded (or at timeout)."""
        with self.idle:
            self.idle.wait_for(lambda: self.sessions == 0, timeout)
        return self.calls

    def close(self):
        self.browser_gone = True
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        finally:
            self.server.close()

    def _poll(self):
        """The page's own polls, as a real page makes them once no DevTools session blocks them.

        Only a loaded console page polls: not about:blank, not a closed page, not a page whose
        browser has gone.
        """
        from urllib.request import ProxyHandler, Request, build_opener
        opener = build_opener(ProxyHandler({}))
        while not self.browser_gone:
            url = self.url or ""
            if self.sessions == 0 and not self.page_closed and url.startswith("http"):
                origin = "/".join(url.split("/")[:3])
                # The lesson page's open post, and a panel's poll.
                for request in (Request(f"{origin}/api/opened?lesson_id=1", data=b"",
                                        method="POST"),
                                Request(f"{origin}/api/decode")):
                    try:
                        opener.open(request, timeout=2).close()
                    except OSError:
                        pass
            threading.Event().wait(0.02)

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
                with self.idle:
                    self.sessions += 1
                    self.calls.append(("<open>", {}))
                try:
                    self._websocket(conn, rest)
                finally:
                    with self.idle:
                        self.sessions -= 1
                        self.idle.notify_all()
                return
            if path.startswith("/mark/"):
                self.calls.append(("<mark>", {"name": path[len("/mark/"):]}))
                body, status = b"{}", b"200 OK"
            elif path == "/json/version":
                url = f"ws://127.0.0.1:{self.port}/devtools/browser/fake"
                body = json.dumps({"webSocketDebuggerUrl": url}).encode()
                status = b"200 OK"
            elif path in ("/json", "/json/list"):
                url = f"ws://127.0.0.1:{self.port}/devtools/page/fake"
                pages = [] if self.page_closed else [
                    {"type": "page", "id": "page-1", "webSocketDebuggerUrl": url}]
                body = json.dumps(pages).encode()
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
                if (self.drop_on_blank and msg["method"] == "Page.navigate"
                        and msg.get("params", {}).get("url") == "about:blank"):
                    self.calls.append((msg["method"], msg.get("params", {})))
                    self.calls.append(("<closed>", {}))
                    conn.shutdown(socket.SHUT_RDWR)
                    return
                result = self._result(msg)
                if msg["method"] in self.errors:
                    reply = {"id": msg["id"],
                             "error": {"code": -32000, "message": f"{msg['method']} refused"}}
                else:
                    reply = {"id": msg["id"], "result": result}
                frame = _ws_frame(json.dumps(reply).encode())
                if self.disconnect_at == "frame":
                    conn.sendall(frame[:-1])
                    return
                conn.sendall(frame)
        except (EOFError, OSError):
            self.calls.append(("<closed>", {}))
            return

    def _result(self, msg):
        method, params = msg["method"], msg.get("params", {})
        self.calls.append((method, params))
        if method in self.errors:
            return {}
        if method == "Target.getTargets":
            pages = [] if self.page_closed else [{"type": "page", "targetId": "page-1"}]
            return {"targetInfos": pages}
        if method == "Target.closeTarget":
            self.page_closed = True
            return {"success": True}
        if method == "Target.createTarget":
            self.page_closed, self.url = False, params.get("url", "about:blank")
            return {"targetId": "page-1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "attached-1"}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"a png").decode()}
        if method == "Page.navigate":
            if self.blank_fails and params.get("url") == "about:blank":
                return {"frameId": "page-1", "errorText": "net::ERR_ABORTED"}
            self.url = params.get("url")
        if method != "Runtime.evaluate":
            return {}
        expression = params["expression"]
        if expression == "document.readyState":
            return {"result": {"type": "string", "value": "complete"}}
        if expression == "location.href":
            return {"result": {"type": "string", "value": self.url}}
        for piece, value in self.answers.items():
            if piece in expression:
                if callable(value):     # an answer that depends on the page loaded
                    value = value(self.url or "")
                if value is THROWS:
                    return {"result": {"type": "object", "subtype": "error"},
                            "exceptionDetails": {"text": "Uncaught"}}
                return {"result": {"type": "object", "value": value}}
        return {"result": {"type": "undefined"}}


@pytest.fixture
def devtools():
    made = []

    def make(answers, *, disconnect_at=None, blank_fails=False, errors=(), drop_on_blank=False):
        fake = FakeDevTools(answers, disconnect_at=disconnect_at, blank_fails=blank_fails,
                            errors=errors, drop_on_blank=drop_on_blank)
        made.append(fake)
        return fake.port

    make.made = made
    yield make
    for fake in made:
        fake.close()


@pytest.fixture
def closed_port():
    """Hold a loopback port without listening so connects fail even during parallel tests."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()[1]


def test_unreachable_render_port_stays_reserved(closed_port):
    with socket.socket() as contender:
        with pytest.raises(OSError) as unavailable:
            contender.bind(("127.0.0.1", closed_port))
        assert unavailable.value.errno == errno.EADDRINUSE


def run(script, *args, port, fast=True, timeout=60):
    env = dict(os.environ, CDP_PORT=str(port), no_proxy="127.0.0.1", NO_PROXY="127.0.0.1")
    if fast:
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


FIT_CASES = {
    "pass": (DEMO_FITS, PREDICT_FITS, 0, "VERDICT: PASS"),
    "choice-below-screen": (
        DEMO_FITS, PREDICT_KEY_BELOW, 1,
        "VERDICT: FAIL - predict: A Screen + storage is outside 800x480 (0, 320, 250, 480.25)",
    ),
    "button-right-of-screen": (
        {**DEMO_FITS, "controls": [{**NEXT_FITS, "right": 800.25}]}, PREDICT_FITS, 1,
        "VERDICT: FAIL - demo: Next step is outside 800x480 (520, 404, 800.25, 480)",
    ),
    "clipped-choice": (
        DEMO_FITS, {**PREDICT_FITS, "controls": [{**CHOICE_FITS, "clipped": True}, COMMIT_FITS]}, 1,
        "VERDICT: FAIL - predict: A Screen + storage is clipped",
    ),
}


@pytest.mark.parametrize("case", FIT_CASES.values(), ids=FIT_CASES.keys())
def test_render_fit_exits_0_only_when_all_controls_fit(devtools, tmp_path, case):
    demo, predict, status, verdict = case
    shot = tmp_path / "predict.png"
    port = devtools({DEMO_FIT: demo, PREDICT_FIT: predict})
    proc = run(FIT, "http://127.0.0.1:1/lesson/29", str(shot), port=port)
    assert_ends(proc, status, verdict)
    assert "800x480  demo Next bottom = 480px  predict Commit bottom = 480px" in proc.stdout
    assert shot.is_file()


@pytest.mark.parametrize("predict", [THROWS, {"err": "missing stage panel"}],
                         ids=["no-result", "no-stage"])
def test_render_fit_cannot_pass_without_a_measurement(devtools, tmp_path, predict):
    port = devtools({DEMO_FIT: DEMO_FITS, PREDICT_FIT: predict})
    proc = run(FIT, "http://127.0.0.1:1/lesson/29", str(tmp_path / "predict.png"), port=port)
    assert_no_pass(proc)
    assert proc.returncode == 1
    assert _verdicts(proc)[0].startswith("VERDICT: FAIL - predict:")


def test_render_fit_cannot_pass_without_devtools(tmp_path, closed_port):
    proc = run(FIT, "http://127.0.0.1:1/lesson/29", str(tmp_path / "predict.png"),
               port=closed_port, timeout=3)
    assert_no_pass(proc)


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


def test_render_scroll_with_no_browser_exits_non_zero(closed_port):
    proc = run(SCROLL, "http://127.0.0.1:1/lesson/2", "explain", port=closed_port)
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


def test_render_figure_errors_exit_non_zero(devtools, tmp_path, closed_port):
    out = str(tmp_path / "fig")
    # The card's expression threw: a page with no app on it (an error page, a wrong address).
    assert_no_pass(run(FIGURE, "http://127.0.0.1:1/exam", "dark", out,
                       port=devtools({CARD: THROWS, OVERLAY: OVERLAY_OK})))
    # No browser listening.
    assert_no_pass(run(FIGURE, "http://127.0.0.1:1/exam", "dark", out, port=closed_port))
    # The screenshots cannot be saved: their folder does not exist.
    assert_no_pass(run(FIGURE, "http://127.0.0.1:1/exam", "dark",
                       str(tmp_path / "missing" / "fig"),
                       port=devtools({CARD: CARD_OK, OVERLAY: OVERLAY_OK})))
    # No page address given.
    assert_no_pass(run(FIGURE, port=closed_port))


@pytest.fixture
def console_url():
    class Console(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/" else 404)
            self.end_headers()

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Console) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            worker.join()


@pytest.fixture
def render_make(tmp_path, console_url, devtools, strays):
    """Exercise the repo's Makefile and runner with stand-ins for the browser and checks."""
    screen = tmp_path / "tools" / "render"
    screen.mkdir(parents=True)
    (screen / "render_check.py").symlink_to(REPO / "tools" / "render" / "render_check.py")
    # The environment running this suite stands in for the checkout's .venv, whole: the scratch
    # console needs its packages, which a lone link to the interpreter would not find.
    (tmp_path / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    assert (tmp_path / ".venv" / "bin" / "python").exists()
    path = tmp_path / "bin"
    path.mkdir()
    # The browser a chromium wrapper launches: it keeps writing into the profile until signalled,
    # the way the real one does after its wrapper has gone.
    browser = tmp_path / "browser_child.py"
    browser.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "default = Path(sys.argv[1]) / 'Default'\n"
        "Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "end, n = time.monotonic() + 20, 0\n"
        "while time.monotonic() < end:\n"
        "    try:\n"
        "        default.mkdir(parents=True, exist_ok=True)\n"
        "        (default / f'cache{n % 50}').write_text('x')\n"
        "    except OSError:\n"
        "        pass\n"
        "    n += 1\n"
        "    time.sleep(0.005)\n",
        encoding="utf-8",
    )
    chromium = path / "chromium"
    chromium.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "profile = next(arg.split('=', 1)[1] for arg in sys.argv[1:]\n"
        "               if arg.startswith('--user-data-dir='))\n"
        "if os.environ.get('CHROMIUM_EXITS'):\n"
        "    sys.exit(3)\n"
        "Path(profile).mkdir()\n"
        "if os.environ.get('CHROMIUM_FORKS'):\n"
        "    Path(os.environ['CHROMIUM_FORKS'] + '.wrapper').write_text(str(os.getpid()))\n"
        f"    subprocess.Popen([sys.executable, {str(browser)!r}, profile,\n"
        "                      os.environ['CHROMIUM_FORKS']],\n"
        "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "if os.environ.get('MARK_STOP'):\n"
        "    import signal, urllib.request\n"
        "    def stopped(*_):\n"
        "        urllib.request.urlopen('http://127.0.0.1:' + os.environ['FAKE_CDP_PORT']\n"
        "                               + '/mark/chromium-stopped', timeout=5).close()\n"
        "        os._exit(0)\n"
        "    signal.signal(signal.SIGTERM, stopped)\n"
        "(Path(profile) / 'DevToolsActivePort').write_text(os.environ['FAKE_CDP_PORT'] + '\\n')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    chromium.chmod(0o755)
    for name in _runner_check_names():
        (screen / name).write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "from urllib.request import urlopen\n"
            "name = Path(__file__).name\n"
            "assert os.environ['CDP_PORT'] == os.environ['FAKE_CDP_PORT']\n"
            "if name == 'render_scroll.py':\n"
            "    assert sys.argv[2] == 'home'\n"
            "print('ran', name, sys.argv[1], flush=True)\n"
            "fake = 'http://127.0.0.1:' + os.environ['FAKE_CDP_PORT']\n"
            "urlopen(f'{fake}/mark/{name}', timeout=10).close()\n"
            "if name == 'render_scroll.py' and os.environ.get('PROBE_CONSOLE'):\n"
            "    with urlopen(sys.argv[1], timeout=10) as page:\n"
            "        print('console', page.status, b'SIGINT 101' in page.read(), flush=True)\n"
            "if name == os.environ.get('STALL_CHECK'):\n"
            "    import time\n"
            "    time.sleep(60)\n"
            "sys.exit(7 if name == os.environ.get('FAILING_CHECK') else 0)\n",
            encoding="utf-8",
        )
    make = shutil.which("make")
    assert make, "the render-check tests require make"

    def environment(fit_answers, overrides, blank_fails=False, errors=()):
        """The check's environment; an override of None removes that variable."""
        port = devtools(fit_answers or {}, blank_fails=blank_fails, errors=errors)
        env = dict(os.environ, PATH=str(path), RENDER_URL=console_url, TMPDIR=str(tmp_path),
                   FAKE_CDP_PORT=str(port),
                   no_proxy="127.0.0.1", NO_PROXY="127.0.0.1")
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def run_make(*, has_chromium=True, fit_answers=None, timeout=15, blank_fails=False,
                 errors=(), real=(), **overrides):
        """Run make render-check with these stand-ins; the scripts in `real` run for real."""
        if not has_chromium:
            chromium.unlink()
        if fit_answers is not None:
            real = (*real, FIT)
        for script in real:
            (screen / script.name).unlink()
            (screen / script.name).symlink_to(script)
        env = environment(fit_answers, overrides, blank_fails, errors)
        return subprocess.run([make, "--no-print-directory", "-f", str(REPO / "Makefile"),
                               "render-check"], cwd=tmp_path, env=env, capture_output=True,
                              text=True, timeout=timeout)

    def start(**overrides):
        """Start the runner itself, as make would, and hand back the process to signal."""
        return subprocess.Popen([str(tmp_path / ".venv" / "bin" / "python"),
                                 str(screen / "render_check.py")], cwd=tmp_path,
                                env=environment(None, overrides), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    run_make.start = start
    run_make.calls = lambda: devtools.made[-1].settled()
    run_make.screen = screen
    return run_make


def test_make_render_check_fails_when_render_fit_detects_overflow(render_make):
    # Both fit runs measure a predict stage (/lesson/29, then /lesson/39), and both see it.
    proc = render_make(fit_answers={DEMO_FIT: DEMO_FITS, PREDICT_FIT: PREDICT_KEY_BELOW})
    assert proc.returncode != 0, proc.stdout
    assert "render_fit.py exited with status 1" in proc.stderr
    assert _verdicts(proc) == [FIT_CASES["choice-below-screen"][-1]] * 2


# --- C69: /lesson/39 (the Wi-Fi survey's predict) is one of the fit runs --------------------------


def _fails_on_lesson_39(fits, defect):
    """A page answer that measures `defect` on /lesson/39 and `fits` on every other page."""
    return lambda url: defect if urlsplit(url).path == "/lesson/39" else fits


def test_a_fit_failure_on_lesson_39_fails_the_run(render_make):
    # M39's predict fits 800x480 by 3 px. A chip or prompt that adds a line must fail the run,
    # while /lesson/29 still fits.
    proc = render_make(fit_answers={
        DEMO_FIT: DEMO_FITS, PREDICT_FIT: _fails_on_lesson_39(PREDICT_FITS, PREDICT_KEY_BELOW)})
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    assert "render_fit.py exited with status 1" in proc.stderr
    assert _verdicts(proc) == ["VERDICT: PASS", FIT_CASES["choice-below-screen"][-1]]
    proxy, _ = _proxy_url(proc)
    loaded = [params["url"] for method, params in render_make.calls()
              if method == "Page.navigate" and params.get("url") != "about:blank"]
    assert f"{proxy}/lesson/39" in loaded, loaded


def test_the_lesson_39_fit_run_measures_its_predict_stage_only():
    # L39 has no demo stage, and its stages past predict are sealed on a fresh console: its fit
    # run measures predict, the one interactive stage the page shows before a commit.
    entries = [entry for entry in _load_render_check().check_list("http://127.0.0.1:9",
                                                                   Path("/out"))
               if entry[0] == FIT.name]
    assert [entry[1] for entry in entries] == ["http://127.0.0.1:9/lesson/29",
                                               "http://127.0.0.1:9/lesson/39"]
    assert entries[0][3:] == ()
    assert entries[1][3:] == ("predict",)


def test_render_fit_measures_the_stages_it_is_given(devtools, tmp_path):
    port = devtools({DEMO_FIT: THROWS, PREDICT_FIT: PREDICT_FITS})
    proc = run(FIT, "http://127.0.0.1:1/lesson/39", str(tmp_path / "predict.png"), "predict",
               port=port)
    assert_ends(proc, 0, "VERDICT: PASS")
    assert "800x480  predict Commit bottom = 480px  (max 480)" in proc.stdout
    evaluated = [params["expression"] for method, params in devtools.made[-1].settled()
                 if method == "Runtime.evaluate"]
    assert not any(DEMO_FIT in expression for expression in evaluated)


def _proxy_url(proc):
    """The render proxy the runner put in front of the console, from its own report."""
    found = re.search(r"behind a render proxy at (http://127\.0\.0\.1:\d+) \(process (\d+)\)",
                      proc.stdout)
    assert found, proc.stdout
    return found.group(1), int(found.group(2))


@pytest.mark.parametrize(
    "failing",
    [
        "", FIT.name, SCROLL.name, FIGURE.name, LOGBOOK.name, LORA.name, CARD_SORT.name,
        SURVEY.name, RECALL.name, LIVE_FIND.name, FIELD.name, BANDMAP.name, CLOSING.name,
        POWER_NOTICE.name,
    ],
    ids=[
        "all-pass", "fit-fails", "scroll-fails", "figure-fails",
        "logbook-fails", "lora-fails", "card-sort-fails", "survey-fails",
        "recall-fails", "live-find-fails", "field-fails", "bandmap-fails", "closing-fails",
        "power-notice-fails",
    ],
)
def test_make_render_check_runs_all_checks_and_reports_failure(render_make, console_url, failing):
    proc = render_make(FAILING_CHECK=failing)
    # The checks get the render proxy's address, never the console's own.
    proxy, pid = _proxy_url(proc)
    assert proxy != console_url
    assert proc.stdout.splitlines() == [
        f"Fenced the console behind a render proxy at {proxy} (process {pid}).",
        f"ran {FIT.name} {proxy}/lesson/29",
        f"ran {FIT.name} {proxy}/lesson/39",
        f"ran {SCROLL.name} {proxy}/",
        f"ran {FIGURE.name} {proxy}/exam/review/T6C02",
        f"ran {LOGBOOK.name} {proxy}",
        f"ran {LORA.name} {proxy}",
        f"ran {CARD_SORT.name} {proxy}",
        f"ran {SURVEY.name} {proxy}",
        f"ran {RECALL.name} {proxy}",
        f"ran {LIVE_FIND.name} {proxy}",
        f"ran {FIELD.name} {proxy}",
        f"ran {BANDMAP.name} {proxy}",
        f"ran {CLOSING.name} {proxy}",
        f"ran {POWER_NOTICE.name} {proxy}",
    ], (proc.stdout, proc.stderr)
    if failing:
        assert proc.returncode != 0
        assert f"{failing} exited with status 7" in proc.stderr
    else:
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr == ""


@pytest.mark.parametrize("overrides, expected", [
    ({"has_chromium": False}, "Chromium executable not found"),
    ({"CHROMIUM_EXITS": "1"}, "Cannot reach Chromium DevTools"),
], ids=["chromium-missing", "chromium-exits-before-devtools"])
def test_make_render_check_fails_clearly_when_chromium_is_unreachable(
    render_make, overrides, expected
):
    proc = render_make(**overrides)
    assert proc.returncode != 0
    assert expected in proc.stderr, proc.stderr
    assert "ran " not in proc.stdout


def test_make_render_check_fails_clearly_when_console_is_unreachable(render_make, closed_port):
    proc = render_make(RENDER_URL=f"http://127.0.0.1:{closed_port}")
    assert proc.returncode != 0
    assert "Cannot reach the console" in proc.stderr, proc.stderr
    assert "ran " not in proc.stdout


# --- C49: a check run starts nothing and writes nothing on the console it measures ---------------


def _ran_urls(proc):
    return [line.split()[2] for line in proc.stdout.splitlines() if line.startswith("ran ")]


def test_render_check_without_render_url_measures_a_scratch_console_not_the_kiosks(render_make):
    # The kiosk's own console (make run, port 8008) holds the learner's state and the radio. With
    # no RENDER_URL the check must measure a console it started itself, on another port.
    proc = render_make(RENDER_URL=None, PROBE_CONSOLE="1", timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    urls = _ran_urls(proc)
    assert len(urls) == len(_runner_check_names()), proc.stdout
    origins = {url.split("/")[2] for url in urls}
    assert len(origins) == 1, urls
    host, port = origins.pop().split(":")
    assert host == "127.0.0.1"
    assert int(port) != LIVE_CONSOLE_PORT
    # The checks measured a real SIGINT 101 console, which is gone once the run ends.
    assert "console 200 True" in proc.stdout.splitlines(), proc.stdout
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", int(port)), timeout=2):
            pass


def _load_render_check():
    import importlib.util
    spec = importlib.util.spec_from_file_location("render_check_under_test", CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_scratch_console_keeps_its_state_in_its_own_folder_and_stops(tmp_path, strays):
    from urllib.request import urlopen
    render_check = _load_render_check()
    data = tmp_path / "console"
    with render_check.scratch_console(data) as url:
        assert int(url.rsplit(":", 1)[1]) != LIVE_CONSOLE_PORT
        with urlopen(f"{url}/operate/ism", timeout=10) as page:
            assert page.status == 200
        assert (data / "progress.db").is_file()
    with pytest.raises(OSError):
        urlopen(f"{url}/", timeout=2)


def _alive(pid):
    return _running(pid)


def test_render_check_stops_the_whole_browser_before_removing_its_profile(render_make, tmp_path):
    # A chromium wrapper that dies on the signal leaves its browser writing into the profile:
    # the check must stop that browser too, and only then remove the profile.
    pidfile = tmp_path / "browser.pid"
    try:
        proc = render_make(CHROMIUM_FORKS=str(pidfile))
        assert pidfile.is_file(), (proc.stdout, proc.stderr)
        pid = int(pidfile.read_text())
        assert not _alive(pid), "the browser outlived the render check"
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert "Traceback" not in proc.stderr, proc.stderr
        assert list(tmp_path.glob("sigint101-render-*")) == []
    finally:
        if pidfile.is_file() and _alive(int(pidfile.read_text())):
            os.kill(int(pidfile.read_text()), 9)


API_TOOLS = {
    "fit": (FIT, ["http://127.0.0.1:1/lesson/29", "{tmp}/predict.png"],
            {DEMO_FIT: DEMO_FITS, PREDICT_FIT: PREDICT_FITS}),
    "scroll": (SCROLL, ["http://127.0.0.1:1/", "home"], {HOME: HOME_OK}),
    "figure": (FIGURE, ["http://127.0.0.1:1/exam/review/T6C02", "dark", "{tmp}/fig"],
               {CARD: CARD_OK, OVERLAY: OVERLAY_OK}),
    "logbook": (LOGBOOK, ["http://127.0.0.1:9"], {}),
    "lora": (LORA, ["http://127.0.0.1:9"], {}),
    "card-sort": (CARD_SORT, ["http://127.0.0.1:9"], {}),
    "eval": (EVAL, ["http://127.0.0.1:1/operate/ism", "1"], {}),
    "survey": (SURVEY, ["http://127.0.0.1:9"], {}),
    "live-find": (LIVE_FIND, ["http://127.0.0.1:9"], {}),
    "field": (FIELD, ["http://127.0.0.1:9"], {}),
    "bandmap": (BANDMAP, ["http://127.0.0.1:9"], {}),
    "closing": (CLOSING, ["http://127.0.0.1:9"], {}),
    "power-notice": (POWER_NOTICE, ["http://127.0.0.1:9"], {}),
}


@pytest.mark.parametrize("tool", API_TOOLS.values(), ids=API_TOOLS.keys())
def test_every_render_tool_blocks_the_console_api_before_it_loads_a_page(devtools, tmp_path,
                                                                         tool):
    # A page's own polls (/api/<receiver>, /api/receptions, ...) start receivers and claim the
    # radio; the rack's and the lessons' posts write learner state. Blocking /api/* before the
    # first navigation keeps a check to layout alone.
    script, args, answers = tool
    run(script, *(arg.format(tmp=tmp_path) for arg in args), port=devtools(answers))
    calls = devtools.made[-1].settled()
    methods = [method for method, _ in calls]
    assert "Page.navigate" in methods, methods
    first_load = methods.index("Page.navigate")
    blocks = [i for i, (method, params) in enumerate(calls)
              if method == "Network.setBlockedURLs" and "*/api/*" in params.get("urls", [])]
    assert blocks and blocks[0] < first_load, methods[:first_load]
    assert "Network.enable" in methods[:blocks[0]], methods[:first_load]
    # Nothing narrows the block afterwards: every later list still covers the whole API.
    for method, params in calls:
        if method == "Network.setBlockedURLs":
            assert "*/api/*" in params.get("urls", []), params


def _leaves_blank_before_closing(calls):
    """The last page a client navigated to is about:blank, reached before its connection closed.

    The /api/* block belongs to the DevTools session, so a page left loaded after the close
    polls the console again.
    """
    methods = [method for method, _ in calls]
    navigations = [i for i, (method, _) in enumerate(calls) if method == "Page.navigate"]
    assert navigations, methods
    last = navigations[-1]
    assert calls[last][1].get("url") == "about:blank", calls[last]
    assert "<closed>" in methods[last:], methods[last:]
    closed = last + methods[last:].index("<closed>")
    confirmed = [method for method, params in calls[last:closed]
                 if method == "Runtime.evaluate" and params.get("expression") == "location.href"]
    assert confirmed, "closed without waiting for about:blank"


@pytest.mark.parametrize("tool", API_TOOLS.values(), ids=API_TOOLS.keys())
def test_every_render_tool_leaves_its_page_on_about_blank_before_it_closes(devtools, tmp_path,
                                                                           tool):
    script, args, answers = tool
    run(script, *(arg.format(tmp=tmp_path) for arg in args), port=devtools(answers))
    _leaves_blank_before_closing(devtools.made[-1].settled())


def test_render_check_holds_its_own_api_block_across_every_check(render_make):
    # Between one tool's close and the next one's attach no tool's block exists. The runner's
    # own session blocks /api/* from before the first check until after the last.
    proc = render_make()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    calls = render_make.calls()
    methods = [method for method, _ in calls]
    marks = [i for i, method in enumerate(methods) if method == "<mark>"]
    assert len(marks) == len(_runner_check_names()), methods
    blocks = [i for i, (method, params) in enumerate(calls)
              if method == "Network.setBlockedURLs" and "*/api/*" in params.get("urls", [])]
    assert blocks and blocks[0] < marks[0], methods
    assert "<closed>" not in methods[blocks[0]:marks[-1]], methods
    _leaves_blank_before_closing(calls)
    assert methods.index("<closed>", marks[-1]) > marks[-1]


def test_no_render_tool_sets_its_own_block_or_closes_devtools_itself():
    # The block and the about:blank teardown live in render_scroll.py alone. A tool that sets its
    # own blocked list replaces the shared one, and one that closes its socket skips the
    # teardown; a tool that drives a page without the shared client has no block at all.
    import re
    for path in sorted((REPO / "tools" / "render").glob("*.py")):
        if path == SCROLL:
            continue
        text = path.read_text(encoding="utf-8")
        assert "setBlockedURLs" not in text, f"{path.name} sets its own API block"
        for match in re.finditer(r"([\w.]+)\.(?:s|sock)\.close\(\)", text):
            # Only a client's own reader closes its socket, when DevTools has already hung up.
            assert match.group(1) == "self", f"{path.name} closes DevTools itself: {match[0]}"
        if "Page.navigate" in text:
            assert re.search(r"\b(?:connect|block_console_api)\(", text), \
                f"{path.name} loads pages without the shared block"


@pytest.mark.parametrize("tool", [API_TOOLS["scroll"], API_TOOLS["fit"]], ids=["scroll", "fit"])
def test_a_tool_whose_page_cannot_reach_about_blank_fails(devtools, tmp_path, tool):
    # The block must not end with a console page loaded. If about:blank cannot be reached the
    # check says so and fails, even when every layout verdict passed.
    script, args, answers = tool
    proc = run(script, *(arg.format(tmp=tmp_path) for arg in args),
               port=devtools(answers, blank_fails=True))
    assert "VERDICT: PASS" in proc.stdout, proc.stdout
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    assert "about:blank" in proc.stderr, proc.stderr


def test_the_runner_keeps_its_block_until_chromium_has_stopped_when_blank_fails(render_make):
    # Chrome refuses both ways off the console page: the blank navigation and closing the target.
    proc = render_make(blank_fails=True, errors={"Target.closeTarget"}, MARK_STOP="1")
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    assert "about:blank" in proc.stderr, proc.stderr
    calls = render_make.calls()
    methods = [method for method, _ in calls]
    stopped = calls.index(("<mark>", {"name": "chromium-stopped"}))
    assert "<closed>" not in methods[:stopped], methods
    assert "<closed>" in methods[stopped:], methods


def test_a_page_that_cannot_reach_blank_is_closed_before_the_block_ends(render_make):
    # The fence after a refused blank navigation: the page target itself is closed, and the
    # session's close comes only once DevTools no longer lists it. The run still fails.
    proc = render_make(blank_fails=True, MARK_STOP="1")
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    calls = render_make.calls()
    methods = [method for method, _ in calls]
    closed_target = methods.index("Target.closeTarget")
    assert "<closed>" in methods[closed_target:], methods
    assert "<closed>" not in methods[methods.index("Network.setBlockedURLs"):closed_target]


@pytest.mark.parametrize("tool", API_TOOLS.values(), ids=API_TOOLS.keys())
@pytest.mark.parametrize("refused", ["Network.enable", "Network.setBlockedURLs"])
def test_a_refused_api_block_stops_a_tool_before_it_loads_any_page(devtools, tmp_path, tool,
                                                                   refused):
    # Chrome answering the block setup with a protocol error must not read as a block in place.
    script, args, answers = tool
    proc = run(script, *(arg.format(tmp=tmp_path) for arg in args),
               port=devtools(answers, errors={refused}))
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    loads = [params.get("url") for method, params in devtools.made[-1].settled()
             if method == "Page.navigate"]
    assert all(url == "about:blank" for url in loads), loads


@pytest.mark.parametrize("tool", [API_TOOLS["scroll"], API_TOOLS["fit"]], ids=["scroll", "fit"])
def test_a_devtools_protocol_error_fails_the_tool(devtools, tmp_path, tool):
    script, args, answers = tool
    proc = run(script, *(arg.format(tmp=tmp_path) for arg in args),
               port=devtools(answers, errors={"Emulation.setDeviceMetricsOverride"}))
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    assert "Emulation.setDeviceMetricsOverride" in proc.stderr, proc.stderr


def test_both_devtools_clients_refuse_to_load_a_page_before_the_block_is_confirmed(
        devtools, monkeypatch):
    import importlib
    port = devtools({})
    fake = devtools.made[-1]
    monkeypatch.syspath_prepend(str(SCROLL.parent))
    monkeypatch.setenv("CDP_PORT", str(port))
    scroll = importlib.import_module("render_scroll")
    fit = importlib.import_module("render_fit")
    monkeypatch.setattr(fit, "PORT", port)
    clients = [scroll.CDP(f"ws://127.0.0.1:{port}/devtools/browser/fake", port=port),
               fit.CDP(f"ws://127.0.0.1:{port}/devtools/page/fake")]
    for client in clients:
        with pytest.raises(RuntimeError, match="block"):
            client.call("Page.navigate", {"url": "http://127.0.0.1:9/lesson/1"})
        _socket = getattr(client, "s", None) or client.sock
        _socket.close()
    assert "Page.navigate" not in [method for method, _ in fake.calls]


@pytest.mark.parametrize("tool", API_TOOLS.values(), ids=API_TOOLS.keys())
def test_a_tool_run_on_its_own_loads_its_pages_through_a_render_proxy(devtools, tmp_path, tool):
    # Run alone, a tool puts its own render proxy in front of the console it was given: the
    # browser never gets the console's own address.
    script, args, answers = tool
    args = [arg.format(tmp=tmp_path) for arg in args]
    given = "/".join(args[0].split("/")[:3])
    run(script, *args, port=devtools(answers))
    pages = [params["url"] for method, params in devtools.made[-1].settled()
             if method == "Page.navigate" and params.get("url") != "about:blank"]
    assert pages, "the tool loaded no page"
    for url in pages:
        assert not url.startswith(given + "/"), url
        assert url.startswith("http://127.0.0.1:"), url


def test_neither_devtools_client_loads_a_console_page_directly(devtools, monkeypatch):
    # A blocked client still refuses a page that is not behind a render proxy.
    import importlib
    port = devtools({})
    monkeypatch.syspath_prepend(str(SCROLL.parent))
    monkeypatch.setenv("CDP_PORT", str(port))
    monkeypatch.delenv("RENDER_FENCE", raising=False)
    scroll = importlib.import_module("render_scroll")
    fit = importlib.import_module("render_fit")
    monkeypatch.setattr(fit, "PORT", port)
    clients = [scroll.CDP(f"ws://127.0.0.1:{port}/devtools/browser/fake", port=port),
               fit.CDP(f"ws://127.0.0.1:{port}/devtools/page/fake")]
    for client in clients:
        client.blocked = True
        with pytest.raises(RuntimeError, match="render proxy"):
            client.call("Page.navigate", {"url": "http://127.0.0.1:9/lesson/1"})
        (getattr(client, "s", None) or client.sock).close()
    assert "Page.navigate" not in [method for method, _ in devtools.made[-1].settled()]


def _stubborn_group(tmp_path):
    """A leader that dies on SIGTERM and a child of its group that ignores it; (leader, pidfile)."""
    child_pid = tmp_path / "child.pid"
    child = tmp_path / "stubborn.py"
    child.write_text("import os, signal, sys, time\n"
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                     "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                     "time.sleep(60)\n", encoding="utf-8")
    leader = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time\n"
         "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
         "time.sleep(60)\n", str(child), str(child_pid)],
        start_new_session=True)
    for _ in range(200):
        if child_pid.is_file() and child_pid.read_text():
            break
        subprocess.run(["sleep", "0.05"])
    return leader, child_pid


@pytest.mark.parametrize("denied", ["enumeration", "stat-read"])
def test_an_unreadable_proc_never_counts_as_the_group_gone(tmp_path, monkeypatch, denied):
    # With /proc unreadable, a TERM-resistant child may still be running: stop_group must not
    # report the group stopped, and the run's live marker stays.
    render_check = _load_render_check()
    leader, child_pid = _stubborn_group(tmp_path)
    try:
        if denied == "enumeration":
            real_listdir = os.listdir
            monkeypatch.setattr(render_check.os, "listdir", lambda path: (
                _raise(PermissionError(13, "Permission denied"))() if str(path) == "/proc"
                else real_listdir(path)))
        else:
            monkeypatch.setattr(render_check, "_proc_stat",
                                _raise(PermissionError(13, "Permission denied")))
        try:
            confirmed = render_check.stop_group(leader, 0.3)
        except OSError:
            confirmed = None
        alive = []
        entry = render_check._launched(leader, "the stand-in", alive)
        assert render_check._stop(leader, 0.3, entry, alive) is False
        monkeypatch.undo()
        assert confirmed is not True
        assert alive == [entry]
        assert _running(int(child_pid.read_text())), "the child really is still running"
    finally:
        monkeypatch.undo()
        if leader.poll() is None:
            leader.kill()
            leader.wait()
        if child_pid.is_file() and _running(int(child_pid.read_text())):
            os.kill(int(child_pid.read_text()), 9)


def _runner_check_names(runner=CHECK):
    """The scripts a runner runs, read from its own check list (render_check.check_list)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"runner_{abs(hash(str(runner)))}", runner)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [entry[0] for entry in module.check_list("http://127.0.0.1:9", Path("/out"))]


def _pages_loaded_from(proxy, calls):
    """The path and query of every page loaded from the runner's render proxy."""
    pages = set()
    for method, params in calls:
        url = params.get("url", "") if method == "Page.navigate" else ""
        if url.startswith(proxy + "/") or url == proxy:
            pages.add(url[len(proxy):] or "/")
    return pages


def _coverage_problems(proc, calls, names):
    """What keeps the route coverage from being whole; empty when it is.

    Every check the runner lists must start (open its DevTools session, in order, after the
    runner's own) and navigate to at least one page, and every page loaded from the runner's
    proxy must be in the GET guard's walk. Recall's pages come from scratch consoles of its own,
    behind proxies of its own, so they count as navigations but not as pages of the target.
    """
    from tests.render_routes import RENDER_CHECK_PAGES
    proxy, _ = _proxy_url(proc)
    sessions, problems = [], []
    for method, params in calls:
        if method == "<open>":
            sessions.append([])
        elif sessions:
            sessions[-1].append((method, params))
    checks = sessions[1:]          # the first session is the runner's own block
    if len(checks) != len(names):
        problems.append(f"{len(checks)} checks started a DevTools session; the runner lists "
                        f"{len(names)}: {names}")
    for name, session in zip(names, checks):
        if not any(method == "Page.navigate" and params.get("url") != "about:blank"
                   for method, params in session):
            problems.append(f"{name} navigated to no page")
    for page in sorted(_pages_loaded_from(proxy, calls) - set(RENDER_CHECK_PAGES)):
        problems.append(f"{page} is not in the GET walk")
    return problems


def _runner_copy(render_make, edit):
    """Replace the fixture's runner with a copy of the real one, changed by `edit`."""
    text = CHECK.read_text(encoding="utf-8")
    text = text.replace("HERE = Path(__file__).resolve().parent",
                        f"HERE = Path({str(CHECK.parent)!r})")
    runner = render_make.screen / "render_check.py"
    runner.unlink()
    runner.write_text(edit(text), encoding="utf-8")
    return runner


def _add_check(text, line):
    old = '        ("render_power_notice.py", console),\n'
    assert old in text
    return text.replace(old, old + line)


def test_every_check_the_runner_lists_starts_and_its_pages_are_walked(render_make):
    # Driven from the runner's own list: each check it names runs for real against the fake
    # DevTools, must start and navigate, and every page it loads from the target must be walked.
    names = _runner_check_names()
    proc = render_make(real=[CHECK.parent / name for name in names], timeout=300)
    calls = render_make.calls()
    assert _coverage_problems(proc, calls, names) == [], (proc.stdout[-1500:],
                                                         proc.stderr[-1500:])
    assert len(_pages_loaded_from(_proxy_url(proc)[0], calls)) >= 10


def test_a_page_the_walk_lacks_is_caught_however_its_url_is_written(render_make):
    # The case that slipped past the source scan: a check given an unwalked page in a single-
    # quoted f-string. Read from the navigations, it is caught.
    old = '("render_fit.py", f"{console}/lesson/29",'
    runner = _runner_copy(render_make, lambda text: text.replace(
        old, "(\"render_fit.py\", f'{console}/lesson/28',"))
    names = _runner_check_names(runner)
    proc = render_make(real=(FIT,), timeout=120)
    problems = _coverage_problems(proc, render_make.calls(), names)
    assert "/lesson/28 is not in the GET walk" in problems, (problems, proc.stderr)


EXTRA_CHECK = (
    "import sys\n"
    "sys.path.insert(0, {real!r})\n"
    "from render_scroll import connect, fence, leave_page\n"
    "browser = connect()\n"
    "browser.call('Page.navigate', {{'url': fence(sys.argv[1] + '/unwalked')}})\n"
    "leave_page(browser)\n"
)


@pytest.mark.parametrize("present", [True, False], ids=["unwalked-page", "never-started"])
def test_a_check_added_to_the_runner_is_measured_too(render_make, present):
    # The case the hand-kept list missed: a new check in the runner. Whether it loads an
    # unwalked page, or never starts at all, the coverage measure must say so.
    runner = _runner_copy(render_make, lambda text: _add_check(
        text, '        ("render_extra.py", console),\n'))
    if present:
        (render_make.screen / "render_extra.py").write_text(
            EXTRA_CHECK.format(real=str(CHECK.parent)), encoding="utf-8")
    names = _runner_check_names(runner)
    assert names[-1] == "render_extra.py"
    # Absent, it is the one check that cannot start, so every other one runs for real.
    real = () if present else [CHECK.parent / name for name in names[:-1]]
    proc = render_make(real=real, timeout=300)
    problems = _coverage_problems(proc, render_make.calls(), names)
    if present:
        assert "/unwalked is not in the GET walk" in problems, (problems, proc.stderr)
    else:
        assert any("checks started a DevTools session" in p for p in problems), problems


def _fake_browser(tmp_path):
    """A stand-in chromium: makes its profile, writes into it, and sleeps until killed."""
    pidfile = tmp_path / "chromium.pid"
    chromium = tmp_path / "chromium"
    chromium.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "profile = Path(next(a.split('=', 1)[1] for a in sys.argv\n"
        "                    if a.startswith('--user-data-dir=')))\n"
        "profile.mkdir()\n"
        "(profile / 'Default').write_text('in use')\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(120)\n", encoding="utf-8")
    chromium.chmod(0o755)
    return chromium, pidfile


def _kill_fake_browser(pidfile):
    import contextlib
    if pidfile.is_file():
        pid = int(pidfile.read_text())
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


def _runner_in_process(monkeypatch, tmp_path, console, chromium, *, real_block=False, **patches):
    """render_check with a stand-in browser, no real checks and no signal handlers installed.

    Without `real_block` the runner holds no DevTools session of its own.
    """
    import contextlib
    render_check = _load_render_check()
    monkeypatch.setenv("RENDER_URL", console)
    # main() adds loopback to these; the test's own values come back afterwards.
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setattr(render_check.shutil, "which",
                        lambda name: str(chromium) if name == "chromium" else None)
    monkeypatch.setattr(render_check, "chromium_port", lambda browser, profile: "1")
    if not real_block:
        monkeypatch.setattr(render_check, "api_block",
                            lambda port, problems, held: contextlib.nullcontext())
    monkeypatch.setattr(render_check, "run_checks", lambda console, port, output: 0)
    monkeypatch.setattr(render_check, "BROWSER_STOP_S", 0.3)
    monkeypatch.setattr(render_check.signal, "signal", lambda *args: None)
    monkeypatch.setattr(render_check.tempfile, "tempdir", str(tmp_path))
    for name, value in patches.items():
        target, attr = (render_check.os, "killpg") if name == "killpg" else (render_check, name)
        monkeypatch.setattr(target, attr, value)
    return render_check


def _raise(error):
    def fail(*args):
        raise error
    return fail


@pytest.mark.parametrize("patch", [
    {"killpg": _raise(PermissionError(1, "Operation not permitted"))},
    {"_group_running": _raise(OSError(5, "cannot read /proc"))},
], ids=["signal-refused", "verify-fails"])
def test_an_unconfirmed_browser_stop_keeps_the_profile(tmp_path, monkeypatch, console_url, capsys,
                                                       patch):
    # From launch until its exit is verified the browser counts as running. A signal that
    # raises, or a check of the group that raises, leaves it unconfirmed: the profile stays.
    chromium, pidfile = _fake_browser(tmp_path)

    def checks_once_the_browser_is_up(console, port, output):
        for _ in range(200):     # the stand-in has made its profile and is running
            if pidfile.is_file() and pidfile.read_text():
                break
            threading.Event().wait(0.05)
        return 0

    render_check = _runner_in_process(monkeypatch, tmp_path, console_url, chromium,
                                      run_checks=checks_once_the_browser_is_up, **patch)
    try:
        try:
            status = render_check.main()
        except SystemExit as stop:
            status = stop.code
        monkeypatch.undo()
        err = capsys.readouterr().err
        profiles = list(tmp_path.glob("sigint101-render-*/profile"))
        assert status not in (0, None), err
        assert len(profiles) == 1 and (profiles[0] / "Default").is_file(), err
        assert str(profiles[0].parent) in err, err
    finally:
        monkeypatch.undo()
        _kill_fake_browser(pidfile)


@pytest.fixture
def recording_console():
    """A target console that records every request it is sent."""
    hits = []

    class Console(BaseHTTPRequestHandler):
        def _answer(self):
            hits.append((self.command, self.path))
            self.send_response(200 if self.path == "/" or self.path.startswith("/api/") else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Console) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", hits
        finally:
            server.shutdown()
            worker.join()


def _writes_or_api(hits):
    """What reached the target that a render check must never send it: any API path, any write.

    The runner's own reachability check of RENDER_URL is a GET of / and does not count.
    """
    return [hit for hit in hits if hit[0] not in ("GET", "HEAD") or "/api" in hit[1]]


def _run_runner_in_thread(render_check):
    result = []
    runner = threading.Thread(target=lambda: result.append(render_check.main()), daemon=True)
    runner.start()
    return runner, result


def test_devtools_gone_before_blank_with_the_browser_alive_reaches_nothing_at_the_target(
        tmp_path, monkeypatch, recording_console, capsys):
    # Codex's D-121 scenario: DevTools drops the connection before the blank navigation finishes,
    # and the browser stays up. The page is still loaded and posts as soon as no session blocks
    # it, but it only ever had the render proxy's address, which refuses the API and writes.
    import contextlib
    console, hits = recording_console
    fake = FakeDevTools({}, drop_on_blank=True, polls=True)
    chromium, pidfile = _fake_browser(tmp_path)
    loaded = []

    def checks_leave_a_page_loaded(fenced, port, output):
        loaded.append(fenced)
        fake.url = f"{fenced}/lesson/1"
        return 0

    render_check = _runner_in_process(
        monkeypatch, tmp_path, console, chromium, real_block=True,
        chromium_port=lambda browser, profile: str(fake.port),
        run_checks=checks_leave_a_page_loaded)
    try:
        runner, result = _run_runner_in_thread(render_check)
        runner.join(timeout=60)
        assert not runner.is_alive()
        # The page goes on polling after the run, at an address that no longer answers.
        threading.Event().wait(0.5)
        assert _writes_or_api(hits) == [], hits
        assert loaded and not loaded[0].startswith(console), loaded
        assert result == [1], capsys.readouterr().err
    finally:
        fake.browser_gone = True
        _kill_fake_browser(pidfile)
        with contextlib.suppress(OSError):
            fake.close()


def test_a_browser_that_will_not_stop_is_held_until_a_deadline_then_the_run_fails(
        tmp_path, monkeypatch, recording_console, capsys):
    # The page cannot reach about:blank, its target cannot be closed, and the browser group will
    # not stop. The runner holds its DevTools block a while, then gives up at the deadline:
    # the run fails and the unconfirmed profile is kept. Through all of it, and after, the
    # still-loaded page reaches nothing at the target, because it only has the proxy's address.
    import contextlib
    console, hits = recording_console
    fake = FakeDevTools({}, blank_fails=True, errors={"Target.closeTarget"}, polls=True)
    chromium, pidfile = _fake_browser(tmp_path)

    def checks_leave_a_page_loaded(fenced, port, output):
        fake.url = f"{fenced}/lesson/1"
        return 0

    render_check = _runner_in_process(
        monkeypatch, tmp_path, console, chromium, real_block=True,
        killpg=lambda pgid, sig: None, HOLD_S=1.0,
        chromium_port=lambda browser, profile: str(fake.port),
        run_checks=checks_leave_a_page_loaded)
    try:
        runner, result = _run_runner_in_thread(render_check)
        runner.join(timeout=60)
        assert not runner.is_alive(), "the hold has no deadline"
        err = capsys.readouterr().err
        assert result == [1], err
        assert "holding" in err.lower() and "gave up" in err, err
        profiles = list(tmp_path.glob("sigint101-render-*/profile"))
        assert len(profiles) == 1 and (profiles[0] / "Default").is_file(), err
        assert "<closed>" in [method for method, _ in fake.settled()]
        threading.Event().wait(0.5)
        assert _writes_or_api(hits) == [], hits
    finally:
        fake.browser_gone = True
        _kill_fake_browser(pidfile)
        with contextlib.suppress(OSError):
            fake.close()


def test_render_check_keeps_the_profile_and_fails_when_the_browser_will_not_stop(
        tmp_path, monkeypatch, console_url, capsys):
    # A group that outlives SIGTERM and SIGKILL (here: signals that never land) may still be
    # writing into its profile. The run fails, says where the folder is, and leaves it.
    import contextlib
    render_check = _load_render_check()
    pidfile = tmp_path / "chromium.pid"
    chromium = tmp_path / "chromium"
    chromium.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "profile = Path(next(a.split('=', 1)[1] for a in sys.argv\n"
        "                    if a.startswith('--user-data-dir=')))\n"
        "profile.mkdir()\n"
        "(profile / 'Default').write_text('in use')\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n", encoding="utf-8")
    chromium.chmod(0o755)
    monkeypatch.setenv("RENDER_URL", console_url)
    monkeypatch.setattr(render_check.shutil, "which",
                        lambda name: str(chromium) if name == "chromium" else None)
    monkeypatch.setattr(render_check, "chromium_port", lambda browser, profile: "1")
    monkeypatch.setattr(render_check, "api_block",
                        lambda port, problems, held: contextlib.nullcontext())
    monkeypatch.setattr(render_check, "run_checks", lambda console, port, output: 0)
    monkeypatch.setattr(render_check, "BROWSER_STOP_S", 0.3)
    monkeypatch.setattr(render_check.signal, "signal", lambda *args: None)
    monkeypatch.setattr(render_check.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(render_check.os, "killpg", lambda pgid, sig: None)
    try:
        try:
            status = render_check.main()
        except SystemExit as stop:
            status = stop.code
        monkeypatch.undo()
        err = capsys.readouterr().err
        profiles = list(tmp_path.glob("sigint101-render-*/profile"))
        assert status not in (0, None), err
        assert len(profiles) == 1 and (profiles[0] / "Default").is_file(), err
        assert str(profiles[0].parent) in err, err
    finally:
        monkeypatch.undo()
        if pidfile.is_file():
            pid = int(pidfile.read_text())
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 9)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)


def _running(pid):
    """Whether a process is alive; an exited one awaiting its reaper counts as gone."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _cmdline(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace")
    except OSError:
        return ""


def _strays(root):
    """Running processes of this test: every one whose command line names its own tmp folder.

    A test's scratch console, browser and wrapper all carry their folder under the test's
    tmp_path (their data folder, profile or script), which no other process names.
    """
    mine = str(root)
    return [int(entry) for entry in os.listdir("/proc")
            if entry.isdigit() and int(entry) != os.getpid()
            and mine in _cmdline(entry) and _running(int(entry))]


def _stop_strays(root):
    """Kill, by PID, every process of this test that is still running; return what it found.

    Each PID is checked against the test's folder again just before the signal, so a number
    reused by another process in between is never signalled.
    """
    found = []
    for pid in _strays(root):
        command = _cmdline(pid)
        if str(root) in command:
            found.append((pid, command))
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
    for _ in range(200):
        if not any(_running(pid) for pid, _ in found):
            break
        subprocess.run(["sleep", "0.05"])
    return found


@pytest.fixture
def strays(tmp_path):
    """Whatever a test leaves running under its tmp folder (a failing run's console) is stopped."""
    yield
    _stop_strays(tmp_path)


def _signal_scenario(render_make, tmp_path, sig, swept):
    """Start the runner with a scratch console and a forking browser, signal it, check the rest.

    Whatever the outcome, its `finally` stops every process the run left, by PID, and records
    them in `swept`.
    """
    import signal as signals
    pidfile = tmp_path / "browser.pid"
    check = render_make.start(RENDER_URL=None, CHROMIUM_FORKS=str(pidfile),
                              STALL_CHECK=FIT.name)
    try:
        line, started = "", ""
        while not line.startswith("ran "):
            line = check.stdout.readline()
            assert line, check.stderr.read()
            if line.startswith("Started a scratch console"):
                started = line
        console = line.split()[2].rsplit("/", 2)[0]
        port = int(console.rsplit(":", 1)[1])
        # The runner names the console's process, so whoever is left with it can stop it.
        server = re.search(r"\(process (\d+)\)", started)
        assert server, started
        server = int(server.group(1))
        assert "render_console.py" in _cmdline(server) and str(tmp_path) in _cmdline(server)
        check.send_signal(getattr(signals, sig))
        check.wait(timeout=60)
        assert check.returncode != 0
        browser = int(pidfile.read_text())
        wrapper = int(Path(str(pidfile) + ".wrapper").read_text())
        assert not _running(browser), "the browser outlived the check"
        assert not _running(wrapper), "the chromium wrapper outlived the check"
        assert not _running(server), "the scratch console outlived the check"
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        assert list(tmp_path.glob("sigint101-render-*")) == []
    finally:
        if check.poll() is None:
            check.kill()
            check.wait()
        check.stdout.close()
        check.stderr.close()
        swept.extend(_stop_strays(tmp_path))


@pytest.mark.parametrize("sig", ["SIGHUP", "SIGINT", "SIGTERM"])
def test_a_signal_to_the_check_stops_the_scratch_console_and_the_whole_browser(render_make,
                                                                              tmp_path, sig):
    # An ssh drop (SIGHUP), Ctrl-C (SIGINT) or a kill (SIGTERM) reaches only the runner: the
    # console and Chromium run in sessions of their own. The runner must stop both on the way out.
    swept = []
    _signal_scenario(render_make, tmp_path, sig, swept)
    assert swept == [], f"the check left these running: {swept}"


def test_a_failing_signal_test_still_leaves_no_console_running(render_make, tmp_path):
    # A fail-first run is the signal test against a check that does not stop its children. Here
    # that check is this one with its signal handlers and parent-death signals taken out: the
    # test must fail, and must still leave none of the run's processes, the console above all.
    real = CHECK.read_text(encoding="utf-8")
    broken = real
    for old, new in [
        ("HERE = Path(__file__).resolve().parent", f"HERE = Path({str(CHECK.parent)!r})"),
        ("        signal.signal(sig, _stop_on_signal)", "        pass  # no handler"),
        ("preexec_fn=_die_with_parent()", "preexec_fn=None"),
    ]:
        assert old in broken, old
        broken = broken.replace(old, new)
    runner = tmp_path / "tools" / "render" / "render_check.py"
    runner.unlink()
    runner.write_text(broken, encoding="utf-8")
    swept = []
    with pytest.raises(AssertionError):
        _signal_scenario(render_make, tmp_path, "SIGHUP", swept)
    assert any("render_console.py" in command for _, command in swept), swept
    assert _strays(tmp_path) == []


def test_stop_group_signals_the_group_only_while_its_leader_is_unreaped(tmp_path, monkeypatch):
    # The repo's rule (C42): once the leader is reaped its number can be reused, so the group is
    # swept of survivors first and the leader reaped last. A wrapper that dies on SIGTERM leaves a
    # child that ignores it; that child must still be gone when stop_group returns.
    render_check = _load_render_check()
    child_pid = tmp_path / "child.pid"
    child = tmp_path / "stubborn.py"
    child.write_text("import os, signal, sys, time\n"
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                     "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                     "time.sleep(60)\n", encoding="utf-8")
    leader = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time\n"
         "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
         "time.sleep(60)\n", str(child), str(child_pid)],
        start_new_session=True)
    try:
        for _ in range(200):
            if child_pid.is_file() and child_pid.read_text():
                break
            subprocess.run(["sleep", "0.05"])
        signalled = []
        real_killpg = os.killpg

        def killpg(pgid, sig):
            signalled.append((sig, leader.returncode))
            return real_killpg(pgid, sig)

        monkeypatch.setattr(render_check.os, "killpg", killpg)
        render_check.stop_group(leader, 0.5)
        monkeypatch.undo()
        assert leader.returncode is not None, "the leader was never reaped"
        assert signalled, "the group was never signalled"
        assert all(code is None for _, code in signalled), signalled
        assert not _running(int(child_pid.read_text())), "a survivor outlived stop_group"
    finally:
        if leader.poll() is None:
            leader.kill()
            leader.wait()
        if child_pid.is_file() and _running(int(child_pid.read_text())):
            os.kill(int(child_pid.read_text()), 9)


def _listening_addresses(port):
    """The local addresses a socket is listening on at this port, from /proc/net/tcp{,6}."""
    found = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in Path(table).read_text().splitlines()[1:]:
            local, state = row.split()[1], row.split()[3]
            address, hexport = local.rsplit(":", 1)
            if int(hexport, 16) == port and state == "0A":
                found.append(address)
    return found


def test_the_scratch_console_listens_on_loopback_only_and_has_no_radio(tmp_path, strays):
    from urllib.request import urlopen
    render_check = _load_render_check()
    with render_check.scratch_console(tmp_path / "console") as url:
        port = int(url.rsplit(":", 1)[1])
        assert _listening_addresses(port) == ["0100007F"]
        with urlopen(f"{url}/api/hardware", timeout=10) as reply:
            radio = json.load(reply)
    assert radio["present"] is False and radio["state"] == "absent", radio
    assert "render check" in radio["detail"], radio


LOGBOOK_FITS = {"back": True, "doc": [800, 480], "scrolls": True, "lastVisible": True,
                "contrast": [8, 9], "differentMarks": True}
RACK_FITS = {"doc": [800, 480], "visible": True, "stripVisible": True, "target": [76, 72]}
THEMES = ("dark", "sunlight", "red-night")


@pytest.mark.parametrize("defect", [None, "notice", "refusal"])
def test_lora_render_checks_send_and_notice_in_all_themes(devtools, defect):
    answers = {
        f"measureLoRa({json.dumps(theme)}": {
            "theme": theme, "doc": [800, 480], "send": [76, 72],
            "visible": not (defect == "notice" and theme == "red-night"), "shapes": True,
            "refusalVisible": not (defect == "refusal" and theme == "red-night"),
        }
        for theme in THEMES
    }
    proc = run(LORA, "http://127.0.0.1:9", port=devtools(answers))
    assert proc.returncode == int(defect is not None), (proc.stdout, proc.stderr)
    assert _verdicts(proc) == [
        f"VERDICT: {'FAIL' if defect and theme == 'red-night' else 'PASS'} - LoRa send {theme}"
        for theme in THEMES
    ]


def _logbook_pages(*, rack_defect=None, logbook_defect=None):
    pages = {}
    for theme in THEMES:
        rack = {**RACK_FITS, "theme": theme}
        logbook = {**LOGBOOK_FITS, "theme": theme}
        if theme == "red-night":
            rack.update(rack_defect or {})
            logbook.update(logbook_defect or {})
        pages[f"measureRack({json.dumps(theme)})"] = rack
        pages[f"measureLogbook({json.dumps(theme)})"] = logbook
    return pages


def test_logbook_render_checks_all_three_themes(devtools):
    port = devtools(_logbook_pages())
    proc = run(LOGBOOK, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert _verdicts(proc) == ["VERDICT: PASS - rack dark",
                               "VERDICT: PASS - logbook dark",
                               "VERDICT: PASS - rack sunlight",
                               "VERDICT: PASS - logbook sunlight",
                               "VERDICT: PASS - rack red-night",
                               "VERDICT: PASS - logbook red-night"]


@pytest.mark.parametrize("defect", [
    {"back": False}, {"doc": [801, 480]}, {"doc": [800, 481]}, {"scrolls": False},
    {"lastVisible": False}, {"contrast": [4.49, 9]}, {"differentMarks": False},
    {"theme": "dark"},
])
def test_logbook_verdict_fails_when_a_later_theme_does_not_fit(devtools, defect):
    port = devtools(_logbook_pages(logbook_defect=defect))
    proc = run(LOGBOOK, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert _verdicts(proc)[-1] == "VERDICT: FAIL - logbook red-night"


def test_logbook_render_refuses_an_undersized_rack_log_key(devtools):
    port = devtools(_logbook_pages(rack_defect={"target": [76, 71]}))
    proc = run(LOGBOOK, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "VERDICT: FAIL - rack red-night" in _verdicts(proc)


@pytest.mark.parametrize("defect", [{"stripVisible": False}, {"doc": [800, 481]}])
def test_rack_render_fails_if_the_later_theme_overflows_or_blocks_the_strip(devtools, defect):
    port = devtools(_logbook_pages(rack_defect=defect))
    proc = run(LOGBOOK, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert _verdicts(proc)[-2:] == ["VERDICT: FAIL - rack red-night",
                                   "VERDICT: PASS - logbook red-night"]


CARD_SORT_FITS = {
    "doc": [800, 480],
    "back": True,
    "choicesFit": True,
    "choiceHeights": [80, 80, 80],
    "footerFit": True,
    "feedbackFits": True,
    "cardScrolls": False,
    "worstDelta": 0,
    "introFontPx": 18,
    "deckSize": 12,
    "cardsChecked": 12,
    "missesChecked": 12,
}
LAW_FITS = {
    "doc": [800, 480],
    "back": True,
    "scrolls": True,
    "lastVisible": True,
    "sections": 8,
}


def _card_sort_pages(*, sort_defect=None, law_defect=None):
    pages = {}
    for theme in THEMES:
        sort = {**CARD_SORT_FITS, "theme": theme}
        law = {**LAW_FITS, "theme": theme}
        if theme == "red-night":
            sort.update(sort_defect or {})
            law.update(law_defect or {})
        pages[f"measureCardSort({json.dumps(theme)})"] = sort
        pages[f"measureLaw({json.dumps(theme)})"] = law
    return pages


def test_card_sort_render_checks_card_screen_and_law_in_all_three_themes(devtools):
    port = devtools(_card_sort_pages())
    proc = run(CARD_SORT, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert _verdicts(proc) == [
        "VERDICT: PASS - card-sort dark",
        "VERDICT: PASS - law dark",
        "VERDICT: PASS - card-sort sunlight",
        "VERDICT: PASS - law sunlight",
        "VERDICT: PASS - card-sort red-night",
        "VERDICT: PASS - law red-night",
    ]


@pytest.mark.parametrize("defect", [
    {"back": False},
    {"doc": [801, 480]},
    {"doc": [800, 481]},
    {"choicesFit": False},
    {"choiceHeights": [80, 71, 80]},
    {"footerFit": False},
    {"feedbackFits": False},
    {"cardScrolls": True},
    {"worstDelta": 1},
    {"introFontPx": 14},
    {"theme": "dark"},
])
def test_card_sort_render_fails_when_card_screen_overflows_or_shrinks_buttons(devtools, defect):
    port = devtools(_card_sort_pages(sort_defect=defect))
    proc = run(CARD_SORT, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert _verdicts(proc)[-2:] == [
        "VERDICT: FAIL - card-sort red-night",
        "VERDICT: PASS - law red-night",
    ]


@pytest.mark.parametrize("defect", [
    {"back": False},
    {"doc": [800, 481]},
    {"scrolls": False},
    {"lastVisible": False},
    {"sections": 0},
    {"theme": "dark"},
])
def test_card_sort_render_fails_when_law_page_does_not_scroll_or_drops_back(devtools, defect):
    port = devtools(_card_sort_pages(law_defect=defect))
    proc = run(CARD_SORT, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert _verdicts(proc)[-1] == "VERDICT: FAIL - law red-night"


def test_card_sort_render_stubs_fetch_before_clicking_so_no_answers_post():
    # F4: executing measureCardSort against a live console must never POST /api/sort writes into
    # the learner's progress.db. Run the actual CARD_SORT JS under node with a click handler that
    # calls window.fetch and assert zero fetches reach the server.
    node = shutil.which("node")
    assert node, "node is required to test the CARD_SORT browser snippet"
    import ast
    tree = ast.parse(CARD_SORT.read_text(encoding="utf-8"))
    snippet = next(
        node_.value.value
        for node_ in tree.body
        if isinstance(node_, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CARD_SORT" for t in node_.targets)
    )
    harness = f"""
const assert = require("node:assert/strict");
let fetchCalls = 0;
const origFetch = () => {{ fetchCalls += 1; return Promise.resolve({{ ok: true }}); }};
const rect = (h = 80) => ({{
  width: 100, height: h, left: 10, top: 10, right: 110, bottom: 10 + h,
}});
const makeEl = (h = 80) => ({{
  hidden: false, disabled: false, scrollWidth: 100, clientWidth: 100,
  scrollHeight: h, clientHeight: h,
  getBoundingClientRect: () => rect(h),
  click() {{}},
}});
const fb = makeEl(40);
const card = makeEl(140);
const prevBtn = Object.assign(makeEl(76), {{ disabled: true }});
const nextBtn = Object.assign(makeEl(76), {{ disabled: true }});
const choices = [0, 1, 2].map(() => {{
  const el = makeEl(80);
  el.click = () => {{ globalThis.window.fetch("/api/sort?lesson_id=5", {{ method: "POST" }}); }};
  return el;
}});
const footBtns = [prevBtn, nextBtn];
const deck = {{
  textContent: JSON.stringify([{{ key: "one", text: "One card", answer: "legal" }}]),
}};
const text = {{ textContent: "One card" }};
const panel = {{
  hidden: false,
  querySelectorAll(sel) {{
    if (sel === "[data-sort-choice]") return choices;
    if (sel.includes(".stage-foot")) return footBtns;
    return [];
  }},
  querySelector(sel) {{
    if (sel === ".sort-card") return card;
    if (sel === "[data-sort-feedback]") return fb;
    if (sel === "[data-sort-prev]") return prevBtn;
    if (sel === "[data-sort-next]") return nextBtn;
    if (sel === "[data-sort-intro]") return makeEl(22);
    if (sel === "[data-sort-deck]") return deck;
    if (sel === "[data-sort-text]") return text;
    return null;
  }},
}};
globalThis.window = {{ fetch: origFetch }};
globalThis.fetch = origFetch;
globalThis.innerWidth = 800;
globalThis.innerHeight = 480;
globalThis.getComputedStyle = () => ({{
  overflowX: "visible", overflowY: "visible", fontSize: "18px",
}});
globalThis.document = {{
  documentElement: {{ scrollWidth: 800, scrollHeight: 480 }},
  querySelector(sel) {{
    if (sel === ".app") return {{ dataset: {{ theme: "dark" }} }};
    if (sel === ".btn-back") return makeEl(76);
    if (sel.includes("data-sort-panel")) return panel;
    return null;
  }},
  querySelectorAll(sel) {{
    if (sel === ".stage-panel") return [panel];
    return [];
  }},
}};
{snippet}
const result = measureCardSort("dark");
assert.equal(result.cardsChecked, 1);
assert.equal(fetchCalls, 0, "measureCardSort posted to fetch instead of stubbing it first");
assert.equal(globalThis.window.fetch, origFetch, "window.fetch should be restored afterwards");
"""
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- C60: the card-sort check walks the whole deck and measures every card's miss --------------


def _page_snippet(script, name):
    """A tool's in-page JavaScript, the string constant `name` of `script`, as the page runs it."""
    import ast
    tree = ast.parse(script.read_text(encoding="utf-8"))
    return next(node_.value.value for node_ in tree.body
                if isinstance(node_, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in node_.targets))


def _run_page_snippet(harness):
    """Run a page snippet in node over a fake DOM; the JSON the harness prints last."""
    node = shutil.which("node")
    assert node, "node is required to run the render checks' page snippets"
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# A page with M5's card sort, as far as measureCardSort sees it: a deck of `size` cards, card
# steps that may break, and one card whose miss feedback may push the card past its box.
CARD_SORT_PAGE = """
const SIZE = {size}, BREAKS_AFTER = {breaks}, DISABLES_AFTER = {disables}, MISS_ON = {miss_on};
const deck = Array.from({{ length: SIZE }}, (_, i) => ({{
  key: "card-" + i, text: "Situation number " + i,
  answer: ["legal", "illegal", "depends"][i % 3],
}}));
let current = 0;
let picked = null;
const rect = (h, top = 10) => ({{
  width: 100, height: h, left: 10, top, right: 110, bottom: top + h,
}});
const box = (h, top) => ({{
  hidden: false, disabled: false, scrollWidth: 100, clientWidth: 100,
  scrollHeight: h, clientHeight: h, getBoundingClientRect: () => rect(h, top),
}});
// Object.assign would copy a getter's value once; the page's state changes as the check taps.
const extend = (base, more) => Object.defineProperties(base,
                                                       Object.getOwnPropertyDescriptors(more));
const missed = () => picked !== null && picked !== deck[current].answer;
const classes = new Set();
const fb = extend(box(40, 200), {{
  hidden: true, classList: {{ contains: name => classes.has(name) }},
}});
const tag = {{
  get textContent() {{ return picked === null ? "" : (missed() ? "Missed" : "Right"); }},
}};
const text = {{ get textContent() {{ return deck[current].text; }} }};
const card = extend(box(140, 60), {{
  get scrollHeight() {{ return MISS_ON === current && missed() ? 152 : 140; }},
}});
function show(choice) {{
  picked = choice;
  fb.hidden = false;
  classes.clear();
  classes.add(missed() ? "verdict--miss" : "verdict--hit");
}}
const choices = ["legal", "illegal", "depends"].map(key => extend(box(80, 300), {{
  getAttribute: name => (name === "data-sort-choice" ? key : null),
  click() {{ show(key); }},
}}));
const prevBtn = extend(box(76, 400), {{
  get disabled() {{ return current <= 0; }},
  click() {{ if (current > 0) {{ current -= 1; picked = null; fb.hidden = true; }} }},
}});
const nextBtn = extend(box(76, 400), {{
  get disabled() {{
    return current >= SIZE - 1 || (DISABLES_AFTER !== null && current >= DISABLES_AFTER);
  }},
  click() {{
    if (BREAKS_AFTER !== null && current >= BREAKS_AFTER) return;
    if (current < SIZE - 1) {{ current += 1; picked = null; fb.hidden = true; }}
  }},
}});
const panel = {{
  hidden: false,
  querySelectorAll(sel) {{
    if (sel === "[data-sort-choice]") return choices;
    if (sel.includes(".stage-foot")) return [prevBtn, nextBtn];
    return [];
  }},
  querySelector(sel) {{
    if (sel === ".sort-card") return card;
    if (sel === "[data-sort-feedback]") return fb;
    if (sel === "[data-sort-tag]") return tag;
    if (sel === "[data-sort-text]") return text;
    if (sel === "[data-sort-deck]") return {{ textContent: JSON.stringify(deck) }};
    if (sel === "[data-sort-prev]") return prevBtn;
    if (sel === "[data-sort-next]") return nextBtn;
    if (sel === "[data-sort-intro]") return box(22, 50);
    return null;
  }},
}};
globalThis.window = {{ fetch: () => Promise.resolve({{ ok: true }}) }};
globalThis.innerWidth = 800;
globalThis.innerHeight = 480;
globalThis.getComputedStyle = () => ({{
  overflowX: "visible", overflowY: "visible", fontSize: "18px",
}});
globalThis.document = {{
  documentElement: {{ scrollWidth: 800, scrollHeight: 480 }},
  querySelector(sel) {{
    if (sel === ".app") return {{ dataset: {{ theme: "dark" }} }};
    if (sel === ".btn-back") return box(76, 0);
    if (sel.includes("data-sort-panel")) return panel;
    return null;
  }},
  querySelectorAll(sel) {{ return sel === ".stage-panel" ? [panel] : []; }},
}};
{snippet}
console.log(JSON.stringify(measureCardSort("dark")));
"""


def _card_sort_measure(size=12, *, breaks=None, disables=None, miss_on=None):
    js = lambda value: "null" if value is None else json.dumps(value)  # noqa: E731
    harness = CARD_SORT_PAGE.format(size=size, breaks=js(breaks), disables=js(disables),
                                    miss_on=js(miss_on),
                                    snippet=_page_snippet(CARD_SORT, "CARD_SORT"))
    return _run_page_snippet(harness)


def _card_sort_verdicts(devtools, measured):
    pages = _card_sort_pages()
    for theme in THEMES:
        pages[f"measureCardSort({json.dumps(theme)})"] = {**measured, "theme": theme}
    proc = run(CARD_SORT, "http://127.0.0.1:9", port=devtools(pages))
    return proc, [line for line in _verdicts(proc) if "card-sort" in line]


def test_card_sort_render_walks_every_card_and_sees_each_ones_miss(devtools):
    measured = _card_sort_measure()
    assert (measured["deckSize"], measured["cardsChecked"], measured["missesChecked"]) == \
        (12, 12, 12), measured
    proc, verdicts = _card_sort_verdicts(devtools, measured)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert verdicts == [f"VERDICT: PASS - card-sort {theme}" for theme in THEMES]


@pytest.mark.parametrize("page", [{"breaks": 0}, {"disables": 0}, {"breaks": 5}],
                         ids=["next-card-does-nothing", "next-card-disabled", "stops-at-card-6"])
def test_card_sort_render_fails_when_stepping_through_the_deck_breaks(devtools, page):
    # "Next card" that stops working leaves the check on the cards it reached: it must fail, not
    # pass on what it measured there.
    measured = _card_sort_measure(**page)
    assert measured["cardsChecked"] < measured["deckSize"] == 12, measured
    proc, verdicts = _card_sort_verdicts(devtools, measured)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert verdicts == [f"VERDICT: FAIL - card-sort {theme}" for theme in THEMES]


def test_card_sort_render_fails_when_one_cards_miss_feedback_overflows(devtools):
    # The "Missed" feedback is the longest text a card shows. On card 7 it overflows the card;
    # its "Right" feedback fits.
    measured = _card_sort_measure(miss_on=6)
    assert measured["worstDelta"] == 12 and measured["cardScrolls"] is True, measured
    proc, verdicts = _card_sort_verdicts(devtools, measured)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert verdicts == [f"VERDICT: FAIL - card-sort {theme}" for theme in THEMES]


@pytest.mark.parametrize("defect", [
    {"cardsChecked": 1},
    {"cardsChecked": 11},
    {"missesChecked": 11},
    {"deckSize": 0, "cardsChecked": 0, "missesChecked": 0},
    {"deckSize": None},
    {"cardsChecked": None},
    {"missesChecked": None},
], ids=["one-card", "eleven-cards", "a-miss-unseen", "empty-deck", "no-deck-size",
        "no-card-count", "no-miss-count"])
def test_card_sort_verdict_requires_the_whole_deck_and_every_miss(devtools, defect):
    pages = _card_sort_pages(sort_defect={k: v for k, v in defect.items() if v is not None})
    for key in [k for k, v in defect.items() if v is None]:
        pages['measureCardSort("red-night")'].pop(key)
    proc = run(CARD_SORT, "http://127.0.0.1:9", port=devtools(pages))
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert _verdicts(proc)[-2:] == [
        "VERDICT: FAIL - card-sort red-night",
        "VERDICT: PASS - law red-night",
    ]


# --- C69: a fit run measures a predict stage with each chip chosen, too ---------------------------


# A predict stage as far as render_fit's MEASURE sees it: three chips, a Lock it in button whose
# bottom sits at 477 px, or at 481 px while chip `OVERFLOW_WITH` is chosen.
PREDICT_PAGE = """
const OVERFLOW_WITH = {overflow_with};
let chosen = null;
const rect = (left, top, right, bottom) => ({{
  left, top, right, bottom, width: right - left, height: bottom - top,
}});
const plain = props => Object.assign({{
  parentElement: null, scrollWidth: 0, clientWidth: 0, scrollHeight: 0, clientHeight: 0,
  clientLeft: 0, clientTop: 0, textContent: "", getClientRects: () => [1],
  getAttribute: () => null, matches: () => false,
}}, props);
const commit = plain({{
  textContent: "Lock it in", matches: sel => sel === ".btn--primary",
  getBoundingClientRect: () => rect(560, 404, 800,
                                    OVERFLOW_WITH !== null && chosen === OVERFLOW_WITH ? 481 : 477),
}});
const chips = ["A", "B", "C"].map((key, i) => {{
  const label = plain({{ textContent: key + " Choice " + key,
                         getBoundingClientRect: () => rect(i * 260, 310, i * 260 + 250, 400) }});
  return Object.assign(label, {{
    key, querySelector: sel => (sel === ".predict-chip__key" ? {{ textContent: key }} : null),
  }});
}});
const radios = chips.map(chip => ({{
  checked: false, disabled: false,
  closest: sel => (sel === ".predict-chip" ? chip : null),
  dispatchEvent(event) {{
    if (event.type !== "change" || !event.bubbles) throw new Error("not a bubbling change");
    const on = radios.find(radio => radio.checked);
    chosen = on ? on.chip.key : null;
    return true;
  }},
  chip,
}}));
const panel = plain({{
  hidden: false,
  querySelectorAll(sel) {{
    if (sel === "button") return [commit];
    if (sel.includes(".predict-chip")) return chips;
    if (sel.includes('input[type="radio"]')) return radios;
    return [];
  }},
}});
globalThis.window = {{ innerWidth: 800, innerHeight: 480 }};
globalThis.getComputedStyle = () => ({{
  visibility: "visible", overflowX: "visible", overflowY: "visible",
}});
globalThis.document = {{
  querySelectorAll: sel => (sel === ".stage-panel" ? [panel] : []),
  querySelector: sel => (sel.includes('data-stage="predict"') ? panel : null),
}};
const result = {measure};
if (chosen !== null || radios.some(radio => radio.checked)) throw new Error("chips left chosen");
console.log(JSON.stringify(result));
"""


def _predict_measure(overflow_with=None):
    harness = PREDICT_PAGE.format(
        overflow_with="null" if overflow_with is None else json.dumps(overflow_with),
        measure=_page_snippet(FIT, "MEASURE") % "predict")
    return _run_page_snippet(harness)


def test_render_fit_measures_the_predict_stage_with_each_chip_chosen(devtools, tmp_path):
    measured = _predict_measure()
    names = [control["name"] for control in measured["controls"]]
    for key in "ABC":
        assert f"Lock it in with {key} chosen" in names, names
    port = devtools({PREDICT_FIT: measured})
    proc = run(FIT, "http://127.0.0.1:1/lesson/39", str(tmp_path / "predict.png"), "predict",
               port=port)
    assert_ends(proc, 0, "VERDICT: PASS")


def test_render_fit_fails_when_a_chosen_chip_pushes_the_commit_off_the_screen(devtools, tmp_path):
    # Choosing a chip swaps in the longer status line. If that pushes Lock it in past 480 px the
    # run must fail, though the stage fits before a choice.
    measured = _predict_measure(overflow_with="B")
    port = devtools({PREDICT_FIT: measured})
    proc = run(FIT, "http://127.0.0.1:1/lesson/39", str(tmp_path / "predict.png"), "predict",
               port=port)
    assert_ends(proc, 1, "VERDICT: FAIL - predict: Lock it in with B chosen is outside 800x480 "
                         "(560, 404, 800, 481)")
    assert "predict Commit bottom = 481px" in proc.stdout


SURVEY_FITS = {
    "doc": [800, 480],
    "back": True,
    "badge": "CACHED",
    "rows": 11,
    "listScrolls": True,
    "noRowOverflow": True,
    "controlsFit": True,
    "explainScrolls": True,
    "completeVisible": True,
}


def _survey_pages(*, defect=None):
    pages = {}
    for theme in THEMES:
        row = {**SURVEY_FITS, "theme": theme}
        if theme == "red-night":
            row.update(defect or {})
        pages[f"measureSurvey({json.dumps(theme)})"] = row
    return pages


def test_survey_render_checks_populated_panel_in_all_three_themes(devtools):
    port = devtools(_survey_pages())
    proc = run(SURVEY, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert _verdicts(proc) == [
        "VERDICT: PASS - survey dark",
        "VERDICT: PASS - survey sunlight",
        "VERDICT: PASS - survey red-night",
    ]


@pytest.mark.parametrize("defect", [
    {"back": False},
    {"doc": [801, 480]},
    {"doc": [800, 481]},
    {"badge": "LIVE"},
    {"badge": "CACHED · 45s"},
    {"badge": "NO RADIO"},
    {"rows": 0},
    {"listScrolls": False},
    {"noRowOverflow": False},
    {"controlsFit": False},
    {"explainScrolls": False},
    {"completeVisible": False},
    {"theme": "dark"},
])
def test_survey_render_fails_when_populated_panel_overflows_or_stays_empty(devtools, defect):
    port = devtools(_survey_pages(defect=defect))
    proc = run(SURVEY, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert _verdicts(proc)[-1] == "VERDICT: FAIL - survey red-night"


def test_survey_render_never_posts_predict_to_console_and_stubs_fetch():
    # F1: render_survey.py must never POST /api/predict into the console it runs against.
    # First prove the script blocks /api/predict at the network layer and never calls
    # fetch('/api/predict') even when L39 is sealed on the target console.
    import re

    seen_evals = []
    blocked_urls = []

    class RecordingDevTools(FakeDevTools):
        def _result(self, msg):
            method = msg.get("method")
            if method == "Network.setBlockedURLs":
                blocked_urls.extend(msg.get("params", {}).get("urls", []))
            if method == "Runtime.evaluate":
                expr = msg.get("params", {}).get("expression", "")
                seen_evals.append(expr)
                if 'data-stage="operate"' in expr and "measureSurvey" not in expr:
                    return {"result": {"type": "boolean", "value": False}}
            return super()._result(msg)

    fake = RecordingDevTools(_survey_pages())
    try:
        proc = run(SURVEY, "http://127.0.0.1:9", port=fake.port)
    finally:
        fake.close()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    # The shared block (render_scroll.block_console_api) covers /api/predict with the whole API.
    assert "*/api/*" in blocked_urls, blocked_urls
    assert not any(
        re.search(r"\bfetch\s*\(\s*['\"][^'\"]*/api/predict", expr) for expr in seen_evals
    ), f"render_survey.py posted /api/predict into the console: {seen_evals!r}"

    # Second, run the actual fetch_shim under node and prove both /api/predict POSTs and
    # /api/survey GETs are stubbed without ever reaching origFetch.
    node = shutil.which("node")
    assert node, "node is required to test the SURVEY fetch shim"
    sys.path.insert(0, str(SURVEY.parent))
    try:
        import render_survey  # noqa: PLC0415
        shim = render_survey.fetch_shim(render_survey.populated_payload())
    finally:
        sys.path.remove(str(SURVEY.parent))
    harness = f"""
const assert = require("node:assert/strict");
let origCalls = 0;
function FakeResponse(body, init) {{
  this.status = (init && init.status) || 200;
  this.ok = this.status >= 200 && this.status < 300;
  this._body = body;
  this.json = () => Promise.resolve(JSON.parse(body));
}}
globalThis.Response = FakeResponse;
const origFetch = () => {{ origCalls += 1; return Promise.resolve(new FakeResponse("{{}}")); }};
globalThis.window = {{ fetch: origFetch }};
{shim}
(async () => {{
  const pred = await window.fetch("/api/predict?lesson_id=39", {{
    method: "POST",
    body: "prediction=envelope-only",
  }});
  assert.equal(pred.status, 200);
  const survey = await window.fetch("/api/survey");
  const data = await survey.json();
  assert.equal(data.available, true);
  assert.equal(data.cached, true);
  assert.equal("age_s" in data, false);
  assert.equal(origCalls, 0, "fetch_shim let a write or survey request reach the console");
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    node_proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=15)
    assert node_proc.returncode == 0, node_proc.stdout + node_proc.stderr


LIVE_FIND_SCREEN_FITS = {
    "doc": [800, 480],
    "back": True,
    "controlsFit": True,
    "keyHeights": [76, 74, 74, 74],
    "promptFontPx": 20,
    "evidenceFontPx": 18,
    "distinctMarks": True,
}
LIVE_FIND_TILE_FITS = {
    "doc": [800, 480],
    "overflowY": "auto",
    "tilesWithClippedMeta": 0,
    "distinctMarks": True,
}
LIVE_FIND_RACK_FITS = {
    "doc": [800, 480],
    "back": True,
    "backHref": "/unit/4/challenge",
    "backLabel": "Live find",
    "titleUnclipped": True,
}


def _live_find_pages(*, screen_defect=None, tile_defect=None, rack_defect=None):
    pages = {}
    for theme in THEMES:
        for unit in (4, 5):
            tile = {**LIVE_FIND_TILE_FITS, "theme": theme}
            screen = {**LIVE_FIND_SCREEN_FITS, "theme": theme}
            if unit == 5:
                screen["keyHeights"] = [76, 74, 74, 74, 74, 74, 74]
            if theme == "red-night" and unit == 5:
                tile.update(tile_defect or {})
                screen.update(screen_defect or {})
            pages[f"measureFindTile({json.dumps(theme)}, {unit})"] = tile
            pages[f"measureFindScreen({json.dumps(theme)}, {unit})"] = screen
        rack = {**LIVE_FIND_RACK_FITS, "theme": theme}
        if theme == "red-night":
            rack.update(rack_defect or {})
        pages[f"measureFindRack({json.dumps(theme)})"] = rack
    return pages


def test_live_find_render_checks_tiles_screens_and_rack_in_all_three_themes(devtools):
    port = devtools(_live_find_pages())
    proc = run(LIVE_FIND, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert _verdicts(proc) == [
        "VERDICT: PASS - live-find tile u4 dark",
        "VERDICT: PASS - live-find screen u4 dark",
        "VERDICT: PASS - live-find tile u5 dark",
        "VERDICT: PASS - live-find screen u5 dark",
        "VERDICT: PASS - live-find rack dark",
        "VERDICT: PASS - live-find tile u4 sunlight",
        "VERDICT: PASS - live-find screen u4 sunlight",
        "VERDICT: PASS - live-find tile u5 sunlight",
        "VERDICT: PASS - live-find screen u5 sunlight",
        "VERDICT: PASS - live-find rack sunlight",
        "VERDICT: PASS - live-find tile u4 red-night",
        "VERDICT: PASS - live-find screen u4 red-night",
        "VERDICT: PASS - live-find tile u5 red-night",
        "VERDICT: PASS - live-find screen u5 red-night",
        "VERDICT: PASS - live-find rack red-night",
    ]


@pytest.mark.parametrize("kind, defect", [
    ("screen", {"doc": [800, 481]}),
    ("screen", {"back": False}),
    ("screen", {"controlsFit": False}),
    ("screen", {"keyHeights": [76, 71, 74, 74, 74, 74, 74]}),
    ("screen", {"keyHeights": [82, 74, 74, 74, 74, 74, 74]}),
    ("screen", {"promptFontPx": 17}),
    ("screen", {"evidenceFontPx": 16}),
    ("screen", {"distinctMarks": False}),
    ("tile", {"tilesWithClippedMeta": 1}),
    ("tile", {"distinctMarks": False}),
    ("rack", {"back": False}),
    ("rack", {"titleUnclipped": False}),
])
def test_live_find_render_fails_on_overflow_undersized_key_or_matching_marks(
    devtools, kind, defect
):
    kwargs = {f"{kind}_defect": defect}
    port = devtools(_live_find_pages(**kwargs))
    proc = run(LIVE_FIND, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    expected = {
        "screen": "VERDICT: FAIL - live-find screen u5 red-night",
        "tile": "VERDICT: FAIL - live-find tile u5 red-night",
        "rack": "VERDICT: FAIL - live-find rack red-night",
    }[kind]
    assert expected in _verdicts(proc)


def test_recall_render_console_uses_no_radio_probe(tmp_path, monkeypatch):
    # F5: render_recall.py must pass a no-radio probe so it never touches a live SDR bus.
    sys.path.insert(0, str(RECALL.parent))
    try:
        import render_recall  # noqa: PLC0415
    finally:
        sys.path.remove(str(RECALL.parent))

    captured: dict = {}

    def fake_create_app(*args, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            return None

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(render_recall, "create_app", fake_create_app)
    monkeypatch.setattr(render_recall.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(render_recall, "urlopen", lambda *_a, **_kw: FakeResp())

    server, thread, _base = render_recall._run_server(tmp_path, lambda: 1_700_000_000.0)
    render_recall._stop_server(server, thread)

    probe_fn = captured["kwargs"].get("probe")
    assert callable(probe_fn), "render_recall._run_server must pass a no-radio probe"
    probe = probe_fn()
    assert probe.present is False


RACK_FIELD_FITS = {
    "doc": [800, 480],
    "target": [76, 72],
    "visible": True,
    "titleUnclipped": True,
}
FIELD_PAGE_FITS = {
    "doc": [800, 480],
    "back": True,
    "scrolls": True,
    "lastVisible": True,
    "noOverflow": True,
    "noSplitHyphens": True,
    "bodyPx": 18,
    "codePx": 18,
}


def _field_pages(*, page_defect=None, rack_defect=None):
    pages = {}
    for theme in THEMES:
        rack = {**RACK_FIELD_FITS, "theme": theme}
        page = {**FIELD_PAGE_FITS, "theme": theme}
        if theme == "red-night":
            rack.update(rack_defect or {})
            page.update(page_defect or {})
        pages[f"measureRackField({json.dumps(theme)})"] = rack
        pages[f"measureFieldPage({json.dumps(theme)},"] = page
    return pages


def test_field_render_checks_rack_and_page_including_no_split_hyphens(devtools):
    port = devtools(_field_pages())
    proc = run(FIELD, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert _verdicts(proc) == [
        "VERDICT: PASS - rack-field-ism dark",
        "VERDICT: PASS - rack-field-waterfall dark",
        "VERDICT: PASS - field dark",
        "VERDICT: PASS - rack-field-ism sunlight",
        "VERDICT: PASS - rack-field-waterfall sunlight",
        "VERDICT: PASS - field sunlight",
        "VERDICT: PASS - rack-field-ism red-night",
        "VERDICT: PASS - rack-field-waterfall red-night",
        "VERDICT: PASS - field red-night",
    ]


@pytest.mark.parametrize("defect", [
    {"noSplitHyphens": False},
    {"noOverflow": False},
    {"bodyPx": 17},
    {"codePx": 17},
])
def test_field_render_fails_on_split_hyphen_overflow_or_small_font(devtools, defect):
    port = devtools(_field_pages(page_defect=defect))
    proc = run(FIELD, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert _verdicts(proc)[-1] == "VERDICT: FAIL - field red-night"


BANDMAP_HOME_FITS = {
    "doc": [800, 480],
    "bandsKey": [76, 72],
    "bandsInViewport": True,
    "titleUnclipped": True,
    "exitWhole": True,
}
BANDMAP_PAGE_FITS = {
    "doc": [800, 480],
    "back": True,
    "captionSingleLine": True,
    "captionUnclipped": True,
    "keyVisible": True,
    "headingsForcedUpper": False,
    "overviewBounds": [18, 782],
    "entriesClientH": 308,
    "rowCount": 24,
    "minRowH": 76,
    "rowsOverlap": False,
    "clippedLines": 0,
    "replayRadius": "0px",
    "replayText": "REPLAY ONLY",
    "lessonBordersDouble": True,
    "offDialBordersDashed": True,
    "minFontPx": 18,
}
BANDMAP_END_FITS = {
    "scrollTop": 1800,
    "lastOffDialVisible": True,
    "noRadioVisible": True,
}


def _bandmap_pages(*, home_defect=None, page_defect=None, end_defect=None):
    pages = {}
    for theme in THEMES:
        home = {**BANDMAP_HOME_FITS, "theme": theme}
        page = {**BANDMAP_PAGE_FITS, "theme": theme}
        end = {**BANDMAP_END_FITS, "theme": theme}
        if theme == "red-night":
            home.update(home_defect or {})
            page.update(page_defect or {})
            end.update(end_defect or {})
        pages[f"measureBandmapHome({json.dumps(theme)})"] = home
        pages[f"measureBandmapPage({json.dumps(theme)})"] = page
        pages[f"measureBandmapEnd({json.dumps(theme)})"] = end
    return pages


def test_bandmap_render_checks_home_and_bands_in_all_three_themes(devtools):
    port = devtools(_bandmap_pages())
    proc = run(BANDMAP, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert _verdicts(proc) == [
        "VERDICT: PASS - bandmap home dark",
        "VERDICT: PASS - bandmap dark",
        "VERDICT: PASS - bandmap home sunlight",
        "VERDICT: PASS - bandmap sunlight",
        "VERDICT: PASS - bandmap home red-night",
        "VERDICT: PASS - bandmap red-night",
    ]


@pytest.mark.parametrize("kind, defect", [
    ("home", {"doc": [800, 481]}),
    ("home", {"bandsKey": [71, 75]}),
    ("home", {"bandsKey": [76, 71]}),
    ("home", {"bandsInViewport": False}),
    ("home", {"titleUnclipped": False}),
    ("home", {"exitWhole": False}),
    ("page", {"doc": [801, 480]}),
    ("page", {"back": False}),
    ("page", {"captionSingleLine": False}),
    ("page", {"captionUnclipped": False}),
    ("page", {"keyVisible": False}),
    ("page", {"headingsForcedUpper": True}),
    ("page", {"overviewBounds": [17, 782]}),
    ("page", {"overviewBounds": [18, 783]}),
    ("page", {"entriesClientH": 279}),
    ("page", {"minRowH": 71}),
    ("page", {"rowsOverlap": True}),
    ("page", {"clippedLines": 1}),
    ("page", {"replayRadius": "16px"}),
    ("page", {"replayText": "REPLAY"}),
    ("page", {"lessonBordersDouble": False}),
    ("page", {"offDialBordersDashed": False}),
    ("page", {"minFontPx": 16}),
    ("end", {"lastOffDialVisible": False}),
    ("end", {"noRadioVisible": False}),
])
def test_bandmap_render_fails_on_layout_or_shape_defects(devtools, kind, defect):
    kwargs = {f"{kind}_defect": defect}
    port = devtools(_bandmap_pages(**kwargs))
    proc = run(BANDMAP, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    expected = (
        "VERDICT: FAIL - bandmap home red-night"
        if kind == "home"
        else "VERDICT: FAIL - bandmap red-night"
    )
    assert expected in _verdicts(proc)


CLOSING_PREDICT_FITS = {
    "doc": [800, 480],
    "back": True,
    "chipsFit": True,
    "commitFit": True,
    "commitBottom": 477,
}
CLOSING_UNSEALED_FITS = {
    "doc": [800, 480],
    "back": True,
    "codePx": 18,
    "noOverflow": True,
    "cmdVisibleAtTop": True,
    "noSplitHyphens": True,
    "operateFoot": True,
    "observeFoot": True,
    "explainFoot": True,
    "completeVisible": True,
}


def _closing_pages(*, predict_defect=None, stages_defect=None):
    pages = {}
    for theme in THEMES:
        pred = {**CLOSING_PREDICT_FITS, "theme": theme}
        stages = {**CLOSING_UNSEALED_FITS, "theme": theme}
        if theme == "red-night":
            pred.update(predict_defect or {})
            stages.update(stages_defect or {})
        pages[f"measureClosingPredict({json.dumps(theme)})"] = pred
        pages[f"measureClosingUnsealed({json.dumps(theme)},"] = stages
    return pages


def test_closing_render_checks_all_stages_in_all_three_themes(devtools):
    port = devtools(_closing_pages())
    proc = run(CLOSING, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert _verdicts(proc) == [
        "VERDICT: PASS - closing-predict dark",
        "VERDICT: PASS - closing-stages dark",
        "VERDICT: PASS - closing-predict sunlight",
        "VERDICT: PASS - closing-stages sunlight",
        "VERDICT: PASS - closing-predict red-night",
        "VERDICT: PASS - closing-stages red-night",
    ]


@pytest.mark.parametrize("kind, defect", [
    ("predict", {"chipsFit": False}),
    ("predict", {"commitFit": False}),
    ("stages", {"cmdVisibleAtTop": False}),
    ("stages", {"noSplitHyphens": False}),
    ("stages", {"noOverflow": False}),
    ("stages", {"codePx": 17}),
    ("stages", {"completeVisible": False}),
])
def test_closing_render_fails_on_overflow_split_hyphen_or_missing_complete(devtools, kind, defect):
    kwargs = {f"{kind}_defect": defect}
    port = devtools(_closing_pages(**kwargs))
    proc = run(CLOSING, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    expected = (
        "VERDICT: FAIL - closing-predict red-night"
        if kind == "predict"
        else "VERDICT: FAIL - closing-stages red-night"
    )
    assert expected in _verdicts(proc)


POWER_NOTICE_OK = {
    "doc": [800, 480],
    "roleAlert": True,
    "initialHidden": True,
    "countdownOk": True,
    "failedOk": True,
    "hiddenAfterClear": True,
    "aboveFigure": True,
    "countdownH": 68,
    "failedH": 88,
    "noticeTop": 75,
    "backBottom": 75,
    "headlinePx": 20,
    "detailPx": 18,
}


def _power_notice_pages(*, defect=None):
    pages = {}
    for theme in THEMES:
        def _answer(url, t=theme):
            m = {**POWER_NOTICE_OK, "theme": t}
            if t == "red-night" and url.endswith("/exam/review/T6C02") and defect:
                m.update(defect)
            return m
        pages[f"measurePowerNotice({json.dumps(theme)},"] = _answer
    return pages


def test_power_notice_render_checks_all_four_screens_in_all_three_themes(devtools):
    port = devtools(_power_notice_pages())
    proc = run(POWER_NOTICE, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    expected = [
        f"VERDICT: PASS - power-notice {name} {theme}"
        for theme in THEMES
        for name in ("home", "lesson1", "predict29", "figure")
    ]
    assert _verdicts(proc) == expected


@pytest.mark.parametrize("defect", [
    {"countdownOk": False},
    {"failedOk": False},
    {"hiddenAfterClear": False},
    {"aboveFigure": False},
    {"initialHidden": False},
    {"roleAlert": False},
])
def test_power_notice_render_fails_on_banner_or_zindex_defects(devtools, defect):
    port = devtools(_power_notice_pages(defect=defect))
    proc = run(POWER_NOTICE, "http://127.0.0.1:9", port=port)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "VERDICT: FAIL - power-notice figure red-night" in _verdicts(proc)
