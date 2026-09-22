-- Migration 160: L1 calibration confidence board + paper-vs-symbol aggregate fix
--
-- 2026-09-22 (worklog 20260922_L1_CONFIDENT_CALIBRATION_RECOMMENDATIONS.md):
-- Product needs a single L1 board that answers "is p_win honest?" without
-- blending sides or hiding thin episode counts. EOD win%/expiry remain L3/L2
-- diagnostics and must not drive this board's status.
--
-- 1. v_calibration_confidence_board — per (symbol, signal) episode-counted
--    raw honesty + geometry context + tightened L1 status.
-- 2. v_paper_vs_optimized_symbol — true per-symbol aggregate (the pre-160
--    LATERAL on signal_id exploded into 457 n=1 rows and was unusable).

-- ─── 1. v_calibration_confidence_board ─────────────────────────────────────
-- Window: 28 days (stability / C3). Episodes = merged [entered_at, closed_at]
-- overlaps per (symbol, side), same rule as migration 143.
-- Side comes from nt_signal_sim via signal_id (migration 121).

CREATE OR REPLACE VIEW v_calibration_confidence_board AS
WITH base AS (
  SELECT
    c.symbol,
    COALESCE(s.signal, 'unknown')              AS signal,
    c.entered_at,
    c.closed_at,
    c.predicted_p_win,
    COALESCE(c.predicted_p_win_raw, c.predicted_p_win) AS predicted_p_win_raw,
    c.calibration_brier,
    c.outcome,
    s.p_win_bracket,
    s.p_win_dir
  FROM nt_calibration_records c
  LEFT JOIN nt_signal_sim s ON s.signal_id = c.signal_id
  WHERE c.outcome = ANY (ARRAY['win', 'loss'])
    AND c.entered_at >= now() - interval '28 days'
),
ordered AS (
  SELECT
    *,
    max(closed_at) OVER (
      PARTITION BY symbol, signal
      ORDER BY entered_at
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS running_max_closed_at
  FROM base
),
grouped AS (
  SELECT
    *,
    sum(
      CASE
        WHEN running_max_closed_at IS NULL OR entered_at > running_max_closed_at
        THEN 1 ELSE 0
      END
    ) OVER (PARTITION BY symbol, signal ORDER BY entered_at) AS episode
  FROM ordered
),
episodes AS (
  SELECT
    symbol,
    signal,
    episode,
    count(*)                                       AS raw_n_in_episode,
    avg(predicted_p_win)                           AS ep_avg_pred,
    avg(predicted_p_win_raw)                       AS ep_avg_pred_raw,
    avg(calibration_brier)                         AS ep_avg_brier,
    avg(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END) AS ep_win_fraction,
    avg(p_win_bracket)                             AS ep_avg_p_win_bracket,
    avg(p_win_dir)                                 AS ep_avg_p_win_dir
  FROM grouped
  GROUP BY symbol, signal, episode
),
cal AS (
  SELECT
    symbol,
    signal,
    count(*)                                       AS n_episodes,
    sum(raw_n_in_episode)                          AS raw_n,
    avg(ep_avg_pred)                               AS avg_p_win_enforced,
    avg(ep_avg_pred_raw)                           AS avg_p_win_raw,
    avg(ep_win_fraction)                           AS win_rate_closed,
    (avg(ep_avg_pred_raw) - avg(ep_win_fraction))  AS bias_raw,
    (avg(ep_avg_pred) - avg(ep_win_fraction))      AS bias_enforced,
    avg(ep_avg_brier)                              AS brier_raw,
    avg(ep_avg_p_win_bracket)                      AS avg_p_win_bracket,
    avg(ep_avg_p_win_dir)                          AS avg_p_win_dir
  FROM episodes
  GROUP BY symbol, signal
),
-- Geometry context on the live book only (qualified=true) — L3, not product KPI.
geom AS (
  SELECT
    symbol,
    signal,
    n_resolved,
    tp_hit,
    sl_hit,
    expired,
    pct_tp,
    pct_sl,
    pct_expired,
    win_pct_closed
  FROM v_signal_outcome_by_side
  WHERE qualified IS TRUE
)
SELECT
  COALESCE(cal.symbol, geom.symbol)                         AS symbol,
  COALESCE(cal.signal, geom.signal)                         AS signal,
  COALESCE(cal.n_episodes, 0)                               AS n_episodes,
  COALESCE(cal.raw_n, 0)                                    AS raw_n,
  cal.avg_p_win_raw,
  cal.avg_p_win_enforced,
  cal.win_rate_closed,
  cal.bias_raw,
  cal.bias_enforced,
  cal.brier_raw,
  cal.avg_p_win_bracket,
  cal.avg_p_win_dir,
  geom.n_resolved,
  geom.tp_hit,
  geom.sl_hit,
  geom.expired,
  geom.pct_tp,
  geom.pct_sl,
  geom.pct_expired,
  geom.win_pct_closed                                      AS geom_win_pct_closed,
  -- Geometry cannot be ignored for an L1 "confident" claim (R4).
  CASE
    WHEN geom.pct_expired IS NOT NULL AND geom.pct_expired > 0.40 THEN true
    ELSE false
  END                                                      AS high_expiry_geometry,
  -- Tightened L1 status (R0/R7): CALIBRATED needs n_ep≥10 and |bias_raw|≤0.05.
  CASE
    WHEN COALESCE(cal.n_episodes, 0) < 2 THEN 'INSUFFICIENT_DATA'
    WHEN COALESCE(cal.n_episodes, 0) < 10 THEN 'PROVISIONAL'
    WHEN cal.bias_raw > 0.10 THEN 'OVERCONFIDENT'
    WHEN cal.bias_raw < -0.10 THEN 'UNDERCONFIDENT'
    WHEN abs(cal.bias_raw) <= 0.05
         AND NOT (geom.pct_expired IS NOT NULL AND geom.pct_expired > 0.40)
      THEN 'CALIBRATED'
    WHEN abs(cal.bias_raw) <= 0.05 THEN 'PROVISIONAL'  -- honest but geometry blocks claim
    ELSE 'PROVISIONAL'
  END                                                      AS status_l1,
  -- Legacy-style honesty label on raw (looser bands) for continuity with 119/143.
  CASE
    WHEN COALESCE(cal.n_episodes, 0) < 2 THEN 'INSUFFICIENT_DATA'
    WHEN cal.bias_raw >= 0.30 THEN 'OVERCONFIDENT'
    WHEN cal.bias_raw <= -0.20 THEN 'UNDERCONFIDENT'
    ELSE 'CALIBRATED'
  END                                                      AS status_raw_legacy,
  now()                                                    AS computed_at
FROM cal
FULL OUTER JOIN geom
  ON geom.symbol = cal.symbol
 AND geom.signal = cal.signal
ORDER BY symbol, signal;

COMMENT ON VIEW v_calibration_confidence_board IS
  'L1 calibration confidence board (migration 160). Per (symbol, side) episode-counted raw honesty (28d) + qualified-book geometry. status_l1 is tightened: CALIBRATED requires n_episodes≥10, |bias_raw|≤0.05, and pct_expired≤0.40. Does NOT use research-stream EOD win%/expiry as product KPI. p_win_raw is directional confidence until p_win_bracket is live.';

GRANT SELECT ON v_calibration_confidence_board TO anon, authenticated;

-- ─── 2. Fix v_paper_vs_optimized_symbol — true per-symbol aggregate ────────
-- Pre-160: GROUP BY s.symbol, p.realized_pnl + per-signal LATERAL exploded
-- the view into hundreds of n=1 rows. Paper PnL must SUM per symbol.

CREATE OR REPLACE VIEW v_paper_vs_optimized_symbol AS
WITH paper_by_symbol AS (
  SELECT
    ss.symbol,
    SUM(COALESCE(pp.realized_pnl, 0)) AS paper_realized_pnl,
    COUNT(*)                          AS n_paper_rows
  FROM nt_paper_positions pp
  JOIN nt_signal_sim ss ON ss.signal_id = pp.signal_id
  GROUP BY ss.symbol
),
resolved AS (
  SELECT
    symbol,
    COUNT(*) FILTER (WHERE outcome IN ('tp_hit', 'sl_hit')) AS n_resolved,
    COUNT(*) FILTER (WHERE outcome = 'tp_hit')              AS n_wins,
    SUM(CASE WHEN outcome IN ('tp_hit', 'sl_hit')
             THEN COALESCE(pnl_dollars, 0) ELSE 0 END)      AS signal_pnl
  FROM nt_signal_sim
  GROUP BY symbol
),
universe AS (
  SELECT COUNT(DISTINCT symbol) AS n_symbols
  FROM nt_signal_sim
  WHERE outcome IN ('tp_hit', 'sl_hit')
)
SELECT
  r.symbol,
  r.n_resolved,
  r.n_wins,
  CASE WHEN r.n_resolved > 0
       THEN round((r.n_wins)::numeric / r.n_resolved, 4)
       ELSE 0 END                                           AS win_rate,
  round(r.signal_pnl::numeric, 4)                           AS signal_pnl,
  round(COALESCE(p.paper_realized_pnl, 0)::numeric, 4)      AS paper_realized_pnl,
  round(COALESCE(p.n_paper_rows, 0)::numeric, 0)            AS n_paper_rows,
  round(1.0 / NULLIF(u.n_symbols, 0), 4)                    AS equal_wt_share
FROM resolved r
LEFT JOIN paper_by_symbol p ON p.symbol = r.symbol
CROSS JOIN universe u
ORDER BY r.signal_pnl DESC;

COMMENT ON VIEW v_paper_vs_optimized_symbol IS
  'Per-symbol paper vs research-stream building block (migration 160 fix). True aggregate — paper_realized_pnl is SUM over nt_paper_positions joined by signal_id. equal_wt_share is the naive 1/N share. Units still differ from paper notional; do not treat paper_minus equal-wt as edge until unit-aligned (worklog 20260922 L1).';

GRANT SELECT ON v_paper_vs_optimized_symbol TO anon, authenticated;
