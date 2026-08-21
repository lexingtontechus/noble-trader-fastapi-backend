# Post-Restart Verification Plan

## Context
Lightning Studio restarted. The L1/L2/L3 fixes are deployed. This document tracks the
verification steps to confirm the HMM degeneracy deadlock is resolved in the live system.

## Verification Steps

### Step 1: Verify Fixes Deployed in Studio (Priority: HIGH)
Check that degenerate symbols show recovery after the restart.

**Symbols to check:**
| Symbol | Before | Expected After |
|--------|--------|---------------|
| AUDUSD | p_win=0.0000, qualified=False | p_win≈0.81, qualified=True |
| GBPUSD | p_win=0.0000, qualified=False | p_win≈0.81, qualified=True |
| US30USD | p_win=0.0000, qualified=False | p_win≈0.81, qualified=True |
| USDCAD | p_win=0.0000, qualified=False | p_win≈0.81, qualified=True |
| USDJPY | p_win=0.0000, qualified=False | p_win≈0.81, qualified=True |
| XAUAUD | p_win=0.0000, qualified=False | p_win≈0.81, qualified=True |
| XAUUSD | p_win=0.0000, qualified=False | p_win≈0.81, qualified=True |
| EURUSD | p_win=0.9624, qualified=False (dedup) | qualified=True |
| XAUEUR | p_win=0.7892, qualified=False (dedup) | qualified=True |

**Method:** Query nt_sweep_result table or run orchestrator._process_symbol for each.

**Note:** fetch_latest_sweep is async — must use asyncio.run() or call within async context.

### Step 2: Monitor Feedback Loop Restoration (6h window)
The key success metric: calibration records start accumulating for previously-deadlocked symbols.

- Before: nt_calibration_records had zero entries for AUDUSD, GBPUSD, etc.
- After: Once p_win crosses 0.55, EOD correction computes bias → records created → feedback loop engages

**Method:** Query nt_calibration_records table for AUDUSD/GBPUSD entries appearing.

### Step 3: Check Size Mult Corrections (4h window)
EURUSD and US500 show size_mult=0.6 (overconfidence penalty). After restart:
- EURUSD: should stay high (clamp=0.01 prevents collapse)
- EOD correction should recompute bias and potentially adjust size_mult back toward 1.0

**Method:** Query nt_sweep_result for size_mult changes over time.

### Step 4: Validate Content Dedup Reset (Immediate)
EURUSD and XAUEUR were suppressed by in-memory _last_signal_hash. After restart, they should qualify.

**Method:** Run _process_symbol('EURUSD') — should return qualified=True.

### Step 5: Parameter Tuning for RR-Gate Symbols (Next Session)
4 symbols still blocked by RR gate (RR=1.67 < 2.0):
- BTCUSD, US500, USDCHF, XAGUSD

**Fix:** Change tp_bricks from 5 to 6 in Redis config, or lower MIN_RR from 2.0 to 1.5.

### Step 6: Alert Setup
Add monitoring for regime switch detection — once AUDUSD/GBPUSD show qualified=True,
the entire degeneracy deadlock chain is functioning end-to-end.
