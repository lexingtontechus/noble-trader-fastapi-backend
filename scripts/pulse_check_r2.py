"""Pulse-check: roadmap overdue + live expectancy + latest heavy sims.

Starts with a **liveness gate**: every writer's heartbeat age vs its expected
cadence, printed first and loud. Exit code **2** means a fast heartbeat (sweep /
signal / PBM stamp) is stale — the signal engine is not producing. The full
report still prints before the exit, so a scheduled run leaves evidence.

Also runs the PBM Phase B calibration audit (``measure_pbm_calibration.py``) as
a subprocess — the instrument, not a copy — plus the cheap PBM ledger-state SQL
and the phase flags. ``--no-pbm`` skips just that step; ``--no-liveness`` skips
the gate.

    PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/pulse_check_r2.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
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

# Ref parsed from SUPABASE_URL (the variable .env.local carries).
REF = os.environ.get("SUPABASE_URL", "").rstrip("/").split("//")[-1].split(".")[0]
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
        select mechanism, current_state, status, review_at::text,
               left(review_condition, 120) as condition, left(notes, 160) as notes
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
        select day::text, cohort, n, n_tp, n_sl, n_expired, win_rate_closed,
               pct_expired, expectancy_r, avg_p_win_bracket
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
    # PBM Phase B ledger state. The calibration audit below is the decision
    # instrument; this is the cheap "is it still collecting / is the window
    # mature yet" read. sim_expired_since_ledger is the maturity gate: the
    # full-triple score is not fair until real expiries land (on a zero-expiry
    # window ~55% of its delta is the resolve-mass term — see the audit's
    # "Log-score decomposition" section).
    "pbm_ledger_state": """
        select
          (select count(*) from nt_pbm_shadow) as stamps_total,
          (select count(*) from nt_pbm_shadow where era = 'live') as stamps_live,
          (select count(*) from nt_pbm_shadow where outcome is null) as stamps_outcome_null,
          (select min(ts)::date::text from nt_pbm_shadow) as ledger_first_day,
          (select max(ts)::text from nt_pbm_shadow) as ledger_last_stamp,
          (select count(*) from nt_cell_posterior) as posterior_cells,
          (select count(distinct symbol) from nt_cell_posterior) as posterior_symbols,
          (select count(*) from nt_signal_sim s
             where s.ts >= (select min(ts) from nt_pbm_shadow)) as sim_since_ledger,
          (select count(*) from nt_signal_sim s
             where s.ts >= (select min(ts) from nt_pbm_shadow)
               and s.outcome in ('tp_hit','sl_hit','expired')) as sim_resolved_since_ledger,
          (select count(*) from nt_signal_sim s
             where s.ts >= (select min(ts) from nt_pbm_shadow)
               and s.outcome = 'expired') as sim_expired_since_ledger;
    """,
}


# ── Liveness gate ───────────────────────────────────────────────────────────
# Runs FIRST so an outage is the first line of output, not a footnote. On
# 2026-10-16 the Lightning platform died at 03:07 UTC and the sweep sat silent
# for ~21h while this script happily reported a 1908-row ledger; nothing
# compared a heartbeat to now(). Thresholds are ~2x the expected cadence.
#   (label, json key, stale-after minutes, expected cadence)
HEARTBEATS: tuple[tuple[str, str, float, str], ...] = (
    ("sweep", "sweep_age_min", 30, "light sweep every 15m"),
    ("signal_sim", "signal_age_min", 30, "one row per emitted signal"),
    ("pbm_stamp", "pbm_stamp_age_min", 30, "stamped with the sim row"),
    ("roadmap_heartbeat", "roadmap_age_min", 150, "hourly kairos-daily"),
    ("cell_posterior", "cell_posterior_age_min", 400, "resolver + rebuild"),
    ("calib_state", "calib_state_age_min", 400, "4h correction slots"),
    ("macro_regime", "macro_age_min", 1800, "daily 06:00/06:10 UTC"),
    ("bracket_reachability", "reachability_age_min", 11520, "nightly + weekly Sun"),
)

# The engine is not producing if any of these is stale.
FAST_HEARTBEATS = frozenset({"sweep", "signal_sim", "pbm_stamp"})

LIVENESS_SQL = """
select
  round(extract(epoch from now() - (select max(sweep_timestamp) from nt_sweep_result))/60.0, 1)
    as sweep_age_min,
  round(extract(epoch from now() - (select max(ts) from nt_signal_sim))/60.0, 1)
    as signal_age_min,
  round(extract(epoch from now() - (select max(ts) from nt_pbm_shadow))/60.0, 1)
    as pbm_stamp_age_min,
  round(extract(epoch from now() - (select max(updated_at) from nt_roadmap))/60.0, 1)
    as roadmap_age_min,
  round(extract(epoch from now() - (select max(updated_at) from nt_cell_posterior))/60.0, 1)
    as cell_posterior_age_min,
  round(extract(epoch from now() - (select max(computed_at) from nt_symbol_calibration_state))/60.0, 1)
    as calib_state_age_min,
  round(extract(epoch from now() - (select max(computed_at) from nt_macro_regime))/60.0, 1)
    as macro_age_min,
  round(extract(epoch from now() - (select max(computed_at) from nt_bracket_reachability))/60.0, 1)
    as reachability_age_min;
"""


def _fmt_age(minutes: float | None) -> str:
    if minutes is None:
        return "never"
    if minutes < 90:
        return f"{minutes:.0f}m"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


async def check_liveness() -> bool:
    """Loud heartbeat gate. Returns True when the signal engine is producing."""
    print("=== LIVENESS ===")
    if not TOKEN:
        print("  (missing SUPABASE_ACCESS_TOKEN — cannot check)\n")
        return True
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"https://api.supabase.com/v1/projects/{REF}/database/query",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"query": LIVENESS_SQL},
            )
            resp.raise_for_status()
            row = resp.json()[0]
        except Exception as exc:
            print(f"  *** LIVENESS QUERY FAILED: {exc} ***\n")
            return False

    dead: list[str] = []
    stale: list[str] = []
    for label, key, limit, note in HEARTBEATS:
        raw = row.get(key)
        try:
            # the management API returns round(numeric, n) as a JSON STRING
            age = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            age = None
        if age is None:
            state = "NEVER"
            dead.append(label)
        elif age > limit * 2:
            state = "DEAD"
            dead.append(label)
        elif age > limit:
            state = "STALE"
            stale.append(label)
        else:
            state = "ok"
        mark = " " if state == "ok" else "!"
        print(f"  {mark} {label:22s} {_fmt_age(age):>7s}  {state:<5s} (stale > {_fmt_age(limit)}; {note})")

    fast_down = [name for name in dead + stale if name in FAST_HEARTBEATS]
    print()
    if fast_down:
        print(f"  *** SIGNAL ENGINE DOWN — {', '.join(fast_down)} not producing ***")
    elif dead or stale:
        print(f"  ! {len(dead)} dead / {len(stale)} stale — no fast heartbeat affected")
    else:
        print("  all heartbeats fresh")
    print()
    return not fast_down


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


def print_pbm_flags() -> None:
    """PBM phase flags — config/feature_flags.yaml is the sole source of truth.

    Only ``pbm_ev_mode == 'live'`` swaps the EV input (light
    ``orchestrator.py:4644``, heavy ``heavy_sweep.py:1590``); ``'shadow'`` adds
    a log line and nothing else.
    """
    print("=== pbm_flags (config/feature_flags.yaml) ===")
    try:
        from regime_platform.core.feature_flags import (
            get_pbm_ev_mode,
            get_pbm_geometry_mode,
            get_pbm_mode,
            get_pbm_sizing_mode,
        )

        print(f"  pbm_mode           = {get_pbm_mode():<8s} # stamps + resolver posterior writes")
        print(f"  pbm_ev_mode        = {get_pbm_ev_mode():<8s} # Phase B EV input swap (only 'live' swaps)")
        print(f"  pbm_geometry_mode  = {get_pbm_geometry_mode():<8s} # Phase C continuous solve")
        print(f"  pbm_sizing_mode    = {get_pbm_sizing_mode():<8s} # Phase D bound-scaled Kelly")
    except Exception as exc:  # pragma: no cover — script must still run standalone
        print(f"  (unreadable: {exc})")
    print()


def run_pbm_calibration(since: str | None, extra: str | None) -> None:
    """Run the Phase B calibration audit as a subprocess — the instrument itself,
    not a reimplementation — and print its summary lines.

    The audit is the decision instrument for the Phase B flip; running it here
    keeps the bar tracked instead of remembered. It writes its own report to
    worklog/kairos/reports/.
    """
    script = Path(__file__).resolve().parent / "measure_pbm_calibration.py"
    if not script.exists():
        print("=== pbm_calibration ===\n(scripts/measure_pbm_calibration.py not found)\n")
        return
    cmd = [sys.executable, str(script)]
    if since:
        cmd += ["--since", since]
    if extra:
        cmd += extra.split()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        )
    except Exception as exc:  # pragma: no cover
        print(f"=== pbm_calibration ===\n(failed to launch: {exc})\n")
        return
    print(f"=== pbm_calibration (exit {proc.returncode}) ===")
    for line in ((proc.stdout or "") + (proc.stderr or "")).splitlines():
        if "INFO httpx" in line:
            continue
        print(line)
    print()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Pulse check: roadmap / expectancy / B5 / PBM calibration.",
    )
    ap.add_argument(
        "--no-pbm",
        action="store_true",
        help="skip the PBM calibration audit step (SQL + flag reads still run)",
    )
    ap.add_argument(
        "--no-liveness",
        action="store_true",
        help="skip the heartbeat gate (it runs first and sets the exit code)",
    )
    ap.add_argument(
        "--pbm-since",
        default=None,
        metavar="ISO",
        help="lookback for the calibration audit (default: the harness's own 60 days)",
    )
    ap.add_argument(
        "--pbm-args",
        default=None,
        help="extra args passed through to measure_pbm_calibration.py (e.g. '--min-n 200')",
    )
    return ap.parse_args(argv)


async def main() -> None:
    args = parse_args()
    live = True
    if not args.no_liveness:
        live = await check_liveness()
    for name, sql in QUERIES.items():
        await run(name, sql)
    print_pbm_flags()
    if not args.no_pbm:
        run_pbm_calibration(args.pbm_since, args.pbm_args)
    if not live:
        # Exit 2 = the signal engine is not producing. The full report still
        # prints first, so a scheduled run leaves evidence behind.
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
