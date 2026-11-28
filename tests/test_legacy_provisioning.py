# ABOUTME: Legacy-provisioning and checkout-boot guard (§8, U2, R2-2) run on every updater PR.
# ABOUTME: Executes real deploy/provision.sh and deploy/install.sh with a stateful sudo sysroot.

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")

CORE_UTILS = (
    "awk", "basename", "bash", "cat", "cut", "dirname", "grep", "head", "install", "ls",
    "mkdir", "paste", "readlink", "rm", "sed", "sort", "timeout",
)


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!{BASH}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


class StatefulLegacyBox:
    """Disposable legacy kiosk fixture where sudo-installed files land in a real sysroot.

    Unlike a no-op sudo stub, R2-2 requires the legacy-install test to observe every file
    installed via sudo and execute the installed systemd user unit through a stateful fixture.
    """

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.sysroot = tmp_path / "sysroot"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "calls.log"
        self.service_state = tmp_path / "service-state"
        self.home.mkdir(parents=True)
        self.sysroot.mkdir(parents=True)
        self.bin.mkdir(parents=True)
        self.log.touch()

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
            '  "--user --quiet is-enabled sigint101.service")\n'
            f'    [ -f "{self.service_state}" ]; exit $? ;;\n'
            '  *) exit 0 ;;\n'
            'esac\n',
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
        _stub(self.bin / "getcap", 'echo "/usr/bin/tcpdump cap_net_admin,cap_net_raw=eip"')
        _stub(self.bin / "fc-list", 'echo "PT Mono"; echo "Russo One"; echo "DejaVu Sans Mono"')
        _stub(self.bin / "lsusb", 'echo "Bus 001 Device 002: ID 0bda:2838 Realtek"')
        _stub(self.bin / "node", 'echo "v22.0.0"')
        _stub(self.bin / "pgrep", "exit 1")
        _stub(self.bin / "touch", 'exec /usr/bin/touch "$@"')

        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "USER": "learner",
            "LANG": "C.UTF-8",
        }

    def run_provision(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, str(self.course / "deploy" / "provision.sh")],
            env=self.env, capture_output=True, text=True, timeout=30,
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


def test_interim_pr_legacy_provisioning_still_boots_checkout(tmp_path):
    assert BASH, "bash is required"
    box = StatefulLegacyBox(tmp_path)

    # 1) Ordinary run on a fresh legacy box installs unit + sudo files and boots the checkout.
    res1 = box.run_provision()
    assert res1.returncode == 0, (res1.stdout, res1.stderr)
    assert (box.home / ".config/systemd/user/sigint101.service").read_bytes() == (
        REPO / "deploy/kiosk/sigint101.service"
    ).read_bytes()
    # R2-2: verify files installed through sudo landed in sysroot with exact checkout bytes.
    for rel_src, rel_dst in (
        ("deploy/kiosk/start-kiosk.sh", "usr/local/bin/sigint101-course"),
        ("deploy/kiosk/sigint101.desktop", "usr/local/share/applications/sigint101.desktop"),
        ("deploy/kiosk/sigint101.svg", "usr/local/share/icons/hicolor/scalable/apps/sigint101.svg"),
        ("deploy/system/journald-sigint101.conf", "etc/systemd/journald.conf.d/50-sigint101.conf"),
    ):
        installed = box.sysroot / rel_dst
        assert installed.is_file(), f"sudo-installed file missing: {rel_dst}"
        assert installed.read_bytes() == (REPO / rel_src).read_bytes()

    workdir, argv, boot1 = box.boot_installed_unit()
    assert boot1.returncode == 0, boot1.stderr
    assert workdir == box.course
    assert Path(argv[0]) == box.course / ".venv" / "bin" / "uvicorn"
    assert "checkout-uvicorn" in box.log.read_text(encoding="utf-8")

    # 2) Stale-unit reinstall: corrupt the installed unit so step_kiosk triggers deploy/install.sh,
    # then confirm the reinstalled unit still boots the checkout cleanly.
    unit_path = box.home / ".config/systemd/user/sigint101.service"
    unit_path.write_text("[Service]\nExecStart=/bin/false\n", encoding="utf-8")
    res2 = box.run_provision()
    assert res2.returncode == 0, (res2.stdout, res2.stderr)
    assert "differs: server unit" in res2.stdout
    _, _, boot2 = box.boot_installed_unit()
    assert boot2.returncode == 0, boot2.stderr

    # 3) Negative guard: a broken interim PR whose legacy unit points into deploy/managed/ MUST
    # fail checkout boot.
    bad_box = StatefulLegacyBox(tmp_path / "bad")
    bad_unit = bad_box.course / "deploy" / "kiosk" / "sigint101.service"
    bad_unit.write_text(
        "[Service]\nWorkingDirectory=%h/sigint-101\n"
        "ExecStart=%h/sigint-101/deploy/managed/launcher.py\n",
        encoding="utf-8",
    )
    assert bad_box.run_provision().returncode == 0
    with pytest.raises(AssertionError, match="deploy/managed"):
        bad_box.boot_installed_unit()
