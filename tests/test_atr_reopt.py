"""Unit tests for monthly ATR re-opt (proposal-only free lattice)."""
from __future__ import annotations

import numpy as np

from regime_platform.sweep_orchestrator.atr_reopt import (
    _rel_drift,
    compare_to_current,
    eval_cell,
    solve_free,
)


def _paths(n=120, edge=2.0, noise=0.3, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(48)
    base = edge * (t / 47.0)
    return np.vstack([base + rng.normal(0, noise, 48) for _ in range(n)])


def _mixed_paths(n=200, edge=2.5, noise=0.15, seed=1):
    """Alternating up/down drifts — wr≈0.5 on symmetric brackets."""
    rng = np.random.default_rng(seed)
    t = np.arange(48)
    out = []
    for i in range(n):
        sign = 1.0 if i % 2 == 0 else -1.0
        out.append(sign * edge * (t / 47.0) + rng.normal(0, noise, 48))
    return np.vstack(out)


def test_eval_cell_wr_and_ev():
    paths = _paths()
    sc = eval_cell(paths, 1.0, 1.0)
    assert sc is not None
    assert sc["dec"] >= 40
    assert 0.0 <= sc["wr"] <= 1.0


def test_solve_free_balanced_cell():
    # Mixed up/down drifts → balanced wr with RR>1 EV-positive cell
    paths = _mixed_paths()
    sc = solve_free(paths)
    assert sc is not None
    assert 0.40 <= sc["wr"] <= 0.60
    assert sc["ev_h1"] > 0 and sc["ev_h2"] > 0
    assert sc["exp"] <= 0.20


def test_rel_drift_and_compare_flags_outside_clamp():
    assert _rel_drift(2.0, 1.0) == 1.0
    assert _rel_drift(1.05, 1.0) < 0.15
    # BTC-like clamp box: 8.07/1.77 with tight clamps — a far-TP cell drifts
    cmp = compare_to_current(
        "BTCUSD",
        {"tp": 15.0, "sl": 2.0, "wr": 0.45, "ev": 1.0, "exp": 0.0, "n": 100, "dec": 100},
        "crypto",
    )
    assert cmp["drift"] is True
    assert cmp["reasons"]
