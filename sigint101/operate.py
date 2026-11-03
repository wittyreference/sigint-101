# ABOUTME: Operator mode ("the rack"): the instruments it offers, each decoder's band limits, and
# ABOUTME: the provenance-stamped record every live reception is logged to events as.

from __future__ import annotations

import math
import shlex
from dataclasses import dataclass, field

from .spectrum import TuneError

# Which instrument /operate opens when the learner has not used the rack before.
DEFAULT_INSTRUMENT = "waterfall"


@dataclass(frozen=True)
class Band:
    """Where a single-channel decoder may be tuned: a sanity range, not a legal gate (D16).

    It keeps a tap from pointing a decoder somewhere its tool cannot decode at all (APRS on the
    FM band); what is lawful to read inside a band is taught, and the pager text rule is the
    POCSAG decoder's own.
    """

    low_hz: int
    high_hz: int
    name: str


@dataclass(frozen=True)
class Instrument:
    """One button in the rack's picker, and what its view drives.

    `receiver` is the app.state attribute of the receiver behind it; `api` the /api/<api> route
    its panel polls (and, with a band, the /api/<api>/tune override). `steps` are the tune steps
    the step key cycles through, the first being the default. `fixed_hz` names where an
    instrument listens when its receiver carries no frequency of its own.
    """

    key: str
    label: str
    tool: str
    receiver: str | None
    api: str | None
    radio: str
    band: Band | None = None
    steps: tuple = ()
    fixed_hz: tuple = field(default=())


_WATERFALL_STEPS = (100_000, 1_000_000, 10_000_000, 10_000)

INSTRUMENTS = (
    Instrument("waterfall", "Waterfall", "rtl_power", "sweeper", "spectrum", "rtl-sdr",
               steps=_WATERFALL_STEPS),
    Instrument("ism", "ISM", "rtl_433", "decoder", "decode", "rtl-sdr",
               band=Band(300_000_000, 928_000_000, "the sensor bands rtl_433 decodes"),
               steps=(20_000, 100_000, 1_000_000, 10_000_000)),
    Instrument("adsb", "ADS-B", "dump1090", "adsb", "adsb", "rtl-sdr",
               fixed_hz=(1_090_000_000,)),
    Instrument("aprs", "APRS", "multimon-ng", "aprs", "aprs", "rtl-sdr",
               band=Band(144_000_000, 148_000_000, "the 2 m band"),
               steps=(5_000, 25_000, 100_000, 1_000_000)),
    Instrument("pager", "Pager framing", "multimon-ng", "pocsag", "pocsag", "rtl-sdr",
               band=Band(138_000_000, 932_000_000, "the VHF, UHF and 900 MHz paging bands"),
               steps=(12_500, 25_000, 100_000, 1_000_000, 10_000_000)),
    Instrument("acars", "ACARS", "acarsdec", "acars", "acars", "rtl-sdr",
               band=Band(118_000_000, 137_000_000, "the VHF air band"),
               steps=(25_000, 5_000, 100_000, 1_000_000)),
    Instrument("ais", "AIS", "rtl_ais", "ais", "ais", "rtl-sdr",
               fixed_hz=(161_975_000, 162_025_000)),
    Instrument("rds", "RDS", "redsea", "rds", "rds", "rtl-sdr",
               band=Band(87_500_000, 108_000_000, "FM broadcast"),
               steps=(200_000, 100_000, 1_000_000)),
    Instrument("dmr", "DMR/P25", "dsd-fme", "dmr", "dmr", "rtl-sdr",
               band=Band(136_000_000, 941_000_000, "the land-mobile bands"),
               steps=(12_500, 6_250, 25_000, 100_000, 1_000_000, 10_000_000)),
    Instrument("apt", "APT", "satdump", "apt", "apt", "rtl-sdr"),
    Instrument("sstv", "SSTV", "sstv", "sstv", "sstv", "rtl-sdr"),
    Instrument("lora", "LoRa", "meshtasticd", "lora", "lora", "lora"),
    Instrument("flock", "Flock", "tcpdump", "flock", "flock", "wlan1"),
    Instrument("rayhunter", "Rayhunter", "Orbic", "rayhunter", "rayhunter", "orbic"),
    # The Flipper reads the device's identity over its USB CLI; it decodes nothing, so it has no
    # receiver to log receptions from (and so no on_reception hook).
    Instrument("flipper", "Flipper", "USB CLI", None, "flipper", "flipper"),
    # The clock and health views read the sweep: the dial picks the station a ppm measurement
    # captures, and the provenance reference level is read off the live sweep.
    Instrument("clock", "Clock", "ppm", "sweeper", "spectrum", "rtl-sdr", steps=_WATERFALL_STEPS),
    Instrument("health", "Health", "provenance", "sweeper", "spectrum", "rtl-sdr",
               steps=_WATERFALL_STEPS),
)

_BY_KEY = {inst.key: inst for inst in INSTRUMENTS}
_TUNABLE = {inst.api: inst for inst in INSTRUMENTS if inst.band}

# The instruments whose receiver reports receptions (on_reception), keyed by that receiver. The
# sweep-driven views draw spectrum rather than decode anything, so they log no receptions.
RECEIVERS = {inst.receiver: inst for inst in INSTRUMENTS
             if inst.receiver is not None and inst.receiver != "sweeper"}


def instrument(key: str) -> Instrument | None:
    """The rack instrument behind a URL key, or None for a key the rack does not have."""
    return _BY_KEY.get(key)


def tunable(api: str) -> Instrument | None:
    """The single-channel decoder behind /api/<api>/tune, or None if that route takes no tune."""
    return _TUNABLE.get(api)


def validate_decoder_hz(inst: Instrument, value) -> int:
    """Read a decoder frequency override, refusing anything unreadable or outside its band.

    Refused rather than clamped, like the sweep's span gate: a decoder pointed somewhere the
    learner did not ask for would decode the wrong thing and look as if it worked.
    """
    if isinstance(value, bool):
        raise TuneError(f"{inst.label} frequency is not a frequency: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TuneError(f"{inst.label} frequency is not a frequency: {value!r}") from exc
    if not math.isfinite(number):
        raise TuneError(f"{inst.label} frequency is not a finite frequency: {value!r}")
    hz = int(round(number))
    band = inst.band
    if not band.low_hz <= hz <= band.high_hz:
        raise TuneError(
            f"{inst.label} tunes {band.low_hz / 1e6:g}-{band.high_hz / 1e6:g} MHz "
            f"({band.name}); {hz / 1e6:.4f} MHz is outside it")
    return hz


def command_gain(command: str | None) -> str | None:
    """The tuner gain a live command line sets, exactly as written, or None if it sets none.

    rtl_fm, rtl_power and acarsdec take `-g`, satdump `--general_gain`, and dsd-fme carries it
    as the fourth field of its rtl: selector (0 is the tuner's AGC). A tool given no gain runs on
    its own default, which the record leaves as None beside the command that shows it.
    """
    if not command:
        return None
    for stage in command.split(" | "):
        try:
            argv = shlex.split(stage)
        except ValueError:
            continue
        for i, arg in enumerate(argv):
            if arg in ("-g", "--general_gain") and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith("rtl:"):
                parts = arg.split(":")
                if len(parts) >= 4:
                    return parts[3]
    return None


def _join(*parts) -> str:
    return " · ".join(str(p) for p in parts if p not in (None, "", []))


def _readings(readings: dict) -> list:
    """An rtl_433 sensor's message, temperature and humidity first (as the decode panel shows)."""
    readings = readings or {}
    parts = []
    if "temperature_C" in readings:
        parts.append(f"{float(readings['temperature_C']):.1f} °C")
    if "humidity" in readings:
        parts.append(f"{readings['humidity']}% RH")
    if not parts:
        parts = [f"{k} {v}" for k, v in readings.items() if k not in ("id", "channel")]
    return parts


def _aprs(item: dict) -> str:
    if item.get("kind") == "position" and item.get("lat") is not None \
            and item.get("lon") is not None:
        said = f"{item['lat']:.3f}, {item['lon']:.3f}"
    elif item.get("kind") == "message":
        said = _join(f"→{item['addressee']}" if item.get("addressee") else None, item.get("text"))
    else:
        said = item.get("text")
    return _join(item.get("source"), said)


def _pager(item: dict) -> str:
    # The words appear only when the decoder kept them; on 929-932 MHz it never does (M15).
    words = "text withheld" if item.get("text_withheld") else item.get("text")
    length = f"{item['length']} chars" if item.get("length") else None
    cap = f"cap {item['address']}" if item.get("address") is not None else None
    return _join(cap, item.get("kind"), length, words)


def _image(item: dict) -> str:
    meta = item.get("meta") or {}
    return _join(item.get("mode"), meta.get("filename") or item.get("path"))


_SUMMARIES = {
    "ism": lambda d: _join(d.get("model"), *_readings(d.get("readings"))),
    "aprs": _aprs,
    "pager": _pager,
    "rds": lambda d: _join(f"PI {d['pi']}" if d.get("pi") else None, d.get("ps")),
    "acars": lambda d: _join(d.get("flight"), d.get("tail"),
                             f"label {d['label']}" if d.get("label") else None),
    "ais": lambda d: _join(f"MMSI {d['mmsi']}" if d.get("mmsi") is not None else None,
                           d.get("name")),
    "adsb": lambda d: _join(f"ICAO {d['hex_ident']}" if d.get("hex_ident") else None,
                            d.get("callsign")),
    "dmr": lambda d: _join(d.get("protocol"),
                           f"TG {d['talkgroup']}" if d.get("talkgroup") is not None else None,
                           f"SRC {d['source']}" if d.get("source") is not None else None),
    "lora": lambda d: _join(f"from {d['from_id']}" if d.get("from_id") else None,
                            f"SNR {d['snr_db']} dB" if d.get("snr_db") is not None else None,
                            d.get("summary")),
    "flock": lambda d: _join(d.get("mac"), d.get("tier")),
    "rayhunter": lambda d: _join(d.get("name"), d.get("severity")),
    "apt": _image,
    "sstv": _image,
}


def reception_summary(key: str, item: dict) -> str:
    """The one line the rack's reception log shows for a decoded item."""
    summarize = _SUMMARIES.get(key)
    return summarize(item or {}) if summarize else ""


def reception_record(inst: Instrument, item: dict, *, freq_hz, command, ppm, radio) -> dict:
    """The provenance a live reception is logged with: what heard it, where, and how.

    `freq_hz` is where the receiver listened (a list for a two-channel decoder, None where the
    radio picks its own channel); `ppm` is the clock-correction state; `radio` names the radio
    (an RTL-SDR's serial and index, or the separate radio's host). The time is the event's own.
    """
    return {
        "instrument": inst.key,
        "tool": inst.tool,
        "freq_hz": freq_hz,
        "gain": command_gain(command),
        "ppm": ppm,
        "radio": radio,
        "command": command,
        "summary": reception_summary(inst.key, item),
        "item": item,
    }
