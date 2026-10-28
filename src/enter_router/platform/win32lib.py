"""Typed Win32 prototypes for every call ``platform/`` makes.

Why this module exists (review round 1, items 1 and 3): ctypes converts an
argument with no ``argtypes`` declared as a C ``int`` and defaults ``restype`` to
``c_int``. On x64 that truncates pointers and HANDLEs, which produced two real
defects on hardware:

* ``CallNextHookEx(None, n_code, wparam, lparam)`` raised
  ``OverflowError: int too long to convert`` for the 64-bit ``lparam`` - on every
  key press, in the try branch AND again in the except branch, so the exception
  escaped the hook proc and the hook chain of every other application broke.
* ``GlobalAlloc``/``GlobalLock``/``GetClipboardData`` returned truncated
  HGLOBALs, so ``clipboard_put`` always failed - which silently disabled
  ClipboardSink, the mandatory HermesSink fallback and the AiChat paste path.

Rule: a Win32 function used by this package is declared here (``proto``) or in
the adapter's own ``_init_win32()`` - exactly once, before first use, never
implicitly. ``IS_WINDOWS`` keeps every module importable on Linux, where the
unit tests run.

Note on ``ctypes.windll``: the loader caches one Library instance per DLL name,
so prototypes set here persist for every later ``ctypes.windll.user32.<fn>``
access in the process.
"""
from __future__ import annotations

import ctypes
import sys

IS_WINDOWS = sys.platform == "win32"

# Fixed-width aliases: c_long is 8 bytes on Linux and 4 on Windows, so never use
# the platform-dependent spellings for anything that crosses the ABI.
BOOL = ctypes.c_int
DWORD = ctypes.c_uint32
UINT = ctypes.c_uint32
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
SHORT = ctypes.c_int16
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

HANDLE = ctypes.c_void_p
HWND = ctypes.c_void_p
HHOOK = ctypes.c_void_p
HGLOBAL = ctypes.c_void_p
HIMC = ctypes.c_void_p
LPVOID = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p
LPWSTR = ctypes.c_wchar_p


def dll(name: str):
    """``ctypes.windll.<name>`` or a loud error (never touched off-Windows)."""
    if not IS_WINDOWS:
        raise OSError(f"{name} is only available on Windows")
    return getattr(ctypes.windll, name)


def proto(dll_name: str, name: str, restype, argtypes) -> None:
    """Declare one function's real prototype. Idempotent, no-op off-Windows."""
    if not IS_WINDOWS:
        return
    fn = getattr(dll(dll_name), name)
    fn.restype = restype
    fn.argtypes = list(argtypes)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32),
                ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class GUITHREADINFO(ctypes.Structure):
    """``tagGUITHREADINFO`` — ONE definition for the whole package.

    Two modules need it (``ime.py`` for the hwndFocus/hwndCaret fallback,
    ``textcap_win.py`` for the focused control). Each used to declare its own
    class and register ``GetGUIThreadInfo`` with its own ``POINTER(...)``: the
    loader caches one ``user32`` instance, so the LAST registration won and the
    other module's ``byref(GUITHREADINFO)`` was rejected at runtime with
    ``ArgumentError: expected LP_GUITHREADINFO`` (measured: the uia_focused
    capture step died on every capture, silently falling back to the clipboard).
    Defining it here, once, makes the registration order irrelevant.
    """

    _fields_ = [("cbSize", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("hwndActive", ctypes.c_void_p), ("hwndFocus", ctypes.c_void_p),
                ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p),
                ("rcCaret", RECT)]


def ptr(value: int | None) -> ctypes.c_void_p:
    """A raw pointer/handle as ``c_void_p`` (never as a truncated int)."""
    return ctypes.c_void_p(value)
