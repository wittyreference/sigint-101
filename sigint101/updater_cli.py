# ABOUTME: Local terminal CLI for the manual course updater (--check, --status, --apply, --recover,
# ABOUTME: --enroll, provision). Enforces PR 4's non-bench gate before any mutation or lock touch.

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .bench_gate import BenchGateError, require_bench_marker
from .enrollment import EnrollmentError, enroll_legacy_fixture
from .locks import EXIT_GATE_BUSY, MUTATION_GATE_FD_ENV, LockBusyError, LockError
from .process_checks import (
    KioskPageOpenError,
    ServiceDrainError,
    SystemdUserController,
    start_unit_with_reset,
    wait_service_drained,
)
from .release_prep import PolicyRefusalError, ReleasePrepError, SpaceBudgetError
from .runtime import RuntimeCompatibilityError
from .storage import DEFAULT_UPDATER_ROOT_REL
from .unit_settings import UnitSettingsError
from .updater import (
    AbortError,
    RecoveryError,
    UpdaterError,
    abort_unserved,
    apply_update,
    check_status,
    recover,
)


def _default_root(home: Path | None = None) -> Path:
    base = home if home is not None else Path.home()
    return base / DEFAULT_UPDATER_ROOT_REL


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigint101-updater",
        description="Manual SIGINT 101 course updater (PR 4: mutations gated to disposable bench).",
    )
    parser.add_argument("--root", default=None, help="Updater ROOT directory.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Read-only preview against remote tip.")
    group.add_argument("--status", action="store_true", help="Read-only active selection summary.")
    group.add_argument("--enroll", action="store_true", help="Enroll a legacy box.")
    group.add_argument("--apply", action="store_true", help="Apply a pinned course release.")
    group.add_argument("--recover", action="store_true", help="Recover the selected pair.")
    group.add_argument("--rollback", default=None, metavar="TARGET", help="Rollback (PR 5).")
    parser.add_argument("--abort", default=None, metavar="OP_ID", help="Abort an unserved apply.")
    parser.add_argument("--remote", default=None, help="Trusted Git remote URL.")
    parser.add_argument("--commit", default=None, help="Pinned full Git commit SHA.")
    parser.add_argument("--ref", default="refs/heads/main", help="Git ref to fetch.")
    parser.add_argument("--unit", default="sigint101.service", help="Systemd user unit name.")
    parser.add_argument("--checkout", default=None, help="Legacy checkout path (for --enroll).")
    parser.add_argument("--data-dir", default=None, help="Legacy data directory (for --enroll).")
    parser.add_argument("--host-python", default=None, help="Host Python interpreter.")
    parser.add_argument("--legacy-python", default=None, help="Legacy venv Python (for --enroll).")
    parser.add_argument(
        "--site-packages", default=None, help="Source site-packages dir (for --enroll)."
    )
    parser.add_argument("--unit-path", default=None, help="User systemd unit path (for --enroll).")
    parser.add_argument(
        "--desktop-wrapper", default=None, help="Desktop launcher path (for --enroll)."
    )
    parser.add_argument("--runtime-dir", default=None, help="XDG runtime dir (for --enroll).")
    parser.add_argument("--with-optional", action="store_true", help="Host-only optional packages.")
    return parser


def _pop_inherited_gate_fd() -> int | None:
    raw = os.environ.pop(MUTATION_GATE_FD_ENV, None)
    if raw is None:
        return None
    try:
        fd = int(raw)
        os.set_inheritable(fd, False)
        return fd
    except (ValueError, OSError):
        return None


def _close_fd_quiet(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _selector_host_port(root: Path) -> tuple[str, int]:
    sel_path = root / "current.json"
    if sel_path.is_file() and not sel_path.is_symlink():
        try:
            sel = json.loads(sel_path.read_text(encoding="utf-8"))
            if isinstance(sel, dict):
                return str(sel.get("host", "127.0.0.1")), int(sel.get("port", 8008))
        except (OSError, ValueError):
            pass
    return "127.0.0.1", 8008


def _discover_site_packages(python_exe: Path) -> Path:
    res = subprocess.run(
        [
            str(python_exe),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        capture_output=True,
        text=True,
        close_fds=True,
        timeout=10,
    )
    if res.returncode != 0 or not res.stdout.strip():
        raise EnrollmentError(
            f"cannot discover site-packages from {python_exe}: {res.stderr.strip()}"
        )
    return Path(res.stdout.strip()).resolve()


def _format_receipt(info: dict) -> str:
    parts = [
        f"action={info.get('action', 'unknown')}",
        f"status={info.get('status', 'unknown')!r}",
    ]
    commit = info.get("short_commit") or (str(info["commit"])[:7] if info.get("commit") else None)
    if commit:
        parts.append(f"commit={commit}")
    if info.get("operation_id"):
        parts.append(f"op={info['operation_id']}")
    if info.get("mode"):
        parts.append(f"mode={info['mode']}")
    if info.get("recovery_command"):
        parts.append(f"next={info['recovery_command']!r}")
    return " ".join(parts)


def main(
    argv: list[str] | None = None,
    *,
    _bench_marker_checker: Callable[[Path], None] = require_bench_marker,
    _service_factory: Callable[[str, int, str], object] | None = None,
    _home: str | Path | None = None,
    _kiosk_process_lister=None,
    _unit_settings_loader=None,
) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    inherited_fd = _pop_inherited_gate_fd()

    # Accept `provision` subcommand handed off from provision-shim.sh -> dispatcher.py
    # (whether before or after `--root <dir>`).
    is_provision_handoff = False
    cleaned_argv: list[str] = []
    i = 0
    while i < len(raw_argv):
        tok = raw_argv[i]
        if tok == "--root" and i + 1 < len(raw_argv):
            cleaned_argv.extend([tok, raw_argv[i + 1]])
            i += 2
            continue
        if tok == "provision" and not is_provision_handoff:
            is_provision_handoff = True
            i += 1
            continue
        cleaned_argv.append(tok)
        i += 1
    raw_argv = cleaned_argv

    parser = _build_parser()
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        _close_fd_quiet(inherited_fd)
        return int(exc.code) if isinstance(exc.code, int) else 2

    home_path = Path(_home).resolve() if _home is not None else Path.home()
    root = Path(args.root).resolve() if args.root else _default_root(home_path).resolve()

    # Rollback ships in PR 5 — refuse cleanly before touching anything.
    if args.rollback is not None:
        _close_fd_quiet(inherited_fd)
        print(
            "sigint101-updater: --rollback is not enabled until PR 5; no changes made",
            file=sys.stderr,
        )
        return 11

    # Read-only preview / status: creates zero locks, dirs, or logs; needs no bench gate.
    if args.check or args.status:
        # Close any gate FD passed from provision-shim.sh --check so check_status doesn't contend
        # against its own inherited FD.
        _close_fd_quiet(inherited_fd)
        info = check_status(
            root=root,
            remote_url=args.remote if args.check else None,
            ref=args.ref,
            home=home_path,
        )
        print(
            f"action={'check' if args.check else 'status'} "
            f"status={info['status']!r} "
            f"commit={str(info.get('commit') or 'none')[:7]} "
            f"op={info.get('operation_id') or 'none'}"
        )
        return int(info["exit_code"])

    # Every mutating command (enroll, apply, recover, abort, provision hand-off) MUST pass the
    # root-owned bench marker check BEFORE creating directories or locks, editing files, invoking
    # systemd, or running candidate code (PR 4 §9 item 4).
    mutating = bool(
        is_provision_handoff or args.enroll or args.apply or args.recover or args.abort is not None
    )
    if not mutating:
        _close_fd_quiet(inherited_fd)
        parser.print_help(sys.stderr)
        return 2

    try:
        _bench_marker_checker(root)
    except BenchGateError as exc:
        _close_fd_quiet(inherited_fd)
        print(f"sigint101-updater: {exc}", file=sys.stderr)
        return 11

    if args.abort is not None and not args.recover:
        _close_fd_quiet(inherited_fd)
        print("sigint101-updater: --abort requires --recover", file=sys.stderr)
        return 2

    host, port = _selector_host_port(root)
    service = (
        _service_factory(host, port, args.unit)
        if _service_factory is not None
        else SystemdUserController(args.unit, host=host, port=port)
    )

    fd_to_pass = inherited_fd
    fd_handed_off = False
    try:
        if args.enroll:
            checkout = (
                Path(args.checkout).resolve()
                if args.checkout
                else (home_path / "sigint-101").resolve()
            )
            data_dir = (
                Path(args.data_dir).resolve() if args.data_dir else (checkout / "data").resolve()
            )
            default_host_py = (
                "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
            )
            host_py = Path(args.host_python) if args.host_python else Path(default_host_py)
            legacy_py = (
                Path(args.legacy_python)
                if args.legacy_python
                else (checkout / ".venv" / "bin" / "python")
            )
            site_packages = (
                Path(args.site_packages).resolve()
                if args.site_packages
                else _discover_site_packages(legacy_py)
            )
            unit_path = (
                Path(args.unit_path)
                if args.unit_path
                else (home_path / ".config" / "systemd" / "user" / args.unit)
            )
            desktop_wrapper = (
                Path(args.desktop_wrapper)
                if args.desktop_wrapper
                else Path("/usr/local/bin/sigint101-course")
            )
            xdg_rt = (
                Path(args.runtime_dir)
                if args.runtime_dir
                else Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
            )

            def _reload() -> None:
                if hasattr(service, "daemon_reload"):
                    service.daemon_reload()

            def _drain() -> None:
                wait_service_drained(
                    stop_unit=service.stop,
                    get_unit_state=service.unit_state,
                    get_cgroup_pids=service.get_cgroup_pids,
                    is_listener_active=service.is_listener_active,
                    kill_cgroup=service.kill_cgroup,
                )

            fd_handed_off = True
            info = enroll_legacy_fixture(
                repo_root=checkout,
                updater_root=root,
                legacy_checkout=checkout,
                legacy_data_dir=data_dir,
                legacy_python=legacy_py,
                source_site_packages=site_packages,
                unit_path=unit_path,
                desktop_wrapper_path=desktop_wrapper,
                home=home_path,
                runtime_dir=xdg_rt,
                host_python=host_py,
                trusted_remote=args.remote,
                legacy_commit=args.commit,
                kiosk_process_lister=_kiosk_process_lister,
                reload_systemd=_reload,
                drain_old_service=_drain,
                inherited_gate_fd=fd_to_pass,
                bench_marker_checker=_bench_marker_checker,
            )
            start_unit_with_reset(reset_failed=service.reset_failed, start_unit=service.start)
            print(_format_receipt({"action": "enroll", "status": "ready", **info}))
            return 0

        if args.apply or (is_provision_handoff and not args.recover):
            enr_path = root / "enrollment.json"
            enr = json.loads(enr_path.read_text(encoding="utf-8")) if enr_path.is_file() else {}
            effective_remote = args.remote or enr.get("trusted_remote")
            if not effective_remote:
                print(
                    "sigint101-updater: --remote is required (missing trusted_remote)",
                    file=sys.stderr,
                )
                return 11
            if not args.commit:
                print(
                    "sigint101-updater: --commit <full-sha> is required for --apply",
                    file=sys.stderr,
                )
                return 2
            fd_handed_off = True
            info = apply_update(
                root=root,
                remote_url=str(effective_remote),
                pinned_commit=args.commit,
                service=service,
                ref=args.ref,
                unit_name=args.unit,
                unit_settings_loader=_unit_settings_loader,
                home=home_path,
                kiosk_process_lister=_kiosk_process_lister,
                inherited_gate_fd=fd_to_pass,
                bench_marker_checker=_bench_marker_checker,
            )
            print(_format_receipt(info))
            return 0 if info.get("status") in ("ready", "noop") else 1

        if args.recover and args.abort is not None:
            fd_handed_off = True
            info = abort_unserved(
                root=root,
                operation_id=args.abort,
                service=service,
                home=home_path,
                kiosk_process_lister=_kiosk_process_lister,
                inherited_gate_fd=fd_to_pass,
                bench_marker_checker=_bench_marker_checker,
            )
            print(_format_receipt(info))
            return 0 if info.get("status") == "ready" else 1

        if args.recover:
            fd_handed_off = True
            info = recover(
                root=root,
                service=service,
                home=home_path,
                inherited_gate_fd=fd_to_pass,
                bench_marker_checker=_bench_marker_checker,
            )
            print(_format_receipt(info))
            return 0 if info.get("status") == "ready" else 1

        parser.print_help(sys.stderr)
        return 2
    except LockBusyError as exc:
        print(f"sigint101-updater: busy — {exc}", file=sys.stderr)
        return EXIT_GATE_BUSY
    except (
        BenchGateError,
        PolicyRefusalError,
        SpaceBudgetError,
        KioskPageOpenError,
        RuntimeCompatibilityError,
        UnitSettingsError,
        AbortError,
        RecoveryError,
        EnrollmentError,
    ) as exc:
        print(f"sigint101-updater: {exc}", file=sys.stderr)
        return 11
    except (UpdaterError, ReleasePrepError, ServiceDrainError, LockError, OSError) as exc:
        print(f"sigint101-updater: {exc}", file=sys.stderr)
        return 1
    finally:
        if not fd_handed_off:
            _close_fd_quiet(fd_to_pass)


if __name__ == "__main__":
    raise SystemExit(main())
