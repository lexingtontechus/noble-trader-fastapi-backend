-- 2026-09-16 Batch B — measurement honesty (worklog
-- 20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md Phase 2).
--
-- FIX (re-run): CREATE OR REPLACE VIEW cannot insert/reorder columns
-- (42P16: cannot change name of view column "expired_pct" to
-- "win_pct_closed"). These scorecard views are recreated via DROP + CREATE
-- so dual-report columns can sit in a stable, documented order.
--
-- 1. Dual-report scorecard columns on all-time / 7d / monthly:
--      pnl_closed_only     — tp_hit + sl_hit only (legacy pnl_notional_1unit)
--      pnl_all_resolved    — includes expired MTM
--      win_pct_closed      — resolved-only win rate (same as win_pct)
--      expiry_pct          — first-class alias of expired_pct
-- 2. Reachability bucket provenance: n_stored / n_proxy
--
-- DROP + CREATE is required here; views are read-only analytics (no
-- dependent views found in-repo). Re-grant after recreate.

-- ═══ Reachability provenance (idempotent) ══════════════════════════════

alter table nt_bracket_reachability
  add column if not exists n_stored integer not null default 0;

alter table nt_bracket_reachability
  add column if not exists n_proxy integer not null default 0;

comment on column nt_bracket_reachability.n_stored is
  'Rows bucketed using the signal''s own stored ATR (point-in-time honest).';
comment on column nt_bracket_reachability.n_proxy is
  'Rows bucketed using a current-ATR proxy (look-ahead). Quarantine from live gates.';

-- ═══ All-time scorecard ════════════════════════════════════════════════

drop view if exists v_symbol_signal_scorecard;

create view v_symbol_signal_scorecard as
select
    s.symbol,
    s.signal,
    s.regime,
    mode() within group (order by s.macro_label) as macro_label,
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
    ) as pnl_001_lot,
    -- ── Batch B dual-report (appended) ─────────────────────────────────
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'tp_hit')
        / nullif(
            count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) as win_pct_closed,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'expired')
        / nullif(
            count(*) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) as expiry_pct,
    round(
        (
            sum(s.pnl_dollars) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_closed_only,
    round(
        (
            sum(s.pnl_dollars) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            )
        )::numeric,
        2
    ) as pnl_all_resolved
from nt_signal_sim s
left join nt_symbol_lot_multiplier m on m.symbol = s.symbol
group by s.symbol, s.signal, s.regime
order by s.symbol, s.signal, s.regime;

comment on view v_symbol_signal_scorecard is
    'Per (symbol, signal, regime) scorecard. Dual-report PnL: '
    'pnl_closed_only = tp_hit+sl_hit (alias of pnl_notional_1unit); '
    'pnl_all_resolved includes expired MTM. win_pct_closed = resolved-only '
    'win rate; expiry_pct is first-class. Batch B migration 153.';

grant select on v_symbol_signal_scorecard to anon, authenticated;

-- ═══ 7-day scorecard ═══════════════════════════════════════════════════

drop view if exists v_symbol_signal_scorecard_7d;

create view v_symbol_signal_scorecard_7d as
select
    s.symbol,
    s.signal,
    s.regime,
    mode() within group (order by s.macro_label) as macro_label,
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
    ) as pnl_001_lot,
    -- ── Batch B dual-report (appended) ─────────────────────────────────
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'tp_hit')
        / nullif(
            count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) as win_pct_closed,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'expired')
        / nullif(
            count(*) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) as expiry_pct,
    round(
        (
            sum(s.pnl_dollars) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_closed_only,
    round(
        (
            sum(s.pnl_dollars) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            )
        )::numeric,
        2
    ) as pnl_all_resolved
from nt_signal_sim s
left join nt_symbol_lot_multiplier m on m.symbol = s.symbol
where s.ts >= now() - interval '7 days'
group by s.symbol, s.signal, s.regime
order by s.symbol, s.signal, s.regime;

comment on view v_symbol_signal_scorecard_7d is
    '7-day scoped dual-report scorecard (Batch B migration 153). '
    'Same columns as v_symbol_signal_scorecard.';

grant select on v_symbol_signal_scorecard_7d to anon, authenticated;

-- ═══ Monthly scorecard (keep parity) ═══════════════════════════════════

drop view if exists v_symbol_signal_scorecard_monthly;

create view v_symbol_signal_scorecard_monthly as
select
    date_trunc('month', s.ts) as month,
    s.symbol,
    s.signal,
    s.regime,
    mode() within group (order by s.macro_label) as macro_label,
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
    ) as pnl_001_lot,
    -- ── Batch B dual-report (appended) ─────────────────────────────────
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'tp_hit')
        / nullif(
            count(*) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit')), 0
        ),
        1
    ) as win_pct_closed,
    round(
        100.0 * count(*) filter (where s.qualified and s.outcome = 'expired')
        / nullif(
            count(*) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            ), 0
        ),
        1
    ) as expiry_pct,
    round(
        (
            sum(s.pnl_dollars) filter (where s.qualified and s.outcome in ('tp_hit', 'sl_hit'))
        )::numeric,
        2
    ) as pnl_closed_only,
    round(
        (
            sum(s.pnl_dollars) filter (
                where s.qualified and s.outcome in ('tp_hit', 'sl_hit', 'expired')
            )
        )::numeric,
        2
    ) as pnl_all_resolved
from nt_signal_sim s
left join nt_symbol_lot_multiplier m on m.symbol = s.symbol
group by date_trunc('month', s.ts), s.symbol, s.signal, s.regime
order by month desc, s.symbol, s.signal, s.regime;

comment on view v_symbol_signal_scorecard_monthly is
    'Monthly dual-report scorecard (Batch B migration 153).';

grant select on v_symbol_signal_scorecard_monthly to anon, authenticated;
