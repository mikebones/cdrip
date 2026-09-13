"""Reading and driving EAC's *dialogs* - the half :mod:`eacdrive` cannot do.

:mod:`eacdrive` walks EAC's ``HMENU``s and posts ``WM_COMMAND``, which starts a
flow. Most useful entries end in "..." and open a dialog, and a dialog is where
the actual work happens. Unlike EAC's main window - whose controls are custom
classes (``myedit``, ``mycombo``, ``mybutton``) and which UI Automation sees as
56 featureless ``Pane``s - its dialogs are ordinary Win32 ``#32770`` windows
with real ``Edit`` and ``Button`` children. Those are fully scriptable.

**The trap that cost the most time here: do not read control text with
``GetWindowText``.** It is documented to return only the *caption* of a window
owned by another process, and an edit control has no caption - so it returns an
empty string for a control that is plainly full of text. That silently produced
"the write failed" for writes that had in fact succeeded, and "the import did
nothing" for an import that had actually mangled the data in an interesting
way. ``WM_GETTEXT`` is marshalled across process boundaries by USER32 and
returns the truth; :func:`get_text` uses it. Verify with the same mechanism you
wrote with, never with a weaker one.

Two further things measured on EAC 1.8 rather than assumed:

* **Clipboard import is positional and literal.** "Get CD Information From /
  Clipboard" takes the clipboard's lines, in order, and makes line *i* the
  title of track *i*. It does not parse a header, a track number, or a
  duration - feeding it EAC's own *export* format puts the whole line
  ("``01.<TAB>Title<TAB><TAB>03:48``") into the title. One bare title per line,
  no header, nothing else. See :func:`titles_to_clipboard`.
* **Clipboard import does not clear per-track artists.** A disc left over from
  an earlier attempt keeps "Unknown Artist" on every track, which would go
  straight into each file's ARTIST tag. "Clear Current CD Information" (the
  ``Clear`` dialog) does clear them, so clear first and set the album fields
  again afterwards - the clear takes those with it.

Both import and clear raise a confirmation dialog. :func:`confirm` answers it.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

WM_COMMAND = 0x0111
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E

BM_GETCHECK = 0x00F0
BM_CLICK = 0x00F5

# Property-sheet page selection. TCM_SETCURSEL (the tab control's own message)
# moves the highlight but sends no TCN_SELCHANGE, so the page never actually
# changes and every tab reads back identically - which looks like the dialog
# has one page repeated. PSM_SETCURSEL is the property sheet's message, takes
# an index in wParam, and needs no pointer, so it marshals cross-process.
PSM_SETCURSEL = 0x0400 + 101

DIALOG_CLASS = "#32770"

# Control ids in EAC 1.8's "CD Information" dialog (Database / Edit CD
# Information..., menu id 518). Read out of the running dialog; not a published
# interface, so verify against your build before trusting them.
CD_INFO = {
    "title": 3701,
    "artist": 3702,
    "year": 3704,
    "first_track": 3716,
    "ok": 3707,
    "cancel": 3708,
}

# Fields on the main window, for confirming that a dialog's OK actually landed.
MAIN_FIELDS = {"title": 992, "artist": 993, "year": 995}

# Confirmations EAC raises, as (window title, button id to accept).
CONFIRM_IMPORT = ("Warning", 6)   # "All data of the current CD will be deleted!"
CONFIRM_CLEAR = ("Clear", 5101)   # "All data of the current CD will be overwritten!"

# Menu command ids used here, read from a running EAC 1.8.
MENU_EDIT_CD_INFO = 518
MENU_CLEAR_CD_INFO = 524
MENU_IMPORT_CLIPBOARD = 662
MENU_EXPORT_CLIPBOARD = 584
MENU_DRIVE_OPTIONS = 541
MENU_EAC_OPTIONS = 504
MENU_COMPRESSION_OPTIONS = 542
MENU_SAVE_PROFILE = 558

# EAC shows an "Important Information" nag before the options dialogs. It has
# to be dismissed or the options dialog never appears; unticking 3301 stops it
# coming back.
NAG = ("Information", 3311)
NAG_SHOW_AGAIN = 3301

# Options dialogs, by the substring that identifies their caption. The drive
# dialog's caption includes the drive model, so it can only be matched loosely.
DIALOG_DRIVE = "options for drive"
DIALOG_EAC = "eac options"
DIALOG_COMPRESSION = "compression options"

# Standard property-sheet buttons.
BUTTON_OK = 1
BUTTON_CANCEL = 2
BUTTON_APPLY = 12321
# "Split Track Information To Artist/Title". The label reads Artist/Title but
# the behaviour is the reverse: part 1 stays the title, part 2 becomes the
# artist. Feeding it "Artist / Title" puts the song name in the artist field.
MENU_SPLIT_ARTIST_TITLE = 547


def available() -> bool:
    return sys.platform == "win32"


@dataclass(frozen=True)
class Control:
    hwnd: int
    ctl_id: int
    cls: str


def _win32():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageW.restype = ctypes.c_void_p
    return ctypes, wintypes, user32


def _enum_top(pid: int | None = None) -> list[int]:
    """Visible top-level windows, optionally limited to one process."""
    ctypes, wintypes, user32 = _win32()
    found: list[int] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        if pid is not None:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
        found.append(int(hwnd))
        return True

    user32.EnumWindows(proto(cb), 0)
    return found


def _class_name(hwnd: int) -> str:
    ctypes, wintypes, user32 = _win32()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def _caption(hwnd: int) -> str:
    """A *window's* caption. Correct for top-level windows, useless for controls."""
    ctypes, wintypes, user32 = _win32()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, 512)
    return buf.value


def children(hwnd: int) -> list[Control]:
    """Every child control of a window, with its control id and class."""
    ctypes, wintypes, user32 = _win32()
    out: list[Control] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(child, _):
        out.append(Control(
            hwnd=int(child),
            ctl_id=int(user32.GetDlgCtrlID(child)),
            cls=_class_name(int(child)),
        ))
        return True

    user32.EnumChildWindows(wintypes.HWND(hwnd), proto(cb), 0)
    return out


def control(parent: int, ctl_id: int) -> int | None:
    """HWND of the child with this control id, or None."""
    for c in children(parent):
        if c.ctl_id == ctl_id:
            return c.hwnd
    return None


def get_text(hwnd: int) -> str:
    """Text of a control, read with WM_GETTEXT.

    ``GetWindowText`` cannot do this across a process boundary - it returns
    captions only, so an edit control reads back as "" no matter what it holds.
    ``WM_GETTEXT`` is marshalled by USER32 and returns the real contents.
    """
    ctypes, wintypes, user32 = _win32()
    length = user32.SendMessageW(wintypes.HWND(hwnd), WM_GETTEXTLENGTH, 0, None)
    length = int(length or 0)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.SendMessageW(wintypes.HWND(hwnd), WM_GETTEXT,
                        ctypes.c_size_t(length + 1), buf)
    return buf.value


def set_text(hwnd: int, value: str) -> None:
    """Set a control's text, then read it back to prove it landed."""
    ctypes, wintypes, user32 = _win32()
    user32.SendMessageW(wintypes.HWND(hwnd), WM_SETTEXT, 0,
                        ctypes.c_wchar_p(value))
    got = get_text(hwnd)
    if got != value:
        raise RuntimeError("set_text did not stick: wrote %r, read back %r"
                           % (value, got))


def get_check(hwnd: int) -> bool:
    """Whether a checkbox or radio button is ticked."""
    ctypes, wintypes, user32 = _win32()
    return bool(int(user32.SendMessageW(
        wintypes.HWND(hwnd), BM_GETCHECK, 0, None) or 0))


def set_check(hwnd: int, wanted: bool) -> None:
    """Tick or untick a box, and confirm it took.

    ``BM_SETCHECK`` would change the box's appearance without telling the
    dialog, so the dialog's own state would never update and OK would write
    back the old value. ``BM_CLICK`` toggles it *and* sends BN_CLICKED to the
    parent, which is what the dialog listens for - so this clicks only when
    the current state is wrong.
    """
    ctypes, wintypes, user32 = _win32()
    if get_check(hwnd) == wanted:
        return
    user32.SendMessageW(wintypes.HWND(hwnd), BM_CLICK, 0, None)
    time.sleep(0.4)
    if get_check(hwnd) != wanted:
        raise RuntimeError("checkbox did not move to %r" % wanted)


def find_dialog_containing(needle: str, pid: int | None = None) -> int | None:
    """A visible dialog whose caption contains ``needle`` (case-insensitive).

    Needed because the drive options dialog is captioned with the drive model
    ("Options for drive SlimtypeDVD A  DS8A5SH "), so it cannot be matched
    exactly.
    """
    lowered = needle.lower()
    for hwnd in _enum_top(pid):
        if _class_name(hwnd) != DIALOG_CLASS:
            continue
        if lowered in _caption(hwnd).lower():
            return hwnd
    return None


def dismiss_nag(pid: int | None = None, stop_showing: bool = True) -> bool:
    """Clear EAC's "Important Information" dialog if it is blocking the way.

    It appears in front of the options dialogs, so an unattended run stalls
    behind it. Returns whether there was one.
    """
    title, ok = NAG
    dialog = find_dialog(title, pid)
    if not dialog:
        return False
    if stop_showing:
        box = control(dialog, NAG_SHOW_AGAIN)
        if box is not None:
            try:
                set_check(box, False)
            except RuntimeError:
                pass  # Not worth failing the run over the nag's own checkbox.
    click(dialog, ok, settle=2.0)
    return True


def set_page(dialog: int, index: int, settle: float = 0.8) -> int:
    """Switch a property sheet to a page and return that page's HWND.

    See :data:`PSM_SETCURSEL` for why the tab control's own message does not
    work here.
    """
    ctypes, wintypes, user32 = _win32()
    user32.SendMessageW(wintypes.HWND(dialog), PSM_SETCURSEL,
                        ctypes.c_size_t(index), None)
    time.sleep(settle)
    page = visible_page(dialog)
    if page is None:
        raise RuntimeError("no visible page after selecting index %d" % index)
    return page


def visible_page(dialog: int) -> int | None:
    """The property sheet's currently visible page.

    Pages are created lazily and previously visited ones stay around hidden,
    so enumerating children returns every page that has ever been shown.
    Filtering on visibility is what picks the current one.
    """
    ctypes, wintypes, user32 = _win32()
    for child in children(dialog):
        if child.cls != DIALOG_CLASS:
            continue
        if user32.IsWindowVisible(wintypes.HWND(child.hwnd)):
            return child.hwnd
    return None


def open_options(main_hwnd: int, menu_id: int, caption: str,
                 pid: int | None = None, timeout: float = 20.0) -> int:
    """Open one of EAC's options dialogs, clearing the nag if it appears."""
    from . import eacdrive

    existing = find_dialog_containing(caption, pid)
    if existing:
        return existing

    eacdrive.post_command(main_hwnd, menu_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        dismiss_nag(pid)
        found = find_dialog_containing(caption, pid)
        if found:
            return found
        time.sleep(0.4)
    raise TimeoutError("options dialog %r did not appear" % caption)


def close_dialog(dialog: int, save: bool, settle: float = 2.5) -> None:
    """OK (writing changes) or Cancel (discarding them)."""
    click(dialog, BUTTON_OK if save else BUTTON_CANCEL, settle=settle)


def find_dialog(title: str, pid: int | None = None) -> int | None:
    """A visible ``#32770`` dialog with exactly this caption."""
    for hwnd in _enum_top(pid):
        if _class_name(hwnd) == DIALOG_CLASS and _caption(hwnd) == title:
            return hwnd
    return None


def wait_for_dialog(title: str, pid: int | None = None,
                    timeout: float = 10.0) -> int:
    """Block until a dialog appears, or raise."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = find_dialog(title, pid)
        if hwnd:
            return hwnd
        time.sleep(0.25)
    raise TimeoutError("dialog %r did not appear within %.0fs" % (title, timeout))


def click(dialog: int, ctl_id: int, settle: float = 1.5) -> None:
    """Press a button by posting WM_COMMAND, as the dialog's own code expects."""
    ctypes, wintypes, user32 = _win32()
    btn = control(dialog, ctl_id) or 0
    user32.PostMessageW(wintypes.HWND(dialog), WM_COMMAND,
                        wintypes.WPARAM(ctl_id), wintypes.LPARAM(btn))
    time.sleep(settle)


def confirm(which: tuple[str, int], pid: int | None = None,
            timeout: float = 10.0) -> bool:
    """Accept one of EAC's confirmation dialogs if it is up.

    Returns whether there was anything to accept - import and clear both raise
    one, so silence means the command did not take.
    """
    title, ctl_id = which
    try:
        dialog = wait_for_dialog(title, pid, timeout)
    except TimeoutError:
        return False
    click(dialog, ctl_id)
    return True


# --- the two operations worth having by name --------------------------------


def titles_to_clipboard(titles: dict[int, str]) -> str:
    """Render track titles in the only shape EAC's clipboard import accepts.

    One bare title per line, ordered by track number, nothing else. A header
    line becomes track 1's title and shifts everything; a track number or
    duration ends up *inside* the title.
    """
    if not titles:
        raise ValueError("no titles")
    ordered = [titles[n] for n in sorted(titles)]
    if sorted(titles) != list(range(1, len(titles) + 1)):
        raise ValueError("titles must be a contiguous run starting at track 1, "
                         "because the import is positional: got %r"
                         % sorted(titles))
    return "\r\n".join(ordered) + "\r\n"


def set_clipboard(text: str) -> None:
    """Put text on the clipboard as CF_UNICODETEXT."""
    ctypes, wintypes, user32 = _win32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p

    data = text.encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE|GMEM_ZEROINIT
    ptr = kernel32.GlobalLock(ctypes.c_void_p(handle))
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(ctypes.c_void_p(handle))

    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed: %d" % ctypes.get_last_error())
    try:
        user32.EmptyClipboard()
        # 13 = CF_UNICODETEXT. Ownership of the handle passes to the clipboard.
        if not user32.SetClipboardData(13, ctypes.c_void_p(handle)):
            raise OSError("SetClipboardData failed: %d" % ctypes.get_last_error())
    finally:
        user32.CloseClipboard()


def set_cd_info(main_hwnd: int, pid: int | None = None, *,
                title: str | None = None, artist: str | None = None,
                year: str | None = None) -> dict[str, str]:
    """Fill the CD Information dialog and OK it; return the main window's values.

    The return value is read back off the *main* window, not the dialog, so a
    caller can see that OK actually committed rather than trusting that it did.
    """
    from . import eacdrive

    eacdrive.post_command(main_hwnd, MENU_EDIT_CD_INFO)
    dialog = wait_for_dialog("CD Information", pid)

    for key, value in (("title", title), ("artist", artist), ("year", year)):
        if value is None:
            continue
        hwnd = control(dialog, CD_INFO[key])
        if hwnd is None:
            raise RuntimeError("no control %d (%s) in CD Information" % (CD_INFO[key], key))
        set_text(hwnd, value)

    click(dialog, CD_INFO["ok"], settle=2.0)
    if find_dialog("CD Information", pid):
        raise RuntimeError("CD Information dialog did not close after OK")

    return {name: get_text(control(main_hwnd, cid) or 0)
            for name, cid in MAIN_FIELDS.items()}


def import_titles(main_hwnd: int, titles: dict[int, str],
                  pid: int | None = None, clear_first: bool = True) -> None:
    """Put track titles into EAC via the clipboard.

    ``clear_first`` wipes stale per-track artists (an aborted earlier attempt
    leaves "Unknown Artist" on every track, and the import will not remove it).
    Clearing also drops the album fields, so set those *after* this call.
    """
    from . import eacdrive

    if clear_first:
        eacdrive.post_command(main_hwnd, MENU_CLEAR_CD_INFO)
        if not confirm(CONFIRM_CLEAR, pid):
            raise RuntimeError("clear did not raise its confirmation dialog")

    set_clipboard(titles_to_clipboard(titles))
    eacdrive.post_command(main_hwnd, MENU_IMPORT_CLIPBOARD)
    if not confirm(CONFIRM_IMPORT, pid):
        raise RuntimeError("clipboard import did not raise its confirmation dialog")


def export_cd_info(main_hwnd: int, pid: int | None = None) -> str:
    """Ask EAC to write its current CD information to the clipboard, and read it.

    This is the honest way to check what EAC actually holds - it is EAC's own
    rendering, not an inference from control contents.
    """
    from . import eacdrive

    set_clipboard("")
    eacdrive.post_command(main_hwnd, MENU_EXPORT_CLIPBOARD)
    time.sleep(2.0)
    return get_clipboard()


# EAC's extraction progress window. Its caption is stable across the test and
# copy passes, so its presence is the signal that a rip is under way.
RIP_DIALOG = "Extracting"


def rip_in_progress(pid: int | None = None) -> bool:
    """Whether EAC is currently extracting.

    Note what this deliberately does *not* use: a process's I/O counters. CD
    reads go out through SCSI passthrough and do not increment
    ``ReadOperationCount``, so a perfectly healthy rip shows a zero read delta
    and near-idle CPU. Concluding "nothing is happening" from that is wrong,
    and was wrong here. The progress window is the honest signal.
    """
    return find_dialog_containing(RIP_DIALOG, pid) is not None


# The extraction dialog's one action button. It reads "Cancel" while the rip
# runs and becomes "OK" when it is done - the window does NOT close by itself.
RIP_BUTTON = 811
RIP_DONE_LABEL = "OK"
# The dialog also has a status ListBox (id 812) whose last line reads "Audio
# Extraction Complete", but its text is not readable from another process:
# unlike WM_GETTEXT, LB_GETTEXT is not marshalled across process boundaries
# and would need a buffer written into EAC's address space. The button label
# gives the same answer for free, so the listbox is left alone.


def rip_complete(pid: int | None = None) -> bool:
    """Whether the extraction dialog is showing a *finished* rip.

    Waiting for the window to disappear does not work: when extraction
    finishes EAC leaves the dialog up with its summary and waits for someone
    to press OK. A wait built on the window closing therefore hangs forever on
    a rip that succeeded. The button's label is the actual signal - it reads
    "Cancel" during the rip and "OK" afterwards.
    """
    dialog = find_dialog_containing(RIP_DIALOG, pid)
    if dialog is None:
        return False
    button = control(dialog, RIP_BUTTON)
    if button is None:
        return False
    return get_text(button).strip() == RIP_DONE_LABEL


# Cue sheet creation. A rip without one is trumpable under RED 2.2.10.7 - "a
# 100%% log rip lacking a cue sheet can be replaced by another 100%% log rip
# with a noncompliant cue sheet" - so the cue is not optional polish.
#
# EAC offers several variants and labels one of them "(Noncompliant)" itself.
# For a track-based rip the wanted one is "Multiple WAV Files With Corrected
# Gaps"; it needs gap detection to have run first, which is a separate pass
# over the disc and takes a few minutes.
MENU_DETECT_GAPS = 539
# "Current Gap Settings" writes a cue that matches however the disc was
# actually ripped, which is the only variant guaranteed to describe the files
# on disk. The named variants each assume a particular layout, and choosing
# one that disagrees with the rip produces a cue that is wrong about the audio
# while looking perfectly well-formed - see the note on gap handling below.
MENU_CUE_CURRENT_GAPS = 575
MENU_CUE_CORRECTED_GAPS = 572
MENU_CUE_LEFTOUT_GAPS = 536
MENU_CUE_NONCOMPLIANT = 586
MENU_CUE_SINGLE_WAV = 535

# Gap handling, which the cue must agree with. EAC's default is "Append Gaps
# To Previous Track": each file is exactly its TOC span, and track N+1's
# pregap therefore sits at the END of file N. A cue for that layout expresses
# it as TRACK N+1 / INDEX 00 late inside FILE N, then INDEX 01 00:00:00 at the
# start of FILE N+1.
#
# Asking for "Corrected Gaps" instead describes the opposite layout - pregap
# at the START of the following file - and EAC will happily write it, giving
# every gapped track an identical "INDEX 00 00:00:00 / INDEX 01 00:01:00".
# That looks like measured data and is not: it contradicts files that are
# exact TOC spans. Verified by measurement - the FLACs matched their TOC
# lengths to the frame, and track 2 began with 0.5s of silence, not the 1.00s
# the corrected-gaps cue claimed.
MENU_GAPS_LEAVE_OUT = 565
MENU_GAPS_APPEND_PREVIOUS = 477   # EAC's default
MENU_GAPS_APPEND_NEXT = 564

CUE_DIALOG = "Create CUE Sheet"


# Gap detection shows its own progress window - "Analyzing", with "Detecting
# Pre-Track Gaps" inside - NOT the "Extracting Audio Data" dialog a rip uses.
# Waiting on the extraction dialog therefore returns instantly, and the cue is
# then written from gap information that does not exist yet.
ANALYZE_DIALOG = "Analyzing"


def gaps_in_progress(pid: int | None = None) -> bool:
    return find_dialog_containing(ANALYZE_DIALOG, pid) is not None


def detect_gaps(main_hwnd: int, pid: int | None = None,
                timeout: float = 1800.0, poll: float = 5.0,
                start_timeout: float = 30.0) -> bool:
    """Run EAC's gap detection, which a compliant cue sheet depends on.

    This reads the disc again, so it is not instant - minutes, not seconds.
    Returns True when detection has finished.
    """
    from . import eacdrive

    eacdrive.post_command(main_hwnd, MENU_DETECT_GAPS)

    # Wait for the window to appear before waiting for it to go away, or a
    # slow start looks like an instant finish.
    appeared = False
    start_deadline = time.time() + start_timeout
    while time.time() < start_deadline:
        if gaps_in_progress(pid):
            appeared = True
            break
        time.sleep(0.5)
    if not appeared:
        # Gaps may already be known from an earlier pass, in which case EAC
        # shows nothing at all. That is a legitimate success, not a failure.
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not gaps_in_progress(pid):
            time.sleep(1.5)   # let EAC settle before the next command
            return True
        time.sleep(poll)
    raise TimeoutError("gap detection did not finish within %.0fs" % timeout)


def create_cue(main_hwnd: int, output_dir: str, pid: int | None = None,
               variant: int = MENU_CUE_CURRENT_GAPS,
               timeout: float = 30.0) -> str:
    """Write a cue sheet and return the path EAC actually wrote.

    EAC does not always ask where to put it. With a fixed extraction directory
    configured it writes straight there under its own name and shows no
    dialog at all, so waiting for one times out on a cue that was created
    successfully. Both routes are handled: answer the dialog if it appears,
    and otherwise look for a cue that was not there before.
    """
    import os

    from . import eacdrive

    before = {f for f in os.listdir(output_dir) if f.lower().endswith(".cue")}
    eacdrive.post_command(main_hwnd, variant)

    deadline = time.time() + timeout
    while time.time() < deadline:
        dialog = (find_dialog_containing(CUE_DIALOG, pid)
                  or find_dialog_containing("Save", pid))
        if dialog is not None:
            name = control(dialog, 1001)
            if name is None:
                raise RuntimeError("save dialog has no filename field")
            target = os.path.join(output_dir, "release.cue")
            set_text(name, target)
            click(dialog, BUTTON_OK, settle=3.0)
            return target

        made = {f for f in os.listdir(output_dir)
                if f.lower().endswith(".cue")} - before
        if made:
            return os.path.join(output_dir, sorted(made)[0])
        time.sleep(0.5)

    raise TimeoutError(
        "no cue sheet appeared in %s and no save dialog was shown" % output_dir)


def dismiss_rip_dialog(pid: int | None = None) -> bool:
    """Press OK on a finished rip so EAC writes its log and returns to idle."""
    dialog = find_dialog_containing(RIP_DIALOG, pid)
    if dialog is None:
        return False
    click(dialog, RIP_BUTTON, settle=2.5)
    return True


def wait_for_rip(pid: int | None = None, timeout: float = 7200.0,
                 poll: float = 15.0, on_tick=None) -> bool:
    """Block until EAC's rip finishes.

    Returns True if it finished, False if it never started - a real case
    worth distinguishing, because posting the menu command succeeds whether or
    not extraction actually begins.

    Completion is detected from the dialog's button, not from the window
    vanishing; see :func:`rip_complete`.
    """
    deadline = time.time() + timeout
    started = False
    while time.time() < deadline:
        if rip_complete(pid):
            return True
        running = rip_in_progress(pid)
        if running:
            started = True
        elif started:
            # The dialog went away without ever reading "OK" - someone closed
            # it, or EAC aborted. Finished either way, just not cleanly.
            return True
        if on_tick is not None:
            on_tick(running)
        time.sleep(poll)
    if not started:
        return False
    raise TimeoutError("rip did not finish within %.0fs" % timeout)


def start_rip(main_hwnd: int, pid: int | None = None,
              settle: float = 5.0) -> bool:
    """Trigger 'Test & Copy Selected Tracks -> Compressed' and confirm it began.

    The menu entry is found by walking the menu rather than by its id, and its
    leaf label is matched exactly: "Uncompressed..." contains "compressed", so
    a substring match silently rips to WAV instead of FLAC.
    """
    from . import eacdrive

    entry = eacdrive.find_rip_command(eacdrive.read_menu(main_hwnd))
    if entry is None:
        raise RuntimeError("could not find the compressed test-and-copy entry")
    eacdrive.post_command(main_hwnd, entry.command_id)
    time.sleep(settle)
    return rip_in_progress(pid)


def parse_export(text: str) -> dict:
    """Turn EAC's clipboard export back into structured data.

    The export is the only view of EAC's state that comes from EAC itself, so
    it is what we verify against. Its one subtlety: a track line is
    ``NN.<TAB>Artist / Title<TAB><TAB>MM:SS``, but **the artist is omitted when
    it equals the CD artist**. That is why a stale "Unknown Artist" on every
    track is invisible while the CD artist is also "Unknown Artist", and pops
    into view the moment the CD artist is corrected - it was never fixed, only
    hidden. Tracks are reported here with the artist resolved, so a caller sees
    the real value either way.
    """
    lines = text.splitlines()
    header = lines[0] if lines else ""
    artist, _, album = header.partition(" - ")
    tracks: dict[int, dict[str, str]] = {}
    for line in lines[1:]:
        if "\t" not in line:
            continue
        number, _, rest = line.partition(".\t")
        if not number.strip().isdigit():
            continue
        parts = [p for p in rest.split("\t") if p]
        if not parts:
            continue
        body, duration = parts[0], (parts[-1] if len(parts) > 1 else "")
        if " / " in body:
            track_artist, _, title = body.partition(" / ")
        else:
            track_artist, title = artist, body
        tracks[int(number)] = {
            "artist": track_artist, "title": title, "duration": duration,
        }
    return {"artist": artist, "album": album, "tracks": tracks}


def populate_metadata(main_hwnd: int, pid: int | None, *,
                      artist: str, album: str, year: str,
                      titles: dict[int, str]) -> dict:
    """Put a complete, correct tracklist into EAC, and verify it took.

    The order matters and each step exists for a measured reason:

    1. Clear, to drop per-track artists left by any earlier attempt. Nothing
       else removes them, and they are invisible until the CD artist is right.
    2. Import ``"Title / Artist"`` per line, then run EAC's split transform.
       The import only ever sets titles, so this is the one route to per-track
       artists that does not involve editing the track list by hand.
    3. Set the album fields, which step 1 also cleared.

    Returns the parsed export, so the caller can see what EAC actually holds
    rather than what we believe we sent it.
    """
    from . import eacdrive

    combined = {n: "%s / %s" % (t, artist) for n, t in titles.items()}
    import_titles(main_hwnd, combined, pid, clear_first=True)

    eacdrive.post_command(main_hwnd, MENU_SPLIT_ARTIST_TITLE)
    time.sleep(2.0)

    set_cd_info(main_hwnd, pid, title=album, artist=artist, year=year)

    state = parse_export(export_cd_info(main_hwnd, pid))
    problems = []
    if state["artist"] != artist:
        problems.append("CD artist is %r, expected %r" % (state["artist"], artist))
    if state["album"] != album:
        problems.append("CD album is %r, expected %r" % (state["album"], album))
    for n, want in sorted(titles.items()):
        got = state["tracks"].get(n)
        if got is None:
            problems.append("track %d missing from export" % n)
        elif got["title"] != want:
            problems.append("track %d title is %r, expected %r"
                            % (n, got["title"], want))
        elif got["artist"] != artist:
            problems.append("track %d artist is %r, expected %r"
                            % (n, got["artist"], artist))
    if problems:
        raise RuntimeError("EAC metadata is not what we set:\n  "
                           + "\n  ".join(problems))
    return state


def get_clipboard() -> str:
    ctypes, wintypes, user32 = _win32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    user32.GetClipboardData.restype = ctypes.c_void_p

    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(ctypes.c_void_p(handle))
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(ctypes.c_void_p(handle))
    finally:
        user32.CloseClipboard()
