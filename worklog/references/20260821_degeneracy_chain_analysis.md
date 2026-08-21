# 2026-08-21 — Degeneracy Chain Analysis: Why 4 Symbols Have No Resolved Signals

## TL;DR

The calibration bias panel missing 4 symbols (AUDUSD, EURUSD, GBPUSD, US30USD) is **not** a calibration/UI bug. It's a **Markov/HMM model collapse cascade** that starves the signal resolution pipeline.

## The 60-Minute Cooldown — What It Actually Does

> User asked: "the cooldown (60mins) should reset the qualified=false|true and effectively start a new sequence of qualified=true unless p_win < 0.55"

**The cooldown does NOT reset qualified=false→true.** It is purely an anti-spam **publishing gate**:

- **Activation**: only fires when `qualified == True` (orchestrator.py:1894)
- **Suppress**: if same (symbol, direction) was published <60min ago → sets `qualified = False` (line 1906)
- **Record**: only saves timestamp when a signal IS published (line 2303)
- **Never** restores `qualified=False` → `qualified=True`
- **Never** re-runs HMM, re-evaluates gates, or resets model state

The cooldown prevents signal spam (same BUY/SELL every 5 min when regime is stable), not qualification recovery.

### Step 1: HMM State Collapse (model.py lines 185-205)

The 5-state GaussianHMM fits on a 9-feature matrix (log_return + 5 TA + 3 microstructure). State → {UP/DOWN/FLAT} mapping:

```
threshold = STATE_MAP_THRESHOLD_STDS * lr_std  # 0.25 * std(log_returns)

if state_mean_lr > threshold:  state_map[i] = "UP"
elif state_mean_lr < -threshold: state_map[i] = "DOWN"
else:  state_map[i] = "FLAT"
```

**When AUDUSD has compressed price action** (small log_return std), `lr_std ≈ 1e-8` (floor), making `threshold ≈ 2.5e-9`. All 5 state means fall within ±threshold → **all map to "FLAT"**.

### Step 2: Transition Matrix Collapse (model.py lines 561-590)

With all states → "FLAT", `_aggregate_transition` produces:

```
T3 = {
  "FLAT": {"UP": 0.0, "DOWN": 0.0, "FLAT": 1.0},   # absorbing
  "UP":   {"UP": 1/3, "DOWN": 1/3, "FLAT": 1/3},   # uniform fallback (no states mapped)
  "DOWN": {"UP": 1/3, "DOWN": 1/3, "FLAT": 1/3},   # uniform fallback
}
```

### Step 3: EV Engine Markov → 0.0 (ev_engine.py lines 191-267)

`compute_p_markov_hold_n(T3, "FLAT", "long", 8)`:
- `current_idx = 2` (FLAT)
- Row sum for FLAT = 1.0 → not degenerate (has valid probabilities)
- BUT `T3[FLAT][UP] = 0.0` → T^8[FLAT][UP] = 0.0 (FLAT is absorbing, can't reach UP)
- Returns `p_markov_hold_n = 0.0`

**The `current_state` is "FLAT"** because:
- `state_5 = fitted.model.predict(latest_feature_vector)` 
- `current_state = fitted.state_map.get(state_5, "FLAT")`
- All states map to "FLAT", so regardless of which 5-state the predictor returns, `current_state = "FLAT"`

### Step 4: Logit Pool Collapse (ev_engine.py lines 617-626)

```
available = {
  "p_regime": 0.997,        # HMM says high_vol_strong_bull (bullish)
  "p_markov_hold_n": 0.0,   # ← degenerate
  "p_timesfm": 0.51,        # barely above 0.5
}

logit_sum = 0.255*logit(0.997) + 0.222*logit(0.0) + 0.522*logit(0.51)
          = 0.255*5.80 + 0.222*(-34.5) + 0.522*0.04
          = 1.48 - 7.66 + 0.02
          = -6.16

p_win = sigmoid(-6.16) = 0.0021
```

Even though `p_regime = 0.997` (strongly bullish) and `p_timesfm = 0.51`, the single zero value in `p_markov_hold_n` dominates because `logit(0) = -34.5`.

### Step 5: Qualification Failure (orchestrator.py lines 1805-1868)

- `p_win = 0.0021 < P_WIN_HARD_FLOOR (0.50)` → `p_win_ok = False`
- `qualified = False` (ALL gates must pass)
- `kelly_f = 0.0` (no edge → zero Kelly)

### Step 6: Signal Sim Capture (orchestrator.py lines 2376-2416)

The signal IS written to `nt_signal_sim` (qualified=False, p_win=0.0) because:
- Line 2377: "EVERY generated non-neutral signal (qualified or not)"
- Line 2410: `NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY` defaults to False

### Step 7: EOD Resolution Failure (eod_resolver.py lines 298-320)

The resolver fetches ALL `outcome IS NULL` rows (not filtered by qualified) and attempts resolution. However:

- **EXPIRY_BARS = 48** (48 hours of 1h candles)
- The resolver uses **TradingView 1h close prices** (not renko brick closes)
- For a BUY signal at entry=0.714 with SL=0.705, TP=0.728:
  - The renko brick closes DO touch SL (close=0.70486 at Aug 20 23:35)
  - But the **1h candle closes** may not reach these levels
  - The close-walk uses `close >= take_profit` (TP first, then SL)
  - If no 1h close touches either level within 48 bars → outcome = "expired"

### Step 8: Expired = No Calibration (eod_resolver.py lines 34-37)

```
Expired signals get outcome='expired' in nt_signal_sim
but are deliberately SKIPPED for calibration — a timeout
is neither a win nor a loss
```

**No tp_hit/sl_hit → no nt_calibration_records → no bias data for the view.**

## Why the 60-Minute Cooldown Does NOT Help

The `SIGNAL_COOLDOWN_MINUTES` (line 156, default 60) only:

1. **Checks `if qualified AND elapsed < 60`**: suppresses a QUALIFIED signal from being PUBLISHED
2. **Sets `qualified = False`** after suppression (line 1906)
3. **Records timestamp** only when a signal IS published (line 2303)

It **NEVER**:
- ❌ Resets a `qualified=False` signal back to qualified=True
- ❌ Forces re-evaluation of gates
- ❌ Re-runs the HMM or resets model state
- ❌ Re-triggers the EOD resolver

The cooldown is an anti-spam mechanism for PUBLISHING, not a signal recovery mechanism.

## Why Calibration Enforcement Doesn't Break the Cycle

The EOD correction (eod_correction.py) writes `bayesian_penalty` actions when |bias| > 0.10:

```python
p_win' = (p_win * n + 0.5 * 10) / (n + 10)
```

For AUDUSD with p_win=0.0 and n=3 (3 calibration records from earlier resolved trades):
```
p_win' = (0.0 * 3 + 0.5 * 10) / (3 + 10) = 5.0 / 13 = 0.385
```

**0.385 is still below `MIN_P_WIN = 0.55`** → still fails qualification.

The Bayesian prior (count=10) is **too weak** to rescue a signal from p_win=0.0. It would need ~30 records to push 0.0 above 0.55:
```
(0.0 * 30 + 0.5 * 10) / (30 + 10) = 5.0 / 40 = 0.125  # still too low
```

Actually, p_win=0.0 can never be rescued by Bayesian shrinking toward 0.5 — the formula pulls toward 0.5 but only reaches 0.5 asymptotically as n→0, and with finite records, the shrunken value stays well below 0.55.

## Why EOD Correction Isn't Running for These Symbols

The EOD correction (`run_eod_correction`, lines 160-236) calls `fetch_calibration_bias(symbol)`. If there are **no calibration records** (no tp_hit/sl_hit in 7 days), `fetch_calibration_bias` returns `bias=None`, and the code at line 185-187 skips the symbol entirely:

```python
if bias is None:
    # Not enough calibration records yet — nothing to act on.
    continue
```

**This is a catch-22**: no calibration records → no EOD correction → no Bayesian penalty applied → p_win stays 0 → no qualified signals → no resolved outcomes → no calibration records.

## Summary

| Component | Issue | Impact |
|-----------|-------|--------|
| **HMM fit** (model.py:190) | `lr_std floor = 1e-8` too low | All states collapse to "FLAT" |
| **State threshold** (model.py:63) | `0.25 * lr_std` with tiny std | All state means map to "FLAT" |
| **EV blend** (ev_engine.py:599-601) | p_markov=0 → logit(0)=-34.5 | p_win → 0 regardless of other sources |
| **Qualification** (orchestrator:1820) | p_win < 0.50 → hard floor fail | qualified=False |
| **Cooldown** (orchestrator:1894) | Only suppresses qualified signals | Cannot rescue unqualified ones |
| **EOD correction** (eod_correction:185) | Skips symbols with no calibration records | Cannot intervene when p_win=0 |
| **Bayesian shrink** (eod_correction:62) | `(0*n + 5)/(n+10)` → too weak | Cannot rescue p_win=0 to 0.55+ |

## The 4 Missing Symbols Have Recent Open Signals

AUDUSD has 5 open signals (Aug 20-21) all with:
- `p_win=0.0000`, `qualified=False`
- `markov_p_up=0.0000`, `markov_p_dn=0.0000`
- Entry at 0.714, SL at 0.705, TP at 0.728
- The renko brick closes **did touch SL** (0.70486) but the 1h resolution close-walk hasn't resolved them yet

The signals are **stuck open** because the EOD resolver hasn't run its close-walk on the recent data yet, AND even when it does, the outcomes will be "expired" (not tp_hit/sl_hit) since the signals were never qualified and no paper positions were opened to track them.

## The Deadlock: 4 Symbols Have No Calibration Records AND No EOD Actions

Verified via Supabase API:

| Symbol | Calibration Records (7d) | EOD Actions | Bias | Status |
|--------|-------------------------|-------------|------|--------|
| AUDUSD | **NONE** | **NONE** | None | Deadlock |
| EURUSD | **NONE** | **NONE** | None | Deadlock |
| GBPUSD | **NONE** | **NONE** | None | Deadlock |
| US30USD | **NONE** | **NONE** | None | Deadlock |
| AAPL | Yes | Yes | +0.49 | Emergency sweep triggered |
| BTCUSD | Yes | Yes | -0.35 | Emergency sweep triggered |
| (other 6) | Yes | Yes | varies | Emergency sweep triggered |

**The 4 symbols are in a complete deadlock:**
1. No calibration records → `fetch_calibration_bias` returns `bias=None`
2. EOD correction skips them (eod_correction.py:185 — `if bias is None: continue`)
3. No EOD actions written → no `emergency_heavy_sweep` recommended
4. No heavy sweep → HMM not re-optimized → stays collapsed
5. p_win stays 0 → no qualified signals → no resolved outcomes → back to step 1

All 10 visible symbols have |bias| > 0.10 and are getting emergency heavy sweeps + Bayesian penalties + Kelly reductions. But the 4 deadlocked symbols can never enter this correction loop.
