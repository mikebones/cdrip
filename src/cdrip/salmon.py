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

    @property
    def library_is_reachable(self) -> bool:
        """Whether the library can be written from the ripping machine.

        False when host_root is unset, which is the real case on a Windows
        ripping host: the library lives on the cluster's NFS server and the
        share exports the parent but enumerates the library itself as empty,
        so there is no local path to copy into. The bytes have to go through
        the pod instead - see publish().
        """
        return bool(self.host_root)

    def container_path(self, host_path: str) -> str:
        # Already a path inside the container - what publish() returns. Pass it
        # through rather than demanding a host path that may not exist here.
        croot = _norm(self.container_root) if self.container_root else ""
        cand = _norm(host_path)
        if croot and (cand == croot or cand.startswith(croot.rstrip("/") + "/")):
            return cand
        if not self.host_root:
            raise SalmonError(
                "host_root is unset, so %s cannot be mapped into the "
                "container. Publish the release with publish() first and use "
                "the container path it returns." % host_path
            )
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


def publish(target: SalmonTarget, host_path: str, timeout: int = 3600) -> str:
    """Put a finished release into salmon's library and return its path there.

    ``adopt`` previously assumed the library was an ordinary directory on the
    ripping machine and used shutil.copytree. That holds when the ripper is
    also the NFS server, and fails completely on a Windows ripping host: the
    server exports the parent, but the library directory itself enumerates as
    empty from Windows and the UNC path to the library does not resolve
    at all. There is no local path to copy into, so the bytes go in through
    the pod as a tar stream.

    Not ``kubectl cp``: under MSYS the pod-side path is rewritten to a Windows
    path and kubectl rejects the result outright ("one of src or dest must be
    a local file specification").

    The copy is verified by re-reading the directory out of the container and
    comparing names and sizes. tar's exit status is not taken as evidence -
    the same reason seeding is confirmed against the client rather than
    against salmon's exit code.
    """
    if target.mode == "local":
        raise SalmonError("publish() is for a containerised salmon; "
                          "mode is 'local', so copy the directory normally.")
    name = os.path.basename(os.path.normpath(host_path))
    parent = os.path.dirname(os.path.normpath(host_path)) or "."
    dest = posixpath.join(target.container_root, name)

    local = {
        n: os.path.getsize(os.path.join(host_path, n))
        for n in os.listdir(host_path)
        if os.path.isfile(os.path.join(host_path, n))
    }
    if not local:
        raise SalmonError("%s has no files to publish" % host_path)

    _run_tar_into_pod(target, parent, name, timeout=timeout)

    # tar carries the ripping host's uid and 0644 across. Transmission seeds
    # these files as a different uid, and unreadable library files have caused
    # a real failed-import incident here before, so this is made explicit
    # rather than left to the nightly permissions job.
    subprocess.run(
        ["kubectl", "-n", target.namespace, "exec", target.deployment,
         "--", "chmod", "-R", "a+rX", dest],
        capture_output=True, text=True, timeout=300,
    )

    remote = _remote_listing(target, dest, timeout=300)
    mismatch = [
        "%s: %s here, %s there" % (n, local[n], remote.get(n, "absent"))
        for n in sorted(local) if remote.get(n) != local[n]
    ]
    if mismatch:
        raise SalmonError(
            "published copy does not match the source:\n  %s"
            % "\n  ".join(mismatch))
    return dest


def _run_tar_into_pod(target: SalmonTarget, parent: str, name: str,
                      timeout: int = 3600) -> None:
    """Stream one directory into the container's library as a tar.

    Separated from publish() so the verification logic is testable without a
    cluster: the part worth testing is what happens when the copy does NOT
    match, and that must not require a real transfer to reach.
    """
    tar = subprocess.Popen(
        ["tar", "-cf", "-", "-C", parent, name], stdout=subprocess.PIPE)
    extract = subprocess.run(
        ["kubectl", "-n", target.namespace, "exec", "-i", target.deployment,
         "--", "tar", "-xf", "-", "-C", target.container_root],
        stdin=tar.stdout, capture_output=True, text=True, timeout=timeout,
    )
    if tar.stdout:
        tar.stdout.close()
    tar.wait()
    if tar.returncode != 0:
        raise SalmonError("tar failed reading %s/%s" % (parent, name))
    if extract.returncode != 0:
        raise SalmonError("extract into the pod failed:\n%s"
                          % (extract.stderr or "")[-2000:])



_LISTING_SNIPPET = """
import json, os, sys
d = sys.argv[1]
print(json.dumps({n: os.path.getsize(os.path.join(d, n))
                  for n in os.listdir(d)
                  if os.path.isfile(os.path.join(d, n))}))
"""


def _remote_listing(target: SalmonTarget, container_dir: str,
                    timeout: int = 300) -> dict[str, int]:
    """Name -> size for every file in a directory inside the container.

    Done in Python rather than by parsing `ls`: the release name contains
    spaces and brackets, and a glob in the shell would treat "[CD FLAC]" as a
    character class - a real mistake made while checking this by hand.
    """
    import json

    proc = subprocess.run(
        ["kubectl", "-n", target.namespace, "exec", target.deployment, "--",
         "python3", "-c", _LISTING_SNIPPET, container_dir],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise SalmonError("could not list %s in the pod:\n%s"
                          % (container_dir, (proc.stderr or "")[-1000:]))
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise SalmonError("no listing returned for %s" % container_dir)
