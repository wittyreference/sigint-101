# ABOUTME: Tests for the desktop launchers in deploy/kiosk and deploy/install.sh, run as real bash.
# ABOUTME: chromium, pgrep, curl, sleep, sudo and systemctl are stubs on PATH (the OS boundary).

import os
import shutil
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

    def run(self, script, *args, **extra):
        return subprocess.run(["bash", str(script), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=20)

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
        if self.socket is not None:
            self.socket.close()
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
