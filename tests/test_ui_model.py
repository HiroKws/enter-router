"""Settings model (``ui/model.py``): schema fidelity, error naming, preview.

The window's whole brain is tested here on Linux, without a display:
* all 18 triggers are listed and editable, a missing one shows as 未設定;
* edits go back out as the *same* schema ``core/config.py`` parses (no UI-only
  fields), and validation failures name the offending item in Japanese while
  core's own reason is kept verbatim;
* the preview strings resolve through ``core.routing`` (two layers), including
  the "undefined app passes through" rule;
* the test-send state machine has an in-flight guard and never reports an
  unconfirmed send as success.
"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from enter_router.core.config import (MEANINGS, default_config_dict,  # noqa: E402
                                      parse_config)
from enter_router.ui import model as m  # noqa: E402


def fresh_model() -> m.SettingsModel:
    return m.SettingsModel(default_config_dict())


class TriggerListTests(unittest.TestCase):
    def test_all_18_triggers_are_listed_in_screen_order(self):
        rows = fresh_model().operation_rows()
        self.assertEqual(len(rows), 18)
        self.assertEqual(tuple(row[0] for row in rows), m.TRIGGERS)
        for op, label, meaning in rows:
            self.assertTrue(label, f"{op} has no Japanese label")
            self.assertIn(meaning, MEANINGS, f"{op} lost its default meaning")

    def test_default_meaning_rows_match_the_design_map(self):
        rows = dict((row[0], row[2]) for row in fresh_model().operation_rows())
        self.assertEqual(rows["enter"], "newline")
        self.assertEqual(rows["enter2"], "send")
        self.assertEqual(rows["enter3"], "hermes")
        self.assertEqual(rows["shift+enter"], "newline")
        self.assertEqual(rows["ctrl+enter"], "send")
        self.assertEqual(rows["alt+enter"], "pass")
        self.assertEqual(rows["f1"], "none")

    def test_missing_required_trigger_is_shown_as_unset_not_hidden(self):
        data = default_config_dict()
        del data["global"]["operations"]["shift+enter"]
        model = m.SettingsModel(data)
        rows = dict((row[0], row[2]) for row in model.operation_rows())
        self.assertEqual(len(rows), 18)                 # still all rows on screen
        self.assertEqual(rows["shift+enter"], "")       # visible as 未設定
        result = model.validate()
        self.assertTrue(result.ok)                      # core only warns
        self.assertTrue(any("shift+enter" in w for w in result.warnings))
        self.assertIn("shift+enter", model.unset_required_triggers())

    def test_extra_custom_combo_is_not_dropped(self):
        data = default_config_dict()
        data["global"]["operations"]["ctrl+alt+h"] = "hermes"
        model = m.SettingsModel(data)
        rows = model.operation_rows()
        self.assertEqual(len(rows), 19)
        self.assertEqual(rows[-1][0], "ctrl+alt+h")
        self.assertEqual(model.to_dict()["global"]["operations"]["ctrl+alt+h"], "hermes")

    def test_editing_a_meaning_round_trips_into_the_document(self):
        model = fresh_model()
        model.set_operation("f5", "clipboard")
        saved = model.to_dict()["global"]["operations"]
        self.assertEqual(saved["f5"], "clipboard")
        self.assertTrue(model.is_dirty())
        # and the document is still exactly what core validates
        self.assertEqual(parse_config(model.to_dict()).operations["f5"], "clipboard")

    def test_unset_operation_removes_the_key(self):
        model = fresh_model()
        model.set_operation("f12", "")
        self.assertNotIn("f12", model.to_dict()["global"]["operations"])


class PolicyTests(unittest.TestCase):
    def test_number_fields_are_written_as_int(self):
        model = fresh_model()
        model.set_policy("hold_window_ms", 300)
        self.assertEqual(model.to_dict()["policy"]["hold_window_ms"], 300)

    def test_out_of_range_value_is_reported_with_japanese_label(self):
        model = fresh_model()
        model.set_policy("hold_window_ms", 9000)
        result = model.validate()
        self.assertFalse(result.ok)
        self.assertEqual(result.issues[0].label, "保留窓（hold_window_ms）")
        self.assertIn("120..600", result.issues[0].detail)   # core's reason kept
        self.assertIn("保留窓", result.error_text())

    def test_hold_mode_outside_vocabulary_is_reported(self):
        model = fresh_model()
        model.set_policy("hold_mode", "wobbly")
        result = model.validate()
        self.assertFalse(result.ok)
        self.assertEqual(result.issues[0].label, "保留モード（hold_mode）")


class ProfileTests(unittest.TestCase):
    def test_add_duplicate_delete(self):
        model = fresh_model()
        start = len(model.profiles())
        index = model.add_profile()
        self.assertEqual(index, start)
        self.assertFalse(model.profile(index)["enabled"])     # opt-in, not opt-out
        dup = model.duplicate_profile(index)
        self.assertFalse(model.profile(dup)["enabled"])       # copies start disabled
        ids = model.profile_ids()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(model.delete_profile(dup))
        self.assertEqual(len(model.profiles()), start + 1)
        while len(model.profiles()) > 1:
            self.assertTrue(model.delete_profile(0))
        self.assertFalse(model.delete_profile(0))             # last one stays

    def test_duplicate_ids_are_rejected_by_core(self):
        model = fresh_model()
        model.set_profile(1, "id", model.profile_ids()[0])
        result = model.validate()
        self.assertFalse(result.ok)
        self.assertIn("unique", result.message)

    def test_match_fields_drop_empty_title_and_url(self):
        model = fresh_model()
        model.set_profile(2, "title", "Book1")
        self.assertEqual(model.profile(2)["match"]["title"], "Book1")
        model.set_profile(2, "title", "")
        self.assertNotIn("title", model.profile(2)["match"])

    def test_keymap_is_normalized_by_core(self):
        model = fresh_model()
        ok, normalized = m.normalize_combo("Shift + Ctrl + F10")
        self.assertTrue(ok)
        self.assertEqual(normalized, "ctrl+shift+f10")
        model.set_keymap(0, "send", "Shift + Ctrl + F10")
        self.assertEqual(model.keymap_value(0, "send"), "Shift + Ctrl + F10")
        self.assertEqual(parse_config(model.to_dict()).profiles[0].keymap["send"],
                         "ctrl+shift+f10")

    def test_win_combo_in_keymap_is_refused_by_core(self):
        model = fresh_model()
        model.set_keymap(0, "send", "win+enter")
        result = model.validate()
        self.assertFalse(result.ok)
        self.assertIn("win", result.message)
        self.assertEqual(result.issues[0].label, "プロファイル")

    def test_keymap_only_accepts_send_and_newline(self):
        with self.assertRaises(ValueError):
            fresh_model().set_keymap(0, "hermes", "ctrl+enter")

    def test_empty_keymap_combo_produces_a_visible_warning(self):
        model = fresh_model()
        model.set_keymap(0, "newline", "")
        result = model.validate()
        self.assertTrue(result.ok)
        self.assertTrue(any("newline" in w and "pass" in w for w in result.warnings))

    def test_cancel_restores_the_saved_document(self):
        model = fresh_model()
        before = model.snapshot()
        model.set_operation("enter", "send")
        self.assertTrue(model.is_dirty())
        model.reload(before)
        self.assertFalse(model.is_dirty())
        self.assertEqual(model.operation_rows()[0][2], "newline")


class SinkTests(unittest.TestCase):
    def test_sink_values_round_trip(self):
        model = fresh_model()
        model.set_sink("ai_chat", "send_after_insert", True)
        model.set_sink("hermes", "timeout_sec", 45)
        model.set_sink("hermes", "profile", None)
        data = model.to_dict()
        self.assertTrue(data["sinks"]["ai_chat"]["send_after_insert"])
        self.assertEqual(data["sinks"]["hermes"]["timeout_sec"], 45)
        self.assertIsNone(data["sinks"]["hermes"]["profile"])
        self.assertEqual(parse_config(data).sinks["hermes"].timeout_sec, 45)

    def test_bad_hermes_timeout_names_the_sink(self):
        model = fresh_model()
        model.set_sink("hermes", "timeout_sec", 0)
        result = model.validate()
        self.assertFalse(result.ok)
        self.assertEqual(result.issues[0].label, "送り先: Hermes")
        self.assertIn("1..300", result.issues[0].detail)


class DocumentShapeTests(unittest.TestCase):
    def test_no_ui_only_fields_leak_into_the_saved_dict(self):
        model = fresh_model()
        model.set_operation("enter", "send")
        model.add_profile()
        data = model.to_dict()
        self.assertEqual(set(data), {"schema_version", "global", "policy", "registration",
                                     "safety", "capture", "sinks", "profiles", "logging"})
        self.assertNotIn("hotkeys", data)
        self.assertFalse(any("enter_action" in p for p in data["profiles"]))
        self.assertEqual(parse_config(data).schema_version, 1)


class ExternalConfigChangeTests(unittest.TestCase):
    """v2-g: the registration hotkey writes the file while the window is open."""

    def test_no_new_mtime_or_same_mtime_is_ignored(self):
        from enter_router.ui import model
        self.assertEqual(model.external_config_change(False, 0.0, 0.0), model.EXTERNAL_IGNORE)
        self.assertEqual(model.external_config_change(False, 5.0, 5.0), model.EXTERNAL_IGNORE)

    def test_a_clean_window_reloads(self):
        from enter_router.ui import model
        self.assertEqual(model.external_config_change(False, 5.0, 6.0), model.EXTERNAL_RELOAD)

    def test_a_dirty_window_only_warns(self):
        from enter_router.ui import model
        self.assertEqual(model.external_config_change(True, 5.0, 6.0), model.EXTERNAL_WARN)


class PreviewTests(unittest.TestCase):
    def test_preview_resolves_the_second_layer_per_profile(self):
        lines = "\n".join(fresh_model().preview_lines())
        self.assertIn("site-a", lines)
        self.assertIn("Enter（1 回） → 改行（このアプリでは shift+enter を送る）", lines)
        self.assertIn("Enter ×2（連打） → 送信（このアプリでは ctrl+enter を送る）", lines)
        self.assertIn("Enter ×3（連打） → Hermes へ送る", lines)

    def test_preview_says_pass_for_undefined_apps(self):
        lines = "\n".join(fresh_model().preview_lines())
        self.assertIn("一致しないアプリ → すべて素通し", lines)

    def test_preview_marks_disabled_profiles(self):
        lines = "\n".join(fresh_model().preview_lines())
        self.assertIn("excel: 無効（このアプリでは常に素通し）", lines)

    def test_preview_reflects_ai_chat_send_after_insert(self):
        model = fresh_model()
        model.set_operation("enter2", "aichat")
        model.set_sink("ai_chat", "send_after_insert", True)
        lines = "\n".join(model.preview_lines())
        self.assertIn("AI チャットへ挿入して送信まで行う", lines)

    def test_preview_reports_an_invalid_document(self):
        model = fresh_model()
        model.set_policy("hold_window_ms", 5)
        lines = "\n".join(model.preview_lines())
        self.assertIn("プレビューできません", lines)

    def test_preview_of_missing_keymap_says_pass(self):
        model = fresh_model()
        model.set_keymap(0, "send", "")
        lines = "\n".join(model.preview_lines())
        self.assertIn("keymap に send が無いため", lines)


class TestSendTrackerTests(unittest.TestCase):
    def test_double_click_is_ignored_while_sending(self):
        tracker = m.TestSendTracker()
        self.assertEqual(tracker.label(), "未実行")
        self.assertTrue(tracker.begin("clipboard", "hello"))
        self.assertFalse(tracker.begin("clipboard", "hello"))   # in-flight guard
        self.assertTrue(tracker.busy)
        self.assertIn("送信中", tracker.label())
        tracker.finish("clipboard: ok")
        self.assertFalse(tracker.busy)

    def test_success_and_failure_labels(self):
        tracker = m.TestSendTracker()
        tracker.begin("hermes", "x")
        tracker.finish("hermes: ok (3.8s)")
        self.assertIn("成功", tracker.label())
        tracker.begin("hermes", "x")
        tracker.finish("hermes: failed (timeout) — クリップボードへ退避しました")
        self.assertIn("失敗", tracker.label())

    def test_exception_clears_busy_and_is_reported(self):
        tracker = m.TestSendTracker()
        tracker.begin("ai_chat", "x")
        tracker.fail(RuntimeError("boom"))
        self.assertFalse(tracker.busy)
        self.assertIn("失敗", tracker.label())
        self.assertIn("boom", tracker.label())

    def test_unknown_outcome_never_reads_as_success(self):
        tracker = m.TestSendTracker()
        tracker.begin("hermes", "x")
        tracker.mark_unknown("worker vanished")
        self.assertFalse(tracker.busy)
        label = tracker.label()
        self.assertIn("結果不明", label)
        self.assertNotIn("成功", label)

    def test_empty_result_is_not_success(self):
        self.assertFalse(m._looks_successful(""))
        self.assertFalse(m._looks_successful("hermes: error"))


class LabelTests(unittest.TestCase):
    def test_path_labels(self):
        self.assertEqual(m.label_for_path("policy.hold_window_ms"), "保留窓（hold_window_ms）")
        self.assertEqual(m.label_for_path("global.operations[f3]"), "操作マップ（global.operations）")
        self.assertEqual(m.label_for_path("sinks.hermes"), "送り先: Hermes")
        self.assertEqual(m.label_for_path("profiles[0](site-a).keymap[send]"), "プロファイル")
        self.assertEqual(m.label_for_path("something.unknown"), "")

    def test_issue_headline_contains_label_and_core_reason(self):
        issues = m.issues_from_message("sinks.hermes: timeout_sec must be 1..300")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].path, "sinks.hermes")
        self.assertIn("送り先: Hermes", issues[0].headline())
        self.assertIn("1..300", issues[0].headline())


if __name__ == "__main__":
    unittest.main()
