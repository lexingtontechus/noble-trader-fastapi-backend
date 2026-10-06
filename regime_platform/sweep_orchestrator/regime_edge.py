"""
Regime Edge — compute (symbol, signal, regime) edge from resolved signals.
═══════════════════════════════════════════════════════════════════════

Builds `nt_regime_edge`: one row per (symbol, signal, regime) with win rate
and expectancy. The edge is direction-specific and regime-specific — AAPL's
buy edge is different from its sell edge, and both are regime-conditional.

The HMM regime label (e.g. `high_vol_bull`, `med_low_vol_bear`) is a stronger
signal than the macro label (which is a constant for AAPL). This module uses
the regime directly, without the macro filter's horizon mismatch.

Fail-open: buckets with n < min_n are not written (no evidence either way).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("regime.sweep.regime_edge")


# ─── Config ────────────────────────────────────────────────────────────


def get_regime_edge_mode() -> str:
    """Regime edge mode: off | shadow | live (default shadow)."""
    from regime_platform.core.feature_flags import get_regime_edge_mode as _get
    return _get()


def get_regime_edge_config() -> dict[str, Any]:
    """Regime edge configuration."""
    from regime_platform.core.feature_flags import get_regime_edge_config as _get
    return _get()


# ─── Pure computation (no I/O — unit-testable) ─────────────────────────


def compute_regime_edge(
    rows: list[dict[str, Any]],
    *,
    min_n: int = 20,
) -> list[dict[str, Any]]:
    """Compute (symbol, signal, regime) edge from resolved signal rows.

    ``rows`` are dicts with keys: symbol, signal, outcome, tp_bricks, sl_bricks.
    Only win/loss-resolved rows are used (outcome in tp_hit/sl_hit).
    Buckets with n < min_n are omitted (fail-open — no evidence).

    Returns a list of dicts ready for upsert into nt_regime_edge.
    """
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}

    for r in rows:
        outcome = r.get("outcome")
        if outcome not in ("tp_hit", "sl_hit"):
            continue

        symbol = r.get("symbol")
        signal = r.get("signal")
        regime = r.get("regime")
        if not symbol or not signal or not regime:
            continue

        key = (str(symbol), str(signal), str(regime))
        if key not in buckets:
            buckets[key] = {
                "symbol": str(symbol),
                "signal": str(signal),
                "regime": str(regime),
                "n": 0,
                "wins": 0,
                "losses": 0,
                "rr_sum": 0.0,
            }

        b = buckets[key]
        b["n"] += 1
        if outcome == "tp_hit":
            b["wins"] += 1
            # RR = tp_bricks / sl_bricks (for expectancy)
            tp = r.get("tp_bricks")
            sl = r.get("sl_bricks")
            if tp and sl and sl > 0:
                b["rr_sum"] += float(tp) / float(sl)
        else:
            b["losses"] += 1

    out: list[dict[str, Any]] = []
    for key, b in buckets.items():
        if b["n"] < min_n:
            continue

        wins = b["wins"]
        losses = b["losses"]
        n = b["n"]
        win_rate = wins / n if n > 0 else 0.0
        avg_rr = b["rr_sum"] / wins if wins > 0 else 1.0
        expectancy_r = win_rate * avg_rr - (1 - win_rate) * 1.0

        out.append({
            "symbol": b["symbol"],
            "signal": b["signal"],
            "regime": b["regime"],
            "n": n,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "expectancy_r": round(expectancy_r, 4),
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })

    return out


# ─── I/O: fetch + upsert ────────────────────────────────────────────────


async def _fetch_resolved_rows(
    symbols: list[str] | None = None,
    *,
    days: int = 90,
) -> list[dict[str, Any]]:
    """Fetch win/loss-resolved nt_signal_sim rows."""
    import httpx
    from regime_platform.sweep_orchestrator.supabase_client import (
        _headers,
        _url,
    )

    params: dict[str, Any] = {
        "select": "symbol,signal,outcome,regime,tp_bricks,sl_bricks",
        "outcome": "in.(tp_hit,sl_hit)",
        "order": "ts.desc",
        "limit": "1000",
    }
    if symbols:
        params["symbol"] = f"in.({','.join(symbols)})"

    rows: list[dict[str, Any]] = []
    offset = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                _url("nt_signal_sim"),
                headers=_headers(),
                params={**params, "offset": str(offset)},
            )
            resp.raise_for_status()
            batch = resp.json() or []
            rows += batch
            offset += len(batch)
            if len(batch) < 1000:
                break

    return rows


async def _upsert_edge_table(rows: list[dict[str, Any]]) -> None:
    """Upsert edge rows into nt_regime_edge."""
    if not rows:
        return

    import httpx
    from regime_platform.sweep_orchestrator.supabase_client import (
        _headers,
        _url,
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            _url("nt_regime_edge"),
            headers={**_headers(), "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
            json=rows,
        )
        if resp.status_code >= 400:
            logger.warning("regime_edge: upsert failed: %s %s", resp.status_code, resp.text[:200])


async def rebuild_regime_edge(
    symbols: list[str] | None = None,
    *,
    days: int = 90,
    min_n: int = 20,
) -> dict[str, Any]:
    """Rebuild the nt_regime_edge table from resolved signals.

    Returns {symbols, rows_fetched, buckets_written, min_n}.
    """
    rows = await _fetch_resolved_rows(symbols, days=days)
    edge = compute_regime_edge(rows, min_n=min_n)
    await _upsert_edge_table(edge)

    logger.info(
        "regime_edge: rebuilt %d buckets from %d rows (min_n=%d)",
        len(edge), len(rows), min_n,
    )

    return {
        "symbols": symbols or "all",
        "rows_fetched": len(rows),
        "buckets_written": len(edge),
        "min_n": min_n,
    }


# ─── Gate lookup ────────────────────────────────────────────────────────


async def get_regime_edge(
    symbol: str,
    signal: str,
    regime: str,
) -> dict[str, Any] | None:
    """Look up the edge for one (symbol, signal, regime) bucket.

    Returns None if no bucket exists (fail-open — no evidence).
    """
    import httpx
    from regime_platform.sweep_orchestrator.supabase_client import (
        _headers,
        _url,
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_regime_edge"),
                headers=_headers(),
                params={
                    "select": "symbol,signal,regime,n,win_rate,expectancy_r",
                    "symbol": f"eq.{symbol}",
                    "signal": f"eq.{signal}",
                    "regime": f"eq.{regime}",
                    "limit": "1",
                },
            )
        rows = resp.json() or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.debug("regime_edge: lookup failed for %s/%s/%s: %s", symbol, signal, regime, exc)
        return None


def should_reject_by_regime_edge(
    edge: dict[str, Any] | None,
    *,
    min_n: int = 20,
    min_win_rate: float = 0.50,
) -> tuple[bool, str | None]:
    """Decide whether to reject a signal based on the regime edge.

    Returns (reject, reason). Fail-open: if edge is None or n < min_n,
    do not reject (no evidence either way).
    """
    if edge is None:
        return False, None

    n = int(edge.get("n", 0))
    if n < min_n:
        return False, None

    wr = float(edge.get("win_rate", 0.0))
    if wr < min_win_rate:
        reason = (
            f"regime_edge: {edge.get('symbol')} {edge.get('signal')} "
            f"{edge.get('regime')} WR={wr:.1%} < {min_win_rate:.1%} (n={n})"
        )
        return True, reason

    return False, None
