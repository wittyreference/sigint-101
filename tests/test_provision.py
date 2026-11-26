# ABOUTME: Tests deploy/provision.sh in a throwaway HOME with stubs on PATH, so nothing changes
# ABOUTME: on the host: missing or old Node stops with a clear message; suitable Node is idempotent.

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PROVISION = REPO / "deploy" / "provision.sh"
BASH = shutil.which("bash")

# Standard read-only utilities provision.sh calls; linked by name so /usr/bin (and real node)
# never enters the test PATH.
CORE_UTILS = (
    "awk", "basename", "cat", "cmp", "cut", "dirname", "grep", "mkdir", "paste", "readlink", "rm",
    "sort",
)


def _stub(path: Path, body: str) -> None:
    """An executable bash script using bash's absolute path (test PATH excludes /usr/bin)."""
    path.write_text(f"#!{BASH}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


class ProvisionBox:
    """Throwaway HOME and stub-only PATH for running deploy/provision.sh end to end safely."""

    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "calls.log"
        self.home.mkdir()
        self.bin.mkdir()
        self.log.touch()

        for tool in CORE_UTILS:
            found = shutil.which(tool)
            assert found, f"host lacks {tool}, needed to run deploy/provision.sh in tests"
            (self.bin / tool).symlink_to(found)

        # Fake checkout at ~/sigint-101 so step_course, step_venv and step_kiosk stay local.
        course = self.home / "sigint-101"
        (course / ".git").mkdir(parents=True)
        (course / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        venv_bin = course / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        _stub(venv_bin / "python", f'echo "venv-python $*" >> "{self.log}"\nexit 0')
        _stub(course / "bootstrap.sh", f'echo "bootstrap $*" >> "{self.log}"\nexit 0')
        (course / "deploy").mkdir()
        _stub(course / "deploy" / "install.sh", f'echo "install.sh $*" >> "{self.log}"\nexit 0')

        # System boundary stubs.
        _stub(self.bin / "id",
              'case "$1" in\n'
              '  -u) echo 1000 ;;\n'
              '  -un) echo learner ;;\n'
              '  -nG) echo "learner dialout plugdev" ;;\n'
              '  *) echo learner ;;\n'
              'esac')
        _stub(self.bin / "uname", 'echo aarch64')
        _stub(self.bin / "sudo", f'echo "sudo $*" >> "{self.log}"\nexit 0')
        _stub(self.bin / "systemctl",
              f'echo "systemctl $*" >> "{self.log}"\n'
              'if [ "$1" = "get-default" ]; then echo graphical.target; fi\n'
              'exit 0')
        _stub(self.bin / "raspi-config", f'echo "raspi-config $*" >> "{self.log}"\nexit 0')
        _stub(self.bin / "dpkg-query", 'echo ii')
        _stub(self.bin / "git",
              f'echo "git $*" >> "{self.log}"\n'
              'case "$*" in\n'
              '  *"remote get-url"*) echo "https://github.com/wittyreference/sigint-101" ;;\n'
              '  *"rev-parse --abbrev-ref"*) echo main ;;\n'
              '  *"rev-parse HEAD"*) echo 1234567890abcdef ;;\n'
              '  *"ls-remote"*) printf "1234567890abcdef\\trefs/heads/main\\n" ;;\n'
              '  *"status --porcelain"*) ;;\n'
              '  *"config --global"*) ;;\n'
              'esac\n'
              'exit 0')
        for tool in ("dump1090-fa", "acarsdec", "redsea", "rtl_ais"):
            _stub(self.bin / tool, 'exit 0')
        _stub(self.bin / "getcap", 'echo "/usr/bin/tcpdump cap_net_admin,cap_net_raw=eip"')
        _stub(self.bin / "fc-list", 'echo "DejaVu Sans Mono"')
        _stub(self.bin / "vcgencmd", 'exit 0')
        _stub(self.bin / "lsusb", 'echo "Bus 001 Device 002: ID 0bda:2838 Realtek"')
        _stub(self.bin / "nmcli", 'exit 0')

        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "USER": "learner",
            "LANG": "C.UTF-8",
        }

    def set_node(self, version: str) -> None:
        _stub(self.bin / "node", f'echo "{version}"')

    def set_node_script(self, body: str) -> None:
        _stub(self.bin / "node", body)

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(PROVISION), *args], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8")


@pytest.fixture
def box(tmp_path):
    assert BASH, "bash is needed to run deploy/provision.sh"
    return ProvisionBox(tmp_path)


@pytest.mark.parametrize("args", [(), ("--check",)], ids=["apply", "check"])
def test_missing_node_stops_with_a_clear_message_naming_the_floor(box, args):
    result = box.run(*args)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "node" in result.stderr.lower() and "20.11" in result.stderr, result.stderr
    assert "raspi-config" not in box.calls()
    assert "apt-get" not in box.calls()
    assert "install.sh" not in box.calls()


@pytest.mark.parametrize("version", ["v18.19.0", "v20.10.0", "v20.10.9", "v21.0.0", "v21.1.9"])
@pytest.mark.parametrize("args", [(), ("--check",)], ids=["apply", "check"])
def test_old_node_stops_with_a_clear_message_naming_version_and_floor(box, version, args):
    box.set_node(version)
    result = box.run(*args)
    assert result.returncode != 0, result.stdout + result.stderr
    assert version in result.stderr, result.stderr
    assert "20.11" in result.stderr, result.stderr
    assert "raspi-config" not in box.calls()
    assert "apt-get" not in box.calls()
    assert "install.sh" not in box.calls()


@pytest.mark.parametrize("body", [
    'echo "not-a-version"; exit 0',
    'echo "broken" >&2; exit 1',
], ids=["unreadable", "nonzero"])
def test_broken_node_version_stops_with_a_clear_message(box, body):
    box.set_node_script(body)
    result = box.run("--check")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "20.11" in result.stderr, result.stderr


@pytest.mark.parametrize("version", ["v20.11.0", "v20.18.1", "v21.2.0", "v22.0.0", "v24.0.0-rc.1"])
def test_supported_node_passes_and_provision_runs_to_completion(box, version):
    box.set_node(version)
    result = box.run("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"ok     node {version}" in result.stdout, result.stdout


def test_provision_with_supported_node_is_idempotent_across_two_runs(box):
    box.set_node("v22.0.0")
    first = box.run()
    second = box.run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "ok     node v22.0.0" in first.stdout
    assert "ok     node v22.0.0" in second.stdout
