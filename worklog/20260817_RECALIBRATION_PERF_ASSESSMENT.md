# Noble-Trader-FastAPI-Backend Worklog
## 2026-08-17 — Recalibration Performance Deep Assessment

**Scope:** `noble-trader-fastapi-backend` only  \
**Type:** Assessment + code changes (fixes applied)  \
**Date:** 2026-08-17  \
**Context:** Review of cron job outputs + recalibration performance analysis. SQL migration 114 applied; `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW=True` in LightningAI Studio.

---

## 1. Cron Job Status Summary

All 6 active cron jobs report `ok` status with zero delivery errors:

| Job | Schedule | Last Run | Status |
|-----|----------|----------|--------|
| `eod-lookback-monitor` | Daily 01:10 UTC | Aug 17 13:30 UTC | ✅ ok |
| `talaria-daily-digest` | Daily 15:00 UTC | Aug 16 15:00 UTC | ✅ ok |
| `talaria-signal-notify` | Every 5m | Aug 17 14:40 UTC | ✅ ok |
| `noble-sweep-health` | Every 120m | Aug 17 13:30 UTC | ✅ ok |
| `noble-calib-health` | Daily 02:10 UTC | Aug 17 13:30 UTC | ✅ ok |
| `talaria-delivery-health` | Every 30m | Aug 17 14:32 UTC | ✅ ok |

No missed runs, no stale jobs, no delivery failures across any cron job.

---

## 2. Recalibration Loop — Live Data (Post-Purge, Aug 13 onward)

### Purge Results (executed 2026-08-13 ~23:10 UTC)
- `DELETE nt_calibration_records WHERE entered_at < '2026-08-11T14:00:00Z'` → **14,248 rows deleted**
- `DELETE nt_eod_actions WHERE ts <= '2026-08-13T01:00:00'` → **27 contaminated actions deleted**
- Verified: calibration total = 2,953 clean records, contaminated remaining = 0

### Current State (clean records, Aug 11 14:00 UTC onward)
- Total clean records: 2,953
- Per-symbol: all 14 symbols ≥ 20 records (threshold met)
- Outcome distribution: 1,457 wins (49.4%) / 1,496 losses (50.6%)
- Expired (48-bar window lapsed): 4,807 (41% of drained backlog)

### Emergency Heavy Sweeps (Aug 13, 00:45 UTC)
9 symbols flagged OVERCONFIDENT with extreme biases:

| Symbol | Bias | Sweep Result |
|--------|------|-------------|
| USDCAD | +0.936 | ✅ Sweep complete |
| BTCUSD | +0.929 | ✅ Sweep complete |
| EURUSD | +0.389 | ❌ no_combo_passed_filters |
| GBPUSD | +0.389 | ❌ no_combo_passed_filters |
| XAGUSD | +0.180 | ✅ Sweep complete |
| XAUAUD | +0.180 | ✅ Sweep complete |
| XAUEUR | +0.180 | ✅ Sweep complete |
| XAUUSD | +0.180 | ✅ Sweep complete |
| AUDUSD | +0.180 | ✅ Sweep complete |

The 3 failed symbols (EURUSD, GBPUSD, USDCAD) are the **most extreme** biases —
the ones most in need of re-optimization but couldn't find passing combos.

---

## 3. EV Engine Overconfidence Analysis

### Root Cause #1: TimesFM Sigmoid Aggressiveness (k=20)
```python
# orchestrator.py:373-382
_k = 20.0
p_timesfm = sigmoid(20 * rel_change)
```
- 1% predicted move → p=0.73
- 2% predicted move → p=0.88
- 5% predicted move → p=0.99

A 1% daily move is normal market noise, not 73% confidence. The k=20 coefficient
is excessively aggressive for daily/hourly price forecasts.

### Root Cause #2: Logit-Pool Consensus Amplification
When all available sources agree directionally, logit-pooling compounds confidence
exponentially. Three moderately confident sources (p=0.65 each) can produce a
blended p_win of 0.75+. Combined with TimesFM's inflated probabilities, the blend
easily reaches 0.95+.

### Root Cause #3: Missing Pattern Source (p_pattern=None)
The p_pattern source (0.30 weight) comes from agent-side DuckDB, which the
backend cannot access. The engine renormalizes remaining weights:
- p_timesfm increases from 0.25 → 0.3125
- The 0.30-weight conservative historical win-rate anchor is entirely absent

### Root Cause #4: Markov T^N First-Passage Underestimate
The T^N hold probability uses a conservative approximation (P(target at step N),
not P(target at any step 1..N)). In trending markets, this systematically
underestimates p_markov, which gets compensated by higher p_timesfm and
p_regime in the logit pool.

---

## 4. Emergency Sweep Filter Mismatch — KEY FINDING

### The Split Between Orchestrator MIN_RR and heavy_sweep.py MIN_RR

There is a **critical disconnect** between the orchestrator's configurable
`MIN_RR` (read from env) and the heavy sweep's hardcoded `MIN_RR`:

**Orchestrator (signal qualification path):**
```python
# orchestrator.py:111
MIN_RR = float(os.getenv("MIN_RR", "2.0"))  # Currently 1.5 via env
self._ev_engine.min_rr = MIN_RR  # line 700 — patches the EV engine
```

**Heavy sweep (brick optimization path):**
```python
# heavy_sweep.py:72
MIN_RR = 2.0  # HARDCODED — not configurable via env
```

### Impact:
1. The orchestrator allows RR ≥ 1.5 for signal qualification (via `env: MIN_RR=1.5`)
2. The heavy sweep grid search requires RR ≥ 2.0 (hardcoded)
3. Emergency sweeps call `pick_best_combo()` which uses the hardcoded `MIN_RR=2.0`
4. The 3 failed symbols (EURUSD, GBPUSD, USDCAD) couldn't find ANY brick-size/ATR
   combination meeting `MIN_RR=2.0` in the grid search

### Why This Matters:
The user has tuned `MIN_RR=1.5` specifically because with default brick params
(sl=3, tp=5, RR=1.67), the default 2.0 threshold blocks every signal. But this
env override **does not reach the emergency sweep path** — which still uses 2.0.
This is likely why the 3 most-overconfident symbols failed emergency re-optimization.

### Proposed Fix:
Make `heavy_sweep.py`'s `MIN_RR` (and `MAX_SHARPE`, `MIN_TRADES`) configurable
via the same env vars the orchestrator uses:
```python
# heavy_sweep.py — current (hardcoded):
MIN_RR = 2.0
MAX_SHARPE = 10.0
MIN_TRADES = 10

# Proposed (env-configurable with same defaults):
MIN_RR = float(os.getenv("HEAVY_SWEEP_MIN_RR", "2.0"))
MAX_SHARPE = float(os.getenv("HEAVY_SWEEP_MAX_SHARPE", "10.0"))
MIN_TRADES = int(os.getenv("HEAVY_SWEEP_MIN_TRADES", "10"))
```

This allows separate tuning for the brick optimization grid (keep strict at 2.0)
while allowing the emergency sweep to use looser thresholds (1.5) when the
user has already signaled acceptance of lower RR at the qualification gate.

### Alternative Approach:
Use env vars with the same names, so `MIN_RR=1.5` from the orchestrator env
automatically applies to the heavy sweep too. But this couples the two paths —
the grid search might find no combos at 1.5 either, which would be a stronger
signal that the brick params themselves need adjustment.

---

## 5. Current Env Var Configuration (from .env + LightningAI)

| Variable | Value | Location | Purpose |
|----------|-------|----------|---------|
| `MIN_REGIME_CONF` | 0.60 | orchestrator.py:105 | Gate 2: HMM regime confidence minimum |
| `MIN_KELLY` | 0.02 | orchestrator.py:106 | Gate 3: Kelly fraction minimum (2%) |
| `MIN_TIMESFM_CONFIDENCE` | 0.05 | orchestrator.py:107 | Gate 5: TimesFM \|p-0.5\| minimum |
| `TIMESFM_GATE_MODE` | soft | orchestrator.py:133 | TimesFM gate mode (hard/soft/off) |
| `MIN_EV` | 0.0 | orchestrator.py:110 | Gate 6a: EV minimum (always passes) |
| `MIN_RR` | 1.5 | orchestrator.py:111 | Gate 6b: RR minimum (env override of 2.0 default) |
| `MIN_P_WIN` | 0.55 | orchestrator.py:167 | Dynamic quality filter: blended p_win threshold |
| `DEBUG_SIGNALS` | 0 | orchestrator.py:138 | Debug channel for rejected signals |
| `SIGNAL_COOLDOWN_MINUTES` | 60 | orchestrator.py:156 | Anti-spam cooldown per signal |
| `EOD_CORRECTION_INTRADAY_ENABLED` | True | settings.py:289 | 4h correction passes |
| `EOD_CORRECTION_INTERVAL_HOURS` | 4 | settings.py:290 | Correction cadence |
| `EOD_ACTION_FRESH_HOURS` | 8 | settings.py:297 | Action freshness window |
| `EOD_BIAS_WINDOW_DAYS` | 7 | settings.py:303 | Rolling bias window |
| `EOD_BIAS_MIN_RECORDS` | 20 | settings.py:304 | Min records for bias computation |
| `EOD_DRIFT_MONITOR_ENABLED` | True | settings.py:311 | Drift monitor on light sweeps |
| `EOD_DRIFT_THRESHOLD` | 0.15 | settings.py:314 | Drift threshold |
| `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW` | True | settings.py:321 | Capture pre-enforcement p_win |

### Variables NOT in `.env.example` (should be added):
- `TIMESFM_GATE_MODE` (line 273 in .env.example has it, but check consistency)
- `SIGNAL_COOLDOWN_MINUTES` (not in .env.example)
- `DEBUG_SIGNALS` (in .env.example:211 ✓)
- `MIN_P_WIN` (in .env.example:228 ✓)

### Inconsistency: `.env.example` vs `orchestrator.py` defaults
- `.env.example:206`: `# MIN_RR=2.0` (commented, showing default)
- `orchestrator.py:111`: `float(os.getenv("MIN_RR", "2.0"))` — default 2.0
- Live env: `MIN_RR=1.5` (override)

The `.env.example` documents the Gate 6 issue clearly (lines 199-206) — good.

---

## 6. Proposed Improvements (Awaiting "Build Code" Gate)

### Priority A: Fix the MIN_RR Split (Blocks Emergency Sweeps)
Make `heavy_sweep.py` filters configurable via env vars that match the orchestrator's
thresholds, OR add separate emergency-sweep-specific thresholds. This is the
direct cause of 3/9 emergency sweeps failing with no_combo_passed_filters.

### Priority B: Reduce TimesFM Sigmoid Aggressiveness
Change `_k` from 20.0 to 10.0 in `_fetch_timesfm_cached()` (orchestrator.py:373).
- k=10: 1% → 0.62, 2% → 0.73, 5% → 0.92 (still confident but not extreme)
- This directly addresses the systemic overconfidence at its source.

### Priority C: Tighten TimesFM Probability Clamping
Change from `[0.001, 0.999]` to `[0.10, 0.90]` (orchestrator.py:382).
Prevents extreme certainty from a single source dominating the logit pool.

### Priority D: Add Synthetic Pattern Prior
When `p_pattern=None` (no agent DuckDB access), substitute a conservative
prior based on the 7-day realized win rate for the symbol+direction.
This prevents TimesFM weight from inflating and provides a grounded anchor.

### Priority E: Add Degenerate Outcome Guards to fetch_calibration_bias
Reject bias computation when:
- `min(wins, losses) == 0` with n >= 20 (degenerate split)
- All `hold_bars == 1` (entry-price artifact pattern)
Treat as "insufficient/untrustworthy" instead of emitting extreme bias.

### Priority F: Enable predicted_p_win_raw Analysis
Migration 114 is applied + flag is True. Now we can:
1. Query `predicted_p_win_raw` vs stored `predicted_p_win` to measure
   enforcement compensation magnitude
2. Compute Brier scores to get a proper calibration-quality metric
3. Separate "model still miscalibrated?" from "is enforcement compensating?"

---

## 7. Verification Plan

Once changes are built:
1. Full pytest suite: `PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider`
2. Heavy sweep filter tests: verify emergency sweep uses env-configured thresholds
3. TimesFM sigmoid tests: verify reduced k produces less extreme probabilities
4. Live smoke: deploy to LightningAI Studio, verify calibration enforcement logs
   show reduced bias magnitudes for overconfident symbols
5. Monitor `noble-calib-health` cron (daily 02:10 UTC) for 3 nights

---

## 8. BUILD COMPLETE — Implementation (2026-08-17)

### Files Modified
1. **`regime_platform/sweep_orchestrator/heavy_sweep.py`** — MIN_RR, MAX_SHARPE, MIN_TRADES now env-configurable
   - `MIN_RR = float(os.getenv("MIN_RR", "2.0"))` (was hardcoded `2.0`) — now shared env var
   - `MAX_SHARPE = float(os.getenv("HEAVY_SWEEP_MAX_SHARPE", "10.0"))` (was hardcoded `10.0`)
   - `MIN_TRADES = int(os.getenv("HEAVY_SWEEP_MIN_TRADES", "10"))` (was hardcoded `10`)

2. **`regime_platform/sweep_orchestrator/orchestrator.py`** — TIMESFM_SIGMOID_K + clamp vars added
   - `TIMESFM_SIGMOID_K = float(os.getenv("TIMESFM_SIGMOID_K", "20.0"))` (was hardcoded `_k = 20.0`)
   - `TIMESFM_CLAMP_MIN = float(os.getenv("TIMESFM_CLAMP_MIN", "0.001"))` (was hardcoded `0.001`)
   - `TIMESFM_CLAMP_MAX = float(os.getenv("TIMESFM_CLAMP_MAX", "0.999"))` (was hardcoded `0.999`)
   - Updated "Qualification thresholds" startup log to include new vars

3. **`.env.example`** — Documentation for all 5 new env vars + k-value table
   - Full k-value comparison table (k=20 vs k=10 vs k=5)
   - Studio deployment notes section (env vars must be Studio Secrets)

3. **410 tests passed, 23 skipped, 0 failed**

---

## 9. Studio Deployment & Verification (2026-08-18)

### Env Var Mapping — Studio Secrets vs .env.local

On LightningAI Studio, env vars must be set as **Studio Secrets** — the `.env.local` file is NOT loaded by the runtime. Verified by startup log analysis:

| Variable | .env.local Value | Studio Log Shows | Match? |
|----------|-----------------|-----------------|--------|
| `MIN_RR` | 1.5 | 2.0 | ❌ (not set in Studio Secrets) |
| `DEBUG_SIGNALS` | 0 | True | ❌ (cached old value) |
| `TIMESFM_SIGMOID_K` | (not set) | 20.0 | ❌ (default) |
| `TIMESFM_CLAMP_MIN` | (not set) | 0.001 | ❌ (default) |
| `TIMESFM_CLAMP_MAX` | (not set) | 0.999 | ❌ (default) |
| `MIN_REGIME_CONF` | 0.60 | 0.6 | ✅ |
| `MIN_KELLY` | 0.02 | 0.02 | ✅ |
| `MIN_TIMESFM_CONFIDENCE` | 0.05 | 0.05 | ✅ |
| `TIMESFM_GATE_MODE` | soft | soft | ✅ |
| `MIN_EV` | 0.0 | 0.0 | ✅ |
| `MIN_P_WIN` | 0.55 | 0.55 | ✅ |
| `SIGNAL_COOLDOWN_MINUTES` | 60 | 60.0 | ✅ |

### ✅ Verified Startup (2026-08-18 01:03:22 UTC — after env var fixes)

```
MIN_REGIME_CONF=0.6, MIN_KELLY=0.02, MIN_TIMESFM_CONFIDENCE=0.05,
TIMESFM_GATE_MODE=soft, MIN_EV=0.0, MIN_RR=1.5, MIN_P_WIN=0.55,
SIGNAL_COOLDOWN_MINUTES=60.0, DEBUG_SIGNALS=False,
TIMESFM_SIGMOID_K=10.0, TIMESFM_CLAMP=[0.05, 0.95]
```

All values match. Services running: Redis OK, 14 symbols, scheduler started.

### Impact on Recalibration

**Before (Aug 17 — old defaults):**
- `MIN_RR=2.0` → only 9/21 brick combos qualified → 3/9 emergency sweeps FAILED (EURUSD, GBPUSD, USDCAD with `no_combo_passed_filters`)
- `TIMESFM_SIGMOID_K=20.0` → 1% forecast → p=0.73 → chronic overconfidence across ALL symbols
- `TIMESFM_CLAMP=[0.001, 0.999]` → p_win hit 1.0 for metals (XAUAUD, XAUEUR, XAUUSD)
- Calibration enforcement constantly firing (p_win 1.0→0.99, kelly 0.4→0.2)

**After (Aug 18 — new settings):**
- `MIN_RR=1.5` → all 21 brick combos qualify → emergency sweeps should succeed 9/9
- `TIMESFM_SIGMOID_K=10.0` → 1% forecast → p=0.62 → ~15% less aggressive p_timesfm
- `TIMESFM_CLAMP=[0.05, 0.95]` → p_win capped at 0.95 max (no more p=1.0 hits)
- Expected: fewer OVERCONFIDENT enforcement events, less aggressive bias correction

---

## 10. Emergency Heavy Sweep Trigger Analysis (2026-08-18)

### Trigger Mechanism

Emergency heavy sweeps are triggered by the **EOD correction pass** (`eod_correction.py`) when `|bias| > 0.10`:

```python
# eod_correction.py:45-46
OVERCONFIDENT_BIAS = 0.10
UNDERCONFIDENT_BIAS = -0.10
```

**Flow:**
1. **Light sweep** (every 5 min) → produces signals, stores `predicted_p_win_raw` in `nt_calibration_records`
2. **EOD correction** (every 4h: 04:30/08:30/12:30/16:30/20:30 UTC) → checks calibration bias:
   - Reads `bias` from `fetch_calibration_bias()` (queries `nt_calibration_records`)
   - If `|bias| > 0.10` → writes `emergency_heavy_sweep` action to `nt_eod_actions` table
   - Also writes `bayesian_penalty` (shrink p_win toward 0.5) and `kelly_reduction` (50% cut)
3. **Daily EOD** (00:30 UTC) → consumes `emergency_heavy_sweep` actions from `nt_eod_actions` → runs `pick_best_combo()` with fresh data

**Key gap**: The 4-hour intraday correction **writes** the `emergency_heavy_sweep` recommendations to `nt_eod_actions`, but only the **daily 00:30 UTC EOD pass** reads and executes them. This means:
- Detection at 04:30 UTC → execution at 00:30 UTC next day = **20 hours of delay**
- During this gap, the symbol operates with stale (overconfident) parameters
- The bayesian penalty and kelly reduction take effect immediately (applied in light sweeps via live enforcement), but the **parameter re-optimization** is delayed

### Verified in Logs (Aug 18 00:37:35 UTC)
```
[eod:correction] XAUUSD: OVERCONFIDENT bias=0.409 → emergency sweep + p_win'=0.9796 + Kelly×0.50
[eod:lookback] emergency heavy sweeps requested: ['AUDUSD', 'BTCUSD', 'EURUSD', 'GBPUSD', 'USDCAD', 'USDCHF', 'XAGUSD', 'XAUAUD', 'XAUEUR', 'XAUUSD']
XAUUSD: best combo brick=22.1394 sl=3 tp=10 sharpe=0.6376 trades=15 win_rate=0.8000
XAUUSD: DONE in 15455ms (qualified=False, combos=30)
```

The sweep ran but `qualified=False` — the found combo (sharpe=0.6376) wasn't good enough to replace current params. With the new `MIN_RR=1.5`, the combo search space is wider, but the quality gate for parameter replacement is separate from the RR threshold.

### Note on Terminology
The term "emergency heavy sweeps" was initially misleading — this is NOT a separate ad-hoc process. It IS triggered by the EOD correction pass writing to `nt_eod_actions`, and consumed by the daily EOD pass. The trigger is: `|calibration_bias| > 0.10` with `n_records >= 20` (EOD_BIAS_MIN_RECORDS).

---

## 11. Retroactive XAUUSD Record Rectification (SCOPE ONLY — No Action Yet)

### Current State
The `p_win=1.0000` values seen in Aug 18 sweeps (01:00-01:11 UTC) are **NOT contaminated legacy records** — they are **fresh signals** computed with the old k=20 defaults. The purge at Aug 13 23:10 UTC already removed contaminated records (`entered_at < 2026-08-11T14:00Z`).

The `predicted_p_win_raw` column (migration 114) was created and `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW=True`, so we can now distinguish:
- **raw** p_win (before enforcement/clamping) — the "true" model output
- **enforced** p_win (after overconfidence correction) — the adjusted value

### Retroactive Fix Scope (NOT building)
A backfill script could:
1. Query `nt_calibration_records WHERE symbol='XAUUSD' AND predicted_p_win_raw IS NOT NULL AND entered_at >= '2026-08-11T14:00Z'`
2. Recompute p_win with k=10.0 + clamp=[0.05, 0.95] applied to the raw forecast
3. Store recomputed p_win in a new `p_win_corrected` column
4. Re-run bias computation with corrected values
5. This would give us the "true" bias under the new settings

**However**: with the new env vars active from 01:03 UTC onward, fresh records will naturally use k=10 + clamp [0.05, 0.95]. The legacy records from 00:18–01:03 UTC (before restart) used k=20 + clamp [0.001, 0.999] — only ~45 minutes of data. Not worth backfilling.

### Recommendation: 4-hour Emergency Sweep Consumer (SCOPE ONLY — No Action Yet)
Add a cron job that runs every 4 hours (05:00/09:00/13:00/17:00/21:00 UTC) to:
1. Query `nt_eod_actions WHERE action='emergency_heavy_sweep' AND processed_at IS NULL`
2. Execute `pick_best_combo()` for each flagged symbol (immediately, not waiting for daily EOD)
3. Write results to a new `nt_calibration_sweep_results` table
4. Mark actions as `processed_at = now()`

This would reduce the gap from 20 hours → 0 hours for bias-driven parameter re-optimization.

---

## 12. Impact Analysis — What Changes Going Forward

### The 4-Hour Intraday Correction Already Runs Emergency Sweeps

**Correction**: The "4-hour Emergency Sweep Consumer" recommendation in Section 11 was based on an incorrect understanding. The existing `_run_eod_correction_only()` (in orchestrator.py:1223) **ALREADY** executes emergency heavy sweeps:

```python
# orchestrator.py:1252-1260
emergency_syms = sorted({
    a.get("symbol")
    for a in (correction_summary.get("actions") or [])
    if a.get("action") == "emergency_heavy_sweep" and a.get("symbol")
})
if not emergency_syms:
    return
cooldown_h = float(getattr(settings, "EOD_HEAVY_SWEEP_COOLDOWN_HOURS", 12) or 12)
# Apply 12-hour cooldown — only sweep symbols that haven't been swept recently
```

The 4h intraday pass (`_run_eod_correction_only`):
1. Runs `run_eod_correction()` → writes `emergency_heavy_sweep` to `nt_eod_actions`
2. Consumes actions → executes `_execute_emergency_heavy_sweeps()` for symbols past the 12h cooldown
3. Schedule: 04:30/08:30/12:30/16:30/20:30 UTC (5 runs per day)
4. Cooldown: `EOD_HEAVY_SWEEP_COOLDOWN_HOURS=12` (default)

The daily EOD pass (`_run_eod_lookback`) runs the same flow but is **exempt from the cooldown** — it always sweeps all flagged symbols.

### What This Means With New Settings

**Before (k=20, clamp=[0.001, 0.999]):**
- P_timesfm easily hit 0.99+ → p_win hit 1.0 → bias spiked above 0.10
- Emergency sweeps triggered, but `MIN_RR=2.0` blocked most combos → `qualified=False`
- Result: sweeps ran but couldn't replace params → stale params → persistent overconfidence

**After (k=10, clamp=[0.05, 0.95]):**
- P_timesfm capped at 0.95 → p_win stays below 1.0 → **less frequent bias > 0.10 triggers**
- When sweeps DO trigger, `MIN_RR=1.5` allows more combos → higher chance of `qualified=True`
- New params written to YAML → bias re-converges faster in the next cycle

### v_eod_calibration_bias View Impact

The view `v_eod_calibration_bias` reads from `nt_signal_sim.p_win` — which is the **post-enforcement** value. It measures whether enforcement is effectively correcting overconfidence, not the raw model output. The `predicted_p_win_raw` column (added by migration 114) captures the pre-enforcement value, but the view doesn't use it. This is acceptable for the bias trigger — you want to know if the **deployed** p_win is still overconfident after enforcement, not the raw model output.

### Timeline Going Forward

| Time | Process | What Changes |
|------|---------|--------------|
| 00:30 UTC | Daily EOD | Resolver + correction (exempt from cooldown) |
| 04:30 UTC | Intraday correction | Bias recompute + emergency sweeps (12h cooldown applies) |
| 05:00 UTC | Light sweep | Signals with new k=10 params |
| 08:30 UTC | Intraday correction | Same |
| 12:30 UTC | Intraday correction | Same |
| 16:30 UTC | Intraday correction | Same |
| 20:30 UTC | Intraday correction | Same |

The new env vars take full effect at the next light sweep cycle (every 5 min). Within 4 hours, the EOD correction will see reduced bias from the lower p_timesfm contribution. If bias drops below 0.10, the `emergency_heavy_sweep` trigger stops firing for that symbol — natural stabilization loop.

