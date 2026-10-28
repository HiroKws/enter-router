"""Measure YOUR real Enter-press intervals (Windows, real keyboard, 3 phases).

The burst window is a property of the operator's hand, so it is measured instead
of guessed. This tool installs a global low-level keyboard hook (``WH_KEYBOARD_LL``)
and records ``VK_RETURN`` key-down timestamps, phase by phase, then writes JSON
and prints the calibration computed by ``enter_router.core.calibrate``.

    phase "double"    5 sets of 2 consecutive presses  -> the 1st->2nd gap
    phase "triple"    3 sets of 3 consecutive presses  -> the 2nd->3rd gap
    phase "separate"  6 presses with a clear pause     -> the "not consecutive" gap

Run it from Windows (WSL interop is fine, the window appears on the desktop):

    python  <repo>\scripts\measure_enter.py --out %USERPROFILE%\enter-measure.json

``F12`` advances to the next phase (and saves after the last one); ``Esc``
aborts without writing. The hook is global, so F12/Esc work even when another
window has the focus. ``--selftest`` only proves the hook can be installed and
exits (no UI, no keyboard input needed).

Nothing is swallowed: every key, including Enter, reaches its normal
destination. Nothing is logged except timestamps (no text is captured).
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import platform
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

REPO_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isdir(os.path.join(REPO_SRC, "src")):
    sys.path.insert(0, os.path.join(REPO_SRC, "src"))

try:
    from enter_router.core import calibrate
except Exception:                                   # pragma: no cover - fallback
    calibrate = None

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_KEYUP, WM_SYSKEYUP = 0x0101, 0x0105
WM_QUIT, WM_TIMER = 0x0012, 0x0113
LLKHF_INJECTED = 0x10
VK_RETURN, VK_ESCAPE, VK_F12 = 0x0D, 0x1B, 0x7B

#: A gap above this is a pause BETWEEN sets, not a burst sample.
SET_GAP_MS = 600
#: In the "separate" phase every gap up to this is a deliberate pause sample.
SEPARATE_GAP_MS = 3000

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
HOOKPROC = _WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.c_ulong), ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ULONG_PTR)]


PHASES = (
    ("double", "① 2連打（1発目→2発目の間隔）",
     "Enter を「2回」続けて叩くのを 5 セット。\n"
     "セットとセットの間は 1 秒以上あけてください。\n\n"
     "普段どおりの速さで。終わったら F12。"),
    ("triple", "② 3連打（2発目→3発目の間隔）",
     "Enter を「3回」続けて叩くのを 3 セット。\n"
     "セットの間は 1 秒以上あけてください。\n\n"
     "終わったら F12。"),
    ("separate", "③ 非連続（別の操作として間をあける）",
     "Enter を「2回」、今度は別々の操作として\n"
     "しっかり間をあけて 6 回叩いてください。\n"
     "目安は 1 秒くらい。\n\n"
     "終わったら F12（保存して終了）。"),
)


def now_ms() -> float:
    return time.perf_counter() * 1000.0


class Recorder:
    """Hook-thread side of the capture: timestamps only, never text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.presses: list[float] = []
        self.advance = 0            # F12 presses
        self.abort = False
        self.error = ""
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.thread_id: int | None = None

    # -- called from the hook thread ------------------------------------
    def _on_enter(self, t_ms: float) -> None:
        with self._lock:
            self.presses.append(t_ms)

    def _on_f12(self) -> None:
        with self._lock:
            self.advance += 1

    def _on_escape(self) -> None:
        with self._lock:
            self.abort = True

    # -- called from the UI thread --------------------------------------
    def take_presses(self) -> list[float]:
        with self._lock:
            out, self.presses = self.presses, []
            return out

    def snapshot(self) -> list[float]:
        with self._lock:
            return list(self.presses)

    def take_advance(self) -> int:
        with self._lock:
            n, self.advance = self.advance, 0
            return n

    def is_aborted(self) -> bool:
        with self._lock:
            return self.abort

    def request_stop(self) -> None:
        self.stop.set()
        tid = self.thread_id
        if tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except BaseException:
                pass


def _protos() -> None:
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    u.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, WPARAM, LPARAM)
    u.CallNextHookEx.restype = LRESULT
    u.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
    u.SetWindowsHookExW.restype = wt.HHOOK
    u.UnhookWindowsHookEx.argtypes = (wt.HHOOK,)
    u.UnhookWindowsHookEx.restype = wt.BOOL
    u.GetMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
    u.GetMessageW.restype = ctypes.c_int
    u.TranslateMessage.argtypes = (ctypes.POINTER(wt.MSG),)
    u.DispatchMessageW.argtypes = (ctypes.POINTER(wt.MSG),)
    u.PostThreadMessageW.argtypes = (wt.DWORD, wt.UINT, WPARAM, LPARAM)
    u.PostThreadMessageW.restype = wt.BOOL
    k.GetCurrentThreadId.restype = wt.DWORD
    k.GetLastError.restype = wt.DWORD


def hook_thread(rec: Recorder) -> None:
    """Own thread + message loop: the hook is delivered on the installing thread."""
    try:
        _protos()
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        rec.thread_id = int(k.GetCurrentThreadId())

        def proc(n_code, wparam, lparam):
            try:
                if n_code >= 0:
                    info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if not (info.flags & LLKHF_INJECTED):
                        vk = int(info.vkCode)
                        if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                            if vk == VK_RETURN:
                                rec._on_enter(now_ms())
                            elif vk == VK_F12:
                                rec._on_f12()
                            elif vk == VK_ESCAPE:
                                rec._on_escape()
            except BaseException:
                pass                                    # never break the chain
            return u.CallNextHookEx(None, n_code, wparam, lparam)

        cb = HOOKPROC(proc)
        handle = u.SetWindowsHookExW(WH_KEYBOARD_LL, cb, None, 0)
        if not handle:
            rec.error = f"SetWindowsHookExW failed: {k.GetLastError()}"
            rec.ready.set()
            return
        rec.error = ""
        rec.ready.set()
        msg = wt.MSG()
        while True:
            ret = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if rec.stop.is_set():
                break
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        u.UnhookWindowsHookEx(handle)
    except BaseException:
        rec.error = traceback.format_exc(limit=4)
        rec.ready.set()


def gaps_of(presses: list[float], max_gap_ms: int) -> list[int]:
    if calibrate is not None:
        return calibrate.gaps_from_presses(presses, max_gap_ms=max_gap_ms)
    out, prev = [], None
    for t in presses:
        if prev is not None:
            gap = int(round(t - prev))
            if 0 < gap <= max_gap_ms:
                out.append(gap)
        prev = t
    return out


def summarise(presses_by_phase: dict[str, list[float]]) -> dict:
    burst = gaps_of(presses_by_phase.get("double", []), SET_GAP_MS)
    burst += gaps_of(presses_by_phase.get("triple", []), SET_GAP_MS)
    burst.sort()
    separated = sorted(gaps_of(presses_by_phase.get("separate", []), SEPARATE_GAP_MS))
    result = {
        "phases": {name: [round(v, 1) for v in vals]
                   for name, vals in presses_by_phase.items()},
        "gaps": {
            "burst": burst,
            "double": gaps_of(presses_by_phase.get("double", []), SET_GAP_MS),
            "triple": gaps_of(presses_by_phase.get("triple", []), SET_GAP_MS),
            "separate": separated,
        },
    }
    if calibrate is not None:
        cal = calibrate.recommend_window(burst, separated)
        result["calibration"] = {
            "window_ms": cal.window_ms, "ok": cal.ok, "note": cal.note,
            "burst_max_ms": cal.burst_max_ms,
            "separated_min_ms": cal.separated_min_ms,
            "burst_samples": cal.burst_samples,
            "separated_samples": cal.separated_samples,
            "report": cal.report(),
        }
    return result


def write_json(path: str, data: dict) -> None:
    data["created_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["python"] = platform.python_version()
    data["platform"] = platform.platform()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def load_previous_phases(path: str) -> dict[str, list[float]]:
    """Phases already measured in this file (a partial re-run must not lose them)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    phases = data.get("phases")
    if not isinstance(phases, dict):
        return {}
    out: dict[str, list[float]] = {}
    for name, values in phases.items():
        if isinstance(values, list) and values:
            out[name] = [float(v) for v in values]
    return out


def selftest() -> int:
    rec = Recorder()
    th = threading.Thread(target=hook_thread, args=(rec,), daemon=True, name="hook")
    th.start()
    rec.ready.wait(3.0)
    if rec.error:
        print("HOOK_FAIL", rec.error)
        return 2
    time.sleep(0.3)
    rec.request_stop()
    th.join(1.0)
    print(f"HOOK_OK thread={rec.thread_id} enter_events={len(rec.presses)}")
    return 0


#: A phase needs at least this many presses before F12 may end it. A slipped
#: F12 (or a double tap on it) must never eat a whole phase.
MIN_PRESSES = {"double": 4, "triple": 3, "separate": 4}
#: How long a "that F12 was ignored" notice stays on screen.
NOTICE_MS = 4000


def run_ui(out_path: str, only: str | None = None) -> int:
    import tkinter as tk
    from tkinter import font as tkfont

    steps = [p for p in PHASES if only in (None, p[0])]
    if not steps:
        print(f"unknown phase: {only}")
        return 2

    rec = Recorder()
    th = threading.Thread(target=hook_thread, args=(rec,), daemon=True, name="hook")
    th.start()
    rec.ready.wait(3.0)
    if rec.error:
        print("HOOK_FAIL", rec.error)
        return 2

    state = {"i": 0, "presses": {name: [] for name, _t, _h in PHASES},
             "done": False, "notice": ("", 0.0)}
    # a partial re-run (--only) keeps the phases measured earlier
    previous = load_previous_phases(out_path)
    state["presses"].update(previous)
    for name, _t, _h in steps:
        state["presses"][name] = []

    root = tk.Tk()
    root.title("enter-router: Enter 間隔の実測")
    root.geometry("780x470")
    root.attributes("-topmost", True)
    bg, fg, dim, warn_fg = "#101418", "#f2f2f2", "#9aa4ae", "#ff9a9a"
    root.configure(bg=bg)
    big = tkfont.Font(family="Yu Gothic UI", size=15)
    mid = tkfont.Font(family="Yu Gothic UI", size=12)
    small = tkfont.Font(family="Consolas", size=11)

    title = tk.Label(root, bg=bg, fg="#7fd1ff", font=tkfont.Font(family="Yu Gothic UI", size=17, weight="bold"))
    title.pack(anchor="w", padx=18, pady=(14, 2))
    body = tk.Label(root, bg=bg, fg=fg, font=big, justify="left")
    body.pack(anchor="w", padx=18, pady=(4, 8))
    status = tk.Label(root, bg=bg, fg="#ffd479", font=mid, justify="left")
    status.pack(anchor="w", padx=18, pady=(4, 4))
    detail = tk.Label(root, bg=bg, fg=dim, font=small, justify="left", anchor="w")
    detail.pack(anchor="w", padx=18, pady=(2, 4), fill="x")
    hint = tk.Label(root, bg=bg, fg=dim, font=mid,
                    text="F12 = 次の段階へ / Esc = 中止（保存しません）")
    hint.pack(anchor="w", padx=18, pady=(6, 12))

    def notice(text: str) -> None:
        state["notice"] = (text, time.monotonic())

    def advance() -> None:
        i = state["i"]
        name, _ttl, _txt = steps[i]
        presses = rec.snapshot()
        need = MIN_PRESSES.get(name, 1)
        if len(presses) < need:
            notice(f"Enter が {len(presses)} 回しかありません。{need} 回以上で F12 が効きます。")
            return
        state["presses"][name] = rec.take_presses()
        if i + 1 >= len(steps):
            state["done"] = True
            data = summarise(state["presses"])
            try:
                write_json(out_path, data)
            except BaseException as exc:
                status.config(text=f"保存に失敗: {exc}")
                return
            cal = data.get("calibration") or {}
            title.config(text="完了 — 計測値を保存しました")
            body.config(text=f"保存先: {out_path}")
            status.config(text="窓（window_ms）= "
                               f"{cal.get('window_ms', '?')} ms\n" + str(cal.get("report", "")))
            detail.config(text="この窓を閉じてください（F12 でも閉じます）。")
            state["i"] = len(steps)
            return
        state["i"] = i + 1
        render()

    def render() -> None:
        i = state["i"]
        if i >= len(steps):
            return
        name, ttl, txt = steps[i]
        suffix = "" if only is None else "（この段階のみ）"
        title.config(text=f"段階 {i + 1}/{len(steps)}  {ttl}{suffix}")
        body.config(text=txt)
        status.config(fg="#ffd479", text="記録した Enter: "
                                         f"{len(state['presses'].get(name, []))} 回")

    def poll() -> None:
        if state["done"]:
            if rec.take_advance() > 0:
                root.destroy()
            else:
                root.after(120, poll)
            return
        if rec.is_aborted():
            title.config(text="中止しました（保存していません）")
            detail.config(text="Esc が押されました。窓を閉じてください。")
            state["done"] = True
            rec.request_stop()
            root.after(1500, root.destroy)
            return
        if rec.take_advance() > 0:
            advance()
        i = state["i"]
        if i < len(steps):
            name = steps[i][0]
            live = rec.snapshot()
            gaps = gaps_of(live, SEPARATE_GAP_MS if name == "separate" else SET_GAP_MS)
            text, when = state["notice"]
            if text and (time.monotonic() - when) * 1000.0 < NOTICE_MS:
                status.config(fg=warn_fg, text=text)
            else:
                status.config(fg="#ffd479",
                              text=f"記録した Enter: "
                                   f"{len(state['presses'].get(name, [])) + len(live)} 回"
                                   + (f"   直前の間隔: {gaps[-1]} ms" if gaps else ""))
            if gaps:
                detail.config(text="間隔(ms): " + ", ".join(str(g) for g in gaps[-12:]))
        root.after(120, poll)

    def on_close() -> None:
        rec.request_stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.lift()
    root.focus_force()
    render()
    root.after(150, poll)
    try:
        root.mainloop()
    finally:
        rec.request_stop()
        th.join(1.0)
    return 0



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="measure_enter", description=__doc__)
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"), "enter-measure.json"))
    ap.add_argument("--only", choices=[name for name, _t, _h in PHASES], default=None,
                    help="run one phase only and merge it into --out (keeps the others)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        print("this tool must run on Windows (it installs a WH_KEYBOARD_LL hook)")
        return 2
    if args.selftest:
        return selftest()
    return run_ui(os.path.abspath(args.out), only=args.only)


if __name__ == "__main__":
    raise SystemExit(main())
