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
    """Read a cue, returning its text and the encoding it was written in."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
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


def check(cue_path: str, directory: str | None = None) -> list[str]:
    """Problems with a cue that would matter to someone using it."""
    problems: list[str] = []
    if not os.path.isfile(cue_path):
        return ["cue sheet %s does not exist" % cue_path]
    directory = directory or os.path.dirname(os.path.abspath(cue_path))

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
