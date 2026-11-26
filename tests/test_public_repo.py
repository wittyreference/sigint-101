# ABOUTME: Guards the public repo: no git-tracked file may hold a tailnet address, a home path, a
# ABOUTME: real device's address, or the owner's host or plan names. A finding says where, not what.

import hashlib
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# A dotted quad standing on its own, not a run inside a longer dotted number such as a five-part
# version. A full stop may still follow it, as at the end of a sentence.
DOTTED_QUAD = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\.?\d)")

# An absolute path into one person's home directory: /home/<user> (inside a WSL UNC path too),
# macOS /Users/<user>, and Windows <drive>:\Users\<user> with either slash, which also covers
# WSL's /mnt/c/Users/<user>. A placeholder such as <user>, $USER or {user} names nobody, so it
# does not match.
HOME_PATH = re.compile(r"(?:[\\/]home|[\\/]Users|\b[A-Za-z]:[\\/]+(?i:users))[\\/]+"
                       r"(?P<user>[A-Za-z0-9._-]+)")

# Made-up users whose home paths are test data, not anyone's machine:
#   learner - the kiosk account tests/test_kiosk.py builds its Chromium-profile fixtures on.
FIXTURE_USERS = frozenset({"learner"})

# The owner's name is not checked here: this file is public, so a test that searched for the
# name would have to contain it, which is the very leak it would guard against.

# A hardware (MAC) address written out: six two-digit hex octets joined by colons or by hyphens,
# one kind throughout, and not part of a longer run of octets.
MAC_TEXT = re.compile(r"(?<![0-9A-Fa-f])(?<![0-9A-Fa-f][:-])[0-9A-Fa-f]{2}([:-])"
                      r"(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?!\1?[0-9A-Fa-f])")

# Vendor prefixes (OUIs) that tests write whole addresses under on purpose, each time with a
# made-up device part. A real device behind one of these prefixes is what this guard lets by.
#   b4:1e:52 - Flock Safety's own IEEE block, which the Flock detector matches on: its positive
#              cases build camera frames under it and write the address they expect out
#              (tests/test_flock.py, tests/flock.test.js, tests/test_app.py,
#              tests/test_reception_hooks.py).
ALLOWED_VENDOR_PREFIXES = frozenset({"b4:1e:52"})

# A capture is read when it opens with a classic pcap magic number (either byte order, micro- or
# nanosecond stamps) and carries 802.11: linktype 105 (bare) or 127 (behind a radiotap header).
PCAP_BYTE_ORDER = {bytes.fromhex("d4c3b2a1"): "<", bytes.fromhex("a1b2c3d4"): ">",
                   bytes.fromhex("4d3cb2a1"): "<", bytes.fromhex("a1b23c4d"): ">"}
PCAPNG_MAGIC = bytes.fromhex("0a0d0d0a")
LINKTYPE_802_11 = 105
LINKTYPE_RADIOTAP = 127

# What the hardware-address guard cannot see, so a person still has to look:
#   - network names (SSIDs), device names and sensor IDs: free text with no shape to test for;
#   - a real device's address behind an allowed vendor prefix;
#   - an address written another way: without separators, in dotted groups of four digits, or
#     inside a longer run of octets;
#   - an address built in code: a byte list, \x escapes, or a vendor prefix joined to a device
#     part, which is how the tests themselves write them;
#   - inside a capture, anything but the 802.11 address fields: the names and addresses that
#     information elements carry (Wi-Fi Direct, WPS, vendor elements) are not read;
#   - an address in any other binary file: a pcapng or a pcap of another link type is reported
#     as unreadable, and every other binary format is not read at all;
#   - git history: like the guards above, it reads the checked-out tree only.

# The owner's machines and private plans, by name. His history holds them, his tree must not. This
# file is public and cannot hold a name, even split into parts, so it holds the SHA-256 of each, in
# lower case, and each word of a tracked file, or a run of words joined by - or _, is hashed and
# compared. Only names already in the repository's history are listed: a digest of a short name is
# easy to reverse, so listing a name that was never published would publish it.
PRIVATE_NAME_DIGESTS = frozenset({
    # the reference Recon's name, written with a hyphen and as one word, and its host name
    "f2d6d65037ccef21e0587e2f3703f06a8232bc2c8d9b3554504d2974b4b8362c",
    "071e75ab14d07ac59a0843becba7df78da35cb68bc7a50f68c7a5795f4570211",
    "8f1a5687a544653f0041f3646266aceae2e756ab861ddb60d867bb387a94e50f",
    # the second Recon's host name, which is also the login user on both Recons
    "f5e490b5eaf7c3c5ffd36b16c5e459e965b8a96b22ead6e86a0c54728ad9127c",
    # a lab Pi's host name, and the droplet's
    "53a7ec63ba41c8d283a99da30e48a150155ab63ada966535147e2f39359fce18",
    "f2e0cb61d7cc17f8411aa030d6f3d551812656778da67b2233ba534b71fba9ec",
    # the file names of two private plans, without their .md
    "f8602ae151c4e0f55718ce8da2f6adb3b9e56a2f575e5506d5626e4aff1bc83d",
    "16d68eea9054a6d26c66990b2c5853648e9ceff98e3f26ccbf0cf6eed64e71bb",
})
# The longest listed name, in words. A run of words inside a longer one is compared too, so a file
# name that wraps a host name in other words is still found.
PRIVATE_NAME_MAX_WORDS = 3
WORD_RUN = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")

# What the name guard cannot see, so a person still has to look:
#   - a name written another way: split by a space, a dot or a slash, misspelt, or with letters
#     swapped for look-alikes;
#   - a machine or plan the list does not hold: a new host's name has to be added as a digest;
#   - the owner's own name (see above), user names other than the one listed, and binary files;
#   - git history: it reads the checked-out tree only.


def tailnet_address_lines(text: str) -> list:
    """The 1-based numbers of the lines in `text` that hold a tailnet address.

    A tailnet hands out IPv4 addresses from RFC 6598's shared address space: first octet 100,
    second octet 64 to 127. A quad with an octet past 255 is no address at all.
    """
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        for match in DOTTED_QUAD.finditer(line):
            octets = [int(part) for part in match.groups()]
            if octets[0] == 100 and 64 <= octets[1] <= 127 and max(octets) <= 255:
                found.append(number)
                break
    return found


def foreign_home_lines(text: str) -> list:
    """The 1-based numbers of the lines in `text` that hold a path into a home whose user is not
    in FIXTURE_USERS. A full stop after the user name is read as the end of a sentence."""
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        users = [match.group("user").rstrip(".") for match in HOME_PATH.finditer(line)]
        if any(user and user not in FIXTURE_USERS for user in users):
            found.append(number)
    return found


def names_real_hardware(octets: bytes) -> bool:
    """True when six address octets name one real device: a unicast address (the group bit, 0x01
    of the first octet, clear) that is globally administered (0x02 clear, so a maker gave it to
    one device), other than the all-zero placeholder and the allowed vendor prefixes. A locally
    administered address, as phones randomise and tests make up, names no particular device."""
    return (not octets[0] & 0x03 and any(octets)
            and octets[:3].hex(":") not in ALLOWED_VENDOR_PREFIXES)


def real_hardware_address_lines(text: str) -> list:
    """The 1-based numbers of the lines in `text` that write out a real device's address."""
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        if any(names_real_hardware(bytes.fromhex(re.sub("[:-]", "", match.group())))
               for match in MAC_TEXT.finditer(line)):
            found.append(number)
    return found


def names_a_private_machine_or_plan(text: str, digests=PRIVATE_NAME_DIGESTS) -> bool:
    """True when a word of `text`, or a run of up to PRIVATE_NAME_MAX_WORDS words joined by - or
    _ inside a longer run, is one of the names behind `digests`, in any case. A run is compared
    with its words joined by -, so an underscore spelling of a hyphenated name is found too."""
    for run in WORD_RUN.findall(text.lower()):
        words = re.split("[-_]", run)
        for start in range(len(words)):
            for end in range(start + 1, min(start + PRIVATE_NAME_MAX_WORDS, len(words)) + 1):
                if hashlib.sha256("-".join(words[start:end]).encode()).hexdigest() in digests:
                    return True
    return False


def private_name_lines(text: str, digests=PRIVATE_NAME_DIGESTS) -> list:
    """The 1-based numbers of the lines in `text` that name the owner's machines or plans."""
    return [number for number, line in enumerate(text.split("\n"), 1)
            if names_a_private_machine_or_plan(line, digests)]


def private_name_findings(repo: Path = REPO, digests=PRIVATE_NAME_DIGESTS) -> list:
    """`path (in its name)` for a tracked file whose path names one, and `path:line` for each line
    of a tracked text file that does. Like `findings`, it says where, never which name."""
    found = [f"{path} (in its name)" for path, _data in tracked_blobs(repo)
             if names_a_private_machine_or_plan(path, digests)]
    return found + [f"{path}:{number}" for path, text in tracked_text_files(repo)
                    for number in private_name_lines(text, digests)]


def dot11_addresses(frame: bytes) -> list:
    """The address fields of an 802.11 frame, as many as its type carries: three for management;
    three for data, four between distribution systems; for control, the receiver, and the
    transmitter too unless it is a CTS or an ACK. A field the capture cut off is not read."""
    if len(frame) < 2:
        return []
    kind, subtype = (frame[0] >> 2) & 0x3, frame[0] >> 4
    offsets = [4]
    if kind in (0, 2) or (kind == 1 and subtype not in (12, 13)):
        offsets.append(10)
    if kind in (0, 2):
        offsets.append(16)
    if kind == 2 and frame[1] & 0x3 == 0x3:
        offsets.append(24)
    return [frame[at:at + 6] for at in offsets if at + 6 <= len(frame)]


def capture_frames_naming_real_hardware(data: bytes) -> list:
    """The 1-based numbers of the frames in a classic pcap whose address fields name a real
    device. A capture it cannot read raises ValueError, so that it is reported, not passed."""
    order = PCAP_BYTE_ORDER.get(data[:4])
    if order is None:
        raise ValueError("not a classic pcap")
    if len(data) < 24:
        raise ValueError("cut short in its header")
    linktype = struct.unpack(order + "I", data[20:24])[0] & 0xFFFF
    if linktype not in (LINKTYPE_802_11, LINKTYPE_RADIOTAP):
        raise ValueError(f"linktype {linktype} is not 802.11")
    found, at, number = [], 24, 0
    while at + 16 <= len(data):
        number += 1
        caplen = struct.unpack(order + "I", data[at + 8:at + 12])[0]
        frame = data[at + 16:at + 16 + caplen]
        at += 16 + caplen
        if linktype == LINKTYPE_RADIOTAP:       # radiotap states its own length, little-endian
            frame = frame[struct.unpack("<H", frame[2:4])[0]:] if len(frame) >= 4 else b""
        if any(names_real_hardware(address) for address in dot11_addresses(frame)):
            found.append(number)
    return found


def tracked_blobs(repo: Path = REPO) -> list:
    """(path, bytes) for every git-tracked file in the checkout at `repo`. A tracked symlink
    counts as the path it points to, which is what git publishes for it.

    Deliberately not a skip when git cannot list them: a guard that checked nothing would still
    report success, the same trap tests/test_js_panels.py refuses for the panel tests.
    """
    git = shutil.which("git")
    assert git, ("git is not on PATH, so the tracked files cannot be listed and this guard would "
                 "check nothing. Install git, or say plainly that the privacy guard is not run.")
    listing = subprocess.run([git, "ls-files", "-s", "-z"], cwd=repo, capture_output=True,
                             timeout=60)
    assert listing.returncode == 0, ("git ls-files failed, so this guard would check nothing. Is "
                                     "this a git checkout? "
                                     + listing.stderr.decode(errors="replace"))
    entries = [entry for entry in listing.stdout.decode("utf-8").split("\0") if entry]
    assert entries, "git lists no tracked files here, so this guard would check nothing"
    files = []
    for entry in entries:
        info, path = entry.split("\t", 1)           # "<mode> <blob> <stage>", a tab, the path
        mode, blob, _stage = info.split(" ")
        if mode == "120000":            # a symlink: its blob is the path it points to
            target = subprocess.run([git, "cat-file", "-p", blob], cwd=repo, capture_output=True,
                                    timeout=60, check=True)
            files.append((path, target.stdout))
            continue
        full = repo / path
        if not full.is_file():          # tracked but deleted in this checkout: nothing to read
            continue
        files.append((path, full.read_bytes()))
    return files


def tracked_text_files(repo: Path = REPO) -> list:
    """(path, text) for every git-tracked text file in the checkout at `repo`, a tracked symlink
    as a one-line text. A binary file, by the first-8000-bytes test git itself uses, is left out."""
    return [(path, data.decode("utf-8", errors="replace")) for path, data in tracked_blobs(repo)
            if b"\0" not in data[:8000]]


def tracked_captures(repo: Path = REPO) -> list:
    """(path, bytes) for every git-tracked file that opens as a packet capture, whatever its
    name: a classic pcap's magic number or a pcapng's."""
    return [(path, data) for path, data in tracked_blobs(repo)
            if data[:4] in PCAP_BYTE_ORDER or data[:4] == PCAPNG_MAGIC]


def findings(lines_of, repo: Path = REPO) -> list:
    """`path:line` for every line `lines_of` flags in a tracked text file of the checkout at
    `repo`. The text itself never leaves this function, so a failure prints where, not what."""
    return [f"{path}:{number}" for path, text in tracked_text_files(repo)
            for number in lines_of(text)]


def capture_findings(repo: Path = REPO) -> list:
    """`path:frame N` for the first frame of each tracked capture whose addresses name a real
    device, and how many more do. A capture this guard cannot read is a finding, never a pass."""
    found = []
    for path, data in tracked_captures(repo):
        try:
            frames = capture_frames_naming_real_hardware(data)
        except ValueError as unreadable:
            found.append(f"{path} (this guard cannot read it: {unreadable})")
            continue
        if frames:
            more = f" and {len(frames) - 1} more" if len(frames) > 1 else ""
            found.append(f"{path}:frame {frames[0]}{more}")
    return found


def test_no_tracked_file_holds_a_tailnet_address():
    found = findings(tailnet_address_lines)
    assert not found, ("tracked files hold a tailnet address; name the host instead. Where (the "
                       "addresses are withheld): " + ", ".join(found))


def test_no_tracked_file_holds_a_path_into_a_real_home():
    found = findings(foreign_home_lines)
    assert not found, ("tracked files hold a path into a real person's home directory; write "
                       "~/... or a named placeholder instead. Where (the paths are withheld): "
                       + ", ".join(found))


def test_no_tracked_file_writes_out_a_real_devices_hardware_address():
    found = findings(real_hardware_address_lines)
    assert not found, ("tracked files write out a real device's hardware address; use a locally "
                       "administered one (0x02 set in the first octet) instead. Where (the "
                       "addresses are withheld): " + ", ".join(found))


def test_no_tracked_capture_carries_a_real_devices_hardware_address():
    found = capture_findings()
    assert not found, ("tracked captures carry a real device's hardware address in their 802.11 "
                       "address fields; rewrite them with locally administered ones. Where (the "
                       "addresses are withheld): " + ", ".join(found))


def test_no_tracked_file_names_the_owners_machines_or_private_plans():
    found = private_name_findings()
    assert not found, ("tracked files name one of the owner's machines or private plans; write "
                       "what it is instead (\"the reference Recon\", \"a lab Pi\", \"the private "
                       "plan\"). Where (the names are withheld): " + ", ".join(found))


# The commit the name guard was made against: main as it stood when the item was confirmed. Its
# CLAUDE.md and provision.sh named the owner's machines and plan on these lines, and one fixture
# file under tests/fixtures/battery/ named the reference Recon in its own name.
NAMED_COMMIT = "8cb6aae3e7cfa0397a269adde0d320d58e3e0a8a"


def at_named_commit(*args) -> bytes:
    """Run a git command against NAMED_COMMIT's objects; skip when this clone does not hold it."""
    shown = subprocess.run(["git", *args], cwd=REPO, capture_output=True, timeout=60)
    if shown.returncode != 0:
        pytest.skip(f"this clone does not hold {NAMED_COMMIT[:7]} (a shallow clone?)")
    return shown.stdout


@pytest.mark.parametrize("path, lines", [
    ("CLAUDE.md", [7, 74, 91, 109, 134]),
    ("deploy/provision.sh", [34, 42, 47, 48, 65, 437]),
])
def test_the_digests_find_the_names_where_main_held_them(path, lines):
    text = at_named_commit("show", f"{NAMED_COMMIT}:{path}").decode("utf-8")
    assert private_name_lines(text) == lines


def test_the_digests_find_the_fixture_whose_file_name_held_one():
    paths = at_named_commit("ls-tree", "-r", "--name-only", NAMED_COMMIT).decode().splitlines()
    named = [path for path in paths if names_a_private_machine_or_plan(path)]
    assert len(named) == 1 and named[0].startswith("tests/fixtures/battery/")


def test_without_git_the_guard_fails_loudly(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(AssertionError, match="would check nothing"):
        tracked_text_files()


def test_outside_a_git_checkout_the_guard_fails_loudly(tmp_path):
    (tmp_path / "notes.md").write_text("anything\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="would check nothing"):
        tracked_text_files(tmp_path)


def test_a_tracked_symlink_into_a_real_home_is_found(tmp_path):
    # Git publishes a symlink as the path it points to, so a link into a home directory leaks that
    # path even when it dangles. The path is joined from parts, as the examples below are, and no
    # GIT_* variable of a calling hook may steer these commands at a real repository.
    (tmp_path / "plan.md").symlink_to("/home/" + "someone" + "/plans/plan.md")
    for command in (["git", "init", "-q"], ["git", "add", "plan.md"]):
        subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, timeout=60,
                       env={name: value for name, value in os.environ.items()
                            if not name.startswith("GIT_")})
    assert findings(foreign_home_lines, tmp_path) == ["plan.md:1"]     # where, never the path


# --- the patterns, proved on examples ---------------------------------------------------------
# Every example is joined from parts at run time, so this file never holds a whole address or a
# whole home path, and the guard reads it like any other tracked file.


def quad(*parts) -> str:
    """A dotted number joined from its parts."""
    return ".".join(str(part) for part in parts)


@pytest.mark.parametrize("line", [
    quad(100, 64, 0, 1),                                 # the first address of the block
    quad(100, 127, 255, 254),                            # the last
    f"ssh someone@{quad(100, 100, 7, 7)} 'uptime'",      # user@address
    f"--host {quad(100, 77, 1, 2)} --port 8008",
    f"run them against http://{quad(100, 81, 3, 4)}:8008/",
    f"The box answers at {quad(100, 99, 5, 6)}.",        # a full stop ends the sentence
    f"(`box`, {quad(100, 120, 9, 9)}, user `someone`)",
])
def test_a_tailnet_address_is_found(line):
    assert tailnet_address_lines(line) == [1]


@pytest.mark.parametrize("line", [
    quad(100, 63, 255, 255),                 # just below the block
    quad(100, 128, 0, 1),                    # just above it
    quad(10, 100, 64, 1),                    # a LAN address whose tail looks like the block
    quad(100, 64, 256, 1),                   # an octet past 255: no address at all
    "Chromium " + quad(100, 0, 4896, 127),   # a browser version that begins with 100
    "version " + quad(100, 64, 1),           # a three-part version
    "build " + quad(100, 64, 1, 2, 3),       # a five-part version
    quad(1, 100, 64, 0, 1),                  # an address-shaped run inside a longer number
    "dated " + quad(2026, 11, 16),           # a date
    "tune to 100.7 MHz",                     # a frequency
    "100_100_000",                           # a frequency in Hz, Python-style
])
def test_a_number_that_is_not_a_tailnet_address_is_left_alone(line):
    assert tailnet_address_lines(line) == []


@pytest.mark.parametrize("line", [
    "/home/" + "someone" + "/sigint-101",
    "cd /home/" + "someone",
    "the plan lives in /home/" + "someone" + ".",        # a full stop ends the sentence
    "/Users/" + "someone" + "/Library",
    "C:\\Users\\" + "someone" + "\\AppData",
    "C:/Users/" + "someone" + "/AppData",
    "c:\\users\\" + "someone",
    "/mnt/c/Users/" + "someone" + "/AppData",
    "\\\\wsl.localhost\\Ubuntu\\home\\" + "someone" + "\\work",
    "/home/learner/x then /home/" + "someone" + "/y",    # a real home beside a fixture one
])
def test_a_path_into_a_real_home_is_found(line):
    assert foreign_home_lines(line) == [1]


@pytest.mark.parametrize("line", [
    "/home/learner/.config/chromium-kiosk",      # the fixture user
    "HOME = Path('/home/learner')",
    "~/.claude/plans/plan.md",
    "ln -s <main checkout>/.venv <wt>/.venv",
    "/home/<user>/x",
    "/home/$USER/x",
    "/home/${USER}/x",
    "/home/{user}/x",
    "$HOME/.config/autostart",
    "/root/.ssh",
    "https://example.com/users/42",
    "C:\\Users\\<user>\\AppData",
])
def test_a_home_path_that_names_nobody_is_left_alone(line):
    assert foreign_home_lines(line) == []


def mac(*octets, separator=":") -> str:
    """A hardware address written out from its octets."""
    return separator.join(f"{octet:02x}" for octet in octets)


@pytest.mark.parametrize("line", [
    mac(0x3c, 0x5a, 0x01, 0x02, 0x03, 0x04),                            # a maker's: one device
    mac(0x3c, 0x5a, 0x01, 0x02, 0x03, 0x04).upper(),                    # either case
    mac(0x3c, 0x5a, 0x01, 0x02, 0x03, 0x04, separator="-"),             # hyphens
    f'"src": "{mac(0x00, 0x1b, 0x2c, 0x3d, 0x4e, 0x5f)}"',              # a first octet of zero
    f"the probe came from {mac(0x60, 0x11, 0x22, 0x33, 0x44, 0x55)}.",  # a full stop ends it
    mac(0xb4, 0x1e, 0x53, 0x00, 0x00, 0x01),                            # beside Flock's prefix
    f"{mac(0x02, 0, 0, 0, 4, 0x10)} then {mac(0x3c, 0x5a, 1, 2, 3, 4)}",  # after a made-up one
])
def test_a_real_devices_hardware_address_is_found(line):
    assert real_hardware_address_lines(line) == [1]


@pytest.mark.parametrize("line", [
    mac(0x02, 0x00, 0x00, 0x00, 0x04, 0x10),          # locally administered: made up
    mac(0xda, 0xa1, 0x19, 0x01, 0x02, 0x03),          # locally administered, as phones randomise
    mac(0xff, 0xff, 0xff, 0xff, 0xff, 0xff),          # broadcast
    mac(0x01, 0x00, 0x5e, 0x00, 0x00, 0xfb),          # multicast
    mac(0, 0, 0, 0, 0, 0),                            # the all-zero placeholder
    mac(0xb4, 0x1e, 0x52, 0x00, 0x00, 0x01),          # Flock's prefix, a made-up device part
    mac(0x3c, 0x5a, 0x01),                            # a vendor prefix alone
    mac(0x3c, 0x5a, 0x01, 0x02, 0x03, 0x04, 0x05),    # seven octets: no hardware address
    mac(0x3c, 0x5a) + "-" + mac(0x01, 0x02, 0x03, 0x04),   # two kinds of separator
    "dated 07:58:35",                                 # a time
    "fe80::1",                                        # an IPv6 address
    mac(0x3c, 0x5a, 0x01, 0x02, 0x03, 0x04, separator=""),  # no separators: out of its reach
])
def test_an_address_that_names_no_real_device_is_left_alone(line):
    assert real_hardware_address_lines(line) == []


def digests_of(*names) -> frozenset:
    """Digests as PRIVATE_NAME_DIGESTS holds them, here of made-up names."""
    return frozenset(hashlib.sha256(name.encode()).hexdigest() for name in names)


# Made-up names in the shapes the real ones have: a hyphenated host, the same as one word, a short
# host, and a plan's three-word file name.
MADE_UP_NAMES = digests_of("lab-box", "labbox", "kit5", "quiet-amber-otter")


@pytest.mark.parametrize("line", [
    "ssh someone@lab-box 'uptime'",
    "**lab-box** is the working target",
    "on lab-box's own USB port",                    # a possessive
    "LAB-BOX and Lab-Box",                          # any case
    "`lab_box`",                                    # an underscore for the hyphen
    "fixtures/battery/daemon-lab-box.json",         # inside a longer file name
    "exported at ~/backups/labBox-2026",            # the one-word spelling
    "a key named kit5->droplet",                    # a short host before an arrow
    "the plan (`~/.claude/plans/quiet-amber-otter.md`)",
    "research in `quiet-amber-otter-agent-ports.md`",
    "(quiet-amber-otter:241)",
])
def test_a_private_machine_or_plan_name_is_found(line):
    assert private_name_lines(line, MADE_UP_NAMES) == [1]


@pytest.mark.parametrize("line", [
    "the reference Recon is the working target",
    "lab-boxes and a lab box",                      # a longer word; a space between the words
    "38-lab-bench.yaml",                            # a lesson file that shares a word
    "kit50 and kit",                                # a longer and a shorter word
    "quiet-amber and amber-otter",                  # part of a plan's name only
])
def test_a_line_that_names_no_private_machine_or_plan_is_left_alone(line):
    assert private_name_lines(line, MADE_UP_NAMES) == []


@pytest.mark.parametrize("line", [
    "Each pin is the commit the reference Recon runs",
    "The reference Recon is the WORKING HIL target; the second Recon is the deaf one.",
    "The owner's lab boxes (a Recon for the SDR, a Pi for the Flipper)",
    "Dev runs on the droplet",
    "the owner's private plan, kept outside this repository",
    "a stock SpecFive Nomad 2 Recon on a Raspberry Pi 5 or a Pi 4",
    "lesson_packs/38-nomad-bench.yaml",                 # a lesson's file name
    "~/.claude/plans/INDEX.md",                         # the planning tools' own index
])
def test_the_generic_wording_is_not_a_private_name(line):
    assert private_name_lines(line) == []


def test_a_tracked_file_whose_name_or_text_names_a_private_machine_is_found(tmp_path):
    (tmp_path / "daemon-lab-box.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# Notes\nmeasured on lab-box today\n", encoding="utf-8")
    git_in(tmp_path, "init", "-q")
    git_in(tmp_path, "add", "daemon-lab-box.json", "notes.md")
    assert private_name_findings(tmp_path, MADE_UP_NAMES) == [
        "daemon-lab-box.json (in its name)", "notes.md:2"]               # where, never the name


# --- captures, built from parts ---------------------------------------------------------------

GLOBAL = bytes([0x3c, 0x5a, 0x01, 0x02, 0x03, 0x04])   # globally administered: a maker's device
LOCAL = bytes([0x02, 0x00, 0x00, 0x00, 0x04, 0x10])    # locally administered: made up
FLOCK = bytes([0xb4, 0x1e, 0x52, 0x00, 0x00, 0x01])    # the allowed prefix, made-up device part
BROADCAST = bytes([0xff] * 6)


def probe(transmitter, receiver=BROADCAST, bssid=BROADCAST) -> bytes:
    """An 802.11 probe request (management, subtype 4) asking for any network."""
    return bytes([0x40, 0, 0, 0]) + receiver + transmitter + bssid + bytes(2) + bytes(2)


def pcap(*frames, linktype=LINKTYPE_RADIOTAP, order="<") -> bytes:
    """A classic pcap of the frames; under linktype 127 each sits behind an 8-byte radiotap."""
    out = struct.pack(order + "IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 512, linktype)
    for frame in frames:
        if linktype == LINKTYPE_RADIOTAP:
            frame = struct.pack("<BBHI", 0, 0, 8, 0) + frame
        out += struct.pack(order + "IIII", 0, 0, len(frame), len(frame)) + frame
    return out


def git_in(repo: Path, *args) -> None:
    """Run git in a scratch repository, never steered at a real one by a calling hook's GIT_*."""
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=60,
                   env={name: value for name, value in os.environ.items()
                        if not name.startswith("GIT_")})


def test_a_capture_names_each_frame_whose_addresses_name_a_real_device():
    data = pcap(probe(LOCAL), probe(GLOBAL), probe(FLOCK), probe(LOCAL, receiver=GLOBAL),
                probe(LOCAL, bssid=GLOBAL))
    assert capture_frames_naming_real_hardware(data) == [2, 4, 5]


def test_bare_802_11_and_big_endian_captures_are_read_too():
    assert capture_frames_naming_real_hardware(pcap(probe(GLOBAL), linktype=105)) == [1]
    assert capture_frames_naming_real_hardware(pcap(probe(GLOBAL), order=">")) == [1]


def test_a_data_frame_between_distribution_systems_carries_a_fourth_address():
    wds = bytes([0x08, 0x03, 0, 0]) + LOCAL + LOCAL + LOCAL + bytes(2) + GLOBAL
    assert capture_frames_naming_real_hardware(pcap(wds)) == [1]


def test_a_control_frame_is_read_only_as_far_as_its_kind_has_addresses():
    # An ACK names its receiver alone, so the bytes after it are no transmitter; an RTS has both.
    ack = bytes([0xd4, 0, 0, 0]) + LOCAL + GLOBAL
    rts = bytes([0xb4, 0, 0, 0]) + LOCAL + GLOBAL
    assert capture_frames_naming_real_hardware(pcap(ack, rts)) == [2]


def test_a_tracked_capture_is_found_by_its_content_not_its_name(tmp_path):
    (tmp_path / "air.bin").write_bytes(pcap(probe(LOCAL), probe(GLOBAL), probe(GLOBAL)))
    (tmp_path / "quiet.pcap").write_bytes(pcap(probe(LOCAL)))
    git_in(tmp_path, "init", "-q")
    git_in(tmp_path, "add", "air.bin", "quiet.pcap")
    assert capture_findings(tmp_path) == ["air.bin:frame 2 and 1 more"]     # where, never what


@pytest.mark.parametrize("data, reason", [
    (pcap(probe(GLOBAL), linktype=1), "linktype 1 is not 802.11"),
    (PCAPNG_MAGIC + bytes(28), "not a classic pcap"),
])
def test_a_capture_this_guard_cannot_read_is_a_finding_not_a_pass(tmp_path, data, reason):
    (tmp_path / "air.cap").write_bytes(data)
    git_in(tmp_path, "init", "-q")
    git_in(tmp_path, "add", "air.cap")
    assert capture_findings(tmp_path) == [f"air.cap (this guard cannot read it: {reason})"]


def test_the_guard_does_not_trip_on_its_own_source():
    source = Path(__file__).read_text(encoding="utf-8")
    assert tailnet_address_lines(source) == []
    assert foreign_home_lines(source) == []
    assert real_hardware_address_lines(source) == []
    assert private_name_lines(source) == []
