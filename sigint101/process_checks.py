# ABOUTME: Process, cgroup-drain, public-listener, kiosk-page, and systemd start-limit checks.
# ABOUTME: Ensures no console writer or kiosk browser remains active before snapshotting or apply.

from __future__ import annotations

import http.client
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .kiosk import KIOSK_PROFILE, proc_cmdlines

_INACTIVE_STATES = frozenset({"inactive", "failed"})
_BUSY_SUBSTATES = frozenset({"deactivating", "stopping", "running", "start-pre", "auto-restart"})
_MINIMAL_SYSTEMCTL_ENV_KEYS = (
    "PATH",
    "HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "LANG",
)


class KioskPageOpenError(RuntimeError):
    """Raised when the kiosk Chromium page is open during enrollment, apply, or rollback."""


class ServiceDrainError(RuntimeError):
    """Raised when the service unit, cgroup, or public listener is not fully drained."""


@dataclass(frozen=True)
class DrainReport:
    """Verified post-stop drain state for the console service."""

    drained: bool
    active_state: str
    sub_state: str
    cgroup_pids: tuple[int, ...]
    listener_active: bool
    killed_cgroup: bool


def kiosk_profile_path(
    profile_dir: str | Path | None = None,
    *,
    home: str | Path | None = None,
) -> Path:
    if profile_dir is not None:
        return Path(profile_dir)
    base_home = Path(home) if home is not None else Path.home()
    return base_home / KIOSK_PROFILE


def is_kiosk_page_open(
    profile_dir: str | Path | None = None,
    *,
    home: str | Path | None = None,
    process_lister: Callable[[], list] = proc_cmdlines,
) -> bool:
    """Check whether any running process matches the kiosk profile (start-kiosk.sh:30-32, F9)."""
    profile = str(kiosk_profile_path(profile_dir, home=home)).rstrip("/")
    marker = f"user-data-dir={profile}"
    pattern = re.compile(rf"(?:^|\s|--){re.escape(marker)}/?(?:\s|$)")
    for _pid, argv in process_lister():
        if pattern.search(" ".join(argv)):
            return True
    return False


def require_kiosk_page_closed(
    profile_dir: str | Path | None = None,
    *,
    home: str | Path | None = None,
    process_lister: Callable[[], list] = proc_cmdlines,
) -> None:
    """Refuse maintenance while the kiosk page is open; instruct operator to tap Exit."""
    if is_kiosk_page_open(profile_dir, home=home, process_lister=process_lister):
        raise KioskPageOpenError(
            "kiosk page is open; tap Exit SIGINT 101 and retry"
        )


def is_tcp_port_listening(
    host: str = "127.0.0.1",
    port: int = 8008,
    *,
    timeout_s: float = 0.3,
) -> bool:
    """Return True if anything accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def read_cgroup_pids(cgroup_path: str | Path) -> list[int]:
    """Read live PIDs recursively from a Linux cgroup subtree's cgroup.procs files (F11)."""
    path = Path(cgroup_path)
    if path.name == "cgroup.procs":
        procs_files = [path] if path.is_file() else []
    elif path.is_dir():
        procs_files = sorted(path.rglob("cgroup.procs"))
    else:
        procs_files = []
    pids: set[int] = set()
    for procs_file in procs_files:
        try:
            text = procs_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            token = line.strip()
            if token.isdigit():
                pids.add(int(token))
    return sorted(pids)


def wait_service_drained(
    *,
    stop_unit: Callable[[float], None],
    get_unit_state: Callable[[], tuple[str, str, int]],
    get_cgroup_pids: Callable[[], list[int]],
    is_listener_active: Callable[[], bool],
    kill_cgroup: Callable[[], None] | None = None,
    timeout_s: float = 15.0,
    poll_s: float = 0.05,
) -> DrainReport:
    """Stop the service and require inactive unit + empty cgroup + gone listener simultaneously."""
    deadline = time.monotonic() + timeout_s
    stop_unit(timeout_s)

    killed_cgroup = False
    active_state = "unknown"
    sub_state = "unknown"
    cgroup_pids: list[int] = []
    listener_active = True

    while True:
        active_state, sub_state, main_pid = get_unit_state()
        cgroup_pids = list(get_cgroup_pids())
        listener_active = bool(is_listener_active())
        unit_stopped = (
            active_state in _INACTIVE_STATES
            and sub_state not in _BUSY_SUBSTATES
            and main_pid == 0
        )

        if unit_stopped and not cgroup_pids and not listener_active:
            return DrainReport(
                drained=True,
                active_state=active_state,
                sub_state=sub_state,
                cgroup_pids=(),
                listener_active=False,
                killed_cgroup=killed_cgroup,
            )

        if unit_stopped and cgroup_pids and kill_cgroup is not None and not killed_cgroup:
            kill_cgroup()
            killed_cgroup = True
            continue

        if time.monotonic() >= deadline:
            reasons: list[str] = []
            if not unit_stopped:
                reasons.append(
                    f"unit still active ({active_state}/{sub_state}, main_pid={main_pid})"
                )
            if cgroup_pids:
                reasons.append(
                    f"ancillary processes still alive in service cgroup: {cgroup_pids}"
                )
            if listener_active:
                reasons.append("public listener still active")
            raise ServiceDrainError("service failed to drain: " + "; ".join(reasons))

        time.sleep(poll_s)


def start_unit_with_reset(
    *,
    reset_failed: Callable[[], None],
    start_unit: Callable[[], None],
) -> None:
    """Reset systemd failed/start-limit state before requesting a service start (U8)."""
    reset_failed()
    start_unit()


class SystemdUserController:
    """Bounded subprocess adapter for systemd --user control satisfying ServiceAdapter."""

    def __init__(
        self,
        unit: str = "sigint101.service",
        *,
        host: str = "127.0.0.1",
        port: int = 8008,
        http_timeout_s: float = 2.0,
        env: dict[str, str] | None = None,
    ):
        self.unit = unit
        self.host = host
        self.port = int(port)
        self.http_timeout_s = http_timeout_s
        self._env = env

    def _effective_env(self) -> dict[str, str]:
        if self._env is not None:
            return dict(self._env)
        clean: dict[str, str] = {}
        for key in _MINIMAL_SYSTEMCTL_ENV_KEYS:
            val = os.environ.get(key)
            if val is not None:
                clean[key] = val
        clean.setdefault("PATH", "/usr/bin:/bin")
        clean.setdefault("LANG", "C.UTF-8")
        return clean

    def _run(self, *args: str, timeout_s: float = 15.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", "--user", *args],
            env=self._effective_env(),
            capture_output=True,
            text=True,
            close_fds=True,
            timeout=timeout_s,
        )

    @staticmethod
    def parse_show_output(text: str) -> dict[str, str]:
        props: dict[str, str] = {}
        for line in text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                props[key.strip()] = value.strip()
        return props

    def show(self, *properties: str, timeout_s: float = 10.0) -> dict[str, str]:
        args = ["show", self.unit]
        for prop in properties:
            args.extend(["-p", prop])
        res = self._run(*args, timeout_s=timeout_s)
        if res.returncode != 0:
            raise ServiceDrainError(f"systemctl show failed: {res.stderr.strip()}")
        return self.parse_show_output(res.stdout)

    def unit_state(self) -> tuple[str, str, int]:
        props = self.show("ActiveState", "SubState", "MainPID")
        main_pid = int(props.get("MainPID", "0") or "0")
        return props.get("ActiveState", "unknown"), props.get("SubState", "unknown"), main_pid

    def cgroup_pids(self, *, cgroup_root: str | Path = "/sys/fs/cgroup") -> list[int]:
        props = self.show("ControlGroup")
        cg = props.get("ControlGroup", "").lstrip("/")
        if not cg:
            return []
        return read_cgroup_pids(Path(cgroup_root) / cg)

    def get_cgroup_pids(self, *, cgroup_root: str | Path = "/sys/fs/cgroup") -> list[int]:
        return self.cgroup_pids(cgroup_root=cgroup_root)

    def is_listener_active(self) -> bool:
        return is_tcp_port_listening(self.host, self.port)

    def is_http_ready(self) -> bool:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.http_timeout_s)
        try:
            conn.request("GET", "/api/hardware", headers={"Host": f"{self.host}:{self.port}"})
            resp = conn.getresponse()
            resp.read()
            return resp.status == 200
        except (OSError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    def stop(self, timeout_s: float = 15.0) -> None:
        res = self._run("stop", self.unit, timeout_s=timeout_s)
        if res.returncode != 0:
            raise ServiceDrainError(f"systemctl stop failed: {res.stderr.strip()}")

    def kill_cgroup(self, timeout_s: float = 10.0) -> None:
        self._run("kill", "--kill-who=all", "--signal=SIGKILL", self.unit, timeout_s=timeout_s)

    def reset_failed(self, timeout_s: float = 10.0) -> None:
        self._run("reset-failed", self.unit, timeout_s=timeout_s)

    def daemon_reload(self, timeout_s: float = 10.0) -> None:
        res = self._run("daemon-reload", timeout_s=timeout_s)
        if res.returncode != 0:
            raise ServiceDrainError(f"systemctl daemon-reload failed: {res.stderr.strip()}")

    def start(self, timeout_s: float = 15.0) -> None:
        res = self._run("start", self.unit, timeout_s=timeout_s)
        if res.returncode != 0:
            raise ServiceDrainError(f"systemctl start failed: {res.stderr.strip()}")
