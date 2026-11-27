# ABOUTME: Read-only Wi-Fi metadata survey via nmcli, grouped into a per-channel census.
# ABOUTME: Reads beacon metadata only (never connects, no payload) and refuses without a radio.

from __future__ import annotations

import glob as _glob
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass

from .childproc import command_line, spawn, stderr_tail, stop_process

NMCLI = "nmcli"
SURVEY_FIELDS = ("SSID", "BSSID", "CHAN", "SIGNAL", "SECURITY")
# Read-only listing of visible access points; never invokes `connect` or mutates NetworkManager.
SURVEY_COMMAND = (NMCLI, "-t", "-f", ",".join(SURVEY_FIELDS), "dev", "wifi", "list")

# Only fields an access point or census row may carry, by construction — no payload field exists.
ENVELOPE_FIELDS = ("ssid", "bssid", "channel", "signal", "security")
CENSUS_FIELDS = ("channel", "count", "hidden", "open", "strongest", "ssids")

# Upper bound on one nmcli scan so a wedged NetworkManager call cannot hang the console.
SCAN_TIMEOUT_S = 10.0


class SurveyUnavailable(RuntimeError):
    """The Wi-Fi survey cannot run. Never a reason to invent a network — the census stays empty."""


@dataclass(frozen=True, slots=True)
class WifiNetwork:
    """One visible access point's broadcast metadata: envelope only, never payload."""

    ssid: str | None
    bssid: str
    channel: int
    signal: int
    security: str

    def as_dict(self) -> dict:
        return {
            "ssid": self.ssid,
            "bssid": self.bssid,
            "channel": self.channel,
            "signal": self.signal,
            "security": self.security,
        }


def split_nmcli_terse(line: str) -> list[str]:
    """Split one `nmcli -t` line on unescaped colons, unescaping `\\:` and `\\\\` per nmcli(1)."""
    fields: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n and line[i + 1] in (":", "\\"):
            buf.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    fields.append("".join(buf))
    return fields


def parse_nmcli_line(line: str) -> WifiNetwork | None:
    """Parse one `nmcli -t -f SSID,BSSID,CHAN,SIGNAL,SECURITY dev wifi list` line, or None."""
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return None
    parts = split_nmcli_terse(raw)
    if len(parts) != len(SURVEY_FIELDS):
        return None
    raw_ssid, raw_bssid, raw_chan, raw_sig, raw_sec = parts
    bssid = raw_bssid.strip().upper()
    if not bssid or bssid == "--":
        return None
    try:
        channel = int(raw_chan.strip())
        signal = int(raw_sig.strip())
    except ValueError:
        return None
    if channel <= 0 or not 0 <= signal <= 100:
        return None
    ssid = None if raw_ssid in ("", "--") else raw_ssid
    sec_clean = raw_sec.strip()
    security = "Open" if sec_clean in ("", "--") else sec_clean
    return WifiNetwork(ssid=ssid, bssid=bssid, channel=channel, signal=signal, security=security)


def parse_nmcli_wifi(text: str) -> list[WifiNetwork]:
    """Parse full `nmcli -t ... dev wifi list` stdout, deduplicating (bssid, channel)."""
    by_key: dict[tuple[str, int], WifiNetwork] = {}
    for line in text.splitlines():
        net = parse_nmcli_line(line)
        if net is None:
            continue
        key = (net.bssid, net.channel)
        prev = by_key.get(key)
        if prev is None or net.signal > prev.signal:
            by_key[key] = net
    return list(by_key.values())


def channel_census(networks: list[WifiNetwork]) -> list[dict]:
    """Aggregate visible access points into a per-channel metadata census, sorted by channel."""
    grouped: dict[int, list[WifiNetwork]] = {}
    for net in networks:
        grouped.setdefault(net.channel, []).append(net)
    rows: list[dict] = []
    for channel in sorted(grouped):
        bucket = grouped[channel]
        rows.append({
            "channel": channel,
            "count": len(bucket),
            "hidden": sum(1 for net in bucket if net.ssid is None),
            "open": sum(1 for net in bucket if net.security == "Open"),
            "strongest": max(net.signal for net in bucket),
            "ssids": sorted({net.ssid for net in bucket if net.ssid is not None}),
        })
    return rows


def _wifi_radio_present() -> bool:
    """True when Linux exposes at least one wireless network interface under /sys/class/net."""
    return bool(_glob.glob("/sys/class/net/*/wireless"))


def _popen(argv):
    """Launch nmcli through the console's managed child-process helper."""
    return spawn(argv, stdout=subprocess.PIPE, text=True)


def _close_quietly(stream) -> None:
    if stream is not None and not getattr(stream, "closed", False):
        try:
            stream.close()
        except OSError:
            pass


class WifiSurvey:
    """Runs one bounded, read-only nmcli Wi-Fi scan and returns a per-channel metadata census."""

    def __init__(self, *, spawn=_popen, which=shutil.which,
                 radio_present=_wifi_radio_present, timeout: float = SCAN_TIMEOUT_S):
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present
        self._timeout = timeout
        self._lock = threading.Lock()

    def scan(self) -> dict:
        """Run one read-only nmcli listing, or raise SurveyUnavailable honestly."""
        with self._lock:
            if not self._which(NMCLI):
                raise SurveyUnavailable(
                    f"{NMCLI} is not installed, so SIGINT 101 cannot scan Wi-Fi"
                )
            if not self._radio_present():
                raise SurveyUnavailable(
                    "no Wi-Fi radio on this host — SIGINT 101 will not fake a scan"
                )
            try:
                proc = self._spawn(list(SURVEY_COMMAND))
            except OSError as exc:
                raise SurveyUnavailable(f"cannot run {NMCLI}: {exc}") from exc

            cmd = command_line(proc) or shlex.join(SURVEY_COMMAND)
            try:
                try:
                    stdout, _ = proc.communicate(timeout=self._timeout)
                except subprocess.TimeoutExpired as exc:
                    stop_process(proc)
                    raise SurveyUnavailable(
                        f"{NMCLI} timed out after {self._timeout:g} s — no networks invented"
                    ) from exc

                if proc.returncode != 0:
                    tail = stderr_tail(proc)
                    detail = ": " + " | ".join(tail) if tail else ""
                    lower = detail.lower()
                    if "wi-fi" in lower or "wifi" in lower:
                        if any(word in lower for word in ("disabled", "not found", "unavailable")):
                            raise SurveyUnavailable(f"no Wi-Fi radio ready ({tail[-1]})")
                    raise SurveyUnavailable(
                        f"{NMCLI} stopped (exit status {proc.returncode}){detail}"
                    )
            finally:
                _close_quietly(getattr(proc, "stdout", None))
                _close_quietly(getattr(proc, "stderr", None))

            networks = parse_nmcli_wifi(stdout or "")
            return {
                "command": cmd,
                "total": len(networks),
                "channels": channel_census(networks),
                "networks": [net.as_dict() for net in networks],
            }
