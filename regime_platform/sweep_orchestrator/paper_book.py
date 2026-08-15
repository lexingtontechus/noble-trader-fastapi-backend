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
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("regime.sweep.paper_book")

# Sizing config (env-tunable)
BASE_NOTIONAL = float(os.environ.get("NOBLE_PAPER_BASE_NOTIONAL", "1000"))
MAX_FRACTION = 0.20  # hard cap: max 20% of base notional per position

# Max age for an open paper position (M4). A position whose signal never
# resolves (or whose resolved row was deleted) would otherwise sit open
# forever. After PAPER_OPEN_MAX_AGE_DAYS the close pass force-expires it
# with $0 PnL (the signal outcome convention for a timeout).
PAPER_OPEN_MAX_AGE_DAYS = float(os.environ.get("NOBLE_PAPER_OPEN_MAX_AGE_DAYS", "7"))

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


async def _lookup_signal_id(symbol: Any, ts: Any) -> Any:
    """Look up the nt_signal_sim signal_id for (symbol, ts).

    The capture payload does not carry signal_id (log_signal_sim generates
    the UUID at insert), so the paper book resolves it from the ledger by
    matching symbol + timestamp. Returns the signal_id or None.
    """
    if not symbol or not ts:
        return None
    try:
        from regime_platform.core.settings import settings
        import httpx
        url = (
            f"{settings.SUPABASE_URL}/rest/v1/nt_signal_sim"
            "?select=signal_id&symbol=eq.{}&ts=eq.{}&limit=1"
            .format(symbol, str(ts).replace("+00:00", "Z").replace(" ", "T"))
        )
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
            if isinstance(rows, list) and rows:
                return rows[0].get("signal_id")
    except Exception as exc:
        logger.warning("paper_book: signal_id lookup for %s failed: %s", symbol, exc)
    return None


async def _fetch_qualified_without_position(limit: int = 2000) -> list[dict[str, Any]]:
    """Qualified nt_signal_sim rows that have no paper position yet.

    L6: default raised 200 → 2000 — the open pass was capped at 200 rows
    per reconcile run while the capture rate is ~3,200 signals/day, so the
    book could starve (old qualified signals never opened). ``order=ts.asc``
    keeps the pass FIFO (open oldest first).
    """
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
    """Open nt_paper_positions rows (for close pass).

    NOTE: ``size_units`` is required by the close pass — the realized PnL
    formula is ``pnl_bricks × brick_size × size_units``; without it every
    close would book $0 (2026-08-08 fix).
    """
    try:
        from regime_platform.core.settings import settings
        import httpx
        url = (
            f"{settings.SUPABASE_URL}/rest/v1/nt_paper_positions"
            "?select=position_id,signal_id,status,size_units,open_ts"
            "&status=eq.open&order=open_ts.asc"
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


async def _fetch_resolved_signals(
    resolved_window_days: float = 14,
) -> list[dict[str, Any]]:
    """Resolved nt_signal_sim rows (outcome set) for the close pass.

    Fetches ALL signals resolved within the last ``resolved_window_days``
    days, ordered by ``resolved_at.desc``. This replaces the old
    ``order=ts.desc&limit=2000`` approach which only saw the newest 2000
    resolved-by-generation-time rows — at ~3,200 signals/day that was only
    ~12–15 hours of history, so positions opened >1 day ago with resolved
    signals were never matched → ``closed=0`` (the M4 orphan bug, 2026-08-15).

    Filtering on ``resolved_at`` (the timestamp the EOD resolver writes when
    it sets the outcome) guarantees every signal that *could* close an open
    position is in scope, regardless of when it was originally generated.

    The default 14-day window covers two full EOD lookback cycles and the
    7-day retention expiry, so it always finds every resolvable signal
    before retention prunes it.
    """
    try:
        from regime_platform.core.settings import settings
        import httpx
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=resolved_window_days)
        ).isoformat()
        # URL-encode the cutoff: replace +00:00 with Z to avoid the + being
        # interpreted as a space in the query string (PostgREST 400 Bad Request).
        cutoff_encoded = cutoff.replace("+00:00", "Z")
        url = (
            f"{settings.SUPABASE_URL}/rest/v1/nt_signal_sim"
            "?select=signal_id,symbol,outcome,exit_price,hold_bars,"
            "pnl_bricks,pnl_dollars,brick_size,sl_bricks,resolved_at"
            "&outcome=in.(tp_hit,sl_hit,expired)"
            f"&or=(resolved_at.gte.{cutoff_encoded},resolved_at.is.null)"
            "&order=resolved_at.desc.nulls.last"
        )
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
            logger.info(
                "paper_book: _fetch_resolved_signals fetched %d resolved signals "
                "(resolved_at >= %s OR NULL, outcome in tp/sl/expired)",
                len(rows) if isinstance(rows, list) else 0,
                cutoff_encoded,
            )
            return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("paper_book: fetch resolved failed: %s", exc)
        return []


async def open_position(sig: dict[str, Any]) -> bool:
    """Open a paper position for one qualified signal (best-effort).

    ``sig`` is the nt_signal_sim payload from the capture path — it does
    NOT carry ``signal_id`` (log_signal_sim generates the UUID at insert).
    The actual signal_id is looked up from nt_signal_sim by (symbol, ts)
    so the paper position can be joined back to the signal ledger. If the
    lookup fails, the position is skipped (the reconcile pass will retry
    on the next run).
    """
    signal_id = sig.get("signal_id")
    if not signal_id:
        signal_id = await _lookup_signal_id(
            sig.get("symbol"), sig.get("ts"),
        )
    if not signal_id:
        logger.warning(
            "paper_book: no signal_id for %s ts=%s — skipping open",
            sig.get("symbol"), sig.get("ts"),
        )
        return False
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
        "signal_id": signal_id,
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
        # ── Realtime broadcast (Talaria dashboard) ─────────────────────
        # Fire-and-forget 'opened' event on realtime:paper. Never raises:
        # broadcast_paper swallows failures and skips silently when there
        # is no running event loop.
        from regime_platform.sweep_orchestrator.realtime_publisher import broadcast_paper
        broadcast_paper({
            "type": "opened",
            "symbol": sig.get("symbol"),
            "direction": sig.get("signal") or "buy",
            "size_units": sizing["size_units"],
            "entry_price": round(float(entry), 6),
            "ts": _signal_epoch_ts(sig),
        })
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
        # ── Realtime broadcast (Talaria dashboard) ─────────────────────
        # Fire-and-forget 'closed' event on realtime:paper (never raises).
        from regime_platform.sweep_orchestrator.realtime_publisher import broadcast_paper
        broadcast_paper({
            "type": "closed",
            "symbol": sig.get("symbol"),
            "direction": sig.get("signal") or sig.get("direction"),
            "size_units": size_units,
            "exit_price": round(float(sig["exit_price"]), 6) if sig.get("exit_price") is not None else None,
            "realized_pnl": pnl["realized_pnl"],
            "r_multiple": pnl["r_multiple"],
            "ts": _signal_epoch_ts(sig),
        })
    return ok


def _signal_epoch_ts(sig: dict[str, Any]) -> float:
    """Epoch seconds from the signal's ISO ``ts``/``resolved_at`` (best-effort).

    Falls back to ``time.time()`` when the timestamp is missing or unparsable
    — a broadcast timestamp is telemetry, never worth raising over.
    """
    raw = sig.get("ts") or sig.get("resolved_at")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return time.time()


async def reconcile(
    limit: int = 2000,
    close_limit: int = 2000,
    resolved_window_days: float = 14,
) -> dict[str, Any]:
    """Idempotent open+close pass over the paper book.

    1. **Open pass**: qualified signals (outcome IS NULL) without a position.
    2. **Close pass**: iterates OPEN positions and batch-looks-up their signals
       by ``signal_id`` to find resolved ones (outcome set) — correct regardless
       of signal age (fixes the M4 orphan bug where the old
       ``ts.desc&limit=2000`` window missed 9-day-old resolved signals).

    ``limit`` bounds the OPEN pass (qualified signals to open) while
    ``close_limit`` is **deprecated** (kept for backward compat with the
    orchestrator call at line 1098: ``reconcile()`` — it is no longer used by
    the close pass, which instead uses ``resolved_window_days`` to filter the
    batch signal lookup).

    They were previously the same value — the close pass inherited the
    open-pass 200 and only ever saw the 200 oldest resolved signals
    (2026-08-08 fix). L6: open default raised to 2000 so the open pass
    keeps pace with the ~3,200 signals/day capture rate.
    """
    summary: dict[str, Any] = {"opened": 0, "closed": 0, "errors": 0, "age_expired": 0}

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
    # Iterate OPEN positions and batch-lookup their signals by signal_id.
    # This is correct regardless of how old the signal is — the old approach
    # (iterating newest-2000 resolved signals ordered ts.desc) missed positions
    # whose signals were older than the fetch window (M4 orphan bug, 2026-08-15).
    try:
        open_positions = await _fetch_open_positions()
        open_signal_ids = [
            p.get("signal_id") for p in open_positions
            if p.get("signal_id")
        ]
        sig_by_id: dict[str, dict[str, Any]] = {}
        if open_signal_ids:
            # PostgREST in. filter: chunk at 500 UUIDs per request to stay
            # well under the URL-length and gateway limits (observed 400
            # on ~1000-UUID not.in payloads in eod_retention 2026-08-13).
            from regime_platform.core.settings import settings
            import httpx
            sig_url = (
                f"{settings.SUPABASE_URL}/rest/v1/nt_signal_sim"
                "?select=signal_id,symbol,outcome,exit_price,hold_bars,"
                "pnl_bricks,pnl_dollars,brick_size,sl_bricks,resolved_at"
                "&outcome=in.(tp_hit,sl_hit,expired)"
            )
            headers = {
                "apikey": settings.SUPABASE_KEY,
                "Authorization": f"Bearer {settings.SUPABASE_KEY}",
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                for i in range(0, len(open_signal_ids), 500):
                    chunk = open_signal_ids[i : i + 500]
                    in_filter = ",".join(str(sid) for sid in chunk)
                    params = {"signal_id": f"in.({in_filter})"}
                    resp = await client.get(sig_url, headers=headers, params=params)
                    resp.raise_for_status()
                    rows = resp.json()
                    if isinstance(rows, list):
                        for r in rows:
                            sid = r.get("signal_id")
                            if sid:
                                sig_by_id[str(sid)] = r
                    # Continue to next chunk (no break) — up to 912 open positions
                    # means 2 chunks of 500 each.
        for pos in open_positions:
            sid = pos.get("signal_id")
            sig = sig_by_id.get(sid)
            if not sig:
                continue  # signal not resolved yet, or not found — leave open
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

    # ── Age-expire pass (M4) ──────────────────────────────────────
    # Force-expire open positions that have sat past
    # PAPER_OPEN_MAX_AGE_DAYS. Their signal may never resolve (or its row
    # was deleted by retention), so the position would otherwise sit open
    # forever and skew the book.
    try:
        open_positions = await _fetch_open_positions()
        now_ts = datetime.now(timezone.utc)
        for p in open_positions:
            raw_open = p.get("open_ts")
            if not raw_open:
                continue
            try:
                open_dt = datetime.fromisoformat(str(raw_open).replace("Z", "+00:00"))
                if open_dt.tzinfo is None:
                    open_dt = open_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            age_days = (now_ts - open_dt).total_seconds() / 86400.0
            if age_days < PAPER_OPEN_MAX_AGE_DAYS:
                continue
            ok = await _patch_position(p.get("position_id"), {
                "status": "expired",
                "close_ts": now_ts.isoformat(),
                "exit_price": None,
                "realized_pnl": 0.0,
                "pnl_bricks": 0,
                "r_multiple": 0.0,
                "resolved_at": now_ts.isoformat(),
            })
            if ok:
                summary["age_expired"] += 1
                logger.info(
                    "paper_book: AGE-EXPIRE %s %s (open %.1fd > %.0fd cap)",
                    p.get("symbol"), p.get("position_id"), age_days,
                    PAPER_OPEN_MAX_AGE_DAYS,
                )
    except Exception as exc:
        summary["errors"] += 1
        logger.warning("paper_book: age-expire pass failed: %s", exc)

    logger.info(
        "paper_book: reconcile done — opened=%d closed=%d age_expired=%d errors=%d",
        summary["opened"], summary["closed"], summary["age_expired"],
        summary["errors"],
    )
    return summary


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", force=True)
    result = await reconcile()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
