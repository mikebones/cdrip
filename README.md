# cdrip

Rip an audio CD to verified FLAC, then hand it to
[smoked-salmon](https://github.com/smokin-salmon/smoked-salmon).

`cdrip` deliberately does **only** the parts salmon does not: driving the
optical drive, and getting a disc ID into MusicBrainz. Everything after the
disc read — rip-log validation, FLAC integrity, upconversion and MQA
detection, spectrals, torrent creation — is salmon's, and is called rather
than re-implemented.

## Why this exists

Three things kept going wrong by hand:

**Mixed-mode discs hide their music.** A CD with audio tracks followed by a
data track shows up in Windows Explorer (and a naive `mount`) as a disc full
of files. The audio programme is invisible. `cdrip` reads the real TOC, so a
disc that looks like a CD-ROM of MP3s is correctly identified as six Red Book
tracks plus a data session.

**Unknown disc IDs produce untagged rips.** whipper matches on disc ID *only*
— passing `--release` just disambiguates among disc-ID hits, so it does not
help when MusicBrainz has never seen the disc. That is the normal case for
promos and small-label pressings. `cdrip submit-discid` attaches the disc ID
to the right release, verifying track lengths against the TOC first, so future
rips tag themselves.

**Not every disc is in AccurateRip.** Two different things get called
"verified", and it is worth keeping them apart:

* whipper already does **test-and-copy per track** — each track is read twice
  and both CRCs go in the log. Equal CRCs mean the read is *repeatable* on this
  drive, with this disc, in this session.
* **AccurateRip** means the read is *correct* — it agrees with other people's
  rips, on other drives. A consistent offset error, or a drive that mis-reads
  the same sector identically twice, sails through test-and-copy.

With no AccurateRip entry there is nothing to agree with. `cdrip` then rips the
whole disc a second time and compares each track's decoded-audio MD5. A full
second pass adds a fresh TOC read, spin-up and seek pattern, so it catches
session-level problems a per-track re-read inside one pass can miss. It is
weaker than AccurateRip and stronger than nothing — a cross-check, not the
primary evidence, when the log already shows Test CRC == Copy CRC throughout.

The comparison uses the FLAC-stored MD5 of the decoded audio, so tags do not
affect it: an untagged pass and a tagged pass of the same disc compare equal.

## Install

```sh
apt install whipper flac sox cd-paranoia libcdio-utils cdrdao python3-libdiscid
pip install -e .
```

## Use

```sh
cdrip info                    # TOC, disc ID, drive offset, MusicBrainz status
cdrip submit-discid --artist "..." --album "..."
cdrip rip --name "Artist - Album (2007) [CD FLAC]"
cdrip finish /path/to/existing/rip    # just the salmon stages
```

## What a rip actually does

1. **Read the TOC** (`cd-paranoia -Q`) — audio tracks only, and fast. `cd-info`
   is used just to locate a trailing data track; it is avoided otherwise
   because its full disc-mode analysis stalls for minutes on mixed-mode discs.
2. **Compute the disc ID** via libdiscid, including the mixed-mode rule that
   the lead-out is the data track's start minus 11400 frames.
3. **Look up MusicBrainz.** Known disc ID → whipper tags during the rip.
   Unknown → optionally tag afterwards from a `--release` whose track lengths
   are verified against the TOC.
4. **Resolve the read offset** from AccurateRip's public drive database,
   preferring the row with the most submissions.
5. **Check the drive cache** via `whipper drive analyze`. If the cache cannot
   be defeated, re-reads are not independent and the rip cannot be verified;
   `cdrip` refuses without `--force`.
6. **Rip** with whipper, twice when the disc is not AccurateRip-verifiable, and
   compare decoded-audio MD5s track by track.

   whipper **ejects the disc when a rip finishes**, so the second pass starts on
   an empty drive. Left alone it fails as a bare `FileNotFoundError` from
   cdrdao's TOC reader, which says nothing about the real cause. `cdrip` detects
   the empty drive, tries `eject -t`, and — many slim USB and slot-loading
   drives cannot close their own tray — otherwise asks and waits.
7. **Tag in place** — never rename. whipper's `.cue` and `.log` reference the
   audio filenames, and renaming afterwards silently invalidates both, which a
   tracker's log checker will reject. If you want proper filenames, attach the
   disc ID to MusicBrainz and re-rip so whipper gets it right at source.
8. **Hand to salmon** for checks, spectrals and the torrent.
9. **Check for a lossy master** — see below.

## Lossless rip vs lossy master

These are different questions and a release can fail the second while passing
the first:

* the **rip** is lossless when the FLAC is a bit-exact copy of the disc's PCM
  (whipper's Test/Copy CRCs, salmon's CRC-vs-log check);
* the **master** is lossy when whoever made the disc encoded to MP3 or AAC
  somewhere upstream. A perfect rip of that disc is still a perfect rip, and
  the audio is still already degraded.

Nothing in the rip chain catches the second case, and salmon does not either —
`specs --no-upload` generates images for a human and answers "not lossy
mastered" non-interactively. Eyeballing the spectrogram is unreliable too: a
natural rolloff and a codec lowpass look alike at a glance, and transient
smearing can suggest content above the cutoff that is not really there.

So measure it instead, requiring two signatures together:

1. a **cliff** — the level falls 15 dB or more across the kHz leading up to the
   cutoff, where a natural master rolls off at 1–2 dB per kHz; and
2. a **flat floor** above it — successive bands at the same level, meaning
   what remains is dither noise, not signal.

Either alone is weak; a quiet track can look cliff-like and a dull master can
sit near the floor. The floor is measured per track rather than assumed, since
it moves with bit depth and level, and a track too quiet to measure is reported
as indeterminate rather than guessed at. A release is called lossy only when a
majority of measurable tracks agree.

The measurement itself lives in smoked-salmon as `salmon check lossy`
(`--json` for machine callers); `cdrip` invokes it rather than keeping a second
copy, so anything else driving salmon reaches the same verdict.

## Overlap with smoked-salmon

Already in salmon, and called from here rather than rebuilt:

| Stage | salmon |
|---|---|
| Rip-log validation | `checks/logs.py` → `check_log_cambia` (cambia, the parser RED uses) |
| Rip verification | same file — recomputes each track's CRC32 from the audio and compares it to the log |
| FLAC integrity | `checks/integrity.py` |
| Upconversion / MQA | `checks/upconverts.py`, `checks/mqa.py` |
| Spectrals | `salmon specs --no-upload` |
| MusicBrainz tagging | `sources/musicbrainz.py` |
| Torrent creation | `uploader/upload.py` → `generate_torrent` |
| Lossy-master check | `checks/lossy_master.py` → `salmon check lossy` |

Not in salmon, and therefore here: drive offset and cache handling, the rip
itself, mixed-mode TOC reading, disc-ID submission, and double-rip comparison.

`salmon up` is intentionally **not** used: it has no dry-run and runs straight
through to the tracker. `cdrip` calls the individual stages so a rip can be
produced and inspected without uploading anything.

## Configuration

`~/.config/cdrip/config.toml`:

```toml
device = "/dev/cdrom"
staging_dir = "/srv/rips/staging"
library_dir = "/srv/music/library"
double_rip_when_unverifiable = true

[salmon]
mode = "local"                        # or "kubectl", if salmon runs in a pod
# Only used when mode = "kubectl":
# namespace = "media"
# deployment = "deploy/smoked-salmon"
# Translate a path on this machine into the path salmon sees. Identical when
# salmon runs locally; different when it runs in a container with the library
# mounted somewhere else.
host_root = "/srv/music/library"
container_root = "/srv/music/library"
tracker = "RED"

[musicbrainz]
# No web-service endpoint exists for attaching a disc ID, so submission drives
# the HTML form with a logged-in session cookie. Same shape as the RED/OPS
# session cookies. Leave empty to be handed the URL to click instead.
session_cookie = ""
vault_path = "secret/cdrip/musicbrainz"
```

`host_root`/`container_root` translate a path on the ripping machine into the
path salmon sees inside its container. Set them equal when salmon runs locally.
