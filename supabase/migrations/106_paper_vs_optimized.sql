-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 106: Phase 3 — v_paper_vs_optimized comparison view
--
-- Part II Phase 3 (Option B): compare the signal-driven paper book against
-- naive equal-weight allocation and the /multi_asset optimizer's weights.
--
-- This view provides the READ-ONLY comparison surface over resolved
-- outcomes. The heavy lifting (returns matrix, DrawdownOptimiser weights,
-- three-way equity stats) runs in portfolio_compare.py (CLI / weekly
-- report); this view exposes the per-symbol + daily inputs so the result
-- is auditable in SQL without recomputation.
--
-- Views:
--   1. v_paper_vs_optimized_daily — daily paper PnL vs equal-weight PnL
--      over resolved signals (the naive baseline).
--   2. v_paper_vs_optimized_symbol — per-symbol realized PnL + win rate +
--      a naive equal-weight share, so optimizer weights can be compared
--      against what each symbol actually contributed.
--
-- Idempotent: CREATE OR REPLACE VIEW. Depends on nt_signal_sim +
-- nt_paper_positions from migrations 102/105.
-- ═══════════════════════════════════════════════════════════════════════════

-- Daily comparison: paper realized PnL vs equal-weight realized PnL over
-- resolved signals (equal-weight = all resolved signals weighted equally,
-- which is the naive allocation baseline).
CREATE OR REPLACE VIEW v_paper_vs_optimized_daily AS
WITH paper_daily AS (
  SELECT
    DATE(COALESCE(close_ts, open_ts)) AS day,
    SUM(COALESCE(realized_pnl, 0))    AS paper_pnl
  FROM nt_paper_positions
  WHERE status IN ('closed', 'expired')
  GROUP BY 1
),
signal_daily AS (
  SELECT
    DATE(ts) AS day,
    SUM(COALESCE(pnl_dollars, 0)) AS equal_wt_pnl
  FROM nt_signal_sim
  WHERE outcome IN ('tp_hit', 'sl_hit')
  GROUP BY 1
)
SELECT
  COALESCE(p.day, s.day)           AS day,
  COALESCE(p.paper_pnl, 0)         AS paper_pnl,
  COALESCE(s.equal_wt_pnl, 0)      AS equal_wt_pnl,
  COALESCE(p.paper_pnl, 0) - COALESCE(s.equal_wt_pnl, 0) AS paper_minus_equal_wt
FROM paper_daily p
FULL OUTER JOIN signal_daily s ON p.day = s.day
ORDER BY day;

-- Per-symbol comparison: realized PnL, win rate, trade count, and the
-- naive equal-weight share. Optimizer weights (from portfolio_compare.py)
-- can be JOINed to this by symbol for the three-way audit.
CREATE OR REPLACE VIEW v_paper_vs_optimized_symbol AS
SELECT
  s.symbol,
  COUNT(*) FILTER (WHERE s.outcome IN ('tp_hit', 'sl_hit')) AS n_resolved,
  COUNT(*) FILTER (WHERE s.outcome = 'tp_hit')              AS n_wins,
  CASE WHEN COUNT(*) FILTER (WHERE s.outcome IN ('tp_hit', 'sl_hit')) > 0
       THEN round(
         (COUNT(*) FILTER (WHERE s.outcome = 'tp_hit'))::numeric /
         (COUNT(*) FILTER (WHERE s.outcome IN ('tp_hit', 'sl_hit'))),
         4)
       ELSE 0 END                                           AS win_rate,
  round(SUM(CASE WHEN s.outcome IN ('tp_hit', 'sl_hit')
                 THEN COALESCE(s.pnl_dollars, 0) ELSE 0 END)::numeric, 4)
                                                           AS signal_pnl,
  round(COALESCE(p.realized_pnl, 0)::numeric, 4)           AS paper_realized_pnl,
  round(1.0 / NULLIF((SELECT COUNT(DISTINCT symbol) FROM nt_signal_sim
                      WHERE outcome IN ('tp_hit', 'sl_hit')), 0), 4)
                                                           AS equal_wt_share
FROM nt_signal_sim s
LEFT JOIN LATERAL (
  SELECT SUM(COALESCE(realized_pnl, 0)) AS realized_pnl
  FROM nt_paper_positions
  WHERE signal_id = s.signal_id
) p ON true
GROUP BY s.symbol, p.realized_pnl
ORDER BY signal_pnl DESC;
