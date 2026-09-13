-- 2026-09-13: widen all three scorecard views to GROUP BY (symbol, signal, regime).
--
-- Views not yet wired to the webapp — safe to change GROUP BY shape.
--
-- Adds two new grouping dimensions:
--   signal  — 'buy' | 'sell': identifies directional bias in wins/losses.
--   regime  — HMM label (e.g. 'med_low_vol_bear'): identifies whether
--             low-vol or choppy-regime signals are systematically losing.
-- Adds one informational column:
--   macro_label — mode() within each (symbol, signal, regime) group: the
--                 most common macro context for that bucket.
--
-- Motivation (2026-09-13 live data):
--   sell / med_low_vol_bear      0 W / 8 L  (0%)  ← systematic loser
--   sell / low_vol_bear          1 W / 8 L  (11%)
--   sell / med_high_vol_bear     3 W / 2 L  (60%) ← same direction, different vol tier
--   sell / med_high_vol_strong_bear  3W/1L  (75%)
--   buy  / high_vol_strong_bull  1 W / 7 L  (12%) ← likely AAPL
--   buy  / med_high_vol_strong_bull  3W/1L  (75%)
-- Pattern: low-vol regimes lose regardless of direction — bracket geometry
-- (tp_bricks × brick_size / ATR > 4) is worse when ATR is small. This is
-- the same finding as heavy_sweep_reachability_mode's "nothing survived"
-- pattern, now visible at the signal-outcome level.
-- See worklog/20260913_PHASE_3B_GRID_WIDENING_SCOPE.md.


-- ═══ All-time ════════════════════════════════════════════════════════════════

create or replace view v_symbol_signal_scorecard as
select
    s.symbol,
    s.signal,
    s.regime,
    mode() within group (order by s.macro_label) as macro_label,
    count(*) filter (where s.qualified)                                          as qualified,
    count(*) filter (
        where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
    )                                                                            as resolved,
    count(*) filter (where s.qualified and s.outcome = 'tp_hit')                as win,
    count(*) filter (where s.qualified and s.outcome = 'sl_hit')                as loss,
    count(*) filter (where s.qualified and s.outcome = 'expired')               as expired,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'tp_hit')
        / nullif(
            count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    )                                                                            as win_pct,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'expired')
        / nullif(
            count(*) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    )                                                                            as expired_pct,
    round(
        (
            sum(s.pnl_dollars) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    )                                                                            as pnl_notional_1unit,
    max(s.ts) filter (where s.qualified)                                         as last_qualified_ts,
    round(
        (avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')))::numeric,
        4
    )                                                                            as avg_predicted_p_win,
    round(
        (
            avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
            - (
                count(*) filter (where s.qualified and s.outcome = 'tp_hit')::numeric
                / nullif(count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0)
            )
        )::numeric,
        4
    )                                                                            as precision_gap,
    round(
        (
            sum(s.pnl_dollars * m.multiplier)
            filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    )                                                                            as pnl_001_lot
from nt_signal_sim s
left join nt_symbol_lot_multiplier m on m.symbol = s.symbol
group by s.symbol, s.signal, s.regime
order by s.symbol, s.signal, s.regime;

comment on view v_symbol_signal_scorecard is
    'Per-symbol qualified-signal scorecard, grouped by (symbol, signal, regime). '
    'signal=buy|sell identifies directional bias; regime (HMM label, e.g. '
    'med_low_vol_bear) identifies whether low-vol or choppy-regime signals '
    'are systematically losing. macro_label is the mode within each group. '
    'pnl_notional_1unit: 1-unit price-delta P&L (not cross-symbol comparable). '
    'pnl_001_lot: scaled to fixed 0.01-lot via nt_symbol_lot_multiplier. '
    'precision_gap > 0 = overconfident. Expired trades excluded from win/loss/PnL. '
    'See migration 138/146/149/151 and worklog/20260913_PHASE_3B_GRID_WIDENING_SCOPE.md.';

grant select on v_symbol_signal_scorecard to anon, authenticated;


-- ═══ 7-day ═══════════════════════════════════════════════════════════════════

create or replace view v_symbol_signal_scorecard_7d as
select
    s.symbol,
    s.signal,
    s.regime,
    mode() within group (order by s.macro_label) as macro_label,
    count(*) filter (where s.qualified)                                          as qualified,
    count(*) filter (
        where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
    )                                                                            as resolved,
    count(*) filter (where s.qualified and s.outcome = 'tp_hit')                as win,
    count(*) filter (where s.qualified and s.outcome = 'sl_hit')                as loss,
    count(*) filter (where s.qualified and s.outcome = 'expired')               as expired,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'tp_hit')
        / nullif(
            count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    )                                                                            as win_pct,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'expired')
        / nullif(
            count(*) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    )                                                                            as expired_pct,
    round(
        (
            sum(s.pnl_dollars) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    )                                                                            as pnl_notional_1unit,
    max(s.ts) filter (where s.qualified)                                         as last_qualified_ts,
    round(
        (avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')))::numeric,
        4
    )                                                                            as avg_predicted_p_win,
    round(
        (
            avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
            - (
                count(*) filter (where s.qualified and s.outcome = 'tp_hit')::numeric
                / nullif(count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0)
            )
        )::numeric,
        4
    )                                                                            as precision_gap,
    round(
        (
            sum(s.pnl_dollars * m.multiplier)
            filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    )                                                                            as pnl_001_lot
from nt_signal_sim s
left join nt_symbol_lot_multiplier m on m.symbol = s.symbol
where s.ts >= now() - interval '7 days'
group by s.symbol, s.signal, s.regime
order by s.symbol, s.signal, s.regime;

comment on view v_symbol_signal_scorecard_7d is
    'Same as v_symbol_signal_scorecard (migration 151) scoped to the last 7 days. '
    'Grouped by (symbol, signal, regime) -- shows directional and regime-level '
    'performance for the "how are we doing lately" view.';

grant select on v_symbol_signal_scorecard_7d to anon, authenticated;


-- ═══ Monthly ═════════════════════════════════════════════════════════════════

create or replace view v_symbol_signal_scorecard_monthly as
select
    date_trunc('month', s.ts)                                                    as month,
    s.symbol,
    s.signal,
    s.regime,
    mode() within group (order by s.macro_label) as macro_label,
    count(*) filter (where s.qualified)                                          as qualified,
    count(*) filter (
        where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
    )                                                                            as resolved,
    count(*) filter (where s.qualified and s.outcome = 'tp_hit')                as win,
    count(*) filter (where s.qualified and s.outcome = 'sl_hit')                as loss,
    count(*) filter (where s.qualified and s.outcome = 'expired')               as expired,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'tp_hit')
        / nullif(
            count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    )                                                                            as win_pct,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'expired')
        / nullif(
            count(*) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    )                                                                            as expired_pct,
    round(
        (
            sum(s.pnl_dollars) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    )                                                                            as pnl_notional_1unit,
    round(
        (avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')))::numeric,
        4
    )                                                                            as avg_predicted_p_win,
    round(
        (
            avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
            - (
                count(*) filter (where s.qualified and s.outcome = 'tp_hit')::numeric
                / nullif(count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0)
            )
        )::numeric,
        4
    )                                                                            as precision_gap,
    round(
        (
            sum(s.pnl_dollars * m.multiplier)
            filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    )                                                                            as pnl_001_lot
from nt_signal_sim s
left join nt_symbol_lot_multiplier m on m.symbol = s.symbol
group by date_trunc('month', s.ts), s.symbol, s.signal, s.regime
order by month desc, s.symbol, s.signal, s.regime;

comment on view v_symbol_signal_scorecard_monthly is
    'Same as v_symbol_signal_scorecard (migration 151) broken out month-by-month. '
    'Grouped by (month, symbol, signal, regime) -- trend view for tracking '
    'whether directional or regime-specific performance improves over time.';

grant select on v_symbol_signal_scorecard_monthly to anon, authenticated;
