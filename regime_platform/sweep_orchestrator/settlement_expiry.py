"""
Per-signal dynamic settlement expiry (worklog 20261009_P90_HOURS_LIVE_MODE.md)
═══════════════════════════════════════════════════════════════════════

The resolver settles every signal against a fixed ``EXPIRY_BARS = 48``
(48 post-entry 1h bars → ``expired``). Reachability data shows that
48h is right for wide brackets and wrong for tight ones, asymmetrically:

  ATR bucket   median p90   median settle   avg expired
  0.5-1          26.1h          7.1h           0.0%
  1-2            23.0h          4.3h           0.0%
  2-4            26.0h         12.1h           0.0%
  4+             56.1h         41.3h          54.3%   ← 48h cuts this tail

Of 180 buckets (n>=30): 120 (67%) have p90 <= 48h (the bar over-generous),
60 (33%) have p90 > 48h (the bar too short). So a flat ``ceil(p90)`` is the
tempting-but-wrong rule: a tight bracket that settles in ~4h median would
get a *looser* leash under p90, not a tighter one. The genuinely
market-driven expiry is bracket-aware:

  * tight/mid buckets (settle fast, ~0% expire) → expiry from the MEDIAN
    resolution time x a safety multiple — a bracket that hasn't resolved
    in a small multiple of its typical settlement time is expired.
  * the wide bucket (long tail, 54% expire) → expiry from p90, because
    that bucket genuinely needs the tail; cutting it at 48h mislabels
    in-progress resolution as expiry.

This module is PURE (no I/O). The reachability row is supplied by the
caller (eod_resolver), which owns the fetch. Ships shadow-first:
``settlement_expiry_mode: fixed | shadow | live`` — fixed (default) is
byte-identical to today; shadow computes + logs the delta, still settles
on EXPIRY_BARS; live passes the dynamic bars into the two resolve sites.

Caveat that ships with this (documented in the worklog): the current
reachability key is the coarse 5-bucket ``distance_atr_bucket``, and the
continuous ``tp_distance_atr`` spans a 10x range WITHIN the "4+" bucket
(29-78h p90). Shadow mode exists precisely to observe whether the coarse
bucket yields a usable expiry distribution before any rekey to a
continuous / per-symbol-normalized ATR (the "reflect dynamic_atr" rework)
is committed to.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("regime.sweep.settlement_expiry")


# Buckets whose resolution distribution is a long tail (high expiry, wide
# p90). These take the p90 percentile; everything else takes median*safety.
_WIDE_BUCKETS = frozenset({"4+"})

# Safety multiple applied to the median for tight/mid buckets: a signal
# that hasn't resolved within this multiple of its typical settlement time
# is expired. 2.0 → a bracket that settles in ~4h gets an ~8h expiry, not
# a 48h leash. Tunable; the shadow run calibrates it.
_TIGHT_SAFETY_MULT = 2.0

# Global clamps (in HOURS). FLOOR never expires before half a day — a
# resolving signal must get a real chance. CAP never holds past 3 days even
# if p90 is extreme (the wide-bucket p90 runs to 78h); an unbounded expiry
# would stall resolution indefinitely.
_FLOOR_HOURS = 12.0
_CAP_HOURS = 72.0


def dynamic_expiry_hours(
    reach_row: Optional[dict[str, Any]],
    *,
    floor_hours: float = _FLOOR_HOURS,
    cap_hours: float = _CAP_HOURS,
    tight_safety_mult: float = _TIGHT_SAFETY_MULT,
    min_n: int = 20,
) -> Optional[float]:
    """Market-driven settlement horizon (hours) for one signal, from its
    bracket's reachability row. Returns None on no/insufficient evidence →
    the caller fails open to the fixed ``EXPIRY_BARS``.

    ``reach_row`` carries (at least) ``distance_atr_bucket``, ``n``,
    ``median_hours_to_resolve`` and ``p90_hours`` — the row the resolver
    looked up for this signal's (symbol, vol_tier, bucket).

    Rule:
      * wide bucket (``4+``) → ``p90_hours`` (the tail matters there).
      * tight/mid buckets    → ``median_hours_to_resolve * tight_safety_mult``.
      * clamped to ``[floor_hours, cap_hours]``.
    """
    if not reach_row:
        return None
    n = int(reach_row.get("n") or 0)
    if n < min_n:
        return None

    bucket = str(reach_row.get("distance_atr_bucket") or "")
    p90 = reach_row.get("p90_hours")
    median = reach_row.get("median_hours_to_resolve")

    if bucket in _WIDE_BUCKETS:
        raw = p90 if p90 not in (None, 0) else median
    else:
        # Prefer the median x safety; fall back to p90 when median absent.
        if median not in (None, 0):
            raw = float(median) * float(tight_safety_mult)
        else:
            raw = p90

    if raw in (None, 0):
        return None
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None

    return max(floor_hours, min(hours, cap_hours))


def hours_to_bars(hours: Optional[float], *, fixed_bars: int) -> int:
    """Convert a dynamic expiry (hours) to 1h settlement bars, clamped to a
    sane ``[1, fixed_bars*1.5]`` band and falling back to ``fixed_bars``
    when ``hours`` is None. The upper clamp keeps a single signal from ever
    being held more than 1.5x the current 48-bar ceiling (72 bars) — the
    worklog's CAP — so the resolver loop's per-run bar fetch
    (``LIGHT_BARS = 200``) always covers it.
    """
    if hours is None:
        return fixed_bars
    import math
    bars = int(math.ceil(hours))
    bars = max(1, min(bars, int(fixed_bars * 1.5)))
    return bars
