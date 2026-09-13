"""Finding ``flac`` and ``metaflac``, including where Windows hides them.

Every module here shelled out to a bare ``"metaflac"``, which works on Linux
where it is packaged, and raises ``FileNotFoundError`` on Windows where it is
not. That is the machine that has to run EAC, so the post-rip half of the
pipeline - tag checks, recompression checks, MD5s - died on exactly the
platform it was needed on.

EAC ships both binaries in a ``FLAC`` subdirectory of its own install, so on a
machine set up to rip they are already present, just not on ``PATH``. Look
there before giving up, and when giving up, say where it looked.
"""

from __future__ import annotations

import functools
import os
import shutil

EAC_DIRS = (
    r"C:\Program Files (x86)\Exact Audio Copy",
    r"C:\Program Files\Exact Audio Copy",
)

# Other common Windows homes for a standalone FLAC install.
EXTRA_DIRS = (
    r"C:\Program Files\FLAC",
    r"C:\Program Files (x86)\FLAC",
)


class FlacToolMissing(RuntimeError):
    pass


def _candidates(name: str) -> list[str]:
    exe = name if os.name != "nt" else name + ".exe"
    out: list[str] = []
    for base in EAC_DIRS:
        out.append(os.path.join(base, "FLAC", exe))
        out.append(os.path.join(base, exe))
    for base in EXTRA_DIRS:
        out.append(os.path.join(base, exe))
    return out


@functools.lru_cache(maxsize=8)
def find(name: str) -> str | None:
    """Absolute path to ``flac``/``metaflac``, or None."""
    found = shutil.which(name)
    if found:
        return found
    for candidate in _candidates(name):
        if os.path.isfile(candidate):
            return candidate
    return None


def require(name: str) -> str:
    """Path to the tool, or an error that says where it was looked for."""
    found = find(name)
    if found:
        return found
    raise FlacToolMissing(
        "%s not found. It is not on PATH, and not in any of: %s. It ships "
        "with EAC (in its FLAC subdirectory) and with the flac package on "
        "Linux." % (name, ", ".join(_candidates(name)[:3]))
    )


def available(name: str) -> bool:
    return find(name) is not None
