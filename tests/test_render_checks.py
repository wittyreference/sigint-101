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
SURVEY = REPO / "tools" / "render" / "render_survey.py"
RECALL = REPO / "tools" / "render" / "render_recall.py"
LIVE_FIND = REPO / "tools" / "render" / "render_live_find.py"

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
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    path = tmp_path / "bin"
    path.mkdir()
    chromium = path / "chromium"
    chromium.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "profile = next(arg.split('=', 1)[1] for arg in sys.argv[1:]\n"
        "               if arg.startswith('--user-data-dir='))\n"
        "if os.environ.get('CHROMIUM_EXITS'):\n"
        "    sys.exit(3)\n"
        "Path(profile).mkdir()\n"
        "(Path(profile) / 'DevToolsActivePort').write_text(os.environ['FAKE_CDP_PORT'] + '\\n')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    chromium.chmod(0o755)
    for script in (FIT, SCROLL, FIGURE, LOGBOOK, LORA, CARD_SORT, SURVEY, RECALL, LIVE_FIND):
        (screen / script.name).write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "name = Path(__file__).name\n"
            "assert os.environ['CDP_PORT'] == os.environ['FAKE_CDP_PORT']\n"
            "if name == 'render_scroll.py':\n"
            "    assert sys.argv[2] == 'home'\n"
            "print('ran', name, sys.argv[1], flush=True)\n"
            "sys.exit(7 if name == os.environ.get('FAILING_CHECK') else 0)\n",
            encoding="utf-8",
        )
    make = shutil.which("make")
    assert make, "the render-check tests require make"

    def run_make(*, has_chromium=True, fit_answers=None, **overrides):
        if not has_chromium:
            chromium.unlink()
        if fit_answers is not None:
            (screen / FIT.name).unlink()
            (screen / FIT.name).symlink_to(FIT)
        env = dict(os.environ, PATH=str(path), RENDER_URL=console_url, TMPDIR=str(tmp_path),
                   FAKE_CDP_PORT=str(devtools(fit_answers or {})),
                   no_proxy="127.0.0.1", NO_PROXY="127.0.0.1")
        env.update(overrides)
        return subprocess.run([make, "--no-print-directory", "-f", str(REPO / "Makefile"),
                               "render-check"], cwd=tmp_path, env=env, capture_output=True,
                              text=True, timeout=15)

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
    assert any("/api/predict" in pattern for pattern in blocked_urls), blocked_urls
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
