-- 2026-09-08: rolling 7-day and month-by-month cuts of the per-symbol
-- scorecard. v_symbol_signal_scorecard (migration 138) is an all-time
-- lifetime aggregate -- useful as a baseline, but the webapp also needs a
-- "how are we doing lately" (7d) and a "how has this trended" (monthly)
-- view. Same columns/semantics/caveats as migration 138 (expired excluded
-- from win/loss/PnL; pnl_notional_1unit is a 1-unit notional proxy, not
-- lot-sized or risk-equalized -- see that migration's header).

create or replace view v_symbol_signal_scorecard_7d as
select
    symbol,
    count(*) filter (where qualified) as qualified,
    count(*) filter (
        where qualified and outcome in ('tp_hit', 'sl_hit', 'expired')
    ) as resolved,
    count(*) filter (where qualified and outcome = 'tp_hit') as win,
    count(*) filter (where qualified and outcome = 'sl_hit') as loss,
    count(*) filter (where qualified and outcome = 'expired') as expired,
    round(
        100.0 * count(*) filter (where qualified and outcome = 'tp_hit')
        / nullif(
            count(*) filter (where qualified and outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) as win_pct,
    round(
        100.0 * count(*) filter (where qualified and outcome = 'expired')
        / nullif(
            count(*) filter (
                where qualified and outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) as expired_pct,
    round(
        (
            sum(pnl_dollars) filter (where qualified and outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_notional_1unit,
    max(ts) filter (where qualified) as last_qualified_ts
from nt_signal_sim
where ts >= now() - interval '7 days'
group by symbol
order by symbol;

comment on view v_symbol_signal_scorecard_7d is
    'Same as v_symbol_signal_scorecard (migration 138) but scoped to '
    'qualified signals from the last 7 days only -- "how are we doing '
    'lately" for the webapp.';

grant select on v_symbol_signal_scorecard_7d to anon, authenticated;

create or replace view v_symbol_signal_scorecard_monthly as
select
    date_trunc('month', ts) as month,
    symbol,
    count(*) filter (where qualified) as qualified,
    count(*) filter (
        where qualified and outcome in ('tp_hit', 'sl_hit', 'expired')
    ) as resolved,
    count(*) filter (where qualified and outcome = 'tp_hit') as win,
    count(*) filter (where qualified and outcome = 'sl_hit') as loss,
    count(*) filter (where qualified and outcome = 'expired') as expired,
    round(
        100.0 * count(*) filter (where qualified and outcome = 'tp_hit')
        / nullif(
            count(*) filter (where qualified and outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) as win_pct,
    round(
        100.0 * count(*) filter (where qualified and outcome = 'expired')
        / nullif(
            count(*) filter (
                where qualified and outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) as expired_pct,
    round(
        (
            sum(pnl_dollars) filter (where qualified and outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_notional_1unit
from nt_signal_sim
group by date_trunc('month', ts), symbol
order by month desc, symbol;

comment on view v_symbol_signal_scorecard_monthly is
    'Same as v_symbol_signal_scorecard (migration 138) but broken out '
    'month-by-month -- trend view for the webapp. One row per '
    '(calendar month, symbol).';

grant select on v_symbol_signal_scorecard_monthly to anon, authenticated;
