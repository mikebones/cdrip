"""Tag defects an EAC rip carries that nothing else notices.

These are not compliance rules a tracker states anywhere; they are wrong facts
that EAC writes into every file and that survive straight into an upload unless
something removes them. Both were fixed by hand on the first release, which is
the usual sign they should not be done by hand.

``TOTALTRACKS`` is the one that recurs. EAC counts the disc's tracks, and on a
mixed-mode disc that includes the trailing data track - so a six-track release
ships claiming seven. It is wrong on every mixed-mode CD, it is invisible in
the audio, and nobody reads the tag closely enough to catch it.

``ARTIST`` carrying a stale per-track value is the other. EAC keeps per-track
artists across discs; "Unknown Artist" on every track is easy to miss, because
EAC's own export *hides* a track artist that matches the album artist and shows
it only once the album artist is correct.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from . import flactools

FLAC_EXT = ".flac"


@dataclass(frozen=True)
class TagFix:
    field: str
    was: str
    now: str
    why: str

    def __str__(self) -> str:
        return "%s: %r -> %r (%s)" % (self.field, self.was, self.now, self.why)


def read_tags(path: str) -> dict[str, list[str]]:
    """Every Vorbis comment on a file, as ``FIELD -> [values]``."""
    out = subprocess.run(
        [flactools.require("metaflac"), "--export-tags-to=-", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tags: dict[str, list[str]] = {}
    for line in (out.stdout or "").splitlines():
        if "=" not in line:
            continue
        field, _, value = line.partition("=")
        tags.setdefault(field.strip().upper(), []).append(value)
    return tags


def audio_files(directory: str) -> list[str]:
    return sorted(
        os.path.join(directory, n) for n in os.listdir(directory)
        if n.lower().endswith(FLAC_EXT)
    )


def check(directory: str, expect_tracks: int | None = None) -> list[TagFix]:
    """Tag values that are wrong, with what they should be. Writes nothing."""
    files = audio_files(directory)
    if not files:
        return []
    actual = expect_tracks if expect_tracks is not None else len(files)
    fixes: list[TagFix] = []

    first = read_tags(files[0])

    for value in first.get("TOTALTRACKS", []):
        if value.strip() != str(actual):
            fixes.append(TagFix(
                "TOTALTRACKS", value, str(actual),
                "EAC counts every track on the disc, so a mixed-mode CD's "
                "trailing data track is included - the release has %d audio "
                "tracks" % actual,
            ))

    album_artist = (first.get("ALBUMARTIST") or first.get("ARTIST") or [""])[0]
    for path in files:
        tags = read_tags(path)
        for value in tags.get("ARTIST", []):
            if value.strip().lower() in ("unknown artist", "unknown", ""):
                fixes.append(TagFix(
                    "ARTIST", value, album_artist,
                    "placeholder left over in %s; EAC's export hides a track "
                    "artist that matches the album artist, so this stays "
                    "invisible until the album artist is right"
                    % os.path.basename(path),
                ))
    return fixes


def apply(directory: str, fixes: list[TagFix]) -> list[str]:
    """Write the fixes onto every file. Never renames anything."""
    if not fixes:
        return []
    metaflac = flactools.require("metaflac")
    wanted = {fix.field: fix.now for fix in fixes}
    touched: list[str] = []
    for path in audio_files(directory):
        cmd = [metaflac]
        for field, value in wanted.items():
            cmd += ["--remove-tag=%s" % field, "--remove-tag=%s" % field.lower()]
        for field, value in wanted.items():
            cmd.append("--set-tag=%s=%s" % (field, value))
        cmd.append(path)
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError("metaflac failed on %s: %s"
                               % (os.path.basename(path), proc.stderr.strip()))
        touched.append(os.path.basename(path))
    return touched


# --- tags a tracker upload needs, which the disc cannot supply ---------------

REQUIRED = ("ARTIST", "TITLE", "ALBUM", "TRACKNUMBER", "DATE")
# Not required by the audio, but an upload without them lands with no edition
# information and no tags - which is what produced a torrent showing "unknown"
# release type and "unknown" tags.
WANTED_FOR_UPLOAD = ("GENRE", "LABEL", "CATALOGNUMBER", "ALBUMARTIST")

# ALBUMARTIST is in that list for honest but narrower reasons than the other
# three. salmon does NOT need it: construct_artists_li builds the group's
# artists from each track's ARTIST tag, so a release with no ALBUMARTIST
# uploads correctly - checked in salmon's own pre_data.py, not assumed. It
# earns its place because the library copy is what Plex and Lidarr group by,
# because one rip carrying it and the next not is the kind of inconsistency
# nobody notices until a library view splits an album in two, and because it
# was filled in by hand on a real rip - which is this module's whole premise.


def missing(directory: str) -> list[str]:
    """Report tags an upload needs that are absent.

    Every file is read, not just the first. A tag written to track 1 and
    missing from track 5 is the shape that survives a spot check, and EAC
    fills tags per track, so partial coverage is a real state rather than a
    hypothetical one.
    """
    files = audio_files(directory)
    if not files:
        return ["no FLAC files in %s" % directory]

    absent: dict[str, list[str]] = {}
    for path in files:
        tags = read_tags(path)
        for field in REQUIRED + WANTED_FOR_UPLOAD:
            if not tags.get(field):
                absent.setdefault(field, []).append(os.path.basename(path))

    def where(field: str) -> str:
        names = absent[field]
        if len(names) == len(files):
            return ""
        return " (on %d of %d files: %s)" % (
            len(names), len(files),
            ", ".join(names[:3]) + (", ..." if len(names) > 3 else ""))

    out = []
    for field in REQUIRED:
        if field in absent:
            out.append("%s is missing%s - required on every music upload "
                       "(2.3.16.1)" % (field, where(field)))
    for field in WANTED_FOR_UPLOAD:
        if field in absent:
            out.append("%s is missing%s - without it the upload carries no %s, "
                       "which is how a release lands with 'unknown' tags and "
                       "no edition information (2.1.22)"
                       % (field, where(field),
                          "genre" if field == "GENRE" else field.lower()))
    return out
