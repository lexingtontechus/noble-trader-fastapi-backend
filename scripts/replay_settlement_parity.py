#!/usr/bin/env python3
"""
replay_settlement_parity.py — same-day proof harness for Batch A Phase 1.

Compares settlement policies (tp_first vs sl_first) and OHLC vs close-only
on a synthetic fixture series, and optionally on live nt_signal_sim rows
when SUPABASE_URL + key are available.

Usage (offline fixture — always works):
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/replay_settlement_parity.py

Usage (live pull, optional):
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/replay_settlement_parity.py --live

Exit 0 always prints a table; use the numbers in the worklog, do not
treat exit code as go/no-go by itself.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from regime_platform.sweep_orchestrator import settlement_contract as sc  # noqa: E402


def _fixture_series() -> list[dict]:
    """Deterministic multi-scenario fixture (no network)."""
    return [
        # name, entry, sl, tp, closes, ohlc_bars, direction, tp_bricks, sl_bricks, bs
        {
            "name": "buy_tp_clean",
            "entry": 100.0, "sl": 97.0, "tp": 106.0,
            "closes": [100.5, 102.0, 107.0],
            "ohlc": [
                {"close": 100.5, "high": 101.0, "low": 100.0},
                {"close": 102.0, "high": 103.0, "low": 100.2},
                {"close": 107.0, "high": 108.0, "low": 101.5},
            ],
            "direction": "buy", "tp_bricks": 6, "sl_bricks": 3, "brick_size": 1.0,
        },
        {
            "name": "buy_sl_clean",
            "entry": 100.0, "sl": 97.0, "tp": 106.0,
            "closes": [99.0, 96.5],
            "ohlc": [
                {"close": 99.0, "high": 99.5, "low": 98.0},
                {"close": 96.5, "high": 99.0, "low": 96.0},
            ],
            "direction": "buy", "tp_bricks": 6, "sl_bricks": 3, "brick_size": 1.0,
        },
        {
            "name": "buy_both_same_close",
            "entry": 100.0, "sl": 97.0, "tp": 106.0,
            "closes": [107.0],  # close >= TP; also > SL side if inverted — both true
            "ohlc": [
                {"close": 107.0, "high": 108.0, "low": 96.0},  # both high and low pierce
            ],
            "direction": "buy", "tp_bricks": 6, "sl_bricks": 3, "brick_size": 1.0,
        },
        {
            "name": "buy_intraday_only",
            "entry": 100.0, "sl": 97.0, "tp": 106.0,
            "closes": [100.0, 101.0, 100.5],  # never close past
            "ohlc": [
                {"close": 100.0, "high": 100.5, "low": 99.5},
                {"close": 101.0, "high": 106.5, "low": 100.5},  # high pierces TP
                {"close": 100.5, "high": 101.0, "low": 100.0},
            ],
            "direction": "buy", "tp_bricks": 6, "sl_bricks": 3, "brick_size": 1.0,
        },
        {
            "name": "buy_expire_mtm",
            "entry": 100.0, "sl": 90.0, "tp": 120.0,  # wide
            "closes": [101.0, 102.0, 101.5],
            "ohlc": [
                {"close": 101.0, "high": 102.0, "low": 100.0},
                {"close": 102.0, "high": 103.0, "low": 100.5},
                {"close": 101.5, "high": 102.5, "low": 100.8},
            ],
            "direction": "buy", "tp_bricks": 20, "sl_bricks": 10, "brick_size": 1.0,
        },
        {
            "name": "sell_tp",
            "entry": 1.2000, "sl": 1.2100, "tp": 1.1900,
            "closes": [1.1980, 1.1880],
            "ohlc": [
                {"close": 1.1980, "high": 1.2010, "low": 1.1970},
                {"close": 1.1880, "high": 1.1990, "low": 1.1870},
            ],
            "direction": "sell", "tp_bricks": 5, "sl_bricks": 2, "brick_size": 0.001,
        },
    ]


def _run_case(case: dict, policy: str, bars) -> sc.SettlementResult:
    return sc.settle(
        entry_price=case["entry"],
        stop_loss=case["sl"],
        take_profit=case["tp"],
        bars=bars,
        direction=case["direction"],
        same_bar_policy=policy,
        tp_bricks=case["tp_bricks"],
        sl_bricks=case["sl_bricks"],
        brick_size=case["brick_size"],
    )


def _summarize(results: list[sc.SettlementResult]) -> dict:
    outcomes = Counter(r.outcome for r in results)
    n = len(results) or 1
    pnl = sum(r.pnl_dollars for r in results)
    return {
        "n": len(results),
        "tp": outcomes.get("tp_hit", 0),
        "sl": outcomes.get("sl_hit", 0),
        "exp": outcomes.get("expired", 0),
        "win_pct": round(100.0 * outcomes.get("tp_hit", 0) / n, 1),
        "exp_pct": round(100.0 * outcomes.get("expired", 0) / n, 1),
        "pnl": round(pnl, 4),
    }


def run_fixture() -> None:
    cases = _fixture_series()
    configs = [
        ("close/tp_first", sc.SAME_BAR_TP_FIRST, "closes"),
        ("close/sl_first", sc.SAME_BAR_SL_FIRST, "closes"),
        ("ohlc/tp_first", sc.SAME_BAR_TP_FIRST, "ohlc"),
        ("ohlc/sl_first", sc.SAME_BAR_SL_FIRST, "ohlc"),
    ]
    print("=== Settlement parity replay (fixture) ===")
    header = f"{'case':<22} {'config':<16} {'outcome':<8} {'exit':>10} {'bricks':>8} {'pnl$':>10}"
    print(header)
    print("-" * len(header))
    for case in cases:
        for label, policy, kind in configs:
            bars = case["closes"] if kind == "closes" else case["ohlc"]
            r = _run_case(case, policy, bars)
            print(
                f"{case['name']:<22} {label:<16} {r.outcome:<8} "
                f"{(r.exit_price if r.exit_price is not None else float('nan')):>10.4f} "
                f"{r.pnl_bricks:>8.2f} {r.pnl_dollars:>10.4f}"
            )
    print()
    for label, policy, kind in configs:
        results = []
        for case in cases:
            bars = case["closes"] if kind == "closes" else case["ohlc"]
            results.append(_run_case(case, policy, bars))
        s = _summarize(results)
        print(
            f"SUMMARY {label:<16} n={s['n']} win%={s['win_pct']} "
            f"exp%={s['exp_pct']} pnl={s['pnl']}"
        )


def run_live(limit: int = 50) -> None:
    import asyncio
    import httpx

    url = os.environ.get("SUPABASE_URL") or ""
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or ""
    if not url or not key:
        print("SUPABASE_URL / key not set — skipping live pull.")
        return

    async def fetch() -> list[dict]:
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        params = {
            "select": "symbol,signal,entry_price,stop_loss,take_profit,tp_bricks,sl_bricks,brick_size",
            "outcome": "is.null",
            "order": "ts.asc",
            "limit": str(limit),
        }
        # Prefer resolved rows for a richer comparison when present
        params_resolved = {
            "select": "symbol,signal,entry_price,stop_loss,take_profit,tp_bricks,sl_bricks,brick_size,outcome",
            "outcome": "not.is.null",
            "order": "resolved_at.desc",
            "limit": str(limit),
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/rest/v1/nt_signal_sim",
                headers=headers,
                params=params_resolved,
            )
            resp.raise_for_status()
            return resp.json() if isinstance(resp.json(), list) else []

    rows = asyncio.run(fetch())
    if not rows:
        print("No live rows returned.")
        return
    print(f"\n=== Live replay ({len(rows)} resolved rows) — close-only policy A/B ===")
    print(f"{'symbol':<10} {'dir':<5} {'stored':<8} {'tp_first':<8} {'sl_first':<8} {'pnl_tp':>10} {'pnl_sl':>10}")
    print("-" * 70)
    mismatches = 0
    for row in rows:
        entry = float(row["entry_price"])
        sl = float(row["stop_loss"])
        tp = float(row["take_profit"])
        # Synthetic walk: we do not re-fetch bars here without a candle source;
        # print stored outcome vs what contract would need bars for.
        # Placeholder: mark that live bar replay needs price_fetcher.
        stored = row.get("outcome") or "?"
        print(
            f"{row.get('symbol',''):<10} {str(row.get('signal',''))[:5]:<5} "
            f"{stored:<8} {'(bars?)':<8} {'(bars?)':<8} {'-':>10} {'-':>10}"
        )
    print(
        "\nNote: full live bar replay requires post-entry candles via "
        "price_fetcher — use fixture table for Batch A sign-off; wire "
        "candle fetch in a follow-up if needed."
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    args = p.parse_args()
    run_fixture()
    if args.live:
        run_live(limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
