# ABOUTME: Verifies WCAG 2.1 AA contrast, CRT overlay layering, glow scope and font-size floors
# ABOUTME: across all three kiosk themes (dark, sunlight, red-night) in app.css and radar labels.

import re

import pytest

from tests.test_templates import APP_CSS, THEMES, _contrast, _theme_tokens

TEMPLATES_DIR = APP_CSS.parent.parent / "templates"
STATIC_DIR = APP_CSS.parent


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _all_rules(css: str | None = None) -> list[tuple[str, str]]:
    """Every (selector, body) pair in app.css with comments removed (excluding @-rule wrappers)."""
    text = _strip_comments(APP_CSS.read_text() if css is None else css)
    rules = []
    for match in re.finditer(r"([^{}@]+)\{([^{}]*)\}", text):
        selector = " ".join(match.group(1).split())
        if selector and not re.match(r"^(?:\d+%|from|to)\b", selector):
            rules.append((selector, match.group(2)))
    return rules


def _decls_for(selector: str, css: str | None = None) -> dict[str, str]:
    """Merged declarations for rules whose selector list includes `selector` exactly."""
    merged = {}
    for sel, body in _all_rules(css):
        parts = [part.strip() for part in sel.split(",")]
        if selector in parts:
            for item in body.split(";"):
                prop, colon, val = item.partition(":")
                if colon and prop.strip():
                    merged[prop.strip()] = val.strip()
    return merged


def _hex_rgb(colour: str) -> tuple[int, int, int]:
    h = colour.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _mix_hex(c1: str, pct: float, c2: str) -> str:
    r1, g1, b1 = _hex_rgb(c1)
    r2, g2, b2 = _hex_rgb(c2)
    p = pct / 100.0
    return "#{:02x}{:02x}{:02x}".format(
        round(r1 * p + r2 * (1 - p)),
        round(g1 * p + g2 * (1 - p)),
        round(b1 * p + b2 * (1 - p)),
    )


def _resolve_colour(expr: str, tokens: dict[str, str]) -> str:
    """Resolve a hex colour, var(--token), or color-mix(in srgb, A N%, B) against theme tokens."""
    expr = expr.strip()
    if expr.startswith("#"):
        return expr
    var_match = re.fullmatch(r"var\((--[\w-]+)\)", expr)
    if var_match:
        return _resolve_colour(tokens[var_match.group(1)], tokens)
    mix_match = re.fullmatch(
        r"color-mix\(\s*in srgb\s*,\s*(.+?)\s+([\d.]+)%\s*,\s*(.+?)\s*\)", expr
    )
    if mix_match:
        c1 = _resolve_colour(mix_match.group(1), tokens)
        pct = float(mix_match.group(2))
        c2 = _resolve_colour(mix_match.group(3), tokens)
        return _mix_hex(c1, pct, c2)
    raise AssertionError(f"unsupported colour expression in app.css: {expr!r}")


def _effective_contrast(fg_hex: str, bg_hex: str, opacity: float = 1.0) -> float:
    """WCAG 2.1 contrast after compositing `fg_hex` at `opacity` over `bg_hex`."""
    if opacity < 1.0:
        fg_hex = _mix_hex(fg_hex, opacity * 100.0, bg_hex)
    return _contrast(fg_hex, bg_hex)


def _opacity_of(selector: str, css: str | None = None) -> float:
    raw = _decls_for(selector, css).get("opacity")
    return float(raw) if raw is not None else 1.0


def _font_size_of(selector: str, css: str | None = None) -> int | None:
    raw = _decls_for(selector, css).get("font-size")
    if raw is None:
        return None
    m = re.fullmatch(r"(\d+)px", raw)
    assert m, f"expected integer px font-size on {selector}, got {raw!r}"
    return int(m.group(1))


# --- Item 3: WCAG 2.1 AA contrast across all three themes ------------------------------------


@pytest.mark.parametrize("theme", THEMES)
def test_meaningful_text_tokens_meet_wcag_aa_on_every_surface(theme):
    tokens = _theme_tokens(theme)
    surfaces = ("--bg", "--surface", "--surface-2", "--surface-3")
    inks = ("--fg", "--fg-dim", "--fg-mute", "--ok", "--warn", "--crit")
    for ink in inks:
        for surface in surfaces:
            ratio = _contrast(_resolve_colour(tokens[ink], tokens), tokens[surface])
            assert ratio >= 4.5, (
                f"{theme}: {ink} ({tokens[ink]}) on {surface} ({tokens[surface]}) "
                f"is {ratio:.2f}:1 (< 4.5:1)"
            )


@pytest.mark.parametrize("theme", THEMES)
def test_tinted_status_badges_and_waterfall_labels_meet_wcag_aa(theme):
    tokens = _theme_tokens(theme)
    # Each (selector, fallback_bg_token) checked with its own CSS color and background.
    pairs = [
        (".decoder__status", "--surface"),
        (".decoder__status--live", "--surface"),
        (".decoder__status--empty", "--surface"),
        (".decoder__status--stopped", "--surface"),
        (".rayhunter__line--warning", "--surface"),
        (".badge--sample", "--wf-bg"),
        (".badge--live", "--wf-bg"),
        (".badge--na", "--wf-bg"),
        (".waterfall__sealed", "--wf-bg"),
        (".waterfall__axis", "--wf-bg"),
        (".waterfall__axis b", "--wf-bg"),
    ]
    for selector, default_bg in pairs:
        decls = _decls_for(selector)
        assert "color" in decls, f"{selector} has no color declaration"
        fg = _resolve_colour(decls["color"], tokens)
        bg_expr = decls.get("background", f"var({default_bg})")
        # A semi-transparent waterfall axis backdrop over --wf-bg resolves to --wf-bg.
        if "transparent" in bg_expr:
            bg = tokens[default_bg]
        else:
            bg = _resolve_colour(bg_expr, tokens)
        ratio = _contrast(fg, bg)
        assert ratio >= 4.5, f"{theme}: {selector} ({fg} on {bg}) is {ratio:.2f}:1 (< 4.5:1)"


@pytest.mark.parametrize("theme", THEMES)
def test_waterfall_live_and_sample_keep_separate_tones(theme):
    tokens = _theme_tokens(theme)
    live = _decls_for(".badge--live")["color"]
    sample = _decls_for(".badge--sample")["color"]
    assert live == "var(--wf-ok)"
    assert sample == "var(--wf-warn)"
    assert _resolve_colour(live, tokens) != _resolve_colour(sample, tokens)


def test_only_refusals_use_the_critical_dashed_badge():
    for selector in (".badge--na", ".decoder__status--empty", ".decoder__status--stopped"):
        badge = _decls_for(selector)
        assert badge["color"] == "var(--crit)", selector
        assert badge["background"] == "var(--bg)", selector
        assert badge["border"] == "2px dashed currentColor", selector
    for selector in (".badge--sample", ".badge--live", ".decoder__status"):
        assert "solid" in _decls_for(selector)["border"], selector


def test_running_decoder_badges_have_plain_backgrounds():
    for selector in (".decoder__status", ".decoder__status--live"):
        assert _decls_for(selector).get("background", "transparent") == "transparent", selector


@pytest.mark.parametrize("theme", THEMES)
def test_frozen_panels_and_rail_never_dim_text_below_wcag_aa(theme):
    tokens = _theme_tokens(theme)
    # Frozen panels dim only borders/grids, never text; rail pips never use opacity dimming.
    for selector in (
        ".decoder--frozen",
        ".waterfall-wrap--frozen",
        ".waterfall-wrap--frozen .waterfall__empty",
        "button.rail-pip:disabled",
    ):
        assert _opacity_of(selector) == 1.0, f"{selector} dims text with opacity < 1"

    # Composited contrast for sealed notices and rail labels (after any ancestor opacity).
    checks = [
        (".decoder__sealed", "--surface", (".decoder--frozen", ".decoder__sealed")),
        (".decoder__axis", "--surface", (".decoder--frozen", ".decoder__axis")),
        (".waterfall__sealed", "--wf-bg", (".waterfall-wrap--frozen",
                                           ".waterfall-wrap--frozen .waterfall__empty",
                                           ".waterfall__sealed")),
        (".rail-pip", "--surface-2", (".rail-pip", "button.rail-pip:disabled")),
        (".rail-pip--locked", "--surface-2", (".rail-pip", ".rail-pip--locked")),
        (".rail__gate", "--surface", (".rail__gate",)),
        (".readout__label", "--surface", (".readout__label",)),
    ]
    for selector, bg_tok, chain in checks:
        decls = _decls_for(selector)
        fg = _resolve_colour(decls["color"], tokens)
        alpha = 1.0
        for ancestor in chain:
            alpha *= _opacity_of(ancestor)
        ratio = _effective_contrast(fg, tokens[bg_tok], alpha)
        assert ratio >= 4.5, (
            f"{theme}: {selector} at opacity {alpha:.2f} on {bg_tok} is {ratio:.2f}:1 (< 4.5:1)"
        )

    # Active tile metadata carries an opacity on inverted ink; verify it stays >= 4.5:1 too.
    meta_alpha = _opacity_of(".tile--active .tile__meta")
    meta_ratio = _effective_contrast(tokens["--sel-ink"], tokens["--sel-bg"], meta_alpha)
    assert meta_ratio >= 4.5, f"{theme}: .tile--active .tile__meta is {meta_ratio:.2f}:1"


def test_themes_preserve_phosphor_green_red_only_and_sunlight_identities():
    dark = _theme_tokens("dark")
    for tok in ("--fg", "--fg-dim", "--fg-mute", "--accent"):
        r, g, b = _hex_rgb(dark[tok])
        assert g > r and g > b, f"dark {tok} is not green: {dark[tok]}"

    red = _theme_tokens("red-night")
    for tok in ("--fg", "--fg-dim", "--fg-mute", "--accent", "--ok", "--warn", "--crit",
                "--wf-ink", "--wf-axis", "--wf-ok", "--wf-warn"):
        r, g, b = _hex_rgb(_resolve_colour(red[tok], red))
        assert r > g and r > b and g == b, f"red-night {tok} is not red-only: {red[tok]}"

    sun = _theme_tokens("sunlight")
    assert sun["--glow"] == "none"
    assert sun["--glow-strong"] == "none"
    assert sun["--scan"] == "transparent"


# --- Item 3: scanlines stay behind text; glow stays on short display titles only -------------


def test_scanlines_sit_behind_foreground_content():
    overlay = _decls_for(".app::after")
    assert overlay.get("pointer-events") == "none"
    overlay_z = int(overlay["z-index"])
    for foreground in (".content", ".statusbar", ".rail"):
        fg_z = int(_decls_for(foreground)["z-index"])
        assert overlay_z < fg_z, (
            f".app::after z-index ({overlay_z}) must sit behind {foreground} ({fg_z})"
        )


def test_glow_is_kept_only_on_short_display_titles():
    # Short display titles allowed to carry phosphor glow (plus the single-letter chip badge).
    allowed = {
        ".statusbar__title",
        ".stage-panel__title",
        ".tile__name",
        ".exam-cta__title",
        ".error-screen__code",
        ".exam-results__verdict",
        ".predict-chip__key",
    }
    glowing = set()
    for sel, body in _all_rules():
        if re.search(r"text-shadow:\s*var\(--glow", body):
            for part in sel.split(","):
                glowing.add(part.strip())
    unexpected = glowing - allowed
    assert not unexpected, f"non-title selectors still carry text-shadow glow: {sorted(unexpected)}"

    # Long prompts that reuse .stage-panel__title or sit in exam cards must suppress glow.
    for selector in (
        '.stage-panel[data-stage="predict"] .stage-panel__title',
        '.stage-panel[data-stage="check"] .stage-panel__title',
    ):
        assert _decls_for(selector).get("text-shadow") == "none", selector
    assert "var(--glow" not in _decls_for(".exam__question").get("text-shadow", "")


# --- Item 6: no visible text under 13 px; instructions & status values >= 18 px --------------


def test_no_font_size_in_app_css_or_radar_svg_is_under_13px():
    too_small = []
    for sel, body in _all_rules():
        for m in re.finditer(r"font-size:\s*([\d.]+)px", body):
            px = float(m.group(1))
            if px < 13:
                too_small.append((sel, px))
    assert not too_small, f"app.css rules with font-size < 13px: {too_small}"

    # Radar SVG labels explicitly checked, and the SVG must not clip compass glyphs at its viewBox.
    for selector in (".radar__plane-label", ".radar__compass", ".radar__ring-label"):
        size = _font_size_of(selector)
        assert size is not None and size >= 13, (selector, size)
    assert _decls_for(".radar__svg").get("overflow") == "visible"

    # No inline font-size under 13px in templates or adsb.js either.
    for path in [*TEMPLATES_DIR.glob("*.html"), STATIC_DIR / "adsb.js"]:
        text = path.read_text()
        for m in re.finditer(r"font-size\s*[:=]\s*[\"']?([\d.]+)(?:px)?", text):
            assert float(m.group(1)) >= 13, (path.name, m.group(0))


def test_instructions_and_status_values_are_at_least_18px():
    must_be_18 = (
        # Instructions and prompts
        ".stage-panel__body",
        '.stage-panel[data-stage="predict"] .stage-panel__title',
        '.stage-panel[data-stage="check"] .stage-panel__title',
        ".predict__hint",
        ".predict__committed-note",
        ".verdict__line",
        ".verdict__why",
        ".waterfall__reason",
        ".decoder__empty",
        ".gain-meter__empty",
        ".error-screen__detail",
        ".empty-note",
        ".exam__question",
        ".exam-result",
        # Status values
        ".readout__value",
        ".gain-meter__value",
        ".gain-meter__gap b",
        ".prov__verdict",
        ".ppm__result",
        ".rayhunter__line",
        ".flipper__radio",
        ".flipper__line",
        ".tune__freq",
        ".rack__tune .tune__freq",
        ".rack__gain-value",
        ".radar__count b",
        ".exam-sim__score",
    )
    failures = []
    for selector in must_be_18:
        size = _font_size_of(selector)
        if size is None or size < 18:
            failures.append((selector, size))
    assert not failures, f"instruction/status-value selectors under 18px: {failures}"


def test_predict_and_check_prompts_use_body_font_at_body_size():
    # Predict and check prompts are multi-sentence questions, not short display titles: set them
    # in the mono body face at the body size (matching .stage-panel__body and .exam__question).
    body_size = _font_size_of(".stage-panel__body")
    assert body_size == 20
    for selector in (
        '.stage-panel[data-stage="predict"] .stage-panel__title',
        '.stage-panel[data-stage="check"] .stage-panel__title',
    ):
        decls = _decls_for(selector)
        assert decls.get("font-family") == "var(--font-mono)", (selector, decls.get("font-family"))
        assert _font_size_of(selector) == body_size, (selector, decls.get("font-size"))


def test_explain_verdict_values_are_not_underlined():
    # Underlined mono words read as links on a touchscreen; brightness alone carries emphasis.
    decls = _decls_for(".verdict__line b")
    assert "underline" not in decls.get("text-decoration", ""), decls
    assert "border-bottom" not in decls, decls


@pytest.mark.parametrize("theme", THEMES)
def test_primary_keys_are_filled_and_readable_in_every_theme(theme):
    primary = _decls_for(".btn--primary") | _decls_for(f'.app[data-theme="{theme}"] .btn--primary')
    tokens = _theme_tokens(theme)
    assert primary["background"] == "var(--accent)"
    assert _contrast(_resolve_colour(primary["color"], tokens), tokens["--accent"]) >= 4.5
    assert primary.get("text-shadow", "none") == "none"


@pytest.mark.parametrize("selector", [".btn--prev", ".exam__sim-link", ".exam__skip"])
def test_secondary_navigation_has_a_quieter_outline(selector):
    style = _decls_for(selector)
    assert style["border"] == "2px solid var(--border)"
    assert style["color"] == "var(--fg-dim)"


def test_prev_has_at_least_24_pixels_clearance_from_tune_up():
    gap = int(_decls_for(".tune")["gap"].removesuffix("px"))
    extra = _decls_for('.tune-btn[data-tune-step="1"] + .btn--prev').get("margin-left", "0px")
    assert gap + int(extra.removesuffix("px")) >= 24


def test_the_gate_label_is_one_line_of_unboxed_status_text():
    gate = _decls_for(".rail__gate")
    assert gate.get("white-space") == "nowrap"
    assert "border" not in gate
