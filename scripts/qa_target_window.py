"""Minimal Win32 (EDIT control) target window for the IME probe check.

Launched as a SEPARATE process by ``scripts/win_ime_check.py`` so the probe is
genuinely cross-process. A plain ``EDIT`` control is used on purpose: it is a
classic USER32 control that owns a real IMM context, so a failure here is a
failure of the probe, not of the target.

The title carries the QA markers (``qa_`` / ``enter-router``) so
``scripts/qa_cleanup.ps1`` can identify and remove it if a run is interrupted.

    python scripts\\qa_target_window.py
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
from enter_router.platform import win32lib  # noqa: E402

TITLE = "qa_ime_target - enter-router"
CLASS = "EnterRouterQaImeWindow"

user32 = win32lib.dll("user32")
kernel32 = win32lib.dll("kernel32")

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_BORDER = 0x00800000
WS_VSCROLL = 0x00200000
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
SW_SHOW = 5
WM_DESTROY = 0x0002
WM_SETFOCUS = 0x0007

WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


edit_handle = {"hwnd": 0}
STATUS = os.path.join(REPO, "qa", "target_status.txt")
WM_IME_STARTCOMPOSITION = 0x010D
WM_IME_ENDCOMPOSITION = 0x010E
WM_IME_COMPOSITION = 0x010F
WM_IME_SETCONTEXT = 0x0281
IME_MSG_NAMES = {WM_IME_STARTCOMPOSITION: "STARTCOMPOSITION",
                 WM_IME_ENDCOMPOSITION: "ENDCOMPOSITION",
                 WM_IME_COMPOSITION: "COMPOSITION",
                 WM_IME_SETCONTEXT: "SETCONTEXT"}


def status(line: str) -> None:
    """Ground truth from INSIDE the target process (the probe cannot see it)."""
    try:
        os.makedirs(os.path.dirname(STATUS), exist_ok=True)
        with open(STATUS, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    print("TARGET " + line, flush=True)


_LAST: dict[str, object] = {"key": None}


def inproc_ime(full: bool = False, sample_only: bool = False) -> None:
    """Report what the TARGET process itself sees through IMM (in-process).

    ``sample_only`` is the 400ms timer path: it logs a line only when the
    composition length changes, so the file shows start/commit transitions of the
    composition without flooding.
    """
    import time
    imm = win32lib.dll("imm32")
    imm.ImmGetContext.restype = win32lib.HIMC
    imm.ImmGetContext.argtypes = (win32lib.HWND,)
    imm.ImmGetDefaultIMEWnd.restype = win32lib.HWND
    imm.ImmGetDefaultIMEWnd.argtypes = (win32lib.HWND,)
    imm.ImmGetOpenStatus.restype = win32lib.BOOL
    imm.ImmGetOpenStatus.argtypes = (win32lib.HIMC,)
    imm.ImmGetCompositionStringW.restype = ctypes.c_int32
    imm.ImmGetCompositionStringW.argtypes = (win32lib.HIMC, win32lib.DWORD,
                                             ctypes.c_void_p, win32lib.DWORD)
    for label, hwnd in (("top", int(user32.GetForegroundWindow() or 0)),
                        ("edit", edit_handle["hwnd"])):
        if not hwnd:
            continue
        himc = imm.ImmGetContext(hwnd)
        ime_wnd = int(imm.ImmGetDefaultIMEWnd(hwnd) or 0)
        if himc:
            open_status = int(imm.ImmGetOpenStatus(himc))
            size = int(imm.ImmGetCompositionStringW(himc, 0x0008, None, 0))
            key = (label, open_status, size)
            if sample_only and key == _LAST["key"]:
                return
            _LAST["key"] = key
            status(f"INPROC {label} himc=0x{himc:X} open={open_status} compstr={size} "
                   f"ime_wnd=0x{ime_wnd:X} t={time.time():.2f}")
        else:
            if sample_only and _LAST["key"] == (label, "null"):
                return
            _LAST["key"] = (label, "null")
            status(f"INPROC {label} himc=0 ime_wnd=0x{ime_wnd:X} t={time.time():.2f}")


def activate_japanese(hwnd: int, edit: int) -> None:
    """Load the Japanese IME for THIS thread and switch it to hiragana.

    Done in-process on purpose: a thread can only change its own input language,
    which is exactly why the card's cross-process read is the hard part. The
    status lines this prints are the ground truth the probe is compared against.
    """
    import time
    KLF_ACTIVATE = 0x00000001
    IME_CMODE_NATIVE = 0x0001
    IME_CMODE_FULLSHAPE = 0x0008
    user32.LoadKeyboardLayoutW.restype = win32lib.HANDLE
    user32.LoadKeyboardLayoutW.argtypes = (ctypes.c_wchar_p, wt.UINT)
    user32.ActivateKeyboardLayout.restype = win32lib.HANDLE
    user32.ActivateKeyboardLayout.argtypes = (win32lib.HANDLE, wt.UINT)
    user32.SetFocus(edit)
    hkl = user32.LoadKeyboardLayoutW("00000411", KLF_ACTIVATE)   # ja-JP
    status(f"HKL ja=0x{int(hkl or 0):X}")
    user32.ActivateKeyboardLayout(hkl, KLF_ACTIVATE)
    user32.SetFocus(edit)
    imm = win32lib.dll("imm32")
    himc = imm.ImmGetContext(edit)
    if not himc:
        status("IME_SET himc=0 (cannot switch)")
        return
    try:
        opened = bool(imm.ImmSetOpenStatus(himc, 1))
        conv = bool(imm.ImmSetConversionStatus(himc, IME_CMODE_NATIVE | IME_CMODE_FULLSHAPE, 0))
        mode = win32lib.DWORD(0)
        sentence = win32lib.DWORD(0)
        imm.ImmGetConversionStatus(himc, ctypes.byref(mode), ctypes.byref(sentence))
        status(f"IME_SET open={opened} conv={conv} mode=0x{mode.value:X} "
               f"open_now={int(imm.ImmGetOpenStatus(himc))} t={time.time():.2f}")
    finally:
        imm.ImmReleaseContext(edit, himc)


WM_TIMER = 0x0113
TIMER_ID = 7


def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_TIMER and int(wparam) == TIMER_ID:
        inproc_ime(sample_only=True)
        return 0
    if msg in IME_MSG_NAMES:
        status(f"MSG {IME_MSG_NAMES[msg]} hwnd=0x{int(hwnd):X}")
        if msg == WM_IME_COMPOSITION:
            inproc_ime()
    if msg == WM_SETFOCUS and edit_handle["hwnd"]:
        user32.SetFocus(edit_handle["hwnd"])
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main() -> int:
    hinst = kernel32.GetModuleHandleW(None)
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    wc = WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = WNDPROC(wndproc)
    wc.hInstance = hinst
    wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))   # IDC_ARROW
    wc.hbrBackground = wt.HBRUSH(6)                                 # COLOR_WINDOW + 1
    wc.lpszClassName = CLASS
    if not user32.RegisterClassW(ctypes.byref(wc)):
        print("TARGET_FAIL RegisterClassW err=%d" % kernel32.GetLastError(), flush=True)
        return 2

    hwnd = user32.CreateWindowExW(
        0, CLASS, TITLE, WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        200, 120, 720, 420, None, None, hinst, None)
    if not hwnd:
        print("TARGET_FAIL CreateWindowExW err=%d" % kernel32.GetLastError(), flush=True)
        return 3

    edit = user32.CreateWindowExW(
        0, "EDIT", "",
        WS_CHILD | WS_VISIBLE | WS_BORDER | ES_MULTILINE | ES_AUTOVSCROLL | WS_VSCROLL,
        10, 10, 680, 340, hwnd, None, hinst, None)
    edit_handle["hwnd"] = edit
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.UpdateWindow(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(edit)
    print("TARGET_READY hwnd=0x%X edit=0x%X" % (hwnd, edit), flush=True)
    status("READY hwnd=0x%X edit=0x%X" % (hwnd, edit))
    inproc_ime()
    activate_japanese(hwnd, edit)
    user32.SetTimer.restype = win32lib.ULONG_PTR
    user32.SetTimer.argtypes = (wt.HWND, win32lib.ULONG_PTR, wt.UINT, wt.HANDLE)
    user32.SetTimer(hwnd, win32lib.ULONG_PTR(TIMER_ID), 400, None)

    msg = wt.MSG()
    while True:
        ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret == 0 or ret == -1:
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
