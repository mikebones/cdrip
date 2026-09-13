"""Write MusicBrainz metadata onto a rip without renaming anything.

This exists because of an ordering problem.  whipper names and tags files from
MusicBrainz *during* the rip, and writes a .cue and .log that reference those
filenames.  If the disc ID was unknown at rip time the files come out as bare
track numbers, and the obvious fix - rename them afterwards - silently
invalidates the .cue and the .log, which a tracker's log checker will then
reject.

So: tag in place, never rename.  If you want pretty filenames, attach the disc
ID to MusicBrainz first and re-rip; whipper will then get it right at source
and the .cue and .log will agree with the filenames.
"""

from __future__ import annotations

import os
import subprocess

from . import flactools

# U+2010 HYPHEN and U+2011 NON-BREAKING HYPHEN are visually identical to ASCII
# "-" but break search, sorting and filename round-tripping.  MusicBrainz uses
# them in titles; fold them.  The ellipsis is left alone - it is a real
# typographic choice, not a lookalike.
_LOOKALIKES = {"‐": "-", "‑": "-"}


def normalise(text: str) -> str:
    for bad, good in _LOOKALIKES.items():
        text = text.replace(bad, good)
    return text


def tags_for_release(release: dict) -> dict[int, dict[str, str]]:
    """Build per-track Vorbis comments from a MusicBrainz release document."""
    album = normalise(release.get("title", ""))
    credits = release.get("artist-credit") or []
    artist = normalise(", ".join(a["artist"]["name"] for a in credits))
    artist_id = credits[0]["artist"]["id"] if credits else ""
    date = release.get("date", "") or ""
    year = date[:4]

    medium = (release.get("media") or [{}])[0]
    tracks = medium.get("tracks") or []
    total = len(tracks)

    out: dict[int, dict[str, str]] = {}
    for track in tracks:
        number = int(track["position"])
        out[number] = {
            "ARTIST": artist,
            "ALBUMARTIST": artist,
            "ALBUM": album,
            "TITLE": normalise(track["title"]),
            "DATE": year,
            "ORIGINALDATE": date,
            "TRACKNUMBER": "%02d" % number,
            "TRACKTOTAL": str(total),
            "TOTALTRACKS": str(total),
            "DISCNUMBER": "1",
            "DISCTOTAL": "1",
            "MEDIA": "CD",
            "MUSICBRAINZ_ALBUMID": release.get("id", ""),
            "MUSICBRAINZ_ALBUMARTISTID": artist_id,
            "MUSICBRAINZ_ARTISTID": artist_id,
            "MUSICBRAINZ_TRACKID": track["recording"]["id"],
        }
    return out


def apply(directory: str, release: dict) -> list[str]:
    """Tag ``NN.flac`` files in ``directory`` from ``release``.

    Returns the list of files tagged.  Files are matched by track number, so a
    missing track is reported rather than silently shifting every tag by one.
    """
    wanted = tags_for_release(release)
    tagged: list[str] = []
    for number, tags in sorted(wanted.items()):
        path = os.path.join(directory, "%02d.flac" % number)
        if not os.path.exists(path):
            raise FileNotFoundError(
                "expected %s for track %d; refusing to tag a partial rip"
                % (path, number)
            )
        cmd = [flactools.require("metaflac"), "--remove-all-tags"]
        cmd += ["--set-tag=%s=%s" % (k, v) for k, v in tags.items() if v]
        cmd.append(path)
        proc = subprocess.run(cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError("metaflac failed on %s: %s" % (path, proc.stderr))
        tagged.append(path)
    return tagged


def read_back(directory: str) -> dict[str, dict[str, str]]:
    """Read the tags actually present, for verification/printing."""
    out: dict[str, dict[str, str]] = {}
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".flac"):
            continue
        path = os.path.join(directory, name)
        proc = subprocess.run(
            [flactools.require("metaflac"), "--export-tags-to=-", path],
            capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        )
        tags: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                tags[key] = value
        out[name] = tags
    return out
