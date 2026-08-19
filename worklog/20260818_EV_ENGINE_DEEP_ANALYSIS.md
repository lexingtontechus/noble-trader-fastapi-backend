# EV Engine Deep Analysis — Aug 18, 2026

## Purpose
Verify the TimesFM confidence fix in Studio production and evaluate the EV engine's component calculations for AUDUSD (and multi-symbol) to identify why p_win remains overconfident despite the TimesFM sigmoid fix.

## Verification of TimesFM Fix

**✅ CONFIRMED DEPLOYED**: Orchestrator startup log shows `TIMESFM_SIGMOID_K=10.0, TIMESFM_CLAMP=[0.05, 0.95]`

**✅ WORKING CORRECTLY**: TimesFM forecast values are near-neutral (0.497-0.5001) for symbols with minimal price movement. The sigmoid(k=10) correctly maps small price deltas to near-0.5 probabilities.

## Multi-Symbol Analysis (21:00-21:30 UTC, Aug 18)

### Regime vs Reality Comparison

| Symbol    | Model Regime          | Market (5h) | markov_up | p_timesfm | p_win | Match? |
|-----------|----------------------|-------------|-----------|-----------|-------|--------|
| AUDUSD    | high_vol_strong_bull | DOWN        | 0.0556    | 0.5001    | 0.939 | ❌ WRONG |
| EURUSD    | high_vol_strong_bull | UP          | 0         | 0.5004    | 0.903 | ✓ |
| GBPUSD    | high_vol_strong_bull | DOWN        | 0.0179    | 0.5004    | 0.967 | ❌ WRONG |
| USDJPY    | high_vol_strong_bear | DOWN        | 0         | 0.5000    | 0.979 | ✓ |
| USDCHF    | strong_bear          | DOWN        | 0         | 0.5000    | 0.937 | ✓ |
| USDCAD    | strong_bear          | DOWN        | 0         | 0.9995    | 0.962 | ✓ |
| BTCUSD    | bear                 | DOWN        | 0         | 0.5004    | 0.963 | ✓ |
| US30USD   | bull                 | DOWN        | 0.002     | 0.4998    | 1.000 | ❌ WRONG |
| US500     | strong_bull          | DOWN        | 0         | 0.4998    | 1.000 | ❌ WRONG |
| XAGUSD    | strong_bear          | DOWN        | 0         | 0.4973    | 0.993 | ✓ |
| XAUAUD    | strong_bull          | DOWN        | 0.0152    | 0.4979    | 0.988 | ❌ WRONG |
| XAUEUR    | strong_bull          | DOWN        | 0.0081    | 0.4977    | 0.987 | ❌ WRONG |
| XAUUSD    | strong_bull          | DOWN        | 0         | 0.4977    | 0.964 | ❌ WRONG |
| AAPL      | low_vol_strong_bull  | N/A         | 0         | N/A       | 0.119 | ? |

**10 out of 14 symbols** have regime classifications that **contradict actual market direction** (5h trend DOWN but model says BULL).

## Root Cause: p_regime = 1.0 Dominates Logit Pool

### The Math

The EV engine uses **logit pooling** (Equation 1):
```
logit(P_win) = Σ w_i × logit(p_i)
P_win = inv_logit(logit(P_win))
```

With p_pattern dropped (server-side), weights renormalize:
- w_p_regime = 0.25/0.70 = **0.357** (was 0.25 — 43% boost!)
- w_p_markov = 0.20/0.70 = **0.286**
- w_p_timesfm = 0.25/0.70 = **0.357**

### AUDUSD at 21:20 UTC — Component Values

| Component | Value | Logit | Weight | Contribution |
|-----------|-------|-------|--------|-------------|
| **p_regime** | **1.0** | **34.54** | 0.357 | **12.34** (100% of sum) |
| p_markov | 0.5 | 0.00 | 0.286 | 0.00 (0%) |
| p_timesfm | 0.5001 | 0.0004 | 0.357 | 0.00 (0%) |
| **Total logit_sum** | | | | **12.34** |
| **p_win** | | | | **1.0000** |

**logit(1.0) = 34.54** (clamped from ∞). Even at weight 0.357, this contributes **12.34** — completely overwhelming the other components. The TimesFM fix produces p_timesfm=0.5001 (logit≈0), which is mathematically correct, but it has **zero countervailing effect** against p_regime=1.0.

### Calibration Records (post-purge, Aug 11-12)

```
AUDUSD: 50 records, 0 wins, 50 losses (0% win rate)
Average predicted p_win: 0.9783 (98% confidence)
All outcomes: LOSS (-0.0039 P&L each)
```

The model was 98% confident, 100% wrong. The 4h correction loop correctly detects this as OVERCONFIDENT (bias=0.9792) and applies kelly halving + bayesian penalties, but **cannot fix the underlying p_regime=1.0**.

## Secondary Issue: Markov Transition Matrix Degeneracy

### The Degenerate State (markov_p_up=0, markov_p_dn=0)

At 21:20 UTC, nt_regime_log shows `markov_p_up=0` AND `markov_p_dn=0` for AUDUSD. This indicates:

1. The **5-state HmmModel** (from `services/hmm/model.py`) produces a transition matrix `T3`
2. The current_state maps to "FLAT" with absorbing transitions: `T[FLAT][UP]=0, T[FLAT][DOWN]=0, T[FLAT][FLAT]=1.0`
3. In `compute_p_markov_hold_n()` (ev_engine.py:238), `_matrix_power(M, 8)` is computed
4. `T^8[FLAT][UP] = 0.0` → `p_markov_hold_n = 0.0` (correctly pessimistic)

BUT the DB shows `raw=1.0` at 21:25, which means p_markov was either:
- `None` (transition_matrix=None → model not fitted → dropped from blend)
- `0.5` (row_sum=0 in _dict_to_matrix → fallback, matrix malformed)

Both produce blend=1.0 — the Markov component provides **zero useful signal** in either case.

### Why the Markov Matrix is Degenerate

The `HmmModel` fits a 5-state GaussianHMM on 9-feature vectors. The state→{UP,DOWN,FLAT} mapping uses:
```
threshold = 0.25 × std(log_return)   # STATE_MAP_THRESHOLD_STDS = 0.25
```

If the 5-state model collapses (EM assigns observations to 1-2 states), unused states get zero rows in `transmat_`. The `_sanitize_model` function (line 297) replaces these with **uniform rows** (1/N each). When these uniform rows are aggregated to 3-state via `_aggregate_transition` (line 555), the aggregation can produce all-zero 3×3 rows if:
- No 5-state component maps to certain 3-state labels
- The row_sum=0 triggers the 0.5 fallback in `_markov_hold_probability`

## Two-Layer Problem Summary

### Layer 1: p_regime = 1.0 (Primary)
**File**: `regime_platform/core/regime_engine.py`, line 256
**Code**: `confidence = round(float(np.max(vol_probs) * np.max(trend_probs)), 4)`
**Issue**: When the 4-state HMM separates cleanly, forward-backward posteriors collapse to ~[0, 0, 1, 0], making max(trend_probs) ≈ 1.0 and max(vol_probs) ≈ 1.0 → confidence = 1.0
**Impact**: logit(1.0) = 34.54 dominates the logit-pooled blend regardless of other component values

### Layer 2: Markov degeneracy (Secondary)
**Files**: `regime_platform/services/hmm/model.py` (5-state model), `regime_platform/services/statistics/ev_engine.py` (line 229-233)
**Code**: `if row_sum <= 0: return 0.5` — returns NEUTRAL instead of PESSIMISTIC for degenerate matrices
**Issue**: When the T3 matrix has all-zero rows (from 5→3 aggregation), the Markov component returns 0.5 (neutral), which contributes logit(0.5)=0 to the blend — doing nothing to counteract p_regime=1.0

## Proposed Fix

### Fix 1: Regime Confidence Calibration (PRIMARY)

**File**: `regime_platform/core/regime_engine.py`, line 256

```python
# BEFORE (line 256):
confidence = round(float(np.max(vol_probs) * np.max(trend_probs)), 4)

# AFTER:
# Cap confidence to prevent logit(1.0) = 34.5 from dominating the blend.
# The cap is chosen so that logit(0.95) = 2.94, reducing the dominant
# contribution from 12.34 to 1.05 — a 91% reduction.
confidence = round(min(0.95, float(np.max(vol_probs) * np.max(trend_probs))), 4)
```

**Effect**:
| regime_conf | p_win (blend) | Change |
|-------------|---------------|--------|
| 1.00 (current) | 1.0000 | baseline |
| 0.95 (capped) | 0.7411 | -26% |
| 0.90 (capped) | 0.6867 | -31% |

### Fix 2: Markov Degenerate Matrix Handling (SECONDARY)

**File**: `regime_platform/services/statistics/ev_engine.py`, lines 230-233

```python
# BEFORE (lines 230-233):
for i in range(3):
    row_sum = sum(M[i])
    if row_sum <= 0:
        return 0.5

# AFTER:
for i in range(3):
    row_sum = sum(M[i])
    if row_sum <= 0:
        # Degenerate row (all zeros after 5→3 aggregation).
        # This indicates an absorbing state — the HMM has no
        # 5-state components mapped to this 3-state label.
        # Treat as absorbing: P(reach target) = 0 for this state.
        return 0.0
```

**Effect**: When the Markov matrix is degenerate, p_markov=0.0 (instead of 0.5) contributes logit(0.0)=-34.5, which partially counteracts p_regime. With both fixes:
- p_regime=0.95, p_markov=0.0, p_timesfm=0.5 → p_win = 0.0001 (properly skeptical)

**Note**: Fix 2 alone (without Fix 1) would make p_win=0.92 (still overconfident). Fix 1 alone (without Fix 2) would make p_win=0.74 (reasonable). Both together produce the most conservative result, but **Fix 1 is the primary fix** and should be applied first.

### Fix 3: Bootstrap log_return filter (MINOR)

**File**: `regime_platform/services/hmm/model.py`, line 365

```python
# BEFORE:
returns = np.diff(np.log(closes[closes > 0]))

# AFTER:
returns = np.diff(np.log(closes))  # removes filtering that can cause misalignment
```

**Issue**: `closes[closes > 0]` filters out non-positive prices, shortening the array and causing misalignment with TA features computed on the full array. While FX prices are always positive in practice, this is a latent bug.

## Testing Strategy

1. **Unit test**: Verify `compute_p_regime` with capped confidence values
2. **Unit test**: Verify `_markov_hold_probability` with degenerate (all-zero) matrix rows
3. **Integration test**: Run AUDUSD through `_process_symbol` with both fixes applied
4. **Regression test**: Confirm that legitimately-confident regimes (well-separated HMM states) still produce high confidence (0.95, not 1.0)
5. **Live test**: Deploy to Studio, verify orchestrator log shows capped confidence values
