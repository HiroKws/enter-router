"""Virtual-key tables and combo parsing (pure, stdlib only — no ctypes/win32).

Two consumers:

* ``core/policy.py`` turns a raw ``KeyEvent`` (vk + modifier flags) into the
  layer-1 operation key ("ctrl+enter", "f5", ...) and compares it with the
  configured ``policy.panic_key``. It must never look at the OS.
* ``platform/inject.py`` turns a resolved ``key:<combo>`` back into the VK
  sequence it feeds to SendInput.

Layer-1 operation keys are, per design.md §3.1, Enter-based combos, F1..F12 and
"any combo" (``ctrl+shift+enter``). Letters/digits are included so a user can
bind something like ``ctrl+k`` without touching this module.

Comparison with ``core/config.py::_norm_combo``: that one raises ``ConfigError``
and is the *validation* gate for config files; this one raises ``ValueError``
and is used at runtime. ``tests/test_policy.py`` asserts both produce the same
string for the same input, so the two cannot drift.
"""
from __future__ import annotations

MOD_ORDER = ("ctrl", "alt", "shift", "win")  # canonical order (NOT alphabetical)

# Modifier VKs are never a *base* key of a combo.
MOD_VKS: dict[int, str] = {0x10: "shift", 0x11: "ctrl", 0x12: "alt", 0x5B: "win", 0x5C: "win"}

# Names accepted in a combo string -> VK.
VK_BY_NAME: dict[str, int] = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "escape": 0x1B,
    "esc": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "win": 0x5B,
}
VK_BY_NAME.update({f"f{i}": 0x6F + i for i in range(1, 25)})
VK_BY_NAME.update({chr(ord("a") + i): 0x41 + i for i in range(26)})
VK_BY_NAME.update({str(d): 0x30 + d for d in range(10)})

# VK -> canonical name (the reverse of the table above, with the preferred
# spelling for aliases: "enter" not "return", "escape" not "esc", ...).
NAME_BY_VK: dict[int, str] = {}
for _name in ("enter", "tab", "space", "escape", "backspace", "delete", "insert",
              "home", "end", "pageup", "pagedown", "left", "up", "right", "down"):
    NAME_BY_VK[VK_BY_NAME[_name]] = _name
for _i in range(1, 25):
    NAME_BY_VK[0x6F + _i] = f"f{_i}"
for _i in range(26):
    NAME_BY_VK[0x41 + _i] = chr(ord("a") + _i)
for _d in range(10):
    NAME_BY_VK[0x30 + _d] = str(_d)


class KeyNameError(ValueError):
    """Unknown key name in a combo string."""


def key_name(vk: int) -> str:
    """Canonical name for a VK; "" for a modifier VK, "vk<hex>" for unknown."""
    if vk in MOD_VKS:
        return ""
    return NAME_BY_VK.get(vk, f"vk{vk:02x}")


def key_to_vk(name: str) -> int:
    try:
        return VK_BY_NAME[name.strip().lower()]
    except KeyError as e:
        raise KeyNameError(f"unknown key name {name!r}") from e


def normalize_combo(combo: str) -> str:
    """Lowercase, modifiers in canonical order, one base key. Raises ValueError."""
    if not isinstance(combo, str) or not combo.strip():
        raise KeyNameError(f"invalid key combo: {combo!r}")
    tokens = [t.strip().lower() for t in combo.split("+")]
    if not all(tokens):
        raise KeyNameError(f"invalid key combo: {combo!r}")
    mods = [t for t in tokens if t in MOD_ORDER]
    keys = [t for t in tokens if t not in MOD_ORDER]
    if len(keys) != 1:
        raise KeyNameError(f"combo must have exactly one non-modifier key: {combo!r}")
    # stop duplicate modifiers ("ctrl+ctrl+enter") from silently collapsing
    if len(set(mods)) != len(mods):
        raise KeyNameError(f"duplicate modifier in combo: {combo!r}")
    return "+".join(mods + keys)


def split_combo(combo: str) -> tuple[tuple[str, ...], str]:
    """("ctrl+shift+enter") -> (("ctrl","shift"), "enter"). Normalized."""
    norm = normalize_combo(combo)
    parts = norm.split("+")
    return tuple(parts[:-1]), parts[-1]


def parse_combo(combo: str) -> tuple[tuple[int, ...], int]:
    """(modifier VKs in press order, base VK) for SendInput."""
    mods, key = split_combo(combo)
    return tuple(VK_BY_NAME[m] for m in mods), key_to_vk(key)


def combo_of(vk: int, ctrl: bool = False, alt: bool = False,
             shift: bool = False, win: bool = False) -> str:
    """Raw key state -> the layer-1 operation key string."""
    flags = {"ctrl": ctrl, "alt": alt, "shift": shift, "win": win}
    name = key_name(vk)
    mods = [m for m in MOD_ORDER if flags[m]]
    return "+".join(mods + [name]) if name else ""


def same_combo(a: str, b: str) -> bool:
    """Compare two combo strings without raising on garbage (never trust config)."""
    try:
        return normalize_combo(a) == normalize_combo(b)
    except KeyNameError:
        return False
