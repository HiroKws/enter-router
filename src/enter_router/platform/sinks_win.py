"""The three v1 sinks: ClipboardSink, AiChatSink, HermesSink.

They live in ``platform/`` because two of them inject keys / spawn a process, but
every OS call is an *injected dependency* with a ``platform.*`` default:

    put / paste / type_text / press_enter / focus_guard / spawn / stash / notify

That keeps the interesting policy (fallback, failure classification, insert-only
default) unit-testable on Linux (tests/test_sinks_fallback.py) while the real
adapters stay thin.

Rules that come from the design and must not be relaxed:

* ``send()`` never raises and never logs typed text (research R9).
* Every failure is classified with a :data:`FailureKind` and is visible: SinkResult
  + notification + log. "Silently did nothing" is a defect (design §5.4 / §6).
* ``AiChatSink`` inserts by PASTE by default and **never presses Enter unless
  ``send_after_insert`` is true** (設計 §5.2 / v1 default "挿入のみ").
* ``AiChatSink`` works for ``<textarea>`` and ``contenteditable`` identically:
  it does not look for the widget (browsers do not expose it — research R11), it
  pastes into whatever holds focus. That is the whole mechanism.
* ``HermesSink`` stashes the PROMPT to the clipboard on any failure so the user
  never loses what they typed, then returns a classified failure.
"""
from __future__ import annotations

import logging
from typing import Callable

from enter_router.core.config import AiChatSinkConfig, CaptureConfig, ClipboardSinkConfig, HermesSinkConfig
from enter_router.core.events import ForegroundInfo
from enter_router.core.sinks import SinkContext, SinkResult, VerifyResult

log = logging.getLogger("enter_router.sinks")


def _preview(text: str, limit: int) -> str:
    """Short preview for notifications only — never written to the log."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


SEND_KEY_FALLBACK = "enter"


def default_focus_guard(fg: ForegroundInfo) -> bool:
    """Is the trigger's window still the one we may type into? (card §2)

    Measured facts this encodes:
    * ``SetForegroundWindow`` alone is refused by Windows, ``AppActivate`` lies
      (returns True, changes nothing) — only the S3 sequence + a
      ``GetForegroundWindow`` readback is trustworthy (research S3).
    * If the user moved to a different *application* between trigger and worker
      we must NOT steal focus back and type into their new window; returning
      False sends the text down the clipboard fallback instead.

    Same window -> True. Same process, window behind -> S3 takeover. Different
    process -> False. Unreadable foreground -> True (fail-open, with a log).
    """
    try:
        from enter_router.platform import hook
        hwnd, process, _title = hook.poll_foreground()
    except BaseException:
        log.exception("focus guard could not read the foreground — failing open")
        return True
    if fg.hwnd and hwnd == fg.hwnd:
        return True
    if (process or "").lower() != (fg.process_name or "").lower():
        log.info("ai_chat focus_guard=different_process expected=%s now=%s",
                 fg.process_name or "-", process or "-")
        return False
    if not fg.hwnd:
        return True                     # same process, no handle to aim at
    from enter_router.platform import inject
    ok = inject.take_foreground(fg.hwnd)
    log.info("ai_chat foreground_takeover=%s hwnd=%s", "ok" if ok else "failed", fg.hwnd)
    return ok


def send_key_from_profiles(config) -> Callable[[SinkContext], str]:
    """``SinkContext`` -> the profile's ``keymap["send"]`` combo (card §2).

    "設定で送信キーまで押すを選べるようにし、そのキーはプロファイル設定から取る":
    the combo is read from the profile that matched the foreground window, not
    hard-coded, so a site that sends on Ctrl+Enter gets Ctrl+Enter.
    """
    keymaps = {p.id: dict(p.keymap) for p in getattr(config, "profiles", ())}

    def resolver(ctx: SinkContext) -> str:
        combo = (keymaps.get(ctx.trigger.profile_id) or {}).get("send")
        return combo or SEND_KEY_FALLBACK

    return resolver


class ClipboardSink:
    """Stash the text on the clipboard — silently on success.

    A successful stash is a light, frequent action: a toast per occurrence does
    not fit it, and the decision log already records the sink + char count. Only
    a FAILURE is announced (and the router announces sink failures as well), so
    "nothing happened" is never silent when something actually broke.
    """

    name = "clipboard"

    def __init__(self, cfg: ClipboardSinkConfig | None = None,
                 put: Callable[[str], bool] | None = None,
                 notify: Callable[[str, str, str], None] | None = None,
                 capture: CaptureConfig | None = None) -> None:
        self._cfg = cfg or ClipboardSinkConfig()
        self._capture = capture or CaptureConfig()
        if put is None:
            from enter_router.platform import inject as _inject
            put = _inject.clipboard_put
        if notify is None:
            from enter_router.platform import notify_win as _notify
            notify = _notify.notify
        self._put = put
        self._notify = notify

    def can_accept(self, ctx: SinkContext) -> bool:
        return bool(self._cfg.enabled)

    def stash(self, text: str, notify: bool = True) -> SinkResult:
        """Write ``text`` to the clipboard; ``notify=False`` for the router's fallback.

        The router already tells the user that the *sink* failed, so the fallback
        path asks for the write only — otherwise one failure produced two toasts.
        """
        if not text:
            return SinkResult(sink=self.name, status="skipped", failure="capture_failed",
                              message="no text to stash")
        try:
            if not self._put(text):
                if notify:
                    self._notify("enter-router", "クリップボードを確保できませんでした",
                                 "warning")
                log.error("sink=clipboard failure=clipboard_busy")
                return SinkResult(sink=self.name, status="failed", failure="clipboard_busy",
                                  message="clipboard busy")
            if notify:
                self._notify("enter-router",
                             f"クリップボードに退避しました: "
                             f"{_preview(text, self._capture.preview_chars)}",
                             "info")
            # Deliberately no restore: the stash IS the deliverable (card §1), so
            # the previous copy-buffer content is meant to be replaced.
            log.info("sink=clipboard status=ok chars=%d notify=%s "
                     "restore=no (stash is the goal)", len(text), notify)
            return SinkResult(sink=self.name, status="ok", message="stashed on clipboard")
        except BaseException:
            log.exception("sink=clipboard failure=internal_error")
            return SinkResult(sink=self.name, status="failed", failure="internal_error",
                              message="unexpected clipboard error")

    def send(self, text: str, ctx: SinkContext) -> SinkResult:
        # notify=False on success; a clipboard-busy failure is reported by the
        # router's fallback path (one failure must not produce two toasts).
        return self.stash(text, notify=False)

    def verify(self) -> VerifyResult:
        try:
            from enter_router.platform import inject as _inject
            _text, ok = _inject.clipboard_get()
            return VerifyResult(sink=self.name, ok=bool(ok),
                                detail="clipboard readable" if ok else "clipboard busy")
        except BaseException as e:
            return VerifyResult(sink=self.name, ok=False, detail=str(e))


class AiChatSink:
    """Insert text into the focused input (textarea OR contenteditable)."""

    name = "ai_chat"

    def __init__(self, cfg: AiChatSinkConfig | None = None,
                 paste: Callable[[str], int] | None = None,
                 type_text: Callable[[str], int] | None = None,
                 press_enter: Callable[[], int] | None = None,
                 focus_guard: Callable[[ForegroundInfo], bool] | None = None,
                 notify: Callable[[str, str, str], None] | None = None,
                 capture: CaptureConfig | None = None,
                 send_key: Callable[[SinkContext], str] | None = None,
                 press_combo: Callable[[str], int] | None = None) -> None:
        self._cfg = cfg or AiChatSinkConfig()
        self._capture = capture or CaptureConfig()
        if paste is None or type_text is None or press_enter is None:
            from enter_router.platform import inject as _inject
            paste = paste or _inject.paste_from_clipboard
            type_text = type_text or _inject.send_text_unicode
            press_enter = press_enter or (lambda: _inject.send_enter(1))
        if press_combo is None:
            from enter_router.platform import inject as _inject
            press_combo = _inject.send_combo
        if notify is None:
            from enter_router.platform import notify_win as _notify
            notify = _notify.notify
        self._paste = paste
        self._type = type_text
        self._enter = press_enter
        self._press_combo = press_combo
        self._focus_guard = focus_guard or default_focus_guard
        self._send_key = send_key or (lambda ctx: SEND_KEY_FALLBACK)
        self._notify = notify

    @property
    def send_after_insert(self) -> bool:
        """v1 default is False: insert only, the user sends (設計 §5.2)."""
        return bool(self._cfg.send_after_insert)

    def can_accept(self, ctx: SinkContext) -> bool:
        return bool(self._cfg.enabled) and bool(ctx.foreground.process_name)

    def send_key(self, ctx: SinkContext) -> str:
        """The combo pressed when ``send_after_insert`` is on (profile config)."""
        return self._send_key(ctx) or SEND_KEY_FALLBACK

    def send(self, text: str, ctx: SinkContext) -> SinkResult:
        if not text:
            return SinkResult(sink=self.name, status="skipped", failure="capture_failed",
                              message="no text captured")
        try:
            if not self._focus_guard(ctx.foreground):
                log.error("sink=ai_chat failure=focus_failed")
                self._notify("enter-router", "入力欄にフォーカスできませんでした", "warning")
                return SinkResult(sink=self.name, status="failed", failure="focus_failed",
                                  message="target window is no longer focused")
            if self._cfg.method == "sendinput":
                sent = self._type(text)
            else:
                sent = self._paste(text)     # one paste event (L2, research 2-c)
            if not sent:
                log.error("sink=ai_chat failure=injected_blocked")
                self._notify("enter-router", "入力が拒否されました（管理者権限のウィンドウ？）",
                             "warning")
                return SinkResult(sink=self.name, status="failed", failure="injected_blocked",
                                  message="SendInput delivered no events (UIPI?)")
            sent_enter = False
            combo = ""
            if self.send_after_insert:
                combo = self.send_key(ctx)     # profile keymap, not hard-coded Enter
                if combo == SEND_KEY_FALLBACK:
                    sent_enter = bool(self._enter())
                else:
                    sent_enter = bool(self._press_combo(combo))
            log.info("sink=ai_chat status=ok method=%s chars=%d send_after_insert=%s send_key=%s",
                     self._cfg.method, len(text), self.send_after_insert, combo or "-")
            return SinkResult(sink=self.name, status="ok",
                              message=f"inserted ({self._cfg.method})"
                                      + (f" + {combo}" if sent_enter else " (insert only)"),
                              data={"sent_enter": sent_enter, "send_key": combo})
        except BaseException:
            log.exception("sink=ai_chat failure=internal_error")
            return SinkResult(sink=self.name, status="failed", failure="internal_error",
                              message="unexpected injection error")

    def verify(self) -> VerifyResult:
        return VerifyResult(sink=self.name, ok=bool(self._cfg.enabled),
                            detail=f"method={self._cfg.method} "
                                   f"send_after_insert={self.send_after_insert}")


class HermesSink:
    """Send the prompt to the Hermes agent through the non-interactive CLI."""

    name = "hermes"

    def __init__(self, cfg: HermesSinkConfig | None = None,
                 spawn: Callable[[str, HermesSinkConfig], object] | None = None,
                 stash: Callable[[str], bool] | None = None,
                 notify: Callable[[str, str, str], None] | None = None,
                 capture: CaptureConfig | None = None) -> None:
        self._cfg = cfg or HermesSinkConfig()
        self._capture = capture or CaptureConfig()
        if spawn is None:
            from enter_router.platform import hermes_cli as _cli
            spawn = _cli.send_prompt
        if stash is None:
            from enter_router.platform import inject as _inject
            stash = _inject.clipboard_put
        if notify is None:
            from enter_router.platform import notify_win as _notify
            notify = _notify.notify
        self._spawn = spawn
        self._stash = stash
        self._notify = notify

    def can_accept(self, ctx: SinkContext) -> bool:
        return bool(self._cfg.enabled)

    def send(self, text: str, ctx: SinkContext) -> SinkResult:
        if not text:
            return SinkResult(sink=self.name, status="skipped", failure="capture_failed",
                              message="no prompt to send")
        try:
            result = self._spawn(text, self._cfg)
        except BaseException:
            # the adapter promises not to raise; if it does, still classify it
            log.exception("sink=hermes failure=internal_error")
            result = None
        failure = getattr(result, "failure", None) if result is not None else "internal_error"
        ok = bool(getattr(result, "ok", False)) if result is not None else False
        elapsed = int(getattr(result, "elapsed_ms", 0)) if result is not None else 0
        stdout = str(getattr(result, "stdout", "")) if result is not None else ""
        session_id = getattr(result, "session_id", None) if result is not None else None

        if ok:
            if session_id:
                # v1: log only (v2 will open the session in the desktop app)
                log.info("sink=hermes session_id=%s", session_id)
            log.info("sink=hermes status=ok elapsed_ms=%d chars_out=%d",
                     elapsed, len(stdout))
            stashed = self._stash_safely(stdout)
            # Summary only, capped by preview_chars: the full body goes to the
            # clipboard, never to the notification or the log (research R9).
            self._notify("Hermes",
                         f"{_preview(stdout, self._capture.preview_chars)}"
                         f" ({elapsed / 1000:.1f}s)",
                         "info")
            return SinkResult(sink=self.name, status="ok",
                              message=f"hermes: ok ({elapsed / 1000:.1f}s)"
                                      + ("" if stashed else " — 応答の退避に失敗"),
                              data={"session_id": session_id or "",
                                    "response_chars": len(stdout),
                                    "response_stashed": stashed})
        # --- failure: never silent, always stash the PROMPT ------------------
        failure = failure or "internal_error"
        # "the executable could not be run" must not read like a model problem
        label = ("cli_spawn_failed (hermes 実行ファイルが見つからない/起動失敗)"
                 if failure == "cli_spawn_failed" else failure)
        log.error("sink=hermes failure=%s elapsed_ms=%d", label, elapsed)
        stashed = self._stash_safely(text)
        self._notify("Hermes 送信失敗",
                     f"{label} — プロンプトをクリップボードに退避しました"
                     if stashed else f"{label} — 退避にも失敗しました",
                     "error")
        return SinkResult(sink=self.name, status="failed", failure=failure,
                          message=f"hermes: {label} -> clipboard fallback"
                                  + ("" if stashed else " (stash failed)"),
                          data={"fallback": "clipboard" if stashed else "none",
                                "session_id": session_id or ""})

    def _stash_safely(self, text: str) -> bool:
        try:
            return bool(self._stash(text)) if text else False
        except BaseException:
            log.exception("sink=hermes clipboard stash failed")
            return False

    def verify(self) -> VerifyResult:
        try:
            from enter_router.platform import hermes_cli as _cli
            ok, detail = _cli.verify(self._cfg)
            return VerifyResult(sink=self.name, ok=ok, detail=detail)
        except BaseException as e:
            return VerifyResult(sink=self.name, ok=False, detail=str(e))


def build_sinks(config, notify=None) -> dict[str, object]:
    """Registry used by the app wiring: name -> sink instance (design §5.1).

    This is the "thin wiring" part: the three sinks are interchangeable and the
    registry is rebuilt from the config on reload, so enabling/disabling or
    swapping a sink is a config edit (card acceptance #1).
    """
    sinks: dict[str, object] = {}
    capture_cfg = config.capture
    send_key = send_key_from_profiles(config)
    clip_cfg = config.sinks.get("clipboard")
    if isinstance(clip_cfg, ClipboardSinkConfig):
        sinks["clipboard"] = ClipboardSink(clip_cfg, notify=notify, capture=capture_cfg)
    ai_cfg = config.sinks.get("ai_chat")
    if isinstance(ai_cfg, AiChatSinkConfig):
        sinks["ai_chat"] = AiChatSink(ai_cfg, notify=notify, capture=capture_cfg,
                                      focus_guard=default_focus_guard, send_key=send_key)
    hermes_cfg = config.sinks.get("hermes")
    if isinstance(hermes_cfg, HermesSinkConfig):
        sinks["hermes"] = HermesSink(hermes_cfg, notify=notify, capture=capture_cfg)
    return sinks
