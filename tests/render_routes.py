# ABOUTME: The GET walk derived from the render tools' source declarations.
# ABOUTME: test_render_checks.py compares real navigations with that same set of tools and pages.
from pathlib import Path

from tools.render.check_specs import discover_checks

RENDER_DIR = Path(__file__).resolve().parents[1] / "tools" / "render"
CHECKS = discover_checks(RENDER_DIR)
RENDER_CHECK_PAGES = tuple(dict.fromkeys(page for check in CHECKS for page in check.pages))
