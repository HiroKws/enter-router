"""IME safety for the settings window (card acceptance criterion, design §7).

The hazard: a Japanese IME confirm is an Enter keypress. If the settings window
binds Enter to a "default button" (save / send), then confirming a conversion
also saves or sends — the classic bug this card must not ship.

Two mechanisms, both cheap:

1. **No Enter binding at all.** The window never binds ``<Return>`` /
   ``<KP_Enter>`` on a widget, a toplevel, a class or ``all``. tkinter has no
   implicit default button (unlike WPF ``IsDefault`` or HTML forms), so the
   absence of a binding is the whole fix: Enter in an Entry inserts nothing and
   triggers nothing, save/send require an explicit click.
2. **A mechanical check that it stays that way.** ``find_enter_bindings`` walks a
   live widget tree and ``assert_no_enter_submit`` fails loudly if anyone adds
   one later. ``tests/test_ui_ime.py`` runs the walk against a fake tree on
   Linux and ``scripts/win_ui_check.py`` runs it against the real Windows Tk
   window. The settings window calls the assert on itself when it is built.

A third, deliberate design choice supports the same goal: key combos are edited
with a Combobox of preset combos (typing is allowed) instead of a "press a key
to record it" capture widget. A capture widget would have to intercept Enter in
an Entry, which is precisely the interaction that must stay unambiguous.
"""
from __future__ import annotations

from typing import Any, Iterable

# Every spelling of the Enter key that can reach a binding table.
ENTER_SEQUENCES: tuple[str, ...] = (
    "<Return>", "<KP_Enter>", "<Key-Return>", "<KeyPress-Return>",
    "<Key-KP_Enter>", "<KeyPress-KP_Enter>",
)


def _is_enter(sequence: str) -> bool:
    text = str(sequence)
    return text in ENTER_SEQUENCES or text.startswith("<Key-Return") \
        or text.startswith("<KeyPress-Return") or text.startswith("<Key-KP_Enter")


def _widget_bindings(widget: Any) -> tuple[str, ...]:
    try:
        bound = widget.bind()
    except Exception:            # pylint: disable=broad-except - never break the UI
        return ()
    return tuple(str(seq) for seq in (bound or ()))


def _all_bindings(root: Any) -> tuple[str, ...]:
    try:
        bound = root.bind_all()
    except Exception:            # pragma: no cover - defensive
        return ()
    return tuple(str(seq) for seq in (bound or ()))


def walk(widget: Any) -> Iterable[Any]:
    """Depth-first over ``widget`` and its children (duck-typed for tests)."""
    yield widget
    try:
        children = widget.winfo_children()
    except Exception:            # pragma: no cover - defensive
        return
    for child in children or ():
        yield from walk(child)


def _name(widget: Any) -> str:
    try:
        return str(widget.winfo_name()) or widget.__class__.__name__
    except Exception:            # pragma: no cover - defensive
        return widget.__class__.__name__


def find_enter_bindings(root: Any) -> list[tuple[str, str]]:
    """[(widget name, sequence)] for every Enter binding under ``root``."""
    found: list[tuple[str, str]] = []
    for widget in walk(root):
        for sequence in _widget_bindings(widget):
            if _is_enter(sequence):
                found.append((_name(widget), sequence))
    for sequence in _all_bindings(root):
        if _is_enter(sequence):
            found.append(("all", sequence))
    return found


def assert_no_enter_submit(root: Any) -> None:
    """Raise AssertionError if Enter is bound anywhere in this window."""
    found = find_enter_bindings(root)
    if found:
        detail = ", ".join(f"{name}:{seq}" for name, seq in found)
        raise AssertionError(
            "IME 安全違反: Enter が束縛されています（IME 確定 Enter が保存/送信を"
            f"誤発火します）: {detail}. 保存/送信は明示クリックのみにしてください。")
