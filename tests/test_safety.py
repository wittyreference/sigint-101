# ABOUTME: Tests the non-blocking antenna hardware-protection notice (sigint101/safety.py):
# ABOUTME: exact screen coverage, no disabled controls (D16), theme contrast, and TX-verb guard.

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101.app import create_app
from sigint101.lessons import load_lessons
from sigint101.operate import INSTRUMENTS
from tests.test_templates import THEMES, _contrast, _theme_tokens
from tests.test_theme_accessibility import _decls_for, _font_size_of, _resolve_colour

REPO = Path(__file__).resolve().parent.parent
PKG_DIR = REPO / "sigint101"
STATIC_DIR = PKG_DIR / "static"
PACK_DIR = REPO / "lesson_packs"
LESSONS = load_lessons(PACK_DIR)

# Lessons that must carry the antenna notice: antenna-swap lessons (M6, M7) and LoRa lessons
# (M11, M28). Every other shipped lesson must omit it.
NOTICED_LESSONS = frozenset({6, 7, 11, 28})

# Static guard patterns for transmit verbs across sigint101/**/*.py and sigint101/static/*.js.
# Only sigint101/safety.py may contain these commands; extending this list requires a comment
# explaining which transmitter/tool command is being guarded and why.
TRANSMIT_VERB_PATTERNS = (
    # Meshtastic Python API text transmit call — keys the Nomad's LoRa PA.
    ("meshtastic sendText", re.compile(r"\bsendText\b")),
    # Meshtastic Python API raw payload transmit call — keys the LoRa PA.
    ("meshtastic sendData", re.compile(r"\bsendData\b")),
    # Meshtastic Python API position broadcast call — keys the LoRa PA.
    ("meshtastic sendPosition", re.compile(r"\bsendPosition\b")),
    # Meshtastic CLI flag that transmits a text message over LoRa.
    ("meshtastic --sendtext", re.compile(r"--sendtext\b")),
    # Flipper Zero CLI sub-GHz transmit command (including subghz tx_from_file) — keys CC1101 TX.
    ("Flipper subghz tx", re.compile(r"\bsubghz\s+tx(?:_from_file)?\b")),
    # Flipper Zero CLI infrared transmit command — drives active IR transmission.
    ("Flipper ir tx", re.compile(r"\bir\s+tx\b")),
    # ESP32 Marauder attack command (e.g. `attack -t deauth`, `attack -t beacon`, `attack -t probe`)
    # — actively transmits 802.11 attack/flood frames.
    ("Marauder attack command", re.compile(
        r"\battack\s+-[a-z]\b|\battack\s+(?:deauth|beacon|probe|rickroll)\b|['\"]attack(?:\s|['\"])"
    )),
    # ESP32 Marauder beacon spam commands (`beaconspam`, `beacon -a`/`beacon -t`, quoted command)
    # — actively transmits forged 802.11 beacon frames.
    ("Marauder beacon spam command", re.compile(
        r"\bbeaconspam\b|\bbeacon\s+-[a-z]\b|['\"]beacon\s+"
    )),
)


def _scan_text_for_tx_verbs(text: str) -> list[tuple[int, str]]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pattern in TRANSMIT_VERB_PATTERNS:
            if pattern.search(line):
                hits.append((lineno, label))
    return hits


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path / "data", pack_dir=PACK_DIR)
    with TestClient(app) as c:
        yield c


# --- Module contract: ANTENNA_NOTICE and notice_for --------------------------------------------


def test_antenna_notice_is_one_short_plain_sentence():
    from sigint101.safety import ANTENNA_NOTICE

    assert isinstance(ANTENNA_NOTICE, str)
    text = ANTENNA_NOTICE.strip()
    assert text and "\n" not in text
    assert len(text.split()) <= 20, f"notice exceeds 20 words ({len(text.split())}): {text}"
    # One sentence: no sentence-ending period/question/exclamation before the final character.
    assert not re.search(r"[.!?]\s+\S", text)
    assert "antenna" in text.lower()


def test_notice_for_returns_text_for_lora_antenna_and_requires_tx_only():
    from sigint101.lessons import parse_lesson
    from sigint101.safety import ANTENNA_NOTICE, notice_for

    assert notice_for() is None
    assert notice_for(instrument="lora") == ANTENNA_NOTICE
    assert notice_for(instrument="waterfall") is None

    for inst in INSTRUMENTS:
        got = notice_for(instrument=inst)
        if inst.key == "lora":
            assert got == ANTENNA_NOTICE
            assert type(got) is str
        else:
            assert got is None, f"unexpected notice for instrument {inst.key}"

    for lid, lesson in LESSONS.items():
        got = notice_for(lesson=lesson)
        if lid in NOTICED_LESSONS:
            assert got == ANTENNA_NOTICE, f"missing notice for lesson {lid}"
            assert type(got) is str
        else:
            assert got is None, f"unexpected notice for lesson {lid}"

    tx_lesson = parse_lesson({
        "id": 99,
        "slug": "tx-fixture",
        "title": "Transmit Fixture",
        "concept": "testing requires_tx notice",
        "type": "consolidation",
        "requires_tx": True,
        "hardware": [],
        "stages": {
            "predict": "What must sit on the jack? Lock it in.",
            "operate": "Check the jack.",
            "observe": "Confirm the load.",
            "explain": "Always attach a load.",
        },
        "choices": [
            {"key": "load", "label": "Antenna or load"},
            {"key": "open", "label": "Open jack"},
        ],
        "answer": ["load"],
        "verdict": "Antenna or load.",
    })
    assert notice_for(lesson=tx_lesson) == ANTENNA_NOTICE


# --- Rendered pages: exact coverage and never disables controls (D16) --------------------------


@pytest.mark.parametrize("lesson_id", sorted(LESSONS))
def test_notice_renders_only_on_m6_m7_m11_m28(client, lesson_id):
    from sigint101.safety import ANTENNA_NOTICE

    res = client.get(f"/lesson/{lesson_id}")
    assert res.status_code == 200
    if lesson_id in NOTICED_LESSONS:
        assert ANTENNA_NOTICE in res.text
        assert 'data-safety-notice' in res.text
    else:
        assert ANTENNA_NOTICE not in res.text
        assert 'data-safety-notice' not in res.text


@pytest.mark.parametrize("inst", INSTRUMENTS, ids=lambda i: i.key)
def test_notice_renders_only_on_operate_lora(client, inst):
    from sigint101.safety import ANTENNA_NOTICE

    res = client.get(f"/operate/{inst.key}")
    assert res.status_code == 200
    if inst.key == "lora":
        assert ANTENNA_NOTICE in res.text
        assert 'data-safety-notice' in res.text
    else:
        assert ANTENNA_NOTICE not in res.text
        assert 'data-safety-notice' not in res.text


@pytest.mark.parametrize("pack_type", ["intro", "consolidation"])
def test_notice_renders_on_fixture_pack_with_requires_tx(tmp_path, pack_type):
    from sigint101.safety import ANTENNA_NOTICE

    packs = tmp_path / "packs"
    packs.mkdir()
    stages = (
        "  frame: Frame text.\n  demo: Demo text.\n  try: Try text.\n"
        "  predict: Predict text? Lock it in.\n  observe: Observe text.\n  explain: Explain text.\n"
        if pack_type == "intro"
        else "  predict: Predict text? Lock it in.\n  operate: Operate text.\n"
             "  observe: Observe text.\n  explain: Explain text.\n"
    )
    (packs / "01-tx.yaml").write_text(
        f"id: 1\nslug: tx-test\ntitle: TX Test\nconcept: TX safety\ntype: {pack_type}\n"
        f"requires_tx: true\nhardware: []\npanels: []\nstages:\n{stages}"
        'choices:\n  - {key: "a", label: "Choice A"}\n  - {key: "b", label: "Choice B"}\n'
        'answer: ["a"]\nverdict: "Choice A."\n',
        encoding="utf-8",
    )
    app = create_app(data_dir=tmp_path / "data", pack_dir=packs)
    with TestClient(app) as tx_client:
        res = tx_client.get("/lesson/1")
        assert res.status_code == 200
        assert ANTENNA_NOTICE in res.text
        assert 'data-safety-notice' in res.text
        # D16: committing a prediction works without any extra gate or checkbox.
        commit = tx_client.post("/api/predict?lesson_id=1", data={"prediction": "a"},
                                follow_redirects=False)
        assert commit.status_code == 303


@pytest.mark.parametrize("lesson_id", sorted(NOTICED_LESSONS))
def test_notice_never_disables_controls_on_noticed_lessons(client, lesson_id):
    before = client.get(f"/lesson/{lesson_id}").text
    # Every Next step button and predict radio input stays present and enabled.
    next_buttons = re.findall(r"<button\b[^>]*data-nav=\"next\"[^>]*>", before)
    assert next_buttons, f"lesson {lesson_id} missing Next step buttons"
    assert all("disabled" not in tag for tag in next_buttons)
    radios = re.findall(r"<input\b[^>]*name=\"prediction\"[^>]*>", before)
    assert len(radios) >= 2
    assert all("disabled" not in tag for tag in radios)

    # Prediction commit unlocks explain and Complete lesson without any acknowledgment gate.
    first_key = LESSONS[lesson_id].choices[0]["key"]
    post = client.post(f"/api/predict?lesson_id={lesson_id}", data={"prediction": first_key},
                       follow_redirects=False)
    assert post.status_code == 303
    after = client.get(f"/lesson/{lesson_id}").text
    assert 'data-complete' in after
    complete_tags = re.findall(r"<button\b[^>]*data-complete[^>]*>", after)
    assert complete_tags and all("disabled" not in tag for tag in complete_tags)


def test_notice_never_disables_controls_on_operate_lora(client):
    html = client.get("/operate/lora").text
    assert 'data-lora-panel' in html
    assert 'data-safety-notice' in html
    # Every rack picker link stays clickable and enabled.
    assert 'href="/operate/waterfall"' in html
    assert 'disabled' not in re.search(r'<div class="decoder"[^>]*data-lora-panel.*?</section>',
                                       html, re.S).group(0)


# --- Theme contrast, size, and visibility in empty/stopped states ------------------------------


@pytest.mark.parametrize("theme", THEMES)
def test_safety_notice_meets_wcag_aa_in_all_three_themes(theme):
    tokens = _theme_tokens(theme)
    decls = _decls_for(".safety-notice")
    assert "color" in decls and "background" in decls
    fg = _resolve_colour(decls["color"], tokens)
    bg = _resolve_colour(decls["background"], tokens)
    ratio = _contrast(fg, bg)
    assert ratio >= 4.5, f"{theme}: .safety-notice ({fg} on {bg}) is {ratio:.2f}:1 (< 4.5:1)"
    size = _font_size_of(".safety-notice")
    assert size is not None and size >= 13


# --- Static guard: transmit verbs may appear only in sigint101/safety.py -----------------------


@pytest.mark.parametrize("sample", [
    'iface.sendText("hello")',
    'iface.sendData(b"x")',
    'iface.sendPosition(0, 0)',
    '["meshtastic", "--sendtext", "hi"]',
    'cli.send("subghz tx 0x00F7F7F7 433920000 350 60 0")',
    'cli.send("subghz tx_from_file /ext/subghz/demo.sub")',
    'cli.send("ir tx NEC 0x00 0x01")',
    'cmd = "attack -t deauth"',
    'cmd = "attack -t beacon"',
    'cmd = "beaconspam"',
    'cmd = "beacon -a"',
])
def test_transmit_verb_scanner_catches_every_forbidden_verb(sample):
    assert _scan_text_for_tx_verbs(sample), f"scanner missed forbidden transmit verb: {sample!r}"


@pytest.mark.parametrize("benign", [
    "# vessels beacon on a schedule; a steady poll keeps the roster current",
    "# an SSID is attacker-settable, so this stays off by default",
    '"""APRS stations beacon their position every few minutes."""',
])
def test_transmit_verb_scanner_allows_benign_receive_comments(benign):
    assert _scan_text_for_tx_verbs(benign) == []


def test_transmit_verbs_appear_only_in_safety_module():
    offenders = []
    targets = sorted(PKG_DIR.rglob("*.py")) + sorted(STATIC_DIR.glob("*.js"))
    assert targets, "no source files scanned"
    for path in targets:
        rel = path.relative_to(REPO).as_posix()
        if rel == "sigint101/safety.py":
            continue
        hits = _scan_text_for_tx_verbs(path.read_text(encoding="utf-8"))
        for lineno, label in hits:
            offenders.append(f"{rel}:{lineno}: {label}")
    assert not offenders, (
        "transmit verbs found outside sigint101/safety.py:\n" + "\n".join(offenders)
    )


def test_only_the_rack_route_reaches_the_transmit_boundary():
    expected = {
        "sendText": [("safety.py", "send_lora_text")],
        "send_lora_text": [("lora.py", "send")],
        "app.state.lora.send": [("app.py", "lora_send")],
        "rack_lora_send": [("app.py", "lora_send")],
    }
    actual = {name: [] for name in expected}

    class CallSites(ast.NodeVisitor):
        def __init__(self, path):
            self.path = path
            self.function = None

        def visit_FunctionDef(self, node):
            outer, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = outer

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            name = ast.unparse(node.func)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "sendText":
                name = node.func.attr
            if name in actual:
                actual[name].append((self.path.name, self.function))
            self.generic_visit(node)

    for path in PKG_DIR.rglob("*.py"):
        CallSites(path).visit(ast.parse(path.read_text(encoding="utf-8")))
    assert actual == expected
    js = (STATIC_DIR / "lora.js").read_text(encoding="utf-8")
    click = js.split('addEventListener("click"', 1)[1].split("\n    });", 1)[0]
    assert 'fetch("/api/lora/send"' in click
    assert js.count("/api/lora/send") == 1
    assert not any("/api/lora/send" in path.read_text(encoding="utf-8")
                   for path in STATIC_DIR.rglob("*.js") if path.name != "lora.js")
