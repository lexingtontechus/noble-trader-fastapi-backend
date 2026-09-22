# Heavy-Sweep R2 Bracket Wiring — Build Log

**Date:** 2026-09-22  
**Scope:** Parity with light — `lookup_bracket_probs` + `apply_bracket_to_ev` / shadow on heavy  
**Status:** Built + tests green. No migration.

## Why

R2 was light-only. Heavy (research stream) published without `p_win_bracket` on
sim rows and never exercised EV/Kelly substitution — incomplete flip evidence
and inconsistent geometry diagnostics.

## What shipped (`heavy_sweep.py`)

1. **`trade_signal` computed before EV** (was after calib) so bracket lookup has
   entry/TP.
2. **`lookup_bracket_probs`** before `compute_ev` (same as light).
3. **After `p_win_raw_heavy` capture:**
   - `p_win_in_ev_mode=live` → `apply_bracket_to_ev` (expire not a loss).
   - `p_win_in_ev_mode=shadow` → log `R2 SHADOW would set EV from p_win_bracket=…`.
4. **Then** calibration enforce (Batch D order preserved).
5. **Sim capture** now stamps `p_win_bracket`, `p_resolve`, `p_win_resolve`.

## Tests

`tests/test_heavy_sweep_bracket.py` — apply / fail-open / expire_prob / source guard.

## Studio note (same restart)

Services healthy after TimesFM 17s model load. Old `nt_calibration_records insert OK`
flood is pre-demotion history. Live-expectancy builder runs on the **weekly**
reachability job (first fill after that cron or standalone module run).
