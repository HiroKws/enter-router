"""End-to-end probe of the REAL composition root on Windows (no manual typing).

Run:  python scripts\\app_probe.py

Unlike ``win_selftest.py`` (adapters in isolation) this drives
``app.main.EnterRouterApp`` itself, with the real hook, the real timer thread,
the real sink worker and SendInput:

1. hold an Enter, let the real hold window expire, and check that the delivery
   travels timer thread -> hand-off queue -> hook.wake() -> hook thread ->
   SendInput exactly once (review round 1, item 2);
2. press the panic key while an Enter is held and check that the pending key is
   flushed FIRST and the hook is then removed (I7).

The probe config maps ``newline`` to ``f24`` so nothing of value is typed into
whatever window happens to have focus. Keys fed to the policy are synthetic
KeyEvents (SendInput can only produce LLKHF_INJECTED events, which rule0 ignores
by design), but every OS effect they cause is real.

Exit code 0 == every check passed. The log file path is printed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from enter_router.app import main as app_main  # noqa: E402
from enter_router.app import logsetup  # noqa: E402
from enter_router.core.events import VK_RETURN, KeyEvent  # noqa: E402
from enter_router.platform import hook  # noqa: E402

VK_F12 = 0x7B
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (" - " + detail if detail else ""),
          flush=True)


def probe_config() -> str:
    """Sample config, but every app is in scope and newline == F24."""
    raw = json.loads(open(os.path.join(REPO, "config", "sample.config.json"),
                          encoding="utf-8").read())
    raw["profiles"] = [{"id": "probe", "enabled": True,
                        "match": {"process": "*"},
                        "keymap": {"send": "f24", "newline": "f24"}}]
    path = os.path.join(tempfile.gettempdir(), "enter-router-probe.config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=2)
    return path


def wait_for(predicate, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def stub_ime(composing: bool | None) -> bool:
    """Pin the IME answer so the hold path is exercised deterministically.

    The IME adapter deliberately fails OPEN (``composing=None`` => pass the
    Enter through, design §3.5 / research R1) whenever the focused window has no
    IMM context — which is the normal state of the terminal this probe runs from.
    That is correct product behaviour, but it would skip the very path this probe
    exists to exercise (hold -> timer -> hand-off -> hook thread -> SendInput).

    So the query is pinned here and the real adapter is covered separately by
    ``scripts/win_selftest.py`` ("IME query returns a state without raising").
    """
    from enter_router.core.events import ImeState
    from enter_router.platform import ime as ime_mod

    ime_mod.query_composition = lambda hwnd: ImeState(composing=composing)
    return True


def main() -> int:
    config_path = probe_config()
    cfg, cfg_path = app_main.load_config(config_path)
    log_path = logsetup.setup(cfg.logging, app_main.appdata_dir(), console=True)
    print("APP_PROBE log=%s" % log_path, flush=True)
    stub_ime(False)
    print("APP_PROBE ime_query=stubbed(composing=False) - the app's real IME "
          "adapter is covered by win_selftest.py", flush=True)

    app = app_main.EnterRouterApp(cfg, cfg_path)
    try:
        app.start()
        installed = wait_for(lambda: app.get_status().hook_installed, 5.0)
        check("resident app installed the hook", installed,
              "state=%s" % app.get_status().state)
        if not installed:
            return 1

        # -- 1) held Enter is delivered through the hook thread --------------
        t0 = app._now_ms()
        swallowed = app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=t0))
        app._decide(KeyEvent(vk=VK_RETURN, kind="up", t_ms=t0 + 15))
        check("the counted Enter was swallowed (I3)", bool(swallowed))
        delivered = wait_for(lambda: app._machine.pending_count == 0, 2.0)
        check("the held Enter was delivered when the window expired", delivered,
              "pending=%d" % app._machine.pending_count)
        # The machine drops the pending count first and only then hands the work
        # to the queue, so wait separately for the hook thread to drain it.
        drained = wait_for(lambda: not app._pending_inject, 2.0)
        check("the hand-off queue drained on the hook thread", drained,
              str(app._pending_inject))
        check("the app is still running (no self-unhook)", app.get_status().state == "running",
              "state=%s" % app.get_status().state)

        # -- 1b) I6: a plain key must never overtake a queued delivery --------
        # Reproduces the review-round-2 race deterministically: the timer thread
        # hands the held Enter over (queue + wake) but the wake-up message has not
        # been processed when the user's next key reaches the decision path. A
        # PASS decision executes nothing, so only the drain inside _decide keeps
        # the order; the app must therefore see the newline BEFORE the "a".
        from enter_router.platform import inject as inject_mod
        sent: list[str] = []
        real_combo = inject_mod.send_combo
        real_wake = hook.wake
        real_now = app._now_ms

        def spy_combo(combo, held=()):
            sent.append(combo)          # recorded BEFORE the real SendInput runs
            return real_combo(combo, held)

        inject_mod.send_combo = spy_combo
        # wake() reports "posted" but nothing will drain it: exactly the window
        # between the timer thread's hand-off and the hook thread's WM_APP_WAKE.
        hook.wake = lambda: True
        try:
            t2 = app._now_ms()
            app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=t2))
            app._decide(KeyEvent(vk=VK_RETURN, kind="up", t_ms=t2 + 15))
            # let the hold window elapse without waiting for real time: the timer
            # callback reads app._now_ms, so moving the app clock past the
            # deadline makes the delivery happen now (as app._t0 does in the tests)
            app._now_ms = lambda: t2 + 400
            app._on_deadline()
            check("the delivery is queued on the hook thread, not injected yet",
                  len(app._pending_inject) == 1 and not sent,
                  "queued=%d sent=%s" % (len(app._pending_inject), sent))
            passed = not app._decide(KeyEvent(vk=0x41, kind="down", t_ms=t2 + 400))
            check("the plain key is passed through", passed)
            check("the queued delivery was injected BEFORE the key was handed over (I6)",
                  sent == ["f24"] and not app._pending_inject,
                  "sent=%s queued=%s" % (sent, app._pending_inject))
        finally:
            inject_mod.send_combo = real_combo
            hook.wake = real_wake
            app._now_ms = real_now

        # -- 2) panic key flushes the pending Enter, then unhooks ------------
        t1 = app._now_ms()
        app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=t1))
        app._decide(KeyEvent(vk=VK_RETURN, kind="up", t_ms=t1 + 15))
        held = app._machine.pending_count == 1
        check("second Enter is held again", held, "pending=%d" % app._machine.pending_count)
        panic_passed = not app._decide(KeyEvent(vk=VK_F12, kind="down",
                                                t_ms=t1 + 60, ctrl=True,
                                                alt=True, shift=True))
        check("the panic key itself is not swallowed", panic_passed)
        stopped = wait_for(lambda: hook.current_handle() is None, 5.0)
        check("the hook was removed after panic", stopped,
              "status=%s" % app.get_status().state)
        check("status reports panic", app.get_status().state == "panic",
              "status=%s" % app.get_status().state)
        check("pending Enter was not left dangling", app._machine.pending_count == 0)
    except BaseException:
        print("APP_PROBE exception:\n" + traceback.format_exc(), flush=True)
        check("no exception escaped the probe", False)
    finally:
        try:
            app.shutdown()
        except BaseException:
            print("shutdown raised:\n" + traceback.format_exc(), flush=True)

    # -- the log is the artefact QA reads --------------------------------
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            log_text = handle.read()
    except OSError:
        log_text = ""
    inject_lines = [line for line in log_text.splitlines() if "inject combo=f24" in line]
    check("SendInput really ran for the delivered key (inject combo=f24)",
          len(inject_lines) >= 2, "lines=%d" % len(inject_lines))
    # A panic is logged at ERROR on purpose; anything else at ERROR level, or a
    # traceback, would be a defect.
    unexpected = [line for line in log_text.splitlines()
                  if ("Traceback" in line
                      or ("ERROR   " in line and "panic state entered" not in line))]
    check("no unexpected ERROR/Traceback in the run log", not unexpected,
          "log=%s unexpected=%d %s" % (log_path, len(unexpected),
                                       unexpected[:3] or ""))
    for line in inject_lines:
        print("LOG " + line, flush=True)

    failed = [name for name, ok, _ in results if not ok]
    print("APP_PROBE %s checks=%d failed=%d"
          % ("OK" if not failed else "FAIL", len(results), len(failed)), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
