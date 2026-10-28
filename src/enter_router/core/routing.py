"""Profile matching, sink selection and the TWO-LAYER resolution (pure, stdlib only).

Resolution (design.md §2 / §3.1) — two pure functions, no I/O, no logging:

    lookup_meaning(config, op_key)              # layer 1: operation -> meaning
    resolve_meaning(config, profile, meaning)   # layer 2: meaning -> resolved action

Resolved action vocabulary (frozen, policy.py RESOLVED_*):
    "pass" | "none" | "key:<combo>" | "sink:<name>"
`pass` from an UNDEFINED app (match_profile -> None) is the only implicit pass
in the system: a missing profile must never fire anything (I8, 誤爆防止).
`send`/`newline` are key meanings and MUST be resolved by the profile's keymap;
a profile that does not define one falls back to `pass` and is reported at
parse time (design §4.3), so the resolver itself stays silent.

Profile matching semantics (design.md 4.3) — TWO-PASS, order-forgiving:
- Pass 1: SPECIFIC profiles (anything narrowing process != catch-all, i.e. a
  profile that sets title or url, or a non-"*" process), evaluated in list
  order; first match wins.
- Pass 2: CATCH-ALL profiles (process == "*" AND title AND url both unset),
  in list order; first match wins. They are the fallback bowl.
=> A catch-all never shadows a specific profile, no matter where it is
   listed. Disabled profiles are skipped entirely.
- process: case-insensitive glob against the exe basename ("firefox.exe").
- title:   case-insensitive glob against the window title; None = don't care.
- url:     case-insensitive glob against the browser URL. A profile that
  specifies url NEVER matches when the URL is unreadable (None): an explicit
  match requires evidence, never a guess.
"""
from __future__ import annotations

import fnmatch
import os

from .config import SINK_MEANINGS, Config, MatchSpec, Profile
from .events import ForegroundInfo
from .sinks import ISink


def _glob(value: str, pattern: str) -> bool:
    return fnmatch.fnmatch(value.lower(), pattern.lower())


def _is_catchall(spec: MatchSpec) -> bool:
    return spec.process == "*" and spec.title is None and spec.url is None


def match_spec_matches(spec: MatchSpec, fg: ForegroundInfo) -> bool:
    if not _glob(fg.process_name or "", spec.process):
        return False
    if spec.title is not None and not _glob(fg.window_title or "", spec.title):
        return False
    if spec.url is not None:
        if fg.url is None:
            return False  # explicit URL match requires a readable URL
        if not _glob(fg.url, spec.url):
            return False
    return True


def match_profile(config: Config, fg: ForegroundInfo) -> Profile | None:
    """Specific profiles first (list order), then catch-alls (list order)."""
    enabled = [p for p in config.profiles if p.enabled]
    ordered = ([p for p in enabled if not _is_catchall(p.match)]
               + [p for p in enabled if _is_catchall(p.match)])
    for profile in ordered:
        if match_spec_matches(profile.match, fg):
            return profile
    return None


def literal_glob(text: str) -> str:
    """Escape glob metacharacters so ``text`` matches only itself.

    Used when the registration hotkey stores a window title as a match pattern:
    a title is data, not a pattern, so ``[``/``*``/``?`` in it must not turn into
    wildcards (a title of "Report [draft]" would otherwise match "Report d").
    ``fnmatch`` has no escape, so each metacharacter is wrapped in a one-element
    character class — the documented idiom (``[[]`` matches a literal ``[``).
    """
    out: list[str] = []
    for ch in text:
        if ch in "*?[":
            out.append(f"[{ch}]")
        else:
            out.append(ch)
    return "".join(out)


def base_name(process_path: str) -> str:
    """'C:/x/Firefox.exe' -> 'firefox.exe' (used by the Windows adapter, kept
    here because the glob semantics are pure)."""
    return os.path.basename(process_path.replace("\\", "/"))


def select_sink(config: Config, sinks: dict[str, ISink], action: str) -> ISink | None:
    """Resolve a 'sink:<name>' action to a sink instance.

    Returns None for unknown/disabled sinks — the caller must then fall back
    (design.md 5.4) and log; it must not silently do nothing.
    """
    if not action.startswith("sink:"):
        return None
    name = action[5:]
    sink = sinks.get(name)
    if sink is None:
        return None
    section = config.sinks.get(name)
    if section is not None and not getattr(section, "enabled", True):
        return None
    return sink


# ---------------------------------------------------------------------------
# two-layer resolution (design.md §2 / §3.1) — pure, called per operation
# ---------------------------------------------------------------------------

_RESOLVED_PASS = "pass"
_RESOLVED_NONE = "none"


def lookup_meaning(config: Config, op_key: str) -> str | None:
    """Layer 1: normalized operation key -> meaning.

    Returns None when the trigger is not defined (a required trigger that was
    missing at load time is already reported in config.warnings; its behaviour
    is "pass"). Callers must treat None as pass.
    """
    return config.operations.get(op_key)


def resolve_meaning(config: Config, profile: Profile | None, meaning: str | None) -> str:
    """Layer 2: meaning -> resolved action ("pass"|"none"|"key:<combo>"|"sink:<name>").

    - profile is None (no profile matched = undefined app) -> always "pass" (I8).
    - meaning in {send, newline}  -> "key:<combo>" from profile.keymap; an
      undefined meaning in this keymap is "pass" (warning already emitted by
      config.parse_config — design §4.3; this function never logs).
    - meaning in {hermes, aichat, clipboard} -> "sink:<name>" (sinks key:
      hermes / ai_chat / clipboard). Sink meanings only fire for a matched
      profile, which is checked above.
    - meaning in {pass, none} -> unchanged (`none` never swallows the key).
    """
    if profile is None or meaning is None:
        return _RESOLVED_PASS
    if meaning in ("send", "newline"):
        combo = profile.keymap.get(meaning)
        return f"key:{combo}" if combo else _RESOLVED_PASS
    sink_name = SINK_MEANINGS.get(meaning)
    if sink_name is not None:
        return f"sink:{sink_name}"
    if meaning in (_RESOLVED_PASS, _RESOLVED_NONE):
        return meaning
    return _RESOLVED_PASS   # unreachable for a validated config (vocabulary is enforced)


def resolve_operation(config: Config, fg: ForegroundInfo | None, op_key: str) -> str:
    """Convenience binding used by the hook pipeline (design §3.2 steps ⑤-⑥).

    Both layers in one call: lookup the meaning, match the profile for the
    foreground window, then resolve. Kept here so callers never re-implement
    the "no profile => pass" rule.
    """
    profile = match_profile(config, fg) if fg is not None else None
    return resolve_meaning(config, profile, lookup_meaning(config, op_key))
