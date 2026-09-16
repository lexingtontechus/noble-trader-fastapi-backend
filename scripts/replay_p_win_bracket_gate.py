#!/usr/bin/env python3
"""
replay_p_win_bracket_gate.py — Phase 3.4 go/no-go for Batch C.

Compares qualification under directional p_win (status quo) vs adding a
p_win_bracket gate on historical resolved signals. Offline fixture always
prints; --live pulls nt_signal_sim when Supabase env is set.

Acceptance (worklog Phase 3.4):
  * expired % of would-be accepted set ↓
  * expectancy_r of accepted set ↑ or flat with higher n
  * |bias| on accepted ↓
  * qualified count not collapsed to ~0

Usage:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/replay_p_win_bracket_gate.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from regime_platform.sweep_orchestrator.p_win_bracket import (
    beta_shrunk_win_rate,
    compute_bracket_probs,
    would_bracket_gate_reject,
    MIN_P_WIN_BRACKET,
)


def _fixture() -> list[dict]:
    """Resolved population: tight buckets (reachable) + wide buckets (expire)."""
    rows = []
    # Tight 0.5-1 ATR: mostly resolve, good win rate
    for i in range(40):
        rows.append({
            "symbol": "EURUSD", "entry": 1.1000, "tp": 1.1008, "atr": 0.0010,
            "regime": "med_low_vol_bull", "p_win_dir": 0.60,
            "outcome": "tp_hit" if i < 24 else ("sl_hit" if i < 32 else "expired"),
            "tp_bricks": 6, "sl_bricks": 3,
        })
    # Wide 4+ ATR: mostly expire
    for i in range(40):
        rows.append({
            "symbol": "XAUUSD", "entry": 2500.0, "tp": 2600.0, "atr": 10.0,
            "regime": "med_low_vol_bull", "p_win_dir": 0.70,
            "outcome": "tp_hit" if i < 4 else ("sl_hit" if i < 8 else "expired"),
            "tp_bricks": 20, "sl_bricks": 5,
        })
    return rows


def _bucket_stats(rows: list[dict]) -> dict:
    """Build empirical p_resolve / p_win|resolve per (symbol, bucket) from the
    same population (in-sample illustration — production uses the
    reachability table, not the eval set)."""
    from collections import defaultdict
    from regime_platform.sweep_orchestrator.bracket_reachability import _bucket_for, _vol_tier

    cells = defaultdict(lambda: {"n": 0, "tp": 0, "sl": 0, "exp": 0})
    for r in rows:
        bucket = _bucket_for(abs(r["tp"] - r["entry"]) / r["atr"])
        key = (r["symbol"], _vol_tier(r["regime"]), bucket)
        cells[key]["n"] += 1
        if r["outcome"] == "tp_hit":
            cells[key]["tp"] += 1
        elif r["outcome"] == "sl_hit":
            cells[key]["sl"] += 1
        else:
            cells[key]["exp"] += 1
    out = {}
    for key, c in cells.items():
        n = c["n"]
        p_resolve = (c["tp"] + c["sl"]) / n if n else 0.0
        p_win_res = beta_shrunk_win_rate(c["tp"], c["sl"])
        out[key] = (p_resolve * p_win_res, p_resolve, p_win_res, n)
    return out


def _stats(accepted: list[dict], label: str) -> None:
    n = len(accepted)
    if n == 0:
        print(f"{label}: n=0")
        return
    wins = sum(1 for r in accepted if r["outcome"] == "tp_hit")
    losses = sum(1 for r in accepted if r["outcome"] == "sl_hit")
    expired = sum(1 for r in accepted if r["outcome"] == "expired")
    resolved = wins + losses
    exp_r = None
    if resolved:
        wr = wins / resolved
        avg_rr = (
            sum(r["tp_bricks"] / r["sl_bricks"] for r in accepted if r["outcome"] == "tp_hit")
            / wins if wins else 0.0
        )
        exp_r = wr * avg_rr - (1 - wr)
    avg_dir = sum(r["p_win_dir"] for r in accepted) / n
    print(
        f"{label:<28} n={n:3d} win%res={100*wins/resolved if resolved else -1:5.1f} "
        f"exp%={100*expired/n:5.1f} expectancy_r={exp_r if exp_r is not None else float('nan'):+.3f} "
        f"avg_p_dir={avg_dir:.3f}"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--min-bracket", type=float, default=MIN_P_WIN_BRACKET)
    p.add_argument("--live", action="store_true")
    args = p.parse_args()

    rows = _fixture()
    stats = _bucket_stats(rows)

    base = [r for r in rows if r["p_win_dir"] >= 0.55]
    bracket = []
    for r in rows:
        if r["p_win_dir"] < 0.55:
            continue
        from regime_platform.sweep_orchestrator.bracket_reachability import _bucket_for, _vol_tier
        key = (r["symbol"], _vol_tier(r["regime"]), _bucket_for(abs(r["tp"] - r["entry"]) / r["atr"]))
        p_br = stats[key][0]
        if would_bracket_gate_reject(p_br, min_p_win_bracket=args.min_bracket):
            continue
        bracket.append(r)

    print("=== p_win_bracket gate replay (fixture) ===")
    print(f"min_p_win_bracket={args.min_bracket}")
    _stats(base, "baseline (p_dir only)")
    _stats(bracket, "bracket gate (p_dir + p_br)")
    print(
        "\nPass if: expired% ↓ and expectancy_r ↑ without n collapsing "
        "to ~0. Fixture is illustrative — attach live table before flip."
    )

    if args.live:
        import asyncio
        import httpx
        from regime_platform.core.settings import settings

        if not settings.SUPABASE_URL:
            print("No SUPABASE_URL — skip live.")
            return 0

        async def fetch():
            headers = {
                "apikey": settings.SUPABASE_KEY,
                "Authorization": f"Bearer {settings.SUPABASE_KEY}",
            }
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.get(
                    f"{settings.SUPABASE_URL.rstrip('/')}/rest/v1/nt_signal_sim",
                    headers=headers,
                    params={
                        "select": "symbol,p_win,p_win_bracket,p_win_dir,outcome,tp_bricks,sl_bricks,bracket_gate_shadow,qualified",
                        "outcome": "not.is.null",
                        "p_win": "not.is.null",
                        "limit": "2000",
                        "order": "ts.desc",
                    },
                )
                r.raise_for_status()
                return r.json() if isinstance(r.json(), list) else []

        live = asyncio.run(fetch())
        print(f"\n=== Live: {len(live)} resolved rows with p_win ===")
        # Historical rows mostly lack p_win_bracket until capture runs
        with_br = [r for r in live if r.get("p_win_bracket") is not None]
        shadow_hits = [r for r in live if r.get("bracket_gate_shadow")]
        print(f"rows with p_win_bracket: {len(with_br)}")
        print(f"bracket_gate_shadow=true: {len(shadow_hits)}")
        if with_br:
            _stats(
                [
                    {
                        "outcome": r["outcome"],
                        "p_win_dir": r.get("p_win_dir") or r.get("p_win") or 0.5,
                        "tp_bricks": r.get("tp_bricks") or 1,
                        "sl_bricks": r.get("sl_bricks") or 1,
                    }
                    for r in with_br
                    if not would_bracket_gate_reject(r["p_win_bracket"], min_p_win_bracket=args.min_bracket)
                ],
                "live accepted by bracket",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
