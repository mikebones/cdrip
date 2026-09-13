"""Making EAC's cue sheet point at the files that actually exist.

A cue is worth having: RED 2.2.10.7 says a 100% log rip lacking one can be
trumped by a rip carrying even a *noncompliant* cue. It is also cheap, because
a cue is built from the disc's TOC and gap information rather than from the
extracted audio - so a missing cue never requires re-ripping. Detect gaps,
write the cue, done.

The catch is the ``FILE`` lines. EAC extracts to WAV and compresses afterwards,
deleting the WAV, but its "Multiple WAV Files" cue variants name the WAVs it
extracted. The result references ``01 - Title.wav`` when the directory holds
``01 - Title.flac``, and a cue pointing at files that do not exist is worse
than no cue at all - it looks complete and fails the moment anyone uses it.

So the FILE lines are retargeted onto the real files, matched by basename so a
renamed extension is all that changes. Nothing else in the cue is touched: the
INDEX values are the disc's gap information and rewriting those would be
fabricating data.
"""

from __future__ import annotations

import os
import re

FILE_RE = re.compile(r'^(\s*FILE\s+")(.+?)("\s+\S+\s*)$', re.M | re.I)

AUDIO_EXTENSIONS = (".flac", ".wav", ".ape", ".wv", ".m4a", ".mp3")

# The cue's FILE type keyword. EAC writes WAVE for its WAV output; FLAC files
# are still declared WAVE by every cue-consuming tool that matters, so the
# keyword is left alone and only the name is corrected.


def _decode(path: str) -> tuple[str, str]:
    """Read a cue, returning its text and the encoding it was written in.

    EAC writes cue sheets in the system codepage, not UTF-8 - on a Western
    install that is cp1252, where ``0x85`` is U+2026. ``latin-1`` decodes the
    same byte as a C1 control character, so falling straight back to it turns
    "Waste… We" into "Waste\\x85 We" and silently corrupts the title on
    rewrite. cp1252 is tried first for that reason; latin-1 remains the last
    resort because it cannot fail.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def referenced_files(path: str) -> list[str]:
    text, _ = _decode(path)
    return [m.group(2) for m in FILE_RE.finditer(text)]


def retarget(cue_path: str, directory: str) -> str:
    """Point every FILE line at a file that exists, and say what happened.

    Matching is by basename without extension, which is exactly the change
    compression makes. A FILE whose target cannot be found is left alone and
    reported rather than guessed at.
    """
    text, encoding = _decode(cue_path)

    present: dict[str, str] = {}
    for name in sorted(os.listdir(directory)):
        stem, ext = os.path.splitext(name)
        if ext.lower() in AUDIO_EXTENSIONS:
            present.setdefault(stem, name)

    changed = 0
    missing: list[str] = []

    def fix(match: re.Match) -> str:
        nonlocal changed
        head, name, tail = match.group(1), match.group(2), match.group(3)
        # EAC may write a full path; the cue should be relative to itself.
        base = os.path.basename(name.replace("\\", "/"))
        if os.path.isfile(os.path.join(directory, base)):
            return "%s%s%s" % (head, base, tail) if base != name else match.group(0)
        stem = os.path.splitext(base)[0]
        replacement = present.get(stem)
        if replacement is None:
            missing.append(base)
            return match.group(0)
        changed += 1
        return "%s%s%s" % (head, replacement, tail)

    fixed = FILE_RE.sub(fix, text)
    if fixed != text:
        with open(cue_path, "w", encoding=encoding, newline="\r\n") as fh:
            fh.write(fixed)

    total = len(FILE_RE.findall(text))
    if missing:
        return ("cue references %d file(s) that do not exist and could not be "
                "matched: %s" % (len(missing), ", ".join(missing[:3])))
    if changed:
        return ("cue written and retargeted: %d of %d FILE lines pointed at "
                "the extracted WAVs, now pointing at the compressed files"
                % (changed, total))
    return "cue written; all %d FILE lines already resolve" % total


INDEX_RE = re.compile(r"^\s*INDEX\s+(\d\d)\s+(\d\d:\d\d:\d\d)\s*$", re.M | re.I)

# A pregap EAC emits when gap detection did not actually run. Real pregaps
# vary track to track; an identical one-second value on several tracks is the
# placeholder, not a measurement.
PLACEHOLDER_PREGAP = "00:01:00"


def suspicious_gaps(cue_path: str) -> list[str]:
    """Gap data that looks like a placeholder rather than a measurement.

    EAC's gap detection can fail outright - on a mixed-mode disc it has been
    seen to die with "Gaps.2154 -> INDEX-RANGE" - and a cue written afterwards
    still contains INDEX lines. They are uniform filler. Shipping them states
    something about the disc that was never measured, which is worse than
    shipping no cue at all: 2.2.10.7 makes a missing cue trumpable, while a
    wrong one is simply wrong.
    """
    text, _ = _decode(cue_path)

    # A pregap is expressed as INDEX 00 (where it starts) followed by INDEX 01
    # (where the track proper starts), so its LENGTH is the INDEX 01 value -
    # INDEX 00 is 00:00:00 on every track that has one. Reading INDEX 00 as
    # the pregap finds nothing, ever.
    pregaps: list[str] = []
    pending = False
    for number, value in INDEX_RE.findall(text):
        if number == "00":
            pending = True
        elif number == "01" and pending:
            pregaps.append(value)
            pending = False

    if len(pregaps) < 2:
        return []
    if len(set(pregaps)) == 1 and pregaps[0] == PLACEHOLDER_PREGAP:
        return [
            "every one of the %d pregaps is exactly %s. Real pregaps vary; "
            "this is what EAC writes when gap detection did not run or "
            "crashed, so the cue asserts something about the disc that was "
            "never measured." % (len(pregaps), PLACEHOLDER_PREGAP)
        ]
    return []


def check(cue_path: str, directory: str | None = None) -> list[str]:
    """Problems with a cue that would matter to someone using it."""
    problems: list[str] = []
    if not os.path.isfile(cue_path):
        return ["cue sheet %s does not exist" % cue_path]
    directory = directory or os.path.dirname(os.path.abspath(cue_path))
    problems.extend(suspicious_gaps(cue_path))

    files = referenced_files(cue_path)
    if not files:
        problems.append("cue has no FILE lines")
    for name in files:
        base = os.path.basename(name.replace("\\", "/"))
        if not os.path.isfile(os.path.join(directory, base)):
            problems.append("cue references a missing file: %s" % base)
        if base != name:
            problems.append(
                "cue uses a path rather than a bare filename (%r); it should "
                "be relative to the cue itself" % name)
    return problems
