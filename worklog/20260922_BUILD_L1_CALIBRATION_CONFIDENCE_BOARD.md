# L1 calibration confidence board + paper-symbol fix (Build)

> **Date:** 2026-09-22 · **Build Code** authorized this turn  
> **Implements (partial):** R0 scoreboard, R1 p_dir language (UI copy), R5 paper-symbol aggregate fix, R6 side dimension  
> **Parent:** `worklog/20260922_L1_CONFIDENT_CALIBRATION_RECOMMENDATIONS.md`  
> **Frontend:** included in this change — see `nobletradingapp/docs/worklog-l1-calibration-confidence-board.md`

## What shipped

### Backend — `supabase/migrations/160_calibration_confidence_board.sql`

1. **`v_calibration_confidence_board`** (new)
   - Per `(symbol, signal)` over **28 days** (C3 stability window).
   - Episode-counted (migration 143 overlap-merge rule) per side via `nt_calibration_records.signal_id → nt_signal_sim`.
   - Columns: `n_episodes`, `raw_n`, `avg_p_win_raw`, `avg_p_win_enforced`, `win_rate_closed`, `bias_raw`, `bias_enforced`, `brier_raw`, `avg_p_win_bracket`, `avg_p_win_dir`, geometry from `v_signal_outcome_by_side` (qualified=true), `high_expiry_geometry`, **`status_l1`**, `status_raw_legacy`.
   - **Tightened `status_l1` (R0/R7):**
     - `INSUFFICIENT_DATA` if n_episodes < 2
     - `PROVISIONAL` if n_episodes < 10
     - `OVERCONFIDENT` / `UNDERCONFIDENT` if |bias_raw| > 0.10
     - `CALIBRATED` only if n_episodes ≥ 10 **and** |bias_raw| ≤ 0.05 **and** pct_expired ≤ 0.40
   - Does **not** use research-stream EOD win%/expiry as product KPI.

2. **`v_paper_vs_optimized_symbol` rewrite (R5)**
   - Pre-160 `LEFT JOIN LATERAL` + `GROUP BY s.symbol, p.realized_pnl` exploded into 457 `n=1` rows.
   - Now true per-symbol: `paper_realized_pnl = SUM(nt_paper_positions.realized_pnl)` via `paper_by_symbol` CTE joined on `signal_id`.
   - Adds `n_paper_rows`. `equal_wt_share` remains naive 1/N (units still not commensurate — documented in comment).

### Frontend — nobletradingapp (this change)

| File | Change |
|---|---|
| `app/api/dashboard-data/route.js` | Fetch `v_calibration_confidence_board` + `v_signal_outcome_by_side` (qualified); expose `calibrationBoard`, `outcomeBySide`; plan-symbol filter |
| `app/dashboard/page.jsx` | New `CalibrationConfidenceBoard` (side, episodes, p_dir raw, bias raw, Brier, expire, status_l1); wire above legacy `CalibrationTable`; R1 copy (direction confidence / p_win_bracket); VsEqualWeightTable unit-incommensurate warning |

Pairs matrix (buy vs sell product surface) remains on Portfolio tab — unchanged.

## Not in this build (still open per recommendations)

| Item | Why deferred |
|---|---|
| R2 `p_win_bracket` live flip | Needs `replay_p_win_bracket_gate.py` evidence — separate green-light batch |
| R3 cell-level enforcement / single correction owner | Backend Python path (calibration_enforcement) |
| R4 `p_resolve` in EV/Kelly | Depends on R2 |
| R5 unit-aligned equal-weight baseline | Needs notional-normalized baseline SQL |
| Backend Python rename `p_win` → `p_dir` in writes | Measurement-only rename batch |

## Verification

- Migration is `CREATE OR REPLACE VIEW` (idempotent); grants `anon, authenticated`.
- `dashboard-data` degrades to `[]` if view missing (PGRST205) — safe rollout.
- Frontend: `npx eslint app/dashboard/page.jsx app/api/dashboard-data/route.js`.

## Apply order

1. Run migration 160 on Supabase (same project as nt_* tables).
2. Deploy nobletradingapp (no migration needed — reads backend views via service role).
3. Dashboard Analysis tab shows L1 board; Portfolio tab keeps pairs matrix.

## Acceptance mapping

| T | Status |
|---|---|
| T1 bias fixture | View logic (status_l1) — SQL only |
| T2 raw fail-closed | Already Batch B / this board uses raw first |
| T3 episodes | 143-style merge per (symbol, side) |
| T4 side split | Yes — `signal` dimension |
| T5 thin n PROVISIONAL | Yes — n_episodes < 10 |
| T6 bracket replay | **Not done** (R2) |
| T7 paper units | **Partial** — aggregate fixed; units still differ |
| T8 high-expire no badge | Yes — blocks CALIBRATED when pct_expired > 0.40 |
