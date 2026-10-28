"""Settings-window model: the UI's whole brain, free of tkinter (design.md §7).

Why this module exists: the card requires the settings window to edit the config
**schema owned by ``core/config.py``** without inventing fields, to name the
offending item when validation fails, and to preview the resulting behaviour.
None of that needs a widget, so all of it lives here and is unit-tested on Linux
(``tests/test_ui_model.py``); ``ui/settings.py`` only paints it.

Rules this module obeys (design §7 / §8):
* The dict handed to ``AppController.save_config_dict`` is exactly the shape
  ``core/config.py`` parses — no UI-only fields are added anywhere.
* Validation is delegated to ``core.config.parse_config``; the UI never has its
  own opinion about what is valid. It only *labels* the failing path in Japanese.
* Combos are normalized by ``core.config._norm_combo`` (§4.3: ctrl→alt→shift→win,
  the UI must not re-normalize on its own).
* Runtime states (test-send) are an explicit state machine including the
  "unknown" outcome, so the window can never show an unconfirmed success.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from enter_router.core import routing
from enter_router.core.config import (COUNT_OPERATIONS, KEY_MEANINGS, ConfigError,
                                      _norm_combo, parse_config)

# ---------------------------------------------------------------------------
# vocabulary the window shows (Japanese). Order = the order on screen.
# ---------------------------------------------------------------------------
TRIGGERS: tuple[str, ...] = (
    "enter", "enter2", "enter3", "shift+enter", "ctrl+enter", "alt+enter",
) + tuple(f"f{i}" for i in range(1, 13))          # 18 triggers, never collapsed

TRIGGER_LABELS: dict[str, str] = {
    "enter": "Enter（1 回）",
    "enter2": "Enter ×2（連打）",
    "enter3": "Enter ×3（連打）",
    "shift+enter": "Shift + Enter",
    "ctrl+enter": "Ctrl + Enter",
    "alt+enter": "Alt + Enter",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}

# Display order for the meaning dropdown (newline first: it is the 1-Enter default).
MEANING_ORDER: tuple[str, ...] = ("newline", "send", "hermes", "aichat",
                                 "clipboard", "none", "pass")
MEANING_LABELS: dict[str, str] = {
    "newline": "改行",
    "send": "送信",
    "hermes": "Hermes へ送る",
    "aichat": "AI チャットへ",
    "clipboard": "クリップボードへ",
    "none": "なし（このキーを触らない）",
    "pass": "素通し（アプリに任せる）",
}

HOLD_MODE_LABELS: dict[str, str] = {
    "deterministic": "deterministic（保留して判定・既定）",
    "fast": "fast（即時・遅延なし）",
}

POLICY_NUMBER_FIELDS: tuple[tuple[str, str], ...] = (
    ("hold_window_ms", "保留窓（hold_window_ms）"),
    ("triple_enter_window_ms", "3 連続判定の窓（triple_enter_window_ms）"),
    ("trigger_cooldown_ms", "連続トリガのクールダウン（ms）"),
    ("panic_repeat", "パニック連打の回数（panic_repeat）"),
)
# Mirrors core/_POLICY_RANGES; used only for the inline hint, the authority stays
# core.parse_config (a value outside this range is still reported by core).
POLICY_RANGES: dict[str, tuple[int, int]] = {
    "hold_window_ms": (120, 600),
    "triple_enter_window_ms": (120, 600),
    "trigger_cooldown_ms": (0, 5000),
    "panic_repeat": (2, 6),
}

#: Combo strings edited as text (core validates/normalizes them on save).
#: (dotted path in the document, Japanese label)
KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("policy.panic_key", "パニックキー（このキーの連打で停止）"),
    ("registration.process_key", "登録キー: プロセスのみ"),
    ("registration.process_title_key", "登録キー: プロセス＋ウィンドウタイトル"),
)

#: What the settings window should do when the config file changed underneath it.
EXTERNAL_IGNORE = "ignore"          # nothing changed (or no mtime available)
EXTERNAL_RELOAD = "reload"          # no unsaved edits -> take the new document
EXTERNAL_WARN = "warn_dirty"        # unsaved edits -> never overwrite them blindly


def external_config_change(dirty: bool, seen_mtime: float, new_mtime: float) -> str:
    """Classify an external write to the config file (v2-g).

    The registration hotkey edits the file while the settings window may be open.
    Reloading a clean window shows the new profile immediately; reloading a window
    with unsaved edits would silently discard the user's typing, so that case only
    warns and leaves the decision to them ("設定を読み直す" discards explicitly).
    """
    if not new_mtime or new_mtime == seen_mtime:
        return EXTERNAL_IGNORE
    return EXTERNAL_WARN if dirty else EXTERNAL_RELOAD


SINKS: tuple[str, ...] = ("clipboard", "ai_chat", "hermes")
SINK_LABELS: dict[str, str] = {
    "clipboard": "クリップボード",
    "ai_chat": "AI チャット（ブラウザ）",
    "hermes": "Hermes",
}
# (field, Japanese label, kind) — the editor is generated from this table so the
# screen cannot drift away from the schema.
SINK_FIELDS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "clipboard": (("enabled", "有効", "bool"),),
    "ai_chat": (("enabled", "有効", "bool"),
                ("method", "挿入方法", "choice:paste|sendinput"),
                ("send_after_insert", "挿入後に送信", "bool")),
    "hermes": (("enabled", "有効", "bool"),
               ("wsl_distro", "WSL", "str"),
               ("shell", "シェル", "str"),
               ("timeout_sec", "タイムアウト（秒）", "int"),
               ("profile", "プロファイル", "str_or_none"),
               ("query_file_stdin", "標準入力で渡す", "bool")),
}

MATCH_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("process", "プロセス名（例 firefox.exe / すべて = *）", "str"),
    ("title", "ウィンドウタイトル（空 = 条件にしない）", "str_or_none"),
    ("url", "URL パターン（空 = 条件にしない）", "str_or_none"),
)

# ---------------------------------------------------------------------------
# failing-path labels: "project the core error onto a Japanese field name"
# ---------------------------------------------------------------------------
FIELD_LABELS: dict[str, str] = {
    "policy.hold_window_ms": "保留窓（hold_window_ms）",
    "policy.triple_enter_window_ms": "3 連続判定の窓（triple_enter_window_ms）",
    "policy.trigger_cooldown_ms": "連続トリガのクールダウン（ms）",
    "policy.hold_mode": "保留モード（hold_mode）",
    "policy.panic_key": "パニックキー",
    "policy.panic_repeat": "パニック連打の回数",
    "registration": "登録キー",
    "registration.process_key": "登録キー: プロセスのみ",
    "registration.process_title_key": "登録キー: プロセス＋ウィンドウタイトル",
    "policy.enabled": "有効（enabled）",
    "policy": "ポリシー設定",
    "global.operations": "操作マップ（global.operations）",
    "global": "操作マップ（global）",
    "sinks.clipboard": "送り先: クリップボード",
    "sinks.ai_chat": "送り先: AI チャット",
    "sinks.hermes": "送り先: Hermes",
    "sinks": "送り先（sinks）",
    "profiles": "プロファイル",
    "capture": "テキスト取得（capture）",
    "safety": "安全設定（safety）",
    "logging": "ログ設定（logging）",
    "schema_version": "設定ファイルのバージョン（schema_version）",
}

_PATH_RE = re.compile(r"^([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)((?:\[[^\]]*\]|\([^)]*\))*)")


def label_for_path(path: str) -> str:
    """Longest known prefix of ``path`` mapped to its Japanese label."""
    best = ""
    for key in FIELD_LABELS:
        if (path == key or path.startswith(key + ".") or path.startswith(key + "[")) \
                and len(key) > len(best):
            best = key
    return FIELD_LABELS.get(best, "")


@dataclass(frozen=True)
class FieldIssue:
    """One rejected item, named in Japanese plus core's own reason."""
    path: str
    label: str
    detail: str

    def headline(self) -> str:
        if self.label:
            return f"{self.label}: {self.detail}"
        return self.detail


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    message: str = ""
    warnings: tuple[str, ...] = ()
    issues: tuple[FieldIssue, ...] = ()

    def error_text(self) -> str:
        if self.ok:
            return ""
        lines = [issue.headline() for issue in self.issues] or [self.message]
        return "\n".join(lines)


def issues_from_message(message: str) -> tuple[FieldIssue, ...]:
    """Turn a ``ConfigError`` message into ``FieldIssue`` rows.

    core/config.py raises messages that start with the failing path
    ("policy.hold_window_ms must be 120..600", "profiles[2](site-a).keymap[send]: …").
    The path is translated into a Japanese label; the rest is kept verbatim
    because core's wording is the authoritative reason.
    """
    path, detail = "", message
    match = _PATH_RE.match(message)
    if match:
        path = (match.group(1) + match.group(2)).strip()
        rest = message[match.end():].lstrip(" :：")
        detail = rest or message
    return (FieldIssue(path=path, label=label_for_path(path), detail=detail),)


# ---------------------------------------------------------------------------
# test-send state machine (explicit, includes the unknown outcome)
# ---------------------------------------------------------------------------
class TestSendTracker:
    """UI-side state machine for the test-send button.

    ``unknown`` is a real state (design/card): if the worker thread vanished or
    the UI lost track of the answer, we must not show success — we say the
    outcome could not be confirmed. The busy flag is cleared in every terminal
    transition, so the button is never stuck.
    """

    IDLE, SENDING, DONE, ERROR, UNKNOWN = "idle", "sending", "done", "error", "unknown"

    def __init__(self) -> None:
        self.state = self.IDLE
        self.sink = ""
        self.text = ""
        self.result = ""
        self.error = ""

    @property
    def busy(self) -> bool:
        return self.state == self.SENDING

    def begin(self, sink: str, text: str) -> bool:
        """Start a send. Returns False when one is already in flight (guard)."""
        if self.busy:
            return False
        self.state, self.sink, self.text = self.SENDING, sink, text
        self.result, self.error = "", ""
        return True

    def finish(self, result: str) -> None:
        self.state = self.DONE
        self.result = result or ""
        self.error = ""

    def fail(self, error: BaseException | str) -> None:
        self.state = self.ERROR
        self.error = str(error) or type(error).__name__

    def mark_unknown(self, reason: str = "no result") -> None:
        self.state = self.UNKNOWN
        self.error = reason

    def reset(self) -> None:
        self.state, self.sink, self.text = self.IDLE, "", ""
        self.result, self.error = "", ""

    def label(self) -> str:
        if self.state == self.IDLE:
            return "未実行"
        if self.state == self.SENDING:
            return f"送信中…（{SINK_LABELS.get(self.sink, self.sink)}、最大 30 秒ほどかかります）"
        if self.state == self.DONE:
            ok = _looks_successful(self.result)
            mark = "成功" if ok else "失敗"
            return f"{mark}: {self.result or '（結果の文言なし）'}"
        if self.state == self.ERROR:
            return f"失敗（例外）: {self.error}"
        return f"結果不明: 送信されたか確認できません（{self.error}）"


def _looks_successful(result: str) -> bool:
    """The controller returns "<sink>: <status>[ (<failure>)] — <message>".

    Only a status that is neither a FailureKind nor "failed"/"error" counts as
    confirmed success. This is a *display* decision; nothing here claims that a
    non-matching string means the send happened.
    """
    lowered = result.lower()
    bad = ("fail", "error", "timeout", "not_acceptable", "not acceptable",
           "empty", "spawn", "no_text", "unknown", "exception", "拒否", "失敗")
    return bool(result.strip()) and not any(token in lowered for token in bad)


# ---------------------------------------------------------------------------
# the editable document
# ---------------------------------------------------------------------------
class SettingsModel:
    """A single editable copy of the config dict (the window's only state).

    Cancel = throw the copy away and re-copy from the controller; save = hand
    ``to_dict()`` to ``AppController.save_config_dict`` (core validates and, on
    failure, keeps the old file — the model is then reloaded from the controller
    so the screen never shows unsaved-and-rejected values as saved).
    """

    def __init__(self, data: dict) -> None:
        self._data: dict = self._clean(data)
        self._saved: dict = copy.deepcopy(self._data)

    # -- document plumbing --------------------------------------------------
    @staticmethod
    def _clean(data: dict) -> dict:
        doc = copy.deepcopy(data)
        doc.setdefault("schema_version", 1)
        global_doc = doc.setdefault("global", {})
        global_doc.setdefault("operations", {})
        doc.setdefault("policy", {})
        doc.setdefault("sinks", {})
        doc.setdefault("profiles", [])
        return doc

    def to_dict(self) -> dict:
        """The JSON the controller will validate and write (schema untouched)."""
        doc = copy.deepcopy(self._data)
        ops = doc.get("global", {}).get("operations", {})
        doc.setdefault("global", {})["operations"] = _ordered_operations(ops)
        return doc

    def reload(self, data: dict) -> None:
        self._data = self._clean(data)
        self._saved = copy.deepcopy(self._data)

    def snapshot(self) -> dict:
        """The document as last saved/loaded (what キャンセル restores)."""
        return copy.deepcopy(self._saved)

    def mark_saved(self) -> None:
        self._saved = copy.deepcopy(self._data)

    def is_dirty(self) -> bool:
        return self.to_dict() != _ordered_document(self._saved)

    # -- tab A: layer 1 ----------------------------------------------------
    def operation_rows(self) -> tuple[tuple[str, str, str], ...]:
        """(op_key, Japanese trigger label, meaning) for all 18 triggers.

        Triggers the file does not define come back with meaning ``""`` so the
        window can show them as "未設定" instead of hiding them (operator
        requirement: never collapse the list). Extra combos present in the file
        are appended after the 18, never dropped.
        """
        ops = self._data["global"]["operations"]
        rows = [(op, TRIGGER_LABELS.get(op, op), ops.get(op, "")) for op in TRIGGERS]
        extra = [(op, op, meaning) for op, meaning in ops.items() if op not in TRIGGERS]
        return tuple(rows + extra)

    def set_operation(self, op: str, meaning: str) -> None:
        ops = self._data["global"]["operations"]
        if meaning:
            ops[op] = meaning
        else:
            ops.pop(op, None)

    def unset_required_triggers(self) -> tuple[str, ...]:
        ops = self._data["global"]["operations"]
        return tuple(op for op in ("enter", "enter2", "enter3", "shift+enter", "ctrl+enter")
                     if op not in ops)

    def policy(self) -> dict:
        return self._data["policy"]

    def policy_value(self, key: str, default: Any = None) -> Any:
        return self._data["policy"].get(key, default)

    def set_policy(self, key: str, value: Any) -> None:
        self._data["policy"][key] = value

    # -- combo fields (policy.panic_key / registration.*) -------------------
    def key_value(self, path: str, default: Any = "") -> Any:
        section, _, key = path.partition(".")
        return self._data.get(section, {}).get(key, default)

    def set_key(self, path: str, value: Any) -> None:
        section, _, key = path.partition(".")
        self._data.setdefault(section, {})[key] = value

    # -- tab B: layer 2 + sinks -------------------------------------------
    def profiles(self) -> list[dict]:
        return self._data["profiles"]

    def profile_ids(self) -> tuple[str, ...]:
        return tuple(str(p.get("id", "")) for p in self._data["profiles"])

    def profile(self, index: int) -> dict:
        return self._data["profiles"][index]

    def profile_caption(self, index: int) -> str:
        """Listbox caption: id, enabled marker and the match targets."""
        profile = self._data["profiles"][index]
        pid = profile.get("id", "")
        match = profile.get("match") or {}
        parts = [str(match.get("process", "*"))]
        if match.get("title"):
            parts.append(f"title={match['title']}")
        if match.get("url"):
            parts.append(f"url={match['url']}")
        flag = "" if profile.get("enabled", True) else "（無効）"
        return f"{pid}{flag} — {', '.join(parts)}"

    def add_profile(self) -> int:
        pid = self._unique_id("new-app")
        self._data["profiles"].append({
            "id": pid,
            "enabled": False,       # explicit opt-in: creating a profile is the opt-in
            "match": {"process": "*"},
            "keymap": {"send": "ctrl+enter", "newline": "shift+enter"},
        })
        return len(self._data["profiles"]) - 1

    def duplicate_profile(self, index: int) -> int:
        source = copy.deepcopy(self._data["profiles"][index])
        source["id"] = self._unique_id(str(source.get("id", "copy")) + "-copy")
        source["enabled"] = False       # two enabled identical profiles would race
        self._data["profiles"].insert(index + 1, source)
        return index + 1

    def delete_profile(self, index: int) -> bool:
        if len(self._data["profiles"]) <= 1:
            return False                # core requires at least one profile
        del self._data["profiles"][index]
        return True

    def set_profile(self, index: int, field_name: str, value: Any) -> None:
        profile = self._data["profiles"][index]
        if field_name == "enabled":
            profile["enabled"] = bool(value)
            return
        if field_name == "id":
            profile["id"] = str(value)
            return
        match = profile.setdefault("match", {})
        if value in (None, "") and field_name in ("title", "url"):
            match.pop(field_name, None)      # keep the file minimal (design §4.2)
        else:
            match[field_name] = str(value)

    def match_value(self, index: int, field_name: str) -> str:
        match = self._data["profiles"][index].get("match") or {}
        value = match.get(field_name, "*" if field_name == "process" else "")
        return "" if value is None else str(value)

    def keymap_value(self, index: int, meaning: str) -> str:
        return str((self._data["profiles"][index].get("keymap") or {}).get(meaning, ""))

    def set_keymap(self, index: int, meaning: str, combo: str) -> None:
        """Store send/newline -> combo. Empty combo drops the mapping (core warns)."""
        if meaning not in KEY_MEANINGS:
            raise ValueError(f"layer 2 maps only {KEY_MEANINGS} (got {meaning!r})")
        keymap = self._data["profiles"][index].setdefault("keymap", {})
        if not combo:
            keymap.pop(meaning, None)
            return
        keymap[meaning] = combo

    def sink(self, name: str) -> dict:
        return self._data["sinks"].setdefault(name, {})

    def sink_value(self, name: str, key: str, default: Any = None) -> Any:
        return self._data["sinks"].get(name, {}).get(key, default)

    def set_sink(self, name: str, key: str, value: Any) -> None:
        self._data["sinks"].setdefault(name, {})[key] = value

    def _unique_id(self, base: str) -> str:
        existing = set(self.profile_ids())
        if base not in existing:
            return base
        n = 2
        while f"{base}-{n}" in existing:
            n += 1
        return f"{base}-{n}"

    # -- validation + preview ---------------------------------------------
    def validate(self) -> ValidationResult:
        try:
            config = parse_config(self.to_dict())
        except ConfigError as exc:
            message = str(exc)
            return ValidationResult(False, message, (), issues_from_message(message))
        return ValidationResult(True, "", tuple(config.warnings))

    def preview_lines(self) -> list[str]:
        return preview_lines(self.to_dict())


# ---------------------------------------------------------------------------
# helpers on the raw document
# ---------------------------------------------------------------------------
def _ordered_operations(ops: dict) -> dict:
    """Keep the file readable: the 18 triggers in screen order, then extras."""
    ordered = {op: ops[op] for op in TRIGGERS if op in ops}
    ordered.update({k: v for k, v in ops.items() if k not in ordered})
    return ordered


def _ordered_document(doc: dict) -> dict:
    out = copy.deepcopy(doc)
    ops = out.get("global", {}).get("operations", {})
    out.setdefault("global", {})["operations"] = _ordered_operations(ops)
    return out


def normalize_combo(combo: str) -> tuple[bool, str]:
    """(ok, normalized-or-error) using core's canonical normalizer (design §4.3).

    The UI must not re-implement modifier ordering; core owns it.
    """
    try:
        return True, _norm_combo(combo)
    except ConfigError as exc:      # ValueError-derived
        return False, str(exc)


def combo_choices() -> tuple[str, ...]:
    """Preset combos offered by the keymap editor (typing a combo also works)."""
    return ("enter", "shift+enter", "ctrl+enter", "alt+enter", "ctrl+shift+enter",
            "ctrl+alt+enter", "tab", "ctrl+tab", "space") + tuple(f"f{i}" for i in range(1, 13))


# ---------------------------------------------------------------------------
# preview: "what will actually happen" (operator requirement 6)
# ---------------------------------------------------------------------------
_SINK_ACTION: dict[str, str] = {
    "clipboard": "クリップボードへ送る",
    "ai_chat": "AI チャットへ挿入",
    "hermes": "Hermes へ送る",
}


def preview_lines(data: dict) -> list[str]:
    """Human-readable preview of the configuration currently on screen.

    Built from the validated config through ``core.routing`` (never a private
    re-implementation of the two layers): the same function the hook uses.
    """
    try:
        config = parse_config(data)
    except ConfigError as exc:
        return [f"設定が不正のためプレビューできません: {exc}"]

    lines: list[str] = ["この設定での動作:"]
    shown = 0
    for profile in config.profiles:
        if not profile.enabled:
            lines.append(f"・{profile.id}: 無効（このアプリでは常に素通し）")
            continue
        shown += 1
        target = profile.match.process
        if profile.match.title:
            target += f" / title={profile.match.title}"
        if profile.match.url:
            target += f" / url={profile.match.url}"
        lines.append(f"・{profile.id}（{target}）")
        for op in COUNT_OPERATIONS:
            meaning = routing.lookup_meaning(config, op)
            resolved = routing.resolve_meaning(config, profile, meaning)
            lines.append(f"    {TRIGGER_LABELS.get(op, op)} → "
                         f"{_describe(config, meaning, resolved)}")
    if shown == 0:
        lines.append("・有効なプロファイルがありません（すべてのアプリで素通し）")
    lines.append("・プロファイルに一致しないアプリ → すべて素通し（このツールは何もしない）")
    return lines


def _describe(config, meaning: str | None, resolved: str) -> str:
    if meaning is None:
        return "未設定のため素通し（アプリに任せる）"
    if resolved == "pass":
        if meaning in KEY_MEANINGS:
            return f"素通し（keymap に {meaning} が無いため）"
        return "素通し（アプリに任せる）"
    if resolved == "none":
        return "なし（このキーは変換しない・アプリにそのまま届く）"
    if resolved.startswith("key:"):
        combo = resolved[4:]
        what = MEANING_LABELS.get(meaning, meaning)
        return f"{what}（このアプリでは {combo} を送る）"
    if resolved.startswith("sink:"):
        name = resolved[5:]
        action = _SINK_ACTION.get(name, f"{name} へ送る")
        if name == "ai_chat":
            section = config.sinks.get("ai_chat")
            if section is not None and getattr(section, "send_after_insert", False):
                action = "AI チャットへ挿入して送信まで行う"
        if name == "clipboard":
            action = "クリップボードへコピー"
        return action
    return resolved
