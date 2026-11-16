# ABOUTME: Contract test for the developer ledger (.claude/learnings.md): the M11 LoRa entry records
# ABOUTME: packets sent with an antenna off, so the same entry must carry the caution against it.

import re
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / ".claude" / "learnings.md"
M11_HEADING = "## 2026-10-27: M11 reads LoRa"

# The record of what was done on 2026-10-27. It is history and stays as written.
ANTENNA_OFF = re.compile(r"antenna (?:off|removed)|removing the antenna", re.IGNORECASE)

# The caution. Each check looks anywhere in the entry, in any case, with the * emphasis marks
# dropped ("Do **not**" reads "Do not"), so the caution can be re-worded or moved in the entry:
# - PROHIBITION: "do not", "don't" (straight or curly apostrophe), "never", "must not" or
#   "should not";
# - "transmit", as plain text (so "transmitter" counts too);
# - FIFTY_OHM_LOAD: "50 ohm" (hyphen, space or neither; "ohm", "ohms" or the ohm sign), an
#   optional comma, at most three more words, then "load", "terminator" or "termination"; or
#   "load (50 ohms)" with the same spellings.
PROHIBITION = re.compile(
    r"\b(?:do not|don['\N{RIGHT SINGLE QUOTATION MARK}]t|never|must not|should not)\b",
    re.IGNORECASE)
FIFTY_OHMS = r"50[ -]?(?:ohms?|[\N{GREEK CAPITAL LETTER OMEGA}\N{OHM SIGN}])"
FIFTY_OHM_LOAD = re.compile(
    rf"\b{FIFTY_OHMS},?(?: [\w.-]+){{0,3}} (?:load|terminator|termination)"
    rf"|\bload \({FIFTY_OHMS}\)", re.IGNORECASE)


def _m11_entries() -> list[str]:
    """Every entry whose heading starts with M11_HEADING (there should be one): its text up to the
    next line that starts "## ", on one line, with the * emphasis marks dropped."""
    sections = re.split(r"^(?=## )", LEDGER.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return [" ".join(section.partition("\n")[2].replace("*", "").split())
            for section in sections if section.startswith(M11_HEADING)]


def _entry_with_the_record() -> str:
    """The M11 entry if there is exactly one and it records the antenna-off step, else "". On ""
    the two caution tests pass without checking: the first test then says what is missing."""
    entries = _m11_entries()
    return entries[0] if len(entries) == 1 and ANTENNA_OFF.search(entries[0]) else ""


def test_the_m11_entry_records_packets_sent_with_an_antenna_off():
    # The premise of the two tests below: the entry tells how M11's below-floor SNR was captured,
    # and the capture took an antenna off while LoRa packets were being sent.
    entries = _m11_entries()
    assert entries, (
        f"looked in {LEDGER.name} for a heading starting {M11_HEADING!r} and found none; if the "
        "entry was moved to .claude/learnings-archive.md, point LEDGER at that file")
    assert len(entries) == 1, (
        f"looked in {LEDGER.name} for one heading starting {M11_HEADING!r} and found "
        f"{len(entries)}")
    assert ANTENNA_OFF.search(entries[0]), (
        "looked in the M11 entry for 'antenna off', 'antenna removed' or 'removing the antenna' "
        "and found none")


def test_the_antenna_off_record_says_not_to_repeat_it_on_a_node_that_can_transmit():
    # Transmitting into an open jack can damage a LoRa radio (docs/quickstart.md), and CLAUDE.md
    # tells agents to grep this ledger, so the entry itself has to say not to do it again.
    entry = _entry_with_the_record()
    if not entry:
        return
    assert PROHIBITION.search(entry), (
        "looked in the M11 entry for 'do not', 'don't', 'never', 'must not' or 'should not' and "
        "found none")
    assert "transmit" in entry.lower(), "looked in the M11 entry for 'transmit' and found none"


def test_the_antenna_off_caution_names_a_50_ohm_load():
    # What a LoRa jack carries when it has no antenna on it: a 50-ohm load, never nothing.
    entry = _entry_with_the_record()
    if not entry:
        return
    assert FIFTY_OHM_LOAD.search(entry), (
        "looked in the M11 entry for '50 ohm' (hyphen, space or neither; 'ohm', 'ohms' or the ohm "
        "sign) with 'load', 'terminator' or 'termination' at most three words after it, or for "
        "'load (50 ohms)', and found neither")
