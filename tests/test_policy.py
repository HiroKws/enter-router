"""State-machine tests for core/policy.py — invariants I1..I8 (design.md §3.7).

Run on Linux/WSL:  python3 -m unittest discover -s tests -v

The harness below plays the role of the hook thread + executor: it feeds real
KeyEvents into the machine and records exactly what the application would
receive (passthrough or injected), in order. That is what makes I3 ("no counted
Enter reaches the app") and I6 ("あ→Enter→い becomes あ改行い") assertable
without Windows.
"""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.core import config, keys, policy, routing  # noqa: E402
from enter_router.core.config import ConfigError, parse_config_text  # noqa: E402
from enter_router.core.events import VK_RETURN, ForegroundInfo, ImeState, KeyEvent  # noqa: E402
from enter_router.core.policy import (  # noqa: E402
    Decision, EnterPolicyMachine, Outcome, State,
)

SAMPLE = REPO / "config" / "sample.config.json"

FG_A = ForegroundInfo(process_name="firefox.exe", window_title="Site A",
                      url="https://site-a.example.com/chat")
FG_B = ForegroundInfo(process_name="chrome.exe", window_title="Site B",
                      url="https://site-b.example.com/chat")
FG_EXCEL = ForegroundInfo(process_name="excel.exe", window_title="Book1 - Excel")
FG_NOTEPAD = ForegroundInfo(process_name="notepad.exe", window_title="Untitled")

VK_A = 0x41
VK_F1 = 0x70
VK_F5 = 0x74
VK_F12 = 0x7B


def sample_raw() -> dict:
    import json
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def sample_cfg() -> config.Config:
    return parse_config_text(SAMPLE.read_text(encoding="utf-8"))


class Harness:
    """Hook-thread + executor stand-in that records the resulting app input."""

    def __init__(self, cfg: config.Config, fg: ForegroundInfo,
                 policy_cfg: config.PolicyConfig | None = None,
                 with_meanings: bool = True,
                 auto_sink_done: bool = True,
                 src: config.Config | None = None,
                 ime_unknown_policy: str = "pass"):
        self.cfg = cfg
        self.fg = fg
        self.auto_sink_done = auto_sink_done
        self.profile = routing.match_profile(cfg, fg)
        # `src` lets a test resolve against a different config than the one
        # driving the policy numbers (used to prove I4 skips the resolver).
        self._src = src or cfg
        kwargs = {}
        if with_meanings:
            kwargs["meaning_of"] = lambda op: routing.lookup_meaning(self._src, op)
        self.machine = EnterPolicyMachine(
            policy_cfg or cfg.policy,
            lambda op: routing.resolve_operation(self._src, fg, op),
            ime_unknown_policy=ime_unknown_policy,
            **kwargs)
        self.app_input: list[tuple[str, str]] = []   # ("down"/"up"/"inject", name)
        self.outcomes: list[Outcome] = []
        self.sinks: list[str] = []
        self.resolver_calls = 0
        # wrap the resolver to count calls (asserted by the snapshot test)
        inner = self.machine._resolver
        self.resolver = self._counting(inner)
        self.machine._resolver = self.resolver
        self._up_at = 20

    def _counting(self, inner):
        def wrapper(op):
            self.resolver_calls += 1
            return inner(op)
        return wrapper

    # -- input ------------------------------------------------------------
    def key(self, t, kind="down", vk=VK_RETURN, ctrl=False, shift=False,
            alt=False, win=False, injected=False, os_repeat=False, ime=None):
        ev = KeyEvent(vk=vk, kind=kind, t_ms=t, ctrl=ctrl, shift=shift, alt=alt,
                      win=win, injected=injected, os_repeat=os_repeat)
        out = self.machine.on_key(ev, ime)
        self._apply(ev, out)
        return out

    def enter(self, t, ctrl=False, shift=False, alt=False, **kw):
        """One full press (down now, up 20ms later unless the test wants it held)."""
        out = self.key(t, "down", VK_RETURN, ctrl=ctrl, shift=shift, alt=alt, **kw)
        return out

    def up(self, t, vk=VK_RETURN, ctrl=False, shift=False, alt=False):
        return self.key(t, "up", vk, ctrl=ctrl, shift=shift, alt=alt)

    def press(self, t, **kw):
        """down + up, returns the down outcome."""
        out = self.enter(t, **kw)
        self.up(t + self._up_at, **{k: v for k, v in kw.items()
                                    if k in ("ctrl", "shift", "alt")})
        return out

    def deadline(self, t):
        out = self.machine.on_deadline(t)
        self._apply(None, out)
        return out

    def interrupt(self, kind, t):
        out = self.machine.on_interrupt(kind, t)
        self._apply(None, out)
        return out

    def sink_done(self, t):
        return self.machine.on_sink_done(t)

    def panic(self, t):
        out = self.machine.on_panic(t)
        self._apply(None, out)
        return out

    # -- executor stand-in -------------------------------------------------
    def _apply(self, ev, out):
        self.outcomes.append(out)
        executed = out.decision in (Decision.TRIGGER, Decision.DELIVER,
                                    Decision.FLUSH, Decision.PANIC)
        if executed and out.resolved.startswith("key:"):
            self.app_input.append(("inject", out.resolved[4:]))
        elif executed and out.resolved.startswith("sink:"):
            self.sinks.append(out.resolved)
            if self.auto_sink_done:
                self.machine.on_sink_done((ev.t_ms if ev else 0) + 1)
        elif executed and out.resolved in ("pass", "none"):
            for _ in range(out.count or 0):
                self.app_input.append(("inject", "enter"))
        if not out.swallow and ev is not None:
            name = keys.key_name(ev.vk) or "modifier"
            self.app_input.append((ev.kind, name))

    # -- assertions helpers -----------------------------------------------
    def app_enters(self, kind="down"):
        return [n for k, n in self.app_input if k == kind and n == "enter"]

    def injected(self):
        return [n for k, n in self.app_input if k == "inject"]


# ---------------------------------------------------------------------------
# A. construction / defaults
# ---------------------------------------------------------------------------
class TestConstruction(unittest.TestCase):
    def test_initial_state_and_views(self):
        m = EnterPolicyMachine(sample_cfg().policy, lambda op: "pass")
        self.assertIs(m.state, State.IDLE)
        self.assertIsNone(m.deadline_ms)
        self.assertEqual(m.pending_count, 0)
        self.assertEqual(m.pending_resolved, "")

    def test_two_arg_form_still_works(self):
        """> the frozen call form is EnterPolicyMachine(policy, resolver)."""
        m = EnterPolicyMachine(sample_cfg().policy, lambda op: "key:enter")
        self.assertIs(m.state, State.IDLE)

    def test_keys_module_matches_config_normalizer(self):
        for combo in ("enter", "Shift + Enter", "ctrl+SHIFT+enter", "F12",
                      "alt+enter", "ctrl+alt+shift+f12", "ctrl+k"):
            with self.subTest(combo=combo):
                self.assertEqual(keys.normalize_combo(combo),
                                 config._norm_combo(combo))
        for combo in ("control+enter", "ctrl+shift", ""):
            with self.subTest(combo=combo):
                with self.assertRaises(ValueError):
                    keys.normalize_combo(combo)
                with self.assertRaises(ValueError):
                    config._norm_combo(combo)

    def test_combo_helpers(self):
        self.assertEqual(keys.combo_of(VK_RETURN), "enter")
        self.assertEqual(keys.combo_of(VK_RETURN, ctrl=True), "ctrl+enter")
        self.assertEqual(keys.combo_of(VK_F12, ctrl=True, alt=True, shift=True),
                         "ctrl+alt+shift+f12")
        self.assertEqual(keys.key_name(0x11), "")          # modifier: no base key
        self.assertEqual(keys.parse_combo("ctrl+shift+enter"),
                         ((0x11, 0x10), VK_RETURN))
        with self.assertRaises(ValueError):
            keys.normalize_combo("ctrl+shift")


# ---------------------------------------------------------------------------
# B. hold / deliver / count (I1, I2) — site-a, deterministic
# ---------------------------------------------------------------------------
class TestHoldAndCount(unittest.TestCase):
    def setUp(self):
        self.h = Harness(sample_cfg(), FG_A)

    def test_first_enter_is_held_with_a_deadline(self):
        out = self.h.enter(1000)
        self.assertIs(out.decision, Decision.HOLD)
        self.assertTrue(out.swallow)
        self.assertIs(out.state, State.HOLDING)
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertEqual(out.count, 1)
        self.assertEqual(self.h.machine.deadline_ms, 1000 + 250)
        self.assertEqual(self.h.machine.pending_count, 1)
        self.assertEqual(self.h.machine.pending_resolved, "key:shift+enter")
        self.assertEqual(self.h.app_input, [])      # nothing reached the app yet

    def test_swallowed_down_swallows_its_up(self):
        self.h.enter(1000)
        up = self.h.up(1020)
        self.assertTrue(up.swallow)
        self.assertEqual(up.reason, "up_of_swallowed")

    def test_deadline_delivers_the_single_enter_exactly_once(self):
        self.h.enter(1000)
        out = self.h.deadline(1350)
        self.assertIs(out.decision, Decision.DELIVER)
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertEqual(out.count, 1)
        self.assertIs(out.state, State.IDLE)
        self.assertEqual(self.h.machine.pending_count, 0)
        self.assertEqual(self.h.injected(), ["shift+enter"])
        # I1: a second timer tick must not deliver anything again
        again = self.h.deadline(1300)
        self.assertIs(again.decision, Decision.IGNORE)
        self.assertEqual(self.h.injected(), ["shift+enter"])

    def test_early_deadline_does_not_deliver(self):
        self.h.enter(1000)
        out = self.h.deadline(1249)
        self.assertIs(out.decision, Decision.IGNORE)
        self.assertEqual(out.reason, "deadline_early")
        self.assertEqual(self.h.machine.pending_count, 1)
        self.assertEqual(self.h.injected(), [])

    def test_second_enter_extends_the_hold(self):
        self.h.enter(1000)
        self.h.up(1020)
        out = self.h.enter(1100)
        self.assertIs(out.decision, Decision.HOLD)
        self.assertEqual(out.reason, "hold_extend")
        self.assertEqual(out.count, 2)
        self.assertEqual(out.resolved, "key:ctrl+enter")
        self.assertEqual(self.h.machine.pending_count, 2)
        # 1->2 extension uses triple_enter_window_ms (250), not the hold window
        self.assertEqual(self.h.machine.deadline_ms, 1100 + 250)

    def test_third_enter_is_held_one_more_window_then_triggers(self):
        """The 3rd press waits one window so a 4th press can still cancel it."""
        self.h.enter(1000); self.h.up(1020)
        self.h.enter(1100); self.h.up(1120)
        out = self.h.enter(1200)
        self.assertIs(out.decision, Decision.HOLD)
        self.assertEqual(out.reason, "hold_extend_third")
        self.assertTrue(out.swallow)
        self.assertEqual(out.count, 3)
        self.assertEqual(out.resolved, "sink:hermes")
        self.assertIs(self.h.machine.state, State.HOLDING)
        self.assertEqual(self.h.machine.pending_count, 3)
        self.assertEqual(self.h.machine.deadline_ms, 1200 + 250)
        self.assertEqual(self.h.sinks, [])          # nothing fired yet
        out = self.h.deadline(1450)
        self.assertIs(out.decision, Decision.DELIVER)
        self.assertEqual(out.resolved, "sink:hermes")
        self.assertIs(out.state, State.BUSY)        # sink runs -> BUSY
        self.assertEqual(self.h.sinks, ["sink:hermes"])
        self.assertEqual(self.h.machine.pending_count, 0)

    def test_four_enters_cancel_the_burst_and_deliver_nothing(self):
        """A mistyped 4-press burst must never fire send/hermes (design §3.4)."""
        self.h.enter(1000); self.h.up(1020)
        self.h.enter(1100); self.h.up(1120)
        self.h.enter(1200); self.h.up(1220)
        out = self.h.enter(1260)                    # the 4th press
        self.assertIs(out.decision, Decision.CANCEL)
        self.assertTrue(out.swallow)
        self.assertIs(out.state, State.CANCELLED)
        self.assertEqual(self.h.machine.pending_count, 0)
        self.assertEqual(self.h.sinks, [])
        self.assertEqual(self.h.injected(), [])
        self.h.up(1280)
        # the rest of the burst stays eaten, and nothing is delivered on expiry
        for t in (1300, 1350, 1400):
            swallow = self.h.press(t)
            self.assertTrue(swallow.swallow)
            self.assertIs(swallow.decision, Decision.IGNORE)
            self.assertEqual(swallow.reason, "cancel_swallow")
        out = self.h.deadline(1650)
        self.assertIs(out.decision, Decision.IGNORE)
        self.assertEqual(out.reason, "cancel_end")
        self.assertIs(self.h.machine.state, State.IDLE)
        self.assertEqual(self.h.injected(), [])
        self.assertEqual(self.h.sinks, [])
        self.assertEqual(self.h.app_enters("down"), [])

    def test_another_key_ends_a_cancelled_burst_and_passes(self):
        self.h.enter(1000); self.h.up(1020)
        self.h.enter(1100); self.h.up(1120)
        self.h.enter(1200); self.h.up(1220)
        self.h.enter(1260)
        self.h.up(1280)
        out = self.h.key(1300, "down", VK_A)
        self.assertIs(out.decision, Decision.PASS)
        self.assertIs(self.h.machine.state, State.IDLE)
        self.assertIn(("down", "a"), self.h.app_input)

    def test_the_next_burst_after_a_cancel_works_again(self):
        self.h.enter(1000); self.h.up(1020)
        self.h.enter(1100); self.h.up(1120)
        self.h.enter(1200); self.h.up(1220)
        self.h.enter(1260); self.h.up(1280)
        self.h.deadline(1600)                       # cancel window expires
        self.h.enter(3000); self.h.up(3020)
        out = self.h.enter(3100)
        self.assertIs(out.decision, Decision.HOLD)
        self.assertEqual(out.reason, "hold_extend")
        out = self.h.deadline(3450)
        self.assertIs(out.decision, Decision.DELIVER)
        self.assertEqual(out.resolved, "key:ctrl+enter")

    def test_i2_pending_never_exceeds_three(self):
        self.h.enter(1000); self.h.up(1020)
        self.h.enter(1100); self.h.up(1120)
        self.h.enter(1200); self.h.up(1220)
        self.assertLessEqual(self.h.machine.pending_count, 3)
        self.h.enter(1260)                          # 4th press cancels instead
        self.assertEqual(self.h.machine.pending_count, 0)

    def test_two_enters_from_the_deadline_deliver_one_send_no_app_enter(self):
        """acceptance: 2 連打で送信 1 回だけ、素の Enter は 0 回.

        site-b sends with a plain Enter, so the delivered action *is* an Enter —
        but it must be the only one, and no counted Enter may pass (I3).
        """
        h = Harness(sample_cfg(), FG_B)
        h.enter(1000); h.up(1020)
        out = h.enter(1100)                     # enter3 (hermes) still has a meaning
        self.assertIs(out.decision, Decision.HOLD)
        self.assertIs(h.machine.state, State.HOLDING)
        out = h.deadline(1450)
        self.assertIs(out.decision, Decision.DELIVER)
        self.assertEqual(out.resolved, "key:enter")
        self.assertEqual(out.count, 1)
        self.assertEqual(h.injected(), ["enter"])        # exactly one send
        self.assertEqual(h.app_enters("down"), [])       # no counted Enter reached the app

    def test_second_enter_without_higher_meaning_resolves_immediately(self):
        raw = sample_raw()
        raw["global"]["operations"]["enter3"] = "pass"
        cfg = config.parse_config(raw)
        h = Harness(cfg, FG_A)
        h.enter(1000); h.up(1020)
        out = h.enter(1100)
        self.assertIs(out.decision, Decision.TRIGGER)
        self.assertEqual(out.reason, "second_enter_no_higher")
        self.assertEqual(out.resolved, "key:ctrl+enter")
        self.assertTrue(out.swallow)
        self.assertEqual(h.injected(), ["ctrl+enter"])

    def test_hold_only_when_a_higher_count_has_a_meaning(self):
        raw = sample_raw()
        raw["global"]["operations"]["enter2"] = "pass"
        raw["global"]["operations"]["enter3"] = "pass"
        cfg = config.parse_config(raw)
        h = Harness(cfg, FG_A)
        out = h.enter(1000)
        self.assertIs(out.decision, Decision.TRIGGER)    # no hold at all
        self.assertIsNone(h.machine.deadline_ms)
        self.assertEqual(out.resolved, "key:shift+enter")

    def test_repeat_down_is_ignored_and_never_activates_twice(self):
        self.h.enter(1000)
        rep = self.h.enter(1016, os_repeat=True)
        self.assertIs(rep.decision, Decision.IGNORE)
        self.assertTrue(rep.swallow)
        self.assertEqual(self.h.machine.pending_count, 1)   # counted once
        self.h.deadline(1350)
        self.assertEqual(self.h.injected(), ["shift+enter"])

    def test_chatter_down_without_keyup_is_ignored(self):
        self.h.enter(1000)
        rep = self.h.enter(1005)          # no key-up in between
        self.assertIs(rep.decision, Decision.IGNORE)
        self.assertEqual(rep.reason, "enter_repeat")      # < chatter_debounce_ms (15)

    def test_missed_keyup_is_reported_but_still_one_press(self):
        """A down without an up is NEVER a second activation, only a log hint."""
        self.h.enter(1000)
        rep = self.h.enter(1016)          # >= chatter_debounce_ms since the down
        self.assertIs(rep.decision, Decision.IGNORE)
        self.assertEqual(rep.reason, "enter_repeat_stale")
        self.assertEqual(self.h.machine.pending_count, 1)
        self.h.deadline(1350)
        self.assertEqual(self.h.injected(), ["shift+enter"])   # still exactly once

    def test_os_repeat_flag_is_treated_as_the_same_press(self):
        h = Harness(sample_cfg(), FG_A)
        h.enter(1000)
        rep = h.enter(1100, os_repeat=True)
        self.assertIs(rep.decision, Decision.IGNORE)
        self.assertEqual(h.machine.pending_count, 1)


# ---------------------------------------------------------------------------
# C. fast mode
# ---------------------------------------------------------------------------
class TestFastMode(unittest.TestCase):
    def setUp(self):
        raw = sample_raw()
        raw["policy"]["hold_mode"] = "fast"
        self.cfg = config.parse_config(raw)

    def test_no_hold_and_immediate_delivery(self):
        h = Harness(self.cfg, FG_A)
        out = h.enter(1000)
        self.assertIs(out.decision, Decision.TRIGGER)
        self.assertIsNone(h.machine.deadline_ms)
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertEqual(h.injected(), ["shift+enter"])

    def test_three_rapid_enters_fire_both_send_and_hermes(self):
        """documented trade-off of fast mode (design §3.3): 3 連で両方発火しうる."""
        h = Harness(self.cfg, FG_A)
        for t in (1000, 1200, 1400):
            h.enter(t); h.up(t + 20)
        self.assertEqual(h.injected(), ["shift+enter", "ctrl+enter"])
        self.assertEqual(len(h.sinks), 1)

    def test_identity_short_circuit_passes_the_key_through(self):
        raw = sample_raw()
        raw["policy"]["hold_mode"] = "fast"                 # identity is a fast-mode path
        raw["profiles"][0]["keymap"]["newline"] = "enter"   # site-a newline == Enter
        cfg = config.parse_config(raw)
        h = Harness(cfg, FG_A)
        out = h.enter(1000)
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)
        self.assertEqual(out.reason, "identity_pass")
        self.assertEqual(h.injected(), [])
        self.assertEqual(h.app_enters("down"), ["enter"])


# ---------------------------------------------------------------------------
# D. undefined app: holds but never converts (I8, design §3.8 / §4.7 example 4)
# ---------------------------------------------------------------------------
class TestUndefinedApp(unittest.TestCase):
    def test_holds_then_passes_exactly_one_enter(self):
        h = Harness(sample_cfg(), FG_NOTEPAD)
        self.assertIsNone(h.profile)
        out = h.enter(1000)
        self.assertIs(out.decision, Decision.HOLD)       # layer-1 meaning decides, not profile
        out = h.deadline(1350)
        self.assertIs(out.decision, Decision.DELIVER)
        self.assertEqual(out.resolved, "pass")
        self.assertEqual(out.count, 1)
        self.assertEqual(h.injected(), ["enter"])        # one plain Enter, 350ms later
        self.assertEqual(h.app_enters("down"), [])

    def test_every_operation_resolves_to_pass(self):
        h = Harness(sample_cfg(), FG_NOTEPAD)
        # A counted Enter is still held (layer-1 meaning), but resolves to pass:
        # delay only, no behaviour change (§3.8 / §4.7 example 4).
        out = h.enter(1000)
        self.assertIs(out.decision, Decision.HOLD)
        out = h.deadline(1350)
        self.assertEqual(out.resolved, "pass")
        h.up(1020)
        for kw in ({"ctrl": True}, {"shift": True}, {"alt": True}):
            out = h.press(2000, **kw)
            self.assertFalse(out.swallow, kw)
            self.assertEqual(out.resolved, "pass", kw)
        out = h.key(3000, "down", VK_F5)
        self.assertFalse(out.swallow)
        h.key(3010, "up", VK_F5)
        self.assertEqual(h.injected(), ["enter"])       # only the held single Enter

    def test_fallback_without_meaning_source_does_not_hold(self):
        """Without `meaning_of` the machine can only see layer 2 -> no hold here.

        Documents why the production wiring MUST pass meaning_of (design §3.8).
        """
        h = Harness(sample_cfg(), FG_NOTEPAD, with_meanings=False)
        out = h.enter(1000)
        self.assertIs(out.decision, Decision.PASS)
        self.assertEqual(h.app_enters("down"), ["enter"])


# ---------------------------------------------------------------------------
# E. combos + the two-layer proof
# ---------------------------------------------------------------------------
class TestComboTriggers(unittest.TestCase):
    def setUp(self):
        self.cfg = sample_cfg()

    def test_same_trigger_sends_different_keys_per_profile(self):
        """acceptance: ctrl+enter -> send must emit ctrl+enter on A and enter on B."""
        h_a = Harness(self.cfg, FG_A)
        out = h_a.press(1000, ctrl=True)
        self.assertIs(out.decision, Decision.TRIGGER)
        self.assertTrue(out.swallow)
        self.assertEqual(h_a.injected(), ["ctrl+enter"])

        h_b = Harness(self.cfg, FG_B)
        out = h_b.press(1000, ctrl=True)
        self.assertEqual(h_b.injected(), ["enter"])

    def test_shift_enter_is_synthesized_not_passed(self):
        h = Harness(self.cfg, FG_A)
        out = h.press(1000, shift=True)
        self.assertIs(out.decision, Decision.TRIGGER)
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertEqual(h.injected(), ["shift+enter"])
        self.assertTrue(all(k != "down" for k, _ in h.app_input))

    def test_alt_enter_default_is_an_explicit_pass(self):
        h = Harness(self.cfg, FG_A)
        out = h.press(1000, alt=True)
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)
        self.assertEqual(out.resolved, "pass")

    def test_f_keys_are_unused_and_not_swallowed(self):
        h = Harness(self.cfg, FG_A)
        out = h.key(1000, "down", VK_F5)
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)
        self.assertEqual(out.resolved, "none")
        self.assertEqual(h.injected(), [])

    def test_undefined_combo_passes(self):
        h = Harness(self.cfg, FG_A)
        out = h.press(1000, ctrl=True, shift=True)      # ctrl+shift+enter not in config
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)

    def test_missing_required_trigger_behaves_as_pass(self):
        raw = sample_raw()
        del raw["global"]["operations"]["ctrl+enter"]
        cfg = config.parse_config(raw)
        self.assertEqual(len(cfg.warnings), 1)
        h = Harness(cfg, FG_A)
        out = h.press(1000, ctrl=True)
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)

    def test_combo_while_holding_flushes_first(self):
        h = Harness(self.cfg, FG_A)
        h.enter(1000)
        h.up(1020)                                  # a combo needs the Enter released
        out = h.key(1050, "down", VK_RETURN, ctrl=True)
        self.assertIs(out.decision, Decision.FLUSH)
        self.assertFalse(out.swallow)
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertEqual(h.machine.pending_count, 0)

    def test_arbitrary_combo_can_be_bound(self):
        raw = sample_raw()
        raw["global"]["operations"]["ctrl+k"] = "hermes"
        cfg = config.parse_config(raw)
        h = Harness(cfg, FG_A)
        out = h.key(1000, "down", 0x4B, ctrl=True)       # ctrl+k
        self.assertIs(out.decision, Decision.TRIGGER)
        self.assertEqual(out.resolved, "sink:hermes")
        self.assertEqual(h.sinks, ["sink:hermes"])


# ---------------------------------------------------------------------------
# F. I4 — injected events are never processed (delete-prohibited rule0)
# ---------------------------------------------------------------------------
class TestInjectedRule0(unittest.TestCase):
    def test_injected_event_is_never_processed_even_if_it_matches(self):
        h = Harness(sample_cfg(), FG_A)
        out = h.key(1000, "down", VK_RETURN, ctrl=True, injected=True)
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)
        self.assertEqual(out.reason, "injected")
        self.assertEqual(h.injected(), [])
        self.assertEqual(h.resolver_calls, 0)      # not even resolved

    def test_injected_enter_does_not_change_a_hold(self):
        h = Harness(sample_cfg(), FG_A)
        h.enter(1000)
        out = h.key(1050, "down", VK_RETURN, injected=True)
        self.assertEqual(out.reason, "injected")
        self.assertEqual(h.machine.pending_count, 1)     # no extra count
        self.assertEqual(h.machine.deadline_ms, 1250)    # deadline untouched

    def test_self_referencing_config_does_not_loop(self):
        """ctrl+enter -> send -> ctrl+enter (site-a). One press, one injection."""
        h = Harness(sample_cfg(), FG_A)
        out = h.press(1000, ctrl=True)                    # user presses ctrl+enter
        self.assertEqual(h.injected(), ["ctrl+enter"])    # one synthesized copy
        # the OS echoes our own injection back to the hook -> must be ignored
        echo_down = h.key(1001, "down", VK_RETURN, ctrl=True, injected=True)
        echo_up = h.key(1002, "up", VK_RETURN, ctrl=True, injected=True)
        self.assertEqual(echo_down.reason, "injected")
        self.assertEqual(echo_up.reason, "injected")
        self.assertEqual(h.injected(), ["ctrl+enter"])    # still exactly one
        self.assertEqual(h.resolver_calls, 1)             # only the user's press resolved

    def test_injected_panic_key_does_not_panic(self):
        h = Harness(sample_cfg(), FG_A)
        out = h.key(1000, "down", VK_F12, ctrl=True, alt=True, shift=True, injected=True)
        self.assertIs(out.decision, Decision.PASS)
        self.assertIsNot(out.state, State.PANIC)


class TestHeldModifiers(unittest.TestCase):
    """What the app hands to inject.plan_events (review round 1, item 4).

    ``held_modifiers`` must describe the PHYSICAL keyboard: injected modifier
    events are rule0 and never enter the tracking set, so our own synthesis can
    never make the app believe the user is holding Ctrl.
    """

    VK_LCONTROL = 0xA2
    VK_LSHIFT = 0xA0

    def test_no_modifiers_by_default(self):
        self.assertEqual(Harness(sample_cfg(), FG_A).machine.held_modifiers(),
                         frozenset())

    def test_a_physically_held_ctrl_is_reported(self):
        h = Harness(sample_cfg(), FG_A)
        h.key(1000, "down", self.VK_LCONTROL, ctrl=True)
        self.assertEqual(h.machine.held_modifiers(), frozenset({self.VK_LCONTROL}))

    def test_the_key_up_clears_it(self):
        h = Harness(sample_cfg(), FG_A)
        h.key(1000, "down", self.VK_LCONTROL, ctrl=True)
        h.key(1010, "up", self.VK_LCONTROL)
        self.assertEqual(h.machine.held_modifiers(), frozenset())

    def test_an_injected_modifier_never_counts_as_held(self):
        h = Harness(sample_cfg(), FG_A)
        h.key(1000, "down", self.VK_LCONTROL, ctrl=True, injected=True)
        self.assertEqual(h.machine.held_modifiers(), frozenset())

    def test_non_modifier_keys_are_not_reported(self):
        h = Harness(sample_cfg(), FG_A)
        h.key(1000, "down", VK_A)
        self.assertEqual(h.machine.held_modifiers(), frozenset())

    def test_both_shift_and_ctrl_can_be_held(self):
        h = Harness(sample_cfg(), FG_A)
        h.key(1000, "down", self.VK_LCONTROL, ctrl=True)
        h.key(1010, "down", self.VK_LSHIFT, ctrl=True, shift=True)
        self.assertEqual(h.machine.held_modifiers(),
                         frozenset({self.VK_LCONTROL, self.VK_LSHIFT}))


# ---------------------------------------------------------------------------
# G. I5 — IME
# ---------------------------------------------------------------------------
class TestIme(unittest.TestCase):
    def setUp(self):
        self.h = Harness(sample_cfg(), FG_A)

    def test_composing_enter_passes_through(self):
        out = self.h.enter(1000, ime=ImeState(composing=True))
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)
        self.assertIs(self.h.machine.state, State.IDLE)
        self.assertEqual(self.h.app_enters("down"), ["enter"])

    def test_composing_enter_delivers_a_pending_hold_first(self):
        self.h.enter(1000)
        out = self.h.enter(1100, ime=ImeState(composing=True))
        self.assertIs(out.decision, Decision.DELIVER)
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertFalse(out.swallow)                     # this Enter goes to the IME
        self.assertEqual(self.h.injected(), ["shift+enter"])
        self.assertIs(self.h.machine.state, State.IDLE)

    def test_a_commit_enter_is_not_a_burst_press(self):
        """v2-h: commit, then ONE Enter = newline, TWO = send (user's config)."""
        out = self.h.machine.on_ime_commit(
            KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)
        self.assertEqual(self.h.machine.pending_count, 0)

        first = self.h.press(1300)                       # 1回目 = 改行
        self.assertIs(first.decision, Decision.HOLD)
        self.assertEqual(self.h.machine.pending_count, 1)
        deadline = self.h.machine.on_deadline(1300 + 250)
        self.assertEqual(deadline.resolved, "key:shift+enter")

        self.h.press(2000)
        second = self.h.press(2150)                      # 2回目 = 送信
        self.assertIs(second.decision, Decision.HOLD)     # held once more so that
        self.assertEqual(self.h.machine.pending_count, 2)  # a 3rd press can cancel
        send = self.h.machine.on_deadline(2150 + 250)
        self.assertEqual(send.resolved, "key:ctrl+enter")

    def test_a_commit_after_a_held_enter_delivers_it_first(self):
        self.h.enter(1000)                               # held single Enter
        out = self.h.machine.on_ime_commit(
            KeyEvent(vk=VK_RETURN, kind="down", t_ms=1100))
        self.assertIs(out.decision, Decision.DELIVER)
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertFalse(out.swallow)

    def test_unknown_ime_state_fails_open(self):
        out = self.h.enter(1000, ime=ImeState(composing=None))
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)
        self.assertEqual(out.reason, "ime_unknown")
        self.assertIs(self.h.machine.state, State.IDLE)

    # -- card t_dd246e5a: three values x ime_unknown_policy ------------------
    def test_three_values_cross_two_policies(self):
        """composing / not_composing / unknown x pass / process, asserted whole.

        "pass" must never be reached by accident: the only PASS rows are
        composing (the IME owns the Enter) and unknown-with-policy-pass.
        """
        cases = (
            # (composing, policy, expected decision, expected swallow)
            (True, "pass", Decision.PASS, False),
            (True, "process", Decision.PASS, False),      # IME wins over policy
            (False, "pass", Decision.HOLD, True),
            (False, "process", Decision.HOLD, True),
            (None, "pass", Decision.PASS, False),
            (None, "process", Decision.HOLD, True),
        )
        for composing, policy_name, decision, swallow in cases:
            with self.subTest(composing=composing, policy=policy_name):
                h = Harness(sample_cfg(), FG_A, ime_unknown_policy=policy_name)
                out = h.enter(1000, ime=ImeState(composing=composing,
                                                 detail="detail-under-test"))
                self.assertIs(out.decision, decision)
                self.assertIs(out.swallow, swallow)
                if policy_name == "pass" and composing is None:
                    self.assertEqual(out.reason, "ime_unknown")

    def test_unknown_policy_process_holds_and_then_resolves_on_the_deadline(self):
        """`process` really runs the policy: hold, then deliver the newline."""
        h = Harness(sample_cfg(), FG_A, ime_unknown_policy="process")
        out = h.enter(1000, ime=ImeState(composing=None))
        self.assertIs(out.decision, Decision.HOLD)
        due = h.machine.deadline_ms
        self.assertIsNotNone(due)
        out = h.deadline(due)
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertEqual(h.injected(), ["shift+enter"])
        self.assertIs(h.machine.state, State.IDLE)

    def test_unknown_policy_pass_does_not_touch_the_burst_count(self):
        """A passed-through unknown Enter must not become the 1st Enter of a burst."""
        h = Harness(sample_cfg(), FG_A, ime_unknown_policy="pass")
        h.enter(1000, ime=ImeState(composing=None))
        self.assertEqual(h.machine.pending_count, 0)
        self.assertIs(h.machine.state, State.IDLE)
        # the NEXT Enter (readable, not composing) is the first one again
        out = h.enter(1100, ime=ImeState(composing=False))
        self.assertIs(out.decision, Decision.HOLD)

    def test_not_composing_is_not_treated_as_unknown(self):
        """A readable "no composition" answer must never fall into the unknown path."""
        h = Harness(sample_cfg(), FG_A, ime_unknown_policy="pass")
        out = h.enter(1000, ime=ImeState(composing=False))
        self.assertNotEqual(out.reason, "ime_unknown")
        self.assertIs(out.decision, Decision.HOLD)

    def test_reconfigure_switches_the_unknown_policy(self):
        h = Harness(sample_cfg(), FG_A, ime_unknown_policy="pass")
        self.assertIs(h.enter(1000, ime=ImeState(composing=None, detail="x")).decision,
                      Decision.PASS)
        h.machine.reconfigure(sample_cfg().policy, h.machine._resolver,
                              ime_unknown_policy="process")
        out = h.enter(2000, ime=ImeState(composing=None))
        self.assertIs(out.decision, Decision.HOLD)


# ---------------------------------------------------------------------------
# H. I6 — ordering: flush first, then the interrupting key
# ---------------------------------------------------------------------------
class TestOrdering(unittest.TestCase):
    def test_other_key_flushes_before_it_is_let_through(self):
        """あ → Enter → い must become あ改行い (never あい改行)."""
        h = Harness(sample_cfg(), FG_A)
        h.key(1000, "down", VK_A)                 # あ
        h.enter(1100)                             # Enter -> held
        out = h.key(1200, "down", 0x49)           # い (I key)
        self.assertIs(out.decision, Decision.FLUSH)
        self.assertFalse(out.swallow)             # the key itself passes
        # injection of the held Enter happened BEFORE the interrupting key
        self.assertEqual(h.app_input[-2:], [("inject", "shift+enter"), ("down", "i")])
        self.assertIs(h.machine.state, State.IDLE)

    def test_mouse_interrupt_flushes(self):
        h = Harness(sample_cfg(), FG_A)
        h.enter(1000)
        out = h.interrupt("mouse", 1100)
        self.assertIs(out.decision, Decision.FLUSH)
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertEqual(h.injected(), ["shift+enter"])

    def test_foreground_interrupt_uses_the_hold_start_snapshot(self):
        """design §3.5-4: never re-resolve into the new window."""
        h = Harness(sample_cfg(), FG_A)
        h.enter(1000)
        before = h.resolver_calls
        out = h.interrupt("foreground", 1100)
        self.assertIs(out.decision, Decision.FLUSH)
        self.assertEqual(out.resolved, "key:shift+enter")   # site-a's newline key
        self.assertEqual(h.resolver_calls, before)          # no re-resolution

    def test_interrupt_with_nothing_pending_is_a_noop(self):
        h = Harness(sample_cfg(), FG_A)
        out = h.interrupt("mouse", 1000)
        self.assertIs(out.decision, Decision.IGNORE)
        self.assertEqual(h.injected(), [])


# ---------------------------------------------------------------------------
# I. I7 — panic/stop: ordered flush, then never touch the keys again
# ---------------------------------------------------------------------------
class TestPanic(unittest.TestCase):
    """v2-g: the stop is a BURST of one key (default F1 x3), not a chord."""

    @staticmethod
    def _burst(h: Harness, vk: int = VK_F1, start: int = 1000, gap: int = 60,
               times: int = 3, **mods):
        """Press the panic key `times`, key-up included, return the last outcome."""
        out: Outcome | None = None
        for i in range(times):
            t = start + i * gap
            out = h.key(t, "down", vk, **mods)
            h.key(t + 10, "up", vk, **mods)
        return out

    def test_panic_flushes_pending_then_stops(self):
        h = Harness(sample_cfg(), FG_A)
        h.enter(1000)
        h.up(1020)
        first = h.key(1100, "down", VK_F1)
        self.assertIs(first.decision, Decision.PASS)        # one F1 is just F1
        self.assertEqual(first.reason, "panic_arm_1")
        out = self._burst(h, start=1160, times=2)           # -> total 3
        self.assertIs(out.decision, Decision.PANIC)
        self.assertIs(out.state, State.PANIC)
        self.assertEqual(out.resolved, "key:shift+enter")   # pending delivered first
        self.assertEqual(h.machine.pending_count, 0)
        self.assertEqual(h.injected(), ["shift+enter"])
        # the panic key itself is not eaten (hook is about to be removed anyway)
        self.assertFalse(out.swallow)

    def test_after_panic_nothing_is_ever_swallowed(self):
        h = Harness(sample_cfg(), FG_A)
        h.enter(1000)
        h.up(1020)
        self._burst(h, start=1100)
        injected_before = list(h.injected())
        out = h.enter(1400)
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)
        self.assertEqual(h.injected(), injected_before)

    def test_panic_without_pending(self):
        h = Harness(sample_cfg(), FG_A)
        out = self._burst(h, start=1000)
        self.assertIs(out.decision, Decision.PANIC)
        self.assertEqual(out.resolved, "")
        self.assertEqual(h.injected(), [])

    def test_a_lone_or_slow_f1_never_stops_the_tool(self):
        h = Harness(sample_cfg(), FG_A)
        self.assertIs(h.key(1000, "down", VK_F1).decision, Decision.PASS)
        # the next F1 arrives after the window: the count restarts, no panic
        out = h.key(1000 + 250 + 1, "down", VK_F1)
        self.assertIs(out.decision, Decision.PASS)
        self.assertEqual(out.reason, "panic_arm_1")
        self.assertIsNot(h.machine.state, State.PANIC)

    def test_panic_repeat_is_configurable(self):
        raw = sample_raw()
        raw["policy"]["panic_repeat"] = 2
        h = Harness(config.parse_config(raw), FG_A)
        out = self._burst(h, start=1000, times=2)
        self.assertIs(out.decision, Decision.PANIC)

    def test_custom_panic_key(self):
        raw = sample_raw()
        raw["policy"]["panic_key"] = "ctrl+alt+f9"
        cfg = config.parse_config(raw)
        h = Harness(cfg, FG_A)
        out = self._burst(h, vk=0x78, start=1000, ctrl=True, alt=True)
        self.assertIs(out.decision, Decision.PANIC)
        h2 = Harness(cfg, FG_A)
        out2 = self._burst(h2, vk=VK_F1, start=1000)                 # F1 is not the key
        self.assertIsNot(out2.decision, Decision.PANIC)

    def test_panic_while_idle_does_not_double_deliver(self):
        h = Harness(sample_cfg(), FG_A)
        h.enter(1000)
        h.deadline(1350)                      # delivered once by the timer
        out = h.panic(1300)
        self.assertIs(out.decision, Decision.PANIC)
        self.assertEqual(h.injected(), ["shift+enter"])      # not twice (I1)


# ---------------------------------------------------------------------------
# J. BUSY / COOLDOWN
# ---------------------------------------------------------------------------
class TestBusyAndCooldown(unittest.TestCase):
    def _busy_on_site_a(self):
        raw = sample_raw()
        h = Harness(config.parse_config(raw), FG_A, auto_sink_done=False)
        h.enter(1000); h.up(1020)
        h.enter(1100); h.up(1120)
        h.enter(1200); h.up(1220)
        out = h.deadline(1450)          # 3rd press is held one window, then fires
        self.assertEqual(out.resolved, "sink:hermes")
        self.assertIs(h.machine.state, State.BUSY)
        return h

    def test_count_one_is_resolved_immediately_while_busy(self):
        h = self._busy_on_site_a()
        out = h.enter(1600)
        self.assertIs(out.decision, Decision.TRIGGER)      # count-1: newline -> shift+enter
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertIs(h.machine.state, State.BUSY)         # still busy
        self.assertIsNone(h.machine.deadline_ms)           # never held

    def test_sink_trigger_while_busy_is_dropped(self):
        raw = sample_raw()
        raw["global"]["operations"]["enter"] = "hermes"    # count-1 resolves to a sink
        cfg = config.parse_config(raw)
        h = Harness(cfg, FG_A, auto_sink_done=False)
        h.enter(1000)                     # hold (enter2/3 still have meanings)
        out = h.deadline(1350)            # DELIVER -> sink -> BUSY
        self.assertEqual(out.resolved, "sink:hermes")
        self.assertIs(h.machine.state, State.BUSY)
        h.up(1260)
        out = h.enter(1400)
        self.assertIs(out.decision, Decision.IGNORE)
        self.assertEqual(out.reason, "busy_drop")
        self.assertTrue(out.swallow)
        self.assertEqual(len(h.sinks), 1)                  # not queued twice

    def test_sink_done_enters_cooldown_then_idle(self):
        h = self._busy_on_site_a()
        out = h.sink_done(5000)
        self.assertIs(out.state, State.COOLDOWN)
        self.assertEqual(h.machine.deadline_ms, 5000 + 500)
        h.up(5220)
        out = h.enter(5100)               # inside the cooldown window
        self.assertIs(out.decision, Decision.TRIGGER)
        self.assertEqual(out.reason, "cooldown_count1")
        self.assertEqual(h.machine.deadline_ms, 5500)   # the cooldown timer still owns it
        out = h.deadline(5500)
        self.assertIs(out.reason, "cooldown_expired")
        self.assertIs(h.machine.state, State.IDLE)
        self.assertIsNone(h.machine.deadline_ms)

    def test_sink_done_when_not_busy_is_a_noop(self):
        h = Harness(sample_cfg(), FG_A)
        out = h.sink_done(1000)
        self.assertIs(out.decision, Decision.IGNORE)
        self.assertIs(out.state, State.IDLE)


# ---------------------------------------------------------------------------
# K. reconfigure / disabled
# ---------------------------------------------------------------------------
class TestReconfigure(unittest.TestCase):
    def test_disable_flushes_the_pending_enter(self):
        h = Harness(sample_cfg(), FG_A)
        h.enter(1000)
        off = replace(h.cfg.policy, enabled=False)
        out = h.machine.reconfigure(off, h.resolver)
        self.assertIs(out.decision, Decision.DELIVER)
        self.assertEqual(out.resolved, "key:shift+enter")
        self.assertIs(h.machine.state, State.IDLE)
        self.assertEqual(h.machine.pending_count, 0)

    def test_disabled_policy_passes_everything(self):
        h = Harness(sample_cfg(), FG_A, policy_cfg=replace(sample_cfg().policy, enabled=False))
        out = h.press(1000)
        self.assertIs(out.decision, Decision.PASS)
        self.assertFalse(out.swallow)
        self.assertEqual(out.reason, "disabled")

    def test_switching_to_fast_stops_holding(self):
        cfg = sample_cfg()
        h = Harness(cfg, FG_A)
        self.assertIs(h.enter(1000).decision, Decision.HOLD)
        h.up(1020)
        fast = replace(cfg.policy, hold_mode="fast")
        h.machine.reconfigure(fast, h.resolver)
        out = h.enter(2000)
        self.assertIs(out.decision, Decision.TRIGGER)
        self.assertIsNone(h.machine.deadline_ms)

    def test_reconfigure_while_idle_is_a_noop(self):
        h = Harness(sample_cfg(), FG_A)
        out = h.machine.reconfigure(sample_cfg().policy, h.resolver)
        self.assertIs(out.decision, Decision.IGNORE)
        self.assertEqual(h.injected(), [])


# ---------------------------------------------------------------------------
# L. I1/I3 end-to-end: every swallowed Enter is delivered exactly once
# ---------------------------------------------------------------------------
class TestExactlyOnce(unittest.TestCase):
    def test_single_enter_is_delivered_once_across_many_ticks(self):
        h = Harness(sample_cfg(), FG_A)
        h.enter(1000)
        for t in range(1350, 1700, 50):
            h.deadline(t)
        self.assertEqual(h.injected(), ["shift+enter"])

    def test_three_sends_in_a_row_do_not_accumulate(self):
        h = Harness(sample_cfg(), FG_A)
        for base in (1000, 3000, 5000):
            h.enter(base)
            h.deadline(base + 350)
            h.up(base + 300)
        self.assertEqual(h.injected(), ["shift+enter"] * 3)

    def test_counted_enters_never_reach_the_app(self):
        """I3 — the acceptance test that protects against double-send."""
        h = Harness(sample_cfg(), FG_B)
        h.enter(1000); h.up(1020)      # press 1
        h.enter(1100); h.up(1120)      # press 2
        h.enter(1200); h.up(1220)      # press 3 -> held, fires on the deadline
        h.deadline(1450)               # -> hermes
        self.assertEqual(h.app_enters("down"), [])
        self.assertEqual(h.sinks, ["sink:hermes"])

    def test_flush_and_then_deadline_does_not_double_deliver(self):
        h = Harness(sample_cfg(), FG_A)
        h.enter(1000)
        h.interrupt("mouse", 1100)
        h.deadline(1350)                # stale timer after the flush
        self.assertEqual(h.injected(), ["shift+enter"])


if __name__ == "__main__":
    unittest.main()
