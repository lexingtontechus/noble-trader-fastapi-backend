-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 110: Talaria analytics views — signal health + portfolio tear-sheet
--
-- WHY: the Talaria desktop plugin (end-user client of noble-trader-admin)
-- reads Supabase REST with the PUBLIC anon key (read-only RLS from migration
-- 107). It must show signal-quality + paper-performance analytics WITHOUT
-- pulling thousands of nt_signal_sim / nt_paper_positions rows into the
-- client — ALL heavy aggregation happens server-side in these two views, and
-- the plugin fetches small result sets (a per-symbol scoreboard + a one-row
-- tear-sheet).
--
--   v_talaria_signal_health   — rolling 30-day per-symbol scoreboard over
--                               RESOLVED signals (tp_hit/sl_hit): win rate,
--                               calibration bias (predicted p_win vs realized),
--                               PnL, profit factor, EV, hold time.
--   v_talaria_portfolio_stats — single-row paper-book tear-sheet: trades,
--                               win rate, profit factor, total return,
--                               Sharpe / Sortino / Calmar / max drawdown /
--                               annualized vol, derived from nt_paper_positions
--                               + the v_paper_equity curve (base notional 1000).
--
-- Both views are READ-ONLY over tables/views already anon-granted by
-- migration 107 (nt_signal_sim, nt_paper_positions, v_paper_equity); no
-- policy changes, no writes. The migration runner is service-role, so view
-- definitions are fine — anon only needs the SELECT GRANTs below.
--
-- IDEMPOTENT: CREATE OR REPLACE VIEW + GRANT ... TO anon are both naturally
-- re-runnable in the Supabase SQL editor.
-- ═══════════════════════════════════════════════════════════════════════════

-- ─── 1. v_talaria_signal_health — 30-day signal-quality scoreboard ─────────
-- One row per symbol over the last 30 days. Only RESOLVED signals
-- (tp_hit/sl_hit) enter the quality math; n_expired counts the expired
-- signals in the same window so the plugin can see how many signals simply
-- faded (joined separately so the resolved-only WHERE stays intact).
CREATE OR REPLACE VIEW v_talaria_signal_health AS
WITH expired_counts AS (
  SELECT
    symbol,
    COUNT(*) AS n_expired
  FROM nt_signal_sim
  WHERE outcome = 'expired'
    AND ts >= now() - interval '30 days'
  GROUP BY symbol
)
SELECT
  s.symbol,
  COUNT(*)                                          AS n_resolved,
  COUNT(*) FILTER (WHERE s.outcome = 'tp_hit')      AS n_tp,
  COUNT(*) FILTER (WHERE s.outcome = 'sl_hit')      AS n_sl,
  COALESCE(MAX(e.n_expired), 0)                     AS n_expired,
  -- win_rate = tp / (tp + sl); every row in the group is resolved, so
  -- COUNT(*) IS the (tp + sl) denominator.
  round((COUNT(*) FILTER (WHERE s.outcome = 'tp_hit'))::numeric
        / NULLIF(COUNT(*)::numeric, 0), 4)          AS win_rate,
  round(AVG(s.p_win)::numeric, 4)                   AS avg_predicted_p_win,
  -- bias = predicted win rate - realized win rate (> 0 ⇒ OVERCONFIDENT).
  round((AVG(s.p_win)::numeric
         - (COUNT(*) FILTER (WHERE s.outcome = 'tp_hit'))::numeric
           / NULLIF(COUNT(*)::numeric, 0)), 4)      AS bias,
  round(AVG(s.pnl_bricks)::numeric, 4)              AS avg_pnl_bricks,
  round(AVG(s.pnl_dollars)::numeric, 2)             AS avg_pnl_dollars,
  round(SUM(s.pnl_dollars)::numeric, 2)             AS total_pnl,
  -- profit_factor = gross wins / |gross losses|; no losses ⇒ NULL
  -- (NULLIF-guarded denominator).
  round((SUM(CASE WHEN s.pnl_dollars > 0 THEN s.pnl_dollars ELSE 0 END)
         / NULLIF(ABS(SUM(CASE WHEN s.pnl_dollars < 0 THEN s.pnl_dollars ELSE 0 END)), 0))::numeric, 4)
                                                    AS profit_factor,
  round(AVG(s.ev)::numeric, 4)                      AS avg_ev,
  round(AVG(s.hold_bars)::numeric, 2)               AS avg_hold_bars,
  MAX(s.ts)                                         AS last_signal_ts
FROM nt_signal_sim s
LEFT JOIN expired_counts e ON e.symbol = s.symbol
WHERE s.ts >= now() - interval '30 days'
  AND s.outcome IN ('tp_hit', 'sl_hit')
GROUP BY s.symbol
ORDER BY total_pnl DESC NULLS LAST;

COMMENT ON VIEW v_talaria_signal_health IS
  'Rolling 30-day per-symbol signal-quality scoreboard over resolved signals (tp_hit/sl_hit): win rate, calibration bias (avg predicted p_win - realized win rate), avg/total PnL, profit factor, avg EV, avg hold bars, last signal ts. Read by the Talaria plugin via anon key.';

-- ─── 2. v_talaria_portfolio_stats — single-row paper-book tear-sheet ───────
-- Always returns EXACTLY ONE row (all metrics are scalar subqueries / COALESCEd
-- aggregates): with an empty book, n_days = n_trades = 0 and every metric is
-- NULL (win_rate = 0 / NULLIF guard, sharpe-family NULL on no-return or
-- zero-deviation data). Conventions:
--   * base notional        = 1000 (NOBLE_PAPER_BASE_NOTIONAL default)
--   * daily_return         = (cumulative_pnl - LAG(cumulative_pnl)) / 1000
--   * total_return_pct     = last cumulative_pnl / 1000, expressed as % (×100)
--   * max_dd_pct           = max (running-peak equity - equity) / peak equity
--                            over the curve, expressed as % (≥ 0 by construction)
--   * sharpe / sortino     = AVG(r) / STDDEV(r) × SQRT(252); sortino uses the
--                            downside subset (negative returns only)
CREATE OR REPLACE VIEW v_talaria_portfolio_stats AS
WITH returns AS (
  SELECT
    day,
    cumulative_pnl,
    (cumulative_pnl - LAG(cumulative_pnl) OVER (ORDER BY day)) / 1000.0 AS daily_return
  FROM v_paper_equity
),
equity_curve AS (
  SELECT
    day,
    cumulative_pnl,
    MAX(cumulative_pnl) OVER (ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_max
  FROM v_paper_equity
),
drawdowns AS (
  SELECT
    (running_max - cumulative_pnl) / NULLIF(1000.0 + running_max, 0) AS dd_frac
  FROM equity_curve
)
SELECT
  -- trade book
  (SELECT COUNT(*) FROM v_paper_equity)                              AS n_days,
  (SELECT COUNT(*) FROM nt_paper_positions
    WHERE status IN ('closed', 'expired'))                           AS n_trades,
  round(((SELECT COUNT(*) FROM nt_paper_positions
           WHERE status IN ('closed', 'expired') AND COALESCE(realized_pnl, 0) > 0)::numeric
         / NULLIF((SELECT COUNT(*) FROM nt_paper_positions
                    WHERE status IN ('closed', 'expired'))::numeric, 0)), 4)  AS win_rate,
  round((SELECT AVG(COALESCE(r_multiple, 0)) FROM nt_paper_positions
          WHERE status IN ('closed', 'expired'))::numeric, 4)        AS avg_r,
  -- gross wins / |gross losses| over the closed book; no losses ⇒ NULL.
  round(((SELECT SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END)
            FROM nt_paper_positions WHERE status IN ('closed', 'expired'))
         / NULLIF(ABS((SELECT SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END)
                         FROM nt_paper_positions WHERE status IN ('closed', 'expired'))), 0))::numeric, 4)
                                                                     AS profit_factor,
  round((SELECT SUM(COALESCE(realized_pnl, 0)) FROM nt_paper_positions
          WHERE status IN ('closed', 'expired'))::numeric, 2)        AS total_pnl,
  -- equity curve
  round(((SELECT cumulative_pnl FROM v_paper_equity ORDER BY day DESC LIMIT 1)
         / 1000.0 * 100)::numeric, 4)                                AS total_return_pct,
  round(((SELECT AVG(daily_return) FROM returns)
         / NULLIF((SELECT STDDEV(daily_return) FROM returns), 0)
         * SQRT(252.0))::numeric, 4)                                 AS sharpe,
  round(((SELECT AVG(daily_return) FROM returns)
         / NULLIF((SELECT STDDEV(daily_return) FROM returns WHERE daily_return < 0), 0)
         * SQRT(252.0))::numeric, 4)                                 AS sortino,
  round((((SELECT cumulative_pnl FROM v_paper_equity ORDER BY day DESC LIMIT 1)
          / 1000.0 * 100)
         / NULLIF((SELECT MAX(dd_frac) FROM drawdowns), 0))::numeric, 4)
                                                                     AS calmar,
  round((COALESCE((SELECT MAX(dd_frac) FROM drawdowns), 0) * 100)::numeric, 4)
                                                                     AS max_dd_pct,
  round((COALESCE((SELECT STDDEV(daily_return) FROM returns), 0)
         * SQRT(252.0) * 100)::numeric, 4)                          AS vol_annual_pct;

COMMENT ON VIEW v_talaria_portfolio_stats IS
  'Single-row paper-book tear-sheet: n_days, n_trades, win_rate, avg_r, profit_factor, total_pnl, total_return_pct, sharpe, sortino, calmar, max_dd_pct, vol_annual_pct — aggregated server-side over nt_paper_positions + v_paper_equity (base notional 1000). Read by the Talaria plugin via anon key.';

-- ─── 3. anon SELECT GRANTs (migration 107 grants the base objects) ─────────
GRANT SELECT ON v_talaria_signal_health TO anon;
GRANT SELECT ON v_talaria_portfolio_stats TO anon;

-- ═══════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION (paste into Supabase SQL editor):
--   SELECT * FROM v_talaria_signal_health ORDER BY total_pnl DESC NULLS LAST;
--   -- Expect one row per symbol with resolved signals in the last 30 days,
--   -- n_expired counting the window's expired signals (may be 0).
--   SELECT * FROM v_talaria_portfolio_stats;
--   -- Expect EXACTLY ONE row; with an empty book n_days = 0 and metrics NULL.
--   SELECT has_table_privilege('anon', 'v_talaria_signal_health', 'SELECT'),
--          has_table_privilege('anon', 'v_talaria_portfolio_stats', 'SELECT');
--   -- Expect true / true.
-- ═══════════════════════════════════════════════════════════════════════════
