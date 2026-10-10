"""Walk-forward held-out gate — worklog 20261009_WALKFORWARD_SPLIT.md.

Tests tiered_atr.walk_forward_validate: the held-out check that a chosen
bracket generalises, which select_tp_sl's in-sample e1/e2 split cannot
provide.
"""
from __future__ import annotations

import numpy as np

from regime_platform.sweep_orchestrator import tiered_atr as ta
from regime_platform.sweep_orchestrator import settlement_contract as sc


def _env(fav_atr, adv_atr, n):
    """(n, 48, 2) envelope. Each signal is either a TP-reacher or an
    SL-hitter, not both: a TP-reacher has a deep favourable excursion and a
    shallow adverse one (so only fav crosses its threshold); an SL-hitter is
    the mirror. ``n`` is split evenly so both halves contain a mix — the gate
    is about the FOLD expectancy, not a degenerate all-one-outcome bar.

    To make a whole fold one outcome, pass n for that fold and concat (see
    the overfit tests, which build two folds with opposite outcomes).
    """
    half = n // 2
    tp_rows = np.concatenate([
        np.full((half, 48, 1), float(fav_atr)),
        np.full((half, 48, 1), -0.1),
    ], axis=2)
    sl_rows = np.concatenate([
        np.full((n - half, 48, 1), 0.1),
        np.full((n - half, 48, 1), float(adv_atr)),
    ], axis=2)
    return np.concatenate([tp_rows, sl_rows], axis=0)


def _all_outcome_env(outcome, n):
    """(n, 48, 2) envelope where EVERY signal resolves the same way — used to
    build a fold that is uniformly TP or uniformly SL, so a fold's expectancy
    is unambiguous. ``outcome`` in {"tp","sl"}."""
    if outcome == "tp":
        # deep favourable, shallow adverse → TP every signal
        return np.concatenate([
            np.full((n, 48, 1), 5.0), np.full((n, 48, 1), -0.1),
        ], axis=2)
    # deep adverse, shallow favourable → SL every signal
    return np.concatenate([
        np.full((n, 48, 1), 0.1), np.full((n, 48, 1), -5.0),
    ], axis=2)


class TestWalkForwardValidate:
    def _run(self, env, tp, sl, **kw):
        kw.setdefault("same_bar_policy", sc.SAME_BAR_SL_FIRST)
        kw.setdefault("min_fold_n", 20)
        return ta.walk_forward_validate(env, tp, sl, **kw)

    def test_both_halves_pass(self):
        # every signal reaches +tp before -sl (fav deep, adv shallow) → both
        # folds positive → ok.
        env = _all_outcome_env("tp", 80)
        r = self._run(env, tp=2.0, sl=1.0, cost_atr=0.05)
        assert r["ok"] is True
        assert r["reason"] == "ok"
        assert r["is_exp"] is not None and r["oos_exp"] is not None
        assert r["is_exp"] > 0 and r["oos_exp"] > 0
        assert r["is_n"] == 40 and r["oos_n"] == 40

    def test_overfit_first_half_rejected(self):
        # FIRST half reaches TP, SECOND half hits SL — the overfit signature:
        # earns on the chosen half, loses out-of-sample.
        env = np.concatenate([_all_outcome_env("tp", 40), _all_outcome_env("sl", 40)], axis=0)
        r = self._run(env, tp=2.0, sl=1.0, cost_atr=0.05)
        assert r["ok"] is False
        assert r["reason"] == "oos_expectancy_below_floor"
        assert r["is_exp"] > 0 and r["oos_exp"] < 0

    def test_overfit_second_half_rejected(self):
        # mirror: first half losing, second half earning. The gate is
        # symmetric — whichever half collapses, the pick is rejected (it was
        # chosen on the full sample, so a collapsing fold means it only earned
        # on the other half).
        env = np.concatenate([_all_outcome_env("sl", 40), _all_outcome_env("tp", 40)], axis=0)
        r = self._run(env, tp=2.0, sl=1.0, cost_atr=0.05)
        assert r["ok"] is False
        assert r["reason"] == "is_expectancy_below_floor"
        assert r["is_exp"] < 0 and r["oos_exp"] > 0

    def test_thin_folds_fail_open(self):
        # n=20 → 10/10 decided, below min_fold_n=20 → fail open, not reject.
        env = _all_outcome_env("tp", 20)
        r = self._run(env, tp=2.0, sl=1.0, cost_atr=0.05, min_fold_n=20)
        assert r["ok"] is True
        assert r["reason"] == "insufficient_data"

    def test_thin_fold_fails_open_even_when_oos_negative(self):
        # A losing out-of-sample half must NOT reject when the fold is too thin
        # — thin data defers to prior behaviour, it does not silently kill.
        env = np.concatenate([_all_outcome_env("tp", 10), _all_outcome_env("sl", 10)], axis=0)
        r = self._run(env, tp=2.0, sl=1.0, cost_atr=0.05, min_fold_n=20)
        assert r["ok"] is True
        assert r["reason"] == "insufficient_data"

    def test_empty_envelope_fails_open(self):
        r = self._run(np.empty((0, 48, 2)), tp=2.0, sl=1.0)
        assert r["ok"] is True
        assert r["reason"] == "insufficient_data"

    def test_min_oos_floor_is_configurable(self):
        # A barely-positive held-out fold passes at floor 0.0 but fails at a
        # higher bar. Interleave TP/SL so BOTH folds are the same 50/50 mix
        # (expectancy = 0.5*tp - 0.5*sl - cost, small and positive), not a
        # per-fold single outcome.
        rng = np.random.default_rng(7)
        tp = _all_outcome_env("tp", 40)
        sl = _all_outcome_env("sl", 40)
        env = np.concatenate([tp, sl], axis=0)
        env = env[rng.permutation(len(env))]
        r0 = self._run(env, tp=2.0, sl=1.0, cost_atr=0.0, min_oos_expectancy=0.0)
        r1 = self._run(env, tp=2.0, sl=1.0, cost_atr=0.0, min_oos_expectancy=0.60)
        assert r0["ok"] is True
        assert r1["ok"] is False


class TestWalkForwardFlagDefaults:
    def test_ships_shadow(self):
        from regime_platform.core.feature_flags import (
            get_dynamic_atr_walkforward_mode,
            get_dynamic_atr_walkforward_min_fold_n,
            get_dynamic_atr_walkforward_min_oos,
        )
        assert get_dynamic_atr_walkforward_mode() == "shadow"
        assert get_dynamic_atr_walkforward_min_fold_n() == 20
        assert get_dynamic_atr_walkforward_min_oos() == 0.0
