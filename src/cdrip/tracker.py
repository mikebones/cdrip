"""Asking the tracker what already exists, before spending an upload.

Three things went wrong on this release that a single API call would have
caught, and all three cost a deleted torrent:

* The upload landed in a **new group** with "unknown" release type and
  "unknown" tags, because nothing told it what the existing group already
  said. The group carries both, and they are simply readable.
* Whether a rip is a duplicate is not obvious by eye. RED 2.2.11.1.1 says a
  different *medium* is not a dupe, so a FLAC CD rip can coexist with a FLAC
  WEB release of the same album - but a second FLAC CD would be a dupe. That
  is a comparison of (format, encoding, medium), not of album names.
* Edition information (2.1.22) is usually already on a sibling torrent, so it
  can be carried over instead of guessed - as long as it is clear which
  medium it came from, because a WEB edition's catalogue number is evidence
  about the WEB release, not proof about the CD.

This module only reads. It does not upload; salmon does that.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE = "https://redacted.sh"

# Gazelle's numeric release types. Only the ones that come up here are named;
# an unknown value is reported as its number rather than guessed at.
RELEASE_TYPES = {
    1: "Album", 3: "Soundtrack", 5: "EP", 6: "Anthology", 7: "Compilation",
    9: "Single", 11: "Live album", 13: "Remix", 14: "Bootleg",
    15: "Interview", 16: "Mixtape", 17: "Demo", 18: "Concert recording",
    19: "DJ mix", 21: "Unknown",
}


class TrackerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Torrent:
    id: int
    format: str
    encoding: str
    media: str
    label: str
    catalogue: str
    edition_year: int
    edition_title: str
    log_score: int | None
    has_log: bool
    has_cue: bool
    size: int

    @property
    def slot(self) -> tuple[str, str, str]:
        """What makes two torrents duplicates of each other."""
        return (self.format, self.encoding, self.media)

    def __str__(self) -> str:
        edition = " / ".join(x for x in (self.label, self.catalogue) if x)
        return "%-8s %-5s %-10s %-4s %s" % (
            self.id, self.format, self.encoding, self.media,
            edition or "(no edition info)",
        )


@dataclass
class Group:
    id: int
    name: str
    artist: str
    year: int
    tags: list[str] = field(default_factory=list)
    release_type: int = 0
    torrents: list[Torrent] = field(default_factory=list)

    @property
    def release_type_name(self) -> str:
        return RELEASE_TYPES.get(self.release_type, "type %d" % self.release_type)

    def occupied_slots(self) -> set[tuple[str, str, str]]:
        return {t.slot for t in self.torrents}

    def would_duplicate(self, fmt: str, encoding: str, media: str) -> Torrent | None:
        """The torrent our upload would duplicate, if any."""
        for t in self.torrents:
            if t.slot == (fmt, encoding, media):
                return t
        return None

    def edition_for(self, media: str) -> Torrent | None:
        """A sibling on the same medium carrying edition information."""
        for t in self.torrents:
            if t.media == media and (t.label or t.catalogue):
                return t
        return None

    def any_edition(self) -> Torrent | None:
        for t in self.torrents:
            if t.label or t.catalogue:
                return t
        return None


def _get(url: str, api_key: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, headers={
        "Authorization": api_key,
        "User-Agent": "cdrip/1.0 ( https://github.com/mikebones/cdrip )",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TrackerError("HTTP %d from %s" % (exc.code, url)) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TrackerError("could not reach the tracker: %s" % exc) from exc
    if payload.get("status") != "success":
        raise TrackerError("tracker said %r" % payload.get("status"))
    return payload["response"]


def group(group_id: int, api_key: str, base: str = DEFAULT_BASE) -> Group:
    """Read a torrent group and everything already in it."""
    data = _get(
        "%s/ajax.php?action=torrentgroup&id=%d" % (base.rstrip("/"), group_id),
        api_key,
    )
    raw = data["group"]
    artists = (raw.get("musicInfo") or {}).get("artists") or []
    out = Group(
        id=group_id,
        name=raw.get("name", ""),
        artist=artists[0]["name"] if artists else "",
        year=raw.get("year") or 0,
        tags=list(raw.get("tags") or []),
        release_type=raw.get("releaseType") or 0,
    )
    for t in data.get("torrents") or []:
        out.torrents.append(Torrent(
            id=int(t["id"]),
            format=t.get("format", ""),
            encoding=t.get("encoding", ""),
            media=t.get("media", ""),
            label=(t.get("remasterRecordLabel") or "").strip(),
            catalogue=(t.get("remasterCatalogueNumber") or "").strip(),
            edition_year=t.get("remasterYear") or 0,
            edition_title=(t.get("remasterTitle") or "").strip(),
            log_score=t.get("logScore"),
            has_log=bool(t.get("hasLog")),
            has_cue=bool(t.get("hasCue")),
            size=t.get("size") or 0,
        ))
    return out


def preflight(grp: Group, fmt: str = "FLAC", encoding: str = "Lossless",
              media: str = "CD") -> list[str]:
    """What to know before uploading into this group.

    Returns lines to show the user. A duplicate is reported as a blocking
    problem; everything else is information worth carrying into the upload.
    """
    lines: list[str] = []
    dupe = grp.would_duplicate(fmt, encoding, media)
    if dupe:
        lines.append(
            "DUPLICATE: %s/%s/%s already exists as torrent %d. RED 2.2.11.1.1 "
            "only exempts a *different* medium." % (fmt, encoding, media, dupe.id)
        )
    else:
        occupied = ", ".join(
            "/".join(s) for s in sorted(grp.occupied_slots())
        ) or "(nothing)"
        lines.append(
            "Not a duplicate: %s/%s/%s is free in this group. Present: %s"
            % (fmt, encoding, media, occupied)
        )

    lines.append("Release type: %s - use this, not the uploader's guess, or "
                 "the upload lands as 'unknown'." % grp.release_type_name)
    lines.append("Group tags: %s" % (", ".join(grp.tags) or "(none)"))

    same = grp.edition_for(media)
    other = grp.any_edition()
    if same:
        lines.append("Edition info from a %s sibling (torrent %d): %s / %s"
                     % (media, same.id, same.label or "-", same.catalogue or "-"))
    elif other:
        lines.append(
            "No %s sibling carries edition info. Torrent %d (%s) has %s / %s - "
            "that is evidence about the %s release, so confirm it against the "
            "disc before using it."
            % (media, other.id, other.media, other.label or "-",
               other.catalogue or "-", other.media)
        )
    else:
        lines.append("No edition information anywhere in this group (2.1.22).")
    return lines
