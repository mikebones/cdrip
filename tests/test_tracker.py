"""Tests for the pre-upload tracker check.

Built from the real shape of group 377594, which is the case that motivated
this: a FLAC WEB release and an MP3 CD release, and a FLAC CD rip that is
therefore not a duplicate of either.
"""

import pytest

from cdrip import tracker


def _torrent(tid, fmt, encoding, media, label="", catno=""):
    return tracker.Torrent(
        id=tid, format=fmt, encoding=encoding, media=media,
        label=label, catalogue=catno, edition_year=0, edition_title="",
        log_score=None, has_log=False, has_cue=False, size=0,
    )


@pytest.fixture
def group():
    return tracker.Group(
        id=377594, name="Virtue Has Few Friends",
        artist="A Thousand Times Repent", year=2007,
        tags=["metal", "metalcore", "hardcore.punk", "christian", "deathcore"],
        release_type=5,
        torrents=[
            _torrent(756114, "MP3", "V2 (VBR)", "CD"),
            _torrent(3912237, "FLAC", "Lossless", "WEB",
                     "Tribunal Records", "TRB092"),
        ],
    )


def test_flac_cd_is_not_a_duplicate_of_flac_web(group):
    """2.2.11.1.1 - a different medium is not a duplicate."""
    assert group.would_duplicate("FLAC", "Lossless", "CD") is None


def test_flac_web_would_duplicate_itself(group):
    hit = group.would_duplicate("FLAC", "Lossless", "WEB")
    assert hit is not None and hit.id == 3912237


def test_mp3_cd_would_duplicate_the_existing_one(group):
    hit = group.would_duplicate("MP3", "V2 (VBR)", "CD")
    assert hit is not None and hit.id == 756114


def test_release_type_is_named_not_guessed(group):
    """Release type 5 is an EP; leaving it unset lands the upload as unknown."""
    assert group.release_type_name == "EP"


def test_unknown_release_type_is_reported_as_its_number():
    g = tracker.Group(id=1, name="x", artist="y", year=2000, release_type=99)
    assert "99" in g.release_type_name


def test_edition_for_medium_prefers_a_sibling_on_the_same_medium(group):
    """No CD sibling carries edition info here, so this must be None."""
    assert group.edition_for("CD") is None
    assert group.edition_for("WEB").catalogue == "TRB092"


def test_preflight_says_not_a_duplicate_for_flac_cd(group):
    lines = "\n".join(tracker.preflight(group))
    assert "Not a duplicate" in lines
    assert "DUPLICATE" not in lines


def test_preflight_flags_a_real_duplicate(group):
    lines = "\n".join(tracker.preflight(group, media="WEB"))
    assert "DUPLICATE" in lines
    assert "3912237" in lines


def test_preflight_carries_tags_and_release_type(group):
    lines = "\n".join(tracker.preflight(group))
    assert "metalcore" in lines
    assert "EP" in lines


def test_preflight_warns_that_cross_medium_edition_info_is_unconfirmed(group):
    """TRB092 comes off the WEB release; it is evidence, not proof, for the CD."""
    lines = "\n".join(tracker.preflight(group))
    assert "TRB092" in lines
    assert "confirm it against the disc" in lines


def test_preflight_reports_a_group_with_no_edition_info_at_all():
    g = tracker.Group(id=1, name="x", artist="y", year=2000, release_type=1,
                      torrents=[_torrent(1, "MP3", "320", "CD")])
    lines = "\n".join(tracker.preflight(g))
    assert "2.1.22" in lines


def test_occupied_slots_are_format_encoding_medium(group):
    assert group.occupied_slots() == {
        ("MP3", "V2 (VBR)", "CD"), ("FLAC", "Lossless", "WEB"),
    }
