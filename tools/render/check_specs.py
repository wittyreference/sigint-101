# ABOUTME: Discovers the layout checks and their GET-walk pages from source declarations.
# ABOUTME: Reads literals without importing scripts, which can inspect argv or start work.
import ast
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit


class Check(NamedTuple):
    path: Path
    name: str
    pages: tuple[str, ...]
    scratch: bool
    order: int = 1000
    target: str = ""
    args: tuple[str, ...] = ()
    runs: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def invocations(self, console, output):
        """Arguments for each subprocess, in declaration order."""
        if self.scratch:
            return ((),)
        return tuple((console + target, *(arg.replace("{output}", str(output)) for arg in args))
                     for target, args in self.runs or ((self.target, self.args),))


def _read_check(path):
    """Read one literal RENDER_CHECK assignment. None marks a support or manual tool."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values = [node.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "RENDER_CHECK"
                          for target in node.targets)]
        if len(values) != 1:
            raise ValueError("expected one RENDER_CHECK declaration")
        declaration = ast.literal_eval(values[0])
        if declaration is None:
            return None
        check = Check(path, **declaration)
        if not isinstance(check.name, str) or not check.name.strip():
            raise ValueError("name must be a nonempty string")
        if not isinstance(check.pages, tuple) or not check.pages or not all(
            isinstance(page, str) and page.startswith("/") and not page.startswith("//")
            and not urlsplit(page).fragment for page in check.pages
        ):
            raise ValueError("pages must be a nonempty tuple of local paths and queries")
        if type(check.scratch) is not bool or type(check.order) is not int:
            raise ValueError("scratch must be a bool and order an integer")
        if not isinstance(check.runs, tuple) or not all(
            isinstance(run, tuple) and len(run) == 2 for run in check.runs
        ):
            raise ValueError("runs must be a tuple of (target, args) pairs")
        if check.runs and (check.target != "" or check.args != ()):
            raise ValueError("runs replaces the single target and args")
        if check.scratch and (check.runs or check.target != "" or check.args != ()):
            raise ValueError("scratch checks cannot take runner arguments")
        for target, args in check.runs or ((check.target, check.args),):
            if target != "" and target not in check.pages:
                raise ValueError("target must be a declared page or the empty string")
            if not isinstance(args, tuple) or not all(isinstance(arg, str) for arg in args):
                raise ValueError("args must be a tuple of strings")
        return check
    except (OSError, SyntaxError, TypeError, ValueError) as error:
        raise ValueError(f"{path.name}: invalid RENDER_CHECK ({error})") from error


def discover_checks(directory):
    """Find every render_*.py declaration, retaining the established output order."""
    checks = {}
    for path in sorted(directory.glob("render_*.py")):
        check = _read_check(path)
        if check is not None:
            if check.name in checks:
                raise ValueError(f"{path.name}: duplicate RENDER_CHECK name {check.name!r}")
            checks[check.name] = check
    if not checks:
        raise ValueError("found no render checks")
    return sorted(checks.values(), key=lambda check: (check.order, check.name))
