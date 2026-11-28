# ABOUTME: Private read-only trial server and child launcher over a mode-0700 Unix domain socket.
# ABOUTME: Uses shared console startup with write suppression, -B -s isolation, and SO_PEERCRED.

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .app import (
    BASE_DIR,
    initialize_console_state,
    shutdown_console_state,
)
from .childproc import _die_with_parent
from .hardware import RadioProbe
from .validation import (
    UnitSettings,
    ValidationError,
    attest_open_pair,
    compute_settings_fingerprint,
    is_forbidden_trial_env_var,
    validate_course_content,
    validate_saved_state,
    verify_pair_attestation,
)

TRIAL_READY_PATH = "/_trial/ready"
_UNIX_PATH_MAX = 100


@dataclass(frozen=True)
class TrialResult:
    """Structured outcome of a private read-only updater trial."""

    ready: bool
    status_code: int
    errors: list[str]
    attestation: dict
    settings_fingerprint: str | None
    pid: int


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Stdlib HTTP client speaking over a Unix socket, optionally pinning peer PID (F9)."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout: float = 5.0,
        expected_pid: int | None = None,
    ):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = os.fspath(socket_path)
        self._expected_pid = expected_pid

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
            if self._expected_pid is not None and hasattr(socket, "SO_PEERCRED"):
                creds = sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                peer_pid, _peer_uid, _peer_gid = struct.unpack("3i", creds)
                if peer_pid != self._expected_pid:
                    raise PermissionError(
                        f"trial socket peer PID {peer_pid} != expected child {self._expected_pid}"
                    )
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def http_get_unix(
    socket_path: str | Path,
    path: str,
    *,
    timeout: float = 5.0,
    expected_pid: int | None = None,
) -> tuple[int, bytes]:
    """Perform a single HTTP GET over a local Unix domain socket."""
    conn = _UnixHTTPConnection(socket_path, timeout=timeout, expected_pid=expected_pid)
    try:
        conn.request("GET", path, headers={"Host": "localhost"})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _radio_free_probe() -> RadioProbe:
    """Honest radio-free probe used during read-only updater trials (no USB/hardware touch)."""
    return RadioProbe(present=False, known=True, method="trial", detail="trial mode")


def create_trial_app(
    *,
    pack_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
    pool_path: str | Path | None = None,
    release_root: str | Path | None = None,
    templates_dir: str | Path | None = None,
    required_lesson_ids: frozenset[int] | set[int] | None = None,
    require_release_manifest: bool = True,
    settings_fingerprint: str | None = None,
    probe=_radio_free_probe,
) -> FastAPI:
    """Build a minimal read-only validation app serving ONLY /_trial/ready."""
    resolved_templates = (
        Path(templates_dir) if templates_dir is not None else BASE_DIR / "templates"
    )
    effective_pack = Path(pack_dir) if pack_dir is not None else BASE_DIR.parent / "lesson_packs"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.startup_errors = []
        app.state.initialized = False
        try:
            initialize_console_state(
                app.state,
                pack_dir=effective_pack,
                data_dir=data_dir,
                pool_path=pool_path,
                managed=True,
                read_only=True,
                trial=True,
                probe=probe,
                list_devices=lambda: [],
            )
            app.state.initialized = True
        except Exception as exc:  # noqa: BLE001
            app.state.startup_errors.append(f"startup failed: {exc}")
        try:
            yield
        finally:
            if app.state.initialized:
                await shutdown_console_state(app.state, trial=True)

    app = FastAPI(
        title="SIGINT 101 Trial",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get(TRIAL_READY_PATH)
    def trial_ready():
        errors: list[str] = list(app.state.startup_errors)
        attestation: dict = {}
        if app.state.initialized:
            errors.extend(
                validate_course_content(
                    lessons=app.state.lessons,
                    exam_pool=app.state.exam_pool,
                    exam_disabled_reason=app.state.exam_disabled_reason,
                    templates_dir=resolved_templates,
                    required_lesson_ids=required_lesson_ids,
                )
            )
            errors.extend(
                validate_saved_state(
                    lessons=app.state.lessons,
                    exam_pool=app.state.exam_pool,
                    progress=app.state.progress,
                    exam_store=app.state.exam_store,
                )
            )
            try:
                attestation = attest_open_pair(
                    progress=app.state.progress,
                    exam_store=app.state.exam_store,
                    data_dir=app.state.data_dir,
                    pack_dir=app.state.pack_dir,
                    pool_path=app.state.pool_path,
                    package_root=release_root,
                    require_release_manifest=require_release_manifest,
                    settings_fingerprint=settings_fingerprint,
                )
            except ValidationError as exc:
                errors.append(str(exc))

        ready = not errors
        status = 200 if ready else 503
        return JSONResponse(
            status_code=status,
            content={"ready": ready, "errors": errors, "attestation": attestation},
        )

    return app


def _make_private_socket_dir(socket_parent_dir: str | Path | None) -> tuple[Path, Path]:
    """Create an exclusive mode-0700 directory for the trial Unix socket within sun_path limits."""
    base = Path(socket_parent_dir) if socket_parent_dir is not None else Path(tempfile.gettempdir())
    sock_dir = Path(tempfile.mkdtemp(prefix="t_", dir=str(base)))
    os.chmod(sock_dir, 0o700)
    sock_path = sock_dir / "s"
    if len(os.fsencode(sock_path)) > _UNIX_PATH_MAX:
        os.rmdir(sock_dir)
        sock_dir = Path(tempfile.mkdtemp(prefix="s101t_", dir=tempfile.gettempdir()))
        os.chmod(sock_dir, 0o700)
        sock_path = sock_dir / "s"
    return sock_dir, sock_path


def _stop_and_reap(proc: subprocess.Popen, *, grace_s: float = 3.0) -> None:
    """Stop the trial process group cleanly and always reap the child."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=grace_s)
    else:
        proc.wait()


def run_private_trial(
    *,
    python_executable: str | Path,
    release_root: str | Path,
    data_dir: str | Path,
    pack_dir: str | Path | None = None,
    pool_path: str | Path | None = None,
    unit_settings: UnitSettings | None = None,
    expected_release_id: str | None = None,
    expected_commit: str | None = None,
    expected_generation_id: str | None = None,
    required_lesson_ids: frozenset[int] | set[int] | None = None,
    require_release_manifest: bool = True,
    socket_parent_dir: str | Path | None = None,
    timeout_s: float = 15.0,
    on_ready: Callable[[Path, int], None] | None = None,
) -> TrialResult:
    """Run a candidate (release, data_dir) pair in a private, read-only, reaped child process."""
    rel_root = Path(release_root).resolve()
    gen_dir = Path(data_dir).resolve()
    fingerprint = (
        compute_settings_fingerprint(unit_settings.unit_only())
        if unit_settings is not None
        else None
    )

    sock_dir, sock_path = _make_private_socket_dir(socket_parent_dir)
    cfg_path = sock_dir / "cfg.json"
    log_path = sock_dir / "trial.log"

    # F1: pack and pool always come from the candidate release (never from unit/drop-in env).
    effective_pack = (
        str(Path(pack_dir).resolve())
        if pack_dir is not None
        else str(rel_root / "lesson_packs")
    )
    effective_pool = str(Path(pool_path).resolve()) if pool_path is not None else None

    cfg_payload = {
        "pack_dir": effective_pack,
        "data_dir": str(gen_dir),
        "pool_path": effective_pool,
        "required_lesson_ids": (
            sorted(required_lesson_ids) if required_lesson_ids is not None else None
        ),
        "require_release_manifest": require_release_manifest,
        "settings_fingerprint": fingerprint,
    }
    priv_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    cfg_fd = os.open(os.fspath(cfg_path), priv_flags, 0o600)
    try:
        os.write(cfg_fd, json.dumps(cfg_payload, sort_keys=True).encode("utf-8"))
    finally:
        os.close(cfg_fd)

    # F1/F5/F10: strip PYTHON*, LD_*, and release-path override vars before adding our own.
    child_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(sock_dir)),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    if unit_settings is not None:
        for k, v in unit_settings.env.items():
            if not is_forbidden_trial_env_var(k):
                child_env[k] = v
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env["PYTHONPATH"] = str(rel_root)

    # F10: -B suppresses bytecode and -s excludes user site-packages.
    cmd = [
        str(python_executable),
        "-B",
        "-s",
        "-m",
        "sigint101.trial",
        "--socket",
        str(sock_path),
        "--config",
        str(cfg_path),
    ]

    # F11: send child stdout/stderr to a private file so >64 KiB output can never deadlock a pipe.
    log_fd = os.open(
        os.fspath(log_path),
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(sock_dir),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            close_fds=True,
            start_new_session=True,
            preexec_fn=_die_with_parent(),
        )
    finally:
        os.close(log_fd)

    try:
        deadline = time.monotonic() + timeout_s
        status_code = 503
        body_dict: dict | None = None
        last_err = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log_tail = (
                    log_path.read_text(encoding="utf-8", errors="replace").strip()[-500:]
                    if log_path.is_file()
                    else ""
                )
                return TrialResult(
                    ready=False,
                    status_code=503,
                    errors=[f"trial process exited early ({proc.returncode}): {log_tail}"],
                    attestation={},
                    settings_fingerprint=fingerprint,
                    pid=proc.pid,
                )
            if sock_path.exists():
                try:
                    # F9: verify SO_PEERCRED peer PID matches our spawned trial child.
                    status_code, raw = http_get_unix(
                        sock_path, TRIAL_READY_PATH, timeout=2.0, expected_pid=proc.pid
                    )
                    body_dict = json.loads(raw.decode("utf-8"))
                    break
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    last_err = str(exc)
            time.sleep(0.05)

        if body_dict is None:
            return TrialResult(
                ready=False,
                status_code=503,
                errors=[f"trial timed out after {timeout_s}s: {last_err or 'socket not ready'}"],
                attestation={},
                settings_fingerprint=fingerprint,
                pid=proc.pid,
            )

        if on_ready is not None:
            on_ready(sock_path, proc.pid)

        errors = list(body_dict.get("errors") or [])
        attestation = body_dict.get("attestation") or {}
        ready = bool(body_dict.get("ready")) and status_code == 200

        if ready:
            try:
                verify_pair_attestation(
                    attestation,
                    expected_release_id=expected_release_id,
                    expected_commit=expected_commit,
                    expected_package_root=rel_root,
                    expected_pack_dir=effective_pack,
                    expected_pool_path=effective_pool,
                    expected_generation_id=expected_generation_id,
                    expected_data_dir=gen_dir,
                )
            except ValidationError as exc:
                ready = False
                status_code = 503
                errors.append(str(exc))

        return TrialResult(
            ready=ready,
            status_code=status_code,
            errors=errors,
            attestation=attestation,
            settings_fingerprint=fingerprint,
            pid=proc.pid,
        )
    finally:
        _stop_and_reap(proc)
        for cleanup_file in (sock_path, cfg_path, log_path):
            try:
                cleanup_file.unlink()
            except FileNotFoundError:
                pass
        try:
            os.rmdir(sock_dir)
        except OSError:
            pass


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run private read-only SIGINT 101 trial server.")
    parser.add_argument("--socket", required=True, help="Unix domain socket path to bind.")
    parser.add_argument("--config", required=True, help="Private JSON config file path.")
    args = parser.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    req_ids = cfg.get("required_lesson_ids")
    app = create_trial_app(
        pack_dir=cfg.get("pack_dir"),
        data_dir=cfg.get("data_dir"),
        pool_path=cfg.get("pool_path"),
        required_lesson_ids=frozenset(req_ids) if req_ids is not None else None,
        require_release_manifest=bool(cfg.get("require_release_manifest", True)),
        settings_fingerprint=cfg.get("settings_fingerprint"),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            uds=args.socket,
            log_level="warning",
            access_log=False,
        )
    )
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
