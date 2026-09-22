# R3 Cell-Level Calibration Enforcement — Build Log

**Date:** 2026-09-22  
**Scope:** R3 from `20260922_L1_CONFIDENT_CALIBRATION_RECOMMENDATIONS.md`  
**Status:** Built (code + tests). Migration **162** must be applied in Supabase.

## What shipped

1. **`supabase/migrations/162_calibration_bias_by_cell.sql`**
   - New table `nt_calibration_bias_by_cell` keyed `(symbol, side, vol_tier, distance_atr_bucket)`.
   - Side = `buy|sell` (`nt_signal_sim.signal`), matching migration 159’s reachability side dimension.
   - `bias_shrunk = avg_p_win − Beta(α=β=2) win_rate` (same prior as `p_win_bracket`).
   - Pooled `nt_calibration_bias_by_bucket` left unchanged for reachability joins.

2. **`calibration_bias_bucket.py`**
   - Fetches `signal`; aggregates side-level cells in parallel with the pooled bucket table.
   - Upserts cells via `upsert_calibration_bias_by_cell`.

3. **`calibration_enforcement.py` — single correction owner + hierarchy**
   - `resolve_bias_evidence`: **cell (n ≥ `CELL_MIN_N`=10) → parent symbol → system (no-op)**.
   - `apply_calibration_adjustment(..., side=, vol_tier=, distance_atr_bucket=)`:
     - `stagnation_kill_switch` still symbol-level hard block (kelly=0).
     - Cell **CALIBRATED** → no penalty (does **not** fall through to a symbol penalty from another cell).
     - Cell **UNDERCONFIDENT** → Bayesian shrink p_win only (Batch D 4.6).
     - Cell **OVERCONFIDENT** → shrink + Kelly × `KELLY_REDUCTION`.
   - `p_historical` stack already retired (2026-09-09); this remains the sole soft/hard shrink path.

4. **Call sites**
   - Light (`orchestrator.py`): passes `side` from `direction`, `vol_tier`/`distance_atr_bucket` from the R2 bracket lookup.
   - Heavy (`heavy_sweep.py`): passes `side` from `direction`, `_vol_tier(result.regime_label)`, `_bucket_for_combo(best, atr)`.

5. **Tests** — `tests/test_r3_cell_enforcement.py` (9 cases) + existing calibration suites: **30 passed**.

## Acceptance (remaining evidence)

- [ ] Apply migration 162 in Supabase; next `calibration_bias_bucket` run populates cells.
- [ ] `replay_calibration_gap.py` shows cell-level bias; no symbol shrunk because another symbol is overconfident.
- [ ] No USDCHF-class “100% of sweeps fail p_win_ok” crush without a cell justification.

## Deploy note

Do **not** hot-edit Studio. Ship via this repo → Studio pull/restart. `min_p_win_bracket: 0.20` is already in `config/feature_flags.yaml` from P0.
