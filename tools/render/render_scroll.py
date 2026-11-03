# ABOUTME: Render a page at a forced 800x480 over CDP and assert the scroll/layout is right.
# ABOUTME: explain: body scrolls + Complete in view; home: tiles unclipped. CDP_PORT picks Chrome.
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

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


def connect():
    """A CDP client on the browser's one page, attached through the browser endpoint, focused.

    Headless Chromium throttles a tab it thinks is in the background until commands stall, so
    the page is brought to the front with focus emulation on, and a missing page is opened in
    a new window rather than as a background tab. Launch Chromium with --remote-allow-origins=*
    and --password-store=basic: on a box with a desktop keyring running, Chromium otherwise
    blocks its first http page on a keyring password prompt nobody can answer.
    """
    version = json.load(urllib.request.urlopen(f"http://{HOST}:{PORT}/json/version"))
    c = CDP(version["webSocketDebuggerUrl"])
    targets = c.call("Target.getTargets")["targetInfos"]
    page = next((t for t in targets if t["type"] == "page"), None)
    if page:
        target = page["targetId"]
    else:
        opened = c.call("Target.createTarget", {"url": "about:blank", "newWindow": True})
        target = opened["targetId"]
    attached = c.call("Target.attachToTarget", {"targetId": target, "flatten": True})
    c.session = attached["sessionId"]
    c.call("Page.bringToFront")
    c.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
    return c


class CDP:
    def __init__(self, ws):
        path = ws.split(f"{HOST}:{PORT}", 1)[1]
        # A silent DevTools socket must fail the check, not hang it forever.
        self.s = socket.create_connection((HOST, PORT), timeout=30)
        k = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        b = b""
        while b"\r\n\r\n" not in b:
            b += self.s.recv(4096)
        self.buf = b.split(b"\r\n\r\n", 1)[1]
        self.id = 0
        self.session = None

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
                self.buf += self.s.recv(65536)

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
        w = self.send(m, p)
        end = time.time() + t
        while time.time() < end:
            msg = json.loads(self.frame().decode("utf-8", "replace"))
            if msg.get("id") == w:
                return msg.get("result", {})
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
    c.call("Page.navigate", {"url": URL})
    time.sleep(2.5)
    for _ in range(20):
        if c.ev("document.readyState") == "complete":
            break
        time.sleep(0.2)
    r = c.ev(EXPLAIN if MODE == "explain" else HOME)
    print(MODE, "=>", json.dumps(r))
    if not isinstance(r, dict) or r.get("err"):
        print("VERDICT: FAIL -", (r or {}).get("err", "no result"))
        return
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


if __name__ == "__main__":
    main()
