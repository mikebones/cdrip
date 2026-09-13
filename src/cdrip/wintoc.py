"""Reading a CD's TOC on Windows, with nothing installed.

``cd-paranoia`` is not available on Windows, which left the whole disc-side of
cdrip - ``info``, ``submit-discid``, the mixed-mode analysis, the AccurateRip
identity - unusable on the machine that has to run EAC. Installing a Cygwin
cdparanoia would work, but it is not necessary: Windows already exposes the
raw TOC through ``DeviceIoControl`` with ``IOCTL_CDROM_READ_TOC_EX``, which
needs no driver, no package and no elevation.

It is also *better* than the cd-paranoia path for this job. cd-paranoia
reports audio tracks only, so the data track on a mixed-mode disc had to be
inferred; this returns every track with its control flags, so the data track
and the real lead-out are read directly rather than deduced. That matters
because those two values are exactly what AccurateRip keys on, and getting
them wrong is what made a disc that *is* in AccurateRip look absent.

The device is opened for shared read with no access rights requested, which is
enough for a TOC query and avoids taking the drive away from anything else -
notably EAC, which locks the tray while extracting.
"""

from __future__ import annotations

import sys

# CTL_CODE(FILE_DEVICE_CD_ROM=2, 0x15, METHOD_BUFFERED=0, FILE_READ_ACCESS=1)
#   = (2 << 16) | (1 << 14) | (0x15 << 2) | 0
IOCTL_CDROM_READ_TOC_EX = 0x00024054

CDROM_READ_TOC_EX_FORMAT_TOC = 0x00

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = -1

# CDROM_TOC: 2-byte length, first track, last track, then 100 8-byte entries.
MAXIMUM_NUMBER_TRACKS = 100
TRACK_DATA_SIZE = 8
CDROM_TOC_SIZE = 4 + MAXIMUM_NUMBER_TRACKS * TRACK_DATA_SIZE

# TRACK_DATA.Control bit 2 marks a data track; 0xAA is the lead-out's number.
CONTROL_DATA = 0x04
LEADOUT_TRACK = 0xAA


class WinTocError(RuntimeError):
    pass


def available() -> bool:
    return sys.platform == "win32"


def device_path(device: str) -> str:
    r"""Turn a drive letter into the path DeviceIoControl wants.

    Accepts ``D``, ``D:``, ``D:\`` or an already-formed ``\\.\D:``.
    """
    device = device.strip()
    if device.startswith("\\\\.\\"):
        return device.rstrip("\\") if len(device) > 6 else device
    letter = device.rstrip(":\\/")
    if len(letter) != 1 or not letter.isalpha():
        raise WinTocError(
            "expected a drive letter like 'D:' , got %r. On Windows a CD is "
            "addressed by letter, not by a /dev path." % device
        )
    return "\\\\.\\%s:" % letter.upper()


def _read_raw(device: str) -> bytes:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]

    path = device_path(device)
    # dwDesiredAccess 0: query the device without asking for read/write, so an
    # application that holds it (EAC mid-rip) is not disturbed.
    handle = kernel32.CreateFileW(
        path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        OPEN_EXISTING, 0, None,
    )
    if handle == INVALID_HANDLE_VALUE or handle is None:
        raise WinTocError("could not open %s: error %d"
                          % (path, ctypes.get_last_error()))

    try:
        class ReadTocEx(ctypes.Structure):
            _fields_ = [
                ("FormatAndFlags", ctypes.c_ubyte),
                ("SessionTrack", ctypes.c_ubyte),
                ("Reserved2", ctypes.c_ubyte),
                ("Reserved3", ctypes.c_ubyte),
            ]

        request = ReadTocEx()
        # Format is the low 4 bits; the Msf flag is bit 7 and is left clear so
        # addresses come back as LBAs, which is what every calculation here
        # wants.
        request.FormatAndFlags = CDROM_READ_TOC_EX_FORMAT_TOC
        request.SessionTrack = 0

        out = ctypes.create_string_buffer(CDROM_TOC_SIZE)
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle, IOCTL_CDROM_READ_TOC_EX,
            ctypes.byref(request), ctypes.sizeof(request),
            out, CDROM_TOC_SIZE, ctypes.byref(returned), None,
        )
        if not ok:
            code = ctypes.get_last_error()
            hint = ""
            if code == 21:       # ERROR_NOT_READY
                hint = " - the drive reports no disc (or is still spinning up)"
            elif code == 5:      # ERROR_ACCESS_DENIED
                hint = " - something else holds the device exclusively"
            raise WinTocError("TOC query failed on %s: error %d%s"
                              % (path, code, hint))
        return out.raw[:returned.value]
    finally:
        kernel32.CloseHandle(handle)


def parse(raw: bytes) -> tuple[list[dict], int]:
    """Decode a ``CDROM_TOC`` into track dicts and the lead-out LBA.

    Each entry's address is a big-endian 32-bit LBA. The lead-out arrives as a
    pseudo-track numbered 0xAA and is returned separately rather than as a
    track, because it is where the disc ends, not something you can rip.
    """
    if len(raw) < 4:
        raise WinTocError("TOC response too short (%d bytes)" % len(raw))

    first, last = raw[2], raw[3]
    entries = (len(raw) - 4) // TRACK_DATA_SIZE

    tracks: list[dict] = []
    leadout = 0
    for i in range(entries):
        off = 4 + i * TRACK_DATA_SIZE
        control = raw[off + 1] & 0x0F
        number = raw[off + 2]
        lba = int.from_bytes(raw[off + 4:off + 8], "big", signed=True)
        if number == LEADOUT_TRACK:
            leadout = lba
            continue
        if number < first or number > last:
            continue
        tracks.append({
            "number": number,
            "start_lba": lba,
            "is_data": bool(control & CONTROL_DATA),
        })

    if not tracks:
        raise WinTocError("no tracks in the TOC - is there an audio disc in "
                          "the drive?")
    if not leadout:
        raise WinTocError("no lead-out in the TOC; track lengths cannot be "
                          "computed without it")
    return tracks, leadout


def read_toc(device: str = "D:"):
    """Read the TOC and build a :class:`cdrip.toc.Toc`.

    Track lengths are derived from the gaps between start addresses, with the
    lead-out closing the last one - the same arithmetic the cd-paranoia path
    uses, so both produce identical Toc objects and everything downstream
    (disc ID, AccurateRip identity, mixed-mode handling) is unchanged.
    """
    from .toc import Toc, Track

    if not available():
        raise WinTocError("this reader is Windows-only")

    entries, leadout = parse(_read_raw(device))
    entries.sort(key=lambda t: t["number"])

    starts = [t["start_lba"] for t in entries] + [leadout]
    tracks = []
    for i, entry in enumerate(entries):
        tracks.append(Track(
            number=entry["number"],
            start_lba=entry["start_lba"],
            length_frames=starts[i + 1] - entry["start_lba"],
            is_data=entry["is_data"],
        ))
    return Toc(device=device, tracks=tuple(tracks), leadout_lba=leadout)
