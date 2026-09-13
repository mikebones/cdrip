"""Tests for the native Windows TOC reader.

The DeviceIoControl call needs a real drive, so what is tested here is the
parsing and the address arithmetic - which is where a TOC reader actually goes
wrong. The fixture is a mixed-mode disc in the shape of the one this was
written for: six audio tracks and a trailing data track.
"""

import struct

import pytest

from cdrip import wintoc


def _entry(number, lba, data=False):
    """One TRACK_DATA: reserved, adr/control, number, reserved, 4-byte LBA."""
    control = wintoc.CONTROL_DATA if data else 0x00
    adr = 0x01
    return struct.pack(">BBBB i", 0x00, (adr << 4) | control, number, 0x00, lba)


def _toc(entries, first, last, leadout):
    body = b"".join(entries) + _entry(wintoc.LEADOUT_TRACK, leadout)
    return struct.pack(">HBB", len(body) + 2, first, last) + body


# Real offsets from "A Thousand Times Repent - Virtue Has Few Friends":
# six audio tracks, a data track at 116325, real lead-out at 232479.
AUDIO = [0, 17100, 41025, 58000, 83000, 101600]
DATA_START = 116325
REAL_LEADOUT = 232479


@pytest.fixture
def mixed_mode():
    entries = [_entry(i + 1, lba) for i, lba in enumerate(AUDIO)]
    entries.append(_entry(7, DATA_START, data=True))
    return _toc(entries, 1, 7, REAL_LEADOUT)


def test_parses_every_track_including_the_data_one(mixed_mode):
    tracks, leadout = wintoc.parse(mixed_mode)
    assert len(tracks) == 7
    assert leadout == REAL_LEADOUT


def test_data_track_is_flagged_from_the_control_bits(mixed_mode):
    tracks, _ = wintoc.parse(mixed_mode)
    assert [t["is_data"] for t in tracks] == [False] * 6 + [True]


def test_leadout_is_not_returned_as_a_track(mixed_mode):
    tracks, _ = wintoc.parse(mixed_mode)
    assert all(t["number"] != wintoc.LEADOUT_TRACK for t in tracks)


def test_addresses_are_big_endian(mixed_mode):
    tracks, _ = wintoc.parse(mixed_mode)
    assert [t["start_lba"] for t in tracks][:6] == AUDIO


def test_reports_the_real_leadout_not_the_audio_end(mixed_mode):
    """AccurateRip keys on the real lead-out; using the audio end makes a
    disc that IS in the database look absent."""
    _, leadout = wintoc.parse(mixed_mode)
    assert leadout == REAL_LEADOUT
    assert leadout > DATA_START


def test_rejects_a_truncated_response():
    with pytest.raises(wintoc.WinTocError):
        wintoc.parse(b"\x00\x02")


def test_rejects_a_toc_with_no_tracks():
    raw = struct.pack(">HBB", 2, 1, 1) + _entry(wintoc.LEADOUT_TRACK, 1000)
    with pytest.raises(wintoc.WinTocError):
        wintoc.parse(raw)


def test_rejects_a_toc_with_no_leadout():
    raw = struct.pack(">HBB", 10, 1, 1) + _entry(1, 0)
    with pytest.raises(wintoc.WinTocError):
        wintoc.parse(raw)


def test_entries_outside_first_last_are_ignored():
    entries = [_entry(1, 0), _entry(9, 50000)]
    raw = _toc(entries, 1, 1, 100000)
    tracks, _ = wintoc.parse(raw)
    assert [t["number"] for t in tracks] == [1]


@pytest.mark.parametrize("given,expected", [
    ("D", r"\\.\D:"),
    ("d", r"\\.\D:"),
    ("D:", r"\\.\D:"),
    ("D:\\", r"\\.\D:"),
    (r"\\.\E:", r"\\.\E:"),
])
def test_device_path_accepts_the_usual_spellings(given, expected):
    assert wintoc.device_path(given) == expected


@pytest.mark.parametrize("bad", ["/dev/cdrom", "", "CDROM", "12"])
def test_device_path_rejects_non_letters(bad):
    with pytest.raises(wintoc.WinTocError):
        wintoc.device_path(bad)
