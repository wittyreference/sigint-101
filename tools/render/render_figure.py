# ABOUTME: Render an exam card with a figure at a forced 800x480 over CDP, then open the diagram.
# ABOUTME: Asserts the card still fits and the overlay's Close bar is on screen; saves PNGs.
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_scroll import CDP, page_ws  # noqa: E402

URL = sys.argv[1]
THEME = sys.argv[2] if len(sys.argv) > 2 else "dark"
OUT = sys.argv[3] if len(sys.argv) > 3 else "/tmp/fig"

CARD = r"""(function(){
  var btn=document.querySelector('.btn-commit')||document.querySelector('.btn--primary');
  var open=document.querySelector('.exam-figure__open');
  var r=open?open.getBoundingClientRect():null;
  return {theme:document.querySelector('.app').getAttribute('data-theme'),
    commitBottom:btn?Math.round(btn.getBoundingClientRect().bottom):-1,
    openW:r?Math.round(r.width):0, openH:r?Math.round(r.height):0,
    docScroll:document.documentElement.scrollHeight, vh:window.innerHeight};
})()"""
OVERLAY = r"""(function(){
  var f=document.querySelector('.exam-figure:target'); if(!f) return {err:'overlay not open'};
  var img=f.querySelector('img').getBoundingClientRect();
  var bar=f.querySelector('.exam-figure__bar').getBoundingClientRect();
  return {imgW:Math.round(img.width), imgH:Math.round(img.height), barBottom:Math.round(bar.bottom),
    barH:Math.round(bar.height), loaded:f.querySelector('img').naturalWidth};
})()"""


def shot(c, path):
    data = c.call("Page.captureScreenshot", {"format": "png"})["data"]
    open(path, "wb").write(base64.b64decode(data))


def main():
    c = CDP(page_ws())
    c.call("Page.enable")
    c.call("Network.enable")
    c.call("Network.setCacheDisabled", {"cacheDisabled": True})
    c.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 800, "height": 480, "deviceScaleFactor": 1, "mobile": False},
    )
    c.call("Page.navigate", {"url": URL})
    time.sleep(2)
    c.ev("localStorage.setItem('sigint101.theme','%s')" % THEME)
    c.call("Page.navigate", {"url": URL})
    time.sleep(2)
    card = c.ev(CARD)
    print("card", json.dumps(card))
    shot(c, OUT + "-card.png")
    c.ev("location.hash='#'+document.querySelector('.exam-figure').id")
    time.sleep(1)
    ov = c.ev(OVERLAY)
    print("overlay", json.dumps(ov))
    shot(c, OUT + "-overlay.png")
    ok = (
        0 < card["commitBottom"] <= 480
        and card["openH"] >= 72
        and card["docScroll"] <= 480
        and isinstance(ov, dict)
        and not ov.get("err")
        and ov["loaded"] > 0
        and ov["barBottom"] <= 480
    )
    print("VERDICT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
