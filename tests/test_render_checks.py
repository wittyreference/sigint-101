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
    THROWS. document.readyState is always "complete"; any other expression gives no value.
    `disconnect_at` can end the connection during the handshake or the first reply frame.
    """

    def __init__(self, answers, *, disconnect_at=None, blank_fails=False):
        self.answers = answers
        self.disconnect_at = disconnect_at
        # A navigation to about:blank that Chrome refuses: the page stays where it was.
        self.blank_fails = blank_fails
        # Every DevTools command a check sent, in order, as (method, params). A websocket that
        # closes adds ("<closed>", {}); a GET of /mark/<name> adds ("<mark>", {"name": name}).
        self.calls = []
        self.url = "about:blank"
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
            if path.startswith("/mark/"):
                self.calls.append(("<mark>", {"name": path[len("/mark/"):]}))
                body, status = b"{}", b"200 OK"
            elif path == "/json/version":
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
            self.calls.append(("<closed>", {}))
            return

    def _result(self, msg):
        method, params = msg["method"], msg.get("params", {})
        self.calls.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "page-1"}]}
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
                if value is THROWS:
                    return {"result": {"type": "object", "subtype": "error"},
                            "exceptionDetails": {"text": "Uncaught"}}
                return {"result": {"type": "object", "value": value}}
        return {"result": {"type": "undefined"}}


@pytest.fixture
def devtools():
    made = []

    def make(answers, *, disconnect_at=None, blank_fails=False):
        fake = FakeDevTools(answers, disconnect_at=disconnect_at, blank_fails=blank_fails)
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
    for script in (FIT, SCROLL, FIGURE, LOGBOOK, LORA, CARD_SORT, SURVEY, RECALL, LIVE_FIND):
        (screen / script.name).write_text(
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

    def environment(fit_answers, overrides, blank_fails=False):
        """The check's environment; an override of None removes that variable."""
        env = dict(os.environ, PATH=str(path), RENDER_URL=console_url, TMPDIR=str(tmp_path),
                   FAKE_CDP_PORT=str(devtools(fit_answers or {}, blank_fails=blank_fails)),
                   no_proxy="127.0.0.1", NO_PROXY="127.0.0.1")
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def run_make(*, has_chromium=True, fit_answers=None, timeout=15, blank_fails=False,
                 **overrides):
        """Run make render-check with these stand-ins."""
        if not has_chromium:
            chromium.unlink()
        if fit_answers is not None:
            (screen / FIT.name).unlink()
            (screen / FIT.name).symlink_to(FIT)
        env = environment(fit_answers, overrides, blank_fails)
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
    run_make.calls = lambda: devtools.made[-1].calls
    return run_make


def test_make_render_check_fails_when_render_fit_detects_overflow(render_make):
    proc = render_make(fit_answers={DEMO_FIT: DEMO_FITS, PREDICT_FIT: PREDICT_KEY_BELOW})
    assert proc.returncode != 0, proc.stdout
    assert "render_fit.py exited with status 1" in proc.stderr
    assert _verdicts(proc) == [FIT_CASES["choice-below-screen"][-1]]


@pytest.mark.parametrize(
    "failing",
    [
        "", FIT.name, SCROLL.name, FIGURE.name, LOGBOOK.name, LORA.name, CARD_SORT.name,
        SURVEY.name, RECALL.name, LIVE_FIND.name,
    ],
    ids=[
        "all-pass", "fit-fails", "scroll-fails", "figure-fails",
        "logbook-fails", "lora-fails", "card-sort-fails", "survey-fails",
        "recall-fails", "live-find-fails",
    ],
)
def test_make_render_check_runs_all_checks_and_reports_failure(render_make, console_url, failing):
    proc = render_make(FAILING_CHECK=failing)
    assert proc.stdout.splitlines() == [
        f"ran {FIT.name} {console_url}/lesson/29",
        f"ran {SCROLL.name} {console_url}/",
        f"ran {FIGURE.name} {console_url}/exam/review/T6C02",
        f"ran {LOGBOOK.name} {console_url}",
        f"ran {LORA.name} {console_url}",
        f"ran {CARD_SORT.name} {console_url}",
        f"ran {SURVEY.name} {console_url}",
        f"ran {RECALL.name} {console_url}",
        f"ran {LIVE_FIND.name} {console_url}",
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
    assert len(urls) == 9, proc.stdout
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
}


@pytest.mark.parametrize("tool", API_TOOLS.values(), ids=API_TOOLS.keys())
def test_every_render_tool_blocks_the_console_api_before_it_loads_a_page(devtools, tmp_path,
                                                                         tool):
    # A page's own polls (/api/<receiver>, /api/receptions, ...) start receivers and claim the
    # radio; the rack's and the lessons' posts write learner state. Blocking /api/* before the
    # first navigation keeps a check to layout alone.
    script, args, answers = tool
    run(script, *(arg.format(tmp=tmp_path) for arg in args), port=devtools(answers))
    calls = devtools.made[-1].calls
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
    _leaves_blank_before_closing(devtools.made[-1].calls)


def test_render_check_holds_its_own_api_block_across_every_check(render_make):
    # Between one tool's close and the next one's attach no tool's block exists. The runner's
    # own session blocks /api/* from before the first check until after the last.
    proc = render_make()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    calls = render_make.calls()
    methods = [method for method, _ in calls]
    marks = [i for i, method in enumerate(methods) if method == "<mark>"]
    assert len(marks) == 9, methods
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
    proc = render_make(blank_fails=True, MARK_STOP="1")
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    assert "about:blank" in proc.stderr, proc.stderr
    calls = render_make.calls()
    methods = [method for method, _ in calls]
    stopped = calls.index(("<mark>", {"name": "chromium-stopped"}))
    assert "<closed>" not in methods[:stopped], methods
    assert "<closed>" in methods[stopped:], methods


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
