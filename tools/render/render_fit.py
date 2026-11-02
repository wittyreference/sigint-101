# ABOUTME: Measures the primary-button bottom of a lesson's demo+predict stages at a forced
# ABOUTME: 800x480 over the DevTools Protocol (stdlib websocket). Also saves a screenshot.
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

HOST, PORT = "127.0.0.1", 9222
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8008/lesson/29"
SHOT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/flipper_m29_predict.png"


def page_ws():
    data = json.load(urllib.request.urlopen(f"http://{HOST}:{PORT}/json"))
    for t in data:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    # No page target yet: open one.
    new = json.load(urllib.request.urlopen(f"http://{HOST}:{PORT}/json/new?about:blank"))
    return new["webSocketDebuggerUrl"]


class CDP:
    def __init__(self, ws_url):
        path = ws_url.split(f"{HOST}:{PORT}", 1)[1]
        self.sock = socket.create_connection((HOST, PORT))
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self._buf = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0

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
                self._buf += self.sock.recv(65536)

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
        want = self._send(method, params)
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self._frame().decode("utf-8", "replace"))
            if msg.get("id") == want:
                return msg.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")


MEASURE = """(function(st){
  document.querySelectorAll('.stage-panel').forEach(function(p){p.hidden=true;});
  var t=document.querySelector('.stage-panel[data-stage="'+st+'"]');
  if(!t) return -1; t.hidden=false;
  var btn=t.querySelector('.btn--primary');
  if(!btn) return -2;
  return Math.round(btn.getBoundingClientRect().bottom);
})(%r)"""


def main():
    cdp = CDP(page_ws())
    cdp.call("Page.enable")
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False},
    )
    cdp.call("Page.navigate", {"url": URL})
    time.sleep(2.5)
    for _ in range(20):
        if cdp.evaluate("document.readyState") == "complete":
            break
        time.sleep(0.2)
    demo = cdp.evaluate(MEASURE % "demo")
    predict = cdp.evaluate(MEASURE % "predict")
    print(f"800x480  demo Next bottom = {demo}px  predict Commit bottom = {predict}px  (max 480)")
    # Screenshot the predict stage (left visible by the last measure).
    shot = cdp.call("Page.captureScreenshot", {"format": "png"})
    if shot.get("data"):
        with open(SHOT, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print(f"screenshot: {SHOT}")


if __name__ == "__main__":
    main()
