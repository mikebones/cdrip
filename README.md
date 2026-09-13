# cdrip

Everything around ripping a CD to FLAC for a Gazelle-style tracker: driving
Exact Audio Copy, verifying the rip, checking the rules, and handing off to
[smoked-salmon](https://github.com/ligh7s/smoked-salmon) for the upload.

It exists because the interesting failures are not in the ripping. A rip can be
bit-perfect, confirmed by AccurateRip, and still be rejected or marked
trumpable for reasons invisible in the audio: an unsigned log, a filename
scheme that drops a separator, a missing cue sheet, a tag nobody looked at.
Each of those cost a deleted torrent before it was understood. This encodes
what was learned so it does not have to be learned twice.

Nothing here is clever. It is a list of things that turned out to matter, with
the reason attached to each one.

---

## What it does

```
                 cdrip eac-load      MusicBrainz -> naming gate -> EAC
                        |
                 cdrip eac-rip       settings gate -> rip -> wait
                        |
                 cdrip adopt         log check -> rules -> tags -> salmon
                        |
                 cdrip preflight     dupes, release type, tags, edition
```

`adopt` also accepts a rip you made by hand, from EAC, XLD or whipper.

## Quick start

Ripping a disc on Windows with EAC running:

```bash
cdrip eac-load  --artist "A Thousand Times Repent" --album "Virtue Has Few Friends"
cdrip eac-rip   --offset 6 --output-dir 'C:\rips' --apply-settings
cdrip preflight 377594
cdrip adopt     'C:\rips' --tracks 6 --expect-offset 6 \
                --label "Tribunal Records" --catalogue "TRB092"
```

`--offset` is your drive's AccurateRip read offset. There is deliberately no
default: another drive's value produces a rip that looks perfectly clean and is
bit-shifted against every other copy of the disc. `cdrip info` will look it up.

## Commands

| Command | What it does |
| --- | --- |
| `info` | TOC, disc ID, drive offset, MusicBrainz status |
| `submit-discid` | Attach this disc's ID to a MusicBrainz release |
| `eac-load` | Put a verified MusicBrainz tracklist into a running EAC |
| `eac-configure` | Check — or `--apply` — the EAC settings a log checker scores |
| `eac-settings` | Check EAC's saved option profile |
| `eac-rip` | Settings gate, start the test-and-copy rip, wait for it |
| `adopt` | Take a finished rip and run everything after it |
| `check` | Check a finished folder against the formatting rules |
| `preflight` | Ask the tracker what is already in a group |
| `rules` | Which tracker rules the code references, and which it does not |
| `rip` | Drive whipper end to end (see *Which ripper* below) |
| `finish` | Run the salmon stages on an existing rip |

## Install

```bash
git clone https://github.com/mikebones/cdrip
cd cdrip && pip install -e .
```

Python 3.11+, no required dependencies. Optional, depending on what you use:
EAC (Windows, for tracker-grade logs), `flac`/`metaflac`, `sox` for lossy-master
detection, whipper and `cd-paranoia` for the non-Windows rip path.

---

## Which ripper, and why it matters

**RED rejects whipper logs outright.** Its checker identifies a log by its
header; anything it does not recognise scores −1 and is marked trumpable for
"Bad/No Checksum(s)" regardless of how good the rip was. A whipper rip scoring
100 in cambia still fails there, and `whipper-plugin-eaclogger`'s checksum is
explicitly not EAC-compatible — its own source says so.

So for those trackers the ripper has to be EAC, and `eac-*` drives it. `rip`
(whipper) is still the right tool where the log does not have to satisfy a
checker: archiving, or a tracker that accepts it.

## Driving EAC

EAC has no usable automation surface, which is worth stating precisely because
two plausible approaches waste a day:

- **The command line does not rip.** `EAC.exe` accepts `-DRIVE`,
  `-OUTPUTDIRECTORY`, `-TESTANDCOPY`, `-CLOSE` and more. Launched with all of
  them it opens its window and idles — flat CPU, no output after 45 seconds.
  Its documentation only ever describes the crash-workaround switches.
- **UI Automation can see EAC but not touch it.** Its window (class `erstes`)
  exposes 56 descendants, every one a bare `Pane`, none supporting
  InvokePattern, and no MenuBar at all. pywinauto's UIA backend is useless here.
- **Win32 works.** The menus are real `HMENU`s, so they can be walked and
  triggered with `WM_COMMAND`, and the dialogs are ordinary `#32770` windows
  with real controls.

Four things about that layer are not guessable, and each produced a wrong
conclusion before it was understood:

- **`GetWindowText` cannot read a control owned by another process.** It
  returns window captions only, and an edit control has none — so it returns
  `''` for a control plainly full of text. This made successful writes look
  failed and a mangled import look like a no-op. `WM_GETTEXT` is marshalled
  across processes and tells the truth. *Verify with the same mechanism you
  wrote with, never a weaker one.*
- **`TCM_SETCURSEL` does not change a property-sheet page.** It moves the tab
  highlight without sending `TCN_SELCHANGE`, so every tab reads back identical
  and the dialog looks like it has one page repeated. `PSM_SETCURSEL` works.
- **`BM_SETCHECK` does not tell the dialog.** The box changes, the dialog's own
  state does not, and OK writes back the old value. `BM_CLICK` does both.
- **CD reads do not move a process's I/O counters.** They go through SCSI
  passthrough, so a healthy rip shows a zero read delta and near-idle CPU.
  "Nothing is happening" is the wrong conclusion; watch the progress window.

EAC also keeps **no readable configuration anywhere** until a profile is saved
— no INI, nothing under `HKCU\Software` — so a carefully configured EAC loses
everything on exit. `eac-configure` reads the live dialogs; `eac-settings`
reads a saved `.cfg`.

## The settings that decide a log's score

`eac-configure` enforces these, each carrying its reason in the source. Three
are easy to get backwards or miss entirely:

- **`Append checksum to status report`** is **off by default** and sits on a
  tab that is easy to miss. Without it EAC writes no `==== Log checksum ====`
  line, the log cannot be verified, and the torrent is trumpable for "Bad/No
  Checksum(s)" however good the rip was. This single unticked box is what made
  an otherwise flawless rip — all tracks at AccurateRip confidence 2, CTDB
  confirmed, 100% quality, no errors — fail.
- **`Drive caches audio data`** is a statement *about the drive*, not a
  request. Ticking it is what makes EAC defeat the cache.
- **`Delete leading and trailing silent blocks`** must stay **off**. It alters
  the audio, so the rip stops matching AccurateRip and everyone else's copy.

The read offset is checked only when you supply it, and the rest are checked
regardless — an unknown offset must not switch the whole gate off.

## Lossless rip vs lossy master

A CD can be pressed from a lossy source. The rip is then perfectly lossless and
the *master* is not, which trackers require you to report (RED 2.1.2.2).

**Do not eyeball the spectrogram.** A lowpass that looks obvious at one zoom
level is invisible at another, and this was got wrong here once. Measure: band
RMS in 1 kHz slices via `sox … sinc <lo>-<hi> stat`, and require **both** a
sharp cliff (≥15 dB) **and** a flat noise floor above it. One without the other
is not evidence. The implementation lives in the smoked-salmon fork so the
Bandcamp approver can use it too.

## AccurateRip on a mixed-mode disc: absence is not evidence

On a disc with audio tracks followed by a data track, tools disagree about the
lead-out, silently. libdiscid — and so whipper — uses the MusicBrainz rule
(data track start − 11400) because that makes a stable *disc ID*. AccurateRip
uses the **real** lead-out and counts the data track in its CDDB component.

The lookup succeeds, returns nothing, and the ripper honestly reports "not in
the database". On the disc this was written for, whipper reported all six
tracks absent while EAC found every one at **confidence 2**. An empty result on
a mixed-mode disc is not evidence of absence — `cdrip info` says so rather than
quietly falling back to weaker verification.

## Rule coverage

`cdrip rules` diffs the tracker's uploading rules against the source to show
which are actually referenced by a check.

The ruleset itself is **not published here** — it is a private tracker's
members-only wiki, and republishing it verbatim is not ours to do. Put your
own copy at `docs/red-rules.md` (or pass `--doc`), one rule per `##` heading:

```markdown
## 2.2.10.9
Log files must not be edited. …
```

Against the ruleset this was built from:

```
in scope           : 69
referenced in code : 19
NOT referenced     : 50
lossy-only         : 11   (this pipeline makes lossless CD rips)
not mechanical     : 7    (vinyl speed, lineage prose, tracker-side state)
```

This makes a deliberately weak claim in one direction and a strong one in the
other: a rule number in a docstring does not prove the check is any good, but a
rule appearing nowhere is definitely not implemented. It does not grade *how
well* a rule is covered — a number there would be trusted more than it
deserves.

## Known gaps

Listed because a tool that hides its gaps is worse than one that has them.

- **Cue sheets are wired up but EAC's gap detection is not reliable.**
  `eac-rip --cue` runs gap detection and writes a cue (RED 2.2.10.7 — a 100%
  log rip lacking one can be trumped by a rip with even a noncompliant cue).
  On a mixed-mode disc, EAC 1.8 has been seen to die inside gap detection with
  an internal `Gaps.2154 -> INDEX-RANGE` exception, hang on "Track 0", and take
  the process down. It still leaves a cue behind, and that cue looks right —
  correct DISCID, titles and performers — while its pregaps are filler: an
  identical `00:01:00` on several tracks and none on the rest. `cue.check`
  refuses those, because a missing cue is merely trumpable whereas an invented
  one is a false statement about the disc.
- **Gap detection accuracy is not enforced.** It is not scored, but it is
  implicated in the crash above.
- **HTOA** (hidden track one audio, 2.2.10.6) is not handled.
- **`preflight` informs but does not feed the upload.** Release type, tags and
  edition info are still entered by hand — the omission that produced an
  upload with "unknown" release type and tags.
- **The 2.3.x tag and filename checks are thin**: required tags (2.3.16.1),
  combined tags (2.3.18.3), sort order (2.3.14).
- **OPS is unverified.** Same Gazelle shape, but the base URL and release-type
  numbering have not been confirmed.
- **The FLAC command line is 447 of EAC's 500-character limit.** One more
  `-T "FIELD=%value%"` tag truncates it silently.

## Related projects

- **[smoked-salmon](https://github.com/ligh7s/smoked-salmon)** does everything
  after the disc read — tagging, spectrals, torrent creation, uploading. cdrip
  does not duplicate it; it handles what salmon cannot, because salmon never
  touches a disc: mixed-mode TOCs, drive offsets, cache defeat, disc-ID
  submission, driving EAC. The lossy-master and release-rule checks were
  contributed to a fork of salmon rather than kept here, so other salmon
  front-ends get them.
- **[EACEnhancements](https://github.com/metaisfacil/EACEnhancements)** solves
  an overlapping problem from the opposite direction: a C# plugin loaded into
  EAC, extending it from the inside. It covers ground this does not — cue
  sheets in the workflow, HTOA, extra metadata fields in EAC's own window, and
  raising the compressor argument limit from 500 to 1000 characters. It also
  validates EAC's configuration read-only, as `eac-configure` does. If you want
  EAC itself to be better, look there; it is the more direct approach, at the
  cost of relying on memory hacking. This drives EAC from outside and is
  concerned with everything either side of the rip.

## Configuration

`config.toml`, found next to the project or passed with `--config`:

```toml
device = "D:"                # drive letter on Windows, /dev/cdrom elsewhere

[salmon]
mode = "local"               # or "kubectl"
# namespace = "media"
# deployment = "deploy/smoked-salmon"
# Translate a path on this machine into the path salmon sees. Identical when
# salmon runs locally; different when it runs in a container.
# path_map = { "/data/music" = "/library" }

[musicbrainz]
# No web-service endpoint exists for attaching a disc ID, so submission drives
# the HTML form with a logged-in session cookie. Leave empty to be handed the
# URL to click instead.
session_cookie = ""
```

## Development

```bash
pip install -e ".[dev]"
pytest                        # 157 tests, no network, no disc, no EAC
```

Tests do not touch the drive, the network or a running EAC. Where a check
depends on Win32, the pure logic is tested and the platform call is not — and
tests that accidentally reached the live system have been fixed, because a test
whose result depends on whether EAC happens to be open is not a test.
