"""Key/text injection + clipboard + foreground takeover (Windows adapter).

Hard constraints (research §2-e, 附録 6-7; measured — do not "simplify"):

* x64 ``INPUT`` is **40 bytes**. The ctypes union MUST include MOUSEINPUT and
  HARDWAREINPUT members: with a 32-byte layout SendInput returns 0 and sets
  error 87 (ERROR_INVALID_PARAMETER) while nothing is typed. ``input_size()`` /
  ``verify_layout()`` expose that check (win_check.ps1 runs it).
* SendInput returning 0 is the only failure signal we get; UIPI-blocked
  injection also returns 0 (research R6), so callers must verify by reading the
  target back (AiChatSink does).
* Foreground takeover that actually works (S3, measured): AttachThreadInput to
  BOTH the current foreground and the target, then BringWindowToTop ->
  SetActiveWindow -> SetForegroundWindow. Plain SetForegroundWindow returns
  False; AppActivate returns True but does not change the foreground.
* Clipboard: stash -> write -> act -> restore, with bounded retries for
  clipboard-busy (~3 attempts / 300ms).

Everything Win32 is imported INSIDE the functions so this module (and its pure
combo planner) stays importable on Linux for the unit tests.
"""
from __future__ import annotations

import ctypes
import logging
import time
from typing import Callable, Iterable

from enter_router.core import keys
from enter_router.platform import win32lib

log = logging.getLogger("enter_router.inject")

# --- Win32 constants -------------------------------------------------------
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Physical modifier VKs (left/right and the generic ones the hook may report)
# -> the combo modifier name they belong to.
MOD_FAMILY: dict[int, str] = {
    0x10: "shift", 0xA0: "shift", 0xA1: "shift",
    0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
    0x12: "alt", 0xA4: "alt", 0xA5: "alt",
    0x5B: "win", 0x5C: "win",
}


class FocusFailed(RuntimeError):
    """The S3 foreground sequence did not end with the target in front."""


_protos_ready = False


def _init_win32() -> None:
    """Declare every prototype used here (review round 1, items 1 and 3).

    ``GetClipboardData``/``GlobalAlloc``/``GlobalLock`` returned truncated
    handles without these, so ``clipboard_put`` always failed on real Windows and
    the mandatory sink fallback silently lost the text.
    """
    global _protos_ready
    if _protos_ready or not win32lib.IS_WINDOWS:
        return
    _protos_ready = True
    BOOL = win32lib.BOOL
    UINT = win32lib.UINT
    HWND = win32lib.HWND
    HANDLE = win32lib.HANDLE
    HGLOBAL = win32lib.HGLOBAL
    LPVOID = win32lib.LPVOID
    DWORD = win32lib.DWORD
    proto = win32lib.proto
    proto("user32", "SendInput", UINT, (UINT, ctypes.POINTER(INPUT), ctypes.c_int))
    proto("user32", "OpenClipboard", BOOL, (HWND,))
    proto("user32", "CloseClipboard", BOOL, ())
    proto("user32", "EmptyClipboard", BOOL, ())
    proto("user32", "GetClipboardData", HANDLE, (UINT,))
    proto("user32", "SetClipboardData", HANDLE, (UINT, HANDLE))
    proto("kernel32", "GlobalAlloc", HGLOBAL, (UINT, win32lib.SIZE_T))
    proto("kernel32", "GlobalLock", LPVOID, (HGLOBAL,))
    proto("kernel32", "GlobalUnlock", BOOL, (HGLOBAL,))
    proto("kernel32", "GlobalFree", HGLOBAL, (HGLOBAL,))
    proto("kernel32", "GetCurrentThreadId", DWORD, ())
    proto("kernel32", "GetLastError", DWORD, ())
    proto("user32", "GetForegroundWindow", HWND, ())
    proto("user32", "GetWindowThreadProcessId", DWORD, (HWND, ctypes.POINTER(DWORD)))
    proto("user32", "BringWindowToTop", BOOL, (HWND,))
    proto("user32", "SetActiveWindow", HWND, (HWND,))
    proto("user32", "SetForegroundWindow", BOOL, (HWND,))
    proto("user32", "AttachThreadInput", BOOL, (DWORD, DWORD, BOOL))


def _user32():
    _init_win32()
    return win32lib.dll("user32")


def _kernel32():
    _init_win32()
    return win32lib.dll("kernel32")
# --- ctypes structures (x64 INPUT is 40 bytes) -----------------------------
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    # explicit fixed-width types: c_long is 8 bytes on Linux and 4 on Windows,
    # which would silently change sizeof(INPUT) and break the 40-byte contract.
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
                ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16),
                ("wParamH", ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("union", _INPUTUNION)]


def input_size() -> int:
    return ctypes.sizeof(INPUT)


def verify_layout() -> bool:
    """x64 must be 40 bytes (see module docstring). Returns the check result."""
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    size = ctypes.sizeof(INPUT)
    if size != expected:
        log.error("SendInput INPUT layout is %d bytes, expected %d — injection "
                  "would fail with error 87 (research 附録 7)", size, expected)
        return False
    return True


# --- pure planner (Linux-testable) -----------------------------------------
def stray_modifiers(combo: str, held_modifiers: Iterable[int] = ()) -> list[int]:
    """Held modifier VKs that ``combo`` itself does not press (pure).

    These are the ones a synthesised key would otherwise inherit from the user's
    physical hand: ``ctrl+enter -> send -> enter`` arrives as Ctrl+Enter unless
    Ctrl is released for the duration of the injection.
    """
    mods, _key = keys.parse_combo(combo)
    needed = {MOD_FAMILY.get(m) for m in mods}
    return [vk for vk in dict.fromkeys(held_modifiers)
            if vk in MOD_FAMILY and MOD_FAMILY[vk] not in needed]


def plan_events(combo: str, held_modifiers: Iterable[int] = (),
                repeats: int = 1) -> list[tuple[int, bool]]:
    """The exact (vk, is_up) sequence ``send_combo`` feeds to SendInput.

    Pure so it can be unit-tested on Linux (tests/test_platform_pure.py).

    ``held_modifiers`` are the modifier VKs the user is *physically* holding. A
    held modifier that the combo itself does not press is released with an
    injected key-up first and pressed again afterwards: otherwise the target app
    still sees the physical modifier, so ``ctrl+enter -> send -> enter`` would
    arrive as Ctrl+Enter and the whole "same trigger, different key per profile"
    behaviour would be a lie (review round 1, item 4).

    Order: release stray modifiers -> modifier downs -> key down/up (xN) ->
    modifier ups in reverse -> restore the released modifiers. The restore is
    tentative: ``send_combo`` re-reads the physical state afterwards and undoes
    it if the user let go in the meantime (see ``_restore_if_still_down``).
    """
    mods, key = keys.parse_combo(combo)
    stray = stray_modifiers(combo, held_modifiers)
    events: list[tuple[int, bool]] = [(vk, True) for vk in stray]
    events.extend((m, False) for m in mods)
    for _ in range(max(1, repeats)):
        events.append((key, False))
        events.append((key, True))
    events.extend((m, True) for m in reversed(mods))
    events.extend((vk, False) for vk in stray)
    return events


def combo_to_events(combo: str) -> list[tuple[int, bool]]:
    """("ctrl+shift+enter") -> [(vk, is_up), ...] in press order (no restore)."""
    mods, key = keys.parse_combo(combo)
    events: list[tuple[int, bool]] = [(m, False) for m in mods]
    events.append((key, False))
    events.append((key, True))
    events.extend((m, True) for m in reversed(mods))
    return events


# --- SendInput primitives --------------------------------------------------
def _key_input(vk: int, up: bool, unicode_scan: int | None = None) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if unicode_scan is not None:
        flags |= KEYEVENTF_UNICODE
    item = INPUT(type=INPUT_KEYBOARD)
    item.union.ki = KEYBDINPUT(wVk=vk, wScan=(unicode_scan or 0), dwFlags=flags,
                               time=0, dwExtraInfo=0)
    return item


def _send(inputs: Iterable[INPUT]) -> int:
    items = list(inputs)
    if not items:
        return 0
    array = (INPUT * len(items))(*items)
    # pass the array itself: with argtypes=(UINT, POINTER(INPUT), c_int) ctypes
    # accepts an INPUT array, but byref(array) would be a pointer-to-array and
    # raise "expected LP_INPUT instance instead of pointer to INPUT_Array_N".
    sent = _user32().SendInput(len(items), array, ctypes.sizeof(INPUT))
    if sent != len(items):
        log.error("SendInput sent %d/%d events (err=%d) — injection blocked (UIPI?) "
                  "or the INPUT layout is wrong", sent, len(items),
                  _kernel32().GetLastError())
    return int(sent)


def send_vk(vk: int) -> int:
    """SendInput keydown+keyup for one virtual key; returns events sent (0/2)."""
    return _send([_key_input(vk, False), _key_input(vk, True)])


def _physically_down(vk: int) -> bool:
    """Is ``vk`` down right now (0x8000 bit of GetAsyncKeyState)?"""
    try:
        return bool(_user32().GetAsyncKeyState(vk) & 0x8000)
    except OSError:
        return True                 # unknown: assume still held (do not undo)


def _restore_if_still_down(stray: list[int]) -> None:
    """Undo the tentative restore if the user let go during the injection.

    ``plan_events`` presses the stray modifier again at the end so the user's
    hand state is unchanged. If the user released it in the milliseconds we were
    injecting, that press would leave the modifier logically stuck down (their
    own key-up already reached the app), so read back and release it.
    """
    if not stray:
        return
    for vk in stray:
        if not _physically_down(vk):
            log.info("modifier vk=0x%02X released during injection - not restoring", vk)
            _send([_key_input(vk, True)])


def send_combo(combo: str, held_modifiers: Iterable[int] = ()) -> int:
    """Synthesize one combo press (e.g. "shift+enter"). Returns events sent.

    ``held_modifiers`` == the user's physical modifier VKs at trigger time; they
    are neutralised for the duration of the injection (see plan_events).
    """
    try:
        plan = plan_events(combo, held_modifiers)
        stray = stray_modifiers(combo, held_modifiers)
    except ValueError:
        log.error("refusing to inject an unparseable combo (config validated?)")
        return 0
    sent = _send([_key_input(vk, up) for vk, up in plan])
    _restore_if_still_down(stray)
    return sent


def send_enter(times: int = 1, held_modifiers: Iterable[int] = ()) -> int:
    """Deliver plain Enters (the pending-Enter 'pass' path). Returns events sent."""
    copies = max(0, times)
    if not copies:
        return 0
    plan = plan_events("enter", held_modifiers, repeats=copies)
    sent = _send([_key_input(vk, up) for vk, up in plan])
    _restore_if_still_down(stray_modifiers("enter", held_modifiers))
    return sent


def send_text_unicode(text: str) -> int:
    """SendInput KEYEVENTF_UNICODE per UTF-16 code unit; returns events sent."""
    if not text:
        return 0
    units = text.encode("utf-16-le")
    items: list[INPUT] = []
    for i in range(0, len(units), 2):
        code = units[i] | (units[i + 1] << 8)
        items.append(_key_input(0, False, unicode_scan=code))
        items.append(_key_input(0, True, unicode_scan=code))
    sent = 0
    # chunk so one call cannot exceed SendInput's sanity limits
    for i in range(0, len(items), 64):
        sent += _send(items[i:i + 64])
    return sent


# --- clipboard -------------------------------------------------------------
def _clipboard_open(attempts: int = 3, delay_s: float = 0.1) -> bool:
    for _ in range(attempts):
        try:
            if _user32().OpenClipboard(None):
                return True
        except OSError:
            pass
        time.sleep(delay_s)
    return False


def clipboard_get() -> tuple[str, bool]:
    if not _clipboard_open():
        return "", False
    try:
        handle = _user32().GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return "", False
        ptr = _kernel32().GlobalLock(handle)
        if not ptr:
            return "", False
        try:
            return ctypes.wstring_at(ptr), True
        finally:
            _kernel32().GlobalUnlock(handle)
    except OSError:
        return "", False
    finally:
        _user32().CloseClipboard()


def clipboard_put(text: str) -> bool:
    if not _clipboard_open():
        log.error("clipboard busy: could not open the clipboard after retries")
        return False
    handle = None
    try:
        if not _user32().EmptyClipboard():
            return False
        size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
        handle = _kernel32().GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            log.error("clipboard: GlobalAlloc(%d) failed (err=%d)", size,
                      _kernel32().GetLastError())
            return False
        ptr = _kernel32().GlobalLock(handle)
        if not ptr:
            log.error("clipboard: GlobalLock failed (err=%d)", _kernel32().GetLastError())
            return False
        try:
            ctypes.memmove(ptr, ctypes.c_wchar_p(text), size)
        finally:
            _kernel32().GlobalUnlock(handle)
        # ownership of `handle` transfers to the clipboard on success
        if not _user32().SetClipboardData(CF_UNICODETEXT, handle):
            log.error("clipboard: SetClipboardData failed (err=%d)",
                      _kernel32().GetLastError())
            return False
        handle = None
        return True
    except OSError:
        log.exception("clipboard_put failed")
        return False
    finally:
        if handle:
            _kernel32().GlobalFree(handle)      # never leak an unowned HGLOBAL
        _user32().CloseClipboard()


#: How long the staged text must stay on the clipboard after Ctrl+V is queued.
#:
#: ``SendInput`` only *queues* the keystroke; the target reads the clipboard when
#: it processes ``WM_PASTE``, and Chrome does that asynchronously. Restoring in
#: the same millisecond (the previous behaviour) handed the target the PREVIOUS
#: clipboard content: QA-B item 5 measured ``pastes=1`` / ``status=ok`` but
#: ``value_len=41`` against ``expect=1274`` — the user's old 41-char buffer was
#: pasted twice. 350ms is the same order as ``hold_window_ms`` and is orders of
#: magnitude above the observed paste latency, while still returning the copy
#: buffer promptly.
PASTE_SETTLE_S = 0.35


def clipboard_stash_restore(text: str, fn: Callable[[], object],
                            *, settle_s: float = 0.0) -> object:
    """Stash the clipboard, put `text`, run fn(), restore in a finally block.

    ``settle_s`` keeps `text` on the clipboard for that long *after* ``fn()``
    returns. ``fn()`` is normally ``send_combo("ctrl+v")``, which does not wait
    for the target to consume the clipboard, so a zero settle restores too early
    and the target pastes stale content (see :data:`PASTE_SETTLE_S`).

    The restore is logged (count only, never content) so "we gave your copy
    buffer back" is observable in the log instead of being a promise.
    """
    previous, had_previous = clipboard_get()
    try:
        if not clipboard_put(text):
            raise OSError("clipboard busy: could not stage the text")
        result = fn()
        if settle_s > 0:
            time.sleep(settle_s)
        return result
    finally:
        if had_previous:
            if clipboard_put(previous):
                log.info("clipboard restore=ok chars=%d", len(previous))
            else:
                log.error("clipboard restore=failed chars=%d", len(previous))
        else:
            log.info("clipboard restore=skipped (previous content unreadable)")


def paste_from_clipboard(text: str, *, settle_s: float = PASTE_SETTLE_S) -> int:
    """One-event L2 paste: stash -> put -> Ctrl+V -> settle -> restore (research 2-c L2).

    The settle is part of the fix for QA-B item 5: the restore must happen after
    the target has processed the paste, not after SendInput merely queued it.
    """
    def do_paste() -> int:
        return send_combo("ctrl+v")
    sent = clipboard_stash_restore(text, do_paste, settle_s=settle_s)
    return sent if isinstance(sent, int) else 0


# --- foreground takeover (S3) ----------------------------------------------
def take_foreground(hwnd: int) -> bool:
    """Put `hwnd` in front using the measured S3 sequence; True when it worked.

    Unlike :func:`with_foreground_s3` this does NOT give the foreground back:
    the caller is about to paste into the window it just brought forward
    (AiChatSink, card §2). ``GetForegroundWindow`` is the only trustworthy
    readback — ``SetForegroundWindow`` returns False even when it works and
    ``AppActivate`` returns True without changing anything (research S3).
    """
    if not hwnd:
        return False
    u = _user32()
    k = _kernel32()
    current = u.GetForegroundWindow()
    if current == hwnd:
        return True
    cur_tid = u.GetWindowThreadProcessId(current, None) if current else 0
    target_tid = u.GetWindowThreadProcessId(hwnd, None)
    my_tid = k.GetCurrentThreadId()
    attached: list[int] = []
    try:
        for tid in (cur_tid, target_tid):
            if tid and tid != my_tid and u.AttachThreadInput(my_tid, tid, True):
                attached.append(tid)
        u.BringWindowToTop(hwnd)
        u.SetActiveWindow(hwnd)
        u.SetForegroundWindow(hwnd)
        time.sleep(0.02)
        return u.GetForegroundWindow() == hwnd
    except OSError:
        log.exception("foreground takeover failed hwnd=%s", hwnd)
        return False
    finally:
        for tid in attached:
            u.AttachThreadInput(my_tid, tid, False)


def with_foreground_s3(hwnd: int, fn: Callable[[], object],
                       settle_s: float = 0.02) -> object:
    """Run fn() with `hwnd` in front using the measured S3 sequence."""
    u = _user32()
    k = _kernel32()
    if not hwnd:
        raise FocusFailed("no target hwnd")
    current = u.GetForegroundWindow()
    cur_tid = u.GetWindowThreadProcessId(current, None) if current else 0
    target_tid = u.GetWindowThreadProcessId(hwnd, None)
    my_tid = k.GetCurrentThreadId()
    attached: list[int] = []
    try:
        for tid in (cur_tid, target_tid):
            if tid and tid != my_tid and u.AttachThreadInput(my_tid, tid, True):
                attached.append(tid)
        u.BringWindowToTop(hwnd)
        u.SetActiveWindow(hwnd)
        u.SetForegroundWindow(hwnd)
        time.sleep(settle_s)
        if u.GetForegroundWindow() != hwnd:
            raise FocusFailed(f"foreground takeover failed for hwnd={hwnd}")
        return fn()
    finally:
        for tid in attached:
            u.AttachThreadInput(my_tid, tid, False)
        if current and u.GetForegroundWindow() != current:
            u.SetForegroundWindow(current)
