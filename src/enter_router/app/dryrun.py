"""``--dry-run``: decide-only observation, no hook, no injection (design §3.9).

The card requires a mode QA can run **without installing a keyboard hook**. So
``--dry-run`` replays a deterministic script of realistic key sequences for every
profile in the config (plus one undefined app) through the real state machine and
prints one line per decision in the frozen §3.9 format:

    DRYRUN t=<t_ms> op=<操作キー|-> meaning=<意味|-> profile=<profile_id|->
           resolved=<pass|none|key:<combo>|sink:<name>>
           decision=<PASS|HOLD|IGNORE|TRIGGER|CANCEL|FLUSH|DELIVER|PANIC>
           swallow=<0|1> reason=<code>

then a single summary line:

    DRYRUN-SUMMARY handled=<n> swallowed=<n> injected_ignored=<n> unresolved=<n>

Nothing is sent, captured or typed, so it is safe on a live desktop. Because the
script is derived from the config file, editing the config changes the log —
that is the "設定ファイルだけで挙動を変えられる" evidence.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

from enter_router.core import routing
from enter_router.core.config import Config, MatchSpec, Profile
from enter_router.core.events import VK_RETURN, ForegroundInfo, KeyEvent
from enter_router.core.policy import Decision, EnterPolicyMachine, Outcome

log = logging.getLogger("enter_router.dryrun")

VK_A = 0x41
VK_I = 0x49
VK_F5 = 0x74
VK_F1 = 0x70
VK_F12 = 0x7B

STEP_MS = 400          # between independent interactions
INTRA_MS = 60          # between the two presses of a double/triple

#: Operation keys that are *triggers* (design §3.1). A typed letter is not a
#: trigger, so it must not be counted as "an operation without a meaning".
_TRIGGER_OP_RE = re.compile(r"^(?:(?:ctrl|alt|shift|win)\+)*(?:enter|enter2|enter3|f\d{1,2})$")


def _is_trigger_op(op: str) -> bool:
    return bool(op) and bool(_TRIGGER_OP_RE.match(op))


def _fg_from_spec(spec: MatchSpec, profile_id: str) -> ForegroundInfo:
    """Synthesize a plausible foreground window for a profile (dry-run only)."""
    process = spec.process if spec.process and spec.process != "*" else "unknown.exe"
    title = (spec.title or f"[{profile_id}]").replace("*", "")
    url = spec.url.replace("*", "") if spec.url else None
    return ForegroundInfo(process_name=process, window_title=title, url=url)


#: Process names tried in order for the "undefined app" target. The first one no
#: enabled profile matches wins, so the "unmatched app -> pass" evidence (I8) is
#: always real: a config may legitimately define notepad.exe
#: (config/notepad-qa.config.json), and then that name is NOT undefined anymore.
_UNDEFINED_CANDIDATES = ("notepad.exe", "enter-router-undefined.exe")


def undefined_fg(cfg: Config) -> ForegroundInfo:
    """A foreground window that no enabled profile matches (I8 evidence target)."""
    for name in _UNDEFINED_CANDIDATES:
        fg = ForegroundInfo(process_name=name, window_title="Untitled - Notepad")
        if routing.match_profile(cfg, fg) is None:
            return fg
    last = _UNDEFINED_CANDIDATES[-1]
    return ForegroundInfo(process_name=last, window_title="Untitled - Notepad")


@dataclass
class _Tally:
    handled: int = 0
    swallowed: int = 0
    injected_ignored: int = 0
    unresolved: int = 0


class _Runner:
    """Feeds synthetic events into one machine and writes DRYRUN lines."""

    def __init__(self, cfg: Config, fg: ForegroundInfo, emit: Callable[[str], None],
                 tally: _Tally) -> None:
        self.cfg = cfg
        self.fg = fg
        self.emit = emit
        self.tally = tally
        self.last_t = 0
        self.profile: Profile | None = routing.match_profile(cfg, fg)
        self.machine = EnterPolicyMachine(
            cfg.policy,
            lambda op: routing.resolve_operation(cfg, fg, op),
            meaning_of=lambda op: routing.lookup_meaning(cfg, op),
        )

    # -- logging ----------------------------------------------------------
    def _profile_id(self) -> str:
        return self.profile.id if self.profile else "-"

    def _line(self, t_ms: int, op: str, out: Outcome) -> None:
        self.last_t = max(self.last_t, t_ms)
        # For FLUSH/DELIVER/PANIC the decision acts on the PENDING operation, so
        # report that operation (not the interrupting key) — the reason code
        # already says what interrupted.
        if out.decision in (Decision.FLUSH, Decision.DELIVER, Decision.PANIC) and out.action:
            op = out.action
        op = op or (out.action or "")
        meaning = routing.lookup_meaning(self.cfg, op) if op and op != "-" else None
        if meaning is None and out.action and out.action != op:
            meaning = routing.lookup_meaning(self.cfg, out.action)
        if meaning is None and _is_trigger_op(op):
            # a trigger the user did not define (already warned at load time)
            self.tally.unresolved += 1
        self.tally.handled += 1
        if out.swallow:
            self.tally.swallowed += 1
        if out.reason == "injected":
            self.tally.injected_ignored += 1
        self.emit(
            f"DRYRUN t={t_ms} op={op or '-'} meaning={meaning or '-'} "
            f"profile={self._profile_id()} resolved={out.resolved or '-'} "
            f"decision={out.decision.name} swallow={1 if out.swallow else 0} "
            f"reason={out.reason}")

    # -- synthetic input --------------------------------------------------
    def key(self, t_ms: int, op_hint: str = "", vk: int = VK_RETURN, kind: str = "down",
            ctrl: bool = False, shift: bool = False, alt: bool = False,
            injected: bool = False) -> Outcome:
        # never let the script clock go backwards (timers may fire "ahead")
        t_ms = max(t_ms, self.last_t)
        ev = KeyEvent(vk=vk, kind=kind, t_ms=t_ms, ctrl=ctrl, shift=shift, alt=alt,
                      injected=injected)
        out = self.machine.on_key(ev)
        self._line(t_ms, op_hint, out)
        self._settle_sink(t_ms)
        return out

    def press(self, t_ms: int, op_hint: str = "", vk: int = VK_RETURN, **kw) -> Outcome:
        down = self.key(t_ms, op_hint, vk, "down", **kw)
        self.key(t_ms + 15, "-", vk, "up", **kw)
        return down

    def deadline(self, t_ms: int | None = None) -> Outcome:
        due = self.machine.deadline_ms
        if due is None:
            return Outcome(Decision.IGNORE, self.machine.state, False,
                           reason="no_deadline")
        out = self.machine.on_deadline(due)
        self._line(due, "", out)
        self._settle_sink(due)
        return out

    def interrupt(self, t_ms: int, kind: str = "mouse") -> Outcome:
        out = self.machine.on_interrupt(kind, t_ms)
        self._line(t_ms, "", out)
        self._settle_sink(t_ms)
        return out

    def _settle_sink(self, t_ms: int) -> None:
        """A sink would finish on the worker; here it finishes immediately."""
        if self.machine.state.name == "BUSY":
            out = self.machine.on_sink_done(t_ms + 1)
            self._line(t_ms + 1, "", out)

    # -- scenarios --------------------------------------------------------
    def _at(self) -> int:
        """Next interaction time: after everything logged so far."""
        return self.last_t + STEP_MS

    def run_all(self, base_t: int) -> int:
        # 1) single Enter (hold -> deadline delivery)
        t = max(base_t, self._at())
        self.press(t)
        self.deadline()
        # 2) double Enter within the window
        t = self._at()
        self.press(t)
        self.press(t + INTRA_MS)
        self.deadline()
        # 3) triple Enter within the window
        t = self._at()
        self.press(t)
        self.press(t + INTRA_MS)
        self.press(t + 2 * INTRA_MS)
        self.deadline()
        # 4) the meaningful combos + one unused trigger
        for op_hint, extra, vk in (("ctrl+enter", {"ctrl": True}, VK_RETURN),
                                   ("shift+enter", {"shift": True}, VK_RETURN),
                                   ("alt+enter", {"alt": True}, VK_RETURN),
                                   ("f5", {}, VK_F5)):
            self.press(self._at(), op_hint, vk, **extra)
        # 5) ordering: a character inside the hold window -> FLUSH, then the key passes
        #    (あ→Enter→い must become あ改行い, so the interrupt must arrive
        #    BEFORE the hold deadline — otherwise the timer would deliver first)
        t = self._at()
        self.key(t, "a", VK_A)
        self.key(t + 10, "-", VK_A, "up")
        hold_t = t + 20
        self.press(hold_t)
        self.key(hold_t + 60, "i", VK_I)
        self.key(hold_t + 70, "-", VK_I, "up")
        # 6) rule0: the injected echo of a synthesized combo is never processed
        echo_t = self._at()
        self.key(echo_t, "ctrl+enter", VK_RETURN, "down", ctrl=True, injected=True)
        self.key(echo_t + 10, "-", VK_RETURN, "up", ctrl=True, injected=True)
        # 7) 4 consecutive Enters: the burst is cancelled — NOTHING is delivered
        #    (a mistyped burst must never fire send/hermes, design §3.4)
        t = self._at()
        self.press(t)
        self.press(t + INTRA_MS)
        self.press(t + 2 * INTRA_MS)
        self.press(t + 3 * INTRA_MS)
        self.deadline()
        # 8) panic inside the hold window: F1 x3 (v2-g burst stop) must flush,
        #    not drop, and the first two presses must pass through as plain F1
        t = self._at()
        self.press(t)
        for i in range(3):
            self.press(t + 100 + i * INTRA_MS, "", VK_F1)
        return self.last_t


def sink_lines(cfg: Config) -> list[str]:
    """Which sink would be selected, and what would it send (card §6).

    Emitted as ``DRYRUN-SINK`` lines so the frozen ``DRYRUN t=...`` format
    (design §3.9) stays untouched. The sink objects, ``resolve_operation`` and
    ``can_accept`` are the production ones — only the capture is replaced by the
    marker ``payload=captured_text``, because --dry-run never reads the screen.
    """
    from enter_router.core.events import TriggerInfo
    from enter_router.core.sinks import SinkContext
    from enter_router.platform.sinks_win import build_sinks

    sinks = build_sinks(cfg, notify=lambda *a, **k: None)
    lines: list[str] = []
    for profile in cfg.profiles:
        if not profile.enabled:
            continue
        fg = _fg_from_spec(profile.match, profile.id)
        for op in RESOLUTION_OPS:
            resolved = routing.resolve_operation(cfg, fg, op)
            if not resolved.startswith("sink:"):
                continue
            name = resolved[5:]
            sink = sinks.get(name)
            ctx = SinkContext(
                trigger=TriggerInfo(kind="hotkey", count=1, profile_id=profile.id,
                                    t_ms=0, op=op,
                                    meaning=routing.lookup_meaning(cfg, op) or ""),
                foreground=fg, captured_text="captured_text")
            accept_fn = getattr(sink, "can_accept", None)
            accept = bool(accept_fn(ctx)) if callable(accept_fn) else False
            section = cfg.sinks.get(name)
            method, after, key, timeout = "-", "-", "-", "-"
            if name == "ai_chat":
                method = getattr(section, "method", "paste")
                after = "1" if getattr(section, "send_after_insert", False) else "0"
                key = sink.send_key(ctx) if hasattr(sink, "send_key") else "enter"   # type: ignore[union-attr]
            elif name == "hermes":
                method = "hermes-stdin" if getattr(section, "query_file_stdin", False) else "hermes-z"
                timeout = str(getattr(section, "timeout_sec", 30))
            elif name == "clipboard":
                method = "clipboard"
            lines.append(
                f"DRYRUN-SINK profile={profile.id} trigger={op} "
                f"resolved={resolved} sink={name} accept={1 if accept else 0} "
                f"payload=captured_text target={fg.process_name} "
                f"method={method} send_after_insert={after} send_key={key} "
                f"timeout_s={timeout}")
    return lines


def run(cfg: Config, emit: Callable[[str], None] = print,
        config_path: str = "") -> dict[str, int]:
    """Replay the scripts for every enabled profile + one undefined app."""
    tally = _Tally()
    emit(f"DRYRUN-START config={config_path or '-'} hold_mode={cfg.policy.hold_mode} "
         f"hold_window_ms={cfg.policy.hold_window_ms} "
         f"triple_enter_window_ms={cfg.policy.triple_enter_window_ms} "
         f"panic_key={cfg.policy.panic_key} panic_repeat={cfg.policy.panic_repeat} "
         f"register_process={cfg.registration.process_key} "
         f"register_process_title={cfg.registration.process_title_key}")
    for warning in cfg.warnings:
        emit(f"DRYRUN-WARN {warning}")
    for line in sink_lines(cfg):
        emit(line)

    targets: list[tuple[str, ForegroundInfo]] = []
    for profile in cfg.profiles:
        if not profile.enabled:
            continue
        targets.append((profile.id, _fg_from_spec(profile.match, profile.id)))
    targets.append(("undefined-app", undefined_fg(cfg)))

    t = 1000
    for profile_id, fg in targets:
        emit(f"DRYRUN-TARGET profile={profile_id} process={fg.process_name} "
             f"url={fg.url or '-'}")
        runner = _Runner(cfg, fg, emit, tally)
        t = runner.run_all(t) + STEP_MS

    emit(f"DRYRUN-SUMMARY handled={tally.handled} swallowed={tally.swallowed} "
         f"injected_ignored={tally.injected_ignored} unresolved={tally.unresolved}")
    return {"handled": tally.handled, "swallowed": tally.swallowed,
            "injected_ignored": tally.injected_ignored,
            "unresolved": tally.unresolved}


# ---------------------------------------------------------------------------
# --once: a static table of "trigger -> meaning -> emitted keys" per profile
# ---------------------------------------------------------------------------
RESOLUTION_OPS = ("enter", "enter2", "enter3", "shift+enter", "ctrl+enter",
                  "alt+enter", "f5")


def preview_lines(cfg: Config) -> list[str]:
    """One line per (profile, trigger): the 3 points QA compares by hand."""
    lines: list[str] = []
    targets: list[tuple[str, ForegroundInfo | None]] = [
        (p.id, _fg_from_spec(p.match, p.id)) for p in cfg.profiles if p.enabled
    ]
    targets.append(("undefined-app", undefined_fg(cfg)))
    for profile_id, fg in targets:
        for op in RESOLUTION_OPS:
            meaning = routing.lookup_meaning(cfg, op)
            resolved = routing.resolve_operation(cfg, fg, op)
            lines.append(f"PREVIEW profile={profile_id:<12} trigger={op:<12} "
                         f"meaning={meaning or '-':<9} resolved={resolved}")
    return lines


def once_report(cfg: Config, config_path: str, log_path: str = "") -> list[str]:
    """Human-readable startup report for --once (no hook, no injection)."""
    lines = [f"config: {config_path}",
             f"log:    {log_path or '(default)'}",
             f"schema_version: {cfg.schema_version}",
             f"hold_mode: {cfg.policy.hold_mode} (hold_window_ms="
             f"{cfg.policy.hold_window_ms}, triple_enter_window_ms="
             f"{cfg.policy.triple_enter_window_ms}, "
             f"trigger_cooldown_ms={cfg.policy.trigger_cooldown_ms})",
             f"panic_key: {cfg.policy.panic_key} x{cfg.policy.panic_repeat} (burst)",
             f"registration: {cfg.registration.process_key} = process only, "
             f"{cfg.registration.process_title_key} = process + window title",
             f"profiles: {', '.join(p.id + ('' if p.enabled else '(disabled)') for p in cfg.profiles) or '-'}",
             f"warnings: {len(cfg.warnings)}"]
    lines += [f"  WARN {w}" for w in cfg.warnings]
    lines.append("layer1 (global.operations):")
    for op in sorted(cfg.operations):
        lines.append(f"  {op:<12} -> {cfg.operations[op]}")
    lines += preview_lines(cfg)
    return lines


def log_summary(result: dict[str, int]) -> None:
    log.info("DRYRUN-SUMMARY handled=%s swallowed=%s injected_ignored=%s unresolved=%s",
             result["handled"], result["swallowed"], result["injected_ignored"],
             result["unresolved"])
