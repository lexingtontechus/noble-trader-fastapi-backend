"""values.py — THE regime value assignment (the user's central ask).

Per bar, a record {trend, chop, vol}:

    trend = er_signed  in [-1, 1]  (magnitude = trendiness, sign = direction)
    chop  = choppiness value in [0, 1]   (1 = pure chop)
    vol   = rolling percentile of ATR% in [0, 1]

Plus a derived tri-state label with HYSTERESIS (causal state machine):

    start NEUTRAL;
    enter TREND_UP when er_signed >= +0.35, TREND_DOWN when <= -0.35;
    exit  to NEUTRAL when |er_signed| < 0.25.

Rules are applied in order each bar (from ANY state): a full-threshold
opposite reading flips the state directly (a strong opposite regime reading
is evidence), readings inside the dead band [0.25, 0.35) keep the current
state (debounce-like), and sub-0.25 readings reset to NEUTRAL.  Deterministic,
no fitting, no lookahead.  This is the neutral state the live 4-state HMM
vocabulary lacks (W-X1).
"""
from __future__ import annotations

import numpy as np

ENTER = 0.35
EXIT = 0.25
LABELS = ("neutral", "trend_up", "trend_down")


def tri_state_hysteresis(er_signed, enter: float = ENTER, exit_: float = EXIT) -> np.ndarray:
    """Causal tri-state label array from signed efficiency ratio."""
    er = np.asarray(er_signed, float)
    out = np.empty(er.size, dtype=object)
    state = "neutral"
    for i, v in enumerate(er):
        if not np.isfinite(v):
            state = "neutral"
            out[i] = "neutral"
            continue
        if v >= enter:
            state = "trend_up"
        elif v <= -enter:
            state = "trend_down"
        elif abs(v) < exit_:
            state = "neutral"
        # else: dead band — keep current state
        out[i] = state
    return out


def regime_values(features: dict) -> dict:
    """Per-bar {trend, chop, vol} values + tri-state regime label."""
    er_s = np.asarray(features["er_signed"], float)
    return {
        "trend": er_s,
        "chop": np.asarray(features["chop"], float),
        "vol": np.asarray(features["vol_pct"], float),
        "regime": tri_state_hysteresis(er_s),
    }
