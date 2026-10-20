"""brackets.py — bracket outcome resolution on the repo's settlement basis.

Faithful to the repo: entry = close[i] (the stale-close policy, kept
deliberately for the head-to-head proof), intrabar high/low barrier checks,
SL-first on same-bar double touches (eod_resolver.py:820-849 /
tiered_atr.py:97-98 basis), honest mark-to-market on expiry.

R multiples: tp -> +tp_atr/sl_atr; sl -> -1; expire -> signed MTM in
SL-distance units.
"""
from __future__ import annotations

import numpy as np


def resolve(entry_i, direction, tp_atr, sl_atr, expiry_bars, high, low, close, atr):
    """Resolve one bracket.

    Returns (outcome, R, end_j): outcome in {tp, sl, expire}; end_j = the bar
    where the trade resolved (entry bar itself if no future bars exist).
    """
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)
    atr = np.asarray(atr, float)
    i = int(entry_i)
    entry = close[i]
    tp_d = float(tp_atr) * atr[i]
    sl_d = float(sl_atr) * atr[i]
    if not (np.isfinite(tp_d) and np.isfinite(sl_d)) or tp_d <= 0 or sl_d <= 0:
        return "expire", 0.0, i
    last = min(i + int(expiry_bars), close.size - 1)
    if direction == 1:  # long
        tp_price = entry + tp_d
        sl_price = entry - sl_d
        for j in range(i + 1, last + 1):
            if low[j] <= sl_price:          # SL first (repo intrabar basis)
                return "sl", -1.0, j
            if high[j] >= tp_price:
                return "tp", float(tp_atr) / float(sl_atr), j
        r = (close[last] - entry) / sl_d
        return "expire", float(r), last
    elif direction == -1:  # short (mirror)
        tp_price = entry - tp_d
        sl_price = entry + sl_d
        for j in range(i + 1, last + 1):
            if high[j] >= sl_price:         # SL first
                return "sl", -1.0, j
            if low[j] <= tp_price:
                return "tp", float(tp_atr) / float(sl_atr), j
        r = (entry - close[last]) / sl_d
        return "expire", float(r), last
    raise ValueError(f"direction must be +1/-1, got {direction}")


def resolve_many(entries, directions, tp_atr, sl_atr, expiry_bars, high, low, close, atr):
    """Resolve many entries (vectorised across entries; per-entry horizon loop)."""
    out = []
    for i, d in zip(entries, directions):
        o, r, j = resolve(i, d, tp_atr, sl_atr, expiry_bars, high, low, close, atr)
        out.append((int(i), int(d), o, float(r), int(j)))
    return out


def slot_grid(n_bars: int, warmup: int = 720, step: int = 24) -> list:
    """Entry slot grid: every `step` bars from `warmup` (>= max(200, 720))."""
    return list(range(warmup, max(n_bars - 1, warmup), step))


def walk_book(slots, decide_fn, resolve_fn):
    """Walk the slot grid under the NO-OVERLAP rule.

    A slot is skipped while the previous taken trade is unresolved
    (its end bar >= this slot).  decide_fn(i) -> +1/-1/None (None = abstain);
    resolve_fn(i, d) -> (outcome, R, end_j).
    """
    trades = []
    last_end = -1
    for i in slots:
        if i <= last_end:
            continue
        d = decide_fn(i)
        if d is None:
            continue
        outcome, r, end_j = resolve_fn(i, d)
        trades.append({"slot": int(i), "direction": int(d), "outcome": outcome,
                       "r": float(r), "end": int(end_j)})
        last_end = end_j
    return trades
