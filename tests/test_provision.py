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
    "awk", "basename", "cat", "cmp", "cut", "date", "dirname", "grep", "mkdir", "paste",
    "readlink", "rm", "sha256sum", "sleep", "sort", "timeout",
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
        _stub(self.bin / "rtl_ais-sigint101", 'echo sigint101-ais-udp-v1')
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
                 active=True, writable=True, tx_writable=True, show_tx_enabled=True,
                 profile_status=0, read_lag=False, restart=None):
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
              f'restart_boundary="{state}.restart-at"\n'
              'restart_daemon() {\n'
              '  echo "$(( $(date +%s%3N) + 3500 ))" > "$restart_boundary"\n'
              '}\n'
              'if [ -f "$restart_boundary" ]; then\n'
              '  read -r down_until < "$restart_boundary"\n'
              '  if (( $(date +%s%3N) < down_until )); then exit 1; fi\n'
              'fi\n'
              f'read -r region preset use power enabled < "{state}"\n'
              'status=0\n'
              f'out="{state}"\n'
              'case "$*" in\n'
              f'  "{get_profile}"|"{get_profile} --get lora.tx_power"'
              f'|"{get_profile} --get lora.tx_power --get lora.tx_enabled")\n'
              '    printf "Connected to radio\\nlora.region: %s\\nlora.modem_preset: %s\\n'
              'lora.use_preset: %s\\n" "$region" "$preset" "$use"\n'
              '    if [[ "$*" == *" --get lora.tx_power"* ]]; then '
              'printf "lora.tx_power: %s\\n" "$power"; fi\n'
              f'    if [[ "{int(show_tx_enabled)}" == 1 && "$*" == *" --get lora.tx_enabled" ]]; '
              'then printf "lora.tx_enabled: %s\\n" "$enabled"; fi\n'
              # The first connection can read the old config while the write is in flight.
              f'    if [ -f "{state}.pending" ]; then '
              f'cat "{state}.pending" > "{state}"; rm "{state}.pending"; fi\n'
              '    echo "Completed getting preferences"; exit 0 ;;\n'
              f'  "{set_profile}"|"{set_profile} --set lora.tx_power "*)\n'
              # Firmware enables TX when a region is set for the first time.
              '    if [[ "$region" == 0 ]]; then enabled=True; fi\n'
              '    region=1; preset=9; use=True\n'
              f'    status={profile_status}\n'
              '    if [ "$#" -eq 12 ]; then power="${12}"; fi\n'
              f'    if [[ "{restart}" == after-region ]]; then restart_daemon; fi ;;\n'
              '  "--set lora.tx_power "*) power="$3" ;;\n'
              '  "--set lora.tx_enabled false")\n'
              f'    if [[ "{restart}" == lost-restore && ! -f "$restart_boundary" ]]; then\n'
              '      restart_daemon\n'
              '      exit 0\n'
              '    fi\n'
              f'    if [[ "{int(tx_writable)}" == 1 ]]; then enabled=False; fi ;;\n'
              '  *) exit 1 ;;\n'
              'esac\n'
              f'if [ "{int(read_lag)}" = 1 ]; then out="{state}.pending"; fi\n'
              f'if [ "{int(writable)}" = 1 ]; then '
              'echo "$region $preset $use $power $enabled" > "$out"; fi\n'
              'exit "$status"')
        return state

    def run(self, *args: str, script=PROVISION, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(script), *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8")


@pytest.fixture
def box(tmp_path):
    assert BASH, "bash is needed to run deploy/provision.sh"
    return ProvisionBox(tmp_path)


@pytest.fixture
def ais_install(box):
    """Redirect only installation paths and replace build commands at their boundaries."""
    course = box.home / "sigint-101"
    deploy = course / "deploy"
    stamp_dir = box.home / "build-stamps"
    stock = box.bin / "rtl_ais"
    companion = box.bin / "rtl_ais-sigint101"
    companion.unlink()
    stamp_dir.mkdir()
    stock_stamp = stamp_dir / "rtl_ais"
    stock_stamp.write_text(
        "https://github.com/dgiardini/rtl-ais@d84764bd20d93de68d3275e4914f897adfb353cc\n",
        encoding="utf-8",
    )
    text = PROVISION.read_text(encoding="utf-8").replace(
        "/usr/local/share/sigint101/builds", str(stamp_dir),
    ).replace("/usr/local/bin/rtl_ais-sigint101", str(companion))
    script = deploy / "provision.sh"
    script.write_text(text, encoding="utf-8")
    patch = deploy / "patches" / "rtl-ais-companion.patch"
    patch.parent.mkdir()
    patch.write_text("patch revision one\n", encoding="utf-8")

    _stub(box.bin / "nproc", "echo 2")
    _stub(box.bin / "patch",
          f'echo "patch $*" >> "{box.log}"\n'
          '[[ "${FAIL_PATCH:-0}" != 1 ]]')
    _stub(box.bin / "make",
          f'echo "make $*" >> "{box.log}"\n'
          '[[ "$*" != *install* ]] || exit 1\n'
          f'printf "%s\\n" "#!{BASH}" "echo ${{CAPABILITY:-sigint101-ais-udp-v1}}" > rtl_ais\n'
          'chmod +x rtl_ais')
    for tool in ("bash", "chmod", "cp", "touch"):
        (box.bin / tool).symlink_to(shutil.which(tool))
    _stub(box.bin / "sudo",
          f'echo "sudo $*" >> "{box.log}"\n'
          'case "$1" in\n'
          f'  tee) if [[ "$2" == "{stamp_dir}/{companion.name}" && '
          '"${FAIL_AIS_STAMP:-0}" == 1 ]]; then\n'
          '      cat >/dev/null; exit 1; fi\n'
          f'    if [[ "$2" == "{stamp_dir}/"* ]]; then cat >"$2"; else cat >/dev/null; fi ;;\n'
          f'  install) if [[ "${{@: -1}}" == "{companion}" ]]; then\n'
          f'    cp rtl_ais "{companion}"; fi ;;\n'
          f'  touch|rm) if [[ "${{@: -1}}" == "{stamp_dir}/{companion.name}.pending" ]]; then\n'
          '    exec "$@"; fi ;;\n'
          '  make) exit 1 ;;\n'
          'esac\n'
          'exit 0')
    _stub(course / ".venv/bin/python", f'echo "venv-python $*" >> "{box.log}"')
    return script, patch, stock, stock_stamp, companion, stamp_dir / companion.name


def test_provision_builds_and_checks_the_ais_companion(box, ais_install):
    script, patch, stock, stock_stamp, companion, stamp = ais_install
    original = (stock.read_bytes(), stock_stamp.read_bytes())
    check = box.run("--check", script=script)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "would  build rtl_ais-sigint101" in check.stdout
    assert not companion.exists() and not stamp.exists()
    assert "patch " not in box.calls()

    apply = box.run(script=script)
    assert apply.returncode == 0, apply.stdout + apply.stderr
    assert "built rtl_ais-sigint101" in apply.stdout
    assert "test_rtl_ais_native.py" in box.calls()
    assert companion.is_file() and stamp.is_file()
    assert (stock.read_bytes(), stock_stamp.read_bytes()) == original
    assert "sudo make install" not in box.calls()
    old_stamp = stamp.read_text(encoding="utf-8")

    check = box.run("--check", script=script)
    assert "would  build rtl_ais-sigint101" not in check.stdout
    assert "rtl_ais-sigint101:" in check.stdout
    assert box.run(script=script).returncode == 0
    assert box.calls().count("patch ") == 1

    patch.write_text("patch revision two\n", encoding="utf-8")
    rebuilt = box.run(script=script)
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    assert box.calls().count("patch ") == 2
    assert stamp.read_text(encoding="utf-8") != old_stamp


@pytest.mark.parametrize("failure", [{"FAIL_PATCH": "1"}, {"CAPABILITY": "wrong-version"}])
def test_provision_ais_build_failure_does_not_stamp(box, ais_install, failure):
    script, _, _, _, companion, stamp = ais_install
    box.env.update(failure)
    result = box.run(script=script)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not companion.exists() and not stamp.exists()


def test_provision_retries_an_ais_install_interrupted_before_stamping(box, ais_install):
    script, patch, _, _, companion, stamp = ais_install
    box.env["FAIL_AIS_STAMP"] = "1"
    interrupted = box.run(script=script)
    assert interrupted.returncode != 0, interrupted.stdout + interrupted.stderr
    assert companion.is_file() and not stamp.exists()

    check = box.run("--check", script=script)
    assert "would  build rtl_ais-sigint101" in check.stdout
    assert "unmanaged" not in check.stdout and not stamp.exists()

    del box.env["FAIL_AIS_STAMP"]
    patch.write_text("patch revision two\n", encoding="utf-8")
    retried = box.run(script=script)
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert "built rtl_ais-sigint101" in retried.stdout
    assert stamp.is_file() and not stamp.with_suffix(".pending").exists()
    assert box.calls().count("patch ") == 2


def test_provision_refuses_an_incompatible_unmanaged_companion(box, ais_install):
    script, _, _, _, companion, stamp = ais_install
    _stub(companion, "echo wrong-version")
    before = companion.read_bytes()
    checked = box.run("--check", script=script)
    assert "incompatible" in checked.stdout and "rtl_ais-sigint101" in checked.stdout
    applied = box.run(script=script)
    assert applied.returncode != 0 and "rtl_ais-sigint101" in applied.stderr
    assert companion.read_bytes() == before and not stamp.exists()
    assert "patch " not in box.calls()


def test_existing_provision_run_installs_companion_after_fast_forward(box, ais_install):
    script, _, _, _, companion, stamp = ais_install
    deploy = script.parent
    legacy = box.home / "provision-before-update.sh"
    legacy.write_text(script.read_text(encoding="utf-8").replace(
        "\n  ensure_ais_companion\n", "\n"), encoding="utf-8")
    # The legacy process keeps its parsed build recipes, then invokes install.sh
    # from the updated checkout because its installed server unit differs.
    install = deploy / "install.sh"
    updated = deploy / "install-after-update.sh"
    shutil.copyfile(PROVISION.with_name("install.sh"), updated)
    updated.chmod(0o755)
    for folder in ("desktop", "kiosk"):
        (deploy / folder).mkdir()
    (deploy / "desktop/apply-theme.sh").write_text("exit 0\n", encoding="utf-8")
    _stub(box.bin / "install", "exit 0")
    git = box.bin / "git"
    original_git = git.read_text(encoding="utf-8")
    git.write_text(original_git.replace('case "$*" in\n',
        'case "$*" in\n'
        f'  *"pull --ff-only"*) cp "{updated}" "{install}"; chmod +x "{install}" ;;\n'
        '  *"ls-remote"*) printf "updated\\trefs/heads/main\\n" ;;\n', 1), encoding="utf-8")
    result = box.run(script=legacy)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pull --ff-only" in box.calls()
    assert "built rtl_ais-sigint101" in result.stdout
    assert companion.is_file() and stamp.is_file()


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


def test_check_reports_an_installed_course_launcher_that_differs_from_the_checkout(box):
    """A box keeps running its installed launcher until install.sh copies the new one over, so a
    launcher fix (the full-panel window bounds) reaches the panel only if --check flags it."""
    kiosk = box.home / "sigint-101" / "deploy" / "kiosk"
    kiosk.mkdir()
    (kiosk / "start-kiosk.sh").write_text((REPO / "deploy" / "kiosk" / "start-kiosk.sh")
                                          .read_text(encoding="utf-8"), encoding="utf-8")
    result = box.run("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    [line] = [ln for ln in result.stdout.splitlines() if "run deploy/install.sh" in ln]
    assert line.startswith("  would  "), line
    assert "course launcher" in line, line
    assert "install.sh" not in box.calls(), "--check ran install.sh"


@pytest.mark.parametrize("region,preset,use_preset,tx_enabled", [(0, 0, False, False),
                                                                 (1, 0, True, True)])
def test_provision_sets_us_long_turbo_and_power_and_preserves_tx_enabled(
        box, region, preset, use_preset, tx_enabled):
    state = box.set_mesh(region, preset, use_preset, tx_power=30, tx_enabled=tx_enabled)
    checked = box.run("--check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert state.read_text() == f"{region} {preset} {use_preset} 30 {tx_enabled}\n"
    assert "--set lora." not in box.calls()
    first = box.run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert state.read_text() == f"1 9 True 8 {tx_enabled}\n"
    mesh_calls = [line for line in box.calls().splitlines() if line.startswith("meshtastic ")]
    assert "--get lora.tx_enabled" in mesh_calls[1]
    assert "--set lora.region US" in mesh_calls[2]
    if region == 0 and not tx_enabled:
        assert "--get lora.tx_enabled" in mesh_calls[3]
        assert mesh_calls[4].endswith("--set lora.tx_enabled false")
        assert "--get lora.tx_enabled" in mesh_calls[5]
    else:
        assert "--set lora.tx_enabled" not in box.calls()
    second = box.run()
    assert second.returncode == 0, second.stdout + second.stderr
    assert box.calls().count("--set lora.region US --set lora.modem_preset LONG_TURBO "
                             "--set lora.use_preset true --set lora.tx_power 8") == 1
    assert box.calls().count("--set lora.tx_enabled false") == int(region == 0 and not tx_enabled)


def test_provision_requires_tx_enabled_readback_before_changing_region(box):
    state = box.set_mesh(0, 0, False, show_tx_enabled=False)
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "could not read LoRa settings" in result.stdout
    assert state.read_text() == "0 0 False 8 False\n"
    assert "--set lora." not in box.calls()


def test_provision_restores_tx_if_the_first_region_write_applied_but_reported_failure(box):
    state = box.set_mesh(0, 0, False, tx_power=30, profile_status=1)
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert state.read_text() == "1 9 True 8 False\n"
    assert "--set lora.tx_enabled false" in box.calls()


def test_provision_waits_for_region_write_before_restoring_tx(box):
    state = box.set_mesh(0, 0, False, tx_power=30, read_lag=True)
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert state.read_text() == "1 9 True 8 False\n"


@pytest.mark.parametrize("restart", ["after-region", "lost-restore"])
def test_provision_restores_transmit_across_daemon_restart(box, restart):
    state = box.set_mesh(0, 0, False, tx_power=30, restart=restart)
    result = box.run(timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert state.read_text() == "1 9 True 8 False\n"
    calls = box.calls()
    assert calls.count("--set lora.region") == 1
    assert calls.count("--set lora.tx_enabled false") >= (2 if restart == "lost-restore" else 1)
    marker = box.home / ".local/state/sigint101/meshtastic-defaults"
    assert marker.is_dir() and not marker.with_name(marker.name + "-tx-off-pending").exists()


@pytest.mark.parametrize("power", [0, 30])
def test_provision_caps_power_in_the_first_region_write(box, power):
    box.set_mesh(0, 0, False, tx_power=power)
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    [region_write] = [line for line in box.calls().splitlines() if "--set lora.region US" in line]
    assert region_write.endswith("--set lora.tx_power 8")


def test_check_previews_temporary_transmit_on_a_fresh_node_with_transmit_off(box):
    state = box.set_mesh(0, 0, False, tx_power=30)
    result = box.run("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "firmware may briefly enable transmit when setting the region" in result.stdout
    assert "turn transmit back off" in result.stdout
    assert "restore local meshtasticd LoRa tx_enabled to false" not in result.stdout
    assert state.read_text() == "0 0 False 30 False\n"
    assert "--set lora." not in box.calls()


def test_provision_retries_unconfirmed_tx_restoration(box):
    state = box.set_mesh(0, 0, False, tx_writable=False)
    marker = box.home / ".local/state/sigint101/meshtastic-defaults"
    pending = marker.with_name(marker.name + "-tx-off-pending")
    first = box.run(timeout=45)
    assert first.returncode != 0
    assert "could not confirm lora.tx_enabled False" in first.stderr
    assert "Transmit may be on now" in first.stderr
    assert "turn it back off" in first.stderr
    assert state.read_text() == "1 9 True 8 True\n"
    assert box.calls().count("--set lora.tx_enabled false") > 1
    assert not marker.exists() and pending.is_dir()

    # Another run must remember that the radio was off before the first region write.
    box.set_mesh(1, 9, tx_enabled=True)
    checked = box.run("--check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "transmit is on now" in checked.stdout
    assert "turn transmit back off" in checked.stdout
    assert state.read_text() == "1 9 True 8 True\n"
    second = box.run()
    assert second.returncode == 0, second.stdout + second.stderr
    assert state.read_text() == "1 9 True 8 False\n"
    assert marker.is_dir() and not pending.exists()


@pytest.mark.parametrize("args", [(), ("--check",)], ids=["apply", "check"])
def test_stopped_meshtasticd_warns_and_keeps_pending_tx_restore(box, args):
    state = box.set_mesh(1, 9, tx_enabled=True, active=False)
    pending = box.home / ".local/state/sigint101/meshtastic-defaults-tx-off-pending"
    pending.mkdir(parents=True)
    result = box.run(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^  WARN   .*TX restoration pending", result.stdout, re.MULTILINE)
    assert "Transmit may be on when meshtasticd starts" in result.stdout
    assert "Hand-over hygiene" in result.stdout
    assert pending.is_dir() and state.read_text() == "1 9 True 8 True\n"
    assert "--set lora." not in box.calls()


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
    assert f"configured tx_power {description}" in result.stdout
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
    assert "--set lora.tx_enabled" not in box.calls()


@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("power,ceiling", [(2, None), (8, "22"), (8, None)])
def test_provision_leaves_power_at_or_below_the_ceiling(box, marked, power, ceiling):
    if marked:
        (box.home / ".local/state/sigint101/meshtastic-defaults").mkdir(parents=True)
    state = box.set_mesh(1, 0, tx_power=power)
    if ceiling:
        box.env["SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM"] = ceiling

    checked = box.run("--check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert not re.search(r"^  would  .*tx_power", checked.stdout, re.MULTILINE)
    applied = box.run()
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert state.read_text() == f"1 {0 if marked else 9} True {power} False\n"
    assert "--set lora.tx_power" not in box.calls()


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


@pytest.mark.parametrize("current,ceiling,requested,wanted", [
    (30, "14", "12", 12), (30, "6", None, 6), (2, "14", "12", 12),
    (2, "22", "22", 22), (2, "-9", "-9", -9),
])
def test_provision_uses_an_explicit_module_ceiling(box, current, ceiling, requested, wanted):
    state = box.set_mesh(1, 9, tx_power=current)
    box.env["SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM"] = ceiling
    if requested is not None:
        box.env["SIGINT101_MESHTASTIC_TX_POWER_DBM"] = requested
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert state.read_text() == f"1 9 True {wanted} False\n"
    assert "--set lora.tx_enabled" not in box.calls()


@pytest.mark.parametrize("variable,value", [
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "0"),
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "1+8"),
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "-226"),
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "-10"),
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "23"),
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "08"),
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", " 8"),
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "abc"),
    ("SIGINT101_MESHTASTIC_TX_POWER_DBM", "a[$(id)]"),
    ("SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM", ""),
    ("SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM", "-10"),
    ("SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM", "23"),
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


def test_provision_help_identifies_the_default_power_grant(box):
    result = box.run("--help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FCC ID 2ALPH-E22900M22S" in result.stdout
    assert "0.0076 W conducted" in result.stdout
    assert "500 kHz Long Turbo" in result.stdout
    assert "its own FCC grant" in result.stdout


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
