"""candles.py — closed-bar extraction and M30 derivation (CRT-1, TDVA seam).

The engine's ONLY data seam is the platform candle fetcher
(``regime_platform.sweep_orchestrator.price_fetcher.fetch_candles_for_symbols``),
whose candle rows are ``{"t","o","h","l","c","v"}``, ASCENDING, with ``t`` the
bar's OPEN time in Unix seconds (TradingView history convention: the TDVA
response mirrors TV's ``time/open/high/low/close`` shape and the repo treats
the last element as the still-forming bar — kairos.py's frozen contract).

Honest caveat (recorded in worklog/20261115_KAIROS_CRT1_BUILD.md §7): the
open-time reading follows the TV-documented convention; this sandbox has no
TDVA key to verify it against a live response, and no downstream consumer in
the repo pins it. The operator's M30-vs-MT4 overlap check (assessment §8)
doubles as the verification gate for this convention.

M30: the TDVA interval map has no 30m key, so M30 bars are derived by
aggregating consecutive closed M15 pairs on :00/:30 boundaries (assessment
§5). An M30 bar is emitted ONLY when both halves are present and closed —
a half-bar gap yields no M30 bar (never a synthetic one).
"""
from __future__ import annotations

from typing import Any

import numpy as np


def closed_bars(rows: list[dict[str, Any]] | None, tf_seconds: int,
                now_epoch: float) -> list[dict[str, Any]]:
    """Closed OHLCV bars from a fetched row list.

    - drops bars without a parseable ``t`` or any missing o/h/l/c
      (the engine needs full OHLC; volume is optional);
    - keeps ASCENDING order (the fetcher's contract);
    - drops the still-forming bar deterministically: a bar with open time
      ``t`` is closed iff ``t + tf_seconds <= now`` (wall-clock truth, robust
      whether or not TDVA included a forming bar in the response — the repo's
      "last element is forming" convention is the special case);
    - dedupes by ``t`` (last wins).
    """
    out: dict[float, dict[str, Any]] = {}
    for row in rows or []:
        try:
            t = row.get("t")
            if t is None:
                continue
            t = float(t)
            o, h = row.get("o"), row.get("h")
            low, c = row.get("l"), row.get("c")
            if None in (o, h, low, c):
                continue
            o, h, low, c = float(o), float(h), float(low), float(c)
        except (TypeError, ValueError):
            continue
        if t + tf_seconds > now_epoch:
            continue    # still forming
        v = row.get("v")
        out[t] = {"t": t, "o": o, "h": h, "l": low, "c": c,
                  "v": (float(v) if v is not None else None)}
    return [out[t] for t in sorted(out)]


def aggregate_m30(m15_closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive closed M30 bars from closed M15 bars (2x15m aggregation).

    The M30 bar opening at ``T`` (T % 1800 == 0) aggregates the M15 bars
    opening at ``T`` and ``T + 900``. Both halves must be present (a gap
    skips the M30 bar entirely — no synthetic bars). o = first half's open,
    h/l = max/min, c = second half's close, v = sum of present volumes.
    """
    by_t: dict[float, dict[str, Any]] = {b["t"]: b for b in m15_closed}
    out: list[dict[str, Any]] = []
    for t in sorted(by_t):
        if t % 1800 != 0:
            continue
        second = by_t.get(t + 900.0)
        if second is None:
            continue
        first = by_t[t]
        v1, v2 = first.get("v"), second.get("v")
        vol = None if (v1 is None and v2 is None) else float(v1 or 0.0) + float(v2 or 0.0)
        out.append({
            "t": t,
            "o": first["o"],
            "h": max(first["h"], second["h"]),
            "l": min(first["l"], second["l"]),
            "c": second["c"],
            "v": vol,
        })
    return out


def to_arrays(bars: list[dict[str, Any]]) -> tuple[np.ndarray, ...]:
    """(t, o, h, low, c) ascending numpy arrays from closed bars."""
    t = np.asarray([b["t"] for b in bars], dtype=float)
    o = np.asarray([b["o"] for b in bars], dtype=float)
    h = np.asarray([b["h"] for b in bars], dtype=float)
    low = np.asarray([b["l"] for b in bars], dtype=float)
    c = np.asarray([b["c"] for b in bars], dtype=float)
    return t, o, h, low, c


def expected_closed_open(now_epoch: float, tf_seconds: int) -> float:
    """Open time of the newest bar that SHOULD be closed at ``now``.

    The bar opening at ``floor(now/tf)*tf`` is still forming; the one before
    it is closed. Used by the job to skip fetches between bar boundaries.
    """
    import math
    return math.floor(now_epoch / tf_seconds) * tf_seconds - tf_seconds
