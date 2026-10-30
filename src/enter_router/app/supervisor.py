"""Hook liveness supervision (companion to design §6-1 / §6-3).

Why this module exists — the failure it closes, seen in the field:

the low-level hook can stop while the process keeps running.  Two ways:

* the hook thread's message loop ends (an exception in the loop, ``GetMessageW``
  returning -1, a stray ``WM_QUIT`` that was not ours).  The hook is gone, the
  keyboard is free, and the app still reports "running" — the user sees the tool
  "stop by itself" and no line in the log says why;
* the OS removes a slow low-level hook on its own (``LowLevelHooksTimeout``).

The second case cannot be observed directly (there is no API that says "your
hook is still installed"), but the first one can: "the app believes it is
intercepting, and no hook exists" is exactly the state that must never persist.
The supervisor therefore only answers one question — should we install the hook
again? — and the app keeps the retry bookkeeping.

Kept pure and Windows-free so the decision is unit-tested on Linux.
"""
from __future__ import annotations

#: Consecutive failed re-installs before the supervisor stops trying and says so.
#: Small on purpose: a hook that cannot be installed 5 times in a row is a
#: condition the user must be told about, not a transient.
MAX_REPAIR_ATTEMPTS = 5


def needs_repair(state: str, hook_installed: bool, thread_alive: bool) -> bool:
    """True when the app says "running" but no live hook backs that claim.

    ``state`` is the app-level state (``running`` / ``paused`` / ``panic`` /
    ``busy``).  ``paused`` and ``panic`` are *intentional* pass-through states —
    the keyboard is supposed to be free there — so they never trigger a repair.
    """
    if state != "running":
        return False
    return not (hook_installed and thread_alive)


def give_up(attempts: int) -> bool:
    """True once the repair attempts have been exhausted."""
    return attempts >= MAX_REPAIR_ATTEMPTS
