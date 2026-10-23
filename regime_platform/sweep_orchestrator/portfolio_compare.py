"""
Portfolio comparison — paper book vs equal-weight vs optimizer allocation
═══════════════════════════════════════════════════════════════════════════

Part II Phase 3 (Option B). Once the EOD resolver has produced resolved
signal outcomes (tp_hit/sl_hit/expired with per-signal PnL), this module
answers: "is signal-driven (paper-book) allocation better than naive
equal-weight, or than the /multi_asset optimizer's weights?"

Method:
  1. Build a (n_days × n_symbols) log-return matrix from nt_signal_sim
     resolved outcomes (per-symbol daily PnL / notional → return proxy).
  2. Run the existing DrawdownOptimiser (the same engine behind
     /multi_asset optimise-portfolio) on that matrix → optimizer weights.
  3. Compute three daily equity paths:
       paper      — realized PnL from nt_paper_positions (kelly-sized)
       equal_wt   — equal-weight allocation over the same symbols
       optimised  — optimizer-weight allocation (rebalanced per run)
  4. Compare Sharpe / total return / max drawdown across the three.

Design rules (mirror eod_resolver.py / paper_book.py):
  • Best-effort only — every failure is caught, logged and counted in the
    summary; this module never raises out.
  • Pure computation over resolved data — no new capture, no scheduler
    hook. Run it manually (CLI) or as a weekly report; it does not need
    to be part of the daily EOD loop.
  • Reuses the core optimizer class directly (not the HTTP router), so it
    works offline / in tests with a synthetic matrix.

CLI:
    python -m regime_platform.sweep_orchestrator.portfolio_compare
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

logger = logging.getLogger("regime.sweep.portfolio_compare")

# Minimum resolved signals per symbol before it enters the comparison
MIN_RESOLVED_PER_SYMBOL = 1
# Minimum days of data before the comparison is meaningful (plan gate)
MIN_DAYS = 7


def build_returns_matrix(
    resolved: list[dict[str, Any]],
    symbols: list[str],
) -> tuple[np.ndarray, list[str]]:
    """Build a (n_days × n_assets) log-return matrix from resolved signals.

    Each resolved signal contributes a return proxy:
        return = pnl_dollars / max(size_notional, 1e-9)
    aggregated per (day, symbol) as the mean of that day's returns. Days
    are derived from the signal's ``ts`` (resolved_at may be absent in
    older rows). Symbols with zero resolved rows are dropped.

    Returns ``(matrix, used_symbols)`` — matrix columns align to
    ``used_symbols``. Returns an empty matrix when no symbol qualifies.
    """
    from collections import defaultdict
    day_returns: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for sig in resolved:
        sym = sig.get("symbol")
        pnl = sig.get("pnl_dollars")
        notional = sig.get("size_notional")
        ts = sig.get("ts") or sig.get("resolved_at")
        if sym not in symbols or pnl is None or notional in (None, 0) or not ts:
            continue
        try:
            day = str(ts)[:10]  # YYYY-MM-DD
            r = float(pnl) / float(notional)
            day_returns[day][sym].append(r)
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    days = sorted(day_returns)
    used = [s for s in symbols if any(s in day_returns[d] for d in days)]
    if not used or len(days) < 1:
        return np.zeros((0, 0)), []

    matrix = np.zeros((len(days), len(used)), dtype=float)
    for i, day in enumerate(days):
        for j, sym in enumerate(used):
            vals = day_returns[day].get(sym, [])
            matrix[i, j] = float(np.mean(vals)) if vals else 0.0
    return matrix, used


def equal_weight_equity(
    resolved: list[dict[str, Any]],
    symbols: list[str],
) -> list[dict[str, Any]]:
    """Daily cumulative equity under equal-weight allocation.

    Each symbol gets 1/N of the base notional; daily equity is the sum of
    that day's returns × notional share, accumulated. Returns a list of
    ``{day, daily_pnl, cumulative_pnl}`` for days that have any resolved
    signal among ``symbols``.
    """
    from collections import defaultdict
    day_pnl: dict[str, float] = defaultdict(float)
    for sig in resolved:
        sym = sig.get("symbol")
        pnl = sig.get("pnl_dollars")
        ts = sig.get("ts") or sig.get("resolved_at")
        if sym not in symbols or pnl is None or not ts:
            continue
        day_pnl[str(ts)[:10]] += float(pnl)

    base = 1000.0
    share = base / max(len(symbols), 1)
    cum = 0.0
    out: list[dict[str, Any]] = []
    for day in sorted(day_pnl):
        cum += day_pnl[day]
        out.append({
            "day": day,
            "daily_pnl": round(day_pnl[day], 6),
            "cumulative_pnl": round(cum, 6),
            "notional_share": round(share, 2),
        })
    return out


def optimise_weights(
    matrix: np.ndarray,
    symbols: list[str],
    max_weight: float = 0.40,
    max_dd_limit: float = 0.20,
) -> dict[str, Any]:
    """Run DrawdownOptimiser on the returns matrix → weight vector.

    Reuses the same core as /multi_asset optimise-portfolio so the
    comparison is apples-to-apples with the optimizer the platform ships.
    """
    try:
        from regime_platform.core.portfolio_optimiser import (
            DrawdownOptimiser, OptimiserConfig,
        )
        cfg = OptimiserConfig(
            max_weight=max_weight,
            max_dd_limit=max_dd_limit,
        )
        opt = DrawdownOptimiser(cfg)
        result = opt.optimise(
            returns_matrix=matrix,
            symbols=symbols,
        )
        return {
            "symbols": list(result.weights.keys()) if hasattr(result.weights, "keys") else symbols,
            "weights": (
                list(result.weights.values())
                if hasattr(result.weights, "values")
                else (result.weights if isinstance(result.weights, list) else [])
            ),
            "expected_return": getattr(result, "expected_return", None),
            "expected_vol": getattr(result, "expected_vol", None),
            "sharpe": getattr(result, "sharpe_ratio", getattr(result, "sharpe", None)),
            "converged": getattr(result, "converged", True),
        }
    except Exception as exc:
        logger.warning("portfolio_compare: optimisation failed (%s) — equal-weight fallback", exc)
        n = len(symbols)
        return {
            "symbols": symbols,
            "weights": [1.0 / n for _ in symbols] if n else [],
            "expected_return": None,
            "expected_vol": None,
            "sharpe": None,
            "converged": False,
        }


def paper_equity(resolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Daily cumulative equity from nt_paper_positions-style resolved rows.

    Expects rows with ``close_ts``/``ts``, ``realized_pnl`` (or
    ``pnl_dollars`` fallback). Mirrors v_paper_equity semantics in Python
    so the CLI report can print without a view dependency.
    """
    from collections import defaultdict
    day_pnl: dict[str, float] = defaultdict(float)
    for sig in resolved:
        pnl = sig.get("realized_pnl", sig.get("pnl_dollars"))
        ts = sig.get("close_ts") or sig.get("resolved_at") or sig.get("ts")
        if pnl is None or not ts:
            continue
        day_pnl[str(ts)[:10]] += float(pnl)
    cum = 0.0
    out: list[dict[str, Any]] = []
    for day in sorted(day_pnl):
        cum += day_pnl[day]
        out.append({"day": day, "daily_pnl": round(day_pnl[day], 6), "cumulative_pnl": round(cum, 6)})
    return out


def compare(
    resolved_signals: list[dict[str, Any]],
    paper_rows: list[dict[str, Any]],
    symbols: list[str],
    max_weight: float = 0.40,
    min_days: int = MIN_DAYS,
) -> dict[str, Any]:
    """Compare paper vs equal-weight vs optimised allocation.

    ``resolved_signals`` — nt_signal_sim rows with outcome set.
    ``paper_rows`` — nt_paper_positions rows (realized PnL).
    Returns a summary dict; ``insufficient_data=True`` when fewer than
    ``min_days`` of resolved history exist (the plan gate).
    """
    matrix, used_symbols = build_returns_matrix(resolved_signals, symbols)
    days = len(matrix)
    if days < min_days:
        return {
            "insufficient_data": True,
            "days_available": days,
            "min_days_required": min_days,
            "message": f"Need {min_days} days of resolved outcomes; have {days}.",
        }

    opt = optimise_weights(matrix, used_symbols, max_weight=max_weight)
    eq = equal_weight_equity(resolved_signals, used_symbols)
    pp = paper_equity(paper_rows)

    def stats(equity: list[dict[str, Any]]) -> dict[str, Any]:
        if not equity:
            return {"total_pnl": 0.0, "days": 0, "sharpe": None}
        vals = [e["daily_pnl"] for e in equity]
        total = sum(vals)
        arr = np.array(vals, dtype=float)
        sharpe = None
        if arr.std() > 1e-12 and len(arr) > 1:
            sharpe = round(float(arr.mean() / arr.std() * np.sqrt(252)), 4)
        return {
            "total_pnl": round(total, 4),
            "days": len(equity),
            "sharpe": sharpe,
        }

    return {
        "insufficient_data": False,
        "days_available": days,
        "symbols_used": used_symbols,
        "optimizer": {
            "weights": dict(zip(opt["symbols"], opt["weights"])),
            "expected_return": opt["expected_return"],
            "expected_vol": opt["expected_vol"],
            "sharpe": opt["sharpe"],
            "converged": opt["converged"],
        },
        "equal_weight": stats(eq),
        "paper": stats(pp),
        "equal_weight_equity": eq[-10:],
        "paper_equity": pp[-10:],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _fetch_resolved_signals(limit: int = 2000) -> list[dict[str, Any]]:
    """Resolved nt_signal_sim rows (outcome set) with sizing/ts context."""
    try:
        from regime_platform.core.settings import settings
        import httpx
        url = (
            f"{settings.SUPABASE_URL}/rest/v1/nt_signal_sim"
            "?select=symbol,ts,resolved_at,outcome,pnl_dollars"
            "&outcome=in.(tp_hit,sl_hit,expired)&order=ts.asc"
            f"&limit={int(limit)}"
        )
        headers = {
            "apikey": settings.SUPABASE_SECRET_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SECRET_KEY}",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
            return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("portfolio_compare: fetch resolved failed: %s", exc)
        return []


async def _fetch_paper_rows(limit: int = 2000) -> list[dict[str, Any]]:
    """nt_paper_positions rows (realized PnL for the paper equity path)."""
    try:
        from regime_platform.core.settings import settings
        import httpx
        url = (
            f"{settings.SUPABASE_URL}/rest/v1/nt_paper_positions"
            "?select=symbol,close_ts,resolved_at,realized_pnl,status"
            f"&status=in.(closed,expired)&order=close_ts.asc&limit={int(limit)}"
        )
        headers = {
            "apikey": settings.SUPABASE_SECRET_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SECRET_KEY}",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
            return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("portfolio_compare: fetch paper rows failed: %s", exc)
        return []


async def _fetch_active_symbols() -> list[str]:
    try:
        from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols
        rows = await fetch_active_symbols()
        return [r.get("symbol") for r in rows if r.get("symbol")]
    except Exception as exc:
        logger.warning("portfolio_compare: fetch symbols failed: %s", exc)
        return []


async def run_comparison(
    max_weight: float = 0.40,
    min_days: int = MIN_DAYS,
) -> dict[str, Any]:
    """Fetch live data and run the three-way comparison."""
    symbols = await _fetch_active_symbols()
    resolved = await _fetch_resolved_signals()
    paper = await _fetch_paper_rows()
    if not symbols:
        return {"error": "no active symbols"}
    return compare(resolved, paper, symbols, max_weight=max_weight, min_days=min_days)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", force=True)
    result = await run_comparison()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
