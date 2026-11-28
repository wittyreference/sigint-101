# ABOUTME: Runs the layout checks against a running SIGINT 101 console.
# ABOUTME: Starts an isolated headless Chromium and reports prerequisite and check failures.
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener, urlopen


def chromium_port(browser, profile):
    """Wait for Chromium to publish and serve its dynamically allocated DevTools port."""
    opener = build_opener(ProxyHandler({}))
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 10
    while browser.poll() is None and time.monotonic() < deadline:
        try:
            port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
            with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1):
                return str(port)
        except (OSError, ValueError, IndexError):
            time.sleep(0.1)
    raise RuntimeError("Cannot reach Chromium DevTools. Check that headless Chromium can start.")


def run_checks(console, port, output):
    # Home scroll can be checked on a fresh console. The exam review URL always has a diagram.
    checks = (
        ("render_fit.py", f"{console}/lesson/29", str(output / "predict.png")),
        ("render_scroll.py", f"{console}/", "home"),
        ("render_figure.py", f"{console}/exam/review/T6C02", "dark", str(output / "figure")),
        ("render_logbook.py", console),
        ("render_lora.py", console),
        ("render_card_sort.py", console),
        ("render_survey.py", console),
        ("render_recall.py", console),
        ("render_live_find.py", console),
        ("render_field.py", console),
        ("render_bandmap.py", console),
    )
    no_proxy = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
    no_proxy = f"{no_proxy},127.0.0.1" if no_proxy else "127.0.0.1"
    env = dict(os.environ, CDP_PORT=port, no_proxy=no_proxy, NO_PROXY=no_proxy)
    status = 0
    for script, *args in checks:
        command = [sys.executable, str(Path(__file__).with_name(script)), *args]
        try:
            result = subprocess.run(command, env=env, timeout=60)
        except subprocess.TimeoutExpired:
            print(f"{script} timed out. Check the console and Chromium DevTools.", file=sys.stderr)
            return status or 1
        if result.returncode:
            print(f"{script} exited with status {result.returncode}", file=sys.stderr)
            status = status or result.returncode
    return status


def main():
    console = os.environ.get("RENDER_URL", "http://127.0.0.1:8008").rstrip("/")
    try:
        with urlopen(f"{console}/", timeout=5):
            pass
    except (OSError, ValueError) as error:
        sys.exit(f"Cannot reach the console at {console}. Start it with make run. ({error})")

    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if not chromium:
        sys.exit("Chromium executable not found. Install Chromium to run the layout checks.")

    with tempfile.TemporaryDirectory(prefix="sigint101-render-") as directory:
        output = Path(directory)
        profile = output / "profile"
        command = [chromium, "--headless=new", "--disable-gpu", "--password-store=basic",
                   "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"]
        try:
            browser = subprocess.Popen(command, stdout=subprocess.DEVNULL)
        except OSError as error:
            sys.exit(f"Cannot start Chromium. ({error})")
        try:
            try:
                port = chromium_port(browser, profile)
            except RuntimeError as error:
                sys.exit(str(error))
            return run_checks(console, port, output)
        finally:
            browser.terminate()
            try:
                browser.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser.kill()
                browser.wait()


if __name__ == "__main__":
    sys.exit(main())
