# ABOUTME: Pure-stdlib systemd unit/drop-in settings parser, loader, and R2-1 parity check.
# ABOUTME: Imported by both managed control launcher/updater (under -I -S) and validation.py.

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_UNIT_DIRECTIVES: frozenset[str] = frozenset(
    {
        "Environment",
        "ExecStart",
        "WorkingDirectory",
        "Restart",
        "RestartSec",
        "TimeoutStartSec",
        "TimeoutStopSec",
        "KillMode",
        "KillSignal",
        "SyslogIdentifier",
        "StandardOutput",
        "StandardError",
    }
)

# Unit or drop-in settings must never redirect release paths or inject linker/python hooks.
FORBIDDEN_UNIT_ENV_VARS: frozenset[str] = frozenset(
    {
        "SIGINT101_PACK_DIR",
        "SIGINT101_DATA_DIR",
        "SIGINT101_POOL_PATH",
    }
)
FORBIDDEN_UNIT_ENV_PREFIXES: tuple[str, ...] = ("PYTHON", "LD_")

# Drop-ins may only set Environment= under [Service]; replacing ExecStart, WorkingDirectory, etc.
# changes execution semantics outside what the trial models.
ALLOWED_DROPIN_DIRECTIVES: frozenset[str] = frozenset({"Environment"})

_SYSTEMCTL_ENV_KEYS = ("PATH", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG")


class UnitSettingsError(ValueError):
    """Raised when systemd unit/drop-in settings are malformed, unsupported, or drifted."""


def _validate_unit_env_name(name: str) -> None:
    if not name:
        raise UnitSettingsError("empty environment variable name in Environment=")
    if name in FORBIDDEN_UNIT_ENV_VARS or name.startswith(FORBIDDEN_UNIT_ENV_PREFIXES):
        raise UnitSettingsError(
            f"forbidden environment variable in unit/drop-in settings: {name}"
        )


def is_forbidden_trial_env_var(name: str) -> bool:
    """True for env vars that must never enter a trial child from unit/manager environment."""
    return name in FORBIDDEN_UNIT_ENV_VARS or name.startswith(FORBIDDEN_UNIT_ENV_PREFIXES)


@dataclass(frozen=True)
class UnitSettings:
    """Structured effective systemd [Service] settings without shell evaluation or secret logs."""

    exec_start: str | None = None
    working_directory: str | None = None
    manager_env: dict[str, str] = field(default_factory=dict)
    unit_env: dict[str, str] = field(default_factory=dict)
    dropin_env: dict[str, str] = field(default_factory=dict)
    other_directives: dict[str, str] = field(default_factory=dict)

    @property
    def env(self) -> dict[str, str]:
        merged = dict(self.manager_env)
        merged.update(self.unit_env)
        merged.update(self.dropin_env)
        return merged

    def unit_only(self) -> UnitSettings:
        """Return only the unit file's own Environment= values for R2-1 parity."""
        return UnitSettings(unit_env=dict(self.unit_env))

    def __repr__(self) -> str:
        fp = compute_settings_fingerprint(self)[:12]
        return (
            f"UnitSettings(manager_env_keys={sorted(self.manager_env)!r}, "
            f"unit_env_keys={sorted(self.unit_env)!r}, "
            f"dropin_env_keys={sorted(self.dropin_env)!r}, "
            f"directives={sorted(self.other_directives)!r}, fingerprint={fp!r})"
        )


def _tokenize_systemd_env_value(raw: str, *, allow_escapes: bool = False) -> list[str]:
    """Split a systemd Environment= RHS into assignments without any shell evaluation."""
    tokens: list[str] = []
    buf: list[str] = []
    in_quote: str | None = None
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_quote is not None:
            if ch == "\\" and in_quote == '"' and i + 1 < n:
                if not allow_escapes:
                    raise UnitSettingsError(
                        "unescaped backslash escape in Environment=; read effective settings "
                        "from systemd rather than guessing"
                    )
                buf.append(raw[i + 1])
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch in ('"', "'"):
            in_quote = ch
            i += 1
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            i += 1
            continue
        if ch == "\\" and not allow_escapes:
            raise UnitSettingsError(
                "backslash escape in Environment=; read effective settings from systemd"
            )
        buf.append(ch)
        i += 1
    if in_quote is not None:
        raise UnitSettingsError("unterminated quote in systemd Environment= directive")
    if buf:
        tokens.append("".join(buf))
    return tokens


def _apply_unit_text(
    text: str,
    *,
    is_dropin: bool,
    unit_env: dict[str, str],
    dropin_env: dict[str, str],
    state: dict[str, object],
    allow_unexpanded: bool = False,
) -> None:
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "Service":
            continue
        if "=" not in line:
            raise UnitSettingsError(f"malformed [Service] line: {line!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if is_dropin:
            if key not in ALLOWED_DROPIN_DIRECTIVES:
                raise UnitSettingsError(f"unsupported drop-in directive: {key}")
        elif key not in SUPPORTED_UNIT_DIRECTIVES:
            raise UnitSettingsError(f"unsupported systemd execution setting: {key}")
        if key == "Environment":
            target = dropin_env if is_dropin else unit_env
            if not value:
                target.clear()
                if is_dropin:
                    unit_env.clear()
                continue
            for item in _tokenize_systemd_env_value(value, allow_escapes=allow_unexpanded):
                if "=" not in item:
                    raise UnitSettingsError("malformed Environment= assignment without '='")
                env_k, env_v = item.split("=", 1)
                _validate_unit_env_name(env_k)
                if not allow_unexpanded and ("%" in env_v or "\\" in env_v):
                    raise UnitSettingsError(
                        f"unexpanded systemd specifier or escape in {env_k}; "
                        "obtain effective settings via systemctl --user show"
                    )
                target[env_k] = env_v
        elif key == "ExecStart":
            state["exec_start"] = value or None
        elif key == "WorkingDirectory":
            state["working_directory"] = value or None
        else:
            other = state["other_directives"]
            assert isinstance(other, dict)
            other[key] = value


def parse_systemd_unit_config(
    unit_text: str,
    dropin_texts: Sequence[str] = (),
) -> UnitSettings:
    """Parse structured [Service] unit/drop-in text, refusing drop-in overrides and guesses."""
    unit_env: dict[str, str] = {}
    dropin_env: dict[str, str] = {}
    state: dict[str, object] = {
        "exec_start": None,
        "working_directory": None,
        "other_directives": {},
    }
    _apply_unit_text(
        unit_text, is_dropin=False, unit_env=unit_env, dropin_env=dropin_env, state=state
    )
    for dropin in dropin_texts:
        _apply_unit_text(
            dropin, is_dropin=True, unit_env=unit_env, dropin_env=dropin_env, state=state
        )
    return UnitSettings(
        exec_start=state["exec_start"],  # type: ignore[arg-type]
        working_directory=state["working_directory"],  # type: ignore[arg-type]
        unit_env=unit_env,
        dropin_env=dropin_env,
        other_directives=dict(state["other_directives"]),  # type: ignore[arg-type]
    )


def _minimal_systemctl_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _SYSTEMCTL_ENV_KEYS:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    return env


def _default_systemctl_runner(args: list[str]) -> str:
    proc = subprocess.run(
        args,
        env=_minimal_systemctl_env(),
        capture_output=True,
        text=True,
        close_fds=True,
        timeout=10,
    )
    if proc.returncode != 0:
        raise UnitSettingsError(f"{' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _inspect_dropin_directives(dropin_path: Path) -> set[str]:
    """Validate a drop-in sets ONLY Environment= under [Service] and return its env keys."""
    try:
        text = dropin_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UnitSettingsError(f"cannot read drop-in {dropin_path.name}: {exc}") from exc
    section = ""
    keys: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "Service":
            continue
        if "=" not in line:
            raise UnitSettingsError(f"malformed [Service] line in drop-in {dropin_path.name}")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in ALLOWED_DROPIN_DIRECTIVES:
            raise UnitSettingsError(
                f"unsupported drop-in directive {key!r} in {dropin_path.name}"
            )
        val = value.strip()
        if val:
            for item in _tokenize_systemd_env_value(val, allow_escapes=True):
                if "=" in item:
                    env_k = item.split("=", 1)[0]
                    _validate_unit_env_name(env_k)
                    keys.add(env_k)
    return keys


def _inspect_unit_env_keys(unit_path: Path) -> set[str]:
    """Extract the Environment= variable names declared in the base unit file."""
    try:
        text = unit_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UnitSettingsError(f"cannot read unit file {unit_path.name}: {exc}") from exc
    section = ""
    keys: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "Service" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "Environment":
            continue
        val = value.strip()
        if not val:
            keys.clear()
            continue
        for item in _tokenize_systemd_env_value(val, allow_escapes=True):
            if "=" in item:
                env_k = item.split("=", 1)[0]
                _validate_unit_env_name(env_k)
                keys.add(env_k)
    return keys


def load_systemd_unit_settings(
    unit: str = "sigint101.service",
    *,
    runner: Callable[[list[str]], str] | None = None,
) -> UnitSettings:
    """Obtain effective unit + manager settings directly from systemd.

    Reads `systemctl --user show-environment` and `systemctl --user show <unit>` so systemd's
    own specifier expansion (%h), C-escape handling, and line continuations are authoritative.
    Refuses drop-ins that set directives other than Environment=, unsupported execution settings,
    and PYTHON*/LD_*/release-path env overrides.
    """
    run = runner or _default_systemctl_runner

    # 1) Inherited user-manager session environment (excluding forbidden trial variables).
    mgr_out = run(["systemctl", "--user", "show-environment"])
    manager_env: dict[str, str] = {}
    for line in mgr_out.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k and not is_forbidden_trial_env_var(k):
            manager_env[k] = v

    # 2) Effective unit properties from systemd.
    show_args = [
        "systemctl",
        "--user",
        "show",
        unit,
        "-p",
        "FragmentPath",
        "-p",
        "DropInPaths",
        "-p",
        "Environment",
        "-p",
        "EnvironmentFiles",
        "-p",
        "UnsetEnvironment",
        "-p",
        "ExecStart",
        "-p",
        "WorkingDirectory",
        "-p",
        "DynamicUser",
        "-p",
        "PrivateTmp",
        "-p",
        "RootDirectory",
        "-p",
        "ProtectSystem",
        "-p",
        "User",
        "-p",
        "Group",
    ]
    show_out = run(show_args)
    props: dict[str, str] = {}
    for line in show_out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()

    # Refuse unsupported effective execution settings.
    if props.get("EnvironmentFiles"):
        raise UnitSettingsError("unsupported systemd execution setting: EnvironmentFiles")
    if props.get("UnsetEnvironment"):
        raise UnitSettingsError("unsupported systemd execution setting: UnsetEnvironment")
    for bool_prop in ("DynamicUser", "PrivateTmp"):
        if props.get(bool_prop, "").lower() == "yes":
            raise UnitSettingsError(f"unsupported systemd execution setting: {bool_prop}=yes")
    for str_prop in ("RootDirectory", "User", "Group"):
        if props.get(str_prop):
            raise UnitSettingsError(f"unsupported systemd execution setting: {str_prop}")
    protect_sys = props.get("ProtectSystem", "").lower()
    if protect_sys not in ("", "no"):
        raise UnitSettingsError(
            f"unsupported systemd execution setting: ProtectSystem={protect_sys}"
        )

    # Inspect every drop-in file to ensure drop-ins only set Environment=.
    dropin_keys: set[str] = set()
    raw_dropins = props.get("DropInPaths", "").strip()
    if raw_dropins:
        for dp in raw_dropins.split():
            dropin_keys.update(_inspect_dropin_directives(Path(dp)))

    unit_keys: set[str] = set()
    frag = props.get("FragmentPath", "").strip()
    if frag and Path(frag).is_file():
        unit_keys = _inspect_unit_env_keys(Path(frag))

    # Parse systemd's already-expanded effective Environment= property.
    unit_env: dict[str, str] = {}
    dropin_env: dict[str, str] = {}
    raw_env = props.get("Environment", "")
    if raw_env:
        for item in _tokenize_systemd_env_value(raw_env, allow_escapes=True):
            if "=" not in item:
                raise UnitSettingsError("malformed systemd Environment property entry")
            env_k, env_v = item.split("=", 1)
            _validate_unit_env_name(env_k)
            if env_k in dropin_keys:
                dropin_env[env_k] = env_v
            elif env_k in unit_keys or not unit_keys:
                unit_env[env_k] = env_v
            else:
                dropin_env[env_k] = env_v

    return UnitSettings(
        exec_start=props.get("ExecStart") or None,
        working_directory=props.get("WorkingDirectory") or None,
        manager_env=manager_env,
        unit_env=unit_env,
        dropin_env=dropin_env,
    )


def compute_settings_fingerprint(settings: UnitSettings) -> str:
    """Return a SHA-256 fingerprint of structured environment settings without exposing values."""
    payload = {
        "manager_env": sorted(settings.manager_env.items()),
        "unit_env": sorted(settings.unit_env.items()),
        "dropin_env": sorted(settings.dropin_env.items()),
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def check_unit_settings_parity(
    *,
    recorded_fingerprint: str,
    current_settings: UnitSettings,
    serving_marker_exists: bool,
) -> bool:
    """Enforce R2-1 parity: unit-only env before serving marker; never refuse served boots."""
    if serving_marker_exists:
        return True
    actual = compute_settings_fingerprint(current_settings.unit_only())
    if not hmac.compare_digest(actual, recorded_fingerprint):
        raise UnitSettingsError(
            "unit settings parity drift detected before serving marker was written"
        )
    return True
