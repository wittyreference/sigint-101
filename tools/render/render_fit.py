# ABOUTME: Checks that demo and predict controls fit the forced 800x480 DevTools viewport.
# ABOUTME: Reports the primary-button positions and saves a screenshot of the predict stage.

import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

# The real folder of this script, so a linked copy still imports the DevTools helpers beside it.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from render_scroll import (  # noqa: E402
    block_console_api,
    check_navigation,
    check_reply,
    fence,
)

RENDER_CHECK = {
    "name": "fit",
    "pages": ("/lesson/29",),
    "scratch": False,
    "order": 10,
    "target": "/lesson/29",
    "args": ("{output}/predict.png",),
}

HOST, PORT = "127.0.0.1", int(os.environ.get("CDP_PORT", "9222"))
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8008/lesson/29"
SHOT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/flipper_m29_predict.png"


def page_ws():
    with urllib.request.urlopen(f"http://{HOST}:{PORT}/json", timeout=5) as response:
        data = json.load(response)
    for t in data:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    # No page target yet: open one.
    new_page = f"http://{HOST}:{PORT}/json/new?about:blank"
    with urllib.request.urlopen(new_page, timeout=5) as response:
        return json.load(response)["webSocketDebuggerUrl"]


class CDP:
    def __init__(self, ws_url):
        path = ws_url.split(f"{HOST}:{PORT}", 1)[1]
        self.sock = socket.create_connection((HOST, PORT), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self._recv(4096)
        self._buf = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0
        # What the shared block and teardown (render_scroll) need: the endpoint, this page's
        # target (the last part of its DevTools path), and whether its block is confirmed.
        self.port = PORT
        self.target_id = path.rsplit("/", 1)[-1]
        self.blocked = False

    def _recv(self, size):
        chunk = self.sock.recv(size)
        if not chunk:
            self.sock.close()
            sys.exit("Chrome DevTools connection closed")
        return chunk

    def _send(self, method, params=None):
        self._id += 1
        payload = json.dumps({"id": self._id, "method": method, "params": params or {}}).encode()
        header = bytearray([0x81])  # FIN + text
        n = len(payload)
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)
        return self._id

    def _frame(self):
        def need(n):
            while len(self._buf) < n:
                self._buf += self._recv(65536)

        need(2)
        ln = self._buf[1] & 0x7F
        off = 2
        if ln == 126:
            need(4)
            ln = struct.unpack(">H", self._buf[2:4])[0]
            off = 4
        elif ln == 127:
            need(10)
            ln = struct.unpack(">Q", self._buf[2:10])[0]
            off = 10
        need(off + ln)
        data = self._buf[off : off + ln]
        self._buf = self._buf[off + ln :]
        return data

    def call(self, method, params=None, timeout=15):
        check_navigation(self, method, params)
        want = self._send(method, params)
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self._frame().decode("utf-8", "replace"))
            if msg.get("id") == want:
                return check_reply(method, msg)
        raise TimeoutError(method)

    def evaluate(self, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")


MEASURE = """(function(st){
  document.querySelectorAll('.stage-panel').forEach(panel => { panel.hidden = true; });
  const panel = document.querySelector('.stage-panel[data-stage="' + st + '"]');
  if (!panel) return {err: 'missing stage panel'};
  panel.hidden = false;
  if (window.innerWidth !== 800 || window.innerHeight !== 480) {
    return {err: `viewport is ${window.innerWidth}x${window.innerHeight}, expected 800x480`};
  }

  const buttons = [...panel.querySelectorAll('button')].filter(button =>
    button.getClientRects().length && getComputedStyle(button).visibility === 'visible');
  const primary = buttons.find(button => button.matches('.btn--primary'));
  if (!primary) return {err: 'no visible primary button'};
  const chips = panel.querySelectorAll('.predict-chip, .predict-chip__key, .predict-chip__label');

  function isClipped(control, bounds) {
    const style = getComputedStyle(control);
    if (style.visibility !== 'visible' ||
        (style.overflowX !== 'visible' && control.scrollWidth > control.clientWidth) ||
        (style.overflowY !== 'visible' && control.scrollHeight > control.clientHeight)) {
      return true;
    }
    // An overflow container clips to the client box, inside its borders and scrollbars.
    for (let parent = control.parentElement; parent; parent = parent.parentElement) {
      const {overflowX, overflowY} = getComputedStyle(parent);
      if (overflowX === 'visible' && overflowY === 'visible') continue;
      const box = parent.getBoundingClientRect();
      const clipLeft = box.left + parent.clientLeft;
      const clipTop = box.top + parent.clientTop;
      if ((overflowX !== 'visible' &&
           (bounds.left < clipLeft || bounds.right > clipLeft + parent.clientWidth)) ||
          (overflowY !== 'visible' &&
           (bounds.top < clipTop || bounds.bottom > clipTop + parent.clientHeight))) {
        return true;
      }
    }
    return false;
  }

  const controls = [...buttons, ...chips].map(control => {
    const bounds = control.getBoundingClientRect();
    const {left, top, right, bottom} = bounds;
    const label = control.getAttribute('aria-label') || control.textContent;
    const name = label.trim().replace(/\\s+/g, ' ') || control.tagName.toLowerCase();
    return {name, left, top, right, bottom, clipped: isClipped(control, bounds)};
  });
  return {primaryBottom: Math.round(primary.getBoundingClientRect().bottom), controls};
})(%r)"""


def fit_failures(stage, measurement):
    if not isinstance(measurement, dict):
        return [f"{stage}: no measurement"]
    if measurement.get("err"):
        return [f"{stage}: {measurement['err']}"]
    if not measurement.get("controls"):
        return [f"{stage}: no controls measured"]

    failures = []
    for control in measurement["controls"]:
        label = f"{stage}: {control['name']}"
        bounds = (control["left"], control["top"], control["right"], control["bottom"])
        left, top, right, bottom = bounds
        if not (0 <= left < right <= 800 and 0 <= top < bottom <= 480):
            failures.append(f"{label} is outside 800x480 {bounds}")
        elif control["clipped"]:
            failures.append(f"{label} is clipped")
    return failures


def main():
    cdp = CDP(page_ws())
    block_console_api(cdp)
    cdp.call("Page.enable")
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False},
    )
    cdp.call("Page.navigate", {"url": fence(URL)})
    time.sleep(2.5)
    for _ in range(20):
        if cdp.evaluate("document.readyState") == "complete":
            break
        time.sleep(0.2)
    demo = cdp.evaluate(MEASURE % "demo")
    predict = cdp.evaluate(MEASURE % "predict")
    demo_bottom, predict_bottom = (
        result.get("primaryBottom", "?") if isinstance(result, dict) else "?"
        for result in (demo, predict)
    )
    print(f"800x480  demo Next bottom = {demo_bottom}px  predict Commit bottom = "
          f"{predict_bottom}px  (max 480)")
    # Screenshot the predict stage (left visible by the last measure).
    shot = cdp.call("Page.captureScreenshot", {"format": "png"})
    if shot.get("data"):
        with open(SHOT, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print(f"screenshot: {SHOT}")
    failures = fit_failures("demo", demo) + fit_failures("predict", predict)
    if failures:
        print("VERDICT: FAIL - " + " | ".join(failures))
        sys.exit(1)
    print("VERDICT: PASS")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except OSError as error:
        sys.exit(f"Cannot complete fit check: {error}")
