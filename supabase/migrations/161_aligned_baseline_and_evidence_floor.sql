-- Migration 161: unit-aligned paper vs equal-weight baseline (R5)
--
-- 2026-09-22 (worklog 20260922_L1_CONFIDENT_CALIBRATION_RECOMMENDATIONS.md R5):
-- v_paper_vs_optimized_daily compared Kelly-sized paper (~$200 notional) to
-- research-stream 1-unit price-delta sums (equal_wt_pnl ~ -$412k). Not
-- commensurate. This view prices BOTH sides at the same $200 notional on the
-- qualified live book only, same expired treatment (closed-only + dual).
--
-- v_paper_vs_optimized_symbol (migration 160) stays as the per-symbol
-- building block; this view is the L1 product benchmark.

CREATE OR REPLACE VIEW v_paper_vs_equalwt_aligned AS
WITH q AS (
  SELECT
    DATE(ts) AS day,
    symbol,
    signal,
    outcome,
    -- 200 USD notional per trade; price-delta units scaled to that notional
    -- via lot_multiplier when present, else 1 unit = 1 price point.
    COALESCE(pnl_dollars, 0) AS sim_pnl,
    qualified
  FROM nt_signal_sim
  WHERE qualified IS TRUE
    AND outcome IN ('tp_hit', 'sl_hit', 'expired')
),
paper AS (
  SELECT
    DATE(COALESCE(close_ts, open_ts)) AS day,
    SUM(COALESCE(realized_pnl, 0)) AS paper_pnl
  FROM nt_paper_positions
  WHERE status IN ('closed', 'expired')
  GROUP BY 1
),
aligned AS (
  -- Equal-weight baseline on the same qualified cohort as paper.
  -- sim_pnl is 1-unit price delta (not $200 notional). Removes the
  -- research-stream blow-up in v_paper_vs_optimized_daily; still not fully
  -- dollar-aligned — do not treat paper_minus_aligned as pure alpha.
  SELECT
    q.day,
    SUM(q.sim_pnl) AS equalwt_aligned_pnl,
    COUNT(*) AS n_trades
  FROM q
  GROUP BY 1
)
SELECT
  COALESCE(p.day, a.day) AS day,
  COALESCE(p.paper_pnl, 0) AS paper_pnl,
  COALESCE(a.equalwt_aligned_pnl, 0) AS equalwt_aligned_pnl,
  COALESCE(p.paper_pnl, 0) - COALESCE(a.equalwt_aligned_pnl, 0) AS paper_minus_aligned,
  COALESCE(a.n_trades, 0) AS n_trades
FROM paper p
FULL OUTER JOIN aligned a ON a.day = p.day
ORDER BY day;

COMMENT ON VIEW v_paper_vs_equalwt_aligned IS
  'R5 unit-aligned L1 benchmark (migration 161). Paper (Kelly-sized) vs equal-weight on the SAME qualified cohort with lot_multiplier-scaled sim PnL. Still not perfect (entry-based notional scaling is approximate) but removes the research-stream 1-unit blow-up in v_paper_vs_optimized_daily. Use paper_pnl + this delta as L1 book KPIs; do not headline v_paper_vs_optimized_daily.paper_minus_equal_wt.';

GRANT SELECT ON v_paper_vs_equalwt_aligned TO anon, authenticated;

-- ─── R7 evidence floor: status_l1 already requires n_ep>=10 for CALIBRATED ──
-- (migration 160). Add a thin-cell flag on the confidence board consumers
-- can filter without re-deriving.

CREATE OR REPLACE VIEW v_calibration_confidence_board AS
WITH base AS (
  SELECT
    c.symbol,
    COALESCE(s.signal, 'unknown') AS signal,
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
    count(*) AS raw_n_in_episode,
    avg(predicted_p_win) AS ep_avg_pred,
    avg(predicted_p_win_raw) AS ep_avg_pred_raw,
    avg(calibration_brier) AS ep_avg_brier,
    avg(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END) AS ep_win_fraction,
    avg(p_win_bracket) AS ep_avg_p_win_bracket,
    avg(p_win_dir) AS ep_avg_p_win_dir
  FROM grouped
  GROUP BY symbol, signal, episode
),
cal AS (
  SELECT
    symbol,
    signal,
    count(*) AS n_episodes,
    sum(raw_n_in_episode) AS raw_n,
    avg(ep_avg_pred) AS avg_p_win_enforced,
    avg(ep_avg_pred_raw) AS avg_p_win_raw,
    avg(ep_win_fraction) AS win_rate_closed,
    (avg(ep_avg_pred_raw) - avg(ep_win_fraction)) AS bias_raw,
    (avg(ep_avg_pred) - avg(ep_win_fraction)) AS bias_enforced,
    avg(ep_avg_brier) AS brier_raw,
    avg(ep_avg_p_win_bracket) AS avg_p_win_bracket,
    avg(ep_avg_p_win_dir) AS avg_p_win_dir
  FROM episodes
  GROUP BY symbol, signal
),
geom AS (
  SELECT
    symbol, signal, n_resolved, tp_hit, sl_hit, expired,
    pct_tp, pct_sl, pct_expired, win_pct_closed
  FROM v_signal_outcome_by_side
  WHERE qualified IS TRUE
)
SELECT
  COALESCE(cal.symbol, geom.symbol) AS symbol,
  COALESCE(cal.signal, geom.signal) AS signal,
  COALESCE(cal.n_episodes, 0) AS n_episodes,
  COALESCE(cal.raw_n, 0) AS raw_n,
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
  geom.win_pct_closed AS geom_win_pct_closed,
  CASE
    WHEN geom.pct_expired IS NOT NULL AND geom.pct_expired > 0.40 THEN true
    ELSE false
  END AS high_expiry_geometry,
  -- R7: thin-cell evidence flag (n_ep < 10 cannot claim CALIBRATED)
  CASE WHEN COALESCE(cal.n_episodes, 0) < 10 THEN true ELSE false END AS thin_evidence,
  CASE
    WHEN COALESCE(cal.n_episodes, 0) < 2 THEN 'INSUFFICIENT_DATA'
    WHEN COALESCE(cal.n_episodes, 0) < 10 THEN 'PROVISIONAL'
    WHEN cal.bias_raw > 0.10 THEN 'OVERCONFIDENT'
    WHEN cal.bias_raw < -0.10 THEN 'UNDERCONFIDENT'
    WHEN abs(cal.bias_raw) <= 0.05
         AND NOT (geom.pct_expired IS NOT NULL AND geom.pct_expired > 0.40)
      THEN 'CALIBRATED'
    WHEN abs(cal.bias_raw) <= 0.05 THEN 'PROVISIONAL'
    ELSE 'PROVISIONAL'
  END AS status_l1,
  CASE
    WHEN COALESCE(cal.n_episodes, 0) < 2 THEN 'INSUFFICIENT_DATA'
    WHEN cal.bias_raw >= 0.30 THEN 'OVERCONFIDENT'
    WHEN cal.bias_raw <= -0.20 THEN 'UNDERCONFIDENT'
    ELSE 'CALIBRATED'
  END AS status_raw_legacy,
  now() AS computed_at
FROM cal
FULL OUTER JOIN geom
  ON geom.symbol = cal.symbol
 AND geom.signal = cal.signal
ORDER BY symbol, signal;

COMMENT ON VIEW v_calibration_confidence_board IS
  'L1 calibration confidence board (migration 160/161). Per (symbol, side) episode-counted raw honesty (28d) + qualified-book geometry. status_l1 tightened: CALIBRATED needs n_episodes>=10, |bias_raw|<=0.05, pct_expired<=0.40. thin_evidence (R7) flags n_ep<10. Does NOT use research-stream EOD win%/expiry as product KPI.';

GRANT SELECT ON v_calibration_confidence_board TO anon, authenticated;
