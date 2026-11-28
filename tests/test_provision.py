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
    "awk", "basename", "cat", "cut", "date", "dirname", "grep", "mkdir", "paste",
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
        # The box's own /usr/local, /etc and so on: what install.sh would have installed.
        self.root = tmp_path / "root"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "calls.log"
        self.home.mkdir()
        self.bin.mkdir()
        self.log.touch()

        for tool in CORE_UTILS:
            found = shutil.which(tool)
            assert found, f"host lacks {tool}, needed to run deploy/provision.sh in tests"
            (self.bin / tool).symlink_to(found)
        # cmp is how --check compares what install.sh installed. Every system path it is given
        # (anything outside this box) is read from the box's fake root, never from the host.
        real_cmp = shutil.which("cmp")
        assert real_cmp, "host lacks cmp, needed to run deploy/provision.sh in tests"
        _stub(self.bin / "cmp",
              'args=()\n'
              'for a in "$@"; do\n'
              '  case "$a" in\n'
              f'    "{tmp_path}"/*) args+=("$a") ;;\n'
              f'    /*) args+=("{self.root}$a") ;;\n'
              '    *) args+=("$a") ;;\n'
              '  esac\n'
              'done\n'
              f'exec "{real_cmp}" "${{args[@]}}"')

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


# What install.sh installs, as (checkout copy under deploy/, installed path). A path under ~ is
# the box's own HOME; an absolute one lives under the box's fake system root.
INSTALLED = (
    ("kiosk/sigint101.service", "~/.config/systemd/user/sigint101.service"),
    ("kiosk/start-kiosk.sh", "/usr/local/bin/sigint101-course"),
    ("kiosk/sigint101.desktop", "/usr/local/share/applications/sigint101.desktop"),
    ("kiosk/sigint101.svg", "/usr/local/share/icons/hicolor/scalable/apps/sigint101.svg"),
    ("kiosk/sigint101-autostart.desktop", "~/.config/autostart/sigint101.desktop"),
    ("kiosk/sigint101-link.desktop", "~/Desktop/sigint101.desktop"),
    ("system/journald-sigint101.conf", "/etc/systemd/journald.conf.d/50-sigint101.conf"),
)


def _install_tree(box, differ=()):
    """Put the checkout's install.sh pieces in the fake checkout and an installed copy of each in
    the box: identical, unless its checkout name is in `differ`."""
    for src, dest in INSTALLED:
        body = (REPO / "deploy" / src).read_bytes()
        checkout = box.home / "sigint-101" / "deploy" / src
        checkout.parent.mkdir(parents=True, exist_ok=True)
        checkout.write_bytes(body)
        installed = (box.home / dest[2:]) if dest.startswith("~/") else box.root / dest[1:]
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes(body + b"# an older copy\n" if src in differ else body)


def _install_line(result):
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [ln for ln in result.stdout.splitlines() if "deploy/install.sh" in ln]
    assert len(lines) == 2, lines  # the step heading and its one verdict
    return lines[1]


@pytest.mark.parametrize("others", ["none-installed", "others-match", "others-differ"])
def test_check_reports_an_installed_course_launcher_that_differs_from_the_checkout(box, others):
    """A box keeps running its installed launcher until install.sh copies the new one over, so a
    launcher fix (the full-panel window bounds) reaches the panel only if --check flags it. It
    reads the box's installed tree, never the host's, whatever else that tree holds."""
    if others == "none-installed":
        kiosk = box.home / "sigint-101" / "deploy" / "kiosk"
        kiosk.mkdir()
        (kiosk / "start-kiosk.sh").write_text((REPO / "deploy" / "kiosk" / "start-kiosk.sh")
                                              .read_text(encoding="utf-8"), encoding="utf-8")
    else:
        rest = {src for src, _ in INSTALLED} if others == "others-differ" else set()
        _install_tree(box, differ=rest | {"kiosk/start-kiosk.sh"})
    result = box.run("--check")
    [line] = [ln for ln in result.stdout.splitlines() if "run deploy/install.sh" in ln]
    assert line.startswith("  would  "), line
    assert "course launcher" in line, line
    assert "install.sh" not in box.calls(), "--check ran install.sh"


def test_check_does_not_flag_an_installed_course_launcher_that_matches_the_checkout(box):
    """The other half of the claim: a launcher that matches is not reported, even when the host
    the suite runs on has none installed, or an older one."""
    _install_tree(box, differ={"kiosk/sigint101.svg"})
    line = _install_line(box.run("--check"))
    assert "course launcher" not in line, line
    assert "icon" in line, line


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


# --- step_power (safe-shutdown watcher provisioning, slice 2a) ------------------


POWER_SOURCE_FILES = (
    "sigint101/__init__.py",
    "sigint101/battery.py",
    "sigint101/power.py",
    "sigint101/power_desktop.py",
    "deploy/system/sigint101-power.service",
    "deploy/system/sigint101-power",
    "deploy/system/sigint101-power-notice.service",
    "deploy/system/sigint101-power-notice",
    "deploy/system/50-sigint101-power.rules",
    "deploy/system/sigint101-power.sysusers.conf",
)


def _setup_power_box(
    box: ProvisionBox,
    tmp_path: Path,
    *,
    i2c_group: str | None = "i2c",
    i2c_gid: int = 998,
    has_i2c_group: bool = True,
    unit_state: str = "enabled",
    notice_unit_state: str = "enabled",
    user_exists: bool = False,
    fail_systemctl_actions: tuple[str, ...] = (),
):
    """Populate a sysroot-backed ProvisionBox with the real power watcher source files."""
    sysroot = tmp_path / "sysroot"
    sysroot.mkdir(exist_ok=True)
    course = box.home / "sigint-101"
    for rel in POWER_SOURCE_FILES:
        src = REPO / rel
        dst = course / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        if os.access(src, os.X_OK):
            dst.chmod(0o755)

    real_cmp = shutil.which("cmp")
    real_install = shutil.which("install")
    assert real_cmp and real_install
    (box.bin / "cmp").unlink()
    _stub(
        box.bin / "cmp",
        'args=()\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        f'    /usr/local/*|/etc/*|/var/lib/*) args+=("{sysroot}$a") ;;\n'
        '    *) args+=("$a") ;;\n'
        '  esac\n'
        'done\n'
        f'exec "{real_cmp}" "${{args[@]}}"\n',
    )
    user_flag = tmp_path / "power-user-created"
    if user_exists:
        user_flag.touch()
    _stub(
        box.bin / "sudo",
        f'echo "sudo $*" >> "{box.log}"\n'
        'cmd="$1"; shift\n'
        'case "$cmd" in\n'
        '  install)\n'
        '    args=()\n'
        '    for a in "$@"; do\n'
        '      case "$a" in\n'
        f'        /usr/local/*|/etc/*|/var/lib/*) args+=("{sysroot}$a") ;;\n'
        '        *) args+=("$a") ;;\n'
        '      esac\n'
        '    done\n'
        f'    exec "{real_install}" "${{args[@]}}"\n'
        '    ;;\n'
        '  systemd-sysusers|useradd)\n'
        f'    touch "{user_flag}"\n'
        '    exit 0 ;;\n'
        '  systemctl)\n'
        f'    exec "{box.bin}/systemctl" "$@"\n'
        '    ;;\n'
        '  tee) exec cat >/dev/null ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n',
    )
    fail_cases = "\n".join(
        f'  "{act}") exit 1 ;;' for act in fail_systemctl_actions
    )
    _stub(
        box.bin / "systemctl",
        f'echo "systemctl $*" >> "{box.log}"\n'
        'case "$*" in\n'
        '  "get-default") echo graphical.target; exit 0 ;;\n'
        '  "is-enabled sigint101-power.service")\n'
        f'    echo "{unit_state}"\n'
        f'    [ "{unit_state}" = "enabled" ] ;;\n'
        '  "--user is-enabled sigint101-power-notice.service")\n'
        f'    echo "{notice_unit_state}"\n'
        f'    [ "{notice_unit_state}" = "enabled" ] ;;\n'
        f'{fail_cases}\n'
        '  *) exit 0 ;;\n'
        'esac\n',
    )
    if i2c_group is None:
        _stub(box.bin / "stat", 'echo "stat: cannot stat /dev/i2c-1" >&2; exit 1')
    else:
        _stub(
            box.bin / "stat",
            'if [ "$*" = "-c %G /dev/i2c-1" ]; then\n'
            f'  echo "{i2c_group}"; exit 0\n'
            'fi\n'
            'exit 1',
        )
    i2c_group_line = (
        'if [ "$1" = "group" ] && [ "$2" = "i2c" ]; then echo "i2c:x:998:"; exit 0; fi\n'
        if has_i2c_group
        else ""
    )
    _stub(
        box.bin / "getent",
        'if [ "$1" = "passwd" ] && [ "$2" = "sigint101-power" ]; then\n'
        f'  if [ -f "{user_flag}" ]; then\n'
        '    echo "sigint101-power:x:997:997::/nonexistent:/usr/sbin/nologin"\n'
        '    exit 0\n'
        '  fi\n'
        '  exit 2\n'
        'fi\n'
        f'{i2c_group_line}'
        f'if [ "$1" = "group" ] && [ "$2" = "{i2c_group or ""}" ]; then\n'
        f'  echo "{i2c_group}:x:{i2c_gid}:"; exit 0\n'
        'fi\n'
        'exit 2',
    )
    for extra in ("install", "mktemp", "touch"):
        found = shutil.which(extra)
        assert found
        if not (box.bin / extra).exists():
            (box.bin / extra).symlink_to(found)
    return sysroot


def _power_section(stdout: str) -> str:
    found = re.search(
        r"^==> Low-battery power watcher[^\n]*\n(.*?)(?=^==> |\Z)",
        stdout,
        re.MULTILINE | re.DOTALL,
    )
    assert found, f"no power watcher section in output:\n{stdout}"
    return found.group(1)


def test_step_power_installs_files_is_idempotent_and_preserves_power_conf(box, tmp_path):
    sysroot = _setup_power_box(box, tmp_path)

    # --check writes nothing.
    chk = box.run("--check")
    assert chk.returncode == 0, chk.stdout + chk.stderr
    assert "  would  " in _power_section(chk.stdout)
    assert not (sysroot / "usr/local/lib/systemd/system/sigint101-power.service").exists()
    assert not (sysroot / "etc/systemd/system/sigint101-power.service").exists()
    assert not (sysroot / "etc/sigint101/power.conf").exists()

    # First apply installs all 7 files (unit under /usr/local/lib/systemd/system so /etc stays
    # free for systemctl mask), creates user, enables unit, and never creates power.conf.
    first = box.run()
    assert first.returncode == 0, first.stdout + first.stderr
    expected_installed = [
        "usr/local/lib/systemd/system/sigint101-power.service",
        "usr/local/bin/sigint101-power",
        "usr/local/bin/sigint101-power-notice",
        "usr/local/lib/sigint101-power/sigint101/__init__.py",
        "usr/local/lib/sigint101-power/sigint101/battery.py",
        "usr/local/lib/sigint101-power/sigint101/power.py",
        "usr/local/lib/sigint101-power/sigint101/power_desktop.py",
        "etc/polkit-1/rules.d/50-sigint101-power.rules",
        "usr/local/lib/sysusers.d/sigint101-power.conf",
    ]
    for rel in expected_installed:
        assert (sysroot / rel).is_file(), f"missing installed {rel}"
    assert (box.home / ".config/systemd/user/sigint101-power-notice.service").is_file()
    assert not (sysroot / "etc/systemd/system/sigint101-power.service").exists()
    assert not (sysroot / "etc/sigint101/power.conf").exists()
    assert "enable --now sigint101-power.service" in box.calls()
    assert "--user enable --now sigint101-power-notice.service" in box.calls()

    # Plant an operator power.conf and confirm a second run makes 0 changes and preserves it.
    conf = sysroot / "etc/sigint101/power.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("SIGINT101_POWER_THRESHOLD_V=9.50\n", encoding="utf-8")
    second = box.run()
    assert second.returncode == 0, second.stdout + second.stderr
    sec2 = _power_section(second.stdout)
    assert "  did    " not in sec2 and "  would  " not in sec2
    assert conf.read_text(encoding="utf-8") == "SIGINT101_POWER_THRESHOLD_V=9.50\n"


def test_step_power_group_dropin_resets_list_refuses_root_and_warns_when_missing(box, tmp_path):
    # (i) Non-system group i2cdev (GID >= 100) writes reset+add drop-in.
    sysroot = _setup_power_box(box, tmp_path, i2c_group="i2cdev", i2c_gid=120)
    res = box.run()
    assert res.returncode == 0, res.stdout + res.stderr
    dropin = sysroot / "etc/systemd/system/sigint101-power.service.d/10-i2c-group.conf"
    assert dropin.is_file()
    assert dropin.read_text(encoding="utf-8") == (
        "[Service]\nSupplementaryGroups=\nSupplementaryGroups=i2cdev\n"
    )

    # (ii) root (or GID < 100) warns and writes no drop-in.
    for grp, gid in [("root", 0), ("staff", 50)]:
        d2 = tmp_path / f"grp-{grp}"
        d2.mkdir()
        b2 = ProvisionBox(d2)
        s2 = _setup_power_box(b2, d2, i2c_group=grp, i2c_gid=gid)
        r2 = b2.run()
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert "  WARN   " in _power_section(r2.stdout)
        assert not (s2 / "etc/systemd/system/sigint101-power.service.d/10-i2c-group.conf").exists()

    # (iii) /dev/i2c-1 missing warns and writes no drop-in.
    d3 = tmp_path / "missing-i2c"
    d3.mkdir()
    b3 = ProvisionBox(d3)
    s3 = _setup_power_box(b3, d3, i2c_group=None)
    r3 = b3.run()
    assert r3.returncode == 0, r3.stdout + r3.stderr
    sec3 = _power_section(r3.stdout)
    assert "  WARN   " in sec3 and "I2C not enabled" in sec3
    assert not (s3 / "etc/systemd/system/sigint101-power.service.d/10-i2c-group.conf").exists()


def test_step_power_skips_older_checkout_and_managed_updater_and_respects_owner_state(
    box, tmp_path
):
    # (a) Older/partial checkout (default ProvisionBox has no deploy/system/ or power.py):
    # warns and continues.
    for args in ((), ("--check",)):
        res = box.run(*args)
        assert res.returncode == 0, res.stdout + res.stderr
        assert "  WARN   " in _power_section(res.stdout)
        assert "Hand-over hygiene" in res.stdout

    # (b) Any non-enabled unit state on an already-installed unit skips and never re-enables.
    for state in ("disabled", "masked", "masked-runtime", "static"):
        d_st = tmp_path / f"st-{state}"
        d_st.mkdir()
        b_st = ProvisionBox(d_st)
        s_st = _setup_power_box(b_st, d_st, unit_state=state, user_exists=True)
        # Pre-install unit so it's not first install.
        unit_dst = s_st / "usr/local/lib/systemd/system/sigint101-power.service"
        unit_dst.parent.mkdir(parents=True, exist_ok=True)
        unit_dst.write_text("[Unit]\nDescription=old\n", encoding="utf-8")
        for args in (("--check",), ()):
            r_st = b_st.run(*args)
            assert r_st.returncode == 0, r_st.stdout + r_st.stderr
            sec = _power_section(r_st.stdout)
            assert "turned off by the owner" in sec and state in sec
            assert "enable --now sigint101-power.service" not in b_st.calls()
            assert "restart sigint101-power.service" not in b_st.calls()

    # (c) Managed updater enrollment markers skip with WARN and install nothing.
    for marker in ("current.json", "control.json"):
        d_m = tmp_path / f"mgr-{marker}"
        d_m.mkdir()
        b_m = ProvisionBox(d_m)
        s_m = _setup_power_box(b_m, d_m)
        updater = b_m.home / ".local/state/sigint101-updater"
        updater.mkdir(parents=True, exist_ok=True)
        (updater / marker).write_text("{}\n", encoding="utf-8")
        for args in (("--check",), ()):
            r_m = b_m.run(*args)
            assert r_m.returncode == 0, r_m.stdout + r_m.stderr
            sec = _power_section(r_m.stdout)
            assert "  WARN   " in sec and "managed box" in sec
            assert not (s_m / "usr/local/lib/systemd/system/sigint101-power.service").exists()


def test_step_power_warns_and_continues_when_unit_start_or_restart_fails(box, tmp_path):
    # F1: a failed enable --now or restart must warn and let provision.sh finish later steps.
    sysroot = _setup_power_box(
        box,
        tmp_path,
        fail_systemctl_actions=(
            "enable --now sigint101-power.service",
            "restart sigint101-power.service",
        ),
    )
    first = box.run()
    assert first.returncode == 0, first.stdout + first.stderr
    sec1 = _power_section(first.stdout)
    assert "  WARN   " in sec1 and "did not start" in sec1
    assert "Meshtastic LoRa defaults" in first.stdout
    assert "Stock Recon pieces" in first.stdout
    assert "Hand-over hygiene" in first.stdout

    # Mutate an installed file so the second run tries to restart; must also warn and continue.
    installed_power = sysroot / "usr/local/lib/sigint101-power/sigint101/power.py"
    installed_power.write_text("# stale\n", encoding="utf-8")
    second = box.run()
    assert second.returncode == 0, second.stdout + second.stderr
    sec2 = _power_section(second.stdout)
    assert "  WARN   " in sec2 and "did not start" in sec2
    assert "Hand-over hygiene" in second.stdout


def test_step_power_writes_reset_only_dropin_when_i2c_group_is_absent(box, tmp_path):
    # F1: when a box lacks the i2c group (and /dev/i2c-1 is missing or refused), write a
    # reset-only drop-in so SupplementaryGroups=i2c never fails unit startup with 216/GROUP.
    for case, i2c_group, i2c_gid in [("missing", None, 0), ("root", "root", 0)]:
        d = tmp_path / f"no-i2c-{case}"
        d.mkdir()
        b = ProvisionBox(d)
        s = _setup_power_box(
            b, d, i2c_group=i2c_group, i2c_gid=i2c_gid, has_i2c_group=False
        )
        res = b.run()
        assert res.returncode == 0, res.stdout + res.stderr
        dropin = s / "etc/systemd/system/sigint101-power.service.d/10-i2c-group.conf"
        assert dropin.is_file(), f"missing reset-only drop-in for {case}"
        assert dropin.read_text(encoding="utf-8") == "[Service]\nSupplementaryGroups=\n"


def test_step_power_preserves_masked_unit_symlink_without_overwriting(box, tmp_path):
    # F2: unit installs under /usr/local/lib/systemd/system so an owner mask symlink in
    # /etc/systemd/system works and is never overwritten, and a masked unit is skipped before
    # any file install runs.
    sysroot = _setup_power_box(box, tmp_path, unit_state="masked", user_exists=True)
    mask_target = tmp_path / "dev-null-standin"
    mask_target.write_text("", encoding="utf-8")
    etc_mask = sysroot / "etc/systemd/system/sigint101-power.service"
    etc_mask.parent.mkdir(parents=True, exist_ok=True)
    etc_mask.symlink_to(mask_target)

    # Also plant a pre-existing vendor unit and prove masked skips before overwriting it.
    vendor_unit = sysroot / "usr/local/lib/systemd/system/sigint101-power.service"
    vendor_unit.parent.mkdir(parents=True, exist_ok=True)
    vendor_unit.write_text("# pre-existing\n", encoding="utf-8")

    res = box.run()
    assert res.returncode == 0, res.stdout + res.stderr
    sec = _power_section(res.stdout)
    assert "turned off by the owner" in sec and "masked" in sec
    assert etc_mask.is_symlink() and etc_mask.resolve() == mask_target.resolve()
    assert vendor_unit.read_text(encoding="utf-8") == "# pre-existing\n"
    assert not (sysroot / "usr/local/bin/sigint101-power").exists()


def test_step_power_respects_disabled_or_masked_desktop_notice_user_unit(box, tmp_path):
    for state in ("disabled", "masked"):
        d = tmp_path / f"notice-{state}"
        d.mkdir()
        b = ProvisionBox(d)
        _setup_power_box(b, d, notice_unit_state=state, user_exists=True)
        user_unit = b.home / ".config/systemd/user/sigint101-power-notice.service"
        user_unit.parent.mkdir(parents=True, exist_ok=True)
        user_unit.write_text("# owner copy\n", encoding="utf-8")
        res = b.run()
        assert res.returncode == 0, res.stdout + res.stderr
        sec = _power_section(res.stdout)
        assert "sigint101-power-notice.service turned off by the owner" in sec
        assert state in sec
        assert "--user enable --now sigint101-power-notice.service" not in b.calls()
        assert "--user restart sigint101-power-notice.service" not in b.calls()
        if state == "masked":
            assert user_unit.read_text(encoding="utf-8") == "# owner copy\n"


def test_f5_failed_first_notice_enable_retries_on_next_run_without_blaming_owner(box, tmp_path):
    """F5: a refused first enable of sigint101-power-notice.service records a pending install and
    retries on the next run rather than mistaking it for an owner disable."""
    _setup_power_box(
        box,
        tmp_path,
        notice_unit_state="disabled",
        fail_systemctl_actions=("--user enable --now sigint101-power-notice.service",),
    )
    first = box.run()
    assert first.returncode == 0, first.stdout + first.stderr
    sec1 = _power_section(first.stdout)
    assert "  WARN   " in sec1 and "sigint101-power-notice.service" in sec1
    assert "Hand-over hygiene" in first.stdout

    # Restore enable on the next run (unit is still reported disabled until enable succeeds).
    _setup_power_box(
        box,
        tmp_path,
        notice_unit_state="disabled",
        user_exists=True,
        fail_systemctl_actions=(),
    )
    box.log.write_text("", encoding="utf-8")
    second = box.run()
    assert second.returncode == 0, second.stdout + second.stderr
    sec2 = _power_section(second.stdout)
    assert "--user enable --now sigint101-power-notice.service" in box.calls()
    assert "turned off by the owner" not in sec2
    assert "enable and start sigint101-power-notice.service" in sec2

    # Once the initial enable succeeded, a later disabled state IS attributed to the owner.
    box.log.write_text("", encoding="utf-8")
    third = box.run()
    assert third.returncode == 0, third.stdout + third.stderr
    sec3 = _power_section(third.stdout)
    assert "sigint101-power-notice.service turned off by the owner" in sec3
    assert "--user enable --now sigint101-power-notice.service" not in box.calls()


def test_f6_refused_user_daemon_reload_on_disabled_update_warns_and_continues(box, tmp_path):
    """F6: updating a disabled user unit when `systemctl --user daemon-reload` fails warns and
    lets the rest of provision.sh finish."""
    _setup_power_box(
        box,
        tmp_path,
        notice_unit_state="disabled",
        user_exists=True,
        fail_systemctl_actions=("--user daemon-reload",),
    )
    user_unit = box.home / ".config/systemd/user/sigint101-power-notice.service"
    user_unit.parent.mkdir(parents=True, exist_ok=True)
    user_unit.write_text("# older unit copy\n", encoding="utf-8")

    res = box.run()
    assert res.returncode == 0, res.stdout + res.stderr
    sec = _power_section(res.stdout)
    assert "  WARN   " in sec and "daemon-reload" in sec
    assert "Meshtastic LoRa defaults" in res.stdout
    assert "Stock Recon pieces" in res.stdout
    assert "Hand-over hygiene" in res.stdout
