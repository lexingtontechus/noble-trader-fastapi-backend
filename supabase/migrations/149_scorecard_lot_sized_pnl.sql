-- 2026-09-09: adds pnl_001_lot (fixed nominal 0.01-lot P&L) to all three
-- scorecard views, alongside the existing pnl_notional_1unit -- both are
-- kept, additive only, per this session's established discipline of
-- never silently removing/renaming an already-exposed column.
--
-- pnl_001_lot = pnl_notional_1unit * nt_symbol_lot_multiplier.multiplier
-- (migration 148). Computed by summing pnl_dollars * multiplier directly
-- per resolved row (not multiplying the already-rounded
-- pnl_notional_1unit) to avoid compounding rounding error.

create or replace view v_symbol_signal_scorecard as
select
    s.symbol,
    count(*) filter (where s.qualified) as qualified,
    count(*) filter (
        where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
    ) as resolved,
    count(*) filter (where s.qualified and s.outcome = 'tp_hit') as win,
    count(*) filter (where s.qualified and s.outcome = 'sl_hit') as loss,
    count(*) filter (where s.qualified and s.outcome = 'expired') as expired,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'tp_hit')
        / nullif(
            count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) as win_pct,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'expired')
        / nullif(
            count(*) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) as expired_pct,
    round(
        (
            sum(s.pnl_dollars) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_notional_1unit,
    max(s.ts) filter (where s.qualified) as last_qualified_ts,
    round(
        (avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')))::numeric,
        4
    ) as avg_predicted_p_win,
    round(
        (
            avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
            - (
                count(*) filter (where s.qualified and s.outcome = 'tp_hit')::numeric
                / nullif(count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0)
            )
        )::numeric,
        4
    ) as precision_gap,
    round(
        (
            sum(s.pnl_dollars * m.multiplier)
            filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_001_lot
from nt_signal_sim s
left join nt_symbol_lot_multiplier m on m.symbol = s.symbol
group by s.symbol
order by s.symbol;

comment on view v_symbol_signal_scorecard is
    'Per-symbol qualified-signal scorecard (webapp). pnl_notional_1unit is a '
    '1-unit-of-underlying notional price-delta (pnl_bricks * brick_size, no '
    'lot/contract sizing) -- comparable within a symbol over time, NOT '
    'comparable across symbols. pnl_001_lot (2026-09-09, migration 148/149) '
    'is the same underlying P&L scaled to a fixed nominal 0.01-lot (micro-'
    'lot) position size per nt_symbol_lot_multiplier -- comparable ACROSS '
    'symbols (see that table for the per-symbol multiplier and confidence '
    'level; forex/gold/silver are high confidence real contract specs, '
    'indices/stock/crypto are defensible common conventions, not each '
    'venues verified live spec). avg_predicted_p_win/precision_gap answer '
    '"how precise/calibrated is this symbols confidence" -- precision_gap '
    '> 0 means overconfident, < 0 means underconfident. Expired trades '
    'excluded from win/loss/PnL/precision pending expiry_rate_threshold_alert. '
    'See migration 138/146/148/149 and '
    'worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md.';

grant select on v_symbol_signal_scorecard to anon, authenticated;

create or replace view v_symbol_signal_scorecard_7d as
select
    s.symbol,
    count(*) filter (where s.qualified) as qualified,
    count(*) filter (
        where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
    ) as resolved,
    count(*) filter (where s.qualified and s.outcome = 'tp_hit') as win,
    count(*) filter (where s.qualified and s.outcome = 'sl_hit') as loss,
    count(*) filter (where s.qualified and s.outcome = 'expired') as expired,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'tp_hit')
        / nullif(
            count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) as win_pct,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'expired')
        / nullif(
            count(*) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) as expired_pct,
    round(
        (
            sum(s.pnl_dollars) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_notional_1unit,
    max(s.ts) filter (where s.qualified) as last_qualified_ts,
    round(
        (avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')))::numeric,
        4
    ) as avg_predicted_p_win,
    round(
        (
            avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
            - (
                count(*) filter (where s.qualified and s.outcome = 'tp_hit')::numeric
                / nullif(count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0)
            )
        )::numeric,
        4
    ) as precision_gap,
    round(
        (
            sum(s.pnl_dollars * m.multiplier)
            filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_001_lot
from nt_signal_sim s
left join nt_symbol_lot_multiplier m on m.symbol = s.symbol
where s.ts >= now() - interval '7 days'
group by s.symbol
order by s.symbol;

comment on view v_symbol_signal_scorecard_7d is
    'Same as v_symbol_signal_scorecard (migration 138/146/148/149) but scoped to '
    'qualified signals from the last 7 days only -- "how are we doing '
    'lately" for the webapp, including precision (avg_predicted_p_win/'
    'precision_gap) and lot-sized P&L (pnl_001_lot).';

grant select on v_symbol_signal_scorecard_7d to anon, authenticated;

create or replace view v_symbol_signal_scorecard_monthly as
select
    date_trunc('month', s.ts) as month,
    s.symbol,
    count(*) filter (where s.qualified) as qualified,
    count(*) filter (
        where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
    ) as resolved,
    count(*) filter (where s.qualified and s.outcome = 'tp_hit') as win,
    count(*) filter (where s.qualified and s.outcome = 'sl_hit') as loss,
    count(*) filter (where s.qualified and s.outcome = 'expired') as expired,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'tp_hit')
        / nullif(
            count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) as win_pct,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'expired')
        / nullif(
            count(*) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) as expired_pct,
    round(
        (
            sum(s.pnl_dollars) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_notional_1unit,
    round(
        (avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')))::numeric,
        4
    ) as avg_predicted_p_win,
    round(
        (
            avg(s.p_win) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
            - (
                count(*) filter (where s.qualified and s.outcome = 'tp_hit')::numeric
                / nullif(count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0)
            )
        )::numeric,
        4
    ) as precision_gap,
    round(
        (
            sum(s.pnl_dollars * m.multiplier)
            filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_001_lot
from nt_signal_sim s
left join nt_symbol_lot_multiplier m on m.symbol = s.symbol
group by date_trunc('month', s.ts), s.symbol
order by month desc, s.symbol;

comment on view v_symbol_signal_scorecard_monthly is
    'Same as v_symbol_signal_scorecard (migration 138/146/148/149) but broken out '
    'month-by-month -- trend view for the webapp, including precision '
    '(avg_predicted_p_win/precision_gap) and lot-sized P&L (pnl_001_lot). '
    'One row per (calendar month, symbol).';

grant select on v_symbol_signal_scorecard_monthly to anon, authenticated;
