"""Confirming an upload is actually seeding.

A release went live on a tracker and seeded nowhere. salmon connected to the
torrent client, printed "Configured local uploader to " with an empty
destination, added nothing, saved no .torrent, and reported success. The
tracker showed 0 seeders and 1 leecher - somebody was already waiting on a
torrent with no seed - and nothing in the pipeline noticed.

Nothing here adds a torrent; that belongs to whichever component owns the
client. What this does is refuse to call an upload finished until a client is
actually seeding it, because every signal that was supposed to say so lied:

* ``salmon up`` exited 0 on an upload it had refused, and exited 1 on an
  upload that fully succeeded.
* Its log said "Uploading torrent... done" for a torrent it never added.

So the check is made against the client, by infohash, and it reports what it
found rather than a boolean.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

# transmission's status codes.
STOPPED, CHECK_WAIT, CHECKING, DL_WAIT, DOWNLOADING, SEED_WAIT, SEEDING = range(7)

STATUS_NAMES = {
    STOPPED: "stopped", CHECK_WAIT: "check pending", CHECKING: "verifying",
    DL_WAIT: "download pending", DOWNLOADING: "downloading",
    SEED_WAIT: "seed pending", SEEDING: "seeding",
}

# States that mean "this will seed"; verifying is on its way there.
HEALTHY = (CHECKING, CHECK_WAIT, SEED_WAIT, SEEDING)


class SeedingError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeedState:
    found: bool
    status: int | None = None
    percent_done: float = 0.0
    error: str = ""
    client: str = ""

    @property
    def seeding(self) -> bool:
        return self.found and self.status == SEEDING and self.percent_done >= 1.0

    @property
    def healthy(self) -> bool:
        """Seeding, or on a path that ends in seeding."""
        return self.found and self.status in HEALTHY and not self.error

    def __str__(self) -> str:
        if not self.found:
            return ("NOT in the torrent client. The upload is live on the "
                    "tracker and seeding nowhere.")
        name = STATUS_NAMES.get(self.status, str(self.status))
        out = "%s, %.1f%% complete" % (name, self.percent_done * 100)
        if self.client:
            out += " on %s" % self.client
        if self.error:
            out += " - ERROR: %s" % self.error
        return out


def infohash(torrent_path: str) -> str:
    """SHA-1 of a .torrent's info dict - how a client identifies it.

    Computed by walking the bencoding rather than with a torrent library, so
    this has no dependencies; the info dict must be hashed exactly as it
    appears on disk, which rules out decode-then-re-encode.
    """
    with open(torrent_path, "rb") as fh:
        raw = fh.read()

    start = raw.find(b"4:infod")
    if start < 0:
        raise SeedingError("%s has no info dict - not a torrent file"
                           % torrent_path)
    start += len(b"4:info")

    def end_of(blob: bytes, pos: int) -> int:
        kind = blob[pos:pos + 1]
        if kind in (b"d", b"l"):
            pos += 1
            while blob[pos:pos + 1] != b"e":
                pos = end_of(blob, pos)
            return pos + 1
        if kind == b"i":
            return blob.index(b"e", pos) + 1
        match = re.match(rb"(\d+):", blob[pos:pos + 20])
        if not match:
            raise SeedingError("malformed bencoding at byte %d" % pos)
        return pos + match.end() + int(match.group(1))

    return hashlib.sha1(raw[start:end_of(raw, start)]).hexdigest()


def _rpc(url: str, method: str, args: dict, session: str | None = None,
         timeout: float = 60.0):
    request = urllib.request.Request(
        url, data=json.dumps({"method": method, "arguments": args}).encode(),
        headers={"Content-Type": "application/json",
                 **({"X-Transmission-Session-Id": session} if session else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response), None
    except urllib.error.HTTPError as exc:
        # 409 carries the session id; it is the handshake, not a failure.
        return None, exc.headers.get("X-Transmission-Session-Id")


def check(rpc_url: str, torrent_hash: str, timeout: float = 60.0,
          client: str = "") -> SeedState:
    """Ask a transmission-compatible client whether it holds this torrent.

    ``rpc_url`` should address ONE client. A proxy that fans out across
    several waits on every member, so a single wedged instance turns this into
    a multi-minute call - measured at fifteen minutes on a fleet with one hung
    member.
    """
    _, session = _rpc(rpc_url, "torrent-get", {"ids": [torrent_hash],
                                               "fields": ["name"]}, timeout=15)
    response, _ = _rpc(rpc_url, "torrent-get", {
        "ids": [torrent_hash],
        "fields": ["status", "percentDone", "errorString", "name"],
    }, session, timeout)

    if not response or "arguments" not in response:
        return SeedState(found=False, client=client)
    torrents = response["arguments"].get("torrents") or []
    if not torrents:
        return SeedState(found=False, client=client)

    first = torrents[0]
    return SeedState(
        found=True,
        status=first.get("status"),
        percent_done=float(first.get("percentDone") or 0.0),
        error=(first.get("errorString") or "").strip(),
        client=client,
    )
