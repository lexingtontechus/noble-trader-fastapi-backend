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


def _envelopes(n=120, edge=2.0, noise=0.3, seed=0):
    """(n, 48, 2) envelope with fav=adv=close — the close-basis degenerate case.

    The reopt now measures on the LIVE (intrabar) envelope, so its unit tests
    build envelopes. A close-degenerate envelope (fav == adv == the close path)
    reproduces the historical close-walk result, which is what these
    assertions were calibrated against.
    """
    paths = _paths(n=n, edge=edge, noise=noise, seed=seed)
    return np.stack([paths, paths], axis=2)


def _mixed_paths(n=200, edge=2.5, noise=0.15, seed=1):
    """Alternating up/down drifts — wr≈0.5 on symmetric brackets."""
    rng = np.random.default_rng(seed)
    t = np.arange(48)
    out = []
    for i in range(n):
        sign = 1.0 if i % 2 == 0 else -1.0
        out.append(sign * edge * (t / 47.0) + rng.normal(0, noise, 48))
    return np.vstack(out)


def _mixed_envelopes(n=200, edge=2.5, noise=0.15, seed=1):
    paths = _mixed_paths(n=n, edge=edge, noise=noise, seed=seed)
    return np.stack([paths, paths], axis=2)


def test_eval_cell_wr_and_ev():
    env = _envelopes()
    sc = eval_cell(env, 1.0, 1.0)
    assert sc is not None
    assert sc["dec"] >= 40
    assert 0.0 <= sc["wr"] <= 1.0


def test_solve_free_balanced_cell():
    # Mixed up/down drifts → balanced wr with RR>1 EV-positive cell
    env = _mixed_envelopes()
    sc = solve_free(env)
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


def test_urgency_config_defaults():
    from regime_platform.core.feature_flags import get_atr_reopt_urgency_config

    cfg = get_atr_reopt_urgency_config()
    assert cfg["enabled"] is True
    assert cfg["elevated_ratio"] == 1.25
    # 4h cycle floor — durable, not a 48h in-memory gate
    assert cfg["min_reopt_hours"] == 4.0
    assert cfg["cooldown_hours"] == 4.0
    # 2026-10-12: auto-apply of clamps into the config was REMOVED — the reopt
    # is proposal-only. Pin the absence so it cannot silently return.
    assert "auto_apply_clamps" not in cfg


def test_ts_or_none_parses_iso():
    from regime_platform.sweep_orchestrator.atr_reopt import _iso, _ts_or_none

    ts = 1727000000.0
    assert abs(_ts_or_none(_iso(ts)) - ts) < 1
    assert _ts_or_none(None) is None
    assert _ts_or_none("not-a-date") is None


def test_forex_drift_flags_against_for_override():
    """Config-layer parity (worklog 20261010_REOPT_CLAMP_HARMONIZATION.md).

    The pooled forex scope's solve must be compared against the SAME
    dynamic_atr layer the picker applies — by_symbol.FOREX. Passing a forex
    member resolves that override, so a wide solve (4.52/2.75) flags drift.
    (by_class.forex was removed 2026-10-12 — FOREX is defined once.)
    """
    sc = {"tp": 4.52, "sl": 2.75, "wr": 0.404, "ev": 0.086, "exp": 0.0,
          "n": 6506, "dec": 6506}
    # The fix path: a forex member → by_symbol.FOREX override → drift flagged.
    new = compare_to_current("USDJPY", sc, "forex")
    assert new["drift"] is True, "wide forex solve must drift vs the FOREX override"
    assert any("above clamp" in r for r in new["reasons"])
    # The FOREX override must be tight enough to reject 4.52 (this is the band
    # the picker is actually bound by).
    assert new["tp_atr_max"] is not None and float(new["tp_atr_max"]) < 4.52

