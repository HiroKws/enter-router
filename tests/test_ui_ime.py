"""IME Enter safety (``ui/ime_guard.py``) — the card's acceptance criterion.

Two independent layers, both tested here without a display:

1. **Static**: the settings/runtime source contains no ``<Return>`` binding at
   all. tkinter has no implicit default button, so "no binding" is the fix; this
   test makes sure nobody adds one later.
2. **Runtime**: the guard walks a widget tree and refuses Enter bindings. Run
   here against a fake tree, and against the real Tk window on Windows by
   ``scripts/win_ui_check.py`` (the settings window also calls it on itself when
   it is built, so a violation cannot ship silently).
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from enter_router.ui import ime_guard  # noqa: E402

UI_DIR = Path(__file__).resolve().parents[1] / "src" / "enter_router" / "ui"


class FakeWidget:
    """Minimal duck-typed widget: only what the guard touches."""

    def __init__(self, name, sequences=(), children=()):
        self._name = name
        self._sequences = tuple(sequences)
        self._children = list(children)

    def winfo_name(self):
        return self._name

    def winfo_children(self):
        return list(self._children)

    def bind(self, *_args):
        return self._sequences


class FakeRoot(FakeWidget):
    def __init__(self, name=".", sequences=(), children=(), all_sequences=()):
        super().__init__(name, sequences, children)
        self._all = tuple(all_sequences)

    def bind_all(self, *_args):
        return self._all


class StaticSourceTests(unittest.TestCase):
    """No Enter binding may exist in the UI source (except the guard's denylist)."""

    def test_settings_and_runtime_never_bind_enter(self):
        offenders = []
        for path in (UI_DIR / "settings.py", UI_DIR / "runtime.py", UI_DIR / "model.py"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"""bind\w*\(\s*["'](<[^"']*[Rr]eturn[^"']*>)""", text):
                offenders.append(f"{path.name}: {match.group(1)}")
            for match in re.finditer(r"""["']<KP_Enter>["']""", text):
                offenders.append(f"{path.name}: {match.group(0)}")
        self.assertEqual(offenders, [],
                         "IME 安全: Enter 束縛は UI に存在してはなりません（明示クリックのみ）")

    def test_the_settings_window_asserts_the_guard_at_build_time(self):
        text = (UI_DIR / "settings.py").read_text(encoding="utf-8")
        self.assertIn("assert_no_enter_submit", text)
        # and the send path is a button command, not a key binding
        self.assertIn("command=self.send_test", text)
        self.assertIn("command=self.save", text)

    def test_settings_imports_without_a_display(self):
        # building widgets needs a display, importing must not
        from enter_router.ui import settings  # noqa: F401
        self.assertEqual(settings.display_to_meaning(settings.UNSET_LABEL), "")
        self.assertEqual(settings.meaning_to_display("newline"), "改行")
        self.assertEqual(settings.meaning_to_display(""), settings.UNSET_LABEL)

    def test_runtime_imports_without_a_display(self):
        from enter_router.ui import runtime  # noqa: F401
        self.assertTrue(hasattr(runtime, "UiRuntime"))


class GuardTests(unittest.TestCase):
    def test_clean_tree_passes(self):
        root = FakeRoot(children=[FakeWidget("entry", ("<KeyRelease>", "<FocusOut>"))])
        self.assertEqual(ime_guard.find_enter_bindings(root), [])
        ime_guard.assert_no_enter_submit(root)          # must not raise

    def test_return_binding_on_an_entry_is_detected(self):
        entry = FakeWidget("entry", ("<Return>",))
        root = FakeRoot(children=[entry])
        found = ime_guard.find_enter_bindings(root)
        self.assertEqual(found, [("entry", "<Return>")])
        with self.assertRaises(AssertionError) as ctx:
            ime_guard.assert_no_enter_submit(root)
        self.assertIn("IME 安全違反", str(ctx.exception))

    def test_keypad_enter_and_alt_spellings_are_detected(self):
        for sequence in ("<KP_Enter>", "<Key-Return>", "<KeyPress-Return>",
                         "<Key-KP_Enter>"):
            root = FakeRoot(children=[FakeWidget("w", (sequence,))])
            self.assertEqual(len(ime_guard.find_enter_bindings(root)), 1, sequence)

    def test_bind_all_is_detected(self):
        root = FakeRoot(all_sequences=("<Return>",))
        self.assertEqual(ime_guard.find_enter_bindings(root), [("all", "<Return>")])

    def test_nested_widgets_are_walked(self):
        inner = FakeWidget("inner", ("<Return>",))
        outer = FakeWidget("outer", children=[FakeWidget("mid", children=[inner])])
        root = FakeRoot(children=[outer])
        self.assertEqual(ime_guard.find_enter_bindings(root), [("inner", "<Return>")])

    def test_breakage_in_bind_does_not_crash_the_guard(self):
        class Broken(FakeWidget):
            def bind(self, *_args):
                raise RuntimeError("no such widget")
        root = FakeRoot(children=[Broken("broken")])
        self.assertEqual(ime_guard.find_enter_bindings(root), [])


if __name__ == "__main__":
    unittest.main()
