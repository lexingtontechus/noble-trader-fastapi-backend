"""replay_router_harmonization.py — replay verification for HARM-01.

Re-runs the 2026-10-13 router replay (route() on every last-12h resolved
nt_signal_sim row, using each row's own macro_label/trend_label/regime and
its REAL nt_symbol.asset_class) and prints before/after.

  BEFORE (2026-10-13): routed 0/225, stand_by 100%
  AFTER  (HARM-01)   : metals become reachable

Run from the project venv:
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/replay_router_harmonization.py

Read-only: no writes to Supabase, no config mutation.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_env = ROOT / ".env.local"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}

SQL = """
    select s.symbol, s.signal, s.regime, s.macro_label, s.trend_label, s.outcome,
      round((s.pnl_bricks/nullif(s.sl_bricks,0))::numeric,3) r_mult,
      s.strategy_archetype, sym.asset_class
    from nt_signal_sim s
    left join nt_symbol sym on sym.symbol = s.symbol
    where s.source='sweep-light' and s.ts > now() - interval '12 hours'
      and s.outcome in ('tp_hit','sl_hit','expired') and s.exit_ts is not null
    order by s.symbol, s.ts;
"""


def replay(rows, **kw):
    """Route every row; return {(symbol, signal, archetype): stats}."""
    import regime_platform.sweep_orchestrator.strategy_router as sr
    import regime_platform.sweep_orchestrator.tiered_atr as ta

    orig_ack = ta.asset_class_key
    orig_align = sr._alignment
    try:
        if kw.get("commodity_as_metal"):
            ta.asset_class_key = lambda a: "metal" if (a and "commod" in str(a).lower()) else orig_ack(a)
        if kw.get("legacy_vocabulary"):
            # Reproduce the pre-HARM-01 registry: metals unreachable.
            ta.asset_class_key = lambda a: "unreachable_metal" if (a and "commod" in str(a).lower()) else orig_ack(a)
        if kw.get("allow_neutral"):
            # Neutral-tolerating alignment: derive a directional verdict from
            # whichever slow read IS directional. Isolates HARM-01 by removing
            # the HARM-04 blocker so the asset-class effect is observable.
            def relaxed(direction, macro_label, trend_label):
                if not direction:
                    return None
                want = "bull" if direction == "buy" else "bear"
                md = None
                if macro_label is not None and str(macro_label).lower() in ("bull", "bear"):
                    md = "with" if str(macro_label).lower() == want else "against"
                td = None
                tl = str(trend_label).lower() if trend_label else None
                if tl in ("trending_up", "reversal_up", "trending_down", "reversal_down"):
                    bull = tl in ("trending_up", "reversal_up")
                    td = "with" if bull == (want == "bull") else "against"
                votes = [v for v in (md, td) if v]
                if not votes:
                    return None
                if all(v == "against" for v in votes):
                    return "opposes"
                if all(v == "with" for v in votes):
                    return "aligned"
                return None

            sr._alignment = relaxed
        from regime_platform.sweep_orchestrator.strategy_router import route

        out = {}
        for r in rows:
            v = route(
                symbol=r["symbol"],
                signal=r["signal"],
                regime_label=r["regime"],
                asset_class=r["asset_class"],
                macro_label=r["macro_label"],
                trend_label=r["trend_label"],
            )
            k = (r["symbol"], r["signal"], v.archetype)
            d = out.setdefault(k, {"n": 0, "tp": 0, "r": 0.0})
            d["n"] += 1
            d["tp"] += 1 if r["outcome"] == "tp_hit" else 0
            d["r"] += float(r["r_mult"] or 0)
        return out
    finally:
        ta.asset_class_key = orig_ack
        sr._alignment = orig_align


async def fetch():
    async with httpx.AsyncClient(timeout=90.0) as c:
        r = await c.post(
            f"https://api.supabase.com/v1/projects/"
            f"{os.environ.get('SUPABASE_URL', '').rstrip('/').split('//')[-1].split('.')[0]}"
            f"/database/query",
            headers={
                "Authorization": f"Bearer {os.environ['SUPABASE_ACCESS_TOKEN']}",
                "Content-Type": "application/json",
            },
            json={"query": SQL},
        )
        r.raise_for_status()
        return r.json()


def show(name, out):
    routed = {k: d for k, d in out.items() if k[2] != "stand_by"}
    tot = sum(d["n"] for d in out.values())
    rt = sum(d["n"] for d in routed.values())
    print(f"\n=== {name} ===  routed {rt}/{tot} ({rt / tot:.1%})")
    if not routed:
        print("  (nothing routed)\n")
        return
    print(f"  {'symbol':<8}{'sig':<6}{'routed':<16}{'n':>5}{'tp%':>7}{'avgR':>8}")
    for (sym, sig, arch), d in sorted(routed.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {sym:<8}{sig:<6}{arch:<16}{d['n']:>5}{d['tp'] / d['n']:>7.1%}{d['r'] / d['n']:>8.3f}")
    print()


def main():
    rows = asyncio.run(fetch())
    print(f"rows fetched: {len(rows)}")
    syms = sorted({r["symbol"] for r in rows})
    print(f"symbols: {syms}")

    # BEFORE: reproduce the pre-fix vocabulary (metals unreachable), with the
    # HARM-04 blocker also removed so the two runs differ ONLY by HARM-01.
    show("BEFORE — pre-HARM-01 (metals unreachable)", replay(rows, legacy_vocabulary=True, allow_neutral=True))
    # AFTER: the shipped registry, same HARM-04 relaxation.
    show("AFTER — HARM-01 shipped (config/strategy_registry.yaml)", replay(rows, allow_neutral=True))
    # As-shipped, no relaxation: the honest current state.
    show("AS-SHIPPED (no relaxation) — HARM-04 still binding", replay(rows))
    # The 2026-10-13 counterfactual D, kept for comparison with that report.
    show(
        "2026-10-13 counterfactual D (both blockers removed)", replay(rows, commodity_as_metal=True, allow_neutral=True)
    )

    print("BEFORE vs AFTER differ only by the asset-class vocabulary, so any")
    print("difference between them is HARM-01's effect in isolation.")
    print("AS-SHIPPED shows the live state: HARM-04 (daily macro/trend label")
    print("refresh vs 15-min signal cadence) is still the binding constraint.")


if __name__ == "__main__":
    main()
