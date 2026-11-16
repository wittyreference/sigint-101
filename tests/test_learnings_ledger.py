# ABOUTME: Contract test for the developer ledger (.claude/learnings.md): the M11 LoRa entry records
# ABOUTME: packets sent with an antenna off, so the same entry must carry the caution against it.

import re
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / ".claude" / "learnings.md"
M11_HEADING = "## 2026-10-27: M11 reads LoRa"

# The record of what was done on 2026-10-27. It is history and stays as written.
ANTENNA_OFF = re.compile(r"antenna (?:off|removed)|removing the antenna", re.IGNORECASE)

# The caution is read loosely, so that it can be re-worded. It needs one sentence that forbids
# something and names a node that can transmit, and a 50-ohm load named anywhere in the entry.
# "Ohm" may be the word, the Greek letter or the ohm sign; a terminator is a load by another name.
NOT_ON_A_NODE_THAT_CAN_TRANSMIT = re.compile(
    r"\b(?:do not|don't|never|must not)\b[^.]*\btransmit", re.IGNORECASE)
OHM = r"(?:ohms?|[\N{GREEK CAPITAL LETTER OMEGA}\N{OHM SIGN}])"
FIFTY_OHM_LOAD = re.compile(rf"\b50[ -]?{OHM} (?:dummy )?(?:load|terminat)", re.IGNORECASE)


def _m11_entry() -> str:
    """The M11 LoRa entry, from its heading to the next one, with the line wrapping flattened."""
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    starts = [n for n, line in enumerate(lines) if line.startswith(M11_HEADING)]
    assert len(starts) == 1, (
        f"expected one ledger entry headed {M11_HEADING!r}, found {len(starts)}; if the entry "
        "moved, its antenna-off record and the caution move together, and so do these tests")
    body = []
    for line in lines[starts[0] + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return " ".join(" ".join(body).split())


def test_the_m11_entry_records_packets_sent_with_an_antenna_off():
    # The premise of the two tests below: the entry tells how M11's below-floor SNR was captured,
    # and the capture took an antenna off while LoRa packets were being sent.
    assert ANTENNA_OFF.search(_m11_entry()), (
        "the M11 entry no longer records the antenna-off step; the two caution tests below "
        "exist because of that record")


def test_the_antenna_off_record_says_not_to_repeat_it_on_a_node_that_can_transmit():
    # Transmitting into an open jack can damage a LoRa radio (docs/quickstart.md), and CLAUDE.md
    # tells agents to grep this ledger, so the entry itself has to say not to do it again.
    assert NOT_ON_A_NODE_THAT_CAN_TRANSMIT.search(_m11_entry()), (
        "the M11 entry records packets sent with an antenna off, and nowhere says not to repeat "
        "that on a node that can transmit")


def test_the_antenna_off_caution_names_a_50_ohm_load():
    # What a LoRa jack carries when it has no antenna on it: a 50-ohm load, never nothing.
    assert FIFTY_OHM_LOAD.search(_m11_entry()), (
        "the M11 entry's antenna-off caution must name a 50-ohm load as what goes on a LoRa "
        "jack that has no antenna")
