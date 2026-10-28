"""Enter-policy state machine (implementation of the frozen contract in docs/design.md v2).

Authoritative source for behaviour is docs/design.md v2 (§3.4 transition table,
§3.5 hold/flash/ordering, §3.7 invariants). This module mirrors that table.

Resolution pipeline (design §3.2) — this machine sits at step 7:
    injected? -> panic? -> IME? -> op detection -> layer1 meaning
    -> layer2 resolved action -> THIS MACHINE -> caller executes the outcome.
The machine never looks at global.operations or keymaps itself: it receives a
`resolver(op_key) -> resolved_action` callable (core/routing.resolve_operation
is the production one; tests inject fakes). Resolved actions are strings:
    "pass"          deliver the original key(s) unchanged
    "none"          trigger unused: tool stays hands-off (original key flows)
    "key:<combo>"   swallow the op and synthesize this combo (exactly once)
    "sink:<name>"   capture text and run the named sink (worker thread)

Hold decision (design §3.3 + §3.8, clarified by the design owner: "保留の要否は
第1層 global.operations の enter2/enter3 で決まる"):
    the decision is made on the LAYER-1 MEANINGS, not on the layer-2 resolution,
    because an undefined app resolves everything to "pass" yet must still hold a
    single Enter (delay only, no behaviour change — §3.8 and §4.7 example 4).
    The machine therefore accepts an optional `meaning_of(op_key) -> meaning|None`
    (production: routing.lookup_meaning bound to the live config). The two-arg
    form of the constructor still works: without it the machine falls back to
    "resolved action is not pass/none", which is equivalent for every profile
    that defines the keys and is what the tests of the fallback use.
    Passing it is a third *optional* argument — the frozen call form is unchanged.

Time model: every input carries an explicit monotonic t_ms; the machine never
reads a clock. The caller schedules a timer for `machine.deadline_ms` and calls
`on_deadline(t_ms)` when it fires.

Operational keys (layer-1 keys, normalized):
    "enter" (count 1), "enter2" (count 2), "enter3" (count 3),
    "shift+enter", "ctrl+enter", "alt+enter", "f1".."f12", any combo.

Transition table (design.md §3.4; P = policy config values):

  state      event                                        | next        | output
  -----------+---------------------------------------------+-------------+---------------------------
|  IDLE       Enter down, hold_mode=fast                    | IDLE        | TRIGGER/DELIVER-ish:
|             (or counts carry only pass/none)              |             | resolve count NOW, inject
|             -> no hold                                    |             | (identity => plain PASS)
|  IDLE       Enter down, deterministic and a higher count   | HOLDING     | HOLD (swallow, deadline =
|             count (2/3) carries a real meaning             |             | t+P.hold_window_ms)
|  HOLDING    Enter down via keyup path, window open, no    | IDLE/       | TRIGGER (count resolved:
|             higher count has a real meaning                | COOLDOWN*   | resolved action, swallowed)
|  HOLDING    Enter down, higher count still has real       | HOLDING     | HOLD (deadline =
|             meaning (2nd -> 3rd press)                    |             | t+P.triple_enter_window_ms)
|  HOLDING    Enter down at count 3 (the 4th press)         | CANCELLED   | CANCEL: the whole burst is
|             |                                             |             | dropped, NOTHING is delivered
|             |                                             |             | (mistyped-burst guard, §3.4)
|  CANCELLED  Enter down within the window                  | CANCELLED   | IGNORE (still eaten)
|  CANCELLED  deadline / other key / interrupt              | IDLE        | - (nothing delivered; the
|             |                                             |             | other key passes)
|  HOLDING    repeat Enter down (no keyup since last down)  | HOLDING     | IGNORE (swallowed)
|  HOLDING    OTHER key down / mouse button / focus change  | IDLE        | FLUSH: deliver the pending
|             (interrupt)                                   |             | FIRST (sync SendInput), then
|             |                                             |             | this event passes (swallow=False)
|  HOLDING    deadline fires                                | IDLE        | DELIVER (resolve pending count,
|             |                                             |             | inject resolved action once)
|  HOLDING    IME composition became active                 | IDLE        | DELIVER (user moved on)
|  HOLDING    config reload while disabled                  | IDLE        | DELIVER
|  any        Enter down while IME composing (True)         | unchanged   | PASS + count reset ("do not
|             |                                             |             |  fight the IME")
|  any        IME state unknown (composing=None)            | unchanged   | ime_unknown_policy: "pass"
|             |                                             |             |  => PASS; "process" => normal
|             |                                             |             |  Enter policy. Never silent: the
|             |                                             |             |  app logs + notifies once.
|  any        injected event (LLKHF_INJECTED / ev.injected) | unchanged   | PASS — rule0: NEVER processed
|             |                                             |             | (no count, no trigger, no
|             |                                             |             |  panic check). I4. 削除禁止.
|  any        chatter: same-vk down < P.chatter_debounce_ms | unchanged   | IGNORE
|             |  since last down w/o keyup                  |             |
|  any        layer-1 combo down (shift/ctrl/alt+enter, f*) | IDLE        | TRIGGER (swallow + resolved
|             |                                             |             | action; "key:" may inject sync)
|  BUSY       Enter down / layer-1 combo                    | BUSY        | counts resolve as count-1
|             |                                             |             | meaning immediately (no hold);
|             |                                             |             | sink-ops => DROP + "busy"
|             |                                             |             | notify + log; "key:" resolves
|  BUSY       SINK_DONE                                     | COOLDOWN    | (no key output)
|  COOLDOWN   Enter down before t+P.trigger_cooldown_ms     | COOLDOWN    | counts resolve as count-1
|             |                                             |             | meaning immediately (no hold)
|  COOLDOWN   deadline                                      | IDLE        | -
|  any        PANIC key down, burst < P.panic_repeat (rule1)                | unchanged   | PASS (the key itself
|             |                                             |             | flows; a lone F1 stays F1)
|  any        PANIC key down, burst reaches P.panic_repeat                  | PANIC       | PANIC: order-preserving
|             |                                             |             | FLUSH of pending FIRST, then
|             |                                             |             | caller unhooks; notify+log.
|             |                                             |             | Pending is delivered once,
|             |                                             |             | NEVER re-sent afterwards.
|  any        WATCHDOG_TRIP                                 | PANIC       | PANIC + best-effort flush
|             |                                             |             | (50ms budget) + caller
|             |                                             |             | self-exits(70)

  * BUSY is entered only when the resolved action is "sink:<name>" and the sink
    actually runs; "key:"/"pass"/"none" resolutions never enter BUSY.

Hold rules (design §3.3):
  - hold_mode="deterministic" (default): hold the 1st Enter whenever enter2 or
    enter3 carries a REAL meaning (send/newline/hermes/aichat/clipboard).
    "pass"/"none" counts do not hold. Defaults hold EVERY single Enter for
    up to hold_window_ms (measured, see §3.6) — accepted trade-off, §3.8.
  - the 2nd press extends the hold by triple_enter_window_ms (2nd -> 3rd gap);
    the 3rd press is held by the SAME window one last time so that a 4th press
    can still cancel the burst (a mistyped 4-press burst must never fire
    send/hermes). The cost is one extra window of latency on the count-3
    action, which is a sink (hermes) anyway.
  - hold_mode="fast": never hold; every down resolves at once (identity
    resolution short-circuits to PASS).
  - Counted Enters are ALWAYS swallowed: the app never receives a counted
    Enter, only the resolved action (I3).

Invariants (tests must assert these):
  I1. A swallowed Enter is delivered EXACTLY ONCE (resolve/flush) or consumed
      by TRIGGER/CANCEL — never twice; re-send/replay never happens.
  I2. Pending Enters never exceed 3, and a 4th press never grows the burst:
      it cancels it (CANCELLED swallows everything, delivers nothing).
  I3. NO counted Enter ever reaches the app — only the resolved action does.
  I4. injected events are never processed at all (rule0; loop-safety; 削除禁止).
  I5. IME-composing Enter always PASSes (the only pass-through exception).
  I6. Ordering: on interrupt, FLUSH's injection happens BEFORE the interrupting
      event is let through (あ→Enter→い must become あ改行い).
  I7. PANIC/stop/exception paths order-flush the pending key BEFORE unhook/
      exit — pending is never silently dropped, and never re-sent after that.
  I8. Unmatched profile (resolve layer returns "pass") => every op passes.

Caller contract (what the hook thread must do with an Outcome):
  PASS            let the event through (return 0 from the LL hook)
  HOLD/IGNORE     swallow (return 1). IGNORE never triggers anything.
  CANCEL          swallow (return 1) and deliver NOTHING; tell the user once
                  (a 4+ press burst was dropped on purpose).
  TRIGGER         swallow (return 1) and execute `resolved` for `count` copies:
                  "key:<combo>"   inject that combo once
                  "sink:<name>"   hand a job to the worker; ALWAYS follow with
                                  on_sink_done() (success, failure or fallback)
                  "pass"/"none"   inject `count` plain Enters (already swallowed)
  FLUSH           execute `resolved` synchronously FIRST, then let this event
                  through (return 0). The injection must not be queued: I6.
  DELIVER         execute `resolved` (timer/IME/reconfigure path); no hook event.
  PANIC           execute `resolved` (order-preserving flush), then unhook.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from . import keys as k
from .config import PolicyConfig
from .events import VK_RETURN, ImeState, KeyEvent


class State(Enum):
    IDLE = "idle"
    HOLDING = "holding"
    COOLDOWN = "cooldown"
    BUSY = "busy"
    CANCELLED = "cancelled"   # a 4+ press burst was cancelled; Enters are eaten
    PANIC = "panic"


class Decision(Enum):
    PASS = "pass"          # return 0 from the hook (event reaches the app)
    HOLD = "hold"          # swallow, arm/extend the pending deadline
    IGNORE = "ignore"      # swallow (repeat/chatter), no state change
    TRIGGER = "trigger"    # swallow; execute resolved action for this op now
    CANCEL = "cancel"      # swallow; 4+ press burst dropped, deliver NOTHING
    FLUSH = "flush"        # deliver pending FIRST (sync), then let THIS event pass
    DELIVER = "deliver"    # timer/interrupt: deliver pending resolved action (count = copies)
    PANIC = "panic"        # caller: flush pending (ordered) -> unhook NOW -> notify+log


# Resolved action vocabulary (routing.resolve_meaning output; frozen)
#   "pass" | "none" | "key:<combo>" | "sink:<name>"
RESOLVED_PASS = "pass"
RESOLVED_NONE = "none"
KEY_PREFIX = "key:"
SINK_PREFIX = "sink:"

# Layer-1 meanings that make the state machine hold (design §3.3).
REAL_MEANINGS = frozenset({"send", "newline", "hermes", "aichat", "clipboard"})
# Operation keys whose meanings decide whether a single Enter must be held.
HOLD_DECIDING_OPS = ("enter2", "enter3")

# Physical modifier VKs (left/right + generic). Used by held_modifiers().
MODIFIER_VKS = frozenset({0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
                          0x5B, 0x5C})


@dataclass(frozen=True)
class Outcome:
    decision: Decision
    state: State
    swallow: bool                 # True => hook returns 1 for THIS event
    action: str | None = None     # trigger operation key (informational)
    resolved: str = ""            # resolved action executed/queued by this outcome
    count: int | None = None      # copies of `resolved` to deliver (pass => plain Enters)
    reason: str = ""              # short machine-readable reason for the log


class EnterPolicyMachine:
    """See module docstring / docs/design.md §3.4 for the authoritative table.

    API (frozen):
        on_key(ev, ime=None)  -> Outcome      # every hook key event (rule0 first)
        on_deadline(t_ms)     -> Outcome      # timer fired at machine.deadline_ms
        on_interrupt(kind, t_ms) -> Outcome    # kind in {"key","mouse","foreground"}
        on_sink_done(t_ms)    -> Outcome      # worker thread finished a sink send
        on_panic(t_ms)        -> Outcome      # panic key / watchdog trip
        reconfigure(policy, resolver) -> Outcome
    Optional (additive, keyword): meaning_of(op_key) -> layer-1 meaning | None.
    Properties:
        state / deadline_ms / pending_count / pending_resolved
    """

    def __init__(self, policy: PolicyConfig,
                 resolver: Callable[[str], str],
                 meaning_of: Callable[[str], str | None] | None = None,
                 ime_unknown_policy: str = "pass") -> None:
        self._policy = policy
        self._resolver = resolver
        self._meaning_of = meaning_of
        # global.ime_unknown_policy (card t_dd246e5a): "pass" | "process"
        self._ime_unknown_policy = ime_unknown_policy

        self._state = State.IDLE
        self._deadline: int | None = None
        self._cooldown_until: int | None = None

        # held Enters: 1 or 2, plus the resolution snapshotted at hold start
        # (design §3.5-4: a focus change must NOT re-resolve into another app).
        self._pending_count = 0
        self._resolved_by_count: dict[int, str] = {}

        # per-key bookkeeping for OS repeat / chatter / swallowing the key-up
        self._down_vks: set[int] = set()
        self._last_down_ms: dict[int, int] = {}
        self._swallow_up: set[int] = set()
        self._enter_press_swallowed = False

        # panic-key burst counter (v2-g): (presses so far, last press time)
        self._panic_burst: tuple[int, int | None] = (0, None)

        # Burst counter for the no-hold path (hold_mode=fast, or no higher count
        # carrying a meaning): fast mode still distinguishes 1st/2nd/3rd press,
        # it just never delays them (design §3.3 "3 連続を押すと send と hermes
        # が両方発火しうる").
        self._burst: tuple[int, int | None] = (0, None)

    # ------------------------------------------------------------------ views
    @property
    def state(self) -> State:
        return self._state

    @property
    def deadline_ms(self) -> int | None:
        return self._deadline

    @property
    def pending_count(self) -> int:
        return self._pending_count

    @property
    def pending_resolved(self) -> str:
        if self._pending_count and self._state is State.HOLDING:
            return self._resolved_by_count.get(self._pending_count, "")
        return ""

    @property
    def policy(self) -> PolicyConfig:
        return self._policy

    def held_modifiers(self) -> frozenset[int]:
        """Modifier VKs the user is physically holding right now.

        ``_down_vks`` only ever contains events the hook reported as NOT injected
        (rule0), so this is the *physical* keyboard state, not our own injected
        keys. ``platform.inject.plan_events`` uses it to release a held Ctrl/Shift
        before synthesising a different key, so ``ctrl+enter -> send -> enter``
        really arrives as Enter instead of Ctrl+Enter (review round 1, item 4).
        """
        return frozenset(self._down_vks & MODIFIER_VKS)

    # ---------------------------------------------------------------- helpers
    def _out(self, decision: Decision, swallow: bool, *, reason: str,
             resolved: str = "", count: int | None = None,
             action: str | None = None) -> Outcome:
        return Outcome(decision=decision, state=self._state, swallow=swallow,
                       action=action, resolved=resolved, count=count, reason=reason)

    def _holds_on_meaning(self, ops: tuple[str, ...]) -> bool:
        """True when any of these layer-1 operations carries a real meaning."""
        if self._meaning_of is not None:
            return any(self._meaning_of(op) in REAL_MEANINGS for op in ops)
        # Fallback (no meaning source injected): use the layer-2 resolution.
        return any(self._resolver(op) not in (RESOLVED_PASS, RESOLVED_NONE)
                   for op in ops)

    def _should_hold_single_enter(self) -> bool:
        if self._policy.hold_mode != "deterministic":
            return False
        return self._holds_on_meaning(HOLD_DECIDING_OPS)

    def _mark_enter_swallowed(self) -> None:
        """We swallow this Enter down, so its key-up must be swallowed too."""
        self._enter_press_swallowed = True
        self._swallow_up.add(VK_RETURN)

    def _snapshot_resolutions(self) -> dict[int, str]:
        return {n: self._resolver(op) for n, op in
                ((1, "enter"), (2, "enter2"), (3, "enter3"))}

    # ------------------------------------------------------------ live keys
    def _emit_live_enter(self, count: int, resolved: str, reason: str,
                         drop_sinks: bool = False) -> Outcome:
        """A counted Enter resolved right now (IDLE/BUSY/COOLDOWN/fast).

        The current press is a LIVE event, so "pass" simply lets it through
        (earlier presses of a fast-mode burst already went out — replaying them
        here would double-send). drop_sinks=True is the BUSY row of §3.4: while a
        sink is running, another sink trigger is DROPPED (notified + logged).
        """
        if drop_sinks and resolved.startswith(SINK_PREFIX):
            self._mark_enter_swallowed()
            return self._out(Decision.IGNORE, True, reason="busy_drop",
                             resolved=resolved, count=1)
        if resolved in (RESOLVED_PASS, RESOLVED_NONE):
            # "the key flows": this is the single press the user made
            self._enter_press_swallowed = False
            return self._out(Decision.PASS, False, reason=reason,
                             resolved=resolved, count=count,
                             action=self._count_op(count))
        if resolved.startswith(KEY_PREFIX):
            combo = resolved[len(KEY_PREFIX):]
            if count == 1 and combo == "enter":
                # identity short-circuit (design §3.3): one plain Enter is
                # exactly what was pressed -> no swallow, no round trip.
                self._enter_press_swallowed = False
                return self._out(Decision.PASS, False, reason="identity_pass",
                                 resolved=resolved, count=1, action="enter")
            self._mark_enter_swallowed()
            return self._out(Decision.TRIGGER, True, reason=reason,
                             resolved=resolved, count=1, action=self._count_op(count))
        # sink:<name> — the sink runs on the worker; BUSY starts now.
        self._mark_enter_swallowed()
        self._state = State.BUSY
        return self._out(Decision.TRIGGER, True, reason=reason,
                         resolved=resolved, count=1, action=self._count_op(count))

    def _next_burst_count(self, t_ms: int) -> int:
        """Consecutive-Enter counter for paths without a hold window."""
        prev_count, prev_t = self._burst
        if prev_t is None or not 1 <= prev_count < 3:
            return 1
        window = (self._policy.hold_window_ms if prev_count == 1
                  else self._policy.triple_enter_window_ms)
        return prev_count + 1 if (t_ms - prev_t) <= window else 1

    @staticmethod
    def _count_op(count: int) -> str:
        return "enter" if count <= 1 else f"enter{count}"

    def _emit_pending(self, decision: Decision, count: int, reason: str,
                      swallow: bool) -> Outcome:
        """Resolve and deliver the held Enters (deadline/interrupt/repeat-resolve).

        The resolution snapshot taken at hold start is used verbatim (design
        §3.5-4). `swallow` is True only when a live key event is being consumed
        by this decision (the 2nd/3rd Enter that completes the burst).
        """
        resolved = self._resolved_by_count.get(count, RESOLVED_PASS)
        op = self._count_op(count)
        self._pending_count = 0
        self._deadline = None
        self._resolved_by_count = {}
        self._burst = (0, None)          # the burst is finished either way
        if decision is not Decision.PANIC:
            self._state = State.IDLE
        if swallow:
            self._mark_enter_swallowed()
        if resolved.startswith(SINK_PREFIX):
            if decision is not Decision.PANIC:
                self._state = State.BUSY
            return self._out(decision, swallow, reason=reason, resolved=resolved,
                             count=1, action=op)
        if resolved.startswith(KEY_PREFIX):
            return self._out(decision, swallow, reason=reason, resolved=resolved,
                             count=1, action=op)
        # pass / none: replay the swallowed Enters, in order, exactly once (I1/I3)
        return self._out(decision, swallow, reason=reason,
                         resolved=RESOLVED_PASS, count=count, action=op)

    # ---------------------------------------------------------------- events
    def on_key(self, ev: KeyEvent, ime: ImeState | None = None) -> Outcome:
        # rule0 (削除禁止): our own injected keys are never processed at all —
        # no counting, no trigger, no panic check (I4 / design §6-6).
        if ev.injected:
            return self._out(Decision.PASS, False, reason="injected")

        if not self._policy.enabled:
            return self._out(Decision.PASS, False, reason="disabled")

        if ev.kind == "up":
            return self._on_up(ev)

        # rule1: panic key, after rule0 and before everything else (design §6-4).
        # v2-g: the stop is a BURST of one key (default F1 x3), not a chord —
        # a chord is not recalled in an emergency and cannot be typed one-handed.
        combo = k.combo_of(ev.vk, ev.ctrl, ev.alt, ev.shift, ev.win)
        if combo and k.same_combo(combo, self._policy.panic_key):
            return self._on_panic_key(ev)

        # Terminal state: once we panicked the hook is being removed, so nothing
        # may be swallowed any more (I7: give the keyboard back immediately).
        if self._state is State.PANIC:
            return self._out(Decision.PASS, False, reason="panic_passthrough")

        # rule2: never fight the IME — checked before op detection/repeat (I5).
        # Three-valued state: True = composing (the IME owns this Enter),
        # False = readable and no composition, None = the probe FAILED.
        # Unknown is never silently swallowed into "pass": the configured
        # ime_unknown_policy decides, and app/main.py announces it once.
        if ev.vk == VK_RETURN and ime is not None:
            if ime.composing is True:
                return self._on_ime_composing(ev)
            if ime.composing is None and self._ime_unknown_policy == "pass":
                return self._out(Decision.PASS, False, reason="ime_unknown")

        # OS repeat / chatter: a down without an intervening up is the SAME press
        if ev.vk in self._down_vks or ev.os_repeat:
            return self._on_repeat(ev)
        self._down_vks.add(ev.vk)
        self._last_down_ms[ev.vk] = ev.t_ms

        if ev.vk == VK_RETURN:
            mods, _key = k.split_combo(combo) if combo else ((), "")
            if mods:
                return self._on_combo_down(ev, combo)
            return self._on_plain_enter_down(ev)
        return self._on_other_down(ev, combo)

    def _on_panic_key(self, ev: KeyEvent) -> Outcome:
        """Count presses of ``panic_key``; reaching ``panic_repeat`` stops the tool.

        The presses themselves are NOT swallowed before the burst completes: a
        lone F1 keeps working as the app's F1 (its layer-1 meaning is ``none`` by
        default, so nothing happens), and swallowing then replaying would add the
        same 250ms delay the Enter hold already costs. The window is the measured
        burst window, so "F1 F1 F1 at typing speed" is the same gesture the user
        already makes with Enter.
        """
        window = self._policy.triple_enter_window_ms
        prev_count, prev_t = self._panic_burst
        count = (prev_count + 1
                 if prev_t is not None and 0 <= ev.t_ms - prev_t <= window else 1)
        self._panic_burst = (count, ev.t_ms)
        if count >= self._policy.panic_repeat:
            self._panic_burst = (0, None)
            return self.on_panic(ev.t_ms, reason=f"panic_key_x{count}")
        return self._out(Decision.PASS, False, reason=f"panic_arm_{count}")

    def _on_ime_composing(self, ev: KeyEvent) -> Outcome:
        """An Enter while an IME composition exists: it belongs to the IME."""
        if self._state is State.HOLDING and self._pending_count:
            # deliver the held Enter first (order preserved), then let the IME
            # have this Enter — the user moved on, we do not wait (§3.4).
            return self._emit_pending(Decision.DELIVER, self._pending_count,
                                      "ime_composing", swallow=False)
        self._burst = (0, None)     # a commit is not a burst press (v2-h)
        return self._out(Decision.PASS, False, reason="ime_composing")

    def on_ime_commit(self, ev: KeyEvent) -> Outcome:
        """An Enter handed to the IME as a conversion commit (v2-h).

        Same handling as a positively observed composition: the Enter belongs to
        the IME and must NOT count toward a burst. Otherwise the commit press
        became "press 1" and the next single Enter turned into ``enter2`` (send) —
        measured: typing Japanese, Enter to commit, then one more Enter sent the
        message instead of inserting a newline.
        """
        if self._state is State.HOLDING and self._pending_count:
            return self._emit_pending(Decision.DELIVER, self._pending_count,
                                      "ime_commit", swallow=False)
        self._burst = (0, None)
        return self._out(Decision.PASS, False, reason="ime_commit")

    def _on_up(self, ev: KeyEvent) -> Outcome:
        self._down_vks.discard(ev.vk)
        self._last_down_ms.pop(ev.vk, None)
        if ev.vk == VK_RETURN:
            swallowed = self._enter_press_swallowed
            self._enter_press_swallowed = False
            if swallowed:
                self._swallow_up.discard(ev.vk)
                return self._out(Decision.IGNORE, True, reason="up_of_swallowed")
            self._swallow_up.discard(ev.vk)
            return self._out(Decision.PASS, False, reason="enter_up")
        if ev.vk in self._swallow_up:
            self._swallow_up.discard(ev.vk)
            return self._out(Decision.IGNORE, True, reason="up_of_swallowed")
        return self._out(Decision.PASS, False, reason="up")

    def _on_repeat(self, ev: KeyEvent) -> Outcome:
        if ev.vk == VK_RETURN:
            # Counted Enters are ours: never let an auto-repeat / duplicate down
            # become a second activation (I1, "keyup を挟まない連続 Enter down は 1 扱い").
            #
            # chatter_debounce_ms does NOT re-count a down without a key-up: doing
            # that would let OS auto-repeat fire send/hermes while a key is held.
            # The value is used to DISTINGUISH the two cases in the log instead:
            # a gap >= chatter_debounce_ms means we missed a key-up (worth seeing
            # in the log), a shorter gap is the hardware chatter the setting is
            # named for. Both stay "the same press".
            gap = ev.t_ms - self._last_down_ms.get(ev.vk, ev.t_ms)
            stale = gap >= self._policy.chatter_debounce_ms
            reason = "enter_repeat_stale" if stale else "enter_repeat"
            if (self._enter_press_swallowed
                    or self._state in (State.HOLDING, State.CANCELLED)):
                if self._state in (State.HOLDING, State.CANCELLED):
                    self._mark_enter_swallowed()
                return self._out(Decision.IGNORE, True, reason=reason)
            return self._out(Decision.PASS, False, reason=reason + "_pass")
        # Other keys: the app owns their auto-repeat.
        return self._out(Decision.PASS, False, reason="repeat")

    def _on_plain_enter_down(self, ev: KeyEvent) -> Outcome:
        if self._state is State.PANIC:
            return self._out(Decision.PASS, False, reason="panic_passthrough")

        if self._state is State.CANCELLED:
            # Same burst still going: keep eating it, deliver nothing. The
            # window slides so a long burst cannot leak a press afterwards.
            self._deadline = ev.t_ms + self._policy.triple_enter_window_ms
            self._mark_enter_swallowed()
            return self._out(Decision.IGNORE, True, reason="cancel_swallow")

        if self._state is State.HOLDING:
            return self._advance_hold(ev)

        if self._state is State.BUSY:
            # count-1 meaning immediately, no hold; sink ops are dropped (§3.4 BUSY)
            return self._emit_live_enter(1, self._resolver("enter"), "busy_count1",
                                         drop_sinks=True)

        if self._state is State.COOLDOWN:
            if self._cooldown_until is not None and ev.t_ms < self._cooldown_until:
                return self._emit_live_enter(1, self._resolver("enter"), "cooldown_count1")
            self._state = State.IDLE
            self._deadline = None
            self._cooldown_until = None

        if self._should_hold_single_enter():
            self._burst = (1, ev.t_ms)
            return self._start_hold(ev)
        count = self._next_burst_count(ev.t_ms)
        self._burst = (count, ev.t_ms)
        reason = "immediate" if count == 1 else f"fast_count{count}"
        return self._emit_live_enter(count, self._resolver(self._count_op(count)), reason)

    def _start_hold(self, ev: KeyEvent) -> Outcome:
        self._state = State.HOLDING
        self._pending_count = 1
        self._resolved_by_count = self._snapshot_resolutions()
        self._deadline = ev.t_ms + self._policy.hold_window_ms
        self._mark_enter_swallowed()
        return self._out(Decision.HOLD, True, reason="hold_start", action="enter",
                         resolved=self._resolved_by_count[1], count=1)

    def _advance_hold(self, ev: KeyEvent) -> Outcome:
        # A fresh press (key-up seen) while holding: extend, cancel or resolve.
        if self._pending_count >= 3:
            # 4th press inside the window: the burst is a typo, drop all of it
            # (design §3.4 CANCELLED row). Nothing is delivered — a mistyped
            # 4-press burst must never fire send/hermes.
            return self._cancel_burst(ev)
        if self._pending_count == 2:
            # 3rd press: hold once more (same window) so a 4th press can still
            # cancel it. The count-3 action fires on the deadline instead of
            # here; that costs one window and buys the typo guard.
            self._pending_count = 3
            self._burst = (3, ev.t_ms)
            self._deadline = ev.t_ms + self._policy.triple_enter_window_ms
            self._mark_enter_swallowed()
            return self._out(Decision.HOLD, True, reason="hold_extend_third",
                             action="enter3", resolved=self._resolved_by_count[3],
                             count=3)
        if self._holds_on_meaning(("enter3",)):
            self._pending_count = 2
            self._burst = (2, ev.t_ms)
            self._deadline = ev.t_ms + self._policy.triple_enter_window_ms
            self._mark_enter_swallowed()
            return self._out(Decision.HOLD, True, reason="hold_extend", action="enter2",
                             resolved=self._resolved_by_count[2], count=2)
        return self._emit_pending(Decision.TRIGGER, 2, "second_enter_no_higher",
                                  swallow=True)

    def _cancel_burst(self, ev: KeyEvent) -> Outcome:
        """4+ consecutive Enters: eat the burst, deliver nothing (design §3.4).

        The dropped presses are NOT replayed (the user meant "that was a
        mistake"), and further Enters of the same burst stay swallowed until
        the window expires or another key ends it. The caller announces this
        once (Decision.CANCEL) so a silent nothing never looks like a bug.
        """
        dropped = self._pending_count
        action = self._count_op(dropped)
        self._pending_count = 0
        self._resolved_by_count = {}
        self._burst = (0, None)
        self._state = State.CANCELLED
        self._deadline = ev.t_ms + self._policy.triple_enter_window_ms
        self._mark_enter_swallowed()
        return self._out(Decision.CANCEL, True, reason=f"burst_cancel_{dropped}",
                         action=action)

    def _on_combo_down(self, ev: KeyEvent, combo: str) -> Outcome:
        if self._state is State.PANIC:
            return self._out(Decision.PASS, False, reason="panic_passthrough")
        if self._state is State.CANCELLED:
            # A real key ends the cancelled burst; the key itself is normal.
            self._leave_cancel()
        if self._state is State.HOLDING:
            # §3.4: another key while holding -> FLUSH first, then this event passes
            return self._emit_pending(Decision.FLUSH, self._pending_count,
                                      "flush_key", swallow=False)
        self._burst = (0, None)          # a combo ends a plain-Enter burst
        resolved = self._resolver(combo)
        if resolved in (RESOLVED_PASS, RESOLVED_NONE):
            return self._out(Decision.PASS, False, reason="combo_pass",
                             resolved=resolved, action=combo)
        if self._state is State.BUSY and resolved.startswith(SINK_PREFIX):
            # §3.4 BUSY row: a sink op while a sink runs is dropped, not queued.
            self._mark_enter_swallowed()
            return self._out(Decision.IGNORE, True, reason="busy_drop",
                             resolved=resolved, count=1, action=combo)
        # Combos are always swallowed and re-synthesized (design §4.7 example 1:
        # "押下元を抑止し Ctrl+Enter を注入（同一だが経路は一貫）").
        self._mark_enter_swallowed()
        if resolved.startswith(KEY_PREFIX):
            return self._out(Decision.TRIGGER, True, reason="combo_trigger",
                             resolved=resolved, count=1, action=combo)
        self._state = State.BUSY
        return self._out(Decision.TRIGGER, True, reason="combo_trigger",
                         resolved=resolved, count=1, action=combo)

    def _on_other_down(self, ev: KeyEvent, combo: str) -> Outcome:
        if self._state is State.HOLDING:
            # The keystroke that ends the hold must reach the app AFTER the
            # pending Enter (I6: あ→Enter→い => あ改行い).
            return self._emit_pending(Decision.FLUSH, self._pending_count,
                                      "flush_key", swallow=False)
        if self._state is State.PANIC:
            return self._out(Decision.PASS, False, reason="panic_passthrough")
        if self._state is State.CANCELLED:
            # Any real key ends the cancelled burst (nothing is delivered).
            self._leave_cancel()
        # Another key ends a consecutive-Enter burst (typing is not double-tapping).
        self._burst = (0, None)
        if not combo:
            return self._out(Decision.PASS, False, reason="modifier")
        resolved = self._resolver(combo)
        if resolved in (RESOLVED_PASS, RESOLVED_NONE):
            return self._out(Decision.PASS, False, reason="key_pass",
                             resolved=resolved, action=combo)
        # An arbitrary combo bound to a key/sink action (design §3.1 "任意 combo 追加可").
        self._swallow_up.add(ev.vk)
        if resolved.startswith(KEY_PREFIX):
            return self._out(Decision.TRIGGER, True, reason="combo_trigger",
                             resolved=resolved, count=1, action=combo)
        self._state = State.BUSY
        return self._out(Decision.TRIGGER, True, reason="combo_trigger",
                         resolved=resolved, count=1, action=combo)

    def _leave_cancel(self) -> None:
        """End a cancelled burst: back to IDLE, nothing pending, nothing sent."""
        if self._state is State.CANCELLED:
            self._state = State.IDLE
        self._deadline = None
        self._pending_count = 0
        self._resolved_by_count = {}
        self._burst = (0, None)

    # ------------------------------------------------------------- timers etc
    def on_deadline(self, t_ms: int) -> Outcome:
        if self._state is State.CANCELLED:
            if self._deadline is not None and t_ms < self._deadline:
                return self._out(Decision.IGNORE, False, reason="cancel_wait")
            self._leave_cancel()
            return self._out(Decision.IGNORE, False, reason="cancel_end")
        if self._state is State.HOLDING and self._pending_count:
            # A timer that fires early (buggy/racy caller) must not deliver
            # before the configured window has actually elapsed.
            if self._deadline is not None and t_ms < self._deadline:
                return self._out(Decision.IGNORE, False, reason="deadline_early")
            return self._emit_pending(Decision.DELIVER, self._pending_count,
                                      "deadline", swallow=False)
        if self._state is State.COOLDOWN:
            if self._cooldown_until is None or t_ms >= self._cooldown_until:
                self._state = State.IDLE
                self._deadline = None
                self._cooldown_until = None
                return self._out(Decision.IGNORE, False, reason="cooldown_expired")
            return self._out(Decision.IGNORE, False, reason="cooldown_wait")
        return self._out(Decision.IGNORE, False, reason="deadline_noop")

    def on_interrupt(self, kind: str, t_ms: int) -> Outcome:
        """Mouse button / foreground change / a key the caller does not route.

        kind == "foreground": the caller must inject into the window captured at
        hold start (design §3.5-1), never into the new foreground.
        """
        if self._state is State.HOLDING and self._pending_count:
            return self._emit_pending(Decision.FLUSH, self._pending_count,
                                      f"flush_{kind}", swallow=False)
        if self._state is State.CANCELLED:
            self._leave_cancel()
            return self._out(Decision.IGNORE, False,
                             reason=f"interrupt_{kind}_cancel_end")
        return self._out(Decision.IGNORE, False, reason=f"interrupt_{kind}_noop")

    def on_sink_done(self, t_ms: int) -> Outcome:
        if self._state is not State.BUSY:
            return self._out(Decision.IGNORE, False, reason="sink_done_noop")
        cooldown = self._policy.trigger_cooldown_ms
        if cooldown <= 0:
            self._state = State.IDLE
            self._deadline = None
            return self._out(Decision.IGNORE, False, reason="sink_done")
        self._state = State.COOLDOWN
        self._cooldown_until = t_ms + cooldown
        self._deadline = self._cooldown_until
        return self._out(Decision.IGNORE, False, reason="sink_done")

    def on_panic(self, t_ms: int, reason: str = "panic_key") -> Outcome:
        """Order-preserving flush of the held Enter, then the caller unhooks (I7)."""
        pending = self._pending_count
        self._state = State.PANIC
        self._deadline = None
        if pending:
            return self._emit_pending(Decision.PANIC, pending, reason, swallow=False)
        return self._out(Decision.PANIC, False, reason=reason)

    def reconfigure(self, policy: PolicyConfig,
                    resolver: Callable[[str], str],
                    ime_unknown_policy: str | None = None) -> Outcome:
        """Hot config reload. Never drops a held Enter (delivers it first)."""
        delivered: Outcome | None = None
        if self._state is State.HOLDING and self._pending_count:
            delivered = self._emit_pending(Decision.DELIVER, self._pending_count,
                                           "reconfigure", swallow=False)
        self._policy = policy
        self._resolver = resolver
        if ime_unknown_policy is not None:
            self._ime_unknown_policy = ime_unknown_policy
        if not policy.enabled and self._state is not State.PANIC:
            self._state = State.IDLE
            self._deadline = None
            self._cooldown_until = None
        if delivered is not None:
            return delivered
        return self._out(Decision.IGNORE, False, reason="reconfigured")
