"""Cross-process IME composition probe (Windows adapter), three-valued.

Why this exists (card t_dd246e5a, diagnosis t_80cfc1eb): the old version called
``ImmGetContext(hwnd)`` and nothing else. IMM hands out a context only for a
window owned by the CALLING thread, so for every other application it returned
NULL, the probe reported ``composing=None``, and the policy failed open. The
product then passed every Enter through without holding, converting or logging
anything — "the app does nothing" was the user-visible symptom.

How the state is read now. Each stage is tried in order; the first stage that
can actually READ the context answers, and the rest are skipped:

1. ``context``   AttachThreadInput(our thread, the window's thread, TRUE) ->
                 ImmGetContext(hwnd) -> ImmGetOpenStatus + the byte length of
                 ImmGetCompositionStringW(himc, GCS_COMPSTR) ->
                 ImmReleaseContext -> DetachThreadInput. The attach is what makes
                 another thread's context valid, and it is always undone in a
                 ``finally``.
2. ``ime_wnd``   the same read on ``ImmGetDefaultIMEWnd(hwnd)`` (some apps expose
                 the context only on the IME window).
3. ``gui_focus`` / ``gui_caret``
                 ``GetGUIThreadInfo(thread)`` -> ``hwndFocus`` / ``hwndCaret``,
                 re-tried with the same attach+context read; a control inside a
                 window whose top-level handle has no context often has one.
4. ``tsf_composition`` / ``tsf_ime_ui``
                 the TSF window scan (see below). This is the stage that actually
                 answers on this machine.

Why stage 4 exists (measured, not assumed). On Win11 with MS-IME the IMM context
of another process is never handed out — stages 1-3 return NULL for EVERY
foreign window — so a probe built only on IMM reports ``unknown`` forever, which
is the "product does nothing" defect this card removes. What IS observable from
outside the process is the TSF composition window that MSCTFIME (the IMM<->TSF
shim) keeps in the target process. ``scripts/win_ime_check.py`` prints the whole
inventory in each state, and the measurement is unambiguous:

    no composition   ``MSCTFIME UI`` present (hidden), ZERO ``MSCTFIME Composition``
    composing        ``MSCTFIME Composition`` window(s) present   (one visible,
                     the rest hidden), plus ``CiceroUIWndFrame``
    after commit     Composition/Cicero windows gone again, ``MSCTFIME UI`` stays

So the rule is:

* at least one ``MSCTFIME Composition`` window on the target thread -> ``composing``.
  Deliberately positive-only: any such window counts even if hidden, and it is
  the only way this probe ever says "composing".
* no Composition window BUT an ``MSCTFIME UI`` window on the thread ->
  ``not_composing``. The UI window is the anchor that proves MSCTFIME is loaded
  for that thread and WOULD have created a Composition window; its absence is
  therefore evidence, not a guess.
* neither -> ``unknown`` (an app that does not host MSCTFIME at all — TSF-native
  apps, other IME vendors). Absence of the anchor never becomes "not composing".

"Could not read" and "not composing" are separate answers. Only a NULL context,
a failed attach or a negative composition length is ``unknown``
(``ImeState(composing=None)``); a readable context with a zero-length
composition string is ``not_composing`` (``composing=False``).

Cache (this is why the hook may call it on every Enter press):

* The expensive part is discovering the ROUTE — ``GetGUIThreadInfo``,
  ``ImmGetDefaultIMEWnd`` and the attach bookkeeping — not reading the string.
  Discovery runs once per foreground window and is cached by hwnd; later calls
  start with the stage that answered last time (``_winner``).
* The composition string itself is re-read on EVERY call: it changes while
  typing ("a" opens a composition, Enter commits it), so caching the ANSWER
  would be a correctness bug.
* A window where every stage failed is remembered as unknown for
  ``unknown_retry_ms`` (default 2s). Such a window fails structurally (no IMM
  context at all), and retrying four attach attempts per Enter press would be
  pure overhead in the hook. The failure expires, so a transient failure never
  becomes permanent.
* The IMM stages are skipped for the rest of a foreground window's life once all
  of them failed (``_imm_dead``). This does not cache an ANSWER: the TSF scan
  below still runs on every call, and the flag is dropped the moment the
  foreground window changes. Measured cost of a fully cached-route probe with the
  TSF stage included: single-digit microseconds-to-0.1 ms (``win_ime_check.py``).
* The TSF scan enumerates the target thread's own windows (``EnumThreadWindows``,
  a handful) and reads the class name of each. Nothing is cached about the
  composition itself, because the composition windows are created and destroyed
  per composition (measured).

Never raises: this runs inside the keyboard hook, where an escaping exception
gets the hook removed by the OS (research §2-a). ``app/main.py`` only calls it
for Enter press-down events, never for ordinary keys.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

from enter_router.core.events import ImeState
from enter_router.platform import win32lib

log = logging.getLogger("enter_router.ime")

GCS_COMPSTR = 0x0008
UNKNOWN_RETRY_MS = 2000
PM_NOREMOVE = 0x0000
WM_USER = 0x0400

# Window classes MSCTFIME (the IMM<->TSF shim) keeps in the application's own
# process. Measured on Win11 + MS-IME: the composition window set appears only
# while a composition is alive, and the UI window stays for the thread's life.
COMPOSITION_CLASS = "MSCTFIME Composition"

# WM_IME_CONTROL / IMC_*: the DEFAULT IME WINDOW answers these cross-process
# (measured: SendMessageTimeoutW(hwnd "IME", WM_IME_CONTROL, IMC_GETOPENSTATUS)
# -> 1 and IMC_GETCONVERSIONMODE -> 9 = NATIVE|FULLSHAPE while typing Japanese).
WM_IME_CONTROL = 0x0283
IMC_GETCONVERSIONMODE = 0x0001
IMC_GETOPENSTATUS = 0x0005
IME_CMODE_NATIVE = 0x0001          # かな入力 (a Japanese composition can exist)
SMTO_ABORTIFHUNG = 0x0002
IME_MODE_TIMEOUT_MS = 40
IME_UI_CLASS = "MSCTFIME UI"
MAX_CLASSES_IN_DETAIL = 6

_protos_ready = False
_queue_ready = threading.local()


#: ``tagGUITHREADINFO`` comes from win32lib: this module and textcap_win.py both
#: call GetGUIThreadInfo, and two private copies made the prototype depend on
#: which module happened to be initialised last (see win32lib.GUITHREADINFO).
RECT = win32lib.RECT
GUITHREADINFO = win32lib.GUITHREADINFO


_WNDENUMPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    win32lib.BOOL, win32lib.HWND, win32lib.LPARAM)


def _init_win32() -> None:
    """Declare prototypes (HIMC/HWND are pointers: never let ctypes guess int)."""
    global _protos_ready
    if _protos_ready or not win32lib.IS_WINDOWS:
        return
    _protos_ready = True
    BOOL, DWORD, HWND, HIMC = win32lib.BOOL, win32lib.DWORD, win32lib.HWND, win32lib.HIMC
    UINT = win32lib.UINT
    proto = win32lib.proto
    proto("imm32", "ImmGetContext", HIMC, (HWND,))
    proto("imm32", "ImmGetCompositionStringW", ctypes.c_int32,
          (HIMC, DWORD, ctypes.c_void_p, DWORD))
    proto("imm32", "ImmGetOpenStatus", BOOL, (HIMC,))
    proto("imm32", "ImmGetDefaultIMEWnd", HWND, (HWND,))
    proto("imm32", "ImmReleaseContext", BOOL, (HWND, HIMC))
    proto("user32", "GetForegroundWindow", HWND, ())
    proto("user32", "GetWindowThreadProcessId", DWORD, (HWND, ctypes.POINTER(DWORD)))
    proto("user32", "AttachThreadInput", BOOL, (DWORD, DWORD, BOOL))
    proto("user32", "GetGUIThreadInfo", BOOL, (DWORD, ctypes.POINTER(GUITHREADINFO)))
    proto("kernel32", "GetCurrentThreadId", DWORD, ())
    proto("kernel32", "GetLastError", DWORD, ())
    proto("user32", "PeekMessageW", BOOL,
          (ctypes.POINTER(wt.MSG), HWND, UINT, UINT, UINT))
    proto("user32", "EnumThreadWindows", BOOL,
          (DWORD, ctypes.c_void_p, win32lib.LPARAM))
    proto("user32", "GetClassNameW", ctypes.c_int,
          (HWND, win32lib.LPWSTR, ctypes.c_int))
    proto("user32", "IsWindow", BOOL, (HWND,))
    proto("user32", "EnumWindows", BOOL, (ctypes.c_void_p, win32lib.LPARAM))
    proto("user32", "SendMessageTimeoutW", win32lib.LRESULT,
          (HWND, UINT, win32lib.WPARAM, ctypes.c_void_p, UINT, UINT,
           ctypes.POINTER(win32lib.ULONG_PTR)))


def ensure_message_queue() -> bool:
    """Give the calling thread a message queue (once per thread).

    Measured on this machine (win_ime_check.py): ``AttachThreadInput`` returned 0
    with ``ERROR_INVALID_PARAMETER`` (87) for every window, because the calling
    thread had never touched a message queue. ``PeekMessage`` is the documented
    way to create one, after which the attach succeeds. The hook thread already
    has a queue (it runs GetMessage), so this is a no-op there — it exists for the
    other callers (probes, self tests) and costs one call per thread.
    """
    if getattr(_queue_ready, "ok", False):
        return True
    try:
        _init_win32()
        msg = wt.MSG()
        win32lib.dll("user32").PeekMessageW(ctypes.byref(msg), None,
                                            WM_USER, WM_USER, PM_NOREMOVE)
        _queue_ready.ok = True
        return True
    except BaseException:
        log.debug("PeekMessageW (message queue creation) failed", exc_info=True)
        return False


def _last_error() -> int:
    """GetLastError value for the failure we just saw (diagnostics only)."""
    try:
        return int(win32lib.dll("kernel32").GetLastError())
    except BaseException:
        return 0


def _window_thread(hwnd: int) -> int:
    """Thread id that owns ``hwnd`` — the RETURN value, not the out parameter.

    ``GetWindowThreadProcessId(hwnd, &pid)`` RETURNS the thread id and writes the
    PROCESS id into the pointer. Reading the out parameter as a thread id is what
    made ``AttachThreadInput`` fail with ERROR_INVALID_PARAMETER (87) on the real
    machine: the "thread id" we passed was the pid, so no thread matched.
    """
    pid = win32lib.DWORD(0)
    try:
        return int(win32lib.dll("user32").GetWindowThreadProcessId(
            hwnd, ctypes.byref(pid)) or 0)
    except BaseException:
        return 0


def _gui_thread_info(tid: int) -> dict[str, int]:
    """hwndFocus / hwndCaret of ``tid`` (0 when the thread has no GUI state)."""
    out = {"hwndFocus": 0, "hwndCaret": 0}
    if not tid:
        return out
    try:
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if win32lib.dll("user32").GetGUIThreadInfo(tid, ctypes.byref(info)):
            out["hwndFocus"] = int(info.hwndFocus or 0)
            out["hwndCaret"] = int(info.hwndCaret or 0)
    except BaseException:
        log.debug("GetGUIThreadInfo failed", exc_info=True)
    return out


def _window_pid(hwnd: int) -> int:
    """Process id that owns ``hwnd`` (0 when unreadable)."""
    if not hwnd:
        return 0
    try:
        pid = win32lib.DWORD(0)
        win32lib.dll("user32").GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except BaseException:
        return 0


def _default_ime_wnd(hwnd: int) -> int:
    """The window that answers WM_IME_CONTROL for ``hwnd`` (0 when none)."""
    if not hwnd:
        return 0
    try:
        return int(win32lib.dll("imm32").ImmGetDefaultIMEWnd(hwnd) or 0)
    except BaseException:
        return 0


def _class_of(hwnd: int) -> str:
    """Class name of ``hwnd`` ("" when it cannot be read)."""
    try:
        buf = ctypes.create_unicode_buffer(128)
        win32lib.dll("user32").GetClassNameW(hwnd, buf, 128)
        return buf.value
    except BaseException:
        return ""


def _thread_windows(tid: int) -> tuple[tuple[int, str], ...]:
    """Top-level windows owned by ``tid`` as ``(hwnd, class)``; never raises.

    ``EnumThreadWindows`` walks one thread's window list instead of the whole
    desktop, which is what keeps the TSF stage cheap (the target thread owns a
    handful of windows). The MSCTFIME windows are top-level and belong to the
    application's UI thread — measured (``win_ime_check.py``) both for the
    ``EDIT`` target and for the ``MSCTFIME UI`` anchor.
    """
    if not tid:
        return ()
    found: list[tuple[int, str]] = []

    def _collect(hwnd, _lparam):
        found.append((int(hwnd or 0), _class_of(hwnd)))
        return True

    try:
        win32lib.dll("user32").EnumThreadWindows(
            tid, _WNDENUMPROC(_collect), win32lib.LPARAM(0))
    except BaseException:
        log.debug("EnumThreadWindows failed", exc_info=True)
        return ()
    return tuple(found)


def _tsf_scan(tids: tuple[int, ...]) -> tuple[str | None, str]:
    """Classify the TSF windows of ``tids``: composing / not_composing / unknown.

    Positive-only for "composing" (a ``MSCTFIME Composition`` window must exist),
    and "not_composing" needs the ``MSCTFIME UI`` anchor on the SAME thread, so a
    window that does not host MSCTFIME at all stays ``unknown``. See the module
    docstring for the measurement this rule comes from.
    """
    if not tids:
        return None, "tsf: no-thread-id"
    composing = 0
    anchors: list[int] = []
    notes: list[str] = []
    for tid in tids:
        windows = _thread_windows(tid)
        comp = sum(1 for _hwnd, cls in windows if cls == COMPOSITION_CLASS)
        anchor = any(cls == IME_UI_CLASS for _hwnd, cls in windows)
        composing += comp
        if anchor:
            anchors.append(tid)
        classes = sorted({cls for _hwnd, cls in windows if cls})
        shown = ",".join(classes[:MAX_CLASSES_IN_DETAIL])
        notes.append(f"tid={tid} composition={comp} ime_ui={anchor} classes=[{shown}]")
    detail = "tsf: " + "; ".join(notes)
    if composing:
        return "composing", detail
    if anchors:
        return "not_composing", detail
    return None, detail


def classify_composition(tsf_thread: str | None,
                         process_classes: tuple[str, ...] | list[str],
                         mode: tuple[bool, int] | None) -> tuple[str | None, str]:
    """Decide composing / not_composing / unknown from the cheap observations.

    Pure (no Win32), so the rule is unit-tested on Linux. The rule exists because
    a WRONG "not_composing" breaks Japanese input: the Enter is then swallowed and
    replaced by a synthesized key, so the IME never commits (measured on hardware:
    every Enter became ``key:shift+enter`` while typing Japanese).

    Order of evidence (strongest first):

    1. ``tsf_thread == "composing"`` (window on the foreground thread) -> composing
    2. a ``MSCTFIME Composition`` window anywhere in the target PROCESS  -> composing
       (a Chromium/Electron app composes in a renderer thread, not on the UI
       thread, which is exactly the case that used to read as "not composing")
    3. the IME window says open + NATIVE (かな) mode               -> UNKNOWN:
       an unobservable composition may exist, so never claim "not composing";
       ``ime_unknown_policy: "pass"`` (default) then lets Enter commit.
    4. the IME window says open but NOT native (英数)              -> not_composing
       (no Japanese composition can exist in alphanumeric mode)
    5. anything else                                              -> unknown

    "not_composing" therefore ALWAYS needs the IME window's answer: the
    ``MSCTFIME UI`` anchor alone is not evidence that no composition exists (that
    inference is what broke Japanese input). Unknown is the safe answer — the
    default ``ime_unknown_policy: "pass"`` hands the Enter to the IME.
    """
    if tsf_thread == "composing":
        return "composing", "tsf_thread"
    if COMPOSITION_CLASS in process_classes:
        return "composing", "tsf_process"
    if mode is not None:
        opened, conversion = mode
        if opened and (conversion & IME_CMODE_NATIVE):
            return None, f"ime_native_unobservable(open=1 conv=0x{conversion:X})"
        if opened:
            return "not_composing", f"ime_alnum(conv=0x{conversion:X})"
        return "not_composing", "ime_closed"
    return None, "no-evidence"


def ime_mode(ime_wnd: int) -> tuple[bool, int] | None:
    """``(open, conversion mode)`` of the default IME window, or None.

    Measured to answer for ANOTHER process's window (WM_IME_CONTROL), unlike
    ``ImmGetContext``. Bounded by a short timeout so a hung app cannot stall the
    hook thread.
    """
    if not ime_wnd:
        return None
    try:
        user32 = win32lib.dll("user32")
        result = win32lib.ULONG_PTR(0)

        def ask(what: int) -> int | None:
            ok = user32.SendMessageTimeoutW(
                ime_wnd, WM_IME_CONTROL, win32lib.WPARAM(what), None,
                SMTO_ABORTIFHUNG, IME_MODE_TIMEOUT_MS, ctypes.byref(result))
            return int(result.value) if ok else None

        opened = ask(IMC_GETOPENSTATUS)
        if opened is None:
            return None
        conversion = ask(IMC_GETCONVERSIONMODE)
        if conversion is None:
            return None
        return bool(opened), conversion
    except BaseException:
        log.debug("ime_mode failed", exc_info=True)
        return None


def _process_windows(pid: int) -> tuple[str, ...]:
    """Class names of every top-level window owned by ``pid``.

    ``EnumThreadWindows`` only sees the foreground thread; a Chromium/Electron
    app keeps its composition on a renderer thread, so the process-wide scan is
    what finds it. Bounded: only called when the thread scan said "not
    composing" and the IME mode did not already decide.
    """
    if not pid:
        return ()
    found: list[str] = []

    def _collect(hwnd, _lparam):
        try:
            owner = win32lib.DWORD(0)
            win32lib.dll("user32").GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if int(owner.value) == pid:
                found.append(_class_of(hwnd))
        except BaseException:
            pass
        return True

    try:
        win32lib.dll("user32").EnumWindows(_WNDENUMPROC(_collect), win32lib.LPARAM(0))
    except BaseException:
        log.debug("EnumWindows failed", exc_info=True)
        return ()
    return tuple(found)


def _read_context(hwnd: int) -> tuple[str | None, str]:
    """One attach+context read of ``hwnd``.

    Returns ``("composing"|"not_composing", note)`` when the context was really
    readable, ``(None, note)`` when it was not. The note always names the API
    and the Win32 error, so an ``unknown`` in the log says WHY.
    """
    if not hwnd:
        return None, "hwnd=0"
    user32 = win32lib.dll("user32")
    imm = win32lib.dll("imm32")
    tid = _window_thread(hwnd)
    our_tid = int(win32lib.dll("kernel32").GetCurrentThreadId())
    attached = False
    if tid and tid != our_tid:
        # Cross-thread (and therefore cross-process) contexts need the attach.
        # A thread with no message queue cannot attach (measured err=87), so
        # create one first — no-op on the hook thread, which already has one.
        ensure_message_queue()
        if not user32.AttachThreadInput(our_tid, tid, 1):
            return None, f"AttachThreadInput=0 err={_last_error()}"
        attached = True
    try:
        himc = imm.ImmGetContext(hwnd)
        if not himc:
            return None, f"ImmGetContext=0 err={_last_error()}"
        try:
            is_open = int(imm.ImmGetOpenStatus(himc))
            size = int(imm.ImmGetCompositionStringW(himc, GCS_COMPSTR, None, 0))
        finally:
            imm.ImmReleaseContext(hwnd, himc)
    finally:
        if attached:
            user32.AttachThreadInput(our_tid, tid, 0)
    if size < 0:
        return None, f"ImmGetCompositionStringW={size} err={_last_error()}"
    note = f"open={is_open} bytes={size}"
    # The composition length is the authoritative signal. ImmGetOpenStatus
    # cannot report an error (BOOL), so it is diagnostics only: using it to say
    # "not composing" would turn a failed read into a false negative, which is
    # exactly the conflation this card removes.
    return ("composing" if size > 0 else "not_composing"), note


class CompositionProbe:
    """Three-valued composition probe with a per-foreground-window route cache."""

    def __init__(self, unknown_retry_ms: int = UNKNOWN_RETRY_MS,
                 clock=None) -> None:
        self._unknown_retry_ms = int(unknown_retry_ms)
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._hwnd = 0
        self._candidates: tuple[tuple[int, str], ...] = ()
        self._tids: tuple[int, ...] = ()
        self._imm_dead = False
        self._winner = ""
        self._failed_at: float | None = None
        self._last_detail = ""

    # -- cache ----------------------------------------------------------
    def reset(self) -> None:
        """Forget the cached route (tests; also harmless at runtime)."""
        with self._lock:
            self._hwnd = 0
            self._candidates = ()
            self._tids = ()
            self._imm_dead = False
            self._winner = ""
            self._failed_at = None
            self._fail_source = ""
            self._last_detail = ""

    def _discover(self, hwnd: int) -> None:
        """Build the candidate list for ``hwnd`` (the expensive part; cached)."""
        self._hwnd = hwnd
        self._winner = ""
        self._failed_at = None
        self._fail_source = ""
        self._last_detail = ""
        self._imm_dead = False
        tid = _window_thread(hwnd)
        cands: list[tuple[int, str]] = [(hwnd, "context")]
        try:
            ime_wnd = int(win32lib.dll("imm32").ImmGetDefaultIMEWnd(hwnd) or 0)
        except BaseException:
            log.debug("ImmGetDefaultIMEWnd failed", exc_info=True)
            ime_wnd = 0
        if ime_wnd and ime_wnd != hwnd:
            cands.append((ime_wnd, "ime_wnd"))
        info = _gui_thread_info(tid)
        for label, handle in (("gui_focus", info["hwndFocus"]),
                              ("gui_caret", info["hwndCaret"])):
            if handle and all(handle != cand for cand, _ in cands):
                cands.append((handle, label))
        self._candidates = tuple(cands)
        # TSF scan targets: the foreground window's thread, plus the threads that
        # own the focused control and the caret (a browser window is not the
        # thread that hosts the edit field). Discovered once, like the IMM route.
        tids: list[int] = [tid]
        for handle in (info["hwndFocus"], info["hwndCaret"]):
            handle_tid = _window_thread(handle) if handle else 0
            if handle_tid and handle_tid not in tids:
                tids.append(handle_tid)
        self._tids = tuple(t for t in tids if t)

    def _tsf_targets(self) -> tuple[int, ...]:
        """Threads to scan now: the cached ones plus the live foreground thread."""
        tids = list(self._tids)
        live = _window_thread(self._hwnd) if self._hwnd else 0
        if live and live not in tids:
            tids.insert(0, live)
        return tuple(tids)

    def _ordered(self):
        """The stage that answered last time first, then the rest."""
        if self._winner:
            for cand, label in self._candidates:
                if label == self._winner:
                    yield cand, label
        for cand, label in self._candidates:
            if label != self._winner:
                yield cand, label

    # -- probe ----------------------------------------------------------
    def query(self, hwnd: int) -> ImeState:
        """Three-valued state for ``hwnd``; never raises."""
        try:
            return self._query(hwnd)
        except BaseException:                 # never break the hook thread
            log.exception("IME probe raised unexpectedly (reporting unknown)")
            return ImeState(composing=None, detail="probe-exception")

    def _query(self, hwnd: int) -> ImeState:
        if not hwnd:
            return ImeState(composing=None, detail="no-foreground-window")
        if not win32lib.IS_WINDOWS:
            return ImeState(composing=None, detail="not-windows")
        _init_win32()
        with self._lock:
            if hwnd != self._hwnd:
                self._discover(hwnd)
            now = self._clock()
            if (self._failed_at is not None
                    and (now - self._failed_at) * 1000.0 < self._unknown_retry_ms):
                # The cached answer keeps its PROVENANCE: "ime_native" means
                # "かな mode, composition invisible", which the app layer must
                # still be able to recognise during the retry window. Losing it
                # made every Enter in those 2s an unclassified unknown => pass
                # (measured: a single Enter sent the message).
                return ImeState(composing=None,
                                source=(self._fail_source or "cached"),
                                detail="cached-unknown; " + self._last_detail)
            if self._failed_at is not None:
                # The unknown retry window has just expired: retry the FULL route
                # (the IMM stages too), so a transient failure never becomes
                # permanent just because the fast path was taken earlier.
                self._imm_dead = False
            notes: list[str] = []
            # Cheap first: the IME window's mode. It answers cross-process in ~0.1ms
            # while the IMM route costs four attach attempts (measured 46ms on the
            # first Enter of a window, over the 5ms callback budget).
            mode = ime_mode(_default_ime_wnd(self._hwnd))
            japanese = bool(mode and mode[0] and (mode[1] & IME_CMODE_NATIVE))
            notes.append(f"ime_mode={mode}")
            if japanese:
                # かな入力中: the precise route cannot see another process's
                # composition anyway, so only the (cheap, positive-only) TSF scan
                # is worth running. No composition window => unknown, which the
                # default policy hands to the IME (the commit must work).
                tsf_state, tsf_note = _tsf_scan(self._tsf_targets())
                notes.append(tsf_note)
                if tsf_state == "composing":
                    self._winner = "tsf_composition"
                    self._failed_at = None
                    self._last_detail = "; ".join(notes)
                    return ImeState(composing=True, source="tsf_composition",
                                    detail=self._last_detail)
                notes.append("ime_native_unobservable(open=1 native)")
                self._failed_at = now
                self._fail_source = "ime_native"
                self._last_detail = "; ".join(notes)
                return ImeState(composing=None, source="ime_native",
                                detail=self._last_detail)
            if self._imm_dead:
                notes.append("imm=skipped (no readable context for this window)")
            else:
                for cand, label in self._ordered():
                    state, note = _read_context(cand)
                    notes.append(f"{label}(0x{cand:X}): {note}")
                    if state is None:
                        continue
                    self._winner = label
                    self._failed_at = None
                    self._last_detail = "; ".join(notes)
                    return ImeState(composing=(state == "composing"), source=label,
                                    detail=self._last_detail)
                # Every IMM candidate failed. That is structural for a foreign
                # process (measured), so stop paying four attach attempts on every
                # Enter press; the flag is cleared when the foreground window
                # changes, i.e. when a new route is discovered.
                self._imm_dead = True
            tsf_state, tsf_note = _tsf_scan(self._tsf_targets())
            notes.append(tsf_note)
            if tsf_state == "composing":
                self._failed_at = None
                self._last_detail = "; ".join(notes)
                return ImeState(composing=True, source="tsf_composition",
                                detail=self._last_detail)
            # No positive evidence yet. Two more observations decide it:
            #  (a) the IME window's open/conversion mode (WM_IME_CONTROL answers
            #      cross-process, unlike ImmGetContext), and
            #  (b) only when (a) is unreadable, a PROCESS-wide window scan — a
            #      Chromium/Electron app composes on a renderer thread, so the
            #      thread-only scan legitimately finds nothing.
            notes.append(f"ime_mode={mode}")
            process_classes: tuple[str, ...] = ()
            if mode is None:
                process_classes = _process_windows(_window_pid(self._hwnd))
                notes.append(f"process_windows={len(process_classes)}")
            verdict, why = classify_composition(tsf_state, process_classes, mode)
            notes.append(why)
            self._last_detail = "; ".join(notes)
            if verdict is not None:
                self._failed_at = None
                return ImeState(composing=(verdict == "composing"),
                                source=("ime_native" if why.startswith("ime_native")
                                        else why.split("(")[0]),
                                detail=self._last_detail)
            self._failed_at = now
            self._fail_source = "unknown"
            return ImeState(composing=None, source="unknown",
                            detail=self._last_detail)


_probe = CompositionProbe()


def query_composition(hwnd: int) -> ImeState:
    """Three-valued IME state of ``hwnd`` as seen from this process.

    ``composing=True``  an uncommitted composition string exists
    ``composing=False`` the read succeeded and no composition exists
    ``composing=None``  the read failed — see ``ImeState.detail`` for the API
                        and Win32 error (the policy decides what to do)

    ``source`` names the stage that answered: ``context`` / ``ime_wnd`` /
    ``gui_focus`` / ``gui_caret`` (IMM) or ``tsf_composition`` / ``tsf_ime_ui``
    (the TSF window scan, which is what answers for other applications).
    """
    return _probe.query(hwnd)


def reset_cache() -> None:
    """Drop the cached route (used by the probe script / tests)."""
    _probe.reset()


def foreground_hwnd() -> int:
    """Small helper so the hook and the app share one definition."""
    try:
        _init_win32()
        return int(win32lib.dll("user32").GetForegroundWindow() or 0)
    except OSError:
        return 0
