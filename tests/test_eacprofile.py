"""Tests for parsing and checking EAC's saved option profile."""

import os

import pytest

from cdrip import eacprofile

STOCK_SCHEME = "%tracknr2% %title% - %artist%"
ENCODER_ARGS = (
    '-6 -V -T "ARTIST=%artist%" -T "TITLE=%title%" -T "TRACKNUMBER=%tracknr%" '
    "%source% -o %dest%"
)


def _blob(strings):
    """Build a file in the shape EAC writes: magic header, UTF-16LE strings."""
    out = bytearray(eacprofile.MAGIC + "1300".encode("utf-16-le"))
    for s in strings:
        out += b"\x00\x00" + s.encode("utf-16-le") + b"\x00\x00"
    return bytes(out)


@pytest.fixture
def profile_path(tmp_path):
    def make(strings):
        p = tmp_path / "p.cfg"
        p.write_bytes(_blob(strings))
        return str(p)
    return make


def test_rejects_a_file_that_is_not_a_profile(tmp_path):
    p = tmp_path / "nope.cfg"
    p.write_bytes(b"not an EAC profile at all")
    with pytest.raises(ValueError):
        eacprofile.read(str(p))


def test_reads_strings_and_version(profile_path):
    prof = eacprofile.read(profile_path([eacprofile.WANTED_SCHEME]))
    assert prof.version.startswith("EACV")
    assert eacprofile.WANTED_SCHEME in prof.strings


def test_encoder_command_line_is_not_mistaken_for_a_naming_scheme(profile_path):
    """It contains %tracknr% and %title%, so a loose filter picks it up."""
    prof = eacprofile.read(profile_path([eacprofile.WANTED_SCHEME, ENCODER_ARGS]))
    assert prof.naming_schemes == [eacprofile.WANTED_SCHEME]


def test_catalog_format_is_not_mistaken_for_a_naming_scheme(profile_path):
    catalog = "%tracknr%;%artist%;%title%;%tracklen%"
    prof = eacprofile.read(profile_path([eacprofile.WANTED_SCHEME, catalog]))
    assert prof.naming_schemes == [eacprofile.WANTED_SCHEME]


def test_naming_schemes_are_deduplicated_in_file_order(profile_path):
    prof = eacprofile.read(profile_path(
        [eacprofile.WANTED_SCHEME, STOCK_SCHEME, eacprofile.WANTED_SCHEME]))
    assert prof.naming_schemes == [eacprofile.WANTED_SCHEME, STOCK_SCHEME]


def test_flac_level_is_read_from_the_command_line(profile_path):
    prof = eacprofile.read(profile_path([ENCODER_ARGS]))
    assert prof.flac_level == 6


def test_level_six_is_reported(profile_path):
    prof = eacprofile.read(profile_path([eacprofile.WANTED_SCHEME, ENCODER_ARGS]))
    problems = eacprofile.check(prof)
    assert any("compression is -6" in p for p in problems)


def test_level_eight_passes(profile_path):
    prof = eacprofile.read(profile_path(
        [eacprofile.WANTED_SCHEME, ENCODER_ARGS.replace("-6 ", "-8 ", 1)]))
    assert eacprofile.check(prof) == []


def test_missing_wanted_scheme_is_reported(profile_path):
    prof = eacprofile.read(profile_path(
        [STOCK_SCHEME, ENCODER_ARGS.replace("-6 ", "-8 ", 1)]))
    problems = eacprofile.check(prof)
    assert any("2.3.13" in p for p in problems)


def test_other_stored_schemes_are_not_reported_on_their_own(profile_path):
    """EAC keeps one scheme per encoder plus a various-artists one.

    Warning about those would fire on every healthy profile, so the only
    reportable condition is the wanted scheme being absent.
    """
    prof = eacprofile.read(profile_path(
        [eacprofile.WANTED_SCHEME, STOCK_SCHEME, "%tracknr2% %title%",
         ENCODER_ARGS.replace("-6 ", "-8 ", 1)]))
    assert eacprofile.check(prof) == []


def test_missing_verify_flag_is_reported(profile_path):
    args = ENCODER_ARGS.replace("-6 -V ", "-8 ", 1)
    prof = eacprofile.read(profile_path([eacprofile.WANTED_SCHEME, args]))
    assert any("-V" in p for p in eacprofile.check(prof))


def test_nonexistent_encoder_is_reported(profile_path):
    missing = os.path.join(os.sep, "definitely", "not", "here", "flac.exe")
    prof = eacprofile.read(profile_path(
        [eacprofile.WANTED_SCHEME, ENCODER_ARGS.replace("-6 ", "-8 ", 1), missing]))
    assert any("does not exist" in p for p in eacprofile.check(prof))


def test_find_profiles_returns_nothing_for_a_missing_directory(tmp_path):
    assert eacprofile.find_profiles(str(tmp_path / "absent")) == []
