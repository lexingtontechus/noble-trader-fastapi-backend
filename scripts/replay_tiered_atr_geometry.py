#!/usr/bin/env python3
"""
replay_tiered_atr_geometry.py — long-lag % as first-class KPI

Worklog: 20260918_TIERED_ATR_GEOMETRY_SCOPE.md Phase A2/B2.

Compares brick geometries by predicted/observed long-lag (expired) share
under 48h settlement, not Sharpe alone.

Offline fixture always prints. --live pulls nt_bracket_reachability +
nt_signal_sim when Supabase env is set.

Usage:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/replay_tiered_atr_geometry.py
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from regime_platform.sweep_orchestrator.geometry_risk import (
    GEOMETRY_RISK_CAP,
    compute_geometry_metrics,
    geometry_risk_hat,
)


def _fixture_buckets() -> list[dict]:
    """Illustrative (symbol, bucket, n, pct_expired, p90) — replace with live."""
    return [
        {"symbol": "XAUUSD", "bucket": "4+", "n": 200, "pct_expired": 0.65, "p90": 140.0},
        {"symbol": "XAUUSD", "bucket": "2-4", "n": 80, "pct_expired": 0.40, "p90": 90.0},
        {"symbol": "XAUUSD", "bucket": "0.5-1", "n": 40, "pct_expired": 0.15, "p90": 30.0},
        {"symbol": "EURUSD", "bucket": "4+", "n": 300, "pct_expired": 0.98, "p90": 200.0},
        {"symbol": "EURUSD", "bucket": "1-2", "n": 50, "pct_expired": 0.35, "p90": 70.0},
        {"symbol": "AAPL", "bucket": "4+", "n": 120, "pct_expired": 0.20, "p90": 48.0},
        {"symbol": "AAPL", "bucket": "0.5-1", "n": 60, "pct_expired": 0.05, "p90": 12.0},
    ]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    args = p.parse_args()

    rows = _fixture_buckets() if not args.live else _fixture_buckets()
    if args.live:
        import asyncio
        import httpx
        from regime_platform.core.settings import settings
        if settings.SUPABASE_URL:
            async def fetch():
                headers = {
                    "apikey": settings.SUPABASE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                }
                async with httpx.AsyncClient(timeout=30.0) as c:
                    r = await c.get(
                        f"{settings.SUPABASE_URL.rstrip('/')}/rest/v1/nt_bracket_reachability",
                        headers=headers,
                        params={
                            "select": "symbol,distance_atr_bucket,n,n_stored,pct_expired,p90_hours",
                            "signal": "eq.all",
                            "limit": "200",
                        },
                    )
                    r.raise_for_status()
                    return r.json() or []
            live = asyncio.run(fetch())
            if live:
                rows = [
                    {
                        "symbol": x["symbol"],
                        "bucket": x["distance_atr_bucket"],
                        "n": x.get("n") or 0,
                        "pct_expired": x.get("pct_expired"),
                        "p90": x.get("p90_hours"),
                    }
                    for x in live
                ]

    print("=== Tiered ATR / long-lag replay ===")
    print("KPI: long_lag % (expired) is geometry risk — lower is better")
    print(f"{'symbol':<9} {'bucket':<7} {'n':>6} {'lag%':>6} {'p90h':>7} {'risk':>6} {'flag':>5}")
    print("-" * 52)
    by_sym = defaultdict(lambda: {"n": 0, "lag_w": 0.0, "risks": []})
    for r in rows:
        lag = r.get("pct_expired")
        if lag is None:
            continue
        risk = geometry_risk_hat(float(lag), r.get("p90"))
        flag = "YES" if risk >= GEOMETRY_RISK_CAP else ""
        print(
            f"{r['symbol']:<9} {r['bucket']:<7} {r['n']:>6} "
            f"{100*lag:>5.1f}% {(r.get('p90') or 0):>7.1f} {risk:>6.3f} {flag:>5}"
        )
        by_sym[r["symbol"]]["n"] += r["n"]
        by_sym[r["symbol"]]["lag_w"] += r["n"] * float(lag)
        by_sym[r["symbol"]]["risks"].append(risk)

    print()
    print("=== Prefer narrower geometry when lag risk high ===")
    print(f"{'symbol':<9} {'wlag%':>7} {'max_risk':>8} {'action':<24}")
    for sym, d in sorted(by_sym.items()):
        if d["n"] <= 0:
            continue
        wlag = d["lag_w"] / d["n"]
        mx = max(d["risks"]) if d["risks"] else 0
        action = "NARROW ATR candidates" if mx >= GEOMETRY_RISK_CAP else "ok / optional tighten"
        print(f"{sym:<9} {100*wlag:>6.1f}% {mx:>8.3f} {action:<24}")

    print(
        "\nPass for live ATR flip: long-lag % down on symbols with high 4+ share; "
        "no symbol loses short-horizon win% without written rationale."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
