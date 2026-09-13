"""The EAC settings a tracker's log checker scores, as a declarative spec.

:mod:`cdrip.eacprofile` reads a saved profile, but a profile only exposes the
settings EAC stores as *strings* - the filename scheme and the encoder command
line. Everything that decides a log's score is a number or a flag in that
binary blob at an undocumented offset: secure mode, cache defeat, C2, the read
offset. Those were being checked by hand, which is exactly the sort of thing
that is right the first time and wrong the fifth.

So this reads them from the live dialogs instead, where they are ordinary Win32
controls. Every setting below carries the rule or scoring reason it exists for,
because a settings list with no rationale rots into cargo cult.

The read offset is deliberately *not* a constant. It is a property of the
drive, and using another drive's value produces a rip that is bit-shifted
against everybody else's while looking perfectly clean - so it has to be passed
in from :mod:`cdrip.drive`'s AccurateRip lookup.

Two settings here are easy to get backwards:

* ``Drive caches audio data`` is a statement *about the drive*, not a request.
  Ticking it is what makes EAC defeat the cache, and an unticked box on a
  caching drive yields "Defeat audio cache: No" and a lower score.
* ``Delete leading and trailing silent blocks`` must stay **off**. It changes
  the audio, so the rip no longer matches AccurateRip or anyone else's copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import eacwin

CHECK = "check"
TEXT = "text"


@dataclass(frozen=True)
class Setting:
    """One control, what it should be, and why."""

    key: str
    dialog: str
    page: int
    ctl_id: int
    kind: str
    wanted: object
    why: str
    # A setting that is merely recommended rather than required: reported, but
    # it does not fail the gate.
    advisory: bool = False

    def describe(self, actual: object) -> str:
        return "%s: is %r, want %r (%s)" % (self.key, actual, self.wanted, self.why)


@dataclass
class Result:
    checked: int = 0
    problems: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def spec(read_offset: int, output_dir: str | None = None,
         encoder: str | None = None) -> list[Setting]:
    """The settings to enforce, given this drive's offset.

    ``read_offset`` is the drive's AccurateRip value. ``output_dir``, when
    given, pins EAC's extraction directory so a rip does not stop on a folder
    prompt.
    """
    drive, options, compression = (
        eacwin.DIALOG_DRIVE, eacwin.DIALOG_EAC, eacwin.DIALOG_COMPRESSION,
    )
    out: list[Setting] = [
        # --- how the disc is read -------------------------------------------
        Setting("secure_mode", drive, 0, 1505, CHECK, True,
                "a log that does not say 'Secure' scores badly and cannot be "
                "trusted for a lossless claim"),
        Setting("accurate_stream", drive, 0, 7501, CHECK, True,
                "expected of any drive made this century; its absence costs "
                "log score"),
        Setting("defeat_cache", drive, 0, 7502, CHECK, True,
                "ticking 'drive caches audio data' is what makes EAC defeat "
                "the cache, so re-reads are independent. Unticked gives "
                "'Defeat audio cache: No'"),
        Setting("c2_pointers", drive, 0, 7506, CHECK, False,
                "C2 makes a rip faster but less trustworthy; graders expect "
                "it off for a secure rip"),
        Setting("use_read_offset", drive, 2, 7606, CHECK, True,
                "without offset correction the rip is shifted against every "
                "other copy of this disc"),
        Setting("read_offset", drive, 2, 1527, TEXT, "%+d" % read_offset,
                "this drive's AccurateRip offset; a wrong value looks clean "
                "but is bit-shifted against everyone else"),
        Setting("accuraterip", drive, 2, 7613, CHECK, True,
                "AccurateRip confirmation is the strongest evidence a rip is "
                "correct, and RED weights it"),

        # --- what EAC does with the samples ---------------------------------
        Setting("fill_offset_silence", options, 0, 4310, CHECK, True,
                "offset correction needs samples to shift into at the disc "
                "edges; without this the first and last tracks are short"),
        Setting("delete_silence", options, 0, 4203, CHECK, False,
                "trimming silence changes the audio, so the rip stops "
                "matching AccurateRip or anyone else's copy"),

        # --- the log itself -------------------------------------------------
        # This one is the whole reason a previous upload was marked
        # "Trumpable For: Bad/No Checksum(s)". EAC does not sign its log
        # unless asked to, and an unsigned log cannot be verified, so the
        # rip's quality is irrelevant - it is trumpable on sight. It is off
        # by default and lives on a tab that is easy to miss.
        Setting("append_log_checksum", options, 2, 13718, CHECK, True,
                "without it EAC writes no '==== Log checksum ====' line, the "
                "log cannot be verified, and the torrent is trumpable for "
                "Bad/No Checksum(s) however good the rip was"),
        Setting("log_in_english", options, 1, 4322, CHECK, True,
                "RED 2.2.10.5 - a log in another language cannot be scored by "
                "the automated checker and needs a manual staff adjustment"),
        Setting("auto_write_log", options, 2, 13706, CHECK, True,
                "writes the log beside the rip automatically, instead of "
                "leaving it behind a 'Create Log' button that is easy to "
                "forget and produces a file nobody checked"),

        # --- naming, which cannot be fixed after the rip --------------------
        Setting("naming_scheme", options, 4, 4488, TEXT,
                "%tracknr2% - %title%",
                "RED 2.3.13 wants '01 - TrackName'. EAC's stock scheme drops "
                "the separator and appends the artist to every filename, and "
                "renaming afterwards invalidates the log and cue"),

        # --- the encoder ----------------------------------------------------
        Setting("external_compression", compression, 1, 6001, CHECK, True,
                "the FLAC encoder is an external program; without this EAC "
                "writes WAV"),
        Setting("flac_extension", compression, 1, 6016, TEXT, ".flac",
                "the extension EAC gives the encoder's output"),
        Setting("delete_wav", compression, 1, 6013, CHECK, True,
                "otherwise the folder ends up with both WAV and FLAC, which "
                "fails the upload's file checks"),
        Setting("check_return_code", compression, 1, 6018, CHECK, True,
                "without this a failed encode is silently treated as success"),
    ]
    if output_dir:
        out.append(Setting("use_fixed_output_dir", options, 8, 12903, CHECK, True,
                           "pins the extraction directory so an unattended rip "
                           "does not stop on a folder prompt"))
        out.append(Setting("output_dir", options, 8, 12904, TEXT, output_dir,
                           "the directory rips land in; it has to be known up "
                           "front so the post-rip steps can find the folder "
                           "without being told where it went"))
    if encoder:
        out.append(Setting("encoder", compression, 1, 6002, TEXT, encoder,
                           "the FLAC binary EAC shells out to; if the path is "
                           "wrong EAC still rips, then fails every track at "
                           "the compression step"))
    return out


def matches(setting: Setting, actual: object) -> bool:
    """Whether a control's value is already what we want.

    Not plain equality, because EAC normalises what it stores: a directory
    comes back with a trailing separator whether or not one was written. A
    strict comparison therefore reports a permanent difference and "fixes" it
    on every single run, which trains people to ignore the output.
    """
    if setting.kind == TEXT and isinstance(actual, str):
        if setting.key.endswith("_dir"):
            return (actual.rstrip("\\/").lower()
                    == str(setting.wanted).rstrip("\\/").lower())
        return actual.strip() == str(setting.wanted).strip()
    return actual == setting.wanted


def _read(page: int, setting: Setting) -> object:
    hwnd = eacwin.control(page, setting.ctl_id)
    if hwnd is None:
        raise RuntimeError("control %d (%s) not found on its page"
                           % (setting.ctl_id, setting.key))
    if setting.kind == CHECK:
        return eacwin.get_check(hwnd)
    return eacwin.get_text(hwnd)


def _write(page: int, setting: Setting) -> None:
    hwnd = eacwin.control(page, setting.ctl_id)
    if hwnd is None:
        raise RuntimeError("control %d (%s) not found on its page"
                           % (setting.ctl_id, setting.key))
    if setting.kind == CHECK:
        eacwin.set_check(hwnd, bool(setting.wanted))
    else:
        eacwin.set_text(hwnd, str(setting.wanted))


_MENUS = {
    eacwin.DIALOG_DRIVE: eacwin.MENU_DRIVE_OPTIONS,
    eacwin.DIALOG_EAC: eacwin.MENU_EAC_OPTIONS,
    eacwin.DIALOG_COMPRESSION: eacwin.MENU_COMPRESSION_OPTIONS,
}


def _by_dialog(settings: list[Setting]) -> dict[str, list[Setting]]:
    grouped: dict[str, list[Setting]] = {}
    for s in settings:
        grouped.setdefault(s.dialog, []).append(s)
    return grouped


def apply(main_hwnd: int, settings: list[Setting], pid: int | None = None,
          dry_run: bool = True) -> Result:
    """Check every setting against the live dialogs, optionally fixing them.

    ``dry_run`` is the default on purpose: reporting is safe, and writing to
    EAC's options is not something to do as a side effect of asking a question.
    With ``dry_run=False`` the dialogs are OK'd, so the changes are committed -
    but they still only live in memory until a profile is saved.
    """
    result = Result()

    for caption, group in _by_dialog(settings).items():
        dialog = eacwin.open_options(main_hwnd, _MENUS[caption], caption, pid)
        touched = False
        try:
            for setting in sorted(group, key=lambda s: s.page):
                page = eacwin.set_page(dialog, setting.page)
                actual = _read(page, setting)
                result.checked += 1
                if matches(setting, actual):
                    continue
                message = setting.describe(actual)
                if dry_run:
                    (result.advisories if setting.advisory
                     else result.problems).append(message)
                    continue
                _write(page, setting)
                touched = True
                result.changed.append(message)
        finally:
            # Cancel on a dry run so nothing is written even by accident.
            eacwin.close_dialog(dialog, save=touched and not dry_run)

    return result


def verify(main_hwnd: int, settings: list[Setting],
           pid: int | None = None) -> Result:
    """Read-only check. Nothing is written and every dialog is cancelled."""
    return apply(main_hwnd, settings, pid, dry_run=True)
