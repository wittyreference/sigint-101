# ABOUTME: Runs the layout checks against a scratch SIGINT 101 console it starts, or RENDER_URL.
# ABOUTME: Pages load through a render proxy that refuses the API and writes; all of it stops whole.
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
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

# The repository this script belongs to, for the scratch console's code. Resolved, so a linked
# copy of this script still finds it and the DevTools helpers beside it.
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(REPO))

from render_scroll import connect, leave_page  # noqa: E402

from sigint101.childproc import _die_with_parent  # noqa: E402

# The scratch console: the real app with its own data folder and no radio (render_console.py),
# on a loopback socket bound here. Nothing of the kiosk's console (make run, port 8008) is
# shared: not its learner state, not its server.
CONSOLE = HERE / "render_console.py"
PROXY = HERE / "render_proxy.py"
FENCE_ENV = "RENDER_FENCE"   # render_scroll.fence: pages at this origin are already fenced
CONSOLE_START_S = 60    # a Pi loads every lesson pack and verifies the exam pool before serving
CONSOLE_STOP_S = 15     # the console's own shutdown stands its receivers down within this
BROWSER_STOP_S = 5
PROXY_STOP_S = 5
HOLD_S = 30            # how long a held block waits for a browser that will not stop

# The signals that end a run early: a kill, an ssh session dropping, Ctrl-C, Ctrl-\. Each stops
# the console and Chromium on the way out. SIGKILL cannot be caught; for that case both are
# started with a parent-death signal, so they die with this process.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT)


def _exited(process):
    """Whether the process has exited, peeked at without reaping it (its pid stays reserved)."""
    if process.returncode is not None:
        return True
    try:
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def chromium_port(browser, profile):
    """Wait for Chromium to publish and serve its dynamically allocated DevTools port."""
    opener = build_opener(ProxyHandler({}))
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 10
    while not _exited(browser) and time.monotonic() < deadline:
        try:
            port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
            with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1):
                return str(port)
        except (OSError, ValueError, IndexError):
            time.sleep(0.1)
    raise RuntimeError("Cannot reach Chromium DevTools. Check that headless Chromium can start.")


def _proc_stat(entry):
    """A process's /proc stat line, or None when the process has gone since it was listed.

    Any other failure to read it raises: an unreadable entry is not evidence of an exit.
    """
    try:
        return Path("/proc", entry, "stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None


def _group_running(pgid):
    """The members of a process group that are still running, read from /proc.

    Exited members awaiting their reaper (the leader, kept unreaped, or orphans init will reap)
    do not count: they hold no files open and run no code. A /proc that cannot be listed, or an
    entry that cannot be read or parsed, raises rather than read as an empty group: the caller
    then counts the group's exit as unconfirmed.
    """
    running = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        stat = _proc_stat(entry)
        if stat is None:
            continue
        try:
            fields = stat.rsplit(")", 1)[1].split()
            group, state = int(fields[2]), fields[0]
        except (IndexError, ValueError) as error:
            raise OSError(f"cannot parse /proc/{entry}/stat: {error}") from error
        if group == pgid and state != "Z":
            running.append(int(entry))
    return running


def stop_group(process, grace):
    """Stop a process started in a session of its own, and everything it started, then reap it.

    A chromium wrapper can exit on the signal while the browser it launched keeps writing into
    its profile, so the whole group is signalled: SIGTERM, then SIGKILL for whatever is still
    running after the grace period. The repo's rule (C42): the group is signalled and swept only
    while its leader is unreaped, so its number cannot have been handed to another process. The
    leader is reaped last, with a bounded wait. True only when no member is left running and
    the leader is reaped; otherwise False, with what is left on stderr. A signal the kernel
    refuses also leaves it False; a failure to read the group raises, and the caller counts that
    as unconfirmed too.
    """
    refused = False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if process.returncode is None:      # the leader is unreaped: the group number is ours
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            except OSError as error:
                print(f"could not signal group {process.pid}: {error}", file=sys.stderr)
                refused = True
        deadline = time.monotonic() + grace
        while _group_running(process.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not _group_running(process.pid):
            break
    stopped = not refused
    if survivors := _group_running(process.pid):
        print(f"processes {survivors} of group {process.pid} still run after SIGKILL",
              file=sys.stderr)
        stopped = False
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        print(f"process {process.pid} was not reaped", file=sys.stderr)
        stopped = False
    return stopped


def _launched(process, name, alive):
    """Count a child as running from launch: only a verified stop takes it off `alive`."""
    entry = f"{name} (process group {process.pid}) is still running"
    if alive is not None:
        alive.append(entry)
    return entry


def _stop(process, grace, entry, alive):
    """Stop a child's group; it stays on `alive` unless its exit was verified."""
    try:
        confirmed = stop_group(process, grace)
    except Exception as error:   # a group that cannot be checked is not known to be gone
        print(f"could not confirm that group {process.pid} stopped: {error}", file=sys.stderr)
        confirmed = False
    if confirmed and alive is not None and entry in alive:
        alive.remove(entry)
    return confirmed


def _hold_until_gone(process, entry, alive):
    """Keep the held /api/* block while the browser group lives, up to HOLD_S, then give up.

    Reached only when the page could neither leave the console nor be closed, and the group
    would not stop. The render proxy is the fence that matters: the page never had the
    console's own address, and the proxy refuses the API and every write, so dropping the block
    cannot open the console to it. The DevTools block is the second layer, held a while longer
    in case the group goes. At the deadline the run fails, and the browser stays on `alive`, so
    its profile is kept. Further signals are ignored while it waits.
    """
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    print(f"render-check: Chromium (process group {process.pid}) will not stop and its page "
          f"could not leave the console. Holding its /api/* block for up to {HOLD_S:g} s; end "
          f"it with: kill -9 -{process.pid}", file=sys.stderr, flush=True)
    deadline = time.monotonic() + HOLD_S
    while time.monotonic() < deadline:
        try:
            if not _group_running(process.pid):
                break
        except Exception as error:   # not known to be gone: keep holding until the deadline
            print(f"render-check: cannot read group {process.pid}: {error}", file=sys.stderr)
        time.sleep(0.2)
    else:
        print(f"render-check: gave up holding the block for group {process.pid} after "
              f"{HOLD_S:g} s; its profile is kept.", file=sys.stderr, flush=True)
        return
    _stop(process, BROWSER_STOP_S, entry, alive)   # confirms the group empty and reaps it


@contextmanager
def render_proxy(console, alive=None):
    """Put the render proxy (render_proxy.py) in front of `console`; yield the proxy's address.

    Its own process, in a session of its own with the parent-death signal, on a loopback socket
    bound and held here. It forwards GET and HEAD of pages, and refuses the console's API and
    every write. The checks' browser only ever gets this address. It stops with the run, by its
    process group, after Chromium.
    """
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        command = [sys.executable, str(PROXY), str(listener.fileno()), console]
        try:
            proxy = subprocess.Popen(command, cwd=REPO, stdout=subprocess.DEVNULL,
                                     pass_fds=(listener.fileno(),), start_new_session=True,
                                     preexec_fn=_die_with_parent())
        except OSError as error:
            sys.exit(f"Cannot start the render proxy. ({error})")
    finally:
        listener.close()
    entry = _launched(proxy, "the render proxy", alive)
    try:
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 10
        while True:
            if _exited(proxy):
                sys.exit("The render proxy exited before it answered.")
            try:
                opener.open(Request(f"{url}/", method="HEAD"), timeout=2).close()
                break
            except HTTPError:
                break    # it answered: an upstream error is still the proxy's answer
            except OSError as error:
                if time.monotonic() > deadline:
                    sys.exit(f"The render proxy did not answer at {url}. ({error})")
                time.sleep(0.1)
        print(f"Fenced the console behind a render proxy at {url} (process {proxy.pid}).",
              flush=True)
        yield url
    finally:
        _stop(proxy, PROXY_STOP_S, entry, alive)


@contextmanager
def scratch_console(data_dir, alive=None):
    """Serve a console of this checkout on a free loopback port with `data_dir` as its data.

    The listening socket is bound and held here and handed to the console, so no other server
    can take the port between choosing it and serving on it. Yields the console's address and
    stops the server, whole, when the block ends. From launch the server is on `alive` until its
    exit is verified.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        command = [sys.executable, str(CONSOLE), str(listener.fileno()), str(data_dir)]
        try:
            server = subprocess.Popen(command, cwd=REPO, stdout=subprocess.DEVNULL,
                                      pass_fds=(listener.fileno(),), start_new_session=True,
                                      preexec_fn=_die_with_parent())
        except OSError as error:
            sys.exit(f"Cannot start the scratch console. ({error})")
    finally:
        listener.close()
    entry = _launched(server, "the scratch console", alive)
    try:
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + CONSOLE_START_S
        while True:
            if _exited(server):
                sys.exit("The scratch console exited before it answered.")
            try:
                with opener.open(f"{url}/", timeout=2):
                    break
            except OSError as error:
                if time.monotonic() > deadline:
                    sys.exit(f"The scratch console did not answer at {url}. ({error})")
                time.sleep(0.2)
        # The process is named, so whoever is left with a console the run could not stop (a
        # SIGKILL of this check on a box without parent-death signals) can stop it by PID.
        print(f"Started a scratch console at {url} (process {server.pid}) with its own data "
              "folder.", flush=True)
        yield url
    finally:
        _stop(server, CONSOLE_STOP_S, entry, alive)


@contextmanager
def headless_chromium(chromium, profile, alive=None, held=None):
    """Run Chromium in a session of its own on `profile`; yield its DevTools port.

    From launch the browser is on `alive` until its exit is verified, and while it is the run
    keeps the profile. If a block is still held (`held`: its page could not leave the console)
    and the browser will not stop, this waits for the browser to go rather than return.
    """
    command = [chromium, "--headless=new", "--disable-gpu", "--password-store=basic",
               "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"]
    try:
        browser = subprocess.Popen(command, stdout=subprocess.DEVNULL, start_new_session=True,
                                   preexec_fn=_die_with_parent())
    except OSError as error:
        sys.exit(f"Cannot start Chromium. ({error})")
    entry = _launched(browser, "Chromium", alive)
    try:
        try:
            port = chromium_port(browser, profile)
        except RuntimeError as error:
            sys.exit(str(error))
        yield port
    finally:
        if not _stop(browser, BROWSER_STOP_S, entry, alive) and held:
            _hold_until_gone(browser, entry, alive)


def check_list(console, output):
    """The checks make render-check runs, in order: (script, *its arguments).

    The one list: run_checks runs it, and the tests read it, so a check added here is one the
    route coverage test also starts and measures.
    """
    # Home scroll can be checked on a fresh console. The exam review URL always has a diagram.
    return (
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


def run_checks(console, port, output):
    checks = check_list(console, output)
    no_proxy = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
    no_proxy = f"{no_proxy},127.0.0.1" if no_proxy else "127.0.0.1"
    env = dict(os.environ, CDP_PORT=port, no_proxy=no_proxy, NO_PROXY=no_proxy)
    env[FENCE_ENV] = _origin(console)   # the checks load pages through this proxy only
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


@contextmanager
def api_block(port, problems, held):
    """Hold a DevTools session on the page that blocks /api/* for the whole run.

    Each check's own block ends with its connection, and the next check's begins only when it
    attaches. This one spans the gaps. At the end the page leaves for about:blank, or failing
    that its target is closed (leave_page); either failure fails the run. If neither worked the
    session's socket goes to `held`: it is closed only once the browser group is gone, so the
    block never ends while a console page can still be loaded.
    """
    session = connect(port, exit_check=False)
    try:
        yield
    finally:
        left = leave_page(session)
        if left != "blank":
            problems.append(f"the page could not leave the console for about:blank ({left})")
        if left == "kept":
            held.append(session.s)


def _stop_on_signal(signum, frame):
    # The browser and the scratch console run in sessions of their own, so a signal to this
    # check reaches neither. Exiting through SystemExit stops both on the way out; a second
    # signal while that runs is ignored rather than cutting the teardown short.
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    sys.exit(128 + signum)


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _close_all(sockets):
    for sock in sockets:
        sock.close()


def main():
    for sig in STOP_SIGNALS:
        signal.signal(sig, _stop_on_signal)
    # This process talks to DevTools on loopback too (api_block), never through a proxy.
    no_proxy = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = (
        f"{no_proxy},127.0.0.1" if no_proxy else "127.0.0.1")
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

    output = Path(tempfile.mkdtemp(prefix="sigint101-render-"))
    problems = []   # what fails the run
    alive = []      # what still runs: while anything does, its folder stays
    held = []       # sockets that keep a block until Chromium has stopped
    status = 1
    try:
        # The exit order, last in first out: the page leaves for about:blank, Chromium stops,
        # any held block closes, the render proxy stops, then the console stops.
        with ExitStack() as running:
            if not console:
                console = running.enter_context(scratch_console(output / "console", alive))
            fenced = running.enter_context(render_proxy(console, alive))
            running.callback(_close_all, held)
            port = running.enter_context(
                headless_chromium(chromium, output / "profile", alive, held))
            running.enter_context(api_block(port, problems, held))
            status = run_checks(fenced, port, output)
    finally:
        for problem in problems + alive:
            print(f"render-check: {problem}.", file=sys.stderr)
        if alive:
            print(f"render-check: left {output} in place while that runs; remove it once the "
                  "processes have stopped.", file=sys.stderr)
        else:
            shutil.rmtree(output)
    return 1 if problems or alive else status


if __name__ == "__main__":
    sys.exit(main())
