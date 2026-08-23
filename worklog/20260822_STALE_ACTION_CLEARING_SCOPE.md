# Stale Action Clearing for 5 Low-Volume Symbols

**Date**: 2026-08-22  
**Author**: Ultron (Developer Agent)  
**Status**: Proposed  
**Related**: [`20260822_RAW_BIAS_CYCLE_RESET_PLAN.md`](./20260822_RAW_BIAS_CYCLE_RESET_PLAN.md) Section 11  

---

## 1. Problem Statement

5 symbols — **AUDUSD, EURUSD, GBPUSD, USDCAD, XAUAUD** — carry stale penalty actions in `nt_eod_actions` from Aug 18–19 (pre-restart). These actions are never cleared by the 4h raw-bias correction pass.

**Stale actions still active:**

| Symbol | Action | Bias | Written | Age (at 12-hour mark) |
|--------|--------|------|---------|----------------------|
| AUDUSD | kelly_reduction | 0.9792 | 2026-08-18T20:30:06 | 62+ hours |
| EURUSD | kelly_reduction | 0.9636 | 2026-08-18T20:30:06 | 62+ hours |
| GBPUSD | kelly_reduction | 0.6357 | 2026-08-19T12:30:05 | 53+ hours |
| USDCAD | kelly_reduction | 0.9763 | 2026-08-19T20:30:06 | 45+ hours |
| XAUAUD | kelly_reduction | 0.8985 | 2026-08-19T20:30:01 | 45+ hours |

## 2. Root Cause Analysis

### 2.1 The 4h Raw-Bias Pass Flow

The 4h intraday correction pass (`run_eod_correction(raw_bias_mode=True)`) iterates over all active symbols:

```python
# eod_correction.py L232-334
for sym in symbols:
    symbol = sym.get("symbol")
    venue = _derive_venue(sym)
    
    if raw_bias_mode:
        cal = await fetch_calibration_bias_raw(symbol, venue=venue)
    else:
        cal = await fetch_calibration_bias(symbol, venue=venue)
    
    bias = cal.get("bias") if cal else None
    
    if bias is None:
        # ── DEGENERACY PATH (L249-300) ──
        # Emergency sweep recommendation for stuck symbols
        # NO _clear_stale_eod_actions call here!
        ...
        continue  # → exits to next symbol
    
    # ── BIAS CLASSIFICATION PATH (L302-334) ──
    status = _classify_bias_raw(bias)
    
    if raw_bias_mode and status == "CALIBRATED":
        await _clear_stale_eod_actions(symbol)  # ← Only runs here
    ...
```

### 2.2 Why the 5 Symbols Fall Into the Degeneracy Path

`fetch_calibration_bias_raw()` (L508-595 in `supabase_client.py`) enforces a minimum record count:

```python
# supabase_client.py L563-564
if not isinstance(data, list) or len(data) < min_n:
    return None
```

Where `min_n = getattr(settings, "EOD_BIAS_MIN_RECORDS", 20)`.

**AUDUSD, EURUSD, GBPUSD, USDCAD, XAUAUD have <20 calibration records** in the 7-day rolling window (`EOD_BIAS_WINDOW_DAYS=7`). These are low-volume FX pairs with infrequent signal triggers compared to AAPL, US500, XAUUSD, etc.

When `fetch_calibration_bias_raw` returns `None` → `bias = None` → code enters the degeneracy path → `_clear_stale_eod_actions` is **never called**.

### 2.3 The Clearing Logic Is Conditional

The stale action clearing at `eod_correction.py` L315-316:

```python
if raw_bias_mode and status == "CALIBRATED":
    await _clear_stale_eod_actions(symbol)
```

This requires BOTH:
1. `raw_bias_mode == True` (true on 4h passes ✅)
2. `status == "CALIBRATED"` (only reached if bias was successfully computed ✅ for 9/14 symbols)

For the 5 low-volume symbols, condition #2 fails because `bias` is `None`.

## 3. Proposed Fix

### 3.1 Primary Fix: Unconditional Stale Action Clearing at 4h Boundaries

**File**: `regime_platform/sweep_orchestrator/eod_correction.py`  
**Change**: Move `_clear_stale_eod_actions(symbol)` call to **before** the bias classification, unconditionally for all symbols when `raw_bias_mode=True`.

```python
# At L232, inside the for loop — BEFORE bias computation:
for sym in symbols:
    symbol = sym.get("symbol")
    if not symbol:
        continue
    summary["checked"] += 1
    
    # ── NEW: Clear stale actions at the 4h cycle boundary ──
    # Every 4h pass should reset enforcement state before re-evaluating,
    # regardless of whether the symbol has sufficient calibration records.
    if raw_bias_mode:
        from regime_platform.sweep_orchestrator.supabase_client import _clear_stale_eod_actions
        await _clear_stale_eod_actions(symbol)
    
    venue = _derive_venue(sym)
    ...  # rest of existing logic unchanged
```

**Rationale**: The 4h raw-bias pass is designed as a "cycle reset" — it re-evaluates all symbols from their raw model output. Stale actions from the previous cycle (whether the symbol is overconfident, underconfident, or calibrated) should be cleared before the new evaluation writes fresh actions. This is the "reset" the user requested.

**Impact**:
- 5 low-volume symbols: stale actions cleared, drift monitor will re-evaluate
- Other 9 symbols: clearing already happened via status=="CALIBRATED" path — now happens unconditionally (no change in behavior since CALIBRATED symbols already get cleared)
- AAPL and US500: stale actions cleared before new overconfident actions are written (no behavioral change, just earlier clearing)

### 3.2 Alternative Fix (Rejected): Lower EOD_BIAS_MIN_RECORDS

Lowering from 20 to 10 would allow the 5 symbols to enter the bias classification path. However:

**Problems**:
- With only ~10 records, bias classification (`avg_pred_raw - win_rate`) produces unstable values
- A single outlier win/loss can swing bias by 0.1 — within the 0.30 threshold band
- **Does NOT guarantee clearing**: if bias happens to exceed 0.30, the symbol is classified OVERCONFIDENT and `_clear_stale_eod_actions` is NOT called (only CALIBRATED triggers clearing)
- **Does NOT address the root cause**: even with 10 records, a symbol classified OVERCONFIDENT won't have stale actions cleared

**Decision**: Reject. The unconditional clearing approach is superior.

### 3.3 No Changes to EOD_BIAS_MIN_RECORDS

The 20-record minimum should remain — it ensures statistical significance for bias classification. The issue is the clearing path, not the classification path.

## 4. Implementation Steps

1. **`eod_correction.py` L232-236**: Add unconditional `_clear_stale_eod_actions(symbol)` call after `summary["checked"] += 1` when `raw_bias_mode=True`
2. **Verify**: `pytest tests/test_raw_bias_cycle_reset.py` — should still pass (no change to classification logic)
3. **Smoke test**: Run a local 4h pass simulation to confirm all 14 symbols get stale actions cleared
4. **Deploy**: Requires 4h cycle reset window — deploy before 04:30 UTC pass

## 5. Edge Cases

| Scenario | Current Behavior | After Fix |
|----------|-----------------|-----------|
| Symbol with 0 records (true degeneracy) | Skips clearing, runs degeneracy check | Clears stale, skips new action write |
| Symbol with stale action + <10 records | Stale action persists forever | Stale action cleared, drift monitor re-evaluates |
| Symbol with 25 records, OVERCONFIDENT | Clears (was CALIBRATED) → writes new | Clears unconditionally → writes new (same outcome) |
| Symbol with 25 records, CALIBRATED | Clears via status=="CALIBRATED" | Clears unconditionally (same outcome) |

## 6. AAPL/US500 Raw Bias Constancy — Related Issue (Not Addressed Here)

AAPL (raw_bias=0.4447) and US500 (raw_bias=0.6771) show identical values across all 5 passes. This is because `predicted_p_win_raw` is written at inference time and never updated. A separate worklog entry is needed to address this architectural issue.
