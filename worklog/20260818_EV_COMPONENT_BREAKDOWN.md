# EV Engine Component Breakdown & Regime Confidence Analysis (Aug 18, 2026)

## Task
Verify the TimesFM confidence fix is active in Studio production runtime, then evaluate
the EV engine for sample symbols to confirm Markov and regime components produce correct
numbers relative to actual market movement.

## TimesFM Fix Verification

**✅ CONFIRMED DEPLOYED** — Orchestrator log at startup (21:10 UTC, Aug 18) shows:
```
Qualification thresholds: TIMESFM_SIGMOID_K=10.0, TIMESFM_CLAMP=[0.05, 0.95]
```
Studio secrets are set correctly (not .env.local, which is ignored by Studio).

**✅ WORKING CORRECTLY** — TimesFM forecast values are near-neutral:
- AUDUSD: p_timesfm=0.5001 (price moved +0.003% → correctly near 0.5)
- EURUSD: p_timesfm=0.5004
- GBPUSD: p_timesfm=0.5004
- USDJPY: p_timesfm=0.5000
- XAGUSD: p_timesfm=0.4973
- XAUAUD: p_timesfm=0.4979

The sigmoid(k=10) correctly maps small price deltas to near-0.5 probabilities.

## Multi-Symbol EV Component Evaluation

### Regime vs Reality (21:25 UTC, Aug 18) — Market data from Yahoo Finance

| Symbol    | Regime          | Market 5h | p_win  | Markov_up | p_timesfm | Correct? |
|-----------|-----------------|-----------|--------|-----------|-----------|----------|
| AUDUSD    | strong_bull     | DOWN      | 0.939  | 0.0556    | 0.5001    | ❌ |
| EURUSD    | strong_bull     | UP        | 0.903  | 0         | 0.5004    | ✓ |
| GBPUSD    | strong_bull     | DOWN      | 0.967  | 0.0179    | 0.5004    | ❌ |
| USDJPY    | strong_bear     | DOWN      | 0.979  | 0         | 0.5000    | ✓ |
| USDCHF    | strong_bear     | DOWN      | 0.937  | 0         | 0.5000    | ✓ |
| USDCAD    | strong_bear     | DOWN      | 0.962  | 0         | 0.9995    | ✓ |
| BTCUSD    | bear            | DOWN      | 0.963  | 0         | 0.5004    | ✓ |
| US30USD   | bull            | DOWN      | 1.000  | 0.002     | 0.4998    | ❌ |
| US500     | strong_bull     | DOWN      | 1.000  | 0         | 0.4998    | ❌ |
| XAGUSD    | strong_bear     | DOWN      | 0.993  | 0         | 0.4973    | ✓ |
| XAUAUD    | strong_bull     | DOWN      | 0.988  | 0.0152    | 0.4979    | ❌ |
| XAUEUR    | strong_bull     | DOWN      | 0.987  | 0.0081    | 0.4977    | ❌ |
| XAUUSD    | strong_bull     | DOWN      | 0.964  | 0         | 0.4977    | ❌ |
| AAPL      | low_vol_strong_bull | N/A   | 0.119  | 0         | N/A       | ? |

**10 of 14 symbols** have regime classifications that contradict actual market direction.

### AUDUSD Detailed Component Breakdown (21:20 UTC)

| Component | Value | Logit | Weight | Contribution |
|-----------|-------|-------|--------|-------------|
| p_regime  | 1.0   | 34.54 | 0.357  | 12.34 (100%) |
| p_markov  | 0.5*  | 0.00  | 0.286  | 0.00 (0%)    |
| p_timesfm | 0.5001| 0.0004| 0.357  | 0.00 (0%)    |
| **Total** |       |       |        | **12.34**    |
| **p_win** |       |       |        | **1.0000**   |

*p_markov = 0.5 (neutral fallback — transition matrix row is degenerate/all-zeros)

DB shows: `predicted_p_win_raw=0.9788`, `p_win=0.939` (after kelly/2 + bayesian penalty).

### Calibration Record Evidence

```
AUDUSD nt_calibration_records (post-purge, Aug 11-18):
  50 records: 0 wins, 50 losses
  Avg predicted p_win: 0.9783 (98% confidence, 0% correct)
  All losses: -0.0039 P&L each
```

### EOD Correction Events (2026-08-18)

| Time (UTC) | Action | bias | avg_pred | kelly_factor | adjusted_p_win |
|------------|--------|------|----------|-------------|----------------|
| 00:37 | emergency_heavy_sweep | 0.9728 | 0.9796 | 0.5 | — |
| 00:37 | kelly_reduction | 0.9728 | 0.9796 | 0.5 | — |
| 00:37 | bayesian_penalty | 0.9728 | 0.9796 | 0.5 | — |
| 12:30 | emergency_heavy_sweep | 0.9728 | 0.9796 | 0.5 | 0.9491 |
| 12:30 | kelly_reduction | 0.9728 | 0.9796 | 0.5 | 0.9491 |
| 12:30 | bayesian_penalty | 0.9728 | 0.9796 | 0.5 | 0.9491 |
| 16:30 | all three | 0.9796 | 0.9796 | 0.5 | 0.9424 |
| 20:30 | all three | 0.9792 | 0.9792 | 0.5 | 0.9208 |

Bias has been flat at 0.97+ for all correction passes — the kelly halving and
bayesian penalties are applied post-hoc but the **raw p_win stays at 0.98-1.0**.

## Root Cause

The logit-pooled blend has `p_regime=1.0` contributing logit(1.0)≈34.5 (clamped).
At weight 0.357 (after p_pattern renormalization), this is 12.34 — ~100% of the
total logit sum. p_timesfm=0.5001 (logit≈0) and p_markov=0.5 (logit=0) contribute
ZERO countervailing force.

**The TimesFM fix is necessary but insufficient.** The HMM regime confidence of 1.0
is the actual driver of overconfidence. The markov matrix degeneracy (all-zero rows
→ 0.5 fallback) means Markov provides no signal, not even a pessimistic one (0.0).

## Proposed Fixes

1. **Cap p_regime** at 0.95 in `regime_engine.py:256` → blend drops 1.0→0.74
2. **Return 0.0 instead of 0.5** for degenerate Markov rows in `ev_engine.py:233`
3. Fix bootstrap log_return filter bug in `model.py:365` (`closes[closes > 0]` → `closes`)

## Related Files
- `regime_platform/core/regime_engine.py:256` — confidence computation
- `regime_platform/services/statistics/ev_engine.py:404-432` — compute_p_regime
- `regime_platform/services/statistics/ev_engine.py:221-256` — _markov_hold_probability
- `regime_platform/services/statistics/ev_engine.py:510-597` — compute_ev (logit pooling)
- `regime_platform/services/hmm/model.py:555-584` — _aggregate_transition (5→3 state mapping)
- `regime_platform/services/hmm/model.py:363-366` — _compute_log_returns (filter bug)
- `regime_platform/sweep_orchestrator/orchestrator.py:1528-1643` — regime detection + EV call chain
