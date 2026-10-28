"""Probe the cross-process composition answer with a REAL Japanese composition.

Run on Windows (from anywhere):  python <repo>\\qa\\probe_ime_foreign.py

What it does (no user interaction):
 1. starts Notepad and focuses it (a FOREIGN process — the interesting case);
 2. injects the IME かな key, then "a", which opens a real composition;
 3. asks ``enter_router.platform.ime.query_composition(notepad_hwnd)`` twice:
    once WHILE composing, once after Enter committed it;
 4. prints the four answers the policy would see, then closes Notepad.

Expected after v2-h:
    while composing : composing=None (unknown) -> policy "pass" lets Enter commit
    after Enter     : composing=False in 英数 / unknown in かな
Nothing here installs a keyboard hook or swallows a key.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time

REPO = r"C:\Users\hirok\enter-router"
sys.path.insert(0, REPO + r"\src")

from enter_router.platform import ime  # noqa: E402

u = ctypes.windll.user32
VK_KANJI = 0x19          # 英数/かな toggle
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002


def tap(vk: int) -> None:
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.08)


def find_notepad() -> int:
    for _ in range(40):
        hwnd = u.FindWindowW("Notepad", None)
        if hwnd:
            return int(hwnd)
        time.sleep(0.25)
    return 0


def describe(label: str, hwnd: int) -> None:
    state = ime.query_composition(hwnd)
    print(f"{label}: state={state.state} composing={state.composing} "
          f"source={state.source}")
    print(f"    detail={state.detail}")


def main() -> int:
    proc = subprocess.Popen(["notepad.exe"])
    try:
        hwnd = find_notepad()
        if not hwnd:
            print("could not find Notepad")
            return 1
        u.ShowWindow(hwnd, 9)                 # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        time.sleep(0.6)
        describe("A. baseline (no composition)", hwnd)

        tap(VK_KANJI)                          # -> かな input mode
        tap(0x41)                              # 'a': opens a composition
        time.sleep(0.35)
        describe("B. while composing (かな)", hwnd)

        tap(VK_RETURN)                         # commit
        time.sleep(0.35)
        describe("C. after the commit", hwnd)

        tap(VK_KANJI)                          # -> 英数 mode
        time.sleep(0.2)
        describe("D. 英数 mode, no composition", hwnd)
        return 0
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
