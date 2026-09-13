"""Fill in the metadata a rip cannot get from the disc.

whipper tags from MusicBrainz, which is authoritative for titles and track
order but frequently carries no label, no catalogue number and no genre. Those
three matter downstream: a tracker wants edition information for a CD-sourced
rip (RED 2.1.22), and salmon builds its upload payload by reading the files -
it looks for ``label``/``recordlabel``/``organization``/``publisher`` and
``catalognumber``/``labelno``/``catno``. If the files are silent, the upload
goes out with no edition information and no tags.

**This runs after the rip, never before.** whipper writes tags during the rip
from MusicBrainz, so anything written beforehand is overwritten. It also runs
before the salmon hand-off, because salmon reads these fields to build the
payload. Nothing here renames a file, so the .cue and .log stay valid.

Sources, in order of preference:

1. **MusicBrainz** - genres from the release group and artist; label and
   catalogue number when the release has them. Free and needs no credentials.
2. **Discogs** - often has label and catalogue number where MusicBrainz has
   "[no label]", plus genres and styles. Looking a release up *by id* needs no
   authentication; *searching* for one does, so a release id has to come from
   somewhere - a MusicBrainz url relation, or the caller.
3. **Explicit values** from the caller, which always win.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = "cdrip/1.0 ( https://github.com/mikebones/cdrip )"

# Vorbis fields salmon actually reads, so these are the names worth writing.
LABEL_FIELD = "LABEL"
CATALOGUE_FIELD = "CATALOGNUMBER"
GENRE_FIELD = "GENRE"


@dataclass
class Metadata:
    label: str | None = None
    catalogue: str | None = None
    genres: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def merge(self, other: "Metadata") -> None:
        """Fill only what is still missing; first source to answer wins."""
        if not self.label and other.label:
            self.label = other.label
        if not self.catalogue and other.catalogue:
            self.catalogue = other.catalogue
        for g in other.genres:
            if g not in self.genres:
                self.genres.append(g)
        self.sources.extend(other.sources)

    @property
    def is_empty(self) -> bool:
        return not (self.label or self.catalogue or self.genres)


def _get_json(url: str, timeout: int = 30) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _titlecase_tag(tag: str) -> str:
    """Turn a tracker-style tag into something sensible for a GENRE field.

    Tracker tags use dots where a name has spaces ("hardcore.punk"), and are
    lowercase by convention. A file's GENRE field should read normally.
    """
    return " ".join(part.capitalize() for part in tag.replace(".", " ").split())


def from_musicbrainz(release_mbid: str) -> Metadata:
    """Genres, label and catalogue number from a MusicBrainz release."""
    out = Metadata()
    rel = _get_json(
        "https://musicbrainz.org/ws/2/release/%s?fmt=json&inc=labels+genres+release-groups"
        % urllib.parse.quote(release_mbid, safe="")
    )
    if not rel:
        return out

    for info in rel.get("label-info") or []:
        name = (info.get("label") or {}).get("name")
        # MusicBrainz uses the literal string "[no label]" for self-released
        # and white-label releases. It is a placeholder, not a label name.
        if name and name != "[no label]" and not out.label:
            out.label = name
        if info.get("catalog-number") and not out.catalogue:
            out.catalogue = info["catalog-number"]

    for g in rel.get("genres") or []:
        out.genres.append(_titlecase_tag(g["name"]))

    rg = rel.get("release-group") or {}
    if rg.get("id"):
        rgd = _get_json("https://musicbrainz.org/ws/2/release-group/%s?fmt=json&inc=genres" % rg["id"])
        for g in (rgd or {}).get("genres") or []:
            name = _titlecase_tag(g["name"])
            if name not in out.genres:
                out.genres.append(name)

    if not out.is_empty:
        out.sources.append("musicbrainz:%s" % release_mbid)
    return out


def discogs_release_from_musicbrainz(release_mbid: str) -> str | None:
    """Find a Discogs release id via MusicBrainz's url relations, if any.

    This is the only way to reach Discogs without credentials: looking a
    release up by id is open, searching for one is not.
    """
    rel = _get_json(
        "https://musicbrainz.org/ws/2/release/%s?fmt=json&inc=url-rels"
        % urllib.parse.quote(release_mbid, safe="")
    )
    for r in (rel or {}).get("relations") or []:
        url = (r.get("url") or {}).get("resource", "")
        if "discogs.com/release/" in url:
            tail = url.rstrip("/").split("/release/")[-1]
            digits = "".join(c for c in tail.split("-")[0] if c.isdigit())
            if digits:
                return digits
    return None


def from_discogs(release_id: str) -> Metadata:
    """Label, catalogue number, genres and styles from a Discogs release id."""
    out = Metadata()
    d = _get_json("https://api.discogs.com/releases/%s" % release_id)
    if not d:
        return out
    for lab in d.get("labels") or []:
        if lab.get("name") and not out.label:
            out.label = lab["name"]
        if lab.get("catno") and not out.catalogue:
            out.catalogue = lab["catno"]
    # Styles are the specific ones ("Deathcore"); genres are broad ("Rock").
    # Put styles first - they are what a music tracker's tags look like.
    for g in (d.get("styles") or []) + (d.get("genres") or []):
        name = _titlecase_tag(g)
        if name not in out.genres:
            out.genres.append(name)
    if not out.is_empty:
        out.sources.append("discogs:%s" % release_id)
    return out


def release_mbid_from_files(directory: str) -> str | None:
    """Read MUSICBRAINZ_ALBUMID out of the first tagged FLAC."""
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".flac"):
            continue
        proc = subprocess.run(
            ["metaflac", "--show-tag=MUSICBRAINZ_ALBUMID", os.path.join(directory, name)],
            capture_output=True, text=True,
        )
        for line in proc.stdout.splitlines():
            if "=" in line:
                return line.split("=", 1)[1].strip()
    return None


def collect(
    directory: str,
    label: str | None = None,
    catalogue: str | None = None,
    genres: list[str] | None = None,
    discogs_release: str | None = None,
) -> Metadata:
    """Gather what we can, explicit values first."""
    out = Metadata(label=label, catalogue=catalogue, genres=list(genres or []))
    if not out.is_empty:
        out.sources.append("explicit")

    mbid = release_mbid_from_files(directory)
    if mbid:
        out.merge(from_musicbrainz(mbid))
        if not (out.label and out.catalogue):
            found = discogs_release or discogs_release_from_musicbrainz(mbid)
            if found:
                out.merge(from_discogs(found))
    elif discogs_release:
        out.merge(from_discogs(discogs_release))
    return out


def apply(directory: str, meta: Metadata) -> list[str]:
    """Write the metadata onto every FLAC. Never renames anything."""
    flacs = sorted(f for f in os.listdir(directory) if f.lower().endswith(".flac"))
    written: list[str] = []
    for name in flacs:
        path = os.path.join(directory, name)
        cmd = ["metaflac"]
        if meta.genres:
            cmd += ["--remove-tag=%s" % GENRE_FIELD, "--remove-tag=%s" % GENRE_FIELD.lower()]
        if meta.label:
            cmd += ["--remove-tag=%s" % LABEL_FIELD, "--remove-tag=%s" % LABEL_FIELD.lower()]
        if meta.catalogue:
            cmd += ["--remove-tag=%s" % CATALOGUE_FIELD,
                    "--remove-tag=%s" % CATALOGUE_FIELD.lower()]
        cmd += ["--set-tag=%s=%s" % (GENRE_FIELD, g) for g in meta.genres]
        if meta.label:
            cmd.append("--set-tag=%s=%s" % (LABEL_FIELD, meta.label))
        if meta.catalogue:
            cmd.append("--set-tag=%s=%s" % (CATALOGUE_FIELD, meta.catalogue))
        if len(cmd) == 1:
            return written
        cmd.append(path)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("metaflac failed on %s: %s" % (name, proc.stderr.strip()))
        written.append(name)
    return written
