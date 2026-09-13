"""Tests for the pure logic in :mod:`cdrip.eacwin`.

Everything here is parsing and payload-shaping, so it runs anywhere; the Win32
calls are not exercised. The cases are the ones that actually bit during
development, written down so they cannot come back silently.
"""

import pytest

from cdrip import eacwin


# --- clipboard payload -------------------------------------------------------


def test_clipboard_is_bare_titles_one_per_line():
    """No header, no numbering, no durations - the import is positional."""
    out = eacwin.titles_to_clipboard({1: "First", 2: "Second"})
    assert out == "First\r\nSecond\r\n"


def test_clipboard_orders_by_track_number_not_insertion():
    out = eacwin.titles_to_clipboard({2: "Second", 1: "First"})
    assert out.splitlines() == ["First", "Second"]


def test_clipboard_rejects_gaps():
    """A gap would silently shift every later title onto the wrong track."""
    with pytest.raises(ValueError):
        eacwin.titles_to_clipboard({1: "First", 3: "Third"})


def test_clipboard_rejects_not_starting_at_one():
    with pytest.raises(ValueError):
        eacwin.titles_to_clipboard({2: "Second", 3: "Third"})


def test_clipboard_rejects_empty():
    with pytest.raises(ValueError):
        eacwin.titles_to_clipboard({})


# --- export parsing ----------------------------------------------------------

EXPORT_HIDDEN_ARTIST = (
    "A Thousand Times Repent - Virtue Has Few Friends\r\n"
    "\r\n"
    "01.\tCurses! Another Shape-Shifting Wraith!\t\t03:48\r\n"
    "02.\tA Band of Hunters Stalk in Edo\t\t05:19\r\n"
)

EXPORT_SHOWN_ARTIST = (
    "A Thousand Times Repent - Virtue Has Few Friends\r\n"
    "\r\n"
    "01.\tUnknown Artist / Curses! Another Shape-Shifting Wraith!\t\t03:48\r\n"
    "07.\tUnknown Artist / Track07\t\t23:19\r\n"
)


def test_parse_export_reads_header():
    got = eacwin.parse_export(EXPORT_HIDDEN_ARTIST)
    assert got["artist"] == "A Thousand Times Repent"
    assert got["album"] == "Virtue Has Few Friends"


def test_parse_export_resolves_omitted_track_artist_to_the_cd_artist():
    """EAC omits a track artist when it equals the CD artist.

    Reading the omission as "no artist" is what hid a stale 'Unknown Artist' on
    every track until the CD artist was corrected.
    """
    got = eacwin.parse_export(EXPORT_HIDDEN_ARTIST)
    assert got["tracks"][1]["artist"] == "A Thousand Times Repent"
    assert got["tracks"][1]["title"] == "Curses! Another Shape-Shifting Wraith!"


def test_parse_export_reads_a_differing_track_artist():
    got = eacwin.parse_export(EXPORT_SHOWN_ARTIST)
    assert got["tracks"][1]["artist"] == "Unknown Artist"
    assert got["tracks"][1]["title"] == "Curses! Another Shape-Shifting Wraith!"


def test_parse_export_keeps_durations_and_track_numbers():
    got = eacwin.parse_export(EXPORT_SHOWN_ARTIST)
    assert sorted(got["tracks"]) == [1, 7]
    assert got["tracks"][7]["duration"] == "23:19"


def test_parse_export_preserves_typography():
    """U+2026 is in compliance.INTENTIONAL and must survive the round trip."""
    text = (
        "Artist - Album\r\n\r\n"
        "03.\tTake Me to the Witch of the Waste… We Have Much to Discuss\t\t03:46\r\n"
    )
    got = eacwin.parse_export(text)
    assert got["tracks"][3]["title"].endswith("We Have Much to Discuss")
    assert "…" in got["tracks"][3]["title"]


def test_parse_export_ignores_the_blank_separator_line():
    got = eacwin.parse_export(EXPORT_HIDDEN_ARTIST)
    assert len(got["tracks"]) == 2


def test_export_format_is_not_the_import_format():
    """The two are asymmetric, which is the whole trap.

    Feeding EAC's own export back in would make the entire line the title.
    """
    exported = EXPORT_HIDDEN_ARTIST.splitlines()[2]
    payload = eacwin.titles_to_clipboard({1: "Curses! Another Shape-Shifting Wraith!"})
    assert exported not in payload
    assert "\t" not in payload
