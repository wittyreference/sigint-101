# ABOUTME: Tests SSTV's owned staging: each decoder stages in its own subfolder under a held flock.
# ABOUTME: Real flock, directories and subprocesses; only a retired owner's subfolder is ever swept.

import fcntl
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
import uuid
from pathlib import Path

import pytest

from sigint101 import childproc, receiver_lifecycle, sstv
from sigint101.hardware import RtlSdrDevice
from sigint101.sstv import SSTV, SstvDecoder, SstvUnavailable
from tests.child_fakes import LateProc
from tests.test_sstv import PNG_BYTES, _finish, _StubbornStage

REPO = Path(__file__).resolve().parent.parent
LOG = "sigint101.sstv"


def _staging(tmp_path):
    return tmp_path / ".sstv.staging"


def _gallery(tmp_path):
    return tmp_path / "sstv"


@pytest.fixture
def consoles(tmp_path):
    """Make decoders on one data folder, as separate consoles would be. Each records its runs."""
    made = []

    def console(**kw):
        launches, heard = [], []

        def spawn(freq, device, path):
            child = LateProc([SSTV, "-o", str(path)])
            launches.append((child, path))
            return child

        kw.setdefault("spawn", spawn)
        dec = SstvDecoder(radio_present=lambda: True, which=lambda name: "/usr/bin/" + name,
                          output_dir=_gallery(tmp_path), data_dir=tmp_path,
                          clock=lambda: 1735689600.0, **kw)
        dec.on_reception = lambda item, **_: heard.append(item)
        made.append((dec, launches))
        return dec, launches, heard

    yield console
    for dec, launches in made:
        for child, _ in launches:
            close = getattr(child, "close_pipe", None)
            if close is not None:
                close()
        try:
            dec.stop()
        except SstvUnavailable:
            pass


def _retired_owner(tmp_path, *names):
    """A subfolder whose owner is gone: its lock file is there and nothing holds it."""
    staging = _staging(tmp_path)
    staging.mkdir(mode=0o700, exist_ok=True)
    owner = staging / uuid.uuid4().hex
    owner.mkdir(mode=0o700)
    (owner / sstv._OWNER_LOCK).touch(mode=0o600)
    for name in names:
        (owner / name).write_bytes(PNG_BYTES)
    return owner


def _held(lock: Path) -> bool:
    """True while some open file description holds this lock file's flock."""
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _snapshot(folder: Path) -> dict:
    """Every entry under a folder: kind, inode and bytes, read without following any link."""
    seen = {}
    for root, dirs, files in os.walk(folder):
        for name in dirs + files:
            path = Path(root) / name
            info = path.lstat()
            kind = "link" if path.is_symlink() else "dir" if path.is_dir() else "file"
            body = path.read_bytes() if kind == "file" else None
            seen[str(path.relative_to(folder))] = (kind, info.st_ino, body)
    return seen


def _gallery_with_bait(tmp_path):
    """A published picture, and a folder in the gallery that looks like a retired owner."""
    gallery = _gallery(tmp_path)
    gallery.mkdir()
    (gallery / "published.png").write_bytes(PNG_BYTES)
    bait = gallery / uuid.uuid4().hex
    bait.mkdir(mode=0o700)
    (bait / sstv._OWNER_LOCK).touch(mode=0o600)
    (bait / "kept.png").write_bytes(PNG_BYTES)
    return gallery


# --- ownership -------------------------------------------------------------------------------


def test_each_decoder_stages_in_its_own_locked_subfolder(tmp_path, consoles):
    first, first_runs, _ = consoles()
    second, second_runs, _ = consoles()
    first.start()
    second.start()
    owners = [path.parent for _, path in first_runs + second_runs]
    assert owners[0] != owners[1]
    for owner in owners:
        assert owner.parent == _staging(tmp_path)
        assert len(owner.name) == 32 and int(owner.name, 16) >= 0
        assert _held(owner / sstv._OWNER_LOCK)


@pytest.mark.parametrize("entry", ["preflight", "start"])
def test_a_second_console_never_removes_a_live_capture(tmp_path, consoles, entry):
    # Codex F2: the second console's first preflight unlinked the first's staged picture.
    first, launches, heard = consoles()
    first.start()
    child, staged = launches[0]
    with open(staged, "wb") as writer:
        writer.write(PNG_BYTES)
        writer.flush()
        second, _, _ = consoles()
        if entry == "preflight":
            second.discard_start(second.prepare_start())
        else:
            second.start()
        assert staged.read_bytes() == PNG_BYTES
    _finish(child)
    first.stop()
    [item] = heard
    assert Path(item["path"]).name == staged.name
    assert (_gallery(tmp_path) / staged.name).read_bytes() == PNG_BYTES


# A console in its own process. "live" holds its staged picture open until told to finish, then
# publishes it. "abort" ends its capture with both the move into .discard and the unlink failing,
# so the picture is left only in its memory, then dies by SIGKILL as a crashed console would.
_CONSOLE = textwrap.dedent("""
    import os, signal, sys
    from pathlib import Path
    from sigint101.sstv import SstvDecoder
    from tests.child_fakes import LateProc

    data, mode = Path(sys.argv[1]), sys.argv[2]
    launches = []

    def spawn(freq, device, path):
        child = LateProc(["sstv", "-o", str(path)])
        launches.append((child, path))
        return child

    dec = SstvDecoder(spawn=spawn, radio_present=lambda: True,
                      which=lambda name: "/usr/bin/" + name,
                      output_dir=data / "sstv", data_dir=data)
    dec.start()
    child, staged = launches[0]
    if mode == "live":
        with open(staged, "wb") as writer:
            writer.write(b"picture")
            writer.flush()
            print(staged, flush=True)
            sys.stdin.readline()
        child.feed("Detected SSTV mode Robot 36")
        child.die(0)
        child.close_pipe()
        dec.stop()
        print(len(dec.images()), flush=True)
        dec.shutdown()
    else:
        staged.write_bytes(b"picture")

        def refuse(*args, **kwargs):
            raise OSError("refused")

        Path.replace = refuse
        Path.unlink = refuse
        child.close_pipe()
        dec.stop()
        print(staged, flush=True)
        sys.stdin.readline()
        os.kill(os.getpid(), signal.SIGKILL)
""")


def _other_console(tmp_path, mode):
    return subprocess.Popen([sys.executable, "-c", _CONSOLE, str(tmp_path), mode], cwd=REPO,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def test_a_live_console_in_another_process_keeps_its_capture(tmp_path, consoles):
    other = _other_console(tmp_path, "live")
    try:
        staged = Path(other.stdout.readline().strip())
        assert staged.read_bytes() == b"picture"
        here, _, _ = consoles()
        here.start()
        assert staged.read_bytes() == b"picture"
        other.stdin.write("finish\n")
        other.stdin.flush()
        assert other.stdout.readline().strip() == "1"
        assert other.wait(timeout=10) == 0
        assert (_gallery(tmp_path) / staged.name).read_bytes() == b"picture"
    finally:
        if other.poll() is None:  # pragma: no cover - only when an assertion failed early
            other.kill()
            other.wait(timeout=5)
        other.stdin.close()
        other.stdout.close()


def test_a_crashed_consoles_leftover_is_swept_by_the_next_console(tmp_path, consoles):
    # Case 2: the move into .discard failed and so did the unlink, so only memory knew the file.
    other = _other_console(tmp_path, "abort")
    try:
        staged = Path(other.stdout.readline().strip())
        assert staged.is_file()
        other.stdin.write("die\n")
        other.stdin.flush()
        assert other.wait(timeout=10) == -signal.SIGKILL
    finally:
        other.stdin.close()
        other.stdout.close()
    here, _, heard = consoles()
    here.start()
    assert not staged.exists()
    assert not staged.parent.exists()
    assert here.images() == [] and heard == []


# --- the three leaks -------------------------------------------------------------------------


def test_a_reaped_nonzero_exit_goes_through_the_discard_queue(tmp_path, consoles, monkeypatch):
    # Case 1: a reaped nonzero exit only went to the in-memory cleanup list.
    dec, launches, heard = consoles()
    moves = []
    replace = Path.replace

    def record(path, target):
        moves.append((Path(path), Path(target)))
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", record)
    dec.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    _finish(child, code=1)
    dec.stop()
    assert moves == [(staged, _staging(tmp_path) / ".discard" / staged.name)]
    assert not staged.exists()
    assert not (_staging(tmp_path) / ".discard" / staged.name).exists()
    assert dec.images() == [] and heard == []


def test_a_run_left_unreaped_at_shutdown_is_swept_once_its_console_is_gone(
        tmp_path, consoles, monkeypatch):
    # Case 3: _retry gives up on _closing, so the unreaped run's staged file outlived the console.
    monkeypatch.setattr(childproc, "EXIT_GRACE_S", 0)
    monkeypatch.setattr(receiver_lifecycle, "REAP_RETRY_MIN_S", 0.02)
    upstream = _StubbornStage(["rtl_fm"])
    dec, launches, heard = consoles()
    spawn = dec._spawn
    monkeypatch.setattr(
        dec, "_spawn", lambda *args: childproc.Pipeline(upstream, spawn(*args), finite=True))
    dec.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    try:
        _finish(child)
        with pytest.raises(SstvUnavailable, match="survived kill"):
            dec.shutdown()
        # The unreaped run's file stays while its console could still be holding it.
        assert staged.is_file()
        later, _, later_heard = consoles()
        later.start()
        assert not staged.exists() and not staged.parent.exists()
        assert later.images() == [] and heard == [] and later_heard == []
    finally:
        upstream.die(7)


def test_normal_shutdown_removes_its_own_subfolder(tmp_path, consoles, monkeypatch):
    dec, launches, heard = consoles()
    dec.start()
    child, staged = launches[0]
    staged.write_bytes(PNG_BYTES)
    unlink = Path.unlink

    def refuse_staged(path, *args, **kwargs):
        if path == staged:
            raise OSError("staged source unlink failed")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_staged)
    _finish(child)
    dec.stop()
    [item] = heard
    published = _gallery(tmp_path) / staged.name
    assert staged.is_file() and published.is_file()
    dec.shutdown()
    assert not staged.exists() and not staged.parent.exists()
    assert published.read_bytes() == PNG_BYTES
    assert [image["path"] for image in dec.images()] == [item["path"]]


# --- the sweep ---------------------------------------------------------------------------------


_ODD_NAMES = ("20261399T999999-000-r1.png", "notes.txt", "no extension", ".hidden", "x.json")


def test_any_name_in_a_retired_subfolder_goes_and_the_same_names_of_a_live_one_stay(
        tmp_path, consoles):
    # Codex F5: an impossible timestamp still matched. Ownership, not the name, now decides.
    live, launches, _ = consoles()
    live.start()
    live_owner = launches[0][1].parent
    for name in _ODD_NAMES:
        (live_owner / name).write_bytes(PNG_BYTES)
    retired = _retired_owner(tmp_path, *_ODD_NAMES)
    later, _, _ = consoles()
    later.start()
    assert not retired.exists()
    assert all((live_owner / name).read_bytes() == PNG_BYTES for name in _ODD_NAMES)


def test_a_nested_directory_is_left_and_so_is_its_subfolder(tmp_path, consoles):
    retired = _retired_owner(tmp_path, "loose.png")
    (retired / "nested").mkdir()
    (retired / "nested" / "inner.png").write_bytes(PNG_BYTES)
    dec, _, _ = consoles()
    dec.start()
    assert not (retired / "loose.png").exists()
    assert (retired / "nested" / "inner.png").read_bytes() == PNG_BYTES
    assert (retired / sstv._OWNER_LOCK).is_file()


def test_the_published_pictures_are_never_touched(tmp_path, consoles):
    gallery = _gallery_with_bait(tmp_path)
    retired = _retired_owner(tmp_path, "staged.png")
    (retired / "points-at-gallery.png").symlink_to(gallery / "published.png")
    (retired / "points-at-gallery-folder").symlink_to(gallery)
    os.link(gallery / "published.png", retired / "same-inode.png")
    before = _snapshot(gallery)
    dec, _, _ = consoles()
    dec.start()
    assert not retired.exists()
    assert _snapshot(gallery) == before


@pytest.mark.parametrize("swap", ["symlink", "directory"])
def test_an_entry_swapped_before_its_unlink_is_removed_only_as_itself(
        tmp_path, consoles, monkeypatch, swap):
    # Codex F4: a regular file became a symlink between the type check and the unlink.
    gallery = _gallery_with_bait(tmp_path)
    retired = _retired_owner(tmp_path, "a.png")
    staging_ino = _staging(tmp_path).stat().st_ino
    retired_ino = retired.stat().st_ino
    before = _snapshot(gallery)
    calls = []
    swapping = []
    unlink, rmdir = os.unlink, os.rmdir

    def swapped_unlink(path, *, dir_fd=None):
        if swapping:  # the test's own swap, not the sweep
            return unlink(path, dir_fd=dir_fd)
        calls.append(("unlink", path, None if dir_fd is None else os.fstat(dir_fd).st_ino))
        if path == "a.png" and dir_fd is not None:
            swapping.append(True)
            try:
                (retired / "a.png").unlink()
                if swap == "symlink":
                    (retired / "a.png").symlink_to(gallery / "published.png")
                else:
                    (retired / "a.png").mkdir()
                    (retired / "a.png" / "inner.png").write_bytes(PNG_BYTES)
            finally:
                swapping.clear()
        return unlink(path, dir_fd=dir_fd)

    def recorded_rmdir(path, *, dir_fd=None):
        calls.append(("rmdir", path, None if dir_fd is None else os.fstat(dir_fd).st_ino))
        return rmdir(path, dir_fd=dir_fd)

    dec, _, _ = consoles()
    monkeypatch.setattr(os, "unlink", swapped_unlink)
    monkeypatch.setattr(os, "rmdir", recorded_rmdir)
    dec.start()
    monkeypatch.undo()
    # Every removal was relative to the retired subfolder, or removed that subfolder from the
    # staging folder. None took a path.
    assert calls and all(call[2] is not None for call in calls)
    assert {call[2] for call in calls if call[0] == "unlink"} == {retired_ino}
    assert all(call == ("rmdir", retired.name, staging_ino)
               for call in calls if call[0] == "rmdir")
    assert _snapshot(gallery) == before
    if swap == "symlink":
        assert not retired.exists()
    else:
        assert (retired / "a.png" / "inner.png").read_bytes() == PNG_BYTES
        assert (retired / sstv._OWNER_LOCK).is_file()


@pytest.mark.parametrize("problem", ["symlink-lock", "fifo-lock", "no-lock", "shared-writable"])
def test_a_subfolder_without_a_trustworthy_lock_is_left(tmp_path, consoles, problem):
    gallery = _gallery_with_bait(tmp_path)
    retired = _retired_owner(tmp_path, "staged.png")
    lock = retired / sstv._OWNER_LOCK
    if problem == "symlink-lock":
        lock.unlink()
        lock.symlink_to(gallery / "published.png")
    elif problem == "fifo-lock":
        lock.unlink()
        os.mkfifo(lock)
    elif problem == "no-lock":
        lock.unlink()
    else:
        retired.chmod(0o770)
    before = _snapshot(gallery)
    dec, _, _ = consoles()
    dec.start()  # A FIFO lock must not block the start: the sweep opens it without waiting.
    assert (retired / "staged.png").read_bytes() == PNG_BYTES
    assert _snapshot(gallery) == before


def test_a_staging_folder_left_group_writable_is_closed_and_still_swept(tmp_path, consoles):
    retired = _retired_owner(tmp_path, "staged.png")
    _staging(tmp_path).chmod(0o775)
    dec, _, _ = consoles()
    dec.start()
    assert _staging(tmp_path).stat().st_mode & 0o777 == 0o755
    assert not retired.exists()


def test_a_staging_folder_of_another_account_is_not_swept(tmp_path, consoles, monkeypatch,
                                                         caplog):
    retired = _retired_owner(tmp_path, "staged.png")
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    dec, launches, _ = consoles()
    with caplog.at_level(logging.WARNING, logger=LOG):
        with pytest.raises(SstvUnavailable, match="cannot stage"):
            dec.start()
    assert launches == []
    assert (retired / "staged.png").is_file()
    assert any("staging" in record.message for record in caplog.records)


@pytest.mark.parametrize("swap", ["symlink", "symlink-elsewhere", "gallery-moved-in"])
def test_a_staging_folder_replaced_before_its_open_aborts_the_sweep(
        tmp_path, consoles, monkeypatch, caplog, swap):
    # Codex F3: identities changed before the open pointed the sweep at the published gallery.
    gallery = _gallery_with_bait(tmp_path)
    staging = _staging(tmp_path)
    before = _snapshot(gallery)
    moved = []
    real_open = os.open

    if swap == "symlink":
        staging.symlink_to(gallery, target_is_directory=True)
    elif swap == "symlink-elsewhere":
        # Another folder of this account's, holding what looks like a retired owner.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        bait = elsewhere / uuid.uuid4().hex
        bait.mkdir(mode=0o700)
        (bait / sstv._OWNER_LOCK).touch(mode=0o600)
        (bait / "kept.png").write_bytes(PNG_BYTES)
        staging.symlink_to(elsewhere, target_is_directory=True)
        gallery = elsewhere
        before = _snapshot(elsewhere)
    else:
        def open_after_swap(path, flags, mode=0o777, *, dir_fd=None):
            if path == staging.name and dir_fd is not None and not moved:
                moved.append(True)
                if staging.exists():
                    staging.rename(tmp_path / ".sstv.staging.old")
                gallery.rename(staging)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", open_after_swap)

    dec, launches, _ = consoles()
    with caplog.at_level(logging.WARNING, logger=LOG):
        with pytest.raises(SstvUnavailable, match="cannot stage"):
            dec.start()
    monkeypatch.undo()
    assert launches == []
    assert any("staging" in record.message for record in caplog.records)
    assert _snapshot(staging if swap == "gallery-moved-in" else gallery) == before


def test_a_staging_folder_replaced_after_its_open_keeps_the_sweep_on_the_opened_folder(
        tmp_path, consoles, monkeypatch):
    gallery = _gallery_with_bait(tmp_path)
    retired = _retired_owner(tmp_path, "staged.png")
    staging = _staging(tmp_path)
    before = _snapshot(gallery)
    moved_away = tmp_path / ".sstv.staging.moved"
    real_listdir = os.listdir
    swapped = []

    def listdir_after_swap(path="."):
        if isinstance(path, int) and not swapped:
            swapped.append(True)
            staging.rename(moved_away)
            gallery.rename(staging)
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", listdir_after_swap)
    dec, _, _ = consoles()
    try:
        dec.start()
    except SstvUnavailable:
        pass  # Its own subfolder moved away too; only where the sweep acted matters here.
    monkeypatch.undo()
    assert swapped
    assert not (moved_away / retired.name).exists()
    assert _snapshot(staging) == before


# --- the lock outlives the console in any stage that still runs --------------------------------


def _real_pipeline(monkeypatch, *commands):
    monkeypatch.setattr(sstv, "pipeline_commands", lambda *args, **kw: [list(c) for c in commands])
    return dict(spawn=None, list_devices=lambda: (RtlSdrDevice(index=0, serial="X"),))


def _holds(pid: int, lock: Path) -> bool:
    want = lock.stat()
    for fd in os.listdir(f"/proc/{pid}/fd"):
        try:
            if os.path.samestat(os.stat(f"/proc/{pid}/fd/{fd}"), want):
                return True
        except FileNotFoundError:
            continue
    return False


def test_every_pipeline_stage_holds_its_consoles_staging_lock(tmp_path, consoles, monkeypatch):
    dec, _, _ = consoles(**_real_pipeline(monkeypatch, ["sleep", "30"], ["cat"], ["cat"]))
    dec.start()
    try:
        stages = childproc._stages(dec._run.proc)
        lock = _staging(tmp_path) / dec._run.args[0].staged_path.parent.name / sstv._OWNER_LOCK
        assert len(stages) == 3
        assert all(_holds(stage.pid, lock) for stage in stages)
    finally:
        dec.stop()
    dec.shutdown()
    assert not lock.parent.exists()


# A console whose first stage leaves a writer behind in a session of its own: a stand-in for a
# stage that survives the console. It reports the writer's pid through a FIFO, then is SIGKILLed.
_ORPHANING_CONSOLE = textwrap.dedent("""
    import os, signal, sys
    from pathlib import Path
    from sigint101 import sstv
    from sigint101.hardware import RtlSdrDevice

    data, fifo = Path(sys.argv[1]), sys.argv[2]
    leave = f"setsid sleep 60 </dev/null >/dev/null 2>&1 & echo $! > {fifo}; exec sleep 60"
    sstv.pipeline_commands = lambda *args, **kw: [["bash", "-c", leave], ["cat"], ["cat"]]
    dec = sstv.SstvDecoder(radio_present=lambda: True, which=lambda name: "/usr/bin/" + name,
                           output_dir=data / "sstv", data_dir=data,
                           list_devices=lambda: (RtlSdrDevice(index=0, serial="X"),))
    dec.start()
    print(dec._run.args[0].staged_path.parent, flush=True)
    sys.stdin.readline()
    os.kill(os.getpid(), signal.SIGKILL)
""")


def _wait_for_lock(lock: Path) -> None:
    """Block until nothing else holds the lock, taking it for an instant to prove it is free."""
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    taken = threading.Event()

    def take():
        fcntl.flock(fd, fcntl.LOCK_EX)
        taken.set()

    waiter = threading.Thread(target=take, daemon=True)
    waiter.start()
    waiter.join(10)
    os.close(fd)
    assert taken.is_set(), "the orphaned writer never released the staging lock"


def test_a_writer_that_outlives_its_console_keeps_the_subfolder_until_it_exits(
        tmp_path, consoles):
    fifo = tmp_path / "writer.pid"
    os.mkfifo(fifo)
    other = subprocess.Popen([sys.executable, "-c", _ORPHANING_CONSOLE, str(tmp_path), str(fifo)],
                             cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    writer = None
    try:
        owner = Path(other.stdout.readline().strip())
        box = {}
        reader = threading.Thread(target=lambda: box.setdefault("pid", fifo.read_text()),
                                  daemon=True)
        reader.start()
        reader.join(10)
        writer = int(box["pid"])
        (owner / "staged.png").write_bytes(PNG_BYTES)
        other.stdin.write("die\n")
        other.stdin.flush()
        assert other.wait(timeout=10) == -signal.SIGKILL
        lock = owner / sstv._OWNER_LOCK
        assert _held(lock), "the console's stages did not carry its staging lock"
        first, _, _ = consoles()
        first.start()
        assert (owner / "staged.png").read_bytes() == PNG_BYTES
        os.kill(writer, signal.SIGKILL)
        writer = None
        _wait_for_lock(lock)
        second, _, _ = consoles()
        second.start()
        assert not owner.exists()
    finally:
        if writer is not None:  # pragma: no cover - only when an assertion failed early
            os.kill(writer, signal.SIGKILL)
        if other.poll() is None:  # pragma: no cover
            other.kill()
            other.wait(timeout=5)
        other.stdin.close()
        other.stdout.close()
