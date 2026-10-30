# ABOUTME: Polls an Orbic's EFF Rayhunter HTTP API for M19 — the IMSI-catcher / cell-site detector.
# ABOUTME: Receive-only over WiFi; no finding is an empty list, an unreachable Orbic honest refusal.

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

# The Orbic hotspot's Rayhunter web server. The console joins the Orbic's WiFi and polls this HTTP
# API, receive-only — it never transmits or controls the modem. The Orbic is a separate radio over
# WiFi, like the Flock detector's wlan1 or the LoRa listener's meshtasticd, so there is no SDR
# release/resume arbitration: this is not an SDR consumer.
DEFAULT_RAYHUNTER_HOST = "192.168.1.1"
DEFAULT_RAYHUNTER_PORT = 8080
DEFAULT_TIMEOUT = 4.0

# Rayhunter's routes, confirmed in EFForg/rayhunter daemon/src/main.rs get_router():
#   GET /api/qmdl-manifest         -> ManifestStats { entries, current_entry }
#   GET /api/analysis-report/{name} -> newline-delimited JSON (metadata line, then AnalysisRows)
MANIFEST_PATH = "/api/qmdl-manifest"
ANALYSIS_REPORT_PATH = "/api/analysis-report/{name}"

# Rayhunter's EventType (lib/src/analysis/analyzer.rs), ordered Informational < Low < Medium < High.
# Informational "does not result in any alert on the display" — it is diagnostic, never an alert —
# so only Low/Medium/High are surfaced as warnings. Mapped to the lesson's own severity words.
_SEVERITY = {"low": "low", "medium": "medium", "high": "high"}

# "(packet N)" is appended to every analyzer message by the Harness (packet_suffix); it is the only
# per-event offset the row carries, so it is read back out when present.
_PACKET_RE = re.compile(r"\(packet (\d+)\)")


class RayhunterUnavailable(RuntimeError):
    """The Orbic's Rayhunter API cannot be reached. Never a reason to invent a finding — the panel
    says the detector is offline rather than imply the air is clear."""


@dataclass(frozen=True)
class RayhunterRecording:
    """One capture in Rayhunter's manifest (a ManifestEntry), as the panel needs it.

    `active` marks the recording Rayhunter is writing right now (the manifest's current_entry) — the
    green/red "line" is only live while a recording is active. `size_bytes` is qmdl_size_bytes.
    """

    name: str
    start_time: str | None
    last_message_time: str | None
    size_bytes: int | None
    active: bool

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "start_time": self.start_time,
            "last_message_time": self.last_message_time,
            "size_bytes": self.size_bytes,
            "active": self.active,
        }


@dataclass(frozen=True)
class RayhunterManifest:
    """The parsed qmdl-manifest: the past recordings and the active one, if any.

    `recordings` keeps Rayhunter's order (oldest first, so the last is the most recent completed
    capture); `current` is the active recording or None when the Orbic is not recording.
    """

    recordings: tuple[RayhunterRecording, ...]
    current: RayhunterRecording | None


@dataclass(frozen=True)
class RayhunterWarning:
    """One alert-level heuristic finding from an analysis report (a non-Informational Event).

    `name` is the analyzer that fired (IMSI Requested, 2G downgrade, Incomplete SIB, ...), resolved
    from the report metadata; `severity` is low/medium/high; `description` is the analyzer's own
    message; `timestamp` and `packet` locate it when the report carries them. A heuristic can never
    prove a catcher, so a finding is reported as exactly that — a finding — never as certainty.
    """

    name: str
    severity: str
    description: str
    timestamp: str | None
    packet: int | None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "severity": self.severity,
            "description": self.description,
            "timestamp": self.timestamp,
            "packet": self.packet,
        }


def _recording(entry: dict, *, active: bool) -> RayhunterRecording:
    """Build a RayhunterRecording from a manifest entry dict, tolerating missing fields."""
    size = entry.get("qmdl_size_bytes")
    return RayhunterRecording(
        name=str(entry.get("name", "")),
        start_time=entry.get("start_time"),
        last_message_time=entry.get("last_message_time"),
        size_bytes=size if isinstance(size, int) else None,
        active=active,
    )


def parse_manifest(raw: bytes) -> RayhunterManifest:
    """Parse a /api/qmdl-manifest body (ManifestStats JSON) into a RayhunterManifest.

    Rayhunter removes the active recording from `entries` and returns it separately as
    `current_entry`, so the two never double-count. A malformed body is an honest empty manifest.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return RayhunterManifest(recordings=(), current=None)
    if not isinstance(data, dict):
        return RayhunterManifest(recordings=(), current=None)

    entries = data.get("entries")
    recordings = tuple(
        _recording(e, active=False) for e in entries if isinstance(e, dict)
    ) if isinstance(entries, list) else ()

    current_entry = data.get("current_entry")
    current = _recording(current_entry, active=True) if isinstance(current_entry, dict) else None
    return RayhunterManifest(recordings=recordings, current=current)


def _severity_of(event_type) -> str | None:
    """Map a Rayhunter EventType to low/medium/high, or None when it is not an alert.

    Handles both the current string form ("Informational"/"Low"/"Medium"/"High") and the legacy
    tagged form ({"type":"QualitativeWarning","severity":"High"} / {"type":"Informational"}).
    Informational (and anything unrecognised) returns None — it is not a warning.
    """
    if isinstance(event_type, str):
        return _SEVERITY.get(event_type.lower())
    if isinstance(event_type, dict):
        if event_type.get("type") == "QualitativeWarning":
            severity = event_type.get("severity")
            if isinstance(severity, str):
                return _SEVERITY.get(severity.lower())
    return None


def _events_of(row: dict):
    """Yield (event_dict, timestamp) for each event slot in a row, across report formats.

    v2 rows carry `events` at the top level with `packet_timestamp`; legacy v1 rows nest them under
    `analysis[].events`, each with its own `timestamp`. The slot index is kept so it can be matched
    to the analyzer that produced it.
    """
    if isinstance(row.get("events"), list):
        ts = row.get("packet_timestamp")
        for index, event in enumerate(row["events"]):
            yield index, event, ts
    elif isinstance(row.get("analysis"), list):
        for entry in row["analysis"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("events"), list):
                continue
            ts = entry.get("timestamp")
            for index, event in enumerate(entry["events"]):
                yield index, event, ts


def parse_analysis(raw: bytes) -> list[RayhunterWarning]:
    """Parse a /api/analysis-report/{name} body (newline-delimited JSON) to warnings, newest first.

    The first line is the report metadata, whose `analyzers` list names each heuristic; every later
    line is an AnalysisRow whose `events` align by index to that list, so a non-null event's name is
    `analyzers[index].name`. Only Low/Medium/High events become warnings — Informational diagnostic
    events are not alerts. A line that does not parse is skipped rather than failing the report.
    """
    analyzer_names: list[str] = []
    warnings: list[RayhunterWarning] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue

        # The metadata line carries the analyzer roster; capture the names and move on.
        if isinstance(obj.get("analyzers"), list):
            analyzer_names = [
                a.get("name", "") for a in obj["analyzers"] if isinstance(a, dict)
            ]
            continue

        for index, event, ts in _events_of(obj):
            if not isinstance(event, dict):
                continue
            severity = _severity_of(event.get("event_type"))
            if severity is None:
                continue
            message = str(event.get("message", ""))
            match = _PACKET_RE.search(message)
            packet = int(match.group(1)) if match else None
            name = analyzer_names[index] if index < len(analyzer_names) else ""
            warnings.append(RayhunterWarning(
                name=name or "unknown heuristic",
                severity=severity,
                description=message,
                timestamp=ts,
                packet=packet,
            ))

    warnings.reverse()  # rows are in capture order, so the panel wants the latest finding first
    return warnings


def _default_fetch(url: str, timeout: float) -> tuple[int, bytes]:
    """GET a URL with the stdlib, returning (status_code, body). No new pip dependency.

    An HTTP error status (e.g. 404 for a report that does not exist yet) is a real response and is
    returned as (code, body); only a transport failure — connection refused, DNS, timeout — raises,
    which is how the monitor learns the Orbic is unreachable.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (http on the LAN)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class RayhunterMonitor:
    """Polls the Orbic's Rayhunter HTTP API for the recording state and its heuristic findings.

    Receive-only: it reads the manifest and analysis reports and never controls the Orbic. Being a
    separate radio over WiFi, it takes no turns with the RTL-SDR — there is no release()/resume().
    An unreachable Orbic raises RayhunterUnavailable rather than inventing a clean or a caught line.
    """

    def __init__(self, *, host=DEFAULT_RAYHUNTER_HOST, port=DEFAULT_RAYHUNTER_PORT,
                 fetch=_default_fetch, timeout=DEFAULT_TIMEOUT):
        self.host = host
        self.port = int(port)
        self._fetch = fetch
        self.timeout = float(timeout)
        self._running = False
        self.error = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        """True once the Orbic has been reached by start()."""
        return self._running

    def start(self) -> None:
        """Probe the Orbic once; an unreachable one is an honest RayhunterUnavailable, not a panel.

        Like the LoRa listener and the Flock detector, this refuses rather than quietly showing an
        empty state when the radio it reads is not there.
        """
        self._manifest()  # raises RayhunterUnavailable if the Orbic cannot be reached
        self._running = True
        self.error = None

    def stop(self) -> None:
        """Stop polling. Nothing on the Orbic is touched — the console only ever read from it."""
        self._running = False

    def status(self) -> str:
        """The green/red/white line: 'warning', 'clean', or 'off'.

        'off' when the Orbic is not recording; otherwise 'warning' when the active recording's
        analysis holds any Low/Medium/High finding, else 'clean' (recording, nothing flagged — which
        never means no catcher, only that no heuristic has fired).
        """
        manifest = self._manifest()
        if manifest.current is None:
            return "off"
        return "warning" if self._warnings_for(manifest.current.name) else "clean"

    def warnings(self, limit: int | None = None) -> list[dict]:
        """Heuristic findings for the active (or, when stopped, the most recent) recording.

        Newest first, as plain dicts for the API. An empty list is the honest answer when nothing is
        flagged or nothing is recorded — it is never dressed up as 'all clear'.
        """
        recording = self._subject()
        if recording is None:
            return []
        rows = self._warnings_for(recording.name)
        if limit is not None:
            rows = rows[:limit]
        return [w.as_dict() for w in rows]

    def recording(self) -> dict | None:
        """The active recording, or the most recent completed one, as a dict — or None if there is
        no recording at all."""
        recording = self._subject()
        return recording.as_dict() if recording is not None else None

    def _subject(self) -> RayhunterRecording | None:
        """The recording the panel reports on: the active one, else the most recent completed."""
        manifest = self._manifest()
        if manifest.current is not None:
            return manifest.current
        if manifest.recordings:
            return manifest.recordings[-1]
        return None

    def _manifest(self) -> RayhunterManifest:
        """Fetch and parse the qmdl-manifest, or raise RayhunterUnavailable."""
        _, body = self._get(MANIFEST_PATH)
        return parse_manifest(body)

    def _warnings_for(self, name: str) -> list[RayhunterWarning]:
        """Fetch and parse one recording's analysis report; a 404 means no report yet (empty)."""
        status, body = self._get(ANALYSIS_REPORT_PATH.format(name=name))
        if status != 200:
            return []
        return parse_analysis(body)

    def _get(self, path: str) -> tuple[int, bytes]:
        """GET a Rayhunter path, turning any transport failure into RayhunterUnavailable."""
        url = f"{self.base_url}{path}"
        try:
            status, body = self._fetch(url, self.timeout)
        except Exception as exc:  # connection refused, timeout, the Orbic off the WiFi
            self.error = f"Rayhunter is not reachable at {self.base_url} ({exc})"
            raise RayhunterUnavailable(self.error) from exc
        self.error = None
        return status, body
