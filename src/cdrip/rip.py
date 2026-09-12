"""Driving whipper, and the two different things "verified" can mean.

whipper already does EAC-style **test-and-copy per track**: it reads each
track twice and records both CRCs in the log ("Test CRC" / "Copy CRC").  Equal
CRCs mean the read is repeatable on this drive, with this disc, in this
session.  That is real verification and it is not what ``double_rip`` is for.

What it does not prove is that the read is *correct*.  A consistent offset
error, or a drive that mis-reads the same sector the same way twice, passes
test-and-copy happily.  That is normally AccurateRip's job - agreeing with
other people's rips, on other drives - and plenty of discs (promos,
small-label pressings) have no AccurateRip entry at all.

``double_rip`` is the fallback for those discs.  Ripping the whole disc again
adds a fresh TOC read, a fresh spin-up and a separate seek pattern, so it
catches session-level problems that a per-track re-read inside one pass can
miss.  It is strictly weaker than AccurateRip and strictly stronger than
nothing; when the log already shows Test CRC == Copy CRC on every track, treat
it as a cross-check rather than the primary evidence.

The comparison uses each FLAC's stored MD5 of the *decoded audio*, so it is
unaffected by tags - an untagged pass and a tagged pass of the same disc
compare equal.
"""

from __future__ import annotations

import os
import time
import shutil
import subprocess
from dataclasses import dataclass


class RipError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrackComparison:
    name: str
    md5_a: str
    md5_b: str

    @property
    def matches(self) -> bool:
        return bool(self.md5_a) and self.md5_a == self.md5_b


@dataclass(frozen=True)
class RipResult:
    directory: str
    log_path: str | None
    cue_path: str | None
    flacs: tuple[str, ...]


def rip(
    output_dir: str,
    offset: int,
    disc_template: str,
    track_template: str = "%t",
    device: str = "/dev/cdrom",
    unknown: bool = False,
    extra_args: tuple[str, ...] = (),
) -> RipResult:
    """Run ``whipper cd rip`` into ``output_dir``.

    ``track_template`` defaults to the bare track number.  That is deliberate:
    when MusicBrainz has no metadata there are no titles to put in a filename,
    and renaming the files afterwards would leave the .cue and .log pointing at
    names that no longer exist.  Tag the files instead and leave them be.
    """
    os.makedirs(output_dir, exist_ok=True)
    cmd = [
        "whipper", "cd", "rip",
        "-d", device,
        "-o", str(offset),
        "-O", output_dir,
        "--track-template", track_template,
        "--disc-template", disc_template,
        "--keep-going",
    ]
    if unknown:
        cmd.append("--unknown")
    cmd.extend(extra_args)

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RipError(
            "whipper exited %d\n%s" % (proc.returncode, (proc.stdout + proc.stderr)[-3000:])
        )
    return collect(output_dir)


def collect(directory: str) -> RipResult:
    """Gather the artefacts whipper left in ``directory``."""
    flacs = sorted(
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if f.lower().endswith(".flac")
    )
    log = next(
        (os.path.join(directory, f) for f in sorted(os.listdir(directory))
         if f.lower().endswith(".log")), None
    )
    cue = next(
        (os.path.join(directory, f) for f in sorted(os.listdir(directory))
         if f.lower().endswith(".cue")), None
    )
    return RipResult(
        directory=directory, log_path=log, cue_path=cue, flacs=tuple(flacs)
    )


def flac_md5(path: str) -> str:
    """Return the MD5 of the *decoded audio* stored in a FLAC header.

    This is the right comparison for rip verification: it ignores tags and
    container differences and compares only the PCM the two rips produced.
    """
    if not shutil.which("metaflac"):
        raise RipError("metaflac not found; install the flac package")
    proc = subprocess.run(
        ["metaflac", "--show-md5sum", path], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RipError("metaflac failed on %s: %s" % (path, proc.stderr.strip()))
    return proc.stdout.strip()


def compare(first: RipResult, second: RipResult) -> list[TrackComparison]:
    """Compare two rips track-by-track on decoded-audio MD5."""
    by_name_b = {os.path.basename(p): p for p in second.flacs}
    results: list[TrackComparison] = []
    for path_a in first.flacs:
        name = os.path.basename(path_a)
        path_b = by_name_b.get(name)
        results.append(
            TrackComparison(
                name=name,
                md5_a=flac_md5(path_a),
                md5_b=flac_md5(path_b) if path_b else "",
            )
        )
    return results


def disc_present(device: str = "/dev/cdrom") -> bool:
    """Whether the drive currently reports an audio disc."""
    proc = subprocess.run(
        ["cd-paranoia", "-d", device, "-Q"], capture_output=True, text=True
    )
    text = proc.stdout + proc.stderr
    return "No medium found" not in text and "Unable find" not in text


def wait_for_disc(
    device: str = "/dev/cdrom",
    timeout: int = 300,
    notify=None,
) -> bool:
    """Get the disc back into the drive between passes.

    whipper ejects the disc when a rip finishes, so the second pass of a double
    rip starts with an empty drive and dies with a confusing
    FileNotFoundError from cdrdao's TOC reader.  Try to close the tray
    ourselves; slot-loading and many slim USB drives do not support the close
    command at all ("CD-ROM tray close command failed"), so fall back to asking
    and waiting rather than failing.
    """
    if disc_present(device):
        return True

    subprocess.run(["eject", "-t", device], capture_output=True, text=True)
    if disc_present(device):
        return True

    if notify:
        notify("The drive ejected the disc and cannot close its own tray. "
               "Please re-insert it; waiting up to %d seconds." % timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if disc_present(device):
            return True
        time.sleep(3)
    return False


def double_rip(
    base_dir: str,
    offset: int,
    disc_template: str,
    device: str = "/dev/cdrom",
    unknown: bool = False,
    notify=None,
) -> tuple[RipResult, list[TrackComparison]]:
    """Rip twice into ``base_dir``/pass1 and pass2 and compare.

    Returns the first rip (the one to keep) and the per-track comparison.  The
    second rip is only evidence; the caller decides whether to delete it.
    """
    first = rip(
        os.path.join(base_dir, "pass1"), offset, disc_template,
        device=device, unknown=unknown,
    )
    if not wait_for_disc(device, notify=notify):
        raise RipError(
            "disc was ejected after the first pass and did not come back; "
            "re-insert it and re-run, or pass --no-double"
        )
    second = rip(
        os.path.join(base_dir, "pass2"), offset, disc_template,
        device=device, unknown=unknown,
    )
    return first, compare(first, second)
