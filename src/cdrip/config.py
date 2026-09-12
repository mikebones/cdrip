"""Configuration for cdrip.

Kept deliberately small: the only things that genuinely vary between machines
are which drive to use, where to stage a rip, and how to reach salmon.
Everything else (drive offset, cache behaviour, metadata) is discovered at run
time, because hard-coding any of it is how rips go quietly wrong.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict

CONFIG_PATH = os.environ.get(
    "CDRIP_CONFIG", os.path.expanduser("~/.config/cdrip/config.toml")
)


@dataclass
class SalmonConfig:
    mode: str = "local"
    namespace: str = ""
    deployment: str = "deploy/smoked-salmon"
    host_root: str = ""
    container_root: str = ""
    tracker: str = "RED"


@dataclass
class MusicBrainzConfig:
    # A musicbrainz_server_session cookie from a logged-in browser.  There is
    # no web-service endpoint for attaching a disc ID, so submission has to go
    # through the ordinary HTML form.  Leave empty to be handed the URL instead.
    session_cookie: str = ""
    # Optional: read the cookie from Vault instead of storing it here, matching
    # how the RED/OPS sessions are already handled.
    vault_path: str = ""


@dataclass
class Config:
    device: str = "/dev/cdrom"
    staging_dir: str = ""
    # Where a finished release is moved so salmon can see it.  Must be under
    # SalmonConfig.host_root.
    library_dir: str = ""
    # Rip twice and compare when AccurateRip has no entry for the disc.  This
    # doubles the wall-clock time and is the only verification available for
    # discs AccurateRip has never seen, which is most promos and small presses.
    double_rip_when_unverifiable: bool = True
    salmon: SalmonConfig = field(default_factory=SalmonConfig)
    musicbrainz: MusicBrainzConfig = field(default_factory=MusicBrainzConfig)


def load(path: str | None = None) -> Config:
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        return Config()
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    cfg = Config(
        device=raw.get("device", Config.device),
        staging_dir=raw.get("staging_dir", Config.staging_dir),
        library_dir=raw.get("library_dir", Config.library_dir),
        double_rip_when_unverifiable=raw.get(
            "double_rip_when_unverifiable", Config.double_rip_when_unverifiable
        ),
    )
    if "salmon" in raw:
        cfg.salmon = SalmonConfig(**{**asdict(cfg.salmon), **raw["salmon"]})
    if "musicbrainz" in raw:
        cfg.musicbrainz = MusicBrainzConfig(
            **{**asdict(cfg.musicbrainz), **raw["musicbrainz"]}
        )
    return cfg


def musicbrainz_cookie(cfg: Config) -> str:
    """Resolve the MusicBrainz session cookie, from Vault if configured."""
    if cfg.musicbrainz.session_cookie:
        return cfg.musicbrainz.session_cookie
    if cfg.musicbrainz.vault_path:
        import subprocess

        proc = subprocess.run(
            ["vault", "kv", "get", "-field=session", cfg.musicbrainz.vault_path],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    return ""
