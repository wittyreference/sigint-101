# ABOUTME: Reception badge eligibility and canonical evidence digests.
# ABOUTME: Only a live reception with instrument-specific provenance earns a stamp.

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import PurePosixPath

from .operate import without_page_words

STAMP_INSTRUMENTS = ("ism", "adsb", "aprs", "pager", "acars", "ais", "rds", "dmr",
                     "apt", "sstv", "meteor", "lora")

# Exclusive upper bound of a UTC datetime, so every accepted timestamp can be displayed.
_END_OF_YEAR_9999 = 253_402_300_800


def _nonblank(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def _bridge_address(address) -> bool:
    if not isinstance(address, str):
        return False
    host, _, port = address.rpartition(":")
    return (bool(host) and host.casefold() != "none" and not any(c.isspace() for c in host)
            and 0 < len(port) <= 5 and port.isascii() and port.isdecimal()
            and 0 < int(port) <= 65_535)


def _complete_provenance(instrument, payload) -> bool:
    radio = payload.get("radio")
    if not isinstance(radio, dict) or not _nonblank(payload.get("tool")):
        return False
    if instrument == "lora":
        return radio.get("kind") == "meshtasticd" and _bridge_address(radio.get("host"))
    index, serial = radio.get("index"), radio.get("serial")
    if (radio.get("kind") != "rtl-sdr" or type(index) is not int or index < 0
            or (serial is not None and not _nonblank(serial))
            or not _nonblank(payload.get("command"))):
        return False
    hz = payload.get("freq_hz")
    if instrument == "ais" and isinstance(hz, list):
        return bool(hz) and all(_positive_int(channel) for channel in hz)
    return _positive_int(hz)


def _recorded(fields) -> bool:
    return bool(fields.get("replay") or fields.get("sample")
                or fields.get("source") in ("replay", "sample"))


def _meteor_picture(item):
    path, product, pass_id = item.get("path"), item.get("product"), item.get("pass_id")
    if _recorded(item) or not all(_nonblank(value) for value in (path, product, pass_id)):
        return False
    image = PurePosixPath(path)
    return (image.parts[:2] == ("meteor", pass_id) and len(image.parts) >= 3
            and ".." not in image.parts and image.suffix == ".png")


def mint(event) -> dict | None:
    """Copy a logged reception's first-heard evidence, or refuse incomplete or recorded data."""
    if not isinstance(event, dict) or event.get("kind") != "reception":
        return None
    ts, payload = event.get("ts"), event.get("payload")
    if (type(ts) not in (int, float) or not 0 < ts < _END_OF_YEAR_9999
            or not isinstance(payload, dict) or not isinstance(payload.get("item"), dict)
            or _recorded(event) or _recorded(payload)):
        return None
    instrument = payload.get("instrument")
    if instrument not in STAMP_INSTRUMENTS or not _complete_provenance(instrument, payload):
        return None
    if instrument == "meteor" and not _meteor_picture(payload["item"]):
        return None
    # The withheld words affect neither the stored evidence nor its digest. Reading the JSON
    # back also copies nested radio and item details, so later mutations cannot change them.
    try:
        canonical = json.dumps(without_page_words(event), sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return None
    return {"instrument": instrument, "first_ts": ts,
            "provenance": json.loads(canonical)["payload"],
            "digest": hashlib.sha256(canonical).hexdigest()}


def stamps_from_log(events):
    """Yield badge credits in append order, including identical pre-ID log lines."""
    legacy = Counter()
    for event in events:
        if (stamp := mint(event)) is None:
            continue
        if "event_id" not in event:
            digest = stamp["digest"]
            legacy[digest] += 1
            if legacy[digest] > 1:
                # The occurrence is part of the watermark only. The evidence hash stays canonical.
                stamp["watermark"] = f"{digest}{legacy[digest]:016x}"
        yield stamp
