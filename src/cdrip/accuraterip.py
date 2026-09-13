"""AccurateRip disc identity - and why a mixed-mode disc can look absent.

AccurateRip identifies a disc by three numbers derived from its TOC. On an
ordinary audio CD every tool agrees on them. On a **mixed-mode** disc - audio
tracks followed by a data track - they do not, and the disagreement is silent:
the lookup succeeds, returns nothing, and the ripper honestly reports "track
not present in AccurateRip database".

That is exactly what happened on the disc this module was written for. whipper
reported all six tracks absent. EAC, on the same disc in the same drive, found
every track at **confidence 2**. The disc was in AccurateRip the whole time;
whipper was asking about a disc that is not.

The difference is the lead-out. libdiscid (and so whipper) applies the
MusicBrainz rule for a trailing data track - lead-out becomes the data track's
start minus 11400 frames - because that is what makes a *MusicBrainz disc ID*
stable. AccurateRip uses the **real** lead-out of the whole disc, and its CDDB
component counts the data track as a track. Verified against EAC's own
AccurateRip-Offset-log.txt for that disc:

    cddb 4f0c1b07 = 7 tracks (data track counted), real lead-out 232479
    id1  000822ea = sum(audio offsets) + real lead-out
    id2  002e671b = sum(offset_i * i) + real lead-out * (n + 1)

So the practical rule this module exists to encode: **on a mixed-mode disc, an
empty AccurateRip result is not evidence of absence.** Compute the identity
both ways and say so, rather than concluding the disc is unknown and falling
back to weaker verification - which cost three redundant rips.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscIds:
    """AccurateRip's three identifiers, plus how they were derived."""

    id1: int
    id2: int
    cddb: int
    track_count: int
    leadout: int
    counted_data_track: bool

    @property
    def url_path(self) -> str:
        """The path AccurateRip's service is queried at."""
        return "dBAR-%03d-%08x-%08x-%08x.bin" % (
            self.track_count, self.id1, self.id2, self.cddb
        )

    def __str__(self) -> str:
        return "%s (leadout %d, %s data track)" % (
            self.url_path, self.leadout,
            "counting" if self.counted_data_track else "ignoring",
        )


def _cddb_id(offsets_lba: list[int], leadout_lba: int) -> int:
    def digit_sum(n: int) -> int:
        return sum(int(d) for d in str(n))

    total = sum(digit_sum((o + 150) // 75) for o in offsets_lba)
    elapsed = (leadout_lba + 150) // 75 - (offsets_lba[0] + 150) // 75
    return ((total % 0xFF) << 24 | elapsed << 8 | len(offsets_lba)) & 0xFFFFFFFF


def disc_ids(
    audio_offsets: list[int],
    leadout: int,
    data_track_start: int | None = None,
) -> DiscIds:
    """Compute AccurateRip's identity for a disc.

    ``audio_offsets`` are audio track start LBAs. ``leadout`` is the real
    lead-out of the whole disc - on a mixed-mode disc that is past the data
    track, NOT the end of the audio programme. ``data_track_start`` is given
    when a trailing data track exists; it only affects the CDDB component,
    which counts it as a track.
    """
    id1 = 0
    id2 = 0
    for index, offset in enumerate(audio_offsets, start=1):
        id1 += offset
        id2 += max(offset, 1) * index
    id1 += leadout
    id2 += max(leadout, 1) * (len(audio_offsets) + 1)

    cddb_offsets = list(audio_offsets)
    if data_track_start is not None:
        cddb_offsets = cddb_offsets + [data_track_start]

    return DiscIds(
        id1=id1 & 0xFFFFFFFF,
        id2=id2 & 0xFFFFFFFF,
        cddb=_cddb_id(cddb_offsets, leadout),
        track_count=len(cddb_offsets),
        leadout=leadout,
        counted_data_track=data_track_start is not None,
    )


def identities_for(toc) -> dict[str, DiscIds]:
    """Both plausible identities for a TOC, so they can be compared.

    ``accuraterip`` is the one AccurateRip actually keys on. ``audio_only`` is
    what a tool using the MusicBrainz mixed-mode lead-out rule would ask about
    - included so a caller can show *why* a lookup came back empty rather than
    just reporting absence.
    """
    audio = [t.start_lba for t in toc.audio_tracks]
    data = toc.data_tracks[0].start_lba if toc.data_tracks else None
    real_leadout = toc.leadout_lba if not toc.data_tracks else None

    out: dict[str, DiscIds] = {}
    if real_leadout is not None:
        out["accuraterip"] = disc_ids(audio, real_leadout)
    out["audio_only"] = disc_ids(audio, toc.discid_leadout - 150)
    return out


def absence_is_inconclusive(toc) -> bool:
    """Whether an empty AccurateRip result should be trusted for this disc.

    On a mixed-mode disc it should not: the lookup was probably made with the
    audio-only lead-out, which is a different disc as far as AccurateRip is
    concerned.
    """
    return bool(getattr(toc, "is_mixed_mode", False))


def explain_absence(toc) -> str:
    """Message for an empty AccurateRip result."""
    if not absence_is_inconclusive(toc):
        return (
            "Not in AccurateRip. For an audio-only disc this is conclusive - "
            "nobody has submitted it."
        )
    return (
        "AccurateRip returned nothing, but this is a MIXED-MODE disc and that "
        "result is not conclusive. AccurateRip keys on the real lead-out of "
        "the whole disc and counts the data track; a ripper using the "
        "MusicBrainz mixed-mode rule (lead-out = data track start - 11400) "
        "asks about a different disc and gets a legitimate empty answer. "
        "Verified on one such disc: whipper reported every track absent while "
        "EAC found all six at confidence 2. Check with a tool that uses "
        "AccurateRip's own convention before concluding the disc is unknown."
    )
