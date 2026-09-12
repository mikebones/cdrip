"""Optical drive discovery, read offset, and cache-defeat capability.

Two facts decide whether a rip can be trusted, and neither is discoverable
from the disc itself:

* the drive's **read offset** - every drive starts reading a few samples
  early or late, and without correcting for it the rip is bit-shifted
  against everyone else's;
* whether the drive's **audio cache can be defeated** - if it cannot, a
  re-read after an error just replays the cache and the "verification" is
  worthless.

The offset comes from AccurateRip's public drive database (a table keyed on
the drive's SCSI vendor/model strings).  The cache answer comes from
``whipper drive analyze``, which whipper caches in its own config file.
"""

from __future__ import annotations

import configparser
import html
import os
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass

ACCURATERIP_OFFSET_URL = "http://www.accuraterip.com/driveoffsets.htm"
WHIPPER_CONFIG = os.path.expanduser("~/.config/whipper/whipper.conf")


@dataclass(frozen=True)
class Drive:
    device: str
    vendor: str
    model: str
    release: str

    @property
    def label(self) -> str:
        return ("%s %s" % (self.vendor, self.model)).strip()


class DriveError(RuntimeError):
    pass


_DRIVE_RE = re.compile(
    r"drive:\s*(?P<device>\S+),\s*vendor:\s*(?P<vendor>.*?),\s*"
    r"model:\s*(?P<model>.*?),\s*release:\s*(?P<release>\S*)\s*$",
    re.M,
)


def detect_drives() -> list[Drive]:
    """Enumerate optical drives via ``whipper drive list``."""
    proc = subprocess.run(
        ["whipper", "drive", "list"], capture_output=True, text=True
    )
    drives = [
        Drive(
            device=m.group("device"),
            vendor=m.group("vendor").strip(),
            model=m.group("model").strip(),
            release=m.group("release").strip(),
        )
        for m in _DRIVE_RE.finditer(proc.stdout + proc.stderr)
    ]
    if not drives:
        raise DriveError(
            "no optical drive found. Output was:\n%s" % (proc.stdout + proc.stderr)
        )
    return drives


def _normalise(text: str) -> str:
    """Collapse whitespace and case so drive strings compare sanely.

    The AccurateRip table and the SCSI INQUIRY response disagree about padding
    ("DVD A  DS8A5SH" vs "DVD A DS8A5SH") and sometimes about whether the
    product family prefix is present at all.
    """
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def fetch_offset_table(timeout: int = 60) -> list[tuple[str, int, int]]:
    """Return ``(normalised drive name, offset, submission count)`` rows."""
    req = urllib.request.Request(
        ACCURATERIP_OFFSET_URL, headers={"User-Agent": "cdrip/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        page = resp.read().decode("utf-8", "replace")

    rows: list[tuple[str, int, int]] = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = [
            html.unescape(re.sub(r"<[^>]*>", "", c)).replace("\xa0", " ").strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S | re.I)
        ]
        if len(cells) < 3:
            continue
        name, offset_text, count_text = cells[0], cells[1], cells[2]
        match = re.match(r"^([+-]?\d+)$", offset_text.replace(" ", ""))
        if not match:
            continue
        try:
            count = int(re.sub(r"[^0-9]", "", count_text) or 0)
        except ValueError:
            count = 0
        rows.append((_normalise(name), int(match.group(1)), count))
    return rows


def lookup_offset(drive: Drive, table: list[tuple[str, int, int]] | None = None) -> int | None:
    """Best-effort read offset for ``drive`` from the AccurateRip table.

    Rows in that table are written "Vendor - Model".  We try the full
    "vendor model" string first, then the model alone, and among the matches
    prefer the one backed by the most submissions.
    """
    if table is None:
        table = fetch_offset_table()

    for candidate in (drive.label, drive.model):
        needle = _normalise(candidate)
        if not needle:
            continue
        matches = [row for row in table if row[0] == needle]
        if not matches:
            matches = [row for row in table if needle and needle in row[0]]
        if matches:
            best = max(matches, key=lambda row: row[2])
            return best[1]
    return None


def analyze_cache(device: str) -> bool | None:
    """Run ``whipper drive analyze`` and report whether the cache can be defeated.

    Returns None if whipper could not decide.  The result is persisted by
    whipper itself, so this is a one-off cost per drive.
    """
    subprocess.run(
        ["whipper", "drive", "analyze", "-d", device],
        capture_output=True,
        text=True,
    )
    return read_cached_cache_flag(device)


def read_cached_cache_flag(device: str) -> bool | None:
    """Read ``defeats_cache`` for the drive out of whipper's own config."""
    if not os.path.exists(WHIPPER_CONFIG):
        return None
    parser = configparser.ConfigParser()
    parser.read(WHIPPER_CONFIG)
    for section in parser.sections():
        if not section.startswith("drive:"):
            continue
        if parser.has_option(section, "defeats_cache"):
            try:
                return parser.getboolean(section, "defeats_cache")
            except ValueError:
                return None
    return None
