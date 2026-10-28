"""Sink behaviour tests: fallback chain, insert-only default, failure classification.

These run on Linux because every OS effect in ``platform/sinks_win.py`` is an
injected dependency. What is asserted here is the policy that the QA card would
otherwise have to discover on hardware:

* HermesSink failure -> the PROMPT is stashed to the clipboard + notify + log.
* AiChatSink never presses Enter unless ``send_after_insert`` is true.
* AiChatSink consumes both textarea and contenteditable the same way (it pastes
  into whatever holds focus — there is no widget lookup to go wrong).
* run_with_fallback never leaves a failure silent and never double-stashes.
* capture failure => nothing is sent (design §5.4-3).
"""
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app.sink_runner import (SinkJob, SinkWorker, capture_text,  # noqa: E402
                                          run_with_fallback)
from enter_router.core import config as cfg_mod  # noqa: E402
from enter_router.core.capture import CaptureOutcome  # noqa: E402
from enter_router.core.config import (AiChatSinkConfig, CaptureConfig,  # noqa: E402
                                      ClipboardSinkConfig, Config, HermesSinkConfig,
                                      parse_config_text)
from enter_router.core.events import ForegroundInfo, TriggerInfo  # noqa: E402
from enter_router.core.sinks import SinkContext, SinkResult  # noqa: E402
from enter_router.platform.sinks_win import (AiChatSink, ClipboardSink,  # noqa: E402
                                             HermesSink)

SAMPLE = REPO / "config" / "sample.config.json"
FG = ForegroundInfo(process_name="chrome.exe", window_title="Chat", url="https://x/")

CTX = SinkContext(
    trigger=TriggerInfo(kind="triple_enter", count=3, profile_id="site-b", t_ms=1,
                        op="enter3", meaning="hermes"),
    foreground=FG, captured_text="hello")

#: The worker does the capture itself, so its jobs start with no text.
CTX_UNCAPTURED = SinkContext(
    trigger=CTX.trigger, foreground=FG, captured_text=None)


@dataclass
class SpawnResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = 0
    elapsed_ms: int = 3800
    failure: str | None = None
    session_id: str | None = None


class Recorder:
    def __init__(self):
        self.stashed: list[str] = []
        self.notified: list[tuple[str, str, str]] = []

    def stash(self, text: str) -> bool:
        self.stashed.append(text)
        return True

    def notify(self, title: str, body: str, level: str = "info") -> None:
        self.notified.append((title, body, level))


def sample_cfg() -> Config:
    return parse_config_text(SAMPLE.read_text(encoding="utf-8"))


class TestSinkContract(unittest.TestCase):
    """Acceptance: the three implementations really satisfy ISink (design §5.1)."""

    def test_all_three_sinks_are_isink(self):
        from enter_router.core.sinks import ISink
        cfg = sample_cfg()
        sinks = [ClipboardSink(ClipboardSinkConfig(), put=lambda t: True,
                              notify=lambda *a: None),
                 AiChatSink(AiChatSinkConfig(), paste=lambda t: 2,
                            type_text=lambda t: 2, press_enter=lambda: 2,
                            notify=lambda *a: None),
                 HermesSink(HermesSinkConfig(), spawn=lambda t, c: SpawnResult(ok=True, stdout="x"),
                            stash=lambda t: True, notify=lambda *a: None)]
        for sink in sinks:
            with self.subTest(sink=sink.name):
                self.assertIsInstance(sink, ISink)
                self.assertTrue(isinstance(sink.name, str) and sink.name)
                self.assertIsInstance(sink.can_accept(CTX), bool)
                self.assertIsInstance(sink.send("x", CTX).status, str)
                self.assertIsNotNone(sink.verify())


# ---------------------------------------------------------------------------
class TestHermesSink(unittest.TestCase):
    def make(self, spawn, rec: Recorder) -> HermesSink:
        return HermesSink(HermesSinkConfig(), spawn=spawn, stash=rec.stash,
                          notify=rec.notify)

    def test_success_stashes_the_response_and_keeps_the_session_id(self):
        rec = Recorder()
        sink = self.make(lambda text, cfg: SpawnResult(
            ok=True, stdout="answer", session_id="local-" + "a" * 64), rec)
        result = sink.send("prompt", CTX)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["session_id"], "local-" + "a" * 64)
        self.assertEqual(rec.stashed, ["answer"])       # the RESPONSE is offered
        self.assertIn("3.8s", result.message)

    def test_success_notification_is_a_capped_summary(self):
        rec = Recorder()
        body = "x" * 400
        sink = self.make(lambda text, cfg: SpawnResult(ok=True, stdout=body), rec)
        self.assertTrue(sink.send("prompt", CTX).ok)
        self.assertTrue(rec.notified)
        title, text, level = rec.notified[0]
        self.assertEqual(title, "Hermes")
        self.assertEqual(level, "info")
        # preview_chars (60) + ellipsis + the elapsed suffix; never the full body
        self.assertLess(len(text), len(body))
        self.assertTrue(text.startswith("x" * 60))
        self.assertIn("3.8s", text)

    def test_each_failure_kind_stashes_the_prompt(self):
        for failure in ("cli_spawn_failed", "model_connection_failed", "timeout",
                        "empty_response"):
            with self.subTest(failure=failure):
                rec = Recorder()
                sink = self.make(lambda text, cfg, f=failure: SpawnResult(
                    ok=False, failure=f, exit_code=0 if f == "empty_response" else 1),
                    rec)
                result = sink.send("my precious prompt", CTX)
                self.assertFalse(result.ok)
                self.assertEqual(result.failure, failure)
                self.assertEqual(rec.stashed, ["my precious prompt"])  # prompt saved
                self.assertEqual(result.data["fallback"], "clipboard")
                self.assertTrue(rec.notified)                          # user told
                self.assertIn("clipboard fallback", result.message)

    def test_spawn_that_raises_is_still_classified(self):
        def boom(text, cfg):
            raise RuntimeError("adapter bug")
        rec = Recorder()
        result = self.make(boom, rec).send("p", CTX)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "internal_error")
        self.assertEqual(rec.stashed, ["p"])

    def test_empty_text_is_skipped_without_touching_the_clipboard(self):
        rec = Recorder()
        result = self.make(lambda t, c: SpawnResult(ok=True, stdout="x"), rec).send("", CTX)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.failure, "capture_failed")
        self.assertEqual(rec.stashed, [])

    def test_stash_failure_is_reported_not_hidden(self):
        rec = Recorder()
        sink = HermesSink(HermesSinkConfig(),
                          spawn=lambda t, c: SpawnResult(ok=False, failure="timeout"),
                          stash=lambda text: False, notify=rec.notify)
        result = sink.send("p", CTX)
        self.assertEqual(result.data["fallback"], "none")
        self.assertIn("stash failed", result.message)
        self.assertTrue(rec.notified)


# ---------------------------------------------------------------------------
class TestAiChatSink(unittest.TestCase):
    def make(self, **kw) -> tuple[AiChatSink, dict]:
        calls = {"paste": [], "type": [], "enter": [], "focus": []}

        def paste(text):
            calls["paste"].append(text)
            return kw.pop("paste_events", 2)

        def type_text(text):
            calls["type"].append(text)
            return kw.pop("type_events", 4)

        def enter():
            calls["enter"].append(1)
            return 2

        def focus(fg):
            calls["focus"].append(fg.process_name)
            return kw.pop("focus_ok", True)

        cfg = kw.pop("cfg", AiChatSinkConfig())
        sink = AiChatSink(cfg, paste=paste, type_text=type_text, press_enter=enter,
                          focus_guard=focus, notify=lambda *a: None)
        return sink, calls

    def test_default_is_insert_only(self):
        sink, calls = self.make()
        result = sink.send("text", CTX)
        self.assertTrue(result.ok)
        self.assertEqual(calls["paste"], ["text"])
        self.assertEqual(calls["enter"], [])            # Enter is NOT pressed
        self.assertFalse(result.data["sent_enter"])
        self.assertIn("insert only", result.message)

    def test_send_after_insert_is_opt_in(self):
        sink, calls = self.make(cfg=AiChatSinkConfig(send_after_insert=True))
        result = sink.send("text", CTX)
        self.assertTrue(result.ok)
        self.assertEqual(calls["enter"], [1])
        self.assertTrue(result.data["sent_enter"])

    def test_sendinput_method_types_instead_of_pasting(self):
        sink, calls = self.make(cfg=AiChatSinkConfig(method="sendinput"))
        self.assertTrue(sink.send("text", CTX).ok)
        self.assertEqual(calls["type"], ["text"])
        self.assertEqual(calls["paste"], [])

    def test_zero_events_is_reported_as_injected_blocked(self):
        sink, _calls = self.make(paste_events=0)
        result = sink.send("text", CTX)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "injected_blocked")

    def test_focus_loss_is_focus_failed(self):
        sink, _calls = self.make(focus_ok=False)
        result = sink.send("text", CTX)
        self.assertEqual(result.failure, "focus_failed")

    def test_can_accept_needs_a_foreground_window(self):
        sink, _calls = self.make()
        self.assertFalse(sink.can_accept(SinkContext(
            trigger=CTX.trigger, foreground=ForegroundInfo(), captured_text="x")))
        self.assertTrue(sink.can_accept(CTX))

    def test_textarea_and_contenteditable_use_the_same_path(self):
        """There is no widget lookup: focus + paste works for both by design."""
        sink, calls = self.make()
        for fg in (ForegroundInfo(process_name="chrome.exe",
                                  window_title="textarea chat"),
                   ForegroundInfo(process_name="firefox.exe",
                                  window_title="contenteditable chat")):
            ctx = SinkContext(trigger=CTX.trigger, foreground=fg, captured_text="x")
            self.assertTrue(sink.send("x", ctx).ok)
        self.assertEqual(calls["paste"], ["x", "x"])
        self.assertEqual(calls["enter"], [])


# ---------------------------------------------------------------------------
class TestClipboardSink(unittest.TestCase):
    def test_ok_and_busy(self):
        rec = Recorder()
        sink = ClipboardSink(ClipboardSinkConfig(), put=rec.stash, notify=rec.notify)
        result = sink.send("payload", CTX)
        self.assertTrue(result.ok)
        self.assertEqual(rec.stashed, ["payload"])
        # silent on success (a light action must not toast); the log records it
        self.assertFalse(rec.notified)

        busy = ClipboardSink(ClipboardSinkConfig(), put=lambda t: False,
                             notify=rec.notify)
        result = busy.send("payload", CTX)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure, "clipboard_busy")


# ---------------------------------------------------------------------------
class FakeSink:
    def __init__(self, name, ok=True, failure=None, accept=True, raises=False,
                 fallback=None):
        self.name = name
        self._ok = ok
        self._failure = failure
        self._accept = accept
        self._raises = raises
        self._fallback = fallback
        self.sent: list[str] = []

    def can_accept(self, ctx):
        return self._accept

    def send(self, text, ctx):
        if self._raises:
            raise RuntimeError("boom")
        self.sent.append(text)
        if self._ok:
            return SinkResult(sink=self.name, status="ok", message="fine")
        data = {"fallback": self._fallback} if self._fallback else {}
        return SinkResult(sink=self.name, status="failed", failure=self._failure,
                          message="nope", data=data)

    def verify(self):
        return None


class TestFallbackChain(unittest.TestCase):
    def setUp(self):
        self.cfg = sample_cfg()
        self.rec = Recorder()
        self.clip = ClipboardSink(ClipboardSinkConfig(), put=self.rec.stash,
                                  notify=self.rec.notify)

    def test_success_does_not_touch_the_clipboard(self):
        hermes = FakeSink("hermes")
        result = run_with_fallback(self.cfg, {"hermes": hermes, "clipboard": self.clip},
                                   "sink:hermes", "text", CTX, self.rec.notify)
        self.assertTrue(result.ok)
        self.assertEqual(self.rec.stashed, [])
        self.assertEqual(hermes.sent, ["text"])

    def test_unknown_sink_falls_back_and_reports(self):
        result = run_with_fallback(self.cfg, {"clipboard": self.clip}, "sink:nope",
                                   "text", CTX, self.rec.notify)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.rec.stashed, ["text"])
        self.assertEqual(result.data["fallback"], "clipboard")

    def test_disabled_sink_falls_back(self):
        raw = sample_cfg().operations  # keep defaults
        cfg = self.cfg
        cfg = cfg_mod.parse_config({
            "schema_version": 1,
            "global": {"operations": dict(raw)},
            "sinks": {"hermes": {"type": "hermes", "enabled": False}},
        })
        result = run_with_fallback(cfg, {"hermes": FakeSink("hermes"),
                                         "clipboard": self.clip},
                                   "sink:hermes", "text", CTX, self.rec.notify)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.rec.stashed, ["text"])

    def test_not_acceptable_falls_back(self):
        result = run_with_fallback(self.cfg, {"hermes": FakeSink("hermes", accept=False),
                                             "clipboard": self.clip},
                                   "sink:hermes", "text", CTX, self.rec.notify)
        self.assertEqual(self.rec.stashed, ["text"])
        self.assertEqual(result.data["reason"], "not_acceptable")

    def test_sink_that_raises_still_falls_back(self):
        result = run_with_fallback(self.cfg, {"hermes": FakeSink("hermes", raises=True),
                                             "clipboard": self.clip},
                                   "sink:hermes", "text", CTX, self.rec.notify)
        self.assertEqual(self.rec.stashed, ["text"])
        self.assertEqual(result.data["reason"], "internal_error")

    def test_sink_that_already_stashed_is_not_stashed_twice(self):
        hermes = FakeSink("hermes", ok=False, failure="timeout", fallback="clipboard")
        result = run_with_fallback(self.cfg, {"hermes": hermes, "clipboard": self.clip},
                                   "sink:hermes", "text", CTX, self.rec.notify)
        self.assertEqual(result.failure, "timeout")
        self.assertEqual(self.rec.stashed, [])          # no double stash

    def test_failure_kind_is_propagated(self):
        hermes = FakeSink("hermes", ok=False, failure="model_connection_failed")
        result = run_with_fallback(self.cfg, {"hermes": hermes, "clipboard": self.clip},
                                   "sink:hermes", "text", CTX, self.rec.notify)
        self.assertEqual(self.rec.stashed, ["text"])
        self.assertGreaterEqual(len(self.rec.notified), 1)   # user was told
        # the stashed fallback must not relabel the cause as internal_error:
        # main.py's sink_finished has to name the real failure kind
        self.assertEqual(result.failure, "model_connection_failed")
        self.assertEqual(result.data["reason"], "model_connection_failed")


# ---------------------------------------------------------------------------
class TestCaptureChain(unittest.TestCase):
    def test_first_successful_step_wins(self):
        steps = {"uia_focused": lambda: None,
                 "clipboard_select_copy": lambda: "from clipboard"}
        out = capture_text(CaptureConfig(), steps)
        self.assertEqual(out.text, "from clipboard")
        self.assertEqual(out.step, "clipboard_select_copy")

    def test_raising_step_is_treated_as_failure(self):
        def boom():
            raise RuntimeError("uia exploded")
        steps = {"uia_focused": boom, "clipboard_select_copy": lambda: "ok"}
        self.assertEqual(capture_text(CaptureConfig(), steps).text, "ok")

    def test_all_failed_returns_none(self):
        steps = {"uia_focused": lambda: None, "clipboard_select_copy": lambda: ""}
        out = capture_text(CaptureConfig(), steps)
        self.assertIsNone(out.text)


# ---------------------------------------------------------------------------
class TestSinkWorker(unittest.TestCase):
    def _worker(self, cfg, sinks, capture_text_value="captured"):
        finished: list[SinkResult] = []
        worker = SinkWorker(config_provider=lambda: cfg, sinks_provider=lambda: sinks,
                            on_finished=finished.append,
                            capture_fn=lambda: CaptureOutcome(text=capture_text_value,
                                                              step="uia_focused"))
        return worker, finished

    def test_job_captures_then_sends(self):
        cfg = sample_cfg()
        hermes = FakeSink("hermes")
        worker, finished = self._worker(cfg, {"hermes": hermes, "clipboard": FakeSink("clipboard")})
        self.assertTrue(worker.submit(SinkJob("sink:hermes", CTX_UNCAPTURED)))
        self.assertEqual(worker.drain(), 1)
        self.assertEqual(hermes.sent, ["captured"])
        self.assertEqual(len(finished), 1)

    def test_capture_failure_sends_nothing(self):
        cfg = sample_cfg()
        hermes = FakeSink("hermes")
        worker, finished = self._worker(cfg, {"hermes": hermes}, capture_text_value=None)
        worker.submit(SinkJob("sink:hermes", CTX_UNCAPTURED))
        worker.drain()
        self.assertEqual(hermes.sent, [])
        self.assertEqual(finished[0].failure, "capture_failed")
        self.assertEqual(finished[0].status, "skipped")

    def test_queue_full_drops_and_counts(self):
        cfg = sample_cfg()
        worker, _finished = self._worker(cfg, {"hermes": FakeSink("hermes")})
        worker._queue.maxsize = 1      # exercise the full path deterministically
        self.assertTrue(worker.submit(SinkJob("sink:hermes", CTX_UNCAPTURED)))
        self.assertFalse(worker.submit(SinkJob("sink:hermes", CTX_UNCAPTURED)))
        self.assertEqual(worker.dropped, 1)


if __name__ == "__main__":
    unittest.main()
