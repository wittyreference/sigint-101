# ABOUTME: Local terminal CLI for the manual course updater (--check, --status, --apply, --recover,
# ABOUTME: --enroll, provision). Enforces PR 4's non-bench gate before any mutation or lock touch.

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .bench_gate import BenchGateError, require_bench_marker
from .storage import DEFAULT_UPDATER_ROOT_REL
from .updater import check_status


def _default_root() -> Path:
    return Path.home() / DEFAULT_UPDATER_ROOT_REL


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
    parser.add_argument("--with-optional", action="store_true", help="Host-only optional packages.")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)

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
        return int(exc.code) if isinstance(exc.code, int) else 2

    root = Path(args.root).resolve() if args.root else _default_root().resolve()

    # Rollback ships in PR 5 — refuse cleanly before touching anything.
    if args.rollback is not None:
        print(
            "sigint101-updater: --rollback is not enabled until PR 5; no changes made",
            file=sys.stderr,
        )
        return 11

    # Read-only preview / status: creates zero locks, dirs, or logs; needs no bench gate.
    if args.check or args.status:
        info = check_status(root=root, remote_url=args.remote if args.check else None, ref=args.ref)
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
        parser.print_help(sys.stderr)
        return 2

    try:
        require_bench_marker(root)
    except BenchGateError as exc:
        print(f"sigint101-updater: {exc}", file=sys.stderr)
        return 11

    if args.abort is not None and not args.recover:
        print("sigint101-updater: --abort requires --recover", file=sys.stderr)
        return 2

    print(
        f"sigint101-updater: bench marker verified for {root}; "
        "interactive host apply/enrollment driver requires explicit remote/commit or bench fixture",
        file=sys.stderr,
    )
    return 11


if __name__ == "__main__":
    raise SystemExit(main())
