"""Pulse-check: nt_roadmap overdue + live expectancy + latest heavy sims."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

env = Path(__file__).resolve().parents[1] / ".env.local"
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

REF = os.environ.get("SUPABASE_PROJECT_REF") or "pcvscowltlrxzgxjurcr"
TOKEN = os.environ.get("SUPABASE_ACCESS_TOKEN")

# Read the cap from config/feature_flags.yaml (sole source of truth).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from regime_platform.core.feature_flags import get_dynamic_atr_risk_cap

    RISK_CAP = float(get_dynamic_atr_risk_cap())
except Exception:  # pragma: no cover — script must still run standalone
    RISK_CAP = 0.35

QUERIES = {
    "roadmap_open": """
        select mechanism, current_state, status, review_at::text, left(review_condition, 120) as condition, left(notes, 160) as notes
        from nt_roadmap
        where status not in ('resolved','superseded')
        order by review_at nulls first
        limit 30;
    """,
    "roadmap_overdue": """
        select mechanism, status, review_at::text
        from nt_roadmap
        where status not in ('resolved','superseded')
          and (review_at is null or review_at < now())
        order by review_at nulls first;
    """,
    "live_expectancy": """
        select day::text, cohort, n, n_tp, n_sl, n_expired, win_rate_closed, pct_expired, expectancy_r, avg_p_win_bracket
        from nt_live_expectancy_daily
        order by day desc, cohort
        limit 24;
    """,
    "live_expectancy_rollup": """
        select * from v_p_win_bracket_live_expectancy order by "window", cohort;
    """,
    "r2_roadmap": """
        select mechanism, current_state, status, review_at::text, left(notes, 220) as notes
        from nt_roadmap
        where mechanism in ('p_win_in_ev_mode', 'p_win_bracket_mode');
    """,
    "heavy_2d": """
        select count(*) as n_heavy,
               count(*) filter (where qualified) as n_q,
               count(*) filter (where p_win_bracket is not null) as n_br
        from nt_signal_sim
        where source like '%heavy%' and ts > now() - interval '2 days';
    """,
    # B5 (worklog 20260924_B5_VETO_SPLIT_SCOPE.md). Risk is derived from
    # geometry_risk_hat (not the bool) so pre/post 2026-09-24 stamps read the
    # same way. Bar = risk_reject_rate, never total reject incl. bracket.
    "b5_risk_share": f"""
        select
          count(*) AS would_qualify,
          count(distinct symbol) AS symbols,
          min(ts)::date::text AS first_day,
          count(*) filter (where geometry_risk_hat > {RISK_CAP}) AS risk_over,
          count(*) filter (where geometry_risk_hat > {RISK_CAP}
                             and bracket_gate_shadow) AS risk_and_bracket,
          count(*) filter (where geometry_risk_hat <= {RISK_CAP}
                             and bracket_gate_shadow) AS bracket_only,
          round(
            count(*) filter (where geometry_risk_hat > {RISK_CAP})::numeric
            / nullif(count(*) filter (where geometry_risk_hat is not null), 0), 3
          ) AS risk_reject_rate,
          (count(*) >= 50
           and min(ts) <= now() - interval '7 days'
           and count(distinct symbol) >= 8) AS decision_grade
        from nt_signal_sim
        where source = 'sweep-light'
          and qualified_geometry_shadow IS NOT NULL
          and ts >= now() - interval '14 days';
    """,
    # Vetoed vs kept outcomes on the reconstructed gate-pass cohort (larger
    # than stamped rows), one episode per (symbol, side, hour). R = pnl/sl.
    # The cap only earns a tune if vetoed < kept.
    "b5_veto_outcomes": f"""
        with ep as (
          select distinct on (symbol, signal, date_trunc('hour', ts))
                 symbol, outcome, geometry_risk_hat as r,
                 pnl_bricks / nullif(sl_bricks, 0) as r_mult
          from nt_signal_sim
          where source = 'sweep-light'
            and outcome in ('tp_hit', 'sl_hit', 'expired')
            and geometry_risk_hat is not null
            and regime_conf >= 0.60 and effective_kelly >= 0.02 and ev > 0
            and ts >= now() - interval '14 days'
          order by symbol, signal, date_trunc('hour', ts), ts
        )
        select case when r > {RISK_CAP} then 'vetoed' else 'kept' end AS arm,
               count(*) AS episodes, count(distinct symbol) AS symbols,
               round(avg((outcome = 'tp_hit')::int)::numeric, 3) AS tp,
               round(avg((outcome = 'sl_hit')::int)::numeric, 3) AS sl,
               round(avg((outcome = 'expired')::int)::numeric, 3) AS expired,
               round(avg(r_mult)::numeric, 3) AS expectancy_r
        from ep group by 1 order by 1;
    """,
    # Offline cap replay — dynamic_atr_risk_cap is shared with LIVE consumers
    # (heavy publish gate, geometry_tuning_sweep, eod_correction), so a
    # "shadow nudge" of the flag is not possible. Replay candidates here.
    "b5_cap_replay": """
        with ep as (
          select distinct on (symbol, signal, date_trunc('hour', ts))
                 symbol, outcome, geometry_risk_hat as r,
                 pnl_bricks / nullif(sl_bricks, 0) as r_mult
          from nt_signal_sim
          where source = 'sweep-light'
            and outcome in ('tp_hit', 'sl_hit', 'expired')
            and geometry_risk_hat is not null
            and regime_conf >= 0.60 and effective_kelly >= 0.02 and ev > 0
            and ts >= now() - interval '14 days'
          order by symbol, signal, date_trunc('hour', ts), ts
        )
        select cap,
               count(*) filter (where r <= cap) AS kept,
               round(avg(r_mult) filter (where r <= cap)::numeric, 3) AS kept_exp_r,
               count(*) filter (where r > cap) AS vetoed,
               round(avg(r_mult) filter (where r > cap)::numeric, 3) AS vetoed_exp_r
        from ep, unnest(array[0.35, 0.45, 0.60, 0.80, 1.00]) AS cap
        group by cap order by cap;
    """,
    "heavy_recent": """
        select symbol, source, qualified, round(p_win::numeric, 4) as p_win,
               round(p_win_bracket::numeric, 4) as p_win_bracket,
               round(ev::numeric, 4) as ev, regime, ts::text
        from nt_signal_sim
        where source like '%heavy%'
        order by ts desc
        limit 15;
    """,
}


async def run(name: str, sql: str) -> None:
    if not TOKEN:
        print(f"=== {name} ===\n(missing SUPABASE_ACCESS_TOKEN)\n{sql}")
        return
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"https://api.supabase.com/v1/projects/{REF}/database/query",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
            json={"query": sql},
        )
        print(f"=== {name} (HTTP {resp.status_code}) ===")
        print(resp.text[:4000])
        print()


async def main() -> None:
    for name, sql in QUERIES.items():
        await run(name, sql)


if __name__ == "__main__":
    asyncio.run(main())
