# ABOUTME: Tests deploy/provision.sh in a throwaway HOME with stubs on PATH, so nothing changes
# ABOUTME: on the host: missing or old Node warns and continues (or reports pending under --check).

import os
import re
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
    "sort", "timeout",
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
        # write_root_file pipes configuration into sudo tee. Drain it like tee would,
        # so the producer can finish even if the stub starts first.
        _stub(self.bin / "sudo",
              f'echo "sudo $*" >> "{self.log}"\n'
              'if [ "$1" = tee ]; then exec cat >/dev/null; fi\n'
              'exit 0')
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

    def set_mesh(self, region, preset, use_preset=True, *, tx_power=8, tx_enabled=False,
                 active=True, writable=True):
        """Stub the local meshtasticd service and its CLI's LoRa preference read/write."""
        state = self.home / "mesh-state"
        state.write_text(f"{region} {preset} {use_preset} {tx_power} {tx_enabled}\n",
                         encoding="utf-8")
        _stub(self.bin / "systemctl",
              f'echo "systemctl $*" >> "{self.log}"\n'
              'if [ "$1" = "get-default" ]; then echo graphical.target; fi\n'
              f'if [[ "$*" == "--quiet is-active meshtasticd" ]]; then exit {int(not active)}; fi\n'
              'exit 0')
        cli = self.home / "sigint-101" / ".venv" / "bin" / "meshtastic"
        get_profile = "--get lora.region --get lora.modem_preset --get lora.use_preset"
        set_profile = ("--set lora.region US --set lora.modem_preset LONG_TURBO "
                       "--set lora.use_preset true")
        _stub(cli,
              f'echo "meshtastic $*" >> "{self.log}"\n'
              '[[ "$1 $2" == "--host 127.0.0.1" ]] || exit 1\n'
              'shift 2\n'
              'if [[ "$1" == "--timeout" ]]; then shift 2; fi\n'
              f'read -r region preset use power enabled < "{state}"\n'
              'case "$*" in\n'
              f'  "{get_profile}"|"{get_profile} --get lora.tx_power")\n'
              '    printf "Connected to radio\\nlora.region: %s\\nlora.modem_preset: %s\\n'
              'lora.use_preset: %s\\n" "$region" "$preset" "$use"\n'
              '    if [[ "$*" == *" --get lora.tx_power" ]]; then '
              'printf "lora.tx_power: %s\\n" "$power"; fi\n'
              '    echo "Completed getting preferences"; exit 0 ;;\n'
              f'  "{set_profile}"|"{set_profile} --set lora.tx_power "*)\n'
              '    region=1; preset=9; use=True\n'
              '    if [ "$#" -eq 12 ]; then power="${12}"; fi ;;\n'
              '  "--set lora.tx_power "*) power="$3" ;;\n'
              '  *) exit 1 ;;\n'
              'esac\n'
              f'if [ "{int(writable)}" = 1 ]; then '
              f'echo "$region $preset $use $power $enabled" > "{state}"; fi')
        return state

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(PROVISION), *args], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8")


@pytest.fixture
def box(tmp_path):
    assert BASH, "bash is needed to run deploy/provision.sh"
    return ProvisionBox(tmp_path)


def _node_section(stdout: str) -> list[str]:
    """Return the output lines emitted inside step_node."""
    found = re.search(r"^==> Node\.js[^\n]*\n(.*?)(?=^==> |\Z)", stdout, re.MULTILINE | re.DOTALL)
    assert found, f"no Node.js section in output:\n{stdout}"
    return [line for line in found.group(1).splitlines() if line.strip()]


def _pending_count(stdout: str) -> int:
    match = re.search(r"^check: (\d+) change\(s\) pending,", stdout, re.MULTILINE)
    assert match, f"no check summary line in output:\n{stdout}"
    return int(match.group(1))


def test_sudo_tee_stub_drains_its_input_without_writing_a_file(box):
    # Keep a copy of the read end to check what tee left behind. Put the bytes in the
    # pipe before launching it so this check needs no scheduling or timeout assumptions.
    read_fd, write_fd = os.pipe()
    target = box.home / "root.conf"
    with os.fdopen(read_fd, "rb") as source:
        with os.fdopen(write_fd, "wb") as sink:
            sink.write(b"provision config\n")
        result = subprocess.run([str(box.bin / "sudo"), "tee", str(target)], stdin=source,
                                env=box.env, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert source.read() == b"", "the tee stub exited without draining the provision pipe"
    assert not target.exists()


def test_missing_node_warns_and_provisioning_continues(box):
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    lines = _node_section(result.stdout)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("  WARN   "), line
    assert "node" in line.lower() and "20.11" in line, line
    assert "panel tests" in line.lower(), line
    assert "install.sh" in box.calls(), "provisioning did not continue past step_node"


def test_missing_node_under_check_is_reported_as_pending_without_failing(box):
    missing = box.run("--check")
    assert missing.returncode == 0, missing.stdout + missing.stderr
    lines = _node_section(missing.stdout)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("  would  "), line
    assert "node" in line.lower() and "20.11" in line, line
    assert "panel tests" in line.lower(), line

    box.set_node("v22.0.0")
    present = box.run("--check")
    assert present.returncode == 0, present.stdout + present.stderr
    assert _pending_count(missing.stdout) == _pending_count(present.stdout) + 1


@pytest.mark.parametrize("version", ["v18.19.0", "v20.10.0", "v20.10.9", "v21.0.0", "v21.1.9"])
def test_old_node_warns_and_provisioning_continues(box, version):
    box.set_node(version)
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    lines = _node_section(result.stdout)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("  WARN   "), line
    assert version in line and "20.11" in line, line
    assert "panel tests" in line.lower(), line
    assert "install.sh" in box.calls(), "provisioning did not continue past step_node"


@pytest.mark.parametrize("version", ["v18.19.0", "v20.10.0", "v20.10.9", "v21.0.0", "v21.1.9"])
def test_old_node_under_check_is_reported_as_pending_without_failing(box, version):
    box.set_node(version)
    result = box.run("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = _node_section(result.stdout)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("  would  "), line
    assert version in line and "20.11" in line, line
    assert "panel tests" in line.lower(), line


@pytest.mark.parametrize("body", [
    'echo "not-a-version"; exit 0',
    'echo "broken" >&2; exit 1',
], ids=["unreadable", "nonzero"])
@pytest.mark.parametrize("args, prefix", [
    ((), "  WARN   "),
    (("--check",), "  would  "),
], ids=["apply", "check"])
def test_broken_node_version_warns_or_reports_pending_without_failing(box, body, args, prefix):
    box.set_node_script(body)
    result = box.run(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = _node_section(result.stdout)
    assert len(lines) == 1, lines
    assert lines[0].startswith(prefix) and "20.11" in lines[0], lines[0]
    assert "panel tests" in lines[0].lower(), lines[0]


@pytest.mark.parametrize("version", ["v20.11.0", "v20.18.1", "v21.2.0", "v22.0.0", "v24.0.0-rc.1"])
def test_supported_node_passes_and_provision_runs_to_completion(box, version):
    box.set_node(version)
    result = box.run("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = _node_section(result.stdout)
    assert len(lines) == 1, lines
    assert lines[0].startswith(f"  ok     node {version}"), lines[0]


@pytest.mark.parametrize("version", [None, "v18.19.0", "v22.0.0"],
                         ids=["missing", "old", "supported"])
def test_provision_node_check_is_idempotent_across_two_runs(box, version):
    if version is not None:
        box.set_node(version)
    first = box.run()
    second = box.run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert _node_section(first.stdout) == _node_section(second.stdout)


@pytest.mark.parametrize("region,preset,use_preset,tx_enabled", [(0, 0, False, False),
                                                                 (1, 0, True, True)])
def test_provision_sets_us_long_turbo_and_power_without_touching_tx_enabled(
        box, region, preset, use_preset, tx_enabled):
    state = box.set_mesh(region, preset, use_preset, tx_power=30, tx_enabled=tx_enabled)
    first = box.run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert "lora.tx_enabled" not in box.calls()
    assert state.read_text() == f"1 9 True 8 {tx_enabled}\n"
    second = box.run()
    assert second.returncode == 0, second.stdout + second.stderr
    assert box.calls().count("--set lora.region US --set lora.modem_preset LONG_TURBO "
                             "--set lora.use_preset true --set lora.tx_power 8") == 1
    assert "lora.tx_enabled" not in box.calls()


def test_provision_check_reports_current_mesh_preset_without_writing(box):
    state = box.set_mesh(1, 0)
    result = box.run("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "region US, modem preset LONG_FAST" in result.stdout
    assert re.search(r"^  would  .*LONG_TURBO", result.stdout, re.MULTILINE)
    assert state.read_text() == "1 0 True 8 False\n"
    assert "--set lora." not in box.calls()
    applied = box.run()
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert state.read_text() == "1 9 True 8 False\n"


@pytest.mark.parametrize("power,description", [(30, "30 dBm"), (0, "radio default (0)")])
def test_provision_check_reports_mesh_power_drift_without_writing(box, power, description):
    state = box.set_mesh(1, 9, tx_power=power)
    result = box.run("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"tx_power {description}" in result.stdout
    assert re.search(r"^  would  .*tx_power (?:to )?8 dBm", result.stdout, re.MULTILINE)
    assert state.read_text() == f"1 9 True {power} False\n"
    assert "--set lora." not in box.calls()


def test_provision_corrects_power_without_overwriting_the_learner_preset(box):
    state = box.set_mesh(1, 0)
    first = box.run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert state.read_text() == "1 9 True 8 False\n"
    state.write_text("1 0 True 30 False\n", encoding="utf-8")
    checked = box.run("--check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert re.search(r"^  would  .*tx_power to 8 dBm", checked.stdout, re.MULTILINE)
    assert state.read_text() == "1 0 True 30 False\n"
    second = box.run()
    assert second.returncode == 0, second.stdout + second.stderr
    assert state.read_text() == "1 0 True 8 False\n"
    assert "region US, modem preset LONG_FAST" in second.stdout
    assert box.calls().count("--set lora.region") == 1
    assert any(line.endswith("--set lora.tx_power 8") and "--set lora.region" not in line
               for line in box.calls().splitlines())
    assert "lora.tx_enabled" not in box.calls()


@pytest.mark.parametrize("ceiling,requested", [(None, "9"), ("10", "11")],
                         ids=["built-in-ceiling", "module-ceiling"])
def test_provision_refuses_requested_mesh_power_above_the_ceiling(box, ceiling, requested):
    state = box.set_mesh(1, 9)
    box.env["SIGINT101_MESHTASTIC_TX_POWER_DBM"] = requested
    if ceiling is not None:
        box.env["SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM"] = ceiling
    result = box.run()
    assert result.returncode != 0
    assert f"{requested} dBm" in result.stderr and f"{ceiling or '8'} dBm ceiling" in result.stderr
    assert state.read_text() == "1 9 True 8 False\n"
    assert not box.calls()


@pytest.mark.parametrize("ceiling,requested,wanted", [("14", "12", 12), ("6", None, 6)])
def test_provision_uses_an_explicit_module_ceiling(box, ceiling, requested, wanted):
    state = box.set_mesh(1, 9, tx_power=30)
    box.env["SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM"] = ceiling
    if requested is not None:
        box.env["SIGINT101_MESHTASTIC_TX_POWER_DBM"] = requested
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert state.read_text() == f"1 9 True {wanted} False\n"
    assert "lora.tx_enabled" not in box.calls()


@pytest.mark.parametrize("variable,value", [
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "0"),
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "1+8"),
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "-226"),
    ("SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM", ""),
    ("SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM", "256"),
    ("SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM", "9223372036854775808"),
])
def test_provision_refuses_invalid_mesh_power_inputs(box, variable, value):
    box.set_mesh(1, 9)
    box.env[variable] = value
    result = box.run("--check")
    assert result.returncode != 0
    assert variable in result.stderr and "nonzero" in result.stderr
    assert not box.calls()


def test_provision_leaves_inactive_meshtasticd_alone(box):
    box.set_mesh(1, 0, active=False)
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "meshtasticd is not running" in result.stdout
    assert not any(line.startswith("meshtastic ") for line in box.calls().splitlines())
    assert not any("meshtasticd" in line and re.search(r"\b(start|restart|enable)\b", line)
                   for line in box.calls().splitlines())


def test_provision_retries_an_unconfirmed_mesh_default(box):
    state = box.set_mesh(1, 0, writable=False)
    first = box.run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert "could not confirm US / LONG_TURBO" in first.stdout
    assert state.read_text() == "1 0 True 8 False\n"
    box.set_mesh(1, 0)
    second = box.run()
    assert second.returncode == 0, second.stdout + second.stderr
    assert state.read_text() == "1 9 True 8 False\n"
    assert box.calls().count("--set lora.region") == 2


def test_provision_does_not_mark_an_unconfirmed_mesh_power_default_done(box):
    state = box.set_mesh(1, 9, tx_power=30, writable=False)
    first = box.run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert "could not confirm US / LONG_TURBO with tx_power 8 dBm" in first.stdout
    assert state.read_text() == "1 9 True 30 False\n"
    assert not (box.home / ".local/state/sigint101/meshtastic-defaults").exists()

    box.set_mesh(1, 9, tx_power=30)
    second = box.run()
    assert second.returncode == 0, second.stdout + second.stderr
    assert state.read_text() == "1 9 True 8 False\n"
    assert box.calls().count("--set lora.tx_power 8") == 2
