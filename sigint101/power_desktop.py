# ABOUTME: Learner-session desktop notifier for low-battery shutdowns (reads notice.json only).
# ABOUTME: Warns learners who left the course for the desktop or Terminal and clears on recovery.

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .power import DEFAULT_NOTICE_PATH, NOTICE_POLL_S, _to_finite_float, read_power_notice

LOGGER = logging.getLogger("sigint101.power_desktop")

APP_NAME = "SIGINT 101"
NOTIFY_ICON = "battery-caution"
NOTIFY_TIMEOUT_S = 5.0
NOTIFY_EXPIRE_MS = 6000  # Refreshed every 2 s; auto-expires cleanly if notifier crashes.

_UINT32_RE = re.compile(r"\buint32\s+(\d+)\b")
_BUSCTL_U_RE = re.compile(r"^\s*u\s+(\d+)\s*$")


def format_desktop_notice(notice: dict | None) -> tuple[str, str] | None:
    """Format a validated read_power_notice() payload into (summary, body) for desktop alerts."""
    if not isinstance(notice, dict) or notice.get("active") is not True:
        return None
    failed = notice.get("failed")
    if not isinstance(failed, bool):
        return None
    voltage = _to_finite_float(notice.get("voltage"))
    threshold_v = _to_finite_float(notice.get("threshold_v"))
    volts = (
        f"Pack {voltage:.2f} V (limit {threshold_v:.2f} V). "
        if voltage is not None and threshold_v is not None
        else ""
    )
    if failed:
        retry = _to_finite_float(notice.get("retry_in_s"))
        if retry is None:
            return None
        sec = max(0, int(retry))
        when = "RETRYING NOW" if sec == 0 else f"RETRYING IN {sec} s"
        summary = f"LOW BATTERY · POWER-OFF FAILED · {when}"
        body = f"Automatic power-off failed. {volts}Save your work and shut down now."
        return summary, body

    rem = _to_finite_float(notice.get("remaining_s"))
    if rem is None:
        return None
    sec = max(0, int(rem))
    when = "POWERING OFF NOW" if sec == 0 else f"POWERING OFF IN {sec} s"
    summary = f"LOW BATTERY · {when}"
    body = f"{volts}Plug in charger now to cancel."
    return summary, body


def _session_env() -> dict[str, str]:
    """Minimal environment carrying the learner's own D-Bus session bus address."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = xdg_runtime
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not bus:
        candidate = Path(xdg_runtime) / "bus"
        if candidate.exists():
            bus = f"unix:path={candidate}"
    if bus:
        env["DBUS_SESSION_BUS_ADDRESS"] = bus
    for key in ("WAYLAND_DISPLAY", "DISPLAY"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


class GdbusNotifier:
    """Delivers and clears in-place freedesktop notifications over the user's session D-Bus."""

    def __init__(self, *, gdbus_bin: str, runner=subprocess.run):
        self._gdbus = gdbus_bin
        self._runner = runner
        self._notify_id: int = 0

    def show(self, summary: str, body: str, *, failed: bool) -> bool:
        cmd = [
            self._gdbus,
            "call",
            "--session",
            "--dest",
            "org.freedesktop.Notifications",
            "--object-path",
            "/org/freedesktop/Notifications",
            "--method",
            "org.freedesktop.Notifications.Notify",
            APP_NAME,
            str(self._notify_id),
            NOTIFY_ICON,
            summary,
            body,
            "[]",
            "{'urgency': <byte 2>}",
            str(NOTIFY_EXPIRE_MS),
        ]
        try:
            res = self._runner(
                cmd,
                check=False,
                timeout=NOTIFY_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                close_fds=True,
                env=_session_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("gdbus notify failed: %s", exc)
            return False
        if res.returncode != 0:
            LOGGER.warning(
                "gdbus notify exited %d: %s",
                res.returncode,
                (res.stderr or res.stdout or "").strip(),
            )
            return False
        match = _UINT32_RE.search(res.stdout or "")
        if match:
            self._notify_id = int(match.group(1))
        return True

    def clear(self) -> None:
        if self._notify_id <= 0:
            return
        nid = self._notify_id
        self._notify_id = 0
        cmd = [
            self._gdbus,
            "call",
            "--session",
            "--dest",
            "org.freedesktop.Notifications",
            "--object-path",
            "/org/freedesktop/Notifications",
            "--method",
            "org.freedesktop.Notifications.CloseNotification",
            str(nid),
        ]
        try:
            self._runner(
                cmd,
                check=False,
                timeout=NOTIFY_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                close_fds=True,
                env=_session_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("gdbus CloseNotification failed: %s", exc)


class BusctlNotifier:
    """Close-capable fallback notifier using systemd's busctl over the user session D-Bus."""

    def __init__(self, *, busctl_bin: str, runner=subprocess.run):
        self._busctl = busctl_bin
        self._runner = runner
        self._notify_id: int = 0

    def show(self, summary: str, body: str, *, failed: bool) -> bool:
        cmd = [
            self._busctl,
            "--user",
            "call",
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
            "susssasa{sv}i",
            APP_NAME,
            str(self._notify_id),
            NOTIFY_ICON,
            summary,
            body,
            "0",
            "1",
            "urgency",
            "y",
            "2",
            str(NOTIFY_EXPIRE_MS),
        ]
        try:
            res = self._runner(
                cmd,
                check=False,
                timeout=NOTIFY_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                close_fds=True,
                env=_session_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("busctl notify failed: %s", exc)
            return False
        if res.returncode != 0:
            LOGGER.warning(
                "busctl notify exited %d: %s",
                res.returncode,
                (res.stderr or res.stdout or "").strip(),
            )
            return False
        match = _BUSCTL_U_RE.match(res.stdout or "")
        if match:
            self._notify_id = int(match.group(1))
        return True

    def clear(self) -> None:
        if self._notify_id <= 0:
            return
        nid = self._notify_id
        self._notify_id = 0
        cmd = [
            self._busctl,
            "--user",
            "call",
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "CloseNotification",
            "u",
            str(nid),
        ]
        try:
            self._runner(
                cmd,
                check=False,
                timeout=NOTIFY_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                close_fds=True,
                env=_session_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("busctl CloseNotification failed: %s", exc)


class NullNotifier:
    """Log-only fallback when no close-capable freedesktop transport (gdbus/busctl) is installed."""

    def __init__(self):
        self._warned = False

    def show(self, summary: str, body: str, *, failed: bool) -> bool:
        if not self._warned:
            LOGGER.warning(
                "no close-capable desktop notification transport (gdbus/busctl) found: %s — %s",
                summary,
                body,
            )
            self._warned = True
        return False

    def clear(self) -> None:
        self._warned = False


def build_notifier():
    """Choose a close-capable learner-session freedesktop notification backend (gdbus or busctl)."""
    gdbus = shutil.which("gdbus")
    if gdbus:
        return GdbusNotifier(gdbus_bin=gdbus)
    busctl = shutil.which("busctl")
    if busctl:
        return BusctlNotifier(busctl_bin=busctl)
    return NullNotifier()


class DesktopNoticeMonitor:
    """Watches notice.json from the learner's user session and drives desktop alerts."""

    def __init__(
        self,
        *,
        notice_path: Path = DEFAULT_NOTICE_PATH,
        clock=time.monotonic,
        notifier=None,
    ):
        self.notice_path = Path(notice_path)
        self._clock = clock
        self._notifier = notifier if notifier is not None else build_notifier()
        self._active = False

    def tick(self) -> str:
        notice = read_power_notice(self.notice_path, clock=self._clock)
        formatted = format_desktop_notice(notice)
        if formatted is None:
            if self._active:
                self._notifier.clear()
                self._active = False
                LOGGER.info("low-battery desktop notice cleared")
            return "INACTIVE"
        summary, body = formatted
        failed = bool(notice["failed"])
        self._notifier.show(summary, body, failed=failed)
        self._active = True
        return "FAILED" if failed else "ACTIVE"

    def shutdown(self) -> None:
        if self._active:
            self._notifier.clear()
            self._active = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SIGINT 101 learner-session low-battery desktop notice"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check notice.json once and exit 0.",
    )
    parser.add_argument(
        "--notice-path",
        type=Path,
        default=DEFAULT_NOTICE_PATH,
        help="Path to the runtime notice.json state file.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=NOTICE_POLL_S,
        help="Polling interval in seconds (default 2.0).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    monitor = DesktopNoticeMonitor(notice_path=args.notice_path)
    if args.once:
        monitor.tick()
        return 0

    stop = False

    def _handle_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    interval = max(0.5, float(args.poll_interval))
    try:
        while not stop:
            monitor.tick()
            time.sleep(interval)
    finally:
        monitor.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
