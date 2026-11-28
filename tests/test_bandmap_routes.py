# ABOUTME: Route and invariant tests for /bands and Home's BANDS status-bar entry key.
# ABOUTME: Verifies markup, honest wording, zero radio touches, and byte-identical progress.db.

import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from sigint101.app import PACK_DIR, create_app
from sigint101.hardware import RadioProbe
from sigint101.lessons import load_lessons
from sigint101.progress import Progress

EXPECTED_CAPTION = "Where each lesson tunes. A pin never means a signal on the air now."
FORBIDDEN_ROW_RE = re.compile(r"\b(live|heard|receiving|on air|active|detected)\b", re.IGNORECASE)


class _SpyReceiver:
    """Records any lifecycle or tuning method call so /bands proves it touches no radio."""

    def __init__(self, name: str, calls: list):
        self._name = name
        self._calls = calls
        self.on_reception = None
        self.reception_sink = None
        self.on_transmission = None

    def start(self, *args, **kwargs):
        self._calls.append((self._name, "start"))

    def retune(self, *args, **kwargs):
        self._calls.append((self._name, "retune"))

    def release(self, *args, **kwargs):
        self._calls.append((self._name, "release"))

    def resume(self, *args, **kwargs):
        self._calls.append((self._name, "resume"))

    def stop(self, *args, **kwargs):
        self._calls.append((self._name, "stop"))

    def close(self, *args, **kwargs):
        self._calls.append((self._name, "close"))


def _make_app(tmp_path: Path, calls: list):
    factory = lambda name: (lambda: _SpyReceiver(name, calls))  # noqa: E731
    return create_app(
        pack_dir=PACK_DIR,
        data_dir=tmp_path,
        probe=lambda: RadioProbe(False, "lsusb", "test"),
        sweeper_factory=factory("sweeper"),
        decoder_factory=factory("decoder"),
        lora_factory=factory("lora"),
        rds_factory=factory("rds"),
        adsb_factory=factory("adsb"),
        aprs_factory=factory("aprs"),
        pocsag_factory=factory("pocsag"),
        acars_factory=factory("acars"),
        ais_factory=factory("ais"),
        dmr_factory=factory("dmr"),
        apt_factory=factory("apt"),
        sstv_factory=factory("sstv"),
        flock_factory=factory("flock"),
        rayhunter_factory=factory("rayhunter"),
        flipper_factory=factory("flipper"),
        survey_factory=factory("survey"),
    )


class _RowExtractor(HTMLParser):
    """Collects anchor rows, their text, and interactive tags inside .bandmap__overview."""

    def __init__(self):
        super().__init__()
        self.rows: list[tuple[str, str]] = []
        self._in_row: str | None = None
        self._row_chunks: list[str] = []
        self._overview_depth = 0
        self.overview_interactive: list[str] = []
        self._statusbar_depth = 0
        self.statusbar_links: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())
        void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                     "param", "source", "track", "wbr"}
        if tag == "header" and "statusbar" in classes:
            self._statusbar_depth = 1
        elif self._statusbar_depth > 0:
            if tag not in void_tags:
                self._statusbar_depth += 1
            if tag == "a" and "href" in attr_map:
                self.statusbar_links.append(attr_map["href"])

        if "bandmap__overview" in classes:
            self._overview_depth = 1
        elif self._overview_depth > 0:
            if tag not in void_tags:
                self._overview_depth += 1
            if tag in ("a", "button"):
                self.overview_interactive.append(tag)

        if tag == "a" and "bandmap__row" in classes:
            self._in_row = attr_map.get("href", "")
            self._row_chunks = []

    def handle_startendtag(self, tag, attrs):
        attr_map = dict(attrs)
        if self._statusbar_depth > 0 and tag == "a" and "href" in attr_map:
            self.statusbar_links.append(attr_map["href"])
        if self._overview_depth > 0 and tag in ("a", "button"):
            self.overview_interactive.append(tag)

    def handle_endtag(self, tag):
        if self._statusbar_depth > 0:
            self._statusbar_depth -= 1
        if self._overview_depth > 0:
            self._overview_depth -= 1
        if tag == "a" and self._in_row is not None:
            self.rows.append((self._in_row, " ".join("".join(self._row_chunks).split())))
            self._in_row = None
            self._row_chunks = []

    def handle_data(self, data):
        if self._in_row is not None:
            self._row_chunks.append(data)


def test_bands_page_markup_honest_wording_and_replay_mark(tmp_path):
    calls: list = []
    app = _make_app(tmp_path, calls)
    with TestClient(app) as client:
        resp = client.get("/bands")
        assert resp.status_code == 200
        html = resp.text

    # Back link goes to Home.
    back = re.search(
        r'<a class="btn-back"[^>]*href="([^"]+)"[^>]*>.*?'
        r'<span class="btn-back__label">([^<]+)</span>',
        html,
        re.S,
    )
    assert back is not None
    assert back.group(1) == "/"
    assert back.group(2).strip() == "Home"

    # Fixed caption is exact.
    caption = re.search(r'<p class="bandmap__caption">([^<]+)</p>', html)
    assert caption is not None
    assert unescape(caption.group(1)).strip() == EXPECTED_CAPTION

    # No reception badge mark anywhere on /bands.
    assert "logbook-mark--stamp" not in html

    # One <a href="/lesson/{id}"> per radio lesson row (18 dial + 6 off-dial = 24).
    parser = _RowExtractor()
    parser.feed(html)
    lessons = load_lessons(PACK_DIR)
    expected_hrefs = {f"/lesson/{lid}" for lid, les in lessons.items() if les.hardware}
    actual_hrefs = [href for href, _ in parser.rows]
    assert len(actual_hrefs) == len(expected_hrefs)
    assert set(actual_hrefs) == expected_hrefs

    # Row text never claims a signal is live/heard/receiving/on air/active/detected.
    for href, text in parser.rows:
        assert not FORBIDDEN_ROW_RE.search(text), f"{href}: forbidden wording in {text!r}"

    # M24's row carries .logbook-mark--replay with "REPLAY ONLY".
    m24_match = re.search(
        r'<a class="bandmap__row"[^>]*href="/lesson/24"[^>]*>(.*?)</a>', html, re.S
    )
    assert m24_match is not None
    m24_html = m24_match.group(1)
    assert "logbook-mark--replay" in m24_html
    assert "REPLAY ONLY" in m24_html
    assert "NOAA APT off the air since August 2025" in m24_html

    # Overview contains no <a> or <button>.
    assert parser.overview_interactive == []


def test_bands_touches_no_radio_and_leaves_progress_byte_identical(tmp_path):
    # Seed progress with opened, completed, prediction, and stamp data before starting the app.
    prog = Progress(tmp_path / "progress.db")
    prog.mark_opened(1)
    prog.record_prediction(1, "peaks")
    prog.mark_completed(1)
    prog.record_stamps(
        [
            {
                "instrument": "ism",
                "first_ts": 1_700_000_000.0,
                "digest": "ab" * 16,
                "provenance": {"tool": "rtl_433", "freq_hz": 433_920_000},
            }
        ]
    )
    prog.close()

    calls: list = []
    app = _make_app(tmp_path, calls)
    with TestClient(app) as client:
        # Clear any startup calls (none expected from spy, just in case) and snapshot progress.db.
        calls.clear()
        db_path = tmp_path / "progress.db"
        before_bytes = db_path.read_bytes()

        resp = client.get("/bands")
        assert resp.status_code == 200

        after_bytes = db_path.read_bytes()

    assert after_bytes == before_bytes
    radio_actions = [c for c in calls if c[1] in ("retune", "start", "release")]
    assert radio_actions == []


def test_home_has_bands_key_in_statusbar(tmp_path):
    calls: list = []
    app = _make_app(tmp_path, calls)
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.text

    parser = _RowExtractor()
    parser.feed(html)
    assert "/bands" in parser.statusbar_links
