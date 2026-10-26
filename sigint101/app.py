# ABOUTME: The SIGINT 101 console — a FastAPI app that serves the touch shell end to end.
# ABOUTME: Wires lessons/progress/events/spectrum with server-side commit-before-reveal.

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

# Form bodies are decoded with parse_qs rather than Starlette's request.form(). Starlette pulls
# in python-multipart for form parsing even when the body is plain urlencoded, and this console
# keeps its dependency list short enough to install on an ARM board offline.
from urllib.parse import parse_qs

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .events import log_event
from .hardware import list_rtlsdr_devices, probe_rtlsdr
from .lessons import load_lessons
from .progress import Progress
from .spectrum import SpectrumUnavailable, Sweeper, TuneError, validate_span

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent
PACK_DIR = REPO_DIR / "lesson_packs"
DATA_DIR = REPO_DIR / "data"

# The line glyph a home tile falls back to. The waterfall glyph is the one every lesson earns —
# the spectrogram is the instrument the whole course is built on.
DEFAULT_TILE_GLYPH = "waterfall"

# Env var that pins which radio to sweep when the bus holds more than one. A serial, not an index:
# the index changes with plug order, so "00000001" handed to rtl_power's -d is read as index 1.
RTLSDR_SERIAL_ENV = "SIGINT101_RTLSDR_SERIAL"


async def _field(request: Request, name: str) -> str:
    """Pull one field from a posted body — urlencoded form or JSON.

    Starlette's request.form() needs python-multipart. The predict gate posts a plain urlencoded
    form, which parse_qs handles from the stdlib, so the Nomad gains no dependency for one
    radio-button field. JSON is accepted too, for curl and tests.
    """
    raw = (await request.body()).decode("utf-8", "replace")
    if "application/json" in request.headers.get("content-type", ""):
        try:
            return str(json.loads(raw).get(name, ""))
        except (ValueError, AttributeError):
            return ""
    return parse_qs(raw).get(name, [""])[0]


def _home_tiles(lessons: dict, progress: Progress) -> list:
    """Build the home grid: one tile per lesson, in id order, carrying its progress.

    There are no artificial gates here (D16), so no tile is ever locked — every lesson is
    reachable. A finished lesson reads as done; the first lesson not yet finished reads as active
    ("you are here"); the rest are plainly available. The tiles are plain dicts because the
    home template reads them by attribute/key, and a lesson has no "unit" grouping to model.
    """
    tiles = []
    active_marked = False
    for lesson_id in sorted(lessons):
        lesson = lessons[lesson_id]
        done = bool((progress.get(lesson_id) or {}).get("completed"))
        if done:
            status = "done"
        elif not active_marked:
            status = "active"
            active_marked = True
        else:
            status = ""
        tiles.append({
            "id": lesson.id,
            "code": f"L{lesson.id}",
            "name": lesson.title,
            "glyph": DEFAULT_TILE_GLYPH,
            "status": status,
            "lessons_total": 1,
            "lessons_done": 1 if done else 0,
        })
    return tiles


def create_app(*, pack_dir=PACK_DIR, data_dir=DATA_DIR, probe=probe_rtlsdr,
               sweeper_factory=None) -> FastAPI:
    """Build the console with its collaborators injectable.

    Tests hand in a fixture pack, a temp data dir, their own probe, and a sweeper that truthfully
    reports "no radio" — which is how the suite honours the no-mock-hardware invariant with no
    dongle attached. The module-level `app` below uses the real ones.
    """
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    # The sweeper trusts the same probe the console does, so a radio-less probe in a test can
    # never reach for the real dongle. The real enumerator is wired here and nowhere else: it is
    # what makes the sweeper check which radio it is about to open, and refuse rather than open
    # the wrong one when the bus holds several and none is pinned.
    if sweeper_factory is None:
        def build_sweeper():
            return Sweeper(
                radio_present=lambda: probe().present,
                serial=os.environ.get(RTLSDR_SERIAL_ENV),
                list_devices=list_rtlsdr_devices,
            )
    else:
        build_sweeper = sweeper_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Open the console's state for the server's lifetime, and put it away cleanly.

        The sweeper must not outlive the server: a leaked rtl_power would keep the dongle claimed,
        and the next start would find the radio busy with nothing visibly running.
        """
        app.state.lessons = load_lessons(pack_dir)
        app.state.progress = Progress(Path(data_dir) / "progress.db")
        app.state.events_path = Path(data_dir) / "events.jsonl"
        app.state.sweeper = build_sweeper()
        try:
            yield
        finally:
            app.state.sweeper.stop()
            app.state.progress.close()

    app = FastAPI(title="SIGINT 101", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def _require_lesson(lesson_id: int):
        """Refuse an unknown lesson id the same way from every lesson route."""
        if lesson_id not in app.state.lessons:
            raise HTTPException(status_code=404, detail=f"lesson {lesson_id} not found")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        """The front page: a tile per lesson, with how far the learner has got."""
        tiles = _home_tiles(app.state.lessons, app.state.progress)
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context={"title": "SIGINT 101", "back_href": "/", "back_label": "Home",
                     "units": tiles},
        )

    @app.get("/lesson/{lesson_id}", response_class=HTMLResponse)
    def lesson_view(request: Request, lesson_id: int):
        """Render one lesson, sealing every post-predict stage until a prediction exists.

        Commit-before-reveal is enforced here, server-side. Whether the learner has committed is
        read from the persisted prediction (`get_prediction(...) is not None`); that single bool
        is what the template uses to decide which stages it emits, so the sealed bodies are never
        put into the HTML before the commit. Lesson.reveal() is asked for the sealed material only
        once committed — it refuses otherwise — as the code-level backstop behind the template.
        Opening a lesson counts as progress but never as completion.
        """
        _require_lesson(lesson_id)
        lesson = app.state.lessons[lesson_id]
        app.state.progress.mark_opened(lesson_id)
        prediction = app.state.progress.get_prediction(lesson_id)
        committed = prediction is not None
        revealed = lesson.reveal(prediction_committed=True) if committed else None
        return templates.TemplateResponse(
            request=request,
            name="console.html",
            context={
                "title": lesson.title,
                "back_href": "/",
                "back_label": "Home",
                "lesson": lesson,
                "current_stage": lesson.sealed_stages[0] if committed else lesson.stage_order[0],
                "prediction_committed": committed,
                "committed_choice": prediction,
                "predict_choices": lesson.choices,
                "commit_url": f"/api/predict?lesson_id={lesson.id}",
                "revealed": revealed,
            },
        )

    @app.post("/api/predict")
    async def predict(lesson_id: int, request: Request):
        """Record the learner's guess. Nothing past predict unlocks without one.

        Fails closed on a blank guess (record_prediction raises), because an empty prediction is
        not a commitment and accepting it would unlock the reveal for free. On success the browser
        is redirected back to the now-unsealed lesson, so the gate works with no JavaScript.
        """
        _require_lesson(lesson_id)
        try:
            app.state.progress.record_prediction(lesson_id, await _field(request, "prediction"))
        except ValueError as exc:
            log_event(app.state.events_path, "prediction_refused",
                      {"lesson": lesson_id, "reason": str(exc)})
            return JSONResponse(status_code=400, content={"recorded": False, "reason": str(exc)})
        log_event(app.state.events_path, "prediction", {"lesson": lesson_id})
        return RedirectResponse(f"/lesson/{lesson_id}", status_code=303)

    @app.post("/api/complete")
    def complete(lesson_id: int):
        """Mark a lesson finished — refused (400) until its prediction was made.

        The teaching loop starts with a guess, so a completion with no recorded prediction is a
        loop run in the wrong order; it is refused rather than silently accepted.
        """
        _require_lesson(lesson_id)
        if app.state.progress.get_prediction(lesson_id) is None:
            reason = "predict before completing; the loop starts with a guess"
            log_event(app.state.events_path, "completion_refused",
                      {"lesson": lesson_id, "reason": reason})
            return JSONResponse(status_code=400, content={"completed": False, "reason": reason})
        app.state.progress.mark_completed(lesson_id)
        log_event(app.state.events_path, "completed", {"lesson": lesson_id})
        return {"completed": True, "lesson": lesson_id}

    @app.get("/api/spectrum")
    def spectrum(limit: int | None = None):
        """Recent real sweeps. No radio is an answer (503 with the reason), never a fake row."""
        sweeper = app.state.sweeper
        # Start lazily on first request, not at boot: the service comes up before the USB bus
        # settles after a power cut, and a boot-time start would wrongly conclude "no radio" and
        # stay dark until a restart.
        if not sweeper.running:
            try:
                sweeper.start()
            except SpectrumUnavailable as exc:
                log_event(app.state.events_path, "spectrum_blocked", {"reason": str(exc)})
                return JSONResponse(status_code=503,
                                    content={"available": False, "reason": str(exc), "rows": []})
            log_event(app.state.events_path, "spectrum_start",
                      {"command": " ".join(sweeper.command())})
        # The span rides along with every response because the waterfall cannot label its axis
        # (or spot a retune) from bare rows of dB values. bin_size is the requested bin — a
        # maximum rtl_power may undercut — while each row's freq_step reports what the hardware
        # actually delivered. gain is None while the tuner runs automatic.
        return {
            "available": True,
            "freq_low": sweeper.low,
            "freq_high": sweeper.high,
            "bin_size": sweeper.bin_size,
            "gain": sweeper.gain,
            "rows": [row.as_dict() for row in sweeper.rows(limit)],
        }

    @app.get("/api/hardware")
    def hardware():
        """What the radio probe sees right now — present, how it looked, and the detail.

        Re-probed on every call, so a dongle unplugged after boot shows up here instead of a stale
        "present". Absence is reported as data, never masked.
        """
        radio = probe()
        return {"present": radio.present, "method": radio.method, "detail": radio.detail}

    @app.post("/api/tune")
    def tune(payload: dict = Body(default={})):
        """Point the sweep at a learner-chosen span, failing closed with the reason.

        The span meets validate_span first, so a span this radio cannot reach is refused (400)
        with the gate's own words, whether or not a dongle is attached. A valid span with no radio
        to drive is a 503 — honest about why nothing retuned, rather than reporting a success the
        hardware never performed.
        """
        try:
            low, high, bin_size = validate_span(
                payload.get("low"), payload.get("high"), payload.get("bin_size"))
        except TuneError as exc:
            log_event(app.state.events_path, "tune_rejected", {"reason": str(exc)})
            return JSONResponse(status_code=400, content={"tuned": False, "reason": str(exc)})

        radio = probe()
        if not radio.present:
            reason = f"no RTL-SDR to tune — {radio.detail}"
            log_event(app.state.events_path, "tune_blocked", {"reason": reason})
            return JSONResponse(status_code=503, content={"tuned": False, "reason": reason})

        app.state.sweeper.retune(low, high, bin_size)
        tuned = {"low": low, "high": high, "bin_size": bin_size}
        log_event(app.state.events_path, "tune", tuned)
        return {"tuned": True, **tuned}

    return app


app = create_app()
