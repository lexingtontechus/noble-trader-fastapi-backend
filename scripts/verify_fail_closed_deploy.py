"""Verify fail-closed deploy + SQL 169 after Studio restart (2026-09-24)."""
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
    "m169_view_exists": """
        select table_name
        from information_schema.views
        where table_name = 'v_bracket_unscored_rejects';
    """,
    "m169_columns": """
        select column_name
        from information_schema.columns
        where table_name = 'nt_signal_sim'
          and column_name in ('gate_outcome','suppress_reason','failed_gates','p_win_bracket')
        order by column_name;
    """,
    "gate_outcome_24h": """
        select gate_outcome, coalesce(suppress_reason,'-') as reason, count(*) as n
        from nt_signal_sim
        where ts > now() - interval '24 hours'
          and signal in ('buy','sell')
        group by 1,2
        order by n desc
        limit 30;
    """,
    "bracket_unscored_24h": """
        select count(*) as n_unscored_reject,
               count(distinct symbol) as symbols,
               min(ts)::text as first_ts,
               max(ts)::text as last_ts
        from nt_signal_sim
        where suppress_reason = 'bracket_unscored'
          and ts > now() - interval '24 hours';
    """,
    "qualified_vs_gate_24h": """
        select
          count(*) filter (where qualified) as qualified_true,
          count(*) filter (where gate_outcome = 'qualified') as go_qualified,
          count(*) filter (where qualified and gate_outcome is distinct from 'qualified') as mismatch_q,
          count(*) filter (where not qualified and gate_outcome = 'qualified') as mismatch_go,
          count(*) filter (where gate_outcome is null) as go_null
        from nt_signal_sim
        where ts > now() - interval '24 hours' and signal in ('buy','sell');
    """,
    "p_bracket_coverage_light": """
        select
          count(*) as n,
          count(*) filter (where p_win_bracket is not null) as n_scored,
          count(*) filter (where p_win_bracket is null) as n_unscored,
          count(*) filter (where qualified) as n_qualified
        from nt_signal_sim
        where source like '%light%'
          and ts > now() - interval '24 hours'
          and signal in ('buy','sell');
    """,
    "light_qualified_recent": """
        select symbol, signal, qualified, gate_outcome, suppress_reason,
               round(p_win::numeric,3) as p_dir,
               round(p_win_bracket::numeric,3) as p_br,
               bracket_bucket, ts::text
        from nt_signal_sim
        where source like '%light%'
          and ts > now() - interval '48 hours'
          and signal in ('buy','sell')
          and (qualified or suppress_reason is not null or gate_outcome is not null)
        order by ts desc
        limit 20;
    """,
    "roadmap_r2": """
        select mechanism, status, review_at::text, left(notes, 160) as notes
        from nt_roadmap
        where mechanism in ('p_win_in_ev_mode','p_win_bracket_mode','light_publish_geometry_gate')
        order by mechanism;
    """,
}


async def run(name: str, sql: str) -> None:
    if not TOKEN:
        print(f"=== {name} ===\n(missing SUPABASE_ACCESS_TOKEN)\n{sql}\n")
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
        print(resp.text[:5000])
        print()


async def main() -> None:
    for name, sql in QUERIES.items():
        await run(name, sql)


if __name__ == "__main__":
    asyncio.run(main())
