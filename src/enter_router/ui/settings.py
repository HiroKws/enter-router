"""The one settings window: 3 tabs, standard tkinter only (design §7 S1).

Tabs
----
A 操作マップ   layer 1 ``global.operations``: **all 18 triggers listed, never
               collapsed**, one meaning dropdown per row; plus ``hold_mode`` and
               the three millisecond windows; the core warnings verbatim; and a
               plain-Japanese preview of what the configuration will do.
B プロファイル layer 2 ``profiles[].keymap`` (send/newline -> real combo) plus the
               match targets and the three sinks' parameters.
C テスト送信   pick a sink, type a sample, explicit click -> result on screen.

Non-negotiables encoded here
----------------------------
* The window edits ``AppController.get_config_dict()`` and saves through
  ``AppController.save_config_dict()`` — the schema stays ``core/config.py``'s.
* Validation errors name the offending item in Japanese and the file is left
  untouched (core decides; the UI only reports).
* Not one ``<Return>`` binding exists anywhere in this window; ``ime_guard``
  asserts it at build time (see ``ui/ime_guard.py`` for the reasoning).
* ``パニック停止`` is a permanent, always-visible button: one click.
* キャンセル restores the last saved document; 保存 reflects immediately, no
  restart (the app re-applies the config in-process and also hot-reloads).
* Test-send is an explicit state machine: in-flight guard, busy flag cleared in
  ``finally``, and an "unknown" outcome that never reads as success.
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from . import ime_guard
from .model import (EXTERNAL_IGNORE, EXTERNAL_RELOAD, EXTERNAL_WARN, HOLD_MODE_LABELS,
                    KEY_FIELDS, MATCH_FIELDS, MEANING_LABELS, MEANING_ORDER,
                    POLICY_NUMBER_FIELDS, POLICY_RANGES, SINK_FIELDS, SINK_LABELS,
                    SINKS, TRIGGER_LABELS, SettingsModel, TestSendTracker,
                    ValidationResult, combo_choices, external_config_change,
                    issues_from_message,
                    normalize_combo)

WINDOW_TITLE = "enter-router 設定"
UNSET_LABEL = "未設定（警告あり）"
STATE_STYLE: dict[str, tuple[str, str]] = {
    # state -> (表示, 背景色)
    "running": ("有効", "#1f7a1f"),
    "paused": ("一時停止", "#8a6d00"),
    "panic": ("パニック停止中", "#a11313"),
    "busy": ("送り先を実行中", "#12467a"),
}
MEANING_DISPLAY: dict[str, str] = {m: MEANING_LABELS[m] for m in MEANING_ORDER}
MEANING_FROM_DISPLAY: dict[str, str] = {v: k for k, v in MEANING_DISPLAY.items()}
MEANING_CHOICES: tuple[str, ...] = tuple(MEANING_DISPLAY[m] for m in MEANING_ORDER)


def meaning_to_display(meaning: str) -> str:
    return MEANING_DISPLAY.get(meaning, UNSET_LABEL if not meaning else meaning)


def display_to_meaning(display: str) -> str:
    if display == UNSET_LABEL:
        return ""
    return MEANING_FROM_DISPLAY.get(display, display)


class SettingsWindow:
    """Owns one Toplevel; the caller (``ui/runtime.py``) owns its lifecycle."""

    def __init__(self, master: tk.Misc, controller: Any,
                 on_close: Callable[[], None] | None = None) -> None:
        self.controller = controller
        self.on_close = on_close
        self.model = SettingsModel(controller.get_config_dict())
        self.tracker = TestSendTracker()
        self._loading = False
        self._profile_index: int | None = None
        self._preview_job: str | None = None
        self._test_queue: "list[tuple[str, Any]]" = []
        self._test_poll_job: str | None = None
        self._pending_test: tuple[str, str] | None = None
        self.status: Any = None
        #: mtime of the config file as last loaded here (None = not seen yet, so
        #: the first status never looks like an external change)
        self._seen_mtime: float | None = None

        self.win = tk.Toplevel(master)
        self.win.title(WINDOW_TITLE)
        # No hardcoded geometry: the height comes from what the tabs need
        # (see _fit_to_content). A fixed 760px clipped the footer — 保存
        # disappeared — as soon as tab A's content grew past it.
        self.win.withdraw()
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self._build()
        self._fit_to_content()
        self._reload_all()
        # The IME guarantee is checked, not just intended (raises on violation).
        ime_guard.assert_no_enter_submit(self.win)
        self.win.deiconify()

    def _fit_to_content(self) -> None:
        """Size the window to its content, clamped to the screen.

        Tk's packer gives the space to whoever was packed FIRST and clips the
        last: with a fixed 760px window, the footer (保存/キャンセル) lost the
        fight against the notebook and was drawn 1px tall. The height therefore
        follows the requested height, never a constant, and the screen is the
        only upper bound.
        """
        self.win.update_idletasks()
        width = max(960, self.win.winfo_reqwidth())
        height = max(620, self.win.winfo_reqheight())
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        width = min(width, max(640, screen_w - 80))
        height = min(height, max(560, screen_h - 140))
        self.win.geometry(f"{width}x{height}")
        self.win.minsize(min(820, width), min(560, height))

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build(self) -> None:
        style = ttk.Style(self.win)
        style.configure("Danger.TButton", foreground="#a11313")
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Big.TLabel", font=("Yu Gothic UI", 12, "bold"))

        header = ttk.Frame(self.win, padding=(10, 8))
        header.pack(side="top", fill="x")
        self.state_chip = tk.Label(header, text="有効", fg="white", bg="#1f7a1f",
                                   font=("Yu Gothic UI", 12, "bold"), padx=12, pady=4)
        self.state_chip.pack(side="left")
        self.state_detail = ttk.Label(header, text="", padding=(10, 0))
        self.state_detail.pack(side="left")
        self.panic_button = ttk.Button(header, text="パニック停止", style="Danger.TButton",
                                       command=self.apply_panic)
        self.panic_button.pack(side="right", padx=(6, 0))
        self.toggle_button = ttk.Button(header, text="一時停止", command=self.apply_toggle)
        self.toggle_button.pack(side="right")

        self.path_label = ttk.Label(self.win, text="設定ファイル: -", padding=(10, 0))
        self.path_label.pack(side="top", fill="x")

        # Bottom bars first, with side="bottom": Tk's packer hands out space in
        # packing order and clips whatever is packed LAST. The notebook (the
        # biggest requester, and the only thing that can usefully shrink) must
        # therefore be packed last — otherwise the footer collapses to 1px and
        # 保存 / キャンセル disappear on a window shorter than the content.
        self.issues_text = tk.Text(self.win, height=3, wrap="word", state="disabled",
                                   bg="#fff8f8")
        self.issues_text.pack(side="bottom", fill="x", padx=10, pady=(0, 8))

        footer = ttk.Frame(self.win, padding=(10, 6))
        footer.pack(side="bottom", fill="x")
        self.save_button = ttk.Button(footer, text="保存", command=self.save)
        self.save_button.pack(side="right")
        self.cancel_button = ttk.Button(footer, text="キャンセル（変更を破棄）",
                                        command=self.cancel)
        self.cancel_button.pack(side="right", padx=(0, 6))
        self.reload_button = ttk.Button(footer, text="設定を読み直す", command=self.cancel)
        self.reload_button.pack(side="right", padx=(0, 6))
        self.message_label = ttk.Label(footer, text="", wraplength=560, justify="left")
        self.message_label.pack(side="left", fill="x", expand=True)

        self.notebook = ttk.Notebook(self.win)
        self.notebook.pack(side="top", fill="both", expand=True, padx=10, pady=(6, 4))
        self.tab_a = ttk.Frame(self.notebook, padding=8)
        self.tab_b = ttk.Frame(self.notebook, padding=8)
        self.tab_c = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_a, text="操作マップ")
        self.notebook.add(self.tab_b, text="プロファイル")
        self.notebook.add(self.tab_c, text="テスト送信")
        self._build_tab_a()
        self._build_tab_b()
        self._build_tab_c()

    # -- tab A ---------------------------------------------------------
    def _build_tab_a(self) -> None:
        grid = ttk.Frame(self.tab_a)
        grid.pack(fill="x", anchor="w")
        self.op_vars: dict[str, tk.StringVar] = {}
        rows = list(TRIGGER_LABELS.items())
        half = (len(rows) + 1) // 2
        for index, (op, label) in enumerate(rows):
            column = 0 if index < half else 2
            row = index % half
            ttk.Label(grid, text=label, width=20).grid(row=row, column=column,
                                                       sticky="w", pady=1)
            var = tk.StringVar()
            combo = ttk.Combobox(grid, textvariable=var, state="readonly", width=26,
                                 values=MEANING_CHOICES + (UNSET_LABEL,))
            combo.grid(row=row, column=column + 1, sticky="w", padx=(0, 18), pady=1)
            combo.bind("<<ComboboxSelected>>",
                       lambda _e, o=op, v=var: self._on_meaning_changed(o, v))
            self.op_vars[op] = var

        policy = ttk.LabelFrame(self.tab_a, text="保留と判定（ポリシー）", padding=8)
        policy.pack(fill="x", pady=(10, 6))
        ttk.Label(policy, text="保留モード（hold_mode）").grid(row=0, column=0, sticky="w")
        self.hold_mode_var = tk.StringVar()
        mode = ttk.Combobox(policy, textvariable=self.hold_mode_var, state="readonly",
                            width=34, values=tuple(HOLD_MODE_LABELS.values()))
        mode.grid(row=0, column=1, sticky="w", padx=(6, 18))
        mode.bind("<<ComboboxSelected>>", lambda _e: self._on_policy_changed())
        self.number_vars: dict[str, tk.StringVar] = {}
        for index, (key, label) in enumerate(POLICY_NUMBER_FIELDS, start=1):
            ttk.Label(policy, text=label).grid(row=index, column=0, sticky="w")
            var = tk.StringVar()
            ttk.Entry(policy, textvariable=var, width=10).grid(row=index, column=1,
                                                               sticky="w", padx=(6, 6))
            low, high = POLICY_RANGES[key]
            ttk.Label(policy, text=f"{low}〜{high}").grid(row=index, column=2, sticky="w")
            var.trace_add("write", lambda *_a: self._on_policy_changed())
            self.number_vars[key] = var

        keys = ttk.LabelFrame(self.tab_a, text="緊急停止と登録キー（コンボ表記: 例 f1 / alt+f5）", padding=8)
        keys.pack(fill="x", pady=(6, 6))
        self.key_vars: dict[str, tk.StringVar] = {}
        for index, (path, label) in enumerate(KEY_FIELDS):
            ttk.Label(keys, text=label).grid(row=index, column=0, sticky="w")
            var = tk.StringVar()
            ttk.Entry(keys, textvariable=var, width=18).grid(row=index, column=1,
                                                             sticky="w", padx=(6, 6))
            var.trace_add("write", lambda *_a: self._on_key_fields_changed())
            self.key_vars[path] = var
        ttk.Label(keys, style="Hint.TLabel",
                  text="パニックは「パニックキーを panic_repeat 回連打」で停止。登録キーは "
                       "alt+F1〜F24 のみ（押した瞬間の前面アプリを設定に追記）").grid(
            row=len(KEY_FIELDS), column=0, columnspan=3, sticky="w", pady=(6, 0))

        warn_frame = ttk.LabelFrame(self.tab_a, text="設定の警告（コアの検証結果をそのまま表示）",
                                    padding=6)
        warn_frame.pack(fill="x")
        self.warnings_text = tk.Text(warn_frame, height=3, wrap="word", state="disabled",
                                     bg="#fffdf0")
        self.warnings_text.pack(fill="x")

        preview_frame = ttk.LabelFrame(self.tab_a, text="この設定での動作プレビュー", padding=6)
        preview_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.preview_text = tk.Text(preview_frame, height=10, wrap="word", state="disabled")
        self.preview_text.pack(fill="both", expand=True)

    # -- tab B ---------------------------------------------------------
    def _build_tab_b(self) -> None:
        # The sinks go at the bottom of the whole tab (full width) so that the
        # three frames are never squeezed into the profile editor's column.
        sinks = ttk.Frame(self.tab_b)
        sinks.pack(side="bottom", fill="x", pady=(8, 0))
        self.sink_vars: dict[tuple[str, str], tk.Variable] = {}
        for column, name in enumerate(SINKS):
            frame = ttk.LabelFrame(sinks, text=f"送り先: {SINK_LABELS[name]}", padding=6)
            frame.grid(row=0, column=column, sticky="nsew", padx=(0, 8))
            sinks.columnconfigure(column, weight=1)
            for row, (field, label, kind) in enumerate(SINK_FIELDS[name]):
                if kind == "bool":
                    var: tk.Variable = tk.BooleanVar()
                    ttk.Checkbutton(frame, text=label, variable=var,
                                    command=self._commit_sinks).grid(row=row, column=0,
                                                                     columnspan=2, sticky="w")
                else:
                    var = tk.StringVar()
                    ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
                    if kind.startswith("choice:"):
                        options = kind.split(":", 1)[1].split("|")
                        widget: tk.Widget = ttk.Combobox(frame, textvariable=var,
                                                         state="readonly", width=9,
                                                         values=options)
                        widget.bind("<<ComboboxSelected>>", lambda _e: self._commit_sinks())
                    else:
                        widget = ttk.Entry(frame, textvariable=var, width=13)
                        widget.bind("<FocusOut>", lambda _e: self._commit_sinks())
                    widget.grid(row=row, column=1, sticky="w", padx=(4, 0))
                self.sink_vars[(name, field)] = var

        body = ttk.Frame(self.tab_b)
        body.pack(side="top", fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(left, text="プロファイル（アプリ / サイト単位）").pack(anchor="w")
        self.profile_list = tk.Listbox(left, height=10, width=38, exportselection=False)
        self.profile_list.pack(fill="y", expand=True)
        self.profile_list.bind("<<ListboxSelect>>", lambda _e: self._on_profile_selected())
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="追加", command=self._add_profile).pack(side="left")
        ttk.Button(buttons, text="複製", command=self._duplicate_profile).pack(side="left", padx=4)
        ttk.Button(buttons, text="削除", command=self._delete_profile).pack(side="left")
        ttk.Label(left, text="未定義のアプリは「素通し」\n（このツールは何もしません）",
                  foreground="#555").pack(anchor="w", pady=(6, 0))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        self.profile_frame = ttk.LabelFrame(right, text="選択中のプロファイル", padding=6)
        self.profile_frame.pack(fill="x")
        self.id_var = tk.StringVar()
        ttk.Label(self.profile_frame, text="ID").grid(row=0, column=0, sticky="w")
        id_entry = ttk.Entry(self.profile_frame, textvariable=self.id_var, width=22)
        id_entry.grid(row=0, column=1, sticky="w", padx=(6, 0))
        id_entry.bind("<FocusOut>", lambda _e: self._commit_profile())
        self.enabled_var = tk.BooleanVar()
        ttk.Checkbutton(self.profile_frame, text="このプロファイルを有効にする",
                        variable=self.enabled_var,
                        command=self._commit_profile).grid(
            row=len(MATCH_FIELDS) + 1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.match_vars: dict[str, tk.StringVar] = {}
        for index, (field, label, _kind) in enumerate(MATCH_FIELDS, start=0):
            ttk.Label(self.profile_frame, text=label).grid(row=index + 1, column=0, sticky="w")
            var = tk.StringVar()
            entry = ttk.Entry(self.profile_frame, textvariable=var, width=30)
            entry.grid(row=index + 1, column=1, sticky="w", padx=(6, 0))
            entry.bind("<FocusOut>", lambda _e: self._commit_profile())
            entry.bind("<KeyRelease>", lambda _e: self._commit_profile())
            self.match_vars[field] = var

        keymap = ttk.LabelFrame(right, text="Enter の挙動（意味 → このアプリで送るキー）",
                                padding=6)
        keymap.pack(fill="x", pady=(8, 0))
        self.keymap_vars: dict[str, tk.StringVar] = {}
        for index, (meaning, label) in enumerate((("send", "送信（send）"),
                                                  ("newline", "改行（newline）"))):
            ttk.Label(keymap, text=label).grid(row=index, column=0, sticky="w")
            var = tk.StringVar()
            combo = ttk.Combobox(keymap, textvariable=var, width=22,
                                 values=combo_choices())
            combo.grid(row=index, column=1, sticky="w", padx=(6, 6))
            combo.bind("<<ComboboxSelected>>",
                       lambda _e, m=meaning, v=var: self._on_keymap_changed(m, v))
            combo.bind("<FocusOut>", lambda _e, m=meaning, v=var: self._on_keymap_changed(m, v))
            self.keymap_vars[meaning] = var
        ttk.Label(keymap, text="空欄にすると「素通し」になり、保存時に警告が出ます",
                  foreground="#555").grid(row=2, column=0, columnspan=3, sticky="w")

    # -- tab C ---------------------------------------------------------
    def _build_tab_c(self) -> None:
        frame = ttk.LabelFrame(self.tab_c, text="テスト送信（現在の設定の送り先へ流します）",
                               padding=10)
        frame.pack(fill="x")
        ttk.Label(frame, text="送り先").grid(row=0, column=0, sticky="w")
        self.test_sink_var = tk.StringVar(value=SINK_LABELS["clipboard"])
        self.test_sink_combo = ttk.Combobox(
            frame, textvariable=self.test_sink_var, state="readonly", width=24,
            values=tuple(SINK_LABELS[name] for name in SINKS))
        self.test_sink_combo.grid(row=0, column=1, sticky="w", padx=(6, 12))

        ttk.Label(frame, text="サンプル文字列").grid(row=1, column=0, sticky="w")
        self.test_text_var = tk.StringVar(value="enter-router のテスト送信です。")
        ttk.Entry(frame, textvariable=self.test_text_var, width=60).grid(
            row=1, column=1, columnspan=2, sticky="w", padx=(6, 0), pady=(4, 0))

        self.test_button = ttk.Button(frame, text="テスト送信を実行", command=self.send_test)
        self.test_button.grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(8, 0))
        ttk.Label(frame, text="送信は明示クリックでのみ実行します（Enter では送信しません）",
                  foreground="#555").grid(row=2, column=2, sticky="w", pady=(8, 0))

        self.test_state_label = ttk.Label(self.tab_c, text="未実行", style="Big.TLabel")
        self.test_state_label.pack(anchor="w", pady=(10, 4))
        self.test_result_text = tk.Text(self.tab_c, height=10, wrap="word", state="disabled")
        self.test_result_text.pack(fill="both", expand=True)
        ttk.Label(self.tab_c, text="Hermes は最大 30 秒かかります。結果はここに残ります。",
                  foreground="#555").pack(anchor="w", pady=(4, 0))

    # ------------------------------------------------------------------
    # loading the document into the widgets
    # ------------------------------------------------------------------
    def _reload_all(self) -> None:
        self._loading = True
        try:
            self._load_operations()
            self._load_policy()
            self._reload_profile_list()
            self._commit_preview()
        finally:
            self._loading = False
        self._update_preview()

    def _load_operations(self) -> None:
        for op, var in self.op_vars.items():
            row = next((r for r in self.model.operation_rows() if r[0] == op), None)
            var.set(meaning_to_display(row[2] if row else ""))

    def _load_policy(self) -> None:
        mode = str(self.model.policy_value("hold_mode", "deterministic"))
        self.hold_mode_var.set(HOLD_MODE_LABELS.get(mode, mode))
        for key, var in self.number_vars.items():
            var.set(str(self.model.policy_value(key, "")))
        for path, var in self.key_vars.items():
            var.set(str(self.model.key_value(path, "")))

    def _on_key_fields_changed(self) -> None:
        if self._loading:
            return
        for path, var in self.key_vars.items():
            raw = var.get().strip()
            if raw:
                self.model.set_key(path, raw)
        # no preview: these are tool hotkeys, not Enter semantics. Core validates
        # and normalizes them on save (a bad combo is reported by field name).

    def _reload_profile_list(self) -> None:
        selection = self._profile_index
        self.profile_list.delete(0, "end")
        for index in range(len(self.model.profiles())):
            self.profile_list.insert("end", self.model.profile_caption(index))
        if not self.model.profiles():
            self._profile_index = None
            self._load_profile_editor(None)
            return
        index = selection if selection is not None and selection < len(self.model.profiles()) else 0
        self.profile_list.selection_clear(0, "end")
        self.profile_list.selection_set(index)
        self._profile_index = index
        self._load_profile_editor(index)

    def _load_profile_editor(self, index: int | None) -> None:
        self._loading = True
        try:
            if index is None:
                self.id_var.set("")
                for var in self.match_vars.values():
                    var.set("")
                self.enabled_var.set(False)
                for var in self.keymap_vars.values():
                    var.set("")
                return
            self.id_var.set(str(self.model.profile(index).get("id", "")))
            self.enabled_var.set(bool(self.model.profile(index).get("enabled", True)))
            for field, var in self.match_vars.items():
                var.set(self.model.match_value(index, field))
            for meaning, var in self.keymap_vars.items():
                var.set(self.model.keymap_value(index, meaning))
            for name in SINKS:
                for field, _label, kind in SINK_FIELDS[name]:
                    var = self.sink_vars[(name, field)]
                    value = self.model.sink_value(name, field, "")
                    if kind == "bool":
                        var.set(bool(value))
                    elif value is None:
                        var.set("")
                    else:
                        var.set(str(value))
        finally:
            self._loading = False

    # ------------------------------------------------------------------
    # edits -> model
    # ------------------------------------------------------------------
    def _on_meaning_changed(self, op: str, var: tk.StringVar) -> None:
        if self._loading:
            return
        self.model.set_operation(op, display_to_meaning(var.get()))
        self._update_preview()

    def _on_policy_changed(self) -> None:
        if self._loading:
            return
        label = self.hold_mode_var.get()
        for mode, text in HOLD_MODE_LABELS.items():
            if label == text:
                self.model.set_policy("hold_mode", mode)
                break
        for key, var in self.number_vars.items():
            raw = var.get().strip()
            if not raw:
                continue
            try:
                self.model.set_policy(key, int(raw))
            except ValueError:
                self.model.set_policy(key, raw)     # core reports the type error
        self._update_preview()

    def _on_keymap_changed(self, meaning: str, var: tk.StringVar) -> None:
        if self._loading or self._profile_index is None:
            return
        raw = var.get().strip()
        if raw:
            ok, normalized = normalize_combo(raw)
            if ok and normalized != raw:
                var.set(normalized)          # core's canonical order, shown at once
                raw = normalized
        self.model.set_keymap(self._profile_index, meaning, raw)
        self._update_preview()

    def _commit_profile(self) -> None:
        if self._loading or self._profile_index is None:
            return
        index = self._profile_index
        self.model.set_profile(index, "id", self.id_var.get().strip())
        self.model.set_profile(index, "enabled", self.enabled_var.get())
        for field, var in self.match_vars.items():
            self.model.set_profile(index, field, var.get().strip())
        caption = self.model.profile_caption(index)
        self.profile_list.delete(index)
        self.profile_list.insert(index, caption)
        # keep the model's id in sync for the very common "renamed" case
        self._update_preview()

    def _commit_sinks(self) -> None:
        if self._loading:
            return
        for name in SINKS:
            for field, _label, kind in SINK_FIELDS[name]:
                var = self.sink_vars[(name, field)]
                if kind == "bool":
                    self.model.set_sink(name, field, bool(var.get()))
                    continue
                raw = str(var.get()).strip()
                if kind == "int":
                    try:
                        self.model.set_sink(name, field, int(raw))
                    except ValueError:
                        self.model.set_sink(name, field, raw)
                elif kind == "str_or_none":
                    self.model.set_sink(name, field, raw or None)
                else:
                    self.model.set_sink(name, field, raw)
        self._update_preview()

    # -- profile buttons ------------------------------------------------
    def _on_profile_selected(self) -> None:
        selection = self.profile_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        if index == self._profile_index:
            return
        self._profile_index = index
        self._load_profile_editor(index)

    def _add_profile(self) -> None:
        self._profile_index = self.model.add_profile()
        self._reload_profile_list()
        self.notebook.select(self.tab_b)
        self._show_message("プロファイルを追加しました。対象を指定して有効にしてください。")

    def _duplicate_profile(self) -> None:
        if self._profile_index is None:
            return
        self._profile_index = self.model.duplicate_profile(self._profile_index)
        self._reload_profile_list()
        self._show_message("複製しました（複製は無効で作成されます）。")

    def _delete_profile(self) -> None:
        if self._profile_index is None:
            return
        if not self.model.delete_profile(self._profile_index):
            self._show_message("プロファイルは最低 1 件必要です（削除できません）。", error=True)
            return
        self._profile_index = min(self._profile_index, len(self.model.profiles()) - 1)
        self._reload_profile_list()
        self._show_message("削除しました（保存するまでファイルは変わりません）。")

    # ------------------------------------------------------------------
    # preview / warnings
    # ------------------------------------------------------------------
    def _update_preview(self) -> None:
        if self._preview_job is not None:
            try:
                self.win.after_cancel(self._preview_job)
            except Exception:            # pragma: no cover - defensive
                pass
        self._preview_job = self.win.after(120, self._commit_preview)

    def _commit_preview(self) -> None:
        self._preview_job = None
        lines = self.model.preview_lines()
        self._set_text(self.preview_text, "\n".join(lines))
        result = self.model.validate()
        if result.ok:
            self._set_text(self.warnings_text, "\n".join(result.warnings) or "警告はありません。")
        else:
            self._set_text(self.warnings_text,
                           "（保存できません）\n" + result.error_text())

    @staticmethod
    def _set_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    # ------------------------------------------------------------------
    # status / panic / pause (single click, always available)
    # ------------------------------------------------------------------
    def refresh_status(self, status: Any) -> None:
        self.status = status
        state = getattr(status, "state", "running")
        text, color = STATE_STYLE.get(state, STATE_STYLE["running"])
        detail = {
            "running": "キーの変換が動いています。",
            "paused": "キーは素通しです（横取りしていません）。",
            "panic": "横取りを停止しました。再開するには「再開」を押してください。",
            "busy": "送り先の処理中です（キーは止まりません）。",
        }.get(state, "")
        if not getattr(status, "hook_installed", True) and state != "panic":
            detail += " フックは未装着です。"
        self.state_chip.configure(text=text, bg=color)
        self.state_detail.configure(text=detail)
        self.toggle_button.configure(text="再開" if state in ("paused", "panic") else "一時停止",
                                    state="normal")
        self.panic_button.configure(state="disabled" if state == "panic" else "normal")
        path = getattr(status, "config_path", "") or "-"
        self.path_label.configure(
            text=f"設定ファイル: {path}"
                 f"    /    フック: {'装着' if getattr(status, 'hook_installed', False) else '未装着'}")
        self._sync_external_change(getattr(status, "config_mtime", 0.0))
        if getattr(status, "last_error", ""):
            self._show_message(f"直近のエラー: {status.last_error}", error=True)

    def _sync_external_change(self, mtime: float) -> None:
        """Another writer (the registration hotkey) edited the file: show it.

        Without this the profile list stayed as it was when the window opened, and
        a later 保存 would have written the OLD document back over the new profile.
        """
        if self._seen_mtime is None:        # first status: adopt, don't reload
            self._seen_mtime = mtime
            return
        action = external_config_change(self.model.is_dirty(), self._seen_mtime, mtime)
        if action == EXTERNAL_IGNORE:
            return
        if action == EXTERNAL_WARN:
            self._show_message(
                "設定ファイルが外部で変更されました（登録キーなど）。未保存の編集があるため"
                "読み直していません。「設定を読み直す」で破棄して読み直してください。", error=True)
            return
        assert action == EXTERNAL_RELOAD
        self._seen_mtime = mtime
        self.model.reload(self.controller.get_config_dict())
        self._reload_all()
        self._show_message("設定ファイルが外部で変更されたため読み直しました（登録キーなど）。")

    def apply_panic(self) -> None:
        """One click, no confirmation dialog (design §7: panic must be 1 step)."""
        try:
            self.controller.panic_stop()
        except Exception as exc:          # noqa: BLE001 - surface, never crash the UI
            self._show_message(f"パニック停止に失敗しました: {exc}", error=True)
            return
        self._show_message("パニック停止: キーの横取りを停止しました。")
        self.refresh_status(self.controller.get_status())

    def apply_toggle(self) -> None:
        state = getattr(self.status, "state", "running") if self.status else "running"
        enable = state in ("paused", "panic")
        try:
            self.controller.set_enabled(enable)
        except Exception as exc:          # noqa: BLE001
            self._show_message(f"切り替えに失敗しました: {exc}", error=True)
            return
        # The live on/off state is part of what 保存 writes (policy.enabled).
        self.model.set_policy("enabled", enable)
        self._show_message("再開しました。" if enable else "一時停止しました（キーは素通し）。")
        self.refresh_status(self.controller.get_status())

    # ------------------------------------------------------------------
    # save / cancel
    # ------------------------------------------------------------------
    def save(self) -> bool:
        self._commit_profile()
        self._commit_sinks()
        self._on_policy_changed()
        result = self.model.validate()
        if not result.ok:
            self._show_validation_failure(result)
            return False
        ok, message = self.controller.save_config_dict(self.model.to_dict())
        if not ok:
            # The file is untouched (core refuses); keep the edits so they can be
            # fixed, and say exactly that.
            self._show_validation_failure(
                ValidationResult(False, message, (), issues_from_message(message)))
            self._show_message(f"保存できませんでした（設定ファイルは変更していません）: {message}",
                               error=True)
            return False
        self.model.reload(self.controller.get_config_dict())
        self.model.mark_saved()
        self._reload_all()
        self._show_message("保存しました（再起動は不要です・即時反映）。"
                           + (f" 警告 {len(result.warnings)} 件。" if result.warnings else ""))
        self.refresh_status(self.controller.get_status())
        return True

    def _show_validation_failure(self, result: ValidationResult) -> None:
        lines = [f"・{issue.headline()}" for issue in result.issues] or [result.message]
        self._set_text(self.issues_text, "保存前の確認で不正な項目があります:\n" + "\n".join(lines))
        self._show_message("入力内容に誤りがあります（下の一覧を確認してください）。", error=True)

    def cancel(self) -> None:
        self.model.reload(self.controller.get_config_dict())
        self.model.mark_saved()
        self._profile_index = 0
        self._reload_all()
        self._set_text(self.issues_text, "")
        self._show_message("変更を破棄し、保存済みの設定に戻しました。")

    def _show_message(self, text: str, error: bool = False) -> None:
        self.message_label.configure(text=text,
                                     foreground="#a11313" if error else "#1f5c1f")

    # ------------------------------------------------------------------
    # test send (explicit state machine, blocks nothing, never lies)
    # ------------------------------------------------------------------
    def send_test(self) -> None:
        sink = next((name for name, label in SINK_LABELS.items()
                     if label == self.test_sink_var.get()), "clipboard")
        text = self.test_text_var.get()
        if not self.tracker.begin(sink, text):
            self._show_test_state()          # in-flight guard: ignore the 2nd click
            return
        self.test_button.configure(state="disabled")
        self._show_test_state()
        self._pending_test = (sink, text)
        self._run_test_async(sink, text)
        self._poll_test_result()

    def _run_test_async(self, sink: str, text: str) -> None:
        def worker() -> None:
            try:
                result = self.controller.test_send(sink, text)
            except BaseException as exc:      # noqa: BLE001 - reported, not swallowed
                self._test_queue.append(("error", exc))
            else:
                self._test_queue.append(("result", result))
        threading.Thread(target=worker, name="test-send", daemon=True).start()

    def _poll_test_result(self) -> None:
        while self._test_queue:
            kind, payload = self._test_queue.pop(0)
            if kind == "result":
                self.tracker.finish(str(payload))
            else:
                self.tracker.fail(payload)        # type: ignore[arg-type]
            self._pending_test = None
            self.test_button.configure(state="normal")     # busy flag cleared
            self._set_text(self.test_result_text,
                           f"送り先: {SINK_LABELS.get(self.tracker.sink, self.tracker.sink)}\n"
                           f"結果: {self.tracker.label()}\n"
                           f"サンプル: {self.tracker.text}")
            self._show_test_state()
            return
        if self.tracker.busy:
            self._test_poll_job = self.win.after(150, self._poll_test_result)

    def _show_test_state(self) -> None:
        self.test_state_label.configure(text=self.tracker.label())

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def focus(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def close(self) -> None:
        if self.model.is_dirty():
            from tkinter import messagebox
            # default="no": Enter (incl. an IME confirm) must not discard edits.
            if not messagebox.askyesno(WINDOW_TITLE,
                                       "保存していない変更があります。破棄して閉じますか？",
                                       default="no", parent=self.win):
                return
        self.destroy()

    def destroy(self) -> None:
        if self._preview_job is not None:
            try:
                self.win.after_cancel(self._preview_job)
            except Exception:            # pragma: no cover - defensive
                pass
        if self._test_poll_job is not None:
            try:
                self.win.after_cancel(self._test_poll_job)
            except Exception:            # pragma: no cover - defensive
                pass
        try:
            self.win.destroy()
        finally:
            if self.on_close is not None:
                self.on_close()
