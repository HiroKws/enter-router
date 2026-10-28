"""Tests for the pure parts of platform/ + the --dry-run contract (design §3.9).

platform/ is Windows code, but three things in it are pure and must stay correct
without a Windows machine:

* ``inject.combo_to_events``  — the exact SendInput sequence for "key:<combo>"
* ``inject`` INPUT layout     — x64 must be 40 bytes (error 87 otherwise)
* ``hermes_cli`` argv + env   — ``_HERMES_GATEWAY`` removal is a hard requirement
                                (research R14) and the argv form must never let a
                                shell interpret the prompt

The dry-run checks assert the log contract QA compares against: the DRYRUN line
format, the summary counters, and the "swallow" behaviour per design §4.7.
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app import dryrun  # noqa: E402
from enter_router.core import config as cfg_mod  # noqa: E402
from enter_router.core import routing  # noqa: E402
from enter_router.core.config import HermesSinkConfig, parse_config_text  # noqa: E402
from enter_router.core.events import ForegroundInfo, ImeState  # noqa: E402
from enter_router.platform import hermes_cli, inject  # noqa: E402

SAMPLE = REPO / "config" / "sample.config.json"
ENTER, CTRL, SHIFT = 0x0D, 0x11, 0x10


def sample_cfg():
    return parse_config_text(SAMPLE.read_text(encoding="utf-8"))


class TestSendInputLayout(unittest.TestCase):
    def test_input_struct_is_40_bytes_on_x64(self):
        if sys.maxsize <= 2 ** 32:
            self.skipTest("32-bit interpreter")
        self.assertEqual(inject.input_size(), 40)
        self.assertTrue(inject.verify_layout())

    def test_key_input_sets_the_right_flags(self):
        down = inject._key_input(ENTER, False)
        up = inject._key_input(ENTER, True)
        self.assertEqual(down.union.ki.wVk, ENTER)
        self.assertEqual(down.union.ki.dwFlags, 0)
        self.assertEqual(up.union.ki.dwFlags, inject.KEYEVENTF_KEYUP)

    def test_unicode_input_uses_the_scan_code(self):
        item = inject._key_input(0, False, unicode_scan=0x3042)   # 'あ'
        self.assertTrue(item.union.ki.dwFlags & inject.KEYEVENTF_UNICODE)
        self.assertEqual(item.union.ki.wVk, 0)
        self.assertEqual(item.union.ki.wScan, 0x3042)


class TestImeState(unittest.TestCase):
    """Three values, never two (card t_dd246e5a)."""

    def test_state_property_names_all_three_values(self):
        self.assertEqual(ImeState(composing=True).state, "composing")
        self.assertEqual(ImeState(composing=False).state, "not_composing")
        self.assertEqual(ImeState(composing=None).state, "unknown")

    def test_unknown_is_not_the_same_value_as_not_composing(self):
        """The old code collapsed these; they must stay distinguishable."""
        self.assertNotEqual(ImeState(composing=None), ImeState(composing=False))
        self.assertIsNone(ImeState(composing=None).composing)
        self.assertEqual(ImeState(composing=None).state, "unknown")

    def test_defaults_keep_old_construction_and_equality_working(self):
        self.assertEqual(ImeState(composing=True), ImeState(composing=True))
        self.assertEqual(ImeState(composing=True).source, "")
        self.assertEqual(ImeState(composing=True).detail, "")


class TestComboEvents(unittest.TestCase):
    def test_plain_enter(self):
        self.assertEqual(inject.combo_to_events("enter"),
                         [(ENTER, False), (ENTER, True)])

    def test_ctrl_enter(self):
        self.assertEqual(inject.combo_to_events("ctrl+enter"),
                         [(CTRL, False), (ENTER, False), (ENTER, True), (CTRL, True)])

    def test_two_modifiers_release_in_reverse_order(self):
        events = inject.combo_to_events("ctrl+shift+enter")
        self.assertEqual(events, [(CTRL, False), (SHIFT, False),
                                  (ENTER, False), (ENTER, True),
                                  (SHIFT, True), (CTRL, True)])

    def test_f12_combo(self):
        events = inject.combo_to_events("ctrl+alt+shift+f12")
        self.assertEqual([vk for vk, _ in events],
                         [CTRL, 0x12, SHIFT, 0x7B, 0x7B, SHIFT, 0x12, CTRL])

    def test_bad_combo_is_rejected_not_half_sent(self):
        with self.assertRaises(ValueError):
            inject.combo_to_events("ctrl+shift")


class TestModifierNeutralisation(unittest.TestCase):
    """Review round 1, item 4: a physically held modifier must not leak into
    the synthesised key.

    Without this, ``ctrl+enter -> send -> enter`` on a profile arrives at the
    application as Ctrl+Enter (the user's Ctrl is still down), so the "same
    trigger, different key per app" behaviour silently does not happen.
    """

    VK_LCONTROL, VK_RCONTROL, VK_LSHIFT = 0xA2, 0xA3, 0xA0

    def test_held_ctrl_is_released_around_a_plain_enter(self):
        events = inject.plan_events("enter", [self.VK_LCONTROL])
        self.assertEqual(events, [(self.VK_LCONTROL, True),      # neutralise
                                  (ENTER, False), (ENTER, True),
                                  (self.VK_LCONTROL, False)])    # restore
        self.assertNotIn((self.VK_LCONTROL, False), events[:1])

    def test_the_key_down_is_not_wrapped_in_the_held_modifier(self):
        events = inject.plan_events("enter", [self.VK_LCONTROL])
        down_index = events.index((ENTER, False))
        # no modifier is down at the moment the Enter goes down
        self.assertEqual(events[down_index - 1], (self.VK_LCONTROL, True))

    def test_a_modifier_the_combo_uses_is_not_released(self):
        events = inject.plan_events("ctrl+enter", [self.VK_LCONTROL])
        self.assertEqual(events, [(CTRL, False), (ENTER, False), (ENTER, True),
                                  (CTRL, True)])
        self.assertNotIn((self.VK_LCONTROL, True), events)

    def test_left_and_right_ctrl_are_the_same_family(self):
        events = inject.plan_events("enter", [self.VK_RCONTROL])
        self.assertEqual(events[0], (self.VK_RCONTROL, True))

    def test_shift_is_released_for_a_ctrl_combo(self):
        events = inject.plan_events("ctrl+enter", [self.VK_LSHIFT])
        self.assertEqual(events, [(self.VK_LSHIFT, True),
                                  (CTRL, False), (ENTER, False), (ENTER, True),
                                  (CTRL, True), (self.VK_LSHIFT, False)])

    def test_non_modifier_vks_are_ignored(self):
        self.assertEqual(inject.plan_events("enter", [ENTER, 0x41]),
                         [(ENTER, False), (ENTER, True)])

    def test_repeats_share_one_release_and_one_restore(self):
        events = inject.plan_events("enter", [self.VK_LCONTROL], repeats=3)
        self.assertEqual(events.count((self.VK_LCONTROL, True)), 1)
        self.assertEqual(events.count((self.VK_LCONTROL, False)), 1)
        self.assertEqual(events.count((ENTER, False)), 3)

    def test_no_held_modifier_means_no_extra_events(self):
        self.assertEqual(inject.plan_events("enter"), inject.combo_to_events("enter"))

    def test_stray_modifiers_are_exactly_the_plan_restore_set(self):
        """The set send_combo re-checks after injecting is the set it restored.

        If these two ever disagree, a modifier would be released (or restored)
        without the thank-you read-back and could stick either way.
        """
        for combo, held in (("enter", [self.VK_RCONTROL]),
                            ("ctrl+enter", [self.VK_LSHIFT]),
                            ("ctrl+enter", [self.VK_LCONTROL]),
                            ("enter", [])):
            stray = inject.stray_modifiers(combo, held)
            plan = inject.plan_events(combo, held)
            self.assertEqual(plan[:len(stray)], [(vk, True) for vk in stray],
                             "leading releases must be the stray modifiers")
            self.assertEqual(plan[len(plan) - len(stray):],
                             [(vk, False) for vk in stray] if stray else [],
                             "trailing restores must be the stray modifiers")

    def test_a_combo_using_the_held_modifier_has_no_stray(self):
        self.assertEqual(inject.stray_modifiers("ctrl+enter", [self.VK_LCONTROL]), [])
        self.assertEqual(inject.stray_modifiers("shift+enter", [self.VK_LSHIFT]), [])
        # left ctrl satisfies a "ctrl" requirement (same family)
        self.assertEqual(inject.stray_modifiers("ctrl+enter", [self.VK_RCONTROL]), [])


def _script_of(argv: list[str]) -> str:
    """The shell script of a wsl.exe argv (``-d <distro>`` is optional)."""
    return argv[argv.index("-c") + 1]


class TestHermesCommands(unittest.TestCase):
    def test_primary_uses_stdin_not_argv(self):
        argv = hermes_cli.build_primary_argv(HermesSinkConfig())
        # no -d: the shipped default distro is empty = "WSL's default distro"
        self.assertEqual(argv[:4], ["wsl.exe", "--exec", "/bin/bash", "-c"])
        self.assertNotIn("-d", argv)
        self.assertIn(f"{hermes_cli.HERMES_BIN} chat --query-file -", _script_of(argv))
        self.assertEqual(len(argv), 5)          # no prompt on the command line

    def test_an_explicit_distro_adds_the_dash_d_flag(self):
        argv = hermes_cli.build_primary_argv(HermesSinkConfig(wsl_distro="Ubuntu-24.04"))
        self.assertEqual(argv[:6], ["wsl.exe", "-d", "Ubuntu-24.04", "--exec",
                                    "/bin/bash", "-c"])
        self.assertEqual(_script_of(argv), argv[6])

    def test_profile_is_a_global_option(self):
        argv = hermes_cli.build_primary_argv(HermesSinkConfig(profile="work"))
        self.assertIn(f"{hermes_cli.HERMES_BIN} -p work chat --query-file -",
                      _script_of(argv))

    def test_the_executable_is_always_an_absolute_path(self):
        """Card t_4674d88c: a bare ``hermes`` is what exit 127 came from.

        The path is written with $HOME so the file carries no machine-specific
        home directory; the shell forms expand it inside WSL.
        """
        cfg = HermesSinkConfig()
        self.assertEqual(hermes_cli.HERMES_BIN, "$HOME/.local/bin/hermes")
        self.assertEqual(hermes_cli.hermes_exe(cfg), "$HOME/.local/bin/hermes")
        self.assertEqual(hermes_cli.hermes_exe(HermesSinkConfig(profile="work")),
                         "$HOME/.local/bin/hermes -p work")
        # every shell form carries the fixed path, none of them a PATH lookup
        windows_scripts = [_script_of(hermes_cli.build_argv_fallback(cfg)),
                           _script_of(hermes_cli.build_stdin_argv(cfg)),
                           _script_of(hermes_cli.build_verify_argv(cfg))]
        for script in windows_scripts:
            with self.subTest(script=script):
                self.assertIn("$HOME/.local/bin/hermes", script)
                self.assertNotIn("command -v hermes", script)
        # the WSL-internal form needs a real path for the OS to exec
        self.assertEqual(hermes_cli.build_direct_argv(cfg, "text")[0],
                         hermes_cli.hermes_bin())
        self.assertEqual(hermes_cli.build_direct_stdin_argv(cfg)[0],
                         hermes_cli.hermes_bin())
        self.assertTrue(hermes_cli.hermes_bin().endswith("/.local/bin/hermes"))
        self.assertNotIn("~", hermes_cli.hermes_bin())

    def test_argv_fallback_passes_the_prompt_as_positional(self):
        argv = hermes_cli.build_argv_fallback(HermesSinkConfig())
        self.assertEqual(argv[-1], "erouter")       # $1 is supplied by the caller
        self.assertIn('-z "$1"', _script_of(argv))
        self.assertNotIn("--query-file", _script_of(argv))

    def test_argv_fallback_uses_exec_and_a_non_login_shell(self):
        """QA-B item 2 + card t_4674d88c: the transport flags are load-bearing.

        Measured on the target machine (no billing, ``/bin/echo`` in place of
        hermes):

        * ``--`` (legacy separator): the interop shell evaluates the tail, so
          ``$1`` is expanded there → bash sees ``$# == 0`` (``printf %s "$1"``
          prints nothing) and the prompt is silently dropped.
        * ``--`` also expands the prompt itself when it rides on the command
          line: a text containing `` `id` `` executed ``id``.
        * ``--exec`` hands the argv over verbatim (``$1`` byte-exact, ``$HOME``
          and backticks literal).
        * the shell no longer needs to be a login shell: with the absolute path
          a plain ``-c`` resolves the binary, so nothing depends on dotfiles.
        """
        argv = hermes_cli.build_argv_fallback(HermesSinkConfig())
        self.assertEqual(argv[:4], ["wsl.exe", "--exec", "/bin/bash", "-c"])
        self.assertNotIn("--", argv[:3])            # never the shell-evaluating form
        # the same flags as the forms that actually worked during that run
        self.assertEqual(argv[1], hermes_cli.WSL_EXEC)
        self.assertEqual(argv[1], hermes_cli.build_stdin_argv(HermesSinkConfig())[1])
        self.assertEqual(argv[1], hermes_cli.build_verify_argv(HermesSinkConfig())[1])
        self.assertEqual(argv[2], hermes_cli.build_stdin_argv(HermesSinkConfig())[2])

    def test_verify_command_checks_the_fixed_path(self):
        argv = hermes_cli.build_verify_argv(HermesSinkConfig())
        # quoted: the path now starts with $HOME, which must survive the shell
        self.assertIn(f'test -x "{hermes_cli.HERMES_BIN}"', argv[-1])
        self.assertNotIn("command -v", argv[-1])


class TestHermesEnv(unittest.TestCase):
    def test_gateway_variable_is_removed_by_default(self):
        env = hermes_cli.child_env(HermesSinkConfig(),
                                   base={"PATH": "/bin", "_HERMES_GATEWAY": "1"})
        self.assertNotIn("_HERMES_GATEWAY", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_every_hermes_prefixed_variable_is_removed(self):
        env = hermes_cli.child_env(HermesSinkConfig(), base={
            "_HERMES_GATEWAY": "1", "_HERMES_PROFILE": "x", "_HERMES_": "y",
            "HERMES_HOME": "/keep-me", "PATH": "/bin"})
        self.assertEqual(sorted(env), ["HERMES_HOME", "PATH"])
        # the exclusion is prefix-based on "_HERMES_"; a bare "HERMES_*" is not ours
        self.assertTrue(env["HERMES_HOME"])

    def test_empty_prefix_list_can_be_overridden(self):
        cfg = HermesSinkConfig(env_remove_prefixes=(), env_remove=())
        env = hermes_cli.child_env(cfg, base={"_HERMES_GATEWAY": "1"})
        self.assertIn("_HERMES_GATEWAY", env)


class TestHermesClassify(unittest.TestCase):
    def test_success_needs_text_not_just_exit_zero(self):
        self.assertEqual(hermes_cli.classify(0, "answer", ""), (True, None))
        self.assertEqual(hermes_cli.classify(0, "   \n", ""), (False, "empty_response"))

    def test_the_three_named_failures(self):
        self.assertEqual(hermes_cli.classify(None, "", "x", spawn_error="ENOENT"),
                         (False, "cli_spawn_failed"))
        self.assertEqual(hermes_cli.classify(3, "", "invalid_params"),
                         (False, "model_connection_failed"))
        self.assertEqual(hermes_cli.classify(None, "", "", timed_out=True),
                         (False, "timeout"))

    def test_a_binary_that_cannot_be_run_is_never_a_model_failure(self):
        """Card t_4674d88c item 2: the old default config's exit 127 was a lie.

        ``model_connection_failed`` asserted a provider problem; the real cause
        was ``exec: hermes: not found`` (our own argv). Boundaries: both shell
        "cannot execute" exit codes, and a stderr-only mention of a missing file.
        """
        cases = [
            (127, "", "exec: hermes: not found"),          # the reproduced failure
            (126, "", "Permission denied"),
            (127, "", ""),                                  # no stderr at all
            (2, "", "bash: ~/.local/bin/hermes: No such file or directory"),
        ]
        for exit_code, stdout, stderr in cases:
            with self.subTest(exit_code=exit_code, stderr=stderr):
                self.assertEqual(hermes_cli.classify(exit_code, stdout, stderr),
                                 (False, "cli_spawn_failed"))
        # a genuine provider/environment rejection stays a model failure
        self.assertEqual(hermes_cli.classify(1, "", "invalid_params"),
                         (False, "model_connection_failed"))

    def test_session_id_is_extracted(self):
        sid = "local-" + "0123456789abcdef" * 4
        self.assertEqual(hermes_cli.find_session_id("noise " + sid + " tail"), sid)
        self.assertIsNone(hermes_cli.find_session_id("nothing here"))


# ---------------------------------------------------------------------------
# --dry-run contract (design §3.9)
# ---------------------------------------------------------------------------
DRYRUN_RE = re.compile(
    r"^DRYRUN t=(?P<t>-?\d+) op=(?P<op>[^ ]+) meaning=(?P<meaning>[^ ]+) "
    r"profile=(?P<profile>[^ ]+) resolved=(?P<resolved>[^ ]+) "
    r"decision=(?P<decision>[A-Z]+) swallow=(?P<swallow>[01]) reason=(?P<reason>\S+)$")

SUMMARY_RE = re.compile(
    r"^DRYRUN-SUMMARY handled=(\d+) swallowed=(\d+) injected_ignored=(\d+) unresolved=(\d+)$")


class TestDryRun(unittest.TestCase):
    def setUp(self):
        self.lines: list[str] = []
        self.result = dryrun.run(sample_cfg(), emit=self.lines.append)

    def test_every_decision_line_matches_the_frozen_format(self):
        decisions = [ln for ln in self.lines if ln.startswith("DRYRUN ")]
        self.assertTrue(decisions)
        for line in decisions:
            with self.subTest(line=line):
                self.assertRegex(line, DRYRUN_RE)

    def test_summary_line_is_present_and_consistent(self):
        summary = [ln for ln in self.lines if ln.startswith("DRYRUN-SUMMARY")]
        self.assertEqual(len(summary), 1)
        handled, swallowed, injected_ignored, unresolved = map(int, SUMMARY_RE.match(summary[0]).groups())
        self.assertEqual(handled, len([ln for ln in self.lines if ln.startswith("DRYRUN ")]))
        self.assertGreater(swallowed, 0)
        self.assertGreater(injected_ignored, 0)     # rule0 demonstrably active
        self.assertEqual(unresolved, 0)             # the sample defines every trigger
        self.assertEqual((handled, swallowed, injected_ignored, unresolved),
                         (self.result["handled"], self.result["swallowed"],
                          self.result["injected_ignored"], self.result["unresolved"]))

    def test_the_four_worked_examples_appear_in_the_log(self):
        """design §4.7: site-a / site-b / excel-off / undefined app, exact keys."""
        site_a = [ln for ln in self.lines if "profile=site-a" in ln and "resolved=" in ln]
        self.assertTrue(any("resolved=key:shift+enter" in ln for ln in site_a))
        self.assertTrue(any("resolved=key:ctrl+enter" in ln for ln in site_a))
        self.assertTrue(any("resolved=sink:hermes" in ln for ln in site_a))
        site_b = [ln for ln in self.lines if "profile=site-b" in ln]
        self.assertTrue(any("resolved=key:enter" in ln for ln in site_b))
        undefined = self._block("undefined-app")
        self.assertTrue(undefined)
        self.assertTrue(any("resolved=pass" in ln for ln in undefined))
        self.assertFalse(any("profile=excel" in ln for ln in self.lines))   # disabled

    def _block(self, target: str) -> list[str]:
        """Lines belonging to one DRYRUN-TARGET section."""
        block: list[str] = []
        inside = False
        for line in self.lines:
            if line.startswith("DRYRUN-TARGET "):
                inside = f"profile={target} " in line
                continue
            if inside:
                block.append(line)
        return block

    def test_a_four_press_burst_is_cancelled_in_the_replay(self):
        """v2-f: the 4th Enter cancels the burst — nothing is resolved for it."""
        cancel = [ln for ln in self.lines if "decision=CANCEL" in ln]
        self.assertEqual(len(cancel), len([1 for ln in self.lines
                                           if ln.startswith("DRYRUN-TARGET")]))
        for line in cancel:
            self.assertIn("swallow=1", line)
            self.assertIn("resolved=-", line)      # nothing resolved, nothing sent
            self.assertIn("reason=burst_cancel_3", line)

    def test_the_third_press_fires_after_one_more_window(self):
        """v2-f: count 3 is DELIVERed on the deadline, not on the 3rd press."""
        held = [ln for ln in self.lines if "reason=hold_extend_third" in ln]
        self.assertTrue(held)
        for line in held:
            self.assertIn("decision=HOLD", line)
            self.assertIn("swallow=1", line)
        # the defined apps resolve to the sink; the undefined app to pass
        self.assertTrue(any("resolved=sink:hermes" in ln for ln in held))
        self.assertTrue(any("profile=- resolved=pass" in ln for ln in held))

    def test_panic_flushes_the_held_enter(self):
        panic = [ln for ln in self.lines if "decision=PANIC" in ln]
        self.assertTrue(panic)
        self.assertTrue(any("resolved=key:" in ln for ln in panic))

    def test_rule0_lines_are_marked_injected(self):
        injected = [ln for ln in self.lines if "reason=injected" in ln]
        self.assertGreaterEqual(len(injected), 2)       # one echo per target
        self.assertEqual(len(injected) % 2, 0)          # down + up pairs

    def test_changing_the_config_changes_the_log(self):
        """The acceptance criterion: behaviour is driven by the file alone."""
        raw = cfg_required()
        raw["profiles"][0]["keymap"]["newline"] = "enter"    # site-a newline -> Enter
        lines: list[str] = []
        dryrun.run(cfg_mod.parse_config(raw), emit=lines.append)
        site_a = [ln for ln in lines if "profile=site-a" in ln]
        self.assertTrue(any("resolved=key:enter" in ln for ln in site_a))
        self.assertFalse(any("resolved=key:shift+enter" in ln for ln in site_a))

    def test_hold_mode_fast_shows_no_hold_decisions(self):
        raw = cfg_required()
        raw["policy"]["hold_mode"] = "fast"
        lines: list[str] = []
        dryrun.run(cfg_mod.parse_config(raw), emit=lines.append)
        self.assertFalse(any("decision=HOLD" in ln for ln in lines))

    def test_the_catch_all_config_makes_every_app_defined(self):
        """config/chat-default.config.json: Enter=newline, x2=send, x3=hermes."""
        chat = REPO / "config" / "chat-default.config.json"
        cfg = cfg_mod.parse_config_text(chat.read_text(encoding="utf-8"))
        # the catch-all is an explicit opt-in and says so — never silent
        self.assertTrue(any("catch-all" in w for w in cfg.warnings), cfg.warnings)
        for fg in (ForegroundInfo(process_name="chrome.exe", window_title="Chat"),
                   ForegroundInfo(process_name="notepad.exe", window_title="Untitled")):
            self.assertEqual(routing.resolve_operation(cfg, fg, "enter"), "key:shift+enter")
            self.assertEqual(routing.resolve_operation(cfg, fg, "enter2"), "key:enter")
            self.assertEqual(routing.resolve_operation(cfg, fg, "enter3"), "sink:hermes")
        # with no readable foreground at all the safety rule still holds
        self.assertEqual(routing.resolve_operation(cfg, None, "enter2"), "pass")

    def test_once_report_lists_every_trigger_and_profile(self):
        lines = dryrun.once_report(sample_cfg(), "config/x.json")
        joined = "\n".join(lines)
        self.assertIn("hold_mode: deterministic", joined)
        for op in ("enter", "enter2", "enter3", "shift+enter", "ctrl+enter",
                   "alt+enter", "f5"):
            self.assertIn(f"trigger={op}", joined)
        self.assertIn("profile=site-a", joined)
        self.assertIn("profile=undefined-app", joined)


def cfg_required() -> dict:
    import json
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
