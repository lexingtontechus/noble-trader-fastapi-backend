#!/usr/bin/env python3
"""
replay_calibration_gap.py — Phase 0 harness: show how calibration looks
when expired outcomes are excluded (status quo) vs included as losses vs
reported as a separate resolve-rate KPI.

Offline fixture always prints. Optional --live pulls nt_signal_sim when
Supabase env is set.

Usage:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/replay_calibration_gap.py
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _fixture_rows() -> list[dict]:
    """Synthetic population: high-expiry wide-TP symbol vs tight-TP symbol."""
    rows: list[dict] = []
    # XAU-like: many expired, few wins
    for i in range(30):
        if i < 20:
            rows.append({"symbol": "XAUUSD", "p_win": 0.70, "outcome": "expired"})
        elif i < 25:
            rows.append({"symbol": "XAUUSD", "p_win": 0.70, "outcome": "tp_hit"})
        else:
            rows.append({"symbol": "XAUUSD", "p_win": 0.70, "outcome": "sl_hit"})
    # EUR-like: more resolved
    for i in range(40):
        if i < 10:
            rows.append({"symbol": "EURUSD", "p_win": 0.58, "outcome": "expired"})
        elif i < 28:
            rows.append({"symbol": "EURUSD", "p_win": 0.58, "outcome": "tp_hit"})
        else:
            rows.append({"symbol": "EURUSD", "p_win": 0.58, "outcome": "sl_hit"})
    return rows


def _stats(rows: list[dict]) -> dict:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["symbol"]].append(r)

    def pack(subset: list[dict], label: str) -> dict:
        n = len(subset)
        if n == 0:
            return {"label": label, "n": 0}
        wins = sum(1 for r in subset if r["outcome"] == "tp_hit")
        losses = sum(1 for r in subset if r["outcome"] == "sl_hit")
        expired = sum(1 for r in subset if r["outcome"] == "expired")
        resolved = wins + losses
        avg_p = sum(float(r.get("p_win") or 0.5) for r in subset) / n
        win_rate_resolved = (wins / resolved) if resolved else None
        bias_resolved = (
            (sum(float(r.get("p_win") or 0.5) for r in subset if r["outcome"] in ("tp_hit", "sl_hit")) / resolved
             - win_rate_resolved)
            if resolved else None
        )
        # Treat expired as loss (honest full-population win rate)
        win_rate_all = wins / n
        bias_all = avg_p - win_rate_all
        return {
            "label": label,
            "n": n,
            "resolved": resolved,
            "expired": expired,
            "expiry_pct": round(100.0 * expired / n, 1),
            "win_pct_resolved": round(100.0 * win_rate_resolved, 1) if win_rate_resolved is not None else None,
            "bias_resolved_only": round(bias_resolved, 4) if bias_resolved is not None else None,
            "win_pct_all_as_loss": round(100.0 * win_rate_all, 1),
            "bias_expired_as_loss": round(bias_all, 4),
        }

    out = [pack(rs, sym) for sym, rs in sorted(by.items())]
    out.append(pack(rows, "ALL"))
    return {"rows": out}


def _print(stats: dict) -> None:
    print("=== Calibration gap replay ===")
    print(
        f"{'symbol':<10} {'n':>5} {'res':>5} {'exp%':>6} "
        f"{'win%res':>8} {'bias_res':>8} {'win%all':>8} {'bias_all':>8}"
    )
    print("-" * 78)
    for row in stats["rows"]:
        if row["n"] == 0:
            continue
        print(
            f"{row['label']:<10} {row['n']:>5} {row['resolved']:>5} "
            f"{row['expiry_pct']:>6.1f} "
            f"{(row['win_pct_resolved'] if row['win_pct_resolved'] is not None else -1):>8.1f} "
            f"{(row['bias_resolved_only'] if row['bias_resolved_only'] is not None else -9):>8.3f} "
            f"{row['win_pct_all_as_loss']:>8.1f} "
            f"{row['bias_expired_as_loss']:>8.3f}"
        )
    print(
        "\nInterpretation: bias_resolved_only is the CURRENT calibration formula "
        "(expired excluded). bias_expired_as_loss is the full-population view. "
        "A large gap between them = reachability/survivorship dominating the "
        "reported calibration. Phase 2 dual-reports both; Phase 3 conditions "
        "p_win on resolve probability."
    )


def run_live(limit: int = 500) -> None:
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
        }
        params = {
            "select": "symbol,p_win,outcome",
            "outcome": "not.is.null",
            "limit": str(limit),
            "order": "ts.desc",
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/rest/v1/nt_signal_sim",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []

    rows = asyncio.run(fetch())
    if not rows:
        print("No live resolved rows.")
        return
    print(f"\n=== Live calibration gap ({len(rows)} rows) ===")
    _print(_stats(rows))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--limit", type=int, default=500)
    args = p.parse_args()
    _print(_stats(_fixture_rows()))
    if args.live:
        run_live(limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
