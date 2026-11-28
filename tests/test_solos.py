# ABOUTME: Tests Tier C solos engine, validator, audit gate, F6 receiver stand-down,
# ABOUTME: process-order gating, legal-call scoring, leak-free GETs, and rack Back preservation.

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sigint101 import app as console
from sigint101 import events as events_mod
from sigint101.app import create_app
from sigint101.decode import Decoder
from sigint101.events import read_events
from sigint101.hardware import RadioProbe
from sigint101.migrations import migrate_store_copy
from sigint101.pocsag import PocsagDecoder
from tests.child_fakes import Child
from tests.test_updater_storage import _counting_write_authorizer

FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "lesson_packs"
RTL433_ASK = (
    '{"time" : "2026-11-28 12:00:00", "model" : "Acurite-Tower", "id" : 12345, '
    '"channel" : "A", "battery_ok" : 1, "temperature_C" : 18.0, "humidity" : 51, '
    '"mod" : "ASK", "freq" : 433.920, "snr" : 36.2}'
)
POCSAG_ALPHA = "POCSAG1200: Address: 1234567  Function: 3  Alpha:   CALL EXT 2210 URGENT"


def _present():
    return RadioProbe(True, "lsusb", "RTL2832U 0bda:2838 on USB bus")


def _absent():
    return RadioProbe(False, "lsusb", "no RTL2832U on USB bus")


def _tools():
    return {"radio_present": lambda: True, "which": lambda name: "/usr/bin/" + name}


class _StubLora:
    """App-boundary LoRa listener stand-in that tracks start/stop cleanly without a daemon."""

    def __init__(self):
        self.hostname = "localhost"
        self.port = 4403
        self.running = False
        self.starts = 0
        self.stops = 0
        self.on_reception = None
        self.reception_sink = None

    def start(self):
        self.running = True
        self.starts += 1

    def stop(self):
        if self.running:
            self.running = False
            self.stops += 1

    def recent(self, limit=None):
        return []

    def send_state(self):
        return {"radio": {"region": "US", "tx_enabled": False}, "sends": []}

    def emit(self, from_id="!abc123"):
        assert self.on_reception is not None
        self.on_reception(
            {"from_id": from_id, "snr_db": -4.5, "rssi_dbm": -108, "summary": "ping"},
            freq_hz=None,
            radio={"kind": "meshtasticd", "host": f"{self.hostname}:{self.port}"},
        )


def _sample_solos_and_audits():
    """Build test-only solos (1-leg SDR, 1-leg LoRa, 2-leg capstone) with valid audit records."""
    from sigint101.solos import Solo, SoloAuditRecord, SoloBandLeg, SoloWhyOption

    ism_leg = SoloBandLeg(
        slot=0,
        unit_id=4,
        band_label="420–450 MHz",
        clue="Short unlicensed telemetry bursts from nearby environmental sensors.",
        instrument="ism",
        band_hz=((420_000_000, 450_000_000),),
        hardware_note="",
        legal_prompt="Is passively receiving this unencrypted sensor telemetry lawful in the US?",
        legal_answers=("legal",),
        ambiguous=False,
        why_options=(
            SoloWhyOption(
                key="readily-accessible",
                label=(
                    "Unencrypted public radio transmissions are exempt under "
                    "18 USC 2511(2)(g)(i)."
                ),
                supports=("legal",),
                feedback=(
                    "18 USC 2511(2)(g)(i) exempts electronic communications readily "
                    "accessible to the general public."
                ),
                cite="18 USC 2511(2)(g)(i)",
                source_url="https://www.govinfo.gov/app/details/USCODE-2023-title18/USCODE-2023-title18-partI-chap119-sec2511",
                read_date="2026-11-28",
                law_section="wiretap-open",
            ),
            SoloWhyOption(
                key="encrypted-bar",
                label=(
                    "Scrambled or encrypted radio traffic is excluded from readily "
                    "accessible radio."
                ),
                supports=(),
                feedback=(
                    "True for encrypted signals under 18 USC 2510(16)(A), but this "
                    "sensor telemetry is unencrypted."
                ),
                cite="18 USC 2510(16)(A)",
                source_url="https://www.govinfo.gov/app/details/USCODE-2023-title18/USCODE-2023-title18-partI-chap119-sec2510",
                read_date="2026-11-28",
                law_section="wiretap-limits",
            ),
            SoloWhyOption(
                key="scanner-rule",
                label=(
                    "47 CFR 15.121 restricts cellular scanner certification, not "
                    "general SDR ownership."
                ),
                supports=(),
                feedback=(
                    "True under 47 CFR 15.121, though it governs scanner certification "
                    "rather than this reception."
                ),
                cite="47 CFR 15.121",
                source_url="https://www.ecfr.gov/current/title-47/section-15.121",
                read_date="2026-11-28",
                law_section="scanner-15121",
            ),
        ),
        sources=("18 USC 2511(2)(g)(i)", "18 USC 2510(16)(A)", "47 CFR 15.121"),
        audit_id="audit-u4-0",
        item_revision="rev-u4-1",
    )

    lora_leg = SoloBandLeg(
        slot=0,
        unit_id=9,
        band_label="902–928 MHz",
        clue="Low-power chirp packets from a peer node on the default public channel.",
        instrument="lora",
        band_hz=(),
        hardware_note="Requires a second peer node in range.",
        legal_prompt=(
            "Does decoding the default published-key channel clearly qualify as "
            "readily accessible?"
        ),
        legal_answers=("depends",),
        ambiguous=True,
        why_options=(
            SoloWhyOption(
                key="published-key-tension",
                label=(
                    "Encrypted traffic falls under 18 USC 2510(16)(A), even when a "
                    "default key is published."
                ),
                supports=("depends", "not_sure"),
                feedback=(
                    "Published default keys sit in tension between 2510(16)(A) "
                    "encryption and 2511(2)(g)(i) open access."
                ),
                cite="18 USC 2510(16)(A)",
                source_url="https://www.govinfo.gov/app/details/USCODE-2023-title18/USCODE-2023-title18-partI-chap119-sec2510",
                read_date="2026-11-28",
                law_section="wiretap-limits",
            ),
            SoloWhyOption(
                key="divulge-rule",
                label=(
                    "47 USC 605(a) bars divulging or using private radio interceptions "
                    "you lack authority to receive."
                ),
                supports=(),
                feedback=(
                    "True under 47 USC 605(a), governing divulging and use rather "
                    "than the encryption boundary itself."
                ),
                cite="47 USC 605(a)",
                source_url="https://www.govinfo.gov/app/details/USCODE-2023-title47/USCODE-2023-title47-chap5-subchapVI-sec605",
                read_date="2026-11-28",
                law_section="divulge-605",
            ),
        ),
        sources=("18 USC 2510(16)(A)", "47 USC 605(a)"),
        audit_id="audit-u9-0",
        item_revision="rev-u9-1",
    )

    pager_leg = SoloBandLeg(
        slot=0,
        unit_id=5,
        band_label="929–932 MHz",
        clue="High-power FSK paging bursts carrying commercial dispatch framing.",
        instrument="pager",
        band_hz=((929_000_000, 932_000_000),),
        hardware_note="",
        legal_prompt="Is intercepting a commercial display page's words lawful in the US?",
        legal_answers=("illegal",),
        ambiguous=False,
        why_options=(
            SoloWhyOption(
                key="carrier-pager",
                label=(
                    "Display pagers on common carriers are excluded from readily "
                    "accessible radio under 18 USC 2510(16)(D)."
                ),
                supports=("illegal",),
                feedback=(
                    "18 USC 2510(16)(D) and 2511(1)(a) exclude commercial text/numeric "
                    "pagers; only tone-only pagers are carved out."
                ),
                cite="18 USC 2510(16)(D)",
                source_url="https://www.govinfo.gov/app/details/USCODE-2023-title18/USCODE-2023-title18-partI-chap119-sec2510",
                read_date="2026-11-28",
                law_section="wiretap-limits",
            ),
            SoloWhyOption(
                key="open-broadcast",
                label=(
                    "Open radio broadcasts intended for the general public are exempt "
                    "under 18 USC 2511(2)(g)(i)."
                ),
                supports=(),
                feedback=(
                    "True for general broadcasts, but commercial paging is not "
                    "intended for the general public."
                ),
                cite="18 USC 2511(2)(g)(i)",
                source_url="https://www.govinfo.gov/app/details/USCODE-2023-title18/USCODE-2023-title18-partI-chap119-sec2511",
                read_date="2026-11-28",
                law_section="wiretap-open",
            ),
        ),
        sources=("18 USC 2510(16)(D)", "18 USC 2511(2)(g)(i)"),
        audit_id="audit-u5-0",
        item_revision="rev-u5-1",
    )

    cap_leg0 = copy.deepcopy(ism_leg)
    cap_leg1 = SoloBandLeg(
        **{**lora_leg.__dict__, "slot": 1, "audit_id": "audit-cap-1"}
    )
    cap_leg0 = SoloBandLeg(
        **{**ism_leg.__dict__, "slot": 0, "audit_id": "audit-cap-0"}
    )

    u4 = Solo(
        id="u4-solo", unit_id=4, title="Unlicensed telemetry solo",
        legs=(ism_leg,), window_s=900,
    )
    u5 = Solo(
        id="u5-solo", unit_id=5, title="Commercial dispatch solo",
        legs=(pager_leg,), window_s=900,
    )
    u9 = Solo(id="u9-solo", unit_id=9, title="Peer packet solo", legs=(lora_leg,), window_s=900)
    cap = Solo(
        id="capstone-4-9",
        unit_id=None,
        title="Two-band solo capstone",
        legs=(cap_leg0, cap_leg1),
        window_s=2700,
    )
    audits = {
        "audit-u4-0": SoloAuditRecord(
            "audit-u4-0", "u4-solo", 0, "author-a", "reviewer-b", "pass", "2026-11-28", "rev-u4-1"
        ),
        "audit-u5-0": SoloAuditRecord(
            "audit-u5-0", "u5-solo", 0, "author-a", "reviewer-b", "pass", "2026-11-28", "rev-u5-1"
        ),
        "audit-u9-0": SoloAuditRecord(
            "audit-u9-0", "u9-solo", 0, "author-a", "reviewer-b", "pass", "2026-11-28", "rev-u9-1"
        ),
        "audit-cap-0": SoloAuditRecord(
            "audit-cap-0", "capstone-4-9", 0, "author-a", "reviewer-b",
            "pass", "2026-11-28", "rev-u4-1",
        ),
        "audit-cap-1": SoloAuditRecord(
            "audit-cap-1", "capstone-4-9", 1, "author-a", "reviewer-b",
            "pass", "2026-11-28", "rev-u9-1",
        ),
    }
    return (u4, u5, u9, cap), audits


def _pin_log_clock(monkeypatch, now_box):
    real_log = events_mod.log_event
    monkeypatch.setattr(
        console,
        "log_event",
        lambda *args, **kwargs: real_log(*args, **kwargs, clock=lambda: now_box[0]),
    )


def _solo_client(tmp_path, *, clock=None, solos=None, solo_audits=None, probe=_absent,
                 managed=False, pack_dir=FIXTURE_PACKS, **factories):
    default_solos, default_audits = _sample_solos_and_audits()
    use_solos = default_solos if solos is None else solos
    use_audits = default_audits if solo_audits is None else solo_audits
    app = create_app(
        pack_dir=pack_dir,
        data_dir=tmp_path,
        probe=probe,
        clock=clock or (lambda: 1_764_330_000.0),
        managed=managed,
        solos=use_solos,
        solo_audits=use_audits,
        **factories,
    )
    return TestClient(app)


# --- 1. Validator, audit gate, caps, US allocation labels, and derived forbidden words --------


def test_solo_catalog_validates_one_instrument_per_leg_disjointness_caps_audit_and_no_leaks():
    from sigint101.solos import (
        Solo,
        SoloAuditRecord,
        SoloBandLeg,
        SoloWhyOption,
        forbidden_leak_words,
        validate_solo_catalog,
    )

    solos, audits = _sample_solos_and_audits()
    validate_solo_catalog(solos, audits, require_audit=True)

    # Derived forbidden words include keys, slash-split labels, tools, rack_label, and Meshtastic.
    words = {w.lower() for w in forbidden_leak_words()}
    for expected in ("ism", "rtl_433", "pocsag", "pager framing", "dmr", "p25", "meshtastic",
                     "satdump", "rtl_power", "sstv", "tcpdump"):
        assert expected in words

    base_solo = solos[0]
    base_leg = base_solo.legs[0]
    base_opt = base_leg.why_options[0]

    # Forbidden instrument (apt/flock/rayhunter) rejected.
    bad_inst = SoloBandLeg(**{**base_leg.__dict__, "instrument": "apt"})
    with pytest.raises(ValueError, match="forbid"):
        validate_solo_catalog((Solo(**{**base_solo.__dict__, "legs": (bad_inst,)}),), audits)

    # LoRa leg with non-empty band_hz rejected (F5).
    lora_solo = solos[2]
    bad_lora = SoloBandLeg(
        **{**lora_solo.legs[0].__dict__, "band_hz": ((902_000_000, 928_000_000),)}
    )
    with pytest.raises(ValueError, match="band_hz"):
        validate_solo_catalog((Solo(**{**lora_solo.__dict__, "legs": (bad_lora,)}),), audits)

    # Overlapping legs (instrument or band_hz) rejected (F12).
    dup_leg = SoloBandLeg(
        **{**base_leg.__dict__, "slot": 1, "unit_id": 5, "audit_id": "audit-cap-1"}
    )
    with pytest.raises(ValueError, match="disjoint|duplicate|overlap"):
        validate_solo_catalog(
            (Solo(**{**base_solo.__dict__, "legs": (base_leg, dup_leg)}),), audits
        )

    # > 3 why options rejected (F8).
    four_opts = tuple(SoloWhyOption(**{**base_opt.__dict__, "key": f"k{i}"}) for i in range(4))
    with pytest.raises(ValueError, match="why_options"):
        four_leg = SoloBandLeg(**{**base_leg.__dict__, "why_options": four_opts})
        validate_solo_catalog((Solo(**{**base_solo.__dict__, "legs": (four_leg,)}),), audits)

    # Chip label > 130 chars rejected (F8).
    long_opt = SoloWhyOption(**{**base_opt.__dict__, "label": "x" * 131})
    with pytest.raises(ValueError, match="130"):
        long_leg = SoloBandLeg(**{**base_leg.__dict__, "why_options": (long_opt,)})
        validate_solo_catalog((Solo(**{**base_solo.__dict__, "legs": (long_leg,)}),), audits)

    # Unknown law_section rejected (F7).
    bad_law = SoloWhyOption(**{**base_opt.__dict__, "law_section": "no-such-section"})
    with pytest.raises(ValueError, match="law_section"):
        bad_law_leg = SoloBandLeg(**{**base_leg.__dict__, "why_options": (bad_law,)})
        validate_solo_catalog((Solo(**{**base_solo.__dict__, "legs": (bad_law_leg,)}),), audits)

    # Ambiguous=False with not_sure supported rejected (F10).
    bad_ns = SoloWhyOption(**{**base_opt.__dict__, "supports": ("legal", "not_sure")})
    with pytest.raises(ValueError, match="not_sure"):
        bad_ns_leg = SoloBandLeg(**{**base_leg.__dict__, "why_options": (bad_ns,)})
        validate_solo_catalog((Solo(**{**base_solo.__dict__, "legs": (bad_ns_leg,)}),), audits)

    # Self-audit (author == evaluator) rejected (F10).
    self_audits = {
        **audits,
        "audit-u4-0": SoloAuditRecord(
            "audit-u4-0", "u4-solo", 0, "same", "same", "pass", "2026-11-28", "rev-u4-1"
        ),
    }
    with pytest.raises(ValueError, match="audit"):
        validate_solo_catalog((base_solo,), self_audits, require_audit=True)

    # Leaking an instrument or tool name in title, band_label, or clue rejected (F2).
    with pytest.raises(ValueError, match="forbidden|leak"):
        validate_solo_catalog(
            (Solo(**{**base_solo.__dict__, "title": "Listen with rtl_433"}),), audits
        )
    with pytest.raises(ValueError, match="forbidden|leak"):
        leak_leg = SoloBandLeg(**{**base_leg.__dict__, "clue": "Decode a P25 frame"})
        validate_solo_catalog((Solo(**{**base_solo.__dict__, "legs": (leak_leg,)}),), audits)

    # Decoder internal range or ITU Region 1 label rejected (N6, R2-F4).
    for bad_band in ("300–928 MHz", "87.5–108 MHz", "433.05–434.79 MHz"):
        with pytest.raises(ValueError, match="band_label"):
            bad_band_leg = SoloBandLeg(**{**base_leg.__dict__, "band_label": bad_band})
            validate_solo_catalog(
                (Solo(**{**base_solo.__dict__, "legs": (bad_band_leg,)}),), audits
            )


# --- 2. Window, LoRa freq_hz=None, and 4 required R2-B2 stand-down / process-order tests ------


def test_solo_window_provenance_lora_band_and_receiver_standdown_process_order(
    tmp_path, monkeypatch
):
    now = [1_764_330_000.0]
    _pin_log_clock(monkeypatch, now)
    lora_stub = _StubLora()
    decode_spawns = []
    pocsag_spawns = []

    def make_decoder():
        def spawn(cmd):
            decode_spawns.append(cmd)
            n = len(decode_spawns)
            line = (
                RTL433_ASK.replace('"id" : 12345', f'"id" : {12340 + n}')
                .replace("12:00:00", f"12:00:0{n}")
            )
            return Child(cmd, said=line)
        return Decoder(spawn=spawn, **_tools())

    def make_pocsag():
        def spawn(freq_hz, device):
            pocsag_spawns.append((freq_hz, device))
            return Child(["rtl_fm", "-M", "fm", "-f", str(freq_hz), "-d", str(device)],
                         said=POCSAG_ALPHA)
        return PocsagDecoder(spawn=spawn, **_tools())

    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        lora_factory=lambda: lora_stub,
        decoder_factory=make_decoder,
        pocsag_factory=make_pocsag,
    ) as client:
        # (1) LoRa opened before Start -> solo Start stands down LoRa -> GET /operate/lora writes
        # nothing -> /api/lora logs fresh lora_start -> LoRa packet (freq_hz=None) passes!
        assert client.get("/api/lora").status_code == 200
        assert lora_stub.running is True and lora_stub.starts == 1
        assert client.post("/api/solos/u9-solo/start").status_code == 200
        assert lora_stub.running is False and lora_stub.stops == 1

        # A packet emitted before a fresh /api/lora start cannot pass.
        lora_stub.emit("!pre")
        assert client.get("/api/solos/u9-solo").json()["state"] == "active"

        # GET /operate/lora writes zero DB rows and zero events.
        events_before = (tmp_path / "events.jsonl").read_bytes()
        assert client.get("/operate/lora").status_code == 200
        assert (tmp_path / "events.jsonl").read_bytes() == events_before

        # Panel polls /api/lora -> fresh lora_start -> packet passes!
        assert client.get("/api/lora").status_code == 200
        assert lora_stub.starts == 2
        now[0] += 10.0
        lora_stub.emit("!post1")
        body = client.get("/api/solos/u9-solo").json()
        assert body["state"] == "reception_passed"
        assert body["legs"][0]["heard"]["instrument"] == "lora"
        assert body["legs"][0]["heard"]["freq_hz"] is None

        # (2) "Try again" after a pass stands down receivers and passes again on fresh start.
        assert client.post(
            "/api/solos/u9-solo/call",
            json={
                "attempt_id": body["attempt_id"],
                "started_at": body["started_at"],
                "band_slot": 0,
                "call": "not_sure",
                "why_key": "published-key-tension",
            },
        ).status_code == 200
        assert client.get("/api/solos/u9-solo").json()["state"] == "passed"

        now[0] += 20.0
        assert client.post("/api/solos/u9-solo/start").status_code == 200
        assert lora_stub.running is False and lora_stub.stops == 2
        assert client.get("/api/lora").status_code == 200
        assert lora_stub.starts == 3
        now[0] += 5.0
        lora_stub.emit("!post2")
        assert client.get("/api/solos/u9-solo").json()["state"] == "reception_passed"

        # (3) SDR decoder left running from Tier B (keyed by operate.instrument(key).api:
        # ism -> decode_start, pager -> pocsag_start) is released by solo Start; only post-restart
        # receptions count both live and on startup backfill.
        assert client.get("/api/pocsag").status_code == 200
        assert len(pocsag_spawns) == 1
        now[0] += 5.0
        assert client.post("/api/solos/u5-solo/start").status_code == 200
        assert client.app.state.pocsag.running is False
        assert client.get("/api/pocsag").status_code == 200
        assert len(pocsag_spawns) == 2
        client.app.state.pocsag.drain()
        assert client.get("/api/solos/u5-solo").json()["state"] == "reception_passed"

        # (4) Backward clock step between receiver start and solo Start:
        # Start decoder at T=100, step clock BACKWARD to T=50, start u4-solo (without restarting
        # decoder via a spoofed pre-Start emit) -> process-order counter rejects pre-Start run!
        now[0] += 50.0
        assert client.get("/api/decode").status_code == 200
        # Simulate clock step backward, then Start u4-solo, then inject a reception with old order.
        now[0] -= 30.0
        pre_order = client.app.state.decoder._run.order
        assert client.post("/api/solos/u4-solo/start").status_code == 200
        saved_u4 = client.app.state.progress.attempt(
            console.SOLO_KIND, "u4-solo"
        )["attempt"]
        assert saved_u4["attempt_order"] > pre_order
        # Without restarting /api/decode, emitting on decoder must NOT credit u4-solo!
        client.app.state.decoder.on_reception(
            {"model": "Acurite-Tower", "mod": "ASK", "readings": {"temperature_C": 18.0}},
            command="rtl_433 -f 433920000 -F json -d 0",
            freq_hz=433_920_000,
            radio={"kind": "rtl-sdr", "index": 0, "serial": None},
        )
        assert client.get("/api/solos/u4-solo").json()["state"] == "active"

        # After restarting /api/decode (fresh decode_start after solo_start), reception credits!
        assert client.get("/api/decode").status_code == 200
        client.app.state.decoder.drain()
        assert client.get("/api/solos/u4-solo").json()["state"] == "reception_passed"


# --- 3. Shared live path, table separation, containment, rollback, legacy reopen, multi-leg ---


def test_solo_shared_live_path_separation_containment_rollback_and_multileg(
    tmp_path, monkeypatch
):
    now = [1_764_331_000.0]
    _pin_log_clock(monkeypatch, now)
    lora_stub = _StubLora()

    # Legacy pre-solo DB (no solo tables) reopens intact without losing existing data.
    legacy_db = tmp_path / "progress.db"
    with closing(sqlite3.connect(legacy_db)) as conn:
        conn.executescript(
            "CREATE TABLE progress ("
            "lesson_id INTEGER PRIMARY KEY, opened INTEGER NOT NULL DEFAULT 0, "
            "completed INTEGER NOT NULL DEFAULT 0, updated REAL);"
            "INSERT INTO progress VALUES(1, 1, 1, 50.0);"
        )
        conn.commit()

    def make_decoder():
        return Decoder(spawn=lambda cmd: Child(cmd, said=""), **_tools())

    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        decoder_factory=make_decoder,
        lora_factory=lambda: lora_stub,
    ) as client:
        assert client.app.state.progress.get(1)["completed"] == 1

        # Start 2-leg capstone AND verify Tier B tables stay untouched while solo tables populate.
        assert client.post("/api/solos/capstone-4-9/start").status_code == 200
        assert client.get("/api/decode").status_code == 200

        # Credit-failure containment: if solo credit fails in SQLite inside _heard, reception event
        # and badge (stamps) still save, and startup backfill recovers on restart!
        with closing(sqlite3.connect(legacy_db)) as db, db:
            db.execute(
                "CREATE TRIGGER block_solo_pass BEFORE INSERT ON solo_passes "
                "BEGIN SELECT RAISE(ABORT, 'solo disk full'); END"
            )
        now[0] += 5.0
        client.app.state.decoder.on_reception(
            {"model": "Acurite-Tower", "mod": "ASK", "readings": {"temperature_C": 18.0}},
            command="rtl_433 -f 433920000 -F json -d 0",
            freq_hz=433_920_000,
            radio={"kind": "rtl-sdr", "index": 0, "serial": None},
        )
        assert client.get("/api/solos/capstone-4-9").json()["state"] == "active"
        assert "solo disk full" in (client.get("/api/solos/capstone-4-9").json()["error"] or "")
        stamps = {s["instrument"]: s for s in client.get("/api/stamps").json()["stamps"]}
        assert stamps["ism"]["count"] >= 1
        with closing(sqlite3.connect(legacy_db)) as db, db:
            db.execute("DROP TRIGGER block_solo_pass")

    # Restarting recovers leg 0 via startup backfill (respecting decode_start > solo_start)!
    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        decoder_factory=make_decoder,
        lora_factory=lambda: lora_stub,
    ) as client:
        cap = client.get("/api/solos/capstone-4-9").json()
        assert cap["state"] == "reception_passed"
        assert cap["legs"][0]["passed"] is True
        assert cap["legs"][1]["passed"] is False

        # Backward clock step between leg 0 and leg 1 still allows leg 1 inside window!
        assert client.get("/api/lora").status_code == 200
        now[0] += 15.0
        lora_stub.emit("!mesh-leg1")
        cap2 = client.get("/api/solos/capstone-4-9").json()
        assert cap2["legs"][0]["passed"] is True
        assert cap2["legs"][1]["passed"] is True

        # Separation: challenges & challenge_passes have zero rows written by solos.
        with closing(sqlite3.connect(legacy_db)) as db:
            assert db.execute("SELECT count(*) FROM challenges").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM challenge_passes").fetchone() == (0,)

        # Start rollback when solo_start log_event fails restores previous attempt and error.
        prev_started = cap2["started_at"]
        now[0] += 50.0
        with monkeypatch.context() as mp:
            def fail_log(path, kind, payload, **kw):
                if kind == "solo_start":
                    raise OSError("disk full on solo_start")
                return events_mod.log_event(path, kind, payload, **kw)
            mp.setattr(console, "log_event", fail_log)
            resp = client.post("/api/solos/capstone-4-9/start")
            assert resp.status_code == 500
            assert "disk full on solo_start" in (resp.json()["error"] or "")
        after = client.get("/api/solos/capstone-4-9").json()
        assert after["started_at"] == prev_started
        assert "disk full on solo_start" in (after["error"] or "")

        # Start on closed store logs NO solo_start event.
        before_starts = sum(
            1 for e in read_events(tmp_path / "events.jsonl") if e["kind"] == "solo_start"
        )
        client.app.state.progress.close()
        resp_closed = client.post("/api/solos/u4-solo/start")
        assert resp_closed.status_code == 500
        after_starts = sum(
            1 for e in read_events(tmp_path / "events.jsonl") if e["kind"] == "solo_start"
        )
        assert after_starts == before_starts


# --- 4. Legal call scoring, 400/409, stale started_at, 500 rollback, managed sync, D16 --------


def test_legal_call_scoring_per_leg_stale_page_409_log_rollback_and_d16(tmp_path, monkeypatch):
    from sigint101.solos import score_legal_call

    solos, audits = _sample_solos_and_audits()
    ism_leg = solos[0].legs[0]
    lora_leg = solos[2].legs[0]

    # Pure scoring: unambiguous leg rejects not_sure; ambiguous leg accepts not_sure.
    ok_res = score_legal_call(ism_leg, "legal", "readily-accessible")
    assert ok_res["call_correct"] is True and ok_res["why_correct"] is True
    assert ok_res["legal_passed"] is True and ok_res["item_revision"] == "rev-u4-1"

    bad_ns = score_legal_call(ism_leg, "not_sure", "readily-accessible")
    assert bad_ns["call_correct"] is False and bad_ns["legal_passed"] is False

    ok_ns = score_legal_call(lora_leg, "not_sure", "published-key-tension")
    assert ok_ns["call_correct"] is True and ok_ns["why_correct"] is True
    assert ok_ns["legal_passed"] is True

    # Managed mode sync test: bootstrap migrated DBs first.
    gen_dir = tmp_path / "gen"
    gen_dir.mkdir()
    migrate_store_copy(tmp_path / "m_p.db", gen_dir / "progress.db",
                       store="progress", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_p")
    migrate_store_copy(tmp_path / "m_e.db", gen_dir / "exam.db",
                       store="exam", allow_missing_source=True,
                       scratch_dir=tmp_path / "scratch_e")

    now = [1_764_332_000.0]

    sync_calls: list[tuple[str, bool]] = []
    real_log_event = events_mod.log_event

    def spy_log_event(path, kind, payload, **kw):
        sync_calls.append((kind, bool(kw.get("sync", False))))
        return real_log_event(path, kind, payload, clock=lambda: now[0], **kw)

    monkeypatch.setattr(console, "log_event", spy_log_event)

    def make_decoder():
        return Decoder(spawn=lambda cmd: Child(cmd, said=RTL433_ASK), **_tools())

    with _solo_client(
        gen_dir,
        clock=lambda: now[0],
        probe=_present,
        managed=True,
        decoder_factory=make_decoder,
    ) as client:
        assert client.post("/api/solos/u4-solo/start").status_code == 200
        u4_state = client.get("/api/solos/u4-solo").json()
        started = u4_state["started_at"]
        attempt_id = u4_state["attempt_id"]

        good_call = {
            "attempt_id": attempt_id,
            "started_at": started,
            "band_slot": 0,
            "call": "legal",
            "why_key": "readily-accessible",
        }

        # Call before reception passes -> 409, writes nothing.
        r_pre = client.post("/api/solos/u4-solo/call", json=good_call)
        assert r_pre.status_code == 409

        # Hear ISM reception to unlock legal call.
        assert client.get("/api/decode").status_code == 200
        client.app.state.decoder.drain()
        assert client.get("/api/solos/u4-solo").json()["state"] == "reception_passed"

        # 400 on blank/missing/unknown/other-solo why_key, missing attempt_id, or unknown call.
        for bad_body in (
            {
                "started_at": started,
                "band_slot": 0,
                "call": "legal",
                "why_key": "readily-accessible",
            },
            {
                "attempt_id": attempt_id,
                "started_at": started,
                "band_slot": 0,
                "call": "legal",
                "why_key": "",
            },
            {"attempt_id": attempt_id, "started_at": started, "band_slot": 0, "call": "legal"},
            {
                "attempt_id": attempt_id,
                "started_at": started,
                "band_slot": 0,
                "call": "legal",
                "why_key": "unknown-key",
            },
            {
                "attempt_id": attempt_id,
                "started_at": started,
                "band_slot": 0,
                "call": "legal",
                "why_key": "published-key-tension",
            },
            {
                "attempt_id": attempt_id,
                "started_at": started,
                "band_slot": 0,
                "call": "maybe",
                "why_key": "readily-accessible",
            },
        ):
            assert client.post("/api/solos/u4-solo/call", json=bad_body).status_code == 400

        # 409 on stale-page attempt_id mismatch (F2).
        r_stale = client.post(
            "/api/solos/u4-solo/call",
            json={**good_call, "attempt_id": "stale-attempt-id"},
        )
        assert r_stale.status_code == 409

        # 500 on solo_call log failure rolls back solo_calls row so learner can resubmit!
        with monkeypatch.context() as mp:
            def fail_call_log(path, kind, payload, **kw):
                if kind == "solo_call":
                    raise OSError("call log write failed")
                return spy_log_event(path, kind, payload, **kw)
            mp.setattr(console, "log_event", fail_call_log)
            r_err = client.post("/api/solos/u4-solo/call", json=good_call)
            assert r_err.status_code == 500

        # Resubmit succeeds cleanly after rollback!
        r_ok = client.post("/api/solos/u4-solo/call", json=good_call)
        assert r_ok.status_code == 200
        assert r_ok.json()["state"] == "passed"
        assert r_ok.json()["legs"][0]["call_result"]["item_revision"] == "rev-u4-1"

        # Duplicate call on same attempt -> 409.
        r_dup = client.post("/api/solos/u4-solo/call", json=good_call)
        assert r_dup.status_code == 409

        # Managed mode synced solo_start, solo_pass, and solo_call.
        for kind in ("solo_start", "solo_pass", "solo_call"):
            assert (kind, True) in sync_calls, (kind, sync_calls)

        # D16: lessons and rack stay completely accessible regardless of solo state.
        assert client.get("/lesson/1").status_code == 200
        assert client.get("/operate/ism").status_code == 200


# --- 5. Read-only GETs, audit-gated tile visibility, no pre-pass leak, and Back preservation --


def test_all_solo_gets_write_nothing_audit_hides_tile_and_rack_preserves_back_link(
    tmp_path, monkeypatch
):
    from sigint101.solos import find_leaked_words

    solos, audits = _sample_solos_and_audits()

    # Unaudited solo is hidden on /unit/4 and returns 404 on /unit/4/solo and /api/solos/u4-solo.
    with _solo_client(
        tmp_path / "unaudited", solos=solos, solo_audits={}, pack_dir=console.PACK_DIR
    ) as client:
        assert "data-solo-tile" not in client.get("/unit/4").text
        assert client.get("/unit/4/solo").status_code == 404
        assert client.get("/capstone").status_code == 404
        assert client.get("/api/solos/u4-solo").status_code == 404

    # Audited solos: GETs write nothing, Back is preserved across rack picks, and no pre-pass leak!
    now = [1_764_333_000.0]
    _pin_log_clock(monkeypatch, now)

    def make_decoder():
        return Decoder(spawn=lambda cmd: Child(cmd, said=RTL433_ASK), **_tools())

    with _solo_client(
        tmp_path / "audited",
        clock=lambda: now[0],
        probe=_present,
        pack_dir=console.PACK_DIR,
        decoder_factory=make_decoder,
    ) as client:
        # First pass u4-solo completely, then start a NEW attempt (active) so we also test F1
        # leak prevention AFTER an earlier pass!
        assert client.post("/api/solos/u4-solo/start").status_code == 200
        u4_1 = client.get("/api/solos/u4-solo").json()
        started1 = u4_1["started_at"]
        assert client.get("/api/decode").status_code == 200
        client.app.state.decoder.drain()
        assert client.post(
            "/api/solos/u4-solo/call",
            json={
                "attempt_id": u4_1["attempt_id"],
                "started_at": started1,
                "band_slot": 0,
                "call": "legal",
                "why_key": "readily-accessible",
            },
        ).status_code == 200
        assert client.get("/api/solos/u4-solo").json()["state"] == "passed"

        now[0] += 60.0
        assert client.post("/api/solos/u4-solo/start").status_code == 200

        # Check zero DB writes and zero event writes on every GET route!
        writes: list[str] = []
        client.app.state.progress._conn.set_authorizer(_counting_write_authorizer(writes))
        events_path = tmp_path / "audited" / "events.jsonl"
        events_before = events_path.read_bytes()

        r_unit = client.get("/unit/4")
        r_solo_html = client.get("/unit/4/solo")
        r_cap_html = client.get("/capstone")
        r_solo_api = client.get("/api/solos/u4-solo")
        r_lora_rack = client.get("/operate/lora")

        assert r_unit.status_code == 200 and "data-solo-tile" in r_unit.text
        assert r_solo_html.status_code == 200
        assert r_cap_html.status_code == 200
        assert r_solo_api.status_code == 200
        assert r_lora_rack.status_code == 200
        assert writes == [], f"GET wrote to SQLite: {writes}"
        assert events_path.read_bytes() == events_before

        # F1 / F2: Pre-pass JSON and hunt HTML leak ZERO forbidden words or scoring fields even
        # after an earlier pass — only "Passed before · YYYY-MM-DD".
        payload = r_solo_api.json()
        assert payload["state"] == "active"
        assert payload["passed_before_date"] == "2025-11-28"
        raw_json = json.dumps(payload)
        for secret_key in ("legal_answers", "ambiguous", "supports", "feedback", "allowed",
                           "freq_hz", "rtl_433"):
            assert secret_key not in raw_json, (secret_key, raw_json)
        assert find_leaked_words(raw_json) == []
        # Extract the solo-screen body (excluding script/nav chrome) and confirm no leaks.
        assert "Passed before" in r_solo_html.text
        assert "rtl_433" not in r_solo_html.text
        assert "433.92" not in r_solo_html.text

        # F4: /operate?back=/unit/4/solo and /operate?back=/capstone preserve back on redirect
        # and across rack picker links; unsafe external back is dropped to "/".
        for back_path, label in (("/unit/4/solo", "Solo"), ("/capstone", "Capstone")):
            redir = client.get(f"/operate?back={back_path}", follow_redirects=False)
            assert redir.status_code == 303
            assert redir.headers["location"].endswith(f"?back={back_path}")
            rack_page = client.get(redir.headers["location"]).text
            assert f'class="btn-back" href="{back_path}"' in rack_page
            assert label in rack_page
            # Picking a second instrument preserves back!
            assert f'href="/operate/rds?back={back_path}"' in rack_page
            second = client.get(f"/operate/rds?back={back_path}").text
            assert f'class="btn-back" href="{back_path}"' in second

        unsafe = client.get("/operate?back=https://evil.example/x", follow_redirects=False)
        assert unsafe.status_code == 303
        assert "evil.example" not in unsafe.headers["location"]
        assert "?back=" not in unsafe.headers["location"]


def test_solo_stand_down_honors_tuning_gate_launch_permit_and_replay_refusal(
    tmp_path, monkeypatch
):
    """Solo Start obeys C81 tuning gate + launch permit, C76a release_for_claim, and 5a-2 replay."""
    from sigint101.radio_control import TuningBusy

    with _solo_client(tmp_path / "gate", probe=_present) as client:
        # 1. While a SigMF replay is running, solo Start refuses with 503 and does NOT stop replay.
        replay = client.app.state.replay
        stopped_replay = [False]
        with monkeypatch.context() as mp:
            mp.setattr(type(replay), "running", property(lambda _self: True))
            mp.setattr(replay, "stop", lambda: stopped_replay.__setitem__(0, True))
            r_rep = client.post("/api/solos/u4-solo/start")
        assert r_rep.status_code == 503
        assert console.REPLAY_RUNNING in (r_rep.json().get("error") or "")
        assert stopped_replay[0] is False

    with _solo_client(tmp_path / "permit", probe=_present) as client:
        gate = client.app.state.tuning_gate
        # 2. If a lesson tuning restore holds the gate past budget, solo Start refuses with 503.
        real_wait = gate.wait_idle
        monkeypatch.setattr(
            gate,
            "wait_idle",
            lambda **kw: (_ for _ in ()).throw(TuningBusy("lesson tuning restore in progress")),
        )
        r_busy = client.post("/api/solos/u4-solo/start")
        assert r_busy.status_code == 503
        assert "restore" in (r_busy.json().get("error") or "").lower()
        monkeypatch.setattr(gate, "wait_idle", real_wait)

        # 3. Normal stand-down acquires launch permit and uses release_for_claim inside it.
        events_seen: list[str] = []
        real_admit = gate.admit_claim
        real_end = gate.end_claim

        def spy_admit(owner=None):
            events_seen.append(f"admit:{owner}")
            return real_admit(owner)

        def spy_end(owner=None):
            events_seen.append(f"end:{owner}")
            return real_end(owner)

        monkeypatch.setattr(gate, "admit_claim", spy_admit)
        monkeypatch.setattr(gate, "end_claim", spy_end)

        dec = client.app.state.decoder
        real_rfc = getattr(dec, "release_for_claim", dec.release)

        def spy_rfc():
            events_seen.append("release_for_claim")
            return real_rfc()

        monkeypatch.setattr(dec, "release_for_claim", spy_rfc)
        r_ok = client.post("/api/solos/u4-solo/start")
        assert r_ok.status_code == 200
        assert events_seen == ["admit:solo", "release_for_claim", "end:solo"]


# --- 7. Round 4 TDD regressions: F1 exact run identity, F2 attempt_id, F3 call durability, F4 ---


def test_f1_parked_pocsag_hook_cannot_borrow_reopened_run_live_or_backfill(
    tmp_path, monkeypatch
):
    """F1: a stopped POCSAG reader hook released after reopen must not credit live or backfill."""
    import threading

    now = [1_764_340_000.0]
    _pin_log_clock(monkeypatch, now)

    children: list = []

    def make_pocsag():
        def spawn(freq_hz, device):
            n = len(children) + 1
            # First run emits a page; second run is silent.
            said = POCSAG_ALPHA if n == 1 else ""
            ch = Child(
                ["rtl_fm", "-M", "fm", "-f", str(freq_hz), "-d", str(device)],
                said=said,
            )
            children.append(ch)
            return ch

        return PocsagDecoder(spawn=spawn, **_tools())

    hook_entered = threading.Event()
    release_hook = threading.Event()
    hook_done = threading.Event()

    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        pocsag_factory=make_pocsag,
    ) as client:
        pocsag = client.app.state.pocsag
        real_hook = pocsag.on_reception
        first_parked = [True]

        def parked_hook(*args, **kwargs):
            if first_parked[0]:
                first_parked[0] = False
                hook_entered.set()
                assert release_hook.wait(timeout=5.0), "test timed out releasing parked hook"
                try:
                    return real_hook(*args, **kwargs)
                finally:
                    hook_done.set()
            return real_hook(*args, **kwargs)

        pocsag.on_reception = parked_hook

        # 1. Start POCSAG child 1; its reader thread enters parked_hook before reception_lock.
        assert client.get("/api/pocsag").status_code == 200
        assert len(children) == 1
        assert hook_entered.wait(timeout=5.0), "POCSAG child 1 reader never reached parked hook"

        # 2. Solo Start stands down and reaps child 1 while its reader hook is still parked.
        now[0] += 5.0
        assert client.post("/api/solos/u5-solo/start").status_code == 200
        assert children[0].poll() is not None, "solo Start did not reap POCSAG child 1"

        # 3. Reopen POCSAG with a silent child 2 while the solo is active.
        assert client.get("/api/pocsag").status_code == 200
        assert len(children) == 2
        assert children[1].poll() is None

        # 4. Release child 1's parked hook: must NOT credit u5-solo live!
        now[0] += 5.0
        release_hook.set()
        assert hook_done.wait(timeout=5.0), "parked hook did not finish after release"
        assert client.get("/api/solos/u5-solo").json()["state"] == "active", (
            "stopped POCSAG child 1 borrowed reopened child 2's order to credit live"
        )

    # 5. Restart console (startup backfill): child 1's reception must STILL not credit u5-solo!
    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        pocsag_factory=make_pocsag,
    ) as client2:
        assert client2.get("/api/solos/u5-solo").json()["state"] == "active", (
            "stopped POCSAG child 1 reception credited u5-solo on startup backfill"
        )


def test_f1_parked_pocsag_failed_live_credit_then_restart_backfill(tmp_path, monkeypatch):
    """F1: failed live solo credit on a stopped run must not credit on restart backfill either."""
    import threading

    now = [1_764_341_000.0]
    _pin_log_clock(monkeypatch, now)
    children: list = []

    def make_pocsag():
        def spawn(freq_hz, device):
            n = len(children) + 1
            said = POCSAG_ALPHA if n == 1 else ""
            ch = Child(
                ["rtl_fm", "-M", "fm", "-f", str(freq_hz), "-d", str(device)],
                said=said,
            )
            children.append(ch)
            return ch

        return PocsagDecoder(spawn=spawn, **_tools())

    hook_entered = threading.Event()
    release_hook = threading.Event()
    hook_done = threading.Event()

    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        pocsag_factory=make_pocsag,
    ) as client:
        pocsag = client.app.state.pocsag
        real_hook = pocsag.on_reception
        first_parked = [True]

        def parked_hook(*args, **kwargs):
            if first_parked[0]:
                first_parked[0] = False
                hook_entered.set()
                assert release_hook.wait(timeout=5.0), "timed out waiting to release hook"
                try:
                    return real_hook(*args, **kwargs)
                finally:
                    hook_done.set()
            return real_hook(*args, **kwargs)

        pocsag.on_reception = parked_hook
        assert client.get("/api/pocsag").status_code == 200
        assert hook_entered.wait(timeout=5.0)

        now[0] += 5.0
        assert client.post("/api/solos/u5-solo/start").status_code == 200
        assert children[0].poll() is not None
        assert client.get("/api/pocsag").status_code == 200

        # Block solo_passes inserts to simulate a failed live solo credit write.
        db_path = tmp_path / "progress.db"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute(
                "CREATE TRIGGER fail_solo_credit BEFORE INSERT ON solo_passes "
                "BEGIN SELECT RAISE(ABORT, 'credit busy'); END"
            )
        now[0] += 5.0
        release_hook.set()
        assert hook_done.wait(timeout=5.0)
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("DROP TRIGGER fail_solo_credit")

    # Restart console -> backfill must NOT grant the stopped child 1's reception!
    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        pocsag_factory=make_pocsag,
    ) as client2:
        assert client2.get("/api/solos/u5-solo").json()["state"] == "active", (
            "backfill credited stopped POCSAG child 1 after failed live credit and restart"
        )


def test_f1_parked_rtl433_get_cannot_advance_order_after_solo_start(tmp_path, monkeypatch):
    """F1: a parked GET /api/decode resuming after solo Start must not advance order."""
    import threading

    now = [1_764_342_000.0]
    _pin_log_clock(monkeypatch, now)
    children: list = []

    def make_decoder():
        def spawn(cmd):
            ch = Child(cmd, said="")
            children.append(ch)
            return ch

        return Decoder(spawn=spawn, **_tools())

    get_claimed = threading.Event()
    release_get = threading.Event()
    get_status: list[int] = []

    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        decoder_factory=make_decoder,
    ) as client:
        real_log = console.log_event
        park_first = [True]

        def park_decode_start(path, kind, payload, **kw):
            if kind == "decode_start" and park_first[0]:
                park_first[0] = False
                get_claimed.set()
                assert release_get.wait(timeout=5.0), "timed out releasing parked GET /api/decode"
            return real_log(path, kind, payload, **kw)

        monkeypatch.setattr(console, "log_event", park_decode_start)

        t = threading.Thread(
            target=lambda: get_status.append(client.get("/api/decode").status_code),
            daemon=True,
        )
        t.start()
        assert get_claimed.wait(timeout=5.0), "GET /api/decode never reached _started"
        assert len(children) == 1 and children[0].poll() is None

        # Solo Start stands down and reaps child 1 while the earlier GET is parked before _started.
        now[0] += 5.0
        assert client.post("/api/solos/u4-solo/start").status_code == 200
        assert children[0].poll() is not None

        # Release the parked GET so _started("decode", ...) finishes without spawning a new child.
        release_get.set()
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert get_status == [200]
        assert len(children) == 1

        # Emitting on the already-released decoder must NOT credit u4-solo live or on backfill!
        now[0] += 5.0
        client.app.state.decoder.on_reception(
            {"model": "Acurite-Tower", "mod": "ASK", "readings": {"temperature_C": 18.0}},
            command="rtl_433 -f 433920000 -F json -d 0",
            freq_hz=433_920_000,
            radio={"kind": "rtl-sdr", "index": 0, "serial": None},
        )
        assert client.get("/api/solos/u4-solo").json()["state"] == "active", (
            "parked GET /api/decode advanced order of released decoder and credited u4-solo"
        )

    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        decoder_factory=make_decoder,
    ) as client2:
        assert client2.get("/api/solos/u4-solo").json()["state"] == "active", (
            "parked GET /api/decode caused backfill to credit released decoder"
        )


def test_f2_try_again_at_same_clock_reading_does_not_reuse_pass_or_call_and_refuses_stale(
    tmp_path, monkeypatch
):
    """F2: Try again at the exact same wall-clock reading starts a fresh attempt_id."""
    now = [1_764_343_000.0]
    _pin_log_clock(monkeypatch, now)

    def make_decoder():
        return Decoder(spawn=lambda cmd: Child(cmd, said=RTL433_ASK), **_tools())

    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        decoder_factory=make_decoder,
    ) as client:
        # 1. Start attempt 1, hear live ISM, submit legal call -> passed.
        r1 = client.post("/api/solos/u4-solo/start")
        assert r1.status_code == 200
        body1 = r1.json()
        aid1 = body1.get("attempt_id")
        assert isinstance(aid1, str) and aid1, "solo payload must expose durable attempt_id"
        assert client.get("/api/decode").status_code == 200
        client.app.state.decoder.drain()
        heard = client.get("/api/solos/u4-solo").json()
        assert heard["state"] == "reception_passed"
        assert heard["attempt_id"] == aid1

        call1 = client.post(
            "/api/solos/u4-solo/call",
            json={
                "attempt_id": aid1,
                "started_at": heard["started_at"],
                "band_slot": 0,
                "call": "legal",
                "why_key": "readily-accessible",
            },
        )
        assert call1.status_code == 200
        assert call1.json()["state"] == "passed"

        # 2. Press Try again at the EXACT SAME clock reading (now[0] unchanged)!
        r2 = client.post("/api/solos/u4-solo/start")
        assert r2.status_code == 200
        body2 = r2.json()
        aid2 = body2.get("attempt_id")
        assert isinstance(aid2, str) and aid2 and aid2 != aid1
        assert body2["started_at"] == body1["started_at"]
        assert body2["state"] == "active", (
            "Try again at same clock reading inherited old pass/call state"
        )
        assert body2["legs"][0]["passed"] is False
        assert body2["legs"][0]["called"] is False
        assert "heard" not in body2["legs"][0]
        assert "call_result" not in body2["legs"][0]
        assert body2["passed_before_date"] is not None

        # 3. Stale submission carrying attempt 1's attempt_id (same started_at!) is refused 409.
        stale = client.post(
            "/api/solos/u4-solo/call",
            json={
                "attempt_id": aid1,
                "started_at": body2["started_at"],
                "band_slot": 0,
                "call": "legal",
                "why_key": "readily-accessible",
            },
        )
        assert stale.status_code == 409

    # 4. Restart console (backfill): old attempt's reception at same clock must not credit #2.
    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        decoder_factory=make_decoder,
    ) as client2:
        after = client2.get("/api/solos/u4-solo").json()
        assert after["attempt_id"] == aid2
        assert after["state"] == "active"
        assert after["legs"][0]["passed"] is False


@pytest.mark.parametrize("managed", [False, True])
def test_f3_legal_call_denied_delete_rollback_quarantine_and_sync_error_after_append(
    tmp_path, monkeypatch, managed
):
    """F3: legal-call 500 leaves no row or visible solo_call event; bad rollback quarantines."""
    now = [1_764_344_000.0]
    _pin_log_clock(monkeypatch, now)
    run_dir = tmp_path / ("managed" if managed else "legacy")
    run_dir.mkdir()
    if managed:
        migrate_store_copy(
            tmp_path / "f3_p.db", run_dir / "progress.db",
            store="progress", allow_missing_source=True,
            scratch_dir=tmp_path / "f3_sp",
        )
        migrate_store_copy(
            tmp_path / "f3_e.db", run_dir / "exam.db",
            store="exam", allow_missing_source=True,
            scratch_dir=tmp_path / "f3_se",
        )

    def make_decoder():
        return Decoder(spawn=lambda cmd: Child(cmd, said=RTL433_ASK), **_tools())

    with _solo_client(
        run_dir,
        clock=lambda: now[0],
        probe=_present,
        decoder_factory=make_decoder,
        managed=managed,
    ) as client:
        r_start = client.post("/api/solos/u4-solo/start")
        assert r_start.status_code == 200
        assert client.get("/api/decode").status_code == 200
        client.app.state.decoder.drain()
        heard = client.get("/api/solos/u4-solo").json()
        assert heard["state"] == "reception_passed"
        aid = heard["attempt_id"]
        call_payload = {
            "attempt_id": aid,
            "started_at": heard["started_at"],
            "band_slot": 0,
            "call": "legal",
            "why_key": "readily-accessible",
        }

        # (A) Fail journal write while SQLite authorizer denies DELETE on solo_calls:
        # staged transaction rolls back without DELETE -> 500, GET stays reception_passed,
        # retry succeeds!
        def deny_delete(action, arg1, arg2, db_name, trigger_name):
            if action == sqlite3.SQLITE_DELETE and arg1 == "solo_calls":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        client.app.state.progress._conn.set_authorizer(deny_delete)
        try:
            real_log = console.log_event
            with monkeypatch.context() as mp:
                mp.setattr(
                    console,
                    "log_event",
                    lambda path, kind, payload, **kw: (
                        (_ for _ in ()).throw(OSError("journal unavailable"))
                        if kind == "solo_call"
                        else real_log(path, kind, payload, **kw)
                    ),
                )
                r_fail = client.post("/api/solos/u4-solo/call", json=call_payload)
                assert r_fail.status_code == 500
            after_fail = client.get("/api/solos/u4-solo").json()
            assert after_fail["state"] == "reception_passed", (
                "denied DELETE left solo_calls committed after 500"
            )
            assert after_fail["legs"][0]["called"] is False
            assert [
                e for e in read_events(run_dir / "events.jsonl") if e["kind"] == "solo_call"
            ] == []
        finally:
            client.app.state.progress._conn.set_authorizer(None)

        # (B) Event sync error AFTER append flushes bytes: 500 leaves NO committed row and NO
        # visible solo_call event in events.jsonl, and retry still succeeds!
        if managed:
            real_fsync = events_mod.fsync_handle
            sync_calls = [0]

            def fail_after_flush(handle):
                handle.flush()
                sync_calls[0] += 1
                raise OSError("injected sync error after append")

            with monkeypatch.context() as mp:
                mp.setattr(events_mod, "fsync_handle", fail_after_flush)
                r_sync = client.post("/api/solos/u4-solo/call", json=call_payload)
                assert r_sync.status_code == 500
            assert sync_calls[0] >= 1
            monkeypatch.setattr(events_mod, "fsync_handle", real_fsync)
            assert client.get("/api/solos/u4-solo").json()["state"] == "reception_passed"
            assert [
                e for e in read_events(run_dir / "events.jsonl") if e["kind"] == "solo_call"
            ] == [], "500 after sync failure left a visible solo_call event in events.jsonl"

        # Retry succeeds cleanly!
        r_ok = client.post("/api/solos/u4-solo/call", json=call_payload)
        assert r_ok.status_code == 200
        assert r_ok.json()["state"] == "passed"
        assert len(
            [e for e in read_events(run_dir / "events.jsonl") if e["kind"] == "solo_call"]
        ) == 1

    # (C) Unconfirmed rollback quarantines the store and GET never reports passed.
    q_dir = tmp_path / f"quarantine_{managed}"
    q_dir.mkdir()
    if managed:
        migrate_store_copy(
            tmp_path / "f3q_p.db", q_dir / "progress.db",
            store="progress", allow_missing_source=True,
            scratch_dir=tmp_path / "f3q_sp",
        )
        migrate_store_copy(
            tmp_path / "f3q_e.db", q_dir / "exam.db",
            store="exam", allow_missing_source=True,
            scratch_dir=tmp_path / "f3q_se",
        )
    with _solo_client(
        q_dir,
        clock=lambda: now[0],
        probe=_present,
        decoder_factory=make_decoder,
        managed=managed,
    ) as client_q:
        assert client_q.post("/api/solos/u4-solo/start").status_code == 200
        assert client_q.get("/api/decode").status_code == 200
        client_q.app.state.decoder.drain()
        heard_q = client_q.get("/api/solos/u4-solo").json()
        aid_q = heard_q["attempt_id"]

        orig_conn = client_q.app.state.progress._conn

        class _BadRollbackConn:
            def __getattr__(self, name):
                return getattr(orig_conn, name)

            def rollback(self):
                raise sqlite3.OperationalError("rollback refused")

        client_q.app.state.progress._conn = _BadRollbackConn()
        real_log = console.log_event
        with monkeypatch.context() as mp:
            mp.setattr(
                console,
                "log_event",
                lambda path, kind, payload, **kw: (
                    (_ for _ in ()).throw(OSError("disk full"))
                    if kind == "solo_call"
                    else real_log(path, kind, payload, **kw)
                ),
            )
            r_q = client_q.post(
                "/api/solos/u4-solo/call",
                json={
                    "attempt_id": aid_q,
                    "started_at": heard_q["started_at"],
                    "band_slot": 0,
                    "call": "legal",
                    "why_key": "readily-accessible",
                },
            )
            assert r_q.status_code == 500
        assert client_q.app.state.progress.quarantined_reason is not None
        r_get = client_q.get("/api/solos/u4-solo")
        assert r_get.status_code == 503
        assert r_get.json()["state"] == "unreadable"
        with closing(sqlite3.connect(q_dir / "progress.db")) as db:
            assert db.execute("SELECT count(*) FROM solo_calls").fetchone() == (0,)


def test_f4_leaf_order_lock_never_nests_and_stand_down_uses_claim_sdr(tmp_path, monkeypatch):
    """F4: order_lock never nests with reception_lock; stand-down delegates to _claim_sdr."""
    import inspect
    import threading

    now = [1_764_345_000.0]
    _pin_log_clock(monkeypatch, now)

    overlap_detected: list[str] = []
    held_locks: set[str] = set()
    real_lock_cls = threading.Lock

    class _TrackedLock:
        def __init__(self, label):
            self._label = label
            self._inner = real_lock_cls()

        def __enter__(self):
            self._inner.acquire()
            if self._label == "order" and "reception" in held_locks:
                overlap_detected.append("order inside reception")
            if self._label == "reception" and "order" in held_locks:
                overlap_detected.append("reception inside order")
            held_locks.add(self._label)
            return self

        def __exit__(self, *exc):
            held_locks.discard(self._label)
            self._inner.release()
            return False

    def make_decoder():
        return Decoder(spawn=lambda cmd: Child(cmd, said=RTL433_ASK), **_tools())

    with _solo_client(
        tmp_path,
        clock=lambda: now[0],
        probe=_present,
        decoder_factory=make_decoder,
    ) as client:
        # Expose and wrap the app's reception_lock and order_lock via app.state probes.
        assert hasattr(client.app.state, "order_lock")
        assert hasattr(client.app.state, "reception_lock")

        # Check stand-down source does not duplicate radio_arbiter.reserve.
        app_src = inspect.getsource(console.create_app)
        sd_idx = app_src.index("def _stand_down_for_solo")
        sd_end = app_src.index("def _solo_qualifies", sd_idx)
        sd_body = app_src[sd_idx:sd_end]
        assert "radio_arbiter.reserve" not in sd_body, (
            "_stand_down_for_solo must delegate to _claim_sdr instead of repeating admission"
        )

        # Verify order_lock is never acquired while reception_lock is held during Start or credit.
        orig_order = client.app.state.order_lock
        orig_rec = client.app.state.reception_lock
        tracked_order = _TrackedLock("order")
        tracked_rec = _TrackedLock("reception")
        assert orig_order is not orig_rec
        client.app.state.order_lock = tracked_order
        client.app.state.reception_lock = tracked_rec

        assert client.post("/api/solos/u4-solo/start").status_code == 200
        assert client.get("/api/decode").status_code == 200
        client.app.state.decoder.drain()
        assert client.get("/api/solos/u4-solo").json()["state"] == "reception_passed"
        assert overlap_detected == [], f"order_lock nested with reception_lock: {overlap_detected}"
