# ABOUTME: Tests for the desktop launchers in deploy/kiosk and deploy/install.sh, run as real bash.
# ABOUTME: chromium, pgrep, curl, sleep, sudo and systemctl are stubs on PATH (the OS boundary).

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "deploy" / "kiosk" / "start-kiosk.sh"
INSTALL = REPO / "deploy" / "install.sh"


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/bin/bash\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)


class Box:
    """A throwaway home, runtime dir and PATH for one run of a deploy script."""

    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.home.mkdir()
        # A unix socket path must stay under ~108 bytes, so the runtime dir is short.
        self.runtime = Path(tempfile.mkdtemp(prefix="k", dir="/tmp"))
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "calls.log"
        self.log.touch()
        self.alive = tmp_path / "kiosk-alive"
        self.flag = self.runtime / "sigint101-kiosk-exited"
        self.socket = None
        # chromium: record the launch. pgrep: answer from a file the test controls.
        _stub(self.bin, "chromium", f'echo "chromium $*" >> {self.log}')
        _stub(self.bin, "pgrep", f'echo "pgrep $*" >> {self.log}; [ -e {self.alive} ]')
        _stub(self.bin, "curl", "exit 0")
        # No output size to read unless a test says so (the host's own wlr-randr never answers).
        _stub(self.bin, "wlr-randr", "exit 1")
        _stub(self.bin, "systemctl", f'echo "systemctl $*" >> {self.log}')
        _stub(self.bin, "loginctl", f'echo "loginctl $*" >> {self.log}')
        # sudo records the system step and runs nothing.
        _stub(self.bin, "sudo", f'echo "sudo $*" >> {self.log}')
        # sleep ends the desktop session, so a watch loop runs exactly one pass.
        _stub(self.bin, "sleep", f'echo "sleep $*" >> {self.log}; rm -f {self.runtime}/wayland-9')
        # The desktop theme (install.sh step 5) talks to fontconfig and GSettings.
        _stub(self.bin, "gsettings", f'echo "gsettings $*" >> {self.log}')
        _stub(self.bin, "fc-cache", "exit 0")
        _stub(self.bin, "fc-list", 'echo "PT Mono"; echo "Russo One"')
        _stub(self.bin, "pkill", f'echo "pkill $*" >> {self.log}')
        # Nothing a test starts writes to the host's journal.
        _stub(self.bin, "systemd-cat", f'echo "systemd-cat $*" >> {self.log}; shift 2; exec "$@"')

    def start_session(self):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(self.runtime / "wayland-9"))

    def env(self, **extra):
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
               "XDG_RUNTIME_DIR": str(self.runtime), "USER": "learner"}
        if self.socket is not None:
            env["WAYLAND_DISPLAY"] = "wayland-9"
        env.update(extra)
        return env

    def run(self, script, *args, timeout=20, **extra):
        return subprocess.run(["bash", str(script), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=timeout)

    def calls(self, prefix):
        return [ln for ln in self.log.read_text().splitlines() if ln.startswith(prefix)]

    def launches(self, wait=1.0):
        """Chromium is started in the background, so give its stub a moment to log."""
        end = time.time() + wait
        while time.time() < end:
            found = self.calls("chromium")
            if found:
                return found
            time.sleep(0.05)
        return self.calls("chromium")

    def close(self):
        """Also runs when a test fails: end anything still holding this box's watchdog lock, such
        as a watchdog install.sh started, so no test leaves a process behind."""
        if self.socket is not None:
            self.socket.close()
        lock = str(self.runtime / "sigint101-kiosk-watch.lock")
        for fd in Path("/proc").glob("[0-9]*/fd/*"):
            try:
                if os.readlink(fd) != lock:
                    continue
                pid = int(fd.parts[2])
                if pid != os.getpid():
                    os.kill(pid, 9)
            except (OSError, ValueError):
                pass
        shutil.rmtree(self.runtime, ignore_errors=True)


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    b.close()


# --- open: the desktop icon and menu entry ---------------------------------------


def test_open_clears_the_exit_flag_and_launches_the_kiosk_browser(box):
    box.flag.write_text("exit requested\n")
    result = box.run(LAUNCHER, "open")
    assert result.returncode == 0, result.stderr
    assert not box.flag.exists()
    [launch] = box.launches()
    # The kiosk lessons in .claude/learnings.md, all kept.
    for arg in ("--kiosk", "--ozone-platform=wayland",
                f"--user-data-dir={box.home}/.config/chromium-kiosk",
                "--app=http://127.0.0.1:8008", "--password-store=basic", "--disk-cache-size=1"):
        assert arg in launch.split(), arg


def test_open_does_not_launch_a_second_browser_when_the_course_is_already_open(box):
    box.alive.touch()
    box.flag.write_text("exit requested\n")
    result = box.run(LAUNCHER, "open")
    assert result.returncode == 0, result.stderr
    assert not box.flag.exists()
    assert box.launches(wait=0.3) == []
    # The watchdog's own check: keyed on the kiosk profile, bracketed so it never matches itself.
    assert box.calls("pgrep") == [f"pgrep -f [u]ser-data-dir={box.home}/.config/chromium-kiosk"]


# --- watch: the XDG autostart entry -----------------------------------------------


def test_watch_relaunches_the_course_when_no_kiosk_is_alive_and_no_exit_was_asked(box):
    box.start_session()
    result = box.run(LAUNCHER, "watch")
    assert result.returncode == 0, result.stderr
    assert len(box.launches()) == 1
    assert box.calls("sleep") == ["sleep 10"]


def test_watch_never_reopens_the_course_after_a_deliberate_exit(box):
    box.start_session()
    box.flag.write_text("exit requested\n")
    result = box.run(LAUNCHER, "watch")
    assert result.returncode == 0, result.stderr
    assert box.launches(wait=0.3) == []
    assert box.flag.exists()


def test_watch_leaves_a_running_course_alone(box):
    box.start_session()
    box.alive.touch()
    result = box.run(LAUNCHER, "watch")
    assert result.returncode == 0, result.stderr
    assert box.launches(wait=0.3) == []


def test_watch_without_a_desktop_session_exits_and_launches_nothing(box):
    """Run from a console (no Wayland), it must not loop launching a browser with no display."""
    result = box.run(LAUNCHER, "watch")
    assert result.returncode != 0
    assert "no Wayland session" in result.stderr
    assert box.launches(wait=0.3) == []
    assert box.calls("sleep") == []


def test_a_second_watchdog_in_the_same_session_stands_down(box):
    box.start_session()
    import fcntl
    with open(box.runtime / "sigint101-kiosk-watch.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = box.run(LAUNCHER, "watch")
    assert result.returncode == 0, result.stderr
    assert box.launches(wait=0.3) == []
    assert box.calls("sleep") == []


# --- the watchdog's lock stays with the watchdog ----------------------------------
# On the reference Recon a restarted watchdog's `flock -n` failed and it exited without a word: a
# process the killed watchdog had started still held the lock it took on fd 9.


def _lock(box):
    return box.runtime / "sigint101-kiosk-watch.lock"


def _holding(box, name, body):
    """A stub that first notes, in holders.log, whether it has the watchdog's lock file open."""
    probe = (f'find /proc/$$/fd -maxdepth 1 -lname "{_lock(box)}" 2>/dev/null | grep -q . && '
             f'echo "{name}" >> "{box.log.parent / "holders.log"}"')
    _stub(box.bin, name, probe + "\n" + body)


def _holders(box):
    path = box.log.parent / "holders.log"
    return sorted(set(path.read_text().split())) if path.exists() else []


def test_no_process_the_watchdog_starts_holds_its_lock(box):
    """Every command a watch pass runs, on the slowest path: the server is down at first (curl
    fails, systemctl starts it), no kiosk is alive (the output size is read, the saved bounds are
    cleared, Chromium is started), then the pass sleeps."""
    up = box.log.parent / "server-up"
    _holding(box, "curl", f'[ -e {up} ] && exit 0; touch {up}; exit 7')
    _holding(box, "systemctl", f'echo "systemctl $*" >> {box.log}')
    _holding(box, "pgrep", f'echo "pgrep $*" >> {box.log}; [ -e {box.alive} ]')
    _holding(box, "wlr-randr", "exit 1")
    _holding(box, "python3", "cat >/dev/null; exit 0")
    _holding(box, "chromium", f'echo "chromium $*" >> {box.log}')
    _holding(box, "sleep", f'echo "sleep $*" >> {box.log}; rm -f {box.runtime}/wayland-9')
    prefs = _prefs(box)
    prefs.parent.mkdir(parents=True)
    prefs.write_text('{"browser": {"window_placement": {}}}', encoding="utf-8")
    box.start_session()
    result = box.run(LAUNCHER, "watch")
    assert result.returncode == 0, result.stderr
    assert box.launches(), "the pass never reached the launch"
    assert box.calls("systemctl") and box.calls("sleep") == ["sleep 10"]
    holders = _holders(box)
    assert holders == [], f"these inherited the lock: {' '.join(holders)}"


def _wait_for(check, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child the test has not reaped yet is a zombie: it is gone.
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _lock_is_free(box):
    import fcntl
    with open(_lock(box), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(f, fcntl.LOCK_UN)
        return True


class _LongSleep:
    """The watchdog's `sleep 10`, made long and real while the hold file exists, so a test can
    kill the watchdog in the middle of it. Without the hold file it ends the session."""

    def __init__(self, box):
        self.box = box
        self.hold = box.log.parent / "hold"
        self.pids = box.log.parent / "sleep-pids"
        real = shutil.which("sleep")
        _stub(box.bin, "sleep",
              f'if [ -e {self.hold} ]; then echo $$ >> {self.pids}; exec {real} 30; fi\n'
              f'echo "sleep $*" >> {box.log}; rm -f {box.runtime}/wayland-9')
        self.hold.touch()

    def first_pid(self, timeout=10.0):
        assert _wait_for(lambda: self.pids.exists() and self.pids.read_text().strip(), timeout)
        return int(self.pids.read_text().split()[0])

    def stop_all(self):
        if self.pids.exists():
            for pid in self.pids.read_text().split():
                try:
                    os.kill(int(pid), 9)
                except ProcessLookupError:
                    pass


def test_a_restarted_watchdog_takes_the_lock_while_the_killed_ones_sleep_lives(box):
    box.alive.touch()  # the course is open, so each pass only checks and sleeps
    box.start_session()
    sleeper = _LongSleep(box)
    old = subprocess.Popen(["bash", str(LAUNCHER), "watch"], env=box.env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        sleep_pid = sleeper.first_pid()
        assert not _lock_is_free(box)
        old.kill()
        old.wait(timeout=10)
        assert _alive(sleep_pid), "the old watchdog's sleep must still be running for this test"
        sleeper.hold.unlink()
        result = box.run(LAUNCHER, "watch")
        assert result.returncode == 0, result.stderr
        # It took the lock and ran its pass, while the old sleep still ran.
        assert box.calls("sleep") == ["sleep 10"], result.stderr
        assert _alive(sleep_pid)
    finally:
        if old.poll() is None:
            old.kill()
        old.wait(timeout=10)
        sleeper.stop_all()


def test_a_watchdog_that_finds_the_lock_held_says_so_and_names_the_holder(box):
    box.alive.touch()
    box.start_session()
    sleeper = _LongSleep(box)
    first = subprocess.Popen(["bash", str(LAUNCHER), "watch"], env=box.env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        sleeper.first_pid()
        second = box.run(LAUNCHER, "watch")
        assert second.returncode == 0, second.stderr
        assert box.calls("sleep") == []
        [line] = second.stderr.splitlines()
        assert line.startswith("sigint101-course: ") and "already" in line, line
        assert str(first.pid) in line, line
    finally:
        first.kill()
        first.wait(timeout=10)
        sleeper.stop_all()


def test_a_watchdog_killed_while_it_reads_the_output_size_frees_its_lock_at_once(box):
    """Reading the output size is shell work under the lock. Done in a forked $(...) shell, that
    shell kept the lock's close-on-exec backup until wlr-randr ended, so a new watchdog could not
    start for that long after the old one was killed."""
    box.start_session()  # no kiosk alive, so the pass launches one and reads the output size
    asked, real_sleep = box.log.parent / "wlr-asked", shutil.which("sleep")
    _wlr_randr(box, f"echo $$ > {asked}; exec {real_sleep} 4")
    old = _watchdog(box, LAUNCHER)
    try:
        assert _wait_for(lambda: asked.exists() and asked.read_text().strip())
        old.kill()
        old.wait(timeout=10)
        assert _lock_is_free(box), "something the killed watchdog forked still holds its lock"
    finally:
        _end(old)
        _kill_all(_pids(asked))


# --- the kiosk window covers the whole panel, whatever the profile saved ----------
# On the reference panel a reopened kiosk came back as the 780x460 window its profile had saved,
# leaving a 20 px strip of the desktop showing down the right edge and along the bottom.


def _arg(launch, name):
    [value] = [a.split("=", 1)[1] for a in launch.split() if a.startswith(name + "=")]
    return value


def _wlr_randr(box, body):
    _stub(box.bin, "wlr-randr", body)


PANEL = """HDMI-A-1 "Panel (HDMI-A-1)"
  Enabled: yes
  Modes:
    640x480 px, 59.940002 Hz
    800x480 px, 59.977001 Hz (preferred, current)
  Position: 0,0
  Transform: {transform}
  Scale: {scale}
"""


def test_launch_sets_the_window_bounds_itself_at_the_panel_size(box):
    """With no output size to read, the bounds are the reference panel's own 800x480 at 0,0."""
    _wlr_randr(box, "exit 1")
    result = box.run(LAUNCHER, "open")
    assert result.returncode == 0, result.stderr
    [launch] = box.launches()
    assert "--kiosk" in launch.split() and "--start-fullscreen" in launch.split()
    assert _arg(launch, "--window-position") == "0,0"
    assert _arg(launch, "--window-size") == "800,480"


@pytest.mark.parametrize("transform, scale, size", [
    ("normal", "1.000000", "800,480"),
    ("normal", "2.000000", "400,240"),
    ("90", "1.000000", "480,800"),
], ids=["panel", "scaled", "rotated"])
def test_launch_sizes_the_window_to_the_output_wlr_randr_reports(box, transform, scale, size):
    _wlr_randr(box, "cat <<'EOF'\n" + PANEL.format(transform=transform, scale=scale) + "EOF")
    result = box.run(LAUNCHER, "open")
    assert result.returncode == 0, result.stderr
    assert _arg(box.launches()[0], "--window-size") == size


def test_launch_skips_a_disabled_output_for_the_enabled_one(box):
    other = 'HDMI-A-2 "TV"\n  Enabled: no\n  Modes:\n    1920x1080 px, 60.000000 Hz (current)\n'
    _wlr_randr(box, "cat <<'EOF'\n" + other + PANEL.format(transform="normal", scale="1.0")
               + "EOF")
    result = box.run(LAUNCHER, "open")
    assert result.returncode == 0, result.stderr
    assert _arg(box.launches()[0], "--window-size") == "800,480"


@pytest.mark.parametrize("body", ['echo "garbage"', "exit 0", 'echo "x" >&2; exit 3'],
                         ids=["garbage", "silent", "failing"])
def test_an_unreadable_output_size_falls_back_to_the_panel(box, body):
    _wlr_randr(box, body)
    result = box.run(LAUNCHER, "open")
    assert result.returncode == 0, result.stderr
    assert _arg(box.launches()[0], "--window-size") == "800,480"


def test_a_wlr_randr_that_ignores_term_cannot_hold_up_the_launch(box):
    """The output size is read in a subshell that holds the watchdog's lock while it runs, so a
    hung wlr-randr must be killed, not only asked to stop."""
    real_sleep = shutil.which("sleep")
    _wlr_randr(box, f"trap '' TERM; exec {real_sleep} 60")
    start = time.time()
    result = box.run(LAUNCHER, "open")
    assert result.returncode == 0, result.stderr
    assert _arg(box.launches()[0], "--window-size") == "800,480"
    assert time.time() - start < 12


def _mode(mode, transform="normal", scale="1.000000"):
    return (f'HDMI-A-1 "P"\n  Enabled: yes\n  Modes:\n    {mode} px, 60.000000 Hz (current)\n'
            f"  Transform: {transform}\n  Scale: {scale}\n")


@pytest.mark.parametrize("text, size", [
    (_mode("800x480", scale="0.010000"), "800,480"),
    (_mode("80000x48000"), "800,480"),
    (_mode("200x100"), "800,480"),
    (_mode("9999999x480"), "800,480"),
    (_mode("7680x4320"), "7680,4320"),
    (_mode("3840x2160", transform="270"), "2160,3840"),
    (_mode("320x240"), "320,240"),
], ids=["tiny-scale", "absurd-mode", "too-small", "too-many-digits", "8k", "4k-rotated", "qvga"])
def test_an_output_size_no_screen_has_falls_back_to_the_panel_and_real_ones_stay(box, text,
                                                                                   size):
    _wlr_randr(box, "cat <<'EOF'\n" + text + "EOF")
    result = box.run(LAUNCHER, "open")
    assert result.returncode == 0, result.stderr
    assert _arg(box.launches()[0], "--window-size") == size


def _prefs(box):
    return box.home / ".config" / "chromium-kiosk" / "Default" / "Preferences"


def test_launch_forgets_the_window_bounds_the_profile_saved_and_keeps_the_rest(box):
    prefs = _prefs(box)
    prefs.parent.mkdir(parents=True)
    saved = {
        "browser": {
            "window_placement": {"left": 0, "top": 0, "right": 780, "bottom": 460,
                                 "maximized": False, "work_area_left": 0, "work_area_top": 0,
                                 "work_area_right": 800, "work_area_bottom": 480},
            "app_window_placement": {"1_/": {"left": 10, "top": 10, "right": 790,
                                             "bottom": 470}},
            "has_seen_welcome_page": True,
        },
        "profile": {"name": "Kiosk", "exit_type": "Normal"},
        "intl": {"selected_languages": "en-US,en"},
        "zoom": 1.25,
    }
    prefs.write_text(json.dumps(saved), encoding="utf-8")
    # Not 0600, which a fresh temporary file gets anyway: the mode must be copied across.
    prefs.chmod(0o644)
    result = box.run(LAUNCHER, "open")
    assert result.returncode == 0, result.stderr
    assert box.launches()
    after = json.loads(prefs.read_text(encoding="utf-8"))
    assert "window_placement" not in after["browser"]
    assert "app_window_placement" not in after["browser"]
    del saved["browser"]["window_placement"], saved["browser"]["app_window_placement"]
    assert after == saved
    assert prefs.stat().st_mode & 0o777 == 0o644
    # Written beside it and renamed over it: no temporary file is left behind.
    assert sorted(p.name for p in prefs.parent.iterdir()) == ["Preferences"]


def test_watch_forgets_the_saved_window_bounds_before_it_relaunches(box):
    prefs = _prefs(box)
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({"browser": {"window_placement": {"right": 780}}}),
                     encoding="utf-8")
    box.start_session()
    result = box.run(LAUNCHER, "watch")
    assert result.returncode == 0, result.stderr
    assert len(box.launches()) == 1
    assert json.loads(prefs.read_text(encoding="utf-8")) == {"browser": {}}


@pytest.mark.parametrize("text", ['{"browser": {"window_placement": {"right": 780', "[1, 2]",
                                  '{"browser": "odd"}', ""],
                         ids=["truncated", "not-an-object", "odd-browser", "empty"])
def test_a_malformed_preferences_file_does_not_stop_the_launch(box, text):
    prefs = _prefs(box)
    prefs.parent.mkdir(parents=True)
    prefs.write_text(text, encoding="utf-8")
    result = box.run(LAUNCHER, "open")
    assert result.returncode == 0, result.stderr
    [launch] = box.launches()
    assert _arg(launch, "--window-size") == "800,480"
    assert prefs.read_text(encoding="utf-8") == text


def test_an_unknown_mode_is_refused(box):
    result = box.run(LAUNCHER, "sideways")
    assert result.returncode == 2
    assert "usage" in result.stderr


# --- install.sh -------------------------------------------------------------------


HOOK = """
# >>> sigint101 kiosk >>>
if [ "$(tty)" = /dev/tty1 ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  exec "$HOME/.local/bin/sigint101-kiosk"
fi
# <<< sigint101 kiosk <<<
"""


def test_install_places_the_desktop_pieces_and_retires_the_tty1_kiosk(box):
    profile = box.home / ".profile"
    profile.write_text("# the learner's own profile\nexport EDITOR=nano\n" + HOOK, encoding="utf-8")
    old = box.home / ".local" / "bin" / "sigint101-kiosk"
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/bash\n")
    for _ in range(2):  # idempotent: the second run changes nothing and does not fail
        result = box.run(INSTALL)
        assert result.returncode == 0, result.stderr
    text = profile.read_text()
    assert "sigint101" not in text
    assert "export EDITOR=nano" in text and "# the learner's own profile" in text
    assert not old.exists()
    kiosk = REPO / "deploy" / "kiosk"
    assert (box.home / ".config/autostart/sigint101.desktop").read_bytes() == \
        (kiosk / "sigint101-autostart.desktop").read_bytes()
    assert (box.home / "Desktop/sigint101.desktop").read_bytes() == \
        (kiosk / "sigint101-link.desktop").read_bytes()
    sudo = box.calls("sudo")
    icon = "/usr/local/share/icons/hicolor/scalable/apps/sigint101.svg"
    for src, dest in (("start-kiosk.sh", "/usr/local/bin/sigint101-course"),
                      ("sigint101.desktop", "/usr/local/share/applications/sigint101.desktop"),
                      ("sigint101.svg", icon)):
        assert any(str(kiosk / src) in c and c.endswith(dest) for c in sudo), (src, sudo)
    # The server unit, linger and the journal stay as they were.
    assert (box.home / ".config/systemd/user/sigint101.service").exists()
    assert "systemctl --user enable --now sigint101.service" in box.calls("systemctl")
    assert any("loginctl enable-linger" in c for c in sudo)
    assert any("journald" in c for c in sudo)


def test_install_with_no_profile_at_all_still_succeeds(box):
    result = box.run(INSTALL)
    assert result.returncode == 0, result.stderr
    assert not (box.home / ".profile").exists()


# --- install.sh hands the running watchdog over to the launcher it installs -----------
# On the reference Recon a watchdog that had run for two weeks kept the old launcher in memory, so
# after a deploy the kiosk relaunched at the old 780x460 until someone restarted the watchdog.


INSTALLED_LAUNCHER = "/usr/local/bin/sigint101-course"


def _installed_launcher(box):
    """What install.sh's (stubbed) sudo would have put at /usr/local/bin: the checkout's launcher,
    marked so the test can tell its runs from the old watchdog's. setsid runs it in its place."""
    path = box.log.parent / "sigint101-course"
    first, rest = LAUNCHER.read_text(encoding="utf-8").split("\n", 1)
    path.write_text(f'{first}\necho "launcher installed $*" >> {box.log}\n{rest}',
                    encoding="utf-8")
    path.chmod(0o755)
    _stub(box.bin, "setsid",
          f'echo "setsid $*" >> {box.log}\n'
          '[ "$1" = -f ] && shift\n'
          'args=()\n'
          f'for a in "$@"; do [ "$a" = {INSTALLED_LAUNCHER} ] && a={path}; args+=("$a"); done\n'
          '"${args[@]}" </dev/null >/dev/null 2>&1 &')
    return path


def _fake_kiosk(box):
    """A stand-in for the open kiosk Chromium: it has the watchdog's lock file open, as a browser
    an older launcher started would, and notes every signal it can catch."""
    signals = box.log.parent / "kiosk-signals"
    real_sleep = shutil.which("sleep")
    script = (f'exec 8<"{_lock(box)}"\n'
              'for s in TERM INT HUP QUIT USR1 USR2 ALRM PIPE CONT; do\n'
              f'  trap "echo $s >> {signals}" "$s"\n'
              'done\n'
              f'while :; do {real_sleep} 0.1; done')
    proc = subprocess.Popen(["bash", "-c", script, "chromium", "--kiosk",
                             f"--user-data-dir={box.home}/.config/chromium-kiosk",
                             "--app=http://127.0.0.1:8008"])
    return proc, signals


def _install(box):
    """install.sh run from a copy of the checkout in the box. Its deploy/kiosk/start-kiosk.sh is
    the checkout launcher install.sh accepts a watchdog as running (besides the installed one at
    /usr/local/bin, which a test cannot write), so a test can put an older launcher there."""
    deploy = box.log.parent / "checkout" / "deploy"
    if not deploy.exists():
        shutil.copytree(REPO / "deploy", deploy, symlinks=True)
        # The desktop theme step installs the course's bundled fonts.
        fonts = deploy.parent / "sigint101" / "static" / "fonts"
        fonts.parent.mkdir(parents=True)
        fonts.symlink_to(REPO / "sigint101" / "static" / "fonts")
    return deploy / "install.sh"


def _checkout_launcher(box):
    return _install(box).parent / "kiosk" / "start-kiosk.sh"


def _quiet_desktop(box):
    """Only the watchdog's kiosk check finds anything: no labwc or bar for the theme to reload."""
    _stub(box.bin, "pgrep", f'echo "pgrep $*" >> {box.log}\n'
                            'case "$*" in\n'
                            f'  *"[u]ser-data-dir="*) [ -e {box.alive} ] ;;\n'
                            '  *) exit 1 ;;\n'
                            'esac')


class _Deploy:
    """A running watchdog (the old launcher, mid-sleep), an open kiosk, then install.sh."""

    def __init__(self, box):
        self.box = box
        box.alive.touch()  # the course is open
        _quiet_desktop(box)
        box.start_session()
        self.sleeper = _LongSleep(box)
        _installed_launcher(box)
        self.old = subprocess.Popen(["bash", str(_checkout_launcher(box)), "watch"],
                                    env=box.env(), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        self.kiosk = None
        try:
            self.sleeper.first_pid()
            self.kiosk, self.signals = _fake_kiosk(box)
            assert _wait_for(lambda: any(
                os.path.realpath(f"/proc/{self.kiosk.pid}/fd/{fd}") == str(_lock(box))
                for fd in os.listdir(f"/proc/{self.kiosk.pid}/fd")))
            self.sleeper.hold.unlink()  # sleeps from here on end the session
            self.result = box.run(_install(box))
        except BaseException:
            self.close()
            raise

    def close(self):
        for proc in (self.old, self.kiosk):
            if proc is not None and proc.poll() is None:
                proc.kill()
            if proc is not None:
                proc.wait(timeout=10)
        self.sleeper.stop_all()


@pytest.fixture
def deploy(box):
    d = _Deploy(box)
    yield d
    d.close()


def test_install_restarts_the_running_watchdog_on_the_launcher_it_installed(deploy, box):
    result = deploy.result
    assert result.returncode == 0, result.stderr
    # The old watchdog was stopped, by its PID.
    deploy.old.wait(timeout=10)
    assert box.calls("pkill") == []
    # A watchdog on the installed launcher took over in the same desktop session: it took the
    # lock and ran its pass.
    [started] = box.calls("setsid")
    assert started.endswith(f"{INSTALLED_LAUNCHER} watch"), started
    assert "WAYLAND_DISPLAY=wayland-9" in started.split(), started
    assert _wait_for(lambda: box.calls("sleep") == ["sleep 10"]), box.log.read_text()
    assert box.calls("launcher installed") == ["launcher installed watch"]


def test_install_leaves_the_open_kiosk_alone_and_says_when_changes_apply(deploy, box):
    result = deploy.result
    assert result.returncode == 0, result.stderr
    assert deploy.kiosk.poll() is None, "install.sh stopped the open kiosk"
    assert not deploy.signals.exists(), deploy.signals.read_text()
    assert box.launches(wait=0.3) == []
    [line] = [ln for ln in result.stdout.splitlines() if "next launch" in ln]
    # `open` does nothing while a kiosk is alive, so the learner must Exit first.
    assert "sigint101-course open" in line, line
    assert "Exit" in line and line.index("Exit") < line.index("sigint101-course open"), line


def test_install_with_no_watchdog_running_starts_none(box):
    box.start_session()
    result = box.run(INSTALL)
    assert result.returncode == 0, result.stderr
    assert box.calls("setsid") == []
    assert len([ln for ln in result.stdout.splitlines() if "next launch" in ln]) == 1


# The launcher the reference Recon runs today (main at 3004a03, byte for byte). Its watch loop's
# children inherit the lock, so its `sleep 10` holds it for a while after the watchdog is stopped.
DEPLOYED_LAUNCHER = REPO / "tests" / "fixtures" / "kiosk" / "start-kiosk-3004a03.sh"


def _sleep_stub(box, seconds):
    """While the hold file exists, each `sleep` is real and lasts `seconds`; its PID goes in
    sleep-pids. Without the hold file a sleep is logged and ends the session."""
    hold, pids = box.log.parent / "hold", box.log.parent / "sleep-pids"
    real = shutil.which("sleep")
    _stub(box.bin, "sleep",
          f'if [ -e {hold} ]; then echo $$ >> {pids}; exec {real} {seconds}; fi\n'
          f'echo "sleep $*" >> {box.log}; rm -f {box.runtime}/wayland-9')
    hold.touch()
    return hold, pids


def _pids(path):
    return [int(p) for p in path.read_text().split()] if path.exists() else []


def _kill_all(pids):
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def _watchdog(box, launcher):
    return subprocess.Popen(["bash", str(launcher), "watch"], env=box.env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _old_install(box, text):
    """An older launcher where install.sh looks for a running watchdog's: the checkout's."""
    path = _checkout_launcher(box)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_install_hands_over_from_the_deployed_launcher_whose_sleep_holds_the_lock(box):
    """The first deploy of this change finds a watchdog on the Recon's launcher: once it is
    stopped, its sleep still holds the lock for up to 10 s (4 s here). install.sh must wait for
    it, or the new watchdog stands down and the box is left with none."""
    box.alive.touch()
    _quiet_desktop(box)
    box.start_session()
    hold, pids = _sleep_stub(box, 4)
    _installed_launcher(box)
    old = _watchdog(box, _old_install(box, DEPLOYED_LAUNCHER.read_text(encoding="utf-8")))
    try:
        assert _wait_for(lambda: _pids(pids))
        assert not _lock_is_free(box)
        hold.unlink()
        result = box.run(_install(box))
        assert result.returncode == 0, result.stderr
        assert "WARN" not in result.stderr, result.stderr
        old.wait(timeout=10)
        assert len(box.calls("setsid")) == 1
        # Exactly one new watchdog, on the installed launcher, took the lock and ran its pass.
        assert _wait_for(lambda: box.calls("sleep") == ["sleep 10"]), box.log.read_text()
        assert box.calls("launcher installed") == ["launcher installed watch"]
    finally:
        if old.poll() is None:
            old.kill()
        old.wait(timeout=10)
        _kill_all(_pids(pids))


@pytest.mark.parametrize("home", ["plain", "with a space"])
@pytest.mark.parametrize("title", ["separate", "joined"])
def test_install_leaves_the_watchdog_running_when_the_kiosk_holds_its_lock(box, title, home):
    """A kiosk Chromium started by a launcher without `9>&-` holds the lock for as long as it
    runs. Stopping the watchdog then would leave none, since no new one could take the lock, so
    install.sh leaves the working one alone, says so, and does not touch the browser. Chromium
    may show its arguments one by one, or rewrite its title into one joined string; the profile
    path may contain a space, so the whole flag is matched, never a word of it."""
    if home != "plain":
        box.home = box.log.parent / "learner home"
        box.home.mkdir()
    _quiet_desktop(box)
    box.start_session()
    hold, pids = _sleep_stub(box, 30)
    text = DEPLOYED_LAUNCHER.read_text(encoding="utf-8")
    assert text.count("--password-store=basic 9>&- &") == 1
    legacy = _old_install(box, text.replace("--password-store=basic 9>&- &",
                                            "--password-store=basic &"))
    signals, kiosk_pid = box.log.parent / "kiosk-signals", box.log.parent / "kiosk-pid"
    real_sleep = shutil.which("sleep")
    browser_loop = box.log.parent / "browser-loop.sh"
    browser_loop.write_text('for s in TERM INT HUP QUIT USR1 USR2 ALRM PIPE CONT; do\n'
                            f'  trap "echo $s >> {signals}" "$s"\n'
                            'done\n'
                            f'while :; do {real_sleep} 0.1; done\n', encoding="utf-8")
    joined = (f"/usr/lib/chromium/chromium --kiosk --ozone-platform=wayland "
              f"--user-data-dir={box.home}/.config/chromium-kiosk --app=http://127.0.0.1:8008")
    run = (f'exec -a "{joined}" {shutil.which("bash")} {browser_loop}' if title == "joined"
           else f'. {browser_loop}')
    _stub(box.bin, "chromium", f'echo $$ > {kiosk_pid}; touch {box.alive}\n{run}')
    _installed_launcher(box)
    old = _watchdog(box, legacy)
    try:
        assert _wait_for(lambda: kiosk_pid.exists() and _pids(pids))
        browser = int(kiosk_pid.read_text())
        if title == "joined":
            assert _wait_for(lambda: Path(f"/proc/{browser}/cmdline").read_bytes()
                             .startswith(joined.encode() + b"\0"))
        result = box.run(_install(box))
        assert result.returncode == 0, result.stderr
        assert old.poll() is None, "install.sh stopped the only watchdog that can run"
        assert box.calls("setsid") == []
        assert _alive(browser) and not signals.exists()
        [warn] = [ln for ln in result.stderr.splitlines() if "WARN" in ln]
        assert f"{browser} " in warn and "left running" in warn, warn
    finally:
        if old.poll() is None:
            old.kill()
        old.wait(timeout=10)
        _kill_all(_pids(pids) + ([int(kiosk_pid.read_text())] if kiosk_pid.exists() else []))


def test_install_never_kills_a_pid_that_is_no_longer_the_watchdog(box):
    """The stand-in for a reused PID: on TERM the watchdog becomes another program under the
    same PID, while a child of it holds the lock past install.sh's wait. When install.sh falls
    back to SIGKILL it must kill only what is still the watchdog, so that PID lives."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    real_sleep = shutil.which("sleep")
    fake = _old_install(box,
                        "#!/bin/bash\n"
                        f'exec 9>"{_lock(box)}"; flock -n 9 || exit 1\n'
                        # Holds the lock until the test ends it, past every wait install.sh has.
                        f'{real_sleep} 600 &\n'
                        f'echo $! > {box.log.parent / "child-pid"}\n'
                        f"trap 'exec 9>&-; exec -a imposter {real_sleep} 60' TERM\n"
                        f'while :; do {real_sleep} 0.2; done\n')
    old = _watchdog(box, fake)
    child = box.log.parent / "child-pid"
    try:
        assert _wait_for(lambda: child.exists() and child.read_text().strip())
        result = box.run(_install(box), timeout=40)
        assert result.returncode == 0, result.stderr
        assert old.poll() is None, "install.sh killed a PID that was no longer the watchdog"
        cmdline = Path(f"/proc/{old.pid}/cmdline").read_bytes().split(b"\0")
        assert cmdline[0] == b"imposter", cmdline
        assert box.calls("setsid") == []
        assert any("still held" in ln for ln in result.stderr.splitlines()), result.stderr
    finally:
        if old.poll() is None:
            old.kill()
        old.wait(timeout=10)
        _kill_all(_pids(child))


def test_install_leaves_a_lock_holder_alone_unless_bash_runs_the_launcher(box):
    """A program that is not the shell running the launcher is never the watchdog, even with the
    launcher's path and `watch` as its arguments and the lock held."""
    box.start_session()
    _installed_launcher(box)
    signals = box.log.parent / "signals"
    script = box.log.parent / "py" / "sigint101-course"
    script.parent.mkdir()
    script.write_text(
        "import fcntl, signal, sys, time\n"
        f"lock = open({str(_lock(box))!r}, 'w')\n"
        "fcntl.flock(lock, fcntl.LOCK_EX)\n"
        f"signal.signal(signal.SIGTERM, lambda *a: open({str(signals)!r}, 'a').write('TERM'))\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n", encoding="utf-8")
    proc = subprocess.Popen([shutil.which("python3"), str(script), "watch"],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        result = box.run(_install(box))
        assert result.returncode == 0, result.stderr
        assert proc.poll() is None and not signals.exists()
        assert box.calls("setsid") == []
        # Held, yet no watchdog holds it: say so rather than restart nothing in silence (a kernel
        # without fdinfo lock lines would look the same).
        [warn] = [ln for ln in result.stderr.splitlines() if "WARN" in ln]
        assert "no watchdog" in warn and "not restarted" in warn, warn
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _stubborn_watchdog(box, extra="", stubborn=True, usr1=None):
    """A watchdog by its command line (bash, the checkout launcher, watch) that holds the lock.
    Stubborn, it ignores TERM, so install.sh falls back to SIGKILL after its 15 s wait. On USR1
    it becomes another program under the same PID ("imposter", which notes the signals it gets),
    as a test hook's stand-in for a process whose identity changes; `usr1` replaces that."""
    real_sleep = shutil.which("sleep")
    imposter = box.log.parent / "imposter.sh"
    imposter.write_text('for s in TERM INT HUP QUIT USR2; do\n'
                        f'  trap "echo $s >> {box.log.parent / "imposter-signals"}" "$s"\n'
                        'done\n'
                        f'while :; do {real_sleep} 0.1; done\n', encoding="utf-8")
    usr1 = usr1 or f"exec 9>&-; exec -a imposter {shutil.which('bash')} {imposter}"
    return _watchdog(box, _old_install(
        box,
        "#!/bin/bash\n"
        f'exec 9>"{_lock(box)}"; flock -n 9 || exit 1\n'
        + ("trap '' TERM\n" if stubborn else "")
        + f"trap '{usr1}' USR1\n"
        + extra
        + f'echo started > {box.log.parent / "stubborn"}\n'
        f'while :; do {real_sleep} 0.2; done\n'))


def _is_imposter(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().startswith(b"imposter\0")
    except OSError:
        return False


def _python_hook(box, prelude):
    """python3 for install.sh's helper (`python3 -I .../watchdog_helper.py MODE ...`): the real
    interpreter running the real helper, with `prelude` run first, standing in for the kernel.
    The prelude sees the helper's own sys.argv (argv[1] is the mode). It logs every pidfd signal
    sent as "pidfd-send N". Other python3 calls run the real interpreter untouched."""
    real = shutil.which("python3")
    hook = box.log.parent / "pidfd-hook.py"
    hook.write_text(
        "import sys\n"
        "_helper = sys.argv[1]\n"
        "sys.argv = [_helper] + sys.argv[2:]\n"
        "import signal as _signal\n"
        "_send = _signal.pidfd_send_signal\n"
        "def _logged(fd, sig, *rest):\n"
        f"    open({str(box.log)!r}, 'a').write(f'pidfd-send {{int(sig)}}\\n')\n"
        "    return _send(fd, sig, *rest)\n"
        "_signal.pidfd_send_signal = _logged\n"
        + prelude +
        "\nwith open(_helper, encoding='utf-8') as _f:\n"
        "    _code = compile(_f.read(), _helper, 'exec')\n"
        "exec(_code, {'__name__': '__main__', '__file__': _helper})\n",
        encoding="utf-8")
    _stub(box.bin, "python3",
          'if [ "$1" = -I ] && [ "${2##*/}" = watchdog_helper.py ]; then\n'
          f'  shift; exec {real} -I {hook} "$@"\n'
          'fi\n'
          f'exec {real} "$@"')


# Runs inside the hook: on the pin for SIGNAL, the pinned process changes (USR1) before the
# helper checks it, until its command line holds MARKER: the check sees another program, or the
# same program run from another file, under the pinned PID.
_CHANGE_AFTER_PIN = """
import os, time
_pidfd_open = os.pidfd_open
def _pin(pid, flags=0):
    fd = _pidfd_open(pid, flags)
    if sys.argv[1] == {signal!r}:
        os.kill(pid, _signal.SIGUSR1)
        end = time.time() + 5
        while time.time() < end:
            with open(f"/proc/{{pid}}/cmdline", "rb") as f:
                if {marker!r} in f.read():
                    break
            time.sleep(0.05)
    return fd
os.pidfd_open = _pin
"""


def _change_after_pin(signal_name, marker=b"imposter\0"):
    return _CHANGE_AFTER_PIN.format(signal=signal_name, marker=marker)


def _sent(box):
    return [int(ln.split()[1]) for ln in box.calls("pidfd-send")]


def _hands_over(box, result):
    assert result.returncode == 0, result.stderr
    assert len(box.calls("setsid")) == 1, box.log.read_text()
    assert _wait_for(lambda: box.calls("sleep") == ["sleep 10"]), box.log.read_text()
    assert len([ln for ln in result.stdout.splitlines() if "next launch" in ln]) == 1


def _end(old):
    if old.poll() is None:
        old.kill()
    old.wait(timeout=10)


def test_install_signals_nothing_when_the_identity_changes_after_the_pin_before_term(box):
    box.alive.touch()
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, _change_after_pin("TERM"))
    old = _stubborn_watchdog(box, stubborn=False)
    try:
        assert _wait_for(lambda: (box.log.parent / "stubborn").exists())
        result = box.run(_install(box), timeout=40)
        assert _sent(box) == [], "a signal went to a process that was no longer the watchdog"
        assert _is_imposter(old.pid) and old.poll() is None
        assert not (box.log.parent / "imposter-signals").exists()
        # The imposter let the lock go, so the new watchdog took over.
        _hands_over(box, result)
    finally:
        _end(old)


def test_install_sends_no_sigkill_when_the_identity_changes_at_the_rescan(box):
    box.alive.touch()
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, _change_after_pin("KILL"))
    old = _stubborn_watchdog(box)
    try:
        assert _wait_for(lambda: (box.log.parent / "stubborn").exists())
        result = box.run(_install(box), timeout=40)
        assert _sent(box) == [signal.SIGTERM], box.log.read_text()  # TERM, ignored; no KILL
        assert _is_imposter(old.pid) and old.poll() is None
        _hands_over(box, result)
    finally:
        _end(old)


@pytest.mark.parametrize("missing", ["python", "kernel"])
def test_install_without_a_pidfd_signals_nothing_and_keeps_the_watchdog(box, missing):
    """Python without os.pidfd_open (before 3.9), or a kernel without pidfd_open (before 5.3):
    nothing is signalled, never a PID by number, and the working watchdog stays."""
    box.alive.touch()
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, "import os\ndel os.pidfd_open\n" if missing == "python" else
                 "import errno, os\n"
                 "def _enosys(*a, **k):\n"
                 "    raise OSError(errno.ENOSYS, 'Function not implemented')\n"
                 "os.pidfd_open = _enosys\n")
    old = _stubborn_watchdog(box, stubborn=False)
    try:
        assert _wait_for(lambda: (box.log.parent / "stubborn").exists())
        result = box.run(_install(box))
        assert result.returncode == 0, result.stderr
        assert old.poll() is None and not _is_imposter(old.pid)
        assert _sent(box) == [] and box.calls("setsid") == []
        [warn] = [ln for ln in result.stderr.splitlines() if "WARN" in ln]
        assert "pidfd" in warn and "left running" in warn, warn
    finally:
        _end(old)


# Runs inside the hook: the launcher file the watchdog's fd 255 names reads as root's and
# unwritable, as on a box where install.sh's `sudo install` put it (a test cannot make one).
_LAUNCHER_ROOT_OWNED = """
import os
_fstat = os.fstat
def _as_installed(fd):
    st = _fstat(fd)
    try:
        target = os.readlink(f"/proc/self/fd/{fd}").removesuffix(" (deleted)")
    except OSError:
        return st
    if target != os.path.realpath(sys.argv[4]):
        return st
    return os.stat_result((st.st_mode & ~0o222, st.st_ino, st.st_dev, st.st_nlink, 0, 0,
                           st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
os.fstat = _as_installed
"""


def _reviewed_watchdog_ignoring_term(box):
    """The checkout's own launcher (byte for byte the shipped one, a reviewed launcher), run as a
    watchdog that ignores TERM (bash keeps a signal ignored at start ignored), so install.sh comes
    to its SIGKILL step. Its sleeps last 30 s while the hold file exists."""
    box.alive.touch()
    hold, pids = _sleep_stub(box, 30)
    launcher = _checkout_launcher(box)
    assert launcher.read_bytes() == LAUNCHER.read_bytes()
    old = subprocess.Popen(["bash", str(launcher), "watch"], env=box.env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN))
    assert _wait_for(lambda: _pids(pids)), "the watchdog never reached its loop"
    return old, hold, pids


def test_install_kills_a_reviewed_launcher_from_a_file_its_user_cannot_rewrite(box):
    """The one case for SIGKILL: the bytes behind fd 255 are a reviewed launcher's, and the file
    is not the kiosk user's to rewrite. Then a watchdog that ignored TERM is killed and replaced."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, _LAUNCHER_ROOT_OWNED)
    old, hold, pids = _reviewed_watchdog_ignoring_term(box)
    try:
        hold.unlink()  # sleeps from here on end the session, the new watchdog's included
        result = box.run(_install(box), timeout=40)
        assert _sent(box) == [signal.SIGTERM, signal.SIGKILL], result.stderr
        old.wait(timeout=10)
        _hands_over(box, result)
    finally:
        _end(old)
        _kill_all(_pids(pids))


def test_install_hands_over_when_the_watchdog_exits_just_before_its_sigkill(box):
    """The watchdog passes the re-check, then exits before SIGKILL lands, so the send fails.
    Under `set -e` that must not end install.sh: it still hands over and prints its message."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, _LAUNCHER_ROOT_OWNED +
                 "_inner = _signal.pidfd_send_signal\n"
                 "def _late(fd, sig, *rest):\n"
                 "    _inner(fd, sig, *rest)\n"
                 "    if sig == _signal.SIGKILL:\n"
                 "        raise ProcessLookupError(3, 'No such process')\n"
                 "_signal.pidfd_send_signal = _late\n")
    old, hold, pids = _reviewed_watchdog_ignoring_term(box)
    try:
        hold.unlink()
        result = box.run(_install(box), timeout=40)
        assert _sent(box) == [signal.SIGTERM, signal.SIGKILL]
        old.wait(timeout=10)
        _hands_over(box, result)
    finally:
        _end(old)
        _kill_all(_pids(pids))


def _term_only(box, result, old, reason):
    assert result.returncode == 0, result.stderr
    assert _sent(box) == [signal.SIGTERM], box.log.read_text()
    assert old.poll() is None, "install.sh killed it"
    assert box.calls("setsid") == []
    [warn] = [ln for ln in result.stderr.splitlines() if "WARN" in ln]
    assert "not killed" in warn and reason in warn, warn
    return warn


def test_install_never_kills_a_watchdog_on_an_unreviewed_launcher(box):
    """Its launcher's digest is no reviewed launcher's, so TERM only, even from a file its user
    cannot rewrite. The exec scan only adds a hint (this one's USR1 trap can exec)."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, _LAUNCHER_ROOT_OWNED)
    old = _stubborn_watchdog(box)
    try:
        assert _wait_for(lambda: (box.log.parent / "stubborn").exists())
        result = box.run(_install(box), timeout=40)
        warn = _term_only(box, result, old, "unreviewed launcher")
        assert "can exec another program" in warn, warn
        assert not _is_imposter(old.pid)
    finally:
        _end(old)


def test_install_never_kills_a_reviewed_launcher_from_a_file_its_user_can_rewrite(box):
    """The digest matches, but the kiosk user owns the file and could have rewritten it in place
    after bash loaded it: the bytes on disk would then not be the ones running. TERM only."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, "")
    old, hold, pids = _reviewed_watchdog_ignoring_term(box)
    try:
        result = box.run(_install(box), timeout=40)
        _term_only(box, result, old, "can be rewritten by the kiosk user")
    finally:
        _end(old)
        _kill_all(_pids(pids))


def test_install_never_kills_a_watchdog_whose_launcher_was_rewritten_in_place(box):
    """Bash loaded one launcher, then the same inode was rewritten to a reviewed launcher's exact
    bytes. The digest now matches what bash is not running; the file being the user's to rewrite
    is what keeps it TERM only."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, "")
    launcher = _old_install(box, LAUNCHER.read_text(encoding="utf-8") + "# the bytes bash ran\n")
    inode = launcher.stat().st_ino
    box.alive.touch()
    hold, pids = _sleep_stub(box, 30)
    old = subprocess.Popen(["bash", str(launcher), "watch"], env=box.env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN))
    try:
        assert _wait_for(lambda: _pids(pids))
        with open(launcher, "r+b") as f:  # the same inode, rewritten
            f.write(LAUNCHER.read_bytes())
            f.truncate()
        assert launcher.stat().st_ino == inode
        assert launcher.read_bytes() == LAUNCHER.read_bytes()
        result = box.run(_install(box), timeout=40)
        _term_only(box, result, old, "can be rewritten by the kiosk user")
    finally:
        _end(old)
        _kill_all(_pids(pids))


def test_install_never_signals_a_non_shell_with_the_watchdogs_exact_command_line(box):
    """argv[0] proves nothing: this is python3 with the command line "bash <checkout launcher>
    watch", this user's, holding the lock. Its executable is not a shell, so no signal at all."""
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, "")
    signals = box.log.parent / "python-signals"
    # Read by python3 as its script; to bash the first lines are a no-op and an exit.
    launcher = _old_install(box,
                            "#!/bin/bash\n"
                            "''':'\n"
                            "exit 0\n"
                            "'''\n"
                            "import fcntl, signal, sys, time\n"
                            f"lock = open({str(_lock(box))!r}, 'w')\n"
                            "fcntl.flock(lock, fcntl.LOCK_EX)\n"
                            "for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):\n"
                            f"    signal.signal(s, lambda n, f: open({str(signals)!r}, 'a')"
                            ".write(f'{n}\\n'))\n"
                            "print('locked', flush=True)\n"
                            "time.sleep(60)\n")
    proc = subprocess.Popen(["bash", str(launcher), "watch"], executable=shutil.which("python3"),
                            env=box.env(), stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        assert Path(f"/proc/{proc.pid}/cmdline").read_bytes() == \
            b"\0".join([b"bash", str(launcher).encode(), b"watch"]) + b"\0"
        result = box.run(_install(box))
        assert result.returncode == 0, result.stderr
        assert proc.poll() is None and not signals.exists() and _sent(box) == []
        assert box.calls("setsid") == []
        [warn] = [ln for ln in result.stderr.splitlines() if "WARN" in ln]
        assert "no watchdog" in warn, warn
    finally:
        _end(proc)


def _helper_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("watchdog_helper",
                                                  REPO / "deploy" / "watchdog_helper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MANAGED_LAUNCHER = REPO / "deploy" / "managed" / "sigint101-course-managed.sh"


@pytest.mark.parametrize("launcher", [LAUNCHER, DEPLOYED_LAUNCHER, MANAGED_LAUNCHER],
                         ids=["shipped", "3004a03", "managed"])
def test_the_launchers_watch_path_never_execs_another_program(launcher):
    """What lets install.sh escalate to SIGKILL: once `watch` runs, the launcher stays in bash
    until it exits. Its `exec 9>...` only moves file descriptors. The managed wrapper, which
    enrollment installs at /usr/local/bin/sigint101-course, holds the same lock and must pass
    too: its `exec 8<"$MUTATION_LOCK"` and `exec 8>&-` only move file descriptors as well."""
    helper = _helper_module()
    text = launcher.read_text(encoding="utf-8")
    path = helper.watch_path(text)
    assert any("exec 9>" in line for line in path), "the scan missed the watch path"
    assert helper.program_execs(path) == []
    assert not helper.can_exec(text)


@pytest.mark.parametrize("body, execs", [
    ('  exec 9>"$LOCK"', False),
    ("  exec 9>&-", False),
    ('  exec 9> "$LOCK"', False),
    ("  exec {fd}>/dev/null 2>&1", False),
    ('  exec "$@"', True),
    ("  exec -a imposter /bin/bash /x", True),
    ("  exec 9>&- sleep 1", True),
    ("  trap 'exec 9>&-; exec sleep 1' USR1", True),
    ('  eval "$cmd"', True),
    ('  . "$HOME/extra.sh"', True),
    ("  [ -e /x ] && exec chromium", True),
    ("  # close-on-exec, and an exec in a comment: exec chromium", False),
    ('  echo "the close-on-exec copy"', False),
], ids=lambda v: str(v)[:24])
def test_the_exec_scan_tells_a_program_exec_from_a_redirection(body, execs):
    helper = _helper_module()
    text = f"#!/bin/bash\nwatch() {{\n{body}\n  helper_fn\n}}\nhelper_fn() {{\n  :\n}}\n"
    assert helper.can_exec(text) is execs


def test_the_exec_scan_follows_the_watch_path_and_skips_here_documents():
    helper = _helper_module()
    reached = ("#!/bin/bash\nwatch() {\n  step\n}\nstep() {\n  exec chromium\n}\n")
    elsewhere = ("#!/bin/bash\nwatch() {\n  :\n}\nopen_course() {\n  exec chromium\n}\n")
    heredoc = ("#!/bin/bash\nwatch() {\n  python3 - <<'PY'\nexec chromium\nPY\n}\n")
    assert helper.can_exec(reached)
    assert not helper.can_exec(elsewhere)
    assert not helper.can_exec(heredoc)


def _install_function(name):
    """One function of install.sh, as text, to run on its own."""
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index(f"\n{name}() {{\n") + 1
    return text[start:text.index("\n}\n", start) + 3]


@pytest.mark.parametrize("home", ["plain", "with a space"])
@pytest.mark.parametrize("form", ["separate", "joined"])
@pytest.mark.parametrize("profile, kiosk", [
    (".config/chromium-kiosk", True),
    (".config/chromium-kiosk-old", False),
    (".config/chromium-kiosk2", False),
    (".config/chromium-kiosk/../other", False),
])
def test_only_the_whole_kiosk_profile_flag_marks_the_kiosk_browser(tmp_path, home, form, profile,
                                                                   kiosk):
    """The kiosk is the browser on exactly ~/.config/chromium-kiosk, never one whose profile path
    merely starts with it, whether Chromium shows its arguments one by one or as one title."""
    home_dir = tmp_path / ("learner home" if home != "plain" else "home")
    home_dir.mkdir()
    flag = f"--user-data-dir={home_dir}/{profile}"
    loop = f"while :; do {shutil.which('sleep')} 1; done"
    bash = shutil.which("bash")
    if form == "separate":
        argv = [bash, "-c", loop, "chromium", "--kiosk", flag, "--app=http://127.0.0.1:8008"]
    else:
        argv = [f"/usr/lib/chromium/chromium --kiosk {flag} --app=http://127.0.0.1:8008",
                "-c", loop]
    proc = subprocess.Popen(argv, executable=bash)
    try:
        assert _wait_for(lambda: flag.encode() in Path(f"/proc/{proc.pid}/cmdline").read_bytes())
        check = subprocess.run([bash, "-c", _install_function("is_kiosk") + 'is_kiosk "$1"',
                                "is_kiosk", str(proc.pid)],
                               env={"HOME": str(home_dir), "PATH": "/usr/bin:/bin"},
                               capture_output=True, text=True, timeout=10)
        assert (check.returncode == 0) is kiosk, (argv, check.stderr)
    finally:
        _end(proc)


def test_a_second_install_that_finds_the_new_watchdog_already_started_says_so_quietly(box):
    """Two installs at once: both stop the old watchdog, one starts the new one, and the other
    then finds the lock held by it. That is the hand-over done, not a stuck lock: no WARN, no
    second watchdog, and no 20 s wait. Staged here by an old watchdog that, on TERM, starts its
    successor (a watchdog by every check) before it exits."""
    box.alive.touch()
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    pids = box.log.parent / "watchdog-pids"
    real_sleep = shutil.which("sleep")
    # The successor inherits the locked fd 9, so the lock is never free in between, as when the
    # other install's new watchdog took it first.
    _old_install(box,
                 "#!/bin/bash\n"
                 'if [ -z "${SUCCESSOR:-}" ]; then\n'
                 f'  exec 9>"{_lock(box)}"; flock -n 9 || exit 1\n'
                 "fi\n"
                 f"echo $$ >> {pids}\n"
                 f"trap 'SUCCESSOR=1 {shutil.which('bash')} \"$0\" watch & exit 0' TERM\n"
                 f"while :; do {real_sleep} 0.2; done\n")
    old = _watchdog(box, _checkout_launcher(box))
    try:
        assert _wait_for(lambda: len(_pids(pids)) == 1)
        start = time.time()
        result = box.run(_install(box), timeout=40)
        took = time.time() - start
        assert result.returncode == 0, result.stderr
        assert "WARN" not in result.stderr, result.stderr
        assert any("already started the new course watchdog" in ln
                   for ln in result.stdout.splitlines()), result.stdout
        assert box.calls("setsid") == []
        old.wait(timeout=10)
        [_, successor] = _pids(pids)
        assert _alive(successor) and not _lock_is_free(box)
        assert took < 10, took
    finally:
        _end(old)
        _kill_all(_pids(pids))


def test_a_replacement_that_appears_right_after_discovery_is_no_reason_to_warn(box):
    """The other install can start the new watchdog between this one finding the old watchdog
    and looking at the lock's other holders. That holder is then a watchdog by every check, not
    under the old one: the hand-over is done, so no WARN and no signal. Staged by having the
    discovery scan itself make the old watchdog start a detached successor that keeps the lock."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    pids, once = box.log.parent / "watchdog-pids", box.log.parent / "successor-asked"
    _python_hook(box,
                 "import atexit, os, time\n"
                 f"def _start_successor(pids={str(pids)!r}, once={str(once)!r}):\n"
                 "    if os.path.exists(once):\n"
                 "        return\n"
                 "    open(once, 'w').close()\n"
                 "    first = int(open(pids).read().split()[0])\n"
                 "    os.kill(first, _signal.SIGUSR1)\n"
                 "    end = time.time() + 5\n"
                 "    while len(open(pids).read().split()) < 2 and time.time() < end:\n"
                 "        time.sleep(0.05)\n"
                 "if sys.argv[1] == 'FIND':\n"
                 "    atexit.register(_start_successor)\n")
    real_sleep = shutil.which("sleep")
    _old_install(box,
                 "#!/bin/bash\n"
                 'if [ -z "${SUCCESSOR:-}" ]; then\n'
                 f'  exec 9>"{_lock(box)}"; flock -n 9 || exit 1\n'
                 "fi\n"
                 f"echo $$ >> {pids}\n"
                 # Detached (its parent subshell exits at once), with the locked fd 9.
                 f"trap '( SUCCESSOR=1 {shutil.which('bash')} \"$0\" watch & )' USR1\n"
                 f"while :; do {real_sleep} 0.2; done\n")
    old = _watchdog(box, _checkout_launcher(box))
    try:
        assert _wait_for(lambda: len(_pids(pids)) == 1)
        result = box.run(_install(box), timeout=40)
        assert result.returncode == 0, result.stderr
        assert len(_pids(pids)) == 2, "the successor never started"
        assert "WARN" not in result.stderr, result.stderr
        assert any("already started the new course watchdog" in ln
                   for ln in result.stdout.splitlines()), result.stdout
        assert _sent(box) == [] and box.calls("setsid") == []
        assert old.poll() is None
    finally:
        _end(old)
        _kill_all(_pids(pids))


def test_the_lock_wait_keeps_to_its_deadline_however_slow_the_scans(box):
    """Each one-second lock attempt is followed by a scan for another install's watchdog; with
    scans that take a second each, the 15 s wait used to take 30. One monotonic deadline covers
    both, and each scan gets only the time left."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, "import time\nif sys.argv[1] == 'FIND':\n    time.sleep(1)\n")
    old = _stubborn_watchdog(box)  # ignores TERM; unreviewed, so TERM only after the wait
    try:
        assert _wait_for(lambda: (box.log.parent / "stubborn").exists())
        start = time.time()
        result = box.run(_install(box), timeout=60)
        took = time.time() - start
        _term_only(box, result, old, "unreviewed launcher")
        # The first discovery and the holder check take their second or so each, plus 15 s.
        assert took < 21, took
    finally:
        _end(old)


def test_the_reviewed_launcher_list_names_exactly_the_reviewed_launchers():
    """One frozen list, read by install.sh's helper at run time: the shipped launcher, the one
    the reference Recon runs (3004a03), and the managed wrapper enrollment installs."""
    import hashlib
    helper = _helper_module()
    listed = helper.reviewed_digests()
    files = [LAUNCHER, DEPLOYED_LAUNCHER, MANAGED_LAUNCHER]
    assert listed == {hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


# Runs inside the hook: only the launcher file's owner reads as root; its real mode stays, so a
# group- or world-writable mode is still seen.
_LAUNCHER_ROOT_OWNER_ONLY = _LAUNCHER_ROOT_OWNED.replace("st.st_mode & ~0o222", "st.st_mode")


@pytest.mark.parametrize("mode", [0o775, 0o757], ids=["group-write", "world-write"])
def test_install_never_kills_a_reviewed_launcher_that_others_can_rewrite(box, mode):
    """A reviewed launcher owned by someone else is safe only if no group or other user can
    rewrite it either: with a real group- or world-writable mode it is TERM only, with a warning,
    and the watchdog survives."""
    assert "st.st_mode, st.st_ino" in _LAUNCHER_ROOT_OWNER_ONLY
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, _LAUNCHER_ROOT_OWNER_ONLY)
    _checkout_launcher(box).chmod(mode)
    old, hold, pids = _reviewed_watchdog_ignoring_term(box)
    try:
        result = box.run(_install(box), timeout=40)
        _term_only(box, result, old, "can be rewritten by the kiosk user")
    finally:
        _end(old)
        _kill_all(_pids(pids))


def _wait_for_lock_script(box, seconds, found="1:1"):
    """install.sh's own wait_for_lock and what it calls, run on their own against the box."""
    names = ("within_scan_limit", "watchdog_helper", "lock_holders", "watchdogs", "uptime_cs",
             "cs_to_s", "wait_for_lock")
    return (f'WATCH_LOCK="{_lock(box)}"\nHERE="{REPO / "deploy"}"\n'
            'COURSE_BIN=/usr/local/bin/sigint101-course\n'
            f'CHECKOUT_LAUNCHER="{LAUNCHER}"\nSCAN_LIMIT=""\n'
            + "".join(_install_function(n) for n in names)
            + f'found=("{found}")\nwait_for_lock {seconds}; echo "rc=$?"\n')


def _slow(box, name, seconds):
    """`name` on the box's PATH, the real one after a pause."""
    _stub(box.bin, name, f"{shutil.which('sleep')} {seconds}; exec {shutil.which(name)} \"$@\"")


def _held_lock(box):
    import fcntl
    held = open(_lock(box), "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return held


def test_the_lock_wait_keeps_its_deadline_when_every_step_of_a_scan_is_slow(box):
    """Each step of a lock-holder scan held up for 0.78 s (finding the holders, reading their
    fdinfo, the identity check): a two-second wait must still end at two seconds, not 2.6."""
    _python_hook(box, "import time\nif sys.argv[1] in ('FIND', 'HOLDERS'):\n"
                      "    time.sleep(0.78)\n")
    _slow(box, "find", 0.78)
    _slow(box, "grep", 0.78)
    held = _held_lock(box)
    try:
        start = time.monotonic()
        run = subprocess.run(["bash", "-c", _wait_for_lock_script(box, 2)], env=box.env(),
                             capture_output=True, text=True, timeout=30)
        took = time.monotonic() - start
    finally:
        held.close()
    assert run.stdout.strip() == "rc=1", (run.stdout, run.stderr)
    assert took < 2.3, took


def test_the_lock_wait_cuts_off_a_scan_that_hangs_past_its_deadline(box):
    """The scan's one time limit is what keeps the deadline: a holder scan that hangs for 20 s
    must not hold up a two-second wait."""
    _python_hook(box, "import time\nif sys.argv[1] in ('FIND', 'HOLDERS'):\n"
                      "    time.sleep(20)\n")
    held = _held_lock(box)
    try:
        start = time.monotonic()
        run = subprocess.run(["bash", "-c", _wait_for_lock_script(box, 2)], env=box.env(),
                             capture_output=True, text=True, timeout=60)
        took = time.monotonic() - start
    finally:
        held.close()
    assert run.stdout.strip() == "rc=1", (run.stdout, run.stderr)
    assert took < 2.3, took


def test_the_lock_wait_takes_no_scan_result_that_came_after_its_deadline(box):
    """A scan that answers "a new watchdog" only as the deadline passes is not a hand-over: the
    time ran out first, so the wait reports that (1), not the replacement (2)."""
    _python_hook(box, "import time\nif sys.argv[1] == 'FIND':\n"
                      "    print('999:1', flush=True)\n"
                      "    time.sleep(5)\n"
                      "    sys.exit(0)\n")
    held = _held_lock(box)
    try:
        run = subprocess.run(["bash", "-c", _wait_for_lock_script(box, 2)], env=box.env(),
                             capture_output=True, text=True, timeout=30)
    finally:
        held.close()
    assert run.stdout.strip() == "rc=1", (run.stdout, run.stderr)


def test_an_empty_first_scan_with_the_lock_held_looks_again_before_it_warns(box):
    """The old watchdog can exit during the first scan while a new one (another install's, or
    autostart's) takes the lock. That first scan finds nothing; a second look finds the new
    watchdog, which is the hand-over done: no "no watchdog" WARN and no signal."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    pids, once = box.log.parent / "watchdog-pids", box.log.parent / "first-find"
    # The first scan: the old watchdog hands its locked fd to a detached successor and exits,
    # and the scan, having seen it go, finds nothing.
    _python_hook(box,
                 "import os, time\n"
                 "def _running(pid):\n"
                 "    try:\n"
                 "        return open(f'/proc/{pid}/stat').read().rpartition(') ')[2][0] != 'Z'\n"
                 "    except OSError:\n"
                 "        return False\n"
                 f"if sys.argv[1] == 'FIND' and not os.path.exists({str(once)!r}):\n"
                 f"    open({str(once)!r}, 'w').close()\n"
                 f"    first = int(open({str(pids)!r}).read().split()[0])\n"
                 "    os.kill(first, _signal.SIGUSR1)\n"
                 "    end = time.time() + 5\n"
                 f"    while time.time() < end and (len(open({str(pids)!r}).read().split()) < 2\n"
                 "                                 or _running(first)):\n"
                 "        time.sleep(0.05)\n"
                 "    sys.exit(0)\n")
    real_sleep = shutil.which("sleep")
    _old_install(box,
                 "#!/bin/bash\n"
                 'if [ -z "${SUCCESSOR:-}" ]; then\n'
                 f'  exec 9>"{_lock(box)}"; flock -n 9 || exit 1\n'
                 "fi\n"
                 f"echo $$ >> {pids}\n"
                 f"trap '( SUCCESSOR=1 {shutil.which('bash')} \"$0\" watch & ); exit 0' USR1\n"
                 f"while :; do {real_sleep} 0.2; done\n")
    old = _watchdog(box, _checkout_launcher(box))
    try:
        assert _wait_for(lambda: len(_pids(pids)) == 1)
        result = box.run(_install(box))
        assert result.returncode == 0, result.stderr
        assert once.exists(), "the first scan was not the staged one"
        old.wait(timeout=10)  # the old watchdog is gone
        [_, successor] = _pids(pids)
        assert "WARN" not in result.stderr, result.stderr
        assert any("already started the new course watchdog" in ln
                   for ln in result.stdout.splitlines()), result.stdout
        assert _sent(box) == [] and box.calls("setsid") == []
        assert _alive(successor) and not _lock_is_free(box)
    finally:
        _end(old)
        _kill_all(_pids(pids))


def test_a_first_scan_that_missed_the_old_watchdog_restarts_it_without_claiming_a_handover(box):
    """When the second look finds a watchdog that is older than the first scan, it is the old
    watchdog that scan missed, not a replacement: install.sh restarts it as usual and does not
    say another install already did."""
    box.alive.touch()
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    once = box.log.parent / "first-find"
    _python_hook(box, "import os\n"
                      f"if sys.argv[1] == 'FIND' and not os.path.exists({str(once)!r}):\n"
                      f"    open({str(once)!r}, 'w').close()\n"
                      "    sys.exit(0)\n")
    old = _stubborn_watchdog(box, stubborn=False)
    try:
        assert _wait_for(lambda: (box.log.parent / "stubborn").exists())
        result = box.run(_install(box), timeout=40)
        assert once.exists(), "the first scan was not the staged empty one"
        assert "already started" not in result.stdout, result.stdout
        assert "WARN" not in result.stderr, result.stderr
        assert _sent(box) == [signal.SIGTERM]
        old.wait(timeout=10)
        _hands_over(box, result)
    finally:
        _end(old)


def test_a_second_install_stops_and_says_why_when_the_wait_runs_out(box):
    """The bounded wait's other end: the lock still held when the wait runs out (cut to 1 s, as
    only a test may), so the second install stops before changing anything and says why."""
    import fcntl
    lock = box.runtime / "sigint101-install.lock"
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        start = time.monotonic()
        result = box.run(INSTALL, SIGINT101_TESTING="1", SIGINT101_TEST_INSTALL_WAIT_S="1")
        took = time.monotonic() - start
    assert result.returncode == 1
    assert box.calls("systemctl") == [] and box.calls("sudo") == []
    assert "waiting for it (up to 1 s)" in result.stderr, result.stderr
    assert (f"stopped: another install.sh still holds {lock} after 1 s; nothing was changed"
            in result.stderr), result.stderr
    assert took < 5, took


def test_the_install_wait_is_cut_short_only_when_a_test_says_so(box):
    """Without SIGINT101_TESTING=1 the shorter wait is ignored: the second install keeps
    waiting for the first."""
    import fcntl
    lock = box.runtime / "sigint101-install.lock"
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = subprocess.Popen(["bash", str(INSTALL)],
                                env=box.env(SIGINT101_TEST_INSTALL_WAIT_S="1"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(3)
        assert proc.poll() is None, "it stopped instead of waiting"
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert "up to 300 s" in err, err


def test_a_second_install_waits_for_the_first_and_says_so(box):
    """Two installs at once take turns: the second waits for the first's lock and says it is
    waiting, then runs to the end."""
    import fcntl
    lock = box.runtime / "sigint101-install.lock"
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = subprocess.Popen(["bash", str(INSTALL)], env=box.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        time.sleep(1.5)
        assert proc.poll() is None, "it did not wait for the lock"
        assert box.calls("systemctl") == [], "it changed things before it had the lock"
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert "another install.sh is running; waiting for it" in err, err


def _tree(home):
    """Every file under HOME, by path, with its digest (the home path itself made neutral)."""
    import hashlib
    files = {}
    for path in sorted(home.rglob("*")):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes().replace(str(home).encode(), b"~")
            files[str(path.relative_to(home))] = hashlib.sha256(data).hexdigest()
    return files


def test_thirty_installs_at_once_all_finish_complete_with_one_watchdog(tmp_path):
    """C83: overlapping installs raced in their `install -D` steps, and the loser stopped half
    done. Thirty at once, over a running watchdog: every one exits 0, every installed file is
    whole (the same as after one install alone), exactly one watchdog is left, and no lasting
    process keeps the install lock."""
    (tmp_path / "one").mkdir()
    reference = Box(tmp_path / "one")
    try:
        assert reference.run(INSTALL).returncode == 0
        expected = _tree(reference.home)
    finally:
        reference.close()
    assert expected, "the single install installed nothing"

    (tmp_path / "race").mkdir()
    box = Box(tmp_path / "race")
    box.alive.touch()
    _quiet_desktop(box)
    box.start_session()
    sleeper = _LongSleep(box)
    # The new watchdogs run this checkout's launcher, a path each later install accepts as the
    # watchdog's (on a box it is /usr/local/bin/sigint101-course).
    _stub(box.bin, "setsid",
          f'echo "setsid $*" >> {box.log}\n[ "$1" = -f ] && shift\nargs=()\n'
          f'for a in "$@"; do [ "$a" = {INSTALLED_LAUNCHER} ] && a={LAUNCHER}; '
          'args+=("$a"); done\n"${args[@]}" </dev/null >/dev/null 2>&1 &')
    old = _watchdog(box, LAUNCHER)
    try:
        sleeper.first_pid()
        procs = [subprocess.Popen(["bash", str(INSTALL)], env=box.env(), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(30)]
        results = [(p.wait(timeout=600), p.stderr.read()) for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        failed = [err for rc, err in results if rc != 0]
        assert failed == [], failed[0]
        assert _tree(box.home) == expected
        watchdogs = set()
        for fd in Path("/proc").glob("[0-9]*/fd/*"):
            try:
                if os.readlink(fd) == str(_lock(box)):
                    argv = Path(f"/proc/{fd.parts[2]}/cmdline").read_bytes().split(b"\0")
                    if argv[1:3] == [str(LAUNCHER).encode(), b"watch"]:
                        watchdogs.add(fd.parts[2])
            except OSError:
                pass
        assert len(watchdogs) == 1, watchdogs
        import fcntl
        with open(box.runtime / "sigint101-install.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free: no watchdog kept it
    finally:
        _end(old)
        sleeper.stop_all()
        box.close()


def test_install_never_kills_a_reused_pid_whose_start_time_differs(box):
    """A reused PID cannot be staged without privileges, so the start time stands in: at the
    SIGKILL re-check, /proc/PID/stat reports another start time for the pinned PID, as it would
    for a new process there. Command line and lock still match; it must not be killed."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, "import builtins, io\n"
                      "_open = builtins.open\n"
                      # FIND may come with no PID now (it scans the holders itself).
                      "_pid = sys.argv[5].partition(':')[0] if len(sys.argv) > 5 else ''\n"
                      "def _reopened(path, *a, **k):\n"
                      "    f = _open(path, *a, **k)\n"
                      "    if sys.argv[1] != 'KILL' or str(path) != f'/proc/{_pid}/stat':\n"
                      "        return f\n"
                      "    head, sep, rest = f.read().rpartition(') ')\n"
                      "    fields = rest.split()\n"
                      "    fields[19] = str(int(fields[19]) + 1)\n"
                      "    return io.StringIO(head + sep + ' '.join(fields))\n"
                      "builtins.open = _reopened\n")
    old = _stubborn_watchdog(box)
    try:
        assert _wait_for(lambda: (box.log.parent / "stubborn").exists())
        result = box.run(_install(box), timeout=40)
        assert result.returncode == 0, result.stderr
        assert _sent(box) == [signal.SIGTERM], box.log.read_text()
        assert old.poll() is None, "install.sh killed a PID whose start time had changed"
        assert box.calls("setsid") == []
        assert any("still held" in ln for ln in result.stderr.splitlines()), result.stderr
    finally:
        _end(old)


@pytest.mark.parametrize("name", ["start-kiosk.sh", "sigint101-course"])
def test_install_never_signals_a_launcher_of_the_same_name_elsewhere(box, name):
    """Only the installed launcher, or this checkout's, is the watchdog's. A bash running some
    other file of the same name with `watch`, and holding the lock, gets no signal at all."""
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, "")
    signals = box.log.parent / "elsewhere-signals"
    other = box.log.parent / "elsewhere" / name
    other.parent.mkdir()
    other.write_text("#!/bin/bash\n"
                     f'exec 9>"{_lock(box)}"; flock -n 9 || exit 1\n'
                     'for s in TERM INT HUP QUIT USR1 USR2; do\n'
                     f'  trap "echo $s >> {signals}" "$s"\n'
                     'done\n'
                     f'echo started > {box.log.parent / "stubborn"}\n'
                     f'while :; do {shutil.which("sleep")} 0.1; done\n', encoding="utf-8")
    old = _watchdog(box, other)
    try:
        assert _wait_for(lambda: (box.log.parent / "stubborn").exists())
        result = box.run(_install(box))
        assert result.returncode == 0, result.stderr
        assert old.poll() is None and not signals.exists() and _sent(box) == []
        assert box.calls("setsid") == []
        [warn] = [ln for ln in result.stderr.splitlines() if "WARN" in ln]
        assert "no watchdog" in warn, warn
    finally:
        _end(old)


def test_install_sends_nothing_once_the_pinned_watchdog_runs_a_same_name_launcher(box):
    """The check at signal time looks at the launcher's path too, not only the scan: found as the
    watchdog, the process then execs a start-kiosk.sh from another directory (same shell, same
    `watch`, same PID and start time, still holding the lock). Neither TERM nor KILL reaches it."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    _python_hook(box, _change_after_pin("TERM", marker=b"/elsewhere/"))
    signals = box.log.parent / "elsewhere-signals"
    other = box.log.parent / "elsewhere" / "start-kiosk.sh"
    other.parent.mkdir()
    other.write_text('for s in TERM INT HUP QUIT USR1 USR2; do\n'
                     f'  trap "echo $s >> {signals}" "$s"\n'
                     'done\n'
                     f'while :; do {shutil.which("sleep")} 0.1; done\n', encoding="utf-8")
    # The lock's fd 9 stays open across the exec, so the lock is still held.
    old = _stubborn_watchdog(box, stubborn=False,
                             usr1=f"exec {shutil.which('bash')} {other} watch")
    try:
        assert _wait_for(lambda: (box.log.parent / "stubborn").exists())
        result = box.run(_install(box), timeout=40)
        assert result.returncode == 0, result.stderr
        argv = Path(f"/proc/{old.pid}/cmdline").read_bytes().split(b"\0")
        assert argv[1:3] == [str(other).encode(), b"watch"], argv
        assert old.poll() is None and not signals.exists() and _sent(box) == []
        assert not _lock_is_free(box)
        assert box.calls("setsid") == []
        assert any("still held" in ln for ln in result.stderr.splitlines()), result.stderr
    finally:
        _end(old)


def test_install_leaves_the_watchdog_running_when_an_orphan_holds_its_lock(box):
    """A holder that is not the kiosk browser but no longer descends from the watchdog (an
    orphan that inherited the lock) would keep any new watchdog out too."""
    _quiet_desktop(box)
    box.start_session()
    _installed_launcher(box)
    real_sleep = shutil.which("sleep")
    orphan = box.log.parent / "orphan-pid"
    old = _stubborn_watchdog(box, f"( {real_sleep} 60 & echo $! > {orphan} )\n")
    try:
        assert _wait_for(lambda: orphan.exists() and orphan.read_text().strip())
        pid = int(orphan.read_text())
        assert _alive(pid)
        result = box.run(_install(box), timeout=40)
        assert result.returncode == 0, result.stderr
        assert old.poll() is None, "install.sh stopped the only watchdog that can run"
        assert box.calls("setsid") == []
        [warn] = [ln for ln in result.stderr.splitlines() if "WARN" in ln]
        assert f"{pid} " in warn and "left running" in warn, warn
    finally:
        if old.poll() is None:
            old.kill()
        old.wait(timeout=10)
        _kill_all(_pids(orphan))


# --- the desktop entries ------------------------------------------------------------


def _entry(name):
    lines = (REPO / "deploy" / "kiosk" / name).read_text(encoding="utf-8").splitlines()
    return dict(ln.split("=", 1) for ln in lines if "=" in ln and not ln.startswith("#"))


def test_the_desktop_icon_is_a_link_pcmanfm_trusts():
    """libfm launches a desktop Link without its "executable script?" prompt only when the target
    entry lives under /usr/share/ or /usr/local/share/, given as a plain path, not a file:// URI."""
    link = _entry("sigint101-link.desktop")
    assert link["Type"] == "Link"
    assert link["URL"] == "/usr/local/share/applications/sigint101.desktop"
    assert link["Name"] == "SIGINT 101"


def test_the_menu_entry_opens_the_course_and_the_autostart_entry_watches_it():
    menu = _entry("sigint101.desktop")
    assert menu["Type"] == "Application"
    assert menu["Exec"] == "/usr/local/bin/sigint101-course open"
    assert menu["Icon"] == "/usr/local/share/icons/hicolor/scalable/apps/sigint101.svg"
    auto = _entry("sigint101-autostart.desktop")
    assert auto["Exec"] == "/usr/local/bin/sigint101-course watch"
    assert auto.get("NoDisplay") == "true"


def test_every_launcher_is_valid_bash():
    for script in (LAUNCHER, INSTALL, REPO / "deploy" / "provision.sh"):
        out = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
    assert os.access(LAUNCHER, os.X_OK)
