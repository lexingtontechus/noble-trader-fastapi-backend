-- 2026-09-08: per-symbol qualified-signal W/L + P/L scorecard for the webapp.
--
-- This is the "exact table format" from the 2026-09-08 signal-quality review
-- (worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md), turned into a
-- live, queryable view instead of a one-off report so the webapp can render
-- it directly.
--
-- IMPORTANT — pnl_notional_1unit is NOT a real dollar P/L figure. It is
-- nt_signal_sim.pnl_dollars summed, which eod_resolver.compute_pnl() derives
-- as pnl_bricks * brick_size with NO position-sizing/lot/contract-size term
-- (confirmed via full codebase trace, 2026-09-08). It represents the P/L of
-- holding a notional 1 unit of the underlying (1 share / 1 unit of quote
-- currency / 1 barrel, etc.), identical convention for every symbol and
-- asset class. It is directionally useful WITHIN a symbol over time, but is
-- NOT comparable ACROSS symbols with different price scales or volatility
-- (e.g. a gold/index brick_size in absolute price terms dwarfs a forex
-- pair's). Two other, materially different dollar-P/L conventions exist
-- elsewhere in the codebase (renko/risk_manager.py's equity-based Kelly
-- sizing; sweep_orchestrator/paper_book.py's $1000-base-notional Kelly
-- sizing) — neither is wired into nt_signal_sim today. Do not present this
-- column to users as "what a real position would have made" without this
-- caveat.
--
-- Expired trades are deliberately excluded from win/loss and P/L per the
-- 2026-09-08 decision (nt_roadmap row expiry_rate_threshold_alert) — a
-- timeout is neither a win nor a loss, and this stays permanent until a
-- proper expiry-rate threshold alert exists.

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
    max(ts) filter (where qualified) as last_qualified_ts
from nt_signal_sim
group by symbol
order by symbol;

comment on view v_symbol_signal_scorecard is
    'Per-symbol qualified-signal scorecard (webapp). pnl_notional_1unit is a '
    '1-unit-of-underlying notional price-delta (pnl_bricks * brick_size, no '
    'lot/contract sizing) -- comparable within a symbol over time, NOT '
    'comparable across symbols. Expired trades excluded from win/loss/PnL '
    'pending expiry_rate_threshold_alert. See migration 138 header and '
    'worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md.';

grant select on v_symbol_signal_scorecard to anon, authenticated;
