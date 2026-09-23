"""Pulse-check: nt_roadmap overdue + live expectancy + latest heavy sims."""

from __future__ import annotations

import asyncio
import os
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
