"""cdrip - everything around a CD rip except, usually, the rip itself.

Two entry points, because which ripper you need depends on where the release
is going.

``adopt`` takes a finished rip - EAC, XLD or whipper - and does everything
after it: validate the log, check the formatting rules, enrich the metadata,
detect a lossy master, hand off to smoked-salmon, make the torrent. This is
the normal path for a tracker that only recognises EAC or XLD logs, because
EAC cannot be driven from here: its command-line switches open the window and
idle without ripping, and its window (class ``erstes``) exposes 56 UI
Automation descendants of which exactly zero support InvokePattern, with no
MenuBar at all. Its menus *are* real HMENUs, so WM_COMMAND can reach them, but
the compressed-rip entries open dialogs - so the rip stays a human step.

``rip`` drives whipper end to end and is still the right tool when the log
does not have to satisfy a tracker's checker: archiving, or somewhere that
accepts whipper. It handles the parts only the disc-side can: mixed-mode TOCs
where the OS shows nothing but a data track, the AccurateRip drive offset,
whether the drive's cache can be defeated, and a second pass when AccurateRip
has never seen the disc.

Either way the rules are the same: never rename after a rip, because the .cue
and .log reference the filenames; and enrich metadata between the rip and the
hand-off, because whipper writes tags during the rip and salmon reads them
afterwards.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import compliance as compliance_mod
from . import config as config_mod
from . import drive as drive_mod
from . import eac as eac_mod
from . import eacprofile as eacprofile_mod
from . import enrich as enrich_mod
from . import musicbrainz as mb
from . import rip as rip_mod
from . import riplog as riplog_mod
from . import salmon as salmon_mod
from . import tagging
from . import toc as toc_mod


def _echo(msg: str = "") -> None:
    print(msg, flush=True)


def _section(title: str) -> None:
    _echo()
    _echo("== %s" % title)


def _describe_toc(toc: toc_mod.Toc, disc_id: str, submit_url: str) -> None:
    _section("Disc")
    for track in toc.tracks:
        _echo(
            "  %-2d %-5s LBA %-7d %s"
            % (track.number, "DATA" if track.is_data else "audio",
               track.start_lba, track.duration if not track.is_data else "")
        )
    _echo("  %d audio tracks, %s total" % (len(toc.audio_tracks), toc.total_duration))
    if toc.is_mixed_mode:
        _echo("  mixed-mode disc: a data track follows the audio programme.")
        _echo("  (This is why the OS may show only files and no music.)")
        _echo("  NOTE: an empty AccurateRip result on this disc is NOT conclusive -")
        _echo("  AccurateRip keys on the real lead-out and counts the data track,")
        _echo("  so a ripper using the MusicBrainz mixed-mode rule asks about a")
        _echo("  different disc. Verified case: whipper said all tracks absent")
        _echo("  while EAC found every one at confidence 2.")
    _echo("  disc ID: %s" % disc_id)
    _echo("  submit : %s" % submit_url)


def _load_toc(args) -> tuple[toc_mod.Toc, str, str]:
    toc = toc_mod.read_toc(args.device)
    disc_id, submit_url = toc_mod.compute_discid(toc)
    return toc, disc_id, submit_url


def cmd_info(args, cfg) -> int:
    toc, disc_id, submit_url = _load_toc(args)
    _describe_toc(toc, disc_id, submit_url)

    _section("Drive")
    try:
        drives = drive_mod.detect_drives()
        for d in drives:
            _echo("  %s  %s (%s)" % (d.device, d.label, d.release))
        target = next((d for d in drives if d.device == args.device), drives[0])
        offset = drive_mod.lookup_offset(target)
        _echo("  read offset: %s" % ("%+d" % offset if offset is not None else "UNKNOWN"))
        cache = drive_mod.read_cached_cache_flag(target.device)
        _echo("  cache defeatable: %s" % ("unknown - run 'cdrip rip'" if cache is None else cache))
    except drive_mod.DriveError as exc:
        _echo("  %s" % exc)

    _section("MusicBrainz")
    found = mb.lookup_discid(disc_id)
    if found:
        for release in found.get("releases", []):
            _echo("  KNOWN: %s (%s) %s" % (
                release.get("title"), release.get("date"), release.get("id")))
    else:
        _echo("  disc ID not in MusicBrainz.")
        _echo("  whipper matches on disc ID only, so a rip now would be untagged.")
        _echo("  Attach it with:  cdrip submit-discid --artist ... --album ...")
    return 0


def cmd_submit_discid(args, cfg) -> int:
    toc, disc_id, submit_url = _load_toc(args)
    _describe_toc(toc, disc_id, submit_url)

    if mb.lookup_discid(disc_id):
        _echo("\nDisc ID is already attached to a release; nothing to do.")
        return 0

    lengths = [t.length_frames for t in toc.audio_tracks]
    release_mbid = args.release
    if not release_mbid:
        if not (args.artist and args.album):
            _echo("\nNeed either --release <mbid>, or --artist and --album to search.")
            return 2
        _section("Searching MusicBrainz")
        matches = mb.find_match(args.artist, args.album, lengths)
        if not matches:
            _echo("  No release matched this disc's track lengths.")
            _echo("  Attach it by hand: %s" % submit_url)
            return 1
        for m in matches:
            _echo("  %s" % m.summary)
            _echo("      %s/release/%s" % (mb.BASE, m.mbid))
        if len(matches) > 1 and not args.yes:
            _echo("\nMore than one release matched; re-run with --release <mbid>.")
            return 1
        release_mbid = matches[0].mbid

    toc_param = "+".join(
        str(v) for v in [1, len(lengths), toc.discid_leadout, *toc.discid_offsets]
    )
    cookie = config_mod.musicbrainz_cookie(cfg)
    if not cookie:
        _echo("\nNo MusicBrainz session cookie configured.")
        _echo("There is no API for attaching a disc ID, so open this and pick the release:")
        _echo("  %s&release=%s" % (submit_url, release_mbid))
        return 1
    try:
        url = mb.attach_discid(disc_id, toc_param, len(lengths), release_mbid, cookie)
    except mb.MusicBrainzError as exc:
        _echo("\nAutomatic submission failed: %s" % exc)
        _echo("Submit manually: %s&release=%s" % (submit_url, release_mbid))
        return 1
    _echo("\nAttached. %s" % url)
    return 0



def _metadata_findings(discid_result: dict) -> list:
    """Run the pre-rip naming check over the release MusicBrainz returned."""
    releases = discid_result.get("releases") or []
    if not releases:
        return []
    rel = releases[0]
    titles = {}
    for medium in rel.get("media") or []:
        for track in medium.get("tracks") or []:
            titles[int(track["position"])] = track.get("title", "")
    artist = ", ".join(
        a["artist"]["name"] for a in rel.get("artist-credit", []) if "artist" in a
    )
    return compliance_mod.check_metadata(titles, album=rel.get("title", ""), artist=artist)


def cmd_rip(args, cfg) -> int:
    toc, disc_id, submit_url = _load_toc(args)
    _describe_toc(toc, disc_id, submit_url)

    _section("Drive")
    drives = drive_mod.detect_drives()
    target = next((d for d in drives if d.device == args.device), drives[0])
    _echo("  %s  %s" % (target.device, target.label))

    offset = args.offset if args.offset is not None else drive_mod.lookup_offset(target)
    if offset is None:
        _echo("  Read offset unknown for this drive and none given.")
        _echo("  Pass --offset, or look it up at %s" % drive_mod.ACCURATERIP_OFFSET_URL)
        return 2
    _echo("  read offset: %+d" % offset)

    cache = drive_mod.read_cached_cache_flag(target.device)
    if cache is None:
        _echo("  analysing drive cache (one-off)...")
        cache = drive_mod.analyze_cache(target.device)
    _echo("  cache defeatable: %s" % cache)
    if cache is False and not args.force:
        _echo("  This drive cannot defeat its audio cache, so re-reads are not")
        _echo("  independent and the rip cannot be verified. Use --force to proceed.")
        return 2

    _section("MusicBrainz")
    known = mb.lookup_discid(disc_id)
    release = None
    if known:
        _echo("  disc ID known; whipper will tag from MusicBrainz directly.")
        # Check the titles whipper is about to turn into filenames, BEFORE
        # spending fifteen minutes ripping. Once the .cue and .log exist they
        # reference those names, and editing a rip log is forbidden - so a bad
        # character here means correcting MusicBrainz and ripping again.
        findings = _metadata_findings(known)
        if findings:
            _echo("")
            for f in findings:
                _echo("  %s" % f)
            _echo("")
            if not args.ignore_naming:
                _echo("  Fix these in MusicBrainz first, then re-run - renaming after")
                _echo("  the rip would invalidate the .cue and .log. Use")
                _echo("  --ignore-naming to rip anyway.")
                return 2
    else:
        _echo("  disc ID unknown - whipper will rip untagged.")
        if args.release:
            release = mb.get_release(args.release)
            lengths = [t.length_frames for t in toc.audio_tracks]
            delta = mb.verify_against_toc(release, lengths)
            if delta is None:
                _echo("  --release does not match this disc's track lengths. Aborting.")
                return 2
            _echo("  will tag from %s after the rip (max delta %+d frames)"
                  % (args.release, delta))
            _echo("  Consider 'cdrip submit-discid' so future rips tag themselves.")

    name = args.name or (
        "%s - %s" % (
            ", ".join(a["artist"]["name"] for a in release.get("artist-credit", [])),
            release.get("title"),
        ) if release else "Unknown Disc %s" % disc_id[:8]
    )
    base = os.path.join(cfg.staging_dir, name)

    _section("Ripping")
    _echo("  into %s" % base)
    double = cfg.double_rip_when_unverifiable and not args.no_double
    if double:
        _echo("  ripping twice and comparing (this disc cannot be AccurateRip-verified)")
        first, comparisons = rip_mod.double_rip(
            base, offset, name, device=target.device, unknown=not known,
            logger=args.logger,
            notify=lambda msg: _echo("  %s" % msg),
        )
        _section("Verification")
        bad = [c for c in comparisons if not c.matches]
        for c in comparisons:
            _echo("  %-10s %s  %s" % (c.name, "OK  " if c.matches else "DIFF", c.md5_a))
        if bad:
            _echo("  %d track(s) differed between passes - the rip is NOT trustworthy."
                  % len(bad))
            return 1
        _echo("  all %d tracks identical across two independent reads." % len(comparisons))
        if not args.keep_second:
            shutil.rmtree(os.path.join(base, "pass2"), ignore_errors=True)
        result = first
    else:
        result = rip_mod.rip(
            os.path.join(base, "pass1"), offset, name,
            device=target.device, unknown=not known, logger=args.logger,
        )

    if release is not None:
        _section("Tagging")
        for path in tagging.apply(result.directory, release):
            _echo("  tagged %s" % os.path.basename(path))
        _echo("  (files deliberately not renamed - the .cue and .log reference them)")

    # Enrichment goes HERE: after the rip, before the hand-off. whipper writes
    # tags from MusicBrainz during the rip, so anything set earlier is
    # overwritten; and salmon builds its upload payload by reading these
    # fields, so they must exist before it runs. Nothing is renamed, so the
    # .cue and .log stay valid.
    if not args.no_enrich:
        _section("Metadata enrichment")
        meta = enrich_mod.collect(
            result.directory,
            label=args.label,
            catalogue=args.catalogue,
            genres=list(args.genre) if args.genre else None,
            discogs_release=args.discogs_release,
        )
        if meta.is_empty:
            _echo("  nothing found - no label, catalogue number or genres.")
            _echo("  MusicBrainz frequently has none of these. Pass --label/")
            _echo("  --catalogue/--genre, or --discogs-release <id>.")
        else:
            _echo("  label     : %s" % (meta.label or "-"))
            _echo("  catalogue : %s" % (meta.catalogue or "-"))
            _echo("  genres    : %s" % (", ".join(meta.genres) or "-"))
            _echo("  sources   : %s" % ", ".join(meta.sources))
            written = enrich_mod.apply(result.directory, meta)
            _echo("  written to %d files (no renames)" % len(written))

    if args.skip_salmon:
        _echo("\nDone: %s" % result.directory)
        return 0
    return _handoff(cfg, result.directory, result.log_path, args)


def _release_name(directory: str) -> str:
    """The name the release folder should have in salmon's library.

    A double rip leaves the audio in ``<release>/pass1``, so the release name
    is the parent's; anything else is named after itself.
    """
    directory = directory.replace("\\", "/").rstrip("/")
    base = os.path.basename(directory)
    if base in ("pass1", "pass2"):
        return os.path.basename(os.path.dirname(directory))
    return base


def _handoff(cfg, directory: str, log_path: str | None, args) -> int:
    target = salmon_mod.SalmonTarget(
        mode=cfg.salmon.mode,
        namespace=cfg.salmon.namespace,
        deployment=cfg.salmon.deployment,
        host_root=cfg.salmon.host_root,
        container_root=cfg.salmon.container_root,
    )

    final = directory
    if not os.path.realpath(directory).startswith(os.path.realpath(cfg.library_dir)):
        final = os.path.join(cfg.library_dir, _release_name(directory))
        _section("Publishing to salmon's library")
        _echo("  %s -> %s" % (directory, final))
        os.makedirs(os.path.dirname(final), exist_ok=True)
        shutil.copytree(directory, final, dirs_exist_ok=True)
        if log_path:
            log_path = os.path.join(final, os.path.basename(log_path))

    _section("Formatting rules")
    rule_findings = compliance_mod.check_release(final)
    for f in rule_findings:
        _echo("  %s" % f)
    _echo("  %s" % compliance_mod.summarise(rule_findings))
    fixable = [f for f in rule_findings if f.fixable_in_place]
    if any(f.rule == "2.2.10.10" for f in fixable):
        _section("Recompressing to level 8")
        _echo(salmon_mod.compress(target, final))

    _section("smoked-salmon checks")
    _echo(salmon_mod.run_checks(target, final, log_path))

    # salmon renders spectral images but deliberately does not judge them
    # (specs --no-upload auto-answers "not lossy mastered"), so this is a
    # separate call. The measurement lives in salmon rather than here so that
    # salmon, the gomission Bandcamp approver and cdrip share one verdict.
    _section("Lossy master check")
    verdict = salmon_mod.lossy_master(target, final)
    for track in verdict.tracks:
        name = os.path.basename(track.get("filepath", ""))[:44]
        if track.get("indeterminate"):
            state = "indeterminate (too quiet to measure)"
        elif salmon_mod._track_is_lossy(track):
            state = "LOSSY - %.1f kHz lowpass, %.1f dB cliff" % (
                (track.get("cutoff_hz") or 0) / 1000.0, track.get("cliff_db", 0))
        else:
            state = "clean"
        _echo("  %-44s %s" % (name, state))
    _echo("")
    _echo("  %s" % verdict.summary)
    if verdict.lossy:
        _echo("")
        _echo("  This disc was pressed from a lossy source. The rip itself is")
        _echo("  still exact - but the audio was degraded before pressing.")
        _echo("  On RED this is allowed (2.1.2.2, official lossy-mastered")
        _echo("  releases are not transcodes) but it must be reported for")
        _echo("  lossy master approval, not presented as a clean rip.")

    _section("Spectrals")
    _echo(salmon_mod.spectrals(target, final))

    if not args.no_torrent:
        _section("Torrent")
        _echo(salmon_mod.make_torrent(target, final, cfg.salmon.tracker))

    _echo("\nDone: %s" % final)
    return 0


def cmd_adopt(args, cfg) -> int:
    """Take a finished rip - from EAC, XLD or whipper - and do everything else.

    This is the main entry point when the rip itself was not made here. EAC
    cannot be driven headlessly (its CLI switches do not rip, and its window
    exposes no actionable UI Automation controls), so for trackers that only
    recognise EAC logs the rip is a human step and this picks up afterwards.

    Order matters: validate the log first and stop on a blocker, because
    everything downstream - spectrals, torrent, upload - is wasted effort on a
    rip that will be rejected.
    """
    path = args.path
    _section("Rip log")
    log = riplog_mod.find_log(path)
    if not log:
        _echo("  no log in the release folder.")
    else:
        _echo("  %s" % os.path.basename(log))
        facts = eac_mod.read_log(log)
        _echo("  ripper        : %s" % (facts.ripper or "unknown"))
        _echo("  read offset   : %s" % ("%+d" % facts.read_offset if facts.read_offset is not None else "-"))
        _echo("  read mode     : %s" % (facts.read_mode or "-"))
        _echo("  cache defeated: %s" % facts.cache_defeated)
        _echo("  has checksum  : %s" % facts.has_checksum)
        if facts.test_copy_pairs:
            _echo("  test/copy CRCs: %d pairs, %s" % (
                len(facts.test_copy_pairs),
                "all match" if facts.crcs_match else "MISMATCH"))

        problems = eac_mod.check_settings(facts, expected_offset=args.expect_offset)
        for p in problems:
            _echo("  ! %s" % p)

        # EAC's own checker is the authority on whether a log will be accepted.
        try:
            verdict = eac_mod.check_log(log, checklog=args.checklog)
            _echo("  CheckLog      : %s" % verdict.summary)
            if not verdict.ok and not args.ignore_log:
                _echo("")
                _echo("  This log will not be accepted. Fix the rip rather than")
                _echo("  uploading and reporting for manual review; use")
                _echo("  --ignore-log to continue anyway.")
                return 1
        except FileNotFoundError as exc:
            _echo("  CheckLog      : unavailable (%s)" % exc)

    _section("Formatting rules")
    findings = compliance_mod.check_release(path, expect_tracks=args.tracks)
    for f in findings:
        _echo("  %s" % f)
    _echo("  %s" % compliance_mod.summarise(findings))
    if compliance_mod.blockers(findings) and not args.ignore_rules:
        _echo("  Blockers present; use --ignore-rules to continue anyway.")
        return 1

    if not args.no_enrich:
        _section("Metadata enrichment")
        meta = enrich_mod.collect(
            path, label=args.label, catalogue=args.catalogue,
            genres=list(args.genre) if args.genre else None,
            discogs_release=args.discogs_release,
        )
        if meta.is_empty:
            _echo("  nothing found - pass --label/--catalogue/--genre or --discogs-release.")
        else:
            _echo("  label     : %s" % (meta.label or "-"))
            _echo("  catalogue : %s" % (meta.catalogue or "-"))
            _echo("  genres    : %s" % (", ".join(meta.genres) or "-"))
            _echo("  sources   : %s" % ", ".join(meta.sources))
            _echo("  written to %d files (no renames)" % len(enrich_mod.apply(path, meta)))

    if args.no_salmon:
        _echo("")
        _echo("Done (stopped before salmon): %s" % path)
        return 0
    return _handoff(cfg, path, log, args)


def cmd_check(args, cfg) -> int:
    """Check a finished release folder against the formatting rules."""
    findings = compliance_mod.check_release(args.path)
    for f in findings:
        _echo("  %s" % f)

    # The log answers two things the files cannot: what kind of disc this was
    # (2.2.10.1) and whether it carried pre-emphasis (2.1.21).
    log_findings = riplog_mod.check_log(args.path)
    for rule, severity, message in log_findings:
        mark = {"blocker": "BLOCK", "trumpable": "TRUMP", "info": "info "}[severity]
        _echo("  %s %-10s %s" % (mark, rule, message))

    _echo("")
    _echo(compliance_mod.summarise(findings))
    blocked = compliance_mod.blockers(findings) or [
        f for f in log_findings if f[1] == "blocker"
    ]
    return 1 if blocked else 0


def cmd_finish(args, cfg) -> int:
    """Run the salmon stages against an already-ripped folder."""
    result = rip_mod.collect(args.path)
    return _handoff(cfg, args.path, result.log_path, args)


def cmd_eac_settings(args, cfg) -> int:
    """Check EAC's saved option profile before spending a rip on it.

    EAC keeps no readable settings until a profile is saved, and two of its
    stock values produce a non-compliant release: the filename scheme appends
    the track artist and omits the ' - ' separator, and FLAC is called at -6.
    Neither is visible in the audio afterwards and both need a re-rip to fix,
    so this is a pre-rip gate.
    """
    paths = [args.profile] if args.profile else eacprofile_mod.find_profiles()
    if not paths:
        _echo("No saved EAC profile found in %s" % eacprofile_mod.DEFAULT_PROFILE_DIR)
        _echo("EAC stores settings only in memory until you save one "
              "(EAC / Profiles / Save Profile...), so there is nothing to "
              "check and nothing that will survive EAC exiting.")
        return 1

    failed = False
    for path in paths:
        _section(os.path.basename(path))
        profile = eacprofile_mod.read(path)
        _echo(eacprofile_mod.describe(profile))
        _echo("")
        problems = eacprofile_mod.check(profile)
        for problem in problems:
            _echo("  PROBLEM %s" % problem)
        if problems:
            failed = True
        else:
            _echo("  settings are compliant")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cdrip", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-d", "--device", default=None, help="optical device")
    parser.add_argument("--config", default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("info", help="show the TOC, disc ID and MusicBrainz status")
    p_info.set_defaults(func=cmd_info)

    p_sub = sub.add_parser("submit-discid",
                           help="attach this disc's ID to a MusicBrainz release")
    p_sub.add_argument("--release", help="MusicBrainz release MBID")
    p_sub.add_argument("--artist", help="artist name, to search for the release")
    p_sub.add_argument("--album", help="album title, to search for the release")
    p_sub.add_argument("--yes", action="store_true",
                       help="accept the best match without confirming")
    p_sub.set_defaults(func=cmd_submit_discid)

    p_rip = sub.add_parser("rip", help="rip, verify, tag and hand to salmon")
    p_rip.add_argument("--name", help="release folder name")
    p_rip.add_argument("--release", help="MusicBrainz release MBID to tag from "
                                         "when the disc ID is unknown")
    p_rip.add_argument("--offset", type=int, help="override the read offset")
    p_rip.add_argument("--no-double", action="store_true",
                       help="rip once even when AccurateRip cannot verify it")
    p_rip.add_argument("--keep-second", action="store_true",
                       help="keep the second rip instead of deleting it")
    p_rip.add_argument("--ignore-naming", action="store_true",
                       help="rip even when the MusicBrainz titles contain "
                            "lookalike characters")
    p_rip.add_argument("--logger", default=None,
                       help="whipper logger to use (e.g. 'eac' with "
                            "whipper-plugin-eaclogger). Changes the log's "
                            "layout only - it does NOT make the log pass an "
                            "EAC log check.")
    p_rip.add_argument("--label", default=None, help="record label for the edition")
    p_rip.add_argument("--catalogue", default=None, help="catalogue number for the edition")
    p_rip.add_argument("--genre", action="append", help="genre (repeatable)")
    p_rip.add_argument("--discogs-release", default=None,
                       help="Discogs release id, for label/catalogue/genres that "
                            "MusicBrainz does not carry")
    p_rip.add_argument("--no-enrich", action="store_true",
                       help="skip metadata enrichment after the rip")
    p_rip.add_argument("--force", action="store_true",
                       help="rip even if the drive cache cannot be defeated")
    p_rip.add_argument("--skip-salmon", action="store_true",
                       help="stop after the rip")
    p_rip.add_argument("--no-torrent", action="store_true",
                       help="run salmon's checks and spectrals but make no torrent")
    p_rip.set_defaults(func=cmd_rip)

    p_adopt = sub.add_parser(
        "adopt",
        help="take a finished rip (EAC/XLD/whipper) and run everything after it",
    )
    p_adopt.add_argument("path")
    p_adopt.add_argument("--tracks", type=int, default=None,
                         help="expected track count, to catch a missing track")
    p_adopt.add_argument("--expect-offset", type=int, default=None,
                         help="the drive's AccurateRip read offset, to catch a bit-shifted rip")
    p_adopt.add_argument("--checklog", default=None,
                         help="path to EAC's CheckLog.exe")
    p_adopt.add_argument("--ignore-log", action="store_true",
                         help="continue even if the log will not be accepted")
    p_adopt.add_argument("--ignore-rules", action="store_true",
                         help="continue even with blocking rule findings")
    p_adopt.add_argument("--label", default=None)
    p_adopt.add_argument("--catalogue", default=None)
    p_adopt.add_argument("--genre", action="append")
    p_adopt.add_argument("--discogs-release", default=None)
    p_adopt.add_argument("--no-enrich", action="store_true")
    p_adopt.add_argument("--no-salmon", action="store_true",
                         help="stop before handing off to salmon")
    p_adopt.add_argument("--no-torrent", action="store_true")
    p_adopt.set_defaults(func=cmd_adopt)

    p_chk = sub.add_parser("check", help="check a finished release against the formatting rules")
    p_chk.add_argument("path")
    p_chk.set_defaults(func=cmd_check)

    p_eac = sub.add_parser(
        "eac-settings",
        help="check EAC's saved option profile for settings that would make "
             "the release non-compliant")
    p_eac.add_argument("--profile", help="path to a .cfg profile "
                                         "(default: every saved profile)")
    p_eac.set_defaults(func=cmd_eac_settings)

    p_fin = sub.add_parser("finish", help="run the salmon stages on an existing rip")
    p_fin.add_argument("path")
    p_fin.add_argument("--no-torrent", action="store_true")
    p_fin.set_defaults(func=cmd_finish)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = config_mod.load(args.config)
    if args.device is None:
        args.device = cfg.device
    try:
        return args.func(args, cfg)
    except (toc_mod.TocReadError, drive_mod.DriveError, rip_mod.RipError,
            salmon_mod.SalmonError, mb.MusicBrainzError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
