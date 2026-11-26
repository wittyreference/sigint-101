# ABOUTME: Tests README.md's Setup section against the repository: the tools, commands, make
# ABOUTME: targets, paths and course folder it names are the ones bootstrap.sh and provision.sh use.

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")


def _setup():
    """The text of the README's Setup section, from its heading to the next level-2 heading."""
    found = re.search(r"^## Setup\s*$(.*?)(?=^## |\Z)", README, re.MULTILINE | re.DOTALL)
    assert found, "README.md has no '## Setup' section"
    return found.group(1)


def test_setup_names_every_tool_development_needs():
    # bootstrap.sh runs python3 -m venv and git; the Makefile drives the rest; make selftest runs
    # tests/test_js_panels.py, which fails without node. The README is where a newcomer looks.
    text = _setup().lower()
    tools = {"python": r"\bpython", "git": r"\bgit\b", "make": r"\bmake\b",
             "node": r"\bnode(?:js)?\b"}
    missing = [tool for tool, pattern in tools.items() if not re.search(pattern, text)]
    assert not missing, f"README's Setup names no {missing}"


def test_setup_gives_both_paths_and_links_the_quick_start():
    text = _setup()
    for command in ("bootstrap.sh", "make selftest", "make run", "deploy/provision.sh"):
        assert command in text, f"README's Setup does not name {command}"
    assert re.search(r"\]\((?:\./)?docs/quickstart\.md\)", text), \
        "README's Setup does not link docs/quickstart.md"


def test_setup_clones_the_course_where_provision_sh_looks_for_it():
    # provision.sh updates and builds the checkout at COURSE_DIR; a clone anywhere else is not
    # the one the kiosk runs.
    provision = (REPO / "deploy" / "provision.sh").read_text(encoding="utf-8")
    course_dir = re.search(r'^COURSE_DIR="\$HOME/([^"]+)"$', provision, re.MULTILINE)
    assert course_dir, "provision.sh no longer sets COURSE_DIR under $HOME"
    assert f"~/{course_dir.group(1)}" in _setup(), \
        f"README's Setup does not clone to ~/{course_dir.group(1)}"


def test_every_make_target_the_readme_names_exists():
    targets = set(re.findall(r"^([\w-]+):", (REPO / "Makefile").read_text(encoding="utf-8"),
                             re.MULTILINE))
    named = set(re.findall(r"\bmake ([a-z][\w-]*)", README))
    assert named, "README.md names no make target"
    assert named <= targets, f"README.md names make targets the Makefile lacks: {named - targets}"


def test_every_script_and_document_the_readme_names_exists():
    # Paths are written from the checkout's top, as ./x, or under the Recon's ~/sigint-101.
    named = set(re.findall(r"(?:\./|~/sigint-101/)?((?:[\w-]+/)*[\w.-]+\.(?:sh|md))", README))
    assert named, "README.md names no script or document"
    missing = sorted(p for p in named if not (REPO / p).is_file())
    assert not missing, f"README.md names files that are not in the repository: {missing}"
