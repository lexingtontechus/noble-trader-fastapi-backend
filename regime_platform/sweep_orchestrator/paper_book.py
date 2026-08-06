"""
Paper portfolio — kelly-sized paper book over the qualified signal stream
═══════════════════════════════════════════════════════════════════════════

Part II Phase 2 (Option A). For every QUALIFIED signal captured in
``nt_signal_sim``, open a paper position sized with the kelly rule, and
close it when the EOD resolver writes the signal outcome.

Sizing rule (locked 2026-08-06, per user decision "as recommended"):
    size_notional = min(base_notional × kelly_f × ev_scale, max_notional)
    size_units    = size_notional / entry_price
where
    base_notional = env NOBLE_PAPER_BASE_NOTIONAL (default 1000)
    max_notional  = 20% of base_notional (hard cap per position)
    kelly_f / effective_kelly come from the nt_signal_sim capture (migration
    105 adds the columns; the capture payload carries effective_kelly which
    already includes the EV scale — see orchestrator._build_signal_sim_payload).

PnL convention (locked):
    realized_pnl = pnl_bricks × brick_size × size_units
i.e. the signal's brick PnL in price units, scaled by the position size in
units. A 6-brick TP at brick_size 0.001 on a $1000 notional position at
entry 0.6900 gives 6 × 0.001 × (1000×0.20/0.6900) ≈ $1.74.

r_multiple = pnl_bricks / sl_bricks  (tp_hit → +tp/sl, sl_hit → -1, expired → 0)

Design rules (mirror eod_resolver.py):
  • Best-effort only — every per-row failure is caught, logged and counted
    in the summary; this module never raises out.
  • ``reconcile()`` is the idempotent entry point: it opens positions for
    qualified signals that have none, and closes positions whose signal was
    resolved. Safe to run manually or from the daily eod-lookback job.

CLI:
    python -m regime_platform.sweep_orchestrator.paper_book
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("regime.sweep.paper_book")

# Sizing config (env-tunable)
BASE_NOTIONAL = float(os.environ.get("NOBLE_PAPER_BASE_NOTIONAL", "1000"))
MAX_FRACTION = 0.20  # hard cap: max 20% of base notional per position

PAPER_POSITION_COLUMNS: frozenset[str] = frozenset({
    "position_id", "signal_id", "symbol", "venue", "direction",
    "entry_price", "stop_loss", "take_profit",
    "size_units", "size_notional",
    "open_ts", "close_ts", "status",
    "exit_price", "realized_pnl", "pnl_bricks", "r_multiple",
    "source", "resolved_at",
})


def compute_size(
    entry_price: float,
    kelly_f: Optional[float] = None,
    effective_kelly: Optional[float] = None,
    base_notional: float = BASE_NOTIONAL,
    max_fraction: float = MAX_FRACTION,
) -> dict[str, float]:
    """Kelly sizing for one paper position.

    Uses ``effective_kelly`` (kelly × EV scale — already blended at capture)
    when present, else raw ``kelly_f``. Falls back to a flat 5% of base when
    neither is available, so a missing sizing input never zeroes the book.

    Returns ``{size_notional, size_units}`` (units = notional / entry).
    """
    kelly = float(effective_kelly) if effective_kelly is not None else (
        float(kelly_f) if kelly_f is not None else 0.05
    )
    kelly = max(0.0, min(1.0, kelly))
    max_notional = base_notional * max_fraction
    size_notional = min(base_notional * kelly, max_notional)
    size_units = size_notional / float(entry_price) if entry_price else 0.0
    return {
        "size_notional": round(size_notional, 6),
        "size_units": round(size_units, 6),
    }


def compute_realized_pnl(
    outcome: str,
    pnl_bricks: float,
    brick_size: float,
    size_units: float,
    sl_bricks: float,
) -> dict[str, float]:
    """$ PnL for a resolved paper position.

    realized_pnl = pnl_bricks × brick_size × size_units
    r_multiple   = pnl_bricks / sl_bricks (tp → +tp/sl, sl → -1, expired → 0)
    """
    pb = float(pnl_bricks or 0.0)
    realized = pb * float(brick_size or 0.0) * float(size_units or 0.0)
    sl = float(sl_bricks or 1.0)
    r_mult = pb / sl if sl else 0.0
    if outcome == "expired":
        r_mult = 0.0
    return {
        "realized_pnl": round(realized, 6),
        "r_multiple": round(r_mult, 4),
    }


def _filter_payload(payload: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k in allowed}


async def _insert_position(row: dict[str, Any]) -> bool:
    """Insert one nt_paper_positions row. True on success."""
    try:
        from regime_platform.sweep_orchestrator.supabase_client import _insert_row
        return await _insert_row("nt_paper_positions", row)
    except Exception as exc:
        logger.warning("paper_book: insert failed: %s", exc)
        return False


async def _patch_position(position_id: Any, patch: dict[str, Any]) -> bool:
    """PATCH one nt_paper_positions row by position_id. True on success."""
    try:
        from regime_platform.core.settings import settings
        import httpx
        url = f"{settings.SUPABASE_URL}/rest/v1/nt_paper_positions"
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(
                url, headers=headers,
                params={"position_id": f"eq.{position_id}"},
                json=patch,
            )
            if not resp.is_success:
                logger.error(
                    "paper_book: PATCH position %s HTTP %d — %s",
                    position_id, resp.status_code, resp.text[:200],
                )
                return False
            return True
    except Exception as exc:
        logger.error("paper_book: PATCH position %s failed: %s", position_id, exc)
        return False


async def _fetch_qualified_without_position(limit: int = 200) -> list[dict[str, Any]]:
    """Qualified nt_signal_sim rows that have no paper position yet."""
    try:
        from regime_platform.core.settings import settings
        import httpx
        url = (
            f"{settings.SUPABASE_URL}/rest/v1/nt_signal_sim"
            "?select=signal_id,symbol,venue,signal,entry_price,stop_loss,"
            "take_profit,kelly_f,effective_kelly,ts,source,qualified"
            "&qualified=eq.true&outcome=is.null&order=ts.asc"
            f"&limit={int(limit)}"
        )
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
            return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("paper_book: fetch qualified failed: %s", exc)
        return []


async def _fetch_open_positions() -> list[dict[str, Any]]:
    """Open nt_paper_positions rows (for close pass)."""
    try:
        from regime_platform.core.settings import settings
        import httpx
        url = (
            f"{settings.SUPABASE_URL}/rest/v1/nt_paper_positions"
            "?select=position_id,signal_id,status"
            "&status=eq.open&limit=1000"
        )
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
            return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("paper_book: fetch open positions failed: %s", exc)
        return []


async def _fetch_resolved_signals(limit: int = 500) -> list[dict[str, Any]]:
    """Resolved nt_signal_sim rows (outcome set) for the close pass."""
    try:
        from regime_platform.core.settings import settings
        import httpx
        url = (
            f"{settings.SUPABASE_URL}/rest/v1/nt_signal_sim"
            "?select=signal_id,symbol,outcome,exit_price,hold_bars,"
            "pnl_bricks,pnl_dollars,brick_size,sl_bricks,resolved_at"
            "&outcome=in.(tp_hit,sl_hit,expired)&order=ts.asc"
            f"&limit={int(limit)}"
        )
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
            return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("paper_book: fetch resolved failed: %s", exc)
        return []


async def open_position(sig: dict[str, Any]) -> bool:
    """Open a paper position for one qualified signal (best-effort)."""
    entry = sig.get("entry_price")
    if not entry:
        return False
    sizing = compute_size(
        float(entry),
        kelly_f=sig.get("kelly_f"),
        effective_kelly=sig.get("effective_kelly"),
    )
    row = _filter_payload({
        "position_id": str(uuid.uuid4()),
        "signal_id": sig.get("signal_id"),
        "symbol": sig.get("symbol"),
        "venue": sig.get("venue"),
        "direction": sig.get("signal") or "buy",
        "entry_price": float(entry),
        "stop_loss": sig.get("stop_loss"),
        "take_profit": sig.get("take_profit"),
        "size_units": sizing["size_units"],
        "size_notional": sizing["size_notional"],
        "open_ts": sig.get("ts") or datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "source": sig.get("source"),
    }, PAPER_POSITION_COLUMNS)
    ok = await _insert_position(row)
    if ok:
        logger.info(
            "paper_book: OPEN %s %s notional=%.2f units=%.4f",
            sig.get("symbol"), sig.get("signal"),
            sizing["size_notional"], sizing["size_units"],
        )
    return ok


async def close_position(sig: dict[str, Any], position_id: Any, size_units: float = 0.0) -> bool:
    """Close one paper position from a resolved signal (best-effort)."""
    outcome = sig.get("outcome")
    if outcome not in ("tp_hit", "sl_hit", "expired"):
        return False
    pnl = compute_realized_pnl(
        outcome,
        sig.get("pnl_bricks") or 0.0,
        sig.get("brick_size") or 0.0,
        size_units,
        sig.get("sl_bricks") or 1.0,
    )
    patch = {
        "status": "closed" if outcome in ("tp_hit", "sl_hit") else "expired",
        "close_ts": sig.get("resolved_at") or datetime.now(timezone.utc).isoformat(),
        "exit_price": sig.get("exit_price"),
        "realized_pnl": pnl["realized_pnl"],
        "pnl_bricks": sig.get("pnl_bricks"),
        "r_multiple": pnl["r_multiple"],
        "resolved_at": sig.get("resolved_at") or datetime.now(timezone.utc).isoformat(),
    }
    ok = await _patch_position(position_id, patch)
    if ok:
        logger.info(
            "paper_book: CLOSE %s %s pnl=%.4f",
            sig.get("symbol"), outcome, pnl["realized_pnl"],
        )
    return ok


async def reconcile(limit: int = 200) -> dict[str, Any]:
    """Idempotent open+close pass over the paper book.

    1. Open: qualified signals (outcome IS NULL) without a position.
    2. Close: resolved signals (outcome set) whose position is still open.
    """
    summary: dict[str, Any] = {"opened": 0, "closed": 0, "errors": 0}

    # ── Open pass ────────────────────────────────────────────────────
    try:
        candidates = await _fetch_qualified_without_position(limit)
        open_positions = await _fetch_open_positions()
        open_signal_ids = {p.get("signal_id") for p in open_positions}
        for sig in candidates:
            sid = sig.get("signal_id")
            if sid in open_signal_ids:
                continue  # already open
            try:
                if await open_position(sig):
                    summary["opened"] += 1
            except Exception as exc:
                summary["errors"] += 1
                logger.warning("paper_book: open %s failed: %s", sid, exc)
    except Exception as exc:
        summary["errors"] += 1
        logger.warning("paper_book: open pass failed: %s", exc)

    # ── Close pass ───────────────────────────────────────────────────
    try:
        resolved = await _fetch_resolved_signals(limit)
        open_positions = await _fetch_open_positions()
        pos_by_signal: dict[str, dict[str, Any]] = {}
        for p in open_positions:
            pos_by_signal[p.get("signal_id")] = p
        for sig in resolved:
            sid = sig.get("signal_id")
            pos = pos_by_signal.get(sid)
            if not pos:
                continue  # no open position for this signal
            try:
                if await close_position(
                    sig, pos.get("position_id"),
                    size_units=float(pos.get("size_units") or 0.0),
                ):
                    summary["closed"] += 1
            except Exception as exc:
                summary["errors"] += 1
                logger.warning("paper_book: close %s failed: %s", sid, exc)
    except Exception as exc:
        summary["errors"] += 1
        logger.warning("paper_book: close pass failed: %s", exc)

    logger.info(
        "paper_book: reconcile done — opened=%d closed=%d errors=%d",
        summary["opened"], summary["closed"], summary["errors"],
    )
    return summary


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", force=True)
    result = await reconcile()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
