# ABOUTME: Runs the layout checks against a scratch SIGINT 101 console it starts, or RENDER_URL.
# ABOUTME: Starts an isolated headless Chromium, stops it whole, and reports check failures.
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.request import ProxyHandler, build_opener, urlopen

# The repository this script belongs to, for the scratch console's code. Resolved, so a linked
# copy of this script still finds it.
REPO = Path(__file__).resolve().parents[2]

# The scratch console: the real app with its own data folder, on a port picked for it. Nothing
# of the kiosk's console (make run, port 8008) is shared: not its learner state, not its server.
SERVE = (
    "import sys\n"
    "import uvicorn\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from sigint101.app import create_app\n"
    "uvicorn.run(create_app(data_dir=sys.argv[2]), host='127.0.0.1', port=int(sys.argv[3]),\n"
    "            log_level='warning')\n"
)
CONSOLE_START_S = 60    # a Pi loads every lesson pack and verifies the exam pool before serving
CONSOLE_STOP_S = 15     # the console's own shutdown stands its receivers down within this
BROWSER_STOP_S = 5


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


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def stop_group(process, grace):
    """Stop a process started in a session of its own, and everything it started, then reap it.

    A chromium wrapper can exit on the signal while the browser it launched keeps writing into
    its profile. So the whole process group is signalled, and this returns only once no member
    is left: a polite SIGTERM first, then SIGKILL for whatever outlives the grace period.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _signal_group(process.pid, sig)
        deadline = time.monotonic() + grace
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        while time.monotonic() < deadline:
            if not _group_alive(process.pid):
                return
            time.sleep(0.05)
    process.wait()
    deadline = time.monotonic() + grace
    while _group_alive(process.pid):
        if time.monotonic() > deadline:
            print(f"processes of group {process.pid} survived SIGKILL", file=sys.stderr)
            return
        time.sleep(0.05)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def scratch_console(data_dir):
    """Serve a console of this checkout on a free loopback port with `data_dir` as its data.

    Yields the console's address and stops the server, whole, when the block ends.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    command = [sys.executable, "-c", SERVE, str(REPO), str(data_dir), str(port)]
    try:
        server = subprocess.Popen(command, cwd=REPO, stdout=subprocess.DEVNULL,
                                  start_new_session=True)
    except OSError as error:
        sys.exit(f"Cannot start the scratch console. ({error})")
    try:
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + CONSOLE_START_S
        while True:
            if server.poll() is not None:
                sys.exit(f"The scratch console exited with status {server.returncode}.")
            try:
                with opener.open(f"{url}/", timeout=2):
                    break
            except OSError as error:
                if time.monotonic() > deadline:
                    sys.exit(f"The scratch console did not answer at {url}. ({error})")
                time.sleep(0.2)
        yield url
    finally:
        stop_group(server, CONSOLE_STOP_S)


@contextmanager
def headless_chromium(chromium, profile):
    """Run Chromium in a session of its own on `profile`; yield its DevTools port.

    The profile is never removed while any process of that session is still running.
    """
    command = [chromium, "--headless=new", "--disable-gpu", "--password-store=basic",
               "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"]
    try:
        browser = subprocess.Popen(command, stdout=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        sys.exit(f"Cannot start Chromium. ({error})")
    try:
        try:
            port = chromium_port(browser, profile)
        except RuntimeError as error:
            sys.exit(str(error))
        yield port
    finally:
        stop_group(browser, BROWSER_STOP_S)


def run_checks(console, port, output):
    # Home scroll can be checked on a fresh console. The exam review URL always has a diagram.
    checks = (
        ("render_fit.py", f"{console}/lesson/29", str(output / "predict.png")),
        ("render_scroll.py", f"{console}/", "home"),
        ("render_figure.py", f"{console}/exam/review/T6C02", "dark", str(output / "figure")),
        ("render_logbook.py", console),
        ("render_lora.py", console),
        ("render_card_sort.py", console),
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


def _stop_on_sigterm(signum, frame):
    # The browser and the scratch console run in sessions of their own, so a signal to this
    # check reaches neither. Exiting through SystemExit stops both on the way out.
    sys.exit(128 + signum)


def main():
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    # With no RENDER_URL the checks measure a scratch console started here, never the kiosk's.
    console = os.environ.get("RENDER_URL", "").strip().rstrip("/")
    if console:
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
        # Both stop before the folder holding their state is removed.
        with ExitStack() as running:
            if not console:
                console = running.enter_context(scratch_console(output / "console"))
                print(f"Started a scratch console at {console} with its own data folder.",
                      flush=True)
            port = running.enter_context(headless_chromium(chromium, output / "profile"))
            return run_checks(console, port, output)


if __name__ == "__main__":
    sys.exit(main())
