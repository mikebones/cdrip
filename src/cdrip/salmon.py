"""Hand a finished rip to smoked-salmon.

Everything after the disc read - rip-log validation, FLAC integrity,
upconversion and MQA detection, spectrals, torrent creation - is already
implemented in smoked-salmon, and re-implementing it here would just mean a
second thing to keep correct.  In particular salmon's ``check_log_cambia``
parses the rip log with cambia (the same parser RED uses) *and* independently
recomputes each track's CRC32 from the audio to check it against the log.

salmon may run locally or as a container; set ``mode`` accordingly.  The only
requirement is that the path this module passes to salmon is the path salmon
can see, which is why ``container_path`` exists.
"""

from __future__ import annotations

import os
import posixpath
import shlex
import subprocess
from dataclasses import dataclass


class SalmonError(RuntimeError):
    pass


def _norm(path: str) -> str:
    """Normalise a POSIX path, resolving symlinks only when it really exists.

    Both sides of this mapping are POSIX: one names a directory on the ripping
    host, the other a directory inside salmon's Linux container. Resolving
    symlinks matters on the host (so /data -> /mnt/data cannot sneak past the
    containment check below), but must not happen when the path is
    hypothetical - otherwise this is untestable anywhere but the host.
    """
    if os.name == "posix" and os.path.exists(path):
        path = os.path.realpath(path)
    return posixpath.normpath(path)


@dataclass(frozen=True)
class SalmonTarget:
    """How to reach salmon, and how paths map into it.

    host_root/container_root translate a path on the ripping machine into the
    path salmon sees.  They are equal when salmon runs locally; they differ
    when salmon runs in a container with the library mounted elsewhere, so
    ripping to ``<host_root>/X`` means salmon sees ``<container_root>/X``.
    """

    mode: str = "local"             # "local" or "kubectl"
    namespace: str = ""
    deployment: str = "deploy/smoked-salmon"
    host_root: str = ""
    container_root: str = ""

    def container_path(self, host_path: str) -> str:
        real = _norm(host_path)
        root = _norm(self.host_root)
        # Compare on a path boundary: a bare startswith would also accept
        # "/data/.../completeX" for root "/data/.../complete".
        if real != root and not real.startswith(root.rstrip("/") + "/"):
            raise SalmonError(
                "%s is not under %s, so salmon cannot see it. Rip into the "
                "library path, or adjust host_root/container_root."
                % (real, root)
            )
        rel = posixpath.relpath(real, root)
        return posixpath.normpath(posixpath.join(self.container_root, rel))

    def argv(self, salmon_args: list[str]) -> list[str]:
        if self.mode == "local":
            return ["salmon", *salmon_args]
        return [
            "kubectl", "-n", self.namespace, "exec", self.deployment, "--",
            "salmon", *salmon_args,
        ]


def _run(target: SalmonTarget, salmon_args: list[str], timeout: int = 3600) -> str:
    cmd = target.argv(salmon_args)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise SalmonError(
            "salmon failed (%s):\n%s" % (shlex.join(cmd), output[-3000:])
        )
    return output


def health(target: SalmonTarget) -> str:
    """``salmon health`` - confirms the pod is up and its deps are present."""
    return _run(target, ["health"], timeout=300)


def spectrals(target: SalmonTarget, host_path: str) -> str:
    """Generate spectrals locally, without touching any image host.

    ``--no-upload`` matters: RED forbids posting spectral images to its image
    host outside two specific forum threads, so anything generating spectrals
    for review rather than for an actual upload must not take the upload path.
    """
    return _run(target, ["specs", "--no-upload", target.container_path(host_path)])


def compress(target: SalmonTarget, host_path: str) -> str:
    """Re-compress FLACs to salmon's configured compression level."""
    return _run(target, ["compress", target.container_path(host_path)])


# salmon exposes its checks through `up`, which goes all the way to a tracker.
# For a rip we only want the checks, so call them directly in the pod's Python.
_CHECK_SNIPPET = """
import anyio, json, sys
from salmon.checks.integrity import handle_integrity_check
from salmon.checks.upconverts import test_upconverted
from salmon.checks.logs import check_log_cambia

path = sys.argv[1]
log = sys.argv[2] if len(sys.argv) > 2 else None

async def main():
    print("=== FLAC integrity ===")
    await handle_integrity_check(path)
    print("=== 24-bit upconversion ===")
    print("upconverted:", await test_upconverted(path))
    if log:
        print("=== rip log (cambia) ===")
        await check_log_cambia(log, path)

anyio.run(main)
"""


def run_checks(target: SalmonTarget, host_path: str, host_log: str | None = None) -> str:
    """Run salmon's integrity, upconversion and rip-log checks on a folder."""
    args = ["-c", _CHECK_SNIPPET, target.container_path(host_path)]
    if host_log:
        args.append(target.container_path(host_log))
    if target.mode == "local":
        full = ["python3", *args]
    else:
        full = [
            "kubectl", "-n", target.namespace, "exec", target.deployment, "--",
            "python3", *args,
        ]
    proc = subprocess.run(full, capture_output=True, text=True, timeout=3600)
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise SalmonError("salmon checks failed:\n%s" % output[-3000:])
    return output


class LossyMasterVerdict:
    """Parsed result of ``salmon check lossy --json``."""

    def __init__(self, payload: dict):
        self.raw = payload
        self.tracks = payload.get("tracks", [])
        self.lossy = bool(payload.get("lossy"))
        self.cutoff_hz = payload.get("cutoff_hz")
        self.likely_source = payload.get("likely_source")

    @property
    def summary(self) -> str:
        if not self.tracks:
            return "no audio measured"
        counted = [t for t in self.tracks if not t.get("indeterminate")]
        if self.lossy:
            return (
                "LOSSY MASTER: %d of %d measurable tracks lowpassed at ~%.1f kHz (%s)"
                % (sum(1 for t in counted if _track_is_lossy(t)), len(counted),
                   (self.cutoff_hz or 0) / 1000.0,
                   self.likely_source or "unknown encoder")
            )
        return "no lossy master detected across %d measurable tracks" % len(counted)


def _track_is_lossy(track: dict) -> bool:
    """Read salmon's own per-track verdict.

    salmon serialises this as a real field precisely so callers do not
    re-implement its thresholds and drift out of agreement with it.
    """
    return bool(track.get("lossy"))


def lossy_master(target: SalmonTarget, host_path: str) -> LossyMasterVerdict:
    """Ask salmon whether this release is a lossy master.

    Deliberately not reimplemented here: salmon owns the measurement so that
    salmon, the gomission Bandcamp approver and cdrip all reach one verdict
    rather than three that drift apart.
    """
    import json

    out = _run(target, ["check", "lossy", "--json", target.container_path(host_path)])
    # salmon prints a version banner before command output; the JSON is the
    # last line that actually parses.
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return LossyMasterVerdict(json.loads(line))
    raise SalmonError("no JSON in `salmon check lossy` output:\n%s" % out[-1500:])


_TORRENT_SNIPPET = """
import sys
from salmon.trackers import get_class
from salmon.uploader.upload import generate_torrent

tracker, path = sys.argv[1], sys.argv[2]
site = get_class(tracker)()
tpath, t = generate_torrent(site, path)
print("TORRENT:", tpath)
print("INFOHASH:", t.infohash)
print("SIZE:", t.size)
print("PIECES:", t.pieces, "of", t.piece_size)
"""


def make_torrent(target: SalmonTarget, host_path: str, tracker: str = "RED") -> str:
    """Create a .torrent with salmon's own announce URL, source tag and piece sizing.

    This produces exactly the torrent salmon's upload path would, without
    uploading anything - the file lands in salmon's configured dottorrents_dir.
    """
    if target.mode == "local":
        full = ["python3", "-c", _TORRENT_SNIPPET, tracker, target.container_path(host_path)]
    else:
        full = [
            "kubectl", "-n", target.namespace, "exec", target.deployment, "--",
            "python3", "-c", _TORRENT_SNIPPET, tracker,
            target.container_path(host_path),
        ]
    proc = subprocess.run(full, capture_output=True, text=True, timeout=1800)
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise SalmonError("torrent generation failed:\n%s" % output[-3000:])
    return output
