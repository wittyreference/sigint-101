# ABOUTME: install.sh's one judge of what is the course watchdog, and its only way to signal it:
# ABOUTME: pins each PID with a pidfd, checks it after the pin, and sends TERM or KILL through it.
"""Usage (install.sh runs it as `python3 -I`, with an emptied environment):

    watchdog_helper.py FIND LOCK INSTALLED CHECKOUT PID...
        prints "PID:START" for each PID that is the watchdog.
    watchdog_helper.py TERM|KILL LOCK INSTALLED CHECKOUT PID:START...
        pins every PID with a pidfd, then signals through it each pinned process that is still
        that watchdog (same start time). Prints each PID signalled. Under KILL, a watchdog whose
        running launcher could exec another program is not killed: it is printed as
        "kept PID". Exit 4: no pidfd here (Linux 5.3+ and Python 3.9+), nothing was signalled.

A watchdog is a process of this user whose executable is a trusted shell (bash or sh, found by
the file /proc/PID/exe points at, not by argv[0]), whose command line is exactly
"<shell> <launcher> watch" with <launcher> the same file as the installed or the checkout
launcher, and which holds the lock (its fdinfo lists the flock). Its start time (stat field 22)
tells it from a later process with the same PID.

No signal ever goes to a PID by number. The checks read /proc/PID after the pin; if the pinned
process exited and its PID was reused meanwhile, the checks may read the newcomer, but the send
goes through the pidfd, which still names the exited process, so it fails and reaches nobody.

The window left: a watchdog that passed the last check can still exec another program before
the signal lands, and that program then gets the signal. Only one of the kiosk user's own
processes can do that. KILL is kept for launchers whose watch path never execs (see
`can_exec`), so the window there needs a launcher that was not what it looked like.
"""

import os
import re
import signal
import sys

SHELLS = ("/bin/bash", "/usr/bin/bash", "/bin/sh", "/usr/bin/sh")


def same_file(path, other):
    try:
        return os.path.samefile(path, other)
    except OSError:
        return False


def runs_a_trusted_shell(pid):
    """True if /proc/PID/exe is the same file as a system bash or sh."""
    return any(os.path.exists(shell) and same_file(f"/proc/{pid}/exe", shell)
               for shell in SHELLS)


def launcher_path(pid, argv):
    script = os.fsdecode(argv[1])
    return script if script.startswith("/") else f"/proc/{pid}/cwd/{script}"


def watchdog_start(pid, lock, launchers):
    """The start time of PID if it is the watchdog, else None."""
    proc = f"/proc/{pid}"
    try:
        if os.stat(proc).st_uid != os.getuid():
            return None
        if not runs_a_trusted_shell(pid):
            return None
        with open(f"{proc}/stat", encoding="utf-8", errors="replace") as f:
            fields = f.read().rpartition(") ")[2].split()
        if len(fields) < 20:
            return None
        with open(f"{proc}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")[:-1]
        if len(argv) != 3 or argv[2] != b"watch":
            return None
        if not any(same_file(launcher_path(pid, argv), path) for path in launchers):
            return None
        for fd in os.listdir(f"{proc}/fd"):
            try:
                if os.readlink(f"{proc}/fd/{fd}") != lock:
                    continue
                with open(f"{proc}/fdinfo/{fd}", encoding="utf-8") as f:
                    if any(ln.startswith("lock:") and "FLOCK" in ln for ln in f):
                        return fields[19]
            except OSError:
                continue
    except OSError:
        pass
    return None


# --- Can a launcher's watch path exec another program? -------------------------------------

FUNCTION = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{\s*$")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A command word where a command can start: line start, or after ; & | ( { ! or a quote (a
# trap's or eval's string), or after then/do/else.
COMMAND = re.compile(r"(?:^|[;&|({!'\"]|\bthen|\bdo|\belse)\s*(exec|eval|source|\.)(?=\s|$|;)")
# A redirection word: an optional fd number or {name}, an operator, and maybe its target.
REDIRECTION = re.compile(r"^(?:\d+|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:>>|<>|>&|<&|>|<)(\S*)$")
# Words that end the command.
SEPARATORS = {";", "&", "&&", "|", "||", ")", "}", ";;"}


def strip_comments_and_heredocs(text):
    """The script's lines without comments and here-document bodies (another program's input)."""
    lines, until = [], None
    for line in text.splitlines():
        if until is not None:
            if line.strip() == until:
                until = None
            continue
        line = re.sub(r"(^|\s)#.*$", r"\1", line)
        heredoc = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if heredoc:
            until = heredoc.group(1)
        lines.append(line)
    return lines


def watch_path(text):
    """The lines that can run once `watch` starts: the top-level code, and the bodies of `watch`
    and of every function reachable from it (any function name a reachable body mentions)."""
    lines = strip_comments_and_heredocs(text)
    bodies, top, name = {}, [], None
    for line in lines:
        if name is None:
            match = FUNCTION.match(line)
            if match:
                name = match.group(1)
                bodies[name] = []
            else:
                top.append(line)
        elif line == "}":
            name = None
        else:
            bodies[name].append(line)
    reach, todo = set(), ["watch"]
    while todo:
        fn = todo.pop()
        if fn in reach or fn not in bodies:
            continue
        reach.add(fn)
        todo.extend(w for line in bodies[fn] for w in WORD.findall(line) if w in bodies)
    return top + [line for fn in sorted(reach) for line in bodies[fn]]


def program_execs(lines):
    """The lines with an exec that runs a program (or an eval, source or `.`, which could).

    `exec` followed only by redirections (`exec 9>"$LOCK"`, `exec 9>&-`) changes the shell's
    own file descriptors and runs nothing; any other word after it is a program to become."""
    found = []
    for line in lines:
        if any(runs_a_program(line[match.end():]) if match.group(1) == "exec" else True
               for match in COMMAND.finditer(line)):
            found.append(line.strip())
    return found


def runs_a_program(rest):
    """Do the words after an `exec` name a program, or only redirections? A word that ends with
    ; ) or } ends the command. Anything unclear counts as a program (KILL is then withheld)."""
    words = iter(rest.split())
    for word in words:
        if word in SEPARATORS:
            return False
        last = word[-1] in ";)}"
        redirection = REDIRECTION.match(word.rstrip(";)}") if last else word)
        if redirection is None:
            return True
        if last:
            return False
        if not redirection.group(1):
            next(words, None)  # its target is the next word
    return False


def can_exec(text):
    return bool(program_execs(watch_path(text)))


def running_launcher_can_exec(pid, argv_path):
    """Read the launcher PID actually runs (bash keeps its script open on fd 255, even after
    install.sh replaced the file) and judge it. Unknown counts as "can exec"."""
    link = f"/proc/{pid}/fd/255"
    try:
        target = os.readlink(link).removesuffix(" (deleted)")
        if target != os.path.realpath(argv_path):
            return True
        with open(link, "rb") as f:
            return can_exec(f.read().decode("utf-8", errors="replace"))
    except OSError:
        return True


def main(args):
    name, lock, installed, checkout, *targets = args
    launchers = (installed, checkout)
    if name == "FIND":
        for pid in targets:
            start = watchdog_start(pid, lock, launchers)
            if start is not None:
                print(f"{pid}:{start}")
        return 0
    signum = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL}[name]
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return 4
    # Pin all first: without a pidfd nothing at all is signalled.
    pinned = []
    for target in targets:
        pid, _, start = target.partition(":")
        try:
            pinned.append((os.pidfd_open(int(pid)), pid, start))
        except ProcessLookupError:
            continue  # already gone
        except OSError:
            return 4
    for fd, pid, start in pinned:
        if watchdog_start(pid, lock, launchers) != start:
            continue
        if signum == signal.SIGKILL:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")[:-1]
                path = launcher_path(pid, argv)
            except (OSError, IndexError):
                continue
            if running_launcher_can_exec(pid, path):
                print(f"kept {pid}")
                continue
        try:
            signal.pidfd_send_signal(fd, signum)
        except OSError:
            continue  # it exited after the check
        print(pid)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
