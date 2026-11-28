# ABOUTME: Tests render tools' verdicts and behavior when Chrome disconnects.
# ABOUTME: A fake DevTools endpoint stands in for Chrome; the scripts run as real processes.

import base64
import errno
import json
import os
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

    def __init__(self, answers, *, disconnect_at=None):
        self.answers = answers
        self.disconnect_at = disconnect_at
        # Every DevTools command a check sent, in order, as (method, params).
        self.calls = []
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
        self.calls.append((method, params))
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
def render_make(tmp_path, console_url, devtools):
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
        f"    subprocess.Popen([sys.executable, {str(browser)!r}, profile,\n"
        "                      os.environ['CHROMIUM_FORKS']],\n"
        "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "(Path(profile) / 'DevToolsActivePort').write_text(os.environ['FAKE_CDP_PORT'] + '\\n')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    chromium.chmod(0o755)
    for script in (FIT, SCROLL, FIGURE, LOGBOOK, LORA, CARD_SORT):
        (screen / script.name).write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "from urllib.request import urlopen\n"
            "name = Path(__file__).name\n"
            "assert os.environ['CDP_PORT'] == os.environ['FAKE_CDP_PORT']\n"
            "if name == 'render_scroll.py':\n"
            "    assert sys.argv[2] == 'home'\n"
            "print('ran', name, sys.argv[1], flush=True)\n"
            "if name == 'render_scroll.py' and os.environ.get('PROBE_CONSOLE'):\n"
            "    with urlopen(sys.argv[1], timeout=10) as page:\n"
            "        print('console', page.status, b'SIGINT 101' in page.read(), flush=True)\n"
            "sys.exit(7 if name == os.environ.get('FAILING_CHECK') else 0)\n",
            encoding="utf-8",
        )
    make = shutil.which("make")
    assert make, "the render-check tests require make"

    def run_make(*, has_chromium=True, fit_answers=None, timeout=15, **overrides):
        """Run make render-check; an override of None removes that variable."""
        if not has_chromium:
            chromium.unlink()
        if fit_answers is not None:
            (screen / FIT.name).unlink()
            (screen / FIT.name).symlink_to(FIT)
        env = dict(os.environ, PATH=str(path), RENDER_URL=console_url, TMPDIR=str(tmp_path),
                   FAKE_CDP_PORT=str(devtools(fit_answers or {})),
                   no_proxy="127.0.0.1", NO_PROXY="127.0.0.1")
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return subprocess.run([make, "--no-print-directory", "-f", str(REPO / "Makefile"),
                               "render-check"], cwd=tmp_path, env=env, capture_output=True,
                              text=True, timeout=timeout)

    return run_make


def test_make_render_check_fails_when_render_fit_detects_overflow(render_make):
    proc = render_make(fit_answers={DEMO_FIT: DEMO_FITS, PREDICT_FIT: PREDICT_KEY_BELOW})
    assert proc.returncode != 0, proc.stdout
    assert "render_fit.py exited with status 1" in proc.stderr
    assert _verdicts(proc) == [FIT_CASES["choice-below-screen"][-1]]


@pytest.mark.parametrize(
    "failing", ["", FIT.name, SCROLL.name, FIGURE.name, LOGBOOK.name, LORA.name, CARD_SORT.name],
    ids=[
        "all-pass", "fit-fails", "scroll-fails", "figure-fails",
        "logbook-fails", "lora-fails", "card-sort-fails",
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
    assert len(urls) == 6, proc.stdout
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


def test_the_scratch_console_keeps_its_state_in_its_own_folder_and_stops(tmp_path):
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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
