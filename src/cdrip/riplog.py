"""Rules that can only be answered by the rip log.

Everything else about a finished release is checkable from the files, which is
why it lives in smoked-salmon where the Bandcamp approver can reach it too.
These two cannot: nothing survives into the FLACs to say what kind of disc was
in the drive, or whether the disc carried pre-emphasis. whipper writes both
into its log and then nobody reads them.

Both matter enough to be worth reading:

* **2.2.10.1** - a rip has to come from a commercially pressed CD, not a CD-R
  copy of one. whipper prints ``CD-R detected: true/false`` and that is the
  only place the answer exists. Uploading a CD-R rip as a CD rip is a
  misrepresentation of source, not a formatting nit.
* **2.1.21** - pre-emphasis is allowed in lossless and deleted in lossy, and
  a pre-emphasised lossless rip belongs in its own edition rather than
  competing with the de-emphasised one. whipper records it per track.

Logs are read, never written. Editing a rip log is forbidden (2.2.10.9) and
would invalidate the CRCs it carries.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

_CDR_RE = re.compile(r"^\s*CD-R detected:\s*(\S+)\s*$", re.M | re.I)
_TRACK_RE = re.compile(r"^\s{2}(\d+):\s*$", re.M)
_PREEMPH_RE = re.compile(r"^\s*Pre-emphasis:\s*(.*?)\s*$", re.M | re.I)
_CACHE_RE = re.compile(r"^\s*Defeat audio cache:\s*(\S+)\s*$", re.M | re.I)
_OFFSET_RE = re.compile(r"^\s*Read offset correction:\s*(-?\d+)\s*$", re.M | re.I)


@dataclass(frozen=True)
class LogFacts:
    path: str
    cdr_detected: bool | None
    cache_defeated: bool | None
    read_offset: int | None
    preemphasised_tracks: tuple[int, ...]

    @property
    def has_preemphasis(self) -> bool:
        return bool(self.preemphasised_tracks)


def _boolish(text: str) -> bool | None:
    text = text.strip().lower()
    if text in ("true", "yes"):
        return True
    if text in ("false", "no"):
        return False
    return None


def read_log(path: str) -> LogFacts:
    """Pull the facts only the log knows out of a whipper log."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    cdr = _CDR_RE.search(text)
    cache = _CACHE_RE.search(text)
    offset = _OFFSET_RE.search(text)

    # Pre-emphasis is printed per track, under a "  N:" heading. Walk the
    # track sections so a value is attributed to the right track rather than
    # to the file as a whole.
    preemph: list[int] = []
    marks = list(_TRACK_RE.finditer(text))
    for i, mark in enumerate(marks):
        number = int(mark.group(1))
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        section = text[mark.end():end]
        found = _PREEMPH_RE.search(section)
        if found and _boolish(found.group(1)) is True:
            preemph.append(number)

    return LogFacts(
        path=path,
        cdr_detected=_boolish(cdr.group(1)) if cdr else None,
        cache_defeated=_boolish(cache.group(1)) if cache else None,
        read_offset=int(offset.group(1)) if offset else None,
        preemphasised_tracks=tuple(preemph),
    )


def find_log(directory: str) -> str | None:
    for name in sorted(os.listdir(directory)):
        if name.lower().endswith(".log"):
            return os.path.join(directory, name)
    return None


def check_log(directory: str) -> list[tuple[str, str, str]]:
    """Return ``(rule, severity, message)`` for what the log reveals."""
    log = find_log(directory)
    if not log:
        return [("2.2.10.2", "trumpable",
                 "no rip log in the release - a FLAC CD rip without a log from "
                 "an approved ripper is trumpable by one that has it")]

    facts = read_log(log)
    out: list[tuple[str, str, str]] = []

    if facts.cdr_detected is True:
        out.append((
            "2.2.10.1", "blocker",
            "the log says CD-R detected: rips must come from commercially "
            "pressed or official CDs, not CD-R copies of them.",
        ))
    elif facts.cdr_detected is None:
        out.append((
            "2.2.10.1", "info",
            "the log does not record whether the disc was a CD-R, so this "
            "could not be verified.",
        ))

    if facts.has_preemphasis:
        out.append((
            "2.1.21", "info",
            "pre-emphasis on track(s) %s. Allowed in lossless, but it belongs "
            "in its own edition rather than competing with a de-emphasised "
            "rip - and it must never be uploaded in a lossy format."
            % ", ".join(str(n) for n in facts.preemphasised_tracks),
        ))

    if facts.cache_defeated is False:
        out.append((
            "2.2.10.3", "info",
            "the drive's audio cache was not defeated, so re-reads were not "
            "independent and the log's own verification is weaker than it looks.",
        ))

    return out
