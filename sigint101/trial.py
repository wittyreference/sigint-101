# ABOUTME: Private read-only trial server and child launcher over a mode-0700 Unix domain socket.
# ABOUTME: Pure-stdlib at import time so control generation runs candidate trials under -B -I -S.

from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if __package__:
    from .migrations import EXAM_SCHEMA_VERSION, PROGRESS_SCHEMA_VERSION
    from .unit_settings import (
        UnitSettings,
        compute_settings_fingerprint,
        is_forbidden_trial_env_var,
    )
else:
    EXAM_SCHEMA_VERSION = 1
    PROGRESS_SCHEMA_VERSION = 1
    UnitSettings = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from fastapi import FastAPI

TRIAL_READY_PATH = "/_trial/ready"
_UNIX_PATH_MAX = 100
_PR_SET_PDEATHSIG = 1


def _libc_prctl():
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None


_PRCTL = _libc_prctl()


def _die_with_parent():
    """Preexec hook that SIGKILLs the trial child if its spawning thread exits."""
    if _PRCTL is None:
        return None
    parent = os.getpid()

    def hook():
        _PRCTL(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        if os.getppid() != parent:
            os._exit(1)

    return hook


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
    """Stdlib HTTP client speaking over a Unix socket, optionally pinning peer PID."""

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


def _radio_free_probe():
    """Honest radio-free probe used during read-only updater trials (no USB/hardware touch)."""
    from .hardware import RadioProbe  # noqa: PLC0415

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
    from fastapi import FastAPI as _FastAPI  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    from .app import (  # noqa: PLC0415
        BASE_DIR,
        initialize_console_state,
        shutdown_console_state,
    )
    from .validation import (  # noqa: PLC0415
        ValidationError,
        attest_open_pair,
        validate_course_content,
        validate_saved_state,
    )

    resolved_templates = (
        Path(templates_dir) if templates_dir is not None else BASE_DIR / "templates"
    )
    effective_pack = Path(pack_dir) if pack_dir is not None else BASE_DIR.parent / "lesson_packs"

    @asynccontextmanager
    async def lifespan(app: _FastAPI):
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

    app = _FastAPI(
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


def _resolve_pool_in_release_stdlib(
    release_root: Path, pool_path: str | Path | None = None
) -> Path:
    if pool_path is not None:
        return Path(pool_path).resolve()
    exam_dir = release_root / "sigint101" / "exam_data"
    if exam_dir.is_dir():
        candidates = sorted(
            p for p in exam_dir.glob("*.json") if p.is_file() and not p.is_symlink()
        )
        if len(candidates) == 1:
            return candidates[0].resolve()
        if len(candidates) > 1:
            raise ValueError(
                f"ambiguous exam pool files in {exam_dir.name}: "
                + ", ".join(c.name for c in candidates)
            )
    raise ValueError(f"exam pool file missing under {exam_dir}")


def _verify_trial_attestation_stdlib(
    attestation: dict,
    *,
    expected_release_id: str | None = None,
    expected_commit: str | None = None,
    expected_package_root: Path | None = None,
    expected_pack_dir: str | Path | None = None,
    expected_pool_path: str | Path | None = None,
    expected_generation_id: str | None = None,
    expected_data_dir: Path | None = None,
) -> list[str]:
    """Pure-stdlib verification of trial attestation so control generation never imports jinja2."""
    if not isinstance(attestation, dict):
        return ["missing pair attestation payload"]
    errors: list[str] = []
    rel = attestation.get("release") or {}
    gen = attestation.get("generation") or {}
    dbs = attestation.get("databases") or {}

    if expected_release_id is not None and rel.get("release_id") != expected_release_id:
        errors.append(
            f"release_id mismatch: attested {rel.get('release_id')!r}, "
            f"expected {expected_release_id!r}"
        )
    if expected_commit is not None and rel.get("commit") != expected_commit:
        errors.append(
            f"commit mismatch: attested {rel.get('commit')!r}, expected {expected_commit!r}"
        )
    if expected_package_root is not None:
        exp_root = expected_package_root.resolve()
        exp_st = os.stat(exp_root)
        if (
            rel.get("package_root") != str(exp_root)
            or rel.get("st_dev") != exp_st.st_dev
            or rel.get("st_ino") != exp_st.st_ino
        ):
            errors.append("package root identity mismatch in attestation")

        exp_pack = (
            Path(expected_pack_dir).resolve()
            if expected_pack_dir is not None
            else (exp_root / "lesson_packs").resolve()
        )
        exp_pack_st = os.stat(exp_pack)
        pack_att = attestation.get("pack") or {}
        if (
            pack_att.get("path") != str(exp_pack)
            or pack_att.get("st_dev") != exp_pack_st.st_dev
            or pack_att.get("st_ino") != exp_pack_st.st_ino
        ):
            errors.append("lesson pack identity mismatch in attestation")

        try:
            exp_pool = _resolve_pool_in_release_stdlib(exp_root, pool_path=expected_pool_path)
            exp_pool_st = os.stat(exp_pool)
            pool_att = attestation.get("pool") or {}
            if (
                pool_att.get("path") != str(exp_pool)
                or pool_att.get("st_dev") != exp_pool_st.st_dev
                or pool_att.get("st_ino") != exp_pool_st.st_ino
            ):
                errors.append("exam pool identity mismatch in attestation")
        except (OSError, ValueError) as exc:
            errors.append(str(exc))

    if expected_generation_id is not None and gen.get("generation_id") != expected_generation_id:
        errors.append(
            f"generation_id mismatch: attested {gen.get('generation_id')!r}, "
            f"expected {expected_generation_id!r}"
        )

    prog_att = dbs.get("progress") or {}
    exam_att = dbs.get("exam") or {}
    if prog_att.get("user_version") != PROGRESS_SCHEMA_VERSION:
        errors.append(
            f"progress schema head mismatch: {prog_att.get('user_version')} != "
            f"{PROGRESS_SCHEMA_VERSION}"
        )
    if exam_att.get("user_version") != EXAM_SCHEMA_VERSION:
        errors.append(
            f"exam schema head mismatch: {exam_att.get('user_version')} != {EXAM_SCHEMA_VERSION}"
        )
    if prog_att.get("journal_mode") != "delete" or exam_att.get("journal_mode") != "delete":
        errors.append("attested databases must be in journal_mode=delete")

    if expected_data_dir is not None:
        exp_data = expected_data_dir.resolve()
        exp_data_st = os.stat(exp_data)
        if (
            gen.get("data_dir") != str(exp_data)
            or gen.get("st_dev") != exp_data_st.st_dev
            or gen.get("st_ino") != exp_data_st.st_ino
        ):
            errors.append("data generation directory identity mismatch in attestation")
        for key, filename in (("progress", "progress.db"), ("exam", "exam.db")):
            actual = dbs.get(key) or {}
            exp_file = (exp_data / filename).resolve()
            exp_st = os.stat(exp_file)
            if (
                actual.get("path") != str(exp_file)
                or actual.get("st_dev") != exp_st.st_dev
                or actual.get("st_ino") != exp_st.st_ino
            ):
                errors.append(f"open {key} database inode/path mismatch in attestation")
    return errors


def run_private_trial(
    *,
    python_executable: str | Path,
    release_root: str | Path,
    data_dir: str | Path,
    runtime_dir: str | Path | None = None,
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
    """Run a candidate (release, data_dir) pair in a private, read-only, reaped child process.

    When `runtime_dir` is supplied (always true during managed apply), launches the candidate
    release's `sigint101/trial.py` under `host_python -B -I -S` so the trial imports dependencies
    strictly from the enrolled sealed runtime (matching `service_entry.py` isolation).
    """
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

    # Pack and pool always come from the candidate release (never from unit/drop-in env).
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
        raw_cfg = json.dumps(cfg_payload, sort_keys=True).encode("utf-8")
        view = memoryview(raw_cfg)
        written = 0
        while written < len(view):
            written += os.write(cfg_fd, view[written:])
    finally:
        os.close(cfg_fd)

    # Strip PYTHON*, LD_*, and release-path override vars before adding our own.
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

    if runtime_dir is not None:
        resolved_rt = Path(runtime_dir).resolve()
        # F9: -B -I -S mirrors service_entry.py so trial imports ONLY from release + sealed runtime.
        cmd = [
            str(python_executable),
            "-B",
            "-I",
            "-S",
            str(rel_root / "sigint101" / "trial.py"),
            "--release-root",
            str(rel_root),
            "--runtime-dir",
            str(resolved_rt),
            "--socket",
            str(sock_path),
            "--config",
            str(cfg_path),
        ]
    else:
        child_env["PYTHONPATH"] = str(rel_root)
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

    # Send child stdout/stderr to a private file so >64 KiB output can never deadlock a pipe.
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
            att_errors = _verify_trial_attestation_stdlib(
                attestation,
                expected_release_id=expected_release_id,
                expected_commit=expected_commit,
                expected_package_root=rel_root,
                expected_pack_dir=effective_pack,
                expected_pool_path=effective_pool,
                expected_generation_id=expected_generation_id,
                expected_data_dir=gen_dir,
            )
            if att_errors:
                ready = False
                status_code = 503
                errors.extend(att_errors)

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


def _assert_module_under(mod, expected_root: Path, label: str) -> None:
    mod_file = getattr(mod, "__file__", None)
    if not mod_file:
        raise RuntimeError(f"imported {label} has no backing file")
    resolved = Path(mod_file).resolve()
    if not resolved.is_relative_to(expected_root):
        raise RuntimeError(f"{label} resolved outside expected {expected_root}: {resolved}")


def _bootstrap_sealed_runtime(release_root: Path, runtime_dir: Path) -> None:
    """Configure isolated sys.path under -I -S and verify imports resolve strictly inside pair."""
    runtime_sp = (runtime_dir / "site-packages").resolve()
    if not runtime_sp.is_dir():
        raise RuntimeError(f"sealed runtime site-packages missing: {runtime_sp}")
    sys.path[:0] = [str(release_root), str(runtime_sp)]
    import fastapi  # noqa: PLC0415
    import pydantic_core  # noqa: PLC0415
    import uvicorn  # noqa: PLC0415

    import sigint101.app as sigint101_app  # noqa: PLC0415

    _assert_module_under(uvicorn, runtime_sp, "uvicorn")
    _assert_module_under(fastapi, runtime_sp, "fastapi")
    _assert_module_under(pydantic_core, runtime_sp, "pydantic_core")
    _assert_module_under(sigint101_app, release_root, "sigint101.app")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run private read-only SIGINT 101 trial server.")
    parser.add_argument("--socket", required=True, help="Unix domain socket path to bind.")
    parser.add_argument("--config", required=True, help="Private JSON config file path.")
    parser.add_argument("--release-root", default=None, help="Candidate release root directory.")
    parser.add_argument("--runtime-dir", default=None, help="Enrolled sealed runtime directory.")
    args = parser.parse_args(argv)

    release_root: Path | None = None
    if args.release_root is not None and args.runtime_dir is not None:
        release_root = Path(args.release_root).resolve()
        _bootstrap_sealed_runtime(release_root, Path(args.runtime_dir).resolve())

    import uvicorn  # noqa: PLC0415

    from sigint101.trial import create_trial_app as _create_trial_app  # noqa: PLC0415

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    req_ids = cfg.get("required_lesson_ids")
    app = _create_trial_app(
        pack_dir=cfg.get("pack_dir"),
        data_dir=cfg.get("data_dir"),
        pool_path=cfg.get("pool_path"),
        release_root=release_root,
        required_lesson_ids=frozenset(req_ids) if req_ids is not None else None,
        require_release_manifest=bool(cfg.get("require_release_manifest", True)),
        settings_fingerprint=cfg.get("settings_fingerprint"),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            uds=args.socket,
            lifespan="on",
            log_level="warning",
            access_log=False,
        )
    )
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
