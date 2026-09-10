# 2026-08-24 — RegimeHMM Feature Standardization (Change A of the HMM consolidation plan)

**Scope:** `regime_platform/core/regime_engine.py` only. First of two independently-shippable
changes scoped in response to explicit direction: don't compound fixes, deprecate/remove
what's proven not to work, keep one coherent pipeline. Change B (deriving `p_markov_hold_n`
from this same, now-fixed model, retiring the separate 9-feature Markov HMM) is scoped
separately and starts only after this change is confirmed stable in production.

**Status:** Code-complete, tested (real-data verified, not just synthetic), not yet deployed.

---

> **Priority**: P1 · **Task**: Standardize RegimeHMM features to fix
> near-universal HMM non-convergence ·
> **Status**: pending · **Date**: 2026-08-24

## Problem

`hmmlearn.base: Model is not converging` fired on **~99% of light-sweep ticks for 13/14
symbols** (confirmed via live log counts: 111-112 of 112 sampled ticks, every non-AAPL
symbol; AAPL's apparent exception was a closed/flat weekend market, not a healthy fit).
R4 (built 2026-08-23) discounts `p_regime`'s blend weight when this fires — but at this
frequency, the "discount" had quietly become the *permanent* state for nearly every
symbol, not the rare-anomaly trigger it was designed as.

## Root cause (real-data tested, not theoretical)

`RegimeHMM._trend_model` fits a 4-state diagonal-covariance Gaussian HMM on 3 features
(`returns`, `cum_short`(10-bar), `cum_long`(30-bar)) with **no standardization**. Real
live scale check for EURUSD: per-column std `[0.000378, 0.001849, 0.003212]` — an 8.5x
spread across 3 features sharing ONE `min_covar` floor value (the R1 fix, computed from
the *largest*-std column). That floor is effectively non-binding for the smallest-scale
feature, letting its per-state variance shrink further than intended — recreating the
overconfidence R1's floor was meant to prevent, on a different dimension, and contributing
to real (not numerical-noise) log-likelihood swings of hundreds-to-thousands across a
single fit's iterations.

Three fix hypotheses were tested empirically against real EURUSD/XAUUSD/BTCUSD/AAPL price
data (`scripts/diagnose_hmm_convergence.py`, run before this fix, results below) before
picking one:

| Hypothesis | Result |
|---|---|
| Drop the redundant `cum_short` feature (address collinearity) | **Made it worse** in 3 of 4 symbols (baseline converged → reduced-feature did not) |
| 5-restart, keep best log-likelihood | Fixed only 1 of 4 symbols |
| **Z-score standardize features before fitting** | **Fixed all 4 of 4 symbols tested** |

Standardization was the only reliable fix. This directly refutes the collinearity
hypothesis this session initially favored, and shows multi-restart isn't sufficient on
its own — both are documented as tested-and-rejected in the module docstring rather than
left as untested assumptions for someone to re-discover later.

## Fix

`RegimeHMM.fit()`: z-score `vol_features`/`trend_features` (per fitting window, per
column) before fitting. Store the fit's `(mu, sigma)` (`_fit_scale`) so `predict()` can
standardize query features identically. Immediately after fitting, convert `means_` back
to original units in place (`_destandardize_means`) — this is what keeps every downstream
consumer (`_label_states`, `vol_label_means`/`trend_label_means`, R1's cross-tick label
continuity matching) working unchanged, on the same units as before this fix.

**Removed, not left dormant:** `_data_min_covar` (R1 fix #1) — a single fixed
`_STANDARDIZED_MIN_COVAR = 0.01` replaces it. Once every feature is on the same scale
(std=1), the entire per-window "compute a floor from the largest-std column" logic is
unnecessary — this is a strict simplification the standardization fix enables, not a
parallel mechanism to maintain. The module docstring documents the removal and why,
struck through rather than silently deleted from the history section, so a future reader
sees the reasoning, not just its absence.

**Unaffected:** R1 fix #2 (label continuity) — verified still works correctly post-fix
(existing tests pass unchanged; means are back in original units by the time continuity
matching runs). R4 (non-convergence discount) — unaffected, but should now fire far less
often given the root cause it was compensating for is fixed.

## Verification

- `tests/test_regime_stability.py`: 6 new tests for the standardization mechanism itself
  (`_fit_scale`, `_standardize`, `_destandardize_means`, zero-variance-column guard, fixed
  min_covar wiring) plus a regression test (`test_fit_converges_with_realistic_scale_disparity`)
  reproducing the real confirmed scale disparity via synthetic data. Manually confirmed
  this test fails on the unstandardized (old) code path and passes on the fixed path
  (reverted-and-confirmed pattern, not just assumed).
- `scripts/diagnose_hmm_convergence.py` (real data, not committed to CI): ran the actual
  (fixed) `RegimeHMM.fit()`/`.predict()` against live EURUSD/XAUUSD/BTCUSD/US500 —
  **0 of 4 symbols showed any non-convergence** (AAPL hit a transient TDVA fetch hiccup,
  unrelated).
- Full suite: **621 passed, 23 skipped, 0 failed** (up from 617 pre-change).

## Not done in this pass

- Change B (retiring the separate Markov HMM, deriving `p_markov_hold_n` from this
  model's own `transmat_`) is scoped in the approved plan but not started — waits for
  this change to prove stable live first.
- No changes to blend weights, signal gates, or any other file.

## Next steps

1. Deploy, verify live (git HEAD match, clean restart, tail `logs/orchestrator.log` for
   the next several sweep ticks).
2. Watch `did not converge cleanly` frequency drop for the 13 previously-affected symbols
   over the following sweeps — this is the live acceptance signal for Change A.
3. After a day or two of confirmed-stable live behavior, start Change B per the approved
   plan (`.claude/plans/goofy-rolling-swing.md`... — plan lives outside the repo; summary
   captured here and in the prior conversation turn for anyone picking this up later).
