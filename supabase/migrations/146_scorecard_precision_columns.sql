-- 2026-09-09: answers a question the user first asked days ago -- "will I
-- be able to look at v_symbol_signal_scorecard_7d and accurately see the
-- precision of each signal." Today it could not: win_pct (what actually
-- happened) and the model's own stated confidence (p_win, already stored
-- per-signal in nt_signal_sim) lived in two different places with no
-- direct side-by-side comparison anywhere the user was actually looking.
--
-- Adds two columns to all three scorecard views (all-time, 7d, monthly),
-- computed over the SAME resolved (tp_hit/sl_hit) population win_pct
-- already uses, so they are directly comparable in the same row:
--   avg_predicted_p_win -- the model's average STATED confidence on the
--     signals that actually resolved (not expired).
--   precision_gap -- avg_predicted_p_win - (win_pct/100). Positive means
--     the model claimed more confidence than it delivered (overconfident,
--     same sign convention as calibration_enforcement's bias). Negative
--     means underconfident. Near zero means well-calibrated -- which is
--     what "precision" of a signal actually means: not just whether it
--     won, but whether the confidence attached to it was honest.
--
-- This reuses nt_signal_sim.p_win directly (already captured per-signal
-- at generation time) -- no join to nt_calibration_records or the
-- calibration-bias views needed; the scorecard's own qualified/resolved
-- population IS the calibration population for this purpose.

create or replace view v_symbol_signal_scorecard as
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
    max(ts) filter (where qualified) as last_qualified_ts,
    round(
        (avg(p_win) filter (where qualified and outcome in ('tp_hit', 'sl_hit')))::numeric,
        4
    ) as avg_predicted_p_win,
    round(
        (
            avg(p_win) filter (where qualified and outcome in ('tp_hit', 'sl_hit'))
            - (
                count(*) filter (where qualified and outcome = 'tp_hit')::numeric
                / nullif(count(*) filter (where qualified and outcome in ('tp_hit', 'sl_hit')), 0)
            )
        )::numeric,
        4
    ) as precision_gap
from nt_signal_sim
group by symbol
order by symbol;

comment on view v_symbol_signal_scorecard is
    'Per-symbol qualified-signal scorecard (webapp). pnl_notional_1unit is a '
    '1-unit-of-underlying notional price-delta (pnl_bricks * brick_size, no '
    'lot/contract sizing) -- comparable within a symbol over time, NOT '
    'comparable across symbols. avg_predicted_p_win/precision_gap (2026-09-09) '
    'answer "how precise/calibrated is this symbol''s confidence" -- '
    'precision_gap > 0 means overconfident (claimed more than delivered), '
    '< 0 means underconfident, computed over the same resolved '
    '(tp_hit/sl_hit) population win_pct uses. Expired trades excluded from '
    'win/loss/PnL/precision pending expiry_rate_threshold_alert. See '
    'migration 138/146 and worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md.';

grant select on v_symbol_signal_scorecard to anon, authenticated;

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
    max(ts) filter (where qualified) as last_qualified_ts,
    round(
        (avg(p_win) filter (where qualified and outcome in ('tp_hit', 'sl_hit')))::numeric,
        4
    ) as avg_predicted_p_win,
    round(
        (
            avg(p_win) filter (where qualified and outcome in ('tp_hit', 'sl_hit'))
            - (
                count(*) filter (where qualified and outcome = 'tp_hit')::numeric
                / nullif(count(*) filter (where qualified and outcome in ('tp_hit', 'sl_hit')), 0)
            )
        )::numeric,
        4
    ) as precision_gap
from nt_signal_sim
where ts >= now() - interval '7 days'
group by symbol
order by symbol;

comment on view v_symbol_signal_scorecard_7d is
    'Same as v_symbol_signal_scorecard (migration 138/146) but scoped to '
    'qualified signals from the last 7 days only -- "how are we doing '
    'lately" for the webapp, including precision (avg_predicted_p_win/'
    'precision_gap).';

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
    ) as pnl_notional_1unit,
    round(
        (avg(p_win) filter (where qualified and outcome in ('tp_hit', 'sl_hit')))::numeric,
        4
    ) as avg_predicted_p_win,
    round(
        (
            avg(p_win) filter (where qualified and outcome in ('tp_hit', 'sl_hit'))
            - (
                count(*) filter (where qualified and outcome = 'tp_hit')::numeric
                / nullif(count(*) filter (where qualified and outcome in ('tp_hit', 'sl_hit')), 0)
            )
        )::numeric,
        4
    ) as precision_gap
from nt_signal_sim
group by date_trunc('month', ts), symbol
order by month desc, symbol;

comment on view v_symbol_signal_scorecard_monthly is
    'Same as v_symbol_signal_scorecard (migration 138/146) but broken out '
    'month-by-month -- trend view for the webapp, including precision '
    '(avg_predicted_p_win/precision_gap). One row per (calendar month, symbol).';

grant select on v_symbol_signal_scorecard_monthly to anon, authenticated;
