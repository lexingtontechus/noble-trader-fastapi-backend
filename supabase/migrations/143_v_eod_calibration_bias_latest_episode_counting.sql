-- 2026-09-08, worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md:
-- v_eod_calibration_bias_latest counted every raw nt_calibration_records
-- row as one independent trial (plain COUNT(*)). USDCHF showed why that's
-- wrong: 294 raw rows, entered every ~5 minutes over one continuous ~68h
-- price move, all resolving the same way -- one real episode of evidence,
-- not 294. This view feeds calibration_enforcement.fetch_historical_p_win()
-- -> the EV blend's p_historical source for EVERY symbol, so the same
-- over-counting silently affected every symbol's blend input, not just
-- USDCHF (confirmed: most symbols collapse to 1-7 real episodes across a
-- 7-day window when checked directly).
--
-- Fix: group nt_calibration_records into independent EPISODES first
-- (records whose [entered_at, closed_at] windows overlap are riding the
-- same underlying price move) via a standard interval-merge (running max
-- of closed_at, not just the immediately preceding row -- an earlier
-- long-hold record can still overlap a later one even if a short-hold
-- record in between already closed). Each episode collapses to ONE
-- representative point (its own average pred / win fraction) before the
-- final per-symbol aggregation, so "n" is a real independent-evidence
-- count instead of a raw row count.
--
-- INSUFFICIENT_DATA threshold changed from "< 20 raw rows" to "< 2
-- episodes" -- mirrors calibration_bias_min_episodes in
-- config/feature_flags.yaml (default 2, used by the parallel Python path
-- fetch_calibration_bias/_raw, supabase_client.py). These two thresholds
-- are NOT read from the same config (SQL views can't read YAML) --
-- if calibration_bias_min_episodes changes there, this view's hardcoded
-- "< 2" should be updated to match, manually.
--
-- n_episodes replaces the old bare "n" as the sufficiency/Bayesian-shrink
-- sample size; raw_n (new column) keeps the original row count for
-- observability only.

create or replace view v_eod_calibration_bias_latest as
with base as (
    select symbol, entered_at, closed_at, predicted_p_win, predicted_p_win_raw, outcome
    from nt_calibration_records
    where outcome = any (array['win', 'loss'])
      and entered_at >= now() - interval '7 days'
),
ordered as (
    select
        *,
        max(closed_at) over (
            partition by symbol order by entered_at
            rows between unbounded preceding and 1 preceding
        ) as running_max_closed_at
    from base
),
grouped as (
    select
        *,
        sum(
            case
                when running_max_closed_at is null or entered_at > running_max_closed_at
                then 1 else 0
            end
        ) over (partition by symbol order by entered_at) as episode
    from ordered
),
episodes as (
    select
        symbol,
        episode,
        count(*) as raw_n_in_episode,
        avg(predicted_p_win) as ep_avg_pred,
        avg(coalesce(predicted_p_win_raw, predicted_p_win)) as ep_avg_pred_raw,
        avg(case when outcome = 'win' then 1.0 else 0.0 end) as ep_win_fraction
    from grouped
    group by symbol, episode
)
select
    (now())::date as day,
    symbol,
    avg(ep_avg_pred) as avg_predicted_p_win,
    avg(ep_win_fraction) as realized_win_rate,
    (avg(ep_avg_pred) - avg(ep_win_fraction))::double precision as bias,
    case
        when count(*) < 2 then 'INSUFFICIENT_DATA'
        when (avg(ep_avg_pred) - avg(ep_win_fraction)) > 0.10 then 'OVERCONFIDENT'
        when (avg(ep_avg_pred) - avg(ep_win_fraction)) < -0.10 then 'UNDERCONFIDENT'
        else 'CALIBRATED'
    end as status,
    avg(ep_avg_pred_raw) as avg_predicted_p_win_raw,
    count(*) as n,
    (avg(ep_avg_pred_raw) - avg(ep_win_fraction))::double precision as bias_raw,
    case
        when count(*) < 2 then 'INSUFFICIENT_DATA'
        when (avg(ep_avg_pred_raw) - avg(ep_win_fraction)) >= 0.30 then 'OVERCONFIDENT'
        when (avg(ep_avg_pred_raw) - avg(ep_win_fraction)) <= -0.20 then 'UNDERCONFIDENT'
        else 'CALIBRATED'
    end as status_raw,
    sum(raw_n_in_episode) as raw_n
from episodes
group by symbol;

comment on view v_eod_calibration_bias_latest is
    '7-day rolling calibration bias per symbol, episode-counted (2026-09-08 fix -- migration 143). n/status are computed over independent EPISODES (overlapping [entered_at, closed_at] windows merged), not raw nt_calibration_records rows -- see worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md. raw_n retained for observability only, no longer used for sufficiency. Feeds calibration_enforcement.fetch_historical_p_win() (EV blend p_historical source).';
