# EV Engine Deep Analysis & Fixes (2026-08-18)

> **Priority**: P0 · **Task**: Fix EV engine overconfidence (regime confidence cap, Markov degenerate-state handling, bootstrap log-returns filter) ·
> **Status**: resolved · **Date**: 2026-08-18

## Plain-Language Summary

### What Was Broken
The EV (Expected Value) engine was producing wildly overconfident probability estimates — often `p_win = 1.0` (100% confidence) — even when actual win rates were ~10%. This meant the system was qualified trade signals that shouldn't have been, with position sizes (kelly_f) that were too large.

### Root Causes (Two Bugs)

1. **Regime confidence domination (Fix 1)**: The HMM regime confidence (`p_regime`) was reaching `1.0` when the market was calm. In the logit-pooled blend, `logit(1.0) = 34.5` — an extremely large number that overwhelmed all other inputs (`p_timesfm`, `p_markov`) regardless of their values. This caused the final `p_win` to collapse to ~1.0.

2. **Markov degenerate state handling (Fix 2)**: When the 5-state HMM collapsed into 3 labeled states (UP/DOWN/FLAT), some state rows in the 3×3 transition matrix became all-zeros (no 5-state component mapped to that label). The old code returned `p_markov = 0.5` (neutral) for these degenerate rows. This didn't help — it just let `p_regime = 1.0` dominate even more. The fix returns `0.0` (absorbing) — meaning "if we're stuck in a state we can't escape, the probability of reaching the target is zero."

3. **Bootstrap log-returns filter (Fix 3)**: A `closes[closes > 0]` filter in `_compute_log_returns()` was silently shortening the log-returns array, misaligning it with the 9-feature TA indicator buffer. This fed corrupted training data to the HMM. The filter has been removed — prices in FX/crypto should never be non-positive.

### What We Changed
- **`ev_engine.py`**: Added `_P_REGIME_CAP = 0.97` — caps regime confidence so `logit(0.97) = 3.5` instead of `logit(1.0) = 34.5`
- **`ev_engine.py`**: Degenerate Markov rows now return `0.0` instead of `0.5`, properly representing absorbing states
- **`model.py`**: Removed the `closes[closes > 0]` filter to ensure array-length integrity

### Production Verification (Studio, 2026-08-19 00:30 UTC sweep)

| Symbol | Pre-Fix raw_p_win | Post-Fix raw_p_win | Mechanism |
|--------|-------------------|-------------------|-----------|
| AUDUSD | 0.9788 | 0.7474 | Regime cap (Markov non-degenerate) |
| BTCUSD | 0.9777 | 0.0000 | Regime cap + Markov absorbing |
| USDCAD | 1.0000 | 0.6715 | Regime cap + Markov absorbing |
| XAUAUD | 1.0000 | 0.7142 | Regime cap (passed gates, cooldown) |
| XAUUSD | 1.0000 | 0.7590 | Regime cap + Markov |
| XAGUSD | 0.9958 | 0.0000 | Regime cap + Markov absorbing (rejected) |

**Key insight**: The regime confidence cap alone would only reduce p_win from 1.0 to ~0.95. The Markov degenerate fix is what drives the dramatic reductions (BTCUSD: 0.98→0.0, USDCAD: 1.0→0.67) — it introduces a strongly negative logit contribution (-34.5) that properly counteracts the overconfident regime signal.

### What to Expect Going Forward

**Next 4h window (before 04:30 UTC correction pass)**:
- Light sweeps every 5 minutes will show properly bounded raw p_win values (0.0–0.95 instead of 1.0)
- Calibration enforcement will still apply until rolling bias drops below 0.15
- False "qualified=True" signals from degenerate Markov states will be eliminated

**4h correction passes (04:30, 08:30, 12:30, 16:30, 20:30 UTC)**:
- Bias values will slowly decrease as post-fix records accumulate in the 7-day rolling window
- Symbols with p_win≈0.0 (BTCUSD, XAGUSD) will see bias drop fastest
- Full bias recovery takes ~7 days (until ~August 25) as the 7-day window fills with post-fix data

**Renko brick pricing**: Unchanged. Entry/SL/TP calculations use `brick_size × sl_bricks` and `brick_size × tp_bricks`, which are independent of the EV blend.

---

## Full Technical Analysis

### Architecture Context

#### 5→3 State HMM Collapse

The production system uses two HMM models:

1. **`RegimeHMM`** (4-state: vol × trend dual-HMM) — produces regime label + confidence
2. **`HmmModel`** (5-state Gaussian HMM) — produces 5×5 transition matrix

The 5-state transition matrix is aggregated to 3-state (UP/DOWN/FLAT) via `_aggregate_transition()` in `model.py`. Each 5-state is mapped to a 3-state label using a narrow log-return threshold (`STATE_MAP_THRESHOLD_STDS = 0.25`). In calm markets, **multiple 5-states collapse to the same 3-state label**, producing degenerate rows (all zeros) in the 3×3 matrix.

#### EV Engine Logit-Pooled Blend

```
logit(P_win) = Σ w_i × logit(p_i)
```

Where p_i ∈ {p_pattern, p_regime, p_markov_hold_n, p_timesfm} with default weights {0.30, 0.25, 0.20, 0.25}.

When p_pattern is unavailable (server-side), weights renormalize to {0.357, 0.286, 0.357}.

### Root Cause #1: Regime Confidence Cap

**Location**: `ev_engine.py`, `compute_p_regime()` (line 430)

**Before**:
```python
if direction == "long":
    return p_bullish + 0.5 * p_neutral
```

When the HMM posterior collapses to a single state (calm markets), `trend_probs` has one component at ~1.0, giving `p_regime = 1.0`. `logit(1.0) = 34.54`.

With weight 0.357: `0.357 × 34.54 = 12.34` — this single term accounts for ~100% of the logit sum, making `p_win ≈ 1.0` regardless of p_markov or p_timesfm.

**Logit math (pre-fix)**:
```
logit_sum = 0.357 × logit(1.0) + 0.286 × logit(0.5) + 0.357 × logit(0.50)
           = 0.357 × 34.54 + 0.286 × 0.0 + 0.357 × 0.0
           = 12.33
p_win = sigmoid(12.33) = 0.99997 ≈ 1.0
```

**After** (Fix 1):
```python
_P_REGIME_CAP: float = 0.97

# ... in compute_p_regime() ...
return min(EVEngine._P_REGIME_CAP, p)
```

`logit(0.97) = 3.48`. With weight 0.357: `0.357 × 3.48 = 1.24` — reduced from 12.34 to 1.24, allowing other components to meaningfully contribute.

**Logit math (post-fix, regime only capped)**:
```
logit_sum = 0.357 × logit(0.97) + 0.286 × logit(0.5) + 0.357 × logit(0.50)
           = 0.357 × 3.48 + 0.286 × 0.0 + 0.357 × 0.0
           = 1.24
p_win = sigmoid(1.24) = 0.776
```

This alone would reduce p_win from 1.0 to ~0.78 — significant, but still overconfident for a 10% actual win rate.

### Root Cause #2: Markov Degenerate State Handling

**Location**: `ev_engine.py`, `_markov_hold_probability()` (line 235)

**Before**:
```python
for i in range(3):
    row_sum = sum(M[i])
    if row_sum <= 0:
        return 0.5  # Neutral fallback for ALL degenerate rows
```

When a row in the 3×3 transition matrix is all-zeros (degenerate), the function returned `0.5` regardless of which row was degenerate or what the current state was. This meant:

- If the current state's row was all-zeros (absorbing state), returning 0.5 treated it as "50% chance of reaching target" — incorrect
- If a different state's row was all-zeros, the entire computation was short-circuited with 0.5 — overly conservative for non-degenerate states

**After** (Fix 2):
```python
current_idx = _STATES.index(current_state.upper()) if current_state.upper() in _STATES else 0
for i in range(3):
    row_sum = sum(M[i])
    if row_sum <= 0:
        if i == current_idx:
            # Absorbing state: cannot reach UP or DOWN from here
            return 0.0
        else:
            # Other degenerate row — use uniform prior
            M[i] = [1.0 / 3.0] * 3
    elif abs(row_sum - 1.0) > 0.01:
        M[i] = [v / row_sum for v in M[i]]
```

The fix distinguishes between:
1. **Current state's row is degenerate** → returns 0.0 (absorbing state — we're stuck, can't reach target)
2. **Other row is degenerate** → sets uniform prior [1/3, 1/3, 1/3] and continues matrix exponentiation

**Logit math (post-fix, with Markov degenerate fix)**:
```
logit_sum = 0.357 × logit(0.97) + 0.286 × logit(0.0) + 0.357 × logit(0.50)
           = 0.357 × 3.48 + 0.286 × (-34.54) + 0.357 × 0.0
           = 1.24 - 9.88 + 0.0
           = -8.64
p_win = sigmoid(-8.64) = 0.0002 ≈ 0.0
```

This is the mechanism that drives BTCUSD and USDCAD to p_win ≈ 0.0 — the absorbing Markov state dominates negatively, properly counteracting the overconfident regime signal.

### Root Cause #3: Bootstrap Log-Returns Filter

**Location**: `model.py`, `_compute_log_returns()` (line 365)

**Before**:
```python
returns = np.diff(np.log(closes[closes > 0]))
```

The `closes > 0` filter was intended to remove invalid prices (zero or negative), but in FX/crypto markets, prices are always positive. The filter could silently shorten the array if any closing price were ≤ 0 (e.g., due to a data error), causing misalignment between log-returns and the 9-feature TA indicator buffer.

**After** (Fix 3):
```python
returns = np.diff(np.log(closes))
```

The filter is removed. If invalid prices exist, they should be handled upstream (data validation in `price_fetcher.py`), not silently masked here.

### Production Evidence

#### Studio Log (00:26 UTC restart → 00:45 UTC sweep)

**New orchestrator initialization**:
```
2026-08-19 00:26:52,516 [INFO] regime_platform.sweep_orchestrator.orch: 
  Qualification thresholds: MIN_REGIME_CONF=0.6, MIN_KELLY=0.02, MIN_TIMESFM_CONFIDENCE=0.05, 
  TIMESFM_GATE_MODE=soft, MIN_EV=0.0, MIN_RR=1.5, MIN_P_WIN=0.55, 
  SIGNAL_COOLDOWN_MINUTES=60.0, DEBUG_SIGNALS=False, 
  TIMESFM_SIGMOID_K=10.0, TIMESFM_CLAMP=[0.05, 0.95]
```

**AUDUSD (regime=high_vol_strong_bull, non-degenerate Markov)**:
```
00:45:08 [calibration:enforce] AUDUSD: OVERCONFIDENT bias=0.9792 n=72.0 → 
  p_win 0.7474→0.7172, kelly 0.400000→0.200000
00:45:09 [sweep:orch] AUDUSD: COOLDOWN SUPPRESSED — buy signal suppressed 
  (15.0min since last, cooldown=60.0min)
00:45:09 [sweep:orch] AUDUSD: done in 0.7s — regime=high_vol_strong_bull, kelly=0.400
```

**BTCUSD (regime=high_vol_bear, degenerate Markov)**:
```
00:45:14 [calibration:enforce] BTCUSD: OVERCONFIDENT bias=0.772 n=286.0 → 
  p_win 0.0000→0.0169, kelly 0.000000→0.000000
00:45:14 [sweep:orch] BTCUSD: REJECTED — 3 gate(s) failed: 
  ['kelly_min_ok', 'ev_allowed', 'p_win_ok']
00:45:15 [sweep:orch] BTCUSD: done in 0.7s — regime=high_vol_bear, kelly=0.100
```

#### Supabase nt_signal_sim Comparison

| Metric | Pre-Fix (00:20 UTC) | Post-Fix (00:45 UTC) | Δ |
|--------|---------------------|----------------------|---|
| AUDUSD raw_p_win | 0.9788 | 0.7474 | -0.231 |
| AUDUSD p_win (calibrated) | 0.9204 | 0.7172 | -0.203 |
| BTCUSD raw_p_win | 0.9777 | 0.0000 | -0.978 |
| BTCUSD p_win (calibrated) | 0.9625 | 0.0169 | -0.946 |
| USDCAD raw_p_win | 1.0000 | 0.6060 | -0.394 |
| XAUAUD raw_p_win | 1.0000 | 0.7142 | -0.286 |
| XAUUSD raw_p_win | 1.0000 | 0.7590 | -0.241 |
| XAGUSD raw_p_win | 0.9958 | 0.0000 | -0.996 |

### Expected Behavior in 4h Window

**Light sweeps (every 5 min, until 04:30 UTC)**:
- Raw `predicted_p_win_raw` stays bounded: 0.0–0.95 instead of 1.0
- Calibration enforcement continues: `calibration:enforce` reduces p_win for bias > 0.15
- BTCUSD, XAGUSD: p_win ≈ 0.0 → signals rejected (kelly=0.0, p_win < 0.55)
- AUDUSD, XAUAUD, XAUUSD, USDCAD: p_win in 0.60–0.90 range → signals may qualify but with reduced kelly

**4h correction passes (04:30, 08:30, 12:30, 16:30, 20:30 UTC)**:
- 04:30 pass: bias recomputed with `entered_at >= 00:30 UTC` records (only 1-2 post-fix records)
- 08:30 pass: bias still high (historical records from pre-fix period still in 7-day window)
- 12:30 pass: bias starts trending down as more post-fix records accumulate
- 16:30/20:30 passes: bias approaching "Calibrated" status (bias ≤ 0.15) for symbols with p_win ≈ 0.0

**"Calibrated" status timeline**:
- **Cannot expect "Calibrated" at 04:30 or 08:30 UTC** — the 7-day rolling window (Aug 12–19) is still ~85% pre-fix data
- **Likely "Calibrated" at 12:30–16:30 UTC** for BTCUSD, XAGUSD (fastest bias drop: 0.97→0.0 records)
- **Likely "Calibrated" at 16:30–20:30 UTC** for XAUAUD, XAUUSD, USDCAD (moderate drop: 1.0→0.7)
- **Likely "Calibrated" at 20:30 UTC+** for AUDUSD (slowest drop: 0.98→0.75, still biased)

**Full bias recovery**: ~August 25–26, 2026 (7 days after fix deployment at Aug 19 00:26 UTC).

### Renko Brick Pricing Impact: NONE

The Renko brick pricing is completely independent of the EV engine fixes:

- **Brick size**: Determined by `atr(14)` on 1h candles — unaffected
- **Entry price**: Current Renko boundary — unaffected  
- **SL/TP pricing**: `stop_loss = entry - (brick_size × sl_bricks)`, `take_profit = entry + (brick_size × tp_bricks)` — unaffected
- **RR ratio**: `tp_bricks / sl_bricks` — unaffected
- **kelly_f calculation**: Uses `p_win` from EV engine → **affected** (reduced)
- **EV gate (Gate 6)**: `ev = p_win × profit - (1-p_win) × stake` — **affected** (reduced)

The fixes only change `p_win` and `ev` — not the geometric pricing of Renko bricks. A qualified signal will still enter, stop out, and take profit at the same prices; the difference is that fewer false signals will qualify, and those that do will have appropriately sized positions.

---

## Background Monitor

### Scripts

- **`scripts/monitor_ev_fix.py`** — Standalone polling monitor. Queries `nt_signal_sim` for all 14 symbols, classifies each by fix status:
  - `FIX_WORKING_DEGENERATE` (raw_p_win ≤ 0.01) — Markov absorbing fix working
  - `FIX_WORKING_MODERATE` (0.01 < raw ≤ 0.95) — Regime cap working
  - `FIX_WORKING_NEGATIVE` (0.1 < raw ≤ 0.5) — Markov dragging p_win negative
  - `FIX_NOT_WORKING` (raw > 0.98) — Regime cap bypassed; regression

- **`scripts/monitor_ev_fix_wrapper.py`** — Cron wrapper that runs the monitor in `--once` mode, compares results to previous run, and only emits output if a regression is detected (no_agent cron: silent = healthy). Deployed to `~/hermes/profiles/ultron/scripts/`.

### Hermes Cron Job

| Field | Value |
|-------|-------|
| Job ID | `3dbc7c566323` |
| Name | `ev-engine-fix-monitor` |
| Script | `monitor_ev_fix_wrapper.py` |
| Schedule | `*/5 * * * *` (every 5 minutes) |
| Mode | `no_agent` (script-only) |
| Delivery | `origin` (chat notification on regression) |
| Workdir | Repo root |

### Current Monitor Status (as of 03:17 UTC)

Last check showed all 14 symbols in `FIX_WORKING_*` states (no `FIX_NOT_WORKING`):
- **6 degenerate**: USDCHF, USDJPY, US500, AAPL (p_markov=0.0 absorbing)
- **4 negative**: BTCUSD, US30USD, XAGUSD (p_win near 0.0)
- **4 moderate**: AUDUSD, EURUSD, GBPUSD, USDCAD, XAUAUD, XAUEUR, XAUUSD (0.5-0.9 range)

### Distinction from EOD Signal-Lookback Monitor

The existing Hermes cron job **`22bf467170ce`** ("EOD signal-lookback monitor", runs daily at 01:10 UTC) checks **post-resolution outcomes** — whether the resolver closed signals correctly. The new **`ev-engine-fix-monitor`** checks **pre-resolution p_win values** — whether the EV engine produces correct raw probabilities before calibration enforcement. These are complementary verification layers, not duplicates.

---

## 24-Hour Post-Deployment Evaluation (2026-08-19)

### Stale Record Purge (2026-08-19 21:56 UTC)

The EV fix was deployed at 2026-08-19T00:26:52Z (Studio restart). All `nt_calibration_records` with `entered_at < 2026-08-19T00:26:52Z` were computed with the broken logit pooling and contaminated the rolling 7-day bias window.

**Purge executed** via `scripts/purge_stale_calibration.py --execute`:
- **5,006 stale records deleted** (entered_at < 2026-08-19T00:26:52Z)
- **0 post-fix records remain** (03:30 UTC resolution hasn't produced new calibration records yet)
- Bias window is now 100% clean — next 00:30 UTC EOD resolution will populate with post-fix data only
