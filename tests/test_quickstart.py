# ABOUTME: Tests docs/quickstart.md: the kit table's row for a second Meshtastic node says how to
# ABOUTME: set that node up, so lessons 11 and 28 can hear it and its radio is not damaged.

from pathlib import Path

QUICKSTART = Path(__file__).resolve().parent.parent / "docs" / "quickstart.md"


def test_quickstart_says_how_to_set_up_the_second_meshtastic_node():
    # The try stages of lessons 11 and 28 have room for the antenna, the region and the preset
    # only. A node whose primary channel was renamed lands on another frequency slot even with
    # those right, so the kit table carries the whole set-up, the primary channel included.
    rows = [line.lower() for line in QUICKSTART.read_text(encoding="utf-8").splitlines()
            if line.startswith("| A second Meshtastic node |")]
    assert len(rows) == 1, "the kit table must have one row for 'A second Meshtastic node'"
    missing = [words for words in ("antenna", "region", "long turbo", "primary channel")
               if words not in rows[0]]
    assert not missing, f"the second-node row must name {missing}"
