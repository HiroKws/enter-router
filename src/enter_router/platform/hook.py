"""WH_KEYBOARD_LL hook + message loop (Windows adapter).

Hard constraints (research.md §2-a; do not "optimize" any of these away):

* ``SetWindowsHookExW(WH_KEYBOARD_LL, proc, 0, 0)`` + a message loop on the
  installing thread ("called in the context of the thread that installed it").
  The loop is *required*: LL callbacks are delivered inside GetMessage.
* The OS silently unhooks slow callbacks (LowLevelHooksTimeout, max 1000ms on
  Win10 1709+). The callback budget is therefore "decide + measure + return";
  the heavy work lives on the worker thread.
* Swallow = return 1. Everything else MUST go through CallNextHookEx or other
  apps' low-level hooks break.
* Injected events (LLKHF_INJECTED) are reported to the policy layer as
  ``KeyEvent.injected`` so it can ignore them (rule0 / I4). The hook itself does
  not swallow them.
* Catch EVERY exception inside the callback (outermost try/except -> pass the
  key through). An exception escaping the proc gets the hook removed by the OS,
  which looks exactly like "the keyboard stopped working".
* Watchdog contract: the loop thread refreshes a monotonic heartbeat on every
  WM_TIMER tick (200ms) and on every callback; app/watchdog.py kills the
  process when the heartbeat goes stale (process death removes the hook).
* Slow-callback accounting is delegated: the callback reports its own duration
  to ``safety.observe(ms)``; the app's SafetyMonitor decides when to unhook
  (safety.callback_overbudget_* / callback_hard_limit_ms).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time
from typing import Callable

from enter_router.core import keys
from enter_router.core.events import KeyEvent
from enter_router.platform import win32lib

log = logging.getLogger("enter_router.hook")

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_TIMER, WM_QUIT = 0x0113, 0x0012
WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN = 0x0201, 0x0204, 0x0207
# App-private "run your on_wake handler" message (see wake()).
WM_APP_WAKE = 0x8000 + 1

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLKHF_ALTDOWN = 0x20
LLKHF_UP = 0x80

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_ESCAPE = 0x1B

HEARTBEAT_TIMER_MS = 200

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t

HookDecisionFn = Callable[[KeyEvent], bool]  # True = swallow (return 1)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.c_ulong), ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ULONG_PTR)]


# ctypes.WINFUNCTYPE(...) is a FACTORY: assign it to get the callback type, then
# call it with the Python function. (Subclassing it raises at import time.)
# WINFUNCTYPE only exists on Windows, and this module must stay importable on
# Linux for the unit tests, so fall back to CFUNCTYPE (never used off-Windows).
_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
HOOKPROC = _WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

_protos_ready = False


def _init_win32() -> None:
    """Declare every prototype this module uses (review round 1, item 1).

    Without ``argtypes`` ctypes converts ``lparam`` as a C ``int``: the 64-bit
    pointer overflows on every key press, and the exception then escaped the hook
    callback (the except branch called the same broken function again).
    """
    global _protos_ready
    if _protos_ready or not win32lib.IS_WINDOWS:
        return
    _protos_ready = True
    BOOL = win32lib.BOOL
    DWORD = win32lib.DWORD
    HANDLE = win32lib.HANDLE
    HHOOK = win32lib.HHOOK
    HWND = win32lib.HWND
    proto = win32lib.proto
    proto("user32", "CallNextHookEx", LRESULT, (HHOOK, ctypes.c_int, WPARAM, LPARAM))
    proto("user32", "SetWindowsHookExW", HHOOK,
          (ctypes.c_int, HOOKPROC, HANDLE, DWORD))
    proto("user32", "UnhookWindowsHookEx", BOOL, (HHOOK,))
    proto("user32", "GetMessageW", ctypes.c_int,
          (ctypes.POINTER(wt.MSG), HWND, win32lib.UINT, win32lib.UINT))
    proto("user32", "TranslateMessage", BOOL, (ctypes.POINTER(wt.MSG),))
    proto("user32", "DispatchMessageW", LRESULT, (ctypes.POINTER(wt.MSG),))
    proto("user32", "PostThreadMessageW", BOOL,
          (DWORD, win32lib.UINT, WPARAM, LPARAM))
    proto("user32", "SetTimer", win32lib.ULONG_PTR,
          (HWND, win32lib.ULONG_PTR, win32lib.UINT, HANDLE))
    proto("user32", "KillTimer", BOOL, (HWND, win32lib.ULONG_PTR))
    proto("user32", "GetAsyncKeyState", win32lib.SHORT, (ctypes.c_int,))
    proto("user32", "GetForegroundWindow", HWND, ())
    proto("user32", "GetWindowTextLengthW", ctypes.c_int, (HWND,))
    proto("user32", "GetWindowTextW", ctypes.c_int, (HWND, win32lib.LPWSTR, ctypes.c_int))
    proto("user32", "GetWindowThreadProcessId", DWORD, (HWND, ctypes.POINTER(DWORD)))
    proto("kernel32", "GetCurrentThreadId", DWORD, ())
    proto("kernel32", "OpenProcess", HANDLE, (DWORD, BOOL, DWORD))
    proto("kernel32", "CloseHandle", BOOL, (HANDLE,))
    proto("kernel32", "QueryFullProcessImageNameW", BOOL,
          (HANDLE, DWORD, win32lib.LPWSTR, ctypes.POINTER(DWORD)))
    proto("kernel32", "GetLastError", DWORD, ())


def _call_next(n_code, wparam, lparam) -> int:
    """``CallNextHookEx`` that can never raise (the chain must not break).

    Every callback exit path goes through here, including the except branch: a
    second failure inside error handling would escape the proc and get our hook
    silently removed by the OS.
    """
    _init_win32()
    try:
        return int(ctypes.windll.user32.CallNextHookEx(None, n_code, wparam, lparam))
    except BaseException:
        log.exception("CallNextHookEx failed (swallowing this event)")
        return 0


class HookHandle:
    """Control block for a running hook. Created by install(), which blocks."""

    def __init__(self) -> None:
        self.thread_id: int | None = None
        self.installed = False
        self.stopped = threading.Event()
        self.stop_reason = ""

    def uninstall(self, reason: str = "uninstall") -> None:
        """Ask the loop thread to unhook and exit (thread-safe, idempotent)."""
        self.stop_reason = self.stop_reason or reason
        if self.thread_id:
            _init_win32()
            try:
                posted = ctypes.windll.user32.PostThreadMessageW(
                    self.thread_id, WM_QUIT, 0, 0)
                if not posted:
                    log.warning("PostThreadMessageW(WM_QUIT) not delivered (thread %d "
                                "queue not ready?)", self.thread_id)
            except BaseException:
                log.exception("PostThreadMessageW(WM_QUIT) failed")
        self.stopped.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.stopped.wait(timeout)


class HookUnavailable(RuntimeError):
    """SetWindowsHookExW failed (or this is not Windows)."""


_current: HookHandle | None = None
_current_lock = threading.Lock()


def current_handle() -> HookHandle | None:
    with _current_lock:
        return _current


def request_stop(reason: str = "stop") -> None:
    handle = current_handle()
    if handle is not None:
        handle.uninstall(reason)


def wake() -> bool:
    """Ask the installed hook thread to run its ``on_wake`` handler.

    Injection must never run on a background thread while the hook callback can
    block on the same lock: SendInput from another thread waits for the LL hook
    to consume the injected events, so a lock held across SendInput deadlocks
    the hook until the OS timeout (measured 617ms, review round 1 item 2).
    The app therefore posts its "deliver now" work to the hook thread with this,
    which has a message loop and is the single injector while it is installed.
    """
    handle = current_handle()
    if handle is None or not handle.installed or not handle.thread_id:
        return False
    _init_win32()
    try:
        return bool(ctypes.windll.user32.PostThreadMessageW(
            handle.thread_id, WM_APP_WAKE, 0, 0))
    except BaseException:
        log.exception("wake: PostThreadMessageW failed")
        return False


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _async_mods() -> tuple[bool, bool, bool, bool]:
    """(ctrl, alt, shift, win) from the system-wide key state."""
    _init_win32()
    u = ctypes.windll.user32
    down = 0x8000

    def is_down(vk: int) -> bool:
        return bool(u.GetAsyncKeyState(vk) & down)

    win = is_down(VK_LWIN) or is_down(VK_RWIN)
    return is_down(VK_CONTROL), is_down(VK_MENU), is_down(VK_SHIFT), win


def _observe(safety: object, elapsed_ms: float) -> None:
    observe = getattr(safety, "observe", None)
    if callable(observe):
        try:
            observe(elapsed_ms)
        except BaseException:      # never let bookkeeping break the hook
            log.exception("safety.observe() failed")


def _heartbeat(safety: object) -> None:
    beat = getattr(safety, "heartbeat", None)
    if callable(beat):
        try:
            beat()
        except BaseException:
            log.exception("safety.heartbeat() failed")


def install(decide: HookDecisionFn, on_panic: Callable[[], None],
            safety: "object", on_mouse: Callable[[], None] | None = None,
            on_wake: Callable[[], None] | None = None) -> HookHandle:
    """Install the LL hook(s) and run the message loop on the CALLING thread.

    Blocks until ``uninstall()``/``request_stop()``. Returns the handle after the
    loop has ended. Raises HookUnavailable when the hook cannot be installed.
    ``on_mouse`` is optional (additive): a mouse-button press is an ordering
    interrupt for a held Enter (design §3.5-1).
    ``on_wake`` is optional (additive): run on the hook thread when another
    thread calls ``wake()`` — the single-injector routing from review item 2.
    """
    global _current
    _init_win32()
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        log.warning("32-bit Python detected; the x64 INPUT layout comments do not apply")

    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    handle = HookHandle()
    handle.thread_id = k.GetCurrentThreadId()

    def keyboard_proc(n_code, wparam, lparam):
        # Outermost guard: an exception escaping here gets the hook removed by
        # the OS (safe requirement §6-1). Every exit path goes through
        # _call_next(), which itself cannot raise.
        started = time.perf_counter()
        try:
            if n_code < 0:
                return _call_next(n_code, wparam, lparam)
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            flags = info.flags
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
                ctrl, alt, shift, win = _async_mods()
                ev = KeyEvent(
                    vk=int(info.vkCode),
                    kind="up" if wparam in (WM_KEYUP, WM_SYSKEYUP) else "down",
                    t_ms=_now_ms(),
                    ctrl=ctrl,
                    shift=shift,
                    alt=alt,
                    win=win,
                    injected=bool(flags & LLKHF_INJECTED),
                    os_repeat=False,
                )
                _heartbeat(safety)
                swallow = bool(decide(ev))
                if swallow:
                    return 1
            return _call_next(n_code, wparam, lparam)
        except BaseException:
            log.exception("keyboard hook callback failed — passing the key through")
            return _call_next(n_code, wparam, lparam)
        finally:
            _observe(safety, (time.perf_counter() - started) * 1000.0)

    def mouse_proc(n_code, wparam, lparam):
        try:
            if n_code >= 0 and wparam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
                if on_mouse is not None:
                    on_mouse()
        except BaseException:
            log.exception("mouse hook callback failed")
        return _call_next(n_code, wparam, lparam)

    keyboard_cb = HOOKPROC(keyboard_proc)
    mouse_cb = HOOKPROC(mouse_proc)

    kb_hook = u.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_cb, None, 0)
    if not kb_hook:
        raise HookUnavailable(f"SetWindowsHookExW(WH_KEYBOARD_LL) failed: "
                              f"{k.GetLastError()}")
    mouse_hook = None
    if on_mouse is not None:
        mouse_hook = u.SetWindowsHookExW(WH_MOUSE_LL, mouse_cb, None, 0)
        if not mouse_hook:
            log.warning("mouse hook install failed (%d): order is guaranteed for keys "
                        "and focus changes only", k.GetLastError())

    u.SetTimer(None, 0, HEARTBEAT_TIMER_MS, None)
    handle.installed = True
    with _current_lock:
        _current = handle
    log.info("hook installed (keyboard=%s mouse=%s thread=%d)",
             bool(kb_hook), bool(mouse_hook), handle.thread_id)

    msg = wt.MSG()
    while True:
        ret = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret == 0:            # WM_QUIT
            break
        if ret == -1:
            log.error("GetMessageW failed: %d", k.GetLastError())
            break
        if msg.message == WM_TIMER:
            _heartbeat(safety)
        elif msg.message == WM_APP_WAKE:
            # Runs on the hook thread: the only place we inject while installed.
            try:
                if on_wake is not None:
                    on_wake()
            except BaseException:
                log.exception("on_wake handler failed")
            continue
        u.TranslateMessage(ctypes.byref(msg))
        u.DispatchMessageW(ctypes.byref(msg))
    try:
        u.KillTimer(None, 0)
        if mouse_hook:
            u.UnhookWindowsHookEx(mouse_hook)
        u.UnhookWindowsHookEx(kb_hook)
    except BaseException:
        log.exception("hook teardown failed")
    finally:
        # Always release waiters, even if a teardown call failed: a stuck
        # handle.stopped would hang shutdown() and the watchdog path.
        handle.installed = False
        handle.stopped.set()
        with _current_lock:
            _current = None
    try:
        on_panic()              # tell the app the hook is gone (idempotent there)
    except BaseException:
        log.exception("on_panic callback failed")
    log.info("hook removed (reason=%s)", handle.stop_reason or "loop-exit")
    return handle


def poll_foreground() -> tuple[int, str, str]:
    """(hwnd, process_basename, window_title) of the foreground window."""
    _init_win32()
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return 0, "", ""
    hwnd = int(hwnd)
    title_len = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(title_len + 1)
    u.GetWindowTextW(hwnd, buf, title_len + 1)
    name = ""
    pid = ctypes.c_uint32(0)
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        hproc = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if hproc:
            try:
                size = ctypes.c_uint32(1024)
                path = ctypes.create_unicode_buffer(1024)
                if k.QueryFullProcessImageNameW(hproc, 0, path, ctypes.byref(size)):
                    name = path.value.replace("\\", "/").rsplit("/", 1)[-1]
            finally:
                k.CloseHandle(hproc)
    return hwnd, name, buf.value


def combo_of_event(ev: KeyEvent) -> str:
    """Helper kept next to the hook so both sides use the same normalization."""
    return keys.combo_of(ev.vk, ev.ctrl, ev.alt, ev.shift, ev.win)
