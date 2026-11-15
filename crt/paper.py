"""paper.py — the CRT paper book sibling (CRT-1, source='crt_cont').

A thin writer/resolver for ``nt_paper_positions`` following the paper_book
conventions (column whitelist, PostgREST via ``supabase_client._insert_row``,
best-effort/fail-open everywhere) WITHOUT touching ``paper_book.py`` or
bending ``nt_signal_sim`` (the Task 67 assessment §3.4 caveat: the shared
book's open/close passes join on nt_signal_sim; CRT owns its rows end to
end instead).

Row contract:

- ``source='crt_cont'`` (the CRT-1 identifier; TEXT, unconstrained);
- ``signal_id`` = self-minted UUID (no nt_signal_sim sibling by design);
- ``venue='crt:{tf}:{SESSION}'`` — the recoverable option key: TF + session
  from the row alone (trade direction + symbol give the setup direction;
  the option lookup gives hold/R). Zero-migration hydration;
- ``open_ts`` = the C3 bar's CLOSE time (the first moment the fill is
  knowable on bar data; the entry bar is the bar CLOSING at open_ts);
- ``take_profit``/``stop_loss`` = the option's FIXED barriers (frozen at
  C2 close — the R target never re-picks live);
- resolution PATCH: TP/SL -> status 'closed', time exit -> 'expired'
  (paper_book's outcome mapping), exit_price/close_ts/realized_pnl/
  r_multiple/resolved_at set; ``realized_pnl = r_multiple x cash_risk``
  (the label's R already carries the cost model; no management overlays —
  CRT-1 ports no break-even/trailing: neither the research labels nor the
  EA's own paper monitors include them).

All DB access is lazy-imported from the platform and fails OPEN: an
unconfigured Supabase or a transient error logs and returns False/[] — the
engine keeps running, nothing raises out.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("crt.paper")

PAPER_TABLE = "nt_paper_positions"
SOURCE = "crt_cont"

PAPER_COLUMNS = (
    "position_id", "signal_id", "symbol", "venue", "direction",
    "entry_price", "stop_loss", "take_profit", "size_units", "size_notional",
    "open_ts", "status", "exit_price", "realized_pnl", "r_multiple",
    "close_ts", "resolved_at", "source",
)

SELECT_OPEN = ("position_id,signal_id,symbol,venue,direction,entry_price,"
               "stop_loss,take_profit,size_units,size_notional,open_ts")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build_open_row(*, symbol: str, tf: str, session_id: int, trade_buy: bool,
                   entry_price: float, stop_loss: float, take_profit: float,
                   cash_risk: float, open_ts_epoch: float,
                   risk_dist: float) -> dict[str, Any]:
    """One nt_paper_positions INSERT row for a gated, filled CRT setup.

    ``risk_dist`` = |entry_ref - sl| (the EA sizes on the stop-entry REFERENCE
    level — ``CalcLots(g_planEntry=g.stopEntry, g_planSL)`` — not the slipped
    fill; ``entry_price`` on the row remains the honest slipped fill).
    """
    size_units = (cash_risk / risk_dist) if risk_dist > 0 else 0.0
    return {
        "position_id": str(uuid.uuid4()),
        "signal_id": str(uuid.uuid4()),
        "symbol": symbol,
        "venue": f"crt:{tf}:{({0: 'OUT', 1: 'NY', 2: 'ASIA'}).get(session_id, 'OUT')}",
        "direction": "buy" if trade_buy else "sell",
        "entry_price": float(entry_price),
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "size_units": float(size_units),
        "size_notional": float(size_units * entry_price),
        "open_ts": _iso(open_ts_epoch),
        "status": "open",
        "source": SOURCE,
    }


def build_close_patch(*, outcome: int, r: float, exit_price: float | None,
                      cash_risk: float, close_ts_epoch: float) -> dict[str, Any]:
    """The resolution PATCH (EA outcome codes: 1 TP / -1 SL / 0 TIME)."""
    expired = outcome == 0     # TIME -> 'expired' (paper_book mapping)
    return {
        "status": "expired" if expired else "closed",
        "exit_price": None if exit_price is None else float(exit_price),
        "close_ts": _iso(close_ts_epoch),
        "resolved_at": _iso(close_ts_epoch),
        "realized_pnl": round(float(r) * float(cash_risk), 6),
        "r_multiple": round(float(r), 4),
    }


async def open_position(row: dict[str, Any]) -> bool:
    """INSERT one CRT paper position. Best-effort: True on success."""
    payload = {k: v for k, v in row.items() if k in PAPER_COLUMNS}
    try:
        from regime_platform.sweep_orchestrator.supabase_client import _insert_row
        return await _insert_row(PAPER_TABLE, payload)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("crt.paper: insert failed: %s", exc)
        return False


async def patch_position(position_id: Any, patch: dict[str, Any]) -> bool:
    """PATCH one CRT paper position by position_id (paper_book pattern)."""
    try:
        from regime_platform.core.settings import settings
        if not settings.SUPABASE_URL:
            return False
        import httpx
        url = f"{settings.SUPABASE_URL}/rest/v1/{PAPER_TABLE}"
        headers = {
            "apikey": settings.SUPABASE_SECRET_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SECRET_KEY}",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(
                url, headers=headers,
                params={"position_id": f"eq.{position_id}"},
                json=patch,
            )
            if not resp.is_success:
                logger.error("crt.paper: PATCH %s HTTP %d — %s",
                             position_id, resp.status_code, resp.text[:200])
                return False
            return True
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.error("crt.paper: PATCH %s failed: %s", position_id, exc)
        return False


async def fetch_open_positions() -> list[dict[str, Any]]:
    """All OPEN CRT paper positions (any symbol — the caps are global)."""
    try:
        from regime_platform.sweep_orchestrator.supabase_client import fetch_all_rows
        return await fetch_all_rows(
            PAPER_TABLE, SELECT_OPEN,
            filters={"source": f"eq.{SOURCE}", "status": "eq.open"},
            order="open_ts.asc",
        )
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("crt.paper: fetch open failed: %s", exc)
        return []


async def fetch_day_pnl(now_epoch: float) -> float:
    """Sum of realized_pnl for CRT positions closed since UTC day start.

    The EA's daily-loss stop compares account equity vs day-start balance
    (closed + floating); the paper approximation is realized-only — open
    risk is already counted by the open-risk cap. Documented divergence.
    """
    day0 = datetime.fromtimestamp(float(now_epoch), tz=timezone.utc)
    day0 = day0.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        from regime_platform.sweep_orchestrator.supabase_client import fetch_all_rows
        rows = await fetch_all_rows(
            PAPER_TABLE, "realized_pnl",
            filters={"source": f"eq.{SOURCE}", "close_ts": f"gte.{_iso(day0.timestamp())}"},
            max_rows=1000,
        )
        return float(sum(float(r.get("realized_pnl") or 0.0) for r in rows))
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("crt.paper: fetch day pnl failed: %s", exc)
        return 0.0


def risk_state_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The shared caps snapshot from the open-position rows."""
    open_risk = 0.0
    symbols: set[str] = set()
    for r in rows or []:
        try:
            entry = float(r.get("entry_price") or 0.0)
            sl = float(r.get("stop_loss") or 0.0)
            units = float(r.get("size_units") or 0.0)
            if entry and sl and units:
                open_risk += abs(entry - sl) * units
            if r.get("symbol"):
                symbols.add(str(r.get("symbol")))
        except (TypeError, ValueError):
            continue
    return {"open_risk_cash": open_risk, "open_symbols": symbols}


async def refresh_risk_state(now_epoch: float) -> dict[str, Any]:
    """Open rows + day PnL -> the caps snapshot (fail-open zeroes)."""
    rows, day_pnl = await asyncio.gather(fetch_open_positions(), fetch_day_pnl(now_epoch))
    state = risk_state_from_rows(rows)
    state["day_pnl"] = day_pnl
    return state
