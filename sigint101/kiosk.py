# ABOUTME: Exit SIGINT 101 to the Pi desktop: flag the exit, then close the full-screen browser.
# ABOUTME: The flag sits in XDG_RUNTIME_DIR (a tmpfs) so the watchdog holds off until the next boot.

from __future__ import annotations

import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path

# The kiosk Chromium's own profile; deploy/kiosk/start-kiosk.sh launches it with this
# --user-data-dir, which is how the kiosk browser is told apart from any other Chromium.
KIOSK_PROFILE = Path(".config") / "chromium-kiosk"

# The watchdog relaunches the kiosk after a crash only while this flag is absent. The desktop
# icon's opener removes it again.
EXIT_FLAG_NAME = "sigint101-kiosk-exited"


@dataclass(frozen=True)
class KioskExit:
    """What an Exit did: where it flagged the exit, which browsers it closed, and why not."""

    flag: Path
    closed: tuple
    reason: str = ""

    @property
    def exited(self) -> bool:
        return bool(self.closed)


def exit_flag(runtime_dir=None) -> Path:
    """The exit flag's path, in the session's runtime dir (systemd's /run/user/<uid> by default)."""
    runtime = runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / EXIT_FLAG_NAME


def proc_cmdlines(proc=Path("/proc")) -> list:
    """Every process's (pid, argv) from /proc. Kernel threads and vanished pids are skipped."""
    found = []
    for entry in Path(proc).iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if argv:
            found.append((int(entry.name), argv))
    return sorted(found)


def request_exit(*, runtime_dir=None, home=None, processes=proc_cmdlines,
                 send_signal=os.kill) -> KioskExit:
    """Flag a deliberate exit, then SIGTERM the kiosk browser so the desktop shows.

    The flag goes down first: the watchdog takes "no kiosk browser alive" for a crash unless the
    flag is already there. Only the browser process itself is signalled (its helpers carry a
    --type= and go down with it); a Chromium on any other profile is the learner's own and stays.
    """
    flag = exit_flag(runtime_dir)
    flag.write_text(f"exit requested {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n", encoding="utf-8")
    marker = f"--user-data-dir={Path(home or Path.home()) / KIOSK_PROFILE}"
    # Matched on the joined command line, whole flags only: Chromium retitles its processes, so
    # /proc shows the browser's flags as one space-joined string rather than one per argument.
    is_kiosk = re.compile(rf"(?:^|\s){re.escape(marker)}(?:\s|$)")
    is_helper = re.compile(r"(?:^|\s)--type=")
    browsers = [pid for pid, argv in processes()
                if is_kiosk.search(" ".join(argv)) and not is_helper.search(" ".join(argv))]
    closed = []
    for pid in browsers:
        try:
            send_signal(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        closed.append(pid)
    reason = "" if closed else (f"no kiosk browser is running (none with {marker}), "
                                "so there is nothing to close")
    return KioskExit(flag=flag, closed=tuple(closed), reason=reason)
