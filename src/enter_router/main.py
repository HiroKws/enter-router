"""Entry point. Runnable as a plain script from source (no build step).

    python  src\\enter_router\\main.py [--config PATH] [--dry-run] [--once] [--settings]
    pythonw src\\enter_router\\main.py        (resident, no console)

CLI surface is FROZEN (README and QA depend on it):
    --config PATH   use this config file (else %APPDATA%\\enter-router\\config.json)
    --dry-run       no hook is installed; replay the decision script and log every
                    would-be decision (safe trial; nothing is injected)
    --once          load config, print status + validation result + the
                    trigger->meaning->keys table, exit 0/2
    --settings      the settings window is the tray UI's job (frontend card); this
                    prints where to find it instead of opening a second, divergent UI
    --version       print version and exit

Exit codes: 0 ok, 2 config error, 70 watchdog self-exit.

This module only parses the CLI; the composition root lives in
``enter_router.app.main`` (config -> sinks -> policy -> hook -> watchdog).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python src/enter_router/main.py` from source
    # .../src/enter_router/main.py -> .../src  (so `import enter_router` resolves)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enter_router import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="enter-router", description=__doc__)
    p.add_argument("--config", metavar="PATH", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--settings", action="store_true")
    p.add_argument("--version", action="version", version=f"enter-router {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from enter_router.app.main import RunOptions, run
    return run(RunOptions(config=args.config, dry_run=args.dry_run,
                          once=args.once, settings=args.settings))


if __name__ == "__main__":
    raise SystemExit(main())
