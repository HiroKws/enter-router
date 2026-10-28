"""Burst-window calibration from measured Enter intervals (pure, stdlib only).

Why this module exists: the burst windows (``policy.hold_window_ms`` for the
1st->2nd gap and ``policy.triple_enter_window_ms`` for the 2nd->3rd gap) decide
whether two or three Enter presses are "one action" or separate actions. Those
numbers are a property of the OPERATOR'S HAND, not of the design, so they are
measured (``scripts/measure_enter.py``) and then reduced here.

Two samples come out of the measurement:

``burst``      gaps the operator produced while deliberately pressing
               consecutively (fast). The window must be >= the slowest of them,
               otherwise a real double-tap is torn into two single presses.
``separated``  gaps the operator produced while deliberately NOT pressing
               consecutively. The window must stay below the fastest of them,
               otherwise two separate actions are merged into one send.

Recommendation rule (deterministic, no clock, no I/O):

    want    = burst_max * burst_factor      (25% headroom over the slowest burst)
    ceiling = separated_min * separated_factor   (20% below the fastest pause)
    window  = want                 when want <= ceiling        (clean clusters)
            = (burst_max + separated_min) / 2   when the clusters still separate
            = want, flagged ``ok=False``  when they overlap (too tight to trust)

rounded to ``step_ms`` and clamped to the configured range (120..600, the same
range ``core/config.py`` validates). Every value the caller needs to explain the
decision (both cluster ends and the chosen window) is in the result, so the log
and the docs can show the measurement instead of the conclusion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

#: Range accepted by ``core/config.py`` for the two burst windows.
WINDOW_LOW_MS = 120
WINDOW_HIGH_MS = 600

#: Headroom the window keeps above the slowest measured burst gap.
BURST_FACTOR = 1.25
#: Fraction of the fastest measured pause the window must stay below.
SEPARATED_FACTOR = 0.80


@dataclass(frozen=True)
class Calibration:
    """Result of ``recommend_window`` (all values in milliseconds)."""

    window_ms: int
    burst_max_ms: int
    separated_min_ms: int
    burst_samples: int
    separated_samples: int
    ok: bool
    note: str
    burst_gaps: tuple[int, ...] = field(default=(), compare=False)
    separated_gaps: tuple[int, ...] = field(default=(), compare=False)

    def report(self) -> str:
        return (f"burst n={self.burst_samples} max={self.burst_max_ms}ms | "
                f"separated n={self.separated_samples} min={self.separated_min_ms}ms "
                f"| window={self.window_ms}ms ({'ok' if self.ok else 'OVERLAP'})"
                f" — {self.note}")


def _round_step(value: float, step_ms: int) -> int:
    return int(round(value / step_ms)) * step_ms


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def recommend_window(burst: Iterable[float], separated: Iterable[float], *,
                     low_ms: int = WINDOW_LOW_MS, high_ms: int = WINDOW_HIGH_MS,
                     step_ms: int = 10,
                     burst_factor: float = BURST_FACTOR,
                     separated_factor: float = SEPARATED_FACTOR) -> Calibration:
    """Reduce measured gaps into the burst window to configure.

    Empty samples are not an error: the corresponding constraint is simply not
    applied (a caller may have measured only one of the two clusters). With both
    empty the configured default (``250``) is returned and flagged.
    """
    burst_gaps = tuple(sorted(int(round(g)) for g in burst if g is not None and g > 0))
    sep_gaps = tuple(sorted(int(round(g)) for g in separated if g is not None and g > 0))
    if not burst_gaps and not sep_gaps:
        return Calibration(window_ms=_clamp(250, low_ms, high_ms),
                           burst_max_ms=0, separated_min_ms=0,
                           burst_samples=0, separated_samples=0, ok=False,
                           note="no samples: kept the configured default (250ms)")

    burst_max = burst_gaps[-1] if burst_gaps else 0
    sep_min = sep_gaps[0] if sep_gaps else 0

    if burst_gaps and sep_gaps:
        want = burst_max * burst_factor
        ceiling = sep_min * separated_factor
        if want <= ceiling:
            raw, ok = want, True
            note = (f"{burst_factor:.2f}x the slowest burst gap ({burst_max}ms) "
                    f"stays below {separated_factor:.2f}x the fastest pause ({sep_min}ms)")
        elif burst_max < sep_min:
            raw = (burst_max + sep_min) / 2.0
            ok = True
            note = (f"clusters only just separate ({burst_max}ms < {sep_min}ms): "
                    f"took the midpoint, no headroom left")
        else:
            raw = want
            ok = False
            note = (f"OVERLAP: the slowest burst gap ({burst_max}ms) is not below the "
                    f"fastest pause ({sep_min}ms); re-measure before trusting this")
    elif burst_gaps:
        raw = burst_max * burst_factor
        ok = True
        note = "only burst gaps measured: window set above them"
    else:                                  # separated only
        raw = sep_min * separated_factor
        ok = True
        note = "only separated gaps measured: window set below them"

    window = _clamp(_round_step(raw, step_ms), low_ms, high_ms)
    if window != int(round(raw)):
        note += f" (rounded/clamped to {window}ms)"
    return Calibration(window_ms=window, burst_max_ms=burst_max,
                       separated_min_ms=sep_min,
                       burst_samples=len(burst_gaps), separated_samples=len(sep_gaps),
                       ok=ok, note=note, burst_gaps=burst_gaps,
                       separated_gaps=sep_gaps)


def gaps_from_presses(presses_ms: Sequence[float], *, max_gap_ms: int = 600) -> list[int]:
    """Gaps between consecutive timestamps, keeping only burst-sized ones.

    The capture tool records every Enter of a phase; the gap between two SETS
    (the operator pausing between them) is not a burst sample, so anything above
    ``max_gap_ms`` is dropped. Order is preserved.
    """
    out: list[int] = []
    for prev, cur in zip(presses_ms, presses_ms[1:]):
        gap = int(round(cur - prev))
        if 0 < gap <= max_gap_ms:
            out.append(gap)
    return out
