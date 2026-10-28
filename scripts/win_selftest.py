"""Windows-side self test for the paths the WSL unit tests cannot touch.

Run on real Windows (no console, no target app needed):

    python scripts\\win_selftest.py

Every check prints one PASS/FAIL line; the exit code is 1 if anything failed.
It is safe to run repeatedly: it installs a low-level hook for a fraction of a
second, never swallows a key, and restores the clipboard it found.

What it covers (review round 1):

* item 3  ``inject.clipboard_put`` / ``clipboard_get`` round trip (the bug was
          truncated HGLOBAL handles: put always returned False on hardware).
* item 1  ``hook._call_next`` with a real >4 GiB ``lparam`` (the bug was
          ``OverflowError: int too long to convert`` on every key press, raised
          again in the except branch, which broke the hook chain for every app).
* item 1  a real hook install that receives an injected key and passes it
          through, then uninstalls.
* item 2  the SendInput-vs-hook-callback deadlock, in both shapes: measured
          "inject while holding the callback's lock" (the old, broken shape)
          versus "release the lock, then inject" and the shipped routing
          ("hand it to the hook thread with hook.wake()").
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from enter_router.platform import hook, inject  # noqa: E402

VK_F24 = 0x87
VK_F23 = 0x86

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (" - " + detail if detail else ""),
          flush=True)


def info(name: str, detail: str) -> None:
    print("INFO " + name + " - " + detail, flush=True)


# ---------------------------------------------------------------------------
# item 3: clipboard round trip
# ---------------------------------------------------------------------------
def check_clipboard() -> None:
    original, had_original = inject.clipboard_get()
    try:
        for label, token in (("ascii", "enter-router-selftest-%d" % int(time.time())),
                             ("unicode", "あいうえお enter-router"),
                             ("long", "x" * 4096)):
            put_ok = inject.clipboard_put(token)
            got, get_ok = inject.clipboard_get()
            check("clipboard %s round trip" % label,
                  bool(put_ok and get_ok and got == token),
                  "put=%s get=%s len=%d" % (put_ok, get_ok, len(got or "")))
    finally:
        if had_original:
            inject.clipboard_put(original)
        else:
            inject.clipboard_put("")


# ---------------------------------------------------------------------------
# item 1: CallNextHookEx with a real 64-bit lparam
# ---------------------------------------------------------------------------
def check_call_next() -> None:
    buf = ctypes.create_string_buffer(64)
    addr = ctypes.cast(buf, ctypes.c_void_p).value
    info("call_next", "lparam=0x%X (buffer address, must be >4GiB)" % addr)
    check("call_next buffer address is above 4 GiB", addr > 2 ** 32, "0x%X" % addr)
    errors: list[str] = []
    values = []
    for label, lparam in (("null", 0), ("real-pointer", addr),
                          ("high-address", 0x7FFF0000_0000_0000)):
        try:
            values.append((label, hook._call_next(0, 0x0100, lparam)))
        except BaseException:
            errors.append(label + ": " + traceback.format_exc().splitlines()[-1])
    check("call_next never raises for a 64-bit lparam", not errors,
          "; ".join(errors) or " ".join("%s=%d" % pair for pair in values))


# ---------------------------------------------------------------------------
# hook install / pass-through + item 2 timing
# ---------------------------------------------------------------------------
class Safety:
    def __init__(self) -> None:
        self.max_ms = 0.0
        self.beats = 0

    def heartbeat(self) -> None:
        self.beats += 1

    def observe(self, elapsed_ms: float) -> None:
        self.max_ms = max(self.max_ms, float(elapsed_ms))


def check_hook_and_injection(handle_box: dict) -> None:
    safety = Safety()
    decide_lock = threading.RLock()
    use_lock = {"on": False}          # mimics app._decide taking the state lock
    seen: list[tuple[int, str, bool]] = []
    callback_errors: list[str] = []
    wake_runs = {"count": 0}
    wake_lock = threading.Lock()
    installed = threading.Event()

    def decide(ev) -> bool:
        # The pass-through hook must NEVER swallow; when use_lock is on, this is
        # exactly the shape that deadlocked against a background SendInput.
        try:
            if use_lock["on"]:
                with decide_lock:
                    seen.append((ev.vk, ev.kind, ev.injected))
            else:
                seen.append((ev.vk, ev.kind, ev.injected))
        except BaseException:
            callback_errors.append(traceback.format_exc().splitlines()[-1])
        return False

    def on_wake() -> None:
        # The shipped routing: the hook thread is the injector (review item 2).
        with wake_lock:
            wake_runs["count"] += 1
        inject.send_vk(VK_F23)

    def run_hook() -> None:
        try:
            hook.install(decide, lambda: None, safety, on_mouse=None, on_wake=on_wake)
        except BaseException:
            callback_errors.append("install: " + traceback.format_exc().splitlines()[-1])

    thread = threading.Thread(target=run_hook, name="selftest-hook", daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and hook.current_handle() is None:
        time.sleep(0.01)
    handle = hook.current_handle()
    check("hook install", handle is not None and handle.installed,
          "handle=%s" % bool(handle))
    if handle is None:
        return
    installed.set()

    try:
        # -- pass-through --------------------------------------------------
        seen.clear()
        t0 = time.perf_counter()
        inject.send_vk(VK_F24)
        pass_ms = (time.perf_counter() - t0) * 1000.0
        time.sleep(0.2)
        got = [item for item in seen if item[0] == VK_F24]
        check("hook saw both events of the injected key", len(got) >= 2,
              "events=%d" % len(got))
        check("injected flag is set on them", bool(got) and all(i[2] for i in got),
              str(got))
        check("no exception escaped the hook callback", not callback_errors,
              "; ".join(callback_errors) or "clean")
        info("pass_through", "SendInput took %.1fms, max callback %.1fms"
             % (pass_ms, safety.max_ms))

        # -- item 2, OLD shape: inject while holding the callback's lock ----
        use_lock["on"] = True
        safety.max_ms = 0.0
        with decide_lock:
            t0 = time.perf_counter()
            inject.send_vk(VK_F24)
            held_ms = (time.perf_counter() - t0) * 1000.0
        time.sleep(0.2)
        old_max = safety.max_ms
        info("old_shape", "SendInput held under the callback's lock: %.1fms, "
                          "max callback %.1fms" % (held_ms, old_max))
        check("the shipped code cannot be the old shape (documented defect)",
              held_ms > 100.0,
              "reproduced a %.0fms stall" % held_ms)

        # -- item 2, NEW shape: release the lock, then inject ----------------
        safety.max_ms = 0.0
        with decide_lock:
            pass                      # "compute the outcome under the lock"
        t0 = time.perf_counter()
        inject.send_vk(VK_F24)        # ...then inject with the lock released
        released_ms = (time.perf_counter() - t0) * 1000.0
        time.sleep(0.2)
        new_max = safety.max_ms
        info("new_shape", "SendInput after releasing the lock: %.1fms, "
                          "max callback %.1fms" % (released_ms, new_max))
        check("releasing the lock before SendInput removes the stall",
              released_ms < held_ms / 2 and new_max < 100.0,
              "%.1fms vs %.1fms" % (released_ms, held_ms))

        # -- item 2, shipped routing: the hook thread injects ----------------
        use_lock["on"] = True
        safety.max_ms = 0.0
        before = wake_runs["count"]
        t0 = time.perf_counter()
        woke = hook.wake()
        wake_ms = (time.perf_counter() - t0) * 1000.0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and wake_runs["count"] == before:
            time.sleep(0.01)
        time.sleep(0.2)
        check("hook.wake() reaches the hook thread's on_wake handler",
              bool(woke) and wake_runs["count"] > before,
              "wake=%s runs=%d post=%.1fms" % (woke, wake_runs["count"], wake_ms))
        check("injecting on the hook thread keeps callbacks fast",
              safety.max_ms < 100.0, "max callback %.1fms" % safety.max_ms)
        check("no exception escaped the hook callback (timing phase)",
              not callback_errors, "; ".join(callback_errors) or "clean")
    finally:
        handle.uninstall("selftest")
        thread.join(3.0)
        handle_box["handle"] = handle
        check("hook uninstall", not thread.is_alive(),
              "thread alive=%s" % thread.is_alive())


def check_capture_and_ime() -> None:
    """The other prototype-declared adapters must not raise on hardware."""
    from enter_router.platform import ime, notify_win, textcap_win

    hwnd = ime.foreground_hwnd()
    info("foreground", "hwnd=0x%X" % hwnd)
    try:
        state = ime.query_composition(hwnd)
        # t_dd246e5a: the answer is three-valued. "unknown" is a LEGAL answer
        # (measured: cross-process ImmGetContext returns no context on Win11) —
        # what must never happen is an exception or a fourth kind of value.
        check("IME probe returns a three-valued state without raising",
              state is not None and state.state in ("composing", "not_composing", "unknown"),
              "state=%s source=%s detail=%s"
              % (state.state, state.source or "-", (state.detail or "-")[:160]))
        info("ime_state", state.state)
        # A known answer must say WHICH stage answered (context / ime_wnd /
        # gui_focus / gui_caret / tsf_composition / tsf_ime_ui): "not composing"
        # without a source is the conflation this card removed.
        check("a known IME answer names the stage that answered",
              state.composing is None or bool(state.source),
              "state=%s source=%s" % (state.state, state.source or "-"))
    except BaseException:
        check("IME probe returns a three-valued state without raising", False,
              traceback.format_exc().splitlines()[-1])
    try:
        text = textcap_win.read_focused_text()
        check("focused-text read does not raise", True, "len=%s" % len(text or ""))
    except BaseException:
        check("focused-text read does not raise", False,
              traceback.format_exc().splitlines()[-1])
    try:
        url = textcap_win.get_document_url(hwnd)
        check("MSAA URL lookup does not raise", True, "url=%r" % (url,))
    except BaseException:
        check("MSAA URL lookup does not raise", False,
              traceback.format_exc().splitlines()[-1])
    try:
        notify_win.register_icon(0xFFFFFFFF, 99)   # bogus tray id: returns FALSE
        notify_win.notify("enter-router", "selftest notification", "info")
        notify_win.register_icon(0, 1)
        check("notification path does not raise", True)
    except BaseException:
        check("notification path does not raise", False,
              traceback.format_exc().splitlines()[-1])


def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout,
                        format="%(levelname)s %(name)s: %(message)s")
    print("WIN_SELFTEST start python=%s" % sys.version.split()[0], flush=True)
    check("64-bit interpreter", ctypes.sizeof(ctypes.c_void_p) == 8,
          "%d bytes" % ctypes.sizeof(ctypes.c_void_p))
    check("SendInput INPUT layout", inject.verify_layout(),
          "%d bytes" % inject.input_size())
    check_clipboard()
    check_call_next()
    check_capture_and_ime()
    check_hook_and_injection({})
    failed = [name for name, ok, _ in results if not ok]
    print("WIN_SELFTEST %s checks=%d failed=%d"
          % ("OK" if not failed else "FAIL", len(results), len(failed)), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
