"""Tag defects an EAC rip carries that nothing else notices.

Both of these were fixed by hand on the first release. The point of the module
is that neither is visible in the audio and neither is reported by any other
check, so without this they ship.
"""

import pytest

from cdrip import posttag


@pytest.fixture
def release(tmp_path):
    """Six FLACs; read_tags is stubbed so metaflac is not needed."""
    for i in range(1, 7):
        (tmp_path / ("%02d - Track.flac" % i)).write_bytes(b"x")
    return str(tmp_path)


def _stub(monkeypatch, tags):
    monkeypatch.setattr(posttag, "read_tags", lambda path: dict(tags))


def test_totaltracks_counting_the_data_track_is_caught(monkeypatch, release):
    """EAC counts disc tracks, so a mixed-mode CD claims one track too many."""
    _stub(monkeypatch, {"TOTALTRACKS": ["7"], "ARTIST": ["A Band"]})
    fixes = posttag.check(release, expect_tracks=6)
    assert len(fixes) == 1
    assert fixes[0].field == "TOTALTRACKS"
    assert fixes[0].was == "7" and fixes[0].now == "6"
    assert "data track" in fixes[0].why


def test_correct_totaltracks_is_left_alone(monkeypatch, release):
    _stub(monkeypatch, {"TOTALTRACKS": ["6"], "ARTIST": ["A Band"]})
    assert posttag.check(release, expect_tracks=6) == []


def test_track_count_defaults_to_the_files_present(monkeypatch, release):
    _stub(monkeypatch, {"TOTALTRACKS": ["7"], "ARTIST": ["A Band"]})
    fixes = posttag.check(release)
    assert fixes and fixes[0].now == "6"


def test_placeholder_artist_is_caught(monkeypatch, release):
    """EAC's export hides a track artist equal to the album artist, so a
    stale 'Unknown Artist' stays invisible until the album artist is right."""
    _stub(monkeypatch, {"TOTALTRACKS": ["6"],
                        "ARTIST": ["Unknown Artist"],
                        "ALBUMARTIST": ["A Band"]})
    fixes = posttag.check(release, expect_tracks=6)
    assert any(f.field == "ARTIST" and f.now == "A Band" for f in fixes)


def test_real_artist_is_not_touched(monkeypatch, release):
    _stub(monkeypatch, {"TOTALTRACKS": ["6"], "ARTIST": ["A Band"],
                        "ALBUMARTIST": ["A Band"]})
    assert posttag.check(release, expect_tracks=6) == []


def test_empty_directory_reports_nothing(tmp_path):
    assert posttag.check(str(tmp_path)) == []


# --- tags an upload needs ----------------------------------------------------


def test_missing_required_tags_are_reported(monkeypatch, release):
    _stub(monkeypatch, {"ARTIST": ["A"], "TITLE": ["T"]})
    problems = posttag.missing(release)
    assert any("ALBUM is missing" in p for p in problems)
    assert any("2.3.16.1" in p for p in problems)


def test_missing_upload_tags_explain_the_unknown_tags_outcome(monkeypatch, release):
    """These are what produced a torrent with 'unknown' release type/tags."""
    _stub(monkeypatch, {f: ["x"] for f in posttag.REQUIRED})
    problems = posttag.missing(release)
    assert any("GENRE" in p for p in problems)
    assert any("LABEL" in p for p in problems)
    assert any("CATALOGNUMBER" in p for p in problems)
    assert any("unknown" in p for p in problems)


def test_a_complete_release_reports_nothing(monkeypatch, release):
    _stub(monkeypatch, {f: ["x"] for f in
                        posttag.REQUIRED + posttag.WANTED_FOR_UPLOAD})
    assert posttag.missing(release) == []


def test_missing_reports_an_empty_folder(tmp_path):
    assert posttag.missing(str(tmp_path))


def test_albumartist_absence_is_reported(monkeypatch, release):
    """The one tag filled in by hand on the Insidious Awakening rip.

    Not an upload blocker - salmon builds the group's artists from each
    track's ARTIST - but the library copy is grouped by album artist, so a
    rip that omits it splits the album in a Plex/Lidarr view.
    """
    _stub(monkeypatch, {f: ["x"] for f in
                        posttag.REQUIRED + ("GENRE", "LABEL", "CATALOGNUMBER")})
    problems = posttag.missing(release)
    assert any("ALBUMARTIST is missing" in p for p in problems)


def test_a_tag_missing_from_only_some_files_is_caught(monkeypatch, release):
    """The failure the previous version could not see.

    `missing` used to read files[0] only, so a tag written to track 1 and
    absent from track 5 passed - which is exactly the shape a spot check
    misses, and EAC writes tags per track.
    """
    complete = {f: ["x"] for f in posttag.REQUIRED + posttag.WANTED_FOR_UPLOAD}

    def partial(path):
        tags = dict(complete)
        if path.endswith("05 - Track.flac"):
            del tags["GENRE"]
        return tags

    monkeypatch.setattr(posttag, "read_tags", partial)
    problems = posttag.missing(release)
    assert any("GENRE is missing" in p for p in problems)
    # and it says WHERE, so the fix does not have to be a hunt
    assert any("1 of 6 files" in p and "05 - Track.flac" in p for p in problems)


def test_a_tag_missing_everywhere_does_not_list_files(monkeypatch, release):
    """A wholly absent tag is the common case; naming all six adds nothing."""
    _stub(monkeypatch, {f: ["x"] for f in posttag.REQUIRED})
    problems = posttag.missing(release)
    genre = [p for p in problems if p.startswith("GENRE")][0]
    assert "of 6 files" not in genre
