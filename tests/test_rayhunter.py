# ABOUTME: Tests the Rayhunter backend — parsing the Orbic's qmdl-manifest and analysis-report JSON.
# ABOUTME: The HTTP fetch is a fake at the boundary; an unreachable Orbic is an honest refusal.

import json

import pytest

from sigint101.rayhunter import (
    DEFAULT_RAYHUNTER_HOST,
    DEFAULT_RAYHUNTER_PORT,
    RayhunterMonitor,
    RayhunterUnavailable,
    RayhunterWarning,
    parse_analysis,
    parse_manifest,
)

# --------------------------------------------------------------------------- fixtures
#
# Built by hand from the real Rayhunter serde shapes (EFForg/rayhunter @ main):
#   - ManifestStats { entries, current_entry } in daemon/src/stats.rs
#   - ManifestEntry { name, start_time, last_message_time, qmdl_size_bytes, ... }
#     in daemon/src/qmdl_store.rs  (no serde rename — JSON names == Rust field names)
#   - ReportMetadata { analyzers: [AnalyzerMetadata{key,name,description,version}], ... }
#     AnalysisRow { packet_timestamp, skipped_message_reason, events: [Event|null] }
#     Event { event_type, message }, EventType = "Informational"/"Low"/"Medium"/"High"
#     (plus the legacy tagged form {"type":"QualitativeWarning","severity":"High"})
#     all in lib/src/analysis/analyzer.rs.
# No real Orbic was polled; these reproduce the documented JSON so the parser is unit-tested.


def _entry(name, size, start, last=None, stop_reason=None):
    return {
        "name": name,
        "start_time": start,
        "last_message_time": last,
        "qmdl_size_bytes": size,
        "rayhunter_version": "0.4.0",
        "system_os": "orbic",
        "arch": "armv7",
        "stop_reason": stop_reason,
        "upload_time": None,
        "gps_mode": None,
        "compressed": False,
    }


PAST = _entry("1730200000", 524288, "2026-10-29T10:00:00-07:00",
              "2026-10-29T10:30:00-07:00", stop_reason="user requested")
CURRENT = _entry("1730300000", 131072, "2026-10-29T11:00:00-07:00",
                 "2026-10-29T11:05:00-07:00")

MANIFEST_RECORDING = json.dumps({"entries": [PAST], "current_entry": CURRENT}).encode()
MANIFEST_STOPPED = json.dumps({"entries": [PAST], "current_entry": None}).encode()
MANIFEST_EMPTY = json.dumps({"entries": [], "current_entry": None}).encode()

# The three analyzers whose names the events index into, in order.
_META = {
    "analyzers": [
        {"key": "imsi_requested", "default_enabled": True, "name": "IMSI Requested",
         "description": "flags a suspicious IMSI/IMEI request", "version": 3},
        {"key": "connection_redirect_2g_downgrade", "default_enabled": True,
         "name": "Connection Release/Redirected Carrier 2G Downgrade",
         "description": "detects a forced 2G downgrade", "version": 1},
        {"key": "incomplete_sib", "default_enabled": True, "name": "Incomplete SIB",
         "description": "detects a SIB1 missing later-SIB scheduling", "version": 1},
    ],
    "rayhunter": {"rayhunter_version": "0.4.0", "system_os": "orbic", "arch": "armv7"},
    "report_version": 2,
}


def _report(*rows):
    """Assemble a newline-delimited JSON analysis report: metadata line, then rows."""
    lines = [json.dumps(_META)] + [json.dumps(r) for r in rows]
    return ("\n".join(lines) + "\n").encode()


# A clean recording: metadata and rows that carry no alert (null events only).
CLEAN_ROW = {"packet_timestamp": "2026-10-29T11:00:01-07:00",
             "skipped_message_reason": None, "events": [None, None, None]}
# A forced 2G downgrade — High, from analyzer index 1.
DOWNGRADE_ROW = {"packet_timestamp": "2026-10-29T11:02:03-07:00",
                 "skipped_message_reason": None,
                 "events": [None,
                            {"event_type": "High",
                             "message": "Connection released and redirected to a 2G cell "
                                        "(packet 4213)"},
                            None]}
# An out-of-turn IMSI request — Low, index 0 — alongside an Informational note at index 2
# (the diagnostic analyzer); the Informational event must NOT be surfaced as a warning.
IMSI_ROW = {"packet_timestamp": "2026-10-29T11:03:04-07:00",
            "skipped_message_reason": None,
            "events": [{"event_type": "Low",
                        "message": "IMSI requested without authentication (packet 5102)"},
                       None,
                       {"event_type": "Informational",
                        "message": "Connected to cell 310-410 (packet 5103)"}]}
# An incomplete broadcast — Medium, index 2 — in the legacy tagged event_type form.
SIB_ROW = {"packet_timestamp": "2026-10-29T11:04:05-07:00",
           "skipped_message_reason": None,
           "events": [None, None,
                      {"event_type": {"type": "QualitativeWarning", "severity": "Medium"},
                       "message": "SIB1 missing scheduling for SIB3/SIB5 (packet 6001)"}]}

REPORT_WARNINGS = _report(CLEAN_ROW, DOWNGRADE_ROW, IMSI_ROW, SIB_ROW)
REPORT_CLEAN = _report(CLEAN_ROW, CLEAN_ROW)


# --------------------------------------------------------------------------- manifest parse


def test_parse_manifest_reads_entries_and_the_active_recording():
    man = parse_manifest(MANIFEST_RECORDING)
    assert man.current is not None
    assert man.current.name == "1730300000"
    assert man.current.active is True
    assert man.current.size_bytes == 131072
    assert man.current.start_time == "2026-10-29T11:00:00-07:00"
    assert [r.name for r in man.recordings] == ["1730200000"]
    assert man.recordings[0].active is False


def test_parse_manifest_with_no_active_recording():
    man = parse_manifest(MANIFEST_STOPPED)
    assert man.current is None
    assert [r.name for r in man.recordings] == ["1730200000"]


def test_parse_manifest_empty_is_honest_not_an_error():
    man = parse_manifest(MANIFEST_EMPTY)
    assert man.current is None
    assert man.recordings == ()


def test_parse_manifest_tolerates_missing_fields():
    raw = json.dumps({"entries": [{"name": "x"}], "current_entry": None}).encode()
    man = parse_manifest(raw)
    assert man.recordings[0].name == "x"
    assert man.recordings[0].size_bytes is None
    assert man.recordings[0].start_time is None


# --------------------------------------------------------------------------- analysis parse


def test_parse_analysis_extracts_warnings_named_from_the_metadata():
    warns = parse_analysis(REPORT_WARNINGS)
    # Three alert-level events; the Informational diagnostic note is not a warning.
    assert len(warns) == 3
    by_name = {w.name: w for w in warns}
    assert by_name["Connection Release/Redirected Carrier 2G Downgrade"].severity == "high"
    assert by_name["IMSI Requested"].severity == "low"
    assert by_name["Incomplete SIB"].severity == "medium"       # legacy tagged form maps too
    assert "2G cell" in by_name["Connection Release/Redirected Carrier 2G Downgrade"].description


def test_parse_analysis_orders_newest_first():
    warns = parse_analysis(REPORT_WARNINGS)
    assert [w.severity for w in warns] == ["medium", "low", "high"]   # by timestamp, latest first
    assert warns[0].timestamp == "2026-10-29T11:04:05-07:00"
    assert warns[-1].timestamp == "2026-10-29T11:02:03-07:00"


def test_parse_analysis_captures_the_packet_offset_when_present():
    warns = parse_analysis(REPORT_WARNINGS)
    offsets = {w.name: w.packet for w in warns}
    assert offsets["IMSI Requested"] == 5102
    assert offsets["Connection Release/Redirected Carrier 2G Downgrade"] == 4213


def test_parse_analysis_clean_report_is_empty_never_all_clear():
    assert parse_analysis(REPORT_CLEAN) == []


def test_parse_analysis_tolerates_a_garbage_line():
    raw = REPORT_WARNINGS + b"this is not json\n"
    warns = parse_analysis(raw)
    assert len(warns) == 3                       # the bad line is skipped, the rest still parse


def test_parse_analysis_handles_a_report_with_no_metadata_line():
    # No leading metadata line: the warning is still reported, with a fallback name.
    raw = (json.dumps(DOWNGRADE_ROW) + "\n").encode()
    warns = parse_analysis(raw)
    assert len(warns) == 1
    assert warns[0].severity == "high"
    assert warns[0].name                          # non-empty fallback, never crashes


def test_warning_as_dict_carries_what_the_panel_needs():
    out = parse_analysis(REPORT_WARNINGS)[0].as_dict()
    assert out["name"] == "Incomplete SIB"
    assert out["severity"] == "medium"
    assert out["timestamp"] == "2026-10-29T11:04:05-07:00"
    assert "SIB1" in out["description"]
    assert out["packet"] == 6001


def test_rayhunter_warning_is_frozen():
    w = RayhunterWarning(name="x", severity="high", description="d", timestamp=None, packet=None)
    with pytest.raises(Exception):
        w.severity = "low"


# --------------------------------------------------------------------------- monitor (fetch seam)


def _fetch(manifest=MANIFEST_RECORDING, reports=None, fail=False):
    """A fake HTTP GET at the boundary: maps a Rayhunter URL to (status, body)."""
    reports = reports or {}

    def fetch(url, timeout):
        if fail:
            raise OSError("connection refused")
        if url.endswith("/api/qmdl-manifest"):
            return (200, manifest)
        if "/api/analysis-report/" in url:
            name = url.rsplit("/", 1)[-1]
            if name in reports:
                return (200, reports[name])
            return (404, b"no report yet")
        return (404, b"")

    return fetch


def test_defaults_point_at_the_orbic():
    assert DEFAULT_RAYHUNTER_HOST == "192.168.1.1"
    assert DEFAULT_RAYHUNTER_PORT == 8080


def test_running_start_stop_lifecycle():
    mon = RayhunterMonitor(fetch=_fetch())
    assert mon.running is False
    mon.start()
    assert mon.running is True
    assert mon.error is None
    mon.stop()
    assert mon.running is False


def test_start_refuses_when_the_orbic_is_unreachable():
    mon = RayhunterMonitor(fetch=_fetch(fail=True))
    with pytest.raises(RayhunterUnavailable):
        mon.start()
    assert mon.running is False
    assert mon.error


def test_status_is_warning_when_recording_with_a_finding():
    mon = RayhunterMonitor(fetch=_fetch(reports={"1730300000": REPORT_WARNINGS}))
    mon.start()
    assert mon.status() == "warning"


def test_status_is_clean_when_recording_with_no_finding():
    mon = RayhunterMonitor(fetch=_fetch(reports={"1730300000": REPORT_CLEAN}))
    mon.start()
    assert mon.status() == "clean"


def test_status_is_clean_when_recording_has_no_report_yet():
    # The active recording's analysis report does not exist yet (404): recording, nothing flagged.
    mon = RayhunterMonitor(fetch=_fetch(reports={}))
    mon.start()
    assert mon.status() == "clean"


def test_status_is_off_when_not_recording():
    mon = RayhunterMonitor(fetch=_fetch(manifest=MANIFEST_STOPPED))
    mon.start()
    assert mon.status() == "off"


def test_warnings_returns_dicts_newest_first_with_a_limit():
    mon = RayhunterMonitor(fetch=_fetch(reports={"1730300000": REPORT_WARNINGS}))
    mon.start()
    rows = mon.warnings()
    assert [r["severity"] for r in rows] == ["medium", "low", "high"]
    assert mon.warnings(limit=1) == rows[:1]


def test_warnings_fall_back_to_the_most_recent_recording_when_stopped():
    mon = RayhunterMonitor(
        fetch=_fetch(manifest=MANIFEST_STOPPED, reports={"1730200000": REPORT_WARNINGS}))
    mon.start()
    rows = mon.warnings()
    assert len(rows) == 3


def test_warnings_are_empty_when_nothing_is_recorded():
    mon = RayhunterMonitor(fetch=_fetch(manifest=MANIFEST_EMPTY))
    mon.start()
    assert mon.warnings() == []


def test_recording_exposes_the_active_capture():
    mon = RayhunterMonitor(fetch=_fetch())
    mon.start()
    rec = mon.recording()
    assert rec["name"] == "1730300000"
    assert rec["active"] is True


def test_a_dropped_orbic_mid_poll_raises_and_is_recorded():
    mon = RayhunterMonitor(fetch=_fetch())
    mon.start()
    mon._fetch = _fetch(fail=True)          # the Orbic drops off the WiFi
    with pytest.raises(RayhunterUnavailable):
        mon.status()
    assert mon.error
