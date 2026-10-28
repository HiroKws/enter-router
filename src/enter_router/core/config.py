"""Configuration schema, defaults, parsing and validation (pure, stdlib only).

Format decision: JSON (config/enter-router.json on Windows, stdlib json —
no new dependency; PyYAML/TOML-writer would need one). Every user-tunable
number from the Enter policy lives here, not in code.

v2 model — TWO LAYERS (design.md §3.1, §4.2, §4.3):
    layer 1  ``global.operations``  operation key -> MEANING   (all apps, the user's habit)
    layer 2  ``profiles[].keymap``  meaning -> real key combo  (per app/site)
The v1 single table (``profiles[].enter_action`` + ``hotkeys[]``) no longer
exists: F keys and arbitrary combos are written directly in layer 1.

Validation policy (design.md §4.3) — reject loudly, never silently fill:
    * unknown top-level keys / wrong schema_version / bad types     -> ConfigError
    * a meaning outside MEANINGS (layer-1 value)                    -> ConfigError
    * a keymap key other than send/newline, or a ``win+`` combo      -> ConfigError
    * missing required layer-1 triggers, or a keymap missing
      send/newline, or a missing ``global`` section                 -> a warning
      (``Config.warnings``), the run continues with that operation
      resolving to ``pass`` — the user must be able to see it.

Location precedence:
    1. --config PATH argument
    %APPDATA%\\enter-router\\config.json   (created from DEFAULTS on first run)
    3. built-in DEFAULTS (used verbatim if the file is missing and creation failed)
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field, replace
from typing import Any

SCHEMA_VERSION = 1

# --- layer-1 vocabulary (design.md §3.1) -----------------------------------
# Operation keys that mean "Enter x N" and are therefore NOT combos.
COUNT_OPERATIONS = ("enter", "enter2", "enter3")
# Every other operation key is a combo normalized by _norm_combo().
REQUIRED_OPERATIONS = ("enter", "enter2", "enter3", "shift+enter", "ctrl+enter")
OPTIONAL_OPERATIONS = ("alt+enter",) + tuple(f"f{i}" for i in range(1, 13))

# Meanings a layer-1 value may take (design.md §3.1). Anything else is fatal.
MEANINGS = frozenset({"send", "newline", "hermes", "aichat", "clipboard", "none", "pass"})
# Meanings that MUST be resolved by layer 2 (profiles[].keymap).
KEY_MEANINGS = ("send", "newline")
# Meanings that resolve to a sink (meaning -> sinks key).
SINK_MEANINGS: dict[str, str] = {"hermes": "hermes", "aichat": "ai_chat", "clipboard": "clipboard"}

HOLD_MODES = frozenset({"deterministic", "fast"})

# ``global.ime_unknown_policy`` (card t_dd246e5a): what to do when the IME state
# cannot be read (three-valued probe: composing / not_composing / unknown).
#   "pass"    (default) let the Enter through untouched — the safe side, but the
#             app must SAY so (one notification + a log line), never be silent.
#   "process" treat unknown as not-composing and run the normal Enter policy.
IME_UNKNOWN_POLICIES = frozenset({"pass", "process"})
DEFAULT_IME_UNKNOWN_POLICY = "pass"

# --- built-in layer-1 map (design.md §4.2 / §4.4) --------------------------
# Enter=newline, 2x=send, 3x=hermes; shift/ctrl+enter carry a MEANING (not pass);
# alt+enter = explicit pass; F1..F12 = unused ("none", the key still reaches the app).
DEFAULT_OPERATIONS: dict[str, str] = {
    "enter": "newline",
    "enter2": "send",
    "enter3": "hermes",
    "shift+enter": "newline",
    "ctrl+enter": "send",
    "alt+enter": "pass",
    **{f"f{i}": "none" for i in range(1, 13)},
}


# ---------------------------------------------------------------------------
# dataclass model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyConfig:
    enabled: bool = True
    hold_mode: str = "deterministic"    # "deterministic" (hold+resolve) | "fast" (never hold)
    # 250ms is MEASURED for this operator, not chosen from a general rule:
    # scripts/measure_enter.py recorded 15 consecutive-Enter gaps (112..180ms)
    # plus 9 from a real session in the app log (96..201ms), and 5 deliberately
    # NON-consecutive ones (495..585ms). 1.25 x the slowest burst (201ms) = 251,
    # rounded to 250: 49ms above the slowest real double-tap and 245ms below the
    # fastest conscious pause (core/calibrate.py; design §3.6, docs/measurements.md).
    # The allowed range (120..600) is unchanged.
    hold_window_ms: int = 250           # 1st->2nd Enter deadline; = single-Enter max latency
    triple_enter_window_ms: int = 250   # 2nd->3rd deadline AND the 3rd->4th cancel window
    chatter_debounce_ms: int = 15       # same-vk keydowns closer than this w/o keyup = noise
    trigger_cooldown_ms: int = 500      # after a trigger, counts resolve as count-1
    # v2-g: the emergency stop is a BURST of one key, not a chord. In an
    # emergency a chord is hard to recall and impossible to type with one hand;
    # F12 is missing on some keyboards and sits in different places, so the
    # default is F1 (top-left, on every layout) pressed `panic_repeat` times
    # within triple_enter_window_ms. A single F1 still behaves normally.
    panic_key: str = "f1"
    panic_repeat: int = 3               # presses of panic_key = panic (2..6)


@dataclass(frozen=True)
class RegistrationConfig:
    """Hotkeys that register the CURRENT foreground app as a profile (v2-g).

    Pressing one of these writes a new entry into the live config file, so the
    two keymaps can be set up without hand-editing JSON: one registers the
    process alone (every window of that exe), one registers process + window
    title (just that window). Both are plain combo strings; validation requires
    ``alt`` + F1..F24 so the keys cannot collide with the Enter policy or with
    a shell shortcut.
    """

    process_key: str = "alt+f5"
    process_title_key: str = "alt+f6"


@dataclass(frozen=True)
class SafetyConfig:
    callback_budget_warn_ms: int = 5        # log WARN when one hook callback exceeds
    callback_overbudget_ms: int = 50        # a callback longer than this counts as "slow"
    callback_overbudget_streak: int = 5     # N slow callbacks in a row -> auto unhook
    callback_hard_limit_ms: int = 250       # a single callback longer than this -> auto unhook now
    watchdog_interval_ms: int = 1000
    watchdog_stale_ms: int = 3000           # no hook-thread heartbeat for this long -> self-exit


@dataclass(frozen=True)
class CaptureConfig:
    chain: tuple[str, ...] = ("uia_focused", "clipboard_select_copy")
    clipboard_restore: bool = True
    preview_chars: int = 60                 # notification preview length (never full text to log)


@dataclass(frozen=True)
class ClipboardSinkConfig:
    type: str = "clipboard"
    enabled: bool = True


@dataclass(frozen=True)
class AiChatSinkConfig:
    type: str = "ai_chat"
    enabled: bool = True
    method: str = "paste"                   # "paste" (clipboard+Ctrl+V) | "sendinput" (unicode)
    send_after_insert: bool = False         # v1 default: INSERT ONLY, never press Enter


@dataclass(frozen=True)
class HermesSinkConfig:
    type: str = "hermes"
    enabled: bool = True
    # "" = whatever distro WSL defaults to (wsl.exe without -d). Keeping the
    # default empty means the shipped configs carry no machine-specific name.
    wsl_distro: str = ""
    shell: str = "/bin/bash"
    timeout_sec: int = 30                   # measured success 3.01-4.44s (research 2-f)
    profile: str | None = None              # global -p <profile>; None = default profile
    query_file_stdin: bool = False          # False = measured `hermes -z` ARGV path (card §3);
    #                                         True = force the stdin form (long prompts use it anyway)
    # Code-level safety constants (design.md §5.3 / research R14), deliberately NOT
    # user-tunable and therefore not part of the §4.2 file schema.
    env_remove_prefixes: tuple[str, ...] = ("_HERMES_",)   # MUST cover _HERMES_GATEWAY
    env_remove: tuple[str, ...] = ("_HERMES_GATEWAY",)


SinkConfig = ClipboardSinkConfig | AiChatSinkConfig | HermesSinkConfig


@dataclass(frozen=True)
class MatchSpec:
    process: str = "*"      # glob, case-insensitive, matched against exe basename ("*.exe")
    title: str | None = None   # glob on window title; None = don't care
    url: str | None = None     # glob on browser URL; None = don't care.
    # NOTE: a profile that specifies url matches ONLY when the URL is readable
    # and matches; unreadable URL (None) => profile does NOT match (explicit
    # match requires evidence — see design.md 4.3).


@dataclass(frozen=True)
class Profile:
    id: str
    enabled: bool = True
    match: MatchSpec = field(default_factory=MatchSpec)
    # layer 2: meaning -> real key combo, values normalized by _norm_combo().
    # Only KEY_MEANINGS ("send"/"newline") may appear; a missing meaning resolves
    # to "pass" and is reported in Config.warnings at parse time (design §4.3).
    keymap: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    dir: str | None = None     # None => %APPDATA%\\enter-router\\logs (resolved on Windows side)
    max_bytes: int = 1_048_576
    backup_count: int = 3


@dataclass(frozen=True)
class Config:
    schema_version: int = SCHEMA_VERSION
    operations: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_OPERATIONS))
    # global.ime_unknown_policy: "pass" | "process" (see IME_UNKNOWN_POLICIES)
    ime_unknown_policy: str = DEFAULT_IME_UNKNOWN_POLICY
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    registration: RegistrationConfig = field(default_factory=RegistrationConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    sinks: dict[str, SinkConfig] = field(default_factory=dict)
    profiles: tuple[Profile, ...] = ()
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    warnings: tuple[str, ...] = ()   # non-fatal (missing required trigger, keymap gap, ...)


# ---------------------------------------------------------------------------
# defaults + sample
# ---------------------------------------------------------------------------

DEFAULT_SINKS: dict[str, SinkConfig] = {
    "clipboard": ClipboardSinkConfig(),
    "ai_chat": AiChatSinkConfig(),
    "hermes": HermesSinkConfig(),
}

# No catch-all profile ships (design §4.3/§4.4): an app without a profile passes
# every operation through (I8). Creating a profile IS the opt-in.
DEFAULT_PROFILES: tuple[Profile, ...] = (
    Profile(
        id="site-a",
        match=MatchSpec(process="firefox.exe", url="*site-a.example.com*"),
        keymap={"send": "ctrl+enter", "newline": "shift+enter"},
    ),
    Profile(
        id="site-b",
        match=MatchSpec(process="chrome.exe", url="*site-b.example.com*"),
        keymap={"send": "enter", "newline": "shift+enter"},
    ),
    Profile(
        id="excel",
        enabled=False,          # off by default: it would change Excel's own Enter behaviour
        match=MatchSpec(process="excel.exe"),
        keymap={"send": "enter", "newline": "alt+enter"},
    ),
)

DEFAULT_CONFIG = Config(
    operations=dict(DEFAULT_OPERATIONS),
    sinks=dict(DEFAULT_SINKS),
    profiles=DEFAULT_PROFILES,
)


# ---------------------------------------------------------------------------
# parsing / validation
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    """Fatal config problem: old config stays active, error is shown in tray/log."""


_POLICY_RANGES: dict[str, tuple[int, int]] = {
    "hold_window_ms": (120, 600),
    "triple_enter_window_ms": (120, 600),
    "chatter_debounce_ms": (0, 200),
    "trigger_cooldown_ms": (0, 5000),
    "panic_repeat": (2, 6),
}
_SAFETY_RANGES: dict[str, tuple[int, int]] = {
    "callback_budget_warn_ms": (1, 100),
    "callback_overbudget_ms": (5, 500),
    "callback_overbudget_streak": (1, 100),
    "callback_hard_limit_ms": (50, 900),
    "watchdog_interval_ms": (100, 10_000),
    "watchdog_stale_ms": (500, 10_000),
}


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


_MOD_ORDER = ("ctrl", "alt", "shift", "win")  # Windows canonical order (NOT alphabetical)


def _norm_combo(combo: str) -> str:
    """Normalize a hotkey combo: lowercase, modifiers in canonical order + key."""
    tokens = [t.strip().lower() for t in combo.split("+")]
    _require(all(tokens), f"invalid key combo: {combo!r}")
    mods = sorted((t for t in tokens if t in _MOD_ORDER), key=_MOD_ORDER.index)
    keys = [t for t in tokens if t not in _MOD_ORDER]
    _require(len(keys) == 1, f"combo must end with exactly one non-modifier key: {combo!r}")
    return "+".join(mods + keys)


def _check_no_win(combo: str, where: str) -> str:
    """Layer-2 values must never use the OS-reserved Win key (design §3.1/§4.3)."""
    _require("win" not in combo.split("+"),
             f"{where}: 'win+…' is reserved by the OS and not allowed ({combo!r})")
    return combo


_FUNCTION_KEYS = frozenset(f"f{i}" for i in range(1, 25))


def _parse_registration(raw: Any) -> RegistrationConfig:
    """registration.*: two alt+F-key combos that register the foreground app."""
    if raw is None:
        return RegistrationConfig()
    _require(isinstance(raw, dict), "registration must be an object")
    unknown = set(raw) - {"process_key", "process_title_key"}
    _require(not unknown, f"registration: unknown keys: {sorted(unknown)}")
    kwargs: dict[str, str] = {}
    for key in ("process_key", "process_title_key"):
        if key not in raw:
            continue
        combo = _norm_combo(str(raw[key]))
        tokens = combo.split("+")
        _require(tokens[:-1] == ["alt"] and tokens[-1] in _FUNCTION_KEYS,
                 f"registration.{key} must be alt+<F1..F24> (got {combo!r}): "
                 "plain F-keys belong to the Enter policy"
                 + " and unmodified function keys must stay usable elsewhere")
        kwargs[key] = combo
    cfg = replace(RegistrationConfig(), **kwargs)
    _require(cfg.process_key != cfg.process_title_key,
             "registration.process_key and registration.process_title_key must differ")
    return cfg


def _parse_operations(raw: dict[str, Any], warnings: list[str]) -> dict[str, str]:
    """Layer 1. Missing required triggers -> warning (never a silent default)."""
    glob_raw = raw.get("global")
    if glob_raw is None:
        warnings.append(
            "config has no 'global' section: the built-in default operation map was applied "
            "(enter=newline, enter2=send, enter3=hermes, shift+enter=newline, ctrl+enter=send, "
            "alt+enter=pass, f1..f12=none)")
        return dict(DEFAULT_OPERATIONS)
    _require(isinstance(glob_raw, dict), "global must be an object")
    unknown = set(glob_raw) - {"operations", "ime_unknown_policy"}
    _require(not unknown, f"global: unknown keys: {sorted(unknown)}")

    ops_raw = glob_raw.get("operations")
    if ops_raw is None:
        warnings.append(
            "'global' has no 'operations' map: the built-in default operation map was applied")
        return dict(DEFAULT_OPERATIONS)
    _require(isinstance(ops_raw, dict), "global.operations must be an object")

    operations: dict[str, str] = {}
    for key, meaning in ops_raw.items():
        _require(isinstance(key, str) and bool(key.strip()),
                 f"global.operations: keys must be non-empty strings (got {key!r})")
        op = key.strip() if key in COUNT_OPERATIONS else _norm_combo(key)
        where = f"global.operations[{op}]"
        _require(isinstance(meaning, str),
                 f"{where}: meaning must be a string (got {meaning!r})")
        _require(meaning in MEANINGS,
                 f"{where}: unknown meaning {meaning!r} "
                 f"(allowed: {', '.join(sorted(MEANINGS))})")
        _require(op not in operations, f"global.operations: duplicate operation {op!r}")
        operations[op] = meaning

    missing = [op for op in REQUIRED_OPERATIONS if op not in operations]
    if missing:
        warnings.append(
            f"global.operations is missing required trigger(s): {', '.join(missing)} "
            "— they pass through unchanged until you define them")
    return operations


def _parse_ime_unknown_policy(raw: dict[str, Any] | None) -> str:
    """``global.ime_unknown_policy`` — "pass" (default) or "process".

    Rejecting an unknown value is deliberate (design §4.3 "reject loudly"):
    a typo silently falling back to "pass" is exactly the "it does nothing and
    nobody knows why" state this card removes.
    """
    if not isinstance(raw, dict):
        return DEFAULT_IME_UNKNOWN_POLICY
    value = raw.get("ime_unknown_policy", DEFAULT_IME_UNKNOWN_POLICY)
    _require(isinstance(value, str) and value in IME_UNKNOWN_POLICIES,
             "global.ime_unknown_policy must be one of "
             f"{sorted(IME_UNKNOWN_POLICIES)} (got {value!r})")
    return value


def _parse_keymap(raw: Any, where: str, warnings: list[str]) -> dict[str, str]:
    """Layer 2. Keys are send/newline only; values are normalized combos, never win+."""
    _require(raw is None or isinstance(raw, dict),
             f"{where}.keymap must be an object ({{'send': 'ctrl+enter'}})")
    keymap: dict[str, str] = {}
    for meaning, combo in (raw or {}).items():
        _require(meaning in KEY_MEANINGS,
                 f"{where}.keymap: key must be 'send' or 'newline' (got {meaning!r}) — "
                 "layer 2 maps meanings to key combos only")
        _require(isinstance(combo, str) and bool(combo.strip()),
                 f"{where}.keymap[{meaning}] must be a non-empty combo string")
        keymap[meaning] = _check_no_win(_norm_combo(combo), f"{where}.keymap[{meaning}]")
    missing = [m for m in KEY_MEANINGS if m not in keymap]
    if missing:
        warnings.append(
            f"{where}.keymap is missing meaning(s): {', '.join(missing)} "
            "— they resolve to pass (nothing is sent)")
    return keymap


def _parse_sink(name: str, raw: dict[str, Any]) -> SinkConfig:
    stype = raw.get("type")
    _require(stype in ("clipboard", "ai_chat", "hermes"), f"sinks.{name}: unknown type {stype!r}")
    enabled = bool(raw.get("enabled", True))
    if stype == "clipboard":
        return ClipboardSinkConfig(enabled=enabled)
    if stype == "ai_chat":
        method = raw.get("method", "paste")
        _require(method in ("paste", "sendinput"), f"sinks.{name}: method must be 'paste' or 'sendinput'")
        return AiChatSinkConfig(enabled=enabled, method=method,
                                send_after_insert=bool(raw.get("send_after_insert", False)))
    timeout = raw.get("timeout_sec", 30)
    _require(isinstance(timeout, int) and 1 <= timeout <= 300,
             f"sinks.hermes: timeout_sec must be 1..300")
    profile = raw.get("profile")
    _require(profile is None or isinstance(profile, str), "sinks.hermes: profile must be string or null")
    return HermesSinkConfig(
        enabled=enabled,
        wsl_distro=str(raw.get("wsl_distro", "")),
        shell=str(raw.get("shell", "/bin/bash")),
        timeout_sec=timeout,
        profile=profile,
        query_file_stdin=bool(raw.get("query_file_stdin", False)),
        env_remove_prefixes=tuple(raw.get("env_remove_prefixes", ["_HERMES_"])),
        env_remove=tuple(raw.get("env_remove", ["_HERMES_GATEWAY"])),
    )


def _parse_profile(raw: dict[str, Any], idx: int, warnings: list[str]) -> Profile:
    where = f"profiles[{idx}]"
    _require(isinstance(raw, dict), f"{where} must be an object")
    pid = raw.get("id")
    _require(isinstance(pid, str) and pid, f"{where}: 'id' (non-empty string) is required")
    where = f"profiles[{idx}]({pid})"
    m = raw.get("match", {}) or {}
    _require(isinstance(m, dict), f"{where}: match must be an object")
    match = MatchSpec(
        process=str(m.get("process", "*")),
        title=m.get("title"),
        url=m.get("url"),
    )
    for field_name, val in (("title", match.title), ("url", match.url)):
        _require(val is None or isinstance(val, str), f"{where}: match.{field_name} must be string or null")

    obsolete = sorted(set(raw) & {"enter_action", "hotkeys"})
    if obsolete:
        warnings.append(
            f"{where}: {', '.join(obsolete)} is obsolete in v2 and was ignored "
            "— operations live in global.operations, per-app keys in keymap")

    keymap = _parse_keymap(raw.get("keymap"), where, warnings)
    return Profile(
        id=pid,
        enabled=bool(raw.get("enabled", True)),
        match=match,
        keymap=keymap,
    )


def parse_config(raw: dict[str, Any]) -> Config:
    """Validate a parsed JSON object into a Config. Raises ConfigError."""
    _require(isinstance(raw, dict), "config root must be an object")
    version = raw.get("schema_version")
    _require(version == SCHEMA_VERSION,
             f"schema_version must be {SCHEMA_VERSION} (got {version!r})")

    known = {"schema_version", "global", "policy", "registration", "safety", "capture",
             "sinks", "profiles", "logging"}
    unknown = set(raw) - known
    _require(not unknown, f"unknown top-level keys: {sorted(unknown)}")

    warnings: list[str] = []
    operations = _parse_operations(raw, warnings)
    ime_unknown_policy = _parse_ime_unknown_policy(raw.get("global"))

    pol_raw = raw.get("policy", {}) or {}
    _require(isinstance(pol_raw, dict), "policy must be an object")
    pol_kwargs: dict[str, Any] = {}
    for k, (lo, hi) in _POLICY_RANGES.items():
        if k in pol_raw:
            v = pol_raw[k]
            _require(isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi,
                     f"policy.{k} must be {lo}..{hi}")
            pol_kwargs[k] = v
    if "enabled" in pol_raw:
        pol_kwargs["enabled"] = bool(pol_raw["enabled"])
    if "hold_mode" in pol_raw:
        hold_mode = pol_raw["hold_mode"]
        _require(isinstance(hold_mode, str) and hold_mode in HOLD_MODES,
                 f"policy.hold_mode must be one of {sorted(HOLD_MODES)} (got {hold_mode!r})")
        pol_kwargs["hold_mode"] = hold_mode
    if "panic_key" in pol_raw:
        pol_kwargs["panic_key"] = _norm_combo(str(pol_raw["panic_key"]))
    if "double_enter_window_ms" in pol_raw:
        warnings.append(
            "policy.double_enter_window_ms is obsolete in v2 and was ignored "
            "— it was replaced by policy.hold_window_ms (default 250)")
    if "ime_unknown_policy" in pol_raw:
        # It lives in `global` (design §4.2). Someone will put it under `policy`
        # — say so instead of silently ignoring it and falling back to "pass".
        warnings.append(
            "policy.ime_unknown_policy is ignored — the setting lives in "
            "global.ime_unknown_policy (\"pass\" | \"process\"); the default "
            "\"pass\" was used")
    policy = replace(PolicyConfig(), **pol_kwargs)

    registration = _parse_registration(raw.get("registration"))

    saf_raw = raw.get("safety", {}) or {}
    _require(isinstance(saf_raw, dict), "safety must be an object")
    saf_kwargs: dict[str, Any] = {}
    for k, (lo, hi) in _SAFETY_RANGES.items():
        if k in saf_raw:
            v = saf_raw[k]
            _require(isinstance(v, int) and lo <= v <= hi, f"safety.{k} must be {lo}..{hi}")
            saf_kwargs[k] = v
    safety = replace(SafetyConfig(), **saf_kwargs)

    cap_raw = raw.get("capture", {}) or {}
    _require(isinstance(cap_raw, dict), "capture must be an object")
    chain = tuple(cap_raw.get("chain", list(CaptureConfig.chain)))
    _require(chain and all(c in ("uia_focused", "clipboard_select_copy") for c in chain),
             "capture.chain must be a non-empty list of 'uia_focused'/'clipboard_select_copy'")
    capture = CaptureConfig(
        chain=chain,
        clipboard_restore=bool(cap_raw.get("clipboard_restore", True)),
        preview_chars=int(cap_raw.get("preview_chars", 60)),
    )

    sinks_raw = raw.get("sinks", {}) or {}
    _require(isinstance(sinks_raw, dict), "sinks must be an object")
    sinks: dict[str, SinkConfig] = {}
    for name, sraw in sinks_raw.items():
        _require(isinstance(sraw, dict), f"sinks.{name} must be an object")
        sinks[name] = _parse_sink(name, sraw)
    for name, default in DEFAULT_SINKS.items():
        if name not in sinks:
            sinks[name] = default  # missing sink sections fall back to defaults

    profs_raw = raw.get("profiles")
    if profs_raw is None:
        # No profiles section = "every app passes through". This must NOT fall
        # back to the sample profiles: a live config that loses the section (or
        # a first-run file) would come back carrying dead example rules the user
        # has already deleted once (design §4.4: creating a profile IS the opt-in).
        profiles: tuple[Profile, ...] = ()
    else:
        _require(isinstance(profs_raw, list), "profiles must be a list")
        profiles = tuple(_parse_profile(p, i, warnings) for i, p in enumerate(profs_raw))
        ids = [p.id for p in profiles]
        _require(len(ids) == len(set(ids)), "profile ids must be unique")
        # An empty list is legal: no profile means every app passes through, which
        # is the safe default and what a freshly created config starts with.

    # A catch-all profile makes EVERY app "defined", which defeats the
    # "unmatched app passes through" safety rule (design §4.3) — warn, don't reject.
    for p in profiles:
        if p.enabled and p.match.process == "*" and p.match.title is None and p.match.url is None:
            warnings.append(
                f"profile '{p.id}' matches every app (catch-all): every app becomes defined, "
                "so the 'unmatched app passes' safety rule no longer protects you (design 4.3)")

    log_raw = raw.get("logging", {}) or {}
    _require(isinstance(log_raw, dict), "logging must be an object")
    logging_cfg = LoggingConfig(
        level=str(log_raw.get("level", "INFO")).upper(),
        dir=log_raw.get("dir"),
        max_bytes=int(log_raw.get("max_bytes", 1_048_576)),
        backup_count=int(log_raw.get("backup_count", 3)),
    )

    return Config(
        schema_version=SCHEMA_VERSION,
        operations=operations,
        ime_unknown_policy=ime_unknown_policy,
        policy=policy,
        registration=registration,
        safety=safety,
        capture=capture,
        sinks=sinks,
        profiles=profiles,
        logging=logging_cfg,
        warnings=tuple(warnings),
    )


def parse_config_text(text: str) -> Config:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON: {e}") from e
    return parse_config(raw)


def first_run_config_dict() -> dict[str, Any]:
    """The document written when no config file exists yet: defaults minus samples.

    The example rules (``site-a`` / ``site-b`` / ``excel``) belong to the shipped
    ``config/sample.config.json`` and to docs/design.md §4.2 — they are
    documentation, not a user's rules. A live config that starts with them makes
    the user delete three dead profiles on day one, and every regeneration of the
    file brings them back. So the first-run file carries **no profiles**: an app
    without a profile passes every operation through, and registering an app
    (Alt+F2 / Alt+F3) is the opt-in (design §4.4).
    """
    doc = default_config_dict()
    doc["profiles"] = []
    return doc


def default_config_dict() -> dict[str, Any]:
    """DEFAULT_CONFIG rendered as a JSON-ready dict (used for first-run file).

    The result is value-identical to the §4.2 block of docs/design.md, i.e. to
    config/sample.config.json: ``json.loads(sample) == default_config_dict()``
    (the sample uses a hand-wrapped layout, so re-serialising it is not byte
    identical — only the file itself is byte-identical to the design block).
    The generated first-run file and the shipped sample must never drift apart
    in content; a test asserts that.
    """
    cfg = DEFAULT_CONFIG
    return {
        "schema_version": cfg.schema_version,
        "global": {"operations": _jsonable(cfg.operations),
                   "ime_unknown_policy": cfg.ime_unknown_policy},
        "policy": _jsonable(cfg.policy),
        "registration": _jsonable(cfg.registration),
        "safety": _jsonable(cfg.safety),
        "capture": _jsonable(cfg.capture),
        "sinks": _jsonable(cfg.sinks),
        "profiles": _jsonable(cfg.profiles),
        "logging": _jsonable(cfg.logging),
    }


# Fields that are code-level constants, not part of the §4.2 file schema
# (parse_config re-applies their defaults when a file omits them).
_JSON_OMIT = frozenset({"warnings", "env_remove_prefixes", "env_remove"})


def _jsonable(obj: Any) -> Any:
    from dataclasses import fields as dc_fields, is_dataclass
    if isinstance(obj, MatchSpec):
        # Only the globs a profile actually narrows on appear in the file.
        out: dict[str, Any] = {"process": obj.process}
        if obj.title is not None:
            out["title"] = obj.title
        if obj.url is not None:
            out["url"] = obj.url
        return out
    if isinstance(obj, Profile):
        return {"id": obj.id, "enabled": obj.enabled,
                "match": _jsonable(obj.match), "keymap": dict(obj.keymap)}
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dc_fields(obj) if f.name not in _JSON_OMIT}
    if isinstance(obj, tuple):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj
