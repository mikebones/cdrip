"""Driving EAC from code, via Win32 - the only layer that works.

Three approaches were measured on EAC 1.8 before settling here:

* **Command line.** ``EAC.exe`` accepts ``-DRIVE``, ``-OUTPUTDIRECTORY``,
  ``-TESTANDCOPY``, ``-CLOSE`` and more, but they do not rip. Launched with all
  of them, EAC opens its window and idles - flat CPU and no output files after
  45 seconds. EAC's own documentation only ever describes the crash-workaround
  switches, which is consistent with these being GUI-state flags.
* **UI Automation.** Can see EAC, cannot act on it. Its window (class
  ``erstes``) exposes 56 descendants, every one a bare ``Pane``, *none*
  supporting InvokePattern, and no MenuBar at all. pywinauto's UIA backend is
  therefore useless, and so is anything built on it.
* **Win32 menus.** These work. EAC's menus are real ``HMENU``s, so they can be
  walked with GetSubMenu/GetMenuItemID and triggered with WM_COMMAND, with no
  clicking and no screen coordinates.

The honest limit: the useful rip entries end in "..." because they open a
dialog. Posting the command starts the flow; the dialog still has to be
answered. Dialogs are ordinary Win32 windows with real controls, so they are
tractable in a way EAC's own window is not - but this is assistance, not
unattended automation, and should not be described as the latter.

This module is Windows-only and imports nothing at module scope that would
break elsewhere; callers should check :func:`available` first.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

WM_COMMAND = 0x0111
MF_BYPOSITION = 0x0400

# Menu labels are matched loosely because EAC strips "&" accelerators and
# embeds tab-separated shortcuts ("Compressed...\tShift+F6").
RIP_COMPRESSED = ("test", "copy", "selected")
RIP_ENTRY = "compressed"


def available() -> bool:
    return sys.platform == "win32"


@dataclass(frozen=True)
class MenuItem:
    path: tuple[str, ...]
    command_id: int
    opens_dialog: bool

    def __str__(self) -> str:
        return "%s -> id=%d%s" % (
            " / ".join(self.path), self.command_id,
            " (opens a dialog)" if self.opens_dialog else "",
        )


def _win32():
    """Import ctypes bindings lazily so the module imports anywhere."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetMenu.restype = wintypes.HMENU
    user32.GetSubMenu.restype = wintypes.HMENU
    user32.GetMenuItemCount.restype = ctypes.c_int
    user32.GetMenuItemID.restype = ctypes.c_uint
    return ctypes, wintypes, user32


def find_window(process_name: str = "EAC") -> int | None:
    """Return EAC's main window handle, or None if it is not running."""
    if not available():
        return None
    import subprocess

    # Ask Windows rather than enumerating: the window title changes with the
    # loaded disc, so matching on it is unreliable.
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process %s -ErrorAction SilentlyContinue).MainWindowHandle" % process_name],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    for line in out:
        line = line.strip()
        if line.isdigit() and int(line):
            return int(line)
    return None


def read_menu(hwnd: int) -> list[MenuItem]:
    """Walk EAC's real Win32 menus and return every command with an id.

    Items whose id is 0xFFFFFFFF are submenus rather than commands; they are
    descended into, not reported.
    """
    ctypes, wintypes, user32 = _win32()
    menu = user32.GetMenu(wintypes.HWND(hwnd))
    if not menu:
        return []

    items: list[MenuItem] = []

    def walk(handle, path: tuple[str, ...], depth: int = 0) -> None:
        if depth > 3:
            return
        count = user32.GetMenuItemCount(handle)
        for pos in range(count):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetMenuStringW(handle, pos, buf, 256, MF_BYPOSITION)
            label = buf.value.replace("&", "").split("\t")[0].strip()
            if not label:
                continue
            sub = user32.GetSubMenu(handle, pos)
            if sub:
                walk(sub, path + (label,), depth + 1)
                continue
            cmd = user32.GetMenuItemID(handle, pos)
            if cmd and cmd != 0xFFFFFFFF:
                items.append(MenuItem(
                    path=path + (label,),
                    command_id=int(cmd),
                    opens_dialog=label.endswith("..."),
                ))

    walk(menu, ())
    return items


def find_command(items: list[MenuItem], *needles: str) -> MenuItem | None:
    """First menu item whose path contains all the given substrings."""
    wanted = [n.lower() for n in needles]
    for item in items:
        joined = " / ".join(item.path).lower()
        if all(n in joined for n in wanted):
            return item
    return None


def find_rip_command(items: list[MenuItem]) -> MenuItem | None:
    """Locate 'Action -> Test & Copy Selected Tracks -> Compressed...'.

    Found by walking the menu rather than hard-coding, because the numeric id
    is not a published interface and can move between builds. On EAC 1.8 it is
    771; treat that as an observation, not a constant.

    The leaf label is matched exactly, not by substring: "Uncompressed..."
    contains "compressed", so a substring match silently selects WAV output
    (id 478) instead of FLAC. Caught live.
    """
    for item in items:
        joined = " / ".join(item.path[:-1]).lower()
        leaf = item.path[-1].lower().rstrip(". ")
        if leaf == RIP_ENTRY and all(n in joined for n in RIP_COMPRESSED):
            return item
    return None


def post_command(hwnd: int, command_id: int) -> None:
    """Trigger a menu command. Does not wait, and cannot answer a dialog."""
    ctypes, wintypes, user32 = _win32()
    if not user32.PostMessageW(wintypes.HWND(hwnd), WM_COMMAND,
                               wintypes.WPARAM(command_id), wintypes.LPARAM(0)):
        raise OSError("PostMessage failed: %d" % ctypes.get_last_error())
