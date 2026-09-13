"""Disc table-of-contents reading and MusicBrainz disc ID computation.

The awkward case this module exists for is the *mixed-mode* CD: audio tracks
first, then a final data track carrying a CDFS session.  Windows Explorer (and
a naive ``mount``) shows only the data track, so a disc that looks like a
CD-ROM full of MP3s can still carry a full Red Book audio programme in front
of it.  ``read_toc`` always reports the audio tracks, and the MusicBrainz disc
ID is computed with libdiscid's mixed-mode lead-out convention (the data
track's start minus 11400 frames).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

# libdiscid's convention for a disc whose last track is data: the lead-out used
# for disc ID purposes is the data track's start address minus this many frames.
DATA_TRACK_LEADOUT_GAP = 11400

# A CD frame (sector) is 1/75 s of audio.
FRAMES_PER_SECOND = 75

# Red Book puts the first track at 2 seconds; libdiscid offsets are LBA + 150.
LBA_OFFSET = 150


@dataclass(frozen=True)
class Track:
    number: int
    start_lba: int
    length_frames: int
    is_data: bool

    @property
    def seconds(self) -> float:
        return self.length_frames / FRAMES_PER_SECOND

    @property
    def duration(self) -> str:
        total = self.length_frames / FRAMES_PER_SECOND
        return "%d:%05.2f" % (int(total // 60), total % 60)


@dataclass(frozen=True)
class Toc:
    device: str
    tracks: tuple[Track, ...]
    leadout_lba: int

    @property
    def audio_tracks(self) -> tuple[Track, ...]:
        return tuple(t for t in self.tracks if not t.is_data)

    @property
    def data_tracks(self) -> tuple[Track, ...]:
        return tuple(t for t in self.tracks if t.is_data)

    @property
    def is_mixed_mode(self) -> bool:
        return bool(self.audio_tracks) and bool(self.data_tracks)

    @property
    def total_frames(self) -> int:
        return sum(t.length_frames for t in self.audio_tracks)

    @property
    def total_duration(self) -> str:
        total = self.total_frames / FRAMES_PER_SECOND
        return "%d:%05.2f" % (int(total // 60), total % 60)

    @property
    def discid_offsets(self) -> list[int]:
        """Track offsets as libdiscid wants them (LBA + 150)."""
        return [t.start_lba + LBA_OFFSET for t in self.audio_tracks]

    @property
    def discid_leadout(self) -> int:
        """Lead-out offset for disc ID purposes.

        For a mixed-mode disc this is the data track's start minus
        DATA_TRACK_LEADOUT_GAP, which is what libdiscid (and therefore
        MusicBrainz) uses.  Otherwise it is the real lead-out.
        """
        data = self.data_tracks
        if data:
            return data[0].start_lba + LBA_OFFSET - DATA_TRACK_LEADOUT_GAP
        return self.leadout_lba + LBA_OFFSET


class TocReadError(RuntimeError):
    pass


_TRACK_RE = re.compile(
    r"^\s*(\d+)\.\s+(\d+)\s+\[[\d:.]+\]\s+(\d+)\s+\[[\d:.]+\]", re.M
)


def read_toc(device: str = "/dev/cdrom") -> Toc:
    """Read the audio TOC, however this platform can.

    On Windows there is no cd-paranoia, which used to make the whole disc side
    of cdrip unusable on the machine that runs EAC. Windows exposes the raw
    TOC through DeviceIoControl instead, so :mod:`cdrip.wintoc` is used there -
    no package to install, and it reports the data track and the real lead-out
    directly rather than leaving them to be inferred.

    Elsewhere this shells out to ``cd-paranoia -Q``, which reports audio tracks
    only - which is what we want - and unlike ``cd-info`` does not stall for
    minutes doing a full disc-mode analysis on a mixed-mode disc.
    """
    if sys.platform == "win32":
        from . import wintoc

        try:
            return wintoc.read_toc(device)
        except wintoc.WinTocError as exc:
            raise TocReadError(str(exc)) from exc

    if not shutil.which("cd-paranoia"):
        raise TocReadError("cd-paranoia not found; install the cdparanoia package")

    proc = subprocess.run(
        ["cd-paranoia", "-d", device, "-Q"],
        capture_output=True,
        text=True,
    )
    # cd-paranoia writes the TOC to stderr.
    text = proc.stderr + proc.stdout
    matches = _TRACK_RE.findall(text)
    if not matches:
        raise TocReadError(
            "no audio tracks found on %s; is this a data-only disc?\n%s"
            % (device, text.strip()[-500:])
        )

    tracks: list[Track] = []
    for number, length, begin in matches:
        tracks.append(
            Track(
                number=int(number),
                start_lba=int(begin),
                length_frames=int(length),
                is_data=False,
            )
        )

    leadout = tracks[-1].start_lba + tracks[-1].length_frames
    data_start = _find_data_track_start(device)
    if data_start is not None:
        tracks.append(
            Track(
                number=tracks[-1].number + 1,
                start_lba=data_start,
                length_frames=0,
                is_data=True,
            )
        )
    return Toc(device=device, tracks=tuple(tracks), leadout_lba=leadout)


_CDINFO_DATA_RE = re.compile(r"^\s*(\d+):\s+\d+:\d+:\d+\s+(\d+)\s+data", re.M | re.I)


def _find_data_track_start(device: str) -> int | None:
    """Return the LBA of the first data track, or None if the disc is audio-only.

    Uses the kernel's TOC via ``cd-info``'s track listing.  Failure here is not
    fatal: an audio-only disc simply has no data track, and a disc we cannot
    interrogate is treated the same way (the caller still gets the audio TOC).
    """
    if not shutil.which("cd-info"):
        return None
    try:
        proc = subprocess.run(
            ["cd-info", "--no-analyze", "--no-cddb", "--no-device-info",
             "--no-disc-mode", "-C", device],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    match = _CDINFO_DATA_RE.search(proc.stdout + proc.stderr)
    if match:
        return int(match.group(2))
    return None


def compute_discid(toc: Toc) -> tuple[str, str]:
    """Return ``(disc_id, submission_url)`` for the disc.

    Prefers libdiscid so we agree byte-for-byte with MusicBrainz; falls back to
    a local implementation of the same SHA-1/base64 scheme when the binding is
    not installed.
    """
    offsets = toc.discid_offsets
    first = 1
    last = len(offsets)
    leadout = toc.discid_leadout
    try:
        import libdiscid

        disc = libdiscid.put(first, last, leadout, offsets)
        return disc.id, disc.submission_url
    except ImportError:
        pass

    import base64
    import hashlib

    parts = ["%02X" % first, "%02X" % last, "%08X" % leadout]
    for i in range(99):
        parts.append("%08X" % (offsets[i] if i < len(offsets) else 0))
    digest = hashlib.sha1("".join(parts).encode()).digest()
    disc_id = (
        base64.b64encode(digest)
        .decode()
        .replace("+", ".")
        .replace("/", "_")
        .replace("=", "-")
    )
    toc_param = "+".join(
        str(v) for v in [first, last, leadout, *offsets]
    )
    url = (
        "https://musicbrainz.org/cdtoc/attach?id=%s&tracks=%d&toc=%s"
        % (disc_id, last, toc_param)
    )
    return disc_id, url
