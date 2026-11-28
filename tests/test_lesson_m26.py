# ABOUTME: Tests the M26 "The Sea Talks" fading pilot — consolidation type, no tune block, band
# ABOUTME: named without naming the decoder, read-only GET starting nothing, and full completion.

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sigint101 import radio_control
from sigint101.app import create_app
from sigint101.hardware import RadioProbe
from sigint101.lessons import REQUIRED_STAGES, load_lesson_file
from sigint101.progress import Progress
from tests.test_item_lint import lint_pack

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = REPO_ROOT / "lesson_packs"
PACK = PACK_DIR / "26-ais.yaml"

# Claim guards (METHOD.md): guard the fading invariant rather than exact words.
DECODER_NAME_RE = re.compile(r"\brtl[_\s-]?ais\b|\baisdecoder\b", re.I)
BAND_NAMED_RE = re.compile(
    r"\bmarine\s+vhf\b[^.?!]*\b162\s*(?:mhz|megahertz)\b|\b162\s*mhz\b", re.I
)
EXACT_PAIR_PRETUNED_RE = re.compile(r"\b161\.975\b[^.?!]*\b162\.025\b", re.I)


def _check_faded_frame_claim(text: str) -> list[str]:
    """Guard: faded frame/operate text names the marine VHF 162 MHz band without the decoder."""
    issues: list[str] = []
    if not BAND_NAMED_RE.search(text):
        issues.append("missing marine VHF / 162 MHz band name")
    if DECODER_NAME_RE.search(text):
        issues.append("names the decoder (rtl_ais / aisdecoder)")
    if EXACT_PAIR_PRETUNED_RE.search(text):
        issues.append("pre-gives the exact channel pair (161.975 / 162.025)")
    return issues


def _snapshot_dir(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _strip_tags(html: str) -> str:
    no_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    no_styles = re.sub(r"<style\b[^>]*>.*?</style>", " ", no_scripts, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", no_styles)


def _absent_probe() -> RadioProbe:
    return RadioProbe(present=False, known=True, method="test", detail="no radio in test")


# --- Both-directions probe for the fading prose claim ------------------------------------------


@pytest.mark.parametrize(
    "defect_text",
    [
        "rtl_ais · AIS on 161.975 / 162.025 MHz",
        "Run rtl_ais on marine VHF around 162 MHz.",
        "Marine VHF · 161.975 / 162.025 MHz",
        "Listen on the radio for ships passing by.",
    ],
)
def test_faded_frame_claim_rejects_decoder_names_pretune_or_missing_band(defect_text):
    assert _check_faded_frame_claim(defect_text), f"should reject: {defect_text!r}"


@pytest.mark.parametrize(
    "good_text",
    [
        "Marine VHF · 162 MHz band",
        "Tune to the marine VHF band around 162 MHz, then listen.",
        "Set the dial to 162 MHz in the marine VHF band.",
    ],
)
def test_faded_frame_claim_accepts_band_only_wordings(good_text):
    assert _check_faded_frame_claim(good_text) == []


# --- Pack structure, loader, and item lint -----------------------------------------------------


def test_m26_pack_is_a_valid_faded_consolidation_lesson():
    lesson = load_lesson_file(PACK)
    assert lesson.type == "consolidation"
    assert lesson.id == 26
    assert lesson.slug == "ais"
    assert lesson.tune is None, "faded pilot must remove its tune: block"
    assert lesson.is_faded is True
    assert lesson.stage_order == ("predict", "operate", "observe", "explain")
    for stage in REQUIRED_STAGES["consolidation"]:
        assert lesson.stages[stage].strip(), f"stage {stage!r} must carry real teaching text"
    assert lesson.uses_ais is True
    assert lesson.panels == ["ais"]
    assert lesson.needs_hardware is True
    assert lesson.requires_tx is False


def test_m26_passes_item_lint_without_waivers():
    raw = yaml.safe_load(PACK.read_text(encoding="utf-8"))
    assert "tune" not in raw
    assert lint_pack(raw) == []


def test_m26_keeps_facts_predict_choices_and_mmsi_reveal_unchanged():
    lesson = load_lesson_file(PACK)
    assert lesson.predict_kind == "knowledge"
    assert lesson.answer == ("mmsi",)
    assert [c["key"] for c in lesson.choices] == ["timing", "onemsg", "mmsi"]
    assert "explain" in lesson.sealed_stages
    assert "mmsi" in lesson.stages["explain"].lower()
    assert "m.1371" in lesson.stages["explain"].lower()
    assert "aivdm" in lesson.stages["explain"].lower()
    assert not DECODER_NAME_RE.search(lesson.concept)
    for stage, body in lesson.stages.items():
        assert not DECODER_NAME_RE.search(body), f"stage {stage} must not name decoder"
    assert _check_faded_frame_claim(lesson.stages["operate"]) == []


# --- Lesson page rendering, honest untuned prompt, and zero-side-effect GET --------------------


def test_m26_page_names_band_without_decoder_and_shows_honest_untuned_prompt(tmp_path):
    app = create_app(pack_dir=PACK_DIR, data_dir=tmp_path, probe=_absent_probe)
    with TestClient(app) as client:
        # Before predict: frozen context frame names only the marine VHF 162 MHz band, no decoder.
        before = client.get("/lesson/26")
        assert before.status_code == 200
        before_html = before.text
        before_visible = _strip_tags(before_html)
        assert not DECODER_NAME_RE.search(before_html)
        assert not EXACT_PAIR_PRETUNED_RE.search(before_visible)
        assert BAND_NAMED_RE.search(before_visible)
        assert "Marine VHF" in before_visible and "162 MHz" in before_visible
        # Sealed stages are not in the HTML yet (only the locked rail pip names them).
        assert "data-ais-faded" not in before_html
        assert "Tune to marine VHF" not in before_html

        # Commit prediction -> operate, observe, explain unlock.
        post = client.post(
            "/api/predict?lesson_id=26",
            data={"prediction": "mmsi"},
            follow_redirects=False,
        )
        assert post.status_code == 303

        after = client.get("/lesson/26")
        assert after.status_code == 200
        after_html = after.text
        after_visible = _strip_tags(after_html)
        assert not DECODER_NAME_RE.search(after_html)
        assert "data-ais-faded" in after_html
        assert 'data-faded-tune="ais"' in after_html
        # Honest untuned state: never silent, never claims live/decode/lock before learner tunes.
        assert "UNTUNED" in after_visible
        assert "Not tuned yet" in after_visible
        assert "LIVE" not in after_visible
        # Dial starts off-band (not pre-tuned to 162 MHz).
        assert 'data-freq-hz="100000000"' in after_html
        assert 'data-stage="operate"' in after_html
        assert 'data-stage="observe"' in after_html
        assert 'data-stage="explain"' in after_html


def test_m26_get_writes_nothing_and_starts_nothing(tmp_path):
    # Seed a prior completed lesson (L1) so we also verify GET /lesson/26 doesn't write retrieval.
    prog = Progress(tmp_path / "progress.db")
    prog.mark_opened(1)
    prog.record_prediction(1, "peaks")
    prog.mark_completed(1)
    prog.close()

    calls: list[str] = []

    class _SpySweeper:
        low = 432_920_000
        high = 434_920_000
        bin_size = 25_000
        target = 433_920_000

        def retune(self, *args, **kwargs):
            calls.append(f"sweeper.retune({args})")

        def stop(self):
            pass

        def release(self):
            calls.append("sweeper.release")

        def resume(self):
            calls.append("sweeper.resume")

    class _SpyAis:
        def start(self):
            calls.append("ais.start")

        def stop(self):
            pass

        def release(self):
            calls.append("ais.release")

        def resume(self):
            calls.append("ais.resume")

        def read(self, *args, **kwargs):
            calls.append("ais.read")
            return {"available": False, "vessels": []}

    app = create_app(
        pack_dir=PACK_DIR,
        data_dir=tmp_path,
        probe=_absent_probe,
        sweeper_factory=_SpySweeper,
        ais_factory=_SpyAis,
    )
    with TestClient(app) as client:
        calls.clear()
        before_files = _snapshot_dir(tmp_path)
        resp = client.get("/lesson/26")
        after_files = _snapshot_dir(tmp_path)

    assert resp.status_code == 200
    assert calls == [], f"GET /lesson/26 must start/retune nothing, saw: {calls}"
    assert after_files == before_files, "GET /lesson/26 must write nothing to data_dir"


def test_m26_is_completable_and_honest_when_tuned(tmp_path):
    class _EmptyLiveAis:
        def __init__(self):
            self.running = False
            self.starts = 0
            self.reads = 0
            self.watch = type("_Watch", (), {"settling": staticmethod(lambda: False)})()

        def start(self):
            self.running = True
            self.starts += 1

        def stop(self):
            self.running = False

        def release(self):
            self.running = False

        def resume(self):
            pass

        def live_command(self):
            return "rtl_ais -n"

        def vessels(self, limit=None):
            self.reads += 1
            return []

    spy_ais = _EmptyLiveAis()
    app = create_app(
        pack_dir=PACK_DIR,
        data_dir=tmp_path,
        probe=lambda: RadioProbe(present=True, known=True, method="test", detail="test"),
        ais_factory=lambda: spy_ais,
    )
    with TestClient(app) as client:
        # 1) GET /lesson/26 starts nothing.
        assert client.get("/lesson/26").status_code == 200
        assert spy_ais.starts == 0
        assert spy_ais.reads == 0

        # 2) Commit prediction -> operate stage opens.
        r_pred = client.post(
            "/api/predict?lesson_id=26", data={"prediction": "mmsi"}, follow_redirects=False
        )
        assert r_pred.status_code == 303

        # 3) Walk through operate -> observe -> explain and complete lesson.
        for stage in ("operate", "observe", "explain"):
            r_st = client.post("/api/stage?lesson_id=26", data={"stage": stage})
            assert r_st.status_code == 200

        # 4) Once tuned to marine VHF 162 MHz, /api/ais answers honestly (empty inland sea).
        r_ais = client.get("/api/ais?limit=12")
        assert r_ais.status_code == 200
        assert r_ais.json()["vessels"] == []
        assert spy_ais.starts == 1
        assert spy_ais.reads == 1

        # 5) Complete lesson succeeds and persists.
        r_done = client.post("/api/complete?lesson_id=26")
        assert r_done.status_code == 200
        assert app.state.progress.get(26)["completed"] == 1


def test_m26_faded_tune_strip_and_badge_fonts_are_at_least_18px():
    css = (
        Path(__file__).resolve().parent.parent / "sigint101" / "static" / "app.css"
    ).read_text(encoding="utf-8")

    def _font_px(selector: str) -> float | None:
        pat = re.compile(re.escape(selector) + r"\s*\{([^}]*)\}", re.S)
        m = pat.search(css)
        if not m:
            return None
        fm = re.search(r"font-size:\s*([\d.]+)px", m.group(1))
        return float(fm.group(1)) if fm else None

    for selector in (
        ".tune--faded .tune__caption",
        ".tune--faded .tune__freq span",
        "[data-ais-faded] .decoder__title",
        "[data-ais-faded] .decoder__status",
    ):
        px = _font_px(selector)
        assert px is not None and px >= 18.0, f"{selector} font-size is {px!r}px; expected >= 18px"


def test_m26_load_sequence_leaves_rack_sweep_aprs_and_running_rows_untouched(tmp_path):
    """F5: opening a faded pack (GET + POST /api/tuning + POST /api/opened) retunes nothing."""
    retunes: list[tuple[str, tuple]] = []

    class _RunningRackSweeper:
        low = 432_920_000
        high = 434_920_000
        bin_size = 25_000
        target = 433_920_000

        def __init__(self):
            self.running = True
            self.rows = [{"low": self.low, "high": self.high, "bins": [-45.0, -40.0]}]

        def retune(self, low, high, bin_size, target=None):
            retunes.append(("sweeper", (low, high, bin_size, target)))
            self.low, self.high, self.bin_size, self.target = low, high, bin_size, target
            self.running = False
            self.rows.clear()

        def stop(self):
            self.running = False

        def release(self):
            retunes.append(("sweeper.release", ()))

        def resume(self):
            pass

    class _RackAprs:
        home_hz = 144_390_000

        def __init__(self):
            self.freq_hz = 144_800_000
            self.running = False

        def retune(self, freq_hz):
            retunes.append(("aprs", (freq_hz,)))
            self.freq_hz = freq_hz

        def stop(self):
            self.running = False

        def release(self):
            pass

        def resume(self):
            pass

    class _SpyAis:
        running = False

        def start(self):
            retunes.append(("ais.start", ()))

        def stop(self):
            pass

        def release(self):
            pass

        def resume(self):
            pass

    sweeper = _RunningRackSweeper()
    aprs = _RackAprs()
    app = create_app(
        pack_dir=PACK_DIR,
        data_dir=tmp_path,
        probe=_absent_probe,
        sweeper_factory=lambda: sweeper,
        aprs_factory=lambda: aprs,
        ais_factory=_SpyAis,
    )
    with TestClient(app) as client:
        epoch, at = app.state.tuning_gate.anchor()
        assert client.get("/lesson/26").status_code == 200
        r_tune = client.post(f"/api/tuning?lesson_id=26&epoch={epoch}&at={at}")
        assert r_tune.status_code == 200
        assert r_tune.json() == {"tuned": False, "lesson_id": 26, "faded": True}
        assert client.post("/api/opened?lesson_id=26").status_code == 200

        assert retunes == [], f"faded load sequence must not retune or start receivers: {retunes}"
        assert (sweeper.low, sweeper.high, sweeper.target) == (
            432_920_000,
            434_920_000,
            433_920_000,
        )
        assert sweeper.running is True
        assert len(sweeper.rows) == 1, "running sweeper must keep its buffered rows"
        assert aprs.freq_hz == 144_800_000


def test_m26_faded_load_preserves_c81_action_ordering(tmp_path):
    """F5: faded restore skips receivers while still participating in C81's action ordering."""
    app = create_app(pack_dir=PACK_DIR, data_dir=tmp_path, probe=_absent_probe)
    with TestClient(app) as client:
        # Leave the rack tuned away from home.
        app.state.aprs.freq_hz = 144_800_000
        app.state.sweeper.low = 432_920_000
        app.state.sweeper.high = 434_920_000
        app.state.sweeper.target = 433_920_000
        epoch, base = app.state.tuning_gate.anchor()

        # Learner opens faded L26 at base - 10; a delayed L1 restore stamped earlier is stale.
        r_faded = client.post(f"/api/tuning?lesson_id=26&epoch={epoch}&at={base - 10}")
        assert r_faded.status_code == 200
        assert r_faded.json() == {"tuned": False, "lesson_id": 26, "faded": True}

        r_stale_l1 = client.post(f"/api/tuning?lesson_id=1&epoch={epoch}&at={base - 20}")
        assert r_stale_l1.status_code == 409
        assert r_stale_l1.json()["stale"] is True
        assert app.state.aprs.freq_hz == 144_800_000
        assert app.state.sweeper.target == 433_920_000

        # Opening non-faded L1 later still restores receivers and supersedes an older L26 POST.
        r_l1 = client.post(f"/api/tuning?lesson_id=1&epoch={epoch}&at={base}")
        assert r_l1.status_code == 200
        assert r_l1.json() == {"tuned": True, "lesson_id": 1}
        assert app.state.aprs.freq_hz == app.state.aprs.home_hz

        r_stale_faded = client.post(f"/api/tuning?lesson_id=26&epoch={epoch}&at={base - 5}")
        assert r_stale_faded.status_code == 409
        assert r_stale_faded.json()["stale"] is True


def _make_parked_rack(parked_step: str):
    """Build a rack sweeper and APRS fake where `parked_step` blocks after step admission."""
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    class _ParkedSweeper:
        low = 432_920_000
        high = 434_920_000
        bin_size = 25_000
        target = 433_920_000

        def __init__(self):
            self.running = True
            self.rows = [{"low": self.low, "high": self.high, "bins": [-45.0, -40.0]}]

        def retune(self, low, high, bin_size, target=None):
            if parked_step == "sweep":
                entered.set()
                assert release.wait(5), "parked sweep step was never released"
            self.low, self.high, self.bin_size, self.target = low, high, bin_size, target
            self.running = False
            self.rows.clear()
            order.append("sweep_step_done")

        def stop(self):
            self.running = False

        def release(self):
            pass

        def resume(self):
            pass

    class _ParkedAprs:
        home_hz = 144_390_000

        def __init__(self):
            self.freq_hz = 144_800_000
            self.running = False

        def retune(self, freq_hz):
            if parked_step == "aprs":
                entered.set()
                assert release.wait(5), "parked APRS step was never released"
            self.freq_hz = freq_hz
            order.append("aprs_step_done")

        def stop(self):
            self.running = False

        def release(self):
            pass

        def resume(self):
            pass

    return _ParkedSweeper(), _ParkedAprs(), entered, release, order


@pytest.mark.parametrize("parked_step", ["aprs", "sweep"])
def test_m26_faded_load_fences_already_admitted_restore_step_before_settling(
    tmp_path, parked_step
):
    """F5: faded load must not report settled while an already-admitted older step can write."""
    sweeper, aprs, entered, release, order = _make_parked_rack(parked_step)
    app = create_app(
        pack_dir=PACK_DIR,
        data_dir=tmp_path,
        probe=_absent_probe,
        sweeper_factory=lambda: sweeper,
        aprs_factory=lambda: aprs,
    )
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        epoch, base = app.state.tuning_gate.anchor()
        old_future = pool.submit(
            client.post, f"/api/tuning?lesson_id=1&epoch={epoch}&at={base - 20}"
        )
        try:
            assert entered.wait(3), f"older restore never entered {parked_step} step"
            assert client.get("/lesson/26").status_code == 200
            faded_done = threading.Event()

            def _run_faded():
                resp = client.post(f"/api/tuning?lesson_id=26&epoch={epoch}&at={base - 5}")
                order.append("faded_returned")
                faded_done.set()
                return resp

            faded_future = pool.submit(_run_faded)
            assert client.post("/api/opened?lesson_id=26").status_code == 200

            # Faded POST must stay fenced (not settled) while the admitted step is still in flight.
            assert not faded_done.wait(0.15), (
                f"faded load reported settled before parked {parked_step} step finished"
            )
            # Newer faded action must already be published so subsequent older steps cancel.
            assert app.state.tuning_gate._newest_restore == pytest.approx(base - 5)
        finally:
            release.set()

        old_resp = old_future.result(timeout=5)
        faded_resp = faded_future.result(timeout=5)
        sweeper_running = sweeper.running

    assert faded_resp.status_code == 200
    assert faded_resp.json() == {"tuned": False, "lesson_id": 26, "faded": True}
    step_label = f"{parked_step}_step_done"
    assert step_label in order and "faded_returned" in order
    assert order.index(step_label) < order.index("faded_returned"), (
        f"admitted {parked_step} step wrote after faded load settled: {order}"
    )
    if parked_step == "aprs":
        assert "sweep_step_done" not in order
        assert old_resp.json()["skipped"]["sweeper"] == radio_control.NEWER_LESSON
        assert (sweeper.low, sweeper.high, sweeper.target) == (
            432_920_000,
            434_920_000,
            433_920_000,
        )
        assert sweeper_running is True and len(sweeper.rows) == 1


@pytest.mark.parametrize("parked_step", ["aprs", "sweep"])
def test_m26_faded_load_reports_busy_when_admitted_restore_exceeds_wait_bound(
    tmp_path, monkeypatch, parked_step
):
    """F5: bounded fence refuses to report settled when an older step outlasts TUNING_WAIT_S."""
    monkeypatch.setattr(radio_control, "TUNING_WAIT_S", 0.05)
    sweeper, aprs, entered, release, order = _make_parked_rack(parked_step)
    app = create_app(
        pack_dir=PACK_DIR,
        data_dir=tmp_path,
        probe=_absent_probe,
        sweeper_factory=lambda: sweeper,
        aprs_factory=lambda: aprs,
    )
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        epoch, base = app.state.tuning_gate.anchor()
        old_future = pool.submit(
            client.post, f"/api/tuning?lesson_id=1&epoch={epoch}&at={base - 20}"
        )
        try:
            assert entered.wait(3), f"older restore never entered {parked_step} step"
            assert client.get("/lesson/26").status_code == 200
            faded_resp = client.post(f"/api/tuning?lesson_id=26&epoch={epoch}&at={base - 5}")
            assert client.post("/api/opened?lesson_id=26").status_code == 200
            assert faded_resp.status_code == 503
            body = faded_resp.json()
            assert body["tuned"] is False and body["faded"] is True and body["busy"] is True
            assert "earlier restore" in body["reason"].lower()
        finally:
            release.set()

        old_resp = old_future.result(timeout=5)
        sweeper_running = sweeper.running

    if parked_step == "aprs":
        assert "sweep_step_done" not in order
        assert old_resp.json()["skipped"]["sweeper"] == radio_control.NEWER_LESSON
        assert sweeper.target == 433_920_000 and sweeper_running is True and len(sweeper.rows) == 1


def test_m26_faded_load_preserves_launch_permit_and_cancels_queued_older_restore(tmp_path):
    """F5: faded load does not block on a held claim permit and cancels queued restores."""
    sweeper, aprs, _, _, order = _make_parked_rack("none")
    app = create_app(
        pack_dir=PACK_DIR,
        data_dir=tmp_path,
        probe=_absent_probe,
        sweeper_factory=lambda: sweeper,
        aprs_factory=lambda: aprs,
    )
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        gate = app.state.tuning_gate
        epoch, base = gate.anchor()
        assert gate.admit_claim() is True
        old_future = pool.submit(
            client.post, f"/api/tuning?lesson_id=1&epoch={epoch}&at={base - 20}"
        )
        try:
            # Wait until the older restore is registered and queued behind our claim permit.
            deadline = radio_control.time.monotonic() + 3.0
            while not gate.state()["restoring"]:
                assert radio_control.time.monotonic() < deadline
                radio_control.time.sleep(0.01)

            faded_resp = client.post(f"/api/tuning?lesson_id=26&epoch={epoch}&at={base - 5}")
            assert faded_resp.status_code == 200
            assert faded_resp.json() == {"tuned": False, "lesson_id": 26, "faded": True}
            # Launch permit is still held intact.
            assert gate._claims == 1
        finally:
            gate.end_claim()

        old_resp = old_future.result(timeout=5)
        sweeper_running = sweeper.running

    assert old_resp.status_code == 200
    assert old_resp.json()["tuned"] is False
    assert old_resp.json()["skipped"]["aprs"] == radio_control.NEWER_LESSON
    assert old_resp.json()["skipped"]["sweeper"] == radio_control.NEWER_LESSON
    assert order == []
    assert aprs.freq_hz == 144_800_000
    assert sweeper.target == 433_920_000 and sweeper_running is True and len(sweeper.rows) == 1


def test_m26_faded_load_overtaken_while_fencing_parked_step_returns_stale(tmp_path):
    """F5: a newer lesson arriving while a faded load waits on _step supersedes the faded load."""
    sweeper, aprs, entered, release, _ = _make_parked_rack("aprs")
    app = create_app(
        pack_dir=PACK_DIR,
        data_dir=tmp_path,
        probe=_absent_probe,
        sweeper_factory=lambda: sweeper,
        aprs_factory=lambda: aprs,
    )
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        gate = app.state.tuning_gate
        epoch, base = gate.anchor()
        old_future = pool.submit(
            client.post, f"/api/tuning?lesson_id=1&epoch={epoch}&at={base - 30}"
        )
        try:
            assert entered.wait(3)
            faded_future = pool.submit(
                client.post, f"/api/tuning?lesson_id=26&epoch={epoch}&at={base - 15}"
            )
            deadline = radio_control.time.monotonic() + 3.0
            while gate._newest_restore < base - 15:
                assert radio_control.time.monotonic() < deadline
                radio_control.time.sleep(0.01)
            # A newer lesson overtakes the waiting faded load before the parked step releases.
            with gate._state:
                gate._newest_restore = base - 5
        finally:
            release.set()

        old_future.result(timeout=5)
        faded_resp = faded_future.result(timeout=5)

    assert faded_resp.status_code == 409
    assert faded_resp.json()["stale"] is True
