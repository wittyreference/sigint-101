# ABOUTME: The SIGINT 101 console — a FastAPI app that serves the touch shell end to end.
# ABOUTME: Wires lessons/progress/events/spectrum with server-side commit-before-reveal.

from __future__ import annotations

import json
import os
import time
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

from .acars import AcarsDecoder, AcarsUnavailable
from .adsb import DEFAULT_ADSB_HZ, AdsbDecoder, AdsbUnavailable
from .aprs import AprsDecoder, AprsUnavailable
from .decode import Decoder, DecodeUnavailable
from .events import log_event
from .exam import EXAM_SIZE, PASS_MARK, ExamStore, build_exam, load_pool, score_exam
from .flock import FlockDetector, FlockUnavailable
from .hardware import list_rtlsdr_devices, probe_rtlsdr
from .lessons import load_lessons
from .lora import LoRaListener, LoRaUnavailable
from .pocsag import PocsagDecoder, PocsagUnavailable
from .ppm import measure_pilot_ppm
from .progress import Progress
from .provenance import (
    compare_to_baseline,
    gather_provenance,
    read_baseline,
    record_baseline_sample,
)
from .rds import RDSDecoder, RDSUnavailable
from .spectrum import SpectrumUnavailable, Sweeper, TuneError, validate_span
from .units import UNITS, get_unit

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

# Where the ADS-B radar is standing. Set to the receiver's own latitude/longitude, the scope turns
# each aircraft's position into a range and bearing; left unset, the scope lists contacts without
# placing them, because a range drawn from a guessed location would be a real-looking lie.
RX_LAT_ENV = "SIGINT101_RX_LAT"
RX_LON_ENV = "SIGINT101_RX_LON"


def _env_float(name: str):
    """Read an environment value as a float, or None if unset or not a number."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None

# A ppm measurement captures this many seconds of IQ from one station. Shorter than the module
# default so the kiosk's "Measure ppm" tap returns promptly; the trade-off is resolution, which the
# measurement reports so the learner sees how finely it could read.
PPM_SECONDS = 8.0


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


def _is_done(progress: Progress, lesson_id: int) -> bool:
    """Has this lesson been completed? (Opening it is progress, not completion.)"""
    return bool((progress.get(lesson_id) or {}).get("completed"))


def _unit_tiles(lessons: dict, progress: Progress) -> list:
    """Build the home grid: one tile per curriculum unit, carrying its lesson progress.

    No tile is ever locked (D16). A unit counts only the lessons whose packs have loaded, so a
    unit with nothing built yet reads 0/0 and drills into an honest empty page rather than
    pretending to be gated. The first unit that has lessons and is not fully finished reads as
    active ("you are here"); a fully-finished unit reads done; the rest are plainly available.
    """
    tiles = []
    active_marked = False
    for unit in UNITS:
        in_unit = [lessons[i] for i in unit.lesson_ids if i in lessons]
        total = len(in_unit)
        done = sum(1 for lesson in in_unit if _is_done(progress, lesson.id))
        if total and done == total:
            status = "done"
        elif total and not active_marked:
            status = "active"
            active_marked = True
        else:
            status = ""
        tiles.append({
            "id": unit.id,
            "code": f"U{unit.id}",
            "name": unit.title,
            "glyph": unit.glyph,
            "status": status,
            "href": f"/unit/{unit.id}",
            "lessons_total": total,
            "lessons_done": done,
        })
    return tiles


def _lesson_tiles(unit, lessons: dict, progress: Progress) -> list:
    """Build a unit page's grid: one tile per loaded lesson in the unit, in curriculum order.

    Mirrors the unit tiles' status logic one level down: a finished lesson reads done, the first
    unfinished one reads active, the rest are available. Unbuilt lessons are simply skipped — not
    shown as locked (D16) — so the page lists only what a learner can actually open today.
    """
    tiles = []
    active_marked = False
    for lesson_id in unit.lesson_ids:
        if lesson_id not in lessons:
            continue
        lesson = lessons[lesson_id]
        done = _is_done(progress, lesson_id)
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
            "href": f"/lesson/{lesson.id}",
            "lessons_total": 1,
            "lessons_done": 1 if done else 0,
        })
    return tiles


def create_app(*, pack_dir=PACK_DIR, data_dir=DATA_DIR, probe=probe_rtlsdr,
               sweeper_factory=None, decoder_factory=None, lora_factory=None,
               rds_factory=None, adsb_factory=None, aprs_factory=None, pocsag_factory=None,
               acars_factory=None, flock_factory=None, pool_path=None,
               ppm_measure=None) -> FastAPI:
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

    # The decoder shares the sweeper's radio and its enumerator, so the two agree on which dongle
    # they are taking turns on. Wired here and nowhere else, same as the sweeper.
    if decoder_factory is None:
        def build_decoder():
            return Decoder(
                radio_present=lambda: probe().present,
                serial=os.environ.get(RTLSDR_SERIAL_ENV),
                list_devices=list_rtlsdr_devices,
            )
    else:
        build_decoder = decoder_factory

    # The LoRa listener reads the local meshtasticd over its TCP API — a separate radio from the
    # RTL-SDR, so it needs no arbitration with the sweeper or decoder. The real default connects to
    # localhost:4403; tests inject their own listener.
    if lora_factory is None:
        build_lora = LoRaListener
    else:
        build_lora = lora_factory

    # The RDS decoder runs rtl_fm|redsea, so it holds the one dongle the sweeper and rtl_433 decoder
    # also want — it joins their release/resume turn-taking. Wired to the same probe and enumerator.
    if rds_factory is None:
        def build_rds():
            return RDSDecoder(
                radio_present=lambda: probe().present,
                serial=os.environ.get(RTLSDR_SERIAL_ENV),
                list_devices=list_rtlsdr_devices,
            )
    else:
        build_rds = rds_factory

    # The ADS-B decoder runs dump1090, which holds the same one dongle; it joins the sweeper /
    # rtl_433 / rtl_fm release/resume turn-taking. Its receiver location comes from the environment
    # so the scope can place aircraft; wired to the same probe and enumerator as the rest.
    if adsb_factory is None:
        def build_adsb():
            return AdsbDecoder(
                radio_present=lambda: probe().present,
                serial=os.environ.get(RTLSDR_SERIAL_ENV),
                list_devices=list_rtlsdr_devices,
                receiver_lat=_env_float(RX_LAT_ENV),
                receiver_lon=_env_float(RX_LON_ENV),
            )
    else:
        build_adsb = adsb_factory

    # The APRS decoder runs rtl_fm|multimon-ng, so it holds the one dongle the sweeper and the other
    # SDR tools also want — it joins their release/resume turn-taking, wired to the same enumerator.
    if aprs_factory is None:
        def build_aprs():
            return AprsDecoder(
                radio_present=lambda: probe().present,
                serial=os.environ.get(RTLSDR_SERIAL_ENV),
                list_devices=list_rtlsdr_devices,
            )
    else:
        build_aprs = aprs_factory

    # The POCSAG decoder runs rtl_fm|multimon-ng on the pager band, so it holds the one dongle too —
    # it joins the sweeper / rtl_433 / rds / adsb / aprs release/resume turn-taking.
    if pocsag_factory is None:
        def build_pocsag():
            return PocsagDecoder(
                radio_present=lambda: probe().present,
                serial=os.environ.get(RTLSDR_SERIAL_ENV),
                list_devices=list_rtlsdr_devices,
            )
    else:
        build_pocsag = pocsag_factory

    # The ACARS decoder runs acarsdec, which opens the dongle directly on the VHF ACARS channel — it
    # joins the sweeper / rtl_433 / rds / adsb / aprs / pocsag release/resume turn-taking.
    if acars_factory is None:
        def build_acars():
            return AcarsDecoder(
                radio_present=lambda: probe().present,
                serial=os.environ.get(RTLSDR_SERIAL_ENV),
                list_devices=list_rtlsdr_devices,
            )
    else:
        build_acars = acars_factory

    # The Flock detector runs tcpdump on wlan1 in monitor mode — a separate radio from the RTL-SDR,
    # like the LoRa listener, so it needs no arbitration with the sweeper or the SDR decoders.
    if flock_factory is None:
        build_flock = FlockDetector
    else:
        build_flock = flock_factory

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
        app.state.decoder = build_decoder()
        app.state.lora = build_lora()
        app.state.rds = build_rds()
        app.state.adsb = build_adsb()
        app.state.aprs = build_aprs()
        app.state.pocsag = build_pocsag()
        app.state.acars = build_acars()
        app.state.flock = build_flock()
        # The exam trainer: the verified NCVEC pool (reference data) plus a per-question
        # spaced-retrieval store, persisted beside the lesson progress so study history
        # survives a kiosk power-cut the same way.
        app.state.exam_pool = load_pool(pool_path)
        app.state.exam_store = ExamStore(Path(data_dir) / "exam.db")
        # Injected so a test drives the ppm route without 8 s of real IQ capture; the default is
        # the real pilot measurement, which holds the dongle exclusively (the route stands the
        # sweeper down first, below).
        app.state.ppm_measure = ppm_measure or measure_pilot_ppm
        try:
            yield
        finally:
            app.state.sweeper.stop()
            app.state.decoder.stop()
            app.state.lora.stop()
            app.state.rds.stop()
            app.state.adsb.stop()
            app.state.aprs.stop()
            app.state.pocsag.stop()
            app.state.acars.stop()
            app.state.flock.stop()
            app.state.progress.close()
            app.state.exam_store.close()

    app = FastAPI(title="SIGINT 101", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def _require_lesson(lesson_id: int):
        """Refuse an unknown lesson id the same way from every lesson route."""
        if lesson_id not in app.state.lessons:
            raise HTTPException(status_code=404, detail=f"lesson {lesson_id} not found")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        """The front page: a tile per curriculum unit, with how far the learner has got."""
        tiles = _unit_tiles(app.state.lessons, app.state.progress)
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context={"title": "SIGINT 101", "back_href": "/", "back_label": "Home",
                     "tiles": tiles},
        )

    @app.get("/unit/{unit_id}", response_class=HTMLResponse)
    def unit_view(request: Request, unit_id: int):
        """One unit's page: the lessons it contains, each drilling into the lesson itself.

        An unknown unit is a 404, the same shape a lesson route uses. A real unit with nothing
        built yet renders with an empty grid and says so plainly — it is not locked (D16).
        """
        unit = get_unit(unit_id)
        if unit is None:
            raise HTTPException(status_code=404, detail=f"unit {unit_id} not found")
        tiles = _lesson_tiles(unit, app.state.lessons, app.state.progress)
        return templates.TemplateResponse(
            request=request,
            name="unit.html",
            context={"title": unit.title, "back_href": "/", "back_label": "Home",
                     "unit": unit, "tiles": tiles},
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
        context = {
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
        }
        # A lesson that names its own band starts the instrument there instead of the default FM
        # sweep: the dial reads its centre and step, and the live waterfall carries the band edges
        # the console retunes the sweep to. A lesson with no band leaves these unset, so the
        # template's FM defaults stand and no retune edges are emitted.
        if lesson.tune:
            context["tuned_hz"] = lesson.tune["center_hz"]
            context["step_hz"] = lesson.tune["step_hz"]
            context["tune_band"] = lesson.tune
        return templates.TemplateResponse(request=request, name="console.html", context=context)

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

    def _next_card_id() -> str:
        """The id of the card to study next: the weakest one due now, else the first in the pool.

        `due` ranks never-seen and lapsed cards weakest-first, so a fresh learner walks the pool in
        order and a returning one meets their shakiest cards first. The fallback only bites when
        every card is in its waiting interval, which is a good problem — any card then serves.
        """
        pool = app.state.exam_pool
        due = app.state.exam_store.due([q.id for q in pool.questions], now=time.time())
        return due[0] if due else pool.questions[0].id

    @app.get("/exam", response_class=HTMLResponse)
    def exam_card(request: Request):
        """The exam trainer's study card: the weakest card due now, tapped to answer.

        The correct answer is never placed in the question HTML — commit-before-reveal, the same
        rule the lesson predict gate keeps. The reveal lives on /exam/review, after an answer is in.
        """
        question = app.state.exam_pool.get(_next_card_id())
        return templates.TemplateResponse(
            request=request,
            name="exam.html",
            context={"title": "Exam Trainer", "back_href": "/", "back_label": "Home",
                     "question": question, "reviewed": False},
        )

    @app.post("/api/exam/answer")
    async def exam_answer(question_id: str, request: Request):
        """Record one tap answer and redirect to its review — no JS, like the predict gate."""
        question = app.state.exam_pool.get(question_id)
        if question is None:
            raise HTTPException(status_code=404, detail=f"question {question_id} not found")
        choice = (await _field(request, "choice")).strip().upper()
        correct = choice == question.answer
        app.state.exam_store.record(question_id, correct)
        log_event(app.state.events_path, "exam_answer",
                  {"question": question_id, "choice": choice, "correct": correct})
        return RedirectResponse(f"/exam/review/{question_id}?choice={choice}", status_code=303)

    @app.get("/exam/review/{question_id}", response_class=HTMLResponse)
    def exam_review(request: Request, question_id: str, choice: str = ""):
        """Show the answered card with the right option revealed and the learner's choice marked."""
        question = app.state.exam_pool.get(question_id)
        if question is None:
            raise HTTPException(status_code=404, detail=f"question {question_id} not found")
        chosen = choice.strip().upper()
        return templates.TemplateResponse(
            request=request,
            name="exam.html",
            context={"title": "Exam Trainer", "back_href": "/", "back_label": "Home",
                     "question": question, "reviewed": True,
                     "chosen": chosen, "correct": chosen == question.answer},
        )

    # --- scored 35-question practice exam (answer-then-advance) ---------------------------

    def _sim_progress(sim):
        """From a sim dict: (first-unanswered id or None, that question's 1-based position,
        correct-so-far). The current question is always the first the learner has not answered."""
        pool = app.state.exam_pool
        ids, answers = sim["question_ids"], sim["answers"]
        correct = sum(1 for qid, c in answers.items()
                      if pool.get(qid) is not None and c == pool.get(qid).answer)
        current = next((qid for qid in ids if qid not in answers), None)
        pos = (ids.index(current) + 1) if current is not None else len(ids)
        return current, pos, correct

    def _sim_breakdown(questions, answers):
        """Per-subelement correct/total for the results screen, in exam order (T1..T9, T0 last)."""
        rows: dict = {}
        for q in questions:
            cell = rows.setdefault(q.subelement, {"subelement": q.subelement, "correct": 0,
                                                  "total": 0})
            cell["total"] += 1
            if answers.get(q.id) == q.answer:
                cell["correct"] += 1
        order = sorted(rows, key=lambda s: (10 if s[1] == "0" else int(s[1]), s))
        return [rows[k] for k in order]

    @app.get("/exam/sim", response_class=HTMLResponse)
    def exam_sim(request: Request):
        """Serve the current question of a scored practice exam, starting one if none is active.

        The current question is the first unanswered on the drawn sheet; once all 35 are in, this
        hands off to the results screen. The correct answer is never in the question HTML —
        commit-before-reveal, exactly like the study card and the lesson predict gate.
        """
        store = app.state.exam_store
        sim = store.sim()
        if sim is None:
            store.start_sim([q.id for q in build_exam(app.state.exam_pool)])
            sim = store.sim()
        current, pos, correct = _sim_progress(sim)
        if current is None:
            return RedirectResponse("/exam/sim/results", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="exam_sim.html",
            context={"title": "Practice Exam", "back_href": "/exam", "back_label": "Exam",
                     "question": app.state.exam_pool.get(current), "reviewed": False,
                     "position": pos, "total": len(sim["question_ids"]),
                     "score": correct, "answered": len(sim["answers"]), "pass_mark": PASS_MARK},
        )

    @app.post("/api/exam/sim/answer")
    async def exam_sim_answer(question_id: str, request: Request):
        """Record one sim answer and redirect to its review — no JS, like the study card."""
        question = app.state.exam_pool.get(question_id)
        if question is None:
            raise HTTPException(status_code=404, detail=f"question {question_id} not found")
        choice = (await _field(request, "choice")).strip().upper()
        correct = choice == question.answer
        app.state.exam_store.record_sim_answer(question_id, choice)
        # Also credit the Leitner history so the sim feeds the study card's weak-first order.
        app.state.exam_store.record(question_id, correct)
        log_event(app.state.events_path, "exam_sim_answer",
                  {"question": question_id, "choice": choice, "correct": correct})
        return RedirectResponse(f"/exam/sim/review/{question_id}?choice={choice}", status_code=303)

    @app.get("/exam/sim/review/{question_id}", response_class=HTMLResponse)
    def exam_sim_review(request: Request, question_id: str, choice: str = ""):
        """Show the answered sim question revealed, with Next pointing at the next question or,
        once the sheet is done, the results screen."""
        question = app.state.exam_pool.get(question_id)
        if question is None:
            raise HTTPException(status_code=404, detail=f"question {question_id} not found")
        sim = app.state.exam_store.sim()
        if sim is None or question_id not in sim["question_ids"]:
            return RedirectResponse("/exam/sim", status_code=303)
        ids = sim["question_ids"]
        _, _, correct = _sim_progress(sim)
        nxt = next((qid for qid in ids if qid not in sim["answers"]), None)
        chosen = choice.strip().upper()
        return templates.TemplateResponse(
            request=request,
            name="exam_sim.html",
            context={"title": "Practice Exam", "back_href": "/exam", "back_label": "Exam",
                     "question": question, "reviewed": True,
                     "chosen": chosen, "correct": chosen == question.answer,
                     "position": ids.index(question_id) + 1, "total": len(ids),
                     "score": correct, "answered": len(sim["answers"]), "pass_mark": PASS_MARK,
                     "next_href": "/exam/sim" if nxt is not None else "/exam/sim/results",
                     "last": nxt is None},
        )

    @app.get("/exam/sim/results", response_class=HTMLResponse)
    def exam_sim_results(request: Request):
        """Score the finished practice exam: total, pass/fail at 26/35, and a per-subelement
        breakdown. An unfinished or absent sim is sent back to answer the rest."""
        sim = app.state.exam_store.sim()
        if sim is None:
            return RedirectResponse("/exam/sim", status_code=303)
        current, _, _ = _sim_progress(sim)
        if current is not None:
            return RedirectResponse("/exam/sim", status_code=303)
        questions = [app.state.exam_pool.get(qid) for qid in sim["question_ids"]]
        result = score_exam(questions, sim["answers"])
        return templates.TemplateResponse(
            request=request,
            name="exam_results.html",
            context={"title": "Practice Exam", "back_href": "/", "back_label": "Home",
                     "result": result, "pass_mark": PASS_MARK, "total": EXAM_SIZE,
                     "pct": round(100 * result.correct / result.total),
                     "pass_pct": round(100 * PASS_MARK / EXAM_SIZE),
                     "breakdown": _sim_breakdown(questions, sim["answers"])},
        )

    @app.post("/api/exam/sim/restart")
    def exam_sim_restart():
        """Discard the finished (or abandoned) sim and start a fresh one."""
        app.state.exam_store.clear_sim()
        return RedirectResponse("/exam/sim", status_code=303)

    def _current_row():
        """The latest real sweep row, lazily starting the sweeper, or None when there is no radio.

        Mirrors /api/spectrum's lazy start so the provenance panel works on its own: a reference
        level can only be read from a sweep the hardware actually produced, never invented.
        """
        sweeper = app.state.sweeper
        if not sweeper.running:
            try:
                sweeper.start()
            except SpectrumUnavailable:
                return None
        rows = sweeper.rows(1)
        return rows[-1] if rows else None

    def _snapshot():
        """The radio's account of itself now — None fields where the hardware cannot answer."""
        return gather_provenance(
            serial=os.environ.get(RTLSDR_SERIAL_ENV),
            gain=app.state.sweeper.gain,
            row=_current_row(),
        )

    @app.get("/api/provenance")
    def provenance_view():
        """The instrument's state plus how its reference level compares to the captured baseline.

        A degraded verdict is the deaf-vs-empty signal of M3: the reference has dropped well below
        the baseline while nothing else moved. With no baseline yet, the comparison says so rather
        than guessing a verdict.
        """
        snapshot = _snapshot()
        verdict = compare_to_baseline(snapshot["ref_db"], read_baseline(app.state.events_path))
        return {"provenance": snapshot, "baseline": verdict}

    @app.post("/api/provenance/baseline")
    def provenance_baseline():
        """Record one baseline sample — refused when the sweep has no reference level to record.

        A baseline against a reference the hardware never read would be a number to trust later
        that was never measured; the honest answer is to refuse, the same way the spectrum route
        refuses to fake a sweep.
        """
        snapshot = _snapshot()
        if snapshot["ref_db"] is None:
            reason = "no reference level — the sweep must cover the reference before a baseline"
            log_event(app.state.events_path, "baseline_refused", {"reason": reason})
            return JSONResponse(status_code=400, content={"recorded": False, "reason": reason})
        record_baseline_sample(app.state.events_path, snapshot)
        return {"recorded": True, "ref_db": snapshot["ref_db"]}

    @app.post("/api/ppm/measure")
    def ppm_measure_route(hz: int = 98_100_000):
        """Measure the receiver's crystal error against a station's 19 kHz stereo pilot.

        rtl_fm needs the dongle to itself, so the sweep stands down first (the next spectrum poll
        lazily brings it back). ppm is None with a reason whenever no pilot is there to measure
        against — a mono station carries none, and a weak one reads the same — never a guessed ppm.
        """
        app.state.sweeper.stop()
        result = app.state.ppm_measure(
            hz=hz, seconds=PPM_SECONDS, device=os.environ.get(RTLSDR_SERIAL_ENV))
        log_event(app.state.events_path, "ppm_measure",
                  {"hz": hz, "ppm": result.get("ppm"), "reason": result.get("reason")})
        return result

    # The RTL-SDR has one antenna input, so only one of these consumers can hold it at a time;
    # each opens the dongle exclusively and would fight the others for it. A route takes the radio
    # by naming only itself to _claim_sdr, which stands every other consumer down (release) and
    # clears any prior stand-down on the caller (resume). LoRa (meshtasticd) and Flock (wlan1
    # monitor) ride their own radios and are deliberately absent from this set.
    sdr_consumers = ("sweeper", "decoder", "rds", "adsb", "aprs", "pocsag", "acars")

    def _claim_sdr(target: str) -> None:
        """Hand the one RTL-SDR to `target`, standing every other SDR-exclusive consumer down."""
        for name in sdr_consumers:
            consumer = getattr(app.state, name)
            if name == target:
                consumer.resume()
            else:
                consumer.release()

    @app.get("/api/spectrum")
    def spectrum(limit: int | None = None):
        """Recent real sweeps. No radio is an answer (503 with the reason), never a fake row."""
        sweeper = app.state.sweeper
        # Start lazily on first request, not at boot: the service comes up before the USB bus
        # settles after a power cut, and a boot-time start would wrongly conclude "no radio" and
        # stay dark until a restart.
        if not sweeper.running:
            # The decoder, the RDS pipeline and the waterfall share one dongle. Taking it back from
            # the SDR-exclusive tools (release) and clearing any prior release on the sweeper lets
            # the sweep start after an M10/M12 lesson handed the radio to rtl_433 or rtl_fm.
            _claim_sdr("sweeper")
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

    @app.get("/api/decode")
    def decode(limit: int | None = None):
        """Recent rtl_433 decodes on the ISM band. No radio or no rtl_433 is a 503 with the reason.

        The decoder holds the dongle rtl_power otherwise sweeps, so the sweep is released first and
        stays released until a /api/spectrum poll takes it back. resume() clears any release left on
        the decoder by that reclaim, so flipping between the two keeps working.
        """
        decoder = app.state.decoder
        if not decoder.running:
            _claim_sdr("decoder")
            try:
                decoder.start()
            except DecodeUnavailable as exc:
                log_event(app.state.events_path, "decode_blocked", {"reason": str(exc)})
                return JSONResponse(status_code=503,
                                    content={"available": False, "reason": str(exc), "decodes": []})
            log_event(app.state.events_path, "decode_start",
                      {"command": " ".join(decoder.command())})
        return {
            "available": True,
            "freq_hz": decoder.freq_hz,
            "decodes": [d.as_dict() for d in decoder.recent(limit)],
        }

    @app.get("/api/lora")
    def lora(limit: int | None = None):
        """Recent LoRa packets the Nomad received, each with the SNR it decoded at.

        A packet below the noise floor (negative SNR) is the lesson. meshtasticd not reachable is a
        503 with the reason, never an empty panel pretending the radio heard nothing. The LoRa radio
        is separate from the RTL-SDR, so this never disturbs a sweep or a decode.
        """
        listener = app.state.lora
        if not listener.running:
            try:
                listener.start()
            except LoRaUnavailable as exc:
                log_event(app.state.events_path, "lora_blocked", {"reason": str(exc)})
                return JSONResponse(status_code=503,
                                    content={"available": False, "reason": str(exc), "packets": []})
            log_event(app.state.events_path, "lora_start",
                      {"host": listener.hostname, "port": listener.port})
        return {
            "available": True,
            "host": listener.hostname,
            "port": listener.port,
            "packets": [p.as_dict() for p in listener.recent(limit)],
        }

    @app.get("/api/flock")
    def flock(limit: int | None = None):
        """Flock ALPR cameras heard on wlan1 in monitor mode, strongest-evidence devices first.

        The detector reads 802.11 management frames off a separate WiFi radio, so this never
        disturbs a sweep or a decode. No monitor interface / no tcpdump is a 503 with the reason,
        never an empty panel pretending to clear the area — no alert never means no camera.
        """
        detector = app.state.flock
        if not detector.running:
            try:
                detector.start()
            except FlockUnavailable as exc:
                log_event(app.state.events_path, "flock_blocked", {"reason": str(exc)})
                return JSONResponse(
                    status_code=503,
                    content={"available": False, "reason": str(exc), "detections": []})
            log_event(app.state.events_path, "flock_start", {"interface": detector.interface})
        return {
            "available": True,
            "interface": detector.interface,
            "detections": detector.detections(limit),
        }

    @app.get("/api/rds")
    def rds():
        """The RDS station assembled off the tuned FM frequency. No radio / no redsea is a 503.

        rtl_fm|redsea holds the dongle the waterfall otherwise sweeps, so the sweep and the rtl_433
        decoder are released first and stay released until a /api/spectrum poll takes the radio.
        """
        decoder = app.state.rds
        if not decoder.running:
            _claim_sdr("rds")
            try:
                decoder.start()
            except RDSUnavailable as exc:
                log_event(app.state.events_path, "rds_blocked", {"reason": str(exc)})
                return JSONResponse(
                    status_code=503,
                    content={"available": False, "reason": str(exc), "station": None})
            log_event(app.state.events_path, "rds_start", {"freq_hz": decoder.freq_hz})
        return {
            "available": True,
            "freq_hz": decoder.freq_hz,
            "station": decoder.station().as_dict(),
        }

    @app.get("/api/adsb")
    def adsb():
        """The aircraft dump1090 is decoding at 1090 MHz, each with range/bearing when located.

        dump1090 holds the dongle the waterfall otherwise sweeps, so the sweep, the rtl_433 decoder
        and the RDS pipeline are released first and stay released until a /api/spectrum poll takes
        the radio. No radio, no dump1090, or no planes overhead is an honest empty sky — a 503 with
        the reason when the feed cannot be read, never a fabricated contact.
        """
        decoder = app.state.adsb
        if not decoder.running:
            _claim_sdr("adsb")
            try:
                decoder.start()
            except AdsbUnavailable as exc:
                log_event(app.state.events_path, "adsb_blocked", {"reason": str(exc)})
                return JSONResponse(
                    status_code=503,
                    content={"available": False, "reason": str(exc), "aircraft": []})
            log_event(app.state.events_path, "adsb_start", {"freq_hz": DEFAULT_ADSB_HZ})
        return {
            "available": True,
            "freq_hz": DEFAULT_ADSB_HZ,
            "receiver_located": decoder.receiver.known,
            "aircraft": decoder.aircraft(),
        }

    @app.get("/api/aprs")
    def aprs():
        """The APRS stations multimon-ng is decoding on the 2m packet channel, latest packet each.

        rtl_fm|multimon-ng holds the dongle the waterfall otherwise sweeps, so the sweep and the
        other SDR tools are released first and stay released until a /api/spectrum poll takes the
        radio. No radio, no multimon-ng, or a quiet channel is an honest empty roster — a 503 with
        the reason when the pipeline cannot be read, never a fabricated station.
        """
        decoder = app.state.aprs
        if not decoder.running:
            _claim_sdr("aprs")
            try:
                decoder.start()
            except AprsUnavailable as exc:
                log_event(app.state.events_path, "aprs_blocked", {"reason": str(exc)})
                return JSONResponse(
                    status_code=503,
                    content={"available": False, "reason": str(exc), "stations": []})
            log_event(app.state.events_path, "aprs_start", {"freq_hz": decoder.freq_hz})
        return {
            "available": True,
            "freq_hz": decoder.freq_hz,
            "stations": decoder.stations(),
        }

    @app.get("/api/pocsag")
    def pocsag(limit: int | None = None):
        """The pager messages multimon-ng is decoding on the POCSAG channel, newest first.

        rtl_fm|multimon-ng holds the dongle the waterfall otherwise sweeps, so the sweep and the
        other SDR tools are released first and stay released until a /api/spectrum poll takes the
        radio. No radio, no multimon-ng, or a silent channel is an honest empty log — a 503 with the
        reason when the pipeline cannot be read, never a fabricated page.
        """
        decoder = app.state.pocsag
        if not decoder.running:
            _claim_sdr("pocsag")
            try:
                decoder.start()
            except PocsagUnavailable as exc:
                log_event(app.state.events_path, "pocsag_blocked", {"reason": str(exc)})
                return JSONResponse(
                    status_code=503,
                    content={"available": False, "reason": str(exc), "pages": []})
            log_event(app.state.events_path, "pocsag_start", {"freq_hz": decoder.freq_hz})
        return {
            "available": True,
            "freq_hz": decoder.freq_hz,
            "pages": decoder.pages(limit),
        }

    @app.get("/api/acars")
    def acars(limit: int | None = None):
        """The ACARS messages acarsdec is decoding on the VHF channel, newest first.

        acarsdec opens the dongle the waterfall otherwise sweeps, so the sweep and the other SDR
        tools are released first and stay released until a /api/spectrum poll takes the radio. No
        radio, no acarsdec, or a quiet channel is an honest empty log — a 503 with the reason when
        the process cannot be read, never a fabricated message.
        """
        decoder = app.state.acars
        if not decoder.running:
            _claim_sdr("acars")
            try:
                decoder.start()
            except AcarsUnavailable as exc:
                log_event(app.state.events_path, "acars_blocked", {"reason": str(exc)})
                return JSONResponse(
                    status_code=503,
                    content={"available": False, "reason": str(exc), "messages": []})
            log_event(app.state.events_path, "acars_start", {"freq_hz": decoder.freq_hz})
        return {
            "available": True,
            "freq_hz": decoder.freq_hz,
            "messages": decoder.messages(limit),
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
