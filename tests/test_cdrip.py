"""Tests built from a real mixed-mode disc.

The fixtures are the actual TOC of "A Thousand Times Repent - Virtue Has Few
Friends" (Tribunal Records, 2007): six audio tracks followed by a CDFS data
track. Its disc ID was confirmed against libdiscid on the ripping machine, so
these tests pin the mixed-mode lead-out rule to a known-good answer.
"""

import textwrap

import pytest

from cdrip import drive, musicbrainz, toc as toc_mod
from cdrip.cli import _release_name
from cdrip.salmon import SalmonTarget, SalmonError

# LBA and length in frames for the six audio tracks, then the data track start.
AUDIO = [
    (1, 0, 17137),
    (2, 17137, 23939),
    (3, 41076, 16942),
    (4, 58018, 24955),
    (5, 82973, 18570),
    (6, 101543, 14634),
]
DATA_TRACK_LBA = 127577
EXPECTED_DISCID = "av9mecK.THzP4T_MmklWalXRMXk-"


def build_toc(with_data_track=True):
    tracks = [
        toc_mod.Track(number=n, start_lba=lba, length_frames=length, is_data=False)
        for n, lba, length in AUDIO
    ]
    leadout = tracks[-1].start_lba + tracks[-1].length_frames
    if with_data_track:
        tracks.append(
            toc_mod.Track(number=7, start_lba=DATA_TRACK_LBA, length_frames=0, is_data=True)
        )
    return toc_mod.Toc(device="/dev/cdrom", tracks=tuple(tracks), leadout_lba=leadout)


def test_mixed_mode_detected():
    t = build_toc()
    assert t.is_mixed_mode
    assert len(t.audio_tracks) == 6
    assert len(t.data_tracks) == 1


def test_leadout_uses_data_track_rule():
    t = build_toc()
    # libdiscid: lead-out is the data track's start (+150) minus 11400.
    assert t.discid_leadout == DATA_TRACK_LBA + 150 - 11400 == 116327


def test_leadout_without_data_track_is_real_leadout():
    t = build_toc(with_data_track=False)
    assert t.discid_leadout == 101543 + 14634 + 150


def test_discid_matches_libdiscid(monkeypatch):
    """The pure-Python fallback must agree with libdiscid byte-for-byte."""
    import builtins

    real_import = builtins.__import__

    def no_libdiscid(name, *args, **kwargs):
        if name == "libdiscid":
            raise ImportError("forced")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_libdiscid)
    disc_id, url = toc_mod.compute_discid(build_toc())
    assert disc_id == EXPECTED_DISCID
    assert "toc=1+6+116327+150+17287+41226+58168+83123+101693" in url


def test_total_duration():
    t = build_toc()
    assert t.total_frames == 116177
    assert t.total_duration == "25:49.03"


CD_PARANOIA_Q = textwrap.dedent(
    """
    cdparanoia III release 10.2 libcdio 2.1.0

    Table of contents (audio tracks only):
    track        length               begin        copy pre ch
    ===========================================================
      1.    17137 [03:48.37]        0 [00:00.00]    no   no  2
      2.    23939 [05:19.14]    17137 [03:48.37]    no   no  2
      3.    16942 [03:45.67]    41076 [09:07.51]    no   no  2
      4.    24955 [05:32.55]    58018 [12:53.43]    no   no  2
      5.    18570 [04:07.45]    82973 [18:26.23]    no   no  2
      6.    14634 [03:15.09]   101543 [22:33.68]    no   no  2
    TOTAL  116177 [25:49.02]    (audio only)
    """
)


def test_cd_paranoia_toc_parsing():
    matches = toc_mod._TRACK_RE.findall(CD_PARANOIA_Q)
    assert len(matches) == 6
    assert matches[0] == ("1", "17137", "0")
    assert matches[5] == ("6", "14634", "101543")


OFFSET_HTML = """
<table>
<tr><td>Slimtype - DVD A  DS8A5SH</td><td>+6</td><td>739</td><td>100%</td></tr>
<tr><td>Slimtype - DS8A5SH</td><td>+6</td><td>10</td><td>100%</td></tr>
<tr><td>Acme - Widget 9000</td><td>-12</td><td>4</td><td>100%</td></tr>
<tr><td>Header row</td><td>not a number</td><td>x</td></tr>
</table>
"""


def test_offset_table_parsing_and_lookup(monkeypatch):
    rows = []
    import re, html as html_mod

    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", OFFSET_HTML, re.S | re.I):
        cells = [
            html_mod.unescape(re.sub(r"<[^>]*>", "", c)).replace("\xa0", " ").strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S | re.I)
        ]
        if len(cells) < 3:
            continue
        m = re.match(r"^([+-]?\d+)$", cells[1].replace(" ", ""))
        if not m:
            continue
        rows.append((drive._normalise(cells[0]), int(m.group(1)), int(re.sub(r"[^0-9]", "", cells[2]) or 0)))

    assert len(rows) == 3  # the header-ish row is rejected

    d = drive.Drive(device="/dev/sr0", vendor="Slimtype", model="DVD A  DS8A5SH", release="XAA2")
    assert drive.lookup_offset(d, rows) == 6

    unknown = drive.Drive(device="/dev/sr0", vendor="Nobody", model="ZZ1", release="")
    assert drive.lookup_offset(unknown, rows) is None


def test_normalise_ignores_padding_and_case():
    assert drive._normalise("DVD A  DS8A5SH") == drive._normalise("dvd a ds8a5sh")


def _release(lengths_ms):
    return {
        "id": "24e97821-d19e-4365-ad04-d4b51c43afe0",
        "title": "Virtue Has Few Friends",
        "media": [
            {
                "format": "CD",
                "tracks": [
                    {"position": i + 1, "title": "T%d" % (i + 1), "length": ms,
                     "recording": {"id": "r%d" % i}}
                    for i, ms in enumerate(lengths_ms)
                ],
            }
        ],
    }


def test_verify_against_toc_accepts_matching_pressing():
    lengths = [length for _, _, length in AUDIO]
    # MusicBrainz rounds to whole seconds; rebuild from that rounding.
    ms = [round(length / 75.0) * 1000 for length in lengths]
    assert musicbrainz.verify_against_toc(_release(ms), lengths) is not None


def test_verify_against_toc_rejects_different_pressing():
    lengths = [length for _, _, length in AUDIO]
    ms = [round(length / 75.0) * 1000 for length in lengths]
    ms[3] += 20_000  # twenty seconds longer: a different version
    assert musicbrainz.verify_against_toc(_release(ms), lengths) is None


def test_verify_against_toc_rejects_wrong_track_count():
    lengths = [length for _, _, length in AUDIO]
    ms = [round(length / 75.0) * 1000 for length in lengths][:5]
    assert musicbrainz.verify_against_toc(_release(ms), lengths) is None


def test_release_name_handles_double_rip_layout():
    assert _release_name("/data/rips/staging/Artist - Album/pass1") == "Artist - Album"
    assert _release_name("/data/rips/staging/Artist - Album") == "Artist - Album"


def test_container_path_maps_into_the_pod():
    t = SalmonTarget(
        host_root="/data/complete/music/complete",
        container_root="/downloads/complete",
    )
    assert t.container_path("/data/complete/music/complete/X") == "/downloads/complete/X"


def test_container_path_rejects_paths_salmon_cannot_see():
    t = SalmonTarget(
        host_root="/data/complete/music/complete",
        container_root="/downloads/complete",
    )
    with pytest.raises(SalmonError):
        t.container_path("/data/rips/staging/X")


# --- disc presence -----------------------------------------------------------
#
# whipper ejects the disc when a rip finishes, so the second pass of a double
# rip finds an empty drive. Observed live: cdrdao then dies with a bare
# FileNotFoundError on its own temp file, which says nothing about the real
# cause. These pin the detection that turns that into a useful message.

class _Proc:
    def __init__(self, out="", err=""):
        self.stdout, self.stderr, self.returncode = out, err, 0


DISC_PRESENT = """
Table of contents (audio tracks only):
  1.    17137 [03:48.37]        0 [00:00.00]    no   no  2
TOTAL  116177 [25:49.02]    (audio only)
"""

NO_MEDIUM = """
++ WARN: error in ioctl CDROMREADTOCHDR: No medium found

Unable find or access a CD-ROM drive with an audio CD in it.
"""


def test_disc_present_true(monkeypatch):
    from cdrip import rip as rip_mod
    monkeypatch.setattr(rip_mod.subprocess, "run", lambda *a, **k: _Proc(DISC_PRESENT))
    assert rip_mod.disc_present("/dev/sr0") is True


def test_disc_present_false_when_ejected(monkeypatch):
    from cdrip import rip as rip_mod
    monkeypatch.setattr(rip_mod.subprocess, "run", lambda *a, **k: _Proc("", NO_MEDIUM))
    assert rip_mod.disc_present("/dev/sr0") is False


def test_wait_for_disc_returns_immediately_when_present(monkeypatch):
    from cdrip import rip as rip_mod
    monkeypatch.setattr(rip_mod, "disc_present", lambda device: True)
    monkeypatch.setattr(rip_mod.time, "sleep", lambda s: None)
    assert rip_mod.wait_for_disc("/dev/sr0") is True


def test_wait_for_disc_gives_up_and_says_so(monkeypatch):
    from cdrip import rip as rip_mod
    monkeypatch.setattr(rip_mod, "disc_present", lambda device: False)
    monkeypatch.setattr(rip_mod.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(rip_mod.time, "sleep", lambda s: None)
    ticks = iter([0.0, 1.0, 999.0])
    monkeypatch.setattr(rip_mod.time, "monotonic", lambda: next(ticks))
    said = []
    assert rip_mod.wait_for_disc("/dev/sr0", timeout=1, notify=said.append) is False
    assert said and "re-insert" in said[0].lower()


# --- formatting rules --------------------------------------------------------
#
# The structural point these pin down: a filename problem has to be caught
# BEFORE the rip. whipper writes a .cue and .log referencing the audio
# filenames and editing a rip log is forbidden, so renaming afterwards
# silently invalidates both. Tag and compression problems are different - they
# change neither filenames nor decoded audio, so they are fixable in place.

from cdrip import compliance


def test_finds_the_real_hyphen_lookalike():
    found = compliance.find_lookalikes("Curses! Another Shape‐Shifting Wraith!")
    assert found == [("‐", "U+2010", "-")]


def test_leaves_intentional_typography_alone():
    """Rewriting a real ellipsis or curly quote is the pointless trump 2.3.18 rejects."""
    assert compliance.find_lookalikes("That Was the Night Everything Changed…") == []
    assert compliance.find_lookalikes("Don’t — Really") == []


def test_catches_cyrillic_passing_as_latin():
    found = compliance.find_lookalikes("Mоster of Puppets")  # Cyrillic o
    assert found and found[0][2] == "o"


def test_normalise_fixes_only_lookalikes():
    src = "Shape‐Shifting…"
    out = compliance.normalise_lookalikes(src)
    assert out == "Shape-Shifting…"  # hyphen fixed, ellipsis kept


def test_metadata_check_flags_lookalikes_before_ripping():
    findings = compliance.check_metadata(
        {1: "Curses! Another Shape‐Shifting Wraith!", 2: "A Band of Hunters Stalk in Edo"},
        album="Virtue Has Few Friends",
        artist="A Thousand Times Repent",
    )
    assert len(findings) == 1
    assert findings[0].rule == "2.3.11.1"
    assert "re-rip" in findings[0].message or "re-run" in findings[0].message.lower() \
        or "renaming after the rip" in findings[0].message


def test_metadata_check_clean_release_has_no_findings():
    assert compliance.check_metadata(
        {1: "A Band of Hunters Stalk in Edo", 2: "So Much for Middle-Earth"},
        album="Virtue Has Few Friends", artist="A Thousand Times Repent",
    ) == []


def _release_folder(tmp_path, names):
    """Build a fake release folder. metaflac/flac are stubbed by the caller."""
    d = tmp_path / "Artist - Album (2007) [CD FLAC]"
    d.mkdir()
    for n in names:
        (d / n).write_bytes(b"fLaC" + b"\0" * 64)
    return d


def _stub_tools(monkeypatch, tags=None, embedded=0, recompress_gain=0.0, id3=False):
    tags = tags or {"ARTIST": "A", "ALBUM": "B", "TITLE": "C", "TRACKNUMBER": "1"}
    monkeypatch.setattr(compliance, "_flac_tags", lambda p: dict(tags))
    monkeypatch.setattr(compliance, "_embedded_bytes", lambda p: embedded)
    monkeypatch.setattr(compliance, "_recompress_gain_pct", lambda p: recompress_gain)
    monkeypatch.setattr(compliance, "_has_id3", lambda p: id3)


def test_release_check_passes_a_clean_release(monkeypatch, tmp_path):
    d = _release_folder(tmp_path, ["01 - One.flac", "02 - Two.flac"])
    _stub_tools(monkeypatch)
    findings = compliance.check_release(str(d), expect_tracks=2)
    assert findings == [], [str(f) for f in findings]


def test_release_check_flags_data_track_leftovers(monkeypatch, tmp_path):
    """2.1.19.3 - an enhanced CD's MP3s and video must not ride along."""
    d = _release_folder(tmp_path, ["01 - One.flac", "bonus.mp3", "sampler.mov"])
    _stub_tools(monkeypatch)
    findings = compliance.check_release(str(d))
    rules = [f.rule for f in findings]
    assert "2.1.19.3" in rules
    assert any(f.severity == compliance.SEVERITY_BLOCKER for f in findings)


def test_release_check_flags_missing_tracks(monkeypatch, tmp_path):
    d = _release_folder(tmp_path, ["01 - One.flac"])
    _stub_tools(monkeypatch)
    findings = compliance.check_release(str(d), expect_tracks=6)
    assert "2.1.19" in [f.rule for f in findings]


def test_release_check_flags_uncompressed_flac_as_fixable(monkeypatch, tmp_path):
    d = _release_folder(tmp_path, ["01 - One.flac"])
    _stub_tools(monkeypatch, recompress_gain=3.16)
    findings = compliance.check_release(str(d), expect_tracks=1)
    hit = [f for f in findings if f.rule == "2.2.10.10"]
    assert hit and hit[0].fixable_in_place


def test_release_check_flags_missing_tags(monkeypatch, tmp_path):
    d = _release_folder(tmp_path, ["01 - One.flac"])
    _stub_tools(monkeypatch, tags={"ARTIST": "A"})
    findings = compliance.check_release(str(d), expect_tracks=1)
    hit = [f for f in findings if f.rule == "2.3.16.4"]
    assert hit and "ALBUM" in hit[0].message and hit[0].fixable_in_place


def test_release_check_flags_long_paths(monkeypatch, tmp_path):
    # Lower the limit instead of creating a 200-character filename: Windows
    # MAX_PATH refuses to create one, so the realistic fixture is unbuildable
    # on this machine and would make the test platform-dependent.
    monkeypatch.setattr(compliance, "MAX_PATH_LEN", 40)
    d = _release_folder(tmp_path, ["01 - A Reasonably Long Track Title.flac"])
    _stub_tools(monkeypatch)
    findings = compliance.check_release(str(d), expect_tracks=1)
    assert "2.3.12" in [f.rule for f in findings]


def test_release_check_flags_missing_track_numbers(monkeypatch, tmp_path):
    d = _release_folder(tmp_path, ["Opening Track.flac"])
    _stub_tools(monkeypatch)
    findings = compliance.check_release(str(d), expect_tracks=1)
    assert "2.3.13" in [f.rule for f in findings]


def test_blockers_are_separable_from_trumpables(monkeypatch, tmp_path):
    d = _release_folder(tmp_path, ["01 - One.flac", "stray.mp3"])
    _stub_tools(monkeypatch, recompress_gain=3.0)
    findings = compliance.check_release(str(d))
    assert compliance.blockers(findings)
    assert len(compliance.blockers(findings)) < len(findings)


# --- rip log facts -----------------------------------------------------------
#
# Two rules can only be answered by the log: nothing survives into the FLACs
# to say whether the disc was a pressed CD or a CD-R, or whether it carried
# pre-emphasis. whipper writes both and nothing read them until now.

from cdrip import riplog

WHIPPER_LOG = """Log created by: whipper 0.10.0 (internal logger)

Ripping phase information:
  Drive: SlimtypeDVD A  DS8A5SH   (revision XAA2)
  Defeat audio cache: true
  Read offset correction: 6
  Overread into lead-out: false
  Gap detection: cdrdao 1.2.4
  CD-R detected: false

Tracks:
  1:
    Filename: 01 - One.flac
    Peak level: 1.0
    Pre-emphasis: false
    Test CRC: C6C31298
    Copy CRC: C6C31298

  2:
    Filename: 02 - Two.flac
    Peak level: 0.9
    Pre-emphasis: true
    Test CRC: 4AA4BB66
    Copy CRC: 4AA4BB66
"""


def _write_log(tmp_path, text):
    d = tmp_path / "release"
    d.mkdir(exist_ok=True)
    (d / "Artist - Album.log").write_text(text, encoding="utf-8")
    return d


def test_reads_the_facts_only_the_log_knows(tmp_path):
    d = _write_log(tmp_path, WHIPPER_LOG)
    facts = riplog.read_log(str(d / "Artist - Album.log"))
    assert facts.cdr_detected is False
    assert facts.cache_defeated is True
    assert facts.read_offset == 6
    assert facts.preemphasised_tracks == (2,)
    assert facts.has_preemphasis


def test_preemphasis_is_attributed_to_the_right_track(tmp_path):
    """Track 1 says false, track 2 says true - order must not smear."""
    d = _write_log(tmp_path, WHIPPER_LOG)
    facts = riplog.read_log(str(d / "Artist - Album.log"))
    assert 1 not in facts.preemphasised_tracks
    assert 2 in facts.preemphasised_tracks


def test_cdr_rip_is_a_blocker(tmp_path):
    """2.2.10.1 - a CD-R copy is not an acceptable source."""
    d = _write_log(tmp_path, WHIPPER_LOG.replace("CD-R detected: false",
                                                 "CD-R detected: true"))
    findings = riplog.check_log(str(d))
    rules = [r for r, _, _ in findings]
    assert "2.2.10.1" in rules
    assert any(sev == "blocker" for _, sev, _ in findings)


def test_clean_pressed_cd_raises_no_blocker(tmp_path):
    d = _write_log(tmp_path, WHIPPER_LOG.replace("Pre-emphasis: true",
                                                 "Pre-emphasis: false"))
    findings = riplog.check_log(str(d))
    assert not [f for f in findings if f[1] == "blocker"]


def test_preemphasis_is_reported_but_not_a_blocker(tmp_path):
    """Allowed in lossless; it just forces its own edition."""
    d = _write_log(tmp_path, WHIPPER_LOG)
    findings = riplog.check_log(str(d))
    hit = [f for f in findings if f[0] == "2.1.21"]
    assert hit and hit[0][1] == "info"
    assert "own edition" in hit[0][2]


def test_undefeated_cache_is_surfaced(tmp_path):
    d = _write_log(tmp_path, WHIPPER_LOG.replace("Defeat audio cache: true",
                                                 "Defeat audio cache: false"))
    findings = riplog.check_log(str(d))
    assert "2.2.10.3" in [r for r, _, _ in findings]


def test_missing_log_is_trumpable(tmp_path):
    d = tmp_path / "nolog"
    d.mkdir()
    findings = riplog.check_log(str(d))
    assert findings and findings[0][0] == "2.2.10.2"


# --- metadata enrichment -----------------------------------------------------
#
# whipper tags from MusicBrainz, which for plenty of small-label releases has
# no label, no catalogue number and no genre. salmon builds its upload payload
# by reading exactly those fields off the files, so if they are empty the
# upload goes out with no edition information. This stage fills them AFTER the
# rip (whipper would overwrite anything earlier) and BEFORE the hand-off.

from cdrip import enrich


def test_titlecase_turns_tracker_tags_into_genres():
    assert enrich._titlecase_tag("hardcore.punk") == "Hardcore Punk"
    assert enrich._titlecase_tag("deathcore") == "Deathcore"


def test_musicbrainz_no_label_placeholder_is_not_treated_as_a_label(monkeypatch):
    """MusicBrainz writes the literal "[no label]" for white-label releases."""
    monkeypatch.setattr(enrich, "_get_json", lambda url, timeout=30: {
        "label-info": [{"label": {"name": "[no label]"}, "catalog-number": None}],
        "genres": [], "release-group": {},
    })
    meta = enrich.from_musicbrainz("some-mbid")
    assert meta.label is None
    assert meta.catalogue is None


def test_musicbrainz_real_label_is_used(monkeypatch):
    monkeypatch.setattr(enrich, "_get_json", lambda url, timeout=30: {
        "label-info": [{"label": {"name": "Tribunal Records"}, "catalog-number": "TRB092"}],
        "genres": [{"name": "deathcore"}], "release-group": {},
    })
    meta = enrich.from_musicbrainz("some-mbid")
    assert meta.label == "Tribunal Records"
    assert meta.catalogue == "TRB092"
    assert meta.genres == ["Deathcore"]


def test_discogs_prefers_styles_over_broad_genres(monkeypatch):
    """Discogs "genres" are broad (Rock); "styles" are what a tracker tags."""
    monkeypatch.setattr(enrich, "_get_json", lambda url, timeout=30: {
        "labels": [{"name": "Tribunal Records", "catno": "TRB092"}],
        "genres": ["Rock"], "styles": ["Deathcore"],
    })
    meta = enrich.from_discogs("4062910")
    assert meta.label == "Tribunal Records"
    assert meta.catalogue == "TRB092"
    assert meta.genres[0] == "Deathcore"   # style first
    assert "Rock" in meta.genres


def test_discogs_release_id_extracted_from_a_musicbrainz_url_rel(monkeypatch):
    monkeypatch.setattr(enrich, "_get_json", lambda url, timeout=30: {
        "relations": [
            {"type": "discogs", "url": {"resource": "https://www.discogs.com/release/4062910-A-Thousand"}},
        ]
    })
    assert enrich.discogs_release_from_musicbrainz("mbid") == "4062910"


def test_no_discogs_relation_returns_none(monkeypatch):
    monkeypatch.setattr(enrich, "_get_json", lambda url, timeout=30: {"relations": []})
    assert enrich.discogs_release_from_musicbrainz("mbid") is None


def test_explicit_values_win_over_lookups(monkeypatch):
    monkeypatch.setattr(enrich, "release_mbid_from_files", lambda d: None)
    meta = enrich.collect("/nowhere", label="My Label", catalogue="CAT1", genres=["Doom"])
    assert (meta.label, meta.catalogue, meta.genres) == ("My Label", "CAT1", ["Doom"])
    assert "explicit" in meta.sources


def test_merge_does_not_overwrite_what_is_already_known():
    a = enrich.Metadata(label="First", genres=["Doom"])
    a.merge(enrich.Metadata(label="Second", catalogue="C2", genres=["Doom", "Sludge"]))
    assert a.label == "First"          # first source wins
    assert a.catalogue == "C2"         # gap filled
    assert a.genres == ["Doom", "Sludge"]   # union, no duplicates


def test_empty_metadata_is_reported_as_empty():
    assert enrich.Metadata().is_empty
    assert not enrich.Metadata(genres=["Doom"]).is_empty


# --- EAC log validation ------------------------------------------------------
#
# Some trackers identify a log purely by its header and reject anything that
# isn't EAC or XLD - a whipper rip scoring 100 in cambia is still rejected as
# "Unrecognized log file!". CheckLog.exe answers that locally, before an
# upload. Its silence is the important case: it prints nothing for a file it
# does not recognise, which must not be read as success.

from cdrip import eac


def test_checklog_silence_means_unrecognised_not_ok(monkeypatch, tmp_path):
    log = tmp_path / "whipper.log"
    log.write_text("Log created by: whipper 0.10.0\n", encoding="utf-8")
    monkeypatch.setattr(eac, "find_checklog", lambda extra=None: "CheckLog.exe")

    class P:
        stdout = ""
        stderr = ""
    monkeypatch.setattr(eac.subprocess, "run", lambda *a, **k: P())

    v = eac.check_log(str(log))
    assert not v.recognised
    assert not v.ok
    assert "not an EAC log" in v.summary


def test_checklog_clean_verdict(monkeypatch, tmp_path):
    log = tmp_path / "eac.log"
    log.write_text("x", encoding="utf-8")
    monkeypatch.setattr(eac, "find_checklog", lambda extra=None: "CheckLog.exe")

    class P:
        stdout = ". Log entry is fine!\n"
        stderr = ""
    monkeypatch.setattr(eac.subprocess, "run", lambda *a, **k: P())

    v = eac.check_log(str(log))
    assert v.recognised and v.ok


def test_checklog_modified_is_not_ok(monkeypatch, tmp_path):
    log = tmp_path / "eac.log"
    log.write_text("x", encoding="utf-8")
    monkeypatch.setattr(eac, "find_checklog", lambda extra=None: "CheckLog.exe")

    class P:
        stdout = ". Log entry was modified, checksum incorrect!\n"
        stderr = ""
    monkeypatch.setattr(eac.subprocess, "run", lambda *a, **k: P())

    v = eac.check_log(str(log))
    assert v.recognised and not v.ok


EAC_LOG = """Exact Audio Copy V1.8 from 15. July 2024

Used drive  : Slimtype DVD A  DS8A5SH   Adapter: 1  ID: 0

Read mode               : Secure
Utilize accurate stream : Yes
Defeat audio cache      : Yes
Make use of C2 pointers : No

Read offset correction                      : 6

Track  1
     Test CRC C6C31298
     Copy CRC C6C31298

Track  2
     Test CRC 4AA4BB66
     Copy CRC 4AA4BB66

==== Log checksum 1B9DA441B4485515F73EC544872BC3C6F8517D49412CD8494E2043D1091A33E1 ====
"""


def test_reads_the_settings_an_eac_log_records(tmp_path):
    p = tmp_path / "eac.log"
    p.write_text(EAC_LOG, encoding="utf-8")
    f = eac.read_log(str(p))
    assert f.ripper.lower().startswith("exact audio copy")
    assert f.read_offset == 6
    assert f.cache_defeated is True
    assert f.accurate_stream is True
    assert f.c2_pointers is False
    assert f.read_mode == "Secure"
    assert f.has_checksum
    assert f.crcs_match


def test_settings_check_passes_a_good_log(tmp_path):
    p = tmp_path / "eac.log"
    p.write_text(EAC_LOG, encoding="utf-8")
    assert eac.check_settings(eac.read_log(str(p)), expected_offset=6) == []


def test_settings_check_catches_a_wrong_offset(tmp_path):
    p = tmp_path / "eac.log"
    p.write_text(EAC_LOG.replace(": 6", ": 0"), encoding="utf-8")
    problems = eac.check_settings(eac.read_log(str(p)), expected_offset=6)
    assert any("bit-shifted" in x for x in problems)


def test_settings_check_catches_a_missing_checksum(tmp_path):
    p = tmp_path / "eac.log"
    p.write_text(EAC_LOG.split("==== Log checksum")[0], encoding="utf-8")
    problems = eac.check_settings(eac.read_log(str(p)))
    assert any("no checksum" in x for x in problems)


def test_settings_check_flags_a_whipper_log(tmp_path):
    p = tmp_path / "w.log"
    p.write_text("whipper 0.10.0\nRead offset correction : 6\n", encoding="utf-8")
    problems = eac.check_settings(eac.read_log(str(p)))
    assert any("whipper" in x for x in problems)


def test_settings_check_catches_mismatched_crcs(tmp_path):
    p = tmp_path / "eac.log"
    p.write_text(EAC_LOG.replace("Copy CRC 4AA4BB66", "Copy CRC DEADBEEF"), encoding="utf-8")
    problems = eac.check_settings(eac.read_log(str(p)), expected_offset=6)
    assert any("not reproducible" in x for x in problems)


def test_ripper_is_found_when_not_at_line_start(tmp_path):
    """whipper writes "Log created by: whipper 0.10.0" - the name is not first.

    Anchoring the ripper regex to line start matched EAC but silently missed
    whipper, so the "this log will not be recognised" warning never fired on
    exactly the logs that need it. Caught against a real whipper log.
    """
    p = tmp_path / "w.log"
    p.write_text("Log created by: whipper 0.10.0 (internal logger)\n", encoding="utf-8")
    facts = eac.read_log(str(p))
    assert facts.ripper is not None
    assert facts.ripper.lower() == "whipper"
    assert any("whipper" in x for x in eac.check_settings(facts))


# --- AccurateRip identity on a mixed-mode disc -------------------------------
#
# Fixtures are EAC's own AccurateRip-Offset-log.txt values for the real disc:
#   FreedB 4f0c1b07, Added 000822ea, Multi 002e671b
# whipper reported all six tracks absent from AccurateRip; EAC found every one
# at confidence 2. Same disc, same drive - the disc IDs differed.

from cdrip import accuraterip


def test_matches_eacs_own_accuraterip_ids():
    """The whole point: reproduce what EAC computed for this disc."""
    ids = accuraterip.disc_ids(
        audio_offsets=[0, 17137, 41076, 58018, 82973, 101543],
        leadout=232479,          # real lead-out, past the data track
        data_track_start=127577,
    )
    assert ids.id1 == 0x000822EA
    assert ids.id2 == 0x002E671B
    assert ids.cddb == 0x4F0C1B07
    assert ids.track_count == 7      # the data track counts
    assert ids.url_path == "dBAR-007-000822ea-002e671b-4f0c1b07.bin"


def test_audio_only_leadout_gives_a_different_disc():
    """Using the MusicBrainz mixed-mode lead-out asks about another disc."""
    real = accuraterip.disc_ids([0, 17137, 41076, 58018, 82973, 101543],
                                leadout=232479, data_track_start=127577)
    audio_only = accuraterip.disc_ids([0, 17137, 41076, 58018, 82973, 101543],
                                      leadout=116177)
    assert audio_only.id1 != real.id1
    assert audio_only.cddb != real.cddb


def test_absence_is_inconclusive_on_a_mixed_mode_disc():
    toc = build_toc(with_data_track=True)
    assert accuraterip.absence_is_inconclusive(toc)
    msg = accuraterip.explain_absence(toc)
    assert "not conclusive" in msg
    assert "confidence 2" in msg


def test_absence_is_conclusive_on_an_audio_only_disc():
    toc = build_toc(with_data_track=False)
    assert not accuraterip.absence_is_inconclusive(toc)
    assert "conclusive" in accuraterip.explain_absence(toc)


def test_identities_for_reports_both_conventions():
    ids = accuraterip.identities_for(build_toc(with_data_track=True))
    assert "audio_only" in ids
    # A mixed-mode TOC has no real disc lead-out recorded, so the AccurateRip
    # identity cannot be computed from it alone - which is worth knowing.
    assert "accuraterip" not in ids


# --- EAC Win32 menu driving --------------------------------------------------
#
# EAC's CLI switches do not rip and its window exposes no actionable UI
# Automation controls, but its menus are real HMENUs reachable via WM_COMMAND.
# The trap below was hit live: "Uncompressed" contains "compressed", so a
# substring match picks WAV (id 478) instead of FLAC (id 771).

from cdrip import eacdrive


def _menu():
    P = eacdrive.MenuItem
    return [
        P(path=("Action", "Test  Copy Selected Tracks", "Uncompressed..."),
          command_id=478, opens_dialog=True),
        P(path=("Action", "Test  Copy Selected Tracks", "Compressed..."),
          command_id=771, opens_dialog=True),
        P(path=("Action", "Detect Gaps"), command_id=539, opens_dialog=False),
        P(path=("Action", "Test Selected Tracks"), command_id=532, opens_dialog=False),
    ]


def test_rip_command_picks_compressed_not_uncompressed():
    """The live bug: substring matching selected WAV output."""
    item = eacdrive.find_rip_command(_menu())
    assert item is not None
    assert item.command_id == 771, "picked %r (id %d) - Uncompressed contains 'compressed'" % (
        item.path[-1], item.command_id)
    assert item.path[-1].lower().startswith("compressed")


def test_rip_command_is_flagged_as_opening_a_dialog():
    """Posting the command is necessary but not sufficient."""
    assert eacdrive.find_rip_command(_menu()).opens_dialog


def test_find_command_matches_on_the_whole_path():
    assert eacdrive.find_command(_menu(), "detect gaps").command_id == 539
    assert eacdrive.find_command(_menu(), "action", "test selected").command_id == 532
    assert eacdrive.find_command(_menu(), "no such entry") is None


def test_menu_item_renders_readably():
    item = eacdrive.find_rip_command(_menu())
    assert "771" in str(item) and "dialog" in str(item)
