"""Exact Audio Copy: log validation, and what it takes to drive EAC.

Why this module exists at all: some trackers only recognise logs from EAC or
XLD. RED's log checker identifies a log purely by its header - anything else is
"Unrecognized log file!", scored -1 and marked trumpable for "Bad/No
Checksum(s)" regardless of how good the rip was. A whipper rip scoring 100 in
cambia still fails there, so for those trackers the ripper has to be EAC.

Two useful things follow.

**Validate before uploading**, but not with ``CheckLog.exe``. EAC ships it, and
it looks like a console validator, but measured on EAC 1.8 it writes nothing to
stdout or stderr, creates no file, opens no window, and exits 0 for a correctly
signed log and an unsigned one alike. Treating its silence as "not an EAC log"
is wrong, and produced a gate here that rejected everything while appearing to
work. :func:`check_log` now says so rather than inferring a verdict from
silence.

What *can* be checked locally is the log's own contents - :func:`read_log` and
:func:`check_settings` - which covers the things that actually decide the
score: the ripper, the read offset, cache defeat, secure mode, C2, matching
Test and Copy CRCs, and whether a ``==== Log checksum ====`` line is present at
all. For the authoritative verdict, run cambia, the parser the tracker uses.

**Driving EAC is a Win32 problem, not a CLI one.** Measured, not assumed:

* ``EAC.exe`` accepts ``-DRIVE``, ``-OUTPUTDIRECTORY``, ``-TESTANDCOPY``,
  ``-CLOSE`` and friends, but they do not start a rip. Launched with all of
  them, EAC opens its window and idles: flat CPU and no output files after 45
  seconds. EAC's own documentation only ever describes the crash-workaround
  switches (``-nocdtext``, ``-notestunit``...), which matches.
* UI Automation can *see* EAC but not *drive* it. Its window (class ``erstes``)
  exposes 56 descendants, every one of them a bare ``Pane``, none supporting
  InvokePattern, and no MenuBar at all. pywinauto's UIA backend is therefore
  useless here.
* The menus are real ``HMENU``s, so the Win32 layer works where UIA does not.
  The command ids below were read out of a running EAC with GetSubMenu /
  GetMenuItemID, and a rip can be triggered with WM_COMMAND.

Note the "..." on the compressed entries: they open a dialog, so posting the
command is necessary but not sufficient for a fully unattended rip.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_EAC_DIRS = (
    r"C:\Program Files (x86)\Exact Audio Copy",
    r"C:\Program Files\Exact Audio Copy",
)

# Menu command ids, read from a running EAC 1.8. Verify against your build
# before relying on them; they are not a published interface.
MENU_COMMANDS = {
    "test_copy_selected_compressed": 771,    # Shift+F6 - the FLAC rip
    "test_copy_selected_uncompressed": 478,  # F6
    "test_copy_image_compressed": 664,
    "test_selected": 532,                    # F8
    "detect_gaps": 539,                       # F4
}

# CheckLog.exe's verdict strings, kept for the case where a build of it does
# behave like a console tool. On EAC 1.8 it never prints any of them - see the
# module docstring - so absence of these is not a verdict.
VERDICT_OK = "Log entry is fine!"
VERDICT_MODIFIED = "Log entry was modified, checksum incorrect!"
VERDICT_DAMAGED = "Log entry is damaged, can't recover!"
VERDICT_NO_CHECKSUM = "Log entry has no checksum!"


@dataclass(frozen=True)
class LogVerdict:
    path: str
    recognised: bool
    entries: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.recognised and bool(self.entries) and all(
            e == VERDICT_OK for e in self.entries
        )

    @property
    def inconclusive(self) -> bool:
        """CheckLog said nothing at all, so this proves nothing either way."""
        return not self.entries

    @property
    def summary(self) -> str:
        if self.inconclusive:
            return (
                "CheckLog.exe produced no verdict, which is NOT evidence the "
                "log is bad. Measured on EAC 1.8: it writes nothing to stdout "
                "or stderr, creates no file, and exits 0 for a correctly "
                "signed log and an unsigned one alike - so it cannot be used "
                "as a command-line validator. Judge the log from its own "
                "contents (see read_log/check_settings) and, for the "
                "authoritative answer, run cambia - the parser the tracker "
                "itself uses."
            )
        if self.ok:
            return "EAC log verified: %s" % ", ".join(self.entries)
        return "EAC log NOT clean: %s" % ", ".join(self.entries)


def find_checklog(extra: str | None = None) -> str | None:
    """Locate CheckLog.exe, EAC's bundled log checker."""
    candidates = ([extra] if extra else []) + [
        os.path.join(d, "CheckLog.exe") for d in DEFAULT_EAC_DIRS
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    found = shutil.which("CheckLog.exe")
    return found


def check_log(log_path: str, checklog: str | None = None) -> LogVerdict:
    """Run CheckLog.exe over a log and interpret whatever it says.

    On EAC 1.8 it says nothing at all, so the usual result is an
    ``inconclusive`` verdict. That is deliberately not treated as failure:
    reading silence as "bad log" is what made this gate reject every log,
    including correctly signed ones, while looking like it worked.
    """
    exe = find_checklog(checklog)
    if not exe:
        raise FileNotFoundError(
            "CheckLog.exe not found - it ships with EAC. Pass its path "
            "explicitly, or validate on a machine with EAC installed."
        )
    proc = subprocess.run([exe, log_path], capture_output=True, text=True)
    text = (proc.stdout or "") + (proc.stderr or "")
    entries = [
        line.strip().lstrip(". ").strip()
        for line in text.splitlines()
        if any(v in line for v in (VERDICT_OK, VERDICT_MODIFIED,
                                   VERDICT_DAMAGED, VERDICT_NO_CHECKSUM))
    ]
    return LogVerdict(path=log_path, recognised=bool(entries), entries=tuple(entries))


# --- reading an EAC log ------------------------------------------------------

# The ripper name is not always first on its line: EAC leads with "Exact Audio
# Copy V1.8 from ..." but whipper writes "Log created by: whipper 0.10.0".
# Anchoring to line start silently missed whipper, so the "this will not be
# recognised" warning never fired on exactly the logs that need it. Search the
# header region instead of the line start.
_RIPPER_RE = re.compile(r"(Exact Audio Copy|X Lossless Decoder|whipper)", re.I)
_OFFSET_RE = re.compile(r"Read offset correction\s*:\s*(-?\d+)", re.I)
_CACHE_RE = re.compile(r"Defeat audio cache\s*:\s*(\w+)", re.I)
_C2_RE = re.compile(r"Make use of C2 pointers\s*:\s*(\w+)", re.I)
_MODE_RE = re.compile(r"Read mode\s*:\s*(\w+)", re.I)
_ACCURATE_RE = re.compile(r"Utilize accurate stream\s*:\s*(\w+)", re.I)
_CHECKSUM_RE = re.compile(r"^==== Log checksum ([0-9A-Fa-f]{64}) ====", re.M)
_TESTCOPY_RE = re.compile(r"Test CRC\s+([0-9A-Fa-f]{8}).*?Copy CRC\s+([0-9A-Fa-f]{8})", re.S | re.I)


@dataclass(frozen=True)
class EacLogFacts:
    ripper: str | None
    read_offset: int | None
    cache_defeated: bool | None
    accurate_stream: bool | None
    c2_pointers: bool | None
    read_mode: str | None
    has_checksum: bool
    test_copy_pairs: tuple[tuple[str, str], ...]

    @property
    def crcs_match(self) -> bool:
        return bool(self.test_copy_pairs) and all(
            t.upper() == c.upper() for t, c in self.test_copy_pairs
        )


def _yesno(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in ("yes", "true")


def read_log(path: str) -> EacLogFacts:
    """Pull the settings an EAC log records about the rip that made it."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    # EAC writes UTF-16 by default; a BOM-less decode leaves NULs behind.
    if "\x00" in text:
        with open(path, encoding="utf-16", errors="replace") as fh:
            text = fh.read()

    ripper = _RIPPER_RE.search(text)
    offset = _OFFSET_RE.search(text)
    return EacLogFacts(
        ripper=ripper.group(1) if ripper else None,
        read_offset=int(offset.group(1)) if offset else None,
        cache_defeated=_yesno(_CACHE_RE.search(text).group(1) if _CACHE_RE.search(text) else None),
        accurate_stream=_yesno(_ACCURATE_RE.search(text).group(1) if _ACCURATE_RE.search(text) else None),
        c2_pointers=_yesno(_C2_RE.search(text).group(1) if _C2_RE.search(text) else None),
        read_mode=(_MODE_RE.search(text).group(1) if _MODE_RE.search(text) else None),
        has_checksum=bool(_CHECKSUM_RE.search(text)),
        test_copy_pairs=tuple(_TESTCOPY_RE.findall(text)),
    )


def check_settings(facts: EacLogFacts, expected_offset: int | None = None) -> list[str]:
    """Report anything in the log that would cost score or credibility."""
    problems: list[str] = []
    if facts.ripper and facts.ripper.lower().startswith("whipper"):
        problems.append(
            "ripped with whipper: trackers that identify logs by header will "
            "not recognise this log at all."
        )
    if not facts.has_checksum:
        problems.append(
            "log carries no checksum - it cannot be verified, and the torrent "
            "will be marked trumpable for Bad/No Checksum(s)."
        )
    if facts.read_mode and facts.read_mode.lower() != "secure":
        problems.append("read mode is %r, not Secure." % facts.read_mode)
    if facts.cache_defeated is False:
        problems.append(
            "audio cache was not defeated, so re-reads were not independent."
        )
    if expected_offset is not None and facts.read_offset is not None \
            and facts.read_offset != expected_offset:
        problems.append(
            "read offset is %+d but this drive's AccurateRip value is %+d - "
            "the rip is bit-shifted against everyone else's."
            % (facts.read_offset, expected_offset)
        )
    if facts.test_copy_pairs and not facts.crcs_match:
        problems.append("Test and Copy CRCs differ - the read is not reproducible.")
    return problems
