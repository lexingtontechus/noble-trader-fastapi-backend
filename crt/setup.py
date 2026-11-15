"""setup.py — CRT setup detection (CRT-1, port of EA ``EvaluateArmedAt``).

One CRT setup: C2 one-sided-sweeps a C1 extreme and closes back inside C1;
the CONT trade places a stop at the C2 sweep extreme in the SWEEP direction,
SL beyond the C2 opposite extreme, valid during C3 only.

Ported VERBATIM from ``CRT_Auction_Reversal_AIO_EA_v5_CONT.mq4`` (raw/config
mode — the EA with ``UseOptimizedConfig`` on detects RAW setups: the
MinSweepATR/MaxSweepATR/chop/session filters are LOOSENED at detection and
the option's filters apply later in ``gates.cfg_filter_pass``):

- ``RequireC2CloseInsideFullC1`` (true): close strictly inside C1's range,
  same condition for bull and bear;
- ``RejectDoubleSweep`` (true): both-side sweeps are rejected;
- bull wins ties (``if(bull) bear = false``);
- ATR(14) at C2 (Wilder, MT4 seed — ``crt/atr.py``);
- ``sweep_atr`` = sweep depth / ATR;
- chop metrics over the 6 bars ENDING AT C1 (wick ratio, overlap ratio with
  the next-older bar, direction changes) — computed, not filtered (raw mode);
- session frozen at C2 close from the C3 boundary time (``c2_open + tf``) —
  the EA reads ``iTime(sym, tf, s2-1)`` of the then-forming C3 bar;
- liquidity: C1-swing computed (10-bar extreme) for the record; the
  ``LIQ_PRIOR_DAY`` variant is NOT ported (default is ``LIQ_C1_ONLY``; the
  artifact's filter space has no liquidity dimension — see the build worklog
  §7);
- vol percentile at C2 over the previous 500 ATRs (unknown -> -1 -> mid bit).

Bars are CLOSED, ASCENDING, open-time stamped (``crt/candles.py``). Index
map from MQL4 shifts: shift s -> ascending index ``len-1-s``; C1 = i-1,
C2 = i, C3 = i+1 (positional; the EA's exact-time ``iBarShift`` C3 match is
enforced in ``labeler.py``).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from crt.atr import atr_mt4, atr_percentile_at
from crt.sessions import SessionWindows, session_of

DIR_BUY, DIR_SELL = 1, -1
DIR_STR = {DIR_BUY: "BUY", DIR_SELL: "SELL"}


@dataclass
class SetupRecord:
    """One detected setup at C2 index ``c2_idx`` (EA ``CRTRecord`` subset)."""

    c2_idx: int
    c1_idx: int
    tf_seconds: int
    dir: int                       # setup direction (+1 BUY / -1 SELL)
    c2_open: float                 # epoch seconds (bar open == C2 close of prior bar)
    c3_open: float                 # c2_open + tf (the C3 boundary; session anchor)
    c1_high: float
    c1_low: float
    c2_open_price: float
    c2_high: float
    c2_low: float
    c2_close: float
    atr: float
    sweep_depth: float
    sweep_atr: float
    chop_wick: float
    chop_overlap: float
    dir_changes: int
    session_id: int
    in_session: bool
    c1_swing: bool
    vol_pct: float                 # ATR percentile at C2 (-1 unknown)

    @property
    def trade_buy(self) -> bool:
        """CONT trade direction: OPPOSITE the setup (EA ``TradeBuy``)."""
        return self.dir == DIR_SELL


def _chop_metrics(o: np.ndarray, h: np.ndarray, low: np.ndarray,
                  c: np.ndarray, i_c1: int, lookback: int) -> tuple[float, float, int]:
    """EA ``CalculateChopMetrics`` at startShift = C1, over ``lookback`` bars.

    Wick ratio: mean over the window of max(0, 1 - |c-o| / range). Overlap:
    bar j vs the OLDER bar j-1 (MQL4 shift i vs i+1), overlap / min(range).
    Direction changes: sign flips of close-open (doji bars skipped).
    """
    n = max(2, lookback)
    lo = max(0, i_c1 - (n - 1))
    wick_sum, overlap_sum = 0.0, 0.0
    overlap_count, dir_changes, prev_dir = 0, 0, 0
    for j in range(i_c1, lo - 1, -1):
        rng = h[j] - low[j]
        if rng <= 0:
            continue
        wick_sum += max(0.0, 1.0 - abs(c[j] - o[j]) / rng)
        d = 1 if c[j] > o[j] else (-1 if c[j] < o[j] else 0)
        if d != 0:
            if prev_dir != 0 and d != prev_dir:
                dir_changes += 1
            prev_dir = d
        if j - 1 >= 0:
            rng_prev = h[j - 1] - low[j - 1]
            overlap = max(0.0, min(h[j], h[j - 1]) - max(low[j], low[j - 1]))
            denom = min(rng, rng_prev)
            if denom > 0:
                overlap_sum += overlap / denom
                overlap_count += 1
    return (wick_sum / n,
            (overlap_sum / overlap_count) if overlap_count > 0 else 0.0,
            dir_changes)


def detect_setups(t: np.ndarray, o: np.ndarray, h: np.ndarray, low: np.ndarray,
                  c: np.ndarray, tf_seconds: int, windows: SessionWindows,
                  chop_lookback: int = 6, liquidity_lookback: int = 10,
                  atr_period: int = 14, vol_lookback: int = 500) -> list[SetupRecord]:
    """All RAW setups over the closed-bar arrays, c2-open time ascending.

    ``i`` ranges over C2 candidates whose C1 (i-1) and the chop/ATR lookbacks
    exist. Setups whose C3 bar has not closed yet (i == len-1) are still
    emitted — they ARM at c2_open; their labels stay PENDING until the C3
    bar closes (labeler). ``tf_seconds`` is the engine's TF (the bar list's
    own time deltas may include gaps; the C3 boundary is clock arithmetic:
    ``c2_open + tf_seconds``, the EA's then-forming-bar time).
    """
    n = t.size
    if n < 3:
        return []
    atr = atr_mt4(h, low, c, atr_period)
    first = max(atr_period, chop_lookback + 1, liquidity_lookback + 1)
    out: list[SetupRecord] = []
    for i in range(first, n):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        i1 = i - 1
        h1, l1 = h[i1], low[i1]
        h2, l2, c2, o2 = h[i], low[i], c[i], o[i]
        if h1 <= l1 or h2 <= l2:
            continue
        sweep_low = l2 < l1
        sweep_high = h2 > h1
        if sweep_low and sweep_high:
            continue    # RejectDoubleSweep
        # RequireC2CloseInsideFullC1: strict inside for both sides
        inside = (l1 < c2 < h1)
        bull = sweep_low and inside
        bear = sweep_high and inside
        if bull:
            bear = False
        if not bull and not bear:
            continue
        depth = (l1 - l2) if bull else (h2 - h1)
        sweep_atr = depth / atr[i]
        # liquidity context: C1 is the lookback-window extreme (EA iLowest/
        # iHighest == s1; recorded only — LIQ_C1_ONLY default never filters)
        lo = max(0, i1 - liquidity_lookback + 1)
        if bull:
            c1_swing = bool(l1 <= np.min(low[lo:i1 + 1]))
        else:
            c1_swing = bool(h1 >= np.max(h[lo:i1 + 1]))
        wick, overlap, dch = _chop_metrics(o, h, low, c, i1, chop_lookback)
        c3_open = float(t[i]) + float(tf_seconds)
        sid, _label, in_sess = session_of(c3_open, windows)
        out.append(SetupRecord(
            c2_idx=i, c1_idx=i1, tf_seconds=int(tf_seconds),
            dir=DIR_BUY if bull else DIR_SELL,
            c2_open=float(t[i]), c3_open=c3_open,
            c1_high=float(h1), c1_low=float(l1),
            c2_open_price=float(o2), c2_high=float(h2), c2_low=float(l2),
            c2_close=float(c2),
            atr=float(atr[i]), sweep_depth=float(depth), sweep_atr=float(sweep_atr),
            chop_wick=float(wick), chop_overlap=float(overlap), dir_changes=int(dch),
            session_id=sid, in_session=bool(in_sess), c1_swing=bool(c1_swing),
            vol_pct=atr_percentile_at(atr, i, vol_lookback),
        ))
    return out
