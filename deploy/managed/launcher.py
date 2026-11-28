#!/usr/bin/env python3
# ABOUTME: Managed control launcher invoked by the stable dispatcher with the shared gate held.
# ABOUTME: Acquires writer lock, fences selector, checks ABI/R2-1 parity, and execs service_entry.

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sigint101.locks import (
    EXIT_GATE_BUSY,
    SHARED_GATE_FD_ENV,
    WRITER_LOCK_FD_ENV,
    LauncherLockSession,
    LockBusyError,
    LockError,
)
from sigint101.runtime import (
    RuntimeCompatibilityError,
    probe_python_abi,
    verify_runtime_compatibility,
)
from sigint101.storage import fsync_dir, fsync_file

SELECTOR_FORMAT_VERSION = 1


def _load_and_fence_selector(root: Path) -> dict:
    selector_path = root / "current.json"
    if not selector_path.is_file() or selector_path.is_symlink():
        raise RuntimeError(
            "current.json is missing or symlinked; run recovery before starting service"
        )
    # Complete §5 selector durability fence under both locks before serving any request.
    fsync_file(selector_path)
    fsync_dir(root)
    try:
        data = json.loads(selector_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"current.json cannot be parsed: {exc}") from exc
    if not isinstance(data, dict) or data.get("format_version") != SELECTOR_FORMAT_VERSION:
        fv = data.get("format_version") if isinstance(data, dict) else None
        raise RuntimeError(f"unsupported current.json format_version: {fv!r}")
    mode = data.get("mode")
    if mode not in ("legacy", "managed"):
        raise RuntimeError(f"unsupported current.json mode: {mode!r}")
    op_id = data.get("operation_id")
    if not isinstance(op_id, str) or not op_id or "/" in op_id:
        raise RuntimeError(f"invalid operation_id in current.json: {op_id!r}")
    return data


def _validate_dir_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or value in (".", ".."):
        raise RuntimeError(f"invalid {label} in current.json: {value!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Managed SIGINT 101 service launcher.")
    parser.add_argument("--root", required=True, help="Updater ROOT directory.")
    parser.add_argument(
        "--control-dir", required=True, help="Selected immutable control directory."
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    control_dir = Path(args.control_dir).resolve()
    service_entry = control_dir / "deploy" / "managed" / "service_entry.py"
    if not service_entry.is_file() or service_entry.is_symlink():
        print(f"sigint101-launcher: missing service_entry.py in {control_dir}", file=sys.stderr)
        return 1

    raw_shared = os.environ.pop(SHARED_GATE_FD_ENV, None)
    inherited_shared_fd = int(raw_shared) if raw_shared is not None else None

    try:
        session = LauncherLockSession.acquire(root, inherited_shared_fd=inherited_shared_fd)
    except LockBusyError as exc:
        print(f"sigint101-launcher: busy — {exc}; retry shortly", file=sys.stderr)
        return EXIT_GATE_BUSY
    except (LockError, ValueError, OSError) as exc:
        print(f"sigint101-launcher: lock error: {exc}", file=sys.stderr)
        return 1

    try:
        selector = _load_and_fence_selector(root)
        mode = selector["mode"]
        op_id = selector["operation_id"]
        host = str(selector.get("host", "127.0.0.1"))
        port = int(selector.get("port", 8008))
        python_exe = str(selector.get("python_executable") or sys.executable)

        exec_argv: list[str]
        if mode == "legacy":
            checkout_dir = Path(selector["checkout_dir"]).resolve()
            data_dir = Path(selector["data_dir"]).resolve()
            if not checkout_dir.is_dir():
                raise RuntimeError(f"legacy checkout missing: {checkout_dir}")
            exec_argv = [
                python_exe,
                "-B",
                str(service_entry),
                "--mode",
                "legacy",
                "--checkout-dir",
                str(checkout_dir),
                "--data-dir",
                str(data_dir),
                "--operation-id",
                op_id,
                "--host",
                host,
                "--port",
                str(port),
            ]
        else:
            release_id = _validate_dir_id(selector.get("release_id"), "release_id")
            generation_id = _validate_dir_id(selector.get("generation_id"), "generation_id")
            runtime_id = _validate_dir_id(selector.get("runtime_id"), "runtime_id")

            release_dir = root / "releases" / release_id
            data_dir = root / "data" / generation_id
            runtime_dir = root / "runtimes" / runtime_id
            ops_dir = root / "operations" / op_id

            for label, target in (
                ("release", release_dir),
                ("generation", data_dir),
                ("runtime", runtime_dir),
            ):
                if not target.is_dir() or target.is_symlink():
                    raise RuntimeError(f"selected {label} directory missing or symlinked: {target}")

            # Verify sealed runtime ABI compatibility (accepts same-ABI host point updates).
            current_abi = probe_python_abi(python_exe)
            verify_runtime_compatibility(runtime_dir, current_abi=current_abi, verify_files=True)

            exec_argv = [
                python_exe,
                "-B",
                str(service_entry),
                "--mode",
                "managed",
                "--release-root",
                str(release_dir),
                "--data-dir",
                str(data_dir),
                "--runtime-dir",
                str(runtime_dir),
                "--operation-id",
                op_id,
                "--generation-id",
                generation_id,
                "--release-id",
                release_id,
                "--operations-dir",
                str(ops_dir),
                "--host",
                host,
                "--port",
                str(port),
            ]

        # Drop shared gate BEFORE serving; transfer ONLY the writer lock FD across service exec.
        session.drop_shared_gate()
        writer_fd = session.prepare_writer_fd_for_exec()
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env[WRITER_LOCK_FD_ENV] = str(writer_fd)
        os.execve(python_exe, exec_argv, env)
    except (RuntimeError, RuntimeCompatibilityError, KeyError, ValueError, OSError) as exc:
        session.close()
        print(f"sigint101-launcher: startup refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
