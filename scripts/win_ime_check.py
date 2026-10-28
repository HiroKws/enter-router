"""Real-machine check for the cross-process IME probe (card t_dd246e5a).

Run on Windows (WSL users: ``powershell.exe /File ...scripts/win_ime_check.ps1``
is not needed — call python directly):

    python scripts\\win_ime_check.py
    python scripts\\win_ime_check.py --target tk

What it proves, on the real IME, without a human at the keyboard:

  1. a fresh target window → the probe says ``not_composing``;
  2. IME switched to hiragana + a synthetic ``a`` → ``composing``  (the whole
     point of the card: the composition is readable from ANOTHER process);
  3. a synthetic Enter → the IME COMMITS (no newline) and the probe goes back to
     ``not_composing``;
  4. a second Enter, not composing → a newline appears (the two cases really are
     different, and the probe distinguishes them);
  5. the probe stays cheap on repeated calls (route cache), reported in ms;
  6. no exception escapes the probe, and the last probe is readable (not unknown).

Cleanup rules (operator note: a previous QA left ~20 Notepad windows behind):

  * at most ONE target window is opened, and its PID is written to
    ``qa/ime_check_pids.json`` BEFORE it is used;
  * the target is closed in a ``finally`` block (WM_CLOSE, then terminate after a
    grace period), the ledger is cleared, and the script verifies the process is
    gone; leftovers from an interrupted earlier run are removed at start;
  * a leftover is only killed when its recorded process name still matches — the
    user's own documents are never touched.

Exit code: 0 only when every check passed (``IME_CHECK_OK``).
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import time
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                  # pragma: no cover
    pass

from enter_router.platform import ime, inject, textcap_win  # noqa: E402
from enter_router.platform import win32lib  # noqa: E402

LEDGER = os.path.join(REPO, "qa", "ime_check_pids.json")
PY = sys.executable

WM_CLOSE = 0x0010
IME_CMODE_NATIVE = 0x0001
IME_CMODE_FULLSHAPE = 0x0008

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (" - " + detail if detail else ""),
          flush=True)


def info(name: str, detail: str) -> None:
    print("INFO " + name + " - " + detail, flush=True)


# ---------------------------------------------------------------------------
# process ledger (so an interrupted run never leaks a window)
# ---------------------------------------------------------------------------
def ledger_load() -> list[dict]:
    try:
        with open(LEDGER, encoding="utf-8") as handle:
            data = json.load(handle)
        return [e for e in data if isinstance(e, dict)]
    except (OSError, ValueError):
        return []


def ledger_save(entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, LEDGER)


def ledger_add(pid: int, name: str, title: str) -> None:
    entries = [e for e in ledger_load() if e.get("pid") != pid]
    entries.append({"pid": int(pid), "name": name, "title": title,
                    "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
    ledger_save(entries)


def ledger_remove(pid: int) -> None:
    ledger_save([e for e in ledger_load() if e.get("pid") != pid])


def _user32():
    return win32lib.dll("user32")


def _init_protos() -> None:
    """Prototypes this script needs on top of the adapters' own."""
    BOOL, DWORD, HWND, UINT = (win32lib.BOOL, win32lib.DWORD, win32lib.HWND,
                               win32lib.UINT)
    proto = win32lib.proto
    proto("user32", "IsWindow", BOOL, (HWND,))
    proto("user32", "IsWindowVisible", BOOL, (HWND,))
    proto("user32", "GetClassNameW", ctypes.c_int, (HWND, win32lib.LPWSTR, ctypes.c_int))
    proto("user32", "PostMessageW", BOOL, (HWND, UINT, win32lib.WPARAM, win32lib.LPARAM))
    proto("user32", "EnumWindows", BOOL,
          (ctypes.c_void_p, win32lib.LPARAM))
    proto("user32", "GetWindowTextW", ctypes.c_int, (HWND, win32lib.LPWSTR, ctypes.c_int))
    proto("user32", "GetWindowTextLengthW", ctypes.c_int, (HWND,))
    proto("user32", "GetWindowThreadProcessId", DWORD, (HWND, ctypes.POINTER(DWORD)))
    proto("user32", "AttachThreadInput", BOOL, (DWORD, DWORD, BOOL))
    proto("kernel32", "GetCurrentThreadId", DWORD, ())
    proto("kernel32", "GetLastError", DWORD, ())
    proto("imm32", "ImmSetOpenStatus", BOOL, (win32lib.HIMC, BOOL))
    proto("imm32", "ImmSetConversionStatus", BOOL, (win32lib.HIMC, DWORD, DWORD))
    proto("imm32", "ImmGetConversionStatus", BOOL,
          (win32lib.HIMC, ctypes.POINTER(DWORD), ctypes.POINTER(DWORD)))


WNDENUMPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(win32lib.BOOL,
                                                               win32lib.HWND,
                                                               win32lib.LPARAM)


def windows_of_pid(pid: int, visible_only: bool = True) -> list[tuple[int, str]]:
    u = _user32()
    found: list[tuple[int, str]] = []

    def callback(hwnd, lparam):
        owner = win32lib.DWORD(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and (not visible_only or u.IsWindowVisible(hwnd)):
            length = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, buf, length + 1)
            found.append((int(hwnd), buf.value))
        return True

    u.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def process_is_alive(pid: int) -> bool:
    """True while a process with this pid exists (independent of its windows).

    ``EnumWindows`` is not enough: a UWP app can run without a visible window,
    and then "no window" would be misread as "process gone" — which is exactly
    how a leftover window is born.
    """
    if not pid:
        return False
    try:
        k = win32lib.dll("kernel32")
        win32lib.proto("kernel32", "OpenProcess", win32lib.HANDLE,
                       (win32lib.DWORD, win32lib.BOOL, win32lib.DWORD))
        win32lib.proto("kernel32", "GetExitCodeProcess", win32lib.BOOL,
                       (win32lib.HANDLE, ctypes.POINTER(win32lib.DWORD)))
        win32lib.proto("kernel32", "CloseHandle", win32lib.BOOL, (win32lib.HANDLE,))
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not handle:
            return False
        try:
            code = win32lib.DWORD(0)
            if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
    except BaseException:
        return False


def close_pid(pid: int, name: str = "") -> bool:
    """WM_CLOSE, then terminate after a grace period. Never raises."""
    u = _user32()
    try:
        for hwnd, _title in windows_of_pid(pid, visible_only=False):
            u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    except BaseException:
        traceback.print_exc()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not process_is_alive(pid):
            return True
        time.sleep(0.1)
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=10)
    except BaseException:
        traceback.print_exc()
    time.sleep(0.3)
    return not process_is_alive(pid)


def cleanup_leftovers() -> int:
    """Kill only what OUR ledger recorded; unknown names are left alone."""
    killed = 0
    known = {"notepad.exe", "python.exe", "pythonw.exe", os.path.basename(PY).lower()}
    for entry in ledger_load():
        pid = int(entry.get("pid") or 0)
        name = str(entry.get("name") or "").lower()
        if not pid or name not in known:
            print(f"SKIP ledger entry (not provably ours): {entry}", flush=True)
            ledger_remove(pid)
            continue
        if process_is_alive(pid):
            print(f"LEFTOVER pid={pid} name={name} - closing", flush=True)
            if close_pid(pid, name):
                killed += 1
        ledger_remove(pid)
    return killed


# ---------------------------------------------------------------------------
# target app
# ---------------------------------------------------------------------------
def launch(target: str) -> tuple[int, str] | tuple[None, None]:
    """Start the one target window; register its pid BEFORE using it."""
    if target == "notepad":
        proc = subprocess.Popen(["notepad.exe"], close_fds=True)
        name = "notepad.exe"
    else:
        script = os.path.join(REPO, "scripts", "qa_target_window.py")
        proc = subprocess.Popen([PY, script], close_fds=True,
                                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        name = os.path.basename(PY).lower()
    ledger_add(proc.pid, name, target)
    deadline = time.monotonic() + 25.0
    fallback_at = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        wins = windows_of_pid(proc.pid, visible_only=False)
        # A UWP target (Windows 11 Notepad) can show an untitled window first;
        # prefer a titled one, but never wait forever for it.
        titled = [w for w in wins if w[1]]
        pick = titled[0] if titled else (wins[0] if wins and time.monotonic() > fallback_at else None)
        if pick is not None:
            hwnd, title = pick
            info("target", f"pid={proc.pid} hwnd=0x{hwnd:X} title={title!r}")
            return proc.pid, str(hwnd)
        if proc.poll() is not None and not wins:
            info("target", f"pid={proc.pid} exited early rc={proc.returncode}")
            break
        time.sleep(0.2)
    # Failed to find a window: never leave the process behind.
    info("target", f"pid={proc.pid} no window appeared - closing it")
    close_pid(proc.pid, name)
    ledger_remove(proc.pid)
    return None, None


def activate(hwnd: int) -> bool:
    return bool(inject.take_foreground(hwnd))


def ime_switch_to_hiragana(hwnd: int) -> tuple[bool, str]:
    """ImmSetOpenStatus/ImmSetConversionStatus on the TARGET's context."""
    u, imm, k = _user32(), win32lib.dll("imm32"), win32lib.dll("kernel32")
    my_tid = int(k.GetCurrentThreadId())
    # GetWindowThreadProcessId RETURNS the thread id and writes the PROCESS id.
    tid = int(u.GetWindowThreadProcessId(hwnd, ctypes.byref(win32lib.DWORD(0))) or 0)
    attached = False
    if tid and tid != my_tid:
        ime.ensure_message_queue()      # without a queue the attach fails (err=87)
        attached = bool(u.AttachThreadInput(my_tid, tid, 1))
        if not attached:
            return False, f"AttachThreadInput=0 err={int(k.GetLastError())}"
    try:
        himc = imm.ImmGetContext(hwnd)
        if not himc:
            return False, f"ImmGetContext=0 err={int(k.GetLastError())}"
        try:
            opened = bool(imm.ImmSetOpenStatus(himc, 1))
            conv = bool(imm.ImmSetConversionStatus(
                himc, IME_CMODE_NATIVE | IME_CMODE_FULLSHAPE, 0))
            is_open = int(imm.ImmGetOpenStatus(himc))
            mode = win32lib.DWORD(0)
            sentence = win32lib.DWORD(0)
            have = bool(imm.ImmGetConversionStatus(himc, ctypes.byref(mode),
                                                   ctypes.byref(sentence)))
        finally:
            imm.ImmReleaseContext(hwnd, himc)
    finally:
        if attached:
            u.AttachThreadInput(my_tid, tid, 0)
    detail = (f"attach={attached} set_open={opened} set_conv={conv} "
              f"open={is_open} mode=0x{mode.value:X} read={have}")
    return bool(opened or conv), detail


def read_back() -> tuple[str, str]:
    """Text of the focused control (WM_GETTEXT only — no clipboard side effects)."""
    text = textcap_win.read_focused_text()
    return (text or ""), "wm_gettext"


def class_of(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(hwnd, buf, 256)
    return buf.value


def list_target_windows(pid: int, label: str) -> list[str]:
    """Cross-process window inventory (visible or not) — diagnostics only."""
    rows = []
    for hwnd, title in windows_of_pid(pid, visible_only=False):
        rows.append(f"0x{hwnd:X} class={class_of(hwnd)!r} visible="
                    f"{bool(_user32().IsWindowVisible(hwnd))} title={title!r}")
    for row in rows:
        info(f"windows[{label}]", row)
    return rows


def describe(state) -> str:
    return f"state={state.state} composing={state.composing} source={state.source or '-'}"


TARGET_STATUS = os.path.join(REPO, "qa", "target_status.txt")


def dump_target_status(label: str) -> None:
    """What the TARGET process saw (in-process IMM + WM_IME_* messages)."""
    try:
        with open(TARGET_STATUS, encoding="utf-8") as handle:
            lines = [line.rstrip() for line in handle if line.strip()]
    except OSError:
        lines = ["<no status file>"]
    for line in lines[-12:]:
        info(f"target[{label}]", line)


# ---------------------------------------------------------------------------
# the check itself
# ---------------------------------------------------------------------------
def run(target: str) -> None:
    pid = hwnd = 0
    try:
        try:
            os.remove(TARGET_STATUS)
        except OSError:
            pass
        pid, hwnd = launch(target)
        check("target window opened", bool(pid and hwnd), f"pid={pid} hwnd={hwnd}")
        if not pid or not hwnd:
            return
        hwnd = int(hwnd)
        check("foreground takeover (S3 procedure)", activate(hwnd),
              f"hWnd=0x{hwnd:X}")
        time.sleep(0.3)

        # -- 1. baseline: no composition in a fresh window -------------------
        ime.reset_cache()
        base = ime.query_composition(hwnd)
        info("baseline", describe(base) + " detail=" + (base.detail or "-"))
        check("fresh window reads not_composing", base.composing is False,
              describe(base))
        check("not_composing names its stage (never a cold 'no')",
              base.source == "tsf_ime_ui", "source=" + (base.source or "-"))

        # -- 2. hiragana + a -> composing ------------------------------------
        switched, ime_detail = ime_switch_to_hiragana(hwnd)
        info("ime switch", f"ok={switched} {ime_detail}")
        time.sleep(0.2)
        ime.reset_cache()
        list_target_windows(pid, "before 'a'")
        inject.send_vk(0x41)                       # 'a' -> あ (uncommitted)
        time.sleep(0.35)
        composing = ime.query_composition(hwnd)
        info("after 'a'", describe(composing) + " detail=" + (composing.detail or "-"))
        dump_target_status("after 'a'")
        list_target_windows(pid, "during composition")
        check("probe reads COMPOSING from another process", composing.composing is True,
              describe(composing))
        check("composing came from the TSF composition window",
              composing.source == "tsf_composition",
              "source=" + (composing.source or "-"))
        if composing.composing is None:
            info("unknown-reason", composing.detail or "-")

        # -- 3. Enter commits, no newline ------------------------------------
        before, how = read_back()
        inject.send_vk(0x0D)
        time.sleep(0.35)
        after, how2 = read_back()
        info("text", f"before={before!r}({how}) after={after!r}({how2})")
        committed = bool(after) and after != before and "\n" not in after \
            and "\r" not in after
        check("Enter while composing COMMITS (no newline)", committed,
              f"before={before!r} after={after!r}")
        ime.reset_cache()
        settled = ime.query_composition(hwnd)
        info("after commit", describe(settled))
        check("after the commit the probe reads not_composing",
              settled.composing is False, describe(settled))
        check("both answers came from the cross-process TSF stage",
              (base.source, composing.source, settled.source)
              == ("tsf_ime_ui", "tsf_composition", "tsf_ime_ui"),
              f"{base.source}/{composing.source}/{settled.source}")
        # -- 4. Enter when not composing inserts a newline --------------------
        inject.send_vk(0x0D)
        time.sleep(0.35)
        with_newline, how3 = read_back()
        info("text2", f"after-enter={with_newline!r}({how3})")
        check("plain Enter inserted a newline (the two cases differ)",
              "\n" in with_newline or "\r" in with_newline,
              f"{with_newline!r}")

        # -- 5. the probe stays cheap (route cache) ---------------------------
        ime.reset_cache()
        started = time.perf_counter()
        for _ in range(10):
            ime.query_composition(hwnd)
        per_call_ms = (time.perf_counter() - started) * 1000.0 / 10.0
        info("cost", f"{per_call_ms:.3f} ms per probe (cached route)")
        check("a cached-route probe is far below the 5ms hook budget",
              per_call_ms < 5.0, f"{per_call_ms:.3f} ms")

        # -- 6. no exception escaped ------------------------------------------
        try:
            ime.query_composition(0)
            ime.query_composition(0xDEAD)
            check("probe never raises on a bogus hwnd", True, "returned a state")
        except BaseException:
            check("probe never raises on a bogus hwnd", False,
                  traceback.format_exc().splitlines()[-1])

        dump_target_status("final")
        readable = any(s.composing is not None for s in (base, composing, settled))
        print("IME_CHECK_PROBE=%s" % ("readable" if readable else "unknown"), flush=True)
        if not readable:
            info("probe-outcome", "cross-process ImmGetContext returned NULL for every "
                                  "stage; the target's own (in-process) context is valid "
                                  "— see the INPROC lines above and the handoff")
    finally:
        # ALWAYS close what we opened, then prove nothing of ours is left.
        if pid:
            closed = close_pid(pid)
            ledger_remove(pid)
            check("target window closed", closed, f"pid={pid}")
        for entry in ledger_load():
            # Anything still in the ledger is ours by construction.
            left_pid = int(entry.get("pid") or 0)
            if left_pid and process_is_alive(left_pid):
                close_pid(left_pid, str(entry.get("name") or ""))
            ledger_remove(left_pid)
        check("pid ledger cleared", not ledger_load(), str(ledger_load()))


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout,
                        format="%(levelname)s %(name)s: %(message)s")
    _init_protos()
    target = "notepad"
    if "--target" in argv:
        target = argv[argv.index("--target") + 1]
    print(f"IME_CHECK start python={sys.version.split()[0]} target={target}", flush=True)
    killed = cleanup_leftovers()
    info("cleanup", f"leftovers closed={killed}")
    try:
        run(target)
    except BaseException:
        traceback.print_exc()
        check("script completed without an exception", False,
              traceback.format_exc().splitlines()[-1])
    failed = [name for name, ok, _ in results if not ok]
    print("IME_CHECK %s checks=%d failed=%d"
          % ("OK" if not failed else "FAIL", len(results), len(failed)), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
