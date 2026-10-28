"""Tray icon: pywin32-free ``Shell_NotifyIconW`` + a small Win32 message loop.

Design §7 S0 (and §1: the main thread owns tray + tkinter, the hook lives on its
own thread). Constraints honoured here:

* **No new dependency** — the shell API is reached through ``ctypes`` (pywin32 is
  installed but the design forbids adding anything, and ctypes keeps this module
  importable, side-effect free, on Linux where the unit tests run).
* The icon lives on a **hidden window** owned by a dedicated thread with its own
  message loop, so a blocking menu never stalls the key hook or tkinter.
* The window procedure never touches the controller: it turns a click into a
  command *name* and hands it to ``on_command``, which the UI runtime forwards to
  the Tk thread. That keeps AppController calls single-threaded on the UI side.
* State is visible in three redundant ways (design §7): icon colour, tooltip,
  and the first menu item. ``パニック停止`` is the first item and is reachable in
  one click from the icon.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("enter_router.ui.tray")

IS_WINDOWS = sys.platform == "win32"

# --- window messages ------------------------------------------------------
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1          # icon callback (lParam = mouse message)
WM_REFRESH = WM_APP + 2       # asked from another thread to repaint state
WM_OPEN_SETTINGS = WM_APP + 4  # asked from ANOTHER PROCESS (main.py --settings)

WINDOW_CLASS = "enter_router_tray_wnd"
WINDOW_TITLE = "enter-router"

# --- Shell_NotifyIcon -----------------------------------------------------
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

# --- menus ---------------------------------------------------------------
MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0000, 0x0800, 0x0001
TPM_RETURNCMD, TPM_NONOTIFY, TPM_RIGHTBUTTON = 0x0100, 0x0080, 0x0002
IDI_APPLICATION = 32512
BLACK_BRUSH = 4

# ---------------------------------------------------------------------------
# pure state → labels / colours / menu (unit-tested on Linux)
# ---------------------------------------------------------------------------
STATE_ORDER: tuple[str, ...] = ("running", "paused", "panic", "busy")

STATE_TEXTS: dict[str, str] = {
    "running": "有効",
    "paused": "一時停止",
    "panic": "パニック停止中",
    "busy": "送り先を実行中",
}
# COLORREF is 0x00BBGGRR
STATE_COLORS: dict[str, int] = {
    "running": 0x2EC522,     # green
    "paused": 0x15CCFA,      # amber
    "panic": 0x2626DC,       # red
    "busy": 0xF6823B,        # blue
}
STATE_DETAILS: dict[str, str] = {
    "running": "Enter の変換が動いています",
    "paused": "キーは素通しです（横取りしていません）",
    "panic": "横取りを停止しました。再開はメニューまたは設定画面から",
    "busy": "送り先の処理中です（キーは止まりません）",
}


def state_text(state: str) -> str:
    return STATE_TEXTS.get(state, state)


def state_color(state: str) -> int:
    return STATE_COLORS.get(state, STATE_COLORS["running"])


def status_tooltip(state: str, *, config_path: str = "", warnings: int = 0,
                   last_error: str = "") -> str:
    """Tooltip text (Shell_NotifyIcon szTip is 128 wide, keep it short)."""
    lines = [f"enter-router — {state_text(state)}", STATE_DETAILS.get(state, "")]
    if config_path:
        lines.append(f"設定: {config_path}")
    if warnings:
        lines.append(f"設定の警告 {warnings} 件（設定画面で確認）")
    if last_error:
        lines.append(f"直近のエラー: {last_error}")
    text = "\n".join(line for line in lines if line)
    return text[:127]


@dataclass(frozen=True)
class MenuItem:
    command: str
    label: str
    enabled: bool = True
    separator_after: bool = False


def menu_items(state: str) -> tuple[MenuItem, ...]:
    """Tray menu for ``state``; パニック停止 is always the first entry.

    ``panic`` stays visible (greyed) once panicked so the user can see *why* the
    keys are no longer intercepted instead of wondering where the item went.
    """
    running = state in ("running", "busy")
    return (
        MenuItem("panic", "パニック停止（キー横取りを止める）",
                 enabled=state != "panic", separator_after=True),
        MenuItem("settings", "設定を開く…"),
        MenuItem("toggle", "一時停止" if running else "再開",
                 enabled=state != "panic", separator_after=True),
        MenuItem("logs", "ログフォルダを開く…"),
        MenuItem("quit", "終了"),
    )


def command_for_id(command_id: int) -> str:
    items = menu_items("running")
    if 1 <= command_id <= len(items):
        return items[command_id - 1].command
    return ""


# ---------------------------------------------------------------------------
# Win32 plumbing (only ever imported/used on Windows)
# ---------------------------------------------------------------------------
class _API:
    """Lazily declared ctypes prototypes (pointers/HANDLEs must be typed)."""

    def __init__(self) -> None:
        from ctypes import wintypes as wt

        self.wt = wt
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        self.WNDPROC = WNDPROC

        self.WNDCLASSW = type("WNDCLASSW", (ctypes.Structure,), {"_fields_": [
            ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
            ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
            ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
        ]})

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HICON), ("szTip", ctypes.c_wchar * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", ctypes.c_wchar * 256), ("uVersion", wt.UINT),
                ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", wt.DWORD),
            ]

        self.NOTIFYICONDATAW = NOTIFYICONDATAW

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD),
                        ("yHotspot", wt.DWORD), ("hbmMask", wt.HBITMAP),
                        ("hbmColor", wt.HBITMAP)]

        self.ICONINFO = ICONINFO

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        self.RECT = RECT

        def proto(dll, name, restype, argtypes):
            fn = getattr(dll, name)
            fn.restype = restype
            fn.argtypes = list(argtypes)
            return fn

        self.user32 = user32
        self.shell32 = shell32
        self.gdi32 = gdi32
        self.kernel32 = kernel32

        self.RegisterClassW = proto(user32, "RegisterClassW", wt.ATOM,
                                    [ctypes.POINTER(self.WNDCLASSW)])
        self.CreateWindowExW = proto(user32, "CreateWindowExW", wt.HWND,
                                     [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wt.HWND, wt.HMENU,
                                      wt.HINSTANCE, wt.LPVOID])
        self.DefWindowProcW = proto(user32, "DefWindowProcW", LRESULT,
                                    [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM])
        self.DestroyWindow = proto(user32, "DestroyWindow", wt.BOOL, [wt.HWND])
        self.PostMessageW = proto(user32, "PostMessageW", wt.BOOL,
                                  [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM])
        self.PostQuitMessage = proto(user32, "PostQuitMessage", None, [ctypes.c_int])
        self.GetMessageW = proto(user32, "GetMessageW", wt.BOOL,
                                 [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT])
        self.TranslateMessage = proto(user32, "TranslateMessage", wt.BOOL,
                                      [ctypes.POINTER(wt.MSG)])
        self.DispatchMessageW = proto(user32, "DispatchMessageW", LRESULT,
                                      [ctypes.POINTER(wt.MSG)])
        self.CreatePopupMenu = proto(user32, "CreatePopupMenu", wt.HMENU, [])
        self.AppendMenuW = proto(user32, "AppendMenuW", wt.BOOL,
                                 [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR])
        self.TrackPopupMenu = proto(user32, "TrackPopupMenu", wt.BOOL,
                                    [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wt.HWND, ctypes.c_void_p])
        self.DestroyMenu = proto(user32, "DestroyMenu", wt.BOOL, [wt.HMENU])
        self.SetForegroundWindow = proto(user32, "SetForegroundWindow", wt.BOOL, [wt.HWND])
        self.FindWindowW = proto(user32, "FindWindowW", wt.HWND,
                                 [wt.LPCWSTR, wt.LPCWSTR])
        self.GetCursorPos = proto(user32, "GetCursorPos", wt.BOOL,
                                  [ctypes.POINTER(wt.POINT)])
        # NOTE: the second parameter is an int resource id (MAKEINTRESOURCE), so it
        # is declared as a void pointer rather than LPWSTR.
        self.LoadIconW = proto(user32, "LoadIconW", wt.HICON,
                               [wt.HINSTANCE, ctypes.c_void_p])
        self.CreateIconIndirect = proto(user32, "CreateIconIndirect", wt.HICON,
                                        [ctypes.POINTER(self.ICONINFO)])
        self.DestroyIcon = proto(user32, "DestroyIcon", wt.BOOL, [wt.HICON])
        self.FillRect = proto(user32, "FillRect", ctypes.c_int,
                              [wt.HDC, ctypes.POINTER(self.RECT), wt.HBRUSH])
        self.GetDC = proto(user32, "GetDC", wt.HDC, [wt.HWND])
        self.ReleaseDC = proto(user32, "ReleaseDC", ctypes.c_int, [wt.HWND, wt.HDC])

        self.CreateCompatibleDC = proto(gdi32, "CreateCompatibleDC", wt.HDC, [wt.HDC])
        self.DeleteDC = proto(gdi32, "DeleteDC", wt.BOOL, [wt.HDC])
        self.CreateCompatibleBitmap = proto(gdi32, "CreateCompatibleBitmap", wt.HBITMAP,
                                            [wt.HDC, ctypes.c_int, ctypes.c_int])
        self.CreateBitmap = proto(gdi32, "CreateBitmap", wt.HBITMAP,
                                  [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT,
                                   ctypes.c_void_p])
        self.SelectObject = proto(gdi32, "SelectObject", wt.HGDIOBJ,
                                  [wt.HDC, wt.HGDIOBJ])
        self.CreateSolidBrush = proto(gdi32, "CreateSolidBrush", wt.HBRUSH, [wt.DWORD])
        self.DeleteObject = proto(gdi32, "DeleteObject", wt.BOOL, [wt.HGDIOBJ])
        self.GetStockObject = proto(gdi32, "GetStockObject", wt.HGDIOBJ, [ctypes.c_int])

        self.Shell_NotifyIconW = proto(shell32, "Shell_NotifyIconW", wt.BOOL,
                                       [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)])
        self.GetModuleHandleW = proto(kernel32, "GetModuleHandleW", wt.HMODULE,
                                      [wt.LPCWSTR])


_api: "_API | None" = None


def _get_api() -> "_API":
    global _api
    if _api is None:
        _api = _API()
    return _api


_instance: "TrayIcon | None" = None
_wndproc = None


def _wnd_proc(hwnd, msg, wparam, lparam):     # pragma: no cover - Windows only
    if _instance is not None:
        return _instance.handle_message(hwnd, msg, wparam, lparam)
    return _get_api().DefWindowProcW(hwnd, msg, wparam, lparam)


def _solid_icon(colorref: int, size: int = 16) -> int:  # pragma: no cover - Windows only
    """A square icon of one colour, built with GDI (no Pillow, no icon files)."""
    api = _get_api()
    rect_t = api.RECT
    screen = api.GetDC(None)
    mem = api.CreateCompatibleDC(screen)
    color_bmp = api.CreateCompatibleBitmap(screen, size, size)
    mask_bmp = api.CreateBitmap(size, size, 1, 1, None)
    mask_dc = api.CreateCompatibleDC(screen)
    icon = 0
    try:
        old = api.SelectObject(mem, color_bmp)
        frame = api.CreateSolidBrush(colorref & 0x1F1F1F)    # darker rim
        inner = api.CreateSolidBrush(colorref)
        try:
            api.FillRect(mem, ctypes.byref(rect_t(0, 0, size, size)), frame)
            api.FillRect(mem, ctypes.byref(rect_t(2, 2, size - 2, size - 2)), inner)
        finally:
            api.DeleteObject(frame)
            api.DeleteObject(inner)
        api.SelectObject(mem, old)
        # AND mask all zeros => the colour bitmap is used as-is (fully opaque)
        old_m = api.SelectObject(mask_dc, mask_bmp)
        api.FillRect(mask_dc, ctypes.byref(rect_t(0, 0, size, size)),
                     api.GetStockObject(BLACK_BRUSH))
        api.SelectObject(mask_dc, old_m)
        info = api.ICONINFO(1, 0, 0, mask_bmp, color_bmp)
        icon = api.CreateIconIndirect(ctypes.byref(info))
    finally:
        api.DeleteObject(color_bmp)
        api.DeleteObject(mask_bmp)
        api.DeleteDC(mask_dc)
        api.DeleteDC(mem)
        api.ReleaseDC(None, screen)
    return icon


class TrayIcon:
    """Shell_NotifyIcon tray icon driven by a dedicated message-loop thread."""

    def __init__(self, on_command: Callable[[str], None], uid: int = 1) -> None:
        self._on_command = on_command
        self._uid = uid
        self._hwnd = 0
        self._icons: dict[str, int] = {}
        self._fallback_icon = 0
        self._state = "running"
        self._tooltip = "enter-router"
        self._visible = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._error = ""

    # -- public -----------------------------------------------------------
    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str:
        return self._error

    def start(self, timeout: float = 8.0) -> bool:
        if not IS_WINDOWS:
            self._error = "tray requires Windows"
            return False
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        ok = self._ready.wait(timeout)
        if not ok:
            self._error = self._error or "tray did not become ready"
        return ok and self._visible

    def refresh(self, state: str | None = None, tooltip: str | None = None) -> None:
        """Safe from any thread: store the desired state, wake the loop."""
        with self._lock:
            if state is not None:
                self._state = state
            if tooltip is not None:
                self._tooltip = tooltip
            hwnd = self._hwnd
        if hwnd:
            _get_api().PostMessageW(hwnd, WM_REFRESH, 0, 0)

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            hwnd = self._hwnd
        if not hwnd:
            return
        _get_api().PostMessageW(hwnd, WM_CLOSE, 0, 0)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    # -- message-loop thread ---------------------------------------------
    def _run(self) -> None:                       # pragma: no cover - Windows only
        global _instance
        api = _get_api()
        try:
            hinst = api.GetModuleHandleW(None)
            class_name = WINDOW_CLASS
            wndproc = api.WNDPROC(_wnd_proc)
            global _wndproc
            _wndproc = wndproc        # keep a reference: GC would crash the loop
            wc = api.WNDCLASSW()
            wc.lpfnWndProc = wndproc
            wc.hInstance = hinst
            wc.lpszClassName = class_name
            api.RegisterClassW(ctypes.byref(wc))   # 1410 = already registered: fine
            hwnd = api.CreateWindowExW(0, class_name, WINDOW_TITLE, 0,
                                       0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                self._error = f"CreateWindowExW failed (err={ctypes.get_last_error()})"
                log.error(self._error)
                self._ready.set()
                return
            self._hwnd = int(hwnd)
            _instance = self
            self._fallback_icon = int(api.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION)) or 0)
            if not self._add_icon():
                self._ready.set()
                return
            self._ready.set()
            msg = api.wt.MSG()
            while True:
                ret = api.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                api.TranslateMessage(ctypes.byref(msg))
                api.DispatchMessageW(ctypes.byref(msg))
        except BaseException as exc:              # never kill the app over the tray
            self._error = f"{type(exc).__name__}: {exc}"
            log.exception("tray thread failed")
            self._ready.set()
        finally:
            _instance = None
            self._hwnd = 0

    def _icon_for(self, state: str) -> int:       # pragma: no cover - Windows only
        if not IS_WINDOWS:
            return 0
        if state not in self._icons:
            icon = _solid_icon(state_color(state))
            self._icons[state] = int(icon) if icon else self._fallback_icon
        return self._icons[state] or self._fallback_icon

    def _notify_data(self) -> "ctypes.Structure":  # pragma: no cover - Windows only
        api = _get_api()
        data = api.NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(api.NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = self._uid
        data.uCallbackMessage = WM_TRAY
        data.szTip = self._tooltip[:127]
        data.hIcon = self._icon_for(self._state)
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        return data

    def _add_icon(self) -> bool:                  # pragma: no cover - Windows only
        api = _get_api()
        data = self._notify_data()
        if not api.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            self._error = f"Shell_NotifyIconW(NIM_ADD) failed (err={ctypes.get_last_error()})"
            log.error(self._error)
            return False
        self._visible = True
        log.info("tray icon added (hwnd=%s state=%s)", self._hwnd, self._state)
        return True

    def handle_message(self, hwnd, msg, wparam, lparam):   # pragma: no cover - Windows only
        api = _get_api()
        if msg == WM_TRAY:
            mouse = int(lparam) & 0xFFFF
            if mouse in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
            elif mouse in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._emit("settings")
            return 0
        if msg == WM_OPEN_SETTINGS:
            self._emit("settings")
            return 0
        if msg == WM_REFRESH:
            with self._lock:
                state, tooltip = self._state, self._tooltip
            data = self._notify_data()
            data.szTip = tooltip[:127]
            api.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
            return 0
        if msg == WM_CLOSE:
            api.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._notify_data()))
            self._visible = False
            api.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            api.PostQuitMessage(0)
            return 0
        return api.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self) -> None:                 # pragma: no cover - Windows only
        api = _get_api()
        with self._lock:
            state = self._state
        menu = api.CreatePopupMenu()
        if not menu:
            return
        try:
            for index, item in enumerate(menu_items(state), start=1):
                flags = MF_STRING | (0 if item.enabled else MF_GRAYED)
                api.AppendMenuW(menu, flags, index, item.label)
                if item.separator_after:
                    api.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            point = api.wt.POINT()
            api.GetCursorPos(ctypes.byref(point))
            # Foreground before the popup, WM_NULL after: without this the menu
            # does not dismiss when the user clicks elsewhere (documented Win32
            # requirement for tray menus).
            api.SetForegroundWindow(self._hwnd)
            chosen = api.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                                        point.x, point.y, 0, self._hwnd, None)
            api.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            command = command_for_id(int(chosen or 0))
            if command:
                self._emit(command)
        finally:
            api.DestroyMenu(menu)

    def _emit(self, command: str) -> None:
        try:
            self._on_command(command)
        except BaseException:
            log.exception("tray command %s failed", command)

    @property
    def icon_handle(self) -> int:
        """The HICON currently registered (0 = the shell has no icon of ours)."""
        with self._lock:
            state = self._state
        return self._icon_for(state)


def post_open_settings() -> bool:
    """Ask a running instance to open its settings window (``main.py --settings``).

    Exists because Windows 11 keeps *new* tray icons in the overflow flyout: the
    icon is not on screen until the user pins it, so there must be a second,
    reliable way to reach the window from outside the process.
    """
    if not IS_WINDOWS:
        return False
    api = _get_api()
    hwnd = api.FindWindowW(WINDOW_CLASS, WINDOW_TITLE)
    if not hwnd:
        return False
    return bool(api.PostMessageW(hwnd, WM_OPEN_SETTINGS, 0, 0))


def tray_available() -> bool:
    """True when a tray icon can be created here (used to pick the UI mode)."""
    return IS_WINDOWS
