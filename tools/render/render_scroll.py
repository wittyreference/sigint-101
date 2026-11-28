# ABOUTME: Render a page at a forced 800x480 over CDP and assert the scroll/layout is right.
# ABOUTME: explain: body scrolls + Complete in view; home: tiles unclipped. CDP_PORT picks Chrome.

import atexit
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from render_proxy import serve_in_thread  # noqa: E402

RENDER_CHECK = {
    "name": "scroll",
    "pages": ("/",),
    "scratch": False,
    "order": 20,
    "target": "/",
    "args": ("home",),
}

# The DevTools endpoint comes from the environment, not argv, so other harness scripts can import
# CDP/page_ws without this module misreading their arguments.
HOST, PORT = "127.0.0.1", int(os.environ.get("CDP_PORT", "9223"))
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011/lesson/2"
MODE = sys.argv[2] if len(sys.argv) > 2 else "explain"


def page_ws():
    data = json.load(urllib.request.urlopen(f"http://{HOST}:{PORT}/json"))
    for t in data:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    return json.load(urllib.request.urlopen(f"http://{HOST}:{PORT}/json/new?about:blank"))[
        "webSocketDebuggerUrl"
    ]


# The console's whole API, which a check's pages never reach. A page's own polls start receivers
# and claim the radio, and its posts write learner state: a check measures layout, nothing more.
CONSOLE_API = "*/api/*"
BLANK = "about:blank"


class CDPError(RuntimeError):
    """Chrome answered a DevTools command with a protocol error, or a command was refused here."""


def check_reply(method, msg):
    """The result of a DevTools reply; a protocol error is a failure, never an empty result."""
    if "error" in msg:
        error = msg["error"]
        raise CDPError(f"{method} failed: {error.get('message', error)}")
    return msg.get("result", {})


# The render proxies this process may load pages through: the runner's (named in the
# environment), and any a tool run on its own started for itself (fence). A page loads from no
# other origin, so the browser never holds the console's own address.
FENCE_ENV = "RENDER_FENCE"
_FENCES = {}


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def fenced_origins():
    runner = os.environ.get(FENCE_ENV)
    return set(_FENCES.values()) | ({runner} if runner else set())


def fence(url):
    """The same page, reached through a render proxy (render_proxy.py) instead of directly.

    Under make render-check the console is already the runner's proxy, named in RENDER_FENCE,
    and stays as it is. A tool run on its own starts its own proxy, once per console, which
    lives as long as the tool does.
    """
    origin = _origin(url)
    if origin in fenced_origins():
        return url
    if origin not in _FENCES:
        _FENCES[origin] = serve_in_thread(origin)
    return _FENCES[origin] + url[len(origin):]


def check_navigation(client, method, params):
    """Refuse a navigation before the /api/* block is confirmed, or past the render proxy.

    about:blank is always allowed. Any other page must come through a render proxy (fence):
    the proxy refuses the API and every write, whatever becomes of DevTools.
    """
    if method != "Page.navigate":
        return
    url = (params or {}).get("url")
    if url == BLANK:
        return
    if not getattr(client, "blocked", False):
        raise CDPError("refusing to load a page before the /api/* block is confirmed")
    if _origin(url or "") not in fenced_origins():
        raise CDPError(f"refusing to load {url} directly; pages load through the render proxy")


def block_console_api(c, exit_check=True):
    """Block every console API request on this page for as long as this client is attached.

    Both commands must succeed (a protocol error raises), and only then is the client marked
    blocked, so no page loads before the block is in place. The block belongs to this DevTools
    session: it ends when the connection closes, while the page would stay loaded and polling.
    So the page is also left on about:blank before the client closes (leave_page), at the latest
    when the script exits. One list holds every blocked pattern, and a later
    Network.setBlockedURLs replaces it, so a check never sets its own: this is the one place
    that does.
    """
    c.call("Network.enable")
    c.call("Network.setBlockedURLs", {"urls": [CONSOLE_API]})
    c.blocked = True
    if exit_check:
        atexit.register(_leave_at_exit, c)


def _socket(c):
    # This module's client keeps its socket as `s`, render_fit's as `sock`.
    return getattr(c, "s", None) or getattr(c, "sock", None)


def _leave_at_exit(c):
    """At exit, leave the page; a check whose page could not leave fails, whatever it measured."""
    if leave_page(c) in ("closed", "kept"):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)


def _blank_failure(c):
    """Why the page is not on about:blank, or None once it is."""
    try:
        nav = c.call("Page.navigate", {"url": BLANK})
        if nav.get("errorText"):
            return f"Chrome refused it: {nav['errorText']}"
        where = None
        for _ in range(25):
            where = c.call("Runtime.evaluate",
                           {"expression": "location.href", "returnByValue": True})
            where = where.get("result", {}).get("value")
            if where == BLANK:
                return None
            time.sleep(0.2)
        return f"the page is still on {where}"
    except (OSError, TimeoutError, ValueError, RuntimeError, SystemExit) as error:
        return f"DevTools failed: {error}"


def _target_listed(c):
    """Whether DevTools still lists this client's page target."""
    with urllib.request.urlopen(f"http://{HOST}:{c.port}/json/list", timeout=5) as reply:
        return any(target.get("id") == c.target_id for target in json.load(reply))


def _close_failure(c):
    """Why the page target is not closed, or None once DevTools no longer lists it."""
    if not getattr(c, "target_id", None):
        return "the page target is not known"
    session, c.session = getattr(c, "session", None), None   # a browser-level command
    try:
        c.call("Target.closeTarget", {"targetId": c.target_id})
    except (OSError, TimeoutError, ValueError, RuntimeError, SystemExit) as error:
        return f"DevTools failed: {error}"
    finally:
        c.session = session
    try:
        for _ in range(25):
            if not _target_listed(c):
                return None
            time.sleep(0.2)
    except (OSError, ValueError) as error:
        return f"DevTools could not list its targets: {error}"
    return "the page target is still listed"


def leave_page(c):
    """Leave the console page, and close the connection only once nothing there can poll.

    "blank" once the page is confirmed on about:blank. If it cannot get there, the page target
    itself is closed instead: "closed" once DevTools no longer lists it. Either way the
    connection then closes. "kept", with the reasons on stderr, when neither worked: the
    connection, and with it the block, stays open, never closed with a console page loaded.
    "gone" when DevTools had already dropped the connection (that check has its own failure to
    report). Safe to call twice.
    """
    if hasattr(c, "left_page"):
        return c.left_page
    sock = _socket(c)
    if sock is None or sock.fileno() == -1:
        c.left_page = "gone"
        return c.left_page
    failure = _blank_failure(c)
    if failure is None:
        c.left_page = "blank"
    elif sock.fileno() == -1:
        c.left_page = "gone"
        return c.left_page
    else:
        print(f"Could not leave the console page for about:blank ({failure}).", file=sys.stderr)
        closing = _close_failure(c)
        if closing is None:
            print("Closed the page target instead.", file=sys.stderr)
            c.left_page = "closed"
        else:
            print(f"Could not close the page target either ({closing}). Its /api/* block stays "
                  "until this DevTools connection ends.", file=sys.stderr)
            c.left_page = "kept"
            return c.left_page
    if sock.fileno() != -1:
        sock.close()
    return c.left_page


def connect(port=None, exit_check=True):
    """A CDP client on the browser's one page, attached through the browser endpoint, focused.

    Headless Chromium throttles a tab it thinks is in the background until commands stall, so
    the page is brought to the front with focus emulation on, and a missing page is opened in
    a new window rather than as a background tab. Launch Chromium with --remote-allow-origins=*
    and --password-store=basic: on a box with a desktop keyring running, Chromium otherwise
    blocks its first http page on a keyring password prompt nobody can answer. The console's API
    is blocked before the client is handed back (block_console_api). `port` overrides CDP_PORT.
    A caller that checks leave_page's answer itself passes exit_check=False.
    """
    port = PORT if port is None else int(port)
    version = json.load(urllib.request.urlopen(f"http://{HOST}:{port}/json/version"))
    c = CDP(version["webSocketDebuggerUrl"], port=port)
    targets = c.call("Target.getTargets")["targetInfos"]
    page = next((t for t in targets if t["type"] == "page"), None)
    if page:
        target = page["targetId"]
    else:
        opened = c.call("Target.createTarget", {"url": "about:blank", "newWindow": True})
        target = opened["targetId"]
    attached = c.call("Target.attachToTarget", {"targetId": target, "flatten": True})
    c.session = attached["sessionId"]
    c.target_id = target
    c.call("Page.bringToFront")
    c.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
    block_console_api(c, exit_check)
    return c


class CDP:
    def __init__(self, ws, port=None):
        port = PORT if port is None else port
        path = ws.split(f"{HOST}:{port}", 1)[1]
        # A silent DevTools socket must fail the check, not hang it forever.
        self.s = socket.create_connection((HOST, port), timeout=30)
        k = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {HOST}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        b = b""
        while b"\r\n\r\n" not in b:
            b += self._recv(4096)
        self.buf = b.split(b"\r\n\r\n", 1)[1]
        self.id = 0
        self.session = None
        self.port = port
        self.target_id = None
        self.blocked = False

    def _recv(self, size):
        chunk = self.s.recv(size)
        if not chunk:
            self.s.close()
            sys.exit("Chrome DevTools connection closed")
        return chunk

    def send(self, m, p=None):
        self.id += 1
        msg = {"id": self.id, "method": m, "params": p or {}}
        if self.session:
            msg["sessionId"] = self.session
        pl = json.dumps(msg).encode()
        h = bytearray([0x81])
        n = len(pl)
        mask = os.urandom(4)
        if n < 126:
            h.append(0x80 | n)
        elif n < 65536:
            h.append(0x80 | 126)
            h += struct.pack(">H", n)
        else:
            h.append(0x80 | 127)
            h += struct.pack(">Q", n)
        h += mask
        self.s.sendall(bytes(h) + bytes(c ^ mask[i % 4] for i, c in enumerate(pl)))
        return self.id

    def frame(self):
        def need(n):
            while len(self.buf) < n:
                self.buf += self._recv(65536)

        need(2)
        ln = self.buf[1] & 0x7F
        off = 2
        if ln == 126:
            need(4)
            ln = struct.unpack(">H", self.buf[2:4])[0]
            off = 4
        elif ln == 127:
            need(10)
            ln = struct.unpack(">Q", self.buf[2:10])[0]
            off = 10
        need(off + ln)
        d = self.buf[off : off + ln]
        self.buf = self.buf[off + ln :]
        return d

    def call(self, m, p=None, t=15):
        check_navigation(self, m, p)
        w = self.send(m, p)
        end = time.time() + t
        while time.time() < end:
            msg = json.loads(self.frame().decode("utf-8", "replace"))
            if msg.get("id") == w:
                return check_reply(m, msg)
        raise TimeoutError(m)

    def ev(self, e):
        return (
            self.call("Runtime.evaluate", {"expression": e, "returnByValue": True})
            .get("result", {})
            .get("value")
        )


EXPLAIN = r"""(function(){
  document.querySelectorAll('.stage-panel').forEach(function(p){p.hidden=true;});
  var t=document.querySelector('.stage-panel[data-stage="explain"]');
  if(!t) return {err:'no explain panel (prediction committed?)'};
  t.hidden=false;
  var body=t.querySelector('.stage-panel__body');
  var btn=t.querySelector('[data-complete]')||t.querySelector('.btn--primary');
  var cs=body?getComputedStyle(body):null;
  return {hasBody:!!body, overflowY:cs?cs.overflowY:'(none)', scrollH:body?body.scrollHeight:0,
    clientH:body?body.clientHeight:0, scrollable:body?(body.scrollHeight-body.clientHeight>2):false,
    hasCompleteBtn:!!t.querySelector('[data-complete]'),
    btnBottom:btn?Math.round(btn.getBoundingClientRect().bottom):-1, viewportH:window.innerHeight};
})()"""

HOME = r"""(function(){
  var tiles=document.querySelector('.tiles');
  if(!tiles) return {err:'no .tiles grid'};
  var cs=getComputedStyle(tiles), items=[].slice.call(document.querySelectorAll('.tile'));
  var clipped=[];
  items.forEach(function(t,i){
    var meta=t.querySelector('.tile__meta'); if(!meta) return;
    if(meta.getBoundingClientRect().bottom - t.getBoundingClientRect().bottom > 1) {
      var code=t.querySelector('.tile__code');
      clipped.push(code ? code.textContent : i);
    }
  });
  return {overflowY:cs.overflowY, gridAutoRows:cs.gridAutoRows, tiles:items.length,
    tilesWithClippedMeta:clipped.length, clipped:clipped, scrollH:tiles.scrollHeight,
    clientH:tiles.clientHeight, viewportH:window.innerHeight};
})()"""


def main():
    c = connect()
    c.call("Page.enable")
    c.call("Network.enable")
    c.call("Network.setCacheDisabled", {"cacheDisabled": True})
    c.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False},
    )
    c.call("Page.navigate", {"url": fence(URL)})
    time.sleep(2.5)
    for _ in range(20):
        if c.ev("document.readyState") == "complete":
            break
        time.sleep(0.2)
    r = c.ev(EXPLAIN if MODE == "explain" else HOME)
    print(MODE, "=>", json.dumps(r))
    if not isinstance(r, dict) or r.get("err"):
        print("VERDICT: FAIL -", (r or {}).get("err", "no result"))
        sys.exit(1)
    if MODE == "explain":
        ok = (
            r["scrollable"]
            and r["overflowY"] in ("auto", "scroll")
            and r["hasCompleteBtn"]
            and 0 < r["btnBottom"] <= 480
        )
    else:
        ok = r["overflowY"] in ("auto", "scroll") and r["tilesWithClippedMeta"] == 0
    print("VERDICT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
