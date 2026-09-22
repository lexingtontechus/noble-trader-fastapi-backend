# P2 Live Expectancy Table + Log Hygiene — Build Log

**Date:** 2026-09-22  
**Scope:** Flip-evidence table for `p_win_in_ev_mode` + demote per-row insert OK logs  
**Status:** Built. Migration **163** must be applied in Supabase.

## Log assessment (screenshot)

`nt_calibration_records insert OK` × hundreds during Studio restart is **not a data bug**:

- Startup-catchup sees stale `resolved_at` and runs the EOD resolver immediately.
- Each resolved `tp_hit`/`sl_hit` writes one calibration row (`uuid` per signal).
- `_insert_row` used to log **INFO per success** — ~800 lines drowned R2 SHADOW / bracket-gate / errors.

**Fix:** success → `DEBUG`; failures stay `ERROR`. EOD still logs one summary
(`resolved N pending in M batch(es)`).

Services after restart: Main / TimesFM / Gateway / orchestrator (8003) all OK.
R2 SHADOW is logging correctly (`p_win_bracket` 0.00–0.26).

## What shipped

1. **`supabase/migrations/163_live_expectancy_daily.sql`**
   - `nt_live_expectancy_daily` keyed `(day, cohort)` — cohorts
     `all_qualified` | `bracket_accept` (≥0.20) | `bracket_reject`.
   - Metrics: n / tp / sl / expired, `win_rate_closed`, `pct_expired`,
     `expectancy_r`, `avg_p_dir`, `avg_p_win_bracket`, `bias_dir`.
   - `v_p_win_bracket_live_expectancy` — 7d/28d rollup for the flip bar.
   - Snapshot survives `eod_retention` of `nt_signal_sim`.

2. **`live_expectancy.py`** — `build_live_expectancy_daily(min_p_win_bracket=…)`
   + `upsert_live_expectancy_daily`. Wired into the weekly reachability job
   (same place as `calibration_bias_bucket`).

3. **`supabase_client._insert_row`** — per-row OK log demoted to DEBUG.

4. **Tests** — `tests/test_live_expectancy.py` (4) + prior R3 suite green.

## Flip bar (unchanged — do not skip)

1. Apply 163; accumulate ≥7d of `nt_live_expectancy_daily` at `min_p_win_bracket: 0.20`.
2. `bracket_accept` vs `all_qualified`: **expired% ↓ AND expectancy_r ↑/flat**.
3. Then `p_win_in_ev_mode: live` → +7d → `p_win_bracket_mode: live` if `|bias_raw|` improves.

## Still open

- Heavy-sweep `apply_bracket_to_ev` (research path; light is the product path).
- Mode flips — evidence-gated (above).
