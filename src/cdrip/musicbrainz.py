"""MusicBrainz lookup, match verification, and disc ID submission.

A disc ID that MusicBrainz has never seen returns 404, and whipper then rips
with no metadata at all - it matches on disc ID only, so passing a release ID
does not help.  The fix is to attach the disc ID to the right release once;
every future rip of that disc then tags itself.

MusicBrainz has no web-service endpoint for attaching a disc ID (Picard and
whipper both just open the browser at the attach URL), so ``attach_discid``
drives the ordinary HTML form with a logged-in session cookie.  That is the
same shape as the RED/OPS session cookies already kept in Vault.  It is also
the most brittle part of this package, so it fails loudly and falls back to
handing you the URL rather than guessing.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

WS = "https://musicbrainz.org/ws/2"
BASE = "https://musicbrainz.org"
USER_AGENT = "cdrip/1.0 ( https://github.com/ )"

# MusicBrainz rounds track lengths it got from other sources, so an exact
# frame match is not expected.  Two seconds is tight enough to reject a
# different pressing and loose enough to accept the same one.
MATCH_TOLERANCE_FRAMES = 150


class MusicBrainzError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseMatch:
    mbid: str
    title: str
    artist: str
    date: str
    country: str
    track_count: int
    fmt: str
    max_delta_frames: int

    @property
    def summary(self) -> str:
        return "%s - %s (%s, %s, %s) max delta %+d frames" % (
            self.artist, self.title, self.date, self.country, self.fmt,
            self.max_delta_frames,
        )


def _request(url: str, tries: int = 5, opener=None) -> dict:
    """GET JSON from the MusicBrainz web service, backing off on 503.

    MusicBrainz throttles aggressively and answers 503 (not 429) when it wants
    you to slow down, so a bare failure here usually just means "wait".
    """
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            fh = opener.open(req, timeout=30) if opener else urllib.request.urlopen(req, timeout=30)
            with fh as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except Exception as exc:  # noqa: BLE001 - network shapes vary
            last = exc
        time.sleep(6 + attempt * 4)
    raise MusicBrainzError("MusicBrainz unreachable: %s" % last)


def lookup_discid(disc_id: str) -> dict | None:
    """Return the release list for a disc ID, or None if MusicBrainz has none."""
    url = "%s/discid/%s?fmt=json&inc=artist-credits+recordings+labels" % (
        WS, urllib.parse.quote(disc_id, safe="")
    )
    try:
        return _request(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def search_releases(artist: str, album: str, limit: int = 10) -> list[dict]:
    """Lucene search for candidate releases by artist and album name."""
    quote = chr(34)
    query = "artist:%s%s%s AND release:%s%s%s" % (
        quote, artist, quote, quote, album, quote
    )
    url = "%s/release?query=%s&fmt=json&limit=%d" % (
        WS, urllib.parse.quote_plus(query), limit
    )
    return _request(url).get("releases", [])


def get_release(mbid: str) -> dict:
    url = ("%s/release/%s?fmt=json&inc=recordings+artist-credits+labels"
           "+release-groups+media" % (WS, mbid))
    return _request(url)


def verify_against_toc(release: dict, track_lengths: list[int]) -> int | None:
    """Return the worst per-track delta in frames, or None if it cannot match.

    ``track_lengths`` is the disc's audio track lengths in frames.  A release
    with a different track count, or any track off by more than
    MATCH_TOLERANCE_FRAMES, is rejected outright - attaching a disc ID to the
    wrong release is a public mistake that someone else has to clean up.
    """
    media = release.get("media") or []
    for medium in media:
        tracks = medium.get("tracks") or []
        if len(tracks) != len(track_lengths):
            continue
        worst = 0
        ok = True
        for track in tracks:
            position = int(track["position"])
            length_ms = track.get("length")
            if not length_ms:
                ok = False
                break
            mb_frames = round(length_ms / 1000.0 * 75)
            delta = mb_frames - track_lengths[position - 1]
            if abs(delta) > MATCH_TOLERANCE_FRAMES:
                ok = False
                break
            worst = max(worst, abs(delta))
        if ok:
            return worst
    return None


def find_match(artist: str, album: str, track_lengths: list[int]) -> list[ReleaseMatch]:
    """Search MusicBrainz and keep only releases whose tracks match the TOC."""
    matches: list[ReleaseMatch] = []
    for candidate in search_releases(artist, album):
        full = get_release(candidate["id"])
        delta = verify_against_toc(full, track_lengths)
        if delta is None:
            continue
        medium = (full.get("media") or [{}])[0]
        matches.append(
            ReleaseMatch(
                mbid=full["id"],
                title=full.get("title", ""),
                artist=", ".join(
                    a["artist"]["name"] for a in full.get("artist-credit", [])
                ),
                date=full.get("date", ""),
                country=full.get("country", ""),
                track_count=len(medium.get("tracks") or []),
                fmt=medium.get("format", ""),
                max_delta_frames=delta,
            )
        )
        time.sleep(1.1)  # MusicBrainz asks for <= 1 request per second.
    return sorted(matches, key=lambda m: m.max_delta_frames)


def _opener_with_session(session_cookie: str):
    """Build a urllib opener carrying a MusicBrainz login cookie."""
    jar = http.cookiejar.CookieJar()
    cookie = http.cookiejar.Cookie(
        version=0, name="musicbrainz_server_session", value=session_cookie,
        port=None, port_specified=False, domain="musicbrainz.org",
        domain_specified=True, domain_initial_dot=False, path="/",
        path_specified=True, secure=True, expires=None, discard=False,
        comment=None, comment_url=None, rest={}, rfc2109=False,
    )
    jar.set_cookie(cookie)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'(\w[\w-]*)\s*=\s*"([^"]*)"')


def _form_fields(page: str) -> dict[str, str]:
    """Scrape hidden form inputs (CSRF token and friends) out of the attach page."""
    fields: dict[str, str] = {}
    for tag in _INPUT_RE.findall(page):
        attrs = dict(_ATTR_RE.findall(tag))
        name = attrs.get("name")
        if name and attrs.get("type", "text").lower() in ("hidden", "text"):
            fields[name] = attrs.get("value", "")
    return fields


def attach_discid(
    disc_id: str,
    toc_param: str,
    tracks: int,
    release_mbid: str,
    session_cookie: str,
) -> str:
    """Attach ``disc_id`` to ``release_mbid`` using a logged-in session cookie.

    Returns the URL of the resulting release.  Raises MusicBrainzError if the
    session is not logged in or the form did not come back as expected - in
    which case the caller should fall back to the manual submission URL rather
    than retry blindly.
    """
    opener = _opener_with_session(session_cookie)
    query = urllib.parse.urlencode(
        {"id": disc_id, "tracks": tracks, "toc": toc_param, "release": release_mbid}
    )
    url = "%s/cdtoc/attach?%s" % (BASE, query)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=30) as resp:
        page = resp.read().decode("utf-8", "replace")
        final_url = resp.geturl()

    if "/login" in final_url or ("Sign in" in page and "csrf_token" not in page):
        raise MusicBrainzError(
            "MusicBrainz session cookie is not logged in; refresh it"
        )

    fields = _form_fields(page)
    if not any(k.endswith("csrf_token") for k in fields):
        raise MusicBrainzError(
            "attach form did not contain a CSRF token - the page layout may "
            "have changed; submit manually at %s" % url
        )
    fields.setdefault("confirm.edit_note", "Disc ID submitted by cdrip")

    data = urllib.parse.urlencode(fields).encode()
    post = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(post, timeout=30) as resp:
        return resp.geturl()
