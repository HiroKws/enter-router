"""Smoke + contract tests for the pure core (run on Linux/WSL: python3 -m unittest discover -s tests -v).

Written unittest-style so they run under BOTH pytest and
`python3 -m unittest discover -s tests` even where pytest is absent.

Coverage maps onto design.md v2:
  §4.2  the sample file is byte-identical to the design block / defaults match it
  §4.3  validation + warning rules (required triggers, keymap gaps, vocabularies)
  §4.6  the six mandatory tests (1 sample, 2 missing trigger, 3 vocab/normalize,
        4 two-layer walkthrough, 5 capture/select_sink/schema, 6 _HERMES_GATEWAY)
  §4.7  the four worked examples, asserted with exact expected strings
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.core import capture, config, routing  # noqa: E402
from enter_router.core.config import ConfigError, parse_config, parse_config_text  # noqa: E402
from enter_router.core.events import ForegroundInfo  # noqa: E402
from enter_router.app import dryrun  # noqa: E402

SAMPLE = REPO / "config" / "sample.config.json"
NOTEPAD_QA = REPO / "config" / "notepad-qa.config.json"
DESIGN = REPO / "docs" / "design.md"

# design.md §4.2 layer 1 — all 18 triggers explicit (no implicit defaults).
SAMPLE_OPERATIONS = {
    "enter": "newline",
    "enter2": "send",
    "enter3": "hermes",
    "shift+enter": "newline",
    "ctrl+enter": "send",
    "alt+enter": "pass",
    **{f"f{i}": "none" for i in range(1, 13)},
}

FG_SITE_A = ForegroundInfo(process_name="firefox.exe", window_title="Site A",
                           url="https://site-a.example.com/chat")
FG_SITE_B = ForegroundInfo(process_name="chrome.exe", window_title="Site B",
                           url="https://site-b.example.com/chat")
FG_EXCEL = ForegroundInfo(process_name="excel.exe", window_title="Book1 - Excel")
FG_NOTEPAD = ForegroundInfo(process_name="notepad.exe", window_title="Untitled")


def sample_text() -> str:
    return SAMPLE.read_text(encoding="utf-8")


def sample_raw() -> dict:
    return json.loads(sample_text())


def resolve_map(cfg: config.Config, profile: config.Profile | None, ops) -> dict:
    """Both layers, explicit: exactly what routing.resolve_operation does."""
    return {op: routing.resolve_meaning(cfg, profile, routing.lookup_meaning(cfg, op))
            for op in ops}


def matched(cfg: config.Config, fg: ForegroundInfo) -> config.Profile:
    """The one profile for this window — asserted, so a missing match is a clear failure."""
    profile = routing.match_profile(cfg, fg)
    if profile is None:
        raise AssertionError(f"no profile matched {fg.process_name!r}")
    return profile


def hermes_sink(cfg: config.Config) -> config.HermesSinkConfig:
    sink = cfg.sinks["hermes"]
    if not isinstance(sink, config.HermesSinkConfig):
        raise AssertionError("sinks.hermes must be a HermesSinkConfig")
    return sink


# ---------------------------------------------------------------------------
# 必須テスト 1 — the sample parses, validates, warns nothing, matches §4.2
# ---------------------------------------------------------------------------
class TestSampleConfig(unittest.TestCase):
    def test_sample_parses_validates_and_warns_nothing(self):
        cfg = parse_config_text(sample_text())
        self.assertEqual(cfg.schema_version, config.SCHEMA_VERSION)
        self.assertEqual(cfg.warnings, ())
        # layer 1: all 18 triggers, exactly the design values
        self.assertEqual(cfg.operations, SAMPLE_OPERATIONS)
        self.assertEqual(len(cfg.operations), 18)
        # global.ime_unknown_policy (card t_dd246e5a): default is the safe "pass"
        self.assertEqual(cfg.ime_unknown_policy, "pass")
        # policy numbers (design §3.6; measured 250 = v2-f, docs/measurements.md)
        self.assertEqual(cfg.policy.hold_mode, "deterministic")
        self.assertEqual(cfg.policy.hold_window_ms, 250)
        self.assertEqual(cfg.policy.triple_enter_window_ms, 250)
        self.assertEqual(cfg.policy.chatter_debounce_ms, 15)
        self.assertEqual(cfg.policy.trigger_cooldown_ms, 500)
        self.assertEqual(cfg.policy.panic_key, "f1")          # v2-g burst stop
        self.assertEqual(cfg.policy.panic_repeat, 3)
        self.assertEqual(cfg.registration.process_key, "alt+f5")
        self.assertEqual(cfg.registration.process_title_key, "alt+f6")
        # layer 2: profiles + keymaps, no catch-all (design §4.4)
        self.assertEqual([p.id for p in cfg.profiles], ["site-a", "site-b", "excel"])
        self.assertEqual(cfg.profiles[0].keymap, {"send": "ctrl+enter", "newline": "shift+enter"})
        self.assertEqual(cfg.profiles[1].keymap, {"send": "enter", "newline": "shift+enter"})
        self.assertEqual(cfg.profiles[2].keymap, {"send": "enter", "newline": "alt+enter"})
        self.assertTrue(cfg.profiles[0].enabled)
        self.assertTrue(cfg.profiles[1].enabled)
        self.assertFalse(cfg.profiles[2].enabled)  # excel stays off unless opted in

    def test_sample_has_no_catch_all_profile(self):
        cfg = parse_config_text(sample_text())
        catch_all = [p.id for p in cfg.profiles
                     if p.match.process == "*" and p.match.title is None and p.match.url is None]
        self.assertEqual(catch_all, [])

    def test_sample_has_no_obsolete_v1_keys(self):
        raw = sample_raw()
        self.assertIn("global", raw)
        self.assertNotIn("double_enter_window_ms", raw["policy"])
        self.assertIn("hold_window_ms", raw["policy"])
        for prof in raw["profiles"]:
            self.assertNotIn("enter_action", prof)
            self.assertNotIn("hotkeys", prof)
            self.assertIn("keymap", prof)

    def test_sample_file_is_byte_identical_to_design_4_2_block(self):
        """design §4.2: 'このブロックと byte 単位で一致させる' — asserted, not assumed."""
        lines = DESIGN.read_text(encoding="utf-8").split("\n")
        heading = next(i for i, l in enumerate(lines) if l.startswith("### 4.2"))
        fence = next(i for i in range(heading, len(lines)) if lines[i].strip() == "```json")
        end = next(i for i in range(fence + 1, len(lines)) if lines[i].strip() == "```")
        block = "\n".join(lines[fence + 1:end]) + "\n"
        self.assertEqual(SAMPLE.read_bytes(), block.encode("utf-8"))

    def test_default_config_dict_equals_sample(self):
        self.assertEqual(config.default_config_dict(), sample_raw())
        cfg = parse_config(config.default_config_dict())
        self.assertEqual(cfg.warnings, ())
        self.assertEqual(cfg.operations, config.DEFAULT_CONFIG.operations)
        self.assertEqual(cfg.profiles, config.DEFAULT_CONFIG.profiles)

    def test_first_run_config_carries_no_sample_profiles(self):
        """"修正のたびにサンプルのプロファイルが復活する" を防ぐ。

        The example rules are documentation (config/sample.config.json, design
        §4.2); a live config must start with none of them, or the user deletes
        site-a/site-b/excel on day one and every regeneration brings them back.
        """
        doc = config.first_run_config_dict()
        self.assertEqual(doc["profiles"], [])
        cfg = parse_config(doc)
        self.assertEqual(cfg.profiles, ())
        self.assertEqual(cfg.warnings, ())
        self.assertEqual(cfg.operations, config.DEFAULT_CONFIG.operations)

    def test_missing_or_empty_profiles_means_pass_through(self):
        """No profile list = every app passes through; never fall back to samples."""
        self.assertEqual(parse_config({"schema_version": 1}).profiles, ())
        self.assertEqual(parse_config({"schema_version": 1, "profiles": []}).profiles, ())

    def test_default_operations_constant_is_the_design_map(self):
        self.assertEqual(config.DEFAULT_OPERATIONS, SAMPLE_OPERATIONS)
        self.assertEqual(config.DEFAULT_CONFIG.operations, SAMPLE_OPERATIONS)

    def test_rejects_wrong_schema_version(self):
        with self.assertRaises(ConfigError):
            parse_config({"schema_version": 99})

    def test_rejects_unknown_top_level_key(self):
        with self.assertRaises(ConfigError):
            parse_config({"schema_version": 1, "polcy": {}})
        with self.assertRaises(ConfigError):
            parse_config({"schema_version": 1, "hotkeys": []})

    # -- global.ime_unknown_policy (card t_dd246e5a) -----------------------
    def test_ime_unknown_policy_defaults_to_pass(self):
        """No global section at all -> "pass", and only the usual warning."""
        cfg = parse_config({"schema_version": 1})
        self.assertEqual(cfg.ime_unknown_policy, "pass")
        self.assertEqual(config.DEFAULT_CONFIG.ime_unknown_policy, "pass")

    def test_ime_unknown_policy_accepts_process(self):
        cfg = parse_config({"schema_version": 1,
                            "global": {"ime_unknown_policy": "process"}})
        self.assertEqual(cfg.ime_unknown_policy, "process")
        # and it round-trips through the default-config renderer
        raw = config.default_config_dict()
        raw["global"]["ime_unknown_policy"] = "process"
        self.assertEqual(parse_config(raw).ime_unknown_policy, "process")

    def test_ime_unknown_policy_rejects_a_typo(self):
        """A wrong value must be loud: silently falling back to "pass" is the bug."""
        for bad in ("pas", "PASS", "", None, True, 1):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError):
                    parse_config({"schema_version": 1,
                                  "global": {"ime_unknown_policy": bad}})

    def test_ime_unknown_policy_in_the_policy_section_is_warned_about(self):
        """It lives in `global`; a misplaced key must say so, not vanish."""
        cfg = parse_config({"schema_version": 1,
                            "policy": {"ime_unknown_policy": "process"}})
        self.assertEqual(cfg.ime_unknown_policy, "pass")      # default used
        self.assertTrue(any("global.ime_unknown_policy" in w for w in cfg.warnings),
                        cfg.warnings)


# ---------------------------------------------------------------------------
# 必須テスト 2 — a missing required trigger is a WARNING, behaviour is pass
# ---------------------------------------------------------------------------
class TestRequiredTriggers(unittest.TestCase):
    def test_missing_shift_enter_warns_and_passes(self):
        raw = sample_raw()
        del raw["global"]["operations"]["shift+enter"]
        cfg = parse_config(raw)                       # must NOT raise
        self.assertTrue(any("shift+enter" in w for w in cfg.warnings), cfg.warnings)
        # missing trigger: no meaning, and the whole pipeline resolves to pass
        self.assertIsNone(routing.lookup_meaning(cfg, "shift+enter"))
        self.assertEqual(routing.resolve_operation(cfg, FG_SITE_A, "shift+enter"), "pass")

    def test_every_required_trigger_is_reported(self):
        for op in ("enter", "enter2", "enter3", "shift+enter", "ctrl+enter"):
            with self.subTest(op=op):
                raw = sample_raw()
                del raw["global"]["operations"][op]
                cfg = parse_config(raw)
                self.assertEqual(len(cfg.warnings), 1, cfg.warnings)
                self.assertIn(op, cfg.warnings[0])
                self.assertEqual(routing.resolve_operation(cfg, FG_SITE_A, op), "pass")

    def test_optional_triggers_are_not_required(self):
        raw = sample_raw()
        for op in ("alt+enter",) + tuple(f"f{i}" for i in range(1, 13)):
            del raw["global"]["operations"][op]
        cfg = parse_config(raw)
        self.assertEqual(cfg.warnings, ())
        self.assertEqual(len(cfg.operations), 5)

    def test_missing_global_section_applies_defaults_with_exactly_one_warning(self):
        raw = sample_raw()
        del raw["global"]
        cfg = parse_config(raw)
        self.assertEqual(cfg.operations, config.DEFAULT_OPERATIONS)
        self.assertEqual(len(cfg.warnings), 1, cfg.warnings)
        self.assertIn("global", cfg.warnings[0])

    def test_global_without_operations_map_applies_defaults_with_one_warning(self):
        raw = sample_raw()
        raw["global"] = {}
        cfg = parse_config(raw)
        self.assertEqual(cfg.operations, config.DEFAULT_OPERATIONS)
        self.assertEqual(len(cfg.warnings), 1, cfg.warnings)

    def test_explicitly_empty_operations_is_not_a_silent_default(self):
        raw = sample_raw()
        raw["global"] = {"operations": {}}
        cfg = parse_config(raw)
        self.assertEqual(cfg.operations, {})          # not filled in behind the user's back
        self.assertEqual(len(cfg.warnings), 1)
        for op in ("enter", "enter2", "enter3", "shift+enter", "ctrl+enter"):
            self.assertIn(op, cfg.warnings[0])


# ---------------------------------------------------------------------------
# 必須テスト 3 — vocabularies, keymap rules, combo normalization
# ---------------------------------------------------------------------------
class TestSemanticsValidation(unittest.TestCase):
    def test_rejects_meaning_outside_vocabulary(self):
        for bad in ("transform:ctrl+enter", "sink:hermes", "SEND", "", "key:enter"):
            with self.subTest(bad=bad):
                raw = sample_raw()
                raw["global"]["operations"]["ctrl+enter"] = bad
                with self.assertRaises(ConfigError):
                    parse_config(raw)

    def test_rejects_non_string_meaning(self):
        for bad in (1, None, ["send"], {"send": True}):
            with self.subTest(bad=bad):
                raw = sample_raw()
                raw["global"]["operations"]["enter2"] = bad
                with self.assertRaises(ConfigError):
                    parse_config(raw)

    def test_rejects_unknown_global_key(self):
        raw = sample_raw()
        raw["global"]["hotkeys"] = []
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_rejects_win_combo_in_keymap(self):
        for combo in ("win+enter", "ctrl+win+enter", "Win + Enter"):
            with self.subTest(combo=combo):
                raw = sample_raw()
                raw["profiles"][0]["keymap"]["send"] = combo
                with self.assertRaises(ConfigError):
                    parse_config(raw)

    def test_rejects_non_key_meaning_as_keymap_key(self):
        for meaning in ("hermes", "aichat", "clipboard", "pass", "none"):
            with self.subTest(meaning=meaning):
                raw = sample_raw()
                raw["profiles"][0]["keymap"][meaning] = "enter"
                with self.assertRaises(ConfigError):
                    parse_config(raw)

    def test_rejects_bad_keymap_value(self):
        for value in ("", "   ", 5, None, "ctrl+shift"):
            with self.subTest(value=value):
                raw = sample_raw()
                raw["profiles"][0]["keymap"]["send"] = value
                with self.assertRaises(ConfigError):
                    parse_config(raw)

    def test_rejects_non_object_keymap(self):
        raw = sample_raw()
        raw["profiles"][0]["keymap"] = ["send", "enter"]
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_normalizes_keymap_combo(self):
        raw = sample_raw()
        raw["profiles"][0]["keymap"]["send"] = "Shift + Ctrl + F10"
        cfg = parse_config(raw)
        self.assertEqual(cfg.profiles[0].keymap["send"], "ctrl+shift+f10")

    def test_normalizes_layer1_combo_key(self):
        raw = sample_raw()
        ops = raw["global"]["operations"]
        ops["Shift+Enter"] = ops.pop("shift+enter")      # same trigger, messy spelling
        ops["Enter2"] = ops.pop("enter2")                # count keys are recognized too
        cfg = parse_config(raw)
        self.assertEqual(cfg.warnings, ())               # still counted as present
        self.assertEqual(cfg.operations["shift+enter"], "newline")
        self.assertEqual(cfg.operations["enter2"], "send")

    def test_keymap_missing_meaning_warns_and_falls_back_to_pass(self):
        raw = sample_raw()
        raw["profiles"][0]["keymap"] = {"send": "ctrl+enter"}     # newline missing
        cfg = parse_config(raw)
        self.assertEqual(len(cfg.warnings), 1, cfg.warnings)
        self.assertIn("site-a", cfg.warnings[0])
        self.assertIn("newline", cfg.warnings[0])
        profile = matched(cfg, FG_SITE_A)
        self.assertEqual(routing.resolve_meaning(cfg, profile, "newline"), "pass")
        self.assertEqual(routing.resolve_meaning(cfg, profile, "send"), "key:ctrl+enter")

    def test_empty_keymap_warns_for_both_meanings(self):
        raw = sample_raw()
        raw["profiles"][0]["keymap"] = {}
        cfg = parse_config(raw)
        self.assertEqual(len(cfg.warnings), 1, cfg.warnings)
        self.assertIn("send", cfg.warnings[0])
        self.assertIn("newline", cfg.warnings[0])
        profile = matched(cfg, FG_SITE_A)
        self.assertEqual(routing.resolve_meaning(cfg, profile, "send"), "pass")

    def test_catch_all_profile_is_warned_not_rejected(self):
        raw = sample_raw()
        raw["profiles"].append({"id": "everything", "enabled": True,
                                "match": {"process": "*"},
                                "keymap": {"send": "enter", "newline": "shift+enter"}})
        cfg = parse_config(raw)
        self.assertEqual(len(cfg.warnings), 1, cfg.warnings)
        self.assertIn("everything", cfg.warnings[0])
        self.assertEqual(matched(cfg, FG_NOTEPAD).id, "everything")


class TestPolicyValidation(unittest.TestCase):
    def test_hold_window_range_boundaries(self):
        for ok in (120, 250, 600):
            with self.subTest(ok=ok):
                raw = sample_raw()
                raw["policy"]["hold_window_ms"] = ok
                self.assertEqual(parse_config(raw).policy.hold_window_ms, ok)
        for bad in (119, 601, 0, -1, "250", None, True):
            with self.subTest(bad=bad):
                raw = sample_raw()
                raw["policy"]["hold_window_ms"] = bad
                with self.assertRaises(ConfigError):
                    parse_config(raw)

    def test_triple_window_range_boundaries(self):
        for bad in (119, 601):
            with self.subTest(bad=bad):
                raw = sample_raw()
                raw["policy"]["triple_enter_window_ms"] = bad
                with self.assertRaises(ConfigError):
                    parse_config(raw)

    def test_hold_mode_values(self):
        for ok in ("deterministic", "fast"):
            with self.subTest(ok=ok):
                raw = sample_raw()
                raw["policy"]["hold_mode"] = ok
                self.assertEqual(parse_config(raw).policy.hold_mode, ok)
        for bad in ("turbo", "", "Deterministic", None, 1):
            with self.subTest(bad=bad):
                raw = sample_raw()
                raw["policy"]["hold_mode"] = bad
                with self.assertRaises(ConfigError):
                    parse_config(raw)

    def test_double_enter_window_ms_is_replaced_not_renamed(self):
        self.assertFalse(hasattr(config.PolicyConfig(), "double_enter_window_ms"))
        raw = sample_raw()
        raw["policy"]["double_enter_window_ms"] = 300
        cfg = parse_config(raw)           # v1 key: ignored, but never silently
        self.assertEqual(cfg.policy.hold_window_ms, 250)
        self.assertTrue(any("double_enter_window_ms" in w for w in cfg.warnings), cfg.warnings)


# ---------------------------------------------------------------------------
# 必須テスト 4 — the two-layer walkthrough (design §4.7), exact expected values
# ---------------------------------------------------------------------------
class TestTwoLayerResolution(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_config_text(sample_text())

    def test_example_1_site_a(self):
        profile = matched(self.cfg, FG_SITE_A)
        self.assertEqual(profile.id, "site-a")
        self.assertEqual(
            resolve_map(self.cfg, profile,
                        ("enter", "enter2", "enter3", "ctrl+enter", "shift+enter", "alt+enter", "f5")),
            {
                "enter": "key:shift+enter",       # Enter x1 -> newline -> Shift+Enter
                "enter2": "key:ctrl+enter",       # Enter x2 -> send    -> Ctrl+Enter
                "enter3": "sink:hermes",          # Enter x3 -> hermes  -> Hermes sink
                "ctrl+enter": "key:ctrl+enter",   # habit: Ctrl+Enter = send
                "shift+enter": "key:shift+enter",
                "alt+enter": "pass",              # explicit pass keeps pass
                "f5": "none",                     # unused trigger stays hands-off
            })

    def test_example_2_site_b(self):
        profile = matched(self.cfg, FG_SITE_B)
        self.assertEqual(profile.id, "site-b")
        self.assertEqual(
            resolve_map(self.cfg, profile, ("enter", "enter2", "ctrl+enter", "shift+enter")),
            {
                "enter": "key:shift+enter",
                "enter2": "key:enter",            # this site sends with a plain Enter
                "ctrl+enter": "key:enter",        # the user's habit mapped onto the site key
                "shift+enter": "key:shift+enter",
            })

    def test_example_3_excel_when_explicitly_enabled(self):
        raw = sample_raw()
        raw["profiles"][2]["enabled"] = True
        cfg = parse_config(raw)
        profile = matched(cfg, FG_EXCEL)
        self.assertEqual(profile.id, "excel")
        self.assertEqual(
            resolve_map(cfg, profile, ("enter", "enter2", "ctrl+enter")),
            {
                "enter": "key:alt+enter",         # in-cell line break
                "enter2": "key:enter",            # commit / bulk entry
                "ctrl+enter": "key:enter",
            })

    def test_example_4_undefined_app_passes_every_operation(self):
        self.assertIsNone(routing.match_profile(self.cfg, FG_NOTEPAD))   # no catch-all profile
        for op in SAMPLE_OPERATIONS:
            with self.subTest(op=op):
                self.assertEqual(routing.resolve_operation(self.cfg, FG_NOTEPAD, op), "pass")

    def test_sink_meanings_never_fire_for_an_undefined_app(self):
        """I8: creating a profile is the opt-in for sinks (design §4.3)."""
        for meaning in ("hermes", "aichat", "clipboard"):
            with self.subTest(meaning=meaning):
                self.assertEqual(routing.resolve_meaning(self.cfg, None, meaning), "pass")

    def test_resolve_operation_matches_the_two_step_form(self):
        for fg in (FG_SITE_A, FG_SITE_B, FG_EXCEL, FG_NOTEPAD):
            profile = routing.match_profile(self.cfg, fg)
            for op in SAMPLE_OPERATIONS:
                with self.subTest(fg=fg.process_name, op=op):
                    self.assertEqual(
                        routing.resolve_operation(self.cfg, fg, op),
                        routing.resolve_meaning(self.cfg, profile, routing.lookup_meaning(self.cfg, op)))

    def test_lookup_meaning_returns_none_for_unknown_operation(self):
        self.assertIsNone(routing.lookup_meaning(self.cfg, "ctrl+shift+enter"))
        self.assertEqual(routing.resolve_operation(self.cfg, FG_SITE_A, "ctrl+shift+enter"), "pass")

    def test_aichat_and_clipboard_meanings_map_to_sink_names(self):
        profile = matched(self.cfg, FG_SITE_A)
        self.assertEqual(routing.resolve_meaning(self.cfg, profile, "aichat"), "sink:ai_chat")
        self.assertEqual(routing.resolve_meaning(self.cfg, profile, "clipboard"), "sink:clipboard")


# ---------------------------------------------------------------------------
# 必須テスト 5 — profile matching (two-pass), select_sink, capture chain
# ---------------------------------------------------------------------------
class TestProfileMatching(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_config_text(sample_text())

    def test_url_profile_matches_when_url_is_readable(self):
        self.assertEqual(matched(self.cfg, FG_SITE_A).id, "site-a")
        self.assertEqual(matched(self.cfg, FG_SITE_B).id, "site-b")

    def test_unreadable_url_never_matches_url_profile(self):
        fg = ForegroundInfo(process_name="firefox.exe", window_title="Chat", url=None)
        self.assertIsNone(routing.match_profile(self.cfg, fg))

    def test_undefined_process_matches_no_profile(self):
        self.assertIsNone(routing.match_profile(self.cfg, FG_NOTEPAD))

    def test_disabled_profile_never_matches(self):
        self.assertIsNone(routing.match_profile(self.cfg, FG_EXCEL))   # excel enabled=false

    def test_specific_profile_wins_over_catch_all_listed_first(self):
        raw = sample_raw()
        raw["profiles"].insert(0, {"id": "everything", "enabled": True,
                                   "match": {"process": "*"},
                                   "keymap": {"send": "enter", "newline": "shift+enter"}})
        cfg = parse_config(raw)
        self.assertEqual(matched(cfg, FG_SITE_A).id, "site-a")
        self.assertEqual(matched(cfg, FG_NOTEPAD).id, "everything")

    def test_select_sink_respects_enabled_flag(self):
        dummy = object()
        self.assertIs(routing.select_sink(self.cfg, {"clipboard": dummy}, "sink:clipboard"), dummy)
        self.assertIsNone(routing.select_sink(self.cfg, {}, "sink:missing"))
        self.assertIsNone(routing.select_sink(self.cfg, {"clipboard": dummy}, "pass"))
        raw = sample_raw()
        raw["sinks"]["clipboard"]["enabled"] = False
        cfg = parse_config(raw)
        self.assertIsNone(routing.select_sink(cfg, {"clipboard": dummy}, "sink:clipboard"))


# ---------------------------------------------------------------------------
# 必須テスト 6 — _HERMES_GATEWAY removal defaults (research R14, design §5.3)
# ---------------------------------------------------------------------------
class TestHermesSinkDefaults(unittest.TestCase):
    def setUp(self):
        self.hermes = hermes_sink(parse_config_text(sample_text()))

    def test_gateway_variable_is_removed_by_default(self):
        self.assertEqual(self.hermes.env_remove, ("_HERMES_GATEWAY",))

    def test_removals_are_covered_by_the_prefixes(self):
        self.assertEqual(self.hermes.env_remove_prefixes, ("_HERMES_",))
        for var in self.hermes.env_remove:
            self.assertTrue(any(var.startswith(p) for p in self.hermes.env_remove_prefixes), var)

    def test_hermes_sink_defaults(self):
        self.assertEqual(self.hermes.timeout_sec, 30)
        self.assertIsNone(self.hermes.profile)
        # t_92c498ed (card §3): the measured invocation is `hermes -z "<text>"`, so
        # the stdin form is no longer the default — it is forced only when this
        # flag is true, or used automatically for prompts > ARGV_FALLBACK_MAX_CHARS.
        self.assertFalse(self.hermes.query_file_stdin)
        self.assertTrue(self.hermes.enabled)

    def test_missing_sink_section_falls_back_to_defaults(self):
        raw = sample_raw()
        del raw["sinks"]["hermes"]
        cfg = parse_config(raw)
        self.assertEqual(hermes_sink(cfg).env_remove, ("_HERMES_GATEWAY",))


class TestCapturePolicy(unittest.TestCase):
    def test_first_success_in_chain_order(self):
        cap = config.CaptureConfig(chain=("uia_focused", "clipboard_select_copy"))
        out = capture.select_outcome(cap, {"uia_focused": None, "clipboard_select_copy": "hello"})
        self.assertEqual((out.text, out.step), ("hello", "clipboard_select_copy"))

    def test_empty_counts_as_failure(self):
        cap = config.CaptureConfig(chain=("uia_focused",))
        out = capture.select_outcome(cap, {"uia_focused": ""})
        self.assertIsNone(out.text)

    def test_validate_chain(self):
        self.assertTrue(capture.validate_chain(("uia_focused", "clipboard_select_copy")))
        self.assertFalse(capture.validate_chain(("nope",)))


# ---------------------------------------------------------------------------
# config/notepad-qa.config.json — the copy-paste config for a real Notepad trial
# ---------------------------------------------------------------------------
class TestNotepadQaConfig(unittest.TestCase):
    """The shipped QA config must actually make Notepad a defined app.

    A sample file alone cannot show anything in Notepad: an app without a
    profile resolves every operation to ``pass`` (I8) — that is exactly the
    "Enter just makes a newline and nothing happens" report this file answers.
    """

    def setUp(self):
        self.cfg = parse_config_text(NOTEPAD_QA.read_text(encoding="utf-8"))

    def test_parses_without_warnings_and_keeps_the_default_layer1_map(self):
        self.assertEqual(self.cfg.schema_version, config.SCHEMA_VERSION)
        self.assertEqual(self.cfg.warnings, ())          # nothing silently missing
        self.assertEqual(self.cfg.operations, SAMPLE_OPERATIONS)
        self.assertEqual(self.cfg.policy.hold_mode, "deterministic")
        self.assertEqual(self.cfg.policy.hold_window_ms, 250)

    def test_defines_exactly_one_profile_for_notepad(self):
        self.assertEqual([p.id for p in self.cfg.profiles], ["notepad"])
        profile = self.cfg.profiles[0]
        self.assertTrue(profile.enabled)
        # Windows reports the executable as "Notepad.exe"; the match stays
        # case-insensitive (routing._glob lowercases both sides).
        self.assertEqual(profile.match.process, "Notepad.exe")
        self.assertIsNone(profile.match.url)             # process match, never URL
        self.assertIsNone(profile.match.title)
        self.assertEqual(profile.keymap, {"send": "ctrl+enter", "newline": "enter"})

    def test_enter2_resolves_to_send_ctrl_enter(self):
        self.assertEqual(matched(self.cfg, FG_NOTEPAD).id, "notepad")
        self.assertEqual(routing.lookup_meaning(self.cfg, "enter2"), "send")
        self.assertEqual(routing.resolve_operation(self.cfg, FG_NOTEPAD, "enter2"),
                         "key:ctrl+enter")

    def test_enter_1_and_3_resolve_to_newline_and_hermes(self):
        self.assertEqual(routing.resolve_operation(self.cfg, FG_NOTEPAD, "enter"),
                         "key:enter")
        self.assertEqual(routing.resolve_operation(self.cfg, FG_NOTEPAD, "enter3"),
                         "sink:hermes")

    def test_process_glob_is_case_insensitive(self):
        for name in ("notepad.exe", "NOTEPAD.EXE", "NotePad.Exe"):
            with self.subTest(name=name):
                fg = ForegroundInfo(process_name=name, window_title="Untitled")
                self.assertEqual(matched(self.cfg, fg).id, "notepad")

    def test_windows_actual_name_matches_both_cases(self):
        """The shipped pattern is Windows' real name `Notepad.exe` and matches
        the lowercase spelling too (routing._glob lowercases both sides, so the
        behaviour is the same on Linux and on Windows)."""
        self.assertEqual(self.cfg.profiles[0].match.process, "Notepad.exe")
        for name in ("Notepad.exe", "notepad.exe"):
            with self.subTest(name=name):
                fg = ForegroundInfo(process_name=name, window_title="Untitled - Notepad")
                self.assertIsNotNone(routing.match_profile(self.cfg, fg))
                self.assertEqual(matched(self.cfg, fg).id, "notepad")

    def test_any_other_app_still_passes_through(self):
        """Defining notepad must not make every app defined (no catch-all)."""
        other = ForegroundInfo(process_name="mspaint.exe", window_title="Untitled")
        self.assertIsNone(routing.match_profile(self.cfg, other))
        self.assertEqual(routing.resolve_operation(self.cfg, other, "enter2"), "pass")

    def test_once_report_shows_notepad_and_a_really_undefined_app(self):
        """--once: notepad rows resolve for real, the undefined row still passes."""
        lines = dryrun.once_report(self.cfg, "config/notepad-qa.config.json")
        notepad = [ln for ln in lines if "profile=notepad" in ln]
        self.assertEqual(len(notepad), len(dryrun.RESOLUTION_OPS))
        fields = {ln.split()[2].split("=", 1)[1]: dict(
            part.split("=", 1) for part in ln.split() if "=" in part) for ln in notepad}
        self.assertEqual(fields["enter2"]["meaning"], "send")
        self.assertEqual(fields["enter2"]["resolved"], "key:ctrl+enter")
        undefined = [ln for ln in lines if "profile=undefined-app" in ln]
        self.assertTrue(undefined)
        self.assertTrue(all("resolved=pass" in ln for ln in undefined), undefined)


if __name__ == "__main__":
    unittest.main()
