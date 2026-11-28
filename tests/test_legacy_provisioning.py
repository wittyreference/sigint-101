# ABOUTME: Legacy-provisioning and checkout-boot guard (§8, U2, R2-2) run on every updater PR.
# ABOUTME: Executes real deploy/provision.sh, installed unit, and desktop launcher in a sysroot.

from __future__ import annotations

import hashlib
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")

CORE_UTILS = (
    "awk", "basename", "bash", "cat", "cut", "dirname", "grep", "head", "install", "ls",
    "mkdir", "paste", "readlink", "rm", "sed", "seq", "sha256sum", "sleep", "sort", "timeout",
)

# Frozen SHA-256 digests of every legacy-installed file from origin/main (updated only on a
# reviewed legacy deploy change). Comparing installed files against these digests prevents an
# interim updater PR from silently editing deploy/kiosk/start-kiosk.sh or any other legacy file.
LEGACY_INSTALLED_FILES: tuple[tuple[str, str, str, str], ...] = (
    (
        "deploy/kiosk/sigint101.service",
        "home",
        ".config/systemd/user/sigint101.service",
        # C47 (F3): the unit comment triggers the installer for a one-run AIS companion upgrade.
        "b894c4af12ae62ddccc28dfba8b6cfd905ad6c5b91093f77f3fb64b99a116013",
    ),
    (
        "deploy/kiosk/start-kiosk.sh",
        "sysroot",
        "usr/local/bin/sigint101-course",
        # Refrozen for C78-C80 (reviewed legacy deploy change): the watch loop runs with the
        # lock's fd 9 closed so no child inherits it, a watchdog that stands down says why, a
        # wlr-randr that ignores TERM is killed (timeout -k), and the loop forks no shell that
        # could keep the lock's backup (the output size is read without $(...); Codex D-111 F4).
        "446893522cf963e16522f943317db2405e602eb0177b4118e95546bfc0795485",
    ),
    (
        "deploy/kiosk/sigint101.svg",
        "sysroot",
        "usr/local/share/icons/hicolor/scalable/apps/sigint101.svg",
        "ded8278933d8af05be2998b58ba26d9d9e605b2dc00e10b36866ead9bbc57184",
    ),
    (
        "deploy/kiosk/sigint101.desktop",
        "sysroot",
        "usr/local/share/applications/sigint101.desktop",
        "6df0a8015b29097781b6e922ac77b0889f070f8d9a3f8d9f67daa4e3d4f89a6a",
    ),
    (
        "deploy/kiosk/sigint101-autostart.desktop",
        "home",
        ".config/autostart/sigint101.desktop",
        "9f5e62c19d24c4ea36c93f0f9b0420729faf72a630784497e0aa395c255cf644",
    ),
    (
        "deploy/kiosk/sigint101-link.desktop",
        "home",
        "Desktop/sigint101.desktop",
        "bc99770b95890f36bb90147f5f0ed46ab41b979cb5516402f0d6579c2bad297d",
    ),
    (
        "deploy/system/journald-sigint101.conf",
        "sysroot",
        "etc/systemd/journald.conf.d/50-sigint101.conf",
        "4b84424a5d9faeb46ad0b2335a962a1d8bac736f2a85d3872dcedfac9ed1ca02",
    ),
    # Safe shutdown slice 2a: package marker copied into /usr/local/lib/sigint101-power/.
    (
        "sigint101/__init__.py",
        "sysroot",
        "usr/local/lib/sigint101-power/sigint101/__init__.py",
        "74cd6c6befc26db56d888f8b0ff22eabc0a522fbbe9692e2e30b91d9a7d21b0c",
    ),
    # Safe shutdown slice 2a: read-only INA219 helper (later battery.py edits re-freeze this).
    (
        "sigint101/battery.py",
        "sysroot",
        "usr/local/lib/sigint101-power/sigint101/battery.py",
        "33e91635e86833a459f812bcadb51a90337734779c7d5e7d221fec0b08562451",
    ),
    # Safe shutdown slice 2a: low-battery watcher (ships hard-disarmed until slice 2b).
    (
        "sigint101/power.py",
        "sysroot",
        "usr/local/lib/sigint101-power/sigint101/power.py",
        "3435292c55df6122ac8d2c8e8786f57ee7631cfdde8c036abf3db95264d2cf18",
    ),
    # Safe shutdown slice 2a: hardened non-root systemd unit for the power watcher.
    (
        "deploy/system/sigint101-power.service",
        "sysroot",
        "etc/systemd/system/sigint101-power.service",
        "b63f4ea7d457355c1c082c2075c27b8b6f60e5ba6718f9b562607d240df71d2a",
    ),
    # Safe shutdown slice 2a: self-locating python3 -I launcher for the power watcher.
    (
        "deploy/system/sigint101-power",
        "sysroot",
        "usr/local/bin/sigint101-power",
        "6dfb10dcd1309d5a0c6771b750cc034c0bccd59e15fe94c04a8c2ae53e92b68e",
    ),
    # Safe shutdown slice 2a: polkit rule granting only logind power-off actions to sigint101-power.
    (
        "deploy/system/50-sigint101-power.rules",
        "sysroot",
        "etc/polkit-1/rules.d/50-sigint101-power.rules",
        "c6c78b2564172c020efee5abf4282ad757e76e027e6f87de7966b2aa225b2535",
    ),
    # Safe shutdown slice 2a: sysusers.d entry defining static system user sigint101-power.
    (
        "deploy/system/sigint101-power.sysusers.conf",
        "sysroot",
        "usr/local/lib/sysusers.d/sigint101-power.conf",
        "3b0456bd4ffe286d045dbb9d0a0fc9d5eadf4727d5d53f82cecf5a6c5afec03e",
    ),
)


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!{BASH}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


class StatefulLegacyBox:
    """Disposable legacy kiosk fixture where sudo-installed files land in a real sysroot.

    Unlike a no-op sudo stub, R2-2 requires the legacy-install test to observe every file
    installed via sudo and execute both the installed systemd user unit and the installed
    desktop launcher (/usr/local/bin/sigint101-course open) through a stateful fixture.
    """

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.sysroot = tmp_path / "sysroot"
        self.runtime = tmp_path / "runtime"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "calls.log"
        self.launcher_log = tmp_path / "launcher.log"
        self.curl_state = tmp_path / "curl-ready"
        self.service_state = tmp_path / "service-state"
        self.home.mkdir(parents=True)
        self.sysroot.mkdir(parents=True)
        self.runtime.mkdir(parents=True)
        self.bin.mkdir(parents=True)
        self.log.touch()
        self.launcher_log.touch()

        for tool in CORE_UTILS:
            found = shutil.which(tool)
            assert found, f"host lacks {tool}"
            (self.bin / tool).symlink_to(found)

        # Copy the real repo deploy/ tree into the fixture's checkout at ~/sigint-101, and link
        # bundled fonts so deploy/desktop/apply-theme.sh runs end to end too.
        self.course = self.home / "sigint-101"
        (self.course / ".git").mkdir(parents=True)
        (self.course / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        shutil.copytree(REPO / "deploy", self.course / "deploy")
        (self.course / "sigint101" / "static").mkdir(parents=True)
        for py_name in ("__init__.py", "battery.py", "power.py"):
            shutil.copyfile(REPO / "sigint101" / py_name, self.course / "sigint101" / py_name)
        (self.course / "sigint101" / "static" / "fonts").symlink_to(
            REPO / "sigint101" / "static" / "fonts"
        )

        venv_bin = self.course / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        _stub(venv_bin / "python", f'echo "venv-python $*" >> "{self.log}"\nexit 0')
        _stub(
            venv_bin / "uvicorn",
            f'echo "checkout-uvicorn cwd=$PWD args=$*" >> "{self.log}"\nexit 0',
        )
        _stub(self.course / "bootstrap.sh", f'echo "bootstrap $*" >> "{self.log}"\nexit 0')

        # Redirect system paths (/usr/local/..., /etc/..., /var/lib/...) into self.sysroot so
        # cmp and sudo install/mkdir/loginctl operate on real files we can inspect (R2-2).
        real_cmp = shutil.which("cmp")
        real_install = shutil.which("install")
        assert real_cmp and real_install
        _stub(
            self.bin / "cmp",
            'args=()\n'
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            f'    /usr/local/*|/etc/*|/var/lib/*) args+=("{self.sysroot}$a") ;;\n'
            '    *) args+=("$a") ;;\n'
            '  esac\n'
            'done\n'
            f'exec "{real_cmp}" "${{args[@]}}"\n',
        )
        _stub(
            self.bin / "sudo",
            f'echo "sudo $*" >> "{self.log}"\n'
            'cmd="$1"; shift\n'
            'case "$cmd" in\n'
            '  install)\n'
            '    args=()\n'
            '    for a in "$@"; do\n'
            '      case "$a" in\n'
            f'        /usr/local/*|/etc/*|/var/lib/*) args+=("{self.sysroot}$a") ;;\n'
            '        *) args+=("$a") ;;\n'
            '      esac\n'
            '    done\n'
            f'    exec "{real_install}" "${{args[@]}}"\n'
            '    ;;\n'
            '  mkdir)\n'
            '    args=()\n'
            '    for a in "$@"; do\n'
            '      case "$a" in\n'
            f'        /var/log/*|/etc/*|/usr/local/*) args+=("{self.sysroot}$a") ;;\n'
            '        *) args+=("$a") ;;\n'
            '      esac\n'
            '    done\n'
            '    exec mkdir "${args[@]}"\n'
            '    ;;\n'
            '  loginctl)\n'
            '    if [ "${1:-}" = "enable-linger" ]; then\n'
            f'      mkdir -p "{self.sysroot}/var/lib/systemd/linger"\n'
            f'      touch "{self.sysroot}/var/lib/systemd/linger/${{2:-learner}}"\n'
            '    fi\n'
            '    exit 0\n'
            '    ;;\n'
            '  tee) exec cat >/dev/null ;;\n'
            '  *) exit 0 ;;\n'
            'esac\n',
        )
        _stub(
            self.bin / "systemctl",
            f'echo "systemctl $*" >> "{self.log}"\n'
            'case "$*" in\n'
            '  "get-default") echo graphical.target; exit 0 ;;\n'
            '  "--user enable --now sigint101.service")\n'
            f'    echo enabled > "{self.service_state}"; exit 0 ;;\n'
            '  "--user start sigint101.service")\n'
            f'    echo ready > "{self.curl_state}"; exit 0 ;;\n'
            '  "--user --quiet is-enabled sigint101.service")\n'
            f'    [ -f "{self.service_state}" ]; exit $? ;;\n'
            '  *) exit 0 ;;\n'
            'esac\n',
        )
        _stub(
            self.bin / "curl",
            f'echo "curl $*" >> "{self.log}"\n'
            f'[ -f "{self.curl_state}" ]\n',
        )
        _stub(
            self.bin / "chromium",
            f'echo "chromium $*" >> "{self.launcher_log}"\n'
            'exit 0\n',
        )
        _stub(self.bin / "id",
              'case "$1" in\n'
              '  -u) echo 1000 ;;\n'
              '  -un) echo learner ;;\n'
              '  -nG) echo "learner dialout plugdev" ;;\n'
              '  *) echo learner ;;\n'
              'esac')
        _stub(self.bin / "uname", "echo aarch64")
        _stub(self.bin / "raspi-config", "exit 0")
        _stub(self.bin / "dpkg-query", "echo ii")
        _stub(self.bin / "git",
              f'echo "git $*" >> "{self.log}"\n'
              'case "$*" in\n'
              '  *"remote get-url"*) echo "https://github.com/wittyreference/sigint-101" ;;\n'
              '  *"rev-parse --abbrev-ref"*) echo main ;;\n'
              '  *"rev-parse HEAD"*) echo 1234567890abcdef ;;\n'
              '  *"ls-remote"*) printf "1234567890abcdef\\trefs/heads/main\\n" ;;\n'
              '  *) ;;\n'
              'esac\n'
              'exit 0')
        for tool in ("dump1090-fa", "acarsdec", "redsea", "rtl_ais", "nmcli",
                     "gsettings", "fc-cache", "pkill", "vcgencmd"):
            _stub(self.bin / tool, "exit 0")
        _stub(self.bin / "rtl_ais-sigint101",
              '[ "$1" = --sigint101-capability ] && echo sigint101-ais-udp-v1')
        _stub(self.bin / "getcap", 'echo "/usr/bin/tcpdump cap_net_admin,cap_net_raw=eip"')
        _stub(self.bin / "fc-list", 'echo "PT Mono"; echo "Russo One"; echo "DejaVu Sans Mono"')
        _stub(self.bin / "lsusb", 'echo "Bus 001 Device 002: ID 0bda:2838 Realtek"')
        _stub(self.bin / "node", 'echo "v22.0.0"')
        _stub(self.bin / "pgrep", "exit 1")
        _stub(self.bin / "touch", 'exec /usr/bin/touch "$@"')
        _stub(
            self.bin / "stat",
            'if [ "$*" = "-c %G /dev/i2c-1" ]; then echo i2c; exit 0; fi\nexit 1',
        )
        _stub(
            self.bin / "getent",
            'if [ "$1" = "group" ] && [ "$2" = "i2c" ]; then echo "i2c:x:998:"; exit 0; fi\n'
            'exit 2',
        )

        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "USER": "learner",
            "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": str(self.runtime),
        }

    def run_provision(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, str(self.course / "deploy" / "provision.sh")],
            env=self.env, capture_output=True, text=True, timeout=30,
        )

    def verify_installed_files(self) -> None:
        """Verify every legacy-installed file matches its frozen origin/main digest and is clean."""
        for rel_src, base_kind, rel_dst, expected_sha in LEGACY_INSTALLED_FILES:
            base = self.home if base_kind == "home" else self.sysroot
            installed = base / rel_dst
            assert installed.is_file(), f"installed file missing: {rel_dst} (from {rel_src})"
            data = installed.read_bytes()
            text = data.decode("utf-8", errors="replace")
            assert "deploy/managed" not in text, (
                f"legacy-installed file {rel_dst} must never reference deploy/managed"
            )
            actual_sha = hashlib.sha256(data).hexdigest()
            assert actual_sha == expected_sha, (
                f"legacy-installed file {rel_dst} ({rel_src}) diverged from frozen origin/main "
                f"digest: got {actual_sha}, expected {expected_sha}"
            )

    def boot_installed_unit(self) -> tuple[Path, list[str], subprocess.CompletedProcess]:
        """Parse and execute the installed systemd user unit in a stateful service fixture."""
        unit_path = self.home / ".config" / "systemd" / "user" / "sigint101.service"
        assert unit_path.is_file(), f"missing installed unit: {unit_path}"
        fields: dict[str, str] = {}
        for line in unit_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith(("#", "[")):
                k, v = line.split("=", 1)
                fields[k.strip()] = v.strip().replace("%h", str(self.home))
        workdir = Path(fields["WorkingDirectory"])
        exec_argv = shlex.split(fields["ExecStart"])
        assert "deploy/managed" not in fields["ExecStart"], (
            f"legacy unit must never point to deploy/managed: {fields['ExecStart']!r}"
        )
        assert workdir == self.course, (
            f"legacy unit must boot checkout {self.course}, got {workdir}"
        )
        proc = subprocess.run(
            exec_argv, cwd=workdir, env=self.env, capture_output=True, text=True, timeout=15,
        )
        return workdir, exec_argv, proc

    def run_installed_launcher(self) -> tuple[subprocess.CompletedProcess, str]:
        """Execute the installed /usr/local/bin/sigint101-course open launcher in the fixture."""
        launcher = self.sysroot / "usr" / "local" / "bin" / "sigint101-course"
        assert launcher.is_file(), f"missing installed desktop launcher: {launcher}"
        if self.curl_state.exists():
            self.curl_state.unlink()
        self.launcher_log.write_text("", encoding="utf-8")
        # Plant the exit flag to confirm `open` clears it before launching the course.
        exit_flag = self.runtime / "sigint101-kiosk-exited"
        exit_flag.write_text("1", encoding="utf-8")
        proc = subprocess.run(
            [str(launcher), "open"],
            env=self.env, capture_output=True, text=True, timeout=15,
        )
        # launch() backgrounds chromium; wait briefly for our stub to write its argv line.
        deadline = time.monotonic() + 3.0
        launched = ""
        while time.monotonic() < deadline:
            launched = self.launcher_log.read_text(encoding="utf-8").strip()
            if launched:
                break
            time.sleep(0.05)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert not exit_flag.exists(), "sigint101-course open did not clear the exit flag"
        assert "chromium " in launched, f"installed launcher did not launch chromium: {launched!r}"
        assert "--app=http://127.0.0.1:8008" in launched, launched
        assert f"--user-data-dir={self.home}/.config/chromium-kiosk" in launched, launched
        assert "deploy/managed" not in launched, launched
        return proc, launched

    def run_installed_power_launcher(self) -> subprocess.CompletedProcess:
        """Run the installed /usr/local/bin/sigint101-power --startup-only under real python3 -I."""
        launcher = self.sysroot / "usr" / "local" / "bin" / "sigint101-power"
        assert launcher.is_file(), f"missing installed power launcher: {launcher}"
        # Run with minimal system PATH and no PYTHONPATH so real /usr/bin/python3 -I proves the
        # self-locating launcher resolves ../lib/sigint101-power cleanly inside the sysroot.
        env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "SIGINT101_POWER_THRESHOLD_V": "9.50",
        }
        proc = subprocess.run(
            [str(launcher), "--startup-only"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        combined = proc.stdout + proc.stderr
        assert "power.py sha256=" in combined, combined
        assert "battery.py sha256=" in combined, combined
        assert "arming waits on the notice slice (2b)" in combined, combined
        return proc


def test_the_frozen_launcher_digest_is_on_install_shs_reviewed_launcher_list():
    """install.sh's helper may SIGKILL a watchdog only on a launcher listed in
    deploy/reviewed-launchers.sha256; the digest frozen here must be that list's entry for the
    shipped launcher, so the two can never drift apart."""
    [frozen] = [sha for src, _, _, sha in LEGACY_INSTALLED_FILES
                if src == "deploy/kiosk/start-kiosk.sh"]
    reviewed = (REPO / "deploy" / "reviewed-launchers.sha256").read_text(encoding="utf-8")
    assert f"{frozen}  deploy/kiosk/start-kiosk.sh" in reviewed.splitlines()


def test_interim_pr_legacy_provisioning_still_boots_checkout(tmp_path):
    assert BASH, "bash is required"
    box = StatefulLegacyBox(tmp_path)

    # 1) Ordinary run on a fresh legacy box installs unit + sudo files and boots checkout & kiosk.
    res1 = box.run_provision()
    assert res1.returncode == 0, (res1.stdout, res1.stderr)
    box.verify_installed_files()

    workdir, argv, boot1 = box.boot_installed_unit()
    assert boot1.returncode == 0, boot1.stderr
    assert workdir == box.course
    assert Path(argv[0]) == box.course / ".venv" / "bin" / "uvicorn"
    assert "checkout-uvicorn" in box.log.read_text(encoding="utf-8")

    _, launched = box.run_installed_launcher()
    assert "--app=http://127.0.0.1:8008" in launched
    assert "systemctl --user start sigint101.service" in box.log.read_text(encoding="utf-8")
    box.run_installed_power_launcher()

    # 2) Stale-unit reinstall: corrupt the installed unit so step_kiosk triggers deploy/install.sh,
    # then confirm the reinstalled unit and desktop launcher still boot the checkout cleanly.
    unit_path = box.home / ".config" / "systemd" / "user" / "sigint101.service"
    unit_path.write_text("[Service]\nExecStart=/bin/false\n", encoding="utf-8")
    res2 = box.run_provision()
    assert res2.returncode == 0, (res2.stdout, res2.stderr)
    assert "differs: server unit" in res2.stdout
    box.verify_installed_files()
    _, _, boot2 = box.boot_installed_unit()
    assert boot2.returncode == 0, boot2.stderr
    box.run_installed_launcher()

    # 3) Negative guard (unit): a broken interim PR whose legacy unit points into deploy/managed/
    # MUST fail both installed-file verification and checkout boot.
    bad_unit_box = StatefulLegacyBox(tmp_path / "bad_unit")
    bad_unit = bad_unit_box.course / "deploy" / "kiosk" / "sigint101.service"
    bad_unit.write_text(
        "[Service]\nWorkingDirectory=%h/sigint-101\n"
        "ExecStart=%h/sigint-101/deploy/managed/launcher.py\n",
        encoding="utf-8",
    )
    assert bad_unit_box.run_provision().returncode == 0
    with pytest.raises(AssertionError, match="deploy/managed"):
        bad_unit_box.verify_installed_files()
    with pytest.raises(AssertionError, match="deploy/managed"):
        bad_unit_box.boot_installed_unit()

    # 4) Negative guard (M6 / R2-2 desktop launcher): pointing deploy/kiosk/start-kiosk.sh at
    # deploy/managed/opener MUST fail both installed-file verification and launcher execution.
    bad_launcher_box = StatefulLegacyBox(tmp_path / "bad_launcher")
    bad_launcher = bad_launcher_box.course / "deploy" / "kiosk" / "start-kiosk.sh"
    bad_launcher.write_text(
        '#!/bin/bash\nexec "$HOME/sigint-101/deploy/managed/opener" "$@"\n',
        encoding="utf-8",
    )
    assert bad_launcher_box.run_provision().returncode == 0
    with pytest.raises(AssertionError, match="deploy/managed"):
        bad_launcher_box.verify_installed_files()
    with pytest.raises(AssertionError):
        bad_launcher_box.run_installed_launcher()
