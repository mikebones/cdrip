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
