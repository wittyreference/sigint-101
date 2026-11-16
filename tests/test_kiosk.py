# ABOUTME: Tests for Exit SIGINT 101: the exit flag, closing only the kiosk browser, and the route.
# ABOUTME: Process scanning and signalling are doubles at the OS boundary; /proc is read for real.

import os
import re
import signal
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from sigint101 import kiosk
from sigint101.app import create_app
from sigint101.events import read_events
from sigint101.hardware import RadioProbe

FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "lesson_packs"
HOME = Path("/home/learner")
PROFILE_ARG = "--user-data-dir=/home/learner/.config/chromium-kiosk"
_BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _kiosk_tree():
    """A running kiosk as /proc shows it: the browser, its helpers, and an unrelated Chromium."""
    return [
        (101, ["/usr/lib/chromium/chromium", "--kiosk", PROFILE_ARG, "--app=http://127.0.0.1:8008"]),
        (102, ["/usr/lib/chromium/chromium", "--type=zygote", PROFILE_ARG]),
        (103, ["/usr/lib/chromium/chromium", "--type=renderer", PROFILE_ARG]),
        # The learner's own browser on the desktop is not the course window.
        (201, ["/usr/lib/chromium/chromium", "--user-data-dir=/home/learner/.config/chromium"]),
        (301, ["bash", "-c", "pgrep -f user-data-dir=/home/learner/.config/chromium-kiosk"]),
    ]


class _Signals:
    """Records each signal the exit sends, instead of sending it."""

    def __init__(self, gone=()):
        self.sent = []
        self._gone = set(gone)

    def __call__(self, pid, sig):
        if pid in self._gone:
            raise ProcessLookupError(pid)
        self.sent.append((pid, sig))


def _exit(tmp_path, processes, signals):
    return kiosk.request_exit(runtime_dir=tmp_path, home=HOME,
                              processes=lambda: processes, send_signal=signals)


# --- request_exit ---------------------------------------------------------------


def test_exit_flags_the_exit_then_terminates_only_the_kiosk_browser(tmp_path):
    signals = _Signals()
    result = _exit(tmp_path, _kiosk_tree(), signals)
    assert (tmp_path / "sigint101-kiosk-exited").exists()
    # The browser process alone: its helpers go with it, and the learner's own browser stays.
    assert signals.sent == [(101, signal.SIGTERM)]
    assert result.exited is True
    assert result.closed == (101,)
    assert result.flag == tmp_path / "sigint101-kiosk-exited"


def test_exit_finds_the_browser_under_chromiums_rewritten_process_title(tmp_path):
    """Chromium retitles itself: /proc shows its whole command line as ONE space-joined string
    (measured on nomad-b, 2026-11-03), not one argv element per flag."""
    retitled = [
        (101, ["/usr/lib/chromium/chromium --load-extension --kiosk --ozone-platform=wayland "
               f"{PROFILE_ARG} --app=http://127.0.0.1:8008 --noerrdialogs"]),
        (103, ["/usr/lib/chromium/chromium --type=renderer --crashpad-handler-pid=0 "
               f"--enable-crash-reporter=,built on Debian GNU/Linux 12 (bookworm) {PROFILE_ARG} "
               "--lang=en-US"]),
        # A profile whose name merely starts the same is not the kiosk.
        (104, [f"/usr/lib/chromium/chromium {PROFILE_ARG}-old --app=http://127.0.0.1:8008"]),
    ]
    signals = _Signals()
    result = _exit(tmp_path, retitled, signals)
    assert signals.sent == [(101, signal.SIGTERM)]
    assert result.closed == (101,)


def test_the_flag_is_written_before_any_browser_is_signalled(tmp_path):
    """The watchdog reads 'no kiosk alive' as a crash unless the flag is already there."""
    flag_seen = []

    def send(pid, sig):
        flag_seen.append((tmp_path / "sigint101-kiosk-exited").exists())

    kiosk.request_exit(runtime_dir=tmp_path, home=HOME,
                       processes=_kiosk_tree, send_signal=send)
    assert flag_seen == [True]


def test_exit_with_no_kiosk_browser_says_so_and_still_flags_the_exit(tmp_path):
    """On the dev server (or with the course already closed) there is nothing to close."""
    signals = _Signals()
    others = [p for p in _kiosk_tree() if PROFILE_ARG not in p[1]]
    result = _exit(tmp_path, others, signals)
    assert signals.sent == []
    assert result.exited is False
    assert result.closed == ()
    assert "no kiosk browser is running" in result.reason
    assert PROFILE_ARG in result.reason
    assert (tmp_path / "sigint101-kiosk-exited").exists()


def test_a_browser_that_is_already_gone_is_not_counted_as_closed(tmp_path):
    result = _exit(tmp_path, _kiosk_tree(), _Signals(gone={101}))
    assert result.exited is False
    assert result.closed == ()


def test_the_flag_lives_in_the_session_runtime_dir(tmp_path, monkeypatch):
    """XDG_RUNTIME_DIR is a tmpfs, so a reboot clears the flag and the course comes back."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert kiosk.exit_flag() == tmp_path / "sigint101-kiosk-exited"


def test_without_a_runtime_dir_the_flag_falls_back_to_the_users_run_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert kiosk.exit_flag() == Path(f"/run/user/{os.getuid()}") / "sigint101-kiosk-exited"


def test_proc_cmdlines_reads_real_processes_including_this_one():
    found = dict(kiosk.proc_cmdlines())
    assert os.getpid() in found
    assert found[os.getpid()][0] == sys.executable or "python" in found[os.getpid()][0]


def test_proc_cmdlines_skips_entries_that_are_not_processes(tmp_path):
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "cmdline").write_bytes(b"chromium\0--kiosk\0")
    (tmp_path / "8").mkdir()  # exited between the listing and the read: no cmdline left
    (tmp_path / "self").mkdir()
    (tmp_path / "9").mkdir()
    (tmp_path / "9" / "cmdline").write_bytes(b"")  # a kernel thread has an empty cmdline
    assert kiosk.proc_cmdlines(proc=tmp_path) == [(7, ["chromium", "--kiosk"])]


# --- POST /api/kiosk/exit ---------------------------------------------------------


def _client(tmp_path, processes, signals):
    def kiosk_exit():
        return kiosk.request_exit(runtime_dir=tmp_path, home=HOME,
                                  processes=lambda: processes, send_signal=signals)

    app = create_app(pack_dir=FIXTURE_PACKS, data_dir=tmp_path,
                     probe=lambda: RadioProbe(False, "lsusb", "no RTL2832U on USB bus"),
                     kiosk_exit=kiosk_exit)
    return TestClient(app)


def test_the_exit_route_closes_the_kiosk_browser(tmp_path):
    signals = _Signals()
    with _client(tmp_path, _kiosk_tree(), signals) as client:
        resp = client.post("/api/kiosk/exit")
    assert resp.status_code == 200
    assert resp.json()["exited"] is True
    assert resp.json()["closed"] == [101]
    assert signals.sent == [(101, signal.SIGTERM)]
    events = [e for e in read_events(tmp_path / "events.jsonl") if e["kind"] == "kiosk_exit"]
    assert events and events[-1]["payload"]["closed"] == [101]


def test_the_exit_route_from_a_browser_form_goes_home_once_closed(tmp_path):
    """Usually the kiosk is gone before it reads this; any other browser lands home."""
    with _client(tmp_path, _kiosk_tree(), _Signals()) as client:
        resp = client.post("/api/kiosk/exit", headers=_BROWSER, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_the_exit_route_with_nothing_to_close_is_an_honest_refusal(tmp_path):
    with _client(tmp_path, [], _Signals()) as client:
        resp = client.post("/api/kiosk/exit")
    assert resp.status_code == 409
    assert resp.json()["exited"] is False
    assert "no kiosk browser is running" in resp.json()["reason"]


def test_a_browser_with_nothing_to_close_gets_a_themed_page_not_json(tmp_path):
    with _client(tmp_path, [], _Signals()) as client:
        resp = client.post("/api/kiosk/exit", headers=_BROWSER)
    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("text/html")
    assert 'class="app"' in resp.text
    assert "no kiosk browser is running" in resp.text


# --- the Exit control on home -------------------------------------------------------


def test_home_swaps_back_for_exit_sigint_101(tmp_path):
    with _client(tmp_path, [], _Signals()) as client:
        html = client.get("/").text
    form = re.search(r'<form[^>]*class="statusbar__exit"[^>]*>(.*?)</form>', html, re.S)
    assert form, "home carries the Exit form in the status bar"
    assert 'method="post"' in form.group(0) and 'action="/api/kiosk/exit"' in form.group(0)
    assert re.search(r'<button[^>]*type="submit"', form.group(1))
    assert re.search(r"Exit SIGINT 101", form.group(1))
    # Home's Back used to link to home itself; it is gone there.
    assert 'class="btn-back"' not in html


def test_other_screens_keep_back(tmp_path):
    with _client(tmp_path, [], _Signals()) as client:
        for path in ("/unit/1", "/lesson/1", "/exam", "/operate/waterfall"):
            html = client.get(path).text
            assert re.search(r'<a class="btn-back" href="', html), path
            assert "/api/kiosk/exit" not in html, path
