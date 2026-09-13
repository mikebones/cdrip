"""Reading EAC's saved option profile, so settings can be checked without the GUI.

EAC keeps no readable configuration anywhere until you save a profile - there
is no INI, and nothing under ``HKCU\\Software`` - so a freshly configured EAC
has its settings only in memory, and they are lost when it exits. That is the
first reason to save one (``EAC / Profiles / Save Profile...``). The second is
that the saved file is the only place the settings can be *verified* from
without driving twenty tab pages through Win32.

The file is a binary blob beginning ``EACV1300`` with UTF-16LE strings embedded
in it. The numeric settings are not decoded here - their offsets are not
documented and guessing at them would be worse than not checking - but the
strings cover the settings that actually decide whether a release is
compliant: the filename scheme, the encoder and its command line, and the
output directory.

What this catches, on a default-ish EAC:

* ``%tracknr2% %title% - %artist%`` as the naming scheme. It is EAC's stock
  value and it is wrong for a single-artist album: RED 2.3.13 wants
  ``01 - TrackName.flac``, and the stock scheme both drops the separator and
  appends the artist to every filename.
* ``-6`` on the FLAC command line. FLAC's default, but a ``-6`` file shrinks
  by more than the 1% :mod:`cdrip.compliance` allows when recompressed at
  ``-8``, which is reportable under 2.2.10.10.

Neither is detectable after the fact from the audio, and both require a re-rip
to fix once the log and cue exist - so they belong in a pre-rip check.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

MAGIC = b"E\x00A\x00C\x00V\x00"

# A run of printable ASCII encoded as UTF-16LE.
_STRING_RE = re.compile(rb"(?:[\x20-\x7e]\x00){3,}")

DEFAULT_PROFILE_DIR = os.path.join(
    os.environ.get("APPDATA", ""), "EAC", "Profiles"
)

# The scheme RED 2.3.13 asks for: "01 - TrackName".
WANTED_SCHEME = "%tracknr2% - %title%"
WANTED_FLAC_LEVEL = 8


@dataclass
class Profile:
    path: str
    version: str
    strings: list[str] = field(default_factory=list)

    @property
    def naming_schemes(self) -> list[str]:
        """The distinct filename templates stored in the profile.

        EAC keeps several - primary, secondary encoder, various-artists, one
        per encoder - and the file does not label which is active, so this
        cannot say "the naming scheme is X". It returns the set, deduplicated
        and in file order, and :func:`check` only asserts that the wanted one
        is among them.

        Two strings that also contain ``%tracknr``/``%title%`` are excluded
        because they are not filename templates at all: the encoder command
        line (it has ``%source%`` and ``%dest%``) and the catalog export
        format (semicolon-separated).
        """
        out: list[str] = []
        for s in self.strings:
            if "%tracknr" not in s or "%title%" not in s:
                continue
            if "%source%" in s or "%dest%" in s or ";" in s:
                continue
            if s not in out:
                out.append(s)
        return out

    @property
    def encoder(self) -> str | None:
        for s in self.strings:
            if s.lower().endswith(".exe") and os.path.sep in s:
                return s
        return None

    @property
    def encoder_args(self) -> str | None:
        for s in self.strings:
            if "%source%" in s and "%dest%" in s:
                return s
        return None

    @property
    def flac_level(self) -> int | None:
        args = self.encoder_args or ""
        match = re.match(r"\s*-(\d)\b", args)
        return int(match.group(1)) if match else None


def read(path: str) -> Profile:
    """Parse a saved ``.cfg`` option profile."""
    with open(path, "rb") as fh:
        blob = fh.read()
    if not blob.startswith(MAGIC):
        raise ValueError(
            "%s does not look like an EAC profile (expected a %r header)"
            % (path, MAGIC.decode("utf-16-le"))
        )
    strings = [m.decode("utf-16-le") for m in _STRING_RE.findall(blob)]
    version = strings[0] if strings else ""
    return Profile(path=path, version=version, strings=strings)


def find_profiles(directory: str | None = None) -> list[str]:
    directory = directory or DEFAULT_PROFILE_DIR
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, n)
        for n in os.listdir(directory)
        if n.lower().endswith(".cfg")
    )


def check(profile: Profile) -> list[str]:
    """Problems that would make the resulting release non-compliant.

    Returns plain strings rather than :class:`cdrip.compliance.Finding`s
    because these are settings, not properties of a release that exists yet.
    """
    problems: list[str] = []

    schemes = profile.naming_schemes
    if not schemes:
        problems.append("no filename scheme found in the profile")
    elif WANTED_SCHEME not in schemes:
        # Only the absence of the right scheme is reportable. Flagging the
        # other stored schemes would be noise: EAC keeps one per encoder and
        # a separate various-artists one, and the file does not say which is
        # in use, so a warning about them would fire on every healthy profile.
        problems.append(
            "no scheme is %r - RED 2.3.13 wants '01 - TrackName'. Stored "
            "schemes: %s" % (WANTED_SCHEME, ", ".join(repr(s) for s in schemes))
        )

    level = profile.flac_level
    if level is None:
        problems.append("no compression level on the encoder command line")
    elif level < WANTED_FLAC_LEVEL:
        problems.append(
            "FLAC compression is -%d; -%d is expected, and a -%d file shrinks "
            "enough on recompression to be reportable under RED 2.2.10.10"
            % (level, WANTED_FLAC_LEVEL, level)
        )

    args = profile.encoder_args or ""
    if args and "-V" not in args.split():
        problems.append(
            "FLAC is not called with -V, so the encoder never verifies its "
            "own output against the input"
        )

    encoder = profile.encoder
    if encoder and not os.path.isfile(encoder):
        problems.append("encoder %r does not exist" % encoder)

    return problems


def describe(profile: Profile) -> str:
    """A short human summary of what the profile will produce."""
    lines = [
        "profile : %s" % profile.path,
        "version : %s" % profile.version,
        "encoder : %s" % (profile.encoder or "(none)"),
        "level   : %s" % (("-%d" % profile.flac_level)
                          if profile.flac_level is not None else "(none)"),
    ]
    for s in profile.naming_schemes:
        lines.append("scheme  : %s" % s)
    return "\n".join(lines)
