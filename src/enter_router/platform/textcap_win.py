"""Text capture adapters (Windows). See core/capture.py for the ordering policy.

Chain order is pure (core/capture.py); this module only performs the steps:

* ``read_focused_text``   ("uia_focused"): read the focused control's text.
  pywin32 ships no UI Automation client and the research card measured that a
  browser's focused node is a Gecko/Blink *Document*, not an Edit (R11), so UIA
  cannot identify a chat input box. What we can do reliably and cheaply is ask
  the focused native control for its text (WM_GETTEXT, with a timeout so a hung
  app cannot freeze the caller). Browsers legitimately return None here — the
  clipboard route below is what makes them viable.
* ``clipboard_select_copy`` ("clipboard_select_copy"): Ctrl+A -> Ctrl+C into a
  stash, then restore the clipboard. Measured PASS (research 2-e).
* ``get_document_url``: best-effort MSAA ``accValue`` of the browser document
  (that is where the URL lives). Strictly validated: anything that does not look
  like a URL is returned as None, because a wrong URL would make a URL-scoped
  profile fire in the wrong place (design §4.3: never guess).

Neither step logs the captured text (research R9). Empty string == failure.
"""
from __future__ import annotations

import ctypes
import logging
import time

from enter_router.platform import inject, win32lib

log = logging.getLogger("enter_router.capture")

WM_GETTEXT = 0x000D
SMTO_ABORTIFHUNG = 0x0002
MAX_CAPTURE_CHARS = 8192

OBJID_CLIENT = 0xFFFFFFFC
CHILDID_SELF = 0
VT_I4 = 3
S_OK = 0

_protos_ready = False


def _init_win32() -> None:
    """Declare prototypes (pointer/handle truncation bug: review round 1)."""
    global _protos_ready
    if _protos_ready or not win32lib.IS_WINDOWS:
        return
    _protos_ready = True
    BOOL = win32lib.BOOL
    DWORD = win32lib.DWORD
    proto = win32lib.proto
    proto("user32", "GetGUIThreadInfo", BOOL,
          (DWORD, ctypes.POINTER(GUITHREADINFO)))
    proto("user32", "SendMessageTimeoutW", win32lib.LRESULT,
          (win32lib.HWND, win32lib.UINT, win32lib.WPARAM, ctypes.c_void_p,
           win32lib.UINT, win32lib.UINT, ctypes.POINTER(win32lib.ULONG_PTR)))
    proto("oleacc", "AccessibleObjectFromWindow", ctypes.c_long,
          (win32lib.HWND, DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)))
    proto("oleaut32", "SysFreeString", None, (ctypes.c_void_p,))


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


# {618736E0-3C3D-11CF-810C-00AA00389B71} — IID_IAccessible
IID_IACCESSIBLE = GUID(0x618736E0, 0x3C3D, 0x11CF,
                       (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71))


class VARIANT_I4(ctypes.Structure):
    """Minimal VARIANT carrying a VT_I4 child id (24 bytes, 8-byte aligned)."""
    _fields_ = [("vt", ctypes.c_ushort), ("wReserved", ctypes.c_ushort * 3),
                ("value", ctypes.c_uint64 * 2)]

    @classmethod
    def child(cls, child_id: int = CHILDID_SELF) -> "VARIANT_I4":
        v = cls()
        v.vt = VT_I4
        v.value[0] = child_id
        return v


#: ONE definition for the whole package (see win32lib.GUITHREADINFO: two local
#: copies made GetGUIThreadInfo's prototype registration order-dependent, which
#: killed this capture step with ArgumentError on every call).
GUITHREADINFO = win32lib.GUITHREADINFO


def focused_hwnd() -> int:
    """HWND of the focused control in the foreground thread (0 when unknown)."""
    try:
        _init_win32()
        u = win32lib.dll("user32")
        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not u.GetGUIThreadInfo(0, ctypes.byref(gti)):
            return 0
        return int(gti.hwndFocus or 0)
    except OSError:
        return 0


def read_focused_text() -> str | None:
    """uia_focused step. None on any failure/ambiguity; never raises."""
    try:
        _init_win32()
        hwnd = focused_hwnd()
        if not hwnd:
            return None
        u = win32lib.dll("user32")
        buf = ctypes.create_unicode_buffer(MAX_CAPTURE_CHARS)
        result = win32lib.ULONG_PTR(0)
        ok = u.SendMessageTimeoutW(
            hwnd, WM_GETTEXT, win32lib.WPARAM(MAX_CAPTURE_CHARS),
            ctypes.cast(buf, ctypes.c_void_p), SMTO_ABORTIFHUNG, 100,
            ctypes.byref(result))
        if not ok:
            return None
        text = buf.value
        return text or None
    except OSError:
        log.debug("read_focused_text failed", exc_info=True)
        return None
    except BaseException:
        log.exception("read_focused_text raised unexpectedly")
        return None


def clipboard_select_copy(restore: bool = True) -> str | None:
    """Ctrl+A -> Ctrl+C, read the clipboard, restore it, then DESELECT.

    The deselect matters: Ctrl+A leaves the whole text selected, so the user
    would keep typing over it (measured on hardware: after an Enter x3 the
    Notepad text stayed highlighted). A bare ``Right`` collapses the selection to
    its end and does nothing else — the caret ends up at the end of the text,
    which is also where a following paste belongs.

    None on failure (the caller falls through the capture chain).
    """
    try:
        previous, had_previous = inject.clipboard_get()
        inject.send_combo("ctrl+a")
        time.sleep(0.03)
        inject.send_combo("ctrl+c")
        time.sleep(0.08)
        text, ok = inject.clipboard_get()
        if restore and had_previous:
            inject.clipboard_put(previous)
        # Always collapse the selection, even when the copy came back empty: the
        # user's text is selected at this point either way.
        inject.send_combo("right")
        if not ok or not text:
            return None
        return text
    except OSError:
        log.debug("clipboard_select_copy failed", exc_info=True)
        return None
    except BaseException:
        log.exception("clipboard_select_copy raised unexpectedly")
        return None


def _looks_like_url(value: str) -> bool:
    if not value:
        return False
    v = value.strip()
    return "://" in v and len(v) < 2048 and " " not in v


def get_document_url(hwnd: int) -> str | None:
    """Best-effort browser URL for `hwnd` via MSAA accValue; None when unsure.

    Not verified on hardware from this card (QA card owns the browser run).
    A non-URL string is rejected so a URL-scoped profile can never match on a
    guess (design §4.3).
    """
    pacc = ctypes.c_void_p()
    try:
        _init_win32()
        if not hwnd:
            return None
        oleacc = win32lib.dll("oleacc")
        hr = oleacc.AccessibleObjectFromWindow(
            hwnd, ctypes.c_uint32(OBJID_CLIENT),
            ctypes.cast(ctypes.byref(IID_IACCESSIBLE), ctypes.c_void_p),
            ctypes.byref(pacc))
        if hr != S_OK or not pacc:
            return None
        vtable = ctypes.cast(pacc, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_acc_value = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, VARIANT_I4,
            ctypes.POINTER(ctypes.c_void_p))(vtable[11])
        bstr = ctypes.c_void_p()
        hr = get_acc_value(pacc, VARIANT_I4.child(), ctypes.byref(bstr))
        if hr != S_OK or not bstr.value:
            return None
        value = ctypes.wstring_at(bstr.value)
        win32lib.dll("oleaut32").SysFreeString(bstr)
        return value if _looks_like_url(value) else None
    except OSError:
        log.debug("MSAA URL lookup failed", exc_info=True)
        return None
    except BaseException:
        log.exception("MSAA URL lookup raised unexpectedly")
        return None
    finally:
        if pacc:
            try:
                vtable = ctypes.cast(
                    pacc, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
                release(pacc)
            except BaseException:
                pass
