"""Sink selection, CanAccept, failure classification, fallback order (card t_92c498ed §5).

Complements ``test_sinks_fallback.py`` (which covers the Hermes/AiChat/Clipboard
behaviour itself) with the boundaries this card is accepted on:

* sink selection from config and towards ``ISink`` interchangeability;
* ``can_accept`` per sink, including the app / site conditions;
* Hermes failure classification (spawn / model / timeout / empty) and the
  ``_HERMES_GATEWAY`` removal, asserted on the argv/env actually handed to the
  child process;
* the fallback order: sink first, clipboard stash + notify + log only on failure,
  exactly once;
* bulk insert (one paste for the whole text, never per character) and the
  clipboard restore order (stash -> put -> Ctrl+V -> put back);
* ``--dry-run`` sink lines agreeing with the real resolution.

Everything runs on Linux: each OS effect is an injected dependency.
"""
import logging
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app import dryrun  # noqa: E402
from enter_router.app.sink_runner import (SinkJob, SinkWorker,  # noqa: E402
                                          run_with_fallback)
from enter_router.core import config as cfg_mod  # noqa: E402
from enter_router.core import routing  # noqa: E402
from enter_router.core.capture import CaptureOutcome  # noqa: E402
from enter_router.core.config import (AiChatSinkConfig, ClipboardSinkConfig,  # noqa: E402
                                      HermesSinkConfig, parse_config,
                                      parse_config_text)
from enter_router.core.events import ForegroundInfo, TriggerInfo  # noqa: E402
from enter_router.core.sinks import ISink, SinkContext, SinkResult  # noqa: E402
from enter_router.platform import hermes_cli, inject  # noqa: E402
from enter_router.platform.sinks_win import (AiChatSink, ClipboardSink,  # noqa: E402
                                             HermesSink, build_sinks)

SAMPLE = REPO / "config" / "sample.config.json"
SITE_A = ForegroundInfo(process_name="firefox.exe", window_title="Chat",
                        url="https://site-a.example.com/c/1")
SITE_B = ForegroundInfo(process_name="chrome.exe", window_title="Chat",
                        url="https://site-b.example.com/c/2")


def sample_cfg():
    return parse_config_text(SAMPLE.read_text(encoding="utf-8"))


def cfg_with_sinks(**sinks):
    """Sample config with individual sink sections overridden."""
    raw = cfg_mod.default_config_dict()
    for name, patch in sinks.items():
        raw["sinks"][name].update(patch)
    return parse_config(raw)


def cfg_with_operations(**operations):
    """Sample config with layer-1 operations overridden (trigger -> sink)."""
    raw = cfg_mod.default_config_dict()
    raw["global"]["operations"].update(operations)
    return parse_config(raw)


def ctx_for(fg: ForegroundInfo = SITE_B, profile_id: str = "site-b",
            op: str = "enter3") -> SinkContext:
    return SinkContext(
        trigger=TriggerInfo(kind="triple_enter", count=3, profile_id=profile_id,
                            t_ms=1, op=op, meaning="hermes"),
        foreground=fg, captured_text="hello")


class Recorder:
    """Ordered event log: it is the ORDER of stash/notify that matters."""

    def __init__(self):
        self.events: list[tuple] = []

    def stash(self, text):
        self.events.append(("stash", text))
        return True

    def notify(self, title, body, level="info"):
        self.events.append(("notify", title, level))

    def kinds(self):
        return [e[0] for e in self.events]

    def stashed(self):
        return [e[1] for e in self.events if e[0] == "stash"]


# ---------------------------------------------------------------------------
class TestSinkSelection(unittest.TestCase):
    def test_registry_holds_the_three_interchangeable_sinks(self):
        sinks = build_sinks(sample_cfg(), notify=lambda *a, **k: None)
        self.assertEqual(sorted(sinks), ["ai_chat", "clipboard", "hermes"])
        self.assertIsInstance(sinks["clipboard"], ClipboardSink)
        self.assertIsInstance(sinks["ai_chat"], AiChatSink)
        self.assertIsInstance(sinks["hermes"], HermesSink)
        for name, sink in sinks.items():
            with self.subTest(sink=name):
                self.assertIsInstance(sink, ISink)      # same contract, swappable
                self.assertEqual(sink.name, name)

    def test_meaning_to_sink_mapping(self):
        cfg = sample_cfg()
        for meaning, name in (("aichat", "ai_chat"), ("clipboard", "clipboard"),
                              ("hermes", "hermes")):
            with self.subTest(meaning=meaning):
                resolved = routing.resolve_meaning(cfg, cfg.profiles[1], meaning)
                self.assertEqual(resolved, f"sink:{name}")

    def test_select_sink_returns_instances_and_none(self):
        cfg = sample_cfg()
        sinks = build_sinks(cfg, notify=lambda *a, **k: None)
        self.assertIs(routing.select_sink(cfg, sinks, "sink:hermes"), sinks["hermes"])
        self.assertIsNone(routing.select_sink(cfg, sinks, "sink:nope"))
        self.assertIsNone(routing.select_sink(cfg, sinks, "key:enter"))
        self.assertIsNone(routing.select_sink(cfg, sinks, "pass"))

    def test_disabling_a_sink_removes_it_from_selection(self):
        for name in ("clipboard", "ai_chat", "hermes"):
            with self.subTest(sink=name):
                cfg = cfg_with_sinks(**{name: {"enabled": False}})
                sinks = build_sinks(cfg, notify=lambda *a, **k: None)
                self.assertIsNone(routing.select_sink(cfg, sinks, f"sink:{name}"))

    def test_swapping_the_sink_in_config_changes_the_target(self):
        """The same trigger can be pointed at a different sink without code."""
        cfg = cfg_with_operations(enter3="aichat")
        sinks = build_sinks(cfg, notify=lambda *a, **k: None)
        resolved = routing.resolve_operation(cfg, SITE_B, "enter3")
        self.assertEqual(resolved, "sink:ai_chat")
        self.assertIs(routing.select_sink(cfg, sinks, resolved), sinks["ai_chat"])

    def test_a_disabled_sink_falls_back_through_the_clipboard(self):
        cfg = cfg_with_sinks(ai_chat={"enabled": False})
        rec = Recorder()
        sink = AiChatSink(AiChatSinkConfig(), paste=lambda t: 2, notify=lambda *a: None)
        sinks = {"ai_chat": sink,
                 "clipboard": ClipboardSink(ClipboardSinkConfig(), put=rec.stash,
                                            notify=rec.notify)}
        result = run_with_fallback(cfg, sinks, "sink:ai_chat", "text", ctx_for(),
                                   rec.notify)
        self.assertEqual(result.status, "failed")
        self.assertEqual(rec.stashed(), ["text"])


# ---------------------------------------------------------------------------
class TestCanAccept(unittest.TestCase):
    """`CanAccept` — target app / site conditions per sink (card §5)."""

    def test_clipboard_accepts_anywhere_but_only_when_enabled(self):
        sink = ClipboardSink(ClipboardSinkConfig(), put=lambda t: True,
                             notify=lambda *a: None)
        self.assertTrue(sink.can_accept(ctx_for(ForegroundInfo())))
        off = ClipboardSink(ClipboardSinkConfig(enabled=False), put=lambda t: True,
                            notify=lambda *a: None)
        self.assertFalse(off.can_accept(ctx_for(SITE_B)))

    def test_ai_chat_needs_an_identifiable_foreground_app(self):
        sink = AiChatSink(AiChatSinkConfig(), paste=lambda t: 2,
                          type_text=lambda t: 2, press_enter=lambda: 2,
                          focus_guard=lambda fg: True, notify=lambda *a: None)
        self.assertTrue(sink.can_accept(ctx_for(SITE_B)))
        self.assertFalse(sink.can_accept(ctx_for(ForegroundInfo())))   # unknown app
        off = AiChatSink(AiChatSinkConfig(enabled=False), paste=lambda t: 2,
                         type_text=lambda t: 2, press_enter=lambda: 2,
                         focus_guard=lambda fg: True, notify=lambda *a: None)
        self.assertFalse(off.can_accept(ctx_for(SITE_B)))

    def test_ai_chat_accepts_textarea_and_contenteditable_targets_alike(self):
        sink = AiChatSink(AiChatSinkConfig(), paste=lambda t: 2, type_text=lambda t: 2,
                          press_enter=lambda: 2, focus_guard=lambda fg: True,
                          notify=lambda *a: None)
        for fg in (ForegroundInfo(process_name="chrome.exe", window_title="textarea"),
                   ForegroundInfo(process_name="firefox.exe", window_title="contenteditable"),
                   ForegroundInfo(process_name="msedge.exe", window_title="chat")):
            with self.subTest(process=fg.process_name):
                self.assertTrue(sink.can_accept(ctx_for(fg)))

    def test_hermes_ignores_the_foreground_but_honours_enabled(self):
        sink = HermesSink(HermesSinkConfig(), spawn=lambda t, c: None,
                          stash=lambda t: True, notify=lambda *a: None)
        self.assertTrue(sink.can_accept(ctx_for(ForegroundInfo())))
        off = HermesSink(HermesSinkConfig(enabled=False), spawn=lambda t, c: None,
                         stash=lambda t: True, notify=lambda *a: None)
        self.assertFalse(off.can_accept(ctx_for(SITE_B)))

    def test_site_scoped_profiles_select_the_sink_only_for_their_site(self):
        """A URL-scoped profile is the "対象サイト条件": other sites never fire."""
        cfg = cfg_with_operations(enter3="hermes")
        self.assertEqual(routing.resolve_operation(cfg, SITE_B, "enter3"), "sink:hermes")
        other = ForegroundInfo(process_name="chrome.exe", window_title="Mail",
                               url="https://mail.example.com/")
        self.assertEqual(routing.resolve_operation(cfg, other, "enter3"), "pass")
        unreadable = ForegroundInfo(process_name="chrome.exe", window_title="Chat",
                                    url=None)
        self.assertEqual(routing.resolve_operation(cfg, unreadable, "enter3"), "pass")


# ---------------------------------------------------------------------------
class TestFailureClassification(unittest.TestCase):
    def test_the_four_hermes_failure_modes(self):
        cases = [
            (dict(exit_code=None, spawn_error="ENOENT"), "cli_spawn_failed"),
            (dict(exit_code=2, stderr="invalid_params"), "model_connection_failed"),
            (dict(exit_code=None, timed_out=True), "timeout"),
            (dict(exit_code=0, stdout="  \n"), "empty_response"),
        ]
        for kwargs, expected in cases:
            with self.subTest(failure=expected):
                ok, failure = hermes_cli.classify(
                    kwargs.get("exit_code"), kwargs.get("stdout", ""),
                    kwargs.get("stderr", ""), kwargs.get("timed_out", False),
                    kwargs.get("spawn_error"))
                self.assertFalse(ok)
                self.assertEqual(failure, expected)

    def test_invalid_params_with_no_output_is_not_a_success(self):
        """The measured gateway failure: exits, prints nothing, must not look ok."""
        ok, failure = hermes_cli.classify(0, "", "")
        self.assertEqual((ok, failure), (False, "empty_response"))

    def test_gateway_variable_is_stripped_from_the_child_environment(self):
        self.assertIn("_HERMES_GATEWAY", os.environ)   # set by this session
        clean = hermes_cli.child_env(HermesSinkConfig())
        self.assertNotIn("_HERMES_GATEWAY", clean)
        self.assertTrue(all(not k.startswith("_HERMES_") for k in clean))

    def test_wsl_invocation_is_hermes_z_direct_with_a_clean_env(self):
        """Boundary: the argv/env actually handed to the child process."""
        cfg = HermesSinkConfig()
        with mock.patch.object(hermes_cli, "_run",
                               return_value=(0, "answer\n", "", False, None, 12)) as run:
            result = hermes_cli.send_prompt("hello world", cfg)
        self.assertTrue(result.ok)
        argv, timeout, env = run.call_args.args
        # absolute path, no wsl.exe hop (card t_4674d88c); the home directory is
        # resolved at runtime, never hard-coded
        from enter_router.platform import hermes_cli as hc
        self.assertEqual(argv[:2], [hc.hermes_bin(), "-z"])
        self.assertEqual(argv[2], "hello world")
        self.assertEqual(timeout, 30)                                 # card §3 default
        self.assertNotIn("_HERMES_GATEWAY", env)

    def test_long_prompt_uses_the_stdin_form(self):
        long_text = "x" * (hermes_cli.ARGV_FALLBACK_MAX_CHARS + 1)
        cfg = HermesSinkConfig(query_file_stdin=True)   # explicit, host-independent
        with mock.patch.object(hermes_cli, "_run",
                               return_value=(0, "ok\n", "", False, None, 5)) as run:
            hermes_cli.send_prompt(long_text, cfg)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["stdin_bytes"], long_text.encode("utf-8"))
        self.assertNotIn(long_text, " ".join(run.call_args.args[0]))

    def test_long_prompt_on_windows_switches_to_stdin_at_the_boundary(self):
        """Boundary: >ARGV_FALLBACK_MAX_CHARS on Windows must leave the argv form.

        The ARGV form carries the prompt as the last argv element; that is the
        exact shape the Windows interop hop used to shell-evaluate, so long text
        has to travel on stdin. Default config (``query_file_stdin=False``) — the
        switch must not depend on the flag.
        """
        exact = "x" * hermes_cli.ARGV_FALLBACK_MAX_CHARS
        over = "y" * (hermes_cli.ARGV_FALLBACK_MAX_CHARS + 1)
        cfg = HermesSinkConfig()
        self.assertFalse(cfg.query_file_stdin)
        with mock.patch.object(hermes_cli, "is_windows_host", return_value=True), \
                mock.patch.object(hermes_cli, "_run",
                                  return_value=(0, "ok\n", "", False, None, 5)) as run:
            hermes_cli.send_prompt(exact, cfg)                  # boundary: still argv
            argv = run.call_args.args[0]
            self.assertEqual(argv[:4], ["wsl.exe", "--exec", "/bin/bash", "-c"])
            self.assertIn("$HOME/.local/bin/hermes", argv[4])        # fixed path
            self.assertEqual(argv[-1], exact)
            self.assertIsNone(run.call_args.kwargs.get("stdin_bytes"))

            run.reset_mock()
            hermes_cli.send_prompt(over, cfg)                   # +1 char: stdin
            argv = run.call_args.args[0]
            self.assertEqual(argv, hermes_cli.build_stdin_argv(cfg))
            self.assertEqual(run.call_args.kwargs["stdin_bytes"], over.encode("utf-8"))
            self.assertNotIn(over, " ".join(argv))

    def test_empty_stdout_retries_once_through_stdin(self):
        cfg = HermesSinkConfig()
        with mock.patch.object(hermes_cli, "_run",
                               side_effect=[(0, "", "", False, None, 8),
                                            (0, "second try\n", "", False, None, 9)]):
            result = hermes_cli.send_prompt("prompt", cfg)
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "second try\n")

    def test_exit_127_reports_a_spawn_failure_and_does_not_retry(self):
        """Card t_4674d88c: the reproduced default-config failure, end to end.

        The child died with ``exec: hermes: not found`` (exit 127). It must come
        back as ``cli_spawn_failed`` — not ``model_connection_failed`` — and a
        dead binary must not be retried through the stdin form either.
        """
        cfg = HermesSinkConfig()
        with mock.patch.object(hermes_cli, "_run",
                               return_value=(127, "", "exec: hermes: not found",
                                             False, None, 150)) as run:
            result = hermes_cli.send_prompt("prompt", cfg)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "cli_spawn_failed")
        self.assertEqual(run.call_count, 1)          # no blind second attempt

    def test_spawn_failure_message_does_not_blame_the_model(self):
        sink = HermesSink(HermesSinkConfig(),
                          spawn=lambda t, c: mock.Mock(ok=False, failure="cli_spawn_failed",
                                                       elapsed_ms=150, stdout="",
                                                       session_id=None),
                          stash=lambda t: True, notify=lambda *a: None)
        result = sink.send("prompt", ctx_for())
        self.assertEqual(result.failure, "cli_spawn_failed")
        self.assertIn("見つからない", result.message)
        self.assertNotIn("model_connection_failed", result.message)

    def test_every_failure_stashes_the_prompt_and_tells_the_user(self):
        rec = Recorder()
        for failure in ("cli_spawn_failed", "model_connection_failed", "timeout",
                        "empty_response", "internal_error"):
            with self.subTest(failure=failure):
                rec = Recorder()
                spawn = mock.Mock(side_effect=RuntimeError("boom")) if failure == "internal_error" \
                    else (lambda t, c, f=failure: mock.Mock(
                        ok=False, failure=f, elapsed_ms=2000, stdout="", session_id=None))
                sink = HermesSink(HermesSinkConfig(), spawn=spawn, stash=rec.stash,
                                  notify=rec.notify)
                result = sink.send("my prompt", ctx_for())
                self.assertFalse(result.ok)
                self.assertEqual(result.failure, failure)
                self.assertEqual(rec.stashed(), ["my prompt"])
                self.assertIn("notify", rec.kinds())
                self.assertEqual(result.data["fallback"], "clipboard")


# ---------------------------------------------------------------------------
class FakeSink:
    def __init__(self, name, ok=True, failure=None, accept=True, raises=False,
                 fallback=None):
        self.name, self._ok, self._failure = name, ok, failure
        self._accept, self._raises, self._fallback = accept, raises, fallback
        self.sent: list[str] = []

    def can_accept(self, ctx):
        return self._accept

    def send(self, text, ctx):
        if self._raises:
            raise RuntimeError("boom")
        self.sent.append(text)
        if self._ok:
            return SinkResult(sink=self.name, status="ok", message="fine")
        return SinkResult(sink=self.name, status="failed", failure=self._failure,
                          message="nope",
                          data={"fallback": self._fallback} if self._fallback else {})

    def verify(self):
        return None


class TestFallbackOrder(unittest.TestCase):
    """sink first; on failure clipboard stash -> notify -> log, exactly once."""

    def test_order_on_failure(self):
        cfg = sample_cfg()
        rec = Recorder()
        hermes = FakeSink("hermes", ok=False, failure="timeout")
        clipboard = ClipboardSink(ClipboardSinkConfig(), put=rec.stash, notify=rec.notify)
        with self.assertLogs("enter_router.sinks", level="ERROR") as logs:
            result = run_with_fallback(cfg, {"hermes": hermes, "clipboard": clipboard},
                                       "sink:hermes", "payload", ctx_for(), rec.notify)
        self.assertEqual(hermes.sent, ["payload"])         # tried the real sink first
        self.assertEqual(rec.stashed(), ["payload"])       # then exactly one stash
        self.assertEqual(rec.kinds(), ["stash", "notify"])  # notify after the stash
        self.assertEqual(result.status, "failed")
        self.assertTrue(any("fallback" in line for line in logs.output))

    def test_no_stash_when_the_sink_succeeds(self):
        cfg = sample_cfg()
        rec = Recorder()
        clipboard = ClipboardSink(ClipboardSinkConfig(), put=rec.stash, notify=rec.notify)
        result = run_with_fallback(cfg, {"hermes": FakeSink("hermes"), "clipboard": clipboard},
                                   "sink:hermes", "payload", ctx_for(), rec.notify)
        self.assertTrue(result.ok)
        self.assertEqual(rec.events, [])

    def test_raising_and_not_acceptable_sinks_still_fall_back(self):
        cfg = sample_cfg()
        for fake, reason in ((FakeSink("hermes", raises=True), "internal_error"),
                             (FakeSink("hermes", accept=False), "not_acceptable")):
            with self.subTest(reason=reason):
                rec = Recorder()
                clipboard = ClipboardSink(ClipboardSinkConfig(), put=rec.stash,
                                          notify=rec.notify)
                result = run_with_fallback(cfg, {"hermes": fake, "clipboard": clipboard},
                                           "sink:hermes", "payload", ctx_for(), rec.notify)
                self.assertEqual(rec.stashed(), ["payload"])
                self.assertEqual(result.data["reason"], reason)

    def test_worker_capture_failure_never_sends(self):
        cfg = sample_cfg()
        rec = Recorder()
        hermes = FakeSink("hermes")
        finished: list[SinkResult] = []
        worker = SinkWorker(config_provider=lambda: cfg,
                            sinks_provider=lambda: {"hermes": hermes},
                            on_finished=finished.append,
                            capture_fn=lambda: CaptureOutcome(text=None, step=None),
                            notify=rec.notify)
        uncaptured = SinkContext(trigger=ctx_for().trigger, foreground=SITE_B,
                                 captured_text=None)      # the worker must capture
        worker.submit(SinkJob("sink:hermes", uncaptured))
        worker.drain()
        self.assertEqual(hermes.sent, [])
        self.assertEqual(rec.stashed(), [])               # nothing to stash, nothing sent
        self.assertEqual(finished[0].failure, "capture_failed")


# ---------------------------------------------------------------------------
class TestBulkInsertAndRestore(unittest.TestCase):
    def _sink(self, **kw):
        calls = {"paste": [], "type": [], "enter": [], "combo": []}

        def paste(text):
            calls["paste"].append(text)
            return 2

        def type_text(text):
            calls["type"].append(text)
            return len(text) * 2

        def enter():
            calls["enter"].append(1)
            return 2

        def combo(name):
            calls["combo"].append(name)
            return 2

        cfg = kw.pop("cfg", AiChatSinkConfig())
        sink = AiChatSink(cfg, paste=paste, type_text=type_text, press_enter=enter,
                          press_combo=combo, focus_guard=lambda fg: True,
                          send_key=kw.pop("send_key", None),
                          notify=lambda *a: None)
        return sink, calls

    def test_the_whole_text_is_one_paste(self):
        sink, calls = self._sink()
        text = "あ" * 5000
        result = sink.send(text, ctx_for())
        self.assertTrue(result.ok)
        self.assertEqual(calls["paste"], [text])     # ONE paste, not per character
        self.assertEqual(calls["type"], [])
        self.assertEqual(calls["enter"], [])         # insert only by default
        self.assertFalse(result.data["sent_enter"])

    def test_sendinput_method_also_sends_the_text_in_one_call(self):
        sink, calls = self._sink(cfg=AiChatSinkConfig(method="sendinput"))
        text = "x" * 3000
        self.assertTrue(sink.send(text, ctx_for()).ok)
        self.assertEqual(calls["type"], [text])
        self.assertEqual(calls["paste"], [])

    def test_send_after_insert_uses_the_profiles_send_key(self):
        sink, calls = self._sink(cfg=AiChatSinkConfig(send_after_insert=True),
                                 send_key=lambda ctx: "ctrl+enter")
        result = sink.send("text", ctx_for(profile_id="site-a"))
        self.assertTrue(result.ok)
        self.assertEqual(calls["combo"], ["ctrl+enter"])   # profile keymap, not Enter
        self.assertEqual(calls["enter"], [])
        self.assertEqual(result.data["send_key"], "ctrl+enter")

    def test_send_after_insert_falls_back_to_enter_when_the_profile_has_no_send(self):
        cfg = cfg_with_operations()          # keymaps intact
        sink = build_sinks(cfg, notify=lambda *a, **k: None)["ai_chat"]
        self.assertEqual(sink.send_key(ctx_for(profile_id="unknown-profile")), "enter")
        self.assertEqual(sink.send_key(ctx_for(profile_id="site-a")), "ctrl+enter")

    def test_clipboard_is_restored_after_the_paste_and_the_restore_is_logged(self):
        order: list[tuple] = []

        def get():
            order.append(("get",))
            return "user buffer", True

        def put(text):
            order.append(("put", text))
            return True

        def combo(name):
            order.append(("combo", name))
            return 2

        with mock.patch.object(inject, "clipboard_get", get), \
                mock.patch.object(inject, "clipboard_put", put), \
                mock.patch.object(inject, "send_combo", combo):
            with self.assertLogs("enter_router.inject", level="INFO") as logs:
                sent = inject.paste_from_clipboard("hello")
        self.assertEqual(sent, 2)
        self.assertEqual(order, [("get",), ("put", "hello"), ("combo", "ctrl+v"),
                                 ("put", "user buffer")])
        self.assertTrue(any("clipboard restore=ok" in line for line in logs.output))

    def test_restore_still_runs_when_the_paste_raises(self):
        order: list[tuple] = []

        def boom(name):
            order.append(("combo", name))
            raise RuntimeError("SendInput exploded")

        with mock.patch.object(inject, "clipboard_get", lambda: ("prev", True)), \
                mock.patch.object(inject, "clipboard_put",
                                  lambda t: order.append(("put", t)) or True), \
                mock.patch.object(inject, "send_combo", boom):
            with self.assertRaises(RuntimeError):
                inject.paste_from_clipboard("hello")
        self.assertEqual(order[-1], ("put", "prev"))     # restored in the finally

    def test_paste_keeps_the_payload_until_the_target_has_read_it(self):
        """QA-B item 5: SendInput only QUEUES Ctrl+V.

        Chrome reads the clipboard when it processes the paste, after
        ``send_combo`` has returned. Restoring in the same millisecond handed it
        the previous 41-char buffer (value_len=41 vs expect=1274). This fake
        target reads 50ms later, on its own thread, exactly like the browser;
        it must see the payload with the production default settle.
        """
        buffer = ["previous buffer"]
        seen: list[str] = []

        def combo(_name):
            def read_later():
                time.sleep(0.05)                 # the target's own queue
                seen.append(buffer[0])
            threading.Thread(target=read_later, daemon=True).start()
            return 2

        with mock.patch.object(inject, "clipboard_get",
                               lambda: (buffer[0], True)), \
                mock.patch.object(inject, "clipboard_put",
                                  lambda t: buffer.__setitem__(0, t) or True), \
                mock.patch.object(inject, "send_combo", combo):
            sent = inject.paste_from_clipboard("payload")   # default settle
            time.sleep(0.1)                                 # let the reader finish

        self.assertEqual(sent, 2)
        self.assertEqual(seen, ["payload"])                 # never the old buffer
        self.assertEqual(buffer[0], "previous buffer")      # and still restored

    def test_the_restore_is_delayed_by_the_settle(self):
        """Boundary: the put-back must happen *after* settle_s, not immediately."""
        stamps: list[tuple[str, float]] = []

        def put(text):
            stamps.append((text, time.monotonic()))
            return True

        with mock.patch.object(inject, "clipboard_get", lambda: ("prev", True)), \
                mock.patch.object(inject, "clipboard_put", put), \
                mock.patch.object(inject, "send_combo", lambda _n: 2):
            inject.paste_from_clipboard("payload", settle_s=0.15)

        staged_at = [t for text, t in stamps if text == "payload"][0]
        restored_at = [t for text, t in stamps if text == "prev"][0]
        self.assertGreaterEqual(restored_at - staged_at, 0.14)

    def test_default_settle_is_long_enough_for_a_browser(self):
        """The production default must dominate the ~50ms async read above."""
        self.assertGreaterEqual(inject.PASTE_SETTLE_S, 0.2)


# ---------------------------------------------------------------------------
class TestDryRunSinkLines(unittest.TestCase):
    def _lines(self, cfg):
        emitted: list[str] = []
        dryrun.run(cfg, emit=emitted.append)
        return [ln for ln in emitted if ln.startswith("DRYRUN-SINK ")], emitted

    def test_every_sink_resolution_has_a_sink_line(self):
        cfg = sample_cfg()
        sink_l, emitted = self._lines(cfg)
        self.assertTrue(sink_l)
        decision_sinks = {(ln.split("profile=")[1].split()[0], ln.split("resolved=")[1].split()[0])
                          for ln in emitted
                          if ln.startswith("DRYRUN ") and "resolved=sink:" in ln}
        logged = {(ln.split("profile=")[1].split()[0], ln.split("resolved=")[1].split()[0])
                  for ln in sink_l}
        self.assertEqual(logged, decision_sinks)   # log == what the machine decides

    def test_all_three_sinks_can_appear(self):
        cfg = cfg_with_operations(enter="clipboard", enter2="aichat", enter3="hermes")
        sink_l, _ = self._lines(cfg)
        names = {ln.split("sink=")[1].split()[0] for ln in sink_l}
        self.assertEqual(names, {"clipboard", "ai_chat", "hermes"})
        ai = [ln for ln in sink_l if "sink=ai_chat" in ln][0]
        self.assertIn("method=paste", ai)
        self.assertIn("accept=1", ai)
        self.assertIn("payload=captured_text", ai)
        hermes = [ln for ln in sink_l if "sink=hermes" in ln][0]
        self.assertIn("method=hermes-z", hermes)
        self.assertIn("timeout_s=30", hermes)

    def test_the_line_follows_the_config(self):
        base = cfg_with_operations(enter3="hermes")
        changed = cfg_with_operations(enter3="hermes")
        raw = cfg_mod.default_config_dict()
        raw["global"]["operations"]["enter3"] = "hermes"
        raw["sinks"]["hermes"].update({"timeout_sec": 12, "query_file_stdin": True})
        changed = parse_config(raw)
        self.assertIn("timeout_s=30", self._lines(base)[0][0])
        self.assertIn("timeout_s=12", self._lines(changed)[0][0])
        self.assertIn("method=hermes-stdin", self._lines(changed)[0][0])

    def test_a_disabled_sink_is_reported_not_accepted(self):
        raw = cfg_mod.default_config_dict()
        raw["global"]["operations"]["enter3"] = "hermes"
        raw["sinks"]["hermes"]["enabled"] = False
        cfg = parse_config(raw)
        sink_l, _ = self._lines(cfg)
        # resolve_operation still resolves the meaning; the sink is not selected
        self.assertEqual(routing.select_sink(
            cfg, build_sinks(cfg, notify=lambda *a, **k: None), "sink:hermes"), None)
        self.assertTrue(all("accept=0" in ln for ln in sink_l))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
