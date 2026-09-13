"""Tests for cue sheet retargeting.

EAC extracts to WAV and compresses afterwards, deleting the WAVs, but its
cue names the WAVs. A cue pointing at files that do not exist is worse than
no cue: it looks complete and fails the moment anyone uses it.
"""

import os

import pytest

from cdrip import cue

CUE = """REM GENRE Metal
PERFORMER "A Thousand Times Repent"
TITLE "Virtue Has Few Friends"
FILE "01 - One.wav" WAVE
  TRACK 01 AUDIO
    TITLE "One"
    INDEX 01 00:00:00
FILE "02 - Two.wav" WAVE
  TRACK 02 AUDIO
    TITLE "Two"
    INDEX 00 03:48:00
    INDEX 01 03:48:37
"""


@pytest.fixture
def release(tmp_path):
    (tmp_path / "01 - One.flac").write_bytes(b"x")
    (tmp_path / "02 - Two.flac").write_bytes(b"x")
    path = tmp_path / "r.cue"
    path.write_text(CUE, encoding="utf-8", newline="\r\n")
    return str(path), str(tmp_path)


def test_wav_references_are_retargeted_onto_the_flacs(release):
    path, directory = release
    message = cue.retarget(path, directory)
    assert "retargeted" in message
    assert cue.referenced_files(path) == ["01 - One.flac", "02 - Two.flac"]


def test_retargeting_leaves_index_values_alone(release):
    """INDEX values are the disc's gap information; rewriting them would be
    fabricating data."""
    path, directory = release
    cue.retarget(path, directory)
    text = open(path, encoding="utf-8").read()
    assert "INDEX 00 03:48:00" in text
    assert "INDEX 01 03:48:37" in text


def test_retargeting_is_idempotent(release):
    path, directory = release
    cue.retarget(path, directory)
    second = cue.retarget(path, directory)
    assert "already resolve" in second


def test_a_full_path_is_reduced_to_a_bare_filename(tmp_path):
    (tmp_path / "01 - One.flac").write_bytes(b"x")
    path = tmp_path / "r.cue"
    path.write_text('FILE "C:\\\\rips\\\\01 - One.flac" WAVE\n',
                    encoding="utf-8", newline="\r\n")
    cue.retarget(str(path), str(tmp_path))
    assert cue.referenced_files(str(path)) == ["01 - One.flac"]


def test_unmatchable_reference_is_reported_not_guessed(tmp_path):
    path = tmp_path / "r.cue"
    path.write_text('FILE "99 - Nope.wav" WAVE\n', encoding="utf-8", newline="\r\n")
    message = cue.retarget(str(path), str(tmp_path))
    assert "do not exist" in message
    # Left alone rather than pointed at something arbitrary.
    assert cue.referenced_files(str(path)) == ["99 - Nope.wav"]


def test_check_flags_a_missing_file(tmp_path):
    path = tmp_path / "r.cue"
    path.write_text('FILE "01 - Gone.flac" WAVE\n', encoding="utf-8", newline="\r\n")
    problems = cue.check(str(path), str(tmp_path))
    assert any("missing file" in p for p in problems)


def test_check_passes_a_good_cue(release):
    path, directory = release
    cue.retarget(path, directory)
    assert cue.check(path, directory) == []


def test_check_reports_a_cue_that_does_not_exist(tmp_path):
    problems = cue.check(str(tmp_path / "absent.cue"))
    assert problems and "does not exist" in problems[0]


def test_utf16_cue_is_read_and_written_back_as_utf16(tmp_path):
    """EAC writes UTF-16; decoding it as UTF-8 leaves NULs and corrupts it."""
    (tmp_path / "01 - One.flac").write_bytes(b"x")
    path = tmp_path / "r.cue"
    path.write_text('FILE "01 - One.wav" WAVE\n', encoding="utf-16", newline="\r\n")
    cue.retarget(str(path), str(tmp_path))
    assert path.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")
    assert cue.referenced_files(str(path)) == ["01 - One.flac"]


def test_no_file_lines_is_reported(tmp_path):
    path = tmp_path / "r.cue"
    path.write_text("REM nothing here\n", encoding="utf-8", newline="\r\n")
    assert any("no FILE lines" in p for p in cue.check(str(path), str(tmp_path)))


def test_cp1252_cue_keeps_its_ellipsis(tmp_path):
    """EAC writes the system codepage. latin-1 decodes 0x85 as a control
    character, silently corrupting the title on rewrite."""
    (tmp_path / "03 - Waste\u2026 We.flac").write_bytes(b"x")
    path = tmp_path / "r.cue"
    path.write_bytes('FILE "03 - Waste\u2026 We.wav" WAVE\r\n'.encode("cp1252"))
    cue.retarget(str(path), str(tmp_path))
    assert cue.referenced_files(str(path)) == ["03 - Waste\u2026 We.flac"]


def test_placeholder_pregaps_are_flagged(tmp_path):
    """EAC still writes INDEX lines after gap detection crashes; they are
    uniform filler, not measurements."""
    path = tmp_path / "r.cue"
    path.write_text(
        "FILE \"a.wav\" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n"
        "FILE \"b.wav\" WAVE\n  TRACK 02 AUDIO\n    INDEX 00 00:00:00\n"
        "    INDEX 01 00:01:00\n"
        "FILE \"c.wav\" WAVE\n  TRACK 03 AUDIO\n    INDEX 00 00:00:00\n"
        "    INDEX 01 00:01:00\n",
        encoding="utf-8", newline="\r\n")
    problems = cue.suspicious_gaps(str(path))
    assert problems and "never measured" in problems[0]


def test_varying_pregaps_are_accepted(tmp_path):
    path = tmp_path / "r.cue"
    path.write_text(
        "FILE \"b.wav\" WAVE\n    INDEX 00 00:00:00\n    INDEX 01 00:01:37\n"
        "FILE \"c.wav\" WAVE\n    INDEX 00 00:00:00\n    INDEX 01 00:02:11\n",
        encoding="utf-8", newline="\r\n")
    assert cue.suspicious_gaps(str(path)) == []


def test_a_cue_with_no_pregaps_is_fine(tmp_path):
    path = tmp_path / "r.cue"
    path.write_text("FILE \"a.wav\" WAVE\n    INDEX 01 00:00:00\n",
                    encoding="utf-8", newline="\r\n")
    assert cue.suspicious_gaps(str(path)) == []
