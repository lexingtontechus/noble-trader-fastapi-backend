# 2026-08-21 — L1/L2/L3 Action Recommendations for HMM Degeneracy Fix

## Status: Complete

## Background

The 4 missing calibration symbols (AUDUSD, EURUSD, GBPUSD, US30USD) are caused by a
**3-level deadlock** where the HMM Markov model collapses, p_win drops to 0, signals never
qualify, and the self-correction loop can never engage because it requires calibration
records that the degenerate model can never produce.

See: `worklog/references/20260821_degeneracy_chain_analysis.md` for the full chain.

## L1 — Immediate Fix (Hot Path): Prevent Logit Pool Collapse

**Problem**: `p_markov_hold_n = 0.0` → `logit(0.0) = -34.5` → dominates the 4-source blend

**Root location**: `regime_platform/services/statistics/ev_engine.py:40`
```python
_LOGIT_CLAMP = 1e-15  # Avoid log(0) and division-by-zero
```

**Recommended fix**: Increase the clamp floor so a degenerate Markov source cannot
overwhelm the blend. When the HMM state collapses to an absorbing FLAT state, p_markov
should contribute a **neutral** rather than **infinitely negative** signal:

```python
_LOGIT_CLAMP = 0.01  # log(0.01) = -4.6, vs current log(1e-15) = -34.5
```

With this change, the logit_sum for AUDUSD would become:
```
logit_sum = 0.255*logit(0.997) + 0.222*logit(0.01) + 0.522*logit(0.51)
          = 0.255*3.5 + 0.222*(-4.6) + 0.522*0.04
          = 0.89 - 1.02 + 0.02
          = -0.11
p_win = sigmoid(-0.11) = 0.47
```

**Result**: p_win rises from ~0.0 to ~0.47. While still below MIN_P_WIN=0.55, this is a
massive improvement that enables the EOD correction loop to engage:
- bias = avg_pred - win_rate = 0.47 - 0.50 = -0.03 (|bias| < 0.10 → CALIBRATED)
- With calibration records, `bayesian_penalty` can actually work
- The `bayesian_adjusted_p_win(0.47, 3) = (0.47*3 + 0.5*10)/(3+10) = 0.54` — just below 0.55

**Action items**:
1. Edit `_LOGIT_CLAMP` in `ev_engine.py` from `1e-15` to `0.01`
2. Deploy + verify on one degenerate symbol (AUDUSD)
3. Check if p_win rises to ≥0.55 with Bayesian adjustment

**Risk**: LOW — changes the floor of log(0) handling only. The clamp already exists for
numerical stability; raising it is a conservative choice that prevents extreme logit
values from dominating.

---

## L2 — Model Fix (Root Cause): Prevent HMM State Collapse

**Problem**: In `model.py:186-205`, when `lr_std` is tiny (compressed price), all 5 HMM
states map to "FLAT" because the threshold becomes `0.25 * 1e-8 = 2.5e-9` — all state
mean log-returns fall within ±2.5e-9 of zero.

**Root location**: `regime_platform/services/hmm/model.py:189-192`
```python
lr_std = float(np.std(mat[:, 0])) if mat.shape[0] > 1 else 1.0
if lr_std < 1e-8:
    lr_std = 1e-8  # avoid div-by-zero in flat markets
threshold = STATE_MAP_THRESHOLD_STDS * lr_std
```

**Recommended fix**: Replace the absolute floor with a **relative floor** based on the
symbol's own price level, and add an **anti-degeneracy guard** that falls back to a
uniform transition matrix when state mapping collapses:

```python
# Current (broken):
if lr_std < 1e-8:
    lr_std = 1e-8

# Fix:
# 1. Raise the floor to prevent state collapse
MIN_LR_STD = 0.0001  # 10bps minimum — prevents all-states-to-FLAT collapse
if lr_std < MIN_LR_STD:
    lr_std = MIN_LR_STD

# 2. After building state_map, detect collapse:
unique_states = set(state_map.values())
if len(unique_states) == 1:
    # All states collapsed to one label — HMM learned nothing useful.
    # Fall back to uniform T3 with no absorbing state.
    logger.warning(
        "hmm.fit: %s — state collapse detected (all → %s), "
        "using uniform 3x3 with p_markov floor",
        symbol, unique_states.pop(),
    )
    # Uniform T3: no state is absorbing, gives ~33% for each direction
    T3 = {s: {s2: 1/3 for s2 in STATES_3} for s in STATES_3}
    # Don't return — use this as fallback T3 instead of the collapsed one
```

**Result**: Even when the HMM collapses, the T3 matrix won't have an absorbing FLAT state,
so `p_markov_hold_n` won't be 0.

**Action items**:
1. Raise `MIN_LR_STD` floor from `1e-8` to `1e-4` (0.01%)
2. Add anti-degeneracy guard: if all states map to one label, use uniform T3
3. Unit test with synthetic near-flat log_return data

**Risk**: MEDIUM — changes model behavior in compressed markets. Need to backtest to
verify this doesn't hurt normal market conditions.

---

## L3 — Systemic Fix (Architecture): Break the Calibration Deadlock

**Problem**: The EOD correction loop cannot engage for symbols with no calibration records.
This creates a deadlock:
```
No calibration records → bias=None → EOD skips symbol →
no emergency heavy sweep → HMM stays collapsed →
no qualified signals → no resolved outcomes → no calibration records
```

**Root location**: `regime_platform/sweep_orchestrator/eod_correction.py:185`
```python
if bias is None:
    # Not enough calibration records yet — nothing to act on.
    continue
```

**Recommended fix**: Add a **degeneracy detection path** that triggers emergency heavy
sweeps for symbols with `p_win=0` even when calibration records are absent:

```python
# In _evaluate_and_write (eod_correction.py):

# Existing: skip when no calibration records
if bias is None:
    # NEW: check for p_win=0 degeneracy from live sweeps
    from regime_platform.sweep_orchestrator.supabase_client import fetch_latest_sweep
    latest = await fetch_latest_sweep(symbol)
    if latest and float(latest.get('p_win') or 1.0) < 0.01:
        # Signal degeneracy detected — recommend emergency re-sweep
        await _write_eod_action(
            symbol, "degeneracy_breaker", None,
            {"reason": "p_win=0 in latest sweep", "latest_p_win": latest.get('p_win')},
        )
        summary["actions"].append({"symbol": symbol, "action": "degeneracy_breaker"})
        logger.warning("[eod:correction] %s: degeneracy detected (p_win=0) — emergency sweep requested", symbol)
    continue
```

Then in the orchestrator's `_run_eod_lookback` (orchestrator.py:1141-1159), add
`degeneracy_breaker` to the emergency sweep triggers:

```python
emergency_syms = sorted({
    a.get("symbol")
    for a in (correction_summary.get("actions") or [])
    if a.get("action") in ("emergency_heavy_sweep", "degeneracy_breaker")
        and a.get("symbol")
})
```

**Result**: Degenerate symbols would get emergency heavy sweeps even without calibration
records, breaking the deadlock. The heavy sweep re-optimizes brick parameters on fresh
data, potentially moving the HMM out of the collapsed state.

**Action items**:
1. Add `fetch_latest_sweep` to `supabase_client.py`
2. Add degeneracy detection to `eod_correction.py:run_eod_correction()`
3. Update orchestrator.py:1149-1153 to include `degeneracy_breaker` in emergency triggers
4. Add `EOD_EMERGENCY_SWEEP_COOLDOWN_H` to settings to prevent repeated heavy sweeps

**Risk**: LOW — additive change, doesn't modify existing calibration logic. The degeneracy
check only fires when p_win is essentially zero, which is already a failure state.

---

## Priority Order

1. **L1 (ev_engine.py `_LOGIT_CLAMP`)** — 5 line change, immediate relief for ALL degenerate symbols
2. **L3 (eod_correction.py deadlock breaker)** — moderate change, breaks the systemic deadlock
3. **L2 (model.py state collapse fix)** — root cause fix, but requires backtesting to validate

## Prevention

To prevent recurrence:

1. **Add a health monitor alert**: When >50% of symbols show `p_win < 0.01`, alert immediately
2. **Add HMM state diversity check**: Track how many unique states are in the state_map — alert when it's 1
3. **Log the degeneracy pattern**: The current log only shows "REJECTED" gates. Add a specific
   "DEGENERATE_HMM" rejection reason for the anti-degeneracy guard case.
