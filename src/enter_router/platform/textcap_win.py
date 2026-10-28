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
import threading
import time

from enter_router.core.browser_url import (AccNode, is_absolute_url,
                                           page_title_of, select_document_url_node,
                                           url_of_node)
from enter_router.platform import inject, win32lib

log = logging.getLogger("enter_router.capture")

WM_GETTEXT = 0x000D
SMTO_ABORTIFHUNG = 0x0002
MAX_CAPTURE_CHARS = 8192

OBJID_CLIENT = 0xFFFFFFFC
CHILDID_SELF = 0
VT_I4 = 3
S_OK = 0

# Bounds for the accessibility walk that finds the URL (see get_document_url).
# The walk discovers a PATH once (background thread) and the hot path re-walks
# only that path, so these bounds only ever cost time on the first lookup in a
# window (measured: ~2.5ms per cross-process MSAA node in Firefox).
ACC_MAX_DEPTH = 5
ACC_NODE_BUDGET = 900
ACC_TIME_BUDGET_S = 2.0

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
    """Kept as the module's documented rule; the predicate lives in core."""
    return is_absolute_url(value)


# ---------------------------------------------------------------------------
# accessibility snapshot -> URL
#
# Measured on hardware (Firefox 2026-10-28): the root MSAA object is the
# APPLICATION node and its accValue is EMPTY. The URL lives on children — the
# address bar (role PROGRESSBAR, accValue "claude.ai/chat/<id>", scheme trimmed)
# and, per tab, that tab's DOCUMENT node. Reading only the root therefore always
# returned None, and every URL-scoped profile silently never matched.
# core/browser_url.select_document_url owns the "which node" policy (pure,
# unit-tested); this adapter only performs the COM calls.
# ---------------------------------------------------------------------------
_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_COM_LOCAL = threading.local()

# IAccessible vtable indexes (IUnknown 0-2, IDispatch 3-6, IAccessible 7-27).
VT_QI, VT_RELEASE = 0, 2
VT_GET_ACC_CHILD_COUNT, VT_GET_ACC_CHILD = 8, 9
VT_GET_ACC_NAME, VT_GET_ACC_VALUE = 10, 11
VT_GET_ACC_ROLE, VT_GET_ACC_STATE = 13, 14


class VARIANT_OUT(ctypes.Structure):
    """24-byte VARIANT for by-reference results (get_accRole / get_accState)."""
    _fields_ = [("vt", ctypes.c_ushort), ("wReserved", ctypes.c_ushort * 3),
                ("data", ctypes.c_uint64 * 2)]

    def as_int(self) -> int:
        return int(self.data[0]) & 0xFFFFFFFF


def _ensure_com() -> None:
    """MSAA needs an initialized apartment on the calling thread.

    Idempotent per thread; S_FALSE (already initialized) and
    RPC_E_CHANGED_MODE (someone initialized MTA) are both acceptable.
    """
    if getattr(_COM_LOCAL, "done", False) or not win32lib.IS_WINDOWS:
        return
    _COM_LOCAL.done = True
    try:
        # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE
        ctypes.windll.ole32.CoInitializeEx(None, 0x2 | 0x4)
    except OSError:
        log.debug("CoInitializeEx failed", exc_info=True)


def _addr_of(ptr, index: int) -> int:
    table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return int(table[index])


#: Prototype cache. Building a WINFUNCTYPE instance is expensive (hundreds of
#: microseconds); doing it per call put the walk over its time budget before it
#: reached the address bar (measured: 135ms and still None).
_PROTOS: dict[tuple, object] = {}


def _proto(shape: tuple):
    proto = _PROTOS.get(shape)
    if proto is None:
        proto = _WINFUNCTYPE(*shape)
        _PROTOS[shape] = proto
    return proto


STR_SHAPE = (ctypes.c_long, ctypes.c_void_p, VARIANT_I4,
             ctypes.POINTER(ctypes.c_void_p))
INT_SHAPE = (ctypes.c_long, ctypes.c_void_p, VARIANT_I4,
             ctypes.POINTER(VARIANT_OUT))
COUNT_SHAPE = (ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_long))
QI_SHAPE = (ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p))
RELEASE_SHAPE = (ctypes.c_ulong, ctypes.c_void_p)


def _acc_fn(ptr, index: int, shape: tuple):
    return _proto(shape)(_addr_of(ptr, index))


def _acc_release(ptr) -> None:
    try:
        _acc_fn(ptr, VT_RELEASE, RELEASE_SHAPE)(ptr)
    except BaseException:
        pass


def _acc_str(ptr, index: int) -> str:
    bstr = ctypes.c_void_p()
    hr = _acc_fn(ptr, index, STR_SHAPE)(ptr, VARIANT_I4.child(), ctypes.byref(bstr))
    if hr != S_OK or not bstr.value:
        return ""
    try:
        return ctypes.wstring_at(bstr.value)
    finally:
        try:
            win32lib.dll("oleaut32").SysFreeString(bstr)
        except OSError:
            pass


def _acc_int(ptr, index: int) -> int:
    var = VARIANT_OUT()
    hr = _acc_fn(ptr, index, INT_SHAPE)(ptr, VARIANT_I4.child(), ctypes.byref(var))
    return var.as_int() if hr == S_OK else -1


def _acc_child_count(ptr) -> int:
    count = ctypes.c_long(0)
    ok = _acc_fn(ptr, VT_GET_ACC_CHILD_COUNT, COUNT_SHAPE)(ptr, ctypes.byref(count))
    return count.value if ok == S_OK else 0


def _acc_child(ptr, child_id: int):
    """Child as IAccessible (child ids are simple elements in Firefox)."""
    disp = ctypes.c_void_p()
    hr = _acc_fn(ptr, VT_GET_ACC_CHILD, STR_SHAPE)(ptr, VARIANT_I4.child(child_id),
                                                  ctypes.byref(disp))
    if hr != S_OK or not disp:
        return None
    try:
        pacc = ctypes.c_void_p()
        hr = _acc_fn(disp, VT_QI, QI_SHAPE)(
            disp, ctypes.cast(ctypes.byref(IID_IACCESSIBLE), ctypes.c_void_p),
            ctypes.byref(pacc))
        return pacc if hr == S_OK and pacc else None
    finally:
        try:
            _acc_fn(disp, VT_RELEASE, RELEASE_SHAPE)(disp)
        except BaseException:
            pass


def _window_title(hwnd: int) -> str:
    u = ctypes.windll.user32
    length = u.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
    buf = ctypes.create_unicode_buffer(length + 1)
    u.GetWindowTextW(ctypes.c_void_p(hwnd), buf, length + 1)
    return buf.value


def _read_node(ptr) -> AccNode:
    return AccNode(role=_acc_int(ptr, VT_GET_ACC_ROLE),
                   state=_acc_int(ptr, VT_GET_ACC_STATE),
                   name=_acc_str(ptr, VT_GET_ACC_NAME),
                   value=_acc_str(ptr, VT_GET_ACC_VALUE))


def _walk_acc_paths(root, page_title: str = "") -> list[tuple[AccNode, tuple[int, ...]]]:
    """Flatten the accessibility tree (bounded, breadth-first), keeping paths.

    Breadth-first on purpose: in Firefox the address bar sits at depth 3
    (root -> navigation toolbar -> its grouping -> the address bar) while the tab
    strip and page content hang deeper, so a depth-first walk burns the budget
    inside the first toolbar button and never reaches the URL (measured: 126ms,
    still None).

    The walk stops as soon as ``select_document_url_node`` can answer. Every COM
    object obtained here is released before returning; callers only see
    AccNode values and integer child-id paths.
    """
    entries: list[tuple[AccNode, tuple[int, ...]]] = []
    deadline = time.monotonic() + ACC_TIME_BUDGET_S
    queue: list[tuple[object, int, bool, tuple[int, ...]]] = [(root, 0, False, ())]
    index = 0
    level_end = 1
    while index < len(queue) and len(entries) < ACC_NODE_BUDGET:
        if time.monotonic() > deadline:
            log.debug("accessibility walk hit its %.0fms budget (%d nodes)",
                      ACC_TIME_BUDGET_S * 1000, len(entries))
            break
        ptr, depth, owned, path = queue[index]
        index += 1
        entries.append((_read_node(ptr), path))
        if depth < ACC_MAX_DEPTH:
            count = min(_acc_child_count(ptr), 80)
            for child_id in range(1, count + 1):
                child = _acc_child(ptr, child_id)
                if child:
                    queue.append((child, depth + 1, True, path + (child_id,)))
        if owned:
            _acc_release(ptr)
        if index >= level_end:                     # end of a depth level
            level_end = len(queue)
            if select_document_url_node([node for node, _p in entries], page_title):
                break
    for ptr, _depth, owned, _path in queue[index:]:
        if owned:
            _acc_release(ptr)
    return entries


# ---------------------------------------------------------------------------
# URL path cache
#
# A full walk costs ~2.5ms per node on hardware, so paying it on every refresh is
# not acceptable. Discovery runs ONCE per window in a background thread and stores
# the child-id path to the node that carried the URL; afterwards a lookup is 3-4
# COM calls (~10ms). A missing node invalidates the cached path (window layout
# changed, browser updated) and the next lookup rediscovers it.
# ---------------------------------------------------------------------------
_URL_PATHS: dict[int, tuple[int, ...]] = {}
_URL_DISCOVERING: set[int] = set()
_URL_LOCK = threading.Lock()


def _open_root(hwnd: int):
    pacc = ctypes.c_void_p()
    oleacc = win32lib.dll("oleacc")
    hr = oleacc.AccessibleObjectFromWindow(
        hwnd, ctypes.c_uint32(OBJID_CLIENT),
        ctypes.cast(ctypes.byref(IID_IACCESSIBLE), ctypes.c_void_p),
        ctypes.byref(pacc))
    return pacc if hr == S_OK and pacc else None


def _node_at(root, path: tuple[int, ...]):
    """Follow child ids from `root`; returns an owned pointer or None."""
    ptr, owned = root, False
    for child_id in path:
        child = _acc_child(ptr, child_id)
        if owned:
            _acc_release(ptr)
        if child is None:
            return None
        ptr, owned = child, True
    return ptr if owned else None        # caller releases


def _discover_url_path(hwnd: int) -> None:
    """Background: find the URL node once and cache its child-id path."""
    try:
        # A fresh thread has no COM apartment: without this the root cannot be
        # obtained at all (the caller's _ensure_com() is thread-local by design).
        _init_win32()
        _ensure_com()
        root = _open_root(hwnd)
        if root is None:
            return
        try:
            title = page_title_of(_window_title(hwnd))
            entries = _walk_acc_paths(root, title)
            nodes = [node for node, _path in entries]
            target = select_document_url_node(nodes, title)
            if target is None:
                log.debug("url discovery found nothing hwnd=%s (nodes=%d)",
                          hwnd, len(entries))
                return
            for node, path in entries:
                if node is target and path:
                    with _URL_LOCK:
                        _URL_PATHS[hwnd] = path
                    log.debug("url path cached hwnd=%s path=%s", hwnd, path)
                    return
        finally:
            _acc_release(root)
    except OSError:
        log.debug("url discovery failed", exc_info=True)
    except BaseException:
        log.exception("url discovery raised unexpectedly")
    finally:
        with _URL_LOCK:
            _URL_DISCOVERING.discard(hwnd)


def _start_url_discovery(hwnd: int) -> None:
    with _URL_LOCK:
        if hwnd in _URL_DISCOVERING:
            return
        _URL_DISCOVERING.add(hwnd)
    threading.Thread(target=_discover_url_path, args=(hwnd,),
                     name=f"url-discover-{hwnd}", daemon=True).start()


def get_document_url(hwnd: int) -> str | None:
    """Best-effort URL of the ACTIVE tab in `hwnd`; None when unsure.

    Order:
    1. the root object's accValue — Chromium puts the document URL there, which
       is ALL this adapter used to read and why Firefox always got None;
    2. the cached path to the URL node (Firefox: address bar / active DOCUMENT);
    3. otherwise return None now and discover the path in the background, so the
       caller is never blocked by a ~1s accessibility walk.

    Nothing that does not look like a URL is ever returned: a URL-scoped profile
    must not match on a guess (design §4.3).
    """
    try:
        _init_win32()
        _ensure_com()
        if not hwnd:
            return None
        root = _open_root(hwnd)
        if root is None:
            return None
        try:
            root_value = _acc_str(root, VT_GET_ACC_VALUE)
            if is_absolute_url(root_value):
                return root_value.strip()
            with _URL_LOCK:
                path = _URL_PATHS.get(hwnd)
            if path:
                node_ptr = _node_at(root, path)
                if node_ptr is None:
                    with _URL_LOCK:
                        _URL_PATHS.pop(hwnd, None)   # layout changed: rediscover
                else:
                    try:
                        url = url_of_node(_read_node(node_ptr))
                    finally:
                        _acc_release(node_ptr)
                    if url is not None:
                        return url
        finally:
            _acc_release(root)
        _start_url_discovery(hwnd)
        return None
    except OSError:
        log.debug("MSAA URL lookup failed", exc_info=True)
        return None
    except BaseException:
        log.exception("MSAA URL lookup raised unexpectedly")
        return None


