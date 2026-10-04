-- Migration 172: Update scorecard views + live expectancy to reflect
-- p_win_dir = bracket probability (renamed from p_win_bracket, 2026-10-03).
--
-- p_win_dir is now the bracket probability (p_resolve * p_win|resolve).
-- p_win_bracket is a deprecated alias. p_win in nt_signal_sim is the
-- EV Engine output, which after apply_bracket_to_ev is the bracket prob.

-- Drop old views first (CREATE OR REPLACE cannot drop columns)
DROP VIEW IF EXISTS v_symbol_signal_scorecard CASCADE;
DROP VIEW IF EXISTS v_symbol_signal_scorecard_7d CASCADE;
DROP VIEW IF EXISTS v_symbol_signal_scorecard_monthly CASCADE;
DROP VIEW IF EXISTS v_p_win_dir_live_expectancy CASCADE;

-- ─── 1. v_symbol_signal_scorecard ─────────────────────────────────────────
-- avg_predicted_p_win = avg(p_win) = average bracket probability (post-enforcement).
-- precision_gap = avg(p_win) - win_rate = bracket prob - realized win rate.
-- Add avg_p_win_dir as an explicit column for clarity.

CREATE OR REPLACE VIEW v_symbol_signal_scorecard AS
SELECT
    s.symbol,
    COUNT(*) FILTER (WHERE s.qualified) AS qualified,
    COUNT(*) FILTER (
        WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit', 'expired')
    ) AS resolved,
    COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'tp_hit') AS win,
    COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'sl_hit') AS loss,
    COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'expired') AS expired,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'tp_hit')
        / NULLIF(
            COUNT(*) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) AS win_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'expired')
        / NULLIF(
            COUNT(*) FILTER (
                WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) AS expired_pct,
    ROUND(
        (
            SUM(s.pnl_dollars) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit'))
        )::NUMERIC,
        2
    ) AS pnl_notional_1unit,
    MAX(s.ts) FILTER (WHERE s.qualified) AS last_qualified_ts,
    ROUND(
        (AVG(s.p_win) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')))::NUMERIC,
        4
    ) AS avg_predicted_p_win,
    ROUND(
        (
            AVG(s.p_win) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit'))
            - (
                COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'tp_hit')::NUMERIC
                / NULLIF(COUNT(*) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')), 0)
            )
        )::NUMERIC,
        4
    ) AS precision_gap,
    ROUND(
        (
            SUM(s.pnl_dollars * m.multiplier)
            FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit'))
        )::NUMERIC,
        2
    ) AS pnl_001_lot,
    -- 2026-10-03: avg_p_win_dir = average bracket probability (p_win_dir).
    -- Same as avg_predicted_p_win when p_win_in_ev_mode=live (p_win = bracket prob).
    ROUND(
        (AVG(s.p_win_dir) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')))::NUMERIC,
        4
    ) AS avg_p_win_dir
FROM nt_signal_sim s
LEFT JOIN nt_symbol_lot_multiplier m ON m.symbol = s.symbol
GROUP BY s.symbol
ORDER BY s.symbol;

COMMENT ON VIEW v_symbol_signal_scorecard IS
    'Per-symbol qualified-signal scorecard (webapp). avg_predicted_p_win = '
    'avg(p_win) = average bracket probability (post-enforcement, '
    'p_win_in_ev_mode=live). avg_p_win_dir (2026-10-03) = average '
    'bracket probability from p_win_dir column. precision_gap = '
    'avg(p_win) - win_rate (bracket prob vs realized). pnl_notional_1unit '
    'is 1-unit notional price-delta; pnl_001_lot is 0.01-lot scaled. '
    'See migration 138/146/148/149/172.';

GRANT SELECT ON v_symbol_signal_scorecard TO anon, authenticated;

-- ─── 2. v_symbol_signal_scorecard_7d ──────────────────────────────────────

CREATE OR REPLACE VIEW v_symbol_signal_scorecard_7d AS
SELECT
    s.symbol,
    COUNT(*) FILTER (WHERE s.qualified) AS qualified,
    COUNT(*) FILTER (
        WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit', 'expired')
    ) AS resolved,
    COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'tp_hit') AS win,
    COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'sl_hit') AS loss,
    COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'expired') AS expired,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'tp_hit')
        / NULLIF(
            COUNT(*) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) AS win_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'expired')
        / NULLIF(
            COUNT(*) FILTER (
                WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) AS expired_pct,
    ROUND(
        (
            SUM(s.pnl_dollars) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit'))
        )::NUMERIC,
        2
    ) AS pnl_notional_1unit,
    MAX(s.ts) FILTER (WHERE s.qualified) AS last_qualified_ts,
    ROUND(
        (AVG(s.p_win) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')))::NUMERIC,
        4
    ) AS avg_predicted_p_win,
    ROUND(
        (
            AVG(s.p_win) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit'))
            - (
                COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'tp_hit')::NUMERIC
                / NULLIF(COUNT(*) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')), 0)
            )
        )::NUMERIC,
        4
    ) AS precision_gap,
    ROUND(
        (
            SUM(s.pnl_dollars * m.multiplier)
            FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit'))
        )::NUMERIC,
        2
    ) AS pnl_001_lot,
    ROUND(
        (AVG(s.p_win_dir) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')))::NUMERIC,
        4
    ) AS avg_p_win_dir
FROM nt_signal_sim s
LEFT JOIN nt_symbol_lot_multiplier m ON m.symbol = s.symbol
WHERE s.ts >= NOW() - INTERVAL '7 days'
GROUP BY s.symbol
ORDER BY s.symbol;

COMMENT ON VIEW v_symbol_signal_scorecard_7d IS
    'Same as v_symbol_signal_scorecard but scoped to last 7 days. '
    'avg_p_win_dir (2026-10-03) = average bracket probability from '
    'p_win_dir column. See migration 138/146/148/149/172.';

GRANT SELECT ON v_symbol_signal_scorecard_7d TO anon, authenticated;

-- ─── 3. v_symbol_signal_scorecard_monthly ─────────────────────────────────

CREATE OR REPLACE VIEW v_symbol_signal_scorecard_monthly AS
SELECT
    DATE_TRUNC('month', s.ts) AS month,
    s.symbol,
    COUNT(*) FILTER (WHERE s.qualified) AS qualified,
    COUNT(*) FILTER (
        WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit', 'expired')
    ) AS resolved,
    COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'tp_hit') AS win,
    COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'sl_hit') AS loss,
    COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'expired') AS expired,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'tp_hit')
        / NULLIF(
            COUNT(*) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) AS win_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'expired')
        / NULLIF(
            COUNT(*) FILTER (
                WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) AS expired_pct,
    ROUND(
        (
            SUM(s.pnl_dollars) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit'))
        )::NUMERIC,
        2
    ) AS pnl_notional_1unit,
    ROUND(
        (AVG(s.p_win) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')))::NUMERIC,
        4
    ) AS avg_predicted_p_win,
    ROUND(
        (
            AVG(s.p_win) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit'))
            - (
                COUNT(*) FILTER (WHERE s.qualified AND s.outcome = 'tp_hit')::NUMERIC
                / NULLIF(COUNT(*) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')), 0)
            )
        )::NUMERIC,
        4
    ) AS precision_gap,
    ROUND(
        (
            SUM(s.pnl_dollars * m.multiplier)
            FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit'))
        )::NUMERIC,
        2
    ) AS pnl_001_lot,
    ROUND(
        (AVG(s.p_win_dir) FILTER (WHERE s.qualified AND s.outcome IN ('tp_hit', 'sl_hit')))::NUMERIC,
        4
    ) AS avg_p_win_dir
FROM nt_signal_sim s
LEFT JOIN nt_symbol_lot_multiplier m ON m.symbol = s.symbol
GROUP BY DATE_TRUNC('month', s.ts), s.symbol
ORDER BY month DESC, s.symbol;

COMMENT ON VIEW v_symbol_signal_scorecard_monthly IS
    'Monthly scorecard. avg_p_win_dir (2026-10-03) = average bracket '
    'probability from p_win_dir column. See migration 138/146/148/149/172.';

GRANT SELECT ON v_symbol_signal_scorecard_monthly TO anon, authenticated;

-- ─── 4. nt_live_expectancy_daily: add avg_p_win_dir column ────────────────

ALTER TABLE nt_live_expectancy_daily
    ADD COLUMN IF NOT EXISTS avg_p_win_dir DOUBLE PRECISION;

COMMENT ON COLUMN nt_live_expectancy_daily.avg_p_win_dir IS
    'Average bracket probability (p_win_dir, 2026-10-03). Same as avg_p_win_bracket for new rows.';

-- ─── 5. v_p_win_dir_live_expectancy (new view, old one kept for compat) ──

CREATE OR REPLACE VIEW v_p_win_dir_live_expectancy AS
SELECT
    cohort,
    '7d'::TEXT AS window,
    SUM(n)::INT AS n,
    SUM(n_tp)::INT AS n_tp,
    SUM(n_sl)::INT AS n_sl,
    SUM(n_expired)::INT AS n_expired,
    CASE WHEN (SUM(n_tp) + SUM(n_sl)) > 0
         THEN SUM(n_tp)::FLOAT / (SUM(n_tp) + SUM(n_sl))
    END AS win_rate_closed,
    CASE WHEN SUM(n) > 0 THEN SUM(n_expired)::FLOAT / SUM(n) END AS pct_expired,
    AVG(expectancy_r) AS expectancy_r_avg,
    AVG(avg_p_dir) AS avg_p_dir,
    AVG(avg_p_win_dir) AS avg_p_win_dir,
    AVG(bias_dir) AS bias_dir
FROM nt_live_expectancy_daily
WHERE day >= (CURRENT_DATE - 7)
GROUP BY cohort
UNION ALL
SELECT
    cohort,
    '28d'::TEXT AS window,
    SUM(n)::INT,
    SUM(n_tp)::INT,
    SUM(n_sl)::INT,
    SUM(n_expired)::INT,
    CASE WHEN (SUM(n_tp) + SUM(n_sl)) > 0
         THEN SUM(n_tp)::FLOAT / (SUM(n_tp) + SUM(n_sl))
    END,
    CASE WHEN SUM(n) > 0 THEN SUM(n_expired)::FLOAT / SUM(n) END,
    AVG(expectancy_r),
    AVG(avg_p_dir),
    AVG(avg_p_win_dir),
    AVG(bias_dir)
FROM nt_live_expectancy_daily
WHERE day >= (CURRENT_DATE - 28)
GROUP BY cohort;

COMMENT ON VIEW v_p_win_dir_live_expectancy IS
    'Flip-evidence rollup (7d/28d) over nt_live_expectancy_daily. '
    'avg_p_win_dir (2026-10-03) = average bracket probability. '
    'Compare bracket_accept vs all_qualified: want expired%↓ and '
    'expectancy_r↑/flat before p_win_in_ev_mode: live. '
    'Supersedes v_p_win_bracket_live_expectancy (kept for backward compat).';

GRANT SELECT ON v_p_win_dir_live_expectancy TO anon, authenticated;
