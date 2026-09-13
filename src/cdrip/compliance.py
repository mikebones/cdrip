"""Mechanically checkable tracker formatting rules.

Rule numbers below are RED's, but nothing here is RED-specific in substance -
they are the ordinary hygiene rules most Gazelle trackers share.

The important structural point is *when* each rule can be fixed:

* A filename problem must be caught **before the rip**. whipper builds
  filenames from MusicBrainz and writes a .cue and .log that reference them,
  and editing a rip log is forbidden (2.2.10.9), so renaming afterwards
  silently invalidates both. The only real fix is to correct MusicBrainz and
  rip again - which is cheap if you find out in the first ten seconds and
  expensive if you find out after fifteen minutes of ripping.
* A *tag* or *compression* problem can be fixed in place afterwards, because
  neither changes filenames and neither changes the decoded audio the log's
  CRCs are computed from.

So ``check_metadata`` runs against MusicBrainz titles before ripping, and
``check_release`` runs against the finished folder.
"""

from __future__ import annotations

import os
import subprocess
import unicodedata
from dataclasses import dataclass

from . import flactools

# 2.3.12 - path length, counted from the release folder down.
MAX_PATH_LEN = 180

# 2.3.19 - combined embedded images and padding per file.
MAX_EMBEDDED_KIB = 1024

# 2.2.10.10 - FLAC must be compressed; level 8 is the recommendation. A file
# that shrinks by more than this when recompressed was encoded at a lower
# level and is reportable.
RECOMPRESS_THRESHOLD_PCT = 1.0

SEVERITY_BLOCKER = "blocker"      # would be deleted
SEVERITY_TRUMPABLE = "trumpable"  # would stand, but someone can replace it
SEVERITY_INFO = "info"


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    message: str
    # Whether this can be fixed without renaming anything - i.e. without
    # invalidating the .cue and .log.
    fixable_in_place: bool = False

    def __str__(self) -> str:
        mark = {"blocker": "BLOCK", "trumpable": "TRUMP", "info": "info "}[self.severity]
        fix = " [fixable in place]" if self.fixable_in_place else ""
        return "%s %-10s %s%s" % (mark, self.rule, self.message, fix)


# Characters that look like ASCII but are not. 2.3.11.1 calls these out
# explicitly ("characters that mean one letter in one language and a different
# letter in another"), and they break search, sorting and filename
# round-tripping. Mapped to what they should almost always be.
LOOKALIKES: dict[str, str] = {
    "‐": "-",   # HYPHEN (vs ASCII hyphen-minus)
    "‑": "-",   # NON-BREAKING HYPHEN
    "‒": "-",   # FIGURE DASH
    " ": " ",   # NO-BREAK SPACE
    "⁄": "/",   # FRACTION SLASH
    "а": "a",   # CYRILLIC SMALL A
    "е": "e",   # CYRILLIC SMALL IE
    "о": "o",   # CYRILLIC SMALL O
    "р": "p",   # CYRILLIC SMALL ER
    "с": "c",   # CYRILLIC SMALL ES
    "х": "x",   # CYRILLIC SMALL HA
    "Α": "A",   # GREEK CAPITAL ALPHA
    "Ο": "O",   # GREEK CAPITAL OMICRON
}

# Non-ASCII that is legitimate typography and must NOT be "corrected".
# Rewriting these would be the kind of pointless trump 2.3.18 rejects.
INTENTIONAL = set("…’“”–—éèüöäñåø")


def find_lookalikes(text: str) -> list[tuple[str, str, str]]:
    """Return ``(char, codepoint, suggested replacement)`` for lookalikes."""
    out = []
    for ch in dict.fromkeys(text):
        if ch in LOOKALIKES:
            out.append((ch, "U+%04X" % ord(ch), LOOKALIKES[ch]))
    return out


def normalise_lookalikes(text: str) -> str:
    for bad, good in LOOKALIKES.items():
        text = text.replace(bad, good)
    return text


def check_metadata(titles: dict[int, str], album: str = "", artist: str = "") -> list[Finding]:
    """Pre-rip check against the metadata whipper is about to use.

    Catching this here is the whole point: once whipper has written the .cue
    and .log, the filenames are load-bearing and the only clean fix is to
    correct MusicBrainz and rip again.
    """
    findings: list[Finding] = []
    for label, text in [("album", album), ("artist", artist)] + [
        ("track %d" % n, t) for n, t in sorted(titles.items())
    ]:
        if not text:
            continue
        for ch, cp, suggestion in find_lookalikes(text):
            findings.append(Finding(
                "2.3.11.1", SEVERITY_TRUMPABLE,
                "%s contains %s %r (looks like %r): %r. Fix it in MusicBrainz "
                "and re-rip - renaming after the rip invalidates the .cue and "
                ".log." % (label, cp, ch, suggestion, text),
            ))
        if text != text.strip():
            findings.append(Finding(
                "2.3.20", SEVERITY_TRUMPABLE,
                "%s has leading or trailing whitespace: %r" % (label, text),
            ))
        if text.isupper() and len(text) > 3:
            findings.append(Finding(
                "2.3.18.2", SEVERITY_INFO,
                "%s is ALL CAPS: %r - trumpable unless the stylisation is "
                "intentional and provable." % (label, text),
            ))
    return findings


def _flac_tags(path: str) -> dict[str, str]:
    proc = subprocess.run(
        [flactools.require("metaflac"), "--export-tags-to=-", path], capture_output=True, text=True,
        encoding="utf-8", errors="replace"
    )
    tags = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            tags[k.upper()] = v
    return tags


def _has_id3(path: str) -> bool:
    """2.2.10.8 - an ID3 header on a FLAC stops some players dead."""
    try:
        with open(path, "rb") as fh:
            return fh.read(3) == b"ID3"
    except OSError:
        return False


def _embedded_bytes(path: str) -> int:
    """Size of embedded pictures plus padding (2.3.19)."""
    proc = subprocess.run(
        [flactools.require("metaflac"), "--list", "--block-type=PICTURE,PADDING", path],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    total = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("length:"):
            try:
                total += int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return total


def _recompress_gain_pct(path: str) -> float | None:
    """How much smaller the file gets at -8, as a percentage (2.2.10.10)."""
    import tempfile

    try:
        original = os.path.getsize(path)
    except OSError:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "test.flac")
        proc = subprocess.run(
            [flactools.require("flac"), "-s", "-8", "-f", "-o", out, path],
            capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0 or not os.path.exists(out):
            return None
        return (original - os.path.getsize(out)) * 100.0 / original


def check_release(path: str, expect_tracks: int | None = None) -> list[Finding]:
    """Post-rip check of a finished release folder."""
    findings: list[Finding] = []
    folder = os.path.basename(path.rstrip("/\\"))
    entries = sorted(os.listdir(path))
    flacs = [f for f in entries if f.lower().endswith(".flac")]

    # 2.3.1 / 2.1.5.1 - a release is a folder of separate tracks.
    if not flacs:
        findings.append(Finding("2.3.1", SEVERITY_BLOCKER, "no audio files in %s" % folder))
        return findings
    if len(flacs) == 1 and (expect_tracks or 0) > 1:
        findings.append(Finding(
            "2.1.5.1", SEVERITY_BLOCKER,
            "only one audio file for a %d-track disc - unsplit rips without a "
            "cue sheet are deleted outright." % expect_tracks,
        ))
    if expect_tracks and len(flacs) != expect_tracks:
        findings.append(Finding(
            "2.1.19", SEVERITY_BLOCKER,
            "%d audio files but the disc has %d audio tracks - a release may "
            "not be missing tracks." % (len(flacs), expect_tracks),
        ))

    # 2.3.3 - no unnecessary nesting for a single-disc release.
    subdirs = [e for e in entries if os.path.isdir(os.path.join(path, e))]
    if subdirs:
        findings.append(Finding(
            "2.3.3", SEVERITY_TRUMPABLE,
            "unnecessary nested folder(s) for a single-disc release: %s"
            % ", ".join(subdirs),
        ))

    # 2.1.19.3 - enhanced CDs must be uploaded without the data track's content.
    strays = [
        e for e in entries
        if os.path.splitext(e)[1].lower() in
        (".mp3", ".m4a", ".mov", ".avi", ".mp4", ".wmv", ".exe", ".iso")
    ]
    if strays:
        findings.append(Finding(
            "2.1.19.3", SEVERITY_BLOCKER,
            "non-audio or lossy files from the disc's data track are present: "
            "%s - enhanced CDs must be uploaded without them." % ", ".join(strays),
        ))

    for name in entries:
        full = os.path.join(path, name)

        # 2.3.20 - leading spaces break interoperability.
        if name != name.lstrip():
            findings.append(Finding(
                "2.3.20", SEVERITY_TRUMPABLE, "leading space in %r" % name))

        # 2.3.12 - path length from the release folder down.
        length = len(folder) + 1 + len(name)
        if length > MAX_PATH_LEN:
            findings.append(Finding(
                "2.3.12", SEVERITY_TRUMPABLE,
                "path is %d characters (limit %d): %s/%s"
                % (length, MAX_PATH_LEN, folder, name),
            ))

        # 2.3.11.1 - lookalike characters.
        for ch, cp, suggestion in find_lookalikes(name):
            findings.append(Finding(
                "2.3.11.1", SEVERITY_TRUMPABLE,
                "filename contains %s %r (looks like %r): %s"
                % (cp, ch, suggestion, name),
            ))

        if not name.lower().endswith(".flac"):
            continue

        # 2.3.13 - track numbers in filenames.
        if not name[:2].isdigit():
            findings.append(Finding(
                "2.3.13", SEVERITY_TRUMPABLE,
                "filename does not start with a track number: %s" % name))

        # 2.2.10.8 - no ID3 headers on FLAC.
        if _has_id3(full):
            findings.append(Finding(
                "2.2.10.8", SEVERITY_TRUMPABLE,
                "%s carries an ID3 header; FLAC must use Vorbis comments" % name,
                fixable_in_place=True,
            ))

        # 2.3.16.4 - required tags.
        tags = _flac_tags(full)
        missing = [t for t in ("ARTIST", "ALBUM", "TITLE", "TRACKNUMBER") if not tags.get(t)]
        if missing:
            findings.append(Finding(
                "2.3.16.4", SEVERITY_TRUMPABLE,
                "%s is missing required tag(s): %s" % (name, ", ".join(missing)),
                fixable_in_place=True,
            ))

        # 2.3.19 - embedded artwork and padding.
        kib = _embedded_bytes(full) / 1024.0
        if kib > MAX_EMBEDDED_KIB:
            findings.append(Finding(
                "2.3.19", SEVERITY_TRUMPABLE,
                "%s has %.0f KiB of embedded images/padding (limit %d)"
                % (name, kib, MAX_EMBEDDED_KIB),
                fixable_in_place=True,
            ))

    # 2.2.10.10 - compression level, sampled on the largest track.
    biggest = max(flacs, key=lambda f: os.path.getsize(os.path.join(path, f)))
    gain = _recompress_gain_pct(os.path.join(path, biggest))
    if gain is not None and gain > RECOMPRESS_THRESHOLD_PCT:
        findings.append(Finding(
            "2.2.10.10", SEVERITY_TRUMPABLE,
            "FLACs are not at maximum compression - %s shrinks %.1f%% at -8. "
            "Recompression is safe: it changes neither filenames nor the "
            "decoded audio the log's CRCs are computed from."
            % (biggest, gain),
            fixable_in_place=True,
        ))

    return findings


def blockers(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == SEVERITY_BLOCKER]


def summarise(findings: list[Finding]) -> str:
    if not findings:
        return "No formatting problems found."
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return ", ".join("%d %s" % (n, s) for s, n in sorted(counts.items()))
