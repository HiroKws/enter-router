"""Tray UI (``ui/tray.py``): state visibility, one-click panic, safe on Linux.

The Win32 calls themselves are exercised on Windows by ``scripts/win_ui_check.py``
(a real icon + a real message loop); here we test the pure parts that decide what
the user sees, plus the fact that importing the module touches no Windows API.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from enter_router.ui import tray  # noqa: E402


class MenuTests(unittest.TestCase):
    def test_panic_is_the_first_item_and_reachable_in_one_click(self):
        items = tray.menu_items("running")
        self.assertEqual(items[0].command, "panic")
        self.assertTrue(items[0].enabled)
        self.assertIn("パニック停止", items[0].label)

    def test_menu_contains_every_required_entry(self):
        commands = [item.command for item in tray.menu_items("running")]
        self.assertEqual(commands, ["panic", "settings", "toggle", "logs", "quit"])

    def test_panic_item_is_greyed_once_panicked_but_still_visible(self):
        items = {item.command: item for item in tray.menu_items("panic")}
        self.assertIn("panic", items)                  # never silently disappears
        self.assertFalse(items["panic"].enabled)

    def test_toggle_label_follows_the_state(self):
        self.assertEqual({i.command: i for i in tray.menu_items("running")}["toggle"].label,
                         "一時停止")
        self.assertEqual({i.command: i for i in tray.menu_items("busy")}["toggle"].label,
                         "一時停止")
        self.assertEqual({i.command: i for i in tray.menu_items("paused")}["toggle"].label,
                         "再開")
        self.assertEqual({i.command: i for i in tray.menu_items("panic")}["toggle"].label,
                         "再開")
        self.assertFalse({i.command: i for i in tray.menu_items("panic")}["toggle"].enabled)

    def test_menu_ids_map_back_to_commands(self):
        for index, item in enumerate(tray.menu_items("running"), start=1):
            self.assertEqual(tray.command_for_id(index), item.command)
        self.assertEqual(tray.command_for_id(0), "")
        self.assertEqual(tray.command_for_id(99), "")


class StatePresentationTests(unittest.TestCase):
    def test_every_state_has_a_japanese_text_and_a_distinct_colour(self):
        colours = set()
        for state in tray.STATE_ORDER:
            self.assertTrue(tray.state_text(state))
            colours.add(tray.state_color(state))
        self.assertEqual(len(colours), len(tray.STATE_ORDER))

    def test_state_texts_cover_the_controller_states(self):
        for state in ("running", "paused", "panic", "busy"):
            self.assertIn(state, tray.STATE_TEXTS)
        self.assertEqual(tray.state_text("running"), "有効")
        self.assertEqual(tray.state_text("paused"), "一時停止")
        self.assertEqual(tray.state_text("panic"), "パニック停止中")

    def test_tooltip_shows_state_warnings_and_last_error(self):
        text = tray.status_tooltip("paused", config_path=r"C:\cfg\config.json",
                                   warnings=2, last_error="sink: timeout")
        self.assertIn("一時停止", text)
        self.assertIn("config.json", text)
        self.assertIn("警告 2 件", text)
        self.assertIn("timeout", text)
        self.assertLessEqual(len(text), 127)

    def test_tooltip_is_truncated_to_the_shell_limit(self):
        text = tray.status_tooltip("running", config_path="C:\\" + "x" * 400)
        self.assertLessEqual(len(text), 127)


class PlatformSafetyTests(unittest.TestCase):
    def test_import_does_not_touch_win32(self):
        # Importing must be side-effect free; the API object is built lazily.
        self.assertIsNone(tray._api)
        self.assertIsNone(tray._instance)

    def test_tray_unavailable_off_windows_and_start_fails_softly(self):
        if sys.platform == "win32":
            self.skipTest("Windows: exercised for real by scripts/win_ui_check.py")
        self.assertFalse(tray.tray_available())
        icon = tray.TrayIcon(on_command=lambda _c: None)
        self.assertFalse(icon.start(timeout=0.1))
        self.assertIn("Windows", icon.error)
        self.assertEqual(icon.hwnd, 0)

    def test_post_open_settings_is_a_no_op_off_windows(self):
        if sys.platform == "win32":
            self.skipTest("Windows: covered by scripts/win_ui_check.py")
        self.assertFalse(tray.post_open_settings())
        self.assertIsNone(tray._api)          # still never touched Win32


if __name__ == "__main__":
    unittest.main()
