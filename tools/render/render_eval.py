# ABOUTME: Load a page at a forced 800x480 over CDP, evaluate a JS expression, print its JSON.
# ABOUTME: Optional third arg saves a screenshot. Usage: render_eval.py URL 'expr' [out.png].
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_scroll import connect  # noqa: E402


def main():
    url, expr = sys.argv[1], sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else None
    c = connect()
    c.call("Page.enable")
    c.call("Network.enable")
    c.call("Network.setCacheDisabled", {"cacheDisabled": True})
    c.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False},
    )
    c.call("Page.navigate", {"url": url})
    time.sleep(2.5)
    for _ in range(20):
        if c.ev("document.readyState") == "complete":
            break
        time.sleep(0.2)
    print(json.dumps(c.ev(expr)))
    if out:
        shot = c.call("Page.captureScreenshot", {"format": "png"})
        with open(out, "wb") as f:
            f.write(base64.b64decode(shot["data"]))


if __name__ == "__main__":
    main()
