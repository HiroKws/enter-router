"""HermesSink spawn adapter (Windows). Research §2-f / R14.

Invocation contract — measured on the target machine (QA-B t_0f0f792f and the
fix card t_69691c23); the earlier "measured PASS" claim only ever held for the
WSL-internal direct form, not for the Windows ``bash -c`` hop:

* The host decides the transport; the executable is always the **absolute path**
  ``~/.local/bin/hermes`` (card t_4674d88c). Never the bare command
  name: ``~/.local/bin`` is only on PATH for a login shell and, when the tray
  process spawns ``wsl.exe``, not at all — the default config died with
  ``exec: hermes: not found`` / exit 127. There is deliberately no
  ``command -v`` resolver and no config knob for this (fixed path by design).
  - inside WSL:  ``~/.local/bin/hermes -z "<text>"`` executed directly
    (no wsl.exe hop);
  - from Windows: ``wsl.exe -d <distro> --exec /bin/bash -c 'exec ~/.local/bin/hermes -z "$1"'``
    with the prompt as ``$1`` (never interpreted by a shell).
* ``--exec`` (not the legacy ``--`` separator) is mandatory, measured on this
  machine (QA-B t_0f0f792f + the probes in the fix card):

  - with ``--`` the interop layer runs the whole tail through a shell. The
    script's ``$1`` is expanded *there* (to the empty string), so the trailing
    ``erouter <prompt>`` argv elements never reach bash: ``printf %s "$1"``
    prints nothing and ``$#`` is 0. Symptom: ``hermes -z ""``.
  - the same layer expands the prompt itself when it rides on the command line:
    ``/bin/echo "a $HOME b `id` c"`` executed ``id``. Sending captured text that
    way is prompt injection.
  - with ``--exec`` the argv is handed over verbatim (``$1`` arrives byte-exact,
    ``$HOME``/backticks stay literal), so the "never interpreted by a shell"
    promise in design §5.3 finally holds.

* The shell is a plain ``-c``: with the absolute path no PATH lookup happens,
  so the login profile is not needed (``-lc`` only existed to put
  ``~/.local/bin`` on PATH, which made the spawn depend on someone's dotfiles).
  ``bash -c 'command -v hermes'`` exits 1 on this machine while the fixed path
  works for both shell flavours — that asymmetry is what exit 127 came from.
* Primary = the ``-z`` ARGV form above (this is the path the card and the
  measurement name). Long prompts on Windows (>``ARGV_FALLBACK_MAX_CHARS``) use
  the stdin form instead of the command line:
      wsl.exe -d <distro> --exec /bin/bash -c 'exec ~/.local/bin/hermes chat --query-file -'
  and an ``empty_response`` from the ARGV form is retried once through it.
* Profile: a GLOBAL option — ``hermes -p <profile> ...`` (the ``chat``
  subcommand has no --profile).
* ENV (research R14, non-negotiable): the child env is rebuilt and every key
  matching ``env_remove_prefixes`` (default ``_HERMES_``) plus ``env_remove``
  (``_HERMES_GATEWAY``) is dropped. Inheriting ``_HERMES_GATEWAY`` makes the
  nested CLI fail with a bare ``invalid_params`` after ~2s and **no response**
  while still exiting — the worst silent failure. That is exactly why success is
  defined as ``exit == 0 AND stdout is not empty`` and never as "the CLI exited".
* Timeout: ``cfg.timeout_sec`` (default 30; measured success 3.01-4.44s). On
  timeout the whole process tree is killed.
* Failure taxonomy (ISink): cli_spawn_failed / model_connection_failed /
  timeout / empty_response. ``cli_spawn_failed`` owns the "the binary could not
  be executed" family: an OSError from Popen, and exit 126/127 (or a stderr
  saying ``not found`` / ``No such file or directory``) — those used to be
  reported as ``model_connection_failed``, which blamed the model for a broken
  spawn. The caller then falls back to ClipboardSink + notify
  + log with the PROMPT as the stashed text (the user never loses their input).
* v2 extension point: a ``local-[0-9a-f]{64}`` session id found in the output is
  reported in ``HermesSpawnResult.session_id``; v1 only logs it.

The pure pieces (command building, env filtering, output classification) are at
the top and unit-tested on Linux (tests/test_platform_pure.py).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass

from enter_router.core.config import HermesSinkConfig

log = logging.getLogger("enter_router.hermes_cli")

WSL_EXE = "wsl.exe"
#: Run the command WITHOUT the interop shell. ``--`` would let the interop layer
#: evaluate the tail: ``$1`` is expanded there (losing the prompt) and any ``$``/
#: backtick in the prompt executes (see the module docstring).
WSL_EXEC = "--exec"
# The Hermes CLI lives in the WSL user's home. It is deliberately written with
# $HOME (not an absolute path) so this file contains no machine-specific path:
# the shell forms expand it inside WSL, and ``hermes_bin()`` expands it here for
# the WSL-internal direct form.
HERMES_BIN = "$HOME/.local/bin/hermes"
#: exit codes a shell uses when it cannot run the command / the file is not
#: executable. Both mean "spawn failed", never "the model failed".
EXE_MISSING_RE = re.compile(r"not found|no such file or directory", re.IGNORECASE)
EXE_MISSING_EXIT = frozenset({126, 127})
SESSION_ID_RE = re.compile(r"local-[0-9a-f]{64}")
ARGV_FALLBACK_MAX_CHARS = 2000
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class HermesSpawnResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    elapsed_ms: int = 0
    failure: str | None = None       # ISink FailureKind value
    session_id: str | None = None    # v1: log-only


# --- pure helpers (Linux-testable) -----------------------------------------
def is_windows_host() -> bool:
    """True on Windows; False inside WSL/Linux where ``hermes`` runs directly."""
    return os.name == "nt"


def resolve_wsl_exe() -> str:
    """``wsl.exe`` resolved for the current host.

    On PATH first; System32 as the fallback because a tray/PythonW process can
    inherit a stripped PATH. From WSL the card names
    ``/mnt/c/Windows/System32/wsl.exe``; that path is only relevant when a Linux
    process wants to cross into Windows, which this module never does (the WSL
    branch calls ``hermes`` directly).
    """
    if not is_windows_host():
        return WSL_EXE
    found = shutil.which(WSL_EXE)
    if found:
        return found
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", WSL_EXE)
    return system32 if os.path.exists(system32) else WSL_EXE


def hermes_bin() -> str:
    """The CLI path expanded for THIS process (used by the WSL-internal form)."""
    return os.path.expanduser("~/.local/bin/hermes")


def hermes_exe(cfg: HermesSinkConfig) -> str:
    """Shell form of the fixed path (+ ``-p <profile>``; profile is GLOBAL).

    The path never comes from PATH (card t_4674d88c): a bare ``hermes`` is only
    resolvable from a login shell, which is exactly what broke the default
    config with ``exec: hermes: not found`` (exit 127). ``$HOME`` is expanded by
    the shell that wsl.exe starts, so the same config works for any user.
    """
    return f"{HERMES_BIN} -p {cfg.profile}" if cfg.profile else HERMES_BIN


def wsl_argv(cfg: HermesSinkConfig, script: str, *extra: str) -> list[str]:
    """``wsl.exe [-d <distro>] --exec <shell> -c <script> [argv...]``.

    An EMPTY ``wsl_distro`` means "whatever distro WSL defaults to" and simply
    omits ``-d`` — the shipped default, so the public configs carry no
    machine-specific distro name.
    """
    argv = [WSL_EXE]
    if cfg.wsl_distro:
        argv += ["-d", cfg.wsl_distro]
    return argv + [WSL_EXEC, cfg.shell, "-c", script, *extra]


def build_stdin_argv(cfg: HermesSinkConfig) -> list[str]:
    """Windows stdin form: no prompt bytes ever reach the command line (long prompts)."""
    script = f"exec {hermes_exe(cfg)} chat --query-file -"
    return wsl_argv(cfg, script)


#: Historical name (design §5.3 called the stdin form the primary path).
build_primary_argv = build_stdin_argv


def build_direct_stdin_argv(cfg: HermesSinkConfig) -> list[str]:
    """WSL stdin form: same reader, no wsl.exe hop."""
    exe = [hermes_bin(), "-p", cfg.profile] if cfg.profile else [hermes_bin()]
    return exe + ["chat", "--query-file", "-"]


def stdin_argv(cfg: HermesSinkConfig) -> list[str]:
    """Host-appropriate stdin form."""
    return build_stdin_argv(cfg) if is_windows_host() else build_direct_stdin_argv(cfg)


def build_argv_fallback(cfg: HermesSinkConfig) -> list[str]:
    """Windows ARGV form: the prompt is passed as $1, never shell-interpreted.

    Three measured details make this reachable and safe on the target machine
    (QA-B item 2 + the probes recorded on the fix card):

    * ``--exec`` hands the argv over verbatim. With the legacy ``--`` separator
      the interop shell expands ``$1`` itself, so bash sees ``$# == 0`` and
      hermes is started with an empty prompt.
    * the executable is the absolute path, so ``-c`` is enough — no PATH lookup
      and no dependency on the login profile (the old ``-lc`` + bare ``hermes``
      pair is what exited 127 with ``exec: hermes: not found``).
    * the prompt rides as a separate argv element (``erouter`` is ``$0``), which
      is exactly why it is never parsed by a shell.

    The caller appends the prompt as the last element.
    """
    script = f'exec {hermes_exe(cfg)} -z "$1"'
    return wsl_argv(cfg, script, "erouter")


def build_direct_argv(cfg: HermesSinkConfig, text: str) -> list[str]:
    """WSL ARGV form: ``~/.local/bin/hermes -z "<text>"`` (no wsl.exe hop).

    ARGV means no shell sees the text, so quotes/newlines/``$`` are literal.
    """
    exe = [hermes_bin(), "-p", cfg.profile] if cfg.profile else [hermes_bin()]
    return exe + ["-z", text]


def verify_script() -> str:
    """Existence check for the fixed path — ``command -v`` is deliberately gone."""
    return f'test -x "{HERMES_BIN}" && echo "{HERMES_BIN}"'


def build_verify_argv(cfg: HermesSinkConfig) -> list[str]:
    return wsl_argv(cfg, verify_script())


def child_env(cfg: HermesSinkConfig, base: dict[str, str] | None = None) -> dict[str, str]:
    """Rebuild the child environment without any ``_HERMES_*`` variable.

    Deleting ``_HERMES_GATEWAY`` is mandatory (research R14): the nested CLI then
    answers instead of failing with invalid_params.
    """
    source = dict(os.environ if base is None else base)
    dropped_prefixes = tuple(cfg.env_remove_prefixes)
    dropped = set(cfg.env_remove)
    clean: dict[str, str] = {}
    for key, value in source.items():
        if key in dropped:
            continue
        if any(key.startswith(p) for p in dropped_prefixes):
            continue
        clean[key] = value
    return clean


def find_session_id(*texts: str) -> str | None:
    for text in texts:
        if not text:
            continue
        match = SESSION_ID_RE.search(text)
        if match:
            return match.group(0)
    return None


def classify(exit_code: int | None, stdout: str, stderr: str,
             timed_out: bool = False, spawn_error: str | None = None) -> tuple[bool, str | None]:
    """(ok, failure) — success means "we actually got response text"."""
    if spawn_error is not None:
        return False, "cli_spawn_failed"
    if timed_out:
        return False, "timeout"
    if exit_code is None:
        return False, "cli_spawn_failed"
    if exit_code != 0:
        # 126/127 and "not found" mean the executable could not be run. Calling
        # that model_connection_failed blamed the model for our own bad argv.
        if exit_code in EXE_MISSING_EXIT or EXE_MISSING_RE.search(stderr or ""):
            return False, "cli_spawn_failed"
        return False, "model_connection_failed"
    if not (stdout or "").strip():
        return False, "empty_response"
    return True, None


# --- process runner --------------------------------------------------------
def _run(argv: list[str], timeout_sec: int, env: dict[str, str],
         stdin_bytes: bytes | None = None) -> tuple[int | None, str, str, bool, str | None, int]:
    """Run a child; returns (exit, stdout, stderr, timed_out, spawn_error, elapsed_ms).

    Host-neutral on purpose: ``creationflags`` only exists on Windows, and a
    ``start_new_session`` process group only makes sense on POSIX (so a timeout
    can kill hermes *and* whatever it started).
    """
    started = time.monotonic()
    argv = list(argv)
    if argv and argv[0] == WSL_EXE:
        argv[0] = resolve_wsl_exe()
    kwargs: dict[str, object] = {}
    if is_windows_host():
        kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, **kwargs,  # type: ignore[arg-type]
        )
    except OSError as e:
        return None, "", str(e), False, f"spawn failed: {e}", int((time.monotonic() - started) * 1000)

    timed_out = False
    try:
        out, err = proc.communicate(input=stdin_bytes, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=5)
        except BaseException:
            out, err = b"", b""
    elapsed = int((time.monotonic() - started) * 1000)
    return (proc.returncode, _decode(out), _decode(err), timed_out, None, elapsed)


def _decode(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


def _kill_tree(pid: int) -> None:
    """Kill the child *and* the process tree it started (design §5.3)."""
    if not is_windows_host():
        # _run starts the child in its own session, so the whole group is ours
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return
        except BaseException:
            try:
                os.kill(pid, signal.SIGKILL)
            except BaseException:
                pass
            return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=10,
                       creationflags=CREATE_NO_WINDOW)
    except BaseException:
        pass


def send_prompt(text: str, cfg: HermesSinkConfig) -> HermesSpawnResult:
    """Blocking (worker thread only). Never raises.

    Success is ``exit == 0 AND stdout is not empty`` (classify), never "the CLI
    exited" — inheriting ``_HERMES_GATEWAY`` yields exit≈2s with no response.
    """
    try:
        env = child_env(cfg)          # _HERMES_GATEWAY dropped on BOTH hosts
        prompt = text or ""
        windows = is_windows_host()
        # long prompts must not ride on a Windows command line
        use_stdin = bool(cfg.query_file_stdin) or (windows and len(prompt) > ARGV_FALLBACK_MAX_CHARS)
        if not use_stdin:
            argv = (build_argv_fallback(cfg) + [prompt]) if windows else build_direct_argv(cfg, prompt)
            log.debug("hermes path=argv host=%s chars=%d", "windows" if windows else "wsl", len(prompt))
            code, out, err, timed_out, spawn_err, elapsed = _run(argv, cfg.timeout_sec, env)
            ok, failure = classify(code, out, err, timed_out, spawn_err)
            if failure == "empty_response" and not timed_out:
                # exit 0 but silence: retry once through the stdin form
                code2, out2, err2, to2, se2, el2 = _run(
                    stdin_argv(cfg), cfg.timeout_sec, env,
                    stdin_bytes=prompt.encode("utf-8"))
                ok2, failure2 = classify(code2, out2, err2, to2, se2)
                log.debug("hermes path=stdin-retry ok=%s failure=%s", ok2, failure2)
                if ok2:
                    return HermesSpawnResult(
                        ok=True, stdout=out2, stderr=err2, exit_code=code2,
                        elapsed_ms=elapsed + el2,
                        session_id=find_session_id(out2, err2))
                out, err, code, elapsed = out2, err2, code2, elapsed + el2
                ok, failure = ok2, failure2
        else:
            log.debug("hermes path=stdin host=%s chars=%d", "windows" if windows else "wsl", len(prompt))
            code, out, err, timed_out, spawn_err, elapsed = _run(
                stdin_argv(cfg), cfg.timeout_sec, env,
                stdin_bytes=prompt.encode("utf-8"))
            ok, failure = classify(code, out, err, timed_out, spawn_err)
        return HermesSpawnResult(
            ok=ok, stdout=out, stderr=err, exit_code=code, elapsed_ms=elapsed,
            failure=failure, session_id=find_session_id(out, err))
    except BaseException:
        # never raise into the worker loop; the caller must see a classified failure
        return HermesSpawnResult(ok=False, failure="internal_error",
                                 stderr="unexpected exception in send_prompt")


def verify(cfg: HermesSinkConfig) -> tuple[bool, str]:
    """Reachability: does the fixed absolute path exist (and is it executable)?

    No PATH resolution any more (card t_4674d88c) — ``command -v hermes`` said
    "not found" from a non-login shell even though the binary was right there.
    """
    try:
        if is_windows_host():
            argv = build_verify_argv(cfg)
        else:
            argv = [cfg.shell or "/bin/bash", "-c", verify_script()]
        code, out, err, timed_out, spawn_err, elapsed = _run(
            argv, min(cfg.timeout_sec, 10), child_env(cfg))
        if spawn_err:
            return False, spawn_err
        if timed_out:
            return False, "timeout"
        if code != 0:
            return False, f"exit={code} {err.strip()[:200]}"
        path = out.strip().splitlines()[0] if out.strip() else ""
        if not path:
            return False, f"not executable: {hermes_bin()}"
        return True, f"{path} ({elapsed}ms)"
    except BaseException as e:
        return False, f"verify failed: {e}"
