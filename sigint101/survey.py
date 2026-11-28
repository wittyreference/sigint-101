# ABOUTME: Read-only Wi-Fi metadata survey via nmcli, grouped into a band+channel census.
# ABOUTME: Checks radio/device state first, uses --rescan no, and never connects or reads payload.

from __future__ import annotations

import glob as _glob
import os
import re
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass

from .childproc import command_line, spawn, stderr_tail, stop_process

NMCLI = "nmcli"
RADIO_COMMAND = (NMCLI, "-t", "-f", "WIFI", "radio")
DEVICE_COMMAND = (NMCLI, "-t", "-f", "DEVICE,TYPE,STATE", "device")
SURVEY_FIELDS = ("SSID", "BSSID", "CHAN", "FREQ", "SIGNAL", "SECURITY")
# Strictly read-only listing of NetworkManager's cached AP list: never triggers a scan or connects.
SURVEY_COMMAND = (
    NMCLI, "-t", "-f", ",".join(SURVEY_FIELDS), "dev", "wifi", "list", "--rescan", "no",
)

# Only fields an access point or census row may carry, by construction — no payload field exists.
ENVELOPE_FIELDS = ("ssid", "bssid", "band", "channel", "freq_mhz", "signal", "security")
CENSUS_FIELDS = ("band", "channel", "count", "hidden", "open", "strongest", "ssids")

# Upper bound on one nmcli call so a wedged NetworkManager call cannot hang the console.
SCAN_TIMEOUT_S = 10.0

_FREQ_RE = re.compile(r"^\s*(\d+)\s*(?:MHz)?\s*$", re.IGNORECASE)
_BAND_ORDER = {"2.4 GHz": 0, "5 GHz": 1, "6 GHz": 2}


class SurveyUnavailable(RuntimeError):
    """The Wi-Fi survey cannot run. Never a reason to invent a network — the census stays empty."""


@dataclass(frozen=True, slots=True)
class WifiNetwork:
    """One visible access point's broadcast metadata: envelope only, never payload."""

    ssid: str | None
    bssid: str
    band: str
    channel: int
    freq_mhz: int
    signal: int
    security: str

    def as_dict(self) -> dict:
        return {
            "ssid": self.ssid,
            "bssid": self.bssid,
            "band": self.band,
            "channel": self.channel,
            "freq_mhz": self.freq_mhz,
            "signal": self.signal,
            "security": self.security,
        }


def band_from_freq_mhz(freq_mhz: int) -> str:
    """Map an 802.11 centre frequency in MHz to its band label (2.4 GHz, 5 GHz, 6 GHz)."""
    if 2400 <= freq_mhz < 2500:
        return "2.4 GHz"
    if 4900 <= freq_mhz < 5900:
        return "5 GHz"
    if 5925 <= freq_mhz <= 7125:
        return "6 GHz"
    return f"{freq_mhz} MHz"


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
    """Parse one `nmcli -t -f SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY dev wifi list` line, or None."""
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return None
    parts = split_nmcli_terse(raw)
    if len(parts) != len(SURVEY_FIELDS):
        return None
    raw_ssid, raw_bssid, raw_chan, raw_freq, raw_sig, raw_sec = parts
    bssid = raw_bssid.strip().upper()
    if not bssid or bssid == "--":
        return None
    freq_match = _FREQ_RE.match(raw_freq)
    if not freq_match:
        return None
    try:
        channel = int(raw_chan.strip())
        freq_mhz = int(freq_match.group(1))
        signal = int(raw_sig.strip())
    except ValueError:
        return None
    if channel <= 0 or freq_mhz <= 0 or not 0 <= signal <= 100:
        return None
    ssid = None if raw_ssid in ("", "--") else raw_ssid
    sec_clean = raw_sec.strip()
    security = "Open" if sec_clean in ("", "--") else sec_clean
    return WifiNetwork(
        ssid=ssid,
        bssid=bssid,
        band=band_from_freq_mhz(freq_mhz),
        channel=channel,
        freq_mhz=freq_mhz,
        signal=signal,
        security=security,
    )


def parse_nmcli_wifi(text: str) -> list[WifiNetwork]:
    """Parse `nmcli -t ... dev wifi list` stdout, deduplicating (bssid, band, channel)."""
    by_key: dict[tuple[str, str, int], WifiNetwork] = {}
    for line in text.splitlines():
        net = parse_nmcli_line(line)
        if net is None:
            continue
        key = (net.bssid, net.band, net.channel)
        prev = by_key.get(key)
        if prev is None or net.signal > prev.signal:
            by_key[key] = net
    return list(by_key.values())


def channel_census(networks: list[WifiNetwork]) -> list[dict]:
    """Aggregate visible APs into a per-(band, channel) metadata census, sorted by band then ch."""
    grouped: dict[tuple[str, int], list[WifiNetwork]] = {}
    for net in networks:
        grouped.setdefault((net.band, net.channel), []).append(net)
    rows: list[dict] = []
    for band, channel in sorted(grouped, key=lambda key: (_BAND_ORDER.get(key[0], 99), key[1])):
        bucket = grouped[(band, channel)]
        rows.append({
            "band": band,
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


def _nmcli_env() -> dict[str, str]:
    """Run nmcli under the C locale so state words (disabled/unmanaged) never localize."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return env


def _popen(argv, *, env=None):
    """Launch nmcli with LC_ALL=C through the console's managed child-process helper."""
    return spawn(argv, stdout=subprocess.PIPE, text=True,
                 env=_nmcli_env() if env is None else env)


def _close_quietly(stream) -> None:
    if stream is not None and not getattr(stream, "closed", False):
        try:
            stream.close()
        except OSError:
            pass


def _check_wifi_devices(devices_stdout: str) -> None:
    """Verify at least one NetworkManager-managed Wi-Fi device is ready (not unmanaged/off)."""
    wifi_states: list[str] = []
    for line in devices_stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        parts = split_nmcli_terse(raw)
        if len(parts) < 3:
            continue
        dev_type = parts[1].strip().lower()
        state = parts[2].strip().lower()
        if dev_type == "wifi":
            wifi_states.append(state)
    if not wifi_states:
        raise SurveyUnavailable("no managed Wi-Fi device ready for NetworkManager")
    if any(state not in ("unmanaged", "unavailable") for state in wifi_states):
        return
    if any(state == "unavailable" for state in wifi_states):
        raise SurveyUnavailable("no Wi-Fi radio ready (device unavailable)")
    raise SurveyUnavailable("no managed Wi-Fi device ready for NetworkManager")


class WifiSurvey:
    """Runs one bounded, read-only nmcli listing and returns a band+channel metadata census."""

    def __init__(self, *, spawn=_popen, which=shutil.which,
                 radio_present=_wifi_radio_present, timeout: float = SCAN_TIMEOUT_S):
        self._spawn = spawn
        self._which = which
        self._radio_present = radio_present
        self._timeout = timeout
        self._lock = threading.Lock()

    def _run_nmcli(self, argv: tuple[str, ...]) -> tuple[str, str]:
        """Run one bounded nmcli command and return (stdout, command_line), or raise honestly."""
        try:
            proc = self._spawn(list(argv))
        except OSError as exc:
            raise SurveyUnavailable(f"cannot run {NMCLI}: {exc}") from exc

        cmd = command_line(proc) or shlex.join(argv)
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
        return stdout or "", cmd

    def scan(self) -> dict:
        """Check Wi-Fi radio and device state, then read NetworkManager's cached AP list."""
        with self._lock:
            if not self._which(NMCLI):
                raise SurveyUnavailable(
                    f"{NMCLI} is not installed, so SIGINT 101 cannot scan Wi-Fi"
                )
            if not self._radio_present():
                raise SurveyUnavailable(
                    "no Wi-Fi radio on this host — SIGINT 101 will not fake a scan"
                )

            # Real nmcli exits 0 with empty output when Wi-Fi is soft-blocked or unmanaged, so
            # check radio and device state explicitly before trusting an empty list.
            radio_out, _ = self._run_nmcli(RADIO_COMMAND)
            if radio_out.strip().lower() in ("disabled", "off"):
                raise SurveyUnavailable("Wi-Fi radio is off — enable Wi-Fi to run the survey")

            devices_out, _ = self._run_nmcli(DEVICE_COMMAND)
            _check_wifi_devices(devices_out)

            stdout, cmd = self._run_nmcli(SURVEY_COMMAND)

            networks = parse_nmcli_wifi(stdout)
            return {
                "command": cmd,
                "cached": True,
                "total": len(networks),
                "channels": channel_census(networks),
                "networks": [net.as_dict() for net in networks],
            }
